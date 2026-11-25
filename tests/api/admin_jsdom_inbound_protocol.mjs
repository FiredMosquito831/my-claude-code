/**
 * Coding-agents inbound MCC endpoint selector scenario (v8.50.0, PR 13),
 * run when MCC_JSDOM_SCENARIO=inbound_protocol_switch.
 *
 * Proves:
 *  - `.agent-inbound-protocol-select` renders ONLY on launchable harnesses
 *    whose `switchable_protocols.length >= 2` (e.g., `opencode`, `commandcode_cli`)
 *    and NEVER on single-protocol / unavailable harnesses (`claude`, `codex`).
 *  - Selecting `openai_responses` on OpenCode posts
 *    `POST /admin/api/harness-inbound-protocol` with
 *    `{harness: "opencode", protocol: "openai_responses"}`, updates `.agent-meta`
 *    `Protocol` text to `OpenAI Responses (POST /v1/responses)`, and shows `"Saved"`.
 *  - Resetting to `""` (`Default`) restores `Anthropic Messages (POST /v1/messages)`.
 *  - XSS strings in `switchable_protocols[].label` render strictly as text.
 */

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const clone = (value) => JSON.parse(JSON.stringify(value));

const PROTOCOL_LABELS = {
  anthropic_messages: "Anthropic Messages (POST /v1/messages)",
  openai_responses: "OpenAI Responses (POST /v1/responses)",
  openai_chat_completions: "OpenAI Chat Completions (POST /v1/chat/completions)",
};

export function prepareInboundProtocolSwitchScenario({ window, ROUTES }) {
  const baseHarnesses = clone(ROUTES["/admin/api/harnesses"]);
  const overrides = { opencode: "" };

  function buildPayload() {
    const payload = clone(baseHarnesses);
    for (const h of payload.harnesses || []) {
      if (h.id === "opencode") {
        const active = overrides.opencode || "anthropic_messages";
        h.protocol = active;
        h.protocol_label = PROTOCOL_LABELS[active];
        h.inbound_protocol = active;
        h.default_protocol = "anthropic_messages";
        h.inbound_protocol_override = overrides.opencode;
        h.switchable_protocols = [
          {
            value: "anthropic_messages",
            label: PROTOCOL_LABELS.anthropic_messages,
            default: true,
          },
          {
            value: "openai_responses",
            label: PROTOCOL_LABELS.openai_responses,
            default: false,
          },
          {
            value: "openai_chat_completions",
            label: PROTOCOL_LABELS.openai_chat_completions,
            default: false,
          },
        ];
      } else if (h.id === "commandcode_cli") {
        h.inbound_protocol = h.protocol;
        h.default_protocol = h.protocol;
        h.inbound_protocol_override = "";
        h.switchable_protocols = [
          {
            value: "anthropic_messages",
            label: '<img src=x onerror="window.__inboundXss=1">Anthropic Messages (POST /v1/messages)',
            default: true,
          },
          {
            value: "openai_chat_completions",
            label: PROTOCOL_LABELS.openai_chat_completions,
            default: false,
          },
        ];
      } else {
        h.inbound_protocol = h.protocol;
        h.default_protocol = h.protocol;
        h.inbound_protocol_override = "";
        h.switchable_protocols = [];
      }
    }
    return payload;
  }

  ROUTES["/admin/api/harnesses"] = buildPayload();

  const posts = [];
  const realFetch = window.fetch;
  window.fetch = (url, options = {}) => {
    const path = String(url).split("?")[0];
    if (path === "/admin/api/harness-inbound-protocol" && options && typeof options.body === "string") {
      posts.push({ path, body: options.body });
      const parsed = JSON.parse(options.body);
      if (parsed.harness === "opencode") {
        overrides.opencode = parsed.protocol || "";
      }
      const nextPayload = buildPayload();
      ROUTES["/admin/api/harnesses"] = nextPayload;
      return Promise.resolve({
        ok: true,
        status: 200,
        headers: { get: () => "application/json" },
        json: async () => ({ status: "ok", ...nextPayload }),
      });
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
  const metaMap = (card) =>
    Object.fromEntries(
      Array.from(card.querySelectorAll(".agent-meta dt")).map((dt, i) => [
        dt.textContent,
        card.querySelectorAll(".agent-meta dd")[i]?.textContent || "",
      ]),
    );

  async function run({ scriptErrors, consoleErrors }) {
    if (!(await waitFor(() => doc.querySelector('.nav-link[data-view="coding_agents"]')))) {
      return { scenarioError: "no Coding agents nav link" };
    }
    doc.querySelector('.nav-link[data-view="coding_agents"]').click();
    if (
      !(await waitFor(
        () => doc.querySelector('.coding-agent-card[data-harness="opencode"] .agent-inbound-protocol-select'),
      ))
    ) {
      return { scenarioError: "no opencode .agent-inbound-protocol-select rendered" };
    }
    await wait(120);

    const list = doc.getElementById("codingAgentsList");
    const claudeCard = list.querySelector('.coding-agent-card[data-harness="claude"]');
    const codexCard = list.querySelector('.coding-agent-card[data-harness="codex"]');
    const opencodeCard = list.querySelector('.coding-agent-card[data-harness="opencode"]');
    const ccCard = list.querySelector('.coding-agent-card[data-harness="commandcode_cli"]');

    const select = opencodeCard.querySelector(".agent-inbound-protocol-select");
    const feedback = opencodeCard.querySelector(".agent-inbound-protocol-feedback");

    const initial = {
      claudeHasSelector: Boolean(claudeCard && claudeCard.querySelector(".agent-inbound-protocol-select")),
      codexHasSelector: Boolean(codexCard && codexCard.querySelector(".agent-inbound-protocol-select")),
      opencodeAriaLabel: select.getAttribute("aria-label"),
      opencodeOptions: Array.from(select.options).map((o) => ({ value: o.value, text: o.textContent })),
      opencodeValue: select.value,
      opencodeProtocolBefore: metaMap(opencodeCard).Protocol || "",
      xssInjectedCount: ccCard ? ccCard.querySelectorAll("img").length : -1,
      xssGlobalFlag: typeof window.__inboundXss === "undefined" ? "" : String(window.__inboundXss),
    };

    // Switch to openai_responses
    select.value = "openai_responses";
    select.dispatchEvent(new window.Event("change", { bubbles: true }));
    await wait(150);
    const afterSwitch = {
      value: select.value,
      feedback: flat(feedback),
      protocolText: metaMap(opencodeCard).Protocol || "",
    };

    // Reset back to Default ("")
    select.value = "";
    select.dispatchEvent(new window.Event("change", { bubbles: true }));
    await wait(150);
    const afterReset = {
      value: select.value,
      feedback: flat(feedback),
      protocolText: metaMap(opencodeCard).Protocol || "",
    };

    return {
      fatal: null,
      scenarioError: null,
      scriptErrors,
      consoleErrors,
      initial,
      afterSwitch,
      afterReset,
      posts,
    };
  }

  return { run };
}
