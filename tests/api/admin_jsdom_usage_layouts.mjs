/**
 * The five Usage layouts, one payload (8.7.0, USAGE-LAYOUTS), proved in jsdom
 * (MCC_JSDOM_SCENARIO=usage_layouts).
 *
 * `specs/PR-USAGE-LAYOUTS-SPEC.md` §5 sets the bar, and this scenario measures
 * exactly those claims through the PRODUCT's own registry, model and
 * renderers — never a simulation of them:
 *
 *  1. `layouts` — the registry is FIVE rows, in the declared order, with the
 *     declared ids and a sentence each. `04-priority-column` is asserted absent
 *     from the table AND from every DOM the five renderers draw.
 *  2. `sameData` — ONE fixture payload through all five renderers. For each
 *     layout the scenario records every provider id that reached the DOM and
 *     every figure string that reached it, then the Python test asserts the
 *     five lists are equal and both are non-empty. A layout may hide a figure
 *     behind an expansion or a pane; it may not DROP it. That is why
 *     `one_screen` is measured with every row open and `master_detail` with
 *     every provider selected in turn.
 *  3. `oneAtATime` — `one_screen`: open A, open B, assert A is closed. §8.3 is
 *     binding and this is the test that pins it.
 *  4. `noCap` — a fixture where NO provider sends a percentage or a pair of
 *     halves. The assertion is that no layout renders a bar, a ring, a `%` or a
 *     `0 %` anywhere: `usageFraction` must return null for every window and
 *     every layout must then draw words.
 *  5. `coverage` — the eight states, each with a provider: a custom provider
 *     matching nothing, one matching a models.dev host, one on a recognised
 *     gateway, one with an expired plan, one failed, one not yet read, one
 *     needing a credential MCC does not hold, and one measured. Every one is
 *     present in all five layouts, in its own state, and no `$0.00` or empty
 *     bar appears for a provider that has nothing.
 *  6. `honesty` — the formatters, read from the product: `usageMoney` on the
 *     user's own values (`34.9300790367`, `0.1840009580000002`, `-0.01`),
 *     `usageInstantParts` on an ISO timestamp, `usagePct` on a provider's own
 *     string, and `usageNoteParts` turning "Free tier: false" into a labelled
 *     pair rather than "Note 1".
 *  7. `picker` — the refresh picker: exactly five choices, 30 seconds marked,
 *     a change posting ONE `USAGE_AUTO_REFRESH_SECONDS` value through the same
 *     apply path, and a stored value outside the five added as its own marked
 *     option rather than silently rounded.
 *  8. `refreshNow` — the page-level button posts ONE
 *     `POST /admin/api/usage/read-all`, and its answer adopts the server's own
 *     `providers`/`coverage` lists verbatim.
 *
 * Prints nothing itself: `run()` returns the measurements and the harness
 * prints them as one JSON object.
 */

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/* ─────────────────────────────── the fixture payload ─────────────────────── */

/** The user's own 8.6.0 data, as the page received it. Every figure below is
 *  one their PDF shows — including the ones that exposed the formatting
 *  problem (`0.1840009580000002`, `34.9300790367`, `-0.01`, a raw ISO reset).
 *  Using their numbers rather than tidy ones is the point: the overflow and
 *  contrast measurements afterwards are against the longest REAL values. */
const FIXTURE_NOW = 1_793_600_000;

function provider(overrides) {
  return {
    provider_id: "deepseek",
    display_name: "DeepSeek",
    mechanisms: ["response-header", "provider-endpoint"],
    source: "response-header",
    detail: "the x-ratelimit response headers",
    state: "measured",
    observed_at: FIXTURE_NOW - 90,
    status_code: null,
    values: {},
    readable: true,
    endpoint: {
      host: "https://api.deepseek.com",
      path: "/user/balance",
      provenance: "vendor-doc",
      reads: "the account's balance, per currency",
      credential: "api-key-env",
      credential_env: "DEEPSEEK_API_KEY",
    },
    fetched_at: FIXTURE_NOW - 90,
    reading: null,
    attempted_at: FIXTURE_NOW - 90,
    read_error: "",
    not_sent: false,
    retry_at: null,
    retry_in_seconds: null,
    interval_seconds: 30,
    consecutive_failures: 0,
    next_read_in_seconds: null,
    ...overrides,
  };
}

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

/** THE ONE PAYLOAD. Every figure below is the user's own. */
function fullPayload() {
  const commandcode = provider({
    provider_id: "commandcode",
    display_name: "Command Code",
    endpoint: {
      host: "https://api.commandcode.ai",
      path: "/alpha/billing/credits",
      provenance: "vendor-binary",
      reads: "the credit pool this account has left",
      credential: "api-key-env",
      credential_env: "COMMANDCODE_API_KEY",
    },
    reading: reading({
      plan: "individual-go — a plan name long enough to wrap in every layout",
      windows: [
        {
          id: "fiveHour",
          label: "5-hour window",
          used: "0",
          limit: "14",
          remaining: "",
          unit: "usd",
          resets_at: "0",
        },
        {
          id: "weekly",
          label: "Weekly window",
          used: "34.9300790367",
          limit: "35",
          remaining: "",
          unit: "usd",
          resets_at: "2026-11-03T21:55:00.000Z",
        },
      ],
      balances: [
        { id: "monthlyCredits", label: "Monthly credits left", amount: "0.0697507979", currency: "USD", kind: "available" },
        { id: "purchasedCredits", label: "Purchased credits left", amount: "0", currency: "USD", kind: "purchased" },
        { id: "freeCredits", label: "Free credits left", amount: "0", currency: "USD", kind: "promotional" },
      ],
      notes: ["Subscription status: active", "This period ends: 2026-11-14T00:00:00Z"],
    }),
  });

  const openrouter = provider({
    provider_id: "open_router",
    display_name: "OpenRouter",
    endpoint: {
      host: "https://openrouter.ai",
      path: "/api/v1/key",
      provenance: "vendor-doc",
      reads: "this key's spend and its remaining limit",
      credential: "api-key-env",
      credential_env: "OPENROUTER_API_KEY",
    },
    reading: reading({
      windows: [
        {
          id: "free_model_daily_requests",
          label: "Free model requests today",
          used: "0",
          limit: "1000",
          remaining: "",
          unit: "requests",
          resets_at: "",
        },
        {
          id: "key-limit",
          label: "Key limit",
          used: "",
          limit: "0.2",
          remaining: "0.1840009580000002",
          unit: "usd",
          resets_at: "2026-11-02T21:00:00.000Z",
        },
      ],
      balances: [
        { id: "usage", label: "Total spend on this key", amount: "0.015999042", currency: "USD", kind: "spend" },
        { id: "usage_daily", label: "Spend today", amount: "0", currency: "USD", kind: "spend" },
        { id: "usage_weekly", label: "Spend this week", amount: "0", currency: "USD", kind: "spend" },
        { id: "usage_monthly", label: "Spend this month", amount: "0", currency: "USD", kind: "spend" },
      ],
      notes: ["This key is labelled sk-or-v1-258…a52", "Free tier: false"],
    }),
  });

  const deepseek = provider({
    provider_id: "deepseek",
    display_name: "DeepSeek",
    state: "unavailable",
    reading: reading({
      available: false,
      unavailable_reason: "the host reports this account is not available for inference",
      balances: [
        { id: "total_balance-USD", label: "Balance (USD)", amount: "-0.01", currency: "USD", kind: "available" },
        { id: "granted_balance-USD", label: "Granted (promotional) (USD)", amount: "0.00", currency: "USD", kind: "promotional" },
        { id: "topped_up_balance-USD", label: "Topped up (purchased) (USD)", amount: "-0.01", currency: "USD", kind: "purchased" },
      ],
    }),
  });

  const opencode = provider({
    provider_id: "opencode_go",
    display_name: "OpenCode Go",
    endpoint: {
      host: "https://opencode.ai",
      path: "/zen/go/v1/usage",
      provenance: "third-party",
      reads: "the rolling, weekly and monthly Go windows",
      credential: "api-key-env",
      credential_env: "OPENCODE_API_KEY",
    },
    reading: reading({
      windows: [
        { id: "rolling", label: "Rolling window", used: "7", limit: "", remaining: "", unit: "percent", resets_at: "2026-11-02T21:08:42.000Z" },
        { id: "weekly", label: "Weekly window", used: "32", limit: "", remaining: "", unit: "percent", resets_at: "2026-11-07T00:00:00.000Z" },
        { id: "monthly", label: "Monthly window", used: "39", limit: "", remaining: "", unit: "percent", resets_at: "2026-11-27T08:18:10.000Z" },
      ],
      notes: ["Rolling window: ok", "Weekly window: ok", "Monthly window: ok"],
    }),
  });

  const kimi = provider({
    provider_id: "kimi",
    display_name: "Kimi (Moonshot)",
    state: "failed",
    read_error: "Invalid Authentication",
    reading: null,
  });

  return {
    observed_at: FIXTURE_NOW,
    stale_after_seconds: 1800,
    minimum_interval_seconds: 1800,
    auto_refresh_seconds: 30,
    read_interval_seconds: 30,
    layout: "one_screen",
    providers: [commandcode, openrouter, deepseek, opencode, kimi],
    coverage: [
      coverage({
        provider_id: "anthropic_oauth",
        display_name: "Claude subscription",
        state: "measured",
        readable: true,
        mechanisms: ["provider-endpoint"],
        reading: reading({
          plan: "max",
          windows: [
            { id: "five_hour", label: "5-hour window", used: "38", limit: "", remaining: "", unit: "percent", resets_at: "2026-11-02T12:00:00Z" },
            { id: "seven_day", label: "Weekly window", used: "11", limit: "", remaining: "", unit: "percent", resets_at: "2026-11-09T00:00:00Z" },
          ],
        }),
      }),
      coverage({
        provider_id: "chatgpt_oauth",
        display_name: "ChatGPT / Codex subscription",
        state: "measured",
        readable: true,
        mechanisms: ["provider-endpoint"],
        reading: reading({
          plan: "plus",
          windows: [
            { id: "primary_window", label: "Primary window", used: "0", limit: "", remaining: "", unit: "percent", resets_at: "2026-11-02T21:12:00Z" },
            { id: "secondary_window", label: "Secondary window", used: "17", limit: "", remaining: "", unit: "percent", resets_at: "2026-11-04T20:16:00Z" },
          ],
          balances: [
            { id: "credits", label: "Credits balance", amount: "0", currency: "USD", kind: "available" },
          ],
          notes: ["Credits unlimited: false", "Has credits: false"],
        }),
      }),
      coverage({
        provider_id: "commandcode",
        display_name: "Command Code",
        reading: commandcode.reading,
        endpoint: commandcode.endpoint,
      }),
      coverage({
        provider_id: "open_router",
        display_name: "OpenRouter",
        reading: openrouter.reading,
        endpoint: openrouter.endpoint,
      }),
      coverage({
        provider_id: "deepseek",
        display_name: "DeepSeek",
        state: "unavailable",
        reading: deepseek.reading,
        endpoint: deepseek.endpoint,
      }),
      coverage({
        provider_id: "opencode_go",
        display_name: "OpenCode Go",
        reading: opencode.reading,
        endpoint: opencode.endpoint,
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
        display_name: "Nous Portal (HyperCharm)",
        state: "not_measured",
        credentials: 2,
        mechanisms: ["response-header"],
        readable: false,
        reading: null,
      }),
      coverage({
        provider_id: "kimi_coding",
        display_name: "Kimi Coding",
        state: "not_measured",
        readable: false,
        mechanisms: ["provider-endpoint"],
        detail: "the route is third-party evidence only; MCC does not read it yet",
        reading: null,
      }),
      coverage({
        provider_id: "custom_91aa",
        display_name: "aihubmix",
        state: "possible_endpoint",
        stage: "discovery",
        readable: false,
        mechanisms: [],
        credentials: 2,
        recognised: "models.dev api host · aihubmix",
        sentence: "its host is the one models.dev names for aihubmix",
        reading: null,
      }),
      coverage({
        provider_id: "custom_77bb",
        display_name: "A local LiteLLM",
        state: "possible_endpoint",
        stage: "discovery",
        readable: false,
        mechanisms: [],
        credentials: 1,
        recognised: "gateway family · litellm",
        sentence: "its address identifies a LiteLLM gateway, which serves /key/info (the key's spend and its remaining budget)",
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
      coverage({
        provider_id: "copilot",
        display_name: "GitHub Copilot",
        state: "needs_credential",
        readable: false,
        mechanisms: [],
        needs_credential: "a Copilot OAuth token MCC does not hold",
        credentials: 1,
        reading: null,
      }),
    ],
  };
}

/** THE NO-CAP PAYLOAD: nothing sent a percentage and nothing sent both halves.
 *
 *  `specs/PR-USAGE-LAYOUTS-SPEC.md` §5.4: "a cap fixture and a no-cap fixture,
 *  asserting the second shows no percentage anywhere in any layout."
 */
function noCapPayload() {
  const onlyUsed = provider({
    provider_id: "commandcode",
    display_name: "Command Code",
    reading: reading({
      windows: [
        // A `used` with NO limit: a number with no ceiling has no fraction.
        { id: "one", label: "Spend so far", used: "12.3456", limit: "", remaining: "", unit: "usd", resets_at: "" },
        { id: "two", label: "Requests made", used: "345", limit: "", remaining: "", unit: "requests", resets_at: "" },
      ],
      balances: [
        { id: "monthlyCredits", label: "Monthly credits left", amount: "3.5", currency: "USD", kind: "available" },
      ],
    }),
  });
  const balanceOnly = provider({
    provider_id: "deepseek",
    display_name: "DeepSeek",
    reading: reading({
      balances: [
        { id: "total_balance-USD", label: "Balance (USD)", amount: "18.42", currency: "USD", kind: "available" },
      ],
    }),
  });
  const unread = provider({
    provider_id: "kimi",
    display_name: "Kimi (Moonshot)",
    state: "not_measured",
    reading: null,
  });
  return {
    observed_at: FIXTURE_NOW,
    stale_after_seconds: 1800,
    minimum_interval_seconds: 1800,
    auto_refresh_seconds: 30,
    read_interval_seconds: 30,
    layout: "one_screen",
    providers: [onlyUsed, balanceOnly, unread],
    coverage: [
      coverage({ provider_id: "commandcode", display_name: "Command Code", reading: onlyUsed.reading, endpoint: onlyUsed.endpoint }),
      coverage({ provider_id: "deepseek", display_name: "DeepSeek", reading: balanceOnly.reading, endpoint: balanceOnly.endpoint }),
      coverage({ provider_id: "kimi", display_name: "Kimi (Moonshot)", state: "not_measured", reading: null, readable: false }),
      // The three states whose ONLY job in this fixture is to prove they draw
      // no percentage either. A no-cap fixture carrying nothing but measured
      // rows would leave the other five states unmeasured against the rule.
      coverage({
        provider_id: "copilot",
        display_name: "GitHub Copilot",
        state: "needs_credential",
        readable: false,
        mechanisms: [],
        needs_credential: "a Copilot OAuth token MCC does not hold",
        credentials: 1,
        reading: null,
      }),
      coverage({
        provider_id: "custom_91aa",
        display_name: "aihubmix",
        state: "possible_endpoint",
        stage: "discovery",
        readable: false,
        mechanisms: [],
        credentials: 2,
        recognised: "models.dev api host · aihubmix",
        sentence: "its host is the one models.dev names for aihubmix",
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
      coverage({
        provider_id: "opencode_go",
        display_name: "OpenCode Go",
        state: "failed",
        read_error: "Invalid Authentication",
        reading: null,
      }),
      coverage({
        provider_id: "nous_portal",
        display_name: "Nous Portal (HyperCharm)",
        state: "not_measured",
        credentials: 2,
        mechanisms: ["response-header"],
        readable: false,
        reading: null,
      }),
      // The two subscriptions are NAMED here as `unsupported` on purpose: it
      // is the case an older server's coverage list produces for them, and the
      // page must still draw their payloads from the Providers routes rather
      // than say "publishes no usage endpoint" about a plan it is reading.
      coverage({
        provider_id: "chatgpt_oauth",
        display_name: "ChatGPT / Codex subscription",
        state: "unsupported",
        readable: false,
        mechanisms: [],
        credentials: 1,
        reading: null,
      }),
      coverage({
        provider_id: "anthropic_oauth",
        display_name: "Claude subscription",
        state: "unsupported",
        readable: false,
        mechanisms: [],
        credentials: 1,
        reading: null,
      }),
    ],
  };
}

export function prepareUsageLayoutsScenario({ window, ROUTES, fetchBodies }) {
  const doc = window.document;
  const flat = (node) =>
    node ? String(node.textContent || "").replace(/\s+/g, " ").trim() : "";
  const allText = (node) => (node ? Array.from(node.querySelectorAll("*")) : []);
  const ids = (node) =>
    node
      ? Array.from(node.querySelectorAll("[data-provider]")).map(
          (entry) => entry.dataset.provider,
        )
      : [];

  /** A subtree's text with every "exactly as sent" disclosure removed.
   *
   *  The disclosure is where 8.7.0 keeps the provider's own strings verbatim,
   *  on purpose: shortening a figure for the human form and keeping the exact
   *  one behind a summary is the compaction the user asked for. So a claim
   *  about "a reader never sees the raw value" is a claim about the human form,
   *  and that is what this returns.
   */
  const humanForm = (node) => {
    if (!node) return "";
    const clone = node.cloneNode(true);
    clone.querySelectorAll(".usage-figures-disclosure").forEach((el) => el.remove());
    return flat(clone);
  };

  /** Every money-looking string in a subtree, as rendered. */
  function moneyStrings(node) {
    return allText(node)
      .map((entry) => flat(entry))
      .filter((text) => /^[\u2212-]?\$/.test(text));
  }

  function run(text) {
    return text;
  }

  async function runScenario({ scriptErrors, consoleErrors }) {
    const out = { scenarioFatal: null, scriptErrors, consoleErrors };
    if (typeof window.usageHarness !== "function") {
      out.scenarioFatal = "window.usageHarness is absent: the page never drew";
      return out;
    }
    const PAGE = window.usageHarness();
    const sys = window.MCCUsageLayouts;
    /* The registry and the five renderers live in `usage_layouts.js` (a classic
       script), and `admin.js`'s harness handle already republishes them — so
       this scenario reads them from the PRODUCT's handle rather than from a
       second global a test-only shim would have to create. */
    const L = {
      USAGE_LAYOUTS: PAGE.USAGE_LAYOUTS,
      usageLayoutFor: PAGE.usageLayoutFor,
      usageLayoutIds: PAGE.usageLayoutIds,
      usageMoney: PAGE.usageMoney,
      usagePct: PAGE.usagePct,
      usageInstantParts: PAGE.usageInstantParts,
      usageFraction: PAGE.usageFraction,
      usageWindowFigure: PAGE.usageWindowFigure,
      usageNoteParts: PAGE.usageNoteParts,
    };
    const started = Date.now();
    while (Date.now() - started < 20000 && PAGE.state.config === null) {
      await wait(50);
    }
    await wait(200);
    PAGE.state.config = PAGE.state.config || {};

    // Every provider the fixture's coverage names is configured on "this
    // install", so the model's own coverage path is what lists them.
    PAGE.state.config.provider_status = [
      { provider_id: "anthropic_oauth", display_name: "Claude subscription", kind: "remote", status: "configured", key_count: 1 },
      { provider_id: "chatgpt_oauth", display_name: "ChatGPT / Codex subscription", kind: "remote", status: "configured", key_count: 1 },
      { provider_id: "commandcode", display_name: "Command Code", kind: "remote", status: "configured", key_count: 1 },
      { provider_id: "open_router", display_name: "OpenRouter", kind: "remote", status: "configured", key_count: 1 },
      { provider_id: "deepseek", display_name: "DeepSeek", kind: "remote", status: "configured", key_count: 1 },
      { provider_id: "opencode_go", display_name: "OpenCode Go", kind: "remote", status: "configured", key_count: 1 },
      { provider_id: "kimi", display_name: "Kimi (Moonshot)", kind: "remote", status: "configured", key_count: 1 },
      { provider_id: "nous_portal", display_name: "Nous Portal (HyperCharm)", kind: "remote", status: "configured", key_count: 2 },
      { provider_id: "kimi_coding", display_name: "Kimi Coding", kind: "remote", status: "configured", key_count: 1 },
      { provider_id: "custom_91aa", display_name: "aihubmix", kind: "remote", status: "configured", key_count: 2, custom: true },
      { provider_id: "custom_77bb", display_name: "A local LiteLLM", kind: "remote", status: "configured", key_count: 1, custom: true },
      { provider_id: "custom_55cc", display_name: "Somebody's own gateway", kind: "remote", status: "configured", key_count: 3, custom: true },
      { provider_id: "copilot", display_name: "GitHub Copilot", kind: "remote", status: "configured", key_count: 1 },
    ];

    const payload = fullPayload();
    ROUTES["/admin/api/usage"] = payload;
    // The real `/admin/api/config` carries every manifest field, so
    // `state.fields` holds `USAGE_LAYOUT` on a real page. The harness's
    // fixture has no fields at all, so this scenario installs the one the
    // page reads — the same door `load()` uses.
    PAGE.state.fields = PAGE.state.fields || new Map();
    PAGE.state.fields.set("USAGE_LAYOUT", { key: "USAGE_LAYOUT", value: "one_screen" });

    // The two subscriptions' payloads arrive from the Providers page's own two
    // routes. `usage_page`'s scenarios write them into `usageSources`; this one
    // does the same through the product's own door, so the subscription rows
    // carry a plan and an account line rather than only the route's reading.
    PAGE.rememberUsagePayload("anthropic_oauth", {
      plan: "max",
      accounts: [
        { account_id: "acct_claude", name: "eduard…@gmail.com", subscription_type: "max", windows: {} },
      ],
      windows: {},
    });
    PAGE.rememberUsagePayload("chatgpt_oauth", {
      plan_type: "plus",
      accounts: [
        { account_id: "acct_chatgpt", name: "ady…@gmail.com", plan_type: "plus" },
      ],
      windows: { observed: false },
    });
    PAGE.rememberUsageCaptured(payload);
    window.setActiveView("usage");
    await wait(120);

    // ── 1. the registry ─────────────────────────────────────────────────
    out.layouts = {
      ids: L.usageLayoutIds(),
      names: L.USAGE_LAYOUTS.map((row) => row.name),
      notes: L.USAGE_LAYOUTS.map((row) => row.note),
      fallback: L.usageLayoutFor("not_a_layout").id,
      forOneScreen: L.usageLayoutFor("one_screen").id,
      hasRenderer: L.USAGE_LAYOUTS.every((row) => typeof row.render === "function"),
      // The excluded preview must not be reachable by any id, including a
      // near-miss.
      priority: ["04", "priority", "priority_column", "priority-column"].map(
        (probe) => L.usageLayoutFor(probe).id,
      ),
    };

    // ── 2/3/5. one payload through all five ─────────────────────────────
    out.sameData = {};
    out.states = {};
    const model = PAGE.usageLayoutModel();
    out.model = {
      rowCount: model.rows.length,
      ids: model.rows.map((row) => String(row.id)),
      states: Object.fromEntries(
        model.rows.map((row) => [String(row.id), String(row.state)]),
      ),
      configured: model.configured,
      measured: model.measured,
      // Every figure a layout could draw, in the words the model holds them:
      // the proof that the five renderers read ONE model rather than five.
      windows: model.rows.map((row) => ({
        id: String(row.id),
        windows: row.windows.map((win) => ({
          label: win.label,
          figure: PAGE.usageWindowFigure(win),
          fraction: PAGE.usageFraction(win),
        })),
        balances: row.balances.map((balance) => ({
          label: balance.label,
          amount: balance.amount,
          currency: balance.currency,
        })),
      })),
    };
    ["one_screen", "ledger_rows", "meter_board", "dial_cards", "master_detail"].forEach(
      (layoutId) => {
        const root = doc.createElement("div");
        doc.body.appendChild(root);
        L.USAGE_LAYOUTS.find((row) => row.id === layoutId).render(root, model);

        // one_screen hides every row's detail until it is opened, and
        // master_detail renders one inspector at a time. Both are measured
        // with EVERYTHING open, which is what "may hide detail behind an
        // expansion, may not drop a provider" means in a measurable form.
        if (layoutId === "one_screen") {
          Array.from(root.querySelectorAll(".usage-srow-button")).forEach((button) =>
            button.click(),
          );
        }
        if (layoutId === "master_detail") {
          const inspectors = [];
          Array.from(root.querySelectorAll(".usage-master-row")).forEach((button) => {
            button.click();
            const inspector = root.querySelector(".usage-inspector");
            inspectors.push(flat(inspector));
          });
          out.sameData[`${layoutId}__inspectors`] = inspectors;
        }

        // The measurement is of the layout's FULL surface for that provider:
        // `master_detail` draws one inspector at a time, so its comparison is
        // the master list plus every inspector snapshot joined. That is the
        // layout's whole page — the master is the list and the inspector is the
        // detail — and reading only the current inspector would compare a
        // third of a layout against three whole ones.
        const body =
          layoutId === "master_detail"
            ? flat(root) +
              " " +
              (out.sameData[`${layoutId}__inspectors`] || []).join(" ")
            : flat(root);

        // ONE debug window, so a figure the comparison says is missing can be
        // read rather than guessed at. It ships with the measurement because
        // "every layout carries this number" is worth nothing if the failure
        // does not say what the layout DID draw.
        out.sameData[`${layoutId}__text`] = body.slice(0, 2400);

        out.sameData[layoutId] = {
          providerIds: Array.from(new Set(ids(root))).sort(),
          // Every figure the layout draws, as text. Forms differ by layout
          // (`$34.93 of $35.00` and `99.8%` are the same fact said twice), so
          // the comparison the Python test makes is on the NUMBERS the fixture
          // contains, which every layout must carry.
          figures: {
            // THE RENDERED STRINGS, not the raw provider ones. `0.184` is
            // never printed — `$0.18 left of $0.20` is, which is the whole
            // point of the compaction — so a marker list of raw values would
            // fail on a page that is right.
            "34.93": /34\.93/.test(body),
            "99.8": /99\.8/.test(body),
            "0.0698": /0\.0698/.test(body),
            "0.18 left": /0\.18 left of/.test(body),
            "negative": /\u2212\$0\.01/.test(body),
            "7 pct": /(?<![\d.])7%/.test(body),
            "32 pct": /(?<![\d.])32%/.test(body),
            "39 pct": /(?<![\d.])39%/.test(body),
            "17 pct": /(?<![\d.])17%/.test(body),
            "38 pct": /(?<![\d.])38%/.test(body),
            "invalid auth": /Invalid Authentication/i.test(body),
            "not published": /publishes no usage endpoint/i.test(body),
            "possible": /hypothesis|possible endpoint|unverified/i.test(body),
            "credential": /Copilot OAuth token/i.test(body),
            "expired/unavailable": /nothing available/i.test(body),
            "free tier note": /Free tier/.test(body),
          },
          // Every provider id the fixture's coverage names must be present.
          textLength: body.length,
          // The percentage/bar count, so the no-cap run can assert zero.
          bars: root.querySelectorAll(".usage-meter").length,
          rings: root.querySelectorAll(".usage-ring").length,
          percentText: (body.match(/\d+(?:\.\d+)?%/g) || []).length,
          // The counts live BESIDE `figures`: a boolean map answers "is this
          // fact here", and mixing counts into it makes a comparison compare a
          // number rather than a presence.
          moneyZero: moneyStrings(root).filter((text) => text === "$0.00").length,
          // The raw values the user's PDF showed. Measured on the HUMAN form
          // only — every element outside the "every figure, exactly as the
          // provider sent it" disclosure, which exists precisely to hold the
          // provider's own strings (§UI-QUALITY A.3's "nothing is lost"). A
          // `title` is not "printed to a reader" either.
          rawIso: /20\d\d-\d\d-\d\dT\d\d:\d\d/.test(flat(root)),
          rawLongMoney: /0\.1840009580000002|34\.9300790367|0\.0697507979/.test(
            humanForm(root),
          ),
        };
        root.remove();
      },
    );

    // ── 3. one row at a time ────────────────────────────────────────────
    const stackHost = doc.createElement("div");
    doc.body.appendChild(stackHost);
    sys.oneScreen(stackHost, model);
    const buttons = Array.from(stackHost.querySelectorAll(".usage-srow-button"));
    const beforeOpen = buttons.map((b) => b.getAttribute("aria-expanded"));
    buttons[0].click();
    const afterA = {
      a: buttons[0].getAttribute("aria-expanded"),
      b: buttons[1].getAttribute("aria-expanded"),
      detailsHidden: Array.from(
        stackHost.querySelectorAll(".usage-srow-detail"),
      ).map((node) => node.hidden),
    };
    buttons[1].click();
    const afterB = {
      a: buttons[0].getAttribute("aria-expanded"),
      b: buttons[1].getAttribute("aria-expanded"),
      detailsHidden: Array.from(
        stackHost.querySelectorAll(".usage-srow-detail"),
      ).map((node) => node.hidden),
      aClosed: stackHost.querySelector(
        `.usage-srow[data-provider="${buttons[0].closest(".usage-srow").dataset.provider}"]`,
      ).dataset.open,
    };
    buttons[1].click();
    const afterClose = buttons[1].getAttribute("aria-expanded");
    out.oneAtATime = { beforeOpen, afterA, afterB, afterClose, count: buttons.length };
    stackHost.remove();

    // ── 4. the no-cap fixture: no percentage, no bar, no ring ───────────
    const noCap = noCapPayload();
    // The route itself, not only the cache: the page re-reads
    // `GET /admin/api/usage` on a poll tick, and a fixture that only replaced
    // the cached copy was overwritten by the next read.
    ROUTES["/admin/api/usage"] = noCap;
    PAGE.rememberUsageCaptured(noCap);
    await wait(60);
    const noCapModel = PAGE.usageLayoutModel();
    out.noCap = {
      model: {
        ids: noCapModel.rows.map((row) => String(row.id)),
        states: Object.fromEntries(
          noCapModel.rows.map((row) => [String(row.id), String(row.state)]),
        ),
      },
      layouts: {},
    };
    ["one_screen", "ledger_rows", "meter_board", "dial_cards", "master_detail"].forEach(
      (layoutId) => {
        const root = doc.createElement("div");
        doc.body.appendChild(root);
        L.USAGE_LAYOUTS.find((row) => row.id === layoutId).render(root, noCapModel);
        if (layoutId === "one_screen") {
          Array.from(root.querySelectorAll(".usage-srow-button")).forEach((b) => b.click());
        }
        if (layoutId === "master_detail") {
          const text = [];
          Array.from(root.querySelectorAll(".usage-master-row")).forEach((b) => {
            b.click();
            text.push(flat(root.querySelector(".usage-inspector")));
          });
          out.noCap.layouts[`${layoutId}__inspectors`] = text;
        }
        // EVERY element whose own text is a percentage, wherever it sits — the
        // rule is about what a reader sees, so a heading that happened to say
        // "Provider" is irrelevant and a `0%` in a badge is not. `bars` counts
        // the FILLED meters only: the hatched unmeasured track is not a bar and
        // is what the page draws instead of one.
        // ONE MORE EXPANSION, for the layout that has none: a no-cap run that
        // only inspected the collapsed cards would report "no percentage"
        // without ever having read the money rows, which is the vacuous-fixture
        // defect the manual names (a guard over a payload that never exercises
        // it).
        const dialMoney = Array.from(root.querySelectorAll(".usage-dial-money"))
          .map(flat)
          .join(" ");
        const dialText = flat(root) + " " + dialMoney;
        const percentNodes = Array.from(root.querySelectorAll("*")).filter((node) =>
          /^(?:[−-])?\d+(?:\.\d+)?%$/.test(flat(node)),
        );
        out.noCap.layouts[layoutId] = {
          providerIds: Array.from(new Set(ids(root))).sort(),
          filledMeters: root.querySelectorAll(
            ".usage-meter:not(.usage-meter-unmeasured)",
          ).length,
          totalMeters: root.querySelectorAll(".usage-meter").length,
          rings: root.querySelectorAll(".usage-ring-arc").length,
          percentText: (flat(root).match(/\b\d+(?:\.\d+)?%/g) || []).length,
          percentNodes: percentNodes.map((node) => flat(node)),
          // The figures that DID arrive, so "no percentage" is not achieved by
          // drawing nothing at all.
          hasUsedOnly: /12\.3456|\$12\.35/.test(dialText),
          hasCount: /345/.test(dialText),
          hasBalance: /18\.42/.test(dialText),
          providerCount: new Set(ids(root)).size,
          text: dialText.slice(0, 1200),
        };
        root.remove();
      },
    );

    // ── 6. the formatters, read from the product ───────────────────────
    out.honesty = {
      money: {
        longDecimal: L.usageMoney("34.9300790367", "USD"),
        tiny: L.usageMoney("0.0697507979", "USD"),
        negative: L.usageMoney("-0.01", "USD"),
        zero: L.usageMoney("0", "USD"),
        cny: L.usageMoney("131.06", "CNY"),
        empty: L.usageMoney("", "USD"),
      },
      pct: {
        sent: L.usagePct("7"),
        decimal: L.usagePct("38.5"),
        empty: L.usagePct(""),
        alreadySigned: L.usagePct("42%"),
      },
      instant: {
        iso: L.usageInstantParts("2026-11-02T21:08:42.000Z"),
        epoch: L.usageInstantParts("1793600000"),
        words: L.usageInstantParts("in 2 hours"),
        zero: L.usageInstantParts("0"),
        empty: L.usageInstantParts(""),
      },
      fraction: {
        percentOnly: L.usageFraction({ unit: "percent", used: "38", limit: "" }),
        usedAndLimit: L.usageFraction({ unit: "usd", used: "34.9300790367", limit: "35" }),
        remainingAndLimit: L.usageFraction({ unit: "usd", used: "", limit: "0.2", remaining: "0.1840009580000002" }),
        usedOnly: L.usageFraction({ unit: "usd", used: "12.3456", limit: "" }),
        requests: L.usageFraction({ unit: "requests", used: "0", limit: "1000" }),
        nothing: L.usageFraction({ unit: "usd", used: "", limit: "" }),
      },
      figures: {
        weekly: L.usageWindowFigure({ unit: "usd", used: "34.9300790367", limit: "35" }),
        keyLimit: L.usageWindowFigure({ unit: "usd", used: "", limit: "0.2", remaining: "0.1840009580000002" }),
        freeRequests: L.usageWindowFigure({ unit: "requests", used: "0", limit: "1000" }),
        percent: L.usageWindowFigure({ unit: "percent", used: "7" }),
        usedOnly: L.usageWindowFigure({ unit: "usd", used: "12.3456", limit: "" }),
      },
      notes: {
        labelled: L.usageNoteParts("Free tier: false"),
        window: L.usageNoteParts("Rolling window: ok"),
        plain: L.usageNoteParts("Subscription status: active."),
      },
    };

    // ── 7. the picker ──────────────────────────────────────────────────
    doc.querySelector('.nav-link[data-view="usage"]');
    PAGE.rememberUsageCaptured(payload);
    PAGE.paintUsageView();
    await wait(80);
    const select = doc.getElementById("usageRefreshSelect");
    out.picker = {
      found: Boolean(select),
      values: select
        ? Array.from(select.options).map((option) => ({ value: option.value, label: option.textContent }))
        : [],
      selected: select ? select.value : null,
      label: select ? select.getAttribute("aria-label") : null,
      choicesConstant: PAGE.USAGE_REFRESH_CHOICES.map((choice) => choice.seconds),
    };
    // A stored value outside the five is added, not rounded.
    PAGE.rememberUsageCaptured({ ...payload, auto_refresh_seconds: 45 });
    PAGE.paintUsageView();
    await wait(60);
    const stored45 = doc.getElementById("usageRefreshSelect");
    out.picker.stored45 = stored45
      ? {
          values: Array.from(stored45.options).map((option) => option.value),
          selected: stored45.value,
        }
      : null;
    // Changing it posts one value through the apply path.
    PAGE.rememberUsageCaptured(payload);
    PAGE.paintUsageView();
    await wait(60);
    fetchBodies.length = 0;
    const live = doc.getElementById("usageRefreshSelect");
    if (live) {
      live.value = "300";
      live.dispatchEvent(new window.Event("change", { bubbles: true }));
      await wait(120);
    }
    out.picker.apply = fetchBodies
      .filter((entry) => entry.path === "/admin/api/config/apply")
      .map((entry) => entry.body);

    // ── 8. Refresh now ─────────────────────────────────────────────────
    ROUTES["/admin/api/usage/read-all"] = {
      attempted: [
        { provider_id: "commandcode", requested: true, refusal: "", state: "measured" },
        { provider_id: "open_router", requested: false, refusal: "interval", message: "This provider was read less than 30 seconds ago; nothing was sent." },
      ],
      layout: "one_screen",
      providers: payload.providers,
      coverage: payload.coverage,
    };
    fetchBodies.length = 0;
    PAGE.rememberUsageCaptured(payload);
    PAGE.paintUsageView();
    await wait(60);
    const refreshButton = doc.getElementById("usageRefreshNowButton");
    if (refreshButton) {
      refreshButton.click();
      await wait(400);
    }
    out.refreshNow = {
      found: Boolean(refreshButton),
      posts: fetchBodies
        .filter((entry) => entry.path === "/admin/api/usage/read-all")
        .map((entry) => ({ method: entry.method, body: entry.body })),
      message: flat(doc.querySelector(".toast, .message, [role='status']")),
    };

    out.hidden = await (async () => {
      // The third safety behaviour, as a MEASURED claim rather than a comment:
      // with `visibilityState` hidden, one walk must ask nothing at all. jsdom
      // lets the property be redefined, so the product's own branch is what
      // runs.
      Object.defineProperty(window.document, "visibilityState", {
        configurable: true,
        get: () => "hidden",
      });
      const hiddenHeader = PAGE.usageVisibilityHeader();
      ROUTES["/admin/api/usage/read-all"] = {
        attempted: [],
        layout: "one_screen",
        providers: payload.providers,
        coverage: payload.coverage,
      };
      fetchBodies.length = 0;
      PAGE.rememberUsageCaptured(payload);
      await PAGE.refreshAllUsage(null);
      const posts = fetchBodies.filter(
        (entry) => entry.path === "/admin/api/usage/read-all",
      );
      Object.defineProperty(window.document, "visibilityState", {
        configurable: true,
        get: () => "visible",
      });
      return {
        posts: posts.length,
        // The header the product would send while hidden, read from the
        // product's own function: jsdom's fetch recorder keeps request bodies
        // and not headers, so a claim about the header is made where it is
        // decided rather than where it would have been written.
        hiddenHeader,
        visibleHeaderAfter: PAGE.usageVisibilityHeader(),
        message: flat(doc.querySelector(".toast, .message, [role='status']")),
      };
    })();

    // ── 9. the headline arithmetic, as three numbers ────────────────────
    // The three safety behaviours restated as arithmetic, so a reader can
    // check the claim rather than trust it:
    //   per provider at 30 s visible          = 2 requests a minute
    //   with N configured providers           = 2N a minute
    //   a hidden tab                          = 0 (asserted above)
    out.arithmetic = {
      seconds: 30,
      perProviderPerMinute: 60 / 30,
      perProviderPerHour: 3600 / 30,
      configured: model.configured,
      allPerMinute: (60 / 30) * model.configured,
      allPerHour: (3600 / 30) * model.configured,
      hiddenPerMinute: 0,
      visible: (() => {
        Object.defineProperty(window.document, "visibilityState", {
          configurable: true,
          get: () => "visible",
        });
        return window.document.visibilityState;
      })(),
    };

    // ── 10. the page as a reader gets it, for every layout ──────────────
    PAGE.rememberUsageCaptured(payload);
    out.pages = {};
    ["one_screen", "ledger_rows", "meter_board", "dial_cards", "master_detail"].forEach(
      (layoutId) => {
        // BOTH sources, because the FIELD wins: this is the state a real save
        // produces (the field updated) and the state an older server produces
        // (the payload carrying it).
        PAGE.state.fields.set("USAGE_LAYOUT", {
          key: "USAGE_LAYOUT",
          value: layoutId,
        });
        PAGE.rememberUsageCaptured({ ...payload, layout: layoutId });
        PAGE.paintUsageView();
        const root = doc.getElementById("usageLayoutRoot");
        out.pages[layoutId] = {
          layoutAttribute: root ? root.dataset.layout : null,
          layoutName: flat(doc.getElementById("usageLayoutName")),
          count: flat(doc.getElementById("usageCount")),
          providerIds: Array.from(new Set(ids(root))).sort(),
          heightish: root ? root.getBoundingClientRect().height : null,
          nodes: root ? root.querySelectorAll("*").length : 0,
        };
      },
    );

    // ── the setting switches with NO reload ─────────────────────────────
    //
    // The FIELD is the live value: editing it and calling the product's own
    // `paintUsageView()` (which is what `load()` does after a save) draws the
    // other layout in the same page, with no navigation and no second route.
    out.liveSwitch = (() => {
      // Start from the default, because the loop above left the page on the
      // last layout it drew.
      const field = PAGE.state.fields && PAGE.state.fields.get("USAGE_LAYOUT");
      if (!field) return { available: false };
      field.value = "one_screen";
      PAGE.paintUsageView();
      const before = doc.getElementById("usageLayoutRoot").dataset.layout;
      field.value = "meter_board";
      PAGE.paintUsageView();
      const after = doc.getElementById("usageLayoutRoot").dataset.layout;
      const nodes = doc.querySelectorAll("#usageLayoutRoot .usage-tile").length;
      field.value = "one_screen";
      PAGE.paintUsageView();
      return {
        available: true,
        before,
        after,
        restored: doc.getElementById("usageLayoutRoot").dataset.layout,
        tilesInMeterBoard: nodes,
        navigations: 0,
      };
    })();

    out.unknownLayout = (() => {
      PAGE.state.fields.set("USAGE_LAYOUT", {
        key: "USAGE_LAYOUT",
        value: "not_a_layout",
      });
      PAGE.rememberUsageCaptured({ ...payload, layout: "not_a_layout" });
      PAGE.paintUsageView();
      const root = doc.getElementById("usageLayoutRoot");
      return root ? root.dataset.layout : null;
    })();

    return out;
  }

  return { run: runScenario };
}
