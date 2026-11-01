/**
 * The 7.122.3 dashboard UX pass, run instead of the main capture when
 * MCC_JSDOM_SCENARIO=ux_pass.
 *
 * One scenario for one release, because the release is one pass over the
 * dashboard: every finding in specs/PR-GET-STARTED-AND-UX-SPEC.md §4 that has
 * an observable in the DOM is measured here, and the Analytics canvas is
 * measured by position (which block comes before which), by the charts it
 * draws, and by what a keyboard and a pointer can read off them.
 *
 * Prints nothing itself: `run()` returns the measurements and the harness
 * prints them as one JSON object.
 */

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function prepareUxPassScenario({ window, ROUTES }) {
  const doc = window.document;
  const flat = (el) => (el ? (el.textContent || "").replace(/\s+/g, " ").trim() : "");
  const el = (id) => doc.getElementById(id);
  const view = (id) => doc.querySelector(`#view-${id}`);
  const evaluate = (code) => window.eval(code);

  /** Wait for a predicate, up to `limit` ms. */
  async function waitFor(fn, limit = 4000) {
    const started = Date.now();
    while (Date.now() - started < limit) {
      if (fn()) return true;
      await wait(25);
    }
    return false;
  }

  /** Where a node sits among the view's own children (-1 when absent). */
  const orderOf = (viewId, node) => {
    const host = view(viewId);
    if (!host || !node) return -1;
    return Array.from(host.children).indexOf(node);
  };

  async function run({ scriptErrors, consoleErrors }) {
    const out = {
      scenarioFatal: null,
      scriptErrors,
      consoleErrors,
    };
    const navTo = async (viewId) => {
      const link = doc.querySelector(`.nav-link[data-view="${viewId}"]`);
      if (!link) return false;
      link.click();
      await wait(120);
      return true;
    };

    // `load()` is async and this scenario runs alongside it: nothing below is
    // meaningful until the config payload has landed and the nav is drawn.
    const harness = window.uxPassHarness();
    const ready = await waitFor(
      () => harness.state.config !== null && doc.querySelectorAll(".nav-link").length > 0,
      20000,
    );
    if (!ready) {
      out.scenarioFatal = "the page never loaded its config";
      return out;
    }
    await wait(200);

    // ── U19: the page title is the nav label ─────────────────────────────
    const titles = {};
    for (const viewId of ["requests", "docs"]) {
      await navTo(viewId);
      titles[viewId] = flat(el("pageTitle"));
    }
    out.titles = titles;

    // ── U20: the fold exists in the markup, the rail is bound ────────────
    const toggle = el("sidebarToggle");
    out.sidebar = {
      toggle: Boolean(toggle),
      controls: toggle ? toggle.getAttribute("aria-controls") : null,
      expanded: toggle ? toggle.getAttribute("aria-expanded") : null,
      sidebarId: el("appSidebar") ? el("appSidebar").id : null,
    };
    if (toggle) {
      toggle.click();
      out.sidebar.opened = el("appSidebar").classList.contains("is-open");
      toggle.click();
      out.sidebar.closed = !el("appSidebar").classList.contains("is-open");
    }

    // ── U13: the footer follows the view ─────────────────────────────────
    const bar = () => doc.querySelector(".action-bar");
    const footer = {};
    for (const viewId of ["guide", "docs", "requests", "providers"]) {
      await navTo(viewId);
      footer[viewId] = Boolean(bar() && !bar().hidden);
    }
    out.footer = footer;

    // ── U22: a success message does not follow the reader ────────────────
    evaluate(`showMessage("Added a thing", "ok")`);
    const beforeSwitch = flat(el("messageArea"));
    await navTo("providers");
    out.message = {
      before: beforeSwitch,
      after: flat(el("messageArea")),
    };

    // ── U16: the Providers rail ──────────────────────────────────────────
    const providersJump = el("providersJumpBar");
    out.providersJump = providersJump
      ? {
          links: Array.from(providersJump.querySelectorAll("a")).map((a) => a.getAttribute("href")),
          targets: Array.from(providersJump.querySelectorAll("a")).map((a) =>
            Boolean(doc.getElementById(a.getAttribute("href").slice(1))),
          ),
        }
      : null;

    // ── U15: the Proxying rail, and the chains before the sources ────────
    const proxyingView = view("proxying");
    const proxyingJump = el("proxyingJumpBar");
    out.proxying = {
      jump: proxyingJump
        ? Array.from(proxyingJump.querySelectorAll("a")).map((a) => a.getAttribute("href"))
        : null,
      chainsBeforeSources: proxyingView
        ? orderOf("proxying", el("proxyingList")) < orderOf("proxying", el("proxyingSourcesSection"))
        : null,
      chainsBeforeFeeds: proxyingView
        ? orderOf("proxying", el("proxyingList")) < orderOf("proxying", el("proxyingFeedSection"))
        : null,
    };

    // ── U23: the Models page's four buttons are styled ───────────────────
    const buttonClass = (id) => (el(id) ? el(id).className : null);
    out.modelsButtons = {
      preview: buttonClass("modelsPreviewVisibility"),
      save: buttonClass("modelsSaveVisibility"),
      migrate: buttonClass("modelsMigrateGlobs"),
      reload: buttonClass("modelsReload"),
    };

    // ── U17: no retired command is rendered on any card ──────────────────
    const commandLines = Array.from(doc.querySelectorAll(".agent-command-line")).map(
      (node) => flat(node),
    );
    out.legacyCommands = {
      total: commandLines.length,
      retired: commandLines.filter((line) => line.toLowerCase().startsWith("fcc-")),
      legacyRows: doc.querySelectorAll('.agent-command-row[data-kind="legacy"]').length,
    };

    // ── U12: the chip says "default value" for a value nobody chose ──────
    // The harness's fields carry no `source` (that is the server's), so two
    // are given one -- a managed line whose value is its own default, and one
    // whose value differs -- and the sections are drawn again.
    const equalField = harness.state.fields.get("LOG_LEVEL");
    if (equalField) {
      equalField.source = "managed_env";
      equalField.default = equalField.value;
    }
    const differentField = harness.state.fields.get("REQUEST_LOG_MAX_ROWS");
    if (differentField) {
      differentField.source = "managed_env";
      differentField.default = "not-the-saved-value";
    }
    harness.renderSections(
      harness.state.config.sections,
      Array.from(harness.state.fields.values()),
    );
    await wait(60);
    const fieldChip = (key) => {
      const wrapper = doc.querySelector(`.field[data-key="${key}"]`);
      const chip = wrapper ? wrapper.querySelector(".field-source") : null;
      const reset = wrapper ? wrapper.querySelector(".field-reset") : null;
      return { chip: flat(chip), reset: Boolean(reset) };
    };
    out.chips = {
      equal: fieldChip("LOG_LEVEL"),
      different: fieldChip("REQUEST_LOG_MAX_ROWS"),
    };

    // ── U27/U30: the two wording fixes in the setting chrome ─────────────
    const selectLabels = (key) => {
      const wrapper = doc.querySelector(`.field[data-key="${key}"]`);
      const select = wrapper ? wrapper.querySelector("select") : null;
      return select
        ? {
            first: flat(select.options[0]),
            all: Array.from(select.options).map((option) => flat(option)),
            meta: flat(wrapper.querySelector(".field-default")),
          }
        : null;
    };
    out.benchSelect = selectLabels("FALLBACK_BENCH_ENABLED");
    out.booleanSelect = selectLabels("ENABLE_TOOL_RESULT_TRIMMING");
    // The rotation select and its "default:" line, from the injected pool.
    out.rotationMeta = {};

    // ── U26: the optimizer's opening sentence ────────────────────────────
    const optimizerIntro = view("optimizer")
      ? flat(view("optimizer").querySelector(".view-intro"))
      : null;
    out.optimizerIntro = optimizerIntro;

    // ── U31: Start at Login's label ──────────────────────────────────────
    // The harness's desktop payload says the app is unavailable, so the
    // options block is built here from the payload it would have had.
    harness.state.autostartOptions = {
      origin: "this machine",
      targets: [{ value: "tray", label: "Tray" }],
    };
    harness.state.desktop = { start_at_login: false };
    window.eval("renderDesktopAutostartOptions")();
    await wait(40);
    const autostart = doc.querySelector("#desktopAutostartOptions label");
    out.autostartLabel = flat(autostart);

    // ── U6: the Web Search find box matches a curated haystack ───────────
    // Two web-search services, one with a credential and its rotation
    // control -- the exact shape that made "rotation" match 13 of 15 cards.
    const setField = (field) => harness.state.fields.set(field.key, field);
    setField({
      key: "WEB_SEARCH_PROVIDER", label: "Web search provider", section: "websearch",
      type: "select", value: "tavily", default: "auto",
      options: [
        { value: "tavily", label: "Tavily" },
        { value: "ddgs", label: "DuckDuckGo (ddgs)" },
      ],
      source: "managed_env", secret: false, configured: true,
    });
    setField({
      key: "TAVILY_API_KEY", label: "Tavily API Key", section: "websearch",
      type: "secret", value: "tvly-FAKE", default: "",
      options: [], source: "managed_env", secret: true, configured: true,
    });
    setField({
      key: "TAVILY_API_KEY_ROTATION", label: "Tavily Key Rotation",
      section: "websearch", type: "select", value: "", default: "",
      options: [
        { value: "", label: "Auto (failover across multiple keys, single otherwise)" },
        { value: "single", label: "Single key" },
        { value: "failover", label: "Failover" },
      ],
      source: "managed_env", secret: false, configured: false,
    });
    setField({
      key: "GROQ_API_KEY", label: "Groq API Key", section: "providers",
      provider: "groq", type: "secret", value: "gsk_FAKE", default: "",
      options: [], source: "managed_env", secret: true, configured: true,
    });
    setField({
      key: "GROQ_API_KEY_ROTATION", label: "Groq Key Rotation",
      section: "providers", provider: "groq", type: "select", value: "failover",
      default: "", options: [
        { value: "", label: "Auto (failover across multiple keys, single otherwise)" },
        { value: "failover", label: "Failover" },
      ],
      source: "managed_env", secret: false, configured: true,
    });
    harness.state.config.provider_status = [
      { provider_id: "groq", display_name: "Groq", status: "configured",
        label: "Configured", credential_env: "GROQ_API_KEY", key_count: 2,
        kind: "remote" },
    ];
    // The key pool is offered for a credential the config names as a
    // provider's (renderField reads this set).
    harness.state.credentialEnvs.add("GROQ_API_KEY");
    // The pool's own list, so opening it is the open state a reader sees.
    ROUTES["/admin/api/credentials/GROQ_API_KEY/keys"] = {
      keys: [],
      key_count: 0,
      locked: false,
      rotation: "",
    };
    harness.renderWebSearchProviders();
    const providersHost = doc.getElementById("providersSections");
    if (providersHost) {
      providersHost.replaceChildren(
        harness.renderProviderGroups([
          harness.state.fields.get("GROQ_API_KEY"),
          harness.state.fields.get("GROQ_API_KEY_ROTATION"),
        ]),
      );
    }
    const limitsHost = doc.getElementById("limitsSections");
    if (limitsHost) {
      limitsHost.replaceChildren(harness.renderCredentialHealth([]));
    }
    await wait(80);

    const searchBox = el("webSearchProviderSearch");
    const wsCount = () => flat(el("webSearchProviderCount"));
    const wsVisible = () =>
      Array.from(doc.querySelectorAll("#webSearchGrid .provider-card"))
        .filter((card) => !card.hidden)
        .map((card) => card.dataset.websearchProvider);
    const typeSearch = async (text) => {
      searchBox.value = text;
      searchBox.dispatchEvent(new window.Event("input", { bubbles: true }));
      await wait(30);
      return { shown: wsVisible(), count: wsCount(), noMatch: !el("webSearchNoMatch").hidden };
    };
    out.webSearch = {
      atRest: { shown: wsVisible(), count: wsCount() },
      rotation: await typeSearch("rotation"),
      tavily: await typeSearch("tavily"),
      key: await typeSearch("TAVILY_API_KEY"),
      nothing: await typeSearch("zzzz"),
    };
    searchBox.value = "";
    searchBox.dispatchEvent(new window.Event("input", { bubbles: true }));
    await wait(30);

    // The rotation select's own "default:" line (U30).
    const rotated = doc.querySelector('.field[data-key="TAVILY_API_KEY_ROTATION"]');
    out.rotationMeta = rotated
      ? {
          firstOption: flat(rotated.querySelector("select")?.options[0]),
          meta: flat(rotated.querySelector(".field-default")),
        }
      : null;

    // ── U1: LINKFIX -- the rotation link lands on the card, pool open ────
    const clickPoolLink = async (credentialKey) => {
      const link = doc.querySelector(
        `.rotation-summary a[data-open-pool="${credentialKey}"]`,
      );
      if (!link) return { found: false };
      link.click();
      await wait(220);
      return { found: true };
    };
    const webPool = await clickPoolLink("TAVILY_API_KEY");
    const wsCard = doc.querySelector('[data-websearch-provider="tavily"]');
    out.linkfixWebSearch = {
      ...webPool,
      view: harness.state.activeView,
      card: Boolean(wsCard),
      poolOpen: wsCard ? !wsCard.querySelector(".ws-key-manager").hidden : null,
      pages: Array.from(doc.querySelectorAll(".admin-view"))
        .filter((node) => !node.hidden)
        .map((node) => node.dataset.view),
    };
    const modelPool = await clickPoolLink("GROQ_API_KEY");
    const pvCard = doc.querySelector('.pv-card[data-provider="groq"]');
    out.linkfixModel = {
      ...modelPool,
      view: harness.state.activeView,
      card: Boolean(pvCard),
      cardOpen: pvCard ? pvCard.classList.contains("pv-open") : null,
      poolOpen: pvCard
        ? !pvCard.querySelector(".key-manager-panel").hidden
        : null,
    };

    // ── U18: the rail re-binds after a re-render ─────────────────────────
    harness.renderSections(
      harness.state.config.sections,
      Array.from(harness.state.fields.values()),
    );
    await wait(60);
    window.dispatchEvent(new window.Event("scroll"));
    await wait(60);
    const railCurrent = Array.from(doc.querySelectorAll("#limitsToc a"))
      .filter((link) => link.getAttribute("aria-current") === "true")
      .map((link) => link.getAttribute("href"));
    out.scrollspy = { current: railCurrent };

    // ── The Analytics canvas (ADDENDUM 2) ────────────────────────────────
    // Its own payloads, so the charts have something to draw.
    ROUTES["/admin/api/requests/stats"] = {
      ...ROUTES["/admin/api/requests/stats"],
      total: 7,
      by_model: [
        { key: "fake-small", requests: 4, tokens_in: 704, tokens_out: 96 },
        { key: "fake-thinker", requests: 2, tokens_in: 2400, tokens_out: 48 },
        { key: "fake-large", requests: 1, tokens_in: 1200, tokens_out: 24 },
      ],
      series: [
        { bucket: "2026-10-31T10:00", requests: 3, errors: 1 },
        { bucket: "2026-10-31T10:15", requests: 4, errors: 0 },
      ],
    };
    ROUTES["/admin/api/requests/cost"] = {
      enabled: true,
      cost_estimation_enabled: true,
      totals: { reported_usd: 1.5, estimated_usd: 0.25, priced: 7, requests: 7 },
      by_day: [
        { key: "2026-10-30", reported_usd: 0.5, estimated_usd: 0.1, priced: 3, requests: 3 },
        { key: "2026-10-31", reported_usd: 1.0, estimated_usd: 0.15, priced: 4, requests: 4 },
      ],
      by_provider: [],
      by_model: [],
      by_harness: [],
      by_source: [],
    };
    ROUTES["/admin/api/requests/latency"] = {
      enabled: true,
      attempts: 6,
      ttft_measured: 6,
      rows: [
        { model_ref: "fake-small", outcome: "answered", attempts: 4, ttft_measured: 4,
          p50_ttft_ms: 82, p95_ttft_ms: 160, tokens_out: 96 },
        { model_ref: "fake-thinker", outcome: "answered", attempts: 2, ttft_measured: 2,
          p50_ttft_ms: 2444, p95_ttft_ms: 4482, tokens_out: 48 },
      ],
    };
    ROUTES["/admin/api/requests/ttft"] = {
      enabled: true, measured: 6, attempts: 6, p50_ttft_ms: 82, p95_ttft_ms: 4482,
    };

    await navTo("requests");
    await waitFor(
      () => doc.querySelectorAll("#reqTableBody tr").length > 0 &&
        doc.querySelectorAll("#reqStatsCards .requests-card").length > 0,
    );
    await wait(300);

    const viewRequests = view("requests");
    const children = Array.from(viewRequests.children);
    const indexOf = (node) => children.indexOf(node);
    /** The view's own child that contains `node` (the blocks are nested). */
    const blockOf = (node) => {
      let cursor = node;
      while (cursor && cursor.parentElement !== viewRequests) {
        cursor = cursor.parentElement;
      }
      return cursor ? children.indexOf(cursor) : -1;
    };
    out.analytics = {
      order: {
        toolbar: indexOf(viewRequests.querySelector(".requests-toolbar")),
        tiles: indexOf(el("reqSummaryHeading")),
        inflight: indexOf(el("reqInflightPanel")),
        log: indexOf(el("reqLogHeading")),
        table: indexOf(el("reqLogTableWrap")),
        charts: indexOf(el("reqChartsHeading")),
        cost: blockOf(el("reqCostHeading")),
        maintenance: indexOf(el("reqMaintenance")),
      },
      headingTitles: {
        summary: flat(el("reqSummaryHeading")),
        log: flat(el("reqLogHeading")),
        charts: flat(el("reqChartsHeading")),
      },
      costTile: (() => {
        const tile = el("reqCostTile");
        return tile
          ? {
              exists: true,
              first: el("reqStatsCards").firstElementChild === tile,
              value: flat(tile.querySelector("strong")),
              label: flat(tile.querySelector("span")),
              note: flat(tile.querySelector("small")),
            }
          : { exists: false };
      })(),
      filters: {
        applyLabel: flat(el("reqApplyFilters")),
        inToolbar: Boolean(
          viewRequests.querySelector(".requests-toolbar #reqApplyFilters"),
        ),
        destructiveInToolbar: viewRequests.querySelectorAll(
          ".requests-toolbar .secondary-button.danger",
        ).length,
        destructiveInMaintenance: el("reqMaintenance")
          ? el("reqMaintenance").querySelectorAll(".secondary-button.danger").length
          : 0,
      },
      charts: ["reqSeriesChart", "reqTtftChart", "reqCostChart", "reqModelChart"].map(
        (id) => ({
          id,
          exists: Boolean(el(id)),
          tabIndex: el(id) ? el(id).tabIndex : null,
          describedBy: el(id) ? el(id).getAttribute("aria-describedby") : null,
          readout: flat(el(`${id}Readout`)),
          dataTable: el(id)
            ? el(id).parentElement.querySelectorAll(".chart-data-table tr").length - 1
            : null,
        }),
      ),
      emptyState: {
        hidden: el("reqEmptyState") ? el("reqEmptyState").hidden : null,
      },
    };

    // Keyboard: focus a chart, press ArrowRight, read the live line.
    const series = el("reqSeriesChart");
    series.focus();
    const keyDown = (target, key) => {
      const event = new window.KeyboardEvent("keydown", {
        key,
        bubbles: true,
        cancelable: true,
      });
      target.dispatchEvent(event);
    };
    keyDown(series, "ArrowRight");
    const afterRight = flat(el("reqSeriesChartReadout"));
    keyDown(series, "Home");
    const afterHome = flat(el("reqSeriesChartReadout"));
    keyDown(series, "End");
    const afterEnd = flat(el("reqSeriesChartReadout"));
    out.chartKeyboard = { afterRight, afterHome, afterEnd };

    // Pointer: the tooltip follows the hovered bucket.
    const rect = { left: 0, top: 0 };
    series.dispatchEvent(
      new window.MouseEvent("pointermove", {
        bubbles: true,
        clientX: rect.left + 60,
        clientY: rect.top + 90,
      }),
    );
    const tip = el("reqSeriesChart").parentElement.querySelector(".chart-tooltip");
    out.chartTooltip = {
      exists: Boolean(tip),
      hidden: tip ? tip.hidden : null,
      text: flat(tip),
    };

    // The row opens its own detail (U35) -- the whole row, not only View.
    const firstRow = doc.querySelector("#reqTableBody tr.req-row");
    const rowId = firstRow ? firstRow.dataset.requestId : null;
    const timeCell = firstRow ? firstRow.querySelector(".req-time-open") : null;
    if (timeCell) timeCell.click();
    await wait(200);
    out.rowOpen = {
      id: rowId,
      modalOpen: el("reqDetailModal") ? !el("reqDetailModal").hidden : null,
      title: flat(el("reqDetailTitle")),
    };
    if (el("reqDetailClose")) el("reqDetailClose").click();
    await wait(40);

    // The empty state: a log with nothing in it, and a filter over it.
    ROUTES["/admin/api/requests"] = {
      ...ROUTES["/admin/api/requests"],
      total: 0,
      rows: [],
      has_more: false,
    };
    await navTo("providers");
    await navTo("requests");
    await waitFor(() => el("reqEmptyState") && !el("reqEmptyState").hidden, 3000);
    out.emptyState = {
      visible: el("reqEmptyState") ? !el("reqEmptyState").hidden : null,
      text: flat(el("reqEmptyState")),
      tableRows: doc.querySelectorAll("#reqTableBody tr").length,
    };
    // A filter makes it false, so it goes.
    el("reqFilterProvider").value = "nobody";
    el("reqApplyFilters").click();
    await wait(400);
    out.emptyState.filtered = {
      visible: el("reqEmptyState") ? !el("reqEmptyState").hidden : null,
    };
    el("reqFilterProvider").value = "";
    el("reqClearFilters").click();
    await wait(300);

    return out;
  }

  return { run };
}
