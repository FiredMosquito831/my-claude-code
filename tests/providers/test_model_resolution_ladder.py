"""The 8-tier model-resolution ladder, one rung at a time.

The defect this pins: capability and limit lookup used to do no tag stripping
and no vendor-prefix stripping on the cross-provider path, so a tagged model id
matched one row or none. ``minimax/minimax-m3-free`` found exactly one match
and was credited with that row's 1,048,576-token output limit at "100%
agreement" -- one sample always agrees with itself.

Each test here isolates one rung, or the ordering between two of them.
"""

from pathlib import Path

import pytest

from my_claude_code.application.model_metadata import (
    ModelReasoningCapability,
    ProviderModelInfo,
)
from my_claude_code.core.model_ids import (
    ResolutionTier,
    candidate_ladder,
    strip_model_id_tag,
)
from my_claude_code.providers.runtime import models_dev
from my_claude_code.providers.runtime.model_cache import ProviderModelCache
from my_claude_code.providers.runtime.models_dev import (
    MIN_APPROXIMATE_NUMERIC_REPORTERS,
    cross_provider_match,
    model_output_limit_tiered,
    model_reasoning_capability_tiered,
    resolve_model_reasoning_capability,
    write_models_dev_cache,
)

EFFORT_OPTIONS = [{"type": "effort", "values": ["low", "high"]}]


def _row(output: int | None = None, efforts: bool = False) -> dict[str, object]:
    row: dict[str, object] = {"reasoning": True}
    if efforts:
        row["reasoning_options"] = EFFORT_OPTIONS
    if output is not None:
        row["limit"] = {"output": output}
    return row


def _cache(tmp_path: Path, index: dict[str, object]) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "models-dev.json"
    write_models_dev_cache(index, path)
    return path


def _bucket(**models: object) -> dict[str, object]:
    return {"models": dict(models)}


# ---------------------------------------------------------------------------
# The normaliser everything else is built on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("minimax/minimax-m3-free", "minimax/minimax-m3"),
        ("tencent/hy3:free", "tencent/hy3"),
        ("tencent/hy3-paid", "tencent/hy3"),
        ("hy3:nitro", "hy3"),
        # Untagged, and tags outside the allow-list, are left completely alone.
        ("z-ai/glm-5.3-flash", None),
        ("deepseek/deepseek-v4-flash", None),
        ("nano-gpt/claude-opus-4-thinking:32000", None),
        ("gemini-2.5-flash-preview:thinking", None),
        ("openai/gpt-5:high", None),
    ],
)
def test_only_allow_listed_tags_are_stripped(
    model_id: str, expected: str | None
) -> None:
    assert strip_model_id_tag(model_id) == expected


def test_the_ladder_loosens_the_tag_before_the_vendor_prefix() -> None:
    """Exactly two loosenings, in a fixed order, giving tiers 5 through 8."""

    assert candidate_ladder("minimax/minimax-m3-free") == (
        (ResolutionTier.CROSS_PROVIDER_EXACT, "minimax/minimax-m3-free"),
        (ResolutionTier.CROSS_PROVIDER_TAG_STRIPPED, "minimax/minimax-m3"),
        (ResolutionTier.CROSS_PROVIDER_BARE_TAGGED, "minimax-m3-free"),
        (ResolutionTier.CROSS_PROVIDER_BARE_UNTAGGED, "minimax-m3"),
    )


def test_an_untagged_unprefixed_id_has_only_one_rung() -> None:
    """Rungs that would repeat an earlier key are omitted, not retried."""

    assert candidate_ladder("glm-5.3-flash") == (
        (ResolutionTier.CROSS_PROVIDER_EXACT, "glm-5.3-flash"),
    )


# ---------------------------------------------------------------------------
# Tiers 1 and 2: the provider's own /models catalogue
# ---------------------------------------------------------------------------


def _cache_with(model_id: str, max_output_tokens: int) -> ProviderModelCache:
    cache = ProviderModelCache({"commandcode"})
    cache.cache_model_infos(
        "commandcode",
        [ProviderModelInfo(model_id, max_output_tokens=max_output_tokens)],
    )
    return cache


def test_tier_one_is_the_providers_own_catalogue_matched_exactly() -> None:
    cache = _cache_with("minimax/minimax-m3-free", max_output_tokens=512000)

    found = cache.cached_model_info_tiered("commandcode", "minimax/minimax-m3-free")

    assert found is not None
    assert found[1] is ResolutionTier.PROVIDER_EXACT
    assert found[0].max_output_tokens == 512000


def test_tier_two_is_the_providers_own_catalogue_with_the_tag_stripped() -> None:
    """The rung that did not exist before, and the most valuable one.

    A gateway that lists ``minimax/minimax-m3`` and routes
    ``minimax/minimax-m3-free`` to it used to fall straight past its own host
    into a stranger's catalogue.
    """

    cache = _cache_with("minimax/minimax-m3", max_output_tokens=512000)

    found = cache.cached_model_info_tiered("commandcode", "minimax/minimax-m3-free")

    assert found is not None
    assert found[1] is ResolutionTier.PROVIDER_TAG_STRIPPED
    assert found[0].max_output_tokens == 512000


def test_tier_one_beats_tier_two_when_both_are_present() -> None:
    cache = ProviderModelCache({"commandcode"})
    cache.cache_model_infos(
        "commandcode",
        [
            ProviderModelInfo("tencent/hy3", max_output_tokens=64000),
            ProviderModelInfo("tencent/hy3:free", max_output_tokens=128000),
        ],
    )

    found = cache.cached_model_info_tiered("commandcode", "tencent/hy3:free")

    assert found is not None
    assert found[1] is ResolutionTier.PROVIDER_EXACT
    assert found[0].max_output_tokens == 128000


def test_an_untagged_miss_in_the_providers_catalogue_stays_a_miss() -> None:
    """Tier 2 loosens the tag only; it never invents a different model."""

    cache = _cache_with("minimax/minimax-m4", max_output_tokens=512000)

    assert cache.cached_model_info_tiered("commandcode", "minimax/minimax-m3") is None


# ---------------------------------------------------------------------------
# Tiers 3 and 4: the model's own models.dev bucket
# ---------------------------------------------------------------------------


def test_tier_three_is_the_providers_own_models_dev_bucket(tmp_path: Path) -> None:
    path = _cache(
        tmp_path,
        {"openrouter": _bucket(**{"tencent/hy3:free": _row(output=96000)})},
    )

    assert model_output_limit_tiered("open_router", "tencent/hy3:free", path) == (
        96000,
        ResolutionTier.MODELS_DEV_BUCKET_EXACT,
    )


def test_tier_four_strips_the_tag_inside_the_providers_own_bucket(
    tmp_path: Path,
) -> None:
    path = _cache(
        tmp_path, {"openrouter": _bucket(**{"tencent/hy3": _row(output=96000)})}
    )

    assert model_output_limit_tiered("open_router", "tencent/hy3:free", path) == (
        96000,
        ResolutionTier.MODELS_DEV_BUCKET_TAG_STRIPPED,
    )


def test_tier_three_beats_tier_four(tmp_path: Path) -> None:
    path = _cache(
        tmp_path,
        {
            "openrouter": _bucket(
                **{
                    "tencent/hy3": _row(output=64000),
                    "tencent/hy3:free": _row(output=128000),
                }
            )
        },
    )

    assert model_output_limit_tiered("open_router", "tencent/hy3:free", path) == (
        128000,
        ResolutionTier.MODELS_DEV_BUCKET_EXACT,
    )


def test_a_described_provider_never_reads_outside_its_own_bucket(
    tmp_path: Path,
) -> None:
    """Tiers 5-8 exist only for a provider models.dev does not describe."""

    path = _cache(
        tmp_path,
        {
            "openrouter": _bucket(**{"something/else": _row(output=1)}),
            "alpha": _bucket(**{"tencent/hy3": _row(output=64000)}),
            "beta": _bucket(**{"tencent/hy3": _row(output=64000)}),
            "gamma": _bucket(**{"tencent/hy3": _row(output=64000)}),
        },
    )

    assert model_output_limit_tiered("open_router", "tencent/hy3:free", path) == (
        None,
        None,
    )


# ---------------------------------------------------------------------------
# Tiers 5 to 8: the approximate cross-provider vote
# ---------------------------------------------------------------------------


def _hosts(model_id: str, count: int, output: int, efforts: bool = False):
    return {
        f"host{index}": _bucket(**{model_id: _row(output=output, efforts=efforts)})
        for index in range(count)
    }


def test_tier_five_matches_the_id_exactly_across_buckets(tmp_path: Path) -> None:
    path = _cache(tmp_path, _hosts("minimax/minimax-m3-free", 3, 512000))

    limit, tier = model_output_limit_tiered(
        "commandcode", "minimax/minimax-m3-free", path
    )

    assert (limit, tier) == (512000, ResolutionTier.CROSS_PROVIDER_EXACT)


def test_tier_six_strips_the_tag_and_keeps_the_vendor(tmp_path: Path) -> None:
    path = _cache(tmp_path, _hosts("minimax/minimax-m3", 3, 512000))

    limit, tier = model_output_limit_tiered(
        "commandcode", "minimax/minimax-m3-free", path
    )

    assert (limit, tier) == (512000, ResolutionTier.CROSS_PROVIDER_TAG_STRIPPED)


def test_tier_seven_drops_the_vendor_but_keeps_the_tag(tmp_path: Path) -> None:
    path = _cache(tmp_path, _hosts("minimax-m3-free", 3, 512000))

    limit, tier = model_output_limit_tiered(
        "commandcode", "minimax/minimax-m3-free", path
    )

    assert (limit, tier) == (512000, ResolutionTier.CROSS_PROVIDER_BARE_TAGGED)


def test_tier_eight_drops_both(tmp_path: Path) -> None:
    path = _cache(tmp_path, _hosts("minimax-m3", 3, 512000))

    limit, tier = model_output_limit_tiered(
        "commandcode", "minimax/minimax-m3-free", path
    )

    assert (limit, tier) == (512000, ResolutionTier.CROSS_PROVIDER_BARE_UNTAGGED)


def test_tier_nine_is_no_answer_at_all(tmp_path: Path) -> None:
    path = _cache(tmp_path, _hosts("somebody-elses-model", 3, 512000))

    assert model_output_limit_tiered(
        "commandcode", "minimax/minimax-m3-free", path
    ) == (None, None)


@pytest.mark.parametrize(
    ("tighter", "looser", "expected"),
    [
        ("minimax/minimax-m3-free", "minimax/minimax-m3", 111),
        ("minimax/minimax-m3", "minimax-m3-free", 111),
        ("minimax-m3-free", "minimax-m3", 111),
    ],
)
def test_a_tighter_rung_always_wins(
    tmp_path: Path, tighter: str, looser: str, expected: int
) -> None:
    index: dict[str, object] = {}
    index.update(_hosts(tighter, 3, expected))
    index.update({f"other{i}": _bucket(**{looser: _row(output=999)}) for i in range(9)})

    limit, _tier = model_output_limit_tiered(
        "commandcode", "minimax/minimax-m3-free", _cache(tmp_path, index)
    )

    assert limit == expected


def test_the_reported_defect_many_matches_and_not_one_million(
    tmp_path: Path,
) -> None:
    """The measured before/after for ``minimax/minimax-m3-free``.

    Before: one match, output limit 1,048,576, "100% agreement". After: the
    tag and the vendor prefix come off, the ~51 hosts of the bare name are
    found, and the modal limit is the one most of them actually serve.
    """

    index: dict[str, object] = {
        "outlier": _bucket(**{"minimax-m3": _row(output=1048576)}),
        "nvidia": _bucket(**{"minimax-m3": _row(output=16384)}),
    }
    index.update(_hosts("minimax-m3", 49, 512000, efforts=True))

    path = _cache(tmp_path, index)
    resolved = cross_provider_match("commandcode", "minimax/minimax-m3-free", path)

    assert resolved is not None
    assert resolved.match_count == 51
    assert resolved.output_limit == 512000
    assert resolved.output_limit != 1048576
    assert resolved.output_agreement == pytest.approx(49 / 51)
    assert resolved.tier is ResolutionTier.CROSS_PROVIDER_BARE_UNTAGGED
    # The effort vocabulary that used to be missing is the reason this model
    # fell through to toggle-only in reasoning gating.
    assert resolved.capability.supported_efforts is not None


def test_a_tagged_id_outside_the_allow_list_still_resolves(tmp_path: Path) -> None:
    """``tencent/hy3-paid`` returned nothing at all before the ladder."""

    path = _cache(tmp_path, _hosts("tencent/hy3", 3, 64000))

    limit, tier = model_output_limit_tiered("commandcode", "tencent/hy3-paid", path)

    assert (limit, tier) == (64000, ResolutionTier.CROSS_PROVIDER_TAG_STRIPPED)


# ---------------------------------------------------------------------------
# The minimum-sample guard
# ---------------------------------------------------------------------------


def test_one_sample_does_not_supply_a_numeric_limit(tmp_path: Path) -> None:
    """A single row is a transcription, not a vote, and always agrees itself."""

    path = _cache(tmp_path, _hosts("minimax-m3", 1, 1048576))

    match = cross_provider_match("commandcode", "minimax/minimax-m3-free", path)

    assert match is not None
    assert match.match_count == 1
    assert match.output_limit is None
    assert match.output_agreement is None
    assert model_output_limit_tiered(
        "commandcode", "minimax/minimax-m3-free", path
    ) == (None, None)


def test_the_guard_is_exactly_the_documented_threshold(tmp_path: Path) -> None:
    below = _cache(
        tmp_path / "below",
        _hosts("minimax-m3", MIN_APPROXIMATE_NUMERIC_REPORTERS - 1, 512000),
    )
    at = _cache(
        tmp_path / "at",
        _hosts("minimax-m3", MIN_APPROXIMATE_NUMERIC_REPORTERS, 512000),
    )

    assert model_output_limit_tiered("commandcode", "minimax-m3", below)[0] is None
    assert model_output_limit_tiered("commandcode", "minimax-m3", at)[0] == 512000


def test_a_boolean_survives_a_sample_a_number_would_not(tmp_path: Path) -> None:
    """The guard differs per field, because the fields differ.

    Whether a model reasons is a property of the model and is near-unanimous
    across hosts; what it will emit is a property of the deployment.
    """

    path = _cache(tmp_path, _hosts("minimax-m3", 1, 1048576))

    match = cross_provider_match("commandcode", "minimax/minimax-m3-free", path)

    assert match is not None
    assert match.capability.can_reason is True
    assert match.output_limit is None


def test_the_log_line_names_the_tier_and_never_claims_full_agreement_on_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _cache(tmp_path, _hosts("minimax-m3", 1, 1048576))
    records: list[str] = []

    class _RecordingLogger:
        def info(self, template: str, *args: object) -> None:
            records.append(template.format(*args))

        def debug(self, template: str, *args: object) -> None:
            return None

    monkeypatch.setattr(models_dev, "logger", _RecordingLogger())

    cross_provider_match("commandcode", "minimax/minimax-m3-free", path)

    assert len(records) == 1
    line = records[0]
    assert "tier 8 (cross_provider_bare_untagged)" in line
    assert "100% agreement" not in line
    assert "withheld: 1 of the required 3 rows reported one" in line


# ---------------------------------------------------------------------------
# Field by field, and the regression guard that matters most
# ---------------------------------------------------------------------------


def test_the_gateway_keeps_outranking_name_matching(tmp_path: Path) -> None:
    """``tencent/hy3:free`` must stay at the gateway's 128,000.

    Cross-provider name matching says 64,000. Letting it win would halve the
    model's real capacity for no reason (WORKING-NOTES 54).
    """

    path = _cache(tmp_path, _hosts("tencent/hy3", 5, 64000))
    cache = ProviderModelCache({"nous_portal"})
    cache.cache_model_infos(
        "nous_portal", [ProviderModelInfo("tencent/hy3:free", max_output_tokens=128000)]
    )

    found = cache.cached_model_info_tiered("nous_portal", "tencent/hy3:free")
    assert found is not None
    assert found[0].max_output_tokens == 128000
    assert found[1] is ResolutionTier.PROVIDER_EXACT
    # And the tier the gateway beat really did have a different answer.
    assert (
        model_output_limit_tiered("nous_portal", "tencent/hy3:free", path)[0] == 64000
    )


def test_the_gateway_supplies_the_limit_and_tier_six_supplies_the_efforts(
    tmp_path: Path,
) -> None:
    """Field by field, not source by source: both survive together."""

    path = _cache(
        tmp_path,
        {
            f"host{number}": _bucket(
                **{"minimax/minimax-m3": _row(output=64000, efforts=True)}
            )
            for number in range(3)
        },
    )

    gateway = ModelReasoningCapability(can_reason=True)
    resolved = resolve_model_reasoning_capability(
        "commandcode", "minimax/minimax-m3-free", gateway, path
    )

    assert resolved is not None
    assert resolved.can_reason is True
    assert resolved.supported_efforts is not None

    capability, tiers = model_reasoning_capability_tiered(
        "commandcode", "minimax/minimax-m3-free", path
    )
    assert capability is not None
    assert tiers["supported_efforts"] is ResolutionTier.CROSS_PROVIDER_TAG_STRIPPED

    # The gateway's own catalogue still owns the number.
    cache = ProviderModelCache({"commandcode"})
    cache.cache_model_infos(
        "commandcode",
        [ProviderModelInfo("minimax/minimax-m3-free", max_output_tokens=131072)],
    )
    found = cache.cached_model_info_tiered("commandcode", "minimax/minimax-m3-free")
    assert found is not None
    assert found[0].max_output_tokens == 131072


@pytest.mark.parametrize(
    "model_id", ["z-ai/glm-5.3-flash", "deepseek/deepseek-v4-flash"]
)
def test_untagged_models_behave_exactly_as_before(
    tmp_path: Path, model_id: str
) -> None:
    """Pinned: the ladder must not move an id it has no tag to strip from."""

    bare = model_id.split("/", 1)[1]
    path = _cache(
        tmp_path,
        {
            **_hosts(model_id, 3, 96000),
            "elsewhere": _bucket(**{bare: _row(output=1)}),
        },
    )

    limit, tier = model_output_limit_tiered("commandcode", model_id, path)

    assert (limit, tier) == (96000, ResolutionTier.CROSS_PROVIDER_EXACT)
