"""The one ``cl100k_base`` encoder, built the first time a token is counted.

``tiktoken.get_encoding("cl100k_base")`` costs roughly 0.4 s in a cold
interpreter -- the ``tiktoken`` import plus loading and compiling the BPE
table -- and three modules used to pay for it at *import* time
(``core.anthropic.tokens``, ``core.anthropic.streaming.ledger`` and
``core.openai_common.usage``). None of them counts a token before the server
answers its first request, so every start paid for an encoder that a
``/health`` probe never touches. They now share this one cached build, which
runs on the first real count instead.

The result is memoised including the failure: a missing or unbuildable encoder
is answered with ``None`` once, not retried on every token estimate.
"""

from typing import Any

_UNSET = object()
_encoder: Any = _UNSET


def cl100k_encoder() -> Any:
    """Return the shared ``cl100k_base`` encoder, or ``None`` if unavailable.

    Callers that can degrade (a token *estimate*) fall back to their own
    heuristic on ``None``; callers that cannot say so explicitly.
    """
    global _encoder
    if _encoder is _UNSET:
        try:
            import tiktoken

            _encoder = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _encoder = None
    return _encoder
