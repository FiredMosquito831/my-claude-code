"""The pure functions behind one bulk visibility gesture.

The route tests prove the endpoint; these prove the rule the endpoint applies,
which is the load-bearing decision of the Models page redesign: a whole-provider
action is a *policy* and is written as one glob, a picked selection is a *fact*
about a closed set and is written as exact patterns, and neither ever deletes a
pattern the user typed by hand.
"""

from my_claude_code.api.model_admin import (
    ALLOW_LIST_SENTINEL,
    apply_visibility_bulk,
    apply_visibility_toggle,
    bulk_result_rows,
    is_exact_ref_under,
    provider_glob,
)
from my_claude_code.core.model_visibility import ModelVisibility


def test_provider_glob_is_the_only_definition_of_the_star_suffix():
    """The writer, the remover and the tests must not each spell it out."""

    assert provider_glob("Nous_Portal") == "nous_portal/*"


def test_is_exact_ref_under_accepts_a_wildcard_free_ref_and_rejects_a_glob():
    assert is_exact_ref_under("open_router/routed", "open_router")
    assert not is_exact_ref_under("open_router/*", "open_router")
    assert not is_exact_ref_under("open_router/a[b]", "open_router")
    assert not is_exact_ref_under("*:free", "open_router")


def test_is_exact_ref_under_does_not_claim_a_pattern_from_a_prefix_sharing_provider():
    """``open_router_x/a`` is not under ``open_router``, and deleting it would
    be an unrecoverable edit to a different provider's list."""

    assert not is_exact_ref_under("open_router_x/a", "open_router")


def test_apply_visibility_bulk_hide_whole_provider_collapses_prior_exact_patterns():
    base = ModelVisibility(deny=("open_router/a", "*:free"))

    outcome = apply_visibility_bulk(
        base,
        action="hide",
        refs=["open_router/a", "open_router/b"],
        provider_id="open_router",
        whole_provider=True,
    )

    assert outcome.visibility.deny == ("*:free", "open_router/*")
    assert outcome.removed_patterns == ("open_router/a",)
    assert outcome.wrote_glob == "open_router/*"


def test_apply_visibility_bulk_is_idempotent_when_the_glob_already_exists():
    """A user who wrote the pattern by hand and then clicks Hide all changed
    nothing, and the panel should say so rather than claim a write."""

    base = ModelVisibility(deny=("open_router/*",))

    outcome = apply_visibility_bulk(
        base,
        action="hide",
        refs=["open_router/a"],
        provider_id="open_router",
        whole_provider=True,
    )

    assert outcome.visibility.deny == ("open_router/*",)
    assert outcome.wrote_glob is None


def test_apply_visibility_bulk_reuses_the_single_toggle_rule_for_a_selection():
    """Fold-equivalence is the guard against the two paths diverging."""

    base = ModelVisibility(deny=("*:free",))
    refs = ["p/a", "p/b", "p/c"]

    outcome = apply_visibility_bulk(base, action="hide", refs=refs)

    folded = base
    for ref in refs:
        folded = apply_visibility_toggle(folded, ref, visible=False)
    assert outcome.visibility == folded
    assert outcome.wrote_glob is None


def test_apply_visibility_bulk_invert_reads_the_state_before_it_writes():
    base = ModelVisibility(deny=("p/b",))

    outcome = apply_visibility_bulk(base, action="invert", refs=["p/a", "p/b"])

    assert outcome.wanted == {"p/a": False, "p/b": True}
    assert outcome.wrote_glob is None
    assert outcome.visibility.is_visible("p/b")
    assert not outcome.visibility.is_visible("p/a")


def test_apply_visibility_bulk_show_under_an_opt_in_allow_list_names_the_provider():
    base = ModelVisibility(allow=("other/*",))

    outcome = apply_visibility_bulk(
        base,
        action="show",
        refs=["p/a"],
        provider_id="p",
        whole_provider=True,
    )

    assert outcome.visibility.allow == ("other/*", "p/*")


def test_bulk_result_rows_names_the_glob_that_overruled_an_exact_tick():
    visibility = ModelVisibility(deny=("*:free",))

    rows = bulk_result_rows(visibility, {"p/a:free": True, "p/b": True})

    assert rows[0] == {
        "model_ref": "p/a:free",
        "visible": False,
        "honored": False,
        "blocked_by": "*:free",
    }
    assert rows[1]["honored"] is True


def test_bulk_result_rows_names_the_allow_list_when_it_is_what_excludes_a_ref():
    visibility = ModelVisibility(allow=("other/*",))

    rows = bulk_result_rows(visibility, {"p/a": True})

    assert rows[0]["blocked_by"] == ALLOW_LIST_SENTINEL
