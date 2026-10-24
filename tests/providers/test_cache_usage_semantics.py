"""Cache token accounting across the two protocols.

An OpenAI-family ``prompt_tokens`` *includes* the tokens served from cache;
Anthropic's ``input_tokens`` excludes them and expects the caller to add
``cache_read_input_tokens`` back for the total. Emitting the first number under
the second name double-counts every cache hit.
"""

from typing import Any

import pytest

from my_claude_code.providers.deepseek.client import DeepSeekProvider
from my_claude_code.providers.openai_chat import GENERIC_OPENAI_PROFILE
from my_claude_code.providers.openai_chat.provider import OpenAIChatProvider


class _Usage:
    """Stand-in for the OpenAI SDK usage object."""

    def __init__(self, **fields: Any) -> None:
        for key, value in fields.items():
            setattr(self, key, value)


class _Details:
    def __init__(self, **fields: Any) -> None:
        for key, value in fields.items():
            setattr(self, key, value)


def _fields(provider_class: type, usage: Any) -> dict[str, int]:
    """Call the mapping on a bare instance; it reads usage, not provider state.

    Since 7.109.0 it reads one declaration off the profile -- whether the host
    is a price marketplace -- so the bare instance carries the generic profile,
    which declares none, exactly as these providers do.
    """
    provider = object.__new__(provider_class)
    vars(provider)["_profile"] = GENERIC_OPENAI_PROFILE
    return provider._anthropic_usage_fields(usage)


class TestOpenAiFamilyCacheFields:
    def test_cache_read_is_reported(self) -> None:
        usage = _Usage(prompt_tokens_details=_Details(cached_tokens=261120))
        fields = _fields(OpenAIChatProvider, usage)
        assert fields["cache_read_input_tokens"] == 261120

    def test_cache_write_is_reported(self) -> None:
        """OpenRouter reports the write side under prompt_tokens_details."""
        usage = _Usage(
            prompt_tokens_details=_Details(cached_tokens=0, cache_write_tokens=4096)
        )
        fields = _fields(OpenAIChatProvider, usage)
        assert fields["cache_creation_input_tokens"] == 4096

    def test_zero_cached_is_kept_distinct_from_silence(self) -> None:
        """0 means "no hit"; absent means "the provider never said"."""
        reported = _fields(
            OpenAIChatProvider, _Usage(prompt_tokens_details=_Details(cached_tokens=0))
        )
        assert reported["cache_read_input_tokens"] == 0

        silent = _fields(OpenAIChatProvider, _Usage(prompt_tokens_details=None))
        assert "cache_read_input_tokens" not in silent

    def test_no_usage_details_reports_nothing(self) -> None:
        assert _fields(OpenAIChatProvider, None) == {}


class TestDeepSeekCacheFields:
    def test_only_the_hit_count_is_mapped(self) -> None:
        """Misses become input_tokens via subtraction, not a second field."""
        usage = _Usage(prompt_cache_hit_tokens=1920, prompt_cache_miss_tokens=128)
        fields = _fields(DeepSeekProvider, usage)
        assert fields == {"cache_read_input_tokens": 1920}
        assert "cache_creation_input_tokens" not in fields

    def test_silence_reports_nothing(self) -> None:
        assert _fields(DeepSeekProvider, _Usage()) == {}


def _anthropic_input(prompt_tokens: int | None, fields: dict[str, int]) -> int | None:
    """Mirror of the subtraction the streaming path performs."""
    cache_read = fields.get("cache_read_input_tokens")
    if prompt_tokens is not None and isinstance(cache_read, int):
        return max(0, prompt_tokens - cache_read)
    return prompt_tokens


class TestInputTokenSubtraction:
    def test_warm_prompt_is_not_double_counted(self) -> None:
        """A real row from the request log: 268k prompt, 261k of it cached."""
        fields = {"cache_read_input_tokens": 261120}
        uncached = _anthropic_input(267968, fields)
        assert uncached == 6848
        # Anthropic total = input + cache_read, which must equal prompt_tokens
        # rather than the 529,088 the old mapping implied.
        assert uncached + fields["cache_read_input_tokens"] == 267968

    def test_deepseek_subtraction_lands_on_the_miss_count(self) -> None:
        usage = _Usage(prompt_cache_hit_tokens=1920, prompt_cache_miss_tokens=128)
        fields = _fields(DeepSeekProvider, usage)
        assert _anthropic_input(2048, fields) == 128

    def test_cold_prompt_is_unchanged(self) -> None:
        assert _anthropic_input(4096, {"cache_read_input_tokens": 0}) == 4096

    def test_unreported_cache_leaves_input_alone(self) -> None:
        assert _anthropic_input(4096, {}) == 4096

    def test_subtraction_never_goes_negative(self) -> None:
        """Defensive: a provider disagreeing with itself must not underflow."""
        assert _anthropic_input(100, {"cache_read_input_tokens": 500}) == 0


@pytest.mark.parametrize(
    ("prompt_tokens", "cached"),
    [(267968, 261120), (42709, 0), (2048, 1920)],
)
def test_total_input_always_reconstructs_prompt_tokens(
    prompt_tokens: int, cached: int
) -> None:
    fields = {"cache_read_input_tokens": cached}
    uncached = _anthropic_input(prompt_tokens, fields)
    assert uncached is not None
    assert uncached + cached == prompt_tokens
