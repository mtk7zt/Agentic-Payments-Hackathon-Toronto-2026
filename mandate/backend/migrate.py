"""Create or upgrade the Mandate schema in the configured database, then exit.

    python -m backend.migrate                 # uses DATABASE_URL (or MANDATE_DB)

Safe to run repeatedly: each migration is recorded once in schema_migrations.
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.parse

from .core.db import connect
from .core.migrations import MIGRATIONS


def _redact(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    if p.password:
        netloc = p.netloc.replace(f":{p.password}@", ":***@")
        return urllib.parse.urlunsplit(p._replace(netloc=netloc))
    return url


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Create or upgrade the Mandate database schema.")
    ap.parse_args(argv)
    url = (os.environ.get("DATABASE_URL") or os.environ.get("MANDATE_DB") or "").strip()
    if not url:
        print("Set DATABASE_URL (for example your Supabase session pooler URI).", file=sys.stderr)
        return 2
    db = connect(url)  # connecting applies any missing migrations
    applied = [r[0] for r in db.execute("SELECT version FROM schema_migrations ORDER BY version")]
    latest = MIGRATIONS[-1].version
    print(f"{_redact(url)}: schema at version {max(applied)} of {latest} ({db.dialect})")
    db.close()
    return 0 if max(applied) == latest else 1


if __name__ == "__main__":
    sys.exit(main())
