/**
 * The Web Search page's search chain, key health and analytics ladder
 * (7.113.0), run instead of the main capture when
 * MCC_JSDOM_SCENARIO=websearch_chain.
 *
 * The payloads are the server's own shapes, built by
 * tests/support/websearch_page_fixture.py from the tree's own manifest and
 * written to the file MCC_JSDOM_WEBSEARCH_FIXTURE names. What this drives,
 * with the events the page itself listens for:
 *
 *  - with no chain set, the Web Search page's route summary, provider cards
 *    (one key manager open), settings section and Recent requests table, as
 *    a stripped DOM string the Python test compares with the one the BASE
 *    tree (7.112.1) drew from the same fixture -- the additive nodes (the
 *    Search chain card, its status panel, the Context7 card and field, the
 *    7.112.0 plain chain fields) removed on both sides;
 *  - the chain editor: add, pick, a repeat, pause, the arrows, a drag and its
 *    Undo, and the Apply body (the two settings, nothing else);
 *  - a reload with a chain set: the summary, the card, no dirty field (an
 *    unchanged Save would send nothing);
 *  - the key health words for every bench reason, the pool note, Reset;
 *  - the Context7 card;
 *  - the Analytics columns and the tries ladder (NULL as a dash), the detail
 *    modal, and third-party text as text nodes only.
 *
 * Prints nothing itself: `run()` returns the measurements.
 */

import { readFileSync } from "node:fs";

const clone = (value) => (value === undefined ? value : JSON.parse(JSON.stringify(value)));
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function prepareWebSearchChainScenario({ window, ROUTES }) {
  const fixture = JSON.parse(readFileSync(process.env.MCC_JSDOM_WEBSEARCH_FIXTURE, "utf-8"));
  const doc = window.document;
  const flat = (el) => (el ? (el.textContent || "").replace(/\s+/g, " ").trim() : "");
  const settle = () => wait(250);

  // Dates render through toLocaleString, which depends on the machine's zone
  // and locale; the baseline is written on one machine and read on another.
  window.Date.prototype.toLocaleString = function toLocaleString() {
    return this.toISOString();
  };

  const baseConfig = ROUTES["/admin/api/config"];
  let fieldsMode = "fields";
  const configFor = () => {
    const config = clone(baseConfig);
    config.sections = [
      ...config.sections.filter((section) => section.id !== "websearch"),
      fixture.section,
    ];
    config.fields = [
      ...config.fields.filter((field) => field.section !== "websearch"),
      ...fixture[fieldsMode],
    ];
    config.websearch_manual_only = fixture.manual_only;
    return config;
  };

  let keysMode = "healthy";
  let statsMode = "stats";
  let requestsMode = "requests";
  let routesMode = null;
  const posts = [];
  const gets = [];
  const analyticsUrls = [];
  const ok = (body) => ({
    ok: true,
    status: 200,
    statusText: "OK",
    headers: { get: () => null },
    json: async () => clone(body),
    text: async () => JSON.stringify(body),
  });

  const harnessFetch = window.fetch;
  window.fetch = async (url, options = {}) => {
    const full = String(url);
    const path = full.split("?")[0];
    const method = String(options.method || "GET").toUpperCase();
    if (path === "/admin/api/config" && method === "GET") return ok(configFor());
    if (path === "/admin/api/config/apply") {
      posts.push({ path, method, body: JSON.parse(options.body) });
      return ok({ applied: true, errors: [], warnings: [], pending_fields: [], restart: {} });
    }
    if (path.startsWith("/admin/api/websearch/credentials/")) {
      if (method === "GET") {
        gets.push(full);
        const keys = clone(fixture.keys[keysMode]);
        const env = path.split("/")[5];
        keys.env_key = env;
        return ok(keys);
      }
      posts.push({ path: full, method, body: options.body ? JSON.parse(options.body) : null });
      if (/\/reset$/.test(path)) {
        keysMode = "reset_after";
        return ok({ reset: true, pool: "chain", index: 0 });
      }
      return ok({});
    }
    if (path === "/admin/api/websearch/stats") {
      analyticsUrls.push(full);
      return ok(fixture[statsMode]);
    }
    if (path === "/admin/api/websearch/requests") {
      analyticsUrls.push(full);
      return ok(fixture[requestsMode]);
    }
    if (path === "/admin/api/websearch/routes") {
      analyticsUrls.push(full);
      return ok(routesMode ? fixture[routesMode] : { total: 0, limit: 50, offset: 0, items: [] });
    }
    const detail = path.match(/^\/admin\/api\/websearch\/requests\/(\d+)$/);
    if (detail) return ok(fixture.details[detail[1]] || {});
    return harnessFetch(url, options);
  };

  const view = () => doc.querySelector('.admin-view[data-view="web_search"]');
  const navLink = () =>
    Array.from(doc.querySelectorAll(".nav-link")).find((link) => link.dataset.view === "web_search");
  const press = (button) => button.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  const choose = (select, value) => {
    select.value = value;
    select.dispatchEvent(new window.Event("change", { bubbles: true }));
  };
  const buttonIn = (root, text) =>
    Array.from((root || doc).querySelectorAll("button")).find((button) => flat(button) === text) || null;
  const card = (id) => doc.querySelector(`[data-websearch-provider="${id}"]`);
  const closeDialog = () => {
    if (!doc.getElementById("providerDetailModal").hidden) {
      doc.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    }
  };
  const chainCard = () => doc.querySelector(".ws-chain-card");
  const chainRows = () => Array.from(doc.querySelectorAll(".ws-chain-card .model-chain-row"));
  const chainInput = () => doc.querySelector('input[data-key="WEB_SEARCH_CHAIN"]');
  const pausedInput = () => doc.querySelector('input[data-key="WEB_SEARCH_CHAIN_PAUSED"]');
  const dirty = () => flat(doc.getElementById("dirtyState"));
  const statusText = () => flat(doc.getElementById("webSearchChainStatus")?.querySelector("p"));

  /* The DOM as text, with the nodes this release adds removed: applied to the
     base tree's page and to this one alike, so what is left must match. */
  const STRIP = [
    ".ws-chain-card",
    "#webSearchChainStatus",
    '[data-websearch-provider="context7"]',
    '.field[data-key="CONTEXT7_API_KEY"]',
    '[data-websearch-provider="deepseek"]',
    '.field[data-key="DEEPSEEK_SEARCH_API_KEY"]',
    '.field[data-key="DEEPSEEK_SEARCH_USE_INFERENCE_KEY"]',
    '.field[data-key="DEEPSEEK_SEARCH_NEED_CONTENT"]',
    '.field[data-key="DEEPSEEK_SEARCH_LOCALIZATION"]',
    '.field[data-key="WEB_SEARCH_CHAIN"]',
    '.field[data-key="WEB_SEARCH_CHAIN_PAUSED"]',
    'option[value="context7"]',
    'option[value="deepseek"]',
    // 7.119.0: the keyless DuckDuckGo hop's request spacing, additive.
    '.field[data-key="WEBSEARCH_LEGACY_MIN_INTERVAL_SECONDS"]',
    // 7.120.2: the older `strict` spelling the ddgs SafeSearch select now
    // offers, additive (Brave's own `strict` option stays compared). The ddgs
    // drawer's corrected help texts are taken out of both sides by the test.
    'select[data-key="DDGS_SAFESEARCH"] option[value="strict"]',
    // 7.121.2: each card's key-pool rotation field, additive -- the thirteen
    // web-search `_ROTATION` settings had no control anywhere before.
    '[data-websearch-provider] .field[data-key$="_ROTATION"]',
    // 8.26.0: multi-door web search, tool loop, and HTTP MCP controls, additive.
    '.field[data-key="WEB_SEARCH_RESPONSES_ENABLED"]',
    '.field[data-key="WEB_SEARCH_ANTHROPIC_MULTI_TOOL_ENABLED"]',
    '.field[data-key="WEB_SEARCH_DOOR_MODE"]',
    '.field[data-key="WEB_SEARCH_TOOL_LOOP_MAX_STEPS"]',
    '.field[data-key="WEB_SEARCH_MCP_HTTP_ENABLED"]',
    // 8.27.0: Gemini /v1beta and Chat Completions /v1/chat/completions web search controls, additive.
    '.field[data-key="WEB_SEARCH_GEMINI_ENABLED"]',
    '.field[data-key="WEB_SEARCH_CHAT_ENABLED"]',
    // 8.33.0: Web search chain resilience controls, additive.
    '.field[data-key="WEB_SEARCH_CHAIN_SKIP_KINDS"]',
    '.field[data-key="WEB_SEARCH_QUOTA_COOLDOWN_SECONDS"]',
    '.field[data-key="WEB_SEARCH_FALLBACK_ON_EMPTY"]',
  ];
  const stripped = (node) => {
    if (!node) return null;
    const copy = node.cloneNode(true);
    STRIP.forEach((selector) => copy.querySelectorAll(selector).forEach((el) => el.remove()));
    return copy.outerHTML;
  };
  const recentTable = () => {
    const blocks = Array.from(doc.querySelectorAll("#webSearchAnalytics .analytics-block"));
    return blocks.find((block) => flat(block.querySelector("h4, h3")) === "Recent requests") || null;
  };
  const tableShape = (block) => {
    const table = block ? block.querySelector("table") : null;
    if (!table) return null;
    return {
      headers: Array.from(table.querySelectorAll("thead th")).map(flat),
      rows: Array.from(table.querySelectorAll("tbody tr")).map((tr) =>
        Array.from(tr.children).map((td) => flat(td)),
      ),
    };
  };

  /* What a reload after Apply does to this page, without the rest of load():
     the config is read again (state.config, state.fields), every section is
     drawn from it, and the provider cards with it. */
  async function reload() {
    await window.refreshConfigState();
    const config = configFor();
    window.renderSections(config.sections, config.fields);
    window.syncRoutePauseUi();
    window.renderWebSearchProviders();
    window.updateDirtyState();
    await wait(400);
  }

  async function openView() {
    press(navLink());
    await wait(900);
  }

  async function openKeys(providerId) {
    /* USER 2026-11-02: the key pool lives in the provider's dialog now, and
       the dialog opens it eagerly (managing keys is the reason to open a
       provider). So a press on the card is the whole gesture; the returned
       panel is the dialog's own `.ws-key-manager`. */
    const host = card(providerId);
    if (host) press(host);
    await settle();
    await wait(400);
    return doc.getElementById("providerDetailBody").querySelector(".ws-key-manager");
  }

  function describeKeys(panel) {
    return {
      note: flat(panel.querySelector(".ws-key-pool-note")),
      empty: flat(panel.querySelector(".ws-key-empty")),
      rows: Array.from(panel.querySelectorAll(".ws-key-row")).map((row) => ({
        health: flat(row.querySelector(".status-pill")),
        reset: Boolean(row.querySelector(".ws-key-reset")),
      })),
    };
  }

  function describeSummary() {
    const summary = doc.getElementById("webSearchRouteSummary");
    return {
      pill: flat(summary.querySelector(".status-pill")),
      headline: flat(summary.querySelector(".ws-hero-provider")),
      path: flat(summary.querySelector(".ws-hero-path")),
      note: flat(summary.querySelector(".ws-hero-chain-note")),
      skipped: Array.from(summary.querySelectorAll(".ws-hero-path-skipped")).map(flat),
      primary: flat(summary.querySelector(".ws-hero-path-primary")),
    };
  }

  function describeChain() {
    const host = chainCard();
    return {
      exists: Boolean(host),
      inSection: Boolean(host && host.closest("#section-websearch")),
      pill: flat(host?.querySelector(".ws-chain-state")),
      add: flat(host?.querySelector(".model-chain-add")),
      value: chainInput() ? chainInput().value : null,
      paused: pausedInput() ? pausedInput().value : null,
      pausedType: pausedInput() ? pausedInput().type : null,
      rows: chainRows().map((row) => ({
        index: flat(row.querySelector(".model-chain-index")),
        value: row.querySelector("select") ? row.querySelector("select").value : null,
        options: row.querySelector("select")
          ? Array.from(row.querySelector("select").options).map((option) => option.value)
          : [],
        paused: row.classList.contains("is-paused"),
        pause: flat(row.querySelector(".ws-chain-pause")),
        repeat: row.querySelector(".route-repeat-chip:not([hidden])")
          ? flat(row.querySelector(".route-repeat-chip"))
          : "",
        hint: flat(row.querySelector(".ws-chain-hint")),
        grip: Boolean(row.querySelector(".route-drag-grip")),
      })),
      gridKeys: Array.from(doc.querySelectorAll("#section-websearch .field-grid > .field")).map(
        (field) => field.dataset.key,
      ),
    };
  }

  async function addEntry(value) {
    press(chainCard().querySelector(".model-chain-add"));
    await settle();
    const rows = chainRows();
    choose(rows[rows.length - 1].querySelector("select"), value);
    await settle();
  }

  async function run({ scriptErrors, consoleErrors }) {
    await wait(900);
    const out = { scenarioFatal: null };
    if (!navLink()) {
      out.scenarioFatal = "no web_search nav link";
      return out;
    }
    await openView();

    // ------------------------------------------- chain unset: E4 DOM text
    const panel = await openKeys("exa");
    await settle();
    out.unset = {
      dom: {
        summary: stripped(doc.getElementById("webSearchRouteSummary")),
        grid: stripped(doc.getElementById("webSearchGrid")),
        section: stripped(doc.getElementById("section-websearch")),
        recent: stripped(recentTable()),
      },
      dirty: dirty(),
      keys: panel ? describeKeys(panel) : null,
      recent: tableShape(recentTable()),
    };
    const hasChainCard = Boolean(chainCard());
    out.hasChainCard = hasChainCard;
    if (!hasChainCard) {
      // The base tree stops here: it has nothing else this scenario drives.
      out.scriptErrors = scriptErrors;
      out.consoleErrors = consoleErrors;
      return out;
    }
    out.unset.chain = describeChain();
    out.unset.summaryText = describeSummary();

    // -------------------------------------------------- the chain editor
    await addEntry("exa");
    await addEntry("context7");
    await addEntry("tavily");
    await addEntry("exa");
    out.edited = describeChain();
    out.editedSummary = describeSummary();
    out.editedDirty = dirty();
    out.editedCards = {
      exa: flat(card("exa")?.querySelector(".provider-meta")),
      context7: flat(card("context7")?.querySelector(".provider-meta")),
      brave: flat(card("brave")?.querySelector(".provider-meta")),
      exaActive: Boolean(card("exa")?.querySelector(".ws-active-badge")),
    };
    // Pause tavily (row 3): every listing of it, and the paused setting.
    press(chainRows()[2].querySelector(".ws-chain-pause"));
    await settle();
    out.pausedOnce = describeChain();
    out.pausedStatus = statusText();
    out.pausedSummary = describeSummary();
    // The arrows: row 1 down, then the last row up.
    press(chainRows()[0].querySelectorAll(".model-chain-move")[1]);
    await settle();
    out.afterDown = describeChain().value;
    press(chainRows()[3].querySelectorAll(".model-chain-move")[0]);
    await settle();
    out.afterUp = describeChain().value;
    // A drag: row 4 dropped in front of row 1, then its Undo.
    const grip = (index) => chainRows()[index].querySelector(".route-drag-grip");
    grip(3).dispatchEvent(new window.MouseEvent("pointerdown", { bubbles: true, button: 0 }));
    chainRows()[0].dispatchEvent(new window.MouseEvent("pointerover", { bubbles: true }));
    doc.dispatchEvent(new window.MouseEvent("pointerup", { bubbles: true }));
    await settle();
    out.afterDrag = describeChain().value;
    out.dragStatus = statusText();
    const undo = buttonIn(doc.getElementById("webSearchChainStatus"), "Undo");
    if (undo) press(undo);
    await settle();
    out.afterUndo = describeChain().value;
    // Remove the second row, then Apply.
    press(chainRows()[1].querySelector(".model-chain-remove"));
    await settle();
    out.afterRemove = describeChain().value;
    out.finalDirty = dirty();
    press(doc.getElementById("applyButton"));
    await wait(600);
    out.applyPosts = posts.filter((post) => post.path === "/admin/api/config/apply").map((post) => post.body);

    // ----------------------------------- reload with a chain set (saved)
    fieldsMode = "fields_chain_set";
    keysMode = "words";
    await reload();
    await openView();
    out.saved = {
      chain: describeChain(),
      summary: describeSummary(),
      dirty: dirty(),
      cards: {
        exa: flat(card("exa")?.querySelector(".provider-meta")),
        exaActive: Boolean(card("exa")?.querySelector(".ws-active-badge")),
        brave: flat(card("brave")?.querySelector(".provider-meta")),
      },
    };

    // --------------------------------------------- key health and Reset
    const words = await openKeys("exa");
    out.words = describeKeys(words);
    const resetsBefore = posts.filter((post) => /\/reset/.test(post.path)).length;
    press(words.querySelector(".ws-key-reset"));
    await wait(500);
    out.resetPosts = posts.filter((post) => /\/reset/.test(post.path)).slice(resetsBefore);
    out.afterReset = describeKeys(
      doc.getElementById("providerDetailBody").querySelector(".ws-key-manager"),
    );

    // ----------------------------- WEB_SEARCH_PROVIDER=disabled still wins
    fieldsMode = "fields_chain_disabled";
    await reload();
    await openView();
    out.disabled = describeSummary();

    // ------------------------------------------------------ Context7 card
    fieldsMode = "fields";
    await reload();
    await openView();
    const c7 = card("context7");
    out.context7 = c7
      ? {
          label: flat(c7.querySelector(".provider-title-label strong")),
          pill: flat(c7.querySelector(".status-pill")),
          meta: flat(c7.querySelector(".provider-meta")),
          buttons: Array.from(c7.querySelectorAll(".card-actions button")).map(flat),
          // USER 2026-11-02: "Test provider" and "Manage keys" moved into the
          // provider's dialog with the rest of the form, so the face carries
          // "Use provider" and those two are read where they now live.
          dialogButtons: (() => {
            press(c7);
            const body = doc.getElementById("providerDetailBody");
            const names = Array.from(body.querySelectorAll("button")).map(flat);
            closeDialog();
            return names;
          })(),
        }
      : null;
    const ds = card("deepseek");
    out.deepseek = ds
      ? {
          label: flat(ds.querySelector(".provider-title-label strong")),
          pill: flat(ds.querySelector(".status-pill")),
          meta: flat(ds.querySelector(".provider-meta")),
          buttons: Array.from(ds.querySelectorAll(".card-actions button")).map(flat),
          gridSecretField: Boolean(
            doc.querySelector('.field[data-key="DEEPSEEK_SEARCH_API_KEY"]'),
          ),
          dialog: (() => {
            press(ds);
            const body = doc.getElementById("providerDetailBody");
            const keys = Array.from(body.querySelectorAll("[data-key]")).map(
              (node) => node.dataset.key,
            );
            const hasKeyManager = Boolean(body.querySelector(".ws-key-manager"));
            closeDialog();
            return { keys, hasKeyManager };
          })(),
        }
      : null;
    out.cardOrder = Array.from(doc.querySelectorAll("#webSearchGrid .provider-card")).map(
      (node) => node.dataset.websearchProvider,
    );
    out.autoActive = Array.from(doc.querySelectorAll("#webSearchGrid .provider-card"))
      .filter((node) => node.querySelector(".ws-active-badge"))
      .map((node) => node.dataset.websearchProvider);

    // ---------------------------------------------- analytics and ladder
    requestsMode = "requests_chain";
    press(doc.getElementById("webSearchStatsRefresh"));
    await wait(600);
    const recent = recentTable();
    out.chainRecent = tableShape(recent);
    const tries = Array.from(recent.querySelectorAll("details.ws-tries"));
    tries.forEach((node) => {
      node.open = true;
    });
    out.triesCells = tries.map((node) => ({
      summary: flat(node.querySelector("summary")),
      lines: Array.from(node.querySelectorAll(".ws-tries-list > li")).map((li) => ({
        cls: li.className,
        text: flat(li),
        decision: flat(li.querySelector(".ws-try-decision")),
      })),
    }));
    out.recentMarkup = recent.querySelectorAll("img, b, script").length;
    const detailFor = async (rowId) => {
      await window.openWebSearchDetail(rowId);
      await settle();
      const meta = {};
      const dl = doc.getElementById("webSearchDetailMeta");
      Array.from(dl.querySelectorAll("dt")).forEach((term) => {
        meta[flat(term)] = flat(term.nextElementSibling);
      });
      const pane = doc.getElementById("webSearchDetailTriesPane");
      const result = {
        meta,
        triesHidden: pane.hidden,
        tries: Array.from(pane.querySelectorAll("li")).map(flat),
        triesText: flat(doc.getElementById("webSearchDetailTries")),
        markup: doc.getElementById("webSearchDetailModal").querySelectorAll("img, b, script").length,
        summary: flat(doc.getElementById("webSearchDetailSummary")),
      };
      window.closeWebSearchDetail();
      await settle();
      return result;
    };
    out.detailFailed = await detailFor(4);
    out.detailSkipped = await detailFor(5);
    out.detailNoChain = await detailFor(3);
    out.detailOld = await detailFor(1);

    // ---------------------------------------------- 8.34.0 analytics + UI
    statsMode = "stats_v834";
    routesMode = "routes_v834";
    requestsMode = "requests_v834_siblings";
    choose(doc.getElementById("webSearchFilterStatus"), "skipped");
    choose(doc.getElementById("webSearchFilterToolKind"), "web_fetch");
    choose(doc.getElementById("webSearchFilterDoor"), "rest");
    const emptyBox = doc.getElementById("webSearchFilterEmptyOnly");
    emptyBox.checked = true;
    emptyBox.dispatchEvent(new window.Event("change", { bubbles: true }));
    await wait(600);

    const blockNamed = (title) => {
      const blocks = Array.from(doc.querySelectorAll("#webSearchAnalytics .analytics-block"));
      return blocks.find((block) => flat(block.querySelector("h4, h3")) === title) || null;
    };
    const routesBlock = blockNamed("Logical Searches (Routes)");
    const keyHealthBlock = blockNamed("Credential health");
    out.v834 = {
      statusOptions: Array.from(doc.getElementById("webSearchFilterStatus").options).map(
        (opt) => opt.value,
      ),
      toolKindOptions: Array.from(doc.getElementById("webSearchFilterToolKind").options).map(
        (opt) => opt.value,
      ),
      analyticsUrls: analyticsUrls.filter((u) => u.includes("status=skipped")),
      routesTable: tableShape(routesBlock),
      routesMarkup: routesBlock ? routesBlock.querySelectorAll("img, b, script").length : -1,
      keyHealthTable: tableShape(keyHealthBlock),
      keyHealthMarkup: keyHealthBlock
        ? keyHealthBlock.querySelectorAll("img, b, script").length
        : -1,
    };

    await window.openWebSearchDetail(101);
    await wait(400);
    const siblingsPane = doc.getElementById("webSearchDetailSiblingsPane");
    const siblingBtns = Array.from(siblingsPane.querySelectorAll("button.ws-sibling-link"));
    const firstModalTitle = flat(doc.getElementById("webSearchDetailTitle"));
    const firstModalMeta = {};
    Array.from(doc.getElementById("webSearchDetailMeta").querySelectorAll("dt")).forEach(
      (term) => {
        firstModalMeta[flat(term)] = flat(term.nextElementSibling);
      },
    );
    const siblingItemsBefore = Array.from(siblingsPane.querySelectorAll("li")).map(flat);
    if (siblingBtns.length) {
      press(siblingBtns[0]);
      await wait(400);
    }
    const secondModalTitle = flat(doc.getElementById("webSearchDetailTitle"));
    const siblingMarkup = siblingsPane.querySelectorAll("img, b, script").length;
    window.closeWebSearchDetail();
    await settle();
    out.v834Siblings = {
      hidden: siblingsPane.hidden,
      items: siblingItemsBefore,
      firstModalTitle,
      firstModalMeta,
      secondModalTitle,
      siblingMarkup,
    };

    out.xssRan = Boolean(window.__wsXss);
    out.gets = gets.slice();

    out.scriptErrors = scriptErrors;
    out.consoleErrors = consoleErrors;
    return out;
  }

  return { run };
}
