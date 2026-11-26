"""Tests for Tier 2 CommandCodeStructuredDriver & Hub structured -> PTY switch."""

import asyncio
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.app import create_app
from my_claude_code.api.ports import ApiServices
from my_claude_code.config.paths import reset_config_dir_cache
from my_claude_code.config.settings import Settings
from my_claude_code.runtime.application import ApplicationRuntime
from my_claude_code.runtime.provider_manager import ProviderRuntimeManager
from my_claude_code.runtime.remote import commandcode_driver
from my_claude_code.runtime.remote.commandcode_driver import (
    CommandCodeStructuredDriver,
    build_commandcode_env,
    build_commandcode_turn_argv,
)
from my_claude_code.runtime.remote.hub import RemoteRuntimeHub, driver_tier_for_harness


def _write_fake_cli(directory: Path, name: str, script_body: str) -> str:
    py_path = directory / f"{name}.py"
    py_path.write_text(script_body, encoding="utf-8")
    if sys.platform == "win32":
        wrapper = directory / f"{name}.cmd"
        wrapper.write_text(f'@"{sys.executable}" -u "{py_path}" %*\n', encoding="utf-8")
        return str(wrapper)
    wrapper = directory / name
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" -u "{py_path}" "$@"\n', encoding="utf-8"
    )
    wrapper.chmod(0o755)
    return str(wrapper)


def test_commandcode_argv_modes_resume_fork_and_no_acp() -> None:
    ask_argv = build_commandcode_turn_argv(
        commandcode_bin="command-code",
        prompt="audit files",
        permission_mode="ask",
    )
    assert "acp" not in ask_argv
    assert "--acp" not in ask_argv
    assert "--permission-mode" in ask_argv
    assert ask_argv[ask_argv.index("--permission-mode") + 1] == "plan"
    assert "--resume" not in ask_argv

    edits_argv = build_commandcode_turn_argv(
        commandcode_bin="command-code",
        prompt="fix bug",
        permission_mode="auto_edits",
        external_session_id="cc-42",
    )
    assert edits_argv[edits_argv.index("--permission-mode") + 1] == "accept-edits"
    assert edits_argv[edits_argv.index("--resume") + 1] == "cc-42"
    assert "--fork-session" not in edits_argv

    yolo_fork = build_commandcode_turn_argv(
        commandcode_bin="command-code",
        prompt="ship it",
        permission_mode="yolo",
        external_session_id="cc-42",
        fork_session=True,
        model="sonnet",
    )
    assert yolo_fork[yolo_fork.index("--permission-mode") + 1] == "yolo"
    assert "--fork-session" in yolo_fork
    assert "--model" in yolo_fork

    env = build_commandcode_env(
        proxy_root_url="http://127.0.0.1:18569",
        auth_token="tok-cc",
        session_id="rc_cc_1",
        base_env={},
    )
    assert env["MCC_COMMANDCODE_API_KEY"] == "tok-cc"
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:18569"
    assert env["MCC_RC_SESSION_ID"] == "rc_cc_1"


def test_hub_pty_argv_resume_flags_for_tier2_harnesses(monkeypatch) -> None:
    hub = RemoteRuntimeHub()
    try:
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
        assert hub._build_pty_argv(
            "commandcode_cli", external_session_id="cc-1", model="sonnet"
        ) == ["/usr/bin/command-code", "--resume", "cc-1", "--model", "sonnet"]
        assert hub._build_pty_argv("gemini_cli", external_session_id="gem-1") == [
            "/usr/bin/gemini",
            "--resume",
            "gem-1",
        ]
        assert hub._build_pty_argv("hermes", external_session_id="her-1") == [
            "/usr/bin/hermes",
            "--resume",
            "her-1",
        ]
        assert hub._build_pty_argv("pi", external_session_id="pi-1") == [
            "/usr/bin/pi",
            "--session",
            "pi-1",
        ]
    finally:
        hub._store.close()


@pytest.mark.spawns_process
def test_commandcode_driver_turn_resume_yolo_and_ask_pty_hint(
    monkeypatch, tmp_path: Path
) -> None:
    cfg_dir = tmp_path / "mcc_home"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCC_CONFIG_DIR", str(cfg_dir))
    reset_config_dir_cache()

    fake_cc_script = """
import json
import sys

args = sys.argv[1:]
prompt = args[args.index("-p") + 1]
mode = args[args.index("--permission-mode") + 1]
resume_id = args[args.index("--resume") + 1] if "--resume" in args else ""

if prompt == "ask_blocked":
    print(json.dumps({"type": "result", "session_id": "cc_s1", "is_error": True, "result": "Needs terminal approval"}), flush=True)
    sys.exit(1)

sid = resume_id or "cc_s1"
print(json.dumps({
    "type": "assistant",
    "session_id": sid,
    "message": {"content": [{"type": "text", "text": f"mode={mode};resume={resume_id};prompt={prompt}"}]},
}), flush=True)
print(json.dumps({"type": "result", "session_id": sid, "subtype": "success", "result": "ok"}), flush=True)
"""
    fake_bin = _write_fake_cli(tmp_path, "command-code", fake_cc_script)

    async def _run() -> None:
        events: list[tuple[str, dict[str, Any]]] = []
        state: dict[str, Any] = {}

        async def _on_event(kind: str, payload: dict[str, Any]) -> None:
            events.append((kind, payload))

        async def _on_approval(
            approval_id: str, _tool: str, _tuid: str, _inp: dict[str, Any]
        ) -> dict[str, Any]:
            return {"id": approval_id}

        async def _on_state(**fields: Any) -> None:
            state.update(fields)

        driver = CommandCodeStructuredDriver(
            session_id="rc_cc_run",
            cwd=str(tmp_path),
            proxy_root_url="http://127.0.0.1:18569",
            auth_token="tok",
            permission_mode="auto_edits",
            commandcode_bin=fake_bin,
            on_event=_on_event,
            on_approval_create=_on_approval,
            on_state_update=_on_state,
        )
        await driver.start()
        try:
            # Turn 1: auto_edits -> accept-edits, captures session_id
            await driver.send_user_message("turn one")
            for _ in range(60):
                if any(k == "turn.completed" for k, _ in events):
                    break
                await asyncio.sleep(0.05)
            assert state.get("external_session_id") == "cc_s1"
            msg1 = next(p["text"] for k, p in events if k == "message.assistant")
            assert "mode=accept-edits" in msg1
            assert "resume=;" in msg1

            # Turn 2: MUST pass --resume cc_s1
            await driver.send_user_message("turn two")
            for _ in range(60):
                if sum(1 for k, _ in events if k == "turn.completed") >= 2:
                    break
                await asyncio.sleep(0.05)
            msg2 = [p["text"] for k, p in events if k == "message.assistant"][-1]
            assert "resume=cc_s1;" in msg2

            # Turn 3: yolo -> --permission-mode yolo
            await driver.set_permission_mode("yolo")
            await driver.send_user_message("turn three")
            for _ in range(60):
                if sum(1 for k, _ in events if k == "turn.completed") >= 3:
                    break
                await asyncio.sleep(0.05)
            msg3 = [p["text"] for k, p in events if k == "message.assistant"][-1]
            assert "mode=yolo;" in msg3

            # Turn 4: ask mode error emits recommend_pty hint
            await driver.set_permission_mode("ask")
            await driver.send_user_message("ask_blocked")
            for _ in range(60):
                if any(
                    k == "session.state" and p.get("recommend_pty") is True
                    for k, p in events
                ):
                    break
                await asyncio.sleep(0.05)
            assert any(
                k == "session.state" and p.get("recommend_pty") is True
                for k, p in events
            )
        finally:
            await driver.close()

    asyncio.run(_run())


@contextmanager
def _rc_client(
    monkeypatch, tmp_path: Path
) -> Iterator[tuple[TestClient, str, Path, RemoteRuntimeHub]]:
    cfg_dir = tmp_path / "mcc_home"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCC_CONFIG_DIR", str(cfg_dir))
    reset_config_dir_cache()
    allowed = tmp_path / "work"
    allowed.mkdir(parents=True, exist_ok=True)

    settings = Settings.model_validate(
        {
            "RC_ENABLED": True,
            "RC_ALLOWED_DIRS": str(allowed),
            "RC_PERMISSION_MODE": "yolo",
            "RC_SESSION_SECRET": "tier2-route-secret-0123456789",
        }
    )
    manager = ProviderRuntimeManager(settings)
    runtime = ApplicationRuntime(manager, transcriber=None)
    hub = RemoteRuntimeHub(settings=settings)
    app = create_app(
        ApiServices(
            requests=manager,
            admin=runtime,
            tasks=runtime,
            media=None,
            remote=hub,
        )
    )
    try:
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            tok = client.post("/admin/api/rc/pair-token", json={}).json()["token"]
            pair = client.post("/rc/api/pair", json={"token": tok}).json()
            yield client, pair["csrf_token"], allowed, hub
    finally:
        hub._store.close()


@pytest.mark.spawns_process
def test_hub_tier2_badges_and_switch_pty_streams_live_output(
    monkeypatch, tmp_path: Path
) -> None:
    with _rc_client(monkeypatch, tmp_path) as (client, csrf, allowed, hub):
        harnesses = {
            h["id"]: h for h in client.get("/rc/api/harnesses").json()["harnesses"]
        }
        for acp_id in (
            "hermes",
            "gemini_cli",
            "qwen_code",
            "kimi_code",
            "cline_cli",
            "goose",
            "droid",
        ):
            assert harnesses[acp_id]["driver_tier"] == "tier_2_acp"
            assert harnesses[acp_id]["supports_structured"] is True
        assert harnesses["pi"]["driver_tier"] == "tier_2_pi_rpc"
        assert harnesses["commandcode_cli"]["driver_tier"] == "tier_2_commandcode"
        assert harnesses["aider"]["driver_tier"] == "tier_3_pty"
        assert harnesses["crush"]["driver_tier"] == "tier_3_pty"
        assert (
            driver_tier_for_harness("commandcode_cli", driver_mode="pty")
            == "tier_3_pty"
        )

        fake_cc = _write_fake_cli(
            tmp_path,
            "command-code",
            """
import json, sys
if "-p" in sys.argv:
    print(json.dumps({"type": "result", "session_id": "cc_pty_99", "result": "structured ok"}), flush=True)
else:
    print("PTY_RESUMED:" + " ".join(sys.argv[1:]), flush=True)
""",
        )
        monkeypatch.setattr(
            commandcode_driver,
            "resolve_commandcode_binary",
            lambda _explicit=None: fake_cc,
        )
        monkeypatch.setattr(
            hub,
            "_build_pty_argv",
            lambda _harness, **_kw: [
                sys.executable,
                "-u",
                "-c",
                "import sys; print('PTY_RESUMED:' + (_kw_id := sys.argv[1])); sys.stdin.readline()",
                str(_kw.get("external_session_id") or "cc_pty_99"),
            ],
        )

        created = client.post(
            "/rc/api/sessions",
            headers={"X-MCC-RC-CSRF": csrf},
            json={
                "harness": "commandcode_cli",
                "cwd": str(allowed),
                "permission_mode": "auto_edits",
                "prompt": "hello",
            },
        )
        assert created.status_code == 200
        sess = created.json()["session"]
        assert sess["driver_tier"] == "tier_2_commandcode"

        # Wait for turn 1 to capture cc_pty_99
        with client.websocket_connect(
            f"/rc/ws/sessions/{sess['id']}?since_seq=0"
        ) as ws:
            for _ in range(10):
                ev = json.loads(ws.receive_text())
                if ev["kind"] == "turn.completed":
                    break

        switched = client.post(
            f"/rc/api/sessions/{sess['id']}/switch-pty",
            headers={"X-MCC-RC-CSRF": csrf},
            json={"cols": 100, "rows": 30},
        )
        assert switched.status_code == 200
        sw_sess = switched.json()["session"]
        assert sw_sess["driver_tier"] == "tier_3_pty"
        assert sw_sess["external_session_id"] == "cc_pty_99"

        with client.websocket_connect(f"/rc/ws/sessions/{sess['id']}/pty") as pty_ws:
            out = b""
            for _ in range(10):
                out += pty_ws.receive_bytes()
                if b"PTY_RESUMED:cc_pty_99" in out:
                    break
            assert b"PTY_RESUMED:cc_pty_99" in out

        stopped = client.post(
            f"/rc/api/sessions/{sess['id']}/stop",
            headers={"X-MCC-RC-CSRF": csrf},
        )
        assert stopped.status_code == 200
