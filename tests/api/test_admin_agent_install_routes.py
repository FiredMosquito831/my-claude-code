"""The Install button's routes (7.102.0, spec N2): guards, plan, job, listing.

Every program a route here can find is a fake in a temp ``bin`` on a PATH
the test owns; nothing is downloaded and nothing real is installed.
"""

import os
import threading
import time
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api import admin_harness_routes
from my_claude_code.application import agent_installer
from my_claude_code.application.agent_installer import Machine, NodeStatus
from my_claude_code.config.harnesses import harness_spec
from my_claude_code.config.install_methods import InstallOs
from tests.api.support import create_test_app
from tests.support.fake_programs import launcher_name, write_fake_program

LOCAL_ORIGIN = {"Origin": "http://127.0.0.1:8082"}
NODE_OK = NodeStatus(
    state="ok",
    node_path="/fake/node",
    node_version="v24.22.0",
    npm_path="/fake/npm",
    npm_version="11.0.0",
    prefix="/fake/prefix",
    prefix_writable=True,
)


@pytest.fixture
def machine(monkeypatch, tmp_path) -> Machine:
    """A Windows machine with a PATH holding only the test's own bin."""

    here = Machine(InstallOs.WINDOWS, "x64")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "ProgramFiles"))
    monkeypatch.setattr(agent_installer, "_registry_path", lambda environ: "")
    monkeypatch.setattr(agent_installer, "current_machine", lambda environ=None: here)
    monkeypatch.setattr(
        agent_installer, "detect_node", lambda machine, environ, cached=True: NODE_OK
    )
    monkeypatch.setattr(
        agent_installer,
        "_interpreter_path",
        lambda interpreter, machine, environ: f"C:/fake/{interpreter.value}.exe",
    )
    monkeypatch.setattr(
        agent_installer,
        "_program_available",
        lambda names, environ: f"C:/fake/{names[0]}.exe",
    )
    return here


def _client(app) -> TestClient:
    return TestClient(app, client=("127.0.0.1", 50000))


def _plan(client: TestClient, target: str, method: str) -> dict:
    response = client.post(
        f"/admin/api/agent-installs/{target}/plan", json={"method": method}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _wait(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + 30
    while True:
        body = client.get(f"/admin/api/agent-installs/jobs/{job_id}").json()
        if not body["running"] or time.monotonic() > deadline:
            return body
        time.sleep(0.05)


def test_agent_install_routes_require_confirm_token_and_local_origin(machine):
    app = create_test_app()
    with _client(app) as client:
        plan = _plan(client, "pi", "native")
        body = {"method": "native", "plan_token": plan["plan_token"], "dry_run": True}
        assert plan["confirm"] == "install-pi"

        # No confirm token: 400, and the message says what to send.
        missing = client.post(
            "/admin/api/agent-installs/pi/run", json=body, headers=LOCAL_ORIGIN
        )
        assert missing.status_code == 400
        assert "confirm=install-pi" in missing.json()["detail"]
        # Another card's token is not this card's.
        wrong = client.post(
            "/admin/api/agent-installs/pi/run?confirm=install-claude",
            json=body,
            headers=LOCAL_ORIGIN,
        )
        assert wrong.status_code == 400
        # No Origin (a bare curl): 403.
        bare = client.post(
            "/admin/api/agent-installs/pi/run?confirm=install-pi", json=body
        )
        assert bare.status_code == 403
        # A remote page's Origin: 403.
        remote = client.post(
            "/admin/api/agent-installs/pi/run?confirm=install-pi",
            json=body,
            headers={"Origin": "https://evil.example"},
        )
        assert remote.status_code == 403
        # All three: the job starts (a dry run here).
        ok = client.post(
            "/admin/api/agent-installs/pi/run?confirm=install-pi",
            json=body,
            headers=LOCAL_ORIGIN,
        )
        assert ok.status_code == 200, ok.text
        job = _wait(client, ok.json()["job_id"])
        assert job["result"]["status"] == "dry_run"

    remote_client = TestClient(app, client=("10.0.0.9", 50000))
    with remote_client:
        assert remote_client.get("/admin/api/agent-installs").status_code == 403


def test_agent_install_run_refuses_a_changed_plan(machine, tmp_path, monkeypatch):
    app = create_test_app()
    with _client(app) as client:
        plan = _plan(client, "gemini_cli", "npm")
        assert "Uses your Node.js v24.22.0" in plan["node"]["text"]

        # The machine moved: Node.js is gone since the page showed the plan.
        monkeypatch.setattr(
            agent_installer,
            "detect_node",
            lambda machine, environ, cached=True: NodeStatus(state="missing"),
        )
        changed = client.post(
            "/admin/api/agent-installs/gemini_cli/run?confirm=install-gemini_cli",
            json={"method": "npm", "plan_token": plan["plan_token"], "dry_run": True},
            headers=LOCAL_ORIGIN,
        )
        assert changed.status_code == 409
        assert "The plan changed" in changed.json()["detail"]

        # A made-up token is refused the same way.
        forged = client.post(
            "/admin/api/agent-installs/gemini_cli/run?confirm=install-gemini_cli",
            json={"method": "npm", "plan_token": "0" * 64, "dry_run": True},
            headers=LOCAL_ORIGIN,
        )
        assert forged.status_code == 409

        # The new plan says Node.js comes first, and its token runs.
        fresh = _plan(client, "gemini_cli", "npm")
        assert fresh["node"]["install"] is True
        assert fresh["plan_token"] != plan["plan_token"]
        ok = client.post(
            "/admin/api/agent-installs/gemini_cli/run?confirm=install-gemini_cli",
            json={"method": "npm", "plan_token": fresh["plan_token"], "dry_run": True},
            headers=LOCAL_ORIGIN,
        )
        assert ok.status_code == 200


def test_a_second_run_while_one_is_running_is_409(machine, monkeypatch):
    release = threading.Event()

    def fake_download(url, hosts, destination, *, cancel):
        destination.write_text("Write-Output hi", encoding="utf-8")
        return agent_installer.Downloaded(destination, "0" * 64, 15, url)

    def held_spawn(self, argv, *, timeout, env, cwd=None):
        release.wait(30)
        return 0

    monkeypatch.setattr(agent_installer, "download", fake_download)
    monkeypatch.setattr(agent_installer._Runner, "_spawn", held_spawn)
    app = create_test_app()
    with _client(app) as client:
        first_plan = _plan(client, "pi", "native")
        second_plan = _plan(client, "claude", "native")
        first = client.post(
            "/admin/api/agent-installs/pi/run?confirm=install-pi",
            json={"method": "native", "plan_token": first_plan["plan_token"]},
            headers=LOCAL_ORIGIN,
        )
        assert first.status_code == 200, first.text
        try:
            second = client.post(
                "/admin/api/agent-installs/claude/run?confirm=install-claude",
                json={"method": "native", "plan_token": second_plan["plan_token"]},
                headers=LOCAL_ORIGIN,
            )
            listing = client.get("/admin/api/agent-installs").json()
        finally:
            release.set()
        _wait(client, first.json()["job_id"])
    assert second.status_code == 409
    assert "One install runs at a time" in second.json()["detail"]
    assert listing["job"]["target"] == "pi"


@pytest.mark.spawns_process
@pytest.mark.local_serial
@pytest.mark.parametrize(
    ("harness_id", "name"),
    [
        ("claude", "claude"),
        ("commandcode_cli", "command-code"),
        ("commandcode_cli", "cmdc"),
        ("kimi_code", "kimi-cli"),
        ("pi", "pi"),
    ],
)
def test_installed_agent_offers_no_install(machine, tmp_path, harness_id, name):
    """Found under its binary or ANY alias, a CLI shows its path and version
    and no button -- and a plan for it is refused (no second copy)."""

    bin_dir = Path(os.environ["PATH"])
    write_fake_program(bin_dir, name, outputs={"--version": f"{name} 9.9.9"})
    app = create_test_app()
    with _client(app) as client:
        listing = client.get("/admin/api/agent-installs").json()
        entry = next(a for a in listing["agents"] if a["id"] == harness_id)
        assert entry["installed"] is True
        assert entry["offer_install"] is False
        assert os.path.normcase(entry["path"]) == os.path.normcase(
            str(bin_dir / launcher_name(name))
        )
        assert entry["version"] == f"{name} 9.9.9"
        # Kimi has no button at all (decision 6); the others have methods
        # but every one refuses an installed program.
        for method in entry["methods"]:
            assert method["runnable"] is False
            assert "already installed" in method["refused"]

        harnesses = client.get("/admin/api/harnesses").json()["harnesses"]
        card = next(h for h in harnesses if h["id"] == harness_id)
        assert card["installed"] is True


def test_windows_card_uses_the_windows_method_and_aliases(
    machine, monkeypatch, tmp_path
):
    """Spec §1 item 3: the card showed Windows users the POSIX line for Pi,
    Goose and Droid, and read "not installed" for a cmdc-only Command Code."""

    found = str(tmp_path / "bin" / "cmdc.cmd")
    monkeypatch.setattr(
        admin_harness_routes, "sys", types.SimpleNamespace(platform="win32")
    )
    monkeypatch.setattr(
        admin_harness_routes.shutil,
        "which",
        lambda name, *args, **kwargs: found if name == "cmdc" else None,
    )
    app = create_test_app()
    with _client(app) as client:
        cards = {
            h["id"]: h for h in client.get("/admin/api/harnesses").json()["harnesses"]
        }
        listing = client.get("/admin/api/agent-installs").json()
    for harness_id in ("pi", "goose", "droid"):
        spec = harness_spec(harness_id)
        assert spec.install_hint_windows is not None
        assert cards[harness_id]["install_hint"] == spec.install_hint_windows
    assert cards["commandcode_cli"]["installed"] is True
    assert cards["commandcode_cli"]["binary_path"] == os.path.abspath(found)

    by_id = {entry["id"]: entry for entry in listing["agents"]}
    assert listing["os"] == "windows"
    pi = by_id["pi"]
    assert pi["recommended"] == "native"
    assert [m["os"] for m in pi["methods"]] == ["windows", "windows"]
    assert pi["methods"][0]["copy_command"] == (
        'powershell -c "irm https://pi.dev/install.ps1 | iex"'
    )
    # The Windows Goose line is Goose's own PowerShell script, not curl | bash.
    assert by_id["goose"]["methods"][0]["url"].endswith("download_cli.ps1")
    # OpenCode on Windows: npm (WSL is not installed into from Windows).
    assert [m["kind"] for m in by_id["opencode"]["methods"]] == ["npm"]


def test_the_listing_offers_install_a_picker_and_a_link_where_no_download(machine):
    app = create_test_app()
    with _client(app) as client:
        listing = client.get("/admin/api/agent-installs").json()
    by_id = {entry["id"]: entry for entry in listing["agents"]}

    claude = by_id["claude"]
    assert claude["installed"] is False
    assert claude["offer_install"] is True
    assert claude["recommended"] == "native"
    assert [m["id"] for m in claude["methods"]] == ["native", "winget", "npm"]
    assert (
        claude["methods"][0]["copy_command"]
        == "irm https://claude.ai/install.ps1 | iex"
    )
    assert claude["methods"][0]["checked_on"] == "2026-10-19"

    # Kimi: no button until the mcc-kimi check passes (decision 6).
    kimi = by_id["kimi_code"]
    assert kimi["methods"] == []
    assert kimi["offer_install"] is False
    assert "has not yet checked that mcc-kimi works" in kimi["unavailable_reason"]
    # Antigravity: never a button.
    assert by_id["antigravity"]["methods"] == []
    # Warp: a link to the vendor's page, never a download (decision 8).
    warp = by_id["warp"]
    assert warp["offer_install"] is False
    assert warp["link"] == "https://www.warp.dev/download"
    # LM Studio on Windows: the vendor publishes a direct download.
    assert by_id["lm_studio"]["recommended"] == "download"
    # The desktop twin of a CLI card is installed from that card.
    assert by_id["crush_desktop"]["same_as"] == "crush"
    assert by_id["crush_desktop"]["methods"] == []


def test_job_routes_answer_unknown_ids_with_404(machine):
    app = create_test_app()
    with _client(app) as client:
        assert client.get("/admin/api/agent-installs/jobs/nope").status_code == 404
        assert (
            client.post("/admin/api/agent-installs/jobs/nope/cancel").status_code == 404
        )
        assert (
            client.post(
                "/admin/api/agent-installs/no-such-agent/plan", json={"method": "npm"}
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/admin/api/agent-installs/claude/plan", json={"method": "nope"}
            ).status_code
            == 400
        )


def test_the_plan_states_elevation_profile_changes_and_bins(machine):
    app = create_test_app()
    with _client(app) as client:
        winget = _plan(client, "claude", "winget")
        commandcode = _plan(client, "commandcode_cli", "npm")
    assert winget["runnable"] is True
    assert winget["elevation"][0].startswith(
        "Windows will ask for administrator permission"
    )
    assert any("agreements" in change for change in winget["changes"])
    assert commandcode["bins"] == ["command-code", "cmd", "cmdc", "commandcode"]
    assert commandcode["node"]["bound"] == "Node.js 22 or newer"
