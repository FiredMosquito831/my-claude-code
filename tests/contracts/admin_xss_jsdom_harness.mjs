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
    section: "limits",
    type: "number",
    value: "200000",
    default: "200000",
  },
];

const SECTIONS = [
  { id: "limits", label: MALICIOUS.label, description: MALICIOUS.description },
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

const ROUTES = {
  "/admin/api/config": configPayload,
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

const heading = doc.querySelector("#limitsSections .settings-section .section-heading");
const card = doc.querySelector('[data-custom-provider="evil"]');
const providerTitle = card?.querySelector(".provider-title");
const notice = doc.getElementById("messagingAuthNotice");

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
    },
    null,
    2,
  ),
);
