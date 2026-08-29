"""Alibaba Model Studio providers: plan and region are both credential boundaries.

The four providers exist because Alibaba splits its Qwen access two ways at once.
A Coding Plan key (``sk-sp-`` prefixed) is rejected by the pay-per-token endpoints
and vice versa, and a key issued for Singapore is not valid in Beijing. Collapsing
any pair into one provider would hand a request a credential its endpoint refuses,
so these tests pin the four (endpoint, credential) pairs against each other.
"""

import pytest

from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.providers.openai_chat.profiles import OPENAI_CHAT_PROFILES
from my_claude_code.providers.openai_chat.reasoning import NoReasoning

ALIBABA_PROVIDER_IDS = (
    "alibaba_coding",
    "alibaba_coding_cn",
    "alibaba",
    "alibaba_cn",
)

# Verified live on 2026-08-08: each host answered an unauthenticated POST to
# /v1/chat/completions with HTTP 401 and an OpenAI-shaped error body, which is
# what distinguishes "wrong URL" from "dead service" (see WORKING-NOTES section 58).
EXPECTED_BASE_URLS = {
    "alibaba_coding": "https://coding-intl.dashscope.aliyuncs.com/v1",
    "alibaba_coding_cn": "https://coding.dashscope.aliyuncs.com/v1",
    "alibaba": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "alibaba_cn": "https://dashscope.aliyuncs.com/compatible-mode/v1",
}


@pytest.mark.parametrize("provider_id", ALIBABA_PROVIDER_IDS)
def test_alibaba_provider_points_at_its_documented_endpoint(provider_id: str) -> None:
    assert (
        PROVIDER_CATALOG[provider_id].default_base_url
        == (EXPECTED_BASE_URLS[provider_id])
    )


def test_coding_plan_does_not_use_the_pay_per_token_host() -> None:
    """The Coding Plan lives on a dedicated host, not on ``compatible-mode``.

    Sending a subscription key to ``compatible-mode`` is the single most common
    Alibaba misconfiguration, so the two must never share a base URL.
    """
    coding_hosts = {
        EXPECTED_BASE_URLS[key] for key in ("alibaba_coding", "alibaba_coding_cn")
    }
    token_hosts = {EXPECTED_BASE_URLS[key] for key in ("alibaba", "alibaba_cn")}

    assert coding_hosts & token_hosts == set()
    assert all("compatible-mode" not in url for url in coding_hosts)
    assert all("coding" in url for url in coding_hosts)
    assert all("compatible-mode" in url for url in token_hosts)


def test_each_alibaba_provider_owns_a_distinct_credential() -> None:
    """Four plans, four keys: no two may read the same environment variable."""
    envs = [
        PROVIDER_CATALOG[provider_id].credential_env
        for provider_id in ALIBABA_PROVIDER_IDS
    ]

    assert len(set(envs)) == len(envs)
    assert set(envs) == {
        "ALIBABA_CODING_API_KEY",
        "ALIBABA_CODING_CN_API_KEY",
        "ALIBABA_API_KEY",
        "ALIBABA_CN_API_KEY",
    }


def test_regions_are_separate_providers_with_separate_keys() -> None:
    """A Singapore key is not valid in Beijing, so the regions cannot be merged."""
    for intl, china in (
        ("alibaba", "alibaba_cn"),
        ("alibaba_coding", "alibaba_coding_cn"),
    ):
        assert (
            PROVIDER_CATALOG[intl].credential_env
            != PROVIDER_CATALOG[china].credential_env
        )
        assert (
            PROVIDER_CATALOG[intl].default_base_url
            != PROVIDER_CATALOG[china].default_base_url
        )


@pytest.mark.parametrize("provider_id", ALIBABA_PROVIDER_IDS)
def test_base_url_is_overridable_for_other_regions(provider_id: str) -> None:
    """Alibaba also serves US and workspace-scoped hosts; an override avoids new code."""
    descriptor = PROVIDER_CATALOG[provider_id]

    assert descriptor.base_url_attr == f"{provider_id}_base_url"
    assert descriptor.proxy_attr == f"{provider_id}_proxy"


@pytest.mark.parametrize("provider_id", ALIBABA_PROVIDER_IDS)
def test_alibaba_reads_reasoning_and_asks_with_the_standard_field(
    provider_id: str,
) -> None:
    """Reads ``reasoning_content``; asks with ``reasoning_effort``, not DashScope's own.

    ``enable_thinking`` is DashScope's native control and is still not sent --
    it is rejected outright by part of each roster, and no encoder emits it.
    What changed in 6.5.0 is that these profiles no longer send *nothing*: they
    speak the OpenAI standard field like every other compatible host, the
    per-model capability gate decides which models are sent it, and a model
    that refuses it is learned from its own 400.
    """
    profile = OPENAI_CHAT_PROFILES[provider_id]

    assert not isinstance(profile.reasoning, NoReasoning)
    assert profile.reasoning.dialect.effort_field == "reasoning_effort"
    # DashScope's own toggle is still nobody's wire field here.
    assert profile.reasoning.dialect.toggle is False
    assert profile.reasoning_delta_field == "reasoning_content"


@pytest.mark.parametrize("provider_id", ALIBABA_PROVIDER_IDS)
def test_caller_extra_body_survives_as_the_thinking_escape_hatch(
    provider_id: str,
) -> None:
    """FCC still never sends ``enable_thinking``, so the user must be able to."""
    policy = OPENAI_CHAT_PROFILES[provider_id].request_policy

    assert policy.include_extra_body is True


@pytest.mark.parametrize("provider_id", ALIBABA_PROVIDER_IDS)
def test_alibaba_providers_are_grouped_for_the_admin_ui(provider_id: str) -> None:
    expected = "subscription" if "coding" in provider_id else "direct"

    assert PROVIDER_CATALOG[provider_id].group == expected
