"""A refused settings save leaves the running server exactly as it was (7.69.7).

``SettingsFileBusyError`` can come from two places in a save: the strict reads
while the update is prepared (before any generation is built), and the commit
inside ``ProviderRuntimeManager.replace`` (after the candidate generation is
built, before it is published). Either way the server keeps the same
``Settings`` object, the same generation, the same pending-restart list, and
the file on disk is byte-identical. The next save, once the file is free,
goes through as normal.
"""

import hashlib
import os
from pathlib import Path

import pytest

from my_claude_code.config.admin import env_io, sources
from my_claude_code.config.admin.env_io import SettingsFileBusyError
from my_claude_code.config.settings import Settings
from my_claude_code.providers.runtime import ProviderRuntime
from my_claude_code.runtime.application import ApplicationRuntime
from my_claude_code.runtime.provider_manager import ProviderRuntimeManager

FABLE_A = "nvidia_nim/vendor/fable-a"
FABLE_B = "nvidia_nim/vendor/fable-b"

SEED = (
    "DEEPSEEK_API_KEY=sk-test-fake-0001\n"
    f"MODEL_FABLE={FABLE_A}\n"
    f"MODEL_FABLE_FALLBACKS={FABLE_B}\n"
    "LOG_LEVEL=INFO\n"
)

_REAL_READ = env_io._attempt_read
_REAL_REPLACE = env_io._attempt_replace


class CountingFactory:
    """Builds a plain runtime and counts how many candidates were built."""

    def __init__(self) -> None:
        self.built = 0

    def __call__(self, settings: Settings) -> ProviderRuntime:
        self.built += 1
        return ProviderRuntime(settings)


def _same(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def managed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / ".mcc"
    config.mkdir()
    monkeypatch.setenv("MCC_CONFIG_DIR", str(config))
    monkeypatch.chdir(tmp_path)
    for key in (
        "MODEL_FABLE",
        "MODEL_FABLE_FALLBACKS",
        "MODEL_FABLE_PAUSED",
        "DEEPSEEK_API_KEY",
        "LOG_LEVEL",
        "MCC_ENV_FILE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(env_io, "_sleep", lambda _seconds: None)
    sources.clear_env_parse_cache()
    path = config / ".env"
    path.write_text(SEED, encoding="utf-8")
    return path


def _runtime() -> tuple[ApplicationRuntime, ProviderRuntimeManager, CountingFactory]:
    factory = CountingFactory()
    settings = Settings().model_copy(
        update={"model_fable": FABLE_A, "model_fable_fallbacks": FABLE_B}
    )
    manager = ProviderRuntimeManager(settings, runtime_factory=factory)
    return ApplicationRuntime(manager, transcriber=None), manager, factory


def _pause_b(settings: Settings) -> dict[str, str]:
    """What the Pause button's route builds, from the running settings."""

    assert settings.model_fable_fallbacks == FABLE_B
    return {"MODEL_FABLE_PAUSED": FABLE_B}


@pytest.mark.asyncio
async def test_a_save_refused_while_reading_changes_nothing_in_memory(
    managed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, manager, factory = _runtime()
    settings_before = runtime.settings
    generation_before = manager.current_generation_id
    built_before = factory.built
    sha_before = _sha(managed)

    def busy(path: Path) -> tuple[bytes, int]:
        if _same(path, managed):
            raise PermissionError(13, "Permission denied", str(path))
        return _REAL_READ(path)

    monkeypatch.setattr(env_io, "_attempt_read", busy)

    with pytest.raises(SettingsFileBusyError):
        await runtime.apply_admin_config_with(_pause_b)

    assert runtime.settings is settings_before
    assert manager.current_generation_id == generation_before
    assert factory.built == built_before, "no candidate generation was built"
    assert runtime._pending_fields == []
    assert _sha(managed) == sha_before

    # The file is free again: the very same gesture goes through.
    monkeypatch.setattr(env_io, "_attempt_read", _REAL_READ)
    result = await runtime.apply_admin_config_with(_pause_b)
    assert result["applied"] is True
    assert manager.current_generation_id == generation_before + 1
    assert runtime.settings.model_fable_paused == FABLE_B
    assert f"MODEL_FABLE_PAUSED={FABLE_B}" in managed.read_text(encoding="utf-8")
    await manager.close()


@pytest.mark.asyncio
async def test_a_save_refused_at_the_replace_publishes_no_generation(
    managed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, manager, factory = _runtime()
    settings_before = runtime.settings
    generation_before = manager.current_generation_id
    sha_before = _sha(managed)

    def refused(source: Path, destination: Path) -> None:
        if _same(destination, managed):
            raise PermissionError(13, "Access is denied", str(destination))
        _REAL_REPLACE(source, destination)

    monkeypatch.setattr(env_io, "_attempt_replace", refused)
    built_before = factory.built

    # A hot key: this save goes through ``replace()`` and its commit.
    with pytest.raises(SettingsFileBusyError):
        await runtime.apply_admin_config({"MODEL_FABLE_PAUSED": FABLE_B})

    # The candidate was built and thrown away; the running one is untouched.
    assert factory.built == built_before + 1
    assert runtime.settings is settings_before
    assert runtime.settings.model_fable_paused == settings_before.model_fable_paused
    assert manager.current_generation_id == generation_before
    assert _sha(managed) == sha_before
    assert not (managed.parent / ".env.tmp").exists()

    monkeypatch.setattr(env_io, "_attempt_replace", _REAL_REPLACE)
    result = await runtime.apply_admin_config({"MODEL_FABLE_PAUSED": FABLE_B})
    assert result["applied"] is True
    assert manager.current_generation_id == generation_before + 1
    assert runtime.settings.model_fable_paused == FABLE_B
    assert (managed.parent / ".env.previous").read_text(encoding="utf-8") == SEED
    await manager.close()


@pytest.mark.asyncio
async def test_a_restart_field_refused_at_the_replace_records_no_pending_restart(
    managed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other commit path: a field that needs a restart, written without a swap."""

    runtime, manager, _factory = _runtime()
    settings_before = runtime.settings
    sha_before = _sha(managed)

    def refused(source: Path, destination: Path) -> None:
        if _same(destination, managed):
            raise PermissionError(13, "Access is denied", str(destination))
        _REAL_REPLACE(source, destination)

    monkeypatch.setattr(env_io, "_attempt_replace", refused)

    with pytest.raises(SettingsFileBusyError):
        await runtime.apply_admin_config({"LOG_LEVEL": "DEBUG"})

    assert runtime._pending_fields == []
    assert runtime.settings is settings_before
    assert _sha(managed) == sha_before

    monkeypatch.setattr(env_io, "_attempt_replace", _REAL_REPLACE)
    result = await runtime.apply_admin_config({"LOG_LEVEL": "DEBUG"})
    assert result["applied"] is True
    assert result["restart"]["fields"] == ["LOG_LEVEL"]
    assert runtime._pending_fields == ["LOG_LEVEL"]
    await manager.close()
