"""The resolved-catalogue snapshot: a hit costs nothing, and every input bites.

``GET /v1/models`` must not walk the resolution ladder per request -- the walk
measured 2.0-2.9 s on a 2,059-model catalogue and the handler runs on the event
loop -- so the records live in a process-wide snapshot keyed by everything they
depend on (8.21.0). These tests pin the two halves of that claim: a hit does no
work at all, and a change to any one input is a miss rather than a stale
answer. A cache that cannot be invalidated by an input is a guess, and a TTL
would bound staleness by a clock instead of by truth.
"""

import asyncio
import threading
import time
from pathlib import Path

import pytest

from my_claude_code.application import catalogue_snapshot
from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogue_snapshot import (
    catalogue_snapshot_key,
    current_catalogue_records,
    read_catalogue_snapshot,
    reset_catalogue_snapshot,
    store_catalogue_snapshot,
)
from my_claude_code.application.ports import CatalogueSnapshotInputs
from my_claude_code.config.harness_tiers import (
    EMPTY_HARNESS_TIERS,
    HarnessTierOverride,
    HarnessTiers,
)
from my_claude_code.config.settings import Settings
from my_claude_code.providers.runtime.model_cache import ProviderModelCache
from my_claude_code.runtime.provider_manager import ProviderRuntimeManager

REF = "nvidia_nim/test-model"


def _settings() -> Settings:
    return Settings.model_validate({"MODEL": REF})


def _record(gateway_id: str = f"anthropic/{REF}") -> CatalogueModel:
    return CatalogueModel(
        gateway_id=gateway_id,
        provider_model_ref=REF,
        display_name=REF,
        context_length=1_000_000,
    )


def _manager(
    monkeypatch: pytest.MonkeyPatch,
    *,
    generation: int = 1,
    revision: int = 0,
    models_dev: tuple[int, int] | None = None,
) -> ProviderRuntimeManager:
    """A real manager whose three revision inputs this test can move."""

    manager = ProviderRuntimeManager(_settings())
    monkeypatch.setattr(
        manager,
        "catalogue_snapshot_inputs",
        lambda: CatalogueSnapshotInputs(generation, revision, models_dev),
    )
    return manager


def _spy_build(monkeypatch: pytest.MonkeyPatch, calls: list[int], delay: float = 0.0):
    def build(*_args: object, **_kwargs: object) -> tuple[CatalogueModel, ...]:
        calls.append(threading.get_ident())
        if delay:
            time.sleep(delay)
        return (_record(),)

    monkeypatch.setattr(catalogue_snapshot, "build_catalogue_models", build)


def test_a_hit_does_not_walk_the_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager(monkeypatch)
    calls: list[int] = []
    _spy_build(monkeypatch, calls)

    first = asyncio.run(current_catalogue_records(_settings(), manager))
    second = asyncio.run(current_catalogue_records(_settings(), manager))

    assert len(calls) == 1, "the second request must not rebuild"
    assert first == second
    assert list(first) == [f"anthropic/{REF}"]


def test_the_key_is_the_same_value_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """No part of the key may be salted per call, or every request would miss."""

    manager = _manager(monkeypatch)
    first = catalogue_snapshot_key(manager, EMPTY_HARNESS_TIERS)
    second = catalogue_snapshot_key(manager, EMPTY_HARNESS_TIERS)
    assert first == second


@pytest.mark.parametrize(
    ("generation", "revision", "models_dev"),
    [
        (2, 0, None),
        (1, 1, None),
        (1, 0, (1, 2)),
    ],
)
def test_a_moved_runtime_revision_invalidates(
    monkeypatch: pytest.MonkeyPatch,
    generation: int,
    revision: int,
    models_dev: tuple[int, int] | None,
) -> None:
    manager = _manager(monkeypatch)
    key = catalogue_snapshot_key(manager, EMPTY_HARNESS_TIERS)
    assert store_catalogue_snapshot(manager, EMPTY_HARNESS_TIERS, key, (_record(),))
    assert read_catalogue_snapshot(manager) is not None

    monkeypatch.setattr(
        manager,
        "catalogue_snapshot_inputs",
        lambda: CatalogueSnapshotInputs(generation, revision, models_dev),
    )

    assert read_catalogue_snapshot(manager) is None


def test_a_tier_edit_invalidates(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager(monkeypatch)
    monkeypatch.setattr(
        catalogue_snapshot, "current_harness_tiers", lambda: EMPTY_HARNESS_TIERS
    )
    key = catalogue_snapshot_key(manager, EMPTY_HARNESS_TIERS)
    assert store_catalogue_snapshot(manager, EMPTY_HARNESS_TIERS, key, (_record(),))
    assert read_catalogue_snapshot(manager) is not None

    edited = HarnessTiers(
        harnesses={
            "opencode": {"best": HarnessTierOverride(model="open_router/grok-5")}
        }
    )
    monkeypatch.setattr(catalogue_snapshot, "current_harness_tiers", lambda: edited)

    assert read_catalogue_snapshot(manager) is None


def test_an_overrides_edit_invalidates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manager = _manager(monkeypatch)
    path = tmp_path / "model_overrides.json"
    monkeypatch.setattr(catalogue_snapshot, "model_overrides_path", lambda: path)
    key = catalogue_snapshot_key(manager, EMPTY_HARNESS_TIERS)
    assert store_catalogue_snapshot(manager, EMPTY_HARNESS_TIERS, key, (_record(),))
    assert read_catalogue_snapshot(manager) is not None

    # The operator's per-model context window is a rung above every other one,
    # so an edit must invalidate even though no publish follows it.
    path.write_text('{"models": {"nvidia_nim/test-model": {"context_length": 4096}}}')

    assert read_catalogue_snapshot(manager) is None


def test_a_models_dev_refresh_invalidates(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager(monkeypatch, models_dev=None)
    key = catalogue_snapshot_key(manager, EMPTY_HARNESS_TIERS)
    assert store_catalogue_snapshot(manager, EMPTY_HARNESS_TIERS, key, (_record(),))

    monkeypatch.setattr(
        manager,
        "catalogue_snapshot_inputs",
        lambda: CatalogueSnapshotInputs(1, 0, (123456789, 6499730)),
    )

    assert read_catalogue_snapshot(manager) is None


def test_a_publish_replaces_the_records(monkeypatch: pytest.MonkeyPatch) -> None:
    """The writer is the fan-out publisher, and a second publish wins."""

    manager = _manager(monkeypatch)
    key = catalogue_snapshot_key(manager, EMPTY_HARNESS_TIERS)
    assert store_catalogue_snapshot(
        manager, EMPTY_HARNESS_TIERS, key, (_record("anthropic/old"),)
    )
    assert store_catalogue_snapshot(
        manager, EMPTY_HARNESS_TIERS, key, (_record("anthropic/new"),)
    )

    snapshot = read_catalogue_snapshot(manager)
    assert snapshot is not None
    assert list(snapshot.by_gateway_id) == ["anthropic/new"]


def test_a_build_whose_inputs_moved_is_not_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The honest cost of a race: drop the tuple, let the next request rebuild."""

    manager = _manager(monkeypatch)
    key = catalogue_snapshot_key(manager, EMPTY_HARNESS_TIERS)
    monkeypatch.setattr(
        manager,
        "catalogue_snapshot_inputs",
        lambda: CatalogueSnapshotInputs(1, 1, None),
    )

    stored = store_catalogue_snapshot(manager, EMPTY_HARNESS_TIERS, key, (_record(),))

    assert stored is None
    assert read_catalogue_snapshot(manager) is None


def test_a_snapshot_is_never_served_to_another_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two managers can share every key component; they are still not one."""

    first = _manager(monkeypatch)
    second = _manager(monkeypatch)
    key = catalogue_snapshot_key(first, EMPTY_HARNESS_TIERS)
    assert catalogue_snapshot_key(second, EMPTY_HARNESS_TIERS) == key
    assert store_catalogue_snapshot(first, EMPTY_HARNESS_TIERS, key, (_record(),))

    assert read_catalogue_snapshot(second) is None
    assert read_catalogue_snapshot(first) is not None


@pytest.mark.asyncio
async def test_one_rebuild_serves_every_concurrent_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(monkeypatch)
    calls: list[int] = []
    _spy_build(monkeypatch, calls, delay=0.05)

    results = await asyncio.gather(
        *(current_catalogue_records(_settings(), manager) for _ in range(8))
    )

    assert len(calls) == 1, "eight misses must produce one ladder walk"
    assert all(result == results[0] for result in results)


@pytest.mark.asyncio
async def test_the_rebuild_runs_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(monkeypatch)
    calls: list[int] = []
    _spy_build(monkeypatch, calls)

    await current_catalogue_records(_settings(), manager)

    assert calls
    assert calls[0] != threading.get_ident()


def test_the_model_cache_revision_moves_on_every_write() -> None:
    """The input that makes a provider sweep invalidate the snapshot."""

    cache = ProviderModelCache(("nvidia_nim",))
    start = cache.revision

    cache.cache_model_infos("nvidia_nim", ())
    after_infos = cache.revision
    assert after_infos > start

    cache.set_available_providers(("nvidia_nim",))
    after_scope = cache.revision
    assert after_scope > after_infos

    cache.clear()
    assert cache.revision > after_scope


def test_reset_forgets_the_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager(monkeypatch)
    key = catalogue_snapshot_key(manager, EMPTY_HARNESS_TIERS)
    assert store_catalogue_snapshot(manager, EMPTY_HARNESS_TIERS, key, (_record(),))

    reset_catalogue_snapshot()

    assert read_catalogue_snapshot(manager) is None
