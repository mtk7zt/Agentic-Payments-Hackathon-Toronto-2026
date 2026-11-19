"""Console accounts. The owner token is the only caller. The browser never writes this table."""
from __future__ import annotations

import time
from typing import Any

from ..core.ledger import Ledger

STATUSES = ("pending", "approved", "rejected")


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if not email or len(email) > 254 or any(ch.isspace() for ch in email) or email.count("@") != 1:
        raise ValueError("email is not usable")
    local, domain = email.split("@")
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise ValueError("email is not usable")
    return email


def _as_dict(row: Any) -> dict[str, Any]:
    return {
        "email": row["email"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _get(ledger: Ledger, email: str) -> dict[str, Any] | None:
    row = ledger.conn.execute(
        "SELECT email, status, created_at, updated_at FROM console_users WHERE email=?",
        (email,),
    ).fetchone()
    return None if row is None else _as_dict(row)


def register_or_get(ledger: Ledger, email: str) -> dict[str, Any]:
    """Return the row, creating it as pending the first time this address appears."""
    with ledger.snapshot(), ledger.conn.write_transaction():
        existing = _get(ledger, email)
        if existing:
            return existing
        now = time.time()
        try:
            ledger.conn.execute(
                "INSERT INTO console_users(email, status, created_at, updated_at) VALUES (?,?,?,?)",
                (email, "pending", now, now),
            )
        except ledger.conn.IntegrityError:
            pass
        row = _get(ledger, email)
    if row is None:
        raise RuntimeError("console user was not stored")
    return row


def list_users(ledger: Ledger) -> list[dict[str, Any]]:
    with ledger.snapshot():
        rows = ledger.conn.execute(
            "SELECT email, status, created_at, updated_at FROM console_users ORDER BY email",
        ).fetchall()
    return [_as_dict(row) for row in rows]


def set_status(ledger: Ledger, email: str, status: str) -> dict[str, Any] | None:
    if status not in STATUSES:
        raise ValueError("status is not usable")
    with ledger.snapshot(), ledger.conn.write_transaction():
        if _get(ledger, email) is None:
            return None
        ledger.conn.execute(
            "UPDATE console_users SET status=?, updated_at=? WHERE email=?",
            (status, time.time(), email),
        )
        return _get(ledger, email)
