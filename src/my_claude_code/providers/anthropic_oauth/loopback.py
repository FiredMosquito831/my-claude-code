"""Loopback-callback sign-in for a Claude subscription credential.

Claude Code itself supports two redirect targets (``l3t``, offset 182767278 of
the 2.1.260 bundle)::

    N.searchParams.append("redirect_uri",
        o ? Vt().MANUAL_REDIRECT_URL : `http://localhost:${r}/callback`);

MCC supports both, for the same reason Claude Code does. The loopback flow is
the one a person should get: they click "Sign in", approve in the browser, and
the browser hands the code straight back -- nothing to copy, nothing to paste,
and no chance of pasting the address bar instead of the code. The manual
redirect stays as the fallback for every case where the browser and this
process do not share a loopback namespace (WSL, SSH, a container, a remote
dashboard), which :func:`loopback_unavailable_reason` detects up front rather
than after a five-minute timeout.

The server binds an **ephemeral** port on ``127.0.0.1`` -- Claude Code's own
port is caller-supplied, so there is no well-known number to collide over -- and
lives only for the duration of one sign-in.

READ ``docs/ANTHROPIC-SUBSCRIPTION.md`` FIRST.
"""

import html
import os
import threading
import time
import webbrowser
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from loguru import logger

from .constants import (
    LOOPBACK_BIND_HOST,
    LOOPBACK_REDIRECT_PATH,
    LOOPBACK_TIMEOUT_SECONDS,
    loopback_redirect_uri,
)
from .oauth_login import (
    AnthropicOAuthLoginError,
    build_authorize_url,
    exchange_code,
    generate_pkce_verifier,
)

# Environments where "localhost" for the browser and "localhost" for this
# process are commonly two different loopback namespaces. Same list, and the
# same reasoning, as ``chatgpt_oauth/browser_login.py``.
_WSL_ENV_KEYS = ("WSL_DISTRO_NAME", "WSL_INTEROP")
_REMOTE_ENV_KEYS = (
    "SSH_CONNECTION",
    "SSH_CLIENT",
    "SSH_TTY",
    "CODESPACES",
    "GITPOD_WORKSPACE_ID",
    "REMOTE_CONTAINERS",
)

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
 body{{font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;
       margin:0;display:grid;place-items:center;height:100vh;
       background:#f6f6f5;color:#1a1a19}}
 main{{max-width:34rem;padding:2rem;text-align:center}}
 h1{{font-size:1.25rem;margin:0 0 .5rem}}
 p{{margin:0;color:#5c5c58}}
</style></head>
<body><main><h1>{title}</h1><p>{body}</p></main></body></html>
"""


class AnthropicOAuthLoopbackUnavailableError(AnthropicOAuthLoginError):
    """Raised when the loopback flow cannot be used; paste instead."""

    def __init__(self, detail: str) -> None:
        super().__init__(0, detail)


def loopback_unavailable_reason(
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Explain why the browser may not reach this process's ``localhost``.

    Returns ``None`` when the loopback flow should work.
    """
    environment = os.environ if environ is None else environ
    if any(environment.get(key, "").strip() for key in _WSL_ENV_KEYS):
        return (
            "MCC is running under WSL, while the browser commonly uses Windows "
            "localhost rather than WSL localhost"
        )
    if any(environment.get(key, "").strip() for key in _REMOTE_ENV_KEYS):
        return (
            "MCC is running in a remote development environment whose browser "
            "may not share this localhost"
        )
    return None


class _Flow:
    """One in-flight loopback sign-in."""

    def __init__(self, verifier: str, port: int) -> None:
        self.verifier = verifier
        self.port = port
        self.redirect_uri = loopback_redirect_uri(port)
        self.started_at = time.time()
        self.done = threading.Event()
        self.code: str | None = None
        self.state: str | None = None
        self.error: str | None = None
        self.tokens: dict[str, Any] | None = None

    @property
    def expired(self) -> bool:
        return time.time() - self.started_at > LOOPBACK_TIMEOUT_SECONDS


class _Handler(BaseHTTPRequestHandler):
    """Answers exactly one path and says nothing to the log."""

    server_version = "MCCAnthropicOAuth/1.0"
    flow: _Flow

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the stdlib's stderr access log."""

    def _page(self, status: int, title: str, body: str) -> None:
        payload = _PAGE.format(title=html.escape(title), body=html.escape(body))
        encoded = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != LOOPBACK_REDIRECT_PATH:
            self._page(404, "Not found", "This address is not part of the sign-in.")
            return
        query = parse_qs(parsed.query)
        flow = self.flow
        error = (query.get("error") or [""])[0].strip()
        code = (query.get("code") or [""])[0].strip()
        if error:
            flow.error = f"Anthropic returned '{error}'."
            flow.done.set()
            self._page(400, "Sign-in refused", flow.error)
            return
        if not code:
            flow.error = "The callback carried no authorization code."
            flow.done.set()
            self._page(400, "Sign-in failed", flow.error)
            return
        flow.code = code
        flow.state = (query.get("state") or [""])[0].strip() or None
        flow.done.set()
        self._page(
            200,
            "Signed in",
            "My Claude Code has the credential. You can close this tab.",
        )


# Module-level, because the dashboard's initiate and status calls are two
# separate HTTP requests into the same process, exactly like the ChatGPT
# browser flow. One sign-in at a time: a second initiate replaces the first.
_LOCK = threading.Lock()
_ACTIVE: _Flow | None = None
_SERVER: HTTPServer | None = None


def _shutdown_locked() -> None:
    global _SERVER, _ACTIVE
    if _SERVER is not None:
        try:
            _SERVER.shutdown()
            _SERVER.server_close()
        except OSError as error:  # pragma: no cover - best effort teardown
            logger.debug("Loopback callback server teardown: {}", error)
    _SERVER = None
    _ACTIVE = None


def start_loopback_login(
    *,
    allow_remote: bool = False,
    open_browser: bool = True,
) -> dict[str, str]:
    """Start a loopback sign-in and return the URL to open.

    ``allow_remote`` is the caller asserting that the browser really does share
    this process's loopback namespace, mirroring the ChatGPT browser flow's
    ``same_host_confirmed``.
    """
    global _ACTIVE, _SERVER
    if not allow_remote and (reason := loopback_unavailable_reason()):
        raise AnthropicOAuthLoopbackUnavailableError(
            f"{reason}. Use the paste flow instead, or confirm that the "
            "browser runs on this machine."
        )

    verifier = generate_pkce_verifier()
    with _LOCK:
        _shutdown_locked()
        server = HTTPServer((LOOPBACK_BIND_HOST, 0), _Handler)
        port = server.server_address[1]
        flow = _Flow(verifier, int(port))
        # The handler class is per-server here, so a second concurrent sign-in
        # cannot deliver its code into the first one's flow.
        handler = type("_BoundHandler", (_Handler,), {"flow": flow})
        server.RequestHandlerClass = handler
        thread = threading.Thread(
            target=server.serve_forever,
            name="mcc-anthropic-oauth-callback",
            daemon=True,
        )
        thread.start()
        _ACTIVE = flow
        _SERVER = server

    url = build_authorize_url(verifier, redirect_uri=flow.redirect_uri)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception as error:  # pragma: no cover - platform dependent
            logger.debug("Could not open a browser for the sign-in: {}", error)
    logger.info(
        "Claude subscription sign-in: listening on {}:{} for the callback.",
        LOOPBACK_BIND_HOST,
        flow.port,
    )
    return {"authorize_url": url, "redirect_uri": flow.redirect_uri}


def loopback_login_state() -> tuple[_Flow | None, bool]:
    """The in-flight flow and whether it has a code waiting."""
    with _LOCK:
        flow = _ACTIVE
    return flow, bool(flow is not None and flow.done.is_set())


async def loopback_login_status() -> dict[str, str]:
    """Poll the in-flight loopback sign-in.

    Returns ``{"status": "pending"|"complete"|"error", ...}``, matching the
    shape the ChatGPT browser flow's status route already returns so the
    dashboard's polling loop is the same code twice.
    """
    global _ACTIVE
    with _LOCK:
        flow = _ACTIVE
    if flow is None:
        return {"status": "error", "message": "No sign-in is in progress."}
    if not flow.done.is_set():
        if flow.expired:
            with _LOCK:
                _shutdown_locked()
            return {
                "status": "error",
                "message": "Timed out waiting for the browser to come back.",
            }
        return {"status": "pending", "message": "Waiting for the browser."}
    if flow.error:
        message = flow.error
        with _LOCK:
            _shutdown_locked()
        return {"status": "error", "message": message}

    assert flow.code is not None
    try:
        tokens = await exchange_code(
            flow.code,
            flow.verifier,
            flow.state,
            redirect_uri=flow.redirect_uri,
        )
    except AnthropicOAuthLoginError as error:
        with _LOCK:
            _shutdown_locked()
        return {"status": "error", "message": str(error)}
    finally:
        with _LOCK:
            _shutdown_locked()
    return {
        "status": "complete",
        "subscription_type": tokens.subscription_type or "",
        "message": "Signed in. Credential stored in MCC's private store.",
    }


def cancel_loopback_login() -> None:
    """Tear down any in-flight sign-in."""
    with _LOCK:
        _shutdown_locked()


__all__ = [
    "AnthropicOAuthLoopbackUnavailableError",
    "cancel_loopback_login",
    "loopback_login_state",
    "loopback_login_status",
    "loopback_unavailable_reason",
    "start_loopback_login",
]
