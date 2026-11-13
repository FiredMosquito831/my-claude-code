"""CARDS-OVERLAY — a compact provider card and a dialog that holds its form.

USER 2026-11-02, said again on 2026-11-03 because it had not been built:

    "instead of extending the cards we could just make a tab appear like it
    happens when we open a request to view its data ... and open that small
    tab/card when we press anywhere on the card of the provider (but keep the
    refresh model buttons and configure button and configured status and detect
    models count)"

``specs/UI-QUALITY-RULE.md`` §D is the contract. Everything here runs the real
``index.html``, ``admin.js`` and ``admin.css`` in jsdom through
``admin_jsdom_harness.mjs``; nothing is mocked past the fetch stub.

What is asserted, and why each one exists:

1. **THE FIVE CONTROLS** — a collapsed card carries the model count, Refresh
   models and Configure, and nothing else. Each is pressed and asked two
   questions: did your own job happen, and did the dialog stay shut. A control
   that both acts and opens the panel would be indistinguishable from a card
   press, which is the defect this whole change is one mistake away from.

2. **THE DIALOG'S CONTENT** — field by field, against the payload the card was
   drawn from. Moving a form into a panel must not lose a setting, and the only
   honest proof of "nothing was dropped" is comparing every key, label, help
   sentence, the advanced fold and the key pool.

3. **THE CONTENT IS NOT ALSO ON THE PAGE** — ``changedValues()`` walks the whole
   document for ``[data-key]`` and Apply posts what it finds, so a form left in
   the card behind ``display: none`` would be a second, stale copy of every
   setting that a save could pick up.

4. **KEYBOARD AND FOCUS** — the card is focusable and acts as a button, Enter
   and Space open, Escape closes, focus returns to the card that opened it, and
   Tab wraps inside the dialog rather than reaching the page behind it.

5. **THE PAGE BEHIND** — locked while the dialog is open and released exactly
   when it closes.

6. **A RE-RENDER WHILE IT IS OPEN** — ``renderSections()`` clears every settings
   container and ``refreshConfigState()`` re-reads the payload; a dialog left
   holding the previous payload is where "it saved the old value" comes from.

7. **THE WEB SEARCH CARDS** — the same face, with "Use provider" still working
   from the card, and its rotation policy / advanced group / key pool in the
   dialog.

8. **THE DOM BASELINE** — with a card collapsed, the only views whose bytes
   move are the two this PR touches, against a baseline written by the same
   scenario against the BASE tree ``32109387`` (v8.9.0).

9. **THE MODEL COUNT** (8.10.1, ``specs/PR-CARDS-FIX-1.md`` §4) — the count on
   the face follows the catalogue on every load and every refresh, reads
   differently for "answered none" and "never asked", and never appears on the
   status pill. Measured on its own page load by
   ``admin_jsdom_model_count.mjs``, because the defect was an ORDER: the cards
   are drawn at ``admin.js:822`` and the list arrives at ``:833``, which a
   scenario that renders a card directly cannot see.

10. **THE TWO MUTATION CONTROLS** (§4.6) — the hook at the end of
   ``setModelOptions`` and the ``disabled`` guard inside the repaint are each
   deleted from a copy of the shipped ``admin.js``, and the same measurements
   are required to change. A suite that passed with either removed would be
   testing that the page loads.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.api.test_admin_static_jsdom import STATIC_DIR, _run

BASELINE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "cards_overlay_dom_8_9_0.json"
)
CARDS_HARNESS = Path(__file__).with_name("admin_jsdom_cards_overlay.mjs")
MODEL_COUNT_HARNESS = Path(__file__).with_name("admin_jsdom_model_count.mjs")

#: CI must not pass a suite it never ran (``MCC_CI=1`` makes a missing node or
#: a missing jsdom a failure inside ``_run``).
ON_CI = os.environ.get("MCC_CI", "") not in ("", "0", "false")

pytestmark = pytest.mark.xdist_group("admin_static_jsdom")


@pytest.fixture(scope="module")
def rendered() -> dict:
    return _run()


@pytest.fixture(scope="module")
def model_count() -> dict:
    """CARDS-FIX-1's own run, on its own page load.

    The defect was an ORDER (``renderSections`` at ``admin.js:822`` paints the
    cards, ``hydrateModelOptions`` at ``:833`` fills the list), so it can only
    be measured on a page that is loaded the way a browser loads one. It is a
    separate, much smaller harness than the UX pass, which also lets the
    mutation controls run it three times without paying for a full UX pass
    each.
    """
    return _run_model_count(STATIC_DIR)


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


def _run_cards_harness(static_dir: Path) -> dict:
    return _run_node(CARDS_HARNESS, static_dir)


def _run_model_count(static_dir: Path) -> dict:
    return _run_node(MODEL_COUNT_HARNESS, static_dir)


@pytest.fixture(scope="module")
def cards_run() -> dict:
    return _run_cards_harness(STATIC_DIR)


# --------------------------------------------------------------------------
# 1. the card is a face, and carries exactly five things
# --------------------------------------------------------------------------


def test_a_collapsed_card_is_a_button(rendered) -> None:
    face = rendered["providerDialog"]["cardIsAFace"]

    assert face["role"] == "button"
    assert face["ariaHaspopup"] == "dialog"
    assert face["tabIndex"] == 0
    assert face["label"] == "Dialog Provider settings"


def test_a_collapsed_card_holds_the_five_things_and_nothing_else(rendered) -> None:
    """Name, status, model count, Refresh models, Configure.

    The face's ordered child list is the contract: a `provider-meta` line, a
    card body or a key manager appearing here again is the card growing back
    into the form this release removed.

    8.10.1: the third entry used to be ``"No models detected"``, and it was the
    only label the control ever had -- the card was built before the model list
    loaded and nothing repainted it. The face is now read twice. As drawn
    (``controlsAsDrawn``) it is the honest unknown state, because nothing has
    asked this provider yet; after its own Detect call answers two models
    (``controls``), it says what the provider said.
    """

    face = rendered["providerDialog"]["cardIsAFace"]

    assert face["children"] == ["provider-title", "pv-actions", "ghost-button"]
    assert face["controls"] == ["Configure", "Refresh models", "2 models"]
    assert face["controlsAsDrawn"] == [
        "Configure",
        "Refresh models",
        "Not detected yet",
    ]


def test_no_setting_control_is_left_on_the_face(rendered) -> None:
    """``changedValues()`` walks ``[data-key]`` over the whole document."""
    assert rendered["providerDialog"]["cardIsAFace"]["dataKeysOnTheFace"] == 0


def test_a_keyless_provider_keeps_its_own_summary_off_the_face(rendered) -> None:
    """7.78.0's sentence for "free models work with no key" is in the dialog.

    It used to be the card's `provider-meta` line. The face carries the status
    pill instead, which is the same fact in the one slot the card has left; the
    sentence itself moved next to the pool it describes.
    """
    assert rendered["providerFaces"]["freeReady"]["pill"] == "Free models ready"
    assert rendered["providerFaces"]["freeReady"]["onFace"] is False


# --------------------------------------------------------------------------
# 2. five controls, five proofs
# --------------------------------------------------------------------------


def test_configure_opens_the_dialog(rendered) -> None:
    """Configure is the one face control that IS the card's own gesture.

    Before this release it toggled `pv-open` in place; the panel it opens now
    is the same form, in the same place. Its own job -- getting the reader to
    the form -- is unchanged, so the proof is "it opened, for the right
    provider", not "it opened nothing".
    """
    configure = rendered["providerDialog"]["configure"]

    assert configure["wasClosed"] is True
    assert configure["openedTheDialog"] is True
    assert configure["labelled"] == "Configure"


def test_refresh_models_does_its_job_and_opens_nothing(rendered) -> None:
    refresh = rendered["providerDialog"]["refreshModels"]

    assert refresh["labelled"] == "Refresh models"
    # The busy label is the proof its own call really started.
    assert refresh["busyLabel"] == "Testing"
    assert refresh["openedTheDialog"] is False


def test_detect_models_does_its_job_and_opens_nothing(rendered) -> None:
    """The resting label was ``"No models detected"`` until 8.10.1.

    That assertion encoded the defect rather than the contract: the card was
    painted before the model list existed and the label was never repainted, so
    the test pinned the empty state as correct. The control is now read after
    its own call has answered, and that number must be what the provider said
    it has -- two, from the route stub -- not what the card was built with.
    """
    detect = rendered["providerDialog"]["detectModels"]

    assert detect["busyLabel"] == "Detecting"
    assert detect["openedTheDialog"] is False
    assert detect["afterTheCall"] == "2 models"
    # And the face it lives on agrees with it in the same run.
    assert rendered["providerDialog"]["cardIsAFace"]["controls"][-1] == "2 models"


def test_the_configured_status_is_on_the_face_and_not_a_dead_spot(rendered) -> None:
    """The pill is not a control, so pressing it is a press on the card.

    That is the point: it reads the provider's state (``status-pill ok`` for a
    configured provider) and it opens the dialog, rather than doing nothing
    where a reader expects the card to respond.

    8.10.1: the pill is read as the card was drawn, before any control on it is
    pressed. It is a status, so pressing Refresh models must not turn it into a
    number -- the second reading is the same status, after two calls answered.
    """
    pill = rendered["providerDialog"]["statusPill"]

    assert pill["text"] == "Configured"
    assert pill["className"] == "status-pill ok"
    assert pill["openedTheDialog"] is True
    assert pill["textAfterTheCalls"] == "Reachable"
    assert pill["wasANumberAsDrawn"] is False
    assert pill["isANumber"] is False


def test_pressing_the_card_opens_the_dialog(rendered) -> None:
    press = rendered["providerDialog"]["cardPress"]

    assert press["openedTheDialog"] is True
    assert press["title"] == "Dialog Provider"
    assert press["role"] == "dialog"
    assert press["ariaModal"] == "true"


# --------------------------------------------------------------------------
# 3. what the dialog holds, field by field
# --------------------------------------------------------------------------


def test_the_dialog_holds_every_field_the_card_used_to(rendered) -> None:
    """Three settings, three labels, three help sentences, one fold, one pool.

    This is the completeness proof: every one of these was on the card body
    before 8.10.0, and every one is in the dialog after it.
    """
    press = rendered["providerDialog"]["cardPress"]

    assert press["fieldKeys"] == [
        "DIALOG_API_KEY",
        "DIALOG_API_KEY_ROTATION",
        "DIALOG_API_KEY_PROXY",
    ]
    assert press["labels"] == ["Dialog API key", "Dialog proxy"]
    assert press["descriptions"] == [
        "One key per line, comma separated.",
        "A proxy this provider leaves through, host:port.",
    ]
    assert press["advancedToggle"] is True
    assert press["keyManagers"] == 1
    assert press["rotationSelects"] == 1


def test_the_dialog_is_not_inside_a_settings_container(rendered) -> None:
    """``renderSections()`` empties every container; the dialog must survive it."""
    assert rendered["providerDialog"]["dialogIsNotInsideASettingsContainer"] is False


# --------------------------------------------------------------------------
# 4. keyboard and focus
# --------------------------------------------------------------------------


def test_escape_closes_and_gives_focus_back_to_the_card(rendered) -> None:
    escape = rendered["providerDialog"]["escape"]

    assert escape["closed"] is True
    assert escape["focusReturned"] is True
    assert escape["bodyCleared"] is True


def test_enter_and_space_both_open_the_dialog(rendered) -> None:
    assert rendered["providerDialog"]["enterOpened"] is True
    assert rendered["providerDialog"]["spaceOpened"] is True


def test_tab_wraps_inside_the_dialog(rendered) -> None:
    trap = rendered["providerDialog"]["trap"]

    assert trap["count"] > 2
    assert trap["wrappedForward"] is True
    assert trap["wrappedBackward"] is True


# --------------------------------------------------------------------------
# 5. the page behind
# --------------------------------------------------------------------------


def test_the_page_behind_is_locked_only_while_the_dialog_is_open(rendered) -> None:
    lock = rendered["providerDialog"]["scrollLock"]

    assert lock["duringOverflow"] == "hidden"
    assert lock["afterOverflow"] == ""
    assert lock["afterPadding"] == ""


# --------------------------------------------------------------------------
# 6. a re-render while the dialog is open
# --------------------------------------------------------------------------


def test_a_re_render_keeps_the_dialog_open_and_correct(rendered) -> None:
    rerender = rendered["providerDialogReRender"]

    assert rerender["controlFound"] is True
    assert rerender["dirtyBefore"] == "1 unsaved change"
    assert rerender["stillOpen"] is True
    assert rerender["titleKept"] == "Re-render Provider"
    assert rerender["typedSurvived"] == "typed-but-not-applied"
    assert rerender["dirtyAfter"] == "1 unsaved change"
    assert rerender["closed"] is True


def test_a_re_render_leaves_exactly_one_control_per_key(rendered) -> None:
    """A rebuilt dialog holding duplicate ``[data-key]`` controls would post
    whichever one Apply happened to walk to."""
    assert rendered["providerDialogReRender"]["rebuiltControls"] == 1
    assert rendered["providerDialogReRender"]["duplicateControls"] == 1


# --------------------------------------------------------------------------
# 7. the Web Search cards
# --------------------------------------------------------------------------


def test_a_web_search_card_is_a_face_too(rendered) -> None:
    cards = rendered["webSearchFaces"]["cards"]

    assert cards, "the web-search grid did not render"
    for card in cards:
        assert card["role"] == "button"
        assert card["tabIndex"] == 0
        assert card["dataKeys"] == 0
        assert card["hasAdvancedGroup"] is False
        assert card["hasKeyPool"] is False


def test_use_provider_works_from_the_collapsed_card(rendered) -> None:
    use = rendered["webSearchFaces"]["useProvider"]

    assert use is not None, "no web-search card offered Use provider"
    assert use["wasClosedBefore"] is True
    assert use["openedTheDialog"] is False
    assert use["selection"] == use["providerId"]


def test_a_web_search_card_opens_its_own_dialog(rendered) -> None:
    press = rendered["webSearchFaces"]["cardPress"]

    assert press is not None
    assert press["openedTheDialog"] is True


def test_the_web_search_dialog_holds_the_rotation_the_options_and_the_pool(
    rendered,
) -> None:
    dialog = rendered["webSearchFaces"]["dialog"]

    assert dialog is not None
    assert dialog["rotation"] == 2  # the `.field` wrapper and its select
    assert dialog["advancedGroups"] == 1
    assert dialog["advancedFields"] == 1
    # 7.122.3: the group opens by default, because a collapsed-by-default group
    # is the same "the setting does not exist" problem in a different shape.
    assert dialog["advancedOpen"] == [True]
    assert dialog["manageKeys"] is True


# --------------------------------------------------------------------------
# 8. the DOM baseline: only the two intended views moved
# --------------------------------------------------------------------------

#: The views whose bytes this PR changes on purpose, and why.
MOVED_VIEWS = {
    "providers": "the provider cards on it are faces now",
    "web_search": "the web-search cards on it are faces now",
    # The Guide is HTML in index.html, and its "Add a provider" step now
    # describes the panel a card opens instead of a card that expands. USER
    # 2026-11-02's brief asks for exactly this note ("a provider's details open
    # in a panel"), so the page it is written on moves with it.
    "guide": "the Guide's provider walkthrough now describes the panel",
    # 8.20.3: the OpenClaw launcher paragraph on the Claude page described the
    # pre-fix write (one key beside the block, the default model replaced and
    # recorded). It now names the two additive keys and the add-only default
    # model. Prose only: no control, id or launcher row moved.
    "claude": "the OpenClaw launcher paragraph names MCC's additive keys",
}

#: Views that had ALREADY moved at this branch's base, measured -- not assumed.
#:
#: The frozen baseline was written from `32109387` (v8.9.0), two commits behind
#: `c434e826`. Running this same harness on a clean detached worktree at
#: `c434e826`, with nothing from this PR applied, reports `requests` as moved
#: too -- and two runs of the base tree give the same digest, so it is the base
#: that moved and not the machine. #357's own subject is the harness's READY
#: waits, which is exactly how a view's markup changes without the view being
#: edited. Recording that here rather than re-recording the baseline (this PR
#: may not touch a frozen fixture) keeps the comparison honest: `requests` is
#: still measured, and it is still compared -- it is just attributed to the
#: commit that moved it.
PRE_EXISTING_MOVES = {
    "requests": "moved at the base commit c434e826, before this branch; see the "
    "#357 harness change to the READY waits",
}


def test_no_view_outside_the_intended_ones_moved(cards_run) -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert "BASE tree 32109387" in baseline["written_by"]

    moved = {
        view
        for view, digest in baseline["views"].items()
        if cards_run["views"].get(view) != digest
    }
    unexpected = sorted(moved - set(MOVED_VIEWS) - set(PRE_EXISTING_MOVES))
    assert not unexpected, (
        f"views moved that this PR does not touch: {unexpected}. Either the "
        "change reached a page it should not, or the change is intended and "
        f"MOVED_VIEWS needs it: {sorted(moved)}"
    )
    # And every view this PR DOES touch has a baseline entry, so the check
    # above is comparing something rather than silently agreeing with itself.
    assert set(MOVED_VIEWS) <= set(baseline["views"])
    assert moved, "nothing moved at all, which means the cards did not change"


def test_the_pre_existing_exemption_is_one_measured_view() -> None:
    """The exemption above must stay a fact about the base, not a loophole.

    It is one view, and it is one view because that is what the base tree was
    measured to move. Widening it needs a new measurement against `fork/main`,
    recorded in the comment above it -- not an edit here to make a red test go
    green.
    """
    assert set(PRE_EXISTING_MOVES) == {"requests"}


def test_the_baseline_comparison_is_not_vacuous(cards_run) -> None:
    """Every view the baseline knows is still rendered, or nothing was compared."""
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))

    missing = [view for view in baseline["views"] if not cards_run["views"].get(view)]
    assert missing == []
    assert len(cards_run["views"]) >= 16


def test_the_base_tree_had_no_dialog(cards_run) -> None:
    """The baseline records the absence it was written from."""
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))

    assert baseline["overlay"]["present"] is False
    assert cards_run["overlay"] == {
        "present": True,
        "role": "dialog",
        "ariaModal": "true",
        "closeable": True,
    }


def test_the_cards_themselves_are_what_moved(cards_run) -> None:
    """The baseline's face is the form; this branch's is the five things."""
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))["cards"]

    assert baseline["provider"]["children"] == [
        "provider-title",
        "provider-meta",
        "pv-actions",
        "pv-card-body",
    ]
    assert baseline["provider"]["dataKeys"] == 5
    assert baseline["provider"]["tabIndex"] == -1

    assert cards_run["cards"]["provider"]["children"] == [
        "provider-title",
        "pv-actions",
        "ghost-button",
    ]
    assert cards_run["cards"]["provider"]["dataKeys"] == 0
    assert cards_run["cards"]["provider"]["role"] == "button"
    assert cards_run["cards"]["provider"]["tabIndex"] == 0

    assert baseline["webSearch"]["dataKeys"] == 2
    assert cards_run["cards"]["webSearch"]["dataKeys"] == 0


def test_the_harness_runs_clean(cards_run) -> None:
    assert cards_run["fatal"] is None
    assert cards_run["scriptErrors"] == []
    assert cards_run["consoleErrors"] == []


# --------------------------------------------------------------------------
# 9. CARDS-FIX-1 — the model count on the face (USER 2026-11-04)
# --------------------------------------------------------------------------
#
# "The card always shows no models detected even after models are detected nad
# it shows with green in the corner (we want to permanently show how many
# models are detected and refresh when the refreshes happen)."
#
# Two independent defects, both of which had to go. The cards were painted
# eleven lines before the model list loaded, and nothing repainted them
# afterwards -- while the pill was handed the count on every refresh, which is
# why the green corner and the label beneath it told different stories.
#
# The proofs are the spec's §4 in its order. They are measured on
# `admin_jsdom_model_count.mjs`, which loads the page from its own two routes:
# that load order IS the first defect, so a scenario that renders a card
# directly cannot see it.


def test_a_counted_provider_shows_its_count_on_the_page_load(
    model_count: dict,
) -> None:
    """§4.1, first half -- a card drawn before the load must catch up.

    This is read after the page's own `load()`, i.e. after `renderSections`
    drew the card from an empty list and `hydrateModelOptions` then filled it.
    On 8.10.0 every one of these read "No models detected".
    """
    load = model_count["afterThePageLoads"]

    assert load["counted"] == "3 models"
    assert len(model_count["everyCardMatchesTheList"]) == 3


def test_a_refresh_changes_the_count(model_count: dict) -> None:
    """§4.1, second half -- "refresh when the refreshes happen".

    Non-empty is not the property. The number before and the number after are
    two different numbers, in both directions, from two loads of the path the
    page's own Refresh models button takes.
    """
    refresh = model_count["refresh"]

    assert refresh == {
        "before": "3 models",
        "afterFewer": "1 model",
        "afterMore": "4 models",
    }


def test_zero_and_never_detected_are_different_sentences(model_count: dict) -> None:
    """§4.2 -- three states, three strings, and no two of them alike.

    "No models detected" is a claim about the provider: it answered and offered
    none. A provider nobody has asked has an UNKNOWN list, which is a different
    fact (IV.16) and used to be rendered as the strong one.
    """
    states = model_count["threeStates"]

    assert states == {
        "counted": "3 models",
        "zero": "No models detected",
        "neverDetected": "Not detected yet",
    }
    assert len(set(states.values())) == 3
    # The same three come out of the labelling function itself, so this is a
    # property of the thing that decides the words, not of one paint.
    assert model_count["labels"] == states


def test_the_pill_is_a_status_and_never_a_count(model_count: dict) -> None:
    """§4.3 -- USER Q1: the pill says Reachable / Configured, never a number."""
    after = model_count["afterRefresh"]

    assert after["pill"] == "Reachable"
    assert after["pillClass"] == "status-pill ok"
    # And the count the same answer produced is on the control, from one tick.
    assert after["detect"] == "4 models"
    for tick in model_count["ticks"]:
        assert tick["pillIsANumber"] is False, tick


def test_the_pill_and_the_count_agree_after_any_loader(model_count: dict) -> None:
    """§4.4 -- both surfaces, same tick, after a load, a refresh and a test.

    The disagreement IS the bug: two writers of one fact. So the property is
    that each of them READS the list -- the card's label equals what the shared
    list computes -- not that they were both assigned a value once.
    """
    ticks = model_count["ticks"]

    assert [tick["loader"] for tick in ticks] == [
        "loadModelOptions: 2",
        "loadModelOptions(refresh): 6",
        "testProvider on a different card",
    ]
    for tick in ticks:
        assert tick["label"] == tick["computed"], tick
        assert tick["pillIsANumber"] is False, tick
    # The count really did move, so the agreement is not three readings of an
    # unchanged value.
    assert [tick["label"] for tick in ticks][:2] == ["2 models", "6 models"]


def test_the_busy_state_is_never_stomped_by_a_load(model_count: dict) -> None:
    """§4.5 -- a poll tick landing mid-Detect must not rewrite the button.

    The control disables itself and reads "Detecting" for the length of its own
    call. Without the guard, `setModelOptions` would put a count there and a
    reader could no longer tell a running call from a finished one.
    """
    busy = model_count["busy"]

    assert busy["whileBusy"] == {"text": "Detecting", "disabled": True}
    # The load really did happen while it was busy -- and the button did not move.
    assert busy["duringALoad"] == "Detecting"
    assert busy["afterTheCall"] == {"text": "1 model", "disabled": False}


def test_every_card_shows_what_the_shared_list_computes(model_count: dict) -> None:
    """The property that makes the number mean anything.

    For every card on the page the visible label is what the one list derives
    for that provider. Two displays of one fact stay equal only while both are
    reads; this fails the moment either becomes a second owner.
    """
    rows = model_count["everyCardMatchesTheList"]

    for row in rows:
        assert row["shown"] == row["computed"], row
    assert [row["refs"] for row in rows] == [3, 0, 1]


def test_the_model_count_harness_runs_clean(model_count: dict) -> None:
    assert model_count["fatal"] is None
    assert model_count["scriptErrors"] == []
    assert model_count["consoleErrors"] == []


# --------------------------------------------------------------------------
# 10. the mutation controls (§4.6)
# --------------------------------------------------------------------------
#
# This fix is two lines of behaviour: the hook at the end of `setModelOptions`
# and the `disabled` guard inside the repaint. A suite that passed with either
# one removed would be testing that the page loads, not that the fix exists.
# So each line is deleted from a COPY of the shipped `admin.js` -- the mutant
# differs from the product by that line and nothing else -- the same harness is
# run against it, and the same measurements are required to CHANGE. That is the
# control: the assertion that saw the difference is named in each test.

HOOK = "  repaintProviderModelCounts();\n"
GUARD = "    if (!detect || detect.disabled) return;\n"


def _mutant_model_count(tmp_path: Path, needle: str, replacement: str) -> dict:
    """Run the model-count harness against a copy with one line changed."""
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
    return _run_model_count(static_dir)


def test_a_mutant_without_the_setModelOptions_hook_never_shows_the_count(
    tmp_path,
) -> None:
    """Remove the hook -> the count never leaves the label the card was built with.

    On the mutant, `test_a_counted_provider_shows_its_count_on_the_page_load`
    is false: the card read after the page's own load still says what an empty
    list said. This is the release's own bug, restored.
    """
    mutant = _mutant_model_count(tmp_path, HOOK, "")

    assert mutant["afterThePageLoads"]["counted"] != "3 models"
    assert mutant["refresh"]["afterMore"] != "4 models"


def test_a_mutant_without_the_disabled_guard_stomps_the_busy_state(tmp_path) -> None:
    """Remove the guard -> a load mid-Detect replaces "Detecting" with a number.

    On the mutant, `test_the_busy_state_is_never_stomped_by_a_load` is false.
    """
    mutant = _mutant_model_count(tmp_path, GUARD, "")

    assert mutant["busy"]["whileBusy"]["text"] == "Detecting"
    assert mutant["busy"]["duringALoad"] != "Detecting"
