/**
 * The Coding agents page's filter box (7.113.3), run instead of the main
 * capture when MCC_JSDOM_SCENARIO=coding_agents_filter.
 *
 * The user's words (2026-10-29): "allow users to search for the coding agent
 * that is of interest to them instead of having to scroll each time". What
 * this drives, on the payloads the harness already serves:
 *
 *  - the empty box: every CLI agent card and every desktop app card visible,
 *    the note hidden, and the page's markup -- the view's children with the
 *    filter bar taken out -- captured as a string the Python test compares
 *    with the one the BASE tree drew (the same scenario file, pointed at the
 *    base admin_static, where the box does not exist);
 *  - a query: the cards whose rendered text contains it stay, the rest get
 *    the `hidden` attribute, and cards are never removed (the page's
 *    [data-key] count is the same before and after);
 *  - a nonsense query: zero cards and the "No agents match." line;
 *  - clearing: every card back, and the two lists' markup equal to the
 *    capture taken before the first keystroke -- the filter leaves nothing
 *    behind;
 *  - no request on a keystroke (fetch calls counted synchronously around the
 *    input events), and a query made of HTML characters that reaches no
 *    markup, no element and no script.
 *
 * Prints nothing itself: `run()` returns the measurements and the harness
 * prints them as one JSON object.
 */

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const PROBE =
  '<img src=x onerror="window.__agentXss=1"><script>window.__agentXss=2</script>';

export function prepareCodingAgentsFilterScenario({ window, ROUTES }) {
  const doc = window.document;
  const flat = (el) => (el ? (el.textContent || "").replace(/\s+/g, " ").trim() : "");

  // Every request the page makes, whatever route it is for. Counted around
  // the input events synchronously, so no timer of the page's own can land in
  // the window and be mistaken for the filter's.
  const calls = [];
  const harnessFetch = window.fetch;
  window.fetch = async (url, options = {}) => {
    calls.push(String(url));
    return harnessFetch(url, options);
  };

  const listEl = (id) => doc.getElementById(id);
  const cardsIn = (id) =>
    Array.from((listEl(id) || doc).querySelectorAll(".coding-agent-card"));
  const allCards = () => [...cardsIn("codingAgentsList"), ...cardsIn("desktopAppsList")];
  const cardId = (card) => card.dataset.harness || card.dataset.desktopApp || "";
  const visibleIds = (id) => cardsIn(id).filter((card) => !card.hidden).map(cardId);
  const noteEl = () => doc.getElementById("codingAgentsFilterNote");
  const barEl = () => doc.getElementById("codingAgentsFilterBar");
  const inputEl = () => doc.getElementById("codingAgentsFilter");

  const state = () => ({
    cli: visibleIds("codingAgentsList"),
    desktop: visibleIds("desktopAppsList"),
    hidden: allCards().filter((card) => card.hidden).map(cardId),
    shown: allCards().filter((card) => !card.hidden).length,
    total: allCards().length,
    noteHidden: noteEl() ? Boolean(noteEl().hidden) : null,
    noteText: flat(noteEl()),
    filtering: barEl() ? barEl().classList.contains("coding-agents-filtering") : null,
    barClass: barEl() ? barEl().className : null,
    value: inputEl() ? inputEl().value : null,
  });

  /** The view's own children, minus the filter bar: the page the cards are on.
   *  Elements are serialised one by one rather than as one innerHTML string,
   *  so taking the bar out leaves the same markup the BASE tree has and not a
   *  whitespace artefact where the bar used to sit. */
  const captureDom = () => {
    const view = doc.querySelector('#view-coding_agents');
    if (!view) return null;
    const children = Array.from(view.children)
      // 8.1.0: the per-page section navigator is a second child this view
      // gained, for the same reason the filter bar is excluded — it is a
      // later release's addition, and this comparison is about the cards the
      // filter was added to. Its own footprint is counted by `viewChildCount`
      // and proved by the design-system scenario.
      .filter(
        (el) =>
          el.id !== "codingAgentsFilterBar" &&
          !el.classList.contains("section-navigator"),
      )
      // The static sections keep the line endings of the checkout that served
      // index.html; the baseline is committed once and read on Linux CI, so
      // it speaks one (LF) or every comparison would be a platform report.
      .map((el) => el.outerHTML.replace(/\r\n/g, "\n"));
    return {
      children,
      // The bar is the only child the branch adds to this view; the count is
      // what proves "apart from the new input node and its wrapper".
      viewChildCount: view.children.length,
      filterBar: barEl() ? barEl().outerHTML.replace(/\r\n/g, "\n") : null,
    };
  };

  /** Capture once the page stops changing. Two captures 150 ms apart that are
   *  byte-identical are the page at rest; a slow route landing later would
   *  otherwise make the equality baseline a race. */
  const stableCapture = async () => {
    let previous = null;
    for (let attempt = 0; attempt < 20; attempt += 1) {
      const current = JSON.stringify(captureDom());
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

  // An independent reading of the same rule the filter states: a card matches
  // when the text it renders contains the query, case-insensitively, with
  // runs of whitespace collapsed. Written here as textContent, not through
  // the page's own helper, so the two have to agree by their output only.
  const haystackHits = (query) => {
    const wanted = query.replace(/\s+/g, " ").trim().toLowerCase();
    return allCards()
      .filter((card) =>
        (card.textContent || "").replace(/\s+/g, " ").trim().toLowerCase().includes(wanted),
      )
      .map(cardId);
  };

  async function run({ scriptErrors, consoleErrors }) {
    const out = { scenarioFatal: null, baseTree: false };
    // The sidebar is drawn by renderNav() once /admin/api/config has answered,
    // so the link is not in the document at DOMContentLoaded.
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
    // The two routes that fill the cards' own controls answer after the cards
    // themselves; the stability pass below is what really waits for them.
    await wait(300);

    if (!inputEl()) {
      /* The BASE tree, pointed at here by the Python test's baseline command:
         it has no filter box, so the page is drawn and captured and nothing
         is typed. */
      out.baseTree = true;
      out.dom = await stableCapture();
      out.cards = allCards().map((card) => ({ id: cardId(card), title: flat(card.querySelector("h4")) }));
      out.scriptErrors = scriptErrors;
      out.consoleErrors = consoleErrors;
      return out;
    }

    out.empty = state();
    out.cards = allCards().map((card) => ({
      id: cardId(card),
      title: flat(card.querySelector("h4")),
      group: card.dataset.harness ? "cli" : "desktop",
    }));
    out.dataKeyCountEmpty = doc.querySelectorAll("[data-key]").length;
    out.dom = await stableCapture();

    // Where the box sits in the tab order: the first control on the page.
    // 8.1.0: the per-page section navigator is a later release's addition and
    // sits above the box by design; this check is about the box being first
    // among the page's own content, so the strip is left out.
    const focusables = Array.from(
      doc.querySelector('#view-coding_agents').querySelectorAll(
        'a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])',
      ),
    ).filter(
      (el) =>
        !el.disabled &&
        !el.hidden &&
        !el.closest(".section-navigator"),
    );
    out.firstFocusableId = focusables.length ? focusables[0].id || focusables[0].className : null;

    /* ------------------------------------------------------------ a query */
    const query = (text) => {
      typeQuery(text);
      return state();
    };

    out.codex = query("codex");
    out.codexHits = haystackHits("codex");
    out.opencode = query("opencode");
    out.opencodeHits = haystackHits("opencode");
    out.claude = query("CLAUDE");
    out.claudeHits = haystackHits("claude");
    // A command line and a legacy alias, not a title: the box matches what the
    // card shows, so both are findable.
    out.legacyAlias = query("fcc-claude");
    out.legacyAliasHits = haystackHits("fcc-claude");

    /* ------------------------------------------------------- nothing matches */
    out.nothing = query("zzzz-no-such-agent");
    out.dataKeyCountFiltered = doc.querySelectorAll("[data-key]").length;

    /* ---------------------------------------------------------- no requests */
    const beforeTyping = calls.length;
    for (const piece of ["c", "co", "cod", "code", "codex"]) typeQuery(piece);
    out.typingFetchCalls = calls.slice(beforeTyping);
    out.typingState = state();

    /* --------------------------------------------------------------- clear */
    out.cleared = query("");
    out.clearedDom = captureDom();
    out.dataKeyCountCleared = doc.querySelectorAll("[data-key]").length;

    /* -------------------------------------------------- markup in the query */
    const imagesBefore = doc.querySelectorAll("img").length;
    const scriptsBefore = doc.querySelectorAll("script").length;
    const elementsBefore = doc.querySelectorAll("*").length;
    out.xss = query(PROBE);
    out.xss = {
      ...out.xss,
      imagesBefore,
      imagesAfter: doc.querySelectorAll("img").length,
      scriptsBefore,
      scriptsAfter: doc.querySelectorAll("script").length,
      elementsBefore,
      elementsAfter: doc.querySelectorAll("*").length,
      flag: typeof window.__agentXss === "undefined" ? "" : String(window.__agentXss),
      viewMarkup: (doc.querySelector('#view-coding_agents').innerHTML || "").includes("onerror"),
      typed: inputEl().value,
    };
    // Leave the page as the next assertions expect it.
    typeQuery("");

    out.scriptErrors = scriptErrors;
    out.consoleErrors = consoleErrors;
    out.harnessRoutes = [
      "/admin/api/harnesses",
      "/admin/api/desktop-apps",
    ].filter((path) => Boolean(ROUTES[path]));
    return out;
  }

  return { run };
}
