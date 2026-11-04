/**
 * CARDS-OVERLAY: what each view's DOM looks like, and what a collapsed provider
 * card carries, so "only the card moved" is a comparison rather than a claim.
 *
 *   node admin_jsdom_cards_overlay.mjs <admin_static_dir>
 *   -> one JSON object on stdout
 *
 * Run it against a BASE tree's admin_static to produce the baseline and
 * against this tree to compare: `test_admin_cards_overlay_jsdom.py` reads
 * `tests/fixtures/cards_overlay_dom_8_9_0.json`, written that way from the
 * base tree `32109387` (v8.9.0) with this same payload.
 *
 * The per-view hash is of the view's own markup with whitespace collapsed and
 * the page's own clock normalised away (a "5 seconds ago" stamp differs
 * between two runs of the SAME tree, so leaving it in would report a change on
 * every run and prove nothing either way). A card that moved by a byte still
 * changes its view's hash.
 */
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { join } from "node:path";
import { JSDOM, VirtualConsole } from "jsdom";

const dir = process.argv[2];
if (!dir) {
  console.error("usage: admin_jsdom_cards_overlay.mjs <admin_static_dir>");
  process.exit(2);
}

const html = readFileSync(join(dir, "index.html"), "utf8");
const designsScript = readFileSync(join(dir, "designs.js"), "utf8");
const usageLayoutsScript = readFileSync(join(dir, "usage_layouts.js"), "utf8");
const script = [usageLayoutsScript, designsScript, readFileSync(join(dir, "admin.js"), "utf8")].join("\n");

/* One provider, one search provider, and the eight fields between them: the
   smallest payload that draws a card of each kind with a key pool, a rotation
   policy, an advanced field and help text. */
const FIELDS = [
  {
    key: "PROBE_API_KEY",
    label: "Probe API key",
    section: "providers",
    provider: "probe",
    type: "text",
    value: "",
    default: "",
    configured: true,
    secret: true,
    advanced: false,
    description: "One key per line, comma separated.",
    options: [],
  },
  {
    key: "PROBE_API_KEY_ROTATION",
    label: "Probe rotation",
    section: "providers",
    provider: "probe",
    type: "select",
    value: "round_robin",
    default: "single",
    configured: true,
    secret: false,
    advanced: false,
    description: "",
    options: [
      { value: "single", label: "Single key" },
      { value: "round_robin", label: "Round robin" },
    ],
  },
  {
    key: "PROBE_API_KEY_PROXY",
    label: "Probe proxy",
    section: "providers",
    provider: "probe",
    type: "text",
    value: "",
    default: "",
    configured: false,
    secret: false,
    advanced: true,
    description: "A proxy this provider leaves through, host:port.",
    options: [],
  },
  {
    key: "WEB_SEARCH_PROVIDER",
    label: "Web search provider",
    section: "websearch",
    provider: null,
    type: "select",
    value: "auto",
    default: "auto",
    configured: true,
    secret: false,
    advanced: false,
    description: "",
    options: [
      { value: "auto", label: "Auto (first configured)" },
      { value: "probe_search", label: "Probe Search" },
    ],
  },
  {
    key: "PROBE_SEARCH_API_KEY",
    label: "Probe Search API Key",
    section: "websearch",
    provider: null,
    type: "text",
    value: "ws-probe1111",
    default: "",
    configured: true,
    secret: true,
    advanced: false,
    description: "Your search key.",
    options: [],
  },
  {
    key: "PROBE_SEARCH_MAX_RESULTS",
    label: "PROBE_SEARCH max results",
    section: "websearch",
    provider: null,
    type: "number",
    value: "8",
    default: "8",
    configured: true,
    secret: false,
    advanced: true,
    description: "How many results this provider returns per search.",
    options: [],
  },
];

const PROVIDER = {
  provider_id: "probe",
  display_name: "Probe Provider",
  group: "cloud",
  kind: "remote",
  status: "configured",
  label: "Configured",
  key_count: 2,
  credential_env: "PROBE_API_KEY",
  credential_owner_id: "probe",
  credential_owner_name: "Probe Provider",
  credential_shared_with: [],
};

const ROUTES = {
  "/admin/api/config": {
    sections: [
      { id: "providers", label: "Providers", description: "Where requests go.", advanced: false },
      { id: "websearch", label: "Web search", description: "Search.", advanced: false },
    ],
    provider_groups: [],
    fields: FIELDS,
    provider_status: [PROVIDER],
    paths: { managed: "/tmp/probe/.env", repo: "", explicit: null },
    messaging_auth_open: [],
  },
  "/admin/api/onboarding": { dismissed: true, complete: true, steps: [] },
  "/admin/api/providers/local-status": { providers: [] },
  "/admin/api/config/validate": { valid: true, errors: [] },
  "/admin/api/version": { current: "9.0.0" },
  "/admin/api/desktop": { available: false },
  "/admin/api/rtk": { installed: false },
  "/admin/api/rtk/gain": { available: false, reason: "not_installed", detail: "" },
  "/admin/api/claude/settings": { configured: false },
  "/admin/api/claude/config": { entries: [], values: {}, path: "", parsed: true },
  "/admin/api/custom-providers": { providers: [] },
  "/admin/api/models": { models: ["probe/one", "probe/two"], failed_providers: [], blind_models: [] },
  "/admin/api/requests": { rows: [], total: 0 },
  "/admin/api/requests/pulse": { enabled: false, total: 0, latest: null },
  "/admin/api/docs": { documents: [] },
  "/admin/api/providers/probe/test": { ok: true, models: ["one", "two"] },
  "/admin/api/credentials/PROBE_API_KEY/keys": {
    count: 2,
    locked: false,
    rows: [
      { index: 0, id: "sha256:11111111111111111111", masked: "prb-…1111", key_label: "prb-…1111", name: "", health: null },
      { index: 1, id: "sha256:22222222222222222222", masked: "prb-…2222", key_label: "prb-…2222", name: "", health: null },
    ],
    keys: ["prb-…1111", "prb-…2222"],
    health: { keys: [] },
  },
  "/admin/api/websearch/credentials/PROBE_SEARCH_API_KEY/keys": {
    locked: false,
    keys: [{ index: 0, id: "sha256:33333333333333333333", key_label: "ws-p…1111" }],
    health: { keys: [] },
  },
};

const virtualConsole = new VirtualConsole();
const consoleErrors = [];
virtualConsole.on("jsdomError", (error) => consoleErrors.push(String(error.message)));

const dom = new JSDOM(html, {
  url: "http://127.0.0.1:8080/admin",
  runScripts: "outside-only",
  pretendToBeVisual: true,
  virtualConsole,
});
const { window } = dom;
class NoopObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() {
    return [];
  }
}
window.IntersectionObserver = NoopObserver;
window.ResizeObserver = NoopObserver;
window.scrollTo = () => {};
// jsdom has no canvas: the charts ask for a 2d context and get null, which is
// what the other harnesses stub for the same reason.
window.HTMLCanvasElement.prototype.getContext = () => ({
  clearRect() {},
  setTransform() {},
  beginPath() {},
  moveTo() {},
  lineTo() {},
  stroke() {},
  fill() {},
  save() {},
  restore() {},
  scale() {},
  arc() {},
  fillText() {},
  measureText: () => ({ width: 0 }),
  createLinearGradient: () => ({ addColorStop() {} }),
  putImageData() {},
  getImageData: () => ({ data: [] }),
});
window.matchMedia =
  window.matchMedia ||
  ((query) => ({
    matches: false,
    media: query,
    addEventListener() {},
    removeEventListener() {},
    addListener() {},
    removeListener() {},
    onchange: null,
    dispatchEvent: () => false,
  }));
window.fetch = async (url) => {
  const path = String(url).split("?")[0];
  const body = Object.prototype.hasOwnProperty.call(ROUTES, path) ? ROUTES[path] : {};
  return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
};

const scriptErrors = [];
window.addEventListener("error", (event) => scriptErrors.push(String(event.message)));
try {
  window.eval(script);
} catch (error) {
  console.log(JSON.stringify({ fatal: String(error && error.stack) }, null, 2));
  process.exit(0);
}
window.document.dispatchEvent(new window.Event("DOMContentLoaded", { bubbles: true }));
await new Promise((resolve) => setTimeout(resolve, 900));

const doc = window.document;
const settle = () => new Promise((resolve) => setTimeout(resolve, 140));

/* A view's own markup, whitespace-collapsed: the thing "this view did not
   move" is a claim about.
 *
 * Three things in this dashboard's markup are the CLOCK, not the page: the
 * "as of" stamps (`5 seconds ago`), the in-flight badge's count, and the
 * request rows' rendered times. They differ between two runs of the same tree,
 * so they are normalised away -- a comparison that reported them would call
 * every run a change and prove nothing either way. */
const VOLATILE = [
  /\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?/g,
  /\b\d+\s*(?:second|minute|hour|day)s?\s+ago\b/g,
  /\bjust now\b/g,
  /\b\d{1,2}:\d{2}(?::\d{2})?\b/g,
  // The in-flight badge and the "last updated" lines carry a live count.
  /(<[^>]*id="reqInflightStatus"[^>]*>)[^<]*(<\/)/g,
  /(<[^>]*id="reqLastUpdated"[^>]*>)[^<]*(<\/)/g,
  /(<[^>]*id="webSearchLastUpdated"[^>]*>)[^<]*(<\/)/g,
];

const fingerprint = (node) => {
  let text = String(node.innerHTML).replace(/\s+/g, " ").trim();
  for (const pattern of VOLATILE) {
    text = text.replace(
      pattern,
      pattern.source.includes("id=") ? "$1<t>$2" : "<t>",
    );
  }
  return createHash("sha256").update(text).digest("hex").slice(0, 16);
};

const views = {};
for (const link of Array.from(doc.querySelectorAll(".nav-link"))) {
  link.click();
  await settle();
  const id = link.dataset.view;
  const view = doc.querySelector(`.admin-view[data-view="${id}"]`);
  views[id] = view ? fingerprint(view) : null;
}

/* What a collapsed provider card holds, as ordered child classes and control
   labels -- the contract this release changes on purpose. */
const cards = {};
{
  const link = Array.from(doc.querySelectorAll(".nav-link")).find(
    (node) => node.dataset.view === "providers",
  );
  link.click();
  await settle();
  const card = doc.querySelector('.pv-card[data-provider="probe"]');
  cards.provider = card
    ? {
        children: Array.from(card.children).map((node) => node.className.split(" ").slice(0, 1)[0]),
        controls: Array.from(card.querySelectorAll("button")).map((b) => b.textContent),
        dataKeys: card.querySelectorAll("[data-key]").length,
        role: card.getAttribute("role") ?? null,
        tabIndex: card.tabIndex,
        pill: card.querySelector(".status-pill")?.textContent ?? null,
      }
    : null;

  const wsLink = Array.from(doc.querySelectorAll(".nav-link")).find(
    (node) => node.dataset.view === "web_search",
  );
  wsLink.click();
  await settle();
  const wsCard = doc.querySelector('.ws-card[data-websearch-provider="probe_search"], #webSearchGrid .provider-card');
  cards.webSearch = wsCard
    ? {
        children: Array.from(wsCard.children).map((node) => node.className.split(" ").slice(0, 1)[0]),
        controls: Array.from(wsCard.querySelectorAll("button")).map((b) => b.textContent),
        dataKeys: wsCard.querySelectorAll("[data-key]").length,
        role: wsCard.getAttribute("role") ?? null,
      }
    : null;
}

/* Is the overlay in the page? A base tree has no `#providerDetailModal`, and
   that absence is recorded rather than assumed. */
const overlay = {
  present: Boolean(doc.getElementById("providerDetailModal")),
  role: doc.getElementById("providerDetailModal")?.getAttribute("role") ?? null,
  ariaModal: doc.getElementById("providerDetailModal")?.getAttribute("aria-modal") ?? null,
  closeable: Boolean(doc.getElementById("providerDetailClose")),
};

/* Exit only once the answer has left the process: a pipe to the test is
   asynchronous on Linux, and `console.log` followed by `process.exit` cut a
   ~190 KB answer at the pipe buffer on CI once already (see
   admin_jsdom_harness.mjs). Nothing else keeps the process alive -- the page's
   poll timers do -- so the exit is required, not a shortcut. */
await new Promise((resolve) =>
  process.stdout.write(
    `${JSON.stringify({ fatal: null, scriptErrors, consoleErrors, views, cards, overlay }, null, 2)}\n`,
    resolve,
  ),
);
process.exit(0);
