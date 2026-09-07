"""The four promises a persisted learned fact makes, pinned.

Persistence changes the blast radius of every one of them. A wrong fact that
used to cost one restart now costs a TTL, so the rules about what may become a
fact at all have to be enforced rather than merely intended.
"""

from datetime import UTC, datetime, timedelta

import pytest

from my_claude_code.providers.chatgpt_oauth.provider import (
    CHATGPT_OAUTH_PROVIDER_ID,
    WithheldModelIds,
    is_model_denial,
)
from my_claude_code.providers.recovery import (
    ALLOWED_FACT_KINDS,
    ALLOWED_FACT_SOURCES,
    FACT_MODEL_WITHHELD,
    FACT_OUTPUT_CAP,
    MAX_EVIDENCE_CHARS,
    RecoveryMemory,
    rejected_reasoning_field,
)
from my_claude_code.providers.recovery.facts import (
    FACT_TTL_SECONDS,
    LearnedFact,
    bounded_evidence,
    fact_from_row,
    utc_now_iso,
)
from my_claude_code.providers.recovery.store import LearnedFactStore


class _Rejection(Exception):
    """A 400 in the shape the recovery matchers read."""

    def __init__(self, text: str, status_code: int = 400) -> None:
        super().__init__(text)
        self.status_code = status_code
        self.body = {"error": {"message": text}}


def test_learned_facts_are_hide_only() -> None:
    """A withheld id is withheld from listings and from nothing else.

    ``WithheldModelIds`` gained persistence and a sink in 6.52.0. Neither may
    give it a second power: no bench, no unsupported mark, no removal of a ref
    the operator configured. A route naming a withheld id still resolves and
    still serves.
    """

    withheld = WithheldModelIds()
    withheld.remember("gpt-9-imaginary")

    assert "gpt-9-imaginary" in withheld
    assert withheld.snapshot() == frozenset({"gpt-9-imaginary"})
    # The whole surface. Anything that could bench a key or mark a model
    # unsupported would have to be a new public name here.
    assert sorted(
        name
        for name in dir(withheld)
        if not name.startswith("__") and not name.startswith("_ids")
    ) == ["clear", "load", "remember", "sink", "snapshot"]


def test_learned_facts_never_bench_a_key() -> None:
    """Credential health is not a learned fact and is not in the allow-list.

    A persisted bench would survive the outage that caused it, which is the
    exact failure mode this store exists to avoid.
    """

    for banned in ("credential_bench", "key_cooldown", "rate_limit", "unsupported"):
        assert banned not in ALLOWED_FACT_KINDS
    assert frozenset({"rejection", "probe", "observation"}) == ALLOWED_FACT_SOURCES


def test_no_fact_is_written_without_upstream_evidence() -> None:
    """Never a model name, never a vote -- only what an upstream said.

    A 400 that names a *sampling* parameter is the case that proves it: it is
    not a reasoning rejection, so it teaches the store nothing.
    """

    body = {"model": "m", "reasoning_effort": "high", "top_p": 1}
    sampling = _Rejection(
        "Validation: top_p is immutable for this model and must be 0.95, got 1"
    )
    assert rejected_reasoning_field(sampling, body) is None

    named = _Rejection("Unrecognized request argument supplied: reasoning_effort")
    assert rejected_reasoning_field(named, body) == "reasoning_effort"


def test_every_fact_kind_is_allow_listed() -> None:
    """An unknown kind is dropped, not carried into request-shaping state."""

    row = {
        "provider_id": "custom_x",
        "model_id": "m",
        "fact_kind": "run_this_please",
        "value": True,
        "learned_at": utc_now_iso(),
        "last_confirmed_at": utc_now_iso(),
        "source": "rejection",
    }
    assert fact_from_row(row) is None
    assert fact_from_row({**row, "fact_kind": FACT_OUTPUT_CAP}) is not None
    # A known kind from an unknown source is dropped too: the source decides
    # the TTL, so an unreadable one has no clock.
    assert (
        fact_from_row({**row, "fact_kind": FACT_OUTPUT_CAP, "source": "hunch"}) is None
    )


@pytest.mark.parametrize("fact_kind", sorted(ALLOWED_FACT_KINDS))
def test_every_negative_expires(fact_kind: str) -> None:
    """C3: no persisted negative is applied for ever."""

    assert FACT_TTL_SECONDS[fact_kind] > 0


def test_stored_evidence_is_bounded_and_redacted() -> None:
    excerpt = bounded_evidence("Authorization: Bearer sk-" + "a" * 400)
    assert len(excerpt) <= MAX_EVIDENCE_CHARS
    assert "sk-" + "a" * 40 not in excerpt


def test_a_withheld_fact_expires_fastest_of_all() -> None:
    """The docs call a restart-forget the safety property; 72 h replaces it."""

    assert FACT_TTL_SECONDS[FACT_MODEL_WITHHELD] == 72 * 3600
    assert FACT_TTL_SECONDS[FACT_MODEL_WITHHELD] < FACT_TTL_SECONDS[FACT_OUTPUT_CAP]


def test_the_withheld_sink_only_fires_on_a_real_denial() -> None:
    """The persisted set is fed by the same predicate the process-wide one was."""

    assert is_model_denial(404, "no such thing")
    assert not is_model_denial(400, "top_p must be 0.95")


def test_stale_is_not_deletion() -> None:
    old = LearnedFact(
        provider_id="p",
        model_id="m",
        fact_kind=FACT_OUTPUT_CAP,
        value=4096,
        learned_at=utc_now_iso(),
        last_confirmed_at=(datetime.now(UTC) - timedelta(days=90))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        source="rejection",
    )
    store = LearnedFactStore(flush_debounce_seconds=0.0)
    store.load_document({"version": 1, "facts": [old.as_row()]})

    assert old.is_stale(datetime.now(UTC))
    assert len(store.all_facts()) == 1
    assert store.memory_for("p").cap_for("m") is None


def test_the_chatgpt_provider_id_is_the_one_the_store_keys_on() -> None:
    """A rename that split the two would silently orphan every withheld id."""

    assert CHATGPT_OAUTH_PROVIDER_ID == "chatgpt_oauth"


def test_a_bare_recovery_memory_is_still_constructible() -> None:
    """Existing tests construct one directly; the sink must stay optional."""

    memory = RecoveryMemory()
    assert memory.learn_cap("m", 16) == 16
    assert memory.remember_rejection("m", "reasoning_effort") is True
    assert memory.remember_rejection("m", "reasoning_effort") is False
