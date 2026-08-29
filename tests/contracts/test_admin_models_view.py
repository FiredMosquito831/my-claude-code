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
        "/admin/api/model-admin/visibility/toggle",
        "/admin/api/model-admin/overrides",
    ):
        assert f'"{path}"' in script, f"admin.js never calls {path}"
        assert f'"{path}"' in routes, f"admin_routes.py does not serve {path}"


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
    assert "refreshModelRow(key)" in save_body
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
    assert "exact-match pattern into the hide list" in view


def test_the_measured_reasoning_chip_names_its_window_and_both_numbers() -> None:
    """Requested and returned are independent facts, and the chip says both."""
    assert "reasoning requested ${measured.requested}/${measured.attempts}" in _script()
    assert "returned ${measured.returned}/${measured.attempts}" in _script()
    assert "measured_days" in _script()


def test_the_measured_chip_is_not_rendered_without_a_measurement() -> None:
    """No traffic is not a measured zero, so no chip rather than a zeroed one."""
    assert "if (measured && measured.attempts) {" in _script()
