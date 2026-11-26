"""Tests for ClaudeBidirectionalDriver stream-json protocol and modes."""

import asyncio
import sys
from pathlib import Path
from typing import Any

from my_claude_code.runtime.remote import claude_driver
from my_claude_code.runtime.remote.claude_driver import (
    ClaudeBidirectionalDriver,
    build_claude_stream_argv,
)


def test_build_claude_stream_argv_flags_for_modes_and_resume() -> None:
    ask_argv = build_claude_stream_argv(claude_bin="claude", permission_mode="ask")
    assert "--output-format" in ask_argv
    assert "--input-format" in ask_argv
    assert "--permission-prompt-tool" in ask_argv
    assert "--include-partial-messages" in ask_argv
    assert ask_argv[-2:] == ["--permission-mode", "default"]

    edits_argv = build_claude_stream_argv(
        claude_bin="claude", permission_mode="auto_edits"
    )
    assert edits_argv[-2:] == ["--permission-mode", "acceptEdits"]

    yolo_argv = build_claude_stream_argv(
        claude_bin="claude",
        permission_mode="yolo",
        external_session_id="ext-99",
        fork_session=True,
        model="sonnet",
    )
    assert "--resume" in yolo_argv
    assert "ext-99" in yolo_argv
    assert "--fork-session" in yolo_argv
    assert "--allow-dangerously-skip-permissions" in yolo_argv
    assert "bypassPermissions" in yolo_argv


def test_claude_driver_approvals_modes_and_questions(
    monkeypatch, tmp_path: Path
) -> None:
    fake_cli = tmp_path / "fake_claude_proto.py"
    fake_cli.write_text(
        "\n".join(
            [
                "import json, sys",
                "print(json.dumps({'type':'system','subtype':'init','session_id':'ext-42','model':'claude-sonnet'}), flush=True)",
                "while True:",
                "    line = sys.stdin.readline()",
                "    if not line: break",
                "    raw = line.strip()",
                "    if not raw: continue",
                "    msg = json.loads(raw)",
                "    if msg.get('type') == 'control_request':",
                "        req = msg['request']",
                "        rid = msg['request_id']",
                "        sub = req.get('subtype')",
                "        if sub in ('interrupt', 'set_model', 'set_permission_mode'):",
                "            print(json.dumps({'type':'control_response','response':{'subtype':'success','request_id':rid,'response':{}}}), flush=True)",
                "            print(json.dumps({'type':'driver.control_ack','sub':sub}), flush=True)",
                "        continue",
                "    if msg.get('type') == 'user':",
                "        txt = msg['message']['content']",
                "        if txt == 'run_bash':",
                "            print(json.dumps({'type':'control_request','request_id':'req_b1','request':{'subtype':'can_use_tool','tool_name':'Bash','tool_use_id':'tu_1','input':{'command':'echo hi'}}}), flush=True)",
                "        elif txt == 'run_edit':",
                "            print(json.dumps({'type':'control_request','request_id':'req_e1','request':{'subtype':'can_use_tool','tool_name':'Edit','tool_use_id':'tu_2','input':{'file_path':'a.py'}}}), flush=True)",
                "        elif txt == 'ask_q':",
                "            print(json.dumps({'type':'control_request','request_id':'req_q1','request':{'subtype':'can_use_tool','tool_name':'AskUserQuestion','tool_use_id':'tu_3','input':{'questions':[{'question':'Pick one'}]}}}), flush=True)",
                "    elif msg.get('type') == 'control_response':",
                "        resp = msg['response']['response']",
                "        print(json.dumps({'type':'result','subtype':'success','result':json.dumps(resp)}), flush=True)",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        claude_driver,
        "build_claude_stream_argv",
        lambda **kwargs: [sys.executable, "-u", str(fake_cli)],
    )

    async def _run() -> None:
        events: list[tuple[str, dict[str, Any]]] = []
        approvals: list[str] = []
        state: dict[str, Any] = {}

        async def _on_event(kind: str, payload: dict[str, Any]) -> None:
            events.append((kind, payload))

        async def _on_approval(
            approval_id: str,
            _tool: str,
            _tuid: str,
            _inp: dict[str, Any],
        ) -> dict[str, Any]:
            approvals.append(approval_id)
            return {"id": approval_id}

        async def _on_state(**fields: Any) -> None:
            state.update(fields)

        driver = ClaudeBidirectionalDriver(
            session_id="rc_t1",
            cwd=str(tmp_path),
            proxy_root_url="http://127.0.0.1:8082",
            auth_token="test-token",
            permission_mode="ask",
            on_event=_on_event,
            on_approval_create=_on_approval,
            on_state_update=_on_state,
        )
        await driver.start()
        try:
            # In ask mode, Bash triggers approval.requested and parks until resolved
            await driver.send_user_message("run_bash")
            for _ in range(50):
                if any(k == "approval.requested" for k, _ in events):
                    break
                await asyncio.sleep(0.05)
            req_evt = next(p for k, p in events if k == "approval.requested")
            await driver.resolve_approval(req_evt["approval_id"], decision="allow_once")
            for _ in range(50):
                if any(k == "turn.completed" for k, _ in events):
                    break
                await asyncio.sleep(0.05)

            # Switch to auto_edits: Edit is auto-approved with zero approval.requested
            before_req_count = sum(1 for k, _ in events if k == "approval.requested")
            await driver.set_permission_mode("auto_edits")
            await driver.send_user_message("run_edit")
            for _ in range(50):
                if sum(1 for k, _ in events if k == "turn.completed") >= 2:
                    break
                await asyncio.sleep(0.05)
            after_req_count = sum(1 for k, _ in events if k == "approval.requested")
            assert after_req_count == before_req_count

            # Bash in auto_edits mode must still prompt for explicit approval
            await driver.send_user_message("run_bash")
            for _ in range(50):
                if (
                    sum(1 for k, _ in events if k == "approval.requested")
                    > after_req_count
                ):
                    break
                await asyncio.sleep(0.05)
            bash_req_count = sum(1 for k, _ in events if k == "approval.requested")
            assert bash_req_count == after_req_count + 1
            bash_evt = [p for k, p in events if k == "approval.requested"][-1]
            await driver.resolve_approval(
                bash_evt["approval_id"], decision="allow_once"
            )
            for _ in range(50):
                if sum(1 for k, _ in events if k == "turn.completed") >= 3:
                    break
                await asyncio.sleep(0.05)

            # AskUserQuestion emits question.requested and forwards answers
            await driver.send_user_message("ask_q")
            for _ in range(50):
                if any(k == "question.requested" for k, _ in events):
                    break
                await asyncio.sleep(0.05)
            q_evt = next(p for k, p in events if k == "question.requested")
            await driver.resolve_approval(
                q_evt["approval_id"],
                decision="allow_once",
                answers={"Pick one": "Option A"},
            )
            for _ in range(50):
                if sum(1 for k, _ in events if k == "turn.completed") >= 4:
                    break
                await asyncio.sleep(0.05)

            last_result = [p for k, p in events if k == "turn.completed"][-1]["result"]
            assert "Option A" in last_result
            assert state.get("external_session_id") == "ext-42"
            await driver.interrupt()
            await driver.set_model("opus")
        finally:
            await driver.close()

    asyncio.run(_run())
