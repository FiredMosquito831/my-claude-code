"""7.69.1: the dashboard's view of who owns a Claude credential.

A credential MCC read from Claude Code's ``.credentials.json`` -- by an Import
or by the automatic fallback -- is **shared**: Claude Code's file is the truth,
and MCC renews it only once it has expired, under Claude Code's own lock,
writing the result back. The card has to say which mode each row is in, why a
shared row is read-only, and what MCC last decided about it; and its
"Refresh now" on a shared row becomes "Re-read from Claude Code", which never
POSTs while the token is still valid.

Every test runs against a fixture ``CLAUDE_CONFIG_DIR`` and a fixture
``MCC_CONFIG_DIR`` under ``tmp_path``. Every token exchange is faked by
replacing ``credentials._post_refresh``; the suite-wide token-host block would
refuse a real one anyway.
"""

import dataclasses
import json
import sqlite3
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from my_claude_code.api import admin_routes
from my_claude_code.api.request_capture import RequestCapture
from my_claude_code.config import paths as config_paths
from my_claude_code.core.credential_attribution import (
    current_credential_event,
    record_credential_event,
)
from my_claude_code.core.request_log import (
    _ADDED_COLUMNS,
    _REQUEST_INSERT_COLUMNS,
    RequestLogStore,
    RequestRecord,
)
from my_claude_code.providers.anthropic_oauth import credentials as creds
from my_claude_code.providers.anthropic_oauth import shared
from my_claude_code.providers.oauth_account_store import ORIGIN_MCC
from my_claude_code.providers.oauth_ownership import (
    fingerprint,
    last_decision,
    record_decision,
)
from tests.api.support import create_test_app

PROVIDER = "anthropic_oauth"

SHARED_UUID = "uuid-shared-0001"
NATIVE_UUID = "uuid-native-0002"

OLD_ACCESS = "sk-ant-oat01-shared-access-old"
OLD_REFRESH = "sk-ant-ort01-shared-refresh-old"
NEW_ACCESS = "sk-ant-oat01-shared-access-new"
NEW_REFRESH = "sk-ant-ort01-shared-refresh-new"
NATIVE_ACCESS = "sk-ant-oat01-native-access"
NATIVE_REFRESH = "sk-ant-ort01-native-refresh"

#: Keys beside ``claudeAiOauth`` that a write-back must leave exactly as found,
#: including one this build has never heard of.
OTHER_KEYS: dict[str, Any] = {
    "mcpOAuth": {"server": {"accessToken": "mcp-not-ours", "expiresAt": 1}},
    "someFutureKey": ["kept", 1, {"nested": True}],
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclasses.dataclass(slots=True)
class Scene:
    """One isolated machine: a Claude Code config dir and an MCC config dir."""

    app: FastAPI
    claude_dir: Path
    credentials: Path
    mcc_dir: Path

    @property
    def refresh_lock(self) -> Path:
        return self.claude_dir / ".oauth_refresh.lock"

    @property
    def legacy_lock(self) -> Path:
        return self.claude_dir.parent / f"{self.claude_dir.name}.lock"

    @property
    def storage_lock(self) -> Path:
        return self.claude_dir / ".storage-write.lock"

    @property
    def owner_record(self) -> Path:
        return self.claude_dir / ".oauth_refresh.lock.owner"

    def write_claude(self, access: str, refresh: str, *, expires_in: float) -> None:
        document = {
            "claudeAiOauth": {
                "accessToken": access,
                "refreshToken": refresh,
                "expiresAt": int((time.time() + expires_in) * 1000),
                "scopes": ["user:inference", "user:profile"],
                "subscriptionType": "max",
            },
            **OTHER_KEYS,
        }
        self.credentials.write_text(json.dumps(document), encoding="utf-8")

    def identity(self, uuid: str, email: str) -> None:
        """``<CLAUDE_CONFIG_DIR>/.claude.json`` -- what an Import names."""
        (self.claude_dir / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"accountUuid": uuid, "emailAddress": email}}),
            encoding="utf-8",
        )

    def document(self) -> dict[str, Any]:
        return json.loads(self.credentials.read_text(encoding="utf-8"))


def _write_back(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    """Flip ``ANTHROPIC_OAUTH_WRITE_BACK`` for this test.

    Both names: the shared refresh path reads it through the ``credentials``
    module, while ``admin_routes`` imported the function under its own alias
    at import time, so patching one name alone would leave the card reading
    the real setting.
    """

    def value() -> bool:
        return enabled

    monkeypatch.setattr(creds, "write_back_enabled", value)
    monkeypatch.setattr(admin_routes, "anthropic_write_back_enabled", value)


def _scene(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, write_back: bool = True
) -> Scene:
    claude_dir = tmp_path / "claude-config"
    claude_dir.mkdir()
    mcc_dir = tmp_path / "mcc"
    mcc_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("MCC_CONFIG_DIR", str(mcc_dir))
    monkeypatch.delenv("ANTHROPIC_OAUTH_WRITE_BACK", raising=False)
    monkeypatch.chdir(tmp_path)
    config_paths.reset_config_dir_cache()
    _write_back(monkeypatch, write_back)
    # The platform is a seam so the Linux/Windows answer holds on any runner.
    monkeypatch.setattr(shared, "_platform", lambda: "win32")
    # Nothing here should ever wait on a lock; if something does, fail fast
    # rather than sit through Claude Code's real 5 x 1-2 s budget.
    fast = dataclasses.replace(
        shared.REFRESH_LOCK_TIMING,
        retries=1,
        retry_min_seconds=0.01,
        retry_max_seconds=0.02,
        liveness_seconds=0.1,
        liveness_poll_seconds=0.02,
        heartbeat_seconds=0.05,
    )
    monkeypatch.setattr(shared, "REFRESH_LOCK_TIMING", fast)
    monkeypatch.setattr(shared, "LEGACY_LOCK_TIMING", fast)
    creds._REFRESH_LOCKS.clear()
    return Scene(
        app=create_test_app(),
        claude_dir=claude_dir,
        credentials=claude_dir / ".credentials.json",
        mcc_dir=mcc_dir,
    )


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, client=("127.0.0.1", 50000))


def _refuse_every_post(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the token exchange with one that fails the test if reached."""

    posted: list[str] = []

    async def refuse(refresh_token: str) -> httpx.Response:
        posted.append(refresh_token)
        pytest.fail("a shared credential was POSTed to the token endpoint")

    monkeypatch.setattr(creds, "_post_refresh", refuse)
    return posted


def _import_claude_code(client: TestClient) -> creds.AccountRecord:
    """The dashboard's own Import button, against the fixture directory."""

    response = client.post("/admin/api/anthropic-oauth/import-claude-code", json={})
    assert response.status_code == 200, response.text
    record = creds.account_for(SHARED_UUID)
    assert record is not None
    return record


def _sign_in_natively() -> creds.AccountRecord:
    """What a loopback/paste sign-in stores: an ``origin=mcc`` record."""

    return creds.add_or_update_account(
        creds.OAuthTokens(
            access_token=NATIVE_ACCESS,
            refresh_token=NATIVE_REFRESH,
            expires_at=int(time.time() + 3600),
            subscription_type="pro",
            scopes=("user:inference",),
            account_uuid=NATIVE_UUID,
            account_email="native@example.test",
            source="mcc",
        ),
        origin=ORIGIN_MCC,
    )


def _sources(client: TestClient) -> dict[str, Any]:
    response = client.get("/admin/api/anthropic-oauth/sources")
    assert response.status_code == 200, response.text
    for secret in (OLD_ACCESS, OLD_REFRESH, NATIVE_ACCESS, NATIVE_REFRESH):
        assert secret not in response.text
    return response.json()


def _rows(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["account_id"]: row for row in data["accounts"]}


# ---------------------------------------------------------------------------
# /sources
# ---------------------------------------------------------------------------


def test_sources_reports_mode_reason_and_last_decision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scene = _scene(monkeypatch, tmp_path, write_back=True)
    scene.write_claude(OLD_ACCESS, OLD_REFRESH, expires_in=3600)
    scene.identity(SHARED_UUID, "shared@example.test")
    posted = _refuse_every_post(monkeypatch)
    client = _client(scene.app)
    untouched = scene.credentials.read_bytes()

    # The automatic fallback has no record, and it is still shared.
    fallback = _sources(client)["claude_code"]
    assert fallback["available"] is True
    assert fallback["mode"] == "shared"
    assert fallback["read_only_reason"] == ""
    assert fallback["last_decision"] == ""
    assert fallback["last_decision_at"] is None
    assert fallback["pending_write_back"] is False

    # Write-back off: MCC may never renew it, and the card says why.
    _write_back(monkeypatch, False)
    fallback = _sources(client)["claude_code"]
    assert fallback["mode"] == "shared"
    assert fallback["read_only_reason"] == "write-back-off"

    # A decision about the fallback lands on the fallback's row, timestamped.
    before = time.time()
    record_decision(PROVIDER, "claude-code", "shared:adopted")
    after = time.time()
    fallback = _sources(client)["claude_code"]
    assert fallback["last_decision"] == "shared:adopted"
    assert fallback["last_decision_at"] is not None
    assert before - 1.0 <= fallback["last_decision_at"] <= after + 1.0

    # An imported account is shared; one MCC signed in itself is native.
    imported = _import_claude_code(client)
    native = _sign_in_natively()
    rows = _rows(_sources(client))
    assert set(rows) == {imported.id, native.id}

    shared_row = rows[imported.id]
    assert shared_row["mode"] == "shared"
    assert shared_row["read_only_reason"] == "write-back-off"
    assert shared_row["pending_write_back"] is False
    # The fallback's decision belongs to the fallback's slot, not to this row.
    assert shared_row["last_decision"] == ""
    assert shared_row["last_decision_at"] is None

    native_row = rows[native.id]
    assert native_row["mode"] == "native"
    assert native_row["read_only_reason"] == ""
    assert native_row["pending_write_back"] is False
    assert native_row["last_decision"] == ""

    # Decisions are per account.
    record_decision(PROVIDER, imported.id, "shared:writeback-pending")
    rows = _rows(_sources(client))
    assert rows[imported.id]["last_decision"] == "shared:writeback-pending"
    assert rows[imported.id]["last_decision_at"] is not None
    assert rows[native.id]["last_decision"] == ""

    # A rotated token whose write-back has not landed is flagged (amber).
    updated = creds.update_account_record(
        imported.id,
        pending_write_back=True,
        pending_posted_fp=fingerprint(OLD_REFRESH),
    )
    assert updated is not None
    rows = _rows(_sources(client))
    assert rows[imported.id]["pending_write_back"] is True
    assert rows[native.id]["pending_write_back"] is False

    # Write-back back on, file present and writable: nothing read-only left.
    _write_back(monkeypatch, True)
    data = _sources(client)
    assert data["claude_code"]["read_only_reason"] == ""
    assert _rows(data)[imported.id]["read_only_reason"] == ""
    assert _rows(data)[native.id]["read_only_reason"] == ""

    # Reporting is read-only: no exchange, and Claude Code's file as found.
    assert posted == []
    assert scene.credentials.read_bytes() == untouched


# ---------------------------------------------------------------------------
# Refresh now on a shared row = "Re-read from Claude Code"
# ---------------------------------------------------------------------------


def test_refresh_now_on_a_valid_shared_row_never_posts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scene = _scene(monkeypatch, tmp_path, write_back=True)
    scene.write_claude(OLD_ACCESS, OLD_REFRESH, expires_in=3600)
    scene.identity(SHARED_UUID, "shared@example.test")
    posted = _refuse_every_post(monkeypatch)
    client = _client(scene.app)
    untouched = scene.credentials.read_bytes()
    stamp = scene.credentials.stat().st_mtime_ns

    # Nothing imported yet: the card's button acts on the fallback, which is
    # shared too (F4: before 7.69.1 this spent Claude Code's refresh token).
    response = client.post("/admin/api/anthropic-oauth/refresh", json={})
    assert response.status_code == 200, response.text
    assert "Re-read" in response.json()["message"]

    imported = _import_claude_code(client)
    for route in (
        "/admin/api/anthropic-oauth/refresh",
        f"/admin/api/anthropic-oauth/accounts/{imported.id}/refresh",
    ):
        response = client.post(route, json={})
        assert response.status_code == 200, (route, response.text)
        body = response.json()
        assert body["status"] == "complete"
        assert "Re-read" in body["message"], route
        assert "Claude Code" in body["message"], route
        assert body["expires_at"] is not None
        assert body["expires_at"] > time.time()
        assert OLD_ACCESS not in response.text
        assert OLD_REFRESH not in response.text

    assert posted == []
    # Claude Code's file is exactly as it was: same bytes, same stamp, no
    # backup, and no lock was ever taken on it.
    assert scene.credentials.read_bytes() == untouched
    assert scene.credentials.stat().st_mtime_ns == stamp
    assert list(scene.claude_dir.glob(".credentials.json.bak-*")) == []
    assert not scene.refresh_lock.exists()
    assert not scene.legacy_lock.exists()
    assert not scene.storage_lock.exists()
    # And the row is still shared, still holding the file's token.
    stored = creds.account_for(imported.id)
    assert stored is not None
    assert stored.is_shared
    assert stored.tokens.access_token == OLD_ACCESS
    assert stored.tokens.refresh_token == OLD_REFRESH


def test_refresh_now_on_an_expired_unchanged_shared_row_takes_the_locked_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scene = _scene(monkeypatch, tmp_path, write_back=True)
    scene.write_claude(OLD_ACCESS, OLD_REFRESH, expires_in=-600)
    scene.identity(SHARED_UUID, "shared@example.test")
    client = _client(scene.app)
    imported = _import_claude_code(client)

    posted: list[str] = []
    locks_held_during_post: list[tuple[bool, bool]] = []

    async def rotate_once(refresh_token: str) -> httpx.Response:
        posted.append(refresh_token)
        locks_held_during_post.append(
            (scene.refresh_lock.is_dir(), shared.legacy_lock_path().is_dir())
        )
        if len(posted) > 1:
            # Single-use: a second exchange of anything is a failure.
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(
            200,
            json={
                "access_token": NEW_ACCESS,
                "refresh_token": NEW_REFRESH,
                "expires_in": 3600,
            },
        )

    monkeypatch.setattr(creds, "_post_refresh", rotate_once)

    response = client.post(
        f"/admin/api/anthropic-oauth/accounts/{imported.id}/refresh", json={}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "complete"
    assert body["expires_at"] is not None
    assert body["expires_at"] > time.time()
    assert NEW_ACCESS not in response.text
    assert NEW_REFRESH not in response.text

    # Exactly one exchange, of the refresh token the file held, and it went
    # out while both of Claude Code's refresh locks were held.
    assert posted == [OLD_REFRESH]
    assert locks_held_during_post == [(True, True)]

    # Written back into Claude Code's file: claudeAiOauth replaced, every
    # other key exactly as found, one backup taken first.
    document = scene.document()
    block = document["claudeAiOauth"]
    assert block["accessToken"] == NEW_ACCESS
    assert block["refreshToken"] == NEW_REFRESH
    assert block["expiresAt"] > time.time() * 1000
    assert block["subscriptionType"] == "max"
    assert {
        key: value for key, value in document.items() if key != "claudeAiOauth"
    } == OTHER_KEYS
    assert len(list(scene.claude_dir.glob(".credentials.json.bak-*"))) == 1

    # Every lock released after the write, and Claude Code's owner record
    # never written.
    assert not scene.refresh_lock.exists()
    assert not scene.legacy_lock.exists()
    assert not shared.legacy_lock_path().exists()
    assert not scene.storage_lock.exists()
    assert not scene.owner_record.exists()

    decision = last_decision(PROVIDER, imported.id)
    assert decision is not None
    assert decision[0] == "shared:refreshed+wrote-back"

    # MCC's copy mirrors the file and the account is still shared.
    stored = creds.account_for(imported.id)
    assert stored is not None
    assert stored.is_shared
    assert stored.tokens.access_token == NEW_ACCESS
    assert stored.pending_write_back is False

    # The token is valid now, so pressing the button again re-reads only.
    again = client.post("/admin/api/anthropic-oauth/refresh", json={})
    assert again.status_code == 200, again.text
    assert posted == [OLD_REFRESH]
    row = _rows(_sources(client))[imported.id]
    assert row["mode"] == "shared"
    assert row["last_decision"] == "shared:refreshed+wrote-back"
    assert row["pending_write_back"] is False


# ---------------------------------------------------------------------------
# The request-log row (rule 15)
# ---------------------------------------------------------------------------


def _record(request_id: str, **overrides: Any) -> RequestRecord:
    defaults: dict[str, Any] = {
        "id": request_id,
        "endpoint": "/v1/messages",
        "protocol": "anthropic",
        "requested_model": "claude-sonnet-4-5",
        "provider": "anthropic_oauth",
        "resolved_model": "claude-sonnet-4-5",
        "stream": True,
        "input_text": "hello",
        "output_text": "world",
        "tokens_in": 10,
        "tokens_out": 20,
        "status": "success",
    }
    defaults.update(overrides)
    return RequestRecord(**defaults)


def _frames() -> list[str]:
    events = (
        ("message_start", {"type": "message_start", "message": {"usage": {}}}),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "hi"},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    )
    return [f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events]


async def _serve_one(
    store: RequestLogStore, request_id: str, decide: Any = None
) -> str | None:
    """One request through the real capture: install, stream, finalize."""

    # Constructing the capture is what installs the request's attribution
    # slot; the provider (here, the body) writes its decision into it from
    # inside the stream, and finalize copies it onto the row.
    capture = RequestCapture(
        store,
        request_id=request_id,
        endpoint="/v1/messages",
        protocol="anthropic",
        stream=True,
        requested_model="claude-sonnet-4-5",
        input_text="hello",
        params={"max_tokens": 16},
    )

    async def body() -> AsyncIterator[str]:
        if decide is not None:
            decide()
        for frame in _frames():
            yield frame

    seen: str | None = None
    async for _chunk in capture.wrap(body()):
        seen = current_credential_event()
    return seen


def _column(path: Path, request_id: str) -> Any:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT credential_event FROM requests WHERE id = ?", (request_id,)
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return row[0]


def _columns(path: Path) -> list[str]:
    connection = sqlite3.connect(path)
    try:
        return [row[1] for row in connection.execute("PRAGMA table_info(requests)")]
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_the_request_log_row_carries_the_credential_event(
    tmp_path: Path,
) -> None:
    assert (
        "credential_event",
        "ALTER TABLE requests ADD COLUMN credential_event TEXT",
    ) in _ADDED_COLUMNS
    assert [name for name, _sql in _ADDED_COLUMNS].count("credential_event") == 1
    assert "credential_event" in _REQUEST_INSERT_COLUMNS

    path = tmp_path / "requests.db"
    store = RequestLogStore(path, max_rows=100)
    try:
        # The attribution slot's own writer.
        seen = await _serve_one(
            store, "req-adopted", lambda: record_credential_event("shared:adopted")
        )
        assert seen == "shared:adopted"
        # The ownership module's emitter reaches the same row (persist=False
        # keeps it off the card state; the row is what is under test here).
        await _serve_one(
            store,
            "req-refreshed",
            lambda: record_decision(
                PROVIDER, "claude-code", "shared:refreshed+wrote-back", persist=False
            ),
        )
        # A plain use decides nothing, and says so as NULL. The slot is per
        # request: the previous request's code does not leak into this row.
        plain = await _serve_one(store, "req-plain")
        assert plain is None
    finally:
        store.close()

    assert _column(path, "req-adopted") == "shared:adopted"
    assert _column(path, "req-refreshed") == "shared:refreshed+wrote-back"
    assert _column(path, "req-plain") is None
    reader = RequestLogStore(path, max_rows=100)
    try:
        detail = reader.get_request("req-adopted")
    finally:
        reader.close()
    assert detail is not None
    assert detail["credential_event"] == "shared:adopted"

    # An existing database from before 7.69.1 gains the column through the
    # guarded ALTER, keeps its old rows as NULL, and stores new codes.
    legacy = tmp_path / "legacy.db"
    old = RequestLogStore(legacy, max_rows=100)
    old.enqueue(_record("req-before"))
    old.close()
    connection = sqlite3.connect(legacy)
    try:
        connection.execute("ALTER TABLE requests DROP COLUMN credential_event")
        connection.commit()
    finally:
        connection.close()
    assert "credential_event" not in _columns(legacy)

    migrated = RequestLogStore(legacy, max_rows=100)
    migrated.enqueue(_record("req-after", credential_event="shared:waited"))
    migrated.close()

    assert _columns(legacy).count("credential_event") == 1
    assert _column(legacy, "req-before") is None
    assert _column(legacy, "req-after") == "shared:waited"

    # Opening it again does not add the column twice.
    reopened = RequestLogStore(legacy, max_rows=100)
    reopened.close()
    assert _columns(legacy).count("credential_event") == 1
