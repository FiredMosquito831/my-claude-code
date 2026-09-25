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
 *
 * ONE AT A TIME. Two of these running against the same tree produced roughly
 * three hundred spurious "script error" lines: they share the page's
 * localStorage shim only by accident, but they do share the machine, and a
 * run starved of CPU misses its own debounce windows -- every timing-sensitive
 * capture then reads a page that has not caught up yet, and reports it as a
 * failure of the page. So the second run refuses instead of producing
 * nonsense. Set MCC_JSDOM_ALLOW_CONCURRENT=1 if you genuinely want both.
 */
import { readFileSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM, VirtualConsole } from "jsdom";

const startedAt = Date.now();

const dir = process.argv[2];
if (!dir) {
  console.error("usage: admin_jsdom_harness.mjs <admin_static_dir>");
  process.exit(2);
}

/* ------------------------------------------------------------------- lock */
const LOCK_PATH = fileURLToPath(new URL("./.admin_jsdom.lock", import.meta.url));
// A lock older than this belonged to a run that was killed (Ctrl-C, a pytest
// timeout, a reboot). Nothing is served by making the next run fail too.
const LOCK_STALE_MS = Number(process.env.MCC_JSDOM_LOCK_STALE_MS || 1_800_000);
let lockHeld = false;

if (process.env.MCC_JSDOM_ALLOW_CONCURRENT !== "1") {
  for (let attempt = 0; attempt < 2 && !lockHeld; attempt += 1) {
    try {
      writeFileSync(LOCK_PATH, `${process.pid} ${new Date().toISOString()}\n`, {
        flag: "wx",
      });
      lockHeld = true;
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
      let ageMs = LOCK_STALE_MS + 1;
      try {
        ageMs = Date.now() - statSync(LOCK_PATH).mtimeMs;
      } catch {
        /* vanished between the failed create and the stat: try again */
        ageMs = LOCK_STALE_MS + 1;
      }
      if (ageMs > LOCK_STALE_MS) {
        try {
          unlinkSync(LOCK_PATH);
        } catch {
          /* someone else cleared it first, which is the same outcome */
        }
        continue;
      }
      console.error(
        "another jsdom harness run is already holding " +
          LOCK_PATH +
          ". Two concurrent runs report each other's missed debounce windows " +
          "as page failures, so this one refuses. Wait for it, or set " +
          "MCC_JSDOM_ALLOW_CONCURRENT=1.",
      );
      process.exit(3);
    }
  }
}

process.on("exit", () => {
  if (!lockHeld) return;
  try {
    unlinkSync(LOCK_PATH);
  } catch {
    /* nothing left to release */
  }
});

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
    ["MODEL_FALLBACKS", "", "model_chain"],
    // Mythos (7.2.0) carries a primary and a one-entry chain, so the new rail
    // is exercised as a configured route rather than as an empty one.
    ["MODEL_MYTHOS", "p1/y0", "optional_model"],
    ["MODEL_MYTHOS_FALLBACKS", "p1/y1", "model_chain"],
    ["MODEL_FABLE", "", "optional_model"],
    ["MODEL_FABLE_FALLBACKS", "", "model_chain"],
    ["MODEL_OPUS", "p1/o0", "optional_model"],
    [
      "MODEL_OPUS_FALLBACKS",
      "p1/o1,p1/o2,p1/o3,p1/o4,p1/o5,p1/o6,p1/o7,p1/o8,p1/o9",
      "model_chain",
    ],
    ["MODEL_SONNET", "p1/s0", "optional_model"],
    ["MODEL_SONNET_FALLBACKS", "p1/s1,p1/s2", "model_chain"],
    ["MODEL_HAIKU", "p1/h0", "optional_model"],
    ["MODEL_HAIKU_FALLBACKS", "", "model_chain"],
    ["MODEL_VISION", "<img src=x onerror=boom()>", "optional_model"],
    ["MODEL_VISION_FALLBACKS", "", "model_chain"],
    ["VISION_ADAPTER_MODE", "route", "select"],
    // The pause lists. Written by the Pause button rather than typed, so they
    // are never rendered as controls -- but they are in the payload, which is
    // where the page reads which rows are switched off.
    ["MODEL_PAUSED", "", "text"],
    ["MODEL_MYTHOS_PAUSED", "", "text"],
    ["MODEL_FABLE_PAUSED", "", "text"],
    ["MODEL_OPUS_PAUSED", "", "text"],
    ["MODEL_SONNET_PAUSED", "", "text"],
    ["MODEL_HAIKU_PAUSED", "", "text"],
    ["MODEL_VISION_PAUSED", "", "text"],
  ].map(([key, value, type]) => ({
    key,
    label: key,
    section: "models",
    ...(key === "VISION_ADAPTER_MODE"
      ? {
          options: [
            { value: "route", label: "Send the request to the vision model" },
            { value: "describe", label: "Describe the image, keep the model" },
          ],
          description: "What the adapter does with an image it has taken.",
        }
      : {}),
    // The real control types the manifest declares, so the route rails the
    // drag operates on are the ones the dashboard actually renders.
    type,
    value,
    default: "",
  })),
  /* The Limits & Resilience payload. Realistic numbers, because the
     calculator's arithmetic is asserted against hand-computed values. */
  ...[
    ["MAX_OUTPUT_TOKENS_UNKNOWN_DEFAULT", "budgets", "32768", "0 to 1048576"],
    [
      "MAX_OUTPUT_TOKENS_CEILING",
      "budgets",
      "131072",
      "0 to 1048576 (0 lifts the ceiling entirely)",
    ],
    ["MAX_OUTPUT_TOKENS_CONTEXT_MARGIN", "budgets", "1024", "0 to 1048576"],
    ["MAX_OUTPUT_TOKENS_CONTEXT_FLOOR", "budgets", "1024", "0 to 1048576"],
    ["REASONING_ANSWER_FLOOR_MAX", "budgets", "8192", "0 to 1048576"],
    [
      "FALLBACK_FIRST_TOKEN_TIMEOUT",
      "deadlines",
      "120",
      "0 to 3600 (0 waits indefinitely for the first token)",
    ],
    /* Deliberately 0 in the loaded payload: every arithmetic assertion below
       was written against the pure equal share, and keeping it that way makes
       them a live proof that the 0 escape hatch reproduces the pre-6.10.0
       calculator exactly. The floor's own behaviour is driven at (c). */
    [
      "FALLBACK_ATTEMPT_SHARE_FLOOR",
      "deadlines",
      "0",
      "0 to 3600 (0 divides the budget equally with no floor)",
    ],
    ["FALLBACK_TOTAL_TIMEOUT", "deadlines", "600", "0 to 86400"],
    ["FALLBACK_STALL_TIMEOUT", "deadlines", "120", "0 to 3600"],
    ["FALLBACK_REASONING_ANSWER_TIMEOUT", "deadlines", "300", "0 to 3600"],
    ["STREAM_COMMIT_HOLDBACK_SECONDS", "deadlines", "0", "0 to 60"],
    ["HTTP_READ_TIMEOUT", "deadlines", "300", "1 to 3600"],
    ["HTTP_WRITE_TIMEOUT", "deadlines", "60", "1 to 3600"],
    ["HTTP_CONNECT_TIMEOUT", "deadlines", "60", "1 to 600"],
    ["SERVER_GRACEFUL_SHUTDOWN_SECONDS", "deadlines", "20", "1 to 600"],
    ["STREAM_KEEPALIVE_IDLE_SECONDS", "stream_keepalive", "30", "0 to 3600"],
    ["STREAM_KEEPALIVE_INTERVAL_SECONDS", "stream_keepalive", "20", "1 to 3600"],
    ["STREAM_KEEPALIVE_MAX_SECONDS", "stream_keepalive", "300", "0 to 86400"],
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
  // 7.47.0: what the keepalive is on /v1/messages. The card's Claude Code
  // line follows it.
  {
    key: "STREAM_KEEPALIVE_MODE",
    label: "Keepalive mode",
    section: "stream_keepalive",
    type: "select",
    value: "ping",
    default: "ping",
    options: [
      { value: "ping", label: "ping" },
      { value: "frames", label: "frames" },
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
  {
    key: "RATE_LIMIT_COOLDOWN_MAX_SECONDS",
    label: "Longest cooldown a provider may ask for",
    section: "credential_health",
    type: "number",
    value: "3600",
    default: "3600",
    range_hint: "0 to 86400",
    description: "The ceiling on a wait a provider asks for in a header.",
    // Both cooldown fields carry `advanced` in the shipped manifest, which is
    // exactly why a reader could not find them beside the lockout ladder.
    // Flagged here so the harness proves they render anyway.
    advanced: true,
  },
  {
    key: "RATE_LIMIT_COOLDOWN_MODE",
    label: "What a 429 costs the key",
    section: "credential_health",
    advanced: true,
    type: "select",
    value: "provider",
    default: "provider",
    options: [
      { value: "provider", label: "Wait as long as the provider asked" },
      { value: "fixed", label: "Always the cooldown above" },
      { value: "off", label: "Never pause -- keep trying" },
    ],
    description: "What a 429 costs the key that met it.",
  },
  {
    key: "PROXY_FETCH_TEST_CONCURRENCY",
    label: "Addresses tested at once by a fetch",
    section: "credential_health",
    type: "number",
    value: "100",
    default: "100",
    range_hint: "4 to 500",
    description: "How many addresses a fetch tests at once.",
  },
  {
    key: "PROXY_FETCH_CONCURRENCY_MODE",
    label: "How that number is read",
    section: "credential_health",
    type: "select",
    value: "fixed",
    default: "fixed",
    options: [
      { value: "fixed", label: "A count of addresses" },
      { value: "percent", label: "A percentage of what the feeds offered" },
    ],
    description: "Whether the number above is a count or a percentage.",
  },
  {
    key: "PROXY_FETCH_CHECK_DEPTH",
    label: "How far a fetch tests each address",
    section: "credential_health",
    type: "select",
    value: "tls",
    default: "tls",
    options: [
      { value: "tls", label: "Tunnel and verify the certificate" },
      { value: "request", label: "Tunnel and send an HTTPS request" },
    ],
    description: "How far a fetch sweep's test of one address goes.",
  },
  // Server responsiveness. Two fields, one of them advanced, exactly as the
  // manifest declares them: the Limits rail links to `#section-loop_health`
  // in static markup, so a fixture that omits the section renders a card that
  // is not there and leaves the rail pointing at nothing.
  {
    key: "HEALTH_BUSY_LAG_MS",
    label: "Busy threshold",
    section: "loop_health",
    type: "number",
    value: "500",
    default: "500",
    description: "How late the loop has to be before /health says it is busy.",
  },
  {
    key: "HEALTH_HEARTBEAT_INTERVAL_MS",
    label: "Loop check interval",
    section: "loop_health",
    type: "number",
    value: "100",
    default: "100",
    advanced: true,
    description: "How often the server measures its own event-loop lag.",
  },
];

/* The OpenCode Zen card. Two fields, because the credential toggle is only
   meaningful beside the key it is an alternative to spending: 7.34.0 sends
   free Zen models on OpenCode's shared `public` credential, and the operator
   has to be able to find the switch and read why. */
FIELDS.push(
  {
    key: "OPENCODE_API_KEY",
    label: "OpenCode Zen API Key",
    section: "providers",
    provider: "opencode",
    type: "text",
    value: "",
    default: "",
    description: "The key paid OpenCode Zen models are fetched with.",
  },
  {
    key: "OPENCODE_FREE_TIER_CREDENTIAL",
    label: "OpenCode free-tier credential",
    section: "providers",
    provider: "opencode",
    type: "select",
    value: "",
    default: "public",
    options: [
      { value: "public", label: "Use OpenCode's shared anonymous credential (default)" },
      { value: "key", label: "Use my own key for everything" },
    ],
    description:
      "Which credential free OpenCode Zen models are fetched with. Free models are " +
      "metered per credential, not per address. public -- the default -- uses " +
      "OpenCode's shared anonymous credential exactly as the OpenCode CLI does when " +
      "no key is configured, so your own key's free allowance is not spent; key uses " +
      "your key for everything. Paid Zen models always use your key.",
  },
);

const SECTIONS = [
  { id: "providers", label: "Providers", description: "" },
  { id: "models", label: "Model Routing", description: "Where each tier sends." },
  { id: "optimizer", label: "Tool-result trimming", description: "" },
  { id: "budgets", label: "Output & thinking budgets", description: "How big an answer." },
  { id: "deadlines", label: "Deadlines", description: "How long a model may hold it." },
  {
    id: "stream_keepalive",
    label: "Stream keepalive",
    description: "What a silent stream is sent.",
  },
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
  {
    id: "loop_health",
    label: "Server responsiveness",
    description: "How the server measures whether it is keeping up.",
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

/* Three providers of 45 models each: deliberately above MODELS_PAGE_SIZE so
   the 40-row page and its "Show 5 more of 5" button are exercised, with one
   provider partly hidden by a *:free deny, one configured route, one measured
   reasoning badge and one host dialect. */
const modelsFor = (providerId, hiddenTail) =>
  Array.from({ length: 45 }, (_, index) => ({
    model_ref: `${providerId}/model-${String(index).padStart(2, "0")}${
      index % 5 === 0 ? ":free" : ""
    }`,
    visible: !(hiddenTail && index % 5 === 0),
    // What dictates the row's state, which the row reports permanently.
    hidden_by: hiddenTail && index % 5 === 0 ? "*:free" : "",
    configured: providerId === "alpha" && index === 0,
    has_metadata: true,
    override: index === 1 ? { temperature: 0.1 } : {},
    effective:
      index === 1
        ? [{ parameter: "temperature", action: "value", value: 0.1 }]
        : [],
    capabilities: {
      max_output_tokens: { value: 40960, source: "provider", source_label: "" },
      supports_vision: { value: true, source: "provider", source_label: "" },
      // The 6.74.0 wire-surface row, on a model whose host serves more than
      // one door -- which is what makes the 7.33.0 override control appear.
      response_surface:
        index === 6
          ? {
              value: "chat_completions",
              source: "default",
              source_label: "this provider's only surface",
              approximate: false,
              reference: false,
              tier: null,
              tier_label: null,
              note: "",
              label: "chat_completions (default)",
              offered: [
                {
                  value: "chat_completions",
                  label: "Chat Completions (/chat/completions)",
                },
                { value: "responses", label: "Responses (/responses)" },
              ],
              override: "",
            }
          : null,
    },
    // One fresh fact, one stale one, and one probe verdict that contradicts
    // the catalogue -- the three states the Learned column has to draw.
    learned:
      index === 3
        ? [
            {
              fact_kind: "output_cap",
              fact_label: "output cap",
              value: 4096,
              detail: "",
              source: "rejection",
              source_label: "the host's own rejection",
              learned_at: "2026-09-01T00:00:00Z",
              last_confirmed_at: "2026-09-06T00:00:00Z",
              age_seconds: 86400,
              stale: false,
              retired: false,
              hits: 3,
              evidence: "max_completion_tokens must be <= 4096",
              field: "max_output_tokens",
              agrees: false,
            },
          ]
        : index === 4
          ? [
              {
                fact_kind: "reasoning_field_rejected",
                fact_label: "reasoning field refused",
                value: true,
                detail: "reasoning_effort",
                source: "rejection",
                source_label: "the host's own rejection",
                learned_at: "2026-07-01T00:00:00Z",
                last_confirmed_at: "2026-07-02T00:00:00Z",
                age_seconds: 5_000_000,
                stale: true,
                retired: false,
                hits: 1,
                evidence: "unknown field reasoning_effort",
              },
            ]
          : [],
    reasoning_measured:
      index === 2 ? { attempts: 12, requested: 12, returned: 12 } : null,
    reasoning_dialect:
      index === 3 ? { known: true, style: "effort", origin: "stated" } : null,
    /* The five shapes the preference control has to draw, one per row:
       ordinary, mandatory (Off withdrawn), cannot reason (control off),
       unknown vocabulary (every rung, unverified), and a host that parses no
       effort field. Row 6 also carries a stored value the catalogue no longer
       offers, which must be kept and badged rather than rewritten. */
    preferences: PREFERENCES_FOR(index),
  }));

const PREFERENCE_SHAPES = {
  0: { options: [
        { value: "client", label: "From client", available: true, reason: null },
        { value: "off", label: "Off", available: true, reason: null },
        { value: "adaptive", label: "Adaptive", available: false, reason: "no adaptive channel" },
        { value: "low", label: "Low", available: true, reason: null },
        { value: "high", label: "High", available: true, reason: null },
      ], capability_known: true, can_reason: true },
  1: { options: [
        { value: "client", label: "From client", available: true, reason: null },
        { value: "off", label: "Off", available: false, reason: "this model cannot run with thinking disabled" },
        { value: "high", label: "High", available: true, reason: null },
      ], capability_known: true, can_reason: true },
  2: { options: [
        { value: "client", label: "From client", available: false, reason: "this model does not reason" },
      ], capability_known: false, can_reason: false },
  3: { options: ["client", "off", "adaptive", "minimal", "low", "medium", "high", "xhigh", "max"].map(
        (value) => ({ value, label: value, available: true, reason: "unverified -- will be clamped" }),
      ), capability_known: false, can_reason: true },
  4: { options: [
        { value: "client", label: "From client", available: true, reason: null },
        { value: "high", label: "High", available: true, reason: "a level has no effect on this host -- nothing is sent" },
      ], capability_known: true, can_reason: true },
};

const PREFERENCES_FOR = (index) => ({
  reasoning_preference: {
    ...(PREFERENCE_SHAPES[index] || PREFERENCE_SHAPES[0]),
    state: index === 6 ? "value" : "inherit",
    value: index === 6 ? "xhigh" : null,
  },
  max_output_tokens: {
    state: "inherit",
    value: null,
    limit: index === 5 ? null : 40960,
    limit_source_label: "models.dev",
    limit_tier_label: "models.dev bucket, exact id",
    note: index === 5
      ? "Nothing publishes an output limit for this model, so your number becomes the limit."
      : "A cap, not a request: a client asking for fewer tokens still gets fewer.",
  },
});

const MODEL_ADMIN_PAGE = {
  measured_days: 7,
  source_labels: {},
  fact_labels: {},
  learned_source_labels: {},
  catalogue_refresh: {
    enabled: true,
    interval_seconds: 3600,
    configured_seconds: 3600,
    last_refreshed_at: Date.now() / 1000 - 720,
    next_refresh_at: Date.now() / 1000 + 2880,
    running: true,
  },
  providers: ["alpha", "beta", "gamma"].map((providerId) => ({
    provider_id: providerId,
    model_count: 45,
    hidden_count: providerId === "beta" ? 9 : 0,
    override: {},
    preferences: {
      reasoning_preference: {
        state: "inherit",
        value: null,
        options: ["off", "client", "adaptive", "minimal", "low", "medium", "high", "xhigh", "max"].map(
          (value) => ({ value, label: value, available: true, reason: null }),
        ),
        capability_known: false,
        note: "each model clamps this to what it accepts",
      },
      max_output_tokens: {
        state: "inherit",
        value: null,
        limit: null,
        limit_source_label: null,
        limit_tier_label: null,
        note: "A cap for every model under this provider.",
      },
    },
    models: modelsFor(providerId, providerId === "beta"),
  })),
  overrides: {
    editable_parameters: ["temperature"],
    preference_parameters: { reasoning_preference: "enum", max_output_tokens: "integer" },
    owned_elsewhere: {},
  },
  visibility: {
    allow_raw: "",
    deny_raw: "*:free",
    hide_only_notice: "Hiding is display-only; a hidden model still resolves.",
    hidden_route_refs: [],
  },
};

/* Mutated by the driving block below so one harness run can exercise a clean
   result, a partly-overruled one and a refused write. */
const BULK_RESULT = {
  action: "hide",
  scope: "provider",
  provider_id: "alpha",
  wrote_glob: "alpha/*",
  removed_patterns: [],
  previous: { allow: [], deny: ["*:free"] },
  honored_count: 45,
  unhonored_count: 0,
  changed: [],
  results: [],
  visibility: { allow: [], deny: ["alpha/*"] },
};

const ROUTES = {
  /* Two credential pools for the key manager: one whose keys hold live
     (key, model) 429 benches, and one plain healthy pool that must render
     exactly as it did before model benches existed. */
  "/admin/api/credentials/SCOPED_API_KEY/keys": {
    count: 2,
    locked: false,
    keys: ["nvap...ubCk", "nvap...9fQt"],
    rows: [
      {
        index: 0,
        id: "sha256:1111111111111111",
        masked: "nvap...ubCk",
        key_label: "nvap\u2026ubCk",
        name: "",
        health: null,
      },
      {
        index: 1,
        id: "sha256:2222222222222222",
        masked: "nvap...9fQt",
        key_label: "nvap\u20269fQt",
        name: "",
        health: null,
      },
    ],
    health: [
      {
        index: 0,
        state: "HEALTHY",
        request_count: 12,
        failure_count: 5,
        cooldown_remaining: 0,
        lockout_remaining: 0,
        model_benches: [
          { model: "moonshotai/kimi-k3", remaining: 58.4 },
          { model: "nvidia/nemotron-3-ultra", remaining: 30.0 },
          { model: "minimaxai/minimax-m2", remaining: 12.0 },
          { model: "openai/gpt-oss-120b", remaining: 4.0 },
        ],
      },
      {
        index: 1,
        state: "COOLDOWN",
        request_count: 3,
        failure_count: 3,
        cooldown_remaining: 45,
        lockout_remaining: 0,
        model_benches: [{ model: "moonshotai/kimi-k3", remaining: 45.0 }],
      },
    ],
  },
  /* A pool benched for money rather than for a throttle. Waiting does not
     clear it, so the badge has to say so on the row. */
  "/admin/api/credentials/CREDITS_API_KEY/keys": {
    count: 1,
    locked: false,
    keys: ["cc...9fQt"],
    rows: [
      {
        index: 0,
        id: "sha256:3333333333333333",
        masked: "cc...9fQt",
        key_label: "cc\u20269fQt",
        name: "",
        health: null,
      },
    ],
    health: [
      {
        index: 0,
        state: "COOLDOWN",
        request_count: 4,
        failure_count: 4,
        cooldown_remaining: 55,
        lockout_remaining: 0,
        cooldown_reason: "credits exhausted",
        model_benches: [],
      },
    ],
  },
  "/admin/api/credentials/PLAIN_API_KEY/keys": {
    count: 1,
    locked: false,
    keys: ["nvap...2vLm"],
    rows: [
      {
        index: 0,
        id: "sha256:4444444444444444",
        masked: "nvap...2vLm",
        key_label: "nvap\u20262vLm",
        name: "",
        health: null,
      },
    ],
    health: [
      {
        index: 0,
        state: "HEALTHY",
        request_count: 7,
        failure_count: 0,
        cooldown_remaining: 0,
        lockout_remaining: 0,
        model_benches: [],
      },
    ],
  },
  /* Three keys, the middle one named. The rail, the name boxes, the Move
     buttons at the ends and the status line are all read off this one. */
  "/admin/api/credentials/NAMED_API_KEY/keys": {
    count: 3,
    locked: false,
    keys: ["sk-one...1111", "sk-two...2222", "sk-thr...3333"],
    health: [null, null, null],
    rows: [
      {
        index: 0,
        id: "sha256:aaaaaaaaaaaaaaaa",
        masked: "sk-one...1111",
        key_label: "sk-o\u20261111",
        name: "",
        health: null,
      },
      {
        index: 1,
        id: "sha256:bbbbbbbbbbbbbbbb",
        masked: "sk-two...2222",
        key_label: "sk-t\u20262222",
        name: "Personal <b>card</b>",
        health: null,
      },
      {
        index: 2,
        id: "sha256:cccccccccccccccc",
        masked: "sk-thr...3333",
        key_label: "sk-t\u20263333",
        name: "",
        health: null,
      },
    ],
  },
  /* The web-search pool's own listing, in the same shape and with the same
     two named/unnamed cases, so the shared rail can be rendered for all
     three pool types from one harness run. */
  "/admin/api/websearch/credentials/TAVILY_API_KEY/keys": {
    locked: false,
    keys: ["tvly-o...1111", "tvly-t...2222"],
    health: { keys: [] },
    rows: [
      {
        index: 0,
        id: "sha256:dddddddddddddddd",
        masked: "tvly-o...1111",
        key_label: "tvly…1111",
        name: "",
      },
      {
        index: 1,
        id: "sha256:eeeeeeeeeeeeeeee",
        masked: "tvly-t...2222",
        key_label: "tvly…2222",
        name: "tavily production europe west billing",
      },
    ],
  },
  "/admin/api/config": {
    fields: FIELDS,
    sections: SECTIONS,
    provider_status: [],
    paths: { managed: "/tmp/.env" },
    /* Which harness alias names each global route, exactly as the API joins it
       on from core/tier_refs.py. MODEL_FABLE is absent on purpose: it is a
       Claude alias, not a tier, and `mcc/best` follows MODEL. */
    route_tier_aliases: {
      MODEL: "mcc/best",
      MODEL_MYTHOS: "mcc/cyber",
      MODEL_OPUS: "mcc/good",
      MODEL_SONNET: "mcc/medium",
      MODEL_HAIKU: "mcc/cheap",
      MODEL_VISION: "mcc/vision",
    },
  },
  "/admin/api/onboarding": { dismissed: true, complete: true, steps: [] },
  "/admin/api/providers/local-status": { providers: [] },
  "/admin/api/config/validate": { valid: true, errors: [] },
  "/admin/api/version": { current: "5.47.1" },
  "/admin/api/desktop": { available: false },
  "/admin/api/rtk": {
    installed: false,
    claude: false,
    codex: true,
    pi: false,
    agents: { claude: false, codex: true, pi: false },
  },
  // One of each shape the Coding agents page has to render: an installed
  // harness with a materialised catalogue carrying CLI defaults, a
  // not-installed one whose catalogue was never written, and one whose model
  // list never touches disk.
  "/admin/api/harnesses": {
    harnesses: [
      {
        id: "claude",
        display_name: "Claude Code",
        binary: "claude",
        installed: true,
        binary_path: "/usr/local/bin/claude",
        install_hint: "Install Claude Code with: npm install -g @anthropic-ai/claude-code",
        command: "mcc-claude",
        commands: ["mcc-claude", "mcc-claude-old"],
        command_lines: [
          { command: "mcc-claude", help: "Launch Claude Code through the proxy", kind: "primary" },
          { command: "mcc-claude --discover-models", help: "Also enable the model picker from the catalog", kind: "flag" },
          { command: "mcc-claude-old", help: "Legacy launcher: full proxy environment", kind: "flag" },
          { command: "fcc-claude", help: "Legacy alias for mcc-claude", kind: "legacy" },
          { command: "mcc-rtk enable claude", help: "Wrap Claude Code's shell tool with the token optimizer", kind: "rtk" },
        ],
        protocol: "anthropic_messages",
        protocol_label: "Anthropic Messages (POST /v1/messages)",
        summary: "Anthropic's Claude Code, pointed here with two environment variables.",
        rtk_agent: true,
        rtk_enabled: false,
        catalogue: null,
      },
      {
        id: "codex",
        display_name: "Codex CLI",
        binary: "codex",
        installed: true,
        binary_path: "/usr/local/bin/codex",
        install_hint: "Install Codex with: npm install -g @openai/codex",
        command: "mcc-codex",
        commands: ["mcc-codex"],
        command_lines: [
          { command: "mcc-codex", help: "Launch Codex through the proxy", kind: "primary" },
          { command: 'mcc-codex exec "<prompt>"', help: "Run Codex non-interactively on one prompt", kind: "flag" },
          { command: "fcc-codex", help: "Legacy alias for mcc-codex", kind: "legacy" },
        ],
        protocol: "openai_responses",
        protocol_label: "OpenAI Responses (POST /v1/responses)",
        summary: "OpenAI's Codex CLI, configured with ephemeral -c assignments.",
        rtk_agent: true,
        rtk_enabled: true,
        catalogue: {
          format: "codex",
          config_env_var: null,
          delivery: "file",
          path: "/home/u/.fcc/codex-model-catalog.json",
          exists: true,
          updated_at: "2026-09-01T09:12:44Z",
          model_count: 12,
          defaulted_model_count: 3,
        },
      },
      {
        id: "pi",
        display_name: "Pi",
        binary: "pi",
        installed: false,
        binary_path: null,
        install_hint: "Install Pi with: curl -fsSL https://pi.dev/install.sh | sh",
        command: "mcc-pi",
        commands: ["mcc-pi"],
        command_lines: [
          { command: "mcc-pi", help: "Launch Pi through the proxy", kind: "primary" },
          { command: "fcc-pi", help: "Legacy alias for mcc-pi", kind: "legacy" },
        ],
        protocol: "anthropic_messages",
        protocol_label: "Anthropic Messages (POST /v1/messages)",
        summary: "The Pi coding agent, registered process-locally by a bundled extension.",
        rtk_agent: true,
        rtk_enabled: false,
        catalogue: {
          format: "pi",
          config_env_var: null,
          delivery: "process_local",
          path: null,
          exists: true,
          updated_at: null,
          model_count: null,
          defaulted_model_count: null,
        },
      },
      {
        id: "opencode",
        display_name: "OpenCode",
        binary: "opencode",
        installed: true,
        binary_path: "/usr/local/bin/opencode",
        install_hint: "Install OpenCode with: npm install -g opencode-ai",
        command: "mcc-opencode",
        commands: ["mcc-opencode"],
        command_lines: [
          { command: "mcc-opencode", help: "Launch OpenCode through the proxy", kind: "primary" },
          { command: 'mcc-opencode run "<prompt>"', help: "Run OpenCode non-interactively on one prompt", kind: "flag" },
          { command: "mcc-opencode models mcc", help: "List the models MCC published", kind: "flag" },
        ],
        protocol: "anthropic_messages",
        protocol_label: "Anthropic Messages (POST /v1/messages)",
        summary: "OpenCode, pointed at an MCC-owned config file through its own OPENCODE_CONFIG variable.",
        rtk_agent: false,
        rtk_enabled: false,
        catalogue: {
          format: "opencode",
          config_env_var: "OPENCODE_CONFIG",
          delivery: "file",
          path: "/home/u/.fcc/opencode-config.json",
          exists: false,
          updated_at: null,
          model_count: null,
          defaulted_model_count: null,
          defaulted_record_in_document: true,
        },
      },
      {
        id: "kilo",
        display_name: "Kilo CLI",
        binary: "kilo",
        installed: false,
        binary_path: null,
        install_hint: "Install Kilo CLI with: npm install -g @kilocode/cli",
        command: "mcc-kilo",
        commands: ["mcc-kilo"],
        command_lines: [
          { command: "mcc-kilo", help: "Launch Kilo CLI through the proxy", kind: "primary" },
        ],
        protocol: "anthropic_messages",
        protocol_label: "Anthropic Messages (POST /v1/messages)",
        summary: "Kilo CLI, a fork of OpenCode that reads the same config schema.",
        rtk_agent: false,
        rtk_enabled: false,
        catalogue: {
          format: "kilo",
          config_env_var: "KILO_CONFIG",
          delivery: "file",
          path: "/home/u/.fcc/kilo-config.json",
          exists: true,
          updated_at: "2026-09-02T09:12:44Z",
          model_count: 82,
          // Kilo's validator refuses unknown root keys, so the generated file
          // carries no ``_mcc_defaulted`` block to count.
          defaulted_model_count: null,
          defaulted_record_in_document: false,
        },
      },
      {
        id: "commandcode_cli",
        display_name: "Command Code",
        binary: "command-code",
        installed: true,
        binary_path: "/usr/local/bin/command-code",
        install_hint: "Install Command Code with: npm install -g command-code",
        command: "mcc-commandcode",
        commands: ["mcc-commandcode"],
        command_lines: [
          { command: "mcc-commandcode", help: "Launch Command Code through the proxy", kind: "primary" },
          { command: 'mcc-commandcode -p "<prompt>"', help: "Run one prompt non-interactively", kind: "flag" },
          { command: "mcc-commandcode --list-models", help: "List every model Command Code can see", kind: "flag" },
          { command: "mcc-commandcode --disconnect", help: "Remove MCC's provider.mcc key and exit", kind: "flag" },
        ],
        protocol: "anthropic_messages",
        protocol_label: "Anthropic Messages (POST /v1/messages)",
        summary: "Command Code, which reads one providers.json and no override file.",
        rtk_agent: false,
        rtk_enabled: false,
        catalogue: {
          format: "commandcode",
          config_env_var: null,
          merged_key: "provider.mcc",
          delivery: "merge",
          path: "/home/u/.commandcode/providers.json",
          exists: true,
          updated_at: "2026-09-02T07:31:02Z",
          model_count: 9,
          defaulted_model_count: 2,
        },
      },
      {
        id: "kimi_code",
        display_name: "Kimi Code",
        binary: "kimi",
        installed: true,
        binary_path: "/home/u/.local/bin/kimi",
        install_hint: "Install Kimi Code with: uv tool install kimi-cli (or: pipx install kimi-cli)",
        command: "mcc-kimi",
        commands: ["mcc-kimi"],
        command_lines: [
          { command: "mcc-kimi", help: "Launch Kimi Code through the proxy", kind: "primary" },
          { command: "mcc-kimi -m mcc/<provider>/<model>", help: "Start on one specific MCC-routed model", kind: "flag" },
          { command: 'mcc-kimi --print -p "<prompt>"', help: "Run one prompt non-interactively", kind: "flag" },
        ],
        protocol: "anthropic_messages",
        protocol_label: "Anthropic Messages (POST /v1/messages)",
        summary: "Kimi Code, pointed at an MCC-owned config.toml through its own --config-file flag.",
        rtk_agent: false,
        rtk_enabled: false,
        catalogue: {
          format: "kimi",
          config_env_var: null,
          config_flag: "--config-file",
          merged_key: null,
          delivery: "file",
          path: "/home/u/.fcc/kimi-code-config.toml",
          exists: true,
          updated_at: "2026-09-02T09:04:11Z",
          model_count: 7,
          defaulted_model_count: 1,
        },
      },
      {
        id: "qwen_code",
        display_name: "Qwen Code",
        binary: "qwen",
        installed: true,
        binary_path: "/home/u/.local/bin/qwen",
        install_hint: "Install Qwen Code with: npm install -g @qwen-code/qwen-code",
        command: "mcc-qwen",
        commands: ["mcc-qwen"],
        command_lines: [
          { command: "mcc-qwen", help: "Launch Qwen Code through the proxy", kind: "primary" },
          { command: 'mcc-qwen "<prompt>"', help: "Run one prompt non-interactively", kind: "flag" },
          { command: "mcc-qwen -m anthropic/<provider>/<model>", help: "Start on one specific MCC-routed model", kind: "flag" },
        ],
        protocol: "anthropic_messages",
        protocol_label: "Anthropic Messages (POST /v1/messages)",
        summary: "Qwen Code, pointed at an MCC-owned settings document through its own QWEN_CODE_SYSTEM_SETTINGS_PATH variable.",
        rtk_agent: false,
        rtk_enabled: false,
        catalogue: {
          format: "qwen",
          config_env_var: "QWEN_CODE_SYSTEM_SETTINGS_PATH",
          config_flag: null,
          merged_key: null,
          delivery: "file",
          path: "/home/u/.fcc/qwen-code-settings.json",
          exists: true,
          updated_at: "2026-09-02T09:05:11Z",
          model_count: 7,
          defaulted_model_count: 3,
        },
      },
      {
        id: "crush",
        display_name: "Crush",
        binary: "crush",
        installed: false,
        binary_path: null,
        install_hint: "Install Crush with: npm install -g @charmland/crush (or: brew install charmbracelet/tap/crush)",
        command: "mcc-crush",
        commands: ["mcc-crush"],
        command_lines: [
          { command: "mcc-crush", help: "Launch Crush through the proxy", kind: "primary" },
          { command: 'mcc-crush run "<prompt>"', help: "Run one prompt non-interactively and exit", kind: "flag" },
          { command: "mcc-crush models", help: "List every model Crush can see", kind: "flag" },
        ],
        protocol: "anthropic_messages",
        protocol_label: "Anthropic Messages (POST /v1/messages)",
        summary: "Crush, pointed at an MCC-owned crush.json through its own CRUSH_GLOBAL_CONFIG variable.",
        rtk_agent: false,
        rtk_enabled: false,
        catalogue: {
          format: "crush",
          config_env_var: "CRUSH_GLOBAL_CONFIG",
          config_flag: null,
          merged_key: null,
          delivery: "file",
          path: "/home/u/.fcc/crush/crush.json",
          exists: false,
          updated_at: null,
          model_count: 0,
          defaulted_model_count: 0,
        },
      },
      {
        id: "cline_cli",
        display_name: "Cline",
        binary: "cline",
        installed: true,
        binary_path: "/usr/local/bin/cline",
        install_hint: "Install Cline with: npm install -g cline",
        command: "mcc-cline",
        commands: ["mcc-cline"],
        command_lines: [
          { command: "mcc-cline", help: "Launch Cline through the proxy", kind: "primary" },
          { command: 'mcc-cline "<prompt>"', help: "Run one prompt non-interactively", kind: "flag" },
          { command: "mcc-cline -m anthropic/<provider>/<model>", help: "Start on one specific MCC-routed model", kind: "flag" },
        ],
        protocol: "openai_chat_completions",
        protocol_label: "OpenAI Chat Completions (POST /v1/chat/completions)",
        summary: "Cline, pointed at a configuration directory this proxy owns through its own --config flag.",
        rtk_agent: false,
        rtk_enabled: false,
        catalogue: {
          format: "cline",
          config_env_var: null,
          config_flag: "--config",
          merged_key: null,
          delivery: "file",
          path: "/home/u/.fcc/cline/data/settings/providers.json",
          exists: true,
          updated_at: "2026-09-02T10:11:12Z",
          model_count: 6,
          defaulted_model_count: 2,
        },
      },
      {
        id: "goose",
        display_name: "Goose",
        binary: "goose",
        installed: false,
        binary_path: null,
        install_hint: "Install Goose from https://github.com/block/goose/releases",
        command: "mcc-goose",
        commands: ["mcc-goose"],
        command_lines: [
          { command: "mcc-goose", help: "Launch Goose through the proxy", kind: "primary" },
          { command: 'mcc-goose run -t "<prompt>" --no-session', help: "Run one prompt non-interactively", kind: "flag" },
          { command: "mcc-goose info -v", help: "Show the provider, model and context limit this launch resolved", kind: "flag" },
        ],
        protocol: "openai_chat_completions",
        protocol_label: "OpenAI Chat Completions (POST /v1/chat/completions)",
        summary: "Goose, pointed here with six environment variables and no file at all.",
        rtk_agent: false,
        rtk_enabled: false,
        catalogue: null,
      },
      {
        id: "aider",
        display_name: "Aider",
        binary: "aider",
        installed: true,
        binary_path: "/home/u/.local/bin/aider",
        install_hint: "Install Aider with: uv tool install aider-chat",
        command: "mcc-aider",
        commands: ["mcc-aider"],
        command_lines: [
          { command: "mcc-aider", help: "Launch Aider through the proxy", kind: "primary" },
          { command: 'mcc-aider --message "<prompt>"', help: "Run one prompt non-interactively", kind: "flag" },
          { command: "mcc-aider --list-models openai/anthropic", help: "List every MCC-routed model Aider can see", kind: "flag" },
        ],
        protocol: "openai_chat_completions",
        protocol_label: "OpenAI Chat Completions (POST /v1/chat/completions)",
        summary: "Aider, pointed at a metadata file and a settings file this proxy owns.",
        rtk_agent: false,
        rtk_enabled: false,
        catalogue: {
          format: "aider",
          config_env_var: null,
          config_flag: "--model-metadata-file",
          merged_key: null,
          delivery: "file",
          path: "/home/u/.fcc/aider-model-metadata.json",
          exists: true,
          updated_at: "2026-09-02T10:12:13Z",
          model_count: 6,
          defaulted_model_count: 1,
        },
      },
      {
        id: "droid",
        display_name: "Droid",
        binary: "droid",
        installed: false,
        binary_path: null,
        install_hint: "Install Droid with: curl -fsSL https://app.factory.ai/cli | sh",
        command: "mcc-droid",
        commands: ["mcc-droid"],
        command_lines: [
          { command: "mcc-droid", help: "Launch Droid through the proxy", kind: "primary" },
          { command: 'mcc-droid exec "<prompt>"', help: "Run one prompt non-interactively", kind: "flag" },
          { command: "mcc-droid doctor", help: "Show the configuration and auth state of this launch", kind: "flag" },
        ],
        protocol: "anthropic_messages",
        protocol_label: "Anthropic Messages (POST /v1/messages)",
        summary: "Factory's Droid, pointed at a runtime settings overlay this proxy owns through --settings.",
        rtk_agent: false,
        rtk_enabled: false,
        catalogue: {
          format: "droid",
          config_env_var: null,
          config_flag: "--settings",
          merged_key: null,
          delivery: "file",
          path: "/home/u/.fcc/droid-settings.json",
          exists: false,
          updated_at: null,
          model_count: 0,
          defaulted_model_count: 0,
        },
      },
      {
        id: "gemini_cli",
        display_name: "Gemini CLI",
        binary: "gemini",
        installed: true,
        binary_path: "/usr/local/bin/gemini",
        install_hint: "Install Gemini CLI with: npm install -g @google/gemini-cli",
        command: "mcc-gemini",
        commands: ["mcc-gemini"],
        command_lines: [
          { command: "mcc-gemini", help: "Launch Gemini CLI through the proxy", kind: "primary" },
          { command: 'mcc-gemini -p "<prompt>"', help: "Run one prompt non-interactively and exit", kind: "flag" },
          { command: 'mcc-gemini --skip-trust -p "<prompt>"', help: "Gemini CLI refuses a headless run in an untrusted folder", kind: "flag" },
        ],
        protocol: "gemini",
        protocol_label: "Google Gemini (POST /v1beta/models/{model}:generateContent)",
        summary: "Gemini CLI, pointed at an MCC-owned settings document.",
        available: true,
        unavailable_reason: "",
        rtk_agent: false,
        rtk_enabled: false,
        catalogue: {
          format: "gemini_cli",
          config_env_var: "GEMINI_CLI_SYSTEM_SETTINGS_PATH",
          config_flag: null,
          merged_key: null,
          delivery: "file",
          path: "/home/u/.fcc/gemini-cli-settings.json",
          exists: true,
          updated_at: "2026-09-02T11:25:00Z",
          model_count: 2,
          defaulted_model_count: 2,
        },
      },
      {
        id: "antigravity",
        display_name: "Antigravity",
        binary: "agy",
        installed: false,
        binary_path: null,
        install_hint: "Install Antigravity from https://antigravity.google",
        command: "",
        commands: [],
        command_lines: [],
        protocol: "gemini",
        protocol_label: "Google Gemini (POST /v1beta/models/{model}:generateContent)",
        summary: "Google's Antigravity CLI speaks a private protocol behind a Google login.",
        available: false,
        unavailable_reason:
          "Antigravity is locked to Google auth -- verified 2026-09-02 against agy 1.0.14.",
        rtk_agent: false,
        rtk_enabled: false,
        catalogue: null,
      },
    ],
  },
  "/admin/api/rtk/gain": {
    available: false,
    reason: "not_installed",
    detail: "No RTK binary was found.",
  },
  "/admin/api/claude/settings": { configured: false },
  "/admin/api/claude/config": { entries: [], values: {}, path: "", parsed: true },
  "/admin/api/providers/custom": { providers: [] },
  "/admin/api/custom-providers/custom_acme/keys": {
    provider_id: "custom_acme",
    env_key: null,
    source: "custom_providers.json",
    locked: false,
    credential_rotation: "failover",
    count: 2,
    keys: ["sk-acm\u2026bbbb", "sk-acm\u2026dddd"],
    health: [
      {
        state: "HEALTHY",
        request_count: 12,
        failure_count: 0,
        cooldown_remaining: 0,
        lockout_remaining: 0,
        model_benches: [],
        index: 0,
        key_label: "sk-acm\u2026bbbb",
      },
      {
        state: "HEALTHY",
        request_count: 4,
        failure_count: 1,
        cooldown_remaining: 0,
        lockout_remaining: 0,
        model_benches: [{ model: "m1", remaining: 42 }],
        index: 1,
        key_label: "sk-acm\u2026dddd",
      },
    ],
    /* `rows` carries the id and the name, which is what turns a custom
       card's key list into a rail. Without it the card renders the
       pre-7.29.0 shape and the shared-rail guards below would pass
       vacuously. The second name is a long one on purpose: 37 characters
       is what set the row's minimum width before 7.34.1. */
    rows: [
      {
        index: 0,
        id: "sha256:1111111111111111",
        masked: "sk-acm\u2026bbbb",
        key_label: "sk-acm\u2026bbbb",
        name: "",
      },
      {
        index: 1,
        id: "sha256:2222222222222222",
        masked: "sk-acm\u2026dddd",
        key_label: "sk-acm\u2026dddd",
        name: "acme production europe west billing",
      },
    ],
  },
  "/admin/api/custom-providers": {
    providers: [
      {
        provider_id: "custom_acme",
        display_name: "Acme",
        base_url: "https://api.acme.example/v1",
        key_count: 2,
        masked_keys: ["sk-acm\u2026bbbb", "sk-acm\u2026dddd"],
        reasoning_effort_enum: ["low", "high", "max"],
        reasoning_field_ignored: false,
        reasoning_probe_status: "learned",
        reasoning_probed_at: "2026-09-01T10:00:00+00:00",
        reasoning_dialect_label: "learned {low, high, max} on 2026-09-01",
        auto_paused_refs: [],
        credential_rotation: "failover",
        proxy: null,
        enabled: true,
        model_count: 0,
        status: "configured",
        models: [],
        added_at: "2026-01-01T00:00:00Z",
        // This host was told it serves two doors, which is what puts a second
        // clause on the card's details line and two ticked boxes in its form.
        surfaces: ["chat_completions", "responses"],
        available_surfaces: [
          {
            value: "chat_completions",
            label: "Chat Completions (/chat/completions)",
          },
          { value: "responses", label: "Responses (/responses)" },
          { value: "messages", label: "Messages (/messages)" },
        ],
      },
      {
        provider_id: "custom_bad_ai",
        display_name: "Bad AI",
        base_url: "https://bad.example/v1",
        key_count: 1,
        masked_keys: ["sk-bad\u2026dddd"],
        credential_rotation: "failover",
        proxy: null,
        enabled: true,
        model_count: 0,
        status: "configured",
        models: [],
        added_at: "2026-01-01T00:00:00Z",
      },
    ],
  },
  "/admin/api/providers/custom_acme/test": {
    provider_id: "custom_acme",
    ok: true,
    models: ["m1", "m2", "m3"],
  },
  /* The Server responsiveness card reads this: what the last gestures
     cost the event loop. Two entries, one of them over the busy
     threshold, so both the ordinary row and the late one are rendered. */
  "/admin/api/loop-health": {
    busy_lag_ms: 500,
    gestures: [
      {
        reason: "the model catalogue is being refreshed",
        max_lag_ms: 2755,
        duration_ms: 4700,
        finished_at: "2026-09-19T12:15:00.000+00:00",
      },
      {
        reason: "a route is being paused",
        max_lag_ms: 84,
        duration_ms: 121,
        finished_at: "2026-09-19T12:14:00.000+00:00",
      },
    ],
  },
  "/admin/api/models": { models: [] },
  "/admin/api/model-admin": MODEL_ADMIN_PAGE,
  "/admin/api/model-admin/visibility/bulk": BULK_RESULT,
  "/admin/api/model-admin/visibility/toggle": {
    visible: false,
    honored: true,
    visibility: { allow: [], deny: [] },
  },
  "/admin/api/model-admin/visibility": { visibility: { allow: [], deny: [] } },
  "/admin/api/model-admin/visibility/migrate-globs": {
    providers: ["beta"],
    removed_patterns: ["beta/model-00:free", "beta/model-05:free"],
    added_patterns: ["beta/*"],
    hidden_before: 9,
    hidden_after: 9,
    identical: true,
    pattern_count_before: 3,
    pattern_count_after: 2,
    previous: { allow: [], deny: ["*:free"] },
    applied: false,
  },
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
    // Same shape as by_provider, with the display names alongside: the page
    // is not allowed to carry a registry of its own.
    by_harness: [
      {
        key: "opencode",
        requests: 90210,
        errors: 41,
        tokens_in: 3900,
        tokens_out: 12400,
        cache_read_tokens: 96100,
        cache_write_tokens: 0,
        avg_duration_ms: 2410,
      },
      {
        key: "claude",
        requests: 61003,
        errors: 0,
        tokens_in: 1200,
        tokens_out: 8800,
        cache_read_tokens: 0,
        cache_write_tokens: 0,
        avg_duration_ms: 1880,
      },
      {
        key: "unknown",
        requests: 6693,
        errors: 12,
        tokens_in: 0,
        tokens_out: 0,
        cache_read_tokens: 0,
        cache_write_tokens: 0,
        avg_duration_ms: null,
      },
    ],
    harness_labels: {
      opencode: "OpenCode",
      claude: "Claude Code",
      unknown: "Unknown",
    },
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
  "/admin/api/requests": {
    total: 480,
    rows: [
      { id: "req-explicit", harness: "opencode",
        ts_iso: "2026-09-02T10:00:00Z",
        endpoint: "/v1/messages",
        protocol: "anthropic_messages",
        provider: "nous_portal",
        key_label: "NOUS_API_KEY",
        requested_model: "claude-sonnet-4",
        resolved_model: "hermes-4-405b",
        status: "success",
        tokens_in: 120,
        tokens_out: 340,
        ttft_ms: 410,
        duration_ms: 2100,
      },
      { id: "req-ua", harness: "claude",
        ts_iso: "2026-09-02T10:00:00Z",
        endpoint: "/v1/messages",
        protocol: "anthropic_messages",
        provider: "nous_portal",
        key_label: "NOUS_API_KEY",
        requested_model: "claude-sonnet-4",
        resolved_model: "hermes-4-405b",
        status: "success",
        tokens_in: 120,
        tokens_out: 340,
        ttft_ms: 410,
        duration_ms: 2100,
      },
      { id: "req-none", harness: "unknown",
        ts_iso: "2026-09-02T10:00:00Z",
        endpoint: "/v1/messages",
        protocol: "anthropic_messages",
        provider: "nous_portal",
        key_label: "NOUS_API_KEY",
        requested_model: "claude-sonnet-4",
        resolved_model: "hermes-4-405b",
        status: "success",
        tokens_in: 120,
        tokens_out: 340,
        ttft_ms: 410,
        duration_ms: 2100,
      },
    ],
  },
  // The launcher stated what it is: the modal has to say so rather than
  // present an inference as a fact.
  "/admin/api/requests/req-explicit": {
    id: "req-explicit",
    harness: "opencode",
    // 7.47.0: frames mode ran and sent four empty deltas.
    keepalive_frames: 4,
    headers: {
      "x-mcc-harness": "opencode",
      "user-agent": "opencode/1.18.26",
    },
        ts_iso: "2026-09-02T10:00:00Z",
        endpoint: "/v1/messages",
        protocol: "anthropic_messages",
        provider: "nous_portal",
        key_label: "NOUS_API_KEY",
        requested_model: "claude-sonnet-4",
        resolved_model: "hermes-4-405b",
        status: "success",
        tokens_in: 120,
        tokens_out: 340,
        ttft_ms: 410,
        duration_ms: 2100,
  },
  // No header: the id was inferred from the user-agent, version and all.
  "/admin/api/requests/req-ua": {
    id: "req-ua",
    harness: "claude",
    // Frames mode ran and was never needed: a fact, so it is shown.
    keepalive_frames: 0,
    headers: { "user-agent": "claude-cli/2.0.14 (external, cli)" },
        ts_iso: "2026-09-02T10:00:00Z",
        endpoint: "/v1/messages",
        protocol: "anthropic_messages",
        provider: "nous_portal",
        key_label: "NOUS_API_KEY",
        requested_model: "claude-sonnet-4",
        resolved_model: "hermes-4-405b",
        status: "success",
        tokens_in: 120,
        tokens_out: 340,
        ttft_ms: 410,
        duration_ms: 2100,
  },
  // Nothing identified itself at all.
  "/admin/api/requests/req-none": {
    id: "req-none",
    harness: "unknown",
    headers: { "user-agent": "python-httpx" },
        ts_iso: "2026-09-02T10:00:00Z",
        endpoint: "/v1/messages",
        protocol: "anthropic_messages",
        provider: "nous_portal",
        key_label: "NOUS_API_KEY",
        requested_model: "claude-sonnet-4",
        resolved_model: "hermes-4-405b",
        status: "success",
        tokens_in: 120,
        tokens_out: 340,
        ttft_ms: 410,
        duration_ms: 2100,
  },
  // Every agent's tier state. `codex` overrides Best -- the state the card has
  // to render as a rail -- and every other agent follows the global chain,
  // which on this fixture has all five tiers collapsed onto MODEL.
  "/admin/api/harness-tiers": {
    tiers: [
      { id: "best", label: "Best", ref: "mcc/best", route_label: "Default", env_var: "MODEL", inherits_default: false,
        global: { primary: "nvidia_nim/one", fallbacks: ["open_router/two"], paused: [], paused_label: "MODEL_PAUSED", source: "global" } },
      { id: "good", label: "Good", ref: "mcc/good", route_label: "Opus", env_var: "MODEL_OPUS", inherits_default: true,
        global: { primary: "nvidia_nim/one", fallbacks: [], paused: [], paused_label: "MODEL_PAUSED", source: "global" } },
      { id: "medium", label: "Medium", ref: "mcc/medium", route_label: "Sonnet", env_var: "MODEL_SONNET", inherits_default: true,
        global: { primary: "nvidia_nim/one", fallbacks: [], paused: [], paused_label: "MODEL_PAUSED", source: "global" } },
      { id: "cheap", label: "Cheap", ref: "mcc/cheap", route_label: "Haiku", env_var: "MODEL_HAIKU", inherits_default: true,
        global: { primary: "nvidia_nim/one", fallbacks: [], paused: [], paused_label: "MODEL_PAUSED", source: "global" } },
      { id: "vision", label: "Vision", ref: "mcc/vision", route_label: "Vision", env_var: "MODEL_VISION", inherits_default: true,
        global: { primary: "nvidia_nim/one", fallbacks: [], paused: [], paused_label: "MODEL_PAUSED", source: "global" } },
    ],
    harnesses: {},
  },
  // The Proxying page. One provider that already has a chain of two proxies
  // plus Direct, and one subscription-login provider that has none -- the two
  // states the card has to render differently.
  "/admin/api/proxy-chains": {
    vocabulary: {
      policies: [
        { id: "single", help: "Always the first entry that is not paused." },
        { id: "round_robin", help: "Spreads requests across every healthy entry. This is the one that multiplies a per-address allowance." },
        { id: "least_used", help: "Picks the entry with the fewest requests so far." },
        { id: "failover", help: "Pins to the first healthy entry and moves only after a failure you selected. Four proxies still use one address until it fails." },
      ],
      default_policy: "failover",
      kinds: [
        { id: "invalid_request", state: "selectable", reason: "The request body is the problem." },
        { id: "model_rejected", state: "selectable", reason: "The model does not exist on that endpoint." },
        { id: "context_length", state: "selectable", reason: "A larger context window is the fix." },
        { id: "authentication", state: "refused", reason: "A new address does not fix a rejected key." },
        { id: "permission", state: "refused", reason: "A new address does not fix a refused scope." },
        { id: "quota", state: "recommended", reason: "" },
        { id: "rate_limit", state: "recommended", reason: "" },
        { id: "overloaded", state: "selectable", reason: "The provider is busy." },
        { id: "timeout", state: "recommended", reason: "" },
        { id: "upstream", state: "selectable", reason: "A fault on the provider's side." },
        { id: "unavailable", state: "selectable", reason: "A dead or refused connection." },
      ],
      default_kinds: ["quota", "rate_limit", "timeout"],
      scopes: ["provider", "credential"],
      max_entries: 12,
      switch_bound: { min: 1, max: 5, default: 2 },
      tls_intercepted: "intercepted",
      // The shipped answer: nothing is measuring these addresses until an
      // operator says so, and no exit-IP URL is named.
      checker: { enabled: false, interval_minutes: 30, exit_ip_configured: false },
      /* The readers this install ships. MCC ships no feed, so this is the
         whole of what the Add form can offer: formats, never sources. All
         seven, because the picker offers all seven and a fixture with two
         could not catch one being dropped from the list. */
      parsers: [
        { id: "proxyscrape", label: "JSON: proxies[] with ip_data", shape: "JSON with per-address metadata under a \"proxies\" key." },
        { id: "databay", label: "JSON: data[] with \"iso\" and \"ssl\"", shape: "JSON with per-address metadata under a \"data\" key." },
        { id: "geonode", label: "JSON: data[] with \"protocols\" and \"anonymityLevel\"", shape: "JSON with per-address metadata under a \"data\" key." },
        { id: "hproxy", label: "JSON array with \"protocols\" and \"alive\"", shape: "A plain JSON array of addresses." },
        { id: "proxifly", label: "JSON array with \"https\" and \"geolocation\"", shape: "A plain JSON array of addresses." },
        { id: "monosans", label: "JSON array with \"host\" and seconds", shape: "A plain JSON array of addresses keyed by \"host\"." },
        { id: "lines", label: "Plain text: one ip:port per line", shape: "Plain ip:port lines with # for a comment, read as http." },
      ],
      feed_name_max_length: 60,
      max_feeds: 20,
      /* What a press of Fetch is about to do, in the operator's own numbers.
         `candidates_max: 0` is the shipped default and means UNLIMITED -- it
         is here as 0 on purpose, because `Number(x) || 60` cannot tell 0 from
         absent and that exact mistake put a retired cap back on this page four
         times in 7.19.0. */
      fetch: {
        concurrency: 100,
        concurrency_mode: "fixed",
        check_depth: "tls",
        connect_timeout_seconds: 5.0,
        check_timeout_seconds: 10.0,
        candidates_max: 0,
      },
    },
    /* Feeds the OPERATOR added, because from 7.18.0 MCC ships none. This array
       was once missing from the fixture entirely, which is why nothing caught
       the page counting its own ticks -- the feed panel rendered empty in
       every test, so no test could press Fetch. It now carries the three
       states a row can be in, for the same reason: a form with no fixture rows
       is untested by construction.

       1. `fd_custom01` -- a feed somebody typed on this install, switched on.
       2. `databay`     -- one converted from a pre-7.18.0 built-in. It keeps
                           the id the old store used, which is what makes a
                           migrated row recognisable in the file itself.
       3. `fd_broken03` -- a feed whose parser this install does not ship
                           (`readable: false`). A hand-edited store, or a
                           reader retired in a later release. The row must ask
                           for a format rather than silently offering nothing,
                           and it must NOT be counted by the Fetch button. */
    feeds: [
      {
        id: "fd_custom01",
        name: "My mirror",
        url: "https://lists.example.com/socks5.json",
        parser: "proxifly",
        parser_shape: "A plain JSON array of addresses, each with one protocol, an https flag, an anonymity level and a country.",
        readable: true,
        observed: "A trial read found 214 addresses.",
        tls_strict: false,
        enabled: true,
      },
      {
        id: "databay",
        name: "Databay (TLS-strict)",
        url: "https://databay.com/api/v1/proxy-list?protocol=socks5&ssl=strict&limit=300",
        parser: "databay",
        parser_shape: 'JSON with per-address metadata under a "data" key: protocol, country as an ISO code, an SSL flag, latency, uptime and a last-checked time.',
        readable: true,
        observed: "JSON whose own ssl=strict filter selects for a tunnel that leaves the destination's certificate checkable.",
        tls_strict: true,
        enabled: false,
      },
      {
        id: "fd_broken03",
        name: "A list in a format MCC stopped reading",
        url: "https://lists.example.com/mystery.txt",
        parser: "",
        parser_shape: "",
        readable: false,
        observed: "",
        tls_strict: false,
        enabled: true,
      },
    ],
    /* Addresses on offer. This array was `[]`, which is the same shape of hole
       the missing `feeds` array was: with no candidate in the fixture no test
       could tick one, so a selection feature would have been untested by
       construction. Six is enough to prove every axis the page sorts and
       filters on -- feeds agreeing, scheme, latency, and an address the
       checker has already refused. */
    candidates: [
      {
        proxy: "px_cand0001", label: "203.0.113.21:8080", scheme: "http",
        source_count: 4,
        sources: [
          { id: "proxyscrape", name: "ProxyScrape" },
          { id: "hproxy", name: "HProxy" },
          { id: "databay", name: "Databay (TLS-strict)" },
          { id: "geonode", name: "Geonode" },
        ],
        country: "DE", anonymity: "elite", https_ok: true,
        latency_ms: 210, uptime_pct: 96, last_check: { at: "2026-09-17T12:00:00Z", ok: true, latency_ms: 190, tls: "strict", detail: "", exit_ip: "", depth: "tls" }, refused: false, working: true, checked_for: "nvidia_nim", checked_for_name: "NVIDIA NIM", untested: false,
      },
      {
        proxy: "px_cand0002", label: "203.0.113.22:1080", scheme: "socks5h",
        source_count: 2,
        sources: [
          { id: "proxyscrape", name: "ProxyScrape" },
          { id: "geonode", name: "Geonode" },
        ],
        country: "NL", anonymity: "anonymous", https_ok: true,
        latency_ms: 480, uptime_pct: 81, last_check: { at: "2026-09-17T12:00:00Z", ok: true, latency_ms: 420, tls: "strict", detail: "", exit_ip: "", depth: "tls" }, refused: false, working: true, checked_for: "nvidia_nim", checked_for_name: "NVIDIA NIM", untested: false,
      },
      {
        // The one the checker will catch terminating TLS when it is added.
        proxy: "px_cand0003", label: "203.0.113.23:3128", scheme: "http",
        source_count: 1,
        sources: [{ id: "proxyscrape", name: "ProxyScrape" }],
        country: "US", anonymity: "transparent", https_ok: false,
        latency_ms: 1200, uptime_pct: 40, last_check: { at: "2026-09-17T12:00:00Z", ok: true, latency_ms: 980, tls: "strict", detail: "", exit_ip: "", depth: "tls" }, refused: false, working: true, checked_for: "nvidia_nim", checked_for_name: "NVIDIA NIM", untested: false,
      },
      {
        // The one that simply will not answer: added, benched, routed around.
        proxy: "px_cand0004", label: "203.0.113.24:1080", scheme: "socks5h",
        source_count: 1,
        sources: [{ id: "hproxy", name: "HProxy" }],
        country: "", anonymity: "", https_ok: false,
        latency_ms: null, uptime_pct: null, last_check: null, refused: false, working: false, checked_for: "", checked_for_name: "", untested: true,
      },
      {
        proxy: "px_cand0005", label: "203.0.113.25:8080", scheme: "http",
        source_count: 3,
        sources: [
          { id: "proxyscrape", name: "ProxyScrape" },
          { id: "databay", name: "Databay (TLS-strict)" },
          { id: "geonode", name: "Geonode" },
        ],
        country: "FR", anonymity: "elite", https_ok: true,
        latency_ms: 95, uptime_pct: 99, last_check: { at: "2026-09-17T12:00:00Z", ok: true, latency_ms: 88, tls: "strict", detail: "", exit_ip: "", depth: "request" }, refused: false, working: true, checked_for: "nvidia_nim", checked_for_name: "NVIDIA NIM", untested: false,
      },
      {
        // Already refused by an earlier check, and still listed: an operator
        // whose offer list got shorter has to be able to see why.
        proxy: "px_cand0006", label: "203.0.113.26:8080", scheme: "http",
        source_count: 2,
        sources: [
          { id: "proxyscrape", name: "ProxyScrape" },
          { id: "hproxy", name: "HProxy" },
        ],
        country: "SG", anonymity: "elite", https_ok: true,
        latency_ms: 300, uptime_pct: 70,
        last_check: {
          at: new Date(Date.now() - 900000).toISOString(), ok: false,
          latency_ms: 77, tls: "intercepted",
          detail: "this proxy breaks certificate validation -- MCC will not route through it",
          exit_ip: "",
        },
        refused: true,
        // Refused, and therefore NOT on the offer any more since 7.21.0. It is
        // in the fixture because a store that already held one has to render
        // without the page assuming every row is working.
        working: false,
        checked_for: "nvidia_nim",
        checked_for_name: "NVIDIA NIM",
        untested: false,
      },
    ],
    /* The durably-refused addresses, which are counted by a fetch and, since
       7.35.1, listed. They are NOT candidates -- the store keeps them so a
       later sweep does not offer them again -- so they arrive on their own
       key, carrying only what a person needs to recognise one: what it was,
       why it was refused, and when. Nothing here is a handle an Add gesture
       reads, and the tests check that no such gesture appears beside them. */
    refused_addresses: [
      {
        proxy: "px_ref0001",
        label: "198.51.100.70:8080",
        scheme: "http",
        reason:
          "this proxy breaks certificate validation -- MCC will not route through it",
        at: new Date(Date.now() - 1800000).toISOString(),
        checked_for: "nvidia_nim",
        checked_for_name: "NVIDIA NIM",
      },
      {
        proxy: "px_ref0002",
        label: "198.51.100.71:1080",
        scheme: "socks5h",
        reason:
          "this proxy breaks certificate validation -- MCC will not route through it",
        at: new Date(Date.now() - 7200000).toISOString(),
        checked_for: "nvidia_nim",
        checked_for_name: "NVIDIA NIM",
      },
    ],
    // Addresses in a chain that got there by passing a check. It is what tells
    // "every address that passed is already in a chain" apart from "you
    // discarded them", and the page must read it as a number, not truthiness.
    chained_passing: 1,
    providers: [
      {
        provider_id: "nvidia_nim",
        display_name: "NVIDIA NIM",
        group: "inference",
        custom: false,
        oauth: false,
        key_count: 2,
        env_var: "NVIDIA_NIM_PROXY",
        // A destination to test an address against. Without one no provider is
        // offered as a destination at all, which is a real state of this page
        // and the reason the second provider below deliberately has none.
        base_url: "https://integrate.api.nvidia.com/v1",
        inherited_label: "203.0.113.7:1080",
        inherited_scheme: "socks5h",
        chain: {
          enabled: true,
          policy: "failover",
          scope: "provider",
          max_switches: 2,
          on: ["quota", "rate_limit", "timeout"],
          oauth_acknowledged: false,
          entries: [
            { proxy: "px_aaaa1111", paused: false, direct: false, label: "203.0.113.7:1080", scheme: "socks5h", source: "manual", source_count: 1,
              refused: false,
              last_check: { at: new Date(Date.now() - 120000).toISOString(), ok: true, latency_ms: 412, tls: "strict", detail: "", exit_ip: "" },
              health: { state: "healthy", checked: true, requests: 9, successes: 8, failures: 1, cooldown_remaining: 0, refused: false, reason: null } },
            { proxy: "px_bbbb2222", paused: false, direct: false, label: "198.51.100.9:8080", scheme: "http", source: "manual", source_count: 1,
              refused: false,
              last_check: { at: new Date(Date.now() - 600000).toISOString(), ok: false, latency_ms: null, tls: "unknown", detail: "198.51.100.9:8080 refused the connection", exit_ip: "" },
              health: { state: "unreachable", checked: true, requests: 3, successes: 0, failures: 3, cooldown_remaining: 300, refused: false, reason: "ConnectTimeout -- benched 300s" } },
            // The address the checker caught terminating TLS. Still on the
            // card, struck through and refused: an operator whose chain got
            // shorter has to be able to see why.
            { proxy: "px_cccc3333", paused: false, direct: false, label: "192.0.2.44:3128", scheme: "http", source: "manual", source_count: 1,
              refused: true,
              last_check: { at: new Date(Date.now() - 300000).toISOString(), ok: false, latency_ms: 88, tls: "intercepted", detail: "this proxy breaks certificate validation -- MCC will not route through it", exit_ip: "" },
              health: { state: "intercepted", checked: true, requests: 0, successes: 0, failures: 1, cooldown_remaining: 0, refused: true, reason: "this proxy breaks certificate validation -- MCC will not route through it" } },
            { proxy: "", paused: false, direct: true, label: "", scheme: "", source: "", source_count: 0,
              refused: false, last_check: null,
              health: { state: "unknown", checked: false, requests: 0, successes: 0, failures: 0, cooldown_remaining: 0, refused: false, reason: null } },
          ],
        },
      },
      {
        provider_id: "chatgpt_oauth",
        display_name: "ChatGPT (OAuth)",
        group: "subscription",
        custom: false,
        oauth: true,
        key_count: 1,
        env_var: "CHATGPT_OAUTH_PROXY",
        // No https base URL, so there is nothing to verify a stranger's tunnel
        // against and this provider is not offered as a destination. The page
        // has to say why rather than silently dropping it from the picker.
        base_url: "",
        inherited_label: "",
        inherited_scheme: "",
        chain: null,
      },
    ],
  },
  "/admin/api/desktop-apps": {
    apps: [
      { id: "codex_desktop", display_name: "Codex desktop",
        summary: "Codex desktop and CLI share one config document.",
        status: "servable", doc_url: "https://example.invalid/codex",
        unavailable_reason: "", protocol: "openai_chat_completions",
        base_url: "http://127.0.0.1:8082/v1", token_form: "env_name_field",
        token_reference: "", token_env_var: "MCC_AUTH_TOKEN",
        attribution_header: "http_headers", restart_required: true,
        open_command: "codex", notes: ["Codex has no UI for picking a model."],
        instruction_fields: [], owned_key: "model_providers.mcc",
        display_path: "~/.codex/config.toml", sidecar_path: "",
        overwrites: ["model_provider", "model"], sets_default_model: true,
        probe: { state: "configured", document_path: "/home/u/.codex/config.toml",
          document_exists: true, error: "", token_env_present: true,
          restorable: true } },
      { id: "opencode_desktop", display_name: "OpenCode desktop",
        summary: "OpenCode expands {env:...} references.",
        status: "servable", doc_url: "https://example.invalid/opencode",
        unavailable_reason: "", protocol: "openai_chat_completions",
        base_url: "http://127.0.0.1:8082/v1", token_form: "env_reference",
        token_reference: "{env:MCC_AUTH_TOKEN}", token_env_var: "MCC_AUTH_TOKEN",
        attribution_header: "headers", restart_required: true,
        open_command: "opencode", notes: [], instruction_fields: [],
        owned_key: "provider.mcc", display_path: "%APPDATA%/opencode/opencode.json",
        sidecar_path: "", overwrites: [], sets_default_model: false,
        probe: { state: "drifted", document_path: "/home/u/opencode.json",
          document_exists: true, error: "", token_env_present: false,
          restorable: false } },
      { id: "goose_desktop", display_name: "Goose desktop",
        summary: "MCC owns a whole file and names it in config.yaml.",
        status: "servable", doc_url: "https://example.invalid/goose",
        unavailable_reason: "", protocol: "openai_chat_completions",
        base_url: "http://127.0.0.1:8082", token_form: "env_only",
        token_reference: "", token_env_var: "MCC_AUTH_TOKEN",
        attribution_header: "", restart_required: true, open_command: "goose",
        notes: ["Goose ignores API keys written into config.yaml."],
        instruction_fields: [], owned_key: "",
        display_path: "%APPDATA%/Block/goose/config/config.yaml",
        sidecar_path: "%APPDATA%/Block/goose/config/custom_providers/mcc.json",
        overwrites: ["GOOSE_PROVIDER"], sets_default_model: false,
        probe: { state: "installed", document_path: "/home/u/config.yaml",
          document_exists: false, error: "", token_env_present: false,
          restorable: false } },
      { id: "crush_desktop", display_name: "Crush",
        summary: "Charm's Crush expands $VAR.", status: "servable",
        doc_url: "https://example.invalid/crush", unavailable_reason: "",
        protocol: "openai_chat_completions", base_url: "http://127.0.0.1:8082/v1",
        token_form: "env_reference", token_reference: "$MCC_AUTH_TOKEN",
        token_env_var: "MCC_AUTH_TOKEN", attribution_header: "extra_headers",
        restart_required: true, open_command: "crush", notes: [],
        instruction_fields: [], owned_key: "providers.mcc",
        display_path: "~/.config/crush/crush.json", sidecar_path: "",
        overwrites: [], sets_default_model: false,
        probe: { state: "not_installed", document_path: "/home/u/crush.json",
          document_exists: false, error: "", token_env_present: false,
          restorable: false } },
      // Servable since 6.56.0, against the configuration library Anthropic's
      // MDM page documents. MCC owns a whole document there and merges one
      // foreign key of _meta.json.
      { id: "claude_desktop", display_name: "Claude Desktop",
        summary: "Claude Desktop has a native gateway mode.",
        status: "servable", doc_url: "https://example.invalid/claude",
        unavailable_reason: "", protocol: "anthropic_messages",
        base_url: "http://127.0.0.1:8082", token_form: "mcc_owned_file",
        token_reference: "", token_env_var: "",
        attribution_header: "inferenceCustomHeaders", restart_required: true,
        open_command: "",
        notes: ["Relaunch Claude Desktop to load it."],
        instruction_fields: [
          { label: "Inference provider", value: "Gateway" },
          { label: "Gateway base URL", value: "http://127.0.0.1:8082" },
          { label: "Gateway auth scheme", value: "Bearer" },
          { label: "Credential kind", value: "Static API key" },
          { label: "Custom headers", value: "x-mcc-harness: claude_desktop" },
        ],
        owned_key: 'entries[id == "mcc-9c2f"]',
        display_path: "%LOCALAPPDATA%/Claude-3p/configLibrary/_meta.json",
        sidecar_path: "%LOCALAPPDATA%/Claude-3p/configLibrary/mcc-9c2f.json",
        sidecar_keys: [
          "inferenceProvider", "inferenceGatewayBaseUrl",
          "inferenceGatewayApiKey", "inferenceCredentialKind",
          "modelDiscoveryEnabled", "inferenceCustomHeaders",
        ],
        managed_source_labels: [
          "Machine policy (HKLM\\SOFTWARE\\Policies\\Claude)",
        ],
        overwrites: ["appliedId"], sets_default_model: false,
        probe: { state: "configured",
          document_path: "/home/u/Claude-3p/configLibrary/_meta.json",
          document_exists: true, error: "", token_env_present: false,
          restorable: true, managed_by: "", managed_keys: [] } },
      // A machine whose organisation manages the app: the card has no button
      // at all, and says what is enforcing that.
      { id: "roo_code", display_name: "Roo Code (VS Code)",
        summary: "Roo Code publishes an import hook.",
        status: "servable", doc_url: "https://example.invalid/roo",
        unavailable_reason: "", protocol: "openai_chat_completions",
        base_url: "http://127.0.0.1:8082/v1", token_form: "mcc_owned_file",
        token_reference: "", token_env_var: "", attribution_header: "",
        restart_required: false, open_command: "code", notes: [],
        instruction_fields: [], owned_key: "roo-cline.autoImportSettingsPath",
        display_path: "%APPDATA%/Code/User/settings.json",
        sidecar_path: "<MCC config dir>/roo-code-settings.json",
        sidecar_keys: [], managed_source_labels: ["Device policy"],
        overwrites: ["roo-cline.autoImportSettingsPath"],
        sets_default_model: false,
        probe: { state: "managed", document_path: "/home/u/settings.json",
          document_exists: false, error: "", token_env_present: false,
          restorable: false,
          managed_by: "Machine policy (HKLM\\SOFTWARE\\Policies\\Claude)",
          managed_keys: ["inferenceProvider"] } },
      // 6.84.0's two new probe states, and the demotion that goes with them.
      // A file MCC wrote whose credential cannot resolve is not configured,
      // and keys that vanished with nobody pressing Undo are the application
      // removing them -- both used to be reported as something else.
      { id: "goose_unresolved", display_name: "Goose desktop (second machine)",
        summary: "Written, and the keyring entry is not there.",
        status: "servable", doc_url: "https://example.invalid/goose",
        unavailable_reason: "", instructions_reason: "",
        protocol: "openai_chat_completions", base_url: "http://127.0.0.1:8082",
        token_form: "env_only", token_reference: "",
        token_env_var: "MCC_AUTH_TOKEN", attribution_header: "",
        restart_required: true, open_command: "goose", notes: [],
        instruction_fields: [], owned_key: "",
        display_path: "%APPDATA%/Block/goose/config/config.yaml",
        sidecar_path: "%APPDATA%/Block/goose/config/custom_providers/mcc.json",
        sidecar_keys: [], managed_source_labels: [],
        overwrites: ["GOOSE_PROVIDER"], sets_default_model: false,
        probe: { state: "credential_unresolved",
          document_path: "/home/u/config.yaml", document_exists: true,
          error: "", token_env_present: false, restorable: true } },
      { id: "codex_removed", display_name: "Codex desktop (second machine)",
        summary: "The application rewrote its own configuration.",
        status: "servable", doc_url: "https://example.invalid/codex",
        unavailable_reason: "", instructions_reason: "",
        protocol: "openai_chat_completions",
        base_url: "http://127.0.0.1:8082/v1",
        token_form: "literal_in_app_file", token_reference: "",
        token_env_var: "", attribution_header: "http_headers",
        restart_required: true, open_command: "codex", notes: [],
        instruction_fields: [], owned_key: "model_providers.mcc",
        display_path: "~/.codex/config.toml", sidecar_path: "",
        sidecar_keys: [], managed_source_labels: [],
        overwrites: ["model_provider", "model"], sets_default_model: true,
        probe: { state: "removed_by_app",
          document_path: "/home/u/.codex/config.toml", document_exists: true,
          error: "", token_env_present: false, restorable: true } },
      { id: "antigravity", display_name: "Antigravity (agy CLI)",
        summary: "The endpoint and the key are environment variables.",
        status: "instructions_only", doc_url: "https://example.invalid/agy",
        unavailable_reason: "",
        instructions_reason: "2026-09-12: demoted from a Configure button. agy takes its key from GEMINI_API_KEY and its endpoint from GOOGLE_GEMINI_BASE_URL, and MCC never sets a user-scope variable.",
        protocol: "gemini", base_url: "http://127.0.0.1:8082/v1beta",
        token_form: "env_only", token_reference: "",
        token_env_var: "GEMINI_API_KEY", attribution_header: "",
        restart_required: true, open_command: "agy",
        notes: ["The Antigravity IDE lists custom endpoints as unsupported."],
        instruction_fields: [
          { label: "~/.gemini/antigravity-cli/settings.json",
            value: '{"modelProvider": "gemini"}' },
          { label: "GEMINI_API_KEY", value: "your MCC proxy token" },
          { label: "GOOGLE_GEMINI_BASE_URL", value: "http://127.0.0.1:8082" },
        ],
        owned_key: "", display_path: "~/.gemini/antigravity-cli/settings.json",
        sidecar_path: "", sidecar_keys: [], managed_source_labels: [],
        overwrites: ["modelProvider"], sets_default_model: false,
        probe: { state: "installed",
          document_path: "/home/u/.gemini/antigravity-cli/settings.json",
          document_exists: false, error: "", token_env_present: false,
          restorable: false } },
      { id: "warp", display_name: "Warp",
        summary: "Rejects loopback and private addresses.",
        status: "not_routable", doc_url: "https://example.invalid/warp",
        unavailable_reason: "Warp rejects 127.0.0.1 and private IP ranges outright. Verified 2026-09-07.",
        protocol: "openai_chat_completions", base_url: "http://127.0.0.1:8082/v1",
        token_form: "env_reference", token_reference: "", token_env_var: "",
        attribution_header: "", restart_required: true, open_command: "",
        notes: [], instruction_fields: [], owned_key: "", display_path: "",
        sidecar_path: "", overwrites: [], sets_default_model: false,
        probe: { state: "not_routable", document_path: "", document_exists: false,
          error: "", token_env_present: false, restorable: false } },
    ],
  },
  "/admin/api/desktop-apps/codex_desktop/plan": {
    document_path: "/home/u/.codex/config.toml",
    diff: "--- a/config.toml\n+++ b/config.toml\n+[model_providers.mcc]\n+base_url = \"http://127.0.0.1:8082/v1\"\n+env_key = \"MCC_AUTH_TOKEN\"\n",
    sidecar_path: "", sidecar_diff: "",
    overwritten_keys: ["model_provider", "model"], no_op: false,
    actions: ["Restart Codex desktop to pick this up."],
  },
  "/admin/api/requests/harness-usage": {
    enabled: true,
    days: 7,
    counts: { opencode: 12480, claude: 3120, codex: 0 },
    labels: { opencode: "OpenCode", claude: "Claude Code", codex: "Codex CLI" },
  },
  "/admin/api/requests/lifetime": {
    enabled: true,
    by_model: [],
    by_provider: [],
    requests: 333838,
    first_day: "2026-08-01",
    last_day: "2026-09-12",
    // What the log costs: a readout, never a cap.
    storage: {
      rows: 333838,
      bytes: 4502450176,
      bytes_by_file: { database: 4501643264, wal: 774144, shm: 32768 },
      path: "/home/user/.mcc/logs/requests.db",
    },
  },
  // 7.43.0: requests by folder and by session. Fake paths and ids; one
  // session with two folders so the "+1" form renders, one with none.
  "/admin/api/requests/origin": {
    enabled: true,
    by_folder: [
      {
        key: "C:\\Users\\devuser\\Projects\\demo",
        short: "Projects\\demo \u00b7 #76b11b",
        requests: 9, errors: 1, tokens_in: 900, tokens_out: 90,
        sessions: 2, last_ts: 1790000000,
      },
      {
        key: "D:\\work\\games\\Phone games",
        short: "games\\Phone games \u00b7 #8fa073",
        requests: 3, errors: 0, tokens_in: 30, tokens_out: 3,
        sessions: 1, last_ts: 1789990000,
      },
    ],
    by_folder_truncated: false,
    by_session: [
      {
        key: "0f3c2a1b-6d5e-4f70-9a8b-1c2d3e4f5a6b", short: "0f3c2a1b",
        requests: 9, errors: 1, tokens_out: 90, subagent_requests: 5,
        subagents: 2, folders: 2,
        folder: "C:\\Users\\devuser\\Projects\\demo",
        folder_short: "Projects\\demo \u00b7 #76b11b", last_ts: 1790000000,
      },
      {
        key: "c4d5e6f7-0819-4a2b-9c3d-4e5f60718293", short: "c4d5e6f7",
        requests: 3, errors: 0, tokens_out: 3, subagent_requests: 0,
        subagents: 0, folders: 0, folder: null, folder_short: null,
        last_ts: 1789990000,
      },
    ],
    by_session_truncated: true,
  },
  "/admin/api/requests/cost": {
    enabled: true,
    cost_estimation_enabled: true,
    cost_estimation_mode: "auto",
    cost_source_litellm_enabled: false,
    // Every sum ships its denominator: 179,897 of 333,838 priced.
    totals: {
      reported_usd: 1.5,
      estimated_usd: 240.25,
      priced: 179897,
      requests: 333838,
    },
    by_source: [{ key: "models_dev", cost_usd: 240.25, requests: 179877 }],
    by_provider: [],
    by_model: [],
    by_harness: [],
    by_day: [],
    harness_labels: {},
  },
  /* Per-model latency, with the awkward cases on purpose: one model measured
     on both outcomes (so the failed group cannot be merged into the answered
     one), one model whose thousands of attempts carry no measurement at all
     (every row written before 7.4.0), and a `sampled` p50 source so the note
     has to say the percentiles are close rather than exact. */
  "/admin/api/requests/latency": {
    enabled: true,
    window_since: null,
    models: 2,
    attempts: 4431,
    ttft_measured: 14,
    p50_source: "sampled",
    stale: true,
    computed_at: 1788500000,
    rows: [
      {
        model_ref: "beta/model-07",
        outcome: "failed",
        attempts: 4416,
        ttft_measured: 0,
        avg_ttft_ms: null,
        avg_first_reasoning_ms: null,
        avg_generating_ms: null,
        tokens_out: null,
        p50_ttft_ms: null,
        p95_ttft_ms: null,
        p50_source: "sampled",
      },
      {
        model_ref: "alpha/model-02",
        outcome: "succeeded",
        attempts: 12,
        ttft_measured: 12,
        avg_ttft_ms: 340.0,
        avg_first_reasoning_ms: 520.0,
        avg_generating_ms: 1600.0,
        tokens_out: 4080,
        p50_ttft_ms: 312.0,
        p95_ttft_ms: 1400.0,
        p50_source: "sampled",
      },
      {
        model_ref: "alpha/model-02",
        outcome: "failed",
        attempts: 3,
        ttft_measured: 2,
        avg_ttft_ms: 9100.0,
        avg_first_reasoning_ms: null,
        avg_generating_ms: 41000.0,
        tokens_out: null,
        p50_ttft_ms: 9000.0,
        p95_ttft_ms: 12000.0,
        p50_source: "sampled",
      },
    ],
  },
  /* The fallback request the whole feature is about: two models that did not
     answer, one that did in 300 ms, and a fourth that was benched out before
     the request began. The request waited 4,712 ms; the winner took 300. */
  /* The worked example from the investigation, as a fixture: a first frame at
     16.7 s, then 296.7 s of nothing, then the client's 300 s idle watchdog.
     `output_chars` is 0 -- MCC had committed a response and gone quiet, which
     is neither "the user pressed Esc" nor "nothing ever arrived". */
  "/admin/api/requests/req-cancelled": {
    id: "req-cancelled",
    harness: "claude",
    headers: {},
    ts_iso: "2026-09-20T05:22:00Z",
    endpoint: "/v1/messages",
    protocol: "anthropic_messages",
    provider: "custom_agnes",
    key_label: "AGNES_API_KEY",
    requested_model: "claude-fable-5.1",
    resolved_model: "custom_agnes/agnes-3.0-flash",
    status: "cancelled",
    cancel_reason: "committed_then_silent",
    tokens_in: 120,
    tokens_out: null,
    ttft_ms: 16700,
    ttft_winner_ms: 16700,
    duration_ms: 313400,
    output_chars: 0,
    thinking_chars: 0,
    route_attempt: 1,
    route_attempts: [
      {
        attempt: 0,
        provider: "custom_agnes",
        model_ref: "custom_agnes/agnes-3.0-pro",
        outcome: "failed",
        duration_ms: 3000,
        ttft_ms: null,
        first_reasoning_ms: null,
        tokens_out: null,
        error_kind: "upstream_status",
        error_message: "503 from the host",
        params: null,
        wire_body: null,
        reasoning_emitted: null,
        key_index: 0,
        key_label: "AGNES_API_KEY",
      },
      {
        attempt: 1,
        provider: "custom_agnes",
        model_ref: "custom_agnes/agnes-3.0-flash",
        // Stored as `failed`, because from the route's point of view this
        // model did not answer -- but `interrupted` is what actually ended it.
        outcome: "failed",
        duration_ms: 310000,
        ttft_ms: 16700,
        first_reasoning_ms: null,
        tokens_out: null,
        error_kind: "interrupted",
        error_message: "client cancelled before the stream finished",
        params: null,
        wire_body: null,
        reasoning_emitted: null,
        key_index: 0,
        key_label: "AGNES_API_KEY",
      },
    ],
    input_images: [],
  },
  "/admin/api/requests/req-fallback": {
    id: "req-fallback",
    harness: "claude",
    headers: {},
    ts_iso: "2026-09-13T10:00:00Z",
    endpoint: "/v1/messages",
    protocol: "anthropic_messages",
    provider: "commandcode",
    key_label: "CC_API_KEY",
    requested_model: "claude-sonnet-4",
    resolved_model: "commandcode/kimi-k3",
    status: "success",
    tokens_in: 120,
    tokens_out: 340,
    ttft_ms: 4712,
    ttft_winner_ms: 300,
    // The whole request, predecessors included -- deliberately NOT the
    // winner's 2,100 ms, so a rate computed from the request's duration and
    // the winner's TTFT is visibly the wrong number (52.2 tok/s against the
    // 188.9 the model actually produced).
    duration_ms: 6812,
    route_attempt: 2,
    route_attempts: [
      {
        attempt: 0,
        provider: "commandcode",
        model_ref: "commandcode/alpha-one",
        outcome: "failed",
        duration_ms: 3000,
        ttft_ms: null,
        first_reasoning_ms: null,
        tokens_out: null,
        error_kind: "upstream_timeout",
        error_message: "no first token in 3s",
        params: null,
        wire_body: null,
        reasoning_emitted: null,
        key_index: 0,
        key_label: "CC_API_KEY",
      },
      {
        attempt: 1,
        provider: "commandcode",
        model_ref: "commandcode/beta-two",
        outcome: "failed",
        duration_ms: 1500,
        ttft_ms: 1412,
        first_reasoning_ms: null,
        tokens_out: null,
        error_kind: "upstream_status",
        error_message: "503 from the host",
        params: null,
        wire_body: null,
        reasoning_emitted: null,
        key_index: 0,
        key_label: "CC_API_KEY",
      },
      {
        attempt: 2,
        provider: "commandcode",
        model_ref: "commandcode/kimi-k3",
        outcome: "succeeded",
        duration_ms: 2100,
        ttft_ms: 300,
        first_reasoning_ms: 460,
        tokens_out: 340,
        error_kind: null,
        error_message: null,
        params: null,
        wire_body: null,
        reasoning_emitted: 1,
        key_index: 0,
        key_label: "CC_API_KEY",
      },
      {
        attempt: 3,
        provider: "commandcode",
        model_ref: "commandcode/benched-four",
        outcome: "skipped",
        duration_ms: null,
        ttft_ms: null,
        first_reasoning_ms: null,
        tokens_out: null,
        error_kind: null,
        error_message: null,
        params: {
          bench: {
            mode: "rate_based",
            window: 20,
            rate: 0.5,
            failures: 12,
            last_kind: "upstream_status",
            last_status: 429,
            remaining_seconds: 240,
            since: 600,
          },
        },
        wire_body: null,
        reasoning_emitted: null,
        key_index: 0,
        key_label: "CC_API_KEY",
      },
    ],
  },
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
  ROUTES["/admin/api/requests/harness-usage"] = { enabled: false, counts: {}, labels: {} };
  ROUTES["/admin/api/rtk"] = { installed: false };
  ROUTES["/admin/api/harnesses"] = { harnesses: [] };
  ROUTES["/admin/api/rtk/gain"] = {
    available: false,
    reason: "not_installed",
    detail: "No RTK binary was found.",
  };
}

/* The key listing sends each row its own health; the older positional
   `health` array is still sent beside it. Mirror that here rather than
   duplicating the objects, so a stub can never disagree with itself. */
for (const path of [
  "/admin/api/credentials/SCOPED_API_KEY/keys",
  "/admin/api/credentials/CREDITS_API_KEY/keys",
  "/admin/api/credentials/PLAIN_API_KEY/keys",
]) {
  ROUTES[path].rows.forEach((row, index) => {
    row.health = ROUTES[path].health[index] || null;
  });
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
/* A 2D context that does nothing but answer every call.
 *
 * Not cosmetic: jsdom's getContext() returns null, and prepareCanvas() calls
 * ctx.setTransform() on it -- so `renderReqModelChart` threw and took the rest
 * of loadRequestsView() down with it. Everything after the two charts (the
 * filter datalists, all six breakdown panels and the request table itself) was
 * silently never rendered, and no test could see any of it. */
function stubCanvasContext() {
  const written = new Map();
  return new Proxy(
    {},
    {
      get(_target, prop) {
        if (written.has(prop)) return written.get(prop);
        if (prop === "measureText") return () => ({ width: 0 });
        if (prop === "createLinearGradient" || prop === "createRadialGradient") {
          return () => ({ addColorStop() {} });
        }
        return () => {};
      },
      set(_target, prop, value) {
        written.set(prop, value);
        return true;
      },
    },
  );
}
window.HTMLCanvasElement.prototype.getContext = function getContext() {
  if (!this._stubContext) this._stubContext = stubCanvasContext();
  return this._stubContext;
};
window.MutationObserver = window.MutationObserver || NoopObserver;
window.scrollTo = () => {};
// jsdom ships no clipboard, and addCopyButton returns without appending a
// button when writeText is missing -- so without this stub the copy controls
// the Coding agents page is built around would be invisible to every test.
if (!window.navigator.clipboard) {
  Object.defineProperty(window.navigator, "clipboard", {
    configurable: true,
    value: { writeText: () => Promise.resolve() },
  });
}
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
// jsdom has no layout engine and does not implement scrollIntoView, so the
// real one would throw where the page scrolls to a Guide anchor. Recording
// the calls is also the only way to prove a Guide link *did* scroll, which is
// half of what that link is for.
const scrolledTo = [];
window.Element.prototype.scrollIntoView = function scrollIntoViewStub() {
  scrolledTo.push(this.id || this.className || this.tagName);
};
const fetchCalls = [];
/* The in-flight registry, emulated: `INFLIGHT.rows` is the server's list,
   oldest first, and the answer honours `limit` the way the route does --
   `total` counts every row, `rows` carries at most `limit`. */
const INFLIGHT = { enabled: true, rows: [] };
const inflightUrls = [];
function inflightAnswer(url) {
  if (!INFLIGHT.enabled) return { enabled: false };
  const limit = Number(new URL(url, "http://127.0.0.1").searchParams.get("limit")) || 200;
  const rows = INFLIGHT.rows.slice(0, limit);
  return {
    enabled: true,
    total: INFLIGHT.rows.length,
    shown: rows.length,
    truncated: rows.length < INFLIGHT.rows.length,
    reaped: 0,
    now_mono: 5000,
    rows,
  };
}
// The pause write is emulated in the fetch stub below, so the seven lists have
// to live somewhere the stub can read and update between calls.
const PAUSE_KEY_BY_MODEL = {
  MODEL: "MODEL_PAUSED",
  MODEL_MYTHOS: "MODEL_MYTHOS_PAUSED",
  MODEL_FABLE: "MODEL_FABLE_PAUSED",
  MODEL_OPUS: "MODEL_OPUS_PAUSED",
  MODEL_SONNET: "MODEL_SONNET_PAUSED",
  MODEL_HAIKU: "MODEL_HAIKU_PAUSED",
  MODEL_VISION: "MODEL_VISION_PAUSED",
};
const pausedByKey = new Map();
const fetchUrls = [];
const fetchBodies = [];
/* The server's one undo point for the candidate list, emulated. It is the
   whole reason a selection can be sent in several batches and still be undone
   as one gesture: the first batch mints the token and keeps the document as it
   was, and every batch carrying that token back extends the same point. */
const PROXY_UNDO = { token: "", before: null, after: null };

/* The fetch job, as the server would hold it. Idle until something presses
   Fetch, which is what a fresh server process reports and what the page has to
   render as "no fetch has run" rather than as "0 of 0". */
const PROXY_FETCH = {
  job: "",
  state: "idle",
  provider: "",
  detail: "",
  at: "2026-09-17T12:00:00Z",
  elapsed_seconds: 0,
  stopping: false,
  feeds_total: 0,
  feeds_read: 0,
  total: 0,
  tested: 0,
  working: 0,
  dead: 0,
  refused: 0,
  offered: 0,
  corroborated: 0,
  persisted: 0,
  concurrency: 0,
  concurrency_mode: "",
  concurrency_requested: 0,
  concurrency_summary: "",
  concurrency_note: "",
  check_depth: "",
  feeds: [],
};

/* One step of the sweep per status poll, so the progress line the page prints
   is driven by numbers that actually move. Two hundred addresses a step, and
   the one that finishes the run puts the tested-and-working addresses on the
   offer list -- which is the whole change: an address is offered because it
   passed, not because a list published it. */
function proxyFetchStatePayload(advance) {
  const state = ROUTES["/admin/api/proxy-chains"];
  if (advance && PROXY_FETCH.state === "running") {
    PROXY_FETCH.tested = Math.min(PROXY_FETCH.total, PROXY_FETCH.tested + 200);
    PROXY_FETCH.working = Math.round(PROXY_FETCH.tested * 0.05);
    PROXY_FETCH.refused = PROXY_FETCH.tested >= 200 ? 2 : 0;
    PROXY_FETCH.dead =
      PROXY_FETCH.tested - PROXY_FETCH.working - PROXY_FETCH.refused;
    PROXY_FETCH.elapsed_seconds += 1.5;
    /* The server resolves the pace once the lists are in and reports it every
       poll, so the page prints what is happening rather than what is set. */
    PROXY_FETCH.concurrency = 96;
    PROXY_FETCH.concurrency_mode = "percent";
    PROXY_FETCH.concurrency_requested = 6;
    PROXY_FETCH.concurrency_summary = "testing 96 at a time (6% of 1,592)";
    PROXY_FETCH.check_depth = "tls";
    if (PROXY_FETCH.tested >= PROXY_FETCH.total) PROXY_FETCH.state = "done";
  }
  return JSON.parse(
    JSON.stringify({
      ...state,
      fetch: { ...PROXY_FETCH, provider_name: "NVIDIA NIM" },
    }),
  );
}

// Pause-route fault injection. Null means the route behaves normally.
let pauseRefusal = null;
let pauseHttpFailure = null;
let pauseGate = null;
// POST and GET share /admin/api/custom-providers, so the create response is
// emulated rather than routed. It starts as the failure shape because the
// contract under test is that a failed discovery cannot render as a healthy
// card.
let customCreateResult = {
  provider_id: "custom_bad_ai",
  display_name: "Bad AI",
  model_count: 0,
  models: [],
  test_error: "PermissionDeniedError",
  discovery: {
    provider_id: "custom_bad_ai",
    ok: false,
    model_count: 0,
    error_type: "PermissionDeniedError",
    message: "query failure: PermissionDeniedError",
  },
};
// Held open by the cost-panel block below: the whole point of taking the cost
// breakdown out of the Analytics `Promise.all` is that a slow cost answer must
// not delay the paint, and a stub that resolves instantly cannot show that.
const slowRoutes = new Map();
window.fetch = async (url, options = {}) => {
  // 7.45.0: the in-flight panel polls on its own timer for the whole run.
  // Its calls are recorded apart, so no other capture's "which calls did this
  // gesture make" window can catch a poll that merely happened to land in it.
  if (String(url).startsWith("/admin/api/requests/in-flight")) {
    inflightUrls.push(String(url));
    const answer = inflightAnswer(String(url));
    return {
      ok: true,
      status: 200,
      json: async () => JSON.parse(JSON.stringify(answer)),
      text: async () => JSON.stringify(answer),
    };
  }
  fetchCalls.push(String(url).split("?")[0]);
  const gate = slowRoutes.get(String(url).split("?")[0]);
  if (gate) await gate;
  // The query string is the whole point for the analytics filters: which
  // filter went out, and whether the page reset to offset 0.
  fetchUrls.push(String(url));
  if (options && options.body) {
    // The method is recorded because order and verb are the claim in the feed
    // flow: a save (PUT) has to precede the read (POST), or the read asks
    // about a selection the store was never told.
    const method = String((options && options.method) || "GET").toUpperCase();
    try {
      fetchBodies.push({
        path: String(url).split("?")[0],
        method,
        body: JSON.parse(options.body),
      });
    } catch {
      fetchBodies.push({ path: String(url).split("?")[0], method, body: null });
    }
  }
  let body = routeFor(url);
  if (
    String(url).split("?")[0] === "/admin/api/custom-providers" &&
    (options.method || "GET").toUpperCase() === "POST"
  ) {
    body = customCreateResult;
  }
  /* The feed routes, emulated against the same document the page reads back,
     because that is the whole point of the pair: the save writes `enabled` on
     the store and the ingest REFUSES when nothing is enabled -- which is
     exactly what the real route does, and exactly what the page used to walk
     into by counting its own ticks. */
  if (String(url).split("?")[0] === "/admin/api/proxy-chains/feeds/detect") {
    /* Detection: one read of a URL the operator typed, proposing a format.
       The stub answers the way the route does -- a proposal plus every trial,
       and a plain refusal for a URL that is not https, so the page's own
       validation and the server's cannot drift apart. */
    const sent = JSON.parse(options.body);
    const target = String((sent && sent.url) || "");
    if (!/^https:\/\/.+/.test(target)) {
      const error = new Error("Give an https URL to read.");
      error.status = 422;
      throw error;
    }
    const unreadable = target.includes("mystery");
    return {
      ok: true,
      status: 200,
      json: async () => ({
        detection: unreadable
          ? {
              ok: true,
              parser: "",
              count: 0,
              detail:
                "The URL answered, but none of the formats MCC reads " +
                "recognised it. Pick one anyway if you know what this list is.",
              trials: [],
            }
          : {
              ok: true,
              parser: "geonode",
              count: 137,
              detail:
                'JSON with per-address metadata under a "data" key. A trial ' +
                "read found 137 addresses.",
              trials: [
                { parser: "geonode", label: "JSON: data[] with \"protocols\" and \"anonymityLevel\"", shape: "", count: 137 },
                { parser: "lines", label: "Plain text: one ip:port per line", shape: "", count: 2 },
              ],
            },
      }),
      text: async () => "",
    };
  }
  if (String(url).split("?")[0] === "/admin/api/proxy-chains/feeds") {
    /* The feed list write: add, edit, enable and remove, all one PUT that
       REPLACES the list. The stub mirrors that rather than merging, because
       "remove" is expressed by a row being absent and a merging stub could
       never fail a page that forgot to send one. Ids are minted for new rows,
       exactly as the store does, so the page's next render addresses them. */
    const sent = JSON.parse(options.body);
    const state = ROUTES["/admin/api/proxy-chains"];
    const previous = new Map((state.feeds || []).map((feed) => [feed.id, feed]));
    let minted = 0;
    state.feeds = (sent.feeds || []).map((row) => {
      const before = previous.get(row.id) || {};
      minted += row.id ? 0 : 1;
      return {
        ...before,
        id: row.id || `fd_new${minted}`,
        name: row.name,
        url: row.url,
        parser: row.parser,
        parser_shape: before.parser_shape || "",
        readable: Boolean(row.parser),
        observed: before.observed || "",
        tls_strict: Boolean(before.tls_strict),
        enabled: Boolean(row.enabled),
      };
    });
    return {
      ok: true,
      status: 200,
      json: async () => JSON.parse(JSON.stringify(state)),
      text: async () => "",
    };
  }
  /* The fetch JOB. Since 7.21.0 a fetch reads the lists and then TESTS every
     address they offered, which for hundreds of addresses is minutes -- so the
     press starts a job and the page asks after it. The stub below walks a job
     through the states a real one does, one step per status poll, so the page's
     progress line, its Stop, and its re-attach after a reload are all driven by
     something that actually changes rather than by a fixed payload. */
  if (String(url).split("?")[0] === "/admin/api/proxy-chains/ingest/status") {
    return {
      ok: true,
      status: 200,
      json: async () => proxyFetchStatePayload(true),
      text: async () => "",
    };
  }
  if (String(url).split("?")[0] === "/admin/api/proxy-chains/ingest/stop") {
    if (PROXY_FETCH.state === "running") {
      PROXY_FETCH.state = "stopped";
      PROXY_FETCH.stopping = false;
      // Stopping KEEPS what already passed. The offer list is not cleared.
      PROXY_FETCH.total = PROXY_FETCH.tested;
    }
    return {
      ok: true,
      status: 200,
      json: async () => proxyFetchStatePayload(false),
      text: async () => "",
    };
  }
  if (String(url).split("?")[0] === "/admin/api/proxy-chains/ingest") {
    const state = ROUTES["/admin/api/proxy-chains"];
    // `readable` as well as `enabled`, exactly as `enabled_feed_ids` does: a
    // feed whose reader this install does not ship has nothing to do with the
    // body it would fetch, so it is not one of the feeds a pass reads.
    const on = (state.feeds || []).filter((feed) => feed.enabled && feed.readable);
    if (!on.length) {
      // The real 422. A page that presses this with nothing saved deserves to
      // see the same refusal the server gives.
      const error = new Error(
        "No feeds are switched on, so there is nothing to read. MCC ships " +
          "none of its own -- add a list above and switch it on, and it " +
          "contacts nobody until you do.",
      );
      error.status = 422;
      throw error;
    }
    if (PROXY_FETCH.state === "running") {
      // One sweep at a time. The real route answers 409 naming the one that is
      // already going, and a page that quietly started a second would double
      // the outbound load on strangers' machines for nothing.
      const error = new Error(
        `A fetch is already running (${PROXY_FETCH.job}). One at a time.`,
      );
      error.status = 409;
      throw error;
    }
    PROXY_FETCH.job = `fetch_${Date.now().toString(16)}`;
    PROXY_FETCH.state = "running";
    PROXY_FETCH.stopping = false;
    PROXY_FETCH.feeds_total = on.length;
    PROXY_FETCH.feeds_read = on.length;
    PROXY_FETCH.feeds = on.map((feed) => ({
      id: feed.id,
      name: feed.name,
      ok: true,
      count: 40,
      detail: "",
    }));
    PROXY_FETCH.total = 834;
    PROXY_FETCH.tested = 0;
    PROXY_FETCH.working = 0;
    PROXY_FETCH.dead = 0;
    PROXY_FETCH.refused = 0;
    PROXY_FETCH.offered = 834;
    PROXY_FETCH.corroborated = 3;
    PROXY_FETCH.provider = JSON.parse(options.body || "{}").provider || "";
    return {
      ok: true,
      status: 200,
      json: async () => proxyFetchStatePayload(false),
      text: async () => "",
    };
  }
  /* The candidate bulk route, emulated against the same document the page
     reads back, because that is what makes a bulk add testable at all: the
     addresses that land have to leave the offer list and appear in the chain,
     or the "one repaint" claim is untested.

     Three outcomes on purpose, which is the point of the whole feature: one
     address answers, one breaks certificate validation and is refused, one
     does not answer and is added benched. A partial result is the normal case
     here and the page has to read as if it were. */
  if (String(url).split("?")[0] === "/admin/api/proxy-chains/candidates/bulk") {
    const sent = JSON.parse(options.body);
    const state = ROUTES["/admin/api/proxy-chains"];
    const asked = sent.proxies || [];
    if (!PROXY_UNDO.token || PROXY_UNDO.token !== sent.undo_token) {
      PROXY_UNDO.token = `undo_${Date.now().toString(16)}`;
      PROXY_UNDO.before = JSON.parse(JSON.stringify(state));
    }
    const provider = state.providers.find(
      (entry) => entry.provider_id === sent.provider,
    );
    const results = [];
    asked.forEach((proxyId) => {
      const candidate = (state.candidates || []).find(
        (item) => item.proxy === proxyId,
      );
      if (!candidate) {
        results.push({ proxy: proxyId, label: "", outcome: "gone", detail: "" });
        return;
      }
      if (sent.action === "discard") {
        state.candidates = state.candidates.filter((item) => item.proxy !== proxyId);
        results.push({
          proxy: proxyId, label: candidate.label, outcome: "discarded", detail: "",
        });
        return;
      }
      const chain = provider && provider.chain;
      if (!chain || chain.entries.length >= (state.vocabulary.max_entries || 12)) {
        results.push({
          proxy: proxyId, label: candidate.label, outcome: "full", detail: "",
        });
        return;
      }
      if (proxyId === "px_cand0003") {
        candidate.refused = true;
        candidate.last_check = {
          at: new Date().toISOString(), ok: false, latency_ms: 88,
          tls: "intercepted",
          detail:
            "this proxy breaks certificate validation -- MCC will not route through it",
          exit_ip: "",
        };
        results.push({
          proxy: proxyId,
          label: candidate.label,
          outcome: "refused",
          detail:
            `${candidate.label} breaks certificate validation: its tunnel ` +
            "presented a certificate this machine does not trust.",
        });
        return;
      }
      const answered = proxyId !== "px_cand0004";
      state.candidates = state.candidates.filter((item) => item.proxy !== proxyId);
      chain.entries.push({
        proxy: proxyId, paused: false, direct: false, label: candidate.label,
        scheme: candidate.scheme, source: "feed",
        source_count: candidate.source_count, refused: false,
        last_check: {
          at: new Date().toISOString(), ok: answered,
          latency_ms: answered ? 305 : null,
          tls: answered ? "strict" : "unknown",
          detail: answered ? "" : "no answer", exit_ip: "",
        },
        health: {
          state: "unknown", checked: false, requests: 0, successes: 0,
          failures: 0, cooldown_remaining: 0, refused: false, reason: null,
        },
      });
      results.push({
        proxy: proxyId,
        label: candidate.label,
        outcome: answered ? "added" : "benched",
        detail: answered ? "" : "no answer",
        latency_ms: answered ? 305 : null,
      });
    });
    const counts = {};
    results.forEach((row) => {
      counts[row.outcome] = (counts[row.outcome] || 0) + 1;
    });
    PROXY_UNDO.after = JSON.parse(JSON.stringify(state));
    return {
      ok: true,
      status: 200,
      json: async () =>
        JSON.parse(
          JSON.stringify({
            ...state,
            bulk: {
              action: sent.action,
              provider: sent.provider || "",
              results,
              counts,
              undo_token: PROXY_UNDO.token,
            },
          }),
        ),
      text: async () => "",
    };
  }
  if (String(url).split("?")[0] === "/admin/api/proxy-chains/candidates/undo") {
    const sent = JSON.parse(options.body);
    if (!PROXY_UNDO.before || sent.token !== PROXY_UNDO.token) {
      const error = new Error("There is nothing to undo any more.");
      error.status = 422;
      throw error;
    }
    const before = PROXY_UNDO.before;
    const state = ROUTES["/admin/api/proxy-chains"];
    state.candidates = JSON.parse(JSON.stringify(before.candidates));
    state.providers = JSON.parse(JSON.stringify(before.providers));
    PROXY_UNDO.token = "";
    PROXY_UNDO.before = null;
    return {
      ok: true,
      status: 200,
      json: async () => JSON.parse(JSON.stringify(state)),
      text: async () => "",
    };
  }
  // The checker's route, emulated against the same document the page reads
  // back: a check writes `last_check` on the store, so the card that was
  // tested has to re-render from a payload that carries the new verdict rather
  // than from the one it was holding.
  if (String(url).split("?")[0] === "/admin/api/proxy-chains/check") {
    const sent = JSON.parse(options.body);
    const state = ROUTES["/admin/api/proxy-chains"];
    const provider = state.providers.find(
      (entry) => entry.provider_id === sent.provider,
    );
    const checked = {};
    const entries = ((provider && provider.chain && provider.chain.entries) || [])
      .filter((entry) => entry.proxy && (!sent.proxy || entry.proxy === sent.proxy));
    entries.forEach((entry) => {
      // Two outcomes, both real: the first address relays honestly and the
      // one already marked intercepted still does what it did.
      const intercepted = entry.proxy === "px_cccc3333";
      const record = intercepted
        ? {
            at: new Date().toISOString(),
            ok: false,
            latency_ms: 91,
            tls: "intercepted",
            detail:
              "this proxy breaks certificate validation -- MCC will not route through it",
            exit_ip: "",
          }
        : {
            at: new Date().toISOString(),
            ok: true,
            latency_ms: 377,
            tls: "strict",
            detail: "",
            exit_ip: "",
          };
      entry.last_check = record;
      entry.refused = intercepted;
      entry.health = { ...entry.health, refused: intercepted };
      if (intercepted) entry.health.state = "intercepted";
      checked[entry.proxy] = { ...record, label: entry.label };
    });
    return {
      ok: true,
      status: 200,
      json: async () => ({ ...state, checked }),
      text: async () => "",
    };
  }
  if (String(url).split("?")[0] === "/admin/api/harness-tiers") {
    // A real enough server: the write lands in the same document the next GET
    // (and the card's own re-render) reads back, so "Override then Revert"
    // exercises two states rather than one payload twice.
    const state = ROUTES["/admin/api/harness-tiers"];
    if ((options.method || "GET").toUpperCase() === "POST") {
      const sent = JSON.parse(options.body);
      const agent = state.harnesses[sent.harness] || {};
      const globalChain = (state.tiers.find((tier) => tier.id === sent.tier) || {}).global || {};
      if (sent.override) {
        agent[sent.tier] = {
          override: true,
          model: sent.model || "",
          fallbacks: sent.fallbacks || [],
          paused: sent.paused || [],
          resolved: {
            primary: sent.model || globalChain.primary,
            fallbacks: sent.fallbacks || [],
            paused: sent.paused || [],
            paused_label: `harness_tiers.json:${sent.harness}.${sent.tier}.paused`,
            source: "override",
          },
        };
      } else {
        agent[sent.tier] = {
          override: false,
          model: "",
          fallbacks: [],
          paused: [],
          resolved: { ...globalChain, source: "global" },
        };
      }
      state.harnesses[sent.harness] = agent;
    }
    body = state;
  }
  if (String(url).split("?")[0] === "/admin/api/config/route-pause") {
    // Held open on request, so the in-flight state of the row's own toggle is
    // observable rather than inferred from the order of two awaits.
    if (pauseGate) await pauseGate;
    // A refusal the server states in the response body: applied is false and
    // nothing reached the file, so the row must go back to what it was.
    if (pauseRefusal) {
      return {
        ok: true,
        status: 200,
        json: async () => ({ applied: false, errors: [pauseRefusal] }),
        text: async () => "",
      };
    }
    // A transport-level failure: `api()` throws, so this is the catch path.
    if (pauseHttpFailure) {
      return {
        ok: false,
        status: 500,
        statusText: "Internal Server Error",
        json: async () => ({ detail: pauseHttpFailure }),
        text: async () => pauseHttpFailure,
      };
    }
    // Emulated rather than routed: the response has to reflect the request,
    // because the page patches its own state from it instead of refetching
    // the whole config payload.
    const request = JSON.parse(options.body);
    const key = PAUSE_KEY_BY_MODEL[request.model_key];
    const held = pausedByKey.get(key) || [];
    const next = request.paused
      ? held.includes(request.model_ref)
        ? held
        : [...held, request.model_ref]
      : held.filter((ref) => ref !== request.model_ref);
    pausedByKey.set(key, next);
    body = {
      applied: true,
      errors: [],
      model_key: request.model_key,
      model_ref: request.model_ref,
      paused_key: key,
      paused: request.paused,
      paused_value: next.join(","),
    };
  }
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

// --------------------------------------------------------------- proxying
// The Proxying cards, and the three gestures that change one without a save:
// reorder, the recommended-set reset, and a per-entry pause. The refused chips
// are checked for being genuinely inert rather than merely styled as such.
const proxyingView = doc.querySelector('.admin-view[data-view="proxying"]');
const proxyCards = proxyingView
  ? Array.from(proxyingView.querySelectorAll(".proxy-card"))
  : [];
// Re-queried on every call, never held: each gesture re-renders the whole
// list, so a card captured before one is a detached node whose contents never
// change again -- which would make every assertion after a click vacuous.
const proxyCardFor = (id) =>
  proxyingView
    ? proxyingView.querySelector(`.proxy-card[data-provider="${id}"]`)
    : null;
const proxyEntryLabels = (card) =>
  Array.from(card.querySelectorAll(".proxy-entry-label")).map((node) =>
    node.textContent.trim(),
  );
const proxyChipState = (card) =>
  Array.from(card.querySelectorAll(".proxy-chip")).map((chip) => ({
    id: chip.textContent.trim(),
    on: chip.getAttribute("aria-pressed") === "true",
    disabled: Boolean(chip.disabled),
  }));
const proxyButton = (card, text) =>
  Array.from(card.querySelectorAll("button")).find(
    (button) => button.textContent.trim() === text,
  ) || null;

const proxying = { present: Boolean(proxyingView), cards: proxyCards.length };
const withChain = proxyCardFor("nvidia_nim");
if (withChain) {
  proxying.honesty = (
    doc.querySelector("#proxyingHonesty")?.textContent || ""
  )
    .replace(/\s+/g, " ")
    .trim();
  proxying.order = proxyEntryLabels(withChain);
  proxying.chips = proxyChipState(withChain);
  proxying.policyHelp = (
    withChain.querySelector(".proxy-policy-help")?.textContent || ""
  ).trim();
  proxying.inherited = (
    withChain.querySelector(".proxy-inherited")?.textContent || ""
  )
    .replace(/\s+/g, " ")
    .trim();

  // The checker's own surfaces, read before anything on the card is clicked.
  proxying.checkerNote = (doc.querySelector("#proxyingChecker")?.textContent || "")
    .replace(/\s+/g, " ")
    .trim();
  proxying.checkReadouts = Array.from(
    withChain.querySelectorAll(".proxy-entry-check"),
  ).map((node) => ({
    text: node.textContent.trim(),
    title: node.getAttribute("title") || "",
  }));
  proxying.refusedRows = Array.from(
    withChain.querySelectorAll(".proxy-entry-refused .proxy-entry-label"),
  ).map((node) => node.textContent.trim());
  // Only a saved address can be tested, so the Direct rung has no button.
  proxying.testButtons = Array.from(withChain.querySelectorAll(".proxy-entry")).map(
    (row) => Boolean(proxyButton(row, "Check now")),
  );
  proxying.testAll = (proxyButton(withChain, "Test all (3)") || {}).textContent || "";

  // A refused chip must not toggle when clicked, not merely look refused.
  const refused = Array.from(withChain.querySelectorAll(".proxy-chip")).find(
    (chip) => chip.disabled,
  );
  if (refused) refused.click();
  proxying.chipsAfterRefusedClick = proxyChipState(proxyCardFor("nvidia_nim"));

  const firstRow = proxyCardFor("nvidia_nim").querySelector(".proxy-entry");
  const down = proxyButton(firstRow, "Move down");
  if (down) down.click();
  proxying.orderAfterMoveDown = proxyEntryLabels(proxyCardFor("nvidia_nim"));
  proxying.announcementAfterMove = (
    doc.querySelector("#proxyingStatus")?.textContent || ""
  )
    .replace(/\s+/g, " ")
    .trim();

  // Live per-entry health, measured by the running pools. Read before the
  // pause below, which overwrites the first row's state word with "paused".
  proxying.entryStates = Array.from(
    withChain.querySelectorAll(".proxy-entry-state"),
  ).map((node) => ({
    text: node.textContent.trim(),
    className: node.className,
    title: node.getAttribute("title") || "",
  }));

  const reset = proxyButton(proxyCardFor("nvidia_nim"), "Recommended set");
  if (reset) reset.click();
  proxying.chipsAfterReset = proxyChipState(proxyCardFor("nvidia_nim"));

  const pause = proxyButton(proxyCardFor("nvidia_nim"), "Pause");
  if (pause) pause.click();
  proxying.pausedRows = Array.from(
    proxyCardFor("nvidia_nim").querySelectorAll(".proxy-entry-paused"),
  ).length;

  const save = proxyButton(proxyCardFor("nvidia_nim"), "Save");
  if (save) save.click();
  await new Promise((resolve) => setTimeout(resolve, 120));
  proxying.saved =
    fetchBodies.filter((entry) => entry.path === "/admin/api/proxy-chains").pop() ||
    null;

  // Drive the Test button on one row, and then on the row the checker has
  // already refused. Two outcomes, and the second is the one that matters:
  // the announcement has to say what a refusal means rather than reporting a
  // failed request.
  const rows = Array.from(proxyCardFor("nvidia_nim").querySelectorAll(".proxy-entry"));
  const testable = rows.find((row) => proxyButton(row, "Check now"));
  if (testable) {
    proxyButton(testable, "Check now").click();
    await new Promise((resolve) => setTimeout(resolve, 150));
    proxying.checkPost =
      fetchBodies
        .filter((entry) => entry.path === "/admin/api/proxy-chains/check")
        .pop() || null;
    proxying.readoutsAfterTest = Array.from(
      proxyCardFor("nvidia_nim").querySelectorAll(".proxy-entry-check"),
    ).map((node) => node.textContent.trim());
    proxying.announcementAfterTest = (
      doc.querySelector("#proxyingStatus")?.textContent || ""
    )
      .replace(/\s+/g, " ")
      .trim();
  }

  const refusedRow = Array.from(
    proxyCardFor("nvidia_nim").querySelectorAll(".proxy-entry"),
  ).find((row) => (row.textContent || "").includes("192.0.2.44:3128"));
  if (refusedRow && proxyButton(refusedRow, "Check now")) {
    proxyButton(refusedRow, "Check now").click();
    await new Promise((resolve) => setTimeout(resolve, 150));
    proxying.announcementAfterRefusedTest = (
      doc.querySelector("#proxyingStatus")?.textContent || ""
    )
      .replace(/\s+/g, " ")
      .trim();
    proxying.refusedRowsAfterTest = Array.from(
      proxyCardFor("nvidia_nim").querySelectorAll(
        ".proxy-entry-refused .proxy-entry-label",
      ),
    ).map((node) => node.textContent.trim());
  }
}

/* The feed switches, driven the way an operator drives them.

   This block exists because its absence shipped a defect: ticking a box
   changes only this page's copy of the feed list, while the ingest route reads
   the store. With no test pressing Fetch after a tick, nothing caught that the
   button offered to "Fetch 7 feeds now" from a server that had been told about
   none -- and the server, correctly, answered that no feeds were switched on.
   So: read the labels before, tick one, read them again, press Fetch, and
   record every request it made and in what order. */
{
  const feedPanel = doc.querySelector("#proxyingFeeds");
  const feedBoxes = Array.from(
    feedPanel?.querySelectorAll(".proxy-feed input[type=checkbox]") || [],
  );
  const fetchButton = () =>
    Array.from(feedPanel?.querySelectorAll("button") || []).find((node) =>
      (node.textContent || "").toLowerCase().includes("fetch"),
    );
  const saveButton = () =>
    Array.from(feedPanel?.querySelectorAll("button") || []).find((node) =>
      (node.textContent || "").includes("Save feed list"),
    );
  const buttonNamed = (text) =>
    Array.from(feedPanel?.querySelectorAll("button") || []).find(
      (node) => (node.textContent || "").trim() === text,
    );
  const unsavedNote = () =>
    (feedPanel?.querySelector(".proxy-feed-unsaved")?.textContent || "")
      .replace(/\s+/g, " ")
      .trim();

  proxying.feeds = {
    count: feedBoxes.length,
    anyTicked: feedBoxes.some((box) => box.checked),
    fetchLabel: (fetchButton() || {}).textContent || "",
    fetchDisabled: Boolean((fetchButton() || {}).disabled),
    saveDisabled: Boolean((saveButton() || {}).disabled),
    unsaved: unsavedNote(),
    // A row whose format MCC has no reader for: its switch must be dead and
    // the row must say why, rather than looking like a feed that works and
    // offers nothing.
    unreadableDisabled: feedBoxes
      .map((box, index) => [index, box])
      .filter(([, box]) => box.disabled)
      .map(([index]) => index),
    warnings: Array.from(
      feedPanel?.querySelectorAll(".proxy-feed-warning") || [],
    ).map((node) => node.textContent.trim()),
    // The URLs are rendered in full. A feed URL is a public list the operator
    // typed; unlike a proxy URL it carries no password and hiding it would
    // leave them unable to tell two lists apart.
    urls: Array.from(feedPanel?.querySelectorAll(".proxy-feed-url") || []).map(
      (node) => node.textContent.trim(),
    ),
  };

  const liveBoxes = () =>
    Array.from(
      doc.querySelectorAll("#proxyingFeeds .proxy-feed input[type=checkbox]"),
    );
  // Tick a row that is OFF and readable, so the change is a real change. The
  // block used to tick index 0, which from 7.18.0 is already on -- a "tick"
  // that changes nothing cannot catch a button counting the wrong thing.
  const target = liveBoxes().find((box) => !box.checked && !box.disabled);
  if (target) {
    target.checked = true;
    target.dispatchEvent(new window.Event("change", { bubbles: true }));
    await new Promise((resolve) => setTimeout(resolve, 60));

    proxying.feedsAfterTick = {
      fetchLabel: (fetchButton() || {}).textContent || "",
      fetchDisabled: Boolean((fetchButton() || {}).disabled),
      saveDisabled: Boolean((saveButton() || {}).disabled),
      unsaved: unsavedNote(),
    };

    const bodiesBefore = fetchBodies.length;
    // Order comes from `fetchUrls`, not `fetchBodies`: the ingest POST carries
    // no body, so a body-only recorder cannot see it at all -- and "did the
    // save happen before the read" is precisely a question about order.
    const urlsBefore = fetchUrls.length;
    const press = fetchButton();
    if (press) {
      press.click();
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
    proxying.feedFetchCalls = fetchUrls
      .slice(urlsBefore)
      .map((url) => String(url).split("?")[0])
      .filter((path) => path.startsWith("/admin/api/proxy-chains"));
    // The save's body, which has to carry the tick that was never stored.
    proxying.feedSaveBody =
      fetchBodies
        .slice(bodiesBefore)
        .filter((entry) => entry.path === "/admin/api/proxy-chains/feeds")
        .pop() || null;
    proxying.feedsAfterFetch = {
      fetchLabel: (fetchButton() || {}).textContent || "",
      unsaved: unsavedNote(),
      saveDisabled: Boolean((saveButton() || {}).disabled),
    };
  }

  /* ------------------------------------------- the fetch job: 7.21.0
     A fetch now tests every address the lists offered and keeps only what
     passed, which for hundreds of addresses is minutes of work. So the press
     starts a job, the page shows what it is doing, and it can be stopped.
     Everything below is read off what is RENDERED, and visibility is asserted
     rather than presence: a progress line in a hidden panel is not a progress
     line. */
  {
    const candidatePanel = () => doc.querySelector("#proxyingCandidates");
    const progressBox = () => candidatePanel()?.querySelector(".proxy-fetch-progress");
    const progressLine = () =>
      (progressBox()?.querySelector(".proxy-fetch-line")?.textContent || "")
        .replace(/\s+/g, " ")
        .trim();
    const stopButton = () => progressBox()?.querySelector(".proxy-fetch-stop");
    /* Visible WITHIN its view, which is the question worth asking here: the
       harness drives every view of a single-page dashboard, so all but one are
       `hidden` at any moment and "is the proxying view on screen" is not what
       this is about. Everything between the node and the view has to be
       showing -- a progress line inside a collapsed panel is not a progress
       line, and that is the failure this would otherwise miss. */
    const visible = (node) => {
      let cursor = node;
      while (cursor && !(cursor.classList || { contains: () => false }).contains("admin-view")) {
        if (cursor.hidden) return false;
        cursor = cursor.parentElement;
      }
      return Boolean(node);
    };

    // The press above started the job. Its first render is before any status
    // poll, so this is what the operator sees the instant they press.
    proxying.fetchStarted = {
      visible: visible(progressBox()),
      line: progressLine(),
      stopVisible: visible(stopButton()),
      refetchDisabled: Boolean((fetchButton() || {}).disabled),
      job: PROXY_FETCH.job,
    };

    // A second press while one is running must be refused by the SERVER, not
    // merely hidden by a disabled button. Called directly, past the button.
    let secondStatus = 0;
    try {
      await window.fetch("/admin/api/proxy-chains/ingest", {
        method: "POST",
        body: JSON.stringify({ provider: "nvidia_nim" }),
      });
    } catch (error) {
      secondStatus = error.status || 0;
    }
    proxying.fetchSecondPress = secondStatus;

    // Two polls of the status route: the numbers have to MOVE.
    await new Promise((resolve) => setTimeout(resolve, 1700));
    const after_one = progressLine();
    await new Promise((resolve) => setTimeout(resolve, 1700));
    proxying.fetchProgress = {
      afterOnePoll: after_one,
      afterTwoPolls: progressLine(),
      moved: after_one !== progressLine(),
    };

    // Stop, mid-run. What has already passed has to survive it.
    const testedBeforeStop = PROXY_FETCH.tested;
    const workingBeforeStop = PROXY_FETCH.working;
    const stop = stopButton();
    if (stop) {
      stop.click();
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
    proxying.fetchStopped = {
      state: PROXY_FETCH.state,
      line: progressLine(),
      stopVisible: visible(stopButton()),
      testedKept: PROXY_FETCH.tested === testedBeforeStop,
      workingKept: PROXY_FETCH.working === workingBeforeStop,
      // The offer list is untouched by stopping: the addresses that passed are
      // still there to be used.
      offered: candidatePanel()?.querySelectorAll(".proxy-candidate[data-proxy]")
        .length,
    };

    /* Re-attach. Leaving the view and coming back runs `loadProxying()` again
       -- byte for byte the path a browser reload takes -- so a sweep that is
       still going has to be picked up from the server rather than forgotten.
       The job is put back into `running` first, because that is the state a
       reload mid-sweep actually finds. */
    PROXY_FETCH.state = "running";
    PROXY_FETCH.stopping = false;
    PROXY_FETCH.total = 834;
    /* And wound back to the start of the sweep, which is what makes this
       block deterministic rather than nearly deterministic. The fake server
       flips the job to "done" the moment `tested` reaches `total`, and it
       advances 200 per poll -- so with the counter left where the stop above
       put it, one or two polls inside the 1.8 seconds below could finish the
       run, take the Stop button away, and fail an assertion about re-attaching
       for a reason that has nothing to do with re-attaching. From zero it
       takes five polls, which is far outside the window. */
    PROXY_FETCH.tested = 0;
    const navTo = (id) => {
      const link = doc.querySelector(`.nav-link[data-view="${id}"]`);
      if (link) link.click();
    };
    // Put the view back afterwards: every block after this one reads a page
    // that was left where it found it.
    const wasActive =
      doc.querySelector(".admin-view.active")?.dataset.view || "get_started";
    navTo("models");
    await new Promise((resolve) => setTimeout(resolve, 60));
    navTo("proxying");
    await new Promise((resolve) => setTimeout(resolve, 120));
    const reattachedLine = progressLine();
    await new Promise((resolve) => setTimeout(resolve, 1700));
    proxying.fetchReattached = {
      visible: visible(progressBox()),
      line: reattachedLine,
      stopVisible: visible(stopButton()),
      // And the timer came back with it: the line moves again without another
      // press.
      stillPolling: progressLine() !== reattachedLine,
    };

    // Let it finish so the rest of the harness is not racing a poll.
    PROXY_FETCH.state = "done";
    PROXY_FETCH.tested = PROXY_FETCH.total;
    await new Promise((resolve) => setTimeout(resolve, 1700));
    proxying.fetchFinished = { line: progressLine(), state: PROXY_FETCH.state };
    navTo(wasActive);
    await new Promise((resolve) => setTimeout(resolve, 60));
  }

  /* "Add all working": one press to use what the fetch found. It is the bulk
     add with the working addresses in it -- the same route, the same repaint,
     the same undo -- which is what 6.24.0 is about. */
  {
    const bar = () => doc.querySelector("#proxyingCandidates .proxy-candidate-bar");
    const allButton = () => bar()?.querySelector(".proxy-candidate-all");
    proxying.addAllWorking = {
      visible: Boolean(allButton()) && !allButton().hidden,
      label: (allButton()?.textContent || "").replace(/\s+/g, " ").trim(),
      disabled: Boolean((allButton() || {}).disabled),
    };
  }

  /* What MCC itself measured, on the row. Every other field on a candidate is
     a claim some list published; this one is the reason the row is there. */
  {
    const measured = Array.from(
      doc.querySelectorAll("#proxyingCandidates .proxy-candidate-measured"),
    ).map((node) => ({
      text: (node.textContent || "").trim(),
      className: node.className,
    }));
    proxying.candidateMeasured = measured;
  }

  /* The Add form, which is the whole of what a FRESH install sees: MCC ships
     no lists, so without this block the release's main surface would be
     untested by construction -- the same hole the missing `feeds` array was.
     Detection proposes and the operator decides, so both halves are driven:
     press Detect and read where the picker moved, then move it back. */
  {
    const nameInput = feedPanel?.querySelectorAll(".proxy-feed-input")[0];
    const urlInput = feedPanel?.querySelectorAll(".proxy-feed-input")[1];
    const picker = () => feedPanel?.querySelector(".proxy-feed-select");
    const detectNote = () =>
      (feedPanel?.querySelector(".proxy-feed-detection")?.textContent || "")
        .replace(/\s+/g, " ")
        .trim();

    proxying.feedAddForm = {
      hasName: Boolean(nameInput),
      hasUrl: Boolean(urlInput),
      // The picker is ALWAYS rendered, before any detection has run. That is
      // the binding decision: detection never decides silently.
      pickerOptions: Array.from(picker()?.options || []).map(
        (option) => option.value,
      ),
      pickerValue: (picker() || {}).value || "",
      detectDisabled: Boolean((buttonNamed("Detect format") || {}).disabled),
      addDisabled: Boolean((buttonNamed("Add to list") || {}).disabled),
      note: detectNote(),
    };

    if (nameInput && urlInput) {
      nameInput.value = "A list I found";
      nameInput.dispatchEvent(new window.Event("input", { bubbles: true }));
      urlInput.value = "https://lists.example.com/fresh.json";
      urlInput.dispatchEvent(new window.Event("input", { bubbles: true }));
      await new Promise((resolve) => setTimeout(resolve, 40));

      const detect = buttonNamed("Detect format");
      if (detect) detect.click();
      await new Promise((resolve) => setTimeout(resolve, 160));
      proxying.feedDetect = {
        request:
          fetchBodies
            .filter(
              (entry) =>
                entry.path === "/admin/api/proxy-chains/feeds/detect",
            )
            .pop() || null,
        pickerValue: (picker() || {}).value || "",
        note: detectNote(),
        // Still exactly one picker, still offering every reader: a detection
        // that "succeeded" must not collapse the choice.
        pickerOptions: Array.from(picker()?.options || []).map(
          (option) => option.value,
        ),
      };

      // The override. The operator moves the picker off the proposal, and
      // THAT is what must end up in the saved row.
      const chooser = picker();
      if (chooser) {
        chooser.value = "monosans";
        chooser.dispatchEvent(new window.Event("change", { bubbles: true }));
        await new Promise((resolve) => setTimeout(resolve, 40));
      }
      proxying.feedOverride = {
        pickerValue: (picker() || {}).value || "",
        note: detectNote(),
      };

      const add = buttonNamed("Add to list");
      if (add) add.click();
      await new Promise((resolve) => setTimeout(resolve, 60));
      proxying.feedAfterAdd = {
        rows: Array.from(
          feedPanel?.querySelectorAll(".proxy-feed-name") || [],
        ).map((node) => node.textContent.trim()),
        // Added switched OFF: adding a list and reading it are separate acts.
        ticked: Array.from(
          feedPanel?.querySelectorAll(".proxy-feed input[type=checkbox]") || [],
        ).map((box) => box.checked),
        saveDisabled: Boolean((saveButton() || {}).disabled),
        // The form is cleared, so a second add cannot silently repeat the
        // first.
        urlValue:
          (feedPanel?.querySelectorAll(".proxy-feed-input")[1] || {}).value || "",
      };

      const saveNow = saveButton();
      if (saveNow) saveNow.click();
      await new Promise((resolve) => setTimeout(resolve, 140));
      proxying.feedSaveAfterAdd =
        fetchBodies
          .filter((entry) => entry.path === "/admin/api/proxy-chains/feeds")
          .pop() || null;
    }
  }

  /* Removing a row. The addresses that row already offered must survive it:
     they are independent facts with their own test results, and often the
     reason the list was added at all. */
  {
    const before = Array.from(
      feedPanel?.querySelectorAll(".proxy-feed-name") || [],
    ).map((node) => node.textContent.trim());
    const remove = feedPanel?.querySelector(".proxy-feed-remove");
    if (remove) remove.click();
    await new Promise((resolve) => setTimeout(resolve, 60));
    proxying.feedRemoval = {
      before,
      after: Array.from(
        doc.querySelectorAll("#proxyingFeeds .proxy-feed-name"),
      ).map((node) => node.textContent.trim()),
      candidatesStillShown: doc.querySelectorAll(
        "#proxyingCandidates .proxy-candidate",
      ).length,
      announcement: (doc.querySelector("#proxyingStatus")?.textContent || "")
        .replace(/\s+/g, " ")
        .trim(),
    };
  }

}

/* The candidate list, driven the way an operator with 1,572 addresses drives
   it: filter, select all of what is left, choose ONE destination, press once.

   Everything here is a gesture a person makes, not a function call. The point
   of the block is that a selection feature with no fixture rows is untested by
   construction -- which is exactly the hole the missing `feeds` array left, and
   why `candidates: []` was the first thing fixed. */
{
  // On the page, the way an operator is: Escape means "drop this selection"
  // only while the Proxying view is the one being looked at.
  const proxyLink = navLinks.find((link) => link.dataset.view === "proxying");
  if (proxyLink) {
    proxyLink.click();
    await new Promise((resolve) => setTimeout(resolve, 120));
  }
  const candidatePanel = () => doc.querySelector("#proxyingCandidates");
  const candidateRows = () =>
    Array.from(candidatePanel().querySelectorAll(".proxy-candidate[data-proxy]"));
  const candidateLabels = () =>
    candidateRows().map((row) =>
      row.querySelector(".proxy-candidate-label").textContent.trim(),
    );
  const candidateBox = (row) => row.querySelector("input.proxy-candidate-select");
  const barButton = (text) =>
    Array.from(candidatePanel().querySelectorAll(".proxy-candidate-bar button")).find(
      (node) => (node.textContent || "").startsWith(text),
    ) || null;
  const control = (labelText) =>
    Array.from(candidatePanel().querySelectorAll(".proxy-candidate-control")).find(
      (node) => (node.textContent || "").includes(labelText),
    );
  const statusText = () =>
    (doc.querySelector("#proxyingStatus")?.textContent || "")
      .replace(/\s+/g, " ")
      .trim();
  const proxySelectedProxies = () =>
    candidateRows()
      .filter((row) => candidateBox(row).checked)
      .map((row) => row.dataset.proxy);

  proxying.candidates = {
    rows: candidateRows().length,
    // Sorted by feeds agreeing out of the box: the one field on a row that is
    // evidence rather than a claim copied from one publisher.
    order: candidateLabels(),
    selectAll: Boolean(candidatePanel().querySelector(".proxy-candidate-select-all")),
    // One destination for the whole selection, and no picker on any row.
    perRowPickers: candidatePanel().querySelectorAll(
      ".proxy-candidate[data-proxy] select",
    ).length,
    destinations: Array.from(
      candidatePanel().querySelectorAll(".proxy-candidate-bar select option"),
    ).map((option) => option.textContent),
    note: (candidatePanel().querySelector(".proxy-candidate-note")?.textContent || "")
      .replace(/\s+/g, " ")
      .trim(),
    countLine: (
      candidatePanel().querySelector(".proxy-candidate-count")?.textContent || ""
    ).trim(),
    capacity: (
      candidatePanel().querySelector(".proxy-candidate-capacity")?.textContent || ""
    )
      .replace(/\s+/g, " ")
      .trim(),
  };

  // Sort by latency, then filter by scheme: the list under the controls
  // repaints and the controls themselves are left alone.
  const sortSelect = control("Sort by").querySelector("select");
  sortSelect.value = "latency";
  sortSelect.dispatchEvent(new window.Event("change", { bubbles: true }));
  proxying.candidates.orderByLatency = candidateLabels();

  const schemeSelect = control("Scheme").querySelector("select");
  schemeSelect.value = "socks5h";
  schemeSelect.dispatchEvent(new window.Event("change", { bubbles: true }));
  proxying.candidates.socksOnly = candidateLabels();

  // Select all, while a filter is on: it means every address the filter
  // matches, which is what the operator is looking at.
  const selectAllBox = () =>
    candidatePanel().querySelector("input.proxy-candidate-select-all");
  selectAllBox().checked = true;
  selectAllBox().dispatchEvent(new window.Event("change", { bubbles: true }));
  proxying.candidates.selectedWhileFiltered = (
    candidatePanel().querySelector(".proxy-candidate-count")?.textContent || ""
  ).trim();

  schemeSelect.value = "";
  schemeSelect.dispatchEvent(new window.Event("change", { bubbles: true }));
  // Dropping the filter does not drop the selection made under it.
  proxying.candidates.selectedAfterFilterCleared = (
    candidatePanel().querySelector(".proxy-candidate-count")?.textContent || ""
  ).trim();

  // Escape drops it, with no modal open.
  doc.dispatchEvent(
    new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
  );
  proxying.candidates.afterEscape = (
    candidatePanel().querySelector(".proxy-candidate-count")?.textContent || ""
  ).trim();

  /* Shift+click a range, pointer-style. The box is never ticked by hand first:
     dispatching `click` on a checkbox runs its activation behaviour, which
     toggles `checked` BEFORE the listeners see it, so a hand-set tick arrives
     at the handler inverted. That cost a confused half hour. */
  const rowsNow = candidateRows();
  candidateBox(rowsNow[0]).dispatchEvent(
    new window.MouseEvent("click", { bubbles: true }),
  );
  candidateBox(rowsNow[2]).dispatchEvent(
    new window.MouseEvent("click", { bubbles: true, shiftKey: true }),
  );
  proxying.candidates.afterShiftClick = candidateRows()
    .filter((row) => candidateBox(row).checked)
    .map((row) => row.dataset.proxy);

  // And the same range with the keyboard, which WCAG 2.2 requires rather than
  // suggests: Escape first, then Shift+ArrowDown from the first row.
  doc.dispatchEvent(
    new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
  );
  const first = candidateBox(candidateRows()[0]);
  first.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  first.dispatchEvent(
    new window.KeyboardEvent("keydown", {
      key: "ArrowDown",
      shiftKey: true,
      bubbles: true,
    }),
  );
  proxying.candidates.afterShiftArrow = candidateRows()
    .filter((row) => candidateBox(row).checked)
    .map((row) => row.dataset.proxy);

  // Now the write. Select the three that produce the three outcomes -- one
  // answers, one breaks certificate validation, one does not answer -- and
  // press once.
  doc.dispatchEvent(
    new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
  );
  ["px_cand0001", "px_cand0003", "px_cand0004"].forEach((proxyId) => {
    const row = candidateRows().find((node) => node.dataset.proxy === proxyId);
    candidateBox(row).dispatchEvent(
      new window.MouseEvent("click", { bubbles: true }),
    );
  });
  proxying.candidates.addLabel = (barButton("Test and add") || {}).textContent || "";
  const urlsBeforeAdd = fetchUrls.length;
  barButton("Test and add").click();
  await new Promise((resolve) => setTimeout(resolve, 250));

  proxying.candidates.addCalls = fetchUrls
    .slice(urlsBeforeAdd)
    .map((url) => String(url).split("?")[0])
    .filter((path) => path.startsWith("/admin/api/proxy-chains"));
  proxying.candidates.addBody =
    fetchBodies
      .filter((entry) => entry.path === "/admin/api/proxy-chains/candidates/bulk")
      .pop() || null;
  proxying.candidates.summary = statusText();
  proxying.candidates.outcomes = candidateRows().map((row) => ({
    proxy: row.dataset.proxy,
    outcome: (row.querySelector(".proxy-candidate-outcome")?.textContent || "").trim(),
    // The standing verdict beside it. A row the checker has just refused
    // carries the badge rather than two near-identical phrases.
    badge: (
      row.querySelector(".proxy-entry-state-intercepted")?.textContent || ""
    ).trim(),
    refused: row.classList.contains("proxy-candidate-refused"),
  }));
  proxying.candidates.chainAfterAdd = proxyEntryLabels(proxyCardFor("nvidia_nim"));
  proxying.candidates.rowsAfterAdd = candidateRows().length;

  // The panel's Undo, which is the one on the page: a status region that stays
  // put, not a toast that vanished before the summary could be read.
  const undo = Array.from(
    doc.querySelectorAll("#proxyingStatus button"),
  ).find((node) => (node.textContent || "").startsWith("Undo"));
  proxying.candidates.undoOffered = Boolean(undo);
  if (undo) {
    undo.click();
    await new Promise((resolve) => setTimeout(resolve, 200));
    proxying.candidates.rowsAfterUndo = candidateRows().length;
    proxying.candidates.chainAfterUndo = proxyEntryLabels(proxyCardFor("nvidia_nim"));
    proxying.candidates.undoSentence = statusText();
  }

  /* What the selection holds after a bulk add is exactly what did NOT land:
     the addresses that went into the chain left the offer list, and left the
     selection with it. Read it here, then drop it, so the discard below is
     unambiguously about the one address it ticks. */
  proxying.candidates.selectionAfterAdd = proxySelectedProxies();
  proxying.candidates.stored = window.localStorage.getItem(
    "mcc.proxying.candidates.v1",
  );
  doc.dispatchEvent(
    new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
  );

  // Discard: the other half of "same for remove", and it touches no chain.
  const doomed = candidateRows().find(
    (row) => row.dataset.proxy === "px_cand0002",
  );
  candidateBox(doomed).dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  barButton("Discard").click();
  await new Promise((resolve) => setTimeout(resolve, 200));
  proxying.candidates.afterDiscard = {
    rows: candidateRows().length,
    labels: candidateLabels(),
    sentence: statusText(),
    chain: proxyEntryLabels(proxyCardFor("nvidia_nim")).length,
  };
}

/* 7.35.1: the refused addresses, and the sentence an emptied offer list gets.

   Both are about the same failure -- a panel reporting a number with nothing
   behind it, or reporting the wrong reason for an empty list -- so they are
   driven together, as gestures, on the page the operator is looking at. */
{
  const panel = () => doc.querySelector("#proxyingCandidates");
  const toggle = () => panel().querySelector(".proxy-refused-toggle");
  const rows = () => Array.from(panel().querySelectorAll(".proxy-refused-row"));
  const read = () => ({
    label: (panel().querySelector(".proxy-refused-label")?.textContent || "").trim(),
    labels: rows().map((row) =>
      (row.querySelector(".proxy-refused-label")?.textContent || "").trim(),
    ),
    reasons: rows().map((row) =>
      (row.querySelector(".proxy-refused-reason")?.textContent || "").trim(),
    ),
    whens: rows().map((row) =>
      (row.querySelector(".proxy-refused-when")?.textContent || "").trim(),
    ),
    // The read-only claim, measured rather than asserted in prose: anything
    // that could be pressed or ticked inside these rows would be a way into a
    // chain for an address the server refuses.
    controls: panel().querySelectorAll(
      ".proxy-refused button, .proxy-refused input, .proxy-refused select, .proxy-refused a",
    ).length,
  });

  proxying.refusedList = {
    collapsed: {
      toggle: (toggle()?.textContent || "").trim(),
      expanded: toggle()?.getAttribute("aria-expanded"),
      rows: rows().length,
    },
  };
  toggle().click();
  await new Promise((resolve) => setTimeout(resolve, 60));
  proxying.refusedList.expanded = {
    toggle: (panel().querySelector(".proxy-refused-toggle")?.textContent || "").trim(),
    ariaExpanded: panel()
      .querySelector(".proxy-refused-toggle")
      .getAttribute("aria-expanded"),
    note: (panel().querySelector(".proxy-refused-note")?.textContent || "")
      .replace(/\s+/g, " ")
      .trim(),
    ...read(),
  };
  // And it folds away again, remembering the choice with the other filters.
  panel().querySelector(".proxy-refused-toggle").click();
  await new Promise((resolve) => setTimeout(resolve, 60));
  proxying.refusedList.reCollapsed = { rows: rows().length };
  panel().querySelector(".proxy-refused-toggle").click();
  await new Promise((resolve) => setTimeout(resolve, 60));

  /* The empty-offer sentence. Three states that used to share one sentence:
     nothing passed; everything that passed is in a chain; everything that
     passed was discarded. */
  const emptySentence = () =>
    (panel().querySelector("p.field-description")?.textContent || "")
      .replace(/\s+/g, " ")
      .trim();
  /* The page is reloaded through its own load path rather than by reaching
     into its state: the server is what decides all three of these, and a test
     that set the page's variables directly would not prove the payload carries
     what the sentence needs. */
  const state = ROUTES["/admin/api/proxy-chains"];
  const savedCandidates = state.candidates;
  const savedWorking = PROXY_FETCH.working;
  const savedState = PROXY_FETCH.state;
  const reload = async () => {
    navLinks.find((link) => link.dataset.view === "providers").click();
    await new Promise((resolve) => setTimeout(resolve, 80));
    navLinks.find((link) => link.dataset.view === "proxying").click();
    await new Promise((resolve) => setTimeout(resolve, 160));
  };

  state.candidates = [];
  state.chained_passing = 1;
  PROXY_FETCH.state = "done";
  PROXY_FETCH.working = 5;
  await reload();
  proxying.emptyOffer = {
    allInAChain: emptySentence(),
    // The progress line above it must not still be claiming things about rows
    // that are not there.
    progressLine: (panel().querySelector(".proxy-fetch-line")?.textContent || "")
      .replace(/\s+/g, " ")
      .trim(),
  };

  state.chained_passing = 0;
  await reload();
  proxying.emptyOffer.allDiscarded = emptySentence();

  PROXY_FETCH.working = 0;
  await reload();
  proxying.emptyOffer.nonePassed = emptySentence();
  // The refused rows are reachable from the empty panel too, which is exactly
  // the state an operator reaches them in. The toggle's own memory survived
  // three reloads, so it is still open.
  proxying.emptyOffer.refusedToggle = (toggle()?.textContent || "").trim();

  state.candidates = savedCandidates;
  state.chained_passing = 1;
  PROXY_FETCH.working = savedWorking;
  PROXY_FETCH.state = savedState;
  await reload();
}

/* The card's own bulk remove: tick two entries, press once, and the draft is
   shorter without anything having been written. */
{
  const card = proxyCardFor("nvidia_nim");
  const boxes = Array.from(card.querySelectorAll("input.proxy-entry-select"));
  proxying.entrySelectBoxes = boxes.length;
  if (boxes.length >= 2) {
    const before = proxyEntryLabels(proxyCardFor("nvidia_nim")).length;
    boxes[0].checked = true;
    boxes[0].dispatchEvent(new window.Event("change", { bubbles: true }));
    const later = Array.from(
      proxyCardFor("nvidia_nim").querySelectorAll("input.proxy-entry-select"),
    );
    later[1].checked = true;
    later[1].dispatchEvent(new window.Event("change", { bubbles: true }));
    const removeMany = Array.from(
      proxyCardFor("nvidia_nim").querySelectorAll("button"),
    ).find((node) => (node.textContent || "").startsWith("Remove 2 selected"));
    proxying.entryBulkRemoveOffered = Boolean(removeMany);
    if (removeMany) removeMany.click();
    proxying.entriesAfterBulkRemove = {
      before,
      after: proxyEntryLabels(proxyCardFor("nvidia_nim")).length,
      sentence: (doc.querySelector("#proxyingStatus")?.textContent || "")
        .replace(/\s+/g, " ")
        .trim(),
    };
  }
}

// The subscription-login card: its rail is inert until the acknowledgement.
const oauthCard = proxyCardFor("chatgpt_oauth");
proxying.oauth = oauthCard
  ? {
      note: (oauthCard.querySelector(".proxy-oauth-note p")?.textContent || "")
        .replace(/\s+/g, " ")
        .trim(),
      acknowledged: Boolean(
        oauthCard.querySelector(".proxy-oauth-ack")?.checked,
      ),
    }
  : null;

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

/* The OpenCode Zen card's credential toggle. Read off the Providers view
   rather than Limits, because the whole point of 7.34.0's switch is that an
   operator finds it on the card whose key it decides the spending of. */
const providersView = doc.querySelector('.admin-view[data-view="providers"]');
const opencodeCard = providersView
  ? Array.from(providersView.querySelectorAll(".provider-card")).find((card) =>
      card.querySelector('[data-key="OPENCODE_FREE_TIER_CREDENTIAL"]'),
    )
  : null;
const opencodeCredentialControl = opencodeCard
  ? opencodeCard.querySelector('select[data-key="OPENCODE_FREE_TIER_CREDENTIAL"]')
  : null;
const opencodeCredentialRow = opencodeCard
  ? opencodeCard.querySelector('.field[data-key="OPENCODE_FREE_TIER_CREDENTIAL"]')
  : null;
const opencodeCredential = opencodeCredentialControl
  ? {
      tag: opencodeCredentialControl.tagName.toLowerCase(),
      value: opencodeCredentialControl.value,
      original: opencodeCredentialControl.dataset.original,
      optionValues: Array.from(opencodeCredentialControl.options).map(
        (item) => item.value,
      ),
      optionLabels: Array.from(opencodeCredentialControl.options).map((item) =>
        item.textContent.trim(),
      ),
      help: opencodeCredentialRow
        ? (opencodeCredentialRow.textContent || "").replace(/\s+/g, " ").trim()
        : "",
      // The key it is an alternative to spending is on the same card.
      cardHasApiKey: Boolean(
        opencodeCard.querySelector('[data-key="OPENCODE_API_KEY"]'),
      ),
    }
  : null;

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
// The sentence the credential-health card paints under its fields. It is the
// only place the page states, in words, what a 429 costs a key -- so it has
// to follow the mode rather than keep describing the 7.21.0 rule.
const cooldownRuleNow = () => {
  const card = doc.getElementById("section-credential_health");
  const paragraphs = card
    ? Array.from(card.querySelectorAll("p.field-description"))
    : [];
  const last = paragraphs[paragraphs.length - 1];
  return last ? (last.textContent || "").replace(/\s+/g, " ").trim() : null;
};
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
  cooldownRule: cooldownRuleNow(),
  cooldownModeOptions: Array.from(
    (controlIn("RATE_LIMIT_COOLDOWN_MODE") || { options: [] }).options,
  ).map((option) => option.value),
  cooldownMaxRange: textOf(rowIn("RATE_LIMIT_COOLDOWN_MAX_SECONDS"), ".field-range"),
  /* 7.22.2's two fetch selects, on the page they belong to. Every env var is
     configurable on the dashboard, and a select that never rendered would be a
     setting only a .env file can reach. */
  fetchConcurrencyModeOptions: Array.from(
    (controlIn("PROXY_FETCH_CONCURRENCY_MODE") || { options: [] }).options,
  ).map((option) => option.value),
  fetchCheckDepthOptions: Array.from(
    (controlIn("PROXY_FETCH_CHECK_DEPTH") || { options: [] }).options,
  ).map((option) => option.value),
  fetchConcurrencyRange: textOf(
    rowIn("PROXY_FETCH_TEST_CONCURRENCY"),
    ".field-range",
  ),
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

// (b1) the 429 cooldown mode. The card's rule sentence is the only place the
// page says what a 429 costs a key, so it has to follow the select rather
// than keep describing the mode the operator just turned off.
const cooldownModeSelect = controlIn("RATE_LIMIT_COOLDOWN_MODE");
if (cooldownModeSelect) {
  cooldownModeSelect.value = "fixed";
  cooldownModeSelect.dispatchEvent(new window.Event("change", { bubbles: true }));
  limits.afterCooldownFixed = { cooldownRule: cooldownRuleNow() };
  cooldownModeSelect.value = "off";
  cooldownModeSelect.dispatchEvent(new window.Event("change", { bubbles: true }));
  limits.afterCooldownOff = { cooldownRule: cooldownRuleNow() };
  cooldownModeSelect.value = "provider";
  cooldownModeSelect.dispatchEvent(new window.Event("change", { bubbles: true }));
}

// (b2) the master switch renders twice on purpose -- once on Limits, where it
// gates the card, and once on Model Config, beside the routes it applies to.
// They are one manifest field and one saved key, so editing either has to be
// the same edit: the twin follows, the Limits card re-gates itself, and the
// dirty counter stays at one because it counts keys, not controls.
const benchOnModelConfig = doc.getElementById(
  "field-FALLBACK_BENCH_ENABLED-model-config",
);
if (benchOnModelConfig && benchSelect) {
  benchOnModelConfig.value = "true";
  benchOnModelConfig.dispatchEvent(new window.Event("change", { bubbles: true }));
  // Nothing is dispatched on the Limits control here on purpose: the mirror
  // has to notify it, or the two cards disagree about whether the feature is
  // on. A browser drive found exactly that -- the value copied across and the
  // Limits card stayed live.
  limits.benchMirror = {
    controls: doc.querySelectorAll(
      'select[data-key="FALLBACK_BENCH_ENABLED"]',
    ).length,
    onModelConfig: benchOnModelConfig.value,
    onLimits: benchSelect.value,
    submitted: Object.keys(window.eval("changedValues()")),
    benchGroups: benchGroupsNow(),
    crosslink: textOf(
      doc.querySelector(".route-bench"),
      ".bench-crosslink",
    ),
    label: textOf(doc.querySelector(".route-bench"), ".route-tier"),
    markup: doc.querySelector(".route-bench")
      ? doc.querySelector(".route-bench").innerHTML.includes("<script")
      : null,
  };
  // Back to where (b) left it so nothing below sees a different world.
  benchOnModelConfig.value = "false";
  benchOnModelConfig.dispatchEvent(new window.Event("change", { bubbles: true }));
}

// (c) raise the silent-attempt floor above the equal share. 600 over ten
// models is 60s, so a 180s floor binds: the share becomes 180, the first-token
// deadline caps the allowance back to 120, and ten models at 180 want 1,800s
// of a 600s budget -- which is the trade the warning has to state out loud.
const floorInput = controlIn("FALLBACK_ATTEMPT_SHARE_FLOOR");
if (floorInput) {
  floorInput.value = "180";
  floorInput.dispatchEvent(new window.Event("input", { bubbles: true }));
  limits.afterFloorRaised = {
    calcHeadline: textOf(limitsView, "#calcHeadline"),
    calcFormula: textOf(limitsView, "#calcFormula"),
    calcWarning: textOf(limitsView, "#calcWarning"),
    calcWarningHidden: calcWarningEl ? calcWarningEl.hidden : null,
    calcRows: calcRowsNow(),
  };
  floorInput.value = floorInput.dataset.original;
  floorInput.dispatchEvent(new window.Event("input", { bubbles: true }));
}

// (d) raise the total budget: the calculator recomputes without a reload.
const totalInput = controlIn("FALLBACK_TOTAL_TIMEOUT");
if (totalInput) {
  totalInput.value = "1200";
  totalInput.dispatchEvent(new window.Event("input", { bubbles: true }));
  limits.calcHeadlineAfterEdit = textOf(limitsView, "#calcHeadline");
  limits.calcRowsAfterEdit = calcRowsNow();
}

// (e) clear a deadline entirely. A field nobody has set renders blank with its
// default in the placeholder, and the server is applying that default -- so a
// calculator that read the blank as "no limit" would describe a machine that
// does not exist. This is the state every install starts the share floor in.
const firstInput = controlIn("FALLBACK_FIRST_TOKEN_TIMEOUT");
if (firstInput) {
  firstInput.value = "";
  firstInput.dispatchEvent(new window.Event("input", { bubbles: true }));
  limits.afterFirstTokenCleared = {
    calcHeadline: textOf(limitsView, "#calcHeadline"),
    calcFormula: textOf(limitsView, "#calcFormula"),
  };
  firstInput.value = firstInput.dataset.original;
  firstInput.dispatchEvent(new window.Event("input", { bubbles: true }));
}

// (f) the shipped state since 6.16.0: every deadline 0. The card must say so
// rather than printing "0 s" or NaN, and none of the warnings that describe a
// budget being carved up may fire, because there is no budget.
if (firstInput && totalInput && floorInput) {
  [firstInput, totalInput, floorInput].forEach((control) => {
    control.value = "0";
    control.dispatchEvent(new window.Event("input", { bubbles: true }));
  });
  limits.afterAllDeadlinesZeroed = {
    calcHeadline: textOf(limitsView, "#calcHeadline"),
    calcFormula: textOf(limitsView, "#calcFormula"),
    calcWarning: textOf(limitsView, "#calcWarning"),
    calcWarningHidden: calcWarningEl ? calcWarningEl.hidden : null,
    calcRows: calcRowsNow(),
  };
  [firstInput, totalInput, floorInput].forEach((control) => {
    control.value = control.dataset.original;
    control.dispatchEvent(new window.Event("input", { bubbles: true }));
  });
}

// (g) six models against a 1800s budget with a 600s floor: 6 x 600 = 3600s of
// demand the budget cannot meet, which is the trade the warning must state.
// Three models fit exactly, and it must stay quiet for those.
if (firstInput && totalInput && floorInput) {
  const driveChain = (models) => {
    // Model Config lives in another view, so this one is not scoped to
    // limitsView -- the calculator reads it off the document the same way.
    const chain = doc.querySelector(CONTROL_SELECTOR("MODEL_OPUS_FALLBACKS"));
    if (!chain) return null;
    chain.value = Array.from({ length: models - 1 }, (_v, i) => `p/m${i}`).join(",");
    chain.dispatchEvent(new window.Event("input", { bubbles: true }));
    firstInput.value = "600";
    totalInput.value = "1800";
    floorInput.value = "600";
    [firstInput, totalInput, floorInput].forEach((control) =>
      control.dispatchEvent(new window.Event("input", { bubbles: true })),
    );
    const seen = {
      calcWarning: textOf(limitsView, "#calcWarning"),
      calcWarningHidden: calcWarningEl ? calcWarningEl.hidden : null,
    };
    chain.value = chain.dataset.original;
    chain.dispatchEvent(new window.Event("input", { bubbles: true }));
    return seen;
  };
  limits.floorAgainstBudget = { six: driveChain(6), three: driveChain(3) };
  [firstInput, totalInput, floorInput].forEach((control) => {
    control.value = control.dataset.original;
    control.dispatchEvent(new window.Event("input", { bubbles: true }));
  });
}

/* (h) The Stream keepalive card's "which clock fires first" warning. The
   loaded payload has every watched deadline at or under the client's 300 s
   floor (120 / 120 / 300), so it starts hidden; a stall deadline of 0 -- no
   limit -- outlasts the client and shows it; 120 hides it again. Keepalive
   off changes the line under it. Nothing here may touch any setting. */
{
  const watchdogNow = () => {
    const card = doc.getElementById("watchdogCard");
    return {
      present: Boolean(card),
      hidden: card ? card.hidden : null,
      inKeepaliveSection: Boolean(
        card && card.closest("#section-stream_keepalive"),
      ),
      lead: textOf(limitsView, "#watchdogLead"),
      items: card
        ? Array.from(card.querySelectorAll("#watchdogList li")).map((li) =>
            li.textContent.trim(),
          )
        : [],
      keepalive: textOf(limitsView, "#watchdogKeepalive"),
      html: card ? card.innerHTML : "",
    };
  };
  const stallInput = controlIn("FALLBACK_STALL_TIMEOUT");
  const idleInput = controlIn("STREAM_KEEPALIVE_IDLE_SECONDS");
  const drive = (control, value) => {
    control.value = value;
    control.dispatchEvent(new window.Event("input", { bubbles: true }));
  };
  limits.watchdog = { loaded: watchdogNow() };
  if (stallInput && idleInput) {
    drive(stallInput, "0");
    limits.watchdog.stallZero = watchdogNow();
    const modeSelect = controlIn("STREAM_KEEPALIVE_MODE");
    if (modeSelect) {
      drive(modeSelect, "frames");
      limits.watchdog.framesMode = watchdogNow();
      drive(modeSelect, modeSelect.dataset.original);
    }
    drive(idleInput, "0");
    limits.watchdog.keepaliveOff = watchdogNow();
    drive(idleInput, idleInput.dataset.original);
    drive(stallInput, "120");
    limits.watchdog.stallBackTo120 = watchdogNow();
    drive(stallInput, stallInput.dataset.original);
    // The card only reads: with both controls back where they were loaded,
    // nothing it could have written is pending.
    limits.watchdog.pendingAfter = Object.keys(window.eval("changedValues()")).filter(
      (key) =>
        key.startsWith("STREAM_KEEPALIVE_") ||
        [
          "FALLBACK_FIRST_TOKEN_TIMEOUT",
          "FALLBACK_STALL_TIMEOUT",
          "FALLBACK_REASONING_ANSWER_TIMEOUT",
        ].includes(key),
    );
  }
}

/* The loop-lag readout on Limits & Resilience. Rendered by the
   loop_health section renderer and filled by loadLoopLag() when the view
   is opened, so it is read after a nav click and a settle rather than
   from the initial paint. */
{
  const link = doc.querySelector('.nav-link[data-view="limits"]');
  if (link) link.click();
  await new Promise((resolve) => setTimeout(resolve, 80));
  const readout = doc.querySelector("#loopLagReadout");
  limits.loopLag = {
    present: Boolean(readout),
    rows: readout
      ? Array.from(readout.querySelectorAll("tbody tr")).map((row) => ({
          reason: row.children[0].textContent.trim(),
          lag: row.children[1].textContent.trim(),
          took: row.children[2].textContent.trim(),
          overBudget: row.classList.contains("calc-over-budget"),
        }))
      : [],
  };
}

// Every control the drive touched goes back to what it loaded with: the
// optimizer's own dirty assertion below counts from zero.
[modeSelect, benchSelect, floorInput, totalInput, firstInput].forEach((control) => {
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
    shapePanes: wire.querySelectorAll(".req-shape-pane").length,
    shapeTerms: Array.from(wire.querySelectorAll(".req-shape-grid dt")).map(
      (dt) => dt.textContent,
    ),
    shapeValues: Array.from(wire.querySelectorAll(".req-shape-grid dd")).map(
      (dd) => dd.textContent,
    ),
    shapeEmpty: textOf(wire, ".req-shape-empty"),
    chainKeys: Array.from(chain.querySelectorAll(".req-chain-key")).map(
      (el) => el.textContent,
    ),
    chainHidden: chain.hidden,
    ladderSummaries: Array.from(chain.querySelectorAll(".req-chain-summary")).map(
      (el) => el.textContent,
    ),
    ladderRootCauses: Array.from(chain.querySelectorAll(".req-chain-rootcause")).map(
      (el) => el.textContent,
    ),
    chainReasons: Array.from(chain.querySelectorAll(".req-chain-reason")).map(
      (el) => (el.textContent || "").replace(/\s+/g, " ").trim(),
    ),
    benchReasons: Array.from(chain.querySelectorAll(".req-chain-bench")).map(
      (el) => el.textContent,
    ),
    truncations: Array.from(chain.querySelectorAll(".req-chain-truncated")).map(
      (el) => el.textContent,
    ),
    continuations: Array.from(chain.querySelectorAll(".req-chain-continued")).map(
      (el) => el.textContent,
    ),
    ladderTries: Array.from(chain.querySelectorAll(".req-chain-try")).map((el) =>
      (el.textContent || "").replace(/\s+/g, " ").trim(),
    ),
    ladderDecisions: Array.from(chain.querySelectorAll(".req-chain-decisions li")).map(
      (el) => el.textContent,
    ),
    ladderBodies: Array.from(chain.querySelectorAll(".req-chain-try-body pre")).map(
      (el) => el.textContent,
    ),
    ladderHeads: Array.from(chain.querySelectorAll(".req-chain-try-head pre")).map(
      (el) => el.textContent,
    ),
    ladderHeadLabels: Array.from(
      chain.querySelectorAll(".req-chain-try-head summary"),
    ).map((el) => el.textContent),
    dialTitles: Array.from(chain.querySelectorAll(".req-chain-dials-title")).map(
      (el) => el.textContent,
    ),
    dials: Array.from(chain.querySelectorAll(".req-chain-dial")).map(
      (el) => el.textContent,
    ),
    dialClasses: Array.from(chain.querySelectorAll(".req-chain-dial")).map(
      (el) => el.className,
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
  // The reply's shape, opposite the body that asked for it. The first row is
  // the live symptom the pane exists for: a reasoning field went out and no
  // reasoning delta came back.
  responseShape: driveDetail({
    reasoning_adaptation: null,
    reasoning_adaptation_kind: null,
    route_attempts: [
      detailAttempt({
        wire_body: { model: "glm-5.3-flash" },
        reasoning_emitted: 1,
        params: {
          wire: { model: "glm-5.3-flash" },
          response_shape: {
            fields: { content: { deltas: 7, chars: 135 } },
            chunks: 9,
            finish_reason: "stop",
            usage: true,
            usage_keys: ["completion_tokens", "prompt_tokens"],
            first_chunk_ms: 14700,
          },
        },
      }),
    ],
  }),
  responseShapeAbsent: driveDetail({
    reasoning_adaptation: null,
    reasoning_adaptation_kind: null,
    route_attempts: [
      detailAttempt({ wire_body: { model: "m" }, reasoning_emitted: 0 }),
    ],
  }),
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
  /* Nothing was asked of the wire and nothing arrived on it: the row and the
     body agree, and a badge here would flag correct behaviour. */
  nothingSent: driveDetail({
    reasoning_adaptation: "NO REASONING INSTRUCTION SENT: ...",
    reasoning_adaptation_kind: "nothing_sent",
    route_attempts: [detailAttempt({ wire_body: { model: "m" }, reasoning_emitted: 0 })],
  }),
  /* A thinking attempt whose output allowance was raised to the routed
     model's own published limit. params.wire.max_tokens carries the "to";
     params.output_widened_from is the "from", and it is only ever present
     when the raise actually happened. */
  widened: driveDetail({
    reasoning_adaptation: null,
    reasoning_adaptation_kind: null,
    route_attempts: [
      detailAttempt({
        reasoning_emitted: 1,
        params: {
          output_widened_from: 64000,
          wire: {
            model: "MiniMaxAI/MiniMax-M3",
            max_tokens: 131072,
            reasoning: { reasoning_effort: "max" },
          },
        },
      }),
    ],
  }),
  /* A body from a dialect whose knobs the pane was never taught by name. Every
     one of these was already being captured and stored; none of them reached
     the screen while the block rendered a hard-coded list. */
  unusualKnobs: driveDetail({
    reasoning_adaptation: null,
    reasoning_adaptation_kind: null,
    route_attempts: [
      detailAttempt({
        reasoning_emitted: 1,
        params: {
          wire: {
            model: "deepseek-ai/DeepSeek-V4",
            max_tokens: 32768,
            tools: 12,
            temperature: 0.6,
            top_k: 40,
            repetition_penalty: 1.05,
            reasoning: { reasoning_effort: "high" },
            min_p: 0.05,
            parallel_tool_calls: false,
            response_format: { type: "json_object" },
            tool_choice: "auto",
            "extra_body.chat_template_kwargs": { thinking: true },
          },
        },
      }),
    ],
  }),
  /* The same outcome as written by a pre-6.6.0 server, which had one value for
     both meanings. Stored rows are never migrated, so this shape is still on
     disk on every upgraded install. */
  legacyDropped: driveDetail({
    reasoning_adaptation: "REASONING LEVEL DROPPED: ...",
    reasoning_adaptation_kind: "dropped",
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
  /* The defect this whole panel exists for: one attempt, fifteen upstream
     tries, and a database row that recorded one status on one key. */
  ladder: driveDetail({
    reasoning_adaptation: null,
    reasoning_adaptation_kind: null,
    route_attempts: [
      detailAttempt({
        outcome: "failed",
        error_kind: "upstream",
        error_message: "Upstream provider NIM returned HTTP 502.",
        duration_ms: 107534.16,
        key_index: 2,
        key_label: "cc...dd",
        params: {
          ladder: {
            tries: [
              {
                source: "upstream",
                key_index: 0,
                key_label: "aa...bb",
                status: 429,
                upstream_ms: 410,
                waited_ms: 2700,
                body: '{"detail":"Too many requests"}',
              },
              {
                source: "upstream",
                key_index: 2,
                key_label: "cc...dd",
                status: 502,
                upstream_ms: 830,
                retry_after: 12,
                /* 7.20.0: the head of a reply whose Content-Encoding did not
                   describe its bytes. Recorded only on that row, so every
                   other try here renders exactly as it did before. */
                response_head: {
                  status: 502,
                  content_type: "text/html",
                  content_encoding: "gzip",
                  content_length: 0,
                  server: "cloudflare",
                  cf_ray: "a3c9a1dc-ORD",
                  retry_after: "25517",
                  body_head_hex: "3c68746d6c3e",
                  decode_error: "Error -3 while decompressing data",
                },
              },
              { source: "limiter_wait", waited_ms: 51900 },
            ],
            summary: {
              tries: 2,
              statuses_by_code: { 429: 1, 502: 1 },
              keys: 2,
              time_upstream_ms: 1240,
              time_sleeping_ms: 2700,
              time_limiter_ms: 51900,
              tries_dropped: 0,
            },
            credentials: [
              {
                key_index: 0,
                key_label: "aa...bb",
                class: "rate_limit",
                benched_for_s: 60,
                status: 429,
                retry_after: null,
                reason: "429, no Retry-After -- operator cooldown 60s",
              },
              {
                key_index: 2,
                key_label: "cc...dd",
                class: null,
                benched_for_s: null,
                status: 502,
                retry_after: null,
                reason: "502 is not credential-shaped",
              },
            ],
            root_cause:
              "2 tries across 2 keys: 1×429, 1×502 — 2s of the 107s were MCC backoff sleeps",
          },
        },
      }),
    ],
  }),
  /* One try, nothing hidden: no headline, no root cause, no try list -- and
     with a single attempt the whole panel stays hidden, as before. */
  singleTry: driveDetail({
    reasoning_adaptation: null,
    reasoning_adaptation_kind: null,
    route_attempts: [
      detailAttempt({
        params: {
          ladder: {
            tries: [{ source: "upstream", key_index: 0, status: 200 }],
            summary: {
              tries: 1,
              statuses_by_code: {},
              keys: 1,
              time_sleeping_ms: 0,
              time_limiter_ms: 0,
              tries_dropped: 0,
            },
            credentials: [],
            root_cause: "",
          },
        },
      }),
    ],
  }),
  /* One upstream try and one diagnostic probe: the routed-around 429. The
     gate used to read summary.tries alone, so precisely the case an operator
     asks about -- "why did my request go somewhere else?" -- showed no
     ladder at all. */
  /* 7.45.1: every proxy dial on the ladder. The 09-16 shape on attempt 0 --
     a 429 on the first address, a dead second one, and a third dial that
     never finished -- and a fallback that went out through one address and
     was answered. */
  proxyDials: driveDetail({
    reasoning_adaptation: null,
    reasoning_adaptation_kind: null,
    route_attempts: [
      detailAttempt({
        outcome: "failed",
        error_kind: "interrupted",
        error_message: "The client went away.",
        key_index: 0,
        key_label: "aa...bb",
        params: {
          ladder: {
            tries: [
              { source: "upstream", key_index: 0, status: 429, upstream_ms: 3800 },
              { source: "upstream", key_index: 0, kind: "ConnectTimeout", upstream_ms: 10000 },
            ],
            summary: {
              tries: 2,
              statuses_by_code: { 429: 1, ConnectTimeout: 1 },
              keys: 1,
              time_sleeping_ms: 0,
              time_limiter_ms: 0,
              tries_dropped: 0,
            },
            credentials: [],
            root_cause: "",
            dials: [
              {
                at_try: 0,
                proxy: "173.249.24.121:1080",
                connect_ms: 41.5,
                handshake_ms: 310.2,
                verdict_ms: 3800,
                outcome: "switched",
                switch_ms: 12.4,
                reason: "429",
              },
              {
                at_try: 1,
                proxy: "45.77.244.108:1080",
                connect_ms: 88,
                verdict_ms: 10000,
                outcome: "switched",
                switch_ms: 3,
                reason: "ConnectTimeout",
              },
              {
                at_try: 2,
                proxy: "direct",
                outcome: "dialing",
                elapsed_ms: 2820000,
              },
            ],
          },
        },
      }),
      detailAttempt({
        attempt: 1,
        outcome: "succeeded",
        key_index: 0,
        key_label: "aa...bb",
        params: {
          ladder: {
            tries: [{ source: "upstream", key_index: 0, upstream_ms: 900 }],
            summary: {
              tries: 1,
              statuses_by_code: {},
              keys: 1,
              time_sleeping_ms: 0,
              time_limiter_ms: 0,
              tries_dropped: 0,
            },
            credentials: [],
            root_cause: "",
            dials: [{ at_try: 0, proxy: "10.0.0.9:3128", verdict_ms: 900, outcome: "answered" }],
          },
        },
      }),
    ],
  }),
  /* One attempt, one try, one dial: the panel used to stay hidden for this
     shape, and the dial is the only place the address is shown per attempt. */
  proxyOneDial: driveDetail({
    reasoning_adaptation: null,
    reasoning_adaptation_kind: null,
    route_attempts: [
      detailAttempt({
        params: {
          ladder: {
            tries: [{ source: "upstream", key_index: 0, status: 429, upstream_ms: 3800 }],
            summary: {
              tries: 1,
              statuses_by_code: { 429: 1 },
              keys: 1,
              time_sleeping_ms: 0,
              time_limiter_ms: 0,
              tries_dropped: 0,
            },
            credentials: [],
            root_cause: "",
            dials: [
              {
                at_try: 0,
                proxy: "173.249.24.121:1080",
                verdict_ms: 3800,
                outcome: "failed",
                idle_ms: 2820000,
                reason: "429",
              },
            ],
          },
        },
      }),
    ],
  }),
  singleTryWithProbe: driveDetail({
    reasoning_adaptation: null,
    reasoning_adaptation_kind: null,
    route_attempts: [
      detailAttempt({
        outcome: "failed",
        error_kind: "rate_limit",
        error_message: "Rate limited.",
        key_index: 0,
        key_label: "aa...bb",
        params: {
          ladder: {
            tries: [
              {
                source: "upstream",
                key_index: 0,
                key_label: "aa...bb",
                status: 429,
                upstream_ms: 210,
              },
              {
                source: "probe",
                key_index: 0,
                key_label: "aa...bb",
                status: 200,
                upstream_ms: 240,
              },
            ],
            summary: {
              tries: 1,
              probes: 1,
              statuses_by_code: { 429: 1 },
              keys: 1,
              time_upstream_ms: 450,
              time_sleeping_ms: 0,
              time_limiter_ms: 0,
              tries_dropped: 0,
            },
            credentials: [
              {
                key_index: 0,
                key_label: "aa...bb",
                class: "rate_limit",
                benched_for_s: null,
                model: "moonshotai/kimi-k3",
                model_benched_for_s: 60,
                status: 429,
                retry_after: null,
                reason: "429, no Retry-After -- moonshotai/kimi-k3 benched 60s on this key",
              },
            ],
            root_cause: "",
          },
        },
      }),
    ],
  }),
  /* A stream that had already reached the client and then stalled. One
     attempt, no ladder: the panel used to hide itself in exactly this shape,
     which is the only place the truncation is ever said. */
  truncated: driveDetail({
    reasoning_adaptation: null,
    reasoning_adaptation_kind: null,
    route_attempts: [
      detailAttempt({
        outcome: "failed",
        error_kind: "timeout",
        error_message:
          "Provider 'commandcode/z-ai/glm-5.3-flash' stopped producing output for 180s.",
        params: {
          truncated_after_commit: {
            chars: 1333,
            blocks: 1,
            reason: "timeout",
            stop_reason_sent: "max_tokens",
            ended_cleanly: true,
          },
        },
      }),
    ],
  }),
  /* The one case that still errors: the stream stopped mid tool-call. */
  truncatedTool: driveDetail({
    reasoning_adaptation: null,
    reasoning_adaptation_kind: null,
    route_attempts: [
      detailAttempt({
        outcome: "failed",
        error_kind: "timeout",
        error_message: "Provider stopped producing output for 180s.",
        params: {
          truncated_after_commit: {
            chars: 902,
            blocks: 2,
            reason: "incomplete_tool_use",
            stop_reason_sent: null,
            ended_cleanly: false,
          },
        },
      }),
    ],
  }),
  /* A message one model started and another finished. There is no seam in the
     stream itself, so this row is the only place the model change is said. */
  continued: driveDetail({
    reasoning_adaptation: null,
    reasoning_adaptation_kind: null,
    route_attempts: [
      detailAttempt({
        outcome: "failed",
        error_kind: "timeout",
        error_message:
          "Provider 'commandcode/z-ai/glm-5.3-flash' stopped producing output for 180s.",
      }),
      detailAttempt({
        attempt: 1,
        model_ref: "commandcode/Qwen/Qwen3.8-Flash",
        outcome: "succeeded",
        error_kind: null,
        error_message: null,
        params: {
          continuation: {
            resumed_from_model: "commandcode/z-ai/glm-5.3-flash",
            prefix_chars: 1333,
            continued_chars: 412,
            dropped_overlap_chars: 0,
            accepted: true,
          },
        },
      }),
    ],
  }),
  /* The continuation ran but said nothing usable; the reader got the short
     message instead, and the row has to say so rather than claim a rescue. */
  continuedUnusable: driveDetail({
    reasoning_adaptation: null,
    reasoning_adaptation_kind: null,
    route_attempts: [
      detailAttempt({
        outcome: "failed",
        error_kind: "timeout",
        error_message: "Provider stopped producing output for 180s.",
      }),
      detailAttempt({
        attempt: 1,
        model_ref: "commandcode/Qwen/Qwen3.8-Flash",
        outcome: "failed",
        error_kind: "timeout",
        error_message: "Provider stopped producing output for 180s.",
        params: {
          continuation: {
            resumed_from_model: "commandcode/z-ai/glm-5.3-flash",
            prefix_chars: 1333,
            continued_chars: 0,
            dropped_overlap_chars: 0,
            accepted: false,
          },
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
  /* A model the chain removed before the request began. The row must say WHY
     -- which mode decided, on what evidence, and how long is left -- not the
     one fixed sentence about consecutive failures every skip used to get. */
  benchReason: driveDetail({
    reasoning_adaptation: null,
    reasoning_adaptation_kind: null,
    route_attempts: [
      detailAttempt({
        outcome: "skipped",
        duration_ms: null,
        error_kind: "ejected",
        error_message:
          "benched: 5 upstream errors in the last 10 attempts (rate_based >= 50%), 22 s left",
        params: {
          bench: {
            mode: "rate_based",
            failures: 5,
            window: 10,
            rate: 0.5,
            last_kind: "upstream",
            last_status: 502,
            remaining_seconds: 22.0,
            since: 8.0,
          },
        },
      }),
      detailAttempt({ attempt: 1, model_ref: "commandcode/kimi-k3" }),
    ],
  }),
};

/* ------------------------------------------------- Models page dialect panel
   Driven directly, because the panel's whole job is to say WHERE a dialect
   came from -- and the three origins cannot all be reached from one fixture
   catalogue. */
function drivePanel(dialect) {
  const node = window.eval(`buildDialectPanel(${JSON.stringify(dialect)})`);
  if (!node) return null;
  return {
    subhead: (node.querySelector(".models-subhead").textContent || "").trim(),
    origin: node.querySelector(".models-dialect-origin")
      ? node.querySelector(".models-dialect-origin").textContent
      : null,
    notes: Array.from(node.querySelectorAll(".models-empty-note")).map((el) =>
      (el.textContent || "").replace(/\s+/g, " ").trim(),
    ),
  };
}

const dialectPanels = {
  default: drivePanel({
    known: true,
    effort_field: "reasoning_effort",
    effort_values: ["high", "low", "medium", "minimal"],
    toggle: false,
    toggle_field: null,
    budget: false,
    budget_field: null,
    off: false,
    adaptive: false,
    origin: "default",
    origin_label: "default OpenAI dialect",
    learned_rejections: [],
  }),
  declared: drivePanel({
    known: true,
    effort_field: null,
    effort_values: null,
    toggle: true,
    toggle_field: "chat_template_kwargs.thinking",
    budget: true,
    budget_field: "reasoning_budget",
    off: true,
    adaptive: false,
    origin: "declared",
    origin_label: "declared by this provider",
    learned_rejections: [],
  }),
  learned: drivePanel({
    known: true,
    effort_field: null,
    effort_values: null,
    toggle: false,
    toggle_field: null,
    budget: false,
    budget_field: null,
    off: false,
    adaptive: false,
    origin: "learned",
    origin_label: "learned from the host's own rejection",
    learned_rejections: [{ field: "reasoning_effort", since: "2026-08-29" }],
  }),
  unknown: drivePanel({ known: false }),
};

// ------------------------------------------------------------------ models
const settle = () => new Promise((resolve) => setTimeout(resolve, 140));
const modelsLink = navLinks.find((link) => link.dataset.view === "models");
const models = { present: Boolean(modelsLink) };
if (modelsLink) {
  modelsLink.click();
  await settle();
  const view = doc.querySelector('.admin-view[data-view="models"]');
  const tree = doc.getElementById("modelsTree");
  const bar = doc.getElementById("modelsBulkBar");
  const panel = doc.getElementById("modelsBulkResult");
  const rows = () => Array.from(tree.querySelectorAll(".models-model-row"));
  const boxes = () => Array.from(tree.querySelectorAll("input.models-select"));
  const flat = (el) => (el.textContent || "").replace(/\s+/g, " ").trim();
  const click = async (el, init) => {
    el.dispatchEvent(new window.MouseEvent("click", { bubbles: true, ...init }));
    await settle();
  };

  models.providerCount = tree.querySelectorAll(".models-provider").length;
  models.collapsedBodies = Array.from(
    tree.querySelectorAll(".models-provider-body"),
  ).filter((body) => body.hidden).length;
  models.rowsWhileCollapsed = rows().length;
  models.viewNodesCollapsed = view.querySelectorAll("*").length;
  models.stickyHeads = tree.querySelectorAll(".models-provider-head").length;
  models.facets = Array.from(doc.querySelectorAll(".models-facet")).map((chip) => [
    flat(chip),
    chip.getAttribute("aria-pressed"),
  ]);

  const firstToggle = tree.querySelector(".models-provider-toggle");
  await click(firstToggle);
  models.rowsAfterOpen = rows().length;
  models.moreLabel = flat(tree.querySelector(".models-more"));
  models.selectBoxes = boxes().length;
  models.visibilityReadouts = tree.querySelectorAll(".models-visible-state").length;
  // The row's second checkbox is gone: there is one control and one readout.
  models.readoutInputs = tree.querySelectorAll(
    ".models-visible-state input",
  ).length;
  models.readoutWords = Array.from(
    new Set(
      Array.from(tree.querySelectorAll(".models-visible-word")).map(flat),
    ),
  ).sort();
  models.measuredBadges = tree.querySelectorAll(".models-chip-measured").length;
  // The Learned column: one chip per stored fact, the stale one marked as
  // such and the disagreement marked as such, plus the refresh readout.
  models.learnedChips = tree.querySelectorAll(".models-chip-learned").length;
  models.learnedStaleChips = tree.querySelectorAll(
    ".models-chip-learned-stale",
  ).length;
  models.learnedDisagreeChips = tree.querySelectorAll(
    ".models-chip-learned-disagree",
  ).length;
  models.learnedFacet = Array.from(doc.querySelectorAll(".models-facet"))
    .map(flat)
    .filter((label) => label.startsWith("Learned"));
  models.refreshReadout = flat(doc.getElementById("modelsRefreshReadout"));

  // --- plain click, then a shift-click range
  await click(boxes()[0]);
  models.afterOneClick = flat(bar);
  await click(boxes()[8], { shiftKey: true });
  models.afterShiftClick = tree.querySelectorAll(".models-model-row.is-selected")
    .length;
  models.barSentence = flat(bar);
  models.barHidden = bar.hidden;

  // --- provider checkbox is tri-state
  const selectAll = tree.querySelector("input.models-select-all");
  models.indeterminateWhenPartial = selectAll.indeterminate;
  selectAll.checked = true;
  selectAll.dispatchEvent(new window.Event("change", { bubbles: true }));
  await settle();
  models.selectAllChecked = selectAll.checked;
  models.selectAllIndeterminate = selectAll.indeterminate;
  models.selectedAfterSelectAll = flat(bar);
  // 45 of 45 alpha models: the whole-provider glob is offered, not taken.
  const promote = Array.from(bar.querySelectorAll("button")).find((button) =>
    flat(button).startsWith("Hide all as one pattern"),
  );
  models.promoteOffer = promote ? flat(promote) : "";

  // --- a selection survives a filter rebuild
  const filter = doc.getElementById("modelsFilter");
  filter.value = "mo";
  filter.dispatchEvent(new window.Event("input", { bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 320));
  models.barAfterTyping = flat(bar);
  filter.value = "";
  filter.dispatchEvent(new window.Event("input", { bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 320));

  // --- Escape clears
  doc.dispatchEvent(
    new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
  );
  await settle();
  models.barHiddenAfterEscape = bar.hidden;

  // --- Shift+ArrowDown extends, Shift+ArrowUp shrinks
  const openToggle = tree.querySelector(".models-provider-toggle");
  if (tree.querySelector(".models-provider-body").hidden) await click(openToggle);
  await click(boxes()[2]);
  const arrow = (key) =>
    doc.activeElement && doc.activeElement.dispatchEvent(
      new window.KeyboardEvent("keydown", { key, shiftKey: true, bubbles: true }),
    );
  boxes()[2].focus();
  arrow("ArrowDown");
  await settle();
  arrow("ArrowDown");
  await settle();
  models.afterArrowDown = tree.querySelectorAll(".models-model-row.is-selected")
    .length;
  arrow("ArrowUp");
  await settle();
  models.afterArrowUp = tree.querySelectorAll(".models-model-row.is-selected")
    .length;

  // --- pointer drag across five rows
  window.eval("clearModelsSelection()");
  await settle();
  const cells = Array.from(tree.querySelectorAll(".models-select-cell"));
  const press = (el, init) =>
    el.dispatchEvent(
      new window.MouseEvent("pointerdown", { bubbles: true, button: 0, ...init }),
    );
  press(cells[10]);
  for (let index = 10; index < 15; index += 1) {
    rows()[index].dispatchEvent(
      new window.MouseEvent("pointerover", { bubbles: true }),
    );
  }
  doc.dispatchEvent(new window.MouseEvent("pointerup", { bubbles: true }));
  await settle();
  models.afterDrag = tree.querySelectorAll(".models-model-row.is-selected").length;

  // --- a drag that starts outside the gutter selects nothing
  window.eval("clearModelsSelection()");
  await settle();
  rows()[20]
    .querySelector(".models-ref")
    .dispatchEvent(
      new window.MouseEvent("pointerdown", { bubbles: true, button: 0 }),
    );
  for (let index = 20; index < 25; index += 1) {
    rows()[index].dispatchEvent(
      new window.MouseEvent("pointerover", { bubbles: true }),
    );
  }
  doc.dispatchEvent(new window.MouseEvent("pointerup", { bubbles: true }));
  await settle();
  models.afterNonGutterDrag = tree.querySelectorAll(
    ".models-model-row.is-selected",
  ).length;
  window.eval("clearModelsSelection()");
  await settle();

  // --- the readout, driven directly: three states, no control in any of them
  const readout = (model) =>
    window.eval(`buildModelsVisibilityState(${JSON.stringify(model)})`);
  const shownState = readout({ model_ref: "beta/a", visible: true, hidden_by: "" });
  const ownState = readout({ model_ref: "beta/b", visible: false, hidden_by: "" });
  const globState = readout({
    model_ref: "beta/c:free",
    visible: false,
    hidden_by: "*:free",
  });
  models.readoutShown = flat(shownState);
  models.readoutOwnHidden = flat(ownState);
  models.readoutGlobHidden = flat(globState);
  models.readoutGlobPattern = flat(
    globState.querySelector(".models-blocked-pattern"),
  );
  models.readoutGlobIsButton =
    globState.querySelector(".models-blocked-pattern").tagName;
  models.readoutGlobAria = globState
    .querySelector(".models-blocked-pattern")
    .getAttribute("aria-label");
  models.readoutsHaveNoInput =
    [shownState, ownState, globState].every(
      (node) => node.querySelectorAll("input").length === 0,
    );
  // Clicking the pattern offers the one edit that would free the row.
  const patternButton = globState.querySelector(".models-blocked-pattern");
  await click(patternButton);
  models.patternOffer = flat(patternButton);

  // --- one row, through the one write path there is
  const soloRef = rows()[1].dataset.ref;
  models.soloWordBefore = flat(rows()[1].querySelector(".models-visible-word"));
  BULK_RESULT.action = "hide";
  BULK_RESULT.scope = "selection";
  BULK_RESULT.provider_id = null;
  BULK_RESULT.wrote_glob = null;
  BULK_RESULT.removed_patterns = [];
  BULK_RESULT.honored_count = 1;
  BULK_RESULT.unhonored_count = 0;
  BULK_RESULT.results = [
    { model_ref: soloRef, visible: false, honored: true, hidden_by: "" },
  ];
  BULK_RESULT.visibility = { allow: [], deny: ["*:free", soloRef] };
  await click(boxes()[1]);
  models.soloBarLabels = Array.from(bar.querySelectorAll("button")).map(flat);
  fetchCalls.length = 0;
  fetchBodies.length = 0;
  const hideOne = Array.from(bar.querySelectorAll("button")).find((button) =>
    flat(button).startsWith("Hide 1 selected"),
  );
  if (hideOne) await click(hideOne);
  models.soloBulkCalls = fetchCalls.filter((path) =>
    path.endsWith("/visibility/bulk"),
  ).length;
  models.soloToggleCalls = fetchCalls.filter((path) =>
    path.endsWith("/visibility/toggle"),
  ).length;
  models.soloRefetches = fetchCalls.filter(
    (path) => path === "/admin/api/model-admin",
  ).length;
  models.soloBody = fetchBodies.find((entry) =>
    entry.path.endsWith("/visibility/bulk"),
  );
  const soloRow = rows().find((row) => row.dataset.ref === soloRef);
  models.soloWordAfter = flat(soloRow.querySelector(".models-visible-word"));
  models.soloHeadAfter = flat(
    tree.querySelector('.models-provider[data-provider="alpha"] .models-chip-hidden'),
  );

  // --- a single write a glob overrules leaves the explanation in the row
  const blockedRef = rows()[2].dataset.ref;
  BULK_RESULT.honored_count = 0;
  BULK_RESULT.unhonored_count = 1;
  BULK_RESULT.results = [
    {
      model_ref: blockedRef,
      visible: false,
      honored: false,
      blocked_by: "*:free",
      hidden_by: "*:free",
    },
  ];
  window.eval("clearModelsSelection()");
  await settle();
  await click(boxes()[2]);
  const hideBlocked = Array.from(bar.querySelectorAll("button")).find((button) =>
    flat(button).startsWith("Hide 1 selected"),
  );
  if (hideBlocked) await click(hideBlocked);
  const blockedRow = rows().find((row) => row.dataset.ref === blockedRef);
  models.blockedRowText = flat(blockedRow.querySelector(".models-visible-state"));
  models.blockedRowPattern = flat(
    blockedRow.querySelector(".models-blocked-pattern"),
  );
  // Dismiss the panel: the row keeps saying it, which the toast never did.
  const dismiss = Array.from(panel.querySelectorAll("button")).find(
    (button) => flat(button) === "Dismiss",
  );
  if (dismiss) await click(dismiss);
  models.blockedRowSurvivesDismiss = flat(
    blockedRow.querySelector(".models-blocked-pattern"),
  );

  // --- the migration is previewed, and the preview is not a write
  fetchBodies.length = 0;
  await click(doc.getElementById("modelsMigrateGlobs"));
  models.migrateBody = fetchBodies.find((entry) =>
    entry.path.endsWith("/visibility/migrate-globs"),
  );
  models.migrateText = flat(panel);
  models.migrateOffersWrite = Array.from(panel.querySelectorAll("button")).some(
    (button) => flat(button).startsWith("Write the"),
  );

  BULK_RESULT.action = "hide";
  BULK_RESULT.scope = "provider";
  BULK_RESULT.provider_id = "alpha";
  BULK_RESULT.wrote_glob = "alpha/*";
  BULK_RESULT.visibility = { allow: [], deny: ["alpha/*"] };
  window.eval("clearModelsSelection()");
  await settle();

  // --- Hide all: one request, no refs, no refetch
  BULK_RESULT.results = MODEL_ADMIN_PAGE.providers[0].models.map((model) => ({
    model_ref: model.model_ref,
    visible: false,
    honored: true,
    hidden_by: "alpha/*",
  }));
  BULK_RESULT.honored_count = BULK_RESULT.results.length;
  BULK_RESULT.unhonored_count = 0;
  fetchCalls.length = 0;
  fetchBodies.length = 0;
  const hideAll = Array.from(
    tree.querySelectorAll(".models-provider-bulk button"),
  ).find((button) => flat(button) === "Hide all");
  await click(hideAll);
  // 45 models is under the 200-row confirm step, so this lands directly.
  models.bulkCalls = fetchCalls.filter((path) => path.endsWith("/visibility/bulk"))
    .length;
  models.catalogueRefetches = fetchCalls.filter(
    (path) => path === "/admin/api/model-admin",
  ).length;
  models.bulkBody = fetchBodies.find((entry) =>
    entry.path.endsWith("/visibility/bulk"),
  );
  models.resultText = flat(panel);
  models.hasUndo = Array.from(panel.querySelectorAll("button")).some(
    (button) => flat(button) === "Undo",
  );

  // --- a partly overruled result names the pattern once
  BULK_RESULT.results = MODEL_ADMIN_PAGE.providers[0].models.map((model, index) => ({
    model_ref: model.model_ref,
    visible: index < 12,
    honored: index >= 12,
    blocked_by: index < 12 ? "*:free" : undefined,
  }));
  BULK_RESULT.honored_count = 33;
  BULK_RESULT.unhonored_count = 12;
  const showAll = Array.from(
    tree.querySelectorAll(".models-provider-bulk button"),
  ).find((button) => flat(button) === "Show all");
  await click(showAll);
  models.partialText = flat(panel);
  models.patternMentions = (models.partialText.match(/\*:free/g) || []).length;

  // --- undo posts the previous pair and then goes away
  fetchBodies.length = 0;
  const undo = Array.from(panel.querySelectorAll("button")).find(
    (button) => flat(button) === "Undo",
  );
  if (undo) await click(undo);
  models.undoBody = fetchBodies.find(
    (entry) => entry.path === "/admin/api/model-admin/visibility",
  );
  models.undoGoneAfterUse = !Array.from(panel.querySelectorAll("button")).some(
    (button) => flat(button) === "Undo",
  );

  // --- a filtered Hide all sends the refs it can see
  filter.value = "model-1";
  filter.dispatchEvent(new window.Event("input", { bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 320));
  fetchBodies.length = 0;
  const narrowedHide = Array.from(
    tree.querySelectorAll(".models-provider-bulk button"),
  ).find((button) => flat(button) === "Hide all");
  await click(narrowedHide);
  models.filteredBody = fetchBodies.find((entry) =>
    entry.path.endsWith("/visibility/bulk"),
  );
  filter.value = "";
  filter.dispatchEvent(new window.Event("input", { bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 320));

  // --- facets
  const hiddenFacet = Array.from(doc.querySelectorAll(".models-facet")).find(
    (chip) => flat(chip).startsWith("Hidden"),
  );
  await click(hiddenFacet);
  models.hiddenFacetSummary = flat(doc.getElementById("modelsTreeSummary"));
  const overridden = Array.from(doc.querySelectorAll(".models-facet")).find(
    (chip) => flat(chip).startsWith("Overridden"),
  );
  await click(overridden);
  models.overriddenSummary = flat(doc.getElementById("modelsTreeSummary"));
  const all = Array.from(doc.querySelectorAll(".models-facet")).find(
    (chip) => flat(chip).startsWith("All"),
  );
  await click(all);

  // --- select all matches across providers
  filter.value = "model-01";
  filter.dispatchEvent(new window.Event("input", { bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 320));
  const selectMatches = doc.querySelector(".models-select-matches");
  models.selectMatchesLabel = selectMatches ? flat(selectMatches) : "";
  if (selectMatches) await click(selectMatches);
  models.crossProviderSelection = flat(bar);
  filter.value = "";
  filter.dispatchEvent(new window.Event("input", { bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 320));
  window.eval("clearModelsSelection()");
  await settle();

  models.toggledByHidden =
    bar.hasAttribute("hidden") && panel.getAttribute("style") === null;
  const modelSummary = tree.querySelector(".models-model > summary");
  if (modelSummary) modelSummary.click();
  await settle();
  models.openBodies = tree.querySelectorAll(".models-readouts").length;
  models.viewNodesOneProviderOpen = view.querySelectorAll("*").length;

  /* --- the two preference controls, drawn per capability shape.
     Built directly rather than by opening six rows: the editor is a pure
     function of (scope, key, row, editable, preferences), and what is under
     test is the drawing rule, not the disclosure. */
  const describeEditor = (scope, key, row, preferences) => {
    const form = window.eval("buildOverrideEditor")(
      scope,
      key,
      row,
      [],
      preferences,
    );
    const wraps = Array.from(form.querySelectorAll(".models-preference-value"));
    const select = wraps.length ? wraps[0].querySelector("select") : null;
    const number = wraps.length > 1 ? wraps[1].querySelector("input") : null;
    return {
      heads: Array.from(form.querySelectorAll(".models-override-head")).map(flat),
      options: select
        ? Array.from(select.options).map((option) => [
            option.value,
            option.disabled,
            option.title || "",
          ])
        : [],
      selectValue: select ? select.value : "",
      selectDisabled: select ? select.disabled : null,
      notes: wraps.map((wrap) => {
        const note = wrap.querySelector(".models-preference-note");
        return note ? flat(note) : "";
      }),
      numberMax: number ? number.getAttribute("max") : null,
      numberType: number ? number.type : "",
      modes: Array.from(form.querySelectorAll("select.models-override-mode")).map(
        (mode) => mode.value,
      ),
      valueDisabledWhileInherit: select ? select.disabled : null,
    };
  };
  const alpha = MODEL_ADMIN_PAGE.providers[0];

  /* --- the wire surface row (6.74.0) and its override control (7.33.0).
     Built directly for the same reason the preference editors above are: the
     panel is a pure function of (capabilities, labels, model), and what is
     under test is which surfaces it offers -- only the ones the host declares
     -- not the disclosure that reveals it. */
  const describeSurfacePanel = (model) => {
    const panel = window.eval("buildCapabilityPanel")(
      model.capabilities,
      MODEL_ADMIN_PAGE.source_labels || {},
      model,
    );
    const editor = panel.querySelector(".models-surface-override");
    const select = panel.querySelector("select.models-surface-select");
    return {
      rows: Array.from(panel.querySelectorAll("tr th")).map(flat),
      hasEditor: Boolean(editor),
      head: editor ? flat(editor.querySelector(".models-subhead")) : "",
      options: select
        ? Array.from(select.options).map((option) => option.value)
        : [],
      selected: select ? select.value : "",
    };
  };
  models.surfaceMultiDoor = describeSurfacePanel(alpha.models[6]);
  models.surfaceSingleDoor = describeSurfacePanel(alpha.models[0]);

  models.preferences = {};
  [0, 1, 2, 3, 4, 5, 6].forEach((index) => {
    const model = alpha.models[index];
    models.preferences[index] = describeEditor(
      "model",
      model.model_ref,
      model.override,
      model.preferences,
    );
  });
  models.providerPreferences = describeEditor(
    "provider",
    alpha.provider_id,
    alpha.override,
    alpha.preferences,
  );
}

// ----------------------------------------------------- analytics filters
/* The filter row has to apply itself: a select on `change`, a text box after a
   pause, Clear back to defaults -- each producing exactly one reload, at page
   1, with the filter in the query and in persisted state. */
const analytics = {};
{
  const requestsLink = navLinks.find((link) => link.dataset.view === "requests");
  requestsLink.click();
  await new Promise((resolve) => setTimeout(resolve, 200));
  const statsCalls = () =>
    fetchUrls.filter((url) => url.startsWith("/admin/api/requests/stats"));
  const lastStats = () => statsCalls()[statsCalls().length - 1] || "";

  analytics.defaultLocal = doc.getElementById("reqFilterLocal").value;
  analytics.loadSendsLocal = lastStats();

  // --- a select applies on change, without touching Apply, from page 2
  const listCalls = () =>
    fetchUrls.filter((url) => url.startsWith("/admin/api/requests?"));
  doc.getElementById("reqNextPage").dispatchEvent(
    new window.MouseEvent("click", { bubbles: true }),
  );
  await new Promise((resolve) => setTimeout(resolve, 250));
  analytics.pagedUrl = listCalls()[listCalls().length - 1] || "";
  fetchUrls.length = 0;
  const status = doc.getElementById("reqFilterStatus");
  status.value = "error";
  status.dispatchEvent(new window.Event("change", { bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 250));
  analytics.statusChangeLoads = statsCalls().length;
  analytics.statusChangeUrl = lastStats();
  analytics.listUrlAfterStatusChange = listCalls()[listCalls().length - 1] || "";

  // --- the Local answers select is wired the same way
  fetchUrls.length = 0;
  const localSelect = doc.getElementById("reqFilterLocal");
  localSelect.value = "only";
  localSelect.dispatchEvent(new window.Event("change", { bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 250));
  analytics.localChangeLoads = statsCalls().length;
  analytics.localChangeUrl = lastStats();

  // --- typing is debounced into one load, and only after the pause
  fetchUrls.length = 0;
  const search = doc.getElementById("reqFilterSearch");
  for (const text of ["a", "ab", "abc"]) {
    search.value = text;
    search.dispatchEvent(new window.Event("input", { bubbles: true }));
    await new Promise((resolve) => setTimeout(resolve, 60));
  }
  analytics.loadsWhileTyping = statsCalls().length;
  await new Promise((resolve) => setTimeout(resolve, 600));
  analytics.loadsAfterTypingPause = statsCalls().length;
  analytics.typedUrl = lastStats();

  // --- what got remembered
  analytics.persisted = JSON.parse(
    window.localStorage.getItem("mcc-dashboard-state") || "{}",
  ).reqFilters;

  // --- Clear resets to the defaults, including Hide, and reloads once
  fetchUrls.length = 0;
  doc.getElementById("reqClearFilters").dispatchEvent(
    new window.MouseEvent("click", { bubbles: true }),
  );
  await new Promise((resolve) => setTimeout(resolve, 600));
  analytics.clearLoads = statsCalls().length;
  analytics.clearUrl = lastStats();
  analytics.localAfterClear = doc.getElementById("reqFilterLocal").value;
  analytics.searchAfterClear = search.value;
  analytics.persistedAfterClear = JSON.parse(
    window.localStorage.getItem("mcc-dashboard-state") || "{}",
  ).reqFilters;

  // --- Enter still applies immediately rather than waiting out the debounce
  fetchUrls.length = 0;
  search.value = "boom";
  search.dispatchEvent(new window.Event("input", { bubbles: true }));
  search.dispatchEvent(
    new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }),
  );
  await new Promise((resolve) => setTimeout(resolve, 120));
  analytics.loadsRightAfterEnter = statsCalls().length;
  await new Promise((resolve) => setTimeout(resolve, 600));
  analytics.loadsAfterEnterAndPause = statsCalls().length;
}

// ------------------------------------------------------- the log's own size
/* The user's decision was "no capping -- show me the size". So the lifetime
   panel has to say how much of the disk the log is using, and the cost card has
   to ship the denominator behind its totals. */
const logReadout = {};
{
  const requestsLink = navLinks.find((link) => link.dataset.view === "requests");
  requestsLink.click();
  await new Promise((resolve) => setTimeout(resolve, 300));
  logReadout.lifetimeSpan = doc.getElementById("reqLifetimeSpan").textContent;
  logReadout.costNote = doc.getElementById("reqCostNote").textContent;
}

// ------------------------------------------------------- cost panel timing
/* The Analytics page must paint from the three fast answers while the cost
   breakdown -- measured at 9 s on a real 4.5 GB log against 0.11 s for stats --
   is still in flight, and then fill its own card. */
const costPanel = {};
{
  let release = () => {};
  slowRoutes.set(
    "/admin/api/requests/cost",
    new Promise((resolve) => {
      release = resolve;
    }),
  );
  doc.getElementById("reqStatsCards").innerHTML = "";
  doc.getElementById("reqCostNote").textContent = "";
  const requestsLink = navLinks.find((link) => link.dataset.view === "requests");
  requestsLink.click();
  await new Promise((resolve) => setTimeout(resolve, 250));

  // The page has painted and the cost card says what it is doing, with the
  // cost request already sent rather than queued behind the paint.
  costPanel.statCardsWhileCostPending = doc.getElementById("reqStatsCards").children.length;
  costPanel.noteWhileCostPending = doc.getElementById("reqCostNote").textContent;
  costPanel.costRequested = fetchCalls.filter(
    (path) => path === "/admin/api/requests/cost",
  ).length;

  release();
  slowRoutes.delete("/admin/api/requests/cost");
  await new Promise((resolve) => setTimeout(resolve, 250));
  costPanel.noteAfterCostLands = doc.getElementById("reqCostNote").textContent;
}

// ------------------------------------------- deferred answers that land first
/* A free-text search paints its rows from a placeholder while the real stats
   and the real count are fetched off the wait. Those two requests are fired
   before the paint's own Promise.all, so nothing stops them answering first --
   and the paint that follows used to redraw every breakdown from the empty
   placeholder ("No activity") and the pager back to "counting…", until the
   next reload. Both orders are driven here: the deferred answers first, and
   the ordinary order where they land after the paint. */
const deferredRace = {};
{
  const snapshot = () => ({
    providerRows: Array.from(
      doc.querySelectorAll("#reqProviderBreakdown tbody tr"),
    ).map((tr) => tr.textContent.trim()),
    harnessRows: Array.from(
      doc.querySelectorAll("#reqHarnessBreakdown tbody tr"),
    ).map((tr) => tr.textContent.trim()),
    keyRows: doc.querySelectorAll("#reqKeyBreakdown tbody tr").length,
    countingCards: Array.from(
      doc.querySelectorAll("#reqStatsCards .requests-card strong"),
    ).filter((el) => el.textContent.startsWith("counting")).length,
    cards: doc.querySelectorAll("#reqStatsCards .requests-card").length,
    pager: doc.getElementById("reqPageInfo").textContent,
    tableRows: doc.querySelectorAll("#reqTableBody tr").length,
  });
  const listRoute = ROUTES["/admin/api/requests"];
  const countRoute = ROUTES["/admin/api/requests/count"];
  // The list a search really gets: no total, the count deferred.
  ROUTES["/admin/api/requests"] = {
    ...listRoute,
    total: null,
    total_deferred: true,
    has_more: false,
  };
  ROUTES["/admin/api/requests/count"] = { total: 7 };
  const hold = (paths) => {
    const releases = [];
    for (const path of paths) {
      slowRoutes.set(
        path,
        new Promise((resolve) => {
          releases.push(resolve);
        }),
      );
    }
    return () => {
      paths.forEach((path) => slowRoutes.delete(path));
      releases.forEach((release) => release());
    };
  };
  doc.getElementById("reqFilterSearch").value = "race";

  // --- the deferred stats and count answer while the list is still out
  let release = hold(["/admin/api/requests", "/admin/api/requests/lifetime"]);
  let load = window.eval("loadRequestsView()");
  await new Promise((resolve) => setTimeout(resolve, 200));
  deferredRace.deferredFirst_beforePaint = snapshot();
  release();
  await load;
  await new Promise((resolve) => setTimeout(resolve, 200));
  deferredRace.deferredFirst_afterPaint = snapshot();

  // --- the ordinary order: the paint first, the deferred answers after it
  release = hold(["/admin/api/requests/stats", "/admin/api/requests/count"]);
  load = window.eval("loadRequestsView()");
  await load;
  await new Promise((resolve) => setTimeout(resolve, 100));
  deferredRace.paintFirst_beforeDeferred = snapshot();
  release();
  await new Promise((resolve) => setTimeout(resolve, 200));
  deferredRace.paintFirst_afterDeferred = snapshot();

  ROUTES["/admin/api/requests"] = listRoute;
  if (countRoute === undefined) delete ROUTES["/admin/api/requests/count"];
  else ROUTES["/admin/api/requests/count"] = countRoute;
  doc.getElementById("reqFilterSearch").value = "boom";
}

// ------------------------------------------------- the Apply banner (7.48.0)
/* Since 7.48.0 only a handful of fields need a restart, so the banner has to
   say which of the reader's changes asked for one. Three answers from the
   apply route: an automatic restart naming its fields, a manual one, and a
   hot apply that names nothing. The automatic branch schedules a navigation;
   `setTimeout` is held for that one call so jsdom never tries to navigate. */
const applyBanner = {};
{
  const area = doc.getElementById("messageArea");
  const applyRoute = ROUTES["/admin/api/config/apply"];
  // apply() reloads the whole dashboard after a non-automatic answer. That
  // reload is not what is under test and it would run against whatever the
  // blocks above left in ROUTES, so it is held for this block only.
  const realLoad = window.load;
  window.load = async () => {};
  const answer = (restart) => ({
    applied: true,
    valid: true,
    errors: [],
    warnings: [],
    pending_fields: restart.required ? restart.fields : [],
    restart,
  });

  ROUTES["/admin/api/config/apply"] = answer({
    required: true,
    automatic: true,
    admin_url: "/admin",
    fields: ["PORT", "LOG_LEVEL"],
  });
  const realSetTimeout = window.setTimeout;
  window.setTimeout = () => 0;
  try {
    await window.eval("apply()");
  } finally {
    window.setTimeout = realSetTimeout;
  }
  applyBanner.automatic = area.textContent.trim();
  doc.getElementById("applyButton").disabled = false;

  ROUTES["/admin/api/config/apply"] = answer({
    required: true,
    automatic: false,
    admin_url: null,
    fields: ["HOST"],
  });
  await window.eval("apply()");
  applyBanner.manual = area.textContent.trim();

  ROUTES["/admin/api/config/apply"] = answer({
    required: false,
    automatic: false,
    admin_url: null,
    fields: [],
  });
  await window.eval("apply()");
  applyBanner.hot = area.textContent.trim();

  window.load = realLoad;
  if (applyRoute === undefined) delete ROUTES["/admin/api/config/apply"];
  else ROUTES["/admin/api/config/apply"] = applyRoute;
}

// ------------------------------------------------- harness attribution
/* Who sent the request, end to end: the column and its chip, the empty-state
   colspan that has to follow the header, the modal's two wordings, the filter
   through all five of its wiring sites, and the breakdown panel. */
const harnessAttr = {};
{
  const statsCalls = () =>
    fetchUrls.filter((url) => url.startsWith("/admin/api/requests/stats"));
  const listCalls = () =>
    fetchUrls.filter((url) => url.startsWith("/admin/api/requests?"));
  const body = doc.getElementById("reqTableBody");

  harnessAttr.headers = Array.from(
    doc.querySelectorAll(".requests-table thead th"),
  ).map((th) => th.textContent.trim());
  harnessAttr.chips = Array.from(body.querySelectorAll(".harness-chip")).map(
    (chip) => ({ text: chip.textContent, harness: chip.dataset.harness }),
  );
  harnessAttr.harnessCellIndex = (() => {
    const chip = body.querySelector(".harness-chip");
    if (!chip) return -1;
    const cells = Array.from(chip.closest("tr").children);
    return cells.indexOf(chip.closest("td"));
  })();

  // The empty state has to span the header the markup declares, not a number
  // someone typed once.
  window.eval("renderRequestsTable([])");
  harnessAttr.emptyText = body.querySelector("td").textContent;
  harnessAttr.emptyColSpan = Number(
    body.querySelector("td").getAttribute("colspan"),
  );

  // --- the modal says how the attribution was reached, not just what it is
  const metaText = () =>
    Array.from(doc.getElementById("reqDetailMeta").children)
      .map((el) => el.textContent)
      .join("\u0000");
  const harnessLine = () => {
    const nodes = Array.from(doc.getElementById("reqDetailMeta").children);
    const index = nodes.findIndex(
      (el) => el.tagName === "DT" && el.textContent === "Harness",
    );
    return index === -1 ? null : nodes[index + 1].textContent;
  };
  for (const [name, id] of [
    ["explicit", "req-explicit"],
    ["inferred", "req-ua"],
    ["unidentified", "req-none"],
  ]) {
    await window.eval(`openRequestDetail(${JSON.stringify(id)})`);
    await settle();
    harnessAttr[`detail_${name}`] = harnessLine();
    harnessAttr[`keepalive_${name}`] = (() => {
      const nodes = Array.from(doc.getElementById("reqDetailMeta").children);
      const index = nodes.findIndex(
        (el) => el.tagName === "DT" && el.textContent === "Keepalive frames",
      );
      return index === -1 ? null : nodes[index + 1].textContent;
    })();
    window.eval("closeRequestDetail()");
  }
  harnessAttr.detailHasMeta = metaText().includes("Harness");

  // --- the breakdown panel, one row per by_harness entry, named
  //
  // The analytics block above left "boom" in the free-text box, and a
  // free-text search is the one filter the page deliberately answers from
  // `reqPlaceholderStats()` while the real figures are counted off the wait.
  // Reading the panel in that state measures the deferral, not the panel, so
  // the filter is cleared first and the debounced reload is waited out.
  {
    const search = doc.getElementById("reqFilterSearch");
    if (search && search.value) {
      doc.getElementById("reqClearFilters").dispatchEvent(
        new window.MouseEvent("click", { bubbles: true }),
      );
      await new Promise((resolve) => setTimeout(resolve, 600));
    }
  }
  harnessAttr.breakdown = Array.from(
    doc.querySelectorAll("#reqHarnessBreakdown tbody tr"),
  ).map((tr) => Array.from(tr.children).map((td) => td.textContent));
  harnessAttr.breakdownHeaders = Array.from(
    doc.querySelectorAll("#reqHarnessBreakdown thead th"),
  ).map((th) => th.textContent);

  // --- the datalist is filled from the payload, labels and all
  harnessAttr.datalist = Array.from(
    doc.getElementById("reqHarnessOptions").querySelectorAll("option"),
  ).map((option) => [option.value, option.getAttribute("label")]);

  // --- typing the filter: one load, after the pause, from page 1
  doc.getElementById("reqNextPage").dispatchEvent(
    new window.MouseEvent("click", { bubbles: true }),
  );
  await new Promise((resolve) => setTimeout(resolve, 250));
  harnessAttr.pagedUrl = listCalls()[listCalls().length - 1] || "";
  fetchUrls.length = 0;
  const input = doc.getElementById("reqFilterHarness");
  for (const text of ["op", "openc", "opencode"]) {
    input.value = text;
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
    await new Promise((resolve) => setTimeout(resolve, 60));
  }
  harnessAttr.loadsWhileTyping = statsCalls().length;
  await new Promise((resolve) => setTimeout(resolve, 600));
  harnessAttr.loadsAfterTypingPause = statsCalls().length;
  harnessAttr.typedStatsUrl = statsCalls()[statsCalls().length - 1] || "";
  harnessAttr.typedListUrl = listCalls()[listCalls().length - 1] || "";
  harnessAttr.persisted = JSON.parse(
    window.localStorage.getItem("mcc-dashboard-state") || "{}",
  ).reqFilters;

  // --- Clear empties it and forgets it
  fetchUrls.length = 0;
  doc.getElementById("reqClearFilters").dispatchEvent(
    new window.MouseEvent("click", { bubbles: true }),
  );
  await new Promise((resolve) => setTimeout(resolve, 600));
  harnessAttr.clearedValue = input.value;
  harnessAttr.clearUrl = statsCalls()[statsCalls().length - 1] || "";
  harnessAttr.persistedAfterClear = JSON.parse(
    window.localStorage.getItem("mcc-dashboard-state") || "{}",
  ).reqFilters;
}

/* ------------------------------------------------------- credential keys
   The key manager is opened from a credential field's panel. Render both
   pools and report what came out, so a model bench sub-line is proven to
   exist and a pool without one is proven byte-identical. */
const keyManager = {};
for (const [name, key] of [
  ["scoped", "SCOPED_API_KEY"],
  ["plain", "PLAIN_API_KEY"],
  ["credits", "CREDITS_API_KEY"],
]) {
  const panel = doc.createElement("div");
  doc.body.appendChild(panel);
  await window.eval(`renderKeyManager`)(panel, { key });
  const rows = Array.from(panel.querySelectorAll(".key-manager-row"));
  keyManager[name] = rows.map((row) => ({
    badge: (row.querySelector(".key-health-badge")?.textContent || "").trim(),
    badgeTitle: row.querySelector(".key-health-badge")?.title || "",
    benchLine: (row.querySelector(".key-model-benches")?.textContent || "").trim(),
    benchTitle: row.querySelector(".key-model-benches")?.title || "",
    html: row.innerHTML,
  }));
}

/* ------------------------------------------------------------- key rail
   The pool is an ordered list and the order is the failover order, so it is
   driven here the way the route rail is: render it, press a Move button, and
   read what went out on the wire and what the status line said. */
const keyRail = {};
{
  const panel = doc.createElement("div");
  doc.body.appendChild(panel);
  await window.eval(`renderKeyManager`)(panel, { key: "NAMED_API_KEY" });
  const rows = () => Array.from(panel.querySelectorAll(".key-manager-row"));
  const moves = (row) => Array.from(row.querySelectorAll(".key-manager-move"));
  keyRail.rows = rows().map((row) => ({
    id: row.dataset.keyId,
    display: row.dataset.keyDisplay,
    keyText: (row.querySelector(".key-manager-key")?.textContent || "").trim(),
    keyTitle: row.querySelector(".key-manager-key")?.title || "",
    grip: Boolean(row.querySelector(".key-drag-grip")),
    gripLabel: row.querySelector(".key-drag-grip")?.getAttribute("aria-label") || "",
    nameValue: row.querySelector(".key-name-input")?.value ?? null,
    nameMax: row.querySelector(".key-name-input")?.maxLength ?? null,
    moveLabels: moves(row).map((button) => button.textContent),
    moveDisabled: moves(row).map((button) => button.disabled),
  }));
  keyRail.addNamePlaceholder =
    panel.querySelector(".key-add-name")?.placeholder || "";
  keyRail.statusBefore = panel.querySelector(".key-pool-status")?.hidden ?? null;

  // A name that is markup must be text, not markup: this box lands in a page
  // that also renders log rows.
  keyRail.namedRowHtml = rows()[1]?.querySelector(".key-manager-key")?.innerHTML || "";

  // Move the last key to the top, one press at a time.
  fetchBodies.length = 0;
  moves(rows()[2])[0].dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 150));
  keyRail.orderPut =
    fetchBodies.filter((entry) => entry.path.endsWith("/keys/order")).pop() || null;
  const status = panel.querySelector(".key-pool-status");
  keyRail.statusText = (status?.querySelector("p")?.textContent || "").trim();
  keyRail.statusRole = status?.getAttribute("role") || "";
  keyRail.statusLive = status?.getAttribute("aria-live") || "";
  keyRail.undoLabel = status?.querySelector(".key-pool-undo")?.textContent || "";

  // Undo re-PUTs the order we started from.
  fetchBodies.length = 0;
  status?.querySelector(".key-pool-undo")?.dispatchEvent(
    new window.MouseEvent("click", { bubbles: true }),
  );
  await new Promise((resolve) => setTimeout(resolve, 150));
  keyRail.undoPut =
    fetchBodies.filter((entry) => entry.path.endsWith("/keys/order")).pop() || null;

  // Renaming: one PUT, to the name route, and nothing to the order route.
  const fresh = doc.createElement("div");
  doc.body.appendChild(fresh);
  await window.eval(`renderKeyManager`)(fresh, { key: "NAMED_API_KEY" });
  fetchBodies.length = 0;
  const box = fresh.querySelectorAll(".key-name-input")[0];
  box.value = "Work laptop";
  box.dispatchEvent(new window.Event("change", { bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 150));
  keyRail.renamePut = fetchBodies.pop() || null;
  keyRail.renameStatus =
    (fresh.querySelector(".key-pool-status p")?.textContent || "").trim();

  // The keyboard equivalent: ArrowDown on a focused grip is the same move.
  const keyed = doc.createElement("div");
  doc.body.appendChild(keyed);
  await window.eval(`renderKeyManager`)(keyed, { key: "NAMED_API_KEY" });
  fetchBodies.length = 0;
  keyed.querySelectorAll(".key-drag-grip")[0].dispatchEvent(
    new window.KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }),
  );
  await new Promise((resolve) => setTimeout(resolve, 150));
  keyRail.keyboardPut =
    fetchBodies.filter((entry) => entry.path.endsWith("/keys/order")).pop() || null;
}

/* The display join everything else renders through, exercised directly: the
   same function paints the log row, the modal, the ladder and the breakdown. */
const keyNames = {};
{
  window.eval(`adoptKeyNames({"sk-t\u20262222": "Personal"})`);
  keyNames.named = window.eval(`keyReferenceText("sk-t\u20262222")`);
  keyNames.unnamed = window.eval(`keyReferenceText("sk-o\u20261111")`);
  keyNames.missing = window.eval(`keyReferenceText("")`);
  keyNames.breakdownNamed = window.eval(`keyBreakdownLabel("sk-t\u20262222")`);
  keyNames.breakdownUnnamed = window.eval(`keyBreakdownLabel("sk-o\u20261111")`);
  keyNames.ladderNamed = window.eval(
    `ladderTryText({key_index: 1, key_label: "sk-t\u20262222", status: 200}, 1)`,
  );
  keyNames.ladderUnnamed = window.eval(
    `ladderTryText({key_index: 0, key_label: "sk-o\u20261111", status: 200}, 1)`,
  );
  window.eval(`adoptKeyNames({})`);
  keyNames.afterClear = window.eval(`keyReferenceText("sk-t\u20262222")`);
}

requestDetail.reasoningRow = window.eval(
  `formatRequestReasoningEmitted({route_attempts:[{outcome:"succeeded",reasoning_emitted:0}]})`,
);
requestDetail.unmeasuredNumber = window.eval(`formatOptionalNumber(null)`);


/* ------------------------------------------------------------ route rails
   Drag, multi-select, cross-tier copy/move, the primary swap, undo and the
   per-entry pause. Driven last, because every gesture here mutates the same
   chains the calculator block above reads.

   MouseEvent, never PointerEvent: jsdom has no PointerEvent at all, which is
   the reason this feature is built on pointer events rather than HTML5
   drag-and-drop -- an HTML5 implementation could not be driven from here. */
const routing = {};
const routingLink = navLinks.find((link) => link.dataset.view === "model_config");
routing.present = Boolean(routingLink);
if (routingLink) {
  routingLink.click();
  await settle();

  const OPUS = "MODEL_OPUS_FALLBACKS";
  const SONNET = "MODEL_SONNET_FALLBACKS";
  const chainValue = (key) => doc.querySelector(CONTROL_SELECTOR(key)).value;
  const primaryValue = (key) => doc.querySelector(CONTROL_SELECTOR(key)).value;
  const nodes = () => Array.from(doc.querySelectorAll("[data-route-id]"));
  const nodeFor = (id) => nodes().find((node) => node.dataset.routeId === id);
  const rowIds = (key) =>
    nodes()
      .filter((node) => node.dataset.chainKey === key)
      .map((node) => node.dataset.routeId);
  const gripOf = (id) => nodeFor(id).querySelector(".route-drag-grip");
  const selectedCount = () => doc.querySelectorAll("[data-route-id].is-selected").length;
  const statusPanel = doc.getElementById("routeStatus");
  const statusText = () =>
    (statusPanel.querySelector("p")?.textContent || "").replace(/\s+/g, " ").trim();
  const dirtyCount = () => {
    const text = doc.getElementById("dirtyState").textContent || "";
    const match = text.match(/(\d+)/);
    return match ? Number(match[1]) : 0;
  };
  const clickNode = async (id, init) => {
    gripOf(id).dispatchEvent(
      new window.MouseEvent("click", { bubbles: true, ...init }),
    );
    await settle();
  };
  const keyOn = async (id, init) => {
    gripOf(id).dispatchEvent(
      new window.KeyboardEvent("keydown", { bubbles: true, ...init }),
    );
    await settle();
  };
  // `state` is a const inside the eval'd script and never becomes a global,
  // so a live drag is read from the class it puts on the rails instead.
  const dragIsLive = () => doc.querySelectorAll(".route-grid.is-dragging").length > 0;
  const drag = async (fromId, toId, init) => {
    gripOf(fromId).dispatchEvent(
      new window.MouseEvent("pointerdown", { bubbles: true, button: 0 }),
    );
    nodeFor(toId).dispatchEvent(new window.MouseEvent("pointerover", { bubbles: true }));
    doc.dispatchEvent(new window.MouseEvent("pointerup", { bubbles: true, ...init }));
    await settle();
  };

  // --- every rail heading, with whatever alias it carries. The ids other
  // coding agents put on the wire are invisible on this page otherwise.
  routing.tierHeadings = Array.from(doc.querySelectorAll(".route-tier")).map((node) =>
    (node.textContent || "").replace(/\s+/g, " ").trim(),
  );
  routing.aliasChips = Array.from(doc.querySelectorAll(".route-tier-alias")).map((node) =>
    (node.textContent || "").trim(),
  );

  // --- the rail's shape: a grip per node, and the arrows are still there
  routing.railNodes = nodes().length;
  routing.gripCount = doc.querySelectorAll(".route-drag-grip").length;
  routing.opusRows = rowIds(OPUS).length;
  routing.arrowsKept = doc.querySelectorAll(
    `[data-chain-key="${OPUS}"] .model-chain-move`,
  ).length;
  routing.primaryHasGrip = Boolean(
    nodeFor("route:MODEL_OPUS").querySelector(".route-drag-grip"),
  );
  routing.pauseButtons = doc.querySelectorAll(".route-pause-toggle").length;

  // --- selection: plain click, Ctrl-click, Shift-click, Shift+Arrow, Escape
  await clickNode(rowIds(OPUS)[0]);
  routing.afterPlainClick = selectedCount();
  await clickNode(rowIds(OPUS)[2], { ctrlKey: true });
  routing.afterCtrlClick = selectedCount();
  await clickNode(rowIds(OPUS)[0]);
  await clickNode(rowIds(OPUS)[3], { shiftKey: true });
  routing.afterShiftClick = selectedCount();
  await keyOn(rowIds(OPUS)[3], { key: "ArrowDown", shiftKey: true });
  routing.afterShiftArrowDown = selectedCount();
  await keyOn(rowIds(OPUS)[4], { key: "ArrowUp", shiftKey: true });
  routing.afterShiftArrowBack = selectedCount();
  doc.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  await settle();
  routing.afterEscape = selectedCount();

  // --- a drag that starts on the row rather than on the grip moves nothing
  routing.opusBeforeStray = chainValue(OPUS);
  nodeFor(rowIds(OPUS)[0]).dispatchEvent(
    new window.MouseEvent("pointerdown", { bubbles: true, button: 0 }),
  );
  nodeFor(rowIds(OPUS)[3]).dispatchEvent(
    new window.MouseEvent("pointerover", { bubbles: true }),
  );
  doc.dispatchEvent(new window.MouseEvent("pointerup", { bubbles: true }));
  await settle();
  routing.opusAfterStray = chainValue(OPUS);

  // --- reorder one row inside its own rail
  await drag(rowIds(OPUS)[0], rowIds(OPUS)[2]);
  routing.opusAfterReorder = chainValue(OPUS);
  routing.reorderSentence = statusText();
  routing.statusHiddenAttr = statusPanel.hidden;

  // --- Ctrl+Z puts it back, and only once
  doc.dispatchEvent(
    new window.KeyboardEvent("keydown", { key: "z", ctrlKey: true, bubbles: true }),
  );
  await settle();
  routing.opusAfterUndo = chainValue(OPUS);
  doc.dispatchEvent(
    new window.KeyboardEvent("keydown", { key: "z", ctrlKey: true, bubbles: true }),
  );
  await settle();
  routing.opusAfterSecondUndo = chainValue(OPUS);

  // --- Ctrl+Z inside a combobox belongs to the browser's own text undo
  await drag(rowIds(OPUS)[0], rowIds(OPUS)[2]);
  routing.opusBeforeTypingUndo = chainValue(OPUS);
  const someInput = nodeFor(rowIds(OPUS)[0]).querySelector("input");
  someInput.dispatchEvent(
    new window.KeyboardEvent("keydown", { key: "z", ctrlKey: true, bubbles: true }),
  );
  await settle();
  routing.opusAfterTypingUndo = chainValue(OPUS);

  // --- a group drop keeps rail order rather than click order
  const sonnetRows = rowIds(SONNET);
  await clickNode(sonnetRows[1]);
  await clickNode(sonnetRows[0], { ctrlKey: true });
  routing.groupSelected = selectedCount();
  routing.sonnetBeforeGroup = chainValue(SONNET);
  routing.opusBeforeGroup = chainValue(OPUS);
  await drag(sonnetRows[0], rowIds(OPUS)[0]);
  routing.opusAfterGroupCopy = chainValue(OPUS);
  routing.sonnetAfterGroupCopy = chainValue(SONNET);
  routing.groupSentence = statusText();

  // --- undo the copy, then a cross-tier Shift-move that empties the source
  doc.dispatchEvent(
    new window.KeyboardEvent("keydown", { key: "z", ctrlKey: true, bubbles: true }),
  );
  await settle();
  routing.opusAfterGroupUndo = chainValue(OPUS);
  routing.sonnetAfterGroupUndo = chainValue(SONNET);

  const changedRouteKeys = () =>
    Object.keys(window.eval("changedValues()"))
      .filter((key) => key.indexOf("MODEL") === 0)
      .sort();
  const sonnetAgain = rowIds(SONNET);
  await clickNode(sonnetAgain[0]);
  await clickNode(sonnetAgain[sonnetAgain.length - 1], { shiftKey: true });
  await drag(sonnetAgain[0], rowIds(OPUS)[0], { shiftKey: true });
  routing.sonnetAfterMove = chainValue(SONNET);
  routing.opusAfterMove = chainValue(OPUS);
  routing.moveSentence = statusText();
  routing.keysAfterCrossTierMove = changedRouteKeys();

  // --- a Shift-move of a primary out of its own rail is refused
  await clickNode("route:MODEL_HAIKU");
  const haikuPrimaryBeforeSteal = primaryValue("MODEL_HAIKU");
  const opusBeforeSteal = chainValue(OPUS);
  await drag("route:MODEL_HAIKU", rowIds(OPUS)[0], { shiftKey: true });
  routing.haikuPrimaryAfterStealAttempt = primaryValue("MODEL_HAIKU");
  routing.haikuPrimarySurvivedSteal =
    primaryValue("MODEL_HAIKU") === haikuPrimaryBeforeSteal;
  routing.opusUnchangedBySteal = chainValue(OPUS) === opusBeforeSteal;
  routing.strandedSentence = statusText();

  // --- a touch that did not begin on the grip is a scroll, not a drag
  const touchPointerDown = (target, node) => {
    const event = new window.MouseEvent("pointerdown", { bubbles: true, button: 0 });
    // jsdom has no PointerEvent, and MouseEvent drops an unknown init key.
    Object.defineProperty(event, "pointerType", { value: "touch" });
    node.dispatchEvent(event);
    return target;
  };
  const touchBefore = chainValue(OPUS);
  touchPointerDown(null, nodeFor(rowIds(OPUS)[0]));
  routing.touchOnRowStartsADrag = dragIsLive();
  doc.dispatchEvent(new window.MouseEvent("pointerup", { bubbles: true }));
  await settle();
  routing.opusAfterTouchScroll = chainValue(OPUS) === touchBefore;
  touchPointerDown(null, gripOf(rowIds(OPUS)[0]));
  routing.touchOnGripStartsADrag = dragIsLive();
  doc.dispatchEvent(new window.MouseEvent("pointerup", { bubbles: true }));
  await settle();

  doc.dispatchEvent(
    new window.KeyboardEvent("keydown", { key: "z", ctrlKey: true, bubbles: true }),
  );
  await settle();
  routing.sonnetAfterMoveUndo = chainValue(SONNET);
  routing.opusAfterMoveUndo = chainValue(OPUS);

  // --- a copy onto a chain that already holds the ref moves the row it has
  routing.opusBeforeDuplicate = chainValue(OPUS);
  await clickNode(rowIds(SONNET)[0]);
  await drag(rowIds(SONNET)[0], rowIds(OPUS)[0]);
  const opusWithSonnet = chainValue(OPUS);
  routing.opusWithSonnetRef = opusWithSonnet;
  await clickNode(rowIds(SONNET)[0]);
  await drag(rowIds(SONNET)[0], rowIds(OPUS)[4]);
  routing.opusAfterDuplicateCopy = chainValue(OPUS);
  routing.duplicateOccurrences = chainValue(OPUS)
    .split(",")
    .filter((ref) => ref === "p1/s1").length;
  routing.duplicateSentence = statusText();

  // --- a copy onto a chain whose primary is that ref is refused
  routing.sonnetPrimary = primaryValue("MODEL_SONNET");
  await clickNode("route:MODEL_SONNET");
  routing.opusBeforeRefusal = chainValue(OPUS);
  await drag("route:MODEL_SONNET", rowIds(SONNET)[0]);
  routing.sonnetAfterOwnPrimaryDrop = chainValue(SONNET);
  routing.sonnetPrimaryAfterOwnDrop = primaryValue("MODEL_SONNET");
  routing.swapSentence = statusText();
  doc.dispatchEvent(
    new window.KeyboardEvent("keydown", { key: "z", ctrlKey: true, bubbles: true }),
  );
  await settle();

  // The Opus chain now holds p1/s1; dropping it onto Sonnet, whose primary is
  // p1/s0, is fine -- so make the refusal explicit by dropping a ref that IS
  // the destination primary.
  const opusRowForSonnetPrimary = rowIds(OPUS).find(
    (id) => nodeFor(id).querySelector("input").value.trim() === "p1/s1",
  );
  if (opusRowForSonnetPrimary) {
    // Promote p1/s1 to Sonnet's primary first, then try to copy it back in.
    await clickNode(rowIds(SONNET)[0]);
    await drag(rowIds(SONNET)[0], "route:MODEL_SONNET");
    routing.sonnetPrimaryAfterPromote = primaryValue("MODEL_SONNET");
    routing.promoteSentence = statusText();
    await clickNode(opusRowForSonnetPrimary);
    const sonnetBeforeRefusal = chainValue(SONNET);
    await drag(opusRowForSonnetPrimary, rowIds(SONNET)[0]);
    routing.sonnetUnchangedByRefusal = chainValue(SONNET) === sonnetBeforeRefusal;
    routing.refusalSentence = statusText();
  }

  // --- dropping on a primary slot swaps and demotes the old primary
  const dirtyBeforeSwap = dirtyCount();
  const haikuPrimaryBefore = primaryValue("MODEL_HAIKU");
  await clickNode(rowIds(OPUS)[0]);
  const promoted = nodeFor(rowIds(OPUS)[0]).querySelector("input").value.trim();
  await drag(rowIds(OPUS)[0], "route:MODEL_HAIKU");
  routing.haikuPrimaryAfterDrop = primaryValue("MODEL_HAIKU");
  routing.haikuChainAfterDrop = chainValue("MODEL_HAIKU_FALLBACKS");
  routing.haikuDemoted = haikuPrimaryBefore;
  routing.haikuPromoted = promoted;
  routing.dirtyAfterPrimarySwap = dirtyCount() - dirtyBeforeSwap;
  routing.primarySwapSentence = statusText();

  // --- pause: one key on the wire, and a dirty drag stays dirty
  const opusFirstRow = rowIds(OPUS)[0];
  const pausedRef = nodeFor(opusFirstRow).querySelector("input").value.trim();
  const dirtyBeforePause = dirtyCount();
  const chainLengthBeforePause = window.eval(
    'chainLength("MODEL_OPUS", "MODEL_OPUS_FALLBACKS")',
  );
  fetchBodies.length = 0;
  nodeFor(opusFirstRow)
    .querySelector(".route-pause-toggle")
    .dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  const pauseCalls = fetchBodies.filter(
    (entry) => entry.path === "/admin/api/config/route-pause",
  );
  routing.pauseCalls = pauseCalls.length;
  routing.pauseBody = pauseCalls[0] ? pauseCalls[0].body : null;
  routing.pauseBodyKeys = pauseCalls[0] ? Object.keys(pauseCalls[0].body).sort() : [];
  routing.dirtyAfterPause = dirtyCount();
  routing.dirtyUnchangedByPause = dirtyCount() === dirtyBeforePause;
  routing.pausedRef = pausedRef;
  routing.chainLengthBeforePause = chainLengthBeforePause;

  // --- the paused row stays visible, with its full ref and a Resume button
  const pausedNode = nodeFor(opusFirstRow);
  routing.pausedRowHidden = pausedNode.hidden;
  routing.pausedRowClass = pausedNode.classList.contains("is-paused");
  routing.pausedRowRef = pausedNode.querySelector("input").value;
  routing.pausedButtonLabel = pausedNode
    .querySelector(".route-pause-toggle")
    .textContent.trim();
  routing.pausedAriaPressed = pausedNode
    .querySelector(".route-pause-toggle")
    .getAttribute("aria-pressed");
  routing.pausedChipShown = !pausedNode.querySelector(".route-pause-chip").hidden;
  routing.pauseSentence = statusText();
  routing.pauseOffersUndo = Array.from(
    statusPanel.querySelectorAll("button"),
  ).map((button) => button.textContent.trim());
  // The deadline calculator stops counting a model the router will not try.
  routing.chainLengthWhilePaused = window.eval(
    'chainLength("MODEL_OPUS", "MODEL_OPUS_FALLBACKS")',
  );

  // --- the deadline calculator stops counting a paused model
  // --- Undo from the panel resumes it
  const undoButton = Array.from(statusPanel.querySelectorAll("button")).find(
    (button) => button.textContent.trim() === "Undo",
  );
  if (undoButton) {
    undoButton.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await settle();
  }
  routing.resumedButtonLabel = nodeFor(opusFirstRow)
    .querySelector(".route-pause-toggle")
    .textContent.trim();
  routing.resumeSentence = statusText();

  // --- a paused primary keeps its ref on screen and leaves the rail counted
  const haikuPrimaryNode = nodeFor("route:MODEL_HAIKU");
  haikuPrimaryNode
    .querySelector(".route-pause-toggle")
    .dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  routing.haikuPrimaryPaused = haikuPrimaryNode.classList.contains("is-paused");
  routing.haikuPrimaryStillShowsItsRef =
    haikuPrimaryNode.querySelector("input").value;
  routing.haikuChainLengthWithPausedPrimary = window.eval(
    'chainLength("MODEL_HAIKU", "MODEL_HAIKU_FALLBACKS")',
  );
  haikuPrimaryNode
    .querySelector(".route-pause-toggle")
    .dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  routing.haikuPrimaryResumed = !haikuPrimaryNode.classList.contains("is-paused");

  // --- a refused pause says so in the pause panel, not only at the top of
  //     the page, and the row goes back to the state it was really in
  const opusToggle = () => nodeFor(opusFirstRow).querySelector(".route-pause-toggle");
  const messageArea = doc.getElementById("messageArea");
  routing.refusedPauseWasPausedBefore = nodeFor(opusFirstRow).classList.contains(
    "is-paused",
  );
  pauseRefusal = "MODEL_OPUS_PAUSED: the managed env file is read-only.";
  opusToggle().dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  routing.refusedPauseSentence = statusText();
  routing.refusedPausePanelButtons = Array.from(
    statusPanel.querySelectorAll("button"),
  ).map((button) => button.textContent.trim());
  routing.refusedPauseMessageArea = messageArea.textContent.trim();
  routing.refusedPauseRowStillUnpaused = !nodeFor(opusFirstRow).classList.contains(
    "is-paused",
  );
  routing.refusedPauseButtonLabel = opusToggle().textContent.trim();
  routing.refusedPauseButtonDisabled = opusToggle().disabled;
  pauseRefusal = null;

  // --- and so does a pause that fails in transport
  pauseHttpFailure = "the server is restarting";
  opusToggle().dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  routing.failedPauseSentence = statusText();
  routing.failedPausePanelButtons = Array.from(
    statusPanel.querySelectorAll("button"),
  ).map((button) => button.textContent.trim());
  routing.failedPauseRowStillUnpaused = !nodeFor(opusFirstRow).classList.contains(
    "is-paused",
  );
  routing.failedPauseButtonLabel = opusToggle().textContent.trim();
  pauseHttpFailure = null;

  // --- Undo disables the row's own toggle for the whole of its POST
  opusToggle().dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  routing.beforeUndoRowPaused = nodeFor(opusFirstRow).classList.contains("is-paused");
  const undoAgain = Array.from(statusPanel.querySelectorAll("button")).find(
    (button) => button.textContent.trim() === "Undo",
  );
  let releaseUndo = () => {};
  pauseGate = new Promise((resolve) => {
    releaseUndo = resolve;
  });
  undoAgain.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  // Still in flight: the gate has not been released.
  routing.rowToggleDisabledDuringUndo = opusToggle().disabled;
  releaseUndo();
  pauseGate = null;
  await settle();
  routing.rowToggleEnabledAfterUndo = opusToggle().disabled;
  routing.afterUndoRowPaused = nodeFor(opusFirstRow).classList.contains("is-paused");


  // --- the panel is toggled by `hidden`, never by style.display
  const dismiss = Array.from(statusPanel.querySelectorAll("button")).find(
    (button) => button.textContent.trim() === "Dismiss",
  );
  if (dismiss) {
    dismiss.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await settle();
  }
  routing.statusHiddenAfterDismiss = statusPanel.hidden;
  routing.statusInlineDisplay = statusPanel.style.display;

  // --- the arrow buttons still work
  const arrowRow = rowIds(OPUS)[1];
  const beforeArrow = chainValue(OPUS);
  nodeFor(arrowRow)
    .querySelectorAll(".model-chain-move")[0]
    .dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  routing.arrowsStillReorder = chainValue(OPUS) !== beforeArrow;

  // --- a drag leaks no nodes
  routing.nodeCountAtEnd = doc.querySelectorAll("*").length;
  routing.strayIndicators = doc.querySelectorAll(".route-drop-indicator").length;
}

/* ------------------------------------------------------ custom providers
   The card's refresh affordance and its model-count line. jsdom cannot say
   whether the extra text fits; it can say what the card claims. */
const customProviders = {};
{
  const providersLink = navLinks.find((link) => link.dataset.view === "providers");
  if (providersLink) providersLink.click();
  await new Promise((resolve) => setTimeout(resolve, 200));
  const card = doc.querySelector('[data-custom-provider="custom_acme"]');
  customProviders.present = Boolean(card);
  if (card) {
    const button = card.querySelector(".test-button");
    customProviders.buttonLabel = button.textContent;
    customProviders.detailsBefore = card.querySelector(".cp-details").textContent;
    button.click();
    await new Promise((resolve) => setTimeout(resolve, 250));
    customProviders.detailsAfter = card.querySelector(".cp-details").textContent;
    customProviders.pillAfter = card.querySelector(".status-pill").textContent;
  }

  // A create whose discovery failed must not settle into a healthy card.
  doc.getElementById("addCustomProviderButton").click();
  doc.getElementById("cpDisplayName").value = "Bad AI";
  doc.getElementById("cpBaseUrl").value = "https://bad.example/v1";
  doc.getElementById("cpApiKey").value = "sk-test-key";
  doc
    .getElementById("customProviderForm")
    .dispatchEvent(
      new window.Event("submit", { bubbles: true, cancelable: true }),
    );
  await new Promise((resolve) => setTimeout(resolve, 300));
  const failedCard = doc.querySelector('[data-custom-provider="custom_bad_ai"]');
  customProviders.failedPill = failedCard
    ? failedCard.querySelector(".status-pill").textContent
    : null;
  customProviders.failedMeta = failedCard
    ? failedCard.querySelector(".provider-meta").textContent
    : null;
  customProviders.failedDetails = failedCard
    ? failedCard.querySelector(".cp-details").textContent
    : null;
  const banner = doc.getElementById("messageArea");
  customProviders.message = banner ? banner.textContent : "";

  // The learned dialect, the hand-edit field, and the per-key health a custom
  // pool has always had and never showed.
  const acme = doc.querySelector('[data-custom-provider="custom_acme"]');
  if (acme) {
    customProviders.dialectLabel = textOf(acme, ".cp-dialect-label");
    const edit = acme.querySelector(".cp-dialect-edit");
    customProviders.dialectValue = edit ? edit.value : null;
    customProviders.probeButton = textOf(acme, ".cp-dialect-probe");
    customProviders.toggleLabel = textOf(acme, ".cp-toggle");
    customProviders.keyHealthStates = Array.from(
      acme.querySelectorAll(".cp-key-health .key-health-badge"),
    ).map((el) => el.textContent);
    customProviders.keyBenches = Array.from(
      acme.querySelectorAll(".cp-key-health .key-model-benches"),
    ).map((el) => el.textContent);
    customProviders.keyRowCount = acme.querySelectorAll(".cp-key-row").length;
  }

  // The 7.33.0 control: which wire APIs this host serves. The create above
  // went out with the default ticked; opening Edit on a host that declares two
  // must tick exactly those two, and the help text must be on the page beside
  // them (the 7.29.1 contract, which the hand-written custom form is not
  // otherwise covered by).
  customProviders.createdSurfaces = (() => {
    const sent = fetchBodies.filter(
      (entry) =>
        entry.path === "/admin/api/custom-providers" && entry.method === "POST",
    );
    return sent.length ? sent[sent.length - 1].body.surfaces : null;
  })();
  const acmeEdit = doc.querySelector(
    '[data-custom-provider="custom_acme"] .cp-edit',
  );
  if (acmeEdit) acmeEdit.click();
  await new Promise((resolve) => setTimeout(resolve, 50));
  customProviders.surfaceLabels = Array.from(
    doc.querySelectorAll("#cpSurfaces .cp-surface span"),
  ).map((el) => el.textContent);
  customProviders.surfaceChecked = Array.from(
    doc.querySelectorAll("#cpSurfaces input[type=checkbox]"),
  )
    .filter((box) => box.checked)
    .map((box) => box.value);
  customProviders.surfaceHelp = textOf(doc, "#desc-cpSurfaces");
  const messagesBox = doc.querySelector(
    '#cpSurfaces input[data-cp-surface="messages"]',
  );
  if (messagesBox) {
    messagesBox.checked = true;
    doc
      .getElementById("customProviderForm")
      .dispatchEvent(
        new window.Event("submit", { bubbles: true, cancelable: true }),
      );
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  customProviders.patchedSurfaces = (() => {
    const sent = fetchBodies.filter(
      (entry) =>
        entry.path.startsWith("/admin/api/custom-providers/") &&
        entry.method === "PATCH",
    );
    return sent.length ? sent[sent.length - 1].body.surfaces : null;
  })();
}

// ------------------------------------------------------------ coding agents
// Every listed agent starts on the global chain, which is the state twelve of
// the thirteen are in on any real install.
for (const agent of ROUTES["/admin/api/harnesses"].harnesses || []) {
  if (!agent.catalogue) continue;
  ROUTES["/admin/api/harness-tiers"].harnesses[agent.id] = Object.fromEntries(
    ROUTES["/admin/api/harness-tiers"].tiers.map((tier) => [
      tier.id,
      {
        override: false,
        model: "",
        fallbacks: [],
        paused: [],
        resolved: { ...tier.global, source: "global" },
      },
    ]),
  );
}

const codingAgentsLink = navLinks.find(
  (link) => link.dataset.view === "coding_agents",
);
const codingAgents = { present: Boolean(codingAgentsLink) };
const desktopApps = { present: false };
if (codingAgentsLink) {
  codingAgentsLink.click();
  await new Promise((resolve) => setTimeout(resolve, 160));
  const list = doc.getElementById("codingAgentsList");
  const cards = Array.from(list.querySelectorAll(".coding-agent-card"));
  const flatten = (el) => (el.textContent || "").replace(/\s+/g, " ").trim();
  codingAgents.cardCount = cards.length;
  codingAgents.cards = cards.map((card) => ({
    id: card.dataset.harness,
    title: card.querySelector("h4").textContent,
    state: card.querySelector(".agent-state").textContent,
    installed: card.querySelector(".agent-state").classList.contains("installed"),
    unavailable: card.querySelector(".agent-state").classList.contains("unavailable"),
    unavailableReason: card.querySelector(".agent-unavailable-reason")
      ? flatten(card.querySelector(".agent-unavailable-reason"))
      : null,
    command: card.querySelector(".agent-command")
      ? card.querySelector(".agent-command").textContent
      : null,
    commandLines: Array.from(card.querySelectorAll(".agent-command-row")).map(
      (row) => ({
        command: row.querySelector(".agent-command-line").textContent,
        kind: row.dataset.kind,
        help: row.querySelector(".agent-command-help")
          ? flatten(row.querySelector(".agent-command-help"))
          : null,
        hasCopy: Boolean(row.querySelector(".guide-copy-button")),
      }),
    ),
    meta: flatten(card.querySelector(".agent-meta")),
    metaTerms: Array.from(card.querySelectorAll(".agent-meta dt")).map(
      (dt) => dt.textContent,
    ),
    metaValues: Array.from(card.querySelectorAll(".agent-meta dd")).map(
      (dd) => dd.textContent,
    ),
    defaulted: card.querySelector(".agent-defaulted")
      ? flatten(card.querySelector(".agent-defaulted"))
      : null,
    installHint: card.querySelector(".agent-install-hint")
      ? flatten(card.querySelector(".agent-install-hint"))
      : null,
  }));
  codingAgents.gatewayNote = flatten(doc.getElementById("codingAgentsGatewayNote"));

  /* ------------------------------------------------- desktop apps
     Six cards covering every state a probe can return, plus the two card
     shapes that have no button at all. What is asserted here is what the
     server's answer *became* in the DOM: the badge, the preview the plan
     button fetched, and which of the two undo modes each card offers. */
  const desktopList = doc.getElementById("desktopAppsList");
  const desktopCards = Array.from(
    desktopList.querySelectorAll(".desktop-app-card"),
  );
  desktopApps.present = Boolean(desktopList);
  desktopApps.cardCount = desktopCards.length;
  desktopApps.ownershipNote = flatten(
    doc.getElementById("desktopAppsOwnershipNote"),
  );
  desktopApps.cards = desktopCards.map((card) => ({
    id: card.dataset.desktopApp,
    title: card.querySelector("h4").textContent,
    badge: card.querySelector(".agent-state").textContent,
    badgeState: card.querySelector(".agent-state").dataset.state,
    drifted: card.querySelector(".agent-state").classList.contains("drifted"),
    unavailable: card
      .querySelector(".agent-state")
      .classList.contains("unavailable"),
    unavailableReason: card.querySelector(".agent-unavailable-reason")
      ? flatten(card.querySelector(".agent-unavailable-reason"))
      : null,
    driftNote: card.querySelector(".desktop-drift-note")
      ? flatten(card.querySelector(".desktop-drift-note"))
      : null,
    metaTerms: Array.from(card.querySelectorAll(".desktop-app-meta dt")).map(
      (dt) => dt.textContent,
    ),
    metaValues: Array.from(card.querySelectorAll(".desktop-app-meta dd")).map(
      (dd) => dd.textContent,
    ),
    hasConfigure: Boolean(card.querySelector('[data-role="configure"]')),
    configureLabel: card.querySelector('[data-role="configure"]')
      ? card.querySelector('[data-role="configure"]').textContent
      : null,
    hasPreview: Boolean(card.querySelector('[data-role="preview"]')),
    hasUndo: Boolean(card.querySelector('[data-role="undo"]')),
    undoModes: Array.from(
      card.querySelectorAll('[data-role="undo-mode"] option'),
    ).map((option) => ({
      value: option.value,
      label: option.textContent,
      disabled: option.disabled,
    })),
    hasDefaultModelCheckbox: Boolean(
      card.querySelector('[data-role="default-model"]'),
    ),
    instructionLabels: Array.from(
      card.querySelectorAll(".desktop-instruction-label"),
    ).map((label) => label.textContent),
    instructionValues: Array.from(
      card.querySelectorAll(".desktop-instruction-value"),
    ).map((value) => value.textContent),
    copyButtons: card.querySelectorAll(".desktop-copy-button").length,
    notes: Array.from(card.querySelectorAll(".desktop-app-notes p")).map((p) =>
      flatten(p),
    ),
  }));

  // Press "What will this write?" on the one card whose plan the fixture
  // serves, and record what landed in the preview block.
  const codexCard = desktopList.querySelector(
    '.desktop-app-card[data-desktop-app="codex_desktop"]',
  );
  if (codexCard) {
    codexCard.querySelector('[data-role="preview"]').click();
    await new Promise((resolve) => setTimeout(resolve, 120));
    const preview = codexCard.querySelector(".desktop-preview");
    desktopApps.preview = {
      hidden: preview.hidden,
      text: preview.textContent,
    };
  }

  /* ------------------------------------------------------------- tiers
     Five rows per agent that has a picker, none for the one that does not,
     and the same rail component the Model Config page draws once Override is
     pressed. jsdom proves what the gesture did to the DOM and what went out on
     the wire; it cannot prove the rail's grid, which is checked in a browser. */
  const tierCard = (id) => list.querySelector(`.coding-agent-card[data-harness="${id}"]`);
  const tierRows = (id) =>
    Array.from((tierCard(id) || doc).querySelectorAll(".agent-tier"));
  const tierRow = (id, tier) =>
    tierRows(id).find((row) => row.dataset.tier === tier) || null;

  const tiers = {};
  // The fresh-install run lists no agents at all, so there is nothing to
  // exercise and nothing to assert; the shape stays present so the driver can
  // tell "no agents" from "no tiers section".
  tiers.present = Boolean(tierRow("codex", "best"));
  tiers.rowsPerAgent = Object.fromEntries(
    codingAgents.cards.map((card) => [card.id, tierRows(card.id).length]),
  );
  if (!tiers.present) {
    codingAgents.tiers = tiers;
  } else {
  tiers.labels = tierRows("codex").map(
    (row) => row.querySelector(".agent-tier-name").textContent,
  );
  tiers.refs = tierRows("codex").map(
    (row) => row.querySelector(".agent-tier-ref").textContent,
  );
  tiers.inheritedReadout = flatten(
    tierRow("codex", "medium").querySelector(".agent-tier-readout"),
  );
  tiers.inheritedChip = tierRow("codex", "medium")
    .querySelector(".route-state")
    .textContent;
  tiers.railsBeforeOverride = tierRow("codex", "best").querySelectorAll(".route-rail").length;

  // --- Override reveals the shared rail component.
  tierRow("codex", "best")
    .querySelector(".agent-tier-actions button")
    .click();
  await new Promise((resolve) => setTimeout(resolve, 60));
  const best = () => tierRow("codex", "best");
  tiers.railsAfterOverride = best().querySelectorAll(".route-rail").length;
  tiers.primaryValue = best().querySelector(".route-node.is-primary input").value;
  tiers.chainRows = best().querySelectorAll(".model-chain-row").length;
  tiers.overrideChip = best().querySelector(".route-state").textContent;
  tiers.hasGrip = Boolean(best().querySelector("[data-route-id]"));
  tiers.hasPauseButton = Boolean(best().querySelector(".route-pause-toggle"));
  tiers.actionLabels = Array.from(
    best().querySelectorAll(".agent-tier-actions button"),
  ).map((button) => button.textContent);

  // --- The rail's inputs must never join the settings diff.
  tiers.changedValuesKeys = Object.keys(window.eval("changedValues()"));
  tiers.dirtyAfterOverride = doc.getElementById("applyButton").disabled;

  // --- A pointer drag inside the agent's own rail. MouseEvent, never
  //     PointerEvent: jsdom implements neither PointerEvent nor DataTransfer,
  //     which is why the product's drags are built on pointer events at all.
  const railNodes = () =>
    Array.from(best().querySelectorAll("[data-route-id]"));
  const before = railNodes().map(
    (node) => (node.querySelector("input") || {}).value,
  );
  const grip = railNodes()[1] && railNodes()[1].querySelector(".route-drag-grip");
  if (grip) {
    grip.dispatchEvent(new window.MouseEvent("pointerdown", { bubbles: true }));
    railNodes()[0].dispatchEvent(
      new window.MouseEvent("pointerover", { bubbles: true, clientY: 0 }),
    );
    doc.dispatchEvent(new window.MouseEvent("pointerup", { bubbles: true }));
    await new Promise((resolve) => setTimeout(resolve, 40));
  }
  tiers.dragBefore = before;
  tiers.dragAfter = railNodes().map(
    (node) => (node.querySelector("input") || {}).value,
  );

  // --- Revert to global deletes the entry and the rail with it.
  const revert = Array.from(
    best().querySelectorAll(".agent-tier-actions button"),
  ).find((button) => button.textContent === "Revert to global");
  if (revert) {
    revert.click();
    await new Promise((resolve) => setTimeout(resolve, 60));
  }
  tiers.railsAfterRevert = tierRow("codex", "best").querySelectorAll(".route-rail").length;
  tiers.readoutAfterRevert = flatten(
    tierRow("codex", "best").querySelector(".agent-tier-readout"),
  );
  tiers.posts = fetchBodies
    .filter((entry) => entry.path === "/admin/api/harness-tiers")
    .map((entry) => entry.body);
  codingAgents.tiers = tiers;
  }
}

const rtkToggleContainer = doc.getElementById("rtkAgentToggles");
const rtkToggles = rtkToggleContainer
  ? Array.from(rtkToggleContainer.querySelectorAll("input[type=checkbox]")).map(
      (input) => ({
        id: input.id,
        harness: input.dataset.harness,
        checked: input.checked,
        label: (input.parentElement.textContent || "").trim(),
      }),
    )
  : null;

// The Claude subscription card renders from the /sources endpoint, so its
// two interesting states are driven by data rather than by a gesture: a live
// credential with observed usage windows, and a credential MCC has never seen
// a rate-limit header for. Both go through the same renderer.
const anthropicOAuthCard = (() => {
  const render = (sources) => {
    const list = doc.createElement("div");
    const buttons = [
      doc.createElement("button"),
      doc.createElement("button"),
      doc.createElement("button"),
      doc.createElement("button"),
    ];
    buttons.status = doc.createElement("div");
    buttons.details = list;
    window.eval("renderAnthropicOAuthDetails")(list, sources, buttons);
    return {
      hidden: list.hidden,
      text: (list.textContent || "").replace(/\s+/g, " ").trim(),
      expired: Array.from(list.querySelectorAll(".value-expired")).map((node) =>
        (node.textContent || "").trim(),
      ),
      rows: Array.from(list.querySelectorAll(".oauth-account-row")).map((row) => ({
        accountId: row.dataset.accountId,
        name: (row.querySelector(".oauth-account-name")?.textContent || "").trim(),
        refresh: (
          row.querySelector(".oauth-account-refresh")?.textContent || ""
        ).trim(),
        disconnect: (
          row.querySelector(".oauth-account-disconnect")?.textContent || ""
        ).trim(),
        text: (row.textContent || "").replace(/\s+/g, " ").trim(),
      })),
      loginLabel: buttons[1].textContent,
    };
  };
  const now = Math.round(Date.now() / 1000);
  const liveAccount = {
    available: true,
    account_id: "uuid-one",
    name: "first@example.test",
    origin: "mcc",
    ordinal: 1,
    subscription_type: "max",
    rate_limit_tier: "default_claude_max_5x",
    source: "mcc",
    expires_at: now + 3600,
    refresh_token_expires_at: now + 259200,
    scopes: ["user:inference", "user:profile"],
    has_inference_scope: true,
    windows: {
      observed: true,
      status: "session-limit-reached",
      five_hour_utilization: "1.0",
      five_hour_reset: "1788393000",
      weekly_utilization: "0.41",
      weekly_reset: "1788433200",
      overage_status: "rejected",
      reset: "not yet observed",
    },
  };
  const live = render({
    claude_code: { available: false },
    mcc: liveAccount,
    accounts: [liveAccount],
    windows: liveAccount.windows,
  });
  // Two accounts, each with its own name, its own origin and its own windows.
  const twoAccounts = render({
    claude_code: { available: false },
    mcc: liveAccount,
    accounts: [
      liveAccount,
      {
        available: true,
        account_id: "uuid-two",
        name: "second@example.test",
        origin: "claude-code",
        origin_path: "C:/Users/someone/.claude/.credentials.json",
        write_back_effective: true,
        ordinal: 2,
        subscription_type: "pro",
        source: "mcc",
        expires_at: now + 7200,
        scopes: ["user:inference"],
        has_inference_scope: true,
        windows: { observed: false },
      },
    ],
    windows: liveAccount.windows,
  });
  const unobserved = render({
    claude_code: { available: false },
    mcc: { available: false },
    accounts: [
      {
        available: true,
        account_id: "uuid-one",
        name: "",
        masked_token: "sk-a…z9",
        origin: "mcc",
        ordinal: 1,
        subscription_type: "max",
        source: "mcc",
        expires_at: now - 3600,
        scopes: ["user:profile"],
        has_inference_scope: false,
        windows: { observed: false },
      },
    ],
    windows: { observed: false },
  });
  const absent = render({
    claude_code: { available: false },
    mcc: { available: false },
    accounts: [],
    windows: { observed: false },
  });
  return { live, twoAccounts, unobserved, absent };
})();

/* "Sign in" becomes "Sign in another account" the moment anything is stored.
   That is the whole difference between "this will replace what I have" and
   "this will give me a second account", so it is asserted from the real
   refresher against the real payload rather than from the renderer alone. */
const anthropicOAuthSignInLabel = await (async () => {
  const read = async (accounts) => {
    ROUTES["/admin/api/anthropic-oauth/sources"] = {
      claude_code: { available: false },
      mcc: accounts[0] || { available: false },
      accounts,
      windows: { observed: false },
    };
    const importButton = doc.createElement("button");
    const loginButton = doc.createElement("button");
    loginButton.textContent = "Sign in with Anthropic";
    const status = doc.createElement("div");
    const details = doc.createElement("div");
    const buttons = [
      importButton,
      loginButton,
      doc.createElement("button"),
      doc.createElement("button"),
    ];
    buttons.status = status;
    buttons.details = details;
    await window.eval("refreshAnthropicOAuthSources")(
      importButton,
      status,
      details,
      buttons,
    );
    return {
      label: loginButton.textContent,
      status: (status.textContent || "").replace(/\s+/g, " ").trim(),
      managedEnabled: !buttons[2].disabled,
    };
  };
  const empty = await read([]);
  const stored = await read([
    {
      available: true,
      account_id: "uuid-one",
      name: "first@example.test",
      subscription_type: "max",
      source: "mcc",
      scopes: [],
      windows: { observed: false },
    },
  ]);
  delete ROUTES["/admin/api/anthropic-oauth/sources"];
  return { empty, stored };
})();

/* --------------------------------------------------- the vision adapter mode
   Two things the mode has to do on this page: sit inside the adapter's own
   card rather than in the leftovers grid, and change what the hop under each
   blind tier claims -- in describe mode the arrow no longer means "your
   request goes here". */
const visionMode = {};
{
  const routingLinkAgain = navLinks.find(
    (link) => link.dataset.view === "model_config",
  );
  if (routingLinkAgain) {
    routingLinkAgain.click();
    await settle();
    const card = doc.querySelector(".route-vision");
    const control = card ? card.querySelector(".route-vision-mode select") : null;
    visionMode.insideTheCard = Boolean(control);
    visionMode.inTheLeftovers = Boolean(
      doc.querySelector(".route-layout > .field-grid [data-key='VISION_ADAPTER_MODE']"),
    );
    visionMode.options = control
      ? Array.from(control.options).map((option) => option.value)
      : [];
    visionMode.value = control ? control.value : null;
    // The card renders the mode under the rail and above the "currently
    // covers" summary; reading the order proves the placement rather than
    // only the presence.
    visionMode.order = card
      ? Array.from(card.children).map((node) => node.className.split(" ")[0])
      : [];
    const hopText = () =>
      Array.from(doc.querySelectorAll(".route-vision-hop"))
        .map((node) => (node.textContent || "").replace(/\s+/g, " ").trim())
        .join(" | ");
    visionMode.hopsInRoute = hopText();
    visionMode.summaryInRoute = (
      doc.querySelector(".route-vision-summary")?.textContent || ""
    )
      .replace(/\s+/g, " ")
      .trim();
    if (control) {
      control.value = "describe";
      control.dispatchEvent(new window.Event("change", { bubbles: true }));
      await settle();
    }
    visionMode.hopsInDescribe = hopText();
    // No tier in this fixture resolves to a model the catalogue calls blind,
    // so the hop itself is drawn directly: the sentence it writes is the
    // thing under test, not which tier happens to trigger it.
    const hopSentence = (mode) => {
      const node = window.eval(
        `buildVisionHop("p1/blind", "groq/eyes", ${JSON.stringify(mode)})`,
      );
      return (node.textContent || "").replace(/\s+/g, " ").trim();
    };
    visionMode.hopSentenceRoute = hopSentence("route");
    visionMode.hopSentenceDescribe = hopSentence("describe");
    visionMode.hopSentenceUnset = (
      window.eval('buildVisionHop("p1/blind", "", "describe")').textContent || ""
    )
      .replace(/\s+/g, " ")
      .trim();
    visionMode.summaryInDescribe = (
      doc.querySelector(".route-vision-summary")?.textContent || ""
    )
      .replace(/\s+/g, " ")
      .trim();
    if (control) {
      control.value = control.dataset.original;
      control.dispatchEvent(new window.Event("change", { bubbles: true }));
      await settle();
    }
  }
}

/* -------------------------------------------- the description in the modal
   Section 5A / Q15: the only way to judge whether describe mode is worth
   using is to read what the coding model was actually handed. */
const describedImages = {};
{
  const driveImages = (row) => {
    window.eval(`renderRequestImages(${JSON.stringify(row)});`);
    const container = doc.getElementById("reqDetailImages");
    return {
      delivery: (container.querySelector(".req-image-delivery")?.textContent || "")
        .replace(/\s+/g, " ")
        .trim(),
      summaries: Array.from(
        container.querySelectorAll(".req-image-description summary"),
      ).map((node) => node.textContent),
      bodies: Array.from(
        container.querySelectorAll(".req-image-description-text"),
      ).map((node) => node.textContent),
    };
  };
  const image = {
    sha256: "sha_one",
    kind: "image",
    media_type: "image/png",
    source_bytes: 213_000,
    width: 1200,
    height: 800,
    thumbnail_media_type: "image/webp",
    thumbnail_base64: null,
    description: "A terminal showing ModuleNotFoundError.",
    described_by: "groq/eyes",
    described_at: 1_757_000_000,
  };
  describedImages.fresh = driveImages({
    input_image_count: 1,
    image_delivery: "described",
    input_images: [image],
    route_attempts: [
      { attempt: 0, model_ref: "p/blind", outcome: "succeeded", params: null },
      {
        attempt: 1000,
        model_ref: "groq/eyes",
        outcome: "succeeded",
        params: { kind: "describe", image_sha: "sha_one", cached: false },
      },
    ],
  });
  describedImages.cached = driveImages({
    input_image_count: 1,
    image_delivery: "described",
    input_images: [image],
    route_attempts: [
      { attempt: 0, model_ref: "p/blind", outcome: "succeeded", params: null },
    ],
  });
  describedImages.plain = driveImages({
    input_image_count: 1,
    image_delivery: "image",
    input_images: [{ ...image, description: null, described_by: null }],
    route_attempts: [],
  });
}

/* ------------------------------------------------- the export window's scopes
   "Export attempts" is a second button onto the same modal, and the only thing
   that makes it a different export is the scope it selects and the field list
   that scope renders. Both are one line of wiring, and both fail silently:
   a button that opens the modal on the request scope looks exactly like one
   that works. Driven here rather than asserted structurally for that reason. */
const exportWindow = (() => {
  const read = (buttonId) => {
    const button = doc.getElementById(buttonId);
    if (!button) return null;
    button.dispatchEvent(new window.Event("click", { bubbles: true }));
    const checked = doc.querySelector('input[name="exportScope"]:checked');
    const groupWrap = doc.getElementById("exportGroupByWrap");
    return {
      scope: checked ? checked.value : null,
      groupByHidden: groupWrap ? groupWrap.hidden : null,
      fields: Array.from(
        doc.querySelectorAll("#exportFieldList .export-field span"),
      ).map((span) => (span.textContent || "").trim()),
      checkedFields: Array.from(
        doc.querySelectorAll("#exportFieldList input:checked"),
      ).map((input) => input.value),
    };
  };
  return {
    scopes: Array.from(doc.querySelectorAll('input[name="exportScope"]')).map(
      (radio) => radio.value,
    ),
    attempts: read("reqExportAttemptsButton"),
    requests: read("reqExportButton"),
  };
})();

/* The segmented theme control. jsdom has no box model, so this cannot prove
   the fourth option stopped overflowing -- only that every option is a child
   of the pill, which is the structural half of the same guarantee and the
   half a future fifth theme could quietly break. */
const themeSwitch = doc.getElementById("themeSwitch");
const themeOptions = Array.from(doc.querySelectorAll(".theme-option"));
const themePicker = {
  present: Boolean(themeSwitch),
  optionCount: themeOptions.length,
  labels: themeOptions.map((option) => (option.textContent || "").trim()),
  allInsidePill: themeOptions.every(
    (option) => option.parentElement === themeSwitch,
  ),
  checked: themeOptions
    .filter((option) => option.getAttribute("aria-checked") === "true")
    .map((option) => option.dataset.themeValue),
};

/* ---------------------------------------------------------- guide links
   Every surface that has a Guide section now carries a small link into it.
   Two things can be wrong and neither shows up as an error: the anchor can
   name a heading that no longer exists, and the click can switch the view
   without scrolling anywhere. Both are checked here against the real DOM. */
const guideLinks = (() => {
  const rendered = Array.from(doc.querySelectorAll(".guide-link")).map((link) => ({
    purpose: link.dataset.guidePurpose,
    anchor: link.dataset.guideAnchor,
    resolves: Boolean(doc.getElementById(link.dataset.guideAnchor)),
    view: link.closest(".admin-view")
      ? link.closest(".admin-view").dataset.view
      : null,
  }));

  // Click one and watch what it does: the Guide view has to become the active
  // one, and the anchor has to be scrolled to.
  const sample = doc.querySelector('.guide-link[data-guide-purpose="cli"]');
  let clicked = null;
  if (sample) {
    const before = scrolledTo.length;
    sample.click();
    const guideView = doc.getElementById("view-guide");
    clicked = {
      anchor: sample.dataset.guideAnchor,
      guideViewVisible: Boolean(guideView) && guideView.hidden === false,
      navActive: Boolean(
        doc.querySelector('button.nav-link[data-view="guide"].active'),
      ),
      scrolledAfter: scrolledTo.slice(before),
    };
  }
  return { rendered, clicked };
})();
// The rAF the scroll runs in has to be allowed to fire before it is reported.
await new Promise((resolve) => setTimeout(resolve, 50));
if (guideLinks.clicked) {
  guideLinks.clicked.scrolledAnchors = scrolledTo.slice();
}

// The desktop app's own half of the version panel (6.60.0). The wheel and the
// window update by different mechanisms and at different moments, and until
// 6.60.0 the banner only ever reported one of them -- so a user could sit
// fifteen releases behind on the window and read "Already up to date".
//
// Driven through `loadVersionInfo()` and the stubbed route rather than by
// assigning `state` directly: `window.eval(script)` is an indirect eval, so
// admin.js's top-level `const state` lives in that eval's own scope and is not
// reachable from a later one. Going through the fetch the page really makes is
// closer to the truth anyway.
const desktopAppBanner = {};
for (const [label, info] of [
  [
    "stale",
    {
      current: "6.60.0",
      shell_installed_tag: "v6.43.0",
      shell_pinned_tag: "v6.60.0",
      shell_update_available: true,
    },
  ],
  [
    "current",
    {
      current: "6.60.0",
      shell_installed_tag: "v6.60.0",
      shell_pinned_tag: "v6.60.0",
      shell_update_available: false,
    },
  ],
]) {
  ROUTES["/admin/api/version"] = info;
  await window.eval("loadVersionInfo()");
  desktopAppBanner[label] = {
    banners: (doc.getElementById("versionBanners")?.textContent || "")
      .replace(/\s+/g, " ")
      .trim(),
    panel: (doc.getElementById("versionDetails")?.textContent || "")
      .replace(/\s+/g, " ")
      .trim(),
  };
}

// The catalogue readout has three sentences to tell apart: a sweep this
// process ran, a catalogue this process only read from disk (which must not
// claim a network call it never made), and the sweep being off entirely.
const catalogueReadout = {};
{
  const seconds = Date.now() / 1000;
  for (const [label, status] of [
    [
      "swept",
      {
        enabled: true,
        last_refreshed_at: seconds - 720,
        next_refresh_at: seconds + 2880,
        refreshing: false,
        from_stored_catalogue: false,
      },
    ],
    [
      "stored",
      {
        enabled: true,
        last_refreshed_at: seconds - 2400,
        next_refresh_at: seconds + 1200,
        refreshing: true,
        from_stored_catalogue: true,
      },
    ],
    ["off", { enabled: false }],
  ]) {
    // Through the real loader and the real route, because the readout is
    // rendered from whatever that payload said -- reaching into the page's
    // own state would be testing a different function.
    MODEL_ADMIN_PAGE.catalogue_refresh = status;
    await window.eval("loadModelsView(true)");
    await new Promise((resolve) => setTimeout(resolve, 200));
    catalogueReadout[label] = (
      doc.getElementById("modelsRefreshReadout").textContent || ""
    )
      .replace(/\s+/g, " ")
      .trim();
  }
}

/* ------------------------------------------------------- latency views
   The four surfaces the per-model latency work added, each driven through the
   real renderer: the request row's winner TTFT and its fallback-loss suffix,
   the modal's three TTFT rows and per-attempt timeline, the Analytics
   breakdown in both its measured and never-measured states, and the Models
   page chip that must be absent rather than zeroed. */
const latencyViews = {};
{
  const body = doc.getElementById("reqTableBody");
  const baseRow = {
    ts_iso: "2026-09-13T10:00:00Z",
    endpoint: "/v1/messages",
    provider: "commandcode",
    key_label: "CC_API_KEY",
    resolved_model: "commandcode/kimi-k3",
    status: "success",
    tokens_in: 120,
    tokens_out: 340,
    duration_ms: 2100,
  };
  window.eval(
    `renderRequestsTable(${JSON.stringify([
      // The regression this feature exists for: the winner answered in 300 ms
      // and the row must say so, not 4.7 s.
      { ...baseRow, id: "r-fallback", ttft_ms: 4712, ttft_winner_ms: 300 },
      // A frame apart is not a fallback loss.
      { ...baseRow, id: "r-frame", ttft_ms: 306, ttft_winner_ms: 305 },
      // Written before 7.4.0: no winner time exists, so the row shows exactly
      // what it always showed.
      { ...baseRow, id: "r-legacy", ttft_ms: 410, ttft_winner_ms: null },
      { ...baseRow, id: "r-none", ttft_ms: null, ttft_winner_ms: null },
    ])})`,
  );
  latencyViews.rowCells = Array.from(body.querySelectorAll(".req-ttft-cell")).map(
    (td) => ({
      text: td.textContent,
      title: td.title,
      lost: Array.from(td.querySelectorAll(".req-ttft-lost")).map(
        (el) => el.textContent,
      ),
    }),
  );

  // --- the modal, opened on the fallback request through the real loader
  await window.eval('openRequestDetail("req-fallback")');
  await settle();
  const meta = Array.from(doc.getElementById("reqDetailMeta").children);
  latencyViews.detailPairs = meta
    .map((el, index) =>
      el.tagName === "DT" ? [el.textContent, (meta[index + 1] || {}).textContent] : null,
    )
    .filter(Boolean);
  const chain = doc.getElementById("reqDetailChain");
  latencyViews.chainHidden = chain.hidden;
  latencyViews.attemptMetrics = Array.from(
    chain.querySelectorAll(".req-chain-metrics"),
  ).map((row) =>
    Array.from(row.querySelectorAll(".req-chain-metric")).map((cell) => [
      cell.querySelector(".req-chain-metric-label").textContent,
      cell.querySelector(".req-chain-metric-value").textContent,
    ]),
  );
  latencyViews.chainModels = Array.from(
    chain.querySelectorAll(".req-chain-model"),
  ).map((el) => el.textContent);
  latencyViews.benchReasons = Array.from(
    chain.querySelectorAll(".req-chain-bench"),
  ).map((el) => el.textContent);
  window.eval("closeRequestDetail()");

  // --- the Analytics breakdown, measured and not
  const readPanel = () => ({
    headers: Array.from(
      doc.querySelectorAll("#reqModelLatency thead th"),
    ).map((th) => th.textContent),
    rows: Array.from(doc.querySelectorAll("#reqModelLatency tbody tr")).map((tr) =>
      Array.from(tr.children).map((td) => td.textContent),
    ),
    p50Titles: Array.from(
      doc.querySelectorAll("#reqModelLatency .req-latency-p50"),
    ).map((el) => el.title),
    note: doc.getElementById("reqModelLatencyNote").textContent,
  });
  window.eval(
    `renderRequestModelLatency(${JSON.stringify(ROUTES["/admin/api/requests/latency"])})`,
  );
  latencyViews.panel = readPanel();
  window.eval(
    `renderRequestModelLatency(${JSON.stringify({
      enabled: true,
      models: 1,
      attempts: 4416,
      ttft_measured: 0,
      p50_source: "exact",
      stale: false,
      rows: [
        {
          model_ref: "beta/model-07",
          outcome: "failed",
          attempts: 4416,
          ttft_measured: 0,
          avg_ttft_ms: null,
          avg_first_reasoning_ms: null,
          avg_generating_ms: null,
          tokens_out: null,
          p50_ttft_ms: null,
          p95_ttft_ms: null,
          p50_source: "exact",
        },
      ],
    })})`,
  );
  latencyViews.notMeasured = readPanel();
  window.eval("renderRequestModelLatency(null)");
  latencyViews.disabled = readPanel();

  // --- the Models page chip: present for the measured model, absent for the
  // one whose 4,416 attempts carry no measurement.
  await window.eval("loadModelsView(true)");
  await new Promise((resolve) => setTimeout(resolve, 200));
  const modelsTree = doc.getElementById("modelsTree");
  latencyViews.modelChips = Array.from(
    modelsTree.querySelectorAll(".models-chip-latency"),
  ).map((chip) => ({ text: chip.textContent, title: chip.title }));
  latencyViews.latencyFetches = fetchCalls.filter(
    (path) => path === "/admin/api/requests/latency",
  ).length;
}

/* ------------------------------------------------ cancelled sub-labels
   "Cancelled" covered four unrelated events and said none of them. Each
   surface is driven through the real renderer: the chip on a list row, the
   modal's sentence, the Analytics breakdown, and the Model latency panel's
   third group -- which exists so a client hang-up stops being counted as a
   failure of whichever model happened to be streaming. */
const cancelledViews = {};
{
  const body = doc.getElementById("reqTableBody");
  const baseRow = {
    ts_iso: "2026-09-20T05:22:00Z",
    endpoint: "/v1/messages",
    provider: "custom_agnes",
    key_label: "AGNES_API_KEY",
    resolved_model: "custom_agnes/agnes-3.0-flash",
    tokens_in: 120,
    tokens_out: null,
  };
  window.eval(
    `renderRequestsTable(${JSON.stringify([
      {
        ...baseRow,
        id: "r-gave-up",
        status: "cancelled",
        cancel_reason: "client_gave_up_waiting",
        ttft_ms: null,
        duration_ms: 600000,
      },
      {
        ...baseRow,
        id: "r-silent",
        status: "cancelled",
        cancel_reason: "committed_then_silent",
        ttft_ms: 16700,
        duration_ms: 313400,
      },
      {
        ...baseRow,
        id: "r-mid",
        status: "cancelled",
        cancel_reason: "stopped_mid_answer",
        ttft_ms: 200,
        duration_ms: 9000,
        output_chars: 140,
      },
      {
        ...baseRow,
        id: "r-restart",
        status: "cancelled",
        cancel_reason: "server_restart",
        ttft_ms: 200,
        duration_ms: 310000,
      },
      // Not cancelled: it must carry no chip at all, not an empty one.
      { ...baseRow, id: "r-fine", status: "success", ttft_ms: 200, duration_ms: 900 },
    ])})`,
  );
  cancelledViews.rows = Array.from(body.querySelectorAll("tr")).map((tr) => {
    const chip = tr.querySelector(".cancel-reason-chip");
    return {
      status: (tr.querySelector(".req-status-text") || {}).textContent,
      chip: chip ? chip.textContent : null,
      reason: chip ? chip.dataset.reason : null,
      title: chip ? chip.title : null,
    };
  });

  // --- the modal, through the real loader
  await window.eval('openRequestDetail("req-cancelled")');
  await settle();
  const meta = Array.from(doc.getElementById("reqDetailMeta").children);
  cancelledViews.detailPairs = meta
    .map((el, index) =>
      el.tagName === "DT" ? [el.textContent, (meta[index + 1] || {}).textContent] : null,
    )
    .filter(Boolean);
  cancelledViews.modalChip = (
    doc.querySelector("#reqDetailMeta .cancel-reason-chip") || {}
  ).textContent;
  cancelledViews.modalSentence = (
    doc.querySelector("#reqDetailMeta .req-cancel-reason-note") || {}
  ).textContent;
  // The interrupted attempt in the chain is badged as the client hanging up,
  // not as a failure of the model that was streaming.
  cancelledViews.chainOutcomes = Array.from(
    doc.querySelectorAll("#reqDetailChain .req-chain-outcome"),
  ).map((el) => el.textContent);
  cancelledViews.chainClasses = Array.from(
    doc.querySelectorAll("#reqDetailChain .req-chain-item"),
  ).map((el) => el.className);
  window.eval("closeRequestDetail()");

  // --- the Analytics breakdown
  const readBreakdown = () => ({
    headers: Array.from(
      doc.querySelectorAll("#reqCancelledBreakdown thead th"),
    ).map((th) => th.textContent),
    rows: Array.from(doc.querySelectorAll("#reqCancelledBreakdown tbody tr")).map(
      (tr) => Array.from(tr.children).map((td) => td.textContent),
    ),
  });
  window.eval(
    `renderRequestCancelledBreakdown(${JSON.stringify({
      total: 935,
      selected: null,
      counts: {
        client_gave_up_waiting: 189,
        committed_then_silent: 78,
        stopped_mid_answer: 608,
        server_restart: 60,
      },
    })})`,
  );
  cancelledViews.breakdown = readBreakdown();
  window.eval(
    `renderRequestCancelledBreakdown(${JSON.stringify({
      total: 0,
      selected: null,
      counts: {
        client_gave_up_waiting: 0,
        committed_then_silent: 0,
        stopped_mid_answer: 0,
        server_restart: 0,
      },
    })})`,
  );
  cancelledViews.emptyBreakdown = readBreakdown();
  window.eval("renderRequestCancelledBreakdown(null)");
  cancelledViews.disabledBreakdown = readBreakdown();

  // --- the third latency group, beside the other two on the same model
  window.eval(
    `renderRequestModelLatency(${JSON.stringify({
      enabled: true,
      models: 1,
      attempts: 15,
      ttft_measured: 15,
      p50_source: "exact",
      stale: false,
      rows: [
        {
          model_ref: "custom_agnes/agnes-3.0-flash",
          outcome: "succeeded",
          attempts: 10,
          ttft_measured: 10,
          avg_ttft_ms: 340.0,
          avg_first_reasoning_ms: null,
          avg_generating_ms: 1600.0,
          tokens_out: 4080,
          p50_ttft_ms: 312.0,
          p95_ttft_ms: 1400.0,
          p50_source: "exact",
        },
        {
          model_ref: "custom_agnes/agnes-3.0-flash",
          outcome: "failed",
          attempts: 3,
          ttft_measured: 3,
          avg_ttft_ms: 9100.0,
          avg_first_reasoning_ms: null,
          avg_generating_ms: null,
          tokens_out: null,
          p50_ttft_ms: 9000.0,
          p95_ttft_ms: 12000.0,
          p50_source: "exact",
        },
        {
          model_ref: "custom_agnes/agnes-3.0-flash",
          outcome: "interrupted",
          attempts: 2,
          ttft_measured: 2,
          avg_ttft_ms: 600000.0,
          avg_first_reasoning_ms: null,
          avg_generating_ms: null,
          tokens_out: null,
          p50_ttft_ms: 600000.0,
          p95_ttft_ms: 604000.0,
          p50_source: "exact",
        },
      ],
    })})`,
  );
  cancelledViews.latencyPanel = Array.from(
    doc.querySelectorAll("#reqModelLatency tbody tr"),
  ).map((tr) => Array.from(tr.children).map((td) => td.textContent));

  // --- the one-line note under the Cancelled card
  window.eval(
    `renderRequestStatsCards(${JSON.stringify({
      enabled: true,
      total: 100,
      success: 90,
      error: 1,
      cancelled: 9,
      error_rate: 0.01,
      cancelled_breakdown: {
        total: 9,
        selected: null,
        counts: {
          client_gave_up_waiting: 4,
          committed_then_silent: 1,
          stopped_mid_answer: 3,
          server_restart: 1,
        },
      },
    })})`,
  );
  cancelledViews.cancelledCard = Array.from(
    doc.querySelectorAll("#reqStatsCards .requests-card"),
  )
    .map((card) => ({
      label: (card.querySelector("span") || {}).textContent,
      value: (card.querySelector("strong") || {}).textContent,
      note: (card.querySelector("small") || {}).textContent,
    }))
    .find((card) => card.label === "Cancelled");
}

/* --------------------------------------------------- advanced fields
   7.29.1: nothing is hidden by default. `advanced` orders a field after the
   common ones in its card and tags it; a collapse control stays for readers
   who want the short form, starts expanded, and is remembered per browser.

   Deliberately LAST in this file: it re-renders the sections to prove the
   stored collapse survives a reload, and every other readout above must be
   taken from the first paint. */
const advancedFields = (() => {
  const SECTION_STORAGE = "mcc.advancedCollapsed.section:credential_health";
  const keysIn = (el) =>
    Array.from(el.querySelectorAll(".field")).map((f) => f.dataset.key);
  // What a reader can actually see: a field is out of sight only when it is
  // advanced AND an ancestor card is collapsed.
  const shownIn = (el) =>
    Array.from(el.querySelectorAll(".field"))
      .filter(
        (f) =>
          !(f.classList.contains("advanced-field") && f.closest(".collapse-advanced")),
      )
      .map((f) => f.dataset.key);
  const read = (name) => {
    try {
      return window.localStorage.getItem(name);
    } catch {
      return "<unreadable>";
    }
  };

  const out = {
    showAdvancedClassAnywhere: doc.querySelectorAll(".show-advanced").length,
    showAdvancedInScript: /show-advanced/.test(script),
  };

  const card = doc.getElementById("section-credential_health");
  out.section = card
    ? {
        all: keysIn(card),
        shownOnLoad: shownIn(card),
        tagged: Array.from(card.querySelectorAll(".field"))
          .filter((f) => f.querySelector(".advanced-tag"))
          .map((f) => f.dataset.key),
        collapsedOnLoad: card.classList.contains("collapse-advanced"),
        storedOnLoad: read(SECTION_STORAGE),
        toggleLabel: (card.querySelector(".advanced-toggle")?.textContent || "").trim(),
      }
    : null;

  const toggle = card ? card.querySelector(".advanced-toggle") : null;
  if (toggle) {
    toggle.click();
    out.afterCollapse = {
      collapsed: card.classList.contains("collapse-advanced"),
      shown: shownIn(card),
      label: toggle.textContent.trim(),
      stored: read(SECTION_STORAGE),
    };
    // A reload is a fresh render against the same stored choice.
    window.eval("renderSections")(SECTIONS, FIELDS);
    const again = doc.getElementById("section-credential_health");
    const againToggle = again ? again.querySelector(".advanced-toggle") : null;
    out.afterReload = again
      ? {
          collapsed: again.classList.contains("collapse-advanced"),
          shown: shownIn(again),
          all: keysIn(again),
          label: (againToggle?.textContent || "").trim(),
        }
      : null;
    if (againToggle) {
      againToggle.click();
      out.afterExpand = {
        collapsed: again.classList.contains("collapse-advanced"),
        shown: shownIn(again),
        label: againToggle.textContent.trim(),
        stored: read(SECTION_STORAGE),
      };
    }
  }

  /* One provider card, rendered straight from renderProviderGroups the way
     the key manager above is driven, because this fixture's /admin/api/config
     carries no provider fields. Until 7.29.1 the proxy field below rendered
     `display: none` with no control anywhere that could reveal it. */
  const providerFields = [
    {
      key: "HARNESS_API_KEY",
      label: "Harness API Key",
      section: "providers",
      provider: "harness_provider",
      type: "secret",
      value: "",
      default: "",
      secret: true,
      description: "API key for the fixture provider.",
    },
    {
      key: "HARNESS_PROXY",
      label: "Harness Proxy",
      section: "providers",
      provider: "harness_provider",
      type: "secret",
      value: "",
      default: "",
      secret: true,
      advanced: true,
      description: "Send fixture traffic through this proxy.",
    },
    {
      key: "HARNESS_BASE_URL",
      label: "Harness Base URL",
      section: "providers",
      provider: "harness_provider",
      type: "text",
      value: "",
      default: "https://example.invalid/v1",
      description: "Endpoint the fixture provider is reached on.",
    },
  ];
  const host = doc.createElement("div");
  doc.body.appendChild(host);
  host.appendChild(window.eval("renderProviderGroups")(providerFields));
  const pv = host.querySelector(".pv-card");
  out.provider = pv
    ? {
        order: keysIn(pv),
        shownOnLoad: shownIn(pv),
        tagged: Array.from(pv.querySelectorAll(".field"))
          .filter((f) => f.querySelector(".advanced-tag"))
          .map((f) => f.dataset.key),
        collapsedOnLoad: pv.classList.contains("collapse-advanced"),
        toggleLabel: (pv.querySelector(".advanced-toggle")?.textContent || "").trim(),
      }
    : null;
  const pvToggle = pv ? pv.querySelector(".advanced-toggle") : null;
  if (pvToggle) {
    pvToggle.click();
    out.providerAfterCollapse = {
      collapsed: pv.classList.contains("collapse-advanced"),
      shown: shownIn(pv),
      stored: read("mcc.advancedCollapsed.provider:harness_provider"),
    };
  }
  host.remove();
  return out;
})();

/* ------------------------------------------------------- the shared rail
   7.29.0 gave every key row a grip, a name box and two Move buttons, but the
   three pools each built their own row and add-form markup, and only the
   env-key one had a stylesheet rule that wrapped. The result was ~280px of
   controls hanging off the right of a 240px custom-provider card.

   What is asserted here is that there is exactly one builder: the class the
   stylesheet addresses is the same on all three pools, and the add form has
   the same three controls in the same order everywhere. The pre-7.34.1
   `.cp-*` / `.ws-*` classes ride along as aliases and are asserted too, so a
   selector written against them cannot be broken silently. */
const sharedRail = {};
{
  const describe = (root) => {
    const list = root.querySelector(".key-manager-list");
    const row = root.querySelector(".key-manager-row");
    const form = root.querySelector(".key-manager-add");
    const classesOf = (node) =>
      node ? Array.from(node.classList).sort() : null;
    return {
      list: classesOf(list),
      row: classesOf(row),
      label: classesOf(row && row.querySelector(".key-manager-key")),
      labelTag: row && row.querySelector(".key-manager-key")
        ? row.querySelector(".key-manager-key").tagName
        : null,
      addClasses: classesOf(form),
      // The add form's shape, in order: the secret, the optional name, the
      // button. Read off the DOM rather than asserted per pool, because the
      // point is that the three are the same list.
      addShape: form
        ? Array.from(form.children).map((node) =>
            [
              node.tagName,
              node.getAttribute("type") || "",
              node.className,
            ].join("|"),
          )
        : null,
      addNamePlaceholder: form
        ? form.querySelector(".key-add-name")?.placeholder || ""
        : null,
      // A row must carry the reorder controls on every pool.
      grip: Boolean(row && row.querySelector(".key-drag-grip")),
      nameBox: Boolean(row && row.querySelector(".key-name-input")),
      moves: row
        ? Array.from(row.querySelectorAll(".key-manager-move")).map(
            (button) => button.textContent,
          )
        : null,
    };
  };

  const envPanel = doc.createElement("div");
  doc.body.appendChild(envPanel);
  await window.eval(`renderKeyManager`)(envPanel, { key: "NAMED_API_KEY" });
  sharedRail.env = describe(envPanel);

  const wsPanel = doc.createElement("div");
  wsPanel.className = "ws-key-manager";
  doc.body.appendChild(wsPanel);
  await window.eval(`loadKeyManager`)({ id: "tavily", envKey: "TAVILY_API_KEY" }, wsPanel);
  sharedRail.websearch = describe(wsPanel);

  // The rail arrives on a custom card a moment after the card does: the card
  // is drawn from the providers payload and the per-key listing enriches it,
  // fire-and-forget. Blocks above this one leave renders in flight, and one
  // landing mid-read would replace the enriched card with a bare one -- so
  // the grid is redrawn here first, settled, and only then enriched.
  await window.eval(`loadCustomProviders`)();
  await new Promise((resolve) => setTimeout(resolve, 250));
  const customCard = doc.querySelector('[data-custom-provider="custom_acme"]');
  sharedRail.custom = customCard ? describe(customCard) : null;
  // The card says it is showing a rail, which is what widens it to the whole
  // grid row -- the rule `.pv-card.pv-open` has always had.
  sharedRail.customIsRailHost = customCard
    ? customCard.classList.contains("has-key-rail")
    : null;
  // The health badge sits between the key and the Move buttons, the way it
  // does on an env-key row. It used to land after Move down.
  sharedRail.customRowOrder = customCard
    ? Array.from(customCard.querySelector(".key-manager-row")?.children || []).map(
        (node) => node.className.split(" ")[0],
      )
    : null;
  sharedRail.envRowOrder = Array.from(
    envPanel.querySelector(".key-manager-row")?.children || [],
  ).map((node) => node.className.split(" ")[0]);
}

/* One `field.note`, once. It used to be appended twice -- once beside the
   badge it explains and once again at the end of the cell -- so every row
   carrying a note read its sentence out two times. */
const capabilityRow = {};
{
  const row = window.eval(`buildCapabilityRow`)(
    "Wire surface",
    {
      value: "responses",
      source: "probe",
      note: "the npm package selected this door",
      approximate: true,
      agreement: 0.5,
      match_count: 2,
      reporters: 3,
      tier: 4,
      tier_label: "id matched exactly",
    },
    { probe: "Probed" },
    null,
  );
  capabilityRow.notes = Array.from(row.querySelectorAll(".models-approx-note")).map(
    (node) => node.textContent,
  );
  capabilityRow.noteCount = capabilityRow.notes.filter(
    (text) => text === "the npm package selected this door",
  ).length;
}

/* The tools array a request carried (7.40.0): the modal shows the catalogue
   hash, the count, and the names behind an expander; choosing a name lists the
   requests that carried it; a request that predates the recording says so; and
   no tool definition ever reaches the page. Driven through the real
   openRequestDetail() loader. */
const toolCatalogue = {};
{
  const shaOf = (seed) => seed.repeat(64 / seed.length).slice(0, 64);
  const base = {
    ts_iso: "2026-09-20T13:02:02Z",
    endpoint: "/v1/messages",
    protocol: "anthropic_messages",
    provider: "chatgpt_oauth",
    requested_model: "claude-opus-4",
    resolved_model: "gpt-5.6-sol",
    status: "success",
    tokens_in: 120,
    tokens_out: 340,
    duration_ms: 2100,
  };
  ROUTES["/admin/api/requests/req-tools"] = {
    ...base,
    id: "req-tools",
    params: { tools_count: 3 },
    tool_catalogue_sha: shaOf("ab12"),
    tool_catalogue: {
      sha: shaOf("ab12"),
      tool_count: 3,
      tools: [
        { name: "Bash", sha: shaOf("0a") },
        { name: "mcp__appium-mcp__appium_screen_recording", sha: shaOf("0b") },
        { name: "Artifact", sha: shaOf("0c") },
      ],
      first_seen: 1789909322,
      last_seen: 1789925035,
      seen: 216,
    },
  };
  ROUTES["/admin/api/requests/req-tools-legacy"] = {
    ...base,
    id: "req-tools-legacy",
    params: { tools_count: 212 },
    tool_catalogue_sha: null,
    tool_catalogue: null,
  };
  ROUTES["/admin/api/requests/req-no-tools"] = {
    ...base,
    id: "req-no-tools",
    params: { tools_count: 0 },
    tool_catalogue_sha: null,
    tool_catalogue: null,
  };
  ROUTES["/admin/api/tool-catalogues/requests"] = {
    enabled: true,
    name: "mcp__appium-mcp__appium_screen_recording",
    definitions: 1,
    catalogues: 2,
    seen: 216,
    first_seen: 1789909322,
    last_seen: 1789925035,
    rows: [
      { id: "req-tools", ts_iso: "2026-09-20T17:23:55Z", requested_model: "claude-opus-4",
        resolved_model: "gpt-5.6-sol", provider: "chatgpt_oauth", status: "success",
        tool_catalogue_sha: shaOf("ab12") },
      { id: "req-older", ts_iso: "2026-09-20T13:02:02Z", requested_model: "claude-opus-4",
        resolved_model: null, provider: "chatgpt_oauth", status: "error",
        tool_catalogue_sha: shaOf("cd34") },
    ],
    has_more: true,
  };
  const pairs = () => {
    const nodes = Array.from(doc.getElementById("reqDetailMeta").children);
    return nodes
      .map((el, index) =>
        el.tagName === "DT" ? [el.textContent, (nodes[index + 1] || {}).textContent] : null,
      )
      .filter(Boolean);
  };
  const catalogueCell = () => {
    const nodes = Array.from(doc.getElementById("reqDetailMeta").children);
    const index = nodes.findIndex(
      (el) => el.tagName === "DT" && el.textContent === "Tool catalogue",
    );
    return index === -1 ? null : nodes[index + 1];
  };

  await window.eval('openRequestDetail("req-tools")');
  await settle();
  const cell = catalogueCell();
  toolCatalogue.present = Boolean(cell);
  toolCatalogue.sha = cell ? textOf(cell, ".req-tool-catalogue-sha") : null;
  toolCatalogue.shaTitle = cell
    ? cell.querySelector(".req-tool-catalogue-sha").getAttribute("title")
    : null;
  toolCatalogue.count = cell ? textOf(cell, ".req-tool-catalogue-count") : null;
  const details = cell ? cell.querySelector("details.req-tool-catalogue-tools") : null;
  toolCatalogue.collapsedByDefault = details ? !details.open : null;
  toolCatalogue.summary = details ? details.querySelector("summary").textContent : null;
  toolCatalogue.names = cell
    ? Array.from(cell.querySelectorAll(".req-tool-name")).map((el) => el.textContent)
    : [];
  toolCatalogue.nameTitles = cell
    ? Array.from(cell.querySelectorAll(".req-tool-name")).map((el) => el.title)
    : [];
  toolCatalogue.notes = cell
    ? Array.from(cell.querySelectorAll(".req-tool-catalogue-note")).map((el) => el.textContent)
    : [];
  toolCatalogue.carriersHiddenBeforeClick = cell
    ? cell.querySelector(".req-tool-carriers").hidden
    : null;
  const before = fetchUrls.length;
  cell
    .querySelectorAll(".req-tool-name")[1]
    .dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  toolCatalogue.carrierUrls = fetchUrls
    .slice(before)
    .filter((url) => url.startsWith("/admin/api/tool-catalogues/requests"));
  const carriers = cell.querySelector(".req-tool-carriers");
  toolCatalogue.carriersHidden = carriers.hidden;
  toolCatalogue.carriersHeading = textOf(carriers, ".req-tool-carriers-heading");
  toolCatalogue.carrierRows = Array.from(carriers.querySelectorAll(".req-tool-carrier")).map(
    (el) => ({ text: el.textContent, title: el.title }),
  );
  toolCatalogue.carrierNotes = Array.from(
    carriers.querySelectorAll(".req-tool-catalogue-note"),
  ).map((el) => el.textContent);
  // Choosing a listed request opens that request's own detail.
  const opened = fetchUrls.length;
  carriers
    .querySelectorAll(".req-tool-carrier")[0]
    .dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  toolCatalogue.carrierOpens = fetchUrls.slice(opened);
  toolCatalogue.modalText = (doc.getElementById("reqDetailModal").textContent || "")
    .replace(/\s+/g, " ")
    .trim();
  window.eval("closeRequestDetail()");

  await window.eval('openRequestDetail("req-tools-legacy")');
  await settle();
  const legacy = catalogueCell();
  toolCatalogue.legacyText = legacy ? legacy.textContent : null;
  toolCatalogue.legacyHasNames = legacy
    ? legacy.querySelectorAll(".req-tool-name").length
    : null;
  window.eval("closeRequestDetail()");

  await window.eval('openRequestDetail("req-no-tools")');
  await settle();
  toolCatalogue.noToolsRow = pairs().some(([label]) => label === "Tool catalogue");
  window.eval("closeRequestDetail()");
}

/* Where a request came from (7.42.0): the Session, Folder, Origin chip and
   Requested model cells, the modal's provenance lines, and the folder backfill
   card on the Request log storage section. Driven through the real
   renderRequestsTable(), openRequestDetail() and startOriginBackfill(). Every
   path is fake. */
const requestOrigin = {};
{
  const originRow = {
    id: "req-origin",
    harness: "claude",
    ts_iso: "2026-09-24T10:00:00Z",
    endpoint: "/v1/messages",
    protocol: "anthropic_messages",
    provider: "nous_portal",
    key_label: "NOUS_API_KEY",
    requested_model: "claude-opus-4",
    resolved_model: "hermes-4-405b",
    status: "error",
    tokens_in: 120,
    tokens_out: 340,
    ttft_ms: 410,
    duration_ms: 2100,
    session_id: "0f3c2a1b-6d5e-4f70-9a8b-1c2d3e4f5a6b",
    session_short: "0f3c2a1b",
    agent_id: "a7b8c9d0-1e2f-4a3b-8c4d-5e6f7a8b9c0d",
    parent_session_id: "0f3c2a1b-6d5e-4f70-9a8b-1c2d3e4f5a6b",
    project_dir: "C:\\Users\\devuser\\Projects\\demo",
    project_short: "Projects\\demo · #3f9a21",
    origin_source:
      "session_id=header.x-claude-code-session-id;agent_id=header.x-claude-code-agent-id;" +
      "parent_session_id=header.x-claude-code-agent-id;project_dir=prompt.env-block",
    origin_provenance: {
      session_id: { source: "header", signal: "x-claude-code-session-id",
        sentence: "stated by the x-claude-code-session-id header" },
      agent_id: { source: "header", signal: "x-claude-code-agent-id",
        sentence: "stated by the x-claude-code-agent-id header" },
      parent_session_id: { source: "header", signal: "x-claude-code-agent-id",
        sentence: "stated by the x-claude-code-agent-id header" },
      project_dir: { source: "prompt", signal: "env-block",
        sentence: "read from the prompt's environment block" },
    },
  };
  const bareRow = {
    ...originRow,
    id: "req-bare",
    status: "success",
    session_id: null,
    session_short: null,
    agent_id: null,
    parent_session_id: null,
    project_dir: null,
    project_short: null,
    origin_source: null,
    origin_provenance: {},
    requested_model: null,
  };
  ROUTES["/admin/api/requests/req-origin"] = originRow;
  ROUTES["/admin/api/requests/req-bare"] = bareRow;

  window.eval(`renderRequestsTable(${JSON.stringify([originRow, bareRow])})`);
  const body = doc.getElementById("reqTableBody");
  const rowsNow = Array.from(body.querySelectorAll("tr"));
  const headers = Array.from(doc.querySelectorAll(".requests-table thead th"));
  requestOrigin.headerClasses = headers.map((th) => [th.textContent.trim(), th.className]);
  requestOrigin.cellCounts = rowsNow.map((tr) => tr.children.length);
  const cellAt = (tr, cls) => tr.querySelector(`td.${cls}`);
  const describe = (tr) => ({
    session: cellAt(tr, "req-col-session").textContent,
    sessionTitle: (cellAt(tr, "req-col-session").querySelector("code") || {}).title || null,
    subagentBadge: Boolean(cellAt(tr, "req-col-session").querySelector(".req-subagent-badge")),
    folder: cellAt(tr, "req-col-folder").textContent,
    folderTitle: (cellAt(tr, "req-col-folder").querySelector(".req-folder") || {}).title || null,
    chip: cellAt(tr, "req-col-origin").textContent,
    chipTitle: (cellAt(tr, "req-col-origin").querySelector(".origin-chip") || {}).title || null,
    requested: cellAt(tr, "req-col-requested-model").textContent,
    statusClass: Boolean(cellAt(tr, "req-col-status")),
    statusText: cellAt(tr, "req-col-status").querySelector(".req-status-text").textContent,
    // Same order as the header, so a cell cannot drift under another label.
    cellClassesByHeader: Array.from(tr.children).map((td, index) => [
      headers[index] ? headers[index].textContent.trim() : null,
      td.className,
    ]),
  });
  requestOrigin.withOrigin = describe(rowsNow[0]);
  requestOrigin.bare = describe(rowsNow[1]);

  const pairs = () => {
    const nodes = Array.from(doc.getElementById("reqDetailMeta").children);
    return nodes
      .map((el, index) =>
        el.tagName === "DT" ? [el.textContent, (nodes[index + 1] || {}).textContent] : null,
      )
      .filter(Boolean);
  };
  await window.eval('openRequestDetail("req-origin")');
  await settle();
  requestOrigin.detail = pairs().filter(([label]) =>
    ["Harness", "Session", "Subagent", "Parent session", "Folder", "Protocol"].includes(label),
  );
  window.eval("closeRequestDetail()");
  await window.eval('openRequestDetail("req-bare")');
  await settle();
  requestOrigin.bareDetailLabels = pairs().map(([label]) => label);
  window.eval("closeRequestDetail()");

  // The backfill card: rendered inside the Request log storage section.
  const card = doc.getElementById("originBackfillCard");
  requestOrigin.cardPresent = Boolean(card);
  requestOrigin.cardInRequestLogSection = Boolean(
    card && card.closest("#section-request_log"),
  );
  requestOrigin.buttonText = card
    ? doc.getElementById("originBackfillButton").textContent
    : null;
  ROUTES["/admin/api/requests/origin-backfill"] = {
    enabled: true, running: true, scanned: 1500, filled: 1421,
    started_at: 1790000000, finished_at: null, error: null,
    completed_at: null, through_rowid: 1500, harnesses: ["claude"],
  };
  const before = fetchBodies.length;
  if (card) {
    doc.getElementById("originBackfillButton").dispatchEvent(
      new window.MouseEvent("click", { bubbles: true }),
    );
    await settle();
  }
  requestOrigin.posted = fetchBodies
    .slice(before)
    .filter((entry) => entry.path === "/admin/api/requests/origin-backfill")
    .map((entry) => entry.method);
  requestOrigin.runningText = card
    ? doc.getElementById("originBackfillStatus").textContent
    : null;
  requestOrigin.buttonDisabledWhileRunning = card
    ? doc.getElementById("originBackfillButton").disabled
    : null;
  window.eval(
    'paintOriginBackfill({ enabled: true, running: false, scanned: 1500, filled: 1421, finished_at: 1790000100, error: null })',
  );
  requestOrigin.doneText = card
    ? doc.getElementById("originBackfillStatus").textContent
    : null;
  window.eval(
    'paintOriginBackfill({ enabled: false, reason: "Folder capture is off (REQUEST_LOG_CAPTURE_FOLDER=false), so older rows are not given one either." })',
  );
  requestOrigin.offText = card
    ? doc.getElementById("originBackfillStatus").textContent
    : null;
  requestOrigin.buttonDisabledWhenOff = card
    ? doc.getElementById("originBackfillButton").disabled
    : null;
  // Let the page's own poll find the walk finished, so no timer outlives the run.
  ROUTES["/admin/api/requests/origin-backfill"] = {
    enabled: true, running: false, scanned: 1500, filled: 1421, error: null,
  };
}

// ------------------------------------------------- origin filters (7.43.0)
/* Session and Folder: the two boxes through every wiring site (debounce,
   offset 0, persistence, restore, Clear), the breakdown route riding along
   with the page's filters, and a breakdown row that filters the page. */
const originFilters = {};
{
  const since = (prefix) => fetchUrls.filter((url) => url.startsWith(prefix));
  const listCalls = () => since("/admin/api/requests?");
  const statsCalls = () => since("/admin/api/requests/stats");
  const originCalls = () => since("/admin/api/requests/origin?");
  const persisted = () =>
    JSON.parse(window.localStorage.getItem("mcc-dashboard-state") || "{}").reqFilters ||
    {};
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const click = (el) => el.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));

  click(doc.getElementById("reqClearFilters"));
  await wait(700);
  originFilters.folderRows = Array.from(
    doc.querySelectorAll("#reqFolderBreakdown tbody tr"),
  ).map((tr) => Array.from(tr.children).map((td) => td.textContent));
  originFilters.folderHeaders = Array.from(
    doc.querySelectorAll("#reqFolderBreakdown thead th"),
  ).map((th) => th.textContent);
  originFilters.sessionRows = Array.from(
    doc.querySelectorAll("#reqSessionBreakdown tbody tr"),
  ).map((tr) => Array.from(tr.children).map((td) => td.textContent));
  originFilters.sessionHeaders = Array.from(
    doc.querySelectorAll("#reqSessionBreakdown thead th"),
  ).map((th) => th.textContent);
  originFilters.sessionNote = doc.getElementById("reqSessionBreakdownNote").textContent;
  originFilters.folderNote = doc.getElementById("reqFolderBreakdownNote").textContent;
  originFilters.folderButtons = doc.querySelectorAll(
    "#reqFolderBreakdown button.origin-filter-button",
  ).length;
  originFilters.folderTitle =
    doc.querySelector("#reqFolderBreakdown .origin-filter-button")?.title || "";
  originFilters.folderOptions = Array.from(
    doc.getElementById("reqFolderOptions").querySelectorAll("option"),
  ).map((option) => [option.value, option.getAttribute("label")]);
  originFilters.sessionOptions = Array.from(
    doc.getElementById("reqSessionOptions").querySelectorAll("option"),
  ).map((option) => [option.value, option.getAttribute("label")]);
  originFilters.unfilteredOriginUrl = originCalls()[originCalls().length - 1] || "";

  for (const [name, id, typed] of [
    ["session", "reqFilterSession", ["0f", "0f3c", "0f3c2a1b"]],
    ["folder", "reqFilterFolder", ["ga", "games", "Phone games"]],
  ]) {
    click(doc.getElementById("reqNextPage"));
    await wait(250);
    const paged = listCalls()[listCalls().length - 1] || "";
    fetchUrls.length = 0;
    const input = doc.getElementById(id);
    for (const text of typed) {
      input.value = text;
      input.dispatchEvent(new window.Event("input", { bubbles: true }));
      await wait(60);
    }
    const whileTyping = statsCalls().length;
    await wait(700);
    originFilters[name] = {
      pagedUrl: paged,
      loadsWhileTyping: whileTyping,
      loadsAfterPause: statsCalls().length,
      listUrl: listCalls()[listCalls().length - 1] || "",
      statsUrl: statsCalls()[statsCalls().length - 1] || "",
      originUrl: originCalls()[originCalls().length - 1] || "",
      costUrl: since("/admin/api/requests/cost")[0] || "",
      ttftUrl: since("/admin/api/requests/ttft")[0] || "",
      persisted: persisted()[name] || null,
    };
    input.value = "";
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
    await wait(700);
  }

  // A breakdown row filters the page: the full path goes into the box.
  click(doc.getElementById("reqNextPage"));
  await wait(250);
  fetchUrls.length = 0;
  const button = doc.querySelector("#reqFolderBreakdown .origin-filter-button");
  if (button) click(button);
  await wait(300);
  originFilters.clickedFolderValue = doc.getElementById("reqFilterFolder").value;
  originFilters.clickListUrl = listCalls()[listCalls().length - 1] || "";
  originFilters.clickPersisted = persisted().folder || null;
  const sessionButton = doc.querySelector("#reqSessionBreakdown .origin-filter-button");
  if (sessionButton) click(sessionButton);
  await wait(300);
  originFilters.clickedSessionValue = doc.getElementById("reqFilterSession").value;

  // A reload restores both boxes from the persisted state.
  const saved = persisted();
  doc.getElementById("reqFilterSession").value = "";
  doc.getElementById("reqFilterFolder").value = "";
  window.eval("restoreReqFilters(" + JSON.stringify(saved) + ")");
  originFilters.restored = {
    session: doc.getElementById("reqFilterSession").value,
    folder: doc.getElementById("reqFilterFolder").value,
  };

  // The export carries them too.
  fetchUrls.length = 0;
  window.eval('openExportModal("requests")');
  try {
    // Only the URL is the claim; the stub has no blob for the download.
    await window.eval("runExport()");
  } catch {
    /* expected under the stub */
  }
  window.eval("closeExportModal()");
  await wait(100);
  originFilters.exportUrl = since("/admin/api/export")[0] || "";

  // Clear empties both, forgets both, and the next query carries neither.
  fetchUrls.length = 0;
  click(doc.getElementById("reqClearFilters"));
  await wait(700);
  originFilters.cleared = [
    doc.getElementById("reqFilterSession").value,
    doc.getElementById("reqFilterFolder").value,
  ];
  originFilters.clearedListUrl = listCalls()[listCalls().length - 1] || "";
  originFilters.clearedPersisted = persisted();

  // A failed breakdown says so in its own panel and nowhere else.
  window.eval('renderRequestOriginBreakdowns({ error: "boom" })');
  originFilters.errorNote = doc.getElementById("reqFolderBreakdownNote").textContent;
  window.eval("renderRequestOriginBreakdowns(null)");
  originFilters.disabledRows = doc.querySelectorAll("#reqFolderBreakdown tr").length;
}

// ------------------------------------------------------- in flight (7.45.0)
/* The panel through every state it can be in: empty, five rows, the
   two-snapshot labels, stuck, a row finishing, 120 rows paged, the live
   detail and its keyboard path, off, collapsed -- and the Requests table
   untouched throughout. Polls are driven by the panel's own Refresh button
   with the interval Off, so no timer decides what a capture sees. */
const inflight = {};
{
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const click = (el) => el.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  const $ = (id) => doc.getElementById(id);
  const poll = async () => {
    click($("reqInflightRefresh"));
    await wait(80);
  };
  const rowsShown = () =>
    Array.from($("reqInflightRows").querySelectorAll("tr.inflight-row"));
  const chips = () =>
    Object.fromEntries(
      rowsShown().map((tr) => [
        tr.dataset.inflightId,
        tr.querySelector(".inflight-chip").textContent,
      ]),
    );
  const row = (n, extra = {}) => ({
    id: `req_${String(n).padStart(4, "0")}`,
    started_at: 1790000000 + n,
    started_at_mono: 1000 + n,
    elapsed_ms: (4000 - n) * 1000,
    endpoint: "/v1/messages",
    protocol: "anthropic",
    stream: true,
    harness: "claude",
    requested_model: "claude-sonnet-4-5",
    tier: "sonnet",
    tier_source: "model",
    attempt_index: 0,
    provider: "opencode",
    model_ref: "opencode/qwen3-coder",
    phase: "awaiting_content",
    phase_since: 1000 + n,
    phase_elapsed_ms: 12000,
    describe_hops: 0,
    observed: true,
    ttft_ms: 17.2,
    first_content_ms: null,
    output_chars: 0,
    thinking_chars: 0,
    chunks_to_client: 1,
    last_chunk_age_s: 11.9,
    waited_s: 0,
    attempt_tries: 0,
    last_try_status: null,
    last_try_error_kind: null,
    key_label: "sk-8\u2026Kofx",
    proxy_label: null,
    tools_count: 12,
    input_chars: 48000,
    image_count: 0,
    session_id: "0f3c2a1b-6d5e-4f70-9a8b-1c2d3e4f5a6b",
    session_short: "0f3c2a1b",
    agent_id: null,
    parent_session_id: null,
    project_dir: "C:\\Users\\devuser\\Projects\\demo",
    project_short: "Projects\\demo \u00b7 #76b11b",
    project_dir_pending: false,
    origin_source: "header",
    ...extra,
  });
  const requestsBody = () => $("reqTableBody").innerHTML;
  const requestsHeaders = () =>
    Array.from(doc.querySelectorAll(".requests-table thead th")).map((th) => th.textContent);

  doc.querySelector('.nav-link[data-view="requests"]').click();
  await wait(300);
  inflight.defaultInterval = $("reqInflightInterval").value;
  inflight.intervalOptions = Array.from($("reqInflightInterval").options).map((o) => o.value);
  $("reqInflightInterval").value = "0";
  $("reqInflightInterval").dispatchEvent(new window.Event("change", { bubbles: true }));
  await wait(80);
  inflight.persistedInterval = JSON.parse(
    window.localStorage.getItem("mcc-dashboard-state") || "{}",
  ).inflightInterval;
  const tableBefore = requestsBody();
  inflight.requestsHeadersBefore = requestsHeaders();

  // Empty.
  INFLIGHT.rows = [];
  await poll();
  inflight.empty = {
    emptyHidden: $("reqInflightEmpty").hidden,
    emptyText: $("reqInflightEmpty").textContent.replace(/\s+/g, " ").trim(),
    tableHidden: $("reqInflightTableWrap").hidden,
    status: $("reqInflightStatus").textContent,
    badgeHidden: $("navInflightBadge").hidden,
    navLabel: doc.querySelector('.nav-link[data-view="requests"]').textContent,
  };

  // Five, served newest first to prove the panel orders by age itself.
  const five = [
    row(1, {
      phase: "attempt",
      phase_elapsed_ms: 301000,
      attempt_index: 1,
      attempt_tries: 2,
      last_try_status: 429,
      last_try_error_kind: "rate_limit",
      waited_s: 2,
      first_content_ms: null,
      ttft_ms: null,
    }),
    row(2, {
      phase: "attempt",
      phase_elapsed_ms: 299000,
      waited_s: 0,
      proxy_label: "proxy-3\u2026a1",
      ttft_ms: null,
    }),
    row(3, {
      phase: "streaming",
      output_chars: 1234,
      thinking_chars: 56,
      first_content_ms: 900,
    }),
    row(4, {
      phase: "routing",
      attempt_index: null,
      provider: null,
      model_ref: null,
      project_dir: null,
      project_short: null,
      project_dir_pending: true,
      agent_id: "agent-7",
    }),
    row(5, {
      elapsed_ms: 5000,
      // Streaming for 400 s with a chunk 2 s ago: still delivering, not stuck.
      phase_elapsed_ms: 400000,
      last_chunk_age_s: 2,
      phase: "streaming",
      observed: false,
      output_chars: null,
      thinking_chars: null,
      session_id: null,
      session_short: null,
      project_dir: null,
      project_short: null,
    }),
  ];
  INFLIGHT.rows = [...five].reverse();
  const statusMutations = [];
  new window.MutationObserver((records) => statusMutations.push(records.length)).observe(
    $("reqInflightStatus"),
    { childList: true, characterData: true, subtree: true },
  );
  await poll();
  inflight.five = {
    order: rowsShown().map((tr) => tr.dataset.inflightId),
    status: $("reqInflightStatus").textContent,
    oldest: $("reqInflightOldest").textContent,
    emptyHidden: $("reqInflightEmpty").hidden,
    headers: Array.from(doc.querySelectorAll(".inflight-table thead th")).map(
      (th) => th.textContent,
    ),
    firstSnapshotChips: chips(),
    cells: rowsShown().map((tr) => Array.from(tr.children).map((td) => td.textContent)),
    stuck: rowsShown().map((tr) => tr.classList.contains("inflight-stuck")),
    stuckBadgeVisible: rowsShown().map(
      (tr) => !tr.querySelector(".inflight-stuck-badge")?.hidden,
    ),
    stuckTitle: doc.querySelector(".inflight-stuck-badge")?.title || "",
    badge: $("navInflightBadge").textContent,
    badgeHidden: $("navInflightBadge").hidden,
    navLabel: doc.querySelector('.nav-link[data-view="requests"]').textContent,
  };
  // Timers tick between refreshes while the panel polls, and stop with it.
  const setInterval_ = async (value) => {
    $("reqInflightInterval").value = value;
    $("reqInflightInterval").dispatchEvent(new window.Event("change", { bubbles: true }));
    await wait(80);
  };
  await setInterval_("30000");
  const ageBefore = rowsShown()[4].querySelector(".inflight-age").textContent;
  await wait(1150);
  inflight.five.ageBefore = ageBefore;
  inflight.five.ageAfter = rowsShown()[4].querySelector(".inflight-age").textContent;
  await setInterval_("0");
  const frozen = rowsShown()[4].querySelector(".inflight-age").textContent;
  await wait(1150);
  inflight.five.ageFrozenWhenOff =
    frozen === rowsShown()[4].querySelector(".inflight-age").textContent;

  // Second snapshot: row 1 slept (waited_s 2 -> 6.5), row 2 did not.
  const mutationsBefore = statusMutations.length;
  INFLIGHT.rows = INFLIGHT.rows.map((r) =>
    r.id === "req_0001" ? { ...r, waited_s: 6.5 } : r,
  );
  await poll();
  inflight.five.secondSnapshotChips = chips();
  inflight.five.backingOffTitle = rowsShown()[0].querySelector(".inflight-chip").title;
  inflight.five.statusMutationsOnSameCount = statusMutations.length - mutationsBefore;
  inflight.liveRegions = {
    inPanel: $("reqInflightPanel").querySelectorAll('[role="status"], [aria-live]').length,
    inRows: $("reqInflightRows").querySelectorAll('[role="status"], [aria-live]').length,
    statusRole: $("reqInflightStatus").getAttribute("role"),
    statusLive: $("reqInflightStatus").getAttribute("aria-live"),
    caption: doc.querySelector(".inflight-table caption")?.textContent.trim() || "",
  };

  // Live detail: open from the Live button, see it follow the next refresh.
  const liveButton = rowsShown()[1].querySelector("button[data-inflight-id]");
  liveButton.focus();
  click(liveButton);
  await wait(50);
  const modalText = () => $("reqInflightModal").textContent.replace(/\s+/g, " ").trim();
  inflight.detail = {
    open: !$("reqInflightModal").hidden,
    title: $("reqInflightDetailTitle").textContent,
    focusOnClose: doc.activeElement === $("reqInflightDetailClose"),
    text: modalText(),
    attempts: Array.from($("reqInflightDetailAttempts").children).map((li) => li.textContent),
  };
  INFLIGHT.rows = INFLIGHT.rows.map((r) =>
    r.id === "req_0002"
      ? { ...r, phase: "awaiting_content", phase_since: 3999, phase_elapsed_ms: 400 }
      : r,
  );
  await poll();
  inflight.detail.afterRefresh = modalText();
  doc.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  await wait(30);
  inflight.detail.closedByEscape = $("reqInflightModal").hidden;
  inflight.detail.focusReturned =
    doc.activeElement && doc.activeElement.dataset
      ? doc.activeElement.dataset.inflightId || ""
      : "";
  // A click anywhere on the row opens it too.
  click(rowsShown()[2].children[2]);
  await wait(30);
  inflight.detail.rowClickOpens = $("reqInflightDetailTitle").textContent;

  // Finishing: row 3 leaves. It stays one refresh as Finished, then goes; the
  // open detail says so and offers the finished record.
  INFLIGHT.rows = INFLIGHT.rows.filter((r) => r.id !== "req_0003");
  await poll();
  inflight.finishing = {
    ids: rowsShown().map((tr) => tr.dataset.inflightId),
    finishingIds: rowsShown()
      .filter((tr) => tr.classList.contains("inflight-finishing"))
      .map((tr) => tr.dataset.inflightId),
    chip: chips().req_0003,
    status: $("reqInflightStatus").textContent,
    detailState: $("reqInflightDetailState").textContent,
    openFinishedVisible: !$("reqInflightDetailOpenFinished").hidden,
  };
  await poll();
  inflight.finishing.idsAfterOneMoreTick = rowsShown().map((tr) => tr.dataset.inflightId);
  click($("reqInflightDetailClose"));
  await wait(30);

  // Every row leaves: the finishing rows keep the table one tick, then empty.
  INFLIGHT.rows = [];
  await poll();
  inflight.drain = {
    finishingRows: rowsShown().length,
    emptyHidden: $("reqInflightEmpty").hidden,
  };
  await poll();
  inflight.drain.rowsAfter = rowsShown().length;
  inflight.drain.emptyHiddenAfter = $("reqInflightEmpty").hidden;
  inflight.drain.badgeHidden = $("navInflightBadge").hidden;

  // 120: fifty at a time, oldest first, the rest counted.
  INFLIGHT.rows = Array.from({ length: 120 }, (_, i) => row(i + 1));
  INFLIGHT.rows[0] = { ...INFLIGHT.rows[0], phase_elapsed_ms: 420000 };
  // Streaming, but nothing for 310 s: the client's idle deadline has passed.
  INFLIGHT.rows[1] = {
    ...INFLIGHT.rows[1],
    phase: "streaming",
    phase_elapsed_ms: 320000,
    last_chunk_age_s: 310,
  };
  inflightUrls.length = 0;
  await poll();
  const pageState = () => ({
    rows: rowsShown().length,
    first: rowsShown()[0]?.dataset.inflightId,
    last: rowsShown()[rowsShown().length - 1]?.dataset.inflightId,
    info: $("reqInflightPageInfo").textContent,
    note: $("reqInflightNote").textContent,
    pagerHidden: $("reqInflightPager").hidden,
    prevDisabled: $("reqInflightPrev").disabled,
    nextDisabled: $("reqInflightNext").disabled,
    url: inflightUrls[inflightUrls.length - 1] || "",
  });
  inflight.many = { page1: pageState() };
  inflight.many.status = $("reqInflightStatus").textContent;
  inflight.many.stuckOnPage1 = rowsShown()
    .filter((tr) => tr.classList.contains("inflight-stuck"))
    .map((tr) => [tr.dataset.inflightId, tr.querySelector(".inflight-stuck-badge").title]);
  click($("reqInflightNext"));
  await wait(80);
  inflight.many.page2 = pageState();
  click($("reqInflightNext"));
  await wait(80);
  inflight.many.page3 = pageState();
  click($("reqInflightPrev"));
  await wait(80);
  click($("reqInflightPrev"));
  await wait(80);
  inflight.many.backToPage1 = pageState();

  // Collapsed: one line, one row asked for.
  inflightUrls.length = 0;
  click($("reqInflightToggle"));
  await wait(80);
  inflight.collapsed = {
    bodyHidden: $("reqInflightBody").hidden,
    expanded: $("reqInflightToggle").getAttribute("aria-expanded"),
    url: inflightUrls[inflightUrls.length - 1] || "",
    status: $("reqInflightStatus").textContent,
    oldest: $("reqInflightOldest").textContent,
    persisted: JSON.parse(window.localStorage.getItem("mcc-dashboard-state") || "{}")
      .inflightCollapsed,
  };
  click($("reqInflightToggle"));
  await wait(80);
  inflight.collapsed.reopenedUrl = inflightUrls[inflightUrls.length - 1] || "";

  // Off.
  INFLIGHT.enabled = false;
  await poll();
  inflight.off = {
    status: $("reqInflightStatus").textContent,
    note: $("reqInflightNote").textContent,
    tableHidden: $("reqInflightTableWrap").hidden,
    emptyHidden: $("reqInflightEmpty").hidden,
    badgeHidden: $("navInflightBadge").hidden,
  };

  // Another page: the badge still counts, with one row asked for.
  INFLIGHT.enabled = true;
  INFLIGHT.rows = five;
  doc.querySelector('.nav-link[data-view="limits"]').click();
  await wait(150);
  inflightUrls.length = 0;
  await window.eval("pollInflight()");
  await wait(50);
  inflight.otherPage = {
    url: inflightUrls[inflightUrls.length - 1] || "",
    badge: $("navInflightBadge").textContent,
    badgeTitle: $("navInflightBadge").title,
  };
  doc.querySelector('.nav-link[data-view="requests"]').click();
  await wait(300);

  // The request log's table and headers were never touched.
  inflight.requestsTableUnchanged = requestsBody() === tableBefore;
  inflight.requestsHeadersAfter = requestsHeaders();

  // Leave the page as found: nothing in flight, badge empty, timers stopped.
  INFLIGHT.rows = [];
  await poll();
  await poll();
  await setInterval_("0");
}

console.log(
  JSON.stringify(
    {
      fatal: null,
      // What this run actually cost, so the subprocess bound in
      // test_admin_static_jsdom.py can be tuned from evidence rather than
      // from whichever machine last timed out.
      harnessWallMs: Date.now() - startedAt,
      advancedFields,
      latencyViews,
      toolCatalogue,
      requestOrigin,
      originFilters,
      inflight,
      cancelledViews,
      catalogueReadout,
      desktopAppBanner,
      guideLinks,
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
        opencodeCredential,
      },
      requestCards,
      requestDetail,
      keyManager,
      keyRail,
      sharedRail,
      capabilityRow,
      keyNames,
      dialectPanels,
      docs,
      limits,
      models,
      routing,
      visionMode,
      describedImages,
      analytics,
      costPanel,
      deferredRace,
      applyBanner,
      logReadout,
      harnessAttr,
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
      exportWindow,
      themePicker,
      customProviders,
      codingAgents,
      proxying,
      desktopApps,
      rtkToggles,
      anthropicOAuthCard,
      anthropicOAuthSignInLabel,
      fetched: Array.from(new Set(fetchCalls)).sort(),
    },
    null,
    2,
  ),
);
