/**
 * Run the real admin.js against a payload crafted for the XSS contracts, and
 * report what rendered.
 *
 * Same bootstrap as tests/api/admin_jsdom_harness.mjs (real index.html, real
 * admin.js, mocked fetch, mandatory Observer stubs), but the payload plants
 * markup-shaped strings everywhere a confirmed injection sink used to
 * interpolate, and the output is limited to the probes those contracts
 * assert on. The probe strings arrive via XSS_PROBE_JSON so the Python side
 * asserts against the exact bytes it planted -- one source of truth.
 *
 * Usage: node admin_xss_jsdom_harness.mjs <admin_static_dir>
 * Env:   XSS_PROBE_JSON={"label":..,"description":..,"display_name":..}
 *        AUTH_OPEN_MODE=open|empty|absent   (drives config.messaging_auth_open)
 * Prints one JSON object on stdout.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { JSDOM, VirtualConsole } from "jsdom";

const dir = process.argv[2];
if (!dir) {
  console.error("usage: admin_xss_jsdom_harness.mjs <admin_static_dir>");
  process.exit(2);
}

const probe = JSON.parse(process.env.XSS_PROBE_JSON || "{}");
const authOpenMode = process.env.AUTH_OPEN_MODE || "open";

const MALICIOUS = {
  label: probe.label ?? '<img src=x onerror=alert(1)>Evil <b>Section</b>',
  description: probe.description ?? "<script>window.__xssRan = true</script>pwned",
  displayName: probe.display_name ?? '<img src=x onerror=alert(2)>Evil <b>P</b>',
};

/* ------------------------------------------------------------------ payload */
const FIELDS = [
  {
    key: "REQUEST_LOG_MAX_ROWS",
    label: "Retained rows",
    section: "budgets",
    type: "number",
    value: "200000",
    default: "200000",
  },
];

const SECTIONS = [
  { id: "budgets", label: MALICIOUS.label, description: MALICIOUS.description },
];

const configPayload = {
  fields: FIELDS,
  sections: SECTIONS,
  provider_status: [],
  paths: { managed: "/tmp/.env" },
};
if (authOpenMode !== "absent") {
  configPayload.messaging_auth_open =
    authOpenMode === "empty" ? [] : ["telegram", "discord"];
}

const CUSTOM_PROVIDER = {
  provider_id: "evil",
  display_name: MALICIOUS.displayName,
  status: "configured",
  base_url: "https://evil.example/v1",
  key_count: 1,
  credential_rotation: "failover",
  model_count: 2,
  masked_keys: ["sk-...abcd"],
  proxy: null,
};

/* 7.122.0: Get Started renders server strings -- every step's label,
   description and instructions, every agent's display name and command, the
   newest request's model and agent, and the hidden-card ids. Until 7.122.0
   the onboarding answer was served EMPTY here, so none of them were probed.
   The page is forced onto Get Started (`dismissed: false`, `complete:
   false`), every string is markup-shaped, and `skipped` carries both a real
   card id and a hostile one. */
const HOSTILE_ONBOARDING = {
  dismissed: false,
  complete: false,
  required_total: 4,
  required_done: 0,
  skipped: ["reasoning", MALICIOUS.displayName],
  request_log: {
    enabled: true,
    total: 3,
    last: {
      id: "<img src=x onerror=alert(9)>req",
      ts_epoch: 1761900000,
      status: MALICIOUS.label,
      provider: MALICIOUS.displayName,
      resolved_model: MALICIOUS.description,
      requested_model: MALICIOUS.label,
      harness: MALICIOUS.displayName,
      optimization: null,
    },
  },
  steps: [
    {
      id: "provider",
      label: MALICIOUS.label,
      description: MALICIOUS.description,
      view: "providers",
      optional: false,
      done: false,
      instructions: [MALICIOUS.description, MALICIOUS.label],
      target: "#providersSections",
      guide_anchors: [],
      agents: [],
      measurable: true,
    },
    {
      id: "client",
      label: MALICIOUS.displayName,
      description: MALICIOUS.description,
      view: "claude",
      optional: false,
      done: true,
      instructions: [MALICIOUS.description],
      target: "#claudeSettingsPanel",
      guide_anchors: [],
      agents: [
        {
          id: "evil",
          display_name: MALICIOUS.displayName,
          kind: "cli",
          installed: true,
          state: "",
          command: "<img src=x onerror=alert(4)>mcc-evil",
          configurable: false,
          requests_7d: 2,
          connected: true,
        },
      ],
      measurable: true,
    },
    {
      id: "analytics",
      label: MALICIOUS.label,
      description: MALICIOUS.description,
      view: "requests",
      optional: false,
      done: true,
      instructions: [MALICIOUS.description],
      target: "#reqStatsCards",
      guide_anchors: [],
      agents: [],
      measurable: true,
    },
  ],
};

const ROUTES = {
  "/admin/api/config": configPayload,
  // A harness display name and catalogue path are server-side strings, but
  // they are the page's only free-text fields and the card renders them into
  // the DOM, so they get the same escaping contract as everything else here.
  "/admin/api/harnesses": {
    harnesses: [
      {
        id: "evil",
        display_name: MALICIOUS.displayName,
        binary: "evil",
        installed: false,
        binary_path: null,
        install_hint: MALICIOUS.description,
        command: "mcc-evil",
        commands: ["mcc-evil"],
        protocol: "anthropic_messages",
        protocol_label: "Anthropic Messages (POST /v1/messages)",
        summary: MALICIOUS.description,
        rtk_agent: true,
        rtk_enabled: false,
        catalogue: {
          format: "evil",
          delivery: "file",
          path: MALICIOUS.label,
          exists: true,
          updated_at: "2026-09-01T00:00:00Z",
          model_count: 1,
          defaulted_model_count: 0,
        },
      },
    ],
  },
  "/admin/api/onboarding": HOSTILE_ONBOARDING,
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
  "/admin/api/models": { models: [] },
  "/admin/api/custom-providers": { providers: [CUSTOM_PROVIDER] },
};

function routeFor(url) {
  const path = String(url).split("?")[0];
  if (Object.prototype.hasOwnProperty.call(ROUTES, path)) return ROUTES[path];
  return {};
}

const virtualConsole = new VirtualConsole();
const consoleErrors = [];
virtualConsole.on("jsdomError", (error) => consoleErrors.push(String(error.message)));
virtualConsole.on("error", (...args) => consoleErrors.push(args.map(String).join(" ")));

const dom = new JSDOM(readFileSync(join(dir, "index.html"), "utf8"), {
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
window.fetch = async (url) => ({
  ok: true,
  status: 200,
  json: async () => routeFor(url),
  text: async () => JSON.stringify(routeFor(url)),
});

const scriptErrors = [];
window.addEventListener("error", (event) => scriptErrors.push(String(event.message)));
window.addEventListener("unhandledrejection", (event) =>
  scriptErrors.push(String(event.reason)),
);

try {
  window.eval(readFileSync(join(dir, "admin.js"), "utf8"));
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

/* 7.122.0: Get Started draws an essential's body only while it is expanded,
   and the agent list lives in "Connect a coding agent"'s body. Open each
   essential in turn -- through the page's own header click, which re-renders
   the list -- and read each row's body while it is open, so every hostile
   string the page can render is in the document at the moment it is read. */
const getStartedRowProbes = [];
{
  const count = doc.querySelectorAll("#getStartedSteps .get-started-step").length;
  for (let index = 0; index < count; index += 1) {
    const header = doc.querySelectorAll("#getStartedSteps .get-started-step-header")[index];
    if (!header) continue;
    header.click();
    await new Promise((resolve) => setTimeout(resolve, 25));
    const row = doc.querySelectorAll("#getStartedSteps .get-started-step")[index];
    if (!row) continue;
    getStartedRowProbes.push({
      label: row.querySelector("strong")?.textContent ?? null,
      line: row.querySelector(".get-started-step-line")?.textContent ?? null,
      pill: row.querySelector(".status-pill")?.textContent ?? null,
      instructions: Array.from(row.querySelectorAll(".get-started-step-instructions li")).map(
        (item) => item.textContent,
      ),
      agentNames: Array.from(row.querySelectorAll(".get-started-agent strong")).map(
        (item) => item.textContent,
      ),
      agentCommands: Array.from(row.querySelectorAll(".get-started-agent-detail code")).map(
        (item) => item.textContent,
      ),
      elementTags: Array.from(row.querySelectorAll(".get-started-step-body *")).map(
        (el) => el.tagName,
      ),
    });
  }
}

const heading = doc.querySelector("#limitsSections .settings-section .section-heading");
const card = doc.querySelector('[data-custom-provider="evil"]');
const providerTitle = card?.querySelector(".provider-title");
const notice = doc.getElementById("messagingAuthNotice");
const agentCard = doc.querySelector(".coding-agent-card[data-harness=\"evil\"]");
const rtkToggle = doc.querySelector("#rtkAgentToggles input[type=checkbox]");

console.log(
  JSON.stringify(
    {
      fatal: null,
      scriptErrors,
      consoleErrors,
      sectionHeading: {
        present: Boolean(heading),
        labelText: heading?.querySelector("h3")?.textContent ?? null,
        descriptionText: heading?.querySelector("p")?.textContent ?? null,
        elementTags: heading
          ? Array.from(heading.querySelectorAll("*")).map((el) => el.tagName)
          : null,
      },
      customProvider: {
        cardPresent: Boolean(card),
        nameText: providerTitle?.querySelector("strong")?.textContent ?? null,
        elementTags: providerTitle
          ? Array.from(providerTitle.querySelectorAll("*")).map((el) => el.tagName)
          : null,
      },
      messagingAuthNotice: {
        present: Boolean(notice),
        hidden: Boolean(notice?.hidden),
        text: notice?.textContent ?? null,
      },
      codingAgent: {
        cardPresent: Boolean(agentCard),
        titleText: agentCard?.querySelector("h4")?.textContent ?? null,
        summaryText: agentCard?.querySelector(".agent-summary")?.textContent ?? null,
        cataloguePath:
          agentCard?.querySelector(".agent-meta dd")?.parentElement
            ?.textContent ?? null,
        elementTags: agentCard
          ? Array.from(agentCard.querySelectorAll("*")).map((el) => el.tagName)
          : null,
        rtkLabel: rtkToggle?.parentElement?.textContent?.trim() ?? null,
      },
      // 7.122.0: every server string Get Started renders, with the element
      // tags it produced -- a markup-shaped string parsed into elements shows
      // up here as IMG/SCRIPT/B/I.
      getStarted: {
        visible: !doc.querySelector("#view-get_started").hidden,
        rows: getStartedRowProbes,
        cardCount: doc.querySelectorAll("#getStartedFeatures .get-started-card").length,
        cardIds: Array.from(
          doc.querySelectorAll("#getStartedFeatures .get-started-card"),
        ).map((card) => card.dataset.getStartedCard),
        moreRows: doc.querySelectorAll("#getStartedMore .get-started-more-row").length,
        panelTags: Array.from(
          doc.querySelectorAll(
            "#getStartedPanel .get-started-step *, #getStartedFeatures *, #getStartedMore *",
          ),
        ).map((el) => el.tagName),
      },
    },
    null,
    2,
  ),
);
// The answer is printed. The page keeps its in-flight poll on a timer from
// the moment a view is shown (7.69.4), and a jsdom window's timers keep Node's
// event loop alive, so this run has to end itself, as the main harness does.
process.exit(0);
