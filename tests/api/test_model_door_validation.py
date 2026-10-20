"""A body a model door cannot accept gets a short 400, never an echo of it.

RESUMED-SESSION-422 PR 2 (spec ``INVESTIGATION-RESUMED-SESSION-422.md`` B1, B3,
B4; decisions of 2026-10-20 16:03): ``/v1/messages``, its ``count_tokens``,
``/v1/chat/completions`` and ``/v1/responses`` answer HTTP 400 in their own
vendor's envelope, naming each failing field with pydantic's reason and never
any of the input; every other route keeps FastAPI's 422 exactly as it was; and
the refusal leaves one ``REJECTED REQUEST`` WARNING per client per window.
Bodies are invented (:mod:`tests.api.tool_change_bodies`).
"""

import json
import re
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api import rejected_request_log as rrl
from my_claude_code.api.model_door_errors import (
    MAX_REASON_CHARS,
    ValidationReason,
    describe_validation_errors,
    validation_error_message,
)
from my_claude_code.config.settings import Settings
from tests.api.support import create_test_app
from tests.api.tool_change_bodies import (
    CLAUDE_CODE_UA,
    MARKER,
    hook_text,
    missing_field_body,
    plain_body,
    unknown_block_body,
)

HEADERS = {"user-agent": CLAUDE_CODE_UA, "x-mcc-harness": "claude"}
TAG_PREFIX = (
    "Input tag 'compaction' found using 'type' does not match any of the"
    " expected tags: 'text', 'image', 'document', 'tool_use', 'tool_result',"
    " 'thinking', 'redacted_thinking', 'server_tool_use',"
    " 'web_search_tool_result', 'web_fetch_tool_result', 'tool_addition',"
    " 'tool_removal'"
)


@pytest.fixture(scope="module")
def client():
    settings = Settings()
    settings.model = "opencode/test-model"
    app = create_test_app(settings, providers={"opencode": MagicMock()})
    with TestClient(app) as test_client:
        yield test_client


def _post(client: TestClient, path: str, body: Any) -> Any:
    return client.post(path, json=body, headers=HEADERS)


def _anthropic_400(response: Any, message: str) -> None:
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/json"
    assert "x-should-retry" not in response.headers
    request_id = response.headers["request-id"]
    assert (
        response.content
        == json.dumps(
            {
                "type": "error",
                "error": {"type": "invalid_request_error", "message": message},
                "request_id": request_id,
            },
            separators=(",", ":"),
        ).encode()
    )


def _openai_400(response: Any, message: str, param: str) -> None:
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/json"
    assert "x-should-retry" not in response.headers
    assert response.headers["request-id"].startswith("req_")
    assert (
        response.content
        == json.dumps(
            {
                "error": {
                    "message": message,
                    "type": "invalid_request_error",
                    "param": param,
                    "code": None,
                }
            },
            separators=(",", ":"),
        ).encode()
    )


@pytest.mark.parametrize("path", ["/v1/messages", "/v1/messages/count_tokens"])
def test_a_missing_field_on_a_messages_door_is_an_anthropic_400(
    client: TestClient, path: str
) -> None:
    _anthropic_400(
        _post(client, path, missing_field_body()), "messages: Field required"
    )


@pytest.mark.parametrize("path", ["/v1/messages", "/v1/messages/count_tokens"])
def test_an_unknown_block_names_its_tag_in_pydantics_own_words(
    client: TestClient, path: str
) -> None:
    response = _post(client, path, unknown_block_body())
    _anthropic_400(response, f"messages.0.content.1: {TAG_PREFIX}")
    # The phrase Claude Code 2.1.304 matches to fall back by itself.
    assert re.search(r"Input tag '[a-z_]+' found using 'type'", response.text)


def test_a_missing_field_on_chat_completions_is_an_openai_400(
    client: TestClient,
) -> None:
    body = {"model": "m", "note": hook_text("note", 4_000)}
    _openai_400(
        _post(client, "/v1/chat/completions", body),
        "messages: Field required",
        "messages",
    )


def test_a_missing_field_on_responses_is_an_openai_400(client: TestClient) -> None:
    body = {"input": f"{MARKER} invented", "stream": True}
    _openai_400(_post(client, "/v1/responses", body), "model: Field required", "model")


@pytest.mark.parametrize(
    ("path", "body", "message", "param"),
    [
        (
            "/v1/chat/completions",
            {"model": "m", "messages": f"{MARKER} not a list"},
            "messages: Input should be a valid list",
            "messages",
        ),
        (
            "/v1/responses",
            {"model": "m", "input": "x", "tools": f"{MARKER} not a list"},
            "tools: Input should be a valid list",
            "tools",
        ),
    ],
)
def test_a_wrong_type_on_an_openai_door_names_the_field(
    client: TestClient, path: str, body: Any, message: str, param: str
) -> None:
    _openai_400(_post(client, path, body), message, param)


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/messages", unknown_block_body()),
        ("/v1/messages", missing_field_body()),
        ("/v1/messages/count_tokens", unknown_block_body()),
        ("/v1/chat/completions", {"model": "m", "messages": hook_text("chat")}),
        ("/v1/responses", {"input": hook_text("responses")}),
    ],
)
def test_no_door_ever_repeats_the_body(
    client: TestClient, path: str, body: Any
) -> None:
    response = _post(client, path, body)
    assert response.status_code == 400
    assert MARKER not in response.text
    assert len(response.content) < 4096
    assert '"input"' not in response.text
    assert '"ctx"' not in response.text


def test_many_failures_name_three_and_count_the_rest(client: TestClient) -> None:
    body = plain_body()
    body["messages"][0]["content"] = [
        {"type": f"invented_kind_{index}", "text": MARKER} for index in range(10)
    ]
    response = _post(client, "/v1/messages", body)
    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert message.count("Input tag 'invented_kind_") == 3
    assert message.endswith("; and 7 more")
    assert len(response.content) < 4096


def test_a_long_client_type_is_bounded(client: TestClient) -> None:
    body = plain_body()
    body["messages"][0]["content"] = [{"type": "x" * 5000}]
    message = _post(client, "/v1/messages", body).json()["error"]["message"]
    assert "x" * 65 not in message
    assert len(message) < MAX_REASON_CHARS + 64


def test_a_body_that_is_not_json_is_a_400_naming_the_body(client: TestClient) -> None:
    response = client.post(
        "/v1/messages",
        content=f'{{"model": "{MARKER}", '.encode(),
        headers={**HEADERS, "content-type": "application/json"},
    )
    _anthropic_400(response, "body: JSON decode error")


def test_a_valid_body_is_not_touched_by_the_handler(client: TestClient) -> None:
    # The handler only runs on a validation failure; a valid count is a 200.
    response = _post(client, "/v1/messages/count_tokens", plain_body())
    assert response.status_code == 200
    assert set(response.json()) == {"input_tokens"}


def test_an_admin_route_keeps_fastapis_422_with_its_detail_list(
    client: TestClient,
) -> None:
    response = client.get("/admin/api/requests?limit=not-a-number")
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    assert detail[0]["loc"] == ["query", "limit"]
    # FastAPI's own item, ``input`` included: nothing about it changed.
    assert detail[0]["input"] == "not-a-number"
    assert set(detail[0]) == {"type", "loc", "msg", "input"}


def test_reasons_merge_union_members_and_prefer_the_deeper_field() -> None:
    errors = [
        {
            "type": "string_type",
            "loc": ("body", "messages", 0, "content", "str"),
            "msg": "Input should be a valid string",
            "input": [MARKER],
        },
        {
            "type": "list_type",
            "loc": ("body", "stop", "str"),
            "msg": "Input should be a valid string",
            "input": 5,
        },
        {
            "type": "list_type",
            "loc": ("body", "stop", "list[str]"),
            "msg": "Input should be a valid list",
            "input": 5,
        },
        {
            "type": "union_tag_invalid",
            "loc": ("body", "messages", 0, "content", "list[tagged-union[A,B]]", 2),
            "msg": "Input tag 'x' found using 'type' does not match any of the"
            " expected tags: 'a', 'b'",
            "input": {"type": "x", "text": MARKER},
            "ctx": {"discriminator": "'type'", "tag": "x", "expected_tags": "'a', 'b'"},
        },
    ]
    assert describe_validation_errors(errors) == [
        ValidationReason(
            path="stop",
            reason="Input should be a valid string or Input should be a valid list",
        ),
        ValidationReason(
            path="messages.0.content.2",
            reason="Input tag 'x' found using 'type' does not match any of the"
            " expected tags: 'a', 'b'",
        ),
    ]


def test_a_reason_that_quotes_a_long_input_quotes_its_length_instead() -> None:
    value = f"{MARKER} " * 10
    reasons = describe_validation_errors(
        [
            {
                "type": "value_error",
                "loc": ("body", "model"),
                "msg": f"Value error, {value} is not a model",
                "input": value,
            }
        ]
    )
    assert reasons == [
        ValidationReason(
            path="model",
            reason=f"Value error, <{len(value)} characters> is not a model",
        )
    ]
    assert MARKER not in validation_error_message(reasons)


def test_an_empty_error_list_still_says_something() -> None:
    assert validation_error_message([]) == "body: the request body is not valid"


# --- the server.log line ------------------------------------------------------


def test_a_refusal_leaves_one_warning_with_metadata_only(client: TestClient) -> None:
    with patch.object(rrl, "logger") as stub:
        response = _post(client, "/v1/messages", unknown_block_body())
        assert response.status_code == 400
        _post(client, "/v1/messages", unknown_block_body())  # same client: counted

    assert stub.warning.call_count == 1
    args = stub.warning.call_args.args
    assert args[0].startswith("REJECTED REQUEST:")
    rendered = args[0].format(*args[1:])
    assert rendered.startswith(
        "REJECTED REQUEST: POST /v1/messages answered 400 -- "
        f"messages.0.content.1: {TAG_PREFIX} (1 field(s) failed) from testclient "
        f"(user-agent='{CLAUDE_CODE_UA}', harness=claude, messages=7, body "
    )
    assert rendered.endswith(" KB).")
    assert MARKER not in rendered


def test_a_valid_request_and_an_admin_422_leave_no_warning(client: TestClient) -> None:
    with patch.object(rrl, "logger") as stub:
        client.get("/admin/api/requests?limit=not-a-number")
        _post(client, "/v1/messages/count_tokens", plain_body())
    stub.warning.assert_not_called()


def _rejected(remote: str = "203.0.113.5") -> rrl.RejectedRequest:
    return rrl.RejectedRequest(
        client=rrl.RejectedClientKey(
            remote_address=remote, user_agent="curl/8.0", door="/v1/messages"
        ),
        method="POST",
        status=400,
        first=ValidationReason(path="messages", reason="Field required"),
        field_count=1,
        harness=None,
        message_count=None,
        body_bytes=12,
    )


def test_the_window_rolling_over_logs_one_summary_and_forgets_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_000.0
    monkeypatch.setattr(rrl.time, "monotonic", lambda: now)
    tracker = rrl.RejectedRequestTracker()
    tracker.note_rejection(_rejected())
    tracker.note_rejection(_rejected())
    with patch.object(rrl, "logger") as stub:
        tracker.note_rejection(_rejected(remote="198.51.100.9"))
    assert stub.warning.call_count == 1  # a new client, same window

    now = 1_000.0 + rrl.SUMMARY_WINDOW_SECONDS + 1.0
    with patch.object(rrl, "logger") as stub:
        tracker.note_rejection(_rejected())
    assert stub.warning.call_count == 2
    summary, fresh = (call.args for call in stub.warning.call_args_list)
    assert summary[0].startswith("REJECTED REQUEST SUMMARY:")
    assert summary[1:3] == (3, 2)
    assert fresh[0].startswith("REJECTED REQUEST:")
    rendered = fresh[0].format(*fresh[1:])
    assert "harness=unknown, messages=-, body 12 B)." in rendered
