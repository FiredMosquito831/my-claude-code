/**
 * The Models page's one table per model (7.101.0, X2), run instead of the main
 * capture when MCC_JSDOM_SCENARIO=models_fields.
 *
 * The user's words (2026-10-10 19:39): every field, the overridable ones
 * included, looks like the read-only "What MCC knows about this model" table
 * -- field, value, rung badge, provenance -- with the override control inline
 * and the provider's or the ladder's DEFAULT always visible beside it.
 *
 * What this drives, on a payload in the server's own shapes:
 *
 *  - three models of one custom provider (a forced output cap and a forced
 *    temperature; a forced context window and a forced-unset top_p; a forced
 *    reasoning word with an output limit only OpenRouter's live list states),
 *    a provider row that forces top_k and an output cap, and a second
 *    provider that is never opened;
 *  - every row each model draws, with its value, badge, rung lines and the
 *    default line(s) under its control;
 *  - the Save body, byte for byte, for a fixed set of edits (the overrides
 *    file keeps the order the body names new keys in), the refused saves'
 *    messages, and what a save repaints;
 *  - the provider-wide table, the "override(s) active" chip and how many
 *    nodes a collapsed provider costs.
 *
 * The capture reads BOTH the 7.100.0 page (separate editor, effective table
 * and capability table) and the 7.101.0 one (one table), so the same file
 * records the old page and replays the same gestures on the new one.
 *
 * Prints nothing itself: `run()` returns the measurements and the harness
 * prints them as one JSON object.
 */

import { readFileSync } from "node:fs";

const clone = (value) => (value === undefined ? value : JSON.parse(JSON.stringify(value)));
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const PROVIDER = "custom_agnes";
const REF_A = `${PROVIDER}/agnes-3.0-flash`;
const REF_B = `${PROVIDER}/agnes-2.5-flash`;
const REF_C = `${PROVIDER}/agnes-1.5-pro`;
const EDITABLE = [
  "frequency_penalty",
  "min_p",
  "presence_penalty",
  "repetition_penalty",
  "seed",
  "stop",
  "temperature",
  "top_k",
  "top_p",
];

// The server's own sentences (model_admin.py), copied so the fixture reads
// like a real answer.
const OUTPUT_CAP_NOTE =
  "A cap, not a request: a client asking for fewer tokens still gets fewer, " +
  "and reasoning room is widened to your number rather than past it.";
const OUTPUT_NO_LIMIT_NOTE =
  "Nothing publishes an output limit for this model, so your number becomes " +
  "the limit.";
const PROVIDER_PREFERENCE_NOTE =
  "A default for every model under this provider. Each model's own row wins " +
  "where it states one, and each model clamps this to what it actually " +
  "accepts.";
const PROVIDER_OUTPUT_NOTE =
  "A cap for every model under this provider. Each model's own published " +
  "limit still wins where it is lower.";
const CONTEXT_LENGTH_SOURCE_NOTE =
  "The routed deployment's own /models payload wins outright. Where it " +
  "published nothing, the value comes off the same ten-rung ladder the " +
  "output limit walks, and the tier beside it names the rung.";
const OPERATOR_CONTEXT_LENGTH_NOTE =
  "Your number from this model's override row. It is the window routing " +
  "keeps the output budget within and the one every generated agent " +
  "catalogue publishes.";

const sourced = (value, source, sourceLabel, tier = null, tierLabel = null, extra = {}) => ({
  value,
  source,
  source_label: sourceLabel,
  approximate: source === "approximate",
  reference: false,
  tier,
  tier_label: tierLabel,
  ...extra,
});
const unknown = () => sourced(null, "unknown", "unknown");
const PROVIDER_EXACT = "provider /models, exact id";
const BUCKET_EXACT = "models.dev bucket, exact id";

const rowState = (row) =>
  Object.fromEntries(
    Object.keys(row)
      .sort()
      .map((name) => [
        name,
        { state: row[name] === null ? "unset" : "value", value: row[name] },
      ]),
  );

const PROVIDER_ROW = { max_output_tokens: 8000, top_k: 40 };

function effectiveFor(modelRow) {
  return EDITABLE.map((name) => {
    const fromModel = Object.prototype.hasOwnProperty.call(modelRow, name);
    const fromProvider = Object.prototype.hasOwnProperty.call(PROVIDER_ROW, name);
    const value = fromModel ? modelRow[name] : fromProvider ? PROVIDER_ROW[name] : null;
    const said = fromModel || fromProvider;
    return {
      name,
      action: !said ? "inherit" : value === null ? "unset" : "force",
      value,
      from: fromModel ? "model" : fromProvider ? "provider" : null,
    };
  });
}

const preferenceState = (row, name) =>
  Object.prototype.hasOwnProperty.call(row, name)
    ? { state: row[name] === null ? "unset" : "value", value: row[name] }
    : { state: "inherit", value: null };

const EFFORT_OPTIONS = (offered) => [
  { value: "client", label: "From client", available: true, reason: null },
  { value: "off", label: "Off", available: true, reason: null },
  {
    value: "adaptive",
    label: "Adaptive",
    available: false,
    reason: "this host has no adaptive channel",
  },
  ...offered.map((word) => ({
    value: word,
    label: word.charAt(0).toUpperCase() + word.slice(1),
    available: true,
    reason: null,
  })),
];

function reasoningFields(shape) {
  if (shape === "bucket") {
    return {
      can_reason: unknown(),
      supports_effort_control: sourced(true, "models_dev", "models.dev", 3, BUCKET_EXACT),
      supports_toggle_control: unknown(),
      supports_budget_control: unknown(),
      supported_efforts: sourced(["high", "low", "medium"], "models_dev", "models.dev", 3, BUCKET_EXACT),
      mandatory: unknown(),
      default_enabled: unknown(),
    };
  }
  return {
    can_reason: sourced(true, "provider", "provider /models", 1, PROVIDER_EXACT),
    supports_effort_control: sourced(true, "provider", "provider /models", 1, PROVIDER_EXACT),
    supports_toggle_control: sourced(false, "provider", "provider /models", 1, PROVIDER_EXACT),
    supports_budget_control: sourced(false, "provider", "provider /models", 1, PROVIDER_EXACT),
    supported_efforts: sourced(["high", "low", "medium"], "provider", "provider /models", 1, PROVIDER_EXACT),
    mandatory: sourced(false, "provider", "provider /models", 1, PROVIDER_EXACT),
    default_enabled: sourced(true, "provider", "provider /models", 1, PROVIDER_EXACT),
  };
}

const DIALECT_EFFORT = {
  known: true,
  source: "host_dialect",
  source_label: "host dialect",
  effort_field: "reasoning_effort",
  effort_values: ["high", "low", "medium", "minimal"],
  toggle: false,
  toggle_field: null,
  budget: false,
  budget_field: null,
  off: false,
  adaptive: false,
  origin: "default",
  origin_label: "default OpenAI dialect",
  learned_rejections: [],
};

function modelA(row) {
  const output = unknown();
  return {
    model_ref: REF_A,
    model_id: "agnes-3.0-flash",
    visible: true,
    hidden_by: "",
    configured: true,
    kind: {
      kinds: ["chat"],
      labels: ["Chat"],
      source: "provider_words",
      source_label: "the provider's model type or endpoints",
      tier: PROVIDER_EXACT,
      approximate: false,
    },
    has_metadata: true,
    listing: null,
    override: rowState(row),
    effective: effectiveFor(row),
    preferences: {
      reasoning_preference: {
        ...preferenceState(row, "reasoning_preference"),
        options: EFFORT_OPTIONS(["minimal", "low", "medium", "high"]),
        capability_known: true,
        can_reason: null,
      },
      max_output_tokens: {
        ...preferenceState(row, "max_output_tokens"),
        limit: null,
        limit_source_label: output.source_label,
        limit_tier_label: null,
        note: OUTPUT_NO_LIMIT_NOTE,
      },
    },
    reasoning_measured: null,
    learned: [],
    capabilities: {
      max_output_tokens: output,
      context_length: Object.prototype.hasOwnProperty.call(row, "context_length")
        ? operatorWindow(row.context_length, 524288)
        : sourced(524288, "provider", "provider /models", 1, PROVIDER_EXACT, {
            note: CONTEXT_LENGTH_SOURCE_NOTE,
          }),
      supports_vision: sourced(true, "provider_or_models_dev", "provider /models or models.dev"),
      supports_tool_calls: sourced(true, "provider", "provider /models", 1, PROVIDER_EXACT),
      input_price: sourced(0.05, "provider_or_models_dev", "provider /models or models.dev"),
      output_price: sourced(0.15, "provider_or_models_dev", "provider /models or models.dev"),
      cache_read_price: sourced(0.01, "models_dev", "models.dev", 3, BUCKET_EXACT),
      cache_write_price: unknown(),
      reasoning_price: sourced(0.2, "approximate", "approximate cross-provider", 8, "cross-provider, exact id", {
        match_count: 3,
        agreement: 0.67,
        reporters: 2,
      }),
      supported_parameters: sourced(
        ["max_tokens", "reasoning_effort", "temperature", "tools", "top_p"],
        "provider",
        "provider /models",
      ),
      default_parameters: sourced(
        [
          ["temperature", 0.7],
          ["top_p", 0.95],
        ],
        "provider",
        "provider /models",
      ),
      reasoning: reasoningFields("bucket"),
      reasoning_dialect: DIALECT_EFFORT,
      response_surface: {
        value: "chat_completions",
        source: "default",
        source_label: "this provider's first surface",
        approximate: false,
        reference: false,
        tier: null,
        tier_label: null,
        note: "",
        label: "chat_completions (default)",
        offered: [
          { value: "chat_completions", label: "Chat Completions (/chat/completions)" },
          { value: "responses", label: "Responses (/responses)" },
        ],
        override: "",
      },
      declared_modalities: sourced("text → text", "provider", "provider /models", 1, PROVIDER_EXACT, {
        inputs: ["text"],
        outputs: ["text"],
      }),
      declared_type: unknown(),
      declared_endpoints: sourced(["openai"], "provider", "provider /models", 1, PROVIDER_EXACT),
      description: sourced("OpenRouter's words.", "openrouter_live", "OpenRouter live", null, "OpenRouter live, exact id", {
        also_stated: {
          value: "The provider's own words.",
          source: "provider",
          source_label: "provider /models",
          tier: 1,
          tier_label: PROVIDER_EXACT,
        },
      }),
      knowledge_cutoff: sourced("2025-06", "models_dev", "models.dev", 3, BUCKET_EXACT),
      published_at: sourced("2026-08-26", "provider", "provider /models", 1, PROVIDER_EXACT, {
        note: "The day the provider's own model list says it published this model.",
      }),
      listed_on_openrouter: sourced("2026-02-11", "openrouter_live", "OpenRouter live", null, "OpenRouter live, exact id", {
        note: "The day OpenRouter added it to its list -- not the model's release date.",
      }),
      retires_at: unknown(),
    },
  };
}

function operatorWindow(value, extracted) {
  return {
    value,
    source: "operator",
    source_label: "operator override",
    approximate: false,
    reference: false,
    tier: null,
    tier_label: null,
    note:
      value === null
        ? "Forced unknown on this model's override row: no window bounds the output budget."
        : OPERATOR_CONTEXT_LENGTH_NOTE,
    also_stated: {
      value: extracted,
      source: "provider",
      source_label: "provider /models",
      tier: 1,
      tier_label: PROVIDER_EXACT,
    },
  };
}

function modelB(row) {
  return {
    model_ref: REF_B,
    model_id: "agnes-2.5-flash",
    visible: true,
    hidden_by: "",
    configured: false,
    kind: {
      kinds: ["chat"],
      labels: ["Chat"],
      source: "models_dev",
      source_label: "models.dev modalities",
      tier: "cross-provider, exact id",
      approximate: true,
    },
    has_metadata: true,
    listing: null,
    override: rowState(row),
    effective: effectiveFor(row),
    preferences: {
      reasoning_preference: {
        ...preferenceState(row, "reasoning_preference"),
        options: EFFORT_OPTIONS(["low", "medium", "high"]),
        capability_known: true,
        can_reason: true,
      },
      max_output_tokens: {
        ...preferenceState(row, "max_output_tokens"),
        limit: 40960,
        limit_source_label: "models.dev",
        limit_tier_label: BUCKET_EXACT,
        note: OUTPUT_CAP_NOTE,
      },
    },
    reasoning_measured: null,
    learned: [],
    capabilities: {
      max_output_tokens: sourced(40960, "models_dev", "models.dev", 3, BUCKET_EXACT),
      context_length: Object.prototype.hasOwnProperty.call(row, "context_length")
        ? operatorWindow(row.context_length, 1048576)
        : sourced(1048576, "provider", "provider /models", 1, PROVIDER_EXACT, {
            note: CONTEXT_LENGTH_SOURCE_NOTE,
          }),
      supports_vision: sourced(true, "approximate", "approximate cross-provider", 8, "cross-provider, exact id", {
        match_count: 4,
        agreement: 1,
        reporters: 4,
      }),
      supports_tool_calls: unknown(),
      input_price: sourced(0, "provider_or_models_dev", "provider /models or models.dev"),
      output_price: sourced(0, "provider_or_models_dev", "provider /models or models.dev"),
      cache_read_price: unknown(),
      cache_write_price: unknown(),
      reasoning_price: unknown(),
      supported_parameters: unknown(),
      default_parameters: unknown(),
      reasoning: reasoningFields("provider"),
      reasoning_dialect: { known: false, source: "unknown", source_label: "unknown" },
      response_surface: null,
      declared_modalities: unknown(),
      declared_type: sourced("chat", "provider", "provider /models", 1, PROVIDER_EXACT),
      declared_endpoints: unknown(),
      description: unknown(),
      knowledge_cutoff: unknown(),
      published_at: unknown(),
      retires_at: unknown(),
    },
  };
}

function modelC(row) {
  return {
    model_ref: REF_C,
    model_id: "agnes-1.5-pro",
    visible: true,
    hidden_by: "",
    configured: false,
    kind: { kinds: null, labels: [], source: null, source_label: null, tier: null, approximate: false },
    has_metadata: true,
    listing: null,
    override: rowState(row),
    effective: effectiveFor(row),
    preferences: {
      reasoning_preference: {
        ...preferenceState(row, "reasoning_preference"),
        options: EFFORT_OPTIONS(["low", "medium", "high"]),
        capability_known: true,
        can_reason: true,
      },
      max_output_tokens: {
        ...preferenceState(row, "max_output_tokens"),
        limit: 32768,
        limit_source_label: "provider /models",
        limit_tier_label: PROVIDER_EXACT,
        note: OUTPUT_CAP_NOTE,
      },
    },
    reasoning_measured: null,
    learned: [],
    capabilities: {
      // OpenRouter's live list placed after `preferences` was built: the page
      // shows its number, routing bounds by the 32,768 above.
      max_output_tokens: sourced(131072, "openrouter_live", "OpenRouter live", null, "OpenRouter live, exact id", {
        note: "OpenRouter live, exact id (agnes/agnes-1.5-pro). From OpenRouter's own live model list.",
      }),
      context_length: sourced(262144, "models_dev", "models.dev", 3, BUCKET_EXACT, {
        note: CONTEXT_LENGTH_SOURCE_NOTE,
      }),
      supports_vision: sourced(false, "provider_or_models_dev", "provider /models or models.dev"),
      input_price: unknown(),
      output_price: unknown(),
      cache_read_price: unknown(),
      cache_write_price: unknown(),
      reasoning_price: unknown(),
      supported_parameters: sourced([], "provider", "provider /models"),
      default_parameters: sourced([], "provider", "provider /models"),
      reasoning: reasoningFields("provider"),
      reasoning_dialect: DIALECT_EFFORT,
      response_surface: null,
    },
  };
}

const ROWS = {
  [REF_A]: { max_output_tokens: 65000, temperature: 0.6 },
  [REF_B]: { context_length: 1000000, top_p: null },
  [REF_C]: { reasoning_preference: "high" },
};
const BUILD = { [REF_A]: modelA, [REF_B]: modelB, [REF_C]: modelC };

function providerEntry(providerId, models, row) {
  return {
    provider_id: providerId,
    model_count: models.length,
    hidden_count: 0,
    override: rowState(row),
    preferences: {
      reasoning_preference: {
        ...preferenceState(row, "reasoning_preference"),
        options: ["off", "client", "adaptive", "minimal", "low", "medium", "high", "xhigh", "max"].map(
          (value) => ({ value, label: value, available: true, reason: null }),
        ),
        capability_known: false,
        note: PROVIDER_PREFERENCE_NOTE,
      },
      max_output_tokens: {
        ...preferenceState(row, "max_output_tokens"),
        limit: null,
        limit_source_label: null,
        limit_tier_label: null,
        note: PROVIDER_OUTPUT_NOTE,
      },
    },
    models,
  };
}

/* MCC_JSDOM_MODELS_FIELDS_PROMPT_CACHE names a file holding the server's
   real `prompt_cache_lifetimes` table (7.105.0). When it is set, the payload
   is the 7.105.0 server's: the 14th key's kind beside the other preferences,
   its control on every model and provider row -- inherited everywhere, so
   no row of this scenario sets it -- and the table beside the page. When it
   is not set, the payload is byte for byte the one 7.104.0 captured, which is
   how the same file records the old page and replays the new one. */
const PROMPT_CACHE_TABLE_PATH = process.env.MCC_JSDOM_MODELS_FIELDS_PROMPT_CACHE || "";

function withPromptCache(payload) {
  if (!PROMPT_CACHE_TABLE_PATH) return payload;
  const table = JSON.parse(readFileSync(PROMPT_CACHE_TABLE_PATH, "utf-8"));
  const control = (row, note) => ({
    ...preferenceState(row, "prompt_cache_ttl"),
    minimum: 1,
    maximum: 60,
    note,
  });
  const rows = (entry) =>
    Object.fromEntries(
      Object.entries(entry.override || {}).map(([name, state]) => [name, state.value]),
    );
  payload.overrides.preference_parameters = {
    max_output_tokens: "integer",
    prompt_cache_ttl: "minutes",
    reasoning_preference: "enum",
  };
  payload.providers.forEach((provider) => {
    provider.preferences.prompt_cache_ttl = control(rows(provider), table.provider_control_note);
    provider.models.forEach((model) => {
      model.preferences.prompt_cache_ttl = control(rows(model), table.model_control_note);
    });
  });
  payload.prompt_cache_lifetimes = table.table;
  return payload;
}

function page(rows) {
  return withPromptCache(plainPage(rows));
}

function plainPage(rows) {
  const zetaModels = ["zeta/one", "zeta/two", "zeta/three"].map((ref) => ({
    ...modelC({}),
    model_ref: ref,
    model_id: ref.split("/")[1],
  }));
  return {
    measured_days: 7,
    source_labels: {
      provider: "provider /models",
      provider_or_models_dev: "provider /models or models.dev",
      models_dev: "models.dev",
      approximate: "approximate cross-provider",
      host_dialect: "host dialect",
      learned: "learned from this host",
      unknown: "unknown",
    },
    fact_labels: {},
    learned_source_labels: {},
    kind_labels: { chat: "Chat", image: "Image", tts: "Speech", asr: "Transcription", video: "Video" },
    catalogue_refresh: { enabled: false },
    providers: [
      providerEntry(
        PROVIDER,
        [REF_A, REF_B, REF_C].map((ref) => BUILD[ref](rows[ref])),
        rows.provider,
      ),
      providerEntry("zeta", zetaModels, {}),
    ],
    overrides: {
      providers: {},
      models: {},
      editable_parameters: EDITABLE,
      preference_parameters: { max_output_tokens: "integer", reasoning_preference: "enum" },
      owned_elsewhere: {},
      inherit_sentinel: "inherit",
    },
    visibility: {
      allow_raw: "",
      deny_raw: "",
      hide_only_notice: "Hiding is display-only; a hidden model still resolves.",
      hidden_route_refs: [],
    },
  };
}

// What the server answers after model A's save below: the same edits applied
// the way `merged_override_row` applies them (an existing key keeps its place,
// a new one is appended in the order the body names it).
const ROWS_AFTER_A = {
  [REF_A]: {
    max_output_tokens: 65000,
    temperature: 0.8,
    reasoning_preference: "high",
    context_length: 200000,
    top_p: null,
  },
  [REF_B]: ROWS[REF_B],
  [REF_C]: ROWS[REF_C],
  provider: PROVIDER_ROW,
};

export function prepareModelsFieldsScenario({ window, ROUTES }) {
  const before = page({ ...ROWS, provider: PROVIDER_ROW });
  const after = page(ROWS_AFTER_A);
  ROUTES["/admin/api/model-admin"] = clone(before);
  ROUTES["/admin/api/model-admin/overrides"] = clone(after);
  ROUTES["/admin/api/model-admin/surface"] = clone(after);
  ROUTES["/admin/api/requests/latency"] = { enabled: false };

  const doc = window.document;
  const flat = (el) => (el ? (el.textContent || "").replace(/\s+/g, " ").trim() : "");
  const settle = () => wait(160);

  const posts = [];
  const installRecorder = () => {
    const realFetch = window.fetch;
    window.fetch = (url, options = {}) => {
      if (options && typeof options.body === "string") {
        posts.push({ path: String(url).split("?")[0], body: options.body });
      }
      return realFetch(url, options);
    };
  };

  const rowFacts = (tr) => {
    const th = tr.querySelector("th");
    const label = th
      ? flat(th.querySelector("label") || th.querySelector(".models-fields-label") || th)
      : "";
    const cells = Array.from(tr.children);
    const sourceCell = cells[2] || null;
    const badge = sourceCell ? sourceCell.querySelector(".models-source") : null;
    return {
      label,
      value: flat(cells[1]),
      badgeClass: badge ? badge.className : null,
      badgeText: badge ? flat(badge) : null,
      notes: sourceCell
        ? Array.from(sourceCell.querySelectorAll(".models-approx-note")).map(flat)
        : [],
    };
  };

  const describeOld = (body) => ({
    capabilityRows: Array.from(
      body.querySelectorAll(".models-capabilities table.models-table tr"),
    ).map(rowFacts),
    effectiveRows: Array.from(body.querySelectorAll(".models-effective table tr")).map(
      (tr) => Array.from(tr.children).map(flat),
    ),
    effectiveEmpty: flat(body.querySelector(".models-effective .models-empty-note")),
    editorRows: Array.from(
      body.querySelectorAll(".models-override-row:not(.models-override-head)"),
    ).map((row) => {
      const mode = row.querySelector("select.models-override-mode");
      const box = row.querySelector(".models-override-value");
      return {
        label: flat(row.querySelector(".models-override-name")),
        modeId: mode ? mode.id : null,
        mode: mode ? mode.value : null,
        value: box ? box.value : null,
        notes: Array.from(row.querySelectorAll(".models-preference-note")).map(flat),
      };
    }),
  });

  const describeNew = (body) => {
    const table = body.querySelector("table.models-fields");
    if (!table) return null;
    return {
      ariaLabel: table.getAttribute("aria-label"),
      head: Array.from(table.querySelectorAll("thead th")).map(flat),
      headScopes: Array.from(table.querySelectorAll("thead th")).map((th) =>
        th.getAttribute("scope"),
      ),
      groups: Array.from(table.querySelectorAll("tbody")).map((tbody) => ({
        group: tbody.dataset.group,
        head: flat(tbody.querySelector(".models-fields-grouphead th")),
        headScope: (tbody.querySelector(".models-fields-grouphead th") || {
          getAttribute: () => null,
        }).getAttribute("scope"),
        fields: Array.from(tbody.querySelectorAll("tr[data-field]")).map(
          (tr) => tr.dataset.field,
        ),
      })),
      rows: Array.from(table.querySelectorAll("tr[data-field]")).map((tr) => {
        const control = tr.querySelector(".models-fields-control");
        const mode = control ? control.querySelector("select.models-override-mode") : null;
        const box = control ? control.querySelector(".models-override-value") : null;
        const label = tr.querySelector("th label");
        const described = (el) => {
          const ids = el ? (el.getAttribute("aria-describedby") || "").split(/\s+/) : [];
          return ids.filter(Boolean).map((id) => {
            const target = doc.getElementById(id);
            return target ? flat(target) : `missing #${id}`;
          });
        };
        return {
          ...rowFacts(tr),
          field: tr.dataset.field,
          overridable: tr.classList.contains("is-overridable"),
          readonly: tr.classList.contains("models-fields-readonly"),
          forced: tr.classList.contains("is-forced"),
          thScope: (tr.querySelector("th") || { getAttribute: () => null }).getAttribute("scope"),
          labelFor: label ? label.htmlFor : null,
          labelForResolves: label ? Boolean(doc.getElementById(label.htmlFor)) : null,
          modeId: mode ? mode.id : null,
          mode: mode ? mode.value : null,
          boxId: box ? box.id : null,
          boxTag: box ? box.tagName.toLowerCase() : null,
          boxType: box ? box.type || null : null,
          boxAria: box ? box.getAttribute("aria-label") : null,
          boxValue: box ? box.value : null,
          boxDisabled: box ? box.disabled : null,
          boxMax: box ? box.getAttribute("max") : null,
          boxDescribedBy: described(box),
          modeDescribedBy: described(mode),
          tabOrder: control
            ? Array.from(control.querySelectorAll("select, input, button")).map(
                (el) => el.id || el.className,
              )
            : [],
          defaults: control
            ? Array.from(control.querySelectorAll(".models-fields-default")).map(flat)
            : [],
          defaultSources: control
            ? Array.from(
                control.querySelectorAll(".models-fields-default .models-source"),
              ).map((el) => ({ className: el.className, text: flat(el) }))
            : [],
          defaultNotes: control
            ? Array.from(
                control.querySelectorAll(".models-fields-default .models-approx-note"),
              ).map(flat)
            : [],
          controlNotes: control
            ? Array.from(control.querySelectorAll(".models-preference-note")).map(flat)
            : [],
          controlText: flat(control),
          srOnly: control
            ? Array.from(control.querySelectorAll(".sr-only")).map(flat)
            : [],
          surfaceEditor: Boolean(tr.querySelector(".models-surface-override")),
          surfaceButton: (() => {
            const button = tr.querySelector(".models-surface-save");
            return button ? { text: flat(button), className: button.className } : null;
          })(),
        };
      }),
    };
  };

  const modelNode = (ref) =>
    Array.from(doc.querySelectorAll("details.models-model")).find(
      (node) => flat(node.querySelector(".models-ref")) === ref,
    ) || null;

  const describeModel = (ref) => {
    const node = modelNode(ref);
    if (!node) return null;
    const body = node.querySelector(".models-model-body");
    const chip = node.querySelector("summary .models-chip-forced");
    return {
      chip: chip ? flat(chip) : null,
      filled: body ? body.dataset.filled || "" : "",
      old: body ? describeOld(body) : null,
      table: body ? describeNew(body) : null,
      readouts: body
        ? Array.from((body.querySelector(".models-readouts") || body).children).map(
            (el) => el.className,
          )
        : [],
      dialect: flat(body && body.querySelector(".models-dialect")),
      emptyNote: Array.from(body ? body.querySelectorAll(".models-empty-note") : []).map(flat),
    };
  };

  const openModel = async (ref) => {
    const node = modelNode(ref);
    if (!node) return false;
    node.open = true;
    await settle();
    return true;
  };

  const setMode = (id, value) => {
    const mode = doc.getElementById(id);
    if (!mode) return `missing #${id}`;
    mode.value = value;
    mode.dispatchEvent(new window.Event("change", { bubbles: true }));
    return doc.activeElement ? doc.activeElement.id || "" : "";
  };
  const setBox = (id, value) => {
    const box = doc.getElementById(id);
    if (!box) return `missing #${id}`;
    box.value = value;
    box.dispatchEvent(new window.Event("input", { bubbles: true }));
    return "";
  };
  const saveIn = async (root) => {
    const button = Array.from(root.querySelectorAll(".models-actions button")).find(
      (el) => flat(el) === "Save overrides",
    );
    if (!button) return { clicked: false };
    const status = button.parentElement.querySelector(".models-status");
    button.click();
    await wait(260);
    return {
      clicked: true,
      status: flat(status),
      statusClass: status ? status.className : null,
      statusConnected: status ? status.isConnected : null,
    };
  };

  const idFor = (prefix, scope, key, name) =>
    `${prefix}-${scope}-${key}-${name}`.replace(/[^A-Za-z0-9_-]/g, "-");

  const sha256 = async (text) => {
    const { createHash } = await import("node:crypto");
    return createHash("sha256").update(text).digest("hex");
  };

  async function run({ scriptErrors, consoleErrors }) {
    await wait(900);
    installRecorder();
    const out = { scenarioFatal: null };
    const navLinks = Array.from(doc.querySelectorAll(".nav-link"));
    const modelsLink = navLinks.find((link) => link.dataset.view === "models");
    if (!modelsLink) {
      out.scenarioFatal = "no Models nav link";
      return out;
    }
    modelsLink.click();
    await wait(600);
    const tree = doc.getElementById("modelsTree");

    // --- C7: nothing is built for a provider nobody opened.
    const providerNodes = Array.from(tree.querySelectorAll(".models-provider"));
    out.collapsed = {
      providers: providerNodes.map((node) => node.dataset.provider),
      selects: tree.querySelectorAll("select").length,
      tables: tree.querySelectorAll("table").length,
      fieldTables: tree.querySelectorAll("table.models-fields").length,
      nodes: tree.querySelectorAll("*").length,
    };

    // The media section, as the first open of the view drew it: shown only,
    // never touched by this change.
    const mediaSection = doc.getElementById("section-media-models");
    out.mediaSection = mediaSection
      ? {
          length: mediaSection.outerHTML.length,
          sha256: await sha256(mediaSection.outerHTML),
          fieldsRows: mediaSection.querySelectorAll(".media-request-fields-row").length,
        }
      : null;

    const providerNode = (id) =>
      providerNodes.find((node) => node.dataset.provider === id) || null;
    const agnes = providerNode(PROVIDER);
    const toggle = agnes ? agnes.querySelector(".models-provider-toggle") : null;
    if (toggle) {
      toggle.click();
      await settle();
    }
    const zeta = providerNode("zeta");
    out.openProvider = {
      modelRows: agnes ? agnes.querySelectorAll(".models-model-row").length : 0,
      filledBodies: agnes
        ? agnes.querySelectorAll('.models-model-body[data-filled="1"]').length
        : 0,
      modelSelects: agnes
        ? agnes.querySelectorAll(".models-model-body select").length
        : 0,
      modelFieldTables: agnes
        ? agnes.querySelectorAll(".models-model-body table.models-fields").length
        : 0,
      zetaSelects: zeta ? zeta.querySelectorAll("select").length : null,
      zetaTables: zeta ? zeta.querySelectorAll("table").length : null,
      zetaNodes: zeta ? zeta.querySelectorAll("*").length : null,
    };

    // --- the provider-wide table (C5), opened by its own disclosure.
    const settings = agnes ? agnes.querySelector("details.models-provider-settings") : null;
    if (settings && !settings.open) {
      settings.open = true;
      await settle();
    }
    out.provider = settings
      ? {
          open: settings.open,
          summary: flat(settings.querySelector("summary")),
          old: describeOld(settings),
          table: describeNew(settings),
        }
      : null;

    // --- the three models, as first drawn.
    out.models = {};
    for (const ref of [REF_A, REF_B, REF_C]) {
      await openModel(ref);
      out.models[ref] = describeModel(ref);
    }

    // --- a mode change to Force value enables and focuses the value box.
    const focusId = idFor("ov", "model", REF_C, "seed");
    out.focusAfterForceValue = setMode(`${focusId}-mode`, "value");
    out.focusBoxDisabled = (doc.getElementById(`${focusId}-value`) || {}).disabled;
    setMode(`${focusId}-mode`, "inherit");
    out.focusBoxDisabledAgain = (doc.getElementById(`${focusId}-value`) || {}).disabled;

    // --- C2: every override row's default stays on screen whatever its mode
    // says, on a model with the provider's default pairs (A), one whose
    // provider published none (B) and one whose list pins nothing (C). Each
    // mode is put back afterwards, so the saves below send what they did.
    const shown = (el) => {
      for (let node = el; node; node = node.parentElement) {
        if (node.hidden || (node.style && node.style.display === "none")) return false;
      }
      return true;
    };
    out.modeSweep = [];
    for (const ref of [REF_A, REF_B, REF_C]) {
      const node = modelNode(ref);
      if (!node) continue;
      const modes = Array.from(
        node.querySelectorAll(".models-model-body select.models-override-mode"),
      );
      for (const mode of modes) {
        const tr = mode.closest("tr");
        const box = doc.getElementById(mode.id.replace(/-mode$/, "-value"));
        const was = mode.value;
        for (const value of ["inherit", "unset", "value"]) {
          mode.value = value;
          mode.dispatchEvent(new window.Event("change", { bubbles: true }));
          const lines = tr ? Array.from(tr.querySelectorAll(".models-fields-default")) : [];
          out.modeSweep.push({
            ref,
            field: tr ? tr.dataset.field : mode.id,
            mode: value,
            defaults: lines.map(flat),
            allShown: lines.length > 0 && lines.every(shown),
            boxDisabled: box ? box.disabled : null,
          });
        }
        mode.value = was;
        mode.dispatchEvent(new window.Event("change", { bubbles: true }));
      }
    }
    // Put focus back where nothing reads it, so no later capture depends on
    // which box the sweep left focused.
    if (doc.activeElement && doc.activeElement.blur) doc.activeElement.blur();

    // --- refused saves: the same messages, in the same order, as before.
    const nodeC = modelNode(REF_C);
    out.refused = {};
    if (nodeC) {
      const ctx = idFor("pref", "model", REF_C, "context_length");
      setMode(`${ctx}-mode`, "value");
      setBox(`${ctx}-value`, "1.5");
      out.refused.fraction = await saveIn(nodeC);
      setBox(`${ctx}-value`, "200000");
      const out1 = idFor("pref", "model", REF_C, "max_output_tokens");
      setMode(`${out1}-mode`, "value");
      setBox(`${out1}-value`, "99999");
      out.refused.overCap = await saveIn(nodeC);
      setBox(`${out1}-value`, "");
      out.refused.blank = await saveIn(nodeC);
      setMode(`${out1}-mode`, "inherit");
      setMode(`${ctx}-mode`, "inherit");
    }
    out.postsAfterRefusals = posts.length;

    // --- C4/C8: the bodies three saves send, byte for byte.
    const nodeB = modelNode(REF_B);
    if (nodeB) out.saveB = await saveIn(nodeB);

    const nodeA = modelNode(REF_A);
    if (nodeA) {
      const aBodyBefore = nodeA.querySelector(".models-model-body");
      const keptMode = doc.getElementById(`${idFor("ov", "model", REF_A, "temperature")}-mode`);
      setMode(`${idFor("ov", "model", REF_A, "temperature")}-mode`, "value");
      setBox(`${idFor("ov", "model", REF_A, "temperature")}-value`, "0.8");
      setMode(`${idFor("ov", "model", REF_A, "top_p")}-mode`, "unset");
      setMode(`${idFor("pref", "model", REF_A, "context_length")}-mode`, "value");
      setBox(`${idFor("pref", "model", REF_A, "context_length")}-value`, "200000");
      setMode(`${idFor("pref", "model", REF_A, "reasoning_preference")}-mode`, "value");
      const reasoningBox = doc.getElementById(
        `${idFor("pref", "model", REF_A, "reasoning_preference")}-value`,
      );
      if (reasoningBox) {
        reasoningBox.value = "high";
        reasoningBox.dispatchEvent(new window.Event("change", { bubbles: true }));
      }
      out.saveA = await saveIn(nodeA);
      // What a save repaints: the controls and the status line are the same
      // elements, the value columns and the default lines are the new answer.
      out.afterSaveA = {
        sameBody: nodeA.querySelector(".models-model-body") === aBodyBefore,
        sameModeControl:
          doc.getElementById(`${idFor("ov", "model", REF_A, "temperature")}-mode`) === keptMode,
        model: describeModel(REF_A),
      };
    }

    if (settings) {
      // The server's answer to the provider save: its row as merged.
      ROUTES["/admin/api/model-admin/overrides"] = clone(
        page({ ...ROWS_AFTER_A, provider: { max_output_tokens: null, top_k: 40, temperature: 0.5 } }),
      );
      setMode(`${idFor("ov", "provider", PROVIDER, "temperature")}-mode`, "value");
      setBox(`${idFor("ov", "provider", PROVIDER, "temperature")}-value`, "0.5");
      setMode(`${idFor("pref", "provider", PROVIDER, "max_output_tokens")}-mode`, "unset");
      out.saveProvider = await saveIn(settings);
      out.afterSaveProvider = { table: describeNew(settings) };
    }

    // --- the surface pin keeps its own button and its own write.
    if (nodeA) {
      const pin = nodeA.querySelector("select.models-surface-select");
      const pinSave = nodeA.querySelector(".models-surface-save");
      out.surface = {
        present: Boolean(pin && pinSave),
        options: pin ? Array.from(pin.options).map((option) => option.value) : [],
        button: pinSave ? { text: flat(pinSave), className: pinSave.className } : null,
        inFieldRow: pin ? Boolean(pin.closest("tr[data-field]")) : null,
      };
      if (pin && pinSave) {
        pin.value = "responses";
        const status = pinSave.parentElement.querySelector(".models-status");
        pinSave.click();
        await wait(260);
        out.surface.status = flat(status);
        out.surface.statusConnected = status ? status.isConnected : null;
      }
    }

    out.posts = posts.map((entry) => ({ path: entry.path, body: entry.body }));

    // --- the MCP notes under the Model Config page's media rails (7.99.0),
    // drawn by code this change never touches.
    const routing = navLinks.find((link) => link.dataset.view === "model_config");
    if (routing) {
      routing.click();
      await wait(400);
      const notes = Array.from(doc.querySelectorAll(".route-mcp-note"));
      out.mcpNotes = {
        count: notes.length,
        sha256: await sha256(notes.map((note) => note.outerHTML).join("\n")),
      };
    }

    out.scriptErrors = scriptErrors.slice();
    out.consoleErrors = consoleErrors.slice();
    return out;
  }

  return { run };
}
