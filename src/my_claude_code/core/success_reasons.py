"""What a successful request that produced no answer produced instead.

``requests.status = 'success'`` means the client received a valid message,
and that stays true: the status is about what reached the client, not about
whether the message said anything. But a small share of successes carry no
answer at all -- no text and no tool call -- and the one word could not tell
them from the rest. Measured on a real log over 14 days, 349 of 116,679
successes (0.30%) had neither. 148 of those were MCC's own local answers
(``suggestion_mode_skip``), empty by design. The other 201 were upstream turns
that ended with nothing the user could use, and 199 of those 201 carried
reasoning and nothing else.

This module names those two shapes, the way :mod:`cancelled_reasons` names
the four kinds of cancellation. It is a pure projection of stored columns: no
new capture, no new column, no migration, the status keeps its three values,
every rollup and total is unchanged, and a historical row classifies exactly
as a row written today would.

The definition, in full. A ``success`` row has **no answer** when all of:

* ``COALESCE(output_chars, 0) = 0`` -- no text reached the client;
* ``COALESCE(tool_call_count, 0) = 0`` -- no tool call either (the column is
  written as NULL, never 0, when a turn called no tool);
* ``optimization IS NULL`` -- it is not one of MCC's own local answers.

``is_local`` is not consulted: it is computed from ``optimization`` (a local
answer is ``provider IS NULL AND optimization IS NOT NULL``), so it adds
nothing, and on a log whose one-time ``is_local`` backfill has not finished it
still reads 0 for local rows that ``optimization`` already names.

A no-answer row is then:

* ``thought_only`` when ``thinking_chars > 0`` -- the model reasoned and the
  turn ended before any answer;
* ``empty`` when ``COALESCE(tokens_out, 0) = 0`` as well -- nothing at all.

A no-answer row with neither -- no reasoning counted but output tokens
reported -- gets no label. Those are the rows written before
``tool_call_count`` existed (the column is NULL on them, whether or not the
turn called a tool): 5,113 of them on the measured log, every one with output
tokens, which is what a tool-call turn with no text looks like. Calling them
"empty" would be a statement about tool calls nobody counted. On every row
written since, a turn with no text, no tool call and no reasoning reported no
output tokens either (138 of 138 on the measured log), so the guard changes
nothing about them.

Every other success -- and every row that is not a success -- has no label.
"""

from collections.abc import Mapping
from typing import Any

from my_claude_code.core.cancelled_reasons import (
    STATUS_FILTER_VALUES,
    STATUS_SUB_LABEL_SEPARATOR,
)

#: The status this module has anything to say about.
SUCCESS_STATUS = "success"

THOUGHT_ONLY = "thought_only"
EMPTY = "empty"

#: Every value :func:`classify_success` can return, in display order.
SUCCESS_SUB_LABELS: tuple[str, ...] = (THOUGHT_ONLY, EMPTY)

#: What the chip says.
SUCCESS_SUB_LABEL_TEXT: dict[str, str] = {
    THOUGHT_ONLY: "thought only",
    EMPTY: "empty",
}

#: One sentence per label, written for somebody who did not read the code.
SUCCESS_SUB_LABEL_EXPLANATION: dict[str, str] = {
    THOUGHT_ONLY: (
        "The model reasoned and the turn ended there: no text and no tool call "
        "reached the client, so it received a valid message with no answer in "
        "it."
    ),
    EMPTY: (
        "The turn ended cleanly with nothing in it: no text, no tool call, no "
        "reasoning and no output tokens."
    ),
}

#: Everything :func:`classify_success` reads off a request row.
SUCCESS_REASON_SOURCE_COLUMNS: tuple[str, ...] = (
    "status",
    "output_chars",
    "tool_call_count",
    "thinking_chars",
    "tokens_out",
    "optimization",
)


def classify_success(
    *,
    status: str | None,
    output_chars: int | None = None,
    tool_call_count: int | None = None,
    thinking_chars: int | None = None,
    tokens_out: int | None = None,
    optimization: str | None = None,
) -> str | None:
    """``thought_only``, ``empty``, or ``None`` for every other row."""

    if status != SUCCESS_STATUS:
        return None
    if int(output_chars or 0) != 0 or int(tool_call_count or 0) != 0:
        return None
    if optimization is not None:
        return None
    if int(thinking_chars or 0) > 0:
        return THOUGHT_ONLY
    if int(tokens_out or 0) == 0:
        return EMPTY
    return None


def classify_success_row(row: Mapping[str, Any]) -> str | None:
    """:func:`classify_success` over a request row, by column name."""

    return classify_success(
        status=row.get("status"),
        output_chars=row.get("output_chars"),
        tool_call_count=row.get("tool_call_count"),
        thinking_chars=row.get("thinking_chars"),
        tokens_out=row.get("tokens_out"),
        optimization=row.get("optimization"),
    )


def success_sub_label_case_sql(table: str = "requests") -> str:
    """The sub-label of a success row, as a SQL expression; NULL for the rest.

    Like :func:`cancelled_reasons.sub_label_case_sql` it says nothing about
    ``status``: the caller narrows to ``status = 'success'`` first (the
    indexed part). Unlike that one it is NULL for most rows, because most
    successes answered; ``CASE ... = ?`` is then NULL, which a ``WHERE``
    treats as false and a ``GROUP BY`` keeps as its own group -- the answered
    successes, which is what lets a breakdown say "of N successes".

    This and :func:`classify_success` are two spellings of one definition,
    and ``test_the_sql_and_python_classifiers_agree`` is what keeps them one.
    """

    t = table
    return (
        f"CASE WHEN COALESCE({t}.output_chars, 0) = 0"
        f" AND COALESCE({t}.tool_call_count, 0) = 0"
        f" AND {t}.optimization IS NULL THEN CASE"
        f" WHEN COALESCE({t}.thinking_chars, 0) > 0 THEN '{THOUGHT_ONLY}'"
        f" WHEN COALESCE({t}.tokens_out, 0) = 0 THEN '{EMPTY}'"
        " END END"
    )


def split_success_status_filter(status: str | None) -> tuple[str | None, str | None]:
    """``"success:thought_only"`` -> ``("success", "thought_only")``.

    Anything else comes back untouched, exactly as
    :func:`cancelled_reasons.split_status_filter` leaves what it does not
    recognise: an unknown value is rejected by the caller's status
    validation rather than silently becoming a filter nobody asked for.
    """

    if not status or STATUS_SUB_LABEL_SEPARATOR not in status:
        return status, None
    head, _, tail = status.partition(STATUS_SUB_LABEL_SEPARATOR)
    if head != SUCCESS_STATUS or tail not in SUCCESS_SUB_LABELS:
        return status, None
    return head, tail


#: The two success values, beside the cancelled ones and never instead of them.
SUCCESS_STATUS_FILTER_VALUES: tuple[str, ...] = tuple(
    f"{SUCCESS_STATUS}{STATUS_SUB_LABEL_SEPARATOR}{label}"
    for label in SUCCESS_SUB_LABELS
)

#: Every value the ``status`` filter accepts. The cancelled list comes first
#: and unchanged, so ``status=success`` still selects every success and every
#: saved link keeps its meaning.
REQUEST_STATUS_FILTER_VALUES: tuple[str, ...] = (
    *STATUS_FILTER_VALUES,
    *SUCCESS_STATUS_FILTER_VALUES,
)
