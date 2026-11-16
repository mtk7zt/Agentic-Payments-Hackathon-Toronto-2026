"""Dialect-aware helpers for tests that inject storage failures or play an attacker with database access."""
from __future__ import annotations

_PROTECTED = {"events": ("events_no_update", "events_no_delete", "events_append_only", "events_no_truncate"),
              "evidence_checkpoints": ("checkpoints_no_update", "checkpoints_no_delete",
                                       "checkpoints_append_only", "checkpoints_no_truncate")}


def inject_failure(conn, name: str, table: str, message: str, *, event_type: str | None = None) -> None:
    """Make the next matching INSERT into `table` fail, as a crashed disk or lost audit store would."""
    if conn.dialect == "postgresql":
        cond = f"NEW.type = '{event_type}'" if event_type else "TRUE"
        conn.execute(f"""CREATE OR REPLACE FUNCTION {name}_fn() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF {cond} THEN RAISE EXCEPTION '{message}' USING ERRCODE = 'integrity_constraint_violation';
            END IF; RETURN NEW; END; $$""")
        conn.execute(f"CREATE TRIGGER {name} BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION {name}_fn()")
    else:
        when = f"WHEN NEW.type='{event_type}' " if event_type else ""
        conn.execute(f"CREATE TRIGGER {name} BEFORE INSERT ON {table} {when}"
                     f"BEGIN SELECT RAISE(ABORT, '{message}'); END")


def remove_failure(conn, name: str, table: str) -> None:
    if conn.dialect == "postgresql":
        conn.execute(f"DROP TRIGGER {name} ON {table}")
    else:
        conn.execute(f"DROP TRIGGER {name}")


def drop_append_only(conn, table: str = "events") -> None:
    """Remove the append-only guard, as someone with schema access could."""
    if conn.dialect == "postgresql":
        conn.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
    else:
        for trig in _PROTECTED[table]:
            conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
