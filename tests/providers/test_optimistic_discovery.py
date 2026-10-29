"""Optimistic discovery: a model nobody published a door for is tried once (7.115.0).

7.114.0 made a model's known doors a *set*, ordered by the inbound client
family. That covers every model the vendor names a package for. It does not
cover the other half of the OpenCode rosters: a model whose registry entry
carries no ``provider.npm`` at all -- or one this build cannot map, like
``@ai-sdk/mistral`` -- publishes no surface, so the resolver falls through
every rung to the profile's floor and the model is served on Chat Completions
for ever, whatever protocol the client spoke.

7.115.0 adds one rung below the floor, on the two OpenCode profiles only
(``optimistic_first_try``): the door the client family prefers is **tried
once**, before the client's request, learned from, and fallen back from inside
the same attempt -- and the answer is remembered for seven days through the
same ``response_surface`` fact the 7.114.0 ladder already writes.

What these tests hold:

* the census, as a committed golden: 88 OpenCode models have no surface this
  build can use (24 Go + 64 Zen, of which 63 have no package at all and one --
  ``mistral-large-4`` -- publishes one this build cannot map), beside the 68
  that are used as declared and the 11 that are probed by 7.114.0's Google
  branch. Every one of the 88 is planned as ``[preferred, floor]`` by the real
  resolver, and every one of the 167 resolves byte for byte its old answer
  with the flag off;
* the loop: a guessed door that answers wrong-door evidence is pruned, the
  floor carries the client's request in the same attempt, the fact records
  the *kind*, *key*, *source* and *TTL* 7.114.0 already used, and the next
  request goes straight to the floor with no second probe; TTL expiry and
  *Forget* both re-arm it;
* the learn/don't-learn table on a **guessed** door: a bare 500, a 4xx naming
  an endpoint and a 404 teach; 429, 401, a credit 403 and a timeout never do
  and are raised as they always were; a country 403 keeps 7.96.1's documented
  asymmetry -- it is the vendor's wrong-door answer on a door MCC guessed at,
  and never evidence on a door MCC knows;
* the scope guards: a declared door, an operator override, a learned fact and
  every provider but the two OpenCode profiles never guess.

Every fake here answers on loopback; nothing in this file reaches a host.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from my_claude_code.application.model_metadata import (
    ResolvedResponseSurface,
    ResponseSurface,
    ResponseSurfaceSource,
)
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.client_family import ClientFamily, client_family_scope
from my_claude_code.core.failures import ExecutionFailure
from my_claude_code.providers.openai_chat import (
    OPENAI_CHAT_PROFILES,
    resolve_response_surface,
    resolve_surface_ladder,
)
from my_claude_code.providers.openai_chat.response_surface import (
    NPM_SURFACES,
    UNKNOWN_SURFACE_GUESS,
    UNSERVABLE_SURFACE_GUESS,
    remember_response_surface,
    remember_response_surfaces,
)
from my_claude_code.providers.openai_chat.responses_transport import (
    PROBE_MAX_OUTPUT_TOKENS,
    PROBE_PROMPT,
)
from my_claude_code.providers.recovery import facts as facts_module
from my_claude_code.providers.recovery import learned_fact_store
from my_claude_code.providers.runtime import models_dev
from tests.providers.test_surface_ladder import (
    BARE_500_BODY,
    GO,
    REGION_BODY,
    _chat_answers,
    _chat_raises,
    _drain,
    _error,
    _FakeMessages,
    _go_provider,
    _install_go_buckets,
    _npm,
    _request,
    _surface_facts,
    _write_zen_registry,
    _zen_provider,
)

HERE = Path(__file__).parent
CENSUS_PATH = HERE / "opencode_optimistic_census.json"

#: Two models the committed golden carries, one per host, whose surface the
#: vendor does not publish: Go's ``deepseek-v4-pro`` has no ``npm`` at all and
#: Zen's ``mistral-large-4`` publishes a package this build cannot map.
GO_UNKNOWN = "deepseek-v4-pro"
ZEN_UNKNOWN = "mistral-large-4"
#: A Go model the vendor DOES place on a door this build speaks.
GO_DECLARED = "minimax-m3"


# --------------------------------------------------------------------------
# support
# --------------------------------------------------------------------------


def _golden() -> dict[str, Any]:
    return json.loads(CENSUS_PATH.read_text(encoding="utf-8"))


def _install_census_registry() -> None:
    """Cache both models.dev buckets exactly as the census measured them."""

    golden = _golden()
    index: dict[str, Any] = {}
    for host, bucket in golden["buckets"].items():
        models: dict[str, Any] = {}
        for kind in ("declared", "unservable", "probed"):
            for model_id, npm in golden[kind][host].items():
                models[model_id] = {"provider": {"npm": npm}} if npm else {}
        index[bucket] = {"npm": "@ai-sdk/openai-compatible", "models": models}
    models_dev.write_models_dev_cache(index)
    models_dev.reset_models_dev_payload_cache()


def _rows(kind: str) -> Any:
    return iter(_golden()[kind].items())


def _profile(provider_id: str) -> Any:
    return OPENAI_CHAT_PROFILES[provider_id]


def _plan(
    provider_id: str,
    model_id: str,
    family: ClientFamily,
    *,
    optimistic: bool,
    measured: Any | None = None,
) -> Any:
    profile = _profile(provider_id)
    return resolve_surface_ladder(
        provider_id,
        model_id,
        client_family=family,
        registry_provider=profile.surface_registry_provider,
        declared=profile.response_surfaces,
        no_information=profile.no_information_surface,
        measured=profile.measured_surfaces if measured is None else measured,
        optimistic=optimistic,
    )


def _preferred(family: ClientFamily) -> ResponseSurface:
    return {
        ClientFamily.ANTHROPIC: ResponseSurface.MESSAGES,
        ClientFamily.OPENAI: ResponseSurface.RESPONSES,
    }[family]


def _expected_from_npm(
    npm: str | None,
) -> tuple[ResponseSurface, ResponseSurfaceSource]:
    """What the rungs above the floor answer, read straight out of the package.

    The oracle for the flag-off census is the vendor's own statement and
    nothing else: a package this build maps to a door it speaks is used as
    declared; no package, an unmapped one, and ``@ai-sdk/google`` all fall to
    the profile's floor -- the last of them as ``unservable`` only when the
    profile is asked for a *catalogue* answer, which this helper is not.
    """

    if npm is None:
        return ResponseSurface.CHAT_COMPLETIONS, ResponseSurfaceSource.PROFILE
    surface = NPM_SURFACES.get(npm)
    if surface is None:
        return ResponseSurface.CHAT_COMPLETIONS, ResponseSurfaceSource.PROFILE
    return surface, ResponseSurfaceSource.REGISTRY


# --------------------------------------------------------------------------
# the census: who is probed, and what is planned for them
# --------------------------------------------------------------------------


def test_the_census_golden_is_the_population_pr_three_probes() -> None:
    """88 models, and every one of them is here with the reason it is."""

    golden = _golden()
    probed = {host: len(rows) for host, rows in golden["probed"].items()}
    declared = {host: len(rows) for host, rows in golden["declared"].items()}
    unservable = {host: len(rows) for host, rows in golden["unservable"].items()}

    assert probed == {"opencode_go": 24, "opencode": 64}
    assert declared == {"opencode_go": 11, "opencode": 57}
    assert unservable == {"opencode_go": 2, "opencode": 9}
    assert sum(probed.values()) == 88
    # 63 models publish no package at all, and one publishes one this build
    # has never been able to map; both are "no surface" to the resolver.
    unmapped = {
        model: npm
        for host, rows in golden["probed"].items()
        for model, npm in rows.items()
        if npm is not None
    }
    assert unmapped == {ZEN_UNKNOWN: "@ai-sdk/mistral"}
    # 68 + 11 + 88 is every published OpenCode row.
    assert (
        sum(probed.values()) + sum(declared.values()) + sum(unservable.values()) == 167
    )
    assert golden["counts"]["opencode_go"]["published"] == 37
    assert golden["counts"]["opencode"]["published"] == 130


@pytest.mark.parametrize(
    "family", [ClientFamily.ANTHROPIC, ClientFamily.OPENAI, ClientFamily.UNKNOWN]
)
def test_every_probed_model_is_tried_once_on_the_preferred_door(
    family: ClientFamily,
) -> None:
    """The whole population, through the real resolver: one guess, then home."""

    _install_census_registry()
    rows = 0
    for host, models in _rows("probed"):
        for model_id in models:
            ladder = _plan(host, model_id, family, optimistic=True)
            floor = _plan(host, model_id, family, optimistic=False)
            assert len(floor.candidates) == 1, (host, model_id)
            assert floor.primary.source is ResponseSurfaceSource.PROFILE
            assert floor.primary.surface is ResponseSurface.CHAT_COMPLETIONS
            if family is ClientFamily.UNKNOWN:
                # Nothing preferred: the floor alone, byte for byte.
                assert ladder == floor, (host, model_id)
                continue
            assert [c.surface for c in ladder.candidates] == [
                _preferred(family),
                ResponseSurface.CHAT_COMPLETIONS,
            ], (host, model_id)
            assert ladder.candidates[0].guessed is True
            assert ladder.candidates[0].detail == UNKNOWN_SURFACE_GUESS
            # The floor keeps the source, the note and the label it had.
            assert ladder.candidates[1] == floor.primary, (host, model_id)
            assert ladder.candidates[1].guessed is False
            rows += 1
    if family is not ClientFamily.UNKNOWN:
        assert rows == 88


def test_the_rest_of_the_census_resolves_what_it_shipped_with() -> None:
    """The 68 declared rows and the 11 Google rows are not guesses.

    The flag-on answer of a declared model is a one-door answer -- the door the
    vendor published -- under every client family; the Google rows keep
    7.114.0's guessed ladder, with their own reason, and are never treated as
    "unknown".
    """

    _install_census_registry()
    for host, models in _rows("declared"):
        for model_id, npm in models.items():
            expected, source = _expected_from_npm(npm)
            for family in ClientFamily:
                # ``measured=()``: rung 3 is a dated table of its own and
                # ``test_the_measured_model_is_untouched_by_this_release``
                # covers the one model it names.
                ladder = _plan(host, model_id, family, optimistic=True, measured=())
                assert len(ladder.candidates) == 1, (host, model_id, family)
                assert ladder.primary.surface is expected, (host, model_id)
                assert ladder.primary.source is source, (host, model_id)
                assert ladder.primary.guessed is False, (host, model_id)

    for host, models in _rows("unservable"):
        for model_id in models:
            for family in ClientFamily:
                ladder = _plan(host, model_id, family, optimistic=True)
                surfaces = [candidate.surface for candidate in ladder.candidates]
                assert ResponseSurface.UNSERVABLE not in surfaces, (host, model_id)
                if family is ClientFamily.UNKNOWN:
                    assert len(ladder.candidates) == 1, (host, model_id)
                    assert ladder.primary.surface is ResponseSurface.CHAT_COMPLETIONS
                    continue
                assert surfaces == [
                    _preferred(family),
                    ResponseSurface.CHAT_COMPLETIONS,
                ], (host, model_id)
                assert ladder.candidates[0].detail == UNSERVABLE_SURFACE_GUESS
            # Without the flag the vendor's statement is still the answer: the
            # catalogue's ``unservable`` is what the page shows, untouched.
            off = _plan(host, model_id, ClientFamily.ANTHROPIC, optimistic=False)
            assert off.primary.surface is ResponseSurface.UNSERVABLE, model_id
            assert "Google" in off.primary.detail, model_id


def test_with_the_flag_off_every_model_answers_what_it_always_did() -> None:
    """The dry census over the golden: nothing moves while the flag is off.

    The oracle is the vendor's own package, read without the resolver: a
    mapped package is the declared door, everything else is the floor. All 167
    published rows, every client family, one candidate each.
    """

    _install_census_registry()
    seen = 0
    for kind in ("declared", "unservable", "probed"):
        for host, models in _rows(kind):
            for model_id, npm in models.items():
                expected, source = _expected_from_npm(npm)
                for family in ClientFamily:
                    ladder = _plan(
                        host, model_id, family, optimistic=False, measured=()
                    )
                    assert len(ladder.candidates) == 1, (host, model_id, family)
                    candidate = ladder.primary
                    assert candidate.surface is expected, (host, model_id, npm)
                    assert candidate.source is source, (host, model_id, npm)
                    assert candidate.guessed is False
                seen += 1
    assert seen == 167


def test_the_measured_model_is_untouched_by_this_release() -> None:
    """Rung 3 answers first, so the new rung is never reached for it."""

    _install_go_buckets()
    for family in ClientFamily:
        on = _plan("opencode_go", "muse-spark-1.3-contributor", family, optimistic=True)
        off = _plan(
            "opencode_go", "muse-spark-1.3-contributor", family, optimistic=False
        )
        assert on == off
        assert all(candidate.guessed is False for candidate in on.candidates)


# --------------------------------------------------------------------------
# the loop, on one fake upstream
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unknown_model_is_probed_once_and_served_from_the_floor() -> None:
    """The whole loop: guess, evidence, fallback inside the attempt, fact."""

    _install_go_buckets()
    provider = _go_provider()
    messages = _FakeMessages(probe_error=_error(500, BARE_500_BODY))
    chat = AsyncMock(return_value=_chat_answers())
    provider._messages_transport = messages
    provider._client.chat.completions.create = chat

    with client_family_scope("messages"):
        events = await _drain(provider.stream_response(_request(GO_UNKNOWN)))

    assert events, "the request is served inside this one attempt"
    assert messages.probes == [GO_UNKNOWN], "one probe on the guess"
    assert messages.streamed == [], "the client's request never reached the guess"
    assert chat.await_count >= 1, "the floor carried the client's request"

    facts = _surface_facts("opencode_go", GO_UNKNOWN)
    assert len(facts) == 1
    fact = facts[0]
    assert fact.fact_kind == facts_module.FACT_RESPONSE_SURFACE
    assert fact.provider_id == "opencode_go"
    assert fact.model_id == GO_UNKNOWN
    assert fact.value == ["chat_completions"]
    assert fact.source == "probe"
    assert fact.ttl_seconds == facts_module.INFERRED_FACT_TTL_SECONDS
    assert facts_module.DOCUMENT_VERSION == 2
    # The learned row is the whole answer now: no second probe, no guess.
    with client_family_scope("messages"):
        ladder = provider.resolved_surface_ladder(GO_UNKNOWN)
    assert len(ladder.candidates) == 1
    assert ladder.primary.source is ResponseSurfaceSource.LEARNED


@pytest.mark.asyncio
async def test_the_next_request_goes_straight_to_the_floor() -> None:
    """A learned fact prevents every later cost, for every client family."""

    _install_go_buckets()
    remember_response_surfaces(
        "opencode_go", GO_UNKNOWN, [ResponseSurface.CHAT_COMPLETIONS]
    )
    provider = _go_provider()
    messages = _FakeMessages()
    chat = AsyncMock(return_value=_chat_answers())
    provider._messages_transport = messages
    provider._client.chat.completions.create = chat

    with client_family_scope("messages"):
        await _drain(provider.stream_response(_request(GO_UNKNOWN)))

    assert messages.probes == [], "the pruned guess is not dialled again"
    assert messages.streamed == []
    assert chat.await_count >= 1


@pytest.mark.asyncio
async def test_a_guess_that_answers_is_remembered_and_reused() -> None:
    """The happy path of the guess: two calls once, one call from then on."""

    _install_go_buckets()
    provider = _go_provider()
    messages = _FakeMessages()
    chat = AsyncMock(return_value=_chat_answers())
    provider._messages_transport = messages
    provider._client.chat.completions.create = chat

    with client_family_scope("messages"):
        events = await _drain(provider.stream_response(_request(GO_UNKNOWN)))

    assert events
    assert messages.probes == [GO_UNKNOWN]
    assert messages.streamed == [GO_UNKNOWN], "then the client's request"
    assert chat.await_count == 0, "the floor is not needed when the guess answers"
    assert _surface_facts("opencode_go", GO_UNKNOWN)[0].value == [
        "messages",
        "chat_completions",
    ]

    again = _FakeMessages()
    provider._messages_transport = again
    with client_family_scope("messages"):
        await _drain(provider.stream_response(_request(GO_UNKNOWN)))
    assert again.probes == [], "no second probe"
    assert again.streamed == [GO_UNKNOWN], "straight to the door that answered"


@pytest.mark.asyncio
async def test_ttl_expiry_tries_the_preferred_door_once_more(monkeypatch) -> None:
    """The row stops applying, so the guess is armed again -- exactly once."""

    from types import SimpleNamespace

    _install_go_buckets()
    remember_response_surfaces(
        "opencode_go", GO_UNKNOWN, [ResponseSurface.CHAT_COMPLETIONS]
    )
    monkeypatch.setattr(
        facts_module,
        "get_settings",
        lambda: SimpleNamespace(
            stated_fact_ttl_seconds=1.0,
            inferred_fact_ttl_seconds=-1.0,
            withheld_fact_ttl_seconds=1.0,
        ),
    )
    provider = _go_provider()
    messages = _FakeMessages(probe_error=_error(500, BARE_500_BODY))
    chat = AsyncMock(return_value=_chat_answers())
    provider._messages_transport = messages
    provider._client.chat.completions.create = chat

    with client_family_scope("messages"):
        await _drain(provider.stream_response(_request(GO_UNKNOWN)))

    assert messages.probes == [GO_UNKNOWN], "exactly one probe after expiry"


@pytest.mark.asyncio
async def test_forget_re_arms_the_preferred_door() -> None:
    """The Models page's Forget control is the operator's way back in."""

    _install_go_buckets()
    remember_response_surfaces(
        "opencode_go", GO_UNKNOWN, [ResponseSurface.CHAT_COMPLETIONS]
    )
    assert learned_fact_store().forget(
        "opencode_go", GO_UNKNOWN, facts_module.FACT_RESPONSE_SURFACE
    )
    provider = _go_provider()
    messages = _FakeMessages(probe_error=_error(500, BARE_500_BODY))
    chat = AsyncMock(return_value=_chat_answers())
    provider._messages_transport = messages
    provider._client.chat.completions.create = chat

    with client_family_scope("messages"):
        await _drain(provider.stream_response(_request(GO_UNKNOWN)))

    assert messages.probes == [GO_UNKNOWN]


# --------------------------------------------------------------------------
# which shapes teach on a guessed door, and which never do
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_404_on_the_guessed_door_is_the_wrong_door_answer() -> None:
    """Nothing vouched for the path, so "not here" is a statement about it."""

    _install_go_buckets()
    provider = _go_provider()
    messages = _FakeMessages(probe_error=_error(404, {"message": "not found"}))
    chat = AsyncMock(return_value=_chat_answers())
    provider._messages_transport = messages
    provider._client.chat.completions.create = chat

    with client_family_scope("messages"):
        events = await _drain(provider.stream_response(_request(GO_UNKNOWN)))

    assert events
    assert messages.streamed == []
    assert _surface_facts("opencode_go", GO_UNKNOWN)[0].value == ["chat_completions"]


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
async def test_an_ordinary_failure_on_the_guess_learns_nothing(
    failure: Exception,
) -> None:
    """429, 401, a credit refusal and a timeout are not about the door."""

    _install_go_buckets()
    provider = _go_provider()
    messages = _FakeMessages(probe_error=failure)
    chat = AsyncMock(return_value=_chat_answers())
    provider._messages_transport = messages
    provider._client.chat.completions.create = chat

    with client_family_scope("messages"), pytest.raises(httpx.HTTPError):
        await _drain(provider.stream_response(_request(GO_UNKNOWN)))

    assert _surface_facts("opencode_go", GO_UNKNOWN) == []
    assert chat.await_count == 0, "no fallback was earned"


@pytest.mark.asyncio
async def test_a_country_refusal_on_the_guess_is_the_vendors_wrong_door_answer() -> (
    None
):
    """7.96.1's asymmetry, unchanged by this release and asserted so.

    On a door MCC *knows*, a country refusal raises before anything else is
    tried -- every door is behind the same refused address. On a door MCC
    *guessed*, the same 403 is sometimes the vendor's wrong-door answer
    (``anomalyco/opencode#47969``: Zen's region check runs in front of its
    endpoint routing), so it teaches and the request falls back. That was true
    of the profile's floor from 7.96.1 and of 7.114.0's guessed doors; 7.115.0
    adds a door to the same rule, not a rule.

    (Spec §9.4's table states this; §9.5's test-24 summary says the opposite.
    The shipped behaviour is the table's, and changing it here would move the
    flag-off answer for all 88 models, which this release must not do.)
    """

    _install_go_buckets()
    provider = _go_provider()
    messages = _FakeMessages(probe_error=_error(403, REGION_BODY))
    chat = AsyncMock(return_value=_chat_answers())
    provider._messages_transport = messages
    provider._client.chat.completions.create = chat

    with client_family_scope("messages"):
        events = await _drain(provider.stream_response(_request(GO_UNKNOWN)))

    assert events
    assert chat.await_count >= 1, "the floor serves the request"
    assert _surface_facts("opencode_go", GO_UNKNOWN)[0].value == ["chat_completions"]


@pytest.mark.asyncio
async def test_a_500_that_names_the_endpoint_teaches_on_the_guess() -> None:
    """The vendor's documented wrong-door answer, and the reason this works."""

    _install_go_buckets()
    provider = _go_provider()
    messages = _FakeMessages(probe_error=_error(500, BARE_500_BODY))
    provider._messages_transport = messages
    provider._client.chat.completions.create = AsyncMock(return_value=_chat_answers())

    with client_family_scope("messages"):
        await _drain(provider.stream_response(_request(GO_UNKNOWN)))

    assert _surface_facts("opencode_go", GO_UNKNOWN)[0].value == ["chat_completions"]


@pytest.mark.asyncio
async def test_the_guess_is_a_probe_not_the_clients_request() -> None:
    """An OpenAI-format client guesses Responses, and it is asked the probe."""

    _write_zen_registry(**{ZEN_UNKNOWN: _npm("@ai-sdk/mistral")})
    provider = _zen_provider()
    bodies: list[dict[str, Any]] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(500, json=BARE_500_BODY, request=request)

    provider._responses._client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)
    )
    _chat_raises(provider, _error(429, {"message": "quota"}))

    client_request = MessagesRequest(
        model=ZEN_UNKNOWN,
        max_tokens=16,
        messages=[Message(role="user", content="the client's own words")],
        tools=[],
    )

    with client_family_scope("chat_completions"), pytest.raises(ExecutionFailure):
        await _drain(provider.stream_response(client_request))

    (probe_body,) = bodies
    assert probe_body["max_output_tokens"] == PROBE_MAX_OUTPUT_TOKENS
    assert probe_body["input"][0]["content"] == [
        {"type": "input_text", "text": PROBE_PROMPT}
    ]
    assert "the client's own words" not in json.dumps(probe_body)
    assert "tools" not in probe_body
    assert _surface_facts("opencode", ZEN_UNKNOWN)[0].value == ["chat_completions"]


# --------------------------------------------------------------------------
# scope guards: what is never guessed at
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_declared_door_is_never_probed_on_an_opted_in_host() -> None:
    """The vendor named the door: it is used as declared, with the request."""

    _install_go_buckets()
    provider = _go_provider()
    messages = _FakeMessages()
    provider._messages_transport = messages

    with client_family_scope("messages"):
        await _drain(provider.stream_response(_request(GO_DECLARED)))

    assert messages.probes == [], "a declared door is not a guess"
    assert messages.streamed == [GO_DECLARED]
    assert _surface_facts("opencode_go", GO_DECLARED) == []


def test_an_operator_override_is_never_guessed(monkeypatch) -> None:
    """Rung 1 is exclusive, and it is not a guess."""

    from my_claude_code.config import model_overrides as overrides_module

    table = overrides_module.ModelParameterOverrides.from_document(
        {"models": {"opencode_go/deepseek-v4-pro": {"response_surface": "messages"}}}
    )
    monkeypatch.setattr(
        "my_claude_code.providers.openai_chat.response_surface.current_model_overrides",
        lambda: table,
    )
    _install_go_buckets()
    ladder = _plan("opencode_go", GO_UNKNOWN, ClientFamily.OPENAI, optimistic=True)
    assert len(ladder.candidates) == 1
    assert ladder.primary.source is ResponseSurfaceSource.OVERRIDE
    assert ladder.primary.surface is ResponseSurface.MESSAGES
    assert ladder.primary.guessed is False


def test_a_learned_fact_outranks_the_guess() -> None:
    """Rung 2 answers with one or more doors and the guess is never built."""

    _install_go_buckets()
    remember_response_surface(
        "opencode_go", GO_UNKNOWN, ResponseSurface.CHAT_COMPLETIONS
    )
    ladder = _plan("opencode_go", GO_UNKNOWN, ClientFamily.ANTHROPIC, optimistic=True)
    assert len(ladder.candidates) == 1
    assert ladder.primary.source is ResponseSurfaceSource.LEARNED


@pytest.mark.parametrize("provider_id", sorted(OPENAI_CHAT_PROFILES))
def test_every_provider_but_the_two_opencode_ones_is_inert(provider_id: str) -> None:
    """The flag, not a provider-id branch: only two profiles set it."""

    profile = OPENAI_CHAT_PROFILES[provider_id]
    expected = provider_id in {"opencode", "opencode_go"}
    assert profile.optimistic_first_try is expected, provider_id
    if expected:
        return
    _install_census_registry()
    for family in ClientFamily:
        ladder = resolve_surface_ladder(
            provider_id,
            "a-model-nobody-published",
            client_family=family,
            declared=profile.response_surfaces,
            no_information=profile.no_information_surface,
            by_upstream=profile.surface_by_upstream,
            measured=profile.measured_surfaces,
            optimistic=profile.optimistic_first_try,
        )
        assert all(candidate.guessed is False for candidate in ladder.candidates), (
            provider_id,
            family,
        )


def test_the_page_shows_the_floor_not_the_guess() -> None:
    """The Models page has no request in flight, so it has no preference.

    That is what keeps the equality baseline still: a page render calls the
    resolver with no family and no optimism, and answers the floor exactly as
    7.114.0 did.
    """

    from my_claude_code.api.model_admin import response_surface_payload

    _install_go_buckets()
    row = response_surface_payload("opencode_go", GO_UNKNOWN)
    assert row is not None
    assert row["value"] == "chat_completions"
    assert row["source"] == "profile"
    assert row["note"] == "no registry entry for this model"


def test_the_flag_off_answer_is_the_old_callers_answer() -> None:
    """``resolve_response_surface`` with no new argument answers the floor."""

    _install_go_buckets()
    before = resolve_response_surface(
        "opencode_go",
        GO_UNKNOWN,
        registry_provider=GO.surface_registry_provider,
        declared=GO.response_surfaces,
        no_information=GO.no_information_surface,
        measured=GO.measured_surfaces,
    )
    after = _plan("opencode_go", GO_UNKNOWN, ClientFamily.ANTHROPIC, optimistic=False)
    assert before == after.primary
    assert isinstance(before, ResolvedResponseSurface)
    assert before.label == "chat_completions (profile)"


def test_the_guess_reads_the_flag_and_nothing_else() -> None:
    """The same model on the same host resolves differently only by the flag."""

    _install_go_buckets()
    off = resolve_surface_ladder(
        "opencode_go",
        GO_UNKNOWN,
        client_family=ClientFamily.ANTHROPIC,
        registry_provider=GO.surface_registry_provider,
        declared=GO.response_surfaces,
        no_information=GO.no_information_surface,
        optimistic=False,
    )
    on = resolve_surface_ladder(
        "opencode_go",
        GO_UNKNOWN,
        client_family=ClientFamily.ANTHROPIC,
        registry_provider=GO.surface_registry_provider,
        declared=GO.response_surfaces,
        no_information=GO.no_information_surface,
        optimistic=True,
    )
    assert len(off.candidates) == 1
    assert len(on.candidates) == 2
    assert on.candidates[1] == off.candidates[0]
