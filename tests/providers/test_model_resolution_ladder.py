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
from my_claude_code.core.reasoning import ReasoningEffort
from my_claude_code.providers.runtime import models_dev
from my_claude_code.providers.runtime.model_cache import ProviderModelCache
from my_claude_code.providers.runtime.models_dev import (
    MIN_APPROXIMATE_BOOLEAN_REPORTERS,
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


def test_one_foreign_row_no_longer_decides_a_boolean(tmp_path: Path) -> None:
    """Booleans need three reporters too, since 6.3.0.

    They used to need one, on the reasoning that same-named rows are
    near-unanimous about whether a model reasons at all. Near-unanimous is not
    the point: one row is a transcription, not a vote, and it always agrees
    with itself. It also let a single row *veto* a capability that a dozen
    rows one rung down contradicted.
    """

    path = _cache(tmp_path, _hosts("minimax-m3", 1, 1048576))

    match = cross_provider_match("commandcode", "minimax/minimax-m3-free", path)

    assert match is not None
    assert match.capability.can_reason is None
    assert match.output_limit is None


def test_three_rows_are_enough_for_a_boolean(tmp_path: Path) -> None:
    """And at the threshold the vote answers, for every field alike."""

    path = _cache(tmp_path, _hosts("minimax-m3", 3, 512000))

    match = cross_provider_match("commandcode", "minimax/minimax-m3-free", path)

    assert match is not None
    assert match.capability.can_reason is True


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
    assert "tier 10 (cross_provider_bare_untagged)" in line
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


# ---------------------------------------------------------------------------
# Tiers 5-6: the OpenRouter reference catalogue
# ---------------------------------------------------------------------------


def _openrouter(**models: object) -> dict[str, object]:
    return {"openrouter": _bucket(**models)}


def test_tier_five_is_the_openrouter_catalogue_exact_with_tag_normalised(
    tmp_path: Path,
) -> None:
    """One routing variant, two spellings, one model.

    OpenRouter writes ``minimax/minimax-m3:free``; Command Code writes
    ``minimax/minimax-m3-free``. The tag is the same tag, so respelling it is
    an exact match -- tier 5 -- not the looser tag-stripped rung.
    """

    path = _cache(
        tmp_path,
        _openrouter(**{"minimax/minimax-m3:free": _row(512000, efforts=True)}),
    )

    capability, tiers = model_reasoning_capability_tiered(
        "commandcode", "minimax/minimax-m3-free", path
    )

    assert capability is not None
    assert capability.can_reason is True
    assert tiers["can_reason"] is ResolutionTier.OPENROUTER_EXACT
    assert ResolutionTier.OPENROUTER_EXACT.is_reference
    assert not ResolutionTier.OPENROUTER_EXACT.is_approximate
    assert not ResolutionTier.OPENROUTER_EXACT.is_authoritative


def test_tier_six_strips_the_tag_in_the_openrouter_catalogue(tmp_path: Path) -> None:
    """No tagged variant to respell, so the tag comes off and the rung says so."""

    path = _cache(tmp_path, _openrouter(**{"minimax/minimax-m3": _row(512000)}))

    _capability, tiers = model_reasoning_capability_tiered(
        "commandcode", "minimax/minimax-m3-free", path
    )

    assert tiers["can_reason"] is ResolutionTier.OPENROUTER_TAG_STRIPPED


def test_openrouter_itself_uses_its_bucket_at_tier_three_not_five(
    tmp_path: Path,
) -> None:
    """For OpenRouter the same rows ARE its own bucket, and rank accordingly."""

    path = _cache(tmp_path, _openrouter(**{"minimax/minimax-m3": _row(512000)}))

    _capability, tiers = model_reasoning_capability_tiered(
        "open_router", "minimax/minimax-m3", path
    )

    assert tiers["can_reason"] is ResolutionTier.MODELS_DEV_BUCKET_EXACT


def test_a_field_the_openrouter_row_leaves_unstated_falls_to_the_vote(
    tmp_path: Path,
) -> None:
    """Per field, not per source -- the same rule every other layer uses."""

    index: dict[str, object] = {
        "openrouter": _bucket(**{"minimax/minimax-m3:free": {"reasoning": True}}),
        **{
            f"host{n}": _bucket(**{"minimax/minimax-m3": _row(512000, efforts=True)})
            for n in range(3)
        },
    }
    path = _cache(tmp_path, index)

    capability, tiers = model_reasoning_capability_tiered(
        "commandcode", "minimax/minimax-m3-free", path
    )

    assert capability is not None
    assert tiers["can_reason"] is ResolutionTier.OPENROUTER_EXACT
    # The reference row says nothing about a vocabulary, so the vote answers it.
    assert capability.supported_efforts is not None
    assert tiers["supported_efforts"] is ResolutionTier.CROSS_PROVIDER_TAG_STRIPPED


def test_the_reference_rung_outranks_the_vote_but_not_the_providers_bucket(
    tmp_path: Path,
) -> None:
    """Ordering, stated as an assertion rather than as a comment."""

    assert (
        ResolutionTier.MODELS_DEV_BUCKET_TAG_STRIPPED
        < ResolutionTier.OPENROUTER_EXACT
        < ResolutionTier.CROSS_PROVIDER_EXACT
    )


def test_a_record_never_carries_a_vocabulary_without_effort_control(
    tmp_path: Path,
) -> None:
    """The two fields are one statement, so they may not contradict each other.

    Live: ``commandcode/minimax/minimax-m3-free`` resolved
    ``supports_effort_control=False`` off one row and
    ``supported_efforts=[high, low, medium]`` off twelve, and gating believed
    the veto.
    """

    index: dict[str, object] = {
        f"host{n}": _bucket(
            **{
                "minimax/minimax-m3": {
                    "reasoning": True,
                    "reasoning_options": [
                        {"type": "effort", "values": ["low", "high"]}
                    ],
                }
            }
        )
        for n in range(3)
    }
    path = _cache(tmp_path, index)

    capability, tiers = model_reasoning_capability_tiered(
        "commandcode", "minimax/minimax-m3-free", path
    )

    assert capability is not None
    assert capability.supported_efforts is not None
    # A vocabulary that cleared its quorum IS the statement that a knob exists.
    assert capability.supports_effort_control is True
    assert tiers["supports_effort_control"] is tiers["supported_efforts"]


def test_a_vetoed_effort_knob_takes_its_vocabulary_with_it(tmp_path: Path) -> None:
    """And in the other direction: no knob, no words for the knob."""

    index: dict[str, object] = {
        f"host{n}": _bucket(
            **{"minimax/minimax-m3": {"reasoning": True, "reasoning_options": []}}
        )
        for n in range(3)
    }
    path = _cache(tmp_path, index)

    capability, tiers = model_reasoning_capability_tiered(
        "commandcode", "minimax/minimax-m3-free", path
    )

    assert capability is not None
    assert capability.supports_effort_control is False
    assert capability.supported_efforts is None
    assert "supported_efforts" not in tiers


# ---------------------------------------------------------------------------
# Tiers 5-6 against tiers 7-10: which record wins when both state a field
# ---------------------------------------------------------------------------

_ROUTE = "widget/w1-free"


def _controls_row(
    *,
    efforts: tuple[str, ...] | None = None,
    toggle: bool = False,
    budget: bool = False,
    output: int | None = None,
) -> dict[str, object]:
    """One models.dev row spelled by the reasoning controls it publishes.

    ``reasoning_options`` is always written, because an absent list is
    "unknown" and an empty one is "no controls at all" -- the difference every
    test below turns on.
    """

    options: list[dict[str, object]] = []
    if efforts is not None:
        options.append({"type": "effort", "values": list(efforts)})
    if toggle:
        options.append({"type": "toggle"})
    if budget:
        options.append({"type": "budget_tokens"})
    row: dict[str, object] = {"reasoning": True, "reasoning_options": options}
    if output is not None:
        row["limit"] = {"output": output}
    return row


def _reference_plus_hosts(
    tmp_path: Path,
    reference: dict[str, object],
    *host_rows: dict[str, object],
) -> Path:
    """Build a cache where the reference row cannot also vote in the vote.

    OpenRouter spells the routing tag ``:free`` and the gateway spells it
    ``-free``. The reference lookup respells the tag and finds the row at tier
    5; the cross-provider ladder never respells, so ``widget/w1:free`` is
    absent from every rung of the vote it is being weighed against. Without
    that separation "three rows voted" would silently mean four, and the
    quorum assertions below would be measuring the wrong number.
    """

    index: dict[str, object] = {"openrouter": _bucket(**{"widget/w1:free": reference})}
    for number, row in enumerate(host_rows):
        index[f"host{number}"] = _bucket(**{_ROUTE: row})
    return _cache(tmp_path, index)


def test_a_richer_vote_beats_a_poorer_reference_row_for_effort_control(
    tmp_path: Path,
) -> None:
    """Stated True outranks stated False for a reasoning *control*.

    The reference catalogue is one editorial description and it is usually
    right, but "this model has no effort knob" is a claim a dozen hosts that
    accept ``reasoning_effort`` from it can refute. Taking the poorer answer
    because it came from the tidier source is how a model that accepts an
    effort ends up gated down to toggle-only.
    """

    path = _reference_plus_hosts(
        tmp_path,
        _controls_row(toggle=True),
        *[_controls_row(efforts=("low", "high"))] * 3,
    )

    capability, tiers = model_reasoning_capability_tiered("commandcode", _ROUTE, path)

    assert capability is not None
    assert capability.supports_effort_control is True
    assert tiers["supports_effort_control"].is_approximate
    # And the rung on screen is the one the value actually came from.
    assert tiers["supports_effort_control"] is ResolutionTier.CROSS_PROVIDER_EXACT


def test_a_poorer_vote_never_beats_a_richer_reference_row(tmp_path: Path) -> None:
    """The converse, the half that keeps the rule from being a coin toss.

    "More capable wins" is directional, not "whoever disagrees last wins": a
    vote saying the knob is absent may not take away a knob the curated
    catalogue describes.
    """

    path = _reference_plus_hosts(
        tmp_path,
        _controls_row(efforts=("low", "high")),
        *[_controls_row(toggle=True)] * 3,
    )

    capability, tiers = model_reasoning_capability_tiered("commandcode", _ROUTE, path)

    assert capability is not None
    assert capability.supports_effort_control is True
    assert tiers["supports_effort_control"] is ResolutionTier.OPENROUTER_EXACT


def test_a_reference_row_still_wins_can_reason(tmp_path: Path) -> None:
    """``can_reason`` is not a ladder, so "more" is not a reason to overturn it.

    The safety property: a curated "this model does not reason" that a name
    vote could invert would send a thinking instruction to a model that has no
    thinking to switch on, and the host answers that with a 400.
    """

    path = _reference_plus_hosts(
        tmp_path,
        {"reasoning": False},
        *[_controls_row(efforts=("low", "high"))] * 3,
    )

    capability, tiers = model_reasoning_capability_tiered("commandcode", _ROUTE, path)

    assert capability is not None
    assert capability.can_reason is False
    assert tiers["can_reason"] is ResolutionTier.OPENROUTER_EXACT
    assert tiers["can_reason"].is_reference


def test_two_outlier_rows_do_not_overturn_a_reference_row(tmp_path: Path) -> None:
    """The live ``minimax-m3`` shape: the vote agrees with the reference.

    Three same-named rows publish a toggle and no effort knob, two publish an
    effort vocabulary. The vote's answer is therefore the modal ``False``, so
    there is nothing richer to prefer and the reference row stands exactly as
    it did before the tie-break existed. Two rows out of five may not quietly
    promote a model.
    """

    path = _reference_plus_hosts(
        tmp_path,
        _controls_row(toggle=True),
        *[_controls_row(toggle=True)] * 3,
        *[_controls_row(efforts=("low", "high", "max"))] * 2,
    )

    capability, tiers = model_reasoning_capability_tiered("commandcode", _ROUTE, path)

    assert capability is not None
    assert capability.supports_effort_control is False
    assert capability.supports_toggle_control is True
    # Two reporters cannot supply a vocabulary either, and a withdrawn field
    # must not leave a rung behind on the Models page.
    assert capability.supported_efforts is None
    assert "supported_efforts" not in tiers
    assert tiers["supports_effort_control"] is ResolutionTier.OPENROUTER_EXACT


def test_the_quorum_still_applies_before_richer_can_win(tmp_path: Path) -> None:
    """The tie-break chooses between two answers; it does not create one.

    Two rows are not a vote -- they cannot break a tie from evidence -- so
    below :data:`MIN_APPROXIMATE_BOOLEAN_REPORTERS` the vote has nothing to
    offer and the reference is not being outranked by anything. Letting
    "richer wins" reach past the quorum would reinstate the single foreign row
    that 6.3.0 removed, pointing the other way.
    """

    assert MIN_APPROXIMATE_BOOLEAN_REPORTERS == 3
    path = _reference_plus_hosts(
        tmp_path,
        _controls_row(toggle=True),
        *[_controls_row(efforts=("low", "high"))] * 2,
    )

    capability, tiers = model_reasoning_capability_tiered("commandcode", _ROUTE, path)

    assert capability is not None
    assert capability.supports_effort_control is False
    assert capability.supported_efforts is None
    assert tiers["supports_effort_control"] is ResolutionTier.OPENROUTER_EXACT


def test_a_larger_voted_vocabulary_beats_a_smaller_reference_one(
    tmp_path: Path,
) -> None:
    """Same direction for the vocabulary, ranked the way the vote ranks it.

    A reference row listing one effort word beside a vote listing three is not
    a disagreement about which words exist; it is one source knowing fewer of
    them. Keeping the shorter list would refuse efforts the model accepts.
    """

    path = _reference_plus_hosts(
        tmp_path,
        _controls_row(efforts=("low",)),
        *[_controls_row(efforts=("low", "high", "max"))] * 3,
    )

    capability, tiers = model_reasoning_capability_tiered("commandcode", _ROUTE, path)

    assert capability is not None
    assert capability.supported_efforts == frozenset(
        {ReasoningEffort.LOW, ReasoningEffort.HIGH, ReasoningEffort.MAX}
    )
    assert tiers["supported_efforts"] is ResolutionTier.CROSS_PROVIDER_EXACT


def test_prefer_richer_never_moves_a_numeric_limit(tmp_path: Path) -> None:
    """Capabilities and limits are resolved by two different functions.

    A limit is a property of *this deployment*, not of the model, so it stays
    at the tightest rung that states it however capable a looser rung claims
    the model is. This is the guard that a later "unify the two reference
    lookups" refactor cannot silently start voting on token counts: the
    reasoning fields on this very route come from the vote, and the number
    does not move with them.
    """

    path = _reference_plus_hosts(
        tmp_path,
        _controls_row(toggle=True, output=111000),
        *[_controls_row(efforts=("low", "high"), output=222000)] * 3,
    )

    capability, tiers = model_reasoning_capability_tiered("commandcode", _ROUTE, path)
    assert capability is not None
    assert tiers["supports_effort_control"] is ResolutionTier.CROSS_PROVIDER_EXACT

    assert model_output_limit_tiered("commandcode", _ROUTE, path) == (
        111000,
        ResolutionTier.OPENROUTER_EXACT,
    )


def test_a_resolved_record_never_states_no_effort_control_beside_a_vocabulary(
    tmp_path: Path,
) -> None:
    """Live: ``commandcode/openai/o3`` resolved exactly that contradiction.

    The reference row publishes a toggle and no effort knob; the same-named
    rows across other hosts publish an effort vocabulary. Merging them
    reference-first kept the reference's ``supports_effort_control=False`` and
    then filled the unstated ``supported_efforts`` from the vote, producing a
    record that said "no effort knob" while listing the words the knob takes.
    Whichever half wins, the two must agree afterwards.
    """

    index: dict[str, object] = {
        "openrouter": _bucket(**{"openai/o3": _controls_row(toggle=True)}),
        **{
            f"host{number}": _bucket(
                **{"openai/o3": _controls_row(efforts=("low", "high", "max"))}
            )
            for number in range(3)
        },
    }
    path = _cache(tmp_path, index)

    capability, _tiers = model_reasoning_capability_tiered(
        "commandcode", "openai/o3", path
    )

    assert capability is not None
    assert not (
        capability.supports_effort_control is False
        and capability.supported_efforts is not None
    )
    # The vocabulary is real, so it is the flag that gives way.
    assert capability.supports_effort_control is True
    assert capability.supported_efforts == frozenset(
        {ReasoningEffort.LOW, ReasoningEffort.HIGH, ReasoningEffort.MAX}
    )


def test_a_provider_with_its_own_bucket_is_untouched_by_the_tie_break(
    tmp_path: Path,
) -> None:
    """Tiers 5-10 are for a provider models.dev does not describe. Only those.

    A provider with a bucket answers from its own row at tier 3 and stops, so
    a richer reference row and a richer vote for the same name are both
    invisible to it. Without this the tie-break would start editing the
    authoritative rungs it was never allowed to reach.
    """

    index: dict[str, object] = {
        "opencode": _bucket(**{"mimo-v2.5-free": _controls_row()}),
        "openrouter": _bucket(
            **{"mimo-v2.5-free": _controls_row(efforts=("low", "high", "max"))}
        ),
        **{
            f"host{number}": _bucket(
                **{"mimo-v2.5-free": _controls_row(efforts=("low", "high", "max"))}
            )
            for number in range(3)
        },
    }
    path = _cache(tmp_path, index)

    capability, tiers = model_reasoning_capability_tiered(
        "opencode", "mimo-v2.5-free", path
    )

    assert capability is not None
    assert capability.supports_effort_control is False
    assert capability.supported_efforts is None
    assert tiers["can_reason"] is ResolutionTier.MODELS_DEV_BUCKET_EXACT
    assert tiers["can_reason"].is_authoritative


# ---------------------------------------------------------------------------
# The four routes that were measured on the live install before the tie-break
# ---------------------------------------------------------------------------

# A checked-in reproduction of the four shapes, NOT the operator's own
# ``~/.fcc`` cache: a test that reads a live 4.4 MB file proves whatever that
# file happened to say on the day it was fetched, and fails for a stranger.
# Two routes resolve from a provider bucket (tier 3) and two from the
# reference catalogue (tiers 5 and 6), so between them they cover both sides
# of the rung the tie-break lives on.
_LIVE_ROUTES_INDEX: dict[str, object] = {
    "openrouter": _bucket(
        **{
            # Toggle only, and spelled untagged, so the ``-free`` route finds
            # it one rung down at tier 6.
            "minimax/minimax-m3": {
                "reasoning": True,
                "reasoning_options": [{"type": "toggle"}],
            },
            "z-ai/glm-5.3-flash": {
                "reasoning": True,
                "reasoning_options": [
                    {"type": "effort", "values": ["low", "high", "max"]}
                ],
            },
        }
    ),
    "opencode": _bucket(
        **{"mimo-v2.5-free": {"reasoning": True, "reasoning_options": []}}
    ),
    "nvidia": _bucket(
        **{
            "moonshotai/kimi-k3": {
                "reasoning": True,
                "reasoning_options": [
                    {"type": "effort", "values": ["low", "high", "max"]},
                    {"type": "toggle"},
                ],
            }
        }
    ),
    # The foreign rows that share the bare name ``minimax-m3``: three publish a
    # toggle, two publish an effort vocabulary. This is the shape that made the
    # tie-break worth checking against, and its modal answer agrees with the
    # reference row.
    **{
        f"toggle-host{number}": _bucket(
            **{
                "minimax-m3": {
                    "reasoning": True,
                    "reasoning_options": [{"type": "toggle"}],
                }
            }
        )
        for number in range(3)
    },
    **{
        f"effort-host{number}": _bucket(
            **{
                "minimax-m3": {
                    "reasoning": True,
                    "reasoning_options": [
                        {"type": "effort", "values": ["low", "high", "max"]}
                    ],
                }
            }
        )
        for number in range(2)
    },
}

_LOW_HIGH_MAX = frozenset(
    {ReasoningEffort.LOW, ReasoningEffort.HIGH, ReasoningEffort.MAX}
)


@pytest.mark.parametrize(
    ("provider", "model_id", "expected", "tier"),
    [
        (
            "commandcode",
            "minimax/minimax-m3-free",
            (True, False, True, False, None),
            ResolutionTier.OPENROUTER_TAG_STRIPPED,
        ),
        (
            "commandcode",
            "z-ai/glm-5.3-flash",
            (True, True, False, False, _LOW_HIGH_MAX),
            ResolutionTier.OPENROUTER_EXACT,
        ),
        (
            "opencode",
            "mimo-v2.5-free",
            (True, False, False, False, None),
            ResolutionTier.MODELS_DEV_BUCKET_EXACT,
        ),
        (
            "nvidia_nim",
            "moonshotai/kimi-k3",
            (True, True, True, False, _LOW_HIGH_MAX),
            ResolutionTier.MODELS_DEV_BUCKET_EXACT,
        ),
    ],
)
def test_the_four_live_routes_are_unchanged_by_the_tie_break(
    tmp_path: Path,
    provider: str,
    model_id: str,
    expected: tuple[bool, bool, bool, bool, frozenset[ReasoningEffort] | None],
    tier: ResolutionTier,
) -> None:
    """Four routes measured on the live install, pinned field by field.

    Preferring the richer record is only defensible if it changes the records
    that were wrong and leaves the rest alone. These four were correct before
    the tie-break, and every one of them must resolve to the same five values
    at the same rung afterwards.
    """

    path = _cache(tmp_path, _LIVE_ROUTES_INDEX)

    capability, tiers = model_reasoning_capability_tiered(provider, model_id, path)

    assert capability is not None
    assert (
        capability.can_reason,
        capability.supports_effort_control,
        capability.supports_toggle_control,
        capability.supports_budget_control,
        capability.supported_efforts,
    ) == expected
    assert tiers["can_reason"] is tier
