"""Authentication, pairing, CSRF, and WebSocket Origin checks for /rc."""

import hashlib
import hmac
import ipaddress
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response, WebSocket

from my_claude_code.application.remote_ports import RemoteSessionPort
from my_claude_code.config.settings import get_settings

RC_SESSION_COOKIE = "mcc_rc_session"
RC_CSRF_COOKIE = "mcc_rc_csrf"
RC_CSRF_HEADER = "x-mcc-rc-csrf"

PAIRING_TOKEN_TTL_SECONDS = 60.0
PAIR_RATE_LIMIT_MAX_ATTEMPTS = 8
PAIR_RATE_LIMIT_WINDOW_SECONDS = 60.0
DEVICE_COOKIE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(slots=True)
class PairingTokenRecord:
    """Single-use short-lived pairing token issued from /admin/api/rc/pair-token."""

    token: str
    label: str
    created_at_mono: float
    expires_at_mono: float
    expires_at_epoch: float
    used: bool = False


class RemoteAuthError(Exception):
    """Authentication or rate-limit failure on /rc endpoints."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _is_loopback_ip(host: str | None) -> bool:
    if not host:
        return False
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _is_https_request(request: Request) -> bool:
    if request.url.scheme.lower() == "https":
        return True
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    first_proto = forwarded_proto.split(",", 1)[0].strip().lower()
    return first_proto == "https"


def _client_rate_key(request: Request) -> str:
    """Build a per-client rate-limit bucket key including tunnel forwarding hints."""

    client_host = request.client.host if request.client else "unknown"
    if _is_loopback_ip(client_host):
        cf_ip = request.headers.get("cf-connecting-ip", "").strip()
        if cf_ip:
            return f"cf:{cf_ip}"
        ts_user = request.headers.get("tailscale-user-login", "").strip()
        if ts_user:
            return f"ts:{ts_user}"
        xff = request.headers.get("x-forwarded-for", "").strip()
        if xff:
            first = xff.split(",", 1)[0].strip()
            if first:
                return f"xff:{first}"
    return f"ip:{client_host}"


def _sign_device_token(secret: str, device_id: str, issued_at: int) -> str:
    payload = f"{device_id}.{issued_at}".encode()
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"{device_id}.{issued_at}.{digest}"


def _verify_device_token(secret: str, raw: str) -> str | None:
    parts = raw.split(".")
    if len(parts) != 3:
        return None
    device_id, issued_raw, sig = parts
    if not device_id or not issued_raw.isdigit() or not sig:
        return None
    payload = f"{device_id}.{issued_raw}".encode()
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return device_id


def _normalize_host(raw: str | None) -> str | None:
    if not raw:
        return None
    candidate = raw.strip().lower()
    if not candidate:
        return None
    if "://" in candidate:
        parsed = urlsplit(candidate)
        return parsed.hostname.lower() if parsed.hostname else None
    if candidate.startswith("["):
        end = candidate.find("]")
        return candidate[1:end] if end != -1 else candidate
    host_only, _, _port = candidate.partition(":")
    return host_only or None


def _allowed_ws_hosts(websocket: WebSocket) -> set[str]:
    hosts: set[str] = set()
    host_header = _normalize_host(websocket.headers.get("host"))
    if host_header:
        hosts.add(host_header)
    client_host = websocket.client.host if websocket.client else None
    if _is_loopback_ip(client_host):
        fwd_host = _normalize_host(websocket.headers.get("x-forwarded-host"))
        if fwd_host:
            hosts.add(fwd_host)
    settings = get_settings()
    for configured in (
        settings.rc_tailscale_hostname,
        settings.rc_cloudflare_public_hostname,
    ):
        normalized = _normalize_host(configured)
        if normalized:
            hosts.add(normalized)
    return hosts


class RemoteAuthManager:
    """Per-app manager for pairing tokens, rate limiting, cookies, and WS auth."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, PairingTokenRecord] = {}
        self._failed_attempts: dict[str, list[float]] = {}

    def _prune_locked(self, now_mono: float) -> None:
        expired = [
            tok
            for tok, rec in self._tokens.items()
            if rec.used or rec.expires_at_mono <= now_mono
        ]
        for tok in expired:
            self._tokens.pop(tok, None)

        cutoff = now_mono - PAIR_RATE_LIMIT_WINDOW_SECONDS
        stale_keys: list[str] = []
        for key, stamps in self._failed_attempts.items():
            kept = [stamp for stamp in stamps if stamp > cutoff]
            if kept:
                self._failed_attempts[key] = kept
            else:
                stale_keys.append(key)
        for key in stale_keys:
            self._failed_attempts.pop(key, None)

    def issue_pairing_token(
        self,
        *,
        label: str = "",
        ttl_seconds: float = PAIRING_TOKEN_TTL_SECONDS,
    ) -> PairingTokenRecord:
        now_mono = time.monotonic()
        now_epoch = time.time()
        token = secrets.token_urlsafe(32)
        record = PairingTokenRecord(
            token=token,
            label=label.strip()[:80] or "Paired device",
            created_at_mono=now_mono,
            expires_at_mono=now_mono + ttl_seconds,
            expires_at_epoch=now_epoch + ttl_seconds,
        )
        with self._lock:
            self._prune_locked(now_mono)
            self._tokens[token] = record
        return record

    def redeem_pairing_token(
        self,
        *,
        token: str,
        label: str,
        request: Request,
        remote: RemoteSessionPort,
    ) -> tuple[dict[str, object], str, str]:
        now_mono = time.monotonic()
        rate_key = _client_rate_key(request)
        cleaned = token.strip()
        with self._lock:
            self._prune_locked(now_mono)
            recent_failures = self._failed_attempts.get(rate_key, [])
            if len(recent_failures) >= PAIR_RATE_LIMIT_MAX_ATTEMPTS:
                raise RemoteAuthError(
                    429,
                    "Too many pairing attempts. Wait 60 seconds and generate a new token.",
                )

            matched: PairingTokenRecord | None = None
            for candidate_key, rec in self._tokens.items():
                if hmac.compare_digest(candidate_key, cleaned):
                    matched = rec
                    break

            if (
                matched is None
                or matched.used
                or matched.expires_at_mono <= now_mono
                or not cleaned
            ):
                self._failed_attempts.setdefault(rate_key, []).append(now_mono)
                raise RemoteAuthError(401, "Invalid or expired pairing token")

            matched.used = True
            self._tokens.pop(matched.token, None)

        device_id = secrets.token_hex(16)
        device_label = (label.strip() or matched.label)[:80]
        user_agent = request.headers.get("user-agent", "")[:240]
        client_ip = rate_key
        device = remote.register_device(
            device_id=device_id,
            label=device_label,
            user_agent=user_agent,
            ip=client_ip,
        )
        session_cookie = _sign_device_token(
            remote.session_signing_secret(),
            device_id,
            int(time.time()),
        )
        csrf_token = secrets.token_urlsafe(24)
        return device, session_cookie, csrf_token

    def set_auth_cookies(
        self,
        response: Response,
        *,
        request: Request,
        session_cookie: str,
        csrf_token: str,
    ) -> None:
        secure = _is_https_request(request)
        response.set_cookie(
            key=RC_SESSION_COOKIE,
            value=session_cookie,
            max_age=DEVICE_COOKIE_MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
            path="/",
            secure=secure,
        )
        response.set_cookie(
            key=RC_CSRF_COOKIE,
            value=csrf_token,
            max_age=DEVICE_COOKIE_MAX_AGE_SECONDS,
            httponly=False,
            samesite="lax",
            path="/",
            secure=secure,
        )

    def clear_auth_cookies(self, response: Response, *, request: Request) -> None:
        secure = _is_https_request(request)
        response.delete_cookie(
            key=RC_SESSION_COOKIE, path="/", samesite="lax", secure=secure
        )
        response.delete_cookie(
            key=RC_CSRF_COOKIE, path="/", samesite="lax", secure=secure
        )

    def authenticate_request(
        self,
        request: Request,
        remote: RemoteSessionPort,
    ) -> str:
        raw_cookie = request.cookies.get(RC_SESSION_COOKIE, "")
        device_id = _verify_device_token(remote.session_signing_secret(), raw_cookie)
        if not device_id or not remote.is_device_active(device_id):
            raise HTTPException(
                status_code=401,
                detail="Remote Control session is not paired or has been revoked",
            )
        remote.touch_device(
            device_id,
            ip=_client_rate_key(request),
            user_agent=request.headers.get("user-agent", "")[:240],
        )
        return device_id

    def require_csrf(self, request: Request) -> None:
        if request.method.upper() not in _MUTATING_METHODS:
            return
        cookie_token = request.cookies.get(RC_CSRF_COOKIE, "")
        header_token = request.headers.get(RC_CSRF_HEADER, "")
        if (
            not cookie_token
            or not header_token
            or not hmac.compare_digest(cookie_token, header_token)
        ):
            raise HTTPException(
                status_code=403,
                detail="CSRF token missing or mismatched",
            )

    def authenticate_websocket(
        self,
        websocket: WebSocket,
        remote: RemoteSessionPort,
    ) -> str | None:
        """Verify WebSocket Origin (when provided) and signed non-revoked cookie."""

        origin = websocket.headers.get("origin")
        if origin is not None:
            stripped = origin.strip()
            if not stripped or stripped.lower() == "null":
                return None
            parsed = urlsplit(stripped)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                return None
            origin_host = parsed.hostname.lower()
            allowed = _allowed_ws_hosts(websocket)
            if origin_host not in allowed and not (
                _is_loopback_ip(origin_host)
                and any(_is_loopback_ip(host) for host in allowed)
            ):
                return None

        raw_cookie = websocket.cookies.get(RC_SESSION_COOKIE, "")
        device_id = _verify_device_token(remote.session_signing_secret(), raw_cookie)
        if not device_id or not remote.is_device_active(device_id):
            return None
        return device_id
