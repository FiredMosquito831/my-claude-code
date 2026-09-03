"""Tests for the one function that decides where configuration lives.

Every consumer of the config directory goes through ``config_dir_path``;
``resolve_config_dir`` encodes the order (``MCC_CONFIG_DIR`` → existing
``~/.mcc`` → healthy ``~/.fcc`` → fresh ``~/.mcc``) and the four-check
legacy health probe. These tests drive it with a redirected ``HOME``.
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


def test_legacy_dir_with_short_request_log_is_rejected(tmp_path: Path) -> None:
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

    assert resolution.legacy_rejected
    assert resolution.legacy_health is not None
    assert resolution.legacy_health.failed_check == "request_log"


def test_legacy_dir_with_bare_array_providers_is_rejected(tmp_path: Path) -> None:
    legacy = tmp_path / ".fcc"
    legacy.mkdir()
    (legacy / ".env").write_text("MODEL=nvidia_nim/test\n")
    (legacy / "custom_providers.json").write_text(json.dumps([{"id": "x"}]))
    resolution = paths.resolve_config_dir(home=tmp_path)

    assert resolution.legacy_rejected
    assert resolution.legacy_health is not None
    assert resolution.legacy_health.failed_check == "custom_providers"


def test_request_log_path_is_under_the_resolved_dir(tmp_path: Path) -> None:
    (tmp_path / ".mcc").mkdir()
    resolution = paths.resolve_config_dir(home=tmp_path)

    assert resolution.path == tmp_path / ".mcc"
    assert (
        resolution.path / "logs" / "requests.db"
        == tmp_path / ".mcc" / "logs" / "requests.db"
    )
