"""What MCC remembers about the exits of a proxy chain that refused (7.81.0).

A chain with "Keep trying exits until one answers" ticked moves a request to
another exit when the one it went out through is refused *because of the
exit*: a free-usage limit metered per address, a refusal for the exit's
country. Moving is half of it. The other half is not going back: an exit that
was spent a minute ago is still spent, and dialling it again on the next
request costs that request a round trip to learn what MCC already knew.

The chain's own rotation engine does remember -- a triggering failure benches
the exit for the provider's published wait -- but that engine lives inside the
provider object, and a chain save, the automatic speed re-sort and every other
rebuild of the provider replace the object and forget it. This table is the
same fact kept where a rebuild cannot reach: one process-wide table, keyed by
``(provider, credential, exit)`` -- the effective scope the engine's own bench
has, since a chain's rotation state is built per credential. The rotation state
of an opted-in chain mirrors it into its engine on every selection, so the
engine's existing skip state -- the one the Direct-fallback rule of 7.79.2
reads -- is what holds a remembered exit out.

Two states, each with the length the existing rules already give it; nothing
here invents a number:

``spent``
    An armed trigger (a 429, a quota answer, a timeout -- the chips the operator
    ticked) through this exit. As long as the provider's own published wait,
    under the existing proxy cap, else the operator's ``PROXY_COOLDOWN_SECONDS``
    -- exactly the bench the engine computes; this table is told the result.
``blocked``
    The host refused this exit's country in its own words. The operator's
    ``PROXY_COOLDOWN_SECONDS``: no host states a duration for that.

An exit that could not be reached, or dropped the connection before the first
byte, is on the process-wide reachability ledger
(``core/proxy_rotation.PROXY_REACHABILITY``) -- unhealthy until a check passes,
as every unreachable exit has been since 7.19.0. That ledger already survives a
rebuild, so it is not copied here.

Cleared by: its own expiry; a success through that exit for that provider and
credential; the Proxying page's **Forget exit memory** button
(:func:`forget_exits`). In memory only: a restart re-learns each spent exit at
the cost of one try. Media keeps a second table of the same shape
(:data:`MEDIA_EXIT_MEMORY`), exactly as it keeps its own reachability ledger.

``core`` may import nothing from ``config`` or ``application``, so the table
takes plain strings: the caller decides what a credential's identity is.
"""

import contextlib
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from my_claude_code.core.proxy_attribution import DIRECT_PROXY_LABEL
from my_claude_code.core.proxy_rotation import PROXY_HEALTH, PROXY_REACHABILITY

#: An armed trigger through this exit: the provider metered it out.
SPENT = "spent"
#: The host refused this exit's country in its own words.
BLOCKED = "blocked"
EXIT_MEMORY_STATES: tuple[str, ...] = (SPENT, BLOCKED)

#: Hard bound on the table. It is process-lifetime and keyed by strings an
#: operator supplies (three hundred exits times every credential of every
#: provider with a chain), so it is capped and pruned oldest-first like the
#: other proxy ledgers rather than trusted to stay small. Pruning only ever
#: costs one try on an exit that was forgotten early.
MAX_REMEMBERED_EXITS = 4096


@dataclass(frozen=True, slots=True)
class RememberedExit:
    """One exit MCC will not dial for one provider and credential until ``until``."""

    provider_id: str
    #: The credential's identity (a fingerprint or an account id) -- never a
    #: secret, never shown.
    credential: str
    #: What the page may show for that credential: its masked label.
    credential_label: str
    #: The chain entry's label (``host:port`` or its name), or ``direct``.
    exit_label: str
    state: str
    #: The failure, in a few words: ``rate_limit``, ``country refusal``.
    reason: str
    #: Deadline on the table's monotonic clock.
    until: float
    #: The same deadline as epoch seconds, for "until 14:05 UTC".
    until_wall: float
    #: The wait the provider itself published, when it published one.
    stated_wait: float | None = None
    #: When it was remembered, as epoch seconds.
    at_wall: float = 0.0


Key = tuple[str, str, str]


class ExitMemory:
    """The process-wide table of spent and blocked exits.

    Thread-safe: requests write it on the event loop and the Proxying page
    reads it from a worker thread.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        bound: int = MAX_REMEMBERED_EXITS,
    ) -> None:
        self._clock = clock
        self._wall = wall
        self._bound = max(1, int(bound))
        self._records: dict[Key, RememberedExit] = {}
        self._lock = threading.Lock()

    @property
    def clock(self) -> Callable[[], float]:
        return self._clock

    def remember(
        self,
        provider_id: str,
        credential: str,
        exit_label: str,
        *,
        state: str,
        seconds: float,
        reason: str,
        stated_wait: float | None = None,
        credential_label: str = "",
    ) -> RememberedExit | None:
        """Hold ``exit_label`` out for ``seconds``; ``None`` when that is no time."""

        if state not in EXIT_MEMORY_STATES:
            raise ValueError(f"not an exit memory state: {state!r}")
        key = (provider_id, credential, exit_label)
        if seconds <= 0:
            with self._lock:
                self._records.pop(key, None)
            return None
        now = self._clock()
        wall = self._wall()
        record = RememberedExit(
            provider_id=provider_id,
            credential=credential,
            credential_label=credential_label,
            exit_label=exit_label,
            state=state,
            reason=reason,
            until=now + seconds,
            until_wall=wall + seconds,
            stated_wait=stated_wait,
            at_wall=wall,
        )
        with self._lock:
            self._records.pop(key, None)
            self._records[key] = record
            self._prune(now)
        return record

    def recall(
        self, provider_id: str, credential: str, exit_label: str
    ) -> RememberedExit | None:
        """The live record for one exit, or ``None`` (an expired one is dropped)."""

        key = (provider_id, credential, exit_label)
        with self._lock:
            record = self._records.get(key)
            if record is None:
                return None
            if record.until <= self._clock():
                del self._records[key]
                return None
            return record

    def live(self, provider_id: str, credential: str) -> dict[str, float]:
        """Every live exit of one provider and credential: label -> seconds left."""

        now = self._clock()
        found: dict[str, float] = {}
        with self._lock:
            for key, record in list(self._records.items()):
                if record.until <= now:
                    del self._records[key]
                    continue
                if key[0] == provider_id and key[1] == credential:
                    found[key[2]] = record.until - now
        return found

    def remaining(self, record: RememberedExit) -> float:
        """Seconds until ``record`` lets its exit be dialled again; 0 once it does."""

        return max(0.0, record.until - self._clock())

    def forget_exit(self, provider_id: str, credential: str, exit_label: str) -> bool:
        """The exit just carried a request for this provider and credential."""

        with self._lock:
            return (
                self._records.pop((provider_id, credential, exit_label), None)
                is not None
            )

    def forget(
        self, provider_id: str | None = None, *, labels: Iterable[str] | None = None
    ) -> int:
        """Drop records: one provider's (``None`` = every provider), optionally
        only for ``labels``. Returns how many were dropped."""

        wanted = None if labels is None else frozenset(labels)
        with self._lock:
            doomed = [
                key
                for key in self._records
                if (provider_id is None or key[0] == provider_id)
                and (wanted is None or key[2] in wanted)
            ]
            for key in doomed:
                del self._records[key]
        return len(doomed)

    def records(self, provider_id: str | None = None) -> tuple[RememberedExit, ...]:
        """Every live record, oldest first, optionally for one provider."""

        now = self._clock()
        with self._lock:
            for key, record in list(self._records.items()):
                if record.until <= now:
                    del self._records[key]
            return tuple(
                record
                for record in self._records.values()
                if provider_id is None or record.provider_id == provider_id
            )

    def clear(self) -> None:
        """Forget everything. Tests."""

        with self._lock:
            self._records.clear()

    def _prune(self, now: float) -> None:
        for key, record in list(self._records.items()):
            if record.until <= now:
                del self._records[key]
        overflow = len(self._records) - self._bound
        if overflow <= 0:
            return
        # Insertion order is age order: ``remember`` re-inserts a key it
        # replaces, so the first keys are the oldest.
        for key in list(self._records)[:overflow]:
            del self._records[key]


#: The one chat table this process has.
EXIT_MEMORY = ExitMemory()
#: Media's own table, the way media keeps its own reachability ledger
#: (``providers/media/proxy_pool``): a media refusal never holds an exit out
#: for chat, nor the other way round. Here rather than beside the media ledger
#: so the Proxying page -- which may read ``core`` and not ``providers`` --
#: can show it and forget it.
MEDIA_EXIT_MEMORY = ExitMemory()

#: Told ``(provider_id, labels)`` when the Proxying page forgets a chain's
#: exits, so a book this module cannot import -- media's own reachability
#: ledger -- forgets them too.
ForgetListener = Callable[[str, frozenset[str]], None]
_FORGET_LISTENERS: list[ForgetListener] = []


def add_forget_listener(listener: ForgetListener) -> None:
    """Register ``listener`` once (a second registration of it is ignored)."""

    if listener not in _FORGET_LISTENERS:
        _FORGET_LISTENERS.append(listener)


def forget_exits(provider_id: str, labels: Iterable[str]) -> int:
    """The Proxying page's **Forget exit memory**, for one provider's chain.

    Drops every spent and blocked exit remembered for ``provider_id`` (chat
    and media, every credential), lifts the cooldown the page shows for each of
    ``labels``, and lets the ones on the reachability ledger be dialled again --
    the chain's own exits only, but for every provider using them, since that
    ledger is per address. Returns how many remembered exits were dropped.
    """

    wanted = frozenset(labels)
    dropped = EXIT_MEMORY.forget(provider_id) + MEDIA_EXIT_MEMORY.forget(provider_id)
    for label in wanted:
        if label != DIRECT_PROXY_LABEL and PROXY_REACHABILITY.is_unhealthy(label):
            PROXY_REACHABILITY.note_success(label)
        if PROXY_HEALTH.snapshot(provider_id, label)["state"] == "cooldown":
            PROXY_HEALTH.record(provider_id, label).benched_until = 0.0
    for listener in _FORGET_LISTENERS:
        with contextlib.suppress(Exception):
            listener(provider_id, wanted)
    return dropped


__all__ = [
    "BLOCKED",
    "EXIT_MEMORY",
    "EXIT_MEMORY_STATES",
    "MAX_REMEMBERED_EXITS",
    "MEDIA_EXIT_MEMORY",
    "SPENT",
    "ExitMemory",
    "ForgetListener",
    "RememberedExit",
    "add_forget_listener",
    "forget_exits",
]
