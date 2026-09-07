"""The durable store: what survives a restart, and what stops being applied.

The point of every case here is the trade the store makes. Persisting a
negative buys back one 400 per model per restart; expiring it buys back the
self-healing property that per-process forgetting used to give for free. Both
halves have to hold, or the feature is a regression against the project's own
stated principle.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from my_claude_code.providers.recovery import (
    FACT_MODEL_WITHHELD,
    FACT_OUTPUT_CAP,
    FACT_REASONING_FIELD_REJECTED,
    FACT_STREAM_USAGE_UNSUPPORTED,
    MAX_EVIDENCE_CHARS,
    RecoveryMemory,
)
from my_claude_code.providers.recovery.facts import (
    MAX_FACT_ROWS,
    SOURCE_REJECTION,
    utc_now_iso,
)
from my_claude_code.providers.recovery.store import LearnedFactStore


def _iso_ago(**delta) -> str:
    moment = datetime.now(UTC) - timedelta(**delta)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _store(tmp_path, name: str = "learned_facts.json") -> LearnedFactStore:
    store = LearnedFactStore(flush_debounce_seconds=0.0)
    store.enable_persistence(tmp_path / name)
    return store


def _restart(tmp_path, name: str = "learned_facts.json") -> LearnedFactStore:
    """Simulate the process ending and starting again on the same directory."""

    return _store(tmp_path, name)


def test_cap_survives_a_restart(tmp_path) -> None:
    store = _store(tmp_path)
    memory = store.memory_for("custom_x")
    memory.learn_cap("vendor/model", 4096, evidence="max_tokens must be <= 4096")
    store.flush()

    reopened = _restart(tmp_path)
    assert reopened.memory_for("custom_x").cap_for("vendor/model") == 4096


def test_rejection_survives_a_restart(tmp_path) -> None:
    store = _store(tmp_path)
    store.memory_for("custom_x").remember_rejection(
        "vendor/model", "reasoning_effort", evidence="unknown field reasoning_effort"
    )
    store.flush()

    rejections = (
        _restart(tmp_path).memory_for("custom_x").rejections_for("vendor/model")
    )
    assert rejections is not None
    assert "reasoning_effort" in rejections


def test_stream_usage_refusal_survives_a_restart(tmp_path) -> None:
    """The fact nobody recorded at all before 6.52.0, and the cheapest win."""

    store = _store(tmp_path)
    store.memory_for("custom_x").remember_stream_usage_refusal(
        "vendor/model", evidence="stream_options.include_usage is not supported"
    )
    store.flush()

    assert (
        _restart(tmp_path).memory_for("custom_x").stream_usage_refused("vendor/model")
    )


def test_withheld_id_survives_a_restart(tmp_path) -> None:
    store = _store(tmp_path)
    store.record(
        "chatgpt_oauth",
        "gpt-9-imaginary",
        FACT_MODEL_WITHHELD,
        True,
        evidence="the backend refused this model id by name",
    )
    store.flush()

    assert _restart(tmp_path).withheld_model_ids("chatgpt_oauth") == frozenset(
        {"gpt-9-imaginary"}
    )


@pytest.mark.parametrize(
    ("fact_kind", "age", "still_applied"),
    [
        (FACT_OUTPUT_CAP, {"days": 29}, True),
        (FACT_OUTPUT_CAP, {"days": 31}, False),
        (FACT_REASONING_FIELD_REJECTED, {"days": 6}, True),
        (FACT_REASONING_FIELD_REJECTED, {"days": 8}, False),
        (FACT_MODEL_WITHHELD, {"hours": 71}, True),
        (FACT_MODEL_WITHHELD, {"hours": 73}, False),
    ],
)
def test_stale_facts_are_loaded_but_not_applied(
    tmp_path, fact_kind, age, still_applied
) -> None:
    """Each evidence class has its own clock, and stale never means deleted."""

    document = {
        "version": 1,
        "facts": [
            {
                "provider_id": "custom_x",
                "model_id": "vendor/model",
                "fact_kind": fact_kind,
                "value": 4096 if fact_kind == FACT_OUTPUT_CAP else True,
                "detail": (
                    "reasoning_effort"
                    if fact_kind == FACT_REASONING_FIELD_REJECTED
                    else ""
                ),
                "learned_at": _iso_ago(days=90),
                "last_confirmed_at": _iso_ago(**age),
                "source": SOURCE_REJECTION,
                "evidence": "the host said so",
                "hits": 1,
            }
        ],
    }
    path = tmp_path / "learned_facts.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    store = _store(tmp_path)
    memory = store.memory_for("custom_x")
    applied = {
        FACT_OUTPUT_CAP: lambda: memory.cap_for("vendor/model") is not None,
        FACT_REASONING_FIELD_REJECTED: lambda: bool(
            memory.rejections_for("vendor/model")
        ),
        FACT_MODEL_WITHHELD: lambda: bool(store.withheld_model_ids("custom_x")),
    }[fact_kind]

    assert applied() is still_applied
    # Loaded either way: the row stays visible so an operator can see what MCC
    # used to believe and why it stopped.
    assert len(store.all_facts()) == 1


def test_a_relearned_fact_becomes_fresh_again(tmp_path) -> None:
    store = _store(tmp_path)
    store.record("custom_x", "m", FACT_OUTPUT_CAP, 4096, evidence="first")
    first = store.all_facts()[0]

    store.record("custom_x", "m", FACT_OUTPUT_CAP, 4096, evidence="second")
    second = store.all_facts()[0]

    assert second.hits == 2
    # learned_at answers "how long has MCC believed this"; last_confirmed_at
    # answers "when did evidence last say so". Conflating them would make a
    # month-old belief look like a discovery.
    assert second.learned_at == first.learned_at
    assert second.last_confirmed_at >= first.last_confirmed_at


def test_learn_cap_still_keeps_the_minimum(tmp_path) -> None:
    memory = _store(tmp_path).memory_for("custom_x")
    assert memory.learn_cap("m", 8192) == 8192
    assert memory.learn_cap("m", 4096) == 4096
    # A higher later statement does not contradict the number already proven.
    assert memory.learn_cap("m", 65536) == 4096


def test_failed_retry_writes_no_fact(tmp_path) -> None:
    """A rejection is written only after the stripped retry succeeded.

    Nothing in the ladder records a refusal; only the provider does, and only
    once the upstream accepted the rewritten body. This pins the seam: merely
    building a rung writes nothing.
    """

    from my_claude_code.providers.recovery import ReasoningStripRecovery

    store = _store(tmp_path)
    memory = store.memory_for("custom_x")
    rung = ReasoningStripRecovery(log_tag="TEST")
    assert rung.kind == "reasoning_field"
    assert memory.rejected_reasoning_fields == {}
    assert store.all_facts() == ()


def test_stored_evidence_is_bounded_and_redacted(tmp_path) -> None:
    store = _store(tmp_path)
    store.record(
        "custom_x",
        "m",
        FACT_OUTPUT_CAP,
        4096,
        evidence="Bearer sk-abcdefghijklmnopqrstuvwxyz0123456789 " + "x" * 400,
    )
    (fact,) = store.all_facts()

    assert len(fact.evidence) <= MAX_EVIDENCE_CHARS
    assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in fact.evidence


def test_unknown_fact_kind_is_dropped_with_a_log_line(tmp_path) -> None:
    path = tmp_path / "learned_facts.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "facts": [
                    {
                        "provider_id": "custom_x",
                        "model_id": "m",
                        "fact_kind": "execute_arbitrary_thing",
                        "value": True,
                        "learned_at": utc_now_iso(),
                        "last_confirmed_at": utc_now_iso(),
                        "source": SOURCE_REJECTION,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    store = _store(tmp_path)
    assert store.all_facts() == ()


def test_corrupt_learned_facts_file_starts_clean(tmp_path) -> None:
    """A decline, never a startup failure. The worst honest outcome is one 400."""

    path = tmp_path / "learned_facts.json"
    path.write_text("{not json at all", encoding="utf-8")

    store = _store(tmp_path)
    assert store.all_facts() == ()
    # And it is still usable afterwards.
    store.record("custom_x", "m", FACT_OUTPUT_CAP, 4096)
    assert len(store.all_facts()) == 1


def test_document_is_capped_and_evicts_oldest_first(tmp_path) -> None:
    store = LearnedFactStore(flush_debounce_seconds=0.0)
    for index in range(MAX_FACT_ROWS + 5):
        store.record("custom_x", f"m{index}", FACT_OUTPUT_CAP, 4096)

    assert len(store.all_facts()) == MAX_FACT_ROWS


@pytest.mark.asyncio
async def test_flush_is_debounced_and_runs_on_shutdown(tmp_path) -> None:
    path = tmp_path / "learned_facts.json"
    store = LearnedFactStore(path=path, flush_debounce_seconds=30.0)
    store.record("custom_x", "m", FACT_OUTPUT_CAP, 4096)

    # A learned fact must never make a request wait on a disk write, so
    # nothing is on disk yet.
    assert not path.exists()

    await store.close()
    assert json.loads(path.read_text(encoding="utf-8"))["facts"][0]["value"] == 4096


def test_forgetting_reaches_the_live_provider(tmp_path) -> None:
    """Forget must not wait for a restart, or the clamp keeps firing."""

    store = _store(tmp_path)
    memory = store.memory_for("custom_x")
    memory.learn_cap("m", 4096)
    assert memory.cap_for("m") == 4096

    assert store.forget("custom_x", "m", FACT_OUTPUT_CAP) == 1
    assert memory.cap_for("m") is None


def test_forget_has_three_granularities(tmp_path) -> None:
    store = _store(tmp_path)
    store.record("a", "m1", FACT_OUTPUT_CAP, 1)
    store.record("a", "m2", FACT_OUTPUT_CAP, 1)
    store.record("b", "m3", FACT_OUTPUT_CAP, 1)

    assert store.forget("a", "m1") == 1
    assert store.forget_provider("a") == 1
    assert store.forget_all() == 1


def test_a_disappeared_model_stops_being_applied_without_being_deleted(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    memory = store.memory_for("custom_x")
    memory.learn_cap("gone", 4096)

    assert store.retire_model("custom_x", "gone") == 1
    assert len(store.all_facts()) == 1
    # The retirement reaches the live memory the same way a forget does.
    store._resync_memories()
    assert memory.cap_for("gone") is None


def test_a_memory_with_no_sink_persists_nothing(tmp_path) -> None:
    """A bare RecoveryMemory is exactly what it always was."""

    memory = RecoveryMemory()
    memory.learn_cap("m", 4096)
    memory.remember_rejection("m", "reasoning_effort")
    memory.remember_stream_usage_refusal("m")

    assert memory.sink is None
    assert not (tmp_path / "learned_facts.json").exists()


def test_a_store_with_no_path_writes_nothing(tmp_path, monkeypatch) -> None:
    """Persistence is opt-in, so no embedded use can touch a real config dir."""

    store = LearnedFactStore(flush_debounce_seconds=0.0)
    store.record("custom_x", "m", FACT_STREAM_USAGE_UNSUPPORTED, True)

    assert store.path is None
    assert store.flush() is False
