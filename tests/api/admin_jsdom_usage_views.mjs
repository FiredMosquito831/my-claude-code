/**
 * Every view's DOM, for the base-tree equality of the Usage page
 * (MCC_JSDOM_SCENARIO=usage_views).
 *
 * The same scenario runs on the BASE tree (v8.2.0, `bb85fe79`) and on this
 * branch, against the same fixture, through the same harness. The Python test
 * compares view by view and asserts that every view the base tree had is
 * byte-identical, and that the only NEW view is `usage`.
 *
 * The base tree has no `usage` entry at all -- no `VIEW_GROUPS` row, no
 * `<section id="view-usage">` -- so this file has to be written so it does not
 * depend on either. It reads the views the page itself declares and digests
 * each one, which is exactly the comparison the release needs: what the base
 * tree drew, unchanged, plus one more page.
 *
 * Prints nothing itself: `run()` returns the measurements and the harness
 * prints them as one JSON object.
 */

import { createHash } from "node:crypto";

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const sha256 = (text) => createHash("sha256").update(text, "utf8").digest("hex");

/** See tests/api/test_admin_ux_pass_jsdom.py: the normaliser the UX pass uses
 *  is applied on BOTH sides so that later releases' additions to every page
 *  (the 8.1.0 section strip, the 7.122.3 provenance chip) cannot hide a real
 *  change on the page underneath them. */
export function usageNormalise(html) {
  const swap = (text, from, to) => text.split(from).join(to);
  let dom = html;
  dom = dom.replace(/<nav class="section-navigator"[\s\S]*?<\/nav>/g, "");
  dom = dom.replace(/<span class="field-source">[^<]*<\/span>/g, "");
  dom = swap(dom, '<button type="button" class="field-reset">Use default</button>', "");
  dom = swap(dom, '<option value="true">On</option>', '<option value="true">true</option>');
  dom = swap(dom, '<option value="false">Off</option>', '<option value="false">false</option>');
  dom = dom.replace(/<option value="">Default \([^<]*\)<\/option>/g, '<option value="">Default (?)</option>');
  dom = dom.replace(/class="field-default">default: [^<]*</g, 'class="field-default">default: ?<');
  dom = swap(dom, ' aria-current="true"', "");
  return dom;
}

export function prepareUsageViewsScenario({ window }) {
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
        sha256Normalised: sha256(usageNormalise(html)),
      };
    });
    out.views = views;
    out.navLabels = Array.from(doc.querySelectorAll("#sectionNav .nav-link")).map(
      (link) => ({ view: link.dataset.view, label: link.textContent.trim() }),
    );
    const dump = process.env.MCC_JSDOM_USAGE_DUMP;
    if (dump) {
      out.html = dump === "*" ? htmls : { [dump]: htmls[dump] };
    }
    return out;
  }

  return { run };
}
