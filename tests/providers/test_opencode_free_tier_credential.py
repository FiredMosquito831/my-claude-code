"""Free Zen models go out on OpenCode's shared credential, not the operator's.

The defect, measured 2026-09-20 from one machine behind one exit IP inside
seventy seconds (``specs/INVESTIGATION-ZEN-429-FREEUSAGELIMIT.md``):

    00:43:49Z  MCC          key sk-8...Kofx   429 FreeUsageLimitError
    00:44:16Z  opencode CLI `public`          200, 29 807 tokens, cost 0
    00:44:56Z  MCC          key sk-8...Kofx   429 FreeUsageLimitError

The limiter is keyed on the credential. Not the address, not the session id,
not the header set -- MCC's five identity headers were already byte-identical
to the client's, and the 200 in the middle of that table came from the same
address with a different key. ``opencode-ai@1.18.31`` contains the rule twice:
with no credential configured it sends the literal key ``public`` and deletes
every model whose ``cost.input`` is non-zero.

What these tests hold:

* a free model, on the shipped default, is fetched as ``Bearer public`` and
  keeps the five identity headers and the translated tool catalogue;
* a paid model on the same host is byte-identical to 7.33.0 -- the operator's
  key, and only the operator's key;
* ``OPENCODE_FREE_TIER_CREDENTIAL=key`` restores 7.33.0 for free models too;
* with no ``OPENCODE_API_KEY`` at all a free model still works and a paid one
  is refused with the sentence 7.33.0 refused it with;
* a 429 on the shared slot moves the shared slot's health and leaves the
  operator's pool exactly as it was;
* the housekeeping that named no model -- the hourly discovery sweep and the
  Test button -- stops spending the operator's key, and falls back to it if
  the anonymous listing fails;
* the request log, Analytics and the exports say ``public``;
* ``opencode_go`` is untouched, and so is every other provider.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from my_claude_code.application.errors import (
    ApplicationUnavailableError,
    ModelRateLimited,
)
from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.config.settings import Settings, get_settings
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.credential_attribution import (
    current_credential,
    install_attribution,
)
from my_claude_code.providers.base import BaseProvider, ProviderConfig
from my_claude_code.providers.openai_chat.opencode_identity import (
    OPENCODE_CLIENT_HEADER,
    OPENCODE_PROJECT_HEADER,
    OPENCODE_REQUEST_HEADER,
    OPENCODE_SESSION_HEADER,
    USER_AGENT_HEADER,
)
from my_claude_code.providers.openai_chat.provider import OpenAIChatProvider
from my_claude_code.providers.runtime.factory import create_provider
from my_claude_code.providers.runtime.opencode_credentials import (
    OPENCODE_PUBLIC_CREDENTIAL,
    OPENCODE_PUBLIC_KEY_LABEL,
    OpenCodeCredentialSplitProvider,
    probe_credential,
    probe_fallback_credential,
)
from my_claude_code.providers.runtime.rotating import RotatingProvider

FREE = "muse-spark-1.3-contributor-free"
PAID = "claude-sonnet-4-5"
OPERATOR_KEY = "sk-operator-0000000000000000"

SSE = (
    b'data: {"id":"c","object":"chat.completion.chunk","created":0,'
    b'"model":"m","choices":[{"index":0,"delta":{"role":"assistant",'
    b'"content":"ok"},"finish_reason":null}]}\n\n'
    b'data: {"id":"c","object":"chat.completion.chunk","created":0,'
    b'"model":"m","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    b"data: [DONE]\n\n"
)


# -- harness -------------------------------------------------------------------


class _Wire:
    """Every outbound request the provider tree made, in order."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200

    def authorizations(self) -> list[str]:
        return [request.headers.get("authorization", "") for request in self.requests]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(
                self.status,
                json={
                    "type": "error",
                    "error": {
                        "type": "FreeUsageLimitError",
                        "message": "Rate limit exceeded",
                    },
                },
                request=request,
            )
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={"data": [{"id": FREE}, {"id": PAID}]},
                request=request,
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=SSE,
            request=request,
        )


PAID_LISTING = frozenset({ProviderModelInfo(model_id=PAID)})


class _ListingSide(BaseProvider):
    """One side of the split that answers a listing, and records being asked.

    A real ``BaseProvider`` rather than a duck: the fallback under test is the
    only thing in the split that treats its two sides differently, so the
    difference between them must be the answer, not the type.
    """

    def __init__(
        self,
        name: str,
        answer: frozenset[ProviderModelInfo] | Exception,
        calls: list[str],
    ) -> None:
        super().__init__(ProviderConfig(api_key=name, base_url="https://example/v1"))
        self._name = name
        self._answer = answer
        self._calls = calls

    async def list_model_infos(self) -> frozenset[ProviderModelInfo]:
        self._calls.append(self._name)
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer

    async def list_model_ids(self) -> frozenset[str]:
        infos = await self.list_model_infos()
        return frozenset(info.model_id for info in infos)

    def preflight_stream(self, request: MessagesRequest, **_kwargs: Any) -> None:
        raise AssertionError("a listing side is never streamed on")

    def stream_response(
        self, request: MessagesRequest, input_tokens: int = 0, **_kwargs: Any
    ) -> Any:
        raise AssertionError("a listing side is never streamed on")

    async def cleanup(self) -> None:
        return None


def _leaves(provider: Any) -> Iterator[OpenAIChatProvider]:
    """Every Chat Completions client under one provider, however it is wrapped."""

    if isinstance(provider, OpenAIChatProvider):
        yield provider
        return
    for attr in ("paid", "public"):
        child = getattr(provider, attr, None)
        if child is not None:
            yield from _leaves(child)
    for child in getattr(provider, "_providers", ()) or ():
        yield from _leaves(child)


def _intercept(provider: Any, wire: _Wire) -> None:
    """Swap the transport under each leaf, leaving its auth exactly as built.

    Only ``_transport`` moves: the ``Authorization`` header these tests read is
    still the one the OpenAI client derives from the credential the factory
    handed that leaf, which is the whole question.
    """

    for leaf in _leaves(provider):
        leaf._client._client._transport = httpx.MockTransport(wire.handler)


def _settings(key: str = OPERATOR_KEY, credential: str = "public") -> Settings:
    """Settings built by *alias*, which is the only form that populates.

    Spelled through a dict because every field on ``Settings`` declares a
    ``validation_alias`` and no ``populate_by_name``: passing the Python field
    name is silently dropped as an extra, and the value that then answers is
    whatever the machine's own ``.env`` holds -- which for this particular
    setting would read the developer's real OpenCode key into a test about
    not spending it.
    """

    by_alias: dict[str, Any] = {
        "OPENCODE_API_KEY": key,
        "OPENCODE_FREE_TIER_CREDENTIAL": credential,
    }
    return Settings(**by_alias)


@pytest.fixture(autouse=True)
def _fresh_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """No ambient OpenCode configuration leaks into a credential decision.

    Including the machine's own config directory: these tests decide which
    credential goes on a wire, and the one credential that must never reach
    one of them is the developer's.
    """

    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    (tmp_path / ".env").write_text("", encoding="utf-8")
    for name in (
        "OPENCODE_API_KEY",
        "OPENCODE_FREE_TIER_CREDENTIAL",
        "OPENCODE_FREE_TIER_MODELS",
        "OPENCODE_CLIENT_IDENTITY",
    ):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _request(model: str) -> MessagesRequest:
    return MessagesRequest(
        model=model,
        max_tokens=16,
        messages=[Message(role="user", content="hi")],
        stream=True,
    )


async def _drain(provider: Any, model: str) -> None:
    stream = provider.stream_response(_request(model), request_id="r")
    async for _chunk in stream:
        pass


def _build(
    monkeypatch: pytest.MonkeyPatch,
    wire: _Wire,
    *,
    key: str = OPERATOR_KEY,
    credential: str = "public",
) -> Any:
    settings = _settings(key, credential)
    monkeypatch.setattr("my_claude_code.config.settings.get_settings", lambda: settings)
    monkeypatch.setattr(
        "my_claude_code.providers.openai_chat.opencode_catalogue"
        ".configured_opencode_free_tier_models",
        lambda: (),
    )
    monkeypatch.setattr(
        "my_claude_code.providers.runtime.opencode_credentials"
        ".configured_opencode_free_tier_credential",
        lambda: credential,
    )
    provider = create_provider("opencode", settings)
    _intercept(provider, wire)
    return provider


# -- the credential on the wire ------------------------------------------------


@pytest.mark.anyio
async def test_a_free_model_goes_out_on_the_shared_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = _Wire()
    provider = _build(monkeypatch, wire)
    await _drain(provider, FREE)

    assert wire.authorizations() == [f"Bearer {OPENCODE_PUBLIC_CREDENTIAL}"]
    assert OPERATOR_KEY not in "".join(wire.authorizations())


@pytest.mark.anyio
async def test_the_five_identity_headers_are_the_same_on_both_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """6.69.0's headers are about who MCC claims to be, not whose key it is."""

    wire = _Wire()
    provider = _build(monkeypatch, wire)
    await _drain(provider, FREE)
    await _drain(provider, PAID)

    free_headers, paid_headers = (request.headers for request in wire.requests)
    for header in (
        OPENCODE_PROJECT_HEADER,
        OPENCODE_CLIENT_HEADER,
        USER_AGENT_HEADER,
    ):
        assert free_headers[header] == paid_headers[header]
    for header in (OPENCODE_SESSION_HEADER, OPENCODE_REQUEST_HEADER):
        assert free_headers.get(header)
        assert paid_headers.get(header)


@pytest.mark.anyio
async def test_the_translated_catalogue_is_the_same_on_both_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Z's scope did not move: the credential and the tool names agree."""

    wire = _Wire()
    provider = _build(monkeypatch, wire)
    free_leaf = next(_leaves(provider.public))
    paid_leaf = next(_leaves(provider.paid))

    assert dict(free_leaf.tool_catalogue_for(FREE)) == {
        "Bash": "bash",
        "Edit": "edit",
        "Glob": "glob",
        "Grep": "grep",
        "Read": "read",
    }
    assert dict(paid_leaf.tool_catalogue_for(FREE)) == dict(
        free_leaf.tool_catalogue_for(FREE)
    )
    assert paid_leaf.tool_catalogue_for(PAID) == {}


@pytest.mark.anyio
async def test_a_paid_model_keeps_the_operator_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = _Wire()
    provider = _build(monkeypatch, wire)
    await _drain(provider, PAID)

    assert wire.authorizations() == [f"Bearer {OPERATOR_KEY}"]


@pytest.mark.anyio
async def test_the_opt_out_restores_the_key_for_free_models_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = _Wire()
    provider = _build(monkeypatch, wire, credential="key")
    await _drain(provider, FREE)
    await _drain(provider, PAID)

    assert wire.authorizations() == [
        f"Bearer {OPERATOR_KEY}",
        f"Bearer {OPERATOR_KEY}",
    ]
    # And nothing extra was built at all: the opt-out is one early return.
    assert not isinstance(provider, OpenCodeCredentialSplitProvider)


# -- no key at all -------------------------------------------------------------


@pytest.mark.anyio
async def test_with_no_key_a_free_model_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = _Wire()
    provider = _build(monkeypatch, wire, key="")
    await _drain(provider, FREE)

    assert wire.authorizations() == [f"Bearer {OPENCODE_PUBLIC_CREDENTIAL}"]


@pytest.mark.anyio
async def test_with_no_key_a_paid_model_is_refused_as_it_always_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = _Wire()
    provider = _build(monkeypatch, wire, key="")

    with pytest.raises(ApplicationUnavailableError) as raised:
        await _drain(provider, PAID)
    assert "OPENCODE_API_KEY is not set" in str(raised.value)
    assert wire.requests == []


# -- health --------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_429_on_the_shared_slot_leaves_the_operator_pool_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of a slot of its own: the shared bucket's limit is its own."""

    wire = _Wire()
    # Two operator keys, so the paid side is a real pool with real health to
    # leave alone rather than an empty list that could not move anyway.
    provider = _build(monkeypatch, wire, key=f"{OPERATOR_KEY},{OPERATOR_KEY}-b")
    before = [dict(row) for row in provider.key_health()]
    assert len(before) == 2

    wire.status = 429
    # The shared slot is a pool, so a 429 routes around the *model* exactly as
    # it does on an operator pool -- and names the shared slot, not a key.
    with pytest.raises(ModelRateLimited) as limited:
        await _drain(provider, FREE)
    assert limited.value.provider_id == "opencode"
    assert limited.value.model == FREE

    after = [dict(row) for row in provider.key_health()]
    assert [row["request_count"] for row in after] == [
        row["request_count"] for row in before
    ]
    assert [row["rate_limits"] for row in after] == [
        row["rate_limits"] for row in before
    ]
    assert [row["state"] for row in after] == [row["state"] for row in before]

    public_rows = provider.public_key_health()
    assert [row["key_label"] for row in public_rows] == [OPENCODE_PUBLIC_KEY_LABEL]
    assert public_rows[0]["rate_limits"] == 1
    # And nothing the operator typed was on that wire.
    assert wire.authorizations() == [f"Bearer {OPENCODE_PUBLIC_CREDENTIAL}"]


@pytest.mark.anyio
async def test_the_request_log_names_the_shared_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``public`` is what the log, Analytics and the CSV exports group on."""

    wire = _Wire()
    provider = _build(monkeypatch, wire)
    install_attribution()
    await _drain(provider, FREE)

    assert current_credential()[1] == OPENCODE_PUBLIC_KEY_LABEL


@pytest.mark.anyio
async def test_the_baseline_label_for_a_paid_request_is_the_operator_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = _Wire()
    provider = _build(monkeypatch, wire)

    assert provider.credential_label not in (None, OPENCODE_PUBLIC_KEY_LABEL)


# -- housekeeping --------------------------------------------------------------


@pytest.mark.anyio
async def test_the_model_listing_stops_spending_the_operator_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hourly discovery sweep and the Test button name no model at all.

    One 429 an hour, every hour, for two and a half days was this call
    re-confirming a limit on a key it had already exhausted.
    """

    wire = _Wire()
    provider = _build(monkeypatch, wire)
    await provider.list_model_infos()

    assert wire.authorizations() == [f"Bearer {OPENCODE_PUBLIC_CREDENTIAL}"]


@pytest.mark.anyio
async def test_an_hour_of_housekeeping_costs_the_operator_key_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The before/after measurement, on a fake Zen host that records auth.

    One hour of this install's housekeeping against Zen is: one discovery
    sweep (``MODEL_DISCOVERY_REFRESH_SECONDS`` defaults to 3600), plus
    whatever the operator presses. Both go through ``list_model_infos``. The
    proxy checker is not in this count at all -- it sends a credential-free
    ``HEAD`` at the provider's base URL and reads no key (``application/
    proxy_check.py``), which is a finding rather than a change.

    before (``key``): every one of them on the operator's key.
    after (``public``, the default): none of them.
    """

    def key_authenticated(wire: _Wire) -> int:
        return sum(1 for value in wire.authorizations() if value.endswith(OPERATOR_KEY))

    # One sweep an hour, plus two Test presses. Same script both times.
    async def one_hour(provider: Any) -> None:
        for _ in range(3):
            await provider.list_model_infos()

    before_wire = _Wire()
    before = _build(monkeypatch, before_wire, credential="key")
    await one_hour(before)

    after_wire = _Wire()
    after = _build(monkeypatch, after_wire, credential="public")
    await one_hour(after)

    assert key_authenticated(before_wire) == 3
    assert key_authenticated(after_wire) == 0
    assert after_wire.authorizations() == [f"Bearer {OPENCODE_PUBLIC_CREDENTIAL}"] * 3


@pytest.mark.anyio
async def test_a_refused_anonymous_listing_falls_back_to_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key stays the authority on what this host actually sells.

    A pruned or refused anonymous catalogue must never be the reason a paid
    Zen model disappears from the Models page.
    """

    calls: list[str] = []
    wire = _Wire()
    provider = _build(monkeypatch, wire)
    split = OpenCodeCredentialSplitProvider(
        provider._config,
        paid=_ListingSide("key", PAID_LISTING, calls),
        public=_ListingSide("public", httpx.HTTPError("nope"), calls),
    )
    assert await split.list_model_infos() == PAID_LISTING
    assert calls == ["public", "key"]


@pytest.mark.anyio
async def test_an_empty_anonymous_listing_falls_back_to_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    wire = _Wire()
    provider = _build(monkeypatch, wire)
    split = OpenCodeCredentialSplitProvider(
        provider._config,
        paid=_ListingSide("key", PAID_LISTING, calls),
        public=_ListingSide("public", frozenset(), calls),
    )
    assert await split.list_model_infos() == PAID_LISTING
    assert calls == ["public", "key"]


def test_the_capability_probe_picks_a_credential_per_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per model, not per press: thirty free models is thirty metered calls."""

    monkeypatch.setattr(
        "my_claude_code.providers.runtime.opencode_credentials"
        ".configured_opencode_free_tier_credential",
        lambda: "public",
    )
    assert probe_credential("opencode", FREE, OPERATOR_KEY) == (
        OPENCODE_PUBLIC_CREDENTIAL
    )
    assert probe_credential("opencode", PAID, OPERATOR_KEY) == OPERATOR_KEY
    # Every other provider, including Go, is identity.
    assert probe_credential("opencode_go", FREE, OPERATOR_KEY) == OPERATOR_KEY
    assert probe_credential("groq", FREE, OPERATOR_KEY) == OPERATOR_KEY


def test_the_opt_out_takes_the_probe_back_to_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "my_claude_code.providers.runtime.opencode_credentials"
        ".configured_opencode_free_tier_credential",
        lambda: "key",
    )
    assert probe_credential("opencode", FREE, OPERATOR_KEY) == OPERATOR_KEY


def test_a_keyless_zen_card_can_still_probe_free_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "my_claude_code.providers.runtime.opencode_credentials"
        ".configured_opencode_free_tier_credential",
        lambda: "public",
    )
    assert probe_fallback_credential("opencode", "") == OPENCODE_PUBLIC_CREDENTIAL
    assert probe_fallback_credential("opencode", OPERATOR_KEY) == OPERATOR_KEY
    assert probe_fallback_credential("groq", "") == ""


# -- scope ---------------------------------------------------------------------


def test_opencode_go_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """A paid subscription endpoint. ``public`` buys nothing there."""

    settings = _settings()
    monkeypatch.setattr("my_claude_code.config.settings.get_settings", lambda: settings)
    provider = create_provider("opencode_go", settings)
    assert not isinstance(provider, OpenCodeCredentialSplitProvider)
    assert next(_leaves(provider))._api_key == OPERATOR_KEY


def test_another_provider_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    groq: dict[str, Any] = {"GROQ_API_KEY": "gsk-1234567890"}
    settings = Settings(**groq)
    monkeypatch.setattr("my_claude_code.config.settings.get_settings", lambda: settings)
    provider = create_provider("groq", settings)
    assert not isinstance(provider, OpenCodeCredentialSplitProvider)


def test_the_shared_slot_is_a_pool_slot_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Labelled, health-tracked, and never inside the operator's rotation."""

    wire = _Wire()
    provider = _build(monkeypatch, wire)
    assert isinstance(provider, OpenCodeCredentialSplitProvider)
    assert isinstance(provider.public, RotatingProvider)
    assert provider.public._key_labels == (OPENCODE_PUBLIC_KEY_LABEL,)
    # The operator's side is what it would have been without this feature.
    assert not isinstance(provider.paid, OpenCodeCredentialSplitProvider)
    assert next(_leaves(provider.paid))._api_key == OPERATOR_KEY
