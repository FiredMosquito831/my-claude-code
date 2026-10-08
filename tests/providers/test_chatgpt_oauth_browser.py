"""Tests for the browser-based ChatGPT OAuth login (PKCE + local callback)."""

import json
import threading
import urllib.parse

import httpx
import pytest

from my_claude_code.providers.chatgpt_oauth import browser_login
from my_claude_code.providers.chatgpt_oauth.browser_login import (
    _BrowserLoginFlow,
    _CallbackHTTPServer,
    _generate_pkce,
    build_authorize_url,
)

FAKE_TOKENS = {
    "access_token": "access_browser_1",
    "refresh_token": "refresh_browser_1",
    "id_token": "id_browser_1",
    "account_id": "acct_browser_1",
    "expires_in": 3600,
}


def test_generate_pkce_shapes():
    verifier, challenge = _generate_pkce()
    assert len(verifier) == 43
    assert len(challenge) == 43
    assert "=" not in challenge
    assert "+" not in challenge
    assert "/" not in challenge


def test_build_authorize_url_contains_codex_params():
    url = build_authorize_url("http://localhost:1455/auth/callback", "chal", "st")
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "auth.openai.com"
    assert parsed.path == "/oauth/authorize"
    assert params["response_type"] == ["code"]
    assert params["client_id"] == ["app_EMoamEEZ73f0CkXaXp7hrann"]
    assert params["redirect_uri"] == ["http://localhost:1455/auth/callback"]
    assert params["scope"] == [
        "openid profile email offline_access api.connectors.read api.connectors.invoke"
    ]
    assert params["code_challenge"] == ["chal"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["id_token_add_organizations"] == ["true"]
    assert params["codex_cli_simplified_flow"] == ["true"]
    assert params["state"] == ["st"]
    assert params["originator"] == ["codex_cli_rs"]


@pytest.fixture
def callback_server(monkeypatch):
    """Start a throwaway callback server on an ephemeral port.

    Tests never bind the production port (1455), so parallel xdist workers
    cannot steal each other's OAuth callbacks on Windows.
    """
    try:
        server = _CallbackHTTPServer(port=0)
    except OSError as exc:
        pytest.skip(f"Could not start callback server: {exc}")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        browser_login,
        "_exchange_code_for_tokens",
        lambda code, redirect_uri, code_verifier: FAKE_TOKENS,
    )
    monkeypatch.setattr(browser_login, "_SERVER", server)
    yield server
    server.shutdown()
    server.server_close()


def _begin_flow(server: _CallbackHTTPServer) -> _BrowserLoginFlow:
    flow = _BrowserLoginFlow()
    server.begin_flow(flow)
    return flow


def _callback_url(server: _CallbackHTTPServer, flow: _BrowserLoginFlow, **extra) -> str:
    params = {"code": "auth_code_1", "state": flow.state, **extra}
    port = server.server_address[1]
    return f"http://127.0.0.1:{port}/auth/callback?" + urllib.parse.urlencode(params)


@pytest.mark.local_serial
def test_browser_login_callback_completes_flow(callback_server, tmp_path):
    flow = _begin_flow(callback_server)

    response = httpx.get(_callback_url(callback_server, flow), timeout=10.0)

    assert response.status_code == 200
    assert "Login successful" in response.text
    assert flow.done.is_set()
    assert flow.tokens == FAKE_TOKENS

    status = browser_login.browser_login_status(
        auth_path=tmp_path / ".fcc" / "auth" / "chatgpt-oauth.json"
    )
    assert status["status"] == "complete"
    assert status["credential_reference"] == "fcc-managed-oauth"
    assert "access_token" not in status

    saved = json.loads(
        (tmp_path / ".fcc" / "auth" / "chatgpt-oauth.json").read_text(encoding="utf-8")
    )
    assert saved["tokens"]["access_token"] == "access_browser_1"
    assert saved["tokens"]["refresh_token"] == "refresh_browser_1"
    assert saved["tokens"]["id_token"] == "id_browser_1"


@pytest.mark.local_serial
def test_browser_login_callback_rejects_bad_state(callback_server, tmp_path):
    flow = _begin_flow(callback_server)

    url = _callback_url(callback_server, flow, state="forged-state")
    response = httpx.get(url, timeout=10.0)

    assert response.status_code == 400
    assert "Invalid state" in response.text
    assert flow.done.is_set()
    assert flow.tokens is None
    assert "CSRF" in (flow.error or "")

    status = browser_login.browser_login_status(
        auth_path=tmp_path / ".fcc" / "auth" / "chatgpt-oauth.json"
    )
    assert status["status"] == "error"


@pytest.mark.local_serial
def test_browser_login_callback_surfaces_oauth_error(callback_server):
    flow = _begin_flow(callback_server)

    params = urllib.parse.urlencode(
        {
            "error": "access_denied",
            "error_description": "User denied access",
            "state": flow.state,
        }
    )
    port = callback_server.server_address[1]
    response = httpx.get(
        f"http://127.0.0.1:{port}/auth/callback?{params}",
        timeout=10.0,
    )

    assert response.status_code == 400
    assert "User denied access" in response.text
    assert flow.error == "User denied access"


@pytest.mark.local_serial
def test_browser_login_callback_validates_state_before_oauth_error(callback_server):
    flow = _begin_flow(callback_server)
    params = urllib.parse.urlencode(
        {
            "error": "access_denied",
            "error_description": "forged",
            "state": "forged-state",
        }
    )
    port = callback_server.server_address[1]

    response = httpx.get(
        f"http://127.0.0.1:{port}/auth/callback?{params}",
        timeout=10.0,
    )

    assert response.status_code == 400
    assert "Invalid state" in response.text
    assert flow.error == "Invalid state - potential CSRF attack"


def test_callback_page_escapes_oauth_error_content():
    body = browser_login._callback_page(
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(2)>",
        success=False,
    ).decode()

    assert "<script>alert(1)</script>" not in body
    assert "<img src=x onerror=alert(2)>" not in body
    assert "&lt;script&gt;" in body
    assert "&lt;img" in body


@pytest.mark.local_serial
def test_start_browser_login_uses_server_port(callback_server):
    payload = browser_login.start_browser_login(allow_remote=True)

    parsed = urllib.parse.urlparse(payload["authorize_url"])
    params = urllib.parse.parse_qs(parsed.query)
    port = callback_server.server_address[1]
    assert params["redirect_uri"] == [f"http://localhost:{port}/auth/callback"]
    assert callback_server.active_flow is not None


def test_callback_server_tries_second_allowlisted_port(monkeypatch):
    attempts: list[int] = []

    class FakeServer:
        def __init__(self, port):
            attempts.append(port)
            if port == 1455:
                raise OSError("blocked")
            self.server_address = ("127.0.0.1", port)
            self.active_flow = None

        def begin_flow(self, flow):
            flow.port = self.server_address[1]
            self.active_flow = flow

        def serve_forever(self):
            return

    monkeypatch.setattr(browser_login, "_SERVER", None)
    monkeypatch.setattr(browser_login, "_CallbackHTTPServer", FakeServer)
    monkeypatch.setattr(browser_login, "_CallbackIPv6HTTPServer", FakeServer)

    payload = browser_login.start_browser_login(allow_remote=True)

    params = urllib.parse.parse_qs(
        urllib.parse.urlparse(payload["authorize_url"]).query
    )
    assert attempts == [1455, 1455, 1457, 1457]
    assert params["redirect_uri"] == ["http://localhost:1457/auth/callback"]


def test_callback_server_reports_immediate_fallback_when_ports_are_blocked(
    monkeypatch,
):
    class BlockedServer:
        def __init__(self, port):
            raise OSError(f"{port} blocked")

    monkeypatch.setattr(browser_login, "_SERVER", None)
    monkeypatch.setattr(browser_login, "_CallbackHTTPServer", BlockedServer)
    monkeypatch.setattr(browser_login, "_CallbackIPv6HTTPServer", BlockedServer)

    with pytest.raises(
        browser_login.ChatGPTOAuthBrowserUnavailableError,
        match="device-code login",
    ):
        browser_login.start_browser_login(allow_remote=True)


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ({"WSL_DISTRO_NAME": "Ubuntu-24.04"}, "WSL"),
        ({"WSL_INTEROP": "/run/WSL/123_interop"}, "WSL"),
        ({"SSH_CONNECTION": "client server"}, "remote development environment"),
        ({"CODESPACES": "true"}, "remote development environment"),
        ({}, None),
    ],
)
def test_browser_callback_remote_reason(environment, expected):
    reason = browser_login.browser_callback_remote_reason(environment)

    if expected is None:
        assert reason is None
    else:
        assert expected in (reason or "")


def test_start_browser_login_rejects_wsl_before_binding(monkeypatch):
    class UnexpectedServer:
        def __init__(self, port):
            raise AssertionError(f"callback server unexpectedly bound port {port}")

    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")
    monkeypatch.setattr(browser_login, "_SERVER", None)
    monkeypatch.setattr(browser_login, "_CallbackHTTPServer", UnexpectedServer)
    monkeypatch.setattr(browser_login, "_CallbackIPv6HTTPServer", UnexpectedServer)

    with pytest.raises(
        browser_login.ChatGPTOAuthBrowserUnavailableError,
        match="Device-code login is required by default",
    ):
        browser_login.start_browser_login()


@pytest.mark.local_serial
def test_explicit_same_device_browser_allows_wsl_override(
    callback_server,
    monkeypatch,
):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")

    payload = browser_login.start_browser_login(allow_remote=True)

    assert payload["authorize_url"].startswith(
        "https://auth.openai.com/oauth/authorize?"
    )


def test_cli_defaults_to_device_login_under_wsl(monkeypatch):
    device_calls: list[str] = []
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")
    monkeypatch.setattr(browser_login.sys, "argv", ["fcc-chatgpt-oauth-login"])
    monkeypatch.setattr(
        browser_login,
        "perform_chatgpt_oauth_login",
        lambda: device_calls.append("device"),
    )

    browser_login.chatgpt_oauth_login_command()

    assert device_calls == ["device"]


def test_cli_explicit_browser_override_under_wsl(monkeypatch):
    browser_calls: list[bool] = []
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")
    monkeypatch.setattr(
        browser_login.sys,
        "argv",
        ["fcc-chatgpt-oauth-login", "--browser"],
    )
    monkeypatch.setattr(
        browser_login,
        "perform_browser_login",
        lambda *, allow_remote=False: browser_calls.append(allow_remote),
    )
    monkeypatch.setattr(
        browser_login,
        "perform_chatgpt_oauth_login",
        lambda: pytest.fail("device login should not run"),
    )

    browser_login.chatgpt_oauth_login_command()

    assert browser_calls == [True]


def test_cli_rejects_conflicting_login_methods(monkeypatch):
    monkeypatch.setattr(
        browser_login.sys,
        "argv",
        ["fcc-chatgpt-oauth-login", "--device", "--browser"],
    )

    with pytest.raises(SystemExit) as exc_info:
        browser_login.chatgpt_oauth_login_command()

    assert exc_info.value.code == 2
