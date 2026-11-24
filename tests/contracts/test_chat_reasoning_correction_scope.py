"""Guard the finite 8.40.0 Chat reasoning replay & MiniMax OFF expectations."""

from tests.contracts.chat_reasoning_expectations import chat_corrections


def test_chat_reasoning_corrections_cover_only_commandcode_and_minimax_off() -> None:
    thinking_fixtures = {
        "string_turn_with_reasoning_content",
        "thinking_after_tool_call",
        "thinking_only_turn",
        "thinking_then_text",
        "thinking_tool_loop",
        "thinking_with_images",
    }
    all_fixtures = thinking_fixtures | {
        "long_mcp_name_and_no_thinking",
        "redacted_thinking",
    }
    expected_history = {
        f"openai_chat/commandcode(standalone)/{fixture}/{policy}"
        for fixture in thinking_fixtures
        for policy in ("off", "on_high")
    } | {f"openai_chat/minimax/{fixture}/off" for fixture in all_fixtures}
    assert set(chat_corrections("history")) == expected_history

    assert set(chat_corrections("empty")) == {"commandcode_chat"}

    market = chat_corrections("market")
    assert len(market) == 68
    assert all(key.endswith("/off/" + key.rsplit("/", 1)[1]) for key in market)

    for group in ("history", "empty", "market"):
        for entry in chat_corrections(group).values():
            assert entry["before"] != entry["after"]
