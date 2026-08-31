"""What the Model Config route rails must keep, in the shipped `admin.js`.

Mirrors `test_admin_models_view.py` for the neighbouring page. These are not a
substitute for the jsdom suite, which drives the gestures; they pin the shape
the gestures are built out of, so a refactor that quietly drops the keyboard
alternative or moves the selection into the DOM fails a check rather than a
user's hands.
"""

import re
from pathlib import Path

STATIC_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "my_claude_code"
    / "api"
    / "admin_static"
)
ADMIN_JS = (STATIC_DIR / "admin.js").read_text(encoding="utf-8")
ADMIN_CSS = (STATIC_DIR / "admin.css").read_text(encoding="utf-8")
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _slice(start: str, end: str) -> str:
    first = ADMIN_JS.index(start)
    return ADMIN_JS[first : ADMIN_JS.index(end, first)]


def test_the_drag_is_pointer_based_and_stays_that_way() -> None:
    """HTML5 drag-and-drop is untestable in the harness this repo ships.

    jsdom has no PointerEvent and no usable DataTransfer, so a `dragstart`
    implementation could be written but never covered. It is also a second
    drag idiom on a page adjacent to one that already has a pointer drag.
    """
    for vector in ("dragstart", "dragover", "dataTransfer", "draggable ="):
        assert vector not in ADMIN_JS, f"admin.js uses HTML5 DnD via {vector}"


def test_every_route_gesture_funnels_through_one_mutator() -> None:
    """Dedupe, the primary swap and the undo snapshot happen in one place."""
    for name in (
        "function routeIdFor(",
        "function setRouteSelection(",
        "function clearRouteSelection(",
        "function routeRangeIds(",
        "function onRouteSelectClick(",
        "function onRouteSelectKeydown(",
        "function startRouteDrag(",
        "function continueRouteDrag(",
        "function endRouteDrag(",
        "function applyRouteDrop(",
        "function announceRoute(",
        "function undoLastRouteDrag(",
    ):
        assert name in ADMIN_JS, f"admin.js no longer declares {name}"
    # One caller writes chains; the pointer path and the primary path both
    # reach it rather than each editing rows themselves.
    assert ADMIN_JS.count("applyRouteDrop(") >= 2


def test_the_route_drag_reuses_the_chain_editor_rather_than_reimplementing_it() -> None:
    """The ordering rules already live in ModelChainEditor."""
    drop = _slice("function applyRouteDrop(", "function undoLastRouteDrag(")
    assert "swapPrimaryAndFirst()" in drop
    assert "setPrimaryValue(" in drop
    assert "canDemotePrimary()" in drop


def test_route_range_selection_is_reachable_without_a_pointer() -> None:
    """WCAG 2.2 wants a keyboard alternative to any author-controlled drag."""
    keys = _slice("function onRouteSelectKeydown(", "function routeGripFor(")
    assert 'event.key === " " && event.shiftKey' in keys
    assert '"ArrowDown"' in keys and '"ArrowUp"' in keys


def test_the_arrow_buttons_are_not_replaced_by_the_drag() -> None:
    """They are the single-pointer alternative and stay wired to move()."""
    assert 'upButton.addEventListener("click", () => this.move(row, -1));' in ADMIN_JS
    assert 'downButton.addEventListener("click", () => this.move(row, 1));' in ADMIN_JS


def test_the_models_page_drag_is_untouched() -> None:
    """Two drags, two pages; refactoring one while adding the other is not it."""
    assert "function startModelsDrag(" in ADMIN_JS
    assert "function continueModelsDrag(" in ADMIN_JS


def test_the_selection_is_held_in_state_not_in_the_dom() -> None:
    """A rail rebuilt by a drop must not silently forget what was picked."""
    assert "routeSelection: new Set()," in ADMIN_JS
    sync = _slice("function syncRouteSelectionUi(", "function onRouteSelectClick(")
    assert "state.routeSelection.has(" in sync


def test_the_rail_registry_exists_so_a_drop_can_find_its_destination() -> None:
    """Every editor used to be reachable only through the closure that built it."""
    assert "routeRails: new Map()," in ADMIN_JS
    assert "state.routeRails.set(chainField.key, editor);" in ADMIN_JS


def test_the_touch_guard_is_scoped_to_the_grip() -> None:
    """A finger scrolling over a card must still scroll the card."""
    start = _slice("function startRouteDrag(", "function continueRouteDrag(")
    assert 'event.pointerType === "touch"' in start
    assert "route-drag-grip" in start
    # The only `touch-action` in the stylesheet, and it is on the grip.
    rules = re.findall(r"([^{}]+)\{[^{}]*touch-action:\s*none", ADMIN_CSS)
    selectors = [rule.strip().splitlines()[-1].strip() for rule in rules]
    assert selectors == [".route-drag-grip"]


def test_undo_is_one_level_and_ignores_a_text_field() -> None:
    """Native text undo belongs to whoever is typing in a combobox."""
    assert "routeUndo: null," in ADMIN_JS
    handler = _slice("function initRouteRails(", "initRouteRails();")
    assert 'event.key.toLowerCase() === "z"' in handler
    assert "if (typing) return;" in handler


def test_the_status_panel_is_declared_statically_and_toggled_by_hidden() -> None:
    """`byId` finds nothing for an element that was never appended."""
    assert 'id="routeStatus"' in INDEX_HTML
    assert 'role="status"' in INDEX_HTML
    announce = _slice("function announceRoute(", "function routeNodeControls(")
    assert "target.hidden = true;" in announce
    assert "style.display" not in announce


def test_the_pause_labels_are_quoted_literals_the_docs_can_name() -> None:
    """A label built by interpolation cannot be pinned against the docs."""
    assert '"Pause"' in ADMIN_JS
    assert '"Resume"' in ADMIN_JS
    assert '"Add fallback"' in ADMIN_JS


def test_the_pause_write_goes_through_the_locked_config_path() -> None:
    """Never read the list here and POST a full replacement: that is the #223 bug."""
    toggle = _slice("async function toggleRoutePause(", "function announceRoute(")
    assert '"/admin/api/config/route-pause"' in toggle
    assert '"model_ref": ' not in toggle  # the body is built, not hand-rendered
    assert "model_ref: ref" in toggle
    # The button is disabled in flight rather than reverting optimistically.
    assert "button.disabled = true;" in toggle


def test_pausing_does_not_refetch_the_whole_payload() -> None:
    """The per-model visibility tick re-reads 3.4 MB after every click."""
    toggle = _slice("async function toggleRoutePause(", "function announceRoute(")
    assert '/admin/api/config"' not in toggle
    assert "load()" not in toggle
    assert "state.fields.get(result.paused_key)" in toggle


def test_the_pause_route_appears_on_both_sides_of_the_wire() -> None:
    """A path the client calls and the server does not serve is a 404 in waiting."""
    routes = (STATIC_DIR.parents[0] / "admin_routes.py").read_text(encoding="utf-8")
    assert "/admin/api/config/route-pause" in routes
    assert "/admin/api/config/route-pause" in ADMIN_JS


def test_the_deadline_calculator_excludes_a_paused_model() -> None:
    """Counting a model the router will not try understates every share."""
    chain_length = _slice("function chainLength(", "/** Mirror of `_attempt_deadline`")
    assert "routePausedRefs(modelKey)" in chain_length
    assert "!paused.has(" in chain_length


def test_the_pause_lists_are_claimed_by_the_routing_view() -> None:
    """Unclaimed manifest fields render as bare text boxes under the grid."""
    assert "...ROUTE_PAUSE_KEY.values()," in ADMIN_JS
