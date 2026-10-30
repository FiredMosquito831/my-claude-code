/**
 * The settings page's saved values (7.121.1), run instead of the main capture
 * when MCC_JSDOM_SCENARIO=select_blank.
 *
 * The payload is not written here: `MCC_JSDOM_CONFIG_FILE` names a JSON file
 * the Python test writes with the server's own `_config_response()` over a
 * scratch managed `.env`, so the harness renders the exact 547 fields the
 * server sends for that file and cannot drift from the manifests. Every value
 * in it is fake.
 *
 * What it measures, on the page the harness already serves:
 *
 *  - every fixture control: what a reader sees (`shown`), what the control
 *    would submit (`submits`, read from the page's own `changedValues()`),
 *    `selectedIndex` and `dataset.original`, its option values and labels,
 *    the marked "saved, not in today's list" option when one exists, and the
 *    "not a number this box can hold" note when one is rendered;
 *  - the footer on load and the exact body a click on Apply posts with no
 *    edit at all -- the body that used to carry five `""` and wipe five
 *    settings -- then the footer and body after ONE typed edit (DDGS_REGION);
 *  - every field key in the payload and whether the document carries a real
 *    control for it (`coverage`), which is what makes a setting with no
 *    control a failing test rather than an invisible gap;
 *  - every control's `outerHTML`, and a digest of every settings view's DOM,
 *    with this release's new markup removed from a CLONE (the marked option
 *    and the saved-number note), so the Python test can prove the page with
 *    the additions taken out is the page it was before, per control and per
 *    view.
 *
 * On the BASE tree (f6de93ad) no marked option exists, so the scenario reports
 * `baseTree: true` and captures the same measurements for the committed
 * baseline: blank selects, the sanitised number, the footer that already says
 * five unsaved changes. Prints nothing itself: `run()` returns the
 * measurements and the harness prints one JSON object.
 */

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** The fixture controls this release is about. The first five are the ones
 *  measured to be wiped by a Save nobody meant to make; the sixth is a
 *  rotation select that has no control at all until the coverage release, and
 *  must simply never be posted; the rest are the neighbours that have to stay
 *  exactly as they were. */
const FIXTURE_KEYS = [
  "NVIDIA_NIM_API_KEY_ROTATION",
  "DDGS_BACKEND",
  "LOG_LEVEL",
  "PROXY_CHECK_ENABLED",
  "MODEL_DISCOVERY_REFRESH_SECONDS",
  "TAVILY_API_KEY_ROTATION",
  "DDGS_SAFESEARCH",
  "WEB_FETCH_EXTRACTOR",
  "REQUEST_LOG_MAX_ROWS",
  "BRAVE_SEARCH_LANG",
];

/** The one field whose control is allowed to change shape: a number box
 *  cannot hold "7_200", so it renders as text. */
const SAVED_NUMBER_KEY = "MODEL_DISCOVERY_REFRESH_SECONDS";

const EDIT_KEY = "DDGS_REGION";
const EDIT_VALUE = "uk-en";

export function prepareSelectBlankScenario({ window, ROUTES }) {
  const doc = window.document;
  const flat = (el) => (el ? (el.textContent || "").replace(/\s+/g, " ").trim() : "");

  const payloadPath = process.env.MCC_JSDOM_CONFIG_FILE || "";
  if (!payloadPath) {
    throw new Error("MCC_JSDOM_CONFIG_FILE is not set: nothing to render");
  }
  const payload = JSON.parse(readFileSync(payloadPath, "utf8"));
  if (!Array.isArray(payload.fields) || payload.fields.length === 0) {
    throw new Error("the config payload carries no fields");
  }
  ROUTES["/admin/api/config"] = payload;

  /* A successful save, so a click on Apply behaves as it does against a real
     server (the page reloads from the payload afterwards). The harness's own
     route map answers nothing for this path, which would make every click a
     refused validation instead of a save. */
  if (!ROUTES["/admin/api/config/apply"]) {
    ROUTES["/admin/api/config/apply"] = {
      applied: true,
      valid: true,
      errors: [],
      warnings: [],
      pending_fields: [],
      restart: { required: false, automatic: false, admin_url: null, fields: [] },
    };
  }

  /* One apply body per click, and nothing else: the wrapper forwards every
     other call to the harness's own stub, so no capture above this line
     changes. */
  const applyBodies = [];
  const realFetch = window.fetch;
  window.fetch = async (url, options = {}) => {
    if (String(url).split("?")[0] === "/admin/api/config/apply") {
      try {
        applyBodies.push({ at: applyBodies.length, body: JSON.parse(options.body) });
      } catch {
        applyBodies.push({ at: applyBodies.length, body: null });
      }
    }
    return realFetch(url, options);
  };

  const controlFor = (key) =>
    doc.querySelector(
      `input[data-key="${key}"], select[data-key="${key}"], textarea[data-key="${key}"]`,
    );

  const isControl = (el) => Boolean(el && el.matches("input, select, textarea"));

  /** Everything about one control this release can move, in one record. */
  const measure = (key) => {
    const input = controlFor(key);
    if (!isControl(input)) return { present: false };
    const options = Array.from(input.options || []);
    const selected = input.selectedIndex >= 0 ? input.options[input.selectedIndex] : null;
    const marked = options.find((item) => item.dataset && item.dataset.savedValue === "true");
    const wrapper = input.closest(".field[data-key]");
    const note = wrapper ? wrapper.querySelector(".field-saved-number") : null;
    return {
      present: true,
      tag: input.tagName.toLowerCase(),
      type: input.getAttribute("type") || "",
      // What a reader sees: the chosen option's own words, or the box's text.
      shown: input.tagName.toLowerCase() === "select" ? flat(selected) : input.value,
      submits: submits()[key] === undefined ? null : submits()[key],
      value: input.value,
      original: input.dataset.original,
      selectedIndex: input.selectedIndex === undefined ? null : input.selectedIndex,
      optionValues: options.map((item) => item.value),
      optionLabels: options.map((item) => flat(item)),
      savedOption: marked ? flat(marked) : null,
      savedNote: note ? flat(note) : null,
      readOnly: Boolean(input.readOnly),
      disabled: Boolean(input.disabled),
    };
  };

  const submits = () => {
    try {
      return window.eval("changedValues()") || {};
    } catch {
      return {};
    }
  };

  const footer = () => {
    const node = doc.getElementById("dirtyState");
    return node ? (node.textContent || "").trim() : null;
  };

  const applyButton = () => doc.getElementById("applyButton");

  /** Click Apply and wait for the POST the click may make. Returns the body of
   *  the request this click caused, or null when the click was refused (a
   *  disabled button makes no request at all -- which, on a page nobody has
   *  edited, is the correct answer). */
  const clickApply = async () => {
    const before = applyBodies.length;
    const button = applyButton();
    if (!button) return { clickable: false, body: null, disabled: null };
    const disabled = Boolean(button.disabled);
    button.click();
    for (let attempt = 0; attempt < 40 && applyBodies.length === before; attempt += 1) {
      await wait(50);
    }
    await wait(150);
    const next = applyBodies[before] || null;
    return { clickable: true, disabled, body: next ? next.body : null };
  };

  /** Every settings view's DOM, with this release's own markup taken out of a
   *  clone: the marked option, the saved-number note, and the saved-number
   *  control itself (replaced by an inert marker, because its *shape* is the
   *  one control change this release makes and the per-control cells below
   *  are where that is measured). Line endings are normalised so a CRLF
   *  artefact cannot read as a page difference. */
  const captureViews = () => {
    const views = {};
    doc.querySelectorAll(".admin-view[data-view]").forEach((view) => {
      const clone = view.cloneNode(true);
      clone.querySelectorAll("[data-saved-value]").forEach((node) => node.remove());
      clone.querySelectorAll(".field-saved-number").forEach((node) => node.remove());
      clone.querySelectorAll(`[data-key="${SAVED_NUMBER_KEY}"]`).forEach((node) => {
        if (!node.matches("input, select, textarea")) return;
        const marker = doc.createElement("i");
        marker.setAttribute("data-normalised", "saved-number-control");
        node.replaceWith(marker);
      });
      const html = clone.innerHTML.replace(/\r\n/g, "\n");
      views[view.dataset.view] = {
        children: clone.children.length,
        length: html.length,
        sha256: sha256(html),
      };
    });
    return views;
  };

  /** Every control's element markup, new markup removed, for the per-control
   *  equality. A select's chosen option is a property, not an attribute, so
   *  this pins the control's shape and option list, not what is selected --
   *  the behavioural cells above are what pin the value. */
  const captureControls = (fieldKeys) => {
    const controls = {};
    fieldKeys.forEach((key) => {
      const input = controlFor(key);
      if (!isControl(input)) {
        controls[key] = null;
        return;
      }
      const clone = input.cloneNode(true);
      clone.querySelectorAll("[data-saved-value]").forEach((node) => node.remove());
      controls[key] = clone.outerHTML.replace(/\r\n/g, "\n");
    });
    return controls;
  };

  /** Which fields the document can actually edit: the coverage half. */
  const captureCoverage = (fieldKeys) => {
    const withControl = [];
    const withoutControl = [];
    fieldKeys.forEach((key) => {
      if (isControl(controlFor(key))) withControl.push(key);
      else withoutControl.push(key);
    });
    return {
      fieldCount: fieldKeys.length,
      controlCount: withControl.length,
      withoutControl,
    };
  };

  const stableFooter = async () => {
    let previous = null;
    for (let attempt = 0; attempt < 40; attempt += 1) {
      const current = footer();
      if (current === previous) return current;
      previous = current;
      await wait(50);
    }
    return footer();
  };

  const waitFor = async (predicate, timeoutMs = 30000) => {
    const started = Date.now();
    while (Date.now() - started < timeoutMs) {
      if (predicate()) return true;
      await wait(50);
    }
    return false;
  };

  async function run({ scriptErrors, consoleErrors }) {
    const out = { scenarioFatal: null, baseTree: false };
    const fieldKeys = payload.fields.map((field) => field.key);

    const firstControl = () =>
      doc.querySelector(".admin-view[data-view='web_search'] [data-key]") ||
      doc.querySelector("[data-key]");
    if (!(await waitFor(() => Boolean(firstControl())))) {
      out.scenarioFatal = "the settings views never rendered";
      out.scriptErrors = scriptErrors;
      out.consoleErrors = consoleErrors;
      return out;
    }
    // The rail is rendered by load(); give the provider cards (which carry the
    // rotation selects) and the web-search cards (which carry the advanced
    // groups) the same settle the other scenarios use.
    await wait(600);
    out.footerOnLoad = await stableFooter();
    out.changedOnLoad = Object.keys(submits()).sort();
    out.baseTree = !doc.querySelector("[data-saved-value]");

    out.fields = {};
    FIXTURE_KEYS.forEach((key) => {
      out.fields[key] = measure(key);
    });

    out.coverage = captureCoverage(fieldKeys);
    out.controlKeys = Object.keys(captureControls(fieldKeys)).filter(
      (key) => doc.querySelector(`[data-key="${key}"]`) !== null,
    ).length;
    out.controls = captureControls(fieldKeys);
    out.views = captureViews();
    out.savedNumberTag = out.fields[SAVED_NUMBER_KEY]
      ? out.fields[SAVED_NUMBER_KEY].tag
      : null;

    // --- Apply with no edit at all. On the fixed tree the button is disabled
    // and nothing is posted; on the base tree this is the five-"" body.
    out.applyOnLoad = await clickApply();
    await wait(400);
    out.footerAfterEmptyApply = await stableFooter();

    // --- One typed edit, on a field nobody has set (so it starts empty and
    // "uk-en" cannot collide with anything loaded).
    const edit = controlFor(EDIT_KEY);
    if (!isControl(edit)) {
      out.scenarioFatal = `no control for ${EDIT_KEY}`;
      out.scriptErrors = scriptErrors;
      out.consoleErrors = consoleErrors;
      return out;
    }
    edit.value = EDIT_VALUE;
    edit.dispatchEvent(new window.Event("input", { bubbles: true }));
    out.afterEdit = {
      typed: edit.value,
      footer: await stableFooter(),
      submitted: Object.keys(submits()).sort(),
    };
    out.applyAfterEdit = await clickApply();
    await wait(600);
    out.afterEdit.footerAfterApply = await stableFooter();

    /* --- The rule itself, isolated from every display fix. A control whose
       value differs from what was loaded, that nobody has touched, must not be
       a change -- otherwise the rule depends on every control being able to
       display everything, which is exactly the assumption that failed. The
       probe is built by the page's own buildFieldControl (so it is wired
       exactly like every real control, on both trees), then told it was loaded
       holding something else. It is never a setting: the key is not a manifest
       field, nothing is saved after this, and the node is removed. */
    const probeField = {
      key: "SELECT_BLANK_UNTOUCHED_PROBE",
      label: "probe",
      type: "text",
      value: "different",
      default: "",
      options: [],
    };
    const built = window.eval("buildFieldControl")(probeField);
    const host = doc.querySelector('.admin-view[data-view="web_search"]') || doc.body;
    host.appendChild(built.control);
    built.input.dataset.original = "loaded";
    out.untouchedProbe = {
      valueDiffersFromOriginal: built.input.value !== built.input.dataset.original,
      beforeTouch: submits().SELECT_BLANK_UNTOUCHED_PROBE === undefined,
    };
    built.input.dispatchEvent(new window.Event("change", { bubbles: true }));
    const afterTouch = submits().SELECT_BLANK_UNTOUCHED_PROBE;
    out.untouchedProbe.afterTouch = afterTouch === undefined ? null : afterTouch;
    built.control.remove();

    out.scriptErrors = scriptErrors;
    out.consoleErrors = consoleErrors;
    return out;
  }

  return { run };
}

/* ---------------------------------------------------------------- sha256
   `node:crypto`, not a hand-rolled digest: the digest is compared against a
   committed baseline generated on a different machine, so it has to be the
   standard algorithm rather than something that merely looks like one. */
const sha256 = (text) => createHash("sha256").update(text, "utf8").digest("hex");
