"""Join two models' output into one Anthropic message.

Once a stream has shown the reader text, the model that wrote it can no longer
be swapped out -- the words are on screen and nothing can un-send them. Until
now that made every mid-stream failure terminal for the answer: the best
available ending was ``truncation``'s, which closes the blocks and stops.

This module is the ending after that one. The words already sent stay exactly
as they are; the *next* model on the route is asked to carry on from them, and
its stream is rewritten so the two read as one message rather than two. The
rewriting is entirely mechanical and every rule below traces to a line in
``stream_contracts``:

* a second ``message_start`` would overwrite the envelope the client already
  has (``sse_aggregation`` keeps the last one), so it is dropped;
* block indices are single-use for the whole message, so every index the
  continuation emits is offset above the highest one already seen;
* the one block that may be *continued* rather than reopened is the ``text``
  block that was open at the failure -- the continuation's first text block is
  mapped onto it, which is what keeps the join out of sight;
* thinking cannot cross the seam at all. ``ContentBlockThinking.signature`` is
  provider-cryptographic and a foreign model cannot produce a valid one, so a
  continuation's reasoning is dropped rather than attributed to the model that
  did not think it;
* exactly one ``message_delta``/``message_stop`` pair survives, emitted here,
  carrying the summed output tokens.

And one rule that is not mechanical, because it is the whole risk of the
feature: **a model that restarts the answer instead of continuing it must not
be spliced.** Measured across thirteen live model/host pairs, the dominant
failure of a continuation prompt was a verbatim restart -- the reader would
have seen the opening paragraph twice. So the continuation's first characters
are held back and inspected before any of them is forwarded, and a restart is
rejected outright: the message then ends on what the first model already said,
which is exactly ``truncation``'s outcome and never an error.

Pure: no I/O, no settings, no provider knowledge. The executor decides *when*
to resume; this decides only what the bytes look like afterwards.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..stream_contracts import SSEEvent, parse_sse_text
from ..tokens import count_text_tokens
from .emitter import format_sse_event
from .truncation import TRUNCATED_STOP_REASON, CommittedStreamTracker

#: How much of the already-sent text a continuation is compared against. The
#: whole prefix is the honest comparison but an unbounded one; a model that
#: repeats more than this before continuing is not a case anything measured
#: produced, and the cap keeps the check cheap on a long answer.
DEDUP_TAIL_CHARS = 2048

#: How many characters of the continuation are held before the restart verdict
#: is reached. Long enough that a repeated tail is caught in full, short enough
#: that the reader is not left staring at a frozen answer: this is pure latency
#: on the rescue path, paid once.
DECISION_BUFFER_CHARS = 512

#: The leading window compared to decide "this model started over". Verbatim
#: restarts -- the measured failure -- reproduce the opening exactly, so a
#: short window is enough and a longer one only risks missing a restart that
#: paraphrases its first line.
RESTART_WINDOW_CHARS = 24

#: Below this there is not enough of an opening to tell a restart from a
#: coincidence, and rejecting on four characters would abandon good
#: continuations.
RESTART_MIN_WINDOW_CHARS = 8

#: The shortest repetition worth stripping. Below it, "the" at the start of a
#: genuine continuation looks exactly like a repeated "the" at the end of the
#: prefix, and stripping it would delete real words.
MIN_OVERLAP_CHARS = 8

#: Block types a continuation may not carry across the seam.
_THINKING_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})


class SpliceState(StrEnum):
    """What the first model left open when it died."""

    IDLE = "idle"
    IN_TEXT = "in_text"
    IN_THINKING = "in_thinking"
    IN_TOOL_USE = "in_tool_use"


@dataclass(frozen=True, slots=True)
class SpliceFreeze:
    """Everything a continuation needs to know about the stream it joins.

    Taken once, at the moment the first model fails, from the frames that
    actually reached the client. Nothing here is derived from what the
    *provider* sent -- only from what was forwarded -- because the splice has
    to be consistent with the reader's screen, not with the upstream.
    """

    state: SpliceState
    #: Blocks the first model left open, in index order.
    open_indexes: tuple[int, ...]
    #: The open ``text`` block a continuation can be written straight into,
    #: which is what keeps the join out of sight. ``None`` when the failure did
    #: not land inside one.
    open_text_index: int | None
    #: First index no block in this message has used.
    next_index: int
    prefix_text: str
    prefix_thinking: str
    #: Visible characters the reader received, for the request log's sentence.
    chars: int
    input_tokens: int
    #: What the first model produced, reported or estimated. The continuation's
    #: own count is added to this so the client is billed for one message.
    output_tokens: int


def freeze_stream(tracker: CommittedStreamTracker) -> SpliceFreeze:
    """Read a :class:`SpliceFreeze` off the frames already forwarded."""
    open_blocks = tracker.open_blocks
    open_text_index: int | None = None
    state = SpliceState.IDLE
    # Priority, not iteration order: a stream is only ever inside one block,
    # but a provider that left two open must be judged by the one that
    # constrains the splice most.
    for index in sorted(open_blocks):
        kind = open_blocks[index]
        if kind == "tool_use":
            state = SpliceState.IN_TOOL_USE
            break
        if kind == "text" and open_text_index is None:
            open_text_index = index
            state = SpliceState.IN_TEXT
        elif kind in _THINKING_BLOCK_TYPES and state is SpliceState.IDLE:
            state = SpliceState.IN_THINKING
    return SpliceFreeze(
        state=state,
        open_indexes=tuple(sorted(open_blocks)),
        open_text_index=open_text_index,
        next_index=tracker.next_index,
        prefix_text=tracker.text_prefix,
        prefix_thinking=tracker.thinking_prefix,
        chars=tracker.chars,
        input_tokens=tracker.input_tokens,
        output_tokens=tracker.consumed_output_tokens,
    )


def close_frames(freeze: SpliceFreeze, *, keep_text_open: bool) -> tuple[str, ...]:
    """Close the blocks a continuation cannot be written into.

    The open ``text`` block is deliberately left open when the continuation is
    going to be mapped onto it: closing and reopening it would give the reader
    two adjacent text blocks and a visible join. Everything else closes -- a
    thinking block cannot be continued by a model that cannot sign it, and the
    contract validator rejects a message that ends with a block open.
    """
    keep = freeze.open_text_index if keep_text_open else None
    return tuple(
        format_sse_event(
            "content_block_stop", {"type": "content_block_stop", "index": index}
        )
        for index in freeze.open_indexes
        if index != keep
    )


def looks_like_a_restart(existing: str, candidate: str) -> bool:
    """Whether a continuation began the answer again instead of continuing it.

    Two shapes, both measured: the model reproduces the opening verbatim, or it
    jumps back to somewhere in the middle of what was already sent. Both are
    judged on a short leading window, and both are deliberately biased towards
    saying yes -- a false positive costs a truncated-but-valid message, a false
    negative puts the same paragraph on the reader's screen twice.
    """
    probe = candidate.lstrip()
    if not probe or not existing:
        return False
    window = min(RESTART_WINDOW_CHARS, len(existing), len(probe))
    if window < RESTART_MIN_WINDOW_CHARS:
        return False
    head = probe[:window]
    if existing[:window] == head:
        return True
    # The candidate's opening appears somewhere inside what was already sent.
    # That is only innocent when the match runs to the *end* of the prefix and
    # the candidate goes on to say the same thing -- an overlap, handled by
    # stripping rather than by rejecting. A match that sits in the middle, with
    # the prefix continuing differently afterwards, means the model jumped back
    # and is about to repeat a passage the reader already has.
    position = existing.find(head)
    while position != -1:
        if not probe.startswith(existing[position:]):
            return True
        position = existing.find(head, position + 1)
    return False


def strict_continuation_suffix(existing: str, candidate: str) -> str | None:
    """The part of ``candidate`` that carries the answer forward.

    ``""`` when the continuation added nothing, ``None`` when it restarted and
    must be thrown away, otherwise the text to forward -- with any repeated
    overlap removed.

    A stricter relative of ``recovery.continuation_suffix``, which is left
    alone because the same-provider recovery path depends on it. Its last
    clause returns the *whole* candidate whenever the candidate is short, which
    turns a short restart into a duplicated opening; that clause is exactly the
    hazard here, so the restart test runs first and unconditionally.
    """
    if not candidate:
        return ""
    if not existing:
        return candidate
    if looks_like_a_restart(existing, candidate):
        return None
    limit = min(len(existing), len(candidate))
    for size in range(limit, MIN_OVERLAP_CHARS - 1, -1):
        if existing.endswith(candidate[:size]):
            return candidate[size:]
    return candidate


class ContinuationSplicer:
    """Rewrite a second model's stream so it finishes the first model's message.

    Fed the continuation's raw SSE, chunk by chunk, exactly as the executor
    receives it; returns the frames that may go to the client, which is not the
    same set and never the same indices.

    Nothing is forwarded until the restart verdict is in. That is the one place
    this class buys correctness with latency, and it buys it on a path whose
    alternative is a half-written answer.
    """

    def __init__(
        self,
        freeze: SpliceFreeze,
        *,
        keep_text_open: bool,
        decision_buffer_chars: int = DECISION_BUFFER_CHARS,
    ) -> None:
        self._freeze = freeze
        self._dest_text_index = freeze.open_text_index if keep_text_open else None
        self._decision_buffer_chars = decision_buffer_chars
        self._next_index = freeze.next_index
        self._buffer = ""
        self._index_map: dict[int, int] = {}
        self._dropped_sources: set[int] = set()
        self._adopted_source: int | None = None
        # What ``close_frames`` left open on the client's side, and therefore
        # what ``finish`` still owes a ``content_block_stop``.
        self._open_dest: set[int] = set()
        if self._dest_text_index is not None:
            self._open_dest.add(self._dest_text_index)
        self._held: list[str] = []
        self._candidate = ""
        self._decided = False
        self._forwarded_chars = 0
        self._stop_reason: str | None = None
        self._output_tokens: int | None = None
        self._emitted_text: list[str] = []
        self.rejected = False
        self.dropped_overlap_chars = 0
        self.finished = False

    # -- observation -----------------------------------------------------

    def rewrite(self, chunk: str) -> list[str]:
        """The frames of one continuation chunk that may reach the client."""
        out: list[str] = []
        self._buffer += chunk
        while "\n\n" in self._buffer:
            frame, self._buffer = self._buffer.split("\n\n", 1)
            for event in parse_sse_text(frame):
                out.extend(self._rewrite_event(event))
        return out

    def _rewrite_event(self, event: SSEEvent) -> list[str]:
        if self.rejected:
            return []
        name = event.event or str(event.data.get("type", ""))
        if name == "message_start":
            return []
        if name == "content_block_start":
            return self._on_block_start(event.data)
        if name == "content_block_delta":
            return self._on_block_delta(event.data)
        if name == "content_block_stop":
            return self._on_block_stop(event.data)
        if name == "message_delta":
            self._on_message_delta(event.data)
            return []
        # ``message_stop`` is re-emitted by ``finish``; an ``error`` frame
        # belongs to a stream that failed, which the executor sees as an
        # exception; anything unrecognised carries an index this class cannot
        # map and is safer dropped than forwarded into a message it may break.
        return []

    def _on_block_start(self, data: dict[str, Any]) -> list[str]:
        index = data.get("index")
        block = data.get("content_block")
        if not isinstance(index, int) or not isinstance(block, dict):
            return []
        kind = str(block.get("type", ""))
        if kind in _THINKING_BLOCK_TYPES:
            # The message's reasoning belongs to the model that signed it.
            self._dropped_sources.add(index)
            return []
        eager = block.get("text")
        eager_text = eager if isinstance(eager, str) else ""
        if (
            kind == "text"
            and self._dest_text_index is not None
            and self._adopted_source is None
        ):
            # The seam: this block *is* the block already on the reader's
            # screen, so its opening frame is dropped and its text flows into
            # the index that is already open.
            self._adopted_source = index
            self._index_map[index] = self._dest_text_index
            if eager_text:
                return self._on_candidate_text(eager_text)
            return []
        out = self._decide_if_pending()
        if self.rejected:
            return []
        dest = self._allocate(index)
        rewritten = dict(data)
        rewritten["index"] = dest
        self._open_dest.add(dest)
        out.append(format_sse_event("content_block_start", rewritten))
        if kind == "text" and eager_text:
            self._emitted_text.append(eager_text)
            self._forwarded_chars += len(eager_text)
        return self._emit(out)

    def _on_block_delta(self, data: dict[str, Any]) -> list[str]:
        index = data.get("index")
        delta = data.get("delta")
        if not isinstance(index, int) or not isinstance(delta, dict):
            return []
        if index in self._dropped_sources:
            return []
        kind = delta.get("type")
        if kind in ("thinking_delta", "signature_delta"):
            return []
        if index == self._adopted_source and kind == "text_delta":
            return self._on_candidate_text(str(delta.get("text", "")))
        out = self._decide_if_pending()
        if self.rejected:
            return []
        dest = self._index_map.get(index)
        if dest is None:
            # A delta for a block whose start never arrived: unmappable.
            return self._emit(out)
        rewritten = dict(data)
        rewritten["index"] = dest
        if kind == "text_delta":
            text = str(delta.get("text", ""))
            self._emitted_text.append(text)
            self._forwarded_chars += len(text)
        out.append(format_sse_event("content_block_delta", rewritten))
        return self._emit(out)

    def _on_block_stop(self, data: dict[str, Any]) -> list[str]:
        index = data.get("index")
        if not isinstance(index, int) or index in self._dropped_sources:
            return []
        out = self._decide_if_pending()
        if self.rejected:
            return []
        dest = self._index_map.get(index)
        if dest is None:
            return self._emit(out)
        self._open_dest.discard(dest)
        out.append(
            format_sse_event(
                "content_block_stop", {"type": "content_block_stop", "index": dest}
            )
        )
        return self._emit(out)

    def _on_message_delta(self, data: dict[str, Any]) -> None:
        delta = data.get("delta")
        if isinstance(delta, dict) and delta.get("stop_reason"):
            self._stop_reason = str(delta["stop_reason"])
        usage = data.get("usage")
        if isinstance(usage, dict) and isinstance(usage.get("output_tokens"), int):
            self._output_tokens = usage["output_tokens"]

    # -- the restart verdict ---------------------------------------------

    def _on_candidate_text(self, text: str) -> list[str]:
        if not text:
            return []
        if self._decided:
            if self.rejected:
                return []
            self._emitted_text.append(text)
            self._forwarded_chars += len(text)
            return [self._text_delta(text)]
        self._candidate += text
        if len(self._candidate) < self._decision_threshold():
            return []
        return self._decide()

    def _decision_threshold(self) -> int:
        tail = min(len(self._freeze.prefix_text), DEDUP_TAIL_CHARS)
        return min(tail, self._decision_buffer_chars) or 1

    def _decide_if_pending(self) -> list[str]:
        """Force the verdict because something other than text has arrived."""
        if self._decided:
            return []
        return self._decide()

    def _decide(self) -> list[str]:
        self._decided = True
        existing = self._freeze.prefix_text[-DEDUP_TAIL_CHARS:]
        suffix = strict_continuation_suffix(existing, self._candidate)
        if suffix is None:
            # A restart. Nothing it has said may be shown, and nothing it says
            # later can be trusted either, so the stream is abandoned here and
            # the message ends on the first model's words.
            self.rejected = True
            self._held.clear()
            return []
        self.dropped_overlap_chars = len(self._candidate) - len(suffix)
        released: list[str] = []
        if suffix:
            self._emitted_text.append(suffix)
            self._forwarded_chars += len(suffix)
            released.append(self._text_delta(suffix))
        released.extend(self._held)
        self._held.clear()
        return released

    def _emit(self, frames: list[str]) -> list[str]:
        """Release frames, or hold them behind an undecided verdict."""
        if self.rejected:
            return []
        if self._decided:
            return frames
        self._held.extend(frames)
        return []

    def _text_delta(self, text: str) -> str:
        return format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": self._dest_text_index,
                "delta": {"type": "text_delta", "text": text},
            },
        )

    def _allocate(self, source_index: int) -> int:
        dest = self._index_map.get(source_index)
        if dest is None:
            dest = self._next_index
            self._next_index += 1
            self._index_map[source_index] = dest
        return dest

    # -- the ending ------------------------------------------------------

    @property
    def usable(self) -> bool:
        """Whether the continuation actually carried the answer forward."""
        return not self.rejected and self._forwarded_chars > 0

    @property
    def forwarded_chars(self) -> int:
        return self._forwarded_chars

    def finish(self) -> list[str]:
        """Close what is open and end the message, exactly once.

        The stop reason is the continuation's own when it produced an answer
        and finished; ``truncation``'s "cut short" reason when it did not,
        because a message ending in ``end_turn`` after a rescue that produced
        nothing would tell Claude Code the answer was complete -- which is the
        one lie this whole feature exists to avoid.
        """
        if self.finished:
            return []
        self.finished = True
        frames = self._decide_if_pending()
        stop_reason = (
            (self._stop_reason or "end_turn") if self.usable else TRUNCATED_STOP_REASON
        )
        frames.extend(
            format_sse_event(
                "content_block_stop", {"type": "content_block_stop", "index": index}
            )
            for index in sorted(self._open_dest)
        )
        self._open_dest.clear()
        frames.append(
            format_sse_event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {
                        "input_tokens": self._freeze.input_tokens,
                        "output_tokens": self._summed_output_tokens(),
                    },
                },
            )
        )
        frames.append(format_sse_event("message_stop", {"type": "message_stop"}))
        return frames

    def _summed_output_tokens(self) -> int:
        """One message, two models, one bill.

        The first model's consumption is already priced into the freeze --
        reported by its own ``message_delta`` where it sent one, estimated from
        what the reader saw where it did not. The continuation's is added on
        the same terms.
        """
        if self._output_tokens is not None:
            continued = self._output_tokens
        else:
            continued = count_text_tokens("".join(self._emitted_text))
        return self._freeze.output_tokens + continued

    def as_params(self, *, resumed_from_model: str) -> dict[str, object]:
        """The request-log payload for ``params.continuation``."""
        return {
            "resumed_from_model": resumed_from_model,
            "prefix_chars": self._freeze.chars,
            "continued_chars": self._forwarded_chars,
            "dropped_overlap_chars": self.dropped_overlap_chars,
            "accepted": self.usable,
        }
