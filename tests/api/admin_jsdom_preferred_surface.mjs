/**
 * Preferred wire surface UI scenario (v8.32.0, PR 1), run when
 * MCC_JSDOM_SCENARIO=preferred_surface.
 *
 * Proves:
 *  - Multi-door provider (`opencode_go`, >= 2 offered doors) renders BOTH the
 *    provider-level Preferred selector and the model-level Preferred selector
 *    beside the Pin selector, plus Q10 `discovered`, Pin vs Preferred
 *    distinction note, and Q9 refusal message.
 *  - Single-door provider (`groq`, no `surface_preference`, `response_surface:
 *    null`) renders zero surface controls at either provider or model scope (Q11).
 *  - Clicking "Set preference" at model scope and provider scope POSTs to
 *    `/admin/api/model-admin/preferred-surface`, keeps `"Saved"` connected in
 *    the DOM, and updates readouts cleanly.
 */

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const clone = (value) => JSON.parse(JSON.stringify(value));

function makePage({
  providerPreferred = "",
  modelPreferred = "",
  preferredRefusal = "",
} = {}) {
  const effective = modelPreferred || providerPreferred;
  const scope = modelPreferred ? "model" : providerPreferred ? "provider" : "";
  const providerOverride = providerPreferred
    ? { preferred_surface: { state: "value", value: providerPreferred } }
    : {};
  const modelOverride = modelPreferred
    ? { preferred_surface: { state: "value", value: modelPreferred } }
    : {};

  return {
    measured_days: 7,
    source_labels: {
      provider: "provider /models",
      unknown: "unknown",
    },
    fact_labels: {},
    learned_source_labels: {},
    kind_labels: { chat: "Chat" },
    catalogue_refresh: { enabled: false },
    providers: [
      {
        provider_id: "opencode_go",
        model_count: 1,
        hidden_count: 0,
        override: providerOverride,
        preferences: {},
        surface_preference: {
          offered: [
            { value: "chat_completions", label: "Chat Completions (/chat/completions)" },
            { value: "responses", label: "Responses (/responses)" },
            { value: "messages", label: "Messages (/messages)" },
          ],
          preferred: providerPreferred,
        },
        models: [
          {
            model_ref: "opencode_go/minimax-m3",
            model_id: "minimax-m3",
            visible: true,
            hidden_by: "",
            configured: true,
            kind: { kinds: ["chat"], labels: ["Chat"], source: "provider_words", approximate: false },
            has_metadata: true,
            listing: null,
            override: modelOverride,
            effective: [],
            preferences: {},
            reasoning_measured: null,
            learned: [],
            capabilities: {
              response_surface: {
                value: "messages",
                source: "registry",
                source_label: "the vendor's published registry",
                approximate: false,
                reference: false,
                tier: null,
                tier_label: null,
                note: "@ai-sdk/anthropic",
                label: "messages (registry)",
                offered: [
                  { value: "chat_completions", label: "Chat Completions (/chat/completions)" },
                  { value: "responses", label: "Responses (/responses)" },
                  { value: "messages", label: "Messages (/messages)" },
                ],
                override: "",
                discovered: [
                  {
                    value: "messages",
                    label: "Messages (/messages)",
                    source: "registry",
                    detail: "@ai-sdk/anthropic",
                  },
                ],
                preferred: effective,
                preferred_scope: scope,
                model_preferred: modelPreferred,
                provider_preferred: providerPreferred,
                preferred_refusal: preferredRefusal,
                preferred_note: "",
              },
            },
          },
        ],
      },
      {
        provider_id: "groq",
        model_count: 1,
        hidden_count: 0,
        override: {},
        preferences: {},
        models: [
          {
            model_ref: "groq/llama-3.3-70b",
            model_id: "llama-3.3-70b",
            visible: true,
            hidden_by: "",
            configured: false,
            kind: { kinds: ["chat"], labels: ["Chat"], source: "provider_words", approximate: false },
            has_metadata: true,
            listing: null,
            override: {},
            effective: [],
            preferences: {},
            reasoning_measured: null,
            learned: [],
            capabilities: {
              response_surface: null,
            },
          },
        ],
      },
    ],
    overrides: {
      providers: {},
      models: {},
      editable_parameters: [],
      preference_parameters: {},
      owned_elsewhere: {},
      inherit_sentinel: "inherit",
    },
    visibility: {
      allow_raw: "",
      deny_raw: "",
      hide_only_notice: "Hiding is display-only.",
      hidden_route_refs: [],
    },
  };
}

export function preparePreferredSurfaceScenario({ window, ROUTES }) {
  ROUTES["/admin/api/model-admin"] = clone(
    makePage({
      providerPreferred: "",
      modelPreferred: "",
      preferredRefusal:
        "you prefer Chat Completions; this host refused it at 20:25 UTC; serving Messages",
    }),
  );
  ROUTES["/admin/api/requests/latency"] = { enabled: false };
  ROUTES["/admin/api/requests/in-loop"] = { enabled: false, rows: [] };

  const posts = [];
  const realFetch = window.fetch;
  window.fetch = (url, options = {}) => {
    const path = String(url).split("?")[0];
    if (options && typeof options.body === "string") {
      posts.push({ path, body: options.body });
      if (path === "/admin/api/model-admin/preferred-surface") {
        const parsed = JSON.parse(options.body);
        if (parsed.scope === "model") {
          ROUTES[path] = clone(makePage({ modelPreferred: parsed.surface }));
        } else {
          ROUTES[path] = clone(makePage({ providerPreferred: parsed.surface }));
        }
      }
    }
    return realFetch(url, options);
  };

  const doc = window.document;
  const flat = (el) => (el ? (el.textContent || "").replace(/\s+/g, " ").trim() : "");
  const waitFor = async (predicate, timeoutMs = 20000) => {
    const started = Date.now();
    while (Date.now() - started < timeoutMs) {
      if (predicate()) return true;
      await wait(50);
    }
    return false;
  };

  async function run({ scriptErrors, consoleErrors }) {
    if (!(await waitFor(() => doc.querySelector('.nav-link[data-view="models"]')))) {
      return { scenarioError: "no Models nav link" };
    }
    doc.querySelector('.nav-link[data-view="models"]').click();
    if (!(await waitFor(() => doc.querySelector('.models-provider[data-provider="opencode_go"]')))) {
      return { scenarioError: "no opencode_go provider node" };
    }

    const goNode = doc.querySelector('.models-provider[data-provider="opencode_go"]');
    const groqNode = doc.querySelector('.models-provider[data-provider="groq"]');
    goNode.querySelector(".models-provider-toggle").click();
    groqNode.querySelector(".models-provider-toggle").click();
    await wait(120);

    const goSettings = goNode.querySelector("details.models-provider-settings");
    goSettings.open = true;
    const groqSettings = groqNode.querySelector("details.models-provider-settings");
    groqSettings.open = true;
    await wait(120);

    const goModel = goNode.querySelector("details.models-model");
    goModel.open = true;
    const groqModel = groqNode.querySelector("details.models-model");
    groqModel.open = true;
    await wait(150);

    const initial = {
      goProviderPrefPresent: Boolean(goNode.querySelector(".models-provider-surface-select")),
      groqProviderPrefPresent: Boolean(groqNode.querySelector(".models-provider-surface-select")),
      groqModelSurfacePresent: Boolean(groqModel.querySelector(".models-surface-override")),
      goModelPrefOptions: Array.from(
        (goModel.querySelector(".models-surface-preferred-select") || {}).options || [],
      ).map((opt) => ({ value: opt.value, text: opt.textContent })),
      discoveredText: flat(goModel.querySelector(".models-surface-discovered")),
      distinctionText: flat(goModel.querySelector(".models-surface-distinction")),
      refusalText: flat(goModel.querySelector(".models-surface-refusal")),
    };

    // Save model-level preference -> responses.
    const modelSelect = goModel.querySelector(".models-surface-preferred-select");
    const modelSave = goModel.querySelector(".models-surface-preferred-save");
    const modelStatus = modelSave.parentElement.querySelector(".models-status");
    modelSelect.value = "responses";
    modelSave.click();
    await wait(200);
    const afterModelSave = {
      status: flat(modelStatus),
      statusConnected: modelStatus.isConnected,
      refusalHidden: Boolean((goModel.querySelector(".models-surface-refusal") || {}).hidden),
    };

    // Save provider-level preference -> chat_completions (after clearing model back to "").
    modelSelect.value = "";
    modelSave.click();
    await wait(200);
    const providerSelect = goNode.querySelector(".models-provider-surface-select");
    const providerSave = goNode.querySelector(".models-provider-surface-save");
    const providerStatus = providerSave.parentElement.querySelector(".models-status");
    providerSelect.value = "chat_completions";
    providerSave.click();
    await wait(200);
    const afterProviderSave = {
      status: flat(providerStatus),
      statusConnected: providerStatus.isConnected,
      modelInheritOptionText:
        modelSelect.options[0] ? modelSelect.options[0].textContent : "",
    };

    return {
      initial,
      afterModelSave,
      afterProviderSave,
      posts,
      scriptErrors: scriptErrors.slice(),
      consoleErrors: consoleErrors.slice(),
    };
  }

  return { run };
}
