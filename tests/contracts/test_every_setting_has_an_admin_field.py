"""Every env var Settings reads is settable from the dashboard.

A setting with no admin field is invisible: the only way to change it is to
hand-edit ``~/.fcc/.env``, which the dashboard then rewrites. Four fields had
drifted out of the manifests this way before this test existed
(``WEBSEARCH_DIGEST_CHARS``, ``WEBSEARCH_DIGEST_CONTENT_CHARS``,
``WEBSEARCH_DIGEST_ANSWER``, ``ANTHROPIC_OAUTH_REQUIRE_CLAUDE_CODE``).

**7.121.2: a manifest field is not enough -- it needs a control, on its page.**
Thirteen web-search key-rotation settings (``TAVILY_API_KEY_ROTATION`` and
twelve more) had a manifest field each, passed this file for releases, and
could be seen nowhere: the generic grid skips every ``_ROTATION`` field because
provider cards host their own, and no web-search card did. So the second half
of this file reads ``settings_control_map.json`` -- every setting, the page its
section renders into, and the id of the form control that edits it there,
generated from a real page and proved equal to the page by
``tests/api/test_admin_static_jsdom.py`` -- and fails any field without an
entry, on the wrong page, or with no control. The only fields allowed to have
no form control are named in ``CONTROL_EXEMPTIONS`` below, each with the reason
the page edits it another way.
"""

import json
import re
from pathlib import Path

from pydantic import AliasChoices

from my_claude_code.config.admin.manifest import (
    FIELD_BY_KEY,
    FIELDS,
    SECTIONS,
    env_keys,
)
from my_claude_code.config.admin.values import ROUTE_PAUSE_KEYS
from my_claude_code.config.settings import Settings

ADMIN_JS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "my_claude_code"
    / "api"
    / "admin_static"
    / "admin.js"
)
CONTROL_MAP = Path(__file__).with_name("settings_control_map.json")

# A nested settings model is not an env var of its own: ``nim`` is populated
# from ``NIM_*`` keys, each of which carries its own manifest field. The group
# name itself can never appear in a manifest, so it is exempt by name rather
# than by a rule that would also excuse a genuinely missing scalar.
NESTED_SETTINGS_GROUPS = frozenset({"NIM"})

# Where a new key belongs, so the failure message can say it instead of
# leaving the author to find out.
_WHERE_TO_ADD = {
    "WEBSEARCH": "config/admin/websearch_manifest.py",
    "WEB_SEARCH": "config/admin/websearch_manifest.py",
    "SEARXNG": "config/admin/websearch_manifest.py",
}


def _env_key(name: str) -> str:
    """Return the env var a Settings field is populated from."""

    field = Settings.model_fields[name]
    alias = field.validation_alias
    if alias is None:
        return name.upper()
    # ``AliasChoices`` lists the canonical name first; the manifest keys on
    # that canonical name, so resolve to it instead of the repr.
    if isinstance(alias, AliasChoices):
        return str(alias.choices[0])
    return str(alias)


def _suggested_manifest(key: str) -> str:
    for prefix, module in _WHERE_TO_ADD.items():
        if key.startswith(prefix):
            return module
    return (
        "config/admin/provider_manifest.py if it belongs to a provider card, "
        "otherwise config/admin/manifest.py"
    )


def test_every_setting_is_reachable_from_the_dashboard() -> None:
    exposed = env_keys()
    missing = sorted(
        key
        for key in (_env_key(name) for name in Settings.model_fields)
        if key not in exposed and key not in NESTED_SETTINGS_GROUPS
    )
    assert not missing, "\n".join(
        f"{key} has no admin field -- add a ConfigFieldSpec to {_suggested_manifest(key)}"
        for key in missing
    )


def test_the_four_recovered_fields_stay_exposed() -> None:
    """Names the fields this test was written for, so removing one fails loudly.

    The general check above would also catch it, but only as a diff; this one
    says which field and therefore which regression.
    """
    exposed = env_keys()
    for key in (
        "WEBSEARCH_DIGEST_CHARS",
        "WEBSEARCH_DIGEST_CONTENT_CHARS",
        "WEBSEARCH_DIGEST_ANSWER",
        "ANTHROPIC_OAUTH_REQUIRE_CLAUDE_CODE",
    ):
        assert key in exposed, f"{key} was removed from the admin manifests"


def test_the_nested_group_allow_list_only_covers_real_groups() -> None:
    """An allow-list is an escape hatch, so it must not outlive its reason."""

    for group in NESTED_SETTINGS_GROUPS:
        name = group.lower()
        assert name in Settings.model_fields, f"{group} is no longer a Settings field"
        assert hasattr(Settings.model_fields[name].annotation, "model_fields"), (
            f"{group} is no longer a nested settings model and needs a real field"
        )


# ------------------------------------------------- a control, on its page ---

_PAUSE_LIST_REASON = (
    "a route's pause list. Every model on that route's rail has its own Pause "
    "button on Model Config, which writes this list through POST "
    "/admin/api/config/route-pause at once, with no Apply (6.35.1). A form "
    "control for the same list would be a second writer competing with it."
)

#: Fields that have no form control of their own, and why the page needs none.
#: This is the ONLY way a field may lack one: the test below fails every other
#: field without a control, so an entry here is a claim, in a diff a reviewer
#: reads, that the page edits the field some other way. Two kinds exist; the
#: last test in this file refuses a third.
CONTROL_EXEMPTIONS: dict[str, str] = {
    **{
        paused_key: _PAUSE_LIST_REASON
        for _model, _chain, paused_key in ROUTE_PAUSE_KEYS
    },
    "ANTHROPIC_OAUTH_MANAGE": (
        "not a value: the Claude subscription's sign-in buttons. Settings has "
        "no field for it; signing in fills ANTHROPIC_OAUTH_ACCESS_TOKEN, which "
        "has its own control on the same card."
    ),
    "CHATGPT_OAUTH_LOGIN": (
        "not a value: the ChatGPT device-code and browser sign-in buttons. "
        "Settings has no field for it; signing in fills "
        "CHATGPT_OAUTH_ACCESS_TOKEN and CHATGPT_OAUTH_ACCOUNT_ID, which have "
        "their own controls on the same card."
    ),
    "CHATGPT_OAUTH_IMPORT_CODEX": (
        "not a value: the button that imports an existing Codex sign-in. "
        "Settings has no field for it; it fills the same two CHATGPT_OAUTH_* "
        "settings, which have their own controls."
    ),
}

_VIEW_GROUPS_BLOCK = re.compile(r"const VIEW_GROUPS = \[(.*?)\n\];", re.DOTALL)
_VIEW_ENTRY = re.compile(
    r"""\bid:\s*["']([^"']+)["'](.*?)(?=\bid:\s*["']|\Z)""", re.DOTALL
)
_SECTIONS_ARRAY = re.compile(r"\bsections:\s*\[([^\]]*)\]")
_QUOTED = re.compile(r"""["']([^"']+)["']""")


def _page_of_section() -> dict[str, str]:
    """Which dashboard page each manifest section renders into, from admin.js.

    Read from the shipped ``VIEW_GROUPS`` rather than a Python mirror, the way
    ``test_admin_sections_are_rendered.py`` reads it: a mirror would agree with
    itself while the page put a field somewhere else.
    """

    block = _VIEW_GROUPS_BLOCK.search(ADMIN_JS.read_text(encoding="utf-8"))
    assert block, "admin.js no longer declares `const VIEW_GROUPS = [...]`"
    pages: dict[str, str] = {}
    for view_id, body in _VIEW_ENTRY.findall(block.group(1)):
        sections = _SECTIONS_ARRAY.search(body)
        if sections:
            for section in _QUOTED.findall(sections.group(1)):
                pages[section] = view_id
    return pages


def _control_map() -> dict[str, dict[str, str | None]]:
    return json.loads(CONTROL_MAP.read_text(encoding="utf-8"))["fields"]


def test_the_page_registry_parse_is_not_vacuous() -> None:
    """Every manifest section maps to a page, so the check below means something."""

    pages = _page_of_section()

    assert set(pages) == {section.section_id for section in SECTIONS}
    assert pages["websearch"] == "web_search"
    assert pages["providers"] == "providers"
    assert pages["deadlines"] == "limits"
    assert pages["models"] == "model_config"


def test_every_admin_field_has_a_control_on_its_page() -> None:
    """A field the page cannot edit is a setting nobody can change.

    Each manifest field must have an entry in ``settings_control_map.json``
    naming the page its section belongs to and the id of the form control that
    edits it there (``field-<KEY>``, the id ``buildFieldControl`` gives every
    control) -- or be one of ``CONTROL_EXEMPTIONS``. A new setting without a
    control fails here, in the ordinary pytest job.
    """

    pages = _page_of_section()
    entries = _control_map()
    problems: list[str] = []
    for field in FIELDS:
        page = pages.get(field.section_id)
        entry = entries.get(field.key)
        if entry is None:
            problems.append(
                f"{field.key}: not in settings_control_map.json. Give it a "
                f"control on the {page!r} page -- renderField() in its section, "
                "or in the card that owns it -- then regenerate the map from a "
                "real run of the select_blank jsdom scenario"
            )
            continue
        if entry["page"] != page:
            problems.append(
                f"{field.key}: rendered on {entry['page']!r}, but its section "
                f"{field.section_id!r} belongs to the {page!r} page"
            )
        if field.key in CONTROL_EXEMPTIONS:
            if entry["control"] is not None:
                problems.append(
                    f"{field.key}: exempted, but it has a control now -- drop "
                    "it from CONTROL_EXEMPTIONS"
                )
            continue
        if entry["control"] != f"field-{field.key}":
            problems.append(
                f"{field.key}: no form control on the {page!r} page "
                f"(map says {entry['control']!r})"
            )
    assert not problems, "\n".join(problems)


def test_the_control_map_names_only_real_settings() -> None:
    """A map entry for a field that no longer exists is a stale claim."""

    stale = sorted(set(_control_map()) - {field.key for field in FIELDS})
    assert not stale, f"settings_control_map.json names unknown fields: {stale}"


def test_the_thirteen_recovered_rotation_controls_stay() -> None:
    """Names the fields 7.121.2 was written for, so losing one says which."""

    entries = _control_map()
    rotations = sorted(
        field.key
        for field in FIELDS
        if field.section_id == "websearch" and field.key.endswith("_ROTATION")
    )
    assert len(rotations) == 13
    for key in rotations:
        assert entries[key] == {"page": "web_search", "control": f"field-{key}"}, key


def test_every_exemption_is_one_of_the_two_justified_kinds() -> None:
    """The escape hatch has two shapes; a third is a field without a control.

    A pause list (``ROUTE_PAUSE_KEYS``) is written by its rail's Pause buttons;
    an ``oauth_login`` field is a set of sign-in buttons that holds no value.
    Anything else added to ``CONTROL_EXEMPTIONS`` fails here, and must instead
    get a control.
    """

    pause_lists = {paused_key for _model, _chain, paused_key in ROUTE_PAUSE_KEYS}
    for key, reason in CONTROL_EXEMPTIONS.items():
        assert key in FIELD_BY_KEY, f"{key} is exempted but is not a manifest field"
        assert len(reason) > 40, f"{key} is exempted without a real reason"
        assert key in pause_lists or FIELD_BY_KEY[key].field_type == "oauth_login", (
            f"{key} is exempted from having a control, but it is neither a "
            "route pause list nor a sign-in button: give it a control"
        )
