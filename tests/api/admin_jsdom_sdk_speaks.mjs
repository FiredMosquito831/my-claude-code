/**
 * Coding-agents "Speaks" readout & SDK client cards scenario (v8.49.0, PR 12),
 * run when MCC_JSDOM_SCENARIO=sdk_harnesses_and_speaks.
 *
 * Proves:
 *  - Launchable CLI cards show `Speaks` in `.agent-meta` and `.agent-surfaces-speaks-hint`
 *    inside `.agent-surfaces`, while preserving `Protocol` and ALL >=2-door provider
 *    options (Q8: never hiding MCC-translated doors).
 *  - SDK cards (`claude_agent_sdk`, `openai_sdk`, XSS probe entry) render inside
 *    `#codingAgentsList` under `.sdk-agent-card[data-sdk-harness]` — NEVER carrying
 *    `.coding-agent-card` or `data-harness` — with `"SDK client · attribution"` badge,
 *    `Speaks`, `Requests (7d)` from `/admin/api/requests/harness-usage`, and
 *    `.agent-surfaces` posting `POST /admin/api/harness-surfaces`.
 *  - Agents find box (`#codingAgentsFilter`) filters `.sdk-agent-card` alongside
 *    `.coding-agent-card`.
 *  - Attacker-shaped markup in `sdk_harnesses` `display_name` / `summary` renders
 *    strictly as text (`0` injected HTML elements).
 */

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const clone = (value) => JSON.parse(JSON.stringify(value));

const XSS_SDK_NAME = '<img src=x onerror="window.__sdkXss=1">Hostile <b>SDK</b>';

function makeHarnessSurfacesPayload(selections = {}) {
  return {
    providers: [
      {
        provider_id: "commandcode",
        display_name: "Command Code",
        custom: false,
        configured: true,
        discovered_model_count: 105,
        status_summary: "Configured · 105 model(s)",
        offered: [
          { value: "chat_completions", label: "Chat Completions (/chat/completions)" },
          { value: "responses", label: "Responses (/responses)" },
        ],
      },
      {
        provider_id: "opencode_go",
        display_name: "OpenCode Go",
        custom: false,
        configured: true,
        discovered_model_count: 37,
        status_summary: "Configured · 37 model(s)",
        offered: [
          { value: "chat_completions", label: "Chat Completions (/chat/completions)" },
          { value: "responses", label: "Responses (/responses)" },
          { value: "messages", label: "Messages (/messages)" },
        ],
      },
    ],
    harnesses: {
      claude: { commandcode: selections.claude_cc || "", opencode_go: "" },
      codex: { commandcode: "", opencode_go: "" },
      opencode: { commandcode: "", opencode_go: "" },
      claude_agent_sdk: {
        commandcode: selections.claude_agent_sdk_cc || "",
        opencode_go: "",
      },
      openai_sdk: {
        commandcode: selections.openai_sdk_cc || "",
        opencode_go: "",
      },
      xss_sdk: { commandcode: "", opencode_go: "" },
    },
    sdk_harnesses: [
      {
        id: "claude_agent_sdk",
        display_name: "Claude Agent SDK",
        protocol: "anthropic_messages",
        protocol_label: "Anthropic Messages (POST /v1/messages)",
        speaks: ["anthropic_messages"],
        speaks_labels: ["Anthropic Messages (POST /v1/messages)"],
        summary: "Anthropic's Claude Agent SDK (Python/TypeScript).",
      },
      {
        id: "openai_sdk",
        display_name: "OpenAI SDK",
        protocol: "openai_chat_completions",
        protocol_label: "OpenAI Chat Completions (POST /v1/chat/completions)",
        speaks: ["openai_chat_completions", "openai_responses"],
        speaks_labels: [
          "OpenAI Chat Completions (POST /v1/chat/completions)",
          "OpenAI Responses (POST /v1/responses)",
        ],
        summary: "Official OpenAI SDKs (Python/Node/JS).",
      },
      {
        id: "xss_sdk",
        display_name: XSS_SDK_NAME,
        protocol: "anthropic_messages",
        protocol_label: "Anthropic Messages (POST /v1/messages)",
        speaks: ["anthropic_messages"],
        speaks_labels: ["Anthropic Messages (POST /v1/messages)"],
        summary: '<script>window.__sdkXss=2</script>Untrusted SDK summary',
      },
    ],
    attribution_note:
      "This is what the client said it was (x-mcc-harness, else its user-agent). " +
      "It is a label, not a login; Claude Desktop shares claude. " +
      "Applies per host; fallback models on other providers ignore it.",
  };
}

export function prepareSdkHarnessesAndSpeaksScenario({ window, ROUTES }) {
  const selections = {
    claude_cc: "",
    claude_agent_sdk_cc: "",
    openai_sdk_cc: "",
  };
  const baseHarnesses = clone(ROUTES["/admin/api/harnesses"]);
  for (const h of baseHarnesses.harnesses || []) {
    if (h.id === "opencode") {
      h.speaks = ["anthropic_messages", "openai_responses", "openai_chat_completions"];
      h.speaks_labels = [
        "Anthropic Messages (POST /v1/messages)",
        "OpenAI Responses (POST /v1/responses)",
        "OpenAI Chat Completions (POST /v1/chat/completions)",
      ];
    } else if (h.id === "commandcode_cli") {
      h.speaks = ["anthropic_messages", "openai_chat_completions"];
      h.speaks_labels = [
        "Anthropic Messages (POST /v1/messages)",
        "OpenAI Chat Completions (POST /v1/chat/completions)",
      ];
    } else {
      h.speaks = [h.protocol];
      h.speaks_labels = [h.protocol_label];
    }
  }
  ROUTES["/admin/api/harnesses"] = baseHarnesses;
  ROUTES["/admin/api/harness-surfaces"] = clone(makeHarnessSurfacesPayload(selections));
  ROUTES["/admin/api/requests/harness-usage"] = {
    enabled: true,
    days: 7,
    counts: {
      claude: 3120,
      opencode: 12480,
      claude_agent_sdk: 31,
      openai_sdk: 5,
    },
  };

  const posts = [];
  const realFetch = window.fetch;
  window.fetch = (url, options = {}) => {
    const path = String(url).split("?")[0];
    if (path === "/admin/api/harness-surfaces" && options && typeof options.body === "string") {
      posts.push({ path, body: options.body });
      const parsed = JSON.parse(options.body);
      const next = parsed.surface === "auto" ? "" : (parsed.surface || "");
      if (parsed.harness === "claude_agent_sdk" && parsed.provider === "commandcode") {
        selections.claude_agent_sdk_cc = next;
      }
      ROUTES[path] = clone(makeHarnessSurfacesPayload(selections));
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
    if (!(await waitFor(() => doc.querySelector('.nav-link[data-view="coding_agents"]')))) {
      return { scenarioError: "no Coding agents nav link" };
    }
    doc.querySelector('.nav-link[data-view="coding_agents"]').click();
    if (
      !(await waitFor(
        () => doc.querySelector('.sdk-agent-card[data-sdk-harness="claude_agent_sdk"] .agent-surfaces'),
      ))
    ) {
      return { scenarioError: "no claude_agent_sdk card rendered" };
    }
    await wait(120);

    const list = doc.getElementById("codingAgentsList");
    const launchableCards = Array.from(list.querySelectorAll(".coding-agent-card"));
    const sdkCards = Array.from(list.querySelectorAll(".sdk-agent-card"));

    const opencodeCard = list.querySelector('.coding-agent-card[data-harness="opencode"]');
    const opencodeMeta = Object.fromEntries(
      Array.from(opencodeCard.querySelectorAll(".agent-meta dt")).map((dt, i) => [
        dt.textContent,
        opencodeCard.querySelectorAll(".agent-meta dd")[i]?.textContent || "",
      ]),
    );
    const claudeCard = list.querySelector('.coding-agent-card[data-harness="claude"]');
    const claudeSurfaces = claudeCard.querySelector(".agent-surfaces");
    const claudeCcOptions = Array.from(
      claudeSurfaces.querySelectorAll('.agent-surface[data-provider="commandcode"] select option'),
    ).map((opt) => opt.value);

    const sdkCard = list.querySelector('.sdk-agent-card[data-sdk-harness="claude_agent_sdk"]');
    const sdkMeta = Object.fromEntries(
      Array.from(sdkCard.querySelectorAll(".agent-meta dt")).map((dt, i) => [
        dt.textContent,
        sdkCard.querySelectorAll(".agent-meta dd")[i]?.textContent || "",
      ]),
    );
    const sdkSurfaces = sdkCard.querySelector(".agent-surfaces");
    sdkSurfaces.open = true;
    const sdkCcRow = sdkSurfaces.querySelector('.agent-surface[data-provider="commandcode"]');
    const sdkCcSelect = sdkCcRow.querySelector(".agent-surface-select");
    const sdkCcSave = sdkCcRow.querySelector(".agent-surface-save");
    const sdkCcFeedback = sdkCcRow.querySelector(".agent-surface-feedback");
    const sdkCcOptions = Array.from(sdkCcSelect.options).map((opt) => opt.value);

    const xssCard = list.querySelector('.sdk-agent-card[data-sdk-harness="xss_sdk"]');

    const initial = {
      launchableCount: launchableCards.length,
      sdkCardIds: sdkCards.map((c) => c.dataset.sdkHarness),
      sdkCardsHaveCodingAgentClass: sdkCards.some((c) => c.classList.contains("coding-agent-card")),
      sdkCardsHaveDataHarness: sdkCards.some((c) => Boolean(c.dataset.harness)),
      opencodeMeta,
      claudeSpeaksHint: flat(claudeSurfaces.querySelector(".agent-surfaces-speaks-hint")),
      claudeCcOptions,
      sdkBadge: flat(sdkCard.querySelector(".agent-state")),
      sdkMeta,
      sdkSpeaksHint: flat(sdkSurfaces.querySelector(".agent-surfaces-speaks-hint")),
      sdkCcOptions,
      xssTitleText: flat(xssCard.querySelector("h4")),
      xssInjectedCount: xssCard.querySelectorAll("img, script, b").length,
      xssGlobalFlag: typeof window.__sdkXss === "undefined" ? "" : String(window.__sdkXss),
    };

    // Save claude_agent_sdk -> commandcode = chat_completions
    sdkCcSelect.value = "chat_completions";
    sdkCcSave.click();
    await wait(200);
    const afterSave = {
      feedbackText: flat(sdkCcFeedback),
      selectValue: sdkCcSelect.value,
      statusText: flat(sdkCcRow.querySelector(".agent-surface-status")),
    };

    // Filter box filters SDK cards alongside launchable cards
    const filterInput = doc.getElementById("codingAgentsFilter");
    filterInput.value = "agent sdk";
    filterInput.dispatchEvent(new window.Event("input", { bubbles: true }));
    const afterFilter = {
      visibleSdkIds: sdkCards.filter((c) => !c.hidden).map((c) => c.dataset.sdkHarness),
      visibleLaunchableIds: launchableCards
        .filter((c) => !c.hidden)
        .map((c) => c.dataset.harness),
    };
    filterInput.value = "";
    filterInput.dispatchEvent(new window.Event("input", { bubbles: true }));

    return {
      initial,
      afterSave,
      afterFilter,
      posts,
      xssSdkName: XSS_SDK_NAME,
      scriptErrors: scriptErrors.slice(),
      consoleErrors: consoleErrors.slice(),
    };
  }

  return { run };
}
