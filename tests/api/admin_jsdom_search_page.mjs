/**
 * The Requests page's half of the search stopgap (7.91.2), run instead of the
 * main capture when MCC_JSDOM_SCENARIO=search_page.
 *
 * Since 7.91.1 the server reads a free-text search once, newest first, and
 * drops a search no request waits for any more. The page half, the user's
 * decisions of 2026-10-10 (Q1, Q2, Q11, Q12), is what this drives:
 *
 *  - a search saved from an earlier visit is put back in the box but NOT run
 *    at start-up: a notice offers Run and Clear, and no request carries it
 *    until Enter or Run;
 *  - while a search's one pass runs, a progress line says how far it has read
 *    where the pager said "counting…"; the list fills first, and the count and
 *    the cards land together when the pass ends;
 *  - search-as-you-type stays at 400 ms, and every request a load makes with
 *    the search is aborted by the next load (AbortController), which is how
 *    the server learns to stop the superseded searches;
 *  - auto-refresh stays on: no pulse while the pass runs (it would wait for
 *    that same pass), and after it only the cheap pulse and table re-read;
 *  - the export still carries the search.
 *
 * Every timer of the page runs on a manual clock, so a 15 s auto-refresh
 * interval and a one-second progress poll are measured exactly. The fetch
 * layer plays the server for the search routes: requests that wait for a pass
 * stay open until the scenario ends that pass, and a request whose signal is
 * aborted ends "aborted", the way a browser's does. Everything else is the
 * main harness's own payload. Every request is recorded with its FULL url.
 *
 * Prints nothing itself: `run()` returns the measurements and the harness
 * prints them as one JSON object.
 */

// 2026-10-01 09:00:00 UTC, as the pause scenario. Fixed, so every date the
// page prints is the same on every run.
const START = Date.UTC(2026, 9, 1, 9, 0, 0);
// What the progress line reads back to: 2026-08-12, mid-day UTC.
const BACK_TO = Date.UTC(2026, 7, 12, 12, 0, 0) / 1000;

class ManualClock {
  constructor(start) {
    this.start = start;
    this.now = start;
    this.seq = 0;
    this.timers = new Map();
    this.errors = [];
  }

  install(win) {
    const add = (fn, ms, args, repeat) => {
      const id = ++this.seq;
      const delay = Math.max(0, Number(ms) || 0);
      this.timers.set(id, {
        at: this.now + delay,
        fn,
        args,
        every: repeat ? Math.max(1, delay) : 0,
      });
      return id;
    };
    win.setTimeout = (fn, ms, ...args) => add(fn, ms, args, false);
    win.setInterval = (fn, ms, ...args) => add(fn, ms, args, true);
    win.clearTimeout = (id) => {
      this.timers.delete(id);
    };
    win.clearInterval = (id) => {
      this.timers.delete(id);
    };
    win.__scenarioNow = () => this.now;
    win.eval("Date.now = function () { return window.__scenarioNow(); };");
    win.performance.now = () => this.now - this.start;
  }

  elapsed() {
    return this.now - this.start;
  }

  async flush() {
    for (let i = 0; i < 6; i += 1) await new Promise((resolve) => setImmediate(resolve));
  }

  /** Move the clock on by `ms`, firing every timer that falls due, in order. */
  async advance(ms) {
    const end = this.now + ms;
    for (let fired = 0; fired < 200000; fired += 1) {
      await this.flush();
      let next = null;
      for (const [id, timer] of this.timers) {
        if (timer.at > end) continue;
        if (!next || timer.at < next.timer.at || (timer.at === next.timer.at && id < next.id)) {
          next = { id, timer };
        }
      }
      if (!next) break;
      this.now = Math.max(this.now, next.timer.at);
      if (next.timer.every) next.timer.at += next.timer.every;
      else this.timers.delete(next.id);
      try {
        if (typeof next.timer.fn === "function") next.timer.fn(...next.timer.args);
      } catch (error) {
        this.errors.push(String((error && error.stack) || error));
      }
    }
    this.now = end;
    await this.flush();
  }
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

const clone = (value) => (value === undefined ? value : JSON.parse(JSON.stringify(value)));

// The routes whose answer waits for the search's one pass to end (7.91.1).
const WAITS_FOR_PASS = new Set([
  "/admin/api/requests/count",
  "/admin/api/requests/stats",
  "/admin/api/requests/cost",
  "/admin/api/requests/ttft",
  "/admin/api/requests/no-answer",
  "/admin/api/requests/origin",
]);

export function prepareSearchPageScenario({ window, ROUTES }) {
  const clock = new ManualClock(START);
  const LOG = [];
  const SERVER = {
    // q -> { done, progress, listHold }
    passes: new Map(),
    // The answer every pass-bound count gives once its pass ends.
    countTotal: 7,
    pulseTotal: 480,
    pulseTs: START / 1000 - 30,
    // Routes answered without waiting even with a search (the list's own
    // early answer is the default; this holds it too, to see it aborted).
    holdList: false,
  };
  const passFor = (q) => {
    if (!SERVER.passes.has(q)) {
      SERVER.passes.set(q, { done: deferred(), ended: false, progress: null, listHold: null });
    }
    return SERVER.passes.get(q);
  };
  const endPass = (q) => {
    const pass = passFor(q);
    pass.ended = true;
    pass.done.resolve();
  };

  // The page arrives at the Requests view with a search saved from an
  // earlier visit: an all-time "too long", the 2026-10-09 stall's search.
  // Written before admin.js runs, as a browser would hold it.
  const saved = {
    activeView: "requests",
    autoRefresh: true,
    autoRefreshInterval: "15000",
    reqFilters: { search: "too long", local: "hide" },
  };
  window.localStorage.setItem("mcc-dashboard-state", JSON.stringify(saved));

  // The shape the real route answers with. The main payload's `{configured:
  // false}` makes the last step of load() throw, so the steps after it -- the
  // ones that arm the polls -- would never run here, as they do in a browser.
  ROUTES["/admin/api/claude-settings"] = {
    status: { state: "unset" },
    targets: [],
    default_path: "",
  };

  // AbortController.abort, counted: the claim is that the page calls it.
  const abortCalls = [];
  const realAbort = window.AbortController.prototype.abort;
  window.AbortController.prototype.abort = function countedAbort(...args) {
    abortCalls.push(clock.elapsed());
    return realAbort.apply(this, args);
  };

  const respond = (status, data) => ({
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Error",
    headers: { get: () => null },
    json: async () => clone(data),
    text: async () => JSON.stringify(data),
    blob: async () => ({ size: JSON.stringify(data).length }),
  });

  const aborted = (signal) =>
    new Promise((_resolve, reject) => {
      if (!signal) return;
      const fail = () => {
        const error = new Error("The operation was aborted.");
        error.name = "AbortError";
        reject(error);
      };
      if (signal.aborted) fail();
      else signal.addEventListener("abort", fail);
    });

  const original = window.fetch;

  async function answer(url, path, options) {
    const params = new URL(url, "http://127.0.0.1").searchParams;
    const q = params.get("q");
    if (path === "/admin/api/requests/pulse") {
      return respond(200, {
        enabled: true,
        total: SERVER.pulseTotal,
        last_ts: SERVER.pulseTs,
        in_flight: 0,
      });
    }
    if (path === "/admin/api/export") {
      return respond(200, { exported: true });
    }
    if (q) {
      const pass = passFor(q);
      if (path === "/admin/api/requests/search-progress") {
        return respond(
          200,
          pass.progress
            ? { enabled: true, searching: true, ...pass.progress }
            : { enabled: true, searching: false },
        );
      }
      if (path === "/admin/api/requests") {
        // A page of rows is answered while the pass is still reading.
        if (SERVER.holdList && !pass.ended) await pass.done.promise;
        return respond(200, {
          ...clone(ROUTES["/admin/api/requests"]),
          total: null,
          total_deferred: true,
          has_more: true,
        });
      }
      if (WAITS_FOR_PASS.has(path)) {
        await pass.done.promise;
        if (path === "/admin/api/requests/count") {
          return respond(200, { enabled: true, total: SERVER.countTotal });
        }
        if (pass.holdAfter && pass.holdAfter.has(path)) {
          await pass.holdAfter.get(path).promise;
        }
      }
    }
    const response = await original(url, options);
    const data = await response.json();
    return respond(response.status || 200, data);
  }

  async function fetchStub(url, options = {}) {
    const full = String(url);
    const path = full.split("?")[0];
    const entry = {
      t: clock.elapsed(),
      url: full,
      path,
      q: new URL(full, "http://127.0.0.1").searchParams.get("q"),
      signal: Boolean(options && options.signal),
      end: null,
      outcome: "pending",
    };
    LOG.push(entry);
    try {
      const response = await Promise.race([
        answer(full, path, options),
        aborted(options && options.signal),
      ]);
      entry.outcome = response.ok ? "ok" : `http ${response.status}`;
      return response;
    } catch (error) {
      entry.outcome = error && error.name === "AbortError" ? "aborted" : "error";
      throw error;
    } finally {
      entry.end = clock.elapsed();
    }
  }

  clock.install(window);
  window.fetch = fetchStub;

  /* ------------------------------------------------------------- reading */
  const doc = window.document;
  const text = (id) => {
    const node = doc.getElementById(id);
    return node ? node.textContent.replace(/\s+/g, " ").trim() : null;
  };
  const shown = (id) => {
    const node = doc.getElementById(id);
    if (!node) return false;
    for (let el = node; el; el = el.parentElement) {
      if (el.hidden) return false;
    }
    return true;
  };
  const since = (mark) => LOG.slice(mark);
  const withSearch = (entries) => entries.filter((entry) => entry.q);
  const brief = (entries) =>
    entries.map((entry) => ({
      t: entry.t,
      end: entry.end,
      url: entry.url,
      path: entry.path,
      q: entry.q,
      signal: entry.signal,
      outcome: entry.outcome,
    }));
  const persisted = () =>
    JSON.parse(window.localStorage.getItem("mcc-dashboard-state") || "{}").reqFilters || {};
  const countingCards = () =>
    Array.from(doc.querySelectorAll("#reqStatsCards .requests-card strong")).filter((el) =>
      el.textContent.startsWith("counting"),
    ).length;
  const cards = () => doc.querySelectorAll("#reqStatsCards .requests-card").length;
  const tableRows = () => doc.querySelectorAll("#reqTableBody tr").length;
  // A missing control is recorded, not thrown: the run then reports what the
  // page did without it (how a page without the change is told apart).
  const missing = [];
  const click = (id) => {
    const node = doc.getElementById(id);
    if (!node) {
      missing.push(id);
      return;
    }
    node.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  };
  const type = (value) => {
    const box = doc.getElementById("reqFilterSearch");
    box.value = value;
    box.dispatchEvent(new window.Event("input", { bubbles: true }));
  };
  const enter = () =>
    doc
      .getElementById("reqFilterSearch")
      .dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  // What the notice says on screen: its text, and the actions only when shown
  // (textContent would read a hidden child too).
  const noticeText = () => {
    if (!shown("reqSearchNotice")) return "";
    const actions = shown("reqSearchNoticeActions") ? ` ${text("reqSearchNoticeActions")}` : "";
    return `${text("reqSearchNoticeText")}${actions}`.trim();
  };
  const snapshot = () => ({
    box: doc.getElementById("reqFilterSearch").value,
    noticeShown: shown("reqSearchNotice"),
    notice: noticeText(),
    actionsShown: shown("reqSearchNoticeActions"),
    pager: text("reqPageInfo"),
    countingCards: countingCards(),
    cards: cards(),
    tableRows: tableRows(),
    persistedSearch: persisted().search || null,
  });

  async function run({ scriptErrors, consoleErrors }) {
    const out = {};

    /* ----------------------------------- 1. start-up with a saved search */
    await clock.advance(2500);
    out.activeView = doc.querySelector(".nav-link.active")
      ? doc.querySelector(".nav-link.active").dataset.view
      : null;
    out.startup = snapshot();
    out.startupRequests = brief(since(0)).filter((entry) =>
      entry.path.startsWith("/admin/api/requests"),
    );
    out.startupSearchRequests = brief(withSearch(since(0)));

    // A minute of auto-refresh, and the search is still only in the box.
    let mark = LOG.length;
    await clock.advance(60000);
    out.idleMinute = {
      pulses: since(mark).filter((entry) => entry.path === "/admin/api/requests/pulse").length,
      searchRequests: brief(withSearch(since(mark))),
      after: snapshot(),
    };

    // The export carries the box's search even while it waits for Run.
    mark = LOG.length;
    window.eval('openExportModal("requests")');
    try {
      await window.eval("runExport()");
    } catch {
      /* only the URL is the claim */
    }
    window.eval("closeExportModal()");
    await clock.advance(100);
    out.heldExportUrl =
      (since(mark).find((entry) => entry.path === "/admin/api/export") || {}).url || "";

    /* -------------------------------------------- 2. Run starts the pass */
    mark = LOG.length;
    click("reqSearchRun");
    await clock.advance(50);
    const runRequests = since(mark);
    out.run = {
      requests: brief(runRequests),
      afterClick: snapshot(),
    };
    // The list answered, the count and the cards wait for the pass.
    out.run.listFilledFirst = {
      tableRows: tableRows(),
      countingCards: countingCards(),
      countOpen: runRequests.some(
        (entry) => entry.path === "/admin/api/requests/count" && entry.outcome === "pending",
      ),
    };

    // The pass reads: the progress line follows it, once a second.
    const pass = passFor("too long");
    pass.progress = {
      finished: false,
      matched: 1900,
      read: 100000,
      total: 598683,
      searched_back_to: BACK_TO + 30 * 86400,
    };
    mark = LOG.length;
    await clock.advance(1000);
    out.progress1 = snapshot();
    pass.progress = {
      finished: false,
      matched: 7400,
      read: 412000,
      total: 598683,
      searched_back_to: BACK_TO,
    };
    await clock.advance(1000);
    out.progress2 = snapshot();
    out.progressRequests = brief(
      since(mark).filter((entry) => entry.path === "/admin/api/requests/search-progress"),
    );

    // Auto-refresh during the pass: two whole intervals, and no pulse.
    mark = LOG.length;
    await clock.advance(31000);
    out.duringPass = {
      pulses: brief(since(mark).filter((entry) => entry.path === "/admin/api/requests/pulse")),
      countsOrStats: brief(
        since(mark).filter(
          (entry) =>
            entry.path === "/admin/api/requests/count" ||
            entry.path === "/admin/api/requests/stats",
        ),
      ),
      progressPolls: since(mark).filter(
        (entry) => entry.path === "/admin/api/requests/search-progress",
      ).length,
      after: snapshot(),
    };

    /* ------------------------- 3. the pass ends: count and cards together */
    // The stats answer is held a moment past the count: nothing lands alone.
    pass.holdAfter = new Map([["/admin/api/requests/stats", deferred()]]);
    const endMark = LOG.length;
    endPass("too long");
    await clock.advance(50);
    out.countAnsweredStatsNot = snapshot();
    pass.holdAfter.get("/admin/api/requests/stats").resolve();
    await clock.advance(50);
    out.passEnded = snapshot();
    mark = LOG.length;
    await clock.advance(3000);
    out.afterPassProgressPolls = since(mark).filter(
      (entry) => entry.path === "/admin/api/requests/search-progress",
    ).length;
    // The four panels the pass also answers ask once it has ended.
    out.panelsAfterPass = brief(
      since(endMark).filter((entry) =>
        [
          "/admin/api/requests/cost",
          "/admin/api/requests/ttft",
          "/admin/api/requests/no-answer",
          "/admin/api/requests/origin",
        ].includes(entry.path),
      ),
    );

    /* -------------------- 4. auto-refresh after the pass: new rows only */
    mark = LOG.length;
    await clock.advance(15000);
    const baseline = since(mark);
    SERVER.pulseTotal += 3;
    SERVER.pulseTs = clock.now / 1000;
    await clock.advance(15000);
    const changed = since(mark).slice(baseline.length);
    out.autoAfterPass = {
      baseline: brief(baseline),
      changed: brief(changed),
    };

    /* ------------------------- 5. typing three prefixes, 400 ms apart */
    SERVER.holdList = true;
    mark = LOG.length;
    const abortsBefore = abortCalls.length;
    const loads = [];
    for (const prefix of ["zq", "zqx", "zqxj"]) {
      const at = LOG.length;
      type(prefix);
      await clock.advance(400);
      loads.push({ prefix, from: at });
    }
    await clock.advance(50);
    out.typing = {
      abortCalls: abortCalls.length - abortsBefore,
      loads: loads.map((load, index) => {
        const until = index + 1 < loads.length ? loads[index + 1].from : LOG.length;
        return {
          prefix: load.prefix,
          requests: brief(LOG.slice(load.from, until).filter((entry) => entry.q === load.prefix)),
        };
      }),
      progressLine: snapshot(),
    };
    // A second on, its first page still not found: the table says so instead
    // of showing the last view's rows.
    await clock.advance(1100);
    const firstCell = doc.querySelector("#reqTableBody tr td");
    out.typing.searchingTable = {
      rows: tableRows(),
      firstCell: firstCell ? firstCell.textContent : null,
      pager: text("reqPageInfo"),
    };
    // The last prefix's pass ends; it is the only one answered.
    endPass("zqxj");
    await clock.advance(100);
    out.typing.lastLoadAfterPass = brief(
      since(mark).filter((entry) => entry.q === "zqxj"),
    );
    out.typing.afterPass = snapshot();
    SERVER.holdList = false;

    /* --------------- 6. Clear and Enter on a saved search, export after Run */
    // The start-up restore is `restoreReqFilters`, run again here exactly as
    // `loadDashboardState` runs it, so the box holds a saved search again.
    window.eval(
      'restoreReqFilters({ search: "too long", local: "hide", window: "604800" })',
    );
    await clock.advance(50);
    out.restoredAgain = snapshot();
    mark = LOG.length;
    click("reqSearchClear");
    await clock.advance(500);
    out.clear = {
      after: snapshot(),
      requests: brief(since(mark).filter((entry) => entry.path.startsWith("/admin/api/requests"))),
    };

    window.eval('restoreReqFilters({ search: "too long", local: "hide" })');
    await clock.advance(50);
    mark = LOG.length;
    enter();
    await clock.advance(50);
    out.enter = {
      searchRequests: brief(withSearch(since(mark))),
      after: snapshot(),
    };
    endPass("too long");
    await clock.advance(100);

    mark = LOG.length;
    window.eval('openExportModal("requests")');
    try {
      await window.eval("runExport()");
    } catch {
      /* only the URL is the claim */
    }
    window.eval("closeExportModal()");
    await clock.advance(100);
    out.runExportUrl =
      (since(mark).find((entry) => entry.path === "/admin/api/export") || {}).url || "";

    out.missingControls = missing;
    out.scriptErrors = [...scriptErrors];
    out.consoleErrors = [...consoleErrors];
    out.clockErrors = clock.errors;
    out.totalRequests = LOG.length;
    out.clockMs = clock.elapsed();
    return out;
  }

  return { run };
}
