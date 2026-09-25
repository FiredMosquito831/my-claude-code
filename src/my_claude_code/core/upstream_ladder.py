"""Per-attempt record of every upstream try behind one route attempt.

One row in ``request_attempts`` used to carry one status, one credential and
one duration -- however many times the provider had actually knocked. A request
that spent 107 seconds making fifteen tries across three keys, seeing twelve
429s before a 502, was stored as a single ``upstream`` failure on key 0. In the
one hour where the live server log and the database overlap, 162 of the 178
upstream statuses the log recorded had no representation in the database at
all: the retry ladder was measured only by a DEBUG-level trace nobody ships
with.

This module is the holder that closes that gap. It mirrors
:mod:`my_claude_code.core.wire_capture` exactly: a mutable dataclass in a
``ContextVar``, installed once per request by the API layer, written by
``providers/`` through module-level functions that are a no-op outside a
tracked request. The holder receives already-extracted primitives -- an
``int | None`` status, a ``str`` body -- so ``core`` never imports an SDK, and
providers keep sole ownership of classifying their own failures.

Nothing here changes what the retry ladder *does*. Every function is a
recorder: it is called after the decision it describes has already been taken,
and it returns ``None``. Retry counts, backoff delays, credential health and
deadlines are identical with the recording switched off.

One deliberate deviation from a naive reading of "record every wait": the
provider-wide reactive block is *set* with the same number that is then slept
(``ProviderRateLimiter.extend_reactive_block`` runs immediately before
``asyncio.sleep`` with the same delay), so recording both would double-count
the same seconds and make ``time_sleeping_ms`` a lie. Time is recorded where it
is actually spent -- the backoff sleep and the limiter's own wait.

Proxy dials
-----------

A request on a provider with a proxy chain may dial several addresses inside
one attempt. ``request_attempts.proxy_label`` keeps only the last of them, so
the 09-16 park -- nine requests that hung after one 429 on their first rung --
could only be diagnosed by noticing *which* label survived. Each dial is now
kept on its attempt's ladder as a :class:`ProxyDial`: which address, where in
the tries it happened, how long the TCP connect and the tunnel handshake took
when a new connection was opened, and what the address answered. A dial that
is followed by another one is a *switch*, and it carries the answer that caused
it and the time MCC took to move on.

Dials are kept beside the tries, never among them. ``tries``, the try count,
``ladder_tries`` and everything that reads them -- the census, the root-cause
sentence, the stuck-request watchdog, the in-flight registry -- are exactly
what they were without a chain.
"""

import json
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from my_claude_code.core.diagnostics import redact_sensitive_error_text
from my_claude_code.core.proxy_attribution import current_proxy
from my_claude_code.core.wire_capture import redact_wire_value

# Per-try bound on the stored upstream body. Small on purpose: a ladder is
# bounded evidence, not a log, and 60 tries at this cap is 48 KB against the
# 8,000-char wire body already stored once per attempt.
# ``config.constants.REQUEST_LOG_LADDER_BODY_MAX_CHARS_DEFAULT`` mirrors this
# number for the settings layer -- ``core`` may not import ``config`` -- and
# ``tests/core/test_upstream_ladder.py`` pins the two together.
DEFAULT_LADDER_BODY_MAX_CHARS = 800

# Hard stop on the array. Past it, tries are dropped and counted, so the
# headline count stays truthful instead of silently flattening.
MAX_TRIES_PER_ATTEMPT = 60

# The same bound for dials. A chain's own switch limit keeps a real attempt far
# below it; the cap exists so a three-hundred-rung chain cannot grow a row
# without limit.
MAX_DIALS_PER_ATTEMPT = 60

# Spelled as a named escape so the linter's confusable-character check can stay
# on for the rest of the file. This really is the multiplication sign: the
# census reads "12x429" everywhere it is rendered.
_TIMES = "\N{MULTIPLICATION SIGN}"

#: Where one entry in the ladder came from.
#:
#: ``upstream``     -- a real call to the provider, with or without a status.
#: ``backoff``      -- MCC's own exponential sleep between two tries.
#: ``limiter_wait`` -- the provider-wide reactive block making this task wait.
#: ``bench``        -- the credential pool had nothing left to hand out.
#: ``probe``        -- a <=16-token diagnostic request to another model on the
#:                    same credential, asked only to decide whether a 429 was
#:                    about the model or about the key.
type TrySource = Literal["upstream", "backoff", "limiter_wait", "bench", "probe"]

# ``waited_ms`` on any other row is MCC's own backoff -- a sleep is normally
# back-filled onto the ``upstream`` try it followed, so keying the total on
# ``source == "backoff"`` would have summed to zero on every real ladder.
# ``limiter_wait`` is accounted separately: it is a provider-wide block, not
# this request's own retry schedule.
_LIMITER_SOURCE = "limiter_wait"


@dataclass(frozen=True, slots=True)
class LadderTry:
    """One upstream try, or one wait between two of them."""

    #: The credential in flight. ``-1`` is
    #: :data:`~my_claude_code.core.credential_attribution.NO_CREDENTIAL_INDEX`:
    #: the pool was fully benched and no key served this entry.
    key_index: int | None = None
    key_label: str | None = None
    #: The real upstream HTTP status, when there was one. ``None`` on a
    #: transport failure, on a success, and on a pure wait.
    status: int | None = None
    #: Transport/exception label, recorded only when ``status`` is ``None``.
    kind: str | None = None
    #: Canonical :class:`~my_claude_code.core.failures.FailureKind` name where
    #: the provider had already classified this failure, and the exception's
    #: own class name where it had not -- the retry frame runs before the
    #: provider's classifier, so a raw SDK error reaches it unnamed.
    error_kind: str | None = None
    #: What the provider *published*. ``None`` means it published none -- never
    #: an operator default substituted here.
    retry_after: float | None = None
    #: MCC's own sleep after this try, before the next one.
    waited_ms: float | None = None
    #: Time inside the provider call, this try.
    upstream_ms: float | None = None
    source: TrySource = "upstream"
    #: Redacted, capped raw upstream body.
    body: str | None = None
    body_truncated: bool = False
    #: The egress address this try went out through: ``host:port`` with any
    #: ``user:pass`` already removed, or the literal
    #: :data:`~my_claude_code.core.proxy_attribution.DIRECT_PROXY_LABEL` for a
    #: chain rung that deliberately uses none. ``None`` is "not measured" --
    #: which is every try on a provider with no chain, and it renders as an
    #: absent term rather than as a claim about the route.
    #:
    #: Appended last, defaulted, and every construction in ``src/`` and
    #: ``tests/`` is by keyword, so nothing that already builds one has to
    #: change. It costs no migration at all: the ladder is stored inside the
    #: attempt's existing ``params.ladder`` JSON.
    proxy: str | None = None
    #: The bounded, allow-listed head of the response behind this try --
    #: status, the transport headers, and the first bytes as hex -- recorded
    #: only where the body could not be decoded and the status would otherwise
    #: have been lost. ``None`` everywhere else, so every row a release before
    #: 7.20 wrote renders exactly as it did.
    response_head: Mapping[str, Any] | None = None
    #: The recovery rung that produced *this* try, when it is a retry a
    #: ``providers/recovery`` ladder rewrote the body for -- the rung's own
    #: ``kind`` (``responses_tool_name_length``, ``responses_tool_choice``).
    #:
    #: On the retry rather than on the try that failed, because the rung is
    #: chosen after that row is already written and because the operator's
    #: question is "why does this body differ from the one above it". ``None``
    #: on every ordinary try, so nothing any release before 7.23 wrote renders
    #: differently.
    recovery: str | None = None


@dataclass(frozen=True, slots=True)
class CredentialDecision:
    """What the pool did to one credential's health, and why."""

    key_index: int
    key_label: str | None = None
    #: ``"auth"`` | ``"rate_limit"`` | ``"quota"`` | ``None``. ``None`` means
    #: the failure was not credential-shaped -- or was a bare ``402`` naming no
    #: billing phrase -- and the health record did not move at all.
    cls: str | None = None
    #: Seconds the credential is benched for, read back out of the rotation
    #: engine after it decided -- never recomputed from tuning here.
    benched_for_s: float | None = None
    #: The upstream status behind the decision, when there was one.
    status: int | None = None
    #: What the provider published, if anything.
    retry_after: float | None = None
    #: The model the 429 was scoped to, when the bench is a (key, model) pair.
    model: str | None = None
    #: Seconds that pair is benched for. ``benched_for_s`` stays the
    #: *credential-wide* bench, which is None for a scoped 429 -- two
    #: different facts, and the modal shows both.
    model_benched_for_s: float | None = None
    reason: str = ""


@dataclass(slots=True)
class ProxyDial:
    """One address of a proxy chain, dialled for this attempt.

    Written at dial time, before anything is known about the dial, and filled
    in as the dial proceeds -- so a dial that never completes is still a row,
    which is the whole point: a try is only recorded when it *ends*.
    """

    #: The address, masked exactly as the try rows carry it.
    proxy: str | None
    #: How many rows of ``tries`` were already recorded when this dial began,
    #: which is where it sits in the ladder's order.
    at_try: int
    #: ``time.monotonic()`` at the dial. Never rendered; durations are.
    started: float
    #: The TCP connect to the proxy, when this dial opened a new connection.
    #: ``None`` when it reused one, or when nothing measured it.
    connect_ms: float | None = None
    #: The SOCKS5 negotiation or the HTTP ``CONNECT`` exchange that followed
    #: the connect, up to the moment the tunnel was ready.
    handshake_ms: float | None = None
    #: What the last upstream try on this address met: its census key -- the
    #: status, else the exception's name -- or ``None`` when it answered.
    verdict: str | None = None
    #: True when the last upstream try on this address was answered.
    answered: bool = False
    #: ``time.monotonic()`` when that last try was recorded.
    verdict_at: float | None = None


@dataclass(slots=True)
class AttemptLadder:
    """Every upstream try behind one route attempt."""

    tries: list[LadderTry] = field(default_factory=list)
    decisions: list[CredentialDecision] = field(default_factory=list)
    time_limiter_ms: float = 0.0
    tries_dropped: int = 0
    dials: list[ProxyDial] = field(default_factory=list)
    dials_dropped: int = 0
    #: ``time.monotonic()`` when the request moved on to its next attempt.
    #: ``None`` while this is the attempt in flight.
    closed_at: float | None = None


@dataclass(slots=True)
class LadderTrace:
    """Mutable per-request collector of upstream tries, keyed by chain index."""

    body_limit: int = DEFAULT_LADDER_BODY_MAX_CHARS
    current_attempt: int = 0
    ladders: dict[int, AttemptLadder] = field(default_factory=dict)

    def slot(self) -> AttemptLadder:
        """The ladder for the chain index currently in flight."""
        ladder = self.ladders.get(self.current_attempt)
        if ladder is None:
            ladder = AttemptLadder()
            self.ladders[self.current_attempt] = ladder
        return ladder

    def enter_attempt(self, attempt: int) -> None:
        """Move to chain index *attempt*, closing the one before it.

        The close time is what a dial left open at the end of an attempt is
        measured against. Announcing the attempt already in flight -- the
        handler and the executor both announce attempt 0 -- closes nothing.
        """
        if attempt != self.current_attempt:
            previous = self.ladders.get(self.current_attempt)
            if previous is not None and previous.closed_at is None:
                previous.closed_at = time.monotonic()
        self.current_attempt = attempt

    def record_dial(self, label: str | None) -> None:
        """Open a row for one proxy dial, before anything is known about it."""
        ladder = self.slot()
        if ladder.dials_dropped or len(ladder.dials) >= MAX_DIALS_PER_ATTEMPT:
            ladder.dials_dropped += 1
            return
        ladder.dials.append(
            ProxyDial(proxy=label, at_try=len(ladder.tries), started=time.monotonic())
        )

    def _open_dial(self) -> ProxyDial | None:
        """The dial the tries now being recorded belong to, if there is one.

        None once dials have been dropped: past the cap the last stored dial
        is no longer the one in flight, and attributing to it would be wrong.
        """
        ladder = self.ladders.get(self.current_attempt)
        if ladder is None or not ladder.dials or ladder.dials_dropped:
            return None
        return ladder.dials[-1]

    def record_dial_connect(self, milliseconds: float) -> None:
        """The open dial's TCP connect to its proxy completed."""
        dial = self._open_dial()
        if dial is None:
            return
        dial.connect_ms = milliseconds
        # A new connection starts a new handshake; an earlier connection's
        # figure would describe a tunnel this one does not use.
        dial.handshake_ms = None

    def record_dial_handshake(self, milliseconds: float) -> None:
        """The open dial's tunnel through its proxy is ready."""
        dial = self._open_dial()
        if dial is None:
            return
        dial.handshake_ms = milliseconds

    def record_try(self, entry: LadderTry) -> None:
        ladder = self.slot()
        if entry.source == "upstream":
            dial = self._open_dial()
            if dial is not None:
                code = _census_key(entry)
                dial.answered = code is None or (
                    entry.status is not None and entry.status < 400
                )
                dial.verdict = None if dial.answered else code
                dial.verdict_at = time.monotonic()
        if len(ladder.tries) >= MAX_TRIES_PER_ATTEMPT:
            ladder.tries_dropped += 1
            return
        ladder.tries.append(entry)

    def record_wait(self, milliseconds: float, *, source: TrySource) -> None:
        """Attribute a sleep to the try it followed, or record it on its own.

        Back-filling keeps the array one row per try in the ordinary case --
        try, sleep, try, sleep -- while a sleep with no preceding try still
        gets a row of its own, so the ordering survives either way.
        """
        ladder = self.slot()
        if ladder.tries and ladder.tries[-1].waited_ms is None:
            ladder.tries[-1] = replace(ladder.tries[-1], waited_ms=milliseconds)
            return
        self.record_try(LadderTry(source=source, waited_ms=milliseconds))

    def record_limiter_wait(self, milliseconds: float) -> None:
        """Record time spent parked on the provider-wide reactive block.

        Consecutive waits are coalesced into one row. ``_wait_for_reactive_block``
        loops until the deadline passes, so a long block produces many short
        waits with nothing in between: appending each one would spend the whole
        try budget on the wait and drop the statuses that matter.
        """
        ladder = self.slot()
        ladder.time_limiter_ms += milliseconds
        if ladder.tries and ladder.tries[-1].source == "limiter_wait":
            previous = ladder.tries[-1]
            ladder.tries[-1] = replace(
                previous, waited_ms=(previous.waited_ms or 0.0) + milliseconds
            )
            return
        self.record_try(LadderTry(source="limiter_wait", waited_ms=milliseconds))

    def record_decision(self, decision: CredentialDecision) -> None:
        self.slot().decisions.append(decision)


_LADDER: ContextVar[LadderTrace | None] = ContextVar(
    "fcc_upstream_ladder", default=None
)


def install_ladder_trace(
    body_limit: int = DEFAULT_LADDER_BODY_MAX_CHARS,
) -> LadderTrace:
    """Start recording upstream tries for the current request."""
    slot = LadderTrace(body_limit=body_limit)
    _LADDER.set(slot)
    return slot


@contextmanager
def paused_ladder() -> Iterator[None]:
    """Stop recording for the duration of a diagnostic probe.

    A probe goes through the same provider stack as a request, so the retry
    frame records its own ``upstream`` row for it -- and a probe that answers
    would then appear twice: once as a try the client never asked for, and
    once as the ``probe`` row that describes it. The caller records the single
    honest row itself, once it knows the verdict.
    """
    token = _LADDER.set(None)
    try:
        yield
    finally:
        _LADDER.reset(token)


def current_ladder() -> LadderTrace | None:
    """The ladder collector for the request in flight, if there is one."""
    return _LADDER.get()


#: The head of the response the *next* recorded try describes.
#:
#: The frame that reads a refusal body and the frame that records the try are
#: not the same frame -- the transport reads it, and the retry ladder in
#: ``providers/rate_limit.py`` records the row one ``except`` later. Rather
#: than widen a signature that every provider shares, the head is left in the
#: same kind of per-request slot the proxy label already uses
#: (``core/proxy_attribution.current_proxy``) and taken by the first try
#: recorded after it. It is *popped*, never merely read, so it can attach to
#: one row and only one.
_PENDING_HEAD: ContextVar[Mapping[str, Any] | None] = ContextVar(
    "fcc_response_head", default=None
)


def note_response_head(head: Mapping[str, Any] | None) -> None:
    """Leave the head of one response for the try about to be recorded."""

    _PENDING_HEAD.set(head)


#: The recovery rung the *next* recorded try is a retry for.
#:
#: The same slot-and-pop shape :data:`_PENDING_HEAD` uses, for the same reason:
#: the frame that chooses a rung (a provider's recovery loop) and the frame
#: that records the try (the retry ladder in ``providers/rate_limit.py``) are
#: not the same frame, and widening a signature every provider shares to carry
#: a value two rungs can produce would be a much larger change than the
#: feature needs.
_PENDING_RECOVERY: ContextVar[str | None] = ContextVar(
    "fcc_recovery_rung", default=None
)


def note_recovery_rung(kind: str | None) -> None:
    """Name the rung whose rewrite the next recorded try is carrying."""

    _PENDING_RECOVERY.set(kind or None)


def take_recovery_rung() -> str | None:
    """Take the pending rung name, clearing it."""

    kind = _PENDING_RECOVERY.get()
    if kind is not None:
        _PENDING_RECOVERY.set(None)
    return kind


def take_response_head() -> Mapping[str, Any] | None:
    """Take the pending head, clearing it."""

    head = _PENDING_HEAD.get()
    if head is not None:
        _PENDING_HEAD.set(None)
    return head


def redact_try_body(body: Any, limit: int) -> tuple[str | None, bool]:
    """Redact and cap one raw upstream body.

    Two passes, the project's existing ones: a parsed body goes through the
    wire scrubber (key-name pass plus value-shape pass), a bare string through
    the value-shape scrubber alone. No new redaction code lives here.
    """
    if body is None:
        return None, False
    if isinstance(body, str):
        text = body.strip()
        if not text:
            return None, False
        try:
            parsed = json.loads(text)
        except ValueError:
            cleaned = redact_sensitive_error_text(text)
        else:
            cleaned = json.dumps(redact_wire_value(parsed), default=str)
    elif isinstance(body, Mapping | list | tuple):
        cleaned = json.dumps(redact_wire_value(body), default=str)
    else:
        cleaned = redact_sensitive_error_text(str(body))
    if limit > 0 and len(cleaned) > limit:
        # Free text, not JSON that has to stay parseable: say it was cut and
        # cut it, rather than degrading the shape the way the wire summary does.
        return cleaned[:limit] + "…", True
    return cleaned, False


def record_upstream_try(
    *,
    key_index: int | None = None,
    key_label: str | None = None,
    status: int | None = None,
    kind: str | None = None,
    error_kind: str | None = None,
    retry_after: float | None = None,
    upstream_ms: float | None = None,
    source: TrySource = "upstream",
    body: Any = None,
    proxy: str | None = None,
) -> None:
    """Record one upstream try, if this request is being tracked.

    A no-op outside a tracked request, so providers exercised directly (unit
    tests, token counting, model discovery) need no special handling.

    ``proxy`` defaults to whatever the proxy pool last dispatched through, read
    from the same per-request slot the credential pool uses for its own choice.
    The retry frame that records most of these rows sits *below* the pool and
    has never been told which address it is on, and widening every provider
    signature to tell it would be a much larger change than the one the feature
    needs.
    """
    head = take_response_head()
    rung = take_recovery_rung()
    slot = _LADDER.get()
    if slot is None:
        return
    text, truncated = redact_try_body(body, slot.body_limit)
    slot.record_try(
        LadderTry(
            response_head=head,
            recovery=rung,
            proxy=proxy if proxy is not None else current_proxy(),
            key_index=key_index,
            key_label=key_label,
            status=status,
            kind=kind,
            error_kind=error_kind,
            retry_after=retry_after,
            upstream_ms=upstream_ms,
            source=source,
            body=text,
            body_truncated=truncated,
        )
    )


def record_proxy_dial(label: str | None) -> None:
    """Record that the proxy pool is about to dial *label*, if tracked.

    Installed as :func:`~my_claude_code.core.proxy_attribution.record_proxy`'s
    observer by the API layer, so it runs on the one call the pool already
    makes immediately before every dial. A no-op outside a tracked request and
    inside a paused one -- a diagnostic probe's dials are not this request's.
    """
    slot = _LADDER.get()
    if slot is None:
        return
    slot.record_dial(label)


def record_proxy_connect(seconds: float) -> None:
    """Record the TCP connect to the proxy the open dial is using, if tracked."""
    slot = _LADDER.get()
    if slot is None:
        return
    slot.record_dial_connect(seconds * 1000.0)


def record_proxy_handshake(seconds: float) -> None:
    """Record the tunnel handshake of the open dial's connection, if tracked."""
    slot = _LADDER.get()
    if slot is None:
        return
    slot.record_dial_handshake(seconds * 1000.0)


def record_upstream_wait(seconds: float, *, source: TrySource = "backoff") -> None:
    """Record a sleep MCC took between two tries, if tracked."""
    slot = _LADDER.get()
    if slot is None:
        return
    slot.record_wait(seconds * 1000.0, source=source)


def record_limiter_wait(seconds: float) -> None:
    """Record time parked on the provider-wide reactive block, if tracked."""
    slot = _LADDER.get()
    if slot is None:
        return
    slot.record_limiter_wait(seconds * 1000.0)


def record_credential_decision(
    *,
    key_index: int,
    key_label: str | None = None,
    cls: str | None = None,
    benched_for_s: float | None = None,
    status: int | None = None,
    retry_after: float | None = None,
    model: str | None = None,
    model_benched_for_s: float | None = None,
    reason: str = "",
) -> None:
    """Record what the pool decided about one credential's health, if tracked."""
    slot = _LADDER.get()
    if slot is None:
        return
    slot.record_decision(
        CredentialDecision(
            key_index=key_index,
            key_label=key_label,
            cls=cls,
            benched_for_s=benched_for_s,
            status=status,
            retry_after=retry_after,
            model=model,
            model_benched_for_s=model_benched_for_s,
            reason=reason,
        )
    )


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(float(value), 1)


def _census_key(entry: LadderTry) -> str | None:
    """The census bucket for one try: its status, else its exception name.

    ``None`` for the try that worked. A success has neither a status nor an
    exception name, and counting it as "unknown" would put a phantom row in
    every census that ends in one.
    """
    if entry.status is not None:
        return str(entry.status)
    return entry.kind or entry.error_kind


def _census_order(item: tuple[str, int]) -> tuple[int, int, str]:
    code, count = item
    # Descending count, then ascending code. Numeric statuses sort ahead of
    # exception names, so a census reads "12x429, 3x502, 1xReadTimeout".
    return (-count, int(code) if code.isdigit() else 10_000, code)


def _sum_optional(values: list[float | None]) -> float:
    return sum(value for value in values if value is not None)


def _elapsed_ms(start: float, end: float) -> float:
    return round(max(0.0, end - start) * 1000.0, 1)


def dial_rows(
    ladder: AttemptLadder, *, now: float | None = None
) -> list[dict[str, Any]]:
    """Render one attempt's proxy dials, in the order they were made.

    ``outcome`` is one of:

    * ``switched`` -- another dial followed this one. ``reason`` is what this
      address's last try met, when a try was recorded; ``switch_ms`` is the
      time from that answer (or from the dial, when there was none) to the
      next dial.
    * ``answered`` -- the last dial, and its last try was answered.
    * ``failed`` -- the last dial, its last try failed, and no switch
      followed. ``idle_ms`` is how long the attempt then went on without
      another dial.
    * ``dialing`` -- the last dial, and no try on it ever ended. ``elapsed_ms``
      is how long it was open when the attempt ended.

    Absent terms are "not measured", never zero: a reused connection has no
    connect time.
    """
    end = ladder.closed_at
    if end is None:
        end = time.monotonic() if now is None else now
    rows: list[dict[str, Any]] = []
    for position, dial in enumerate(ladder.dials):
        following = (
            ladder.dials[position + 1] if position + 1 < len(ladder.dials) else None
        )
        row: dict[str, Any] = {"at_try": dial.at_try}
        if dial.proxy is not None:
            row["proxy"] = dial.proxy
        if dial.connect_ms is not None:
            row["connect_ms"] = _rounded(dial.connect_ms)
        if dial.handshake_ms is not None:
            row["handshake_ms"] = _rounded(dial.handshake_ms)
        if dial.verdict_at is not None:
            row["verdict_ms"] = _elapsed_ms(dial.started, dial.verdict_at)
        settled = dial.started if dial.verdict_at is None else dial.verdict_at
        if following is not None:
            row["outcome"] = "switched"
            row["switch_ms"] = _elapsed_ms(settled, following.started)
        elif ladder.dials_dropped:
            # More dials followed this one than were kept. It was switched
            # away from; when, and to what, went with the dropped rows.
            row["outcome"] = "switched"
        elif dial.verdict_at is None:
            row["outcome"] = "dialing"
            row["elapsed_ms"] = _elapsed_ms(dial.started, end)
        elif dial.answered:
            row["outcome"] = "answered"
        else:
            row["outcome"] = "failed"
            row["idle_ms"] = _elapsed_ms(settled, end)
        if dial.verdict is not None:
            row["reason"] = dial.verdict
        rows.append(row)
    return rows


def ladder_payload(
    ladder: AttemptLadder, *, now: float | None = None
) -> dict[str, Any]:
    """Render one attempt's ladder into the JSON stored under ``params``.

    ``dials`` is present only when the attempt dialled through a proxy chain,
    so the payload of every other attempt is exactly what it always was.
    """
    upstream = [entry for entry in ladder.tries if entry.source == "upstream"]
    labels: dict[int, str] = {}
    for entry in upstream:
        if entry.key_index is not None and entry.key_label:
            labels[entry.key_index] = entry.key_label

    census: dict[str, int] = {}
    for entry in upstream:
        code = _census_key(entry)
        if code is not None:
            census[code] = census.get(code, 0) + 1

    tries: list[dict[str, Any]] = []
    for entry in ladder.tries:
        # Absent rather than null: a missing term is "not measured", and the
        # modal renders a dash for it instead of a reassuring zero.
        row: dict[str, Any] = {"source": entry.source} | {
            name: value
            for name, value in (
                ("key_index", entry.key_index),
                ("key_label", entry.key_label),
                ("status", entry.status),
                ("kind", entry.kind),
                ("error_kind", entry.error_kind),
                ("retry_after", entry.retry_after),
                ("waited_ms", _rounded(entry.waited_ms)),
                ("upstream_ms", _rounded(entry.upstream_ms)),
                ("proxy", entry.proxy),
                ("body", entry.body),
                ("response_head", entry.response_head),
                ("recovery", entry.recovery),
            )
            if value is not None
        }
        if entry.body_truncated:
            row["body_truncated"] = True
        tries.append(row)

    credentials = [
        {
            "key_index": decision.key_index,
            "key_label": decision.key_label or labels.get(decision.key_index),
            "class": decision.cls,
            "benched_for_s": _rounded(decision.benched_for_s),
            "status": decision.status,
            "retry_after": decision.retry_after,
            "reason": decision.reason,
        }
        # Absent rather than null, exactly like the try rows above: an
        # unscoped decision has no model and says so by omission.
        | {
            name: value
            for name, value in (
                ("model", decision.model),
                ("model_benched_for_s", _rounded(decision.model_benched_for_s)),
            )
            if value is not None
        }
        for decision in ladder.decisions
    ]

    keys = {entry.key_index for entry in upstream if entry.key_index is not None}
    # A probe is not a try the client asked for, so it stays out of ``tries``
    # -- but it is the whole reason a one-try ladder is worth showing, so the
    # count is published beside it and the dashboard's "nothing was hidden"
    # gate reads both.
    probes = sum(1 for entry in ladder.tries if entry.source == "probe")
    payload: dict[str, Any] = {
        "tries": tries,
        "summary": {
            # Upstream tries only. A backoff sleep and a limiter wait get their
            # own rows so the ordering survives, but neither is a try.
            "tries": len(upstream),
            "probes": probes,
            "statuses_by_code": dict(sorted(census.items(), key=_census_order)),
            "keys": len(keys),
            "time_upstream_ms": _rounded(
                _sum_optional([entry.upstream_ms for entry in ladder.tries])
            ),
            "time_sleeping_ms": _rounded(
                _sum_optional(
                    [
                        entry.waited_ms
                        for entry in ladder.tries
                        if entry.source != _LIMITER_SOURCE
                    ]
                )
            ),
            "time_limiter_ms": _rounded(ladder.time_limiter_ms),
            "tries_dropped": ladder.tries_dropped,
        },
        "credentials": credentials,
    }
    if ladder.dials:
        payload["dials"] = dial_rows(ladder, now=now)
    if ladder.dials_dropped:
        payload["dials_dropped"] = ladder.dials_dropped
    return payload


def ladder_proxy_label(payload: Mapping[str, Any]) -> str | None:
    """The address the *last* real try of this attempt went out through.

    Denormalised onto ``request_attempts.proxy_label`` exactly as
    ``ladder_tries`` is, so the analytics breakdown can group by address
    without scanning JSON. The last upstream row rather than the first: after a
    switch, the address that answered is the one the attempt's verdict belongs
    to, and the whole sequence is still in the ladder for anyone who wants it.

    ``None`` means "not measured", never "direct" -- Direct is a rung an
    operator chose and is stored as the literal ``"direct"``.
    """

    for row in reversed(list(payload.get("tries") or [])):
        if not isinstance(row, Mapping):
            continue
        if row.get("source") != "upstream":
            continue
        label = row.get("proxy")
        if label:
            return str(label)
    return None


def _seconds(milliseconds: float) -> str:
    """Whole seconds, floored.

    Floored rather than rounded so a rendered duration never claims more time
    than was measured: 107.5 seconds reads as 107s, and the sleep fraction it
    is compared against is floored the same way.
    """
    return f"{int(milliseconds // 1000)}s"


def _join(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _census_text(statuses: Mapping[str, int]) -> str:
    return ", ".join(
        f"{count}{_TIMES}{code}"
        for code, count in sorted(statuses.items(), key=_census_order)
    )


def format_status_census(counts: Mapping[str, int]) -> str:
    """Render a merged status census as an export cell: ``429x12, 502x3``.

    Code-first here, count-first in the root-cause sentence, because a column
    of these is read down the codes while a sentence is read along the counts.
    Both orders come out of the same measured census.
    """
    return ", ".join(
        f"{code}{_TIMES}{count}"
        for code, count in sorted(counts.items(), key=_census_order)
    )


def _quota_clause(entry: Mapping[str, Any]) -> str:
    """Say an exhausted balance in the words that name the fix.

    "key 2 benched 60s on 400" is true and sends the reader to the wrong page:
    nothing about the request, the model or the throttle explains it, and the
    only thing that clears it is a top-up. So the credits decision gets its own
    sentence rather than the shared benched/charged one.
    """
    label = entry.get("key_label") or entry.get("key_index")
    text = f"credits exhausted on key {label}"
    benched = entry.get("benched_for_s")
    if benched:
        text += f" -- benched {float(benched):.0f}s"
    status = entry.get("status")
    if status is not None:
        text += f" on {status}"
    return text


def _charged_clause(credentials: list[Mapping[str, Any]]) -> str:
    """Name the credentials whose health actually moved, and by how much."""
    groups: dict[tuple[Any, Any, Any, Any, Any], list[int]] = {}
    quota: list[Mapping[str, Any]] = []
    for entry in credentials:
        if entry.get("class") is None:
            continue
        if entry.get("class") == "quota":
            quota.append(entry)
            continue
        key = (
            entry.get("status"),
            entry.get("benched_for_s"),
            entry.get("retry_after"),
            entry.get("model"),
            entry.get("model_benched_for_s"),
        )
        groups.setdefault(key, []).append(int(entry["key_index"]))
    parts: list[str] = [_quota_clause(entry) for entry in quota]
    # A credits bench is whole-key by construction, so the "no key charged"
    # footnote -- which exists to stop a (key, model) bench reading as a
    # charged key -- must not fire when one is present.
    scoped_only = not quota
    for (status, benched, retry_after, model, model_benched), indexes in groups.items():
        noun = "keys" if len(indexes) > 1 else "key"
        text = f"{noun} {_join([str(index) for index in sorted(indexes)])}"
        if benched is None and model is not None and model_benched is not None:
            # A (key, model) bench: the key is still healthy for every other
            # model, so saying "charged" would overstate what the pool did.
            text += f" benched {float(model_benched):.0f}s for {model}"
        else:
            scoped_only = False
            text += " charged" if benched is None else f" benched {float(benched):.0f}s"
        if status is not None:
            text += f" on {status}"
        text += (
            " (no Retry-After)"
            if retry_after is None
            else f" (Retry-After {float(retry_after):g}s)"
        )
        parts.append(text)
    if parts and scoped_only:
        parts.append("no key charged")
    return "; ".join(parts)


def _uncharged_clause(credentials: list[Mapping[str, Any]]) -> str:
    """Name the credentials the pool deliberately left alone, with its reason."""
    groups: dict[str, list[int]] = {}
    for entry in credentials:
        if entry.get("class") is not None:
            continue
        reason = str(entry.get("reason") or "not credential-shaped")
        groups.setdefault(reason, []).append(int(entry["key_index"]))
    parts: list[str] = []
    for reason, indexes in groups.items():
        noun = "keys" if len(indexes) > 1 else "key"
        parts.append(
            f"{noun} {_join([str(index) for index in sorted(indexes)])}"
            f" not charged ({reason})"
        )
    return "; ".join(parts)


def ladder_root_cause(
    payload: Mapping[str, Any],
    *,
    attempt_error_kind: str | None = None,
    attempt_duration_ms: float | None = None,
) -> str:
    """Say in one sentence what this attempt actually met upstream.

    Every number comes out of ``summary`` or ``credentials``; nothing is
    inferred from ``error_kind`` alone, and a component that was not measured
    drops its clause rather than defaulting to zero.
    """
    summary = payload.get("summary") or {}
    tries = int(summary.get("tries") or 0)
    if tries <= 1:
        # Nothing is hidden: the attempt's own ``error_message`` already says
        # everything there is to say, and a second sentence repeating it would
        # imply a ladder that does not exist.
        return ""
    statuses = summary.get("statuses_by_code") or {}
    census = _census_text(statuses)
    sleeping = float(summary.get("time_sleeping_ms") or 0.0)
    limiter = float(summary.get("time_limiter_ms") or 0.0)

    if (
        attempt_error_kind == "timeout"
        and attempt_duration_ms
        and (sleeping + limiter) >= 0.5 * float(attempt_duration_ms)
    ):
        # The row says the model produced no output within the deadline, which
        # is true and useless: the model was never handed an accepted request.
        head = (
            f"deadline reached after {_seconds(sleeping + limiter)} of backoff"
            " — the model never received an accepted request"
        )
        return f"{head}: {census}" if census else head

    credentials = list(payload.get("credentials") or [])
    keys = int(summary.get("keys") or 0)
    per_key: dict[int, int] = {}
    for row in payload.get("tries") or []:
        if row.get("source") != "upstream":
            continue
        index = row.get("key_index")
        if index is None:
            continue
        per_key[int(index)] = per_key.get(int(index), 0) + 1
    counts = set(per_key.values())
    if keys and len(counts) == 1:
        head = f"{keys} keys {_TIMES} {counts.pop()} tries"
    elif keys:
        head = f"{tries} tries across {keys} keys"
    else:
        head = f"{tries} tries"

    clauses = [f"{head}: {census}" if census else head]
    if sleeping > 0 and attempt_duration_ms:
        clauses[0] += (
            f" — {_seconds(sleeping)} of the"
            f" {_seconds(float(attempt_duration_ms))} were MCC backoff sleeps"
        )
    elif sleeping > 0:
        clauses[0] += f" — {_seconds(sleeping)} were MCC backoff sleeps"
    if limiter > 0:
        clauses.append(
            f"{_seconds(limiter)} were spent on the provider's reactive block"
        )
    charged = _charged_clause(credentials)
    if charged:
        clauses.append(charged)
    uncharged = _uncharged_clause(credentials)
    if uncharged:
        clauses.append(uncharged)
    return "; ".join(clauses)
