"""Tests for /rc RFC 8292 VAPID, RFC 8291 Web Push encryption, and alert routing."""

import base64
import json
import os
import stat
import struct
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from my_claude_code.config.settings import Settings
from my_claude_code.runtime.remote.notifications import (
    NotificationDispatcher,
    build_vapid_jwt,
    encrypt_web_push_payload,
    load_or_create_vapid_keys,
)
from my_claude_code.runtime.remote.store import RemoteStore


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def _decrypt_aes128gcm(
    ciphertext_frame: bytes,
    *,
    ua_private_key: ec.EllipticCurvePrivateKey,
    ua_public_bytes: bytes,
    auth_secret: bytes,
) -> bytes:
    salt = ciphertext_frame[:16]
    rs, key_id_len = struct.unpack("!IB", ciphertext_frame[16:21])
    assert rs == 4096
    assert key_id_len == 65
    as_pub_bytes = ciphertext_frame[21 : 21 + key_id_len]
    encrypted = ciphertext_frame[21 + key_id_len :]

    as_pub_key = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), as_pub_bytes
    )
    shared = ua_private_key.exchange(ec.ECDH(), as_pub_key)
    key_info = b"WebPush: info\x00" + ua_public_bytes + as_pub_bytes
    ikm = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=auth_secret,
        info=key_info,
    ).derive(shared)
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
    padded = AESGCM(cek).decrypt(nonce, encrypted, None)
    assert padded.endswith(b"\x02")
    return padded[:-1]


def test_vapid_key_generation_persistence_and_es256_jwt(tmp_path: Path) -> None:
    vapid_path = tmp_path / "rc_vapid.json"
    keys1 = load_or_create_vapid_keys(vapid_path)
    keys2 = load_or_create_vapid_keys(vapid_path)
    assert keys1 == keys2
    pub_raw = _b64url_decode(keys1["public_key_b64url"])
    assert len(pub_raw) == 65
    assert pub_raw[0] == 0x04

    if os.name != "nt":
        assert stat.S_IMODE(vapid_path.stat().st_mode) == 0o600

    token = build_vapid_jwt(
        private_pem=keys1["ec_private_pem"],
        endpoint="https://updates.push.services.mozilla.com/wpush/v2/abc123",
    )
    hdr_b64, claims_b64, sig_b64 = token.split(".")
    hdr = json.loads(_b64url_decode(hdr_b64))
    claims = json.loads(_b64url_decode(claims_b64))
    assert hdr == {"typ": "JWT", "alg": "ES256"}
    assert claims["aud"] == "https://updates.push.services.mozilla.com"

    raw_sig = _b64url_decode(sig_b64)
    assert len(raw_sig) == 64
    r_val = int.from_bytes(raw_sig[:32], "big")
    s_val = int.from_bytes(raw_sig[32:], "big")
    pub_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), pub_raw)
    pub_key.verify(
        encode_dss_signature(r_val, s_val),
        f"{hdr_b64}.{claims_b64}".encode("ascii"),
        ec.ECDSA(hashes.SHA256()),
    )


def test_rfc8291_aes128gcm_roundtrip_encryption() -> None:
    ua_private = ec.generate_private_key(ec.SECP256R1())
    ua_pub_bytes = ua_private.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    auth_secret = os.urandom(16)
    plaintext = json.dumps({"title": "Approval needed", "session_id": "rc_123"}).encode(
        "utf-8"
    )

    frame = encrypt_web_push_payload(
        plaintext=plaintext,
        p256dh_b64url=_b64url_encode(ua_pub_bytes),
        auth_b64url=_b64url_encode(auth_secret),
    )
    decrypted = _decrypt_aes128gcm(
        frame,
        ua_private_key=ua_private,
        ua_public_bytes=ua_pub_bytes,
        auth_secret=auth_secret,
    )
    assert decrypted == plaintext


@pytest.mark.asyncio
async def test_web_push_410_removes_expired_subscription_and_respects_alerts_gate(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path / "mcc_home"))
    (tmp_path / "mcc_home").mkdir(parents=True, exist_ok=True)
    store = RemoteStore()
    try:
        ua_private = ec.generate_private_key(ec.SECP256R1())
        ua_pub = _b64url_encode(
            ua_private.public_key().public_bytes(
                encoding=serialization.Encoding.X962,
                format=serialization.PublicFormat.UncompressedPoint,
            )
        )
        auth_b64 = _b64url_encode(os.urandom(16))

        ep_ok = "https://push.example/ok"
        ep_gone = "https://push.example/gone"
        store.upsert_push_subscription(
            endpoint=ep_ok, p256dh=ua_pub, auth=auth_b64, device_id="dev1"
        )
        store.upsert_push_subscription(
            endpoint=ep_gone, p256dh=ua_pub, auth=auth_b64, device_id="dev1"
        )

        sent_requests: list[tuple[str, dict[str, str], bytes]] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            sent_requests.append(
                (str(request.url), dict(request.headers), request.content)
            )
            if str(request.url) == ep_gone:
                return httpx.Response(410)
            return httpx.Response(201)

        transport = httpx.MockTransport(_handler)

        class _MockMessenger:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, object]] = []

            async def queue_send_message(
                self,
                chat_id: str,
                text: str,
                reply_to: str | None = None,
                parse_mode: str | None = None,
                fire_and_forget: bool = True,
                message_thread_id: str | None = None,
            ) -> str | None:
                self.calls.append((chat_id, text, parse_mode))
                return "msg_1"

            async def queue_edit_message(
                self,
                chat_id: str,
                message_id: str,
                text: str,
                parse_mode: str | None = None,
                fire_and_forget: bool = True,
            ) -> None:
                return None

            async def queue_delete_messages(
                self,
                chat_id: str,
                message_ids: list[str],
                fire_and_forget: bool = True,
            ) -> None:
                return None

            def fire_and_forget(self, task: Awaitable[Any]) -> None:
                return None

        messenger = _MockMessenger()
        dispatcher = NotificationDispatcher(
            store,
            messaging_target_provider=lambda: (messenger, ["chat_42"]),
            http_client_factory=lambda: httpx.AsyncClient(transport=transport),
        )

        session = {"id": "rc_1", "title": "claude — demo", "harness": "claude"}
        settings_off = Settings.model_validate({"RC_MESSAGING_ALERTS": False})
        await dispatcher.dispatch_session_event(
            session,
            "approval.requested",
            {"approval_id": "ap_1", "tool_name": "Bash"},
            settings_off,
        )

        # Web Push was delivered to ep_ok; 410 subscription removed; messenger gated OFF
        assert len(sent_requests) == 2
        assert sent_requests[0][1]["content-encoding"] == "aes128gcm"
        assert sent_requests[0][1]["authorization"].startswith("vapid t=")
        assert len(store.list_push_subscriptions()) == 1
        assert store.list_push_subscriptions()[0]["endpoint"] == ep_ok
        assert messenger.calls == []

        # Enable opt-in bridge -> dispatches plain text (parse_mode=None)
        settings_on = Settings.model_validate({"RC_MESSAGING_ALERTS": True})
        await dispatcher.dispatch_session_event(
            session,
            "turn.completed",
            {"stop_reason": "end_turn"},
            settings_on,
            telemetry={"reported_cost_usd": 0.015, "last_request": {"ttft_ms": 120.0}},
        )
        assert len(messenger.calls) == 1
        chat_id, msg_text, parse_mode = messenger.calls[0]
        assert chat_id == "chat_42"
        assert "Turn completed" in msg_text
        assert "$0.0150 reported" in msg_text
        assert parse_mode is None
    finally:
        store.close()
