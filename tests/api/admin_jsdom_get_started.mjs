/**
 * Get Started (7.122.0), run instead of the main capture when
 * MCC_JSDOM_SCENARIO=get_started.
 *
 * The payloads are not written here: `MCC_JSDOM_GET_STARTED_FILE` names a
 * JSON file the Python test writes with the server's own answers -- the
 * `/admin/api/config` payload its renderers build (`tests/support/
 * select_blank_payload.py`), the onboarding payloads `build_state` +
 * `_onboarding_state_response` really return for each state, and the few
 * values the page reads off those payloads (provider status, route status,
 * harness tiers, the manual-only web-search ids). So the page is drawn from
 * the strings the server sends and cannot drift from the manifest or from
 * `config/onboarding.py`.
 *
 * What it measures, by driving the real page with real clicks:
 *
 *  - the four essentials: pill, extra line, and the body's instructions, for
 *    a fresh install, a launcher-only Claude Code user (step 3 done from
 *    traffic), a user whose only Claude settings file is inside WSL, and a
 *    user whose request log is off (step 4 "Not measured", three counted);
 *  - the nine main cards and their status chips, the collapsed niche list
 *    with its own filter (titles and one-liners only), and the collapsed
 *    advanced section with the CURRENT value of each setting and never a
 *    suggested one;
 *  - the raw-log warning: absent with every switch off, present with one on,
 *    and its "Switch them off" lands on the switch that is on;
 *  - "Not for me" -> the server's `skipped` list -> the card gone -> "Show
 *    hidden (n)" -> the card back, all through POST /admin/api/onboarding
 *    (never localStorage), and Dismiss -> "Undo dismiss" -> back;
 *  - every Open button: clicked, and the element it scrolled to (the page's
 *    own scrollIntoView is instrumented) is the target it declares, inside
 *    the view it declares, and not inside a collapsed fold;
 *  - zero POSTs to /admin/api/config/apply or /validate while every control
 *    on the page is clicked: this page writes no setting;
 *  - every view's DOM, so the Python test can prove the page other than Get
 *    Started is the BASE tree's page byte for byte.
 *
 * On the BASE tree admin.js has none of this; the scenario detects that,
 * reports `baseTree: true`, and captures only the per-view DOM the baseline
 * is made of. Prints nothing itself: `run()` returns the measurements and the
 * harness prints one JSON object.
 */

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const sha256 = (text) => createHash("sha256").update(text, "utf8").digest("hex");
const flat = (el) => (el ? (el.textContent || "").replace(/\s+/g, " ").trim() : "");

/** The row ids the page must draw, so a dropped row fails here, not silently. */
const MAIN_CARDS = 9;
const MORE_ROWS = 22;

export function prepareGetStartedScenario({ window, ROUTES }) {
  const doc = window.document;

  const filePath = process.env.MCC_JSDOM_GET_STARTED_FILE || "";
  if (!filePath) {
    throw new Error("MCC_JSDOM_GET_STARTED_FILE is not set: nothing to render");
  }
  const world = JSON.parse(readFileSync(filePath, "utf8"));
  const config = world.config;
  if (!config || !Array.isArray(config.fields)) {
    throw new Error("the world file carries no config payload");
  }
  ROUTES["/admin/api/config"] = config;
  if (world.world.provider_status) {
    config.provider_status = world.world.provider_status;
  }
  if (world.world.route_status) config.route_status = world.world.route_status;
  if (world.world.websearch_manual_only) {
    config.websearch_manual_only = world.world.websearch_manual_only;
  }
  if (world.world.harness_tiers) {
    ROUTES["/admin/api/harness-tiers"] = world.world.harness_tiers;
  }
  if (world.world.desktop_apps) ROUTES["/admin/api/desktop-apps"] = world.world.desktop_apps;
  if (world.world.marketplaces) ROUTES["/admin/api/marketplaces?summary=true"] = world.world.marketplaces;

  /* Detected in run(), not here: prepare() runs before the page's script is
     evaluated, so at this point no function of the page exists on either tree. */
  let baseTree = false;

  /* The onboarding answer, served from a state this scenario owns: a POST
     updates the state and the next GET answers it, which is what makes the
     hidden/dismiss round trip a server-state round trip. */
  const server = {
    dismissed: false,
    visited: [],
    skipped: [],
    variant: "fresh",
  };
  const onboardingFile = world.onboarding || {};
  const answerFor = () => {
    const payload = JSON.parse(JSON.stringify(onboardingFile[server.variant] || {}));
    payload.dismissed = Boolean(server.dismissed);
    payload.skipped = [...server.skipped];
    payload.steps = (payload.steps || []).map((step) => ({
      ...step,
      done: step.id === "guide" ? server.visited.includes("guide") : step.done,
    }));
    return payload;
  };
  ROUTES["/admin/api/onboarding"] = answerFor();

  const onboardingPosts = [];
  const settingWrites = [];
  const realFetch = window.fetch;
  window.fetch = async (url, options = {}) => {
    const path = String(url).split("?")[0];
    const method = String((options && options.method) || "GET").toUpperCase();
    if (path === "/admin/api/config/apply" || path === "/admin/api/config/validate") {
      settingWrites.push({ path, method, body: options.body || null });
    }
    if (path === "/admin/api/onboarding" && method === "POST") {
      let patch = null;
      try {
        patch = JSON.parse(options.body || "{}");
      } catch {
        patch = null;
      }
      onboardingPosts.push(patch);
      if (patch) {
        if (patch.dismissed !== undefined) server.dismissed = Boolean(patch.dismissed);
        if (Array.isArray(patch.visited)) {
          server.visited = [...new Set([...server.visited, ...patch.visited])];
        }
        if (Array.isArray(patch.skipped)) server.skipped = [...patch.skipped];
      }
      ROUTES["/admin/api/onboarding"] = answerFor();
      const answer = answerFor();
      return {
        ok: true,
        status: 200,
        json: async () => JSON.parse(JSON.stringify(answer)),
        text: async () => JSON.stringify(answer),
      };
    }
    return realFetch(url, options);
  };

  /* Instrument scrollIntoView: the page's whole claim about a deep link is
     that it scrolls the RIGHT element into view. jsdom has no layout, so the
     call itself is the observable fact. */
  const scrolled = [];
  const realScroll = window.Element.prototype.scrollIntoView;
  window.Element.prototype.scrollIntoView = function scrollIntoView(...args) {
    scrolled.push({ element: this, options: args[0] || null });
    if (typeof realScroll === "function") return realScroll.apply(this, args);
    return undefined;
  };

  const viewOf = (el) => {
    const view = el ? el.closest(".admin-view[data-view]") : null;
    return view ? view.dataset.view : null;
  };
  const collapsed = (el) => Boolean(el && el.closest("[hidden]"));
  const describe = (el) => {
    if (!el) return null;
    return {
      id: el.id || "",
      tag: el.tagName.toLowerCase(),
      className: typeof el.className === "string" ? el.className : "",
      dataKey: el.dataset.key || "",
      view: viewOf(el),
      hidden: collapsed(el),
    };
  };

  const getStartedVisible = () =>
    !doc.querySelector("#view-get_started").hidden;

  const backToGetStarted = async () => {
    if (typeof window.setActiveView === "function") {
      window.setActiveView("get_started", { scroll: false });
      await wait(30);
    }
  };

  /** Render one onboarding variant through the page's own loader. */
  const renderVariant = async (variant) => {
    server.variant = variant;
    ROUTES["/admin/api/onboarding"] = answerFor();
    if (typeof window.loadOnboarding === "function") {
      await window.loadOnboarding();
    }
    await backToGetStarted();
    await wait(60);
  };

  const waitFor = async (predicate, timeoutMs = 20000) => {
    const startedAt = Date.now();
    while (Date.now() - startedAt < timeoutMs) {
      if (predicate()) return true;
      await wait(50);
    }
    return false;
  };

  const progressText = () => flat(doc.getElementById("getStartedProgress"));

  const stepRows = () =>
    Array.from(doc.querySelectorAll("#getStartedSteps .get-started-step")).map((row) => ({
      id: row.dataset.step || "",
      pill: flat(row.querySelector(".status-pill")),
      state: row.querySelector("[data-get-started-step-state]")
        ? row.querySelector("[data-get-started-step-state]").dataset.getStartedStepState
        : null,
      label: flat(row.querySelector("strong")),
      line: flat(row.querySelector(".get-started-step-line")),
      expanded: row.classList.contains("expanded"),
    }));

  const cards = () =>
    Array.from(doc.querySelectorAll("#getStartedFeatures .get-started-card")).map((card) => ({
      id: card.dataset.getStartedCard,
      title: flat(card.querySelector(".get-started-card-title")),
      pill: flat(card.querySelector(".status-pill")),
      state: card.querySelector("[data-get-started-state]")
        ? card.querySelector("[data-get-started-state]").dataset.getStartedState
        : null,
      detail: flat(card.querySelector(".get-started-card-detail")),
      line: flat(card.querySelector(".get-started-card-line")),
      opens: Array.from(card.querySelectorAll(".get-started-open")).map((button) => ({
        key: button.dataset.getStartedOpen,
        label: flat(button),
      })),
      guides: Array.from(card.querySelectorAll(".guide-link")).map((link) => ({
        purpose: link.dataset.guidePurpose,
        anchor: link.dataset.guideAnchor,
      })),
      hasStepNotes: Boolean(card.querySelector(".get-started-card-steps")),
    }));

  const rowActions = (row) => ({
    opens: Array.from(row.querySelectorAll(".get-started-open")).map((button) => ({
      key: button.dataset.getStartedOpen,
      label: flat(button),
    })),
    hide: row.querySelector("[data-get-started-hide]")
      ? flat(row.querySelector("[data-get-started-hide]"))
      : null,
  });

  const moreRows = () =>
    Array.from(doc.querySelectorAll("#getStartedMore .get-started-more-row")).map((row) => ({
      id: row.dataset.getStartedFeature,
      area: flat(row.querySelector(".get-started-more-area")),
      title: flat(row.querySelector("strong")),
      line: flat(row.querySelector(".get-started-more-line")),
      pill: flat(row.querySelector(".status-pill")),
      ...rowActions(row),
    }));

  const advancedGroups = () =>
    Array.from(doc.querySelectorAll("#getStartedAdvanced .get-started-advanced-group")).map(
      (group) => ({
        key: group.dataset.getStartedDisclosure,
        title: flat(group.querySelector(".get-started-disclosure-head strong")),
        open: group.querySelector(".get-started-disclosure-body").hidden === false,
        rows: Array.from(group.querySelectorAll(".get-started-advanced-row")).map((row) => ({
          id: row.dataset.getStartedFeature,
          title: flat(row.querySelector("strong")),
          values: Array.from(row.querySelectorAll("[data-get-started-value]")).map((chip) => ({
            key: chip.dataset.getStartedValue,
            text: flat(chip),
            title: chip.getAttribute("title") || "",
          })),
          ...rowActions(row),
        })),
      }),
    );

  const disclosure = (key) =>
    doc.querySelector(`[data-get-started-disclosure="${key}"] [role="button"]`);
  const disclosureBody = (key) =>
    doc.querySelector(`[data-get-started-disclosure="${key}"] .get-started-disclosure-body`);
  const isOpen = (key) => {
    const body = disclosureBody(key);
    return Boolean(body) && body.hidden === false;
  };
  const openDisclosure = async (key) => {
    if (isOpen(key)) return;
    const head = disclosure(key);
    if (head) head.click();
    await wait(30);
  };

  const rawWarning = () => {
    const warning = doc.getElementById("getStartedRawWarning");
    return warning
      ? { present: true, text: flat(warning), count: warning.dataset.getStartedRawOn }
      : { present: false };
  };

  const footer = () => ({
    dismiss: flat(doc.getElementById("getStartedDismissButton")),
    dismissed: doc.getElementById("getStartedDismissButton").dataset.getStartedDismissed,
    message: flat(doc.getElementById("getStartedMessage")),
    messageHidden: doc.getElementById("getStartedMessage").hidden,
    showHidden: doc.getElementById("getStartedShowHidden")
      ? flat(doc.getElementById("getStartedShowHidden"))
      : null,
  });

  /** Every view's innerHTML, line endings normalised. `get_started` is this
   *  release's subject; the Python test compares every other view. */
  const captureViews = () => {
    const views = {};
    doc.querySelectorAll(".admin-view[data-view]").forEach((view) => {
      const html = view.innerHTML.replace(/\r\n/g, "\n");
      views[view.dataset.view] = {
        children: view.children.length,
        length: html.length,
        sha256: sha256(html),
      };
    });
    return views;
  };

  async function run({ scriptErrors, consoleErrors }) {
    baseTree = typeof window.getStartedSnapshot !== "function";
    const out = {
      scenarioFatal: null,
      baseTree,
      scriptErrors,
      consoleErrors,
    };
    // `load()` is async and the onboarding answer is its first call.
    const drew = await waitFor(
      () =>
        doc.querySelectorAll("#getStartedSteps .get-started-step").length > 0 ||
        doc.querySelectorAll("#getStartedFeatures .get-started-card").length > 0,
    );
    if (!drew) {
      out.scenarioFatal = "the page never drew its essentials or its cards";
      return out;
    }
    // The whole page as `load()` left it, before any gesture: this is what the
    // Python test compares, view by view, with the BASE tree's run. Captured
    // here (not at the end) so the comparison is the same sequence on both
    // trees whatever this scenario goes on to click.
    await wait(900);
    out.viewsOnLoad = captureViews();

    if (baseTree) {
      out.applyCalls = settingWrites.length;
      return out;
    }

    // The raw-log warning is a second, small run: the switch is a config
    // value, and the page reads the config it loaded (spec section 5.6).
    if (process.env.MCC_JSDOM_GET_STARTED_MODE === "raw") {
      out.rawOnly = { warning: rawWarning() };
      const switchOff = doc.querySelector("#getStartedRawWarning .link-button");
      if (switchOff) {
        scrolled.length = 0;
        switchOff.click();
        await wait(40);
        out.rawOnly.landed = describe(
          scrolled.length ? scrolled[scrolled.length - 1].element : null,
        );
      }
      out.advancedGroups = advancedGroups();
      out.applyCalls = settingWrites.length;
      return out;
    }

    // ---- fresh install
    await renderVariant("fresh");
    out.fresh = {
      progress: progressText(),
      steps: stepRows(),
      cards: cards(),
      moreCount: doc.querySelectorAll("#getStartedMore .get-started-more-row").length,
      moreDisclosureOpen: isOpen("more"),
      advancedOpen: isOpen("advanced"),
      groups: advancedGroups(),
      rawWarning: rawWarning(),
      footer: footer(),
      lead: flat(doc.querySelector(".get-started-lead")),
      about: flat(doc.getElementById("getStartedAbout")),
    };
    out.cardCount = doc.querySelectorAll("#getStartedFeatures .get-started-card").length;
    out.mainCards = MAIN_CARDS;
    out.moreRows = MORE_ROWS;

    // ---- a launcher-only Claude Code user, and their first request
    await renderVariant("launcher");
    out.launcher = {
      steps: stepRows(),
      cards: cards(),
      requestLog: (typeof window.getStartedSnapshot === "function"
        ? window.getStartedSnapshot().requestLog
        : null),
    };

    // ---- the only Claude settings file is inside WSL
    await renderVariant("wsl");
    out.wsl = { steps: stepRows() };

    // ---- request logging off: step 4 is not measured and not counted
    await renderVariant("log_off");
    const analyticsRow = doc.querySelector('[data-step="analytics"]');
    // A step's body is drawn only while it is expanded: open it the way a
    // reader does, by its header.
    if (analyticsRow && !analyticsRow.classList.contains("expanded")) {
      analyticsRow.querySelector(".get-started-step-header")?.click();
      await wait(40);
    }
    const logOffStep = doc.querySelector('[data-step="analytics"]');
    out.logOff = {
      steps: stepRows(),
      progress: progressText(),
      analyticsBody: logOffStep
        ? Array.from(logOffStep.querySelectorAll(".get-started-step-body .link-button")).map(
            (button) => flat(button),
          )
        : [],
    };

    // ---- a payload with no steps at all must still render
    await renderVariant("empty");
    out.empty = {
      progressHidden: doc.getElementById("getStartedProgress").hidden,
      steps: stepRows().length,
      cards: cards().length,
      scriptErrors,
    };

    await renderVariant("launcher");

    // ---- the niche list's filter: titles and one-liners only
    await openDisclosure("more");
    const filter = doc.getElementById("getStartedFilter");
    const count = () => flat(doc.getElementById("getStartedFilterCount"));
    const empty = () => ({
      hidden: doc.getElementById("getStartedFilterEmpty").hidden,
      text: flat(doc.getElementById("getStartedFilterEmpty")),
    });
    const filterFrames = [];
    const typeQuery = async (query) => {
      filter.value = query;
      filter.dispatchEvent(new window.Event("input", { bubbles: true }));
      await wait(20);
      filterFrames.push({
        query,
        count: count(),
        rows: moreRows().map((row) => row.id),
        empty: empty(),
      });
    };
    await typeQuery("");
    await typeQuery("cache");
    await typeQuery("rotation");
    await typeQuery("zzz-nothing");
    await typeQuery("");
    out.filter = { count: count(), frames: filterFrames, rows: moreRows() };

    // ---- the advanced section: current values, three groups
    await openDisclosure("advanced");
    for (const key of ["limits", "raw", "testing"]) await openDisclosure(key);
    out.advanced = {
      open: isOpen("advanced"),
      groups: advancedGroups(),
      rawWarning: rawWarning(),
    };

    // ---- every Open button lands on the element it names
    const opens = [];
    const buttons = Array.from(doc.querySelectorAll(".get-started-open"));
    for (const button of buttons) {
      const key = button.dataset.getStartedOpen || "";
      await backToGetStarted();
      scrolled.length = 0;
      button.click();
      await wait(40);
      const last = scrolled.length ? scrolled[scrolled.length - 1].element : null;
      const described = describe(last);
      const className = described ? described.className.split(/\s+/).filter(Boolean) : [];
      opens.push({
        key,
        found: Boolean(last),
        element: described,
        inCollapsedFold: last ? Boolean(last.closest(".collapse-advanced")) : null,
        isTarget:
          !last
            ? false
            : key.includes(":key:")
              ? last === doc.getElementById(`field-${key.split(":key:")[1]}`) ||
                Boolean(last.querySelector && last.querySelector(`#field-${key.split(":key:")[1]}`))
              : key.includes(":section:")
                ? last.id === `section-${key.split(":section:")[1]}`
                : key.includes(":id:")
                  ? last.id === key.split(":id:")[1]
                  : true,
        classes: className,
      });
      // A Guide link is not clicked here; a target with no element on this
      // page is recorded as found: false rather than thrown.
    }
    out.opens = opens;
    out.openButtonCount = buttons.length;

    // ---- "Not for me" -> Show hidden, through the server's skipped list
    await backToGetStarted();
    const before = cards().map((card) => card.id);
    const hideButton = doc.querySelector('[data-get-started-hide="fallback_chains"]');
    if (hideButton) hideButton.click();
    await wait(120);
    const afterHide = cards().map((card) => card.id);
    const showHidden = doc.getElementById("getStartedShowHidden");
    if (showHidden) showHidden.click();
    await wait(120);
    out.hidden = {
      before,
      afterHide,
      afterShow: cards().map((card) => card.id),
      showHiddenLabelSeen: showHidden ? flat(showHidden) : null,
      posts: onboardingPosts.map((entry) => (entry ? { ...entry } : entry)),
    };

    // ---- Dismiss, its sentence, and Undo
    await backToGetStarted();
    doc.getElementById("getStartedDismissButton").click();
    await wait(120);
    const dismissedFooter = footer();
    doc.getElementById("getStartedDismissButton").click();
    await wait(120);
    out.dismiss = {
      dismissedFooter,
      undoneFooter: footer(),
      posts: onboardingPosts.map((entry) => (entry ? { ...entry } : entry)),
    };

    // ---- a Guide link: the Guide opens, and the visit is recorded
    await backToGetStarted();
    const guideLink = doc.querySelector("#getStartedFeatures .guide-link");
    if (guideLink) {
      guideLink.click();
      await wait(80);
      out.guideLink = {
        guideVisible: !doc.querySelector("#view-guide").hidden,
        purpose: guideLink.dataset.guidePurpose,
        anchor: guideLink.dataset.guideAnchor,
        visitedPosted: onboardingPosts.some(
          (entry) => entry && Array.isArray(entry.visited) && entry.visited.includes("guide"),
        ),
      };
      await backToGetStarted();
    }

    out.applyCalls = settingWrites.length;
    out.applyWrites = settingWrites;
    // Every key the page left in localStorage, so the Python test can prove
    // the hidden list is nowhere local: what a reader hid lives in MCC's
    // config folder, which is why the desktop app and every browser agree.
    out.localStorageKeys = (() => {
      try {
        return Object.keys(window.localStorage);
      } catch {
        return [];
      }
    })();
    out.getStartedVisibleAtEnd = getStartedVisible();
    return out;
  }

  return { run };
}
