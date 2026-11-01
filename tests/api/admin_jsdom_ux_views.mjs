/**
 * Every view's DOM, for the base-tree equality of the 7.122.3 UX pass
 * (MCC_JSDOM_SCENARIO=ux_views).
 *
 * The same scenario runs on the BASE tree and on this branch. The Python test
 * compares the two, view by view, and asserts that exactly the views this
 * pass declares changed, and no others. Two digests per view: the raw one and
 * one with this pass's settings-wide display changes undone -- the "default
 * value" chip, the "Use default" buttons it stops offering, and the words a
 * select's options wear -- so the pages those touch can stay in the
 * comparison instead of being excluded wholesale.
 *
 * Prints nothing itself: `run()` returns the measurements and the harness
 * prints them as one JSON object.
 */

import { createHash } from "node:crypto";

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const sha256 = (text) => createHash("sha256").update(text, "utf8").digest("hex");

/** The same replacements tests/api/test_admin_static_jsdom.py documents. */
export function uxNormalise(html) {
  const swap = (text, from, to) => text.split(from).join(to);
  let dom = html;
  // U12: the provenance chip is normalised out on both sides. Its words and
  // its place both moved (it left the label for the meta row so "default
  // value" could not wrap a long label); the ux_pass scenario proves what it
  // says, and this comparison is about everything else on the page.
  dom = dom.replace(
    /<span class="field-source">[^<]*<\/span>/g,
    "",
  );
  dom = swap(
    dom,
    '<button type="button" class="field-reset">Use default</button>',
    "",
  );
  dom = swap(dom, '<option value="true">On</option>', '<option value="true">true</option>');
  dom = swap(dom, '<option value="false">Off</option>', '<option value="false">false</option>');
  // U27/U30: the words a select uses for its default. The setting does not
  // move; the option labels and the "default:" line do, so both are
  // replaced wholesale rather than one phrase at a time.
  dom = dom.replace(
    /<option value="">Default \([^<]*\)<\/option>/g,
    '<option value="">Default (?)</option>',
  );
  dom = dom.replace(
    /class="field-default">default: [^<]*</g,
    'class="field-default">default: ?<',
  );
  // U18: the in-page rail's mark is written on a scroll frame now, not by the
  // IntersectionObserver at bind time, so the base tree had it on the first
  // link in this capture and this one does not. Where the mark goes is proved
  // by the ux_pass scenario; here it is the same rail either way.
  dom = swap(dom, ' aria-current="true"', "");
  return dom;
}

export function prepareUxViewsScenario({ window }) {
  const doc = window.document;

  async function run({ scriptErrors, consoleErrors }) {
    const out = { scenarioFatal: null, scriptErrors, consoleErrors };
    // `load()` is async; the last thing it does is draw the Coding agents
    // cards, so their presence means the page is the one a reader sees.
    const started = Date.now();
    while (
      Date.now() - started < 20000 &&
      !doc.querySelector("#codingAgentsList .coding-agent-card")
    ) {
      await wait(50);
    }
    await wait(900);
    const views = {};
    const htmls = {};
    doc.querySelectorAll(".admin-view[data-view]").forEach((view) => {
      const html = view.innerHTML.replace(/\r\n/g, "\n");
      htmls[view.dataset.view] = html;
      views[view.dataset.view] = {
        children: view.children.length,
        length: html.length,
        sha256: sha256(html),
        sha256Normalised: sha256(uxNormalise(html)),
      };
    });
    out.views = views;
    // A debugging aid, never used in CI: MCC_JSDOM_UX_DUMP=<view> ships that
    // view's HTML so a baseline difference can be read rather than guessed.
    const dump = process.env.MCC_JSDOM_UX_DUMP;
    if (dump) {
      out.html = dump === "*" ? htmls : { [dump]: htmls[dump] };
    }
    return out;
  }

  return { run };
}
