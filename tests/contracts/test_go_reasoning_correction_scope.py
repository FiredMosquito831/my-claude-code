"""Guard the finite historical exceptions independently of profile execution."""

from tests.contracts.go_reasoning_expectations import corrections


def test_go_correction_has_exact_history_and_empty_system_cells() -> None:
    fixtures = {
        "string_turn_with_reasoning_content",
        "thinking_after_tool_call",
        "thinking_only_turn",
        "thinking_then_text",
        "thinking_tool_loop",
        "thinking_with_images",
    }
    assert set(corrections("history")) == {
        f"openai_chat/opencode_go/{fixture}/{policy}"
        for fixture in fixtures
        for policy in ("off", "on_high")
    }
    assert set(corrections("empty")) == {"fold_every_block"}
    assert len(corrections("market")) == 264
    for group in ("metadata", "metadata_off", "metadata_on"):
        assert set(corrections(group)) == {"page", "routing/opencode_go/minimax-m3"}
        entry = corrections(group)["routing/opencode_go/minimax-m3"]
        moved = {
            key
            for key in entry["before"]
            if entry["before"][key] != entry["after"][key]
        }
        assert moved == {"reasoning_dialect"}
    for group in ("history", "empty", "market"):
        for entry in corrections(group).values():
            assert entry["before"] != entry["after"]
