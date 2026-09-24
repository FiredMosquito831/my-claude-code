"""The tool catalogue from the request to the page: capture, detail, lookup, export.

The capture holds the client's tools by reference and hashes nothing; the
writer thread does the rest (``tests/core/test_request_log_tool_catalogue.py``).
These tests follow one real request through ``build_capture`` and the admin
routes that read it back.
"""

import csv
import io
import json

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.request_capture import build_capture
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import Message, MessagesRequest, Tool
from my_claude_code.core.request_log import get_request_log_store
from my_claude_code.core.tool_catalogue import fingerprint_tools
from tests.api.support import create_test_app

VIDEO_SCALE = r"^(?:[1-9]\d{0,4}|-[12]):(?:[1-9]\d{0,4}|-[12])(?![\s\S])"
APPIUM = "mcp__appium-mcp__appium_screen_recording"


def _tools() -> list[Tool]:
    return [
        Tool.model_validate(
            {"name": "Bash", "description": "Run a command.", "input_schema": {}}
        ),
        Tool.model_validate(
            {
                "name": APPIUM,
                "description": "Record the screen.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "videoScale": {"type": "string", "pattern": VIDEO_SCALE}
                    },
                },
                "cache_control": {"type": "ephemeral"},
            }
        ),
    ]


def _request(tools: list[Tool] | None) -> MessagesRequest:
    return MessagesRequest(
        model="chatgpt_oauth/gpt-5.6-sol",
        max_tokens=100,
        stream=True,
        messages=[Message(role="user", content="hi")],
        tools=tools,
    )


def _capture(
    request: MessagesRequest, request_id: str, settings: Settings | None = None
):
    return build_capture(
        settings or Settings(),
        request,
        request_id=request_id,
        endpoint="/v1/messages",
        protocol="anthropic",
    )


class TestTheCapture:
    def test_it_holds_the_clients_tools_by_reference(self) -> None:
        request = _request(_tools())

        capture = _capture(request, "req_ref")

        record = capture._record
        assert request.tools is not None
        assert len(record.tools) == 2
        assert all(
            held is sent for held, sent in zip(record.tools, request.tools, strict=True)
        )
        # Hashing is the writer's job, never the request's.
        assert record.tool_catalogue is None
        assert record.keep_tool_definitions is True
        capture.finish_success("done")

    def test_body_capture_off_keeps_no_definitions(self, monkeypatch) -> None:
        monkeypatch.setenv("REQUEST_LOG_CAPTURE_BODIES", "false")

        capture = _capture(_request(_tools()), "req_off", Settings())

        assert capture._record.keep_tool_definitions is False
        capture.finish_success("done")

    def test_a_request_without_tools_holds_nothing(self) -> None:
        capture = _capture(_request(None), "req_none")

        assert capture._record.tools == ()
        capture.finish_success("done")

    def test_a_captured_request_is_fingerprinted_by_the_writer(self) -> None:
        request = _request(_tools())
        capture = _capture(request, "req_e2e")
        capture.finish_success("done")
        store = get_request_log_store()
        assert store is not None
        store.close()

        detail = store.get_request("req_e2e")

        expected = fingerprint_tools(request.tools or [])
        assert detail is not None and expected is not None
        assert detail["tool_catalogue_sha"] == expected.sha.hex()
        assert [tool["name"] for tool in detail["tool_catalogue"]["tools"]] == [
            "Bash",
            APPIUM,
        ]


@pytest.fixture
def seeded():
    for index in range(3):
        capture = _capture(_request(_tools()), f"req_{index}")
        capture.finish_success("done")
    capture = _capture(_request(None), "req_plain")
    capture.finish_success("done")
    store = get_request_log_store()
    assert store is not None
    store.close()
    return store


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_test_app(), client=("127.0.0.1", 50000))


class TestTheRoutes:
    def test_the_detail_carries_the_catalogue(self, client, seeded) -> None:
        response = client.get("/admin/api/requests/req_1")

        assert response.status_code == 200
        body = response.json()
        assert len(body["tool_catalogue_sha"]) == 64
        assert body["tool_catalogue"]["tool_count"] == 2
        assert body["tool_catalogue"]["seen"] == 3
        # Names and hashes; the definition itself never leaves the server.
        assert "videoScale" not in response.text
        assert "Record the screen" not in response.text

    def test_the_lookup_names_the_requests_that_carried_a_tool(
        self, client, seeded
    ) -> None:
        response = client.get(
            "/admin/api/tool-catalogues/requests", params={"name": APPIUM}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is True
        assert sorted(row["id"] for row in body["rows"]) == ["req_0", "req_1", "req_2"]
        assert body["seen"] == 3
        assert body["catalogues"] == 1
        assert "videoScale" not in response.text

    def test_the_lookup_is_loopback_only(self, client, seeded) -> None:
        remote = TestClient(client.app, client=("203.0.113.10", 50000))

        response = remote.get(
            "/admin/api/tool-catalogues/requests", params={"name": APPIUM}
        )

        assert response.status_code == 403

    def test_the_lookup_requires_a_name(self, client, seeded) -> None:
        assert client.get("/admin/api/tool-catalogues/requests").status_code == 422

    def test_the_export_column_is_opt_in(self, client, seeded) -> None:
        default = client.get(
            "/admin/api/export", params={"format": "csv", "scope": "requests"}
        )
        chosen = client.get(
            "/admin/api/export",
            params={
                "format": "csv",
                "scope": "requests",
                "fields": "providers,tool_catalogue",
            },
        )

        default_header = next(
            csv.reader(io.StringIO(default.content.decode("utf-8-sig")))
        )
        assert "Tool catalogue SHA-256" not in default_header
        rows = list(csv.reader(io.StringIO(chosen.content.decode("utf-8-sig"))))
        column = rows[0].index("Tool catalogue SHA-256")
        values = {row[rows[0].index("ID")]: row[column] for row in rows[1:]}
        assert len(values["req_0"]) == 64
        assert values["req_plain"] == ""

    def test_the_json_export_carries_hex(self, client, seeded) -> None:
        response = client.get(
            "/admin/api/export",
            params={"format": "json", "scope": "requests", "fields": "tool_catalogue"},
        )

        rows = {row["id"]: row for row in json.loads(response.content)}
        assert len(rows["req_2"]["tool_catalogue_sha"]) == 64
        assert rows["req_plain"]["tool_catalogue_sha"] is None
