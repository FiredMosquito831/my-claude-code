/**
 * CARDS-FIX-1: the model count on a provider card's face.
 *
 *   node admin_jsdom_model_count.mjs <admin_static_dir>
 *   -> one JSON object on stdout
 *
 * USER 2026-11-04: "The card always shows no models detected even after models
 * are detected nad it shows with green in the corner (we want to permanently
 * show how many models are detected and refresh when the refreshes happen)."
 *
 * This harness exists as its own file, and its own page load, for one reason:
 * the defect was an ORDER, not a render. `renderSections` draws every provider
 * card at `admin.js:822` and `hydrateModelOptions` loads the model list at
 * `:833`, so the card is built from an empty list and the label has to be
 * repainted when the list arrives. A scenario that renders a card directly
 * (as the UX pass does) cannot see that, because it renders it after the list
 * is already in state. So this page is loaded the way a browser loads it, from
 * `/admin/api/config` and `/admin/api/models`, and every reading below is taken
 * from the cards that load drew.
 *
 * It is also small on purpose: the mutation controls in
 * `test_admin_cards_overlay_jsdom.py` run it against copies of `admin.js` with
 * one line removed, and each of those runs costs this file and nothing else
 * (the full UX harness is ~2.5 minutes; this is a few seconds).
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { JSDOM, VirtualConsole } from "jsdom";

const dir = process.argv[2];
if (!dir) {
  console.error("usage: admin_jsdom_model_count.mjs <admin_static_dir>");
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

/* Three providers, one per state. Each needs at least one field or
   `renderProviderGroups` omits it from the grid entirely. */
const providerIds = ["mc_cat", "mc_empty", "mc_silent"];
const fields = providerIds.map((id) => ({
  key: `${id.toUpperCase()}_API_KEY_PROXY`,
  label: `${id} proxy`,
  section: "providers",
  provider: id,
  type: "text",
  value: "",
  default: "",
  configured: false,
  secret: false,
  advanced: true,
  description: "",
  options: [],
}));

const provider = (id, name) => ({
  provider_id: id,
  display_name: name,
  group: "cloud",
  kind: "remote",
  status: "configured",
  label: "Configured",
  key_count: 1,
  credential_env: "",
  credential_owner_id: "",
  credential_owner_name: "",
  credential_shared_with: [],
});

/* The catalogue: three refs for `mc_cat` and NONE for the other two, which is
   the state every card used to render as "No models detected". */
let catalogue = ["mc_cat/one", "mc_cat/two", "mc_cat/three"];
/* The answer the provider-scoped test route gives, per provider, and an
   optional gate that holds it open so the busy state can be observed. */
const answers = new Map();
let held = null;

const ROUTES = {
  "/admin/api/config": {
    sections: [{ id: "providers", label: "Providers", description: "", advanced: false }],
    provider_groups: [],
    fields,
    provider_status: [
      provider("mc_cat", "Catalogue Provider"),
      provider("mc_empty", "Empty Provider"),
      provider("mc_silent", "Silent Provider"),
    ],
    paths: { managed: "/tmp/mc/.env", repo: "", explicit: null },
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
  "/admin/api/requests": { rows: [], total: 0 },
  "/admin/api/requests/pulse": { enabled: false, total: 0, latest: null },
  "/admin/api/docs": { documents: [] },
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
window.fetch = async (url, options = {}) => {
  const path = String(url).split("?")[0];
  const method = String(options.method || "GET").toUpperCase();
  // The catalogue. `refresh` is the POST the Models page's own refresh makes,
  // and it answers from the same variable, so a test that changes the list
  // changes what the next load sees -- which is what "refresh when the
  // refreshes happen" means.
  if (path === "/admin/api/models" || path === "/admin/api/models/refresh") {
    return answer({
      models: catalogue,
      failed_providers: [],
      blind_models: [],
      kinds: {},
      kind_labels: {},
    });
  }
  const tested = path.match(/^\/admin\/api\/providers\/([^/]+)\/test$/);
  if (tested && method === "POST") {
    if (held && held.provider === tested[1]) {
      await new Promise((resolve) => {
        held.release = resolve;
      });
    }
    const result = answers.get(tested[1]) || { ok: true, models: [] };
    return answer(result);
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
/* The page's own load: `renderSections` draws the cards and `hydrateModelOptions`
   fills the list afterwards. Both have to have finished. */
await new Promise((resolve) => setTimeout(resolve, 1200));

const doc = window.document;
const page = window.eval("uxPassHarness")();
const { state } = page;
const tick = () => new Promise((resolve) => setTimeout(resolve, 80));

const card = (id) => doc.querySelector(`.pv-card[data-provider="${id}"]`);
const detect = (id) => card(id)?.querySelector(".pv-detect");
const pill = (id) => card(id)?.querySelector(".status-pill");
const shown = (id) => (detect(id) ? detect(id).textContent : null);
const computed = (id) => window.eval("providerModelCountLabel")(id);

const payload = {
  fatal: null,
  scriptErrors,
  consoleErrors,
  /* The defect itself, measured on the load the browser performs: the card is
     drawn before the list exists, so only a repaint can make this read "3". */
  afterThePageLoads: {
    counted: shown("mc_cat"),
    zero: shown("mc_empty"),
    neverDetected: shown("mc_silent"),
  },
};

/* 1. THREE STATES, THREE SENTENCES. A provider with catalogue refs, one that
   has answered "none", and one nobody has asked. */
answers.set("mc_empty", { ok: true, models: [] });
detect("mc_empty").click();
await tick();
payload.threeStates = {
  counted: shown("mc_cat"),
  zero: shown("mc_empty"),
  neverDetected: shown("mc_silent"),
};
payload.labels = {
  counted: computed("mc_cat"),
  zero: computed("mc_empty"),
  neverDetected: computed("mc_silent"),
};

/* 2. A REFRESH CHANGES THE COUNT. Two loads of the product's own refresh path,
   in both directions, because "it is non-empty" is not the claim the user
   made: they asked for it to follow the refreshes. */
catalogue = ["mc_cat/one"];
await window.eval("loadModelOptions")(true);
const afterFewer = shown("mc_cat");
catalogue = ["mc_cat/one", "mc_cat/two", "mc_cat/four", "mc_cat/five"];
await window.eval("loadModelOptions")(true);
payload.refresh = {
  before: "3 models",
  afterFewer,
  afterMore: shown("mc_cat"),
};

/* 3. THE PILL IS A STATUS. Pressing the card's own Refresh models is what the
   user pressed; it must leave a status in the pill and the count on the
   control, from one answer, in the same tick. */
catalogue = [];
await window.eval("loadModelOptions")(true);
answers.set("mc_cat", { ok: true, models: ["m1", "m2", "m3", "m4"] });
card("mc_cat").querySelector(".pv-actions .test-button").click();
await tick();
payload.afterRefresh = {
  pill: pill("mc_cat").textContent,
  pillClass: pill("mc_cat").className,
  detect: shown("mc_cat"),
  title: detect("mc_cat").title,
};

/* 4. THE TWO SURFACES AGREE, in the same tick, after each kind of loader. Both
   are reads of the same list; the bug was one of them being assigned once. */
const agreement = (loader) => ({
  loader,
  pill: pill("mc_cat").textContent,
  label: shown("mc_cat"),
  computed: computed("mc_cat"),
  pillIsANumber: /^\d/.test(pill("mc_cat").textContent),
});
payload.ticks = [];
catalogue = ["mc_cat/one", "mc_cat/two"];
await window.eval("loadModelOptions")();
payload.ticks.push(agreement("loadModelOptions: 2"));
catalogue = ["mc_cat/one", "mc_cat/two", "mc_cat/three", "mc_cat/four", "mc_cat/five", "mc_cat/six"];
await window.eval("loadModelOptions")(true);
payload.ticks.push(agreement("loadModelOptions(refresh): 6"));
answers.set("mc_silent", { ok: true, models: ["only"] });
await window.eval("testProvider")("mc_silent", card("mc_silent").querySelector(".pv-actions .test-button"));
payload.ticks.push(agreement("testProvider on a different card"));

/* 5. THE BUSY STATE IS NEVER STOMPED. The Detect control disables itself and
   reads "Detecting" for the length of its own call; a load landing in the
   middle of it -- a poll tick, the Models page's own refresh -- must not put a
   number there. The answer is held open so the middle is observable. */
held = { provider: "mc_silent" };
detect("mc_silent").click();
await tick();
const whileBusy = { text: shown("mc_silent"), disabled: detect("mc_silent").disabled };
catalogue = ["mc_cat/one", "mc_cat/two", "mc_silent/only"];
await window.eval("loadModelOptions")();
const duringALoad = shown("mc_silent");
if (held && held.release) held.release();
await tick();
payload.busy = {
  whileBusy,
  duringALoad,
  afterTheCall: { text: shown("mc_silent"), disabled: detect("mc_silent").disabled },
};
held = null;

/* 6. EVERY CARD SHOWS WHAT THE SHARED LIST COMPUTES. The property that makes
   the number mean anything: it is a reading, not a second copy. `mc_silent`
   answered one model above, so its ref is in this list too; the card that has
   answered nothing at all is covered by
   `afterThePageLoads.neverDetected`, on the load the browser performs. */
catalogue = ["mc_cat/one", "mc_cat/two", "mc_cat/three", "mc_silent/only"];
await window.eval("loadModelOptions")();
payload.everyCardMatchesTheList = providerIds.map((id) => ({
  provider: id,
  shown: shown(id),
  computed: computed(id),
  refs: window.eval("providerModelCount")(id),
}));

/* Exit only once the answer has left the process (see admin_jsdom_harness.mjs:
   a pipe to the test is asynchronous on Linux). The page's poll timers are
   what keep the process alive, so the explicit exit is required. */
await new Promise((resolve) =>
  process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`, resolve),
);
process.exit(0);
