/**
 * The InferHub page (7.110.0), run instead of the main capture when
 * MCC_JSDOM_SCENARIO=marketplaces.
 *
 * Every payload is the server's own: the Python test builds them with the
 * real page builders (api/marketplace_page.py) and writes them to the file
 * MCC_JSDOM_MARKETPLACES_FIXTURE names. What this drives, with the events the
 * page itself listens for (click, change, input):
 *
 *  - the sidebar: the link appears, named after the dialect, only while a
 *    marketplace provider has a key;
 *  - each block renders from the keyed fixture: the route notes as text, the
 *    account with its UNVERIFIED label and "not readable with this key",
 *    the profiles with "would match now", the assignments, the snapshot;
 *  - the editor validates before anything is sent, shows "follows the market"
 *    until a cap is set, and its Save sends the whole store;
 *  - an assignment Save posts the 15th key, one row, to the Models route;
 *  - the explorer draws the ceiling line on the ladder for a cc/ and a cx/
 *    model;
 *  - the last requests render a NULL charge as a dash, never 0, and the
 *    charged ask arrives only on its button;
 *  - the no-key fixture: no link, and every keyed block says so.
 *
 * Prints nothing itself: `run()` returns the measurements.
 */

import { readFileSync } from "node:fs";

const clone = (value) => (value === undefined ? value : JSON.parse(JSON.stringify(value)));
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function prepareMarketplacesScenario({ window, ROUTES }) {
  const fixture = JSON.parse(
    readFileSync(process.env.MCC_JSDOM_MARKETPLACES_FIXTURE, "utf-8"),
  );
  const doc = window.document;
  const flat = (el) => (el ? (el.textContent || "").replace(/\s+/g, " ").trim() : "");
  const settle = () => wait(250);
  let mode = "keyed";
  const posts = [];
  const gets = [];
  const ok = (body) => ({
    ok: true,
    status: 200,
    statusText: "OK",
    headers: { get: () => null },
    json: async () => clone(body),
    text: async () => JSON.stringify(body),
  });
  const refuse = (status, detail) => ({
    ok: false,
    status,
    statusText: "Refused",
    headers: { get: () => null },
    json: async () => ({ detail }),
    text: async () => JSON.stringify({ detail }),
  });

  const harnessFetch = window.fetch;
  window.fetch = async (url, options = {}) => {
    const full = String(url);
    const path = full.split("?")[0];
    const method = String(options.method || "GET").toUpperCase();
    if (path === "/admin/api/model-admin/overrides") {
      posts.push({ path, method, body: JSON.parse(options.body) });
      return ok(ROUTES["/admin/api/model-admin"] || {});
    }
    if (path === "/admin/api/config/apply") {
      posts.push({ path, method, body: JSON.parse(options.body) });
      return ok({ applied: true, errors: [], warnings: [], pending_fields: [], restart: {} });
    }
    if (!path.startsWith("/admin/api/marketplaces")) return harnessFetch(url, options);
    if (method === "GET") gets.push(full);
    else posts.push({ path, method, body: options.body ? JSON.parse(options.body) : null });
    const set = fixture[mode];
    if (path === "/admin/api/marketplaces") {
      return ok(full.includes("summary=true") ? set.summary : set.page);
    }
    if (path === "/admin/api/marketplaces/profiles" && method === "PUT") {
      const sent = JSON.parse(options.body);
      if (sent.expected_digest !== set.page.store.digest) return refuse(409, "changed");
      return ok(set.saved || set.page);
    }
    if (path === "/admin/api/marketplaces/refresh") return ok(set.refreshed || set.page);
    const offers = path.match(/^\/admin\/api\/marketplaces\/([^/]+)\/offers$/);
    if (offers) {
      const model = new URL(full, "http://127.0.0.1").searchParams.get("model");
      return ok((set.offers || {})[model] || { model_id: model, known: false, levels: [] });
    }
    if (/\/recent$/.test(path)) return ok(set.recent || { enabled: true, rows: [] });
    if (/\/charged$/.test(path)) return ok(set.charged || { state: "no_key", rows: {} });
    return ok({});
  };

  const navLink = () =>
    Array.from(doc.querySelectorAll(".nav-link")).find((link) => link.dataset.view === "marketplaces");
  const view = () => doc.querySelector('.admin-view[data-view="marketplaces"]');
  const body = (id) => doc.getElementById(id);

  function describeAccount() {
    const box = body("marketplaceAccountBody");
    const facts = {};
    if (box) {
      Array.from(box.querySelectorAll("dt")).forEach((term) => {
        facts[flat(term)] = flat(term.nextElementSibling);
      });
    }
    return { text: flat(box), facts };
  }

  function describeProfiles() {
    return Array.from(doc.querySelectorAll("#marketplaceProfilesBody .mkt-profile")).map((card) => ({
      name: card.dataset.profile,
      summary: flat(card.querySelector(".mkt-profile-summary")),
      follows: flat(card.querySelector(".mkt-follows")),
      rows: flat(card.querySelector(".mkt-profile-rows")),
      counts: flat(card.querySelector(".mkt-match-counts")),
      matches: Array.from(card.querySelectorAll(".mkt-match")).map((item) => ({
        cls: item.className,
        text: flat(item),
      })),
    }));
  }

  function describeLadder() {
    const ladder = doc.querySelector("#marketplaceExplorerBody .mkt-ladder");
    if (!ladder) return null;
    return Array.from(ladder.children).map((child) => ({
      cls: child.className,
      text: flat(child),
      width: child.querySelector(".mkt-ladder-fill")
        ? child.querySelector(".mkt-ladder-fill").style.width
        : null,
    }));
  }

  function describeRecent() {
    const table = doc.querySelector("#marketplaceRecentBody .mkt-recent-table");
    if (!table) return { headers: [], rows: [], text: flat(body("marketplaceRecentBody")) };
    return {
      headers: Array.from(table.querySelectorAll("thead th")).map(flat),
      rows: Array.from(table.querySelectorAll("tbody tr")).map((tr) => ({
        id: tr.dataset.request,
        cells: Array.from(tr.children).map(flat),
        classes: Array.from(tr.children).map((cell) => cell.className),
      })),
      words: flat(doc.querySelector("#marketplaceRecentBody .mkt-recent-words")),
    };
  }

  function editorState() {
    const editor = body("marketplaceEditor");
    return {
      hidden: editor ? editor.hidden : null,
      problems: editor ? Array.from(editor.querySelectorAll(".mkt-editor-problem")).map(flat) : [],
      follows: editor ? flat(editor.querySelector(".mkt-editor-follows")) : "",
      fields: editor
        ? Array.from(editor.querySelectorAll(".mkt-editor-input")).map((input) => input.dataset.field)
        : [],
    };
  }

  const type = (input, value) => {
    input.value = value;
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
  };
  const choose = (select, value) => {
    select.value = value;
    select.dispatchEvent(new window.Event("change", { bubbles: true }));
  };
  const press = (button) => button.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  const buttonIn = (root, text) =>
    Array.from((root || doc).querySelectorAll("button")).find((button) => flat(button) === text) || null;

  async function run({ scriptErrors, consoleErrors }) {
    await wait(900);
    const out = { scenarioFatal: null };
    const link = navLink();
    if (!link) {
      out.scenarioFatal = "no marketplaces nav link";
      return out;
    }
    out.nav = { hidden: link.hidden, label: flat(link) };
    press(link);
    await wait(700);
    const page = view();
    out.view = {
      hidden: page ? page.hidden : null,
      title: flat(doc.getElementById("pageTitle")),
      blockOrder: Array.from(page.querySelectorAll(":scope > .mkt-honesty, :scope > .mkt-card")).map(
        (el) => el.id,
      ),
    };
    out.gets = gets.slice();
    const notes = body("marketplaceRouteNotes");
    out.routeNotes = {
      quotes: Array.from(notes.querySelectorAll(".mkt-route-note")).map(flat),
      markupInside: notes.querySelectorAll("b, img, script").length,
    };
    out.honesty = flat(body("marketplaceHonesty"));
    out.warning = { hidden: body("marketplaceWarning").hidden, text: flat(body("marketplaceWarning")) };
    out.account = describeAccount();
    out.profiles = describeProfiles();
    out.assignments = Array.from(
      doc.querySelectorAll("#marketplaceAssignmentsBody .mkt-assign-table tr"),
    ).map((tr) => ({
      scope: tr.dataset.scope,
      key: tr.dataset.key,
      value: tr.querySelector("select") ? tr.querySelector("select").value : null,
      note: flat(tr.lastElementChild),
      noteClass: tr.lastElementChild.firstElementChild
        ? tr.lastElementChild.firstElementChild.className
        : null,
    }));
    out.snapshot = {
      text: flat(body("marketplaceSnapshotBody")),
      sources: Array.from(doc.querySelectorAll("#marketplaceSnapshotBody .mkt-sources tbody tr")).map(
        (tr) => ({ name: tr.dataset.source, cells: Array.from(tr.children).map(flat) }),
      ),
      publicMarket: body("marketplacePublicMarket") ? body("marketplacePublicMarket").checked : null,
    };
    out.recent = describeRecent();

    // ---------------------------------------------------- the editor
    press(buttonIn(body("marketplaceProfilesBody"), "New profile"));
    await settle();
    const editor = body("marketplaceEditor");
    out.editorOpened = editorState();
    choose(body("marketplaceEditorKind"), "pct_of_official");
    await settle();
    type(body("marketplaceEditorName"), "Bad Name");
    await settle();
    out.editorInvalid = editorState();
    const putsBefore = posts.filter((post) => post.method === "PUT").length;
    press(buttonIn(editor, "Save profile"));
    await settle();
    out.invalidSaveSent = posts.filter((post) => post.method === "PUT").length - putsBefore;
    choose(body("marketplaceEditorKind"), "cheapest_level_now");
    await settle();
    out.editorFollows = editorState();
    const cap = editor.querySelector('.mkt-editor-input[data-field="cap_percent"]');
    type(cap, "20");
    await settle();
    out.editorCapped = editorState();
    choose(body("marketplaceEditorKind"), "pct_of_official");
    await settle();
    type(body("marketplaceEditorName"), "opus-cap");
    type(editor.querySelector('.mkt-editor-input[data-field="percent"]'), "10");
    await settle();
    out.editorValid = editorState();
    press(buttonIn(editor, "Save profile"));
    await wait(500);
    out.savedPut = posts.filter((post) => post.method === "PUT").slice(-1)[0] || null;
    out.editorAfterSave = editorState();
    out.statusAfterSave = flat(body("marketplaceStatus"));

    // ----------------------------------------------- an assignment Save
    const providerRow = doc.querySelector('#marketplaceAssignmentsBody tr[data-scope="provider"]');
    const assignPosts = () => posts.filter((post) => post.path === "/admin/api/model-admin/overrides");
    choose(providerRow.querySelector("select"), "cap20");
    press(buttonIn(providerRow, "Save"));
    await wait(500);
    const providerRowAgain = doc.querySelector('#marketplaceAssignmentsBody tr[data-scope="provider"]');
    choose(providerRowAgain.querySelector("select"), "inherit");
    press(buttonIn(providerRowAgain, "Save"));
    await wait(500);
    const modelRow = doc.querySelector('#marketplaceAssignmentsBody tr[data-scope="model"]');
    choose(modelRow.querySelector("select"), "__decides__");
    press(buttonIn(modelRow, "Save"));
    await wait(500);
    out.assignPosts = assignPosts().map((post) => post.body);

    // ------------------------------------------------- the offer explorer
    const pickModel = async (model) => {
      choose(body("marketplaceExplorerModel"), model);
      await wait(500);
      return {
        title: flat(doc.querySelector("#marketplaceExplorerBody .mkt-offers-title")),
        who: flat(doc.querySelector("#marketplaceExplorerBody .mkt-offers-who")),
        ladder: describeLadder(),
        facts: Object.fromEntries(
          Array.from(doc.querySelectorAll("#marketplaceExplorerBody dt")).map((term) => [
            flat(term),
            flat(term.nextElementSibling),
          ]),
        ),
        note: flat(doc.querySelector("#marketplaceExplorerBody .mkt-route-note")),
        warning: flat(doc.querySelector("#marketplaceExplorerBody .mkt-ladder .mkt-warning")),
      };
    };
    out.groups = Array.from(body("marketplaceExplorerModel").querySelectorAll("optgroup")).map(
      (group) => group.label,
    );
    type(body("marketplaceExplorerSearch"), "cx/");
    await settle();
    out.searchedGroups = Array.from(body("marketplaceExplorerModel").querySelectorAll("optgroup")).map(
      (group) => [group.label, group.children.length],
    );
    out.explorerCx = await pickModel(fixture.models.cx);
    type(body("marketplaceExplorerSearch"), "");
    await settle();
    out.explorerCc = await pickModel(fixture.models.cc);

    // --------------------------------------------- the charged ask (Q17)
    const chargedBefore = posts.filter((post) => /\/charged$/.test(post.path)).length;
    out.chargedBeforePress = chargedBefore;
    press(buttonIn(body("marketplaceRecentBody"), "Fetch the charged ask"));
    await wait(500);
    out.chargedPosts = posts.filter((post) => /\/charged$/.test(post.path)).map((post) => post.body);
    out.recentAfterCharged = describeRecent();

    // ------------------------------------------------------- no key at all
    mode = "nokey";
    await window.refreshMarketplaceNav();
    await settle();
    out.noKeyNav = { hidden: navLink().hidden };
    await window.loadMarketplaces({ account: true });
    await window.loadMarketplaceRecent();
    await settle();
    out.noKey = {
      account: describeAccount(),
      warningHidden: body("marketplaceWarning").hidden,
      refreshDisabled: buttonIn(body("marketplaceSnapshotBody"), "Refresh")
        ? buttonIn(body("marketplaceSnapshotBody"), "Refresh").disabled
        : null,
      chargedDisabled: buttonIn(body("marketplaceRecentBody"), "Fetch the charged ask")
        ? buttonIn(body("marketplaceRecentBody"), "Fetch the charged ask").disabled
        : null,
      recent: describeRecent(),
      snapshot: flat(body("marketplaceSnapshotBody")),
    };

    out.scriptErrors = scriptErrors;
    out.consoleErrors = consoleErrors;
    return out;
  }

  return { run };
}
