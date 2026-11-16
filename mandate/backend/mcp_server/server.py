"""Thin MCP adapter: all authority and accounting remain in the gateway."""
from __future__ import annotations

import json
import math
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any
from urllib.parse import quote

import httpx
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send


@dataclass(frozen=True)
class Config:
    gateway_url: str = "http://127.0.0.1:8000"
    host: str = "127.0.0.1"
    port: int = 8765
    allowed_origins: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        url = httpx.URL(self.gateway_url)
        if url.scheme not in ("http", "https") or not url.host or url.username or url.password or url.query or url.fragment:
            raise ValueError("Gateway URL must be HTTP(S), without credentials, query or fragment")
        if not 1 <= self.port <= 65535 or "*" in self.allowed_origins:
            raise ValueError("Valid port and explicit allowed origins required")

    @classmethod
    def from_env(cls) -> Config:
        return cls(gateway_url=os.getenv("MANDATE_GATEWAY_URL", cls.gateway_url),
                   host=os.getenv("MANDATE_MCP_HOST", cls.host), port=int(os.getenv("MANDATE_MCP_PORT", "8765")),
                   allowed_origins=tuple(value.strip() for value in os.getenv("MANDATE_MCP_ALLOWED_ORIGINS", "").split(",") if value.strip()))


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Proposal(Empty):
    seller_domain: Annotated[str, Field(min_length=1, max_length=253)]
    seller_address: Annotated[str, Field(min_length=1, max_length=128)]
    item: Annotated[str, Field(min_length=1, max_length=128)]
    amount: Annotated[str, Field(pattern=r"^[0-9]+(?:\.[0-9]{1,3})?$")]
    request_key: Annotated[str, Field(min_length=8, max_length=128)]
    arguments: dict[str, Any] = Field(default_factory=dict, max_length=20,
        description="At most 20 object keys total, string values <=500 characters, containers <=3 levels including root")

    @field_validator("arguments")
    @classmethod
    def bounded_arguments(cls, value: dict[str, Any]) -> dict[str, Any]:
        keys = 0

        def visit(node: Any, depth: int) -> None:
            nonlocal keys
            if isinstance(node, (dict, list)):
                if depth > 3:
                    raise ValueError("arguments containers must be no deeper than 3 levels")
                if isinstance(node, dict):
                    keys += len(node)
                    if keys > 20:
                        raise ValueError("arguments must contain at most 20 keys total")
                    children = node.values()
                else:
                    children = node
                for child in children:
                    visit(child, depth + 1 if isinstance(child, (dict, list)) else depth)
            elif isinstance(node, str):
                if len(node) > 500:
                    raise ValueError("argument strings must be at most 500 characters")
            elif isinstance(node, float) and not math.isfinite(node):
                raise ValueError("arguments must contain finite JSON values")
            elif node is not None and not isinstance(node, (str, int, float, bool)):
                raise ValueError("arguments must contain JSON values")

        visit(value, 1)
        return value


class Status(Empty):
    purchase_id: Annotated[str, Field(min_length=1, max_length=128)]


def tool_error(detail: Any) -> types.CallToolResult:
    return types.CallToolResult(isError=True, content=[types.TextContent(type="text", text=json.dumps(detail, ensure_ascii=False))])


class RequestGuard:
    def __init__(self, app: ASGIApp, config: Config) -> None:
        self.app = app
        self.config = config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            origins = headers.getlist("origin")
            if origins and (len(origins) != 1 or origins[0] not in self.config.allowed_origins):
                await JSONResponse({"detail": "Origin not allowed"}, status_code=403)(scope, receive, send)
                return
            credentials = headers.getlist("authorization")
            parts = credentials[0].split() if len(credentials) == 1 else []
            if len(parts) != 2 or parts[0].lower() != "bearer":
                await JSONResponse({"detail": "Agent bearer key required"}, status_code=401,
                                   headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(config: Config | None = None, *, gateway_transport: httpx.AsyncBaseTransport | None = None) -> ASGIApp:
    config = config or Config.from_env()
    server: Server = Server("Mandate")
    manager = StreamableHTTPSessionManager(server, stateless=True, json_response=True)
    client: httpx.AsyncClient | None = None
    definitions: dict[str, type[BaseModel]] = {"catalog": Empty, "propose_purchase": Proposal, "purchase_status": Status}

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [types.Tool(name=name, description={"catalog": "Discover Mandate's permitted catalog.",
                "propose_purchase": "Propose one exact purchase with your own stable request_key. Mandate decides whether it is allowed.",
                "purchase_status": "Read an existing purchase by its gateway ID."}[name], inputSchema=model.model_json_schema())
                for name, model in definitions.items()]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any] | types.CallToolResult:
        model = definitions.get(name)
        if model is None:
            return tool_error("Unknown tool")
        try:
            validated = model.model_validate(arguments)
        except ValidationError as exc:
            return tool_error([{"location": list(error["loc"]), "message": error["msg"]}
                               for error in exc.errors(include_input=False, include_url=False)])
        request = server.request_context.request
        key = request.headers["authorization"].split()[1]
        path = "/api/v1/catalog"
        body = None
        if isinstance(validated, Proposal):
            body = validated.model_dump(exclude={"arguments"})
            body["tool_call"] = {"tool": "propose_purchase", "arguments": validated.arguments}
            path = "/api/v1/purchases"
        elif isinstance(validated, Status):
            path = "/api/v1/purchases/" + quote(validated.purchase_id, safe="")
        assert client is not None
        try:
            response = await client.request("POST" if body is not None else "GET", path, json=body,
                                            headers={"X-Session-Key": key}, timeout=10.0)
        except httpx.HTTPError:
            if isinstance(validated, Proposal):
                return {"outcome": "unknown", "request_key": validated.request_key, "instruction":
                    "The outcome is unknown. To check it, call propose_purchase again with EXACTLY the same request_key and the same arguments. Mandate is idempotent on request_key: it returns the existing purchase, or creates it once if the first call never arrived; it never creates a second purchase. Do not change any argument and do not use a new request_key. If it still fails, stop and tell the owner, quoting the request_key."}
            return tool_error("Gateway unavailable; outcome unknown. Do not infer purchase success.")
        if response.status_code in (401, 403):
            return tool_error("session key missing, frozen or revoked")
        try:
            result = response.json()
        except ValueError:
            return tool_error("Gateway returned an invalid response; outcome unknown. Do not infer success.")
        if response.status_code in (409, 422):
            return tool_error(result.get("detail", result) if isinstance(result, dict) else result)
        if not response.is_success or not isinstance(result, dict):
            return tool_error("Gateway request failed; outcome unknown. Do not infer success or change the request_key.")
        if result.get("stage") == "PENDING_APPROVAL":
            result["guidance"] = "A person must approve this in Mandate; do not retry."
        return result

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        nonlocal client
        async with httpx.AsyncClient(base_url=config.gateway_url, transport=gateway_transport,
                                     timeout=10.0, follow_redirects=False, trust_env=False) as active:
            client = active
            async with manager.run():
                yield
            client = None

    class Endpoint:
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await manager.handle_request(scope, receive, send)

    return RequestGuard(Starlette(routes=[Route("/mcp", Endpoint(), methods=["GET", "POST", "DELETE"])], lifespan=lifespan), config)
