"""A resumed Claude Code 2.1.304 session is accepted at both Anthropic doors.

RESUMED-SESSION-422 PR 1: the request validates, the handler folds its tool
changes before routing, and what reaches the provider is the folded request --
the same request, field for field, as the conversation would be had Claude
Code sent the tool list instead of the changes. Bodies are invented
(:mod:`tests.api.tool_change_bodies`).
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import MessagesRequest, get_token_count
from my_claude_code.providers.nvidia_nim import NvidiaNimProvider
from tests.api.support import create_test_app
from tests.api.tool_change_bodies import (
    ADDED_TOOLS,
    CLAUDE_CODE_UA,
    FOLDED_TOOLS,
    REMOVED_TOOLS,
    plain_body,
    resumed_session_body,
)

_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
_provider = MagicMock(spec=NvidiaNimProvider)


async def _record_stream(*args: Any, **kwargs: Any):
    _calls.append((args, kwargs))
    yield "event: message_start\ndata: {}\n\n"
    yield "[DONE]\n\n"


_provider.stream_response = _record_stream
HEADERS = {"user-agent": CLAUDE_CODE_UA, "x-mcc-harness": "claude"}


@pytest.fixture(scope="module")
def client():
    settings = Settings()
    settings.model = "opencode/test-model"
    app = create_test_app(settings, providers={"opencode": _provider})
    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=_provider),
        TestClient(app) as test_client,
    ):
        yield test_client


def _routed(client: TestClient, body: dict[str, Any]) -> MessagesRequest:
    _calls.clear()
    response = client.post("/v1/messages", json=body, headers=HEADERS)
    assert response.status_code == 200, response.text[:500]
    assert len(_calls) == 1
    routed = _calls[0][0][0]
    assert isinstance(routed, MessagesRequest)
    return routed


def _shape(request: MessagesRequest) -> dict[str, Any]:
    """Everything a provider reads, without the fields routing rewrites."""
    dumped = request.model_dump(mode="json", exclude_none=True)
    return {key: dumped.get(key) for key in ("messages", "system", "tools")}


def test_a_resumed_session_reaches_the_provider_with_its_tools_folded(
    client: TestClient,
) -> None:
    routed = _routed(client, resumed_session_body())

    names = [tool.name for tool in routed.tools or ()]
    assert names == list(FOLDED_TOOLS)
    assert ADDED_TOOLS[0] in names
    assert not set(REMOVED_TOOLS) & set(names)
    kinds = {
        block.type
        for message in routed.messages
        if isinstance(message.content, list)
        for block in message.content
    }
    assert not kinds & {"tool_addition", "tool_removal"}
    # The resume's breakpoint left the stripped removal for the hook text.
    last = routed.messages[-1].content
    assert isinstance(last, list)
    assert last[-1].model_dump()["cache_control"] == {"type": "ephemeral"}


def test_the_folded_request_is_the_request_the_tool_list_would_have_been(
    client: TestClient,
) -> None:
    folded = _shape(_routed(client, resumed_session_body()))
    plain = _shape(_routed(client, plain_body()))
    assert folded == plain


def test_a_body_without_tool_changes_reaches_the_provider_as_it_was_sent(
    client: TestClient,
) -> None:
    body = plain_body()
    routed = _routed(client, body)
    assert _shape(routed) == _shape(MessagesRequest.model_validate(body))


def test_count_tokens_counts_the_folded_tool_list(client: TestClient) -> None:
    response = client.post(
        "/v1/messages/count_tokens", json=resumed_session_body(), headers=HEADERS
    )
    assert response.status_code == 200, response.text[:500]
    plain = MessagesRequest.model_validate(plain_body())
    assert response.json() == {
        "input_tokens": get_token_count(plain.messages, plain.system, plain.tools)
    }
    unfolded = client.post(
        "/v1/messages/count_tokens", json=plain_body(), headers=HEADERS
    )
    assert unfolded.json() == response.json()
