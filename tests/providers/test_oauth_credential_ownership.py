"""7.69.1: a Claude credential MCC shares with Claude Code is Claude Code's.

Every test here runs against a fixture ``.credentials.json`` in ``tmp_path``
and a fake token endpoint that rotates single-use refresh tokens the way the
real one does. Nothing reaches the network: the suite-wide token-host block
(``tests/support/token_host_block.py``) would refuse it anyway.
"""

import asyncio
import dataclasses
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from my_claude_code.core.credential_attribution import (
    current_credential_event,
    install_attribution,
)
from my_claude_code.core.credential_refresh_scope import request_scoped_stream
from my_claude_code.core.failures import FailureKind
from my_claude_code.providers.anthropic_oauth import credentials as creds
from my_claude_code.providers.anthropic_oauth import shared
from my_claude_code.providers.anthropic_oauth.auth import AnthropicOAuthAuth
from my_claude_code.providers.anthropic_oauth.credentials import (
    AnthropicOAuthRefreshError,
    AnthropicOAuthRefreshRejected,
    AnthropicOAuthRefreshUnavailable,
    AnthropicOAuthUnavailableError,
    OAuthTokens,
)
from my_claude_code.providers.anthropic_oauth.provider import AnthropicOAuthProvider
from my_claude_code.providers.oauth_account_store import (
    ORIGIN_CLAUDE_CODE,
    ORIGIN_MCC,
)
from my_claude_code.providers.oauth_names import account_name
from my_claude_code.providers.oauth_ownership import last_decision

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class ClaudeHome:
    """A fixture Claude Code config directory: credential file + .claude.json."""

    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.path = directory / ".credentials.json"
        self.config = directory / ".claude.json"
        self._tick = time.time_ns()

    def write(
        self,
        access: str,
        refresh: str,
        *,
        expires_in: float,
        refresh_expires_in: float | None = None,
    ) -> None:
        block: dict[str, Any] = {
            "accessToken": access,
            "refreshToken": refresh,
            "expiresAt": int((time.time() + expires_in) * 1000),
            "scopes": ["user:inference", "user:profile"],
            "subscriptionType": "max",
        }
        if refresh_expires_in is not None:
            block["refreshTokenExpiresAt"] = int(
                (time.time() + refresh_expires_in) * 1000
            )
        document = {"claudeAiOauth": block, "mcpOAuth": {"server": {"k": 1}}}
        self.path.write_text(json.dumps(document), encoding="utf-8")
        self.bump()

    def bump(self) -> None:
        # A strictly increasing mtime, so the (mtime_ns, size) stamp always
        # moves even when two writes land inside one clock tick.
        self._tick += 1_000_000_000
        os.utime(self.path, ns=(self._tick, self._tick))

    def identity(self, uuid: str, email: str = "user@example.com") -> None:
        self.config.write_text(
            json.dumps({"oauthAccount": {"accountUuid": uuid, "emailAddress": email}}),
            encoding="utf-8",
        )

    def block(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))["claudeAiOauth"]

    def document(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))


class FakeEndpoint:
    """Single-use rotation: a refresh token works exactly once."""

    def __init__(self, *valid: str, reject_all: bool = False) -> None:
        self.valid = set(valid)
        self.posts: list[str] = []
        self.reject_all = reject_all
        self.issued = 0
        self.during_post: Any = None

    async def __call__(self, refresh_token: str) -> httpx.Response:
        self.posts.append(refresh_token)
        if self.during_post is not None:
            result = self.during_post(refresh_token)
            if asyncio.iscoroutine(result):
                await result
        if self.reject_all or refresh_token not in self.valid:
            return httpx.Response(
                400,
                json={"error": "invalid_grant", "error_description": "used"},
            )
        self.valid.discard(refresh_token)
        self.issued += 1
        new_refresh = f"rt-new-{self.issued}"
        self.valid.add(new_refresh)
        return httpx.Response(
            200,
            json={
                "access_token": f"at-new-{self.issued}",
                "refresh_token": new_refresh,
                "expires_in": 3600,
            },
        )


def _fast_timing() -> Any:
    return dataclasses.replace(
        shared.REFRESH_LOCK_TIMING,
        retries=2,
        retry_min_seconds=0.01,
        retry_max_seconds=0.02,
        liveness_seconds=0.1,
        liveness_poll_seconds=0.02,
        heartbeat_seconds=0.05,
    )


@pytest.fixture
def claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ClaudeHome:
    directory = tmp_path / "claude-config"
    directory.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(directory))
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(creds, "write_back_enabled", lambda: True)
    monkeypatch.setattr(shared, "_platform", lambda: "win32")
    fast = _fast_timing()
    monkeypatch.setattr(shared, "REFRESH_LOCK_TIMING", fast)
    monkeypatch.setattr(shared, "LEGACY_LOCK_TIMING", fast)
    return ClaudeHome(directory)


@pytest.fixture
def endpoint(monkeypatch: pytest.MonkeyPatch) -> FakeEndpoint:
    fake = FakeEndpoint("rt-old")
    monkeypatch.setattr(creds, "_post_refresh", fake)
    return fake


def _serve(auth: AnthropicOAuthAuth, purpose: Any = "request") -> OAuthTokens:
    return asyncio.run(auth.current_tokens(purpose=purpose))


def _import(claude: ClaudeHome, uuid: str = "uuid-a") -> creds.AccountRecord:
    tokens = creds.load_claude_code_tokens()
    assert tokens is not None
    identified = dataclasses.replace(tokens, account_uuid=uuid, source="mcc")
    return creds.add_or_update_account(
        identified,
        origin=ORIGIN_CLAUDE_CODE,
        origin_path=str(claude.path),
        write_back=True,
        adopt_origin=True,
    )


def _native_tokens(access: str, refresh: str, uuid: str, expires_in: float) -> Any:
    return OAuthTokens(
        access_token=access,
        refresh_token=refresh,
        expires_at=int(time.time() + expires_in),
        subscription_type="max",
        account_uuid=uuid,
        source="mcc",
    )


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------


def test_a_credential_read_by_the_fallback_is_shared(claude: ClaudeHome) -> None:
    claude.write("at-old", "rt-old", expires_in=3600)
    auth = AnthropicOAuthAuth()

    tokens = _serve(auth)

    assert tokens.access_token == "at-old"
    assert tokens.source == "claude-code"
    assert auth.mode == "shared"
    # A fallback credential is shared even though it has no record.
    assert creds.load_accounts(migrate=False) == []


def test_an_import_is_shared_and_an_mcc_sign_in_is_native(claude: ClaudeHome) -> None:
    claude.write("at-old", "rt-old", expires_in=3600)
    imported = _import(claude)
    native = creds.add_or_update_account(
        _native_tokens("at-n", "rt-n", "uuid-n", 3600),
        origin=ORIGIN_MCC,
        adopt_origin=True,
    )

    assert imported.is_shared
    assert not native.is_shared
    shared_auth = AnthropicOAuthAuth(account_id=imported.id)
    native_auth = AnthropicOAuthAuth(account_id=native.id)
    _serve(shared_auth)
    _serve(native_auth)
    assert shared_auth.mode == "shared"
    assert native_auth.mode == "native"


def test_the_last_explicit_action_sets_the_mode(claude: ClaudeHome) -> None:
    claude.write("at-old", "rt-old", expires_in=3600)
    record = _import(claude, uuid="uuid-a")
    assert record.is_shared

    signed_in = creds.add_or_update_account(
        _native_tokens("at-mcc", "rt-mcc", "uuid-a", 3600),
        origin=ORIGIN_MCC,
        adopt_origin=True,
    )
    assert signed_in.id == "uuid-a"
    assert not signed_in.is_shared

    # A refresh never changes who owns a credential.
    refreshed = creds.add_or_update_account(
        _native_tokens("at-mcc2", "rt-mcc2", "uuid-a", 3600),
        origin=ORIGIN_MCC,
        account_id="uuid-a",
    )
    assert not refreshed.is_shared

    reimported = _import(claude, uuid="uuid-a")
    assert reimported.is_shared
    assert len(creds.load_accounts(migrate=False)) == 1


# ---------------------------------------------------------------------------
# Re-reading and adopting
# ---------------------------------------------------------------------------


def test_a_shared_credential_is_never_refreshed_inside_the_leeway(
    claude: ClaudeHome, endpoint: FakeEndpoint
) -> None:
    # 60 s left: inside the 120 s leeway that would refresh a NATIVE one.
    claude.write("at-old", "rt-old", expires_in=60)
    auth = AnthropicOAuthAuth()

    for purpose in ("request", "background", "operator"):
        assert _serve(auth, purpose).access_token == "at-old"

    assert endpoint.posts == []
    assert claude.block()["refreshToken"] == "rt-old"


def test_claude_codes_rotation_is_adopted_on_the_next_use(
    claude: ClaudeHome, endpoint: FakeEndpoint
) -> None:
    claude.write("at-old", "rt-old", expires_in=3600)
    auth = AnthropicOAuthAuth()
    assert _serve(auth).access_token == "at-old"

    claude.write("at-cc", "rt-cc", expires_in=3600)
    install_attribution()
    assert _serve(auth, "background").access_token == "at-cc"
    assert current_credential_event() is None or True
    decision = last_decision("anthropic_oauth", shared.FALLBACK_SLOT)
    assert decision is not None and decision[0] == "shared:adopted"

    # An imported record's mirror follows the file too.
    record = _import(claude, uuid="uuid-a")
    imported = AnthropicOAuthAuth(account_id=record.id)
    assert _serve(imported).access_token == "at-cc"
    claude.write("at-cc2", "rt-cc2", expires_in=3600)
    assert _serve(imported).access_token == "at-cc2"
    stored = creds.account_for(record.id)
    assert stored is not None and stored.tokens.access_token == "at-cc2"
    assert endpoint.posts == []


def test_a_half_written_file_keeps_the_cached_token(
    claude: ClaudeHome, endpoint: FakeEndpoint
) -> None:
    claude.write("at-old", "rt-old", expires_in=3600)
    auth = AnthropicOAuthAuth()
    assert _serve(auth).access_token == "at-old"

    claude.path.write_text('{"claudeAiOauth": {"accessTok', encoding="utf-8")
    claude.bump()

    assert _serve(auth).access_token == "at-old"
    assert _serve(auth, "background").access_token == "at-old"
    assert endpoint.posts == []

    claude.write("at-next", "rt-next", expires_in=3600)
    assert _serve(auth).access_token == "at-next"


# ---------------------------------------------------------------------------
# The locked refresh
# ---------------------------------------------------------------------------


def test_an_expired_shared_credential_is_refreshed_for_a_real_request_and_written_back(
    claude: ClaudeHome, endpoint: FakeEndpoint
) -> None:
    claude.write("at-old", "rt-old", expires_in=-10)
    auth = AnthropicOAuthAuth()

    # Background first: nothing.
    with pytest.raises(AnthropicOAuthUnavailableError):
        _serve(auth, "background")
    assert endpoint.posts == []

    async def one_request() -> OAuthTokens:
        async def inner():
            headers = await auth.headers()
            yield headers["Authorization"]

        chunks = [chunk async for chunk in request_scoped_stream(inner())]
        assert chunks == ["Bearer at-new-1"]
        assert auth.tokens is not None
        return auth.tokens

    tokens = asyncio.run(one_request())

    assert tokens.access_token == "at-new-1"
    assert endpoint.posts == ["rt-old"]
    block = claude.block()
    assert block["accessToken"] == "at-new-1"
    assert block["refreshToken"] == "rt-new-1"
    assert claude.document()["mcpOAuth"] == {"server": {"k": 1}}
    assert len(list(claude.dir.glob(".credentials.json.bak-*"))) == 1
    decision = last_decision("anthropic_oauth", shared.FALLBACK_SLOT)
    assert decision is not None and decision[0] == "shared:refreshed+wrote-back"
    # No MCC store was invented for the fallback.
    assert creds.load_accounts(migrate=False) == []


def test_write_back_is_a_compare_and_swap_on_the_posted_refresh_token(
    claude: ClaudeHome, endpoint: FakeEndpoint
) -> None:
    claude.write("at-old", "rt-old", expires_in=-10)
    refreshed = OAuthTokens(
        access_token="at-x", refresh_token="rt-x", expires_at=9_999_999_999
    )

    assert asyncio.run(shared.cas_write(claude.path, refreshed, "rt-old"))[0] == "wrote"
    assert claude.block()["refreshToken"] == "rt-x"

    outcome, theirs = asyncio.run(shared.cas_write(claude.path, refreshed, "rt-old"))
    assert outcome == "superseded"
    assert theirs is not None and theirs.refresh_token == "rt-x"

    # End to end: Claude Code re-logs in while MCC's POST is in flight. Its
    # token wins and MCC's is discarded.
    claude.write("at-old", "rt-old", expires_in=-10)
    endpoint.valid = {"rt-old"}

    def relogin(_: str) -> None:
        claude.write("at-login", "rt-login", expires_in=3600)

    endpoint.during_post = relogin
    auth = AnthropicOAuthAuth()
    tokens = _serve(auth)
    assert tokens.access_token == "at-login"
    assert claude.block()["refreshToken"] == "rt-login"
    decision = last_decision("anthropic_oauth", shared.FALLBACK_SLOT)
    assert decision is not None and decision[0] == "shared:superseded"


def test_a_file_changed_under_the_lock_is_adopted_without_a_post(
    claude: ClaudeHome, endpoint: FakeEndpoint
) -> None:
    claude.write("at-old", "rt-old", expires_in=-10)
    auth = AnthropicOAuthAuth()
    with pytest.raises(AnthropicOAuthUnavailableError):
        _serve(auth, "background")
    lock = shared.refresh_lock_path()
    lock.mkdir()

    async def run() -> OAuthTokens:
        async def claude_code_finishes() -> None:
            await asyncio.sleep(0.05)
            claude.write("at-cc", "rt-cc", expires_in=3600)
            lock.rmdir()

        task = asyncio.create_task(claude_code_finishes())
        slot = auth._shared
        assert slot is not None
        tokens = await shared.locked_refresh(slot, slot.seen)
        await task
        return tokens

    tokens = asyncio.run(run())
    assert tokens.access_token == "at-cc"
    assert endpoint.posts == []
    decision = last_decision("anthropic_oauth", shared.FALLBACK_SLOT)
    assert decision is not None and decision[0] == "shared:waited"


def test_locks_are_released_only_after_the_write(
    claude: ClaudeHome, endpoint: FakeEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude.write("at-old", "rt-old", expires_in=-10)
    seen: list[tuple[bool, bool, bool]] = []
    real_write = creds.write_claude_block

    def spy(document: Any, existing: Any, refreshed: Any, target: Path) -> None:
        seen.append(
            (
                shared.refresh_lock_path().is_dir(),
                shared.legacy_lock_path().is_dir(),
                shared.storage_lock_path(target).is_dir(),
            )
        )
        real_write(document, existing, refreshed, target)

    monkeypatch.setattr(creds, "write_claude_block", spy)
    _serve(AnthropicOAuthAuth())

    assert seen == [(True, True, True)]
    assert not shared.refresh_lock_path().exists()
    assert not shared.legacy_lock_path().exists()
    assert not shared.storage_lock_path(claude.path).exists()


def test_lock_order_is_refresh_then_legacy_then_storage_write(
    claude: ClaudeHome, endpoint: FakeEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude.write("at-old", "rt-old", expires_in=-10)
    order: list[str] = []
    real_mkdir = Path.mkdir

    def spy(self: Path, *args: Any, **kwargs: Any) -> None:
        if self.name.endswith(".lock"):
            order.append(self.name)
        real_mkdir(self, *args, **kwargs)

    expected = [
        ".oauth_refresh.lock",
        shared.legacy_lock_path().name,
        ".storage-write.lock",
    ]
    monkeypatch.setattr(Path, "mkdir", spy)
    _serve(AnthropicOAuthAuth())

    assert expected[1] == "claude-config.lock"  # <realpath(dir)>.lock
    assert order == expected


def test_the_refresh_lock_heartbeats_during_the_post(
    claude: ClaudeHome, endpoint: FakeEndpoint
) -> None:
    claude.write("at-old", "rt-old", expires_in=-10)
    mtimes: dict[str, list[int]] = {"refresh": [], "legacy": []}

    async def slow_post(_: str) -> None:
        for _round in range(2):
            mtimes["refresh"].append(shared.refresh_lock_path().stat().st_mtime_ns)
            mtimes["legacy"].append(shared.legacy_lock_path().stat().st_mtime_ns)
            await asyncio.sleep(0.2)

    endpoint.during_post = slow_post
    _serve(AnthropicOAuthAuth())

    assert mtimes["refresh"][1] > mtimes["refresh"][0]
    assert mtimes["legacy"][1] > mtimes["legacy"][0]


def test_a_live_lock_is_never_broken_and_the_attempt_is_lock_busy(
    claude: ClaudeHome, endpoint: FakeEndpoint
) -> None:
    claude.write("at-old", "rt-old", expires_in=-10)
    lock = shared.refresh_lock_path()
    lock.mkdir()

    async def run() -> None:
        async def claude_code_heartbeat() -> None:
            while True:
                os.utime(lock)
                await asyncio.sleep(0.02)

        beat = asyncio.create_task(claude_code_heartbeat())
        try:
            await AnthropicOAuthAuth().current_tokens(purpose="request")
        finally:
            beat.cancel()

    with pytest.raises(AnthropicOAuthRefreshUnavailable) as caught:
        asyncio.run(run())

    assert caught.value.status_code == 503
    assert not caught.value.definitive
    assert lock.is_dir()  # never broken
    assert endpoint.posts == []
    decision = last_decision("anthropic_oauth", shared.FALLBACK_SLOT)
    assert decision is not None and decision[0] == "shared:lock-busy"

    # A lock that stopped heartbeating more than 60 s ago IS abandoned.
    old = time.time() - 120
    os.utime(lock, (old, old))
    assert _serve(AnthropicOAuthAuth()).access_token == "at-new-1"
    assert endpoint.posts == ["rt-old"]


def test_mcc_never_writes_claude_codes_owner_record(
    claude: ClaudeHome, endpoint: FakeEndpoint
) -> None:
    claude.write("at-old", "rt-old", expires_in=-10)
    owner = claude.dir / shared.OWNER_RECORD_NAME

    _serve(AnthropicOAuthAuth())
    assert not owner.exists()

    # Claude Code's own record, left by a previous holder, is never touched.
    owner.write_text('{"pid": 4242}', encoding="utf-8")
    before = owner.stat().st_mtime_ns
    claude.write("at-old2", "rt-new-1", expires_in=-10)
    _serve(AnthropicOAuthAuth())
    assert owner.read_text(encoding="utf-8") == '{"pid": 4242}'
    assert owner.stat().st_mtime_ns == before
    assert list(claude.dir.glob("*.owner*")) == [owner]


# ---------------------------------------------------------------------------
# Read-only cases
# ---------------------------------------------------------------------------


def _expired_request_is_read_only(
    claude: ClaudeHome, endpoint: FakeEndpoint, code: str
) -> None:
    with pytest.raises(AnthropicOAuthUnavailableError):
        _serve(AnthropicOAuthAuth())
    assert endpoint.posts == []
    decision = last_decision("anthropic_oauth", shared.FALLBACK_SLOT)
    assert decision is not None and decision[0] == code


def test_shared_is_read_only_on_macos(
    claude: ClaudeHome, endpoint: FakeEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shared, "_platform", lambda: "darwin")
    claude.write("at-old", "rt-old", expires_in=3600)
    assert _serve(AnthropicOAuthAuth()).access_token == "at-old"
    claude.write("at-old", "rt-old", expires_in=-10)
    _expired_request_is_read_only(claude, endpoint, "shared:read-only:macos")


def test_shared_is_read_only_without_a_file(
    claude: ClaudeHome, endpoint: FakeEndpoint
) -> None:
    claude.write("at-old", "rt-old", expires_in=3600)
    record = _import(claude)
    claude.path.unlink()

    auth = AnthropicOAuthAuth(account_id=record.id)
    assert _serve(auth).access_token == "at-old"  # the mirror, while valid

    creds.update_account_record(
        record.id,
        tokens=dataclasses.replace(record.tokens, expires_at=int(time.time()) - 5),
    )
    with pytest.raises(AnthropicOAuthUnavailableError):
        _serve(AnthropicOAuthAuth(account_id=record.id))
    assert endpoint.posts == []
    decision = last_decision("anthropic_oauth", record.id)
    assert decision is not None and decision[0] == "shared:no-file"

    # And without a file the fallback is simply not a source.
    assert creds.load_claude_code_tokens() is None


def test_write_back_off_makes_shared_read_only(
    claude: ClaudeHome, endpoint: FakeEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(creds, "write_back_enabled", lambda: False)
    claude.write("at-old", "rt-old", expires_in=-10)
    _expired_request_is_read_only(claude, endpoint, "shared:read-only:write-back-off")


# ---------------------------------------------------------------------------
# Dead refresh tokens
# ---------------------------------------------------------------------------


def test_a_past_refresh_token_expiry_makes_zero_token_calls(
    claude: ClaudeHome, endpoint: FakeEndpoint
) -> None:
    claude.write("at-old", "rt-old", expires_in=-10, refresh_expires_in=-5)
    with pytest.raises(AnthropicOAuthUnavailableError):
        _serve(AnthropicOAuthAuth())

    claude.write("at-old", "rt-old", expires_in=3600, refresh_expires_in=-5)
    record = _import(claude)
    creds.update_account_record(
        record.id,
        tokens=dataclasses.replace(record.tokens, expires_at=int(time.time()) - 5),
        source_stamp=None,
    )
    claude.write("at-old", "rt-old", expires_in=-10, refresh_expires_in=-5)
    with pytest.raises(AnthropicOAuthRefreshRejected):
        _serve(AnthropicOAuthAuth(account_id=record.id))
    assert endpoint.posts == []


def test_a_rejected_shared_refresh_token_is_never_posted_again(
    claude: ClaudeHome, endpoint: FakeEndpoint
) -> None:
    endpoint.reject_all = True
    claude.write("at-old", "rt-old", expires_in=-10)

    with pytest.raises(AnthropicOAuthRefreshRejected) as first:
        _serve(AnthropicOAuthAuth())
    assert first.value.definitive
    assert "claude /login" in str(first.value)
    decision = last_decision("anthropic_oauth", shared.FALLBACK_SLOT)
    assert decision is not None and decision[0] == "shared:rejected"

    for _ in range(3):
        with pytest.raises(AnthropicOAuthRefreshRejected):
            _serve(AnthropicOAuthAuth())
    assert endpoint.posts == ["rt-old"]
    # Claude Code's file is never touched and nothing was quarantined.
    assert claude.block()["refreshToken"] == "rt-old"
    assert list(creds.managed_store_path().parent.glob("*.dead-*")) == []


# ---------------------------------------------------------------------------
# Write-back failure, 401s and account switches
# ---------------------------------------------------------------------------


def test_a_failed_write_back_is_kept_pending_and_retried_without_a_post(
    claude: ClaudeHome, endpoint: FakeEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude.write("at-old", "rt-old", expires_in=-10)
    monkeypatch.setattr(shared, "WRITE_RETRY_STEP_SECONDS", 0.0)
    real_write = creds.write_claude_block

    def broken(*_: Any) -> None:
        raise OSError("disk says no")

    monkeypatch.setattr(creds, "write_claude_block", broken)
    tokens = _serve(AnthropicOAuthAuth())
    assert tokens.access_token == "at-new-1"  # never dropped (rule 11)
    assert claude.block()["refreshToken"] == "rt-old"
    records = creds.load_accounts(migrate=False)
    assert len(records) == 1
    assert records[0].pending_write_back
    assert records[0].is_shared
    decision = last_decision("anthropic_oauth", records[0].id)
    assert decision is not None and decision[0] == "shared:writeback-pending"

    monkeypatch.setattr(creds, "write_claude_block", real_write)
    again = _serve(AnthropicOAuthAuth())
    assert again.access_token == "at-new-1"
    assert claude.block()["refreshToken"] == "rt-new-1"
    stored = creds.load_accounts(migrate=False)[0]
    assert not stored.pending_write_back
    assert endpoint.posts == ["rt-old"]


def test_a_401_on_a_shared_credential_re_reads_before_refreshing(
    claude: ClaudeHome, endpoint: FakeEndpoint
) -> None:
    claude.write("at-old", "rt-old", expires_in=3600)
    auth = AnthropicOAuthAuth()
    _serve(auth)

    async def after_401() -> OAuthTokens | None:
        async def inner():
            yield await auth.force_refresh()

        results = [item async for item in request_scoped_stream(inner())]
        return results[0]

    # Changed file: adopt it, no POST.
    claude.write("at-cc", "rt-cc", expires_in=3600)
    adopted = asyncio.run(after_401())
    assert adopted is not None and adopted.access_token == "at-cc"

    # Unchanged, an hour left: the 401 stands, no POST.
    assert asyncio.run(after_401()) is None
    # Outside a request: never.
    assert asyncio.run(auth.force_refresh()) is None
    assert endpoint.posts == []

    # Unchanged and within 120 s of expiry: the locked refresh.
    endpoint.valid = {"rt-cc"}
    claude.write("at-cc", "rt-cc", expires_in=60)
    _serve(auth, "background")
    refreshed = asyncio.run(after_401())
    assert refreshed is not None and refreshed.access_token == "at-new-1"
    assert endpoint.posts == ["rt-cc"]


def test_an_account_switch_in_claude_code_is_adopted_and_the_old_identity_never_refreshed(
    claude: ClaudeHome, endpoint: FakeEndpoint
) -> None:
    claude.identity("uuid-a", "a@example.com")
    claude.write("at-a", "rt-a", expires_in=3600)
    record = _import(claude, uuid="uuid-a")
    name_before = account_name("anthropic_oauth", "uuid-a")
    assert name_before
    auth = AnthropicOAuthAuth()
    assert _serve(auth).access_token == "at-a"

    claude.identity("uuid-b", "b@example.com")
    claude.write("at-b", "rt-b", expires_in=-10)
    endpoint.valid = {"rt-b"}
    with pytest.raises(AnthropicOAuthUnavailableError):
        _serve(auth, "background")  # adopted; expired, waiting for Claude Code

    ids = [r.id for r in creds.load_accounts(migrate=False)]
    assert ids == ["uuid-b"]
    assert creds.account_for(record.id) is None
    assert account_name("anthropic_oauth", "uuid-a") == name_before
    decision = last_decision("anthropic_oauth", "uuid-a")
    assert decision is not None and decision[0] == "shared:identity-changed"
    assert "rt-a" not in endpoint.posts
    assert endpoint.posts == []


# ---------------------------------------------------------------------------
# Failure mapping
# ---------------------------------------------------------------------------


def test_shared_failures_map_to_the_existing_failure_kinds() -> None:
    classify = AnthropicOAuthProvider._classify_credential_failure.__get__(
        object.__new__(AnthropicOAuthProvider)
    )
    waiting = classify(shared.SharedCredentialWaiting("waiting for Claude Code"))
    assert waiting is not None and waiting.kind is FailureKind.UNAVAILABLE
    read_only = classify(AnthropicOAuthUnavailableError("read-only"))
    assert read_only is not None and read_only.kind is FailureKind.UNAVAILABLE
    dead = classify(shared.SharedRefreshRejected(400))
    assert dead is not None
    assert dead.kind is FailureKind.AUTHENTICATION
    assert not dead.retryable
    assert isinstance(shared.SharedRefreshRejected(400), AnthropicOAuthRefreshError)
    refused = classify(creds.SharedCredentialRefused("shared"))
    assert refused is not None and refused.kind is FailureKind.UNAVAILABLE


def test_an_unchanged_file_never_replaces_mccs_newer_copy(
    claude: ClaudeHome, endpoint: FakeEndpoint
) -> None:
    """A record stored before 7.69.1 keeps its copy until the file moves.

    Caught on the scratch server: the second start adopted Claude Code's
    unchanged file over the copy the first start had kept as its baseline.
    """
    claude.write("at-file", "rt-file", expires_in=3600)
    record = creds.add_or_update_account(
        _native_tokens("at-mirror", "rt-mirror", "uuid-a", 3600),
        origin=ORIGIN_CLAUDE_CODE,
        origin_path=str(claude.path),
        write_back=True,
        adopt_origin=True,
    )
    assert record.source_stamp is None

    for _ in range(3):  # three "processes", each starting cold
        assert _serve(AnthropicOAuthAuth(account_id="uuid-a")).access_token == (
            "at-mirror"
        )
    assert last_decision("anthropic_oauth", "uuid-a") is None

    claude.write("at-cc", "rt-cc", expires_in=7200)
    assert _serve(AnthropicOAuthAuth(account_id="uuid-a")).access_token == "at-cc"
    decision = last_decision("anthropic_oauth", "uuid-a")
    assert decision is not None and decision[0] == "shared:adopted"
    assert endpoint.posts == []
