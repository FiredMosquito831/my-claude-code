"""Request origin from the request to the page: capture, finalize, routes, export.

The extractor table is tested on its own in ``tests/core/test_request_origin.py``
and the columns in ``tests/core/test_request_log_origin.py``. These follow one
request through ``build_capture`` and the admin routes that read it back, with
recorded header shapes and synthetic system blocks. Every path is fake.
"""

import asyncio
import csv
import io
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.dependencies import get_settings
from my_claude_code.api.request_capture import build_capture
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.request_headers import capture_headers
from my_claude_code.core.request_log import get_request_log_store
from tests.api.support import create_test_app

SESSION = "0f3c2a1b-6d5e-4f70-9a8b-1c2d3e4f5a6b"
AGENT = "a7b8c9d0-1e2f-4a3b-8c4d-5e6f7a8b9c0d"
FOLDER = "C:\\Users\\devuser\\Projects\\demo"
SYSTEM = (
    "You are Claude Code.\n# Environment\n"
    "You have been invoked in the following environment:\n"
    f" - Primary working directory: {FOLDER}\n - Platform: win32\n"
)
# The header shapes a real Claude Code request carries, values faked.
CLAUDE_HEADERS = {
    "user-agent": "claude-cli/2.1.258 (external, cli)",
    "x-app": "cli",
    "anthropic-version": "2023-06-01",
    "x-claude-code-session-id": SESSION,
    "x-claude-code-agent-id": AGENT,
    "authorization": "Bearer fake-token",
}
SDK_HEADERS = {
    "user-agent": "claude-cli/2.1.258 (external, sdk-py, agent-sdk/0.1.9)",
    "x-claude-code-session-id": SESSION,
}


def _request(system: str | None = SYSTEM) -> MessagesRequest:
    return MessagesRequest(
        model="claude-sonnet-4-5",
        max_tokens=100,
        stream=True,
        system=system,
        messages=[Message(role="user", content="hi")],
    )


def _capture(request, request_id, headers, settings: Settings | None = None):
    return build_capture(
        settings or Settings(),
        request,
        request_id=request_id,
        endpoint="/v1/messages",
        protocol="anthropic",
        headers=headers,
    )


def _stored(request_id: str) -> dict:
    store = get_request_log_store()
    assert store is not None
    store.close()
    row = store.get_request(request_id)
    assert row is not None
    return row


class TestTheCapture:
    def test_the_session_is_known_at_capture_and_the_folder_at_finalize(
        self,
    ) -> None:
        capture = _capture(_request(), "req_claude", CLAUDE_HEADERS)

        record = capture._record
        # Headers are a dict read: resolved the moment the request arrives.
        assert record.session_id == SESSION
        assert record.agent_id == AGENT
        assert record.parent_session_id == SESSION
        # The prompt is not read on the request path.
        assert record.project_dir is None
        capture.finish_success("done")

        row = _stored("req_claude")
        assert row["project_dir"] == FOLDER
        assert row["origin_source"] == (
            "session_id=header.x-claude-code-session-id;"
            "agent_id=header.x-claude-code-agent-id;"
            "parent_session_id=header.x-claude-code-agent-id;"
            "project_dir=prompt.env-block"
        )

    def test_the_streaming_path_resolves_the_folder_off_the_loop(self) -> None:
        async def body() -> AsyncIterator[str]:
            yield 'event: message_stop\ndata: {"type": "message_stop"}\n\n'

        async def drive() -> None:
            capture = _capture(_request(), "req_stream", CLAUDE_HEADERS)
            async for _chunk in capture.wrap(body()):
                pass

        asyncio.run(drive())

        assert _stored("req_stream")["project_dir"] == FOLDER

    def test_the_agent_sdk_gets_a_session_and_no_folder(self) -> None:
        capture = _capture(_request(), "req_sdk", SDK_HEADERS)
        capture.finish_success("done")

        row = _stored("req_sdk")
        assert row["harness"] == "claude_agent_sdk"
        assert row["session_id"] == SESSION
        assert row["project_dir"] is None
        assert row["origin_source"] == "session_id=header.x-claude-code-session-id"

    def test_a_client_that_says_nothing_leaves_every_column_null(self) -> None:
        capture = _capture(
            _request(system=None), "req_curl", {"user-agent": "curl/8.9.0"}
        )
        capture.finish_success("done")

        row = _stored("req_curl")
        for column in (
            "session_id",
            "agent_id",
            "parent_session_id",
            "project_dir",
            "origin_source",
        ):
            assert row[column] is None

    def test_folder_capture_off(self, monkeypatch) -> None:
        monkeypatch.setenv("REQUEST_LOG_CAPTURE_FOLDER", "false")
        capture = _capture(_request(), "req_nofolder", CLAUDE_HEADERS, Settings())
        capture.finish_success("done")

        row = _stored("req_nofolder")
        assert row["session_id"] == SESSION
        assert row["project_dir"] is None

    def test_session_capture_off(self, monkeypatch) -> None:
        monkeypatch.setenv("REQUEST_LOG_CAPTURE_SESSION", "false")
        capture = _capture(_request(), "req_nosession", CLAUDE_HEADERS, Settings())
        capture.finish_success("done")

        row = _stored("req_nosession")
        assert (row["session_id"], row["agent_id"], row["parent_session_id"]) == (
            None,
            None,
            None,
        )
        assert row["project_dir"] == FOLDER

    def test_the_headers_column_is_unchanged_by_the_new_capture(self) -> None:
        """The origin values never ride in the ``headers`` JSON; it stores
        exactly what 7.41.0 stored for the same request."""

        capture = _capture(_request(), "req_headers", CLAUDE_HEADERS)
        capture.finish_success("done")

        row = _stored("req_headers")
        assert row["headers"] == capture_headers(CLAUDE_HEADERS)
        assert SESSION not in str(row["headers"])
        assert AGENT not in str(row["headers"])

    def test_a_logging_off_capture_does_not_extract(self, monkeypatch) -> None:
        monkeypatch.setenv("REQUEST_LOG_ENABLED", "false")
        capture = _capture(_request(), "req_off", CLAUDE_HEADERS, Settings())

        assert capture.enabled is False
        assert capture._record.session_id is None
        capture.finish_success("done")


@pytest.fixture
def seeded():
    for index, headers in enumerate((CLAUDE_HEADERS, SDK_HEADERS)):
        capture = _capture(_request(), f"req_{index}", headers)
        capture.finish_success("done")
    capture = _capture(_request(system=None), "req_plain", {"user-agent": "curl/8"})
    capture.finish_success("done")
    store = get_request_log_store()
    assert store is not None
    store.close()
    return store


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_test_app(), client=("127.0.0.1", 50000))


class TestTheRoutes:
    def test_the_list_carries_origin_and_its_display_forms(
        self, client, seeded
    ) -> None:
        response = client.get("/admin/api/requests", params={"limit": 10})

        assert response.status_code == 200
        rows = {row["id"]: row for row in response.json()["rows"]}
        assert rows["req_0"]["session_short"] == "0f3c2a1b"
        assert rows["req_0"]["project_short"].startswith("Projects\\demo · #")
        assert rows["req_0"]["requested_model"] == "claude-sonnet-4-5"
        assert rows["req_1"]["project_short"] is None
        assert rows["req_plain"]["session_short"] is None

    def test_the_detail_says_how_each_value_is_known(self, client, seeded) -> None:
        body = client.get("/admin/api/requests/req_0").json()

        assert body["origin_provenance"]["session_id"]["sentence"] == (
            "stated by the x-claude-code-session-id header"
        )
        assert body["origin_provenance"]["project_dir"]["sentence"] == (
            "read from the prompt's environment block"
        )

    def test_the_export_group_is_opt_in(self, client, seeded) -> None:
        default = client.get(
            "/admin/api/export", params={"format": "csv", "scope": "requests"}
        )
        chosen = client.get(
            "/admin/api/export",
            params={"format": "csv", "scope": "requests", "fields": "origin"},
        )

        default_header = next(
            csv.reader(io.StringIO(default.content.decode("utf-8-sig")))
        )
        assert not {"Session", "Folder", "Origin source"} & set(default_header)
        rows = list(csv.reader(io.StringIO(chosen.content.decode("utf-8-sig"))))
        header = rows[0]
        for label in ("Session", "Subagent", "Parent session", "Folder"):
            assert label in header
        values = {row[header.index("ID")]: row for row in rows[1:]}
        assert values["req_0"][header.index("Folder")] == FOLDER
        assert values["req_1"][header.index("Session")] == SESSION
        assert values["req_plain"][header.index("Session")] == ""

    def test_the_backfill_status_is_served_before_the_detail_route(
        self, client, seeded
    ) -> None:
        response = client.get("/admin/api/requests/origin-backfill")

        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is True
        assert body["running"] is False
        assert body["harnesses"] == ["claude"]

    def test_the_backfill_starts_only_on_a_post(self, client, seeded) -> None:
        response = client.post("/admin/api/requests/origin-backfill")

        assert response.status_code == 200
        assert response.json()["enabled"] is True

    def test_the_backfill_is_loopback_only(self, client, seeded) -> None:
        remote = TestClient(client.app, client=("203.0.113.10", 50000))

        assert remote.get("/admin/api/requests/origin-backfill").status_code == 403
        assert remote.post("/admin/api/requests/origin-backfill").status_code == 403

    def test_the_backfill_refuses_when_folder_capture_is_off(self, seeded) -> None:
        settings = Settings()
        settings.request_log_capture_folder = False
        app = create_test_app(settings)
        app.dependency_overrides[get_settings] = lambda: settings
        client = TestClient(app, client=("127.0.0.1", 50000))

        status = client.get("/admin/api/requests/origin-backfill").json()
        started = client.post("/admin/api/requests/origin-backfill")

        assert status["enabled"] is False
        assert "REQUEST_LOG_CAPTURE_FOLDER" in status["reason"]
        assert started.status_code == 409

    def test_the_backfill_says_so_when_the_log_is_off(self) -> None:
        settings = Settings()
        settings.request_log_enabled = False
        app = create_test_app(settings)
        app.dependency_overrides[get_settings] = lambda: settings
        client = TestClient(app, client=("127.0.0.1", 50000))

        assert client.get("/admin/api/requests/origin-backfill").json() == {
            "enabled": False,
            "reason": "The request log is off, so there are no rows to fill in.",
        }
        assert client.post("/admin/api/requests/origin-backfill").status_code == 409
