"""Every row Get Started declares points at something that exists.

7.122.0 turns the page into a map of MCC: nine status cards, one collapsed
filterable list of the niche features, and one collapsed read-only section
about limits, raw logs and testing. Every row is one entry of ONE declared
table in ``admin.js`` (``GET_STARTED_FEATURES``), the same way the Guide links
are one table -- so a link cannot be written for a page, a control, a section,
a Docs slug or a Guide section that does not exist.

The failure this guards is silent by nature: a renamed setting, a moved
section, a retyped view id or a deleted Guide heading leaves a button that
looks right, switches to a page, and scrolls nowhere. The jsdom scenario
proves the buttons land at runtime; this proves the table agrees with the
server and the markup before anything runs.
"""

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_STATIC = REPO_ROOT / "src/my_claude_code/api/admin_static"
JS = (ADMIN_STATIC / "admin.js").read_text(encoding="utf-8")
HTML = (ADMIN_STATIC / "index.html").read_text(encoding="utf-8")
CONTROL_MAP = json.loads(
    (Path(__file__).resolve().parent / "settings_control_map.json").read_text(
        encoding="utf-8"
    )
)["fields"]


# ------------------------------------------------------------------ parsing

_TABLE = re.compile(r"const GET_STARTED_FEATURES = \[(?P<body>.*?)\n\];", re.DOTALL)
_ENTRY = re.compile(r"\n  \{\n(?P<body>.*?)\n  \},", re.DOTALL)
_OPEN = re.compile(
    r'\{ view: "(?P<view>[a-z_]+)"'
    r"(?:, (?P<kind>key|section|id|doc|providerQuery|agentQuery|webSearchQuery): "
    r'"(?P<value>[^"]*)")?'
    r'(?:, label: "[^"]*")? \}'
)
_VIEW_GROUPS = re.compile(r"const VIEW_GROUPS = \[(?P<body>.*?)\n\];", re.DOTALL)
_VIEW = re.compile(
    r"\{\s*//[^\n]*\n\s*id: \"(?P<id>[a-z_]+)\"[^}]*?sections: \[(?P<sections>[^\]]*)\]",
    re.DOTALL,
)
_GUIDE_ANCHORS = re.compile(r"const GUIDE_ANCHORS = \{(?P<body>.*?)\n\};", re.DOTALL)
_GUIDE_ENTRY = re.compile(r"""(\w+):\s*["']([^"']+)["']""")
_STATUS_TABLE = re.compile(
    r"const GET_STARTED_STATUS = \{(?P<body>.*?)\n\};", re.DOTALL
)
_STATUS_ENTRY = re.compile(r"\n  ([a-zA-Z]+)\(")


def _entries() -> list[dict]:
    match = _TABLE.search(JS)
    assert match, "admin.js no longer declares GET_STARTED_FEATURES"
    entries = []
    for entry in _ENTRY.finditer(match.group("body")):
        body = entry.group("body")
        fields = {}
        for key in ("id", "group", "area", "title", "line"):
            found = re.search(rf'{key}: "((?:[^"\\]|\\.)*)"', body)
            if found:
                fields[key] = found.group(1)
        opens = [
            {
                name: value
                for name, value in item.groupdict().items()
                if value is not None
            }
            for item in _OPEN.finditer(body)
        ]
        guide = re.search(r"guide: \[(.*?)\]", body, re.DOTALL)
        values = re.search(r"values: \[(.*?)\]", body, re.DOTALL)
        # `status: "fallbackChains"` or `status: null`.
        status = re.search(r'status: "(\w+)"|status: (null)', body)
        entries.append(
            {
                "raw": body,
                "id": fields.get("id"),
                "group": fields.get("group"),
                "area": fields.get("area"),
                "title": fields.get("title"),
                "line": fields.get("line"),
                "opens": opens,
                "guide": re.findall(r'"([^"]+)"', guide.group(1)) if guide else [],
                "values": re.findall(r'"([^"]+)"', values.group(1)) if values else [],
                "status": (
                    status.group(1)
                    if status and status.group(1)
                    else "null"
                    if status
                    else None
                ),
            }
        )
    return entries


def _view_groups() -> dict[str, list[str]]:
    """``{view id: [section ids it claims]}``, read from the declaration."""

    match = _VIEW_GROUPS.search(JS)
    assert match, "admin.js no longer declares VIEW_GROUPS"
    groups: dict[str, list[str]] = {}
    for block in re.split(r"\n  \{\n", match.group("body")):
        found = re.search(r'id: "(?P<id>[a-z_]+)"', block)
        if not found:
            continue
        sections = re.search(r"sections: \[(?P<body>[^\]]*)\]", block)
        groups[found.group("id")] = (
            re.findall(r'"([^"]+)"', sections.group("body")) if sections else []
        )
    return groups


def _guide_anchors() -> dict[str, str]:
    match = _GUIDE_ANCHORS.search(JS)
    assert match, "admin.js no longer declares GUIDE_ANCHORS"
    return dict(_GUIDE_ENTRY.findall(match.group("body")))


def _status_names() -> set[str]:
    match = _STATUS_TABLE.search(JS)
    assert match, "admin.js no longer declares GET_STARTED_STATUS"
    return set(_STATUS_ENTRY.findall(match.group("body")))


def _markup_ids() -> set[str]:
    return set(re.findall(r'id="([A-Za-z0-9_-]+)"', HTML))


def _doc_slugs() -> set[str]:
    from my_claude_code.api.docs_content import available_documents

    return {document.slug for document in available_documents()}


# ------------------------------------------------------------------- tests


def test_the_catalogue_parses() -> None:
    """A silent parse failure would make every assertion below vacuous."""

    entries = _entries()

    assert len(entries) >= 50, len(entries)
    assert all(entry["id"] for entry in entries)
    assert all(entry["title"] for entry in entries)
    assert all(entry["line"] for entry in entries)
    assert all(entry["group"] in {"main", "more", "advanced"} for entry in entries)
    groups = {
        group: [e for e in entries if e["group"] == group]
        for group in ("main", "more", "advanced")
    }
    assert len(groups["main"]) == 9
    assert len(groups["more"]) == 22
    assert {entry["area"] for entry in groups["advanced"]} == {
        "limits",
        "raw",
        "testing",
    }


def test_every_row_has_a_unique_id_and_an_open_target() -> None:
    entries = _entries()
    ids = [entry["id"] for entry in entries]

    assert len(ids) == len(set(ids)), sorted({i for i in ids if ids.count(i) > 1})
    assert [entry["id"] for entry in entries if not entry["opens"]] == []


def test_every_open_target_names_a_declared_view() -> None:
    views = _view_groups()

    assert {"providers", "model_config", "coding_agents"} <= set(views)
    unknown = sorted(
        {
            f"{entry['id']} -> {target['view']}"
            for entry in _entries()
            for target in entry["opens"]
            if target["view"] not in views
        }
    )
    assert unknown == []


def test_every_setting_key_is_edited_on_the_page_the_row_opens() -> None:
    """The control map is the server's own answer for which page owns a key."""

    wrong = []
    for entry in _entries():
        for target in entry["opens"]:
            key = target.get("key")
            if not key:
                continue
            mapped = CONTROL_MAP.get(key)
            if mapped is None:
                wrong.append(f"{entry['id']}: {key} is not a setting")
            elif (
                mapped["page"] != target["view"] or mapped["control"] != f"field-{key}"
            ):
                wrong.append(
                    f"{entry['id']}: {key} lives on {mapped['page']} as "
                    f"{mapped['control']}, the row opens {target['view']}"
                )
    assert wrong == []


def test_every_section_target_is_claimed_by_the_view_it_opens() -> None:
    views = _view_groups()

    wrong = [
        f"{entry['id']}: section {target['section']} is not one of {target['view']}"
        for entry in _entries()
        for target in entry["opens"]
        if target.get("section")
        and target["section"] not in views.get(target["view"], [])
    ]
    assert wrong == []


def test_every_id_target_is_declared_in_the_markup() -> None:
    declared = _markup_ids()

    missing = sorted(
        {
            f"{entry['id']} -> #{target['id']}"
            for entry in _entries()
            for target in entry["opens"]
            if target.get("id") and target["id"] not in declared
        }
    )
    assert missing == []


def test_every_docs_target_is_a_bundled_document() -> None:
    slugs = _doc_slugs()

    assert {"clients", "messaging", "oauth-providers"} <= slugs
    missing = sorted(
        {
            f"{entry['id']} -> {target['doc']}"
            for entry in _entries()
            for target in entry["opens"]
            if target.get("doc") and target["doc"] not in slugs
        }
    )
    assert missing == []


def test_every_query_target_is_a_filter_on_the_page_that_has_one() -> None:
    """A query is only a target on the page whose find box reads it."""

    allowed = {
        "providerQuery": {"providers"},
        "agentQuery": {"coding_agents"},
        "webSearchQuery": {"web_search"},
    }

    wrong = [
        f"{entry['id']}: {kind} on {target['view']}"
        for entry in _entries()
        for target in entry["opens"]
        for kind, views in allowed.items()
        if kind in target and target["view"] not in views
    ]
    assert wrong == []
    empty = sorted(
        {
            f"{entry['id']}: {kind}"
            for entry in _entries()
            for target in entry["opens"]
            for kind in allowed
            if kind in target and not target[kind].strip()
        }
    )
    assert empty == []


def test_every_value_chip_names_a_setting_on_the_same_page() -> None:
    """The advanced rows show a setting's CURRENT value: the key must be real,
    and it must be a setting the row's own Open button lands on."""

    wrong = []
    for entry in _entries():
        pages = {target["view"] for target in entry["opens"]}
        for key in entry["values"]:
            mapped = CONTROL_MAP.get(key)
            if mapped is None:
                wrong.append(f"{entry['id']}: {key} is not a setting")
            elif mapped["page"] not in pages:
                wrong.append(
                    f"{entry['id']}: {key} lives on {mapped['page']}, row opens {pages}"
                )
    assert wrong == []


def test_every_guide_purpose_is_declared() -> None:
    anchors = _guide_anchors()
    declared_ids = _markup_ids()

    assert anchors
    unknown = sorted(
        {
            f"{entry['id']} -> {purpose}"
            for entry in _entries()
            for purpose in entry["guide"]
            if purpose not in anchors
        }
    )
    assert unknown == []
    # And every anchor the table declares really is a Guide section.
    missing = sorted(
        anchor for anchor in anchors.values() if anchor not in declared_ids
    )
    assert missing == []
    not_guide = sorted(
        anchor for anchor in anchors.values() if not anchor.startswith("guide-")
    )
    assert not_guide == []


def test_every_status_function_is_declared_and_used() -> None:
    statuses = _status_names()
    used = {
        entry["status"]
        for entry in _entries()
        if entry["status"] and entry["status"] != "null"
    }

    assert used <= statuses
    # Every main card carries a status; no niche or advanced row does (those
    # show a value, not an achievement).
    main = [entry for entry in _entries() if entry["group"] == "main"]
    assert all(entry["status"] and entry["status"] != "null" for entry in main)
    assert {
        entry["id"]
        for entry in _entries()
        if entry["group"] != "main" and entry["status"] not in (None, "null")
    } == set()


def test_the_main_cards_offer_a_guide_link() -> None:
    """Every main feature has a Guide section that explains it."""

    without = [
        entry["id"]
        for entry in _entries()
        if entry["group"] == "main" and not entry["guide"]
    ]
    assert without == []


def test_no_row_names_a_retired_fcc_command() -> None:
    """7.122.0 (decision 13, partial): Get Started mentions no fcc-* command and
    no free-claude-code string. The dashboard-wide removal is the UX PR; this
    page must not add one."""

    for entry in _entries():
        text = f"{entry['title']} {entry['line']} {entry.get('area') or ''}"
        assert "fcc" not in text.lower(), entry["id"]
        assert "free-claude-code" not in text.lower(), entry["id"]


def test_the_copy_names_only_controls_the_docs_already_document() -> None:
    """Every control the server's Get Started copy quotes is a documented one.

    The copy is the server's (`config/onboarding.py`), so a new quoted name
    here means a control a reader is told to press; DOCUMENTED_CONTROLS is
    where that promise is kept (and `test_docs_ui_labels` proves each exists).
    """

    from my_claude_code.config.onboarding import build_state
    from tests.contracts.test_docs_ui_labels import DOCUMENTED_CONTROLS

    state = build_state(claude_settings_configured=False, has_requests=False, agents=())
    texts = []
    for step in state.steps:
        texts.extend([step.description, *step.instructions])
    # A control name starts with a capital ("Search providers", "Add key",
    # "Add MCP server"). The same apostrophes also punctuate ordinary prose --
    # "Claude Code's settings or a desktop app's file" reads as one quoted
    # fragment starting with "s" -- and a prompt such as 'say OK' is lower
    # case, so the shape is what separates a name from a sentence.
    quoted = {
        phrase
        for text in texts
        for phrase in re.findall(r"'([^']{3,40})'", text)
        if phrase[:1].isupper()
    }
    assert quoted <= set(DOCUMENTED_CONTROLS), quoted


@pytest.mark.parametrize("needle", ["GET_STARTED_FEATURES", "GET_STARTED_STATUS"])
def test_the_parsers_find_what_they_claim(needle: str) -> None:
    assert needle in JS
