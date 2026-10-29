/**
 * The Models page's in-loop chip (7.115.1), run instead of the main capture
 * when MCC_JSDOM_SCENARIO=in_loop_chip.
 *
 * The user's question (2026-10-29): which model says it will keep reading and
 * then stops. The count is served by `/admin/api/requests/in-loop` beside the
 * page -- the page's own payload is pinned against an equality baseline and
 * this readout moves with every logged request -- and this scenario proves
 * the browser half of that decision:
 *
 *  - the page asks for it in the same breath as the page and the latency
 *    rollup, and draws one `measured` chip per model that has sequenced turns:
 *    `last 7d: 8/86 in-loop turns ended with text only`;
 *  - the unmeasured turns are named in the chip rather than folded into the
 *    numerator or dropped;
 *  - a model with a measured zero draws `0/4` (measured zero, not absent), and
 *    a model with no row in the payload draws no chip at all;
 *  - the tooltip says what an in-loop turn is, in the words a reader needs;
 *  - the endpoint failing leaves the page exactly as it was: models rendered,
 *    no chips, no error in place of the tree;
 *  - and the page's own model rows carry no in-loop field, because the count
 *    never enters that payload.
 *
 * Prints nothing itself: `run()` returns the measurements and the harness
 * prints them as one JSON object.
 */

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const IN_LOOP = {
  enabled: true,
  days: 7,
  window_since: 1_761_700_000,
  rows: [
    { model_ref: "alpha/model-01", in_loop_turns: 86, text_only_turns: 8, unmeasured_turns: 0 },
    { model_ref: "alpha/model-02", in_loop_turns: 12, text_only_turns: 3, unmeasured_turns: 2 },
    { model_ref: "alpha/model-03", in_loop_turns: 4, text_only_turns: 0, unmeasured_turns: 0 },
    { model_ref: "beta/model-01", in_loop_turns: 9, text_only_turns: 1, unmeasured_turns: 0 },
  ],
  models: 4,
  in_loop_turns: 111,
  text_only_turns: 12,
  unmeasured_turns: 2,
};

export function prepareInLoopChipScenario({ window, ROUTES }) {
  ROUTES["/admin/api/requests/in-loop"] = JSON.parse(JSON.stringify(IN_LOOP));

  const doc = window.document;
  const flat = (el) => (el ? (el.textContent || "").replace(/\s+/g, " ").trim() : "");

  // A switch the scenario flips to make the in-loop route fail, exactly as a
  // dropped connection would: `api()` rejects, the page's own `.catch` turns
  // it into "no chips".
  let failInLoop = false;
  let pageRowKeys = [];
  const inLoopCalls = [];
  const harnessFetch = window.fetch;
  window.fetch = (url, options = {}) => {
    const path = String(url).split("?")[0];
    if (path === "/admin/api/requests/in-loop") {
      inLoopCalls.push(String(url));
      if (failInLoop) return Promise.reject(new Error("network down"));
    }
    const answer = harnessFetch(url, options);
    if (path !== "/admin/api/model-admin") return answer;
    // The model rows the browser actually received, read off the response
    // the page parses -- so "the page carries no in-loop field" is a fact
    // about the payload, not about the file that builds it.
    return answer.then((response) => ({
      ...response,
      json: async () => {
        const body = await response.json();
        if (!pageRowKeys.length && body.providers && body.providers[0]) {
          pageRowKeys = Object.keys(body.providers[0].models[0] || {}).sort();
        }
        return body;
      },
    }));
  };

  const waitFor = async (predicate, timeoutMs = 20000) => {
    const started = Date.now();
    while (Date.now() - started < timeoutMs) {
      if (predicate()) return true;
      await wait(50);
    }
    return false;
  };

  /** Every model row the alpha provider has rendered, with its chips. The ref
   *  is the summary's own `.models-ref`, so a chip can never be attributed to
   *  a row by position. */
  const collect = () =>
    Array.from(doc.querySelectorAll('[data-provider="alpha"] .models-model')).map(
      (node) => {
        const summary = node.querySelector("summary");
        const ref = summary ? flat(summary.querySelector(".models-ref")) : "";
        const chips = Array.from(summary ? summary.querySelectorAll(".models-chip") : [])
          .filter((chip) => /in-loop/.test(flat(chip)))
          .map((chip) => ({
            className: chip.className,
            text: flat(chip),
            title: chip.title || "",
          }));
        return { ref, chips };
      },
    );

  const pageKeys = () => pageRowKeys;

  async function run({ scriptErrors, consoleErrors }) {
    // The sidebar is built by `renderNav()` once the page's own config load
    // resolves, so the link appears a beat after DOMContentLoaded.
    if (
      !(await waitFor(() => doc.querySelector('.nav-link[data-view="models"]')))
    ) {
      return { scenarioError: "no Models nav link" };
    }
    const navLink = doc.querySelector('.nav-link[data-view="models"]');
    navLink.click();
    if (!(await waitFor(() => doc.querySelector('[data-provider="alpha"]')))) {
      return { scenarioError: "the Models tree never rendered" };
    }
    const toggle = doc.querySelector(
      '[data-provider="alpha"] .models-provider-toggle',
    );
    if (!toggle) return { scenarioError: "no provider toggle" };
    toggle.click();
    if (
      !(await waitFor(
        () =>
          doc.querySelectorAll('[data-provider="alpha"] .models-model').length > 0,
      ))
    ) {
      return { scenarioError: "the provider never opened" };
    }
    // The chips are drawn by the summary that builds with the node, so the
    // first row is enough to know the fill has run; a short beat covers the
    // rest of the body being appended.
    await wait(120);
    const loaded = collect();
    const keys = pageKeys();
    const callsBeforeFailure = inLoopCalls.length;

    failInLoop = true;
    try {
      // The page's own reload, awaited: its `.catch(() => null)` turns the
      // rejected fetch into "no measurement", and `renderModelsPage()` has
      // run by the time the promise settles.
      await window.eval("loadModelsView(true)");
    } catch (error) {
      return { scenarioError: `the reload threw: ${error && error.message}` };
    }
    await wait(200);
    const afterFailure = collect();
    const callsAfterFailure = inLoopCalls.length;

    return {
      loaded,
      afterFailure,
      pageRowKeys: keys,
      callsBeforeFailure,
      callsAfterFailure,
      treeStillRendered: Boolean(
        doc.querySelectorAll('[data-provider="alpha"] .models-model').length,
      ),
      statusText: flat(doc.getElementById("modelsTreeStatus")),
      scriptErrors,
      consoleErrors,
      harnessRoutes: ["/admin/api/requests/in-loop"].filter((path) =>
        Boolean(ROUTES[path]),
      ),
    };
  }

  return { run };
}
