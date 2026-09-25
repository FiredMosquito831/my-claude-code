"""Keep the fastest healthy proxy first: the chain's ORDER is the data (7.56.0).

Option A of the proxy speed spec, as the user decided it: the selection engine
(``core/proxy_rotation.py``, ``providers/runtime/proxy_rotating.py``) is not
touched. Under ``failover`` the engine takes the lowest selectable index and
under ``single`` the first unpaused one, so putting the fastest healthy address
first is a rewrite of the chain's ``entries`` -- through the ordinary chain
save, which since 7.55.0 rebuilds that one provider and nothing else.

This module is the arithmetic, and nothing here writes: it reads the stored
chain, the speed ledger and the health ledgers, and says what order it would
write and whether the rules allow it. The writer is
``api/admin_proxy_routes.commit_speed_order``, under the chain writer lock.

**What is sorted.** Only entries that are not paused, not Direct, not refused
(the checker caught them terminating TLS), not unhealthy (the reachability
bench), and not holding a trigger bench ("cooldown"), by the §7.3 ``rank_key``
for *this chain's provider*. Ties keep their current order. Everything else
keeps its relative order after the sorted ones, with two exceptions that keep
their exact place: **Direct** (the user's decision 6 -- it keeps its distance
from the end of the list, and the list's length never changes) and an address
holding a **trigger bench**, which is never promoted while it is benched and
is not demoted for a bench either.

**When an automatic sort is written** (spec §7.4), all of:

* the first healthy entry -- the one failover actually uses -- changes;
* the newcomer's rank is at most 0.75 x the incumbent's **and** at least
  500 ms better (an incumbent MCC has no measured setup for loses to any
  measured newcomer);
* the newcomer has at least 3 samples in the ledger's 24-hour window;
* no sort was written into this chain in the last
  ``PROXY_ORDER_RESORT_MINUTES``.

A change below the first healthy entry is never written on its own: it rides
along with a first-entry change, or waits for the operator's "Sort by speed
now", which applies the same sort without those thresholds.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from my_claude_code.config.credentials import mask_proxy_label
from my_claude_code.config.proxy_chains import ProxyChain, ProxyChains
from my_claude_code.core.proxy_rotation import (
    PROXY_HEALTH,
    PROXY_INTERCEPTION,
    PROXY_REACHABILITY,
)
from my_claude_code.core.proxy_speed import PROXY_SPEED

#: The policies speed ordering is offered for (user decision 7). Under
#: ``round_robin`` and ``least_used`` the order does not decide which address
#: carries a request in a way "fastest first" could improve.
ORDERABLE_POLICIES: tuple[str, ...] = ("failover", "single")

#: A newcomer's rank must be at most this fraction of the incumbent's ...
RESORT_MARGIN_RATIO = 0.75
#: ... and at least this many milliseconds better.
RESORT_MARGIN_MS = 500.0
#: Samples (checks and dials, 24-hour window) a newcomer needs before it can
#: be put first automatically. A single lucky pass is not a measurement.
RESORT_MIN_SAMPLES = 3

#: The health snapshot's word for a live trigger bench (429, quota, timeout).
COOLDOWN_STATE = "cooldown"

#: How often the ordering loop (``runtime/proxy_order.py``) looks at the chains
#: with the switch on. Looking is free -- a store read from the mtime cache and
#: a few ledger reads; a write happens only when every rule above holds.
PROXY_ORDER_TICK_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class EntryFacts:
    """What the sort needs to know about one entry, read once."""

    index: int
    proxy: str
    label: str
    direct: bool
    paused: bool
    refused: bool
    unhealthy: bool
    cooldown: bool
    #: The §7.3 rank for this chain's provider; ``None`` when MCC has no
    #: measured setup for it. Lower is better.
    rank_key: float | None
    #: Check and dial samples inside the ledger's 24-hour window.
    samples: int

    @property
    def pinned(self) -> bool:
        """Keeps its exact index: Direct, and an address on a trigger bench."""

        return self.direct or self.cooldown

    @property
    def rankable(self) -> bool:
        """May be sorted by speed, and so may be put first."""

        return bool(self.proxy) and not (
            self.direct
            or self.paused
            or self.refused
            or self.unhealthy
            or self.cooldown
        )

    @property
    def name(self) -> str:
        return "Direct" if self.direct else (self.label or self.proxy)


@dataclass(frozen=True, slots=True)
class OrderPlan:
    """A proposed order, and whether the rules let it be written."""

    #: The new order as old indexes: ``entries[order[0]]`` goes first.
    order: tuple[int, ...]
    #: Whether the chain would change at all.
    changed: bool
    #: Whether the rules allow writing it (always ``changed`` for an explicit
    #: sort).
    write: bool
    #: One sentence: why it is written, or why it is not.
    reason: str
    #: The first healthy entry now, and after the sort.
    old_first: EntryFacts | None
    new_first: EntryFacts | None


def entry_facts(
    chain: ProxyChain,
    store: ProxyChains,
    provider_id: str,
    *,
    failure_cost_ms: float,
    slow_ms: float,
    now: float | None = None,
) -> tuple[EntryFacts, ...]:
    """Read every entry's standing from the store and the ledgers."""

    facts: list[EntryFacts] = []
    for index, entry in enumerate(chain.entries):
        if entry.is_direct:
            facts.append(
                EntryFacts(
                    index=index,
                    proxy="",
                    label="",
                    direct=True,
                    paused=entry.paused,
                    refused=False,
                    unhealthy=False,
                    cooldown=False,
                    rank_key=None,
                    samples=0,
                )
            )
            continue
        endpoint = store.endpoint(entry.proxy)
        if endpoint is None:
            # A dangling id: never sorted, never promoted, kept where the
            # "rest" go. The store's own reader drops these on the next load.
            facts.append(
                EntryFacts(
                    index=index,
                    proxy=entry.proxy,
                    label="",
                    direct=False,
                    paused=entry.paused,
                    refused=False,
                    unhealthy=True,
                    cooldown=False,
                    rank_key=None,
                    samples=0,
                )
            )
            continue
        label = endpoint.label or mask_proxy_label(endpoint.url)
        score = PROXY_SPEED.score(
            label,
            provider_id,
            failure_cost_ms=failure_cost_ms,
            slow_ms=slow_ms,
            now=now,
        )
        facts.append(
            EntryFacts(
                index=index,
                proxy=entry.proxy,
                label=label,
                direct=False,
                paused=entry.paused,
                refused=endpoint.refused or PROXY_INTERCEPTION.is_refused(label),
                unhealthy=PROXY_REACHABILITY.is_unhealthy(label),
                cooldown=(
                    PROXY_HEALTH.snapshot(provider_id, label).get("state")
                    == COOLDOWN_STATE
                ),
                rank_key=score.rank_key,
                samples=score.samples,
            )
        )
    return tuple(facts)


def ranked(facts: Sequence[EntryFacts]) -> list[EntryFacts]:
    """The rankable entries, fastest first; ties and the unmeasured keep order.

    ``sorted`` is stable, and ``facts`` is in chain order, so two entries with
    the same rank -- or two MCC has not measured -- keep the order the
    operator gave them.
    """

    return sorted(
        (fact for fact in facts if fact.rankable),
        key=lambda fact: (
            fact.rank_key is None,
            0.0 if fact.rank_key is None else fact.rank_key,
        ),
    )


def speed_order(facts: Sequence[EntryFacts]) -> tuple[int, ...]:
    """The sorted order, as old indexes. Never changes the list's length.

    Pinned entries keep their index. The other slots, in ascending order, take
    the ranked entries fastest first and then everything else in its current
    relative order -- so an unhealthy, refused or paused entry can only ever
    move *down*, never up.
    """

    order: list[int] = [-1] * len(facts)
    for fact in facts:
        if fact.pinned:
            order[fact.index] = fact.index
    free = [fact.index for fact in facts if not fact.pinned]
    fastest = ranked(facts)
    rest = [fact for fact in facts if not fact.pinned and not fact.rankable]
    for slot, fact in zip(free, [*fastest, *rest], strict=True):
        order[slot] = fact.index
    return tuple(order)


def first_healthy(facts: Sequence[EntryFacts]) -> EntryFacts | None:
    """The entry failover uses now: the first rankable one in chain order."""

    return next((fact for fact in facts if fact.rankable), None)


def plan_speed_order(
    facts: Sequence[EntryFacts],
    *,
    explicit: bool,
    last_sorted_at: str = "",
    resort_minutes: float,
    now: datetime | None = None,
) -> OrderPlan:
    """What a sort would write, and whether it may (spec §7.4)."""

    order = speed_order(facts)
    changed = order != tuple(range(len(facts)))
    old_first = first_healthy(facts)
    fastest = ranked(facts)
    new_first = fastest[0] if fastest else None

    def plan(write: bool, reason: str) -> OrderPlan:
        return OrderPlan(
            order=order,
            changed=changed,
            write=write,
            reason=reason,
            old_first=old_first,
            new_first=new_first,
        )

    if not changed:
        return plan(False, "the chain is already in speed order")
    if explicit:
        return plan(True, "sorted by speed on request")
    if new_first is None or old_first is None or new_first.index == old_first.index:
        return plan(
            False,
            "the first healthy entry would not change; a change further down "
            "waits for one that does, or for Sort by speed now",
        )
    if new_first.rank_key is None:  # pragma: no cover - ranked sorts None last
        return plan(False, "the newcomer has no measured setup")
    if new_first.samples < RESORT_MIN_SAMPLES:
        return plan(
            False,
            f"{new_first.name} has {new_first.samples} sample(s); "
            f"{RESORT_MIN_SAMPLES} are needed",
        )
    incumbent = old_first.rank_key
    if incumbent is not None and not (
        new_first.rank_key <= RESORT_MARGIN_RATIO * incumbent
        and incumbent - new_first.rank_key >= RESORT_MARGIN_MS
    ):
        return plan(
            False,
            f"{new_first.name} ({new_first.rank_key:.0f} ms) is not clearly "
            f"faster than {old_first.name} ({incumbent:.0f} ms)",
        )
    since = minutes_since(last_sorted_at, now=now)
    if since is not None and since < resort_minutes:
        return plan(
            False,
            f"a speed order was written {since:.0f} minute(s) ago; the next "
            f"may be written after {resort_minutes:g}",
        )
    return plan(True, "a clearly faster healthy entry is now first")


def pause_all_but_fastest(facts: Sequence[EntryFacts], keep: int) -> tuple[int, ...]:
    """The indexes "Pause all but the fastest N" would pause.

    The N best-ranked rankable entries stay as they are; every other address
    that is not already paused is paused. Direct is never paused by it, and
    nothing is un-paused: the gesture only ever takes addresses out.
    """

    kept = {fact.index for fact in ranked(facts)[: max(0, keep)]}
    return tuple(
        fact.index
        for fact in facts
        if not fact.direct and not fact.paused and fact.index not in kept
    )


def minutes_since(stamp: str, *, now: datetime | None = None) -> float | None:
    """Minutes from an ISO timestamp to ``now``; ``None`` for none or garbage."""

    text = (stamp or "").strip()
    if not text:
        return None
    try:
        then = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    current = now if now is not None else datetime.now(UTC)
    return max(0.0, (current - then).total_seconds() / 60.0)


def iso_now(now: datetime | None = None) -> str:
    """The store's timestamp shape: ISO 8601, UTC, ``Z``."""

    current = now if now is not None else datetime.now(UTC)
    return current.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "COOLDOWN_STATE",
    "ORDERABLE_POLICIES",
    "PROXY_ORDER_TICK_SECONDS",
    "RESORT_MARGIN_MS",
    "RESORT_MARGIN_RATIO",
    "RESORT_MIN_SAMPLES",
    "EntryFacts",
    "OrderPlan",
    "entry_facts",
    "first_healthy",
    "iso_now",
    "minutes_since",
    "pause_all_but_fastest",
    "plan_speed_order",
    "ranked",
    "speed_order",
]
