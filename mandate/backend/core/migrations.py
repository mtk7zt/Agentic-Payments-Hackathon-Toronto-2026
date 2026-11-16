"""Versioned schema migrations for the ledger, for SQLite and PostgreSQL.

Each migration runs once per database, in order, inside a write transaction, and is
recorded in `schema_migrations`. Migrations only ever add: tables, columns, indexes,
triggers. Money and evidence tables are append-only by trigger on both engines.

Databases created before this module existed already hold the version-1 tables; version 1
uses IF NOT EXISTS and version 2 adds any missing columns, so they upgrade in place.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[Any], None]


def _types(db: Any) -> dict[str, str]:
    pg = db.dialect == "postgresql"
    return {
        "INT": "BIGINT" if pg else "INTEGER",      # money is integer thousandths, up to 2**53 - 1
        "REAL": "DOUBLE PRECISION" if pg else "REAL",
    }


_TABLES = """
CREATE TABLE IF NOT EXISTS mandate (
  id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, policy_json TEXT NOT NULL,
  held {INT} NOT NULL DEFAULT 0 CHECK (held >= 0),
  settled {INT} NOT NULL DEFAULT 0 CHECK (settled >= 0)
);
CREATE TABLE IF NOT EXISTS wallet_monitor (
  mandate_id TEXT PRIMARY KEY, baseline {INT} NOT NULL, baseline_settled {INT} NOT NULL,
  observed {INT} NOT NULL, observed_settled {INT} NOT NULL,
  mismatch INTEGER NOT NULL CHECK (mismatch IN (0,1)), reference TEXT NOT NULL,
  recorded_at {REAL} NOT NULL
);
CREATE TABLE IF NOT EXISTS purchases (
  id TEXT PRIMARY KEY, mandate_id TEXT NOT NULL, request_key TEXT NOT NULL UNIQUE,
  fingerprint TEXT NOT NULL, seller_domain TEXT NOT NULL, seller_address TEXT NOT NULL,
  item TEXT NOT NULL, amount {INT} NOT NULL CHECK (amount > 0), attempt INTEGER NOT NULL,
  tool_call_json TEXT NOT NULL, stage TEXT NOT NULL, decision TEXT, clause TEXT,
  policy_version INTEGER, policy_hash TEXT, session_key_id TEXT, expires_at {REAL},
  approved_by_human INTEGER NOT NULL DEFAULT 0, rail_order_id TEXT, delivery TEXT NOT NULL DEFAULT 'NONE',
  created_at {REAL} NOT NULL, updated_at {REAL} NOT NULL
);
CREATE TABLE IF NOT EXISTS dispatch_claims (
  idempotency_key TEXT PRIMARY KEY, purchase_id TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL CHECK (state IN ('CLAIMED','SETTLED','UNKNOWN','UNPAID')),
  created_at {REAL} NOT NULL
);
CREATE TABLE IF NOT EXISTS rail_approvals (
  approval_id TEXT PRIMARY KEY, rail TEXT NOT NULL, dispatch_key TEXT NOT NULL,
  context_json TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN ('PENDING','RESOLVED')),
  created_at {REAL} NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  seq {INT} PRIMARY KEY, ts {INT} NOT NULL, type TEXT NOT NULL, actor TEXT NOT NULL,
  purchase_id TEXT, amount {INT}, clause TEXT, ref TEXT, detail TEXT NOT NULL,
  policy_hash TEXT NOT NULL, prev TEXT NOT NULL, hash TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS session_keys (
  key_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, token_sha256 TEXT NOT NULL UNIQUE, state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_checkpoints (
  id TEXT PRIMARY KEY, mandate_id TEXT NOT NULL, seq {INT} NOT NULL, checkpoint_json TEXT NOT NULL
);
"""

_SQLITE_APPEND_ONLY = """
CREATE TRIGGER IF NOT EXISTS checkpoints_no_update BEFORE UPDATE ON evidence_checkpoints
BEGIN SELECT RAISE(ABORT, 'checkpoints are append-only'); END;
CREATE TRIGGER IF NOT EXISTS checkpoints_no_delete BEFORE DELETE ON evidence_checkpoints
BEGIN SELECT RAISE(ABORT, 'checkpoints are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""

_PG_APPEND_ONLY = """
CREATE OR REPLACE FUNCTION mandate_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% are append-only', TG_TABLE_NAME USING ERRCODE = 'integrity_constraint_violation';
END;
$$;
DROP TRIGGER IF EXISTS events_append_only ON events;
CREATE TRIGGER events_append_only BEFORE UPDATE OR DELETE ON events
  FOR EACH ROW EXECUTE FUNCTION mandate_append_only();
DROP TRIGGER IF EXISTS events_no_truncate ON events;
CREATE TRIGGER events_no_truncate BEFORE TRUNCATE ON events
  FOR EACH STATEMENT EXECUTE FUNCTION mandate_append_only();
DROP TRIGGER IF EXISTS checkpoints_append_only ON evidence_checkpoints;
CREATE TRIGGER checkpoints_append_only BEFORE UPDATE OR DELETE ON evidence_checkpoints
  FOR EACH ROW EXECUTE FUNCTION mandate_append_only();
DROP TRIGGER IF EXISTS checkpoints_no_truncate ON evidence_checkpoints;
CREATE TRIGGER checkpoints_no_truncate BEFORE TRUNCATE ON evidence_checkpoints
  FOR EACH STATEMENT EXECUTE FUNCTION mandate_append_only();
"""


def _v1_base(db: Any) -> None:
    db.script(_TABLES.format(**_types(db)))
    db.script(_PG_APPEND_ONLY if db.dialect == "postgresql" else _SQLITE_APPEND_ONLY)


# Columns added to purchases after the first release (MND-32 rail records, MND-10 approval binding).
_PURCHASE_COLUMNS = (("rail", "TEXT"), ("approval_id", "TEXT"), ("rail_tx_id", "TEXT"), ("approval_digest", "TEXT"))


def _v2_purchase_columns(db: Any) -> None:
    have = db.columns("purchases")
    for name, kind in _PURCHASE_COLUMNS:
        if name not in have:
            db.execute(f"ALTER TABLE purchases ADD COLUMN {name} {kind}")


def _v3_indexes(db: Any) -> None:
    # Hot paths: listing a mandate's purchases, recovery scans, repeat-window counts, one payment one purchase.
    for sql in (
        "CREATE INDEX IF NOT EXISTS purchases_mandate_created ON purchases (mandate_id, created_at)",
        "CREATE INDEX IF NOT EXISTS purchases_stage ON purchases (stage)",
        "CREATE INDEX IF NOT EXISTS purchases_repeat ON purchases (mandate_id, item, seller_domain, created_at)",
        "CREATE INDEX IF NOT EXISTS purchases_rail_order ON purchases (rail_order_id)",
        "CREATE INDEX IF NOT EXISTS purchases_rail_tx ON purchases (rail_tx_id)",
        "CREATE INDEX IF NOT EXISTS events_purchase ON events (purchase_id, seq)",
        "CREATE INDEX IF NOT EXISTS session_keys_agent ON session_keys (agent_id, state)",
        "CREATE INDEX IF NOT EXISTS checkpoints_mandate_seq ON evidence_checkpoints (mandate_id, seq)",
    ):
        db.execute(sql)


def _v4_checkpoint_order(db: Any) -> None:
    # Replaces SQLite's implicit rowid as the tie-break between checkpoints at the same sequence.
    if "issued_at_ms" not in db.columns("evidence_checkpoints"):
        db.execute(f"ALTER TABLE evidence_checkpoints ADD COLUMN issued_at_ms {_types(db)['INT']}")


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "base tables and append-only evidence", _v1_base),
    Migration(2, "rail records and approval binding columns", _v2_purchase_columns),
    Migration(3, "query indexes", _v3_indexes),
    Migration(4, "checkpoint issue time for ordering", _v4_checkpoint_order),
)
