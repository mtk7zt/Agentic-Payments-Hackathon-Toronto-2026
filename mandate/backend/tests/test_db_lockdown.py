"""Other database roles (Supabase anon/authenticated) can read nothing; the gateway is unaffected."""
import pytest

from backend.core.ledger import Ledger
from backend.core.policy import Intent, Policy
from backend.services import orchestrator
from backend.services.payment_rail import MockRail
from backend.services.seller import MockSeller


def test_gateway_still_reads_and_writes_after_lockdown():
    led = Ledger(policy=Policy(whitelisted_sellers={"s.example": "0xa1"}))
    p = orchestrator.submit(led, MockRail(), MockSeller(), request_key="k", intent=Intent("s.example", "0xa1", "i", 40),
                            tool_call={})
    assert p["stage"] == "VERIFIED" and led.verify_chain()["intact"]
    assert 5 in [r[0] for r in led.conn.execute("SELECT version FROM schema_migrations")]


def test_other_roles_are_shut_out_on_postgres():
    led = Ledger()
    db = led.conn
    if db.dialect != "postgresql":  # SQLite has no roles or network API; the migration is a recorded no-op
        assert 5 in [r[0] for r in db.execute("SELECT version FROM schema_migrations")]
        return
    if not db.execute("SELECT 1 FROM pg_roles WHERE rolname='anon'").fetchone():
        db.execute("CREATE ROLE anon NOLOGIN")
    schema = db.execute("SELECT current_schema()").fetchone()[0]
    db.execute(f"GRANT USAGE ON SCHEMA {schema} TO anon")
    tables = db.execute("SELECT relname, relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = current_schema() AND relkind = 'r'").fetchall()
    assert tables and all(r[1] for r in tables), tables
    db.execute("BEGIN")
    try:
        db.execute("SET LOCAL ROLE anon")
        with pytest.raises(Exception, match="permission denied"):
            db.execute("SELECT * FROM purchases")
    finally:
        db.execute("ROLLBACK")
