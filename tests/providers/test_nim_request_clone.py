"""Tests for NVIDIA NIM request body cloning helpers."""

from copy import deepcopy

from my_claude_code.providers.nvidia_nim.retry import (
    chat_template_evidence,
    clone_body_without_reasoning_budget,
    complaint_evidence_snippet,
    reasoning_budget_evidence,
    reasoning_content_evidence,
    sampling_parameter_evidence,
    upstream_complaint,
)


def test_clone_body_without_reasoning_budget_strips_top_level_and_nested():
    body: dict = {
        "model": "x",
        "extra_body": {
            "reasoning_budget": 99,
            "chat_template_kwargs": {"reasoning_budget": 42, "thinking": True},
            "top_k": 1,
        },
    }
    original_extra = deepcopy(body["extra_body"])
    out = clone_body_without_reasoning_budget(body)

    assert out is not None
    assert out["extra_body"]["chat_template_kwargs"] == {"thinking": True}
    assert "reasoning_budget" not in out["extra_body"]
    assert body["extra_body"] == original_extra


def test_clone_body_without_reasoning_budget_returns_none_when_unchanged():
    body = {"model": "x", "extra_body": {"top_k": 3}}
    assert clone_body_without_reasoning_budget(body) is None


def test_clone_body_without_reasoning_budget_returns_none_without_extra_body():
    assert clone_body_without_reasoning_budget({"model": "y"}) is None


def test_clone_body_drops_empty_extra_body_after_strip():
    body = {"model": "z", "extra_body": {"reasoning_budget": 7}}
    out = clone_body_without_reasoning_budget(body)
    assert out is not None
    assert "extra_body" not in out
    assert "extra_body" in body


class _Err(Exception):
    def __init__(self, body):
        super().__init__("Error code: 400 - see body")
        self.body = body


def test_upstream_complaint_ignores_echoed_request_payload():
    complaint = upstream_complaint(
        _Err(
            {
                "detail": [
                    {
                        "msg": "top_p is immutable",
                        "loc": ["body", "top_p"],
                        "input": {"chat_template_kwargs": {"thinking": True}},
                    }
                ]
            }
        )
    )

    assert "chat_template" not in complaint
    assert "top_p is immutable" in complaint


def test_upstream_complaint_falls_back_to_str_without_body():
    assert "chat_template" in upstream_complaint(Exception("bad chat_template"))


def test_evidence_matchers_require_whole_words():
    assert sampling_parameter_evidence("stop_p is wrong") is None
    assert sampling_parameter_evidence("top_p is immutable") == "top_p"
    assert (
        chat_template_evidence("chat_template_kwargs rejected")
        == "chat_template_kwargs"
    )
    assert chat_template_evidence("template rejected") is None
    assert (
        reasoning_content_evidence("reasoning_content rejected") == "reasoning_content"
    )


def test_reasoning_budget_evidence_requires_paired_reasoning_config():
    assert reasoning_budget_evidence("reasoning_budget too large") == "reasoning_budget"
    assert reasoning_budget_evidence("thinking_token_budget too large") is None
    assert (
        reasoning_budget_evidence("reasoning_config.thinking_token_budget too large")
        == "thinking_token_budget"
    )


def test_complaint_evidence_snippet_is_bounded():
    assert complaint_evidence_snippet("a" * 500).endswith("...")
    assert len(complaint_evidence_snippet("a" * 500)) == 203
