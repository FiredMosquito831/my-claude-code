"""CARDS-FIX-2 — the custom-provider card is a face, and its panel holds all nine controls.

USER 2026-11-04: *"also i noticed that custom providers didn't get the same
benefits that normal providers and websearch providers did."*

``customProviderCard`` was a separate renderer 8.10.0 never touched: no face,
no dialog, nine controls inline. ``specs/PR-CARDS-FIX-2.md`` is the contract and
``specs/UI-QUALITY-RULE.md`` §D.1 is the rule behind it. Everything here runs
the real ``index.html``, ``admin.js`` and ``admin.css`` in jsdom through
``admin_jsdom_custom_cards.mjs``; nothing is mocked past the fetch stub.

What each section proves, and why it exists:

1. **THE FACE** — every custom card is ``.provider-card .pv-card``, is a
   ``role="button"``, is focusable, carries the same three tight rows a built-in
   face carries (name+status / Refresh models+Configure / the model count), and
   holds none of the card's old body: no ``.cp-details``, no ``.cp-dialect``,
   no key rail, no ``[data-key]``.

2. **THE COMPLETENESS LIST** (§D.1 point 7, the hard part) — the nine controls
   the card carried are each asserted present, by selector AND by the label
   they rendered before this PR. ``EXPECTED_CONTROLS`` is the list; a control
   dropped from the body shows up as a missing id.

3. **THE THREE REGIONS** — ``.cp-details``, ``.cp-dialect`` and the key rail,
   in the unified body, with the masked keys in order and one Remove per key.

4. **DELETE AND DISABLE ARE NOT NEAR THE PRIMARY POSITION** — measured as each
   control's index in the body's own button order. Delete must be last, Disable
   second-to-last, both at least ``SAFE_DISTANCE`` from the first control, and
   neither may be the first thing in the body.

5. **THE MODEL COUNT** — it tracks a refresh from ITS source
   (``provider.model_count`` in the listing payload, never
   ``state.modelOptions``), with CARDS-FIX-1's three states; the pill is a
   status and never a number; the busy word is never stomped by a repaint; and
   the face and the panel's details line agree in the same tick.

6. **A RE-RENDER WHILE OPEN** — ``renderSections`` and ``loadCustomProviders``
   both rebuild the grid under the dialog, which is where these bugs live.

7. **EVERY OTHER VIEW UNMOVED** — the same whole-view hash comparison
   ``test_admin_cards_overlay_jsdom.py`` runs, with a CEILING and one declared
   move. ``providers`` moves because the custom cards on it are faces now.

8. **THE TWO MUTATION CONTROLS** (§4.6) — a control is deleted from the body in
   a copy of the shipped ``admin.js`` and the completeness test must fail; Delete
   is moved next to Edit and the position test must fail.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.api.test_admin_static_jsdom import STATIC_DIR

BASELINE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "cards_overlay_dom_8_9_0.json"
)
CARDS_HARNESS = Path(__file__).with_name("admin_jsdom_custom_cards.mjs")
OVERLAY_HARNESS = Path(__file__).with_name("admin_jsdom_cards_overlay.mjs")

#: CI must not pass a suite it never ran.
ON_CI = os.environ.get("MCC_CI", "") not in ("", "0", "false")

pytestmark = pytest.mark.xdist_group("admin_static_jsdom")

#: How far a destructive control must sit from the panel's primary position.
#: A row of six key controls, the Add form, the dialect block's five and the
#: action row put Delete at index 19 of 20 -- the measurement is in the report.
SAFE_DISTANCE = 4


def _run_node(harness: Path, static_dir: Path) -> dict:
    node = shutil.which("node")
    if node is None:
        if ON_CI:
            pytest.fail("MCC_CI is set but node is not on PATH")
        pytest.skip("node is not on PATH")
    result = subprocess.run(
        [node, str(harness), str(static_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        env=os.environ.copy(),
    )
    if result.returncode != 0:
        if "Cannot find package 'jsdom'" in result.stderr:
            if ON_CI:
                pytest.fail("MCC_CI is set but jsdom is not installed")
            pytest.skip("jsdom is not installed")
        pytest.fail(f"{harness.name} failed: {result.stderr[-2000:]}")
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def cards() -> dict:
    return _run_node(CARDS_HARNESS, STATIC_DIR)


@pytest.fixture(scope="module")
def overlay() -> dict:
    """The CARDS-OVERLAY harness, whose view hashes the baseline was written from."""
    return _run_node(OVERLAY_HARNESS, STATIC_DIR)


# --------------------------------------------------------------------------
# 1. the card is a face
# --------------------------------------------------------------------------


def test_a_custom_card_is_a_face(cards) -> None:
    faces = cards["face"]

    assert faces, "the custom provider grid did not render"
    for face in faces:
        assert "provider-card" in face["className"]
        assert "pv-card" in face["className"], face["provider"]
        assert face["role"] == "button"
        assert face["tabIndex"] == 0
        assert face["ariaHaspopup"] == "dialog"
        # The same three tight rows a built-in face draws: name+status, the
        # action row, the model count.
        assert face["children"] == ["provider-title", "pv-actions", "ghost-button"]


def test_the_face_carries_the_five_decisions_and_nothing_else(cards) -> None:
    """Name, status, model count, Refresh models, Configure.

    USER Q2 (`all rec`): "Configure" opens the panel and **Edit stays inside
    it**, so the two card types read the same. An "Edit" on this face would be
    the sixth thing and the reason the count of five stopped being the point.
    """
    faces = {face["provider"]: face for face in cards["face"]}

    assert faces["custom_probe"]["controls"] == [
        "Configure",
        "Refresh models",
        "Not detected yet",
    ]


def test_no_region_of_the_old_card_body_is_left_on_the_face(cards) -> None:
    for face in cards["face"]:
        assert face["hasDetails"] is False, face["provider"]
        assert face["hasDialect"] is False, face["provider"]
        assert face["hasKeyList"] is False, face["provider"]
        assert face["dataKeys"] == 0, face["provider"]
        # The rail is in the panel, so the card no longer claims the whole
        # grid row for it.
        assert face["hasRailHost"] is False, face["provider"]


def test_a_custom_card_answers_the_pages_own_filters(cards) -> None:
    """The Providers page's find box and "Only configured" walk `.pv-card`."""
    faces = {face["provider"]: face for face in cards["face"]}
    probe = faces["custom_probe"]

    assert probe["configured"] == "true"
    assert faces["custom_quiet"]["configured"] == "false"
    # The haystack is curated, never the card's rendered text, and it holds
    # what a reader would search by: the name, the id, the URL, a key mask and
    # the doors this host serves.
    assert "probe gateway" in probe["search"]
    assert "https://api.probe.example/v1" in probe["search"]
    assert "responses" in probe["search"]


# --------------------------------------------------------------------------
# 2. the completeness list -- §D.1 point 7
# --------------------------------------------------------------------------

#: The nine controls the pre-CARDS-FIX-2 card rendered inline, in the spec's
#: §2 order, keyed by the harness's own id. This is a DEAD LIST on purpose: it
#: must not be derived from what the page happens to render, or dropping a
#: control would drop it from the expectation too.
EXPECTED_CONTROLS = {
    "delete": "Delete",
    "enable-disable": "Disable",
    "edit": "Edit",
    "remove": "Remove",
    "add-key": "Add key",
    "dialect-save": "Save",
    "probe-dialect": "Probe reasoning dialect",
    "probe-capabilities": "Probe capabilities",
    "forget-learned": "Forget everything learned",
}


def test_the_panel_holds_all_nine_controls(cards) -> None:
    """Dropping any one of these is the failure §D.1 point 7 exists to catch."""
    panel = cards["panel"]
    found = {control["id"]: control for control in panel["controls"]}

    assert panel["missing"] == [], (
        "the panel lost a control the card used to carry: "
        f"{[EXPECTED_CONTROLS[c] for c in panel['missing']]}"
    )
    assert panel["wrongLabel"] == []
    for control_id, label in EXPECTED_CONTROLS.items():
        assert found[control_id]["expected"] == label, control_id
        assert found[control_id]["found"], control_id


def test_every_masked_key_has_its_own_remove(cards) -> None:
    """One Remove per masked key, not a single row with a loop."""
    panel = cards["panel"]

    assert panel["keyLabels"] == [
        "prb-…aaaa",
        "prb-…bbbb",
        "prb-…cccc",
    ]
    assert panel["removeCount"] == len(panel["keyLabels"]) == 3


def test_the_add_key_form_is_the_shared_rail_one(cards) -> None:
    """The multi-key paste box, the optional name, the button -- in that order."""
    panel = cards["panel"]

    assert panel["addShape"] == [
        "INPUT|password|key-add-secret",
        "INPUT|text|key-name-input key-add-name",
        "BUTTON|button|secondary-button key-add-submit",
    ]
    assert "several separated by commas" in panel["addPlaceholder"]


def test_the_panel_opens_as_a_dialog_for_the_provider_that_was_pressed(
    cards,
) -> None:
    panel = cards["panel"]

    assert panel["opened"] is True
    assert panel["role"] == "dialog"
    assert panel["ariaModal"] == "true"
    assert panel["ariaLabelledBy"] == "providerDetailTitle"
    assert panel["title"] == "Probe Gateway"


# --------------------------------------------------------------------------
# 3. the three regions
# --------------------------------------------------------------------------


def test_the_panel_carries_the_three_regions_the_card_had(cards) -> None:
    """`.cp-details`, `.cp-dialect` and the key rail -- moved, not rewritten.

    They keep their own classes and their own places, which is what makes the
    completeness proof a count instead of a judgement call.
    """
    panel = cards["panel"]

    assert panel["regions"] == [
        "provider-meta",
        "cp-details",
        "key-manager-list",
        "cp-dialect",
        "pv-detail-actions",
    ]
    assert panel["baseUrl"] == "https://api.probe.example/v1"
    # Every clause the card's own line carried, in its order.
    assert panel["details"] == (
        "3 keys · failover · serves chat_completions, responses "
        "· media speech · 0 models · proxy http://127.0.0.1:7890"
    )
    assert panel["dialectLabel"] == (
        "reasoning dialect: learned {low, high, max} on 2026-09-01"
    )
    assert panel["dialectValue"] == "low, high, max"


def test_the_count_on_the_speed_dialect_line_comes_from_the_listing(cards) -> None:
    """It is `provider.model_count`, never a count of `state.modelOptions`.

    The label function is called directly with a payload whose count is 44 and
    whose id has no refs at all: a reading of the shared list could not answer
    "44 models" for it.
    """
    assert cards["count"]["source"] == "44 models"


def test_nothing_in_the_panel_is_a_setting_control(cards) -> None:
    """``changedValues()`` walks the whole document for ``[data-key]``.

    A custom card's panel holds no manifest setting, so it must contribute none
    -- on the face and in the body alike.
    """
    assert cards["formIsOnlyInThePanel"] == {"onTheCards": 0, "inThePanel": 0}


# --------------------------------------------------------------------------
# 4. Delete and Disable are not near the primary position
# --------------------------------------------------------------------------


def test_delete_is_the_last_control_and_disable_the_one_before_it(cards) -> None:
    """A mis-click on Delete inside a panel is a data loss.

    Asserted by POSITION, not by eye: the destructive pair sits at the end of
    the body's own button order, after the settings, after the key rail, and
    after the dialect block's own destructive control.
    """
    danger = cards["danger"]

    assert danger["deleteIsLast"] is True, danger["order"]
    assert danger["deleteIndex"] == danger["lastIndex"]
    assert danger["toggleIndex"] == danger["lastIndex"] - 1
    # Edit sits with them -- USER Q2: it is a panel control, not a face one.
    assert danger["editIndex"] == danger["lastIndex"] - 2


def test_neither_destructive_control_is_at_the_primary_position(cards) -> None:
    danger = cards["danger"]

    assert danger["firstIsDestructive"] is False
    assert danger["deleteDistance"] >= SAFE_DISTANCE
    assert danger["toggleDistance"] >= SAFE_DISTANCE
    assert danger["safe"] is True
    # And not adjacent to Edit either: the pair is separated from the settings
    # by the block's own top border, and Delete from Disable by one button.
    assert "Delete" not in danger["order"][:SAFE_DISTANCE]


# --------------------------------------------------------------------------
# 5. the model count, from ITS source
# --------------------------------------------------------------------------


def test_the_count_tracks_a_refresh_in_both_directions(cards) -> None:
    """§4.3 -- "refresh when the refreshes happen", from `provider.model_count`.

    Non-empty is not the property. Two loads of the product's own path give two
    different numbers.
    """
    count = cards["count"]

    assert count["afterFewer"] == "3 models"
    assert count["afterMore"] == "6 models"


def test_the_three_states_are_three_sentences(cards) -> None:
    """counted / zero / never detected -- CARDS-FIX-1's three, same words.

    "No models detected" is a claim about the provider: it answered and offered
    none. A provider nobody has asked has an UNKNOWN list (IV.16).
    """
    states = cards["count"]["states"]

    assert states == {
        "counted": "6 models",
        "zero": "No models detected",
        "neverDetected": "Not detected yet",
    }
    assert len(set(states.values())) == 3
    # Both cards' first paint read the unknown state: a listing whose count is
    # 0 has not been asked by anything yet.
    assert cards["face"][0]["detect"] == "Not detected yet"
    assert cards["face"][1]["detect"] == "Not detected yet"


def test_the_pill_is_a_status_and_never_a_count(cards) -> None:
    """The same rule CARDS-FIX-1 gave a built-in card's pill."""
    count = cards["count"]

    assert count["pillAfterRefresh"] == "Configured"
    assert count["pillIsANumber"] is False


def test_a_repaint_never_stomps_the_busy_word(cards) -> None:
    """§4.5, ported: a repaint landing mid-Detect must not put a number there."""
    busy = cards["count"]["busy"]

    assert busy["whileBusy"] == {"text": "Detecting", "disabled": True}
    # The repaint really did run, with a different count, and the button moved.
    assert busy["duringARepaint"] == "Detecting"
    assert busy["afterTheCall"] == {"text": "6 models", "disabled": False}


def test_the_face_and_the_panel_agree_after_a_refresh(cards) -> None:
    """One source, three readings: face, `.cp-details`, subtitle."""
    repaint = cards["panelRepaint"]

    assert repaint["opened"] is True
    assert repaint["face"] == "1 model"
    assert repaint["faceSaysThePanel"] is True
    assert "· 1 models ·" in repaint["after"]
    assert "· 1 models ·" in repaint["subtitle"]
    # And the number really moved, so this is not three readings of one value.
    assert "· 6 models ·" in repaint["before"]


# --------------------------------------------------------------------------
# 6. keyboard, focus, and a re-render while it is open
# --------------------------------------------------------------------------


def test_the_card_answers_the_keyboard_like_a_face(cards) -> None:
    keyboard = cards["keyboard"]

    assert keyboard["enterOpened"] is True
    assert keyboard["enterThenEscape"] is True
    assert keyboard["spaceOpened"] is True
    assert keyboard["escapeClosed"] is True


def test_escape_closes_and_gives_focus_back_to_the_card(cards) -> None:
    keyboard = cards["keyboard"]

    assert keyboard["closed"] is True
    assert keyboard["bodyCleared"] is True
    assert keyboard["focusReturned"] is True


def test_the_two_face_controls_act_and_open_nothing(cards) -> None:
    """Refresh models and the count are the same call, two busy words."""
    keyboard = cards["keyboard"]

    assert keyboard["detectBusyWord"] == "Detecting"
    assert keyboard["detectOpenedThePanel"] is False
    assert keyboard["refreshBusyWord"] == "Refreshing"
    assert keyboard["refreshOpenedThePanel"] is False


def test_a_re_render_keeps_the_panel_open_and_correct(cards) -> None:
    """`renderSections` and `loadCustomProviders` both rebuild the grid."""
    reread = cards["reRead"]

    assert reread["titleBefore"] == "Probe Gateway"
    assert reread["removeBefore"] == 3
    assert reread["afterSections"]["stillOpen"] is True
    assert reread["afterSections"]["title"] == "Probe Gateway"
    assert reread["afterSections"]["removeCount"] == 3
    assert reread["afterListing"] == {
        "stillOpen": True,
        "title": "Probe Gateway",
        "removeCount": 3,
        "deleteCount": 1,
    }


def test_a_deleted_provider_takes_its_panel_with_it(cards) -> None:
    """A dialog editing an entry that no longer exists is worse than a closed one."""
    gone = cards["deletedProvider"]

    assert gone["openBefore"] is True
    assert gone["cardGone"] is True
    assert gone["openAfter"] is False


def test_the_harness_runs_clean(cards) -> None:
    assert cards["fatal"] is None
    assert cards["scriptErrors"] == []
    assert cards["consoleErrors"] == []


# --------------------------------------------------------------------------
# 7. every other view unmoved -- a CEILING, never an equality
# --------------------------------------------------------------------------

#: The one view whose bytes this PR changes on purpose.
MOVED_VIEWS = {
    "providers": "the custom provider cards on it are faces now",
    # 8.20.3: the OpenClaw launcher paragraph on the Claude page described the
    # pre-fix write; it now names the two additive keys MCC writes beside its
    # block and the add-only default model. Prose only.
    "claude": "the OpenClaw launcher paragraph names MCC's additive keys",
}

#: Views that had ALREADY moved at this branch's base, measured -- not assumed.
#:
#: The frozen baseline was written from `32109387` (v8.9.0). Running this same
#: harness on a clean detached worktree at **`fea480c2` (v8.10.1, this branch's
#: base)**, with nothing from this PR applied, reports `guide`, `providers`,
#: `requests` and `web_search` as moved -- and two runs of the base tree give
#: the same digest, so it is the base that moved and not the machine. `guide`
#: and `web_search` are 8.10.0's own additions to the Guide and to the search
#: cards; `requests` is the #357 harness change to the READY waits;
#: `providers` is 8.10.0's face.
#:
#: This is the SAME measurement
#: `test_admin_cards_overlay_jsdom.py::PRE_EXISTING_MOVES` records, extended by
#: the two views 8.10.0 itself moved -- that suite's own `MOVED_VIEWS` names
#: all three of `providers`, `web_search` and `guide`, so its two sets together
#: are exactly this one. Recording them here rather than re-recording the
#: frozen fixture (this PR may not touch it) keeps the comparison honest: every
#: one of these is still measured and still compared, it is just attributed to
#: the commit that moved it.
PRE_EXISTING_MOVES = {
    "requests": "moved at the base commit c434e826; see the #357 harness change "
    "to the READY waits",
    "web_search": "moved by 8.10.0's web-search face, before this branch",
    "guide": "moved by 8.10.0's Guide rewrite for the provider panel",
}


def test_no_view_outside_the_intended_ones_moved(overlay) -> None:
    """A CEILING: nothing outside the declared sets may have moved.

    Never an equality against `MOVED_VIEWS` -- a declared view that
    legitimately converges back would fail that, which is the shape WORKING
    NOTES IX.1 records as wrong.
    """
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert "BASE tree 32109387" in baseline["written_by"]

    moved = {
        view
        for view, digest in baseline["views"].items()
        if overlay["views"].get(view) != digest
    }
    unexpected = sorted(moved - set(MOVED_VIEWS) - set(PRE_EXISTING_MOVES))
    assert not unexpected, (
        f"views moved that this PR does not touch: {unexpected}. Either the "
        "change reached a page it should not, or the change is intended and "
        f"MOVED_VIEWS needs it: {sorted(moved)}"
    )
    # Anti-vacuity, twice: something moved, and every baseline view was
    # compared against something this run actually rendered.
    assert moved, "nothing moved at all, which means the cards did not change"
    assert set(MOVED_VIEWS) <= set(baseline["views"])
    missing = [view for view in baseline["views"] if not overlay["views"].get(view)]
    assert missing == []


def test_the_pre_existing_exemption_is_still_one_measured_view() -> None:
    """Widening it needs a new measurement, not an edit to make a red test green.

    The measurement above is `fea480c2` run with nothing from this PR applied.
    Widening this set means running that tree again and reading the real
    moved-set off the diff -- which is what produced the three entries here.
    """
    assert set(PRE_EXISTING_MOVES) == {"requests", "web_search", "guide"}
    # And exactly the views this release moves: this PR's own face, and
    # 8.20.3's OpenClaw paragraph on the Claude page (prose only). The
    # pre-existing set above is untouched by either.
    assert set(MOVED_VIEWS) == {"providers", "claude"}


# --------------------------------------------------------------------------
# 8. the mutation controls (§4.6)
# --------------------------------------------------------------------------


def _mutant(tmp_path: Path, needle: str, replacement: str) -> dict:
    """Run the completeness harness against a copy with one line changed."""
    static_dir = tmp_path / "admin_static"
    static_dir.mkdir()
    for name in ("index.html", "admin.js", "designs.js", "usage_layouts.js"):
        (static_dir / name).write_text(
            (STATIC_DIR / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    source = (static_dir / "admin.js").read_text(encoding="utf-8")
    assert source.count(needle) == 1, (
        f"the line this control removes is not in admin.js exactly once: {needle!r}"
    )
    (static_dir / "admin.js").write_text(
        source.replace(needle, replacement), encoding="utf-8"
    )
    return _run_node(CARDS_HARNESS, static_dir)


#: The action row's two lines: build it, then put it at the END of the body.
#: Delete is the third of the three buttons, so its position IS this line.
ACTION_ROW = """  actions.append(editButton, toggleButton, deleteButton);
  body.appendChild(actions);"""

#: The control dropped: the row is built and placed, Delete is not in it.
DELETE_DROPPED = """  actions.append(editButton, toggleButton);
  body.appendChild(actions);"""

#: The action row hoisted to the TOP of the body -- the regression a "tidy up
#: the panel" edit would produce, where `body.appendChild(actions)` is replaced
#: by an insert at the front, putting Delete at the panel's primary position.
#:
#: Moving Delete WITHIN the row is not the counter-example: that leaves it
#: nineteen buttons below the top, which is where it belongs. The property
#: under test is distance from the primary position, so the mutant has to
#: break that property. (`insertBefore` alone would not move it either -- the
#: stock `appendChild` after it moves the same node straight back to the end,
#: which is why this replacement carries both lines.)
ACTION_ROW_HOISTED = """  actions.append(editButton, toggleButton, deleteButton);
  body.insertBefore(actions, body.firstChild);"""


def test_a_mutant_without_the_delete_control_fails_the_completeness_test(
    tmp_path,
) -> None:
    """Drop Delete from the body -> the panel is incomplete, loudly."""
    mutant = _mutant(tmp_path, ACTION_ROW, DELETE_DROPPED)
    found = {control["id"]: control for control in mutant["panel"]["controls"]}

    assert found["delete"]["found"] == []
    assert "delete" in mutant["panel"]["missing"]
    # The other eight are untouched: this is one control, not a broken body.
    assert set(mutant["panel"]["missing"]) == {"delete"}


def test_a_mutant_with_the_action_row_at_the_top_fails_the_position_test(
    tmp_path,
) -> None:
    """Hoist the action row -> Delete is the third control a reader can reach."""
    mutant = _mutant(tmp_path, ACTION_ROW, ACTION_ROW_HOISTED)
    danger = mutant["danger"]

    # The row is before the key rail and the dialect block, so the destructive
    # trio lands in the first three positions of the body.
    assert danger["deleteIndex"] == danger["firstIndex"] + 2
    assert danger["deleteIsLast"] is False
    assert danger["deleteDistance"] < SAFE_DISTANCE
    assert danger["toggleDistance"] < SAFE_DISTANCE
    assert danger["safe"] is False
