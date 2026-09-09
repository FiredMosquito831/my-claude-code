"""A fresh machine starts, and a legacy machine moves itself once.

Until 6.65.0 the first ``mcc-server`` on a machine that had never run
``mcc-init`` met the 6.30.0 refusal and exited 1 -- the code default for
``ANTHROPIC_AUTH_TOKEN`` was ``""``, ``HOST`` was ``0.0.0.0``, and nothing on
any install path ever wrote a ``.env``. These tests pin the four cases the
server now handles for itself, and the one thing it must never do: rewrite a
configuration a user already has.
"""

import os
from pathlib import Path

import pytest

from my_claude_code.cli import first_start
from my_claude_code.cli import migrate_config_dir as migration
from my_claude_code.config import paths
from my_claude_code.config.env_template import (
    generate_proxy_auth_token,
    render_default_env,
)
from my_claude_code.config.proxy_auth import open_proxy_without_auth_error


def _home(tmp_path: Path, monkeypatch) -> Path:
    """Redirect the home directory and silence the migration liveness probe.

    The probe knocks on whatever port the legacy ``.env`` names, falling back
    to the ``Settings`` default -- which on a developer machine is the port
    their own server listens on. ``test_a_locked_legacy_home_refuses_the_start``
    exercises the refusal path deliberately instead.
    """

    monkeypatch.delenv(paths.CONFIG_DIR_ENV, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(migration, "_mcc_is_running", lambda home: "")
    return tmp_path


def _env_value(env_path: Path, key: str) -> str:
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip().strip('"').strip("'")
    raise AssertionError(f"{key} not found in {env_path}")


def _legacy_home(tmp_home: Path) -> Path:
    legacy = tmp_home / ".fcc"
    (legacy / "logs").mkdir(parents=True, exist_ok=True)
    (legacy / ".env").write_text("MODEL=nvidia_nim/test\n", encoding="utf-8")
    (legacy / "logs" / "requests.db").write_bytes(b"")
    return legacy


# ------------------------------------------------------------- fresh machine


def test_a_fresh_machine_gets_a_config_home_and_an_env(tmp_path, monkeypatch) -> None:
    home = _home(tmp_path, monkeypatch)

    notice = first_start.ensure_config_home()

    env_path = home / ".mcc" / ".env"
    assert env_path.is_file()
    assert notice
    assert str(env_path.name) in notice or ".env" in notice
    assert _env_value(env_path, "HOST") == "127.0.0.1"
    assert _env_value(env_path, "PORT") == "8082"


def test_the_written_token_is_neither_empty_nor_the_published_one(
    tmp_path, monkeypatch
) -> None:
    home = _home(tmp_path, monkeypatch)

    first_start.ensure_config_home()

    token = _env_value(home / ".mcc" / ".env", "ANTHROPIC_AUTH_TOKEN")
    assert token
    assert token != "freecc"
    assert len(token) >= 40


def test_a_fresh_config_home_starts_without_a_refusal(tmp_path, monkeypatch) -> None:
    """The whole point: the first start no longer ends in the 6.30.0 message."""

    home = _home(tmp_path, monkeypatch)

    first_start.ensure_config_home()

    env_path = home / ".mcc" / ".env"
    refusal = open_proxy_without_auth_error(
        host=_env_value(env_path, "HOST"),
        auth_token=_env_value(env_path, "ANTHROPIC_AUTH_TOKEN"),
    )
    assert refusal is None


def test_the_generated_token_is_unique_per_machine() -> None:
    assert generate_proxy_auth_token() != generate_proxy_auth_token()


def test_self_init_never_rewrites_an_existing_env(tmp_path, monkeypatch) -> None:
    """An install that emptied its token still meets the refusal it earned."""

    home = _home(tmp_path, monkeypatch)
    existing = home / ".mcc"
    existing.mkdir()
    (existing / ".env").write_text(
        "HOST=0.0.0.0\nANTHROPIC_AUTH_TOKEN=\n", encoding="utf-8"
    )

    notice = first_start.ensure_config_home()

    assert notice == ""
    assert (existing / ".env").read_text(
        encoding="utf-8"
    ) == "HOST=0.0.0.0\nANTHROPIC_AUTH_TOKEN=\n"
    assert open_proxy_without_auth_error(host="0.0.0.0", auth_token="") is not None


def test_a_config_dir_without_an_env_gets_one(tmp_path, monkeypatch) -> None:
    home = _home(tmp_path, monkeypatch)
    (home / ".mcc").mkdir()

    first_start.ensure_config_home()

    assert (home / ".mcc" / ".env").is_file()
    assert not (home / ".fcc").exists()


def test_the_env_override_outranks_everything(tmp_path, monkeypatch) -> None:
    home = _home(tmp_path, monkeypatch)
    _legacy_home(home)
    scratch = tmp_path / "elsewhere"
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(scratch))

    first_start.ensure_config_home()

    assert (scratch / ".env").is_file()
    # No migration was even considered.
    assert (home / ".fcc").is_dir()
    assert not (home / ".mcc").exists()


# ----------------------------------------------------------------- migration


def test_a_legacy_home_is_moved_once_on_the_first_start(tmp_path, monkeypatch) -> None:
    home = _home(tmp_path, monkeypatch)
    _legacy_home(home)

    notice = first_start.ensure_config_home()

    assert not (home / ".fcc").exists()
    assert (home / ".mcc" / ".env").read_text(encoding="utf-8") == (
        "MODEL=nvidia_nim/test\n"
    )
    assert (home / ".mcc" / "logs" / "requests.db").is_file()
    assert (home / ".fcc-migrated.txt").is_file()
    assert ".fcc" in notice and ".mcc" in notice


def test_the_pointer_says_what_moved_and_how_to_move_it_back(
    tmp_path, monkeypatch
) -> None:
    home = _home(tmp_path, monkeypatch)
    _legacy_home(home)

    first_start.ensure_config_home()

    pointer = (home / ".fcc-migrated.txt").read_text(encoding="utf-8")
    assert ".fcc" in pointer
    assert ".mcc" in pointer
    assert "move the data back" in pointer


def test_a_second_start_migrates_nothing_and_says_nothing(
    tmp_path, monkeypatch
) -> None:
    home = _home(tmp_path, monkeypatch)
    _legacy_home(home)

    first_start.ensure_config_home()
    before = (home / ".mcc" / ".env").read_text(encoding="utf-8")
    second = first_start.ensure_config_home()

    assert second == ""
    assert (home / ".mcc" / ".env").read_text(encoding="utf-8") == before


def test_both_homes_present_leaves_the_legacy_one_exactly_where_it_is(
    tmp_path, monkeypatch
) -> None:
    home = _home(tmp_path, monkeypatch)
    legacy = _legacy_home(home)
    (home / ".mcc").mkdir()

    notice = first_start.ensure_config_home()

    assert legacy.is_dir()
    assert (legacy / ".env").read_text(encoding="utf-8") == "MODEL=nvidia_nim/test\n"
    assert "ignored and left untouched" in notice
    assert "merged" in notice
    # ~/.mcc had no .env of its own, so one was written there and only there.
    assert (home / ".mcc" / ".env").is_file()


def test_a_locked_legacy_home_refuses_the_start(tmp_path, monkeypatch) -> None:
    """A held ``~/.fcc`` is a refusal, never a half-move and never a fresh home."""

    home = _home(tmp_path, monkeypatch)
    _legacy_home(home)
    monkeypatch.setattr(
        migration,
        "_mcc_is_running",
        lambda legacy_home: (
            "an MCC server is answering on http://127.0.0.1:8082/health"
        ),
    )

    with pytest.raises(first_start.ConfigHomeLocked) as caught:
        first_start.ensure_config_home()

    assert "Refusing to migrate" in str(caught.value)
    assert (home / ".fcc").is_dir()
    assert not (home / ".mcc").exists()


def test_the_refusal_exits_one_and_names_the_holders(
    tmp_path, monkeypatch, capsys
) -> None:
    home = _home(tmp_path, monkeypatch)
    _legacy_home(home)
    monkeypatch.setattr(
        migration,
        "_mcc_is_running",
        lambda legacy_home: "the desktop tray still holds desktop.lock",
    )

    with pytest.raises(SystemExit) as caught:
        first_start.ensure_config_home_or_exit()

    assert caught.value.code == 1
    printed = capsys.readouterr().err
    assert "Refusing to start" in printed
    assert "desktop.lock" in printed
    # The desktop app's error page names server_log; the reason has to be in it.
    written = Path(os.environ["LOG_FILE"]).read_text(encoding="utf-8", errors="replace")
    assert "Refusing to start" in written
    assert (home / ".fcc").is_dir()
    assert not (home / ".mcc").exists()


# ---------------------------------------------------------------- the banner


def test_the_first_start_notice_reaches_the_dashboard(tmp_path, monkeypatch) -> None:
    """``api`` cannot import ``cli``; the fact crosses through ``config.paths``."""

    _home(tmp_path, monkeypatch)

    assert paths.first_start_notice() == ""
    notice = first_start.ensure_config_home()
    assert paths.first_start_notice() == notice

    paths.reset_config_dir_cache()
    assert paths.first_start_notice() == ""


# ------------------------------------------------------------- the .env text


def test_the_rendered_env_is_the_template_with_one_line_changed() -> None:
    rendered = render_default_env(token="TOKEN-FOR-THIS-TEST")

    lines = [
        line
        for line in rendered.splitlines()
        if line.startswith("ANTHROPIC_AUTH_TOKEN=")
    ]
    assert lines == ['ANTHROPIC_AUTH_TOKEN="TOKEN-FOR-THIS-TEST"']
    assert "HOST=127.0.0.1" in rendered
    assert "PORT=8082" in rendered


def test_the_shipped_template_ships_no_token_at_all() -> None:
    from my_claude_code.config.env_template import load_env_template

    template = load_env_template()
    assert "ANTHROPIC_AUTH_TOKEN=\n" in template
    assert "freecc" not in template


def test_nothing_in_the_repository_is_the_token(tmp_path, monkeypatch) -> None:
    """The one property behind the whole change: no shipped password."""

    home = _home(tmp_path, monkeypatch)
    first_start.ensure_config_home()
    written = (home / ".mcc" / ".env").read_text(encoding="utf-8")
    assert "freecc" not in written


def test_serve_initialises_the_config_home_before_anything_reads_it(
    monkeypatch,
) -> None:
    """The wiring, pinned: ``serve()`` calls it, and calls it first.

    ``tests/conftest.py`` stubs this out for every other test, so without
    this one the call site could be deleted and the suite stay green.
    """

    from my_claude_code.cli import commands

    order: list[str] = []
    monkeypatch.setattr(
        commands, "ensure_config_home_or_exit", lambda: order.append("first_start")
    )
    monkeypatch.setattr(
        commands, "_bootstrap_request_log_path", lambda: order.append("request_log")
    )
    monkeypatch.setattr(
        commands, "_emit_config_dir_banner", lambda: order.append("banner")
    )
    monkeypatch.setattr(commands, "kill_all_best_effort", lambda: order.append("kill"))

    def stop_immediately() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(commands, "_migrate_legacy_env_if_missing", stop_immediately)

    commands.serve()

    assert order[:3] == ["first_start", "request_log", "banner"]
