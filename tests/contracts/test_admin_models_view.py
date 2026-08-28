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
