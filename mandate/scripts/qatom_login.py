#!/usr/bin/env python3
"""Sign the Mandate backend in to Qatom once and save the token to QATOM_TOKEN_FILE.

    python -m scripts.qatom_login            # opens your browser, you approve in Qatom
    python -m scripts.qatom_login --refresh  # renew with the saved refresh token
    python -m scripts.qatom_login --status   # show where the token is and when it expires

This follows the MCP authorization spec, so it needs no Qatom-specific endpoints:
  1. Ask the MCP server where its authorization server is (RFC 9728, with a 401 fallback).
  2. Read that server's metadata (RFC 8414 / OpenID discovery).
  3. Register this script as a public client (RFC 7591) unless QATOM_CLIENT_ID is set.
  4. Authorization code + PKCE (S256) with a one-time loopback redirect on 127.0.0.1.
  5. Exchange the code and save {access_token, refresh_token, expires_at, ...}.

The token file is written with owner-only permissions (0600). Nothing is printed except
where it was saved. Never commit it; it is outside the repo by default.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar

import httpx

DEFAULT_MCP_URL = "https://mcp.m.todaq.net/mcp"
DEFAULT_TOKEN_FILE = "~/.toda-mcp-token.json"
CLIENT_NAME = "Mandate gateway (local sign-in)"


def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


def token_path() -> Path:
    return Path(os.path.expanduser(_env("QATOM_TOKEN_FILE", DEFAULT_TOKEN_FILE)))


# ───────── discovery ─────────

def _origin(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def _resource_metadata_url(http: httpx.Client, mcp_url: str) -> list[str]:
    """Where to look for protected-resource metadata, best first."""
    urls: list[str] = []
    try:  # a bare MCP request without a token should answer 401 with a pointer to the metadata
        r = http.post(mcp_url, json={"jsonrpc": "2.0", "id": 0, "method": "ping"},
                      headers={"Accept": "application/json, text/event-stream"})
        challenge = r.headers.get("www-authenticate", "")
        for part in challenge.split(","):
            if "resource_metadata=" in part:
                urls.append(part.split("resource_metadata=", 1)[1].strip().strip('"'))
    except httpx.HTTPError:
        pass
    path = urllib.parse.urlsplit(mcp_url).path.rstrip("/")
    origin = _origin(mcp_url)
    urls += [f"{origin}/.well-known/oauth-protected-resource{path}", f"{origin}/.well-known/oauth-protected-resource"]
    return urls


def discover(http: httpx.Client, mcp_url: str) -> dict[str, Any]:
    issuer = None
    for url in _resource_metadata_url(http, mcp_url):
        try:
            r = http.get(url)
        except httpx.HTTPError:
            continue
        if r.status_code == 200:
            servers = r.json().get("authorization_servers") or []
            if servers:
                issuer = servers[0]
                break
    issuer = issuer or _origin(mcp_url)  # older servers host both on one origin
    base = issuer.rstrip("/")
    split = urllib.parse.urlsplit(base)
    candidates = [f"{split.scheme}://{split.netloc}/.well-known/oauth-authorization-server{split.path}",
                  f"{base}/.well-known/oauth-authorization-server",
                  f"{base}/.well-known/openid-configuration"]
    for url in candidates:
        try:
            r = http.get(url)
        except httpx.HTTPError:
            continue
        if r.status_code == 200:
            meta = r.json()
            if meta.get("authorization_endpoint") and meta.get("token_endpoint"):
                return meta
    raise SystemExit(f"Could not find Qatom's OAuth metadata from {mcp_url}. "
                     "Check QATOM_MCP_URL, or ask Qatom for their client id and set QATOM_CLIENT_ID.")


# ───────── PKCE + loopback ─────────

def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class _Catch(BaseHTTPRequestHandler):
    result: ClassVar[dict[str, str]] = {}

    def do_GET(self) -> None:
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
        if "code" in q or "error" in q:
            _Catch.result = q
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = "Signed in. You can close this tab." if "code" in q else "Sign-in was not completed."
        self.wfile.write(f"<!doctype html><title>Mandate</title><p style='font:16px system-ui'>{msg}</p>".encode())

    def log_message(self, *_: Any) -> None:  # keep the terminal clean
        pass


def _register(http: httpx.Client, meta: dict[str, Any], redirect_uri: str) -> str:
    client_id = os.environ.get("QATOM_CLIENT_ID")
    if client_id:
        return client_id
    reg = meta.get("registration_endpoint")
    if not reg:
        raise SystemExit("Qatom does not offer dynamic client registration. Set QATOM_CLIENT_ID and run again.")
    r = http.post(reg, json={"client_name": CLIENT_NAME, "redirect_uris": [redirect_uri],
                             "grant_types": ["authorization_code", "refresh_token"],
                             "response_types": ["code"], "token_endpoint_auth_method": "none"})
    if r.status_code not in (200, 201):
        raise SystemExit(f"Client registration failed (HTTP {r.status_code}).")
    return r.json()["client_id"]


def restrict_to_owner(path: Path) -> None:
    """Make the token file readable by its owner only.

    POSIX: mode 0600. Windows ignores those bits, so remove inherited access and grant only the
    current user full control with icacls. If that fails the file is deleted rather than left open.
    """
    if os.name != "nt":
        os.chmod(path, 0o600)
        return
    user = os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN", "")
    who = f"{domain}\\{user}" if domain else user
    result = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{who}:F"],
                            capture_output=True, text=True, check=False)
    if not user or result.returncode != 0:
        path.unlink(missing_ok=True)
        raise SystemExit("Could not restrict the token file to your Windows account; it was removed. "
                         "Run the sign-in again from your own user account.")


def _save(data: dict[str, Any], meta: dict[str, Any], client_id: str, mcp_url: str) -> Path:
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "access_token": data["access_token"],
        "token_type": data.get("token_type", "Bearer"),
        "refresh_token": data.get("refresh_token"),
        "expires_at": int(time.time()) + int(data["expires_in"]) if data.get("expires_in") else None,
        "scope": data.get("scope"),
        "client_id": client_id,
        "token_endpoint": meta["token_endpoint"],
        "mcp_url": mcp_url,
        "saved_at": int(time.time()),
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(record, f, indent=2)
    restrict_to_owner(path)
    return path


# ───────── commands ─────────

def login(mcp_url: str, open_browser: bool = True) -> Path:
    with httpx.Client(timeout=20, follow_redirects=True) as http:
        meta = discover(http, mcp_url)
        if "S256" not in meta.get("code_challenge_methods_supported", ["S256"]):
            raise SystemExit("Qatom's authorization server does not support PKCE S256; refusing to continue.")
        server = HTTPServer(("127.0.0.1", 0), _Catch)
        redirect_uri = f"http://127.0.0.1:{server.server_port}/callback"
        client_id = _register(http, meta, redirect_uri)
        verifier, challenge = _pkce()
        state = secrets.token_urlsafe(24)
        params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
                  "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
                  "resource": mcp_url}
        if meta.get("scopes_supported"):
            params["scope"] = " ".join(meta["scopes_supported"])
        url = f"{meta['authorization_endpoint']}?{urllib.parse.urlencode(params)}"
        _Catch.result = {}

        def serve() -> None:  # listen before the browser opens, so the redirect always lands
            server.timeout = 300
            for _ in range(5):
                if _Catch.result:
                    break
                server.handle_request()
        t = threading.Thread(target=serve, daemon=True)
        t.start()
        print("Opening Qatom sign-in in your browser. If it does not open, visit:\n" + url)
        if open_browser:
            webbrowser.open(url)
        t.join(timeout=305)
        server.server_close()
        got = _Catch.result
        if not got:
            raise SystemExit("Timed out waiting for the Qatom sign-in (5 minutes).")
        if got.get("error"):
            raise SystemExit(f"Qatom sign-in failed: {got.get('error')}")
        if not secrets.compare_digest(got.get("state", ""), state):
            raise SystemExit("Sign-in response did not match this request (state mismatch); nothing was saved.")
        r = http.post(meta["token_endpoint"], data={
            "grant_type": "authorization_code", "code": got["code"], "redirect_uri": redirect_uri,
            "client_id": client_id, "code_verifier": verifier, "resource": mcp_url})
        if r.status_code != 200 or "access_token" not in r.json():
            raise SystemExit(f"Token exchange failed (HTTP {r.status_code}).")
        return _save(r.json(), meta, client_id, mcp_url)


def refresh() -> Path:
    path = token_path()
    try:
        saved = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise SystemExit(f"No token at {path}. Run without --refresh first.") from exc
    if not saved.get("refresh_token"):
        raise SystemExit("The saved token has no refresh token. Sign in again.")
    with httpx.Client(timeout=20) as http:
        r = http.post(saved["token_endpoint"], data={"grant_type": "refresh_token", "client_id": saved["client_id"],
                                                     "refresh_token": saved["refresh_token"],
                                                     "resource": saved.get("mcp_url", DEFAULT_MCP_URL)})
    if r.status_code != 200 or "access_token" not in r.json():
        raise SystemExit(f"Refresh failed (HTTP {r.status_code}). Sign in again.")
    data = r.json()
    data.setdefault("refresh_token", saved["refresh_token"])  # some servers do not rotate it
    return _save(data, {"token_endpoint": saved["token_endpoint"]}, saved["client_id"], saved.get("mcp_url", DEFAULT_MCP_URL))


def status() -> str:
    path = token_path()
    if not path.exists():
        return f"No Qatom token at {path}."
    saved = json.loads(path.read_text())
    exp = saved.get("expires_at")
    left = "no expiry given" if not exp else ("expired" if exp <= time.time() else f"expires in {int(exp - time.time()) // 60} min")
    mode = oct(path.stat().st_mode & 0o777)
    return f"Qatom token at {path} ({mode}), {left}, refresh token {'saved' if saved.get('refresh_token') else 'missing'}."


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Sign the Mandate backend in to Qatom.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--refresh", action="store_true", help="renew the saved token")
    g.add_argument("--status", action="store_true", help="show the saved token's location and expiry")
    ap.add_argument("--no-browser", action="store_true", help="print the sign-in link instead of opening it")
    args = ap.parse_args(argv)
    if args.status:
        print(status())
        return 0
    path = refresh() if args.refresh else login(_env("QATOM_MCP_URL", DEFAULT_MCP_URL), not args.no_browser)
    print(f"Saved Qatom token to {path} (owner-only). Set MANDATE_RAIL=qatom to use it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
