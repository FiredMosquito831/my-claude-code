"""A 400 on one effort word costs that word, not the whole reasoning channel.

Before 7.12.0 the reactive path had exactly one thing to write down -- "this
model refused ``reasoning_effort``" -- so a host that answered ``xhigh`` with a
400 pinned the model to the endpoint's own default for the rest of the TTL,
including every request that only ever wanted ``medium``. The parsing needed to
do better already existed: ``parse_effort_enum`` has read hosts' own effort
vocabularies out of their rejections since 6.52.0 for the *Probe capabilities*
path. These tests pin the reactive path using it, and pin the two things that
must not change while it does: a host that refuses the whole field still loses
the whole field, and a value rejection that narrows nothing falls back to
exactly that coarse answer rather than leaving MCC re-sending a refused word.
"""

import json
from dataclasses import replace
from typing import Any

import httpx
import openai
import pytest

from my_claude_code.core.reasoning import (
    ReasoningDialectOrigin,
    ReasoningEffort,
    narrow_dialect_by_rejections,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import OpenAIChatProvider
from my_claude_code.providers.openai_chat.profiles import OPENAI_CHAT_PROFILES
from my_claude_code.providers.openai_chat.reasoning import NamedEffortReasoning
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.recovery import FACT_EFFORT_VALUE_REJECTED
from my_claude_code.providers.recovery.facts import (
    DOCUMENT_VERSION,
    FACT_REASONING_FIELD_REJECTED,
    SOURCE_REJECTION,
    utc_now_iso,
)
from my_claude_code.providers.recovery.reasoning_reject import rejected_effort_values
from my_claude_code.providers.recovery.store import (
    LearnedFactStore,
    set_learned_fact_store,
)

_MODEL = "m"
_PROVIDER_ID = "value_reject_test"

#: A host whose rungs are spelled under their own names, which is what makes a
#: value-level answer possible at all: ``xhigh`` here means ``xhigh`` on the
#: wire. The fleet default folds ``xhigh`` and ``max`` onto ``"high"``, and a
#: vocabulary that does that has no ``xhigh`` to remove.
_NATURAL_EFFORTS = NamedEffortReasoning(
    (
        (ReasoningEffort.LOW, "low"),
        (ReasoningEffort.MEDIUM, "medium"),
        (ReasoningEffort.HIGH, "high"),
        (ReasoningEffort.XHIGH, "xhigh"),
    )
)


def _bad_request(message: str) -> openai.BadRequestError:
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    response = httpx.Response(400, request=request)
    return openai.BadRequestError(
        message, response=response, body={"error": {"message": message}}
    )


class _FakeCreate:
    """Raises a scripted error per call, then succeeds, recording each body."""

    def __init__(self, errors: list[Exception | None]) -> None:
        self._errors = errors
        self.bodies: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> str:
        self.bodies.append(dict(kwargs))
        index = len(self.bodies) - 1
        error = self._errors[index] if index < len(self._errors) else None
        if error is not None:
            raise error
        return "stream"


def _provider(provider_id: str = "") -> OpenAIChatProvider:
    profile = replace(OPENAI_CHAT_PROFILES["xai"], reasoning=_NATURAL_EFFORTS)
    provider = OpenAIChatProvider(
        ProviderConfig(api_key="k", base_url="https://example.invalid/v1"),
        profile=profile,
        rate_limiter=ProviderRateLimiter(),
        provider_id=provider_id,
    )
    return provider


def _install(provider: OpenAIChatProvider, create: _FakeCreate) -> None:
    """Swap the SDK's ``create`` for a scripted double.

    Bound through an ``Any`` alias because the SDK types ``create`` as a
    three-way overload that no test double is assignable to, and this project
    bans suppression comments.
    """
    client: Any = provider._client
    client.chat.completions.create = create


def _body(effort: str = "xhigh") -> dict[str, Any]:
    return {"model": _MODEL, "messages": [], "reasoning_effort": effort}


@pytest.fixture
def store(tmp_path):
    """A real durable store for the process, torn down after the test."""

    opened = LearnedFactStore(flush_debounce_seconds=0.0)
    opened.enable_persistence(tmp_path / "learned_facts.json")
    set_learned_fact_store(opened)
    yield opened
    set_learned_fact_store(LearnedFactStore())


# --------------------------------------------------------------- the proof


@pytest.mark.asyncio
async def test_a_400_on_xhigh_leaves_high_reachable():
    """The spec's proof requirement, stated as the spec states it."""

    provider = _provider()
    _install(
        provider,
        _FakeCreate(
            [_bad_request("Invalid value 'xhigh' for parameter reasoning_effort")]
        ),
    )

    await provider._create_stream(_body())

    dialect = provider.reasoning_dialect(_MODEL)
    assert dialect.effort_values is not None
    assert ReasoningEffort.HIGH in dialect.effort_values
    assert ReasoningEffort.MEDIUM in dialect.effort_values
    assert ReasoningEffort.XHIGH not in dialect.effort_values
    # The channel itself survives: this is the whole difference from the
    # coarse answer, where the field name went with the word.
    assert dialect.effort_field == "reasoning_effort"


@pytest.mark.asyncio
async def test_the_field_rejection_is_not_written_alongside_the_value_one():
    """Writing both would be writing a contradiction.

    The coarse fact drops the whole channel, so a value fact stored beside it
    would be invisible for the rest of the TTL.
    """

    provider = _provider()
    _install(
        provider,
        _FakeCreate([_bad_request("Invalid value 'xhigh' for reasoning_effort")]),
    )

    await provider._create_stream(_body())

    assert provider._rejected_reasoning_fields == {}


# ------------------------------------------------ the two readings of a 400


def test_a_host_that_names_its_vocabulary_refuses_everything_else():
    error = _bad_request(
        "Invalid value: 'xhigh'. Supported values are: 'low', 'medium' and 'high'"
    )

    refused = rejected_effort_values(error, _body(), "reasoning_effort")

    assert "xhigh" in refused
    assert "max" in refused
    assert "minimal" in refused
    assert "high" not in refused
    assert "medium" not in refused


def test_an_echoed_invalid_value_is_not_read_as_a_rung():
    """The ``sent=`` argument's whole job, mirrored from the probe's own test.

    A host that writes ``xhigh is not one of low, high`` has named two rungs
    and one mistake; reading the mistake back as a rung would leave MCC
    believing the word it just had refused.
    """

    error = _bad_request("xhigh is not one of xhigh, low, high")

    refused = rejected_effort_values(error, _body(), "reasoning_effort")

    assert "xhigh" in refused
    assert "low" not in refused


def test_a_host_that_names_nothing_but_the_value_refuses_only_that_value():
    error = _bad_request("reasoning_effort 'xhigh' is not available on this model")

    assert rejected_effort_values(error, _body(), "reasoning_effort") == {"xhigh"}


def test_a_host_that_names_only_the_field_proves_nothing_about_a_value():
    """The invariant: value-level only ever *narrows* the old behaviour.

    ``Unsupported parameter: reasoning_effort`` says the host has no such knob.
    Reading it as "not that word" would leave MCC sending the field forever.
    """

    error = _bad_request("Unsupported parameter: reasoning_effort")

    assert rejected_effort_values(error, _body(), "reasoning_effort") == set()


@pytest.mark.asyncio
async def test_a_field_only_rejection_still_costs_the_whole_channel():
    provider = _provider()
    _install(
        provider, _FakeCreate([_bad_request("Unsupported parameter: reasoning_effort")])
    )

    await provider._create_stream(_body())

    dialect = provider.reasoning_dialect(_MODEL)
    assert dialect.effort_values is None
    assert dialect.effort_field == ""
    assert provider._rejected_reasoning_fields[_MODEL] != {}


def test_an_extra_body_effort_object_is_read_too():
    error = _bad_request("reasoning.effort 'xhigh' is not supported")
    body = {
        "model": _MODEL,
        "messages": [],
        "extra_body": {"reasoning": {"effort": "xhigh"}},
    }

    assert rejected_effort_values(error, body, "reasoning") == {"xhigh"}


# ------------------------------------------------------------- the collapse


def test_when_the_last_value_goes_the_channel_goes_with_it():
    """And the answer is byte-identical to the coarse rejection's."""

    dialect = _NATURAL_EFFORTS.dialect
    every_word = {effort.value for effort in ReasoningEffort}

    collapsed = narrow_dialect_by_rejections(
        dialect, {}, {"reasoning_effort": every_word}
    )
    coarse = narrow_dialect_by_rejections(dialect, {"reasoning_effort": "2026-09-14"})

    assert collapsed.effort_values is None
    assert collapsed.effort_field == ""
    assert collapsed.origin is ReasoningDialectOrigin.LEARNED
    assert replace(collapsed, learned_rejections=coarse.learned_rejections) == coarse


def test_a_refused_word_this_vocabulary_cannot_spell_falls_back_to_coarse():
    """A gateway whose rungs are its own words, pinned.

    Removing nothing and keeping the channel would mean re-sending the refused
    word and paying a 400 on every single request -- strictly worse than the
    behaviour this PR set out to improve.
    """

    dialect = _NATURAL_EFFORTS.dialect

    narrowed = narrow_dialect_by_rejections(
        dialect, {}, {"reasoning_effort": {"detailed"}}
    )

    assert narrowed.effort_values is None
    assert narrowed.effort_field == ""


def test_narrowing_with_nothing_to_narrow_is_the_identity():
    dialect = _NATURAL_EFFORTS.dialect

    assert narrow_dialect_by_rejections(dialect, {}, {}) is dialect
    assert narrow_dialect_by_rejections(dialect, {}, None) is dialect


# ------------------------------------------------------------- persistence


@pytest.mark.asyncio
async def test_the_refused_value_survives_a_restart(store, tmp_path):
    provider = _provider(_PROVIDER_ID)
    _install(
        provider,
        _FakeCreate([_bad_request("Invalid value 'xhigh' for reasoning_effort")]),
    )

    await provider._create_stream(_body())
    store.flush()

    reopened = LearnedFactStore(flush_debounce_seconds=0.0)
    reopened.enable_persistence(tmp_path / "learned_facts.json")
    set_learned_fact_store(reopened)
    second_start = _provider(_PROVIDER_ID)

    dialect = second_start.reasoning_dialect(_MODEL)
    assert ReasoningEffort.XHIGH not in (dialect.effort_values or frozenset())
    assert ReasoningEffort.HIGH in (dialect.effort_values or frozenset())


@pytest.mark.asyncio
async def test_one_fact_per_value_so_one_word_can_be_forgotten(store):
    """The key shape, pinned: the value is in the key, not in a list.

    Forgetting a single word on the Models page has to be possible; a list
    under one key could only ever be forgotten wholesale.
    """

    provider = _provider(_PROVIDER_ID)
    _install(
        provider,
        _FakeCreate(
            [
                _bad_request(
                    "Invalid value 'xhigh' for reasoning_effort. Supported "
                    "values are: 'low', 'medium' and 'high'"
                )
            ]
        ),
    )

    await provider._create_stream(_body())

    details = {
        fact.detail
        for fact in store.all_facts()
        if fact.fact_kind == FACT_EFFORT_VALUE_REJECTED
    }
    assert "reasoning_effort=xhigh" in details
    assert "reasoning_effort=max" in details
    assert len(details) == len(
        [f for f in store.all_facts() if f.fact_kind == FACT_EFFORT_VALUE_REJECTED]
    )


def test_a_detail_without_an_equals_sign_is_skipped(tmp_path):
    """No version of this code wrote such a row, so it is not guessed at."""

    document = {
        "version": DOCUMENT_VERSION,
        "facts": [
            {
                "provider_id": _PROVIDER_ID,
                "model_id": _MODEL,
                "fact_kind": FACT_EFFORT_VALUE_REJECTED,
                "value": True,
                "detail": "reasoning_effort",
                "learned_at": utc_now_iso(),
                "last_confirmed_at": utc_now_iso(),
                "source": SOURCE_REJECTION,
            }
        ],
    }
    path = tmp_path / "learned_facts.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    opened = LearnedFactStore(flush_debounce_seconds=0.0)
    opened.enable_persistence(path)

    assert opened.memory_for(_PROVIDER_ID).rejected_values_for(_MODEL) is None
    # Loaded, not applied: an operator can still see the row.
    assert len(opened.all_facts()) == 1


def test_the_value_fact_and_the_field_fact_share_one_clock():
    """A coarse fact outliving the fine one would re-take the whole channel."""

    from my_claude_code.providers.recovery.facts import FACT_TTL_SECONDS

    assert (
        FACT_TTL_SECONDS[FACT_EFFORT_VALUE_REJECTED]
        == FACT_TTL_SECONDS[FACT_REASONING_FIELD_REJECTED]
    )


# --------------------------------------------------------------- migration


def _version_one_document() -> dict[str, Any]:
    return {
        "version": 1,
        "facts": [
            {
                "provider_id": _PROVIDER_ID,
                "model_id": _MODEL,
                "fact_kind": FACT_REASONING_FIELD_REJECTED,
                "value": True,
                "detail": "reasoning_effort",
                "learned_at": utc_now_iso(),
                "last_confirmed_at": utc_now_iso(),
                "source": SOURCE_REJECTION,
            }
        ],
    }


def test_a_version_one_documents_field_rejections_are_not_carried_forward(tmp_path):
    """The migration, and why it exists.

    A version-1 row cannot distinguish "this model has no reasoning knob" from
    "this model would not take ``xhigh`` that once", because before 7.12.0 the
    second had nowhere else to be written. Carrying it forward would pin a
    model to the endpoint default that this build would have kept thinking on,
    for the rest of a six-week TTL. Dropping it costs one re-paid 400.
    """

    path = tmp_path / "learned_facts.json"
    path.write_text(json.dumps(_version_one_document()), encoding="utf-8")
    opened = LearnedFactStore(flush_debounce_seconds=0.0)
    opened.enable_persistence(path)

    assert opened.all_facts() == ()
    assert opened.memory_for(_PROVIDER_ID).rejections_for(_MODEL) in (None, {})


def test_a_current_version_documents_field_rejections_are_kept(tmp_path):
    document = _version_one_document()
    document["version"] = DOCUMENT_VERSION
    path = tmp_path / "learned_facts.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    opened = LearnedFactStore(flush_debounce_seconds=0.0)
    opened.enable_persistence(path)

    assert len(opened.all_facts()) == 1
    assert opened.memory_for(_PROVIDER_ID).rejections_for(_MODEL)


def test_a_document_with_no_version_marker_is_read_as_version_one(tmp_path):
    document = _version_one_document()
    del document["version"]
    path = tmp_path / "learned_facts.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    opened = LearnedFactStore(flush_debounce_seconds=0.0)
    opened.enable_persistence(path)

    assert opened.all_facts() == ()


def test_the_next_flush_writes_the_current_version(tmp_path):
    """The migration is a read-time drop; adoption is what upgrades the file."""

    path = tmp_path / "learned_facts.json"
    path.write_text(json.dumps(_version_one_document()), encoding="utf-8")
    opened = LearnedFactStore(flush_debounce_seconds=0.0)
    opened.enable_persistence(path)
    opened.memory_for(_PROVIDER_ID).learn_cap(_MODEL, 4096, evidence="<= 4096")
    opened.flush()

    assert json.loads(path.read_text(encoding="utf-8"))["version"] == DOCUMENT_VERSION
