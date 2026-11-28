"""Tests for ``mcc-apps skill-*`` CLI subcommands."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_claude_code.cli import apps_command as module
from my_claude_code.config import paths
from tests.api.support import create_test_app


@pytest.fixture
def skill_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path / "mcc"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    paths.reset_config_dir_cache()

    app = create_test_app()
    client = TestClient(app, client=("127.0.0.1", 50000))

    def call(method: str, path: str, payload=None):
        headers = {"origin": "http://127.0.0.1:8082"}
        if method == "GET":
            response = client.get(path, headers=headers)
        else:
            response = client.post(path, json=payload or {}, headers=headers)
        if response.status_code >= 400:
            raise module.AppsCommandError(response.json().get("detail", ""))
        return response.json()

    monkeypatch.setattr(module, "_call", call)
    with client:
        yield home


def test_skill_list_status_and_install_preview_apply_remove(
    skill_cli: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module.apps_command(["skill-list"])
    out = capsys.readouterr().out
    assert "Bundled skill: mcc-media" in out
    assert "shared_agents" in out
    assert "not installed" in out

    module.apps_command(["skill-status", "codex"])
    status_out = capsys.readouterr().out
    assert "Codex CLI (codex)" in status_out
    assert "aliases shared ~/.agents/skills/" in status_out

    module.apps_command(["skill-install", "claude", "--preview"])
    preview_out = capsys.readouterr().out
    assert "+ SKILL.md" in preview_out
    assert not (skill_cli / ".claude" / "skills" / "mcc-media").exists()

    module.apps_command(["skill-install", "claude"])
    inst_out = capsys.readouterr().out
    assert "Installed skill into" in inst_out
    assert (skill_cli / ".claude" / "skills" / "mcc-media" / "SKILL.md").is_file()

    module.apps_command(["skill-update", "claude", "--preview"])
    assert "Nothing to change." in capsys.readouterr().out

    module.apps_command(["skill-remove", "claude"])
    rem_out = capsys.readouterr().out
    assert "Removed skill from" in rem_out
    assert not (skill_cli / ".claude" / "skills" / "mcc-media").exists()


def test_skill_adopt_revert_and_unknown_target(
    skill_cli: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target_dir = skill_cli / ".gemini" / "skills" / "mcc-media"
    target_dir.mkdir(parents=True)
    (target_dir / "SKILL.md").write_text("custom foreign", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        module.apps_command(["skill-install", "gemini_cli"])
    assert exc_info.value.code == 1
    assert "--adopt" in capsys.readouterr().err

    module.apps_command(["skill-install", "gemini_cli", "--adopt"])
    assert "Backed up previous directory" in capsys.readouterr().out
    assert (target_dir / "scripts" / "mcc_media.py").is_file()

    module.apps_command(["skill-revert", "gemini_cli"])
    assert "Restored original folder" in capsys.readouterr().out
    assert (target_dir / "SKILL.md").read_text(encoding="utf-8") == "custom foreign"

    with pytest.raises(SystemExit) as bad_target:
        module.apps_command(["skill-status", "no_such_target"])
    assert bad_target.value.code == 1
    assert "unknown skill target" in capsys.readouterr().err
