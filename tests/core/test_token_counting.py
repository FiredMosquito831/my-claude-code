"""Direct coverage for ``count_text_tokens``.

It was published in 5.42.0 so the local-optimization handlers could report an
honest reply length, and until now its behaviour was only asserted indirectly
through those handlers. The two properties below are the reasons it exists at
all, and neither is visible from a handler test.

Since 8.19.0 it also holds the ``redacted_thinking`` branch's contract: the
class at the bottom is the only place the estimator's treatment of an opaque
carrier is pinned.
"""

import base64
import io
import random

import pytest
import tiktoken
from loguru import logger

from my_claude_code.core.anthropic.models import Message
from my_claude_code.core.anthropic.tokens import count_text_tokens, get_token_count


class TestNoPerMessageFraming:
    """The reason this is not just ``get_token_count`` with one message.

    ``get_token_count`` bills a fixed overhead per message, which is correct
    for a request and wrong for a lone reply: it scores the empty string at 4.
    A handler that answers a request locally reports this number as
    ``usage.output_tokens``, so the overhead would be invented tokens.
    """

    def test_the_empty_string_costs_nothing(self):
        assert count_text_tokens("") == 0

    def test_get_token_count_does_charge_framing_for_the_same_text(self):
        # Pins the discrepancy that motivates the separate function, so nobody
        # "simplifies" one into the other.
        framed = get_token_count([Message(role="assistant", content="")], None, None)
        assert framed > count_text_tokens("")

    def test_a_reply_is_counted_as_its_own_text_only(self):
        text = "Conversation"
        expected = tiktoken.get_encoding("cl100k_base").encode(text)
        assert count_text_tokens(text) == len(expected)


class TestSpecialTokensDoNotRaise:
    """``_DISALLOWED_SPECIAL = ()`` is load-bearing, not decoration.

    tiktoken's default raises ValueError when the text contains a special
    token literal. Prompts are arbitrary user text and Claude Code transcripts
    routinely quote such strings, so the default would turn a countable
    request into a crash on the request path.
    """

    @pytest.mark.parametrize(
        "text",
        ["<|endoftext|>", "prefix <|endoftext|> suffix", "<|fim_prefix|>"],
    )
    def test_special_token_literals_are_counted_not_rejected(self, text):
        assert count_text_tokens(text) > 0


class TestMonotonicity:
    def test_longer_text_never_costs_fewer_tokens(self):
        counts = [count_text_tokens("word " * n) for n in (0, 1, 10, 100)]
        assert counts == sorted(counts)
        assert counts[0] == 0
        assert counts[-1] > counts[1]

    def test_unicode_is_counted_without_error(self):
        assert count_text_tokens("résumé 日本語 🎉") > 0


def _carrier(chars: int, seed: int = 0) -> str:
    """A base64url blob of ``chars`` characters, shaped like a real carrier.

    High-entropy bytes, from a seeded PRNG so the test is deterministic. A run
    of one repeated character would be compressed by cl100k to about 0.13
    tokens/char and would measure nothing about a real carrier.
    """

    rng = random.Random(seed)
    raw = bytes(rng.getrandbits(8) for _ in range((chars * 3) // 4 + 3))
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")[:chars]


def _count_with_log(block: object) -> tuple[int, str]:
    """``get_token_count`` for a one-message request, and what it logged.

    ``block`` is handed to ``Message`` as a **plain dict** where the shape is
    one the model would refuse (an unknown ``type``, a ``redacted_thinking``
    with no ``data``), because ``get_token_count`` is documented to take a
    ``Message`` whose blocks may also be dicts -- that is how a block from a
    client's raw payload reaches it.
    """

    sink = io.StringIO()
    handler_id = logger.add(sink, level="DEBUG", format="{message}")
    try:
        message = Message.model_construct(role="assistant", content=[block])
        total = get_token_count([message], None, None)
    finally:
        logger.remove(handler_id)
    return total, sink.getvalue()


class TestRedactedThinkingIsCountedAsItsBlob:
    """8.19.0: the ``redacted_thinking`` branch beside the ``thinking`` one.

    Before it, a carrier fell to the ``else`` fallback, which counted
    ``json.dumps(block)`` -- the blob plus a ~10-token JSON wrapper -- and
    logged ``Unexpected block type`` at DEBUG on every call. Measured on a
    32,768-character carrier: 23,273 counted against 23,259 for the blob alone.
    """

    @pytest.mark.parametrize("chars", [133, 4096, 32768])
    def test_the_count_is_the_blob_plus_framing_only(self, chars: int) -> None:
        data = _carrier(chars, seed=chars)
        total, _log = _count_with_log({"type": "redacted_thinking", "data": data})
        # The message framing is 4 tokens and nothing else is in the request.
        assert total == 4 + count_text_tokens(data)

    @pytest.mark.parametrize("chars", [133, 4096, 32768])
    def test_the_json_wrapper_is_no_longer_counted(self, chars: int) -> None:
        """The old path was this same count plus ``json.dumps``'s wrapper.

        Asserting the *difference* rather than a magic number pins the defect
        itself: a regression that restored the fallback would add the wrapper
        back and this would be the assertion that says so.
        """

        data = _carrier(chars, seed=chars)
        total, _log = _count_with_log({"type": "redacted_thinking", "data": data})
        wrapper = count_text_tokens(
            '{"type": "redacted_thinking", "data": "' + data + '"}'
        ) - count_text_tokens(data)
        assert wrapper > 0
        assert total == 4 + count_text_tokens(data)
        assert total != 4 + count_text_tokens(data) + wrapper

    @pytest.mark.parametrize("chars", [133, 4096, 32768])
    def test_no_unexpected_block_type_record_is_logged(self, chars: int) -> None:
        data = _carrier(chars, seed=chars)
        _total, log = _count_with_log({"type": "redacted_thinking", "data": data})
        assert "Unexpected block type" not in log

    def test_an_empty_data_is_counted_as_nothing(self) -> None:
        """``data`` is a required field on the model, but a dict passed by a
        caller may omit it; the branch must not raise."""

        total, log = _count_with_log({"type": "redacted_thinking"})
        assert total == 4
        assert "Unexpected block type" not in log

    def test_a_thinking_block_of_the_same_length_is_unchanged(self) -> None:
        """The branch beside it must not have moved. Same characters, same
        count, and the same absence of a complaint."""

        text = _carrier(4096, seed=7)
        total, log = _count_with_log({"type": "thinking", "thinking": text})
        assert total == 4 + count_text_tokens(text)
        assert "Unexpected block type" not in log

    def test_a_block_type_with_no_branch_still_falls_back_and_complains(self) -> None:
        """The fallback itself is untouched: it is what every unknown block
        type still gets. Without this, the branch above could be made to pass
        by deleting the fallback's complaint for everything."""

        total, log = _count_with_log({"type": "some_future_block", "x": "y"})
        assert total > 4
        assert "Unexpected block type" in log
