"""Tests for the one function that decides where configuration lives.

Every consumer of the config directory goes through ``config_dir_path``;
``resolve_config_dir`` encodes the order (``MCC_CONFIG_DIR`` → existing
``~/.mcc`` → any existing ``~/.fcc`` → fresh ``~/.mcc``) and the four-check
legacy health probe. These tests drive it with a redirected ``HOME``.

The single most important property here is negative: **resolution never moves,
creates or abandons a directory.** A legacy home that fails a health check is
still the legacy home; only ``mcc-migrate`` ever changes which directory holds
the data.
"""

import json
import sqlite3
from pathlib import Path

from my_claude_code.config import paths


def _reset() -> None:
    paths.reset_config_dir_cache()


def test_config_dir_defaults_to_dot_mcc(tmp_path: Path) -> None:
    resolution = paths.resolve_config_dir(home=tmp_path)

    assert resolution.source == "created"
    assert resolution.path == tmp_path / ".mcc"


def test_mcc_config_dir_overrides_the_default(tmp_path: Path) -> None:
    override = tmp_path / "custom-config"
    resolution = paths.resolve_config_dir(
        env={"MCC_CONFIG_DIR": str(override)}, home=tmp_path
    )

    assert resolution.source == "env"
    assert resolution.path == override


def test_mcc_config_dir_expands_a_tilde(tmp_path: Path) -> None:
    resolution = paths.resolve_config_dir(
        env={"MCC_CONFIG_DIR": "~/custom"}, home=tmp_path
    )

    assert resolution.path == Path.home() / "custom"


def test_mcc_config_dir_skips_every_check(tmp_path: Path) -> None:
    """An explicit dir is a deliberate choice; the health check never runs."""
    (tmp_path / ".fcc").mkdir()
    (tmp_path / ".fcc" / ".env").write_text("THIS_IS_NOT_VALID_EVEN\n")
    override = tmp_path / "explicit"
    resolution = paths.resolve_config_dir(
        env={"MCC_CONFIG_DIR": str(override)}, home=tmp_path
    )

    assert resolution.source == "env"
    assert resolution.legacy_health is None


def test_existing_dot_mcc_is_used_directly(tmp_path: Path) -> None:
    (tmp_path / ".mcc").mkdir()
    resolution = paths.resolve_config_dir(home=tmp_path)

    assert resolution.source == "current"
    assert resolution.path == tmp_path / ".mcc"


def test_both_dirs_present_prefers_dot_mcc(tmp_path: Path) -> None:
    (tmp_path / ".mcc").mkdir()
    (tmp_path / ".fcc").mkdir()
    resolution = paths.resolve_config_dir(home=tmp_path)

    assert resolution.source == "current"
    assert resolution.path == tmp_path / ".mcc"
    assert "wins" in resolution.warning


def test_healthy_legacy_dir_is_used_when_no_dot_mcc(tmp_path: Path) -> None:
    legacy = tmp_path / ".fcc"
    legacy.mkdir()
    (legacy / ".env").write_text("MODEL=nvidia_nim/test\n")
    resolution = paths.resolve_config_dir(home=tmp_path)

    assert resolution.source == "legacy"
    assert resolution.uses_legacy_home
    assert resolution.legacy_health is not None
    assert resolution.legacy_health.healthy


def test_env_check_reports_build_failure(tmp_path: Path) -> None:
    """If ``Settings()`` won't build from the legacy ``.env``, it fails the check.

    ``Settings`` itself is lenient enough that almost no ``.env`` content breaks
    it, so this forces the failure to prove the check reports it rather than
    swallowing it.
    """
    from unittest.mock import patch

    legacy = tmp_path / ".fcc"
    legacy.mkdir()
    (legacy / ".env").write_text("MODEL=nvidia_nim/test\n")

    class _Boom(Exception):
        pass

    def fake_settings(*args, **kwargs):
        raise _Boom("synthetic env build failure")

    with patch("my_claude_code.config.settings.Settings", side_effect=fake_settings):
        health = paths.check_legacy_home(legacy)

    assert health is not None
    assert health.failed_check == "env"
    assert "synthetic env build failure" in health.detail


def test_legacy_dir_with_short_request_log_is_flagged_but_still_used(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / ".fcc"
    legacy.mkdir()
    (legacy / ".env").write_text("MODEL=nvidia_nim/test\n")
    db_dir = legacy / "logs"
    db_dir.mkdir()
    conn = sqlite3.connect(db_dir / "requests.db")
    conn.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    resolution = paths.resolve_config_dir(home=tmp_path)

    assert resolution.legacy_unhealthy
    assert resolution.legacy_health is not None
    assert resolution.legacy_health.failed_check == "request_log"
    # The point of the fix: a failed probe warns, it does not relocate.
    assert resolution.path == legacy
    assert resolution.uses_legacy_home
    assert not (tmp_path / ".mcc").exists()
    assert "still the config directory in use" in resolution.warning


def test_legacy_dir_with_bare_array_providers_is_flagged_but_still_used(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / ".fcc"
    legacy.mkdir()
    (legacy / ".env").write_text("MODEL=nvidia_nim/test\n")
    (legacy / "custom_providers.json").write_text(json.dumps([{"id": "x"}]))
    resolution = paths.resolve_config_dir(home=tmp_path)

    assert resolution.legacy_unhealthy
    assert resolution.legacy_health is not None
    assert resolution.legacy_health.failed_check == "custom_providers"
    assert resolution.path == legacy
    assert not (tmp_path / ".mcc").exists()


def test_request_log_path_is_under_the_resolved_dir(tmp_path: Path) -> None:
    (tmp_path / ".mcc").mkdir()
    resolution = paths.resolve_config_dir(home=tmp_path)

    assert resolution.path == tmp_path / ".mcc"
    assert (
        resolution.path / "logs" / "requests.db"
        == tmp_path / ".mcc" / "logs" / "requests.db"
    )


# --------------------------------------------------------------- never demote


def _legacy_home(tmp_path: Path) -> Path:
    legacy = tmp_path / ".fcc"
    legacy.mkdir()
    (legacy / ".env").write_text("MODEL=nvidia_nim/test\n")
    return legacy


def test_unopenable_request_log_does_not_demote_the_legacy_home(
    tmp_path: Path, monkeypatch
) -> None:
    """A locked or transiently unreadable database must not fork a fresh config.

    This is the failure 6.40.0 turned into silent data loss: one ``sqlite3``
    error from a five-second read-only open -- a lock held by a live writer, a
    short read, a filesystem hiccup -- returned ``source="created"`` and started
    the user on an empty ``~/.mcc``. Rule 2 (``~/.mcc`` wins whenever it exists)
    then made that permanent, and the keys and history in ``~/.fcc`` looked gone.
    """
    legacy = _legacy_home(tmp_path)
    logs = legacy / "logs"
    logs.mkdir()
    (logs / "requests.db").write_bytes(b"not a database at all")

    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(paths.sqlite3, "connect", explode)
    resolution = paths.resolve_config_dir(home=tmp_path)

    assert resolution.path == legacy
    assert resolution.source == "legacy"
    assert resolution.legacy_unhealthy
    assert resolution.legacy_health is not None
    assert resolution.legacy_health.failed_check == "request_log"
    assert not (tmp_path / ".mcc").exists()


def test_truncated_request_log_does_not_demote_the_legacy_home(
    tmp_path: Path,
) -> None:
    """A ``requests`` table missing columns is reported, not routed around."""
    legacy = _legacy_home(tmp_path)
    logs = legacy / "logs"
    logs.mkdir()
    connection = sqlite3.connect(logs / "requests.db")
    connection.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()

    resolution = paths.resolve_config_dir(home=tmp_path)

    assert resolution.path == legacy
    assert resolution.legacy_unhealthy
    assert not (tmp_path / ".mcc").exists()


def test_settings_that_will_not_build_do_not_demote_the_legacy_home(
    tmp_path: Path, monkeypatch
) -> None:
    """An ``.env`` a future validator rejects keeps its own directory."""
    legacy = _legacy_home(tmp_path)

    from my_claude_code.config import settings as settings_module

    def explode(*args, **kwargs):
        raise ValueError("synthetic settings failure")

    monkeypatch.setattr(settings_module, "Settings", explode)
    resolution = paths.resolve_config_dir(home=tmp_path)

    assert resolution.path == legacy
    assert resolution.legacy_unhealthy
    assert resolution.legacy_health is not None
    assert resolution.legacy_health.failed_check == "env"
    assert not (tmp_path / ".mcc").exists()


def test_unreadable_legacy_home_is_still_the_legacy_home(
    tmp_path: Path, monkeypatch
) -> None:
    """Even "cannot list the directory" does not create a replacement."""
    legacy = _legacy_home(tmp_path)

    def explode(path):
        raise OSError("permission denied")

    monkeypatch.setattr(paths.os, "scandir", explode)
    resolution = paths.resolve_config_dir(home=tmp_path)

    assert resolution.path == legacy
    assert resolution.legacy_health is not None
    assert resolution.legacy_health.failed_check == "readable"
    assert not (tmp_path / ".mcc").exists()


def test_dual_directories_warn_and_never_merge(tmp_path: Path) -> None:
    (tmp_path / ".mcc").mkdir()
    (tmp_path / ".fcc").mkdir()
    resolution = paths.resolve_config_dir(home=tmp_path)

    assert resolution.path == tmp_path / ".mcc"
    assert resolution.source == "current"
    assert "Nothing is ever merged" in resolution.warning
    assert (tmp_path / ".fcc").is_dir()


# ------------------------------------------------------- provisional answer


def test_provisional_resolution_follows_the_env_override(tmp_path: Path) -> None:
    """The pre-check answer honours ``MCC_CONFIG_DIR``.

    6.40.0 published ``~/.fcc`` unconditionally before running the legacy
    health probe, so for the duration of that probe an override -- or a
    ``~/.mcc``-only install -- resolved to a legacy home the user may not even
    have. Nothing re-entered it in practice; any future import-time caller
    would have read the wrong directory silently.
    """
    override = tmp_path / "pinned"
    resolution = paths.provisional_resolution(
        env={"MCC_CONFIG_DIR": str(override)}, home=tmp_path
    )

    assert resolution.path == override
    assert resolution.source == "env"


def test_provisional_resolution_prefers_an_existing_mcc_home(tmp_path: Path) -> None:
    (tmp_path / ".mcc").mkdir()
    assert paths.provisional_resolution(env={}, home=tmp_path).path == tmp_path / ".mcc"


def test_provisional_resolution_uses_the_legacy_home_when_it_is_the_only_one(
    tmp_path: Path,
) -> None:
    """This is the case that makes the probe re-entrant, and it still works."""
    (tmp_path / ".fcc").mkdir()
    assert paths.provisional_resolution(env={}, home=tmp_path).path == tmp_path / ".fcc"


def test_provisional_resolution_of_a_fresh_machine_is_the_new_home(
    tmp_path: Path,
) -> None:
    assert paths.provisional_resolution(env={}, home=tmp_path).path == tmp_path / ".mcc"


def test_new_config_dir_path_is_the_new_home_not_the_one_in_use(
    tmp_path: Path, monkeypatch
) -> None:
    """``new_config_dir_path`` answers "where would a migration land", always.

    Confusing it with ``config_dir_path()`` is what made the Get Started
    migrate button unreachable: for a legacy user ``config_dir_path()`` *is*
    ``~/.fcc``, so "the new home does not exist yet" was tested against a
    directory that always existed.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".fcc").mkdir()
    (tmp_path / ".fcc" / ".env").write_text("MODEL=nvidia_nim/test\n")
    _reset()

    assert paths.config_dir_path() == tmp_path / ".fcc"
    assert paths.new_config_dir_path() == tmp_path / ".mcc"
