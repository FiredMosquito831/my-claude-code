"""Edge case tests for api/detection.py."""

from typing import Literal

from my_claude_code.api.detection import (
    is_model_routing_probe_request,
    is_safety_classifier_request,
    is_suggestion_mode_request,
    is_title_generation_request,
)
from my_claude_code.core.anthropic.models import Message, MessagesRequest, Tool


def _make_request(
    content: str, *, inline_system: str | None = None, **kwargs
) -> MessagesRequest:
    messages = []
    if inline_system is not None:
        messages.append(Message(role="system", content=inline_system))
    messages.append(Message(role="user", content=content))
    return MessagesRequest(
        model="claude-3-sonnet",
        max_tokens=kwargs.pop("max_tokens", 100),
        messages=messages,
        **kwargs,
    )


def test_title_detection_reads_inline_system_context() -> None:
    request = _make_request(
        "Summarize this session",
        inline_system=(
            "Generate a concise, sentence-case title for this coding session. "
            'Return JSON with a single "title" field.'
        ),
    )

    assert is_title_generation_request(request) is True


class TestIsModelRoutingProbeRequest:
    def test_the_observed_probe_shape_matches(self):
        """The exact shape measured in the live log: one user turn, 16 tokens."""
        request = MessagesRequest(
            model="test-model",
            max_tokens=16,
            messages=[Message(role="user", content="Say OK")],
        )
        assert is_model_routing_probe_request(request) is True

    def test_period_after_the_prompt_still_matches(self):
        request = MessagesRequest(
            model="test-model",
            max_tokens=16,
            messages=[Message(role="user", content="Say OK.")],
        )
        assert is_model_routing_probe_request(request) is True

    def test_case_and_surrounding_whitespace_do_not_block(self):
        request = MessagesRequest(
            model="test-model",
            max_tokens=16,
            messages=[Message(role="user", content="  say OK ")],
        )
        assert is_model_routing_probe_request(request) is True

    def test_any_other_prompt_is_not_a_probe(self):
        request = MessagesRequest(
            model="test-model",
            max_tokens=16,
            messages=[Message(role="user", content="Say OK please")],
        )
        assert is_model_routing_probe_request(request) is False

    def test_a_real_conversation_never_matches(self):
        request = MessagesRequest(
            model="test-model",
            max_tokens=64000,
            messages=[
                Message(role="user", content="Say OK"),
                Message(role="assistant", content="OK"),
                Message(role="user", content="now do the real task"),
            ],
        )
        assert is_model_routing_probe_request(request) is False

    def test_missing_max_tokens_is_not_a_probe(self):
        request = MessagesRequest(
            model="test-model",
            max_tokens=None,
            messages=[Message(role="user", content="Say OK")],
        )
        assert is_model_routing_probe_request(request) is False

    def test_a_normal_output_budget_is_not_a_probe(self):
        request = MessagesRequest(
            model="test-model",
            max_tokens=100,
            messages=[Message(role="user", content="Say OK")],
        )
        assert is_model_routing_probe_request(request) is False

    def test_streaming_is_not_a_probe(self):
        request = MessagesRequest(
            model="test-model",
            max_tokens=16,
            stream=True,
            messages=[Message(role="user", content="Say OK")],
        )
        assert is_model_routing_probe_request(request) is False

    def test_system_text_is_not_a_probe(self):
        request = MessagesRequest(
            model="test-model",
            max_tokens=16,
            system="You are a helpful assistant.",
            messages=[Message(role="user", content="Say OK")],
        )
        assert is_model_routing_probe_request(request) is False

    def test_tools_are_not_a_probe(self):
        request = MessagesRequest(
            model="test-model",
            max_tokens=16,
            tools=[Tool(name="read", type="custom", input_schema={})],
            messages=[Message(role="user", content="Say OK")],
        )
        assert is_model_routing_probe_request(request) is False

    def test_inline_system_message_is_not_a_probe(self):
        request = MessagesRequest(
            model="test-model",
            max_tokens=16,
            messages=[
                Message(role="system", content="be brief"),
                Message(role="user", content="Say OK"),
            ],
        )
        assert is_model_routing_probe_request(request) is False

    def test_assistant_first_turn_is_not_a_probe(self):
        request = MessagesRequest(
            model="test-model",
            max_tokens=16,
            messages=[Message(role="assistant", content="Say OK")],
        )
        assert is_model_routing_probe_request(request) is False


class TestIsSafetyClassifierRequest:
    _SYSTEM = (
        "You are a security monitor. Respond with <block>yes</block> "
        "or <block>no</block>."
    )
    _USER = (
        "<transcript>\nUser: review the repo\n"
        "WebFetch https://example.com: fetch\n</transcript>\n<block> immediately."
    )

    def test_classifier_request_detected(self):
        req = _make_request(self._USER, system=self._SYSTEM)
        assert is_safety_classifier_request(req) is True

    def test_markers_split_across_system_and_user(self):
        req = _make_request(
            "<transcript>\nWebFetch x\n</transcript>", system=self._SYSTEM
        )
        assert is_safety_classifier_request(req) is True

    def test_request_with_tools_is_not_classifier(self):
        req = _make_request(self._USER, system=self._SYSTEM, tools=[{"name": "search"}])
        assert is_safety_classifier_request(req) is False

    def test_missing_transcript_marker(self):
        req = _make_request("<block> immediately", system=self._SYSTEM)
        assert is_safety_classifier_request(req) is False

    def test_missing_verdict_instruction(self):
        req = _make_request(
            "<transcript>\nWebFetch x\n</transcript>", system="just chatting"
        )
        assert is_safety_classifier_request(req) is False

    def test_xml_content_without_verdict_instruction(self):
        req = _make_request(
            "Explain this format: <transcript> ... </transcript> and a <block> tag."
        )
        assert is_safety_classifier_request(req) is False


class TestSuggestionModeTrigger:
    """Only the trailing user turn counts.

    Measured against 61 real suggestion requests in a production log: the
    marker never appears earlier than 97.61% into the prompt and is always
    followed by the same 1,363-character instruction block. Matching anywhere
    in the transcript therefore buys nothing and costs a whole class of false
    positive, because the reply for this rule is an empty string.
    """

    _MARKER = (
        "[SUGGESTION MODE: Suggest what the user might naturally type next"
        " into Claude Code.]\n\nFIRST: Look at the user's recent messages."
    )

    def _request(
        self, *turns: tuple[Literal["user", "assistant", "system"], str]
    ) -> MessagesRequest:
        return MessagesRequest(
            model="claude-3-sonnet",
            max_tokens=100,
            messages=[Message(role=role, content=text) for role, text in turns],
        )

    def test_marker_in_the_final_user_turn_matches(self):
        req = self._request(
            ("user", "refactor the parser"),
            ("assistant", "done"),
            ("user", self._MARKER),
        )
        assert is_suggestion_mode_request(req) is True

    def test_marker_only_in_history_does_not_match(self):
        # A conversation that merely discusses the marker must still get a
        # real answer. Before this guard it received an empty string.
        req = self._request(
            ("user", f"what does {self._MARKER} do?"),
            ("assistant", "it asks for a suggestion"),
            ("user", "now fix the failing test"),
        )
        assert is_suggestion_mode_request(req) is False

    def test_trailing_assistant_turn_does_not_shadow_the_last_user_turn(self):
        req = self._request(
            ("user", self._MARKER),
            ("assistant", "partial"),
        )
        assert is_suggestion_mode_request(req) is True

    def test_no_user_turn_at_all_does_not_match(self):
        req = self._request(("assistant", self._MARKER))
        assert is_suggestion_mode_request(req) is False
