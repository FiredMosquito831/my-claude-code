"""models.dev's four display facts walk the generic field ladder (7.85.0).

``release_date``, ``knowledge``, ``description`` and ``status == "deprecated"``
are new ``_LadderField``s and nothing else: the same rungs a price walks (own
bucket 3-4, the OpenRouter copy 5-6 for a provider with no bucket, the
quorum-guarded vote 7-10), the same "a provider with a bucket never reads
outside it" rule, the same memo. A split vote over dates goes to the EARLIEST
of the equally attested days; over the deprecated flag to "not deprecated".
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from my_claude_code.core.model_ids import ResolutionTier
from my_claude_code.providers.runtime.models_dev import (
    DEPRECATED_FIELD,
    RELEASE_DATE_FIELD,
    model_display_facts_lookup,
)

_INDEX = {
    "acme": {
        "models": {
            "acme/flash": {
                "release_date": "2026-01-15",
                "knowledge": "2025-06",
                "description": "Acme's fast model.",
            },
            "acme/old": {"release_date": "2023-02-01", "status": "deprecated"},
            "acme/beta": {"release_date": "2026-09-01", "status": "beta"},
            "acme/undescribed": {"description": "  "},
        }
    },
    "openrouter": {
        "models": {
            "vendor/reference": {
                "release_date": "2025-11-04",
                "knowledge": "2025-03-31",
                "description": "From OpenRouter's catalogue copy.",
                "status": "deprecated",
            }
        }
    },
    # The vote: two reporters say one day, two another -- a tie.
    "votera": {"models": {"vendor/tied": {"release_date": "2025-02-05"}}},
    "voterb": {"models": {"vendor/tied": {"release_date": "2025-06-16"}}},
    "voterc": {"models": {"vendor/tied": {"release_date": "2025-06-16"}}},
    "voterd": {"models": {"vendor/tied": {"release_date": "2025-02-05"}}},
    # Two of three hosts deprecate it, one lists it as current.
    "votere": {"models": {"vendor/retiring": {"status": "deprecated"}}},
    "voterf": {"models": {"vendor/retiring": {"status": "deprecated"}}},
    "voterg": {"models": {"vendor/retiring": {"release_date": "2024-01-01"}}},
    "lonevoter": {"models": {"vendor/undersampled": {"release_date": "2024-05-05"}}},
}


def _cache(tmp_path: Path) -> Path:
    path = tmp_path / "models-dev.json"
    path.write_text(
        json.dumps({"fetched_at": datetime.now(UTC).isoformat(), "index": _INDEX}),
        encoding="utf-8",
    )
    now = datetime.now(UTC).timestamp()
    os.utime(path, (now, now))
    return path


def test_the_own_bucket_answers_at_tier_three(tmp_path: Path) -> None:
    facts = model_display_facts_lookup(_cache(tmp_path))("acme", "acme/flash")
    exact = ResolutionTier.MODELS_DEV_BUCKET_EXACT
    assert facts.release_date == ("2026-01-15", exact)
    assert facts.knowledge_cutoff == ("2025-06", exact)
    assert facts.description == ("Acme's fast model.", exact)
    # Listed with no status: a current model, which is an answer.
    assert facts.deprecated == (False, exact)


def test_the_deprecated_flag_is_a_flag_and_beta_is_not_deprecated(
    tmp_path: Path,
) -> None:
    lookup = model_display_facts_lookup(_cache(tmp_path))
    assert lookup("acme", "acme/old").deprecated == (
        True,
        ResolutionTier.MODELS_DEV_BUCKET_EXACT,
    )
    assert lookup("acme", "acme/beta").deprecated == (
        False,
        ResolutionTier.MODELS_DEV_BUCKET_EXACT,
    )


def test_a_bucketed_provider_never_reads_outside_its_bucket(tmp_path: Path) -> None:
    facts = model_display_facts_lookup(_cache(tmp_path))("acme", "vendor/reference")
    assert facts.release_date == (None, None)
    assert facts.description == (None, None)
    # A blank description in its own bucket states nothing either.
    blank = model_display_facts_lookup(_cache(tmp_path))("acme", "acme/undescribed")
    assert blank.description == (None, None)


def test_a_bucket_less_provider_reads_the_openrouter_copy(tmp_path: Path) -> None:
    facts = model_display_facts_lookup(_cache(tmp_path))("resold", "vendor/reference")
    assert facts.release_date == ("2025-11-04", ResolutionTier.OPENROUTER_EXACT)
    assert facts.knowledge_cutoff == ("2025-03-31", ResolutionTier.OPENROUTER_EXACT)
    assert facts.deprecated == (True, ResolutionTier.OPENROUTER_EXACT)


def test_a_tied_vote_goes_to_the_earliest_day(tmp_path: Path) -> None:
    facts = model_display_facts_lookup(_cache(tmp_path))("resold", "vendor/tied")
    assert facts.release_date == ("2025-02-05", ResolutionTier.CROSS_PROVIDER_EXACT)


def test_the_earliest_tie_break_orders_days_and_months() -> None:
    order = sorted(
        ["2025-06-16", "2025-02-05", "2025-02", "2024-12-31"],
        key=RELEASE_DATE_FIELD.tie_break,
        reverse=True,
    )
    # ``max`` picks the first of this order: the earliest day, a month alone
    # counting as its first day.
    assert order == ["2024-12-31", "2025-02", "2025-02-05", "2025-06-16"]


def test_a_deprecation_vote_takes_a_majority_and_ties_say_current(
    tmp_path: Path,
) -> None:
    facts = model_display_facts_lookup(_cache(tmp_path))("resold", "vendor/retiring")
    assert facts.deprecated == (True, ResolutionTier.CROSS_PROVIDER_EXACT)
    assert max([True, False], key=DEPRECATED_FIELD.tie_break) is False


def test_below_the_quorum_nothing_is_stated(tmp_path: Path) -> None:
    facts = model_display_facts_lookup(_cache(tmp_path))(
        "resold", "vendor/undersampled"
    )
    assert facts.release_date == (None, None)


def test_no_catalogue_on_disk_states_nothing(tmp_path: Path) -> None:
    facts = model_display_facts_lookup(tmp_path / "missing.json")("acme", "acme/flash")
    assert facts.release_date == (None, None)
    assert facts.deprecated == (None, None)
