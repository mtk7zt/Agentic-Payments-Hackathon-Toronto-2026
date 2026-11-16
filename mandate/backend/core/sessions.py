"""Agent session keys: issue, freeze, revoke.

Only a SHA-256 of each token is stored. A frozen key can be thawed by the
owner; a revoked key is dead and a new one must be issued.
"""
from __future__ import annotations

import hashlib
import secrets
from enum import Enum
from typing import Any

from .ids import ulid


class SessionState(str, Enum):
    ACTIVE = "active"
    FROZEN = "frozen"
    REVOKED = "revoked"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class SessionKeys:
    """Operates on the ledger's connection so key changes share its transactions."""

    def __init__(self, conn: Any):
        self.conn = conn

    def issue(self, agent_id: str) -> tuple[str, str]:
        """Returns (key_id, token). The token is shown once and never stored."""
        key_id = f"sk_{agent_id}_{ulid()}"
        token = secrets.token_urlsafe(32)
        self.conn.execute(
            "INSERT INTO session_keys(key_id, agent_id, token_sha256, state) VALUES (?,?,?,?)",
            (key_id, agent_id, _digest(token), SessionState.ACTIVE.value),
        )
        return key_id, token

    def resolve(self, token: str) -> tuple[str, SessionState] | None:
        row = self.conn.execute(
            "SELECT key_id, state FROM session_keys WHERE token_sha256=?", (_digest(token),)
        ).fetchone()
        return (row[0], SessionState(row[1])) if row else None

    def set_state(self, key_id: str, state: SessionState) -> None:
        cur = self.conn.execute(
            "UPDATE session_keys SET state=? WHERE key_id=? AND state!=?",
            (state.value, key_id, SessionState.REVOKED.value),
        )
        if cur.rowcount == 0:
            raise KeyError(f"{key_id} is unknown or already revoked")

    def active_key_ids(self, agent_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT key_id FROM session_keys WHERE agent_id=? AND state=?", (agent_id, SessionState.ACTIVE.value)
        ).fetchall()
        return [r[0] for r in rows]
