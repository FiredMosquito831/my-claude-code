"""The per-model in-loop count: a turn that followed a tool result, per model.

The user's question (2026-10-29) is "which model says it will keep reading and
then stops", and the answer has to come out of the request log alone: a turn is
**in-loop** when the request immediately before it in the same conversation
thread ended with a tool call, and it **ended with text only** when it called no
tool and delivered visible text. Everything that can be known is counted and
everything that cannot is left out rather than guessed -- the two failure modes
this file exists to keep apart are "this model stops often" and "this log does
not measure that".
"""

import time
from typing import Literal

import pytest

from my_claude_code.core.request_log import RequestLogStore, RequestRecord


@pytest.fixture
def store(tmp_path):
    store = RequestLogStore(tmp_path / "requests.db", max_rows=100)
    yield store
    store.close()


def _turn(
    request_id: str,
    at: float,
    *,
    session: str | None = "s1",
    agent: str | None = None,
    tool_calls: int | None = None,
    text_chars: int | None = 100,
    status: Literal["success", "error", "cancelled"] = "success",
    provider: str | None = "p1",
    model: str | None = "m1",
) -> RequestRecord:
    """One logged request. ``tool_calls=None`` is how the writer records none."""

    return RequestRecord(
        id=request_id,
        endpoint="/v1/messages",
        protocol="anthropic",
        provider=provider,
        resolved_model=model,
        ts_epoch=at,
        status=status,
        tool_call_count=tool_calls,
        output_chars=text_chars,
        session_id=session,
        agent_id=agent,
    )


def _count(store: RequestLogStore, **kwargs) -> dict[str, dict[str, int]]:
    return {row["model_ref"]: row for row in store.in_loop_text_only_by_model(**kwargs)}


def test_a_text_only_turn_after_a_tool_call_is_counted(store) -> None:
    """The whole shape: tool call, then a turn that answers and calls nothing."""

    store.enqueue(_turn("one", 100.0, tool_calls=1, text_chars=0))
    store.enqueue(_turn("two", 110.0, tool_calls=None, text_chars=120))
    store.close()

    (row,) = store.in_loop_text_only_by_model()
    assert row["model_ref"] == "p1/m1"
    assert row["in_loop_turns"] == 1
    assert row["text_only_turns"] == 1
    assert row["unmeasured_turns"] == 0


def test_a_turn_after_a_text_answer_is_not_in_loop(store) -> None:
    """A follow-up to a text answer is a new user turn, not a loop turn."""

    store.enqueue(_turn("one", 100.0, tool_calls=None, text_chars=50))
    store.enqueue(_turn("two", 110.0, tool_calls=None, text_chars=60))
    store.close()

    assert store.in_loop_text_only_by_model() == []


def test_an_in_loop_turn_that_called_a_tool_is_the_denominator_only(store) -> None:
    """Working turns are in-loop and are not stops; the rate needs both."""

    store.enqueue(_turn("one", 100.0, tool_calls=2, text_chars=0))
    store.enqueue(_turn("two", 110.0, tool_calls=1, text_chars=30))
    store.enqueue(_turn("three", 120.0, tool_calls=None, text_chars=90))
    store.close()

    (row,) = store.in_loop_text_only_by_model()
    assert row["in_loop_turns"] == 2
    assert row["text_only_turns"] == 1


def test_a_reasoning_only_turn_is_in_loop_but_not_a_text_stop(store) -> None:
    """No tool and no visible text is not "ended with text only"."""

    store.enqueue(_turn("one", 100.0, tool_calls=1, text_chars=0))
    store.enqueue(_turn("two", 110.0, tool_calls=None, text_chars=0))
    store.close()
    (row,) = store.in_loop_text_only_by_model()
    assert row["in_loop_turns"] == 1
    assert row["text_only_turns"] == 0


def test_a_measured_zero_tool_call_reads_as_no_tool_call(store) -> None:
    """The writer stores NULL for "none" today; a 0 has to read the same way,
    or a future writer that starts storing the zero would silently zero this."""

    store.enqueue(_turn("one", 100.0, tool_calls=1, text_chars=0))
    store.enqueue(_turn("two", 110.0, tool_calls=0, text_chars=40))
    store.close()

    (row,) = store.in_loop_text_only_by_model()
    assert row["in_loop_turns"] == 1
    assert row["text_only_turns"] == 1


def test_two_sessions_never_chain_into_one_another(store) -> None:
    store.enqueue(_turn("a_one", 100.0, session="a", tool_calls=1, text_chars=0))
    store.enqueue(_turn("b_one", 101.0, session="b", tool_calls=None, text_chars=40))
    store.close()

    assert store.in_loop_text_only_by_model() == []


def test_a_subagents_turns_are_their_own_thread(store) -> None:
    """A subagent speaks in its own transcript; its turns interleave."""

    store.enqueue(_turn("main_one", 100.0, tool_calls=1, text_chars=0))
    store.enqueue(_turn("sub_one", 101.0, agent="agent-a", tool_calls=1, text_chars=0))
    store.enqueue(
        _turn("sub_two", 102.0, agent="agent-a", tool_calls=None, text_chars=9)
    )
    store.enqueue(_turn("main_two", 103.0, tool_calls=None, text_chars=70))
    store.close()

    (row,) = store.in_loop_text_only_by_model()
    # main_one -> main_two is a chain; sub_one -> sub_two is another. The
    # interleaving does not make main_two follow the subagent's tool call.
    assert row["in_loop_turns"] == 2
    assert row["text_only_turns"] == 2


def test_a_failed_request_stays_in_the_sequence_but_is_not_counted(store) -> None:
    """Counting the turn after a failure as in-loop would skip a turn."""

    store.enqueue(_turn("one", 100.0, tool_calls=1, text_chars=0))
    store.enqueue(_turn("failed", 110.0, tool_calls=None, text_chars=0, status="error"))
    store.enqueue(_turn("three", 120.0, tool_calls=None, text_chars=80))
    store.close()

    assert store.in_loop_text_only_by_model() == []


def test_a_turn_whose_text_was_never_measured_is_counted_apart(store) -> None:
    """Unknown is not "no text"; it is inside the denominator and nothing else."""

    store.enqueue(_turn("one", 100.0, tool_calls=1, text_chars=0))
    store.enqueue(_turn("two", 110.0, tool_calls=None, text_chars=None))
    store.close()

    (row,) = store.in_loop_text_only_by_model()
    assert row["in_loop_turns"] == 1
    assert row["text_only_turns"] == 0
    assert row["unmeasured_turns"] == 1


def test_rows_without_a_session_are_outside_the_population(store) -> None:
    """No client-stated session id means no sequence to read the turn against."""

    store.enqueue(_turn("one", 100.0, session=None, tool_calls=1, text_chars=0))
    store.enqueue(_turn("two", 110.0, session=None, tool_calls=None, text_chars=50))
    store.close()

    assert store.in_loop_text_only_by_model() == []


def test_a_local_answer_breaks_the_chain_between_two_model_turns(store) -> None:
    """A local answer is a turn of its own; the model turn after it is new."""

    store.enqueue(_turn("one", 100.0, tool_calls=1, text_chars=0))
    store.enqueue(
        _turn(
            "local",
            110.0,
            tool_calls=None,
            text_chars=0,
            provider=None,
            model=None,
        )
    )
    store.enqueue(_turn("three", 120.0, tool_calls=None, text_chars=80))
    store.close()

    assert store.in_loop_text_only_by_model() == []


def test_each_model_is_counted_on_its_own_rows(store) -> None:
    store.enqueue(_turn("one", 100.0, model="m1", tool_calls=1, text_chars=0))
    store.enqueue(_turn("two", 110.0, model="m1", tool_calls=None, text_chars=40))
    store.enqueue(_turn("three", 120.0, model="m2", tool_calls=1, text_chars=0))
    store.enqueue(_turn("four", 130.0, model="m2", tool_calls=1, text_chars=0))
    store.close()

    counts = _count(store)
    assert counts["p1/m1"]["text_only_turns"] == 1
    assert counts["p1/m2"] == {
        "model_ref": "p1/m2",
        "in_loop_turns": 1,
        "text_only_turns": 0,
        "unmeasured_turns": 0,
    }


def test_the_window_is_honoured(store) -> None:
    now = time.time()
    store.enqueue(_turn("old_one", now - 10 * 86_400, tool_calls=1, text_chars=0))
    store.enqueue(_turn("old_two", now - 10 * 86_400 + 10, text_chars=40))
    store.enqueue(_turn("new_one", now - 60, tool_calls=1, text_chars=0))
    store.enqueue(_turn("new_two", now - 30, text_chars=40))
    store.close()

    (row,) = store.in_loop_text_only_by_model(since=now - 86_400)
    assert row["in_loop_turns"] == 1
    assert row["text_only_turns"] == 1


def test_the_stops_are_ordered_first(store) -> None:
    """The page lists models; the ones that stop most shall be findable."""

    store.enqueue(_turn("a_one", 100.0, model="quiet", tool_calls=1, text_chars=0))
    store.enqueue(_turn("a_two", 110.0, model="quiet", tool_calls=1, text_chars=0))
    store.enqueue(_turn("b_one", 120.0, model="stopper", tool_calls=1, text_chars=0))
    store.enqueue(_turn("b_two", 130.0, model="stopper", tool_calls=None, text_chars=5))
    store.close()

    rows = store.in_loop_text_only_by_model()
    assert [row["model_ref"] for row in rows] == ["p1/stopper", "p1/quiet"]
