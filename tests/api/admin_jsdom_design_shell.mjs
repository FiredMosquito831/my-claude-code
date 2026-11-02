/**
 * The design SHELL loader, proved in jsdom (MCC_JSDOM_SCENARIO=design_shell).
 *
 * 8.2.0 ships the machinery that makes a design a design: `loadDesignShell` in
 * admin.js, plus the first real design (`hybrid`). This scenario drives the
 * REAL loader — not a re-implementation of it — through four things a reader
 * would notice if they broke:
 *
 *  1. `roundTrip` — the page as `today` renders it, then `hybrid`, then `today`
 *     again, digested at each step. The third digest must equal the first.
 *     "Equal" is scoped to what a shell can touch (see `shellResidue` below and
 *     the note on `roundTrip.equal` in the scenario): the chrome outside the app
 *     shell, `<html>`'s attributes, `<body>`'s classes, the rail's markup with
 *     the design's decoration stripped, and every place a design-named class or
 *     attribute could survive — all identical, and a second trip changes nothing
 *     further. Per-view digests are REPORTED but not asserted: three pages carry
 *     a live clock stamp and a polled counter and move between two reads of the
 *     same page.
 *  2. `hybrid` — what the running shell actually built: the masthead, the
 *     numbered grouped rail, the palette over pages/sections/settings, the
 *     g-keys, the drawer's attributes.
 *  3. `switchUnderLoad` — two switches with no await between them, which is the
 *     reader clicking twice; exactly one shell must survive, and it must be
 *     the last one asked for.
 *  4. `failure` — a shell whose `start()` throws. The page must still work
 *     (the same nav, the same views), no leftover node may remain, and exactly
 *     one honest sentence must reach the Appearance card.
 *
 * HOW THE MODULE ARRIVES. jsdom hands the page its scripts as strings and
 * there is no server behind it, so `import("/admin/designs/hybrid.js")` cannot
 * resolve. The loader therefore fetches a shell through one named seam
 * (`importDesignShellModule`), and this scenario replaces THAT seam — with a
 * function that evaluates the real `designs/hybrid.js` (read from disk, by the
 * harness) in the page's own context, exactly as a module would run there. The
 * serialisation, the generation checks, the teardown and the failure path are
 * all the product's.
 */

import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { join } from "node:path";

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const sha256 = (text) => createHash("sha256").update(text, "utf8").digest("hex");
const flat = (node) =>
  node ? String(node.textContent || "").replace(/\s+/g, " ").trim() : "";

/** The real shell module, as source. Read once; the scenario evaluates it when
 *  `import()` would have. */
export function prepareDesignShellScenario({ window, ROUTES, staticDir }) {
  const doc = window.document;
  const hybridSource = readFileSync(join(staticDir, "designs", "hybrid.js"), "utf8");

  /** The page's own handle on the machinery (designSystemHarness in admin.js). */
  const PAGE = () => window.designSystemHarness();

  /** Digest the parts of the page a shell could disturb.
   *
   * Deliberately narrow. The Settings payload, the live clock stamps the
   * Analytics panels write and the message area are all moving on their own —
   * a digest over the whole document would be different on two reads of the
   * SAME page, and a proof that cannot distinguish "unchanged" from "noise"
   * proves nothing. What a shell can add is chrome (nodes outside `.app-shell`
   * and outside `.admin-view`s), attributes on `<html>`, classes on `<body>`,
   * and decoration inside the pages. Every one of those is in the digest.
   */
  const digestPage = () => {
    const root = doc.documentElement;
    const body = doc.body;
    const chrome = Array.from(body.children)
      .filter((node) => !node.classList.contains("app-shell"))
      .map((node) => node.outerHTML)
      .join("");
    const views = Array.from(doc.querySelectorAll(".admin-view")).map((view) => {
      // 8.2.0's shell decoration is the thing under test, so it is INCLUDED —
      // the point of the round trip is that it is gone again afterwards. The
      // product's own transient marks (a jump's pulse, its tabindex) are not
      // the shell's and are normalised out on both sides, exactly as the
      // design-system baseline does.
      const clone = view.cloneNode(true);
      clone.querySelectorAll(".onboarding-highlight").forEach((node) => {
        node.classList.remove("onboarding-highlight");
        if (!node.className) node.removeAttribute("class");
      });
      clone.querySelectorAll("[tabindex='-1']").forEach((node) => node.removeAttribute("tabindex"));
      return `${view.dataset.view}:${clone.innerHTML}`;
    });
    const rail = doc.getElementById("sectionNav");
    const attrs = Array.from(root.attributes)
      .map((attr) => `${attr.name}=${attr.value}`)
      .filter((pair) => !pair.startsWith("style="))
      .sort();
    return {
      chrome,
      views,
      // The rail's serialised markup WITH the shell's decoration in it, so the
      // trip can be read as "decorated while running, decorated nowhere after".
      rail: sha256(rail ? rail.innerHTML.replace(/\r\n/g, "\n") : ""),
      // ...and the same without it, which is the equality the base tree makes.
      railBare: railDigest(),
      railLinks: Array.from(doc.querySelectorAll("#sectionNav .nav-link")).map(
        (link) => `${link.dataset.view}${link.hidden ? ":hidden" : ""}`,
      ),
      bodyClass: body.className,
      htmlAttrs: attrs,
      shellNodes: doc.querySelectorAll("[data-mcc-shell]").length,
      mastheads: doc.querySelectorAll(".hy-masthead").length,
      railNumbers: doc.querySelectorAll(".hy-nav-num").length,
      railGroups: doc.querySelectorAll(".hy-nav-group").length,
      palette: doc.querySelectorAll(".hy-palette").length,
      residue: shellResidue(),
    };
  };

  /** A stable digest of one digestPage() result. */
  const digest = (page) => ({
    chrome: sha256(page.chrome),
    views: Object.fromEntries(
      page.views.map((entry) => [entry.split(":", 1)[0], sha256(entry)]),
    ),
    rail: page.rail,
    railLinks: page.railLinks,
    bodyClass: page.bodyClass,
    htmlAttrs: page.htmlAttrs,
    shellNodes: page.shellNodes,
    mastheads: page.mastheads,
    railNumbers: page.railNumbers,
    railGroups: page.railGroups,
    palette: page.palette,
    residue: page.residue,
  });

  /** Settle the loader: the chain, then the frame's worth of work it schedules. */
  const settleShell = async () => {
    await PAGE().designShellIdle();
    await wait(120);
    await PAGE().designShellIdle();
    await wait(60);
  };

  /** Collect every class and attribute the shell family could ever leave on a
   *  node. Used to prove a leak rather than to compare whole pages: three of
   *  the fifteen views carry a live clock stamp and a polled counter, so their
   *  bytes move on their own and a whole-view digest would report noise.
   *
   *  Everything a shell CAN add is in one of these four shapes, and each is a
   *  name no product code uses:
   *    - a `data-mcc-shell` attribute (what `api.mount` writes),
   *    - a `hy-`-prefixed class (this design's whole namespace),
   *    - the `hy-mode` body class,
   *    - a `data-sections|fields|detail|analytics|shell` attribute on <html>.
   *  A product node carrying none of them is a product node the shell did not
   *  touch, which is exactly the claim the round trip has to make. */
  const shellResidue = () => {
    const residue = { attributes: 0, classes: [], nodes: 0 };
    doc.querySelectorAll("[data-mcc-shell]").forEach(() => {
      residue.nodes += 1;
    });
    doc.querySelectorAll("*").forEach((node) => {
      node.classList.forEach((name) => {
        if (name.startsWith("hy-") || name === "hy-mode") {
          residue.classes.push(`${node.tagName.toLowerCase()}.${name}`);
        }
      });
    });
    ["sections", "fields", "detail", "analytics", "shell"].forEach((name) => {
      if (doc.documentElement.dataset[name] !== undefined) residue.attributes += 1;
    });
    return residue;
  };

  /** The rail, serialised WITHOUT the shell's own decoration.
   *
   * The rail is the one element the shell decorates in place, so a plain
   * innerHTML comparison would compare the decoration rather than the product's
   * markup. This strips the three things the shell adds and restores a label
   * span to the text node it replaced — which is the exact transformation
   * teardown performs, so equality here means teardown did its job. */
  const railDigest = () => {
    const nav = doc.getElementById("sectionNav");
    if (!nav) return "";
    const clone = nav.cloneNode(true);
    clone.querySelectorAll(".hy-nav-num, .hy-nav-group").forEach((node) => node.remove());
    clone.querySelectorAll(".hy-nav-text").forEach((span) => {
      span.replaceWith(doc.createTextNode(span.textContent));
    });
    return sha256(clone.innerHTML.replace(/\r\n/g, "\n"));
  };

  /** Switch design the way the Appearance card does, and wait for it to land. */
  const chooseDesign = async (id) => {
    window.localStorage.setItem("mcc-admin-design", id);
    PAGE().applyAppearance(PAGE().resolveAppearance());
    await settleShell();
  };

  async function run({ scriptErrors, consoleErrors }) {
    const out = { scenarioFatal: null, scriptErrors, consoleErrors };
    const page = PAGE();
    if (!page) {
      out.scenarioFatal = "designSystemHarness() is missing from admin.js";
      return out;
    }
    // Wait for the first render: the nav links are what every step below
    // counts, and the settings payload is what the Appearance card reads.
    const began = Date.now();
    while (Date.now() - began < 20000 && doc.querySelectorAll(".nav-link").length === 0) {
      await wait(50);
    }
    await wait(400);

    /* ------------------------------------------------- the transport seam -- */

    const transports = [];
    let hybridFailure = null;
    page.setDesignShellTransport(async (url) => {
      transports.push(url);
      if (url.endsWith("/designs/hybrid.js") && hybridFailure) {
        // A module that threw while evaluating — the same passing of the error
        // to the loader that a 404'd import produces.
        throw new Error("synthetic: " + hybridFailure);
      }
      if (url.endsWith("/designs/hybrid.js")) {
        // Evaluate the REAL file, in the page, as a module would be. A thrown
        // error propagates to the loader exactly as an import failure does.
        window.eval(`${hybridSource}\n//# sourceURL=${url}`);
        return;
      }
      throw new Error(`unexpected shell url ${url}`);
    });
    out.shellUrls = page.designAssetUrls({ css: "designs/hybrid.css", shell: "designs/hybrid.js" });
    out.transportsAfterTransportInstall = transports.slice();

    /* ---------------------------------------------------------- the round trip */

    window.localStorage.removeItem("mcc-admin-design");
    PAGE().applyAppearance(PAGE().resolveAppearance());
    await settleShell();
    const before = digest(digestPage());
    out.roundTrip = { before };

    await chooseDesign("hybrid");
    out.hybrid = {
      running: page.designShellState().running,
      failure: page.designShellState().failure,
      masthead: Boolean(doc.querySelector(".hy-masthead")),
      brand: flat(doc.querySelector(".hy-masthead .hy-brand")),
      sub: flat(doc.querySelector(".hy-masthead .hy-sub")),
      searchLabel: doc.querySelector(".hy-masthead .hy-search")
        ? doc.querySelector(".hy-masthead .hy-search").getAttribute("aria-label")
        : null,
      versionInMasthead: Boolean(
        doc.querySelector(".hy-masthead #versionIndicator"),
      ),
      versionInSidebar: Boolean(
        doc.querySelector(".sidebar #versionIndicator"),
      ),
      numbers: Array.from(doc.querySelectorAll("#sectionNav .hy-nav-num")).map(flat),
      groups: Array.from(doc.querySelectorAll("#sectionNav .hy-nav-group")).map(flat),
      // Which page each group heading sits before, which is the structure the
      // user approved: SETUP · AGENTS · ROUTING · OBSERVE · SYSTEM over the
      // numbered items.
      groupOrder: Array.from(doc.querySelectorAll("#sectionNav > *"))
        .map((node) =>
          node.classList.contains("hy-nav-group")
            ? `#${flat(node)}`
            : node.classList.contains("nav-link")
              ? node.dataset.view
              : null,
        )
        .filter(Boolean),
      bodyClass: doc.body.className,
      htmlAttrs: Array.from(doc.documentElement.attributes)
        .map((attr) => `${attr.name}=${attr.value}`)
        .filter((pair) => !pair.startsWith("style="))
        .sort(),
      sectionNavButtons: doc.querySelectorAll("#sectionNav .nav-link").length,
      /* The per-page section navigator is the PRODUCT's (8.1.0) and Hybrid
         inherits it: present, visible, and exactly one per page. A design that
         hid it — or drew a second one — fails here rather than in a screenshot
         somebody has to notice. */
      navigatorsByPage: Object.fromEntries(
        Array.from(doc.querySelectorAll(".admin-view")).map((view) => [
          view.dataset.view,
          view.querySelectorAll(":scope > .section-navigator").length,
        ]),
      ),
      liveSectionNavigators: Array.from(doc.querySelectorAll(".section-navigator"))
        .filter((nav) => !nav.hidden)
        .map((nav) => nav.id),
      source: digest(digestPage()),
    };

    /* The palette: pages, sections and settings, and the keyboard. */
    const searchButton = doc.querySelector(".hy-masthead .hy-search");
    if (!searchButton) {
      // Nothing past here is meaningful without a running shell, and a null
      // dereference would report a stack instead of the state that caused it.
      out.scenarioFatal =
        "the Hybrid shell did not start: " + JSON.stringify(page.designShellState());
      out.transports = transports;
      out.scriptErrorsTail = scriptErrors.slice(-8);
      out.consoleErrorsTail = consoleErrors.slice(-8);
      return out;
    }
    /* A REAL CLICK, not `element.click()`.
     *
     * A pointer click focuses the element before dispatching, and that is the
     * behaviour the focus-return requirement is about — "focus returns to the
     * card that opened it" is only meaningful if something had focus. jsdom's
     * `HTMLElement.click()` dispatches without focusing, which would leave this
     * probe asserting on `document.body` no matter what the shell did. So the
     * focus is set explicitly and then a `MouseEvent` is dispatched, which is
     * what a browser produces.
     */
    const realClick = (node) => {
      node.focus({ preventScroll: true });
      node.dispatchEvent(
        new window.MouseEvent("click", { bubbles: true, cancelable: true, view: window }),
      );
    };
    realClick(searchButton);
    await wait(80);
    const palette = doc.getElementById("hyPalette");
    out.palette = {
      present: Boolean(palette),
      role: palette ? palette.getAttribute("role") : null,
      modal: palette ? palette.getAttribute("aria-modal") : null,
      labelled: palette ? palette.getAttribute("aria-label") : null,
      itemsOnOpen: palette ? palette.querySelectorAll("li[role='option']").length : 0,
      hintsOnOpen: palette
        ? Array.from(palette.querySelectorAll("li[role='option'] span:last-child")).map(flat)
        : [],
      expandedOnButton: searchButton.getAttribute("aria-expanded"),
    };
    // Type something only a setting matches, and read what the palette found.
    const input = palette ? palette.querySelector("input") : null;
    if (input) {
      input.value = "output token ceiling";
      input.dispatchEvent(new window.Event("input", { bubbles: true }));
      await wait(40);
      out.palette.afterQuery = {
        value: input.value,
        items: Array.from(palette.querySelectorAll("li[role='option']")).map((node) => ({
          label: flat(node.querySelector("span")),
          code: flat(node.querySelector("code")).trim(),
          hint: flat(node.querySelector("span:last-child")),
        })),
      };
      // Escape closes it and returns focus to whatever opened it.
      out.paletteAfterEscape = {
        activeBeforeEscape: doc.activeElement === input,
      };
      input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      // One task, not one microtask: the focus restore is scheduled so the
      // removal canvas own blur cannot undo it (see closePalette).
      await wait(80);
    }
    out.paletteAfterEscape = {
      ...(out.paletteAfterEscape || {}),
      present: Boolean(doc.getElementById("hyPalette")),
      expanded: searchButton.getAttribute("aria-expanded"),
      /* FOCUS RETURN. jsdom's `HTMLElement.focus()` only moves `activeElement`
         for an element it considers focusable AND attached; a `<button>` that
         is inside the page is both, so this is a real check rather than a
         formality — but the browser is what the requirement is about, and the
         PR's screenshots are the other half of the proof. */
      focusReturned: doc.activeElement === searchButton,
      activeElement: doc.activeElement
        ? doc.activeElement.tagName + "." + doc.activeElement.className
        : null,
    };

    /* A section item: it must switch the page AND land on the section. */
    realClick(searchButton);
    await wait(60);
    const palette2 = doc.getElementById("hyPalette");
    const input2 = palette2 ? palette2.querySelector("input") : null;
    let sectionItem = null;
    if (input2) {
      input2.value = "diagnostics";
      input2.dispatchEvent(new window.Event("input", { bubbles: true }));
      await wait(40);
      sectionItem = Array.from(palette2.querySelectorAll("li[role='option']")).find((node) =>
        flat(node.querySelector("span")).toLowerCase().includes("diagnostics"),
      );
    }
    out.paletteSection = { found: Boolean(sectionItem) };
    if (sectionItem) {
      sectionItem.click();
      await wait(260);
      out.paletteSection.view = page.state.activeView;
      out.paletteSection.hash = window.location.hash;
      out.paletteSection.paletteGone = !doc.getElementById("hyPalette");
      out.paletteSection.stripCurrent = Array.from(
        doc.querySelectorAll(
          `.admin-view[data-view="${page.state.activeView}"] .section-navigator button[aria-current="true"]`,
        ),
      ).map(flat);
    }

    /* The g-keys: `g` then `l` is Limits, the way the sheet advertises. */
    const key = (k) =>
      doc.dispatchEvent(
        new window.KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true }),
      );
    key("g");
    key("l");
    await wait(140);
    out.shortcuts = { afterGL: page.state.activeView };
    key("g");
    key("r");
    await wait(140);
    out.shortcuts.afterGR = page.state.activeView;
    // A g-key INSIDE a text field must type, not navigate.
    const box = doc.querySelector("#reqFilterSearch") || doc.querySelector(".admin-view[data-view='requests'] input");
    const beforeTyping = page.state.activeView;
    if (box) {
      box.focus();
      box.dispatchEvent(
        new window.KeyboardEvent("keydown", { key: "g", bubbles: true, cancelable: true }),
      );
      box.dispatchEvent(
        new window.KeyboardEvent("keydown", { key: "d", bubbles: true, cancelable: true }),
      );
      await wait(120);
    }
    out.shortcuts.typingIgnored = page.state.activeView === beforeTyping;

    /* The drawer: the product's own modal, moved by the design's attributes. */
    out.drawer = {
      detailAttr: doc.documentElement.dataset.detail,
      sectionsAttr: doc.documentElement.dataset.sections,
      fieldsAttr: doc.documentElement.dataset.fields,
      analyticsAttr: doc.documentElement.dataset.analytics,
      densityAttr: doc.documentElement.dataset.density,
    };

    /* ------------------------------------------------- switch under load --- */
    /* Two switches with nothing awaited between them, which is a reader
       clicking twice. The chain must serialise them and exactly one shell —
       the last one asked for — must be attached at the end. */
    window.localStorage.setItem("mcc-admin-design", "hybrid");
    PAGE().applyAppearance(PAGE().resolveAppearance());
    window.localStorage.removeItem("mcc-admin-design");
    PAGE().applyAppearance(PAGE().resolveAppearance());
    window.localStorage.setItem("mcc-admin-design", "hybrid");
    PAGE().applyAppearance(PAGE().resolveAppearance());
    await settleShell();
    out.switchUnderLoad = {
      design: page.state.appearance.design,
      mastheads: doc.querySelectorAll(".hy-masthead").length,
      shellNodes: doc.querySelectorAll("[data-mcc-shell]").length,
      numbers: doc.querySelectorAll(".hy-nav-num").length,
      groups: doc.querySelectorAll(".hy-nav-group").length,
      palettes: doc.querySelectorAll(".hy-palette").length,
      versionIndicators: doc.querySelectorAll("#versionIndicator").length,
      state: page.designShellState(),
      transports: transports.length,
    };

    /* ------------------------------------------------------------- failure - */
    /* A shell whose module throws. The page must keep working; the card must
       say so once; nothing the failed shell touched may remain. */
    hybridFailure = "the shell threw while starting";
    window.localStorage.setItem("mcc-admin-design", "today");
    PAGE().applyAppearance(PAGE().resolveAppearance());
    await settleShell();
    const beforeFailure = digest(digestPage());
    window.localStorage.setItem("mcc-admin-design", "hybrid");
    const errorsBefore = consoleErrors.length;
    PAGE().applyAppearance(PAGE().resolveAppearance());
    await settleShell();
    const afterFailure = digest(digestPage());
    const failureLine = doc.getElementById("appearanceShellFailure");
    out.failure = {
      design: page.state.appearance.design,
      shellState: page.designShellState(),
      mastheads: doc.querySelectorAll(".hy-masthead").length,
      shellNodes: doc.querySelectorAll("[data-mcc-shell]").length,
      railNumbers: doc.querySelectorAll(".hy-nav-num").length,
      versionInSidebar: Boolean(doc.querySelector(".sidebar #versionIndicator")),
      versionCount: doc.querySelectorAll("#versionIndicator").length,
      // The page is the page: same chrome digest, same attrs, same body class.
      chromeMatches: afterFailure.chrome === beforeFailure.chrome,
      railMatches: afterFailure.rail === beforeFailure.rail,
      htmlAttrs: afterFailure.htmlAttrs,
      bodyClass: afterFailure.bodyClass,
      // ONE honest line, and it is a sentence rather than a stack.
      line: failureLine ? flat(failureLine) : null,
      lineRole: failureLine ? failureLine.getAttribute("role") : null,
      consoleErrorsAdded: consoleErrors.length - errorsBefore,
      // And the dashboard still navigates.
      navWorks: (() => {
        const link = doc.querySelector('.nav-link[data-view="limits"]');
        if (!link) return false;
        link.click();
        return page.state.activeView === "limits";
      })(),
    };
    await wait(120);
    out.failure.viewsRendered = Array.from(doc.querySelectorAll(".admin-view"))
      .filter((view) => view.dataset.view === "limits")
      .map((view) => !view.hidden);
    hybridFailure = null;

    /* ------------------------------------------------ back to today, twice - */
    /* The round trip proper: today → hybrid → today must land where it began,
       and doing it twice must land on the same bytes both times. */
    window.localStorage.setItem("mcc-admin-design", "hybrid");
    PAGE().applyAppearance(PAGE().resolveAppearance());
    await settleShell();
    window.localStorage.setItem("mcc-admin-design", "today");
    PAGE().applyAppearance(PAGE().resolveAppearance());
    await settleShell();
    const afterRoundTrip = digest(digestPage());
    out.roundTrip.after = afterRoundTrip;
    /* THE EQUALITY IS ON WHAT A SHELL CAN TOUCH, not on the whole document.
     *
     * `chrome` (every node outside `.app-shell`), `htmlAttrs`, `bodyClass`,
     * `residue` and the de-decorated rail are the complete set of things a
     * shell can add or change, and all five must be identical. The per-view
     * digests are reported rather than asserted: three pages (`get_started`,
     * `requests`, `limits`) re-render on their own — a clock stamp and a
     * polled counter — so their bytes move between two reads of the SAME page,
     * and asserting on them would be asserting on noise. What must be true of
     * those pages is that none of the shell's names appears in them, which is
     * `residue` below.
     */
    const equalKeys = ["chrome", "attrs", "bodyClass", "residue", "rail", "viewsEqual"];
    out.roundTrip.equalKeys = equalKeys;
    out.roundTrip.equal =
      before.chrome === afterRoundTrip.chrome &&
      JSON.stringify(before.htmlAttrs) === JSON.stringify(afterRoundTrip.htmlAttrs) &&
      before.bodyClass === afterRoundTrip.bodyClass &&
      before.railBare === afterRoundTrip.railBare &&
      before.railLinks.join(",") === afterRoundTrip.railLinks.join(",") &&
      before.shellNodes === 0 &&
      afterRoundTrip.shellNodes === 0 &&
      before.mastheads === 0 &&
      afterRoundTrip.mastheads === 0 &&
      before.railNumbers === 0 &&
      afterRoundTrip.railNumbers === 0 &&
      before.railGroups === 0 &&
      afterRoundTrip.railGroups === 0 &&
      before.palette === 0 &&
      afterRoundTrip.palette === 0 &&
      afterRoundTrip.residue.attributes === 0 &&
      afterRoundTrip.residue.nodes === 0 &&
      afterRoundTrip.residue.classes.length === 0;
    out.roundTrip.viewsEqual = Object.fromEntries(
      Object.keys(before.views).map((view) => [
        view,
        before.views[view] === afterRoundTrip.views[view],
      ]),
    );
    // ...and a second round trip lands on the same bytes, so nothing accumulates.
    window.localStorage.setItem("mcc-admin-design", "hybrid");
    PAGE().applyAppearance(PAGE().resolveAppearance());
    await settleShell();
    window.localStorage.setItem("mcc-admin-design", "today");
    PAGE().applyAppearance(PAGE().resolveAppearance());
    await settleShell();
    const secondTime = digest(digestPage());
    out.roundTrip.idempotent = JSON.stringify(afterRoundTrip) === JSON.stringify(secondTime);
    // And the residue is still nothing after the second trip: a leak that only
    // shows after two switches is exactly the kind a single test misses.
    out.roundTrip.residueBefore = before.residue;
    out.roundTrip.residueAfter = afterRoundTrip.residue;
    out.roundTrip.residueSecond = secondTime.residue;

    out.transports = transports;
    // Leave the page as the rest of the suite expects it: Today, nothing saved.
    window.localStorage.removeItem("mcc-admin-design");
    PAGE().applyAppearance(PAGE().resolveAppearance());
    await settleShell();
    return out;
  }

  return { run };
}
