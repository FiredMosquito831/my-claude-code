"""Best-effort token estimation for OpenAI-compatible usage details.

Anthropic reports one ``output_tokens`` for a turn and never says how many of
them were thinking. Both OpenAI surfaces have a field for exactly that
(``output_tokens_details.reasoning_tokens`` on Responses,
``completion_tokens_details.reasoning_tokens`` on Chat Completions), and a
client that reads it to price a request is better served by a bounded estimate
of the reasoning text it actually received than by a zero.

The estimate is capped by the real ``output_tokens`` at the point of use, so it
can never make a response look like it produced more than the provider billed.
"""

from my_claude_code.core.token_encoder import cl100k_encoder

_DISALLOWED_SPECIAL: tuple[str, ...] = ()


def estimate_text_tokens(text: str) -> int:
    """Return a best-effort token estimate for OpenAI usage details."""
    if not text:
        return 0
    encoder = cl100k_encoder()
    if encoder is not None:
        return len(encoder.encode(text, disallowed_special=_DISALLOWED_SPECIAL))
    return max(1, len(text) // 4)
