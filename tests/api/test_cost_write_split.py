"""1-hour prompt-cache writes, priced by the lifetime the host says it wrote (7.111.0).

Anthropic reports per request how many cache tokens it wrote with a 5-minute
and with a 1-hour lifetime (``usage.cache_creation``); the two bill at
different rates and models.dev states only the 5-minute one. These drive the
real capture, the real ladder and the real readers over the corpus catalogues
(``tests/support/cost_equality_corpus.py``): the rates a source states, which
source wins in which order, "not priced" with its reason where none states
one, and the Models page's line.
"""

from contextlib import ExitStack
from typing import Any

import pytest

from my_claude_code.api import request_pricing
from my_claude_code.api.model_admin import prompt_cache_write_1h_prices
from my_claude_code.api.request_capture import RequestCapture
from my_claude_code.application.cost import (
    MODE_REPORTED_ONLY,
    SOURCE_LITELLM,
    SOURCE_MODELS_DEV,
    SOURCE_PROVIDER,
    SOURCE_UNPRICED_1H_WRITE,
    TokenUsage,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.request_log import get_request_log_store
from tests.support import cost_equality_corpus as corpus

_PER_MILLION = 1_000_000

# The corpus rates the expectations below are built from, per token.
_LITELLM_SONNET_5_5 = corpus.LITELLM_INDEX["anthropic/claude-sonnet-5-5"]
_LITELLM_SONNET_5 = corpus.LITELLM_INDEX["claude-sonnet-5"]
# models.dev's anthropic bucket, claude-sonnet-5 (USD per million).
_MODELS_DEV_SONNET_5 = {
    "input": 2.0,
    "output": 10.0,
    "cache_read": 0.2,
    "cache_write": 2.5,
}
_LIVE_ONE_HOUR = float(corpus.LIVE_ONE_HOUR_PRICE)


@pytest.fixture(autouse=True)
def catalogues(tmp_path):
    config_dir = tmp_path / "config"
    corpus.write_catalogues(config_dir)
    with ExitStack() as stack:
        corpus.patched_config_dir(stack, config_dir)
        yield config_dir


@pytest.fixture
def store(tmp_path):
    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    yield store
    store.close()


def _finish(
    store,
    *,
    provider: str,
    model: str,
    split: Any,
    family: str = "anthropic_native",
    usage: tuple[int, int, int, int] = (1200, 4000, 900, 300),
    litellm: bool = False,
    live: bool = False,
) -> dict[str, Any]:
    capture = RequestCapture(
        store,
        request_id="req-1",
        endpoint="/v1/messages",
        protocol="anthropic",
        stream=True,
        requested_model=model,
        input_text=None,
        params=None,
        cost_litellm_enabled=litellm,
        cost_openrouter_live_enabled=live,
    )
    capture._record.provider = provider
    capture._record.resolved_model = model
    for frame in corpus.capture_frames(family, split, usage, model):
        capture._parse_frame(frame)
    capture._finalize("success")
    store.close()
    row = store.get_request("req-1")
    assert row is not None
    return row


def _split(five: int, hour: int) -> dict[str, int]:
    return {"ephemeral_5m_input_tokens": five, "ephemeral_1h_input_tokens": hour}


class TestTheSplitIsRecorded:
    def test_beside_the_total_never_instead_of_it(self, store) -> None:
        row = _finish(
            store,
            provider="anthropic_oauth",
            model="claude-sonnet-5",
            split=_split(600, 300),
        )
        assert row["cache_write_tokens"] == 900
        assert (row["cache_write_5m_tokens"], row["cache_write_1h_tokens"]) == (
            600,
            300,
        )
        # Every other counter exactly as the host reported it.
        assert (row["tokens_in"], row["cache_read_tokens"], row["tokens_out"]) == (
            1200,
            4000,
            300,
        )

    def test_a_host_reporting_only_the_total_leaves_it_null(self, store) -> None:
        row = _finish(
            store, provider="anthropic_oauth", model="claude-sonnet-5", split=None
        )
        assert row["cache_write_5m_tokens"] is None
        assert row["cache_write_1h_tokens"] is None

    def test_a_chat_translated_stream_carries_no_split(self, store) -> None:
        row = _finish(
            store,
            provider="open_router",
            model="anthropic/claude-sonnet-5",
            split=_split(600, 300),
            family="chat_translated",
        )
        assert row["cache_write_tokens"] == 900
        assert row["cache_write_1h_tokens"] is None

    def test_a_reported_zero_is_a_zero(self, store) -> None:
        row = _finish(
            store,
            provider="anthropic_oauth",
            model="claude-sonnet-5",
            split=_split(900, 0),
        )
        assert (row["cache_write_5m_tokens"], row["cache_write_1h_tokens"]) == (900, 0)
        assert row["params"] is None


class TestTheRate:
    def test_litellm_states_it_and_prices_the_row_whole(self, store) -> None:
        row = _finish(
            store,
            provider="anthropic",
            model="claude-sonnet-5-5",
            split=_split(600, 300),
            litellm=True,
        )
        rates = _LITELLM_SONNET_5_5
        expected = (
            1200 * rates["input_cost_per_token"]
            + 4000 * rates["cache_read_input_token_cost"]
            + 600 * rates["cache_creation_input_token_cost"]
            + 300 * rates["cache_creation_input_token_cost_above_1hr"]
            + 300 * rates["output_cost_per_token"]
        )
        assert row["cost_source"] == SOURCE_LITELLM
        assert row["cost_usd"] == pytest.approx(expected)
        note = row["params"]["cost_write_1h"]
        assert note["source"] == "litellm"
        assert note["usd_per_million"] == pytest.approx(6.0)
        assert "anthropic/claude-sonnet-5-5" in note["label"]

    def test_the_one_hour_rate_is_not_the_five_minute_rate(self, store) -> None:
        """The error 7.111.0 removed: the same row priced as if every write were 5 min."""
        row = _finish(
            store,
            provider="anthropic",
            model="claude-sonnet-5-5",
            split=_split(600, 300),
            litellm=True,
        )
        rates = _LITELLM_SONNET_5_5
        as_before = (
            1200 * rates["input_cost_per_token"]
            + 4000 * rates["cache_read_input_token_cost"]
            + 900 * rates["cache_creation_input_token_cost"]
            + 300 * rates["output_cost_per_token"]
        )
        assert row["cost_usd"] != pytest.approx(as_before)

    def test_no_source_states_it_so_the_row_is_not_priced_and_says_why(
        self, store
    ) -> None:
        row = _finish(
            store,
            provider="anthropic",
            model="claude-sonnet-5-5",
            split=_split(600, 300),
        )
        assert row["cost_usd"] is None
        assert row["cost_source"] == SOURCE_UNPRICED_1H_WRITE
        assert row["params"] is None

    def test_openrouter_live_fills_the_gap_last(self, store) -> None:
        row = _finish(
            store,
            provider="anthropic_oauth",
            model="claude-sonnet-5",
            split=_split(600, 300),
            live=True,
        )
        rates = _MODELS_DEV_SONNET_5
        expected = (
            1200 * rates["input"]
            + 4000 * rates["cache_read"]
            + 600 * rates["cache_write"]
            + 300 * rates["output"]
        ) / _PER_MILLION + 300 * _LIVE_ONE_HOUR
        assert row["cost_source"] == SOURCE_MODELS_DEV
        assert row["cost_usd"] == pytest.approx(expected)
        note = row["params"]["cost_write_1h"]
        assert note["source"] == "openrouter_live"
        assert note["label"].startswith("OpenRouter live, ")

    def test_litellm_wins_over_the_openrouter_gap_fill(self, store) -> None:
        row = _finish(
            store,
            provider="anthropic_oauth",
            model="claude-sonnet-5",
            split=_split(600, 300),
            litellm=True,
            live=True,
        )
        assert row["cost_source"] == SOURCE_LITELLM
        assert row["params"]["cost_write_1h"]["usd_per_million"] == pytest.approx(
            _LITELLM_SONNET_5["cache_creation_input_token_cost_above_1hr"]
            * _PER_MILLION
        )

    def test_the_providers_own_list_comes_first(self, store) -> None:
        request_pricing.set_published_row_lookup(
            lambda provider, model: (
                {"pricing": {"input_cache_write_1h": "0.000005"}}
                if (provider, model) == ("anthropic_oauth", "claude-sonnet-5")
                else None
            )
        )
        row = _finish(
            store,
            provider="anthropic_oauth",
            model="claude-sonnet-5",
            split=_split(600, 300),
            litellm=True,
            live=True,
        )
        assert row["cost_source"] == SOURCE_MODELS_DEV
        note = row["params"]["cost_write_1h"]
        assert note["source"] == "provider_list"
        assert note["usd_per_million"] == pytest.approx(5.0)

    def test_a_row_nothing_could_price_stays_null(self, store) -> None:
        row = _finish(
            store, provider="nowhere", model="unknown-model", split=_split(600, 300)
        )
        assert (row["cost_usd"], row["cost_source"]) == (None, None)

    def test_five_minute_writes_only_price_as_before(self, store) -> None:
        row = _finish(
            store,
            provider="anthropic_oauth",
            model="claude-sonnet-5",
            split=_split(900, 0),
        )
        rates = _MODELS_DEV_SONNET_5
        expected = (
            1200 * rates["input"]
            + 4000 * rates["cache_read"]
            + 900 * rates["cache_write"]
            + 300 * rates["output"]
        ) / _PER_MILLION
        assert row["cost_source"] == SOURCE_MODELS_DEV
        assert row["cost_usd"] == pytest.approx(expected)


class TestTheLadderEntryPoint:
    _USAGE = TokenUsage(
        tokens_in=10, tokens_out=10, cache_write_tokens=300, cache_write_1h_tokens=300
    )

    def test_a_reported_cost_still_wins(self) -> None:
        answer = request_pricing.price_request(
            "anthropic",
            "claude-sonnet-5-5",
            self._USAGE,
            reported_usd=0.5,
            litellm_enabled=False,
        )
        assert (answer.cost_usd, answer.cost_source) == (0.5, SOURCE_PROVIDER)

    def test_reported_only_without_a_report_prices_nothing(self) -> None:
        answer = request_pricing.price_request(
            "anthropic",
            "claude-sonnet-5-5",
            self._USAGE,
            reported_usd=None,
            mode=MODE_REPORTED_ONLY,
            litellm_enabled=True,
        )
        assert (answer.cost_usd, answer.cost_source) == (None, None)

    def test_a_split_larger_than_the_total_never_prices_more_writes(self) -> None:
        usage = TokenUsage(cache_write_tokens=100, cache_write_1h_tokens=900)
        assert usage.one_hour_write_tokens == 100
        answer = request_pricing.price_request(
            "anthropic",
            "claude-sonnet-5-5",
            usage,
            reported_usd=None,
            litellm_enabled=True,
        )
        assert answer.cost_usd == pytest.approx(
            100 * _LITELLM_SONNET_5_5["cache_creation_input_token_cost_above_1hr"]
        )


def test_the_models_page_lists_the_rate_for_hosts_that_take_one_hour() -> None:
    settings = Settings.model_validate(
        {
            "COST_SOURCE_LITELLM_ENABLED": "true",
            "MODEL_METADATA_OPENROUTER_LIVE": "true",
        }
    )
    page = {
        "providers": [
            {
                "provider_id": "anthropic_oauth",
                "models": [
                    {
                        "model_ref": "anthropic_oauth/claude-sonnet-5",
                        "model_id": "claude-sonnet-5",
                    },
                    {
                        "model_ref": "anthropic_oauth/claude-unknown",
                        "model_id": "claude-unknown",
                    },
                ],
            },
            {
                "provider_id": "nvidia_nim",
                "models": [
                    {
                        "model_ref": "nvidia_nim/z-ai/glm-5.3-flash",
                        "model_id": "z-ai/glm-5.3-flash",
                    }
                ],
            },
        ]
    }
    found = prompt_cache_write_1h_prices(page, settings)
    assert set(found) == {
        "anthropic_oauth/claude-sonnet-5",
        "anthropic_oauth/claude-unknown",
    }
    sonnet = found["anthropic_oauth/claude-sonnet-5"]
    assert [entry["source"] for entry in sonnet] == ["litellm", "openrouter_live"]
    assert sonnet[0]["usd_per_million"] == pytest.approx(6.0)
    assert sonnet[1]["usd_per_million"] == pytest.approx(4.0)
    # No source states it: listed, empty -- the page says "not priced".
    assert found["anthropic_oauth/claude-unknown"] == []
