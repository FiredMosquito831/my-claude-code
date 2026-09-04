"""What the server is allowed to import before it can answer ``/health``.

Building the ASGI app used to drag four heavyweight third-party packages into
the process -- ``openai``, ``openpyxl``, ``aiohttp`` and ``tiktoken`` -- none of
which any startup path asks a question of. Together they were most of the
import cost of a cold start. They are now imported by the first caller that
genuinely needs one, and this module pins that: a fresh interpreter that builds
the app must not have loaded any of them, and every deferred caller must still
work when it does.
"""

import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from tests.api.support import create_test_app

# Top-level package name -> why the startup path must not import it.
DEFERRED_UNTIL_FIRST_USE: dict[str, str] = {
    "openai": (
        "The OpenAI SDK pulls its whole Assistants type tree (~2 s cold). Only "
        "an OpenAI-compatible provider construction and the exception-class "
        "questions on a failure path need it."
    ),
    "openpyxl": (
        "Only ``core.export.render_xlsx`` needs it, and only for an admin "
        "export the operator actually asked for."
    ),
    "aiohttp": (
        "Only the outbound web-server-tool stack needs it, and only for a "
        "request that carries a web search or fetch tool."
    ),
    "tiktoken": (
        "Only a token count needs it, and the encoder build (the BPE table) "
        "costs more than the import. Nothing counts tokens before the first "
        "request."
    ),
}

_PROBE = "\n".join(
    (
        "import sys",
        "import my_claude_code.runtime.bootstrap",
        "import my_claude_code.api.app",
        f"names = {sorted(DEFERRED_UNTIL_FIRST_USE)!r}",
        "loaded = [name for name in names if name in sys.modules]",
        "print(','.join(loaded))",
    )
)


def test_building_the_asgi_app_imports_no_deferred_heavyweight() -> None:
    """A fresh interpreter that imports the app builder loads none of them."""
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    loaded = [name for name in completed.stdout.strip().split(",") if name]
    assert loaded == [], (
        "importing the ASGI app builder loaded packages that are supposed to "
        "wait for their first real caller: "
        + "; ".join(f"{name}: {DEFERRED_UNTIL_FIRST_USE[name]}" for name in loaded)
    )


def test_openai_failure_classification_still_answers_after_the_deferral() -> None:
    """The exception-class questions still resolve once the SDK is imported."""
    import openai

    from my_claude_code.providers.failure_policy import (
        is_retryable_provider_error,
        provider_error_message,
    )

    error = openai.AuthenticationError(
        "bad key", response=_stub_response(401), body=None
    )

    assert is_retryable_provider_error(error) is False
    assert provider_error_message(error)


def test_web_tools_facade_still_re_exports_its_names() -> None:
    """The lazy ``__getattr__`` hands back the submodule's own objects."""
    import my_claude_code.api.web_tools as facade
    from my_claude_code.api.web_tools.egress import WebFetchEgressPolicy
    from my_claude_code.api.web_tools.request import is_web_server_tool_request
    from my_claude_code.api.web_tools.streaming import (
        stream_web_server_tool_response,
    )

    assert facade.WebFetchEgressPolicy is WebFetchEgressPolicy
    assert facade.is_web_server_tool_request is is_web_server_tool_request
    assert facade.stream_web_server_tool_response is stream_web_server_tool_response
    assert set(facade.__all__) <= set(dir(facade))
    unknown = "not_a_web_tool_name"
    with pytest.raises(AttributeError):
        getattr(facade, unknown)


def test_the_first_admin_export_request_still_renders_xlsx() -> None:
    """The admin surface loads openpyxl on demand and answers normally."""
    client = TestClient(create_test_app(), client=("127.0.0.1", 50000))

    response = client.get(
        "/admin/api/export", params={"format": "xlsx", "scope": "requests"}
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    # A real XLSX is a zip container; the deferred import produced a workbook.
    assert response.content[:2] == b"PK"


def test_token_counting_still_uses_the_shared_encoder() -> None:
    """The encoder is built on the first count and reused afterwards."""
    from my_claude_code.core.anthropic.tokens import count_text_tokens
    from my_claude_code.core.token_encoder import cl100k_encoder

    assert count_text_tokens("hello world") > 0
    assert cl100k_encoder() is cl100k_encoder()


def _stub_response(status_code: int):
    import httpx

    return httpx.Response(
        status_code=status_code, request=httpx.Request("POST", "https://example.test")
    )


#: What ``mcc-desktop --print-status`` must not drag in. A desktop shell calls
#: it on every launch and on every reconnect attempt, so its cost is paid over
#: and over on a machine that may not even have a server running yet. The four
#: heavyweights above are joined here by the admin surface: the status document
#: is built from ``config`` and ``core`` alone, and importing the routes that
#: serve the dashboard to read four numbers out of ``Settings`` would be a
#: silent regression nothing else notices.
PRINT_STATUS_MUST_NOT_IMPORT: tuple[str, ...] = (
    *sorted(DEFERRED_UNTIL_FIRST_USE),
    "my_claude_code.api.admin_routes",
    "my_claude_code.api.app",
    "my_claude_code.application.release_updates",
    "pystray",
)

_PRINT_STATUS_PROBE = "\n".join(
    (
        "import io, sys",
        "from contextlib import redirect_stdout",
        "from my_claude_code.cli.desktop_entrypoint import launch",
        "with redirect_stdout(io.StringIO()) as sink:",
        "    launch(['--print-status'])",
        "import json",
        "json.loads(sink.getvalue())",
        f"names = {list(PRINT_STATUS_MUST_NOT_IMPORT)!r}",
        "print(','.join(name for name in names if name in sys.modules))",
    )
)


def test_print_status_imports_nothing_heavyweight(tmp_path) -> None:
    """A fresh interpreter that answers ``--print-status`` stays cheap."""
    import os

    environment = dict(os.environ)
    # A scratch config directory and a port nothing owns: the probe must never
    # read the developer's real configuration or contact their live server.
    environment["MCC_CONFIG_DIR"] = str(tmp_path / "config")
    environment["PORT"] = "8199"
    environment["HOST"] = "127.0.0.1"
    (tmp_path / "config").mkdir()

    completed = subprocess.run(
        [sys.executable, "-c", _PRINT_STATUS_PROBE],
        capture_output=True,
        check=False,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    loaded = [name for name in completed.stdout.strip().split(",") if name]
    assert loaded == [], (
        "mcc-desktop --print-status is meant to be the cheapest command in the "
        "product -- a shell calls it on every launch and every reconnect. It "
        "loaded: " + ", ".join(loaded)
    )
