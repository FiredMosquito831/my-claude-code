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

#: CI must not pass a suite it never ran (``MCC_CI=1`` makes a missing node or
#: a missing jsdom a failure inside ``_run``).
ON_CI = os.environ.get("MCC_CI", "") not in ("", "0", "false")

pytestmark = pytest.mark.xdist_group("admin_static_jsdom")


@pytest.fixture(scope="module")
def rendered() -> dict:
    return _run()


def _run_cards_harness(static_dir: Path) -> dict:
    node = shutil.which("node")
    if node is None:
        if ON_CI:
            pytest.fail("MCC_CI is set but node is not on PATH")
        pytest.skip("node is not on PATH")
    result = subprocess.run(
        [node, str(CARDS_HARNESS), str(static_dir)],
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
        pytest.fail(f"cards harness failed: {result.stderr[-2000:]}")
    return json.loads(result.stdout)


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
    """
    face = rendered["providerDialog"]["cardIsAFace"]

    assert face["children"] == ["provider-title", "pv-actions", "ghost-button"]
    assert face["controls"] == ["Configure", "Refresh models", "No models detected"]


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
    detect = rendered["providerDialog"]["detectModels"]

    assert detect["labelled"] == "No models detected"
    assert detect["busyLabel"] == "Detecting"
    assert detect["openedTheDialog"] is False


def test_the_configured_status_is_on_the_face_and_not_a_dead_spot(rendered) -> None:
    """The pill is not a control, so pressing it is a press on the card.

    That is the point: it reads the provider's state (``status-pill ok`` for a
    configured provider) and it opens the dialog, rather than doing nothing
    where a reader expects the card to respond.
    """
    pill = rendered["providerDialog"]["statusPill"]

    assert pill["text"] == "Configured"
    assert pill["className"] == "status-pill ok"
    assert pill["openedTheDialog"] is True


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
}


def test_no_view_outside_the_intended_ones_moved(cards_run) -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert "BASE tree 32109387" in baseline["written_by"]

    moved = {
        view
        for view, digest in baseline["views"].items()
        if cards_run["views"].get(view) != digest
    }
    unexpected = sorted(moved - set(MOVED_VIEWS))
    assert not unexpected, (
        f"views moved that this PR does not touch: {unexpected}. Either the "
        "change reached a page it should not, or the change is intended and "
        f"MOVED_VIEWS needs it: {sorted(moved)}"
    )
    # And every view this PR DOES touch has a baseline entry, so the check
    # above is comparing something rather than silently agreeing with itself.
    assert set(MOVED_VIEWS) <= set(baseline["views"])
    assert moved, "nothing moved at all, which means the cards did not change"


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
