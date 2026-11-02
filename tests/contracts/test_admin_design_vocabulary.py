"""The design registry and the server's vocabulary agree, in both directions.

A design lives in two places that nothing used to compare:

* ``api/admin_static/designs.js`` — the rows the page draws and selects from;
* ``config/constants.ADMIN_DESIGN_VALUES`` — the words ``ADMIN_DESIGN`` will
  accept, which the page also uses to refuse a hand-edited value before it is
  saved.

8.1.0 shipped exactly one word in each and they happened to agree. The first
design PR is where that luck runs out in both directions:

* a ROW the server does not know is a design a reader can pick in the card and
  cannot save as the machine default — the page would accept it, the settings
  payload would reject it, and the error would name a variable rather than the
  design;
* a WORD the registry does not have is the worse one: ``ADMIN_DESIGN=hybrid``
  in a hand-edited ``.env`` passes every server check and then resolves, in the
  browser, to no row at all. The page falls back to ``today`` without a word,
  which is precisely the "silently did nothing" failure that a settings file is
  not allowed to have.

The registry is read by PARSING the shipped JavaScript rather than by
importing a Python mirror of it: a mirror would only ever agree with itself
(the same reason ``test_admin_sections_are_rendered`` reads ``VIEW_GROUPS`` out
of admin.js).

Two more things are pinned here because they are the registry's own contract
and a later design could break them by accident:

* every row that declares a ``shell`` also declares the ``css`` beside it (a
  shell styles its own nodes; a shell without a stylesheet is a design that
  renders unstyled chrome);
* every ``attrs`` key is one the foundation actually implements, read out of
  ``foundation.css`` — a design cannot declare a variant attribute nothing
  styles and then wonder why its page looks like Today's.
"""

import re
from pathlib import Path

from my_claude_code.config.constants import ADMIN_DESIGN_VALUES

REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_STATIC = REPO_ROOT / "src/my_claude_code/api/admin_static"
DESIGNS_JS = (ADMIN_STATIC / "designs.js").read_text(encoding="utf-8")
FOUNDATION_CSS = (ADMIN_STATIC / "foundation.css").read_text(encoding="utf-8")

#: The variant attributes `foundation.css` implements, read from its selectors.
#: `density` is the loader's own (written by `applyAppearance`) and is not a
#: design's to declare, so it is deliberately absent from the registry's
#: allowlist in admin.js; `shell` covers the shell's own body class.
_IMPLEMENTED_VARIANTS = frozenset(
    re.findall(r"html\[data-([a-z]+)=", FOUNDATION_CSS)
) | {"sections", "fields", "detail", "analytics", "density", "shell"}


def _registry_rows() -> list[dict[str, str]]:
    """Every row in ``ADMIN_DESIGNS``, as a dict of its literal fields.

    Only the fields this test needs, and only when they are written as plain
    literals — which is the shape the registry itself documents. A row that
    computed its id at runtime would not be a row a reader could be shown.
    """

    start = DESIGNS_JS.index("const ADMIN_DESIGNS = [")
    end = DESIGNS_JS.index("\n];", start)
    block = DESIGNS_JS[start:end]
    rows: list[dict[str, str]] = []
    # Each row opens with `{` on its own line and its `id:` is the first field.
    for chunk in re.split(r"\n  \{\n", block)[1:]:
        row: dict[str, str] = {}
        for field in ("id", "name", "status", "defaultTheme", "defaultDensity"):
            match = re.search(rf'^\s*{field}: "([^"]+)"', chunk, re.MULTILINE)
            if match:
                row[field] = match.group(1)
        css = re.search(r"^\s*css: (\S+),$", chunk, re.MULTILINE)
        shell = re.search(r"^\s*shell: (\S+),$", chunk, re.MULTILINE)
        row["css"] = (css.group(1) if css else "").strip('"')
        row["shell"] = (shell.group(1) if shell else "").strip('"')
        attrs = re.search(r"^\s*attrs: \{(.*?)\},$", chunk, re.MULTILINE | re.DOTALL)
        row["attrs"] = attrs.group(1) if attrs else ""
        rows.append(row)
    return rows


def test_the_registry_parses() -> None:
    """A silent parse failure would make every assertion below vacuous."""

    rows = _registry_rows()
    assert [row["id"] for row in rows][:2] == ["today", "hybrid"], (
        "the registry parse found no rows, or not the two 8.2.0 ships: "
        f"{[row.get('id') for row in rows]}"
    )
    assert rows[0]["status"] == "classic"


def test_every_registry_row_is_a_word_the_server_accepts() -> None:
    """A row a reader can pick must be a value ``ADMIN_DESIGN`` will save."""

    rows = _registry_rows()
    missing = sorted(row["id"] for row in rows if row["id"] not in ADMIN_DESIGN_VALUES)
    assert not missing, (
        f"designs.js declares {missing} but ADMIN_DESIGN_VALUES "
        f"({', '.join(ADMIN_DESIGN_VALUES)}) refuses them; picking one as the "
        "machine default would be rejected by the settings payload"
    )


def test_every_word_the_server_accepts_is_a_registry_row() -> None:
    """The worse direction: a saved value that resolves to no design at all."""

    rows = _registry_rows()
    ids = {row["id"] for row in rows}
    orphans = sorted(word for word in ADMIN_DESIGN_VALUES if word not in ids)
    assert not orphans, (
        f"ADMIN_DESIGN accepts {orphans} but designs.js has no row for them, so "
        "a .env naming one would render Today without saying so"
    )


def test_a_row_with_a_shell_also_has_a_stylesheet() -> None:
    """A shell builds nodes; something has to style them."""

    for row in _registry_rows():
        if row["shell"] and row["shell"] != "null":
            assert row["css"] and row["css"] != "null", (
                f"design {row['id']!r} declares shell {row['shell']!r} and no css"
            )


def test_every_declared_attribute_is_a_variant_the_foundation_implements() -> None:
    """A design declares only attributes something actually styles."""

    for row in _registry_rows():
        for name in re.findall(r"([a-z_]+):", row["attrs"]):
            assert name in _IMPLEMENTED_VARIANTS, (
                f"design {row['id']!r} declares attrs.{name}, which no selector "
                "in foundation.css implements — the design would silently look "
                "like Today's"
            )


def test_the_referenced_files_ship() -> None:
    """A registry row naming a file that is not in the wheel is a 404.

    The route answers from the directory listing
    (``admin_routes._bundled_design_names``), so a missing file is a 404 on the
    stylesheet or the module rather than an error anyone sees at build time.
    """

    designs_dir = ADMIN_STATIC / "designs"
    for row in _registry_rows():
        for field in ("css", "shell"):
            value = row[field]
            if not value or value == "null":
                continue
            # The registry spells paths relative to admin_static/.
            path = ADMIN_STATIC / value
            assert path.is_file(), (
                f"design {row['id']!r}: {field} {value!r} does not exist"
            )
            assert designs_dir in path.parents, (
                f"design {row['id']!r}: {field} {value!r} is outside admin_static/designs/, "
                "which is the only directory the asset route serves from"
            )
