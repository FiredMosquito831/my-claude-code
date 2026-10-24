"""The InferHub page's routes (7.110.0): what they read, write, refuse and never do.

* the sidebar summary costs no read and says whether a key is set (Q16);
* with no key the page reads nothing and every keyed block says so (never 0);
* the profile store is written whole, through PR A's parser and owner-only
  writer, and a Save never loses anything the operator did not confirm;
* assignments are not written here at all: the page's inherit Save through
  the Models page's route leaves model_overrides.json byte for byte (E6);
* "would match now", the offer ladder, the last requests (NULL is None, never
  0) and the charged-ask matching.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api import admin_marketplace_routes
from my_claude_code.api.marketplace_page import (
    match_charged,
    note_snapshot,
    recent_rows,
    would_match,
)
from my_claude_code.application.marketplace import (
    current_market_snapshot,
    market_snapshot_cache,
    market_snapshot_from_payload,
)
from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.config.marketplace_profiles import (
    MarketplaceProfiles,
    load_marketplace_profiles,
    parse_profile,
    reset_marketplace_profiles_cache,
    save_marketplace_profiles,
)
from my_claude_code.config.marketplaces import INFERHUB_MARKETPLACE, snapshot_key
from my_claude_code.config.model_overrides import reset_model_overrides_cache
from my_claude_code.config.settings import Settings
from my_claude_code.runtime.application import ApplicationRuntime
from tests.api.support import create_test_app, provider_manager_for_app

PAGE = "/admin/api/marketplaces"
PROFILES = "/admin/api/marketplaces/profiles"
OVERRIDES = "/admin/api/model-admin/overrides"
KEY = "sk-airo-" + "p" * 24
#: 2026-10-24T01:20:00Z, the instant the request rows below carry.
T0 = datetime(2026, 10, 24, 1, 20, tzinfo=UTC).timestamp()
OPUS = "cc/claude-opus-4-6"
ASTRA = "cx/gpt-6-astra"
SNAPSHOT = {
    "fetched_at": "2026-10-24T01:07:35Z",
    "sources": {"listing": "ok", "market": "ok", "catalog": "http_403", "combos": "ok"},
    "platform": {"max_ask_pct_of_official": 0.5},
    "notes": {"cc": "A one-line Claude Code client header is added. <b>not markup</b>"},
    "combos": [{"id": "combo/fast", "members": [OPUS, ASTRA], "selection": "fastest"}],
    "models": {
        OPUS: {
            "official": [5.0, 25.0],
            "levels_in": [[0.75, 38], [1.0, 10], [2.5, 3]],
            "levels_out": [[3.75, 38], [5.0, 10], [12.5, 3]],
            "min": [0.75, 3.75],
            "max": [2.5, 12.5],
            "last": 1.9,
            "perf": {"success_pct": 99.1, "ttft_ms": 812.0, "tps": 61.5},
            "cache_hit": 0.62,
        },
        ASTRA: {"official": [1.25, 10.0], "min": [0.3, 2.4]},
    },
}


@pytest.fixture
def home(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    for key in ("MODEL", "MODEL_VISIBILITY_ALLOW", "MODEL_VISIBILITY_DENY"):
        monkeypatch.delenv(key, raising=False)
    reset_model_overrides_cache()
    reset_marketplace_profiles_cache()
    return tmp_path


def _app(*, key: str = KEY):
    settings = Settings()
    settings.model = f"inferhub/{OPUS}"
    settings.inferhub_api_key = key
    app = create_test_app(settings)
    provider_manager_for_app(app).cache_model_infos(
        "inferhub", {ProviderModelInfo(OPUS), ProviderModelInfo(ASTRA)}
    )
    return app


def _client(app) -> TestClient:
    return TestClient(app, client=("127.0.0.1", 50000))


def _snapshot(payload: dict[str, Any] = SNAPSHOT) -> None:
    assert market_snapshot_cache().write(
        INFERHUB_MARKETPLACE.snapshot_entry,
        key=snapshot_key(INFERHUB_MARKETPLACE),
        payload=payload,
        computed_at=0.0,
    )


def _profiles(**documents: dict[str, Any]) -> None:
    parsed = {}
    for name, document in documents.items():
        profile, problem = parse_profile(name, document)
        assert profile is not None, problem
        parsed[name] = profile
    save_marketplace_profiles(MarketplaceProfiles(profiles=parsed))


def _store(home: Path) -> Path:
    return home / ".mcc" / "marketplace_profiles.json"


def _overrides(home: Path) -> Path:
    return home / ".mcc" / "model_overrides.json"


# ------------------------------------------------------------ the sidebar


def test_the_sidebar_summary_says_whether_a_key_is_set(home):
    keyed = _client(_app()).get(PAGE, params={"summary": "true"}).json()
    assert keyed == {
        "views": [
            {
                "provider_id": "inferhub",
                "dialect_id": "inferhub_bid_headers",
                "label": "InferHub",
                "display_name": "InferHub",
                "has_key": True,
                "account_reads": True,
            }
        ]
    }
    bare = _client(_app(key="")).get(PAGE, params={"summary": "true"}).json()
    assert bare["views"][0]["has_key"] is False


def test_with_no_key_nothing_is_read_and_the_account_block_says_so(home):
    # The autouse isolation refuses every keyed read: reaching one fails here.
    page = _client(_app(key="")).get(PAGE, params={"account": "true"}).json()
    provider = page["providers"][0]
    assert provider["has_key"] is False
    assert provider["account"]["state"] == "no_key"
    assert "nothing is read" in provider["account"]["words"]
    assert "not verified on a real account" in provider["account"]["unverified"]
    refreshed = _client(_app(key="")).post("/admin/api/marketplaces/refresh").json()
    assert refreshed["refresh"]["snapshot_built"] is False


def test_the_routes_are_local_only(home):
    client = TestClient(_app(), client=("127.0.0.1", 50000))
    response = client.get(PAGE, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


# -------------------------------------------------------- the page payload


def test_the_page_shows_snapshot_ages_assignments_and_would_match(home):
    _snapshot()
    _profiles(
        cap20={"kind": "pct_of_official", "percent": 20},
        follow={"kind": "cheapest_level_now"},
        tight={"kind": "max_price", "max_input_usd": 0.5, "max_output_usd": 2.5},
    )
    client = _client(_app())
    assert (
        client.post(
            OVERRIDES,
            json={
                "scope": "provider",
                "key": "inferhub",
                "updates": {"marketplace_profile": "cap20"},
            },
        ).status_code
        == 200
    )
    assert (
        client.post(
            OVERRIDES,
            json={
                "scope": "model",
                "key": f"inferhub/{ASTRA}",
                "updates": {"marketplace_profile": "tight"},
            },
        ).status_code
        == 200
    )
    page = client.get(PAGE).json()
    provider = page["providers"][0]
    assert provider["assignments"]["provider"] == {"state": "value", "value": "cap20"}
    assert provider["assignments"]["models"] == [
        {
            "model_ref": f"inferhub/{ASTRA}",
            "model_id": ASTRA,
            "state": "value",
            "value": "tight",
        }
    ]
    sources = {source["name"]: source for source in provider["snapshot"]["sources"]}
    assert sources["listing"]["words"] == "read"
    assert sources["catalog"]["words"].startswith("not readable with this key (403)")
    assert provider["snapshot"]["platform_cap"] == 0.5
    assert provider["snapshot"]["public_market"] is True
    assert provider["notes"]["cc"].endswith("<b>not markup</b>")
    by_name = {profile["name"]: profile for profile in page["profiles"]}
    cap = by_name["cap20"]
    assert cap["rows"] == [{"scope": "provider", "key": "inferhub"}]
    opus = next(m for m in cap["would_match"] if m["model_id"] == OPUS)
    assert opus["state"] == "matches"
    assert opus["ceiling"] == [1.0, 5.0]
    assert (opus["accounts"], opus["of_accounts"]) == (48, 51)
    # ASTRA has its own row naming "tight"; the provider default does not reach it.
    assert [m["model_id"] for m in cap["would_match"]] == [OPUS]
    tight = by_name["tight"]["would_match"][0]
    assert tight["state"] == "unknown"  # no levels for ASTRA, only a min
    assert by_name["follow"]["follows_market"] is True
    assert by_name["follow"]["rows"] == []
    assert (
        page["store"]["digest"] == hashlib.sha256(_store(home).read_bytes()).hexdigest()
    )


def test_would_match_names_a_ceiling_under_the_market_as_matching_nothing(home):
    _snapshot()
    profile, _ = parse_profile("low", {"kind": "max_price", "max_input_usd": 0.1})
    assert profile is not None
    snapshot = current_market_snapshot(INFERHUB_MARKETPLACE)
    entry = would_match(INFERHUB_MARKETPLACE, profile, OPUS, snapshot)
    assert entry["state"] == "nothing"
    assert entry["text"].startswith("matches nothing: No InferHub offer is under")
    decides, _ = parse_profile("free", {"kind": "inferhub_decides"})
    assert decides is not None
    assert (
        would_match(INFERHUB_MARKETPLACE, decides, OPUS, snapshot)["state"] == "decides"
    )


# ------------------------------------------------------ the profile store


def test_a_profile_saved_from_the_page_is_validated_and_written_by_the_store(home):
    client = _client(_app())
    digest = client.get(PAGE).json()["store"]["digest"]
    assert digest == ""
    answer = client.put(
        PROFILES,
        json={
            "profiles": {"opus-cap": {"kind": "pct_of_official", "percent": 10}},
            "expected_digest": digest,
        },
    )
    assert answer.status_code == 200, answer.text
    assert [p["name"] for p in answer.json()["profiles"]] == ["opus-cap"]
    reset_marketplace_profiles_cache()
    assert load_marketplace_profiles().get("opus-cap") is not None
    assert json.loads(_store(home).read_text(encoding="utf-8")) == {
        "version": 1,
        "profiles": {"opus-cap": {"kind": "pct_of_official", "percent": 10}},
    }


@pytest.mark.parametrize(
    ("document", "words"),
    [
        ({"kind": "pct_of_official"}, "needs percent"),
        ({"kind": "pct_of_official", "percent": 0}, "greater than zero"),
        ({"kind": "pct_of_official", "percent": 101}, "at most 100"),
        ({"kind": "max_price"}, "needs max_input_usd or max_output_usd"),
        ({"kind": "cheapest_level_min_accounts"}, "needs min_accounts"),
        ({"kind": "fastest"}, "kind must be one of"),
        (
            {"kind": "cheapest_level_now", "cap_percent": 10, "cap_input_usd": 1},
            "not both",
        ),
    ],
)
def test_an_invalid_profile_is_refused_with_the_stores_own_words(home, document, words):
    response = _client(_app()).put(PROFILES, json={"profiles": {"p1": document}})
    assert response.status_code == 422
    assert words in response.json()["detail"]
    assert not _store(home).exists()


def test_a_store_changed_since_the_page_read_it_is_not_overwritten(home):
    _profiles(old={"kind": "pct_of_official", "percent": 10})
    before = _store(home).read_bytes()
    response = _client(_app()).put(
        PROFILES,
        json={
            "profiles": {"new": {"kind": "pct_of_official", "percent": 5}},
            "expected_digest": "0" * 64,
        },
    )
    assert response.status_code == 409
    assert _store(home).read_bytes() == before


def test_an_unreadable_store_is_never_overwritten(home):
    _store(home).parent.mkdir(parents=True, exist_ok=True)
    _store(home).write_bytes(b"{not json")
    client = _client(_app())
    page = client.get(PAGE).json()
    assert page["store"]["unreadable"] == "the file is not valid JSON"
    response = client.put(
        PROFILES,
        json={
            "profiles": {"p1": {"kind": "pct_of_official", "percent": 5}},
            "expected_digest": page["store"]["digest"],
        },
    )
    assert response.status_code == 409
    assert _store(home).read_bytes() == b"{not json"


def test_entries_mcc_cannot_use_are_dropped_only_when_confirmed(home):
    _store(home).parent.mkdir(parents=True, exist_ok=True)
    _store(home).write_text(
        json.dumps(
            {
                "version": 1,
                "profiles": {
                    "good": {"kind": "pct_of_official", "percent": 10},
                    "broken": {"kind": "pct_of_official"},
                },
            }
        ),
        encoding="utf-8",
    )
    client = _client(_app())
    page = client.get(PAGE).json()
    assert [entry["name"] for entry in page["store"]["unusable"]] == ["broken"]
    body = {
        "profiles": {"good": {"kind": "pct_of_official", "percent": 12}},
        "expected_digest": page["store"]["digest"],
    }
    refused = client.put(PROFILES, json=body)
    assert refused.status_code == 409
    assert "broken" in refused.json()["detail"]
    assert "broken" in _store(home).read_text(encoding="utf-8")
    saved = client.put(PROFILES, json={**body, "drop_unusable": True})
    assert saved.status_code == 200
    assert "broken" not in _store(home).read_text(encoding="utf-8")


def test_removing_a_profile_a_row_still_names_needs_the_operators_yes(home):
    _profiles(cap={"kind": "pct_of_official", "percent": 10})
    client = _client(_app())
    assert (
        client.post(
            OVERRIDES,
            json={
                "scope": "provider",
                "key": "inferhub",
                "updates": {"marketplace_profile": "cap"},
            },
        ).status_code
        == 200
    )
    digest = client.get(PAGE).json()["store"]["digest"]
    body = {"profiles": {}, "expected_digest": digest}
    refused = client.put(PROFILES, json=body)
    assert refused.status_code == 409
    assert "provider inferhub" in refused.json()["detail"]
    assert (
        client.put(PROFILES, json={**body, "confirm_removal": True}).status_code == 200
    )
    reset_marketplace_profiles_cache()
    assert load_marketplace_profiles().profiles == {}


# ------------------------------------------- assignments: one write path, E6


def test_the_pages_inherit_save_leaves_the_override_file_byte_identical(home):
    """What saveMarketplaceAssignment() posts for an untouched row: only the
    15th key, as "inherit", one row per call -- through the Models route."""

    seed = {
        "providers": {"inferhub": {"top_k": 40}, "nvidia_nim": {"temperature": 0.2}},
        "models": {f"inferhub/{OPUS}": {"max_output_tokens": 65000}},
    }
    _overrides(home).parent.mkdir(parents=True, exist_ok=True)
    _overrides(home).write_bytes((json.dumps(seed, indent=2) + "\n").encode())
    reset_model_overrides_cache()
    before = hashlib.sha256(_overrides(home).read_bytes()).hexdigest()
    client = _client(_app())
    for scope, key in (("provider", "inferhub"), ("model", f"inferhub/{OPUS}")):
        response = client.post(
            OVERRIDES,
            json={
                "scope": scope,
                "key": key,
                "updates": {"marketplace_profile": "inherit"},
            },
        )
        assert response.status_code == 200
        assert hashlib.sha256(_overrides(home).read_bytes()).hexdigest() == before


def test_the_page_routes_never_write_the_override_file(home):
    _snapshot()
    _profiles(cap={"kind": "pct_of_official", "percent": 10})
    client = _client(_app())
    client.get(PAGE)
    digest = client.get(PAGE).json()["store"]["digest"]
    client.put(
        PROFILES,
        json={
            "profiles": {"cap": {"kind": "pct_of_official", "percent": 11}},
            "expected_digest": digest,
        },
    )
    client.get(f"{PAGE}/inferhub/offers", params={"model": OPUS})
    client.get(f"{PAGE}/inferhub/recent")
    assert not _overrides(home).exists()


# ------------------------------------------------------- the offer explorer


def test_the_offers_carry_the_ladder_and_the_ceiling_it_would_be_sent(home):
    _snapshot()
    _profiles(cap20={"kind": "pct_of_official", "percent": 20})
    client = _client(_app())
    client.post(
        OVERRIDES,
        json={
            "scope": "provider",
            "key": "inferhub",
            "updates": {"marketplace_profile": "cap20"},
        },
    )
    offers = client.get(f"{PAGE}/inferhub/offers", params={"model": OPUS}).json()
    assert offers["levels"] == [
        {"price_in": 0.75, "price_out": 3.75, "accounts": 38},
        {"price_in": 1.0, "price_out": 5.0, "accounts": 10},
        {"price_in": 2.5, "price_out": 12.5, "accounts": 3},
    ]
    assert offers["official"] == [5.0, 25.0]
    assert offers["cap_prices"] == [2.5, 12.5]
    assert offers["perf"] == {"success_pct": 99.1, "ttft_ms": 812.0, "tps": 61.5}
    assert offers["cache_hit"] == 0.62
    assert offers["note"].startswith("A one-line Claude Code")
    assert offers["combos"] == [{"id": "combo/fast", "selection": "fastest"}]
    assert offers["aliases"]["state"] == "not_read"
    assert offers["assignment"] == {"profile": "cap20", "origin": "provider_override"}
    assert offers["ceiling"]["ceiling"] == [1.0, 5.0]
    unassigned = client.get(
        f"{PAGE}/inferhub/offers", params={"model": "zz/none"}
    ).json()
    assert unassigned["known"] is False
    assert unassigned["levels"] == []
    # The provider row's 20 % needs an official price this model has none of.
    assert unassigned["ceiling"]["state"] == "nothing"
    assert (
        client.get(f"{PAGE}/nvidia_nim/offers", params={"model": OPUS}).status_code
        == 404
    )


def test_offers_moved_since_compares_two_snapshots_the_page_saw(home):
    first = market_snapshot_from_payload(SNAPSHOT)
    assert note_snapshot(INFERHUB_MARKETPLACE, first) is None
    moved_payload = json.loads(json.dumps(SNAPSHOT))
    moved_payload["fetched_at"] = "2026-10-24T02:07:35Z"
    moved_payload["models"][OPUS]["levels_in"] = [[0.9, 30], [2.5, 3]]
    moved = note_snapshot(
        INFERHUB_MARKETPLACE, market_snapshot_from_payload(moved_payload)
    )
    assert moved == {
        "since": "2026-10-24 01:07Z",
        "until": "2026-10-24 02:07Z",
        "models": {OPUS: {"before": [0.75, 3.75], "after": [0.9, 3.75]}},
    }


# --------------------------------------------------------- the last N rows


def _request(request_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": request_id,
        "ts_epoch": T0,
        "ts_iso": "2026-10-24T01:20:00Z",
        "resolved_model": f"inferhub/{OPUS}",
        "status": "success",
        "tokens_in": 1000,
        "tokens_out": 200,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        **extra,
    }


def _attempt(request_id: str, marketplace: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "attempt": 1,
        "attempt_provider": "inferhub",
        "outcome": "succeeded",
        "_attempt_params": {"marketplace": marketplace},
    }


def test_the_recent_rows_flag_a_charge_above_the_ceiling_and_keep_null_as_none():
    ceiling = {
        "profile": "cap",
        "kind": "max_price",
        "sent": {"x-max-input-price": "1", "x-max-output-price": "5"},
    }
    headers = {
        "x-inferhub-model": OPUS,
        "x-inferhub-publisher": "6b652a8e5328",
        "x-inferhub-request-id": "08a9befdcacfb82b",
        "x-inferhub-attempt": "1",
    }
    rows = recent_rows(
        "inferhub",
        [_request("over"), _request("within"), _request("silent"), _request("bare")],
        [
            # bound = (1000 * 1 + 200 * 5) / 1e6 = 0.002
            _attempt(
                "over", {"ceiling": ceiling, "headers": headers, "charged_usd": 0.0031}
            ),
            _attempt(
                "within",
                {"ceiling": ceiling, "headers": headers, "charged_usd": 0.0015},
            ),
            _attempt("silent", {"ceiling": ceiling}),
            _attempt("bare", {"headers": headers, "charged_usd": 0.0009}),
        ],
        "_attempt_params",
    )
    by_id = {row["request_id"]: row for row in rows}
    assert by_id["over"]["charged_vs_ceiling"] == "over"
    assert by_id["over"]["bound_usd"] == pytest.approx(0.002)
    assert by_id["over"]["inferhub_publisher"] == "6b652a8e5328"
    assert by_id["within"]["charged_vs_ceiling"] == "within"
    # Not reported is None, never a zero.
    assert by_id["silent"]["charged_usd"] is None
    assert by_id["silent"]["charged_vs_ceiling"] is None
    assert by_id["silent"]["inferhub_model"] is None
    # No ceiling sent: nothing to compare with.
    assert by_id["bare"]["ceiling"] is None
    assert by_id["bare"]["charged_vs_ceiling"] is None


def test_a_tiny_request_paying_the_minimum_charge_is_not_over_its_ceiling():
    ceiling = {"sent": {"x-max-input-price": "1", "x-max-output-price": "5"}}
    # 1 token in, 1 out: $0.000006 at the ceiling, under the $0.00001 minimum.
    rows = recent_rows(
        "inferhub",
        [
            _request("tiny", tokens_in=1, tokens_out=1),
            _request("big", tokens_in=1, tokens_out=1),
        ],
        [
            _attempt("tiny", {"ceiling": ceiling, "charged_usd": 0.00001}),
            _attempt("big", {"ceiling": ceiling, "charged_usd": 0.00002}),
        ],
        "_attempt_params",
    )
    by_id = {row["request_id"]: row for row in rows}
    assert by_id["tiny"]["bound_usd"] == pytest.approx(0.00001)
    assert by_id["tiny"]["charged_vs_ceiling"] == "within"
    assert by_id["big"]["charged_vs_ceiling"] == "over"


def test_the_recent_route_is_bounded_and_reads_the_log_off_the_loop(home, monkeypatch):
    asked: dict[str, Any] = {}

    class _Store:
        def list_requests_page(self, **kwargs):
            asked["page"] = kwargs
            return [_request("r1")], None, False

        def iter_export_attempt_rows(self, **kwargs):
            asked["attempts"] = kwargs
            yield _attempt("r1", {"charged_usd": 0.001})

    monkeypatch.setattr(
        admin_marketplace_routes, "store_from_settings", lambda _s: _Store()
    )
    answer = (
        _client(_app()).get(f"{PAGE}/inferhub/recent", params={"limit": 5000}).json()
    )
    assert answer["limit"] == 100
    assert asked["page"]["limit"] == 100
    assert asked["page"]["provider"] == "inferhub"
    assert asked["attempts"]["since"] == asked["attempts"]["until"] == T0
    assert answer["rows"][0]["charged_usd"] == 0.001


# ------------------------------------------------------ the charged ask (Q17)


def test_the_charged_ask_matches_by_request_id_then_by_time_and_model():
    rows = [
        {
            "request_id": "a",
            "ts_epoch": T0,
            "inferhub_request_id": "uuid-a",
            "model": f"inferhub/{OPUS}",
            "inferhub_model": OPUS,
            "tokens_out": 200,
        },
        {
            "request_id": "b",
            # 101 s later; the log row says 01:21:41 -- 0 s apart.
            "ts_epoch": T0 + 101,
            "inferhub_request_id": None,
            "model": f"inferhub/{OPUS}",
            "inferhub_model": None,
            "tokens_out": 50,
        },
        {
            "request_id": "c",
            "ts_epoch": T0 + 9999,
            "inferhub_request_id": None,
            "model": f"inferhub/{OPUS}",
            "inferhub_model": None,
            "tokens_out": 50,
        },
    ]
    logs = [
        {
            "id": "uuid-a",
            "ts": "2026-10-24T01:20:00Z",
            "model": OPUS,
            "completion_tokens": 200,
            "ask_input_per_mtok": "0.750000",
            "ask_output_per_mtok": "3.750000",
            "cost_consumer_usdc": "0.001500",
        },
        {
            "id": "uuid-b",
            "ts": "2026-10-24T01:21:41Z",
            "model": OPUS,
            "completion_tokens": 50,
            "ask_input_per_mtok": "1.000000",
            "ask_output_per_mtok": "<b>5</b>",
            "cost_consumer_usdc": "0.000300",
        },
    ]
    matched = match_charged(rows, logs)
    assert matched["a"] == {
        "found": True,
        "matched_by": "request id",
        "ask_in": "0.750000",
        "ask_out": "3.750000",
        "cost_usdc": "0.001500",
        "status": None,
    }
    assert matched["b"]["matched_by"] == "time and model"
    # A third party's non-number is never shown as one.
    assert matched["b"]["ask_out"] is None
    assert matched["c"] == {"found": False}


def test_the_charged_route_reads_only_on_the_press_and_says_when_it_may_not(
    home, monkeypatch
):
    calls: list[tuple[Any, ...]] = []

    async def reads(self, provider_id, names, params=None):
        calls.append((provider_id, names, params))
        return {
            "state": "read",
            "reads": {
                "usage_logs": {"status": "http_403", "code": "insufficient_key_scope"}
            },
            "key_kind": {"kind": "inference_only", "how": "refusal"},
        }

    class _Store:
        def list_requests_page(self, **_kwargs):
            return [_request("r1")], None, False

        def iter_export_attempt_rows(self, **_kwargs):
            yield _attempt("r1", {"charged_usd": 0.001})

    monkeypatch.setattr(ApplicationRuntime, "marketplace_reads", reads)
    monkeypatch.setattr(
        admin_marketplace_routes, "store_from_settings", lambda _s: _Store()
    )
    client = _client(_app())
    client.get(f"{PAGE}/inferhub/recent")
    assert calls == []
    answer = client.post(
        f"{PAGE}/inferhub/charged", json={"request_ids": ["r1"]}
    ).json()
    assert answer["state"] == "not_readable"
    assert "not readable with this key" in answer["words"]
    assert len(calls) == 1
    provider_id, names, params = calls[0]
    assert (provider_id, names) == ("inferhub", ("usage_logs",))
    assert params["usage_logs"]["range"] == "custom"
    bare = _client(_app(key="")).post(
        f"{PAGE}/inferhub/charged", json={"request_ids": ["r1"]}
    )
    assert bare.json()["state"] == "no_key"
    assert len(calls) == 1


# -------------------------------------------------------------- the account


def test_the_account_block_shows_what_was_read_and_never_a_zero(home, monkeypatch):
    async def reads(self, provider_id, names, params=None):
        return {
            "state": "read",
            "reads": {
                "usage": {
                    "status": "ok",
                    "body": {
                        "currency": "USDC",
                        "balance": {"amount_usdc": "1.000000"},
                        "window": {
                            "kind": "day",
                            "spend_usdc": "0.012000",
                            "requests": 3,
                        },
                        "all_time": {"spend_usdc": "0.500000", "requests": 40},
                    },
                },
                "keys": {"status": "ok"},
                "budgets": {"status": "http_403", "code": "insufficient_key_scope"},
                "budget_aliases": {"status": "http_403"},
                "budget_default": {"status": "failed:ConnectError"},
            },
            "key_kind": {"kind": "inference", "how": "key_list"},
        }

    monkeypatch.setattr(ApplicationRuntime, "marketplace_reads", reads)
    page = _client(_app()).get(PAGE, params={"account": "true"}).json()
    account = page["providers"][0]["account"]
    assert account["state"] == "read"
    assert account["usage"]["values"]["balance"] == "1.000000"
    assert account["usage"]["values"]["window_spend"] == "0.012000"
    assert account["key_kind"]["kind"] == "inference"
    assert account["budgets"]["state"] == "not_readable"
    assert account["budgets"]["words"].startswith("not readable with this key (403")
    assert account["budgets"]["rows"] == []
    assert account["aliases"]["state"] == "not_readable"
    assert account["budgets"]["default"]["state"] == "failed"
    assert "not verified on a real account" in account["unverified"]


def test_refresh_says_when_the_market_did_not_change(home, monkeypatch):
    """The store keeps its time when a read finds the same market; Refresh says so."""

    _snapshot()

    async def reads(self, provider_id, names, params=None):
        assert names[0] == "snapshot"
        return {"snapshot_built": True, "state": "read", "reads": {}, "key_kind": None}

    monkeypatch.setattr(ApplicationRuntime, "marketplace_reads", reads)
    answer = _client(_app()).post("/admin/api/marketplaces/refresh").json()
    assert answer["refresh"]["snapshot_built"] is True
    assert "nothing in the market changed" in answer["refresh"]["words"]
    assert answer["providers"][0]["snapshot"]["stamp"] == "2026-10-24 01:07Z"
