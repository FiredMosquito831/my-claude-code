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
    hiding_pattern,
    is_exact_ref_under,
    migrate_exact_patterns_to_globs,
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


def test_apply_visibility_bulk_hide_whole_provider_shadows_prior_exact_patterns():
    """Changed deliberately in 6.24.0: the glob shadows, it does not delete.

    Deleting was defensible on the reasoning that ``open_router/*`` subsumes
    ``open_router/a`` -- until Show all, at which point the per-model choices
    made before the Hide all were gone rather than restored. That is the
    "forced by the show/hide logic" half of the user's report.
    """

    base = ModelVisibility(deny=("open_router/a", "*:free"))

    outcome = apply_visibility_bulk(
        base,
        action="hide",
        refs=["open_router/a", "open_router/b"],
        provider_id="open_router",
        whole_provider=True,
    )

    assert outcome.visibility.deny == ("open_router/a", "*:free", "open_router/*")
    assert outcome.removed_patterns == ()
    assert outcome.wrote_glob == "open_router/*"


def test_hide_all_then_show_all_restores_per_model_patterns():
    """The round trip is an identity, which is what makes Hide all safe."""

    base = ModelVisibility(deny=("open_router/a", "*:free"))
    refs = ["open_router/a", "open_router/b"]

    hidden = apply_visibility_bulk(
        base, action="hide", refs=refs, provider_id="open_router", whole_provider=True
    )
    shown = apply_visibility_bulk(
        hidden.visibility,
        action="show",
        refs=refs,
        provider_id="open_router",
        whole_provider=True,
    )

    assert shown.visibility == base
    assert shown.removed_patterns == ("open_router/*",)


def test_show_all_without_a_glob_to_lift_clears_the_exact_patterns():
    """Second press: with the glob gone, what still hides these models is the
    exact patterns their own ticks wrote, and Show all owns those."""

    base = ModelVisibility(deny=("open_router/a", "*:free"))

    outcome = apply_visibility_bulk(
        base,
        action="show",
        refs=["open_router/a", "open_router/b"],
        provider_id="open_router",
        whole_provider=True,
    )

    assert outcome.visibility.deny == ("*:free",)
    assert outcome.removed_patterns == ("open_router/a",)


def test_hide_selected_on_mixed_selection_hides_every_ref():
    """ "Hide" on a mixed selection is not a toggle; it is an assertion."""

    base = ModelVisibility(deny=("p/b",))

    outcome = apply_visibility_bulk(base, action="hide", refs=["p/a", "p/b", "p/c"])

    assert outcome.wanted == {"p/a": False, "p/b": False, "p/c": False}
    assert all(not outcome.visibility.is_visible(ref) for ref in ("p/a", "p/b", "p/c"))


def test_invert_targets_are_computed_before_any_mutation():
    """Folding as we go would let the first write decide the second target."""

    base = ModelVisibility(deny=("p/b", "p/c"))

    outcome = apply_visibility_bulk(base, action="invert", refs=["p/a", "p/b", "p/c"])

    assert outcome.wanted == {"p/a": False, "p/b": True, "p/c": True}


def test_a_selection_covering_a_whole_provider_still_writes_exact_patterns():
    """Promotion to ``provider/*`` is offered by the page, never taken here.

    A selection is a fact about a closed set; a glob is a standing policy that
    will also hide models the provider has not published yet. Only the client's
    explicit whole-provider gesture -- ``scope=provider`` with no refs -- says
    the user meant the second.
    """

    base = ModelVisibility()

    outcome = apply_visibility_bulk(
        base,
        action="hide",
        refs=["p/a", "p/b"],
        provider_id="p",
        whole_provider=False,
    )

    assert outcome.wrote_glob is None
    assert outcome.visibility.deny == ("p/a", "p/b")


def test_hiding_pattern_stays_silent_about_a_row_hidden_by_its_own_tick():
    """ "Hidden" and "Hidden by <glob>" are different rows: the first the
    selection can change, the second it cannot."""

    visibility = ModelVisibility(deny=("p/a", "q/*"))

    assert hiding_pattern(visibility, "p/a") == ""
    assert hiding_pattern(visibility, "q/a") == "q/*"
    assert hiding_pattern(visibility, "p/b") == ""


def test_hiding_pattern_names_the_glob_even_when_the_exact_pattern_is_there_too():
    """The glob is what a Show would fail against, so it is what to name."""

    visibility = ModelVisibility(deny=("q/a", "q/*"))

    assert hiding_pattern(visibility, "q/a") == "q/*"


def test_hiding_pattern_names_the_allow_list_when_that_is_what_excludes_a_ref():
    assert hiding_pattern(ModelVisibility(allow=("other/*",)), "p/a") == (
        ALLOW_LIST_SENTINEL
    )


def test_migrate_folds_a_wholly_hidden_provider_into_one_glob():
    visibility = ModelVisibility(deny=("p/a", "p/b", "p/c", "q/a"))
    catalogue = {"p": ["p/a", "p/b", "p/c"], "q": ["q/a", "q/b"]}

    migration = migrate_exact_patterns_to_globs(visibility, catalogue)

    assert migration.providers == ("p",)
    assert migration.removed_patterns == ("p/a", "p/b", "p/c")
    assert migration.added_patterns == ("p/*",)
    assert migration.visibility.deny == ("q/a", "p/*")
    assert migration.identical is True


def test_migrate_never_changes_what_is_visible():
    """The identity is the entire licence for rewriting somebody's patterns."""

    visibility = ModelVisibility(deny=("p/a", "p/b", "p/c", "q/a"))
    catalogue = {"p": ["p/a", "p/b", "p/c"], "q": ["q/a", "q/b"]}

    migration = migrate_exact_patterns_to_globs(visibility, catalogue)

    for refs in catalogue.values():
        for ref in refs:
            assert migration.visibility.is_visible(ref) == visibility.is_visible(ref)
    assert migration.hidden_before == migration.hidden_after == 4


def test_migrate_leaves_a_provider_with_one_visible_model_alone():
    visibility = ModelVisibility(deny=("p/a", "p/b"))
    catalogue = {"p": ["p/a", "p/b", "p/c"]}

    migration = migrate_exact_patterns_to_globs(visibility, catalogue)

    assert migration.providers == ()
    assert migration.visibility == visibility


def test_migrate_leaves_a_provider_already_hidden_by_a_broader_glob_alone():
    """Nothing to save: ``*:free`` is already one pattern."""

    visibility = ModelVisibility(deny=("*:free",))
    catalogue = {"p": ["p/a:free", "p/b:free"]}

    migration = migrate_exact_patterns_to_globs(visibility, catalogue)

    assert migration.providers == ()
    assert migration.removed_patterns == ()


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
        # Two questions, two fields: what stopped this gesture, and what
        # dictates the row's state from now on.
        "blocked_by": "*:free",
        "hidden_by": "*:free",
    }
    assert rows[1]["honored"] is True


def test_bulk_result_rows_names_the_allow_list_when_it_is_what_excludes_a_ref():
    visibility = ModelVisibility(allow=("other/*",))

    rows = bulk_result_rows(visibility, {"p/a": True})

    assert rows[0]["blocked_by"] == ALLOW_LIST_SENTINEL
