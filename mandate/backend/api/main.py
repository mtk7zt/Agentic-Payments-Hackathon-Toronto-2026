"""Mandate gateway API.

    uvicorn backend.api.main:app --reload

Agent routes take X-Session-Key. Owner routes require X-Owner-Token;
they fail closed when MANDATE_OWNER_TOKEN is unset.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ..core.ledger import Ledger, LedgerError
from ..core.policy import Intent
from ..core.sessions import SessionState
from ..models.money import MAX_MILLI, MoneyError, format_milli, parse_milli
from ..models.purchase import PurchaseIn
from ..services import orchestrator
from ..services.payment_rail import rail_from_env
from ..services.seller import seller_from_env
from .streams import GRANT_SECONDS, mint_grant, read_grant, stream_frames

DEMO_SELLERS = {
    "seller-a.example": "0x00000000000000000000000000000000000000a1",
    "seller-b.example": "0x00000000000000000000000000000000000000b2",
}

app = FastAPI(title="Mandate gateway", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("MANDATE_CORS_ORIGINS", "http://localhost:3000").split(","),
    allow_methods=["*"], allow_headers=["*"],
    allow_credentials=True,
)

ledger = Ledger(os.environ.get("MANDATE_DB", ":memory:"))
if not ledger.policy().whitelisted_sellers:
    ledger.set_policy(whitelisted_sellers=DEMO_SELLERS)
rail = rail_from_env(store=ledger)
seller = seller_from_env()


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PolicyPatch(Strict):
    expected_version: int = Field(strict=True, ge=1)
    max_single_spend: str | None = None
    strict_whitelist: bool | None = None
    max_attempts: int | None = Field(default=None, ge=1, le=5)
    whitelisted_sellers: dict[str, str] | None = None


class WalletObservation(Strict):
    balance_milli: int = Field(strict=True, ge=0, le=MAX_MILLI)
    reference: str = Field(min_length=1, max_length=512)


class KillSwitch(Strict):
    engaged: bool


class SessionChange(Strict):
    state: SessionState


class CheckpointVerification(Strict):
    checkpoint: dict[str, Any]


def owner(x_owner_token: str | None = Header(default=None)) -> None:
    expected = os.environ.get("MANDATE_OWNER_TOKEN")
    if not expected or not x_owner_token or not secrets.compare_digest(x_owner_token.encode(), expected.encode()):
        raise HTTPException(403, "owner token required")


def active_agent(x_session_key: str | None = Header(default=None)) -> str:
    resolved = ledger.sessions.resolve(x_session_key) if x_session_key else None
    if not resolved or resolved[1] is not SessionState.ACTIVE:
        raise HTTPException(401, "session key missing, frozen or revoked")
    return resolved[0]


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


@app.get("/api/v1/state", dependencies=[Depends(owner)])
def state() -> dict[str, Any]:
    pol = ledger.policy()
    return {
        "mandate_id": ledger.mandate_id, "rail": rail.name, "unit": "USD-TDN thousandths",
        "balances": ledger.balances(), "policy": {**json.loads(pol.canonical()), "hash": pol.hash()},
        "purchases": [view(p) for p in ledger.purchases()], "chain": ledger.verify_chain(),
        "session_keys": ledger.session_status(),
    }


@app.post("/api/v1/purchases", status_code=201)
def propose(body: PurchaseIn, session_key_id: str = Depends(active_agent)) -> dict[str, Any]:
    """Agent route. The agent can only propose; it never reaches the rail directly."""
    try:
        amount = parse_milli(body.amount)
    except MoneyError as exc:
        raise HTTPException(422, str(exc)) from exc
    if amount <= 0:
        raise HTTPException(422, "amount must be greater than zero")
    intent = Intent(body.seller_domain, body.seller_address, body.item, amount)
    return view(guard(orchestrator.submit, ledger, rail, seller, request_key=body.request_key, intent=intent,
                      tool_call=body.tool_call, session_key_id=session_key_id))


@app.get("/api/v1/catalog", dependencies=[Depends(active_agent)])
def discover() -> dict[str, Any]:
    return {
        "mandate_id": ledger.mandate_id, "rail": rail.name, "unit": "USD-TDN",
        "sellers": ledger.policy().whitelisted_sellers, "items": seller.catalog(),
    }


@app.get("/api/v1/purchases/{purchase_id}", dependencies=[Depends(active_agent)])
def purchase_status(purchase_id: str) -> dict[str, Any]:
    try:
        purchase = ledger.purchase(purchase_id)
    except LedgerError as exc:
        raise HTTPException(404, "purchase not found") from exc
    if purchase["mandate_id"] != ledger.mandate_id:
        raise HTTPException(404, "purchase not found")
    return view(purchase)


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
    """Engaging revokes active and frozen keys atomically and returns the revocation slip."""
    if not body.engaged:
        guard(ledger.release_kill_switch)
        return {"engaged": False}
    revoked = guard(ledger.engage_kill_switch)
    slip = [e for e in ledger.events() if e["type"] == "SESSION_KEY_REVOKED" and e["ref"] in revoked][-len(revoked):] \
        if revoked else []
    return {
        "engaged": True, "revoked": revoked, "slip": slip, "chain": ledger.verify_chain(),
        "notice": "Paused. Already dispatched payments may still complete; their held funds remain reserved.",
    }


@app.get("/api/v1/session-keys", dependencies=[Depends(owner)])
def session_status() -> list[dict[str, str]]:
    return ledger.session_status()


@app.post("/api/v1/session-keys/{key_id}/state", dependencies=[Depends(owner)])
def change_session_state(key_id: str, body: SessionChange) -> dict[str, str]:
    return guard(ledger.change_session_state, key_id, body.state)


@app.get("/api/v1/purchases/{purchase_id}/evidence", dependencies=[Depends(owner)])
def evidence(purchase_id: str) -> dict[str, Any]:
    p = guard(ledger.purchase, purchase_id)
    return {"schema": "mandate.evidence.v1", "purchase": view(p),
            "events": [e for e in ledger.events() if e["purchase_id"] == purchase_id],
            "chain": ledger.verify_chain(), "checkpoint": ledger.latest_checkpoint(),
            "redactions": ["credentials", "approval links", "session tokens"]}


@app.get("/api/v1/evidence/verify", dependencies=[Depends(owner)])
def verify() -> dict[str, Any]:
    return ledger.verify_chain()


@app.post("/api/v1/evidence/verify", dependencies=[Depends(owner)])
def verify_checkpoint(body: CheckpointVerification) -> dict[str, Any]:
    return ledger.verify_chain(checkpoint=body.checkpoint)


@app.post("/api/v1/evidence/checkpoints", dependencies=[Depends(owner)])
def create_checkpoint() -> dict[str, Any]:
    return guard(ledger.create_checkpoint)


@app.get("/api/v1/evidence/checkpoint", dependencies=[Depends(owner)])
def latest_checkpoint() -> dict[str, Any]:
    checkpoint = ledger.latest_checkpoint()
    return {"checkpoint": None, "note": "No signed checkpoint exists."} if checkpoint is None else checkpoint


@app.post("/api/v1/stream-session", dependencies=[Depends(owner)])
def stream_session(request: Request, response: Response) -> dict[str, int]:
    """Grant only read-only SSE authority; no credential is placed in a URL."""
    secret = os.environ["MANDATE_OWNER_TOKEN"]
    response.set_cookie("mandate_stream", mint_grant(secret, ledger.mandate_id, ledger.agent_id),
                        max_age=GRANT_SECONDS, httponly=True, secure=request.url.scheme == "https",
                        samesite="strict", path="/api")
    response.headers["Cache-Control"] = "no-store"
    return {"expires_in": GRANT_SECONDS}


@app.get("/api/events")
@app.get("/api/v1/stream")
async def stream(request: Request, last_event_id: str | None = Header(default=None),
                 after_seq: int = Query(default=0, ge=0, le=9007199254740991)) -> StreamingResponse:
    """Owner header or signed browser cookie; scoped, expiring, read-only SSE."""
    secret = os.environ.get("MANDATE_OWNER_TOKEN", "")
    supplied = request.headers.get("x-owner-token", "")
    if secret and supplied and secrets.compare_digest(secret.encode(), supplied.encode()):
        expires = time.time() + GRANT_SECONDS
    else:
        expires = read_grant(request.cookies.get("mandate_stream", ""), secret, ledger.mandate_id, ledger.agent_id)
        if expires is None:
            raise HTTPException(403, "owner stream session required")
    if last_event_id is not None:
        if not last_event_id.isascii() or not last_event_id.isdecimal() or len(last_event_id) > 16:
            raise HTTPException(422, "invalid Last-Event-ID")
        after_seq = int(last_event_id)
        if after_seq > 9007199254740991:
            raise HTTPException(422, "invalid Last-Event-ID")
    def valid() -> bool:
        return time.time() < expires and os.environ.get("MANDATE_OWNER_TOKEN") == secret
    return StreamingResponse(stream_frames(request, ledger, after_seq, valid), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/api/v1/wallet", dependencies=[Depends(owner)])
def wallet_status() -> dict[str, Any]:
    return ledger.wallet_status()


@app.post("/api/v1/wallet/configure", dependencies=[Depends(owner)])
def configure_wallet(body: WalletObservation) -> dict[str, Any]:
    return guard(ledger.configure_wallet, body.balance_milli, body.reference)


@app.post("/api/v1/wallet/observations", dependencies=[Depends(owner)])
def observe_wallet(body: WalletObservation) -> dict[str, Any]:
    return guard(ledger.record_wallet_observation, body.balance_milli, body.reference)


@app.post("/api/v1/wallet/reconcile", dependencies=[Depends(owner)])
def reconcile_wallet(body: WalletObservation) -> dict[str, Any]:
    return guard(ledger.reconcile_wallet, body.balance_milli, body.reference)
