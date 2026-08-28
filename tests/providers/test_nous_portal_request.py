"""Tests for the Nous Portal request policy.

Nous Portal rejects an API-key request that carries no ``tags`` array with a
``user=`` entry. Verified against the live API on 2026-08-28:

    no tags  -> HTTP 400 {"status":400,"message":"This request is not valid.
                Check the model name and other parameters. Additional info:
                missing tags"}
    +tags    -> HTTP 200, a normal completion

The requirement is undocumented in the OpenAPI spec because OAuth callers are
identified by their bearer token instead. Enforcement began 2026-08-27, when
every previously-working ``tencent/hy3:free`` request started failing.
"""

from typing import Any

from my_claude_code.config.constants import NOUS_PORTAL_USER_TAG_DEFAULT
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.providers.nous_portal.client import (
    _PROFILE,
    apply_nous_user_tag,
)
from my_claude_code.providers.openai_chat.request_policy import (
    build_openai_chat_request_body,
)
from tests.providers.request_factory import make_messages_request


def _build(**overrides) -> dict:
    kwargs: dict[str, Any] = {
        "model": "tencent/hy3:free",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 16,
        "system": None,
        "temperature": None,
        "top_p": None,
        "top_k": None,
        "stop_sequences": None,
        "tools": None,
        "extra_body": None,
        "thinking": None,
    }
    kwargs.update(overrides)
    request = make_messages_request(**kwargs)
    return build_openai_chat_request_body(
        request,
        reasoning=ReasoningPolicy.provider_default(),
        policy=_PROFILE.request_policy,
        postprocessors=_PROFILE.postprocessors,
    )


class TestNousUserTag:
    def test_the_profile_registers_the_tag_postprocessor(self):
        assert apply_nous_user_tag in _PROFILE.postprocessors

    def test_a_request_carries_the_user_tag(self):
        body = _build()

        assert body["extra_body"]["tags"] == [NOUS_PORTAL_USER_TAG_DEFAULT]

    def test_the_tag_has_the_mandatory_user_prefix(self):
        """Nous answers "missing user tag" to a tags array without one."""
        assert NOUS_PORTAL_USER_TAG_DEFAULT.startswith("user=")
        assert len(NOUS_PORTAL_USER_TAG_DEFAULT) > len("user=")

    def test_the_tag_goes_in_extra_body_not_the_top_level(self):
        """``body`` is splatted as ``**create_body`` into the OpenAI SDK.

        ``chat.completions.create`` has no ``tags`` parameter, so a top-level
        key would raise locally instead of reaching the wire. The SDK flattens
        ``extra_body`` into the JSON root, which is where Nous wants it.
        """
        body = _build()

        assert "tags" not in body
        assert "tags" in body["extra_body"]

    def test_a_caller_supplied_tags_value_is_preserved(self):
        body = _build(extra_body={"tags": ["user=someone-else"]})

        assert body["extra_body"]["tags"] == ["user=someone-else"]

    def test_the_postprocessor_is_idempotent(self):
        body: dict = {}
        request = make_messages_request(model="tencent/hy3:free")
        policy = ReasoningPolicy.provider_default()

        apply_nous_user_tag(body, request, policy)
        apply_nous_user_tag(body, request, policy)

        assert body["extra_body"]["tags"] == [NOUS_PORTAL_USER_TAG_DEFAULT]

    def test_reasoning_replay_postprocessor_is_still_registered(self):
        """The tag must be added to the gateway chain, not replace it."""
        assert len(_PROFILE.postprocessors) >= 2
