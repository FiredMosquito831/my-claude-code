"""Claude Code 2.1.304's tool changes, folded into ``tools`` (RESUMED-SESSION-422).

Every body here is invented: the shapes follow the investigation's census
(``specs/INVESTIGATION-RESUMED-SESSION-422.md`` §1.1, §1.6), the text does not
come from any conversation.
"""

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from my_claude_code.core.anthropic import (
    ContentBlockText,
    ContentBlockToolAddition,
    ContentBlockToolRemoval,
    ContentBlockToolResult,
    MessagesRequest,
    TokenCountRequest,
    carries_tool_changes,
    fold_tool_changes,
)


def _tool(name: str, description: str | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": description or f"the {name} tool",
        "input_schema": {"type": "object", "properties": {}},
    }


def _define(name: str, description: str | None = None) -> dict[str, Any]:
    return {
        "type": "tool_addition",
        "tool": {"type": "tool_definition", "definition": _tool(name, description)},
    }


def _refer(kind: str, name: str) -> dict[str, Any]:
    return {"type": kind, "tool": {"type": "tool_reference", "name": name}}


def _request(
    messages: list[dict[str, Any]],
    tools: tuple[str, ...] | list[str] | None = ("Bash", "Read"),
) -> MessagesRequest:
    payload: dict[str, Any] = {
        "model": "claude-fable-5-1",
        "max_tokens": 64,
        "messages": messages,
    }
    if tools is not None:
        payload["tools"] = [_tool(name) for name in tools]
    return MessagesRequest.model_validate(payload)


def _names(request: MessagesRequest | TokenCountRequest) -> list[str]:
    return [tool.name for tool in request.tools or ()]


def _types(request: MessagesRequest) -> list[list[str]]:
    return [
        [block.type for block in message.content]
        if isinstance(message.content, list)
        else ["str"]
        for message in request.messages
    ]


USER = {"role": "user", "content": "invented question"}


def test_both_blocks_validate_and_keep_their_cache_control() -> None:
    request = _request(
        [
            USER,
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": "invented hook text"},
                    _define("Lookup"),
                    {
                        **_refer("tool_removal", "Read"),
                        "cache_control": {"type": "ephemeral"},
                    },
                ],
            },
        ]
    )
    blocks = request.messages[1].content
    assert isinstance(blocks, list)
    assert isinstance(blocks[1], ContentBlockToolAddition)
    assert isinstance(blocks[2], ContentBlockToolRemoval)
    assert blocks[2].model_dump()["cache_control"] == {"type": "ephemeral"}


def test_a_request_without_tool_changes_is_the_same_object() -> None:
    request = _request(
        [
            USER,
            {"role": "system", "content": [{"type": "text", "text": "invented"}]},
            {"role": "assistant", "content": "invented answer"},
        ]
    )
    fold = fold_tool_changes(request)

    assert fold.request is request
    assert not fold.applied
    assert carries_tool_changes(request.messages) is False


def test_a_definition_is_added_and_its_block_removed() -> None:
    request = _request(
        [
            USER,
            {
                "role": "system",
                "content": [{"type": "text", "text": "invented"}, _define("Lookup")],
            },
        ]
    )
    fold = fold_tool_changes(request)

    assert _names(fold.request) == ["Bash", "Read", "Lookup"]
    assert _types(fold.request) == [["str"], ["text"]]
    assert fold.added == ("Lookup",)
    assert fold.blocks == 1
    added = (fold.request.tools or [])[-1]
    assert added.input_schema == {"type": "object", "properties": {}}


def test_a_removed_tool_is_no_longer_offered() -> None:
    request = _request(
        [
            USER,
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": "invented"},
                    _refer("tool_removal", "Read"),
                ],
            },
        ]
    )
    fold = fold_tool_changes(request)

    assert _names(fold.request) == ["Bash"]
    assert fold.removed == ("Read",)


def test_add_then_remove_ends_removed_and_remove_then_add_ends_offered() -> None:
    add_then_remove = _request(
        [
            {"role": "system", "content": [_define("Lookup")]},
            USER,
            {"role": "system", "content": [_refer("tool_removal", "Lookup")]},
            {"role": "assistant", "content": "invented"},
        ]
    )
    assert _names(fold_tool_changes(add_then_remove).request) == ["Bash", "Read"]

    remove_then_add = _request(
        [
            {"role": "system", "content": [_refer("tool_removal", "Bash")]},
            USER,
            {"role": "system", "content": [_refer("tool_addition", "Bash")]},
            {"role": "assistant", "content": "invented"},
        ]
    )
    fold = fold_tool_changes(remove_then_add)
    # A re-offered tool goes to the end, with the definition ``tools`` gave it.
    assert _names(fold.request) == ["Read", "Bash"]
    assert (fold.request.tools or [])[-1] is (remove_then_add.tools or [])[0]


def test_a_reference_re_offers_a_tool_an_earlier_addition_defined() -> None:
    request = _request(
        [
            {"role": "system", "content": [_define("Lookup", "first schema")]},
            USER,
            {"role": "system", "content": [_refer("tool_removal", "Lookup")]},
            {"role": "assistant", "content": "invented"},
            {"role": "system", "content": [_define("Lookup", "second schema")]},
            {"role": "user", "content": "invented"},
            {"role": "system", "content": [_refer("tool_removal", "Lookup")]},
            {"role": "system", "content": [_refer("tool_addition", "Lookup")]},
        ]
    )
    fold = fold_tool_changes(request)

    assert _names(fold.request) == ["Bash", "Read", "Lookup"]
    # The definition it was last given.
    assert (fold.request.tools or [])[-1].description == "second schema"


def test_a_duplicate_name_keeps_the_first_definition() -> None:
    request = _request(
        [
            {
                "role": "system",
                "content": [_define("Bash", "a second Bash"), _define("Lookup")],
            },
            USER,
        ]
    )
    fold = fold_tool_changes(request)

    assert _names(fold.request) == ["Bash", "Read", "Lookup"]
    assert (fold.request.tools or [])[0].description == "the Bash tool"
    assert fold.added == ("Lookup",)


def test_the_order_is_deterministic_from_the_body() -> None:
    def body() -> MessagesRequest:
        return _request(
            [
                {
                    "role": "system",
                    "content": [_define("Zeta"), _define("Alpha"), _define("Mid")],
                },
                USER,
                {"role": "system", "content": [_refer("tool_removal", "Alpha")]},
            ]
        )

    first = fold_tool_changes(body()).request
    second = fold_tool_changes(body()).request
    assert _names(first) == ["Bash", "Read", "Zeta", "Mid"]
    assert first.model_dump_json() == second.model_dump_json()


def test_a_breakpoint_on_a_stripped_block_moves_to_the_previous_block() -> None:
    # The resume's own shape: text, then three removals, the breakpoint on the
    # last removal (``messages/N/content/3`` in the census).
    request = _request(
        [
            USER,
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": "invented hook text"},
                    _refer("tool_removal", "Read"),
                    _refer("tool_removal", "Bash"),
                    {
                        **_refer("tool_removal", "Gone"),
                        "cache_control": {"type": "ephemeral", "ttl": "1h"},
                    },
                ],
            },
        ],
        tools=["Bash", "Read", "Gone", "Kept"],
    )
    fold = fold_tool_changes(request)

    assert _names(fold.request) == ["Kept"]
    system = fold.request.messages[1].content
    assert isinstance(system, list)
    assert len(system) == 1
    assert system[0].model_dump() == {
        "type": "text",
        "text": "invented hook text",
        "cache_control": {"type": "ephemeral", "ttl": "1h"},
    }
    assert fold.breakpoints_moved == 1
    # The request the client sent is untouched.
    original = request.messages[1].content
    assert isinstance(original, list)
    assert "cache_control" not in original[0].model_dump()
    assert len(original) == 4


def test_an_emptied_system_message_is_dropped_and_its_breakpoint_moves_back() -> None:
    request = _request(
        [
            {
                "role": "user",
                "content": [{"type": "text", "text": "invented question"}],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "invented answer"},
                    {"type": "thinking", "thinking": "invented", "signature": "sig"},
                ],
            },
            {
                "role": "system",
                "content": [
                    _define("Lookup"),
                    {**_define("Other"), "cache_control": {"type": "ephemeral"}},
                ],
            },
        ]
    )
    fold = fold_tool_changes(request)

    assert [message.role for message in fold.request.messages] == ["user", "assistant"]
    assert fold.messages_dropped == 1
    assistant = fold.request.messages[1].content
    assert isinstance(assistant, list)
    # Anthropic refuses a breakpoint on a thinking block: it skips to the text.
    assert assistant[0].model_dump()["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in assistant[1].model_dump()
    assert _names(fold.request) == ["Bash", "Read", "Lookup", "Other"]


def test_the_breakpoint_goes_to_the_nearest_earlier_block_not_the_first() -> None:
    request = _request(
        [
            USER,
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "invented answer"},
                    {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}},
                ],
            },
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": "invented first"},
                    {"type": "text", "text": "invented second"},
                    {**_define("Lookup"), "cache_control": {"type": "ephemeral"}},
                ],
            },
            {
                "role": "system",
                "content": [
                    {
                        **_refer("tool_removal", "Read"),
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            },
        ]
    )
    fold = fold_tool_changes(request)
    system = fold.request.messages[2].content
    assert isinstance(system, list)
    assert [("cache_control" in block.model_dump()) for block in system] == [
        False,
        True,
    ]
    # The emptied last message's breakpoint walks back to the message before.
    assert len(fold.request.messages) == 3
    assistant = fold.request.messages[1].content
    assert isinstance(assistant, list)
    assert [("cache_control" in block.model_dump()) for block in assistant] == [
        False,
        False,
    ]
    assert fold.breakpoints_moved == 2


def test_a_block_that_already_has_a_breakpoint_keeps_its_own() -> None:
    request = _request(
        [
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": "invented",
                        "cache_control": {"type": "ephemeral", "ttl": "1h"},
                    },
                    {**_define("Lookup"), "cache_control": {"type": "ephemeral"}},
                ],
            },
            USER,
        ]
    )
    fold = fold_tool_changes(request)
    system = fold.request.messages[0].content
    assert isinstance(system, list)
    assert system[0].model_dump()["cache_control"] == {
        "type": "ephemeral",
        "ttl": "1h",
    }


def test_a_breakpoint_with_nowhere_earlier_to_go_is_counted_as_dropped() -> None:
    request = _request(
        [
            {
                "role": "system",
                "content": [
                    {**_define("Lookup"), "cache_control": {"type": "ephemeral"}}
                ],
            },
            USER,
        ]
    )
    fold = fold_tool_changes(request)
    assert fold.breakpoints_dropped == 1
    assert [message.role for message in fold.request.messages] == ["user"]


def test_what_cannot_be_offered_is_dropped_and_counted() -> None:
    request = _request(
        [
            {
                "role": "system",
                "content": [
                    {
                        "type": "tool_addition",
                        "tool": {"type": "mcp_toolset_reference", "server_name": "srv"},
                    },
                    {
                        "type": "tool_removal",
                        "tool": {
                            "type": "mcp_tool_reference",
                            "server_name": "srv",
                            "name": "x",
                        },
                    },
                    _refer("tool_addition", "NeverDefined"),
                    {
                        "type": "tool_addition",
                        "tool": {"type": "tool_definition", "definition": {"x": 1}},
                    },
                    {"type": "text", "text": "invented"},
                ],
            },
            USER,
        ]
    )
    fold = fold_tool_changes(request)

    assert fold.unresolved == 4
    assert _names(fold.request) == ["Bash", "Read"]
    assert _types(fold.request) == [["text"], ["str"]]


def test_no_tools_stays_no_tools_unless_something_was_added() -> None:
    nothing = _request(
        [{"role": "system", "content": [_refer("tool_removal", "X")]}, USER],
        tools=None,
    )
    assert fold_tool_changes(nothing).request.tools is None

    added = _request(
        [{"role": "system", "content": [_define("Lookup")]}, USER], tools=None
    )
    assert _names(fold_tool_changes(added).request) == ["Lookup"]


def test_the_folded_request_carries_every_other_field_unchanged() -> None:
    payload: dict[str, Any] = {
        "model": "claude-fable-5-1",
        "max_tokens": 64,
        "stream": True,
        "system": [{"type": "text", "text": "invented system"}],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
        "metadata": {"user_id": "invented"},
        "tools": [_tool("Bash")],
        "messages": [USER, {"role": "system", "content": [_define("Lookup")]}],
    }
    request = MessagesRequest.model_validate(payload)
    folded = fold_tool_changes(request).request
    before = request.model_dump()
    after = folded.model_dump()
    for field in ("messages", "tools"):
        before.pop(field)
        after.pop(field)
    assert after == before


def test_token_count_requests_fold_the_same_way() -> None:
    request = TokenCountRequest.model_validate(
        {
            "model": "claude-fable-5-1",
            "tools": [_tool("Bash")],
            "messages": [
                USER,
                {
                    "role": "system",
                    "content": [_define("Lookup"), _refer("tool_removal", "Bash")],
                },
            ],
        }
    )
    fold = fold_tool_changes(request)
    assert isinstance(fold.request, TokenCountRequest)
    assert _names(fold.request) == ["Lookup"]


def test_per_turn_settings_on_a_system_message_are_ignored_not_refused() -> None:
    # Spec decision Q7: ``output_config`` and ``clear_at`` validate and are not
    # carried anywhere; the request's own effort applies.
    request = _request(
        [
            USER,
            {
                "role": "system",
                "output_config": {"effort": "low"},
                "clear_at": "next_user_message",
                "content": [{"type": "text", "text": "invented"}, _define("Lookup")],
            },
        ]
    )
    folded = fold_tool_changes(request).request
    assert folded.messages[1].model_dump() == {
        "role": "system",
        "content": [{"type": "text", "text": "invented"}],
        "reasoning_content": None,
    }


def test_the_input_dicts_are_never_mutated() -> None:
    messages = [
        USER,
        {
            "role": "system",
            "content": [
                {"type": "text", "text": "invented"},
                {**_define("Lookup"), "cache_control": {"type": "ephemeral"}},
            ],
        },
    ]
    snapshot = copy.deepcopy(messages)
    request = _request(messages)
    dumped = request.model_dump_json()
    fold_tool_changes(request)
    assert messages == snapshot
    assert request.model_dump_json() == dumped


def test_a_tool_result_without_content_validates() -> None:
    request = _request(
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_1"}],
            },
        ]
    )
    block = request.messages[1].content[0]
    assert isinstance(block, ContentBlockToolResult)
    assert block.content is None
    assert block.model_dump(exclude_none=True) == {
        "type": "tool_result",
        "tool_use_id": "toolu_1",
    }


def test_an_unknown_block_is_one_error_naming_the_tag() -> None:
    with pytest.raises(ValidationError) as caught:
        _request(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "invented"},
                        {"type": "compaction", "content": "invented"},
                    ],
                }
            ]
        )
    errors = caught.value.errors()
    list_errors = [error for error in errors if "list[" in str(error["loc"])]
    assert len(list_errors) == 1
    assert list_errors[0]["type"] == "union_tag_invalid"
    assert str(list_errors[0]["msg"]).startswith(
        "Input tag 'compaction' found using 'type' does not match any of the"
        " expected tags:"
    )


def test_known_blocks_parse_into_the_same_models_as_before() -> None:
    request = _request(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "invented", "citations": []},
                    {"type": "image", "source": {"type": "url", "url": "x"}},
                ],
            }
        ]
    )
    blocks = request.messages[0].content
    assert isinstance(blocks, list)
    assert isinstance(blocks[0], ContentBlockText)
    assert blocks[0].model_dump() == {
        "type": "text",
        "text": "invented",
        "citations": [],
    }
