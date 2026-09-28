/**
 * Render the Claude subscription card's account rows from one /sources
 * payload, with the real admin.js, and report what each row says.
 *
 * Same bootstrap as admin_jsdom_harness.mjs (real index.html, real admin.js,
 * the mandatory Observer stubs, a fetch that answers nothing) but no page
 * start-up and no gestures: renderAnthropicOAuthDetails is a pure function of
 * the payload, so this takes well under a second instead of minutes, and it
 * does not take the main harness's lock.
 *
 * Usage: node admin_oauth_ownership_jsdom_harness.mjs <admin_static_dir>
 * Env:   OAUTH_SOURCES_JSON=<the /admin/api/anthropic-oauth/sources payload>
 * Prints one JSON object on stdout.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { JSDOM, VirtualConsole } from "jsdom";

const dir = process.argv[2];
if (!dir) {
  console.error("usage: admin_oauth_ownership_jsdom_harness.mjs <admin_static_dir>");
  process.exit(2);
}

const sources = JSON.parse(process.env.OAUTH_SOURCES_JSON || "{}");

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
window.scrollTo = () => {};
window.fetch = async () => ({
  ok: true,
  status: 200,
  json: async () => ({}),
  text: async () => "{}",
});

const scriptErrors = [];
window.addEventListener("error", (event) => scriptErrors.push(String(event.message)));
window.addEventListener("unhandledrejection", (event) =>
  scriptErrors.push(String(event.reason)),
);

try {
  window.eval(readFileSync(join(dir, "admin.js"), "utf8"));
} catch (error) {
  console.log(JSON.stringify({ fatal: `eval threw: ${error && error.stack}` }));
  process.exit(0);
}

const doc = window.document;
const squash = (value) => (value || "").replace(/\s+/g, " ").trim();

// Each <dt> paired with the <dd> after it: the term, what it says, and the
// class that colours it (value-expired = red, value-pending = amber).
const terms = (list) =>
  Array.from(list.querySelectorAll("dt")).map((dt) => {
    const dd = dt.nextElementSibling;
    return {
      term: squash(dt.textContent),
      value: squash(dd ? dd.textContent : ""),
      className: dd ? dd.className : "",
    };
  });

const details = doc.createElement("div");
const buttons = [
  doc.createElement("button"),
  doc.createElement("button"),
  doc.createElement("button"),
  doc.createElement("button"),
];
buttons.status = doc.createElement("div");
buttons.details = details;
let fatal = null;
try {
  window.eval("renderAnthropicOAuthDetails")(details, sources, buttons);
} catch (error) {
  fatal = `render threw: ${error && error.stack}`;
}

console.log(
  JSON.stringify({
    fatal,
    scriptErrors,
    consoleErrors,
    hidden: details.hidden,
    rows: Array.from(details.querySelectorAll(".oauth-account-row")).map((row) => ({
      accountId: row.dataset.accountId,
      refresh: squash(row.querySelector(".oauth-account-refresh")?.textContent),
      text: squash(row.textContent),
      terms: terms(row),
      pending: Array.from(row.querySelectorAll(".value-pending")).map((node) =>
        squash(node.textContent),
      ),
    })),
  }),
);
