"""The client-family surface ladder, its learning, and the unservable probe.

7.114.0 (OCGO-2). The identity fix in 7.113.5 taught MCC *which* door each Go
model is published on. This release lets a model have more than one door:

* a request is served from the doors **known** to serve the model -- an
  operator override, a fact this deployment learned, a dated measurement
  shipped with the build, the vendor's registry, the profile's floor -- in the
  order the **inbound client family** prefers (``core/client_family.py``): a
  Claude Code request asks Messages first, an OpenAI-format one asks Responses
  first, and everything with no request in flight prefers nothing;
* a known door that answers **wrong-door evidence** is pruned from the learned
  set for seven days and the client's request continues on the next known door
  **inside the same attempt**;
* a model the vendor places on an API this build cannot speak is no longer
  refused on an opted-in host: the door MCC *can* speak is probed once, ~16
  tokens, and the answer is remembered.

What these tests hold, in the order the spec lists them:

* a model with one known door resolves byte for byte what 7.113.5 answered --
  under every client family -- and a single-surface profile never resolves at
  all; the one model that moves in steady state is ``muse-spark-1.3-contributor``
  and it moves only for ``/v1/messages`` clients;
* the ladder falls back inside one attempt with no probe, writes the doors it
  has not disproved, and the next request starts where the working door is;
  TTL expiry and *Forget* both re-open the preferred door;
* the learn/don't-learn table, shape by shape: 429, 401, timeout and a country
  refusal on a **known** door learn nothing and probe nothing; a 404 teaches
  only on a door MCC **guessed**;
* the unservable probe: on and off, the Google door never dialled, the Models
  page two-state fixture, and the probe carrying the probe body rather than
  the client's request.

Every fake here answers on loopback; nothing in this file reaches a host.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from my_claude_code.application.errors import ApplicationUnavailableError
from my_claude_code.application.execution import ProviderExecutor
from my_claude_code.application.model_metadata import (
    MUSE_SPARK_GO_MEASURED,
    ResponseSurface,
    ResponseSurfaceSource,
)
from my_claude_code.application.routing import (
    ResolvedModel,
    RoutedMessagesPlan,
    RoutedMessagesRequest,
)
from my_claude_code.config import model_overrides as overrides_module
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.client_family import (
    ClientFamily,
    client_family_scope,
    current_client_family,
)
from my_claude_code.core.failures import ExecutionFailure
from my_claude_code.core.reasoning import (
    ReasoningAdaptation,
    ReasoningAdaptationKind,
    ReasoningPolicy,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import (
    CLIENT_FAMILY_ORDER,
    OPENAI_CHAT_PROFILES,
    create_openai_chat_provider,
    resolve_response_surface,
    resolve_surface_ladder,
)
from my_claude_code.providers.openai_chat.provider import _KNOWN_SURFACE_SOURCES
from my_claude_code.providers.openai_chat.response_surface import (
    remember_response_surface,
    remember_response_surfaces,
)
from my_claude_code.providers.recovery import facts as facts_module
from my_claude_code.providers.recovery import learned_fact_store
from my_claude_code.providers.runtime import models_dev
from tests.providers.support import passthrough_rate_limiter

HERE = Path(__file__).parent
BUCKETS_PATH = HERE / "opencode_go_registry_buckets.json"

ZEN = OPENAI_CHAT_PROFILES["opencode"]
GO = OPENAI_CHAT_PROFILES["opencode_go"]
GENERIC = OPENAI_CHAT_PROFILES["groq"]
INFERHUB = OPENAI_CHAT_PROFILES["inferhub"]

ZEN_BASE = "https://opencode.ai/zen/v1"
GO_BASE = "https://opencode.ai/inference/go/v1"

MUSE = "muse-spark-1.3-contributor"
GOOGLE = "gemini-4-argon-contributor"

BARE_500_BODY = {
    "type": "error",
    "error": {"type": "error", "message": "Internal server error"},
}
REGION_BODY = {
    "type": "error",
    "error": {
        "type": "RegionError",
        "message": "This model is not available in your country.",
    },
}


# --------------------------------------------------------------------------
# support
# --------------------------------------------------------------------------


def _install_go_buckets() -> None:
    """Cache both models.dev buckets as the world ships them (trimmed copy)."""

    path = models_dev.models_dev_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    models_dev.write_models_dev_cache(
        json.loads(BUCKETS_PATH.read_text(encoding="utf-8"))
    )
    models_dev.reset_models_dev_payload_cache()


def _write_zen_registry(**models: object) -> None:
    path = models_dev.models_dev_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    models_dev.write_models_dev_cache(
        {"opencode": {"npm": "@ai-sdk/openai-compatible", "models": dict(models)}}
    )
    models_dev.reset_models_dev_payload_cache()


def _npm(package: str) -> dict[str, object]:
    return {"provider": {"npm": package}}


def _error(status: int, body: object) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", f"{ZEN_BASE}/chat/completions")
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    return httpx.HTTPStatusError(
        f"upstream said {status}",
        request=request,
        response=httpx.Response(status, content=content, request=request),
    )


def _request(model: str) -> MessagesRequest:
    return MessagesRequest(
        model=model,
        max_tokens=16,
        messages=[Message(role="user", content="hi")],
    )


def _go_provider() -> Any:
    return create_openai_chat_provider(
        "opencode_go",
        ProviderConfig(api_key="sk-test", base_url=GO_BASE),
        passthrough_rate_limiter(),
        profile=GO,
    )


def _zen_provider(profile: Any = None) -> Any:
    return create_openai_chat_provider(
        "opencode",
        ProviderConfig(api_key="sk-test", base_url=ZEN_BASE),
        passthrough_rate_limiter(),
        profile=ZEN if profile is None else profile,
    )


def _go(model_id: str, **kwargs: Any) -> Any:
    return resolve_surface_ladder(
        "opencode_go",
        model_id,
        registry_provider=GO.surface_registry_provider,
        declared=GO.response_surfaces,
        no_information=GO.no_information_surface,
        measured=GO.measured_surfaces,
        **kwargs,
    )


def _zen(model_id: str, **kwargs: Any) -> Any:
    return resolve_surface_ladder(
        "opencode",
        model_id,
        registry_provider=ZEN.surface_registry_provider,
        declared=ZEN.response_surfaces,
        no_information=ZEN.no_information_surface,
        **kwargs,
    )


def _surface_facts(provider_id: str, model_id: str) -> list[Any]:
    return [
        fact
        for fact in learned_fact_store().facts_for_model(provider_id, model_id)
        if fact.fact_kind == facts_module.FACT_RESPONSE_SURFACE
    ]


async def _drain(stream: Any) -> list[str]:
    return [event async for event in stream]


class _FakeMessages:
    """A Messages transport that answers what a test tells it to."""

    def __init__(
        self,
        *,
        probe_error: Exception | None = None,
        stream_error: Exception | None = None,
    ) -> None:
        self.probe_error = probe_error
        self.stream_error = stream_error
        self.probes: list[str] = []
        self.streamed: list[str] = []
        self.labels: list[str] = []

    async def probe(self, model_id: str) -> None:
        self.probes.append(model_id)
        if self.probe_error is not None:
            raise self.probe_error

    def stream(self, request: MessagesRequest, **kwargs: Any) -> Any:
        self.streamed.append(request.model)
        self.labels.append(str(kwargs.get("surface_label") or ""))
        error = self.stream_error

        async def _run() -> Any:
            if error is not None:
                raise error
            yield "event: message_start\n\n"

        return _run()

    async def aclose(self) -> None:
        return None


class _FakeResponses:
    """A Responses transport that answers what a test tells it to."""

    def __init__(
        self,
        *,
        probe_error: Exception | None = None,
        stream_error: Exception | None = None,
    ) -> None:
        self.probe_error = probe_error
        self.stream_error = stream_error
        self.probes: list[str] = []
        self.streamed: list[str] = []
        self.labels: list[str] = []

    def build_body(
        self, request: MessagesRequest, **_: Any
    ) -> tuple[dict[str, Any], dict[str, str]]:
        return {"model": request.model, "input": []}, {}

    async def probe(self, model_id: str, *, free_tier_tools: Any = ()) -> None:
        self.probes.append(model_id)
        if self.probe_error is not None:
            raise self.probe_error

    def stream(self, request: MessagesRequest, **kwargs: Any) -> Any:
        self.streamed.append(request.model)
        self.labels.append(str(kwargs.get("surface_label") or ""))
        error = self.stream_error

        async def _run() -> Any:
            if error is not None:
                raise error
            yield "event: message_start\n\n"

        return _run()

    async def aclose(self) -> None:
        return None


class _ChatStream:
    """An SDK-shaped Chat Completions stream: chunks plus its response."""

    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks
        self.response = SimpleNamespace(headers=httpx.Headers())

    def __aiter__(self) -> Any:
        async def run() -> Any:
            for chunk in self._chunks:
                yield chunk

        return run()

    async def close(self) -> None:
        return None


def _chunk(content: str | None = None, finish: str | None = None) -> Any:
    if content is None and finish is None:
        return SimpleNamespace(choices=[], usage=None)
    delta = SimpleNamespace(content=content, reasoning_content=None, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish)], usage=None
    )


def _chat_answers() -> _ChatStream:
    return _ChatStream([_chunk("hi"), _chunk(finish="stop")])


def _chat_raises(provider: Any, error: Exception) -> None:
    async def _create(*_: Any, **__: Any) -> Any:
        raise error

    provider._client.chat.completions.create = _create


def _know_chat_door(source: str, monkeypatch: pytest.MonkeyPatch) -> None:
    if source == "override":
        table = overrides_module.ModelParameterOverrides.from_document(
            {"models": {"opencode/m": {"response_surface": "chat_completions"}}}
        )
        monkeypatch.setattr(
            "my_claude_code.providers.openai_chat.response_surface.current_model_overrides",
            lambda: table,
        )
    elif source == "learned":
        remember_response_surface(
            "opencode", "m", ResponseSurface.CHAT_COMPLETIONS, evidence="probe"
        )


# --------------------------------------------------------------------------
# one known door: the equality the fleet depends on
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "family", [ClientFamily.ANTHROPIC, ClientFamily.OPENAI, ClientFamily.UNKNOWN]
)
def test_a_model_with_one_known_door_resolves_what_it_shipped_with(
    family: ClientFamily,
) -> None:
    """A one-element set cannot be reordered, under any client family."""

    _write_zen_registry(**{"m": {}})
    before = resolve_response_surface(
        "opencode",
        "m",
        registry_provider=ZEN.surface_registry_provider,
        declared=ZEN.response_surfaces,
        no_information=ZEN.no_information_surface,
    )
    ladder = _zen("m", client_family=family)
    assert len(ladder.candidates) == 1
    assert ladder.primary == before
    assert ladder.primary.label == "chat_completions (profile)"


def test_every_profile_but_the_measured_one_has_a_one_door_ladder() -> None:
    """The blast radius, asserted rather than assumed."""

    declaring = {
        provider_id
        for provider_id, profile in OPENAI_CHAT_PROFILES.items()
        if profile.measured_surfaces
    }
    assert declaring == {"opencode_go"}
    optimistic = {
        provider_id
        for provider_id, profile in OPENAI_CHAT_PROFILES.items()
        if profile.optimistic_first_try
    }
    assert optimistic == {"opencode", "opencode_go"}


def test_each_rung_resolves_to_yesterdays_triple(monkeypatch) -> None:
    """Override, learned, registry, by-upstream and floor: one door each."""

    _write_zen_registry(**{"m": _npm("@ai-sdk/openai")})
    registry = _zen("m")
    assert len(registry.candidates) == 1
    assert registry.primary.label == "responses (registry)"

    learned = _zen("proved")
    assert learned.primary.source is ResponseSurfaceSource.PROFILE
    remember_response_surface("opencode", "proved", ResponseSurface.RESPONSES)
    learned = _zen("proved")
    assert len(learned.candidates) == 1
    assert learned.primary.label == "responses (learned)"

    table = overrides_module.ModelParameterOverrides.from_document(
        {"models": {"opencode/m": {"response_surface": "messages"}}}
    )
    monkeypatch.setattr(
        "my_claude_code.providers.openai_chat.response_surface.current_model_overrides",
        lambda: table,
    )
    assert _zen("m").primary.label == "messages (override)"

    inferhub = resolve_surface_ladder(
        "inferhub",
        "cc/claude-opus-4-6",
        declared=INFERHUB.response_surfaces,
        no_information=INFERHUB.no_information_surface,
        by_upstream=INFERHUB.surface_by_upstream,
    )
    assert len(inferhub.candidates) == 1
    assert inferhub.primary.surface is ResponseSurface.MESSAGES
    assert inferhub.primary.source is ResponseSurfaceSource.PROFILE


@pytest.mark.asyncio
async def test_a_single_surface_profile_never_resolves_a_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fast path at ``provider.stream_response``: no resolution, no label."""

    provider = create_openai_chat_provider(
        "groq",
        ProviderConfig(api_key="sk-test", base_url="https://api.groq.invalid/v1"),
        passthrough_rate_limiter(),
    )
    calls: list[str] = []

    def spy(model_id: str) -> Any:
        calls.append(model_id)
        raise AssertionError("a single-surface profile must not resolve a surface")

    monkeypatch.setattr(provider, "resolved_surface_ladder", spy)
    stream: Any = provider.stream_response(_request("llama-3.3-70b"))
    assert calls == []
    await stream.aclose()


# --------------------------------------------------------------------------
# the measured set and the client family
# --------------------------------------------------------------------------


def test_the_measured_set_is_ordered_by_the_client_family() -> None:
    """One steady-state move, and it is the *order*, never the membership."""

    _install_go_buckets()
    anthropic = [
        c.surface for c in _go(MUSE, client_family=ClientFamily.ANTHROPIC).candidates
    ]
    openai = [
        c.surface for c in _go(MUSE, client_family=ClientFamily.OPENAI).candidates
    ]
    unknown = [
        c.surface for c in _go(MUSE, client_family=ClientFamily.UNKNOWN).candidates
    ]

    assert anthropic == [
        ResponseSurface.MESSAGES,
        ResponseSurface.RESPONSES,
        ResponseSurface.CHAT_COMPLETIONS,
    ]
    # The door the family does not name is kept and sorted last, never dropped.
    assert openai == [
        ResponseSurface.RESPONSES,
        ResponseSurface.CHAT_COMPLETIONS,
        ResponseSurface.MESSAGES,
    ]
    # Nothing preferred: the discovery order the entry was authored in.
    assert unknown == anthropic
    assert _go(MUSE).primary.label == "messages (measured)"
    assert _go(MUSE, client_family=ClientFamily.OPENAI).primary.label == (
        "responses (measured)"
    )
    assert CLIENT_FAMILY_ORDER[ClientFamily.UNKNOWN] == ()


def test_the_measurement_outranks_the_registry() -> None:
    """The shipped measurement sits between a learned fact and the registry."""

    _install_go_buckets()
    # Go's bucket publishes ``@ai-sdk/openai`` for this model -- Responses.
    assert resolve_response_surface(
        "opencode_go",
        MUSE,
        registry_provider=GO.surface_registry_provider,
        declared=GO.response_surfaces,
        no_information=GO.no_information_surface,
    ).label == ("responses (registry)")
    # The measurement was authored messages-first and outranks it.
    assert _go(MUSE).primary.label == "messages (measured)"


def test_a_learned_scalar_row_still_reads_as_one_door() -> None:
    """The 7.113 shape on disk is a string, and it must keep working."""

    _install_go_buckets()
    learned_fact_store().record(
        "opencode_go",
        MUSE,
        facts_module.FACT_RESPONSE_SURFACE,
        "chat_completions",
        source="probe",
    )
    ladder = _go(MUSE, client_family=ClientFamily.ANTHROPIC)
    assert len(ladder.candidates) == 1
    assert ladder.primary.surface is ResponseSurface.CHAT_COMPLETIONS
    assert ladder.primary.source is ResponseSurfaceSource.LEARNED


# --------------------------------------------------------------------------
# the learning cycle, on one fake upstream
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_preferred_door_falls_back_inside_one_attempt_and_is_learned() -> (
    None
):
    """Messages answers wrong once; Responses serves; the fact is a list."""

    _install_go_buckets()
    provider = _go_provider()
    messages = _FakeMessages(stream_error=_error(500, BARE_500_BODY))
    responses = _FakeResponses()
    provider._messages_transport = messages
    provider._responses_transport = responses

    with client_family_scope("messages"):
        events = await _drain(provider.stream_response(_request(MUSE)))

    assert events, "the request must be served inside this one attempt"
    assert messages.streamed == [MUSE]
    assert messages.probes == [], "a known door is asked with the real request"
    assert responses.streamed == [MUSE]
    assert responses.probes == [], "no probe: the next candidate is known too"
    assert messages.labels == ["messages (measured)"]
    assert responses.labels == ["responses (measured)"]

    facts = _surface_facts("opencode_go", MUSE)
    assert len(facts) == 1
    assert facts[0].value == ["responses", "chat_completions"]
    assert facts[0].source == "probe"
    assert facts[0].ttl_seconds == facts_module.INFERRED_FACT_TTL_SECONDS
    assert facts_module.INFERRED_FACT_TTL_SECONDS == 7 * 86_400.0
    # A list rides through the document store with no schema change at all.
    assert facts_module.DOCUMENT_VERSION == 2


@pytest.mark.asyncio
async def test_the_next_request_starts_on_the_working_door() -> None:
    """No second cost: the pruned set is the whole answer next time."""

    _install_go_buckets()
    remember_response_surfaces(
        "opencode_go",
        MUSE,
        [ResponseSurface.RESPONSES, ResponseSurface.CHAT_COMPLETIONS],
    )
    provider = _go_provider()
    messages = _FakeMessages()
    responses = _FakeResponses()
    provider._messages_transport = messages
    provider._responses_transport = responses

    with client_family_scope("messages"):
        await _drain(provider.stream_response(_request(MUSE)))

    assert messages.streamed == [], "the pruned door is not dialled again"
    assert messages.probes == []
    assert responses.streamed == [MUSE]


@pytest.mark.asyncio
async def test_ttl_expiry_retries_the_preferred_door(monkeypatch) -> None:
    """The clock runs out, the learned row stops applying, the set returns."""

    _install_go_buckets()
    remember_response_surfaces(
        "opencode_go",
        MUSE,
        [ResponseSurface.RESPONSES, ResponseSurface.CHAT_COMPLETIONS],
    )
    assert _go(MUSE).primary.surface is ResponseSurface.RESPONSES

    monkeypatch.setattr(
        facts_module,
        "get_settings",
        lambda: SimpleNamespace(
            stated_fact_ttl_seconds=1.0,
            inferred_fact_ttl_seconds=-1.0,
            withheld_fact_ttl_seconds=1.0,
        ),
    )
    ladder = _go(MUSE, client_family=ClientFamily.ANTHROPIC)
    assert ladder.primary.surface is ResponseSurface.MESSAGES
    assert ladder.primary.source is ResponseSurfaceSource.MEASURED


@pytest.mark.asyncio
async def test_forget_clears_the_learned_set() -> None:
    _install_go_buckets()
    remember_response_surfaces("opencode_go", MUSE, [ResponseSurface.CHAT_COMPLETIONS])
    assert _go(MUSE).primary.surface is ResponseSurface.CHAT_COMPLETIONS

    forgotten = learned_fact_store().forget(
        "opencode_go", MUSE, facts_module.FACT_RESPONSE_SURFACE
    )
    assert forgotten == 1
    assert _go(MUSE).primary.surface is ResponseSurface.MESSAGES


# --------------------------------------------------------------------------
# which shapes teach, and which never do
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        _error(429, {"type": "FreeUsageLimitError", "message": "daily limit"}),
        _error(401, {"message": "invalid api key"}),
        _error(403, {"type": "CreditsError", "message": "out of credit"}),
        httpx.ReadTimeout("upstream timed out"),
    ],
)
async def test_an_ordinary_failure_on_a_known_door_learns_nothing(
    failure: Exception,
) -> None:
    _install_go_buckets()
    provider = _go_provider()
    provider._messages_transport = _FakeMessages(stream_error=failure)
    responses = _FakeResponses()
    provider._responses_transport = responses

    with client_family_scope("messages"), pytest.raises(httpx.HTTPError):
        await _drain(provider.stream_response(_request(MUSE)))

    assert _surface_facts("opencode_go", MUSE) == []
    assert responses.streamed == []
    assert responses.probes == []


@pytest.mark.asyncio
async def test_a_country_refusal_on_a_known_measured_door_probes_no_further() -> None:
    """7.96.1, and the mutation guard for ``MEASURED`` being *known*."""

    _install_go_buckets()
    provider = _go_provider()
    provider._messages_transport = _FakeMessages(stream_error=_error(403, REGION_BODY))
    responses = _FakeResponses()
    provider._responses_transport = responses

    with (
        client_family_scope("messages"),
        pytest.raises(httpx.HTTPStatusError) as caught,
    ):
        await _drain(provider.stream_response(_request(MUSE)))

    assert caught.value.response.status_code == 403
    assert responses.streamed == []
    assert responses.probes == []
    assert _surface_facts("opencode_go", MUSE) == []


@pytest.mark.asyncio
async def test_a_404_on_a_vouched_for_door_teaches_nothing() -> None:
    """A model-not-found is a statement about the model name."""

    _install_go_buckets()
    provider = _go_provider()
    provider._messages_transport = _FakeMessages(
        stream_error=_error(404, {"message": "model_not_found"})
    )
    responses = _FakeResponses()
    chat = AsyncMock()
    provider._responses_transport = responses
    provider._client.chat.completions.create = chat

    with client_family_scope("messages"), pytest.raises(httpx.HTTPError):
        await _drain(provider.stream_response(_request("minimax-m3")))

    assert _surface_facts("opencode_go", "minimax-m3") == []
    assert responses.probes == []
    assert chat.await_count == 0
    ladder = _go("minimax-m3", client_family=ClientFamily.ANTHROPIC)
    assert ladder.primary.label == "messages (registry)"


@pytest.mark.asyncio
async def test_a_404_on_a_guessed_door_is_the_wrong_door_answer() -> None:
    """Nothing vouched for the path, so "not here" is a statement about it."""

    _write_zen_registry(**{"gemini-3.8-flash": _npm("@ai-sdk/google")})
    provider = _zen_provider()
    messages = _FakeMessages(probe_error=_error(404, {"message": "not found"}))
    provider._messages_transport = messages
    provider._responses_transport = _FakeResponses()
    chat = AsyncMock(return_value=_chat_answers())
    provider._client.chat.completions.create = chat

    with client_family_scope("messages"):
        events = await _drain(provider.stream_response(_request("gemini-3.8-flash")))

    assert events
    assert messages.probes == ["gemini-3.8-flash"]
    assert chat.await_count == 1, "the floor carries the client's request"
    assert _surface_facts("opencode", "gemini-3.8-flash")[0].value == [
        "chat_completions"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        _error(429, {"type": "FreeUsageLimitError", "message": "daily limit"}),
        _error(403, {"type": "CreditsError", "message": "out of credit"}),
        httpx.ReadTimeout("upstream timed out"),
    ],
)
async def test_an_ordinary_failure_on_the_guess_learns_nothing(
    failure: Exception,
) -> None:
    _write_zen_registry(**{"gemini-3.8-flash": _npm("@ai-sdk/google")})
    provider = _zen_provider()
    provider._messages_transport = _FakeMessages(probe_error=failure)
    chat = AsyncMock(return_value=_chat_answers())
    provider._client.chat.completions.create = chat

    with client_family_scope("messages"), pytest.raises(httpx.HTTPError):
        await _drain(provider.stream_response(_request("gemini-3.8-flash")))

    assert _surface_facts("opencode", "gemini-3.8-flash") == []
    assert chat.await_count == 0


# --------------------------------------------------------------------------
# the unservable probe
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unservable_model_is_probed_instead_of_refused(monkeypatch) -> None:
    """``@ai-sdk/google`` is a reason to ask, not a reason to refuse (7.114.0)."""

    _write_zen_registry(**{"gemini-3.8-flash": _npm("@ai-sdk/google")})
    provider = _zen_provider()
    messages = _FakeMessages()
    provider._messages_transport = messages
    chat = AsyncMock(return_value=_chat_answers())
    provider._client.chat.completions.create = chat

    with client_family_scope("messages"):
        events = await _drain(provider.stream_response(_request("gemini-3.8-flash")))

    assert events
    assert messages.probes == ["gemini-3.8-flash"], "one probe on the guess"
    assert messages.streamed == ["gemini-3.8-flash"], "then the client's request"
    assert chat.await_count == 0, "the floor is not needed when the guess answers"
    facts = _surface_facts("opencode", "gemini-3.8-flash")
    assert facts[0].value == ["messages", "chat_completions"]
    assert facts[0].source == "probe"
    # The next request goes straight there, no probe.
    messages_again = _FakeMessages()
    provider._messages_transport = messages_again
    with client_family_scope("messages"):
        await _drain(provider.stream_response(_request("gemini-3.8-flash")))
    assert messages_again.probes == []
    assert messages_again.streamed == ["gemini-3.8-flash"]


@pytest.mark.asyncio
async def test_an_unservable_model_is_still_refused_without_the_flag() -> None:
    """Every host that did not opt in keeps the raise, byte for byte."""

    from dataclasses import replace

    _write_zen_registry(**{"gemini-3.8-flash": _npm("@ai-sdk/google")})
    provider = _zen_provider(profile=replace(ZEN, optimistic_first_try=False))
    provider._messages_transport = _FakeMessages()

    with (
        client_family_scope("messages"),
        pytest.raises(ApplicationUnavailableError) as caught,
    ):
        await _drain(provider.stream_response(_request("gemini-3.8-flash")))

    assert "cannot serve gemini-3.8-flash" in str(caught.value)
    assert "Google" in str(caught.value)


@pytest.mark.asyncio
async def test_the_floor_is_never_abandoned_and_nothing_replaces_the_last_answer() -> (
    None
):
    """Guess fails, floor fails, and the host's own failure is raised."""

    _write_zen_registry(**{"gemini-3.8-flash": _npm("@ai-sdk/google")})
    provider = _zen_provider()
    provider._messages_transport = _FakeMessages(probe_error=_error(500, BARE_500_BODY))
    provider._responses_transport = _FakeResponses(
        probe_error=_error(500, BARE_500_BODY)
    )
    _chat_raises(provider, _error(500, BARE_500_BODY))

    with client_family_scope("messages"), pytest.raises(ExecutionFailure):
        await _drain(provider.stream_response(_request("gemini-3.8-flash")))

    # The guess's failure was written (the model is on the floor), and the
    # floor's own failure writes nothing further: never an empty list.
    facts = _surface_facts("opencode", "gemini-3.8-flash")
    assert [fact.value for fact in facts] == [["chat_completions"]]


@pytest.mark.asyncio
async def test_the_guess_is_a_probe_not_the_clients_request() -> None:
    """OPENAI client: the guess is Responses, and it is asked the probe body."""

    _write_zen_registry(**{"gemini-3.8-flash": _npm("@ai-sdk/google")})
    provider = _zen_provider()
    bodies: list[dict[str, Any]] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(500, json=BARE_500_BODY, request=request)

    provider._responses._client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)
    )
    client_request = MessagesRequest(
        model="gemini-3.8-flash",
        max_tokens=16,
        messages=[Message(role="user", content="the client's own words")],
        tools=[],
    )
    _chat_raises(provider, _error(429, {"message": "quota"}))

    with client_family_scope("chat_completions"), pytest.raises(ExecutionFailure):
        await _drain(provider.stream_response(client_request))

    (probe_body,) = bodies
    assert probe_body["max_output_tokens"] == 16
    assert probe_body["input"][0]["content"] == [{"type": "input_text", "text": "hi"}]
    assert "the client's own words" not in json.dumps(probe_body)
    assert "tools" not in probe_body
    # And the failure taught the floor, because a bare 500 on a guess is the
    # wrong-door answer.
    assert _surface_facts("opencode", "gemini-3.8-flash")[0].value == [
        "chat_completions"
    ]


def test_the_google_door_is_never_dialled() -> None:
    """The vendor's statement is honoured: ``unservable`` is not a candidate."""

    _write_zen_registry(**{"gemini-3.8-flash": _npm("@ai-sdk/google")})
    provider = _zen_provider()
    ladder = provider.resolved_surface_ladder("gemini-3.8-flash")
    surfaces = [candidate.surface for candidate in ladder.candidates]
    assert ResponseSurface.UNSERVABLE not in surfaces
    assert surfaces == [ResponseSurface.CHAT_COMPLETIONS], (
        "UNKNOWN family: nothing preferred, so the floor alone"
    )
    with client_family_scope("messages"):
        assert [
            c.surface
            for c in provider.resolved_surface_ladder("gemini-3.8-flash").candidates
        ] == [
            ResponseSurface.MESSAGES,
            ResponseSurface.CHAT_COMPLETIONS,
        ]


def test_the_flag_is_inert_on_every_other_profile() -> None:
    assert GENERIC.optimistic_first_try is False
    assert INFERHUB.optimistic_first_try is False
    assert INFERHUB.measured_surfaces == ()
    assert [c.surface for c in resolve_surface_ladder("groq", "m").candidates] == [
        ResponseSurface.CHAT_COMPLETIONS
    ]
    unprefixed = resolve_response_surface(
        "inferhub",
        "some-model",
        declared=INFERHUB.response_surfaces,
        no_information=INFERHUB.no_information_surface,
        by_upstream=INFERHUB.surface_by_upstream,
        optimistic=INFERHUB.optimistic_first_try,
    )
    assert unprefixed.surface is ResponseSurface.CHAT_COMPLETIONS
    assert unprefixed.source is ResponseSurfaceSource.PROFILE


@pytest.mark.asyncio
async def test_a_registry_door_self_verifies_on_first_use() -> None:
    """The four ``@ai-sdk/anthropic`` models: used as declared, corrected if wrong.

    A single known door has no second candidate, so the fallback is the rung
    that already existed: one probe on the next declared door, proved and
    remembered. What 7.114.0 changes is what happens *after* that -- the fact
    is a set, and the next request starts on the working door.
    """

    _install_go_buckets()
    provider = _go_provider()
    provider._messages_transport = _FakeMessages(
        stream_error=_error(500, BARE_500_BODY)
    )
    chat = AsyncMock(return_value=_chat_answers())
    provider._client.chat.completions.create = chat

    with client_family_scope("messages"):
        events = await _drain(provider.stream_response(_request("minimax-m3")))

    assert events
    assert chat.await_count == 2, "one probe, then the client's request"
    assert _surface_facts("opencode_go", "minimax-m3")[0].value == ["chat_completions"]
    with client_family_scope("messages"):
        assert provider.resolved_surface_ladder("minimax-m3").primary.label == (
            "chat_completions (learned)"
        )


# --------------------------------------------------------------------------
# the Models page and the vocabulary
# --------------------------------------------------------------------------


def test_measured_is_a_known_source_with_a_page_label() -> None:
    from my_claude_code.api.model_admin import SURFACE_SOURCE_LABELS

    assert ResponseSurfaceSource.MEASURED in _KNOWN_SURFACE_SOURCES
    assert SURFACE_SOURCE_LABELS[ResponseSurfaceSource.MEASURED] == (
        "a measurement of this deployment"
    )


def test_the_models_page_shows_unservable_then_the_learned_door() -> None:
    """Two states, so the never-hidden rule is asserted on both sides."""

    from my_claude_code.api.model_admin import response_surface_payload

    _write_zen_registry(**{"gemini-3.8-flash": _npm("@ai-sdk/google")})
    before = response_surface_payload("opencode", "gemini-3.8-flash")
    assert before is not None
    assert before["value"] == "unservable"
    assert before["source"] == "registry"
    assert before["source_label"] == "the vendor's published registry"
    assert "Google" in before["note"]
    assert before["note"].endswith("MCC will probe it on first use")

    remember_response_surfaces(
        "opencode", "gemini-3.8-flash", [ResponseSurface.CHAT_COMPLETIONS]
    )
    after = response_surface_payload("opencode", "gemini-3.8-flash")
    assert after is not None
    assert after["value"] == "chat_completions"
    assert after["source"] == "learned"
    assert after["source_label"] == "a probe of this deployment"


def test_the_measured_row_is_what_the_page_shows_for_muse() -> None:
    from my_claude_code.api.model_admin import response_surface_payload

    _install_go_buckets()
    row = response_surface_payload("opencode_go", MUSE)
    assert row is not None
    assert row["value"] == "messages"
    assert row["source"] == "measured"
    assert row["source_label"] == "a measurement of this deployment"
    assert row["note"] == "measured 2026-10-29"
    assert MUSE_SPARK_GO_MEASURED.measured_on.isoformat() == "2026-10-29"


# --------------------------------------------------------------------------
# the seam: the executor installs the family, and nothing else has one
# --------------------------------------------------------------------------


class _FamilyRecordingProvider:
    """A provider port whose stream records the family it was started under."""

    def __init__(self) -> None:
        self.families: list[ClientFamily] = []

    def throttle_remaining(self, model: str | None = None) -> float:
        return 0.0

    @property
    def credential_label(self) -> str | None:
        return None

    def preflight_stream(
        self, request: MessagesRequest, *, reasoning: ReasoningPolicy
    ) -> None:
        return None

    def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy,
    ) -> Any:
        self.families.append(current_client_family())

        async def _run() -> Any:
            yield "event: message_stop\ndata: {}\n\n"

        return _run()


def _routed() -> RoutedMessagesRequest:
    request = MessagesRequest(
        model="some-model",
        messages=[Message(role="user", content="hello")],
        stream=True,
    )
    return RoutedMessagesRequest(
        request=request,
        resolved=ResolvedModel(
            original_model="gateway-model",
            provider_id="provider",
            provider_model="some-model",
            provider_model_ref="provider/some-model",
            reasoning_preference=ReasoningPreference.CLIENT,
        ),
        reasoning=ReasoningPolicy.on(),
        requested_reasoning=ReasoningPolicy.on(),
        reasoning_adaptation=ReasoningAdaptation(
            ReasoningAdaptationKind.UNCHANGED, None
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wire_api", "expected"),
    [
        ("messages", ClientFamily.ANTHROPIC),
        ("chat_completions", ClientFamily.OPENAI),
        ("responses", ClientFamily.OPENAI),
        ("gemini", ClientFamily.UNKNOWN),
    ],
)
async def test_the_executor_installs_the_client_family(
    wire_api: Any, expected: ClientFamily
) -> None:
    provider = _FamilyRecordingProvider()
    executor = ProviderExecutor(
        lambda _provider_id: provider,
        token_counter=lambda _messages, _system, _tools: 7,
    )
    stream = executor.stream(
        RoutedMessagesPlan((_routed(),)),
        wire_api=wire_api,
        raw_log_label="TEST",
        raw_log_payload={},
        request_id="req_family",
    )
    await _drain(stream)
    assert provider.families == [expected]
    assert current_client_family() is ClientFamily.UNKNOWN


def test_outside_a_request_the_family_is_unknown_and_resets() -> None:
    assert current_client_family() is ClientFamily.UNKNOWN
    with client_family_scope("messages"):
        assert current_client_family() is ClientFamily.ANTHROPIC
        with client_family_scope("chat_completions"):
            assert current_client_family() is ClientFamily.OPENAI
        assert current_client_family() is ClientFamily.ANTHROPIC
    assert current_client_family() is ClientFamily.UNKNOWN
