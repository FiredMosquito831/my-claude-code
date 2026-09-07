"""The periodic sweep, and the guards that make one safe to ship at all.

``refresh_provider_models`` says in its own docstring that *a periodic sweep is
deliberately not the answer, because the sweep is what caused the race this
replaces*. Every case here is one half of the answer to that sentence.
"""

import asyncio

import pytest

from my_claude_code.application.model_metadata import (
    ProviderDiscoveryFailure,
    ProviderModelInfo,
    ProviderModelRefreshResult,
)
from my_claude_code.config.constants import (
    MODEL_DISCOVERY_REFRESH_MINIMUM_SECONDS,
    MODEL_DISCOVERY_REFRESH_SECONDS_DEFAULT,
    MODEL_PROBE_NEW_MODELS_DEFAULT,
)
from my_claude_code.providers.recovery import FACT_OUTPUT_CAP
from my_claude_code.providers.recovery.store import (
    LearnedFactStore,
    set_learned_fact_store,
)
from my_claude_code.providers.runtime.discovery import (
    SHRINK_GUARD_MINIMUM_MODELS,
    CatalogueShrankError,
    cache_enriched_model_infos,
)
from my_claude_code.runtime.discovery_timer import (
    AUTH_BACKOFF_TICKS,
    ProviderBackoff,
    ProviderDiscoveryTimer,
    resolve_refresh_interval,
)


def _infos(*model_ids: str) -> tuple[ProviderModelInfo, ...]:
    return tuple(ProviderModelInfo(model_id=model_id) for model_id in model_ids)


def test_the_timer_ships_on_hourly_and_zero_turns_it_off() -> None:
    assert MODEL_DISCOVERY_REFRESH_SECONDS_DEFAULT == 3600.0
    assert resolve_refresh_interval(3600) == 3600
    assert resolve_refresh_interval(0) == 0.0
    assert resolve_refresh_interval(-5) == 0.0


def test_a_typo_below_the_floor_is_raised_not_obeyed() -> None:
    """One sweep is one upstream request per provider; 30 s would be an attack."""

    assert resolve_refresh_interval(30) == MODEL_DISCOVERY_REFRESH_MINIMUM_SECONDS


def test_probing_new_models_ships_off() -> None:
    """Pinned, not derived: the value is the point of the decision."""

    assert MODEL_PROBE_NEW_MODELS_DEFAULT is False


@pytest.mark.asyncio
async def test_timer_off_by_default_starts_no_task() -> None:
    timer = ProviderDiscoveryTimer(lambda _skipped: None, lambda: 0.0)
    assert timer.start() is False
    assert timer.running is False
    await timer.close()


@pytest.mark.asyncio
async def test_timer_is_cancelled_on_close() -> None:
    timer = ProviderDiscoveryTimer(
        lambda _skipped: ProviderModelRefreshResult(), lambda: 3600.0
    )
    assert timer.start() is True
    assert timer.running is True
    await timer.close()
    assert timer.running is False


@pytest.mark.asyncio
async def test_a_sweep_that_raises_does_not_kill_the_loop() -> None:
    def boom(_skipped):
        raise RuntimeError("upstream is on fire")

    timer = ProviderDiscoveryTimer(boom, lambda: 3600.0)
    assert await timer.tick() == ProviderModelRefreshResult()


@pytest.mark.asyncio
async def test_provider_failing_401_is_backed_off() -> None:
    """A rejected credential asked again every hour is how an account is banned."""

    seen: list[frozenset[str]] = []

    async def sweep(skipped: frozenset[str]) -> ProviderModelRefreshResult:
        seen.append(skipped)
        return ProviderModelRefreshResult(
            failed_provider_ids=("moody",),
            failures=(
                ProviderDiscoveryFailure(
                    provider_id="moody",
                    error_type="PermissionDeniedError",
                    message="denied",
                    status_code=403,
                ),
            ),
        )

    timer = ProviderDiscoveryTimer(sweep, lambda: 3600.0)
    await timer.tick()
    await timer.tick()

    assert seen[0] == frozenset()
    assert seen[1] == frozenset({"moody"})


@pytest.mark.asyncio
async def test_a_timeout_is_not_an_auth_failure() -> None:
    async def sweep(_skipped: frozenset[str]) -> ProviderModelRefreshResult:
        return ProviderModelRefreshResult(
            failed_provider_ids=("slow",),
            failures=(
                ProviderDiscoveryFailure(
                    provider_id="slow",
                    error_type="TimeoutError",
                    message="timed out",
                    status_code=None,
                ),
            ),
        )

    timer = ProviderDiscoveryTimer(sweep, lambda: 3600.0)
    await timer.tick()
    await timer.tick()
    assert timer._backoff.skipped_provider_ids() == frozenset()


def test_a_one_tick_bench_actually_misses_one_tick() -> None:
    """Read before decrement, or the first rung of the ladder is a no-op."""

    backoff = ProviderBackoff()
    backoff.note_auth_failure("p")
    assert backoff.consume() == frozenset({"p"})
    assert backoff.consume() == frozenset()


def test_the_backoff_ladder_escalates_and_clamps() -> None:
    backoff = ProviderBackoff()
    walked = [backoff.note_auth_failure("p") for _ in range(6)]
    assert walked == [1, 2, 4, 8, 8, 8]
    assert walked[-1] == AUTH_BACKOFF_TICKS[-1]

    backoff.note_success("p")
    assert backoff.skipped_provider_ids() == frozenset()
    assert backoff.note_auth_failure("p") == 1


@pytest.mark.asyncio
async def test_catalogue_change_logs_one_line_per_changed_provider() -> None:
    cached: dict[str, tuple[ProviderModelInfo, ...]] = {}

    def cache(provider_id: str, infos) -> None:
        cached[provider_id] = tuple(infos)

    lines: list[str] = []
    from loguru import logger

    # Filtered to this module's own records: the suite re-propagates loguru
    # through std logging, so an unfiltered sink sees every line twice and the
    # "exactly one line" assertion would be untestable.
    sink = logger.add(
        lambda message: lines.append(str(message)),
        level="INFO",
        filter=lambda record: (
            record["name"] == "my_claude_code.providers.runtime.discovery"
        ),
    )
    try:
        await cache_enriched_model_infos(
            "stub", _infos("a", "c"), cache, previous_ids=frozenset({"a", "b"})
        )
    finally:
        logger.remove(sink)

    changed = [line for line in lines if "catalogue changed" in line]
    assert len(changed) == 1
    assert "+1 -1 for stub" in changed[0]
    assert "now 2 models" in changed[0]


@pytest.mark.asyncio
async def test_unchanged_catalogue_logs_nothing_at_info() -> None:
    """57 unchanged lines an hour is noise, so an unchanged sweep is silent."""

    lines: list[str] = []
    from loguru import logger

    sink = logger.add(
        lambda message: lines.append(str(message)),
        level="INFO",
        filter=lambda record: (
            record["name"] == "my_claude_code.providers.runtime.discovery"
        ),
    )
    try:
        await cache_enriched_model_infos(
            "stub",
            _infos("a", "b"),
            lambda _p, _i: None,
            previous_ids=frozenset({"a", "b"}),
            quiet=True,
        )
    finally:
        logger.remove(sink)

    assert [line for line in lines if "stub" in line] == []


@pytest.mark.asyncio
async def test_disappeared_model_marks_its_facts_stale() -> None:
    store = LearnedFactStore(flush_debounce_seconds=0.0)
    set_learned_fact_store(store)
    try:
        store.record("stub", "b", FACT_OUTPUT_CAP, 4096)
        await cache_enriched_model_infos(
            "stub",
            _infos("a", "c"),
            lambda _p, _i: None,
            previous_ids=frozenset({"a", "b"}),
        )
        (fact,) = store.all_facts()
        # Retired, never deleted: the row stays so the page can still say what
        # MCC used to believe about a model that has gone.
        assert fact.retired is True
        assert store.memory_for("stub").cap_for("b") is None
    finally:
        set_learned_fact_store(LearnedFactStore(flush_debounce_seconds=0.0))


@pytest.mark.asyncio
async def test_a_collapsed_catalogue_is_a_failed_sweep_not_300_deletions() -> None:
    """One bad upstream response must not stale hundreds of learned facts."""

    previous = frozenset(f"m{index}" for index in range(40))
    with pytest.raises(CatalogueShrankError):
        await cache_enriched_model_infos(
            "stub", _infos("m0"), lambda _p, _i: None, previous_ids=previous
        )


@pytest.mark.asyncio
async def test_a_small_catalogue_is_not_guarded() -> None:
    """Below the minimum the ratio is noise, not evidence."""

    previous = frozenset(
        f"m{index}" for index in range(SHRINK_GUARD_MINIMUM_MODELS - 1)
    )
    cached: dict[str, tuple[ProviderModelInfo, ...]] = {}
    await cache_enriched_model_infos(
        "stub",
        _infos("m0"),
        lambda provider_id, infos: cached.__setitem__(provider_id, tuple(infos)),
        previous_ids=previous,
    )
    assert len(cached["stub"]) == 1


@pytest.mark.asyncio
async def test_timer_tick_during_sweep_is_skipped(monkeypatch) -> None:
    """A tick that lands mid-sweep is dropped, never queued behind it."""

    class _Manager:
        def __init__(self) -> None:
            self.calls = 0
            self.in_flight = False

        @property
        def refresh_in_flight(self) -> bool:
            return self.in_flight

        async def sweep(self, skipped: frozenset[str]) -> ProviderModelRefreshResult:
            if self.in_flight:
                return ProviderModelRefreshResult()
            self.calls += 1
            return ProviderModelRefreshResult(refreshed_provider_ids=("stub",))

    manager = _Manager()
    timer = ProviderDiscoveryTimer(manager.sweep, lambda: 3600.0)
    await timer.tick()
    manager.in_flight = True
    await timer.tick()

    assert manager.calls == 1


@pytest.mark.asyncio
async def test_the_loop_re_reads_the_interval_each_pass() -> None:
    """Turning the setting off stops the loop without a restart."""

    interval = {"value": 3600.0}
    ticks = {"count": 0}

    async def sweep(_skipped: frozenset[str]) -> ProviderModelRefreshResult:
        ticks["count"] += 1
        interval["value"] = 0.0
        return ProviderModelRefreshResult()

    async def instant(_seconds: float) -> None:
        await asyncio.sleep(0)

    timer = ProviderDiscoveryTimer(sweep, lambda: interval["value"], sleep=instant)
    await asyncio.wait_for(timer.run(), timeout=5)

    assert ticks["count"] == 1
