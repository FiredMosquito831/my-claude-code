/**
 * CARDS-FIX-2: the custom-provider card is a face, and its panel holds every
 * control the card used to carry.
 *
 *   node admin_jsdom_custom_cards.mjs <admin_static_dir>
 *   -> one JSON object on stdout
 *
 * USER 2026-11-04: "also i noticed that custom providers didn't get the same
 * benefits that normal providers and websearch providers did."
 *
 * This is its own harness, and its own page load, so the mutation controls in
 * `test_admin_custom_cards_jsdom.py` can run it against copies of `admin.js`
 * with one line removed without paying for the full UX pass each time.
 *
 * The completeness proof is a LIST, not a judgement: `CARD_CONTROLS` is what
 * the pre-8.11.0 `customProviderCard` rendered inline, and every assertion
 * compares the rendered body against it. Dropping a control from the body
 * makes `missing` non-empty.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { JSDOM, VirtualConsole } from "jsdom";

const dir = process.argv[2];
if (!dir) {
  console.error("usage: admin_jsdom_custom_cards.mjs <admin_static_dir>");
  process.exit(2);
}

const html = readFileSync(join(dir, "index.html"), "utf8");
const designsScript = readFileSync(join(dir, "designs.js"), "utf8");
const usageLayoutsScript = readFileSync(join(dir, "usage_layouts.js"), "utf8");
const script = [
  usageLayoutsScript,
  designsScript,
  readFileSync(join(dir, "admin.js"), "utf8"),
].join("\n");

/* Two custom providers, the smallest payload that draws the two states a face
   has to distinguish: one that has answered a model list and one nobody has
   asked. `probe` carries every optional key so the panel's own blocks all
   render -- a media clause, a second wire door, a proxy, a learned dialect.

   `probe.model_count` is 0 on purpose. A custom provider's count comes from
   the listing payload's own `model_count`, so a factory-fresh entry reads 0
   until one of its controls asks -- which is the third state
   ("Not detected yet"), and it is what the page's own load draws. A non-zero
   number here would make that state untestable on the load. */
const providers = [
  {
    provider_id: "custom_probe",
    display_name: "Probe Gateway",
    base_url: "https://api.probe.example/v1",
    key_count: 3,
    masked_keys: [
      "prb-…aaaa",
      "prb-…bbbb",
      "prb-…cccc",
    ],
    credential_rotation: "failover",
    proxy: "http://127.0.0.1:7890",
    enabled: true,
    model_count: 0,
    status: "configured",
    models: [],
    added_at: "2026-01-01T00:00:00Z",
    reasoning_effort_enum: ["low", "high", "max"],
    reasoning_field_ignored: false,
    reasoning_probe_status: "learned",
    reasoning_probed_at: "2026-09-01T10:00:00+00:00",
    reasoning_dialect_label: "learned {low, high, max} on 2026-09-01",
    surfaces: ["chat_completions", "responses"],
    available_surfaces: [
      { value: "chat_completions", label: "Chat Completions (/chat/completions)" },
      { value: "responses", label: "Responses (/responses)" },
      { value: "messages", label: "Messages (/messages)" },
    ],
    media_operations: ["speech"],
    available_media_operations: [
      { value: "speech", label: "Speech (/audio/speech)" },
    ],
    auto_paused_refs: [],
  },
  {
    provider_id: "custom_quiet",
    display_name: "Quiet Gateway",
    base_url: "https://api.quiet.example/v1",
    key_count: 1,
    masked_keys: ["qut-…dddd"],
    credential_rotation: "single",
    proxy: null,
    enabled: false,
    model_count: 0,
    status: "disabled",
    models: [],
    added_at: "2026-01-01T00:00:00Z",
    reasoning_effort_enum: null,
    reasoning_dialect_label: "unknown (not probed)",
    surfaces: ["chat_completions"],
    available_surfaces: [],
    media_operations: [],
    available_media_operations: [],
    auto_paused_refs: [],
  },
];

/* What the provider's own answer says. One entry per provider id; a test can
   change it before pressing the control. */
const answers = new Map([
  ["custom_probe", { ok: true, models: ["m1", "m2", "m3"] }],
  ["custom_quiet", { ok: true, models: [] }],
]);
/* What the SERVER's cached catalogue holds, which is what the listing route
   reports as `model_count`. A discovery writes it; the listing only reads it.
   Two separate facts on purpose: that is how the product works, and a harness
   that made the listing echo the control's own answer would let a repaint bug
   pass.
   Seeded from the provider payload, so the first load's count is what the
   listing said before anything was asked. */
const listingCounts = new Map(
  providers.map((provider) => [provider.provider_id, provider.model_count]),
);
let held = null;

const keyListing = (provider) => ({
  provider_id: provider.provider_id,
  env_key: null,
  source: "custom_providers.json",
  locked: false,
  credential_rotation: provider.credential_rotation,
  count: provider.masked_keys.length,
  keys: provider.masked_keys,
  health: provider.masked_keys.map((label, index) => ({
    state: "HEALTHY",
    request_count: 12 - index,
    failure_count: 0,
    cooldown_remaining: 0,
    lockout_remaining: 0,
    model_benches: [],
    index,
    key_label: label,
  })),
  rows: provider.masked_keys.map((label, index) => ({
    index,
    id: `sha256:${"1".repeat(15)}${index}`,
    masked: label,
    key_label: label,
    name: "",
  })),
});

const ROUTES = {
  "/admin/api/config": {
    sections: [
      { id: "providers", label: "Providers", description: "", advanced: false },
    ],
    provider_groups: [],
    // No built-in provider fields at all: this page is about the custom grid,
    // and an empty `fields` keeps `renderProviderGroups` out of the way.
    fields: [],
    provider_status: [],
    paths: { managed: "/tmp/cc/.env", repo: "", explicit: null },
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
  "/admin/api/custom-providers": { providers },
  "/admin/api/models": { models: [], failed_providers: [], blind_models: [] },
  "/admin/api/requests": { rows: [], total: 0 },
  "/admin/api/requests/pulse": { enabled: false, total: 0, latest: null },
  "/admin/api/docs": { documents: [] },
};
for (const provider of providers) {
  ROUTES[`/admin/api/custom-providers/${provider.provider_id}/keys`] =
    keyListing(provider);
}

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
window.confirm = () => true;

const fetchBodies = [];
window.fetch = async (url, options = {}) => {
  const path = String(url).split("?")[0];
  const method = String(options.method || "GET").toUpperCase();
  if (options.body) {
    try {
      fetchBodies.push({ path, method, body: JSON.parse(options.body) });
    } catch {
      fetchBodies.push({ path, method, body: null });
    }
  }
  const tested = path.match(/^\/admin\/api\/providers\/([^/]+)\/test$/);
  if (tested && method === "POST") {
    if (held && held.provider === tested[1]) {
      await new Promise((resolve) => {
        held.release = resolve;
      });
    }
    const result = answers.get(tested[1]) || { ok: true, models: [] };
    // A successful discovery republishes the catalogue, which is what the
    // listing route then reports. `counted` overrides the number so a test can
    // show "the catalogue moved but this provider's own answer did not".
    if (result.ok) {
      listingCounts.set(
        tested[1],
        result.counted === undefined ? result.models.length : result.counted,
      );
    }
    return answer(result);
  }
  // The add/remove routes answer the same listing the page then re-reads. The
  // live `model_count` is the catalogue's, not the mock provider object's.
  if (path === "/admin/api/custom-providers") {
    return answer({
      providers: providers.map((provider) => ({
        ...provider,
        model_count: listingCounts.get(provider.provider_id) ?? 0,
      })),
    });
  }
  const body = Object.prototype.hasOwnProperty.call(ROUTES, path) ? ROUTES[path] : {};
  return answer(body);
};

function answer(body) {
  return {
    ok: true,
    status: 200,
    json: async () => JSON.parse(JSON.stringify(body)),
    text: async () => JSON.stringify(body),
  };
}

const scriptErrors = [];
window.addEventListener("error", (event) => scriptErrors.push(String(event.message)));
try {
  window.eval(script);
} catch (error) {
  await new Promise((resolve) =>
    process.stdout.write(`${JSON.stringify({ fatal: String(error && error.stack) }, null, 2)}\n`, resolve),
  );
  process.exit(0);
}
window.document.dispatchEvent(new window.Event("DOMContentLoaded", { bubbles: true }));
await new Promise((resolve) => setTimeout(resolve, 1000));

const doc = window.document;
const settle = () => new Promise((resolve) => setTimeout(resolve, 120));
const tick = () => new Promise((resolve) => setTimeout(resolve, 80));
const card = (id) => doc.querySelector(`[data-custom-provider="${id}"]`);
const openModal = () => !doc.getElementById("providerDetailModal").hidden;
const bodyHost = () => doc.getElementById("providerDetailBody");

const toProviders = async () => {
  const link = Array.from(doc.querySelectorAll(".nav-link")).find(
    (node) => node.dataset.view === "providers",
  );
  if (link) link.click();
  await settle();
};

await toProviders();

/* THE NINE CONTROLS, and the labels the pre-CARDS-FIX-2 card rendered for
   each. This list is the contract: it is what `customProviderCard` drew on the
   card body before this PR moved it, one entry per control in the spec's §2
   table. A control dropped from the panel shows up as `missing`. */
const CARD_CONTROLS = [
  { id: "delete", label: "Delete", selector: ".cp-delete" },
  { id: "enable-disable", label: "Disable", selector: ".cp-toggle" },
  { id: "edit", label: "Edit", selector: ".cp-edit" },
  { id: "remove", label: "Remove", selector: ".cp-key-remove" },
  { id: "add-key", label: "Add key", selector: ".cp-key-add .key-add-submit" },
  { id: "dialect-save", label: "Save", selector: ".cp-dialect-save" },
  {
    id: "probe-dialect",
    label: "Probe reasoning dialect",
    selector: ".cp-dialect-probe",
  },
  {
    id: "probe-capabilities",
    label: "Probe capabilities",
    selector: ".cp-capability-probe",
  },
  {
    id: "forget-learned",
    label: "Forget everything learned",
    selector: ".cp-forget-learned",
  },
];

const payload = {
  fatal: null,
  scriptErrors,
  consoleErrors,
  controls: CARD_CONTROLS,
};

/* 1. THE FACE. Three tight rows and nothing else, on every custom card. */
payload.face = Array.from(
  doc.querySelectorAll("#customProviderGrid .provider-card"),
).map((node) => ({
  provider: node.dataset.customProvider,
  className: node.className,
  role: node.getAttribute("role"),
  tabIndex: node.tabIndex,
  ariaHaspopup: node.getAttribute("aria-haspopup"),
  children: Array.from(node.children).map(
    (child) => child.className.split(" ")[0],
  ),
  controls: Array.from(node.querySelectorAll("button")).map((b) => b.textContent),
  dataKeys: node.querySelectorAll("[data-key]").length,
  detect: node.querySelector(".pv-detect")?.textContent ?? null,
  pill: node.querySelector(".status-pill")?.textContent ?? null,
  pillClass: node.querySelector(".status-pill")?.className ?? null,
  // The three regions and the nine controls must NOT be on the card.
  hasDetails: Boolean(node.querySelector(".cp-details")),
  hasDialect: Boolean(node.querySelector(".cp-dialect")),
  hasKeyList: Boolean(node.querySelector(".key-manager-list")),
  hasRailHost: node.classList.contains("has-key-rail"),
  configured: node.dataset.pvConfigured,
  search: node.dataset.pvSearch,
}));

/* 2. THE PANEL'S CONTENT -- the completeness proof. Opened the way a reader
   opens it (a press on the card), then read field by field. */
const probe = card("custom_probe");
probe.click();
await settle();
await tick();

const field = (selector) => bodyHost().querySelector(selector);

payload.panel = {
  opened: openModal(),
  title: doc.getElementById("providerDetailTitle").textContent,
  subtitle: doc.getElementById("providerDetailSubtitle").textContent,
  role: doc.getElementById("providerDetailModal").getAttribute("role"),
  ariaModal: doc.getElementById("providerDetailModal").getAttribute("aria-modal"),
  ariaLabelledBy: doc
    .getElementById("providerDetailModal")
    .getAttribute("aria-labelledby"),
  // The three regions, in the order the card drew them.
  regions: Array.from(bodyHost().querySelector('.pv-detail').children).map(
    (node) => node.className.split(" ")[0],
  ),
  baseUrl: field(".cp-base-url")?.textContent ?? null,
  details: field(".cp-details")?.textContent ?? null,
  dialectLabel: field(".cp-dialect-label")?.textContent ?? null,
  dialectValue: field(".cp-dialect-edit")?.value ?? null,
  dialectPlaceholder: field(".cp-dialect-edit")?.placeholder ?? null,
  // The masked key list, in order, and one Remove per key.
  keyLabels: Array.from(bodyHost().querySelectorAll(".cp-key-label")).map(
    (node) => node.textContent,
  ),
  removeCount: bodyHost().querySelectorAll(".cp-key-remove").length,
  addPlaceholder: field(".cp-key-add .key-add-secret")?.placeholder ?? null,
  addShape: Array.from(field(".cp-key-add")?.children || []).map((node) =>
    [node.tagName, node.getAttribute("type") || "", node.className].join("|"),
  ),
  // Every control the card carried, by the label it rendered then.
  controls: CARD_CONTROLS.map((control) => ({
    id: control.id,
    expected: control.label,
    found: Array.from(bodyHost().querySelectorAll(control.selector)).map(
      (node) => node.textContent.trim(),
    ),
  })),
  // Ordered list of every button in the body, so position is assertable.
  buttonOrder: Array.from(bodyHost().querySelectorAll("button")).map((node) => ({
    label: node.textContent.trim(),
    className: node.className,
  })),
  // The one setting-control question that matters on a custom card: nothing in
  // here is a `[data-key]`, and the page's Apply must not see a second copy.
  dataKeyControls: bodyHost().querySelectorAll("[data-key]").length,
};
payload.panel.missing = payload.panel.controls
  .filter((control) => control.found.length === 0)
  .map((control) => control.id);
payload.panel.wrongLabel = payload.panel.controls
  .filter(
    (control) =>
      control.found.length > 0 && !control.found.every((text) => text === control.expected),
  )
  .map((control) => ({ id: control.id, found: control.found }));

/* 3. DELETE AND DISABLE ARE NOT NEAR THE PRIMARY POSITION.
 *
 * Measured, not eyeballed: `buttonIndex` is each control's place in the panel
 * body's own button order. The first button is the panel's top-left position
 * -- the thing a reader's hand reaches for first -- and the last is the
 * furthest from it. Delete must be last, Disable second-to-last, and both must
 * be at least `SAFE_DISTANCE` positions away from the first control. */
const SAFE_DISTANCE = 4;
{
  const order = payload.panel.buttonOrder.map((entry) => entry.label);
  const at = (label) => order.lastIndexOf(label);
  payload.danger = {
    order,
    deleteIndex: at("Delete"),
    toggleIndex: at("Disable"),
    editIndex: at("Edit"),
    firstIndex: 0,
    lastIndex: order.length - 1,
    deleteIsLast: at("Delete") === order.length - 1,
    // Distance from the panel's PRIMARY position: the first control in the
    // body, which is where a reader's press lands first.
    deleteDistance: at("Delete") - 0,
    toggleDistance: at("Disable") - 0,
    // And from Edit, the other half of the same action row.
    deleteToToggle: at("Delete") - at("Disable"),
    safe: at("Delete") > SAFE_DISTANCE && at("Disable") > SAFE_DISTANCE,
    // Nothing destructive may be the first thing in the body.
    firstIsDestructive:
      order[0] === "Delete" ||
      order[0] === "Disable" ||
      order[0] === "Forget everything learned",
  };
}

/* 4. THE PANEL IS THE ONLY PLACE THE FORM LIVES. A `[data-key]` control left
 * on the card behind `display: none` is a second answer Apply would post. */
payload.formIsOnlyInThePanel = {
  onTheCards: Array.from(
    doc.querySelectorAll("#customProviderGrid [data-key]"),
  ).length,
  inThePanel: bodyHost().querySelectorAll("[data-key]").length,
};

/* 5. KEYBOARD AND FOCUS -- the same contract a built-in card's panel has. */
{
  doc.getElementById("providerDetailClose").click();
  await tick();
  payload.keyboard = {
    closed: !openModal(),
    bodyCleared: bodyHost().textContent === "",
    focusReturned: doc.activeElement === card("custom_probe"),
  };
  // Enter and Space both open it, on a focusable card.
  const target = card("custom_probe");
  target.focus();
  target.dispatchEvent(
    new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }),
  );
  await tick();
  payload.keyboard.enterOpened = openModal();
  // Escape goes on the CARD this time: the document-level kit listener has
  // already answered the previous one, and dispatching at the document again
  // would be answered by a second listener that never saw this dialog open.
  target.focus();
  doc.dispatchEvent(
    new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
  );
  await tick();
  payload.keyboard.enterThenEscape = !openModal();
  target.dispatchEvent(
    new window.KeyboardEvent("keydown", { key: " ", bubbles: true }),
  );
  await tick();
  payload.keyboard.spaceOpened = openModal();
  // Escape dispatched ON the document, which is where `dialogKit` listens; a
  // key dispatched on a card inside the modal would bubble to the same
  // listener, and that is the case the kit was written for.
  doc.dispatchEvent(
    new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
  );
  await tick();
  payload.keyboard.escapeClosed = !openModal();
  // A control on the face acts, and does not also open the panel. Refresh
  // models and Detect models are the same call with a different busy word, so
  // the proof reads the word while the request is held open.
  held = { provider: "custom_probe" };
  card("custom_probe").querySelector(".pv-detect").click();
  await tick();
  payload.keyboard.detectBusyWord =
    card("custom_probe").querySelector(".pv-detect").textContent;
  if (held && held.release) held.release();
  await tick();
  held = null;
  payload.keyboard.detectOpenedThePanel = openModal();
  held = { provider: "custom_probe" };
  card("custom_probe").querySelector(".pv-actions .test-button").click();
  await tick();
  payload.keyboard.refreshBusyWord = card("custom_probe").querySelector(
    ".pv-actions .test-button",
  ).textContent;
  if (held && held.release) held.release();
  await tick();
  held = null;
  payload.keyboard.refreshOpenedThePanel = openModal();
}

/* 6. THE MODEL COUNT TRACKS A REFRESH, from ITS source (`provider.model_count`
 * in the listing payload) -- and the three states stay three. */
{
  const detect = () => card("custom_probe").querySelector(".pv-detect");
  const quietDetect = () => card("custom_quiet").querySelector(".pv-detect");
  payload.count = {
    // Read off the FIRST paint, before any control on the card was pressed --
    // section 1's reading. Anything read here has already been refreshed.
    asDrawn: payload.face[0].detect,
    zeroAsDrawn: payload.face[1].detect,
    // Where the number came from: the listing, not the shared model list.
    source: window.eval("customProviderCountLabel")({
      provider_id: "custom_probe",
      model_count: 44,
    }),
  };
  // A refresh answers three models where the listing's first load said none.
  answers.set("custom_probe", { ok: true, models: ["m1", "m2", "m3"] });
  detect().click();
  await tick();
  await settle();
  payload.count.afterFewer = detect().textContent;
  payload.count.pillAfterRefresh = card("custom_probe").querySelector(
    ".status-pill",
  ).textContent;
  payload.count.pillIsANumber = /^\d/.test(payload.count.pillAfterRefresh);
  // And more of them, from a second load of the same path.
  answers.set("custom_probe", {
    ok: true,
    models: ["m1", "m2", "m3", "m4", "m5", "m6"],
  });
  detect().click();
  await tick();
  await settle();
  payload.count.afterMore = detect().textContent;
  // A provider that answers NONE reads differently from one nobody asked.
  answers.set("custom_quiet", { ok: true, models: [] });
  quietDetect().click();
  await tick();
  await settle();
  payload.count.zeroAfterAnswer = quietDetect().textContent;
  payload.count.states = {
    counted: payload.count.afterMore,
    zero: payload.count.zeroAfterAnswer,
    neverDetected: payload.count.asDrawn,
  };
  // THE BUSY STATE IS NEVER STOMPED. `updateCustomProviderCard` is the one
  // repaint path this card has, and the control disables itself for the length
  // of its own call; a repaint landing in the middle of that must leave
  // "Detecting" where it is. The count underneath is moved so a stomp would be
  // visible: a repaint that read the same number the button already shows
  // would pass vacuously.
  held = { provider: "custom_probe" };
  detect().click();
  await tick();
  const whileBusy = {
    text: detect().textContent,
    disabled: detect().disabled,
  };
  // Exactly what a loader repaint does, while the call is still open.
  window.eval("updateCustomProviderCard")("custom_probe", "reachable", "Reachable", "", 9);
  const duringARepaint = detect().textContent;
  if (held && held.release) held.release();
  await tick();
  held = null;
  payload.count.busy = {
    whileBusy,
    duringARepaint,
    afterTheCall: { text: detect().textContent, disabled: detect().disabled },
  };
}

/* 7. THE COUNT IS REPAINTED IN THE PANEL TOO, from the same one source. */
{
  card("custom_probe").click();
  await settle();
  const before = bodyHost().querySelector(".cp-details").textContent;
  answers.set("custom_probe", { ok: true, models: ["only-one"] });
  card("custom_probe").querySelector(".pv-detect").click();
  await new Promise((resolve) => setTimeout(resolve, 200));
  await settle();
  // The panel is rebuilt by `renderCustomProviders` -> `refreshProviderOverlay`
  // after the listing reload, so the line is a fresh one, not a stale node.
  const after = bodyHost().querySelector(".cp-details")?.textContent ?? null;
  payload.panelRepaint = {
    opened: openModal(),
    before,
    after,
    subtitle: doc.getElementById("providerDetailSubtitle").textContent,
    // The same count on every surface that shows it, in one tick.
    face: card("custom_probe").querySelector(".pv-detect").textContent,
    faceSaysThePanel: Boolean(
      after && card("custom_probe").querySelector(".pv-detect").textContent === "1 model",
    ),
  };
  doc.dispatchEvent(
    new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
  );
  await tick();
}

/* 8. A RE-RENDER WHILE THE PANEL IS OPEN keeps it open and correct. This is
 * where these bugs actually live: `renderSections` and `loadCustomProviders`
 * both rebuild the grid under the dialog. */
{
  card("custom_probe").click();
  await settle();
  const titleBefore = doc.getElementById("providerDetailTitle").textContent;
  const removeBefore = bodyHost().querySelectorAll(".cp-key-remove").length;
  window.eval("renderSections")([{ id: "providers", label: "Providers", description: "", advanced: false }], []);
  await settle();
  const afterSections = {
    stillOpen: openModal(),
    title: doc.getElementById("providerDetailTitle").textContent,
    removeCount: bodyHost().querySelectorAll(".cp-key-remove").length,
    details: bodyHost().querySelector(".cp-details")?.textContent ?? null,
  };
  await window.eval("loadCustomProviders")();
  await settle();
  payload.reRead = {
    titleBefore,
    removeBefore,
    afterSections,
    afterListing: {
      stillOpen: openModal(),
      title: doc.getElementById("providerDetailTitle").textContent,
      removeCount: bodyHost().querySelectorAll(".cp-key-remove").length,
      // A dialog holding two copies of one control would post whichever Apply
      // walked to; one body, one of each.
      deleteCount: bodyHost().querySelectorAll(".cp-delete").length,
    },
  };
  doc.dispatchEvent(
    new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
  );
  await tick();
}

/* 9. A DELETED PROVIDER TAKES ITS PANEL WITH IT (and nothing else). */
{
  const doomed = card("custom_quiet");
  doomed.click();
  await settle();
  const openBefore = openModal();
  providers.pop();
  await window.eval("loadCustomProviders")();
  await settle();
  payload.deletedProvider = {
    openBefore,
    openAfter: openModal(),
    cardGone: card("custom_quiet") === null,
  };
}

/* Exit only once the answer has left the process (see admin_jsdom_harness.mjs:
   a pipe to the test is asynchronous on Linux, and the page's poll timers are
   what keep the process alive, so the explicit exit is required). */
await new Promise((resolve) =>
  process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`, resolve),
);
process.exit(0);
