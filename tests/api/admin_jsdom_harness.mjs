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
    section: "request_log",
    type: "number",
    value: "200000",
    default: "200000",
  },
  /* Nobody ever set this one: `value` is empty and `set` is false. It has to
     load showing its default and count as no change -- the bug was that it
     displayed the first option ("false"), disagreed with dataset.original, and
     every Save wrote that value. */
  {
    key: "FALLBACK_BENCH_ENABLED",
    label: "Bench failures",
    section: "benching",
    type: "select",
    value: "",
    default: "true",
    set: false,
    source: "default",
    options: [
      { value: "false", label: "false" },
      { value: "true", label: "true" },
    ],
  },
  /* Set from the dashboard, and to something other than the default: this is
     the field that gets a "Use default" button. */
  {
    key: "LOG_LEVEL",
    label: "Log level",
    section: "diagnostics",
    type: "select",
    value: "DEBUG",
    default: "INFO",
    set: true,
    source: "managed_env",
    options: [
      { value: "INFO", label: "INFO" },
      { value: "DEBUG", label: "DEBUG" },
    ],
  },
  {
    key: "MODEL",
    label: "Default Model",
    section: "models",
    type: "text",
    value: "p1/m1",
    default: "",
  },
  /* Routes the deadline calculator divides the budget between. Opus is the
     longest chain (10), Sonnet 3, Haiku 1, and Fable is empty on both halves
     so the "a route with no model of its own is skipped" branch is exercised
     -- it falls back to MODEL, and counting it would double-count Default.
     Vision's primary is markup, which must render as text. */
  ...[
    ["MODEL_FALLBACKS", ""],
    ["MODEL_FABLE", ""],
    ["MODEL_FABLE_FALLBACKS", ""],
    ["MODEL_OPUS", "p1/o0"],
    [
      "MODEL_OPUS_FALLBACKS",
      "p1/o1,p1/o2,p1/o3,p1/o4,p1/o5,p1/o6,p1/o7,p1/o8,p1/o9",
    ],
    ["MODEL_SONNET", "p1/s0"],
    ["MODEL_SONNET_FALLBACKS", "p1/s1,p1/s2"],
    ["MODEL_HAIKU", "p1/h0"],
    ["MODEL_HAIKU_FALLBACKS", ""],
    ["MODEL_VISION", "<img src=x onerror=boom()>"],
    ["MODEL_VISION_FALLBACKS", ""],
  ].map(([key, value]) => ({
    key,
    label: key,
    section: "models",
    // Plain text, not the chain editor: the calculator reads the joined
    // string off [data-key], and the editor's own hidden input carries the
    // same attribute, so the read path is identical either way.
    type: "text",
    value,
    default: "",
  })),
  /* The Limits & Resilience payload. Realistic numbers, because the
     calculator's arithmetic is asserted against hand-computed values. */
  ...[
    ["MAX_OUTPUT_TOKENS_UNKNOWN_DEFAULT", "budgets", "32768", "0 to 1048576"],
    ["MAX_OUTPUT_TOKENS_CEILING", "budgets", "", "0 to 1048576"],
    ["MAX_OUTPUT_TOKENS_CONTEXT_MARGIN", "budgets", "1024", "0 to 1048576"],
    ["MAX_OUTPUT_TOKENS_CONTEXT_FLOOR", "budgets", "1024", "0 to 1048576"],
    ["REASONING_ANSWER_FLOOR_MAX", "budgets", "8192", "0 to 1048576"],
    [
      "FALLBACK_FIRST_TOKEN_TIMEOUT",
      "deadlines",
      "120",
      "0 to 3600 (0 waits indefinitely for the first token)",
    ],
    ["FALLBACK_TOTAL_TIMEOUT", "deadlines", "600", "0 to 86400"],
    ["FALLBACK_STALL_TIMEOUT", "deadlines", "120", "0 to 3600"],
    ["FALLBACK_REASONING_ANSWER_TIMEOUT", "deadlines", "300", "0 to 3600"],
    ["STREAM_COMMIT_HOLDBACK_SECONDS", "deadlines", "0", "0 to 60"],
    ["HTTP_READ_TIMEOUT", "deadlines", "300", "1 to 3600"],
    ["HTTP_WRITE_TIMEOUT", "deadlines", "60", "1 to 3600"],
    ["HTTP_CONNECT_TIMEOUT", "deadlines", "60", "1 to 600"],
    ["SERVER_GRACEFUL_SHUTDOWN_SECONDS", "deadlines", "300", "0 to 3600"],
    ["FALLBACK_EJECT_WINDOW", "benching", "10", "1 to 1000"],
    ["FALLBACK_EJECT_FAILURE_RATE", "benching", "0.5", "0 to 1"],
    ["FALLBACK_EJECT_MIN_SAMPLES", "benching", "8", "1 to 1000"],
    ["FALLBACK_EJECT_AFTER_FAILURES", "benching", "3", "0 to 100"],
    ["FALLBACK_EJECT_SECONDS", "benching", "30", "0 to 86400"],
    ["FALLBACK_COOLDOWN_STEP_OVER_FLOOR", "benching", "5", "0 to 3600"],
    ["PROVIDER_RETRY_ATTEMPTS", "provider_retries", "2", "0 to 10"],
    ["RATE_LIMIT_COOLDOWN_SECONDS", "credential_health", "60", "0 to 86400"],
  ].map(([key, section, value, rangeHint]) => ({
    key,
    label: key,
    section,
    type: "number",
    value,
    default: value || "0",
    range_hint: rangeHint,
    description: `What ${key} decides.`,
  })),
  // The nine storage fields now live on Analytics, and the desktop card now
  // lives on Providers: both moves are asserted, so both need a payload.
  ...[
    ["REQUEST_LOG_ENABLED", "boolean", "true"],
    ["REQUEST_LOG_CAPTURE_BODIES", "boolean", "true"],
    ["REQUEST_LOG_COMPRESS_BODIES", "boolean", "true"],
    ["REQUEST_LOG_CAPTURE_IMAGES", "boolean", "true"],
    ["REQUEST_LOG_IMAGE_MAX_PIXELS", "number", "4000000"],
    ["REQUEST_LOG_TEXT_MAX_CHARS", "number", "2000000"],
    ["REQUEST_LOG_COMPRESSION_LEVEL", "number", "6"],
    ["REQUEST_LOG_QUEUE_MAX_SIZE", "number", "10000"],
  ].map(([key, type, value]) => ({
    key,
    label: key,
    section: "request_log",
    type,
    value,
    default: value,
  })),
  ...["DESKTOP_HEALTH_POLL_SECONDS", "DESKTOP_WINDOW_WIDTH"].map((key) => ({
    key,
    label: key,
    section: "desktop",
    type: "number",
    value: "10",
    default: "10",
  })),
  {
    key: "FALLBACK_RETRY_FIRST",
    label: "Retry the first model once",
    section: "benching",
    type: "boolean",
    value: "false",
    default: "false",
  },
  {
    key: "FALLBACK_BEHAVIOR",
    label: "Eject mode",
    section: "benching",
    type: "select",
    value: "rate_based",
    default: "rate_based",
    set: true,
    source: "managed_env",
    options: [
      { value: "rate_based", label: "rate_based" },
      { value: "legacy", label: "legacy" },
    ],
  },
  {
    key: "CREDENTIAL_LOCKOUT_TIERS",
    label: "Lockout ladder",
    section: "credential_health",
    type: "text",
    value: "300,3600,86400",
    default: "300,3600,86400",
  },
];

const SECTIONS = [
  { id: "providers", label: "Providers", description: "" },
  { id: "models", label: "Model Routing", description: "Where each tier sends." },
  { id: "optimizer", label: "Tool-result trimming", description: "" },
  { id: "budgets", label: "Output & thinking budgets", description: "How big an answer." },
  { id: "deadlines", label: "Deadlines", description: "How long a model may hold it." },
  { id: "benching", label: "Chain benching", description: "When to stop trying a model." },
  {
    id: "provider_retries",
    label: "Provider retries & throughput",
    description: "How hard one model is retried.",
  },
  {
    id: "credential_health",
    label: "Credential health",
    description: "What a key's failures cost it.",
  },
  { id: "request_log", label: "Request log storage", description: "What the log keeps." },
  { id: "diagnostics", label: "Diagnostics", description: "Logging flags." },
  { id: "desktop", label: "Desktop", description: "Tray and window timing." },
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
    recovery: { early_retries: 41, midstream_recoveries: 7, salvages: 3 },
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

const requestsView = doc.querySelector('.admin-view[data-view="requests"]');
const requestCards = requestsView
  ? Array.from(
      requestsView.querySelectorAll("#reqStatsCards .requests-card"),
    ).map((card) =>
      Array.from(card.children).map((el) => el.textContent.trim()),
    )
  : [];

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

/* ------------------------------------------------- unset fields and defaults
   Read before anything on this page is clicked: these describe the state the
   form loaded in, and "loaded clean" is half of what is being proved. */
const limitsView = doc.querySelector('.admin-view[data-view="limits"]');
// The wrapper carries data-key too, so ask for the control element by name:
// the shared dirty machinery only ever looks at input/select/textarea.
const CONTROL_SELECTOR = (key) =>
  `select[data-key="${key}"], input[data-key="${key}"], textarea[data-key="${key}"]`;
const controlIn = (key) =>
  limitsView ? limitsView.querySelector(CONTROL_SELECTOR(key)) : null;
const describeControl = (control) =>
  control
    ? {
        tag: control.tagName.toLowerCase(),
        value: control.value,
        original: control.dataset.original,
        defaultAttr: control.dataset.default,
        optionValues: Array.from(control.options || []).map((item) => item.value),
        firstOptionLabel: control.options ? control.options[0].textContent : null,
      }
    : null;
const rowIn = (key) =>
  limitsView ? limitsView.querySelector(`.field[data-key="${key}"]`) : null;
const metaTextIn = (key) => {
  const row = rowIn(key);
  const meta = row ? row.querySelector(".field-default") : null;
  return meta ? meta.textContent.trim() : null;
};

const unsetSelect = describeControl(controlIn("FALLBACK_BENCH_ENABLED"));
const setSelect = describeControl(controlIn("LOG_LEVEL"));
const booleanControl = describeControl(
  doc.querySelector(CONTROL_SELECTOR("ENABLE_TOOL_RESULT_TRIMMING")),
);
const fieldDefaults = {
  FALLBACK_BENCH_ENABLED: metaTextIn("FALLBACK_BENCH_ENABLED"),
  LOG_LEVEL: metaTextIn("LOG_LEVEL"),
};
const resetButtons = {
  FALLBACK_BENCH_ENABLED: Boolean(
    rowIn("FALLBACK_BENCH_ENABLED")?.querySelector(".field-reset"),
  ),
  LOG_LEVEL: Boolean(rowIn("LOG_LEVEL")?.querySelector(".field-reset")),
};
const dirtyOnLoad = (doc.getElementById("dirtyState") || {}).textContent || null;

/* "Use default" has to submit the empty value -- that is what tells the server
   to drop the line rather than to store a second copy of the default. The
   control is put back afterwards so the counter below still starts at zero. */
let useDefault = null;
const resetButton = rowIn("LOG_LEVEL")?.querySelector(".field-reset");
if (resetButton) {
  const select = controlIn("LOG_LEVEL");
  resetButton.click();
  useDefault = {
    value: select.value,
    dirty: (doc.getElementById("dirtyState") || {}).textContent || null,
  };
  select.value = select.dataset.original;
  select.dispatchEvent(new window.Event("change", { bubbles: true }));
}

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

/* -------------------------------------------------- limits & resilience
   Snapshot the six cards, the calculator's arithmetic and the derived
   readouts, then drive the two mode switches and one deadline edit. */
const textOf = (root, selector) => {
  const el = root ? root.querySelector(selector) : null;
  return el ? (el.textContent || "").replace(/\s+/g, " ").trim() : null;
};
const benchGroupsNow = () =>
  limitsView
    ? Array.from(limitsView.querySelectorAll("[data-bench-mode]")).map((group) => ({
        mode: group.dataset.benchMode,
        inert: group.classList.contains("is-inert"),
        disabled: Array.from(group.querySelectorAll("input, select, textarea")).every(
          (el) => el.disabled,
        ),
        note: textOf(group, ".bench-group-note"),
      }))
    : [];
const calcRowsNow = () =>
  limitsView
    ? Array.from(limitsView.querySelectorAll("#calcTable tr")).map((tr) =>
        Array.from(tr.children).map((cell) => cell.textContent.trim()),
      )
    : [];
const hintOf = (key) => textOf(rowIn(key), ".field-hint");
const tocLinks = Array.from(doc.querySelectorAll("#limitsToc a")).map((a) =>
  a.getAttribute("href"),
);
const calcWarningEl = limitsView ? limitsView.querySelector("#calcWarning") : null;

const limits = {
  cardIds: limitsView
    ? Array.from(limitsView.querySelectorAll(".settings-section")).map((el) => el.id)
    : [],
  cardTitles: limitsView
    ? Array.from(limitsView.querySelectorAll(".settings-section .section-heading h3")).map(
        (el) => el.textContent.trim(),
      )
    : [],
  cardDescriptions: limitsView
    ? Array.from(limitsView.querySelectorAll(".settings-section .section-heading p")).map(
        (el) => el.textContent.trim(),
      )
    : [],
  /* A card whose every field is behind "Show advanced" renders a heading, a
     description and nothing else. That is the failure this counts. */
  cardVisibleFields: limitsView
    ? Array.from(limitsView.querySelectorAll(".settings-section")).map(
        (el) => el.querySelectorAll(".field:not(.advanced-field)").length,
      )
    : [],
  calcHeadline: textOf(limitsView, "#calcHeadline"),
  calcFormula: textOf(limitsView, "#calcFormula"),
  calcWarning: textOf(limitsView, "#calcWarning"),
  calcWarningHidden: calcWarningEl ? calcWarningEl.hidden : null,
  calcRows: calcRowsNow(),
  calcTableHtml: limitsView
    ? (limitsView.querySelector("#calcTable") || { innerHTML: "" }).innerHTML
    : "",
  benchGroups: benchGroupsNow(),
  hints: {
    FALLBACK_EJECT_WINDOW: hintOf("FALLBACK_EJECT_WINDOW"),
    FALLBACK_EJECT_MIN_SAMPLES: hintOf("FALLBACK_EJECT_MIN_SAMPLES"),
    FALLBACK_EJECT_AFTER_FAILURES: hintOf("FALLBACK_EJECT_AFTER_FAILURES"),
    FALLBACK_EJECT_SECONDS: hintOf("FALLBACK_EJECT_SECONDS"),
    CREDENTIAL_LOCKOUT_TIERS: hintOf("CREDENTIAL_LOCKOUT_TIERS"),
  },
  ranges: {
    count: limitsView ? limitsView.querySelectorAll(".field-range").length : 0,
    FALLBACK_FIRST_TOKEN_TIMEOUT: textOf(
      rowIn("FALLBACK_FIRST_TOKEN_TIMEOUT"),
      ".field-range",
    ),
    describedBy: controlIn("FALLBACK_FIRST_TOKEN_TIMEOUT")
      ? controlIn("FALLBACK_FIRST_TOKEN_TIMEOUT").getAttribute("aria-describedby")
      : null,
  },
  crosslinks: limitsView ? limitsView.querySelectorAll(".bench-crosslink").length : 0,
  crosslinkText: textOf(limitsView, ".bench-crosslink"),
  skipKindsOnLimits: limitsView
    ? limitsView.querySelectorAll('[data-key="FALLBACK_SKIP_KINDS"]').length
    : 0,
  tocLinks,
  deadLinks: tocLinks.filter((href) => !doc.querySelector(href)).length,
  requestLogCards: requestsView
    ? Array.from(requestsView.querySelectorAll(".settings-section")).map((el) => ({
        id: el.id,
        fields: el.querySelectorAll(".field").length,
      }))
    : [],
  desktopCardView: doc.getElementById("section-desktop")
    ? doc.getElementById("section-desktop").closest(".admin-view").dataset.view
    : null,
};

// (a) switch to legacy: the rate_based knobs go inert, and the counter must
// read one setting -- the mode -- not one plus the knobs it just disabled.
const modeSelect = controlIn("FALLBACK_BEHAVIOR");
if (modeSelect) {
  modeSelect.value = "legacy";
  modeSelect.dispatchEvent(new window.Event("change", { bubbles: true }));
  limits.afterLegacy = {
    benchGroups: benchGroupsNow(),
    dirty: (doc.getElementById("dirtyState") || {}).textContent || null,
    windowStillInDom: Boolean(controlIn("FALLBACK_EJECT_WINDOW")),
    windowValue: controlIn("FALLBACK_EJECT_WINDOW")
      ? controlIn("FALLBACK_EJECT_WINDOW").value
      : null,
    submitted: Object.keys(window.eval("changedValues()")),
  };
}

// (b) benching off: both groups inert, whatever the mode says.
const benchSelect = controlIn("FALLBACK_BENCH_ENABLED");
if (benchSelect) {
  benchSelect.value = "false";
  benchSelect.dispatchEvent(new window.Event("change", { bubbles: true }));
  limits.afterBenchOff = { benchGroups: benchGroupsNow() };
}

// (c) raise the total budget: the calculator recomputes without a reload.
const totalInput = controlIn("FALLBACK_TOTAL_TIMEOUT");
if (totalInput) {
  totalInput.value = "1200";
  totalInput.dispatchEvent(new window.Event("input", { bubbles: true }));
  limits.calcHeadlineAfterEdit = textOf(limitsView, "#calcHeadline");
  limits.calcRowsAfterEdit = calcRowsNow();
}

// Every control the drive touched goes back to what it loaded with: the
// optimizer's own dirty assertion below counts from zero.
[modeSelect, benchSelect, totalInput].forEach((control) => {
  if (!control) return;
  control.value = control.dataset.original;
  control.dispatchEvent(new window.Event("change", { bubbles: true }));
});

// The dirty counter must count settings, not widgets: the visible switch and
// the hidden manifest input are one setting.
let dirtyAfterToggle = null;
const masterButton = optimizer ? optimizer.querySelector(".opt-switch") : null;
if (masterButton) {
  masterButton.click();
  await new Promise((resolve) => setTimeout(resolve, 60));
  dirtyAfterToggle = (doc.getElementById("dirtyState") || {}).textContent || null;
}

/* ------------------------------------------------- request detail (wire pane)
   The modal's renderers are driven directly with fixture rows rather than
   through openRequestDetail(), because the point here is what each stored
   shape renders as: a degraded body, an unmeasured attempt, a legacy
   truncated body, a benched-out pool, and gating disagreeing with the wire. */
function driveDetail(row) {
  window.eval(
    `renderWireRequest(${JSON.stringify(row)});` +
      `renderRequestChain(${JSON.stringify(row)});`,
  );
  const wire = doc.getElementById("reqDetailWire");
  const chain = doc.getElementById("reqDetailChain");
  return {
    hidden: wire.hidden,
    text: (wire.textContent || "").replace(/\s+/g, " ").trim(),
    knobs: Array.from(wire.querySelectorAll(".req-wire-knobs dd")).map(
      (dd) => dd.textContent,
    ),
    knobKeys: Array.from(wire.querySelectorAll(".req-wire-knobs dt")).map(
      (dt) => dt.textContent,
    ),
    reasoningBadge: textOf(wire, ".req-wire-reasoning"),
    contradictions: wire.querySelectorAll(".req-wire-contradiction").length,
    pre: wire.querySelector("pre") ? wire.querySelector("pre").textContent : null,
    unmeasured: wire.querySelectorAll(".req-wire-unmeasured").length,
    chainKeys: Array.from(chain.querySelectorAll(".req-chain-key")).map(
      (el) => el.textContent,
    ),
  };
}

const degradedBody = {
  model: "z-ai/glm-5.3-flash",
  reasoning_effort: "max",
  temperature: 0.7,
  messages: { _degraded: "list", _count: 40, _chars: 1200 },
  tools: { _degraded: "names", _count: 59, _names: ["Read", "Bash"] },
  _degraded: ["messages", "tools"],
  _original_chars: 41000,
  _limit: 8000,
};

const detailAttempt = (extra) =>
  Object.assign(
    {
      attempt: 0,
      provider: "commandcode",
      model_ref: "commandcode/z-ai/glm-5.3-flash",
      outcome: "succeeded",
      duration_ms: 900,
      params: null,
      wire_body: null,
      reasoning_emitted: null,
      key_index: null,
      key_label: null,
    },
    extra,
  );

const requestDetail = {
  degraded: driveDetail({
    reasoning_adaptation: null,
    reasoning_adaptation_kind: null,
    route_attempts: [
      detailAttempt({
        wire_body: degradedBody,
        reasoning_emitted: 1,
        params: {
          wire: {
            model: "z-ai/glm-5.3-flash",
            max_tokens: 16384,
            tools: 59,
            temperature: 0.7,
            top_p: 0.9,
            reasoning: { reasoning_effort: "max" },
          },
        },
      }),
    ],
  }),
  unmeasured: driveDetail({
    reasoning_adaptation: null,
    reasoning_adaptation_kind: null,
    route_attempts: [detailAttempt({})],
  }),
  contradiction: driveDetail({
    reasoning_adaptation: "REASONING EFFORT CLAMPED: ...",
    reasoning_adaptation_kind: "clamped",
    route_attempts: [detailAttempt({ wire_body: { model: "m" }, reasoning_emitted: 0 })],
  }),
  suppressed: driveDetail({
    reasoning_adaptation: "REASONING SUPPRESSED: ...",
    reasoning_adaptation_kind: "suppressed",
    route_attempts: [detailAttempt({ wire_body: { model: "m" }, reasoning_emitted: 0 })],
  }),
  unkinded: driveDetail({
    reasoning_adaptation: "REASONING LEVEL DROPPED: ...",
    reasoning_adaptation_kind: null,
    route_attempts: [detailAttempt({ wire_body: { model: "m" }, reasoning_emitted: 0 })],
  }),
  legacyTruncated: driveDetail({
    reasoning_adaptation: null,
    reasoning_adaptation_kind: null,
    route_attempts: [
      detailAttempt({
        wire_body: {
          _truncated: true,
          _limit: 8000,
          _original_chars: 41000,
          _preview: '{"messages": [{"role": "us',
        },
      }),
    ],
  }),
  benched: driveDetail({
    reasoning_adaptation: null,
    reasoning_adaptation_kind: null,
    route_attempts: [
      detailAttempt({ key_index: 0, key_label: "ab...cd" }),
      detailAttempt({
        attempt: 1,
        outcome: "failed",
        key_index: -1,
        key_label: "(no key available)",
      }),
    ],
  }),
};

requestDetail.reasoningRow = window.eval(
  `formatRequestReasoningEmitted({route_attempts:[{outcome:"succeeded",reasoning_emitted:0}]})`,
);
requestDetail.unmeasuredNumber = window.eval(`formatOptionalNumber(null)`);

console.log(
  JSON.stringify(
    {
      fatal: null,
      scriptErrors,
      consoleErrors,
      navLabels: navLinks.map((link) => link.textContent),
      views,
      fields: {
        unsetSelect,
        setSelect,
        booleanControl,
        fieldDefaults,
        resetButtons,
        dirtyOnLoad,
        useDefault,
      },
      requestCards,
      requestDetail,
      docs,
      limits,
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
