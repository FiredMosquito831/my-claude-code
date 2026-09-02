"""Usage helpers for OpenAI Responses payloads.

The estimator moved to ``core.openai_common.usage`` when Chat Completions
needed the identical calculation for its own ``reasoning_tokens`` field. It
stays importable from here because that is where this package's streaming
ledger has always read it from.
"""

from my_claude_code.core.openai_common import estimate_text_tokens

__all__ = ["estimate_text_tokens"]
