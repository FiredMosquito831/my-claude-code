"""Tests for ``GET /admin/api/skills`` and ``POST /admin/api/skills/{target_id}/{plan,apply}``."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_claude_code.config import paths
from tests.api.support import create_test_app


@pytest.fixture
def skill_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    for key in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(key, str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path / "mcc"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    paths.reset_config_dir_cache()
    return home


def _local_client() -> TestClient:
    return TestClient(
        create_test_app(),
        client=("127.0.0.1", 50000),
        headers={"origin": "http://127.0.0.1:8082"},
    )


def _remote_client() -> TestClient:
    return TestClient(create_test_app(), client=("203.0.113.10", 50000))


def test_skill_routes_require_loopback_admin(skill_home: Path) -> None:
    with _remote_client() as client:
        assert client.get("/admin/api/skills").status_code in (401, 403)
        assert client.post(
            "/admin/api/skills/shared_agents/plan", json={"action": "install"}
        ).status_code in (401, 403)
        assert client.post(
            "/admin/api/skills/shared_agents/apply", json={"action": "install"}
        ).status_code in (401, 403)


def test_list_plan_apply_remove_and_errors(skill_home: Path) -> None:
    with _local_client() as client:
        listing = client.get("/admin/api/skills")
        assert listing.status_code == 200
        body = listing.json()
        assert body["skill"]["name"] == "mcc-media"
        assert body["skill"]["total_bytes"] > 0
        assert "SKILL.md" in body["skill"]["files"]
        by_id = {t["id"]: t for t in body["targets"]}
        assert by_id["shared_agents"]["state"] == "absent"
        assert "codex" in by_id["shared_agents"]["shared_with"]
        assert by_id["codex"]["aliases_shared_agents"] is True

        # 404 on unknown target_id.
        assert (
            client.post(
                "/admin/api/skills/unknown_target/plan", json={"action": "install"}
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/admin/api/skills/unknown_target/apply", json={"action": "install"}
            ).status_code
            == 404
        )

        # Plan without mutating disk.
        plan = client.post(
            "/admin/api/skills/shared_agents/plan", json={"action": "install"}
        )
        assert plan.status_code == 200
        assert "SKILL.md" in plan.json()["files_to_create"]
        assert not (skill_home / ".agents" / "skills" / "mcc-media").exists()

        # Apply install.
        applied = client.post(
            "/admin/api/skills/shared_agents/apply", json={"action": "install"}
        )
        assert applied.status_code == 200
        assert applied.json()["changed"] is True
        assert applied.json()["target"]["state"] == "installed"
        assert (skill_home / ".agents" / "skills" / "mcc-media" / "SKILL.md").is_file()

        # Modify on disk -> drifted -> 409 without force -> succeeds with force.
        (skill_home / ".agents" / "skills" / "mcc-media" / "SKILL.md").write_text(
            "edited", encoding="utf-8"
        )
        refused = client.post(
            "/admin/api/skills/shared_agents/apply", json={"action": "remove"}
        )
        assert refused.status_code == 409
        assert "force" in refused.json()["detail"]

        removed = client.post(
            "/admin/api/skills/shared_agents/apply",
            json={"action": "remove", "force": True},
        )
        assert removed.status_code == 200
        assert removed.json()["changed"] is True
        assert removed.json()["target"]["state"] == "revertable"

        reverted = client.post(
            "/admin/api/skills/shared_agents/apply", json={"action": "revert"}
        )
        assert reverted.status_code == 200
        assert reverted.json()["restored_from_backup"] is True
        assert (skill_home / ".agents" / "skills" / "mcc-media" / "SKILL.md").read_text(
            encoding="utf-8"
        ) == "edited"
