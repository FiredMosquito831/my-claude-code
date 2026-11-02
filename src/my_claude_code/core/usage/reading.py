"""What one provider answered when MCC asked it for its usage, and what it means.

The data model of `specs/INVESTIGATION-USAGE-AND-CREDITS.md` §4, in code, and
the one place the four states are decided (plus the one distinction §4.2 does
not name: a provider that answers *"there is nothing available here"*).

THE ONE RULE (§4.1), inherited from
:mod:`my_claude_code.providers.anthropic_oauth.rate_limit_headers`:

    Every field is a string the provider sent, or the literal words "not
    available" / "not yet observed" -- with the instant it was observed.

So every figure here is a **string**. Not a float: a balance is money, and a
rounding error on a balance is a wrong invoice (§4.5). The adapters decode JSON
with ``parse_float=str`` and ``parse_int=str`` so a number arrives as the exact
text the provider wrote, and nothing downstream re-derives it.

TWO BLOCKS, NEVER ONE (§4.3). A *window* is a percentage of an allowance that
resets; a *balance* is money that does not. They are separate tuples with
separate headings and the page never adds them together, because adding them
would be a new instance of the defect Part IV.9 exists to prevent.

THE STATES, and why there are five words for four states:

``measured``      a reading arrived inside the freshness bound (§4.2 state 1)
``stale``         a reading arrived at T and nothing has been read since (2)
``not_measured``  there is a mechanism and nothing has come through it yet
``unsupported``   this provider publishes nothing MCC can read (3)
``failed``        the read did not happen, and the reason is carried (4)
``unavailable``   the read **succeeded** and the provider's own answer is
                  "there is nothing available here" -- an expired plan, an
                  account that is not enabled for inference. It is NOT state 4:
                  the call worked, the provider answered, and what it said is
                  a fact about the account rather than about the read. It is
                  not state 3 either, because state 3 is permanent and this is
                  a reading with a timestamp that the next real reading will
                  replace. The user's own instruction (2026-11-01) is that an
                  expired plan "reads as a real 'not available', not an error",
                  and collapsing the two would be the IV.16 mistake -- "we know
                  there is nothing left" and "we could not ask" must never look
                  alike.

A provider with no mechanism at all is ``unsupported`` and can never be
anything else: :func:`state_of` checks that **before** it looks at the store, so
a stray reading can never promote it.
"""

import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any

__all__ = [
    "MAX_BACKOFF_DOUBLINGS",
    "MAX_REASON_CHARS",
    "READINGS",
    "Balance",
    "ReadRecord",
    "Reading",
    "UsageCredential",
    "UsageReadStore",
    "UsageRequest",
    "UsageUnreadable",
    "Window",
    "backoff_seconds",
    "freshest_observation",
    "state_of",
]

#: How long a provider's own reason may be on the page. A balance route's error
#: is one sentence; an edge that echoes a whole HTML page into an error field
#: must not become an unbounded string on a card. The same instinct as
#: ``core.usage_capture.MAX_VALUE_CHARS``, one layer up.
MAX_REASON_CHARS = 300


def _bounded(text: str) -> str:
    """One provider-supplied string, trimmed and capped, never empty."""

    cleaned = " ".join(str(text).split())
    if len(cleaned) <= MAX_REASON_CHARS:
        return cleaned
    return cleaned[: MAX_REASON_CHARS - 1] + "…"


@dataclass(frozen=True, slots=True)
class Window:
    """One allowance window: a percentage (or a used/limit pair) and a reset.

    A window never renders as money. ``unit`` is the provider's own measure --
    ``"percent"`` for a utilisation figure, ``"usd"`` for Command Code's 5-hour
    credit cap, which is an allowance that resets rather than a balance.

    **``used`` and ``remaining`` are two different providers' words for the
    same window**, and both are kept because a bar needs both: OpenRouter
    reports ``limit_remaining`` and Anthropic reports ``utilization``. Neither
    is ever computed from the other -- a provider that sends only one keeps
    only that one, and the renderer draws what it was given (§4.1: never a
    derived figure). ``remaining`` exists so a page that wants a percentage can
    show ``used``/``limit`` as one, and a page that wants a bar can draw
    ``remaining`` without inventing the complement of a number nobody sent.
    """

    id: str
    label: str
    used: str = ""
    limit: str = ""
    unit: str = ""
    resets_at: str = ""
    #: What the provider said is LEFT, where it said it (OpenRouter's
    #: ``limit_remaining``). Empty when it reported only utilisation.
    remaining: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "used": self.used,
            "limit": self.limit,
            "remaining": self.remaining,
            "unit": self.unit,
            "resets_at": self.resets_at,
        }


@dataclass(frozen=True, slots=True)
class Balance:
    """One cash figure, in the provider's own currency, with its own kind.

    ``kind`` separates the three things a provider reports under one heading:
    ``available`` (what can be spent), ``purchased`` (topped up) and
    ``promotional`` (granted). DeepSeek splits the last two on purpose and the
    page keeps them apart; summing them into one "balance" is the mistake §4.3
    names.
    """

    id: str
    label: str
    amount: str
    currency: str = ""
    kind: str = "available"

    def as_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "amount": self.amount,
            "currency": self.currency,
            "kind": self.kind,
        }


@dataclass(frozen=True, slots=True)
class Reading:
    """One successful answer from a provider's usage route.

    ``fetched_at`` is epoch seconds, stamped when the provider's bytes arrived
    -- not when the page rendered, and not when this was read back. It is the
    ``fetchedAt`` §3.9.4 asks for, and the page refuses to show a number
    without it.

    The adapters build one with :meth:`at`, which stamps the arrival time in
    the one place that decides it; a test builds one with the constructor and
    its own clock.
    """

    fetched_at: float
    windows: tuple[Window, ...] = ()
    balances: tuple[Balance, ...] = ()
    plan: str = ""
    #: ``False`` when the provider's answer was "there is nothing available
    #: here" -- an expired plan, an account not enabled. A reading with this
    #: unset is still a measurement; it is the *content* that is empty.
    available: bool = True
    #: The provider's own words for why nothing is available, bounded.
    unavailable_reason: str = ""
    #: Sentences the provider sent that are not a figure and not a refusal --
    #: a status word, a period end, a warning. Shown verbatim.
    notes: tuple[str, ...] = ()

    @classmethod
    def at(
        cls,
        fetched_at: float,
        *,
        windows: tuple[Window, ...] = (),
        balances: tuple[Balance, ...] = (),
        plan: str = "",
        available: bool = True,
        unavailable_reason: str = "",
        notes: tuple[str, ...] = (),
    ) -> Reading:
        """One reading, with the provider's own strings bounded once.

        A ``None`` reason and an over-long one are different problems and both
        end here: the first is absent (``""``), the second is capped.
        """

        return cls(
            fetched_at=fetched_at,
            windows=windows,
            balances=balances,
            plan=_bounded(plan) if plan else "",
            available=available,
            unavailable_reason=_bounded(unavailable_reason)
            if unavailable_reason
            else "",
            notes=tuple(_bounded(note) for note in notes if note),
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "fetched_at": self.fetched_at,
            "plan": self.plan,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "windows": [window.as_payload() for window in self.windows],
            "balances": [balance.as_payload() for balance in self.balances],
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class ReadRecord:
    """What the last attempt at one provider's route left behind.

    Two clocks and one reason, because the page has to answer three different
    questions: when did we last try, what did it say, and when may we try
    again. ``retry_at`` is the provider's own ``Retry-After`` translated into
    an instant -- never a guess, and ``None`` when the provider did not say.

    ``consecutive_failures`` is 8.6.0's, and it is what makes a 30-second
    automatic refresh safe: the poller backs off on THIS provider alone while
    it keeps failing, and one success puts it back on the operator's interval.
    A host that is down must not be asked every 30 seconds for ever.
    """

    attempted_at: float = 0.0
    status_code: int | None = None
    error: str = ""
    retry_at: float | None = None
    #: Set when the attempt never reached the host (no credential, no exit).
    #: Distinct from a failure the host answered, in the reader's words.
    not_sent: bool = False
    #: Failures since the last success, for this provider alone. Reset to 0 by
    #: a reading that arrived. The page turns it into a wait; the server keeps
    #: the count so two tabs cannot disagree about how far back off it is.
    consecutive_failures: int = 0


@dataclass(frozen=True, slots=True)
class UsageRequest:
    """One concrete GET, as an adapter plans it. Never carries a credential."""

    url: str
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class UsageCredential:
    """The credential one read goes out with.

    ``repr=False`` on the token field is load-bearing: a dataclass's generated
    ``repr`` puts every field in a traceback and a debug log, and a token in
    either is the one thing this feature must never do.
    """

    token: str = field(repr=False)
    #: The ``ChatGPT-Account-ID`` header's value for a subscription read; empty
    #: for an API-key provider, which has no account id to send.
    account_id: str = ""
    #: The masked label of the credential, for the provenance line. Never the
    #: value: ``mask_key_label``'s ``first4…last4`` or an account's own name.
    label: str = ""


class UsageUnreadable(Exception):
    """The host answered, and the answer was not the shape the row declares.

    Carries a sentence for the page, never a body: the route turns this into
    state 4 with the reason, which is the honest answer to "the call did
    happen and its answer was not what this row knows how to read".
    """


def state_of(
    *,
    supported: bool,
    observed_at: float | None,
    failure: ReadRecord | None,
    now: float,
    stale_after_seconds: float,
    available: bool = True,
) -> str:
    """One provider's state, decided once, for the chip and the sentence both.

    The order is the argument:

    1. **No mechanism** is checked first, so a stray reading can never promote
       a provider the declared table says publishes nothing (IV.16).
    2. **A failure newer than the observation** is state 4. A read that broke
       ten seconds ago is the fact the reader needs, even if a good number sat
       here an hour ago -- and the page still shows that number, with its age,
       under the failure's own sentence.
    3. **An observation** is measured or stale by age, and *unavailable* when
       the provider's answer was "nothing available here".
    4. **Nothing at all** is ``not_measured``: a point of capture exists and
       nothing has come through it yet -- which is a different fact from state
       3 and from state 4, and says so.

    ``observed_at`` and ``available`` are one provider's **freshest** answer,
    whichever mechanism carried it: a provider may report on its own traffic
    and through a declared read, and the card's chip describes the card, not
    one of its two halves. :func:`freshest_observation` is how the caller gets
    them from more than one source.
    """

    if not supported:
        return "unsupported"
    if failure is not None and (
        observed_at is None or failure.attempted_at >= observed_at
    ):
        return "failed"
    if observed_at is not None:
        if (now - observed_at) > stale_after_seconds:
            return "stale"
        return "measured" if available else "unavailable"
    return "not_measured"


def freshest_observation(
    *observations: tuple[float | None, bool],
) -> tuple[float | None, bool]:
    """The newest instant among several sources, and what it said.

    Each argument is ``(observed_at, available)``. A ``None`` instant is a
    source that has reported nothing. The answer is the newest one's instant
    and **its** availability, never an ``or`` across them: if the endpoint said
    "nothing available" ten seconds ago and a header carried a figure an hour
    ago, the provider is unavailable, because that is the most recent thing it
    said.
    """

    best_at: float | None = None
    best_available = True
    for observed_at, available in observations:
        if observed_at is None:
            continue
        if best_at is None or observed_at > best_at:
            best_at = observed_at
            best_available = available
    return best_at, best_available


class UsageReadStore:
    """The last reading and last attempt per provider, plus the read gate.

    In memory only: §4.5's verdict for PR 3. A restart blanks the readings,
    which is a cost accepted on purpose (the page then says "not read yet"
    rather than showing a number of unknown age), and PR 4 is where a small
    ``~/.mcc/usage_cache.json`` may be added.

    **The gate is what keeps a press cheap.** One read per provider per
    :data:`DEFAULT_INTERVAL_SECONDS`, a provider's own ``Retry-After``
    honoured as a deadline, and one read in flight at a time -- claimed
    atomically, under a plain lock held for the dictionary write only, so two
    page loads a millisecond apart make ONE request between them rather than
    two. That property is proved twice: end to end, at the fake host, in
    ``tests/api/test_admin_usage_reads.py::test_two_presses_inside_the_interval_make_one_request``
    and ``::test_a_concurrent_claim_is_refused_rather_than_duplicated``.
    """

    #: §5.5.3's per-provider minimum interval, floored at five minutes: below
    #: that nothing in the research justified a read (§3.6), and 30 minutes is
    #: the same bound the page already calls "stale".
    DEFAULT_INTERVAL_SECONDS = 30 * 60

    __slots__ = ("_in_flight", "_lock", "_readings", "_records")

    def __init__(self) -> None:
        self._readings: dict[str, Reading] = {}
        self._records: dict[str, ReadRecord] = {}
        self._in_flight: set[str] = set()
        self._lock = threading.Lock()

    # -- reads the route serves -------------------------------------------

    def reading_for(self, provider_id: str) -> Reading | None:
        with self._lock:
            return self._readings.get(provider_id)

    def record_for(self, provider_id: str) -> ReadRecord | None:
        with self._lock:
            return self._records.get(provider_id)

    def last_attempt_at(self, provider_id: str) -> float | None:
        record = self.record_for(provider_id)
        return (
            None if record is None or not record.attempted_at else record.attempted_at
        )

    # -- the gate ----------------------------------------------------------

    def claim(self, provider_id: str, *, now: float, interval: float) -> str:
        """Take the right to read, or say why not.

        Returns ``""`` when the caller owns the read and must call
        :meth:`finish` exactly once, or one of:

        ``"in_flight"``   another press is reading this provider right now
        ``"cooldown"``    the provider's own ``Retry-After`` has not elapsed
        ``"interval"``    a read happened less than ``interval`` ago

        The claim sets ``attempted_at`` immediately, which is what makes two
        concurrent presses one request: the second sees the first's claim
        rather than a stale "never read" time.
        """

        with self._lock:
            if provider_id in self._in_flight:
                return "in_flight"
            record = self._records.get(provider_id)
            # The provider's own deadline wins over our interval: a 429's
            # `Retry-After` is a fact about the host, not a preference.
            if (
                record is not None
                and record.retry_at is not None
                and now < record.retry_at
            ):
                return "cooldown"
            if (
                record is not None
                and record.attempted_at
                and (now - record.attempted_at) < interval
            ):
                return "interval"
            self._in_flight.add(provider_id)
            self._records[provider_id] = ReadRecord(
                attempted_at=now,
                status_code=record.status_code if record else None,
                retry_at=None,
                consecutive_failures=record.consecutive_failures if record else 0,
            )
            return ""

    def finish(
        self, provider_id: str, reading: Reading | None, record: ReadRecord
    ) -> None:
        """Close a claimed read: keep the reading, or keep the reason.

        A failed read never overwrites a reading -- "this host said nothing
        this time" must not erase what it said forty minutes ago -- which is
        why ``reading=None`` leaves the previous one in place with its own
        ``fetched_at``.

        **The failure streak is counted here, not by the caller.** One success
        resets it to zero; every attempt that produced no reading extends it,
        whichever way it failed. That is what the page's per-provider backoff
        is computed from, and keeping the count in one place is what stops two
        tabs from disagreeing about how far back off a broken host is.
        """

        with self._lock:
            previous = self._records.get(provider_id)
            streak = 0 if reading is not None else _streak_after(previous, record)
            self._in_flight.discard(provider_id)
            if reading is not None:
                self._readings[provider_id] = reading
            self._records[provider_id] = replace(record, consecutive_failures=streak)

    def reset(self) -> None:
        """Forget everything. Tests and a process restart only."""

        with self._lock:
            self._readings.clear()
            self._records.clear()
            self._in_flight.clear()


#: Process-wide, for the same reason ``core.usage_capture.OBSERVER`` is: the
#: admin route reads it while the provider instances sit inside a generation
#: the routes cannot reach. One process, one answer per provider.
READINGS = UsageReadStore()

#: The most consecutive failures the backoff arithmetic will consider. A
#: provider down for a week has the same wait as one down for an hour; the cap
#: is on the exponent, not on how long MCC keeps trying.
MAX_BACKOFF_DOUBLINGS = 8


def backoff_seconds(interval: float, consecutive_failures: int, *, cap: float) -> float:
    """How long to wait before asking this provider again, after failures.

    ``interval`` at zero failures -- the operator's own number, unmodified --
    then doubled once per consecutive failure up to :data:`MAX_BACKOFF_DOUBLINGS`
    and capped. One provider's failures never lengthen another's wait: the
    count this reads is per provider, and the poller asks each provider for its
    own answer.

    A capped value rather than an unbounded one because the point is to stop a
    broken host being asked every 30 seconds, not to give up on it: at the cap
    MCC still tries, just rarely.
    """

    if consecutive_failures <= 0:
        return interval
    doublings = min(consecutive_failures, MAX_BACKOFF_DOUBLINGS)
    return min(max(cap, interval), interval * 2**doublings)


def _streak_after(previous: ReadRecord | None, record: ReadRecord) -> int:
    """The failure count a finished attempt leaves behind.

    A record that already carries a count (the route set one) wins; otherwise
    yesterday's count plus one. The `not_sent` case counts too -- "no
    credential" is a fact that does not fix itself on the next tick, and a
    provider whose key was removed must not be pressed every 30 seconds
    either.
    """

    if record.consecutive_failures:
        return record.consecutive_failures
    carried = previous.consecutive_failures if previous is not None else 0
    return carried + 1


def _now() -> float:
    """Epoch seconds. One function so a test can freeze it if it ever needs to."""

    return time.time()
