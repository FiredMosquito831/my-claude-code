/**
 * The Usage page (8.3.0, USAGE PR 1), proved in jsdom
 * (MCC_JSDOM_SCENARIO=usage_page).
 *
 * The release makes one promise that is easy to state and easy to break:
 * **opening this page adds no request.** Everything else here measures the
 * four honest states around it.
 *
 *  1. `position` — the view is FIRST in the OBSERVE group: its nav button sits
 *     immediately before Analytics, and it claims no settings section, so
 *     renderSections() skipping it is what keeps every other page intact.
 *  2. `requests` — opening the view, painting it, and re-rendering it with a
 *     payload already in memory each add ZERO URLs to the fetch recorder. The
 *     two admin paths it draws from are named so a reader can check that no
 *     third one is involved.
 *  3. `cards` — the two subscriptions as a payload four ways: measured now,
 *     measured at T (stale, with its timestamp), nothing observed yet, and a
 *     read that failed with a reason. The exact sentences are captured so the
 *     Python test asserts them rather than a regex of them.
 *  4. `providers` — who appears and who does not. A provider with a key is
 *     listed; one the server does not call configured is not; the two
 *     subscription ids are cards rather than "no endpoint" sentences; `openai`
 *     (the same subscription behind another name) is not a second row.
 *  5. `blocks` — a subscription allowance and a cash balance are two elements
 *     with two headings, and NO element on the page prints a combined total.
 *  6. `longest` — the page rendered with the longest realistic values a
 *     provider can send (§UI-QUALITY-RULE A.3): a two-decimal percentage, a
 *     reset timestamp, `$4,050.2926` and a 60-character account id. jsdom has
 *     no layout engine, so this half proves the strings survive the renderer;
 *     the measured overflow table comes from the real browser run and is in
 *     the release's report.
 *
 * Prints nothing itself: `run()` returns the measurements and the harness
 * prints them as one JSON object.
 */

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const NOT_MEASURED = "not measured by this provider";

/** The four payloads the two cards are measured with, written out in full so
 *  the shapes are visible in the file that asserts on them. */
function usagePayloads(nowSeconds) {
  const longAccountId = "acc_0123456789abcdef0123456789abcdef0123456789abcdef01";
  return {
    // State 1: the provider answered inside the freshness bound.
    measured: {
      anthropic: {
        claude_code: { available: false },
        mcc: { available: true },
        accounts: [
          {
            account_id: "acc-measured",
            name: "work",
            plan: "max",
            subscription_type: "max",
            mode: "shared",
            windows: {
              observed: true,
              observed_at: nowSeconds - 90,
              status: "within-limits",
              reset: "1793600000",
              five_hour_utilization: "38.50",
              five_hour_reset: "1793600000",
              weekly_utilization: "11.00",
              weekly_reset: "1794200000",
              overage_status: "disabled",
              overage_reset: "1794200000",
              usage_limit: "",
            },
          },
        ],
        windows: {
          observed: true,
          observed_at: nowSeconds - 90,
          five_hour_utilization: "38.50",
        },
      },
      chatgpt: {
        plan_type: "",
        windows: {
          observed: true,
          observed_at: nowSeconds - 120,
          primary_used_percent: "12.00",
          primary_reset_at: "1793600000",
          primary_window_minutes: "300",
          secondary_used_percent: "3.00",
          secondary_reset_at: "1794200000",
          secondary_window_minutes: "10080",
          credits_balance: "4.18",
          credits_unlimited: "false",
          limit_name: "not yet observed",
          rate_limit_reached_type: "not yet observed",
        },
        accounts: [],
      },
    },
    // State 2: the provider answered at T and nothing has been read since.
    stale: {
      anthropic: {
        claude_code: { available: false },
        mcc: { available: true },
        accounts: [
          {
            account_id: "acc-stale",
            name: "personal",
            windows: {
              observed: true,
              observed_at: nowSeconds - 4 * 3600,
              status: "within-limits",
              five_hour_utilization: "74.00",
              five_hour_reset: "1793600000",
              weekly_utilization: "41.25",
              weekly_reset: "1794200000",
              overage_status: "not yet observed",
              overage_reset: "not yet observed",
              usage_limit: "not yet observed",
              reset: "not yet observed",
            },
          },
        ],
        windows: { observed: false },
      },
      chatgpt: {
        plan_type: "",
        windows: {
          observed: true,
          observed_at: nowSeconds - 3 * 3600,
          primary_used_percent: "41.00",
          primary_reset_at: "1793600000",
          secondary_used_percent: "9.00",
          secondary_reset_at: "1794200000",
          credits_balance: "4.18",
          credits_unlimited: "false",
          limit_name: "not yet observed",
          rate_limit_reached_type: "not yet observed",
        },
        accounts: [],
      },
    },
    // State 3's reading case: nothing has ever passed through the observer.
    unobserved: {
      anthropic: {
        claude_code: { available: false },
        mcc: { available: true },
        accounts: [],
        windows: { observed: false },
      },
      chatgpt: {
        plan_type: "",
        windows: { observed: false },
        accounts: [],
      },
    },
    // The longest realistic values §A.3 names, in one payload.
    longest: {
      anthropic: {
        claude_code: { available: false },
        mcc: { available: true },
        accounts: [
          {
            account_id: longAccountId,
            name: "team account the operations crew shares with the london office",
            subscription_type: "max-5x-annual-team-unlimited-seat-enterprise",
            windows: {
              observed: true,
              observed_at: nowSeconds - 60,
              status: "weekly-limit-reached",
              five_hour_utilization: "99.99",
              five_hour_reset: "1793600000",
              weekly_utilization: "100.00",
              weekly_reset: "1794200000",
              overage_status: "overage-exhausted-with-a-disabled-reason-not-shown-here",
              overage_reset: "1793600000",
              usage_limit: "$4,050.2926",
              reset: "1794000000",
            },
          },
        ],
        windows: { observed: false },
      },
      chatgpt: {
        plan_type: "plus-a-very-long-plan-name-that-a-provider-could-send-one-day",
        windows: {
          observed: true,
          observed_at: nowSeconds - 60,
          primary_used_percent: "100.00",
          primary_reset_at: "1793600000",
          primary_window_minutes: "300",
          secondary_used_percent: "99.99",
          secondary_reset_at: "1794200000",
          secondary_window_minutes: "10080",
          credits_balance: "$4,050.2926",
          credits_unlimited: "false",
          limit_name: "a-limit-name-sixty-characters-long-x",
          rate_limit_reached_type: "primary-window",
        },
        accounts: [],
      },
    },
  };
}

export function prepareUsagePageScenario({ window, ROUTES, fetchCalls, fetchUrls }) {
  const doc = window.document;
  const flat = (el) =>
    el ? String(el.textContent || "").replace(/\s+/g, " ").trim() : "";

  async function waitFor(predicate, limit = 20000) {
    const started = Date.now();
    while (Date.now() - started < limit) {
      if (predicate()) return true;
      await wait(25);
    }
    return false;
  }

  return {
    async run({ scriptErrors, consoleErrors }) {
      const out = {
        scenarioFatal: null,
        scriptErrors,
        consoleErrors,
      };

      const ready = await waitFor(
        () =>
          window.usageHarness &&
          typeof window.usageHarness === "function" &&
          doc.querySelectorAll(".nav-link").length > 0,
      );
      if (!ready) {
        out.scenarioFatal =
          "the Usage page is not in this build: window.usageHarness is absent " +
          "or the nav never drew";
        return out;
      }
      const PAGE = window.usageHarness();
      // `load()` is async and keeps going after the config payload lands, and
      // its last steps replace `state.config` with a fresh object. Nothing
      // below is a measurement of the real page until it has drawn its own
      // last card, so the wait is for that rather than for a timer.
      await waitFor(() => PAGE.state.config !== null, 20000);
      await waitFor(
        () => doc.querySelector("#codingAgentsList .coding-agent-card"),
        20000,
      );
      await wait(400);

      const navLinks = Array.from(doc.querySelectorAll("#sectionNav .nav-link"));
      const usageLink = navLinks.find((link) => link.dataset.view === "usage");
      const requestsLink = navLinks.find((link) => link.dataset.view === "requests");
      out.build = { hasHarness: true, navCount: navLinks.length };
      out.position = {
        exists: Boolean(usageLink),
        label: flat(usageLink),
        title: flat(doc.getElementById("pageTitle")),
        index: usageLink ? navLinks.indexOf(usageLink) : -1,
        indexOfAnalytics: requestsLink ? navLinks.indexOf(requestsLink) : -1,
        immediatelyBeforeAnalytics: Boolean(
          usageLink &&
            requestsLink &&
            navLinks.indexOf(requestsLink) === navLinks.indexOf(usageLink) + 1,
        ),
        behindGetStarted: Boolean(
          usageLink &&
            navLinks.indexOf(usageLink) >
              navLinks.indexOf(
                navLinks.find((link) => link.dataset.view === "get_started") || {},
              ),
        ),
        // A page that claims a settings section would have renderSections()
        // fill it; `usage` claims none, which is what keeps every other page
        // and this one's own markup independent.
        claimsNoSection: (() => {
          const entry = PAGE.VIEW_GROUPS.find((view) => view.id === "usage");
          return Boolean(entry) && (entry.sections || []).length === 0;
        })(),
      };

      // ── who the server says is configured ───────────────────────────────
      // The harness payload carries `provider_status: []`, so the scenario
      // supplies the shape `/admin/api/config` really sends for an install
      // with the user's own providers, plus two that must NOT appear.
      PAGE.state.config = PAGE.state.config || {};
      PAGE.state.config.provider_status = [
        {
          provider_id: "anthropic_oauth",
          display_name: "Anthropic Claude subscription (OAuth, Caution)",
          kind: "remote",
          status: "configured",
          key_count: 1,
        },
        {
          provider_id: "openai",
          display_name: "OpenAI / ChatGPT",
          kind: "remote",
          status: "configured",
          key_count: 1,
        },
        {
          provider_id: "chatgpt_oauth",
          display_name: "ChatGPT OAuth (experimental)",
          kind: "remote",
          status: "configured",
          key_count: 1,
        },
        {
          provider_id: "deepseek",
          display_name: "DeepSeek",
          kind: "remote",
          status: "configured",
          key_count: 2,
        },
        {
          provider_id: "novita",
          display_name: "Novita AI",
          kind: "remote",
          status: "configured",
          key_count: 3,
        },
        {
          provider_id: "cline",
          display_name: "Cline",
          kind: "remote",
          status: "configured",
          key_count: 1,
        },
        {
          provider_id: "ollama",
          display_name: "Ollama (local)",
          kind: "local",
          status: "unknown",
          key_count: 0,
        },
        // No credential: must be absent from the page entirely.
        {
          provider_id: "groq",
          display_name: "Groq",
          kind: "remote",
          status: "missing_key",
          key_count: 0,
        },
        {
          provider_id: "cerebras",
          display_name: "Cerebras",
          kind: "remote",
          status: "missing_key",
          key_count: 0,
        },
      ];

      const nowSeconds = Math.floor(Date.now() / 1000);
      const payloads = usagePayloads(nowSeconds);

      // ── the one route 8.4.0 adds (USAGE PR 2) ───────────────────────────
      // Stubbed in the shape the Python route returns, so the page renders a
      // real answer rather than a fixture invented for the renderer. Every
      // state it can produce is here once: measured, stale, not read yet, and
      // a provider with no mechanism at all (which is not drawn).
      ROUTES["/admin/api/usage"] = {
        observed_at: nowSeconds,
        stale_after_seconds: 1800,
        providers: [
          {
            provider_id: "deepseek",
            display_name: "DeepSeek",
            mechanisms: ["response-header", "provider-endpoint"],
            source: "response-header",
            detail: "the x-ratelimit response headers",
            state: "measured",
            observed_at: nowSeconds - 90,
            status_code: 200,
            values: {
              "x-ratelimit-limit-requests": "5000",
              "x-ratelimit-remaining-requests": "4997",
              "x-ratelimit-reset-requests": "1s",
            },
          },
          {
            // A provider this install does not hold, so it must NOT be drawn:
            // the page is about what you have.
            provider_id: "nous_portal",
            display_name: "Nous Portal",
            mechanisms: ["response-header"],
            source: "response-header",
            detail: "Nous Portal's credits response headers",
            state: "measured",
            observed_at: nowSeconds - 60,
            status_code: 200,
            values: { "x-nous-credits-remaining-usd": "142.5" },
          },
        ],
      };

      // ── zero new requests ───────────────────────────────────────────────
      // `fetchCalls` and `fetchUrls` are the harness's own recorders, filled
      // by the fetch stub. 8.4.0 changed what this block proves: opening the
      // view now makes EXACTLY ONE request -- `/admin/api/usage`, this
      // server's own memory -- and re-rendering makes none at all.
      const fetchCounters = () => ({
        calls: fetchCalls.length,
        urls: fetchUrls.length,
      });
      const snapshot = fetchCounters();

      const openUsage = async () => {
        const link = doc.querySelector('.nav-link[data-view="usage"]');
        if (!link) return false;
        link.click();
        await wait(120);
        return true;
      };

      PAGE.rememberUsagePayload("anthropic_oauth", payloads.measured.anthropic);
      PAGE.rememberUsagePayload("chatgpt_oauth", payloads.measured.chatgpt);
      const beforeOpen = fetchCounters();
      const opened = await openUsage();
      await waitFor(() => PAGE.usageCaptured.state !== "pending", 4000);
      const afterOpen = fetchCounters();
      // Re-render three times with the payload already in memory: the page's
      // own re-render path, which is what a theme switch or a settings reload
      // does to it. Still nothing.
      PAGE.paintUsageView();
      PAGE.paintUsageView();
      PAGE.openUsageView();
      await wait(60);
      const afterRepaint = fetchCounters();
      out.requests = {
        opened,
        before: beforeOpen,
        afterOpen,
        afterRepaint,
        // The two admin paths the subscription figures arrive on, named so a
        // reader can check that no third one exists. 8.4.0 adds exactly one
        // more, below, and it is this server's own memory.
        sources: PAGE.USAGE_SUBSCRIPTIONS.map((spec) => spec.route),
        // What opening the view added, URL by URL. One entry on a first open
        // (`/admin/api/usage`), none on the repaints that follow it.
        addedOnOpen: fetchUrls.slice(beforeOpen.urls, afterOpen.urls),
        addedOnRepaint: fetchUrls.slice(afterOpen.urls, afterRepaint.urls),
        // The payloads a page load ALREADY made, for context: these were
        // recorded before this scenario touched anything.
        preexisting: snapshot,
      };

      /** Open every provider row's detail.
       *
       * 8.7.0 puts a provider's reading, its Read now and its full figure list
       * behind the row's own expansion in `one_screen` — the default layout —
       * so every capture below opens the rows first. The control and the
       * figures are still on the page; they are one press in, which is what
       * keeps the page on one screen (UI-QUALITY-RULE §D's accordion).
       */
      const openAllRows = () => {
        doc.querySelectorAll(".usage-srow-button").forEach((button) => {
          if (button.getAttribute("aria-expanded") !== "true") button.click();
        });
      };

      // ── the header-derived block (8.4.0) ────────────────────────────────
      // Read once from the route's own shape above. `PAGE.usageCaptured` is
      // the state a real answer arrives in, so what this reads is what a real
      // GET would have produced.
      const capturedCards = () => {
        openAllRows();
        const grid = doc.getElementById("usageLayoutRoot");
        return Array.from(grid ? grid.querySelectorAll(".usage-card") : []).map((card) => ({
          title: flat(card.querySelector(".usage-card-title")),
          chip: flat(card.querySelector(".usage-chip")),
          chipState: card.querySelector(".usage-chip")
            ? card.querySelector(".usage-chip").dataset.state
            : null,
          keys: flat(card.querySelector(".usage-card-keys")),
          blocks: Array.from(
            card.querySelectorAll(".usage-detail-columns .usage-block"),
          ).map(
            (block) => ({
              heading: flat(block.querySelector(".usage-block-heading")),
              figures: Array.from(block.querySelectorAll("dt")).map((dt, i) => [
                flat(dt),
                flat(block.querySelectorAll("dd")[i]),
                block.querySelectorAll("dd")[i]
                  ? block.querySelectorAll("dd")[i].getAttribute("title") || ""
                  : "",
              ]),
            }),
          ),
          stateNote: flat(card.querySelector(".usage-state-note")),
          plan: flat(card.querySelector(".usage-plan")).replace(/^Plan: /, ""),
          account: flat(card.querySelector(".usage-account")),
          provenance: flat(card.querySelector(".usage-provenance")),
        }));
      };
      const capturedFromRoute = capturedCards();
      out.captured = {
        loadedState: PAGE.usageCaptured.state,
        cards: capturedFromRoute,
        titles: capturedFromRoute.map((card) => card.title),
        // One card per CONFIGURED provider with a mechanism: DeepSeek is
        // configured and has one, Nous is not configured and must be absent
        // even though the server reports a reading for it.
        shownFor: capturedFromRoute.map((card) => card.title),
        // Neither of the two subscriptions, and nothing with no credential.
        drawsSubscriptions: capturedFromRoute.some((card) =>
          /subscription/i.test(card.title),
        ),
        // The four states the route can produce, rendered faithfully. The
        // state word comes from the server, so the page is checked against
        // every value it can be handed rather than against one fixture.
        stateFixtures: [
          "measured",
          "stale",
          "not_measured",
          "unsupported",
          "something_new",
        ].map((serverState) => ({
          serverState,
          kind: PAGE.usageCapturedStateKind(serverState),
        })),
        // A host that sent nothing is "not read yet" rather than a zero, and
        // one with no mechanism gets the one sentence.
        unreadCard: (() => {
          // The coverage row is what the page draws from (8.7.0), so that is
          // what the fixture feeds it: a provider with a header mechanism that
          // has carried nothing.
          PAGE.rememberUsageCaptured({
            observed_at: nowSeconds,
            stale_after_seconds: 1800,
            auto_refresh_seconds: 30,
            providers: [
              {
                provider_id: "deepseek",
                display_name: "DeepSeek",
                mechanisms: ["response-header"],
                source: "response-header",
                detail: "the x-ratelimit response headers",
                state: "not_measured",
                observed_at: null,
                status_code: null,
                values: {},
                readable: false,
                endpoint: null,
                fetched_at: null,
                reading: null,
                attempted_at: null,
                read_error: "",
                not_sent: false,
                retry_in_seconds: null,
                next_read_in_seconds: null,
                interval_seconds: 30,
                consecutive_failures: 0,
              },
            ],
            coverage: [
              {
                provider_id: "deepseek",
                display_name: "DeepSeek",
                credentials: 1,
                state: "not_measured",
                stage: "read",
                recognised: "",
                sentence: "",
                readable: false,
                mechanisms: ["response-header"],
                detail: "the x-ratelimit response headers",
                needs_credential: "",
                reading: null,
                fetched_at: null,
                observed_at: null,
                attempted_at: null,
                read_error: "",
                not_sent: false,
                retry_in_seconds: null,
                next_read_in_seconds: null,
                consecutive_failures: 0,
                interval_seconds: 30,
                values: {},
                endpoint: null,
              },
            ],
          });
          return capturedCards();
        })(),
        // The read of this server failing is state 4 and says so, in the
        // block's own note rather than as a blank grid.
        failedNote: (() => {
          PAGE.rememberUsageCapturedFailure(
            new Error("the server did not answer"),
          );
          const note = doc.getElementById("usageStatus");
          return {
            hidden: note.hidden,
            text: flat(note),
          };
        })(),
      };
      // Restore the good answer for the blocks that follow.
      PAGE.rememberUsageCaptured(ROUTES["/admin/api/usage"]);
      PAGE.paintUsageView();
      await wait(20);

      // ── the four states ─────────────────────────────────────────────────
      const readCards = () => {
        openAllRows();
        const grid = doc.getElementById("usageLayoutRoot");
        const cards = Array.from(grid ? grid.querySelectorAll(".usage-card") : []);
        return cards.map((card) => ({
          title: flat(card.querySelector(".usage-card-title")),
          chip: flat(card.querySelector(".usage-chip")),
          chipState: card.querySelector(".usage-chip")
            ? card.querySelector(".usage-chip").dataset.state
            : null,
          chipTone: card.querySelector(".usage-chip")
            ? Array.from(card.querySelector(".usage-chip").classList)
                .filter((name) => name.startsWith("usage-chip-"))
                .map((name) => name.slice("usage-chip-".length))[0]
            : null,
          keys: flat(card.querySelector(".usage-card-keys")),
          blocks: Array.from(
            card.querySelectorAll(".usage-detail-columns .usage-block"),
          ).map(
            (block) => ({
              heading: flat(block.querySelector(".usage-block-heading")),
              note: flat(block.querySelector(".usage-block-note, .usage-figures-note")),
              figures: (() => {
                // The provider's own numbers, as this card draws them. A
                // window is a bar with a money pair; a balance is money. The
                // value printed here is the one a reader sees, and the exact
                // provider strings are on the element's `title`.
                const pairs = [];
                block.querySelectorAll(".usage-window").forEach((win) => {
                  const name = flat(win.querySelector(".usage-window-title"));
                  const value = win.querySelector(".usage-window-figure");
                  if (!name || !value) return;
                  pairs.push([
                    name,
                    flat(value),
                    value.className,
                    value.getAttribute("title") || "",
                  ]);
                  // The reset belongs to the same window and travels with it:
                  // a percentage without its reset cannot be acted on.
                  const reset = win.querySelector(".usage-reset");
                  if (reset) {
                    pairs.push([
                      `${name} resets`,
                      flat(reset).replace(/^resets /, ""),
                      reset.className,
                      reset.getAttribute("title") || "",
                    ]);
                  }
                });
                block.querySelectorAll(".usage-money-row").forEach((line) => {
                  const name = flat(line.querySelector(".usage-money-label"));
                  const value = line.querySelector(".usage-money-amount");
                  if (!name || !value) return;
                  pairs.push([name, flat(value), value.className, value.getAttribute("title") || ""]);
                });
                if (pairs.length) return pairs;
                return Array.from(block.querySelectorAll("dt")).map((dt, i) => [
                  flat(dt),
                  flat(block.querySelectorAll("dd")[i]),
                  block.querySelectorAll("dd")[i]
                    ? block.querySelectorAll("dd")[i].className
                    : "",
                  block.querySelectorAll("dd")[i]
                    ? block.querySelectorAll("dd")[i].getAttribute("title") || ""
                    : "",
                ]);
              })(),
              // The EXACT strings, from the disclosure 8.7.0 keeps them in.
              // Read from the CARD, not the block: the disclosure is a sibling
              // of the two human blocks, not a child of either.
              exact: Array.from(
                card.querySelectorAll(".usage-figures dt"),
              ).map((dt, index) => [
                flat(dt),
                flat(card.querySelectorAll(".usage-figures dd")[index]),
              ]),
            }),
          ),
          stateNote: flat(card.querySelector(".usage-state-note")),
          // The plan and the account: what a reader looks for BEFORE a number.
          plan: flat(card.querySelector(".usage-plan")).replace(/^Plan: /, ""),
          account: flat(card.querySelector(".usage-account")),
          failure:
            flat(card.querySelector(".usage-failure")) ||
            flat(card.querySelector('.usage-state-note[data-tone="error"]')),
          provenance: flat(card.querySelector(".usage-provenance")),
          // The thing this page must never print: one combined "remaining"
          // figure. Measured over the card's TERMS AND VALUES only, never over
          // its prose: every balance block says in words that the two are not
          // added, and a scan of the whole card would fire on that sentence
          // rather than on a failure.
          hasCombinedTotal: (() => {
            const values = Array.from(
              card.querySelectorAll(
                ".usage-window-title, .usage-window-figure, .usage-money-label, .usage-money-amount, dt, dd",
              ),
            )
              .map(flat)
              .join(" ");
            return /remaining total|total remaining/i.test(values);
          })(),
        }));
      };

      const sweep = {};
      for (const name of ["measured", "stale", "unobserved"]) {
        PAGE.rememberUsagePayload("anthropic_oauth", payloads[name].anthropic);
        PAGE.rememberUsagePayload("chatgpt_oauth", payloads[name].chatgpt);
        PAGE.paintUsageView();
        await wait(20);
        sweep[name] = readCards();
      }
      // The failed read, through the same door a real failure takes.
      PAGE.rememberUsageFailure(
        "anthropic_oauth",
        new Error("401 Unauthorized: the stored credential was refused"),
      );
      PAGE.rememberUsageFailure(
        "chatgpt_oauth",
        new Error("no answer within 30 s"),
      );
      PAGE.paintUsageView();
      await wait(20);
      sweep.failed = readCards();
      sweep.failedStatus = flat(doc.getElementById("usageStatus"));
      sweep.failedStatusHidden = doc.getElementById("usageStatus").hidden;
      out.cards = sweep;

      // ── who appears, who does not ───────────────────────────────────────
      PAGE.rememberUsagePayload("anthropic_oauth", payloads.measured.anthropic);
      PAGE.rememberUsagePayload("chatgpt_oauth", payloads.measured.chatgpt);
      PAGE.paintUsageView();
      await wait(20);
      // ── the providers with nothing readable ─────────────────────────────
      //
      // 8.7.0 draws EVERY configured provider as a row in the one list, in
      // exactly one of eight states, so "other providers" is no longer a
      // separate sentinel list — it is the cards whose state says there is
      // nothing to show. The four words are the ones the page prints, and a
      // card in any of them must carry no number and no control that promises
      // one.
      const NOTHING_READABLE = new Set([
        "unsupported",
        "needs_credential",
        "possible_endpoint",
      ]);
      // The rows must be OPEN before their sentences can be read: `one_screen`
      // builds a row's detail on its first expansion, and the paint above made
      // fresh rows.
      openAllRows();
      const others = Array.from(
        doc.querySelectorAll("#usageLayoutRoot .usage-card"),
      )
        .filter((item) => NOTHING_READABLE.has(item.dataset.state))
        .map((item) => ({
          provider: item.dataset.provider,
          state: item.dataset.state,
          name: flat(item.querySelector(".usage-card-title")),
          sentence: flat(item.querySelector(".usage-state-note")),
          pool: "",
          controls: item.querySelectorAll(
            "button, input, select, textarea, [role='button']",
          ).length,
          // A control that CANNOT be pressed. A provider with nothing to read
          // draws a DISABLED Read now (which says why), so the claim is "no
          // control that can only fail" rather than "no element".
          enabled_controls: Array.from(
            item.querySelectorAll(
              "button, input, select, textarea, [role='button']",
            ),
          ).filter((control) => !control.disabled).length,
          disabled_read_now: (() => {
            const button = item.querySelector(".usage-read-button");
            return button ? Boolean(button.disabled) : false;
          })(),
          spinner: item.querySelectorAll(".spinner, [aria-busy='true']").length,
        }));
      out.providers = {
        others,
        otherIds: others.map((item) => item.provider),
        cardTitles: readCards().map((card) => card.title),
        emptyNoteHidden: doc.getElementById("usageStatus").hidden,
        otherEmptyHidden: doc.getElementById("usageStatus").hidden,
      };

      // ── subscription allowance and cash, kept apart ─────────────────────
      out.blocks = readCards().map((card) => ({
        title: card.title,
        headings: card.blocks.map((block) => block.heading),
        hasSeparateHeadings: card.blocks.length >= 2,
        sameHeadingTwice:
          card.blocks.length !== new Set(card.blocks.map((b) => b.heading)).size,
      }));

      // ── the longest realistic values ────────────────────────────────────
      PAGE.rememberUsagePayload("anthropic_oauth", payloads.longest.anthropic);
      PAGE.rememberUsagePayload("chatgpt_oauth", payloads.longest.chatgpt);
      PAGE.paintUsageView();
      await wait(20);
      const longestCards = readCards();
      out.longest = {
        cards: longestCards,
        // Every digit and every character made it to the page rather than
        // being truncated by the renderer.
        hasTwoDecimalPercent: longestCards.some((card) =>
          card.blocks.some((block) =>
            block.figures.some(
              ([term, value]) =>
                term.includes("window") && value === "100.00%",
            ),
          ),
        ),
        // The provider's own `$4,050.2926` — the LONGEST dollar figure the
        // page meets — rendered as money. Above ten cents that is two decimals
        // (`$4,050.29`); the exact string is on the element's `title`, which
        // the assertion below checks rather than restating the rule here.
        hasDollarFigure: longestCards.some((card) =>
          card.blocks.some((block) =>
            block.figures.some(
              ([, value, , title]) =>
                value === "$4,050.29" && /4,050\.2926/.test(title || ""),
            ),
          ),
        ),
        // A reset instant, in the reader's own clock, with the provider's raw
        // value on the `title` — "resets Nov 2, 8:13 AM", never the epoch.
        hasResetTimestamp: longestCards.some((card) =>
          card.blocks.some((block) =>
            block.figures.some(
              ([, value]) =>
                /^[A-Z][a-z]{2} \d{1,2}, \d{1,2}:\d{2}/.test(value || "") &&
                !/T\d\d:/.test(value || ""),
            ),
          ),
        ),
        // The 60-character model/provider id §A.3 names, and the reset instant
        // on the element's `title`: a long value is allowed to be short on
        // screen, never to be gone.
        hasLongIdentifier: longestCards.some((card) =>
          card.blocks.some((block) =>
            block.figures.some(([, value]) => (value || "").length >= 40),
          ),
        ),
        // The EXACT provider strings, from the disclosure where 8.7.0 keeps
        // them: the human form deliberately shortens a figure, and the claim
        // §A.3 makes is that the whole value is still on the page — never that
        // it is the first thing a reader sees.
        // The widest single value the disclosure holds, which is where the
        // long provider strings live: `$4,050.2926 USD`, `100.00%`, and a
        // 62-character account line above them.
        exactFigureChars: Math.max(
          0,
          ...longestCards.flatMap((card) =>
            (card.blocks || []).flatMap((block) =>
              (block.exact || []).flatMap(([term, value]) => [
                String(value || "").length,
                String(term || "").length,
              ]),
            ),
          ),
        ),
        longestFigureChars: Math.max(
          0,
          ...longestCards.flatMap((card) =>
            card.blocks.flatMap((block) =>
              block.figures.map(([, value]) => (value || "").length),
            ),
          ),
        ),
      };

      // ── the view's own DOM, for the base-written equality baseline ──────
      // `innerHTML` of every VIEW, so the Python test can prove that the only
      // view this release changed is `usage` (a new one) and that every other
      // page is what the BASE tree drew.
      const views = {};
      doc.querySelectorAll(".admin-view[data-view]").forEach((host) => {
        views[host.dataset.view] = {
          children: host.children.length,
          length: host.innerHTML.length,
        };
      });
      out.views = views;
      out.viewIds = Object.keys(views);

      return out;
    },
  };
}
