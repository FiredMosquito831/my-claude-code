"""Every manifest section must be claimed by a dashboard view.

A settings page has a spine -- schema, persistence, serialization, render, nav
-- and missing one link is silent, because every other link still works. That
is not hypothetical here: a release once registered a section and eighteen
fields, confirmed the API returned them, and shipped it as an editable page.
No view claimed the section, so the fields rendered nowhere. Nothing failed.

`admin.js` owns the mapping in `VIEW_GROUPS`, so this reads the shipped
JavaScript rather than a Python mirror of it -- a mirror would agree with
itself while the page stayed blank.
"""

import re
from pathlib import Path

from my_claude_code.config.admin.manifest import SECTIONS

ADMIN_JS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "my_claude_code"
    / "api"
    / "admin_static"
    / "admin.js"
)

# `sections: ["limits", "desktop", "diagnostics"],` -> the bracketed body.
_SECTIONS_ARRAY = re.compile(r"\bsections:\s*\[([^\]]*)\]")
_QUOTED = re.compile(r"""["']([^"']+)["']""")


def _sections_claimed_by_views() -> set[str]:
    source = ADMIN_JS.read_text(encoding="utf-8")
    claimed: set[str] = set()
    for array_body in _SECTIONS_ARRAY.findall(source):
        claimed.update(_QUOTED.findall(array_body))
    return claimed


def test_the_view_registry_is_parsable() -> None:
    """A silent parse failure would make every assertion below vacuous."""

    claimed = _sections_claimed_by_views()

    assert claimed, "found no `sections: [...]` arrays in admin.js"
    # Sanity anchors: long-standing sections that must always be claimed.
    assert {"providers", "models", "deadlines"} <= claimed


# Sections that are deliberately not on any page, with the reason. Keep this
# empty if you can: an entry here is a field the user can set in .env and can
# never see in the dashboard.
KNOWN_UNRENDERED: set[str] = set()


def test_every_manifest_section_is_rendered_by_some_view() -> None:
    """A section no view claims is served by the API and shown to nobody."""

    claimed = _sections_claimed_by_views()
    declared = {section.section_id for section in SECTIONS} - KNOWN_UNRENDERED

    orphaned = sorted(declared - claimed)
    assert not orphaned, (
        "these manifest sections are not listed in any VIEW_GROUPS entry in "
        f"admin.js, so their fields render nowhere: {orphaned}. Add the key to "
        "the `sections` array of the view that should show them."
    )


def test_no_view_claims_a_section_that_does_not_exist() -> None:
    """The mirror of the above: a typo in admin.js silently renders nothing."""

    claimed = _sections_claimed_by_views()
    declared = {section.section_id for section in SECTIONS}

    unknown = sorted(claimed - declared)
    assert not unknown, (
        "these section keys appear in admin.js VIEW_GROUPS but no manifest "
        f"section declares them: {unknown}. Either the key is misspelled or "
        "the section was removed without updating the view."
    )
