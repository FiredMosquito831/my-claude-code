"""What one image costs, per billing family, from each provider's own docs.

Before this module the estimator scored every picture as ``max(85, len(base64)
// 3000)``. The 85 floor is OpenAI's legacy low-detail base and is defensible;
the 3000 divisor corresponds to no published formula from any provider, and it
measured the one quantity that does not determine the bill -- compressed byte
count. Every formula below is a function of width and height and of nothing
else.

Two rules hold throughout, and both are load-bearing:

* **Nothing branches on a model name.** The family is declared data on
  ``config.provider_catalog.ProviderDescriptor.image_token_family`` and arrives
  here as a value. Per-model constants (a tile price, a patch budget) are
  parameters with a documented family default, never a lookup table of model
  strings.
* **A family ships only if it can reproduce its own published worked example.**
  ``tests/core/anthropic/test_image_tokens.py`` asserts each number that
  appears in a provider's own documentation. A provider with no published
  formula is ``UNKNOWN`` and is charged the Anthropic rate, which is the budget
  the client itself is reasoning about because the request arrived in the
  Anthropic protocol.
"""

import math
from enum import StrEnum


class ImageTokenFamily(StrEnum):
    """How a host bills an image, as declared by its provider descriptor."""

    ANTHROPIC = "anthropic"
    OPENAI_TILE = "openai_tile"
    OPENAI_PATCH = "openai_patch"
    GEMINI = "gemini"
    DEEPSEEK = "deepseek"
    QWEN = "qwen"
    PIXTRAL = "pixtral"
    #: No published formula. Charged as Anthropic; the fallback is recorded
    #: rather than hidden, so the Models page can show how wrong it is.
    UNKNOWN = "unknown"


IMAGE_TOKEN_FAMILY_NAMES: frozenset[str] = frozenset(
    member.value for member in ImageTokenFamily
)

# ------------------------------------------------------------------ Anthropic

#: platform.claude.com/docs/en/build-with-claude/vision -- "each image costs
#: (width px x height px) / 750 tokens" is the legacy approximation; the exact
#: rule the same page's worked examples reproduce is one token per 28x28 patch.
ANTHROPIC_PX_PER_TOKEN = 28
#: Standard-tier budget from the same page: an image is resized down until its
#: long edge is at most 1568 px *and* it costs at most 1568 tokens.
ANTHROPIC_MAX_LONG_EDGE = 1568
ANTHROPIC_MAX_TOKENS = 1568


def anthropic_image_tokens(
    width: int,
    height: int,
    *,
    max_long_edge: int = ANTHROPIC_MAX_LONG_EDGE,
    max_tokens: int = ANTHROPIC_MAX_TOKENS,
) -> int:
    """Cost of one image on Anthropic's standard vision tier.

    Anthropic resizes server-side before billing, so an oversized image is
    charged at the size it was shrunk to, not at the size it was sent.
    """
    fitted_w, fitted_h = anthropic_fit(
        width, height, max_long_edge=max_long_edge, max_tokens=max_tokens
    )
    return _patch_count(fitted_w, fitted_h, ANTHROPIC_PX_PER_TOKEN)


def anthropic_fit(
    width: int,
    height: int,
    *,
    max_long_edge: int = ANTHROPIC_MAX_LONG_EDGE,
    max_tokens: int = ANTHROPIC_MAX_TOKENS,
) -> tuple[int, int]:
    """Return the size Anthropic would resize this image to before billing.

    Both constraints bind, and the token one is the tighter of the two on a
    wide image: 1920x1080 goes to 1456x819 (1560 tokens), not to 1568x882,
    because 1568x882 would be 1792 tokens. Claude Code implements exactly this
    as a binary search against ``maxTargetPx: 1568`` and
    ``maxTargetTokens: 1568``, which is what this reproduces.

    The predicate is monotone in the scale factor -- both the long edge and the
    patch count are non-decreasing as the image grows -- so bisection finds the
    largest admissible scale rather than merely an admissible one.
    """
    if width <= 0 or height <= 0:
        return (max(1, width), max(1, height))

    def fits(w: int, h: int) -> bool:
        if max_long_edge > 0 and max(w, h) > max_long_edge:
            return False
        return not (
            max_tokens > 0 and _patch_count(w, h, ANTHROPIC_PX_PER_TOKEN) > max_tokens
        )

    if fits(width, height):
        return (width, height)
    low, high = 0.0, 1.0
    best = (1, 1)
    for _ in range(48):
        mid = (low + high) / 2
        candidate = (max(1, round(width * mid)), max(1, round(height * mid)))
        if fits(*candidate):
            best = candidate
            low = mid
        else:
            high = mid
    return best


# ---------------------------------------------------------------- OpenAI tile

#: developers.openai.com/api/docs/guides/images-vision -- the image is first
#: fitted inside a 2048x2048 square, then scaled so its *short* side is 768 px,
#: then divided into 512x512 tiles. Both steps only ever shrink.
OPENAI_TILE_FIT_SQUARE = 2048
OPENAI_TILE_SHORT_SIDE = 768
OPENAI_TILE_SIZE = 512
#: The gpt-4o / gpt-4.1 price, and the family default. The same page lists
#: 2833 + 5667 for gpt-4o-mini, 70 + 140 for gpt-5 and gpt-5.1, and 75 + 150
#: for o1 / o1-pro / o3; those are passed in by a caller that knows them, never
#: looked up from a model name here.
OPENAI_TILE_BASE = 85
OPENAI_TILE_PER_TILE = 170


def openai_tile_image_tokens(
    width: int,
    height: int,
    *,
    base: int = OPENAI_TILE_BASE,
    per_tile: int = OPENAI_TILE_PER_TILE,
    low_detail: bool = False,
) -> int:
    """Cost of one image on OpenAI's tile-billed vision models.

    ``detail: low`` bills the base only and the model sees a thumbnail. It is
    exposed here so the parity test can assert it, and it is deliberately *not*
    reachable from the request's own ``detail`` setting: keying the estimate on
    a field the Anthropic protocol does not carry is precisely the bug LiteLLM
    ships, where every Anthropic image counts as a flat 85 tokens.
    """
    if width <= 0 or height <= 0:
        return base
    if low_detail:
        return base
    w, h = _shrink_into_square(width, height, OPENAI_TILE_FIT_SQUARE)
    w, h = _shrink_short_side_to(w, h, OPENAI_TILE_SHORT_SIDE)
    tiles = math.ceil(w / OPENAI_TILE_SIZE) * math.ceil(h / OPENAI_TILE_SIZE)
    return base + tiles * per_tile


# --------------------------------------------------------------- OpenAI patch

#: Same page, the newer patch-billed models: the image is cut into 32x32
#: patches, and if that is over the model's budget it is shrunk by
#: ``sqrt((32^2 x budget) / (w x h))``, snapped down to a whole number of
#: patches across, and recounted. The count is then multiplied by the model's
#: own text-token multiplier.
OPENAI_PATCH_SIZE = 32
#: The budget the gpt-5.4/5.5/5.6 families use at their default ``high``
#: detail. Other documented budgets are 6144 (gpt-4.1-mini, gpt-5.2) and 10000
#: (``original`` detail); they are parameters, not a model-name lookup.
OPENAI_PATCH_BUDGET = 2500
#: The multiplier shared by the gpt-5.6 / 5.5 / 5.4 / 5.2 families and
#: gpt-5-mini. Documented alternatives: 1.5 (gpt-5-nano), 1.62 (gpt-4.1-mini),
#: 2.46 (gpt-4.1-nano 2025-04-14), 1.72 (o4-mini).
OPENAI_PATCH_MULTIPLIER = 1.2
#: The same page's hard rejection: an image over this many patches is refused
#: rather than shrunk.
OPENAI_PATCH_HARD_LIMIT = 30_000


def openai_patch_image_tokens(
    width: int,
    height: int,
    *,
    budget: int = OPENAI_PATCH_BUDGET,
    multiplier: float = OPENAI_PATCH_MULTIPLIER,
) -> int:
    """Cost of one image on OpenAI's patch-billed vision models."""
    if width <= 0 or height <= 0:
        return 0
    patches = _patch_count(width, height, OPENAI_PATCH_SIZE)
    if patches > budget:
        shrink = math.sqrt((OPENAI_PATCH_SIZE**2 * budget) / (width * height))
        w = width * shrink
        h = height * shrink
        # Second step, from the same worked pipeline: snap the width down to a
        # whole number of patches and carry the same factor into the height,
        # so the count lands on the budget rather than one patch over it.
        across = math.floor(w / OPENAI_PATCH_SIZE)
        if across >= 1:
            snap = (across * OPENAI_PATCH_SIZE) / w
            w *= snap
            h *= snap
        patches = math.ceil(w / OPENAI_PATCH_SIZE) * math.ceil(h / OPENAI_PATCH_SIZE)
    patches = min(patches, OPENAI_PATCH_HARD_LIMIT)
    return int(patches * multiplier)


# --------------------------------------------------------------------- Gemini

#: ai.google.dev/gemini-api/docs/image-understanding and /docs/tokens -- an
#: image small enough in both dimensions is a flat 258 tokens; anything larger
#: is cropped into tiles whose side is ``floor(min(w, h) / 1.5)``, each tile
#: costing the same 258.
GEMINI_SMALL_EDGE = 384
GEMINI_TOKENS_PER_TILE = 258
GEMINI_CROP_DIVISOR = 1.5


def gemini_image_tokens(width: int, height: int) -> int:
    """Cost of one image on Gemini."""
    if width <= 0 or height <= 0:
        return GEMINI_TOKENS_PER_TILE
    if width <= GEMINI_SMALL_EDGE and height <= GEMINI_SMALL_EDGE:
        return GEMINI_TOKENS_PER_TILE
    crop_unit = math.floor(min(width, height) / GEMINI_CROP_DIVISOR)
    if crop_unit < 1:
        return GEMINI_TOKENS_PER_TILE
    tiles = math.ceil(width / crop_unit) * math.ceil(height / crop_unit)
    return tiles * GEMINI_TOKENS_PER_TILE


# ------------------------------------------------------------------- DeepSeek

#: api-docs.deepseek.com/guides/vision -- the image is rescaled toward the
#: pixel count of roughly 800x800 and the per-image cost is then capped hard at
#: 384 tokens. No sub-cap formula is published, so nothing finer is claimed
#: here: the cap is the documented number and the only documented number.
DEEPSEEK_MAX_TOKENS = 384


def deepseek_image_tokens(width: int, height: int) -> int:
    """Cost of one image on DeepSeek: the documented hard cap."""
    del width, height
    return DEEPSEEK_MAX_TOKENS


# ----------------------------------------------------------------------- Qwen

#: huggingface.co/docs/transformers/en/model_doc/qwen2_vl -- a 14 px patch with
#: a 2x spatial merge, which is one token per 28x28 block, clamped to the
#: min/max-pixels window Model Studio documents.
QWEN_PX_PER_TOKEN = 28
QWEN_MIN_TOKENS = 256
QWEN_MAX_TOKENS = 1280


def qwen_image_tokens(width: int, height: int) -> int:
    """Cost of one image on a Qwen2-VL / Qwen2.5-VL model."""
    if width <= 0 or height <= 0:
        return QWEN_MIN_TOKENS
    raw = _patch_count(width, height, QWEN_PX_PER_TOKEN)
    return max(QWEN_MIN_TOKENS, min(QWEN_MAX_TOKENS, raw))


# -------------------------------------------------------------------- Pixtral

#: docs.mistral.ai/capabilities/vision -- native resolution, 16x16 patches, one
#: token per patch, plus an ``[IMG_BREAK]`` at the end of every row and one
#: ``[IMG_END]``. **UNVERIFIED**: Mistral publishes no worked example, so no
#: provider descriptor is assigned this family; a family that cannot reproduce
#: its own published number ships as ``unknown``, not as a guess. It is
#: implemented and tested for its own arithmetic so that the moment Mistral
#: publishes one, assigning the family is a one-line change.
PIXTRAL_PATCH_SIZE = 16


def pixtral_image_tokens(width: int, height: int) -> int:
    """Cost of one image on Mistral Pixtral. UNVERIFIED -- see the constant."""
    if width <= 0 or height <= 0:
        return 1
    cols = math.ceil(width / PIXTRAL_PATCH_SIZE)
    rows = math.ceil(height / PIXTRAL_PATCH_SIZE)
    return rows * (cols + 1) + 1


# ------------------------------------------------------------------- dispatch


def image_tokens(width: int, height: int, family: str | ImageTokenFamily) -> int:
    """Cost of one image of the given size on the given billing family.

    An unrecognised family name is treated as ``UNKNOWN`` rather than raising:
    the family is operator-editable data on a provider descriptor, and a typo
    in it must degrade the estimate, not fail a live request.
    """
    resolved = resolve_family(family)
    if resolved is ImageTokenFamily.OPENAI_TILE:
        return openai_tile_image_tokens(width, height)
    if resolved is ImageTokenFamily.OPENAI_PATCH:
        return openai_patch_image_tokens(width, height)
    if resolved is ImageTokenFamily.GEMINI:
        return gemini_image_tokens(width, height)
    if resolved is ImageTokenFamily.DEEPSEEK:
        return deepseek_image_tokens(width, height)
    if resolved is ImageTokenFamily.QWEN:
        return qwen_image_tokens(width, height)
    if resolved is ImageTokenFamily.PIXTRAL:
        return pixtral_image_tokens(width, height)
    # ANTHROPIC and UNKNOWN share this line deliberately: the request arrived
    # in the Anthropic protocol, so Anthropic's budget is the one the client is
    # reasoning about, and it is the least wrong guess for a host that
    # publishes nothing. The distinction survives on the descriptor and in the
    # Models page readout, which is where it can be audited.
    return anthropic_image_tokens(width, height)


def resolve_family(family: str | ImageTokenFamily) -> ImageTokenFamily:
    """Return the declared family, or ``UNKNOWN`` for anything unrecognised."""
    if isinstance(family, ImageTokenFamily):
        return family
    try:
        return ImageTokenFamily(str(family).strip().lower())
    except ValueError:
        return ImageTokenFamily.UNKNOWN


def _patch_count(width: int, height: int, patch: int) -> int:
    return math.ceil(width / patch) * math.ceil(height / patch)


def _shrink_into_square(width: int, height: int, square: int) -> tuple[int, int]:
    longest = max(width, height)
    if longest <= square:
        return (width, height)
    scale = square / longest
    return (max(1, math.floor(width * scale)), max(1, math.floor(height * scale)))


def _shrink_short_side_to(width: int, height: int, short: int) -> tuple[int, int]:
    shortest = min(width, height)
    if shortest <= short:
        return (width, height)
    scale = short / shortest
    return (max(1, math.floor(width * scale)), max(1, math.floor(height * scale)))
