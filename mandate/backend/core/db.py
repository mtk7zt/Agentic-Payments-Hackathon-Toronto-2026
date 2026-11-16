"""Database access for the ledger: one small interface, two drivers.

The ledger is written against `Database`, never against a driver. SQLite serves local
development and single-node installs; PostgreSQL (Supabase, RDS, Cloud SQL, self-hosted)
serves production. Moving between hosts changes `DATABASE_URL`, not code.

SQL in the ledger is written once, with `?` positional or `:name` named parameters and the
small portable subset both engines share. The PostgreSQL driver translates the parameter
style; anything that genuinely differs (schema, triggers, column introspection, write
locking) lives here or in the per-dialect migrations.
"""
from __future__ import annotations

import re
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, Protocol

from .migrations import MIGRATIONS


class Row(Sequence[Any]):
    """A result row readable by position (`row[0]`) and by column (`row["id"]`), like sqlite3.Row."""

    __slots__ = ("_cols", "_vals")

    def __init__(self, cols: Sequence[str], vals: Sequence[Any]):
        self._cols = tuple(cols)
        self._vals = tuple(vals)

    def __getitem__(self, key: int | str | slice) -> Any:  # type: ignore[override]
        if isinstance(key, str):
            try:
                return self._vals[self._cols.index(key)]
            except ValueError as exc:
                raise IndexError(f"no column {key!r}") from exc
        return self._vals[key]

    def __len__(self) -> int:
        return len(self._vals)

    def keys(self) -> list[str]:
        return list(self._cols)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._vals)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Row) and self._cols == other._cols and self._vals == other._vals

    def __repr__(self) -> str:
        return f"Row({dict(zip(self._cols, self._vals, strict=True))!r})"


class Cursor:
    def __init__(self, rows: list[Row], rowcount: int):
        self._rows, self.rowcount = rows, rowcount

    def fetchone(self) -> Row | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Row]:
        return list(self._rows)

    def __iter__(self) -> Iterator[Row]:
        return iter(self._rows)


class Database(Protocol):
    dialect: str
    IntegrityError: tuple[type[BaseException], ...]

    def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> Cursor: ...
    def write_transaction(self) -> Any: ...
    def columns(self, table: str) -> set[str]: ...
    def close(self) -> None: ...


# ───────── SQLite ─────────

class SqliteDatabase:
    dialect = "sqlite"
    IntegrityError: tuple[type[BaseException], ...] = (sqlite3.IntegrityError,)

    def __init__(self, path: str):
        self.path = path
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")  # readers do not block the writer
            self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.RLock()
        _migrate(self)

    def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = [Row(cols, r) for r in cur.fetchall()] if cols else []
            return Cursor(rows, cur.rowcount)

    def script(self, sql: str) -> None:
        with self._lock:
            self._conn.executescript(sql)

    @contextmanager
    def write_transaction(self) -> Iterator[None]:
        # IMMEDIATE takes the write lock up front, so two writers can never interleave.
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def columns(self, table: str) -> set[str]:
        return {r[1] for r in self._conn.execute(f"PRAGMA table_info({_ident(table)})")}

    def close(self) -> None:
        self._conn.close()


# ───────── PostgreSQL ─────────

_NAMED = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")
_INSERT_OR_IGNORE = re.compile(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO", re.IGNORECASE)
# One advisory lock key for all ledger writes in a database: the hash-linked event chain is
# a single sequence, so its appends must be serialised across every gateway process.
_WRITE_LOCK_KEY = 0x4D414E44  # "MAND"


def _to_pg(sql: str, params: Sequence[Any] | Mapping[str, Any]) -> str:
    if _INSERT_OR_IGNORE.match(sql):
        sql = _INSERT_OR_IGNORE.sub("INSERT INTO", sql) + " ON CONFLICT DO NOTHING"
    if not params:  # no parameters: psycopg sends the text as-is, so leave % and ? alone
        return sql
    sql = sql.replace("%", "%%")
    if isinstance(params, Mapping):
        return _NAMED.sub(lambda m: f"%({m.group(1)})s", sql)
    return sql.replace("?", "%s")


class PostgresDatabase:
    dialect = "postgresql"

    def __init__(self, url: str, *, schema: str | None = None):
        import psycopg  # imported here so SQLite-only installs do not need the driver

        self._psycopg = psycopg
        self.IntegrityError = (psycopg.errors.IntegrityError,)
        self._conn = psycopg.connect(url, autocommit=True)
        self._lock = threading.RLock()
        self._in_tx = False
        if schema:
            self._conn.execute(f"CREATE SCHEMA IF NOT EXISTS {_ident(schema)}")
            self._conn.execute(f"SET search_path TO {_ident(schema)}")
        _migrate(self)

    def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> Cursor:
        with self._lock:
            cur = self._conn.execute(_to_pg(sql, params), params or None)  # type: ignore[arg-type]
            cols = [d.name for d in cur.description] if cur.description else []
            rows = [Row(cols, r) for r in cur.fetchall()] if cols else []
            return Cursor(rows, cur.rowcount)

    def script(self, sql: str) -> None:
        with self._lock:
            self._conn.execute(sql)  # type: ignore[arg-type]

    @contextmanager
    def write_transaction(self) -> Iterator[None]:
        with self._lock:
            if self._in_tx:  # re-entrant use inside an open transaction joins it
                yield
                return
            self._conn.execute("BEGIN")
            self._in_tx = True
            try:
                self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (_WRITE_LOCK_KEY,))
                yield
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            finally:
                self._in_tx = False

    def columns(self, table: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s", (table,)).fetchall()
        return {r[0] for r in rows}

    def close(self) -> None:
        self._conn.close()


# ───────── shared ─────────

def _ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", name):
        raise ValueError(f"unsafe identifier {name!r}")
    return name


def _migrate(db: SqliteDatabase | PostgresDatabase) -> None:
    """Apply every migration not yet recorded, in order, each in its own transaction."""
    db.script("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    done = {r[0] for r in db.execute("SELECT version FROM schema_migrations")}
    for m in MIGRATIONS:
        if m.version in done:
            continue
        # SQLite's executescript commits on its own, so its migrations are not wrapped; every SQLite
        # migration is written to be safely re-runnable instead. PostgreSQL DDL is transactional.
        if db.dialect == "postgresql":
            with db.write_transaction():
                m.apply(db)
                db.execute("INSERT INTO schema_migrations(version, name) VALUES (?, ?)", (m.version, m.name))
        else:
            m.apply(db)
            db.execute("INSERT INTO schema_migrations(version, name) VALUES (?, ?)", (m.version, m.name))


def connect(url: str) -> SqliteDatabase | PostgresDatabase:
    """`postgresql://…` (or `postgres://…`) -> PostgreSQL; `sqlite:///path`, a path or `:memory:` -> SQLite."""
    if url.startswith(("postgresql://", "postgres://")):
        return PostgresDatabase(url)
    if url.startswith("sqlite:///"):
        url = url[len("sqlite:///"):] or ":memory:"
    return SqliteDatabase(url)


def rows_as_dicts(rows: Iterable[Row]) -> list[dict[str, Any]]:
    return [dict(zip(r.keys(), r, strict=True)) for r in rows]
