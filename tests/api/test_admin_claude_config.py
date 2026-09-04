"""Tests for the Configure Claude Code settings-editor API.

The API's two jobs are to never hand the browser a credential and to never
write outside a Claude Code settings file. Both are tested by trying to make it
do those things.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.api.support import create_test_app


@pytest.fixture(autouse=True)
def _no_real_wsl(monkeypatch):
    """Never shell out to ``wsl.exe`` to answer a settings-discovery question.

    ``discover_settings_files`` enumerates WSL distributions, so every request
    to these routes launched ``wsl.exe --list --quiet`` on the developer's
    machine: real machine state queried as a side effect of an assertion about
    a JSON document.
    """

    from my_claude_code.config import claude_discovery

    monkeypatch.setattr(claude_discovery, "wsl_distributions", lambda: ())


def _local_client(app):
    return TestClient(app, client=("127.0.0.1", 50000))


def _set_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)


def _settings_file(tmp_path: Path, document: dict[str, object]) -> Path:
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


class TestCatalogRoute:
    def test_catalog_describes_the_whole_surface(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        response = _local_client(create_test_app()).get(
            "/admin/api/claude-config/catalog"
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body["entries"]) > 400
        assert body["categories"]

    def test_every_entry_carries_the_control_the_page_needs(
        self, monkeypatch, tmp_path
    ):
        _set_home(monkeypatch, tmp_path)
        body = (
            _local_client(create_test_app())
            .get("/admin/api/claude-config/catalog")
            .json()
        )
        for entry in body["entries"]:
            assert entry["control"]
            assert entry["name"]
            assert "editable" in entry
        selects = [e for e in body["entries"] if e["control"] == "enum"]
        assert all(e.get("values") for e in selects)

    def test_a_remote_caller_is_refused(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        remote = TestClient(create_test_app(), client=("203.0.113.10", 50000))
        assert remote.get("/admin/api/claude-config/catalog").status_code == 403


class TestDocumentRoute:
    def test_reports_a_missing_file_without_failing(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        body = (
            _local_client(create_test_app())
            .get("/admin/api/claude-config/document")
            .json()
        )
        assert body["exists"] is False
        assert body["parsed"] is True
        assert body["is_default"] is True
        assert body["values"] == {}

    def test_returns_the_values_the_file_sets(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        _settings_file(tmp_path, {"model": "claude-opus-5", "env": {"DEBUG": "1"}})
        body = (
            _local_client(create_test_app())
            .get("/admin/api/claude-config/document")
            .json()
        )
        assert body["values"]["model"] == "claude-opus-5"
        assert body["values"]["env.DEBUG"] == "1"

    def test_a_credential_never_reaches_the_browser(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        _settings_file(tmp_path, {"env": {"ANTHROPIC_API_KEY": "sk-ant-secret"}})
        response = _local_client(create_test_app()).get(
            "/admin/api/claude-config/document"
        )
        assert "sk-ant-secret" not in response.text
        assert response.json()["values"]["env.ANTHROPIC_API_KEY"] == "********"

    def test_a_broken_file_reports_the_parse_error(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = tmp_path / ".claude" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken", encoding="utf-8")

        body = (
            _local_client(create_test_app())
            .get("/admin/api/claude-config/document")
            .json()
        )
        assert body["parsed"] is False
        assert body["error"]


class TestPathGuard:
    """The API edits Claude Code settings files. It is not a JSON file writer."""

    def test_a_relative_path_is_refused(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        response = _local_client(create_test_app()).get(
            "/admin/api/claude-config/document",
            params={"path": ".claude/settings.json"},
        )
        assert response.status_code == 400

    def test_an_arbitrary_file_is_refused(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        target = tmp_path / ".claude" / "id_rsa"
        response = _local_client(create_test_app()).get(
            "/admin/api/claude-config/document", params={"path": str(target)}
        )
        assert response.status_code == 400

    def test_a_settings_json_outside_a_claude_directory_is_refused(
        self, monkeypatch, tmp_path
    ):
        _set_home(monkeypatch, tmp_path)
        target = tmp_path / "vscode" / "settings.json"
        response = _local_client(create_test_app()).get(
            "/admin/api/claude-config/document", params={"path": str(target)}
        )
        assert response.status_code == 400

    def test_a_project_settings_file_is_accepted(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        target = tmp_path / "repo" / ".claude" / "settings.json"
        target.parent.mkdir(parents=True)
        target.write_text('{"model": "sonnet"}', encoding="utf-8")

        body = (
            _local_client(create_test_app())
            .get("/admin/api/claude-config/document", params={"path": str(target)})
            .json()
        )
        assert body["is_default"] is False
        assert body["values"]["model"] == "sonnet"

    def test_settings_local_json_is_accepted(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        target = tmp_path / "repo" / ".claude" / "settings.local.json"
        target.parent.mkdir(parents=True)
        target.write_text("{}", encoding="utf-8")

        response = _local_client(create_test_app()).get(
            "/admin/api/claude-config/document", params={"path": str(target)}
        )
        assert response.status_code == 200

    def test_the_write_route_applies_the_same_guard(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        response = _local_client(create_test_app()).post(
            "/admin/api/claude-config/apply",
            json={
                "path": str(tmp_path / "evil.json"),
                "changes": [{"name": "model", "op": "set", "value": "x"}],
            },
        )
        assert response.status_code == 400


class TestPlanRoute:
    def test_planning_writes_nothing(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = _settings_file(tmp_path, {"model": "sonnet"})
        before = path.read_text(encoding="utf-8")

        response = _local_client(create_test_app()).post(
            "/admin/api/claude-config/plan",
            json={
                "changes": [{"name": "model", "op": "set", "value": "claude-opus-5"}]
            },
        )
        assert response.status_code == 200
        assert response.json()["changes"][0]["before"] == "sonnet"
        assert path.read_text(encoding="utf-8") == before

    def test_a_falsey_presence_variable_is_rewritten_to_a_removal(
        self, monkeypatch, tmp_path
    ):
        _set_home(monkeypatch, tmp_path)
        _settings_file(tmp_path, {"env": {"DISABLE_TELEMETRY": "1"}})

        body = (
            _local_client(create_test_app())
            .post(
                "/admin/api/claude-config/plan",
                json={
                    "changes": [
                        {"name": "env.DISABLE_TELEMETRY", "op": "set", "value": "0"}
                    ]
                },
            )
            .json()
        )
        assert body["changes"][0]["op"] == "unset"
        assert body["changes"][0]["warnings"]

    def test_an_unparseable_target_is_a_conflict_not_a_crash(
        self, monkeypatch, tmp_path
    ):
        _set_home(monkeypatch, tmp_path)
        path = tmp_path / ".claude" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken", encoding="utf-8")

        response = _local_client(create_test_app()).post(
            "/admin/api/claude-config/plan",
            json={"changes": [{"name": "model", "op": "set", "value": "x"}]},
        )
        assert response.status_code == 409

    def test_an_unknown_op_is_rejected_by_validation(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        response = _local_client(create_test_app()).post(
            "/admin/api/claude-config/plan",
            json={"changes": [{"name": "model", "op": "delete", "value": None}]},
        )
        assert response.status_code == 422


class TestApplyRoute:
    def test_applying_writes_the_file(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = _settings_file(tmp_path, {"theme": "dark"})

        response = _local_client(create_test_app()).post(
            "/admin/api/claude-config/apply",
            json={
                "changes": [
                    {"name": "model", "op": "set", "value": "claude-opus-5"},
                    {"name": "theme", "op": "unset"},
                ]
            },
        )
        assert response.status_code == 200
        assert json.loads(path.read_text(encoding="utf-8")) == {
            "model": "claude-opus-5"
        }
        assert len(response.json()["applied"]) == 2

    def test_applying_nothing_is_not_an_error(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        _settings_file(tmp_path, {"theme": "dark"})

        response = _local_client(create_test_app()).post(
            "/admin/api/claude-config/apply",
            json={"changes": [{"name": "theme", "op": "set", "value": "dark"}]},
        )
        assert response.status_code == 200
        assert response.json()["applied"] == []

    def test_the_response_masks_a_credential_it_just_wrote(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        _settings_file(tmp_path, {})

        response = _local_client(create_test_app()).post(
            "/admin/api/claude-config/apply",
            json={
                "changes": [
                    {
                        "name": "env.ANTHROPIC_API_KEY",
                        "op": "set",
                        "value": "sk-ant-written",
                    }
                ]
            },
        )
        assert response.status_code == 200
        assert "sk-ant-written" not in response.text
        stored = json.loads(
            (tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8")
        )
        assert stored["env"]["ANTHROPIC_API_KEY"] == "sk-ant-written"

    def test_a_backup_exists_after_the_first_write(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = _settings_file(tmp_path, {"theme": "dark"})

        _local_client(create_test_app()).post(
            "/admin/api/claude-config/apply",
            json={"changes": [{"name": "theme", "op": "set", "value": "light"}]},
        )
        backup = path.with_name(path.name + ".fcc-backup")
        assert json.loads(backup.read_text(encoding="utf-8")) == {"theme": "dark"}
