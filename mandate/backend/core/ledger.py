"""Durable ledger: atomic reservations, dispatch claims and a hash-linked event log.

Every public method runs inside one `BEGIN IMMEDIATE` SQLite transaction, so a
reservation and its evidence commit together or not at all (MND-08), and a
dispatch claim is on disk before any money-moving call is made (MND-09).
Pass ":memory:" for an in-process ledger with the same guarantees.

Invariant, checked before every commit:  available + held + settled = budget.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from enum import Enum
from typing import Any

from ..models.money import MoneyError, validate_milli
from .policy import Clause, Decision, Evaluation, Intent, Policy, evaluate
from .sessions import SessionKeys, SessionState

GENESIS = "0" * 64


class LedgerError(RuntimeError):
    pass


class Stage(str, Enum):
    PROPOSED = "PROPOSED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    RESERVED = "RESERVED"
    DISPATCHED = "DISPATCHED"
    SETTLED = "SETTLED"  # paid, delivery not yet verified
    VERIFIED = "VERIFIED"  # paid and delivery verified
    BLOCKED = "BLOCKED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    RECOVERY_UNKNOWN = "RECOVERY_UNKNOWN"  # outcome unknown; hold retained
    RECOVERY_UNDELIVERED = "RECOVERY_UNDELIVERED"  # paid; delivery failed


HOLDING = (Stage.PENDING_APPROVAL, Stage.RESERVED, Stage.DISPATCHED, Stage.RECOVERY_UNKNOWN)
PAID = (Stage.SETTLED, Stage.VERIFIED, Stage.RECOVERY_UNDELIVERED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS mandate (
  id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, policy_json TEXT NOT NULL,
  held INTEGER NOT NULL DEFAULT 0 CHECK (held >= 0),
  settled INTEGER NOT NULL DEFAULT 0 CHECK (settled >= 0)
);
CREATE TABLE IF NOT EXISTS purchases (
  id TEXT PRIMARY KEY, mandate_id TEXT NOT NULL, request_key TEXT NOT NULL UNIQUE,
  fingerprint TEXT NOT NULL, seller_domain TEXT NOT NULL, seller_address TEXT NOT NULL,
  item TEXT NOT NULL, amount INTEGER NOT NULL CHECK (amount > 0), attempt INTEGER NOT NULL,
  tool_call_json TEXT NOT NULL, stage TEXT NOT NULL, decision TEXT, clause TEXT,
  policy_version INTEGER, policy_hash TEXT, session_key_id TEXT, expires_at REAL,
  approved_by_human INTEGER NOT NULL DEFAULT 0, rail_order_id TEXT, delivery TEXT NOT NULL DEFAULT 'NONE',
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS dispatch_claims (
  idempotency_key TEXT PRIMARY KEY, purchase_id TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL CHECK (state IN ('CLAIMED','SETTLED','UNKNOWN','UNPAID')),
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY, ts REAL NOT NULL, type TEXT NOT NULL, actor TEXT NOT NULL,
  purchase_id TEXT, amount INTEGER, clause TEXT, ref TEXT, detail TEXT NOT NULL,
  policy_hash TEXT NOT NULL, prev TEXT NOT NULL, hash TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS session_keys (
  key_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, token_sha256 TEXT NOT NULL UNIQUE, state TEXT NOT NULL
);
-- The application role may append evidence but never rewrite it.
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""


def canonical(body: dict[str, Any]) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def event_hash(prev: str, body: dict[str, Any]) -> str:
    return hashlib.sha256((prev + canonical(body)).encode()).hexdigest()


class Ledger:
    def __init__(self, path: str = ":memory:", mandate_id: str = "mnd_acme_04", agent_id: str = "agent04",
                 policy: Policy | None = None):
        self.mandate_id = mandate_id
        self.agent_id = agent_id
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.sessions = SessionKeys(self.conn)
        if not self.conn.execute("SELECT 1 FROM mandate WHERE id=?", (mandate_id,)).fetchone():
            with self._tx():
                pol = policy or Policy()
                self.conn.execute("INSERT INTO mandate(id, agent_id, policy_json) VALUES (?,?,?)",
                                  (mandate_id, agent_id, pol.canonical()))
                self._emit("POLICY_SNAPSHOT", "owner", detail=f"Policy v{pol.version} snapshot")

    # ───────── transaction plumbing ─────────

    @contextmanager
    def _tx(self) -> Iterator[None]:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._check()
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def _check(self) -> None:
        m = self._mandate()
        pol = self.policy()
        held = self.conn.execute(
            f"SELECT COALESCE(SUM(amount),0) FROM purchases WHERE stage IN ({','.join('?' * len(HOLDING))})",
            [s.value for s in HOLDING]).fetchone()[0]
        settled = self.conn.execute(
            f"SELECT COALESCE(SUM(amount),0) FROM purchases WHERE stage IN ({','.join('?' * len(PAID))})",
            [s.value for s in PAID]).fetchone()[0]
        if held != m["held"] or settled != m["settled"]:
            raise LedgerError("invariant: held/settled do not match purchases")
        if pol.task_budget_cap - m["held"] - m["settled"] < 0:
            raise LedgerError("invariant: allocation overdrawn")

    def _mandate(self) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM mandate WHERE id=?", (self.mandate_id,)).fetchone()

    def _emit(self, type_: str, actor: str, *, purchase_id: str | None = None, amount: int | None = None,
              clause: str | None = None, ref: str | None = None, detail: str = "") -> None:
        last = self.conn.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        prev = last["hash"] if last else GENESIS
        body = {
            "seq": (last["seq"] if last else 0) + 1, "ts": round(time.time(), 3), "type": type_, "actor": actor,
            "purchase_id": purchase_id, "amount": amount, "clause": clause, "ref": ref, "detail": detail,
            "policy_hash": self.policy().hash(),
        }
        self.conn.execute(
            "INSERT INTO events(seq,ts,type,actor,purchase_id,amount,clause,ref,detail,policy_hash,prev,hash) "
            "VALUES (:seq,:ts,:type,:actor,:purchase_id,:amount,:clause,:ref,:detail,:policy_hash,:prev,:hash)",
            {**body, "prev": prev, "hash": event_hash(prev, body)})

    def _purchase(self, purchase_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM purchases WHERE id=?", (purchase_id,)).fetchone()
        if not row:
            raise LedgerError(f"unknown purchase {purchase_id}")
        return row

    def _stage(self, purchase_id: str, stage: Stage, **cols: Any) -> None:
        cols = {"stage": stage.value, "updated_at": time.time(), **cols}
        sets = ", ".join(f"{k}=?" for k in cols)
        self.conn.execute(f"UPDATE purchases SET {sets} WHERE id=?", [*cols.values(), purchase_id])

    # ───────── reads ─────────

    def policy(self) -> Policy:
        return Policy(**json.loads(self._mandate()["policy_json"]))

    def balances(self) -> dict[str, int]:
        m, pol = self._mandate(), self.policy()
        return {"budget": pol.task_budget_cap, "held": m["held"], "settled": m["settled"],
                "available": pol.task_budget_cap - m["held"] - m["settled"]}

    def purchase(self, purchase_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._purchase(purchase_id))

    def purchases(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM purchases ORDER BY created_at DESC")]

    def events(self, after_seq: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM events WHERE seq>? ORDER BY seq", (after_seq,))]

    def verify_chain(self) -> dict[str, Any]:
        """Re-hash every event. Detects edits, reordering and gaps."""
        prev, n = GENESIS, 0
        for e in self.events():
            n += 1
            body = {k: e[k] for k in ("seq", "ts", "type", "actor", "purchase_id", "amount", "clause", "ref",
                                      "detail", "policy_hash")}
            if e["seq"] != n or e["prev"] != prev or event_hash(prev, body) != e["hash"]:
                return {"intact": False, "first_bad_seq": e["seq"], "events": n}
            prev = e["hash"]
        return {"intact": True, "head": prev, "events": n}

    # ───────── owner ─────────

    def set_policy(self, **changes: Any) -> Policy:
        with self._tx():
            cur = self.policy()
            new = replace(cur, **changes, version=cur.version + 1)
            m = self._mandate()
            if new.task_budget_cap < m["held"] + m["settled"]:
                raise LedgerError("budget cannot drop below held + settled")
            self.conn.execute("UPDATE mandate SET policy_json=? WHERE id=?", (new.canonical(), self.mandate_id))
            self._emit("POLICY_SNAPSHOT", "owner", detail=f"Policy v{new.version} snapshot")
            return new

    def issue_session_key(self) -> tuple[str, str]:
        with self._tx():
            key_id, token = self.sessions.issue(self.agent_id)
            self._emit("SESSION_KEY_ISSUED", "owner", ref=key_id, detail=f"{key_id} issued")
            return key_id, token

    def engage_kill_switch(self) -> list[str]:
        """Pause the mandate and revoke every active key, in one transaction."""
        with self._tx():
            cur = self.policy()
            self.conn.execute("UPDATE mandate SET policy_json=? WHERE id=?",
                              (replace(cur, paused=True, version=cur.version + 1).canonical(), self.mandate_id))
            self._emit("AGENT_FROZEN", "owner", clause=Clause.AGENT_FROZEN.value,
                       detail="Kill-switch engaged. No new dispatch claims.")
            revoked = self.sessions.active_key_ids(self.agent_id)
            for key_id in revoked:
                self.sessions.set_state(key_id, SessionState.REVOKED)
                self._emit("SESSION_KEY_REVOKED", "owner", clause=Clause.AGENT_FROZEN.value, ref=key_id,
                           detail=f"{key_id} revoked by kill-switch")
            return revoked

    def release_kill_switch(self) -> None:
        with self._tx():
            cur = self.policy()
            self.conn.execute("UPDATE mandate SET policy_json=? WHERE id=?",
                              (replace(cur, paused=False, version=cur.version + 1).canonical(), self.mandate_id))
            self._emit("AGENT_RESUMED", "owner", detail="Owner released the lockdown")

    # ───────── purchase lifecycle ─────────

    def propose(self, *, request_key: str, intent: Intent, tool_call: dict[str, Any],
                session_key_id: str | None = None) -> tuple[dict[str, Any], bool]:
        """Idempotent on request_key. Same body returns the prior purchase; a changed body is refused."""
        try:
            validate_milli(intent.amount, positive=True)
        except MoneyError as exc:
            raise LedgerError(str(exc)) from exc
        fingerprint = hashlib.sha256(canonical({
            "seller_domain": intent.seller_domain.lower(), "seller_address": intent.seller_address.lower(),
            "item": intent.item, "amount": intent.amount}).encode()).hexdigest()
        with self._tx():
            prior = self.conn.execute("SELECT * FROM purchases WHERE request_key=?", (request_key,)).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise LedgerError("request_key reused with a different purchase")
                return dict(prior), False
            n = self.conn.execute("SELECT COUNT(*) FROM purchases").fetchone()[0] + 1
            pid = f"pur_{n:04d}"
            attempt = 1 + self.conn.execute(
                "SELECT COUNT(*) FROM purchases WHERE item=? AND stage IN (?,?)",
                (intent.item, Stage.RECOVERY_UNKNOWN.value, Stage.RECOVERY_UNDELIVERED.value)).fetchone()[0]
            now = time.time()
            self.conn.execute(
                "INSERT INTO purchases(id,mandate_id,request_key,fingerprint,seller_domain,seller_address,item,amount,"
                "attempt,tool_call_json,stage,session_key_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, self.mandate_id, request_key, fingerprint, intent.seller_domain.lower(),
                 intent.seller_address.lower(), intent.item, intent.amount, attempt, canonical(tool_call),
                 Stage.PROPOSED.value, session_key_id, now, now))
            self._emit("INTENT_PROPOSED", "agent", purchase_id=pid, amount=intent.amount, ref=session_key_id,
                       detail=f"{tool_call.get('tool', 'tool')} -> {intent.seller_domain}")
            return dict(self._purchase(pid)), True

    def evaluate_and_reserve(self, purchase_id: str, approval_window_s: int = 900) -> Evaluation:
        """Policy check and, if it passes, the hold. One transaction."""
        with self._tx():
            p = self._purchase(purchase_id)
            if p["stage"] != Stage.PROPOSED.value:
                raise LedgerError("already evaluated")
            pol = self.policy()
            ev = evaluate(Intent(p["seller_domain"], p["seller_address"], p["item"], p["amount"], p["attempt"]),
                          pol, self.balances()["available"])
            base = {"decision": ev.decision.value, "clause": ev.clause.value,
                    "policy_version": ev.policy_version, "policy_hash": ev.policy_hash}
            self._emit("POLICY_EVALUATED", "gateway", purchase_id=purchase_id, amount=p["amount"],
                       clause=ev.clause.value, detail=f"{ev.decision.value} · {ev.clause.value}")

            if ev.decision is Decision.BLOCKED:
                self._stage(purchase_id, Stage.BLOCKED, **base)
                self._emit("PURCHASE_BLOCKED", "gateway", purchase_id=purchase_id, amount=0, clause=ev.clause.value,
                           detail="Dropped before checkout. 0 calls to the rail.")
                redirect = ev.clause in (Clause.VENDOR_NOT_WHITELISTED, Clause.VENDOR_ADDRESS_MISMATCH)
                if redirect and p["session_key_id"]:
                    try:
                        self.sessions.set_state(p["session_key_id"], SessionState.REVOKED)
                        self._emit("SESSION_KEY_REVOKED", "gateway", purchase_id=purchase_id, clause=ev.clause.value,
                                   ref=p["session_key_id"], detail="Revoked after an out-of-mandate seller")
                    except KeyError:
                        pass
                return ev

            self.conn.execute("UPDATE mandate SET held=held+? WHERE id=?", (p["amount"], self.mandate_id))
            self._emit("BUDGET_RESERVED", "gateway", purchase_id=purchase_id, amount=p["amount"],
                       detail="Hold placed")
            if ev.decision is Decision.HELD_FOR_REVIEW:
                self._stage(purchase_id, Stage.PENDING_APPROVAL, expires_at=time.time() + approval_window_s, **base)
                self._emit("APPROVAL_REQUESTED", "gateway", purchase_id=purchase_id, amount=p["amount"],
                           clause=ev.clause.value, detail="Waiting for a person")
            else:
                self._stage(purchase_id, Stage.RESERVED, **base)
            return ev

    def approve(self, purchase_id: str) -> None:
        with self._tx():
            p = self._purchase(purchase_id)
            if p["stage"] != Stage.PENDING_APPROVAL.value:
                raise LedgerError("not awaiting approval")
            if self.policy().paused:
                raise LedgerError("kill-switch engaged")
            if (p["expires_at"] or 0) <= time.time():
                raise LedgerError("approval window closed")
            self._stage(purchase_id, Stage.RESERVED, approved_by_human=1)
            self._emit("APPROVAL_GRANTED", "approver", purchase_id=purchase_id, amount=p["amount"],
                       detail="Authorized for this exact seller, item and amount")

    def release(self, purchase_id: str, *, expired: bool = False) -> None:
        with self._tx():
            p = self._purchase(purchase_id)
            if p["stage"] != Stage.PENDING_APPROVAL.value:
                raise LedgerError("not awaiting approval")
            self.conn.execute("UPDATE mandate SET held=held-? WHERE id=?", (p["amount"], self.mandate_id))
            self._stage(purchase_id, Stage.EXPIRED if expired else Stage.REJECTED)
            self._emit("APPROVAL_EXPIRED" if expired else "APPROVAL_REJECTED", "gateway" if expired else "approver",
                       purchase_id=purchase_id, amount=p["amount"], detail="No money moved")
            self._emit("HOLD_RELEASED", "gateway", purchase_id=purchase_id, amount=p["amount"], detail="Hold released")

    def claim_dispatch(self, purchase_id: str) -> str:
        """Record intent to pay BEFORE calling the rail. Exactly one claim per purchase."""
        with self._tx():
            p = self._purchase(purchase_id)
            if p["stage"] != Stage.RESERVED.value:
                raise LedgerError("not reserved")
            if self.policy().paused:
                raise LedgerError("kill-switch engaged")
            key = f"mnd-{p['fingerprint'][:8]}-{p['id']}"
            try:
                self.conn.execute(
                    "INSERT INTO dispatch_claims(idempotency_key,purchase_id,state,created_at) VALUES (?,?,?,?)",
                    (key, purchase_id, "CLAIMED", time.time()))
            except sqlite3.IntegrityError as exc:
                raise LedgerError("dispatch already claimed") from exc
            self._stage(purchase_id, Stage.DISPATCHED)
            self._emit("DISPATCH_CLAIMED", "executor", purchase_id=purchase_id, amount=p["amount"], ref=key,
                       detail="Claim recorded before calling the rail")
            return key

    def settle(self, purchase_id: str, rail_order_id: str) -> bool:
        """Move the hold to settled exactly once. A repeat is recorded and ignored."""
        with self._tx():
            p = self._purchase(purchase_id)
            claim = self.conn.execute("SELECT * FROM dispatch_claims WHERE purchase_id=?", (purchase_id,)).fetchone()
            if not claim:
                raise LedgerError("no dispatch claim")
            if claim["state"] == "SETTLED":
                self._emit("DUPLICATE_CALLBACK_IGNORED", "executor", purchase_id=purchase_id, amount=p["amount"],
                           ref=rail_order_id, detail="Second settlement ignored")
                return False
            if claim["state"] == "UNPAID":
                raise LedgerError("claim was closed as unpaid")
            self.conn.execute("UPDATE mandate SET held=held-?, settled=settled+? WHERE id=?",
                              (p["amount"], p["amount"], self.mandate_id))
            self.conn.execute("UPDATE dispatch_claims SET state='SETTLED' WHERE purchase_id=?", (purchase_id,))
            self._stage(purchase_id, Stage.SETTLED, rail_order_id=rail_order_id, delivery="PENDING")
            self._emit("PAYMENT_SETTLED", "executor", purchase_id=purchase_id, amount=p["amount"], ref=rail_order_id,
                       detail="Hold moved to settled once")
            return True

    def attach_order(self, purchase_id: str, rail_order_id: str) -> None:
        """Remember the rail's order id for a dispatched purchase that is not settled yet."""
        with self._tx():
            if self._purchase(purchase_id)["stage"] != Stage.DISPATCHED.value:
                raise LedgerError("not dispatched")
            self.conn.execute("UPDATE purchases SET rail_order_id=?, updated_at=? WHERE id=?",
                              (rail_order_id, time.time(), purchase_id))

    def mark_unknown(self, purchase_id: str, why: str) -> None:
        """Rail did not answer. Keep the hold; forbid a blind retry."""
        with self._tx():
            p = self._purchase(purchase_id)
            if p["stage"] != Stage.DISPATCHED.value:
                raise LedgerError("not dispatched")
            self.conn.execute("UPDATE dispatch_claims SET state='UNKNOWN' WHERE purchase_id=?", (purchase_id,))
            self._stage(purchase_id, Stage.RECOVERY_UNKNOWN)
            self._emit("OUTCOME_UNKNOWN", "executor", purchase_id=purchase_id, amount=p["amount"],
                       detail=f"{why}. Hold retained; no blind retry.")

    def close_unpaid(self, purchase_id: str, why: str) -> None:
        """Rail proved no payment happened or can happen. Only then is the hold released."""
        with self._tx():
            p = self._purchase(purchase_id)
            if p["stage"] not in (Stage.DISPATCHED.value, Stage.RECOVERY_UNKNOWN.value):
                raise LedgerError("nothing to close")
            self.conn.execute("UPDATE dispatch_claims SET state='UNPAID' WHERE purchase_id=?", (purchase_id,))
            self.conn.execute("UPDATE mandate SET held=held-? WHERE id=?", (p["amount"], self.mandate_id))
            self._stage(purchase_id, Stage.EXPIRED)
            self._emit("HOLD_RELEASED", "executor", purchase_id=purchase_id, amount=p["amount"],
                       detail=f"Definitively unpaid ({why}). Hold released.")

    def record_delivery(self, purchase_id: str, ok: bool, detail: str) -> None:
        """Delivery is verified separately from, and after, settlement."""
        with self._tx():
            p = self._purchase(purchase_id)
            if p["stage"] not in (Stage.SETTLED.value, Stage.RECOVERY_UNDELIVERED.value):
                raise LedgerError("payment not settled")
            if ok:
                self._stage(purchase_id, Stage.VERIFIED, delivery="VERIFIED")
                self._emit("DELIVERY_VERIFIED", "seller", purchase_id=purchase_id, ref=p["rail_order_id"], detail=detail)
            else:
                self._stage(purchase_id, Stage.RECOVERY_UNDELIVERED, delivery="FAILED")
                self._emit("DELIVERY_FAILED", "seller", purchase_id=purchase_id, ref=p["rail_order_id"],
                           detail=f"{detail}. Payment stays settled.")
