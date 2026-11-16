"""Regression tests always start with mock integrations, even in a live-configured shell."""

import pytest

_ENV = pytest.StashKey[pytest.MonkeyPatch]()


def pytest_configure(config):
    config.addinivalue_line("markers", "sqlite_only: test inspects the SQLite file format directly")
    patch = pytest.MonkeyPatch()
    patch.setenv("MANDATE_MODE", "sandbox")  # tests use the sandbox rail and an in-memory database
    patch.setenv("MANDATE_DB", ":memory:")
    patch.delenv("DATABASE_URL", raising=False)
    patch.setenv("MANDATE_RAIL", "mock")
    patch.setenv("MANDATE_SELLER_MODE", "mock")
    config.stash[_ENV] = patch


def pytest_unconfigure(config):
    patch = config.stash.get(_ENV, None)
    if patch is not None:
        patch.undo()


# ───────── run the whole suite on PostgreSQL ─────────
# Set MANDATE_TEST_DATABASE_URL=postgresql://… to run every test against PostgreSQL. See tests/pgmap.py.
from . import pgmap


@pytest.fixture(autouse=True)
def _use_postgres(request, monkeypatch):
    if not pgmap.url():
        yield
        return
    if request.node.get_closest_marker("sqlite_only"):
        pytest.skip("checks the SQLite file format itself")
    import backend.core.db as db_mod
    import backend.core.ledger as ledger_mod

    pgmap.run_id()  # exported in the environment, so child processes map paths to the same schemas
    monkeypatch.setattr(ledger_mod, "connect", pgmap.connect)
    monkeypatch.setattr(db_mod, "connect", pgmap.connect)
    yield


def pytest_sessionfinish(session, exitstatus):
    if not pgmap.url():
        return
    import psycopg

    with psycopg.connect(pgmap.url(), autocommit=True) as conn:
        prefix = f"t_{pgmap.run_id()}_"
        for (schema,) in conn.execute("SELECT schema_name FROM information_schema.schemata").fetchall():
            if schema.startswith(prefix):
                conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")

