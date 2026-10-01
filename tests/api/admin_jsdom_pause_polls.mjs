/**
 * The pause-button and dashboard-polling scenario (7.69.4), run instead of the
 * main capture when MCC_JSDOM_SCENARIO=pause_polls.
 *
 * Why its own run: everything here is about *time* -- what a row shows during
 * the five seconds after a Save, how often a poll fires, whether two polls
 * overlap, how long a second tab waits before taking over -- and the main run
 * uses the real clock, where "every 3 s for a minute" costs a minute and a
 * loaded CI runner moves every number. Here every timer of the page runs on a
 * manual clock that only moves when the scenario says so, so a minute of
 * polling is measured exactly and in milliseconds of wall time.
 *
 * What it drives is the real admin.js, against the main harness's payload,
 * through a fetch layer that behaves like the server for the routes under
 * test: the config's pause lists and the route-pause write share one store,
 * in-flight rows finish and the pulse counts them, and any route can be held
 * open, failed, or marked busy (`x-mcc-busy: 1`). Every request either tab
 * makes is recorded with its FULL url, method, body, clock time, how many of
 * the same route that tab had out at once, and how it ended.
 *
 * Two tabs are two JSDOM windows sharing the clock, the server and a
 * BroadcastChannel stand-in (jsdom has none); a tab can be hidden, shown, or
 * killed outright (its timers and its channel go silent, as a crashed tab's
 * would).
 *
 * Prints nothing itself: `run()` returns the measurements and the harness
 * prints them as one JSON object.
 */
import { JSDOM, VirtualConsole } from "jsdom";

// 2026-10-01 09:00:00 UTC. Fixed, so a clock reading is the same on every run.
const START = Date.UTC(2026, 9, 1, 9, 0, 0);

class ManualClock {
  constructor(start) {
    this.start = start;
    this.now = start;
    this.seq = 0;
    this.timers = new Map();
    this.dead = new Set();
    this.errors = [];
  }

  install(win, owner) {
    const add = (fn, ms, args, repeat) => {
      const id = ++this.seq;
      const delay = Math.max(0, Number(ms) || 0);
      this.timers.set(id, {
        at: this.now + delay,
        fn,
        args,
        every: repeat ? Math.max(1, delay) : 0,
        owner,
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

  /** A tab that died: none of its timers ever fires again. */
  kill(owner) {
    this.dead.add(owner);
    for (const [id, timer] of this.timers) {
      if (timer.owner === owner) this.timers.delete(id);
    }
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
      if (this.dead.has(next.timer.owner)) continue;
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

/** BroadcastChannel for JSDOM windows in one process. Delivery is async (a
 *  microtask), structured-cloned, never to the sender, and never to or from a
 *  tab marked dead. */
class ChannelBus {
  constructor(clock) {
    this.clock = clock;
    this.members = [];
    this.dead = new Set();
    this.posted = [];
  }

  classFor(owner) {
    const bus = this;
    return class FakeBroadcastChannel {
      constructor(name) {
        this.name = String(name);
        this.owner = owner;
        this.onmessage = null;
        this.listeners = [];
        this.closed = false;
        bus.members.push(this);
      }

      postMessage(data) {
        if (this.closed || bus.dead.has(owner)) return;
        const copy = structuredClone(data);
        bus.posted.push({ from: owner, type: copy && copy.type, t: bus.clock.elapsed() });
        for (const member of bus.members) {
          if (member === this || member.closed || member.name !== this.name) continue;
          if (bus.dead.has(member.owner)) continue;
          queueMicrotask(() => member.deliver(structuredClone(copy)));
        }
      }

      deliver(data) {
        if (this.closed || bus.dead.has(this.owner)) return;
        const event = { data };
        if (typeof this.onmessage === "function") this.onmessage(event);
        this.listeners.forEach((listener) => listener(event));
      }

      addEventListener(type, listener) {
        if (type === "message") this.listeners.push(listener);
      }

      removeEventListener(type, listener) {
        this.listeners = this.listeners.filter((entry) => entry !== listener);
      }

      close() {
        this.closed = true;
      }
    };
  }
}

/** The stubs the main harness puts on its window, for a second window. */
function prepareTabWindow(win) {
  class NoopObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
    takeRecords() {
      return [];
    }
  }
  win.IntersectionObserver = NoopObserver;
  win.ResizeObserver = NoopObserver;
  win.MutationObserver = win.MutationObserver || NoopObserver;
  const context = new Proxy(
    {},
    {
      get(_target, prop) {
        if (prop === "measureText") return () => ({ width: 0 });
        if (prop === "createLinearGradient" || prop === "createRadialGradient") {
          return () => ({ addColorStop() {} });
        }
        return () => {};
      },
      set() {
        return true;
      },
    },
  );
  win.HTMLCanvasElement.prototype.getContext = () => context;
  win.scrollTo = () => {};
  if (!win.navigator.clipboard) {
    Object.defineProperty(win.navigator, "clipboard", {
      configurable: true,
      value: { writeText: () => Promise.resolve() },
    });
  }
  win.matchMedia =
    win.matchMedia ||
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
  win.requestAnimationFrame = (fn) => win.setTimeout(() => fn(Date.now()), 0);
  win.cancelAnimationFrame = (id) => win.clearTimeout(id);
  win.Element.prototype.scrollIntoView = function scrollIntoViewStub() {};
}

/** `document.visibilityState` under the scenario's control. */
function installVisibility(win) {
  const tab = { hidden: false };
  Object.defineProperty(win.document, "visibilityState", {
    configurable: true,
    get: () => (tab.hidden ? "hidden" : "visible"),
  });
  Object.defineProperty(win.document, "hidden", {
    configurable: true,
    get: () => tab.hidden,
  });
  return (hidden) => {
    tab.hidden = hidden;
    win.document.dispatchEvent(new win.Event("visibilitychange"));
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

const clone = (value) => (value === undefined ? value : JSON.parse(JSON.stringify(value)));

export function preparePausePollScenario({ window, html, script, ROUTES }) {
  const clock = new ManualClock(START);
  const bus = new ChannelBus(clock);
  const LOG = [];
  const SERVER = {
    paused: new Map([
      ["MODEL_FABLE_PAUSED", ["p1/f0-sol", "p1/f1-muse"]],
    ]),
    holds: new Map(),
    fail: new Map(),
    busy: false,
    rows: [],
    nextRow: 1,
    finished: 0,
    lastTs: null,
  };

  /* ---------------------------------------------------------- the payload
     A Fable rail of three rows, the top two paused (the 2026-09-24 shape:
     the paused gpt-*-sol head, a paused muse, live bunny), and one coding
     agent with its own Sonnet-tier rail -- the rows every guard below walks. */
  const config = ROUTES["/admin/api/config"];
  const field = (key) => config.fields.find((entry) => entry.key === key);
  field("MODEL_FABLE").value = "p1/f0-sol";
  field("MODEL_FABLE_FALLBACKS").value = "p1/f1-muse,p1/f2-bunny";
  ROUTES["/admin/api/config/apply"] = {
    applied: true,
    restart: {},
    pending_fields: [],
    warnings: [],
  };
  // The shape the real route answers with. The main payload's `{configured:
  // false}` makes the last step of load() throw, so the steps after it -- the
  // ones that arm the polls -- would never run here, as they do in a browser.
  ROUTES["/admin/api/claude-settings"] = {
    status: { state: "unset" },
    targets: [],
    default_path: "",
  };
  const tiers = ROUTES["/admin/api/harness-tiers"];
  const medium = tiers.tiers.find((tier) => tier.id === "medium");
  tiers.harnesses.codex = {
    medium: {
      override: true,
      model: "p1/a0",
      fallbacks: ["p1/a1", "p1/a2"],
      paused: ["p1/a0"],
      resolved: {
        primary: "p1/a0",
        fallbacks: ["p1/a1", "p1/a2"],
        paused: ["p1/a0"],
        paused_label: "harness_tiers.json:codex.medium.paused",
        source: "override",
      },
    },
  };
  void medium;

  const configPayload = () => {
    const payload = clone(config);
    payload.fields.forEach((entry) => {
      if (entry.key.endsWith("_PAUSED")) {
        entry.value = (SERVER.paused.get(entry.key) || []).join(",");
      }
    });
    return payload;
  };

  const row = (n) => ({
    id: `req_${String(n).padStart(4, "0")}`,
    started_at: START / 1000 + n,
    started_at_mono: 1000 + n,
    elapsed_ms: 2000,
    endpoint: "/v1/messages",
    protocol: "anthropic",
    stream: true,
    harness: "claude",
    requested_model: "claude-fable-5.1",
    tier: "fable",
    tier_source: "model",
    attempt_index: 0,
    provider: "p1",
    model_ref: "p1/f2-bunny",
    phase: "streaming",
    phase_since: 1000 + n,
    phase_elapsed_ms: 1000,
    describe_hops: 0,
    observed: true,
    ttft_ms: 300,
    first_content_ms: 300,
    output_chars: 10,
    thinking_chars: 0,
    chunks_to_client: 2,
    last_chunk_age_s: 0.5,
    waited_s: 0,
    attempt_tries: 1,
    last_try_status: null,
    last_try_error_kind: null,
    key_label: "fake…key1",
    proxy_label: null,
    tools_count: 0,
    input_chars: 100,
    image_count: 0,
    session_id: null,
    session_short: null,
    agent_id: null,
    parent_session_id: null,
    project_dir: null,
    project_short: null,
    project_dir_pending: false,
    origin_source: "header",
  });
  const startOne = () => {
    SERVER.rows.push(row(SERVER.nextRow));
    SERVER.nextRow += 1;
  };
  const finishOne = () => {
    if (!SERVER.rows.length) return;
    SERVER.rows.shift();
    SERVER.finished += 1;
    SERVER.lastTs = clock.now / 1000;
  };
  const inflightAnswer = (url) => {
    const limit = Number(new URL(url, "http://127.0.0.1").searchParams.get("limit")) || 200;
    const rows = SERVER.rows.slice(0, limit);
    return {
      enabled: true,
      total: SERVER.rows.length,
      shown: rows.length,
      truncated: rows.length < SERVER.rows.length,
      reaped: 0,
      now_mono: 5000,
      rows,
    };
  };

  const respond = (status, data, busy) => ({
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Internal Server Error",
    headers: {
      get: (name) => (String(name).toLowerCase() === "x-mcc-busy" && busy ? "1" : null),
    },
    json: async () => clone(data),
    text: async () => JSON.stringify(data),
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

  async function answer(url, path, method, body, options) {
    const hold = SERVER.holds.get(path);
    if (hold) await hold.promise;
    const failure = SERVER.fail.get(path);
    if (failure) return respond(500, { detail: failure }, false);
    if (path === "/admin/api/config" && method === "GET") {
      return respond(200, configPayload(), false);
    }
    if (path === "/admin/api/config/route-pause" && method === "POST") {
      const key = `${body.model_key}_PAUSED`;
      const held = SERVER.paused.get(key) || [];
      const next = body.paused
        ? held.includes(body.model_ref)
          ? held
          : [...held, body.model_ref]
        : held.filter((ref) => ref !== body.model_ref);
      SERVER.paused.set(key, next);
      return respond(
        200,
        {
          applied: true,
          errors: [],
          model_key: body.model_key,
          model_ref: body.model_ref,
          paused_key: key,
          paused: body.paused,
          paused_value: next.join(","),
        },
        false,
      );
    }
    if (path === "/admin/api/requests/in-flight") {
      return respond(200, inflightAnswer(url), SERVER.busy);
    }
    if (path === "/admin/api/requests/pulse") {
      return respond(
        200,
        {
          enabled: true,
          total: SERVER.finished,
          last_ts: SERVER.lastTs,
          in_flight: SERVER.rows.length,
        },
        SERVER.busy,
      );
    }
    const response = await original(url, options);
    const data = await response.json();
    return respond(response.status || 200, data, false);
  }

  const scenarioFetch = (tab) =>
    async function fetchStub(url, options = {}) {
      const full = String(url);
      const path = full.split("?")[0];
      const method = String(options.method || "GET").toUpperCase();
      let body = null;
      if (options.body) {
        try {
          body = JSON.parse(options.body);
        } catch {
          body = String(options.body);
        }
      }
      const entry = {
        tab,
        t: clock.elapsed(),
        url: full,
        path,
        method,
        body,
        end: null,
        outcome: "pending",
      };
      entry.concurrent = LOG.filter(
        (other) => other.tab === tab && other.path === path && other.outcome === "pending",
      ).length + 1;
      LOG.push(entry);
      try {
        const response = await Promise.race([
          answer(full, path, method, body, options),
          aborted(options.signal),
        ]);
        entry.outcome = response.ok ? "ok" : `http ${response.status}`;
        return response;
      } catch (error) {
        entry.outcome = error && error.name === "AbortError" ? "aborted" : "error";
        throw error;
      } finally {
        entry.end = clock.elapsed();
      }
    };

  // Tab 1 is the harness's own window, prepared before admin.js is evaluated.
  clock.install(window, "tab1");
  window.BroadcastChannel = bus.classFor("tab1");
  window.fetch = scenarioFetch("tab1");
  const setHidden1 = installVisibility(window);

  async function openTab2(errors) {
    const virtualConsole = new VirtualConsole();
    virtualConsole.on("jsdomError", (error) => errors.push(`tab2: ${error.message}`));
    const dom = new JSDOM(html, {
      url: "http://127.0.0.1:8080/admin",
      runScripts: "outside-only",
      pretendToBeVisual: true,
      virtualConsole,
    });
    const win = dom.window;
    prepareTabWindow(win);
    clock.install(win, "tab2");
    win.BroadcastChannel = bus.classFor("tab2");
    win.fetch = scenarioFetch("tab2");
    const setHidden = installVisibility(win);
    win.addEventListener("error", (event) => errors.push(`tab2: ${event.message}`));
    win.addEventListener("unhandledrejection", (event) =>
      errors.push(`tab2: ${String(event.reason)}`),
    );
    win.eval(script);
    win.document.dispatchEvent(new win.Event("DOMContentLoaded", { bubbles: true }));
    await clock.advance(2500);
    return { win, setHidden };
  }

  /* ------------------------------------------------------------- reading */
  const rowsOf = (doc, match) =>
    Array.from(doc.querySelectorAll("[data-route-id]")).filter((node) =>
      match(node.dataset.modelKey || ""),
    );
  const describe = (node) => {
    const button = node.querySelector(".route-pause-toggle");
    const chip = node.querySelector(".route-pause-chip");
    return {
      ref: (node.querySelector("input") || {}).value || "",
      label: button ? button.textContent.trim() : "",
      pressed: button ? button.getAttribute("aria-pressed") : null,
      chipShown: Boolean(chip) && !chip.hidden,
      rowPaused: node.classList.contains("is-paused"),
      inDocument: node.isConnected,
    };
  };
  const fableRows = (doc) => rowsOf(doc, (key) => key === "MODEL_FABLE");
  const agentRows = (doc) => rowsOf(doc, (key) => key.includes("::codex::medium::"));
  const sample = (doc, label) => ({
    label,
    fable: fableRows(doc).map(describe),
    agent: agentRows(doc).map(describe),
  });
  const findRow = (rows, ref) => rows.find((node) => describe(node).ref === ref);
  const click = (win, node) =>
    node.dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
  const nav = (win, view) =>
    win.document.querySelector(`.nav-link[data-view="${view}"]`).click();
  const since = (mark, tab) => LOG.slice(mark).filter((entry) => !tab || entry.tab === tab);
  const countBy = (entries) => {
    const counts = {};
    entries.forEach((entry) => {
      const key = `${entry.method} ${entry.path}`;
      counts[key] = (counts[key] || 0) + 1;
    });
    return counts;
  };
  const brief = (entries) =>
    entries.map((entry) => ({
      tab: entry.tab,
      t: entry.t,
      end: entry.end,
      method: entry.method,
      url: entry.url,
      concurrent: entry.concurrent,
      outcome: entry.outcome,
    }));
  const pollsOf = (entries) =>
    entries.filter(
      (entry) =>
        entry.path === "/admin/api/requests/in-flight" ||
        entry.path === "/admin/api/requests/pulse",
    );

  async function traffic(ms, everyMs = 1000) {
    for (let done = 0; done < ms; done += everyMs) {
      await clock.advance(everyMs);
      finishOne();
      startOne();
    }
  }

  async function run({ scriptErrors, consoleErrors }) {
    const out = {};
    const doc = window.document;

    /* ------------------------------------------------------- page load */
    await clock.advance(2500);
    out.loadUrls = since(0, "tab1").map((entry) => `${entry.method} ${entry.url}`);

    /* ------------------------------------------------- pause: A0 .. B1 */
    out.A0 = sample(doc, "A0 after the page loaded");

    // A Save of an unrelated setting while the Coding agents calls are slow:
    // the request-log query behind harness-usage, held open.
    const usage = deferred();
    SERVER.holds.set("/admin/api/requests/harness-usage", usage);
    const applyA = window.eval("apply()");
    applyA.catch(() => {});
    await clock.advance(400);
    out.A1 = sample(doc, "A1 Save in flight: config re-read, rails rebuilt, agents still loading");

    // The reader clicks the button on the paused head row, whatever it says.
    let mark = LOG.length;
    const sol = findRow(fableRows(doc), "p1/f0-sol");
    out.A2 = { labelAtClick: describe(sol).label };
    click(window, sol.querySelector(".route-pause-toggle"));
    await clock.advance(300);
    out.A2.sent = since(mark, "tab1")
      .filter((entry) => entry.path === "/admin/api/config/route-pause")
      .map((entry) => entry.body);
    out.A2.serverPausedAfter = [...(SERVER.paused.get("MODEL_FABLE_PAUSED") || [])];
    out.A2.after = sample(doc, "A2 after the click");

    // And on the agent's tier rail, in the same window: pause its live a1.
    mark = LOG.length;
    const a1 = findRow(agentRows(doc), "p1/a1");
    out.A2agent = { labelAtClick: a1 ? describe(a1).label : null };
    if (a1) click(window, a1.querySelector(".route-pause-toggle"));
    await clock.advance(300);
    out.A2agent.sent = since(mark, "tab1")
      .filter((entry) => entry.path === "/admin/api/harness-tiers" && entry.method === "POST")
      .map((entry) => entry.body);

    usage.resolve();
    SERVER.holds.delete("/admin/api/requests/harness-usage");
    try {
      await applyA;
    } catch (error) {
      out.A3error = String(error && (error.stack || error.message));
    }
    await clock.advance(500);
    out.A3 = sample(doc, "A3 after the Save finished");

    // Back to the shape under test before each further step.
    const reset = async () => {
      SERVER.paused.set("MODEL_FABLE_PAUSED", ["p1/f0-sol", "p1/f1-muse"]);
      const reload = window.eval("apply()");
      reload.catch(() => {});
      await clock.advance(1500);
      try {
        await reload;
      } catch {
        /* reported by the step that cares */
      }
    };

    // A stale paint: the label disagrees with the saved state at click time.
    await reset();
    const staleClick = async (ref, fakeLabel) => {
      const node = findRow(fableRows(doc), ref);
      const button = node.querySelector(".route-pause-toggle");
      button.textContent = fakeLabel;
      button.setAttribute("aria-pressed", fakeLabel === "Resume" ? "true" : "false");
      const before = LOG.length;
      click(window, button);
      await clock.advance(300);
      return {
        ref,
        labelAtClick: fakeLabel,
        sent: since(before, "tab1")
          .filter((entry) => entry.path === "/admin/api/config/route-pause")
          .map((entry) => entry.body),
        after: describe(findRow(fableRows(doc), ref)),
        status: (doc.getElementById("routeStatus") || {}).textContent || "",
      };
    };
    out.staleClicks = [
      await staleClick("p1/f0-sol", "Pause"),
      await staleClick("p1/f2-bunny", "Resume"),
    ];

    // The pause write itself fails: the rows stay as the server has them.
    await reset();
    SERVER.fail.set("/admin/api/config/route-pause", "simulated write failure");
    const muse = findRow(fableRows(doc), "p1/f1-muse");
    click(window, muse.querySelector(".route-pause-toggle"));
    await clock.advance(500);
    SERVER.fail.delete("/admin/api/config/route-pause");
    out.failedPauseWrite = sample(doc, "after a failed pause write");

    // B1: a Save during which a call that load() awaits fails.
    await reset();
    SERVER.fail.set("/admin/api/providers/local-status", "simulated timeout under load");
    const applyB = window.eval("apply()");
    let applyBError = null;
    applyB.catch((error) => {
      applyBError = String(error && error.message);
    });
    await clock.advance(1500);
    SERVER.fail.delete("/admin/api/providers/local-status");
    out.B1 = sample(doc, "B1 Save finished with local-status failing");
    out.B1.applyError = applyBError;
    await reset();

    out.validateCalls = LOG.filter(
      (entry) => entry.path === "/admin/api/config/validate",
    ).length;

    /* --------------------------------------------- polls: one tab first */
    nav(window, "requests");
    await clock.advance(500);
    const auto = doc.getElementById("reqAutoRefresh");
    auto.checked = true;
    auto.dispatchEvent(new window.Event("change", { bubbles: true }));
    const autoInterval = doc.getElementById("reqAutoRefreshInterval");
    autoInterval.value = "15000";
    autoInterval.dispatchEvent(new window.Event("change", { bubbles: true }));
    for (let i = 0; i < 3; i += 1) startOne();
    // The view as a reader opens it: one full load.
    doc.getElementById("reqRefreshButton").click();
    await clock.advance(2000);

    // A request finishes every second for 61 s.
    mark = LOG.length;
    const p1Start = clock.elapsed();
    await traffic(61000);
    const p1 = since(mark, "tab1");
    out.finishTicks = {
      seconds: (clock.elapsed() - p1Start) / 1000,
      counts: countBy(p1),
      requests: p1.length,
      urls: brief(p1.filter((entry) => entry.path !== "/admin/api/requests/in-flight")),
    };
    out.asOf = {
      lastUpdated: doc.getElementById("reqLastUpdated").textContent,
      captions: Array.from(doc.querySelectorAll("#view-requests .analytics-window")).map(
        (caption) => caption.textContent.trim(),
      ),
    };

    // The interval the reader chose is what drives the table: 5 s.
    autoInterval.value = "5000";
    autoInterval.dispatchEvent(new window.Event("change", { bubbles: true }));
    mark = LOG.length;
    await traffic(20000);
    out.chosenInterval = { counts: countBy(since(mark, "tab1")) };
    autoInterval.value = "15000";
    autoInterval.dispatchEvent(new window.Event("change", { bubbles: true }));

    // The badge on another view: every 10 s, one row.
    nav(window, "limits");
    await clock.advance(100);
    mark = LOG.length;
    await clock.advance(60000);
    const badge = since(mark, "tab1").filter(
      (entry) => entry.path === "/admin/api/requests/in-flight",
    );
    out.badgeOffView = { count: badge.length, urls: brief(badge) };
    nav(window, "requests");
    await clock.advance(100);
    mark = LOG.length;
    await clock.advance(9500);
    const panel = since(mark, "tab1").filter(
      (entry) => entry.path === "/admin/api/requests/in-flight",
    );
    out.panelOnView = { count: panel.length, urls: brief(panel) };

    // Busy answers: the in-flight poll backs off, and comes back after.
    auto.checked = false;
    auto.dispatchEvent(new window.Event("change", { bubbles: true }));
    await clock.advance(3500);
    SERVER.busy = true;
    mark = LOG.length;
    await clock.advance(60000);
    const busy = since(mark, "tab1").filter(
      (entry) => entry.path === "/admin/api/requests/in-flight",
    );
    SERVER.busy = false;
    const calm = LOG.length;
    await clock.advance(90000);
    const after = since(calm, "tab1").filter(
      (entry) => entry.path === "/admin/api/requests/in-flight",
    );
    out.busyBackoff = {
      busyStarts: busy.map((entry) => entry.t),
      calmStarts: after.map((entry) => entry.t),
    };

    // Errors back off the same way (the pulse, failing).
    auto.checked = true;
    auto.dispatchEvent(new window.Event("change", { bubbles: true }));
    await clock.advance(100);
    SERVER.fail.set("/admin/api/requests/pulse", "simulated 500");
    mark = LOG.length;
    await clock.advance(120000);
    out.errorBackoff = {
      pulseStarts: since(mark, "tab1")
        .filter((entry) => entry.path === "/admin/api/requests/pulse")
        .map((entry) => entry.t),
    };
    SERVER.fail.delete("/admin/api/requests/pulse");
    await clock.advance(60000);

    // A server that holds the in-flight request: never two out at once.
    auto.checked = false;
    auto.dispatchEvent(new window.Event("change", { bubbles: true }));
    await clock.advance(3500);
    const held = deferred();
    SERVER.holds.set("/admin/api/requests/in-flight", held);
    mark = LOG.length;
    await clock.advance(40000);
    const overlap = since(mark, "tab1").filter(
      (entry) => entry.path === "/admin/api/requests/in-flight",
    );
    out.heldPoll = {
      urls: brief(overlap),
      maxConcurrent: Math.max(0, ...overlap.map((entry) => entry.concurrent)),
    };
    held.resolve();
    SERVER.holds.delete("/admin/api/requests/in-flight");
    await clock.advance(20000);

    /* -------------------------------------------------- polls: two tabs */
    auto.checked = true;
    auto.dispatchEvent(new window.Event("change", { bubbles: true }));
    await clock.advance(1000);
    const tab2Errors = [];
    const tab2 = await openTab2(tab2Errors);
    nav(tab2.win, "limits");
    await clock.advance(3000);
    mark = LOG.length;
    await traffic(30000);
    const both = pollsOf(since(mark));
    out.twoTabs = {
      tab1Polls: both.filter((entry) => entry.tab === "tab1").length,
      tab2Polls: both.filter((entry) => entry.tab === "tab2").length,
      tab2InflightUrls: both
        .filter((entry) => entry.tab === "tab2" && entry.path.endsWith("/in-flight"))
        .map((entry) => entry.url),
      tab1Badge: doc.getElementById("navInflightBadge").textContent.trim(),
      tab1PanelRows: doc.querySelectorAll(
        "#reqInflightRows tr.inflight-row:not(.inflight-finishing)",
      ).length,
      serverRows: SERVER.rows.length,
    };

    // The polling tab is hidden: the other takes over at once. The clock is
    // read at the moment of hiding, and everything from then on is counted.
    mark = LOG.length;
    const hiddenAt = clock.elapsed();
    tab2.setHidden(true);
    await traffic(15000);
    const handover = pollsOf(since(mark));
    out.hiddenLeader = {
      tab1Polls: handover.filter((entry) => entry.tab === "tab1").length,
      tab2Polls: handover.filter((entry) => entry.tab === "tab2").length,
      tab1FirstPollAfter: (handover.find((entry) => entry.tab === "tab1") || {}).t,
      hiddenAt,
    };

    // Shown again, it claims again; then it dies without a word.
    tab2.setHidden(false);
    await clock.advance(3000);
    clock.kill("tab2");
    bus.dead.add("tab2");
    const killedAt = clock.elapsed();
    mark = LOG.length;
    await traffic(30000);
    const takeover = pollsOf(since(mark));
    const firstTab1 = takeover.find((entry) => entry.tab === "tab1");
    out.deadLeader = {
      tab1Polls: takeover.filter((entry) => entry.tab === "tab1").length,
      tab2Polls: takeover.filter((entry) => entry.tab === "tab2").length,
      takeoverMs: firstTab1 ? firstTab1.t - killedAt : null,
    };
    out.channel = {
      posted: bus.posted.length,
      types: [...new Set(bus.posted.map((entry) => entry.type))].sort(),
    };

    out.scriptErrors = [...scriptErrors];
    out.consoleErrors = [...consoleErrors];
    out.clockErrors = clock.errors;
    out.tab2Errors = tab2Errors;
    out.totalRequests = LOG.length;
    out.clockMs = clock.elapsed();
    return out;
  }

  return { run, setHidden: setHidden1 };
}
