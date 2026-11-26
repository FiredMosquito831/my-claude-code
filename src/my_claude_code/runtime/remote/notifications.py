"""Multi-channel Remote Control push notifications (RFC 8292/8291 Web Push + Telegram/Discord bridge).

All ``cryptography`` imports are strictly function-local so cold server startup
and ``mcc-desktop --print-status`` import zero ``cryptography`` modules.
"""

import base64
import contextlib
import hashlib
import json
import os
import struct
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from loguru import logger

from my_claude_code.config.paths import rc_vapid_path
from my_claude_code.config.settings import Settings
from my_claude_code.messaging.platforms.ports import OutboundMessenger

from .store import RemoteStore, _atomic_write_text

MessagingTargetProvider = Callable[[], tuple[OutboundMessenger | None, list[str]]]

_DEFAULT_VAPID_SUBJECT = "mailto:rc@localhost"
_DEFAULT_RECORD_SIZE = 4096
_DEDUP_TTL_SECONDS = 5.0
_EXPIRED_PUSH_STATUSES = frozenset({404, 410})
_VAPID_LOCK = threading.Lock()


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(raw: str) -> bytes:
    cleaned = raw.strip()
    padding = "=" * (-len(cleaned) % 4)
    return base64.urlsafe_b64decode(cleaned + padding)


def _generate_vapid_keypair_data() -> dict[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    pub_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return {
        "ec_private_pem": pem,
        "public_key_b64url": _b64url_encode(pub_bytes),
        "subject": _DEFAULT_VAPID_SUBJECT,
    }


def load_or_create_vapid_keys(path: Path | None = None) -> dict[str, str]:
    """Load persisted P-256 VAPID keys or generate atomically with ``0o600``."""

    target = path if path is not None else rc_vapid_path()
    with _VAPID_LOCK:
        if target.is_file():
            try:
                parsed = json.loads(target.read_text(encoding="utf-8"))
                if (
                    isinstance(parsed, dict)
                    and isinstance(parsed.get("ec_private_pem"), str)
                    and parsed["ec_private_pem"].strip()
                    and isinstance(parsed.get("public_key_b64url"), str)
                    and parsed["public_key_b64url"].strip()
                ):
                    return {
                        "ec_private_pem": parsed["ec_private_pem"],
                        "public_key_b64url": parsed["public_key_b64url"],
                        "subject": str(parsed.get("subject") or _DEFAULT_VAPID_SUBJECT),
                    }
            except OSError, json.JSONDecodeError:
                pass
        data = _generate_vapid_keypair_data()
        _atomic_write_text(
            target,
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            private=True,
        )
        return data


def build_vapid_jwt(
    *,
    private_pem: str,
    endpoint: str,
    subject: str = _DEFAULT_VAPID_SUBJECT,
    ttl_seconds: int = 43200,
) -> str:
    """Build an RFC 8292 ES256 VAPID JWT using raw 64-byte IEEE P1363 ``(r || s)``."""

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    parsed = urlsplit(endpoint)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid push subscription endpoint: {endpoint!r}")
    audience = f"{parsed.scheme}://{parsed.netloc}"
    now = int(time.time())
    header = {"typ": "JWT", "alg": "ES256"}
    claims = {
        "aud": audience,
        "exp": now + max(60, min(ttl_seconds, 86400)),
        "sub": subject,
    }
    header_b64 = _b64url_encode(
        json.dumps(header, separators=(",", ":")).encode("utf-8")
    )
    claims_b64 = _b64url_encode(
        json.dumps(claims, separators=(",", ":")).encode("utf-8")
    )
    signing_input = f"{header_b64}.{claims_b64}".encode("ascii")

    private_key = serialization.load_pem_private_key(
        private_pem.encode("utf-8"), password=None
    )
    if not isinstance(private_key, ec.EllipticCurvePrivateKey):
        raise TypeError("VAPID private key must be an EC P-256 key")
    der_sig = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r_val, s_val = decode_dss_signature(der_sig)
    raw_sig = r_val.to_bytes(32, "big") + s_val.to_bytes(32, "big")
    return f"{header_b64}.{claims_b64}.{_b64url_encode(raw_sig)}"


def encrypt_web_push_payload(
    *,
    plaintext: bytes,
    p256dh_b64url: str,
    auth_b64url: str,
) -> bytes:
    """Encrypt *plaintext* into RFC 8291 / RFC 8188 ``aes128gcm`` content framing."""

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    ua_pub_bytes = _b64url_decode(p256dh_b64url)
    if len(ua_pub_bytes) != 65 or ua_pub_bytes[0] != 0x04:
        raise ValueError("Subscription p256dh key must be an uncompressed P-256 point")
    auth_secret = _b64url_decode(auth_b64url)
    if len(auth_secret) != 16:
        raise ValueError("Subscription auth secret must be 16 bytes")

    ua_public_key = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), ua_pub_bytes
    )
    as_private_key = ec.generate_private_key(ec.SECP256R1())
    as_pub_bytes = as_private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    ecdh_secret = as_private_key.exchange(ec.ECDH(), ua_public_key)

    salt = os.urandom(16)
    key_info = b"WebPush: info\x00" + ua_pub_bytes + as_pub_bytes
    ikm = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=auth_secret,
        info=key_info,
    ).derive(ecdh_secret)

    cek = HKDF(
        algorithm=hashes.SHA256(),
        length=16,
        salt=salt,
        info=b"Content-Encoding: aes128gcm\x00",
    ).derive(ikm)
    nonce = HKDF(
        algorithm=hashes.SHA256(),
        length=12,
        salt=salt,
        info=b"Content-Encoding: nonce\x00",
    ).derive(ikm)

    padded_record = plaintext + b"\x02"
    ciphertext = AESGCM(cek).encrypt(nonce, padded_record, None)
    header = (
        salt
        + struct.pack("!IB", _DEFAULT_RECORD_SIZE, len(as_pub_bytes))
        + as_pub_bytes
    )
    return header + ciphertext


def _format_notification_content(
    session: Mapping[str, Any],
    kind: str,
    payload: Mapping[str, Any],
    *,
    telemetry: Mapping[str, Any] | None = None,
) -> tuple[str, str, str] | None:
    session_id = str(session.get("id") or "")
    title_label = str(session.get("title") or session.get("harness") or session_id)
    if kind == "approval.requested":
        tool_name = str(payload.get("tool_name") or "tool")
        return (
            f"Approval needed · {title_label}",
            f"Approve {tool_name} in Remote Control.",
            "high",
        )
    if kind == "question.requested":
        return (
            f"Question waiting · {title_label}",
            "Agent is waiting for your answer in Remote Control.",
            "high",
        )
    if kind == "turn.completed":
        summary_parts: list[str] = []
        if isinstance(telemetry, Mapping):
            reported = telemetry.get("reported_cost_usd")
            estimated = telemetry.get("estimated_cost_usd")
            total = telemetry.get("total_cost_usd")
            if isinstance(reported, (int, float)):
                summary_parts.append(f"${reported:.4f} reported")
            elif isinstance(estimated, (int, float)):
                summary_parts.append(f"${estimated:.4f} est")
            elif isinstance(total, (int, float)) and total > 0:
                summary_parts.append(f"${total:.4f}")
            last_req = telemetry.get("last_request")
            if isinstance(last_req, Mapping):
                ttft = last_req.get("ttft_ms")
                if isinstance(ttft, (int, float)):
                    summary_parts.append(f"TTFT {round(ttft)} ms")
                model = last_req.get("model")
                if model:
                    summary_parts.append(str(model))
        body = (
            " · ".join(summary_parts)
            if summary_parts
            else "Turn finished in Remote Control."
        )
        return (f"Turn completed · {title_label}", body, "normal")
    if kind == "session.error":
        msg = str(payload.get("message") or "Session encountered an error").strip()
        return (f"Session error · {title_label}", msg[:200], "high")
    if kind == "session.exited":
        rc = payload.get("returncode")
        if rc is None or rc == 0:
            return None
        return (
            f"Session exited ({rc}) · {title_label}",
            f"Process exited with non-zero code {rc}.",
            "high",
        )
    return None


class NotificationDispatcher:
    """Unified VAPID Web Push + Telegram/Discord alert dispatcher for /rc."""

    def __init__(
        self,
        store: RemoteStore,
        *,
        messaging_target_provider: MessagingTargetProvider | None = None,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._store = store
        self._messaging_target_provider = messaging_target_provider
        self._http_client_factory = http_client_factory
        self._dedup: dict[str, float] = {}
        self._lock = threading.Lock()

    def set_messaging_target_provider(
        self, provider: MessagingTargetProvider | None
    ) -> None:
        self._messaging_target_provider = provider

    def vapid_public_key(self) -> dict[str, Any]:
        keys = load_or_create_vapid_keys()
        return {
            "public_key": keys["public_key_b64url"],
            "enabled": True,
        }

    def subscribe_push(
        self,
        *,
        endpoint: str,
        p256dh: str,
        auth: str,
        device_id: str,
    ) -> dict[str, Any]:
        clean_ep = endpoint.strip()
        parsed = urlsplit(clean_ep)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise ValueError("Invalid push subscription endpoint URL")
        if not p256dh.strip() or not auth.strip():
            raise ValueError("Push subscription keys.p256dh and keys.auth are required")
        return self._store.upsert_push_subscription(
            endpoint=clean_ep,
            p256dh=p256dh.strip(),
            auth=auth.strip(),
            device_id=device_id,
        )

    def unsubscribe_push(self, endpoint: str) -> bool:
        return self._store.delete_push_subscription(endpoint.strip())

    def _should_dispatch(
        self, session_id: str, kind: str, payload: Mapping[str, Any]
    ) -> bool:
        detail_key = str(
            payload.get("approval_id")
            or payload.get("returncode")
            or payload.get("message")
            or ""
        )[:80]
        raw_key = f"{session_id}:{kind}:{detail_key}"
        digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:24]
        now = time.monotonic()
        with self._lock:
            expired = [
                k for k, ts in self._dedup.items() if now - ts > _DEDUP_TTL_SECONDS
            ]
            for k in expired:
                self._dedup.pop(k, None)
            prev = self._dedup.get(digest)
            if prev is not None and now - prev < _DEDUP_TTL_SECONDS:
                return False
            self._dedup[digest] = now
        return True

    async def send_web_push_to_subscriptions(
        self,
        subscriptions: Sequence[Mapping[str, Any]],
        payload: Mapping[str, Any],
        *,
        urgency: str = "normal",
    ) -> dict[str, int]:
        if not subscriptions:
            return {"sent": 0, "failed": 0, "removed": 0}
        vapid = load_or_create_vapid_keys()
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        sent = 0
        failed = 0
        removed = 0

        client_cm = (
            self._http_client_factory()
            if self._http_client_factory is not None
            else httpx.AsyncClient(timeout=10.0)
        )
        async with client_cm as client:
            for sub in subscriptions:
                endpoint = str(sub.get("endpoint") or "").strip()
                p256dh = str(sub.get("p256dh") or "").strip()
                auth = str(sub.get("auth") or "").strip()
                if not endpoint or not p256dh or not auth:
                    continue
                try:
                    jwt_token = build_vapid_jwt(
                        private_pem=vapid["ec_private_pem"],
                        endpoint=endpoint,
                        subject=vapid["subject"],
                    )
                    encrypted = encrypt_web_push_payload(
                        plaintext=body_bytes,
                        p256dh_b64url=p256dh,
                        auth_b64url=auth,
                    )
                    headers = {
                        "Authorization": (
                            f"vapid t={jwt_token}, k={vapid['public_key_b64url']}"
                        ),
                        "Content-Encoding": "aes128gcm",
                        "Content-Type": "application/octet-stream",
                        "TTL": "300",
                        "Urgency": urgency,
                    }
                    resp = await client.post(
                        endpoint, content=encrypted, headers=headers
                    )
                    if resp.status_code in _EXPIRED_PUSH_STATUSES:
                        self._store.delete_push_subscription(endpoint)
                        removed += 1
                    elif 200 <= resp.status_code < 300:
                        sent += 1
                    else:
                        failed += 1
                except Exception as exc:
                    failed += 1
                    logger.debug(
                        "Web Push send failed for {}: {}",
                        endpoint[:48],
                        type(exc).__name__,
                    )
        return {"sent": sent, "failed": failed, "removed": removed}

    async def send_test_push(self, device_id: str) -> dict[str, Any]:
        subs = self._store.list_push_subscriptions(device_id=device_id)
        if not subs:
            subs = self._store.list_push_subscriptions()
        payload = {
            "title": "MCC Remote Control",
            "body": "Test push notification is working.",
            "kind": "push.test",
            "session_id": "",
            "url": "/rc",
        }
        result = await self.send_web_push_to_subscriptions(
            subs, payload, urgency="normal"
        )
        return {**result, "subscriptions": len(subs)}

    async def dispatch_session_event(
        self,
        session: Mapping[str, Any],
        kind: str,
        payload: Mapping[str, Any],
        settings: Settings,
        *,
        telemetry: Mapping[str, Any] | None = None,
    ) -> None:
        formatted = _format_notification_content(
            session, kind, payload, telemetry=telemetry
        )
        if formatted is None:
            return
        session_id = str(session.get("id") or "")
        if not self._should_dispatch(session_id, kind, payload):
            return
        title, body, urgency = formatted

        subs = self._store.list_push_subscriptions()
        if subs:
            push_payload = {
                "title": title,
                "body": body,
                "kind": kind,
                "session_id": session_id,
                "url": f"/rc?session={session_id}" if session_id else "/rc",
            }
            with contextlib.suppress(Exception):
                await self.send_web_push_to_subscriptions(
                    subs, push_payload, urgency=urgency
                )

        if not bool(getattr(settings, "rc_messaging_alerts", False)):
            return
        if self._messaging_target_provider is None:
            return
        outbound, targets = self._messaging_target_provider()
        if outbound is None or not targets:
            return
        message_text = f"[MCC RC] {title}\n{body}"
        for chat_id in targets:
            with contextlib.suppress(Exception):
                await outbound.queue_send_message(
                    chat_id,
                    message_text,
                    parse_mode=None,
                    fire_and_forget=True,
                )
