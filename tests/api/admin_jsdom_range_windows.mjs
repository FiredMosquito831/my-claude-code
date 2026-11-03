/**
 * The custom time window (RANGE), proved as URLs (MCC_JSDOM_SCENARIO=range_windows).
 *
 * Two things are captured, and they answer the two questions this release has
 * to answer with a comparison rather than an assertion:
 *
 *  1. THE EQUALITY BASELINE. For each of the five period values the page has
 *     always offered (``""``, ``3600``, ``86400``, ``604800``, ``2592000``),
 *     the scenario drives the real control the way a reader does and records
 *     every URL the page fetched afterwards. The SAME scenario runs against
 *     the BASE tree's ``admin_static`` and the Python test diffs the two
 *     lists: every existing period must produce the byte-identical request it
 *     produced at 8.8.0. ``Date.now`` is frozen (MCC_JSDOM_NOW_MS) because
 *     four of the five values resolve ``now - N``, and a diff of two live
 *     clocks would be measuring the clock.
 *
 *     The export half is captured the same way, including the legacy
 *     ``Custom…`` with an empty pair -- which must still send neither bound.
 *
 *  2. THE NEW SHAPES. Each custom shape's ``{since, until}`` as the PRODUCT
 *     converts it, read back from the URL the page actually built. The Python
 *     test asserts these against the boundary table, with the browser's zone
 *     pinned by ``TZ`` -- October 2026 in UTC+2 is 745 hours, the end month is
 *     included whole, a DST month is 743 or 745 hours, and a leap day is
 *     inside a leap year.
 *
 * Prints nothing itself: ``run()`` returns the measurements and the harness
 * prints them as one JSON object.
 */

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** The five values every saved view, bookmark and link may already carry. */
const FIXED_PERIODS = ["", "3600", "86400", "604800", "2592000"];

/** Every ANALYTICS route the toolbar's window reaches, in the order the loader
 *  asks for them. Anything else in the fetch log is not the window's business
 *  and is dropped, so a later release adding a request cannot make this diff
 *  say the window moved. */
const ANALYTICS_PREFIXES = [
  "/admin/api/requests/stats",
  "/admin/api/requests?",
  "/admin/api/requests/lifetime",
  "/admin/api/requests/cost",
  "/admin/api/requests/ttft",
  "/admin/api/requests/no-answer",
  "/admin/api/requests/origin",
  "/admin/api/requests/pulse",
  "/admin/api/requests/latency",
  "/admin/api/requests/in-loop",
  "/admin/api/analytics/media",
  "/admin/api/requests/count",
];

/** The query parameters that carry a WINDOW, and only those.
 *
 * The rest of a captured URL is deliberately dropped: a diff of whole URLs is
 * mostly a diff of ``local=hide`` and of the filter boxes the scenario left
 * alone, which says nothing about the window. What this release must not move
 * is exactly ``since`` and ``until`` -- and, for the export, ``scope`` and
 * ``format``, which identify which control spoke. */
function windowOf(url) {
  const [path, query = ""] = String(url).split("?");
  const params = new URLSearchParams(query);
  const out = { path };
  for (const key of ["since", "until", "window", "shape"]) {
    if (params.has(key)) out[key] = params.get(key);
  }
  return out;
}

export function prepareRangeWindowsScenario({ window, ROUTES, fetchUrls }) {
  const doc = window.document;

  /** Everything the page asked for since the last reset, restricted to the
   *  Analytics routes, reduced to the window-bearing parameters. */
  function takeUrls(prefixes) {
    const taken = fetchUrls
      .filter((url) => prefixes.some((prefix) => String(url).startsWith(prefix)))
      .map(windowOf);
    fetchUrls.length = 0;
    return taken;
  }

  async function settle() {
    // The view loads a dozen panels, some of them deferred behind the paint
    // (the cost panel deliberately resolves after it). Four event-loop turns
    // and a real pause is what the other scenarios use for the same reason.
    await wait(60);
    await wait(60);
    await wait(60);
  }

  /** Change a control the way a reader does: set it, then fire `change`. */
  async function pick(id, value) {
    const el = doc.getElementById(id);
    if (!el) throw new Error(`no #${id} on this page`);
    el.value = value;
    el.dispatchEvent(new window.Event("change", { bubbles: true }));
    await settle();
  }

  async function type(id, value) {
    const el = doc.getElementById(id);
    if (!el) throw new Error(`no #${id} on this page`);
    el.value = value;
    el.dispatchEvent(new window.Event("input", { bubbles: true }));
    // The toolbar's typing debounce is 400 ms.
    await wait(520);
    await settle();
  }

  async function run({ scriptErrors, consoleErrors }) {
    const out = {
      scenarioFatal: null,
      scriptErrors,
      consoleErrors,
      // The clock every fixed period resolves against, so the Python side can
      // say what it froze.
      frozenNowMs: Number(process.env.MCC_JSDOM_NOW_MS || 0) || null,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    };

    // Wait for the page a reader sees: the Coding agents cards are the last
    // thing `load()` draws, exactly as the ux_views scenario waits.
    const started = Date.now();
    while (
      Date.now() - started < 20000 &&
      !doc.querySelector("#codingAgentsList .coding-agent-card")
    ) {
      await wait(50);
    }
    await wait(900);

    // Open Analytics, which is where the toolbar lives.
    const nav = Array.from(doc.querySelectorAll("#sectionNav .nav-link")).find(
      (link) => link.dataset.view === "requests",
    );
    if (nav) nav.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await settle();
    await settle();

    // ---- 1. the five existing periods, toolbar half -----------------------
    //
    // Each value is selected, the load it triggers is allowed to finish, and
    // the URLs that load produced are recorded. Selecting the SAME value twice
    // would not fire `change`, so the sequence walks away and back.
    await pick("reqFilterWindow", "");
    out.toolbar = {};
    for (const value of FIXED_PERIODS) {
      await pick("reqFilterWindow", value === "" ? "3600" : "");
      fetchUrls.length = 0;
      await pick("reqFilterWindow", value);
      await settle();
      out.toolbar[value || "all"] = takeUrls(ANALYTICS_PREFIXES);
    }

    // The two controls this release adds are invisible on any of the five.
    // Captured on BOTH trees: the base tree has no such elements, so it reads
    // `null`, and the branch reads `true` only for the value that asks for it.
    const hiddenOf = (id) => {
      const el = doc.getElementById(id);
      return el ? Boolean(el.hidden) : null;
    };
    out.customHidden = {};
    for (const value of FIXED_PERIODS) {
      await pick("reqFilterWindow", value);
      out.customHidden[value || "all"] = {
        custom: hiddenOf("reqWindowCustomWrap"),
        range: hiddenOf("reqWindowRange"),
      };
    }

    // ---- 2. the five existing periods, export half -----------------------
    //
    // `openExportModal` reads the current view and sets the dialog's defaults;
    // `exportParamsFor` is the function under test and returns the parameters
    // without running anything. `runExport` would have to fetch a blob the
    // stub cannot produce, so the parameters are read directly -- and the URL
    // the page would have sent is rebuilt from them exactly as `runExport`
    // builds it.
    window.eval('openExportModal("requests")');
    await wait(120);
    out.export = {};
    for (const value of ["all", ...FIXED_PERIODS.filter(Boolean), "custom"]) {
      await pick("exportPeriod", value);
      if (value === "custom") {
        // Both boxes empty: the URL must carry neither bound, which is what
        // this dialog sent before RANGE existed.
        doc.getElementById("exportSince").value = "";
        doc.getElementById("exportUntil").value = "";
        doc.getElementById("exportSince").dispatchEvent(new window.Event("change"));
      }
      out.export[value] = window.eval("(() => { const p = exportParamsFor('requests'); const o = {}; for (const k of ['since','until']) { if (p.has(k)) o[k] = p.get(k); } return o; })()");
    }

    // ---- 3. the new shapes, read back from the URL ------------------------
    //
    // These only exist on the branch. On the base tree the scenario reports
    // `baseTree: true` and skips them, so one file serves both runs.
    const hasCustom = Boolean(doc.getElementById("reqWindowCustomWrap"));
    out.baseTree = !hasCustom;
    if (!hasCustom) {
      window.eval("closeExportModal()");
      return out;
    }

    /** The `{since, until}` the real control put in the URL. Reading it from
     *  the URL rather than from `windowParams()` is deliberate: it proves the
     *  helper's answer is the one that goes out, not merely that it computes. */
    async function toolbarWindow() {
      fetchUrls.length = 0;
      // The toolbar only loads on a change; nudge it through the select.
      doc.getElementById("reqFilterWindow").dispatchEvent(new window.Event("change", { bubbles: true }));
      await settle();
      const calls = takeUrls(ANALYTICS_PREFIXES);
      // The stats request is the one whose window is unambiguous: the list
      // paginates, the pulse drops `since` by design.
      const stats =
        calls.find((entry) => entry.path === "/admin/api/requests/stats") || calls[0] || {};
      return { since: stats.since ?? null, until: stats.until ?? null };
    }

    out.shapes = {};

    // -- relative ----------------------------------------------------------
    await pick("reqFilterWindow", "custom-relative");
    out.shapes.relativeHidden = {
      custom: hiddenOf("reqWindowCustomWrap"),
      range: hiddenOf("reqWindowRange"),
    };
    for (const [amount, unit, key] of [
      ["7", "days", "7 days"],
      ["3", "weeks", "3 weeks"],
      ["36", "hours", "36 hours"],
      ["1", "days", "1 day"],
    ]) {
      await type("reqWindowAmount", amount);
      await pick("reqWindowUnit", unit);
      out.shapes[`relative:${key}`] = await toolbarWindow();
    }
    out.shapes.relativeLabel = doc
      .querySelector("#view-requests .analytics-window")
      ? doc.querySelector("#view-requests .analytics-window").textContent
      : "";

    // -- calendar ----------------------------------------------------------
    await pick("reqFilterWindow", "custom-calendar");
    out.shapes.calendarHidden = {
      custom: hiddenOf("reqWindowCustomWrap"),
      range: hiddenOf("reqWindowRange"),
    };
    const shapeVisibility = async (shape) => {
      await pick("reqWindowShape", shape);
      return {
        month: hiddenOf("reqWindowMonthWrap"),
        from: hiddenOf("reqWindowFromWrap"),
        to: hiddenOf("reqWindowToWrap"),
        year: hiddenOf("reqWindowYearWrap"),
        exact: hiddenOf("reqWindowExactWrap"),
      };
    };
    out.shapes.shapeVisibility = {
      month: await shapeVisibility("month"),
      months: await shapeVisibility("months"),
      year: await shapeVisibility("year"),
      exact: await shapeVisibility("exact"),
    };

    await pick("reqWindowShape", "month");
    for (const value of ["2026-10", "2026-11", "2026-09", "2026-03", "2026-01", "2026-12", "2028-02"]) {
      await type("reqWindowMonth", value);
      out.shapes[`month:${value}`] = await toolbarWindow();
    }
    out.shapes.monthLabel = doc
      .querySelector("#view-requests .analytics-window")
      ? doc.querySelector("#view-requests .analytics-window").textContent
      : "";

    await pick("reqWindowShape", "months");
    for (const [from, to] of [
      ["2026-09", "2026-11"],
      ["2026-10", "2026-10"],
      ["2026-01", "2026-12"],
    ]) {
      await type("reqWindowFrom", from);
      await type("reqWindowTo", to);
      out.shapes[`months:${from}..${to}`] = await toolbarWindow();
    }
    // A backwards range is an unfinished pick: no bound at all, never a query
    // whose `since` is after its `until`.
    await type("reqWindowFrom", "2026-11");
    await type("reqWindowTo", "2026-09");
    out.shapes["months:backwards"] = await toolbarWindow();

    await pick("reqWindowShape", "year");
    for (const value of ["2026", "2024", "2028"]) {
      await type("reqWindowYear", value);
      out.shapes[`year:${value}`] = await toolbarWindow();
    }
    out.shapes.yearLabel = doc
      .querySelector("#view-requests .analytics-window")
      ? doc.querySelector("#view-requests .analytics-window").textContent
      : "";

    await pick("reqWindowShape", "exact");
    await type("reqWindowFromTime", "2026-10-01T00:00");
    await type("reqWindowUntilTime", "2026-11-01T00:00");
    out.shapes["exact:october"] = await toolbarWindow();

    // The three sentences of PR-CW-2, read from the product's own functions.
    //
    // Sentence 1, the hour-snap gap: the payload's requested and snapped
    // bounds, exactly as `core/request_log.py:10889-10896` sends them, with
    // the page's own exact total beside it (`reqState.total`) -- which is what
    // lets the clause name a number rather than an hour count.
    const r = window.rangeHarness();
    const octoberStats = {
      window: {
        since: 1790802000,
        until: 1793484000,
        snapped_since: 1790802000,
        // The rollup rounds the end up to the end of its UTC hour.
        snapped_until: 1793487599,
      },
      total: 212317,
    };
    const savedTotal = r.reqState.total;
    r.reqState.total = 211992; // the table's exact count for the same window
    out.hourSnap = {
      october: r.hourSnapNote(octoberStats),
      // Two hours of overshoot says "hours", not "1 hour".
      twoHours: r.hourSnapNote({
        window: { ...octoberStats.window, snapped_until: 1793491199 },
        total: 212317,
      }),
      // A window the rollup did not widen says nothing.
      exact: r.hourSnapNote({
        window: { ...octoberStats.window, snapped_until: 1793484000 },
        total: 212317,
      }),
      // All time carries no bounds and says nothing.
      allTime: r.hourSnapNote({ window: { since: null, until: null } }),
      noWindow: r.hourSnapNote({}),
    };
    // Sentence 2, the retention gap: the all-time counters against the rows.
    const savedLifetimeForGap = r.reqState.lastLifetime;
    r.reqState.lastLifetime = { requests: 738165 };
    out.retention = {
      // The measured copy: 700,005 stored rows against 738,165 rolled.
      outrun: r.lifetimeOutrunsRows({ total: 700005 }),
      // The two agree: no sentence.
      agrees: r.lifetimeOutrunsRows({ total: 738165 }),
      // A handful of rows is a race between two requests, not a sentence.
      tiny: r.lifetimeOutrunsRows({ total: 738160 }),
      // And with no all-time payload there is nothing to compare.
      noLifetime: (() => {
        r.reqState.lastLifetime = null;
        return r.lifetimeOutrunsRows({ total: 1 });
      })(),
    };
    r.reqState.lastLifetime = savedLifetimeForGap;
    r.reqState.total = savedTotal;

    // Sentence 3, the empty custom window. A window far before any stored row
    // must say it is unmeasured rather than that it is empty. The harness's
    // fixture answers every list call with rows, so the empty TABLE is the
    // product's own row renderer with an empty list -- `renderRequestsTable`
    // is the function that decides the words, and it is the one driven here.
    await pick("reqWindowShape", "year");
    await type("reqWindowYear", "1990");
    await settle();
    await settle();
    window.eval("renderRequestsTable([])");
    out.shapes.emptyYearText = window.eval(
      'document.querySelector("#reqTableBody .analytics-empty")?.textContent || ""',
    );
    // The other "not measured": a window that ends before uptime tracking
    // began. Same words, reached through the coverage branch instead of the
    // retention one -- 2020 ends before a log that starts tracking in 2026.
    const harness = window.rangeHarness();
    const savedStats = harness.reqState.lastStats;
    const savedLifetime = harness.reqState.lastLifetime;
    const savedTotal2 = harness.reqState.total;
    await type("reqWindowYear", "2020");
    await settle();
    harness.reqState.lastStats = { coverage: { tracking_since: 1786000000 } };
    harness.reqState.lastLifetime = null;
    harness.reqState.total = 0;
    out.shapes.emptyCoverageBranch = window.eval("emptyTableText()");
    harness.reqState.lastStats = savedStats;
    harness.reqState.lastLifetime = savedLifetime;
    harness.reqState.total = savedTotal2;
    await type("reqWindowYear", "1990");
    await settle();
    // And the same table with the window on "all time" keeps the old words, so
    // the new sentence is the custom window's and not everyone's.
    await pick("reqFilterWindow", "");
    await wait(150);
    out.shapes.emptyAllTimeText = window.eval(
      '(renderRequestsTable([]), document.querySelector("#reqTableBody .analytics-empty")?.textContent || "")',
    );

    // ---- 4. the wide-search warning (PR-CW-3) -----------------------------
    //
    // The line under the box must name the measured cost on a wide window and
    // say nothing on a narrow one -- and the search must still be a search:
    // the box keeps its text and pressing Enter still sends `q`.
    await pick("reqFilterWindow", "2592000");
    await type("reqFilterSearch", "needle");
    out.cost30d = (() => {
      const el = doc.getElementById("reqSearchCost");
      return el && !el.hidden ? el.textContent : "";
    })();
    await pick("reqFilterWindow", "");
    await type("reqFilterSearch", "needle");
    out.costAllTime = (() => {
      const el = doc.getElementById("reqSearchCost");
      return el && !el.hidden ? el.textContent : "";
    })();
    await pick("reqFilterWindow", "3600");
    await type("reqFilterSearch", "needle");
    out.costHour = (() => {
      const el = doc.getElementById("reqSearchCost");
      return el && !el.hidden ? el.textContent : "";
    })();
    // And it is advice, not a gate: Enter still puts `q` in the URL.
    fetchUrls.length = 0;
    doc
      .getElementById("reqFilterSearch")
      .dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await settle();
    const searchCalls = takeUrls(ANALYTICS_PREFIXES);
    out.searchStillRuns = searchCalls.length > 0;
    // The search is still the whole window's: a 30-day window's search URL
    // still carries the same `since` its load did, so nothing was narrowed.
    out.searchWindowMatchesLoad =
      searchCalls.some((entry) => entry.since) && !searchCalls.some((entry) => entry.until);

    // A held search (restored from a saved view, waiting for Run) has its own
    // notice and runs nothing, so the cost line must not fire on it either --
    // it would be pricing a search that is not being paid for. Reached through
    // the page's own handle: `reqState` is a script-level const and an
    // indirect eval cannot see it.
    const range = window.rangeHarness();
    range.reqState.searchHeld = true;
    window.eval("renderReqSearchNotice()");
    out.costWhenHeld = (() => {
      const el = doc.getElementById("reqSearchCost");
      return el && !el.hidden ? el.textContent : "";
    })();
    range.reqState.searchHeld = false;
    // Back to a window that genuinely costs something before asking again: the
    // hour the sequence left behind is the case where SILENCE is correct, so
    // asserting it speaks here would be asserting the wrong thing.
    await pick("reqFilterWindow", "2592000");
    window.eval("renderReqSearchNotice()");
    out.costAfterHeldCleared = (() => {
      const el = doc.getElementById("reqSearchCost");
      return el && !el.hidden ? el.textContent : "";
    })();

    // ---- 5. a saved view keeps the shape, not an epoch --------------------
    //
    // Pick a rolling window, save the view the way the page saves it, and read
    // back what was written. The stored object must name the SHAPE and its
    // inputs; a resolved epoch in localStorage would make "last 7 days" a
    // snapshot that reads as broken a week later.
    await pick("reqFilterWindow", "custom-relative");
    await type("reqWindowAmount", "14");
    await pick("reqWindowUnit", "days");
    window.eval("persistDashboardState()");
    out.savedRelative = JSON.parse(
      window.localStorage.getItem("mcc-dashboard-state") || "{}",
    ).reqFilters;

    // An absolute one stores its own inputs, and restores to the same window.
    await pick("reqFilterWindow", "custom-calendar");
    await pick("reqWindowShape", "month");
    await type("reqWindowMonth", "2026-10");
    window.eval("persistDashboardState()");
    out.savedCalendar = JSON.parse(
      window.localStorage.getItem("mcc-dashboard-state") || "{}",
    ).reqFilters;
    // Restoring is the same code at start-up and under test.
    const saved = out.savedCalendar;
    for (const id of [
      "reqFilterWindow",
      "reqWindowShape",
      "reqWindowMonth",
      "reqWindowAmount",
      "reqWindowUnit",
    ]) {
      const el = doc.getElementById(id);
      if (el) el.value = "";
    }
    window.eval(`restoreReqFilters(${JSON.stringify(saved)})`);
    await settle();
    out.restored = {
      window: doc.getElementById("reqFilterWindow").value,
      shape: doc.getElementById("reqWindowShape").value,
      month: doc.getElementById("reqWindowMonth").value,
      rangeHidden: hiddenOf("reqWindowRange"),
      // And the window the restored control resolves to is the same pair.
      params: await toolbarWindow(),
    };

    // A saved view written BEFORE this release has no `windowCustom` key at
    // all. It must restore exactly as it did, with no exception and no blank
    // select (the SELGUARD class of defect this project has already paid for).
    window.eval(
      `restoreReqFilters(${JSON.stringify({
        provider: "nous_portal",
        window: "604800",
      })})`,
    );
    await settle();
    out.restoredPreRange = {
      window: doc.getElementById("reqFilterWindow").value,
      provider: doc.getElementById("reqFilterProvider").value,
      customHidden: hiddenOf("reqWindowCustomWrap"),
      rangeHidden: hiddenOf("reqWindowRange"),
      params: await toolbarWindow(),
    };

    window.eval("closeExportModal()");
    return out;
  }

  return { run };
}
