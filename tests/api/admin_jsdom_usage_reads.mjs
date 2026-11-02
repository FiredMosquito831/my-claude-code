/**
 * The Usage page's on-demand read (8.5.0, USAGE PR 3), proved in jsdom
 * (MCC_JSDOM_SCENARIO=usage_reads).
 *
 * `usage_page` already proves the page's four states and that opening it adds
 * exactly one local GET. This scenario proves what PR 3 adds, and nothing
 * else:
 *
 *  1. `button` — the control exists on a readable provider's card, is DISABLED
 *     with a countdown inside the interval, and is ENABLED when the server
 *     says a read is allowed. The countdown is the server's own number.
 *  2. `press` — pressing it sends exactly ONE `POST /admin/api/usage/read`
 *     carrying exactly one provider id, and the answer repaints the card.
 *  3. `interval` — a second press while the server answers "already read"
 *     sends one request in total and the button stays disabled, because the
 *     server's `next_read_in_seconds` is what decides.
 *  4. `unreadable` — a provider this release cannot read draws no working
 *     button at all: a control that can only fail is not drawn as if it could
 *     work.
 *  5. `blocks` — the reading's windows and its balances are two blocks under
 *     two headings, and no element prints a combined "remaining".
 *  6. `states` — every state word the route can send renders as the chip it
 *     names, including `unavailable`, which is neither an error nor a zero.
 *  7. `deadline` — a 429's retry deadline disables the button and says when.
 *
 * Prints nothing itself: `run()` returns the measurements and the harness
 * prints them as one JSON object.
 */

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** A route payload for one readable provider, with a real reading on it. */
function readableRow(overrides = {}) {
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
    reading: {
      fetched_at: 1793600000,
      plan: "",
      available: true,
      unavailable_reason: "",
      windows: [
        {
          id: "fiveHour",
          label: "5-hour window",
          used: "1.25",
          limit: "8",
          unit: "usd",
          resets_at: "2026-11-02T18:00:00Z",
        },
      ],
      balances: [
        {
          id: "total_balance-USD",
          label: "Balance (USD)",
          amount: "$4,050.2926",
          currency: "USD",
          kind: "available",
        },
        {
          id: "granted_balance-USD",
          label: "Granted (promotional) (USD)",
          amount: "0.00",
          currency: "USD",
          kind: "promotional",
        },
      ],
      notes: [],
    },
    attempted_at: 1793600000,
    read_error: "",
    not_sent: false,
    retry_at: null,
    retry_in_seconds: null,
    next_read_in_seconds: null,
    interval_seconds: 1800,
    ...overrides,
  };
}

/** A subscription row: the declared read, on the card that also has headers.
 *
 *  8.5.0 gives Claude and Codex a usage route of their own, carried by the
 *  OAuth token MCC already holds. The card must show what it answered AND the
 *  control, beside the header-derived windows -- two readings of one
 *  subscription, both on the card the user already reads.
 */
function subscriptionRow(nowSeconds) {
  return {
    provider_id: "anthropic_oauth",
    display_name: "Claude subscription",
    mechanisms: ["provider-endpoint"],
    source: "provider-endpoint",
    detail: "",
    state: "measured",
    observed_at: null,
    status_code: null,
    values: {},
    readable: true,
    endpoint: {
      host: "https://api.anthropic.com",
      path: "/api/oauth/usage",
      provenance: "vendor-binary",
      reads: "the 5-hour and weekly windows",
      credential: "oauth-store",
      credential_env: "anthropic_oauth",
    },
    fetched_at: nowSeconds - 45,
    reading: {
      fetched_at: nowSeconds - 45,
      plan: "max",
      available: true,
      unavailable_reason: "",
      windows: [
        {
          id: "five_hour",
          label: "5-hour window",
          used: "38.0",
          limit: "",
          unit: "percent",
          resets_at: "2026-11-02T18:00:00Z",
        },
      ],
      balances: [],
      notes: [],
    },
    attempted_at: nowSeconds - 45,
    read_error: "",
    not_sent: false,
    retry_at: null,
    retry_in_seconds: null,
    next_read_in_seconds: 1755,
    interval_seconds: 1800,
  };
}

/** A provider this release cannot read: declared, unbuilt. */
function unreadableRow() {  return {
    provider_id: "kimi_coding",
    display_name: "Kimi Coding",
    mechanisms: ["provider-endpoint"],
    source: "provider-endpoint",
    detail: "the route is third-party evidence only; MCC does not read it yet",
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
    retry_at: null,
    retry_in_seconds: null,
    next_read_in_seconds: null,
    interval_seconds: 1800,
  };
}

export function prepareUsageReadsScenario({ window, ROUTES, fetchBodies }) {
  const doc = window.document;

  const flat = (node) =>
    node ? String(node.textContent || "").replace(/\s+/g, " ").trim() : "";

  /** Wait until `probe()` is truthy, or give up. Bounded: no infinite polls. */
  async function waitFor(probe, timeout = 4000) {
    const started = Date.now();
    while (Date.now() - started < timeout) {
      if (probe()) return true;
      await wait(25);
    }
    return false;
  }

  /** One card's figures, as the assertions read them. */
  const readCard = (card) => ({
    // EVERYTHING the row's detail holds. A card here is one provider's whole
    // block whatever the layout: its row, its expansion, and every figure in
    // it — which is what the assertions below are about.
    text: flat(card),
    title: flat(card.querySelector(".usage-card-title")),
    chip: flat(card.querySelector(".usage-chip")),
    chipState: card.querySelector(".usage-chip")
      ? card.querySelector(".usage-chip").dataset.state
      : null,
    // ONLY the card's own blocks: the complete figure list is behind a
    // `<details class="usage-figures-disclosure">`, and counting it here
    // would report the same heading twice.
    blocks: Array.from(
      card.querySelectorAll(".usage-detail-columns .usage-block"),
    ).map((block) => {
      // THE BLOCK'S OWN FIGURES, in the shape every card has used since 8.3.0.
      //
      // 8.7.0 draws a window as a bar with a money pair (`$1.25 of $8.00`)
      // rather than as a `<dt>/<dd>` pair, so a reader who wants the strict
      // term/value form finds it in the disclosure. What the block still holds
      // is its human form, and the pairs below are that form's own text read
      // back into the same shape — so an assertion about "this window's used
      // and limit" is answered by the card a person sees.
      const rows = [];
      block.querySelectorAll(".usage-window").forEach((win) => {
        const name = flat(win.querySelector(".usage-window-title"));
        const figure = win.querySelector(".usage-window-figure");
        const text = flat(figure);
        if (name && text) rows.push([name, text]);
        const reset = flat(win.querySelector(".usage-reset"));
        if (name && reset) rows.push([`${name} resets`, reset.replace(/^resets /, "")]);
      });
      block.querySelectorAll(".usage-money-row").forEach((line) => {
        const label = flat(line.querySelector(".usage-money-label"));
        const amount = flat(line.querySelector(".usage-money-amount"));
        if (label && amount) rows.push([label, amount]);
      });
      const exact = Array.from(block.querySelectorAll("dt")).map((dt, index) => [
        flat(dt),
        flat(block.querySelectorAll("dd")[index]),
      ]);
      return {
        heading: flat(block.querySelector(".usage-block-heading")),
        figures: rows.length ? rows : exact,
        exact,
      };
    }),
    // The complete figure list, behind its own disclosure.
    figures: Array.from(
      card.querySelectorAll(".usage-figures-disclosure .usage-figures dt"),
    ).map((dt) => flat(dt)),
    stateNote: flat(card.querySelector(".usage-state-note")),
    // The failure sentence and the state sentence are ONE paragraph now:
    // a card said the same thing twice in two boxes (the red one and the
    // muted one) until 8.7.0.
    failure: flat(card.querySelector(".usage-failure")) ||
      flat(card.querySelector('.usage-state-note[data-tone="error"]')),
    provenance: flat(card.querySelector(".usage-provenance")),
    button: (() => {
      const button = card.querySelector(".usage-read-button");
      return button
        ? {
            text: flat(button),
            disabled: Boolean(button.disabled),
            title: button.getAttribute("title") || "",
          }
        : null;
    })(),
    // The thing this page must never print.
    hasCombinedTotal: /remaining total/i.test(flat(card)),
  });

  const cards = () =>
    Array.from(doc.querySelectorAll("#usageLayoutRoot .usage-card")).map(readCard);


  /** Open every row's detail.
   *
   * 8.7.0 puts a provider's reading, its Read now and its figure list behind
   * the row's own expansion in `one_screen` (the default layout): the row is
   * 44 px and the detail is one press away, which is what keeps the page on
   * one screen. Every scenario that reads a card's CONTENTS therefore opens
   * it first — the control is still there, one press in, exactly as
   * UI-QUALITY-RULE §D describes a collapsed card's actions.
   */
  const openAllRows = () => {
    doc.querySelectorAll(".usage-srow-button").forEach((button) => {
      if (button.getAttribute("aria-expanded") !== "true") button.click();
    });
  };

  async function run({ scriptErrors, consoleErrors }) {
    const out = { scenarioFatal: null, scriptErrors, consoleErrors };
    // The harness hands us a page whose script has finished evaluating; the
    // Usage handle is what the scenario drives, and its absence means the page
    // never drew -- stated rather than reported as "the grid is empty".
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
    const nowSeconds = Math.floor(Date.now() / 1000);

    // A configured DeepSeek and a configured Kimi Coding: one readable, one
    // deliberately not. Everything else on the page is out of this scenario.
    const config = PAGE.state.config;
    ROUTES["/admin/api/config"] = {
      ...config,
      provider_status: [
        {
          provider_id: "anthropic_oauth",
          display_name: "Claude subscription",
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
          provider_id: "kimi_coding",
          display_name: "Kimi Coding",
          kind: "remote",
          status: "configured",
          key_count: 1,
        },
      ],
    };
    PAGE.state.config = ROUTES["/admin/api/config"];

    ROUTES["/admin/api/usage"] = {
      observed_at: nowSeconds,
      stale_after_seconds: 1800,
      minimum_interval_seconds: 1800,
      providers: [readableRow(), unreadableRow(), subscriptionRow(nowSeconds)],
    };
    // The read's own answer. The stub returns it for any provider id, which is
    // what a real route does for a row it can read -- and the scenario asserts
    // the request it was asked with.
    ROUTES["/admin/api/usage/read"] = {
      provider_id: "deepseek",
      requested: true,
      refusal: "",
      message: "",
      attempted_at: nowSeconds,
      next_read_in_seconds: 1800,
      retry_in_seconds: null,
      status_code: 200,
      state: "measured",
      fetched_at: nowSeconds,
      reading: {
        fetched_at: nowSeconds,
        plan: "",
        available: true,
        unavailable_reason: "",
        windows: [
          {
            id: "fiveHour",
            label: "5-hour window",
            used: "2.5",
            limit: "8",
            unit: "usd",
            resets_at: "2026-11-02T18:00:00Z",
          },
        ],
        balances: [
          {
            id: "total_balance-USD",
            label: "Balance (USD)",
            amount: "12.34",
            currency: "USD",
            kind: "available",
          },
        ],
        notes: [],
      },
      read_error: "",
      not_sent: false,
    };

    PAGE.rememberUsageCaptured(ROUTES["/admin/api/usage"]);
    PAGE.paintUsageView();
    openAllRows();
    await wait(30);

    // ── 1. the button, and the countdown the SERVER owns ─────────────────
    // The subscription card is measured first, because 8.5.0 gives it a read of
    // its own; the header-derived cards follow.
    out.subscription = (() => {
      const grid = doc.getElementById("usageLayoutRoot");
      const card = grid ? grid.querySelector(".usage-card") : null;
      if (!card) return { drawn: false };
      return {
        drawn: true,
        title: flat(card.querySelector(".usage-card-title")),
        headings: Array.from(card.querySelectorAll(".usage-block-heading")).map(flat),
        readsBlock: /Allowance/.test(flat(card)),
        button: (() => {
          const button = card.querySelector(".usage-read-button");
          return button
            ? { text: flat(button), disabled: Boolean(button.disabled) }
            : null;
        })(),
      };
    })();
    const first = cards();
    const enabled = first.filter((card) => card.button && !card.button.disabled);
    out.button = {
      cards: first.length,
      onReadable: enabled.length ? enabled[0].button : null,
      onUnreadable: first[1] ? first[1].button : null,
      // A card whose server says a read is allowed gets a live button.
      enabledWhenAllowed: (() => {
        const button = PAGE.usageReadButton({ ...readableRow(), next_read_in_seconds: null });
        return { disabled: button.disabled, text: button.textContent };
      })(),
      disabledInsideInterval: (() => {
        const button = PAGE.usageReadButton({ ...readableRow(), next_read_in_seconds: 1620 });
        return {
          disabled: button.disabled,
          text: button.textContent,
          title: button.getAttribute("title") || "",
        };
      })(),
      disabledByProviderDeadline: (() => {
        const button = PAGE.usageReadButton({
          ...readableRow(),
          retry_in_seconds: 300,
        });
        return { disabled: button.disabled, text: button.textContent };
      })(),
      // The one vocabulary for one wait.
      waits: [0, 30, 90, 1620, 7200].map((seconds) => ({
        seconds,
        text: PAGE.formatUsageWait(seconds),
      })),
    };

    // ── 2. the press: exactly one POST, exactly one provider id ──────────
    //
    // THE BUTTON THAT CAN BE PRESSED, not the first one on the page. A provider
    // inside its read interval correctly draws a DISABLED control, and
    // pressing that proves nothing — the scenario drives the first ENABLED
    // one, which is what a reader does too.
    const bodiesBefore = fetchBodies.length;
    const firstButton = Array.from(
      doc.querySelectorAll("#usageLayoutRoot .usage-read-button"),
    ).find((button) => !button.disabled);
    out.press = { found: Boolean(firstButton), posts: [], bodies: [], stateAfter: null };
    if (firstButton) {
      firstButton.click();
      await waitFor(() => fetchBodies.length > bodiesBefore, 2000);
      await wait(60);
      out.press.bodies = fetchBodies
        .slice(bodiesBefore)
        .filter((entry) => entry.path === "/admin/api/usage/read");
      out.press.posts = out.press.bodies.map((entry) => entry.method);
      openAllRows();
      out.press.stateAfter = cards().map((card) => ({
        title: card.title,
        chipState: card.chipState,
        button: card.button,
        blocks: card.blocks,
        figures: card.figures,
        hasCombinedTotal: card.hasCombinedTotal,
      }));
    }

    // ── 3. a second press inside the interval asks for nothing ───────────
    // The stub now answers the way the gate does: `requested: false`.
    ROUTES["/admin/api/usage/read"] = {
      provider_id: "deepseek",
      requested: false,
      refusal: "interval",
      message: "This provider was read less than 30 minutes ago; nothing was sent.",
      attempted_at: nowSeconds,
      next_read_in_seconds: 1750,
      retry_in_seconds: null,
      state: "measured",
      fetched_at: nowSeconds,
      reading: null,
      read_error: "",
      not_sent: false,
    };
    const bodiesBeforeSecond = fetchBodies.length;
    // The card's button is disabled after the first answer (the server said
    // 1800 seconds), so there is nothing to press -- which IS the proof. Ask
    // the button's own state rather than dispatching a click the product would
    // never receive.
    // The button of the provider that WAS read — the DeepSeek card — because
    // "it is now disabled for the interval" is a claim about that provider.
    const secondButton = (() => {
      const card = cards().find((entry) => entry.title === "DeepSeek");
      if (card) {
        const found = doc.querySelector(
          `.usage-srow[data-provider="deepseek"] .usage-read-button, ` +
            `[data-provider="deepseek"] .usage-read-button`,
        );
        if (found) return found;
      }
      return doc.querySelector("#usageLayoutRoot .usage-read-button");
    })();
    out.interval = {
      stillDisabled: secondButton ? Boolean(secondButton.disabled) : null,
      text: secondButton ? flat(secondButton) : null,
      bodiesBefore: bodiesBeforeSecond,
      // And a direct call through the same door the button uses, with the
      // stub answering `refused`, sends one request and changes nothing.
      refusalMessage: null,
    };
    if (secondButton) {
      await PAGE.readUsageForProvider("deepseek", secondButton);
      await wait(40);
      out.interval.bodiesAfter = fetchBodies.length;
      out.interval.asked = fetchBodies
        .slice(bodiesBeforeSecond)
        .filter((entry) => entry.path === "/admin/api/usage/read").length;
      out.interval.buttonAfter = (() => {
        const button = doc.querySelector("#usageLayoutRoot .usage-read-button");
        return button ? { disabled: Boolean(button.disabled), text: flat(button) } : null;
      })();
    }

    // ── 4. an unreadable provider draws no working control ───────────────
    // The provider whose row declares NO read this release builds. Found by
    // its own state rather than by list position: the provider list is ordered
    // by state since 8.7.0, so "the second card" is not a stable way to name
    // one.
    const unreadable = cards().find(
      (card) =>
        card.button !== null &&
        card.button.disabled === true &&
        /no read for this provider/i.test(card.button.title),
    );
    out.unreadable = {
      button: unreadable ? unreadable.button : null,
      stateNote: unreadable ? unreadable.stateNote : null,
      // A provider that sends no header figure never has that promised to it:
      // its card must not say a figure "fills in on the next request".
      promisesHeaders: unreadable
        ? /fills in on the next request/i.test(unreadable.stateNote)
        : null,
    };

    // ── 5. windows and balances are two blocks, never one total ──────────
    // The two blocks a card keeps apart, read from the provider whose reading
    // carries BOTH a window and a balance — DeepSeek's row here. The FIRST
    // card is the Claude subscription, which has a window and no cash at all,
    // so asking it for two headings would be asking the wrong card.
    // READ FROM THE PRESSED STATE. `cards()` after a fresh paint is a page
    // that has asked nothing, so its providers are `Not read yet` and carry no
    // figures at all — a fixture that answers "no two headings" without ever
    // having had a reading on screen.
    const pressed = out.press.stateAfter;
    const both = pressed.find((card) =>
      card.blocks.some((block) => block.heading === "Balance and spend"),
    );
    out.blocks = {
      headings: both ? both.blocks.map((block) => block.heading) : [],
      combined: both ? both.hasCombinedTotal : null,
      figuresBehindDisclosure: pressed.some((card) => card.figures && card.figures.length),
    };

    // ── 6. every state word the route can send ───────────────────────────
    out.states = [
      "measured",
      "stale",
      "not_measured",
      "unsupported",
      "unavailable",
      "failed",
      "something_new",
    ].map((serverState) => ({
      serverState,
      kind: PAGE.usageCapturedStateKind(serverState),
      chip: (() => {
        const chip = window.document.createElement("div");
        return null;
      })(),
    }));

    // ── 7. a 429's deadline ──────────────────────────────────────────────
    // The server sends the deadline; the card must show it and disable the
    // control until it passes.
    PAGE.rememberUsageCaptured({
      ...ROUTES["/admin/api/usage"],
      providers: [
        readableRow({
          state: "failed",
          read_error: "the provider is rate-limiting usage reads",
          retry_at: nowSeconds + 1600,
          retry_in_seconds: 1600,
          next_read_in_seconds: 1600,
          fetched_at: null,
          reading: null,
        }),
        unreadableRow(),
      ],
    });
    PAGE.paintUsageView();
    openAllRows();
    await wait(30);
    const deadlineCard = cards()[0];
    out.deadline = {
      chipState: deadlineCard.chipState,
      failure: deadlineCard.failure,
      button: deadlineCard.button,
      stateNote: deadlineCard.stateNote,
    };

    // ── 8. the unavailable state is neither an error nor a zero ──────────
    PAGE.rememberUsageCaptured({
      ...ROUTES["/admin/api/usage"],
      providers: [
        readableRow({
          state: "unavailable",
          reading: {
            fetched_at: nowSeconds,
            plan: "EXPIRED",
            available: false,
            unavailable_reason: "the host reports the plan status as EXPIRED",
            windows: [],
            balances: [],
            notes: [],
          },
          fetched_at: nowSeconds,
        }),
        unreadableRow(),
      ],
    });
    PAGE.paintUsageView();
    openAllRows();
    await wait(30);
    const unavailableCard = cards()[0];
    out.unavailable = {
      chip: unavailableCard.chip,
      chipState: unavailableCard.chipState,
      stateNote: unavailableCard.stateNote,
      failure: unavailableCard.failure,
      // The words a reader gets, which must not be the read-failed sentence.
      saysNotAvailable: /nothing available/i.test(
        `${unavailableCard.stateNote} ${unavailableCard.failure}`,
      ) || /Nothing available/i.test(flat(doc.querySelector("#usageLayoutRoot"))),
    };

    out.scriptErrorsSeen = scriptErrors;
    return out;
  }

  return { run };
}
