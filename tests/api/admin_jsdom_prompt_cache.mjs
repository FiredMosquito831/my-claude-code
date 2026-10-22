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
 *  - the Models page (7.105.0): the "prompt cache lifetime" row is the 14th
 *    overridable key, `prompt_cache_ttl`. For a declared provider it shows
 *    what the host would be sent, the host's lifetimes (a fresh learned
 *    refusal named, with Forget) and the Model Config lifetime each route
 *    reads as its default; a model row's minutes, a provider row's minutes
 *    and a model row's Force unset each decide; the gateways' Messages models
 *    say "lifetimes unknown — sent only when set on this row", and a
 *    host-wide refusal stops them; an undeclared provider says "lifetime
 *    control not known for this host". The provider editor carries the same
 *    row, the "override(s) active" chip counts the key, and a Save sends it
 *    last, validated to whole minutes 1-60.
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
const UNSET_REF = `${DECLARED}/claude-sonnet-5-5`;
// The rule's edges, each forced on its own model row (parity with the server's
// adapt_prompt_cache_minutes, asserted by the Python test).
const MINUTE_REFS = [1, 4, 5, 6, 30, 59, 60].map((minutes) => [
  `${DECLARED}/edge-${minutes}`,
  minutes,
]);
const GATEWAY_PLAIN_REF = "commandcode/claude-opus-5-5";
const GATEWAY_ROW_REF = "commandcode/claude-sonnet-5-5";
const GATEWAY_OPEN_REF = "commandcode/deepseek/deepseek-v4-flash";
const GATEWAY_REFUSED_REF = "opencode/claude-fable-5-1";

const HOST_REFUSAL_FACT = {
  fact_kind: "prompt_cache_lifetime_refused",
  fact_label: "cache lifetime refused",
  model_id: "*",
  value: true,
  detail: "1h",
  source: "rejection",
  source_label: "the host's own rejection",
  learned_at: "2026-10-22T07:00:00Z",
  last_confirmed_at: "2026-10-22T07:00:00Z",
  age_seconds: 60,
  ttl_seconds: 2592000,
  stale: false,
  retired: false,
  hits: 1,
  evidence: "system.1.cache_control.ephemeral.ttl: Extra inputs are not permitted",
};

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
  const rowState = (row) =>
    Object.fromEntries(
      Object.keys(row)
        .sort()
        .map((name) => [
          name,
          { state: row[name] === null ? "unset" : "value", value: row[name] },
        ]),
    );
  const control = (row, note) => ({
    ...(Object.prototype.hasOwnProperty.call(row, "prompt_cache_ttl")
      ? {
          state: row.prompt_cache_ttl === null ? "unset" : "value",
          value: row.prompt_cache_ttl,
        }
      : { state: "inherit", value: null }),
    minimum: 1,
    maximum: 60,
    note,
  });
  const modelEntry = (ref, row, learned) => ({
    ...clone(alpha.models[1]),
    model_ref: ref,
    model_id: ref.slice(ref.indexOf("/") + 1),
    configured: false,
    override: rowState(row),
    effective: [],
    learned: learned || [],
    preferences: {
      ...clone(alpha.models[1].preferences),
      prompt_cache_ttl: control(row, fixture.model_control_note),
    },
  });
  const providerEntry = (providerId, row, models) => ({
    ...clone(alpha),
    provider_id: providerId,
    model_count: models.length,
    hidden_count: 0,
    override: rowState(row),
    preferences: {
      ...clone(alpha.preferences),
      prompt_cache_ttl: control(row, fixture.provider_control_note),
    },
    models,
  });
  // The 7.105.0 server's shapes on the harness's own providers too.
  page.providers.forEach((provider) => {
    provider.preferences.prompt_cache_ttl = control({}, fixture.provider_control_note);
    provider.models.forEach((model) => {
      model.preferences.prompt_cache_ttl = control({}, fixture.model_control_note);
    });
  });
  page.overrides.preference_parameters = fixture.preference_parameters;
  const declaredModels = [
    modelEntry(REFUSED_REF, { prompt_cache_ttl: 10 }, [clone(REFUSAL_FACT)]),
    modelEntry(PLAIN_REF, {}),
    modelEntry(UNSET_REF, { prompt_cache_ttl: null }),
    ...MINUTE_REFS.map(([ref, minutes]) => modelEntry(ref, { prompt_cache_ttl: minutes })),
  ];
  page.providers.push(
    providerEntry(DECLARED, { prompt_cache_ttl: 45 }, declaredModels),
    providerEntry("commandcode", {}, [
      modelEntry(GATEWAY_PLAIN_REF, {}),
      modelEntry(GATEWAY_ROW_REF, { prompt_cache_ttl: 20 }),
      modelEntry(GATEWAY_OPEN_REF, { prompt_cache_ttl: 20 }),
    ]),
    providerEntry("opencode", { prompt_cache_ttl: 20 }, [
      modelEntry(GATEWAY_REFUSED_REF, {}, [clone(HOST_REFUSAL_FACT)]),
    ]),
  );
  page.prompt_cache_lifetimes = clone(fixture.prompt_cache_lifetimes);
  ROUTES["/admin/api/model-admin"] = page;
  ROUTES["/admin/api/model-admin/overrides"] = clone(page);
  ROUTES["/admin/api/model-admin/learned/forget"] = { forgotten: 1 };

  const doc = window.document;
  const flat = (el) => (el ? (el.textContent || "").replace(/\s+/g, " ").trim() : "");
  const settle = () => wait(200);

  const applied = [];
  const posts = [];
  const installRecorder = () => {
    const realFetch = window.fetch;
    window.fetch = async (url, options = {}) => {
      const path = String(url).split("?")[0];
      if (
        path === "/admin/api/model-admin/overrides" ||
        path === "/admin/api/model-admin/learned/forget"
      ) {
        posts.push({ path, body: options.body });
      }
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
    const chip = node.querySelector("summary .models-chip-forced");
    return {
      ref,
      row: describeRow(node.querySelector('tr[data-field="prompt_cache_ttl"]')),
      // 7.104.0's read-only row is gone: the overridable one replaces it.
      oldRow: Boolean(node.querySelector('tr[data-field="prompt_cache_lifetimes"]')),
      chip: chip ? flat(chip) : null,
      emptyNote: flat(node.querySelector(".models-empty-note")),
    };
  };

  function describeRow(tr) {
    if (!tr) return null;
    const cells = Array.from(tr.children);
    const badge = cells[2] ? cells[2].querySelector(".models-source") : null;
    const group = tr.closest("tbody");
    const mode = tr.querySelector("select.models-override-mode");
    const box = tr.querySelector("input.models-override-value");
    const label = tr.querySelector("th label");
    return {
      label: flat(label),
      fileKey: flat(tr.querySelector(".models-fields-key")),
      labelForResolves: label ? Boolean(doc.getElementById(label.htmlFor)) : null,
      value: flat(cells[1]),
      badgeClass: badge ? badge.className : null,
      badge: flat(badge),
      notes: Array.from((cells[2] || tr).querySelectorAll(".models-approx-note")).map(flat),
      forgets: Array.from((cells[2] || tr).querySelectorAll("button.models-learned-forget")).map(
        flat,
      ),
      overridable: tr.classList.contains("is-overridable"),
      forced: tr.classList.contains("is-forced"),
      readonly: tr.classList.contains("models-fields-readonly"),
      group: group ? group.dataset.group : null,
      lastInGroup: group ? group.lastElementChild === tr : null,
      mode: mode ? mode.value : null,
      modeId: mode ? mode.id : null,
      boxId: box ? box.id : null,
      boxValue: box ? box.value : null,
      boxMin: box ? box.getAttribute("min") : null,
      boxMax: box ? box.getAttribute("max") : null,
      boxAria: box ? box.getAttribute("aria-label") : null,
      boxDisabled: box ? box.disabled : null,
      describedBy: box ? flat(doc.getElementById(box.getAttribute("aria-describedby"))) : null,
      defaults: Array.from(tr.querySelectorAll(".models-fields-default")).map(flat),
      controlNotes: Array.from(tr.querySelectorAll(".models-preference-note")).map(flat),
    };
  }

  const openProvider = async (id) => {
    const tree = doc.getElementById("modelsTree");
    const node = Array.from(tree.querySelectorAll(".models-provider")).find(
      (el) => el.dataset.provider === id,
    );
    const toggle = node ? node.querySelector(".models-provider-toggle") : null;
    if (toggle) {
      toggle.click();
      await settle();
    }
    return node;
  };

  const providerRow = async (node) => {
    const settings = node ? node.querySelector("details.models-provider-settings") : null;
    if (!settings) return null;
    settings.open = true;
    await settle();
    return describeRow(settings.querySelector('tr[data-field="prompt_cache_ttl"]'));
  };

  const modelNode = (ref) =>
    Array.from(doc.querySelectorAll("details.models-model")).find(
      (el) => flat(el.querySelector(".models-ref")) === ref,
    ) || null;

  const saveWith = async (ref, mode, value) => {
    const node = modelNode(ref);
    const tr = node ? node.querySelector('tr[data-field="prompt_cache_ttl"]') : null;
    if (!tr) return { missing: ref };
    const select = tr.querySelector("select.models-override-mode");
    const box = tr.querySelector("input.models-override-value");
    select.value = mode;
    select.dispatchEvent(new window.Event("change", { bubbles: true }));
    box.value = value;
    box.dispatchEvent(new window.Event("input", { bubbles: true }));
    const before = posts.length;
    const button = Array.from(node.querySelectorAll(".models-actions button")).find(
      (el) => flat(el) === "Save overrides",
    );
    button.click();
    await wait(300);
    const status = button.parentElement.querySelector(".models-status");
    return {
      status: flat(status),
      statusClass: status ? status.className : null,
      posted: posts.slice(before).map((entry) => JSON.parse(entry.body)),
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
    const providerNodes = {};
    for (const id of [DECLARED, "alpha", "commandcode", "opencode"]) {
      providerNodes[id] = await openProvider(id);
    }
    out.models = {
      refused: await lifetimeRow(REFUSED_REF),
      plain: await lifetimeRow(PLAIN_REF),
      unset: await lifetimeRow(UNSET_REF),
      undeclared: await lifetimeRow(UNDECLARED_REF),
      gatewayPlain: await lifetimeRow(GATEWAY_PLAIN_REF),
      gatewayRow: await lifetimeRow(GATEWAY_ROW_REF),
      gatewayOpen: await lifetimeRow(GATEWAY_OPEN_REF),
      gatewayRefused: await lifetimeRow(GATEWAY_REFUSED_REF),
    };
    out.edges = {};
    for (const [ref, minutes] of MINUTE_REFS) {
      const described = await lifetimeRow(ref);
      out.edges[String(minutes)] = described.row ? described.row.value : null;
    }
    out.providers = {
      declared: await providerRow(providerNodes[DECLARED]),
      alpha: await providerRow(providerNodes.alpha),
      commandcode: await providerRow(providerNodes.commandcode),
      opencode: await providerRow(providerNodes.opencode),
    };

    // Saves: refused out of range (nothing posted), then 20 minutes, sent last.
    out.saves = {
      zero: await saveWith(PLAIN_REF, "value", "0"),
      tooMany: await saveWith(PLAIN_REF, "value", "61"),
      fraction: await saveWith(PLAIN_REF, "value", "2.5"),
      twenty: await saveWith(PLAIN_REF, "value", "20"),
      unset: await saveWith(PLAIN_REF, "unset", ""),
    };

    // Forget on the host-wide refusal names the fact's own subject.
    const refusedNode = modelNode(GATEWAY_REFUSED_REF);
    const forget = refusedNode
      ? refusedNode.querySelector('tr[data-field="prompt_cache_ttl"] button.models-learned-forget')
      : null;
    const beforeForget = posts.length;
    if (forget) {
      forget.click();
      await wait(400);
    }
    out.forgetPosted = posts.slice(beforeForget).map((entry) => ({
      path: entry.path,
      body: JSON.parse(entry.body),
    }));

    out.scriptErrors = scriptErrors;
    out.consoleErrors = consoleErrors;
    return out;
  }

  return { run };
}
