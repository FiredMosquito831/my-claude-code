"""The error hint sends the reader somewhere that exists.

An error that says "change it on the dashboard under Limits & Resilience ->
Deadlines" is only useful while a page called *Limits & Resilience* has a card
called *Deadlines* on it. Rename either without touching the hint and the
error becomes a worse lie than no hint at all: the reader goes looking for a
thing that is not there and concludes the setting is gone.

Nothing enforces that at runtime, because the hint is Python and the page is
JavaScript. So it is enforced here, in both directions, the same way
``test_admin_limits_view`` guards which sections the page claims.
"""

import re
from pathlib import Path

from my_claude_code.application.deadline_hints import (
    _PAGE_FOR_SECTION,
    _SECTION_FOR_ENV_VAR,
    LIMITS_PAGE_LABEL,
    PROVIDERS_PAGE_LABEL,
    card_for,
    limit_hint,
    page_for,
    providers_hint,
)
from my_claude_code.config.admin.manifest import FIELDS, SECTIONS

ADMIN_JS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "my_claude_code"
    / "api"
    / "admin_static"
    / "admin.js"
)

_LIMITS_VIEW = re.compile(
    r"id:\s*\"limits\",\s*\n\s*label:\s*\"(?P<label>[^\"]+)\"",
)


def _view_blocks() -> dict[str, tuple[str, set[str]]]:
    """Every VIEW_GROUPS entry as ``id -> (nav label, claimed section ids)``.

    Parsed rather than hardcoded because the point of this file is that the
    two languages cannot drift: `admin.js` owns both the tab name and the
    sections the tab renders, and a hint quotes both.
    """
    script = ADMIN_JS.read_text(encoding="utf-8")
    blocks: dict[str, tuple[str, set[str]]] = {}
    pattern = (
        r"id:\s*\"(?P<id>[a-z_]+)\",\s*\n\s*label:\s*\"(?P<label>[^\"]+)\""
        r".*?sections:\s*\[(?P<sections>[^\]]*)\]"
    )
    for match in re.finditer(pattern, script, re.DOTALL):
        blocks.setdefault(
            match.group("id"),
            (
                match.group("label"),
                set(re.findall(r"\"([^\"]+)\"", match.group("sections"))),
            ),
        )
    return blocks


def test_the_page_name_in_a_hint_is_the_nav_label_the_page_ships() -> None:
    """One string, two languages. `admin.js` owns the tab; the hint quotes it."""
    match = _LIMITS_VIEW.search(ADMIN_JS.read_text(encoding="utf-8"))
    assert match, "the limits view is not where every parser in this suite looks"
    assert match.group("label") == LIMITS_PAGE_LABEL


def test_every_card_named_in_a_hint_is_a_manifest_section() -> None:
    """The label is read from SECTIONS, so this pins the section ids."""
    section_ids = {section.section_id for section in SECTIONS}
    assert set(_SECTION_FOR_ENV_VAR.values()) <= section_ids


def test_every_env_var_with_a_hint_is_a_field_the_dashboard_edits() -> None:
    """Naming a knob the page cannot change sends the reader to a dead end."""
    keys = {field.key for field in FIELDS}
    assert set(_SECTION_FOR_ENV_VAR) <= keys


def test_a_hint_names_the_card_that_actually_owns_the_field() -> None:
    """Not just *a* card: the one the field is rendered under.

    The mapping is written by hand and the manifest is the truth, so a field
    moved between cards has to move the hint with it.
    """
    section_by_key = {field.key: field.section_id for field in FIELDS}
    label_by_id = {section.section_id: section.label for section in SECTIONS}

    for env_var in _SECTION_FOR_ENV_VAR:
        assert card_for(env_var) == label_by_id[section_by_key[env_var]]
        assert card_for(env_var) in limit_hint(env_var)


def test_every_page_named_in_a_hint_is_a_nav_label_the_page_ships() -> None:
    """Generalised from the limits-only check when pausing gained a hint.

    A routing switch is not a limit, so its hint names Model Config. One
    hardcoded page label could only ever be right for one page; the map is
    pinned here against the nav `admin.js` actually renders.
    """
    labels = {label for label, _sections in _view_blocks().values()}
    assert set(_PAGE_FOR_SECTION.values()) <= labels


def test_the_page_a_hint_names_claims_the_card_it_names() -> None:
    """A card on a page the nav does not show is a card nobody can reach."""
    blocks = _view_blocks()
    label_to_sections: dict[str, set[str]] = {}
    for label, sections in blocks.values():
        label_to_sections.setdefault(label, set()).update(sections)

    for env_var, section_id in _SECTION_FOR_ENV_VAR.items():
        page = page_for(env_var)
        assert section_id in label_to_sections[page], (
            f"{env_var} sends the reader to {page}, which does not render "
            f"the {section_id} card"
        )
        assert page in limit_hint(env_var)


def test_the_credits_pointer_names_a_page_the_nav_ships() -> None:
    """An exhausted balance sends the reader to Providers, so Providers must exist.

    The pointer names no env var -- there is no number to change, only a key
    to add or fund -- so it is not in ``_SECTION_FOR_ENV_VAR`` and is pinned
    here instead.
    """
    labels = {label for label, _sections in _view_blocks().values()}

    assert PROVIDERS_PAGE_LABEL in labels
    assert PROVIDERS_PAGE_LABEL in providers_hint()
