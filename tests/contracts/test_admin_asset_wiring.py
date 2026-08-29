"""Coupling checks between `admin.js`, `index.html`, and `admin.css`.

`admin.js` reaches into the DOM by id, emits CSS class names it expects
`admin.css` to style, and drives navigation off a table of view ids it
expects `index.html` to declare a section for. None of that is checked by
the type checker, a unit test, or the Python side of the app -- the three
assets only agree with each other by convention, and the project has
already shipped every kind of disagreement once:

* a control's id was looked up by `byId(...)` but the element was never
  appended to the DOM, so the lookup returned null and the feature was
  silently inert for four releases;
* a bulk string replace injected an unrelated id into a shared function,
  so `controlId is not defined` would have thrown on three other pages;
* six CSS classes were emitted by JS with no matching rule after a bad
  merge silently dropped a stylesheet block -- the page loaded and every
  test passed;
* CSS declarations referenced `var(--token)` custom properties that were
  never defined, which makes the whole declaration invalid at computed-
  value time -- the rule is dropped, not defaulted, so the symptom is
  geometry (`padding: 0`), not colour.

Each guard below parses the shipped files directly, the same way
`test_admin_sections_are_rendered.py` reads `VIEW_GROUPS` out of the
JavaScript instead of a Python mirror of it -- a mirror would only ever
agree with itself.
"""

import re
from pathlib import Path

ADMIN_STATIC = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "my_claude_code"
    / "api"
    / "admin_static"
)
ADMIN_JS = ADMIN_STATIC / "admin.js"
INDEX_HTML = ADMIN_STATIC / "index.html"
ADMIN_CSS = ADMIN_STATIC / "admin.css"

_JS_SOURCE = ADMIN_JS.read_text(encoding="utf-8")
_HTML_SOURCE = INDEX_HTML.read_text(encoding="utf-8")
_CSS_SOURCE = ADMIN_CSS.read_text(encoding="utf-8")


# --------------------------------------------------------------------- A ---
# byId(...) / getElementById(...) lookups must resolve in the shipped markup.

_ID_LOOKUP = re.compile(r"""\b(?:byId|getElementById)\(\s*["']([^"']+)["']\s*\)""")
_MARKUP_ID = re.compile(r"""\bid=["']([^"']+)["']""")

# Ids that byId()/getElementById() looks up but that only ever exist because
# JS creates and appends the element itself, never because index.html
# declares one. An entry here without a real dynamic-creation site would
# hide exactly the defect this guard exists to catch, so keep it empty
# unless you can point at the `createElement` + `id = "..."` (or
# `.id = ...`) call.
KNOWN_DYNAMIC_ONLY_IDS: set[str] = {
    # Created by renderOptimizerSettings() in admin.js -- the heading note for
    # the per-tool trimming rules, whose text depends on the master switch.
    # It lives inside the section the settings renderer builds, so it cannot
    # be declared in index.html: that container is emptied on every render.
    "optPerToolNote",
    # messagingAuthNotice: built dynamically by renderMessagingAuthNotice
    # (admin.js ~1144); it is never declared in index.html.
    "messagingAuthNotice",
    # field-FALLBACK_SKIP_KINDS: buildFieldControl() ids every control
    # `field-<KEY>`, so this one exists only once the Model Config view has
    # rendered. The Benching card's cross-link focuses it after switching view.
    "field-FALLBACK_SKIP_KINDS",
}


def _js_id_lookups() -> set[str]:
    return set(_ID_LOOKUP.findall(_JS_SOURCE))


def _html_ids() -> set[str]:
    return set(_MARKUP_ID.findall(_HTML_SOURCE))


def test_the_id_lookup_scan_is_parsable() -> None:
    """A silent parse failure would make the assertions below vacuous."""

    lookups = _js_id_lookups()
    assert lookups, "found no byId(...)/getElementById(...) calls in admin.js"
    # Sanity anchors: long-standing ids that must always be looked up.
    assert {"sectionNav", "pageTitle"} <= lookups


def test_every_id_lookup_in_admin_js_exists_in_index_html() -> None:
    """A lookup for an id index.html never declares returns null silently.

    This is the defect that shipped a control whose edits could never be
    saved: the element existed in memory, `byId(...)` found nothing because
    it was never appended to the DOM, and no error surfaced anywhere.
    """

    lookups = _js_id_lookups() - KNOWN_DYNAMIC_ONLY_IDS
    declared = _html_ids()

    missing = sorted(lookups - declared)
    assert not missing, (
        "admin.js looks these ids up with byId(...)/getElementById(...) but "
        f'index.html declares no matching id="...": {missing}. Either add '
        "the id in src/my_claude_code/api/admin_static/index.html, or if the "
        "element is created dynamically by admin.js, add it to "
        "KNOWN_DYNAMIC_ONLY_IDS in this file with a comment pointing at the "
        "createElement call."
    )


# --------------------------------------------------------------------- B ---
# CSS classes emitted by admin.js must have a matching rule in admin.css.
#
# What this extracts:
#   - `el.classList.add("a", "b")` where every argument is a plain quoted
#     string literal (multiple classes in one call are each taken).
#   - `el.className = "literal"` / `el.className = 'literal'`.
#   - `el.className = \`template literal\``: split into the *static* text
#     segments around each `${...}` interpolation (matched by brace depth,
#     so a nested template literal like `` `route-node${m ? ` ${m}` : ""}` ``
#     is skipped as a whole, correctly, rather than breaking on its inner
#     backtick), then split each static segment on whitespace. A word that
#     sits directly against an interpolation boundary with no whitespace
#     -- e.g. the "cc-diff-" in `` `cc-diff-${change.op}` `` -- is a name
#     fragment, not a class, and is dropped rather than checked.
#   - `class="literal"` attributes that appear inside JS string/template
#     literals, only when the attribute value contains no `${` (an
#     interpolated attribute value is skipped, not guessed at).
#
# What this deliberately skips (never turned into a class name to check):
#   - any `classList.add(...)` argument that is not a bare string literal
#     (a variable, ternary, or function call);
#   - the interpolated `${...}` portion of any template literal, and any
#     literal word glued directly to one;
#   - `class="..."` attributes whose value contains an interpolation.
# A construct this loose about matters is exactly the kind that produced
# noise before, so ambiguous cases are skipped rather than emitting a
# fragment that was never a real class name.

_CLASSLIST_ADD = re.compile(r"\.classList\.add\(([^)]*)\)")
_CLASSNAME_ASSIGN = re.compile(
    r"""className\s*=\s*(`(?:[^`\\]|\\.)*`|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')"""
)
_STATIC_CLASS_ATTR = re.compile(r'class="([^"$]*)"')
_SIMPLE_STRING_LITERAL = re.compile(r"""^["']([^"']*)["']$""")


def _template_literal_static_segments(body: str) -> list[str]:
    """Static text segments of a template literal body, split around each
    top-level `${...}` interpolation (matched by brace depth, so a nested
    template literal inside the interpolation cannot desync the parse)."""

    segments: list[str] = []
    buffer: list[str] = []
    i = 0
    length = len(body)
    while i < length:
        char = body[i]
        if char == "$" and i + 1 < length and body[i + 1] == "{":
            segments.append("".join(buffer))
            buffer = []
            depth = 1
            i += 2
            while i < length and depth > 0:
                if body[i] == "{":
                    depth += 1
                elif body[i] == "}":
                    depth -= 1
                i += 1
            continue
        buffer.append(char)
        i += 1
    segments.append("".join(buffer))
    return segments


def _words_dropping_glued_fragments(segments: list[str]) -> list[str]:
    words: list[str] = []
    last = len(segments) - 1
    for index, segment in enumerate(segments):
        segment_words = segment.split()
        if not segment_words:
            continue
        starts_at_boundary = index == 0 or segment[:1].isspace()
        ends_at_boundary = index == last or segment[-1:].isspace()
        if not starts_at_boundary:
            segment_words = segment_words[1:]
        if not ends_at_boundary:
            segment_words = segment_words[:-1]
        words.extend(segment_words)
    return words


def _classes_emitted_by_admin_js() -> set[str]:
    classes: set[str] = set()

    for args_source in _CLASSLIST_ADD.findall(_JS_SOURCE):
        for arg in (a.strip() for a in args_source.split(",")):
            literal = _SIMPLE_STRING_LITERAL.match(arg)
            if literal:
                classes.update(literal.group(1).split())

    for literal in _CLASSNAME_ASSIGN.findall(_JS_SOURCE):
        quote, body = literal[0], literal[1:-1]
        if quote == "`":
            segments = _template_literal_static_segments(body)
            classes.update(_words_dropping_glued_fragments(segments))
        else:
            classes.update(body.split())

    for attr_value in _STATIC_CLASS_ATTR.findall(_JS_SOURCE):
        classes.update(attr_value.split())

    return classes


_CSS_CLASS_RULE = re.compile(r"\.([a-zA-Z_][a-zA-Z0-9_-]*)")


def _classes_defined_in_admin_css() -> set[str]:
    return set(_CSS_CLASS_RULE.findall(_CSS_SOURCE))


# Classes admin.js emits with no matching rule in admin.css, confirmed by
# reading the surrounding markup rather than guessing at styling: each is a
# plain wrapper/button whose layout already comes from a sibling class
# (`.cc-section-title`/`.cc-section-count` inside `.cc-section`, the
# `.secondary-button` half of `secondary-button cc-rule-add`, or default
# block flow for the `.cc-row-body` wrapper div). Guessing at spacing rules
# to close this out risks a visual regression the guard cannot see; each
# entry names the element that has no rule so a future change can style it
# deliberately instead of by accident.
#
# Measured, so a future reader knows which fix each one wants: `cc-section` is
# the only one whose CHILDREN are styled (`.cc-section-title`,
# `.cc-section-count`) while the parent is not, so it is a missing rule. The
# other two are referenced nowhere at all -- no CSS, no selector, no test --
# so they are dead class names and deleting them from admin.js is the fix,
# not styling them.
KNOWN_UNSTYLED_CLASSES = {
    "cc-section",  # <section> wrapper; children styled, parent has no rule
    "cc-row-body",  # dead name: zero references outside its own assignment
    "cc-rule-add",  # dead name: the .secondary-button half does all the work
}


def test_the_class_emission_scan_is_parsable() -> None:
    """A silent parse failure would make the assertion below vacuous."""

    emitted = _classes_emitted_by_admin_js()
    assert emitted, "found no classList.add(...)/className assignments in admin.js"
    # Sanity anchor: a long-standing class that must always be found.
    assert "onboarding-highlight" in emitted


def test_every_css_class_emitted_by_admin_js_has_a_rule_in_admin_css() -> None:
    """A class admin.js applies but admin.css never styles is invisible.

    This is the defect that dropped a stylesheet block in a bad conflict
    resolution: six classes kept being applied by JS, the page kept
    loading, and every existing test kept passing, because nothing checked
    that a rule still existed for them.
    """

    emitted = _classes_emitted_by_admin_js() - KNOWN_UNSTYLED_CLASSES
    defined = _classes_defined_in_admin_css()

    unstyled = sorted(emitted - defined)
    assert not unstyled, (
        "admin.js applies these CSS classes but admin.css has no matching "
        f"rule for them: {unstyled}. Add a rule in "
        "src/my_claude_code/api/admin_static/admin.css, or if the class is "
        "genuinely unstyled on purpose, add it to KNOWN_UNSTYLED_CLASSES in "
        "this file with a reason."
    )


# --------------------------------------------------------------------- C ---
# Every var(--token) used in admin.css must resolve to a defined token,
# either directly or through its fallback chain.

_CSS_NO_COMMENTS = re.sub(r"/\*.*?\*/", "", _CSS_SOURCE, flags=re.DOTALL)
_CUSTOM_PROPERTY_DEFINITION = re.compile(r"(--[a-zA-Z0-9-]+)\s*:")
_VAR_REFERENCE = re.compile(r"var\(\s*(--[a-zA-Z0-9-]+)\s*(,)?")


def _defined_custom_properties() -> set[str]:
    return set(_CUSTOM_PROPERTY_DEFINITION.findall(_CSS_NO_COMMENTS))


def _var_references_without_a_fallback() -> set[str]:
    """Names referenced by `var(--token)` with no `, fallback` at all.

    A fallback -- literal or another `var(...)`, including a chain like
    `var(--a, var(--b))` -- means an undefined `--a` still resolves, so
    only a token with no fallback whatsoever is a defect if undefined.
    """

    undefended: set[str] = set()
    for match in _VAR_REFERENCE.finditer(_CSS_NO_COMMENTS):
        name, has_comma = match.group(1), match.group(2)
        if not has_comma:
            undefended.add(name)
    return undefended


def test_the_custom_property_scan_is_parsable() -> None:
    """A silent parse failure would make the assertion below vacuous."""

    defined = _defined_custom_properties()
    assert defined, "found no `--token: ...` custom property definitions in admin.css"
    assert "--line-strong" in defined


def test_every_fallback_free_css_variable_is_defined() -> None:
    """A `var(--token)` with no fallback and no definition drops the rule.

    An unresolvable custom property makes the whole declaration invalid at
    computed-value time -- not "fall back to nothing", the declaration is
    dropped -- so the shipped symptom is broken geometry, not colour, and
    nothing before this guard checked it.
    """

    defined = _defined_custom_properties()
    referenced_without_fallback = _var_references_without_a_fallback()

    undefined = sorted(referenced_without_fallback - defined)
    assert not undefined, (
        "admin.css uses these custom properties with no fallback value and "
        f"no matching definition: {undefined}. Either define them (e.g. in "
        "the `:root` block) or add a `var(--token, fallback)` fallback in "
        "src/my_claude_code/api/admin_static/admin.css."
    )


# --------------------------------------------------------------------- D ---
# Every VIEW_GROUPS view id must resolve to a real `data-view="..."` section
# in index.html, and vice versa -- the nav is JS-rendered buttons keyed off
# VIEW_GROUPS[].id, matched at runtime against `.admin-view[data-view]`
# sections that only exist statically in the markup.

_VIEW_GROUPS_BLOCK = re.compile(r"const VIEW_GROUPS = \[(.*?)\n\];", re.DOTALL)
_VIEW_GROUP_ID = re.compile(r"""\bid:\s*["']([^"']+)["']""")
_DATA_VIEW_ATTR = re.compile(r"""\bdata-view=["']([^"']+)["']""")


def _view_group_ids() -> set[str]:
    match = _VIEW_GROUPS_BLOCK.search(_JS_SOURCE)
    assert match, "could not find `const VIEW_GROUPS = [...]` in admin.js"
    return set(_VIEW_GROUP_ID.findall(match.group(1)))


def _data_view_sections() -> set[str]:
    return set(_DATA_VIEW_ATTR.findall(_HTML_SOURCE))


def test_the_view_group_id_scan_is_parsable() -> None:
    """A silent parse failure would make the assertions below vacuous."""

    ids = _view_group_ids()
    assert ids, 'found no `id: "..."` entries inside VIEW_GROUPS in admin.js'
    assert {"providers", "get_started"} <= ids


def test_every_view_group_id_has_a_matching_data_view_section() -> None:
    """A VIEW_GROUPS id with no `data-view` section can be selected but
    never shown: `setActiveView` toggles `.admin-view` elements by matching
    `view.dataset.view === activeView.id`, so a nav click for an id with no
    matching section hides every view and shows nothing.
    """

    view_ids = _view_group_ids()
    sections = _data_view_sections()

    missing = sorted(view_ids - sections)
    assert not missing, (
        "these VIEW_GROUPS ids in admin.js have no matching "
        f'`data-view="..."` section in index.html: {missing}. Add '
        '`<section class="admin-view" data-view="...">` for each in '
        "src/my_claude_code/api/admin_static/index.html, or remove the "
        "VIEW_GROUPS entry."
    )


def test_every_data_view_section_has_a_matching_view_group_id() -> None:
    """The mirror of the above: an orphaned `data-view` section can never
    be navigated to, because no nav button is ever rendered for it.
    """

    view_ids = _view_group_ids()
    sections = _data_view_sections()

    orphaned = sorted(sections - view_ids)
    assert not orphaned, (
        'these data-view="..." sections in index.html have no matching id '
        f"in VIEW_GROUPS in admin.js, so no nav button ever shows them: "
        f"{orphaned}. Either add a VIEW_GROUPS entry with that id in "
        "src/my_claude_code/api/admin_static/admin.js, or remove the "
        "orphaned section."
    )
