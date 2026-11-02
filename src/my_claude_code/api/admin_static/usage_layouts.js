/* ══════════════════════════════════════════════════════════════════════════
   8.7.0 (USAGE-LAYOUTS) — THE USAGE LAYOUT REGISTRY.

   specs/PR-USAGE-LAYOUTS-SPEC.md §3, following §13.2.2's precedent exactly:
   **one declared table, one renderer that switches on the chosen id, and no
   layout logic in the poller, the payload or the routes.**

   What this file is, and what it is not:

   * It is DATA — five rows of `{ id, name, note, render }`.
   * It is not a fork of `admin.js`. Every renderer below composes the SAME
     vocabulary that `admin.js` already decides (`usageLayoutModel()`,
     `usageWindow`, `usageState`, `usageMoney`, …). A layout chooses WHERE a
     figure goes. It never chooses WHAT a figure means, whether a bar may be
     drawn, or whether a provider appears at all.
   * It is loaded BEFORE `admin.js`, like `designs.js`, so the renderer finds
     `USAGE_LAYOUTS` already declared.

   THE FIVE IDS ARE SPELLED TWICE ON PURPOSE. `config/constants.py` carries
   `USAGE_LAYOUT_VALUES` (the server's vocabulary, refused on save and folded on
   load) and this file carries the table the page renders from. They are pinned
   together in both directions by `tests/contracts/test_usage_layout_registry.py`:
   a word in the constants with no row here is a setting the page cannot draw,
   and a row here with no constant is a layout nobody can select.

   THE FOURTH PREVIEW DOES NOT EXIST HERE. `specs/PR-USAGE-LAYOUTS-SPEC.md`
   §8.2 excludes it permanently — no row, no CSS, no setting value — so this
   file, like every other shipped asset, never names it.
   `tests/contracts/test_usage_layout_registry.py` asserts that in both the
   folder-name and the id spellings.

   ADDING A SIXTH LAYOUT IS ONE ROW AND ONE RENDER FUNCTION, exactly as the
   design registry's rule says. Nothing in the routes, the payload, the poller
   or the settings layer changes: they all read the id from one place.

   KEYS
     id      the value stored in `USAGE_LAYOUT`. Lower-case, no spaces, and
             identical to the entry in `config/constants.USAGE_LAYOUT_VALUES`.
     name    the label a reader sees in the setting's option list and in the
             page's own picker.
     note    one sentence: what this layout looks like, in a reader's words.
             The manifest's option labels and the picker both quote it, so the
             Control, the picker and the docs cannot describe it three ways.
     render  `(root, model) => void`. Builds the whole page body inside
             `root`. It must render EVERY provider in `model.rows` — that is
             the coverage rule (§2) and the proof in §5.1 asserts it per
             layout with one fixture.
   ══════════════════════════════════════════════════════════════════════════ */

/* eslint-disable no-unused-vars -- read by admin.js, which is a classic script */

const USAGE_LAYOUTS = [
  {
    id: "one_screen",
    name: "One screen",
    note: "One compact row per provider that opens in place, one row at a time — the shortest of the five.",
    /* Preview 06. The default (§8.1), because it is the shortest at every
       width the spec measures and because its expansion is the pattern
       UI-QUALITY-RULE §D already established for a request's detail. */
    render: (root, model) => window.MCCUsageLayouts.oneScreen(root, model),
  },
  {
    id: "ledger_rows",
    name: "Ledger rows",
    note: "One table row per provider with a micro-bar beside each window — the densest.",
    render: (root, model) => window.MCCUsageLayouts.ledgerRows(root, model),
  },
  {
    id: "meter_board",
    name: "Meter board",
    note: "Four counted headline figures over full-width bands, so every bar shares one column and one scale.",
    render: (root, model) => window.MCCUsageLayouts.meterBoard(root, model),
  },
  {
    id: "dial_cards",
    name: "Dial cards",
    note: "A card per provider leading with one large ring for its most-pressured window.",
    render: (root, model) => window.MCCUsageLayouts.dialCards(root, model),
  },
  {
    id: "master_detail",
    name: "Master and detail",
    note: "A provider list beside a full inspector for the one you select.",
    render: (root, model) => window.MCCUsageLayouts.masterDetail(root, model),
  },
];

/*: What an unknown id draws. §3: "Unknown value → `one_screen`". The server
 *  already falls back with one log line (``Settings.normalise_usage_layout``);
 *  this is the page's half, so a payload from an older server, a stale cached
 *  `state.config`, or a layout a later release removed still paints a page
 *  instead of an empty one. */
const USAGE_LAYOUT_FALLBACK_ID = "one_screen";

/** The registry row for one id, or the fallback row.
 *
 *  Never returns `undefined`: every caller draws a page, and a missing row is
 *  a fallback rather than a blank. The one log line lives in the server's
 *  settings validator, so this stays a pure lookup.
 */
function usageLayoutFor(id) {
  const wanted = String(id == null ? "" : id).trim().toLowerCase();
  return (
    USAGE_LAYOUTS.find((row) => row.id === wanted) ||
    USAGE_LAYOUTS.find((row) => row.id === USAGE_LAYOUT_FALLBACK_ID) ||
    USAGE_LAYOUTS[0]
  );
}

/** Every id this page can draw, in the order the picker lists them. */
function usageLayoutIds() {
  return USAGE_LAYOUTS.map((row) => row.id);
}
