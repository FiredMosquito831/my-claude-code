"""The measured generation window: one token-bearing clock, two minimums.

A rate is a ratio of two measurements, and both halves have to describe the
same thing. MCC's own holdback (``providers.stream_recovery``), its reasoning
rule (``FALLBACK_ON_REASONING_ONLY``), its tool-argument assembly and its
recovery flushes all delay or reshape what a *client* sees, so the first byte a
client receives is not the first token a model produced. Dividing the tokens a
host reported -- reasoning and tool arguments included -- by
``duration_ms - ttft_ms`` therefore mixes two bases, and on a real log it
printed 429,152 tok/s for a 68 s answer.

This module is the one place that defines the honest alternative:

* the window starts at the first delta of **any** kind -- reasoning, text or
  tool arguments, the same tokens the numerator counts -- and ends at the last
  one, both observed upstream before any MCC buffer;
* a window shorter than :data:`GEN_MIN_WINDOW_MS`, or a count below
  :data:`GEN_MIN_TOKENS`, is **not measured** rather than a number: ``12 tokens
  / 0.004 s`` is not information;
* a token count that was estimated rather than reported cannot back a rate at
  all, because an estimate over a measured clock is still a mixed basis.

The stamps are taken where each provider parses its own frames, which is the
same clock ``ttft_ms`` is derived from -- only before the buffer rather than
after it -- so nothing about the emitted stream changes.
"""

import time
from collections.abc import Callable

from my_claude_code.core.anthropic.stream_contracts import sse_is_scaffolding

#: The shortest window a rate may be computed over. Below this the divisor is
#: a scheduling artifact: 12 tokens over 4 ms is 3,000 tok/s no model had.
GEN_MIN_WINDOW_MS = 500

#: The fewest tokens a rate may be computed from. A handful of tokens is not a
#: rate, it is one chunk's latency.
GEN_MIN_TOKENS = 8


def measured_generation_rate(
    tokens: int | None,
    gen_first_ms: float | None,
    gen_last_ms: float | None,
) -> float | None:
    """Tokens per second over a measured generation window, or ``None``.

    ``None`` -- never a number and never zero -- whenever either stamp or the
    count is missing, the window is not positive, or either minimum fails.
    The same rule the aggregate applies in SQL (``latency_by_model``) and the
    dashboard applies in JavaScript; the constants are shared, and the two
    server-side spellings are held together by a test.
    """
    if tokens is None or gen_first_ms is None or gen_last_ms is None:
        return None
    window_ms = float(gen_last_ms) - float(gen_first_ms)
    if window_ms <= 0 or window_ms < GEN_MIN_WINDOW_MS:
        return None
    if tokens < GEN_MIN_TOKENS:
        return None
    return tokens * 1000.0 / window_ms


def throughput_rate(tokens: int | None, duration_ms: float | None) -> float | None:
    """Tokens per second over a whole request or attempt, or ``None``.

    The other honest rate: no window to measure, so none to fail, which is why
    every row ever stored can support it. ``None`` only when one of the two
    halves is missing or unusable.
    """
    if tokens is None or duration_ms is None:
        return None
    if tokens <= 0 or duration_ms <= 0:
        return None
    return tokens * 1000.0 / float(duration_ms)


class GenerationClock:
    """First and last token-bearing delta of one upstream stream, in order.

    Fed every frame the provider parses, before the holdback sees it. A frame
    is token-bearing when it is not scaffolding -- the same rule the commit
    boundary uses -- with reasoning counting as content, because reasoning
    tokens are in the numerator. The clock therefore cannot be distorted by a
    holdback release, a recovery flush or a buffered tool call: all three delay
    *delivery*, and none of them is where the model was generating.

    ``first_at`` and ``last_at`` are ``time.monotonic`` stamps, the same scale
    :class:`~my_claude_code.core.wire_capture.ResponseShape` counts from, so a
    reply that carried no token-bearing delta says so by leaving both ``None``
    rather than by reporting a zero-length window.
    """

    def __init__(self, *, now: Callable[[], float] | None = None) -> None:
        self._now = now or time.monotonic
        self._first: float | None = None
        self._last: float | None = None

    def observe(self, event: str) -> None:
        """Stamp ``event`` if it carries any part of the model's output."""
        if not event or sse_is_scaffolding(event):
            return
        self.stamp()

    def stamp(self) -> None:
        """Record one token-bearing delta at the current instant.

        For a door that reads its protocol's own fields rather than SSE text:
        a withheld reasoning delta never becomes an event a client or a
        holdback sees, but it is in the token count the rate divides.
        """
        now = self._now()
        if self._first is None:
            self._first = now
        self._last = now

    def reset(self) -> None:
        """Forget the window: the next delta starts a new one.

        Used when the attempt is retried from the top, because the token count
        that will be reported belongs to the stream that finishes, not to the
        one that was discarded.
        """
        self._first = None
        self._last = None

    @property
    def first_at(self) -> float | None:
        return self._first

    @property
    def last_at(self) -> float | None:
        return self._last

    @property
    def measured(self) -> bool:
        return self._first is not None and self._last is not None
