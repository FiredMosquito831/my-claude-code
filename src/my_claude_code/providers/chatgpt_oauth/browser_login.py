"""Browser-based ChatGPT/Codex OAuth login with PKCE and a local callback.

This mirrors OpenCode's primary "ChatGPT Pro/Plus (browser)" method: a local
HTTP server on ``127.0.0.1:1455`` receives the ``/auth/callback`` redirect,
and the authorization code is exchanged for tokens using PKCE (S256). The
user never copies a URL or code — the browser opens automatically and the
callback completes the flow.
"""

import base64
import hashlib
import html
import os
import secrets
import socket
import string
import sys
import threading
import time
import webbrowser
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from my_claude_code.config.constants import (
    CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
)

from .credentials import (
    CODEX_OAUTH_CLIENT_ID,
    CODEX_OAUTH_ORIGINATOR,
    CODEX_OAUTH_SCOPE,
    CODEX_OAUTH_TOKEN_URL,
    extract_account_id_from_tokens,
)
from .oauth_login import (
    ChatGPTOAuthLoginError,
    ChatGPTOAuthLoginTimeoutError,
    _write_managed_auth_file,
    perform_chatgpt_oauth_login,
)

CHATGPT_OAUTH_ISSUER = "https://auth.openai.com"
CHATGPT_OAUTH_AUTHORIZE_URL = f"{CHATGPT_OAUTH_ISSUER}/oauth/authorize"
OAUTH_CALLBACK_HOST = "localhost"
OAUTH_CALLBACK_PORT = 1455
OAUTH_CALLBACK_PORTS = (1455, 1457)
OAUTH_CALLBACK_PATH = "/auth/callback"
OAUTH_CANCEL_PATH = "/cancel"
OAUTH_SCOPE = CODEX_OAUTH_SCOPE
OAUTH_ORIGINATOR = CODEX_OAUTH_ORIGINATOR
BROWSER_LOGIN_TIMEOUT_SECONDS = 300.0
_WSL_ENV_KEYS = ("WSL_DISTRO_NAME", "WSL_INTEROP")
_REMOTE_ENV_KEYS = (
    "SSH_CONNECTION",
    "SSH_CLIENT",
    "SSH_TTY",
    "CODESPACES",
    "GITPOD_WORKSPACE_ID",
    "REMOTE_CONTAINERS",
)


class ChatGPTOAuthBrowserLoginError(ChatGPTOAuthLoginError):
    """Raised when the browser-based OAuth login fails."""


class ChatGPTOAuthBrowserUnavailableError(ChatGPTOAuthBrowserLoginError):
    """Raised when the browser flow cannot start (no browser or busy port)."""


def browser_callback_remote_reason(
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Explain why a browser may not share this process's loopback namespace."""

    environment = os.environ if environ is None else environ
    if any(environment.get(key, "").strip() for key in _WSL_ENV_KEYS):
        return (
            "MCC is running under WSL, while the browser commonly uses Windows "
            "localhost instead of WSL localhost"
        )
    if any(environment.get(key, "").strip() for key in _REMOTE_ENV_KEYS):
        return (
            "MCC is running in a remote development environment whose browser "
            "may not share the same localhost"
        )
    return None


def _require_same_host_browser(*, allow_remote: bool) -> None:
    """Reject automatic browser login when callback reachability is ambiguous."""

    if allow_remote:
        return
    if reason := browser_callback_remote_reason():
        raise ChatGPTOAuthBrowserUnavailableError(
            f"{reason}. Device-code login is required by default. "
            "Use the explicit same-device browser option only when the browser "
            "shares MCC's network namespace."
        )


def _generate_pkce() -> tuple[str, str]:
    """Return a (verifier, S256 challenge) pair shaped like OpenCode's."""
    alphabet = string.ascii_letters + string.digits + "-._~"
    verifier = "".join(secrets.choice(alphabet) for _ in range(43))
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(
    redirect_uri: str,
    code_challenge: str,
    state: str,
) -> str:
    """Build the ChatGPT OAuth authorize URL (Codex simplified flow)."""
    params = {
        "response_type": "code",
        "client_id": CODEX_OAUTH_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": OAUTH_SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "state": state,
        "originator": OAUTH_ORIGINATOR,
    }
    return f"{CHATGPT_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


def _callback_page(title: str, message: str, *, success: bool) -> bytes:
    color = "#10a37f" if success else "#d93025"
    safe_title = html.escape(title)
    safe_message = html.escape(message)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{safe_title}</title>
<style>
  body {{ font-family: system-ui, sans-serif; display: flex; min-height: 100vh;
         align-items: center; justify-content: center; margin: 0;
         background: #0d0d0d; color: #ececec; }}
  .card {{ text-align: center; max-width: 420px; padding: 32px; }}
  h1 {{ color: {color}; font-size: 1.4rem; margin-bottom: 12px; }}
  p {{ color: #b4b4b4; line-height: 1.5; }}
</style>
</head>
<body>
  <div class="card">
    <h1>{safe_title}</h1>
    <p>{safe_message}</p>
    <p>You can close this tab now.</p>
  </div>
  <script>setTimeout(function () {{ window.close(); }}, 3000);</script>
</body>
</html>""".encode()


def _exchange_code_for_tokens(
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> dict[str, Any]:
    """Exchange an authorization code for OAuth tokens (PKCE)."""
    response = httpx.post(
        CODEX_OAUTH_TOKEN_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "originator": CODEX_OAUTH_ORIGINATOR,
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": CODEX_OAUTH_CLIENT_ID,
            "code_verifier": code_verifier,
        },
        timeout=httpx.Timeout(30.0),
    )
    if response.status_code != 200:
        raise ChatGPTOAuthBrowserLoginError(
            f"Token exchange failed: HTTP {response.status_code}"
        )
    data = response.json()
    if not isinstance(data, dict):
        raise ChatGPTOAuthBrowserLoginError(
            "Token exchange response was not a JSON object"
        )
    return data


class _BrowserLoginFlow:
    """State for one in-flight browser login."""

    def __init__(self, port: int = OAUTH_CALLBACK_PORT) -> None:
        self.port = port
        self.verifier, self.challenge = _generate_pkce()
        self.state = secrets.token_urlsafe(32)
        self.done = threading.Event()
        self.deadline = time.monotonic() + BROWSER_LOGIN_TIMEOUT_SECONDS
        self.tokens: dict[str, Any] | None = None
        self.error: str | None = None

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{self.port}{OAUTH_CALLBACK_PATH}"

    def finish_tokens(self, tokens: dict[str, Any]) -> None:
        self.tokens = tokens
        self.done.set()

    def finish_error(self, message: str) -> None:
        self.error = message
        self.done.set()

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.deadline


class _CallbackHandler(BaseHTTPRequestHandler):
    """Handle the OAuth redirect from the user's browser."""

    server: _CallbackHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_page(
        self, status: int, title: str, message: str, *, success: bool
    ) -> None:
        body = _callback_page(title, message, success=success)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        flow = self.server.active_flow

        if parsed.path == OAUTH_CANCEL_PATH:
            if flow is not None:
                flow.finish_error("Login cancelled")
            self._send_page(
                200, "Login cancelled", "The login was cancelled.", success=False
            )
            return

        if parsed.path != OAUTH_CALLBACK_PATH:
            self.send_error(404)
            return

        params = parse_qs(parsed.query)
        state = (params.get("state") or [None])[0]
        if flow is None or state != flow.state:
            message = "Invalid state - potential CSRF attack"
            if flow is not None:
                flow.finish_error(message)
            self._send_page(400, "Login failed", message, success=False)
            return

        error = (params.get("error") or [None])[0]
        if error is not None:
            description = (params.get("error_description") or [error])[0]
            flow.finish_error(description)
            self._send_page(400, "Login failed", description, success=False)
            return

        code = (params.get("code") or [None])[0]
        if not code:
            message = "Missing authorization code"
            flow.finish_error(message)
            self._send_page(400, "Login failed", message, success=False)
            return

        try:
            tokens = _exchange_code_for_tokens(code, flow.redirect_uri, flow.verifier)
        except ChatGPTOAuthLoginError as exc:
            flow.finish_error(str(exc))
            self._send_page(200, "Login failed", str(exc), success=False)
            return

        flow.finish_tokens(tokens)
        self._send_page(
            200,
            "Login successful",
            "ChatGPT OAuth login completed. Returning to My Claude Code...",
            success=True,
        )


class _CallbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int = OAUTH_CALLBACK_PORT) -> None:
        self.active_flow: _BrowserLoginFlow | None = None
        self.flow_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), _CallbackHandler)

    def begin_flow(self, flow: _BrowserLoginFlow) -> None:
        with self.flow_lock:
            flow.port = self.server_address[1]
            self.active_flow = flow


class _CallbackIPv6HTTPServer(_CallbackHTTPServer):
    address_family = socket.AF_INET6

    def __init__(self, port: int = OAUTH_CALLBACK_PORT) -> None:
        self.active_flow = None
        self.flow_lock = threading.Lock()
        ThreadingHTTPServer.__init__(self, ("::1", port), _CallbackHandler)

    def server_bind(self) -> None:
        if hasattr(socket, "IPV6_V6ONLY"):
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        super().server_bind()


class _CallbackServerGroup:
    """One localhost callback port served on every available loopback family."""

    def __init__(self, servers: list[_CallbackHTTPServer]) -> None:
        self.servers = tuple(servers)

    @property
    def active_flow(self) -> _BrowserLoginFlow | None:
        return self.servers[0].active_flow

    def begin_flow(self, flow: _BrowserLoginFlow) -> None:
        flow.port = self.servers[0].server_address[1]
        for server in self.servers:
            server.begin_flow(flow)

    def start(self) -> None:
        for index, server in enumerate(self.servers):
            thread = threading.Thread(
                target=server.serve_forever,
                name=f"chatgpt-oauth-callback-{index}",
                daemon=True,
            )
            thread.start()


_SERVER_LOCK = threading.Lock()
_SERVER: _CallbackServerGroup | _CallbackHTTPServer | None = None


def _ensure_callback_server(
    *,
    allow_remote: bool = False,
) -> _CallbackServerGroup | _CallbackHTTPServer:
    """Start (or reuse) the local OAuth callback server."""
    _require_same_host_browser(allow_remote=allow_remote)
    global _SERVER
    with _SERVER_LOCK:
        if _SERVER is not None:
            return _SERVER
        errors: list[str] = []
        for port in OAUTH_CALLBACK_PORTS:
            servers: list[_CallbackHTTPServer] = []
            for server_type in (_CallbackHTTPServer, _CallbackIPv6HTTPServer):
                try:
                    servers.append(server_type(port))
                except OSError as exc:
                    errors.append(f"{server_type.__name__}:{port}: {exc}")
            if servers:
                group = _CallbackServerGroup(servers)
                group.start()
                _SERVER = group
                return group
        detail = "; ".join(errors)
        raise ChatGPTOAuthBrowserUnavailableError(
            "Could not bind localhost callback ports 1455 or 1457. "
            f"The device-code login will be used instead. ({detail})"
        )


def start_browser_login(*, allow_remote: bool = False) -> dict[str, str]:
    """Begin a browser login and return the authorize URL to open.

    Replaces any older pending flow. The caller opens the returned URL in the
    user's browser and then polls :func:`browser_login_status`. Remote/WSL
    callers must opt in explicitly because bind success does not prove that
    the browser can reach the same loopback namespace.
    """
    server = _ensure_callback_server(allow_remote=allow_remote)
    flow = _BrowserLoginFlow()
    server.begin_flow(flow)
    return {
        "authorize_url": build_authorize_url(
            flow.redirect_uri, flow.challenge, flow.state
        ),
        "expires_in": str(int(BROWSER_LOGIN_TIMEOUT_SECONDS)),
    }


def browser_login_status(
    *,
    auth_path: Path | None = None,
) -> dict[str, Any]:
    """Return the status of the in-flight browser login.

    Status values: ``idle`` (no flow started), ``pending``, ``complete``
    (tokens persisted and included), or ``error``.
    """
    server = _SERVER
    flow = server.active_flow if server is not None else None
    if flow is None:
        return {"status": "idle"}

    if not flow.done.is_set():
        if flow.expired:
            flow.finish_error("OAuth callback timeout - authorization took too long")
        else:
            return {"status": "pending"}

    if flow.tokens is not None:
        tokens = flow.tokens
        access_token = tokens.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            return {
                "status": "error",
                "message": "Token exchange returned no access token",
            }
        account_id = tokens.get("account_id")
        if not isinstance(account_id, str) or not account_id:
            account_id = extract_account_id_from_tokens(
                access_token=access_token,
                id_token=tokens.get("id_token"),
            )
        tokens["account_id"] = account_id
        _write_managed_auth_file(tokens, auth_path=auth_path)
        return {
            "status": "complete",
            "credential_reference": CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
            "account_id": account_id,
            "message": "Login successful. Credentials saved to MCC's private store.",
        }

    return {
        "status": "error",
        "message": flow.error or "Browser login failed",
    }


def perform_browser_login(
    *,
    timeout_seconds: float = BROWSER_LOGIN_TIMEOUT_SECONDS,
    auth_path: Path | None = None,
    allow_remote: bool = False,
) -> dict[str, Any]:
    """Run the full browser login: start server, open browser, wait, persist.

    Raises ChatGPTOAuthBrowserUnavailableError when the flow cannot start,
    ChatGPTOAuthLoginTimeoutError on timeout, or ChatGPTOAuthBrowserLoginError
    on failure. Returns the token payload on success.
    """
    server = _ensure_callback_server(allow_remote=allow_remote)
    flow = _BrowserLoginFlow()
    server.begin_flow(flow)
    authorize_url = build_authorize_url(flow.redirect_uri, flow.challenge, flow.state)

    print("Opening ChatGPT OAuth login in your browser...", flush=True)
    print(f"If it does not open, visit: {authorize_url}", flush=True)
    if not webbrowser.open(authorize_url):
        raise ChatGPTOAuthBrowserUnavailableError(
            "Could not open a browser automatically. "
            "Use --device for the headless device-code login instead."
        )

    if not flow.done.wait(timeout=timeout_seconds):
        raise ChatGPTOAuthLoginTimeoutError("Timed out waiting for the OAuth callback.")

    if flow.tokens is None:
        raise ChatGPTOAuthBrowserLoginError(flow.error or "Browser login failed")

    tokens = flow.tokens
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ChatGPTOAuthBrowserLoginError(
            "Token exchange did not return an access_token"
        )

    account_id = tokens.get("account_id")
    if not isinstance(account_id, str) or not account_id:
        account_id = extract_account_id_from_tokens(
            access_token=access_token,
            id_token=tokens.get("id_token"),
        )
    tokens["account_id"] = account_id
    path = _write_managed_auth_file(tokens, auth_path=auth_path)
    print(f"Tokens saved to: {path}", flush=True)
    if account_id:
        print(f"Account ID: {account_id}", flush=True)
    return tokens


def chatgpt_oauth_login_command() -> None:
    """CLI entry point for ``mcc-chatgpt-oauth-login``.

    Uses the browser PKCE flow by default on a local machine and falls back to
    device-code login when the callback may be remote or unavailable.
    ``--device`` forces device login; ``--browser`` explicitly allows browser
    login when the caller knows the browser shares FCC's localhost.
    """
    force_device = "--device" in sys.argv[1:]
    force_browser = "--browser" in sys.argv[1:]

    if force_device and force_browser:
        print(
            "Choose only one ChatGPT OAuth method: --device or --browser.",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(2)

    try:
        if force_browser:
            perform_browser_login(allow_remote=True)
            return
        if not force_device:
            try:
                perform_browser_login()
                return
            except ChatGPTOAuthBrowserUnavailableError as exc:
                print(f"Browser login unavailable: {exc}", file=sys.stderr, flush=True)
                print("Falling back to device-code login...", flush=True)
        perform_chatgpt_oauth_login()
    except ChatGPTOAuthLoginTimeoutError as exc:
        print(f"Timeout: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from exc
    except ChatGPTOAuthLoginError as exc:
        print(f"Login failed: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from exc
