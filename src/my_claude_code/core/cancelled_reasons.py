"""Why a cancelled request was cancelled, derived from what is already stored.

``requests.status`` has exactly three values -- ``success``, ``error``,
``cancelled`` -- and "cancelled" is the only one that answers a question nobody
asked. It means the consumer stopped reading before the stream finished, and
that covers four genuinely different events: the client's own idle watchdog
firing while MCC had produced nothing readable, the same watchdog firing after
MCC had committed a response and then gone quiet, the user pressing Esc part of
the way through an answer, and MCC itself restarting under an open stream.

All four are derivable from columns the log has carried for a long time, so
this module is a pure projection: no new capture, no new column, no migration,
and every historical row classifies exactly as a row written today would.

The one input that is not on the request row is the set of *restart
boundaries* -- the ``server_sessions.last_seen_at`` values that were followed
by a later session. The caller supplies them (there are a few hundred, and the
store caches them); passing an empty sequence simply disables that one label,
which is the honest answer when the session history is unavailable.
"""

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from typing import Any

#: The status this module has anything to say about.
CANCELLED_STATUS = "cancelled"

#: How close to a restart boundary a request has to end to be blamed on it.
#:
#: Five seconds, the same window the investigation measured: 54 of 781
#: interrupted requests ended within +/-5 s of a session end against a 0.33%
#: base rate for successes, a ~21x enrichment. Widening it to 30 s moved the
#: count by 5 and started catching coincidences.
RESTART_WINDOW_SECONDS = 5.0

#: When a first frame counts as "too late to have helped".
#:
#: ``ttft_ms`` is the first frame of *any* kind, not the first content, so a row
#: whose first frame landed at 95% of its own duration produced nothing the
#: client could read in time -- the frame and the hang-up are the same instant.
#: This is the first investigation's own silence rule; the second pass asked
#: only that it not be used *alone*, because on its own it misses the rows that
#: committed early and then went quiet. Those are the ones
#: ``COMMITTED_THEN_SILENT`` names.
SILENT_FRACTION = 0.95

CLIENT_GAVE_UP_WAITING = "client_gave_up_waiting"
COMMITTED_THEN_SILENT = "committed_then_silent"
STOPPED_MID_ANSWER = "stopped_mid_answer"
SERVER_RESTART = "server_restart"

#: Every value :func:`classify_cancelled` can return, in precedence order.
CANCELLED_SUB_LABELS: tuple[str, ...] = (
    SERVER_RESTART,
    STOPPED_MID_ANSWER,
    CLIENT_GAVE_UP_WAITING,
    COMMITTED_THEN_SILENT,
)

#: What the chip says.
CANCELLED_SUB_LABEL_TEXT: dict[str, str] = {
    SERVER_RESTART: "server restart",
    STOPPED_MID_ANSWER: "stopped mid-answer",
    CLIENT_GAVE_UP_WAITING: "client gave up waiting",
    COMMITTED_THEN_SILENT: "committed, then silent",
}

#: One sentence per label, written for somebody who did not read the code.
CANCELLED_SUB_LABEL_EXPLANATION: dict[str, str] = {
    SERVER_RESTART: (
        "MCC stopped or restarted while this stream was still open, so the "
        "stream was cut at the shutdown drain deadline rather than by the "
        "client."
    ),
    STOPPED_MID_ANSWER: (
        "Part of the answer had already been delivered when the connection "
        "closed - normally the user stopping the stream, or the client giving "
        "up during a long gap between chunks."
    ),
    CLIENT_GAVE_UP_WAITING: (
        "Nothing the client could read ever arrived: MCC was still working "
        "through the route when the client's own idle timer ended the request."
    ),
    COMMITTED_THEN_SILENT: (
        "MCC had sent the start of the message and then nothing more arrived "
        "from the model; the client's own idle timer ended it."
    ),
}


def content_chars(output_chars: int | None, thinking_chars: int | None) -> int:
    """Characters this request actually put on the client's screen.

    Reasoning counts. A turn that streamed thinking and then stopped did reach
    the user -- calling it "committed, then silent" would be a false sentence
    about a request the client watched moving.
    """

    return int(output_chars or 0) + int(thinking_chars or 0)


def restart_boundaries(sessions: Sequence[tuple[float, float]]) -> tuple[float, ...]:
    """Session ends that a later session followed, sorted.

    ``sessions`` is ``(started_at, last_seen_at)`` per row. The newest session's
    end is not a boundary: it is either still running or simply where the log
    stops, and blaming a stream on it would turn "the log ends here" into "the
    server restarted".

    A one-second slack on the comparison is deliberate: a restart writes the new
    session's ``started_at`` and stops updating the old one's heartbeat at
    almost the same instant, and which of the two lands first is not something
    the writer promises.
    """

    starts = sorted(float(start) for start, _ in sessions)
    ends: list[float] = []
    for _, last_seen in sessions:
        end = float(last_seen)
        # A boundary needs a session that started after this one stopped being
        # heard from. ``bisect_right`` on ``end - slack`` is the first such
        # start; a session's own start is at least a second earlier than its own
        # end and so is never the one found.
        if bisect_right(starts, end - 1.0) < len(starts):
            ends.append(end)
    ends.sort()
    return tuple(ends)


def _ended_at_restart(
    ts_epoch: float | None,
    duration_ms: float | None,
    boundaries: Sequence[float],
) -> bool:
    if not boundaries or ts_epoch is None or duration_ms is None:
        # NULL duration is "not measured", never "ended at zero": a row whose
        # end time is unknown cannot be placed next to a restart, so it is not.
        return False
    end = float(ts_epoch) + float(duration_ms) / 1000.0
    index = bisect_left(boundaries, end - RESTART_WINDOW_SECONDS)
    return index < len(boundaries) and boundaries[index] <= end + RESTART_WINDOW_SECONDS


def classify_cancelled(
    *,
    status: str | None,
    ts_epoch: float | None = None,
    ttft_ms: float | None = None,
    duration_ms: float | None = None,
    output_chars: int | None = None,
    thinking_chars: int | None = None,
    boundaries: Sequence[float] = (),
) -> str | None:
    """Which of the four things happened, or ``None`` if the row is not cancelled.

    Precedence, highest first -- a request can satisfy two rules at once and the
    order below is what decides:

    1. ``server_restart`` -- the request ended within
       :data:`RESTART_WINDOW_SECONDS` of a restart boundary. It wins outright:
       whatever the stream had managed to deliver, it was MCC that ended it,
       and that is the only one of the four the operator can act on.
    2. ``stopped_mid_answer`` -- output or thinking characters were delivered.
    3. ``client_gave_up_waiting`` -- nothing readable was delivered and there was
       no first frame at all, or the first frame arrived at
       :data:`SILENT_FRACTION` of the request's own duration, i.e. at the
       instant the client had already gone.
    4. ``committed_then_silent`` -- the remainder: a first frame arrived with
       time to spare and no content ever followed it.

    The four are exhaustive over cancelled rows, so a cancelled row always gets
    exactly one label and the breakdown always sums to the Cancelled card.
    """

    if status != CANCELLED_STATUS:
        return None
    if _ended_at_restart(ts_epoch, duration_ms, boundaries):
        return SERVER_RESTART
    if content_chars(output_chars, thinking_chars) > 0:
        return STOPPED_MID_ANSWER
    if ttft_ms is None:
        return CLIENT_GAVE_UP_WAITING
    if (
        duration_ms is not None
        and float(duration_ms) > 0
        and float(ttft_ms) >= SILENT_FRACTION * float(duration_ms)
    ):
        return CLIENT_GAVE_UP_WAITING
    return COMMITTED_THEN_SILENT


#: The same four rules, in SQL, for the questions Python cannot answer a row at
#: a time: the Analytics breakdown (a GROUP BY over the whole cancelled
#: population) and the list filter (which has to be part of the ``WHERE`` or the
#: pager's ``LIMIT``/``OFFSET`` would page over the wrong rows).
#:
#: Written against ``server_sessions`` directly rather than against a list of
#: boundaries pasted into the SQL: 311 sessions would have meant a 311-branch
#: ``OR``, and SQLite serves the inner lookup from
#: ``idx_server_sessions_started`` as a covering index. Measured on a 6.3 GB
#: log, 1,574 cancelled rows all time: the breakdown 0.438 s, a 25-row filtered
#: page 0.40 s, both still seeking ``idx_requests_status``.
#:
#: This and :func:`classify_cancelled` are two spellings of one definition, and
#: ``test_the_sql_and_python_classifiers_agree`` is what keeps them one.
_RESTART_SQL = (
    "({t}.duration_ms IS NOT NULL AND EXISTS ("
    " SELECT 1 FROM server_sessions s"
    " WHERE abs(s.last_seen_at - ({t}.ts_epoch + {t}.duration_ms / 1000.0))"
    f" <= {RESTART_WINDOW_SECONDS}"
    " AND EXISTS (SELECT 1 FROM server_sessions s2"
    " WHERE s2.started_at > s.last_seen_at - 1.0)))"
)
_CONTENT_SQL = "(COALESCE({t}.output_chars, 0) + COALESCE({t}.thinking_chars, 0))"
_GAVE_UP_SQL = (
    "({t}.ttft_ms IS NULL OR ({t}.duration_ms IS NOT NULL AND {t}.duration_ms > 0"
    f" AND {{t}}.ttft_ms >= {SILENT_FRACTION} * {{t}}.duration_ms))"
)


def sub_label_case_sql(table: str = "requests") -> str:
    """The sub-label of a cancelled row, as a SQL expression.

    Says nothing about ``status``: the caller is expected to have narrowed to
    ``status = 'cancelled'`` already (that is the indexed part), and a
    ``CASE`` that also had to test the status would return a label for rows
    that have none.
    """

    restart = _RESTART_SQL.format(t=table)
    content = _CONTENT_SQL.format(t=table)
    gave_up = _GAVE_UP_SQL.format(t=table)
    return (
        f"CASE WHEN {restart} THEN '{SERVER_RESTART}'"
        f" WHEN {content} > 0 THEN '{STOPPED_MID_ANSWER}'"
        f" WHEN {gave_up} THEN '{CLIENT_GAVE_UP_WAITING}'"
        f" ELSE '{COMMITTED_THEN_SILENT}' END"
    )


#: How a sub-label travels in the ``status`` filter.
#:
#: ``status=cancelled:server_restart`` rather than a second query parameter:
#: every existing URL keeps its meaning, the three old values are untouched, and
#: a saved link to ``status=cancelled`` still selects all four.
STATUS_SUB_LABEL_SEPARATOR = ":"


def split_status_filter(status: str | None) -> tuple[str | None, str | None]:
    """``"cancelled:server_restart"`` -> ``("cancelled", "server_restart")``.

    Anything that is not a recognised sub-label comes back untouched, so an
    unknown value is rejected by the caller's existing status validation rather
    than silently becoming a filter nobody asked for.
    """

    if not status or STATUS_SUB_LABEL_SEPARATOR not in status:
        return status, None
    head, _, tail = status.partition(STATUS_SUB_LABEL_SEPARATOR)
    if head != CANCELLED_STATUS or tail not in CANCELLED_SUB_LABELS:
        return status, None
    return head, tail


#: The three statuses and the cancelled sub-labels, old ones first and
#: unchanged. ``success_reasons.REQUEST_STATUS_FILTER_VALUES`` appends the two
#: success sub-labels and is what the routes validate against.
STATUS_FILTER_VALUES: tuple[str, ...] = (
    "success",
    "error",
    CANCELLED_STATUS,
    *(
        f"{CANCELLED_STATUS}{STATUS_SUB_LABEL_SEPARATOR}{label}"
        for label in CANCELLED_SUB_LABELS
    ),
)


def classify_row(row: dict[str, Any], boundaries: Sequence[float] = ()) -> str | None:
    """:func:`classify_cancelled` over a request row dict, by column name."""

    return classify_cancelled(
        status=row.get("status"),
        ts_epoch=row.get("ts_epoch"),
        ttft_ms=row.get("ttft_ms"),
        duration_ms=row.get("duration_ms"),
        output_chars=row.get("output_chars"),
        thinking_chars=row.get("thinking_chars"),
        boundaries=boundaries,
    )
