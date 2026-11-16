"""MND-01: the Qatom sign-in script, run against a fake authorization server (no network)."""
import base64
import hashlib
import json
import stat
import urllib.parse
import urllib.request

import httpx
import pytest

from scripts import qatom_login

MCP = "https://mcp.example.test/mcp"
AS = "https://auth.example.test"


class FakeQatom:
    def __init__(self):
        self.registered, self.codes, self.refreshes = [], {}, 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "POST" and url == MCP:
            return httpx.Response(401, headers={"WWW-Authenticate":
                                                f'Bearer resource_metadata="{MCP.rsplit("/", 1)[0]}/.well-known/oauth-protected-resource/mcp"'})
        if url.endswith("/.well-known/oauth-protected-resource/mcp"):
            return httpx.Response(200, json={"resource": MCP, "authorization_servers": [AS]})
        if url == f"{AS}/.well-known/oauth-authorization-server":
            return httpx.Response(200, json={"issuer": AS, "authorization_endpoint": f"{AS}/authorize",
                                             "token_endpoint": f"{AS}/token", "registration_endpoint": f"{AS}/register",
                                             "code_challenge_methods_supported": ["S256"], "scopes_supported": ["agent"]})
        if url == f"{AS}/register":
            body = json.loads(req.content)
            assert body["token_endpoint_auth_method"] == "none"
            self.registered.append(body)
            return httpx.Response(201, json={"client_id": "cli_123"})
        if url == f"{AS}/token":
            form = dict(urllib.parse.parse_qsl(req.content.decode()))
            if form["grant_type"] == "authorization_code":
                challenge = self.codes.pop(form["code"])
                assert base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode() == challenge
                assert form["resource"] == MCP
                return httpx.Response(200, json={"access_token": "at_1", "refresh_token": "rt_1", "expires_in": 3600})
            self.refreshes += 1
            assert form["refresh_token"] == "rt_1"
            return httpx.Response(200, json={"access_token": "at_2", "expires_in": 3600})
        return httpx.Response(404)


@pytest.fixture
def fake(monkeypatch, tmp_path):
    q = FakeQatom()
    real = httpx.Client
    monkeypatch.setattr(qatom_login.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(q.handler), **kw))
    monkeypatch.setenv("QATOM_TOKEN_FILE", str(tmp_path / "tok.json"))
    monkeypatch.delenv("QATOM_CLIENT_ID", raising=False)
    return q


def _approve_in_browser(q: FakeQatom, *, tamper_state=False):
    def open_(url):
        p = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        assert p["code_challenge_method"] == "S256" and p["client_id"] == "cli_123" and p["resource"] == MCP
        q.codes["code_1"] = p["code_challenge"]
        state = "forged" if tamper_state else p["state"]
        urllib.request.urlopen(f"{p['redirect_uri']}?code=code_1&state={state}", timeout=5).read()
        return True
    return open_


def test_login_registers_uses_pkce_and_saves_owner_only(fake, monkeypatch, tmp_path):
    monkeypatch.setattr(qatom_login.webbrowser, "open", _approve_in_browser(fake))
    path = qatom_login.login(MCP)
    saved = json.loads(path.read_text())
    assert saved["access_token"] == "at_1" and saved["refresh_token"] == "rt_1" and saved["client_id"] == "cli_123"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert fake.registered[0]["redirect_uris"][0].startswith("http://127.0.0.1:")
    # the rail reads exactly this file
    from backend.services.qatom_rail import QatomConfig
    assert QatomConfig(token_file=str(path)).token() == "at_1"


def test_refresh_keeps_the_refresh_token_when_not_rotated(fake, monkeypatch):
    monkeypatch.setattr(qatom_login.webbrowser, "open", _approve_in_browser(fake))
    qatom_login.login(MCP)
    path = qatom_login.refresh()
    saved = json.loads(path.read_text())
    assert saved["access_token"] == "at_2" and saved["refresh_token"] == "rt_1" and fake.refreshes == 1
    assert "expires in" in qatom_login.status()


def test_state_mismatch_saves_nothing(fake, monkeypatch, tmp_path):
    monkeypatch.setattr(qatom_login.webbrowser, "open", _approve_in_browser(fake, tamper_state=True))
    with pytest.raises(SystemExit, match="state mismatch"):
        qatom_login.login(MCP)
    assert not (tmp_path / "tok.json").exists()
