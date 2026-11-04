/**
 * Every provider's rendered block, per Usage layout — the byte-identity proof
 * for SPEC §9.1 (`MCC_JSDOM_SCENARIO=usage_non_commandcode`).
 *
 * THE QUESTION THIS ANSWERS: "every other provider keeps its money exactly as
 * it is today." The same scenario file runs twice — once on the BASE tree
 * (`56a8a7ab`, v8.11.0, before this release) and once on the branch — against
 * the same fixture, through the same harness, and the Python test compares
 * provider by provider and layout by layout.
 *
 * WHY IT IS ITS OWN SCENARIO rather than a key inside `usage_layouts`: that
 * scenario's fixture is 8.7.0's, and a base tree cannot run it (the base has no
 * `percent_only_windows` handling and would read the flag as nothing — which
 * is the point, but it also means the *fixture* would differ between the two
 * sides and the comparison would be between two fixtures rather than two
 * trees). This one builds its payload from ONLY the fields the base tree
 * already understands, so both sides draw from byte-identical input.
 *
 * COMMAND CODE IS DELIBERATELY IN THE FIXTURE. Its BALANCE rows are inside
 * "every other provider keeps its money" (§9.1: the balance figures are
 * outside the percentage rule), so they must come out unchanged too. Its
 * subscription WINDOWS are the one thing this release is allowed to move, and
 * they are excluded from the comparison and asserted separately — that
 * exclusion is named in `DECLARED_MOVES` on the Python side rather than being
 * a silent wildcard.
 */

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const FIXTURE_NOW = 1762200000;

function reading(overrides) {
  return {
    fetched_at: FIXTURE_NOW - 90,
    plan: "",
    available: true,
    unavailable_reason: "",
    windows: [],
    balances: [],
    notes: [],
    ...overrides,
  };
}

function endpoint(host, path, env, provenance) {
  return {
    host,
    path,
    provenance,
    reads: "",
    credential: "api-key-env",
    credential_env: env,
  };
}

function coverage(overrides) {
  return {
    provider_id: "deepseek",
    display_name: "DeepSeek",
    credentials: 1,
    state: "measured",
    stage: "read",
    recognised: "",
    sentence: "",
    readable: true,
    mechanisms: ["response-header", "provider-endpoint"],
    detail: "",
    needs_credential: "",
    reading: null,
    fetched_at: FIXTURE_NOW - 90,
    observed_at: FIXTURE_NOW - 90,
    attempted_at: FIXTURE_NOW - 90,
    read_error: "",
    not_sent: false,
    retry_in_seconds: null,
    next_read_in_seconds: null,
    consecutive_failures: 0,
    interval_seconds: 30,
    values: {},
    endpoint: null,
    ...overrides,
  };
}

/** The one payload. Every field below is one the BASE tree already knows.
 *
 *  It is the same provider set 8.7.0's fixture uses *minus* every value this
 *  release could plausibly have disturbed, plus the user's own figures for the
 *  three providers §9.1 names by hand: OpenRouter's `$0.18 left of $0.20`,
 *  DeepSeek's `−$0.01`, Anthropic's `$50.00` extra-usage cap.
 */
function payload() {
  return {
    observed_at: FIXTURE_NOW,
    stale_after_seconds: 1800,
    minimum_interval_seconds: 1800,
    auto_refresh_seconds: 30,
    read_interval_seconds: 30,
    layout: "one_screen",
    providers: [],
    coverage: [
      coverage({
        provider_id: "anthropic_oauth",
        display_name: "Claude subscription",
        credentials: 1,
        mechanisms: ["provider-endpoint"],
        reading: reading({
          plan: "max",
          windows: [
            { id: "five_hour", label: "5-hour window", used: "38", limit: "", remaining: "", unit: "percent", resets_at: "2026-11-02T12:00:00Z" },
            { id: "seven_day", label: "Weekly window", used: "11", limit: "", remaining: "", unit: "percent", resets_at: "2026-11-09T00:00:00Z" },
            // THE USER'S OWN `$50.00` extra-usage cap (§9.1 names it).
            { id: "extra_usage", label: "Extra usage", used: "12.5", limit: "50", remaining: "", unit: "usd", resets_at: "2026-12-01T00:00:00Z" },
          ],
        }),
      }),
      coverage({
        provider_id: "chatgpt_oauth",
        display_name: "ChatGPT / Codex subscription",
        credentials: 1,
        mechanisms: ["provider-endpoint"],
        reading: reading({
          plan: "plus",
          windows: [
            { id: "primary_window", label: "Primary window", used: "0", limit: "", remaining: "", unit: "percent", resets_at: "2026-11-02T21:12:00Z" },
            { id: "secondary_window", label: "Secondary window", used: "17", limit: "", remaining: "", unit: "percent", resets_at: "2026-11-04T20:16:00Z" },
          ],
          balances: [{ id: "credits", label: "Credits balance", amount: "0", currency: "USD", kind: "available" }],
        }),
      }),
      coverage({
        provider_id: "open_router",
        display_name: "OpenRouter",
        endpoint: endpoint("https://openrouter.ai", "/api/v1/key", "OPENROUTER_API_KEY", "vendor-doc"),
        reading: reading({
          windows: [
            { id: "free_model_daily_requests", label: "Free model requests today", used: "0", limit: "1000", remaining: "", unit: "requests", resets_at: "" },
            // §9.1's own example: `$0.18 left of $0.20`.
            { id: "key-limit", label: "Key limit", used: "", limit: "0.2", remaining: "0.1840009580000002", unit: "usd", resets_at: "2026-11-02T21:00:00Z" },
          ],
          balances: [
            { id: "usage", label: "Total spend on this key", amount: "0.015999042", currency: "USD", kind: "spend" },
            { id: "usage_today", label: "Spend today", amount: "0", currency: "USD", kind: "spend" },
          ],
          notes: ["Free tier: false"],
        }),
      }),
      coverage({
        provider_id: "deepseek",
        display_name: "DeepSeek",
        endpoint: endpoint("https://api.deepseek.com", "/user/balance", "DEEPSEEK_API_KEY", "vendor-doc"),
        reading: reading({
          // §9.1's own example: the arrear reads `−$0.01`.
          balances: [
            { id: "total_balance-USD", label: "Balance (USD)", amount: "-0.01", currency: "USD", kind: "available" },
            { id: "granted_balance-USD", label: "Granted (promotional) (USD)", amount: "0.00", currency: "USD", kind: "promotional" },
          ],
        }),
      }),
      coverage({
        provider_id: "opencode_go",
        display_name: "OpenCode Go",
        endpoint: endpoint("https://opencode.ai", "/zen/go/v1/usage", "OPENCODE_API_KEY", "third-party"),
        reading: reading({
          // A percentage the PROVIDER sent: 7 / 32 / 39, shown as sent (§9.1).
          windows: [
            { id: "rolling", label: "Rolling window", used: "7", limit: "", remaining: "", unit: "percent", resets_at: "2026-11-02T21:08:42Z" },
            { id: "weekly", label: "Weekly window", used: "32", limit: "", remaining: "", unit: "percent", resets_at: "2026-11-07T00:00:00Z" },
            { id: "monthly", label: "Monthly window", used: "39", limit: "", remaining: "", unit: "percent", resets_at: "2026-11-27T08:18:10Z" },
          ],
        }),
      }),
      coverage({
        provider_id: "kimi",
        display_name: "Kimi (Moonshot)",
        state: "failed",
        read_error: "Invalid Authentication",
        reading: null,
      }),
      coverage({
        provider_id: "nous_portal",
        display_name: "Nous Portal",
        state: "not_measured",
        mechanisms: ["response-header"],
        readable: false,
        reading: null,
      }),
      coverage({
        provider_id: "custom_55cc",
        display_name: "Somebody's own gateway",
        state: "unsupported",
        readable: false,
        mechanisms: [],
        credentials: 3,
        reading: null,
      }),
      // COMMAND CODE. Its window rows will MOVE (that is this release) and its
      // balance rows must NOT. The comparison below splits the two.
      coverage({
        provider_id: "commandcode",
        display_name: "Command Code",
        endpoint: endpoint("https://api.commandcode.ai", "/alpha/billing/credits", "COMMANDCODE_API_KEY", "vendor-binary"),
        reading: reading({
          plan: "individual-pro",
          windows: [
            { id: "fiveHour", label: "5-hour window", used: "0", limit: "14", remaining: "", unit: "usd", resets_at: "0" },
            { id: "weekly", label: "Weekly window", used: "34.9300790367", limit: "35", remaining: "", unit: "usd", resets_at: "2026-11-03T21:55:00Z" },
          ],
          balances: [
            { id: "monthlyCredits", label: "Monthly credits left", amount: "0.0697507979", currency: "USD", kind: "available" },
            { id: "purchasedCredits", label: "Purchased credits left", amount: "0", currency: "USD", kind: "purchased" },
            { id: "freeCredits", label: "Free credits left", amount: "0", currency: "USD", kind: "promotional" },
          ],
        }),
      }),
    ],
  };
}

export function prepareUsageNonCommandCodeScenario({ window, ROUTES }) {
  const doc = window.document;
  const flat = (node) =>
    node ? String(node.textContent || "").replace(/\s+/g, " ").trim() : "";

  async function run({ scriptErrors, consoleErrors }) {
    const out = { scenarioFatal: null, scriptErrors, consoleErrors };
    if (typeof window.usageHarness !== "function") {
      out.scenarioFatal = "window.usageHarness is absent: the page never drew";
      return out;
    }
    const PAGE = window.usageHarness();
    const started = Date.now();
    while (Date.now() < started + 20000 && PAGE.state.config === null) {
      await wait(50);
    }
    await wait(200);
    PAGE.state.config = PAGE.state.config || {};
    PAGE.state.config.provider_status = [
      { provider_id: "anthropic_oauth", display_name: "Claude subscription", kind: "remote", status: "configured", key_count: 1 },
      { provider_id: "chatgpt_oauth", display_name: "ChatGPT / Codex subscription", kind: "remote", status: "configured", key_count: 1 },
      { provider_id: "open_router", display_name: "OpenRouter", kind: "remote", status: "configured", key_count: 1 },
      { provider_id: "deepseek", display_name: "DeepSeek", kind: "remote", status: "configured", key_count: 1 },
      { provider_id: "opencode_go", display_name: "OpenCode Go", kind: "remote", status: "configured", key_count: 1 },
      { provider_id: "kimi", display_name: "Kimi (Moonshot)", kind: "remote", status: "configured", key_count: 1 },
      { provider_id: "nous_portal", display_name: "Nous Portal", kind: "remote", status: "configured", key_count: 2 },
      { provider_id: "custom_55cc", display_name: "Somebody's own gateway", kind: "remote", status: "configured", key_count: 3, custom: true },
      { provider_id: "commandcode", display_name: "Command Code", kind: "remote", status: "configured", key_count: 1 },
    ];
    PAGE.state.fields = PAGE.state.fields || new Map();
    PAGE.state.fields.set("USAGE_LAYOUT", { key: "USAGE_LAYOUT", value: "one_screen" });

    const fixture = payload();
    ROUTES["/admin/api/usage"] = fixture;
    PAGE.rememberUsageCaptured(fixture);
    window.setActiveView("usage");
    await wait(120);

    const model = PAGE.usageLayoutModel();
    out.model = {
      ids: model.rows.map((row) => String(row.id)).sort(),
      states: Object.fromEntries(model.rows.map((row) => [String(row.id), String(row.state)])),
    };

    out.layouts = {};
    ["one_screen", "ledger_rows", "meter_board", "dial_cards", "master_detail"].forEach(
      (layoutId) => {
        const root = doc.createElement("div");
        doc.body.appendChild(root);
        PAGE.USAGE_LAYOUTS.find((row) => row.id === layoutId).render(root, model);
        if (layoutId === "one_screen") {
          Array.from(root.querySelectorAll(".usage-srow-button")).forEach((b) => b.click());
        }
        if (layoutId === "master_detail") {
          const inspectors = {};
          Array.from(root.querySelectorAll(".usage-master-row")).forEach((button) => {
            button.click();
            inspectors[button.dataset.provider] = flat(root.querySelector(".usage-inspector"));
          });
          out.layouts[`${layoutId}__inspectors`] = inspectors;
        }
        // One entry per provider, the provider's whole rendered block.
        //
        // `master_detail` draws a provider twice — a master button and an
        // inspector — and the INSPECTOR is the block (the master row is a
        // one-line summary). Where there is an inspector, it wins; where there
        // is not, the card does.
        const blocks = {};
        Array.from(root.querySelectorAll("[data-provider]")).forEach((node) => {
          const id = node.dataset.provider;
          if (node.classList.contains("usage-master-row")) return;
          // A card may contain descendants that also carry `data-provider`
          // (never today, but a nested card would silently overwrite the
          // outer one). Keep the FIRST, which is the outermost in document
          // order.
          if (blocks[id]) return;
          blocks[id] = flat(node);
        });
        if (layoutId === "master_detail") {
          Object.entries(out.layouts[`${layoutId}__inspectors`] || {}).forEach(
            ([id, text]) => {
              blocks[id] = text;
            },
          );
        }
        out.layouts[layoutId] = blocks;
        root.remove();
      },
    );
    return out;
  }

  return { run };
}
