"""Tests for shared multi-door web search/fetch tool loop, schema errors, and citations."""

import json
from typing import Any
from unittest.mock import patch

import pytest

from my_claude_code.api.web_tools.egress import WebFetchEgressViolation
from my_claude_code.api.web_tools.outbound import WebSearchRouteMeta
from my_claude_code.api.web_tools.request import (
    WebSearchToolOptions,
    intersect_allowed_domains,
)
from my_claude_code.api.web_tools.tool_loop import (
    SYNTHETIC_WEB_FETCH_TOOL_NAME,
    SYNTHETIC_WEB_SEARCH_TOOL_NAME,
    WEB_FETCH_TOOL_SCHEMA,
    WEB_SEARCH_TOOL_SCHEMA,
    GatheredSource,
    WebToolLoopConfig,
    execute_web_tool_loop,
    extract_grounded_citations,
    sanitize_messages_for_loop,
)
from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import (
    ContentBlockText,
    ContentBlockThinking,
    ContentBlockToolResult,
    Message,
    MessagesRequest,
    Tool,
)
from my_claude_code.core.anthropic.conversion import (
    AnthropicToOpenAIConverter,
    OpenAIConversionError,
)
from my_claude_code.core.websearch.models import WebSearchResponse, WebSearchResultItem


def _fake_search_response(
    query: str,
    *,
    url: str = "https://docs.python.org/3/",
    title: str = "Python 3 Docs",
) -> tuple[WebSearchResponse, list[dict[str, str]], WebSearchRouteMeta]:
    item = WebSearchResultItem(
        title=title,
        url=url,
        snippet="Official Python documentation.",
        content=None,
        published="2026-10-01",
    )
    resp = WebSearchResponse(
        provider="exa", query=query, results=(item,), key_index=0, cost_usd=None
    )
    items = [
        {
            "title": title,
            "url": url,
            "snippet": "Official Python documentation.",
            "content": "",
            "published": "2026-10-01",
            "answer": "",
            "provider": "exa",
        }
    ]
    meta = WebSearchRouteMeta(
        route_id="route_1",
        primary_provider="exa",
        terminal_provider="exa",
        providers_tried=("exa",),
        fallback_used=False,
        supports_domain_filters=True,
    )
    return resp, items, meta


@pytest.mark.asyncio
async def test_loop_preserves_caller_tool_prefix_and_rejects_name_collision() -> None:
    custom_a = Tool.model_validate(
        {
            "name": "get_weather",
            "description": "Weather",
            "input_schema": {"type": "object"},
            "cache_control": {"type": "ephemeral"},
        }
    )
    custom_b = Tool(name="run_sql", description="SQL", input_schema={"type": "object"})
    req = MessagesRequest(
        model="m",
        max_tokens=100,
        messages=[Message(role="user", content="Hi")],
        tools=[custom_a, custom_b, Tool(name="web_search", type="web_search_20250305")],
    )
    seen_tools: list[list[str]] = []

    async def call_step(step_req: MessagesRequest, _idx: int) -> dict[str, Any]:
        seen_tools.append([t.name for t in (step_req.tools or [])])
        # Caller's cache_control prefix on custom_a must remain intact at index 0.
        assert step_req.tools is not None
        assert getattr(step_req.tools[0], "cache_control", None) == {
            "type": "ephemeral"
        }
        return {
            "id": "msg_1",
            "model": "m",
            "role": "assistant",
            "content": [{"type": "text", "text": "Done"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 4},
        }

    result = await execute_web_tool_loop(
        req,
        WebToolLoopConfig(enable_search=True, enable_fetch=True),
        Settings(),
        call_step,
    )
    assert seen_tools == [
        [
            "get_weather",
            "run_sql",
            SYNTHETIC_WEB_SEARCH_TOOL_NAME,
            SYNTHETIC_WEB_FETCH_TOOL_NAME,
        ]
    ]
    assert result.final_step_blocks == [{"type": "text", "text": "Done"}]

    colliding = MessagesRequest(
        model="m",
        max_tokens=100,
        messages=[Message(role="user", content="Hi")],
        tools=[
            Tool(name=SYNTHETIC_WEB_SEARCH_TOOL_NAME, input_schema={"type": "object"})
        ],
    )
    with pytest.raises(InvalidRequestError, match="reserved"):
        await execute_web_tool_loop(
            colliding, WebToolLoopConfig(enable_search=True), Settings(), call_step
        )


def test_history_sanitization_protects_loop_while_direct_openai_reject_stays_intact() -> (
    None
):
    thinking = ContentBlockThinking(type="thinking", thinking="hmm", signature="sig1")
    hist_msg = Message.model_validate(
        {
            "role": "assistant",
            "content": [
                thinking.model_dump(),
                {
                    "type": "server_tool_use",
                    "id": "srvtoolu_1",
                    "name": "web_search",
                    "input": {"query": "python"},
                },
                {
                    "type": "web_search_tool_result",
                    "tool_use_id": "srvtoolu_1",
                    "content": [
                        {
                            "type": "web_search_result",
                            "title": "Py",
                            "url": "https://python.org",
                        }
                    ],
                },
                {"type": "text", "text": "Answer"},
            ],
        }
    )
    # Direct OpenAI conversion still rejects raw server_tool_use blocks cleanly (IV.7).
    with pytest.raises(OpenAIConversionError, match="Anthropic server tool blocks"):
        AnthropicToOpenAIConverter.convert_messages([hist_msg])

    # Loop sanitization converts historical server tool blocks AFTER leading thinking!
    sanitized = sanitize_messages_for_loop([hist_msg])
    blocks = sanitized[0].content
    assert isinstance(blocks, list)
    assert blocks[0].type == "thinking"
    assert isinstance(blocks[1], ContentBlockText)
    assert "python.org" in blocks[1].text
    assert all(
        getattr(b, "type", None) not in ("server_tool_use", "web_search_tool_result")
        for b in blocks
    )


@pytest.mark.asyncio
async def test_max_steps_runs_final_summarization_step_without_synthetic_tools() -> (
    None
):
    req = MessagesRequest(
        model="m",
        max_tokens=100,
        messages=[Message(role="user", content="Find latest news")],
    )
    step_tools: list[list[str]] = []

    async def call_step(step_req: MessagesRequest, step_idx: int) -> dict[str, Any]:
        step_tools.append([t.name for t in (step_req.tools or [])])
        if step_idx < 2:
            return {
                "id": f"msg_{step_idx}",
                "model": "m",
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"toolu_{step_idx}",
                        "name": SYNTHETIC_WEB_SEARCH_TOOL_NAME,
                        "input": {"query": f"query-{step_idx}"},
                    }
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        # Step 2 (final summarization): synthetic tools MUST NOT be present.
        return {
            "id": "msg_final",
            "model": "m",
            "role": "assistant",
            "content": [
                {"type": "text", "text": "See https://docs.python.org/3/ for details."}
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 15, "output_tokens": 8},
        }

    with patch(
        "my_claude_code.api.web_tools.outbound._run_web_search_response",
        side_effect=lambda q, *_args, **_kw: _fake_search_response(q),
    ):
        result = await execute_web_tool_loop(
            req,
            WebToolLoopConfig(enable_search=True, max_steps=2),
            Settings(),
            call_step,
        )

    assert step_tools == [
        [SYNTHETIC_WEB_SEARCH_TOOL_NAME],
        [SYNTHETIC_WEB_SEARCH_TOOL_NAME],
        [],
    ]
    assert result.loop_limit_hit is True
    assert result.steps_executed == 3
    assert result.usage["input_tokens"] == 35
    assert result.usage["output_tokens"] == 18
    assert result.usage["server_tool_use"] == {"web_search_requests": 2}
    assert len(result.grounded_citations) == 1
    assert result.grounded_citations[0]["url"] == "https://docs.python.org/3/"


@pytest.mark.asyncio
async def test_invalid_tool_input_feeds_schema_back_and_allows_self_correction() -> (
    None
):
    """M11: invalid __mcc_web_search arguments return JSON schema in tool_result."""
    req = MessagesRequest(
        model="m",
        max_tokens=100,
        messages=[Message(role="user", content="Search python")],
    )
    seen_tool_results: list[str] = []

    async def call_step(step_req: MessagesRequest, step_idx: int) -> dict[str, Any]:
        if step_idx == 0:
            # Missing 'query' + extra key -> fails validation and feeds back schema!
            return {
                "id": "msg_0",
                "model": "m",
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_bad",
                        "name": SYNTHETIC_WEB_SEARCH_TOOL_NAME,
                        "input": {"q": "python"},
                    }
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 5, "output_tokens": 3},
            }
        # Verify the loop sent the full expected JSON Schema back to the model.
        last_msg = step_req.messages[-1]
        assert isinstance(last_msg.content, list)
        first_block = last_msg.content[0]
        assert isinstance(first_block, ContentBlockToolResult)
        seen_tool_results.append(str(first_block.content))
        if step_idx == 1:
            return {
                "id": "msg_1",
                "model": "m",
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_good",
                        "name": SYNTHETIC_WEB_SEARCH_TOOL_NAME,
                        "input": {"query": "python 3.14"},
                    }
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 8, "output_tokens": 4},
            }
        return {
            "id": "msg_2",
            "model": "m",
            "role": "assistant",
            "content": [{"type": "text", "text": "Read https://docs.python.org/3/"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }

    with patch(
        "my_claude_code.api.web_tools.outbound._run_web_search_response",
        side_effect=lambda q, *_args, **_kw: _fake_search_response(q),
    ):
        result = await execute_web_tool_loop(
            req,
            WebToolLoopConfig(enable_search=True, max_steps=5),
            Settings(),
            call_step,
        )

    assert len(seen_tool_results) >= 1
    assert json.dumps(WEB_SEARCH_TOOL_SCHEMA, sort_keys=True) in seen_tool_results[0]
    assert result.web_calls[0].status == "failed"
    assert result.web_calls[0].error_code == "invalid_tool_input"
    assert result.web_calls[1].status == "completed"
    assert result.usage["server_tool_use"] == {"web_search_requests": 1}

    # Same contract for web_fetch missing url -> includes WEB_FETCH_TOOL_SCHEMA.
    async def bad_fetch_only(
        step_req: MessagesRequest, step_idx: int
    ) -> dict[str, Any]:
        if step_idx == 0:
            return {
                "id": "msg_f0",
                "model": "m",
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_fbad",
                        "name": SYNTHETIC_WEB_FETCH_TOOL_NAME,
                        "input": {},
                    }
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 5, "output_tokens": 2},
            }
        last_msg = step_req.messages[-1]
        assert isinstance(last_msg.content, list)
        first_block = last_msg.content[0]
        assert isinstance(first_block, ContentBlockToolResult)
        assert json.dumps(WEB_FETCH_TOOL_SCHEMA, sort_keys=True) in str(
            first_block.content
        )
        return {
            "id": "msg_f1",
            "model": "m",
            "role": "assistant",
            "content": [{"type": "text", "text": "Done"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 2},
        }

    fetch_res = await execute_web_tool_loop(
        req,
        WebToolLoopConfig(enable_search=False, enable_fetch=True),
        Settings(),
        bad_fetch_only,
    )
    assert fetch_res.web_calls[0].error_code == "invalid_tool_input"


@pytest.mark.asyncio
async def test_max_uses_external_web_access_mixed_turn_and_ssrf_redirect() -> None:
    req = MessagesRequest(
        model="m",
        max_tokens=100,
        messages=[Message(role="user", content="Check weather after searching")],
        tools=[Tool(name="get_weather", input_schema={"type": "object"})],
    )

    # 1. Mixed client + web turn executes synthetic web search AND returns client tool_use intact.
    async def mixed_step(step_req: MessagesRequest, _idx: int) -> dict[str, Any]:
        return {
            "id": "msg_mix",
            "model": "m",
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Checking web and weather."},
                {
                    "type": "tool_use",
                    "id": "toolu_ws",
                    "name": SYNTHETIC_WEB_SEARCH_TOOL_NAME,
                    "input": {"query": "weather api"},
                },
                {
                    "type": "tool_use",
                    "id": "toolu_weather",
                    "name": "get_weather",
                    "input": {"city": "Paris"},
                },
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 12, "output_tokens": 8},
        }

    with patch(
        "my_claude_code.api.web_tools.outbound._run_web_search_response",
        side_effect=lambda q, *_args, **_kw: _fake_search_response(q),
    ):
        mixed = await execute_web_tool_loop(
            req,
            WebToolLoopConfig(
                enable_search=True,
                search_options=WebSearchToolOptions(max_uses=1),
            ),
            Settings(),
            mixed_step,
        )
    assert mixed.stop_reason == "tool_use"
    types = [b["type"] for b in mixed.all_client_blocks]
    assert types == ["text", "server_tool_use", "web_search_tool_result", "tool_use"]
    assert mixed.all_client_blocks[-1]["name"] == "get_weather"
    assert not any(
        b.get("name") == SYNTHETIC_WEB_SEARCH_TOOL_NAME for b in mixed.all_client_blocks
    )

    # 1b. M4 guard: max_uses=1 executes 1st call and rejects 2nd call with max_uses_exceeded.
    async def two_search_steps(
        step_req: MessagesRequest, step_idx: int
    ) -> dict[str, Any]:
        if step_idx == 0:
            return {
                "id": "msg_u0",
                "model": "m",
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_u0",
                        "name": SYNTHETIC_WEB_SEARCH_TOOL_NAME,
                        "input": {"query": "first"},
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_u1",
                        "name": SYNTHETIC_WEB_SEARCH_TOOL_NAME,
                        "input": {"query": "second"},
                    },
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 6, "output_tokens": 4},
            }
        return {
            "id": "msg_u1",
            "model": "m",
            "role": "assistant",
            "content": [{"type": "text", "text": "Only used first search."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 8, "output_tokens": 4},
        }

    with patch(
        "my_claude_code.api.web_tools.outbound._run_web_search_response",
        side_effect=lambda q, *_args, **_kw: _fake_search_response(q),
    ) as mocked_two:
        budget_res = await execute_web_tool_loop(
            req,
            WebToolLoopConfig(
                enable_search=True, search_options=WebSearchToolOptions(max_uses=1)
            ),
            Settings(),
            two_search_steps,
        )
    assert mocked_two.call_count == 1
    assert budget_res.web_calls[0].status == "completed"
    assert budget_res.web_calls[1].status == "failed"
    assert budget_res.web_calls[1].error_code == "max_uses_exceeded"

    # 2. external_web_access=False must execute ZERO outbound search calls and emit 'unavailable'.
    with patch(
        "my_claude_code.api.web_tools.outbound._run_web_search_response",
        side_effect=AssertionError("must not call outbound"),
    ):
        disabled = await execute_web_tool_loop(
            req,
            WebToolLoopConfig(enable_search=True, search_external_web_access=False),
            Settings(),
            mixed_step,
        )
    assert disabled.web_calls[0].error_code == "unavailable"

    # 3. SSRF violation and redirect-to-blocked-domain both map cleanly to 'url_not_allowed'.
    async def fetch_step(_req: MessagesRequest, _idx: int) -> dict[str, Any]:
        return {
            "id": "msg_fetch",
            "model": "m",
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_f",
                    "name": SYNTHETIC_WEB_FETCH_TOOL_NAME,
                    "input": {"url": "https://allowed.example/page"},
                },
                {
                    "type": "tool_use",
                    "id": "toolu_weather",
                    "name": "get_weather",
                    "input": {},
                },
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 5, "output_tokens": 3},
        }

    with patch(
        "my_claude_code.api.web_tools.outbound._run_web_fetch_detail",
        side_effect=WebFetchEgressViolation("private target blocked"),
    ):
        ssrf_res = await execute_web_tool_loop(
            req,
            WebToolLoopConfig(
                enable_search=False,
                enable_fetch=True,
                fetch_options=WebSearchToolOptions(
                    allowed_domains=("allowed.example",)
                ),
            ),
            Settings(),
            fetch_step,
        )
    assert ssrf_res.web_calls[0].error_code == "url_not_allowed"

    # Redirected final URL outside allowed_domains is also blocked cleanly!
    with patch(
        "my_claude_code.api.web_tools.outbound._run_web_fetch_detail",
        return_value=(
            {
                "url": "https://evil.example/leak",
                "title": "Redirected",
                "media_type": "text/plain",
                "data": "secret",
            },
            "basic",
            False,
        ),
    ):
        redir_res = await execute_web_tool_loop(
            req,
            WebToolLoopConfig(
                enable_search=False,
                enable_fetch=True,
                fetch_options=WebSearchToolOptions(
                    allowed_domains=("allowed.example",)
                ),
            ),
            Settings(),
            fetch_step,
        )
    assert redir_res.web_calls[0].error_code == "url_not_allowed"


def test_domain_intersection_and_grounded_citation_rules() -> None:
    narrowed, disjoint = intersect_allowed_domains(
        ("python.org", "github.com"), ("docs.python.org",)
    )
    assert narrowed == ("docs.python.org",)
    assert disjoint is False

    empty, is_disjoint = intersect_allowed_domains(("a.com",), ("b.com",))
    assert empty == ()
    assert is_disjoint is True

    sources = [
        GatheredSource(url="https://docs.python.org/3/", title="Python 3"),
    ]
    text = (
        "Official ref [Py3](https://docs.python.org/3/) and fake "
        "https://hallucinated.example/nope."
    )
    citations = extract_grounded_citations(text, sources)
    assert len(citations) == 1
    assert citations[0]["url"] == "https://docs.python.org/3/"
    s, e = citations[0]["start_index"], citations[0]["end_index"]
    assert 0 <= s < e <= len(text)
    assert text[s:e] == "[Py3](https://docs.python.org/3/)"
