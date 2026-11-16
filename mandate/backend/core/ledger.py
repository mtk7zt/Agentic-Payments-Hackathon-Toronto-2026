"""Durable ledger: atomic reservations, dispatch claims and a hash-linked event log.

Every public method runs inside one `BEGIN IMMEDIATE` SQLite transaction, so a
reservation and its evidence commit together or not at all (MND-08), and a
dispatch claim is on disk before any money-moving call is made (MND-09).
Pass ":memory:" for an in-process ledger with the same guarantees.

Invariant, checked before every commit:  available + held + settled = budget.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
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
CREATE TABLE IF NOT EXISTS wallet_monitor (
  mandate_id TEXT PRIMARY KEY, baseline INTEGER NOT NULL, baseline_settled INTEGER NOT NULL,
  observed INTEGER NOT NULL, observed_settled INTEGER NOT NULL,
  mismatch INTEGER NOT NULL CHECK (mismatch IN (0,1)), reference TEXT NOT NULL,
  recorded_at REAL NOT NULL
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
CREATE TABLE IF NOT EXISTS rail_approvals (
  approval_id TEXT PRIMARY KEY, rail TEXT NOT NULL, dispatch_key TEXT NOT NULL,
  context_json TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN ('PENDING','RESOLVED')),
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY, ts INTEGER NOT NULL, type TEXT NOT NULL, actor TEXT NOT NULL,
  purchase_id TEXT, amount INTEGER, clause TEXT, ref TEXT, detail TEXT NOT NULL,
  policy_hash TEXT NOT NULL, prev TEXT NOT NULL, hash TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS session_keys (
  key_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, token_sha256 TEXT NOT NULL UNIQUE, state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_checkpoints (
  id TEXT PRIMARY KEY, mandate_id TEXT NOT NULL, seq INTEGER NOT NULL, checkpoint_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS checkpoints_no_update BEFORE UPDATE ON evidence_checkpoints
BEGIN SELECT RAISE(ABORT, 'checkpoints are append-only'); END;
CREATE TRIGGER IF NOT EXISTS checkpoints_no_delete BEFORE DELETE ON evidence_checkpoints
BEGIN SELECT RAISE(ABORT, 'checkpoints are append-only'); END;
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
                 policy: Policy | None = None, *, checkpoint_key: str | None = None, checkpoint_interval: int = 10):
        if type(checkpoint_interval) is not int or checkpoint_interval < 1:
            raise ValueError("checkpoint interval must be a positive integer")
        self._checkpoint_key = os.environ.get("MANDATE_CHECKPOINT_KEY", "") if checkpoint_key is None else checkpoint_key
        self._checkpoint_interval = checkpoint_interval
        self.mandate_id = mandate_id
        self.agent_id = agent_id
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.sessions = SessionKeys(self.conn)
        if not self.conn.execute("SELECT 1 FROM mandate WHERE id=?", (mandate_id,)).fetchone():
            with self._tx():
                pol = policy or Policy()
                self.conn.execute("INSERT INTO mandate(id, agent_id, policy_json) VALUES (?,?,?)",
                                  (mandate_id, agent_id, pol.canonical()))
                self._emit("POLICY_SNAPSHOT", "owner", detail=f"Policy v{pol.version} snapshot")

    # Columns added after the first release. Older databases get them on open; existing rows stay valid (NULL).
    _ADDED_PURCHASE_COLUMNS = (("rail", "TEXT"), ("approval_id", "TEXT"), ("rail_tx_id", "TEXT"),
                               ("approval_digest", "TEXT"))

    def _migrate(self) -> None:
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(purchases)")}
        for name, kind in self._ADDED_PURCHASE_COLUMNS:
            if name not in have:
                self.conn.execute(f"ALTER TABLE purchases ADD COLUMN {name} {kind}")

    # ───────── rail approvals (MND-32) ─────────
    # A rail can ask the owner to approve before it pays (Qatom does). The context needed to resume
    # that exact checkout is stored here, so a gateway restart cannot lose a pending approval.

    def save_rail_approval(self, approval_id: str, *, rail: str, dispatch_key: str, context: dict[str, Any]) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO rail_approvals(approval_id,rail,dispatch_key,context_json,state,created_at) "
                "VALUES (?,?,?,?, 'PENDING', ?)",
                (approval_id, rail, dispatch_key, canonical(context), time.time()))

    def rail_approval(self, approval_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM rail_approvals WHERE approval_id=? AND state='PENDING'",
                                    (approval_id,)).fetchone()
        return None if row is None else {**dict(row), "context": json.loads(row["context_json"])}

    def resolve_rail_approval(self, approval_id: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE rail_approvals SET state='RESOLVED' WHERE approval_id=?", (approval_id,))

    # ───────── approval binding (MND-10) ─────────

    def _approval_terms(self, p: sqlite3.Row, policy_version: int) -> dict[str, Any]:
        """What the owner is approving. Any change to these makes the approval invalid."""
        return {"purchase_id": p["id"], "mandate_id": self.mandate_id, "seller_domain": p["seller_domain"],
                "seller_address": p["seller_address"], "item": p["item"], "amount_milli": p["amount"],
                "arguments_sha256": hashlib.sha256(p["tool_call_json"].encode()).hexdigest(),
                "policy_version": policy_version}

    def _approval_digest(self, p: sqlite3.Row, policy_version: int) -> str:
        return hashlib.sha256(canonical(self._approval_terms(p, policy_version)).encode()).hexdigest()

    def approval_summary(self, purchase_id: str) -> dict[str, Any]:
        """The exact terms shown to the owner, plus the digest they must send back to approve."""
        with self._lock:
            p = self._purchase(purchase_id)
            if p["stage"] != Stage.PENDING_APPROVAL.value or not p["approval_digest"]:
                raise LedgerError("not awaiting approval")
            return {**self._approval_terms(p, p["policy_version"]), "digest": p["approval_digest"],
                    "expires_at": p["expires_at"]}

    def _check_binding(self, p: sqlite3.Row) -> None:
        """The stored digest must still describe this row under the current policy version."""
        if not p["approval_digest"]:
            return  # created before MND-10; the policy recheck still applies
        current = self.policy().version
        if current != p["policy_version"]:
            raise LedgerError(f"approval was requested under policy v{p['policy_version']}; policy is now v{current}")
        if self._approval_digest(p, current) != p["approval_digest"]:
            raise LedgerError("purchase changed after approval was requested")

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
        with self._lock:
            return self.conn.execute("SELECT * FROM mandate WHERE id=?", (self.mandate_id,)).fetchone()

    def snapshot(self):
        """Hold the ledger lock across several reads so they come from one consistent state.

        The ledger shares one SQLite connection between threads; every read and write goes
        through this re-entrant lock so overlapping requests never interleave on that connection.
        """
        return self._lock

    def _emit(self, type_: str, actor: str, *, purchase_id: str | None = None, amount: int | None = None,
              clause: str | None = None, ref: str | None = None, detail: str = "") -> None:
        last = self.conn.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        prev = last["hash"] if last else GENESIS
        # ts is integer milliseconds so Python and the browser serialize it identically when re-hashing.
        body = {
            "seq": (last["seq"] if last else 0) + 1, "ts": int(time.time() * 1000), "type": type_, "actor": actor,
            "purchase_id": purchase_id, "amount": amount, "clause": clause, "ref": ref, "detail": detail,
            "policy_hash": self.policy().hash(),
        }
        self.conn.execute(
            "INSERT INTO events(seq,ts,type,actor,purchase_id,amount,clause,ref,detail,policy_hash,prev,hash) "
            "VALUES (:seq,:ts,:type,:actor,:purchase_id,:amount,:clause,:ref,:detail,:policy_hash,:prev,:hash)",
            {**body, "prev": prev, "hash": event_hash(prev, body)})
        if self._checkpoint_key and body["seq"] % self._checkpoint_interval == 0:
            self._store_checkpoint()

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
        with self._lock:
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

    def events_page(self, *, after_seq: int = 0, limit: int = 100, purchase_id: str | None = None,
                    event_type: str | None = None, stage: str | None = None) -> list[dict[str, Any]]:
        """Ordered page of events. `stage` filters by the purchase's current stage."""
        sql, args = "SELECT e.* FROM events e LEFT JOIN purchases p ON p.id=e.purchase_id WHERE e.seq>?", [after_seq]
        if purchase_id:
            sql, args = sql + " AND e.purchase_id=?", [*args, purchase_id]
        if event_type:
            sql, args = sql + " AND e.type=?", [*args, event_type]
        if stage:
            sql, args = sql + " AND p.stage=?", [*args, stage]
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql + " ORDER BY e.seq LIMIT ?", [*args, limit])]

    def latest_checkpoint(self) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT checkpoint_json FROM evidence_checkpoints WHERE mandate_id=? ORDER BY seq DESC, rowid DESC LIMIT 1",
                (self.mandate_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def _store_checkpoint(self) -> dict[str, Any]:
        if not self._checkpoint_key:
            raise LedgerError("checkpoint key is not configured")
        if not self.verify_chain()["intact"]:
            raise LedgerError("cannot checkpoint a broken or untrusted chain")
        head = self.conn.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        body = {"schema": "mandate.checkpoint.v1", "mandate_id": self.mandate_id, "agent_id": self.agent_id,
                "seq": head["seq"] if head else 0, "head": head["hash"] if head else GENESIS,
                "issued_at_ms": time.time_ns() // 1_000_000,
                "key_id": hashlib.sha256(self._checkpoint_key.encode()).hexdigest()[:16]}
        body["ref"] = "sha256:" + hashlib.sha256(canonical(body).encode()).hexdigest()
        signature = hmac.new(self._checkpoint_key.encode(), canonical(body).encode(), hashlib.sha256).hexdigest()
        checkpoint = {**body, "signature": signature}
        self.conn.execute("INSERT OR IGNORE INTO evidence_checkpoints VALUES (?,?,?,?)",
                          (body["ref"], self.mandate_id, body["seq"], canonical(checkpoint)))
        return checkpoint

    def create_checkpoint(self) -> dict[str, Any]:
        with self._tx():
            return self._store_checkpoint()

    def verify_chain(self, checkpoint: dict[str, Any] | None = None) -> dict[str, Any]:
        """Re-hash every event. Detects edits, reordering and gaps."""
        with self._lock:
            return self._verify_chain(checkpoint)

    def _verify_chain(self, checkpoint: dict[str, Any] | None = None) -> dict[str, Any]:
        checkpoint = self.latest_checkpoint() if checkpoint is None else checkpoint
        prev, n = GENESIS, 0
        events = self.events()
        for e in events:
            n += 1
            body = {k: e[k] for k in ("seq", "ts", "type", "actor", "purchase_id", "amount", "clause", "ref",
                                      "detail", "policy_hash")}
            if e["seq"] != n or e["prev"] != prev or event_hash(prev, body) != e["hash"]:
                return {"intact": False, "first_bad_seq": e["seq"], "events": n}
            prev = e["hash"]
        result = {"intact": True, "head": prev, "events": n, "checkpoint_verified": False,
                  "checkpoint_ref": checkpoint.get("ref") if isinstance(checkpoint, dict) else None}
        if checkpoint is None:
            return result
        if not isinstance(checkpoint, dict):
            return {**result, "intact": False, "reason": "checkpoint_invalid"}
        body = {k: v for k, v in checkpoint.items() if k != "signature"}
        expected_fields = {"schema", "mandate_id", "agent_id", "seq", "head", "issued_at_ms", "key_id", "ref"}
        signature = checkpoint.get("signature")
        valid = (self._checkpoint_key and set(body) == expected_fields and isinstance(signature, str)
                 and len(signature) == 64 and all(c in "0123456789abcdef" for c in signature)
                 and hmac.compare_digest(hmac.new(self._checkpoint_key.encode(), canonical(body).encode(),
                                                 hashlib.sha256).hexdigest(), signature)
                 and body["schema"] == "mandate.checkpoint.v1" and body["mandate_id"] == self.mandate_id
                 and body["agent_id"] == self.agent_id and type(body["seq"]) is int and body["seq"] >= 0)
        if not valid:
            return {**result, "intact": False, "reason": "checkpoint_invalid"}
        seq = body["seq"]
        if seq > n:
            return {**result, "intact": False, "reason": "checkpoint_truncated", "first_bad_seq": n + 1}
        observed = events[seq - 1]["hash"] if seq else GENESIS
        if observed != body["head"]:
            return {**result, "intact": False, "reason": "checkpoint_head_mismatch", "first_bad_seq": seq}
        return {**result, "checkpoint_verified": True, "checkpoint_seq": seq}

    # ───────── owner ─────────

    def set_policy(self, *, expected_version: int | None = None, **changes: Any) -> Policy:
        """Owner HTTP writes supply expected_version; trusted bootstrap callers may omit it."""
        with self._tx():
            cur = self.policy()
            if expected_version is not None:
                if type(expected_version) is not int or expected_version < 1:
                    raise LedgerError("expected policy version must be a positive integer")
                if cur.version != expected_version:
                    raise LedgerError("policy version mismatch; refresh state before editing")
            new = replace(cur, **changes, version=cur.version + 1)
            m = self._mandate()
            committed = m["held"] + m["settled"]
            if new.task_budget_cap < committed:
                raise LedgerError(f"budget {new.task_budget_cap} is below the {committed} already held or spent "
                                  f"({m['held']} held + {m['settled']} settled, in thousandths)")
            self.conn.execute("UPDATE mandate SET policy_json=? WHERE id=?", (new.canonical(), self.mandate_id))
            self._emit("POLICY_SNAPSHOT", "owner", detail=f"Policy v{new.version} snapshot")
            return new

    def wallet_status(self) -> dict[str, Any]:
        """Owner-supplied snapshots, never a live provider balance verification."""
        with self._lock:
            row = self.conn.execute("SELECT * FROM wallet_monitor WHERE mandate_id=?",
                                    (self.mandate_id,)).fetchone()
            if not row:
                return {"configured": False, "mismatch": False, "dispatch_blocked": False,
                        "provenance": "unconfigured"}
            settled = self._mandate()["settled"]
            expected = row["baseline"] - (settled - row["baseline_settled"])
            coverage = expected >= self.policy().task_budget_cap - settled
            reconciled = self.conn.execute(
                "SELECT amount, ts FROM events WHERE type IN ('WALLET_CONFIGURED','WALLET_RECONCILED') "
                "ORDER BY seq DESC LIMIT 1").fetchone()
            return {"configured": True, "mismatch": bool(row["mismatch"]),
                    "dispatch_blocked": bool(row["mismatch"]) or not coverage,
                    "expected_milli": expected, "observed_milli": row["observed"],
                    "observed_at_settled_milli": row["observed_settled"],
                    "allocation_covered": coverage, "reference": row["reference"],
                    "recorded_at": row["recorded_at"], "provenance": "owner_recorded",
                    "last_reconciled_milli": reconciled["amount"] if reconciled else None,
                    "last_reconciled_at": reconciled["ts"] if reconciled else None}

    def _wallet_input(self, balance_milli: int, reference: str) -> None:
        try:
            validate_milli(balance_milli)
        except MoneyError as exc:
            raise LedgerError(str(exc)) from exc
        if not isinstance(reference, str) or not reference.strip() or len(reference) > 512:
            raise LedgerError("wallet observation requires a reference of 1 to 512 characters")

    def _wallet_baseline(self, balance_milli: int, reference: str, *, configure: bool) -> dict[str, Any]:
        self._wallet_input(balance_milli, reference)
        with self._tx():
            configured = self.wallet_status()["configured"]
            if configure == configured:
                raise LedgerError("wallet already configured" if configure else "wallet not configured")
            unresolved = self.conn.execute(
                "SELECT 1 FROM purchases WHERE mandate_id=? AND stage IN (?,?) LIMIT 1",
                (self.mandate_id, Stage.DISPATCHED.value, Stage.RECOVERY_UNKNOWN.value)).fetchone()
            if unresolved:
                raise LedgerError("wallet baseline cannot change with unresolved payments")
            settled = self._mandate()["settled"]
            self.conn.execute(
                "INSERT INTO wallet_monitor VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(mandate_id) DO UPDATE SET baseline=excluded.baseline, "
                "baseline_settled=excluded.baseline_settled, observed=excluded.observed, "
                "observed_settled=excluded.observed_settled, mismatch=0, "
                "reference=excluded.reference, recorded_at=excluded.recorded_at",
                (self.mandate_id, balance_milli, settled, balance_milli, settled, 0, reference, time.time()))
            self._emit("WALLET_CONFIGURED" if configure else "WALLET_RECONCILED", "owner",
                       amount=balance_milli, ref=reference,
                       detail=canonical({"provenance": "owner_recorded", "baseline_settled_milli": settled,
                                         "previously_configured": configured}))
            return self.wallet_status()

    def configure_wallet(self, balance_milli: int, reference: str) -> dict[str, Any]:
        """Establish a trusted owner baseline once; monitoring cannot be disabled."""
        return self._wallet_baseline(balance_milli, reference, configure=True)

    def reconcile_wallet(self, balance_milli: int, reference: str) -> dict[str, Any]:
        """Explicit owner acknowledgement of externally explained wallet movements."""
        return self._wallet_baseline(balance_milli, reference, configure=False)

    def record_wallet_observation(self, balance_milli: int, reference: str) -> dict[str, Any]:
        self._wallet_input(balance_milli, reference)
        with self._tx():
            status = self.wallet_status()
            if not status["configured"]:
                raise LedgerError("wallet not configured")
            mismatch = balance_milli != status["expected_milli"]
            self.conn.execute(
                "UPDATE wallet_monitor SET observed=?, observed_settled=?, mismatch=?, reference=?, recorded_at=? "
                "WHERE mandate_id=?",
                (balance_milli, self._mandate()["settled"], int(mismatch), reference, time.time(), self.mandate_id))
            self._emit("WALLET_OBSERVED", "owner", amount=balance_milli, ref=reference,
                       detail=canonical({"provenance": "owner_recorded", "expected_milli": status["expected_milli"],
                                         "mismatch": mismatch}))
            return self.wallet_status()

    def issue_session_key(self) -> tuple[str, str]:
        with self._tx():
            if self.policy().paused:
                raise LedgerError("cannot issue a session key while paused")
            key_id, token = self.sessions.issue(self.agent_id)
            self._emit("SESSION_KEY_ISSUED", "owner", ref=key_id, detail=f"{key_id} issued")
            return key_id, token

    def session_status(self) -> list[dict[str, str]]:
        """Owner-visible metadata, excluding tokens and their digests."""
        with self._lock:
            return [dict(row) for row in self.conn.execute(
                "SELECT key_id, agent_id, state FROM session_keys WHERE agent_id=? ORDER BY key_id",
                (self.agent_id,))]

    def change_session_state(self, key_id: str, state: SessionState) -> dict[str, str]:
        with self._tx():
            if not isinstance(state, SessionState):
                raise LedgerError("invalid session state")
            row = self.conn.execute(
                "SELECT key_id, agent_id, state FROM session_keys WHERE key_id=? AND agent_id=?",
                (key_id, self.agent_id)).fetchone()
            if not row:
                raise LedgerError("session key not found")
            if row["state"] == SessionState.REVOKED.value:
                raise LedgerError("session key is permanently revoked")
            if state is SessionState.ACTIVE and self.policy().paused:
                raise LedgerError("cannot activate a session key while paused")
            if row["state"] != state.value:
                self.sessions.set_state(key_id, state)
                event = {SessionState.ACTIVE: "SESSION_KEY_THAWED", SessionState.FROZEN: "SESSION_KEY_FROZEN",
                         SessionState.REVOKED: "SESSION_KEY_REVOKED"}[state]
                self._emit(event, "owner", ref=key_id, detail=f"Owner set session state to {state.value}")
            return {"key_id": key_id, "agent_id": self.agent_id, "state": state.value}

    def _require_active_session(self, key_id: str | None) -> None:
        # Trusted offline/bootstrap callers can omit a key; HTTP proposals cannot.
        if key_id is None:
            return
        row = self.conn.execute(
            "SELECT state FROM session_keys WHERE key_id=? AND agent_id=?", (key_id, self.agent_id)).fetchone()
        if not row or row["state"] != SessionState.ACTIVE.value:
            raise LedgerError("session key missing, frozen, revoked or belongs to another agent")

    def engage_kill_switch(self) -> list[str]:
        """Pause the mandate and revoke every non-revoked key, in one transaction."""
        with self._tx():
            cur = self.policy()
            self.conn.execute("UPDATE mandate SET policy_json=? WHERE id=?",
                              (replace(cur, paused=True, version=cur.version + 1).canonical(), self.mandate_id))
            self._emit("AGENT_FROZEN", "owner", clause=Clause.AGENT_FROZEN.value,
                       detail="Kill-switch engaged. No new dispatch claims. Already dispatched payments may still complete.")
            revoked = [row["key_id"] for row in self.session_status() if row["state"] != SessionState.REVOKED.value]
            for key_id in revoked:
                self.sessions.set_state(key_id, SessionState.REVOKED)
                self._emit("SESSION_KEY_REVOKED", "owner", clause=Clause.AGENT_FROZEN.value, ref=key_id,
                           detail=f"{key_id} revoked by kill-switch")
            return revoked

    def release_kill_switch(self) -> None:
        with self._tx():
            cur = self.policy()
            unresolved = self.conn.execute(
                "SELECT 1 FROM purchases WHERE mandate_id=? AND stage IN (?,?) LIMIT 1",
                (self.mandate_id, Stage.DISPATCHED.value, Stage.RECOVERY_UNKNOWN.value)).fetchone()
            if unresolved:
                raise LedgerError("cannot resume with unresolved payments; reconcile the original orders first")
            if self.wallet_status()["dispatch_blocked"]:
                raise LedgerError("wallet must be reconciled before resume")
            if not cur.paused:
                return
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
        try:
            if not isinstance(tool_call, dict):
                raise TypeError("not an object")
            tool_json = json.dumps(tool_call, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise LedgerError("tool_call must be a JSON object") from exc
        saved_tool = json.loads(tool_json)
        envelope = {
            "seller_domain": intent.seller_domain.strip().lower(),
            "seller_address": intent.seller_address.strip().lower(),
            "item": intent.item, "amount": intent.amount, "tool_call": saved_tool,
        }
        fingerprint = hashlib.sha256(canonical({
            "version": 2, "mandate_id": self.mandate_id, "agent_id": self.agent_id, **envelope,
        }).encode()).hexdigest()
        with self._tx():
            self._require_active_session(session_key_id)
            prior = self.conn.execute("SELECT * FROM purchases WHERE request_key=?", (request_key,)).fetchone()
            if prior:
                # Compare stored envelopes so pre-v2 purchases retain their original
                # fingerprint and dispatch claim, while changed arguments still fail.
                prior_envelope = {
                    "seller_domain": prior["seller_domain"].strip().lower(),
                    "seller_address": prior["seller_address"].strip().lower(),
                    "item": prior["item"], "amount": prior["amount"],
                    "tool_call": json.loads(prior["tool_call_json"]),
                }
                if prior["mandate_id"] != self.mandate_id or canonical(prior_envelope) != canonical(envelope):
                    raise LedgerError("request_key reused with a different purchase")
                return dict(prior), False
            n = self.conn.execute("SELECT COUNT(*) FROM purchases").fetchone()[0] + 1
            pid = f"pur_{n:04d}"
            # Every earlier purchase of this item from this seller that holds or spent money counts as an
            # attempt, so a runaway loop re-buying the same thing hits max_attempts (MND-22). Blocked,
            # rejected and definitively unpaid purchases moved no money and do not count.
            counted = (*HOLDING, *PAID)
            attempt = 1 + self.conn.execute(
                f"SELECT COUNT(*) FROM purchases WHERE mandate_id=? AND item=? AND seller_domain=? "
                f"AND stage IN ({','.join('?' * len(counted))})",
                (self.mandate_id, intent.item, envelope["seller_domain"], *[c.value for c in counted])).fetchone()[0]
            now = time.time()
            self.conn.execute(
                "INSERT INTO purchases(id,mandate_id,request_key,fingerprint,seller_domain,seller_address,item,amount,"
                "attempt,tool_call_json,stage,session_key_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, self.mandate_id, request_key, fingerprint, envelope["seller_domain"],
                 envelope["seller_address"], intent.item, intent.amount, attempt, tool_json,
                 Stage.PROPOSED.value, session_key_id, now, now))
            self._emit("INTENT_PROPOSED", "agent", purchase_id=pid, amount=intent.amount, ref=session_key_id,
                       detail=f"{saved_tool.get('tool', 'tool')} -> {envelope['seller_domain']}")
            return dict(self._purchase(pid)), True

    def _evaluate_purchase(self, purchase: sqlite3.Row, policy: Policy, available: int) -> Evaluation:
        tool_call = json.loads(purchase["tool_call_json"])
        arguments = tool_call.get("arguments")
        intent = Intent(purchase["seller_domain"], purchase["seller_address"], purchase["item"],
                        purchase["amount"], purchase["attempt"], arguments)
        return evaluate(intent, policy, available, now=time.time())

    def _recheck_reserved_policy(self, purchase: sqlite3.Row, *, require_approval: bool = False) -> None:
        # This purchase already holds its allocation; count it back exactly once.
        result = self._evaluate_purchase(purchase, self.policy(), self.balances()["available"] + purchase["amount"])
        if result.decision is Decision.BLOCKED:
            raise LedgerError(f"{result.clause.value}: {result.reason}")
        if require_approval and result.decision is Decision.HELD_FOR_REVIEW and not purchase["approved_by_human"]:
            raise LedgerError(f"{result.clause.value}: approval required under current policy")

    def evaluate_and_reserve(self, purchase_id: str, approval_window_s: int = 900) -> Evaluation:
        """Policy check and, if it passes, the hold. One transaction."""
        with self._tx():
            p = self._purchase(purchase_id)
            if p["stage"] != Stage.PROPOSED.value:
                raise LedgerError("already evaluated")
            self._require_active_session(p["session_key_id"])
            pol = self.policy()
            ev = self._evaluate_purchase(p, pol, self.balances()["available"])
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
                digest = self._approval_digest(self._purchase(purchase_id), ev.policy_version)
                self.conn.execute("UPDATE purchases SET approval_digest=? WHERE id=?", (digest, purchase_id))
                self._emit("APPROVAL_REQUESTED", "gateway", purchase_id=purchase_id, amount=p["amount"],
                           clause=ev.clause.value, detail="Waiting for a person")
            else:
                self._stage(purchase_id, Stage.RESERVED, **base)
            return ev

    def approve(self, purchase_id: str, digest: str | None = None) -> None:
        """Approve exactly the terms in approval_summary(). `digest` must match when given (the API requires it)."""
        with self._tx():
            p = self._purchase(purchase_id)
            if p["stage"] != Stage.PENDING_APPROVAL.value:
                raise LedgerError("not awaiting approval")
            if digest is not None and not hmac.compare_digest(digest, p["approval_digest"] or ""):
                raise LedgerError("approval does not match this purchase's terms")
            self._check_binding(p)
            if self.policy().paused:
                raise LedgerError("kill-switch engaged")
            self._require_active_session(p["session_key_id"])
            if (p["expires_at"] or 0) <= time.time():
                raise LedgerError("approval window closed")
            self._recheck_reserved_policy(p)
            self._stage(purchase_id, Stage.RESERVED, approved_by_human=1)
            self._emit("APPROVAL_GRANTED", "approver", purchase_id=purchase_id, amount=p["amount"],
                       ref=p["approval_digest"],
                       detail=f"Authorized for this exact seller, item, arguments and amount under policy v{p['policy_version']}")

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

    def claim_dispatch(self, purchase_id: str, rail: str | None = None) -> str:
        """Record intent to pay BEFORE calling the rail. Exactly one claim per purchase."""
        with self._tx():
            p = self._purchase(purchase_id)
            if p["stage"] != Stage.RESERVED.value:
                raise LedgerError("not reserved")
            if self.policy().paused:
                raise LedgerError("kill-switch engaged")
            self._require_active_session(p["session_key_id"])
            if p["approved_by_human"]:
                self._check_binding(p)
            self._recheck_reserved_policy(p, require_approval=True)
            if self.wallet_status()["dispatch_blocked"]:
                raise LedgerError("wallet mismatch or insufficient monitored wallet coverage")
            key = f"mnd-{p['fingerprint'][:8]}-{p['id']}"
            try:
                self.conn.execute(
                    "INSERT INTO dispatch_claims(idempotency_key,purchase_id,state,created_at) VALUES (?,?,?,?)",
                    (key, purchase_id, "CLAIMED", time.time()))
            except sqlite3.IntegrityError as exc:
                raise LedgerError("dispatch already claimed") from exc
            self._stage(purchase_id, Stage.DISPATCHED)
            if rail:
                self.conn.execute("UPDATE purchases SET rail=? WHERE id=?", (rail, purchase_id))
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
            other = self.conn.execute(
                "SELECT id FROM purchases WHERE id<>? AND (rail_order_id=? OR rail_tx_id=?) AND stage IN (?,?,?)",
                (purchase_id, rail_order_id, rail_order_id, *[s.value for s in PAID])).fetchone()
            if other:  # one rail payment can only ever pay for one purchase
                raise LedgerError(f"rail order {rail_order_id} already settled purchase {other['id']}")
            self.conn.execute("UPDATE mandate SET held=held-?, settled=settled+? WHERE id=?",
                              (p["amount"], p["amount"], self.mandate_id))
            self.conn.execute("UPDATE dispatch_claims SET state='SETTLED' WHERE purchase_id=?", (purchase_id,))
            self._stage(purchase_id, Stage.SETTLED, rail_order_id=rail_order_id, delivery="PENDING")
            self.conn.execute("UPDATE purchases SET rail_tx_id=? WHERE id=?", (rail_order_id, purchase_id))
            self._emit("PAYMENT_SETTLED", "executor", purchase_id=purchase_id, amount=p["amount"], ref=rail_order_id,
                       detail="Hold moved to settled once")
            return True

    def attach_order(self, purchase_id: str, rail_order_id: str, approval_id: str | None = None) -> None:
        """Remember the rail's order id (and any rail approval id) for a dispatched, unsettled purchase."""
        with self._tx():
            if self._purchase(purchase_id)["stage"] != Stage.DISPATCHED.value:
                raise LedgerError("not dispatched")
            self.conn.execute("UPDATE purchases SET rail_order_id=?, approval_id=COALESCE(?, approval_id), "
                              "updated_at=? WHERE id=?", (rail_order_id, approval_id, time.time(), purchase_id))

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

    def claim_key(self, purchase_id: str) -> str | None:
        """The idempotency key sent to the rail as dapp_order_id: the original order reference."""
        with self._lock:
            row = self.conn.execute("SELECT idempotency_key FROM dispatch_claims WHERE purchase_id=?",
                                    (purchase_id,)).fetchone()
        return row["idempotency_key"] if row else None

    def record_reconcile(self, purchase_id: str, detail: str, ref: str | None = None) -> None:
        """Evidence for every recovery lookup, including the ones that change nothing."""
        with self._tx():
            p = self._purchase(purchase_id)
            self._emit("RECONCILE_ATTEMPTED", "owner", purchase_id=purchase_id, amount=p["amount"], ref=ref,
                       detail=detail)

    def resolve_manually(self, purchase_id: str, *, outcome: str, reference: str, note: str = "") -> None:
        """Operator path when the rail cannot be asked (MND-11).

        The owner inspected the provider by hand and records what they found, with the provider's own
        reference. PAID settles once against that reference. UNPAID releases the hold only as an explicit,
        recorded attestation that no payment happened. Amount or balance alone is never accepted as proof.
        """
        reference = reference.strip()
        if not reference:
            raise LedgerError("a provider reference is required")
        with self._tx():
            p = self._purchase(purchase_id)
            if p["stage"] not in (Stage.RECOVERY_UNKNOWN.value, Stage.DISPATCHED.value):
                raise LedgerError("only an unresolved payment can be resolved by hand")
            self._emit("OPERATOR_RESOLUTION", "owner", purchase_id=purchase_id, amount=p["amount"], ref=reference,
                       detail=f"Owner inspected the provider: {outcome}. {note}".strip())
        if outcome == "PAID":
            self.settle(purchase_id, reference)
        elif outcome == "UNPAID":
            self.close_unpaid(purchase_id, f"owner attested no payment ({reference})")
        else:
            raise LedgerError("outcome must be PAID or UNPAID")

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
