"""Tests for api/optimization_handlers.py."""

from unittest.mock import patch

from my_claude_code.api.optimization_handlers import (
    OPTIMIZATION_HANDLERS,
    OPTIMIZATION_RULES,
    try_optimizations,
    try_probe_auto_response,
    try_suggestion_skip,
    try_title_skip,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import (
    ContentBlockText,
    Message,
    MessagesRequest,
)
from my_claude_code.core.anthropic.tokens import get_token_count


def _make_request(
    messages_content: str, max_tokens: int | None = None
) -> MessagesRequest:
    """Create a MessagesRequest with a single user message."""
    return MessagesRequest(
        model="claude-3-sonnet",
        max_tokens=max_tokens if max_tokens is not None else 100,
        messages=[Message(role="user", content=messages_content)],
    )


class TestTryTitleSkip:
    def test_disabled_returns_none(self):
        settings = Settings()
        settings.enable_title_generation_skip = False
        req = _make_request("write a 5-10 word title")
        with patch(
            "my_claude_code.api.optimization_handlers.is_title_generation_request",
            return_value=True,
        ):
            assert try_title_skip(req, settings, get_token_count) is None

    def test_enabled_and_match_returns_response(self):
        settings = Settings()
        settings.enable_title_generation_skip = True
        req = _make_request("x")
        with patch(
            "my_claude_code.api.optimization_handlers.is_title_generation_request",
            return_value=True,
        ):
            result = try_title_skip(req, settings, get_token_count)
        assert result is not None
        block = result.response.content[0]
        assert isinstance(block, ContentBlockText)
        assert block.text == "Conversation"


class TestTryProbeAutoResponse:
    def _probe(self) -> MessagesRequest:
        return MessagesRequest(
            model="test-model",
            max_tokens=16,
            messages=[Message(role="user", content="Say OK")],
        )

    def test_disabled_returns_none(self):
        settings = Settings()
        settings.enable_probe_auto_response = False
        assert try_probe_auto_response(self._probe(), settings, get_token_count) is None

    def test_enabled_probe_is_answered_locally(self):
        settings = Settings()
        result = try_probe_auto_response(self._probe(), settings, get_token_count)
        assert result is not None
        assert result.rule == "probe_auto_response"
        block = result.response.content[0]
        assert isinstance(block, ContentBlockText)
        assert block.text == "OK"

    def test_reply_keeps_the_requested_model_id_at_this_layer(self):
        """The resolved id is stamped later; here the request's own id stands."""
        settings = Settings()
        result = try_probe_auto_response(self._probe(), settings, get_token_count)
        assert result is not None
        assert result.response.model == "test-model"

    def test_non_probe_request_returns_none(self):
        settings = Settings()
        request = MessagesRequest(
            model="test-model",
            max_tokens=100,
            messages=[Message(role="user", content="Say OK and then explain why")],
        )
        assert try_probe_auto_response(request, settings, get_token_count) is None

    def test_input_tokens_track_the_probe_size(self):
        settings = Settings()
        result = try_probe_auto_response(self._probe(), settings, get_token_count)
        assert result is not None
        assert result.tokens_saved == result.response.usage.input_tokens
        assert result.tokens_saved > 0


class TestTrySuggestionSkip:
    def test_disabled_returns_none(self):
        settings = Settings()
        settings.enable_suggestion_mode_skip = False
        req = _make_request("[SUGGESTION MODE: x]")
        with patch(
            "my_claude_code.api.optimization_handlers.is_suggestion_mode_request",
            return_value=True,
        ):
            assert try_suggestion_skip(req, settings, get_token_count) is None

    def test_enabled_and_match_returns_response(self):
        settings = Settings()
        settings.enable_suggestion_mode_skip = True
        req = _make_request("x")
        with patch(
            "my_claude_code.api.optimization_handlers.is_suggestion_mode_request",
            return_value=True,
        ):
            result = try_suggestion_skip(req, settings, get_token_count)
        assert result is not None
        block = result.response.content[0]
        assert isinstance(block, ContentBlockText)
        assert block.text == ""


class TestTryOptimizations:
    def test_first_match_wins(self):
        """Title skip is first in OPTIMIZATION_HANDLERS; it should win."""
        settings = Settings()
        settings.enable_title_generation_skip = True
        settings.enable_suggestion_mode_skip = True
        req = _make_request("[SUGGESTION MODE: on]")
        with patch(
            "my_claude_code.api.optimization_handlers.is_title_generation_request",
            return_value=True,
        ):
            result = try_optimizations(req, settings, get_token_count)
        assert result is not None
        assert result.rule == "title_generation_skip"
        block = result.response.content[0]
        assert isinstance(block, ContentBlockText)
        assert block.text == "Conversation"

    def test_no_match_returns_none(self):
        settings = Settings()
        settings.enable_title_generation_skip = False
        settings.enable_suggestion_mode_skip = False
        req = _make_request("random user message")
        assert try_optimizations(req, settings, get_token_count) is None


class TestUsageIsCountedNotInvented:
    """The reported usage used to be hardcoded regardless of the request."""

    def test_input_tokens_track_the_real_prompt_size(self):
        settings = Settings()
        settings.enable_title_generation_skip = True
        small = _make_request("hi")
        large = _make_request("word " * 4000)
        with patch(
            "my_claude_code.api.optimization_handlers.is_title_generation_request",
            return_value=True,
        ):
            small_result = try_title_skip(small, settings, get_token_count)
            large_result = try_title_skip(large, settings, get_token_count)

        assert small_result is not None and large_result is not None
        # The old implementation reported 100 for both of these.
        assert small_result.response.usage.input_tokens < 20
        assert large_result.response.usage.input_tokens > 3000
        assert (
            large_result.response.usage.input_tokens
            != small_result.response.usage.input_tokens
        )

    def test_tokens_saved_equals_the_prompt_that_never_went_upstream(self):
        settings = Settings()
        settings.enable_title_generation_skip = True
        req = _make_request("word " * 500)
        with patch(
            "my_claude_code.api.optimization_handlers.is_title_generation_request",
            return_value=True,
        ):
            result = try_title_skip(req, settings, get_token_count)
        assert result is not None
        assert result.tokens_saved == result.response.usage.input_tokens
        assert result.tokens_saved == get_token_count(req.messages, None, None)

    def test_output_tokens_track_the_reply_actually_returned(self):
        settings = Settings()
        settings.enable_suggestion_mode_skip = True
        settings.enable_title_generation_skip = True
        req = _make_request("x")
        with patch(
            "my_claude_code.api.optimization_handlers.is_suggestion_mode_request",
            return_value=True,
        ):
            empty = try_suggestion_skip(req, settings, get_token_count)
        with patch(
            "my_claude_code.api.optimization_handlers.is_title_generation_request",
            return_value=True,
        ):
            titled = try_title_skip(req, settings, get_token_count)
        assert empty is not None and titled is not None
        # "" against "Conversation": the old code reported 1 and 5 by fiat.
        assert empty.response.usage.output_tokens == 0
        assert titled.response.usage.output_tokens > 0


class TestRuleNames:
    def test_every_handler_reports_a_name_from_the_published_tuple(self):
        settings = Settings()
        settings.enable_title_generation_skip = True
        req = _make_request("x")
        with patch(
            "my_claude_code.api.optimization_handlers.is_title_generation_request",
            return_value=True,
        ):
            result = try_title_skip(req, settings, get_token_count)
        assert result is not None
        assert result.rule == "title_generation_skip"
        assert result.rule in OPTIMIZATION_RULES

    def test_published_tuple_covers_every_registered_handler(self):
        # A rule the tuple does not know about is a rule the dashboard cannot
        # name, which is how these went uncounted for their whole life.
        assert len(OPTIMIZATION_RULES) == len(OPTIMIZATION_HANDLERS)
