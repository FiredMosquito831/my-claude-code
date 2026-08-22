/**
 * Run the real admin.js against a real payload in jsdom, and report what
 * rendered.
 *
 * There is no browser here and no layout engine: this proves the script
 * evaluates, the views wire up, and each view renders the number of sections
 * it claims. It proves nothing about spacing, overflow, contrast, breakpoints
 * or anything else that needs boxes to have sizes.
 *
 * Usage: node admin_jsdom_harness.mjs <admin_static_dir>
 * Prints one JSON object on stdout.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { JSDOM, VirtualConsole } from "jsdom";

const dir = process.argv[2];
if (!dir) {
  console.error("usage: admin_jsdom_harness.mjs <admin_static_dir>");
  process.exit(2);
}

const html = readFileSync(join(dir, "index.html"), "utf8");
const script = readFileSync(join(dir, "admin.js"), "utf8");

/* ------------------------------------------------------------------ payload
   Shaped like the real API, including the awkward cases: a provider that
   reports no cache figures, a locally answered family, a rule that has never
   fired, and RTK absent. */
const FIELDS = [
  {
    key: "ENABLE_TOOL_RESULT_TRIMMING",
    label: "Trim large tool results",
    section: "optimizer",
    type: "boolean",
    value: "false",
    default: "false",
    description: "Master switch.",
  },
  ...["READ", "GREP", "GLOB"].map((tool) => ({
    key: `TOOL_RESULT_TRIM_${tool}`,
    label: `${tool} results`,
    section: "optimizer",
    type: "select",
    value: "off",
    default: "off",
    options: [
      { value: "off", label: "Off" },
      { value: "observe", label: "Observe" },
      { value: "on", label: "On" },
    ],
  })),
  ...[
    ["TOOL_RESULT_TRIM_THRESHOLD_CHARS", "20000"],
    ["TOOL_RESULT_TRIM_KEEP_HEAD_CHARS", "4000"],
    ["TOOL_RESULT_TRIM_KEEP_TAIL_CHARS", "4000"],
    ["TOOL_RESULT_TRIM_PROTECT_RECENT_RESULTS", "2"],
  ].map(([key, value]) => ({
    key,
    label: key,
    section: "optimizer",
    type: "number",
    value,
    default: value,
  })),
  ...["ENABLE_TITLE_GENERATION_SKIP", "ENABLE_SUGGESTION_MODE_SKIP"].map((key) => ({
    key,
    label: key,
    section: "optimizer",
    type: "boolean",
    value: "true",
    default: "true",
  })),
  {
    key: "REQUEST_LOG_MAX_ROWS",
    label: "Retained rows",
    section: "limits",
    type: "number",
    value: "200000",
    default: "200000",
  },
  {
    key: "MODEL",
    label: "Default Model",
    section: "providers",
    type: "text",
    value: "p1/m1",
    default: "",
  },
];

const SECTIONS = [
  { id: "providers", label: "Providers", description: "" },
  { id: "optimizer", label: "Tool-result trimming", description: "" },
  { id: "limits", label: "Limits", description: "" },
];

const daily = (n) =>
  Array.from({ length: n }, (_, i) => ({
    bucket: `2026-08-${String(i + 1).padStart(2, "0")}`,
    requests: 10 + i,
    tokens_saved: (10 + i) * 100,
  }));

const ROUTES = {
  "/admin/api/config": {
    fields: FIELDS,
    sections: SECTIONS,
    provider_status: [],
    paths: { managed: "/tmp/.env" },
  },
  "/admin/api/onboarding": { dismissed: true, complete: true, steps: [] },
  "/admin/api/providers/local-status": { providers: [] },
  "/admin/api/config/validate": { valid: true, errors: [] },
  "/admin/api/version": { current: "5.47.1" },
  "/admin/api/desktop": { available: false },
  "/admin/api/rtk": { installed: false, claude: false, codex: false, pi: false },
  "/admin/api/rtk/gain": {
    available: false,
    reason: "not_installed",
    detail: "No RTK binary was found.",
  },
  "/admin/api/claude/settings": { configured: false },
  "/admin/api/claude/config": { entries: [], values: {}, path: "", parsed: true },
  "/admin/api/providers/custom": { providers: [] },
  "/admin/api/models": { models: [] },
  "/admin/api/requests/optimization-stats": {
    enabled: true,
    series_days: 14,
    total_requests: 157906,
    answered_locally: 3252,
    tokens_saved: 15300000,
    window: { since: null, until: null },
    rules: [
      {
        rule: "title_generation_skip",
        label: "Title generation skip",
        description: "Claude Code asking a model to name your session.",
        answer: "Conversation",
        env_key: "ENABLE_TITLE_GENERATION_SKIP",
        enabled: true,
        requests: 3122,
        tokens_saved: 14300000,
        tokens_reported: 3122,
        daily: daily(14),
      },
      {
        rule: "suggestion_mode_skip",
        label: "Suggestion mode skip",
        description: "The suggested next message Claude Code offers you.",
        answer: "",
        env_key: "ENABLE_SUGGESTION_MODE_SKIP",
        enabled: true,
        // Registered, never fired: a real zero count, an unknown saving.
        requests: 0,
        tokens_saved: null,
        tokens_reported: 0,
        daily: [],
      },
    ],
  },
  "/admin/api/requests/stats": {
    enabled: true,
    total: 157906,
    by_provider: [
      {
        key: "nous_portal",
        requests: 106434,
        tokens_in: 3900,
        cache_read_tokens: 96100,
        cache_write_tokens: 0,
        cache_reported: 106434,
        errors: 0,
      },
      {
        key: "chatgpt_oauth",
        requests: 9736,
        tokens_in: 5000,
        cache_read_tokens: 0,
        cache_write_tokens: 0,
        // Reports nothing: must render an em dash, never 0.0%.
        cache_reported: 0,
        errors: 0,
      },
      {
        key: "local:title_generation_skip",
        requests: 3122,
        tokens_in: 0,
        cache_read_tokens: 0,
        cache_write_tokens: 0,
        cache_reported: 0,
        errors: 0,
      },
    ],
    by_model: [],
    by_key: [],
    series: [],
    top_errors: [],
    fallback_routes: [],
    diverted_routes: [],
    coverage: {},
  },
  /* The Docs page. The html here is what the *server* produced -- the point
     of the assertions below is that the page places it and wires the two
     link lists beside it, never that it parsed anything. */
  "/admin/api/docs": {
    documents: [
      {
        slug: "readme",
        title: "README",
        summary: "What MCC is.",
        github_url: "https://github.com/FiredMosquito831/my-claude-code/blob/main/README.md",
      },
      {
        slug: "usage",
        title: "Usage",
        summary: "Running the server.",
        github_url: "https://github.com/FiredMosquito831/my-claude-code/blob/main/docs/USAGE.md",
      },
    ],
  },
  "/admin/api/docs/readme": {
    slug: "readme",
    title: "README",
    summary: "What MCC is.",
    github_url: "https://github.com/FiredMosquito831/my-claude-code/blob/main/README.md",
    html:
      '<h2 id="install">Install</h2><p>Text.</p>' +
      '<h3 id="windows">Windows</h3>' +
      '<table class="guide-table"><tbody><tr><td>1</td></tr></tbody></table>' +
      '<a href="#doc-usage">see usage</a>' +
      '<pre><code>long line</code></pre>',
    headings: [
      { anchor: "install", text: "Install", level: 2 },
      { anchor: "windows", text: "Windows", level: 3 },
    ],
  },
  "/admin/api/requests": { rows: [], total: 0 },
  "/admin/api/requests/lifetime": { enabled: true, by_model: [], by_provider: [] },
  "/admin/api/requests/pulse": { enabled: true, total: 0, latest: null },
  "/admin/api/websearch/analytics/stats": { enabled: false },
  "/admin/api/websearch/analytics": { enabled: false, rows: [], total: 0 },
};

/* A fresh install: logging never turned on, no requests, no RTK. The page has
   to say so rather than print a wall of zeros. Selected with EMPTY=1. */
if (process.env.EMPTY === "1") {
  ROUTES["/admin/api/requests/optimization-stats"] = {
    enabled: false,
    rules: [
      {
        rule: "title_generation_skip",
        label: "Title generation skip",
        description: "Claude Code asking a model to name your session.",
        answer: "Conversation",
        env_key: "ENABLE_TITLE_GENERATION_SKIP",
        enabled: true,
      },
      {
        rule: "suggestion_mode_skip",
        label: "Suggestion mode skip",
        description: "The suggested next message Claude Code offers you.",
        answer: "",
        env_key: "ENABLE_SUGGESTION_MODE_SKIP",
        enabled: true,
      },
    ],
  };
  ROUTES["/admin/api/requests/stats"] = { enabled: false };
  ROUTES["/admin/api/rtk"] = { installed: false };
  ROUTES["/admin/api/rtk/gain"] = {
    available: false,
    reason: "not_installed",
    detail: "No RTK binary was found.",
  };
}

function routeFor(url) {
  const path = String(url).split("?")[0];
  if (Object.prototype.hasOwnProperty.call(ROUTES, path)) return ROUTES[path];
  return {};
}

const virtualConsole = new VirtualConsole();
const consoleErrors = [];
virtualConsole.on("jsdomError", (error) => consoleErrors.push(String(error.message)));
virtualConsole.on("error", (...args) => consoleErrors.push(args.map(String).join(" ")));

const dom = new JSDOM(html, {
  url: "http://127.0.0.1:8080/admin",
  runScripts: "outside-only",
  pretendToBeVisual: true,
  virtualConsole,
});
const { window } = dom;

// Mandatory stubs. The two Observers are not optional -- the script constructs
// them at eval time and jsdom does not provide them.
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
window.MutationObserver = window.MutationObserver || NoopObserver;
window.scrollTo = () => {};
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
if (!window.requestAnimationFrame) {
  window.requestAnimationFrame = (fn) => window.setTimeout(() => fn(Date.now()), 0);
  window.cancelAnimationFrame = (id) => window.clearTimeout(id);
}
const fetchCalls = [];
window.fetch = async (url, options = {}) => {
  fetchCalls.push(String(url).split("?")[0]);
  const body = routeFor(url);
  return {
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
};

const scriptErrors = [];
window.addEventListener("error", (event) => scriptErrors.push(String(event.message)));
window.addEventListener("unhandledrejection", (event) =>
  scriptErrors.push(String(event.reason)),
);

try {
  window.eval(script);
} catch (error) {
  console.log(
    JSON.stringify({ fatal: `eval threw: ${error && error.stack}` }, null, 2),
  );
  process.exit(0);
}

window.document.dispatchEvent(
  new window.Event("DOMContentLoaded", { bubbles: true }),
);

await new Promise((resolve) => setTimeout(resolve, 900));

const doc = window.document;
const navLinks = Array.from(doc.querySelectorAll(".nav-link"));
const views = {};
for (const link of navLinks) {
  link.click();
  await new Promise((resolve) => setTimeout(resolve, 120));
  const id = link.dataset.view;
  const view = doc.querySelector(`.admin-view[data-view="${id}"]`);
  views[id] = {
    label: link.textContent,
    exists: Boolean(view),
    hidden: view ? Boolean(view.hidden) : null,
    sections: view ? view.querySelectorAll(".settings-section").length : 0,
    fieldInputs: view ? view.querySelectorAll("[data-key]").length : 0,
    text: view ? (view.textContent || "").replace(/\s+/g, " ").trim().length : 0,
  };
}

const docsView = doc.querySelector('.admin-view[data-view="docs"]');
const docs = docsView
  ? {
      present: true,
      docLinks: Array.from(docsView.querySelectorAll("#docsList a")).map((a) =>
        a.textContent.trim(),
      ),
      currentDoc: (docsView.querySelector("#docsList a[aria-current]") || {})
        .textContent,
      headingLinks: Array.from(docsView.querySelectorAll("#docsHeadings a")).map(
        (a) => `${a.className}:${a.textContent.trim()}`,
      ),
      title: (doc.getElementById("docsTitle") || {}).textContent || "",
      githubHref: (doc.getElementById("docsGithub") || {}).getAttribute
        ? doc.getElementById("docsGithub").getAttribute("href")
        : null,
      statusHidden: (doc.getElementById("docsStatus") || {}).hidden,
      // Every table must sit in its own scroll box or a wide one pushes the
      // whole page sideways.
      tables: docsView.querySelectorAll("#docsContent table").length,
      scrollBoxes: docsView.querySelectorAll("#docsContent .docs-scroll").length,
      unwrappedTables: docsView.querySelectorAll(
        "#docsContent > table, #docsContent > * > table:not(.docs-scroll > table)",
      ).length,
      anchorIds: Array.from(docsView.querySelectorAll("#docsContent [id]")).map(
        (el) => el.id,
      ),
      crossLinks: docsView.querySelectorAll('#docsContent a[href^="#doc-"]').length,
      contentLength: (docsView.querySelector("#docsContent") || { textContent: "" })
        .textContent.length,
    }
  : { present: false };

const optimizer = doc.querySelector('.admin-view[data-view="optimizer"]');
// `> table >` matters: the per-rule <details> holds a nested table, and an
// unscoped selector reports its rows as extra rule rows.
const rowsOf = (id) =>
  optimizer
    ? Array.from(
        optimizer.querySelectorAll(`#${id} > table > tbody > tr`),
      ).map((tr) => Array.from(tr.children).map((td) => td.textContent.trim().slice(0, 70)))
    : [];
const cacheRows = rowsOf("optCache");
const ruleRows = rowsOf("optRules");

// Snapshot the KPIs BEFORE anything is clicked: they describe the state the
// page loaded in, and a toggle below deliberately changes that state.
const kpiText = optimizer
  ? Array.from(optimizer.querySelectorAll(".opt-kpi")).map((kpi) =>
      (kpi.textContent || "").replace(/\s+/g, " ").trim(),
    )
  : [];
const segDisabledWhileMasterOff = optimizer
  ? Array.from(optimizer.querySelectorAll("[data-opt-seg] .opt-seg button")).every(
      (button) => button.disabled,
    )
  : null;

// The dirty counter must count settings, not widgets: the visible switch and
// the hidden manifest input are one setting.
let dirtyAfterToggle = null;
const masterButton = optimizer ? optimizer.querySelector(".opt-switch") : null;
if (masterButton) {
  masterButton.click();
  await new Promise((resolve) => setTimeout(resolve, 60));
  dirtyAfterToggle = (doc.getElementById("dirtyState") || {}).textContent || null;
}

console.log(
  JSON.stringify(
    {
      fatal: null,
      scriptErrors,
      consoleErrors,
      navLabels: navLinks.map((link) => link.textContent),
      views,
      docs,
      optimizer: {
        present: Boolean(optimizer),
        kpis: optimizer ? optimizer.querySelectorAll(".opt-kpi").length : 0,
        kpiText,
        segDisabledWhileMasterOff,
        ruleRows,
        cacheRows,
        sparklines: optimizer ? optimizer.querySelectorAll(".opt-spark").length : 0,
        dataTables: optimizer ? optimizer.querySelectorAll("details.opt-data").length : 0,
        segControls: optimizer ? optimizer.querySelectorAll(".opt-seg").length : 0,
        warning: optimizer
          ? (optimizer.querySelector(".opt-note")?.textContent || "")
              .replace(/\s+/g, " ")
              .trim()
          : "",
        dirtyAfterToggle,
      },
      fetched: Array.from(new Set(fetchCalls)).sort(),
    },
    null,
    2,
  ),
);
