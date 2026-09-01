"""Guards for the dashboard's view registry, and for the Models page's wiring.

A settings page is a spine of independent links, and a broken one is silent.
The trap this file exists for is real and shipped once: a view registered with
no settings container made ``byId(view.containerId).innerHTML = ""`` throw
inside the shared render loop, which aborted the loop -- so adding one static
page blanked *every* tab, not just the new one. Both call sites now guard on
``containerId``, and both guards are asserted here.

Static assertions on the shipped JavaScript rather than a browser: the project
needs UI guards that run on every platform, not a runtime check that silently
skips wherever node or jsdom is missing.
"""

import re
from pathlib import Path

STATIC = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "my_claude_code"
    / "api"
    / "admin_static"
)
ADMIN_JS = STATIC / "admin.js"
ADMIN_HTML = STATIC / "index.html"
ADMIN_CSS = STATIC / "admin.css"

_VIEW_GROUPS_BLOCK = re.compile(r"const VIEW_GROUPS = \[(.*?)\n\];", re.DOTALL)
_VIEW_ENTRY = re.compile(
    r"\bid:\s*\"(?P<id>[^\"]+)\".*?\bcontainerId:\s*(?P<container>null|\"[^\"]+\")",
    re.DOTALL,
)


def _script() -> str:
    return ADMIN_JS.read_text(encoding="utf-8")


def _markup() -> str:
    return ADMIN_HTML.read_text(encoding="utf-8")


def _view_entries() -> dict[str, str | None]:
    block = _VIEW_GROUPS_BLOCK.search(_script())
    assert block is not None, "could not find the VIEW_GROUPS array in admin.js"
    entries: dict[str, str | None] = {}
    for match in _VIEW_ENTRY.finditer(block.group(1)):
        container = match.group("container")
        entries[match.group("id")] = (
            None if container == "null" else container.strip('"')
        )
    assert entries, "parsed no views out of VIEW_GROUPS"
    return entries


def test_the_shared_render_loop_tolerates_a_view_with_no_container() -> None:
    """The §36 trap: an unguarded `byId(null)` blanked every tab, not just one.

    ``renderSections`` walks every view twice -- once to clear, once to fill --
    and both walks must treat a container-less view as "nothing to do" rather
    than dereferencing a null element.
    """

    script = _script()
    guards = script.count("const container = view.containerId ? byId(view.containerId)")

    assert guards == 2, (
        "renderSections() must resolve a view's container through a "
        "`view.containerId ? byId(...) : null` guard at both call sites; found "
        f"{guards}. Without it a static view (containerId: null) throws inside "
        "the shared loop and no view renders at all."
    )
    # And the result must be checked before it is written to.
    assert "if (container) container.innerHTML" in script
    assert "if (!container) return;" in script


def test_every_registered_view_has_markup_and_every_view_is_registered() -> None:
    """A view in one place and not the other is a nav link to nothing."""

    markup = _markup()
    declared = set(re.findall(r'data-view="([^"]+)"', markup))
    registered = set(_view_entries())

    assert registered - declared == set(), (
        "these VIEW_GROUPS ids have no <section data-view=...> in index.html: "
        f"{sorted(registered - declared)}"
    )
    assert declared - registered == set(), (
        "these index.html views are not registered in VIEW_GROUPS, so no nav "
        f"link reaches them: {sorted(declared - registered)}"
    )


def test_every_view_container_exists_in_the_markup() -> None:
    markup = _markup()
    missing = [
        container
        for container in _view_entries().values()
        if container is not None and f'id="{container}"' not in markup
    ]

    assert not missing, (
        f"VIEW_GROUPS names containers index.html does not define: {missing}"
    )


def test_the_models_page_is_registered_as_a_container_less_view() -> None:
    """It owns no manifest section, so it must declare no container at all."""

    entries = _view_entries()

    assert "models" in entries, "the Models page is not registered in VIEW_GROUPS"
    assert entries["models"] is None, (
        "the Models page renders its own markup and claims no settings section, "
        "so containerId must stay null; giving it one would make "
        "renderSections() clear a container nothing ever fills."
    )


def test_the_models_page_is_loaded_when_its_view_is_opened() -> None:
    """Registration without a load hook is a page that renders an empty tree."""

    script = _script()

    assert 'if (activeView.id === "models") {' in script
    assert "loadModelsView()" in script


def test_the_models_page_talks_to_the_endpoints_that_exist() -> None:
    """A typo in a path is a page that only fails once a user clicks it."""

    script = _script()
    routes = Path(
        Path(__file__).resolve().parents[2]
        / "src"
        / "my_claude_code"
        / "api"
        / "admin_routes.py"
    ).read_text(encoding="utf-8")

    for path in (
        "/admin/api/model-admin",
        "/admin/api/model-admin/visibility",
        "/admin/api/model-admin/visibility/preview",
        "/admin/api/model-admin/visibility/bulk",
        "/admin/api/model-admin/visibility/migrate-globs",
        "/admin/api/model-admin/overrides",
    ):
        assert f'"{path}"' in script, f"admin.js never calls {path}"
        assert f'"{path}"' in routes, f"admin_routes.py does not serve {path}"
    # The single-tick route is still served for API callers and still tested,
    # but the page no longer has a second write path to reach it with.
    assert '"/admin/api/model-admin/visibility/toggle"' in routes


def test_the_override_editor_offers_all_three_states() -> None:
    """An empty text box cannot distinguish "not set" from "force unset"."""

    script = _script()

    assert '["inherit", "Inherit"]' in script
    assert '["unset", "Force unset"]' in script
    assert '["value", "Force value"]' in script
    # The value box is only the third mode's argument.
    assert 'box.disabled = mode.value !== "value";' in script


def test_the_approximate_capability_tier_is_visually_distinct() -> None:
    """A one-sample cross-provider guess must not look like a published number."""

    script = _script()
    styles = ADMIN_CSS.read_text(encoding="utf-8")

    assert "models-source-${field.source}" in script
    assert "if (field.approximate) {" in script
    assert "same-named row(s)" in script
    assert ".models-source-approximate" in styles
    assert ".models-source-provider" in styles


def test_the_models_page_never_interpolates_a_model_ref_into_markup() -> None:
    """Model refs are upstream text; the page builds nodes, not HTML strings."""

    script = _script()
    models_section = script[
        script.index(
            "/* ----------------------------------------------------------------- models"
        ) :
    ]

    assert ".innerHTML" not in models_section, (
        "the Models page must build its DOM with createElement/textContent: a "
        "model ref is upstream text and half of it is user-typed configuration."
    )


def _models_section() -> str:
    script = _script()
    return script[
        script.index(
            "/* ----------------------------------------------------------------- models"
        ) :
    ]


def test_the_tree_is_built_lazily_rather_than_all_at_once() -> None:
    """The defect a browser found: 186,290 nodes before the user opened one row.

    Driving the shipped page against a real install (1,021 models over 10
    providers) put 9,279 ``<select>`` elements and 186k nodes into the tree
    while every provider was still collapsed, because the render walked every
    provider's every model and built a full override editor and two tables for
    each. Provider bodies, model bodies and the provider's own override editor
    are now filled on first open, and the model list is paged.
    """

    section = _models_section()

    # Every deferred body is guarded by the same filled-once marker, so a
    # second toggle does not duplicate the contents.
    assert section.count('body.dataset.filled = "1";') >= 2, (
        "provider and model bodies must both be filled on first open"
    )
    assert 'editor.dataset.filled = "1";' in section, (
        "a provider's own override editor must also be built on first open"
    )
    assert "if (node.open) fill();" in section, (
        "a row restored open by modelsState.open must still get a body"
    )
    assert "const MODELS_PAGE_SIZE" in section, (
        "a provider with hundreds of models must page its rows, not render "
        "them all: nous_portal alone publishes 317"
    )
    assert "models.slice(already, already + MODELS_PAGE_SIZE)" in section


def test_a_filter_does_not_force_every_matching_provider_open() -> None:
    """Typing three letters used to unfold nine providers at once."""

    section = _models_section()

    assert "matching.length === 1" in section, (
        "a filter may auto-open a provider only when it is the single match; "
        "opening all of them is how a short search produced twelve thousand "
        "pixels of page"
    )
    assert "window.setTimeout" in section and "modelsState.filter = next;" in section, (
        "filtering a thousand refs on every keystroke must be debounced"
    )


def test_the_visibility_tick_is_not_inside_a_summary_element() -> None:
    """Chrome reported "interactive element inside a summary" 1,021 times.

    The checkbox is now a sibling of the ``<details>``, which also removes the
    ``stopPropagation`` that stopped ticking a box from folding the row.
    """

    section = _models_section()

    assert "models-model-row" in section
    assert "row.appendChild(buildModelNode(model, editable));" in section
    assert "event.stopPropagation()" not in section, (
        "a checkbox that needs stopPropagation to avoid folding its own row is "
        "a checkbox in the wrong place"
    )
    # The summary carries the ref and its chips, and no form control.
    summary_builder = section[
        section.index("function buildModelSummary(") : section.index(
            "function fillModelBody("
        )
    ]
    assert 'createElement("input")' not in summary_builder
    assert 'createElement("select")' not in summary_builder


def test_saving_an_override_does_not_destroy_its_own_status_element() -> None:
    """Verified in a browser: a save that worked showed no confirmation at all.

    ``renderModelsPage()`` rebuilt the whole tree, so the "Saved" text landed
    in a node that had already been replaced -- and every other open editor's
    unsaved edits went with it. A save now patches the payload and repaints
    only the affected rows' read-only panels.
    """

    section = _models_section()

    save_body = section[
        section.index("async function saveModelOverrides(") : section.index(
            "function renderModelsPreview("
        )
    ]
    assert "renderModelsPage()" not in save_body, (
        "saving must not rebuild the tree: it discards unsaved edits in every "
        "other open editor and destroys the button's own status element"
    )
    assert "refreshProviderRow(key)" in save_body
    assert "refreshModelRows([key])" in save_body
    # The refresh repaints the read-only panels, never the editor around them.
    assert "function fillModelReadouts(" in section
    assert "if (readouts) fillModelReadouts(readouts, model);" in section


def test_force_value_refuses_an_empty_box() -> None:
    """Force value with a blank box forced an empty string upstream."""

    section = _models_section()

    assert "blank.push(name);" in section
    assert "if (blank.length) {" in section
    assert "back to Inherit or Force unset" in section


def test_the_override_grid_names_its_columns() -> None:
    """A parameter, a select and a box with no headers do not say which is which."""

    section = _models_section()
    styles = ADMIN_CSS.read_text(encoding="utf-8")

    assert '["Parameter", "What to send", "Value"]' in section
    assert ".models-override-head" in styles


def test_the_page_has_no_heading_without_controls_under_it() -> None:
    """The middle of the three sections was a heading and three paragraphs.

    The override explanation now lives beside the editors it explains, inside
    the tree section, so every heading on the page owns something the user can
    operate.
    """

    markup = _markup()
    view = markup[
        markup.index('id="view-models"') : markup.index('id="view-messaging"')
    ]

    assert 'id="section-model-parameters"' not in view, (
        "the prose-only Parameter overrides section must not come back: it "
        "put a third competing heading between the two that had controls"
    )
    assert 'id="section-model-visibility"' in view
    assert 'id="section-model-tree"' in view
    assert 'class="models-help"' in view, (
        "the override explanation belongs beside the editors, as disclosure"
    )
    assert 'id="modelsOwnedElsewhere"' in view
    assert 'id="modelsTreeSummary"' in view, (
        "a collapsed tree must say how much it is hiding"
    )


def test_the_owned_elsewhere_list_is_grouped_by_owner() -> None:
    """It rendered as one run-on line repeating the same clause four times."""

    section = _models_section()

    assert "const byOwner = new Map();" in section
    assert "models-owned-list" in section


def test_the_visibility_labels_do_not_promise_to_block_a_model() -> None:
    """The field said it would never show a model; it is hide-only.

    A denied model is still routed when a chain names it -- documented and
    intended -- so the label must not read like an off switch. The semantics
    are unchanged; only the words are.
    """

    manifest = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "my_claude_code"
        / "config"
        / "admin"
        / "manifest.py"
    ).read_text(encoding="utf-8")

    assert '"Never show these models"' not in manifest, (
        "the deny field must not be labelled as never showing a model: it "
        "hides a model from the listings and does not stop it being routed"
    )
    assert '"Hide these models from the listings"' in manifest
    assert '"Only list these models"' in manifest
    assert "it is simply not listed" in manifest, (
        "the deny help must say out loud that a hidden model still answers"
    )


def test_the_page_says_hiding_is_display_only_where_the_user_ticks() -> None:
    """The toast after a tick has to repeat what the field label promises."""

    section = _models_section()

    assert "Routing is unaffected either way." in section
    markup = _markup()
    view = markup[
        markup.index('id="view-models"') : markup.index('id="view-messaging"')
    ]
    assert "Hiding wins over showing" in view
    assert "an exact-match pattern per model" in view
    assert "Hiding never affects routing" in view


def test_the_measured_reasoning_chip_names_its_window_and_both_numbers() -> None:
    """Requested and returned are independent facts, and the chip says both."""
    assert "reasoning requested ${measured.requested}/${measured.attempts}" in _script()
    assert "returned ${measured.returned}/${measured.attempts}" in _script()
    assert "measured_days" in _script()


def test_the_measured_chip_is_not_rendered_without_a_measurement() -> None:
    """No traffic is not a measured zero, so no chip rather than a zeroed one."""
    assert "if (measured && measured.attempts) {" in _script()


def _bulk_slice() -> str:
    """The bulk runner, up to the repaint it hands its result to."""

    section = _models_section()
    return section[
        section.index("async function runModelsBulk(") : section.index(
            "function applyModelsBulkResult("
        )
    ]


def test_the_models_page_has_exactly_one_write_path_for_visibility() -> None:
    """The report was "2 overlapping functions for showing/hiding", and it was.

    A per-row tick wrote through ``/visibility/toggle`` and then re-read the
    whole 3.4 MB catalogue; the select column wrote through ``/visibility/bulk``
    and patched the payload in place. The two disagreed about who owned client
    state, and the row repainted differently depending on which had touched it.
    There is now one endpoint, and the row carries a readout rather than a
    second checkbox.
    """

    section = _models_section()

    assert '"/admin/api/model-admin/visibility/toggle"' not in section, (
        "a second write path is how the page came to disagree with itself"
    )
    assert "function toggleModelVisibility(" not in section
    assert "function refreshModelRow(" not in section, (
        "two repaint functions is defect D1: one moved the box and left the word"
    )
    assert "function refreshModelRows(" in section
    assert "function applyModelsBulkResult(" in section


def test_the_row_reports_visibility_and_does_not_offer_a_second_checkbox() -> None:
    """A row whose state a glob dictates must not present a control that cannot
    change it -- and the word beside it is a state, not an imperative."""

    section = _models_section()
    row_builder = section[
        section.index("function buildModelRow(") : section.index(
            "function buildModelsVisibilityState("
        )
    ]
    assert 'select.className = "models-select";' in row_builder
    assert row_builder.count('createElement("input")') == 1, (
        "the gutter select is the only input on the row"
    )
    state_builder = section[
        section.index("function fillModelsVisibilityState(") : section.index(
            "function modelsPatternName("
        )
    ]
    assert 'createElement("input")' not in state_builder
    assert 'model.visible ? "Shown" : "Hidden"' in state_builder
    assert '"Show"' not in state_builder


def test_a_glob_overruled_row_names_the_pattern_and_offers_to_remove_it() -> None:
    """Not a toast, and not a silently disabled control."""

    section = _models_section()

    assert "model.hidden_by" in section
    assert "function offerModelsPatternRemoval(" in section
    assert "async function removeModelsDenyPattern(" in section
    assert "`remove ${pattern}?`" in section, "the removal is confirmed in place"


def test_the_action_bar_says_how_much_of_the_selection_is_already_done() -> None:
    """No tri-state control; the count instead."""

    section = _models_section()

    assert '["hide", "Hide", already.hidden, "already hidden"]' in section
    assert "`${label} ${count} selected${suffix}`" in section


def test_a_whole_provider_selection_is_offered_a_glob_and_never_given_one() -> None:
    """Automatic promotion is lossy in the way Hide all used to be."""

    section = _models_section()

    assert "function modelsWholeProviderSelection(" in section
    assert "`Hide all as one pattern, ${whole.glob}`" in section


def test_show_all_explains_the_per_model_states_it_restored() -> None:
    """Lifting a provider glob uncovers the choices it was shadowing, and rows
    that stay hidden are the point of that, not a failure of it."""

    section = _models_section()

    assert "Your per-model choices from before the Hide all are back:" in section
    assert 'pattern.endsWith("/*")' in section


def test_the_glob_migration_is_previewed_before_it_is_written() -> None:
    """994 exact patterns and no globs is worth folding, but not silently."""

    section = _models_section()

    assert '"/admin/api/model-admin/visibility/migrate-globs"' in section
    assert "JSON.stringify({ apply: Boolean(apply) })" in section
    assert "not identical, so it will not be written." in section


def test_the_selection_box_is_not_inside_a_summary_element() -> None:
    """The gutter checkbox is a sibling of the <details>, like the tick.

    Two controls per row is the redesign's biggest legibility bet; putting
    either of them inside a ``<summary>`` would also make it the a11y
    violation Chrome reported 1,021 times before the tick was moved out.
    """

    section = _models_section()
    row_builder = section[
        section.index("function buildModelRow(") : section.index(
            "function buildModelNode("
        )
    ]
    assert 'select.className = "models-select";' in row_builder
    assert "row.appendChild(buildModelNode(model, editable));" in row_builder
    summary_builder = section[
        section.index("function buildModelSummary(") : section.index(
            "function fillModelBody("
        )
    ]
    assert 'createElement("input")' not in summary_builder
    assert 'createElement("select")' not in summary_builder


def test_the_provider_bulk_controls_are_not_inside_a_summary_element() -> None:
    """The provider-side twin of the rule the model rows already follow.

    A ``<summary>`` must be the first child of its ``<details>``, which leaves
    nowhere legal for a select-all checkbox and three buttons; the provider
    header is a button with ``aria-expanded`` and a sibling body instead.
    """

    section = _models_section()
    builder = section[
        section.index("function buildModelsProviderNode(") : section.index(
            "function providerHasOverrides("
        )
    ]
    assert 'createElement("summary")' not in builder
    assert 'toggle.setAttribute("aria-expanded"' in builder
    assert "head.appendChild(selectAll);" in builder
    assert "head.appendChild(bulk);" in builder


def test_a_bulk_action_is_one_request_and_never_a_loop_of_toggles() -> None:
    """N toggles is not a slow implementation of "hide all"; it is a lossy one.

    Each toggle derives a full replacement pattern pair from a base it read
    before the others committed, so the last writer wins and the earlier
    patterns vanish.
    """

    slice_ = _bulk_slice()
    assert '"/admin/api/model-admin/visibility/bulk"' in slice_
    assert "Promise.all" not in slice_
    assert '"/admin/api/model-admin/visibility/toggle"' not in slice_


def test_a_bulk_action_does_not_refetch_the_whole_page_payload() -> None:
    """Re-reading 3.4 MB after a bulk write is the cost the bulk route removes."""

    assert 'api("/admin/api/model-admin")' not in _bulk_slice()


def test_the_selection_is_held_in_state_not_in_the_dom() -> None:
    """renderModelsTree() empties the tree on every filter keystroke.

    A selection kept in checkboxes would not survive typing one character.
    """

    section = _models_section()
    assert "selected: new Set()," in section
    assert 'tree.textContent = "";' in section


def test_the_bulk_result_is_a_live_region_that_says_a_whole_sentence() -> None:
    """One atomic status, never a competing live region per counter."""

    markup = _markup()
    panel = markup[markup.index('id="modelsBulkResult"') :][:400]
    assert 'role="status"' in panel
    assert 'aria-live="polite"' in panel
    assert 'aria-atomic="true"' in panel


def test_the_page_still_says_hiding_is_display_only_where_a_bulk_action_lands() -> None:
    """The hide-only principle is repeated where the user acts, not only at the top."""

    section = _models_section()
    builder = section[
        section.index("function renderModelsBulkResult(") : section.index(
            "async function runModelsBulk("
        )
    ]
    assert "Routing is unaffected either way." in builder


def test_the_bulk_buttons_name_what_they_act_on() -> None:
    """ "Hide all" must say how many, and of what, to a screen reader."""

    section = _models_section()
    assert '["hide", "Hide all"]' in section
    assert '["show", "Show all"]' in section
    assert '["invert", "Invert"]' in section
    assert "matching ${provider.provider_id} models" in section


def test_range_selection_is_reachable_without_a_pointer() -> None:
    """WCAG 2.2 wants a keyboard alternative to any author-controlled drag."""

    section = _models_section()
    assert 'event.key === " " && event.shiftKey' in section
    assert '"ArrowDown"' in section and '"ArrowUp"' in section
    assert "function startModelsDrag(" in section
    assert "function continueModelsDrag(" in section


def test_the_lazy_fill_and_the_page_size_survive_the_redesign() -> None:
    """The regression this design is most at risk of, re-asserted after it."""

    section = _models_section()
    assert "const MODELS_PAGE_SIZE = 40;" in section
    assert 'body.dataset.filled === "1"' in section
    assert 'editor.dataset.filled === "1"' in section
    assert "matching.length === 1" in section
    assert "window.setTimeout" in section and "modelsState.filter = next;" in section
