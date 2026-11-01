/* ══════════════════════════════════════════════════════════════════════════
   8.1.0 — THE DESIGN REGISTRY.

   specs/DESIGN-REVAMP-OPTIONS.md §13.2.2: "One declared table, read by the
   server and the dashboard." A design is **data + CSS + an optional shell
   module, never a fork of admin.js**. Adding one is 1 CSS file (+1 optional
   JS) + 1 registry row + screenshots + the parity run — no route, no payload,
   no setting and no test change.

   8.1.0 SHIPS EXACTLY ONE REAL ENTRY: `today`. Zero CSS, zero JS, the current
   rendering. Everything else — Ledger, Console, Hybrid, Palette, and the rest
   — is a later PR that appends a row to this array.

   THE PROOF THAT ADDING A ROW IS DATA-ONLY is that nothing in admin.js names
   a design: it reads this array, and every field it uses is one of the keys
   documented below. `tests/contracts/test_admin_design_registry.py` checks
   that, and `test_design_loader_takes_a_new_row_as_data` feeds the loader a
   two-row table and asserts both tiles render without any other change.

   KEYS (all optional except `id` and `name`):
     id               the value stored in `mcc-admin-design` and in
                      ADMIN_DESIGN. Lower-case, no spaces.
     name             the label a reader sees.
     version          integer, bumped when a design's own CSS changes shape.
     status           "stable" | "preview" | "classic" | "deprecated".
                      §13.2.4: `today` stays selectable FOREVER and is
                      labelled "Classic" (round-4 Q5).
     defaultTheme     "velvet" | "today" | "own" | … — the family this design
                      wants. The machine setting ADMIN_THEME overrides it.
     defaultDensity   "compact" | "comfortable" — §13.2.1's per-design default
                      (comfortable for Ledger/Atelier/Inset; compact for
                      Console/Panels/Grid/Terminal/Workbench).
     css              a path under admin_static/, or null for zero CSS.
     shell            a path under admin_static/, or null for no shell module.
     layout           free-form tags describing the structure, used by the
                      tiles' one-line note and by later parity reports.
     defaultFor       "fallback" for the row a page uses when nothing else is
                      chosen. Exactly one row may carry it.
     note             one line, shown under the tile's name.

   NO URL EVER NAMES A DESIGN (§13.2.3 Q6, round-4 Q13): it is a preference,
   not a route, and the loader reads it from storage + config only.
   ══════════════════════════════════════════════════════════════════════════ */

/* eslint-disable no-unused-vars -- read by admin.js, which is a classic script */

const ADMIN_DESIGNS = [
  {
    id: "today",
    name: "Today",
    version: 1,
    status: "classic",
    defaultTheme: "velvet",
    defaultDensity: "comfortable",
    css: null,
    shell: null,
    layout: ["pages", "sidebar", "sections"],
    defaultFor: "fallback",
    note: "The dashboard as it is: one page at a time, a grouped sidebar, every setting on its page.",
  },
];

/* The two axis vocabularies live beside the registry so the Appearance card
 * and any later design read the same lists. A family here with no palette in
 * `themes.css` would render an unstyled page, so the contract test compares
 * this list against that file's selectors. */
const ADMIN_THEME_FAMILIES = [
  { id: "today", name: "Today" },
  { id: "midnight", name: "Midnight" },
  { id: "paper", name: "Paper" },
  { id: "hc", name: "High Contrast" },
  { id: "velvet", name: "Velvet" },
  { id: "navy", name: "Navy" },
];

const ADMIN_THEME_MODES = [
  { id: "light", name: "Light" },
  { id: "dark", name: "Dark" },
];

/* §13.2.1: each design declares a default and Settings offers Compact /
 * Comfortable on top of it. "design" means "whatever the selected row says",
 * which is why it is not `data-density` value — the loader simply writes no
 * attribute at all in that case. */
const ADMIN_DENSITIES = [
  { id: "design", name: "Design default" },
  { id: "compact", name: "Compact" },
  { id: "comfortable", name: "Comfortable" },
];

/* 8.1.0's compiled defaults, §13.2.1 and round-4 Q4: Velvet in dark is the
 * user's explicit instruction; Today in light is the interim default. These
 * are what a user with nothing saved and nothing set on the machine gets. */
const ADMIN_APPEARANCE_DEFAULTS = {
  design: "today",
  theme: "velvet",
  mode: "dark",
  density: "design",
  lightTheme: "today",
};
