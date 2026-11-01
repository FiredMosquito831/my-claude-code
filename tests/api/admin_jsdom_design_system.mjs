/**
 * The 8.1.0 design-system foundation, proved in jsdom
 * (MCC_JSDOM_SCENARIO=design_system).
 *
 * This release adds two things a reader can see — the per-page section
 * navigator, and the Appearance card with its seven theme families — plus the
 * storage machinery behind both. What this scenario measures, in one run:
 *
 *  1. `navigator` — for every page the shipped table covers: its label list,
 *     whether every selector resolves to a real element, where each button
 *     lands when clicked, the URL hash it sets, and the `aria-current` mark.
 *  2. `rails` — that the three 7.122.3 topbar rails now live INSIDE the strip
 *     and not in the topbar, and that no page shows two mechanisms.
 *  3. `theme` — the family × mode attributes, the AA-corrected values, and the
 *     legacy `mcc-theme` → (family, mode) migration for all four values and
 *     for no value at all.
 *  4. `appearance` — the card's controls: what clicking each writes to
 *     localStorage, what it does to `<html>`, and that none of them applies a
 *     setting.
 *  5. `density` — the resolved `data-density` under each choice.
 *  6. `surface` — the layout facts jsdom can see: that the strip is the view's
 *     first child and that the base markup under it is unchanged.
 *
 * The base-written half: the same file runs against the BASE tree's
 * admin_static (v8.0.2), where none of this exists, and `stripFree` digests
 * the page with the two new features taken out. Those two digests are the
 * equality baseline (`design_system_dom_8_1_0.json`).
 *
 * Prints nothing itself: `run()` returns the measurements and the harness
 * prints them as one JSON object.
 */

import { createHash } from "node:crypto";

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const sha256 = (text) => createHash("sha256").update(text, "utf8").digest("hex");
const flat = (node) => (node ? (node.textContent || "").replace(/\s+/g, " ").trim() : "");

/** The Appearance card's own id and the strip's class, in one place. */
const STRIP = ".section-navigator";
const CARD = "#appearanceCard";

export function prepareDesignSystemScenario({ window, ROUTES }) {  const doc = window.document;
  /* The page's own handle on its appearance machinery. `setActiveView`,
     `resolveAppearance` and the tables are script-level bindings an indirect
     eval does not publish, and re-evaluating admin.js would re-run every
     start-up path — so the page exposes them through one function that has no
     behaviour of its own (designSystemHarness() in admin.js).
     On the BASE tree that function does not exist (the base tree is v8.0.2,
     which has no design system at all), so every accessor degrades to
     something the base tree can answer. That is what lets ONE scenario file
     produce both halves of the proof: the same code, on both trees. */
  const hasPage = () => typeof window.designSystemHarness === "function";
  const PAGE = () => (hasPage() ? window.designSystemHarness() : null);
  const NAV_TABLE = () =>
    (PAGE() && PAGE().SECTION_NAVIGATOR) || window.SECTION_NAVIGATOR || {};
  const openView = (id) => {
    if (PAGE()) PAGE().setActiveView(id, { scroll: false });
    else if (typeof window.setActiveView === "function") {
      window.setActiveView(id, { scroll: false });
    }
  };
  const appearanceState = () => (PAGE() ? PAGE().state.appearance : null);

  const view = (id) => doc.querySelector(`#view-${id}`);
  const strip = (id) => {
    const host = view(id);
    return host ? host.querySelector(`:scope > ${STRIP}`) : null;
  };

  /** The DOM of a page with the two new features taken out.
   *
   * Removal is by the same selectors the features are built with, on a CLONE,
   * so the bytes this digests are the bytes the base tree produced plus
   * nothing. The card's machine section is taken out with it: the four
   * ADMIN_* controls are new markup on Get Started as well.
   *
   * Two interaction leftovers are cleared as well, and neither is markup a
   * page ships: `onboarding-highlight` is the two-second pulse a jump adds,
   * and `tabindex="-1"` is what the same jump adds so a keyboard can carry on
   * from the target. Both are the residue of being clicked, not the page, and
   * a digest that included them would compare two instants of the same page.
   */
  const stripFreeHtml = (host) => {
    const clone = host.cloneNode(true);
    clone.querySelectorAll(STRIP).forEach((node) => node.remove());
    clone.querySelectorAll(CARD).forEach((node) => node.remove());
    const clean = (node) => {
      node.classList.remove("onboarding-highlight");
      // `classList.remove` leaves `class=""` behind on an element that had no
      // class of its own, and an empty class attribute is not a difference in
      // the page — it is a difference in serialisation. Dropped, always.
      if (!node.className) node.removeAttribute("class");
      if (node.getAttribute("tabindex") === "-1") node.removeAttribute("tabindex");
    };
    clean(clone);
    clone.querySelectorAll(".onboarding-highlight, [tabindex='-1']").forEach(clean);
    /* HTML comments are stripped from both sides. The Appearance card's own
       explanatory comment is not a rendered thing, and the base tree cannot
       have one: it is a sentence about markup the base tree does not carry. */
    const walker = doc.createTreeWalker(clone, 128 /* NodeFilter.SHOW_COMMENT */);
    const comments = [];
    while (walker.nextNode()) comments.push(walker.currentNode);
    comments.forEach((node) => node.remove());
    // A clock is not markup. The Analytics page stamps every panel with the
    // moment it last computed, and the two trees run minutes apart, so the
    // digest is taken with every rendered time replaced by one placeholder --
    // on BOTH sides, so the comparison is still of the same bytes.
    return clone.innerHTML
      .replace(/\r\n/g, "\n")
      // Whitespace left where a removed comment used to sit is not markup
      // either. Collapsed on both sides, so it can never be the difference.
      .replace(/[ \t]+\n/g, "\n")
      .replace(/\n{2,}/g, "\n")
      .replace(
        /\b\d{1,2}:\d{2}:\d{2}(?:\s?[AP]M)?\b/g,
        "__TIME__",
      )
      .replace(
        /\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?Z?\b/g,
        "__STAMP__",
      );
  };

  /** Wait until the page stops changing.
   *
   * Two identical captures 150 ms apart. Every click this scenario makes
   * (the navigator's buttons, the card's) leaves two transient marks behind —
   * the `onboarding-highlight` pulse and the `tabindex="-1"` the jump adds so
   * a keyboard can carry on from the target — and both are gone within a few
   * seconds. Digests taken mid-pulse would compare two different instants of
   * the same page, which is noise, not a difference.
   */
  const signature = () =>
    doc.querySelectorAll(".admin-view[data-view]").length +
    ":" +
    doc.querySelectorAll("[tabindex='-1']").length +
    ":" +
    doc.querySelectorAll(".onboarding-highlight").length;
  const settle = async () => {
    let previous = null;
    for (let attempt = 0; attempt < 40; attempt += 1) {
      const current = signature();
      if (current === previous) return;
      previous = current;
      await wait(150);
    }
  };

  /** The digests, for every view.
   *
   * Each view is OPENED first and its own lazy load given time to land, so
   * the digest is of the page a reader sees rather than of the page a fetch
   * has not filled in yet. Doing it per view is what makes the two trees
   * comparable: they run the same scenario in the same order and wait for the
   * same marker, so a slower machine moves both runs rather than one.
   */
  const READY = {
    models: "#modelsTree .models-tree-row, #modelsTree .models-tree-card, #modelsTree *",
    docs: "#docsContent h1, #docsContent h2, #docsContent h3",
    requests: "#reqLogTableWrap table, #reqStatsCards *",
    web_search: "#webSearchGrid *",
    providers: "#providersSections .pv-card, #providersSections *",
    coding_agents: "#codingAgentsList .coding-agent-card",
    limits: "#limitsSections .settings-section",
    optimizer: "#optKpis *",
    claude: "#claudeSettingsStatus *",
    model_config: "#modelConfigSections .settings-section",
    proxying: "#proxyingList *",
    marketplaces: "#marketplaceAccountBody *",
    guide: ".guide-body h3",
    get_started: "#getStartedFeatures *",
    messaging: "#messagingSections *",
  };
  const captureViews = async () => {
    const out = {};
    const hosts = Array.from(doc.querySelectorAll(".admin-view[data-view]"));
    for (const host of hosts) {
      const viewId = host.dataset.view;
      openView(viewId);
      const marker = READY[viewId];
      const began = Date.now();
      while (
        marker &&
        Date.now() - began < 8000 &&
        !doc.querySelector(marker)
      ) {
        await wait(50);
      }
      await wait(80);
      const html = host.innerHTML.replace(/\r\n/g, "\n");
      const without = stripFreeHtml(host);
      out[viewId] = {
        children: host.children.length,
        length: html.length,
        sha256: sha256(html),
        stripFreeSha256: sha256(without),
        stripFreeLength: without.length,
      };
    }
    return out;
  };

  /** Walk every page the shipped table covers and click every button. */
  const captureNavigator = async () => {
    const table = NAV_TABLE();
    const out = {};
    Object.keys(table).forEach((viewId) => {
      openView(viewId);
      const nav = strip(viewId);
      if (!nav) {
        out[viewId] = { present: false };
        return;
      }
      const buttons = Array.from(
        nav.querySelectorAll("button[data-section-target]"),
      );
      const entries = buttons.map((button) => {
        const selector = button.dataset.sectionTarget;
        const resolved = (() => {
          try {
            return Boolean(doc.querySelector(selector));
          } catch (_) {
            return false;
          }
        })();
        /* A `section-<id>` selector is RENDERED from the settings payload, so
         * whether it resolves depends on which sections the server sent. The
         * harness's fixture carries no fields for `reasoning`, `web_tools` or
         * `catalogue`, so renderSections() draws no card for them and the
         * button has nowhere to land — in jsdom only. What the table must
         * guarantee is that the selector names a section its page's
         * VIEW_GROUPS claims; whether the payload happens to fill it is a
         * different question, asked (and answered) by every other page's
         * tests. `renderable` carries that distinction so the Python side can
         * assert on it instead of on this run's fixture.
         */
        const sectionMatch = /^#section-([a-z_]+)$/.exec(selector);
        const claimed = sectionMatch
          ? ((PAGE() && PAGE().VIEW_GROUPS) || [])
              .filter((entry) => entry.id === viewId)
              .flatMap((entry) => entry.sections || [])
              .includes(sectionMatch[1])
          : null;
        return {
          label: flat(button),
          selector,
          resolves: resolved,
          resolvedId: resolved ? doc.querySelector(selector).id || null : null,
          declared: table[viewId].some((entry) => entry[1] === selector),
          sectionId: sectionMatch ? sectionMatch[1] : null,
          claimed,
        };
      });
      // Every button, clicked, one at a time. The landing element is read
      // from what the page itself marks: the highlight class it adds, and
      // the hash it writes.
      const landings = [];
      buttons.forEach((button) => {
        const selector = button.dataset.sectionTarget;
        const target = doc.querySelector(selector);
        const before = window.location.hash;
        button.click();
        landings.push({
          label: flat(button),
          selector,
          // A button whose target the fixture never rendered cannot land on
          // it; `entries[].claimed` above is what proves the selector is the
          // page's own section rather than a typo.
          resolvable: Boolean(target),
          highlighted: target ? target.classList.contains("onboarding-highlight") : null,
          focused: target ? doc.activeElement === target : null,
          hashBefore: before,
          hash: window.location.hash,
          current: Array.from(
            nav.querySelectorAll('button[aria-current="true"]'),
          ).map((node) => node.dataset.sectionTarget),
        });
      });
      out[viewId] = {
        present: true,
        id: nav.id,
        hidden: nav.hidden,
        firstChild: view(viewId).firstElementChild === nav,
        labelCount: buttons.length,
        labels: buttons.map(flat),
        entries,
        landings,
        rails: (nav.dataset.rails || "").split(" ").filter(Boolean),
      };
    });
    return out;
  };

  /** Where the three old rails ended up. */
  const captureRails = () => {
    const ids = ["providersJumpBar", "proxyingJumpBar", "reqJumpBar"];
    const railIds = ["providersJumpBar", "proxyingJumpBar", "reqJumpBar"];
    const rails = {};
    ids.forEach((id) => {
      const node = doc.getElementById(id);
      rails[id] = {
        exists: Boolean(node),
        insideStrip: Boolean(node && node.closest(STRIP)),
        inTopbar: Boolean(node && node.closest(".topbar")),
        hidden: node ? node.hidden : null,
        links: node ? node.querySelectorAll("a").length : 0,
      };
    });
    // No page may show both: a view whose strip is visible must not also
    // have a visible rail outside it.
    const both = [];
    doc.querySelectorAll(".admin-view[data-view]").forEach((host) => {
      const nav = host.querySelector(`:scope > ${STRIP}`);
      if (!nav || nav.hidden) return;
      host.querySelectorAll(".jump-bar, .toc-rail").forEach((rail) => {
        if (!nav.contains(rail)) both.push(`${host.dataset.view}:${rail.id || rail.className}`);
      });
    });
    const topbarBars = doc.querySelectorAll(".topbar .jump-bar").length;
    return { declared: railIds, rails, both, topbarBars };
  };

  /** The theme attributes the page resolved for the storage it found. */
  const captureTheme = () => {
    const root = doc.documentElement;
    const style = window.getComputedStyle(root);
    const read = (name) => style.getPropertyValue(name).trim();
    const local = (key) => {
      try {
        return window.localStorage.getItem(key);
      } catch (_) {
        return null;
      }
    };
    return {
      family: root.dataset.mccTheme || null,
      mode: root.dataset.mccMode || null,
      design: root.dataset.design || null,
      density: root.dataset.density || null,
      legacyAttribute: root.dataset.theme || null,
      resolved: appearanceState()
        ? {
            design: appearanceState().design,
            theme: appearanceState().theme,
            mode: appearanceState().mode,
            density: appearanceState().density,
            resolvedDensity: appearanceState().resolvedDensity,
            source: { ...appearanceState().source },
          }
        : null,
      tokens: {
        bg: read("--bg"),
        accent: read("--accent"),
        accentOn: read("--accent-on"),
        warn: read("--warn"),
        focus: read("--focus"),
      },
      storage: {
        design: local("mcc-admin-design"),
        theme: local("mcc-admin-theme"),
        mode: local("mcc-admin-mode"),
        density: local("mcc-admin-density"),
        legacy: local("mcc-theme"),
      },
    };
  };

  /** Probe the resolver with a storage state the page did not start with.
   *
   * `machine` seeds the config payload's four ADMIN_* fields for the call, so
   * the third layer can be probed without a server. It is put back before the
   * next read, exactly like the storage keys.
   */
  const probeStorage = (legacy, viewer, machine) => {
    if (!PAGE() || !PAGE().resolveAppearance) {
      return { unsupported: true, legacy, viewer: viewer || {}, machine: machine || {} };
    }
    const keys = [
      "mcc-theme",
      "mcc-admin-theme",
      "mcc-admin-mode",
      "mcc-admin-design",
      "mcc-admin-density",
    ];
    keys.forEach((key) => window.localStorage.removeItem(key));
    if (legacy !== null) window.localStorage.setItem("mcc-theme", legacy);
    Object.entries(viewer || {}).forEach(([key, value]) => {
      window.localStorage.setItem(`mcc-admin-${key}`, value);
    });
    const fields = PAGE().state.fields;
    const saved = new Map();
    Object.entries(machine || {}).forEach(([axis, value]) => {
      const key = `ADMIN_${axis.toUpperCase()}`;
      if (fields.has(key)) saved.set(key, fields.get(key));
      fields.set(key, { ...(saved.get(key) || { key, type: "select" }), value });
    });
    const resolved = PAGE().resolveAppearance();
    // Put the reader's real state back before anything else reads it.
    keys.forEach((key) => window.localStorage.removeItem(key));
    saved.forEach((field, key) => fields.set(key, field));
    Object.entries(machine || {}).forEach(([axis]) => {
      const key = `ADMIN_${axis.toUpperCase()}`;
      if (!saved.has(key)) fields.delete(key);
    });
    const design = PAGE().ADMIN_DESIGNS.find((row) => row.id === resolved.values.design);
    return {
      legacy,
      viewer: viewer || {},
      machine: machine || {},
      values: { ...resolved.values },
      source: { ...resolved.source },
      resolvedDensity:
        resolved.values.density === "design"
          ? (design && design.defaultDensity) || "comfortable"
          : resolved.values.density,
    };
  };

  /** The Appearance card: what each control writes and what it never does. */
  const captureAppearance = async () => {
    const card = doc.querySelector(CARD);
    if (!card) return { present: false };
    const out = {
      present: true,
      tiles: [],
      options: { family: [], mode: [], density: [], design: [] },
      applyCallsBefore: (window.fetchCalls || []).filter((call) =>
        String(call.url || call).includes("/admin/api/config/apply"),
      ).length,
      clicks: [],
    };
    card.querySelectorAll(".appearance-tile").forEach((tile) => {
      out.tiles.push({
        id: tile.dataset.designId,
        name: flat(tile.querySelector(".appearance-tile-name")),
        note: flat(tile.querySelector(".appearance-tile-note")),
        badge: flat(tile.querySelector(".appearance-tile-badge")) || null,
        checked: tile.getAttribute("aria-checked"),
        role: tile.getAttribute("role"),
        hasPreview: Boolean(tile.querySelector(".appearance-preview")),
      });
    });
    [
      ["family", '[data-appearance-axis="theme"]'],
      ["mode", '[data-appearance-axis="mode"]'],
      ["density", '[data-appearance-axis="density"]'],
    ].forEach(([group, selector]) => {
      card.querySelectorAll(selector).forEach((button) => {
        out.options[group].push({
          value: button.dataset.appearanceValue,
          label: flat(button),
          checked: button.getAttribute("aria-checked"),
        });
      });
    });
    out.options.design = out.tiles.map((tile) => tile.id);
    out.machineSectionPresent = Boolean(doc.getElementById("appearanceMachineSections"));
    /* The four machine defaults are ordinary manifest fields, so whether their
     * controls exist depends on whether the payload carried the `appearance`
     * section — the harness's fixture is a fixed list of sections and carries
     * none of them. `fields` records whether the payload offered the key at
     * all, which is what separates "the page cannot draw it" from "this
     * payload did not include it".
     */
    const payloadFields = (PAGE() && PAGE().state.fields) || new Map();
    out.machineControls = [
      "ADMIN_DESIGN",
      "ADMIN_THEME",
      "ADMIN_MODE",
      "ADMIN_DENSITY",
    ].map((key) => {
      const control = doc.querySelector(`#field-${key}`);
      return {
        key,
        inPayload: payloadFields.has(key),
        exists: Boolean(control),
        tag: control ? control.tagName.toLowerCase() : null,
        options: control && control.options
          ? Array.from(control.options).map((option) => option.value)
          : [],
        value: control ? control.value : null,
      };
    });

    // Click each axis once and record what it wrote and what it painted.
    const clickAxis = async (axis, value) => {
      const button = card.querySelector(
        `[data-appearance-axis="${axis}"][data-appearance-value="${value}"]`,
      );
      if (!button) return null;
      button.click();
      await wait(20);
      return {
        axis,
        value,
        storage: window.localStorage.getItem(`mcc-admin-${axis}`),
        family: doc.documentElement.dataset.mccTheme,
        mode: doc.documentElement.dataset.mccMode,
        density: doc.documentElement.dataset.density || null,
        design: doc.documentElement.dataset.design,
        checked: Array.from(
          card.querySelectorAll(
            `[data-appearance-axis="${axis}"][aria-checked="true"]`,
          ),
        ).map((node) => node.dataset.appearanceValue),
      };
    };
    out.clicks.push(await clickAxis("theme", "paper"));
    out.clicks.push(await clickAxis("mode", "light"));
    out.clicks.push(await clickAxis("mode", "dark"));
    out.clicks.push(await clickAxis("theme", "velvet"));
    out.clicks.push(await clickAxis("density", "compact"));
    out.clicks.push(await clickAxis("density", "design"));
    const designTile = card.querySelector(".appearance-tile");
    if (designTile) {
      designTile.click();
      await wait(20);
      out.clicks.push({
        axis: "design",
        value: designTile.dataset.designId,
        storage: window.localStorage.getItem("mcc-admin-design"),
        design: doc.documentElement.dataset.design,
      });
    }
    // "Follow the machine" clears all four.
    const follow = doc.getElementById("appearanceFollowMachine");
    if (follow) {
      follow.click();
      await wait(20);
      out.followMachine = {
        design: window.localStorage.getItem("mcc-admin-design"),
        theme: window.localStorage.getItem("mcc-admin-theme"),
        mode: window.localStorage.getItem("mcc-admin-mode"),
        density: window.localStorage.getItem("mcc-admin-density"),
        family: doc.documentElement.dataset.mccTheme,
        mode2: doc.documentElement.dataset.mccMode,
      };
    }
    out.applyCallsAfter = (window.fetchCalls || []).filter((call) =>
      String(call.url || call).includes("/admin/api/config/apply"),
    ).length;
    return out;
  };

  async function run({ scriptErrors, consoleErrors }) {
    const out = { scenarioFatal: null, scriptErrors, consoleErrors };
    /* `MCC_JSDOM_DS_FEATURE` is this scenario's own switch, and it is the
       half of the proof that makes the other half meaningful.
         `disabled` — take the two new visible features back out before the
                      digests are taken. The per-page section navigator is
                      removed, and so is the Appearance card. What is left is
                      the page the BASE tree draws, which is the equality
                      baseline.
         `enabled`  — the page as a reader gets it. The deltas this produces
                      are the release's own, declared view by view.
       It is an environment variable rather than a second scenario so both
       halves run exactly the same code path over exactly the same fixtures,
       which is what makes the comparison mean anything. */
    const feature = process.env.MCC_JSDOM_DS_FEATURE || "enabled";
    out.feature = feature;
    // `load()` is async and the last thing it does is draw the Coding agents
    // cards; their presence means the page is the one a reader sees.
    const started = Date.now();
    while (
      Date.now() - started < 20000 &&
      !doc.querySelector("#codingAgentsList .coding-agent-card")
    ) {
      await wait(50);
    }
    // And the Docs page loads its first document on demand, so the digest is
    // taken only once that has landed. Both trees wait the same way, which is
    // what keeps the comparison a comparison.
    const docsStarted = Date.now();
    while (
      Date.now() - docsStarted < 15000 &&
      !doc.querySelector("#view-docs #docsContent h1, #view-docs #docsContent h2")
    ) {
      await wait(50);
    }
    await settle();

    out.themeOnLoad = captureTheme();
    out.navigator = await captureNavigator();
    out.rails = captureRails();
    out.appearance = await captureAppearance();
    // And the theme probes, with the reader's own state restored after each.
    out.probes = {
      nothingSaved: probeStorage(null, {}),
      midnight: probeStorage("midnight", {}),
      paper: probeStorage("paper", {}),
      highContrast: probeStorage("high-contrast", {}),
      velvet: probeStorage("velvet", {}),
      unknownLegacy: probeStorage("chartreuse", {}),
      badViewerValue: probeStorage(null, { theme: "not-a-family" }),
      viewerBeatsLegacy: probeStorage("paper", { theme: "velvet" }),
      emptyViewerFollowsMachine: probeStorage(null, { theme: "" }, { theme: "navy" }),
      viewerBeatsMachine: probeStorage(null, { theme: "paper" }, { theme: "navy" }),
      legacyBeatsMachine: probeStorage("paper", {}, { theme: "navy" }),
      machineBeatsCompiled: probeStorage(null, {}, {
        design: "today",
        theme: "navy",
        mode: "light",
        density: "compact",
      }),
      badMachineValueIgnored: probeStorage(null, {}, { theme: "chartreuse" }),
      viewerDensity: probeStorage(null, { density: "compact" }),
      viewerDesign: probeStorage(null, { design: "today" }),
      lightModeFollowsLightTheme: probeStorage(null, { mode: "light" }),
    };
    // Restore whatever the page started with, so the captures below are the
    // page a reader sees and not one a probe left behind. On the base tree
    // there is nothing to restore.
    if (PAGE()) {
      PAGE().applyAppearance(PAGE().resolveAppearance());
      PAGE().renderAppearanceCard();
    }
    // A jump's marks are transient; wait for them to clear before digesting.
    await settle();

    if (feature === "disabled") {
      // The two visible features, taken back out. Removing them is not a
      // mock of "what the base tree did": it is the same DOM the base tree
      // produced, element for element, because nothing else was added.
      doc.querySelectorAll(STRIP).forEach((node) => node.remove());
      doc.querySelectorAll(CARD).forEach((node) => node.remove());
    }

    out.views = await captureViews();
    /* And the same views with the two features taken out of the CLONE. On the
       `disabled` run this is identical to `views` by construction; on the
       `enabled` run it is the pair the release's own delta is measured
       between — full digest with them, strip-free digest without — taken from
       ONE run so the two numbers describe the same page at the same moment. */
    out.feature_off_views = {};
    Object.keys(out.views).forEach((viewId) => {
      const host = view(viewId);
      const without = stripFreeHtml(host);
      out.feature_off_views[viewId] = {
        length: without.length,
        sha256: sha256(without),
      };
    });
    out.themeAfterProbes = captureTheme();
    // A debugging aid, never used in CI.
    const dump = process.env.MCC_JSDOM_DS_DUMP;
    if (dump) {
      const host = view(dump);
      out.html = host
        ? {
            [dump]: process.env.MCC_JSDOM_DS_DUMP_STRIPFREE
              ? stripFreeHtml(host)
              : host.innerHTML,
          }
        : {};
    }
    return out;
  }

  return { run };
}
