"""Per-model wire-surface resolution, and the rung that corrects it.

The defect: OpenCode Zen fronts four APIs behind one base URL, MCC spoke Chat
Completions to all of them, and Zen answers the wrong one with a bare HTTP 500
that names nothing. 23 of those in five minutes on 2026-09-11, all on
``muse-spark-1.3-contributor-free``, all after the 6.69.0 identity fix landed.

What these tests hold:

* the surface comes from metadata -- an operator override, then a probe MCC
  ran, then the vendor's published registry, then the default -- and never from
  the model's name;
* the probe fires on a refusal that is *about the endpoint* and on nothing
  else. Never a 429, never a 401, never a credit 403, never a 500 that carried
  a real complaint;
* a probe that succeeds is remembered as ``source="probe"`` so the next request
  goes straight to the right door;
* a model that fails on every surface still fails the ordinary way, and keeps
  its place in the catalogue. Nothing here ever hides a model.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest

from my_claude_code.application.errors import ApplicationUnavailableError
from my_claude_code.application.model_metadata import (
    ResponseSurface,
    ResponseSurfaceSource,
)
from my_claude_code.config import model_overrides as overrides_module
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.failures import ExecutionFailure
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import (
    OPENAI_CHAT_PROFILES,
    create_openai_chat_provider,
    resolve_response_surface,
)
from my_claude_code.providers.openai_chat.response_surface import (
    alternative_surfaces,
    remember_response_surface,
)
from my_claude_code.providers.recovery import learned_fact_store
from my_claude_code.providers.recovery.surface import surface_shaped_failure
from my_claude_code.providers.runtime import models_dev
from tests.providers.support import passthrough_rate_limiter

DECLARED = OPENAI_CHAT_PROFILES["opencode"].response_surfaces

BARE_500_BODY = {
    "type": "error",
    "error": {"type": "error", "message": "Internal server error"},
}


# --------------------------------------------------------------------------
# support
# --------------------------------------------------------------------------


def _write_registry(**models: object) -> None:
    """Write a models.dev cache with one ``opencode`` bucket, as MCC caches it."""

    path = models_dev.models_dev_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    models_dev.write_models_dev_cache(
        {"opencode": {"npm": "@ai-sdk/openai-compatible", "models": dict(models)}},
        path,
    )
    models_dev.reset_models_dev_payload_cache()


def _npm(package: str) -> dict[str, object]:
    return {"provider": {"npm": package}}


def _error(status: int, body: object) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://opencode.ai/zen/v1/chat/completions")
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    return httpx.HTTPStatusError(
        f"upstream said {status}",
        request=request,
        response=httpx.Response(status, content=content, request=request),
    )


def _provider() -> Any:
    return create_openai_chat_provider(
        "opencode",
        ProviderConfig(api_key="sk-test", base_url="https://opencode.ai/zen/v1"),
        passthrough_rate_limiter(),
        profile=OPENAI_CHAT_PROFILES["opencode"],
    )


def _resolve(model: str) -> Any:
    return resolve_response_surface(
        "opencode", model, registry_provider="opencode", declared=DECLARED
    )


# --------------------------------------------------------------------------
# the resolver
# --------------------------------------------------------------------------


def test_a_model_with_no_provider_npm_override_uses_chat_completions() -> None:
    _write_registry(**{"mimo-v2.5-free": {}})
    resolved = _resolve("mimo-v2.5-free")
    assert resolved.surface is ResponseSurface.CHAT_COMPLETIONS
    assert resolved.source is ResponseSurfaceSource.DEFAULT


def test_a_model_whose_registry_says_ai_sdk_openai_uses_the_responses_path() -> None:
    _write_registry(**{"muse-spark-1.3-contributor-free": _npm("@ai-sdk/openai")})
    resolved = _resolve("muse-spark-1.3-contributor-free")
    assert resolved.surface is ResponseSurface.RESPONSES
    assert resolved.source is ResponseSurfaceSource.REGISTRY
    assert resolved.detail == "@ai-sdk/openai"
    assert resolved.label == "responses (registry)"


def test_a_messages_model_resolves_to_the_messages_surface() -> None:
    """The third door, wired in 7.13.0.

    Until then this same registry entry resolved to UNSERVABLE carrying "the
    host serves this model on its Messages API, which this provider does not
    speak". It does now.
    """

    _write_registry(**{"claude-fable-5-1": _npm("@ai-sdk/anthropic")})
    resolved = _resolve("claude-fable-5-1")
    assert resolved.surface is ResponseSurface.MESSAGES
    assert resolved.source is ResponseSurfaceSource.REGISTRY
    assert resolved.detail == "@ai-sdk/anthropic"
    assert resolved.label == "messages (registry)"


def test_a_messages_model_is_still_unservable_where_the_profile_cannot_speak_it():
    """The listed-never-hidden rule is a property of the resolver, not of Zen.

    A profile that declares only Chat Completions must still *list* such a
    model with the reason attached, which is the user's binding decision and
    the thing wiring one more surface must not quietly delete.
    """

    _write_registry(**{"claude-fable-5-1": _npm("@ai-sdk/anthropic")})
    resolved = resolve_response_surface(
        "opencode",
        "claude-fable-5-1",
        registry_provider="opencode",
        declared=(ResponseSurface.CHAT_COMPLETIONS,),
    )
    assert resolved.surface is ResponseSurface.UNSERVABLE
    assert "Messages API" in resolved.detail
    assert "@ai-sdk/anthropic" in resolved.detail


def test_a_google_model_is_unservable_and_says_why() -> None:
    _write_registry(**{"gemini-3.8-flash": _npm("@ai-sdk/google")})
    resolved = _resolve("gemini-3.8-flash")
    assert resolved.surface is ResponseSurface.UNSERVABLE
    assert "Google" in resolved.detail


def test_an_unknown_package_falls_through_instead_of_guessing() -> None:
    _write_registry(**{"a-model": _npm("@ai-sdk/something-nobody-has-shipped")})
    assert _resolve("a-model").surface is ResponseSurface.CHAT_COMPLETIONS


def test_a_missing_registry_leaves_every_model_on_the_default() -> None:
    """A fresh install with no cache must not lose a single model."""

    models_dev.reset_models_dev_payload_cache()
    resolved = _resolve("muse-spark-1.3-contributor-free")
    assert resolved.surface is ResponseSurface.CHAT_COMPLETIONS
    assert resolved.source is ResponseSurfaceSource.DEFAULT


def test_a_learned_probe_outranks_the_published_registry() -> None:
    _write_registry(**{"mimo-v2.5-free": {}})
    remember_response_surface(
        "opencode", "mimo-v2.5-free", ResponseSurface.RESPONSES, evidence="HTTP 500"
    )
    resolved = _resolve("mimo-v2.5-free")
    assert resolved.surface is ResponseSurface.RESPONSES
    assert resolved.source is ResponseSurfaceSource.LEARNED


def test_an_operator_override_outranks_everything(monkeypatch) -> None:
    _write_registry(**{"muse-spark-1.3-contributor-free": _npm("@ai-sdk/openai")})
    remember_response_surface(
        "opencode",
        "muse-spark-1.3-contributor-free",
        ResponseSurface.RESPONSES,
        evidence="probe",
    )
    table = overrides_module.ModelParameterOverrides.from_document(
        {
            "models": {
                "opencode/muse-spark-1.3-contributor-free": {
                    "response_surface": "chat_completions"
                }
            }
        }
    )
    monkeypatch.setattr(
        "my_claude_code.providers.openai_chat.response_surface.current_model_overrides",
        lambda: table,
    )
    resolved = _resolve("muse-spark-1.3-contributor-free")
    assert resolved.surface is ResponseSurface.CHAT_COMPLETIONS
    assert resolved.source is ResponseSurfaceSource.OVERRIDE


def test_an_unreadable_override_is_ignored_rather_than_obeyed(monkeypatch) -> None:
    _write_registry(**{"muse-spark-1.3-contributor-free": _npm("@ai-sdk/openai")})
    table = overrides_module.ModelParameterOverrides.from_document(
        {"providers": {"opencode": {"response_surface": "carrier-pigeon"}}}
    )
    monkeypatch.setattr(
        "my_claude_code.providers.openai_chat.response_surface.current_model_overrides",
        lambda: table,
    )
    assert _resolve("muse-spark-1.3-contributor-free").surface is (
        ResponseSurface.RESPONSES
    )


def test_a_surface_override_never_reaches_a_request_body() -> None:
    """The security boundary, enforced rather than documented."""

    table = overrides_module.ModelParameterOverrides.from_document(
        {"models": {"opencode/m": {"response_surface": "responses", "top_p": 0.5}}}
    )
    body: dict[str, Any] = {"model": "m"}
    applied = overrides_module.apply_model_parameter_overrides(
        body, provider_id="opencode", model_ref="opencode/m", overrides=table
    )
    assert "response_surface" not in body
    assert "response_surface" not in applied
    assert body["top_p"] == 0.5


def test_a_single_surface_profile_resolves_nothing_and_changes_nothing() -> None:
    """39 of the 41 profiles. Their behaviour must be untouched."""

    for provider_id, profile in OPENAI_CHAT_PROFILES.items():
        if provider_id.startswith("opencode"):
            continue
        assert profile.response_surfaces == ()
        assert profile.surface_registry_provider == ""


# --------------------------------------------------------------------------
# the failure signature
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (500, BARE_500_BODY),
        (500, {}),
        (403, {"type": "RegionError", "message": "This model is not available"}),
        (
            403,
            {"message": "This model is not available in your country"},
        ),
        (
            400,
            {"message": "this model must be called on /zen/v1/responses"},
        ),
    ],
)
def test_these_refusals_are_about_the_endpoint(status: int, body: object) -> None:
    assert surface_shaped_failure(_error(status, body)) is not None


@pytest.mark.parametrize(
    ("status", "body"),
    [
        # The two that must never move a request to another endpoint.
        (429, {"type": "FreeUsageLimitError", "message": "daily limit reached"}),
        (401, {"type": "CreditsError", "message": "out of credit"}),
        (403, {"type": "CreditsError", "message": "out of credit"}),
        (404, {"message": "model_not_found"}),
        # A 500 that actually said something is an outage, not a wrong door.
        (500, {"message": "upstream model timed out after 60s"}),
        (400, {"message": "reasoning_effort: Invalid option"}),
    ],
)
def test_these_refusals_are_not_about_the_endpoint(status: int, body: object) -> None:
    assert surface_shaped_failure(_error(status, body)) is None


def test_the_alternatives_never_include_the_surface_that_just_failed() -> None:
    assert alternative_surfaces(ResponseSurface.CHAT_COMPLETIONS, DECLARED) == (
        ResponseSurface.RESPONSES,
        ResponseSurface.MESSAGES,
    )
    assert alternative_surfaces(ResponseSurface.RESPONSES, DECLARED) == (
        ResponseSurface.CHAT_COMPLETIONS,
        ResponseSurface.MESSAGES,
    )
    assert alternative_surfaces(ResponseSurface.MESSAGES, DECLARED) == (
        ResponseSurface.CHAT_COMPLETIONS,
        ResponseSurface.RESPONSES,
    )
    assert alternative_surfaces(ResponseSurface.CHAT_COMPLETIONS, ()) == ()


# --------------------------------------------------------------------------
# the rung
# --------------------------------------------------------------------------


class _FakeResponses:
    """A Responses transport that answers exactly what a test tells it to."""

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
        self.url = "https://opencode.ai/zen/v1/responses"

    def build_body(self, request, **_: Any) -> tuple[dict[str, Any], dict[str, str]]:
        return {"model": request.model, "input": []}, {}

    async def probe(self, model_id: str) -> None:
        self.probes.append(model_id)
        if self.probe_error is not None:
            raise self.probe_error

    def stream(self, request, **_: Any):
        self.streamed.append(request.model)
        error = self.stream_error

        async def _run():
            if error is not None:
                raise error
            yield "event: message_start\n\n"

        return _run()

    async def aclose(self) -> None:
        return None


def _chat_raises(provider: Any, error: Exception) -> None:
    async def _create(*_: Any, **__: Any):
        raise error

    provider._client.chat.completions.create = _create


async def _drain(stream) -> list[str]:
    return [event async for event in stream]


def _request(model: str) -> MessagesRequest:
    return MessagesRequest(
        model=model,
        max_tokens=16,
        messages=[Message(role="user", content="hi")],
    )


@pytest.mark.asyncio
async def test_a_bare_500_on_chat_probes_responses_and_remembers_it() -> None:
    _write_registry(**{"m": {}})
    provider = _provider()
    fake = _FakeResponses()
    provider._responses_transport = fake
    _chat_raises(provider, _error(500, BARE_500_BODY))

    events = await _drain(provider.stream_response(_request("m")))

    assert events, "the retry on the other surface must actually serve the request"
    assert fake.probes == ["m"], "exactly one probe, on the other surface"
    assert fake.streamed == ["m"]
    facts = learned_fact_store().facts_for_model("opencode", "m")
    surface_facts = [f for f in facts if f.fact_kind == "response_surface"]
    assert len(surface_facts) == 1
    assert surface_facts[0].value == "responses"
    assert surface_facts[0].source == "probe"
    assert _resolve("m").source is ResponseSurfaceSource.LEARNED


@pytest.mark.asyncio
async def test_a_429_never_probes_another_surface() -> None:
    """The half that must not over-fire: a quota refusal is not a wrong door."""

    _write_registry(**{"m": {}})
    provider = _provider()
    fake = _FakeResponses()
    provider._responses_transport = fake
    _chat_raises(
        provider, _error(429, {"type": "FreeUsageLimitError", "message": "limit"})
    )

    with pytest.raises(ExecutionFailure):
        await _drain(provider.stream_response(_request("m")))

    assert fake.probes == []
    assert learned_fact_store().facts_for_model("opencode", "m") == ()


@pytest.mark.asyncio
async def test_a_failure_on_both_surfaces_raises_the_hosts_own_failure() -> None:
    """And learns nothing, and hides nothing."""

    _write_registry(**{"m": {}})
    provider = _provider()
    fake = _FakeResponses(probe_error=_error(500, BARE_500_BODY))
    provider._responses_transport = fake
    _chat_raises(provider, _error(500, BARE_500_BODY))

    with pytest.raises(ExecutionFailure):
        await _drain(provider.stream_response(_request("m")))

    assert fake.probes == ["m"]
    assert fake.streamed == []
    assert learned_fact_store().facts_for_model("opencode", "m") == ()
    # Still resolvable, still routable: nothing was withheld.
    assert _resolve("m").surface is ResponseSurface.CHAT_COMPLETIONS


# --------------------------------------------------------------------------
# a country refusal (7.96.1)
# --------------------------------------------------------------------------

REGION_BODY = {
    "type": "error",
    "error": {
        "type": "RegionError",
        "message": "This model is not available in your country.",
    },
}


def _region_403(carrier: str) -> Exception:
    """Zen's country refusal, on either carrier the chat door can raise."""

    if carrier == "httpx":
        return _error(403, REGION_BODY)
    request = httpx.Request("POST", "https://opencode.ai/zen/v1/chat/completions")
    return openai.PermissionDeniedError(
        "Error code: 403",
        response=httpx.Response(403, json=REGION_BODY, request=request),
        body=REGION_BODY["error"],
    )


def _know_chat_door(source: str, monkeypatch) -> None:
    """Make model ``m`` resolve to Chat Completions *because* of ``source``."""

    if source == "override":
        _write_registry(**{"m": {}})
        table = overrides_module.ModelParameterOverrides.from_document(
            {"models": {"opencode/m": {"response_surface": "chat_completions"}}}
        )
        monkeypatch.setattr(
            "my_claude_code.providers.openai_chat.response_surface.current_model_overrides",
            lambda: table,
        )
    elif source == "learned":
        _write_registry(**{"m": {}})
        remember_response_surface(
            "opencode", "m", ResponseSurface.CHAT_COMPLETIONS, evidence="probe"
        )
    else:
        _write_registry(**{"m": _npm("@ai-sdk/openai-compatible")})


class _FakeMessages:
    """A Messages transport whose probe is counted and refused."""

    def __init__(self) -> None:
        self.probes: list[str] = []

    async def probe(self, model_id: str) -> None:
        self.probes.append(model_id)
        raise _error(403, REGION_BODY)

    def stream(self, request, **_: Any):
        raise AssertionError("the Messages door is never streamed here")

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("carrier", ["httpx", "openai"])
@pytest.mark.parametrize("source", ["override", "learned", "registry"])
async def test_a_country_refusal_on_a_known_door_never_probes_the_others(
    source: str, carrier: str, monkeypatch
) -> None:
    """The other doors are behind the same refused address.

    The Responses fake would *serve* a probe, so a probe that ran would turn
    this failure into a success. Instead the host's own 403 must reach the
    proxy pool untouched, with no probe made and nothing learned.
    """

    _know_chat_door(source, monkeypatch)
    provider = _provider()
    assert provider.resolved_surface("m").source.value == source
    fake = _FakeResponses()
    provider._responses_transport = fake
    _chat_raises(provider, _region_403(carrier))

    with pytest.raises(ExecutionFailure) as caught:
        await _drain(provider.stream_response(_request("m")))

    assert caught.value.status_code == 403
    assert fake.probes == []
    assert fake.streamed == []
    resolved = provider.resolved_surface("m")
    assert resolved.surface is ResponseSurface.CHAT_COMPLETIONS
    assert resolved.source.value == source


@pytest.mark.asyncio
@pytest.mark.parametrize("carrier", ["httpx", "openai"])
async def test_a_country_refusal_on_a_guessed_door_still_probes_and_learns(
    carrier: str,
) -> None:
    """anomalyco/opencode#47969, kept.

    Zen sometimes answers the *wrong door* with this same country 403 while
    ``/responses`` serves the identical request from the same address. On a
    door nobody vouched for, the probe is what recovers the request and
    teaches MCC the right door.
    """

    _write_registry(**{"m": {}})
    provider = _provider()
    assert provider.resolved_surface("m").source is ResponseSurfaceSource.PROFILE
    fake = _FakeResponses()
    provider._responses_transport = fake
    _chat_raises(provider, _region_403(carrier))

    events = await _drain(provider.stream_response(_request("m")))

    assert events, "the probe on the other door must actually serve the request"
    assert fake.probes == ["m"]
    assert fake.streamed == ["m"]
    surface_facts = [
        (fact.value, fact.source)
        for fact in learned_fact_store().facts_for_model("opencode", "m")
        if fact.fact_kind == "response_surface"
    ]
    assert surface_facts == [("responses", "probe")]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["override", "learned", "registry"])
async def test_a_known_door_still_probes_on_a_wrong_door_answer_that_is_not_a_country(
    source: str, monkeypatch
) -> None:
    """Only the country refusal is exempted; a bare 500 still means a wrong door."""

    _know_chat_door(source, monkeypatch)
    provider = _provider()
    fake = _FakeResponses()
    provider._responses_transport = fake
    _chat_raises(provider, _error(500, BARE_500_BODY))

    events = await _drain(provider.stream_response(_request("m")))

    assert events
    assert fake.probes == ["m"]
    assert fake.streamed == ["m"]


@pytest.mark.asyncio
async def test_a_country_refusal_on_the_door_a_probe_just_proved_probes_no_further() -> (
    None
):
    """The door a probe proved inside this request is a known door from then on."""

    _write_registry(**{"m": {}})
    provider = _provider()
    fake = _FakeResponses(stream_error=_error(403, REGION_BODY))
    provider._responses_transport = fake
    messages = _FakeMessages()
    provider._messages_transport = messages
    _chat_raises(provider, _error(500, BARE_500_BODY))

    with pytest.raises(httpx.HTTPStatusError) as caught:
        await _drain(provider.stream_response(_request("m")))

    assert caught.value.response.status_code == 403
    assert fake.probes == ["m"]
    assert fake.streamed == ["m"]
    assert messages.probes == []


@pytest.mark.asyncio
async def test_a_registry_responses_model_goes_straight_to_responses() -> None:
    """No probe at all when the vendor already published the answer."""

    _write_registry(**{"muse-spark-1.3-contributor-free": _npm("@ai-sdk/openai")})
    provider = _provider()
    fake = _FakeResponses()
    provider._responses_transport = fake

    events = await _drain(
        provider.stream_response(_request("muse-spark-1.3-contributor-free"))
    )

    assert events
    assert fake.probes == []
    assert fake.streamed == ["muse-spark-1.3-contributor-free"]


@pytest.mark.asyncio
async def test_an_unservable_model_fails_with_the_reason_and_stays_listed() -> None:
    _write_registry(**{"gemini-3.8-flash": _npm("@ai-sdk/google")})
    provider = _provider()
    provider._responses_transport = _FakeResponses()

    with pytest.raises(ApplicationUnavailableError) as caught:
        await _drain(provider.stream_response(_request("gemini-3.8-flash")))

    assert "Google" in str(caught.value)
    assert _resolve("gemini-3.8-flash").surface is ResponseSurface.UNSERVABLE


# --------------------------------------------------------------------------
# what the operator sees
# --------------------------------------------------------------------------


def test_the_models_page_names_the_surface_and_where_it_came_from() -> None:
    from my_claude_code.api.model_admin import capability_payload

    _write_registry(**{"muse-spark-1.3-contributor-free": _npm("@ai-sdk/openai")})
    payload = capability_payload("opencode", "muse-spark-1.3-contributor-free", None)
    surface = payload["response_surface"]
    assert surface["value"] == "responses"
    assert surface["source"] == "registry"
    assert surface["source_label"] == "the vendor's published registry"
    assert surface["note"] == "@ai-sdk/openai"


def test_the_models_page_names_the_surface_a_model_is_served_on() -> None:
    from my_claude_code.api.model_admin import capability_payload

    _write_registry(**{"claude-fable-5-1": _npm("@ai-sdk/anthropic")})
    surface = capability_payload("opencode", "claude-fable-5-1", None)[
        "response_surface"
    ]
    assert surface["value"] == "messages"


def test_the_models_page_still_shows_unservable_with_the_reason_not_a_blank():
    """``@ai-sdk/google`` is not an endpoint of this gateway at all."""

    from my_claude_code.api.model_admin import capability_payload

    _write_registry(**{"gemini-3.8-flash": _npm("@ai-sdk/google")})
    surface = capability_payload("opencode", "gemini-3.8-flash", None)[
        "response_surface"
    ]
    assert surface["value"] == "unservable"
    assert "Google" in surface["note"]


def test_a_single_surface_provider_gains_no_row_at_all() -> None:
    from my_claude_code.api.model_admin import capability_payload

    payload = capability_payload("groq", "llama-3.3-70b", None)
    assert payload["response_surface"] is None


def test_the_surface_fact_kind_is_allow_listed_with_a_stated_ttl() -> None:
    from my_claude_code.providers.recovery.facts import (
        ALLOWED_FACT_KINDS,
        FACT_RESPONSE_SURFACE,
        FACT_TTL_SECONDS,
    )

    assert FACT_RESPONSE_SURFACE in ALLOWED_FACT_KINDS
    assert FACT_TTL_SECONDS[FACT_RESPONSE_SURFACE] > 0


def test_the_fixture_directory_holds_the_reference_capture() -> None:
    reference = (
        Path(__file__).resolve().parents[1]
        / "contracts"
        / "opencode_reference_request.json"
    )
    assert reference.is_file()
