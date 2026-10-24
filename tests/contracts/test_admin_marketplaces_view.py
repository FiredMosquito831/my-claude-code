"""What the InferHub page (7.110.0) must keep, in the shipped files.

Mirrors `test_admin_proxying_view.py`: these do not replace the jsdom
scenario, which drives the gestures; they pin the shape the gestures are
built out of, so a refactor that quietly adds a second writer of
``model_overrides.json``, or draws a third party's text as markup, fails a
check rather than a user's file.

The one that matters most is `test_assignments_have_exactly_one_write_path`:
6.24.0 exists because the Models page kept a per-row write beside its bulk
one and the two drifted. The page's assignments are the Models page's own
override route, called from one function, and nothing on the server side of
this page can write the override file at all.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "my_claude_code"
STATIC_DIR = SRC / "api" / "admin_static"
ADMIN_JS = (STATIC_DIR / "admin.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
ROUTES_PY = (SRC / "api" / "admin_marketplace_routes.py").read_text(encoding="utf-8")
PAGE_PY = (SRC / "api" / "marketplace_page.py").read_text(encoding="utf-8")
OVERRIDE_ROUTE = "/admin/api/model-admin/overrides"
BLOCK_START = (
    "/* ----------------------------------------------------------- marketplaces"
)
BLOCK_END = (
    "/* --------------------------------------------------------------- proxying"
)


def _block() -> str:
    first = ADMIN_JS.index(BLOCK_START)
    return ADMIN_JS[first : ADMIN_JS.index(BLOCK_END, first)]


def _function(name: str) -> str:
    block = _block()
    start = block.index(f"function {name}(")
    following = re.search(r"\n(?:async )?function ", block[start + 1 :])
    end = start + 1 + following.start() if following else len(block)
    return block[start:end]


def test_assignments_have_exactly_one_write_path() -> None:
    """One function posts the 15th key, to the Models page's existing route."""

    block = _block()
    assert block.count(OVERRIDE_ROUTE) == 1
    writer = _function("saveMarketplaceAssignment")
    assert OVERRIDE_ROUTE in writer
    assert 'method: "POST"' in writer
    # Both assignment gestures (a row's Save, "Add row") call that one function.
    calls = re.findall(r"(?<!function )saveMarketplaceAssignment\((?!\))", block)
    assert len(calls) == 2
    # The page adds no override route of its own on either side.
    assert not re.search(r'@router\.\w+\(\s*"[^"]*model-admin', ROUTES_PY)
    assert all(
        path.startswith("/admin/api/marketplaces")
        for path in re.findall(r'@router\.\w+\(\s*"([^"]+)"', ROUTES_PY)
    )
    for source in (ROUTES_PY, PAGE_PY):
        assert "save_model_overrides" not in source
        assert "model_overrides_path" not in source
        assert "with_override_row" not in source


def test_only_the_models_route_writes_the_override_file() -> None:
    """The one writer is still admin_routes.py's two Models routes."""

    writers = sorted(
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if "save_model_overrides" in path.read_text(encoding="utf-8")
        and path.name != "model_overrides.py"
        and path.name != "model_admin.py"  # a docstring naming the owner
    )
    assert writers == ["api/admin_routes.py"]


def test_the_profile_store_has_one_write_path_too() -> None:
    block = _block()
    assert len(re.findall(r'api\(\s*"/admin/api/marketplaces/profiles"', block)) == 1
    assert "/admin/api/marketplaces/profiles" in _function("putMarketplaceProfiles")
    assert ROUTES_PY.count("save_marketplace_profiles(") == 1


def test_the_page_draws_text_never_markup() -> None:
    """Route notes, model ids and combo names are a third party's words."""

    block = _block()
    assert not re.search(r"\.(?:inner|outer)HTML\b|insertAdjacentHTML\(", block)


def test_the_view_is_registered_like_the_proxying_page() -> None:
    match = re.search(r'\{\s*//[^{}]*?id: "marketplaces",(.*?)\}', ADMIN_JS, re.DOTALL)
    assert match is not None
    entry = match.group(1)
    assert 'label: "InferHub"' in entry
    assert "sections: []" in entry
    assert "containerId: null" in entry
    assert 'data-view="marketplaces"' in INDEX_HTML


def test_the_blocks_are_in_the_specified_order() -> None:
    order = [
        'id="marketplaceHonesty"',
        'id="marketplaceAccount"',
        'id="marketplaceProfiles"',
        'id="marketplaceAssignments"',
        'id="marketplaceExplorer"',
        'id="marketplaceRecent"',
        'id="marketplaceSnapshot"',
    ]
    positions = [INDEX_HTML.index(marker) for marker in order]
    assert positions == sorted(positions)


def test_the_charged_ask_is_read_only_on_its_button() -> None:
    """The user's Q17: InferHub's usage log is never read on its own."""

    block = _block()
    assert block.count("/charged`") == 1
    fetcher = _function("fetchMarketplaceCharged")
    assert "/charged`" in fetcher
    # Its only caller is the button's click handler.
    assert len(re.findall(r"(?<!function )fetchMarketplaceCharged\(\)", block)) == 1
    assert "() => fetchMarketplaceCharged()" in block
    assert "mkt-recent-charged" in block
