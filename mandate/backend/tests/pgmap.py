"""Run tests on PostgreSQL: map each SQLite path a test uses to its own PostgreSQL schema.

Active only when MANDATE_TEST_DATABASE_URL is set. `:memory:` gets a fresh schema every time;
a file path always maps to the same schema within one test run (MANDATE_TEST_RUN), so tests
that reopen a database, including from a crashed child process, see the same data.
"""
from __future__ import annotations

import hashlib
import os
import uuid

SCHEMAS: set[str] = set()


def url() -> str:
    return os.environ.get("MANDATE_TEST_DATABASE_URL", "")


def run_id() -> str:
    return os.environ.setdefault("MANDATE_TEST_RUN", uuid.uuid4().hex[:8])


def connect(target: str):
    from backend.core import db

    if target.startswith(("postgresql://", "postgres://")):
        return db.PostgresDatabase(target)
    path = target.removeprefix("sqlite:///")
    tag = uuid.uuid4().hex[:12] if path in ("", ":memory:") else hashlib.sha256(path.encode()).hexdigest()[:12]
    schema = f"t_{run_id()}_{tag}"
    SCHEMAS.add(schema)
    return db.PostgresDatabase(url(), schema=schema)


def install() -> None:
    """Patch the ledger to use PostgreSQL. Safe to call in a child process; a no-op without the env var."""
    if not url():
        return
    import backend.core.db as db_mod
    import backend.core.ledger as ledger_mod

    ledger_mod.connect = connect
    db_mod.connect = connect
