"""Tests for the models.dev metadata fallback."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest

from my_claude_code.application.model_metadata import (
    ModelReasoningCapability,
    ProviderModelInfo,
)
from my_claude_code.core import model_ids
from my_claude_code.providers.runtime import models_dev
from my_claude_code.providers.runtime.models_dev import (
    enrich_model_infos,
    enrich_provider_model_infos,
    read_models_dev_cache,
    refresh_models_dev_cache,
    write_models_dev_cache,
)

_INDEX = {
    "acme": {
        "models": {
            "acme/llama-3.3-70b": {
                "cost": {"input": 0.1, "output": 0.2},
                "limit": {"context": 131072},
            },
            "acme/small": {"cost": {"input": 0.01, "output": 0.02}},
        }
    },
    "other": {
        "models": {
            "other-org/deepseek-v3.2": {
                "cost": {"input": 0.3, "output": 0.5},
                "limit": {"context": 65536},
            }
        }
    },
}


def _write_cache(path: Path, *, age_hours: float = 0.0) -> None:
    fetched = datetime.now(UTC) - timedelta(hours=age_hours)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"fetched_at": fetched.isoformat(), "index": _INDEX}),
        encoding="utf-8",
    )
    # Freshness reads the last time the payload was *revalidated*, which a 304
    # advances by touching the file. Age the mtime with the payload, or the
    # cache looks brand new however old its ``fetched_at`` claims to be.
    aged = fetched.timestamp()
    os.utime(path, (aged, aged))


def test_cache_roundtrip_and_freshness(tmp_path: Path) -> None:
    path = tmp_path / "cache" / "models-dev.json"
    write_models_dev_cache(_INDEX, path)

    cache = read_models_dev_cache(path)

    assert cache is not None
    assert cache.fresh is True
    assert cache.index == _INDEX


def test_read_cache_missing_and_corrupt(tmp_path: Path) -> None:
    assert read_models_dev_cache(tmp_path / "nope.json") is None

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert read_models_dev_cache(corrupt) is None


def test_read_cache_stale_after_24h(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    _write_cache(path, age_hours=25)

    cache = read_models_dev_cache(path)

    assert cache is not None
    assert cache.fresh is False


def test_enrich_matches_exact_and_normalized_ids() -> None:
    infos = (
        ProviderModelInfo(model_id="acme/llama-3.3-70b"),
        # Provider-side id carries extra prefix segments: last-segment match.
        ProviderModelInfo(model_id="accounts/acme/models/llama-3.3-70b"),
        # models.dev-side prefix stripped via candidate normalization.
        ProviderModelInfo(model_id="deepseek-v3.2"),
        ProviderModelInfo(model_id="unknown-model"),
    )

    enriched = enrich_model_infos(infos, _INDEX)

    assert enriched[0].context_length == 131072
    assert enriched[0].input_price == 0.1
    assert enriched[0].output_price == 0.2
    assert enriched[1].context_length == 131072
    assert enriched[2].context_length == 65536
    assert enriched[3].context_length is None
    assert enriched[3].input_price is None


def test_enrich_preserves_existing_metadata() -> None:
    infos = (
        ProviderModelInfo(
            model_id="acme/llama-3.3-70b",
            supports_thinking=True,
            context_length=1000,
            input_price=9.9,
        ),
    )

    enriched = enrich_model_infos(infos, _INDEX)

    assert enriched[0].supports_thinking is True
    assert enriched[0].context_length == 1000
    assert enriched[0].input_price == 9.9
    assert enriched[0].output_price == 0.2


@pytest.mark.asyncio
async def test_enrich_uses_cache_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "models-dev.json"
    _write_cache(path)

    async def _boom(etag=None):
        raise AssertionError("network must not be touched")

    monkeypatch.setattr(models_dev, "fetch_models_dev_index", _boom)

    enriched = await enrich_provider_model_infos(
        [ProviderModelInfo(model_id="acme/llama-3.3-70b")], path
    )

    assert enriched[0].context_length == 131072


@pytest.mark.asyncio
async def test_enrich_without_cache_schedules_refresh_and_passes_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "models-dev.json"
    fetched: list[str] = []

    async def _fake_fetch(etag=None):
        fetched.append("hit")
        return models_dev.ModelsDevFetch(index=_INDEX)

    monkeypatch.setattr(models_dev, "fetch_models_dev_index", _fake_fetch)

    enriched = await enrich_provider_model_infos(
        [ProviderModelInfo(model_id="acme/llama-3.3-70b")], path
    )

    assert enriched[0].context_length is None
    for _ in range(100):
        await asyncio.sleep(0.01)
        if path.is_file():
            break
    assert fetched == ["hit"]
    assert path.is_file()


@pytest.mark.asyncio
async def test_refresh_is_silent_when_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "models-dev.json"

    async def _offline(etag=None):
        return None

    monkeypatch.setattr(models_dev, "fetch_models_dev_index", _offline)

    result = await refresh_models_dev_cache(path)

    assert result is False
    assert not path.exists()


@pytest.mark.asyncio
async def test_fetch_returns_none_on_httpx_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    class _FailingClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _FailingClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str, headers: object = None) -> object:
            raise httpx.ConnectError("offline")

    monkeypatch.setattr(models_dev.httpx, "AsyncClient", _FailingClient)

    assert await models_dev.fetch_models_dev_index() is None


def test_normalize_candidates() -> None:
    candidates = model_ids.normalize_candidates("Acme/Llama-3.3-70B")

    assert "acme/llama-3.3-70b" in candidates
    assert "llama-3.3-70b" in candidates


# --------------------------------------------------------------------------
# Reasoning capability lookup
# --------------------------------------------------------------------------

from my_claude_code.core.reasoning import ReasoningEffort  # noqa: E402
from my_claude_code.providers.runtime.models_dev import (  # noqa: E402
    PROVIDER_ID_ALIASES,
    model_reasoning_capability_from_models_dev,
    resolve_model_reasoning_capability,
)

_REASONING_INDEX = {
    "openrouter": {
        "models": {
            "acme/all-controls": {
                "reasoning": True,
                "reasoning_options": [
                    {"type": "toggle"},
                    {
                        "type": "effort",
                        "values": [
                            "none",
                            "minimal",
                            "low",
                            "medium",
                            "high",
                            "xhigh",
                            "max",
                            "default",
                        ],
                    },
                    {"type": "budget_tokens"},
                ],
            },
            "acme/no-reasoning": {"reasoning": False},
            "acme/malformed-options": {
                "reasoning": True,
                "reasoning_options": "not-a-list",
            },
            "acme/malformed-effort": {
                "reasoning": True,
                "reasoning_options": [{"type": "effort", "values": "not-a-list"}],
            },
        }
    },
    "anthropic": {
        "models": {
            "claude-x": {"reasoning": True, "reasoning_options": [{"type": "toggle"}]}
        }
    },
    "not-a-provider-bucket": "oops",
}


def _write_reasoning_cache(path: Path) -> None:
    write_models_dev_cache(_REASONING_INDEX, path)


def test_parses_effort_toggle_and_budget_together(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    _write_reasoning_cache(path)

    capability = model_reasoning_capability_from_models_dev(
        "open_router", "acme/all-controls", path
    )

    assert capability is not None
    assert capability.can_reason is True
    assert capability.supports_effort_control is True
    assert capability.supports_toggle_control is True
    assert capability.supports_budget_control is True


def test_effort_values_map_onto_reasoning_effort_ignoring_unknown(
    tmp_path: Path,
) -> None:
    path = tmp_path / "models-dev.json"
    _write_reasoning_cache(path)

    capability = model_reasoning_capability_from_models_dev(
        "open_router", "acme/all-controls", path
    )

    assert capability is not None
    assert capability.supported_efforts == frozenset(
        {
            ReasoningEffort.MINIMAL,
            ReasoningEffort.LOW,
            ReasoningEffort.MEDIUM,
            ReasoningEffort.HIGH,
            ReasoningEffort.XHIGH,
            ReasoningEffort.MAX,
        }
    )
    # "none" and "default" are not ReasoningEffort members and must not raise.
    assert capability.supported_efforts is not None
    assert "none" not in [effort.value for effort in capability.supported_efforts]


def test_known_not_reasoning_is_distinct_from_unknown_model(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    _write_reasoning_cache(path)

    known_false = model_reasoning_capability_from_models_dev(
        "open_router", "acme/no-reasoning", path
    )
    unknown = model_reasoning_capability_from_models_dev(
        "open_router", "acme/does-not-exist", path
    )

    assert known_false is not None
    assert known_false.can_reason is False
    assert unknown is None


@pytest.mark.parametrize(
    "provider_id,models_dev_id",
    [
        ("open_router", "openrouter"),
        ("azure_openai", "azure"),
        ("bedrock", "amazon-bedrock"),
        ("gemini", "google"),
        ("vertex", "google-vertex"),
        ("fireworks", "fireworks-ai"),
        ("together", "togetherai"),
        ("novita", "novita-ai"),
        ("cline", "cline-pass"),
        ("kimi_coding", "kimi-for-coding"),
        ("alibaba_cn", "alibaba-cn"),
        ("alibaba_coding", "alibaba-coding-plan"),
        ("alibaba_coding_cn", "alibaba-coding-plan-cn"),
        ("ollama_cloud", "ollama-cloud"),
        ("chatgpt_oauth", "openai"),
        ("anthropic_oauth", "anthropic"),
        ("github_models", "github-copilot"),
    ],
)
def test_all_declared_aliases_resolve(provider_id: str, models_dev_id: str) -> None:
    assert PROVIDER_ID_ALIASES[provider_id] == models_dev_id


def test_alias_resolution_actually_reaches_the_model(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    _write_reasoning_cache(path)

    # "open_router" is our provider id; the fixture only has "openrouter".
    capability = model_reasoning_capability_from_models_dev(
        "open_router", "acme/no-reasoning", path
    )
    # "anthropic_oauth" aliases onto "anthropic", which does exist directly
    # here too, exercising both the alias path and a same-named provider.
    aliased = model_reasoning_capability_from_models_dev(
        "anthropic_oauth", "claude-x", path
    )

    assert capability is not None
    assert aliased is not None
    assert aliased.supports_toggle_control is True


def test_provider_absent_from_index_is_unknown_not_an_error(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    _write_reasoning_cache(path)

    # "ollama" is a real project provider id with no models.dev entry at all.
    result = model_reasoning_capability_from_models_dev("ollama", "any-model", path)

    assert result is None


def test_layering_provider_reported_wins_when_known(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    _write_reasoning_cache(path)

    # models.dev says False, provider says True: provider wins for can_reason.
    resolved = resolve_model_reasoning_capability(
        "open_router",
        "acme/no-reasoning",
        ModelReasoningCapability(can_reason=True),
        path,
    )

    assert resolved is not None
    assert resolved.can_reason is True


def test_layering_falls_back_to_models_dev_when_provider_unknown(
    tmp_path: Path,
) -> None:
    path = tmp_path / "models-dev.json"
    _write_reasoning_cache(path)

    resolved = resolve_model_reasoning_capability(
        "open_router", "acme/all-controls", None, path
    )

    assert resolved is not None
    assert resolved.can_reason is True
    assert resolved.supports_budget_control is True


def test_layering_unknown_when_neither_layer_has_data(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    _write_reasoning_cache(path)

    resolved = resolve_model_reasoning_capability("ollama", "any-model", None, path)

    assert resolved is None


def test_lookup_works_with_empty_in_memory_provider_model_cache(
    tmp_path: Path,
) -> None:
    """Anti-'gate that never opens' test.

    ProviderModelCache is populated only by an admin refresh and is empty on
    a fresh server. The reasoning lookup must read the disk-cached models.dev
    index directly and must not depend on ProviderModelCache at all.
    """
    from my_claude_code.providers.runtime.model_cache import ProviderModelCache

    path = tmp_path / "models-dev.json"
    _write_reasoning_cache(path)

    empty_cache = ProviderModelCache(available_provider_ids=["open_router"])
    assert empty_cache.has_provider("open_router") is False

    capability = model_reasoning_capability_from_models_dev(
        "open_router", "acme/all-controls", path
    )

    assert capability is not None
    assert capability.can_reason is True


def test_malformed_reasoning_options_degrade_to_unknown_not_raise(
    tmp_path: Path,
) -> None:
    path = tmp_path / "models-dev.json"
    _write_reasoning_cache(path)

    not_a_list = model_reasoning_capability_from_models_dev(
        "open_router", "acme/malformed-options", path
    )
    bad_values = model_reasoning_capability_from_models_dev(
        "open_router", "acme/malformed-effort", path
    )

    assert not_a_list is not None
    assert not_a_list.can_reason is True
    assert not_a_list.supports_effort_control is None
    assert not_a_list.supports_toggle_control is None
    assert not_a_list.supports_budget_control is None

    assert bad_values is not None
    assert bad_values.supports_effort_control is True
    assert bad_values.supported_efforts == frozenset()


def test_missing_index_returns_unknown_not_raise(tmp_path: Path) -> None:
    result = model_reasoning_capability_from_models_dev(
        "open_router", "acme/all-controls", tmp_path / "nope.json"
    )

    assert result is None


def test_reasoning_mandatory_flag_is_parsed_and_silence_stays_unknown(
    tmp_path: Path,
) -> None:
    """``reasoning_mandatory`` sets the capability flag; absence means unknown.

    The index here is local to this test on purpose: no other test's fixture
    grows a key, and the parse contract (True / False / absent -> None) is
    asserted in one place. A wrong True would rewrite every OFF request for
    the model, so the absent case is asserted explicitly, not assumed.
    """

    path = tmp_path / "models-dev.json"
    write_models_dev_cache(
        {
            "openrouter": {
                "models": {
                    "acme/mandatory": {
                        "reasoning": True,
                        "reasoning_options": [{"type": "toggle"}],
                        "reasoning_mandatory": True,
                    },
                    "acme/explicitly-optional": {
                        "reasoning": True,
                        "reasoning_mandatory": False,
                    },
                    "acme/no-flag": {"reasoning": True},
                }
            }
        },
        path,
    )

    mandatory = model_reasoning_capability_from_models_dev(
        "open_router", "acme/mandatory", path
    )
    optional = model_reasoning_capability_from_models_dev(
        "open_router", "acme/explicitly-optional", path
    )
    unknown = model_reasoning_capability_from_models_dev(
        "open_router", "acme/no-flag", path
    )

    assert mandatory is not None
    assert mandatory.mandatory is True
    assert optional is not None
    assert optional.mandatory is False
    assert unknown is not None
    assert unknown.mandatory is None


def test_reasoning_index_is_memoized_until_mtime_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "models-dev.json"
    _write_reasoning_cache(path)

    calls: list[str] = []
    real_read = models_dev.read_models_dev_cache

    def _counting_read(cache_path: Path | None = None) -> object:
        calls.append("read")
        return real_read(cache_path)

    monkeypatch.setattr(models_dev, "read_models_dev_cache", _counting_read)
    models_dev._reasoning_index_cache.clear()

    model_reasoning_capability_from_models_dev("open_router", "acme/no-reasoning", path)
    model_reasoning_capability_from_models_dev("open_router", "acme/no-reasoning", path)
    model_reasoning_capability_from_models_dev("open_router", "acme/no-reasoning", path)

    assert calls == ["read"]

    _write_reasoning_cache(path)  # bumps mtime
    model_reasoning_capability_from_models_dev("open_router", "acme/no-reasoning", path)

    assert calls == ["read", "read"]


# --------------------------------------------------------------------------
# Output-token limit lookup
# --------------------------------------------------------------------------

from my_claude_code.providers.runtime.models_dev import (  # noqa: E402
    model_output_limit_from_models_dev,
)

_LIMIT_INDEX = {
    "openrouter": {
        "models": {
            "acme/limited": {"limit": {"context": 200000, "output": 64000}},
            "acme/no-limit-block": {"reasoning": True},
            "acme/context-only": {"limit": {"context": 200000}},
            "acme/zero-output": {"limit": {"output": 0}},
        }
    },
    "not-a-provider-bucket": "oops",
}


def _write_limit_cache(path: Path) -> None:
    write_models_dev_cache(_LIMIT_INDEX, path)


def test_output_limit_is_read_from_models_dev(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    _write_limit_cache(path)

    assert model_output_limit_from_models_dev("open_router", "acme/limited", path) == (
        64000
    )
    # The alias map is honored exactly as the capability lookup honors it.
    assert model_output_limit_from_models_dev("openrouter", "limited", path) == 64000


@pytest.mark.parametrize(
    "model_id",
    ["acme/no-limit-block", "acme/context-only", "acme/zero-output", "acme/absent"],
)
def test_missing_or_unusable_output_limit_is_unknown(
    tmp_path: Path, model_id: str
) -> None:
    path = tmp_path / "models-dev.json"
    _write_limit_cache(path)

    assert model_output_limit_from_models_dev("open_router", model_id, path) is None


def test_output_limit_without_a_cache_is_unknown(tmp_path: Path) -> None:
    assert (
        model_output_limit_from_models_dev(
            "open_router", "acme/limited", tmp_path / "nope.json"
        )
        is None
    )


def test_output_limit_index_is_memoized_until_mtime_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "models-dev.json"
    _write_limit_cache(path)

    calls: list[str] = []
    real_read = models_dev.read_models_dev_cache

    def _counting_read(cache_path: Path | None = None) -> object:
        calls.append("read")
        return real_read(cache_path)

    monkeypatch.setattr(models_dev, "read_models_dev_cache", _counting_read)
    models_dev._output_limit_index_cache.clear()

    for _ in range(3):
        model_output_limit_from_models_dev("open_router", "acme/limited", path)

    assert calls == ["read"]


# --------------------------------------------------------------------------
# Provider alias coverage, drift guard, and tag-stripped model matching
# --------------------------------------------------------------------------

from my_claude_code.config.provider_catalog import PROVIDER_CATALOG  # noqa: E402

_MATCHING_INDEX = {
    "opencode-go": {"models": {"opencode-go/gpt-5": {"reasoning": True}}},
    "wafer.ai": {"models": {"wafer-1": {"reasoning": True}}},
    "moonshotai": {"models": {"kimi-k3": {"reasoning": True}}},
    "cloudflare-workers-ai": {"models": {"@cf/nvidia/nemotron": {"reasoning": True}}},
    # A "llama" provider exists here on purpose: our llamacpp provider must
    # still resolve to None (rejected pairing, see PROVIDER_ID_ALIASES).
    "llama": {"models": {"llama-4": {"reasoning": True}}},
    "openrouter": {
        "models": {
            "deepseek/deepseek-r1:free": {"reasoning": False},
            "vendor/tagged-only:free": {"reasoning": True},
            "vendor/both": {
                "reasoning": True,
                "reasoning_options": [{"type": "toggle"}],
            },
            "vendor/both:free": {"reasoning": False},
            "vendor/thinker": {"reasoning": False},
            "vendor/numeric": {"reasoning": False},
        }
    },
}


def _write_matching_cache(path: Path) -> None:
    write_models_dev_cache(_MATCHING_INDEX, path)


@pytest.mark.parametrize(
    ("provider_id", "model_id"),
    [
        ("opencode_go", "opencode-go/gpt-5"),
        ("wafer", "wafer-1"),
        ("kimi", "kimi-k3"),
        ("cloudflare", "@cf/nvidia/nemotron"),
    ],
)
def test_new_aliases_reach_their_models_dev_bucket(
    tmp_path: Path, provider_id: str, model_id: str
) -> None:
    path = tmp_path / "models-dev.json"
    _write_matching_cache(path)

    capability = model_reasoning_capability_from_models_dev(provider_id, model_id, path)

    assert capability is not None
    assert capability.can_reason is True


def test_every_alias_key_is_a_real_provider_id() -> None:
    unknown = sorted(set(PROVIDER_ID_ALIASES) - set(PROVIDER_CATALOG))

    assert unknown == []


def test_no_alias_maps_a_provider_id_onto_itself() -> None:
    self_mapped = sorted(
        key for key, value in PROVIDER_ID_ALIASES.items() if key == value
    )

    assert self_mapped == []


def test_alias_map_has_no_duplicate_keys() -> None:
    source = Path(models_dev.__file__).read_text(encoding="utf-8")
    block = source.split("PROVIDER_ID_ALIASES: dict[str, str] = {", 1)[1]
    block = block.split("\n}", 1)[0]
    keys = [
        line.split('"')[1]
        for line in block.splitlines()
        if line.strip().startswith('"')
    ]

    assert sorted(keys) == sorted(set(keys))


def test_free_tag_falls_back_to_the_untagged_entry(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    _write_matching_cache(path)

    tagged = model_reasoning_capability_from_models_dev(
        "open_router", "vendor/thinker:free", path
    )

    # "vendor/thinker:free" is not listed; the tag is stripped and it resolves
    # to the untagged "vendor/thinker" entry in the same provider bucket.
    assert tagged is not None
    assert tagged.can_reason is False


def test_exact_match_wins_over_tag_stripped_match(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    _write_matching_cache(path)

    exact = model_reasoning_capability_from_models_dev(
        "open_router", "vendor/both:free", path
    )

    assert exact is not None
    assert exact.can_reason is False
    # The untagged "vendor/both" carries a toggle; the exact ":free" row does
    # not, so getting the toggle here would mean the wrong row was returned.
    assert exact.supports_toggle_control is not True


@pytest.mark.parametrize(
    "tag", ["free", "nitro", "floor", "online", "extended", "discounted"]
)
def test_every_allow_listed_tag_strips(tmp_path: Path, tag: str) -> None:
    """Pricing/routing/capability tags never change thinking, so they strip."""
    path = tmp_path / "models-dev.json"
    _write_matching_cache(path)

    capability = model_reasoning_capability_from_models_dev(
        "open_router", f"vendor/thinker:{tag}", path
    )

    assert capability is not None, f"':{tag}' should have been stripped"
    assert capability.can_reason is False


@pytest.mark.parametrize(
    "tag", ["thinking", "32000", "1024", "low", "medium", "high", "max"]
)
def test_reasoning_tags_are_never_stripped(tmp_path: Path, tag: str) -> None:
    """For these the tag IS the reasoning difference; stay unknown instead."""
    path = tmp_path / "models-dev.json"
    _write_matching_cache(path)

    assert (
        model_reasoning_capability_from_models_dev(
            "open_router", f"vendor/thinker:{tag}", path
        )
        is None
    ), f"':{tag}' must never be stripped"


def test_thinking_and_numeric_tags_are_not_stripped(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    _write_matching_cache(path)

    thinking = model_reasoning_capability_from_models_dev(
        "open_router", "vendor/thinker:thinking", path
    )
    numeric = model_reasoning_capability_from_models_dev(
        "open_router", "vendor/numeric:32000", path
    )

    assert thinking is None
    assert numeric is None


def test_rejected_llamacpp_alias_is_still_not_installed(tmp_path: Path) -> None:
    """The alias stays rejected; only the approximate tier may answer.

    Before cross-provider matching existed, "no alias" and "no answer" were the
    same thing. They no longer are: llamacpp has no models.dev bucket, so a
    same-named row elsewhere now supplies an explicitly approximate answer.
    What must not happen is llamacpp being *aliased* onto models.dev's "llama"
    bucket, which would make another product's catalogue authoritative for a
    local server.
    """
    path = tmp_path / "models-dev.json"
    _write_matching_cache(path)

    assert "llamacpp" not in PROVIDER_ID_ALIASES
    # A name that exists nowhere stays unknown even through the new tier.
    assert (
        model_reasoning_capability_from_models_dev("llamacpp", "not-a-model", path)
        is None
    )


# --------------------------------------------------------------------------
# Reverse tag matching: an untagged query against a tagged-only index entry
# --------------------------------------------------------------------------

_REVERSE_INDEX = {
    "openrouter": {
        "models": {
            # Exactly one tagged variant: usable.
            "vendor/one:free": {"reasoning": True},
            # Two tagged variants: ambiguous, so unknown.
            "vendor/two:free": {"reasoning": True},
            "vendor/two:nitro": {"reasoning": False},
            # An untagged row must always win over its tagged sibling.
            "vendor/exact": {
                "reasoning": True,
                "reasoning_options": [{"type": "toggle"}],
            },
            "vendor/exact:free": {"reasoning": False},
            # ":thinking" is not allow-listed in either direction.
            "vendor/thinky:thinking": {"reasoning": True},
        }
    },
    # Same model id, different provider bucket: never reachable from
    # "open_router".
    "anthropic": {"models": {"vendor/elsewhere:free": {"reasoning": True}}},
}


def _write_reverse_cache(path: Path) -> None:
    write_models_dev_cache(_REVERSE_INDEX, path)


def test_untagged_query_finds_a_single_tagged_variant(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    _write_reverse_cache(path)

    capability = model_reasoning_capability_from_models_dev(
        "open_router", "vendor/one", path
    )

    assert capability is not None
    assert capability.can_reason is True


def test_untagged_query_with_two_tagged_variants_stays_unknown(
    tmp_path: Path,
) -> None:
    """Picking one would assert a variant the user never wrote."""
    path = tmp_path / "models-dev.json"
    _write_reverse_cache(path)

    assert (
        model_reasoning_capability_from_models_dev("open_router", "vendor/two", path)
        is None
    )


def test_exact_untagged_entry_wins_over_its_tagged_variant(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    _write_reverse_cache(path)

    capability = model_reasoning_capability_from_models_dev(
        "open_router", "vendor/exact", path
    )

    assert capability is not None
    # Only the untagged row carries a toggle; the ":free" row does not.
    assert capability.supports_toggle_control is True


def test_reverse_tag_match_never_crosses_provider_buckets(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    _write_reverse_cache(path)

    assert (
        model_reasoning_capability_from_models_dev(
            "open_router", "vendor/elsewhere", path
        )
        is None
    )


def test_reverse_tag_match_ignores_tags_outside_the_allow_list(
    tmp_path: Path,
) -> None:
    path = tmp_path / "models-dev.json"
    _write_reverse_cache(path)

    assert (
        model_reasoning_capability_from_models_dev("open_router", "vendor/thinky", path)
        is None
    )


# ------------------------------------------------- provider-scoped matching --
#
# The flat index is every provider's models in one namespace, first writer
# wins, and the winner is whichever order models.dev serialises its JSON. On
# the live 2026-08 snapshot 3,757 model ids were claimed by more than one
# provider and 234 of those disagreed about whether the model reads images.


def _two_providers_claiming_one_name() -> dict[str, object]:
    """Same model id, opposite vision answers, different context lengths."""
    return {
        "first-alphabetically": {
            "models": {
                "shared-name": {
                    "modalities": {"input": ["text"]},
                    "limit": {"context": 8_000},
                }
            }
        },
        "groq": {
            "models": {
                "shared-name": {
                    "modalities": {"input": ["text", "image"]},
                    "limit": {"context": 128_000},
                }
            }
        },
    }


def test_a_models_own_provider_decides_what_it_can_do():
    """A namesake hosted elsewhere must not answer for this model.

    Without scoping this model is told it cannot read images because another
    provider happens to publish something with the same name earlier in the
    file -- and vision routing then diverts a request that never needed it.
    """
    index = _two_providers_claiming_one_name()
    infos = (ProviderModelInfo(model_id="shared-name"),)

    (scoped,) = enrich_model_infos(infos, index, "groq")
    assert scoped.supports_vision is True
    assert scoped.context_length == 128_000

    # The other provider's own answer is equally its own.
    (other,) = enrich_model_infos(infos, index, "first-alphabetically")
    assert other.supports_vision is False
    assert other.context_length == 8_000


def test_a_provider_models_dev_does_not_know_still_gets_the_flat_index():
    """Scoping is a preference, not a restriction.

    Most gateways resell models they do not publish metadata for, so falling
    back to the cross-provider index is what keeps them described at all.
    """
    index = _two_providers_claiming_one_name()
    (info,) = enrich_model_infos(
        (ProviderModelInfo(model_id="shared-name"),), index, "some-gateway"
    )
    assert info.supports_vision is not None
    assert info.context_length in {8_000, 128_000}


def test_a_sparse_provider_entry_does_not_strip_what_the_index_knows():
    """Merged field by field, not chosen wholesale.

    A provider that lists a model but says little about it would otherwise
    remove fields the flat index could still supply. Measured against the live
    index, choosing wholesale cost 11 models metadata they have today.
    """
    index = {
        "groq": {
            "models": {"shared-name": {"modalities": {"input": ["text", "image"]}}}
        },
        "elsewhere": {"models": {"shared-name": {"limit": {"context": 64_000}}}},
    }
    (info,) = enrich_model_infos(
        (ProviderModelInfo(model_id="shared-name"),), index, "groq"
    )

    # Its own provider is believed about vision...
    assert info.supports_vision is True
    # ...and the gap it left is still filled from elsewhere.
    assert info.context_length == 64_000


def test_the_provider_alias_map_is_used_for_scoping_too():
    """MCC's provider ids and models.dev's do not always agree.

    Another provider claims the same name and answers differently, and is
    reached first by the flat fallback -- so the alias is the only thing that
    can produce the right answer here.
    """
    index = {
        "aaa-other": {"models": {"a-model": {"modalities": {"input": ["text"]}}}},
        "openrouter": {
            "models": {"a-model": {"modalities": {"input": ["text", "image"]}}}
        },
    }
    # Without the alias, "open_router" finds no bucket and the flat fallback
    # answers with aaa-other's False.
    (info,) = enrich_model_infos(
        (ProviderModelInfo(model_id="a-model"),), index, "open_router"
    )
    assert info.supports_vision is True


# --------------------------------------------------------------------------
# Gateway-first, field-by-field merge
# --------------------------------------------------------------------------

_MERGE_INDEX = {
    "openrouter": {
        "models": {
            "vendor/merged": {
                "reasoning": True,
                "reasoning_options": [
                    {"type": "effort", "values": ["low", "high"]},
                ],
            }
        }
    }
}


def test_merge_keeps_both_sources_fields(tmp_path: Path) -> None:
    """The bug this PR fixes: rebuilding by source dropped ``mandatory``.

    The gateway is the only source that publishes ``reasoning.mandatory``, and
    models.dev is the only one publishing an effort vocabulary here. A merge
    that picks a winning source keeps one and silently loses the other, which
    is exactly why the v5.60.0 mandatory-model handling never ran.
    """
    path = tmp_path / "models-dev.json"
    write_models_dev_cache(_MERGE_INDEX, path)

    resolved = resolve_model_reasoning_capability(
        "open_router",
        "vendor/merged",
        ModelReasoningCapability(can_reason=True, mandatory=True),
        path,
    )

    assert resolved is not None
    assert resolved.mandatory is True
    assert resolved.supported_efforts == frozenset(
        {ReasoningEffort.LOW, ReasoningEffort.HIGH}
    )
    assert resolved.supports_effort_control is True


def test_merge_prefers_the_gateway_field_by_field(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    write_models_dev_cache(_MERGE_INDEX, path)

    resolved = resolve_model_reasoning_capability(
        "open_router",
        "vendor/merged",
        ModelReasoningCapability(
            can_reason=True, supported_efforts=frozenset({ReasoningEffort.MAX})
        ),
        path,
    )

    assert resolved is not None
    # The gateway's vocabulary wins; models.dev still supplies effort control.
    assert resolved.supported_efforts == frozenset({ReasoningEffort.MAX})
    assert resolved.supports_effort_control is True


def test_merge_keeps_a_known_false_rather_than_deferring(tmp_path: Path) -> None:
    """``False`` is an answer; only ``None`` defers to the other source."""
    merged = models_dev.merge_reasoning_capabilities(
        ModelReasoningCapability(supports_toggle_control=False),
        ModelReasoningCapability(supports_toggle_control=True),
    )

    assert merged is not None
    assert merged.supports_toggle_control is False


def test_merge_of_two_unknowns_is_unknown() -> None:
    assert models_dev.merge_reasoning_capabilities(None, None) is None


# --------------------------------------------------------------------------
# reasoning_options: [] -- known, but with no caller control
# --------------------------------------------------------------------------

_CONTROL_INDEX = {
    "nvidia": {
        "models": {
            # models.dev's own schema forbids omitting reasoning_options when
            # reasoning is true, so [] is a deliberate value meaning "reasoning
            # is on and you cannot steer it". 1,223 of 5,230 reasoning models
            # (23%) carry it.
            "thinkingmachines/inkling": {"reasoning": True, "reasoning_options": []},
            "minimaxai/minimax-m3": {
                "reasoning": True,
                "reasoning_options": [{"type": "toggle"}],
            },
        }
    }
}


def test_empty_reasoning_options_is_known_no_control_not_unknown(
    tmp_path: Path,
) -> None:
    path = tmp_path / "models-dev.json"
    write_models_dev_cache(_CONTROL_INDEX, path)

    capability = model_reasoning_capability_from_models_dev(
        "nvidia_nim", "thinkingmachines/inkling", path
    )

    assert capability is not None
    assert capability.can_reason is True
    assert capability.supports_effort_control is False
    assert capability.supports_toggle_control is False
    assert capability.supports_budget_control is False


def test_absent_models_dev_row_is_unknown_everywhere(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    write_models_dev_cache(_CONTROL_INDEX, path)

    # "nvidia" HAS a bucket, so the approximate tier is never consulted and an
    # unlisted model stays entirely unknown.
    assert (
        model_reasoning_capability_from_models_dev("nvidia_nim", "not-listed", path)
        is None
    )


def test_toggle_only_model_still_parses_as_it_did(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    write_models_dev_cache(_CONTROL_INDEX, path)

    capability = model_reasoning_capability_from_models_dev(
        "nvidia_nim", "minimaxai/minimax-m3", path
    )

    assert capability is not None
    assert capability.supports_toggle_control is True
    assert capability.supports_effort_control is False
    assert capability.supported_efforts is None


# --------------------------------------------------------------------------
# limit.output / limit.context == 0 is a miss, never a ceiling
# --------------------------------------------------------------------------

_ZERO_INDEX = {
    "openrouter": {
        "models": {
            "vendor/zeroed": {
                "limit": {"context": 0, "output": 0},
                "cost": {"input": 0.1, "output": 0.2},
            }
        }
    }
}


def test_zero_output_limit_reads_as_unknown(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    write_models_dev_cache(_ZERO_INDEX, path)

    assert model_output_limit_from_models_dev("open_router", "vendor/zeroed", path) is (
        None
    )


def test_zero_context_limit_never_enriches_a_model() -> None:
    """195 models publish output 0 and 132 publish context 0 on the live feed."""
    enriched = enrich_model_infos(
        (ProviderModelInfo(model_id="vendor/zeroed"),), _ZERO_INDEX
    )

    assert enriched[0].context_length is None
    # The rest of the row is still perfectly usable.
    assert enriched[0].input_price == 0.1


# --------------------------------------------------------------------------
# Cross-provider fallback for a provider models.dev does not describe
# --------------------------------------------------------------------------

_CROSS_INDEX = {
    "alpha": {
        "models": {
            "tencent/hy3": {
                "reasoning": True,
                "reasoning_options": [{"type": "effort", "values": ["low", "high"]}],
                "limit": {"output": 64000},
            }
        }
    },
    "beta": {
        "models": {
            "tencent/hy3": {
                "reasoning": True,
                "reasoning_options": [{"type": "effort", "values": ["low", "high"]}],
                "limit": {"output": 64000},
            }
        }
    },
    "gamma": {
        "models": {
            "tencent/hy3": {
                "reasoning": True,
                "reasoning_options": [{"type": "toggle"}],
                "limit": {"output": 262144},
            }
        }
    },
    # Publishes a vocabulary but no limit, so the two votes have deliberately
    # different denominators: four rows match the name, three report a limit,
    # three report a vocabulary.
    "delta": {
        "models": {
            "tencent/hy3": {
                "reasoning": True,
                "reasoning_options": [{"type": "effort", "values": ["low", "high"]}],
            }
        }
    },
}


def test_cross_provider_uses_the_modal_value_not_the_min_or_max(
    tmp_path: Path,
) -> None:
    path = tmp_path / "models-dev.json"
    write_models_dev_cache(_CROSS_INDEX, path)

    limit = model_output_limit_from_models_dev("nous_portal", "tencent/hy3:free", path)

    # min would be 64000 too, so pin the mode explicitly instead: two rows say
    # 64000 and one says 262144, and the answer is the value two agreed on.
    match = models_dev.cross_provider_match("nous_portal", "tencent/hy3:free", path)
    assert match is not None
    assert match.match_count == 4
    # Three of the four matched rows publish a limit; the ratio is over those.
    assert match.output_reporters == 3
    assert match.output_agreement == pytest.approx(2 / 3)
    assert limit == 64000


def test_cross_provider_votes_each_capability_field(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    write_models_dev_cache(_CROSS_INDEX, path)

    capability = model_reasoning_capability_from_models_dev(
        "nous_portal", "tencent/hy3:free", path
    )

    assert capability is not None
    assert capability.can_reason is True
    # Two effort rows against one toggle row: the modal vocabulary wins rather
    # than the intersection (which collapses) or the union (which invents).
    assert capability.supported_efforts == frozenset(
        {ReasoningEffort.LOW, ReasoningEffort.HIGH}
    )
    assert capability.supports_effort_control is True


def test_cross_provider_resolution_is_logged_as_approximate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An approximate answer must announce itself, with its evidence.

    The module logger is stubbed rather than a loguru sink added: global sink
    and level state is shared across the whole suite, so a sink-based
    assertion passes or fails depending on what ran before it.
    """
    path = tmp_path / "models-dev.json"
    write_models_dev_cache(_CROSS_INDEX, path)
    records: list[str] = []

    class _RecordingLogger:
        def info(self, template: str, *args: object) -> None:
            records.append(template.format(*args))

        def debug(self, template: str, *args: object) -> None:
            return None

    monkeypatch.setattr(models_dev, "logger", _RecordingLogger())

    models_dev.cross_provider_match("nous_portal", "tencent/hy3:free", path)

    assert len(records) == 1
    line = records[0]
    assert "APPROXIMATE" in line
    assert "no bucket for nous_portal" in line
    # The rung that answered, by number and by name.
    assert "tier 8 (cross_provider_tag_stripped)" in line
    assert "across 4 rows" in line
    # The real agreement, over the rows that actually reported a limit --
    # never "100%" off a single sample.
    assert "67% agreement across 3 reporting rows" in line
    assert "64000" in line


def test_cross_provider_never_outranks_the_gateway(tmp_path: Path) -> None:
    """The hy3 case: the gateway says 128,000, name-matching says 64,000.

    Letting the approximate tier win would halve the model's real capacity,
    which is precisely what WORKING-NOTES 54 forbids.
    """
    path = tmp_path / "models-dev.json"
    write_models_dev_cache(_CROSS_INDEX, path)

    gateway = ModelReasoningCapability(
        can_reason=True,
        supports_effort_control=True,
        supported_efforts=frozenset({ReasoningEffort.HIGH}),
        mandatory=False,
    )
    resolved = resolve_model_reasoning_capability(
        "nous_portal", "tencent/hy3:free", gateway, path
    )

    assert resolved is not None
    assert resolved.supported_efforts == frozenset({ReasoningEffort.HIGH})
    assert resolved.mandatory is False
    # models.dev still fills what the gateway left unstated.
    assert resolved.supports_toggle_control is False


def test_a_provider_with_its_own_bucket_never_reads_outside_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "models-dev.json"
    write_models_dev_cache(_CROSS_INDEX, path)

    # "alpha" is a bucket in this fixture, so a model it does not list stays
    # unknown rather than borrowing a namesake from "gamma".
    assert model_output_limit_from_models_dev("alpha", "not-listed", path) is None


# --------------------------------------------------------------------------
# Conditional GET
# --------------------------------------------------------------------------


class _ConditionalClient:
    """Minimal httpx.AsyncClient stand-in that honours If-None-Match."""

    requests: ClassVar[list[dict[str, str]]] = []
    current_etag: ClassVar[str] = 'W/"v1"'
    body: ClassVar[dict[str, object]] = {"alpha": {"models": {}}}

    def __init__(self, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> _ConditionalClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def get(self, url: str, headers: dict[str, str] | None = None) -> object:
        type(self).requests.append(dict(headers or {}))
        if (headers or {}).get("If-None-Match") == type(self).current_etag:
            return _Response(304, {}, {})
        return _Response(200, {"etag": type(self).current_etag}, type(self).body)


class _Response:
    def __init__(
        self, status_code: int, headers: dict[str, str], payload: dict[str, object]
    ) -> None:
        self.status_code = status_code
        self.headers = headers
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


@pytest.mark.asyncio
async def test_refresh_sends_if_none_match_and_304_keeps_the_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "models-dev.json"
    _ConditionalClient.requests = []
    _ConditionalClient.current_etag = 'W/"v1"'
    _ConditionalClient.body = {"alpha": {"models": {"a/b": {"reasoning": True}}}}
    monkeypatch.setattr(models_dev.httpx, "AsyncClient", _ConditionalClient)

    assert await refresh_models_dev_cache(path) is True
    first_bytes = path.read_bytes()
    stored = read_models_dev_cache(path)
    assert stored is not None
    assert stored.etag == 'W/"v1"'

    # Age the cache so the second refresh is a genuine revalidation.
    aged = (datetime.now(UTC) - timedelta(hours=48)).timestamp()
    os.utime(path, (aged, aged))
    aged_cache = read_models_dev_cache(path)
    assert aged_cache is not None and aged_cache.fresh is False

    assert await refresh_models_dev_cache(path) is True

    assert _ConditionalClient.requests[0] == {}
    assert _ConditionalClient.requests[1] == {"If-None-Match": 'W/"v1"'}
    # The 4.4MB body is not rewritten; only the revalidation stamp moves.
    assert path.read_bytes() == first_bytes
    revalidated = read_models_dev_cache(path)
    assert revalidated is not None and revalidated.fresh is True


@pytest.mark.asyncio
async def test_changed_etag_rewrites_the_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "models-dev.json"
    _ConditionalClient.requests = []
    _ConditionalClient.current_etag = 'W/"v1"'
    _ConditionalClient.body = {"alpha": {"models": {"a/b": {"reasoning": True}}}}
    monkeypatch.setattr(models_dev.httpx, "AsyncClient", _ConditionalClient)

    assert await refresh_models_dev_cache(path) is True

    _ConditionalClient.current_etag = 'W/"v2"'
    _ConditionalClient.body = {"alpha": {"models": {"a/c": {"reasoning": False}}}}

    assert await refresh_models_dev_cache(path) is True

    stored = read_models_dev_cache(path)
    assert stored is not None
    assert stored.etag == 'W/"v2"'
    assert stored.index == {"alpha": {"models": {"a/c": {"reasoning": False}}}}


@pytest.mark.asyncio
async def test_a_stale_cache_survives_a_failed_revalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silent failure must not cost the user the catalogue they already have."""
    path = tmp_path / "models-dev.json"
    write_models_dev_cache(_CROSS_INDEX, path, 'W/"v1"')

    async def _offline(etag: str | None = None) -> None:
        return None

    monkeypatch.setattr(models_dev, "fetch_models_dev_index", _offline)

    assert await refresh_models_dev_cache(path) is False
    stored = read_models_dev_cache(path)
    assert stored is not None
    assert stored.index == _CROSS_INDEX


# --------------------------------------------------------------------------
# The effort flag and the effort vocabulary are one statement
# --------------------------------------------------------------------------

from my_claude_code.core.model_ids import ResolutionTier  # noqa: E402
from my_claude_code.providers.runtime.models_dev import (  # noqa: E402
    _BOOLEAN_CAPABILITY_FIELDS,
    MIN_APPROXIMATE_BOOLEAN_REPORTERS,
    MIN_APPROXIMATE_VOCABULARY_REPORTERS,
    _build_cross_provider_index,
    _cross_provider_capability,
    _CrossProviderRow,
    _reconcile_effort_statement,
    _vote_across_rungs,
)

_LOW_HIGH = frozenset({ReasoningEffort.LOW, ReasoningEffort.HIGH})


def test_a_stated_false_effort_flag_takes_the_vocabulary_down_with_it() -> None:
    """A stated "no effort knob" is the stronger claim, and it wins outright.

    A record that says the knob is absent while listing the words the knob
    accepts is not a fact about anything, and gating believed the veto: it
    discarded the caller's effort on the strength of the flag while the
    Models page still showed the vocabulary beside it. The rung must go too,
    or the page reports a source for a field that was withdrawn.
    """

    tiers = {
        "supported_efforts": ResolutionTier.CROSS_PROVIDER_TAG_STRIPPED,
        "supports_effort_control": ResolutionTier.CROSS_PROVIDER_EXACT,
    }

    flag, vocabulary = _reconcile_effort_statement(False, _LOW_HIGH, tiers)

    assert flag is False
    assert vocabulary is None
    assert "supported_efforts" not in tiers
    # Only the withdrawn field loses its rung.
    assert tiers["supports_effort_control"] is ResolutionTier.CROSS_PROVIDER_EXACT

    # A stated False with no vocabulary beside it is already consistent, and
    # the rung it drops is one no field is claiming any more -- so applying
    # the rule twice says the same thing as applying it once.
    assert _reconcile_effort_statement(False, None, tiers) == (False, None)
    assert "supported_efforts" not in tiers


def test_an_unstated_flag_takes_true_from_the_vocabularys_own_rung() -> None:
    """A vocabulary IS the statement that an effort knob exists.

    Leaving the flag unknown beside three published effort words would make
    the record say less than its own evidence, and gating reads the flag, not
    the list. The rung is copied across because that is where the claim came
    from -- reporting the flag at a rung that never stated it is the untruth
    the whole ladder exists to remove.
    """

    tiers = {"supported_efforts": ResolutionTier.OPENROUTER_TAG_STRIPPED}

    flag, vocabulary = _reconcile_effort_statement(None, _LOW_HIGH, tiers)

    assert flag is True
    assert vocabulary == _LOW_HIGH
    assert tiers["supports_effort_control"] is ResolutionTier.OPENROUTER_TAG_STRIPPED


@pytest.mark.parametrize(
    ("flag", "vocabulary"),
    [
        # Already agreeing: a knob and the words for it.
        (True, _LOW_HIGH),
        # A knob nobody enumerated the words for.
        (True, None),
        # Nothing known about either half.
        (None, None),
        # An empty vocabulary is not a vocabulary, so it states nothing and
        # may not promote an unknown flag to True.
        (None, frozenset()),
    ],
)
def test_an_already_consistent_effort_statement_passes_through_untouched(
    flag: bool | None, vocabulary: frozenset[ReasoningEffort] | None
) -> None:
    """Reconciliation only fires on a contradiction; otherwise it is identity.

    Pinned because the function mutates ``tiers`` in place: a version that
    rewrote a rung on every call would move fields that nothing disagreed
    about, and no assertion about the contradiction cases would notice.
    """

    tiers: dict[str, ResolutionTier] = {
        "supported_efforts": ResolutionTier.CROSS_PROVIDER_EXACT
    }
    before = dict(tiers)

    assert _reconcile_effort_statement(flag, vocabulary, tiers) == (flag, vocabulary)
    assert tiers == before


def _legacy_cross_provider_capability(
    rungs: tuple[tuple[ResolutionTier, tuple[_CrossProviderRow, ...]], ...],
) -> tuple[ModelReasoningCapability, dict[str, ResolutionTier], object]:
    """The pre-extraction body of ``_cross_provider_capability``, verbatim.

    Kept as a literal copy of the code that was replaced, so the "this is a
    pure refactor" claim is checked rather than asserted. If someone later
    changes the shared :func:`_reconcile_effort_statement` in a way that moves
    the vote's answer, the two implementations diverge and say so here.
    """

    tiers: dict[str, ResolutionTier] = {}
    values: dict[str, bool | None] = {}
    for name in _BOOLEAN_CAPABILITY_FIELDS:
        won = _vote_across_rungs(
            rungs,
            lambda row, name=name: getattr(row.capability, name),
            lambda value: value,
            MIN_APPROXIMATE_BOOLEAN_REPORTERS,
        )
        values[name] = won[0].value if won is not None else None
        if won is not None:
            tiers[name] = won[1]

    efforts = _vote_across_rungs(
        rungs,
        lambda row: row.capability.supported_efforts,
        lambda value: (len(value), sorted(effort.value for effort in value)),
        MIN_APPROXIMATE_VOCABULARY_REPORTERS,
    )
    if efforts is not None:
        tiers["supported_efforts"] = efforts[1]

    vocabulary = efforts[0].value if efforts is not None else None
    if values["supports_effort_control"] is False:
        vocabulary = None
        tiers.pop("supported_efforts", None)
    elif values["supports_effort_control"] is None and vocabulary:
        values["supports_effort_control"] = True
        tiers["supports_effort_control"] = tiers["supported_efforts"]

    capability = ModelReasoningCapability(supported_efforts=vocabulary, **values)
    return capability, tiers, efforts


def _rungs_for(
    index: dict[str, object], model_id: str
) -> tuple[tuple[ResolutionTier, tuple[_CrossProviderRow, ...]], ...]:
    """Assemble the tier-7-to-10 rungs the vote walks, from a raw index."""

    built = _build_cross_provider_index(index)
    return tuple(
        (tier, rows)
        for tier, candidate in model_ids.candidate_ladder(model_id)
        if (rows := built.get(candidate))
    )


# One toggle-only row and one effort row, lifted straight out of _CROSS_INDEX
# so the veto case below is built from the same evidence every other test in
# this section uses.
_TOGGLE_ROW = _CROSS_INDEX["gamma"]["models"]["tencent/hy3"]
_EFFORT_ROW = _CROSS_INDEX["alpha"]["models"]["tencent/hy3"]

# The flag and the vocabulary deliberately come from different rungs: three
# toggle-only rows answer supports_effort_control at tier 7, and the effort
# rows one rung down are the only source of a vocabulary. That is the shape
# the reconciliation exists for, and no single-rung fixture produces it.
_SPLIT_RUNG_INDEX: dict[str, object] = {
    **{
        f"toggle{number}": {"models": {"tencent/hy3-free": _TOGGLE_ROW}}
        for number in range(3)
    },
    **{
        f"effort{number}": {"models": {"tencent/hy3": _EFFORT_ROW}}
        for number in range(3)
    },
}


@pytest.mark.parametrize(
    ("index", "model_id"),
    [
        # Two effort rows against one toggle row, one row with no limit.
        (_CROSS_INDEX, "tencent/hy3:free"),
        # A single row everywhere: every field is under quorum and unknown.
        (_CONTROL_INDEX, "thinkingmachines/inkling"),
        (_REASONING_INDEX, "acme/all-controls"),
        (_REASONING_INDEX, "acme/no-reasoning"),
        # The veto arm: a flag from one rung against a vocabulary from another.
        (_SPLIT_RUNG_INDEX, "tencent/hy3-free"),
    ],
)
def test_extracting_the_reconciliation_did_not_change_the_vote(
    index: dict[str, object], model_id: str
) -> None:
    """The extraction was a pure refactor at this call site, and this proves it.

    ``_reconcile_effort_statement`` was lifted out of the middle of
    ``_cross_provider_capability`` so the reference-then-vote rung could reuse
    it. Moving working code is where silent behaviour changes hide, so the
    live function is compared against a verbatim copy of what it replaced, on
    the fixtures this file already votes over.
    """

    rungs = _rungs_for(index, model_id)

    assert _cross_provider_capability(rungs) == _legacy_cross_provider_capability(rungs)


def test_the_split_rung_fixture_really_exercises_the_veto() -> None:
    """The guard above is only worth anything if its hardest case is reached.

    A fixture that quietly stopped producing a flag/vocabulary disagreement
    would still pass -- both implementations would agree about nothing
    happening -- so the disagreement itself is asserted here.
    """

    rungs = _rungs_for(_SPLIT_RUNG_INDEX, "tencent/hy3-free")
    capability, tiers, efforts = _cross_provider_capability(rungs)

    # The vocabulary really was voted for, one rung below the flag...
    assert efforts is not None
    assert efforts[0].value == frozenset({ReasoningEffort.LOW, ReasoningEffort.HIGH})
    assert efforts[1] is ResolutionTier.CROSS_PROVIDER_TAG_STRIPPED
    # ...and the flag from the tighter rung took it away again.
    assert capability.supports_effort_control is False
    assert capability.supported_efforts is None
    assert tiers["supports_effort_control"] is ResolutionTier.CROSS_PROVIDER_EXACT
    assert "supported_efforts" not in tiers
