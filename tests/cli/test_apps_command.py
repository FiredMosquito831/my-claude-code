"""``mcc-apps``, driven against a real app instance over the loopback API.

The command is a client of the four admin routes, so the tests give it a real
server -- a ``TestClient``-backed transport standing in for ``urlopen`` -- and
a scratch home. Nothing here can reach a real application's configuration file:
``HOME``/``USERPROFILE``/``APPDATA``/``LOCALAPPDATA`` and ``MCC_CONFIG_DIR`` all
point inside ``tmp_path``.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_claude_code.cli import apps_command as module
from tests.api.support import create_test_app

CODEX_DOCUMENT = """# a comment the user wrote
model = "gpt-5.6-luna"

[projects."C:/work"]
trust_level = "trusted"
"""


@pytest.fixture
def scratch(monkeypatch, tmp_path: Path):
    """A scratch home, an installed Codex, and the command wired to a test app."""

    home = tmp_path / "home"
    (home / "AppData" / "Local" / "OpenAI" / "Codex").mkdir(parents=True)
    (home / ".codex").mkdir(parents=True)
    document = home / ".codex" / "config.toml"
    document.write_text(CODEX_DOCUMENT, encoding="utf-8", newline="")

    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path / "mcc"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.chdir(tmp_path)

    app = create_test_app()
    client = TestClient(app, client=("127.0.0.1", 50000))

    def call(method: str, path: str, payload=None):
        """Stand in for the command's one HTTP helper, against the real app."""

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
        yield document


def test_list_names_every_app_and_its_state(scratch, capsys):
    module.apps_command(["list"])
    out = capsys.readouterr().out

    assert "codex_desktop" in out
    assert "installed, not configured" in out
    # A NOT_ROUTABLE row is still listed: silence would be the wrong answer to
    # "can I use this?".
    assert "warp" in out
    assert "not routable" in out


def test_status_reports_the_file_the_owned_key_and_the_export(scratch, capsys):
    module.apps_command(["status", "codex_desktop"])
    out = capsys.readouterr().out

    assert "config.toml" in out
    assert "model_providers.mcc" in out
    assert "model_provider, model" in out
    assert "MCC_AUTH_TOKEN" in out
    assert "NOT exported" in out


def test_status_of_a_not_routable_app_gives_the_dated_reason(scratch, capsys):
    module.apps_command(["status", "warp"])
    out = capsys.readouterr().out

    assert "why not" in out
    assert "127.0.0.1" in out


def test_status_of_claude_desktop_names_the_file_it_writes(scratch, capsys):
    """It has a Configure button now, so status reports a file, not a dialog."""

    module.apps_command(["status", "claude_desktop"])
    out = capsys.readouterr().out

    assert "config file" in out
    assert "configLibrary" in out


def test_configure_preview_prints_the_diff_and_writes_nothing(scratch, capsys):
    before = scratch.read_bytes()
    module.apps_command(["configure", "codex_desktop", "--preview"])
    out = capsys.readouterr().out

    assert "model_providers.mcc" in out
    assert "Replaces existing values at: model_provider, model" in out
    assert "--restore" in out
    assert scratch.read_bytes() == before


def test_configure_writes_and_names_the_export_and_the_restart(scratch, capsys):
    module.apps_command(["configure", "codex_desktop"])
    out = capsys.readouterr().out

    assert "Wrote " in out
    assert "Backed up your original to" in out
    assert "MCC_AUTH_TOKEN" in out
    assert "Restart" in out
    assert "model_providers.mcc" in scratch.read_text(encoding="utf-8", newline=None)


def test_configure_twice_reports_no_change(scratch, capsys):
    module.apps_command(["configure", "codex_desktop"])
    capsys.readouterr()
    module.apps_command(["configure", "codex_desktop"])

    assert "already said this" in capsys.readouterr().out


def test_undo_restore_puts_back_the_users_model(scratch, capsys):
    original = scratch.read_bytes()
    module.apps_command(["configure", "codex_desktop"])
    capsys.readouterr()

    module.apps_command(["undo", "codex_desktop", "--restore"])
    out = capsys.readouterr().out

    assert "Restored your original values at: model" in out
    assert scratch.read_bytes() == original


def test_undo_without_restore_leaves_the_users_other_keys(scratch, capsys):
    module.apps_command(["configure", "codex_desktop"])
    capsys.readouterr()

    module.apps_command(["undo", "codex_desktop"])
    out = capsys.readouterr().out
    text = scratch.read_text(encoding="utf-8", newline=None)

    assert "Removed MCC's keys" in out
    # Keys-only puts back a value MCC replaced; it deletes only what MCC
    # created. Before 6.56.0 it deleted the user's own model line as well.
    assert "Restored your original values at: model" in out
    assert 'model = "gpt-5.6-luna"' in text
    assert "model_providers.mcc" not in text
    assert "# a comment the user wrote" in text


def test_an_unknown_app_exits_non_zero_and_lists_the_real_ones(scratch, capsys):
    with pytest.raises(SystemExit) as exit_info:
        module.apps_command(["status", "not-an-app"])

    assert exit_info.value.code == 1
    assert "codex_desktop" in capsys.readouterr().err


def test_configuring_a_not_routable_card_refuses_with_what_to_do_instead(
    scratch, capsys
):
    with pytest.raises(SystemExit):
        module.apps_command(["configure", "lm_studio"])

    assert "mcc-apps status lm_studio" in capsys.readouterr().err


def test_the_command_reports_a_server_that_is_not_running(monkeypatch, capsys):
    """The most likely failure, and the one worth a sentence rather than a trace."""

    from urllib.error import URLError

    def refuse(*_args, **_kwargs):
        raise URLError("connection refused")

    monkeypatch.setattr(module, "urlopen", refuse)
    with pytest.raises(SystemExit):
        module.apps_command(["list"])

    assert "mcc-server" in capsys.readouterr().err


def test_the_command_sends_a_local_origin_so_the_admin_guard_applies(monkeypatch):
    """The guard stays one rule; MCC's own CLI is not waved through it."""

    seen: dict[str, str] = {}

    class FakeResponse:
        def read(self):
            return json.dumps({"apps": []}).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def capture(request, timeout):
        del timeout
        seen.update(dict(request.header_items()))
        return FakeResponse()

    monkeypatch.setattr(module, "urlopen", capture)
    module._call("GET", "/admin/api/desktop-apps")

    origin = seen.get("Origin") or seen.get("origin")
    assert origin is not None
    assert origin.startswith("http://127.0.0.1") or origin.startswith(
        "http://localhost"
    )
