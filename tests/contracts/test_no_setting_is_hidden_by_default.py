"""Every setting is on its page, and every setting says what it does.

A user went looking for ``RATE_LIMIT_COOLDOWN_MODE`` and
``RATE_LIMIT_COOLDOWN_MAX_SECONDS`` on Limits & Resilience and concluded they
did not exist. They did: both carried ``advanced=True``, which the dashboard
rendered as ``display: none`` behind a per-card "Show advanced" button. The
field beside them, ``CREDENTIAL_LOCKOUT_TIERS``, was not flagged -- so the card
looked complete while two of its three controls were missing.

The providers section was worse. ``renderSections`` skipped it when attaching
the "Show advanced" button, on the grounds that the section handled advanced
per card -- and no per-card control was ever written. So 110 provider fields
were styled ``display: none`` with nothing anywhere on the dashboard that
could reveal them.

7.29.1 makes ``advanced`` mean two things and no others: the field sorts after
the common ones in its card, and it carries a small "advanced" tag. A collapse
control stays for readers who want the short form; it starts expanded and is
remembered per browser. This file pins that, plus the contract that made the
confusion possible in the first place: every manifest field has real help
text.

Static assertions against the shipped assets, like
``test_admin_limits_view.py`` -- these have to run on every platform, not only
where node and jsdom happen to be installed. The behavioural half (the field
renders, the tag is there, the collapse persists) lives in
``tests/api/test_admin_static_jsdom.py``.
"""

import json
import re
from pathlib import Path

from my_claude_code.config.admin.manifest import FIELDS

CONTROL_MAP = Path(__file__).with_name("settings_control_map.json")

STATIC = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "my_claude_code"
    / "api"
    / "admin_static"
)
ADMIN_JS = (STATIC / "admin.js").read_text(encoding="utf-8")
ADMIN_CSS = (STATIC / "admin.css").read_text(encoding="utf-8")


# ------------------------------------------------------------------- help ---

# The bar: a description that is present, long enough to be a sentence, and
# not a restatement of the label. 20 characters is deliberately low -- it
# rejects "plan-gated" and "Empty = any time." without inviting anyone to pad
# a real line out to a word count.
MIN_DESCRIPTION_CHARS = 20

# Names allowed to ship without help text. 7.24.0's audit found 117 fields
# with an empty description; 7.29.1 wrote one for every one of them, so this
# list is empty and must stay empty. A new field with nothing to say is a new
# field nobody can use -- write the line rather than adding the name here.
HELP_TEXT_ALLOW_LIST: frozenset[str] = frozenset()


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def test_every_manifest_field_has_help_text() -> None:
    missing = sorted(
        field.key
        for field in FIELDS
        if not field.description.strip() and field.key not in HELP_TEXT_ALLOW_LIST
    )
    assert not missing, (
        f"{len(missing)} manifest fields ship with no help text, so the "
        "dashboard renders a label, a control and no explanation: "
        f"{missing}"
    )


def test_help_text_is_a_sentence_not_a_fragment() -> None:
    short = sorted(
        (field.key, field.description.strip())
        for field in FIELDS
        if field.key not in HELP_TEXT_ALLOW_LIST
        and 0 < len(field.description.strip()) < MIN_DESCRIPTION_CHARS
    )
    assert not short, (
        "help text shorter than "
        f"{MIN_DESCRIPTION_CHARS} characters cannot say what a setting does "
        f"and what changing it costs: {short}"
    )


def test_help_text_is_not_a_restatement_of_the_label() -> None:
    echoes = sorted(
        field.key
        for field in FIELDS
        if field.key not in HELP_TEXT_ALLOW_LIST
        and _normalise(field.description) == _normalise(field.label)
    )
    assert not echoes, (
        "these fields repeat their own label as help text, which tells a "
        f"reader nothing they could not already see: {echoes}"
    )


def test_the_allow_list_stays_empty() -> None:
    """A guard on the guard: the bar is never lowered quietly.

    Shipping a field with no help text is allowed only by adding its name
    above, which is a diff a reviewer sees. This test is what makes that the
    only way.
    """
    assert not HELP_TEXT_ALLOW_LIST


# ---------------------------------------------------------------- hiding ---


def test_an_advanced_field_is_not_hidden_by_default() -> None:
    """`.field.advanced-field` must not resolve to `display: none` on its own.

    The only rule allowed to hide one is gated on `.collapse-advanced`, which
    nothing but a reader's own click ever applies.
    """
    rule = re.search(
        r"\.field\.advanced-field\s*\{([^}]*)\}",
        ADMIN_CSS,
    )
    assert rule, "admin.css no longer styles .field.advanced-field at all"
    assert "display: none" not in rule.group(1), (
        "`.field.advanced-field { display: none }` is the whole bug: every "
        "advanced field disappears, and in the providers section nothing "
        "could bring it back"
    )


def test_hiding_is_gated_on_the_readers_own_collapse() -> None:
    hiding = [
        match.group(0)
        for match in re.finditer(r"[^}]*\{[^}]*display:\s*none[^}]*\}", ADMIN_CSS)
        if "advanced-field" in match.group(0)
    ]
    assert hiding, "no rule hides an advanced field, not even a collapsed one"
    for block in hiding:
        assert "collapse-advanced" in block, (
            "an advanced field is hidden by something other than the reader's "
            f"own collapse: {block.strip()[:200]}"
        )


def test_the_show_advanced_toggle_is_gone() -> None:
    """The old control hid by default; its class must not come back."""
    assert "show-advanced" not in ADMIN_JS, (
        "admin.js still emits `show-advanced`, the class whose absence was "
        "what hid every advanced field"
    )
    assert "show-advanced" not in ADMIN_CSS or re.search(
        r"^\s*\*?\s.*show-advanced",
        ADMIN_CSS,
        re.MULTILINE,
    ), "admin.css still has a live .show-advanced rule"


def test_the_providers_section_has_a_per_card_collapse_control() -> None:
    """The section excluded from the old toggle now grows its own.

    `renderSections` still skips `providers`; what changed is that
    `renderProviderCard` attaches a control of its own, so the 110 provider
    fields that had no control anywhere on the dashboard now have one.
    """
    assert "attachAdvancedCollapse(card, `provider:${provider.provider_id}`" in (
        ADMIN_JS
    ), "a provider card no longer attaches its own advanced collapse control"


def test_the_collapse_choice_is_remembered_per_card_in_a_try_catch() -> None:
    assert 'const ADVANCED_COLLAPSE_PREFIX = "mcc.advancedCollapsed."' in ADMIN_JS
    for name in ("advancedCollapsed", "rememberAdvancedCollapsed"):
        body = re.search(rf"function {name}\(.*?\n\}}", ADMIN_JS, re.DOTALL)
        assert body, f"{name}() is gone"
        assert "try {" in body.group(0) and "catch" in body.group(0), (
            f"{name}() touches localStorage without a try/catch, which throws "
            "in a private window and takes the whole render with it"
        )


# -------------------------------------------------------------- ordering ---


def test_advanced_only_orders_a_field_after_the_common_ones() -> None:
    helper = re.search(r"function advancedLast\(fields\) \{(.*?)\n\}", ADMIN_JS, re.S)
    assert helper, "advancedLast() -- the whole ordering rule -- is gone"
    body = helper.group(1)
    assert ".sort(" in body, "advancedLast() no longer sorts"
    assert "[...fields]" in body, (
        "advancedLast() sorts the caller's array in place, which reorders "
        "state that other views read"
    )


def test_both_the_grid_and_the_provider_cards_use_that_one_rule() -> None:
    """One ordering rule, applied in both places, or the two drift apart."""
    assert ADMIN_JS.count("advancedLast(") >= 3, (
        "advancedLast() is defined but not applied by both renderSections() "
        "and renderProviderCard()"
    )
    assert "const ordered = advancedLast(fields);" in ADMIN_JS


def test_the_advanced_tag_is_emitted_and_styled() -> None:
    assert 'tag.className = "advanced-tag"' in ADMIN_JS
    assert 'tag.textContent = "advanced"' in ADMIN_JS
    assert re.search(r"\.advanced-tag\s*\{", ADMIN_CSS), (
        "admin.js applies .advanced-tag but admin.css never styles it"
    )


# ------------------------------------------------------------- hosting ---
# Hidden is not only `display: none`. The generic grid in renderSections()
# leaves two kinds of field out because another renderer draws them -- and
# until 7.121.2 one kind, the thirteen web-search `_ROTATION` fields, was left
# out with nothing drawing it at all: savable in .env, visible nowhere. These
# pin the skip to exactly those two kinds and each kind to its host, and check
# every skipped field against the control map the jsdom suite proves is the
# page.

#: The grid's one filter, as written in renderSections(). A new clause here is
#: a new way for a field to vanish, so it must be argued in this test first.
_GRID_SKIP_PREDICATE = (
    '!field.key.endsWith("_ROTATION") && '
    '!(field.section === "websearch" && field.advanced)'
)


def _function_body(name: str) -> str:
    body = re.search(rf"function {name}\([^)]*\) \{{(.*?)\n\}}", ADMIN_JS, re.S)
    assert body, f"admin.js no longer defines {name}()"
    return body.group(1)


def test_the_grid_skips_only_the_two_kinds_another_renderer_draws() -> None:
    skip = re.search(
        r"const gridFields = advancedLast\(\s*sectionFields\.filter\(\s*"
        r"\(field\) =>\s*(?P<predicate>.*?),?\s*\),\s*\);",
        ADMIN_JS,
        re.S,
    )
    assert skip, "renderSections() no longer builds gridFields from one filter"
    predicate = " ".join(skip.group("predicate").split())
    assert predicate == _GRID_SKIP_PREDICATE, (
        "renderSections() now leaves a different set of fields out of the grid: "
        f"{predicate!r}. Every field it skips must be drawn somewhere else."
    )


def test_each_skipped_kind_has_its_host() -> None:
    """Provider rotation -> the key manager; web-search rotation -> its card;
    web-search options -> the card's Advanced options group."""

    assert "const rotationField = state.fields.get(`${field.key}_ROTATION`);" in (
        _function_body("keyManagerForField")
    ), "keyManagerForField() no longer draws a provider pool's rotation select"
    assert "card.appendChild(renderField(rotationField))" in (
        _function_body("renderWebSearchProviders")
    ), "a Web Search card no longer draws its key pool's rotation field (7.121.2)"
    assert "details.appendChild(renderField(field))" in (
        _function_body("renderWebSearchAdvanced")
    ), "renderWebSearchAdvanced() no longer draws the cards' option fields"


def test_every_field_the_grid_skips_has_a_control_on_its_page() -> None:
    entries = json.loads(CONTROL_MAP.read_text(encoding="utf-8"))["fields"]
    skipped = [
        field
        for field in FIELDS
        if field.key.endswith("_ROTATION")
        or (field.section_id == "websearch" and field.advanced)
    ]
    web_search_rotations = [
        field
        for field in skipped
        if field.section_id == "websearch" and field.key.endswith("_ROTATION")
    ]
    assert len(web_search_rotations) == 13, "the scan no longer finds the 13"
    missing = sorted(
        field.key
        for field in skipped
        if (entries.get(field.key) or {}).get("control") != f"field-{field.key}"
    )
    assert not missing, (
        f"these fields are left out of the generic grid and drawn by nothing: {missing}"
    )
