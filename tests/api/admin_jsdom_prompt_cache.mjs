/**
 * The prompt-cache lifetime on the dashboard (7.104.0), run instead of the
 * main capture when MCC_JSDOM_SCENARIO=prompt_cache.
 *
 * What this drives, on the server's own shapes (the Python test writes the
 * real manifest's Reasoning section and the real declared lifetime table to
 * the file MCC_JSDOM_PROMPT_CACHE_FIXTURE names):
 *
 *  - Model Config: the Reasoning section draws the thinking levels as before
 *    and, under a "Prompt cache lifetime" subheading, the six lifetime fields
 *    in the tier order of the thinking levels, each a select whose default is
 *    "From client" (global) or "Inherit" (tiers);
 *  - a Save of two edits posts exactly those two keys and values;
 *  - the Models page: a model of a declared provider draws the read-only
 *    "prompt cache lifetimes" row with the host's lifetimes, a fresh learned
 *    refusal names the refused one, and a model of an undeclared provider says
 *    "lifetime control not known for this host".
 *
 * Prints nothing itself: `run()` returns the measurements.
 */

import { readFileSync } from "node:fs";

const clone = (value) => (value === undefined ? value : JSON.parse(JSON.stringify(value)));
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const DECLARED = "anthropic_oauth";
const REFUSED_REF = `${DECLARED}/claude-opus-5-5`;
const PLAIN_REF = `${DECLARED}/claude-haiku-5-5`;
const UNDECLARED_REF = "alpha/model-01";

const REFUSAL_FACT = {
  fact_kind: "prompt_cache_lifetime_refused",
  fact_label: "cache lifetime refused",
  model_id: "claude-opus-5-5",
  value: true,
  detail: "1h",
  source: "rejection",
  source_label: "the host's own rejection",
  learned_at: "2026-10-21T10:00:00Z",
  last_confirmed_at: "2026-10-21T10:00:00Z",
  age_seconds: 60,
  ttl_seconds: 2592000,
  stale: false,
  retired: false,
  hits: 1,
  evidence: "system.1.cache_control.ephemeral.ttl: Input should be 5m",
};

export function preparePromptCacheScenario({ window, ROUTES }) {
  const fixture = JSON.parse(
    readFileSync(process.env.MCC_JSDOM_PROMPT_CACHE_FIXTURE, "utf-8"),
  );
  const config = ROUTES["/admin/api/config"];
  config.sections = [
    ...config.sections.filter((section) => section.id !== "reasoning"),
    fixture.section,
  ];
  config.fields = [
    ...config.fields.filter((field) => field.section !== "reasoning"),
    ...fixture.fields,
  ];

  const page = clone(ROUTES["/admin/api/model-admin"]);
  const alpha = page.providers.find((entry) => entry.provider_id === "alpha");
  const declaredModels = [REFUSED_REF, PLAIN_REF].map((ref, index) => ({
    ...clone(alpha.models[1 + index]),
    model_ref: ref,
    model_id: ref.split("/")[1],
    configured: false,
    override: {},
    effective: [],
    learned: ref === REFUSED_REF ? [clone(REFUSAL_FACT)] : [],
  }));
  page.providers.push({
    ...clone(alpha),
    provider_id: DECLARED,
    model_count: declaredModels.length,
    hidden_count: 0,
    models: declaredModels,
  });
  page.prompt_cache_lifetimes = clone(fixture.prompt_cache_lifetimes);
  ROUTES["/admin/api/model-admin"] = page;

  const doc = window.document;
  const flat = (el) => (el ? (el.textContent || "").replace(/\s+/g, " ").trim() : "");
  const settle = () => wait(200);

  const applied = [];
  const installRecorder = () => {
    const realFetch = window.fetch;
    window.fetch = async (url, options = {}) => {
      const path = String(url).split("?")[0];
      if (path === "/admin/api/config/apply") {
        applied.push(JSON.parse(options.body));
        const body = { applied: true, errors: [], warnings: [], pending_fields: [], restart: {} };
        return {
          ok: true,
          status: 200,
          json: async () => clone(body),
          text: async () => JSON.stringify(body),
        };
      }
      return realFetch(url, options);
    };
  };

  const describeField = (container, key) => {
    const select = container.querySelector(`select[data-key="${key}"]`);
    const wrapper = select ? select.closest(".field") : null;
    return select
      ? {
          key,
          value: select.value,
          optionCount: select.options.length,
          firstOptions: Array.from(select.options)
            .slice(0, 4)
            .map((option) => ({ value: option.value, text: flat(option) })),
          lastOption: flat(select.options[select.options.length - 1]),
          label: flat(wrapper && wrapper.querySelector("label")),
          help: flat(wrapper && wrapper.querySelector(".field-help, .help, small, p")),
        }
      : null;
  };

  const lifetimeRow = async (ref) => {
    const node = Array.from(doc.querySelectorAll("details.models-model")).find(
      (el) => flat(el.querySelector(".models-ref")) === ref,
    );
    if (!node) return { missing: ref };
    node.open = true;
    await settle();
    const tr = node.querySelector('tr[data-field="prompt_cache_lifetimes"]');
    if (!tr) return { ref, row: null };
    const cells = Array.from(tr.children);
    const badge = tr.querySelector(".models-source");
    const group = tr.closest("tbody");
    return {
      ref,
      row: {
        label: flat(cells[0]),
        value: flat(cells[1]),
        badgeClass: badge ? badge.className : null,
        badge: flat(badge),
        notes: Array.from(tr.querySelectorAll(".models-approx-note")).map(flat),
        readonly: tr.classList.contains("models-fields-readonly"),
        group: group ? group.dataset.group : null,
        srOnly: Array.from(tr.querySelectorAll(".sr-only")).map(flat),
      },
      emptyNote: flat(node.querySelector(".models-empty-note")),
    };
  };

  async function run({ scriptErrors, consoleErrors }) {
    await wait(900);
    installRecorder();
    const out = { scenarioFatal: null };
    const navLinks = Array.from(doc.querySelectorAll(".nav-link"));
    const configLink = navLinks.find((link) => link.dataset.view === "model_config");
    if (!configLink) {
      out.scenarioFatal = "no Model Config nav link";
      return out;
    }
    configLink.click();
    await wait(400);

    const section = doc.getElementById("section-reasoning");
    const sub = doc.getElementById("subsection-prompt-cache-lifetime");
    out.section = section
      ? {
          heading: flat(section.querySelector(".section-heading h3")),
          firstGridKeys: Array.from(
            section.querySelector(".field-grid").querySelectorAll(".field[data-key]"),
          ).map((el) => el.dataset.key),
          subInsideSection: Boolean(sub && section.contains(sub)),
        }
      : null;
    out.subsection = sub
      ? {
          heading: flat(sub.querySelector(".settings-subheading")),
          lead: flat(sub.querySelector(".settings-subheading-lead")),
          keys: Array.from(sub.querySelectorAll(".field[data-key]")).map(
            (el) => el.dataset.key,
          ),
          fields: [
            "PROMPT_CACHE_TTL",
            "PROMPT_CACHE_TTL_MYTHOS",
            "PROMPT_CACHE_TTL_FABLE",
            "PROMPT_CACHE_TTL_OPUS",
            "PROMPT_CACHE_TTL_SONNET",
            "PROMPT_CACHE_TTL_HAIKU",
          ].map((key) => describeField(sub, key)),
        }
      : null;

    const dirty = doc.getElementById("dirtyState");
    out.dirtyBefore = flat(dirty);
    const edit = (key, value) => {
      const select = sub ? sub.querySelector(`select[data-key="${key}"]`) : null;
      if (!select) return false;
      select.value = value;
      select.dispatchEvent(new window.Event("change", { bubbles: true }));
      return true;
    };
    out.edited = [edit("PROMPT_CACHE_TTL", "30"), edit("PROMPT_CACHE_TTL_OPUS", "3")];
    await settle();
    out.dirtyAfter = flat(dirty);
    const applyButton = doc.getElementById("applyButton");
    out.applyEnabled = applyButton ? !applyButton.disabled : null;
    if (applyButton) {
      applyButton.click();
      await wait(600);
    }
    out.applied = applied;

    const modelsLink = navLinks.find((link) => link.dataset.view === "models");
    modelsLink.click();
    await wait(600);
    const tree = doc.getElementById("modelsTree");
    for (const id of [DECLARED, "alpha"]) {
      const node = Array.from(tree.querySelectorAll(".models-provider")).find(
        (el) => el.dataset.provider === id,
      );
      const toggle = node ? node.querySelector(".models-provider-toggle") : null;
      if (toggle) {
        toggle.click();
        await settle();
      }
    }
    out.models = {
      refused: await lifetimeRow(REFUSED_REF),
      plain: await lifetimeRow(PLAIN_REF),
      undeclared: await lifetimeRow(UNDECLARED_REF),
    };

    out.scriptErrors = scriptErrors;
    out.consoleErrors = consoleErrors;
    return out;
  }

  return { run };
}
