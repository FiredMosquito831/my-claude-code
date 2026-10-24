"""7.109.0: InferHub's market snapshot -- built, merged, bounded, masked, stored."""

import asyncio
import json
from typing import Any

import pytest

from my_claude_code.config.marketplaces import INFERHUB_MARKETPLACE
from my_claude_code.config.settings import Settings
from my_claude_code.providers.runtime import marketplace_market
from my_claude_code.providers.runtime.config import MaskedExit
from my_claude_code.providers.runtime.marketplace_market import (
    build_snapshot,
    refresh_market_snapshot,
)
from my_claude_code.runtime.catalogue_store import (
    read_market_snapshot,
    store_market_snapshot,
)

DIALECT = INFERHUB_MARKETPLACE
KEY = "sk-airo-" + "q" * 24
ROWS = {
    "cc/claude-opus-4-6": {
        "id": "cc/claude-opus-4-6",
        "pricing": {
            "official_in": 5,
            "official_out": 25,
            "asks_in": {"summarised_from": 41, "levels": [[0.75, 38], [2.5, 3]]},
            "asks_out": {"summarised_from": 41, "levels": [[3.75, 38], [12.5, 3]]},
            "min_ask_in": 0.75,
            "min_ask_out": 3.75,
        },
    },
    "claude-opus-4-6": {"id": "claude-opus-4-6", "owned_by": "alias"},
}
PUBLIC = {
    "market": {
        "ts": 1,
        "models": [
            {
                "slug": "cc/claude-opus-4-6",
                "minAskIn": 0.75,
                "minAskOut": 3.75,
                "maxAskIn": 2.5,
                "maxAskOut": 12.5,
                "lastRate": 1.9,
            },
            {"slug": "zai/glm-5", "minAskIn": None, "minAskOut": 0.4, "lastRate": None},
        ],
    },
    "status": {
        "models": [
            {
                "id": "cc/claude-opus-4-6",
                "successRatePct": 99.1,
                "avgTtftMs": 812,
                "avgTps": 61.5,
            }
        ]
    },
    "pricing_cache": {"models": [{"slug": "cc/claude-opus-4-6", "hitRate": 0.42}]},
    "pricing_config": {"maxAskPctOfOfficial": 0.5, "platformFeePct": 0.2},
    "catalog": {
        "upstreams": [{"prefix": "cc", "systemPromptNote": "A header is added."}]
    },
    "combos": [
        {
            "slug": "fast",
            "selection": "fastest",
            "members": ["cc/claude-opus-4-6", {"model": "cx/gpt-6"}],
        }
    ],
}
ALL_OK = {source.name: "ok" for source in DIALECT.market_sources}


def test_the_document_merges_every_source_deterministically() -> None:
    document = build_snapshot(DIALECT, ROWS, PUBLIC, ALL_OK, None, at="T")
    opus = document["models"]["cc/claude-opus-4-6"]
    assert opus["official"] == [5.0, 25.0]
    assert opus["levels_in"] == [[0.75, 38], [2.5, 3]]
    assert opus["min"] == [0.75, 3.75]
    assert opus["max"] == [2.5, 12.5]
    assert opus["perf"] == {"success_pct": 99.1, "ttft_ms": 812.0, "tps": 61.5}
    assert opus["cache_hit"] == 0.42
    assert "claude-opus-4-6" not in document["models"]
    # A side the public market reports as null is not invented.
    assert "min" not in document["models"]["zai/glm-5"]
    assert document["platform"] == {"max_ask_pct_of_official": 0.5}
    assert document["notes"] == {"cc": "A header is added."}
    assert document["combos"] == [
        {
            "id": "combo/fast",
            "members": ["cc/claude-opus-4-6", "cx/gpt-6"],
            "selection": "fastest",
        }
    ]
    again = build_snapshot(DIALECT, ROWS, PUBLIC, ALL_OK, None, at="T")
    assert json.dumps(again) == json.dumps(document)


def test_a_failed_source_keeps_what_it_said_last() -> None:
    previous = build_snapshot(DIALECT, ROWS, PUBLIC, ALL_OK, None, at="T0")
    failed = {**ALL_OK, "status": "failed:ConnectError", "combos": "http_403"}
    fetched = {
        name: value
        for name, value in PUBLIC.items()
        if name not in {"status", "combos"}
    }
    document = build_snapshot(DIALECT, ROWS, fetched, failed, previous, at="T1")
    assert document["models"]["cc/claude-opus-4-6"]["perf"]["tps"] == 61.5
    assert document["combos"] == previous["combos"]
    assert document["sources"]["status"] == "failed:ConnectError"


def test_the_document_stays_small_at_the_worst_case() -> None:
    """270 models x 29 levels each side (spec §8: about 0.3 MB at most)."""

    rows = {
        f"up/model-{index}": {
            "pricing": {
                "official_in": 5,
                "official_out": 25,
                "asks_in": {
                    "levels": [[0.05 * level, level] for level in range(1, 30)]
                },
                "asks_out": {
                    "levels": [[0.25 * level, level] for level in range(1, 30)]
                },
            }
        }
        for index in range(270)
    }
    document = build_snapshot(DIALECT, rows, {}, {}, None, at="T")
    size = len(json.dumps(document, separators=(",", ":")))
    assert size < 400_000, size


def _settings(**values: Any) -> Settings:
    return Settings.model_validate(
        {"INFERHUB_MARKET_BASE_URL": "https://market.invalid/api", **values}
    )


def test_no_key_reads_nothing() -> None:
    outcome = asyncio.run(
        refresh_market_snapshot(_settings(), "inferhub", DIALECT, ROWS, None)
    )
    assert outcome.status == "no_key"


def test_a_chain_that_says_no_sends_nothing(monkeypatch) -> None:
    monkeypatch.setattr(
        marketplace_market,
        "masked_exit_for",
        lambda *args, **kwargs: MaskedExit(
            proxy=None, refused="Direct fallback is off"
        ),
    )
    outcome = asyncio.run(
        refresh_market_snapshot(
            _settings(INFERHUB_API_KEY=KEY), "inferhub", DIALECT, ROWS, None
        )
    )
    assert outcome.status == "not_sent"


@pytest.mark.parametrize("public", [True, False])
def test_every_read_goes_through_the_chosen_exit(monkeypatch, public: bool) -> None:
    seen: list[tuple[str, str | None, str | None]] = []

    async def fetch(
        url: str, *, proxy: str | None, timeout: float, key: str | None
    ) -> Any:
        seen.append((url, proxy, key))
        name = next(s.name for s in DIALECT.market_sources if url.endswith(s.path))
        return PUBLIC[name]

    monkeypatch.setattr(
        marketplace_market,
        "masked_exit_for",
        lambda *args, **kwargs: MaskedExit(
            proxy="socks5h://exit.invalid:1080", label="px"
        ),
    )
    monkeypatch.setattr(marketplace_market, "fetch_marketplace_source", fetch)
    outcome = asyncio.run(
        refresh_market_snapshot(
            _settings(INFERHUB_API_KEY=KEY, MARKETPLACE_PUBLIC_MARKET=public),
            "inferhub",
            DIALECT,
            ROWS,
            None,
        )
    )
    assert outcome.status == "built"
    assert {proxy for _url, proxy, _key in seen} == {"socks5h://exit.invalid:1080"}
    keyed = {url.rsplit("/api", 1)[1] for url, _proxy, key in seen if key == KEY}
    keyless = {url.rsplit("/api", 1)[1] for url, _proxy, key in seen if key is None}
    assert keyed == {"/catalog", "/combos"}
    assert keyless == (
        {"/market", "/status", "/pricing/cache", "/pricing/config"} if public else set()
    )
    assert outcome.payload is not None
    assert outcome.payload["sources"]["market"] == ("ok" if public else "off")


def test_the_store_writes_only_when_the_market_moved() -> None:
    first = build_snapshot(DIALECT, ROWS, PUBLIC, ALL_OK, None, at="T0")
    assert store_market_snapshot(DIALECT, first, computed_at=1.0) is True
    same = build_snapshot(DIALECT, ROWS, PUBLIC, ALL_OK, None, at="T1")
    assert store_market_snapshot(DIALECT, same, computed_at=2.0) is False
    stored = read_market_snapshot(DIALECT)
    assert stored is not None and stored["fetched_at"] == "T0"
