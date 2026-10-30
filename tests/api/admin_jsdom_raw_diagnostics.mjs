/**
 * The request detail's raw diagnostics (7.121.0), run instead of the main
 * capture when MCC_JSDOM_SCENARIO=raw_diagnostics.
 *
 * Nothing here is written by hand: `MCC_JSDOM_RAW_FILE` names a JSON file the
 * Python test writes by running the server's own reader
 * (`api/raw_diagnostics.py`) over fake log files -- the summary, every page
 * and every normalised answer the page will ask for, keyed by
 * `id|kind|view|start`, plus the two request rows. So the page renders the
 * server's real sentences, and a wording change on either side shows here.
 *
 * What it measures:
 *
 *  - request 1 (four kinds, four states): the boxes are collapsed, their
 *    summary lines, each Raw body (present / pruned by rotation / no error /
 *    setting off, naming the setting and its page), each Normalized body, and
 *    that neither view ever shows the other's text;
 *  - the Raw | Normalized choice: remembered in localStorage, restored on the
 *    next open, Raw when the store is blocked;
 *  - request 2: payload pretty-print (labelled; the Python test proves it
 *    lossless against the repr), Copy copying exactly the shown text, "load
 *    more" paging with the anchor sent back, the traceback note, the CLI box;
 *  - the per-request export picker: what it asks for, the file it saves, the
 *    server's refusal shown in its own words, an empty selection;
 *  - the card's DOM with the new section taken out of a clone, for request 1
 *    and the harness's own "req-fallback" row, which the Python test compares
 *    byte for byte with the BASE tree's run of this same scenario.
 *
 * On the BASE tree (53d78dab) there is no #reqDetailRaw: the scenario says
 * `baseTree: true` and captures only the DOM for the baseline.
 */

import { readFileSync } from "node:fs";

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function prepareRawDiagnosticsScenario({ window, ROUTES }) {
  const doc = window.document;
  const data = JSON.parse(readFileSync(process.env.MCC_JSDOM_RAW_FILE || "", "utf8"));
  /* The fixture's rows are put on the harness's route table; a row the harness
     already defines ("req-fallback", the 7.120.2 payload) keeps its own, so
     the fallback half of the equality compares the harness's real card. */
  for (const [id, row] of Object.entries(data.rows)) {
    const path = `/admin/api/requests/${id}`;
    if (!Object.prototype.hasOwnProperty.call(ROUTES, path)) ROUTES[path] = row;
  }

  const rawUrls = [];
  const exportUrls = [];
  const missing = [];
  const downloads = [];
  const copied = [];
  let exportFailure = null;

  const harnessFetch = window.fetch;
  window.fetch = async (url, options = {}) => {
    const parsed = new window.URL(String(url), "http://127.0.0.1:8080");
    if (parsed.pathname === "/admin/api/requests/raw") {
      rawUrls.push(String(url));
      const params = parsed.searchParams;
      const id = params.get("id");
      const kind = params.get("kind");
      const view = params.get("view") || "raw";
      const key = !kind
        ? `${id}|summary`
        : view === "normalized"
          ? `${id}|${kind}|normalized`
          : `${id}|${kind}|raw|${params.get("start") || "0"}`;
      const answer = data.answers[key];
      if (!answer) missing.push(key);
      const body = answer || {};
      return {
        ok: true,
        status: 200,
        json: async () => JSON.parse(JSON.stringify(body)),
        text: async () => JSON.stringify(body),
      };
    }
    if (parsed.pathname === "/admin/api/requests/raw/export") {
      exportUrls.push(String(url));
      if (exportFailure) {
        const failure = exportFailure;
        return {
          ok: false,
          status: failure.status,
          statusText: "Unprocessable",
          json: async () => ({ detail: failure.detail }),
        };
      }
      return {
        ok: true,
        status: 200,
        headers: {
          get: (name) =>
            String(name).toLowerCase() === "content-disposition"
              ? `attachment; filename="${data.exportFilename}"`
              : null,
        },
        blob: async () => new window.Blob(["Request,Kind\n"], { type: "text/csv" }),
      };
    }
    return harnessFetch(url, options);
  };

  const flat = (el) => (el ? (el.textContent || "").replace(/\s+/g, " ").trim() : "");

  const waitFor = async (predicate, timeoutMs = 15000) => {
    const started = Date.now();
    while (Date.now() - started < timeoutMs) {
      if (predicate()) return true;
      await wait(25);
    }
    return false;
  };

  /** The modal card, element by element, with the raw section taken out of a
   *  clone (the live page is never mutated). Line endings normalised so the
   *  committed baseline reads the same on every platform. */
  const captureCard = () => {
    const card = doc.querySelector("#reqDetailModal .requests-modal-card");
    if (!card) return null;
    const clone = card.cloneNode(true);
    const raw = clone.querySelector("#reqDetailRaw");
    if (raw) raw.remove();
    return {
      children: Array.from(clone.children).map((el) => el.outerHTML.replace(/\r\n/g, "\n")),
      childCount: card.children.length,
    };
  };

  const stableCard = async () => {
    let previous = null;
    for (let attempt = 0; attempt < 20; attempt += 1) {
      const current = JSON.stringify(captureCard());
      if (current === previous) return JSON.parse(current);
      previous = current;
      await wait(100);
    }
    return captureCard();
  };

  const panes = () => Array.from(doc.querySelectorAll("#reqDetailRawBoxes .req-raw-pane"));
  const pane = (kind) => panes().find((el) => el.dataset.rawKind === kind) || null;

  const summaryOf = (el) => ({
    kind: el.dataset.rawKind,
    open: el.open,
    label: flat(el.querySelector(".req-raw-kind")),
    facts: flat(el.querySelector(".req-wire-facts")),
    pressed: Array.from(el.querySelectorAll(".req-raw-view")).map((button) => [
      button.dataset.view,
      button.getAttribute("aria-pressed"),
    ]),
  });

  /** What one open box shows: its source line, the sentence when there are no
   *  lines, its notes, the text block, and whether it offers Copy. */
  const bodyOf = (el) => {
    const body = el.querySelector(".req-raw-body");
    const pre = body ? body.querySelector("pre") : null;
    return {
      view: body ? body.dataset.view || "" : "",
      source: flat(body && body.querySelector(".req-raw-source")),
      state: flat(body && body.querySelector(".req-raw-state")),
      stateHeadline: flat(body && body.querySelector(".req-raw-state strong")),
      notes: body ? Array.from(body.querySelectorAll(".req-raw-note")).map(flat).filter(Boolean) : [],
      codeSpans: body ? body.querySelectorAll(".req-raw-state code").length : 0,
      pre: pre ? pre.textContent : null,
      more: flat(body && body.querySelector(".req-raw-more")),
      hasCopy: Boolean(body && body.querySelector(".guide-copy-button")),
    };
  };

  const settled = (el, view) => {
    const body = el.querySelector(".req-raw-body");
    if (!body || body.dataset.view !== view) return false;
    return Boolean(body.querySelector(".req-raw-state, pre"));
  };

  /** Open a box the way the browser does when its summary is clicked; jsdom
   *  fires `toggle` itself, and a second dispatch would only re-render. */
  const openBox = async (kind, view) => {
    const el = pane(kind);
    if (!el) return null;
    if (!el.open) el.open = true;
    if (!(await waitFor(() => settled(el, view), 3000))) {
      el.dispatchEvent(new window.Event("toggle"));
      await waitFor(() => settled(el, view));
    }
    return bodyOf(el);
  };

  const clickView = async (kind, view) => {
    const el = pane(kind);
    const button = Array.from(el.querySelectorAll(".req-raw-view")).find(
      (candidate) => candidate.dataset.view === view,
    );
    button.dispatchEvent(new window.MouseEvent("click", { bubbles: true, cancelable: true }));
    await waitFor(() => settled(el, view));
  };

  const storedView = () => {
    try {
      return window.localStorage.getItem("mcc.rawDiagnosticsView");
    } catch {
      return "blocked";
    }
  };

  const openDetail = async (id) => {
    await window.eval(`openRequestDetail(${JSON.stringify(id)})`);
    await waitFor(() => !doc.getElementById("reqDetailModal").hidden);
  };

  async function run({ scriptErrors, consoleErrors }) {
    const out = { scenarioFatal: null, baseTree: false };
    await wait(300);
    const [first, second] = data.order;

    await openDetail(first);
    out.domFirst = await stableCard();
    window.eval("closeRequestDetail()");
    await openDetail("req-fallback");
    out.domFallback = await stableCard();
    window.eval("closeRequestDetail()");

    if (!doc.getElementById("reqDetailRaw")) {
      /* The BASE tree: no raw section exists, so the card is the 7.120.2
         card. That is the equality baseline, and all there is to capture. */
      out.baseTree = true;
      out.scriptErrors = scriptErrors;
      out.consoleErrors = consoleErrors;
      return out;
    }

    // ---- request 1: four kinds, four states -------------------------------
    try {
      window.localStorage.removeItem("mcc.rawDiagnosticsView");
    } catch {
      /* no store */
    }
    await openDetail(first);
    const painted = await waitFor(() => panes().length === 4);
    if (!painted) {
      out.scenarioFatal = "the four raw boxes never painted";
      out.status = flat(doc.getElementById("reqDetailRawStatus"));
      out.missing = missing;
      return out;
    }
    out.sectionHidden = doc.getElementById("reqDetailRaw").hidden;
    out.sectionIsLast =
      doc.querySelector("#reqDetailModal .requests-modal-card").lastElementChild ===
      doc.getElementById("reqDetailRaw");
    out.status = flat(doc.getElementById("reqDetailRawStatus"));
    out.warning = flat(doc.querySelector("#reqDetailRaw .req-raw-warning"));
    out.collapsed = panes().map(summaryOf);
    out.rawFirst = {};
    for (const kind of ["payloads", "sse", "traceback", "cli"]) {
      out.rawFirst[kind] = await openBox(kind, "raw");
    }
    await clickView("sse", "normalized");
    out.storedAfterNormalized = storedView();
    out.normalizedFirst = {};
    for (const kind of ["payloads", "sse", "traceback", "cli"]) {
      await waitFor(() => settled(pane(kind), "normalized"));
      out.normalizedFirst[kind] = bodyOf(pane(kind));
    }
    out.pressedAfterNormalized = panes().map(summaryOf).map((s) => s.pressed);

    // Remembered: the next request detail opens on Normalized.
    window.eval("closeRequestDetail()");
    await openDetail(first);
    await waitFor(() => panes().length === 4);
    out.pressedOnReopen = panes().map(summaryOf).map((s) => s.pressed);
    await clickView("sse", "raw");
    out.storedAfterRaw = storedView();

    // A blocked store reads as Raw even though Normalized was the last choice.
    try {
      window.localStorage.setItem("mcc.rawDiagnosticsView", "normalized");
    } catch {
      /* no store */
    }
    window.eval("closeRequestDetail()");
    let blocked = false;
    try {
      Object.defineProperty(window, "localStorage", {
        configurable: true,
        get() {
          throw new window.DOMException("blocked", "SecurityError");
        },
      });
      blocked = true;
    } catch (error) {
      out.blockError = String(error);
    }
    await openDetail(first);
    await waitFor(() => panes().length === 4);
    out.blockedStore = blocked;
    out.pressedWhenBlocked = panes().map(summaryOf).map((s) => s.pressed);
    if (blocked) delete window.localStorage;
    window.eval("closeRequestDetail()");

    // ---- request 2: pretty-print, copy, paging, traceback, CLI ------------
    try {
      window.localStorage.removeItem("mcc.rawDiagnosticsView");
    } catch {
      /* no store */
    }
    Object.defineProperty(window.navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: (text) => {
          copied.push(String(text));
          return Promise.resolve();
        },
      },
    });
    await openDetail(second);
    await waitFor(() => panes().length === 4);
    out.collapsedSecond = panes().map(summaryOf);

    const payloads = await openBox("payloads", "raw");
    out.payloadVerbatim = payloads.pre;
    const prettyButton = pane("payloads").querySelector(".req-raw-pretty");
    out.prettyButton = flat(prettyButton);
    prettyButton.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await wait(50);
    out.prettyButtonOn = flat(prettyButton);
    out.prettyPre = pane("payloads").querySelector("pre").textContent;
    out.prettyNotes = bodyOf(pane("payloads")).notes;
    pane("payloads")
      .querySelector(".guide-copy-button")
      .dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await waitFor(() => copied.length === 1);
    prettyButton.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await wait(50);
    out.prettyOffPre = pane("payloads").querySelector("pre").textContent;
    pane("payloads")
      .querySelector(".guide-copy-button")
      .dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await waitFor(() => copied.length === 2);
    out.copied = copied.slice();

    const sse = await openBox("sse", "raw");
    out.ssePages = [{ pre: sse.pre, more: sse.more }];
    for (let page = 0; page < 5; page += 1) {
      const button = pane("sse").querySelector(".req-raw-more button");
      if (!button) break;
      const before = pane("sse").querySelector("pre").textContent;
      button.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
      await waitFor(() => pane("sse").querySelector("pre").textContent !== before);
      await wait(30);
      const now = bodyOf(pane("sse"));
      out.ssePages.push({ pre: now.pre, more: now.more });
    }
    out.traceback = await openBox("traceback", "raw");
    out.cli = await openBox("cli", "raw");

    // ---- the per-request export picker ------------------------------------
    window.downloadBlob = (filename, blob) => {
      downloads.push({ filename, size: blob.size });
    };
    const toggle = doc.getElementById("reqRawExportToggle");
    const panel = doc.getElementById("reqRawExportPanel");
    out.exportPanelHiddenAtFirst = panel.hidden;
    toggle.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    out.exportPanelOpened = { hidden: panel.hidden, expanded: toggle.getAttribute("aria-expanded") };
    out.exportKinds = Array.from(panel.querySelectorAll('input[name="reqRawExportKind"]')).map(
      (input) => [input.value, input.checked, flat(input.parentElement)],
    );
    out.exportViews = Array.from(doc.getElementById("reqRawExportView").options).map((o) => [o.value, flat(o)]);
    out.exportFormats = Array.from(doc.getElementById("reqRawExportFormat").options).map((o) => o.value);
    panel.querySelector('input[value="cli"]').checked = false;
    doc.getElementById("reqRawExportFormat").value = "csv";
    doc.getElementById("reqRawExportView").value = "raw";
    const download = doc.getElementById("reqRawExportDownload");
    download.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await waitFor(() => downloads.length === 1);
    out.exportUrls = exportUrls.slice();
    out.downloads = downloads.slice();
    out.exportHint = flat(doc.getElementById("reqRawExportHint"));
    exportFailure = data.exportFailure;
    download.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await waitFor(() => flat(doc.getElementById("reqRawExportHint")).startsWith("Not exported"));
    out.exportFailureHint = flat(doc.getElementById("reqRawExportHint"));
    exportFailure = null;
    panel.querySelectorAll('input[name="reqRawExportKind"]').forEach((input) => {
      input.checked = false;
    });
    const before = exportUrls.length;
    download.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await wait(50);
    out.exportEmptyHint = flat(doc.getElementById("reqRawExportHint"));
    out.exportEmptyFetched = exportUrls.length - before;
    window.eval("closeRequestDetail()");
    // The next detail starts with the picker folded and its hint cleared.
    await openDetail(first);
    out.exportPanelOnNextOpen = {
      hidden: panel.hidden,
      expanded: toggle.getAttribute("aria-expanded"),
      hint: flat(doc.getElementById("reqRawExportHint")),
    };
    window.eval("closeRequestDetail()");

    out.rawUrls = rawUrls;
    out.missing = missing;
    out.scriptErrors = scriptErrors;
    out.consoleErrors = consoleErrors;
    return out;
  }

  return { run };
}
