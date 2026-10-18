"""``mcc-mcp`` over real stdio, against a stand-in MCC (7.99.0).

Each test starts the program as an app would -- a child process speaking MCP
on stdin/stdout -- with the official SDK's own client, and a stand-in MCC on a
port the OS picked. The child gets only what an MCP client hands a server
(the SDK's short allow-list of variables, plus the config folder MCC's
registrations pass), so what it does with its address and token is what it
does on a user's machine.
"""

import json
import os
import socket
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from tests.mcp_media.mcp_fake_mcc import PNG, FakeMcc

pytestmark = pytest.mark.local_serial

TOKEN = "test-token"


@pytest.fixture
def fake_mcc() -> Iterator[FakeMcc]:
    mcc = FakeMcc(token=TOKEN).start()
    try:
        yield mcc
    finally:
        mcc.stop()


def _config_dir(tmp_path: Path, port: int, token: str = TOKEN) -> Path:
    """MCC's settings folder as the server's own first start leaves it."""

    home = tmp_path / "mcc-config"
    home.mkdir(exist_ok=True)
    (home / ".env").write_text(
        f"HOST=127.0.0.1\nPORT={port}\nANTHROPIC_AUTH_TOKEN={token}\n",
        encoding="utf-8",
    )
    return home


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _params(
    config_dir: Path, work: Path, extra: Mapping[str, str] | None = None
) -> StdioServerParameters:
    work.mkdir(exist_ok=True)
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "my_claude_code.mcp_media.server", "--app", "pytest_app"],
        env={"MCC_CONFIG_DIR": str(config_dir), **(extra or {})},
        cwd=str(work),
    )


def _tools_wire(result: Any) -> str:
    return json.dumps(
        [
            tool.model_dump(mode="json", by_alias=True, exclude_none=True)
            for tool in result.tools
        ]
    )


def _text(result: Any) -> str:
    return "\n".join(
        block.text for block in result.content if getattr(block, "type", "") == "text"
    )


async def _session(
    params: StdioServerParameters, mode: str = "legacy"
) -> dict[str, Any]:
    async with Client(params, mode=mode) as client:
        first = await client.list_tools()
        second = await client.list_tools()
        return {
            "first": _tools_wire(first),
            "second": _tools_wire(second),
            "names": [tool.name for tool in first.tools],
            "list_changed": client.server_capabilities.tools.list_changed
            if client.server_capabilities.tools is not None
            else None,
            "protocol": client.session.protocol_version,
        }


async def _call(
    params: StdioServerParameters, name: str, arguments: Mapping[str, Any]
) -> Any:
    async with Client(params, mode="legacy") as client:
        return await client.call_tool(name, dict(arguments))


def test_both_protocol_eras_answer(fake_mcc: FakeMcc, tmp_path: Path) -> None:
    params = _params(_config_dir(tmp_path, fake_mcc.port), tmp_path / "work")

    legacy = anyio.run(_session, params, "legacy")
    modern = anyio.run(_session, params, "auto")

    assert legacy["protocol"] != modern["protocol"]
    assert modern["protocol"] == "2026-07-28"
    assert (
        legacy["names"]
        == modern["names"]
        == [
            "media_status",
            "generate_image",
            "speak",
            "create_video",
            "get_video",
        ]
    )


def test_tools_list_identical_across_calls_and_restarts(
    fake_mcc: FakeMcc, tmp_path: Path
) -> None:
    params = _params(_config_dir(tmp_path, fake_mcc.port), tmp_path / "work")

    one = anyio.run(_session, params, "legacy")
    two = anyio.run(_session, params, "legacy")

    assert one["first"] == one["second"]
    assert one["first"] == two["first"]


def test_no_list_changed_capability(fake_mcc: FakeMcc, tmp_path: Path) -> None:
    params = _params(_config_dir(tmp_path, fake_mcc.port), tmp_path / "work")

    for mode in ("legacy", "auto"):
        assert not anyio.run(_session, params, mode)["list_changed"], mode


def test_mcc_down_is_an_honest_error(fake_mcc: FakeMcc, tmp_path: Path) -> None:
    config_dir = _config_dir(tmp_path, fake_mcc.port)
    params = _params(config_dir, tmp_path / "work")
    with_mcc = anyio.run(_session, params, "legacy")
    port = fake_mcc.port
    fake_mcc.stop()

    # MCC is gone: the session still offers the tools the last one had, the
    # same bytes -- read from the catalogue mcc-mcp saved -- ...
    without = anyio.run(_session, params, "legacy")
    assert without["first"] == with_mcc["first"]

    # ... and a call says plainly what is wrong and what to do.
    result = anyio.run(_call, params, "generate_image", {"prompt": "a red kite"})
    assert result.is_error is True
    text = _text(result)
    assert f"MCC is not answering at http://127.0.0.1:{port}" in text
    assert "Start MCC" in text

    status = anyio.run(_call, params, "media_status", {})
    assert "not answering" in _text(status)

    # With nothing saved either, media_status alone, which says why.
    (config_dir / "cache" / "mcp-media-catalogue.json").unlink()
    alone = anyio.run(_session, params, "legacy")
    assert alone["names"] == ["media_status"]


def test_address_and_token_come_from_mcc_settings(
    fake_mcc: FakeMcc, tmp_path: Path
) -> None:
    """R14: MCC's .env decides where MCC is; the app's own PORT never does."""

    params = _params(
        _config_dir(tmp_path, fake_mcc.port),
        tmp_path / "work",
        {
            # An app's own variables, as a web project's shell would carry
            # them: neither may redirect or re-key a media call.
            "PORT": str(_free_port()),
            "ANTHROPIC_AUTH_TOKEN": "an-app-token",
        },
    )

    result = anyio.run(_call, params, "generate_image", {"prompt": "a red kite"})

    assert result.is_error is False, _text(result)
    (call,) = fake_mcc.calls("/v1/images/generations")
    assert call.headers["authorization"] == f"Bearer {TOKEN}"
    assert call.headers["x-mcc-harness"] == "pytest_app"
    assert call.headers["x-mcc-inline-images"] == "1"
    assert call.headers["user-agent"].startswith("mcc-mcp/")
    saved = [Path(line) for line in _text(result).splitlines()[1:2]]
    assert saved and saved[0].read_bytes() == PNG
    assert saved[0].parent == (tmp_path / "work").resolve()
    # The token is in no result, and the address the server used is named.
    status = _text(anyio.run(_call, params, "media_status", {}))
    assert f"http://127.0.0.1:{fake_mcc.port}" in status
    assert TOKEN not in status and TOKEN not in _text(result)


def test_loopback_ignores_proxy_env(fake_mcc: FakeMcc, tmp_path: Path) -> None:
    """R8: MCC is on loopback; no proxy variable can route a call elsewhere."""

    dead = f"http://127.0.0.1:{_free_port()}"
    params = _params(
        _config_dir(tmp_path, fake_mcc.port),
        tmp_path / "work",
        {
            "HTTP_PROXY": dead,
            "HTTPS_PROXY": dead,
            "ALL_PROXY": dead,
            "http_proxy": dead,
            "NO_PROXY": "",
        },
    )

    result = anyio.run(_call, params, "generate_image", {"prompt": "a red kite"})

    assert result.is_error is False, _text(result)
    assert len(fake_mcc.calls("/v1/images/generations")) == 1
    assert os.environ.get("HTTP_PROXY") != dead
