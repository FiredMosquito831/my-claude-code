"""The Usage layout registry is one declared table, and the setting is its key.

``specs/PR-USAGE-LAYOUTS-SPEC.md`` §3, following ``DESIGN-REVAMP-OPTIONS.md``
§13.2.2's precedent: **one declared table, one renderer that switches on the
chosen id, and nothing about layout in the poller, the payload or the routes.**

The vocabulary is spelled TWICE on purpose and pinned together here, in both
directions:

* ``config/constants.USAGE_LAYOUT_VALUES`` is the server's list — what a save is
  refused for naming, and what an unknown value falls back from;
* ``api/admin_static/usage_layouts.js``'s ``USAGE_LAYOUTS`` is the page's table —
  what a renderer is looked up in.

A word in the constants with no row is a setting the page cannot draw; a row
with no constant is a layout nobody can select. A static contract test cannot
know whether the page agrees with itself (that is
``tests/api/test_admin_usage_layouts_jsdom.py``), but it CAN know that the two
declarations name the same five things — which is the failure that would
otherwise be found by a reader staring at a page that fell back.

Also pinned here:

* **04 does not exist.** §8.2 — not a row, not a CSS selector, not a setting
  value. The greps below are over every shipped asset, so a future copy-paste
  cannot smuggle it in from ``usage-previews/04-priority-column/``.
* **no layout logic in the routes or the payload.** The route reports the id and
  nothing branches on it; a branch would be the "layout logic in the payload"
  §3 forbids, and the grep for one is deliberately narrow (the route's own
  ``_layout_for`` is the only place the word appears).
"""

import re
from pathlib import Path

from my_claude_code.config import constants

REPO = Path(__file__).resolve().parents[2]
ADMIN_STATIC = REPO / "src" / "my_claude_code" / "api" / "admin_static"
REGISTRY = ADMIN_STATIC / "usage_layouts.js"
ENV_TEMPLATE = REPO / ".env.example"


def _registry_rows() -> list[dict[str, str]]:
    """The five rows, parsed out of the shipped file.

    Deliberately a regex over the real source rather than a Python mirror: a
    mirror would only ever agree with itself (the rule
    ``test_admin_sections_are_rendered.py`` states for ``VIEW_GROUPS``).
    """

    text = REGISTRY.read_text(encoding="utf-8")
    block = re.search(r"const USAGE_LAYOUTS = \[(.*?)\n\];", text, re.DOTALL)
    assert block, "usage_layouts.js no longer declares `const USAGE_LAYOUTS = [...]`"
    return [
        {"id": entry.group(1), "name": entry.group(2)}
        for entry in re.finditer(
            r"\{\s*id: \"([^\"]+)\",\s*name: \"([^\"]+)\",", block.group(1)
        )
    ]


def test_the_registry_parses_and_has_exactly_five_rows() -> None:
    rows = _registry_rows()
    assert len(rows) == 5, rows


def test_the_registry_order_is_the_declared_order() -> None:
    """The picker and the setting list the five in this order, always."""

    assert [row["id"] for row in _registry_rows()] == [
        "one_screen",
        "ledger_rows",
        "meter_board",
        "dial_cards",
        "master_detail",
    ]


def test_the_setting_vocabulary_is_the_registry() -> None:
    """Both directions: no orphan word, no orphan row."""

    assert tuple(row["id"] for row in _registry_rows()) == constants.USAGE_LAYOUT_VALUES


def test_the_default_is_the_first_row_and_is_one_screen() -> None:
    assert constants.USAGE_LAYOUT_DEFAULT == "one_screen"
    assert _registry_rows()[0]["id"] == constants.USAGE_LAYOUT_DEFAULT


def test_every_row_names_itself_for_a_reader() -> None:
    """A `name` is a label; a row with the id as its name is a row nobody
    labelled."""

    for row in _registry_rows():
        assert row["name"].strip(), row
        assert row["name"] != row["id"], row


def test_every_row_carries_a_one_line_note() -> None:
    """The manifest's option labels and the docs quote these, so a row without
    one would leave the setting's help text inventing a description."""

    text = REGISTRY.read_text(encoding="utf-8")
    notes = re.findall(r"note: \"([^\"]+)\",", text)
    assert len(notes) == 5, notes
    for note in notes:
        assert len(note) >= 30, note


def test_every_row_has_a_renderer() -> None:
    text = REGISTRY.read_text(encoding="utf-8")
    renders = re.findall(r"render: \(root, model\) =>", text)
    assert len(renders) == 5, renders


def test_the_fallback_is_one_screen() -> None:
    text = REGISTRY.read_text(encoding="utf-8")
    assert 'const USAGE_LAYOUT_FALLBACK_ID = "one_screen";' in text


def test_layout_04_does_not_ship_anywhere() -> None:
    """§8.2: the excluded preview stays exploration only.

    The greps are over every SHIPPED asset (the page, the stylesheets, the
    scripts, the settings template), not over ``usage-previews/``, where the
    folder legitimately exists as the design record.
    """

    needles = ("04-priority-column", "priority_column")
    template = ENV_TEMPLATE.read_text(encoding="utf-8")
    offenders = [
        f"{path.name}: {needle}"
        for path in sorted(ADMIN_STATIC.rglob("*"))
        if not path.is_dir()
        for needle in needles
        if needle in path.read_text(encoding="utf-8", errors="replace")
    ]
    offenders.extend(
        f".env.example: {needle}" for needle in needles if needle in template
    )
    assert not offenders, offenders


def test_the_page_has_no_priority_column_selector() -> None:
    """The word may appear in prose about the excluded preview and nowhere else.

    The registry explains WHY 04 is out, which is worth keeping; a class name
    or a data attribute is a layout that could be drawn.
    """

    for path in sorted(ADMIN_STATIC.glob("*.css")):
        text = path.read_text(encoding="utf-8")
        assert "priority" not in text.lower(), path.name


def test_the_route_reports_the_layout_and_never_branches_on_it() -> None:
    """§3: nothing about layout in the payload."""

    routes = (
        REPO / "src" / "my_claude_code" / "api" / "admin_usage_routes.py"
    ).read_text(encoding="utf-8")
    # ONE reader of the setting, in ONE function. Two call sites (the GET and
    # the read-all answer) is the whole of the route's interest in layout; a
    # third would be a place that could disagree with the other two.
    assert routes.count("usage_layout") == 1, routes.count("usage_layout")
    assert routes.count("_layout_for(") == 3, routes.count("_layout_for(")
    # No layout id is ever compared against a provider or a state.
    for layout_id in constants.USAGE_LAYOUT_VALUES:
        assert f'== "{layout_id}"' not in routes, layout_id
        assert f'!= "{layout_id}"' not in routes, layout_id


def test_the_setting_is_on_the_dashboard_with_help_text() -> None:
    """III.6: a real help sentence, and it names what each layout looks like."""

    from my_claude_code.config.admin.manifest import FIELD_BY_KEY
    from my_claude_code.config.admin.spec import ConfigOptionSpec

    field = FIELD_BY_KEY["USAGE_LAYOUT"]
    assert field.section_id == "loop_health"
    assert len(field.description) >= 20
    assert len(field.options) == 5
    # `options` is `tuple[str | ConfigOptionSpec, ...]`, so narrow before
    # reading `.value`, and a suppression comment is banned repo-wide; the
    # narrowing is what makes the assertion below check the right thing.
    options = [
        option for option in field.options if isinstance(option, ConfigOptionSpec)
    ]
    assert len(options) == 5
    assert tuple(option.value for option in options) == constants.USAGE_LAYOUT_VALUES
    # The label for each option is a reader's words, not the id.
    for option in options:
        assert option.label.strip(), option
        assert option.label != option.value, option
    # A view: no provider client reads this word.
    assert field.affects_providers is False


def test_the_setting_is_in_the_env_template() -> None:
    """A fresh install must be able to see the knob the page obeys."""

    template = ENV_TEMPLATE.read_text(encoding="utf-8")
    assert "\nUSAGE_LAYOUT=one_screen\n" in template
    for layout_id in constants.USAGE_LAYOUT_VALUES:
        assert f"#   {layout_id}" in template, layout_id


def test_an_unknown_value_falls_back_without_refusing_to_start() -> None:
    """A layout is a VIEW: a stale ``.env`` must serve a page, not stop a server."""

    from my_claude_code.config.settings import Settings

    settings = Settings.model_validate({"USAGE_LAYOUT": "not_a_layout"})
    assert settings.usage_layout == constants.USAGE_LAYOUT_DEFAULT
    for layout_id in constants.USAGE_LAYOUT_VALUES:
        assert (
            Settings.model_validate({"USAGE_LAYOUT": layout_id}).usage_layout
            == layout_id
        )
    # Case and space fold, because the template and a human disagree about both.
    assert (
        Settings.model_validate({"USAGE_LAYOUT": " One_Screen "}).usage_layout
        == "one_screen"
    )


def test_the_page_never_persists_a_layout_in_the_browser() -> None:
    """§3: machine-level only — no localStorage layer.

    The registry and the page's paint path may not touch web storage. The
    dashboard does use `localStorage` for other things (the Analytics filters,
    the design), so this checks the USAGE strings rather than the API.
    """

    for path in (REGISTRY, ADMIN_STATIC / "admin.js"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "localStorage" not in line and "sessionStorage" not in line:
                continue
            stripped = line.strip()
            # A PROSE line (a comment or a docstring sentence) explains the
            # rule rather than breaking it; a statement is what could store
            # something.
            if stripped.startswith(("*", "//", "/*")):
                continue
            lowered = line.lower()
            assert "usage" not in lowered and "layout" not in lowered, (path.name, line)
