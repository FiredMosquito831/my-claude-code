"""Who decides one attempt's prompt-cache lifetime (7.105.0, user Q5/Q7).

The model's override row, then its provider's row, then what the door stamped
from Model Config (tier, then global), then the client. A row's ``null`` is
"from client" there, however the levels below are set. A gateway family takes
a lifetime only from a row, and once it has refused one it takes none.
"""

import pytest

from my_claude_code.application.prompt_cache import (
    MODEL_OVERRIDE_SOURCE,
    PROVIDER_OVERRIDE_SOURCE,
    choose_prompt_cache_lifetime,
    prompt_cache_ask_for_attempt,
)
from my_claude_code.config.model_overrides import (
    EMPTY_MODEL_OVERRIDES,
    ModelParameterOverrides,
)
from my_claude_code.config.prompt_cache_lifetimes import (
    ANTHROPIC_MESSAGES_GATEWAY_LIFETIMES,
    ANTHROPIC_MESSAGES_LIFETIMES,
    PROMPT_CACHE_LIFETIME_FAMILIES,
    prompt_cache_gateway_family,
)
from my_claude_code.core.anthropic.models import PromptCacheAsk

ANTHROPIC = PROMPT_CACHE_LIFETIME_FAMILIES[ANTHROPIC_MESSAGES_LIFETIMES]
GATEWAY = PROMPT_CACHE_LIFETIME_FAMILIES[ANTHROPIC_MESSAGES_GATEWAY_LIFETIMES]
TIER = PromptCacheAsk(30, "PROMPT_CACHE_TTL_OPUS")


def _rows(model: object = ..., provider: object = ...) -> ModelParameterOverrides:
    document: dict = {"providers": {}, "models": {}}
    if model is not ...:
        document["models"]["anthropic_oauth/claude-opus-5-5"] = {
            "prompt_cache_ttl": model
        }
    if provider is not ...:
        document["providers"]["anthropic_oauth"] = {"prompt_cache_ttl": provider}
    return ModelParameterOverrides.from_document(document)


def _ask(table: ModelParameterOverrides, stamped=TIER, family=ANTHROPIC):
    return prompt_cache_ask_for_attempt(
        stamped,
        family,
        provider_id="anthropic_oauth",
        model_id="claude-opus-5-5",
        overrides=table,
    )


def test_nothing_in_the_file_is_the_stamp_itself() -> None:
    assert _ask(EMPTY_MODEL_OVERRIDES) is TIER
    assert _ask(EMPTY_MODEL_OVERRIDES, stamped=None) is None
    # A file with other keys only: still the stamp.
    other = ModelParameterOverrides.from_document(
        {"models": {"anthropic_oauth/claude-opus-5-5": {"temperature": 0.3}}}
    )
    assert _ask(other) is TIER


def test_the_model_row_wins_over_everything() -> None:
    assert _ask(_rows(model=10, provider=45)) == PromptCacheAsk(
        10, MODEL_OVERRIDE_SOURCE
    )


def test_the_provider_row_wins_over_the_tier_when_the_model_row_inherits() -> None:
    assert _ask(_rows(provider=45)) == PromptCacheAsk(45, PROVIDER_OVERRIDE_SOURCE)
    assert _ask(_rows(provider=45), stamped=None) == PromptCacheAsk(
        45, PROVIDER_OVERRIDE_SOURCE
    )


def test_force_unset_on_a_row_is_from_client_over_a_tier() -> None:
    assert _ask(_rows(model=None)) is None
    assert _ask(_rows(model=None, provider=45)) is None
    assert _ask(_rows(provider=None)) is None
    # A model row's minutes still beat a provider row's force unset.
    assert _ask(_rows(model=3, provider=None)) == PromptCacheAsk(
        3, MODEL_OVERRIDE_SOURCE
    )


def test_the_rows_of_another_model_or_provider_do_not_apply() -> None:
    table = ModelParameterOverrides.from_document(
        {
            "models": {"anthropic_oauth/claude-haiku-5-5": {"prompt_cache_ttl": 9}},
            "providers": {"anthropic": {"prompt_cache_ttl": 9}},
        }
    )
    assert _ask(table) is TIER


def test_a_gateway_never_reads_the_stamp() -> None:
    assert _ask(EMPTY_MODEL_OVERRIDES, family=GATEWAY) is None
    assert _ask(_rows(), family=GATEWAY) is None
    assert _ask(_rows(model=20), family=GATEWAY) == PromptCacheAsk(
        20, MODEL_OVERRIDE_SOURCE
    )
    assert _ask(_rows(provider=45), family=GATEWAY) == PromptCacheAsk(
        45, PROVIDER_OVERRIDE_SOURCE
    )


@pytest.mark.parametrize(
    ("minutes", "wire"), [(1, "5m"), (5, "5m"), (6, "1h"), (20, "1h"), (60, "1h")]
)
def test_a_gateway_is_sent_anthropics_spelling_of_the_nearest_lifetime(
    minutes: int, wire: str
) -> None:
    choice = choose_prompt_cache_lifetime(
        PromptCacheAsk(minutes, MODEL_OVERRIDE_SOURCE), prompt_cache_gateway_family()
    )

    assert choice is not None and choice.sent is not None
    assert choice.sent.wire == wire
    assert choice.note()["host_lifetimes_known"] is False


@pytest.mark.parametrize("refused", [("1h",), ("5m",), ("5m", "1h")])
def test_a_gateway_that_refused_any_lifetime_is_sent_none(
    refused: tuple[str, ...],
) -> None:
    choice = choose_prompt_cache_lifetime(
        PromptCacheAsk(3, MODEL_OVERRIDE_SOURCE), GATEWAY, refused=refused
    )

    assert choice is not None
    assert choice.sent is None
    assert choice.note()["refused_by_host"] == list(refused)


def test_a_declared_host_that_refused_one_lifetime_is_sent_the_other() -> None:
    """Unchanged from 7.104.0 (user decision 2026-10-21 13:21)."""

    choice = choose_prompt_cache_lifetime(
        PromptCacheAsk(30, PROVIDER_OVERRIDE_SOURCE), ANTHROPIC, refused=("1h",)
    )

    assert choice is not None and choice.sent is not None
    assert choice.sent.wire == "5m"
    assert "host_lifetimes_known" not in choice.note()


def test_the_anthropic_family_is_exactly_what_7_104_0_declared() -> None:
    assert ANTHROPIC.lifetimes_known is True
    assert ANTHROPIC.rows_only is False
    assert ANTHROPIC.refusal_per_host is False
    assert ANTHROPIC.automatic_marker is True
    assert [life.wire for life in ANTHROPIC.lifetimes] == ["5m", "1h"]


def test_the_gateway_family_adds_no_marker_and_learns_per_host() -> None:
    assert GATEWAY.automatic_marker is False
    assert GATEWAY.lifetimes_known is False
    assert GATEWAY.rows_only is True
    assert GATEWAY.refusal_per_host is True
    assert [life.wire for life in GATEWAY.lifetimes] == ["5m", "1h"]
