"""Qatom (TODAQ) rail over its MCP endpoint (MND-04).

Contract observed in MND-01 (see docs/rails/QATOM.md):
  1. tools/call agent_checkout {catalog_item_id, arguments}
     -> "Not done yet ... Approval id: aar_..."  (owner must approve), or
     -> "Bought ... Transaction: <uuid>"         (settled)
  2. after the owner approves: the same call again with approval_id
     -> "Bought ... Transaction: <uuid>"

Mandate decides first. This adapter is only reached for purchases Mandate's
rules already allowed, so blocked requests make zero Qatom calls.

Qatom has no client-supplied idempotency key, so the orchestrator's rule holds:
an UNKNOWN answer is never retried here; it goes to reconciliation.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .payment_rail import RailOutcome, RailResult, provider_message

_BOUGHT = re.compile(r"Bought .*? for ([0-9]+(?:\.[0-9]+)?) USD-TDN.*?Transaction:\s*([0-9a-fA-F-]{36})", re.DOTALL)
_APPROVAL = re.compile(r"Approval id:\s*(aar_[0-9a-fA-F-]+)")
_APPROVAL_URL = re.compile(r"(https://\S+/approvals/\S+/review)")
_INSUFFICIENT = re.compile(r"cannot cover|insufficient", re.IGNORECASE)
APPROVAL_PREFIX = "qatom-approval:"


@dataclass
class QatomConfig:
    mcp_url: str = "https://mcp.m.todaq.net/mcp"
    token_file: str = str(Path.home() / ".toda-mcp-token.json")
    item_map: dict[str, int] = field(default_factory=dict)  # Mandate catalog item -> Qatom catalog id
    timeout_s: float = 20.0

    @classmethod
    def from_env(cls) -> QatomConfig:
        e = os.environ.get
        raw_map = e("QATOM_ITEM_MAP", "{}")
        try:
            item_map = {str(k): int(v) for k, v in json.loads(raw_map).items()}
        except (ValueError, AttributeError) as exc:
            raise RuntimeError("QATOM_ITEM_MAP must be a JSON object of item -> catalog id") from exc
        return cls(mcp_url=e("QATOM_MCP_URL", cls.mcp_url),
                   token_file=os.path.expanduser(e("QATOM_TOKEN_FILE", cls.token_file)), item_map=item_map)

    def token(self) -> str:
        try:
            data = json.loads(Path(self.token_file).read_text())
        except FileNotFoundError as exc:
            raise RuntimeError(f"Qatom token file not found at {self.token_file}; run the Qatom sign-in once") from exc
        tok = data.get("access_token") if isinstance(data, dict) else None
        if not tok:
            raise RuntimeError("Qatom token file has no access_token")
        return tok


class McpClient:
    """Minimal MCP streamable-HTTP client: initialize once, then tools/call."""

    def __init__(self, url: str, token: str, client: httpx.Client | None = None, timeout_s: float = 20.0):
        self.url, self.token = url, token
        self.http = client or httpx.Client(timeout=timeout_s)
        self.session: str | None = None
        self._id = 0

    def _post(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        self._id += 1
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        r = self.http.post(self.url, json=body, headers=headers)
        r.raise_for_status()
        self.session = r.headers.get("mcp-session-id", self.session)
        return _parse_rpc(r)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        if self.session is None:
            self._post("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "mandate", "version": "1"}})
        res = self._post("tools/call", {"name": name, "arguments": arguments}).get("result", {})
        text = "\n".join(c.get("text", "") for c in res.get("content", []) if isinstance(c, dict))
        return text, bool(res.get("isError"))


def _parse_rpc(r: httpx.Response) -> dict[str, Any]:
    if "text/event-stream" in r.headers.get("content-type", ""):
        for line in r.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return {}
    return r.json()


class QatomRail:
    name = "qatom"

    def __init__(self, config: QatomConfig, mcp: McpClient | None = None, store: Any | None = None):
        self.cfg = config
        self.mcp = mcp or McpClient(config.mcp_url, config.token(), timeout_s=config.timeout_s)
        # approval id -> the exact purchase it approves (same item, same arguments, same cap).
        # With a store (the ledger) this survives a restart; without one it lives in memory.
        self.store = store
        self.pending: dict[str, dict[str, Any]] = {}

    def _remember(self, approval_id: str, dispatch_key: str, ctx: dict[str, Any]) -> None:
        if self.store is not None:
            self.store.save_rail_approval(approval_id, rail=self.name, dispatch_key=dispatch_key, context=ctx)
        else:
            self.pending[approval_id] = ctx

    def _recall(self, approval_id: str) -> dict[str, Any] | None:
        if self.store is not None:
            row = self.store.rail_approval(approval_id)
            return None if row is None else row["context"]
        return self.pending.get(approval_id)

    def _forget(self, approval_id: str) -> None:
        if self.store is not None:
            self.store.resolve_rail_approval(approval_id)
        self.pending.pop(approval_id, None)

    def _checkout(self, item_id: int, arguments: dict[str, Any], amount_milli: int,
                  approval_id: str | None = None, dispatch_key: str = "") -> RailResult:
        args: dict[str, Any] = {"catalog_item_id": item_id}
        if arguments:
            args["arguments"] = arguments
        if approval_id:
            args["approval_id"] = approval_id
        try:
            text, is_error = self.mcp.call_tool("agent_checkout", args)
        except (httpx.HTTPError, ValueError) as exc:
            return RailResult(RailOutcome.UNKNOWN, raw={"error": type(exc).__name__})
        m = _BOUGHT.search(text)
        if m:
            paid_milli = round(float(m.group(1)) * 1000)
            if paid_milli > amount_milli:  # the rail charged more than Mandate reserved
                return RailResult(RailOutcome.UNKNOWN, m.group(2), "PRICE_ABOVE_RESERVED", {"text": text[:500]})
            return RailResult(RailOutcome.SETTLED, m.group(2), "BOUGHT", {"paid_milli": paid_milli, "text": text[:500]})
        a = _APPROVAL.search(text)
        if a:
            if not approval_id:  # first time we see this approval: keep the exact checkout to resume
                self._remember(a.group(1), dispatch_key,
                               {"item_id": item_id, "arguments": arguments, "amount_milli": amount_milli})
            u = _APPROVAL_URL.search(text)
            return RailResult(RailOutcome.PENDING, APPROVAL_PREFIX + a.group(1), "APPROVAL_REQUIRED",
                              {"approval_id": a.group(1), "approval_url": u.group(1) if u else None})
        if _INSUFFICIENT.search(text):
            return RailResult(RailOutcome.UNPAID, status=_with_reason("INSUFFICIENT_FUNDS", text), raw={"text": text[:500]})
        if is_error:
            return RailResult(RailOutcome.UNPAID, status=_with_reason("REFUSED", text), raw={"text": text[:500]})
        return RailResult(RailOutcome.UNKNOWN, raw={"text": text[:500]})

    def create_order(self, *, dapp_order_id: str, amount_milli: int, item: str | None = None,
                     arguments: dict[str, Any] | None = None, **_: Any) -> RailResult:
        item_id = self.cfg.item_map.get(item or "")
        if item_id is None:
            return RailResult(RailOutcome.UNPAID, status="ITEM_NOT_ON_QATOM", raw={"item": item})
        return self._checkout(item_id, arguments or {}, amount_milli, dispatch_key=dapp_order_id)

    def get_order(self, order_id: str) -> RailResult:
        """For an approval: retry the exact same checkout with the approval id. Otherwise unknown."""
        if order_id.startswith(APPROVAL_PREFIX):
            aid = order_id[len(APPROVAL_PREFIX):]
            p = self._recall(aid)
            if not p:
                return RailResult(RailOutcome.UNKNOWN, order_id, "APPROVAL_CONTEXT_LOST")
            res = self._checkout(p["item_id"], p["arguments"], p["amount_milli"], approval_id=aid)
            if res.outcome in (RailOutcome.SETTLED, RailOutcome.UNPAID):
                self._forget(aid)
            elif res.outcome is RailOutcome.PENDING:  # still waiting for the owner
                return RailResult(RailOutcome.PENDING, order_id, "APPROVAL_REQUIRED", res.raw)
            return res
        return RailResult(RailOutcome.UNKNOWN, order_id, "NO_LOOKUP_ON_QATOM")

    def get_proof(self, order_id: str) -> dict[str, Any]:
        return {"rail": "qatom", "transaction_id": order_id}


def _with_reason(status: str, text: str) -> str:
    """`REFUSED: <Qatom's own words>` so the evidence log says why, never anything credential-like."""
    msg = provider_message(text)
    return f"{status}: {msg}" if msg else status
