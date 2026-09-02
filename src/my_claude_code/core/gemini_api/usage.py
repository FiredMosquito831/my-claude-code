"""Anthropic usage translated into Gemini's ``usageMetadata``.

The two protocols count the same tokens and name them differently, and one of
the differences is not cosmetic -- the same one that bites the Chat Completions
adapter. Anthropic's ``input_tokens`` *excludes* what the prompt cache served,
while Gemini's ``promptTokenCount`` **includes** it and then says how much of
it was cached in ``cachedContentTokenCount``. A straight rename would
under-report the prompt of every cached request, which is most of them for a
coding agent, and Gemini CLI renders that number as the session's context
gauge.

``thoughtsTokenCount`` is Gemini's field for thinking tokens; Anthropic reports
one ``output_tokens`` for the whole turn and never says how many of them were
thinking, so the same bounded estimate the OpenAI surfaces use is reused here
and capped by the real output count at the point of use.
"""

from collections.abc import Mapping
from typing import Any

from my_claude_code.core.openai_common import estimate_text_tokens


class GeminiUsageLedger:
    """Accumulate the token counts an Anthropic stream reports."""

    def __init__(self) -> None:
        self._input_tokens: int | None = None
        self._output_tokens: int | None = None
        self._cache_read_tokens = 0
        self._cache_write_tokens = 0
        self._thoughts_estimate = 0

    def record_message_start(self, data: Mapping[str, Any]) -> None:
        """Absorb the counts reported up front by ``message_start``."""

        message = data.get("message")
        if isinstance(message, Mapping):
            self.absorb(message.get("usage"))

    def record_usage_delta(self, data: Mapping[str, Any]) -> None:
        self.absorb(data.get("usage"))

    def absorb(self, usage: object) -> None:
        if not isinstance(usage, Mapping):
            return
        input_tokens = usage.get("input_tokens")
        # A later zero must not erase a positive count seeded by
        # ``message_start``: it means the provider did not recount, not that
        # nobody sent anything.
        if isinstance(input_tokens, int) and (
            self._input_tokens is None or input_tokens > 0
        ):
            self._input_tokens = input_tokens
        output_tokens = usage.get("output_tokens")
        if isinstance(output_tokens, int):
            self._output_tokens = output_tokens
        cache_read = usage.get("cache_read_input_tokens")
        if isinstance(cache_read, int) and cache_read > 0:
            self._cache_read_tokens = cache_read
        cache_write = usage.get("cache_creation_input_tokens")
        if isinstance(cache_write, int) and cache_write > 0:
            self._cache_write_tokens = cache_write

    def add_thought_text(self, text: str) -> None:
        self._thoughts_estimate += estimate_text_tokens(text)

    def has_counts(self) -> bool:
        return self._input_tokens is not None or self._output_tokens is not None

    def payload(self) -> dict[str, Any]:
        """Return the Gemini ``usageMetadata`` object for what was counted."""

        prompt_tokens = (
            (self._input_tokens or 0)
            + self._cache_read_tokens
            + self._cache_write_tokens
        )
        candidates_tokens = self._output_tokens or 0
        usage: dict[str, Any] = {
            "promptTokenCount": prompt_tokens,
            "candidatesTokenCount": candidates_tokens,
            "totalTokenCount": prompt_tokens + candidates_tokens,
        }
        if self._cache_read_tokens:
            usage["cachedContentTokenCount"] = self._cache_read_tokens
        # Capped by the real output count: an estimate must never make a
        # response look like it produced more than the provider billed.
        thoughts = min(self._thoughts_estimate, candidates_tokens)
        if thoughts:
            usage["thoughtsTokenCount"] = thoughts
        return usage
