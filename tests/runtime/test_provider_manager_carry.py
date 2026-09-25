"""A scoped replace carries objects; each is closed once, by its last holder.

``ProviderRuntimeManager.replace(rebuild_provider_ids=...)`` (7.55.0) hands the
previous generation's already-built providers to the new generation for every
id it was not asked to rebuild. Everything here is about ownership: two
generations holding one object must never close it while either still can use
it, and nothing may be closed twice -- not by a leased generation finishing
late, not by the replace failure path, not by a shutdown closing every
generation at once.
"""

import asyncio
from typing import Any, cast

import pytest

from my_claude_code.config.settings import Settings
from my_claude_code.providers.base import BaseProvider
from my_claude_code.providers.runtime import ProviderRuntime
from my_claude_code.runtime.provider_manager import ProviderRuntimeManager


class _Provider:
    """A provider double whose only job is to count its closes."""

    def __init__(self, provider_id: str, serial: int) -> None:
        self.provider_id = provider_id
        self.serial = serial
        self.cleanups = 0

    async def cleanup(self) -> None:
        self.cleanups += 1

    def __repr__(self) -> str:
        return f"<{self.provider_id}#{self.serial} closed {self.cleanups}x>"


class _Built:
    """Every provider object any runtime built, in build order."""

    def __init__(self) -> None:
        self.objects: list[_Provider] = []

    def create(self, provider_id: str, _settings: Settings) -> BaseProvider:
        provider = _Provider(provider_id, len(self.objects) + 1)
        self.objects.append(provider)
        return cast(BaseProvider, provider)


@pytest.fixture
def built(monkeypatch) -> _Built:
    record = _Built()
    monkeypatch.setattr(
        "my_claude_code.providers.runtime.runtime.create_provider", record.create
    )
    return record


def _settings() -> Settings:
    return Settings().model_copy(update={"model": "nvidia_nim/one"})


def _manager(settings: Settings | None = None) -> ProviderRuntimeManager:
    # The shipping factory: every runtime starts empty and builds lazily.
    return ProviderRuntimeManager(
        settings or _settings(), runtime_factory=ProviderRuntime
    )


async def _resolve(manager: ProviderRuntimeManager, provider_id: str) -> Any:
    async with await manager.acquire() as lease:
        return lease.resolve_provider(provider_id)


async def _scoped(manager: ProviderRuntimeManager, *rebuild: str) -> int:
    return await manager.replace(
        manager.current_settings(),
        commit=lambda: None,
        reason="proxy_chains",
        background_refresh=False,
        rebuild_provider_ids=frozenset(rebuild),
    )


@pytest.mark.asyncio
async def test_a_scoped_replace_carries_every_other_built_provider(built) -> None:
    manager = _manager()
    a1 = await _resolve(manager, "a")
    b1 = await _resolve(manager, "b")

    await _scoped(manager, "a")

    assert await _resolve(manager, "b") is b1
    a2 = await _resolve(manager, "a")
    assert a2 is not a1
    assert (a1.cleanups, a2.cleanups, b1.cleanups) == (1, 0, 0)
    await manager.close()
    assert [provider.cleanups for provider in built.objects] == [1, 1, 1]


@pytest.mark.asyncio
async def test_a_full_replace_carries_nothing(built) -> None:
    manager = _manager()
    a1 = await _resolve(manager, "a")
    b1 = await _resolve(manager, "b")

    await manager.replace(
        manager.current_settings(), commit=lambda: None, background_refresh=False
    )

    assert await _resolve(manager, "a") is not a1
    assert await _resolve(manager, "b") is not b1
    assert (a1.cleanups, b1.cleanups) == (1, 1)
    await manager.close()
    assert all(provider.cleanups == 1 for provider in built.objects)


@pytest.mark.asyncio
async def test_new_settings_widen_a_scoped_replace_to_a_full_one(built) -> None:
    """An object built from other settings is not what these would build."""

    manager = _manager()
    b1 = await _resolve(manager, "b")

    await manager.replace(
        _settings().model_copy(update={"model": "nvidia_nim/two"}),
        commit=lambda: None,
        background_refresh=False,
        rebuild_provider_ids=frozenset({"a"}),
    )

    assert await _resolve(manager, "b") is not b1
    assert b1.cleanups == 1


@pytest.mark.asyncio
async def test_a_leased_old_generation_finishing_late_closes_only_its_own(
    built,
) -> None:
    manager = _manager()
    a1 = await _resolve(manager, "a")
    b1 = await _resolve(manager, "b")
    lease = await manager.acquire()

    await _scoped(manager, "a")

    assert (a1.cleanups, b1.cleanups) == (0, 0)
    assert lease.resolve_provider("b") is b1
    await lease.release()
    assert (a1.cleanups, b1.cleanups) == (1, 0)
    assert await _resolve(manager, "b") is b1


@pytest.mark.asyncio
async def test_a_newer_generation_never_closes_what_an_older_lease_uses(
    built,
) -> None:
    """Generation 1 is leased and uses b1; 2 carries b1; 3 rebuilds b.

    Retiring 2 must not close b1: the request on 1 is still using it. Only
    when that lease ends is b1 closed -- once.
    """

    manager = _manager()
    b1 = await _resolve(manager, "b")
    lease = await manager.acquire()
    await _scoped(manager, "a")  # generation 2 carries b1
    await _scoped(manager, "b")  # generation 3 rebuilds b; 2 retires, unleased

    assert b1.cleanups == 0
    assert lease.resolve_provider("b") is b1
    b2 = await _resolve(manager, "b")
    assert b2 is not b1

    await lease.release()
    assert b1.cleanups == 1
    await manager.close()
    assert (b1.cleanups, b2.cleanups) == (1, 1)


@pytest.mark.asyncio
async def test_shutdown_closes_every_carried_object_exactly_once(built) -> None:
    """Every generation closes concurrently at shutdown; none twice, none never."""

    manager = _manager()
    await _resolve(manager, "a")
    await _resolve(manager, "b")
    await _resolve(manager, "c")
    leases = [await manager.acquire()]
    await _scoped(manager, "a")
    await _resolve(manager, "a")
    leases.append(await manager.acquire())
    await _scoped(manager, "b")
    await _resolve(manager, "b")
    leases.append(await manager.acquire())
    await _scoped(manager, "a")

    assert all(
        provider.cleanups == 0
        for provider in built.objects
        if provider.provider_id == "c"
    )
    for lease in leases:
        await lease.release()
    await manager.close()

    assert [provider.cleanups for provider in built.objects] == [1] * len(
        built.objects
    ), built.objects


@pytest.mark.asyncio
async def test_a_failed_scoped_replace_closes_nothing_it_would_have_carried(
    built,
) -> None:
    manager = _manager()
    a1 = await _resolve(manager, "a")
    b1 = await _resolve(manager, "b")
    generation = manager.current_generation_id

    def fail() -> None:
        raise RuntimeError("the commit failed")

    with pytest.raises(RuntimeError, match="commit failed"):
        await manager.replace(
            manager.current_settings(),
            commit=fail,
            background_refresh=False,
            rebuild_provider_ids=frozenset({"a"}),
        )

    assert manager.current_generation_id == generation
    assert (a1.cleanups, b1.cleanups) == (0, 0)
    assert await _resolve(manager, "a") is a1
    assert await _resolve(manager, "b") is b1
    await manager.close()
    assert (a1.cleanups, b1.cleanups) == (1, 1)


@pytest.mark.asyncio
async def test_a_factory_that_prebuilds_keeps_its_own_objects(built) -> None:
    """``adopt`` never overrides what a runtime already holds: nothing orphaned."""

    own: list[_Provider] = []

    def factory(settings: Settings) -> ProviderRuntime:
        provider = _Provider("b", 100 + len(own))
        own.append(provider)
        return ProviderRuntime(settings, {"b": cast(BaseProvider, provider)})

    manager = ProviderRuntimeManager(_settings(), runtime_factory=factory)
    a1 = await _resolve(manager, "a")

    await _scoped(manager, "x")

    assert await _resolve(manager, "a") is a1
    assert await _resolve(manager, "b") is own[1]
    assert own[0].cleanups == 1
    await manager.close()
    assert (a1.cleanups, own[0].cleanups, own[1].cleanups) == (1, 1, 1)


@pytest.mark.asyncio
async def test_concurrent_scoped_replaces_keep_single_ownership(built) -> None:
    manager = _manager()
    for provider_id in ("a", "b", "c"):
        await _resolve(manager, provider_id)

    await asyncio.gather(*(_scoped(manager, provider_id) for provider_id in "abcab"))
    for provider_id in ("a", "b", "c"):
        await _resolve(manager, provider_id)
    await manager.close()

    assert [provider.cleanups for provider in built.objects] == [1] * len(
        built.objects
    ), built.objects
