"""7.69.1 rule 7: only a real request may refresh a SHARED Claude credential.

F5 of the spec: the background trigger is ``auth.headers()`` itself. Model
listing, probes, startup validation and discovery all reach
``current_tokens()``, so the 401 path is not the only way in. Each test here
drives one of those callers through its real code, against a fixture Claude
Code config directory in ``tmp_path`` (``CLAUDE_CONFIG_DIR``) and a fake
``_post_refresh`` that fails the test the moment it is called. Write-back is
on and the platform is pinned to Windows, so the purpose is the only thing
that can stop a refresh.

The last test is the control: a NATIVE credential MCC signed in itself still
refreshes in the background inside its 120 s leeway, exactly as before.
"""

import json
import os
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.application.errors import ApplicationUnavailableError
from my_claude_code.application.execution import ProviderExecutor
from my_claude_code.application.routing import ModelRouter
from my_claude_code.application.vision_describe import VisionDescribeAdapter
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.credential_refresh_scope import (
    background_scope,
    current_purpose,
)
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.providers.anthropic_oauth import credentials as creds
from my_claude_code.providers.anthropic_oauth import shared
from my_claude_code.providers.anthropic_oauth.auth import AnthropicOAuthAuth
from my_claude_code.providers.anthropic_oauth.credentials import (
    AccountRecord,
    AnthropicOAuthUnavailableError,
    OAuthTokens,
)
from my_claude_code.providers.anthropic_oauth.provider import AnthropicOAuthProvider
from my_claude_code.providers.base import BaseProvider, ProviderConfig
from my_claude_code.providers.oauth_account_store import (
    ORIGIN_CLAUDE_CODE,
    ORIGIN_MCC,
)
from my_claude_code.providers.oauth_ownership import last_decision
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.runtime.discovery import ProviderModelDiscovery
from my_claude_code.providers.runtime.model_cache import ProviderModelCache
from my_claude_code.providers.runtime.validation import ConfiguredModelValidator
from my_claude_code.runtime.discovery_timer import ProviderDiscoveryTimer
from tests.api.support import create_test_app

PROVIDER_ID = "anthropic_oauth"
MODEL = "claude-sonnet-4-6"
MODEL_REF = f"{PROVIDER_ID}/{MODEL}"

# Fixture credentials, shaped like Claude Code's own and worth nothing.
EXPIRED_ACCESS = "sk-ant-oat01-fixture-expired-access"
EXPIRED_REFRESH = "sk-ant-ort01-fixture-expired-family"
LEEWAY_ACCESS = "sk-ant-oat01-fixture-leeway-access"
LEEWAY_REFRESH = "sk-ant-ort01-fixture-leeway-family"
NATIVE_ACCESS = "sk-ant-oat01-fixture-native-access"
NATIVE_REFRESH = "sk-ant-ort01-fixture-native-family"

#: Seconds left on the token: expired ten minutes ago, or one minute to go --
#: inside the 120 s leeway that refreshes a NATIVE credential in the background.
EXPIRED = -600.0
INSIDE_LEEWAY = 60.0

# 1x1 PNG, base64: the picture a describe call is made for.
PIXEL = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
    "hKmMIQAAAABJRU5ErkJggg=="
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class ClaudeCodeDir:
    """A fixture ``CLAUDE_CONFIG_DIR``: Claude Code's ``.credentials.json``."""

    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.path = directory / ".credentials.json"
        self._tick = time.time_ns()

    def write(self, access: str, refresh: str, *, expires_in: float) -> None:
        now = time.time()
        document = {
            "claudeAiOauth": {
                "accessToken": access,
                "refreshToken": refresh,
                # Milliseconds, the way Claude Code writes them.
                "expiresAt": int((now + expires_in) * 1000),
                "refreshTokenExpiresAt": int((now + 30 * 86400) * 1000),
                "scopes": [
                    "user:inference",
                    "user:profile",
                    "user:sessions:claude_code",
                ],
                "subscriptionType": "max",
                "rateLimitTier": "default_claude_max_20x",
            },
            "mcpOAuth": {"fixture-server": {"clientId": "fixture"}},
        }
        self.path.write_text(json.dumps(document), encoding="utf-8")
        # A strictly increasing mtime, so the (mtime_ns, size) stamp always
        # moves even when two writes land inside one clock tick.
        self._tick += 1_000_000_000
        os.utime(self.path, ns=(self._tick, self._tick))

    def snapshot(self) -> tuple[bytes, int]:
        """The file's bytes and mtime: what "untouched" means."""
        return self.path.read_bytes(), self.path.stat().st_mtime_ns

    def block(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))["claudeAiOauth"]

    def leftovers(self) -> list[str]:
        """Locks or backups a refresh would have left beside the file."""
        names = [
            entry.name for entry in self.dir.iterdir() if entry.name != self.path.name
        ]
        legacy = self.dir.with_name(self.dir.name + ".lock")
        if legacy.exists():
            names.append(legacy.name)
        return sorted(names)


class TokenEndpoint:
    """The fake ``_post_refresh``. Unarmed, any call fails the test.

    Every call is recorded before anything else happens: a ``pytest.fail``
    raised inside an ``asyncio.gather(..., return_exceptions=True)`` -- which
    discovery, validation and the describe adapter all use -- would otherwise
    be swallowed, so every test also asserts ``posts == []`` at the end.
    """

    def __init__(self) -> None:
        self.posts: list[str] = []
        self.answers = False

    async def __call__(self, refresh_token: str) -> httpx.Response:
        self.posts.append(refresh_token)
        if not self.answers:
            pytest.fail(
                "A background caller POSTed a refresh token to the OAuth token "
                "endpoint. Only a real request may refresh a shared credential "
                "(spec rule 7)."
            )
        issued = len(self.posts)
        return httpx.Response(
            200,
            json={
                "access_token": f"sk-ant-oat01-fixture-rotated-{issued}",
                "refresh_token": f"sk-ant-ort01-fixture-rotated-{issued}",
                "expires_in": 28800,
            },
        )


class Upstream:
    """``api.anthropic.com``, mocked: records every request that reached it."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "data": [{"id": MODEL, "type": "model", "display_name": MODEL}],
                    "has_more": False,
                },
            )
        return httpx.Response(
            200,
            content=_sse_body(),
            headers={"content-type": "text/event-stream"},
        )

    def bearers(self) -> list[str]:
        return [request.headers.get("authorization", "") for request in self.requests]


def _sse_body() -> bytes:
    frames: list[tuple[str, dict[str, Any]]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_fixture",
                    "type": "message",
                    "role": "assistant",
                    "model": MODEL,
                    "content": [],
                    "stop_reason": None,
                    "usage": {"input_tokens": 1, "output_tokens": 0},
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "pong"},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 1},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    text = "".join(
        f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in frames
    )
    return text.encode("utf-8")


@pytest.fixture
def claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ClaudeCodeDir:
    directory = tmp_path / "claude-code-config"
    directory.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(directory))
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
    # Write-back is possible and this is not macOS: if a refresh is refused,
    # the purpose refused it, not a read-only rule.
    monkeypatch.setattr(creds, "write_back_enabled", lambda: True)
    monkeypatch.setattr(shared, "_platform", lambda: "win32")
    return ClaudeCodeDir(directory)


@pytest.fixture
def token_endpoint(monkeypatch: pytest.MonkeyPatch) -> TokenEndpoint:
    fake = TokenEndpoint()
    monkeypatch.setattr(creds, "_post_refresh", fake)
    return fake


@pytest.fixture
def upstream() -> Upstream:
    return Upstream()


def _config() -> ProviderConfig:
    return ProviderConfig(
        # Empty: the credential is discovered from disk, the maintained path.
        api_key="",
        base_url="https://api.anthropic.com/v1",
        rate_limit=100,
        rate_window=60,
        max_concurrency=5,
        retry_attempts=1,
        early_retry_attempts=1,
        commit_holdback_seconds=0,
    )


async def _provider(
    upstream: Upstream, *, account_id: str = ""
) -> AnthropicOAuthProvider:
    """The real OAuth provider, its two HTTP clients pointed at ``upstream``.

    ``require_claude_code_cli=False`` because the describe side call carries
    no ``cc_entrypoint`` marker: with the gate on it is refused before any
    credential is read, so the gate off is the configuration in which the
    describe path reaches ``auth.headers()`` at all.
    """
    provider = AnthropicOAuthProvider(
        _config(),
        rate_limiter=ProviderRateLimiter(
            rate_limit=100, rate_window=60, max_concurrency=5, max_retries=0
        ),
        require_claude_code_cli=False,
        provider_id=PROVIDER_ID,
        account_id=account_id,
    )
    await provider._client.aclose()
    await provider._messages._client.aclose()
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    provider._messages._client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)
    )
    return provider


def _only(provider: AnthropicOAuthProvider) -> Callable[[str], BaseProvider]:
    """A resolver that builds nothing: any other provider id is a failure."""

    def resolve(provider_id: str) -> BaseProvider:
        if provider_id != PROVIDER_ID:
            raise LookupError(f"{provider_id} is not part of this test")
        return provider

    return resolve


def _settings() -> Settings:
    return Settings.model_construct(
        model=MODEL_REF,
        model_fable=None,
        model_opus=None,
        model_sonnet=None,
        model_haiku=None,
        log_api_error_tracebacks=False,
    )


def _request() -> MessagesRequest:
    return MessagesRequest(
        model=MODEL,
        max_tokens=32,
        messages=[Message(role="user", content="ping")],
        stream=True,
    )


def _import_shared(claude: ClaudeCodeDir, uuid: str = "uuid-shared") -> AccountRecord:
    """What the dashboard's Import does: a SHARED record of Claude Code's file."""
    tokens = creds.load_claude_code_tokens()
    assert tokens is not None
    record = creds.add_or_update_account(
        replace(tokens, account_uuid=uuid, source="mcc"),
        origin=ORIGIN_CLAUDE_CODE,
        origin_path=str(claude.path),
        write_back=True,
        adopt_origin=True,
    )
    assert record.is_shared
    return record


def _native_tokens(access: str, refresh: str, *, expires_in: float) -> OAuthTokens:
    return OAuthTokens(
        access_token=access,
        refresh_token=refresh,
        expires_at=int(time.time() + expires_in),
        scopes=("user:inference", "user:profile"),
        subscription_type="max",
        account_uuid="uuid-native",
        source="mcc",
    )


def _decision(slot: str) -> str:
    decision = last_decision(PROVIDER_ID, slot)
    return decision[0] if decision is not None else ""


async def _failure_of(stream: AsyncIterator[str]) -> BaseException | None:
    """Drain ``stream``; return what it raised, or ``None``."""
    try:
        async for _chunk in stream:
            pass
    except Exception as error:
        return error
    return None


# ---------------------------------------------------------------------------
# Background callers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_discovery_timer_never_refreshes_a_shared_credential(
    claude: ClaudeCodeDir, token_endpoint: TokenEndpoint, upstream: Upstream
) -> None:
    # The automatic fallback: Claude Code's file, expired, and no MCC store.
    claude.write(EXPIRED_ACCESS, EXPIRED_REFRESH, expires_in=EXPIRED)
    assert not creds.managed_store_path().exists()
    before = claude.snapshot()
    provider = await _provider(upstream)
    discovery = ProviderModelDiscovery(
        _settings(),
        _only(provider),
        ProviderModelCache((PROVIDER_ID,)),
        (PROVIDER_ID,),
        quiet=True,
    )
    # The real timer, its sweep the real discovery pass
    # (``providers/runtime/discovery.py``). Two ticks: the second is where a
    # "retry the refresh next time" bug would show.
    timer = ProviderDiscoveryTimer(
        lambda skipped: discovery.refresh_model_list_cache(skip_provider_ids=skipped),
        lambda: 3600.0,
    )

    for _ in range(2):
        assert current_purpose() == "background"
        result = await timer.tick()
        assert PROVIDER_ID in result.failed_provider_ids
        failure = result.failure_for(PROVIDER_ID)
        assert failure is not None
        assert "waiting for Claude Code" in failure.message

    assert token_endpoint.posts == []
    assert upstream.requests == [], "an expired token must not be sent either"
    assert claude.snapshot() == before
    assert claude.leftovers() == []
    assert not creds.managed_store_path().exists()
    assert _decision(shared.FALLBACK_SLOT) == "shared:expired-waiting"
    await provider.cleanup()


@pytest.mark.asyncio
async def test_startup_validation_never_refreshes_a_shared_credential(
    claude: ClaudeCodeDir, token_endpoint: TokenEndpoint, upstream: Upstream
) -> None:
    # An imported (SHARED) record this time, served by its own provider.
    claude.write(EXPIRED_ACCESS, EXPIRED_REFRESH, expires_in=EXPIRED)
    record = _import_shared(claude)
    before = claude.snapshot()
    provider = await _provider(upstream, account_id=record.id)
    validator = ConfiguredModelValidator(
        _settings(), _only(provider), ProviderModelCache((PROVIDER_ID,))
    )

    with pytest.raises(ApplicationUnavailableError) as caught:
        await validator.validate_configured_models()

    assert f"provider={PROVIDER_ID}" in caught.value.message
    assert "waiting for Claude Code" in caught.value.message
    assert token_endpoint.posts == []
    assert upstream.requests == []
    assert claude.snapshot() == before
    assert claude.leftovers() == []
    stored = creds.account_for(record.id)
    assert stored is not None and stored.is_shared
    assert stored.tokens.access_token == EXPIRED_ACCESS
    assert stored.tokens.refresh_token == EXPIRED_REFRESH
    assert not stored.pending_write_back
    assert _decision(record.id) == "shared:expired-waiting"
    await provider.cleanup()


@pytest.mark.asyncio
async def test_a_probe_never_refreshes_a_shared_credential(
    claude: ClaudeCodeDir, token_endpoint: TokenEndpoint, upstream: Upstream
) -> None:
    claude.write(EXPIRED_ACCESS, EXPIRED_REFRESH, expires_in=EXPIRED)
    before = claude.snapshot()
    provider = await _provider(upstream)

    # Expired: the probe fails, and nothing is posted or sent.
    with pytest.raises(AnthropicOAuthUnavailableError):
        await provider._messages.probe(MODEL)

    assert token_endpoint.posts == []
    assert upstream.requests == []
    assert claude.snapshot() == before
    assert _decision(shared.FALLBACK_SLOT) == "shared:expired-waiting"

    # Inside the leeway, which would start a background refresh of a NATIVE
    # credential: the probe goes out on the token Claude Code holds, and no
    # refresh task is started.
    claude.write(LEEWAY_ACCESS, LEEWAY_REFRESH, expires_in=INSIDE_LEEWAY)
    before = claude.snapshot()

    await provider._messages.probe(MODEL)

    assert upstream.bearers() == [f"Bearer {LEEWAY_ACCESS}"]
    assert provider._oauth._background is None
    assert provider._oauth.mode == "shared"
    assert token_endpoint.posts == []
    assert claude.snapshot() == before
    assert claude.leftovers() == []
    await provider.cleanup()


@pytest.mark.asyncio
async def test_the_describe_side_call_never_refreshes_a_shared_credential(
    claude: ClaudeCodeDir, token_endpoint: TokenEndpoint, upstream: Upstream
) -> None:
    claude.write(EXPIRED_ACCESS, EXPIRED_REFRESH, expires_in=EXPIRED)
    before = claude.snapshot()
    provider = await _provider(upstream)

    # 1. The provider's own ``stream_response`` -- which wraps its stream in
    #    ``request_scoped_stream`` -- nested inside the describe call's
    #    background scope cannot elevate itself to a request.
    with background_scope():
        failure = await _failure_of(provider.stream_response(_request()))

    assert isinstance(failure, ExecutionFailure), failure
    assert failure.kind is FailureKind.UNAVAILABLE
    assert token_endpoint.posts == []
    assert upstream.requests == []
    assert claude.snapshot() == before
    assert _decision(shared.FALLBACK_SLOT) == "shared:expired-waiting"

    # 2. The real describe adapter, whose vision chain is this provider.
    settings = Settings()
    settings.model = "nvidia_nim/blind"
    settings.model_sonnet = "nvidia_nim/blind"
    settings.model_fable = None
    settings.model_opus = None
    settings.model_haiku = None
    settings.model_fallbacks = None
    settings.model_sonnet_fallbacks = None
    settings.model_vision = MODEL_REF
    settings.model_vision_fallbacks = None
    settings.vision_adapter_mode = "describe"
    adapter = VisionDescribeAdapter(
        router=ModelRouter(
            settings,
            vision_lookup=lambda _provider, model: {"blind": False, MODEL: True}.get(
                model
            ),
        ),
        executor=ProviderExecutor(_only(provider)),
        store=None,
    )
    image_request = MessagesRequest.model_validate(
        {
            "model": MODEL,
            "max_tokens": 64,
            "stream": False,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": PIXEL,
                            },
                        },
                        {"type": "text", "text": "what is this?"},
                    ],
                }
            ],
        }
    )

    described = await adapter.apply(image_request, request_id="req_describe_shared")

    assert described.failed and not described.applied
    assert MODEL_REF in described.unreachable_refs
    assert token_endpoint.posts == []
    assert upstream.requests == []
    assert claude.snapshot() == before
    assert claude.leftovers() == []

    # 3. The control: the very same ``stream_response`` for a real client
    #    request does refresh -- once, under Claude Code's lock, written back.
    #    So what stopped 1 and 2 was the background scope, nothing else.
    token_endpoint.answers = True

    assert await _failure_of(provider.stream_response(_request())) is None
    assert token_endpoint.posts == [EXPIRED_REFRESH]
    assert upstream.bearers() == ["Bearer sk-ant-oat01-fixture-rotated-1"]
    assert claude.block()["refreshToken"] == "sk-ant-ort01-fixture-rotated-1"
    assert _decision(shared.FALLBACK_SLOT) == "shared:refreshed+wrote-back"
    await provider.cleanup()


def test_the_sources_route_never_refreshes(
    claude: ClaudeCodeDir, token_endpoint: TokenEndpoint
) -> None:
    # Every shape the card shows: the expired fallback, an expired imported
    # record of the same file, and a NATIVE account inside its leeway.
    claude.write(EXPIRED_ACCESS, EXPIRED_REFRESH, expires_in=EXPIRED)
    shared_record = _import_shared(claude)
    native_record = creds.add_or_update_account(
        _native_tokens(NATIVE_ACCESS, NATIVE_REFRESH, expires_in=INSIDE_LEEWAY),
        origin=ORIGIN_MCC,
        adopt_origin=True,
    )
    assert not native_record.is_shared
    before = claude.snapshot()
    client = TestClient(create_test_app(), client=("127.0.0.1", 50000))

    for _ in range(2):
        response = client.get("/admin/api/anthropic-oauth/sources")
        assert response.status_code == 200

    data = response.json()
    assert data["claude_code"]["available"] is True
    assert data["claude_code"]["mode"] == "shared"
    modes = {row["account_id"]: row["mode"] for row in data["accounts"]}
    assert modes == {shared_record.id: "shared", native_record.id: "native"}
    assert token_endpoint.posts == []
    assert claude.snapshot() == before
    assert claude.leftovers() == []
    shared_after = creds.account_for(shared_record.id)
    native_after = creds.account_for(native_record.id)
    assert shared_after is not None and native_after is not None
    assert shared_after.tokens.refresh_token == EXPIRED_REFRESH
    assert native_after.tokens.access_token == NATIVE_ACCESS
    assert native_after.tokens.refresh_token == NATIVE_REFRESH


# ---------------------------------------------------------------------------
# The control: NATIVE is unchanged (rule 13)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_native_credential_still_refreshes_in_the_background(
    claude: ClaudeCodeDir, token_endpoint: TokenEndpoint
) -> None:
    token_endpoint.answers = True
    # Claude Code's own credential sits beside it and is not MCC's to touch:
    # a NATIVE refresh never writes back.
    claude.write(LEEWAY_ACCESS, LEEWAY_REFRESH, expires_in=3600)
    before = claude.snapshot()
    record = creds.add_or_update_account(
        _native_tokens(NATIVE_ACCESS, NATIVE_REFRESH, expires_in=INSIDE_LEEWAY),
        origin=ORIGIN_MCC,
        adopt_origin=True,
    )
    assert not record.is_shared
    auth = AnthropicOAuthAuth()

    # No request scope: this is exactly what discovery or a probe would do.
    assert current_purpose() == "background"
    served = await auth.current_tokens()

    # The call in hand goes out on the token it has, which is still valid...
    assert served.access_token == NATIVE_ACCESS
    assert auth.mode == "native"
    # ...and the refresh runs out of band.
    task = auth._background
    assert task is not None
    await task

    assert token_endpoint.posts == [NATIVE_REFRESH]
    stored = creds.account_for(record.id)
    assert stored is not None
    assert stored.tokens.access_token == "sk-ant-oat01-fixture-rotated-1"
    assert stored.tokens.refresh_token == "sk-ant-ort01-fixture-rotated-1"
    assert not stored.is_shared
    assert (await auth.current_tokens()).access_token == (
        "sk-ant-oat01-fixture-rotated-1"
    )
    assert token_endpoint.posts == [NATIVE_REFRESH]
    assert claude.snapshot() == before
    assert _decision(record.id) == "native:refreshed"
