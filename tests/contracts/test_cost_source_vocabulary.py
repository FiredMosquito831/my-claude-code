"""``cost_source`` is a wire contract, so its vocabulary is pinned in one place.

The column is stored in the request log, exported by the CSV writer and
rendered by the dashboard; 6.54.0's own docstring calls renaming one of its
values "rewriting history's labels". The historical backfill adds three
retroactive spellings and one sentinel to it, and this file is what stops the
four surfaces that read the column from drifting apart about what may appear
in it.
"""

import re
from pathlib import Path

from my_claude_code.application.cost import (
    ALL_COST_SOURCES,
    BACKFILL_COST_SOURCES,
    COST_SOURCES,
    SOURCE_PROVIDER,
    SOURCE_UNPRICED,
    retroactive_source,
)
from my_claude_code.core import request_log as request_log_module

ADMIN_JS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "my_claude_code"
    / "api"
    / "admin_static"
    / "admin.js"
)


def _js_cost_source_labels() -> dict[str, str]:
    source = ADMIN_JS.read_text(encoding="utf-8")
    match = re.search(
        r"const COST_SOURCE_LABELS = \{(.*?)\n\};", source, flags=re.DOTALL
    )
    assert match is not None, "COST_SOURCE_LABELS is not where this test looks"
    return dict(re.findall(r"(\w+): \"([^\"]+)\"", match.group(1)))


def test_cost_sources_are_a_closed_set() -> None:
    """Every value the column may carry, once each, and nothing else."""
    assert len(ALL_COST_SOURCES) == len(set(ALL_COST_SOURCES))
    assert set(ALL_COST_SOURCES) == {
        *COST_SOURCES,
        *BACKFILL_COST_SOURCES,
        SOURCE_UNPRICED,
    }


def test_a_reported_cost_has_no_retroactive_spelling() -> None:
    """The ``provider`` rung is a figure a host reported, and it is gone.

    Every other rung is a calculation that can be redone from a published rate
    card; a reported cost cannot be reconstructed, so there is deliberately no
    ``provider_backfill`` for a backfill to reach for.
    """
    assert retroactive_source(SOURCE_PROVIDER) is None
    assert "provider_backfill" not in ALL_COST_SOURCES
    computed = [source for source in COST_SOURCES if source != SOURCE_PROVIDER]
    assert [retroactive_source(source) for source in computed] == list(
        BACKFILL_COST_SOURCES
    )


def test_a_source_outside_the_ladder_gets_no_retroactive_label() -> None:
    """Inventing a label is the one thing the contract forbids."""
    assert retroactive_source("something_else") is None
    assert retroactive_source(SOURCE_UNPRICED) is None


def test_core_and_application_agree_on_the_unpriced_marker() -> None:
    """``core`` may not import the vocabulary, so the two spellings are pinned.

    ``cost_breakdown`` has to keep the sentinel out of "priced by", which means
    ``core`` has to know how it is spelled. This is the seam that keeps the
    copy honest.
    """
    assert request_log_module._UNPRICED_COST_SOURCE == SOURCE_UNPRICED


def test_every_cost_source_has_a_label() -> None:
    """A value with no label renders as its raw column value to a reader."""
    assert set(_js_cost_source_labels()) == set(ALL_COST_SOURCES)


def test_a_backfilled_label_says_it_was_priced_later() -> None:
    """The label is the whole point: an estimate must read as one."""
    labels = _js_cost_source_labels()
    for source in BACKFILL_COST_SOURCES:
        assert "later" in labels[source]
