"""The 7.106.0 rows of the declared lifetime table, as data.

* OpenRouter's Claude upstreams take Anthropic's two lifetimes, found by the
  author segment of OpenRouter's own ``<author>/<slug>`` ids (aliases too);
  every other upstream on OpenRouter caches automatically;
* Azure OpenAI takes OpenAI's retention words, ``in_memory`` and ``24h``,
  adapted by the same rule (the shortest at least the minutes asked, else the
  longest);
* the hosts with evidence of automatic caching are declared automatic --
  ChatGPT and its ``openai`` alias among them -- and take no lifetime;
* a host nothing is known about stays unknown (``None``), never automatic;
* the Anthropic declarations read exactly as 7.105.0's.
"""

import pytest

from my_claude_code.application.prompt_cache import adapt_prompt_cache_minutes
from my_claude_code.config.prompt_cache_lifetimes import (
    ANTHROPIC_MESSAGES_GATEWAY_LIFETIMES,
    ANTHROPIC_MESSAGES_LIFETIMES,
    AUTOMATIC_PROMPT_CACHE,
    OPENAI_RETENTION_LIFETIMES,
    OPENROUTER_ANTHROPIC_LIFETIMES,
    PLACEMENT_BODY_FIELD,
    PLACEMENT_CLIENT_MARKERS,
    PLACEMENT_NONE,
    PLACEMENT_TOP_LEVEL_MARKER,
    PROMPT_CACHE_LIFETIME_FAMILIES,
    chat_prompt_cache_family_id,
    upstream_author,
)

OPENROUTER = PROMPT_CACHE_LIFETIME_FAMILIES[OPENROUTER_ANTHROPIC_LIFETIMES]
RETENTION = PROMPT_CACHE_LIFETIME_FAMILIES[OPENAI_RETENTION_LIFETIMES]
AUTOMATIC = PROMPT_CACHE_LIFETIME_FAMILIES[AUTOMATIC_PROMPT_CACHE]


@pytest.mark.parametrize(
    ("model_id", "author"),
    [
        ("anthropic/claude-haiku-5.5", "anthropic"),
        ("~anthropic/claude-sonnet-latest", "anthropic"),
        ("Anthropic/claude-opus-5.5", "anthropic"),
        ("moonshotai/kimi-k2.6:free", "moonshotai"),
        ("claude-haiku-5-5", ""),
        ("anthropic/", ""),
        ("", ""),
    ],
)
def test_the_upstream_is_the_author_segment(model_id: str, author: str) -> None:
    assert upstream_author(model_id) == author


@pytest.mark.parametrize(
    ("provider_id", "model_id", "family_id"),
    [
        ("open_router", "anthropic/claude-haiku-5.5", OPENROUTER_ANTHROPIC_LIFETIMES),
        (
            "open_router",
            "~anthropic/claude-opus-latest",
            OPENROUTER_ANTHROPIC_LIFETIMES,
        ),
        ("open_router", "stealth/space-bunny-alpha", AUTOMATIC_PROMPT_CACHE),
        ("open_router", "google/gemini-3.6-flash", AUTOMATIC_PROMPT_CACHE),
        ("azure_openai", "anthropic/claude-haiku-5.5", OPENAI_RETENTION_LIFETIMES),
        ("azure_openai", "my-gpt-5-4", OPENAI_RETENTION_LIFETIMES),
        ("chatgpt_oauth", "gpt-6", AUTOMATIC_PROMPT_CACHE),
        ("openai", "gpt-6", AUTOMATIC_PROMPT_CACHE),
        ("gemini", "gemini-3.6-flash", AUTOMATIC_PROMPT_CACHE),
        ("vertex", "gemini-3.6-flash", AUTOMATIC_PROMPT_CACHE),
        ("deepseek", "deepseek-v4", AUTOMATIC_PROMPT_CACHE),
        ("nous_portal", "anthropic/claude-haiku-5.5", AUTOMATIC_PROMPT_CACHE),
        ("nvidia_nim", "z-ai/glm-5", AUTOMATIC_PROMPT_CACHE),
        ("opencode", "kimi-k2.6", AUTOMATIC_PROMPT_CACHE),
        ("opencode_go", "kimi-k2.6", AUTOMATIC_PROMPT_CACHE),
        ("agnes", "agnes-3.0-flash", AUTOMATIC_PROMPT_CACHE),
        # Nothing is known: unknown, never "automatic".
        ("kilo", "anthropic/claude-haiku-5.5", None),
        ("groq", "llama-5", None),
        ("commandcode", "deepseek/deepseek-v4-flash", None),
        ("custom_agnes", "agnes-3.0-flash", None),
        # The Anthropic hosts are not OpenAI-shaped; their own table decides.
        ("anthropic", "claude-haiku-5-5", None),
        ("anthropic_oauth", "claude-haiku-5-5", None),
        ("", "anthropic/claude-haiku-5.5", None),
    ],
)
def test_each_host_is_joined_to_its_family(
    provider_id: str, model_id: str, family_id: str | None
) -> None:
    assert chat_prompt_cache_family_id(provider_id, model_id) == family_id


def test_openrouter_claude_takes_anthropics_two_lifetimes_as_one_top_level_marker() -> (
    None
):
    assert [
        (life.minutes, life.wire, life.write_price) for life in OPENROUTER.lifetimes
    ] == [
        (5, "5m", "1.25x"),
        (60, "1h", "2x"),
    ]
    assert OPENROUTER.placement == PLACEMENT_TOP_LEVEL_MARKER
    assert OPENROUTER.automatic_marker is True
    assert OPENROUTER.lifetimes_known is True
    assert OPENROUTER.rows_only is False
    assert OPENROUTER.refusal_per_host is False
    assert OPENROUTER.wire_field == "cache_control.ttl"


@pytest.mark.parametrize(
    ("minutes", "wire"),
    [(1, "in_memory"), (5, "in_memory"), (6, "24h"), (30, "24h"), (60, "24h")],
)
def test_the_retention_word_is_the_shortest_at_least_the_minutes(
    minutes: int, wire: str
) -> None:
    sent = adapt_prompt_cache_minutes(minutes, RETENTION.lifetimes)
    assert sent is not None and sent.wire == wire


def test_azure_writes_one_body_field_and_learns_per_deployment() -> None:
    assert RETENTION.wire_field == "prompt_cache_retention"
    assert RETENTION.placement == PLACEMENT_BODY_FIELD
    assert RETENTION.automatic_marker is False
    assert RETENTION.rows_only is False
    assert RETENTION.refusal_per_host is False
    assert not RETENTION.automatic


def test_an_automatic_host_takes_no_lifetime() -> None:
    assert AUTOMATIC.lifetimes == ()
    assert AUTOMATIC.automatic
    assert AUTOMATIC.placement == PLACEMENT_NONE


def test_the_anthropic_families_keep_their_placement() -> None:
    for family_id in (
        ANTHROPIC_MESSAGES_LIFETIMES,
        ANTHROPIC_MESSAGES_GATEWAY_LIFETIMES,
    ):
        family = PROMPT_CACHE_LIFETIME_FAMILIES[family_id]
        assert family.placement == PLACEMENT_CLIENT_MARKERS
        assert not family.automatic
