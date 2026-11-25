/**
 * Endpoint readouts & synchronized Provider Card <-> Models selector scenario
 * (v8.48.0 / PR 11), run when MCC_JSDOM_SCENARIO=endpoint_readouts.
 */

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const clone = (value) => JSON.parse(JSON.stringify(value));

export function prepareEndpointReadoutsScenario({ window, ROUTES }) {
  const xssToken = "<img src=x onerror=boom()>";
  const configPayload = {
    sections: [
      { id: "providers", label: "Providers", description: "Where requests go.", advanced: false },
    ],
    provider_groups: [
      { id: "gateway", label: "Gateways", description: "" },
      { id: "inference", label: "Inference", description: "" },
    ],
    fields: [
      {
        key: "COMMANDCODE_API_KEY",
        label: "Command Code API key",
        section: "providers",
        provider: "commandcode",
        type: "text",
        value: "cc-1111",
        default: "",
        configured: true,
        secret: true,
        advanced: false,
        description: "Command Code key.",
        options: [],
      },
      {
        key: "GROQ_API_KEY",
        label: "Groq API key",
        section: "providers",
        provider: "groq",
        type: "text",
        value: "gsk-2222",
        default: "",
        configured: true,
        secret: true,
        advanced: false,
        description: "Groq key.",
        options: [],
      },
    ],
    provider_status: [
      {
        provider_id: "commandcode",
        display_name: "Command Code",
        group: "gateway",
        kind: "remote",
        status: "configured",
        label: "Configured",
        key_count: 1,
        credential_env: "COMMANDCODE_API_KEY",
        credential_owner_id: "commandcode",
        credential_owner_name: "Command Code",
        credential_shared_with: [],
        endpoint_summary: {
          publishes: [
            { value: "/chat/completions", label: "/chat/completions", model_count: 79 },
            { value: "/responses", label: "/responses", model_count: 79 },
          ],
          can_speak: [
            { value: "chat_completions", label: "Chat Completions (/chat/completions)" },
            { value: "responses", label: "Responses (/responses)" },
            { value: "messages", label: "Messages (/messages)" },
          ],
          observed: [
            {
              surface: "responses",
              label: "Responses",
              count: 4,
              last_used_at: "2026-11-25T15:00:00+00:00",
              last_outcome: "succeeded",
              source: "request_log",
            },
          ],
          surface_preference: {
            offered: [
              { value: "chat_completions", label: "Chat Completions (/chat/completions)" },
              { value: "responses", label: "Responses (/responses)" },
            ],
            preferred: "",
          },
          face_text: "Speaks: Chat Completions, Responses, Messages · Publishes: /chat/completions, /responses",
        },
      },
      {
        provider_id: "groq",
        display_name: "Groq",
        group: "inference",
        kind: "remote",
        status: "configured",
        label: "Configured",
        key_count: 1,
        credential_env: "GROQ_API_KEY",
        credential_owner_id: "groq",
        credential_owner_name: "Groq",
        credential_shared_with: [],
        endpoint_summary: {
          publishes: [],
          can_speak: [
            { value: "chat_completions", label: "Chat Completions (/chat/completions)" },
          ],
          observed: [],
          surface_preference: null,
          face_text: "Speaks: Chat Completions",
        },
      },
    ],
    paths: { managed: "/tmp/probe/.env", repo: "", explicit: null },
    messaging_auth_open: [],
  };

  const customProviders = [
    {
      provider_id: "custom_house",
      display_name: "Single House",
      base_url: "https://single.example/v1",
      key_count: 1,
      masked_keys: ["sk-s…1111"],
      credential_rotation: "failover",
      proxy: null,
      enabled: true,
      model_count: 1,
      status: "configured",
      models: ["m-1"],
      added_at: "2026-11-25T00:00:00Z",
      reasoning_effort_enum: null,
      reasoning_dialect_label: "unknown (not probed)",
      surfaces: ["chat_completions"],
      available_surfaces: [
        { value: "chat_completions", label: "Chat Completions (/chat/completions)" },
        { value: "responses", label: "Responses (/responses)" },
      ],
      media_operations: [],
      available_media_operations: [],
      cache_affinity: "off",
      available_cache_affinity_modes: [],
      auto_paused_refs: [],
      endpoint_summary: {
        publishes: [],
        can_speak: [
          { value: "chat_completions", label: "Chat Completions (/chat/completions)" },
        ],
        observed: [],
        surface_preference: null,
        face_text: "Speaks: Chat Completions",
      },
    },
    {
      provider_id: "custom_dual",
      display_name: "Dual House",
      base_url: "https://dual.example/v1",
      key_count: 2,
      masked_keys: ["sk-d…1111", "sk-d…2222"],
      credential_rotation: "failover",
      proxy: null,
      enabled: true,
      model_count: 1,
      status: "configured",
      models: ["m-dual"],
      added_at: "2026-11-25T00:00:00Z",
      reasoning_effort_enum: ["low", "high"],
      reasoning_dialect_label: "learned {low, high} on 2026-11-25",
      surfaces: ["chat_completions", "responses"],
      available_surfaces: [
        { value: "chat_completions", label: "Chat Completions (/chat/completions)" },
        { value: "responses", label: "Responses (/responses)" },
      ],
      media_operations: [],
      available_media_operations: [],
      cache_affinity: "off",
      available_cache_affinity_modes: [],
      auto_paused_refs: [],
      endpoint_summary: {
        publishes: [{ value: xssToken, label: xssToken, model_count: 1 }],
        can_speak: [
          { value: "chat_completions", label: "Chat Completions (/chat/completions)" },
          { value: "responses", label: "Responses (/responses)" },
        ],
        observed: [],
        surface_preference: {
          offered: [
            { value: "chat_completions", label: "Chat Completions (/chat/completions)" },
            { value: "responses", label: "Responses (/responses)" },
          ],
          preferred: "",
        },
        face_text: "Speaks: Chat Completions, Responses",
      },
    },
  ];

  function makeModelsPayload(ccPreferred = "") {
    return {
      measured_days: 7,
      source_labels: { provider: "provider /models", unknown: "unknown" },
      fact_labels: {},
      learned_source_labels: {},
      kind_labels: { chat: "Chat" },
      catalogue_refresh: { enabled: false },
      providers: [
        {
          provider_id: "commandcode",
          model_count: 2,
          hidden_count: 0,
          override: ccPreferred
            ? { preferred_surface: { state: "value", value: ccPreferred } }
            : {},
          preferences: {},
          surface_preference: {
            offered: [
              { value: "chat_completions", label: "Chat Completions (/chat/completions)" },
              { value: "responses", label: "Responses (/responses)" },
            ],
            preferred: ccPreferred,
          },
          endpoint_summary: {
            ...clone(configPayload.provider_status[0].endpoint_summary),
            surface_preference: {
              offered: [
                { value: "chat_completions", label: "Chat Completions (/chat/completions)" },
                { value: "responses", label: "Responses (/responses)" },
              ],
              preferred: ccPreferred,
            },
          },
          models: [
            {
              model_ref: "commandcode/deepseek/deepseek-v4.1-flash",
              model_id: "deepseek/deepseek-v4.1-flash",
              visible: true,
              hidden_by: "",
              configured: true,
              kind: { kinds: ["chat"], labels: ["Chat"], source: "provider_words", approximate: false },
              has_metadata: true,
              listing: null,
              override: {},
              effective: [],
              preferences: {},
              reasoning_measured: null,
              learned: [],
              last_used_endpoint: {
                surface: "responses",
                outcome: "succeeded",
                ts_epoch: 1764082800,
                ts_iso: "2026-11-25T15:00:00+00:00",
              },
              endpoint_readout: {
                publishes: ["/chat/completions", "/responses"],
                can_speak: [
                  { value: "chat_completions", label: "Chat Completions (/chat/completions)" },
                  { value: "responses", label: "Responses (/responses)" },
                ],
                effective: "chat_completions",
                effective_label: "Chat Completions",
                preferred: ccPreferred,
                source_label: "this provider's declared first door",
                last_used: {
                  surface: "responses",
                  outcome: "succeeded",
                  ts_epoch: 1764082800,
                  ts_iso: "2026-11-25T15:00:00+00:00",
                },
              },
              capabilities: {
                response_surface: {
                  value: "chat_completions",
                  source: "profile",
                  source_label: "this provider's declared first door",
                  approximate: false,
                  reference: false,
                  tier: null,
                  tier_label: null,
                  note: "",
                  label: "chat_completions (profile)",
                  offered: [
                    { value: "chat_completions", label: "Chat Completions (/chat/completions)" },
                    { value: "responses", label: "Responses (/responses)" },
                  ],
                  override: "",
                  discovered: [],
                  preferred: ccPreferred,
                  preferred_scope: ccPreferred ? "provider" : "",
                  model_preferred: "",
                  provider_preferred: ccPreferred,
                  preferred_refusal: "",
                  preferred_note: "",
                },
              },
            },
            {
              model_ref: "commandcode/claude-sonnet-5",
              model_id: "claude-sonnet-5",
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
              last_used_endpoint: null,
              endpoint_readout: {
                publishes: ["/messages"],
                can_speak: [{ value: "messages", label: "Messages (/messages)" }],
                effective: "messages",
                effective_label: "Messages",
                preferred: "",
                source_label: "commandcode native Messages delegate",
                last_used: null,
              },
              capabilities: { response_surface: null },
            },
          ],
        },
        {
          provider_id: "groq",
          model_count: 1,
          hidden_count: 0,
          override: {},
          preferences: {},
          endpoint_summary: clone(configPayload.provider_status[1].endpoint_summary),
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
              last_used_endpoint: {
                surface: "chat_completions",
                outcome: "succeeded",
                ts_epoch: 1764082800,
                ts_iso: "2026-11-25T15:00:00+00:00",
              },
              endpoint_readout: {
                publishes: [xssToken],
                can_speak: [
                  { value: "chat_completions", label: "Chat Completions (/chat/completions)" },
                ],
                effective: "chat_completions",
                effective_label: "Chat Completions",
                preferred: "",
                source_label: "provider's native endpoint",
                last_used: {
                  surface: "chat_completions",
                  outcome: "succeeded",
                  ts_epoch: 1764082800,
                  ts_iso: "2026-11-25T15:00:00+00:00",
                },
              },
              capabilities: { response_surface: null },
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

  ROUTES["/admin/api/config"] = configPayload;
  ROUTES["/admin/api/custom-providers"] = { providers: customProviders };
  ROUTES["/admin/api/model-admin"] = makeModelsPayload("");
  ROUTES["/admin/api/models"] = {
    models: [
      "commandcode/deepseek/deepseek-v4.1-flash",
      "commandcode/claude-sonnet-5",
      "groq/llama-3.3-70b",
    ],
    failed_providers: [],
    blind_models: [],
  };
  ROUTES["/admin/api/requests/latency"] = { enabled: false };
  ROUTES["/admin/api/requests/in-loop"] = { enabled: false, rows: [] };
  ROUTES["/admin/api/credentials/COMMANDCODE_API_KEY/keys"] = {
    count: 1,
    locked: false,
    rows: [{ index: 0, id: "sha256:1111", masked: "cc-…1111", key_label: "cc-…1111", name: "" }],
    keys: ["cc-…1111"],
    health: { keys: [] },
  };
  ROUTES["/admin/api/credentials/GROQ_API_KEY/keys"] = {
    count: 1,
    locked: false,
    rows: [{ index: 0, id: "sha256:2222", masked: "gsk…2222", key_label: "gsk…2222", name: "" }],
    keys: ["gsk…2222"],
    health: { keys: [] },
  };
  for (const cp of customProviders) {
    ROUTES[`/admin/api/custom-providers/${cp.provider_id}/keys`] = {
      provider_id: cp.provider_id,
      count: cp.masked_keys.length,
      locked: false,
      keys: cp.masked_keys,
      rows: cp.masked_keys.map((masked, idx) => ({
        index: idx,
        id: `sha256:${idx}`,
        masked,
        key_label: masked,
        name: "",
      })),
      health: [],
    };
  }

  const posts = [];
  const realFetch = window.fetch;
  window.fetch = (url, options = {}) => {
    const path = String(url).split("?")[0];
    if (options && typeof options.body === "string") {
      posts.push({ path, body: options.body });
      if (path === "/admin/api/model-admin/preferred-surface") {
        const parsed = JSON.parse(options.body);
        ROUTES[path] = makeModelsPayload(parsed.key === "commandcode" ? parsed.surface : "");
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
    if (!(await waitFor(() => doc.querySelector('.pv-card[data-provider="commandcode"]')))) {
      return { scenarioError: "commandcode card did not render" };
    }
    if (!(await waitFor(() => doc.querySelector('[data-custom-provider="custom_house"]')))) {
      return { scenarioError: "custom_house card did not render" };
    }

    const ccCard = doc.querySelector('.pv-card[data-provider="commandcode"]');
    const groqCard = doc.querySelector('.pv-card[data-provider="groq"]');
    const customSingleCard = doc.querySelector('[data-custom-provider="custom_house"]');
    const customDualCard = doc.querySelector('[data-custom-provider="custom_dual"]');

    const faces = {
      ccButtons: Array.from(ccCard.querySelectorAll("button")).map((b) => b.textContent),
      ccDataKeys: ccCard.querySelectorAll("[data-key]").length,
      ccEndpointsFace: flat(ccCard.querySelector(".provider-endpoints-summary")),
      groqEndpointsFace: flat(groqCard.querySelector(".provider-endpoints-summary")),
      customSingleEndpointsFace: flat(customSingleCard.querySelector(".provider-endpoints-summary")),
    };

    // Open single-door custom overlay -> serves chat_completions, panel present, NO select.
    customSingleCard.click();
    await wait(120);
    const customSingleModal = {
      details: flat(doc.querySelector("#providerDetailBody .cp-details")),
      panelText: flat(doc.querySelector("#providerDetailBody .pv-endpoints-panel")),
      selectCount: doc.querySelectorAll("#providerDetailBody .pv-preferred-surface").length,
    };
    doc.getElementById("providerDetailClose").click();
    await wait(80);

    // Open multi-door custom overlay -> panel placed before .cp-actions, Delete still last button, XSS escaped.
    customDualCard.click();
    await wait(120);
    const customBody = doc.getElementById("providerDetailBody");
    const customButtons = Array.from(customBody.querySelectorAll("button")).map((b) =>
      b.textContent.trim(),
    );
    const customDualModal = {
      regions: Array.from(customBody.querySelector(".pv-detail").children).map(
        (n) => n.className.split(" ")[0],
      ),
      selectCount: customBody.querySelectorAll(".pv-preferred-surface").length,
      selectHasDataKey: Boolean(
        customBody.querySelector(".pv-preferred-surface")?.hasAttribute("data-key"),
      ),
      deleteIsLast: customButtons[customButtons.length - 1] === "Delete",
      injectedImgs: customBody.querySelectorAll("img").length,
    };
    doc.getElementById("providerDetailClose").click();
    await wait(80);

    // Open groq overlay -> endpoints panel, no selector.
    groqCard.click();
    await wait(120);
    const groqModal = {
      panelText: flat(doc.querySelector("#providerDetailBody .pv-endpoints-panel")),
      selectCount: doc.querySelectorAll("#providerDetailBody .pv-preferred-surface").length,
    };
    doc.getElementById("providerDetailClose").click();
    await wait(80);

    // Navigate to Models page first so both views are live, then open CommandCode modal and sync!
    doc.querySelector('.nav-link[data-view="models"]').click();
    if (!(await waitFor(() => doc.querySelector('.models-provider[data-provider="commandcode"]')))) {
      return { scenarioError: "models view did not render commandcode" };
    }
    const ccModelsNode = doc.querySelector('.models-provider[data-provider="commandcode"]');
    const groqModelsNode = doc.querySelector('.models-provider[data-provider="groq"]');
    ccModelsNode.querySelector(".models-provider-toggle").click();
    groqModelsNode.querySelector(".models-provider-toggle").click();
    await wait(120);
    ccModelsNode.querySelector("details.models-provider-settings").open = true;
    await wait(120);

    const ccModelRows = Array.from(ccModelsNode.querySelectorAll("details.models-model"));
    for (const row of ccModelRows) {
      row.open = true;
    }
    const groqModelRow = groqModelsNode.querySelector("details.models-model");
    groqModelRow.open = true;
    await wait(150);

    const modelsInitial = {
      ccRollup: flat(ccModelsNode.querySelector(".models-provider-endpoints")),
      groqRollup: flat(groqModelsNode.querySelector(".models-provider-endpoints")),
      groqLastUsedChip: flat(groqModelRow.querySelector(".models-chip-endpoint")),
      groqReadout: flat(groqModelRow.querySelector(".models-endpoint-readout")),
      groqInjectedImgs: groqModelRow.querySelectorAll("img").length,
      ccClaudeReadout: flat(ccModelRows[1].querySelector(".models-endpoint-readout")),
      ccDeepseekLastUsedChip: flat(ccModelRows[0].querySelector(".models-chip-endpoint")),
    };

    // Go back to Providers, open CommandCode modal, change Preferred wire endpoint -> chat_completions
    doc.querySelector('.nav-link[data-view="providers"]').click();
    await wait(120);
    doc.querySelector('.pv-card[data-provider="commandcode"]').click();
    await wait(120);
    const modalSelect = doc.querySelector("#providerDetailBody .pv-preferred-surface");
    const modalStatus = doc.querySelector("#providerDetailBody .pv-preferred-surface-status");
    const ccModalInitial = {
      panelText: flat(doc.querySelector("#providerDetailBody .pv-endpoints-panel")),
      selectValue: modalSelect ? modalSelect.value : null,
      selectHasDataKey: modalSelect ? modalSelect.hasAttribute("data-key") : null,
    };
    modalSelect.value = "chat_completions";
    modalSelect.dispatchEvent(new window.Event("change", { bubbles: true }));
    await wait(200);
    const ccModalAfterSave = {
      status: flat(modalStatus),
      selectValue: modalSelect.value,
    };
    doc.getElementById("providerDetailClose").click();
    await wait(80);

    // Check Models page was synchronized immediately!
    doc.querySelector('.nav-link[data-view="models"]').click();
    await wait(120);
    const syncedModels = {
      providerSelectValue: doc.querySelector(
        '.models-provider[data-provider="commandcode"] .models-provider-surface-select',
      )?.value,
      rollupText: flat(
        doc.querySelector(
          '.models-provider[data-provider="commandcode"] .models-provider-endpoints',
        ),
      ),
    };

    // Change via Models page back to responses and verify modal re-opens with responses!
    const modelsPrefSelect = doc.querySelector(
      '.models-provider[data-provider="commandcode"] .models-provider-surface-select',
    );
    const modelsPrefSave = doc.querySelector(
      '.models-provider[data-provider="commandcode"] .models-provider-surface-save',
    );
    modelsPrefSelect.value = "responses";
    modelsPrefSave.click();
    await wait(200);
    doc.querySelector('.nav-link[data-view="providers"]').click();
    await wait(120);
    doc.querySelector('.pv-card[data-provider="commandcode"]').click();
    await wait(120);
    const modalSyncedFromModels =
      doc.querySelector("#providerDetailBody .pv-preferred-surface")?.value ?? null;

    return {
      fatal: null,
      scriptErrors,
      consoleErrors,
      xssToken,
      faces,
      customSingleModal,
      customDualModal,
      groqModal,
      modelsInitial,
      ccModalInitial,
      ccModalAfterSave,
      syncedModels,
      modalSyncedFromModels,
      posts,
    };
  }

  return { run };
}
