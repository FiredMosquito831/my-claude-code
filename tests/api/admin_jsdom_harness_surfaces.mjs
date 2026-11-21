/**
 * Coding-agents "Surfaces it uses" UI scenario (v8.35.0, PR B), run when
 * MCC_JSDOM_SCENARIO=harness_surfaces.
 *
 * Proves:
 *  - Multi-door providers (`opencode_go`, `custom_evil`) render inside each
 *    Coding-agent card's `.agent-surfaces` section (`claude`, `codex`,
 *    `opencode`), while any single-door provider (`groq`) is filtered out (Q11).
 *  - Q8 attribution disclaimer (`x-mcc-harness`, `user-agent`, `Claude Desktop`)
 *    renders on the section.
 *  - Attacker-shaped markup in `display_name` renders strictly as literal text
 *    (`.textContent`), producing zero parsed HTML elements (XSS hygiene).
 *  - Saving a preference POSTs `/admin/api/harness-surfaces`, keeps `"Saved"`
 *    connected in the DOM, and resetting via `"Follow the client (auto)"`
 *    sends `surface: "auto"`.
 */

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const clone = (value) => JSON.parse(JSON.stringify(value));

const XSS_DISPLAY_NAME = "<img src=x onerror=alert(9)>Hostile <b>Custom</b>";

function makePayload(selections = {}) {
  return {
    providers: [
      {
        provider_id: "opencode_go",
        display_name: "OpenCode Go",
        custom: false,
        configured: true,
        discovered_model_count: 4,
        status_summary: "Configured · 4 model(s)",
        offered: [
          { value: "chat_completions", label: "Chat Completions (/chat/completions)" },
          { value: "responses", label: "Responses (/responses)" },
          { value: "messages", label: "Messages (/messages)" },
        ],
      },
      {
        provider_id: "custom_evil",
        display_name: XSS_DISPLAY_NAME,
        custom: true,
        configured: true,
        discovered_model_count: 1,
        status_summary: "Configured · 1 model(s)",
        offered: [
          { value: "chat_completions", label: "Chat Completions (/chat/completions)" },
          { value: "responses", label: "Responses (/responses)" },
        ],
      },
      // Belt-and-suspenders Q11 check: a single-door entry must never render.
      {
        provider_id: "groq",
        display_name: "Groq",
        custom: false,
        configured: true,
        discovered_model_count: 2,
        status_summary: "Configured · 2 model(s)",
        offered: [
          { value: "chat_completions", label: "Chat Completions (/chat/completions)" },
        ],
      },
    ],
    harnesses: {
      claude: { opencode_go: selections.claude || "", custom_evil: "" },
      codex: { opencode_go: selections.codex || "", custom_evil: "" },
      opencode: { opencode_go: selections.opencode || "", custom_evil: "" },
    },
    attribution_note:
      "This is what the client said it was (x-mcc-harness, else its user-agent). " +
      "It is a label, not a login; Claude Desktop shares claude. " +
      "Applies per host; fallback models on other providers ignore it.",
  };
}

export function prepareHarnessSurfacesScenario({ window, ROUTES }) {
  const selections = { claude: "", codex: "", opencode: "" };
  ROUTES["/admin/api/harness-surfaces"] = clone(makePayload(selections));
  ROUTES["/admin/api/requests/latency"] = { enabled: false };
  ROUTES["/admin/api/requests/in-loop"] = { enabled: false, rows: [] };

  const posts = [];
  let harnessSurfacesGets = 0;
  const realFetch = window.fetch;
  window.fetch = (url, options = {}) => {
    const path = String(url).split("?")[0];
    if (path === "/admin/api/harness-surfaces") {
      if (options && typeof options.body === "string") {
        posts.push({ path, body: options.body });
        const parsed = JSON.parse(options.body);
        const next = parsed.surface === "auto" ? "" : (parsed.surface || "");
        selections[parsed.harness] = next;
        ROUTES[path] = clone(makePayload(selections));
      } else {
        harnessSurfacesGets += 1;
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
    if (!(await waitFor(() => doc.querySelector('.nav-link[data-view="coding_agents"]')))) {
      return { scenarioError: "no Coding agents nav link" };
    }
    const getsBeforeClick = harnessSurfacesGets;
    doc.querySelector('.nav-link[data-view="coding_agents"]').click();
    if (
      !(await waitFor(
        () =>
          harnessSurfacesGets > getsBeforeClick &&
          doc.querySelector('.coding-agent-card[data-harness="codex"] .agent-surfaces'),
      ))
    ) {
      return { scenarioError: "no codex .agent-surfaces section rendered" };
    }
    await wait(100);

    const codexSection = doc.querySelector('.coding-agent-card[data-harness="codex"] .agent-surfaces');
    const claudeSection = doc.querySelector('.coding-agent-card[data-harness="claude"] .agent-surfaces');
    codexSection.open = true;

    const customNameNode = codexSection.querySelector(
      '.agent-surface[data-provider="custom_evil"] .agent-surface-provider',
    );
    const goRow = codexSection.querySelector('.agent-surface[data-provider="opencode_go"]');
    const select = goRow.querySelector(".agent-surface-select");
    const save = goRow.querySelector(".agent-surface-save");
    const feedback = goRow.querySelector(".agent-surface-feedback");
    const statusBadge = goRow.querySelector(".agent-surface-status");

    const initial = {
      claudeHasSurfaces: Boolean(claudeSection),
      codexSummary: flat(codexSection.querySelector(".agent-surfaces-summary")),
      attributionNote: flat(codexSection.querySelector(".field-description")),
      renderedProviderIds: Array.from(codexSection.querySelectorAll(".agent-surface")).map(
        (el) => el.dataset.provider,
      ),
      groqRendered: Boolean(codexSection.querySelector('.agent-surface[data-provider="groq"]')),
      customDisplayNameText: customNameNode ? customNameNode.textContent : "",
      injectedElementCount: codexSection.querySelectorAll("img, script, b").length,
      goOptions: Array.from(select.options).map((opt) => ({
        value: opt.value,
        text: opt.textContent,
      })),
    };

    select.value = "responses";
    save.click();
    await wait(200);
    const afterSave = {
      feedbackText: flat(feedback),
      feedbackConnected: feedback.isConnected,
      selectValue: select.value,
      statusText: flat(statusBadge),
    };

    select.value = "";
    save.click();
    await wait(200);
    const afterReset = {
      feedbackText: flat(feedback),
      feedbackConnected: feedback.isConnected,
      selectValue: select.value,
      statusText: flat(statusBadge),
    };

    return {
      initial,
      afterSave,
      afterReset,
      posts,
      xssDisplayName: XSS_DISPLAY_NAME,
      scriptErrors: scriptErrors.slice(),
      consoleErrors: consoleErrors.slice(),
    };
  }

  return { run };
}
