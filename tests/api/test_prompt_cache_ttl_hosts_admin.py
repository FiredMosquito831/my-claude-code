"""The Models page table gains the 7.106.0 hosts, appended (E4: additive only)."""

from my_claude_code.api.model_admin import (
    PROMPT_CACHE_AUTOMATIC_LABEL,
    prompt_cache_lifetimes_payload,
)

#: The table's keys as 7.105.0 served them, in its order.
_7_105_0_KEYS = [
    "families",
    "providers",
    "refused_fact_kind",
    "note",
    "unknown_label",
    "unknown_note",
    "override_key",
    "control",
    "models",
    "gateway_label",
    "gateway_note",
    "settings",
    "routes",
    "route_note",
    "global_setting",
]


def test_the_new_keys_come_after_every_7_105_0_key() -> None:
    table = prompt_cache_lifetimes_payload()

    assert list(table)[: len(_7_105_0_KEYS)] == _7_105_0_KEYS
    assert list(table)[len(_7_105_0_KEYS) :] == [
        "chat_providers",
        "upstreams",
        "upstream_alias_prefix",
        "automatic_providers",
        "automatic_family",
        "automatic_label",
        "automatic_note",
    ]
    # The Anthropic declarations are exactly what 7.105.0 served.
    assert table["providers"] == {
        "anthropic": "anthropic_messages",
        "anthropic_oauth": "anthropic_messages",
    }


def test_the_new_hosts_are_served_as_data() -> None:
    table = prompt_cache_lifetimes_payload()

    assert table["chat_providers"] == {"azure_openai": "openai_retention"}
    assert table["upstreams"] == {"open_router": {"anthropic": "openrouter_anthropic"}}
    assert table["upstream_alias_prefix"] == "~"
    assert table["automatic_family"] == "automatic"
    assert table["automatic_label"] == PROMPT_CACHE_AUTOMATIC_LABEL
    assert "chatgpt_oauth" in table["automatic_providers"]
    assert "openai" in table["automatic_providers"]
    assert table["automatic_providers"] == sorted(table["automatic_providers"])
    families = table["families"]
    assert families["openrouter_anthropic"]["placement"] == "top_level_marker"
    assert families["openai_retention"]["placement"] == "body_field"
    assert [life["wire"] for life in families["openai_retention"]["lifetimes"]] == [
        "in_memory",
        "24h",
    ]
    assert families["automatic"]["automatic"] is True
    assert families["automatic"]["lifetimes"] == []
    # The 7.105.0 families gain only the two appended keys.
    anthropic = families["anthropic_messages"]
    assert list(anthropic)[-2:] == ["placement", "automatic"]
    assert anthropic["placement"] == "client_markers"
    assert anthropic["automatic"] is False
