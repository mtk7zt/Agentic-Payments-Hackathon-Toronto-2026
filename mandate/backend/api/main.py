"""Mandate gateway API.

    uvicorn backend.api.main:app --reload

Agent routes take X-Session-Key. Owner routes take X-Owner-Token when
MANDATE_OWNER_TOKEN is set; with it unset the API runs open, for local demos only.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ..core.ledger import Ledger, LedgerError
from ..core.policy import Intent
from ..core.sessions import SessionState
from ..models.money import MoneyError, format_milli, parse_milli
from ..services import orchestrator
from ..services.payment_rail import rail_from_env
from ..services.seller import MockSeller

DEMO_SELLERS = {
    "seller-a.example": "0x00000000000000000000000000000000000000a1",
    "seller-b.example": "0x00000000000000000000000000000000000000b2",
}

app = FastAPI(title="Mandate gateway", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("MANDATE_CORS_ORIGINS", "http://localhost:3000").split(","),
    allow_methods=["*"], allow_headers=["*"],
)

ledger = Ledger(os.environ.get("MANDATE_DB", ":memory:"))
if not ledger.policy().whitelisted_sellers:
    ledger.set_policy(whitelisted_sellers=DEMO_SELLERS)
rail = rail_from_env()
seller = MockSeller()


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PurchaseIn(Strict):
    request_key: str = Field(min_length=8, max_length=128)
    seller_domain: str = Field(min_length=1, max_length=253)
    seller_address: str = Field(min_length=1, max_length=128)
    item: str = Field(min_length=1, max_length=128)
    amount: str = Field(description="Decimal string in USD-TDN, at most 3 decimals, e.g. '0.040'")
    tool_call: dict[str, Any] = Field(default_factory=dict)


class PolicyPatch(Strict):
    max_single_spend: str | None = None
    strict_whitelist: bool | None = None
    max_attempts: int | None = Field(default=None, ge=1, le=5)
    whitelisted_sellers: dict[str, str] | None = None


class KillSwitch(Strict):
    engaged: bool


def owner(x_owner_token: str | None = Header(default=None)) -> None:
    expected = os.environ.get("MANDATE_OWNER_TOKEN")
    if expected and x_owner_token != expected:
        raise HTTPException(403, "owner token required")


def view(p: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in p.items() if k not in ("tool_call_json", "fingerprint")}
    out["tool_call"] = json.loads(p["tool_call_json"])
    out["amount_milli"] = p["amount"]
    out["amount"] = format_milli(p["amount"])
    return out


def guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except LedgerError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "rail": rail.name}


@app.get("/api/v1/state")
def state() -> dict[str, Any]:
    pol = ledger.policy()
    return {
        "mandate_id": ledger.mandate_id, "rail": rail.name, "unit": "USD-TDN thousandths",
        "balances": ledger.balances(), "policy": {**json.loads(pol.canonical()), "hash": pol.hash()},
        "purchases": [view(p) for p in ledger.purchases()], "chain": ledger.verify_chain(),
        "session_keys": ledger.session_keys(),
    }


@app.post("/api/v1/purchases", status_code=201)
def propose(body: PurchaseIn, x_session_key: str | None = Header(default=None)) -> dict[str, Any]:
    """Agent route. The agent can only propose; it never reaches the rail directly."""
    resolved = ledger.sessions.resolve(x_session_key) if x_session_key else None
    if not resolved or resolved[1] is not SessionState.ACTIVE:
        raise HTTPException(401, "session key missing, frozen or revoked")
    try:
        amount = parse_milli(body.amount)
    except MoneyError as exc:
        raise HTTPException(422, str(exc)) from exc
    if amount <= 0:
        raise HTTPException(422, "amount must be greater than zero")
    intent = Intent(body.seller_domain, body.seller_address, body.item, amount)
    return view(guard(orchestrator.submit, ledger, rail, seller, request_key=body.request_key, intent=intent,
                      tool_call=body.tool_call, session_key_id=resolved[0]))


@app.post("/api/v1/purchases/{purchase_id}/approve", dependencies=[Depends(owner)])
def approve(purchase_id: str) -> dict[str, Any]:
    guard(ledger.approve, purchase_id)
    return view(guard(orchestrator.execute, ledger, rail, seller, purchase_id))


@app.post("/api/v1/purchases/{purchase_id}/reject", dependencies=[Depends(owner)])
def reject(purchase_id: str) -> dict[str, Any]:
    guard(ledger.release, purchase_id)
    return view(ledger.purchase(purchase_id))


@app.post("/api/v1/purchases/{purchase_id}/reconcile", dependencies=[Depends(owner)])
def reconcile(purchase_id: str, rail_order_id: str | None = None) -> dict[str, Any]:
    return view(guard(orchestrator.reconcile, ledger, rail, seller, purchase_id, rail_order_id))


@app.patch("/api/v1/policy", dependencies=[Depends(owner)])
def patch_policy(body: PolicyPatch) -> dict[str, Any]:
    changes = body.model_dump(exclude_none=True)
    if "max_single_spend" in changes:
        try:
            changes["max_single_spend"] = parse_milli(changes["max_single_spend"])
        except MoneyError as exc:
            raise HTTPException(422, str(exc)) from exc
    if "whitelisted_sellers" in changes:
        changes["whitelisted_sellers"] = {k.lower(): v.lower() for k, v in changes["whitelisted_sellers"].items()}
    pol = guard(ledger.set_policy, **changes)
    return {**json.loads(pol.canonical()), "hash": pol.hash()}


@app.post("/api/v1/session-keys", dependencies=[Depends(owner)], status_code=201)
def issue_key() -> dict[str, str]:
    key_id, token = guard(ledger.issue_session_key)
    return {"key_id": key_id, "token": token, "note": "Shown once. Only its SHA-256 is stored."}


@app.post("/api/v1/kill-switch", dependencies=[Depends(owner)])
def kill_switch(body: KillSwitch) -> dict[str, Any]:
    """Engaging revokes every active key in the same transaction and returns the revocation slip."""
    if not body.engaged:
        guard(ledger.release_kill_switch)
        return {"engaged": False}
    revoked = guard(ledger.engage_kill_switch)
    slip = [e for e in ledger.events() if e["type"] == "SESSION_KEY_REVOKED" and e["ref"] in revoked][-len(revoked):] \
        if revoked else []
    return {"engaged": True, "revoked": revoked, "slip": slip, "chain": ledger.verify_chain()}


@app.get("/api/v1/purchases/{purchase_id}/evidence")
def evidence(purchase_id: str) -> dict[str, Any]:
    p = guard(ledger.purchase, purchase_id)
    return {"schema": "mandate.evidence.v1", "purchase": view(p),
            "events": [e for e in ledger.events() if e["purchase_id"] == purchase_id],
            "chain": ledger.verify_chain(), "redactions": ["credentials", "approval links", "session tokens"]}


@app.get("/api/v1/evidence/verify")
def verify() -> dict[str, Any]:
    return ledger.verify_chain()


@app.get("/api/v1/stream")
async def stream(request: Request, last_event_id: str | None = Header(default=None)) -> StreamingResponse:
    """Server-sent events, one per ledger event. Resumes from Last-Event-ID; read-only."""
    cursor = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0

    async def gen():
        nonlocal cursor
        yield "retry: 2000\n\n"
        idle = 0
        while not await request.is_disconnected():
            fresh = ledger.events(after_seq=cursor)
            for e in fresh:
                cursor = e["seq"]
                yield f"id: {e['seq']}\nevent: ledger.appended\ndata: {json.dumps(e, separators=(',', ':'))}\n\n"
            idle = 0 if fresh else idle + 1
            if idle >= 30:  # heartbeat roughly every 15 s
                idle = 0
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/events")
async def events(request: Request, last_event_id: str | None = Header(default=None)) -> StreamingResponse:
    """Alias of /api/v1/stream, the path the cockpit connects to."""
    return await stream(request, last_event_id)
