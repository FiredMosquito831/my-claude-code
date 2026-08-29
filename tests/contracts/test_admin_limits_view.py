"""Guards for the Limits & Resilience page: one owner per field, no dead ends.

The page this covers used to be one flat grid of 37 fields mixing output
budgets, deadlines, benching, provider retries, credential health and log
storage. Splitting it into six cards moved eight fields between *views*, and a
section that no view claims renders nowhere at all -- the manifest registers
it, the API serves it, and nothing fails. That gap shipped once already. So
the claims are asserted here, in both directions.

Static assertions on the shipped JavaScript rather than a browser: the project
needs UI guards that run on every platform, not a runtime check that silently
skips wherever node or jsdom is missing.
"""

import re
from pathlib import Path

from my_claude_code.config.admin.manifest import FIELDS, SECTIONS

STATIC = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "my_claude_code"
    / "api"
    / "admin_static"
)
ADMIN_JS = STATIC / "admin.js"
ADMIN_HTML = STATIC / "index.html"

_VIEW_GROUPS_BLOCK = re.compile(r"const VIEW_GROUPS = \[(.*?)\n\];", re.DOTALL)
_VIEW_ENTRY = re.compile(
    r"\bid:\s*\"(?P<id>[^\"]+)\".*?\bsections:\s*\[(?P<sections>[^\]]*)\]",
    re.DOTALL,
)

LIMITS_SECTIONS = (
    "budgets",
    "deadlines",
    "benching",
    "provider_retries",
    "credential_health",
    "diagnostics",
)


def _script() -> str:
    return ADMIN_JS.read_text(encoding="utf-8")


def _view_sections() -> dict[str, list[str]]:
    block = _VIEW_GROUPS_BLOCK.search(_script())
    assert block, "VIEW_GROUPS is not where every parser in this suite looks"
    claims: dict[str, list[str]] = {}
    for entry in _VIEW_ENTRY.finditer(block.group(1)):
        claims[entry.group("id")] = re.findall(r"\"([^\"]+)\"", entry.group("sections"))
    return claims


def _function_source(name: str) -> str:
    script = _script()
    start = script.index(f"function {name}(")
    end = script.index("\n}\n", start)
    return script[start:end]


def test_the_limits_view_claims_every_resilience_section() -> None:
    """Six cards in the order the page reads, top to bottom."""

    assert _view_sections()["limits"] == list(LIMITS_SECTIONS)


def test_no_section_is_claimed_by_two_views() -> None:
    """Every field is claimed by exactly one view.

    A field has exactly one ``section_id``, so uniqueness of section claims is
    sufficient -- and it is the only thing ``admin.js`` can get wrong. A
    setting rendered on two pages is a setting that can show two answers, and
    ``changedValues()`` submits whichever control it walked last.
    """

    claimed = [
        section for sections in _view_sections().values() for section in sections
    ]
    duplicates = {section for section in claimed if claimed.count(section) > 1}
    assert not duplicates, f"claimed by more than one view: {sorted(duplicates)}"


def test_every_manifest_field_is_claimed_by_exactly_one_view() -> None:
    """The Python half: a section nobody claims is a settings page with no page."""

    claims = _view_sections()
    for field in FIELDS:
        owners = [
            view for view, sections in claims.items() if field.section_id in sections
        ]
        assert len(owners) == 1, (
            f"{field.key} is in section {field.section_id!r}, claimed by {owners}"
        )


def test_the_limits_rail_links_only_to_sections_the_view_claims() -> None:
    """The rail is static markup; its targets are rendered. Dead links are silent."""

    markup = ADMIN_HTML.read_text(encoding="utf-8")
    rail = markup[markup.index('id="limitsToc"') : markup.index('class="toc-body"')]
    linked = re.findall(r'href="#section-([a-z_]+)"', rail)
    assert linked == list(LIMITS_SECTIONS)


def test_the_deadline_calculator_never_builds_markup_from_a_model_name() -> None:
    """A route label is a model name, and half of a model name is user-typed."""

    source = _function_source("updateDeadlineCalculator")
    for forbidden in (".innerHTML", "insertAdjacentHTML"):
        assert forbidden not in source, (
            "the calculator must build its rows with createElement/textContent"
        )
    # A numeric comparison is fine; an opening tag is not.
    assert not re.search(r"<\s*/[A-Za-z]|<[A-Za-z]+[\s>]", source), (
        "the calculator must not assemble a tag from a route label"
    )


def test_the_inert_mode_group_is_disabled_not_removed() -> None:
    """Removing the nodes would lose a value the user typed before switching.

    ``changedValues()`` already skips a disabled control, so a disabled knob is
    not submitted and the dirty counter does not count it -- with no new code
    in the dirty machinery. ``hidden`` alone would leave it enabled, and
    therefore saved, which is the failure this page exists to remove.
    """

    source = _function_source("applyBenchMode")
    assert "el.disabled = inert" in source
    assert ".remove()" not in source
    assert "updateDirtyState()" in source, (
        "the counter must drop at the moment of the switch, not at Apply time"
    )


def test_the_section_renderer_table_covers_only_named_sections() -> None:
    script = _script()
    table = script[
        script.index("const SECTION_RENDERERS = {") : script.index(
            "function renderSections("
        )
    ]
    named = set(re.findall(r"^\s{2}(\w+):", table, re.MULTILINE))
    assert named <= {section.section_id for section in SECTIONS}


def test_the_generic_grid_is_still_the_default() -> None:
    """Adding a renderer must never change how any other section renders."""

    source = _function_source("renderSections")
    assert "const renderer = SECTION_RENDERERS[section.id];" in source
    assert 'grid.className = "field-grid";' in source


def test_the_page_has_no_heading_without_controls_under_it() -> None:
    """A card whose every field is advanced renders prose and a toggle.

    That is the Models page's old failure, and the six-card split walks into
    it: ``credential_health`` holds two fields and both shipped ``advanced``,
    so the card would render a heading, a description and nothing else until
    the reader found "Show advanced".
    """

    for section_id in LIMITS_SECTIONS:
        fields = [field for field in FIELDS if field.section_id == section_id]
        assert fields, f"{section_id} renders a heading with no fields at all"
        assert any(not field.advanced for field in fields), (
            f"every field in {section_id} is behind 'Show advanced', so the "
            "card renders a heading, a description and nothing operable"
        )
