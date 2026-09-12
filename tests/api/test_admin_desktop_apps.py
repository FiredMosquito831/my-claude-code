"""The Desktop apps group's four routes.

Every test redirects ``HOME``/``USERPROFILE``/``APPDATA``/``LOCALAPPDATA`` and
MCC's own config directory into ``tmp_path``, so nothing here can reach a real
application's configuration file even by accident.
"""

import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from my_claude_code.config.desktop_apps import DESKTOP_APPS_BY_ID
from tests.api.support import create_test_app

CODEX_DOCUMENT = """# a comment the user wrote
model = "gpt-5.6-luna"

[projects."C:/work"]
trust_level = "trusted"
"""


def _scratch_home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path / "mcc"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    # And the PATH, since 6.83.0: "installed" means the program is here, and
    # the routes read ``os.environ``. Without this the real machine's PATH
    # decided whether a card in a scratch home was installed -- which is how a
    # local run of these tests passed while CI's did not.
    binaries = home / "bin"
    binaries.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PATH", str(binaries))
    monkeypatch.chdir(tmp_path)
    return home


def _install_codex(home: Path, contents: str | None = CODEX_DOCUMENT) -> Path:
    """Install the Codex *program*, and write its document when one is wanted."""

    suffix = ".exe" if sys.platform == "win32" else ""
    executable = home / "bin" / f"codex{suffix}"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"")
    executable.chmod(0o755)
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    path = home / ".codex" / "config.toml"
    if contents is not None:
        path.write_text(contents, encoding="utf-8", newline="")
    return path


def _local_client(app):
    return TestClient(
        app, client=("127.0.0.1", 50000), headers={"origin": "http://127.0.0.1:8082"}
    )


def _remote_client(app):
    return TestClient(app, client=("10.0.0.9", 50000))


def test_the_list_route_returns_every_registered_app(monkeypatch, tmp_path):
    _scratch_home(monkeypatch, tmp_path)
    app = create_test_app()

    with _local_client(app) as client:
        body = client.get("/admin/api/desktop-apps").json()

    returned = {entry["id"] for entry in body["apps"]}
    assert returned == set(DESKTOP_APPS_BY_ID)
    for entry in body["apps"]:
        assert entry["doc_url"].startswith("https://")
        assert "probe" in entry


def test_a_not_routable_card_carries_its_reason_and_no_document(monkeypatch, tmp_path):
    """A refusal is rendered verbatim, so "can I use X?" gets a dated answer."""

    _scratch_home(monkeypatch, tmp_path)
    app = create_test_app()

    with _local_client(app) as client:
        body = client.get("/admin/api/desktop-apps").json()

    warp = next(entry for entry in body["apps"] if entry["id"] == "warp")
    assert warp["status"] == "not_routable"
    assert "127.0.0.1" in warp["unavailable_reason"]
    assert warp["probe"]["state"] == "not_routable"
    assert warp["display_path"] == ""


def test_the_claude_desktop_card_carries_the_gateway_values(monkeypatch, tmp_path):
    """A real Configure button, against the file Anthropic's MDM page names."""

    _scratch_home(monkeypatch, tmp_path)
    app = create_test_app()

    with _local_client(app) as client:
        body = client.get("/admin/api/desktop-apps").json()

    card = next(entry for entry in body["apps"] if entry["id"] == "claude_desktop")
    assert card["status"] == "servable"
    assert card["display_path"].endswith(r"configLibrary\_meta.json")
    # Reconciled with the entry proven to work on a real machine: model
    # discovery off with the models named, and no custom-headers key -- see
    # specs/CLAUDE-DESKTOP-CONFIG-REFERENCE.md.
    assert card["sidecar_keys"] == [
        "inferenceProvider",
        "inferenceGatewayBaseUrl",
        "inferenceGatewayApiKey",
        "inferenceCredentialKind",
        "modelDiscoveryEnabled",
        "inferenceModels",
    ]
    # The proxy ROOT: Claude Desktop appends /v1/messages itself.
    assert card["base_url"].startswith("http://")
    assert not card["base_url"].endswith("/v1")
    # The meaningless live warning is gone with the variable it named.
    assert card["token_env_var"] == ""


def test_plan_returns_a_diff_and_writes_nothing(monkeypatch, tmp_path):
    home = _scratch_home(monkeypatch, tmp_path)
    path = _install_codex(home)
    before = path.read_bytes()
    app = create_test_app()

    with _local_client(app) as client:
        body = client.post("/admin/api/desktop-apps/codex_desktop/plan", json={}).json()

    assert body["diff"]
    assert "model_providers.mcc" in body["diff"]
    assert body["overwritten_keys"] == ["model_provider", "model"]
    assert any("Restart" in action for action in body["actions"])
    assert path.read_bytes() == before
    assert not (home / ".codex" / "config.toml.mcc-backup").exists()


def test_plan_never_renders_a_credential(monkeypatch, tmp_path):
    home = _scratch_home(monkeypatch, tmp_path)
    _install_codex(home)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-super-secret-value")
    app = create_test_app()

    with _local_client(app) as client:
        body = client.post("/admin/api/desktop-apps/codex_desktop/plan", json={}).json()

    assert "sk-super-secret-value" not in json.dumps(body)


def test_configure_then_undo_returns_the_document_to_its_original_bytes(
    monkeypatch, tmp_path
):
    home = _scratch_home(monkeypatch, tmp_path)
    path = _install_codex(home)
    original = path.read_bytes()
    app = create_test_app()

    with _local_client(app) as client:
        applied = client.post(
            "/admin/api/desktop-apps/codex_desktop/configure", json={}
        ).json()
        assert applied["changed"]
        assert applied["probe"]["state"] == "configured"
        assert "model_providers.mcc" in path.read_text(encoding="utf-8", newline=None)

        undone = client.post(
            "/admin/api/desktop-apps/codex_desktop/undo", json={"mode": "restore"}
        ).json()

    assert undone["restored_keys"] == ["model"]
    assert path.read_bytes() == original


def test_undo_keys_only_leaves_the_users_own_keys_and_removes_mccs(
    monkeypatch, tmp_path
):
    home = _scratch_home(monkeypatch, tmp_path)
    path = _install_codex(home)
    app = create_test_app()

    with _local_client(app) as client:
        client.post("/admin/api/desktop-apps/codex_desktop/configure", json={})
        body = client.post(
            "/admin/api/desktop-apps/codex_desktop/undo", json={"mode": "keys_only"}
        ).json()

    assert body["mode"] == "keys_only"
    # The user's own default model comes back rather than being deleted.
    assert body["restored_keys"] == ["model"]
    text = path.read_text(encoding="utf-8", newline=None)
    assert 'model = "gpt-5.6-luna"' in text
    assert "model_providers.mcc" not in text
    assert "# a comment the user wrote" in text
    assert '[projects."C:/work"]' in text


def test_configure_is_idempotent_across_two_calls(monkeypatch, tmp_path):
    home = _scratch_home(monkeypatch, tmp_path)
    path = _install_codex(home)
    app = create_test_app()

    with _local_client(app) as client:
        client.post("/admin/api/desktop-apps/codex_desktop/configure", json={})
        first = path.read_bytes()
        second = client.post(
            "/admin/api/desktop-apps/codex_desktop/configure", json={}
        ).json()

    assert not second["changed"]
    assert path.read_bytes() == first


def test_a_hand_edited_owned_key_reports_as_drifted(monkeypatch, tmp_path):
    home = _scratch_home(monkeypatch, tmp_path)
    path = _install_codex(home)
    app = create_test_app()

    with _local_client(app) as client:
        client.post("/admin/api/desktop-apps/codex_desktop/configure", json={})
        path.write_text(
            path.read_text(encoding="utf-8", newline=None).replace(
                "base_url", "base_url_hand_edited"
            ),
            encoding="utf-8",
            newline="",
        )
        body = client.get("/admin/api/desktop-apps").json()

    codex = next(entry for entry in body["apps"] if entry["id"] == "codex_desktop")
    assert codex["probe"]["state"] == "drifted"


def test_an_unknown_app_is_a_404_naming_the_ones_that_exist(monkeypatch, tmp_path):
    _scratch_home(monkeypatch, tmp_path)
    app = create_test_app()

    with _local_client(app) as client:
        response = client.post("/admin/api/desktop-apps/not-an-app/plan", json={})

    assert response.status_code == 404
    assert "codex_desktop" in response.json()["detail"]


def test_a_card_with_no_document_refuses_to_be_configured(monkeypatch, tmp_path):
    """A NOT_ROUTABLE card has no file, and a write to it is a caller bug."""

    _scratch_home(monkeypatch, tmp_path)
    app = create_test_app()

    with _local_client(app) as client:
        response = client.post("/admin/api/desktop-apps/lm_studio/configure", json={})

    assert response.status_code == 409
    assert "not configured by writing a file" in response.json()["detail"]


def test_every_route_refuses_a_non_loopback_caller(monkeypatch, tmp_path):
    _scratch_home(monkeypatch, tmp_path)
    app = create_test_app()

    with _remote_client(app) as client:
        assert client.get("/admin/api/desktop-apps").status_code == 403
        for route in ("plan", "configure", "undo"):
            response = client.post(
                f"/admin/api/desktop-apps/codex_desktop/{route}", json={}
            )
            assert response.status_code == 403, route


def test_a_write_route_refuses_a_foreign_origin(monkeypatch, tmp_path):
    """The same Origin guard every other admin write route carries."""

    _scratch_home(monkeypatch, tmp_path)
    app = create_test_app()

    with TestClient(
        app, client=("127.0.0.1", 50000), headers={"origin": "https://evil.example"}
    ) as client:
        response = client.post(
            "/admin/api/desktop-apps/codex_desktop/configure", json={}
        )

    assert response.status_code == 403


def test_undo_rejects_a_mode_that_is_not_one_of_the_two(monkeypatch, tmp_path):
    _scratch_home(monkeypatch, tmp_path)
    _install_codex(tmp_path / "home")
    app = create_test_app()

    with _local_client(app) as client:
        response = client.post(
            "/admin/api/desktop-apps/codex_desktop/undo", json={"mode": "delete-it-all"}
        )

    assert response.status_code == 422


def test_an_instruction_card_resolves_the_proxy_root_it_tells_you_to_type(
    monkeypatch, tmp_path
):
    """A placeholder the reader has to substitute by hand is not an instruction."""

    _scratch_home(monkeypatch, tmp_path)
    app = create_test_app()

    with _local_client(app) as client:
        body = client.get("/admin/api/desktop-apps").json()

    card = next(entry for entry in body["apps"] if entry["id"] == "claude_desktop")
    fields = {item["label"]: item["value"] for item in card["instruction_fields"]}
    assert "{root}" not in json.dumps(card)
    # The dialog's own labels, from Anthropic's in-app configuration page.
    assert fields["Inference provider"] == "Gateway"
    assert fields["Credential kind"] == "Static API key"
    assert fields["Gateway base URL"].startswith("http://")
    assert fields["Gateway base URL"] == card["base_url"]
