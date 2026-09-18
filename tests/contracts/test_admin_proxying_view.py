"""What bulk management of the fetched addresses must keep, in the shipped files.

Mirrors `test_admin_models_view.py` and `test_admin_routing_view.py` for the
Proxying page. These do not replace the jsdom suite, which drives the gestures;
they pin the shape the gestures are built out of, so a refactor that quietly
reintroduces a second write path, drops the keyboard alternative, or moves the
selection into the DOM fails a check rather than a user's hands.

The one that matters most is `test_there_is_exactly_one_write_path_for_a
_candidate`. 6.24.0 exists because the Models page kept a per-row write beside
its bulk one, the two drifted, and the per-row one silently skipped the
counters. A single-address action here is the bulk action with one element.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = ROOT / "src" / "my_claude_code" / "api" / "admin_static"
ADMIN_JS = (STATIC_DIR / "admin.js").read_text(encoding="utf-8")
ADMIN_CSS = (STATIC_DIR / "admin.css").read_text(encoding="utf-8")
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
ROUTES_PY = (
    ROOT / "src" / "my_claude_code" / "api" / "admin_proxy_routes.py"
).read_text(encoding="utf-8")


def _slice(start: str, end: str) -> str:
    first = ADMIN_JS.index(start)
    return ADMIN_JS[first : ADMIN_JS.index(end, first)]


def test_there_is_exactly_one_write_path_for_a_candidate() -> None:
    """One route on the client, one route on the server, and no per-row twin."""

    assert "/admin/api/proxy-chains/candidates/add" not in ADMIN_JS
    assert "/admin/api/proxy-chains/candidates/add" not in ROUTES_PY
    # Exactly one place in the page posts a candidate write.
    assert ADMIN_JS.count("/admin/api/proxy-chains/candidates/bulk") == 1
    assert "async function runProxyCandidateBulk(" in ADMIN_JS


def test_a_single_address_press_goes_through_the_bulk_function() -> None:
    """The row's button and the bar's button reach the same function.

    Written as `proxies: [candidate.proxy]`: one element, not one route.
    """

    row = _slice("function proxyCandidateRow(candidate)", "/* ---------")
    assert "runProxyCandidateBulk({" in row
    assert "proxies: [candidate.proxy]" in row
    # And the card's per-entry Remove is the bulk remove with one element.
    assert "proxyRemoveEntries(provider, draft, [entry])" in ADMIN_JS


def test_the_selection_is_held_in_state_not_in_the_dom() -> None:
    """The page re-renders after every batch of a bulk add.

    A selection living in the checkboxes would be thrown away by its own
    progress, which is why `modelsState.selected` exists on the other page.
    """

    assert "selected: new Set()" in ADMIN_JS
    assert "function proxySelectedIds()" in ADMIN_JS
    assert "proxyState.selected.has" in ADMIN_JS


def test_range_selection_is_reachable_without_a_pointer() -> None:
    """WCAG 2.2, and 6.7.0 records it as a requirement rather than a nicety."""

    keys = _slice("function onProxyCandidateKeydown(", "const PROXY_OUTCOME_WORDS")
    assert 'event.key === " " && event.shiftKey' in keys
    assert '"ArrowDown"' in keys and '"ArrowUp"' in keys
    assert "event.preventDefault()" in keys


def test_escape_clears_the_selection_only_when_no_modal_is_open() -> None:
    escape = _slice("function initProxyingSelection()", "initProxyingSelection();")
    assert "proxyModalIsOpen()" in escape
    assert "clearProxyCandidateSelection()" in escape
    modals = _slice("function proxyModalIsOpen()", "/* Escape anywhere")
    for modal in ("webSearchDetailModal", "exportModal", "reqDetailModal"):
        assert modal in modals


def test_the_outcome_is_reported_through_the_one_status_panel() -> None:
    """One `role=status` region with Undo, not a toast that vanishes.

    A partial result is four sentences long; three seconds of a toast is not a
    way to read it, and the Undo it offers has to be where it was read.
    """

    assert 'id="proxyingStatus" class="route-status" role="status"' in INDEX_HTML
    assert "function announceProxyBulk(" in ADMIN_JS
    assert "undo: token ? () => undoProxyCandidateBulk(token) : undefined" in ADMIN_JS


def test_the_bulk_write_is_batched_and_can_be_stopped() -> None:
    """Every address is a TLS handshake with a ten-second ceiling.

    One request for a long selection is a progress bar that cannot move and a
    gesture that cannot be stopped.
    """

    run = _slice("async function runProxyCandidateBulk(", "/* The one repaint.")
    assert "PROXY_CANDIDATE_BATCH" in run
    assert "proxyState.run.stop" in run
    # The undo token is carried forward, so Undo means "before I pressed Add".
    assert "undo_token: token" in run


def test_a_large_gesture_asks_for_a_second_press_rather_than_a_dialog() -> None:
    run = _slice("async function runProxyCandidateBulk(", "/* The one repaint.")
    assert "PROXY_CANDIDATE_CONFIRM_AT" in run
    assert "proxyState.confirming" in run
    assert re.search(r"PROXY_CANDIDATE_CONFIRM_AT = \d+", ADMIN_JS)


def test_only_a_provider_with_somewhere_to_test_against_is_offered() -> None:
    """An address is verified against the provider's own host.

    A provider with no https base URL has nothing to verify against, and the
    reason the per-row picker used to give has to survive somewhere sensible.
    """

    assert "function proxyCandidateProviders()" in ADMIN_JS
    assert "function proxyIneligibleProviders()" in ADMIN_JS
    assert "is not offered as a " in ADMIN_JS
    assert "has no https base " in ADMIN_JS


def test_every_candidate_write_route_re_reads_inside_the_writer_lock() -> None:
    """The read-modify-write race, closed on every write route.

    6.7.0 found parallel toggles were lossy on the Models page for exactly this
    reason: each write derived its replacement from a base read before the
    other committed.
    """

    for function in ("_commit_promotions", "_commit_discard", "_commit_feeds"):
        body = ROUTES_PY[ROUTES_PY.index(f"def {function}(") :]
        body = body[: body.index("\ndef ", 1)]
        assert "_CHAIN_WRITE_LOCK" in body, function
        assert "load_proxy_chains()" in body, function
    # And one save for a whole batch, not one per address.
    promotions = ROUTES_PY[ROUTES_PY.index("def _commit_promotions(") :]
    promotions = promotions[: promotions.index("\ndef ", 1)]
    assert promotions.count("save_proxy_chains(") == 1


def test_the_bulk_route_appears_on_both_sides_of_the_wire() -> None:
    assert '@router.post("/admin/api/proxy-chains/candidates/bulk")' in ROUTES_PY
    assert '@router.post("/admin/api/proxy-chains/candidates/undo")' in ROUTES_PY


def test_the_checks_of_a_bulk_add_are_bounded_rather_than_serial_or_unbounded() -> None:
    """Twelve ten-second timeouts in a row is two minutes of a spinner.

    Unbounded would be an outbound flood from an admin page. Since 7.22.2 the
    bound is the operator's own fetch number rather than the four this route
    used to hard-code -- "Add all working" on three hundred addresses is the
    same gesture as the sweep that found them -- and it is still a bound: the
    ceiling is the setting's own maximum and every check carries the per-address
    budget. The background checker keeps its serial default either way.
    """

    assert "concurrency=pace.value" in ROUTES_PY
    assert "max_concurrency=PROXY_FETCH_TEST_CONCURRENCY_MAX" in ROUTES_PY
    assert "budget=check_budget(" in ROUTES_PY
    checker = (
        ROOT / "src" / "my_claude_code" / "application" / "proxy_check.py"
    ).read_text(encoding="utf-8")
    assert "PROXY_CHECK_MAX_CONCURRENCY = " in checker
    assert "concurrency: int = 1" in checker
    assert "max_concurrency: int = PROXY_CHECK_MAX_CONCURRENCY" in checker
    assert "asyncio.Semaphore(" in checker


def test_the_selection_and_the_filters_are_persisted_best_effort() -> None:
    """Stored the way the dashboard stores its Analytics filters."""

    save = _slice("function saveProxyCandidateView()", "function renderProxyCandidates")
    assert "localStorage.setItem(" in save
    assert "localStorage.getItem(" in save
    # Never at the cost of the page: storage can be off, full, or unreadable.
    assert save.count("catch (_)") == 2


def test_every_new_candidate_class_has_a_rule() -> None:
    """The same rule `test_admin_asset_wiring` enforces, named here for the
    classes this feature introduced, so a rename fails loudly."""

    for name in (
        "proxy-candidate-bar",
        "proxy-candidate-controls",
        "proxy-candidate-select",
        "proxy-candidate-select-all",
        "proxy-candidate-progress",
        "proxy-candidate-outcome",
        "proxy-entry-select",
    ):
        assert f".{name}" in ADMIN_CSS, name
