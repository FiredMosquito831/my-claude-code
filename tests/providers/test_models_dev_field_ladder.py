"""The tiered siblings walk the same ten rungs the output limit already did.

``model_output_limit_tiered`` answered 124 of 128 models on a real install
while ``context_length``, ``supports_vision``, ``supports_tool_calls`` and the
prices reached the catalogue through ``enrich_model_infos`` -- a flat,
provider-blind, un-tiered name match that consults no reference bucket, strips
no routing tag and records no rung. These tests pin that the new lookups are
the *same* ladder, rung for rung, and that the guards that keep it honest --
the "a provider with a bucket never reads outside it" rule and the
minimum-sample quorum -- apply to them too.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from my_claude_code.core.model_ids import ResolutionTier
from my_claude_code.providers.runtime.models_dev import (
    model_context_length_tiered,
    model_prices_tiered,
    model_tool_call_tiered,
    model_vision_tiered,
)

#: Three buckets and a reference catalogue, shaped like the live index:
#:
#: * ``acme`` has a bucket of its own and publishes everything.
#: * ``openrouter`` is the reference rung (tiers 5-6).
#: * ``votera``/``voterb``/``voterc`` exist only to be counted by the
#:   cross-provider quorum, which needs three reporters.
#:
#: ``resold`` is deliberately absent: a gateway models.dev does not describe is
#: exactly the case the flat match could never answer.
_INDEX = {
    "acme": {
        "models": {
            "acme/flash": {
                "limit": {"context": 131072, "output": 8192},
                "cost": {
                    "input": 0.1,
                    "output": 0.2,
                    "cache_read": 0.01,
                    "cache_write": 0.5,
                },
                "modalities": {"input": ["text", "image"]},
                "tool_call": True,
            },
            "acme/silent": {"limit": {"output": 4096}},
        }
    },
    "openrouter": {
        "models": {
            "vendor/reference-model:free": {
                "limit": {"context": 262144, "output": 16384},
                "cost": {"input": 1.0, "output": 2.0},
                "modalities": {"input": ["text"]},
                "tool_call": False,
            }
        }
    },
    "votera": {
        "models": {
            "vendor/voted": {
                "limit": {"context": 1048576},
                "tool_call": True,
                "modalities": {"input": ["text", "image"]},
                "cost": {"input": 5.0, "output": 6.0},
            }
        }
    },
    "voterb": {
        "models": {
            "vendor/voted": {
                "limit": {"context": 1048576},
                "tool_call": True,
                "modalities": {"input": ["text", "image"]},
                "cost": {"input": 5.0, "output": 6.0},
            }
        }
    },
    "voterc": {
        "models": {
            "vendor/voted": {
                "limit": {"context": 1048576},
                "tool_call": True,
                "modalities": {"input": ["text", "image"]},
                "cost": {"input": 5.0, "output": 6.0},
            }
        }
    },
    "lonevoter": {
        "models": {
            "vendor/undersampled": {
                "limit": {"context": 999_999},
                "tool_call": True,
            }
        }
    },
}


def _cache(tmp_path: Path) -> Path:
    path = tmp_path / "models-dev.json"
    path.write_text(
        json.dumps({"fetched_at": datetime.now(UTC).isoformat(), "index": _INDEX}),
        encoding="utf-8",
    )
    # Every index here is memoized per (path, mtime); a fresh tmp_path per test
    # is what keeps them from sharing one.
    now = datetime.now(UTC).timestamp()
    os.utime(path, (now, now))
    return path


def test_the_providers_own_bucket_answers_every_field_at_tier_three(
    tmp_path: Path,
) -> None:
    path = _cache(tmp_path)

    assert model_context_length_tiered("acme", "acme/flash", path) == (
        131072,
        ResolutionTier.MODELS_DEV_BUCKET_EXACT,
    )
    assert model_vision_tiered("acme", "acme/flash", path) == (
        True,
        ResolutionTier.MODELS_DEV_BUCKET_EXACT,
    )
    assert model_tool_call_tiered("acme", "acme/flash", path) == (
        True,
        ResolutionTier.MODELS_DEV_BUCKET_EXACT,
    )
    prices = model_prices_tiered("acme", "acme/flash", path)
    assert prices["input_price"] == (0.1, ResolutionTier.MODELS_DEV_BUCKET_EXACT)
    assert prices["cache_read_price"] == (0.01, ResolutionTier.MODELS_DEV_BUCKET_EXACT)
    assert prices["cache_write_price"] == (0.5, ResolutionTier.MODELS_DEV_BUCKET_EXACT)


def test_a_field_its_own_bucket_omits_is_unknown_not_borrowed(tmp_path: Path) -> None:
    """The rule that keeps a wrong same-name row from overruling a catalogue.

    ``acme`` has a bucket, so nothing outside it may answer for an ``acme``
    model -- even a field ``acme`` left blank. Unknown, not a guess.
    """

    path = _cache(tmp_path)

    assert model_context_length_tiered("acme", "acme/silent", path) == (None, None)
    assert model_tool_call_tiered("acme", "acme/silent", path) == (None, None)


def test_a_gateway_with_no_bucket_reaches_the_reference_rung(tmp_path: Path) -> None:
    """Tiers 5-6, which the flat enrichment match never consults at all.

    The id is respelled on the way -- ``vendor/reference-model-free`` against a
    catalogue that lists ``vendor/reference-model:free`` -- because one routing
    variant of one model written two ways is still an exact match on the model.
    """

    path = _cache(tmp_path)

    assert model_context_length_tiered(
        "resold", "vendor/reference-model-free", path
    ) == (262144, ResolutionTier.OPENROUTER_EXACT)
    assert model_vision_tiered("resold", "vendor/reference-model-free", path) == (
        False,
        ResolutionTier.OPENROUTER_EXACT,
    )
    # A published ``false`` is a statement and survives; only silence is None.
    assert model_tool_call_tiered("resold", "vendor/reference-model-free", path) == (
        False,
        ResolutionTier.OPENROUTER_EXACT,
    )


def test_the_cross_provider_vote_answers_only_with_a_real_sample(
    tmp_path: Path,
) -> None:
    """Tiers 7-10, under the same three-reporter quorum as the output limit.

    Three rows agreeing is a vote. One row is a transcription that always
    agrees with itself, and it is how a single models.dev entry once credited a
    gateway's free model with a 1M-token limit at "100% agreement".
    """

    path = _cache(tmp_path)

    assert model_context_length_tiered("resold", "vendor/voted", path) == (
        1048576,
        ResolutionTier.CROSS_PROVIDER_EXACT,
    )
    assert model_tool_call_tiered("resold", "vendor/voted", path) == (
        True,
        ResolutionTier.CROSS_PROVIDER_EXACT,
    )
    assert model_prices_tiered("resold", "vendor/voted", path)["output_price"] == (
        6.0,
        ResolutionTier.CROSS_PROVIDER_EXACT,
    )

    # One reporter is below the quorum, so the honest answer is unknown.
    assert model_context_length_tiered("resold", "vendor/undersampled", path) == (
        None,
        None,
    )
    assert model_tool_call_tiered("resold", "vendor/undersampled", path) == (None, None)


def test_nothing_anywhere_publishes_it_and_the_answer_stays_none(
    tmp_path: Path,
) -> None:
    path = _cache(tmp_path)

    assert model_context_length_tiered("resold", "vendor/never-heard-of-it", path) == (
        None,
        None,
    )
    prices = model_prices_tiered("resold", "vendor/never-heard-of-it", path)
    assert set(prices) == {
        "input_price",
        "output_price",
        "cache_read_price",
        "cache_write_price",
    }
    assert all(value == (None, None) for value in prices.values())


def test_a_published_zero_context_reads_as_absent(tmp_path: Path) -> None:
    """models.dev's schema permits ``limit.context: 0`` and means unknown by it.

    132 live rows publish it. Zero must never become a ceiling of zero tokens,
    which is a number a context manager acts on.
    """

    path = tmp_path / "models-dev.json"
    path.write_text(
        json.dumps(
            {
                "fetched_at": datetime.now(UTC).isoformat(),
                "index": {
                    "acme": {"models": {"acme/zeroed": {"limit": {"context": 0}}}}
                },
            }
        ),
        encoding="utf-8",
    )

    assert model_context_length_tiered("acme", "acme/zeroed", path) == (None, None)
