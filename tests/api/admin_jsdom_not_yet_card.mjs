/**
 * The Coding agents page's "not yet" card (7.118.1), run instead of the main
 * capture when MCC_JSDOM_SCENARIO=not_yet_card.
 *
 * The payload is not written here: `MCC_JSDOM_NOT_YET_FILE` names a JSON file
 * the Python test writes from the registry itself (`not_yet_cards()` ->
 * `_not_yet_entry`), so this scenario renders the strings the server really
 * sends and cannot drift from `config/harnesses.py`. The file is put on the
 * harness's own `/admin/api/harnesses` payload under the new `not_yet` key --
 * the one additive key 7.118.1 introduces -- and nothing else is touched.
 *
 * What it measures, on the page the harness already serves:
 *
 *  - the card itself: its title, its "Not yet" badge, its date, and its whole
 *    text, so the Python test can assert the reason and the condition that
 *    would change the decision are on the page rather than in a comment;
 *  - that it carries no control of any kind -- no button, no input, no host
 *    div for an Install button or an MCP toggle, no [data-key] -- which is the
 *    difference between "explains" and "offers";
 *  - the find box still reaches it, and reaches nothing else for "deepseek";
 *  - the DOM with the not-yet card removed (a deep clone has it taken out, so
 *    the live page is never mutated), plus every other card's HTML, both of
 *    which the Python test compares byte for byte against the BASE tree's run
 *    of this same scenario on this same payload. That equality is the proof
 *    that the card is the only thing this release added.
 *
 * On the BASE tree (16bc7b23) admin.js does not know the key, so no card is
 * drawn: the scenario detects that, reports `baseTree: true`, and captures the
 * same measurements for the baseline. Prints nothing itself: `run()` returns
 * them and the harness prints one JSON object.
 */

import { readFileSync } from "node:fs";

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const NOT_YET_SELECTOR = "[data-not-yet]";

export function prepareNotYetCardScenario({ window, ROUTES }) {
  const doc = window.document;
  const flat = (el) => (el ? (el.textContent || "").replace(/\s+/g, " ").trim() : "");

  const payloadPath = process.env.MCC_JSDOM_NOT_YET_FILE || "";
  const cards = payloadPath
    ? JSON.parse(readFileSync(payloadPath, "utf8"))
    : [];
  const harnessRoute = ROUTES["/admin/api/harnesses"];
  if (!harnessRoute || !Array.isArray(harnessRoute.harnesses)) {
    throw new Error("the harness payload is not the shape this scenario needs");
  }
  harnessRoute.not_yet = cards;

  const listEl = (id) => doc.getElementById(id);
  const cardsIn = (id) =>
    Array.from((listEl(id) || doc).querySelectorAll(".coding-agent-card"));
  const notYetIn = (id) =>
    Array.from((listEl(id) || doc).querySelectorAll(NOT_YET_SELECTOR));
  const ordinaryCards = () =>
    cardsIn("codingAgentsList").filter((card) => !card.matches(NOT_YET_SELECTOR) && !card.dataset.desktopApp);
  const desktopCards = () => cardsIn("desktopAppsList");
  const cardId = (card) =>
    card.dataset.harness || card.dataset.desktopApp || card.dataset.notYet || "";
  const inputEl = () => doc.getElementById("codingAgentsFilter");

  /** Every view child, with any not-yet card taken out of a *clone*.
   *  Elements are serialised one by one, exactly as the 7.113.3 baseline does,
   *  so the comparison is per child and a difference names the child it is in.
   *  Line endings are normalised to LF: the baseline is committed once and read
   *  on Linux CI, and a CRLF artefact would read as a page difference. */
  const captureDom = () => {
    const view = doc.querySelector("#view-coding_agents");
    if (!view) return null;
    const children = Array.from(view.children)
      // 8.1.0: the per-page section navigator is a child this view gained
      // after this comparison was written. It is a later release's addition,
      // so it is taken out here the same way the not-yet card is -- what this
      // capture compares is the cards.
      .filter((el) => !el.classList.contains("section-navigator"))
      .map((el) => {
        const clone = el.cloneNode(true);
        clone.querySelectorAll(NOT_YET_SELECTOR).forEach((node) => node.remove());
        return clone.outerHTML.replace(/\r\n/g, "\n");
      });
    return { children, viewChildCount: view.children.length };
  };

  const captureCards = () =>
    [...ordinaryCards(), ...desktopCards()].map((card) => ({
      id: cardId(card),
      title: flat(card.querySelector("h4")),
      html: card.outerHTML.replace(/\r\n/g, "\n"),
    }));

  /** The card's own measurements. `controls` is every element a reader could
   *  press: a not-yet card that grows one has stopped explaining and started
   *  offering, which is the regression this test exists for. */
  const measureCard = (card) => {
    if (!card) return null;
    const controls = Array.from(
      card.querySelectorAll("button, input, select, textarea, [role='button']"),
    );
    return {
      id: card.dataset.notYet || "",
      title: flat(card.querySelector("h4")),
      badge: flat(card.querySelector(".agent-heading .agent-state")),
      text: flat(card),
      html: card.outerHTML.replace(/\r\n/g, "\n"),
      rows: Array.from(card.querySelectorAll(".agent-meta dt")).map(flat),
      values: Array.from(card.querySelectorAll(".agent-meta dd")).map(flat),
      links: Array.from(card.querySelectorAll("a")).map((a) => a.getAttribute("href")),
      controls: controls.map((el) => `${el.tagName.toLowerCase()}:${flat(el)}`),
      installHosts: card.querySelectorAll("[data-install-target]").length,
      mcpHosts: card.querySelectorAll(".mcp-media-control").length,
      dataKeys: card.querySelectorAll("[data-key]").length,
      hasTiers: Boolean(card.querySelector(".agent-tiers")),
    };
  };

  const stableCapture = async () => {
    let previous = null;
    let current = null;
    for (let attempt = 0; attempt < 20; attempt += 1) {
      current = JSON.stringify(captureDom());
      if (current === previous) return JSON.parse(current);
      previous = current;
      await wait(150);
    }
    return captureDom();
  };

  const waitFor = async (predicate, timeoutMs = 20000) => {
    const started = Date.now();
    while (Date.now() - started < timeoutMs) {
      if (predicate()) return true;
      await wait(50);
    }
    return false;
  };

  const typeQuery = (query) => {
    const input = inputEl();
    input.value = query;
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
  };

  /** The filter's own rule, read independently: rendered text, case-insensitive,
   *  whitespace collapsed. This run computes what it *should* show so the
   *  assertion is not the page agreeing with itself. */
  const haystackHits = (query) => {
    const wanted = query.replace(/\s+/g, " ").trim().toLowerCase();
    return [...ordinaryCards(), ...notYetIn("codingAgentsList"), ...desktopCards()]
      .filter((card) =>
        (card.textContent || "").replace(/\s+/g, " ").trim().toLowerCase().includes(wanted),
      )
      .map(cardId);
  };

  async function run({ scriptErrors, consoleErrors }) {
    const out = { scenarioFatal: null, baseTree: false };
    const linkOf = () =>
      Array.from(doc.querySelectorAll(".nav-link")).find(
        (el) => el.dataset.view === "coding_agents",
      );
    if (!(await waitFor(() => Boolean(linkOf())))) {
      out.scenarioFatal = "no Coding agents nav link";
      return out;
    }
    linkOf().click();

    const rendered = await waitFor(
      () => cardsIn("codingAgentsList").length > 0 && cardsIn("desktopAppsList").length > 0,
    );
    if (!rendered) {
      out.scenarioFatal = "the card lists never rendered";
      out.dom = captureDom();
      return out;
    }
    // The install and MCP hosts fill after the cards themselves; the stability
    // pass below is what waits for them.
    await wait(300);

    out.dom = await stableCapture();
    out.cards = captureCards();
    out.cardCount = captureCards().length;
    out.notYetIds = notYetIn("codingAgentsList").map((card) => card.dataset.notYet || "");
    out.card = measureCard(notYetIn("codingAgentsList")[0]);

    if (out.card === null) {
      /* The BASE tree: its admin.js does not know the `not_yet` key, so the
         payload is identical and the page has no such card. Everything above is
         captured for the baseline; there is nothing else to measure. */
      out.baseTree = true;
      out.scriptErrors = scriptErrors;
      out.consoleErrors = consoleErrors;
      return out;
    }

    if (inputEl()) {
      const query = (text) => {
        typeQuery(text);
        return {
          visibleCli: cardsIn("codingAgentsList")
            .filter((card) => !card.hidden)
            .map(cardId),
          visibleNotYet: notYetIn("codingAgentsList")
            .filter((card) => !card.hidden)
            .map((card) => card.dataset.notYet || ""),
          noteHidden: (doc.getElementById("codingAgentsFilterNote") || {}).hidden,
        };
      };
      out.deepseek = query("deepseek");
      out.deepseekHits = haystackHits("deepseek");
      out.dsh = query("dsh");
      out.dshHits = haystackHits("dsh");
      typeQuery("");
      out.cleared = {
        visibleCli: cardsIn("codingAgentsList")
          .filter((card) => !card.hidden)
          .map(cardId),
        dom: captureDom(),
      };
    }

    out.scriptErrors = scriptErrors;
    out.consoleErrors = consoleErrors;
    return out;
  }

  return { run };
}
