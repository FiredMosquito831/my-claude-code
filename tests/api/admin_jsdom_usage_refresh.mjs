/**
 * The Usage page's automatic refresh (8.6.0), proved in jsdom
 * (MCC_JSDOM_SCENARIO=usage_refresh).
 *
 * The user's instruction: "i want the usage per configured provider to be read
 * and updated every 30 seconds (and have the timer configurable in limits and
 * resilience". This scenario measures the three decisions that make a
 * 30-second default safe, driving the PRODUCT's own functions rather than a
 * simulation of them:
 *
 *  1. `cadence` — whether the poller runs at all, and its interval, come from
 *     the SERVER's `auto_refresh_seconds`. 30 -> 30000 ms; 0 -> off; the page
 *     not being the active view -> off.
 *  2. `due` — which providers one pass may ask. Only configured ones, only
 *     ones whose declared read this release can perform, and only ones whose
 *     own `next_read_in_seconds` has elapsed. A provider cooling down after a
 *     429 is skipped while the others are still due.
 *  3. `poll` — one real pass: exactly one `POST /admin/api/usage/read` per due
 *     provider, each carrying exactly that provider's id, plus the one
 *     `GET /admin/api/usage` that refreshes the page's own view.
 *  4. `hidden` — a pass while the tab is hidden asks nothing at all, and one
 *     on becoming visible refreshes once. That is the difference between a
 *     few hundred requests a day and tens of thousands.
 *  5. `off` — with the setting at 0 nothing polls, and the page is exactly
 *     8.5.0's: a Read now button and no timer.
 *
 * Prints nothing itself: `run()` returns the measurements and the harness
 * prints them as one JSON object.
 */

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** One provider's row, as `GET /admin/api/usage` answers it. */
function row(overrides) {
  return {
    provider_id: "deepseek",
    display_name: "DeepSeek",
    mechanisms: ["response-header", "provider-endpoint"],
    source: "response-header",
    detail: "the x-ratelimit response headers",
    state: "measured",
    observed_at: null,
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
    fetched_at: 1793600000,
    reading: null,
    attempted_at: null,
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

export function prepareUsageRefreshScenario({ window, ROUTES, fetchBodies, fetchUrls }) {
  const doc = window.document;
  const flat = (node) =>
    node ? String(node.textContent || "").replace(/\s+/g, " ").trim() : "";

  async function waitFor(probe, timeout = 4000) {
    const started = Date.now();
    while (Date.now() - started < timeout) {
      if (probe()) return true;
      await wait(25);
    }
    return false;
  }

  async function run({ scriptErrors, consoleErrors }) {
    const out = { scenarioFatal: null, scriptErrors, consoleErrors };
    if (typeof window.usageHarness !== "function") {
      out.scenarioFatal = "window.usageHarness is absent: the page never drew";
      return out;
    }
    const PAGE = window.usageHarness();
    const ready = await waitFor(() => PAGE.state.config !== null, 20000);
    if (!ready) {
      out.scenarioFatal = "the config payload never landed";
      return out;
    }
    await wait(200);

    // Four configured providers: two readable and due, one readable but
    // cooling down after a 429, one with no read this release builds. And a
    // fifth that the server knows about but this install has not configured.
    PAGE.state.config = PAGE.state.config || {};
    PAGE.state.config.provider_status = [
      { provider_id: "deepseek", display_name: "DeepSeek", kind: "remote", status: "configured", key_count: 1 },
      { provider_id: "open_router", display_name: "OpenRouter", kind: "remote", status: "configured", key_count: 1 },
      { provider_id: "kimi", display_name: "Kimi (Moonshot)", kind: "remote", status: "configured", key_count: 1 },
      { provider_id: "kimi_coding", display_name: "Kimi Coding", kind: "remote", status: "configured", key_count: 1 },
    ];

    const providers = [
      row({ provider_id: "deepseek", display_name: "DeepSeek", next_read_in_seconds: null }),
      row({ provider_id: "open_router", display_name: "OpenRouter", next_read_in_seconds: null }),
      row({
        provider_id: "kimi",
        display_name: "Kimi (Moonshot)",
        state: "failed",
        read_error: "the provider is rate-limiting usage reads",
        retry_at: 1793600600,
        retry_in_seconds: 600,
        next_read_in_seconds: 600,
      }),
      row({
        provider_id: "kimi_coding",
        display_name: "Kimi Coding",
        readable: false,
        endpoint: null,
        mechanisms: ["provider-endpoint"],
        state: "not_measured",
      }),
      // Not configured on this install: the page must never ask it.
      row({ provider_id: "nous_portal", display_name: "Nous Portal", next_read_in_seconds: null }),
    ];

    const usagePayload = (autoRefreshSeconds) => ({
      observed_at: Math.floor(Date.now() / 1000),
      stale_after_seconds: 1800,
      minimum_interval_seconds: 1800,
      auto_refresh_seconds: autoRefreshSeconds,
      read_interval_seconds: autoRefreshSeconds || 1800,
      providers,
    });

    /** The same envelope over a different provider list, for the sub-runs. */
    const usagePayloadWith = (rows) => ({
      observed_at: Math.floor(Date.now() / 1000),
      stale_after_seconds: 1800,
      minimum_interval_seconds: 1800,
      auto_refresh_seconds: 30,
      read_interval_seconds: 30,
      providers: rows,
    });

    ROUTES["/admin/api/usage"] = usagePayload(30);
    ROUTES["/admin/api/usage/read"] = {
      provider_id: "deepseek",
      requested: true,
      refusal: "",
      message: "",
      attempted_at: Math.floor(Date.now() / 1000),
      next_read_in_seconds: 30,
      retry_in_seconds: null,
      consecutive_failures: 0,
      status_code: 200,
      state: "measured",
      fetched_at: Math.floor(Date.now() / 1000),
      reading: null,
      read_error: "",
      not_sent: false,
    };

    // Open the page the way a reader does, so the payload and the poller's
    // arming both happen through the product's own path.
    const link = doc.querySelector('.nav-link[data-view="usage"]');
    if (link) link.click();
    window.setActiveView("usage");
    PAGE.rememberUsageCaptured(ROUTES["/admin/api/usage"]);
    PAGE.paintUsageView();
    await wait(80);

    // ── 1. the cadence, from the server's number ─────────────────────────
    out.cadence = {
      enabledAt30: PAGE.usageAutoRefreshEnabled(),
      msAt30: PAGE.usageAutoRefreshMs(),
      activeView: PAGE.state.activeView,
    };

    // ── 2. who one pass may ask ──────────────────────────────────────────
    out.due = {
      dueNow: PAGE.usageDueProviders(),
      // The cooling-down provider, the unreadable one and the unconfigured one
      // are all absent above; this states each exclusion in one place.
      excluded: {
        coolingDown: !PAGE.usageDueProviders().includes("kimi"),
        noReadableRoute: !PAGE.usageDueProviders().includes("kimi_coding"),
        notConfigured: !PAGE.usageDueProviders().includes("nous_portal"),
      },
    };

    // ── 3. one real pass: one POST per due provider ──────────────────────
    const bodiesBefore = fetchBodies.length;
    const urlsBefore = fetchUrls.length;
    const outcome = await PAGE.pollUsageReads();
    await wait(80);
    out.poll = {
      clean: outcome.clean,
      posts: fetchBodies
        .slice(bodiesBefore)
        .filter((entry) => entry.path === "/admin/api/usage/read")
        .map((entry) => entry.body && entry.body.provider_id),
      gets: fetchUrls
        .slice(urlsBefore)
        .filter((url) => String(url).startsWith("/admin/api/usage"))
        .map((url) => String(url).split("?")[0]),
    };

    // ── 4. a failing provider does not slow the others ───────────────────
    // The user's requirement is explicit: back off PER PROVIDER, not globally.
    // The per-provider half is the server's (`next_read_in_seconds`); the risk
    // this measures is the page's own poller doubling every wait because one
    // provider failed. A pass with a provider at 3 consecutive failures must
    // still ask the healthy one.
    // The route answers the same rows, so the pass's own `GET` refresh cannot
    // swap the payload out from under the measurement: what is being measured
    // is the poller's decision, not a race with its own resync.
    const healthPayload = usagePayloadWith([
      row({
        provider_id: "deepseek",
        state: "failed",
        read_error: "the host answered HTTP 500",
        consecutive_failures: 3,
        next_read_in_seconds: 240,
      }),
      row({ provider_id: "open_router", next_read_in_seconds: null }),
    ]);
    ROUTES["/admin/api/usage"] = healthPayload;
    PAGE.rememberUsageCaptured(healthPayload);
    PAGE.paintUsageView();
    await wait(60);
    const dueBefore = PAGE.usageDueProviders();
    const failingBodies = fetchBodies.length;
    const failingOutcome = await PAGE.pollUsageReads();
    await wait(80);
    out.perProviderBackoff = {
      dueBefore,
      asked: fetchBodies
        .slice(failingBodies)
        .filter((entry) => entry.path === "/admin/api/usage/read")
        .map((entry) => entry.body && entry.body.provider_id),
      clean: failingOutcome.clean,
    };
    // Back to the four-provider world for the sections that follow.
    ROUTES["/admin/api/usage"] = usagePayload(30);

    // ── 5. a hidden tab asks nothing ─────────────────────────────────────
    // jsdom's visibilityState is a getter on `document`; overriding it is how
    // the real condition is reproduced rather than simulated by skipping the
    // call.
    const descriptor = Object.getOwnPropertyDescriptor(doc, "visibilityState");
    Object.defineProperty(doc, "visibilityState", {
      configurable: true,
      get: () => "hidden",
    });
    const hiddenBodies = fetchBodies.length;
    const hiddenOutcome = await PAGE.pollUsageReads();
    await wait(60);
    out.hidden = {
      clean: hiddenOutcome.clean,
      asked: fetchBodies
        .slice(hiddenBodies)
        .filter((entry) => entry.path === "/admin/api/usage/read").length,
    };
    if (descriptor) Object.defineProperty(doc, "visibilityState", descriptor);
    else delete doc.visibilityState;

    // ── 5. with the setting at 0 nothing polls ───────────────────────────
    PAGE.rememberUsageCaptured(usagePayload(0));
    PAGE.paintUsageView();
    await wait(60);
    out.off = {
      enabled: PAGE.usageAutoRefreshEnabled(),
      ms: PAGE.usageAutoRefreshMs(),
      due: PAGE.usageDueProviders(),
      // The page is 8.5.0's: the control is still there, and it is the only
      // way to ask.
      readNowButtons: doc.querySelectorAll("#usageCapturedGrid .usage-read-button").length,
      buttonText: (() => {
        const button = doc.querySelector("#usageCapturedGrid .usage-read-button");
        return button ? flat(button) : null;
      })(),
    };

    return out;
  }

  return { run };
}
