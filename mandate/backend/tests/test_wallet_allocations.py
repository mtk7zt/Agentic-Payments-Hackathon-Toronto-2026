import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.core.ledger import Ledger, LedgerError
from backend.core.policy import Intent, Policy
from backend.services import orchestrator
from backend.services.payment_rail import MockRail
from backend.services.seller import MockSeller

POLICY = Policy(task_budget_cap=100, max_single_spend=100, whitelisted_sellers={'seller.example': '0xa1'})


def reserve(ledger, key, amount=40):
    purchase, _ = ledger.propose(request_key=key, intent=Intent('seller.example', '0xa1', key, amount), tool_call={})
    ledger.evaluate_and_reserve(purchase['id'])
    return purchase['id']


def test_mismatch_persists_blocks_rail_and_matching_observation_recovers(tmp_path):
    path = str(tmp_path / 'ledger.db')
    ledger = Ledger(path, policy=POLICY)
    ledger.configure_wallet(100, 'owner: block 10')
    pid = reserve(ledger, 'wallet-request')
    ledger.record_wallet_observation(99, 'owner: block 11')
    ledger.conn.close()
    ledger = Ledger(path)
    ledger.set_policy(max_single_spend=90)
    rail = MockRail()
    before = ledger.events()
    with pytest.raises(LedgerError, match='wallet'):
        orchestrator.execute(ledger, rail, MockSeller(), pid)
    assert rail.create_calls == 0
    assert ledger.purchase(pid)['stage'] == 'RESERVED'
    assert ledger.events() == before
    assert ledger.wallet_status()['mismatch'] is True
    assert ledger.balances() == {'budget': 100, 'held': 40, 'settled': 0, 'available': 60}
    ledger.record_wallet_observation(100, 'owner: corrected block 12')
    orchestrator.execute(ledger, rail, MockSeller(), pid)
    assert rail.create_calls == 1
    assert ledger.wallet_status()['expected_milli'] == 60
    ledger.record_wallet_observation(60, 'owner: block 13')
    assert ledger.wallet_status()['dispatch_blocked'] is False
    assert ledger.verify_chain()['intact'] is True


def test_reconciliation_is_audited_and_refuses_unresolved_payments():
    ledger = Ledger(policy=POLICY)
    ledger.configure_wallet(100, 'owner: opening')
    ledger.record_wallet_observation(150, 'owner: deposit')
    assert ledger.wallet_status()['dispatch_blocked'] is True
    ledger.reconcile_wallet(150, 'owner: deposit verified')
    assert ledger.wallet_status()['dispatch_blocked'] is False
    pid = reserve(ledger, 'pending-request')
    ledger.claim_dispatch(pid)
    ledger.record_wallet_observation(110, 'owner: debit before callback')
    with pytest.raises(LedgerError, match='unresolved'):
        ledger.reconcile_wallet(110, 'owner: unsafe')
    ledger.mark_unknown(pid, 'timeout')
    with pytest.raises(LedgerError, match='unresolved'):
        ledger.reconcile_wallet(110, 'owner: unsafe')
    ledger.settle(pid, 'order-1')
    ledger.record_wallet_observation(110, 'owner: settled')
    assert ledger.wallet_status()['dispatch_blocked'] is False
    events = [e for e in ledger.events() if e['type'].startswith('WALLET_')]
    assert [e['type'] for e in events] == ['WALLET_CONFIGURED', 'WALLET_OBSERVED', 'WALLET_RECONCILED',
                                          'WALLET_OBSERVED', 'WALLET_OBSERVED']
    assert all(e['actor'] == 'owner' and e['ref'] for e in events)


def test_policy_increase_cannot_exceed_monitored_wallet():
    ledger = Ledger(policy=POLICY)
    ledger.configure_wallet(100, 'owner: opening')
    pid = reserve(ledger, 'policy-request')
    ledger.set_policy(task_budget_cap=101)
    assert ledger.wallet_status()['dispatch_blocked'] is True
    with pytest.raises(LedgerError, match='wallet'):
        ledger.claim_dispatch(pid)
    ledger.reconcile_wallet(101, 'owner: deposit')
    assert ledger.claim_dispatch(pid)


@pytest.mark.parametrize('method', ['configure_wallet', 'record_wallet_observation', 'reconcile_wallet'])
@pytest.mark.parametrize('bad', [True, 1.0, '100', -1, 2**53])
def test_wallet_money_is_strict_without_writing_audit(method, bad):
    ledger = Ledger(policy=POLICY)
    if method != 'configure_wallet':
        ledger.configure_wallet(100, 'owner: baseline')
    before = ledger.events()
    with pytest.raises(LedgerError):
        getattr(ledger, method)(bad, 'owner: invalid')
    assert ledger.events() == before


def test_configuration_cannot_be_overwritten_and_requires_provenance():
    ledger = Ledger(policy=POLICY)
    with pytest.raises(LedgerError):
        ledger.configure_wallet(100, '   ')
    ledger.configure_wallet(100, 'owner: baseline')
    with pytest.raises(LedgerError):
        ledger.configure_wallet(500, 'owner: bypass')
    assert ledger.wallet_status()['expected_milli'] == 100


def test_separate_connections_serialize_eighty_milli_reservations(tmp_path):
    path = str(tmp_path / 'concurrent.db')
    first = Ledger(path, policy=POLICY)
    second = Ledger(path)
    ids = [led.propose(request_key=f'concurrent-{n}', intent=Intent('seller.example', '0xa1', str(n), 80),
                       tool_call={})[0]['id'] for n, led in enumerate([first, second])]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda pair: pair[0].evaluate_and_reserve(pair[1]), zip([first, second], ids)))
    assert sorted(first.purchase(pid)['stage'] for pid in ids) == ['BLOCKED', 'RESERVED']
    assert first.balances() == {'budget': 100, 'held': 80, 'settled': 0, 'available': 20}
    with pytest.raises(LedgerError):
        first.set_policy(task_budget_cap=79)
    assert len([e for e in first.events() if e['type'] == 'BUDGET_RESERVED']) == 1


def test_reservation_and_event_roll_back_together():
    ledger = Ledger(policy=POLICY)
    purchase, _ = ledger.propose(request_key='rollback-reserve', intent=Intent('seller.example', '0xa1', 'item', 80),
                                 tool_call={})
    ledger.conn.execute("CREATE TRIGGER fail_reserve BEFORE INSERT ON events WHEN NEW.type='BUDGET_RESERVED' "
                        "BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END")
    before = ledger.events()
    with pytest.raises(sqlite3.IntegrityError):
        ledger.evaluate_and_reserve(purchase['id'])
    assert ledger.balances() == {'budget': 100, 'held': 0, 'settled': 0, 'available': 100}
    assert ledger.purchase(purchase['id'])['stage'] == 'PROPOSED'
    assert ledger.events() == before


def test_another_connection_observation_is_seen_at_claim(tmp_path):
    path = str(tmp_path / 'shared-wallet.db')
    dispatcher = Ledger(path, policy=POLICY)
    dispatcher.configure_wallet(100, 'owner: opening')
    pid = reserve(dispatcher, 'stale-dispatcher')
    assert dispatcher.wallet_status()['dispatch_blocked'] is False
    observer = Ledger(path)
    observer.record_wallet_observation(99, 'owner: external withdrawal')
    with pytest.raises(LedgerError, match='wallet'):
        dispatcher.claim_dispatch(pid)
    assert dispatcher.conn.execute('SELECT COUNT(*) FROM dispatch_claims').fetchone()[0] == 0


def test_settled_and_held_budget_floor_and_wallet_audit_rollback():
    ledger = Ledger(policy=POLICY)
    ledger.configure_wallet(100, 'owner: opening')
    pid = reserve(ledger, 'settled-first')
    ledger.claim_dispatch(pid)
    ledger.settle(pid, 'order-1')
    reserve(ledger, 'held-second')
    before = ledger.balances()
    with pytest.raises(LedgerError):
        ledger.set_policy(task_budget_cap=79)
    assert ledger.balances() == before == {'budget': 100, 'held': 40, 'settled': 40, 'available': 20}
    ledger.conn.execute("CREATE TRIGGER fail_wallet BEFORE INSERT ON events WHEN NEW.type='WALLET_OBSERVED' "
                        "BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.record_wallet_observation(1, 'owner: uncommitted mismatch')
    assert ledger.wallet_status()['mismatch'] is False
    assert ledger.wallet_status()['observed_milli'] == 100
