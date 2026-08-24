"""The dashboard renders attacker-influenceable strings as text, never markup.

Two confirmed injection sinks shipped in admin.js: section headings built by
interpolating the manifest's label/description into innerHTML (a custom
provider's user-typed display_name reaches that surface), and the custom-
provider card title doing the same with display_name. Both now go through
createElement/textContent like renderProviderCard always has.

This file pins that contract two ways: the real script runs in jsdom against
payloads shaped like markup and must produce text nodes instead of elements,
and a static scan forbids any future ``innerHTML =`` assignment that
interpolates a template literal outside an explicitly enumerated allowance
list (empty today -- see ALLOWED_INNERHTML_INTERPOLATIONS for why).
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).with_name("admin_xss_jsdom_harness.mjs")
STATIC_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "my_claude_code"
    / "api"
    / "admin_static"
)

# Markup-shaped probes. If admin.js ever regresses to interpolating these
# into innerHTML, jsdom parses them into real elements (img/script/b/i) --
# with textContent they survive verbatim as characters. The harness receives
# exactly these bytes, so the assertions below compare against one truth.
PROBE = {
    "label": "<img src=x onerror=alert(1)>Evil <b>Section</b>",
    "description": "<script>window.__xssRan = true</script>pwned <i>desc</i>",
    "display_name": "<img src=x onerror=alert(2)>Evil <b>Provider</b>",
}


def _run(auth_open_mode: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    result = subprocess.run(
        [node, str(HARNESS), str(STATIC_DIR)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        env={
            **os.environ,
            "XSS_PROBE_JSON": json.dumps(PROBE),
            "AUTH_OPEN_MODE": auth_open_mode,
        },
    )
    if result.returncode != 0:
        if "Cannot find package 'jsdom'" in result.stderr:
            pytest.skip("jsdom is not installed")
        pytest.fail(f"harness failed: {result.stderr[-2000:]}")
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def rendered_open() -> dict:
    """messaging_auth_open carries platforms, per the wave-2 lane contract."""
    return _run("open")


@pytest.fixture(scope="module")
def rendered_locked() -> dict:
    return _run("empty")


def test_the_script_evaluates_cleanly_under_hostile_payloads(rendered_open) -> None:
    assert rendered_open["fatal"] is None
    assert rendered_open["scriptErrors"] == []
    assert rendered_open["consoleErrors"] == []


def test_a_malicious_section_label_renders_as_text_not_elements(
    rendered_open,
) -> None:
    heading = rendered_open["sectionHeading"]
    assert heading["present"] is True
    assert heading["labelText"] == PROBE["label"]
    assert heading["descriptionText"] == PROBE["description"]
    # Exactly the structure renderSections builds by hand: wrapper div, h3, p.
    assert heading["elementTags"] == ["DIV", "H3", "P"]


def test_a_malicious_custom_provider_name_renders_as_text_not_elements(
    rendered_open,
) -> None:
    provider = rendered_open["customProvider"]
    assert provider["cardPresent"] is True
    assert provider["nameText"] == PROBE["display_name"]
    # strong (the name) plus the status pill span -- no parsed markup.
    assert provider["elementTags"] == ["STRONG", "SPAN"]


def test_the_messaging_auth_notice_appears_exactly_when_platforms_are_open(
    rendered_open,
    rendered_locked,
) -> None:
    notice = rendered_open["messagingAuthNotice"]
    assert notice["present"] is True
    assert notice["hidden"] is False
    assert "Messaging auth is OPEN" in notice["text"]
    assert "telegram, discord" in notice["text"]
    assert "TELEGRAM_ALLOWED_USER_ID" in notice["text"]
    assert "DISCORD_ALLOWED_CHANNEL_IDS" in notice["text"]

    # Empty means locked everywhere: the notice is removed outright, not
    # rendered empty.
    assert rendered_locked["messagingAuthNotice"]["present"] is False


# --------------------------------------------------------------------- static
# The jsdom runs above cover today's sinks; this scan covers tomorrow's. An
# innerHTML assignment whose right-hand side interpolates a template literal
# is an injection sink by construction, whatever the data looks like today.


def _innerhtml_assignment_rhs(source: str) -> list[tuple[int, str]]:
    """Return (line, rhs) for every innerHTML assignment, template-aware.

    Walks each right-hand side up to the terminating semicolon, tracking
    backticks so a ``;`` inside a template literal does not cut the scan
    short.
    """
    sites: list[tuple[int, str]] = []
    for match in re.finditer(r"innerHTML\s*=", source):
        index = match.end()
        buffer: list[str] = []
        in_template = False
        while index < len(source):
            char = source[index]
            if char == "`":
                in_template = not in_template
                buffer.append(char)
            elif in_template and char == "\\":
                buffer.append(source[index : index + 2])
                index += 2
                continue
            elif char == ";" and not in_template:
                break
            buffer.append(char)
            index += 1
        line = source.count("\n", 0, match.start()) + 1
        sites.append((line, "".join(buffer)))
    return sites


# Assignments allowed to interpolate a template literal, each entry a marker
# substring that must occur in exactly one site. Deliberately EMPTY today:
#
#   - the docs viewer (~line 545) assigns ``data.html || ""`` -- server-
#     rendered HTML produced with raw markdown disabled, carrying no ``${``;
#   - the chart legend concatenates two fully static string literals.
#
# Every other innerHTML use in the file clears a container (``= "";``) or
# assigns a plain literal. If you believe a new sink is justified, add its
# marker here WITH the justification inline -- and a test above that proves
# the data reaching it cannot carry markup.
ALLOWED_INNERHTML_INTERPOLATIONS: list[str] = []


def test_no_innerhtml_assignment_interpolates_a_template_literal() -> None:
    source = (STATIC_DIR / "admin.js").read_text(encoding="utf-8")
    sites = _innerhtml_assignment_rhs(source)

    offenders = []
    for line, rhs in sites:
        stripped = rhs.strip()
        if "${" not in stripped:
            continue
        if any(marker in stripped for marker in ALLOWED_INNERHTML_INTERPOLATIONS):
            continue
        offenders.append(f"line {line}: innerHTML ={stripped[:120]}")

    assert not offenders, (
        "admin.js assigns innerHTML from an interpolated template literal "
        f"outside ALLOWED_INNERHTML_INTERPOLATIONS: {offenders}"
    )


def test_every_allowance_marker_actually_matches_one_site() -> None:
    """A stale allowance entry would silently re-widen the scan."""
    source = (STATIC_DIR / "admin.js").read_text(encoding="utf-8")
    sites = _innerhtml_assignment_rhs(source)
    for marker in ALLOWED_INNERHTML_INTERPOLATIONS:
        matches = sum(marker in rhs for _, rhs in sites)
        assert matches == 1, f"allowance {marker!r} matches {matches} sites"


def test_the_scan_is_not_vacuous() -> None:
    """The scanner must see the real assignment sites, or it guards nothing."""
    source = (STATIC_DIR / "admin.js").read_text(encoding="utf-8")
    sites = _innerhtml_assignment_rhs(source)
    assert len(sites) >= 20, "scanner stopped finding innerHTML assignments"
    # The sanctioned docs-viewer site is still the one non-clearing consumer
    # of remote-derived HTML, and it still interpolates nothing.
    assert any('data.html || ""' in rhs for _, rhs in sites)


def test_no_adjacent_html_injection_vectors_exist() -> None:
    """The other DOM-injection spellings, banned outright rather than policed."""
    source = (STATIC_DIR / "admin.js").read_text(encoding="utf-8")
    for vector in ("insertAdjacentHTML", "document.write"):
        assert vector not in source, f"admin.js uses {vector}"
    assert not re.search(r"\.outerHTML\s*=", source), "admin.js assigns outerHTML"
