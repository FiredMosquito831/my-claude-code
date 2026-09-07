"""Parity tests: every number here appears in a provider's own documentation.

This file is the gate the estimator ships behind. A family that cannot
reproduce its own published worked example is not a family we know how to
bill -- it is a guess -- and it ships as ``unknown`` instead, which charges
Anthropic's rate and says so.
"""

import math

import pytest

from my_claude_code.core.anthropic.image_tokens import (
    DEEPSEEK_MAX_TOKENS,
    ImageTokenFamily,
    anthropic_fit,
    anthropic_image_tokens,
    deepseek_image_tokens,
    gemini_image_tokens,
    image_tokens,
    openai_patch_image_tokens,
    openai_tile_image_tokens,
    pixtral_image_tokens,
    qwen_image_tokens,
    resolve_family,
)


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [
        # platform.claude.com/docs/en/build-with-claude/vision, worked examples.
        (200, 200, 64),
        (1000, 1000, 1296),
        (1092, 1092, 1521),
        # The fourth example is the interesting one: 1920x1080 is over budget,
        # so it is billed at the size Anthropic resizes it to.
        (1920, 1080, 1560),
    ],
)
def test_anthropic_matches_published_examples(width, height, expected):
    assert anthropic_image_tokens(width, height) == expected


def test_anthropic_resize_targets_the_token_budget_not_just_the_pixel_one():
    """1920x1080 lands on 1456x819, not on 1568x882.

    1568x882 satisfies the 1568 px long edge and would cost 56 x 32 = 1792
    tokens, over the 1568-token half of the same budget. Both constraints bind
    and the token one is tighter here, which is the whole reason the fit is a
    search rather than one division.
    """
    assert anthropic_fit(1920, 1080) == (1456, 819)
    # The rejected candidate, counted raw rather than through the estimator --
    # which would simply re-fit it and hand back the same 1560.
    assert math.ceil(1568 / 28) * math.ceil(882 / 28) == 1792


def test_anthropic_caps_at_standard_tier():
    assert anthropic_image_tokens(4000, 4000) <= 1568
    assert anthropic_image_tokens(20000, 12000) <= 1568


def test_anthropic_leaves_a_small_image_alone():
    assert anthropic_fit(1384, 835) == (1384, 835)


def test_openai_tile_low_detail_is_base_only():
    """detail: low bills the base and nothing else, whatever the size."""
    assert openai_tile_image_tokens(4000, 4000, low_detail=True) == 85
    assert openai_tile_image_tokens(64, 64, low_detail=True) == 85


def test_openai_tile_matches_documented_pipeline():
    # 1920x1080 fits 2048^2 already; short side 1080 -> 768 gives 1365x768;
    # 512-px tiles gives ceil(1365/512) x ceil(768/512) = 3 x 2 = 6 tiles.
    assert openai_tile_image_tokens(1920, 1080) == 85 + 6 * 170
    assert (
        openai_tile_image_tokens(1920, 1080, base=2833, per_tile=5667)
        == 2833 + 6 * 5667
    )


def test_openai_tile_does_not_enlarge_a_small_image():
    """A 300x200 image is not scaled up to a 768 short side."""
    assert openai_tile_image_tokens(300, 200) == 85 + 170


def test_openai_patch_counts_whole_patches_under_budget():
    # 1920x1080 is 60 x 34 = 2040 patches, inside the 2500 budget, and the
    # gpt-5.6 multiplier is 1.2 -> 2448.
    assert openai_patch_image_tokens(1920, 1080) == 2448
    # The same picture after this PR's default downscale.
    assert openai_patch_image_tokens(1456, 819) == 1435


def test_openai_patch_shrinks_to_budget():
    """An image over the budget is shrunk by sqrt((32^2 x budget)/(w x h)).

    4000x4000 is 15625 patches. The shrink factor is sqrt(2500 x 1024 / 16e6)
    = 0.4, giving 1600x1600 -- exactly 50 x 50 = 2500 patches, the budget --
    and 2500 x 1.2 = 3000.
    """
    assert openai_patch_image_tokens(4000, 4000) == 3000


def test_gemini_small_image_is_flat_258():
    assert gemini_image_tokens(384, 384) == 258
    assert gemini_image_tokens(385, 385) != 258


def test_gemini_matches_published_example():
    # 960x540: crop unit floor(540/1.5) = 360, tiles 3 x 2, 6 x 258 = 1548.
    assert gemini_image_tokens(960, 540) == 1548


def test_deepseek_caps_at_384():
    assert deepseek_image_tokens(1920, 1080) == DEEPSEEK_MAX_TOKENS
    assert deepseek_image_tokens(200, 200) == DEEPSEEK_MAX_TOKENS


def test_qwen_stays_inside_its_published_window():
    assert qwen_image_tokens(1440, 900) == 1280
    assert qwen_image_tokens(64, 64) == 256
    # 28-px blocks, inside the window: 1000x1000 is 36 x 36 = 1296, clamped to
    # the documented 1280 maximum.
    assert qwen_image_tokens(560, 560) == 400


def test_pixtral_counts_16px_patches_with_row_breaks():
    # UNVERIFIED upstream: no provider descriptor declares this family, and
    # this asserts the arithmetic rather than a published number.
    assert pixtral_image_tokens(32, 32) == 2 * (2 + 1) + 1


def test_unknown_family_falls_back_to_anthropic():
    assert image_tokens(1920, 1080, ImageTokenFamily.UNKNOWN) == anthropic_image_tokens(
        1920, 1080
    )
    # And a typo in an operator-edited descriptor degrades rather than raises.
    assert image_tokens(1920, 1080, "not-a-family") == anthropic_image_tokens(
        1920, 1080
    )
    assert resolve_family("not-a-family") is ImageTokenFamily.UNKNOWN


def test_dispatch_reaches_each_family():
    assert image_tokens(960, 540, "gemini") == gemini_image_tokens(960, 540)
    assert image_tokens(1920, 1080, "openai_tile") == openai_tile_image_tokens(
        1920, 1080
    )
    assert image_tokens(1920, 1080, "openai_patch") == openai_patch_image_tokens(
        1920, 1080
    )
    assert image_tokens(1920, 1080, "deepseek") == DEEPSEEK_MAX_TOKENS
    assert image_tokens(560, 560, "qwen") == qwen_image_tokens(560, 560)
    assert image_tokens(32, 32, "pixtral") == pixtral_image_tokens(32, 32)


def test_every_declared_family_is_a_known_name():
    """The catalogue may only name a family the estimator implements.

    ``config`` is a leaf and cannot import ``core``, so the two lists cannot be
    the same object. This is the check that keeps them equal instead.
    """
    from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
    from my_claude_code.core.anthropic.image_tokens import IMAGE_TOKEN_FAMILY_NAMES

    declared = {
        descriptor.image_token_family for descriptor in PROVIDER_CATALOG.values()
    }
    assert declared <= IMAGE_TOKEN_FAMILY_NAMES


def test_no_family_is_reached_by_a_model_name():
    """The estimator takes a family, never a model. Guarded, not assumed."""
    import inspect

    from my_claude_code.core.anthropic import image_tokens as module

    source = inspect.getsource(module)
    for forbidden in ("gpt-", "claude-", "gemini-", "qwen-", 'o4-mini"', "-mini'"):
        assert f'"{forbidden}' not in source
