
// tokens_in is Anthropic's input_tokens: the uncached portion only. Providers
// translate their own accounting to that at the boundary, so total input is
// tokens_in plus whatever the cache served and whatever it wrote.
function uncachedInputTokens(row) {
  return Math.max(0, Number(row?.tokens_in || 0));
}

function totalInputTokens(row) {
  return (
    Number(row?.tokens_in || 0) +
    Number(row?.cache_read_tokens || 0) +
    Number(row?.cache_write_tokens || 0)
  );
}

// A request a local rule answered never reached a provider, so its provider
// column is NULL and the breakdown keys it as "local:<rule>" (see
// PROVIDER_KEY_SQL). "(unknown)" is still used, and still honest, for a row
// that has no provider and no rule -- we really do not know what served it.
const LOCAL_PROVIDER_PREFIX = "local:";
const UNKNOWN_PROVIDER_KEY = "(unknown)";

/** Turn a rule name into the words a reader of this dashboard uses. */
function optimizationRuleLabel(rule) {
  return String(rule || "").replace(/_/g, " ").trim();
}

/** The label for a provider-shaped value, wherever one is shown.
 *
 * Takes the breakdown key OR a raw row: a request row carries `provider` and
 * `optimization` as separate columns, and both have to reach the same words.
 */
function providerDisplayLabel(value, optimization) {
  const key = value == null ? "" : String(value);
  if (key.startsWith(LOCAL_PROVIDER_PREFIX)) {
    return `answered locally · ${optimizationRuleLabel(
      key.slice(LOCAL_PROVIDER_PREFIX.length),
    )}`;
  }
  if (key && key !== UNKNOWN_PROVIDER_KEY) return key;
  if (optimization) {
    return `answered locally · ${optimizationRuleLabel(optimization)}`;
  }
  return key;
}

function formatCacheHitRate(row) {
  const total = totalInputTokens(row);
  const cached = Number(row?.cache_read_tokens || 0);
  if (!total) return "—";
  // Not every upstream reports prompt caching. Showing 0.0% for those reads as
  // "caching is broken" rather than "this provider never told us", so an em
  // dash is reserved for the case where nothing reported a figure at all.
  if (row?.cache_reported === 0) return "—";
  return `${((cached / total) * 100).toFixed(1)}%`;
}

const state = {
  config: null,
  fields: new Map(),
  localStatus: new Map(),
  modelOptions: [],
  // Models the provider itself says reject images. Empty is honest:
  // an unreported capability is not a refusal.
  blindModels: new Set(),
  modelComboboxes: new Set(),
  // Route rails: the drag's whole state. The selection is held here and
  // never in the DOM, so a rail rebuilt by a drop cannot forget it.
  routeSelection: new Set(),
  routeAnchorId: null,
  routeArrowRange: [],
  routeDrag: null,
  // Depth 1, and one drag is one entry however many rows it moved.
  routeUndo: null,
  // chainKey -> ModelChainEditor. Cross-rail drag has to reach the
  // destination editor from a DOM node, and every editor used to be
  // reachable only through the closure that built it.
  routeRails: new Map(),
  // GET /admin/api/harness-tiers: the five tiers, what each resolves to
  // globally, and which of them each coding agent overrides.
  harnessTiers: null,
  activeView: "providers",
  webSearchStatsPeriod: "daily",
  webSearchAnalyticsStats: null,
  webSearchAnalyticsStatsKey: "",
  webSearchAnalyticsPage: null,
  webSearchAnalyticsPageKey: "",
  webSearchAnalyticsLoadId: 0,
  webSearchLastRoute: null,
  webSearchDetailReturnFocus: null,
  customProviders: [],
  editingCustomProviderId: null,
  versionInfo: null,
  versionUpgrading: false,
  desktop: null,
  desktopBusy: false,
  autostartOptions: null,
  rtk: null,
  rtkBusy: false,
  // Every desktop application MCC knows about, from /admin/api/desktop-apps.
  // Null until the Coding agents view is opened: the probe stats every marker
  // path on the machine, which is not work to do on a page nobody looked at.
  desktopApps: null,
  // What the last Configure or Undo said, per app id. A write is followed by
  // a reload, which rebuilds the card and would otherwise discard the one
  // sentence telling the user what just happened to their file.
  desktopAppMessages: {},
  // Every registered coding-agent harness, from /admin/api/harnesses. Owned
  // here rather than by the Coding agents view because the Token Optimizer
  // page's RTK checkboxes are generated from the same list.
  harnesses: null,
  // {enabled, days, counts, labels} from /admin/api/requests/harness-usage.
  // Fetched beside the harness list so a card can say how much traffic the
  // agent actually sent; null until that call lands or when it failed.
  harnessUsage: null,
  claudeSettings: null,
  claudeSettingsBusy: false,
  claudeConfig: {
    entries: [],
    values: {},
    // Pending edits keyed by the settings.json dotted path. A value of
    // `undefined` means "remove this key", which is a distinct operation from
    // writing false and the only way to turn a presence-read variable off.
    pending: new Map(),
    query: "",
    configuredOnly: false,
    showAll: false,
    busy: false,
    path: "",
    parsed: true,
  },
  onboarding: null,
  onboardingExpandedStepId: null,
  // Whether the expanded step was opened by a click rather than chosen
  // for the user. Auto-advance is a convenience; it must never overrule
  // someone who deliberately opened a step to re-read it.
  onboardingExpandedByUser: false,
  userNavigated: false,
  // True while load() is running. Navigation during the initial render must
  // not persist half-restored state (e.g. empty analytics filters) over the
  // saved state we are in the middle of restoring.
  loading: false,
};

const MASKED_SECRET = "********";
const VIEW_GROUPS = [
  {
    // Static content: no settings sections, nothing to fetch, so it stays
    // readable even when the server cannot reach a provider or the network.
    id: "get_started",
    label: "Get Started",
    title: "Get Started",
    sections: [],
    containerId: null,
  },
  {
    id: "providers",
    label: "Providers",
    title: "Providers",
    sections: ["providers", "runtime", "desktop"],
    containerId: "providersSections",
  },
  {
    // Egress: which addresses each configured provider may go out through.
    // Backed by ~/.mcc/proxy_chains.json rather than settings keys, so it
    // claims no manifest section and `containerId` stays null -- the same
    // shape the Coding agents Tiers section uses over harness_tiers.json.
    id: "proxying",
    label: "Proxying",
    title: "Proxying",
    sections: [],
    containerId: null,
  },
  {
    id: "claude",
    label: "Configure Claude Code",
    title: "Configure Claude Code",
    sections: [],
    containerId: null,
  },
  {
    // One page per concept: which agents exist, what MCC tells each of them,
    // and the RTK toggle. It claims no manifest section, so `containerId`
    // stays null and renderSections() skips it.
    id: "coding_agents",
    label: "Coding agents",
    title: "Coding agents",
    sections: [],
    containerId: null,
  },
  {
    id: "model_config",
    label: "Model Config",
    title: "Model Config",
    // `catalogue` joins them in 7.32.0: where a model's capabilities and
    // prices come from when the provider publishes none, and how long a fact
    // learned from a rejection stays applicable. It is model configuration,
    // and the Models page owns no manifest section of its own.
    sections: ["models", "reasoning", "web_tools", "catalogue"],
    containerId: "modelConfigSections",
  },
  {
    // Static markup filled from /admin/api/model-admin when the view opens.
    // It claims no manifest section, so `containerId` stays null and
    // renderSections() skips it -- both of its loops guard on containerId,
    // which is what stops a container-less view blanking every other tab.
    id: "models",
    label: "Models",
    title: "Models",
    sections: [],
    containerId: null,
  },
  {
    id: "messaging",
    label: "Messaging",
    title: "Messaging",
    sections: ["messaging", "voice"],
    containerId: "messagingSections",
  },
  {
    id: "requests",
    label: "Analytics",
    title: "Observability",
    // The page that shows the consequence owns the control: what the log keeps
    // is what these tables and content search can ever display, and what
    // prices a request is what the cost cards on this page can ever total.
    sections: ["request_log", "cost"],
    containerId: "requestsSections",
  },
  {
    // Measurement first, controls beside the number they affect. The
    // trimming settings are the only fields this view owns; everything above
    // them is read out of the request log.
    id: "optimizer",
    label: "Token Optimizer",
    title: "Token Optimizer",
    sections: ["optimizer"],
    containerId: "optimizerSections",
  },
  {
    id: "web_search",
    label: "Web Search",
    title: "Web Search",
    sections: ["websearch"],
    containerId: "webSearchSections",
  },
  {
    // Ceilings on one request, and what happens when a model will not honour
    // one. Request-log storage moved to Analytics and desktop timing to
    // Providers, so each subsystem is configured on the page that shows it.
    id: "limits",
    label: "Limits & Resilience",
    title: "Limits & Resilience",
    // Every one of these must be claimed by a view or its fields render
    // nowhere: the manifest registers them and the API serves them, and
    // nothing fails. That exact gap shipped once already, for "desktop", as a
    // settings page with no page.
    sections: [
      "budgets",
      "deadlines",
      "benching",
      "provider_retries",
      "credential_health",
      "loop_health",
      "diagnostics",
    ],
    containerId: "limitsSections",
  },
  {
    // Static content: no settings sections, nothing to fetch, so it stays
    // readable even when the server cannot reach a provider or the network.
    id: "guide",
    label: "Guide",
    title: "Guide",
    sections: [],
    containerId: null,
  },
  {
    // The project's documentation, rendered by the server from files bundled
    // in the wheel. Like the other static views it owns no settings section,
    // so containerId stays null and renderSections() skips it.
    id: "docs",
    label: "Docs",
    title: "Documentation",
    sections: [],
    containerId: null,
  },
];

const byId = (id) => document.getElementById(id);

function sourceLabel(source) {
  const labels = {
    default: "default",
    template: "template",
    repo_env: "repo .env",
    managed_env: "set here",
    explicit_env_file: "MCC_ENV_FILE",
    process: "process env",
  };
  return Object.prototype.hasOwnProperty.call(labels, source) ? labels[source] : source;
}

function sourceText(field) {
  const parts = [];
  const label = sourceLabel(field.source);
  if (label) {
    parts.push(label);
  }
  if (field.locked) {
    parts.push("locked");
  }
  return parts.join(" ");
}

function statusClass(status) {
  if (["configured", "reachable", "running"].includes(status)) return "ok";
  if (["missing_key", "missing_url", "unknown"].includes(status)) return "warn";
  if (["offline", "error"].includes(status)) return "error";
  return "neutral";
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
    cache: "no-store",
  });
  if (!response.ok) {
    let detail = "";
    try {
      const data = await response.json();
      detail = typeof data.detail === "string" ? data.detail : "";
    } catch {
      // Non-JSON error body; fall back to the status line.
    }
    throw new Error(detail || `${response.status} ${response.statusText}`);
  }
  return response.json();
}

async function load() {
  showMessage("Loading admin config");
  state.loading = true;
  try {
    await loadOnboarding().catch((error) => showMessage(error.message, "error"));
    await loadConfigDir().catch((error) => showMessage(error.message, "error"));
    await loadDashboardState();
  } finally {
    state.loading = false;
  }
}

async function loadDashboardState() {
  // Read the persisted state after the first await: at the very start of load()
  // (during initial script execution) the browser can still be settling
  // localStorage and a synchronous read returns an empty object. Yielding to
  // the event loop first makes the read reliable.
  const savedState = restoreDashboardState();
  if (
    state.onboarding &&
    !state.onboarding.dismissed &&
    !state.onboarding.complete &&
    !state.userNavigated
  ) {
    state.activeView = "get_started";
  } else if (savedState?.activeView) {
    state.activeView = savedState.activeView;
  }
  if (savedState?.autoRefresh != null && byId("reqAutoRefresh")) {
    byId("reqAutoRefresh").checked = Boolean(savedState.autoRefresh);
  }
  if (savedState?.autoRefreshInterval && byId("reqAutoRefreshInterval")) {
    byId("reqAutoRefreshInterval").value = String(savedState.autoRefreshInterval);
  }
  if (savedState?.webSearchStatsPeriod) {
    state.webSearchStatsPeriod = savedState.webSearchStatsPeriod;
    const periodSelect = byId("webSearchStatsPeriod");
    if (periodSelect) periodSelect.value = savedState.webSearchStatsPeriod;
  }
  // Restore the analytics filters and page so a refresh continues the same query.
  if (savedState?.reqFilters) {
    const f = savedState.reqFilters;
    if (byId("reqFilterProvider")) byId("reqFilterProvider").value = f.provider || "";
    if (byId("reqFilterModel")) byId("reqFilterModel").value = f.model || "";
    if (byId("reqFilterKey")) byId("reqFilterKey").value = f.key || "";
    if (byId("reqFilterHarness")) byId("reqFilterHarness").value = f.harness || "";
    if (byId("reqFilterSearch")) byId("reqFilterSearch").value = f.search || "";
    if (f.status && byId("reqFilterStatus")) byId("reqFilterStatus").value = f.status;
    if (byId("reqFilterEndpoint")) byId("reqFilterEndpoint").value = f.endpoint || "";
    if (f.local && byId("reqFilterLocal")) byId("reqFilterLocal").value = f.local;
    if (f.window && byId("reqFilterWindow")) byId("reqFilterWindow").value = f.window;
    if (f.pageSize && byId("reqPageSize")) {
      byId("reqPageSize").value = f.pageSize;
      reqState.limit = Number(f.pageSize) || reqState.limit;
    }
  }
  if (savedState?.reqOffset) {
    reqState.offset = Number(savedState.reqOffset) || 0;
  }
  const config = await api("/admin/api/config");
  state.config = config;
  state.fields = new Map(config.fields.map((field) => [field.key, field]));
  state.credentialEnvs = new Set(
    (config.provider_status || [])
      .map((provider) => provider.credential_env)
      .filter(Boolean),
  );
  renderNav();
  mountGuideLinks();
  renderSections(config.sections, config.fields);
  renderMessagingAuthNotice(config.messaging_auth_open);
  renderWebSearchProviders();
  await loadCustomProviders();
  byId("configPath").textContent = config.paths.managed;
  await hydrateModelOptions();
  await validate(false);
  await refreshLocalStatus();
  updateDirtyState();
  showMessage("");
  await loadVersionInfo();
  await loadDesktopState();
  await loadHarnesses();
  await loadDesktopApps();
  await loadOtherServers();
  await loadRtkState();
  await loadClaudeSettings();
  initClaudeConnectCopyButtons();
  // A restored "on" auto-refresh must actually start polling.
  updateRequestAutoRefresh();
}

function renderNav() {
  const nav = byId("sectionNav");
  nav.innerHTML = "";
  VIEW_GROUPS.forEach((view, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `nav-link${index === 0 ? " active" : ""}`;
    button.dataset.view = view.id;
    button.textContent = view.label;
    if (index === 0) {
      button.setAttribute("aria-current", "page");
    }
    button.addEventListener("click", () => {
      state.userNavigated = true;
      setActiveView(view.id, { scroll: true });
    });
    nav.appendChild(button);
  });
  setActiveView(state.activeView, { scroll: false });
}

function setActiveView(viewId, { scroll = false } = {}) {
  const activeView =
    VIEW_GROUPS.find((view) => view.id === viewId) || VIEW_GROUPS[0];
  state.activeView = activeView.id;
  byId("pageTitle").textContent = activeView.title;
  // Persist real navigation, but never the forced onboarding view, and never
  // while load() is mid-restore (it would clobber the saved state with the
  // not-yet-restored DOM).
  if (activeView.id !== "get_started" && !state.loading) persistDashboardState();

  document.querySelectorAll(".nav-link").forEach((link) => {
    const selected = link.dataset.view === activeView.id;
    link.classList.toggle("active", selected);
    if (selected) {
      link.setAttribute("aria-current", "page");
    } else {
      link.removeAttribute("aria-current");
    }
  });

  document.querySelectorAll(".admin-view").forEach((view) => {
    const selected = view.dataset.view === activeView.id;
    view.classList.toggle("active", selected);
    view.hidden = !selected;
  });

  if (activeView.id === "get_started") {
    loadOnboarding().catch((error) => showMessage(error.message, "error"));
  }

  if (activeView.id === "limits") {
    loadLoopLag().catch(() => {
      // A readout is not a reason to interrupt somebody editing limits: the
      // card keeps whatever it last said, and says so.
    });
    loadStuckRequests().catch(() => {
      // Same rule. An older server has no /admin/api/tasks/stacks at all.
    });
  }

  if (activeView.id === "web_search") {
    loadWebSearchAnalytics().catch((error) => showMessage(error.message, "error"));
  }

  if (activeView.id === "claude") {
    loadClaudeSettings().catch((error) => showMessage(error.message, "error"));
    loadClaudeConfig().catch((error) => showMessage(error.message, "error"));
    // Wiring at startup alone is not enough: the view is hidden then, and a
    // hidden block still needs its button before the reader first sees it.
    initClaudeConnectCopyButtons();
  }

  if (scroll) {
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  if (activeView.id === "requests") {
    loadRequestsView().catch((error) => showMessage(error.message, "error"));
  }

  if (activeView.id === "optimizer") {
    loadOptimizerView().catch((error) => showMessage(error.message, "error"));
  }

  if (activeView.id === "models") {
    loadModelsView().catch((error) => showMessage(error.message, "error"));
  }

  if (activeView.id === "coding_agents") {
    loadHarnesses().catch((error) => showMessage(error.message, "error"));
    loadDesktopApps().catch((error) => showMessage(error.message, "error"));
  }

  if (activeView.id === "docs") {
    loadDocsView().catch((error) => showMessage(error.message, "error"));
  }

  if (activeView.id === "proxying") {
    loadProxying().catch((error) => showMessage(error.message, "error"));
  }
}

/* --------------------------------------------------------------- proxying
   One card per configured provider: the ordered list of addresses it may go
   out through, the policy that picks between them, and the failures that move
   it along. Backed by ~/.mcc/proxy_chains.json through
   /admin/api/proxy-chains -- not by settings keys, so nothing here goes
   through changedValues() or the Apply button.

   Edits are local until Save. A reorder, a pause and a chip are all the same
   kind of change to the same list, and a page that wrote each one as it
   happened would make "put that back" impossible without an undo stack.

   Nothing in this block uses innerHTML: every label on this page can contain
   an address somebody else's feed supplied. */

const proxyState = {
  data: null,
  // provider_id -> the chain being edited, or null while it follows the
  // provider's static <PROVIDER>_PROXY. Cleared on every reload so a saved
  // card cannot keep showing a draft the server rejected.
  drafts: new Map(),
  // The feed list the server last confirmed, as a signature, and the page's
  // own editable copy of it. See `rememberSavedFeeds`.
  savedFeedSignature: "",
  feeds: [],
  // The Add form's half-finished row, and what one press of Detect format
  // last said about its URL. Held in state so a re-render -- which this page
  // performs after every change -- cannot wipe half-typed input.
  feedDraft: { name: "", url: "", parser: "", detecting: false, detection: null },
  loading: false,
  /* ---------------------------------------------- the candidate selection
     Held in state rather than read back out of the DOM, so it survives every
     re-render this page performs -- and it performs one after every batch of
     a bulk add. It is also written to localStorage, so it survives a reload:
     choosing forty addresses out of 1,572 is work, and an F5 is not a
     decision to throw that work away. */
  selected: new Set(),
  // proxy id -> what the last bulk action did to it. The per-row half of
  // reporting a partial result; the summary in the status panel is the other.
  outcomes: new Map(),
  // What is on screen, and where a bulk add would send it. Persisted with the
  // selection, the way the dashboard persists its Analytics filters.
  view: {
    text: "",
    scheme: "",
    minSources: 1,
    sort: "sources",
    destination: "",
    // Whether the addresses the sweep refused are shown. Off by default: they
    // are not offers, and the common case is not wanting to look at them.
    showRefused: false,
  },
  // The one-level undo point the server minted for the last bulk write.
  undo: null,
  /* ------------------------------------------------------- the fetch job
     A fetch reads the lists and then TESTS every address they offered, which
     for eight hundred addresses is minutes of work. So the press starts a job
     on the server and this page asks after it: `fetch` is the last status it
     was handed, `fetchPoll` is the interval asking for the next one.
     Both are rebuilt from the server on load, which is what lets a reload
     re-attach to a sweep that is still running instead of losing it. */
  fetch: null,
  fetchPoll: 0,
  // How many candidate rows are drawn. Paged rather than capped: hundreds of
  // tested, working addresses is the ordinary result now, and "narrow the
  // filter" is not an answer when every one of them is usable.
  drawn: 0,
  // The bulk run in flight: { total, done, stop, action }. A run is batched so
  // the page can show progress and stay usable, and so a long selection is not
  // one request that either all works or all does not.
  run: null,
  // Which bulk button is waiting for its second press (the inline confirm).
  confirming: "",
  // Whether the stored selection and filters have been read back yet. Once.
  restored: false,
  // The anchor of a Shift range, and the rows the last Shift+Arrow walk added.
  anchor: null,
  arrowRange: [],
};

/* Where the selection and the filters are kept between visits. Best-effort
   throughout: a browser with storage switched off gets the same page without
   the memory, never an exception. */
const PROXY_CANDIDATE_KEY = "mcc.proxying.candidates.v1";

/* How many addresses go in one request. Small on purpose: each one is a TLS
   handshake through a stranger's machine with a ten-second ceiling, so a
   selection sent as a single request would be a progress bar that never moves
   and a page that cannot be stopped. */
const PROXY_CANDIDATE_BATCH = 10;

/* How many rows are drawn at a time. A pass over seven lists offered 1,572
   addresses when this number was chosen, and a list that long is neither
   readable nor cheap to paint.

   It is a PAGE rather than a cap since 7.21.0. It used to be the end of the
   list -- "narrow the filter to see the rest" -- which was a fair answer while
   every row was an untested claim and most of them were dead. Now a fetch
   tests everything it found and keeps only what worked, so three hundred rows
   are three hundred addresses that were all working a minute ago and "the rest
   are not shown" is hiding usable work. Select all still means every address
   the filter matches, drawn or not. */
const PROXY_CANDIDATE_RENDER_CAP = 300;

/* How often the page asks how the fetch is getting on. Slow enough that a
   sweep of eight hundred addresses is not also a thousand requests to this
   process, fast enough that the counter visibly moves. */
const PROXY_FETCH_POLL_MS = 1500;

/* Above this, a bulk button asks for a second press. Not 200 like the Models
   page: the number was chosen while a chain held at most twelve entries, and
   7.19.0 removed that cap -- but the reason to confirm did not go with it. A
   discard of two hundred offers is still a bigger loss than hiding two hundred
   models ever was, and adding two hundred addresses in front of a credential
   is still worth one deliberate second press. */
const PROXY_CANDIDATE_CONFIRM_AT = 25;

/* The whole page in one request, including what a fetch started before this
   tab existed is doing.

   The status route answers the ordinary payload plus a `fetch` block, so
   asking it instead of `/admin/api/proxy-chains` is how a reload re-attaches:
   an operator who pressed Fetch on eight hundred addresses and then hit F5
   comes back to the progress line rather than to a page that has forgotten the
   sweep is running. It is a superset, so nothing else on the page can tell. */
async function loadProxying() {
  if (proxyState.loading) return;
  proxyState.loading = true;
  try {
    const payload = await api("/admin/api/proxy-chains/ingest/status");
    proxyState.data = payload;
    proxyState.fetch = payload.fetch || null;
    proxyState.drafts.clear();
    rememberSavedFeeds();
  } finally {
    proxyState.loading = false;
  }
  renderProxying();
  // Re-attach to a sweep that is still going. `watchProxyFetch` is idempotent,
  // so a second load while one is being watched does not start a second timer.
  if (proxyState.fetch && proxyState.fetch.state === "running") watchProxyFetch();
}

/* What the SERVER last told us the feed list is, and the page's own copy of it.

   Editing a row mutates `proxyState.feeds`, which is what relabels the Fetch
   button -- but the store is only written by the save route. Without a record
   of what was actually saved, the page counts rows and the server counts the
   store, and the two disagree the moment you change one without saving: the
   button offers to fetch feeds the server has never been told about, and the
   server answers "no feeds are switched on". That is what this signature
   exists to prevent.

   A signature over the whole row rather than a set of ids, because from
   7.18.0 a row can differ from the stored one by more than its switch: a
   rename, a re-pointed URL, a different format, an addition and a removal are
   all unsaved changes and all have to light the same button. */
function proxyFeedSignature(feeds) {
  return JSON.stringify(
    (feeds || []).map((feed) => [
      feed.id || "",
      feed.name || "",
      feed.url || "",
      feed.parser || "",
      Boolean(feed.enabled),
    ]),
  );
}

function rememberSavedFeeds() {
  const served = (proxyState.data && proxyState.data.feeds) || [];
  proxyState.savedFeedSignature = proxyFeedSignature(served);
  // The page edits its own copy, so a draft removal cannot reach back into
  // the payload the rest of the page renders from.
  proxyState.feeds = served.map((feed) => ({ ...feed }));
  proxyState.feedDraft = {
    name: "",
    url: "",
    parser: "",
    detecting: false,
    detection: null,
  };
}

/* The rows as the save route wants them: the four fields it accepts, plus the
   id, which is empty for a row the operator just added and is what makes add
   and edit the same request. */
function proxyFeedsForSave() {
  return (proxyState.feeds || []).map((feed) => ({
    id: feed.id || "",
    name: feed.name || "",
    url: feed.url || "",
    parser: feed.parser || "",
    enabled: Boolean(feed.enabled),
  }));
}

/* Whether the rows on screen differ from what the store holds. */
function proxyFeedsAreDirty() {
  return (
    proxyFeedSignature(proxyState.feeds) !==
    (proxyState.savedFeedSignature || proxyFeedSignature([]))
  );
}

/* The page's one live region.
 *
 * Every outcome on this page lands here -- a reorder, a save, and now the
 * summary of a bulk add with its Undo. One `role=status` panel that stays put
 * rather than a toast that vanishes before a partial result can be read: a
 * bulk add reports six different things about twelve addresses, and none of
 * them is readable in three seconds.
 *
 * `lines` are extra sentences under the lead; `undo` is a function, and the
 * button is only offered when there is one.
 */
function announceProxy(sentence, options = {}) {
  const target = byId("proxyingStatus");
  if (!target) return;
  target.textContent = "";
  if (!sentence) {
    target.hidden = true;
    return;
  }
  target.hidden = false;
  const lead = document.createElement("p");
  lead.textContent = sentence;
  target.appendChild(lead);
  (options.lines || []).forEach((line) => {
    const item = document.createElement("p");
    item.className = "proxy-status-line";
    item.textContent = line;
    target.appendChild(item);
  });
  if (typeof options.undo === "function") {
    const undo = document.createElement("button");
    undo.type = "button";
    undo.className = "secondary-button route-status-button proxy-status-undo";
    undo.textContent = options.undoLabel || "Undo";
    undo.addEventListener("click", () => {
      undo.disabled = true;
      options.undo();
    });
    target.appendChild(undo);
  }
  const dismiss = document.createElement("button");
  dismiss.type = "button";
  dismiss.className = "secondary-button route-status-button";
  dismiss.textContent = "Dismiss";
  dismiss.addEventListener("click", () => {
    target.textContent = "";
    target.hidden = true;
  });
  target.appendChild(dismiss);
}

function proxyVocabulary() {
  return (proxyState.data && proxyState.data.vocabulary) || {};
}

/** The chain being edited for one provider, created on first touch.
 *
 * A provider with no chain gets one seeded from its <PROVIDER>_PROXY, which
 * is the whole upgrade story: pressing Add proxy on a provider that already
 * has a static proxy keeps that address as entry 1 instead of silently
 * dropping it. The .env key itself is never rewritten.
 */
function proxyDraft(provider) {
  if (proxyState.drafts.has(provider.provider_id)) {
    return proxyState.drafts.get(provider.provider_id);
  }
  const vocabulary = proxyVocabulary();
  const chain = provider.chain;
  const draft = chain
    ? {
        enabled: Boolean(chain.enabled),
        policy: chain.policy,
        scope: chain.scope,
        max_switches: chain.max_switches,
        // Absent from a chain saved before 7.19.0, and absent means on: the
        // server reads a missing key the same way, so the two cannot drift.
        direct_fallback: chain.direct_fallback !== false,
        on: (chain.on || []).slice(),
        oauth_acknowledged: Boolean(chain.oauth_acknowledged),
        entries: (chain.entries || []).map((entry) => ({ ...entry })),
        existing: true,
      }
    : {
        enabled: false,
        policy: vocabulary.default_policy || "failover",
        scope: "provider",
        max_switches: (vocabulary.switch_bound || {}).default || 2,
        direct_fallback: true,
        on: (vocabulary.default_kinds || []).slice(),
        oauth_acknowledged: false,
        entries: [],
        existing: false,
      };
  proxyState.drafts.set(provider.provider_id, draft);
  return draft;
}

/* What is measuring these addresses, if anything.
 *
 * The page says it out loud because the shipped answer is "nothing": the
 * background checker is off until an operator turns it on, and a row reading
 * "not tested" beside no explanation would look like a page that forgot to
 * load rather than an install that has not been asked to make the call. */
function renderProxyCheckerNote() {
  const note = byId("proxyingChecker");
  if (!note) return;
  const checker = proxyVocabulary().checker || {};
  const sentences = [];
  if (checker.enabled && Number(checker.interval_minutes) > 0) {
    sentences.push(
      `Background checking is on: every address in a chain is re-measured ` +
        `about every ${checker.interval_minutes} minutes.`,
    );
  } else {
    sentences.push(
      "Background checking is off, so nothing here is measured until you " +
        "press Test. Turn on Check proxies in the background on Limits & " +
        "Resilience to have MCC watch them for you.",
    );
  }
  sentences.push(
    "A test opens one HTTPS request through the proxy to that provider's own " +
      "host, with ordinary strict certificate verification. If the " +
      "certificate does not verify, the tunnel is being read rather than " +
      "relayed: that address is marked TLS intercepted and refused outright.",
  );
  sentences.push(
    checker.exit_ip_configured
      ? "Your exit-IP URL is also fetched through each proxy, so the address " +
          "it reports is shown on the row."
      : "No exit-IP URL is set, so MCC contacts nobody but the provider. Set " +
          "Exit-IP check URL on Limits & Resilience if you want proof that " +
          "the source address really changed.",
  );
  note.textContent = sentences.join(" ");
}

/* ------------------------------------------------------- feeds and candidates
   The named public lists this install knows how to read, and what they
   currently offer.

   Two things this block must keep saying out loud, because they are the whole
   safety argument and not a disclaimer:

   1. Nothing is fetched until the operator ticks a feed and presses Fetch (or
      turns on the timer). A fresh install has no feed selected.
   2. An address that arrives here is a CANDIDATE. It is in no chain, no
      credential goes through it, and moving it into a chain tests it first --
      the same TLS-interception refusal a typed address gets.

   source_count is rendered with the feed names behind it rather than as a bare
   number: "4 feeds" is a score, "ProxyScrape, HProxy, Databay, Geonode" is an
   answer to "where did this machine's address come from". */

function renderProxyFeeds() {
  const panel = byId("proxyingFeeds");
  if (!panel) return;
  panel.textContent = "";
  const feeds = proxyState.feeds || [];

  /* A fresh install lands here: MCC ships no lists, so there is nothing to
     render but the form. This used to `return` on an empty array, which from
     7.18.0 would have meant every fresh install seeing a blank panel and no
     way to add anything -- the page's own version of shipping a Fetch button
     with no feeds behind it. */
  if (!feeds.length) {
    const empty = document.createElement("p");
    empty.className = "field-description proxy-feed-empty";
    empty.textContent =
      "No lists yet. MCC ships none of its own, so it has contacted nobody " +
      "and will not until you add one below and switch it on.";
    panel.appendChild(empty);
  } else {
    const row = document.createElement("div");
    row.className = "proxy-feed-row";
    feeds.forEach((feed) => row.appendChild(proxyFeedSwitch(feed)));
    panel.appendChild(row);
  }

  const actions = document.createElement("div");
  actions.className = "proxy-feed-actions";
  const enabled = feeds.filter((feed) => feed.enabled && feed.readable);

  const dirty = proxyFeedsAreDirty();
  const running = proxyState.fetch && proxyState.fetch.state === "running";
  const concurrency = proxyFetchConcurrency();

  const save = document.createElement("button");
  save.type = "button";
  save.className = "secondary-button";
  save.textContent = "Save feed list";
  save.disabled = !dirty;
  save.addEventListener("click", () => saveProxyFeeds(save));
  actions.appendChild(save);

  const fetchNow = document.createElement("button");
  fetchNow.type = "button";
  fetchNow.className = "primary-button proxy-feed-fetch";
  /* The label says what the press will DO, including the save it now performs
     when the rows differ from the store. It used to count ticked boxes while
     the route counted the store, so it could offer to fetch feeds from a
     server that had been told about none. `enabled` also excludes a row whose
     format MCC cannot read, for the same reason: the server would not read it
     either, so counting it would be the same lie in a new place. */
  fetchNow.textContent = !enabled.length
    ? "Fetch now"
    : dirty
      ? `Save and fetch ${enabled.length} feed${enabled.length === 1 ? "" : "s"}`
      : `Fetch ${enabled.length} feed${enabled.length === 1 ? "" : "s"} now`;
  fetchNow.disabled = !enabled.length || Boolean(running);
  fetchNow.title =
    "Reads the lists you switched on, then tests every address they offered " +
    "against the provider chosen below -- and keeps only the ones whose " +
    "tunnel left that provider's own certificate verifying. " +
    proxyFetchDepthSentence("that provider") +
    " Hundreds of addresses take minutes; " +
    (proxyFetchConcurrencyMode() === "percent"
      ? `${Number((proxyVocabulary().fetch || {}).concurrency) || 0}% of what ` +
        "the lists offer are tested at once"
      : `${concurrency} are tested at once`) +
    " and you can stop it at any point without losing what has already passed.";
  fetchNow.addEventListener("click", () => ingestProxyFeeds(fetchNow));
  actions.appendChild(fetchNow);
  panel.appendChild(actions);

  if (dirty) {
    const unsaved = document.createElement("p");
    unsaved.className = "field-description proxy-feed-unsaved";
    unsaved.textContent = enabled.length
      ? "This list is not saved yet. Fetch saves it first; Save feed list " +
        "stores it without reading anything."
      : "This list is not saved yet. Saving it with nothing switched on " +
        "means MCC reads nothing.";
    panel.appendChild(unsaved);
  }

  panel.appendChild(proxyFeedAddForm());

  const refresh = proxyVocabulary().refresh || {};
  const note = document.createElement("p");
  note.className = "field-description";
  note.textContent = refresh.enabled
    ? `Scheduled refresh is on: the feeds you switched on above are re-read ` +
      `about every ${Math.max(
        Number(refresh.interval_minutes) || 0,
        Number(refresh.minimum_minutes) || 30,
      )} minutes, tested the same way, and only what passes is kept. It ` +
      "writes this candidate list and nothing else."
    : "Scheduled refresh is off, so these lists are read only when you press " +
      "Fetch. Turn on PROXY_FEED_REFRESH_ENABLED on Limits & Resilience to " +
      "have them re-read -- and re-tested -- on a timer.";
  panel.appendChild(note);

  /* The bound, said out loud, in the operator's own number. 0 ships and means
     there is none, so the line says that rather than printing a 0 or, worse,
     a number this page invented. */
  const cap = proxyCandidateCap();
  const bound = document.createElement("p");
  bound.className = "field-description proxy-feed-bound";
  bound.textContent = cap
    ? `A fetch tests at most ${cap} address(es), best-ranked first -- you set ` +
      `PROXY_CANDIDATES_MAX to ${cap} on Limits & Resilience. Set it to 0 to ` +
      "test everything the lists offer."
    : "A fetch tests every address the lists offer, however many that is, and " +
      "keeps the ones that work. Set PROXY_CANDIDATES_MAX on Limits & " +
      "Resilience if you would rather it stopped at a number.";
  panel.appendChild(bound);
}

function proxyFeedSwitch(feed) {
  const label = document.createElement("label");
  label.className = "proxy-feed";
  const box = document.createElement("input");
  box.type = "checkbox";
  box.checked = Boolean(feed.enabled);
  // A feed MCC cannot read has nothing to switch on: the pass would skip it.
  // Disabled rather than hidden, with the reason on the row.
  box.disabled = !feed.readable;
  box.addEventListener("change", () => {
    feed.enabled = box.checked;
    renderProxying();
  });

  const body = document.createElement("span");
  body.className = "proxy-feed-body";
  const name = document.createElement("span");
  name.className = "proxy-feed-name";
  name.textContent = feed.name;
  body.appendChild(name);

  const url = document.createElement("span");
  url.className = "proxy-feed-url";
  /* The URL is shown in full, unlike a proxy URL. A proxy URL can carry
     user:pass and is masked everywhere on this page; a feed URL is a public
     list the operator typed themselves, and hiding it would leave them unable
     to tell two lists apart or see what they pointed MCC at. */
  url.textContent = feed.url;
  body.appendChild(url);

  const parser = document.createElement("span");
  parser.className = "proxy-feed-parser";
  parser.textContent = feed.readable
    ? proxyParserLabel(feed.parser)
    : "Format not recognised";
  if (feed.parser_shape) parser.title = feed.parser_shape;
  body.appendChild(parser);
  label.append(box, body);

  if (feed.tls_strict) {
    const badge = document.createElement("span");
    badge.className = "proxy-feed-badge";
    badge.textContent = "TLS-strict filter";
    badge.title =
      "This list's own filter selects for addresses that tunnel HTTPS " +
      "without breaking the destination's certificate checks -- the one " +
      "property MCC needs. It is a preference, not a verdict: only the Test " +
      "button finds out.";
    label.appendChild(badge);
  }

  if (!feed.readable) {
    const warning = document.createElement("span");
    warning.className = "proxy-feed-warning";
    warning.textContent =
      "MCC has no reader for this list's format, so it is skipped. Remove it, " +
      "or add it again and press Detect format.";
    label.appendChild(warning);
  }

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "secondary-button proxy-feed-remove";
  remove.textContent = "Remove";
  remove.title =
    "Removes the list. The addresses it already offered stay on offer -- " +
    "they are facts of their own, with their own test results.";
  remove.addEventListener("click", () => {
    proxyState.feeds = (proxyState.feeds || []).filter((item) => item !== feed);
    renderProxying();
    announceProxy(
      `${feed.name} removed from the list. Press Save feed list to keep that. ` +
        "Any addresses it offered stay on offer.",
    );
  });
  label.appendChild(remove);

  if (feed.observed) label.title = feed.observed;
  return label;
}

/* The readers this install ships, for the picker. MCC ships no feed, so this
   is the whole of what the page can offer: formats, never sources. */
function proxyParsers() {
  return proxyVocabulary().parsers || [];
}

function proxyParserLabel(parserId) {
  const found = proxyParsers().find((parser) => parser.id === parserId);
  return found ? found.label : parserId || "";
}

/** Name, URL, and a format the operator picks.
 *
 * Detection proposes and the operator decides, so the picker is ALWAYS
 * rendered -- never hidden behind a successful detection, never pre-submitted
 * on the page's behalf. Pressing Detect format reads the URL once and moves
 * the picker to what it found; pressing nothing leaves the picker where the
 * operator left it, and that is what gets saved.
 */
function proxyFeedAddForm() {
  const draft = proxyState.feedDraft;
  const form = document.createElement("div");
  form.className = "proxy-feed-add";

  /* Typing must reach the buttons WITHOUT a re-render.
   *
   * The first cut of this form computed `disabled` once, at render time, and
   * left the input handlers to mutate the draft only -- so an operator typed a
   * URL and watched Detect format stay greyed out until something unrelated
   * redrew the panel. Re-rendering on every keystroke is not the fix either:
   * it would tear out the very input being typed into and drop the caret. So
   * the handlers call this, and it is assigned once the buttons exist. */
  let syncAddForm = () => {};

  const title = document.createElement("h4");
  title.className = "proxy-feed-add-title";
  title.textContent = "Add a list";
  form.appendChild(title);

  const fields = document.createElement("div");
  fields.className = "proxy-feed-add-fields";

  const nameField = document.createElement("label");
  nameField.className = "proxy-feed-field";
  const nameLabel = document.createElement("span");
  nameLabel.textContent = "Name";
  const nameInput = document.createElement("input");
  nameInput.type = "text";
  nameInput.className = "proxy-feed-input";
  nameInput.value = draft.name;
  nameInput.placeholder = "What you will recognise it by";
  nameInput.maxLength = Number(proxyVocabulary().feed_name_max_length) || 60;
  nameInput.addEventListener("input", () => {
    draft.name = nameInput.value;
    syncAddForm();
  });
  nameField.append(nameLabel, nameInput);
  fields.appendChild(nameField);

  const urlField = document.createElement("label");
  urlField.className = "proxy-feed-field";
  const urlLabel = document.createElement("span");
  urlLabel.textContent = "URL (https)";
  const urlInput = document.createElement("input");
  urlInput.type = "url";
  urlInput.className = "proxy-feed-input";
  urlInput.value = draft.url;
  urlInput.placeholder = "https://example.com/proxies.json";
  urlInput.addEventListener("input", () => {
    draft.url = urlInput.value;
    // A URL that changed invalidates what the last detection said about the
    // old one. Clearing it is more honest than leaving a sentence about a
    // different address sitting under the picker.
    draft.detection = null;
    syncAddForm();
  });
  urlField.append(urlLabel, urlInput);
  fields.appendChild(urlField);

  const parserField = document.createElement("label");
  parserField.className = "proxy-feed-field";
  const parserLabel = document.createElement("span");
  parserLabel.textContent = "Format";
  const select = document.createElement("select");
  select.className = "proxy-feed-select";
  const blank = document.createElement("option");
  blank.value = "";
  blank.textContent = "Choose a format...";
  select.appendChild(blank);
  proxyParsers().forEach((parser) => {
    const option = document.createElement("option");
    option.value = parser.id;
    option.textContent = parser.label;
    option.title = parser.shape;
    select.appendChild(option);
  });
  select.value = draft.parser;
  select.addEventListener("change", () => {
    draft.parser = select.value;
    /* The operator has overridden the proposal, so the note under the picker
       must stop describing what was detected and start describing what they
       chose -- otherwise an override reads as if it had not registered. The
       detection is forgotten at the same time, for the same reason: it is no
       longer what this form is about to store. */
    draft.detection = null;
    syncAddForm();
  });
  parserField.append(parserLabel, select);
  fields.appendChild(parserField);
  form.appendChild(fields);

  const buttons = document.createElement("div");
  buttons.className = "proxy-feed-add-actions";

  const detect = document.createElement("button");
  detect.type = "button";
  detect.className = "secondary-button proxy-feed-detect";
  detect.title =
    "Reads this URL once and proposes the format it recognises. It only " +
    "proposes -- the picker stays yours.";
  detect.addEventListener("click", () => detectProxyFeed(detect));
  buttons.appendChild(detect);

  const add = document.createElement("button");
  add.type = "button";
  add.className = "secondary-button proxy-feed-add-button";
  add.textContent = "Add to list";
  add.addEventListener("click", () => addProxyFeed());
  buttons.appendChild(add);
  form.appendChild(buttons);

  const note = document.createElement("p");
  note.className = "field-description proxy-feed-detection";
  form.appendChild(note);

  /* The one place the form's derived state is computed, so the render path
     and every keystroke agree about it by construction. */
  syncAddForm = () => {
    detect.textContent = draft.detecting ? "Reading..." : "Detect format";
    detect.disabled = draft.detecting || !draft.url.trim();
    add.disabled = !draft.url.trim() || !draft.parser;
    const shape = draft.parser
      ? (proxyParsers().find((parser) => parser.id === draft.parser) || {}).shape
      : "";
    if (draft.detection && draft.detection.detail) {
      note.textContent = draft.detection.parser
        ? `${draft.detection.detail} The picker has been set to that; change ` +
          "it if you know better."
        : draft.detection.detail;
    } else if (shape) {
      note.textContent = shape;
    } else {
      note.textContent =
        "Pick the format yourself, or press Detect format to have MCC read " +
        "the URL once and propose one. Nothing else is fetched until you " +
        "switch the feed on.";
    }
  };
  syncAddForm();
  return form;
}

async function detectProxyFeed(button) {
  const draft = proxyState.feedDraft;
  const url = draft.url.trim();
  if (!/^https:\/\//i.test(url)) {
    announceProxy(
      "A feed URL must start with https://. MCC reads a proxy list only over " +
        "https: it is a list of addresses that will end up in front of a " +
        "credential.",
    );
    return;
  }
  draft.detecting = true;
  button.disabled = true;
  renderProxying();
  try {
    const answer = await api("/admin/api/proxy-chains/feeds/detect", {
      method: "POST",
      body: JSON.stringify({ url }),
    });
    draft.detection = answer.detection || {};
    /* The proposal moves the picker. It does NOT save, and it does not stop
       the operator moving it back: detection proposes, the operator decides,
       and the value in the picker when Add is pressed is what is stored. */
    if (draft.detection.parser) draft.parser = draft.detection.parser;
  } catch (error) {
    draft.detection = { ok: false, parser: "", detail: error.message };
    announceProxy(error.message);
  } finally {
    draft.detecting = false;
    renderProxying();
  }
}

function addProxyFeed() {
  const draft = proxyState.feedDraft;
  const url = draft.url.trim();
  if (!/^https:\/\//i.test(url)) {
    announceProxy("A feed URL must start with https://.");
    return;
  }
  if ((proxyState.feeds || []).some((feed) => feed.url === url)) {
    announceProxy("That URL is already in the list.");
    return;
  }
  proxyState.feeds = (proxyState.feeds || []).concat([
    {
      id: "",
      name: draft.name.trim() || url,
      url,
      parser: draft.parser,
      parser_shape: (
        proxyParsers().find((parser) => parser.id === draft.parser) || {}
      ).shape || "",
      readable: Boolean(draft.parser),
      observed: (draft.detection && draft.detection.detail) || "",
      tls_strict: false,
      // Added switched OFF. Adding a list and reading it are separate
      // decisions, and a list that started fetching because it was typed
      // would be the surprise this whole page exists to avoid.
      enabled: false,
    },
  ]);
  proxyState.feedDraft = { name: "", url: "", parser: "", detecting: false, detection: null };
  renderProxying();
  announceProxy(
    "Added to the list, switched off. Press Save feed list to keep it, then " +
      "switch it on when you want MCC to read it.",
  );
}

async function saveProxyFeeds(button) {
  const feeds = proxyFeedsForSave();
  button.disabled = true;
  try {
    proxyState.data = await api("/admin/api/proxy-chains/feeds", {
      method: "PUT",
      body: JSON.stringify({ feeds }),
    });
    rememberSavedFeeds();
    renderProxying();
    const on = feeds.filter((feed) => feed.enabled).length;
    announceProxy(
      on
        ? `${feeds.length} list(s) saved, ${on} switched on. Nothing has been ` +
            "fetched yet -- press Fetch, or turn on the scheduled refresh."
        : `${feeds.length} list(s) saved, none switched on. MCC contacts none ` +
            "of them.",
    );
  } catch (error) {
    button.disabled = false;
    announceProxy(error.message);
    showMessage(error.message, "error");
  }
}

async function ingestProxyFeeds(button) {
  const original = button.textContent;
  button.disabled = true;
  try {
    /* Save the selection first when it differs from the store.
       Ticking a box changes only this page's copy; the ingest route reads the
       store. Fetching without saving therefore asked the server to read feeds
       it had never been told about, and it correctly answered that none were
       switched on -- while this button said "Fetch 7 feeds now". Pressing
       Fetch is an unambiguous statement that the boxes on screen are the
       selection, so persist them rather than refuse, and say so. */
    if (proxyFeedsAreDirty()) {
      button.textContent = "Saving the list...";
      proxyState.data = await api("/admin/api/proxy-chains/feeds", {
        method: "PUT",
        body: JSON.stringify({ feeds: proxyFeedsForSave() }),
      });
      rememberSavedFeeds();
    }
    if (!(proxyState.feeds || []).some((feed) => feed.enabled && feed.readable)) {
      // Nothing to read, and the server would say so. Answer here instead of
      // spending a request on a question this page can already answer.
      renderProxying();
      announceProxy(
        "No feeds are switched on, so there is nothing to read. MCC ships " +
          "none of its own -- add a list above and switch it on, and it " +
          "contacts nobody until you do.",
      );
      return;
    }
    button.textContent = "Starting...";
    /* The press starts a JOB and comes straight back. A fetch now tests every
       address the lists offered -- a TCP connect, a tunnel, and a strict-TLS
       request to the chosen provider's own host, each -- and for a list of
       eight hundred that is minutes. Held open as one request it would time
       out in the browser, die on a reload, and leave nothing to press Stop on.
       The destination travels with it: a check is a question about one host,
       so the page says which. */
    const destination = proxyDestination();
    proxyState.data = await api("/admin/api/proxy-chains/ingest", {
      method: "POST",
      body: JSON.stringify({
        provider: destination ? destination.provider_id : "",
      }),
    });
    proxyState.fetch = proxyState.data.fetch || null;
    proxyState.outcomes = new Map();
    rememberSavedFeeds();
    renderProxying();
    announceProxy(proxyFetchSentence());
    watchProxyFetch();
  } catch (error) {
    button.disabled = false;
    button.textContent = original;
    announceProxy(error.message);
    showMessage(error.message, "error");
  }
}

/* ------------------------------------------------------------ the fetch job

   One timer, asking the server how the sweep is getting on. It is started by a
   press and by a page load that finds one already running, and it is stopped
   by the job ending -- never by navigating away from the page, because the job
   lives on the server and coming back has to find it. */
function watchProxyFetch() {
  if (proxyState.fetchPoll) return;
  proxyState.fetchPoll = window.setInterval(readProxyFetchStatus, PROXY_FETCH_POLL_MS);
}

function unwatchProxyFetch() {
  if (!proxyState.fetchPoll) return;
  window.clearInterval(proxyState.fetchPoll);
  proxyState.fetchPoll = 0;
}

async function readProxyFetchStatus() {
  let payload;
  try {
    payload = await api("/admin/api/proxy-chains/ingest/status");
  } catch (_) {
    // A poll that could not be answered is not a failed fetch. The sweep is on
    // the server and the next poll will find it; saying so would be inventing
    // an outcome the server never reported.
    return;
  }
  const previous = proxyState.fetch ? proxyState.fetch.state : "";
  proxyState.data = payload;
  proxyState.fetch = payload.fetch || null;
  rememberSavedFeeds();
  renderProxying();
  const state = proxyState.fetch ? proxyState.fetch.state : "idle";
  if (state === "running") return;
  unwatchProxyFetch();
  // Announce the end once, not on every poll after it.
  if (previous === "running") announceProxy(proxyFetchSentence());
}

async function stopProxyFetch() {
  const job = proxyState.fetch ? proxyState.fetch.job : "";
  try {
    const payload = await api("/admin/api/proxy-chains/ingest/stop", {
      method: "POST",
      body: JSON.stringify({ job }),
    });
    proxyState.data = payload;
    proxyState.fetch = payload.fetch || null;
    rememberSavedFeeds();
    renderProxying();
  } catch (error) {
    announceProxy(error.message);
    showMessage(error.message, "error");
  }
}

/* What the fetch is doing, or what it did, in one sentence a person can act
   on. The numbers are the server's own: the page keeps no count of its own,
   which is how "Tested 212 of 834" cannot drift from what was actually
   measured. */
function proxyFetchSentence() {
  const fetch = proxyState.fetch || {};
  const where = fetch.provider_name || fetch.provider || "the chosen provider";
  const measured =
    `Tested ${fetch.tested || 0} of ${fetch.total || 0} · ` +
    `${fetch.working || 0} working · ${fetch.dead || 0} dead · ` +
    `${fetch.refused || 0} refused`;
  if (fetch.state === "running") {
    const read = `${fetch.feeds_read || 0} of ${fetch.feeds_total || 0} list(s) read`;
    if (!fetch.total) {
      return (
        `Reading the lists: ${read}. Nothing is tested until they have all ` +
        "answered, and only addresses that pass are kept."
      );
    }
    const saved = `${fetch.persisted || 0} already saved`;
    const pace = proxyFetchPaceSentence();
    return (
      `${measured} · ${saved}. ` +
      (pace ? `${pace} ` : "") +
      proxyFetchDepthSentence(where) +
      " Passing addresses are written as they are found -- stopping, or a " +
      "restart, keeps them."
    );
  }
  if (fetch.state === "interrupted") {
    return (
      `${measured}. ${fetch.detail || "The fetch did not finish."} ` +
      `The ${fetch.persisted || 0} that passed are on offer below.`
    );
  }
  if (fetch.state === "failed") {
    return `The fetch did not finish: ${fetch.detail || "no reason was given"}.`;
  }
  if (fetch.state === "stopped") {
    return (
      `Stopped. ${measured}. The ${fetch.working || 0} that passed are on ` +
      "offer and on disk -- stopping kept them; the rest were not tested."
    );
  }
  if (fetch.state === "done") {
    const failed = (fetch.feeds || []).filter((item) => !item.ok);
    const extra = failed.length
      ? ` No usable answer from ${failed.map((item) => item.name).join(", ")}.`
      : "";
    const pace = proxyFetchPaceSentence();
    /* "Every address on offer below verified ..." is a claim about rows, and
       with no rows it is a claim about nothing -- which is how this line came
       to read as a contradiction of the panel beneath it once the addresses
       that passed had been promoted. With an empty offer list the line says
       what it measured and stops; the panel says where they went. */
    if (!proxyCandidates().length) {
      return `${measured}. ` + (pace ? `${pace}` : "").trim() + extra;
    }
    return (
      `${measured}. ` +
      (pace ? `${pace} ` : "") +
      `Every address on offer below verified ${where}'s certificate through ` +
      "its own tunnel a moment ago" +
      (proxyFetchCheckDepth() === "tls"
        ? `, without a request being sent to ${where}`
        : "") +
      ". They are still candidates: none is in a chain, and none carries a " +
      `credential until you add it to one.${extra}`
    );
  }
  return "No fetch has run yet.";
}

/* ------------------------------------------------------ the candidate list

   Three facts shape everything below. A pass over several lists offers
   hundreds of addresses -- seven of them offered 1,572 when this was written.
   One destination is chosen for the whole selection, not one per row. And
   adding an address *tests* it against that provider's own host, so adding
   twelve is twelve network calls with a ten-second ceiling each.

   The selection model is the Models page's, to the letter (6.7.0, and its
   one-write-path defect fixed in 6.24.0): a select column with Shift+click and
   the keyboard equivalents WCAG 2.2 requires, filter-then-apply-to-filtered,
   one `role=status` panel with Undo instead of a vanishing toast, an inline
   confirm above a threshold, a batched endpoint, and ONE write path with ONE
   repaint. A single-address action is the bulk action with one element in it,
   never a second route -- which is the whole content of 6.24.0. */

function proxyCandidates() {
  return (proxyState.data && proxyState.data.candidates) || [];
}

/* The addresses the checker found terminating TLS. Counted since 7.21.0 and
   shown since 7.35.1: "12 refused" with nothing to look at could not tell an
   operator which address it meant, or whether the verdict was from this
   afternoon or from March. These are never offers -- the server refuses them
   outright -- so nothing built from this list carries a gesture. */
function proxyRefusedAddresses() {
  return (proxyState.data && proxyState.data.refused_addresses) || [];
}

/* How many addresses in the operator's chains got there by passing a check.
   Read with a test for a number rather than `|| 0`, because 0 is a meaningful
   answer here and is the difference between "they are all in a chain" and
   "you discarded them". */
function proxyChainedPassing() {
  const value = Number(proxyState.data && proxyState.data.chained_passing);
  return Number.isFinite(value) ? value : 0;
}

/* The providers a candidate can actually be tested against. A chain entry is
   verified by opening an HTTPS request through the proxy to the provider's own
   host, so a provider with no https base URL has nothing to verify against and
   is not offered as a destination -- the same reason the per-row picker used
   to leave that row with a sentence instead of a dropdown. */
function proxyCandidateProviders() {
  return ((proxyState.data && proxyState.data.providers) || []).filter((provider) =>
    String(provider.base_url || "")
      .toLowerCase()
      .startsWith("https://"),
  );
}

function proxyIneligibleProviders() {
  return ((proxyState.data && proxyState.data.providers) || []).filter(
    (provider) =>
      !String(provider.base_url || "")
        .toLowerCase()
        .startsWith("https://"),
  );
}

/* Where the destination picker currently points, defaulting to the first
   provider that has somewhere to test against. */
function proxyDestination() {
  const providers = proxyCandidateProviders();
  const wanted = providers.find(
    (provider) => provider.provider_id === proxyState.view.destination,
  );
  // A subscription login is never the default destination. Changing source IP
  // between requests is more likely to be flagged on a personal subscription
  // than on a pay-as-you-go key, and the card makes the operator acknowledge
  // that before it will take a chain -- so it must not be what a press lands
  // on by accident either.
  return (
    wanted || providers.find((provider) => !provider.oauth) || providers[0] || null
  );
}

/* The operator's own ceiling on chain length, or 0 for "there is none".
 *
 * 0 is what ships since 7.19.0, and reading it with `|| 12` -- which is what
 * this page did while twelve was a hard cap -- turned the shipped default into
 * the old limit and put "a chain holds at most 12 entries" back on a card with
 * two hundred rows on it. `Number(...) || 12` cannot tell 0 from absent, so the
 * cap is read once, here, and every caller asks this. */
function proxyEntryCap() {
  const raw = proxyVocabulary().max_entries;
  const cap = Number(raw);
  return Number.isFinite(cap) && cap > 0 ? cap : 0;
}

/* The fetch settings, read from the server's own answer rather than kept as
   constants here.
 *
 * `PROXY_CANDIDATES_MAX` ships as 0 meaning UNLIMITED, which is the same trap
 * `proxyEntryCap` exists for: `Number(x) || 60` cannot tell 0 from absent, and
 * 7.19.0 shipped the old chain limit back onto the page four times over
 * exactly that. So it is read once, here, with a test for "is it a positive
 * number", and every caller asks this. */
function proxyCandidateCap() {
  const cap = Number((proxyVocabulary().fetch || {}).candidates_max);
  return Number.isFinite(cap) && cap > 0 ? cap : 0;
}

/* The number the operator set, read the way they set it to be read. In percent
   mode the setting is a percentage of a list nobody has fetched yet, so there
   is no honest count to print before a pass has run -- the live one from the
   job is used the moment there is one, and until then the page says what it
   knows rather than inventing a count. The fallback is the shipped default and
   has to be kept equal to PROXY_FETCH_TEST_CONCURRENCY_DEFAULT: `Number(x) ||
   32` is how a browser copy of a server number goes stale. */
function proxyFetchConcurrency() {
  const live = Number((proxyState.fetch || {}).concurrency);
  if (Number.isFinite(live) && live > 0) return live;
  const value = Number((proxyVocabulary().fetch || {}).concurrency);
  return Number.isFinite(value) && value > 0 ? value : 100;
}

function proxyFetchConcurrencyMode() {
  return String((proxyVocabulary().fetch || {}).concurrency_mode || "fixed");
}

/* How far a fetch tests each address. "tls" is the shipped answer and is the
   one that changes what the provider sees, so the page says it out loud. */
function proxyFetchCheckDepth() {
  const live = String((proxyState.fetch || {}).check_depth || "");
  if (live) return live;
  return String((proxyVocabulary().fetch || {}).check_depth || "tls");
}

/* The one sentence that says what a press of Fetch will do to the provider.
   It is the honest half of the default: a handshake proves the tunnel and the
   certificate, and it proves nothing about whether that provider would have
   answered a request -- which is why Add tests again, all the way. */
function proxyFetchDepthSentence(where) {
  const who = where || "the chosen provider";
  return proxyFetchCheckDepth() === "tls"
    ? `Each address is tunnelled to ${who}'s own host and the certificate is ` +
        `verified through it -- no request is sent to ${who}. Adding one to a ` +
        "chain tests it again with a real HTTPS request."
    : `Each address opens a real HTTPS request through that machine to ${who}` +
        "'s own host, and only the ones that answer with that host's " +
        "certificate intact are kept.";
}

/* What the concurrency worked out to, said only when it is not simply the
   number in the settings: a resolved percentage, or a value MCC had to
   reinterpret. Never a silence -- a percentage of 200 is a typo and the
   operator finds out here. */
function proxyFetchPaceSentence() {
  const fetch = proxyState.fetch || {};
  const parts = [];
  if (fetch.concurrency_summary && fetch.concurrency_mode === "percent") {
    parts.push(
      String(fetch.concurrency_summary)
        .replace(/^testing /, "Testing ")
        .concat("."),
    );
  }
  if (fetch.concurrency_note) parts.push(String(fetch.concurrency_note));
  return parts.join(" ");
}

/* How much of that provider's chain is already spoken for. This is the fact
   that shapes a bulk add more than any other and the page used to leave it to
   be discovered by a 422. With no cap set there is always room, and "add fifty
   selected" means fifty. */
function proxyChainRoom(provider) {
  const max = proxyEntryCap();
  const chain = provider && provider.chain;
  const used = chain ? (chain.entries || []).length : 0;
  return {
    used,
    max,
    room: max ? Math.max(0, max - used) : Number.MAX_SAFE_INTEGER,
  };
}

function proxyCandidateSources(candidate) {
  return Number(
    candidate.source_count || (candidate.sources || []).length || 1,
  );
}

function proxyCandidateMatches(candidate) {
  const view = proxyState.view;
  const text = String(view.text || "").trim().toLowerCase();
  if (text && !String(candidate.label || "").toLowerCase().includes(text)) {
    return false;
  }
  if (view.scheme && candidate.scheme !== view.scheme) return false;
  if (proxyCandidateSources(candidate) < Number(view.minSources || 1)) return false;
  return true;
}

/* What the filter matches, in the order the sort asks for. "Select all" means
   this list -- every address matching what the operator is looking at, not
   only the rows that happened to be drawn. */
function proxyFilteredCandidates() {
  const sort = proxyState.view.sort;
  const rows = proxyCandidates().filter(proxyCandidateMatches);
  const latency = (candidate) =>
    candidate.latency_ms === null || candidate.latency_ms === undefined
      ? Number.POSITIVE_INFINITY
      : Number(candidate.latency_ms);
  rows.sort((left, right) => {
    if (sort === "latency") return latency(left) - latency(right);
    if (sort === "address") {
      return String(left.label || "").localeCompare(String(right.label || ""));
    }
    if (sort === "scheme") {
      return String(left.scheme || "").localeCompare(String(right.scheme || ""));
    }
    // Most feeds agreeing first: the one field on a row that is evidence
    // rather than a claim copied from one publisher.
    return proxyCandidateSources(right) - proxyCandidateSources(left);
  });
  return rows;
}

function proxySelectedIds() {
  // Always in filtered order, so what is sent matches what was read.
  return proxyFilteredCandidates()
    .map((candidate) => candidate.proxy)
    .filter((proxy) => proxyState.selected.has(proxy));
}

/* An offer that a refetch dropped, or that has just become a chain entry, is
   not selectable any more -- and a stale id in the set would be silently sent
   to the server on the next press. */
function pruneProxyCandidateSelection(candidates) {
  const live = new Set(candidates.map((candidate) => candidate.proxy));
  let dropped = false;
  Array.from(proxyState.selected).forEach((proxy) => {
    if (live.has(proxy)) return;
    proxyState.selected.delete(proxy);
    dropped = true;
  });
  // What is left selected after a bulk add is exactly what did not land: the
  // addresses that went into the chain left the offer list and left the
  // selection with it. Persist that rather than the set as it was pressed.
  if (dropped && proxyState.restored) saveProxyCandidateView();
}

/* ------------------------------------------------------------ persistence
   The selection and the filters, kept where the dashboard keeps its Analytics
   filters. Picking forty addresses out of 1,572 is work, and an F5 is not a
   decision to throw it away. Best-effort in both directions: a browser with
   storage switched off gets the same page without the memory. */
function saveProxyCandidateView() {
  try {
    localStorage.setItem(
      PROXY_CANDIDATE_KEY,
      JSON.stringify({
        selected: Array.from(proxyState.selected),
        view: proxyState.view,
      }),
    );
  } catch (_) {
    /* storage unavailable or full; persistence is best-effort */
  }
}

function restoreProxyCandidateView() {
  if (proxyState.restored) return;
  proxyState.restored = true;
  try {
    const raw = localStorage.getItem(PROXY_CANDIDATE_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return;
    (parsed.selected || []).forEach((proxy) => proxyState.selected.add(String(proxy)));
    Object.assign(proxyState.view, parsed.view || {});
  } catch (_) {
    /* unreadable: the page is correct without it */
  }
}

function renderProxyCandidates() {
  const panel = byId("proxyingCandidates");
  if (!panel) return;
  restoreProxyCandidateView();
  panel.textContent = "";
  // The progress line comes first and is rendered whether or not anything is
  // on offer: the commonest moment to look at this panel is while a fetch is
  // running and the offer list is still empty, and an empty panel then reads
  // as a page that did nothing.
  const progress = proxyFetchPanel();
  if (progress) panel.appendChild(progress);
  const candidates = proxyCandidates();
  pruneProxyCandidateSelection(candidates);
  if (!candidates.length) {
    const empty = document.createElement("p");
    empty.className = "field-description";
    /* Two different reasons for an empty list, and they need different
       instructions. On a fresh install there is nothing to tick -- MCC ships
       no lists -- so "tick a feed above" would point at a row that does not
       exist and read as a page that failed to load. */
    const running = proxyState.fetch && proxyState.fetch.state === "running";
    const ran = proxyState.fetch && proxyState.fetch.state !== "idle";
    /* Whether anything passed is NOT the same question as whether anything is
       on offer, and until 7.35.1 this branch answered the second one with the
       first one's sentence. An address that passed leaves the offer list the
       moment it is in a chain -- which is the ordinary end of a successful
       sweep, and which used to be reported as "none of them passed". */
    const passed = Number((proxyState.fetch || {}).working) || 0;
    empty.textContent = running
      ? "Nothing has passed yet. Addresses appear here as they are tested, " +
        "and only the ones that answered with the provider's own certificate " +
        "intact are kept."
      : ran && passed > 0
        ? proxyChainedPassing() > 0
          ? `${passed} passed -- all of them are already in a chain, so there ` +
            "is nothing left here to choose from. Fetch again to look for more."
          : `${passed} passed, and none of them is still on offer: an address ` +
            "leaves this list when it goes into a chain or is discarded. " +
            "Fetch again to look for more."
        : ran
        ? "None of the addresses those lists offered passed the test, so " +
          "none is on offer. That is an ordinary result for public lists: " +
          "most of what they publish has stopped listening. Try another list."
        : (proxyState.feeds || []).length
          ? "No addresses are on offer. Switch a list on above and press " +
            "Fetch; every address the lists offer is tested and only the ones " +
            "that work come back -- as candidates you choose from, not a chain."
          : "No addresses are on offer, and no lists have been added yet. Add " +
            "one above and switch it on; every address it offers is tested " +
            "and only the ones that work come back -- as candidates you " +
            "choose from, not a chain.";
    panel.appendChild(empty);
    appendProxyRefused(panel);
    return;
  }
  // The controls are built once per full render and then left alone: the
  // filter repaints the list under them, and rebuilding the text field the
  // operator is typing into would take the caret with it.
  panel.appendChild(proxyCandidateControls());
  const bar = document.createElement("div");
  bar.className = "proxy-candidate-bar";
  panel.appendChild(bar);
  const list = document.createElement("ol");
  list.className = "proxy-candidates";
  panel.appendChild(list);
  const notes = document.createElement("div");
  notes.className = "proxy-candidate-notes";
  panel.appendChild(notes);
  paintProxyCandidateList();
  appendProxyRefused(panel);
}

/* ------------------------------------------------------- the refused list

   A fetch reports "N refused" and, until 7.35.1, showed nothing: the operator
   could see the count and not the addresses. These rows close that, and they
   are read-only by construction rather than by discipline -- there is no
   checkbox, no Add, no destination picker, and the payload they are built from
   carries none of the fields those gestures read. That is deliberate. The
   server refuses an intercepting address on every path that could add one, and
   a button here would only be a press that ends in a 422.

   The toggle is off by default and is remembered with the other filters. */
function appendProxyRefused(panel) {
  const refused = proxyRefusedAddresses();
  if (!refused.length) return;

  const box = document.createElement("div");
  box.className = "proxy-refused-panel";

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "secondary-button proxy-refused-toggle";
  const shown = Boolean(proxyState.view.showRefused);
  toggle.textContent = shown
    ? `Hide the ${refused.length} refused`
    : `Show the ${refused.length} refused`;
  toggle.setAttribute("aria-expanded", shown ? "true" : "false");
  toggle.title =
    "Addresses that answered and then presented a certificate that was not " +
    "the provider's. MCC will not route through one, and these rows are " +
    "here to be read, not chosen.";
  toggle.addEventListener("click", () => {
    proxyState.view.showRefused = !proxyState.view.showRefused;
    saveProxyCandidateView();
    renderProxyCandidates();
  });
  box.appendChild(toggle);

  if (shown) {
    const note = document.createElement("p");
    note.className = "proxy-refused-note";
    note.textContent =
      "These broke certificate validation when they were last tested. They " +
      "cannot be added to a chain -- the server refuses them -- and a later " +
      "test that succeeds is what clears one.";
    box.appendChild(note);

    const list = document.createElement("ol");
    list.className = "proxy-refused";
    refused.forEach((entry) => {
      const row = document.createElement("li");
      row.className = "proxy-refused-row";

      const label = document.createElement("span");
      label.className = "proxy-refused-label";
      label.textContent = entry.scheme
        ? `${entry.scheme}://${entry.label || entry.proxy}`
        : String(entry.label || entry.proxy);
      row.appendChild(label);

      const reason = document.createElement("span");
      reason.className = "proxy-refused-reason";
      reason.textContent =
        entry.reason ||
        "this proxy breaks certificate validation -- MCC will not route " +
          "through it";
      row.appendChild(reason);

      const when = document.createElement("span");
      when.className = "proxy-refused-when";
      const where = entry.checked_for_name ? ` against ${entry.checked_for_name}` : "";
      const ago = proxyCheckedAgo(entry.at);
      when.textContent = ago ? `Refused ${ago}${where}` : `Refused${where}`;
      row.appendChild(when);

      list.appendChild(row);
    });
    box.appendChild(list);
  }
  panel.appendChild(box);
}

/* The live progress of a fetch, and the Stop that ends it.

   `null` when nothing has run in this server process, which is every fresh
   start: a progress line reading "0 of 0" for a sweep nobody asked for is
   worse than no line. */
function proxyFetchPanel() {
  const fetch = proxyState.fetch;
  if (!fetch || fetch.state === "idle") return null;
  const box = document.createElement("div");
  box.className = "proxy-fetch-progress";
  box.setAttribute("role", "status");

  const line = document.createElement("p");
  line.className = "proxy-fetch-line";
  line.textContent = proxyFetchSentence();
  box.appendChild(line);

  if (fetch.state !== "running") return box;

  const actions = document.createElement("div");
  actions.className = "proxy-fetch-actions";
  const stop = document.createElement("button");
  stop.type = "button";
  stop.className = "secondary-button proxy-fetch-stop";
  stop.textContent = fetch.stopping ? "Stopping..." : "Stop";
  stop.disabled = Boolean(fetch.stopping);
  stop.title =
    "Stops testing the rest. Everything that has already passed stays on " +
    "offer -- stopping keeps the work, it does not throw it away.";
  stop.addEventListener("click", () => stopProxyFetch());
  actions.appendChild(stop);
  box.appendChild(actions);
  return box;
}

function proxyCandidateControls() {
  const controls = document.createElement("div");
  controls.className = "proxy-candidate-controls";

  const search = document.createElement("label");
  search.className = "proxy-candidate-control";
  const searchText = document.createElement("span");
  searchText.textContent = "Address contains";
  const searchBox = document.createElement("input");
  searchBox.type = "search";
  searchBox.className = "proxy-candidate-filter";
  searchBox.value = proxyState.view.text;
  searchBox.addEventListener("input", () => {
    proxyState.view.text = searchBox.value;
    saveProxyCandidateView();
    paintProxyCandidateList();
  });
  search.append(searchText, searchBox);
  controls.appendChild(search);

  const schemes = Array.from(
    new Set(proxyCandidates().map((candidate) => candidate.scheme).filter(Boolean)),
  ).sort();
  controls.appendChild(
    proxyCandidateSelect(
      "Scheme",
      [{ value: "", label: "any" }].concat(
        schemes.map((scheme) => ({ value: scheme, label: scheme })),
      ),
      proxyState.view.scheme,
      (value) => {
        proxyState.view.scheme = value;
      },
    ),
  );

  controls.appendChild(
    proxyCandidateSelect(
      "Feeds agreeing",
      [
        { value: "1", label: "any" },
        { value: "2", label: "2 or more" },
        { value: "3", label: "3 or more" },
        { value: "4", label: "4 or more" },
      ],
      String(proxyState.view.minSources || 1),
      (value) => {
        proxyState.view.minSources = Number(value) || 1;
      },
    ),
  );

  controls.appendChild(
    proxyCandidateSelect(
      "Sort by",
      [
        { value: "sources", label: "feeds agreeing" },
        { value: "latency", label: "latency the feed published" },
        { value: "address", label: "address" },
        { value: "scheme", label: "scheme" },
      ],
      proxyState.view.sort,
      (value) => {
        proxyState.view.sort = value;
      },
    ),
  );
  return controls;
}

function proxyCandidateSelect(text, options, value, apply) {
  const label = document.createElement("label");
  label.className = "proxy-candidate-control";
  const name = document.createElement("span");
  name.textContent = text;
  const select = document.createElement("select");
  select.className = "proxy-candidate-provider";
  options.forEach((option) => {
    const node = document.createElement("option");
    node.value = option.value;
    node.textContent = option.label;
    select.appendChild(node);
  });
  select.value = value;
  select.addEventListener("change", () => {
    apply(select.value);
    saveProxyCandidateView();
    paintProxyCandidateList();
  });
  label.append(name, select);
  return label;
}

/* The bar, the rows and the notes. Everything that depends on the filter or on
   the selection is painted here, and the controls above are not touched. */
function paintProxyCandidateList() {
  const panel = byId("proxyingCandidates");
  if (!panel) return;
  const bar = panel.querySelector(".proxy-candidate-bar");
  const list = panel.querySelector(".proxy-candidates");
  const notes = panel.querySelector(".proxy-candidate-notes");
  if (!bar || !list || !notes) return;
  const shown = proxyFilteredCandidates();
  const page = Math.max(PROXY_CANDIDATE_RENDER_CAP, proxyState.drawn || 0);
  const drawn = shown.slice(0, page);

  list.textContent = "";
  list.appendChild(proxyCandidateHead(shown, drawn));
  drawn.forEach((candidate) => list.appendChild(proxyCandidateRow(candidate)));

  notes.textContent = "";
  if (shown.length > drawn.length) {
    /* Paged, not truncated. Every row here is an address that was working when
       it was tested, so "narrow the filter to see the rest" would be hiding
       usable work behind a search box. The button is the way through the list;
       the filter is still there for finding one address in it. */
    const more = document.createElement("p");
    more.className = "field-description proxy-candidate-more";
    more.textContent =
      `Showing ${drawn.length} of ${shown.length} matching addresses. ` +
      "Select all still means every address the filter matches, not only the " +
      "ones drawn.";
    notes.appendChild(more);
    const showMore = document.createElement("button");
    showMore.type = "button";
    showMore.className = "ghost-button proxy-candidate-more-button";
    const next = Math.min(
      PROXY_CANDIDATE_RENDER_CAP,
      shown.length - drawn.length,
    );
    showMore.textContent = `Show ${next} more`;
    showMore.addEventListener("click", () => {
      proxyState.drawn = drawn.length + PROXY_CANDIDATE_RENDER_CAP;
      paintProxyCandidateList();
    });
    notes.appendChild(showMore);
  }
  const ineligible = proxyIneligibleProviders();
  if (ineligible.length) {
    const why = document.createElement("p");
    why.className = "field-description proxy-candidate-note";
    why.textContent =
      `${ineligible.map((provider) => provider.display_name).join(", ")} ` +
      `${ineligible.length === 1 ? "is" : "are"} not offered as a ` +
      "destination: an address is verified by tunnelling through it to the " +
      "provider's own host and checking that host's certificate, and this one " +
      "has no https base URL to verify against. Set its base URL on the " +
      "Providers page.";
    notes.appendChild(why);
  }
  paintProxyCandidateBar(bar, shown);
}

/* The header row: one control that selects everything the filter matches. */
function proxyCandidateHead(shown, drawn) {
  const head = document.createElement("li");
  head.className = "proxy-candidate proxy-candidate-head";
  const label = document.createElement("label");
  label.className = "proxy-candidate-control";
  const box = document.createElement("input");
  box.type = "checkbox";
  box.className = "proxy-candidate-select-all";
  const picked = shown.filter((candidate) =>
    proxyState.selected.has(candidate.proxy),
  ).length;
  box.checked = shown.length > 0 && picked === shown.length;
  box.indeterminate = picked > 0 && picked < shown.length;
  box.addEventListener("change", () => {
    setProxyCandidateSelection(
      shown.map((candidate) => candidate.proxy),
      box.checked,
    );
  });
  const text = document.createElement("span");
  text.textContent =
    shown.length === proxyCandidates().length
      ? `Select all ${shown.length} on offer`
      : `Select all ${shown.length} matching this filter`;
  label.append(box, text);
  head.appendChild(label);
  const counted = document.createElement("span");
  counted.className = "proxy-candidate-facts";
  counted.textContent =
    drawn.length === shown.length
      ? `${shown.length} shown`
      : `${drawn.length} of ${shown.length} drawn`;
  head.appendChild(counted);
  return head;
}

/* One destination for the whole selection, one press, and the two facts a
   press needs before it is made: how many addresses it will touch, and how
   many of them can actually fit in that chain. */
function paintProxyCandidateBar(bar, shown) {
  bar.textContent = "";
  const selected = proxySelectedIds();
  const providers = proxyCandidateProviders();
  const destination = proxyDestination();

  const count = document.createElement("span");
  count.className = "proxy-candidate-count";
  count.textContent = selected.length
    ? `${selected.length} selected of ${shown.length} shown`
    : `${shown.length} shown, none selected`;
  bar.appendChild(count);

  if (!providers.length) {
    const none = document.createElement("span");
    none.className = "field-description";
    none.textContent =
      "No provider on this install has an https base URL to test an address " +
      "against, so nothing here can be added yet.";
    bar.appendChild(none);
    return;
  }

  const pick = document.createElement("label");
  pick.className = "proxy-candidate-control proxy-candidate-destination";
  const pickText = document.createElement("span");
  pickText.textContent = "Add to";
  const select = document.createElement("select");
  select.className = "proxy-candidate-provider";
  select.setAttribute("aria-label", "Which provider's chain to add the selection to");
  providers.forEach((provider) => {
    const option = document.createElement("option");
    option.value = provider.provider_id;
    option.textContent = provider.display_name;
    select.appendChild(option);
  });
  if (destination) select.value = destination.provider_id;
  select.addEventListener("change", () => {
    proxyState.view.destination = select.value;
    saveProxyCandidateView();
    paintProxyCandidateList();
  });
  pick.append(pickText, select);
  bar.appendChild(pick);

  const room = proxyChainRoom(destination);
  const fits = Math.min(selected.length, room.room);
  const running = Boolean(proxyState.run);

  const add = document.createElement("button");
  add.type = "button";
  add.className = "primary-button proxy-candidate-bulk-button";
  const confirming = proxyState.confirming === "add";
  add.textContent = confirming
    ? `Test and add ${fits} -- press again to confirm`
    : `Test and add ${selected.length || 0} selected`;
  add.disabled = running || !selected.length || room.room === 0;
  add.addEventListener("click", () =>
    runProxyCandidateBulk({
      action: "add",
      providerId: destination ? destination.provider_id : "",
      proxies: selected.slice(0, room.room),
    }),
  );
  bar.appendChild(add);

  const discard = document.createElement("button");
  discard.type = "button";
  discard.className = "secondary-button proxy-candidate-bulk-button";
  discard.textContent =
    proxyState.confirming === "discard"
      ? `Discard ${selected.length} -- press again to confirm`
      : `Discard ${selected.length || 0} selected`;
  discard.disabled = running || !selected.length;
  discard.addEventListener("click", () =>
    runProxyCandidateBulk({ action: "discard", proxies: selected }),
  );
  bar.appendChild(discard);

  /* One press to use what the fetch found.

     Selecting four hundred rows to add four hundred addresses that a fetch has
     just proved all work is a gesture with no decision in it, so the page
     offers to make it. It is the SAME write path -- `runProxyCandidateBulk`
     with the working addresses in it -- because a second one is how 6.24.0
     happened, and it re-tests every address exactly as a hand-picked add does:
     the check is against one provider's host, this button may be pointing at a
     different provider, and skipping the one gate standing between a stranger's
     machine and a credential to save a few seconds is not a trade worth
     making. */
  const working = shown.filter((candidate) => candidate.working);
  if (working.length) {
    const all = document.createElement("button");
    all.type = "button";
    all.className = "primary-button proxy-candidate-bulk-button proxy-candidate-all";
    const fitAll = Math.min(working.length, room.room);
    all.textContent =
      proxyState.confirming === "all"
        ? `Add all ${fitAll} working -- press again to confirm`
        : `Add all ${working.length} working to ${
            destination ? destination.display_name : "this provider"
          }`;
    all.disabled = running || !destination || room.room === 0;
    all.title =
      "Every address below passed its test a moment ago. Adding them tests " +
      "each one again against this provider's own host -- a verdict is about " +
      "one destination, and this may not be the one they were tested for.";
    all.addEventListener("click", () => {
      // Selecting them first is what makes the progress, the per-row outcomes
      // and the Undo read exactly as they do for a hand-made selection.
      setProxyCandidateSelection(
        working.map((candidate) => candidate.proxy),
        true,
      );
      runProxyCandidateBulk({
        action: "add",
        // Its own confirm key, so the inline confirm on this button cannot be
        // satisfied by a second press of the one beside it.
        confirmAs: "all",
        providerId: destination ? destination.provider_id : "",
        proxies: working.map((candidate) => candidate.proxy).slice(0, room.room),
      });
    });
    bar.appendChild(all);
  }

  const clear = document.createElement("button");
  clear.type = "button";
  clear.className = "ghost-button proxy-candidate-bulk-button";
  clear.textContent = "Clear selection";
  clear.disabled = running || !selected.length;
  clear.addEventListener("click", () => clearProxyCandidateSelection());
  bar.appendChild(clear);

  const capacity = document.createElement("p");
  capacity.className = "field-description proxy-candidate-capacity";
  // With no cap set -- what ships -- there is no "of N" to report and no
  // overflow to warn about, so the line says what it knows and stops. Printing
  // Number.MAX_SAFE_INTEGER here would be worse than saying nothing.
  const over = room.max ? selected.length - room.room : 0;
  capacity.textContent = !destination
    ? ""
    : room.max
      ? `${destination.display_name} has ${room.used} of ${room.max} entries, ` +
        `so ${room.room} more will fit.` +
        (over > 0
          ? ` ${over} of the selected addresses will not be added this press.`
          : "")
      : `${destination.display_name} has ${room.used} entries, and you have ` +
        "set no limit on how many it may hold.";
  bar.appendChild(capacity);

  if (running) {
    const progress = document.createElement("p");
    progress.className = "proxy-candidate-progress";
    // The batch in flight is counted as being tested, not as still to come: a
    // counter that reads "0 of 7" for the whole of a seven-address batch is a
    // progress bar that never moves.
    const first = proxyState.run.done + 1;
    const last = Math.min(
      proxyState.run.total,
      proxyState.run.done + (proxyState.run.inflight || 1),
    );
    progress.textContent =
      `Testing ${first === last ? first : `${first}-${last}`} of ` +
      `${proxyState.run.total} addresses. ` +
      "Each one opens an HTTPS request through that machine to the " +
      "provider's own host; the page stays usable while it runs.";
    bar.appendChild(progress);
    const stop = document.createElement("button");
    stop.type = "button";
    stop.className = "secondary-button proxy-candidate-bulk-button proxy-candidate-stop";
    stop.textContent = "Stop";
    stop.disabled = proxyState.run.stop;
    stop.addEventListener("click", () => {
      if (proxyState.run) proxyState.run.stop = true;
      paintProxyCandidateList();
    });
    bar.appendChild(stop);
  }
}

/* ------------------------------------------------------- the selection
   Held in `proxyState.selected`, never read back out of the DOM: this page
   re-renders after every batch of a bulk add, and a selection living in the
   checkboxes would be thrown away by its own progress. */
function setProxyCandidateSelection(proxies, on) {
  proxies.forEach((proxy) => {
    if (on) proxyState.selected.add(proxy);
    else proxyState.selected.delete(proxy);
  });
  saveProxyCandidateView();
  syncProxyCandidateSelectionUi();
}

function clearProxyCandidateSelection() {
  proxyState.selected.clear();
  proxyState.anchor = null;
  proxyState.arrowRange = [];
  saveProxyCandidateView();
  syncProxyCandidateSelectionUi();
}

/* Walks the rendered rows only, so its cost tracks what is on screen rather
   than the 1,572 addresses the payload can hold. */
function syncProxyCandidateSelectionUi() {
  const panel = byId("proxyingCandidates");
  if (!panel) return;
  panel.querySelectorAll(".proxy-candidate[data-proxy]").forEach((row) => {
    const on = proxyState.selected.has(row.dataset.proxy);
    const box = row.querySelector("input.proxy-candidate-select");
    if (box) box.checked = on;
    row.classList.toggle("is-selected", on);
  });
  const shown = proxyFilteredCandidates();
  const all = panel.querySelector("input.proxy-candidate-select-all");
  if (all) {
    const picked = shown.filter((candidate) =>
      proxyState.selected.has(candidate.proxy),
    ).length;
    all.checked = shown.length > 0 && picked === shown.length;
    all.indeterminate = picked > 0 && picked < shown.length;
  }
  const bar = panel.querySelector(".proxy-candidate-bar");
  if (bar) paintProxyCandidateBar(bar, shown);
}

/* Escape belongs to whichever modal is open; only when none is does it mean
   "drop this selection". The same three modals the Models page and the route
   rails check, because they are the dashboard's only modal surfaces. */
function proxyModalIsOpen() {
  return ["webSearchDetailModal", "exportModal", "reqDetailModal"]
    .map(byId)
    .some((modal) => modal && !modal.hidden);
}

/* Escape anywhere on the Proxying page, not only on a checkbox: a selection is
   dropped from wherever the operator's focus happens to be. */
function initProxyingSelection() {
  document.addEventListener("keydown", (event) => {
    const view = byId("view-proxying");
    if (!view || view.hidden) return;
    if (event.key !== "Escape") return;
    if (proxyModalIsOpen()) return;
    if (!proxyState.selected.size) return;
    clearProxyCandidateSelection();
    announceProxy("Selection cleared.");
  });
}

initProxyingSelection();

/* The rendered ids, in the order they are drawn. A row the filter left out is
   not part of a visual range, so a range is read from what can be seen. */
function proxyRenderedIds() {
  const panel = byId("proxyingCandidates");
  if (!panel) return [];
  return Array.from(panel.querySelectorAll(".proxy-candidate[data-proxy]")).map(
    (row) => row.dataset.proxy,
  );
}

function proxyRangeIds(fromProxy, toProxy) {
  const ids = proxyRenderedIds();
  const start = ids.indexOf(fromProxy);
  const end = ids.indexOf(toProxy);
  if (start < 0 || end < 0) return [toProxy];
  return ids.slice(Math.min(start, end), Math.max(start, end) + 1);
}

function onProxyCandidateClick(proxy, box, event) {
  const on = box.checked;
  if (event.shiftKey && proxyState.anchor) {
    setProxyCandidateSelection(proxyRangeIds(proxyState.anchor, proxy), on);
  } else {
    setProxyCandidateSelection([proxy], on);
    proxyState.anchor = proxy;
  }
  proxyState.arrowRange = [];
}

/* Range selection must not be pointer-only: WCAG 2.2 asks for a keyboard
   alternative to any author-controlled drag or range gesture, so the same
   range is reachable with Shift+Space and Shift+Arrow. 6.7.0 records this as a
   requirement rather than a nicety, and it is copied here unchanged. */
function onProxyCandidateKeydown(proxy, box, event) {
  if (event.key === " " && event.shiftKey) {
    event.preventDefault();
    const on = !box.checked;
    setProxyCandidateSelection(proxyRangeIds(proxyState.anchor || proxy, proxy), on);
    proxyState.arrowRange = [];
    return;
  }
  if (event.key === "Escape") {
    if (proxyModalIsOpen()) return;
    event.preventDefault();
    clearProxyCandidateSelection();
    announceProxy("Selection cleared.");
    return;
  }
  if (!event.shiftKey) return;
  if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
  event.preventDefault();
  const ids = proxyRenderedIds();
  const here = ids.indexOf(proxy);
  const next = ids[here + (event.key === "ArrowDown" ? 1 : -1)];
  if (!next) return;
  if (!proxyState.anchor) proxyState.anchor = proxy;
  const wanted = proxyRangeIds(proxyState.anchor, next);
  // Walking back towards the anchor shrinks the range rather than leaving the
  // rows behind the cursor selected.
  setProxyCandidateSelection(
    proxyState.arrowRange.filter((id) => !wanted.includes(id)),
    false,
  );
  setProxyCandidateSelection(wanted, true);
  proxyState.arrowRange = wanted;
  const panel = byId("proxyingCandidates");
  const nextRow =
    panel &&
    Array.from(panel.querySelectorAll(".proxy-candidate[data-proxy]")).find(
      (row) => row.dataset.proxy === next,
    );
  const nextBox = nextRow && nextRow.querySelector("input.proxy-candidate-select");
  if (nextBox) nextBox.focus();
}

/* What the last bulk action did to this address, in the words the summary uses
   for the same group. One vocabulary, so a row and the panel above it cannot
   describe the same outcome differently. */
const PROXY_OUTCOME_WORDS = {
  added: "added, verified",
  benched: "added, benched -- no answer",
  refused: "refused -- TLS intercepted",
  already: "already in that chain",
  full: "not added -- chain full",
  gone: "no longer on offer",
  discarded: "discarded",
};

function proxyCandidateRow(candidate) {
  const row = document.createElement("li");
  row.className = candidate.refused
    ? "proxy-candidate proxy-candidate-refused"
    : "proxy-candidate";
  row.dataset.proxy = candidate.proxy;
  if (proxyState.selected.has(candidate.proxy)) row.classList.add("is-selected");

  const select = document.createElement("input");
  select.type = "checkbox";
  select.className = "proxy-candidate-select";
  select.checked = proxyState.selected.has(candidate.proxy);
  select.setAttribute("aria-label", `Select ${candidate.label}`);
  select.addEventListener("click", (event) =>
    onProxyCandidateClick(candidate.proxy, select, event),
  );
  select.addEventListener("keydown", (event) =>
    onProxyCandidateKeydown(candidate.proxy, select, event),
  );
  row.appendChild(select);

  const label = document.createElement("span");
  label.className = "proxy-candidate-label";
  label.textContent = candidate.label;

  const scheme = document.createElement("span");
  scheme.className = "proxy-candidate-scheme";
  scheme.textContent = candidate.scheme || "";

  const facts = document.createElement("span");
  facts.className = "proxy-candidate-facts";
  const bits = [];
  if (candidate.country) bits.push(candidate.country);
  if (candidate.anonymity) bits.push(candidate.anonymity);
  if (candidate.https_ok) bits.push("https");
  if (candidate.latency_ms !== null && candidate.latency_ms !== undefined) {
    bits.push(`${candidate.latency_ms} ms`);
  }
  if (candidate.uptime_pct !== null && candidate.uptime_pct !== undefined) {
    bits.push(`${candidate.uptime_pct}% up`);
  }
  facts.textContent = bits.join(" · ");
  facts.title =
    "What the feeds published about this address. None of it was measured " +
    "by MCC -- the check beside it is.";

  /* What MCC itself found, which since 7.21.0 is the reason this row is here
     at all: a fetch tests every address the lists offered and keeps only the
     ones that passed. The destination is named because a verdict is about one
     host -- "working" with no "for whom" would be a claim about providers
     nobody measured. */
  const measured = document.createElement("span");
  const check = candidate.last_check || null;
  if (candidate.untested) {
    /* A candidate stored by 7.18-7.20, which offered addresses without testing
       them. It is shown rather than deleted -- throwing away somebody's stored
       list on an upgrade is not a migration anyone asked for -- and it is
       never counted as working. The next fetch replaces the offer list, so it
       clears itself the first time the button is pressed. */
    measured.className = "proxy-candidate-measured proxy-candidate-untested";
    measured.textContent = "not tested -- fetch again";
    measured.title =
      "This address was offered by a release that did not test what it " +
      "found. Press Fetch to replace this list with addresses that have " +
      "been measured, or add it and it will be tested then.";
  } else if (candidate.working) {
    measured.className = "proxy-candidate-measured proxy-candidate-working";
    const latency =
      check && check.latency_ms !== null && check.latency_ms !== undefined
        ? ` · ${check.latency_ms} ms`
        : "";
    /* How it was proven, not only that it was. A verified handshake and an
       answered request are both passes and both catch an intercepting proxy,
       but they are not the same evidence, and a row that said only "working"
       would leave the operator to guess which one this was. A record written
       before 7.22.2 carries no depth and was always the request. */
    const provenBy =
      check && check.depth === "tls"
        ? "tunnel + certificate verified"
        : "HTTPS request answered";
    measured.textContent = candidate.checked_for_name
      ? `working for ${candidate.checked_for_name}${latency} · ${provenBy}`
      : `working${latency} · ${provenBy}`;
    measured.title =
      (check && check.depth === "tls"
        ? "MCC opened a tunnel through this address and completed a full TLS " +
          "handshake to " +
          (candidate.checked_for_name || "that provider") +
          "'s own host through it: the certificate verified against this " +
          "machine's own trust store and the hostname matched. No request was " +
          "sent. "
        : "MCC opened a tunnel through this address and an HTTPS request to " +
          (candidate.checked_for_name || "that provider") +
          "'s own host came back with that host's certificate verifying. ") +
      (check && check.exit_ip ? `It answered from ${check.exit_ip}. ` : "") +
      "Adding it to a chain tests it again with a real request, and adding it " +
      "to a different provider tests it against that one.";
  } else {
    measured.className = "proxy-candidate-measured proxy-candidate-untested";
    measured.textContent = "tested, did not pass";
    measured.title =
      (check && check.detail) ||
      "The last check of this address did not succeed.";
  }

  // The one signal that is evidence rather than a copied claim, and it names
  // the feeds rather than only counting them.
  const sources = document.createElement("span");
  const names = (candidate.sources || []).map((item) => item.name);
  sources.className =
    names.length > 1
      ? "proxy-candidate-sources proxy-candidate-agreed"
      : "proxy-candidate-sources";
  sources.textContent =
    names.length > 1 ? `${names.length} feeds agree` : names.join("") || "1 feed";
  sources.title = names.length
    ? `Listed by: ${names.join(", ")}.`
    : "No feed recorded for this address.";

  const actions = document.createElement("div");
  actions.className = "proxy-candidate-actions";

  // What the last bulk action did to this row, kept on the row rather than
  // only in the summary: a partial result is read address by address, and a
  // panel that has been dismissed still leaves that question open.
  const outcome = proxyState.outcomes.get(candidate.proxy);
  // Except when the standing badge below already says it in the same words: a
  // refusal is both what this press found and what the row is from now on, and
  // printing it twice side by side reads as two different findings.
  const duplicated = candidate.refused && outcome && outcome.outcome === "refused";
  if (outcome && !duplicated && PROXY_OUTCOME_WORDS[outcome.outcome]) {
    const state = document.createElement("span");
    state.className = `proxy-candidate-outcome proxy-candidate-outcome-${outcome.outcome}`;
    state.textContent = PROXY_OUTCOME_WORDS[outcome.outcome];
    if (outcome.detail) state.title = outcome.detail;
    actions.appendChild(state);
  }

  if (candidate.refused) {
    const refused = document.createElement("span");
    refused.className = "proxy-entry-state proxy-entry-state-intercepted";
    refused.textContent = "TLS intercepted";
    refused.title =
      (candidate.last_check && candidate.last_check.detail) ||
      "This address breaks certificate validation and cannot be added.";
    actions.appendChild(refused);
  } else {
    const destination = proxyDestination();
    if (destination) {
      // The one-element form of the bulk action, never a second write path:
      // the row's press and the bar's press reach the same function, the same
      // route and the same repaint.
      const add = document.createElement("button");
      add.type = "button";
      add.className = "ghost-button proxy-candidate-button";
      // The destination is named once, in the bar, not thirty-nine times down
      // the right-hand edge: the provider name is long, and repeating it on
      // every row is the visual noise the per-row picker was.
      add.textContent = "Add";
      add.title = `Test and add ${candidate.label} to ${destination.display_name}`;
      add.setAttribute(
        "aria-label",
        `Test and add ${candidate.label} to ${destination.display_name}`,
      );
      add.disabled = Boolean(proxyState.run);
      add.addEventListener("click", () =>
        runProxyCandidateBulk({
          action: "add",
          providerId: destination.provider_id,
          proxies: [candidate.proxy],
        }),
      );
      actions.appendChild(add);
    }
  }

  const discard = document.createElement("button");
  discard.type = "button";
  discard.className = "ghost-button proxy-candidate-button";
  discard.textContent = "Discard";
  discard.title =
    "Stops this address being offered. It is not in any chain, so nothing " +
    "stops routing; a later fetch may offer it again.";
  discard.disabled = Boolean(proxyState.run);
  discard.addEventListener("click", () =>
    runProxyCandidateBulk({ action: "discard", proxies: [candidate.proxy] }),
  );
  actions.appendChild(discard);

  row.append(label, scheme, measured, facts, sources, actions);
  return row;
}

/* --------------------------------------------------------- the write path

   The **one** write path for a candidate, for one address and for four
   hundred. 6.24.0 exists because the Models page kept a second, single-row
   path beside its bulk one and the two drifted apart until the single-row one
   silently skipped the counters; a single-item action here is this function
   with one element in `proxies`.

   Sent in batches, because each address is a TLS handshake through a
   stranger's machine with a ten-second ceiling: one request for a long
   selection is a progress bar that cannot move and a gesture that cannot be
   stopped. Every batch carries the undo token the first one minted, so Undo
   means "before I pressed Add", not "before the last ten of them". */
async function runProxyCandidateBulk(request) {
  const action = request.action;
  // Which button is asking, for the inline confirm only. The route sees
  // `action` and nothing else: "Add all working" is the add path with a longer
  // list, not a third thing the server has to know about.
  const asking = request.confirmAs || action;
  const proxies = request.proxies || [];
  if (proxyState.run) return;
  if (!proxies.length) {
    announceProxy(
      action === "add"
        ? "Select at least one address first, then choose where it goes."
        : "Select at least one address first.",
    );
    return;
  }
  if (action === "add" && !request.providerId) {
    announceProxy(
      "No provider on this install has an https base URL to test an address " +
        "against, so there is nowhere to add these yet.",
    );
    return;
  }
  // No modal: this is reversible through the panel's own Undo, and a dialog on
  // every press is the friction being removed. One inline confirm for a
  // gesture large enough to be a slip.
  if (
    proxies.length >= PROXY_CANDIDATE_CONFIRM_AT &&
    proxyState.confirming !== asking
  ) {
    proxyState.confirming = asking;
    paintProxyCandidateList();
    window.setTimeout(() => {
      if (proxyState.confirming !== asking) return;
      proxyState.confirming = "";
      paintProxyCandidateList();
    }, 5000);
    return;
  }
  proxyState.confirming = "";
  proxyState.outcomes = new Map();
  proxyState.run = { action, total: proxies.length, done: 0, stop: false };
  paintProxyCandidateList();

  let token = "";
  let stopped = false;
  // Whether a batch has landed addresses that nothing has rebuilt the provider
  // generation for yet. Every batch but the last asks the server NOT to
  // republish -- a generation replace per ten addresses is thirty replaces for
  // three hundred, each one a sweep the event loop pays for while the operator
  // is still waiting -- so a run that is stopped part-way owes one republish.
  let owesRepublish = false;
  try {
    for (let index = 0; index < proxies.length; index += PROXY_CANDIDATE_BATCH) {
      if (proxyState.run.stop) {
        stopped = true;
        break;
      }
      const batch = proxies.slice(index, index + PROXY_CANDIDATE_BATCH);
      const isLastBatch = index + PROXY_CANDIDATE_BATCH >= proxies.length;
      proxyState.run.inflight = batch.length;
      paintProxyCandidateList();
      const payload = await api("/admin/api/proxy-chains/candidates/bulk", {
        method: "POST",
        body: JSON.stringify({
          action,
          provider: request.providerId || "",
          proxies: batch,
          undo_token: token,
          republish: isLastBatch,
        }),
      });
      owesRepublish = !isLastBatch;
      const bulk = payload.bulk || {};
      token = bulk.undo_token || token;
      (bulk.results || []).forEach((row) =>
        proxyState.outcomes.set(row.proxy, row),
      );
      proxyState.run.done = Math.min(proxies.length, index + batch.length);
      applyProxyCandidateBulk(payload, request.providerId);
    }
  } catch (error) {
    proxyState.run = null;
    renderProxying();
    announceProxy(error.message);
    showMessage(error.message, "error");
    return;
  }
  // A stopped run still has to start routing through what it did add. One
  // call, no store write, and a failure here is not a failure of the add:
  // the addresses are saved either way and a restart would pick them up.
  if (owesRepublish) {
    try {
      const republished = await api("/admin/api/proxy-chains/republish", {
        method: "POST",
        body: JSON.stringify({}),
      });
      applyProxyCandidateBulk(republished, request.providerId);
    } catch (error) {
      showMessage(error.message, "error");
    }
  }
  proxyState.run = null;
  renderProxying();
  announceProxyBulk(action, request.providerId, token, stopped);
}

/* The one repaint. The route answers with the whole refreshed page state, so
   the page is repainted from the server's word rather than from a guess about
   what the write did -- and the card that gained entries drops its draft, or
   it would keep showing a chain from before the addresses landed in it. */
function applyProxyCandidateBulk(payload, providerId) {
  proxyState.data = payload;
  if (providerId) proxyState.drafts.delete(providerId);
  rememberSavedFeeds();
  renderProxying();
}

/* What a bulk action did, as a summary a person can act on. A partial result
   is the NORMAL outcome here -- these are strangers' machines read from public
   lists -- so it is reported as a set of groups, never as one opaque "done"
   and never as an error because three of twelve did not answer. */
function announceProxyBulk(action, providerId, token, stopped) {
  const rows = Array.from(proxyState.outcomes.values());
  const counts = {};
  rows.forEach((row) => {
    counts[row.outcome] = (counts[row.outcome] || 0) + 1;
  });
  const provider = ((proxyState.data && proxyState.data.providers) || []).find(
    (entry) => entry.provider_id === providerId,
  );
  const where = provider ? provider.display_name : "that provider";
  const lines = [];
  let lead = "";

  if (action === "discard") {
    lead =
      `${counts.discarded || 0} address(es) are no longer offered.` +
      (counts.gone ? ` ${counts.gone} had already gone.` : "") +
      " No chain was touched: a discarded address that is already an entry " +
      "somewhere keeps routing exactly as it did.";
  } else {
    const landed = (counts.added || 0) + (counts.benched || 0);
    lead =
      `${landed} of ${rows.length} address(es) went into ${where}'s chain. ` +
      "That chain is still off until you enable it.";
    if (counts.added) {
      lines.push(
        `${counts.added} answered and the destination's certificate verified ` +
          "through the tunnel.",
      );
    }
    if (counts.benched) {
      lines.push(
        `${counts.benched} did not answer, so they were added benched and ` +
          "the chain routes around them until they do. A free address that " +
          "is down right now is an ordinary thing, not a failure of this " +
          "press.",
      );
    }
    if (counts.refused) {
      lines.push(
        `${counts.refused} break certificate validation and were refused: ` +
          "their tunnels presented certificates this machine does not trust, " +
          "which means they are reading the traffic rather than relaying it. " +
          "They stay on offer, marked, and no credential went near them.",
      );
    }
    if (counts.already) {
      lines.push(`${counts.already} were already entries of that chain.`);
    }
    if (counts.full) {
      lines.push(
        `${counts.full} did not fit: you have set PROXY_CHAIN_MAX_ENTRIES to ` +
          `${proxyEntryCap()}, so a chain holds at most that many entries.`,
      );
    }
    if (counts.gone) {
      lines.push(
        `${counts.gone} were no longer on offer -- a later fetch had already ` +
          "dropped them.",
      );
    }
  }
  if (stopped) {
    lines.push(
      "Stopped before the rest of the selection. What had already been tested " +
        "is reported above and is on disk; the rest is untouched and still " +
        "selected.",
    );
  }
  announceProxy(lead, {
    lines,
    undoLabel: action === "discard" ? "Undo the discard" : "Undo the add",
    undo: token ? () => undoProxyCandidateBulk(token) : undefined,
  });
}

/* One level of undo, through the token the write handed back. The store is put
   back as it was, or the page says why it was not -- a snapshot restore across
   somebody else's edit would quietly delete that edit. */
async function undoProxyCandidateBulk(token) {
  try {
    const payload = await api("/admin/api/proxy-chains/candidates/undo", {
      method: "POST",
      body: JSON.stringify({ token }),
    });
    proxyState.data = payload;
    proxyState.drafts.clear();
    proxyState.outcomes = new Map();
    rememberSavedFeeds();
    renderProxying();
    announceProxy(
      "Put back as it was before that action. Any address that was refused " +
        "keeps its verdict: that was a measurement, not a change this undoes.",
    );
  } catch (error) {
    announceProxy(error.message);
    showMessage(error.message, "error");
  }
}

function renderProxying() {
  const list = byId("proxyingList");
  const empty = byId("proxyingEmpty");
  renderProxyCheckerNote();
  renderProxyFeeds();
  renderProxyCandidates();
  if (!list) return;
  list.textContent = "";
  const providers = (proxyState.data && proxyState.data.providers) || [];
  if (empty) empty.hidden = providers.length > 0;
  providers.forEach((provider) => list.appendChild(proxyCard(provider)));
}

function proxyCard(provider) {
  const draft = proxyDraft(provider);
  const card = document.createElement("article");
  card.className = "proxy-card";
  card.dataset.provider = provider.provider_id;

  card.appendChild(proxyCardHead(provider, draft));
  card.appendChild(proxyPolicyHelp(draft));
  if (provider.oauth) card.appendChild(proxyOauthNote(provider, draft));
  card.appendChild(proxyInheritedNote(provider, draft));
  card.appendChild(proxyTriggers(provider, draft));
  card.appendChild(proxyEntryList(provider, draft));
  card.appendChild(proxyAddRow(provider, draft));
  card.appendChild(proxyCardFoot(provider, draft));
  return card;
}

function proxyCardHead(provider, draft) {
  // A plain flex row, deliberately not a grid: an explicit-column grid row
  // wraps the moment a card grows a control, and 6.21.0 shipped exactly that
  // with 462 green tests behind it.
  const head = document.createElement("div");
  head.className = "proxy-card-head";

  const title = document.createElement("div");
  title.className = "proxy-card-title";
  const name = document.createElement("h4");
  name.className = "proxy-card-name";
  name.textContent = provider.display_name;
  const id = document.createElement("code");
  id.className = "proxy-card-id";
  id.textContent = provider.provider_id;
  title.append(name, id);

  const controls = document.createElement("div");
  controls.className = "proxy-card-controls";

  const policyLabel = document.createElement("label");
  policyLabel.className = "proxy-control";
  const policyText = document.createElement("span");
  policyText.textContent = "Rotation";
  const policy = document.createElement("select");
  policy.className = "proxy-policy";
  (proxyVocabulary().policies || []).forEach((entry) => {
    const option = document.createElement("option");
    option.value = entry.id;
    option.textContent = entry.id;
    policy.appendChild(option);
  });
  policy.value = draft.policy;
  policy.addEventListener("change", () => {
    draft.policy = policy.value;
    renderProxying();
  });
  policyLabel.append(policyText, policy);

  const enableLabel = document.createElement("label");
  enableLabel.className = "proxy-control";
  const enable = document.createElement("input");
  enable.type = "checkbox";
  enable.className = "proxy-enable";
  enable.checked = draft.enabled;
  enable.addEventListener("change", () => {
    draft.enabled = enable.checked;
  });
  const enableText = document.createElement("span");
  enableText.textContent = "Enabled";
  enableLabel.append(enable, enableText);

  controls.append(policyLabel, enableLabel);
  head.append(title, controls);
  return head;
}

function proxyPolicyHelp(draft) {
  // The practical difference between failover and round_robin is the whole
  // point of the feature for a provider metered by address, and it is the one
  // thing four policy names do not say on their own. It sits under the select
  // and changes with it rather than in a help modal nobody opens.
  const help = document.createElement("p");
  help.className = "proxy-policy-help";
  const entry = (proxyVocabulary().policies || []).find(
    (item) => item.id === draft.policy,
  );
  help.textContent = entry ? entry.help : "";
  return help;
}

function proxyOauthNote(provider, draft) {
  const note = document.createElement("div");
  note.className = "proxy-oauth-note";
  const text = document.createElement("p");
  const lead = document.createElement("strong");
  lead.textContent = "Subscription login. ";
  text.append(lead);
  text.append(
    document.createTextNode(
      `${provider.display_name} uses your personal subscription rather than an ` +
        "API key. Changing source IP between requests is more likely to be " +
        "flagged here than on a pay-as-you-go key.",
    ),
  );
  note.appendChild(text);

  const label = document.createElement("label");
  label.className = "proxy-control";
  const box = document.createElement("input");
  box.type = "checkbox";
  box.className = "proxy-oauth-ack";
  box.checked = draft.oauth_acknowledged;
  box.addEventListener("change", () => {
    draft.oauth_acknowledged = box.checked;
    renderProxying();
  });
  const boxText = document.createElement("span");
  boxText.textContent = "I understand, give this provider a chain";
  label.append(box, boxText);
  note.appendChild(label);
  return note;
}

function proxyInheritedNote(provider, draft) {
  const note = document.createElement("p");
  note.className = "proxy-inherited";
  if (draft.entries.length) {
    note.textContent = provider.env_var
      ? `This chain replaces ${provider.env_var} while it has entries. ` +
        `${provider.env_var} keeps its value and is never rewritten.`
      : "This chain replaces this provider's stored proxy while it has entries.";
    return note;
  }
  if (provider.inherited_label) {
    note.textContent = provider.env_var
      ? `Inherited from ${provider.env_var}: ${provider.inherited_label}` +
        `${provider.inherited_scheme ? ` (${provider.inherited_scheme})` : ""}. ` +
        "Add a proxy to turn it into a chain; the first entry is that address."
      : `Inherited from this provider's stored proxy: ${provider.inherited_label}.`;
    return note;
  }
  note.textContent =
    "No proxy configured. Requests go out on this machine's own address.";
  return note;
}

function proxyTriggers(provider, draft) {
  const block = document.createElement("div");
  block.className = "proxy-triggers";

  const intro = document.createElement("p");
  intro.className = "field-description";
  intro.textContent = "Switch when the provider says:";
  block.appendChild(intro);

  const row = document.createElement("div");
  row.className = "proxy-chip-row";
  (proxyVocabulary().kinds || []).forEach((kind) => {
    row.appendChild(proxyChip(kind, draft));
  });
  block.appendChild(row);

  const reset = document.createElement("button");
  reset.type = "button";
  reset.className = "ghost-button proxy-chip-reset";
  reset.textContent = "Recommended set";
  reset.addEventListener("click", () => {
    draft.on = (proxyVocabulary().default_kinds || []).slice();
    renderProxying();
    announceProxy(
      `${provider.display_name}: the recommended set is ${draft.on.join(", ")}.`,
    );
  });
  block.appendChild(reset);
  // The non-configurable rule that goes with these chips is stated once, at
  // the top of the page, rather than under every card: five copies of one
  // sentence is how a page teaches a reader to stop reading it.
  return block;
}

function proxyChip(kind, draft) {
  const chip = document.createElement("button");
  chip.type = "button";
  const refused = kind.state === "refused";
  const on = draft.on.includes(kind.id);
  chip.className = refused
    ? "proxy-chip proxy-chip-refused"
    : on
      ? "proxy-chip proxy-chip-on"
      : "proxy-chip";
  chip.textContent = kind.id;
  chip.disabled = refused;
  chip.setAttribute("aria-pressed", refused ? "false" : String(on));
  if (kind.reason) chip.title = kind.reason;
  if (refused) {
    chip.setAttribute("aria-disabled", "true");
  } else {
    chip.addEventListener("click", () => {
      draft.on = on
        ? draft.on.filter((name) => name !== kind.id)
        : draft.on.concat([kind.id]);
      renderProxying();
    });
  }
  return chip;
}

/* How many rows of one chain are drawn before the operator asks for more.
 *
 * The chain cap went in 7.19.0, so a card can now hold three hundred entries
 * and every row is a handful of elements with listeners on them. The Models
 * page answered the same question the same way: draw a page, say how many are
 * behind it, and let a press fill the next one. Fifty is what fits on a screen
 * with room to scroll, and the whole list is still one press away. */
const PROXY_ENTRY_PAGE_SIZE = 50;

/* How many rows each card is currently showing, by provider id. Lives outside
 * the draft on purpose: how much of a list you are looking at is not part of
 * the chain you are editing, and it must not travel to the server or make the
 * card dirty. */
const proxyEntryShown = new Map();

function proxyEntriesShown(providerId, total) {
  const asked = Number(proxyEntryShown.get(providerId)) || PROXY_ENTRY_PAGE_SIZE;
  return Math.min(Math.max(asked, PROXY_ENTRY_PAGE_SIZE), total);
}

function proxyShowMoreEntries(providerId, total, all) {
  const shown = proxyEntriesShown(providerId, total);
  proxyEntryShown.set(
    providerId,
    all ? total : Math.min(total, shown + PROXY_ENTRY_PAGE_SIZE),
  );
  renderProxying();
}

function proxyEntryList(provider, draft) {
  const list = document.createElement("ol");
  list.className = "proxy-entries";
  if (!draft.entries.length) {
    const empty = document.createElement("li");
    empty.className = "proxy-empty";
    empty.textContent =
      "No entries yet. Add an address, or add Direct to keep this machine's " +
      "own IP in the rotation.";
    list.appendChild(empty);
    return list;
  }
  const total = draft.entries.length;
  const shown = proxyEntriesShown(provider.provider_id, total);
  draft.entries.slice(0, shown).forEach((entry, index) => {
    list.appendChild(proxyEntryRow(provider, draft, entry, index));
  });
  if (shown < total) {
    // The reorder buttons and the drag both address the draft array by index,
    // and the rows behind this line are in that array whether they are drawn
    // or not, so nothing an operator does to a drawn row depends on the rows
    // that are not.
    const more = document.createElement("li");
    more.className = "proxy-entries-more";
    const note = document.createElement("span");
    note.className = "proxy-entries-more-note";
    note.textContent = `Showing ${shown} of ${total} entries.`;
    const next = document.createElement("button");
    next.type = "button";
    next.className = "secondary-button proxy-entries-more-next";
    next.textContent = `Show ${Math.min(PROXY_ENTRY_PAGE_SIZE, total - shown)} more`;
    next.addEventListener("click", () =>
      proxyShowMoreEntries(provider.provider_id, total, false),
    );
    const all = document.createElement("button");
    all.type = "button";
    all.className = "ghost-button proxy-entries-more-all";
    all.textContent = `Show all ${total}`;
    all.addEventListener("click", () =>
      proxyShowMoreEntries(provider.provider_id, total, true),
    );
    more.append(note, next, all);
    list.appendChild(more);
  }
  return list;
}

/* What one entry's row says about that address, from the health the server
 * measured. A draft entry the operator just typed has no health at all and is
 * not pretended to have any. */
function proxyEntryHealth(entry) {
  const health = entry.health || {};
  const waiting = Math.round(Number(health.cooldown_remaining) || 0);
  const wait = waiting >= 60 ? `${Math.round(waiting / 60)}m` : `${waiting}s`;
  // Ahead of every other state, and it is not a bench: the checker measured
  // this address terminating TLS, so it is refused until a later check says
  // the destination's certificate verifies again.
  if (entry.refused || health.state === "intercepted") {
    return {
      state: "intercepted",
      text: "refused -- TLS intercepted",
      title:
        health.reason ||
        "This proxy breaks certificate validation -- MCC will not route through it.",
    };
  }
  if (health.state === "unreachable") {
    // Since 7.19.0 a bench running out does not put an address back: it makes
    // it due for a re-check, and only a check that PASSES returns it to the
    // rotation. The row has to say which of the two it is looking at, or an
    // operator watching a countdown reach zero would expect traffic to start
    // flowing through it again.
    const detail = health.reason ? ` (${health.reason})` : "";
    return {
      state: "unreachable",
      text: health.due_for_recheck
        ? `unhealthy -- due for a re-check${detail}`
        : `unhealthy -- next check in ${wait}${detail}`,
      title:
        (health.reason || "This address would not carry a request.") +
        " It stays out of the rotation until a check passes. Press Check now " +
        "to run one.",
    };
  }
  if (health.state === "cooldown") {
    return {
      state: "cooldown",
      text: `cooldown ${wait}`,
      title: health.reason || "Benched after a failure you selected.",
    };
  }
  if (health.state === "healthy") {
    return {
      state: "healthy",
      text: "healthy",
      title: `${health.successes} of ${health.requests} requests answered.`,
    };
  }
  if (health.state === "failing") {
    return {
      state: "failing",
      text: "failing",
      title: health.reason || `${health.failures} failures, nothing answered.`,
    };
  }
  return {
    state: "unknown",
    text: "not checked yet",
    title: "No request has gone through this address yet.",
  };
}

/* How long ago the checker last looked at this address, in words.
 *
 * Coarse on purpose: the operator's question is "is this measurement current",
 * not "was it 412 or 413 seconds ago", and a second-precise clock on a page
 * that does not tick would be wrong the moment it rendered. */
function proxyCheckedAgo(at) {
  const when = Date.parse(String(at || ""));
  if (!Number.isFinite(when)) return "";
  const seconds = Math.max(0, Math.round((Date.now() - when) / 1000));
  if (seconds < 90) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 90) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  return hours < 48 ? `${hours}h ago` : `${Math.round(hours / 24)}d ago`;
}

/* The checker's own line on a row: latency, what the tunnel did to certificate
 * validation, and when that was measured. Separate from the health span beside
 * it because they answer different questions -- health is what the *pools*
 * observed carrying real traffic, this is what a deliberate test found. An
 * address with neither says so rather than borrowing the other's wording. */
function proxyCheckReadout(entry) {
  if (entry.direct) {
    return { text: "no proxy to test", title: "" };
  }
  const check = entry.last_check;
  if (!check) {
    return {
      text: "not tested",
      title: "Press Test to measure this address against this provider's host.",
    };
  }
  const ago = proxyCheckedAgo(check.at);
  if (check.tls === "intercepted") {
    return {
      text: `TLS intercepted${ago ? ` · ${ago}` : ""}`,
      title:
        check.detail ||
        "The tunnel presented a certificate this machine does not trust.",
    };
  }
  if (!check.ok) {
    return {
      text: `no answer${ago ? ` · ${ago}` : ""}`,
      title: check.detail || "This address did not answer the last check.",
    };
  }
  const latency =
    check.latency_ms === null || check.latency_ms === undefined
      ? ""
      : `${check.latency_ms} ms · `;
  const exit = check.exit_ip ? ` · exit ${check.exit_ip}` : "";
  return {
    text: `${latency}TLS strict${exit}${ago ? ` · ${ago}` : ""}`,
    title:
      "The destination's certificate verified through this tunnel." +
      (check.exit_ip ? ` Your exit-IP URL saw ${check.exit_ip}.` : ""),
  };
}

function proxyEntryRow(provider, draft, entry, index) {
  const row = document.createElement("li");
  const classes = ["proxy-entry"];
  if (entry.paused) classes.push("proxy-entry-paused");
  if (entry.refused) classes.push("proxy-entry-refused");
  row.className = classes.join(" ");
  row.dataset.index = String(index);

  // A real <button>, so the keyboard entry point to the reorder is the same
  // element the pointer uses rather than a second mechanism bolted on -- the
  // rule the Model Config rails' grip already follows. Pointer events, never
  // the HTML5 drag API: jsdom has neither of the two objects that API needs,
  // so an implementation built on it could be written here and never covered
  // by a test, and it would be a second drag idiom on an adjacent page.
  const handle = document.createElement("button");
  handle.type = "button";
  handle.className = "proxy-entry-handle";
  handle.textContent = "\u283F";
  handle.setAttribute(
    "aria-label",
    `Reorder ${entry.direct ? "Direct" : entry.label || entry.proxy}`,
  );
  handle.addEventListener("pointerdown", (event) =>
    startProxyDrag(provider, draft, row, event),
  );
  handle.addEventListener("keydown", (event) => {
    if (event.key === "ArrowUp" && index > 0) {
      event.preventDefault();
      proxyMoveEntry(provider, draft, index, index - 1);
    } else if (event.key === "ArrowDown" && index < draft.entries.length - 1) {
      event.preventDefault();
      proxyMoveEntry(provider, draft, index, index + 1);
    }
  });

  const position = document.createElement("span");
  position.className = "proxy-entry-index";
  position.textContent = String(index + 1);

  const label = document.createElement("span");
  label.className = "proxy-entry-label";
  label.textContent = entry.direct ? "Direct (no proxy)" : entry.label || entry.proxy;

  const scheme = document.createElement("span");
  scheme.className = "proxy-entry-scheme";
  scheme.textContent = entry.direct ? "this machine" : entry.scheme || "";

  const state = document.createElement("span");
  // Live health, measured by the running pools themselves rather than by a
  // checker -- a request that went through this address is a better answer
  // than a probe, and it is the only one available until the checker ships.
  // "not checked yet" stays the wording for an address nothing has used, and
  // it means exactly that: no measurement, not a bad one.
  const health = proxyEntryHealth(entry);
  state.className = `proxy-entry-state proxy-entry-state-${health.state}`;
  state.textContent = entry.paused ? "paused" : health.text;
  // Always a title, not only when there is a longer explanation: the word is
  // ellipsised when the row is tight, and a cut sentence the operator cannot
  // read back is worse than no sentence.
  state.title = health.title || state.textContent;

  const readout = proxyCheckReadout(entry);
  const check = document.createElement("span");
  check.className = "proxy-entry-check";
  check.textContent = readout.text;
  if (readout.title) check.title = readout.title;

  const actions = document.createElement("div");
  actions.className = "proxy-entry-actions";
  // Only a saved address can be tested: the check dials the store's URL, and
  // the page has never held one. An entry typed a moment ago has to be saved
  // before there is anything to measure, and the button says so by not being
  // there rather than by failing when pressed.
  if (!entry.direct && entry.proxy) {
    // "Check now", not "Test". Since 7.19.0 this button is not a diagnostic
    // an operator might reasonably skip: an address that failed is held out of
    // the rotation until a check PASSES, and this is how a person asks for
    // that check without waiting for the re-prober's next pass. One word for
    // one action, on every row, so the healthy row and the benched one do not
    // look like two different controls.
    actions.appendChild(
      proxyEntryButton("Check now", true, (button) =>
        testProxyEntry(provider, entry, button),
      ),
    );
  }
  actions.appendChild(
    proxyEntryButton("Move up", index > 0, () => {
      proxyMoveEntry(provider, draft, index, index - 1);
    }),
  );
  actions.appendChild(
    proxyEntryButton("Move down", index < draft.entries.length - 1, () => {
      proxyMoveEntry(provider, draft, index, index + 1);
    }),
  );
  actions.appendChild(
    proxyEntryButton(entry.paused ? "Resume" : "Pause", true, () => {
      entry.paused = !entry.paused;
      renderProxying();
      announceProxy(
        `${label.textContent} is ${entry.paused ? "paused" : "back in the chain"}` +
          " for " +
          `${provider.display_name}. Press Save to keep it.`,
      );
    }),
  );
  actions.appendChild(
    // The one-element form of the card's bulk remove, not a second path: the
    // row's press and "Remove N selected" reach the same function and leave
    // the draft in the same state.
    proxyEntryButton("Remove", true, () =>
      proxyRemoveEntries(provider, draft, [entry]),
    ),
  );

  // The select column, symmetric with the candidate list above: an operator
  // who can add twelve addresses in one press can take twelve out in one.
  const select = document.createElement("input");
  select.type = "checkbox";
  select.className = "proxy-entry-select";
  select.checked = Boolean(entry.selected);
  select.setAttribute(
    "aria-label",
    `Select ${entry.direct ? "Direct" : entry.label || entry.proxy}`,
  );
  select.addEventListener("change", () => {
    entry.selected = select.checked;
    renderProxying();
  });

  row.append(select, handle, position, label, scheme, state, check, actions);
  return row;
}

/* A pointer drag over one card's entry list.
 *
 * The rows are moved in the DOM as the pointer passes them and the draft array
 * is kept in step, rather than re-rendering per step: a re-render would
 * destroy the captured element mid-gesture and the drag would end on its first
 * move. One render happens at the end, which is also where the reorder is
 * announced. */
const proxyDrag = { provider: null, draft: null, from: -1, node: null };

function startProxyDrag(provider, draft, row, event) {
  // A touch that did not begin on the grip is a scroll, not a drag, so the
  // card still moves under a finger. The same guard the route rails use, and
  // the reason neither page needs a `touch-action` rule of its own.
  if (
    event.pointerType === "touch" &&
    !(
      event.target.classList &&
      event.target.classList.contains("proxy-entry-handle")
    )
  ) {
    return;
  }
  if (typeof event.button === "number" && event.button !== 0) return;
  if (draft.entries.length < 2) return;
  event.preventDefault();
  proxyDrag.provider = provider;
  proxyDrag.draft = draft;
  proxyDrag.node = row;
  proxyDrag.from = Number(row.dataset.index);
  row.classList.add("proxy-entry-dragging");
  try {
    // Capture keeps the grip receiving moves once the pointer leaves it. A
    // synthetic event carries no tracked pointer id and throws here, so the
    // drag must not depend on it: the window listeners below do the work.
    event.target.setPointerCapture(event.pointerId);
  } catch {
    // No capture: the window-level listeners still see every move.
  }
  window.addEventListener("pointermove", continueProxyDrag);
  window.addEventListener("pointerup", endProxyDrag);
  window.addEventListener("pointercancel", endProxyDrag);
}

function continueProxyDrag(event) {
  if (!proxyDrag.node) return;
  const under = document.elementFromPoint(event.clientX, event.clientY);
  const row = under && under.closest ? under.closest(".proxy-entry") : null;
  if (!row || row === proxyDrag.node) return;
  const list = proxyDrag.node.parentElement;
  if (!list || row.parentElement !== list) return;
  const to = Array.prototype.indexOf.call(list.children, row);
  if (to < 0 || to === proxyDrag.from) return;
  const [moved] = proxyDrag.draft.entries.splice(proxyDrag.from, 1);
  proxyDrag.draft.entries.splice(to, 0, moved);
  list.insertBefore(
    proxyDrag.node,
    to > proxyDrag.from ? row.nextSibling : row,
  );
  proxyDrag.from = to;
}

function endProxyDrag() {
  window.removeEventListener("pointermove", continueProxyDrag);
  window.removeEventListener("pointerup", endProxyDrag);
  window.removeEventListener("pointercancel", endProxyDrag);
  if (!proxyDrag.node) return;
  const { provider, draft, from } = proxyDrag;
  proxyDrag.node.classList.remove("proxy-entry-dragging");
  proxyDrag.node = null;
  proxyDrag.from = -1;
  renderProxying();
  const moved = draft.entries[from];
  if (!moved) return;
  announceProxy(
    `${moved.direct ? "Direct" : moved.label || moved.proxy} is now entry ` +
      `${from + 1} of ${draft.entries.length} for ${provider.display_name}. ` +
      "Press Save to keep it.",
  );
}

/* Measure one address, or every saved address on one card.
 *
 * The response is the whole refreshed page state, so the drafts of the card
 * that was tested are dropped and re-seeded from the server: a check writes
 * `last_check` on the store, and a card still holding a pre-check draft would
 * keep showing "not tested" beside a row that had just been measured.
 *
 * Nothing else's draft is touched. The other cards may be halfway through an
 * arrangement somebody is still thinking about. */
async function runProxyCheck(provider, proxyId, button, announcement) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Testing...";
  try {
    proxyState.data = await api("/admin/api/proxy-chains/check", {
      method: "POST",
      body: JSON.stringify({ provider: provider.provider_id, proxy: proxyId || "" }),
    });
    proxyState.drafts.delete(provider.provider_id);
    renderProxying();
    announceProxy(announcement(proxyState.data.checked || {}));
  } catch (error) {
    button.disabled = false;
    button.textContent = label;
    announceProxy(error.message);
    showMessage(error.message, "error");
  }
}

function testProxyEntry(provider, entry, button) {
  const name = entry.label || entry.proxy;
  return runProxyCheck(provider, entry.proxy, button, (checked) => {
    const result = checked[entry.proxy];
    if (!result) return `${name} was not measured.`;
    if (result.tls === "intercepted") {
      return (
        `${name} breaks certificate validation and is refused: its tunnel ` +
        "presented a certificate this machine does not trust, so something " +
        "is reading the traffic rather than relaying it. It cannot be saved " +
        "into a chain and it is held out of the ones it is already in."
      );
    }
    if (!result.ok) {
      return `${name} did not answer: ${result.detail || "no reason given"}.`;
    }
    return (
      `${name} answered in ${result.latency_ms} ms and the destination's ` +
      "certificate verified through its tunnel." +
      (result.exit_ip ? ` Your exit-IP URL saw ${result.exit_ip}.` : "")
    );
  });
}

function testProxyChain(provider, button) {
  return runProxyCheck(provider, "", button, (checked) => {
    const results = Object.values(checked);
    if (!results.length) {
      return `Nothing to test on ${provider.display_name}: save an address first.`;
    }
    const refused = results.filter((item) => item.tls === "intercepted");
    const dead = results.filter((item) => !item.ok && item.tls !== "intercepted");
    const ok = results.filter((item) => item.ok);
    const parts = [`${ok.length} verified the destination's certificate`];
    if (dead.length) parts.push(`${dead.length} did not answer`);
    if (refused.length) {
      const one = refused.length === 1;
      parts.push(
        `${refused.length} ${one ? "breaks" : "break"} certificate validation ` +
          `and ${one ? "is" : "are"} refused ` +
          `(${refused.map((item) => item.label).join(", ")})`,
      );
    }
    return (
      `Tested ${results.length} address(es) for ${provider.display_name}: ` +
      `${parts.join(", ")}.`
    );
  });
}

function proxyEntryButton(text, enabled, action) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "ghost-button proxy-entry-button";
  button.textContent = text;
  button.disabled = !enabled;
  // The handler is given the button rather than the event: the only thing any
  // of these actions has ever wanted from the click is the element to disable
  // while it waits, and passing the event would hand it a target that is not
  // reliably the button once an icon lands inside one.
  if (enabled) button.addEventListener("click", () => action(button));
  return button;
}

function proxyMoveEntry(provider, draft, from, to) {
  if (from === to || from < 0 || to < 0) return;
  if (from >= draft.entries.length || to >= draft.entries.length) return;
  const [moved] = draft.entries.splice(from, 1);
  draft.entries.splice(to, 0, moved);
  renderProxying();
  announceProxy(
    `${moved.direct ? "Direct" : moved.label || moved.proxy} is now entry ` +
      `${to + 1} of ${draft.entries.length} for ${provider.display_name}. ` +
      "Press Save to keep it.",
  );
}

function proxyAddRow(provider, draft) {
  const row = document.createElement("div");
  row.className = "proxy-add";
  const cap = proxyEntryCap();
  const full = Boolean(cap) && draft.entries.length >= cap;

  const input = document.createElement("input");
  input.type = "text";
  input.className = "proxy-url-input";
  input.placeholder = "socks5h://user:pass@203.0.113.7:1080";
  input.setAttribute("aria-label", `Proxy address for ${provider.display_name}`);
  input.disabled = full;

  const add = document.createElement("button");
  add.type = "button";
  add.className = "secondary-button";
  add.textContent = "Add proxy";
  add.disabled = full;
  add.addEventListener("click", () => {
    const url = input.value.trim();
    if (!url) {
      announceProxy("Type a proxy address first, with its scheme.");
      return;
    }
    // The URL is held only until Save; the page never reads one back from the
    // server, so what is on screen from here is the masked host:port.
    draft.entries.push({
      proxy: "",
      url,
      direct: false,
      paused: false,
      label: proxyMaskedLabel(url),
      scheme: proxyScheme(url),
    });
    input.value = "";
    renderProxying();
    announceProxy(
      `Added ${proxyMaskedLabel(url)} to ${provider.display_name} as entry ` +
        `${draft.entries.length}. Press Save to keep it.`,
    );
  });

  const direct = document.createElement("button");
  direct.type = "button";
  direct.className = "secondary-button";
  direct.textContent = "Add Direct";
  direct.disabled = full;
  direct.addEventListener("click", () => {
    draft.entries.push({ proxy: "", url: "", direct: true, paused: false, label: "" });
    renderProxying();
    announceProxy(
      `Added Direct to ${provider.display_name}: when the chain reaches it, ` +
        "the request goes out on this machine's own address.",
    );
  });

  // Seeded from <PROVIDER>_PROXY the first time, so turning a static proxy
  // into a chain cannot lose it. The entry carries `inherit` rather than a
  // URL: the page has never been told that address, and the server resolves
  // it on save -- which is how the upgrade works without the password ever
  // crossing the wire in either direction.
  if (!draft.entries.length && provider.inherited_label) {
    const seed = document.createElement("button");
    seed.type = "button";
    seed.className = "secondary-button";
    seed.textContent = `Start from ${provider.inherited_label}`;
    seed.addEventListener("click", () => {
      draft.entries.push({
        proxy: "",
        url: "",
        inherit: true,
        direct: false,
        paused: false,
        label: provider.inherited_label,
        scheme: provider.inherited_scheme,
      });
      renderProxying();
      announceProxy(
        `${provider.inherited_label} is entry 1 for ${provider.display_name}. ` +
          `${provider.env_var || "The stored proxy"} keeps its value; the ` +
          "chain is what gets used once you save.",
      );
    });
    row.append(input, add, direct, seed);
  } else {
    row.append(input, add, direct);
  }

  if (full) {
    const note = document.createElement("p");
    note.className = "proxy-note";
    note.textContent =
      `A chain holds at most ${cap} entries -- your own ` +
      "PROXY_CHAIN_MAX_ENTRIES, on Limits & Resilience. 0 means no limit.";
    row.appendChild(note);
  }
  return row;
}

function proxyCardFoot(provider, draft) {
  const foot = document.createElement("div");
  foot.className = "proxy-card-foot";

  const bound = proxyVocabulary().switch_bound || { min: 1, max: 5 };
  const boundLabel = document.createElement("label");
  boundLabel.className = "proxy-control";
  const boundText = document.createElement("span");
  boundText.textContent = "Switches per request";
  const boundInput = document.createElement("input");
  boundInput.type = "number";
  boundInput.className = "proxy-bound-input";
  boundInput.min = String(bound.min);
  boundInput.max = String(bound.max);
  boundInput.value = String(draft.max_switches);
  boundInput.addEventListener("change", () => {
    draft.max_switches = Number(boundInput.value);
  });
  boundLabel.append(boundText, boundInput);

  const scopeLabel = document.createElement("label");
  scopeLabel.className = "proxy-control";
  const scopeText = document.createElement("span");
  scopeText.textContent = "Quota is metered per";
  const scope = document.createElement("select");
  scope.className = "proxy-scope";
  (proxyVocabulary().scopes || []).forEach((name) => {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name === "provider" ? "address" : "address and key";
    scope.appendChild(option);
  });
  scope.value = draft.scope;
  scope.addEventListener("change", () => {
    draft.scope = scope.value;
  });
  scopeLabel.append(scopeText, scope);

  // What happens when this chain has nothing healthy left. ON for every chain
  // including a subscription-login one, because the alternative is a provider
  // that stops answering the moment its free proxies die -- and an operator
  // who added proxies to REACH a provider did not ask for that. Off is
  // available and means it: a chain with this off never sends a request from
  // this machine's own address.
  const directLabel = document.createElement("label");
  directLabel.className = "proxy-control proxy-direct-fallback";
  const directInput = document.createElement("input");
  directInput.type = "checkbox";
  directInput.className = "proxy-direct-fallback-input";
  directInput.checked = draft.direct_fallback !== false;
  directInput.addEventListener("change", () => {
    draft.direct_fallback = directInput.checked;
    renderProxying();
  });
  const directText = document.createElement("span");
  directText.textContent = "Fall back to this machine's own address";
  directLabel.title =
    directInput.checked
      ? "When no healthy proxy is left, the request goes out with no proxy " +
        "at all rather than failing. It is tried once, and the request log " +
        "says Direct on that try."
      : "This chain never uses this machine's own address. When every proxy " +
        "in it is unhealthy, requests fail through to the model fallback " +
        "chain as they did before 7.19.0.";
  directLabel.append(directInput, directText);

  const actions = document.createElement("div");
  actions.className = "proxy-card-actions";

  // Only offered once there is something saved to measure. The check dials the
  // stored URL against this provider's own host, so a card whose chain has
  // never been saved has no address and no destination to aim at.
  const savedEntries = ((provider.chain && provider.chain.entries) || []).filter(
    (entry) => entry.proxy && !entry.direct,
  );
  if (savedEntries.length) {
    const testAll = document.createElement("button");
    testAll.type = "button";
    testAll.className = "secondary-button proxy-test-all";
    testAll.textContent = `Test all (${savedEntries.length})`;
    testAll.addEventListener("click", () => testProxyChain(provider, testAll));
    actions.appendChild(testAll);
  }

  // The symmetric bulk action on the card. Offered only when something is
  // ticked, so a card nobody is editing carries no control it does not need.
  const ticked = draft.entries.filter((entry) => entry.selected);
  if (ticked.length) {
    const removeMany = document.createElement("button");
    removeMany.type = "button";
    removeMany.className = "secondary-button proxy-entry-bulk-remove";
    removeMany.textContent = `Remove ${ticked.length} selected`;
    removeMany.addEventListener("click", () =>
      proxyRemoveEntries(provider, draft, ticked),
    );
    actions.appendChild(removeMany);
  }

  const save = document.createElement("button");
  save.type = "button";
  save.className = "primary-button";
  save.textContent = "Save";
  save.addEventListener("click", () => saveProxyChain(provider, draft, save));
  actions.appendChild(save);

  if (provider.chain) {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "ghost-button";
    remove.textContent = "Remove chain";
    remove.addEventListener("click", () =>
      saveProxyChain(provider, draft, remove, true),
    );
    actions.appendChild(remove);
  }

  foot.append(boundLabel, scopeLabel, directLabel, actions);
  return foot;
}

/* Take entries out of a chain being edited -- one, or every ticked one.
 *
 * A draft change, not a write: the chain on disk is unchanged until Save, and
 * the announcement says so. Undo for this one is the card's own Save button
 * not being pressed, which is why it does not go through the status panel's
 * Undo -- there is nothing on disk to put back.
 */
function proxyRemoveEntries(provider, draft, entries) {
  const doomed = new Set(entries);
  if (!doomed.size) return;
  const names = entries.map((entry) =>
    entry.direct ? "Direct (no proxy)" : entry.label || entry.proxy,
  );
  draft.entries = draft.entries.filter((entry) => !doomed.has(entry));
  renderProxying();
  announceProxy(
    `Removed ${
      names.length === 1 ? names[0] : `${names.length} entries`
    } from ${provider.display_name}. ` +
      `${draft.entries.length} entr${draft.entries.length === 1 ? "y" : "ies"} ` +
      "left. Press Save to keep it -- nothing has changed on disk yet.",
  );
}

async function saveProxyChain(provider, draft, button, remove = false) {
  button.disabled = true;
  try {
    proxyState.data = await api("/admin/api/proxy-chains", {
      method: "PUT",
      body: JSON.stringify({
        provider: provider.provider_id,
        remove,
        enabled: draft.enabled,
        policy: draft.policy,
        scope: draft.scope,
        max_switches: draft.max_switches,
        direct_fallback: draft.direct_fallback !== false,
        on: draft.on,
        oauth_acknowledged: draft.oauth_acknowledged,
        entries: draft.entries.map((entry) => ({
          proxy: entry.proxy || "",
          url: entry.url || "",
          inherit: Boolean(entry.inherit),
          direct: Boolean(entry.direct),
          paused: Boolean(entry.paused),
        })),
      }),
    });
    // Only this card's draft. Saving one provider must not silently discard
    // an order somebody is halfway through arranging on another: they are
    // separate documents on the server and separate edits on the page.
    proxyState.drafts.delete(provider.provider_id);
    renderProxying();
    announceProxy(
      remove
        ? `${provider.display_name} follows its stored proxy again. Its ` +
            "providers were rebuilt, so key health starts from zero."
        : `Saved ${provider.display_name}. Requests through it use this ` +
            "chain from now on. Its providers were rebuilt to pick the " +
            "change up, so key health on the Providers page starts from " +
            "zero -- the old numbers were not wrong, the pools they were " +
            "measured on are gone.",
    );
  } catch (error) {
    button.disabled = false;
    announceProxy(error.message);
    showMessage(error.message, "error");
  }
}

function proxyScheme(url) {
  const match = /^([a-z0-9]+):\/\//i.exec(String(url).trim());
  return match ? match[1].toLowerCase() : "";
}

/** host:port for a URL the operator just typed, with any user:pass removed.
 *
 * The client-side twin of config.credentials.mask_proxy_label, and it exists
 * for the same reason: the password in that box must not reach a label, a
 * title attribute or the announcement region. The server never sends one
 * back, so this is the only place on the page a raw URL is ever seen.
 */
function proxyMaskedLabel(url) {
  const withoutScheme = String(url).trim().replace(/^[a-z0-9]+:\/\//i, "");
  const authority = withoutScheme.split("/")[0];
  const at = authority.lastIndexOf("@");
  return at >= 0 ? authority.slice(at + 1) : authority;
}

/* ------------------------------------------------------------------- docs
   The Docs page shows the documentation shipped inside this install. The
   markdown is parsed on the server (see api/docs_render.py) with raw HTML
   disabled; this file only ever places the result and wires the two lists
   of links beside it. Nothing here parses markdown. */

const docsState = { index: null, slug: null, loading: false };

async function loadDocsView() {
  if (docsState.index === null && !docsState.loading) {
    docsState.loading = true;
    try {
      const data = await api("/admin/api/docs");
      docsState.index = Array.isArray(data.documents) ? data.documents : [];
    } finally {
      docsState.loading = false;
    }
    renderDocsList();
  }
  if (docsState.index && docsState.index.length === 0) {
    setDocsStatus(
      "No documentation is bundled with this install. Use the GitHub link above.",
    );
    return;
  }
  if (docsState.slug === null && docsState.index && docsState.index.length) {
    await selectDocument(docsState.index[0].slug);
  }
}

function setDocsStatus(text) {
  const status = byId("docsStatus");
  if (!status) return;
  status.textContent = text || "";
  status.hidden = !text;
}

function renderDocsList() {
  const list = byId("docsList");
  if (!list) return;
  list.innerHTML = "";
  (docsState.index || []).forEach((document_) => {
    const link = document.createElement("a");
    link.href = `#doc-${document_.slug}`;
    link.textContent = document_.title;
    link.title = document_.summary || "";
    link.dataset.docSlug = document_.slug;
    if (document_.slug === docsState.slug) {
      link.setAttribute("aria-current", "true");
    }
    link.addEventListener("click", (event) => {
      event.preventDefault();
      selectDocument(document_.slug).catch((error) =>
        showMessage(error.message, "error"),
      );
    });
    list.appendChild(link);
  });
}

function renderDocsHeadings(headings) {
  const container = byId("docsHeadings");
  const label = byId("docsHeadingsLabel");
  if (!container) return;
  container.innerHTML = "";
  const entries = Array.isArray(headings) ? headings : [];
  if (label) label.hidden = entries.length === 0;
  entries.forEach((heading) => {
    const link = document.createElement("a");
    link.href = `#${heading.anchor}`;
    link.textContent = heading.text;
    // Level 3 sits under level 2. One step of indent is the whole hierarchy
    // this needs; anything deeper is not in the table of contents at all.
    link.className = heading.level >= 3 ? "docs-heading-sub" : "docs-heading-top";
    link.addEventListener("click", (event) => {
      event.preventDefault();
      scrollToDocsAnchor(heading.anchor);
    });
    container.appendChild(link);
  });
}

function scrollToDocsAnchor(anchor) {
  const content = byId("docsContent");
  if (!content || !anchor) return;
  const target = content.querySelector(`[id="${anchor}"]`);
  if (!target) return;
  const reduced =
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  target.scrollIntoView({
    behavior: reduced ? "auto" : "smooth",
    block: "start",
  });
}

async function selectDocument(slug) {
  const content = byId("docsContent");
  if (!content) return;
  setDocsStatus("Loading…");
  let data;
  try {
    data = await api(`/admin/api/docs/${encodeURIComponent(slug)}`);
  } catch (error) {
    content.innerHTML = "";
    renderDocsHeadings([]);
    setDocsStatus(`That document could not be loaded: ${error.message}`);
    return;
  }
  docsState.slug = data.slug;
  setDocsStatus("");
  const title = byId("docsTitle");
  if (title) title.textContent = data.title || "Documentation";
  const summary = byId("docsSummary");
  if (summary) summary.textContent = data.summary || "";
  const github = byId("docsGithub");
  if (github && data.github_url) github.href = data.github_url;
  // Server-rendered, raw HTML disabled at the parser -- see the module
  // docstring in api/docs_render.py for why that is not negotiable.
  content.innerHTML = data.html || "";
  wrapDocsTables(content);
  bindDocsCrossLinks(content);
  renderDocsList();
  renderDocsHeadings(data.headings);
  window.scrollTo({ top: 0, behavior: "auto" });
}

/* A wide table is the one thing in a document that can push the whole page
   sideways. Each one gets its own scroll box so the body never does. */
function wrapDocsTables(root) {
  root.querySelectorAll("table").forEach((table) => {
    if (table.parentElement && table.parentElement.classList.contains("docs-scroll")) {
      return;
    }
    const box = document.createElement("div");
    box.className = "docs-scroll";
    table.replaceWith(box);
    box.appendChild(table);
  });
}

/* A cross-reference to another bundled document switches documents in place
   rather than throwing the reader out to a browser tab. The server emits
   these as `#doc-<slug>`; everything else it emits is a real external link
   and is left alone. */
function bindDocsCrossLinks(root) {
  root.querySelectorAll('a[href^="#doc-"]').forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      const [, slug, anchor] = link.getAttribute("href").split("#");
      selectDocument(slug.replace(/^doc-/, ""))
        .then(() => {
          if (anchor) scrollToDocsAnchor(anchor);
        })
        .catch((error) => showMessage(error.message, "error"));
    });
  });
}

// Providers now render inline as searchable, grouped cards inside
// providersSections (see renderProviderGroups) instead of a separate flat
// status strip, so there is one place to read a provider's status rather
// than two. testProvider() / refreshLocalStatus() still update a card's
// pill and meta line in place after a test call, by provider id.
function updateProviderCard(providerId, status, label, metaText) {
  const card = document.querySelector(`.pv-card[data-provider="${providerId}"]`);
  if (!card) return;
  const pill = card.querySelector(".status-pill");
  pill.className = `status-pill ${statusClass(status)}`;
  pill.textContent = label;
  if (metaText) {
    const meta = card.querySelector(".provider-meta");
    if (meta) meta.textContent = metaText;
  }
}

/* ------------------------------------------------------------- get started */

async function loadOnboarding() {
  const data = await api("/admin/api/onboarding");
  state.onboarding = data;
  renderOnboarding();
  return data;
}

async function updateOnboarding(patch) {
  const data = await api("/admin/api/onboarding", {
    method: "POST",
    body: JSON.stringify(patch),
  });
  state.onboarding = data;
  renderOnboarding();
  return data;
}

// The config-dir banner tells a user on the legacy ``~/.fcc`` home where their
// configuration actually lives and what to type to move it. It is purely
// informational and always has been safe to ignore: moving the directory is a
// single atomic rename that only ``mcc-migrate`` performs, from a shell, with
// the server stopped. A dashboard button could not do it -- on Windows the
// server serving this page holds the request log open, so the rename refuses --
// and a one-click route that relocates a user's keys and history is not
// something a stray local POST should be able to reach. So there is no button
// and no write route; there is this sentence.
async function loadConfigDir() {
  try {
    const data = await api("/admin/api/config-dir");
    state.configDir = data;
    renderConfigDirBanner();
  } catch (err) {
    // A pre-6.40.0 server has no such route; fail quietly and leave the
    // checklist on its own.
    state.configDir = null;
  }
  return state.configDir;
}

function renderConfigDirBanner() {
  const banner = byId("configDirBanner");
  if (!banner) return;
  const data = state.configDir;
  if (!data || !data.banner) {
    banner.hidden = true;
    banner.innerHTML = "";
    return;
  }
  banner.hidden = false;
  banner.innerHTML = "";

  const text = document.createElement("p");
  text.className = "config-dir-banner-text";
  text.textContent = data.banner;
  banner.appendChild(text);
}

// The first incomplete required step is "next" — the one worth walking
// through in full. Everything else collapses to a single line so the
// checklist reads as "here is your next action" instead of a wall of text.
function primaryOnboardingStepId(steps) {
  const nextRequired = steps.find((step) => !step.optional && !step.done);
  return nextRequired ? nextRequired.id : null;
}

// Sentinel for "the user explicitly collapsed the expanded step" — distinct
// from `null` ("nothing chosen yet, auto-select"). It never matches a real
// step id, so a step becoming done can't accidentally re-expand a checklist
// the user just closed.
const ONBOARDING_NOTHING_EXPANDED = "__onboarding_nothing_expanded__";

// A label between the two groups of steps, not a step itself -- listed as
// presentation so a screen reader announces it as a divider rather than an
// interactive list item with nothing to activate.
function onboardingGroupHeading(text) {
  const heading = document.createElement("li");
  heading.className = "get-started-group-heading";
  heading.setAttribute("role", "presentation");
  heading.textContent = text;
  return heading;
}

function renderOnboarding() {
  const onboarding = state.onboarding;
  const progress = byId("getStartedProgress");
  const list = byId("getStartedSteps");
  if (!progress || !list || !onboarding) return;

  // A number alone is easy to skim past; a filled bar reads as progress at a
  // glance and is the one place this view spends visual weight.
  progress.innerHTML = "";
  const progressLabel = document.createElement("span");
  progressLabel.className = "get-started-progress-label";
  progressLabel.textContent = `${onboarding.required_done} of ${onboarding.required_total} essential steps done`;
  progress.appendChild(progressLabel);

  const progressBar = document.createElement("div");
  progressBar.className = "get-started-progress-bar";
  progressBar.setAttribute("role", "progressbar");
  progressBar.setAttribute("aria-valuemin", "0");
  progressBar.setAttribute("aria-valuemax", String(onboarding.required_total));
  progressBar.setAttribute("aria-valuenow", String(onboarding.required_done));
  progressBar.setAttribute(
    "aria-label",
    `${onboarding.required_done} of ${onboarding.required_total} essential steps done`,
  );
  const progressFill = document.createElement("div");
  progressFill.className = "get-started-progress-fill";
  const pct =
    onboarding.required_total > 0
      ? (onboarding.required_done / onboarding.required_total) * 100
      : 0;
  progressFill.style.width = `${pct}%`;
  progressBar.appendChild(progressFill);
  progress.appendChild(progressBar);

  // Expanded/collapsed is view state, not persisted. `null` means nothing has
  // been chosen yet, so auto-select the next action. When a step the app chose
  // becomes done, advance to the new next action, or finishing a step would
  // leave it expanded while the real next one sits collapsed out of sight.
  //
  // Auto-advance applies only to steps the app picked. Opening a completed
  // step to re-read what you did is a legitimate thing to want, and advancing
  // out of it would make already-finished steps impossible to view at all.
  // A user who collapsed everything (ONBOARDING_NOTHING_EXPANDED) is likewise
  // left alone: that id never matches a step.
  if (state.onboardingExpandedStepId === null) {
    state.onboardingExpandedStepId = primaryOnboardingStepId(onboarding.steps);
    state.onboardingExpandedByUser = false;
  } else if (!state.onboardingExpandedByUser) {
    const expandedStep = onboarding.steps.find(
      (step) => step.id === state.onboardingExpandedStepId,
    );
    if (expandedStep && expandedStep.done) {
      state.onboardingExpandedStepId = primaryOnboardingStepId(onboarding.steps);
    }
  }

  // The 3 required steps are a real causal chain -- a client can't be pointed
  // anywhere until a model is set, which needs a provider first -- while the
  // rest are independent extras with no order between them. Rendering all 7
  // as one undifferentiated list buries that shape behind a per-card
  // "Optional" pill you have to read every time. Grouping is derived from
  // `step.optional`, which the step already carries, so nothing new is
  // stored and the boundary just falls out of the array's existing order.
  list.innerHTML = "";
  let optionalHeadingShown = false;
  onboarding.steps.forEach((step, index) => {
    if (index === 0 && !step.optional) {
      list.appendChild(onboardingGroupHeading("Essential"));
    }
    if (step.optional && !optionalHeadingShown) {
      list.appendChild(onboardingGroupHeading("Optional"));
      optionalHeadingShown = true;
    }

    const expanded = step.id === state.onboardingExpandedStepId;

    const item = document.createElement("li");
    item.className = `get-started-step${expanded ? " expanded" : " collapsed"}${step.done ? " done" : ""}`;

    const header = document.createElement("div");
    header.className = "get-started-step-header";
    header.setAttribute("role", "button");
    header.setAttribute("aria-expanded", expanded ? "true" : "false");
    header.tabIndex = 0;
    const toggle = () => {
      state.onboardingExpandedByUser = !expanded;
      state.onboardingExpandedStepId = expanded
        ? ONBOARDING_NOTHING_EXPANDED
        : step.id;
      renderOnboarding();
    };
    header.addEventListener("click", toggle);
    header.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        toggle();
      }
    });

    const marker = document.createElement("span");
    const state_ = step.done ? "ok" : step.optional ? "neutral" : "warn";
    marker.className = `status-pill ${state_}`;
    marker.textContent = step.done ? "Done" : step.optional ? "Optional" : "To do";
    header.appendChild(marker);

    const label = document.createElement("strong");
    label.textContent = step.label;
    header.appendChild(label);

    // A collapsed step reads two ways: closed because it's done, or closed
    // because it hasn't been opened yet. The pill already says which, but a
    // scanning eye shouldn't have to read text to tell them apart -- a
    // finished step gets a check where an unopened one gets the chevron that
    // invites a click.
    const chevron = document.createElement("span");
    const doneAndCollapsed = step.done && !expanded;
    chevron.className = `get-started-step-chevron${doneAndCollapsed ? " is-done" : ""}`;
    chevron.setAttribute("aria-hidden", "true");
    chevron.textContent = doneAndCollapsed ? "✓" : "›";
    header.appendChild(chevron);

    item.appendChild(header);

    if (expanded) {
      const body = document.createElement("div");
      body.className = "get-started-step-body";

      const description = document.createElement("p");
      description.textContent = step.description;
      body.appendChild(description);

      if ((step.guide_anchors || []).length) {
        const guides = document.createElement("p");
        guides.className = "get-started-step-guides";
        step.guide_anchors.forEach((anchor) => {
          const purpose = Object.keys(GUIDE_ANCHORS).find(
            (key) => GUIDE_ANCHORS[key] === anchor,
          );
          if (purpose) guides.append(guideLink(purpose, guideLinkLabel(purpose)));
        });
        if (guides.children.length) body.appendChild(guides);
      }

      if ((step.agents || []).length) {
        body.appendChild(onboardingAgentList(step.agents));
      }

      if (step.instructions && step.instructions.length) {
        const instructionList = document.createElement("ol");
        instructionList.className = "get-started-step-instructions";
        step.instructions.forEach((instruction) => {
          const instructionItem = document.createElement("li");
          instructionItem.textContent = instruction;
          instructionList.appendChild(instructionItem);
        });
        body.appendChild(instructionList);
      }

      const targetView = VIEW_GROUPS.find((view) => view.id === step.view);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "secondary-button";
      button.textContent = targetView
        ? `Go to ${targetView.label}`
        : `Open ${step.view}`;
      button.addEventListener("click", () => {
        // Mark every step visited, not just the Guide. Steps whose doneness
        // cannot be derived from configuration -- reading the Guide, opening
        // the Coding agents page -- are done because you went there, and the
        // server is the only thing that knows which ones those are. Sending
        // the id unconditionally keeps that decision in one place instead of
        // growing a second list of special cases here.
        updateOnboarding({ visited: [step.id] }).catch((error) =>
          showMessage(error.message, "error"),
        );
        state.userNavigated = true;
        setActiveView(step.view, { scroll: true });
        if (step.target) {
          highlightOnboardingTarget(step.target);
        }
      });
      body.appendChild(button);

      item.appendChild(body);
    }

    list.appendChild(item);
  });

  const dismissButton = byId("getStartedDismissButton");
  dismissButton.textContent = onboarding.dismissed
    ? "Checklist dismissed"
    : "Dismiss checklist";
  dismissButton.disabled = onboarding.dismissed;
}

/* ------------------------------------------------------------ guide links
   The Guide already explains every one of these surfaces at length, and until
   now the only way from a surface to its explanation was to open the Guide and
   hunt. One small link per surface closes that, and the anchors are declared
   here rather than spelled at each call site so a renamed heading fails one
   test instead of silently pointing seven links at nothing.

   `guide-*` ids live in index.html, in the Guide view, which is always in the
   DOM (hidden, not absent) -- so the link switches the view first and scrolls
   afterwards, in a rAF, for the same reason the onboarding targets do: the
   element has no box until its section stops being hidden. */

const GUIDE_ANCHORS = {
  cli: "guide-cli",
  desktop_apps: "guide-desktop-apps",
  agent_tiers: "guide-agent-tiers",
  learned: "guide-learned",
  images: "guide-images",
  cost: "guide-cost",
};

// The surfaces that exist in the static markup. Everything else is appended by
// the code that builds it, through the same `guideLink`.
const GUIDE_LINK_MOUNTS = [
  ["#codingAgentsHeading", "cli"],
  ["#desktopAppsHeading", "desktop_apps"],
  ["#reqCostHeading", "cost"],
];

const GUIDE_VIEW_ID = "guide";

/** Return a small "Guide" link into one section of the Guide.
 *
 * `purpose` is a key of GUIDE_ANCHORS, never a raw anchor, so a link can only
 * ever be written for a section that is declared to exist. */
function guideLink(purpose, label) {
  const anchor = GUIDE_ANCHORS[purpose];
  const link = document.createElement("a");
  link.className = "guide-link";
  link.href = `#${anchor || ""}`;
  link.dataset.guidePurpose = purpose;
  link.dataset.guideAnchor = anchor || "";
  link.textContent = label || "Guide";
  link.setAttribute("aria-label", `Open the Guide at ${anchor || purpose}`);
  link.addEventListener("click", (event) => {
    event.preventDefault();
    if (!anchor) return;
    state.userNavigated = true;
    setActiveView(GUIDE_VIEW_ID, { scroll: false });
    scrollToGuideAnchor(anchor);
  });
  return link;
}

function scrollToGuideAnchor(anchor) {
  requestAnimationFrame(() => {
    const target = document.getElementById(anchor);
    if (!target) return;
    const reduced =
      window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    target.scrollIntoView({
      behavior: reduced ? "auto" : "smooth",
      block: "start",
    });
    target.classList.add("onboarding-highlight");
    window.setTimeout(() => target.classList.remove("onboarding-highlight"), 2000);
  });
}

/** Attach the static links once, at boot. Idempotent: a second call is a no-op. */
function mountGuideLinks() {
  GUIDE_LINK_MOUNTS.forEach(([selector, purpose]) => {
    const host = document.querySelector(selector);
    if (!host || host.querySelector(":scope > .guide-link")) return;
    host.appendChild(guideLink(purpose));
  });
}

const GUIDE_LINK_LABELS = {
  cli: "Guide: CLI agents",
  desktop_apps: "Guide: desktop apps",
};

function guideLinkLabel(purpose) {
  return GUIDE_LINK_LABELS[purpose] || "Guide";
}

/** The agents detected on this machine, as the Get Started step lists them.
 *
 * The step used to be "done" because the user had *visited* the Coding agents
 * page, which is a fact about navigation. This is the same detection the page
 * itself runs -- one implementation, so the two cannot disagree about whether
 * Codex is installed -- and it writes nothing: every row is a status, a
 * command to copy, or a link to the card that has the button. */
function onboardingAgentList(agents) {
  const wrapper = document.createElement("div");
  wrapper.className = "get-started-agents";

  const installed = agents.filter((agent) => agent.installed);
  const lead = document.createElement("p");
  lead.className = "field-description";
  lead.textContent = installed.length
    ? `${installed.length} of ${agents.length} agents MCC knows about are on this machine.`
    : "None of the agents MCC knows about were detected on this machine.";
  wrapper.appendChild(lead);

  const list = document.createElement("ul");
  list.className = "get-started-agent-list";
  // Installed first, then connected first among those: the rows worth acting
  // on are the ones at the top.
  const ordered = agents
    .slice()
    .sort(
      (a, b) =>
        Number(b.installed) - Number(a.installed) ||
        Number(b.connected) - Number(a.connected) ||
        a.display_name.localeCompare(b.display_name),
    );
  ordered.forEach((agent) => {
    if (!agent.installed && !agent.connected) return;
    const item = document.createElement("li");
    item.className = "get-started-agent";
    item.dataset.agent = agent.id;

    const pill = document.createElement("span");
    pill.className = `status-pill ${agent.connected ? "ok" : "neutral"}`;
    pill.textContent = agent.connected
      ? "Connected"
      : agent.state === "managed"
        ? "Managed"
        : "Installed";
    item.appendChild(pill);

    const name = document.createElement("strong");
    name.textContent = agent.display_name;
    item.appendChild(name);

    const detail = document.createElement("span");
    detail.className = "get-started-agent-detail";
    if (agent.kind === "cli") {
      const command = document.createElement("code");
      command.textContent = agent.command;
      detail.appendChild(command);
    } else {
      detail.textContent =
        DESKTOP_STATE_LABELS[agent.state] || agent.state || "Installed";
    }
    item.appendChild(detail);

    if (agent.requests_7d > 0) {
      const traffic = document.createElement("span");
      traffic.className = "get-started-agent-traffic";
      traffic.textContent = `${formatAnalyticsNumber(agent.requests_7d)} requests (7d)`;
      item.appendChild(traffic);
    }

    if (agent.kind === "desktop" && agent.configurable) {
      const jump = document.createElement("button");
      jump.type = "button";
      jump.className = "secondary-button get-started-agent-configure";
      jump.dataset.role = "configure-agent";
      jump.textContent = "Configure";
      // Never a write from here: the checklist takes you to the card that
      // owns the button, and the button is still a deliberate click.
      jump.addEventListener("click", () => {
        state.userNavigated = true;
        setActiveView("coding_agents", { scroll: true });
        highlightOnboardingTarget(`[data-desktop-app="${agent.id}"]`);
      });
      item.appendChild(jump);
    }

    list.appendChild(item);
  });

  if (!list.children.length) {
    const none = document.createElement("p");
    none.className = "field-description";
    none.textContent =
      "Install one of the agents on the Coding agents page, then come back.";
    wrapper.appendChild(none);
  } else {
    wrapper.appendChild(list);
  }
  return wrapper;
}

// The target may be a field that only exists once its (previously hidden)
// view section is in the layout; a rAF lets setActiveView's DOM change settle
// before we measure it for scrollIntoView.
function highlightOnboardingTarget(selector) {
  requestAnimationFrame(() => {
    const target = document.querySelector(selector);
    if (!target) return;
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    target.classList.add("onboarding-highlight");
    window.setTimeout(() => target.classList.remove("onboarding-highlight"), 2000);
  });
}

/* ------------------------------------------------------- model routing ---
   A tier's primary model and its fallbacks are one thing: the path a request
   takes. The generic field grid flowed them into separate, often
   non-adjacent, cells, so the ordering that governs every request was
   invisible. Each tier is rendered as one card instead, with its models on a
   vertical rail -- the rail's length is the depth of the safety net. */

const ROUTE_TIERS = [
  {
    id: "default",
    label: "Default",
    modelKey: "MODEL",
    chainKey: "MODEL_FALLBACKS",
    note: "Used by any tier without a route of its own.",
  },
  { id: "mythos", label: "Mythos", modelKey: "MODEL_MYTHOS", chainKey: "MODEL_MYTHOS_FALLBACKS" },
  { id: "fable", label: "Fable", modelKey: "MODEL_FABLE", chainKey: "MODEL_FABLE_FALLBACKS" },
  { id: "opus", label: "Opus", modelKey: "MODEL_OPUS", chainKey: "MODEL_OPUS_FALLBACKS" },
  { id: "sonnet", label: "Sonnet", modelKey: "MODEL_SONNET", chainKey: "MODEL_SONNET_FALLBACKS" },
  { id: "haiku", label: "Haiku", modelKey: "MODEL_HAIKU", chainKey: "MODEL_HAIKU_FALLBACKS" },
];

/* ------------------------------------------------------------ route drag
   A route is one ordered path drawn as a rail, but reordering it was only
   ever possible one step at a time, with one arrow press per step, and moving
   a model from the Sonnet rail to the Opus rail meant reading the ref off one
   card and retyping it into the other. Both are now a drag.

   Pointer events, not HTML5 drag-and-drop. The only drag this product already
   has is pointer-based (startModelsDrag), two drag idioms on adjacent pages
   read as a bug rather than as a distinction, HTML5 DnD has no touch story at
   all, and -- decisively -- jsdom has neither PointerEvent nor a usable
   DataTransfer, so an HTML5 implementation could not be covered by the
   harness this repo tests its UI with.

   Every gesture funnels into applyRouteDrop, which is the only function that
   mutates a chain. Dedupe, the primary swap and the undo snapshot therefore
   happen in exactly one place instead of once per entry point. */

// Which setting holds the refs paused on a route, keyed by the route's own
// primary model setting. Pause is per route by definition: the same ref
// paused on Opus keeps serving Sonnet.
const ROUTE_PAUSE_KEY = new Map([
  ["MODEL", "MODEL_PAUSED"],
  ["MODEL_MYTHOS", "MODEL_MYTHOS_PAUSED"],
  ["MODEL_FABLE", "MODEL_FABLE_PAUSED"],
  ["MODEL_OPUS", "MODEL_OPUS_PAUSED"],
  ["MODEL_SONNET", "MODEL_SONNET_PAUSED"],
  ["MODEL_HAIKU", "MODEL_HAIKU_PAUSED"],
  ["MODEL_VISION", "MODEL_VISION_PAUSED"],
]);

/** Put the harness alias for a route beside its heading, if it has one.
 *
 * The ids the other coding agents put on the wire -- `mcc/best` and friends --
 * are invisible on this page otherwise, so a reader who has just moved a model
 * onto the Sonnet rail has no way to see that `mcc/medium` is the name their
 * Codex or OpenCode session has to ask for to reach it.
 *
 * The map comes from the config payload, which reads `core/tier_refs.py`. It
 * is deliberately not a table in this file: a second list of the aliases is a
 * second source of truth, and the first thing it would do is disagree. A route
 * with no entry -- MODEL_FABLE, which is a Claude alias rather than a tier --
 * gets no suffix rather than a guessed one.
 */
function tierAliasFor(modelKey) {
  const aliases = (state.config && state.config.route_tier_aliases) || {};
  const alias = aliases[modelKey];
  return typeof alias === "string" && alias ? alias : null;
}

function appendTierAlias(heading, modelKey) {
  const alias = tierAliasFor(modelKey);
  if (!alias) return null;
  const chip = document.createElement("span");
  chip.className = "route-tier-alias";
  // Inside the heading, so the accessible name of the card is "Sonnet
  // (mcc/medium)" -- the whole point is that the two are one name.
  chip.textContent = ` (${alias})`;
  heading.appendChild(chip);
  return chip;
}

/** The name this route is called on the page, for a sentence about it. */
function routeLabelFor(modelKey) {
  const tier = ROUTE_TIERS.find((candidate) => candidate.modelKey === modelKey);
  if (tier) return tier.label;
  return modelKey === "MODEL_VISION" ? "Vision" : String(modelKey || "");
}

function routeRailFor(chainKey) {
  return state.routeRails.get(chainKey) || null;
}

function routeRailForModel(modelKey) {
  let found = null;
  state.routeRails.forEach((editor) => {
    if (editor.modelKey === modelKey) found = editor;
  });
  return found;
}

/** The nearest draggable node's stable id, or null.
 *
 * Ids are stable and are not the model ref: a ref may legitimately sit on two
 * rails at once, and a freshly added row holds no ref at all.
 */
function routeIdFor(node) {
  const owner = node && node.closest ? node.closest("[data-route-id]") : null;
  return owner ? owner.dataset.routeId : null;
}

/** Resolve one route id back to the editor, row and ref behind it. */
function routeEntryFor(id) {
  if (!id) return null;
  if (id.indexOf("route:") === 0) {
    const modelKey = id.slice("route:".length);
    const editor = routeRailForModel(modelKey);
    if (!editor || !editor.primary) return null;
    return { id, editor, row: null, ref: editor.primaryValue(), modelKey };
  }
  const hash = id.lastIndexOf("#");
  if (hash < 0) return null;
  const editor = routeRailFor(id.slice("chain:".length, hash));
  if (!editor) return null;
  const row = editor.rows.find((candidate) => candidate.routeId === id);
  if (!row) return null;
  return {
    id,
    editor,
    row,
    ref: row.combobox.input.value.trim(),
    modelKey: editor.modelKey,
  };
}

/** One rail's ids top to bottom: the primary, then every fallback in order. */
function routeRailIds(editor) {
  const ids = editor && editor.primary ? [`route:${editor.modelKey}`] : [];
  if (editor) editor.rows.forEach((row) => ids.push(row.routeId));
  return ids;
}

function routeRailOf(id) {
  const entry = routeEntryFor(id);
  return entry ? entry.editor : null;
}

/** The contiguous run between two ids, within one rail only.
 *
 * A range that spanned two cards would have no visual meaning: the rails are
 * separate lists that happen to sit side by side.
 */
function routeRangeIds(fromId, toId) {
  const editor = routeRailOf(fromId);
  if (!editor || routeRailOf(toId) !== editor) return [toId];
  const ids = routeRailIds(editor);
  const start = ids.indexOf(fromId);
  const end = ids.indexOf(toId);
  if (start < 0 || end < 0) return [toId];
  return ids.slice(Math.min(start, end), Math.max(start, end) + 1);
}

function setRouteSelection(ids, on) {
  ids.forEach((id) => {
    if (on) state.routeSelection.add(id);
    else state.routeSelection.delete(id);
  });
  syncRouteSelectionUi();
}

function clearRouteSelection() {
  state.routeSelection.clear();
  state.routeAnchorId = null;
  state.routeArrowRange = [];
  syncRouteSelectionUi();
}

/* The selection lives in state, never in the DOM: a rail rebuilt by a drag
   would otherwise silently forget which rows were picked. */
function syncRouteSelectionUi() {
  document.querySelectorAll("[data-route-id]").forEach((node) => {
    node.classList.toggle("is-selected", state.routeSelection.has(node.dataset.routeId));
  });
}

function onRouteSelectClick(id, event) {
  if (!routeEntryFor(id)) return;
  if (event.shiftKey && state.routeAnchorId) {
    setRouteSelection(routeRangeIds(state.routeAnchorId, id), true);
  } else if (event.ctrlKey || event.metaKey) {
    // Ctrl/Cmd-click rather than a checkbox gutter: a rail is one to four rows
    // on a narrow card, and a checkbox column would push the combobox into
    // clipping. The arrow buttons remain the WCAG 2.2 keyboard equivalent.
    setRouteSelection([id], !state.routeSelection.has(id));
    state.routeAnchorId = id;
  } else {
    state.routeSelection.clear();
    setRouteSelection([id], true);
    state.routeAnchorId = id;
  }
  state.routeArrowRange = [];
}

/* Range selection must not be pointer-only: WCAG 2.2 asks for a keyboard
   alternative to any author-controlled drag, so the same range is reachable
   with Shift+Space and Shift+ArrowUp / Shift+ArrowDown from a focused grip. */
function onRouteSelectKeydown(id, event) {
  if (event.key === " " && event.shiftKey) {
    event.preventDefault();
    setRouteSelection(
      routeRangeIds(state.routeAnchorId || id, id),
      !state.routeSelection.has(id),
    );
    state.routeArrowRange = [];
    return;
  }
  if (!event.shiftKey) return;
  if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
  event.preventDefault();
  const editor = routeRailOf(id);
  if (!editor) return;
  const ids = routeRailIds(editor);
  const next = ids[ids.indexOf(id) + (event.key === "ArrowDown" ? 1 : -1)];
  if (!next) return;
  if (!state.routeAnchorId) state.routeAnchorId = id;
  const wanted = routeRangeIds(state.routeAnchorId, next);
  // Walking back towards the anchor shrinks the range rather than leaving the
  // rows behind the cursor selected.
  setRouteSelection(
    state.routeArrowRange.filter((entry) => !wanted.includes(entry)),
    false,
  );
  setRouteSelection(wanted, true);
  state.routeArrowRange = wanted;
  const grip = routeGripFor(next);
  if (grip) grip.focus();
}

/** The grip button on one route node, found by scan rather than by selector:
 *  a route id embeds a settings key and a sequence number, but building a
 *  selector out of anything the payload names needs an escape the page cannot
 *  rely on. */
function routeGripFor(id) {
  const node = Array.from(document.querySelectorAll("[data-route-id]")).find(
    (candidate) => candidate.dataset.routeId === id,
  );
  return node ? node.querySelector(".route-drag-grip") : null;
}

function routeDropIndicator() {
  // One instance, reused: a drag that created a node per hovered row would
  // leak them, and the browser drive measures the node count before and after.
  let bar = byId("routeDropIndicator");
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "routeDropIndicator";
    bar.className = "route-drop-indicator";
    bar.setAttribute("aria-hidden", "true");
  }
  return bar;
}

function clearRouteDropIndicator() {
  const bar = byId("routeDropIndicator");
  if (bar && bar.parentNode) bar.parentNode.removeChild(bar);
  document
    .querySelectorAll(".route-card.is-drop-target")
    .forEach((card) => card.classList.remove("is-drop-target"));
}

function startRouteDrag(id, event) {
  // A touch that did not begin on the grip is a scroll, not a drag, so the
  // card still moves under a finger.
  if (
    event.pointerType === "touch" &&
    !(event.target.classList && event.target.classList.contains("route-drag-grip"))
  ) {
    return;
  }
  if (typeof event.button === "number" && event.button !== 0) return;
  if (!state.routeSelection.has(id)) {
    state.routeSelection.clear();
    setRouteSelection([id], true);
    state.routeAnchorId = id;
  }
  const editor = routeRailOf(id);
  if (!editor) return;
  // Rail order, top to bottom, not click order: it is what the reader is
  // looking at, and it is deterministic under a Shift-range selection.
  const ids = routeRailIds(editor).filter((entry) => state.routeSelection.has(entry));
  state.routeDrag = { ids, sourceChainKey: editor.chainKey, target: null };
  document
    .querySelectorAll(".route-grid, .route-vision")
    .forEach((node) => node.classList.add("is-dragging"));
}

/* Bound on the container: a pointerover dispatched on a row does not reach a
   listener bound to an inner cell. */
function continueRouteDrag(event) {
  if (!state.routeDrag) return;
  const node = event.target.closest ? event.target.closest("[data-route-id]") : null;
  if (!node) return;
  const id = node.dataset.routeId;
  if (id.indexOf("route:") === 0) {
    state.routeDrag.target = { modelKey: node.dataset.modelKey };
  } else {
    const entry = routeEntryFor(id);
    if (!entry) return;
    state.routeDrag.target = {
      chainKey: entry.editor.chainKey,
      index: entry.editor.rows.indexOf(entry.row),
    };
  }
  clearRouteDropIndicator();
  const card = node.closest(".route-card");
  if (card) card.classList.add("is-drop-target");
  if (node.parentNode) node.parentNode.insertBefore(routeDropIndicator(), node);
}

function endRouteDrag(event) {
  const drag = state.routeDrag;
  state.routeDrag = null;
  clearRouteDropIndicator();
  document
    .querySelectorAll(".route-grid, .route-vision")
    .forEach((node) => node.classList.remove("is-dragging"));
  if (!drag || !drag.target) return;
  const source = routeRailFor(drag.sourceChainKey);
  const dest = drag.target.modelKey
    ? routeRailForModel(drag.target.modelKey)
    : routeRailFor(drag.target.chainKey);
  // The modifier is read at drop rather than at press, so the reader can
  // change their mind mid-drag.
  const copy = dest !== source && !(event && event.shiftKey);
  applyRouteDrop(drag.target, drag.ids, copy);
}

/** Every hidden input a drop is about to touch, and its value right now. */
function routeSnapshot(editors) {
  const values = [];
  editors.forEach((editor) => {
    values.push([editor.input, editor.input.value]);
    if (editor.primary) values.push([editor.primary.input, editor.primary.input.value]);
  });
  return { values };
}

/** Put one ref into a chain at a given position, as one new row. */
function insertRouteRow(editor, ref, index) {
  editor.addRow(ref, false);
  const row = editor.rows.pop();
  editor.rows.splice(Math.max(0, Math.min(index, editor.rows.length)), 0, row);
  editor.rows.forEach((item) => editor.rowsEl.appendChild(item.wrapper));
  editor.renumber();
  editor.syncValue();
  return row;
}

/** The one place a drag mutates a chain.
 *
 * `target` is either `{chainKey, index}` -- land in front of that row -- or
 * `{modelKey}`, the rail's primary slot. `copy` leaves the source rows where
 * they are; a same-rail drop is always a move, because a rail that grew a
 * duplicate of its own row would lose it again on Apply.
 */
function applyRouteDrop(target, ids, copy) {
  const dest = target.modelKey
    ? routeRailForModel(target.modelKey)
    : routeRailFor(target.chainKey);
  if (!dest) return false;
  const entries = ids
    .map((id) => routeEntryFor(id))
    .filter((entry) => entry && entry.ref);
  if (!entries.length) return false;

  const touched = new Set([dest]);
  entries.forEach((entry) => touched.add(entry.editor));
  const snapshot = routeSnapshot(touched);
  const notes = [];
  const stranded = `${routeLabelFor(dest.modelKey)} needs a model of its own -- drag a copy instead, or promote a fallback first.`;

  // A primary is a drag source for a reorder and for a copy, never for a move
  // out of its own rail: an empty MODEL fails validation and the server
  // refuses to start, which is why the arrows only ever offer a swap.
  const usable = entries.filter((entry) => {
    if (entry.row || copy || entry.editor === dest) return true;
    notes.push(
      `${routeLabelFor(entry.editor.modelKey)} needs a model of its own -- drag a copy instead, or promote a fallback first.`,
    );
    return false;
  });
  if (!usable.length) {
    announceRoute(notes.join(" "), null);
    return false;
  }

  // Dragging a rail's own primary down into its own chain is the swap the
  // down arrow already performs; anywhere else in the rail would empty it.
  if (
    target.chainKey &&
    usable.length === 1 &&
    !usable[0].row &&
    usable[0].editor === dest
  ) {
    if (target.index !== 0 || !dest.canDemotePrimary()) {
      announceRoute(stranded, null);
      return false;
    }
    dest.swapPrimaryAndFirst();
    state.routeUndo = snapshot;
    syncRouteSelectionUi();
    syncRoutePauseUi();
    announceRoute(
      `${routeLabelFor(dest.modelKey)} and fallback 1 traded places. Nothing was saved yet -- press Apply.`,
      "Undo",
    );
    return true;
  }

  const refs = usable.map((entry) => entry.ref);
  const sourceLabel = routeLabelFor(usable[0].editor.modelKey);
  let anchorRow = null;
  if (target.chainKey) {
    const removing = new Set(
      usable
        .filter((entry) => entry.row && entry.editor === dest && !copy)
        .map((entry) => entry.row),
    );
    for (let at = target.index; at < dest.rows.length; at += 1) {
      if (!removing.has(dest.rows[at])) {
        anchorRow = dest.rows[at];
        break;
      }
    }
  }
  if (!copy) {
    usable.forEach((entry) => {
      if (entry.row) entry.editor.removeRow(entry.row);
    });
  }

  let sentence = "";
  if (target.modelKey) {
    const head = refs[0];
    const demoted = dest.primaryValue();
    dest.setPrimaryValue(head);
    dest.rows.slice().forEach((row) => {
      if (row.combobox.input.value.trim() === head) dest.removeRow(row);
    });
    let at = 0;
    if (demoted && demoted !== head) {
      insertRouteRow(dest, demoted, at);
      at += 1;
    }
    refs.slice(1).forEach((ref) => {
      if (ref === head) return;
      const existing = dest.rows.find((row) => row.combobox.input.value.trim() === ref);
      if (existing) dest.removeRow(existing);
      insertRouteRow(dest, ref, at);
      at += 1;
    });
    sentence = `${head} is now the ${routeLabelFor(dest.modelKey)} route's model.`;
    if (demoted && demoted !== head) {
      sentence += ` The previous one, ${demoted}, is fallback 1.`;
    }
  } else {
    let landed = 0;
    let firstAt = dest.rows.length;
    refs.forEach((ref) => {
      // A chain entry equal to its own primary is dropped at resolve time, so
      // the row could never fire: saying so beats adding a row that vanishes.
      if (ref === dest.primaryValue()) {
        notes.push(
          `${routeLabelFor(dest.modelKey)} already routes to ${ref} first, so it was not added to its own chain.`,
        );
        return;
      }
      // Duplicates are dropped on save, so a second copy would be a row the
      // reader watches disappear. Move the one that is already there instead.
      const existing = dest.rows.find((row) => row.combobox.input.value.trim() === ref);
      if (existing) {
        if (existing === anchorRow) {
          anchorRow = dest.rows[dest.rows.indexOf(existing) + 1] || null;
        }
        dest.removeRow(existing);
        notes.push(
          `${ref} was already in the ${routeLabelFor(dest.modelKey)} chain -- moved instead of copied.`,
        );
      }
      const at = anchorRow ? dest.rows.indexOf(anchorRow) : dest.rows.length;
      if (!landed) firstAt = at;
      insertRouteRow(dest, ref, at);
      landed += 1;
    });
    if (!landed) {
      announceRoute(notes.join(" "), null);
      return false;
    }
    const where = `at position ${firstAt + 1}`;
    if (dest === usable[0].editor) {
      sentence = `Moved ${landed} model${landed === 1 ? "" : "s"} inside the ${routeLabelFor(dest.modelKey)} chain, ${where}.`;
    } else if (copy) {
      sentence = `Copied ${landed} model${landed === 1 ? "" : "s"} into the ${routeLabelFor(dest.modelKey)} chain, ${where}. They are still in the ${sourceLabel} chain.`;
    } else {
      sentence = `Moved ${landed} model${landed === 1 ? "" : "s"} into the ${routeLabelFor(dest.modelKey)} chain, ${where}.`;
    }
  }
  state.routeUndo = snapshot;
  syncRouteSelectionUi();
  syncRoutePauseUi();
  announceRoute(
    `${[sentence, ...notes].join(" ")} Nothing was saved yet -- press Apply.`,
    "Undo",
  );
  return true;
}

/** Put the last drag back. One drag is one entry, however many rows it moved. */
function undoLastRouteDrag() {
  const undo = state.routeUndo;
  if (!undo) return false;
  state.routeUndo = null;
  undo.values.forEach((pair) => {
    const input = pair[0];
    const value = pair[1];
    const editor = state.routeRails.get(input.dataset.key);
    if (editor) {
      editor.setValue(value);
    } else {
      input.value = value;
      // Assigning .value fires nothing, so the hover title and the dirty
      // state would go stale after an undo.
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }
  });
  clearRouteSelection();
  updateDirtyState();
  syncRoutePauseUi();
  announceRoute("Put the last drag back. Nothing was saved yet -- press Apply.", null);
  return true;
}

/* ---------------------------------------------------------------- pause --
   Pausing stops a model being tried. Hiding, on the Models page, only removes
   it from listings and never changes routing -- the two are deliberately
   different words for deliberately different things. A pause is written the
   moment it is clicked, through the same locked read-derive-write a
   visibility edit uses, so two clicks landing together cannot lose one. */

function routePausedRefs(modelKey) {
  const key = ROUTE_PAUSE_KEY.get(modelKey);
  const field = key ? state.fields.get(key) : null;
  return String((field && field.value) || "")
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);
}

function isRoutePaused(modelKey, ref) {
  return Boolean(ref) && routePausedRefs(modelKey).includes(ref);
}

/** Repaint every pause control from state. Rows carry no pause state of their
 *  own, so a rebuilt rail cannot disagree with the settings payload. */
function syncRoutePauseUi() {
  document.querySelectorAll("[data-route-id]").forEach((node) => {
    const button = node.querySelector(".route-pause-toggle");
    if (!button) return;
    const modelKey = node.dataset.modelKey || "";
    const input = node.querySelector("input");
    const ref = input ? input.value.trim() : "";
    const paused = isRoutePaused(modelKey, ref);
    // A paused row stays fully visible with its complete ref: hiding it would
    // be the one thing the Models page is documented never to do to routing.
    node.classList.toggle("is-paused", paused);
    button.textContent = paused ? "Resume" : "Pause";
    button.setAttribute("aria-pressed", paused ? "true" : "false");
    button.setAttribute(
      "aria-label",
      `${paused ? "Resume" : "Pause"} ${ref || "this entry"} on the ${routeLabelFor(modelKey)} route`,
    );
    button.disabled = !modelKey || !ref;
    const chip = node.querySelector(".route-pause-chip");
    if (chip) chip.hidden = !paused;
  });
}

async function toggleRoutePause(modelKey, ref, paused, button) {
  if (!modelKey || !ref) return;
  const harnessTier = parseHarnessTierKey(modelKey);
  if (harnessTier) {
    await toggleHarnessTierPause(harnessTier, modelKey, ref, paused, button);
    return;
  }
  // The models tick reverts optimistically and leaves its box live while the
  // write is in flight; a second click there raced the first. Disable instead.
  if (button) button.disabled = true;
  try {
    const result = await api("/admin/api/config/route-pause", {
      method: "POST",
      body: JSON.stringify({ model_key: modelKey, model_ref: ref, paused }),
    });
    if ((result.errors || []).length) {
      announceRoutePauseFailure(modelKey, ref, paused, result.errors.join("; "));
      showMessage(result.errors.join("; "), "error");
      return;
    }
    // Patched in place rather than refetched: the whole payload is megabytes,
    // and one key changed.
    const field = state.fields.get(result.paused_key);
    if (field) field.value = result.paused_value || "";
    syncRoutePauseUi();
    updateDeadlineCalculator();
    announceRoute(
      paused
        ? `Paused ${ref} on the ${routeLabelFor(modelKey)} route. It is skipped without spending an attempt, and still shows in the request log as not tried.`
        : `Resumed ${ref} on the ${routeLabelFor(modelKey)} route.`,
      "Undo",
      // The Undo POST must disable the row's own toggle exactly as the first
      // click did: passing null left it live, so a click on the row could
      // race the Undo and the two writes could land in either order.
      () => toggleRoutePause(modelKey, ref, !paused, button),
    );
  } catch (error) {
    announceRoutePauseFailure(modelKey, ref, paused, error.message);
    showMessage(error.message, "error");
  } finally {
    if (button) button.disabled = false;
    syncRoutePauseUi();
  }
}

/** Say a failed pause failed, in the panel the pause control speaks through.
 *
 * The failure used to go only to #messageArea, at the top of the page: the
 * pause panel said nothing, so the row simply snapped back to its old state
 * with no explanation. No Undo button -- there is nothing to undo, because
 * nothing reached the file; the `finally` repaint has already put the row
 * back into its true state.
 */
function announceRoutePauseFailure(modelKey, ref, paused, reason) {
  const verb = paused ? "pause" : "resume";
  const detail = (reason || "").trim();
  announceRoute(
    `Could not ${verb} ${ref} on the ${routeLabelFor(modelKey)} route -- nothing changed.${detail ? ` ${detail}` : ""}`,
    null,
  );
}

/** One live region for the whole Model Config page: drag and pause both.
 *
 * Whole sentences, never a bare number, and a persistent panel rather than a
 * toast -- the same shape the Models page's bulk result panel uses, and
 * `hidden` is toggled rather than `style.display`.
 */
function announceRoute(sentence, undoLabel, undoAction) {
  const target = byId("routeStatus");
  if (!target) return;
  target.textContent = "";
  if (!sentence) {
    target.hidden = true;
    return;
  }
  target.hidden = false;
  const lead = document.createElement("p");
  lead.textContent = sentence;
  target.appendChild(lead);
  if (undoLabel) {
    const undo = document.createElement("button");
    undo.type = "button";
    undo.className = "secondary-button route-status-button";
    undo.textContent = undoLabel;
    undo.addEventListener("click", () => {
      undo.disabled = true;
      if (undoAction) undoAction();
      else undoLastRouteDrag();
    });
    target.appendChild(undo);
  }
  const dismiss = document.createElement("button");
  dismiss.type = "button";
  dismiss.className = "secondary-button route-status-button";
  dismiss.textContent = "Dismiss";
  dismiss.addEventListener("click", () => {
    target.textContent = "";
    target.hidden = true;
  });
  target.appendChild(dismiss);
}

/** Grip, pause button and paused chip for one route node.
 *
 * A real <button> for each, so the keyboard entry point to the drag is the
 * same element the pointer uses rather than a second mechanism bolted on.
 */
function routeNodeControls(node, id, label) {
  node.dataset.routeId = id;

  const grip = document.createElement("button");
  grip.type = "button";
  grip.className = "route-drag-grip";
  grip.textContent = "⠿";
  grip.setAttribute("aria-label", `Reorder ${label}`);
  grip.addEventListener("pointerdown", (event) => startRouteDrag(id, event));
  grip.addEventListener("click", (event) => onRouteSelectClick(id, event));
  grip.addEventListener("keydown", (event) => onRouteSelectKeydown(id, event));

  // Chip and button share one cell. A hidden chip is `display: none` and
  // would otherwise vacate a grid column, shifting every control on a paused
  // row one place left of the same control on the row above it.
  const cell = document.createElement("div");
  cell.className = "route-pause-cell";

  const chip = document.createElement("span");
  chip.className = "route-pause-chip";
  chip.textContent = "Paused";
  chip.hidden = true;

  const pause = document.createElement("button");
  pause.type = "button";
  pause.className = "ghost-button route-pause-toggle";
  pause.textContent = "Pause";
  pause.setAttribute("aria-pressed", "false");
  pause.addEventListener("click", () => {
    const modelKey = node.dataset.modelKey || "";
    const input = node.querySelector("input");
    const ref = input ? input.value.trim() : "";
    toggleRoutePause(modelKey, ref, !isRoutePaused(modelKey, ref), pause);
  });

  cell.append(chip, pause);
  return { grip, cell };
}

function routeNode(marker, control, modifier) {
  const node = document.createElement("div");
  node.className = `route-node${modifier ? ` ${modifier}` : ""}`;
  const dot = document.createElement("span");
  dot.className = "route-marker";
  dot.setAttribute("aria-hidden", "true");
  dot.textContent = marker;
  node.append(dot, control);
  return node;
}

/** Fill a route rail with its primary model and the chain under it.
 *
 * The primary and the fallbacks are two settings drawn as one ordered path,
 * and until they reordered as one the arrows on every fallback stopped short
 * of the entry that actually serves the traffic: promoting a fallback meant
 * retyping two fields and hoping they matched. The primary gets the same two
 * buttons every row below it has, wired to the chain editor, which is what
 * owns the ordering rules.
 *
 * Shared by the tier cards and the vision adapter so the six rails on the page
 * cannot drift apart -- a rail that reorders differently from the one beside
 * it reads as a bug, not as a distinction.
 */
function appendRouteRail(rail, modelField, chainField) {
  const { control, input } = buildFieldControl(modelField);
  const node = routeNode("", control, "is-primary");
  node.dataset.modelKey = modelField.key;
  const primaryControls = routeNodeControls(
    node,
    `route:${modelField.key}`,
    modelField.label,
  );
  node.insertBefore(primaryControls.grip, node.firstChild);
  node.appendChild(primaryControls.cell);
  rail.appendChild(node);
  if (!chainField) return;

  const { control: chainControl, editor } = buildFieldControl(chainField);
  rail.appendChild(chainControl);
  if (!editor) return;

  const moves = document.createElement("div");
  moves.className = "route-node-move";

  const upButton = document.createElement("button");
  upButton.type = "button";
  upButton.className = "ghost-button model-chain-move";
  upButton.textContent = "↑";

  const downButton = document.createElement("button");
  downButton.type = "button";
  downButton.className = "ghost-button model-chain-move";
  downButton.textContent = "↓";

  // The primary cannot be removed -- a route without one is not a route -- but
  // its buttons still have to line up with the buttons on every row below.
  // A hidden copy of the remove button is the only spacer guaranteed to stay
  // the same width as the thing it stands in for; a hardcoded margin would be
  // correct until someone changed that button's padding.
  const spacer = document.createElement("button");
  spacer.type = "button";
  spacer.className = "ghost-button model-chain-remove route-node-move-spacer";
  spacer.textContent = "×";
  spacer.disabled = true;
  spacer.tabIndex = -1;
  spacer.setAttribute("aria-hidden", "true");

  moves.append(upButton, downButton, spacer);
  node.appendChild(moves);
  node.classList.add("has-move");
  editor.setPrimary({ input, label: modelField.label, upButton, downButton });
  // The registry cross-rail drag needs: the destination editor has to be
  // reachable from a DOM node, and until now every editor was reachable only
  // through the closure that built it.
  state.routeRails.set(chainField.key, editor);
  syncRoutePauseUi();
}

function renderRouteCard(tier, fieldByKey) {
  const modelField = fieldByKey.get(tier.modelKey);
  const chainField = fieldByKey.get(tier.chainKey);
  if (!modelField) return null;

  const card = document.createElement("article");
  card.className = "route-card";
  card.dataset.tier = tier.id;
  // The onboarding checklist deep-links to [data-key="MODEL"]; keep that
  // selector resolvable now the field lives inside a card.
  card.dataset.key = modelField.key;

  const head = document.createElement("header");
  head.className = "route-card-head";

  const name = document.createElement("h4");
  name.className = "route-tier";
  name.textContent = tier.label;
  appendTierAlias(name, tier.modelKey);

  head.appendChild(name);
  // The default route has no state to report: it is the thing the others
  // inherit, so calling it "custom" would be noise on every install.
  if (tier.id !== "default") {
    const inherits = !String(modelField.value || "").trim();
    const stateChip = document.createElement("span");
    stateChip.className = `route-state${inherits ? " is-inherited" : ""}`;
    stateChip.textContent = inherits ? "Inherits default" : "Custom route";
    head.appendChild(stateChip);
  }
  card.appendChild(head);

  if (tier.note) {
    const note = document.createElement("p");
    note.className = "route-note";
    note.textContent = tier.note;
    card.appendChild(note);
  }

  const rail = document.createElement("div");
  rail.className = "field route-rail";

  appendRouteRail(rail, modelField, chainField);

  card.appendChild(rail);
  return card;
}

/** Where this tier sends a request that carries an image.
 *
 * The fallback rail answers "what covers this model when it fails". It cannot
 * answer "what happens to a screenshot", because the vision adapter fires on
 * what the request *contains* rather than on a failure -- so it never appeared
 * on the rail and the routing page simply did not mention it. A tier whose
 * model is documented not to read images silently sends them somewhere else,
 * and that was invisible until it showed up in the request log.
 */
function buildVisionHop(tierModel, visionModel, mode) {
  const hop = document.createElement("p");
  hop.className = `route-vision-hop${visionModel ? "" : " is-unset"}`;

  const describe = mode === "describe";
  const label = document.createElement("span");
  label.className = "route-vision-hop-label";
  label.textContent = "Images";
  hop.appendChild(label);

  const arrow = document.createElement("span");
  arrow.className = "route-vision-hop-arrow";
  arrow.setAttribute("aria-hidden", "true");
  // In describe mode the picture makes a round trip and comes back as words,
  // so a one-way arrow would say the wrong thing about where the request goes.
  arrow.textContent = describe && visionModel ? "⇄" : "→";
  hop.appendChild(arrow);

  const target = document.createElement("code");
  target.textContent = visionModel || "nowhere — set a Vision adapter";
  hop.appendChild(target);

  const why = document.createElement("span");
  why.className = "route-vision-hop-why";
  if (!visionModel) {
    why.textContent = `${tierModel} cannot read them, so they will fail here`;
  } else if (describe) {
    why.textContent =
      `${tierModel} cannot read them, so they go there as pictures and come ` +
      `back as words — ${tierModel} still answers`;
  } else {
    why.textContent = `${tierModel} cannot read them`;
  }
  hop.appendChild(why);
  return hop;
}

/** Current live value of a routing field, falling back to the default route. */
function routedModelValue(key) {
  const direct = String((state.fields.get(key) || {}).value || "").trim();
  if (direct) return direct;
  return String((state.fields.get("MODEL") || {}).value || "").trim();
}

/** Re-draw the vision hops and the adapter summary from the current state.
 *
 * Called again after the model catalog loads, because the routing section is
 * rendered from the config payload *before* the blind-model set arrives -- so
 * the first paint has no idea which tiers need the adapter. Updating in place
 * rather than re-rendering keeps unsaved edits in the fields untouched.
 */
function visionAdapterMode() {
  const raw = String(
    (state.fields.get("VISION_ADAPTER_MODE") || {}).value || "",
  )
    .trim()
    .toLowerCase();
  return raw === "describe" ? "describe" : "route";
}

function updateVisionRouting() {
  const visionModel = String(
    (state.fields.get("MODEL_VISION") || {}).value || "",
  ).trim();
  const mode = visionAdapterMode();
  const covered = [];
  ROUTE_TIERS.forEach((tier) => {
    const card = document.querySelector(`.route-card[data-tier="${tier.id}"]`);
    if (!card) return;
    const existing = card.querySelector(".route-vision-hop");
    if (existing) existing.remove();
    const tierModel = routedModelValue(tier.modelKey);
    if (!tierModel || !state.blindModels.has(tierModel)) return;
    covered.push(tier.label);
    card.appendChild(buildVisionHop(tierModel, visionModel, mode));
  });

  const summary = document.querySelector(".route-vision-summary");
  if (!summary) return;
  summary.classList.toggle("is-idle", covered.length === 0);
  const what =
    mode === "describe"
      ? "those tiers picked a model that cannot read images, so their images " +
        "are described and the tier's own model still answers."
      : "those tiers picked a model that cannot read images.";
  summary.textContent = covered.length
    ? `Currently covers ${covered.join(", ")} — ${what}`
    : "No tier needs it right now: no tier's model is known to reject images.";
}

/** The Chain benching master switch, rendered on Model Config.
 *
 * Whether a chain is allowed to drop one of its own members is a routing
 * decision, and this is the page where routes are read. It is the same
 * variable the Limits & Resilience card gates itself on -- one manifest
 * field, one saved key, two controls kept in step by `syncSharedControls` --
 * so flipping it here and flipping it there are the same edit. The tuning
 * knobs stay where the consequence is explained, and the link below goes
 * there rather than repeating them.
 */
function renderBenchMasterSwitch(field) {
  const card = document.createElement("article");
  card.className = "route-card route-bench";

  const head = document.createElement("header");
  head.className = "route-card-head";
  const name = document.createElement("h4");
  name.className = "route-tier";
  name.textContent = "Chain benching";
  head.appendChild(name);
  const source = sourceText(field);
  if (source) {
    const chip = document.createElement("span");
    chip.className = "field-source";
    chip.textContent = source;
    head.appendChild(chip);
  }
  card.appendChild(head);

  const control = document.createElement("label");
  control.className = "field route-bench-control";
  const labelText = document.createElement("span");
  labelText.textContent = field.label;
  const input = inputForField(field);
  input.id = `field-${field.key}-model-config`;
  input.dataset.key = field.key;
  input.dataset.original = field.value || "";
  input.dataset.default = field.default ?? "";
  input.dataset.secret = "false";
  input.dataset.configured = field.configured ? "true" : "false";
  input.dataset.fieldType = field.type;
  input.disabled = field.locked;
  const onEdit = () => {
    syncSharedControls(input);
    updateDirtyState();
  };
  input.addEventListener("input", onEdit);
  input.addEventListener("change", onEdit);
  control.append(labelText, input);
  card.appendChild(control);

  const note = document.createElement("p");
  note.className = "route-note";
  note.textContent = field.description || "";
  card.appendChild(note);

  const crosslink = document.createElement("p");
  crosslink.className = "bench-crosslink";
  crosslink.append(
    document.createTextNode("Tuning (window, rate, duration) lives on "),
  );
  const link = document.createElement("a");
  link.href = "#";
  link.textContent = "Limits & Resilience → Chain benching";
  link.addEventListener("click", (event) => {
    event.preventDefault();
    setActiveView("limits", { scroll: true });
    const target = byId("section-benching");
    if (target) target.scrollIntoView({ block: "start" });
  });
  crosslink.append(link, document.createTextNode("."));
  card.appendChild(crosslink);
  return card;
}

/** The adapter's mode, rendered where the adapter is read.
 *
 * The same control the leftovers grid would have drawn, moved into the card
 * and wired to the same `syncSharedControls` + `updateDirtyState` pair every
 * other field uses, plus a re-draw of the hops: switching the mode changes
 * what the arrows above it mean, and a page that keeps saying the old thing
 * until reload is a page that lies.
 */
function renderVisionModeControl(field) {
  const control = document.createElement("label");
  control.className = "field route-vision-mode";
  const labelText = document.createElement("span");
  labelText.textContent = field.label;
  const input = inputForField(field);
  input.id = `field-${field.key}`;
  input.dataset.key = field.key;
  input.dataset.original = field.value || "";
  input.dataset.default = field.default ?? "";
  input.dataset.secret = "false";
  input.dataset.configured = field.configured ? "true" : "false";
  input.dataset.fieldType = field.type;
  input.disabled = field.locked;
  const onEdit = () => {
    syncSharedControls(input);
    const stored = state.fields.get(field.key);
    if (stored) stored.value = input.value;
    updateVisionRouting();
    updateDirtyState();
  };
  input.addEventListener("input", onEdit);
  input.addEventListener("change", onEdit);
  control.append(labelText, input);
  const note = document.createElement("p");
  note.className = "route-note";
  note.textContent = field.description || "";
  const wrap = document.createElement("div");
  wrap.className = "route-vision-mode-wrap";
  wrap.append(control, note);
  return wrap;
}

function renderModelRouting(fields, allFields) {
  const fieldByKey = new Map(fields.map((field) => [field.key, field]));
  const wrap = document.createElement("div");
  wrap.className = "route-layout";

  const rule = document.createElement("p");
  rule.className = "route-rule";
  rule.textContent =
    "Each tier tries its models in order. If one cannot serve a request the " +
    "next takes over, up until the response starts streaming.";
  wrap.appendChild(rule);

  const benchField = (allFields || []).find(
    (candidate) => candidate.key === "FALLBACK_BENCH_ENABLED",
  );
  if (benchField) wrap.appendChild(renderBenchMasterSwitch(benchField));

  const grid = document.createElement("div");
  grid.className = "route-grid";
  ROUTE_TIERS.forEach((tier) => {
    const card = renderRouteCard(tier, fieldByKey);
    if (card) grid.appendChild(card);
  });
  wrap.appendChild(grid);

  // The vision adapter is not a tier. It fires on what a request contains
  // rather than on which model was asked for, so it gets its own shape
  // instead of masquerading as a sixth route.
  const visionField = fieldByKey.get("MODEL_VISION");
  if (visionField) {
    const vision = document.createElement("article");
    vision.className = "route-card route-vision";
    // Cross-tier drag selects rails uniformly; the adapter had no tier at all.
    vision.dataset.tier = "vision";
    vision.dataset.key = visionField.key;

    const head = document.createElement("header");
    head.className = "route-card-head";
    const name = document.createElement("h4");
    name.className = "route-tier";
    name.textContent = "Vision adapter";
    appendTierAlias(name, "MODEL_VISION");
    head.appendChild(name);
    head.appendChild(guideLink("images"));
    vision.appendChild(head);

    const note = document.createElement("p");
    note.className = "route-note";
    note.textContent =
      "Takes any request carrying an image when the model its tier picked " +
      "is known not to read images. Leave as None to send images wherever " +
      "the tier resolves to.";
    vision.appendChild(note);

    // The adapter is a route, so it gets a route's rail: its own model on
    // top and its own fallbacks under it, using the same editor as every
    // tier rather than a second way to express the same idea.
    const rail = document.createElement("div");
    rail.className = "field route-rail route-vision-control";
    appendRouteRail(rail, visionField, fieldByKey.get("MODEL_VISION_FALLBACKS"));
    vision.appendChild(rail);

    // Directly under the rail, because the rail says WHERE images go and this
    // says WHAT HAPPENS when they get there. Read apart, either one is half an
    // answer.
    const modeField = fieldByKey.get("VISION_ADAPTER_MODE");
    if (modeField) vision.appendChild(renderVisionModeControl(modeField));

    // Which tiers this actually covers today. "It fires when a model cannot
    // read images" is a rule; this is the answer for *your* configuration,
    // which is the thing you came to the page to find out. The text is filled
    // by updateVisionRouting, which runs again once the catalog has loaded.
    const summary = document.createElement("p");
    summary.className = "route-vision-summary is-idle";
    vision.appendChild(summary);
    wrap.appendChild(vision);
  }

  // Anything the manifest adds to this section later still has to appear.
  const claimed = new Set([
    "MODEL_VISION",
    "MODEL_VISION_FALLBACKS",
    // Rendered inside the vision card above; leaving it unclaimed would draw
    // it a second time in the leftovers grid.
    "VISION_ADAPTER_MODE",
    ...ROUTE_TIERS.flatMap((tier) => [tier.modelKey, tier.chainKey]),
    // The pause lists are written by the Pause button beside the ref they
    // name, never typed. Leaving them unclaimed would render six bare text
    // boxes under the route grid saying nothing a reader could act on.
    ...ROUTE_PAUSE_KEY.values(),
  ]);
  const unclaimed = fields.filter((field) => !claimed.has(field.key));
  if (unclaimed.length) {
    const rest = document.createElement("div");
    rest.className = "field-grid";
    unclaimed.forEach((field) => rest.appendChild(renderField(field)));
    wrap.appendChild(rest);
  }

  return wrap;
}

/* ------------------------------------------------------- limits & resilience
   One 37-field grid mixed six unrelated concerns, and the number that actually
   decides a handover -- the total budget divided by the number of models still
   to try -- was shown nowhere. Six cards, each stating what it decides, and a
   calculator that reproduces `_attempt_deadline` + `_chunk_timeout` for the
   reader's own routes. Nothing here changes what the server does. */

const CALC_KEYS = new Set([
  ...ROUTE_TIERS.flatMap((tier) => [tier.modelKey, tier.chainKey]),
  "MODEL_VISION",
  "MODEL_VISION_FALLBACKS",
  "FALLBACK_TOTAL_TIMEOUT",
  "FALLBACK_FIRST_TOKEN_TIMEOUT",
  "FALLBACK_ATTEMPT_SHARE_FLOOR",
  "HTTP_READ_TIMEOUT",
  "SERVER_GRACEFUL_SHUTDOWN_SECONDS",
]);

let calcListenerBound = false;
let limitsScrollspyBound = false;

/** The value a setting holds right now: the live control first, payload second.
 *
 * Every view's fields are in the document at all times -- only the <section>
 * is hidden -- so the Model Config page's chain editors are readable while
 * Limits & Resilience is open, and an unsaved edit is reflected immediately.
 */
function liveValue(key) {
  // The field wrapper carries data-key as well as the control, and a <div>'s
  // .value is undefined -- ask for the control by tag or every read is stale.
  const input = document.querySelector(
    `input[data-key="${key}"], select[data-key="${key}"], textarea[data-key="${key}"]`,
  );
  if (input) return String(input.value ?? "");
  return String((state.fields.get(key) || {}).value ?? "");
}

/** A number the calculator should mirror the *server* with.
 *
 * A field nobody has touched renders blank -- its default lives in the
 * placeholder -- and reading that blank as 0 made the calculator claim a limit
 * was switched off when the server was applying its default. Harmless while
 * every deadline shipped pre-set in the managed file; not harmless for a
 * setting a fresh install has never written, which is every install's state
 * for the silent-attempt floor. Fall back to what the server says is in
 * effect, which is what the payload's `effective` field is for.
 */
function calcNumber(key) {
  const typed = liveValue(key).trim();
  if (typed !== "") return Number(typed) || 0;
  const field = state.fields.get(key) || {};
  return Number(field.effective ?? field.default ?? 0) || 0;
}

/** The control bound to a key, never the .field wrapper that repeats data-key. */
const CONTROL_FOR_KEY = (key) =>
  `input[data-key="${key}"], select[data-key="${key}"], textarea[data-key="${key}"]`;

/** How many models this route would actually try.
 *
 * Paused entries are excluded. The calculator's whole claim is that it
 * reproduces `_attempt_deadline` for *your* routes, and the router will not
 * try a paused model -- counting it would overstate the divisor and
 * understate every share on the page.
 */
function chainLength(modelKey, chainKey) {
  const paused = new Set(routePausedRefs(modelKey));
  const primary = liveValue(modelKey).trim();
  const chain = liveValue(chainKey)
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean)
    .filter((ref) => !paused.has(ref));
  const head =
    primary && primary.toLowerCase() !== "none" && !paused.has(primary) ? 1 : 0;
  return head + chain.length;
}

/** Mirror of `_attempt_deadline` + `_chunk_timeout` before the first chunk.
 *
 *  share = total / models; raised to the floor; never above what is left;
 *  then capped by the first-token deadline. Same order as the server, so a
 *  number read here is the number the log will print.
 */
function firstTokenShare(total, first, floor, n) {
  let share = total > 0 ? total / Math.max(1, n) : Infinity;
  if (total > 0 && floor > 0) share = Math.min(Math.max(share, floor), total);
  const cap = first > 0 ? first : Infinity;
  return Math.min(share, cap);
}

/** Whether the floor is what decides this route's share, rather than the
 *  equal division. Only then is it worth naming in the formula. */
function floorBinds(total, floor, n) {
  return total > 0 && floor > 0 && total / Math.max(1, n) < floor;
}

/** Routes with at least one model of their own. A route with none falls back
 *  to MODEL, so counting it would double-count the default route. */
function calculatorRoutes() {
  return [
    ...ROUTE_TIERS.map((tier) => ({
      label: tier.label,
      modelKey: tier.modelKey,
      chainKey: tier.chainKey,
    })),
    { label: "Vision", modelKey: "MODEL_VISION", chainKey: "MODEL_VISION_FALLBACKS" },
  ]
    .map((route) => ({ label: route.label, models: chainLength(route.modelKey, route.chainKey) }))
    .filter((route) => route.models > 0);
}

function formatShare(seconds) {
  return Number.isFinite(seconds) ? `${Math.round(seconds)} s` : "no limit";
}

/** Append an empty live readout under a rendered field and point the input at
 *  it, joined with whatever `renderField` already referenced. */
function attachHint(wrapper) {
  const hint = document.createElement("p");
  hint.className = "field-hint";
  hint.id = `hint-${wrapper.dataset.key}`;
  // Deliberately not a live region. It is referenced by aria-describedby, so
  // it is read when the field it belongs to takes focus, and it only ever
  // changes because of what the reader just typed into that same field.
  // Nine competing polite regions on one page announce over each other.
  wrapper.appendChild(hint);
  const input = wrapper.querySelector("input, select, textarea");
  if (input) describedBy(input, hint.id);
  return hint;
}

function describedBy(input, id) {
  const ids = (input.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
  if (!ids.includes(id)) ids.push(id);
  input.setAttribute("aria-describedby", ids.join(" "));
}

function renderDeadlines(fields) {
  const wrap = document.createElement("div");
  const grid = document.createElement("div");
  grid.className = "field-grid";
  fields.forEach((field) => grid.appendChild(renderField(field)));
  wrap.appendChild(grid);

  const card = document.createElement("div");
  card.className = "calc-card";
  const title = document.createElement("h4");
  title.textContent = "What each model actually gets";
  const headline = document.createElement("p");
  headline.className = "calc-line";
  headline.id = "calcHeadline";
  headline.setAttribute("aria-live", "polite");
  const formula = document.createElement("p");
  formula.className = "calc-formula";
  formula.id = "calcFormula";
  const warning = document.createElement("p");
  warning.className = "calc-warning";
  warning.id = "calcWarning";
  warning.hidden = true;
  const table = document.createElement("table");
  table.className = "calc-table";
  table.id = "calcTable";
  const caveat = document.createElement("p");
  caveat.className = "calc-caveat";
  caveat.textContent =
    "Time an attempt does not use flows to the models behind it, so this is " +
    "the worst case for the first model on the route, not a fixed slot. A " +
    "model that has started thinking is governed by the thinking deadline " +
    "above instead, while the fall-back-when-a-model-only-thinks switch is on.";
  card.append(title, headline, formula, warning, table, caveat);
  wrap.appendChild(card);

  // Delegated, because the chain editor rebuilds its rows on every reorder and
  // would strand a listener bound to a removed row. Registered once: a second
  // renderSections() after load() must not stack recomputes.
  if (!calcListenerBound) {
    calcListenerBound = true;
    const recompute = (event) => {
      const key = event.target && event.target.dataset ? event.target.dataset.key : null;
      if (key && CALC_KEYS.has(key)) updateDeadlineCalculator();
    };
    document.addEventListener("input", recompute);
    document.addEventListener("change", recompute);
  }
  // The card is not in the document yet, so scope the first paint to it.
  updateDeadlineCalculator(card);
  return wrap;
}

function updateDeadlineCalculator(root) {
  const scope = root || document;
  const headline = scope.querySelector("#calcHeadline");
  if (!headline) return;
  const formula = scope.querySelector("#calcFormula");
  const warning = scope.querySelector("#calcWarning");
  const table = scope.querySelector("#calcTable");

  const total = calcNumber("FALLBACK_TOTAL_TIMEOUT");
  const first = calcNumber("FALLBACK_FIRST_TOKEN_TIMEOUT");
  const floor = calcNumber("FALLBACK_ATTEMPT_SHARE_FLOOR");
  const httpRead = calcNumber("HTTP_READ_TIMEOUT");
  const shutdown = calcNumber("SERVER_GRACEFUL_SHUTDOWN_SECONDS");
  const routes = calculatorRoutes().map((route) => ({
    ...route,
    share: firstTokenShare(total, first, floor, route.models),
  }));

  table.replaceChildren();
  const head = document.createElement("tr");
  ["Route", "Models", "First-token share"].forEach((text) => {
    const cell = document.createElement("th");
    cell.textContent = text;
    head.appendChild(cell);
  });
  table.appendChild(head);
  // Model names are user text: built with textContent, never interpolated.
  routes.forEach((route) => {
    const row = document.createElement("tr");
    [route.label, String(route.models), formatShare(route.share)].forEach((text) => {
      const cell = document.createElement("td");
      cell.textContent = text;
      row.appendChild(cell);
    });
    table.appendChild(row);
  });

  if (total === 0 && first === 0) {
    headline.textContent =
      "No first-token deadline is set: a silent model holds the request until " +
      `the transport gives up (HTTP read timeout, currently ${httpRead || 300} s).`;
    formula.textContent = "";
    warning.hidden = true;
    return;
  }
  if (routes.length === 0) {
    headline.textContent =
      "No route names a model of its own yet, so there is nothing to divide " +
      "the request budget between.";
    formula.textContent = "";
    warning.hidden = true;
    return;
  }

  const longest = routes.reduce((best, route) => (route.models > best.models ? route : best));
  headline.textContent =
    `With your longest chain (${longest.label}, ${longest.models} models), each ` +
    `model gets about ${formatShare(longest.share)} to produce its first token.`;

  const bound = floorBinds(total, floor, longest.models);
  const terms = [];
  if (first > 0) terms.push(`the first-token deadline (${first} s)`);
  if (total > 0) {
    // Name the floor only where it is doing the deciding, so the line stays
    // an explanation of this route rather than a tour of the settings.
    const raw = `${total} ÷ ${longest.models} = ${formatShare(total / longest.models)}`;
    terms.push(
      bound
        ? `its share of the total budget (${raw}, raised to the ${floor} s ` +
            "silent-attempt floor)"
        : `its share of the total budget (${raw})`,
    );
  }
  formula.textContent =
    terms.length > 1 ? `= the smaller of ${terms.join(" and ")}.` : `= ${terms[0]}.`;

  // One warning at a time, most severe first. The word "Warning" carries the
  // meaning; the colour is redundant.
  let text = "";
  const effective = total > 0 && floor > 0 ? Math.min(Math.max(total / longest.models, floor), total) : total / longest.models;
  if (first > 0 && total > 0 && effective < first) {
    text =
      "Warning: The first-token deadline never applies on this route -- the " +
      "budget share is smaller. A total budget of " +
      `${Math.ceil(first * longest.models)} s would give every model the ` +
      `${first} s you asked for` +
      (floor > 0 ? `, or raise the silent-attempt floor to ${first} s.` : ".");
  } else if (bound && floor * longest.models > total) {
    // The trade the floor makes, stated rather than hidden: raising a share
    // above the equal division has to come out of the models behind it.
    const covered = Math.floor(total / floor);
    text =
      `Warning: ${longest.models} models at the ${floor} s floor add up to ` +
      `${floor * longest.models} s, more than the ${total} s budget. The ` +
      `first ${covered} silent ${covered === 1 ? "model" : "models"} can use ` +
      "the whole floor; the models after them get whatever is left, then " +
      "nothing. Lower the floor, shorten the chain, or raise the budget to " +
      `${floor * longest.models} s.`;
  } else if (httpRead > 0 && Number.isFinite(longest.share) && httpRead < longest.share) {
    text =
      `Warning: HTTP read timeout (${httpRead} s) is below the deadline above, ` +
      "so a slow model produces a transport error instead of a clean handover.";
  } else if (shutdown > 0 && total > 0 && shutdown < total) {
    text =
      `Warning: A reload force-drops requests after ${shutdown} s, before the ` +
      `${total} s budget expires.`;
  }
  warning.textContent = text;
  warning.hidden = !text;
}

const BENCH_RATE_KEYS = [
  "FALLBACK_EJECT_WINDOW",
  "FALLBACK_EJECT_FAILURE_RATE",
  "FALLBACK_EJECT_MIN_SAMPLES",
];
const BENCH_LEGACY_KEYS = ["FALLBACK_EJECT_AFTER_FAILURES"];
const BENCH_SHARED_KEYS = [
  "FALLBACK_EJECT_SECONDS",
  "FALLBACK_RETRY_FIRST",
  "FALLBACK_COOLDOWN_STEP_OVER_FLOOR",
];

/** A control the manifest locked stays locked when a mode group re-enables. */
function isLockedControl(el) {
  const key = el.dataset ? el.dataset.key : null;
  if (!key) return false;
  return Boolean((state.fields.get(key) || {}).locked);
}

function applyBenchMode(root, mode, benchEnabled) {
  root.querySelectorAll("[data-bench-mode]").forEach((group) => {
    const inert = !benchEnabled || group.dataset.benchMode !== mode;
    group.classList.toggle("is-inert", inert);
    group.querySelectorAll("input, select, textarea, button").forEach((el) => {
      el.disabled = inert || isLockedControl(el);
    });
    group.querySelector(".bench-group-note").textContent = inert
      ? benchEnabled
        ? `Not used while eject mode is ${mode}.`
        : "Not used while benching is off."
      : "";
  });
  // changedValues() already skips a disabled control, so the counter has to be
  // re-read at the moment of the switch or the drop is only discovered at Apply.
  updateDirtyState();
}

function benchGroup(mode, legendText, keys, fieldByKey) {
  const group = document.createElement("fieldset");
  group.className = "bench-group";
  group.dataset.benchMode = mode;
  const legend = document.createElement("legend");
  legend.textContent = legendText;
  const note = document.createElement("p");
  note.className = "bench-group-note";
  const grid = document.createElement("div");
  grid.className = "field-grid";
  keys.forEach((key) => {
    const field = fieldByKey.get(key);
    if (field) grid.appendChild(renderField(field));
  });
  group.append(legend, note, grid);
  return group;
}

function benchHintText(key) {
  const value = Number(liveValue(key));
  switch (key) {
    case "FALLBACK_EJECT_WINDOW":
    case "FALLBACK_EJECT_FAILURE_RATE": {
      const window = Number(liveValue("FALLBACK_EJECT_WINDOW"));
      const rate = Number(liveValue("FALLBACK_EJECT_FAILURE_RATE"));
      if (!Number.isFinite(window) || !Number.isFinite(rate) || window <= 0 || rate <= 0) return "";
      return `benched after ${Math.ceil(window * rate)} of the last ${window} requests fail`;
    }
    case "FALLBACK_EJECT_MIN_SAMPLES":
      if (!Number.isFinite(value) || value <= 0) return "";
      return `no model is benched until ${value} of its requests have been seen`;
    case "FALLBACK_EJECT_AFTER_FAILURES":
      if (!Number.isFinite(value)) return "";
      return value <= 0 ? "benching by count is off" : `${value} failures in a row`;
    case "FALLBACK_EJECT_SECONDS":
      if (!Number.isFinite(value) || value < 0) return "";
      return `a benched model stays out for ${formatSeconds(value)}`;
    case "FALLBACK_COOLDOWN_STEP_OVER_FLOOR":
      if (!Number.isFinite(value) || value < 0) return "";
      return `a model whose cooldown has ${formatSeconds(value)} or less left is tried anyway`;
    default:
      return "";
  }
}

function renderBenching(fields) {
  const fieldByKey = new Map(fields.map((field) => [field.key, field]));
  const wrap = document.createElement("div");
  const hints = new Map();

  const master = document.createElement("div");
  master.className = "bench-master";
  const enabled = fieldByKey.get("FALLBACK_BENCH_ENABLED");
  // No consequence line of its own: the field's own help already says that
  // OFF makes every Eject setting below inert, and saying it twice in one
  // card reads as two different rules.
  if (enabled) master.appendChild(renderField(enabled));
  const alsoOn = document.createElement("p");
  alsoOn.className = "bench-crosslink";
  alsoOn.append(
    document.createTextNode("The same switch is on "),
  );
  const alsoLink = document.createElement("a");
  alsoLink.href = "#";
  alsoLink.textContent = "Model Config";
  alsoLink.addEventListener("click", (event) => {
    event.preventDefault();
    setActiveView("model_config", { scroll: true });
  });
  alsoOn.append(
    alsoLink,
    document.createTextNode(", beside the routes it applies to — one setting, two places to reach it."),
  );
  master.appendChild(alsoOn);
  wrap.appendChild(master);

  const modeRow = document.createElement("div");
  modeRow.className = "bench-mode";
  const behavior = fieldByKey.get("FALLBACK_BEHAVIOR");
  if (behavior) modeRow.appendChild(renderField(behavior));
  const modeNote = document.createElement("p");
  modeNote.className = "bench-group-note";
  modeNote.textContent =
    "The unused mode's controls stay on the page but are not saved, so " +
    "switching mode drops a pending edit to them from the next Apply.";
  modeRow.appendChild(modeNote);
  wrap.appendChild(modeRow);

  wrap.appendChild(benchGroup("rate_based", "Rate-based ejection", BENCH_RATE_KEYS, fieldByKey));
  wrap.appendChild(benchGroup("legacy", "Legacy ejection", BENCH_LEGACY_KEYS, fieldByKey));

  const shared = document.createElement("div");
  shared.className = "field-grid";
  BENCH_SHARED_KEYS.forEach((key) => {
    const field = fieldByKey.get(key);
    if (field) shared.appendChild(renderField(field));
  });
  wrap.appendChild(shared);

  // FALLBACK_SKIP_KINDS is a routing decision and renders once, on Model
  // Config. A setting rendered on two pages is a setting that can show two
  // answers, and changedValues() would submit whichever control it walked last.
  const crosslink = document.createElement("p");
  crosslink.className = "bench-crosslink";
  crosslink.append(
    document.createTextNode(
      "Which failure kinds end a route instead of trying the next model is set on ",
    ),
  );
  const link = document.createElement("a");
  link.href = "#";
  link.textContent = "Model Config";
  link.addEventListener("click", (event) => {
    event.preventDefault();
    setActiveView("model_config", { scroll: true });
    const target = byId("field-FALLBACK_SKIP_KINDS");
    if (target) target.focus();
  });
  crosslink.append(link, document.createTextNode("."));
  wrap.appendChild(crosslink);

  const claimed = new Set([
    "FALLBACK_BENCH_ENABLED",
    "FALLBACK_BEHAVIOR",
    ...BENCH_RATE_KEYS,
    ...BENCH_LEGACY_KEYS,
    ...BENCH_SHARED_KEYS,
  ]);
  const unclaimed = fields.filter((field) => !claimed.has(field.key));
  if (unclaimed.length) {
    const rest = document.createElement("div");
    rest.className = "field-grid";
    unclaimed.forEach((field) => rest.appendChild(renderField(field)));
    wrap.appendChild(rest);
  }

  [...BENCH_RATE_KEYS, ...BENCH_LEGACY_KEYS, ...BENCH_SHARED_KEYS].forEach((key) => {
    const wrapper = wrap.querySelector(`.field[data-key="${key}"]`);
    if (wrapper) hints.set(key, attachHint(wrapper));
  });
  const paintHints = () => {
    hints.forEach((hint, key) => {
      hint.textContent = benchHintText(key);
    });
  };
  paintHints();

  const modeInput = modeRow.querySelector(CONTROL_FOR_KEY("FALLBACK_BEHAVIOR"));
  const enabledInput = master.querySelector(CONTROL_FOR_KEY("FALLBACK_BENCH_ENABLED"));
  const apply = () => {
    // An unset select reads "" and would match neither mode, leaving both
    // groups inert on first paint -- read what the control effectively means.
    const mode = modeInput ? effectiveControlValue(modeInput) : "rate_based";
    const on = enabledInput ? effectiveControlValue(enabledInput) !== "false" : true;
    applyBenchMode(wrap, mode, on);
  };
  if (modeInput) modeInput.addEventListener("change", apply);
  if (enabledInput) enabledInput.addEventListener("change", apply);
  wrap.addEventListener("input", paintHints);
  wrap.addEventListener("change", paintHints);
  apply();
  return wrap;
}

/** 86400 reads "24h" through formatSeconds; a lockout ladder is read in days. */
function formatLockoutSpan(seconds) {
  const value = Math.max(0, Math.round(seconds));
  if (value >= 86400 && value % 86400 === 0) return `${value / 86400}d`;
  return formatSeconds(value);
}

function describeLockoutTiers(raw) {
  const parts = String(raw)
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
  const seconds = parts.map(Number);
  if (!seconds.length || seconds.some((value) => !Number.isFinite(value) || value <= 0)) {
    return "Enter one or more positive numbers of seconds, separated by commas.";
  }
  const ordinals = ["1st", "2nd", "3rd"];
  return seconds
    .map((value, index) => {
      const ordinal = ordinals[index] || `${index + 1}th`;
      const last = index === seconds.length - 1;
      const lead = index === 0 ? `${ordinal} auth failure` : ordinal;
      return `${lead}${last ? " and after" : ""}: ${formatLockoutSpan(value)} out`;
    })
    .join(" · ");
}

function poolNameFromKey(key) {
  return key
    .replace(/_(API_KEY|TOKEN|KEY)$/, "")
    .toLowerCase();
}

function renderCredentialHealth(fields) {
  const wrap = document.createElement("div");
  const grid = document.createElement("div");
  grid.className = "field-grid";
  const wrappers = new Map();
  fields.forEach((field) => {
    const rendered = renderField(field);
    wrappers.set(field.key, rendered);
    grid.appendChild(rendered);
  });
  wrap.appendChild(grid);

  const tiers = wrappers.get("CREDENTIAL_LOCKOUT_TIERS");
  if (tiers) {
    const hint = attachHint(tiers);
    const paint = () => {
      hint.textContent = describeLockoutTiers(liveValue("CREDENTIAL_LOCKOUT_TIERS"));
    };
    const input = tiers.querySelector("input, select, textarea");
    if (input) {
      input.addEventListener("input", paint);
      input.addEventListener("change", paint);
    }
    paint();
  }

  const rule = document.createElement("p");
  rule.className = "field-description";
  const paintRule = () => {
    // calcNumber, not liveValue: an untouched field renders blank and its
    // default lives in the placeholder, so reading the blank as 0 would claim
    // the key is never benched when the server is applying 2.
    const escalation = state.fields.has("CREDENTIAL_MODEL_BENCH_ESCALATION")
      ? calcNumber("CREDENTIAL_MODEL_BENCH_ESCALATION")
      : 2;
    // The mode decides whether any of the sentence below is true at all, so
    // it is read first. An untouched select still renders its current option,
    // so liveValue is right here; the fallback is the shipped default and not
    // an empty string, because "" is not one of the three answers.
    const mode = state.fields.has("RATE_LIMIT_COOLDOWN_MODE")
      ? liveValue("RATE_LIMIT_COOLDOWN_MODE").trim() || "provider"
      : "provider";
    let scope;
    if (mode === "off") {
      rule.textContent =
        "A 401/403 walks the lockout ladder; a 429 benches nothing at all — " +
        "MCC still rotates to the next key and still moves down the fallback " +
        "chain, so the provider goes on answering 429 and MCC goes on " +
        "spending attempts on it; a timeout or 5xx costs a key nothing.";
      return;
    }
    const benchWindow =
      mode === "fixed"
        ? "the cooldown above, whatever the provider published"
        : "the provider's Retry-After or the cooldown above when it sends none";
    if (escalation === 1) {
      scope = `a 429 benches the whole key for ${benchWindow}`;
    } else if (escalation <= 0) {
      scope =
        "a 429 benches only the model it happened on, on that key, and never " +
        "the whole key";
    } else {
      scope =
        "a 429 benches only the model it happened on, on that key, for " +
        `${benchWindow} — the key itself is benched once ${escalation} different ` +
        "models are rate-limited on it at the same time";
    }
    rule.textContent = `A 401/403 walks the lockout ladder; ${scope}; a timeout or 5xx costs a key nothing.`;
  };
  for (const key of [
    "CREDENTIAL_MODEL_BENCH_ESCALATION",
    "RATE_LIMIT_COOLDOWN_MODE",
  ]) {
    const field = wrappers.get(key);
    if (!field) continue;
    const input = field.querySelector("input, select, textarea");
    if (input) {
      input.addEventListener("input", paintRule);
      input.addEventListener("change", paintRule);
    }
  }
  paintRule();
  wrap.appendChild(rule);

  // Rotation is per pool and the provider card owns it; this is a readout, and
  // the credential value is masked server-side so no key count is invented.
  const pools = [];
  state.fields.forEach((field, key) => {
    if (!key.endsWith("_ROTATION")) return;
    const credentialKey = key.slice(0, -"_ROTATION".length);
    const credential = state.fields.get(credentialKey);
    if (!credential || !credential.configured) return;
    pools.push({
      // Websearch rotation fields carry no provider, so the env key is the
      // only name there is: read it as a pool name rather than as shouting.
      label: field.provider || poolNameFromKey(credentialKey),
      mode: String(field.value || field.default || "").trim() || "default",
      credentialKey,
    });
  });
  if (pools.length) {
    const heading = document.createElement("p");
    heading.className = "rotation-summary-heading";
    heading.textContent = "Rotation, per pool";
    wrap.appendChild(heading);
    const list = document.createElement("ul");
    list.className = "rotation-summary";
    pools.forEach((pool) => {
      const item = document.createElement("li");
      item.append(document.createTextNode(`${pool.label} — ${pool.mode} `));
      const open = document.createElement("a");
      open.href = "#";
      open.dataset.openProvider = pool.label;
      open.textContent = "Open provider card";
      open.addEventListener("click", (event) => {
        event.preventDefault();
        state.reopenKeyManager = pool.credentialKey;
        setActiveView("providers", { scroll: true });
      });
      item.appendChild(open);
      list.appendChild(item);
    });
    wrap.appendChild(list);
  }
  return wrap;
}

// Wave-2 cross-lane contract: the config GET payload carries a top-level
// `messaging_auth_open` array naming every platform any unauthenticated
// client can message right now ([] once every platform is locked behind an
// allowlist). An open install is a security posture the reader should not
// have to reverse-engineer from env vars, so it says so on the Messaging
// page itself. Hidden entirely while the array is empty.
function renderMessagingAuthNotice(openPlatforms) {
  const view = byId("view-messaging");
  const sections = byId("messagingSections");
  if (!view || !sections) return;
  byId("messagingAuthNotice")?.remove();
  const platforms = Array.isArray(openPlatforms)
    ? openPlatforms.filter(
        (platform) => typeof platform === "string" && platform.trim() !== "",
      )
    : [];
  if (platforms.length === 0) return;
  const notice = document.createElement("p");
  notice.id = "messagingAuthNotice";
  notice.className = "analytics-warning";
  notice.textContent =
    "Messaging auth is OPEN: anyone can message these platforms: " +
    `${platforms.join(", ")}. Set TELEGRAM_ALLOWED_USER_ID / ` +
    "DISCORD_ALLOWED_CHANNEL_IDS.";
  view.insertBefore(notice, sections);
}

/* Per-section renderers. A section absent from this table renders as the
   generic field grid, which is still what most sections want; adding a
   renderer here must never change how any other section renders.

   `providers` is grouped, searchable cards rather than catalog order in one
   flat grid, which stopped scaling past 30 providers to scan; each provider's
   own advanced fields (proxy, etc.) move into that provider's card instead of
   floating in the same grid. */
const SECTION_RENDERERS = {
  models: renderModelRouting,
  optimizer: renderOptimizerSettings,
  providers: renderProviderGroups,
  deadlines: renderDeadlines,
  benching: renderBenching,
  credential_health: renderCredentialHealth,
  loop_health: renderLoopHealth,
};

/* ------------------------------------------------------------------ *
 * Advanced fields: ordered and tagged, never hidden by default.
 *
 * `advanced` used to mean `display: none` behind a per-card "Show advanced"
 * button. A reader looking for RATE_LIMIT_COOLDOWN_MODE on Limits &
 * Resilience found CREDENTIAL_LOCKOUT_TIERS beside it and concluded the
 * cooldown settings did not exist -- and the providers section never grew
 * that button at all, so 110 provider fields had no control anywhere.
 *
 * Now `advanced` only sorts a field after the common ones in its card and
 * tags it. A collapse control stays for people who want the short form: it
 * is opt-in, per card, and remembered per browser.
 * ------------------------------------------------------------------ */

const ADVANCED_COLLAPSE_PREFIX = "mcc.advancedCollapsed.";

/** Is this card's advanced block collapsed for this browser?
 *
 * Absent, unreadable (private mode, blocked site data) and malformed all
 * answer "no": expanded is the default the whole feature rests on, so the
 * failure mode of storage must be the default, never a blank card.
 */
function advancedCollapsed(scopeKey) {
  try {
    return window.localStorage.getItem(ADVANCED_COLLAPSE_PREFIX + scopeKey) === "1";
  } catch {
    return false;
  }
}

/** Remember one card's collapse choice. Expanded is stored as *absence*, so a
 *  reader who never touches the control leaves no key behind. */
function rememberAdvancedCollapsed(scopeKey, collapsed) {
  try {
    const name = ADVANCED_COLLAPSE_PREFIX + scopeKey;
    if (collapsed) window.localStorage.setItem(name, "1");
    else window.localStorage.removeItem(name);
  } catch {
    /* Storage is a convenience here; the page works without it. */
  }
}

/** Common fields first, advanced after, original order kept inside each half.
 *
 * `Array.prototype.sort` is specified stable, so this is the whole ordering
 * rule: `advanced` moves a field down, it never reshuffles its neighbours.
 */
function advancedLast(fields) {
  return [...fields].sort(
    (left, right) => Number(Boolean(left.advanced)) - Number(Boolean(right.advanced)),
  );
}

/** Attach the per-card "Collapse advanced" control to `container`.
 *
 * `container` is the element the CSS collapse rule is written against (a
 * `.settings-section` or a `.pv-card`). `scopeKey` namespaces the stored
 * choice so two cards never share one memory. The control is appended to
 * `host`, which defaults to the container.
 */
function attachAdvancedCollapse(container, scopeKey, host) {
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "ghost-button advanced-toggle";
  const paint = (collapsed) => {
    toggle.textContent = collapsed ? "Show advanced" : "Collapse advanced";
    toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
  };
  const collapsed = advancedCollapsed(scopeKey);
  if (collapsed) container.classList.add("collapse-advanced");
  paint(collapsed);
  toggle.addEventListener("click", () => {
    const nowCollapsed = container.classList.toggle("collapse-advanced");
    paint(nowCollapsed);
    rememberAdvancedCollapsed(scopeKey, nowCollapsed);
  });
  (host || container).appendChild(toggle);
  return toggle;
}

function renderSections(sections, fields) {
  state.modelComboboxes.clear();
  // Rebuilt rails mean stale editors and stale ids; the drag's whole state is
  // view state and must not survive a re-render.
  state.routeRails.clear();
  state.routeSelection.clear();
  state.routeAnchorId = null;
  state.routeArrowRange = [];
  state.routeUndo = null;
  VIEW_GROUPS.forEach((view) => {
    // Static views (the guide) have no settings container to clear.
    const container = view.containerId ? byId(view.containerId) : null;
    if (container) container.innerHTML = "";
  });

  const sectionById = new Map(sections.map((section) => [section.id, section]));
  const bySection = new Map();
  sections.forEach((section) => bySection.set(section.id, []));
  fields.forEach((field) => {
    if (!bySection.has(field.section)) bySection.set(field.section, []);
    bySection.get(field.section).push(field);
  });

  VIEW_GROUPS.forEach((view) => {
    const container = view.containerId ? byId(view.containerId) : null;
    if (!container) return;
    view.sections.forEach((sectionId) => {
      const section = sectionById.get(sectionId);
      const sectionFields = bySection.get(sectionId) || [];
      if (!section || sectionFields.length === 0) return;

      const sectionEl = document.createElement("section");
      sectionEl.className = "settings-section";
      sectionEl.id = `section-${section.id}`;

      // Rotation selects are rendered inside the credential key manager
      // instead of the generic grid. Websearch advanced option fields are
      // rendered inside the provider cards' collapsed groups.
      // Advanced last, common first -- the only thing `advanced` now does to
      // a field's placement. Sorted before the renderer sees the list so a
      // section with its own renderer gets the same order as the grid.
      const gridFields = advancedLast(
        sectionFields.filter(
          (field) =>
            !field.key.endsWith("_ROTATION") &&
            !(field.section === "websearch" && field.advanced),
        ),
      );

      const heading = document.createElement("div");
      heading.className = "section-heading";
      // label/description come from the manifest and can carry custom-provider
      // display names, so they render as text nodes -- never as markup.
      const headingText = document.createElement("div");
      const headingLabel = document.createElement("h3");
      headingLabel.textContent = section.label;
      const headingDescription = document.createElement("p");
      headingDescription.textContent = section.description;
      headingText.append(headingLabel, headingDescription);
      heading.appendChild(headingText);
      if (section.id === "models") {
        const refreshButton = document.createElement("button");
        refreshButton.type = "button";
        refreshButton.className = "secondary-button";
        refreshButton.textContent = "Refresh models";
        refreshButton.addEventListener("click", () => refreshModelOptions(refreshButton));
        heading.appendChild(refreshButton);
      }
      sectionEl.appendChild(heading);

      const renderer = SECTION_RENDERERS[section.id];
      if (renderer) {
        // Second argument: every field, not just this section's. A renderer
        // that shows a control owned by another section (the Chain benching
        // master switch on Model Config) needs the spec, and reaching for it
        // through a module global is how two pages start disagreeing.
        sectionEl.appendChild(renderer(gridFields, fields));
      } else {
        const grid = document.createElement("div");
        grid.className = "field-grid";
        gridFields.forEach((field) => {
          grid.appendChild(renderField(field));
        });
        sectionEl.appendChild(grid);
      }

      // The providers section collapses per provider card (see
      // renderProviderCard) rather than for all 35 cards at once.
      if (section.id !== "providers" && gridFields.some((field) => field.advanced)) {
        attachAdvancedCollapse(sectionEl, `section:${section.id}`);
      }

      container.appendChild(sectionEl);
    });
  });

  // The limits rail's targets are rendered, not static markup, so the observer
  // cannot be set up at script evaluation time the way the guide's is. Guarded
  // so a re-render after load() does not stack observers.
  if (!limitsScrollspyBound && document.querySelector("#limitsToc a")) {
    limitsScrollspyBound = true;
    setupScrollspy("#limitsToc");
  }
}

/** The Server responsiveness card: its settings, and what they measured.
 *
 * The fields decide when a /health probe is told the loop is late and which
 * gesture it names. Underneath them, the gestures that have already finished
 * and the worst lateness each one caused -- the same table the perf specs
 * record by hand, read off the running server instead.
 */
function renderLoopHealth(fields) {
  const wrap = document.createElement("div");
  const grid = document.createElement("div");
  grid.className = "field-grid";
  fields.forEach((field) => grid.appendChild(renderField(field)));
  wrap.appendChild(grid);

  const card = document.createElement("div");
  card.className = "calc-card";
  const title = document.createElement("h4");
  title.textContent = "What recent gestures cost the event loop";
  const readout = document.createElement("div");
  readout.id = "loopLagReadout";
  readout.setAttribute("aria-live", "polite");
  const caveat = document.createElement("p");
  caveat.className = "calc-caveat";
  caveat.textContent =
    "Newest first, and only gestures that named themselves. The lateness is " +
    "measured against the heartbeat above, so it is how late the loop was " +
    "for everything else -- a request, a probe, the desktop window's health " +
    "check -- while that gesture ran.";
  card.append(title, readout, caveat);
  wrap.appendChild(card);

  /* The stuck-request watchdog's readout. Read-only and deliberately small:
     the count of requests that have crossed the threshold right now, and a
     link that hands the reader the whole frames-only document. There is no
     new page, because the answer to "is anything stuck" is one number and the
     answer to "what is it stuck on" is a file somebody pastes into a bug
     report. */
  const stuck = document.createElement("div");
  stuck.className = "calc-card";
  const stuckTitle = document.createElement("h4");
  stuckTitle.textContent = "Requests that have stopped making progress";
  const stuckReadout = document.createElement("div");
  stuckReadout.id = "stuckRequestsReadout";
  stuckReadout.setAttribute("aria-live", "polite");
  const stuckLink = document.createElement("a");
  stuckLink.id = "stuckStacksLink";
  stuckLink.className = "calc-caveat";
  stuckLink.href = "/admin/api/tasks/stacks";
  stuckLink.target = "_blank";
  stuckLink.rel = "noopener";
  stuckLink.textContent = "Download stacks";
  const stuckCaveat = document.createElement("p");
  stuckCaveat.className = "calc-caveat";
  stuckCaveat.textContent =
    "Code locations only -- file, line and function -- never a prompt, a " +
    "header, a key or any value. The watchdog observes and never intervenes: " +
    "nothing here ends, cancels, retries or changes a request.";
  stuck.append(stuckTitle, stuckReadout, stuckLink, stuckCaveat);
  wrap.appendChild(stuck);
  return wrap;
}

/** Paint the per-gesture loop-lag table from /admin/api/loop-health.
 *
 * The busy reason on the Server responsiveness card names the gesture the
 * loop is inside *right now*; this is the other half -- what each finished
 * gesture cost -- so the before/after table in the perf specs is reproducible
 * from the dashboard rather than only from a harness.
 *
 * Tolerant of a server that does not send the key: an older server, or one
 * whose monitor never armed, leaves the card saying nothing has finished,
 * which is true.
 */
async function loadLoopLag() {
  const readout = byId("loopLagReadout");
  if (!readout) return;
  const health = await api("/admin/api/loop-health");
  const gestures = Array.isArray(health.gestures) ? health.gestures : [];
  if (!gestures.length) {
    readout.textContent = "";
    const empty = document.createElement("p");
    empty.className = "calc-caveat";
    empty.textContent = "No gesture has finished in this server yet.";
    readout.appendChild(empty);
    return;
  }
  const busyMs = Number(health.busy_lag_ms) || 0;
  const table = document.createElement("table");
  table.className = "calc-table";
  table.id = "loopLagTable";
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  ["Gesture", "Max loop lag", "Took"].forEach((label) => {
    const cell = document.createElement("th");
    cell.textContent = label;
    headRow.appendChild(cell);
  });
  head.appendChild(headRow);
  table.appendChild(head);
  const body = document.createElement("tbody");
  gestures.forEach((gesture) => {
    const row = document.createElement("tr");
    const lag = Number(gesture.max_lag_ms) || 0;
    if (busyMs > 0 && lag >= busyMs) row.classList.add("calc-over-budget");
    const reason = document.createElement("td");
    reason.textContent = String(gesture.reason || "a long operation");
    const lagCell = document.createElement("td");
    lagCell.textContent = `${lag} ms`;
    lagCell.dataset.maxLagMs = String(lag);
    const took = document.createElement("td");
    took.textContent = `${Number(gesture.duration_ms) || 0} ms`;
    row.append(reason, lagCell, took);
    body.appendChild(row);
  });
  table.appendChild(body);
  readout.textContent = "";
  readout.appendChild(table);
}

/** Paint the stuck-request count from /admin/api/tasks/stacks.
 *
 * One fetch, no database work on the server, and the endpoint is bounded on
 * both axes. Tolerant of a server that does not have the route -- an older
 * one, or a build with the watchdog compiled out of the operator's mind --
 * because a missing readout must not blank the card above it.
 *
 * Note `Number(x) || 0` is wrong for a count that is meaningfully zero only if
 * absence and zero must read differently; here they do not, and `stuck: 0` and
 * no answer at all are painted with different sentences on purpose.
 */
async function loadStuckRequests() {
  const readout = byId("stuckRequestsReadout");
  if (!readout) return;
  const report = await api("/admin/api/tasks/stacks");
  const inFlight = Number(report.in_flight);
  const stuck = Number(report.stuck);
  const stall = Number(report.stall_seconds);
  const line = document.createElement("p");
  line.className = "calc-caveat";
  line.id = "stuckRequestsLine";
  if (report.watchdog_enabled === false) {
    line.textContent =
      "The watchdog is off. Nothing is being watched, so nothing can be " +
      "reported the next time a request goes quiet.";
  } else if (!Number.isFinite(stuck) || !Number.isFinite(inFlight)) {
    line.textContent = "This server did not answer with a count.";
  } else if (stuck === 0) {
    line.textContent =
      `${inFlight} request${inFlight === 1 ? "" : "s"} in flight, none still ` +
      `for ${Number.isFinite(stall) ? stall : "?"} s or more.`;
  } else {
    line.textContent =
      `${stuck} of ${inFlight} in-flight request${inFlight === 1 ? "" : "s"} ` +
      `ha${stuck === 1 ? "s" : "ve"} made no progress for ` +
      `${Number.isFinite(stall) ? stall : "?"} s or more.`;
  }
  readout.textContent = "";
  readout.appendChild(line);
}

function prefersReducedMotion() {
  return (
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

/** Render the providers section as one flat, searchable grid of cards.
 *
 * There are 35 built-in providers and each one is a small form, not a single
 * field: a pool of keys with per-key health, a rotation policy, an optional
 * proxy and base URL, and a model refresh. All 35 forms cannot be open at
 * once, so a card shows a summary and expands in place to hold the whole
 * form. That keeps a provider's status and its key fields on ONE card, which
 * is what the old status-strip-plus-separate-field-grid layout got wrong.
 *
 * Deliberately flat: no category grouping, and nothing collapsed by default
 * beyond a card's own body. Grouping providers by kind hid most of the page
 * behind headings and made finding one harder rather than easier.
 *
 * Reads `field.provider` and `provider_status[]` defensively -- a custom
 * provider may carry neither, and anything that cannot be attributed to a
 * provider still renders in an "Other configuration" grid rather than
 * disappearing.
 */
function renderProviderGroups(fields) {
  const wrap = document.createElement("div");
  wrap.className = "pv-wrap";

  const statusById = new Map(
    (state.config?.provider_status || []).map((provider) => [
      provider.provider_id,
      provider,
    ]),
  );

  const fieldsByProvider = new Map();
  const unclaimed = [];
  fields.forEach((field) => {
    if (!field.provider) {
      unclaimed.push(field);
      return;
    }
    if (!fieldsByProvider.has(field.provider)) fieldsByProvider.set(field.provider, []);
    fieldsByProvider.get(field.provider).push(field);
  });

  wrap.appendChild(renderProviderToolbar(wrap));

  const grid = document.createElement("div");
  grid.className = "provider-grid pv-grid";
  // Card order follows provider_status, which is catalog order, so related
  // gateways stay adjacent. Ordering by first-field-seen instead put OpenCode
  // Go last on the page -- its only field was an advanced proxy, generated
  // after every credential -- which is nowhere near the OpenCode Zen card it
  // shares an account with.
  const ordered = [
    ...[...statusById.keys()].filter((id) => fieldsByProvider.has(id)),
    ...[...fieldsByProvider.keys()].filter((id) => !statusById.has(id)),
  ];
  ordered.forEach((providerId) => {
    const provider = statusById.get(providerId) || {
      provider_id: providerId,
      display_name: providerId,
      status: "unknown",
      label: "Unknown",
    };
    grid.appendChild(renderProviderCard(provider, fieldsByProvider.get(providerId)));
  });
  wrap.appendChild(grid);

  const empty = document.createElement("p");
  empty.className = "pv-empty";
  empty.textContent =
    "No provider matches that. Try part of the name, or the variable name such as GROQ_API_KEY.";
  wrap.appendChild(empty);

  if (unclaimed.length) {
    const other = document.createElement("section");
    other.className = "pv-other";
    const heading = document.createElement("p");
    heading.className = "pv-other-heading";
    heading.textContent = "Other configuration";
    other.appendChild(heading);
    const otherGrid = document.createElement("div");
    otherGrid.className = "field-grid";
    unclaimed.forEach((field) => otherGrid.appendChild(renderField(field)));
    other.appendChild(otherGrid);
    wrap.appendChild(other);
  }

  return wrap;
}

function renderProviderToolbar(wrap) {
  const toolbar = document.createElement("div");
  toolbar.className = "pv-toolbar";

  const searchWrap = document.createElement("label");
  searchWrap.className = "pv-search";
  const searchLabel = document.createElement("span");
  searchLabel.className = "pv-search-label";
  searchLabel.textContent = "Search providers";
  const search = document.createElement("input");
  search.type = "search";
  search.placeholder = "Search by name, key, or URL\u2026";
  search.autocomplete = "off";
  searchWrap.append(searchLabel, search);

  const count = document.createElement("span");
  count.className = "pv-count";

  const configuredOnly = document.createElement("label");
  configuredOnly.className = "toggle-control pv-configured-toggle";
  const configuredCheckbox = document.createElement("input");
  configuredCheckbox.type = "checkbox";
  configuredOnly.append(configuredCheckbox, document.createTextNode("Only configured"));

  const apply = () =>
    applyProviderFilter(
      wrap,
      search.value.trim().toLowerCase(),
      configuredCheckbox.checked,
      count,
    );
  search.addEventListener("input", apply);
  configuredCheckbox.addEventListener("change", apply);
  // Run once after the grid exists so the count is correct on first paint.
  window.setTimeout(apply, 0);

  toolbar.append(searchWrap, count, configuredOnly);
  return toolbar;
}

// Filtering hides with the `hidden` attribute and never removes anything:
// changedValues() finds fields by walking [data-key] across the document, so a
// detached input would silently stop being saveable. `hidden` also takes an
// element out of the tab order, so a filtered-out card cannot trap focus.
function applyProviderFilter(wrap, query, configuredOnly, countEl) {
  const cards = wrap.querySelectorAll(".pv-card");
  let shown = 0;
  let configured = 0;
  cards.forEach((card) => {
    if (card.dataset.pvConfigured === "true") configured += 1;
    const matchesQuery = !query || (card.dataset.pvSearch || "").includes(query);
    const matchesConfigured = !configuredOnly || card.dataset.pvConfigured === "true";
    const show = matchesQuery && matchesConfigured;
    card.hidden = !show;
    // A filtered-out card must not stay expanded, or it reappears mid-form.
    if (!show) closeProviderCard(card);
    if (show) shown += 1;
  });
  wrap.classList.toggle("pv-no-results", shown === 0);
  if (countEl) {
    countEl.textContent =
      query || configuredOnly
        ? `${shown} of ${cards.length}`
        : `${cards.length} providers \u00b7 ${configured} configured`;
  }
}

function closeProviderCard(card) {
  card.classList.remove("pv-open");
  const configure = card.querySelector(".pv-configure");
  if (configure) {
    configure.textContent = "Configure";
    configure.setAttribute("aria-expanded", "false");
  }
}

/** One provider: a summary face, and its whole form behind Configure. */
function renderProviderCard(provider, fields) {
  const card = document.createElement("article");
  card.className = "provider-card pv-card";
  card.dataset.provider = provider.provider_id;
  card.dataset.pvConfigured = provider.status === "configured" ? "true" : "false";
  card.dataset.pvSearch = [
    provider.display_name,
    provider.provider_id,
    provider.credential_env,
    provider.base_url,
    ...fields.map((field) => field.label),
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();

  const title = document.createElement("div");
  title.className = "provider-title";
  const name = document.createElement("strong");
  name.textContent = provider.display_name || provider.provider_id;
  title.appendChild(name);
  const pill = document.createElement("span");
  pill.className = `status-pill ${statusClass(provider.status)}`;
  pill.textContent = provider.label || provider.status;
  title.appendChild(pill);
  card.appendChild(title);

  const meta = document.createElement("div");
  meta.className = "provider-meta";
  meta.textContent = providerSummaryText(provider);
  card.appendChild(meta);

  const actions = document.createElement("div");
  actions.className = "pv-actions";

  const configure = document.createElement("button");
  configure.type = "button";
  configure.className = "secondary-button pv-configure";
  configure.textContent = "Configure";
  configure.setAttribute("aria-expanded", "false");
  actions.appendChild(configure);

  if (provider.custom !== true) {
    const testButton = document.createElement("button");
    testButton.type = "button";
    testButton.className = "ghost-button test-button";
    testButton.textContent =
      provider.kind === "local" ? "Test connection" : "Refresh models";
    testButton.addEventListener("click", () =>
      testProvider(provider.provider_id, testButton),
    );
    actions.appendChild(testButton);
  }
  card.appendChild(actions);

  // Every field the provider owns, primary and advanced alike, lives here.
  // renderField() attaches the multi-key manager and its rotation select for a
  // credential field, so opening a card gives the whole key pool -- add,
  // remove, per-key health and rotation policy -- not a single input.
  const body = document.createElement("div");
  body.className = "pv-card-body";
  const ordered = advancedLast(fields);
  ordered.forEach((field) => body.appendChild(renderField(field)));
  // Two providers can be one account behind two endpoints (OpenCode Zen and
  // OpenCode Go). Only one of them owns the credential input, because a second
  // control bound to the same variable would be two ways to write one value.
  // Without the block below the other card has nothing to configure at all,
  // which is indistinguishable from broken. The key pool is addressed by
  // variable rather than by provider, so add and remove work from either card.
  const owner = provider.credential_owner_id;
  if (owner && owner !== provider.provider_id) {
    const shared = state.fields?.get(provider.credential_env);
    if (shared) body.appendChild(renderSharedCredential(provider, shared));
  } else if ((provider.credential_shared_with || []).length) {
    body.appendChild(renderSharedCredentialNote(provider));
  }
  // Until 7.29.1 a provider's advanced fields (proxy, base URL override) were
  // `display: none`, and this section was explicitly excluded from the
  // section-wide toggle -- so no control anywhere in the dashboard could
  // reveal them. They render now; the collapse is the reader's choice and is
  // remembered for this card alone.
  if (ordered.some((field) => field.advanced)) {
    attachAdvancedCollapse(card, `provider:${provider.provider_id}`, body);
  }
  card.appendChild(body);

  configure.addEventListener("click", () => {
    if (card.classList.toggle("pv-open")) {
      configure.textContent = "Done";
      configure.setAttribute("aria-expanded", "true");
      // Managing keys IS the reason to open a provider, so go straight there
      // rather than making Configure reveal a second button to press.
      card.querySelectorAll(".key-manager").forEach((manager) => {
        if (typeof manager.openKeyPool === "function") manager.openKeyPool();
      });
    } else {
      closeProviderCard(card);
    }
  });

  return card;
}

/** Manage a credential this provider borrows from another provider's card. */
function renderSharedCredential(provider, field) {
  const wrapper = document.createElement("div");
  wrapper.className = "field field-pooled";

  const label = document.createElement("label");
  const labelText = document.createElement("span");
  labelText.textContent = field.label;
  label.appendChild(labelText);

  const note = document.createElement("div");
  note.className = "field-description";
  note.textContent =
    `Shared with ${provider.credential_owner_name}: one ` +
    `${provider.credential_env} serves both. Keys and rotation can be managed ` +
    `from either card, and a change here applies to both.`;

  // Its own key manager and its own rotation select, with a card-scoped
  // element id so the duplicate control is still addressable and labelled.
  wrapper.append(
    label,
    note,
    keyManagerForField(field, { idSuffix: `--${provider.provider_id}` }),
  );
  return wrapper;
}

/** Say on the owning card that other providers draw on the same key. */
function renderSharedCredentialNote(provider) {
  const note = document.createElement("div");
  note.className = "field-description";
  const names = (provider.credential_shared_with || []).map(
    (other) => other.display_name,
  );
  note.textContent =
    `This key is also used by ${names.join(", ")}, and can be managed from ` +
    `either card.`;
  return note;
}

/** The one line on a card face that says what you actually have. */
function providerSummaryText(provider) {
  if (provider.kind === "local") {
    return provider.base_url || "No local URL configured";
  }
  const count = Number(provider.key_count || 0);
  if (count === 0) return provider.credential_env || "No key yet";
  const keys = count === 1 ? "1 key" : `${count} keys`;
  const rotation = count > 1 ? providerRotationLabel(provider) : "";
  return rotation ? `${keys} \u00b7 ${rotation}` : keys;
}

function providerRotationLabel(provider) {
  const field = state.fields?.get(`${provider.credential_env}_ROTATION`);
  const value = field?.value || field?.default || "";
  const labels = {
    single: "Single key",
    round_robin: "Round robin",
    least_used: "Least used",
    failover: "Failover",
  };
  return labels[value] || "";
}

/** Build one field's live control, wired into the dirty/apply machinery.
 *
 * Shared by the generic field grid and the Model Routing view, so a control
 * behaves identically wherever it is placed and there is one place to change
 * when a new field type appears.
 */
function buildFieldControl(field) {
  const input = inputForField(field);
  input.id = `field-${field.key}`;
  input.dataset.key = field.key;
  input.dataset.original = field.value || "";
  // The value this control falls back to when it holds nothing. Read by the
  // optimizer's proxied widgets and by "Use default", so neither has to guess
  // what an empty control means.
  input.dataset.default = field.default ?? "";
  input.dataset.secret = field.secret ? "true" : "false";
  input.dataset.configured = field.configured ? "true" : "false";
  input.dataset.fieldType = field.type;
  input.disabled = field.locked;
  if (field.type !== "oauth_login") {
    // A field may legitimately render on two pages (see the Chain benching
    // master switch). Mirroring on edit is what keeps that a second view of
    // one value rather than a second copy of it.
    const onEdit = () => {
      syncSharedControls(input);
      updateDirtyState();
    };
    input.addEventListener("input", onEdit);
    input.addEventListener("change", onEdit);
    if (field.type === "optional_model") {
      input.addEventListener("blur", () => {
        if (!input.value.trim() || input.value.trim().toLowerCase() === "none") {
          input.value = "None";
          updateDirtyState();
        }
      });
    }
  }

  // The chain editor is returned as well as rendered: a route's primary model
  // is a separate setting sitting on the same rail, and the editor is what
  // owns the ordering rules that let the two trade places.
  const editor = field.type === "model_chain" ? new ModelChainEditor(input, field) : null;
  const control =
    field.type === "model" || field.type === "optional_model"
      ? new ModelCombobox(input, field).element
      : editor
        ? editor.element
        : input;
  // A control that wraps its input must still place it in the document.
  // `changedValues()` collects fields by walking [data-key] over the page, so
  // a wrapper that keeps its input detached produces a field that looks
  // edited, never marks the form dirty, and is silently never saved. Enforced
  // here rather than trusted to each wrapper: it is one line, and the failure
  // is invisible until someone tries to save.
  if (!control.contains(input)) control.appendChild(input);
  return { input, control, editor };
}

function renderField(field) {
  const wrapper = document.createElement("div");
  wrapper.className = `field${field.advanced ? " advanced-field" : ""}`;
  wrapper.dataset.key = field.key;

  const label = document.createElement("label");
  label.htmlFor = `field-${field.key}`;
  const labelText = document.createElement("span");
  labelText.textContent = field.label;
  label.appendChild(labelText);

  // `advanced` is a label, not a hiding place: the field is on the page
  // either way, this only says it is one of the rarer knobs.
  if (field.advanced) {
    const tag = document.createElement("span");
    tag.className = "advanced-tag";
    tag.textContent = "advanced";
    label.appendChild(tag);
  }

  const source = sourceText(field);
  if (source) {
    const sourceEl = document.createElement("span");
    sourceEl.className = "field-source";
    sourceEl.textContent = source;
    label.appendChild(sourceEl);
  }

  const { input, control } = buildFieldControl(field);
  wrapper.append(label, control);
  // Bounds, then provenance, then prose. The range used to be the last
  // sentence of an up-to-80-word paragraph, which is where nobody looked.
  if (field.range_hint) {
    const hint = document.createElement("div");
    hint.className = "field-range";
    hint.id = `range-${field.key}`;
    hint.textContent = `Accepts ${field.range_hint}`;
    wrapper.appendChild(hint);
    describedBy(input, hint.id);
  }
  const meta = fieldMetaRow(field, input);
  if (meta) wrapper.appendChild(meta);
  if (field.description) {
    const description = document.createElement("div");
    description.className = "field-description";
    description.id = `desc-${field.key}`;
    description.textContent = field.description;
    wrapper.appendChild(description);
    describedBy(input, description.id);
  }
  if (
    field.secret &&
    state.credentialEnvs &&
    state.credentialEnvs.has(field.key)
  ) {
    // A provider credential is a POOL, managed by add and remove below. The
    // raw field replaced the entire comma-separated value, so offering both
    // put "enter a new value to replace" directly above a list of individual
    // keys with Remove buttons -- two different mental models, one of them
    // destructive. The control stays in the document so the shared
    // dirty/apply machinery is unchanged, but it is not shown and not
    // focusable; nothing can set it, so it never goes dirty.
    control.hidden = true;
    control.tabIndex = -1;
    control.querySelectorAll("input, select, textarea, button").forEach((node) => {
      node.tabIndex = -1;
    });
    // The label now heads the key pool, so it must not focus a hidden input.
    label.removeAttribute("for");
    wrapper.classList.add("field-pooled");
    wrapper.appendChild(keyManagerForField(field));
  }
  return wrapper;
}

/** The line under a control: what it falls back to, and a way back to it.
 *
 * A dashboard that shows a value and nothing else cannot tell you whether the
 * value is yours or the code's, so a default that later changed looked
 * identical to a deliberate choice. `field.set` is true only when the managed
 * file holds a line for the key, which is the only thing that means "chosen".
 */
function fieldMetaRow(field, input) {
  if (field.type === "oauth_login") return null;
  const row = document.createElement("div");
  row.className = "field-meta";
  const defaults = document.createElement("span");
  defaults.className = "field-default";
  defaults.textContent = `default: ${field.default || "none"}`;
  row.appendChild(defaults);
  if (fieldCanResetToDefault(field)) {
    const reset = document.createElement("button");
    reset.type = "button";
    reset.className = "field-reset";
    reset.textContent = "Use default";
    reset.addEventListener("click", () => resetFieldToDefault(input));
    row.appendChild(reset);
  }
  return row;
}

/** Secrets are managed as a key pool and chains by their own editor; clearing
 *  either from here would be a second, contradictory way to edit them. */
function fieldCanResetToDefault(field) {
  if (!field.set || field.locked || field.secret) return false;
  return field.type !== "model_chain" && field.type !== "oauth_login";
}

function resetFieldToDefault(input) {
  if (input.type === "checkbox") {
    input.checked = String(input.dataset.default).toLowerCase() === "true";
  } else {
    input.value = "";
  }
  input.dispatchEvent(new Event("change", { bubbles: true }));
  updateDirtyState();
}

function keyManagerForField(field, { idSuffix = "" } = {}) {
  const container = document.createElement("div");
  container.className = "key-manager";

  const header = document.createElement("div");
  header.className = "key-manager-header";

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "ghost-button key-manager-toggle";
  toggle.textContent = "Manage keys";
  header.appendChild(toggle);

  // Rotation policy select for this credential (participates in the normal
  // dirty/apply flow via the shared input machinery). Providers that share a
  // credential each get their own select, kept in step by syncSharedControls:
  // the policy is a property of the key pool, so changing it on either card
  // has to be the same change, not two competing ones.
  const rotationField = state.fields.get(`${field.key}_ROTATION`);
  if (rotationField) {
    const rotationWrap = document.createElement("label");
    rotationWrap.className = "key-manager-rotation";
    const rotationLabel = document.createElement("span");
    rotationLabel.textContent = "Rotation";
    const rotationInput = inputForField(rotationField);
    rotationInput.id = `field-${rotationField.key}${idSuffix}`;
    rotationInput.dataset.key = rotationField.key;
    rotationInput.dataset.original = rotationField.value || "";
    rotationInput.dataset.secret = "false";
    rotationInput.dataset.configured = rotationField.configured ? "true" : "false";
    rotationInput.dataset.fieldType = rotationField.type;
    rotationInput.disabled = rotationField.locked;
    const onRotationChange = () => {
      syncSharedControls(rotationInput);
      updateDirtyState();
    };
    rotationInput.addEventListener("input", onRotationChange);
    rotationInput.addEventListener("change", onRotationChange);
    rotationInput.title = rotationField.description || "Key rotation policy";
    rotationWrap.append(rotationLabel, rotationInput);
    header.appendChild(rotationWrap);
  }

  const panel = document.createElement("div");
  panel.className = "key-manager-panel";
  panel.hidden = true;

  const open = async () => {
    panel.hidden = false;
    toggle.textContent = "Hide keys";
    await renderKeyManager(panel, field);
  };
  const close = () => {
    panel.hidden = true;
    toggle.textContent = "Manage keys";
  };
  toggle.addEventListener("click", () => {
    if (panel.hidden) {
      open();
    } else {
      close();
    }
  });

  container.append(header, panel);
  // Let the provider card open the pool when it expands. Opening eagerly on
  // render would fire one request per provider on every page load.
  container.openKeyPool = () => {
    if (panel.hidden) open();
  };

  if (state.reopenKeyManager === field.key) {
    state.reopenKeyManager = null;
    open();
  }
  return container;
}

/* ------------------------------------------------------------------ key pools
   A credential pool is an ordered list, and the order *is* the failover order:
   `failover` serves the lowest healthy slot and `single` serves slot 0 and
   nothing else. So the pool is a rail, built the same way the route rail is --
   pointer events rather than HTML5 drag-and-drop (see the note above
   startRouteDrag), one mutate point, one `role="status"` line with one level
   of Undo.

   A key may also carry a name. The name is display only: it is stored in
   ~/.mcc/credential_names.json against a hash of the secret, never sent
   upstream, and never written to the request log. Everywhere a key is shown,
   a named key reads as its name and keeps its masked label in the tooltip. */

let keyPoolDrag = null;
let keyPoolUndo = null;

/** What to call this credential: its name if it has one, else its mask. */
function credentialDisplay(name, label) {
  const named = typeof name === "string" ? name.trim() : "";
  return named || String(label || "");
}

/** Adopt the server's masked-label to name index. */
function adoptKeyNames(names) {
  state.keyNames = names && typeof names === "object" ? names : {};
}

/** Render "<name>" for a named key and "<mask>" for an unnamed one.
 *
 * A key nobody renamed reads exactly as it did before this release. */
function keyReferenceText(label) {
  return credentialDisplay(keyNameForLabel(label), label);
}

/** The name the server resolved for one masked label, or "". */
function keyNameForLabel(label) {
  if (!label || !state.keyNames) return "";
  const name = state.keyNames[label];
  return typeof name === "string" ? name : "";
}

/** Render a key reference as text: the name when there is one, else the mask.
 *
 * The mask never disappears -- it moves to the tooltip -- because "track them
 * either by key or by name" was the whole ask. */
function paintKeyReference(node, label, name) {
  const resolved = name === undefined ? keyNameForLabel(label) : name;
  const display = credentialDisplay(resolved, label);
  node.textContent = display;
  if (resolved && label && display !== label) {
    node.title = label;
    node.dataset.keyLabel = label;
  }
  return display;
}

/** The status line for one key panel, created on demand. */
function keyPoolStatus(panel) {
  let status = panel.querySelector(".route-status.key-pool-status");
  if (!status) {
    status = document.createElement("div");
    status.className = "route-status key-pool-status";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    status.setAttribute("aria-atomic", "true");
    status.hidden = true;
    panel.insertBefore(status, panel.firstChild);
  }
  return status;
}

function announceKeyPool(panel, sentence, undoLabel, undoAction) {
  const target = keyPoolStatus(panel);
  target.textContent = "";
  if (!sentence) {
    target.hidden = true;
    return;
  }
  target.hidden = false;
  const lead = document.createElement("p");
  lead.textContent = sentence;
  target.appendChild(lead);
  if (undoLabel && undoAction) {
    const undo = document.createElement("button");
    undo.type = "button";
    undo.className = "secondary-button route-status-button key-pool-undo";
    undo.textContent = undoLabel;
    undo.addEventListener("click", () => {
      undo.disabled = true;
      undoAction();
    });
    target.appendChild(undo);
  }
  const dismiss = document.createElement("button");
  dismiss.type = "button";
  dismiss.className = "secondary-button route-status-button";
  dismiss.textContent = "Dismiss";
  dismiss.addEventListener("click", () => {
    target.textContent = "";
    target.hidden = true;
  });
  target.appendChild(dismiss);
}

/* Why a reorder says something about health: it rides the same apply path an
   add and a remove ride, which rebuilds the provider and therefore discards
   that pool's counters and benches. That was already true; it was never said,
   and a badge that silently went back to HEALTHY looked like a bug. */
const KEY_POOL_HEALTH_NOTE =
  "Reordering rebuilds this pool, so its health counters start again.";

/** The ids of a panel's rows, top to bottom. */
function keyPoolIds(list) {
  return Array.from(list.querySelectorAll("[data-key-id]")).map(
    (row) => row.dataset.keyId,
  );
}

function keyPoolBusy(panel, busy) {
  panel
    .querySelectorAll("button, input")
    .forEach((node) => {
      if (busy) node.setAttribute("data-key-pool-busy", "1");
      else node.removeAttribute("data-key-pool-busy");
      node.disabled = busy ? true : node.dataset.keyPoolStaysDisabled === "1";
    });
}

/** The one mutate point for an order change. Everything else calls this. */
async function applyKeyOrder(pool, ids, sentence, previous) {
  const panel = pool.panel;
  keyPoolBusy(panel, true);
  try {
    await api(pool.orderUrl, {
      method: "PUT",
      body: JSON.stringify({ order: ids }),
    });
  } catch (error) {
    keyPoolBusy(panel, false);
    announceKeyPool(panel, `Could not reorder: ${error.message}`);
    return false;
  }
  keyPoolUndo = previous ? { pool, ids: previous } : null;
  const message = {
    key: pool.stateKey,
    sentence: `${sentence} ${KEY_POOL_HEALTH_NOTE}`,
    undo: Boolean(previous),
  };
  // Said at once, and again after the reload. The reload replaces the panel,
  // and a status line that only appeared afterwards would leave the two or
  // three seconds an apply takes with nothing on screen saying what happened.
  announceKeyPool(
    panel,
    message.sentence,
    message.undo ? "Undo" : "",
    message.undo ? undoKeyPoolOrder : null,
  );
  state.keyPoolMessage = message;
  await pool.reload();
  return true;
}

function undoKeyPoolOrder() {
  if (!keyPoolUndo) return;
  const { pool, ids } = keyPoolUndo;
  keyPoolUndo = null;
  applyKeyOrder(pool, ids, "Order restored.", null);
}

/** Move one row by an offset and apply, sharing the drag's mutate point. */
function moveKeyRow(pool, list, id, offset) {
  const ids = keyPoolIds(list);
  const from = ids.indexOf(id);
  const to = from + offset;
  if (from < 0 || to < 0 || to >= ids.length) return;
  const next = ids.slice();
  next.splice(to, 0, next.splice(from, 1)[0]);
  const row = list.querySelector(`[data-key-id="${cssEscape(id)}"]`);
  const display = row ? row.dataset.keyDisplay || id : id;
  applyKeyOrder(
    pool,
    next,
    `Moved ${display} to position ${to + 1} of ${next.length}.`,
    ids,
  );
}

function cssEscape(value) {
  if (window.CSS && typeof window.CSS.escape === "function") {
    return window.CSS.escape(value);
  }
  return String(value).replace(/[^a-zA-Z0-9_-]/g, (ch) => `\\${ch}`);
}

function startKeyDrag(pool, list, id, event) {
  if (
    event.pointerType === "touch" &&
    !(event.target.classList && event.target.classList.contains("key-drag-grip"))
  ) {
    return;
  }
  if (typeof event.button === "number" && event.button !== 0) return;
  keyPoolDrag = { pool, list, id, target: null, before: keyPoolIds(list) };
  list.classList.add("is-dragging");
}

function continueKeyDrag(event) {
  if (!keyPoolDrag) return;
  const node = event.target.closest ? event.target.closest("[data-key-id]") : null;
  if (!node || !keyPoolDrag.list.contains(node)) return;
  keyPoolDrag.target = node.dataset.keyId;
  keyPoolDrag.list
    .querySelectorAll(".is-drop-target")
    .forEach((row) => row.classList.remove("is-drop-target"));
  node.classList.add("is-drop-target");
}

function endKeyDrag() {
  const drag = keyPoolDrag;
  keyPoolDrag = null;
  if (!drag) return;
  drag.list.classList.remove("is-dragging");
  drag.list
    .querySelectorAll(".is-drop-target")
    .forEach((row) => row.classList.remove("is-drop-target"));
  if (!drag.target || drag.target === drag.id) return;
  const ids = drag.before;
  const from = ids.indexOf(drag.id);
  const to = ids.indexOf(drag.target);
  if (from < 0 || to < 0) return;
  const next = ids.slice();
  next.splice(to, 0, next.splice(from, 1)[0]);
  const row = drag.list.querySelector(`[data-key-id="${cssEscape(drag.id)}"]`);
  const display = row ? row.dataset.keyDisplay || drag.id : drag.id;
  applyKeyOrder(
    drag.pool,
    next,
    `Moved ${display} to position ${to + 1} of ${next.length}.`,
    ids,
  );
}

/** Rename one key. Store only: no restart, no rebuild, no lost counters. */
async function renameKeyInPool(pool, id, value, previous, input) {
  const next = String(value || "").trim().slice(0, 60);
  if (next === (previous || "")) return;
  const panel = pool.panel;
  try {
    await api(pool.nameUrl(id), {
      method: "PUT",
      body: JSON.stringify({ name: next }),
    });
  } catch (error) {
    if (input) input.value = previous || "";
    announceKeyPool(panel, `Could not rename: ${error.message}`);
    return;
  }
  const row = panel.querySelector(`[data-key-id="${cssEscape(id)}"]`);
  const label = row ? row.dataset.keyLabel || "" : "";
  if (row) {
    row.dataset.keyDisplay = credentialDisplay(next, row.dataset.keyMasked || label);
    const code = row.querySelector(".key-manager-key");
    if (code) paintKeyReference(code, row.dataset.keyMasked || label, next);
  }
  announceKeyPool(
    panel,
    next
      ? `Renamed to ${next}. The name is stored on this machine only.`
      : "Name cleared.",
    "Undo",
    () => {
      if (input) input.value = previous || "";
      renameKeyInPool(pool, id, previous || "", next, input);
    },
  );
}

/** The controls that turn one key row into a rail row. */
function keyRowControls(pool, list, row, entry, index, total) {
  row.dataset.keyId = entry.id;
  row.dataset.keyLabel = entry.key_label || "";
  row.dataset.keyMasked = entry.masked || entry.key_label || "";
  row.dataset.keyDisplay = credentialDisplay(entry.name, row.dataset.keyMasked);

  const grip = document.createElement("button");
  grip.type = "button";
  grip.className = "key-drag-grip";
  grip.textContent = "⠿";
  grip.setAttribute("aria-label", `Reorder ${row.dataset.keyDisplay}`);
  grip.disabled = pool.locked;
  if (pool.locked) grip.dataset.keyPoolStaysDisabled = "1";
  grip.addEventListener("pointerdown", (event) =>
    startKeyDrag(pool, list, entry.id, event),
  );
  grip.addEventListener("keydown", (event) => {
    if (event.key === "ArrowUp") {
      event.preventDefault();
      moveKeyRow(pool, list, entry.id, -1);
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      moveKeyRow(pool, list, entry.id, 1);
    }
  });

  const name = document.createElement("input");
  name.type = "text";
  name.className = "key-name-input";
  name.maxLength = 60;
  name.placeholder = "Name this key";
  name.value = entry.name || "";
  name.setAttribute("aria-label", `Name for ${row.dataset.keyMasked}`);
  name.disabled = pool.locked;
  if (pool.locked) name.dataset.keyPoolStaysDisabled = "1";
  let previousName = entry.name || "";
  const commit = () => {
    const wanted = name.value;
    const before = previousName;
    previousName = String(wanted || "").trim().slice(0, 60);
    renameKeyInPool(pool, entry.id, wanted, before, name);
  };
  name.addEventListener("change", commit);
  name.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      commit();
    }
  });

  const up = document.createElement("button");
  up.type = "button";
  up.className = "ghost-button key-manager-move";
  up.textContent = "Move up";
  up.disabled = pool.locked || index === 0;
  if (up.disabled) up.dataset.keyPoolStaysDisabled = "1";
  up.setAttribute("aria-label", `Move ${row.dataset.keyDisplay} up`);
  up.addEventListener("click", () => moveKeyRow(pool, list, entry.id, -1));

  const down = document.createElement("button");
  down.type = "button";
  down.className = "ghost-button key-manager-move";
  down.textContent = "Move down";
  down.disabled = pool.locked || index === total - 1;
  if (down.disabled) down.dataset.keyPoolStaysDisabled = "1";
  down.setAttribute("aria-label", `Move ${row.dataset.keyDisplay} down`);
  down.addEventListener("click", () => moveKeyRow(pool, list, entry.id, 1));

  return { grip, name, up, down };
}

/* ----------------------------------------------------------- the one rail --
   One rail, three pools. Env-key providers, custom providers and web-search
   providers all build their rows, their list, their key label and their Add
   form through the four builders below, so the rail cannot look one way on a
   built-in card and another way on a custom one -- which is exactly what
   happened when 7.29.0 gave every row a grip, a name box and two Move
   buttons: `.key-manager-row` wrapped, `.cp-key-row` and `.ws-key-row` did
   not, and ~280px of controls hung off the right edge of a 240px grid column.

   Each builder keeps the caller's pre-7.34.1 class beside the shared one, so
   a selector written against `.cp-key-row`, `.ws-key-label` or `.cp-key-add`
   still finds the same element. The shared class is what admin.css styles;
   the legacy class is an alias and carries no rules of its own. */

/** A rail's list container. `legacy` is this pool's pre-7.34.1 class. */
function keyRailList(legacy) {
  const list = document.createElement("div");
  list.className = legacy ? `key-manager-list ${legacy}` : "key-manager-list";
  return list;
}

/** One rail row. */
function keyRailRow(legacy) {
  const row = document.createElement("div");
  row.className = legacy ? `key-manager-row ${legacy}` : "key-manager-row";
  return row;
}

/** The masked-or-named key label: one element, so one ellipsis rule covers
 *  every pool and a 40-character name cannot push a row wider than its card. */
function keyRailLabel(legacy) {
  const label = document.createElement("code");
  label.className = legacy ? `key-manager-key ${legacy}` : "key-manager-key";
  return label;
}

/** Say that this card is showing a rail, so the stylesheet can widen it.
 *
 * A rail is six controls across and a provider-grid column is 240px. A
 * built-in card has always taken the whole grid row while its pool is open
 * (`.pv-card.pv-open`, "so the key pool has room to breathe"); this is the
 * same rule, said once, for every card that opens a pool. */
function markKeyRailHost(node, showing) {
  const card = node && node.closest ? node.closest(".provider-card") : null;
  if (!card) return;
  card.classList.toggle("has-key-rail", showing !== false);
}

/** The Add-a-key form: the secret box, the optional name, and the button.
 *
 * `onSubmit(secretInput, addButton, nameInput)` is the pool's own add call --
 * the routes and payloads are unchanged; only the markup is now shared. */
function keyRailAddForm(options) {
  const form = document.createElement("div");
  form.className = options.legacy
    ? `key-manager-add ${options.legacy}`
    : "key-manager-add";

  const secret = document.createElement("input");
  secret.type = "password";
  secret.className = "key-add-secret";
  secret.autocomplete = "off";
  secret.placeholder = options.placeholder;
  secret.disabled = Boolean(options.locked);

  // Optional, and honoured only for a single key: a paste of five keys has
  // one name box and no way to say which key it meant.
  const name = document.createElement("input");
  name.type = "text";
  name.className = "key-name-input key-add-name";
  name.maxLength = 60;
  name.placeholder = "Name (optional)";
  name.setAttribute("aria-label", "Name for the key being added");
  name.disabled = Boolean(options.locked);

  const add = document.createElement("button");
  add.type = "button";
  add.className = "secondary-button key-add-submit";
  add.textContent = "Add key";
  add.disabled = Boolean(options.locked);

  const submit = () => options.onSubmit(secret, add, name);
  add.addEventListener("click", submit);
  for (const control of [secret, name]) {
    control.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        submit();
      }
    });
  }

  form.append(secret, name, add);
  return { element: form, secret, name, add };
}

async function renderKeyManager(panel, field) {
  panel.textContent = "Loading keys...";
  let info;
  try {
    info = await api(`/admin/api/credentials/${field.key}/keys`);
  } catch (error) {
    panel.textContent = `Could not load keys: ${error.message}`;
    return;
  }

  panel.innerHTML = "";

  const pool = {
    stateKey: `env:${field.key}`,
    panel,
    locked: Boolean(info.locked),
    orderUrl: `/admin/api/credentials/${field.key}/keys/order`,
    nameUrl: (id) =>
      `/admin/api/credentials/${field.key}/keys/${encodeURIComponent(id)}/name`,
    // Just this panel, not the whole dashboard. A reorder changes the pool's
    // order and nothing else on the page, and re-reading every card to redraw
    // six rows would cost the two seconds a full Apply costs. Add and Remove
    // still reload everything: they change the key count the card face shows.
    reload: () => renderKeyManager(panel, field),
  };

  const list = keyRailList();
  markKeyRailHost(panel);
  if (info.count === 0) {
    const empty = document.createElement("div");
    empty.className = "key-manager-empty";
    empty.textContent = "No keys configured.";
    list.appendChild(empty);
  }
  // `rows` is the structured listing; `keys` is the pre-7.29.0 shape, still
  // sent, and still all a dashboard from before this release needs.
  const entries = Array.isArray(info.rows)
    ? info.rows
    : info.keys.map((masked, index) => ({
        index,
        id: "",
        masked,
        key_label: "",
        name: "",
        health: Array.isArray(info.health) ? info.health[index] : null,
      }));
  entries.forEach((entry, index) => {
    const row = keyRailRow();

    const controls = entry.id
      ? keyRowControls(pool, list, row, entry, index, entries.length)
      : null;
    if (controls) row.append(controls.grip, controls.name);

    const label = keyRailLabel();
    paintKeyReference(label, entry.masked, entry.name || "");

    row.appendChild(label);

    const health = entry.health || null;
    if (health && health.state) {
      row.appendChild(keyHealthBadge(health));
    }

    if (controls) row.append(controls.up, controls.down);

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "ghost-button key-manager-remove";
    remove.textContent = "Remove";
    remove.disabled = info.locked;
    if (info.locked) remove.dataset.keyPoolStaysDisabled = "1";
    remove.addEventListener("click", () =>
      removeCredentialKey(field, index, remove, entry.id),
    );

    row.appendChild(remove);
    // Last, so it wraps onto a line of its own under the key rather than
    // pushing Remove off the first line.
    if (health && Array.isArray(health.model_benches) && health.model_benches.length) {
      row.appendChild(modelBenchList(health.model_benches));
    }
    list.appendChild(row);
  });
  list.addEventListener("pointerover", continueKeyDrag);
  panel.appendChild(list);

  const pending =
    state.keyPoolMessage && state.keyPoolMessage.key === pool.stateKey
      ? state.keyPoolMessage
      : null;
  state.keyPoolMessage = null;
  if (pending) {
    announceKeyPool(
      panel,
      pending.sentence,
      pending.undo ? "Undo" : "",
      pending.undo ? undoKeyPoolOrder : null,
    );
  }

  const addForm = keyRailAddForm({
    placeholder: info.locked
      ? "Locked by process environment"
      : "Paste a key, or several separated by commas",
    locked: info.locked,
    onSubmit: (secret, button, name) =>
      addCredentialKey(field, secret, button, name),
  });
  panel.appendChild(addForm.element);

  if (info.locked) {
    const note = document.createElement("div");
    note.className = "key-manager-note";
    note.textContent =
      "This credential comes from the process environment and is read-only here.";
    panel.appendChild(note);
  }
}

function formatSeconds(seconds) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 60) return rem ? `${m}m ${rem}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const mrem = m % 60;
  return mrem ? `${h}h ${mrem}m` : `${h}h`;
}

function keyHealthBadge(health) {
  const state = String(health.state || "HEALTHY");
  const badge = document.createElement("span");
  badge.className = `key-health-badge key-health-${state.toLowerCase().replace(/_/g, "-")}`;

  let backIn = "";
  const remaining =
    state === "LOCKED_OUT"
      ? health.lockout_remaining || 0
      : health.cooldown_remaining || 0;
  if (remaining > 0 && state !== "HEALTHY") {
    backIn = ` — back in ${formatSeconds(remaining)}`;
  }
  badge.textContent = state;

  // A slot the engine keeps HEALTHY can still be benched for individual
  // models; reading plain "HEALTHY" there is the exact confusion this PR
  // exists to remove. A key with no model benches renders byte-identically.
  const benched = Array.isArray(health.model_benches) ? health.model_benches : [];
  let modelNote = "";
  if (benched.length) {
    badge.textContent = `${state} (${benched.length} model${benched.length === 1 ? "" : "s"})`;
    modelNote = ` — rate-limited for ${benched.map((b) => b.model).join(", ")}`;
  }

  // A bench the pool can name. "COOLDOWN — back in 55s" reads as a throttle
  // that lifts on its own; an exhausted balance never does, and the operator
  // has to top up or add a key. The pool publishes the reason only for that
  // case, so every other bench renders byte-identically.
  const reason =
    typeof health.cooldown_reason === "string" ? health.cooldown_reason : "";
  let benchNote = "";
  if (reason && state !== "HEALTHY") {
    badge.textContent = `${state} — ${reason}`;
    benchNote = remaining > 0
      ? ` — benched: ${reason}, ${formatSeconds(remaining)} left`
      : ` — benched: ${reason}`;
  }

  const requests = health.request_count || 0;
  const failures = health.failure_count || 0;
  badge.title = `${state}${backIn}${benchNote} — ${requests} requests, ${failures} failures${modelNote}`;
  return badge;
}

/** The models this key is rate-limited for right now, capped at three. */
function modelBenchList(benches) {
  const wrap = document.createElement("span");
  wrap.className = "key-model-benches";
  const shown = benches.slice(0, 3);
  let text = shown.map((b) => `${b.model} ${formatSeconds(b.remaining)}`).join(", ");
  if (benches.length > shown.length) {
    text += `, +${benches.length - shown.length} more`;
  }
  wrap.textContent = text;
  wrap.title =
    "Rate-limited on this key for these models only. Every other model on " +
    "this key still serves.";
  return wrap;
}

async function reloadAndReopenKeyManager(field, message) {
  state.reopenKeyManager = field.key;
  await load();
  if (message) showMessage(message, "ok");
}

async function addCredentialKey(field, input, button, nameInput) {
  const value = input.value.trim();
  if (!value) return;
  const name = nameInput ? nameInput.value.trim().slice(0, 60) : "";
  button.disabled = true;
  try {
    const result = await api(`/admin/api/credentials/${field.key}/keys`, {
      method: "POST",
      body: JSON.stringify({ key: value, name }),
    });
    const called = result.name ? ` Called ${result.name}.` : "";
    await reloadAndReopenKeyManager(
      field,
      `Added key ${result.added} (${result.count} configured). Applied.${called}`,
    );
  } catch (error) {
    button.disabled = false;
    showMessage(`Could not add key: ${error.message}`, "error");
  }
}

async function removeCredentialKey(field, index, button, id) {
  button.disabled = true;
  // The id the row was rendered with rides along: if another tab reordered
  // this pool underneath us, the server refuses rather than removing whatever
  // now sits at this position.
  const guard = id ? `?id=${encodeURIComponent(id)}` : "";
  try {
    const result = await api(
      `/admin/api/credentials/${field.key}/keys/${index}${guard}`,
      { method: "DELETE" },
    );
    await reloadAndReopenKeyManager(
      field,
      `Removed key ${result.removed} (${result.count} remaining). Applied.`,
    );
  } catch (error) {
    button.disabled = false;
    showMessage(`Could not remove key: ${error.message}`, "error");
  }
}

/** Build an option control that can say "nobody chose this".
 *
 * The unset option comes first and carries the empty value, so a field the
 * user never touched loads showing the default it will actually use. Falling
 * back to `field.options[0]` instead -- which is what this did -- displayed
 * the first option, disagreed with `dataset.original`, and made every Save
 * submit a value nobody had picked: that is how `FALLBACK_BENCH_ENABLED=false`
 * ended up written into managed .env files that had never been edited.
 */
function selectWithDefaultOption(field, options) {
  const select = document.createElement("select");
  const fallback = field.default ?? "";
  const match = options.find((item) => item.value === fallback);
  select.appendChild(
    option("", `Default (${match ? match.label : fallback || "none"})`),
  );
  options.forEach((item) => select.appendChild(option(item.value, item.label)));
  select.value = field.value ?? "";
  return select;
}

function inputForField(field) {
  if (field.type === "boolean") {
    // A checkbox has two positions and a setting has three states: on, off,
    // and never chosen. Rendered as a checkbox, an untouched setting showed
    // its default as if someone had picked it, and the first Save wrote it.
    return selectWithDefaultOption(field, [
      { value: "true", label: "On" },
      { value: "false", label: "Off" },
    ]);
  }

  if (field.type === "oauth_login") {
    const wrapper = document.createElement("div");
    wrapper.className = "oauth-login-control";
    if (field.key === "ANTHROPIC_OAUTH_MANAGE") {
      return buildAnthropicOAuthControl(wrapper);
    }
    if (field.key === "CHATGPT_OAUTH_IMPORT_CODEX") {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "secondary-button";
      button.textContent = "Import existing Codex login";
      button.addEventListener("click", () => {
        importChatGPTOAuthCodexTokens(button);
      });
      wrapper.appendChild(button);
      const details = document.createElement("dl");
      // Shares the Anthropic card's definition-list layout on purpose -- the
      // two subscription cards answer the same shape of question -- and
      // carries its own class so tests and stylesheets can address it.
      details.className = "anthropic-oauth-details chatgpt-oauth-details";
      details.hidden = true;
      wrapper.appendChild(details);
      refreshChatGPTOAuthStatus(details);
      return wrapper;
    }

    const deviceButton = document.createElement("button");
    deviceButton.type = "button";
    deviceButton.className = "primary-button";
    deviceButton.textContent = "Log in with device code";

    const browserButton = document.createElement("button");
    browserButton.type = "button";
    browserButton.className = "secondary-button";
    browserButton.textContent = "Browser login (same device)";

    const loginButtons = [deviceButton, browserButton];
    deviceButton.addEventListener("click", () => {
      startChatGPTOAuthDeviceLogin(deviceButton, loginButtons);
    });
    browserButton.addEventListener("click", () => {
      startChatGPTOAuthBrowserLogin(browserButton, loginButtons);
    });
    wrapper.append(deviceButton, browserButton);
    return wrapper;
  }

  if (field.type === "select") {
    return selectWithDefaultOption(field, field.options);
  }

  if (field.type === "textarea") {
    const textarea = document.createElement("textarea");
    textarea.value = field.value || "";
    return textarea;
  }

  if (field.type === "model" || field.type === "optional_model") {
    const input = document.createElement("input");
    input.type = "text";
    input.value = field.value || (field.type === "optional_model" ? "None" : "");
    input.autocomplete = "off";
    return input;
  }

  if (field.type === "model_chain") {
    // Wire value is the comma-joined chain string; the chain editor UI
    // (built in renderField) reads/writes this hidden input so the normal
    // dirty-state/apply machinery needs no special-casing for this type.
    const input = document.createElement("input");
    input.type = "hidden";
    input.value = field.value || "";
    return input;
  }

  const input = document.createElement("input");
  input.type = field.type === "number" ? "number" : "text";
  if (field.default) input.placeholder = field.default;
  // Bounds come from the server so the browser refuses a value the server
  // would only clamp afterwards; a form that silently changes what was typed
  // teaches nobody anything.
  if (field.type === "number") {
    if (field.minimum !== null && field.minimum !== undefined) {
      input.min = String(field.minimum);
    }
    if (field.maximum !== null && field.maximum !== undefined) {
      input.max = String(field.maximum);
    }
  }
  if (field.type === "secret") {
    input.type = "password";
    input.placeholder = field.configured
      ? "Configured - enter a new value to replace"
      : "Not configured";
    input.value = "";
    input.autocomplete = "off";
  } else {
    input.value = field.value || "";
  }
  return input;
}

class ModelCombobox {
  constructor(input, field) {
    this.input = input;
    this.fieldType = field.type;
    this.activeIndex = -1;
    this.query = "";

    this.element = document.createElement("div");
    this.element.className = "model-combobox";
    this.listbox = document.createElement("div");
    this.listbox.className = "model-combobox-list";
    this.listbox.id = `model-options-${field.key}`;
    this.listbox.setAttribute("role", "listbox");
    this.listbox.hidden = true;
    this.toggle = document.createElement("button");
    this.toggle.type = "button";
    this.toggle.className = "model-combobox-toggle";
    this.toggle.disabled = input.disabled;
    this.toggle.setAttribute("aria-label", `Show ${field.label} options`);

    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-haspopup", "listbox");
    for (const control of [input, this.toggle]) {
      control.setAttribute("aria-controls", this.listbox.id);
      control.setAttribute("aria-expanded", "false");
    }

    // A `provider/model` ref is routinely longer than the field it sits in --
    // the longest on a default install needs 360px and the routing rail can
    // spare 335 -- and an input clips silently, with no ellipsis to say so.
    // The title makes the whole value recoverable by hovering, whatever the
    // window width, instead of only by clicking in and scrolling.
    this.syncTitle();
    input.addEventListener("click", () => this.open());
    input.addEventListener("input", () => {
      this.syncTitle();
      this.open(input.value);
    });
    input.addEventListener("change", () => this.syncTitle());
    input.addEventListener("keydown", (event) => this.handleKeydown(event));
    this.toggle.addEventListener("mousedown", (event) => event.preventDefault());
    this.toggle.addEventListener("click", () => {
      if (this.isOpen) this.close();
      else this.open();
      input.focus();
    });
    this.listbox.addEventListener("mousedown", (event) => event.preventDefault());
    this.listbox.addEventListener("mousemove", (event) => {
      const optionEl = event.target.closest('[role="option"]');
      if (optionEl) this.setActive(this.visibleOptions.indexOf(optionEl));
    });
    this.listbox.addEventListener("click", (event) => {
      const optionEl = event.target.closest('[role="option"]');
      if (optionEl) this.select(optionEl.dataset.value);
    });

    this.element.append(input, this.toggle, this.listbox);
    state.modelComboboxes.add(this);
  }

  get isOpen() {
    return this.element.classList.contains("open");
  }

  get values() {
    return this.fieldType === "optional_model"
      ? ["None", ...state.modelOptions]
      : state.modelOptions;
  }

  get visibleOptions() {
    return Array.from(this.listbox.querySelectorAll('[role="option"]'));
  }

  open(query = "") {
    if (this.input.disabled) return;
    state.modelComboboxes.forEach((combobox) => {
      if (combobox !== this) combobox.close();
    });
    this.render(query);
    this.element.classList.add("open");
    this.listbox.hidden = false;
    this.setExpanded(true);
  }

  close() {
    this.element.classList.remove("open");
    this.listbox.hidden = true;
    this.activeIndex = -1;
    this.input.removeAttribute("aria-activedescendant");
    this.setExpanded(false);
  }

  setExpanded(expanded) {
    for (const control of [this.input, this.toggle]) {
      control.setAttribute("aria-expanded", String(expanded));
    }
  }

  render(query) {
    this.query = query;
    const normalizedQuery = query.trim().toLocaleLowerCase();
    const values = normalizedQuery
      ? this.values.filter((value) =>
          value.toLocaleLowerCase().includes(normalizedQuery),
        )
      : this.values;
    this.listbox.innerHTML = "";

    if (values.length === 0) {
      const empty = document.createElement("div");
      empty.className = "model-combobox-empty";
      empty.textContent = state.modelOptions.length
        ? "No matching models. You can still enter a custom slug."
        : "No discovered models. Refresh models or enter a custom slug.";
      this.listbox.appendChild(empty);
      this.activeIndex = -1;
      this.input.removeAttribute("aria-activedescendant");
      return;
    }

    values.forEach((value, index) => {
      const optionEl = document.createElement("div");
      optionEl.className = "model-combobox-option";
      optionEl.id = `${this.listbox.id}-option-${index}`;
      optionEl.dataset.value = value;
      optionEl.setAttribute("role", "option");
      optionEl.textContent = value;
      this.listbox.appendChild(optionEl);
    });
    const selectedIndex = values.indexOf(this.input.value);
    this.setActive(selectedIndex >= 0 ? selectedIndex : 0, false);
  }

  setActive(index, scroll = true) {
    const options = this.visibleOptions;
    if (options.length === 0) return;
    this.activeIndex = Math.max(0, Math.min(index, options.length - 1));
    options.forEach((optionEl, optionIndex) => {
      const active = optionIndex === this.activeIndex;
      optionEl.classList.toggle("active", active);
      optionEl.setAttribute("aria-selected", String(active));
    });
    const activeOption = options[this.activeIndex];
    this.input.setAttribute("aria-activedescendant", activeOption.id);
    if (scroll) activeOption.scrollIntoView({ block: "nearest" });
  }

  move(offset) {
    const count = this.visibleOptions.length;
    if (count) this.setActive((this.activeIndex + offset + count) % count);
  }

  /** Keep the hover text equal to the value, and absent when there is none. */
  syncTitle() {
    const value = this.input.value.trim();
    if (value) this.input.title = value;
    else this.input.removeAttribute("title");
  }

  select(value) {
    this.input.value = value;
    this.syncTitle();
    this.input.dispatchEvent(new Event("change", { bubbles: true }));
    this.close();
    this.input.focus();
  }

  handleKeydown(event) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (this.isOpen) {
        this.move(event.key === "ArrowDown" ? 1 : -1);
      } else {
        this.open();
        if (event.key === "ArrowUp") {
          this.setActive(this.visibleOptions.length - 1);
        }
      }
    } else if (this.isOpen && (event.key === "Home" || event.key === "End")) {
      event.preventDefault();
      this.setActive(event.key === "Home" ? 0 : this.visibleOptions.length - 1);
    } else if (this.isOpen && event.key === "Enter") {
      const active = this.visibleOptions[this.activeIndex];
      if (active) {
        event.preventDefault();
        this.select(active.dataset.value);
      }
    } else if (this.isOpen && event.key === "Escape") {
      event.preventDefault();
      this.close();
    } else if (this.isOpen && event.key === "Tab") {
      this.close();
    }
  }
}

// Renders a "model_chain" field (e.g. MODEL_FALLBACKS) as an ordered list of
// rows, each reusing ModelCombobox for search/autocomplete. Keeps the field's
// hidden <input> (data-key/data-original/data-field-type) as the single
// source of truth for changedValues()/apply; rows themselves carry no
// data-key so they never get picked up as standalone settings.
class ModelChainEditor {
  constructor(input, field) {
    this.input = input;
    this.field = field;
    this.rows = [];
    this.rowSeq = 0;
    // The two settings identifying this rail, so a route id resolves back to
    // an editor without walking the DOM.
    this.chainKey = field.key;
    this.modelKey = "";
    // Set by setPrimary() when this chain sits on a route rail. Null for a
    // chain rendered on its own, which then has nothing to trade places with.
    this.primary = null;

    this.element = document.createElement("div");
    this.element.className = "model-chain-editor";

    this.rowsEl = document.createElement("div");
    this.rowsEl.className = "model-chain-rows";

    this.addButton = document.createElement("button");
    this.addButton.type = "button";
    this.addButton.className = "secondary-button model-chain-add";
    this.addButton.textContent = "Add fallback";
    this.addButton.setAttribute("aria-label", `Add fallback to ${field.label}`);
    this.addButton.addEventListener("click", () => this.addRow("", true));

    // The hidden input carries this field's value and its data-key, so it
    // has to be in the document: `changedValues()` finds fields by walking
    // [data-key] across the page, and an input left detached is invisible
    // to Apply no matter what gets written to it.
    this.element.append(this.input, this.rowsEl, this.addButton);

    // Seed rows from the current value without touching the hidden input or
    // dirty state - this is initial render, not a user edit.
    this.parseValue(input.value).forEach((value) => this.addRow(value, false));
  }

  parseValue(value) {
    return String(value || "")
      .split(",")
      .map((entry) => entry.trim())
      .filter(Boolean);
  }

  syncValue() {
    this.input.value = this.rows
      .map((row) => row.combobox.input.value.trim())
      .filter(Boolean)
      .join(",");
    updateDirtyState();
    // A row's ref is what a pause names, and editing the combobox changes it.
    syncRoutePauseUi();
  }

  /** Replace every row from a comma-joined value. Used by undo, which restores
   *  the string the drop started from rather than replaying the moves. */
  setValue(value) {
    this.rows.slice().forEach((row) => this.removeRow(row));
    this.input.value = value;
    this.parseValue(value).forEach((entry) => this.addRow(entry, false));
    this.renumber();
    this.syncValue();
  }

  addRow(value, notify) {
    const row = {};
    const seq = this.rowSeq++;
    const rowField = {
      type: "model",
      key: `${this.field.key}__chain_${seq}`,
      label: `${this.field.label} fallback`,
    };

    const rowInput = document.createElement("input");
    rowInput.type = "text";
    rowInput.autocomplete = "off";
    rowInput.value = value;
    // No data-key: this row is not an independent setting, only a fragment
    // of the parent hidden input's comma-joined value.

    const combobox = new ModelCombobox(rowInput, rowField);
    rowInput.addEventListener("input", () => this.syncValue());
    rowInput.addEventListener("change", () => this.syncValue());

    const numberEl = document.createElement("span");
    numberEl.className = "model-chain-index";
    numberEl.setAttribute("aria-hidden", "true");

    const upButton = document.createElement("button");
    upButton.type = "button";
    upButton.className = "ghost-button model-chain-move";
    upButton.textContent = "↑";
    upButton.addEventListener("click", () => this.move(row, -1));

    const downButton = document.createElement("button");
    downButton.type = "button";
    downButton.className = "ghost-button model-chain-move";
    downButton.textContent = "↓";
    downButton.addEventListener("click", () => this.move(row, 1));

    const removeButton = document.createElement("button");
    removeButton.type = "button";
    removeButton.className = "ghost-button model-chain-remove";
    removeButton.textContent = "×";
    removeButton.addEventListener("click", () => this.removeRow(row));

    const wrapper = document.createElement("div");
    wrapper.className = "model-chain-row";
    // A ref may legitimately sit on two rails and a fresh row holds none, so
    // identity is the sequence number rather than the value.
    const routeId = `chain:${this.chainKey}#${seq}`;
    wrapper.dataset.chainKey = this.chainKey;
    const controls = routeNodeControls(wrapper, routeId, rowField.label);
    wrapper.append(
      controls.grip,
      numberEl,
      combobox.element,
      controls.cell,
      upButton,
      downButton,
      removeButton,
    );

    Object.assign(row, {
      wrapper,
      combobox,
      numberEl,
      upButton,
      downButton,
      removeButton,
      routeId,
    });
    this.rows.push(row);
    this.rowsEl.appendChild(wrapper);
    this.renumber();
    if (notify) {
      wrapper.classList.add("route-fallback-enter");
      this.syncValue();
      rowInput.focus();
    }
  }

  removeRow(row) {
    const index = this.rows.indexOf(row);
    if (index === -1) return;
    this.rows.splice(index, 1);
    row.wrapper.remove();
    state.modelComboboxes.delete(row.combobox);
    state.routeSelection.delete(row.routeId);
    this.renumber();
    this.syncValue();
  }

  move(row, offset) {
    const index = this.rows.indexOf(row);
    const target = index + offset;
    if (index === -1) return;
    // Above fallback 1 is the route's primary model, not the top of the list.
    if (target < 0) {
      if (this.canPromoteFirst()) this.swapPrimaryAndFirst();
      return;
    }
    if (target >= this.rows.length) return;
    this.rows.splice(index, 1);
    this.rows.splice(target, 0, row);
    // Re-append in the new order; appendChild moves existing nodes rather
    // than duplicating them, so this is enough to reorder the DOM.
    this.rows.forEach((item) => this.rowsEl.appendChild(item.wrapper));
    this.renumber();
    this.syncValue();
  }

  renumber() {
    this.rows.forEach((row, index) => {
      row.numberEl.textContent = String(index + 1);
      // Fallback 1's "up" is a promotion into the primary slot, so it stays
      // live whenever a primary is attached -- it is only the top of the list
      // for a chain rendered without one.
      row.upButton.disabled = index === 0 && !this.canPromoteFirst();
      row.upButton.setAttribute(
        "aria-label",
        index === 0 && this.canPromoteFirst()
          ? `Promote fallback 1 to ${this.primaryLabel()}`
          : `Move fallback ${index + 1} up`,
      );
      row.downButton.disabled = index === this.rows.length - 1;
      row.downButton.setAttribute("aria-label", `Move fallback ${index + 1} down`);
      row.removeButton.setAttribute("aria-label", `Remove fallback ${index + 1}`);
      row.combobox.input.setAttribute(
        "aria-label",
        `${this.field.label} fallback ${index + 1}`,
      );
      // Which route a pause on this row belongs to. Known only once the rail
      // has adopted a primary, which happens after the rows are built.
      row.wrapper.dataset.modelKey = this.modelKey;
    });
    this.renumberPrimary();
  }

  // ------------------------------------------------------------------ rail --
  // A route is one ordered path, but it is stored as two settings: the primary
  // model (MODEL, MODEL_OPUS, ...) and the comma-joined chain beside it. The
  // buttons below let the two trade places so the rail reorders as the single
  // list it looks like, and both hidden inputs go dirty so Apply writes them
  // together.

  /** Adopt a route's primary model field as position 0 of this rail. */
  setPrimary({ input, label, upButton, downButton }) {
    this.primary = { input, label, upButton, downButton };
    this.modelKey = input.dataset.key || "";
    // upButton carries no handler: nothing sits above the primary. It is
    // rendered, permanently disabled, so the primary reads as position 1 of
    // the list rather than as a field that happens to sit above one.
    downButton.addEventListener("click", () => {
      if (this.canDemotePrimary()) this.swapPrimaryAndFirst();
    });
    // Whether the primary can be demoted depends on its own value, so the
    // enable pass has to run again when the user edits it -- not only when
    // the chain changes.
    input.addEventListener("input", () => this.renumberPrimary());
    input.addEventListener("change", () => {
      this.renumberPrimary();
      // The primary's ref is what a pause on the primary names.
      syncRoutePauseUi();
    });
    this.renumber();
  }

  primaryLabel() {
    return this.primary ? this.primary.label : "the primary model";
  }

  /** The primary's value, with an optional route's "None" read as unset. */
  primaryValue() {
    return this.primary ? readFieldValue(this.primary.input).trim() : "";
  }

  /** Whether the primary may move down into the chain.
   *
   * Only a swap is offered, never an insert, so the primary can never be left
   * empty by a button press. That is not cosmetic: an empty MODEL fails
   * validation and the server refuses to start, and an empty tier override
   * silently orphans the chain sitting next to it, because routing only reads
   * a route's own fallbacks when that route has its own primary.
   */
  canDemotePrimary() {
    return Boolean(this.primary) && this.rows.length > 0 && this.primaryValue() !== "";
  }

  /** Whether fallback 1 may move up into the primary slot.
   *
   * Unlike demotion this needs no value on the primary: promoting into an
   * unset override is exactly how a route stops inheriting the default.
   */
  canPromoteFirst() {
    return Boolean(this.primary) && this.rows.length > 0;
  }

  setPrimaryValue(value) {
    const input = this.primary.input;
    input.value =
      value || (input.dataset.fieldType === "optional_model" ? "None" : "");
    // Assigning .value fires nothing, so anything listening for a change --
    // the hover title, the dirty state -- would go stale after a reorder.
    // Dispatching is cheaper and safer than re-implementing each listener.
    input.dispatchEvent(new Event("change", { bubbles: true }));
    updateDirtyState();
  }

  /** Trade the primary model with fallback 1. */
  swapPrimaryAndFirst() {
    if (!this.primary || !this.rows.length) return;
    const row = this.rows[0];
    const promoted = readFieldValue(row.combobox.input).trim();
    const demoted = this.primaryValue();
    this.setPrimaryValue(promoted);
    if (demoted) {
      row.combobox.input.value = demoted;
      row.combobox.input.dispatchEvent(new Event("change", { bubbles: true }));
      this.syncValue();
      this.renumber();
    } else {
      // The primary was unset, so this was a promotion rather than a swap and
      // there is nothing to leave in the row it came from.
      this.removeRow(row);
      this.primary.input.focus();
    }
  }

  renumberPrimary() {
    if (!this.primary) return;
    const { upButton, downButton } = this.primary;
    upButton.disabled = true;
    upButton.setAttribute("aria-label", "Already first in this route");
    downButton.disabled = !this.canDemotePrimary();
    downButton.setAttribute(
      "aria-label",
      `Move ${this.primaryLabel()} down to fallback 1`,
    );
  }
}

function option(value, label) {
  const optionEl = document.createElement("option");
  optionEl.value = value;
  optionEl.textContent = label;
  return optionEl;
}

/** What a control is actually configuring, unset controls included.
 *
 * `readFieldValue` answers what would be *saved*; this answers what is in
 * effect. A widget that renders state -- a switch, a segmented control -- has
 * to draw the second one or an unset setting reads as off.
 */
function effectiveControlValue(input) {
  if (input.type === "checkbox") return input.checked ? "true" : "false";
  return input.value || input.dataset.default || "";
}

function readFieldValue(input) {
  if (input.type === "checkbox") return input.checked ? "true" : "false";
  if (
    input.dataset.fieldType === "optional_model" &&
    input.value.trim().toLowerCase() === "none"
  ) {
    return "";
  }
  if (input.dataset.secret === "true" && input.dataset.configured === "true") {
    return input.value ? input.value : MASKED_SECRET;
  }
  return input.value;
}

function changedValues() {
  const values = {};
  document.querySelectorAll("[data-key]").forEach((input) => {
    if (input.disabled || !input.matches("input, select, textarea")) return;
    // A coding agent's tier rail is not a setting: it is one entry in
    // harness_tiers.json, written by that section's own Save. Collected here
    // it would be posted to /admin/api/config/apply as an unknown env key on
    // every Apply, and it would make the page read dirty from a section the
    // dirty count cannot explain.
    if (input.closest(".agent-tiers")) return;
    const value = readFieldValue(input);
    if (value !== input.dataset.original) {
      values[input.dataset.key] = value;
    }
  });
  return values;
}

/** Keep every control bound to one variable showing the same value.
 *
 * Providers that share a credential each render their own rotation select, so
 * the setting is editable wherever you happen to be looking. They are the same
 * variable, so leaving them to disagree would mean the page shows two answers
 * and `changedValues()` submits whichever it walked last. Mirroring on edit
 * makes the duplicate a view of one value rather than a second copy of it, and
 * the dirty count stays at one because it counts keys, not controls.
 *
 * The twin is notified, not just assigned. A control can be the thing another
 * card is gated on -- the Chain benching master switch is -- and that card
 * listens for `change` on its own control. Setting `.value` silently left the
 * Limits card live while the Model Config copy said the feature was off,
 * which is precisely the "two answers" this function exists to prevent. The
 * re-entry guard is what stops the twin's own handler bouncing it back.
 */
let syncingSharedControls = false;
function syncSharedControls(source) {
  if (syncingSharedControls) return;
  const key = source.dataset.key;
  syncingSharedControls = true;
  try {
    document
      .querySelectorAll(`input[data-key="${key}"], select[data-key="${key}"]`)
      .forEach((twin) => {
        if (twin === source || twin.value === source.value) return;
        twin.value = source.value;
        twin.dispatchEvent(new Event("change", { bubbles: true }));
      });
  } finally {
    syncingSharedControls = false;
  }
}

function updateDirtyState() {
  const count = Object.keys(changedValues()).length;
  byId("dirtyState").textContent =
    count === 0 ? "No changes" : `${count} unsaved change${count === 1 ? "" : "s"}`;
  byId("applyButton").disabled = count === 0;
}

async function validate(showResult = true) {
  const result = await api("/admin/api/config/validate", {
    method: "POST",
    body: JSON.stringify({ values: changedValues() }),
  });
  if (showResult) {
    showValidationResult(result);
  }
  return result;
}

function showValidationResult(result) {
  if (result.valid) {
    showMessage("Config shape is valid", "ok");
  } else {
    showMessage(result.errors.join("; "), "error");
  }
}

async function apply() {
  const result = await api("/admin/api/config/apply", {
    method: "POST",
    body: JSON.stringify({ values: changedValues() }),
  });
  if (!result.applied) {
    showValidationResult(result);
    return;
  }
  const restart = result.restart || {};
  if (restart.required && restart.automatic) {
    showMessage("Applied. Restarting server...", "ok");
    byId("applyButton").disabled = true;
    setTimeout(() => {
      window.location.href = restart.admin_url || "/admin";
    }, 1600);
    return;
  }
  const pending = restart.required ? restart.fields || [] : result.pending_fields || [];
  const warnings = result.warnings || [];
  await load();
  const applied = pending.length
    ? `Applied. Restart my-claude-code to use: ${pending.join(", ")}`
    : "Applied";
  showMessage(
    warnings.length ? `${applied} ${warnings.join("; ")}` : applied,
    warnings.length ? "warn" : "ok",
  );
}

async function refreshLocalStatus() {
  const result = await api("/admin/api/providers/local-status");
  result.providers.forEach((provider) => {
    state.localStatus.set(provider.provider_id, provider);
    const meta = provider.status_code
      ? `${provider.base_url} returned HTTP ${provider.status_code}`
      : provider.base_url;
    updateProviderCard(provider.provider_id, provider.status, provider.label, meta);
  });
}

async function testProvider(providerId, button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Testing";
  try {
    const result = await api(`/admin/api/providers/${providerId}/test`, {
      method: "POST",
      body: "{}",
    });
    if (result.ok) {
      updateProviderCard(
        providerId,
        "reachable",
        `${result.models.length} models`,
        result.models.slice(0, 3).join(", ") || "No models returned",
      );
      setModelOptions([
        ...state.modelOptions,
        ...result.models.map((model) => `${providerId}/${model}`),
      ]);
    } else {
      // error_type alone reads as "application error". The message says which
      // variable is missing and where to get a key, so lead with it.
      updateProviderCard(
        providerId,
        "offline",
        result.error_type,
        result.message || result.error_type,
      );
    }
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

async function hydrateModelOptions() {
  try {
    await loadModelOptions();
  } catch {
    // Model fields remain editable when optional catalog hydration is unavailable.
  }
}

async function loadModelOptions(refresh = false) {
  const result = await api("/admin/api/models" + (refresh ? "/refresh" : ""), {
    method: refresh ? "POST" : "GET",
  });
  setModelOptions(result.models);
  setBlindModels(result.blind_models);
  return result;
}

async function refreshModelOptions(button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Refreshing";
  try {
    const result = await loadModelOptions(true);
    const failedProviders = result.failed_providers || [];
    if (failedProviders.length) {
      const labels = failedProviders.map(providerDisplayName).join(", ");
      showMessage(
        `${state.modelOptions.length} models available; could not refresh ${labels}`,
        "warn",
      );
    } else {
      showMessage(`${state.modelOptions.length} models available`, "ok");
    }
  } catch (error) {
    showMessage(`Could not refresh models: ${error.message}`, "error");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

function buildAnthropicOAuthControl(wrapper) {
  const warning = document.createElement("div");
  warning.className = "guide-note guide-note-warn";
  const warningText = document.createElement("p");
  warningText.textContent =
    "Anthropic does not permit routing requests through Free, Pro, or Max " +
    "plan credentials in third-party tools, and this provider additionally " +
    "refuses any request that did not come from Claude Code or the Claude " +
    "Agent SDK. Read docs/ANTHROPIC-SUBSCRIPTION.md before using either " +
    "option below.";
  warning.appendChild(warningText);

  const status = document.createElement("div");
  status.className = "field-description";
  status.textContent = "Checking for available credentials...";

  const details = document.createElement("dl");
  details.className = "anthropic-oauth-details";
  details.hidden = true;

  const importButton = document.createElement("button");
  importButton.type = "button";
  importButton.className = "secondary-button";
  importButton.textContent = "Use Claude Code credentials";
  importButton.disabled = true;

  const loginButton = document.createElement("button");
  loginButton.type = "button";
  loginButton.className = "primary-button";
  loginButton.textContent = "Sign in with Anthropic";

  const refreshButton = document.createElement("button");
  refreshButton.type = "button";
  refreshButton.className = "secondary-button";
  refreshButton.textContent = "Refresh now";
  refreshButton.disabled = true;

  const disconnectButton = document.createElement("button");
  disconnectButton.type = "button";
  disconnectButton.className = "secondary-button";
  disconnectButton.textContent = "Disconnect";
  disconnectButton.disabled = true;

  // buttons[0] must stay the import button: refreshAnthropicOAuthSources
  // takes it positionally and is what enables or disables it.
  const buttons = [importButton, loginButton, refreshButton, disconnectButton];
  // The per-row Refresh/Disconnect buttons are built inside the renderer,
  // which has no other way back to the card's status line, so the array the
  // card already passes around carries them.
  buttons.status = status;
  buttons.details = details;
  importButton.addEventListener("click", () => {
    importAnthropicOAuthClaudeCode(importButton, buttons, status, details);
  });
  loginButton.addEventListener("click", () => {
    startAnthropicOAuthLogin(loginButton, buttons, status, details, paste);
  });
  refreshButton.addEventListener("click", () => {
    refreshAnthropicOAuthCredential(refreshButton, buttons, status, details);
  });
  disconnectButton.addEventListener("click", () => {
    disconnectAnthropicOAuthCredential(
      disconnectButton,
      buttons,
      status,
      details,
    );
  });

  const paste = buildAnthropicOAuthPasteField();

  wrapper.append(
    warning,
    status,
    details,
    importButton,
    loginButton,
    refreshButton,
    disconnectButton,
    paste.root,
  );
  refreshAnthropicOAuthSources(importButton, status, details, buttons);
  return wrapper;
}

// The sign-in code used to be collected with a modal window prompt, which some
// browsers suppress outright and which cannot show the warning the paste is
// consenting to. An inline field can.
function buildAnthropicOAuthPasteField() {
  const root = document.createElement("div");
  root.className = "anthropic-oauth-paste";
  root.hidden = true;

  const note = document.createElement("p");
  note.className = "field-description";
  note.textContent =
    "Pasting this code stores a Claude subscription credential in MCC and " +
    "uses it for Claude Code traffic, which Anthropic's terms do not permit " +
    "for third-party tools. See docs/ANTHROPIC-SUBSCRIPTION.md.";

  const input = document.createElement("input");
  input.type = "text";
  input.autocomplete = "off";
  input.spellcheck = false;
  input.placeholder = "Paste the code Anthropic showed (code#state)";

  const submit = document.createElement("button");
  submit.type = "button";
  submit.className = "primary-button";
  submit.textContent = "Finish sign-in";

  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "secondary-button";
  cancel.textContent = "Cancel";

  root.append(note, input, submit, cancel);
  return { root, note, input, submit, cancel };
}

function formatOAuthInstant(seconds) {
  if (seconds === null || seconds === undefined) return null;
  const when = new Date(seconds * 1000);
  if (Number.isNaN(when.getTime())) return null;
  const deltaMs = when.getTime() - Date.now();
  const minutes = Math.round(Math.abs(deltaMs) / 60000);
  const relative =
    minutes < 60
      ? `${minutes} min`
      : `${Math.round(minutes / 60)} h`;
  const suffix = deltaMs < 0 ? `${relative} ago` : `in ${relative}`;
  return { text: `${when.toLocaleString()} (${suffix})`, expired: deltaMs < 0 };
}

// Anthropic sends the reset headers as unix seconds. Rendering the number
// verbatim would be honest and unreadable; rendering the same instant in the
// viewer's own clock is the same fact, legible. Anything that is not a bare
// integer is printed exactly as it arrived.
function formatOAuthReset(value) {
  if (typeof value !== "string" || !/^\d{9,13}$/.test(value)) return value;
  const seconds = value.length > 10 ? Number(value) / 1000 : Number(value);
  const when = new Date(seconds * 1000);
  if (Number.isNaN(when.getTime())) return value;
  return `${when.toLocaleString()} (${value})`;
}

function appendOAuthDetail(list, term, value, options) {
  const dt = document.createElement("dt");
  dt.textContent = term;
  const dd = document.createElement("dd");
  dd.textContent = value;
  if (options && options.warn) dd.className = "value-expired";
  list.append(dt, dd);
}

// Never invents a window. Every figure below is either a string Anthropic sent
// on a real response or the literal "not yet observed".
// One row per stored account, plus -- when nothing is stored yet -- the
// read-only view of Claude Code's own credential, which is what this card
// showed for its whole life before 7.30.0.
function renderAnthropicOAuthDetails(details, sources, buttons) {
  details.replaceChildren();
  const accounts = Array.isArray(sources.accounts) ? sources.accounts : [];
  if (!accounts.length) {
    const tokens = sources.claude_code;
    if (!tokens || !tokens.available) {
      details.hidden = true;
      return;
    }
    details.hidden = false;
    const list = document.createElement("dl");
    list.className = "anthropic-oauth-details";
    details.appendChild(list);
    renderAnthropicOAuthTokenDetails(list, tokens, sources.windows || {});
    return;
  }
  details.hidden = false;
  accounts.forEach((account) => {
    const row = document.createElement("section");
    row.className = "oauth-account-row";
    row.dataset.accountId = account.account_id || "";

    const heading = document.createElement("div");
    heading.className = "oauth-account-heading";
    const title = document.createElement("strong");
    title.className = "oauth-account-name";
    // The account's name, and only the mask when it has none: exactly the
    // fallback every other credential row uses.
    title.textContent =
      account.name || account.masked_token || account.account_id || "account";
    heading.appendChild(title);

    const refreshRow = document.createElement("button");
    refreshRow.type = "button";
    refreshRow.className = "secondary-button oauth-account-refresh";
    refreshRow.textContent = "Refresh now";
    refreshRow.addEventListener("click", () => {
      refreshAnthropicOAuthAccount(account.account_id, refreshRow, buttons);
    });

    const disconnectRow = document.createElement("button");
    disconnectRow.type = "button";
    disconnectRow.className = "secondary-button oauth-account-disconnect";
    disconnectRow.textContent = "Disconnect";
    disconnectRow.addEventListener("click", () => {
      disconnectAnthropicOAuthAccount(
        account.account_id,
        disconnectRow,
        buttons,
      );
    });

    heading.append(refreshRow, disconnectRow);
    row.appendChild(heading);

    const list = document.createElement("dl");
    list.className = "anthropic-oauth-details";
    row.appendChild(list);
    appendOAuthDetail(list, "Account", account.account_id || "unknown");
    appendOAuthDetail(list, "Added from", account.origin || "mcc");
    if (account.origin_path) {
      appendOAuthDetail(
        list,
        "Write-back",
        account.write_back_effective
          ? `on -- refreshes are written to ${account.origin_path}`
          : "off -- refreshes stay in MCC's own store",
      );
    }
    renderAnthropicOAuthTokenDetails(list, account, account.windows || {});
    details.appendChild(row);
  });
}

function renderAnthropicOAuthTokenDetails(details, tokens, windows) {
  appendOAuthDetail(details, "Plan", tokens.subscription_type || "unknown");
  if (tokens.rate_limit_tier) {
    appendOAuthDetail(details, "Rate-limit tier", tokens.rate_limit_tier);
  }
  appendOAuthDetail(details, "Credential source", tokens.source || "unknown");

  const access = formatOAuthInstant(tokens.expires_at);
  if (access) {
    appendOAuthDetail(details, "Access token expires", access.text, {
      warn: access.expired,
    });
  }
  const refresh = formatOAuthInstant(tokens.refresh_token_expires_at);
  if (refresh) {
    appendOAuthDetail(details, "Refresh token expires", refresh.text, {
      warn: refresh.expired,
    });
    if (refresh.expired) {
      // Without this the card showed a stale date and nothing else, and the
      // credential looked renewable while being unrenewable. Refreshing it
      // cannot help; only a new sign-in can.
      appendOAuthDetail(
        details,
        "Warning",
        "Refresh token expired -- sign in again, or import your Claude Code " +
          "credential. Refresh now cannot renew this.",
        { warn: true },
      );
    }
  }
  if (tokens.scopes && tokens.scopes.length) {
    appendOAuthDetail(details, "Scopes", tokens.scopes.join(" "), {
      warn: !tokens.has_inference_scope,
    });
    if (!tokens.has_inference_scope) {
      appendOAuthDetail(
        details,
        "Warning",
        "This credential is missing user:inference and cannot answer requests.",
        { warn: true },
      );
    }
  }

  if (!windows || !windows.observed) {
    appendOAuthDetail(
      details,
      "Usage windows",
      "not yet observed -- no Anthropic response has carried a rate-limit header yet",
    );
    return;
  }
  appendOAuthDetail(details, "5-hour window used", windows.five_hour_utilization);
  appendOAuthDetail(details, "5-hour window resets", formatOAuthReset(windows.five_hour_reset));
  appendOAuthDetail(details, "Weekly window used", windows.weekly_utilization);
  appendOAuthDetail(details, "Weekly window resets", formatOAuthReset(windows.weekly_reset));
  appendOAuthDetail(details, "Overage", windows.overage_status);
  if (windows.reset && windows.reset !== "not yet observed") {
    appendOAuthDetail(details, "Unified reset", formatOAuthReset(windows.reset));
  }
  const limited =
    windows.status === "session-limit-reached" ||
    windows.status === "weekly-limit-reached";
  if (limited) {
    const which =
      windows.status === "session-limit-reached" ? "5-hour" : "weekly";
    const resets = formatOAuthReset(
      windows.status === "session-limit-reached"
        ? windows.five_hour_reset
        : windows.weekly_reset,
    );
    appendOAuthDetail(
      details,
      "Anthropic says",
      `You hit your ${which} window; it resets at ${resets}.`,
      { warn: true },
    );
  } else if (windows.status && windows.status !== "not yet observed") {
    appendOAuthDetail(details, "Anthropic says", windows.status);
  }
}

async function refreshAnthropicOAuthSources(
  importButton,
  status,
  details,
  buttons,
) {
  try {
    const sources = await api("/admin/api/anthropic-oauth/sources");
    if (details) renderAnthropicOAuthDetails(details, sources, buttons);
    const accounts = Array.isArray(sources.accounts) ? sources.accounts : [];
    // Refresh and Disconnect only mean anything against MCC's own store:
    // Claude Code's file is read-only to MCC and must never be renewed or
    // removed from here. With accounts stored the per-row buttons are the
    // real controls and these two act on the primary account.
    setAnthropicOAuthManagedButtons(buttons, accounts.length > 0);
    // Once any account is stored, signing in ADDS one. Saying so on the
    // button is the whole difference between "this will replace what I have"
    // and "this will give me a second account".
    const loginButton = Array.isArray(buttons) ? buttons[1] : null;
    if (loginButton) {
      loginButton.textContent = accounts.length
        ? "Sign in another account"
        : "Sign in with Anthropic";
    }
    const mccNote = accounts.length
      ? `${accounts.length} account(s) stored.`
      : "No credential stored in MCC yet.";
    if (sources.claude_code.available) {
      importButton.disabled = false;
      status.textContent =
        `Claude Code credential found (${sources.claude_code.masked_token}). ` +
        mccNote;
    } else {
      importButton.disabled = true;
      status.textContent = accounts.length
        ? `Signed in. ${mccNote}`
        : "No credentials found. Sign in below, or log in to Claude Code first.";
    }
  } catch (error) {
    status.textContent = `Could not check credential sources: ${error.message}`;
  }
}

// The two managed-store controls live at fixed positions in the buttons array
// created by buildAnthropicOAuthControl.
function setAnthropicOAuthManagedButtons(buttons, available) {
  if (!Array.isArray(buttons)) return;
  const refreshButton = buttons[2];
  const disconnectButton = buttons[3];
  if (refreshButton) refreshButton.disabled = !available;
  if (disconnectButton) disconnectButton.disabled = !available;
}

// The Anthropic twin of fillChatGPTOAuthFields. Both admin routes return a
// credential_reference and the dashboard used to throw it away, which left
// ANTHROPIC_OAUTH_ACCESS_TOKEN empty and the provider inactive until some
// later settings load happened to back-fill it.
function fillAnthropicOAuthFields(credentialReference) {
  const tokenField = document.querySelector(
    '[data-key="ANTHROPIC_OAUTH_ACCESS_TOKEN"] input',
  );
  if (tokenField && credentialReference) {
    tokenField.value = credentialReference;
    // The input event is what marks the form dirty so Apply picks it up.
    tokenField.dispatchEvent(new Event("input"));
  }
}

async function refreshAnthropicOAuthCredential(
  button,
  buttons,
  status,
  details,
) {
  buttons.forEach((candidate) => {
    candidate.disabled = true;
  });
  const original = button.textContent;
  button.textContent = "Refreshing...";
  try {
    const result = await api("/admin/api/anthropic-oauth/refresh", {
      method: "POST",
      body: "{}",
    });
    if (result.status === "complete") {
      fillAnthropicOAuthFields(result.credential_reference);
      const expires = formatOAuthInstant(result.expires_at);
      showMessage(
        expires
          ? `Refreshed the Claude subscription credential. Access token expires ${expires.text}.`
          : "Refreshed the Claude subscription credential.",
        "ok",
      );
    }
  } catch (error) {
    // The route answers 503 for a transient failure and 401 for a definitive
    // one, and the detail text already says which. Showing it verbatim is the
    // point: "Anthropic is rate-limiting refreshes, the credential was kept"
    // and "sign in again" call for opposite actions.
    showMessage(`Could not refresh: ${error.message}`, "error");
  } finally {
    button.textContent = original;
    buttons.forEach((candidate) => {
      candidate.disabled = false;
    });
    refreshAnthropicOAuthSources(buttons[0], status, details, buttons);
  }
}

async function disconnectAnthropicOAuthCredential(
  button,
  buttons,
  status,
  details,
) {
  const confirmed = window.confirm(
    "Disconnect the Claude subscription credential MCC owns?\n\n" +
      "It is kept on disk as anthropic_oauth.json.dead-<timestamp>, not " +
      "deleted, and your Claude Code login is not touched.",
  );
  if (!confirmed) return;
  buttons.forEach((candidate) => {
    candidate.disabled = true;
  });
  const original = button.textContent;
  button.textContent = "Disconnecting...";
  try {
    const result = await api("/admin/api/anthropic-oauth/disconnect", {
      method: "POST",
      body: "{}",
    });
    if (result.status === "complete") {
      showMessage(result.message, "ok");
    }
  } catch (error) {
    showMessage(`Could not disconnect: ${error.message}`, "error");
  } finally {
    button.textContent = original;
    buttons.forEach((candidate) => {
      candidate.disabled = false;
    });
    refreshAnthropicOAuthSources(buttons[0], status, details, buttons);
  }
}

// The per-row controls. Both take the account id in the path, so each acts on
// exactly one account and the rest of the pool is untouched either way.
async function refreshAnthropicOAuthAccount(accountId, button, buttons) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Refreshing...";
  try {
    const result = await api(
      `/admin/api/anthropic-oauth/accounts/${encodeURIComponent(accountId)}/refresh`,
      { method: "POST", body: "{}" },
    );
    if (result.status === "complete") showMessage(result.message, "ok");
  } catch (error) {
    showMessage(`Could not refresh that account: ${error.message}`, "error");
  } finally {
    button.textContent = original;
    button.disabled = false;
    refreshAnthropicOAuthSources(
      buttons[0],
      buttons.status,
      buttons.details,
      buttons,
    );
  }
}

async function disconnectAnthropicOAuthAccount(accountId, button, buttons) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Disconnecting...";
  try {
    const result = await api(
      `/admin/api/anthropic-oauth/accounts/${encodeURIComponent(accountId)}/disconnect`,
      { method: "POST", body: "{}" },
    );
    if (result.status === "complete") showMessage(result.message, "ok");
  } catch (error) {
    showMessage(`Could not disconnect that account: ${error.message}`, "error");
  } finally {
    button.textContent = original;
    button.disabled = false;
    refreshAnthropicOAuthSources(
      buttons[0],
      buttons.status,
      buttons.details,
      buttons,
    );
  }
}

async function importAnthropicOAuthClaudeCode(button, buttons, status, details) {
  buttons.forEach((candidate) => {
    candidate.disabled = true;
  });
  const original = button.textContent;
  button.textContent = "Importing...";
  try {
    const result = await api("/admin/api/anthropic-oauth/import-claude-code", {
      method: "POST",
      body: "{}",
    });
    if (result.status === "complete") {
      fillAnthropicOAuthFields(result.credential_reference);
      showMessage(
        "Imported the Claude Code credential into MCC's private store. " +
          "Apply settings to activate the provider.",
        "ok",
      );
    }
  } catch (error) {
    showMessage(`Could not import Claude Code credentials: ${error.message}`, "error");
  } finally {
    button.textContent = original;
    buttons.forEach((candidate) => {
      candidate.disabled = false;
    });
    refreshAnthropicOAuthSources(buttons[0], status, details, buttons);
  }
}

// Sign-in has two transports, the same two Claude Code itself offers.
//
// The loopback flow is tried first: a callback server on 127.0.0.1 catches
// Anthropic's redirect, so approving in the browser is the whole interaction
// and there is no code to mis-copy. If this process and the browser do not
// share a localhost -- WSL, SSH, a container -- the server answers 503 up
// front and the paste flow runs instead, rather than silently waiting five
// minutes for a callback that can never arrive.
async function startAnthropicOAuthLogin(button, buttons, status, details, paste) {
  buttons.forEach((candidate) => {
    candidate.disabled = true;
  });
  const original = button.textContent;
  button.textContent = "Starting sign-in...";
  try {
    let completed = false;
    try {
      completed = await runAnthropicOAuthLoopbackLogin(status);
    } catch (error) {
      if (!isAnthropicLoopbackUnavailable(error)) throw error;
      showMessage(
        `Automatic sign-in is not available here (${error.message}) -- ` +
          "falling back to pasting the code.",
        "warn",
      );
    }
    if (!completed) {
      await runAnthropicOAuthPasteLogin(status, paste);
    }
  } catch (error) {
    showMessage(`Anthropic sign-in failed: ${error.message}`, "error");
  } finally {
    button.textContent = original;
    buttons.forEach((candidate) => {
      candidate.disabled = false;
    });
    refreshAnthropicOAuthSources(buttons[0], status, details, buttons);
  }
}

// api() throws a bare Error carrying the route's detail text, so the 503 the
// loopback initiate route answers is identified by that detail rather than by
// a status code the helper does not surface.
function isAnthropicLoopbackUnavailable(error) {
  const message = String(error && error.message ? error.message : "");
  return (
    message.includes("Use the paste flow instead") ||
    message.startsWith("503 ")
  );
}

async function runAnthropicOAuthLoopbackLogin(status) {
  const initiate = await api(
    "/admin/api/anthropic-oauth/loopback/initiate?same_host_confirmed=true",
    { method: "POST", body: "{}" },
  );
  window.open(initiate.authorize_url, "_blank", "noopener");
  showMessage(
    "Anthropic OAuth: approve access in the new tab. Nothing to copy -- " +
      "this page finishes on its own.",
    "warn",
  );
  status.textContent = "Waiting for the browser to come back...";
  const deadline = Date.now() + 5 * 60 * 1000;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 3000));
    const result = await api("/admin/api/anthropic-oauth/loopback/status", {
      method: "POST",
      body: "{}",
    });
    if (result.status === "complete") {
      fillAnthropicOAuthFields(result.credential_reference);
      showMessage(
        "Signed in with Anthropic. Apply settings to activate the provider.",
        "ok",
      );
      return true;
    }
    if (result.status === "error") {
      throw new Error(result.message || "Sign-in failed");
    }
  }
  throw new Error("Timed out waiting for the browser to complete the sign-in");
}

async function runAnthropicOAuthPasteLogin(status, paste) {
  const initiate = await api("/admin/api/anthropic-oauth/initiate", {
    method: "POST",
    body: "{}",
  });
  window.open(initiate.authorize_url, "_blank", "noopener");
  showMessage(
    "Anthropic OAuth: approve access in the new tab, then paste the code " +
      "it shows into the field below. Pasting the whole callback URL from " +
      "the address bar works too.",
    "warn",
  );
  const pasted = await promptForAnthropicOAuthCode(paste);
  if (!pasted) {
    status.textContent = "Sign-in cancelled: no code entered.";
    return false;
  }
  const result = await api("/admin/api/anthropic-oauth/complete", {
    method: "POST",
    body: JSON.stringify({
      pasted_code: pasted,
      verifier: initiate.verifier,
    }),
  });
  if (result.status === "complete") {
    fillAnthropicOAuthFields(result.credential_reference);
    showMessage(
      "Signed in with Anthropic. Apply settings to activate the provider.",
      "ok",
    );
    return true;
  }
  return false;
}

function promptForAnthropicOAuthCode(paste) {
  if (!paste) return Promise.resolve(null);
  paste.root.hidden = false;
  paste.input.value = "";
  paste.input.focus();
  return new Promise((resolve) => {
    const finish = (value) => {
      paste.root.hidden = true;
      paste.submit.onclick = null;
      paste.cancel.onclick = null;
      paste.input.onkeydown = null;
      resolve(value);
    };
    paste.submit.onclick = () => finish(paste.input.value.trim() || null);
    paste.cancel.onclick = () => finish(null);
    paste.input.onkeydown = (event) => {
      if (event.key === "Enter") finish(paste.input.value.trim() || null);
    };
  });
}

/* Everything below is offline. The plan is decoded from the credential
   already on disk, the catalogue is the installed Codex CLI's own document,
   and the windows are headers a real OpenAI response carried. Nothing here
   contacts OpenAI, and no token, sub or email is ever fetched or shown. */
// The two per-account controls, which this card did not have at all before
// 7.30.0: ChatGPT had no refresh-now route and no disconnect route.
async function chatgptOAuthAccountAction(accountId, action, button, details) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = action === "refresh" ? "Refreshing..." : "Disconnecting...";
  try {
    const result = await api(
      `/admin/api/chatgpt-oauth/accounts/${encodeURIComponent(accountId)}/${action}`,
      { method: "POST", body: "{}" },
    );
    if (result.status === "complete") showMessage(result.message, "ok");
  } catch (error) {
    showMessage(`Could not ${action} that account: ${error.message}`, "error");
  } finally {
    button.textContent = original;
    button.disabled = false;
    refreshChatGPTOAuthStatus(details);
  }
}

async function refreshChatGPTOAuthStatus(details) {
  if (!details) return;
  let status;
  try {
    status = await api("/admin/api/chatgpt-oauth/status");
  } catch (error) {
    details.hidden = true;
    return;
  }
  details.replaceChildren();
  details.hidden = false;
  const accounts = Array.isArray(status.accounts) ? status.accounts : [];
  // One row per account, each with the two controls this card never had
  // before 7.30.0: it could say a credential was stale and offer nothing.
  accounts.forEach((account) => {
    const row = document.createElement("div");
    row.className = "oauth-account-heading";
    row.dataset.accountId = account.account_id || "";
    const title = document.createElement("strong");
    title.className = "oauth-account-name";
    title.textContent = account.name || account.account_id || "account";
    row.appendChild(title);

    const refreshRow = document.createElement("button");
    refreshRow.type = "button";
    refreshRow.className = "secondary-button oauth-account-refresh";
    refreshRow.textContent = "Refresh now";
    refreshRow.addEventListener("click", () => {
      chatgptOAuthAccountAction(account.account_id, "refresh", refreshRow, details);
    });

    const disconnectRow = document.createElement("button");
    disconnectRow.type = "button";
    disconnectRow.className = "secondary-button oauth-account-disconnect";
    disconnectRow.textContent = "Disconnect";
    disconnectRow.addEventListener("click", () => {
      chatgptOAuthAccountAction(
        account.account_id,
        "disconnect",
        disconnectRow,
        details,
      );
    });

    row.append(refreshRow, disconnectRow);
    details.appendChild(row);

    const list = document.createElement("dl");
    list.className = "anthropic-oauth-details";
    appendOAuthDetail(list, "Account", account.account_id || "unknown");
    appendOAuthDetail(list, "Plan", account.plan_type || "unknown");
    appendOAuthDetail(list, "Added from", account.origin || "mcc");
    if (account.origin_path) {
      appendOAuthDetail(
        list,
        "Write-back",
        account.write_back_effective
          ? `on -- refreshes are written to ${account.origin_path}`
          : "off -- refreshes stay in MCC's own store",
      );
    }
    details.appendChild(list);
  });
  if (!accounts.length) {
    appendOAuthDetail(details, "Plan", status.plan_type || "unknown");
  }
  const catalogue = status.catalogue || {};
  if (catalogue.available) {
    appendOAuthDetail(
      details,
      "Model catalogue",
      `Codex CLI ${catalogue.version || "?"} (${catalogue.model_count} models, ` +
        `read from ${catalogue.source_name})`,
    );
    if ((catalogue.retired_model_ids || []).length) {
      appendOAuthDetail(
        details,
        "Retired upstream",
        catalogue.retired_model_ids.join(", "),
      );
    }
  } else {
    appendOAuthDetail(
      details,
      "Model catalogue",
      "Codex CLI not found — the model list falls back to what this " +
        "credential has already been served, then to the offline seed list.",
    );
  }
  const windows = status.windows || {};
  if (!windows.observed) {
    appendOAuthDetail(
      details,
      "Usage windows",
      "not yet observed — no OpenAI response has carried a usage header yet",
    );
    return;
  }
  appendOAuthDetail(details, "Primary window used", windows.primary_used_percent);
  appendOAuthDetail(details, "Primary window resets", windows.primary_reset_at);
  appendOAuthDetail(
    details,
    "Secondary window used",
    windows.secondary_used_percent,
  );
  appendOAuthDetail(details, "Secondary window resets", windows.secondary_reset_at);
  appendOAuthDetail(details, "Credits balance", windows.credits_balance);
  if (windows.limit_name && windows.limit_name !== "not yet observed") {
    appendOAuthDetail(details, "OpenAI says", windows.limit_name, { warn: true });
  }
}

async function importChatGPTOAuthCodexTokens(button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Importing...";
  try {
    const result = await api("/admin/api/chatgpt-oauth/import-codex", {
      method: "POST",
      body: "{}",
    });
    if (result.status === "complete") {
      fillChatGPTOAuthFields(
        result.credential_reference,
        result.account_id,
      );
      showMessage(
        "Copied renewable Codex credentials. Apply settings to activate the provider.",
        "ok",
      );
    }
  } catch (error) {
    showMessage(`Could not import Codex tokens: ${error.message}`, "error");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

async function runChatGPTOAuthLogin(button, buttons, progressLabel, login) {
  const labels = buttons.map((candidate) => candidate.textContent);
  buttons.forEach((candidate) => {
    candidate.disabled = true;
  });
  button.textContent = progressLabel;
  try {
    await login();
  } catch (error) {
    showMessage(`ChatGPT OAuth login failed: ${error.message}`, "error");
  } finally {
    buttons.forEach((candidate, index) => {
      candidate.disabled = false;
      candidate.textContent = labels[index];
    });
  }
}

async function startChatGPTOAuthDeviceLogin(button, buttons) {
  await runChatGPTOAuthLogin(
    button,
    buttons,
    "Starting device login...",
    startDeviceOAuthLogin,
  );
}

async function startChatGPTOAuthBrowserLogin(button, buttons) {
  await runChatGPTOAuthLogin(
    button,
    buttons,
    "Starting browser login...",
    async () => {
      // This explicit option is only safe when the browser and My Claude Code share the
      // same localhost. Device-code login is the cross-WSL/remote default.
      const initiate = await api(
        "/admin/api/chatgpt-oauth/browser/initiate?same_host_confirmed=true",
        {
          method: "POST",
          body: "{}",
        },
      );
      window.open(initiate.authorize_url, "_blank", "noopener");
      showMessage(
        "ChatGPT OAuth: complete the login in the same-device browser tab.",
        "warn",
      );
      await pollBrowserOAuthLogin();
    },
  );
}

async function pollBrowserOAuthLogin() {
  const deadline = Date.now() + 5 * 60 * 1000;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 3000));
    const result = await api("/admin/api/chatgpt-oauth/browser/status", {
      method: "POST",
      body: "{}",
    });
    if (result.status === "complete") {
      fillChatGPTOAuthFields(
        result.credential_reference,
        result.account_id,
      );
      showMessage(
        "ChatGPT OAuth login complete. Apply settings to activate the provider.",
        "ok",
      );
      return;
    }
    if (result.status === "error") {
      throw new Error(result.message || "Browser login failed");
    }
  }
  throw new Error("Timed out waiting for the browser login to complete");
}

function fillChatGPTOAuthFields(credentialReference, accountId) {
  const tokenField = document.querySelector(
    '[data-key="CHATGPT_OAUTH_ACCESS_TOKEN"] input',
  );
  const accountField = document.querySelector(
    '[data-key="CHATGPT_OAUTH_ACCOUNT_ID"] input',
  );
  if (tokenField) {
    tokenField.value = credentialReference;
    tokenField.dispatchEvent(new Event("input"));
  }
  if (accountField) {
    accountField.value = accountId || "";
    accountField.dispatchEvent(new Event("input"));
  }
}

async function startDeviceOAuthLogin() {
  const initiate = await api("/admin/api/chatgpt-oauth/initiate", {
    method: "POST",
    body: "{}",
  });
  const verificationUrl = initiate.verification_url;
  const userCode = initiate.user_code;

  // Open the verification page automatically; the user only enters the code.
  window.open(verificationUrl, "_blank", "noopener");
  showMessage(
    `ChatGPT OAuth: a browser tab was opened for ${verificationUrl} - enter code ${userCode}`,
    "warn",
  );

  const deadline = Date.now() + 10 * 60 * 1000;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 8000));
    const result = await api("/admin/api/chatgpt-oauth/exchange", {
      method: "POST",
      body: JSON.stringify({
        device_auth_id: initiate.device_auth_id,
        user_code: userCode,
      }),
    });
    if (result.status === "complete") {
      fillChatGPTOAuthFields(
        result.credential_reference,
        result.account_id,
      );
      showMessage(
        "ChatGPT OAuth login complete. Apply settings to activate the provider.",
        "ok",
      );
      return;
    }
  }
  throw new Error("Timed out waiting for device authorization");
}

function providerDisplayName(providerId) {
  const provider = state.config?.provider_status?.find(
    (candidate) => candidate.provider_id === providerId,
  );
  return provider?.display_name || providerId;
}

function setBlindModels(models) {
  state.blindModels = new Set(
    (models || []).filter((model) => typeof model === "string" && model.trim()),
  );
  updateVisionRouting();
}

function setModelOptions(models) {
  state.modelOptions = Array.from(
    new Set(models.filter((model) => typeof model === "string" && model.trim())),
  ).sort((left, right) => left.localeCompare(right));
  state.modelComboboxes.forEach((combobox) => {
    if (combobox.isOpen) combobox.render(combobox.query);
  });
}

function webSearchProviders() {
  const providerField = state.fields.get("WEB_SEARCH_PROVIDER");
  if (!providerField) return [];
  const credentialFields = Array.from(state.fields.values()).filter(
    (field) => field.section === "websearch" && field.secret,
  );
  return providerField.options
    .filter((item) => !["auto", "off", "disabled"].includes(item.value))
    .map((item) => {
      const credential = credentialFields.find(
        (field) => field.label === `${item.label} API Key`,
      );
      const baseUrlField =
        item.value === "searxng" ? state.fields.get("SEARXNG_BASE_URL") : null;
      const configured = credential
        ? credential.configured
        : baseUrlField
          ? baseUrlField.configured
          : true;
      return {
        id: item.value,
        label: item.label,
        envKey: credential ? credential.key : null,
        rotationKey: credential ? `${credential.key}_ROTATION` : null,
        configured,
      };
    });
}

function effectiveWebSearchProvider(providers, activeSelection) {
  if (activeSelection === "disabled") return null;
  if (activeSelection === "off") return "legacy";
  if (activeSelection !== "auto") return activeSelection;
  return (
    providers.find((provider) => provider.id !== "ddgs" && provider.configured)?.id ||
    "ddgs"
  );
}

function webSearchProviderMeta(provider, activeSelection, effectiveProvider) {
  const parts = [];
  if (effectiveProvider === provider.id) {
    parts.push(activeSelection === "auto" ? "Effective via auto" : "Selected");
  } else if (activeSelection === "auto" && provider.configured) {
    parts.push("Available");
  }
  parts.push(
    provider.envKey ||
      (provider.id === "searxng" ? "SEARXNG_BASE_URL" : "No key required"),
  );
  return parts.join(" · ");
}

// "What should happen" -- the configured route, in try-order -- is needed by
// both the hero headline and the observed-route line below it. Factored out
// so the two can't drift into two slightly different definitions of "the
// route" as the manifest evolves.
function webSearchConfiguredRoute(providers, activeSelection, effectiveProvider) {
  const fallbackPolicy =
    state.fields.get("WEB_SEARCH_FALLBACK_POLICY")?.value || "auto";
  const resolvedPolicy =
    fallbackPolicy === "auto"
      ? activeSelection === "auto"
        ? "legacy"
        : "none"
      : fallbackPolicy;
  const routeIds = [];
  if (activeSelection === "disabled") {
    routeIds.push("disabled");
  } else if (activeSelection === "off") {
    routeIds.push("legacy");
  } else if (effectiveProvider) {
    routeIds.push(effectiveProvider);
    if (
      (resolvedPolicy === "ddgs" || resolvedPolicy === "legacy") &&
      effectiveProvider !== "ddgs"
    ) {
      routeIds.push("ddgs");
    }
    if (resolvedPolicy === "legacy") routeIds.push("legacy");
  }
  return { fallbackPolicy, resolvedPolicy, routeIds };
}

function renderWebSearchRouteSummary(providers, activeSelection, effectiveProvider) {
  const summary = byId("webSearchRouteSummary");
  if (!summary) return;
  const effectiveDescriptor = providers.find(
    (provider) => provider.id === effectiveProvider,
  );
  const providerLabel = (providerId) =>
    providerId === "legacy"
      ? "Legacy DuckDuckGo scraper"
      : providers.find((provider) => provider.id === providerId)?.label || providerId;
  const selectionLabel =
    activeSelection === "auto"
      ? "Auto"
      : activeSelection === "off"
        ? "Legacy compatibility"
        : activeSelection === "disabled"
          ? "Disabled"
          : providers.find((provider) => provider.id === activeSelection)?.label ||
            activeSelection;
  const { fallbackPolicy, resolvedPolicy, routeIds } = webSearchConfiguredRoute(
    providers,
    activeSelection,
    effectiveProvider,
  );
  const ready =
    effectiveProvider === "legacy" ||
    Boolean(effectiveDescriptor && effectiveDescriptor.configured);
  // The headline is the one thing this bar has to answer at a glance: which
  // provider is actually serving requests right now. Everything else --
  // selection mode, fallback policy, the full chain -- is supporting detail.
  const headline =
    routeIds[0] === "disabled" ? "Web search disabled" : providerLabel(routeIds[0]);

  summary.innerHTML = "";
  const head = document.createElement("div");
  head.className = "ws-hero-head";
  const eyebrow = document.createElement("span");
  eyebrow.className = "eyebrow";
  eyebrow.textContent = "Active web search route";
  const note = document.createElement("span");
  note.className = `status-pill ${
    ready ? "ok" : effectiveProvider ? "warn" : "neutral"
  }`;
  note.textContent = ready
    ? "Ready"
    : effectiveProvider
      ? "Needs configuration"
      : "Search disabled";
  head.append(eyebrow, note);

  const headlineEl = document.createElement("strong");
  headlineEl.className = "ws-hero-provider";
  headlineEl.textContent = headline;

  const route = document.createElement("div");
  route.className = "route-summary-main";
  const path = document.createElement("span");
  path.className = "ws-hero-path";
  const pathLabel = document.createElement("span");
  pathLabel.className = "ws-hero-path-label";
  pathLabel.textContent = "Route: ";
  path.appendChild(pathLabel);
  // The headline already answers "which provider". This answers "and then
  // what": the primary hop carries the visual weight, each fallback hop
  // after it is quieter, so try-order is legible without reading the prose
  // sentence below -- the one thing a picker UI for a single value has no
  // equivalent of.
  if (routeIds[0] === "disabled") {
    path.appendChild(document.createTextNode("Disabled"));
  } else {
    routeIds.forEach((id, index) => {
      if (index > 0) {
        const arrow = document.createElement("span");
        arrow.className = "ws-hero-path-arrow";
        arrow.textContent = " → ";
        path.appendChild(arrow);
      }
      const hop = document.createElement("span");
      hop.className = index === 0 ? "ws-hero-path-primary" : "ws-hero-path-fallback";
      hop.textContent = providerLabel(id);
      path.appendChild(hop);
    });
  }
  const detail = document.createElement("span");
  detail.textContent =
    `Selection: ${selectionLabel} · Fallback: ${fallbackPolicy}` +
    (fallbackPolicy === "auto" ? ` (resolves to ${resolvedPolicy})` : "") +
    " · Configuration errors stop the route";
  route.append(path, detail);

  summary.append(head, headlineEl, route);
  renderWebSearchObservedRoute(state.webSearchLastRoute, routeIds);
}

// configuredRouteIds is optional: renderWebSearchRouteSummary already has it
// on hand and passes it through, but this is also called on its own after an
// analytics refresh (loadWebSearchAnalytics), where it recomputes the same
// route from current field state.
function renderWebSearchObservedRoute(lastRoute, configuredRouteIds = null) {
  const route = byId("webSearchRouteSummary")?.querySelector(".route-summary-main");
  if (!route) return;
  route.querySelector(".route-summary-observed")?.remove();
  if (!lastRoute) return;
  const observed = document.createElement("span");
  observed.className = "route-summary-observed";
  const providers = Array.isArray(lastRoute.providers)
    ? lastRoute.providers
    : [];
  const path =
    providers.length > 0
      ? providers.join(" → ")
      : lastRoute.terminal_provider || lastRoute.primary_provider || "unknown";
  const duration =
    lastRoute.duration_ms == null ? "unknown latency" : `${lastRoute.duration_ms} ms`;

  // The configured route describes intent; this line describes what the
  // last request actually did. When the two disagree on which provider goes
  // first -- almost always because the config changed after that request
  // ran -- that gap is the operationally true thing worth flagging here,
  // not just a timestamped restatement of the same fact as the headline.
  let routeIds = configuredRouteIds;
  if (!routeIds) {
    const allProviders = webSearchProviders();
    const activeSelection = state.fields.get("WEB_SEARCH_PROVIDER")?.value || "auto";
    const effectiveProvider = effectiveWebSearchProvider(allProviders, activeSelection);
    routeIds = webSearchConfiguredRoute(
      allProviders,
      activeSelection,
      effectiveProvider,
    ).routeIds;
  }
  const observedPrimary = lastRoute.primary_provider || providers[0] || null;
  const configuredPrimary = routeIds[0] || null;
  const drifted = Boolean(
    observedPrimary && configuredPrimary && observedPrimary !== configuredPrimary,
  );
  observed.classList.toggle("route-summary-observed-drift", drifted);
  observed.textContent =
    `Last observed: ${path} · ${lastRoute.status || "unknown"} · ${duration}` +
    (drifted ? " — configuration has changed since" : "");
  route.appendChild(observed);
}

function populateWebSearchAnalyticsProviders(providers) {
  const select = byId("webSearchFilterProvider");
  if (!select) return;
  const selected = select.value;
  select.replaceChildren(new Option("all providers", ""));
  providers.forEach((provider) => {
    select.add(new Option(provider.label, provider.id));
  });
  if (providers.some((provider) => provider.id === selected)) {
    select.value = selected;
  }
}

function selectWebSearchProvider(providerId) {
  const input = document.querySelector(
    'select[data-key="WEB_SEARCH_PROVIDER"]',
  );
  const field = state.fields.get("WEB_SEARCH_PROVIDER");
  if (!input || !field) return;
  input.value = providerId;
  field.value = providerId;
  input.dispatchEvent(new Event("change", { bubbles: true }));
  updateWebSearchCardsFromState();
}

// Advanced option fields are dotenv-only catalog entries whose env names are
// prefixed with the provider id (e.g. EXA_*, DDGS_*); the manifest marks them
// advanced so they group under each provider card instead of the grid.
function webSearchAdvancedFields(provider) {
  const prefix = `${provider.id.toUpperCase()}_`;
  return Array.from(state.fields.values()).filter(
    (field) =>
      field.section === "websearch" &&
      field.advanced &&
      field.key.startsWith(prefix),
  );
}

function renderWebSearchAdvanced(provider) {
  const fields = webSearchAdvancedFields(provider);
  if (fields.length === 0) return null;
  const details = document.createElement("details");
  details.className = "ws-advanced";
  // Open unless this browser was told otherwise: a collapsed-by-default group
  // is the same "the setting does not exist" problem in a different shape.
  const scopeKey = `websearch:${provider.id}`;
  details.open = !advancedCollapsed(scopeKey);
  details.addEventListener("toggle", () =>
    rememberAdvancedCollapsed(scopeKey, !details.open),
  );
  const summary = document.createElement("summary");
  summary.textContent = "Advanced options";
  details.appendChild(summary);
  fields.forEach((field) => details.appendChild(renderField(field)));
  return details;
}

// Only the effective provider needs to compete for attention; the rest of
// the strip stays legible but visibly secondary. Shared with
// updateWebSearchCardsFromState() so a live selection change re-applies the
// same badge instead of drifting from the initial render.
function setWebSearchCardEffective(card, isEffective) {
  card.classList.toggle("effective-provider", isEffective);
  const labelWrap = card.querySelector(".provider-title-label");
  let badge = labelWrap?.querySelector(".ws-active-badge");
  if (isEffective && labelWrap && !badge) {
    badge = document.createElement("span");
    badge.className = "ws-active-badge";
    badge.textContent = "Active";
    labelWrap.prepend(badge);
  } else if (!isEffective && badge) {
    badge.remove();
  }
}

function renderWebSearchProviders() {
  const grid = byId("webSearchGrid");
  if (!grid) return;
  grid.innerHTML = "";
  const active = state.fields.get("WEB_SEARCH_PROVIDER")?.value || "auto";
  const providers = webSearchProviders();
  const effectiveProvider = effectiveWebSearchProvider(providers, active);
  populateWebSearchAnalyticsProviders(providers);
  renderWebSearchRouteSummary(providers, active, effectiveProvider);
  providers.forEach((provider) => {
    const card = document.createElement("article");
    card.className = "provider-card";
    card.dataset.websearchProvider = provider.id;

    const title = document.createElement("div");
    title.className = "provider-title";
    const labelWrap = document.createElement("div");
    labelWrap.className = "provider-title-label";
    const label = document.createElement("strong");
    label.textContent = provider.label;
    labelWrap.appendChild(label);
    title.appendChild(labelWrap);
    const pill = document.createElement("span");
    pill.className = `status-pill ${provider.configured ? "ok" : "warn"}`;
    pill.textContent = provider.configured ? "Configured" : "Missing key";
    title.appendChild(pill);

    const meta = document.createElement("div");
    meta.className = "provider-meta";
    meta.textContent = webSearchProviderMeta(provider, active, effectiveProvider);

    const actions = document.createElement("div");
    actions.className = "card-actions";

    const selectButton = document.createElement("button");
    selectButton.type = "button";
    selectButton.className = "ghost-button";
    selectButton.textContent =
      active === provider.id ? "Selected" : "Use provider";
    selectButton.disabled = active === provider.id || !provider.configured;
    selectButton.addEventListener("click", () =>
      selectWebSearchProvider(provider.id),
    );
    actions.appendChild(selectButton);

    const testButton = document.createElement("button");
    testButton.type = "button";
    testButton.className = "test-button";
    testButton.textContent = "Test provider";
    testButton.addEventListener("click", () =>
      testWebSearchProvider(provider, testButton),
    );
    actions.appendChild(testButton);

    card.append(title, meta, actions);
    setWebSearchCardEffective(card, effectiveProvider === provider.id);
    const advanced = renderWebSearchAdvanced(provider);
    if (advanced) {
      card.appendChild(advanced);
    }
    if (provider.envKey) {
      const manageButton = document.createElement("button");
      manageButton.type = "button";
      manageButton.className = "ghost-button";
      manageButton.textContent = "Manage keys";
      const panel = document.createElement("div");
      panel.className = "ws-key-manager";
      panel.hidden = true;
      manageButton.addEventListener("click", () =>
        toggleKeyManager(provider, panel, manageButton),
      );
      actions.appendChild(manageButton);
      card.appendChild(panel);
    }
    grid.appendChild(card);
  });
  ["WEB_SEARCH_PROVIDER", "WEB_SEARCH_FALLBACK_POLICY"].forEach((key) => {
    const input = document.querySelector(`select[data-key="${key}"]`);
    if (!input || input.dataset.routeSummaryWired === "true") return;
    input.dataset.routeSummaryWired = "true";
    input.addEventListener("change", () => {
      const field = state.fields.get(key);
      if (field) field.value = input.value;
      updateWebSearchCardsFromState();
    });
  });
  applyWebSearchProviderFilter(byId("webSearchProviderSearch")?.value.trim().toLowerCase() || "");
  wireWebSearchProviderSearch();
}

// Filters by hiding (not detaching), same reasoning as the Providers tab's
// applyProviderFilter(): every field input has to stay in the document for
// changedValues()/Apply to see it, and `hidden` already removes an element
// from the tab order, so a hidden card cannot trap keyboard focus.
function applyWebSearchProviderFilter(query) {
  document.querySelectorAll("#webSearchGrid .provider-card").forEach((card) => {
    const haystack = (card.textContent || "").toLowerCase();
    card.hidden = Boolean(query) && !haystack.includes(query);
  });
}

function wireWebSearchProviderSearch() {
  const input = byId("webSearchProviderSearch");
  if (!input || input.dataset.wired === "true") return;
  input.dataset.wired = "true";
  input.addEventListener("input", () => {
    applyWebSearchProviderFilter(input.value.trim().toLowerCase());
  });
}

function updateWebSearchCard(providerId, status, label, metaText) {
  const card = document.querySelector(`[data-websearch-provider="${providerId}"]`);
  if (!card) return;
  const pill = card.querySelector(".status-pill");
  pill.className = `status-pill ${statusClass(status)}`;
  pill.textContent = label;
  if (metaText) {
    card.querySelector(".provider-meta").textContent = metaText;
  }
}

function updateWebSearchCardsFromState() {
  const active = state.fields.get("WEB_SEARCH_PROVIDER")?.value || "auto";
  const providers = webSearchProviders();
  const effectiveProvider = effectiveWebSearchProvider(providers, active);
  renderWebSearchRouteSummary(providers, active, effectiveProvider);
  providers.forEach((provider) => {
    const card = document.querySelector(
      `[data-websearch-provider="${provider.id}"]`,
    );
    if (!card) return;
    setWebSearchCardEffective(card, effectiveProvider === provider.id);
    const pill = card.querySelector(".status-pill");
    pill.className = `status-pill ${provider.configured ? "ok" : "warn"}`;
    pill.textContent = provider.configured ? "Configured" : "Missing key";
    card.querySelector(".provider-meta").textContent = webSearchProviderMeta(
      provider,
      active,
      effectiveProvider,
    );
    const selectButton = Array.from(card.querySelectorAll("button")).find(
      (button) =>
        button.textContent === "Selected" || button.textContent === "Use provider",
    );
    if (selectButton) {
      selectButton.textContent = active === provider.id ? "Selected" : "Use provider";
      selectButton.disabled = active === provider.id || !provider.configured;
    }
  });
}

async function refreshConfigState() {
  const config = await api("/admin/api/config");
  state.config = config;
  state.fields = new Map(config.fields.map((field) => [field.key, field]));
  config.fields.forEach((field) => {
    const input = document.querySelector(`[data-key="${field.key}"]`);
    if (input && input.dataset) {
      input.dataset.configured = field.configured ? "true" : "false";
    }
  });
  updateWebSearchCardsFromState();
  // state.fields is the calculator's fallback when a control has not been
  // touched, so a refresh that repopulates it must repaint the readout.
  updateDeadlineCalculator();
}

async function toggleKeyManager(provider, panel, button) {
  if (panel.hidden) {
    panel.hidden = false;
    button.textContent = "Hide keys";
    await loadKeyManager(provider, panel);
  } else {
    panel.hidden = true;
    button.textContent = "Manage keys";
    // The card only needs the whole grid row while a rail is on it.
    markKeyRailHost(panel, false);
  }
}

function keyHealthClass(health) {
  if (!health) return "neutral";
  if (health.state === "healthy") return "ok";
  if (health.state === "cooldown") return "warn";
  return "error";
}

function keyHealthText(health) {
  if (!health) return "Unused";
  const stateName = String(health.state || "unknown").replace(/_/g, " ");
  return `${stateName} · ${health.requests} req · ${health.failures} err`;
}

async function loadKeyManager(provider, panel) {
  panel.innerHTML = "";
  const list = keyRailList("ws-key-list");
  markKeyRailHost(panel);
  panel.appendChild(list);
  let result;
  try {
    result = await api(`/admin/api/websearch/credentials/${provider.envKey}/keys`);
  } catch (error) {
    list.textContent = `Could not load keys: ${error.message}`;
    return;
  }
  const healthByIndex = new Map(
    ((result.health && result.health.keys) || []).map((entry) => [
      entry.index,
      entry,
    ]),
  );
  if (result.keys.length === 0) {
    const empty = document.createElement("div");
    empty.className = "ws-key-empty";
    empty.textContent = "No keys configured.";
    list.appendChild(empty);
  }
  const pool = {
    stateKey: `websearch:${provider.envKey}`,
    panel,
    locked: Boolean(result.locked),
    orderUrl: `/admin/api/websearch/credentials/${provider.envKey}/keys/order`,
    nameUrl: (id) =>
      `/admin/api/websearch/credentials/${provider.envKey}/keys/${encodeURIComponent(id)}/name`,
    reload: () => loadKeyManager(provider, panel),
  };
  // `rows` carries the id and the name; `keys` is the pre-7.29.0 shape and
  // is still what the add and delete responses return.
  const wsEntries = Array.isArray(result.rows) ? result.rows : result.keys;
  wsEntries.forEach((entry, index) => {
    const row = keyRailRow("ws-key-row");
    const label = keyRailLabel("ws-key-label");
    const controls = entry.id
      ? keyRowControls(
          pool,
          list,
          row,
          { ...entry, masked: entry.masked || entry.key_label || "(empty)" },
          index,
          wsEntries.length,
        )
      : null;
    paintKeyReference(label, entry.key_label || "(empty)", entry.name || "");
    const health = healthByIndex.get(entry.index);
    const healthEl = document.createElement("span");
    healthEl.className = `status-pill ${keyHealthClass(health)}`;
    healthEl.textContent = keyHealthText(health);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "ghost-button";
    remove.textContent = "Delete";
    remove.disabled = result.locked;
    remove.addEventListener("click", () =>
      deleteWebSearchKey(provider, entry.index, panel, remove, entry.id),
    );
    if (controls) row.append(controls.grip, controls.name);
    row.append(label, healthEl);
    if (controls) row.append(controls.up, controls.down);
    row.append(remove);
    list.appendChild(row);
  });
  list.addEventListener("pointerover", continueKeyDrag);
  const pendingWs =
    state.keyPoolMessage && state.keyPoolMessage.key === pool.stateKey
      ? state.keyPoolMessage
      : null;
  state.keyPoolMessage = null;
  if (pendingWs) {
    announceKeyPool(
      panel,
      pendingWs.sentence,
      pendingWs.undo ? "Undo" : "",
      pendingWs.undo ? undoKeyPoolOrder : null,
    );
  }
  const form = keyRailAddForm({
    legacy: "ws-key-add",
    placeholder: "Paste a new API key",
    locked: result.locked,
    onSubmit: (secret, button, name) =>
      addWebSearchKey(provider, secret, panel, button, name),
  });
  panel.appendChild(form.element);
  if (result.locked) {
    const note = document.createElement("div");
    note.className = "field-description";
    note.textContent = "This credential is locked by an external source; edit it there.";
    panel.appendChild(note);
  }
}

async function addWebSearchKey(provider, input, panel, button, nameInput) {
  const key = input.value.trim();
  if (!key) {
    showMessage("Enter a key first", "warn");
    return;
  }
  const name = nameInput ? nameInput.value.trim().slice(0, 60) : "";
  button.disabled = true;
  try {
    const result = await api(
      `/admin/api/websearch/credentials/${provider.envKey}/keys`,
      { method: "POST", body: JSON.stringify({ key, name }) },
    );
    if (!result.applied) {
      showMessage((result.errors || []).join("; ") || "Key was not applied", "error");
      return;
    }
    showMessage(`Added key to ${provider.envKey}`, "ok");
    await refreshConfigState();
    await loadKeyManager(provider, panel);
  } catch (error) {
    showMessage(`Could not add key: ${error.message}`, "error");
  } finally {
    button.disabled = false;
  }
}

async function deleteWebSearchKey(provider, index, panel, button, id) {
  button.disabled = true;
  const guard = id ? `?id=${encodeURIComponent(id)}` : "";
  try {
    const result = await api(
      `/admin/api/websearch/credentials/${provider.envKey}/keys/${index}${guard}`,
      { method: "DELETE" },
    );
    if (!result.applied) {
      showMessage((result.errors || []).join("; ") || "Key was not applied", "error");
      return;
    }
    showMessage(`Removed key ${index} from ${provider.envKey}`, "ok");
    await refreshConfigState();
    await loadKeyManager(provider, panel);
  } catch (error) {
    showMessage(`Could not delete key: ${error.message}`, "error");
  } finally {
    button.disabled = false;
  }
}

async function testWebSearchProvider(provider, button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Testing";
  try {
    const result = await api(`/admin/api/websearch/providers/${provider.id}/test`, {
      method: "POST",
      body: "{}",
    });
    if (result.ok) {
      const titles = (result.titles || []).filter(Boolean).slice(0, 2).join("; ");
      updateWebSearchCard(
        provider.id,
        "ok",
        `${result.result_count} results`,
        `OK in ${Math.round(result.latency_ms)} ms${titles ? ` — ${titles}` : ""}`,
      );
    } else {
      const error = result.error || {};
      updateWebSearchCard(
        provider.id,
        "error",
        error.kind || "error",
        error.message || "Web search test failed",
      );
    }
  } catch (error) {
    updateWebSearchCard(provider.id, "error", "error", error.message);
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

function asAnalyticsRows(value, keyName) {
  if (Array.isArray(value)) return value;
  if (value && typeof value === "object") {
    return Object.entries(value).map(([name, row]) => ({ [keyName]: name, ...row }));
  }
  return [];
}

function analyticsTable(headers, rows, emptyText) {
  const table = document.createElement("table");
  table.className = "analytics-table";
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  headers.forEach((header) => {
    const th = document.createElement("th");
    th.textContent = header;
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  const tbody = document.createElement("tbody");
  if (rows.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = headers.length;
    td.className = "analytics-empty";
    td.textContent = emptyText;
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
  rows.forEach((cells) => {
    const tr = document.createElement("tr");
    cells.forEach((cell) => {
      const td = document.createElement("td");
      if (cell instanceof Node) {
        td.appendChild(cell);
      } else {
        td.textContent = cell;
      }
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.append(thead, tbody);
  return table;
}

function analyticsBlock(title, table) {
  const block = document.createElement("div");
  block.className = "analytics-block";
  const heading = document.createElement("h4");
  heading.textContent = title;
  const scroll = document.createElement("div");
  scroll.className = "table-scroll";
  scroll.appendChild(table);
  block.append(heading, scroll);
  return block;
}

function formatRequestTime(entry) {
  const iso = entry.ts_iso || entry.ts || "";
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) return iso || "—";
  return new Date(parsed).toLocaleString();
}

function formatAnalyticsNumber(value, maximumFractionDigits = 0) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString(undefined, { maximumFractionDigits });
}

function formatAnalyticsCost(value) {
  if (value == null || Number.isNaN(Number(value))) return "Unknown";
  return `$${Number(value).toFixed(Number(value) < 0.01 ? 4 : 2)}`;
}

function analyticsMetricCards(metrics) {
  const container = document.createElement("div");
  container.className = "requests-cards";
  metrics.forEach(([label, value, detail = ""]) => {
    const card = document.createElement("div");
    card.className = "requests-card";
    const valueElement = document.createElement("strong");
    valueElement.textContent = value;
    const labelElement = document.createElement("span");
    labelElement.textContent = label;
    card.append(valueElement, labelElement);
    if (detail) {
      const detailElement = document.createElement("small");
      detailElement.textContent = detail;
      card.appendChild(detailElement);
    }
    container.appendChild(card);
  });
  return container;
}

function aggregateWebSearchSeries(series) {
  const buckets = new Map();
  (series || []).forEach((entry) => {
    const bucket = entry.bucket || "unknown";
    const aggregate = buckets.get(bucket) || {
      bucket,
      requests: 0,
      errors: 0,
      results: 0,
    };
    aggregate.requests += Number(entry.searches ?? entry.requests ?? 0);
    aggregate.errors += Number(entry.errors || 0);
    aggregate.results += Number(entry.results || 0);
    buckets.set(bucket, aggregate);
  });
  return Array.from(buckets.values()).sort((left, right) =>
    left.bucket.localeCompare(right.bucket),
  );
}

function webSearchSeriesChart(series) {
  const wrapper = document.createElement("section");
  wrapper.className = "requests-chart analytics-panel";
  const heading = document.createElement("div");
  heading.className = "chart-heading";
  const title = document.createElement("h4");
  title.textContent = "Search volume and errors";
  const legend = document.createElement("div");
  legend.className = "chart-legend";
  legend.innerHTML =
    '<span><i class="legend-swatch requests"></i>Logical searches</span>' +
    '<span><i class="legend-swatch errors"></i>Errors</span>';
  heading.append(title, legend);
  const canvas = document.createElement("canvas");
  canvas.id = "wsSeriesChart";
  canvas.width = 960;
  canvas.height = 220;
  canvas.setAttribute("role", "img");
  canvas.setAttribute("aria-label", "Logical web searches and errors over time");
  wrapper.append(heading, canvas);
  const aggregate = aggregateWebSearchSeries(series);
  requestAnimationFrame(() => {
    const drawWs = () => drawBarChart(
      canvas,
      aggregate.map((entry) => entry.bucket),
      [
        { values: aggregate.map((entry) => entry.requests) },
        { values: aggregate.map((entry) => entry.errors) },
      ],
    );
    drawWs();
    registerChartRedraw("wsSeriesChart", drawWs);
  });
  return wrapper;
}

function renderWebSearchAnalytics(
  container,
  stats,
  requests,
  period,
  partialErrors = [],
  stale = {},
) {
  container.innerHTML = "";
  const routeTotals = stats?.routes?.totals || stats?.route_totals || null;
  const attemptStats = stats?.attempts || stats || {};
  const totals = routeTotals || attemptStats.totals || {};
  const totalRequests = Number(totals.searches ?? totals.requests ?? 0);
  const totalErrors = Number(totals.errors || 0);
  const successRate =
    totalRequests > 0 ? ((totalRequests - totalErrors) / totalRequests) * 100 : 0;
  const resultsPerSearch =
    totalRequests > 0 ? Number(totals.results || 0) / totalRequests : 0;

  if (partialErrors.length) {
    const warning = document.createElement("div");
    warning.className = "analytics-warning";
    const staleParts = [];
    if (stale.stats) staleParts.push("summary");
    if (stale.requests) staleParts.push("recent requests");
    warning.textContent =
      `Some analytics could not be loaded: ${partialErrors.join("; ")}` +
      (staleParts.length
        ? `. Showing the last successful ${staleParts.join(" and ")} data.`
        : ".");
    container.appendChild(warning);
  }
  if (
    stats &&
    routeTotals &&
    totalRequests === 0 &&
    Number(attemptStats.totals?.requests || 0) > 0
  ) {
    const migrationNote = document.createElement("div");
    migrationNote.className = "analytics-warning";
    migrationNote.textContent =
      "Logical-route telemetry starts with My Claude Code 4.12.0. Historical provider-attempt rows remain available below.";
    container.appendChild(migrationNote);
  }

  const metricValue = (value, formatter = formatAnalyticsNumber) =>
    stats ? formatter(value) : "Unavailable";
  container.appendChild(
    analyticsMetricCards([
      [
        "Logical searches",
        metricValue(totals.searches ?? totals.requests ?? 0),
      ],
      ["Route success rate", stats ? `${successRate.toFixed(1)}%` : "Unavailable"],
      [
        "Fallback rate",
        stats
          ? `${(Number(totals.fallback_rate || 0) * 100).toFixed(1)}%`
          : "Unavailable",
      ],
      [
        "Average attempts",
        stats ? formatAnalyticsNumber(totals.avg_attempts, 2) : "Unavailable",
      ],
      ["Failed searches", metricValue(totals.errors ?? 0)],
      [
        "End-to-end latency",
        !stats
          ? "Unavailable"
          : totals.avg_duration_ms == null
          ? "—"
          : `${formatAnalyticsNumber(totals.avg_duration_ms)} ms`,
      ],
      ["Results", metricValue(totals.results ?? 0)],
      [
        "Results / search",
        stats ? formatAnalyticsNumber(resultsPerSearch, 2) : "Unavailable",
      ],
      [
        "Known spend",
        stats ? formatAnalyticsCost(totals.cost_usd) : "Unavailable",
        "Best-effort provider-reported cost; unavailable costs are excluded",
      ],
      [
        "Dropped records",
        metricValue(stats?.dropped_records ?? 0),
        "Writer queue overflow",
      ],
    ]),
  );

  const routeSeries = stats?.routes?.series || stats?.route_series || stats?.series;
  if (stats && Array.isArray(routeSeries)) {
    container.appendChild(webSearchSeriesChart(routeSeries));
  }

  const terminalRows = asAnalyticsRows(
    stats?.routes?.by_terminal_provider,
    "provider",
  ).map((row) => {
    const searches = Number(row.searches ?? row.requests ?? 0);
    const errors = Number(row.errors || 0);
    return [
      row.provider || row.terminal_provider || "—",
      formatAnalyticsNumber(searches),
      searches ? `${(((searches - errors) / searches) * 100).toFixed(1)}%` : "0%",
      formatAnalyticsNumber(row.fallbacks ?? 0),
      row.avg_duration_ms != null
        ? `${formatAnalyticsNumber(row.avg_duration_ms)} ms`
        : "—",
      formatAnalyticsNumber(row.results ?? 0),
      formatAnalyticsCost(row.cost_usd),
    ];
  });
  container.appendChild(
    analyticsBlock(
      "Terminal route outcomes",
      analyticsTable(
        [
          "Terminal provider",
          "Searches",
          "Success rate",
          "Fallbacks",
          "End-to-end latency",
          "Results",
          "Cost",
        ],
        terminalRows,
        stats ? "No completed search routes yet." : "Route metrics unavailable.",
      ),
    ),
  );

  const providerRows = asAnalyticsRows(
    attemptStats.by_provider,
    "provider",
  ).map(
    (row) => {
      const requestsCount = Number(row.requests || 0);
      const errorsCount = Number(row.errors || 0);
      return [
        row.provider || "—",
        formatAnalyticsNumber(requestsCount),
        requestsCount ? `${((errorsCount / requestsCount) * 100).toFixed(1)}%` : "0%",
        row.avg_duration_ms != null
          ? `${formatAnalyticsNumber(row.avg_duration_ms)} ms`
          : "—",
        formatAnalyticsNumber(row.results ?? 0),
        formatAnalyticsCost(row.cost_usd),
      ];
    },
  );
  container.appendChild(
    analyticsBlock(
      "Provider attempt performance",
      analyticsTable(
        ["Provider", "Attempts", "Error rate", "Avg latency", "Results", "Cost"],
        providerRows,
        stats ? "No provider attempts recorded yet." : "Provider metrics unavailable.",
      ),
    ),
  );

  const keyRows = asAnalyticsRows(attemptStats.by_key, "key_label").map((row) => [
    row.provider || "—",
    row.key_label || row.key || "—",
    // Not coerced to zero: an unmeasured column is a dash, so a credential
    // whose counters were never recorded cannot read as one that did nothing.
    formatOptionalNumber(row.requests),
    formatOptionalNumber(row.errors),
    row.avg_duration_ms != null
      ? `${formatAnalyticsNumber(row.avg_duration_ms)} ms`
      : NOT_MEASURED,
    formatOptionalNumber(row.results),
  ]);
  container.appendChild(
    analyticsBlock(
      "Credential health",
      analyticsTable(
        ["Provider", "Key", "Requests", "Errors", "Avg latency", "Results"],
        keyRows,
        stats ? "No key usage recorded yet." : "Credential metrics unavailable.",
      ),
    ),
  );

  const routeErrorRows = asAnalyticsRows(
    stats?.routes?.top_errors,
    "error_kind",
  ).map((row) => [
    row.error_kind || "unknown",
    row.error_message || "No message",
    formatAnalyticsNumber(row.count ?? 0),
  ]);
  container.appendChild(
    analyticsBlock(
      "Top terminal route errors",
      analyticsTable(
        ["Kind", "Message", "Count"],
        routeErrorRows,
        stats ? "No terminal route errors in this range." : "Error metrics unavailable.",
      ),
    ),
  );

  const errorRows = asAnalyticsRows(attemptStats.top_errors, "error_kind").map(
    (row) => [
      row.error_kind || "unknown",
      row.error_message || "No message",
      formatAnalyticsNumber(row.count ?? 0),
    ],
  );
  container.appendChild(
    analyticsBlock(
      "Top provider-attempt errors",
      analyticsTable(
        ["Kind", "Message", "Count"],
        errorRows,
        stats
          ? "No provider-attempt errors in this range."
          : "Error metrics unavailable.",
      ),
    ),
  );

  const requestItems = requests
    ? requests.requests || requests.items || (Array.isArray(requests) ? requests : [])
    : [];
  const requestRows = requestItems.map((entry) => [
    formatRequestTime(entry),
    entry.route_id ? String(entry.route_id).slice(0, 8) : "—",
    entry.attempt_number ?? "—",
    entry.provider || "—",
    entry.key_label || "—",
    entry.query || "—",
    entry.results_count ?? 0,
    entry.duration_ms != null ? `${Math.round(entry.duration_ms)} ms` : "—",
    entry.status || "—",
    entry.error_kind || "—",
    formatAnalyticsCost(entry.cost_usd),
    webSearchDetailButton(entry),
  ]);
  container.appendChild(
    analyticsBlock(
      "Recent requests",
      analyticsTable(
        [
          "Time",
          "Route",
          "Attempt",
          "Provider",
          "Key",
          "Query",
          "Results",
          "Latency",
          "Status",
          "Error",
          "Cost",
          "Details",
        ],
        requestRows,
        requests ? "No recent provider attempts." : "Recent attempts unavailable.",
      ),
    ),
  );

  const periodLabel = {
    hourly: "hour",
    daily: "day",
    weekly: "ISO week",
    monthly: "month",
  }[period];
  const footer = document.createElement("p");
  footer.className = "analytics-footnote";
  footer.textContent =
    `Series bucket: ${periodLabel || period}; bucket boundaries use UTC. ` +
    "Route metrics count one user search; provider tables and recent rows count attempts. " +
    "Queries are stored locally and truncated to 256 characters. " +
    (stats?.capture_content
      ? "Full normalized provider input/output is captured; a configurable cap "
        + `(${formatAnalyticsNumber(stats.max_content_chars)} characters per `
        + "payload) guards against pathological sizes."
      : "Search I/O capture is disabled; only lengths and SHA-256 hashes are retained.");
  container.appendChild(footer);
}

function webSearchDetailButton(entry) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "secondary-button req-detail-button";
  button.textContent = "View";
  button.setAttribute(
    "aria-label",
    `View web search attempt ${entry.id || entry.attempt_number || ""}`.trim(),
  );
  button.addEventListener("click", () =>
    openWebSearchDetail(entry.id).catch((error) =>
      showMessage(`Could not load web search detail: ${error.message}`, "error"),
    ),
  );
  return button;
}

function prettyJson(value) {
  return value == null ? "" : JSON.stringify(value, null, 2);
}

function capturedPayloadText(row, field) {
  const payload = row[field];
  if (payload != null) return prettyJson(payload);
  const chars = row[`${field}_chars`];
  const hash = row[`${field}_sha256`];
  if (chars == null && !hash) return "(not available for this historical record)";
  return [
    "(content not captured)",
    chars != null ? `Characters: ${chars}` : "",
    hash ? `SHA-256: ${hash}` : "",
  ]
    .filter(Boolean)
    .join("\n");
}

function appendWebSearchDetailMeta(meta, fields) {
  meta.innerHTML = "";
  fields.forEach(([label, value]) => {
    if (value == null || value === "") return;
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    meta.append(dt, dd);
  });
}

function renderWebSearchInputSummary(input) {
  const container = byId("webSearchDetailInput");
  container.innerHTML = "";
  if (!input) {
    const note = document.createElement("dd");
    note.className = "ws-input-empty";
    note.textContent = "No tool input was captured for this attempt.";
    container.appendChild(note);
    return;
  }
  const fields = [];
  if (input.query != null) fields.push(["Query", String(input.query)]);
  if (input.max_results != null) fields.push(["Max results", String(input.max_results)]);
  if (input.allowed_domains?.length) {
    fields.push(["Allowed domains", String(input.allowed_domains.join(", "))]);
  }
  if (input.blocked_domains?.length) {
    fields.push(["Blocked domains", String(input.blocked_domains.join(", "))]);
  }
  // Any provider-specific input fields beyond the common shape get a raw line
  // so nothing is silently hidden behind the summary.
  Object.entries(input).forEach(([key, value]) => {
    if (["query", "max_results", "allowed_domains", "blocked_domains"].includes(key)) {
      return;
    }
    if (value == null || value === "") return;
    fields.push([key, typeof value === "string" ? value : JSON.stringify(value)]);
  });
  if (fields.length === 0) {
    const note = document.createElement("dd");
    note.className = "ws-input-empty";
    note.textContent = "No readable fields in the tool input.";
    container.appendChild(note);
    return;
  }
  fields.forEach(([label, value]) => {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    container.append(dt, dd);
  });
}

function renderWebSearchRawOutput(row) {
  // The raw JSON pane exists to inspect provider-specific fields the readable
  // summary does not draw, and to expose the preview + hash for legacy
  // truncated rows. It is hidden only when there is genuinely nothing to show.
  const pane = byId("webSearchDetailRawPane");
  const pre = byId("webSearchDetailOutput");
  const text = capturedPayloadText(row, "output");
  const truncated = Boolean(row.output && row.output._truncated);
  const hasContent =
    (row.output && !truncated) ||
    truncated ||
    (!row.output && (row.output_chars != null || row.output_sha256));
  pane.hidden = !hasContent;
  pane.querySelector(".guide-copy-button")?.remove();
  if (hasContent) addCopyButton(pane, () => text, { inSummary: true });
  pre.textContent = text;
  // Keep the pane collapsed by default for full output (the readable summary
  // is the surface), but leave legacy-truncated rows open so the notice plus
  // preview read together.
  pane.open = truncated;
}

function addCopyButton(host, getText, { inSummary = false } = {}) {
  if (!navigator.clipboard || !navigator.clipboard.writeText) return;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "guide-copy-button";
  button.textContent = "Copy";
  button.setAttribute("aria-label", "Copy to clipboard");
  button.addEventListener("click", () => {
    navigator.clipboard
      .writeText(getText())
      .then(() => {
        button.textContent = "Copied";
        button.classList.add("is-copied");
        window.setTimeout(() => {
          button.textContent = "Copy";
          button.classList.remove("is-copied");
        }, 1500);
      })
      .catch(() => {
        // Clipboard writes can fail on permissions or policy; the text stays
        // selectable by hand.
      });
  });
  if (inSummary) {
    const summary = host.querySelector("summary");
    if (summary) {
      summary.appendChild(button);
      return;
    }
  }
  host.appendChild(button);
}

function renderWebSearchResponseSummary(output) {
  const container = byId("webSearchDetailSummary");
  container.innerHTML = "";
  if (!output) {
    container.textContent = "No captured provider response is available.";
    return;
  }
  if (output._truncated) {
    // Legacy rows stored before the cap was raised. The stored preview is the
    // first chunk of the serialized output; parse out the results it contains
    // and render them as readable cards instead of dumping raw JSON.
    const notice = document.createElement("div");
    notice.className = "analytics-warning";
    notice.textContent =
      `This attempt predates the larger capture cap; only the first ` +
      `characters of its output were stored (${Number(
        output.original_chars ?? 0,
      ).toLocaleString()} characters originally).`;
    container.appendChild(notice);
    const preview = typeof output.preview === "string" ? output.preview : "";
    const parsed = previewResultsFromJson(preview);
    if (parsed.results.length > 0) {
      if (parsed.answer) {
        const answer = document.createElement("div");
        answer.className = "websearch-result-answer";
        const title = document.createElement("strong");
        title.textContent = "Provider answer / rich summary";
        const text = document.createElement("p");
        text.textContent = parsed.answer;
        answer.append(title, text);
        container.appendChild(answer);
      }
      renderWebSearchResultCards(container, parsed.results, { fromPreview: true });
      const note = document.createElement("p");
      note.className = "analytics-footnote ws-preview-note";
      note.textContent =
        `The preview contains the first ${parsed.results.length} of ` +
        `${Number(output.original_chars ?? 0).toLocaleString()} characters; ` +
        `expand “Raw output JSON” for the exact stored preview and hash.`;
      container.appendChild(note);
    } else if (preview) {
      // Unparseable (cut mid-string): show the raw preview as text.
      const pre = document.createElement("pre");
      pre.className = "requests-detail-body ws-preview-body";
      pre.textContent = preview;
      container.appendChild(pre);
    }
    return;
  }
  if (output.error) {
    const error = document.createElement("div");
    error.className = "analytics-warning";
    error.textContent = `${output.error.kind || "error"}: ${
      output.error.message || output.error.type || "Provider attempt failed"
    }`;
    container.appendChild(error);
    return;
  }
  if (output.answer) {
    const answer = document.createElement("div");
    answer.className = "websearch-result-answer";
    const title = document.createElement("strong");
    title.textContent = "Provider answer / rich summary";
    const text = document.createElement("p");
    text.textContent = output.answer;
    answer.append(title, text);
    container.appendChild(answer);
  }
  const results = Array.isArray(output.results) ? output.results : [];
  renderWebSearchResultCards(container, results);
  if (!output.answer && results.length === 0) {
    container.textContent = "The provider returned no results or answer.";
  }
}

/**
 * Render search results as readable cards: title, url link, published date,
 * snippet (the search result description), and — when present and different —
 * the extracted page text behind a "Show full content" toggle.
 */
function renderWebSearchResultCards(container, results, { fromPreview = false } = {}) {
  results.forEach((result, index) => {
    const item = document.createElement("article");
    item.className = "websearch-result-item";
    item.dataset.fromPreview = fromPreview ? "true" : undefined;
    const title = document.createElement("strong");
    title.textContent = `${index + 1}. ${result.title || "Untitled result"}`;
    item.appendChild(title);
    if (result.url && /^https?:\/\//i.test(result.url)) {
      const link = document.createElement("a");
      link.href = result.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = result.url;
      item.appendChild(link);
    } else if (result.url) {
      const url = document.createElement("small");
      url.textContent = result.url;
      item.appendChild(url);
    }
    if (result.published) {
      const published = document.createElement("small");
      published.textContent = `Published: ${result.published}`;
      item.appendChild(published);
    }
    // The snippet is the provider's description of the result — the most
    // useful line for a human. The extracted content is often far longer.
    const description = result.snippet || result.description || result.text || "";
    if (description) {
      const snippet = document.createElement("p");
      snippet.textContent = description;
      item.appendChild(snippet);
    }
    const content = result.content && result.content !== description ? result.content : "";
    if (content) {
      const contentP = document.createElement("p");
      contentP.className = "ws-result-content";
      const truncated = content.length > 600;
      contentP.textContent = truncated ? content.slice(0, 600) + "…" : content;
      item.appendChild(contentP);
      if (truncated) {
        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "ghost-button ws-content-toggle";
        toggle.textContent = "Show full content";
        toggle.addEventListener("click", () => {
          const expanded = contentP.classList.toggle("ws-content-expanded");
          contentP.textContent = expanded
            ? content
            : content.slice(0, 600) + "…";
          toggle.textContent = expanded ? "Collapse content" : "Show full content";
        });
        item.appendChild(toggle);
      }
    }
    container.appendChild(item);
  });
}

/**
 * Best-effort parse of a truncated serialized-JSON preview (the first N chars
 * of an output payload). Returns { answer, results } for whatever complete
 * fields survived the cut. Falls back to an empty result set when the preview
 * is not parseable (it can be cut mid-string).
 */
function previewResultsFromJson(preview) {
  if (!preview) return { answer: "", results: [] };
  try {
    const parsed = JSON.parse(preview);
    return {
      answer: parsed && typeof parsed.answer === "string" ? parsed.answer : "",
      results: Array.isArray(parsed && parsed.results) ? parsed.results : [],
    };
  } catch (_) {
    // The preview can be truncated inside a string value, so JSON.parse fails.
    // Extract the leading "answer" (it precedes "results" in the payload), then
    // walk the "results": [ array. Each result's small leading fields (title,
    // url, snippet/description, published) come before its huge content, so a
    // result that is cut mid-content still contributes a readable card.
    let answer = "";
    const answerMatch = preview.match(/"answer"\s*:\s*"((?:[^"\\]|\\.)*)"/);
    if (answerMatch) answer = answerMatch[1];
    const match = preview.match(/"results"\s*:\s*\[/);
    if (!match) return { answer, results: [] };
    const start = match.index + match[0].length;
    const results = [];
    let i = start;
    let guard = 0;
    while (i < preview.length && guard < 20) {
      guard += 1;
      while (i < preview.length && /\s|,/.test(preview[i])) i += 1;
      if (i >= preview.length || preview[i] !== "{") break;
      const objStart = i;
      let depth = 0;
      let j = i;
      let inString = false;
      let stringChar = "";
      for (; j < preview.length; j += 1) {
        const ch = preview[j];
        if (inString) {
          if (ch === "\\") { j += 1; continue; }
          if (ch === stringChar) inString = false;
          continue;
        }
        if (ch === '"') { inString = true; stringChar = ch; continue; }
        if (ch === "{") depth += 1;
        else if (ch === "}") {
          depth -= 1;
          if (depth === 0) break;
        }
      }
      let obj;
      if (depth === 0) {
        try {
          obj = JSON.parse(preview.slice(objStart, j + 1));
        } catch (_) {
          obj = null;
        }
        i = j + 1;
      } else {
        // Object is cut mid-way (usually inside the huge content string). Pull
        // the small leading fields out of the partial text.
        const partial = preview.slice(objStart);
        obj = {};
        const field = (name) => {
          const re = new RegExp(`"${name}"\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)"`);
          const m = partial.match(re);
          return m ? m[1] : "";
        };
        const title = field("title");
        const url = field("url");
        const snippet = field("snippet") || field("description") || field("text");
        const published = field("published");
        if (title || url || snippet) {
          obj = { title, url, snippet, published };
          results.push(obj);
        }
        break; // the cut object is the last one
      }
      if (obj && (obj.title || obj.url || obj.snippet || obj.content)) {
        results.push(obj);
      } else if (obj) {
        break;
      }
    }
    return { answer, results };
  }
}

async function openWebSearchDetail(requestId) {
  state.webSearchDetailReturnFocus = document.activeElement;
  const row = await api(`/admin/api/websearch/requests/${requestId}`);
  byId("webSearchDetailTitle").textContent =
    `Web search ${String(row.route_id || "route").slice(0, 8)} · attempt ${
      row.attempt_number
    }`;
  appendWebSearchDetailMeta(byId("webSearchDetailMeta"), [
    ["Time", formatRequestTime(row)],
    ["Route ID", row.route_id],
    ["Attempt", row.attempt_number],
    ["Provider", row.provider],
    // A dash, not "keyless": the field was not recorded, which is not the
    // same claim as the provider having needed no key.
    ["Credential", row.key_label || NOT_MEASURED],
    ["Status", row.status],
    ["Results", row.results_count],
    ["Latency", row.duration_ms != null ? `${Math.round(row.duration_ms)} ms` : "—"],
    ["Cost", formatAnalyticsCost(row.cost_usd)],
    ["Error", row.error_kind ? `${row.error_kind}: ${row.error_message || ""}` : ""],
    ["Input characters", row.input_chars],
    ["Output characters", row.output_chars],
    ["Input SHA-256", row.input_sha256],
    ["Output SHA-256", row.output_sha256],
  ]);
  const configPre = byId("webSearchDetailConfig");
  const configText = prettyJson(row.provider_config) || "(configuration unavailable)";
  configPre.textContent = configText;
  const configPane = byId("webSearchDetailConfigPane");
  configPane.hidden = !row.provider_config;
  configPane.querySelector(".guide-copy-button")?.remove();
  if (row.provider_config) addCopyButton(configPane, () => configText, { inSummary: true });
  renderWebSearchInputSummary(row.input);
  renderWebSearchRawOutput(row);
  renderWebSearchResponseSummary(row.output);
  byId("webSearchDetailModal").hidden = false;
  byId("webSearchDetailClose").focus();
}

function closeWebSearchDetail() {
  byId("webSearchDetailModal").hidden = true;
  if (state.webSearchDetailReturnFocus instanceof HTMLElement) {
    state.webSearchDetailReturnFocus.focus();
  }
  state.webSearchDetailReturnFocus = null;
}

function trapWebSearchDetailFocus(event) {
  const modal = byId("webSearchDetailModal");
  if (event.key !== "Tab" || modal.hidden) return;
  const focusable = Array.from(
    modal.querySelectorAll(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  ).filter((element) => element instanceof HTMLElement && !element.hidden);
  if (focusable.length === 0) {
    event.preventDefault();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function webSearchAnalyticsParams({ includePeriod = false, limit = null } = {}) {
  const params = new URLSearchParams();
  const provider = byId("webSearchFilterProvider")?.value || "";
  const status = byId("webSearchFilterStatus")?.value || "";
  const query = byId("webSearchFilterQuery")?.value.trim() || "";
  const windowSeconds = byId("webSearchFilterWindow")?.value || "";
  if (includePeriod) {
    params.set("period", state.webSearchStatsPeriod);
  }
  if (provider) params.set("provider", provider);
  if (status) params.set("status", status);
  if (query) params.set("q", query);
  if (windowSeconds) {
    params.set(
      "since",
      new Date(Date.now() - Number(windowSeconds) * 1000).toISOString(),
    );
  }
  if (limit != null) params.set("limit", String(limit));
  return params;
}

async function loadWebSearchAnalytics() {
  const loadId = ++state.webSearchAnalyticsLoadId;
  const period = byId("webSearchStatsPeriod")?.value || state.webSearchStatsPeriod;
  state.webSearchStatsPeriod = period;
  const container = byId("webSearchAnalytics");
  container.textContent = "Loading analytics…";
  const statsParams = webSearchAnalyticsParams({ includePeriod: true });
  const statsKey = statsParams.toString();
  const requestParams = webSearchAnalyticsParams({ limit: 50 });
  const requestKey = requestParams.toString();
  const [statsResult, requestsResult] = await Promise.allSettled([
    api(`/admin/api/websearch/stats?${statsParams}`),
    api(`/admin/api/websearch/requests?${requestParams}`),
  ]);
  if (loadId !== state.webSearchAnalyticsLoadId) return;

  let stats = null;
  let requests = null;
  const partialErrors = [];
  const stale = { stats: false, requests: false };
  if (statsResult.status === "fulfilled") {
    stats = statsResult.value;
    state.webSearchAnalyticsStats = stats;
    state.webSearchAnalyticsStatsKey = statsKey;
    state.webSearchLastRoute =
      stats?.last_route || stats?.routes?.last_route || null;
    renderWebSearchObservedRoute(state.webSearchLastRoute);
  } else {
    partialErrors.push(
      `summary: ${statsResult.reason?.message || String(statsResult.reason)}`,
    );
    stats =
      state.webSearchAnalyticsStatsKey === statsKey
        ? state.webSearchAnalyticsStats
        : null;
    stale.stats = Boolean(stats);
    if (!stats) {
      state.webSearchLastRoute = null;
      renderWebSearchObservedRoute(null);
    }
  }
  if (requestsResult.status === "fulfilled") {
    requests = requestsResult.value;
    state.webSearchAnalyticsPage = requests;
    state.webSearchAnalyticsPageKey = requestKey;
  } else {
    partialErrors.push(
      `requests: ${requestsResult.reason?.message || String(requestsResult.reason)}`,
    );
    requests =
      state.webSearchAnalyticsPageKey === requestKey
        ? state.webSearchAnalyticsPage
        : null;
    stale.requests = Boolean(requests);
  }
  renderWebSearchAnalytics(container, stats, requests, period, partialErrors, stale);
  byId("webSearchLastUpdated").textContent =
    `${partialErrors.length ? "Refresh incomplete" : "Updated"} ${new Date().toLocaleTimeString()}`;
}

function showMessage(message, kind = "") {
  const area = byId("messageArea");
  area.textContent = message;
  area.className = `message-area ${kind}`.trim();
}

/* --------------------------------------------------------------------- */
/* Custom providers                                                        */
/* --------------------------------------------------------------------- */

const CUSTOM_PROVIDER_STATUS_LABELS = {
  configured: "Configured",
  missing_key: "Missing key",
  disabled: "Disabled",
};

async function loadCustomProviders() {
  const grid = byId("customProviderGrid");
  if (!grid) return;
  let result;
  try {
    result = await api("/admin/api/custom-providers");
  } catch (error) {
    grid.innerHTML = "";
    const note = document.createElement("div");
    note.className = "cp-note";
    note.textContent = `Custom providers unavailable: ${error.message}`;
    grid.appendChild(note);
    return;
  }
  state.customProviders = result.providers || [];
  renderCustomProviders();
}

function renderCustomProviders() {
  const grid = byId("customProviderGrid");
  grid.innerHTML = "";
  if (state.customProviders.length === 0) {
    const empty = document.createElement("div");
    empty.className = "cp-note";
    empty.textContent = "No custom providers yet.";
    grid.appendChild(empty);
    return;
  }
  state.customProviders.forEach((provider) => {
    grid.appendChild(customProviderCard(provider));
  });
}

function customProviderDetailsText(provider) {
  return (
    `${provider.key_count} key${provider.key_count === 1 ? "" : "s"} · ` +
    `${provider.credential_rotation}` +
    // Before the model count rather than after it, which is not cosmetic: the
    // count is the clause that moves when a refresh lands, and a reader --
    // like the test that watches it -- follows it at the end of the line.
    customProviderSurfacesText(provider) +
    ` · ${provider.model_count} models` +
    (provider.proxy ? ` · proxy ${provider.proxy}` : "")
  );
}

/* What this host was told to serve, on the card, and nothing at all for the
   default. An entry that speaks Chat Completions alone is every custom
   provider that existed before 7.33.0, so saying so on its card would be a new
   sentence about an unchanged thing; a host that serves two doors is a fact
   worth reading without opening the form. */
function customProviderSurfacesText(provider) {
  const surfaces = Array.isArray(provider.surfaces) ? provider.surfaces : [];
  if (surfaces.length < 2) return "";
  return ` · serves ${surfaces.join(", ")}`;
}

/* The checkbox group, built from the vocabulary the server sent rather than
   from a list in the page: adding a surface must be one change, in the
   registry, not two. */
function renderCustomProviderSurfaces(provider) {
  const host = byId("cpSurfaces");
  if (!host) return;
  host.textContent = "";
  const available =
    (provider && Array.isArray(provider.available_surfaces)
      ? provider.available_surfaces
      : null) || CUSTOM_PROVIDER_SURFACE_FALLBACK;
  const declared =
    provider && Array.isArray(provider.surfaces)
      ? provider.surfaces
      : ["chat_completions"];
  available.forEach((surface) => {
    const row = document.createElement("label");
    row.className = "cp-surface";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = surface.value;
    box.checked = declared.indexOf(surface.value) !== -1;
    box.dataset.cpSurface = surface.value;
    const text = document.createElement("span");
    text.textContent = surface.label;
    row.append(box, text);
    host.appendChild(row);
  });
}

/* Only reached by a page that somehow has no provider to read the vocabulary
   off -- the Add form before any provider exists. The server sends the same
   three with every entry. */
const CUSTOM_PROVIDER_SURFACE_FALLBACK = [
  { value: "chat_completions", label: "Chat Completions (/chat/completions)" },
  { value: "responses", label: "Responses (/responses)" },
  { value: "messages", label: "Messages (/messages)" },
];

/* What the form submits: every ticked box, or Chat Completions when the
   operator unticked all three. A host that serves nothing is not a
   configuration anybody means, and the registry refuses it -- answering that
   with a 422 the operator has to read would be a worse way to say the same
   thing. */
function customProviderSurfacesValue() {
  const host = byId("cpSurfaces");
  if (!host) return ["chat_completions"];
  const ticked = Array.from(host.querySelectorAll("input[type=checkbox]"))
    .filter((box) => box.checked)
    .map((box) => box.value);
  return ticked.length ? ticked : ["chat_completions"];
}

function customProviderCard(provider) {
  const card = document.createElement("article");
  card.className = "provider-card";
  card.dataset.customProvider = provider.provider_id;

  const title = document.createElement("div");
  title.className = "provider-title";
  // display_name is free-text the user typed, so it renders as a text node --
  // same contract as renderProviderCard().
  const name = document.createElement("strong");
  name.textContent = provider.display_name || provider.provider_id;
  title.appendChild(name);
  const pill = document.createElement("span");
  pill.className = `status-pill ${statusClass(provider.status)}`;
  pill.textContent =
    CUSTOM_PROVIDER_STATUS_LABELS[provider.status] || provider.status;
  title.appendChild(pill);

  const meta = document.createElement("div");
  meta.className = "provider-meta";
  meta.textContent = provider.base_url;

  const details = document.createElement("div");
  details.className = "cp-details";
  details.textContent = customProviderDetailsText(provider);

  const keyList = keyRailList("cp-key-list");
  provider.masked_keys.forEach((masked, index) => {
    const row = keyRailRow("cp-key-row");
    const label = keyRailLabel("cp-key-label");
    label.textContent = masked;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "ghost-button cp-key-remove";
    remove.textContent = "Remove";
    // The id arrives with the key listing a moment later; until it does the
    // remove behaves exactly as it always has.
    remove.addEventListener("click", () =>
      removeCustomProviderKey(provider, index, remove, row.dataset.keyId),
    );
    row.append(label, remove);
    keyList.appendChild(row);
  });

  const addForm = keyRailAddForm({
    legacy: "cp-key-add",
    placeholder: "Paste a key, or several separated by commas",
    onSubmit: (secret, button, name) =>
      addCustomProviderKey(provider, secret, button, name),
  });
  keyList.appendChild(addForm.element);
  // A custom card renders its rail inline and never behind a Configure
  // toggle, so it declares itself a rail host the moment it is built.
  card.classList.add("has-key-rail");

  const actions = document.createElement("div");
  actions.className = "card-actions";

  // Same endpoint, same label as a static remote provider card. It used to
  // read "Test", so the one affordance that re-runs discovery was invisible to
  // anyone hunting for a refresh. A custom provider is always remote, so
  // "Test connection" -- which stays on local static cards -- never applies.
  const testButton = document.createElement("button");
  testButton.type = "button";
  testButton.className = "test-button";
  testButton.textContent = "Refresh models";
  testButton.addEventListener("click", () =>
    refreshCustomProviderModels(provider, testButton),
  );

  const editButton = document.createElement("button");
  editButton.type = "button";
  // ``cp-edit`` beside the shared class so the gesture is addressable: every
  // other control on this card already is, and a test that has to find "Edit"
  // by its label is a test that breaks when the label is translated.
  editButton.className = "secondary-button cp-edit";
  editButton.textContent = "Edit";
  editButton.addEventListener("click", () => openCustomProviderForm(provider));

  const deleteButton = document.createElement("button");
  deleteButton.type = "button";
  deleteButton.className = "secondary-button danger";
  deleteButton.textContent = "Delete";
  deleteButton.addEventListener("click", () =>
    deleteCustomProvider(provider, deleteButton),
  );

  // Enable/disable lives on the card, not only in the API. Disabling a
  // provider a MODEL* route names used to take the whole Settings object
  // down; it now pauses those chain entries, and the gesture that does it has
  // to be reachable for anyone to find out.
  const toggleButton = document.createElement("button");
  toggleButton.type = "button";
  toggleButton.className = "secondary-button cp-toggle";
  toggleButton.textContent = provider.enabled ? "Disable" : "Enable";
  toggleButton.addEventListener("click", () =>
    setCustomProviderEnabled(provider, !provider.enabled, toggleButton),
  );

  actions.append(testButton, toggleButton, editButton, deleteButton);
  card.append(title, meta, details, customProviderDialect(provider), keyList, actions);
  loadCustomProviderKeyHealth(provider.provider_id);
  return card;
}

/**
 * What this host was measured spelling `reasoning_effort` with.
 *
 * A static provider declares its effort vocabulary in a profile. A custom one
 * cannot, so until 6.25.0 every custom provider was assumed to speak the four
 * standard OpenAI words -- and a host documenting `{low, high, max}` had every
 * request for `max` clamped to `high`, because MCC had no way to spell the
 * word the host itself had named in a 400. This line says what was learned,
 * and the field beside it lets the answer be corrected by hand.
 */
function customProviderDialect(provider) {
  const wrap = document.createElement("div");
  wrap.className = "cp-dialect";

  const label = document.createElement("span");
  label.className = "cp-dialect-label";
  label.textContent = `reasoning dialect: ${provider.reasoning_dialect_label || "unknown"}`;
  label.title =
    "The reasoning_effort values this host accepted when asked with an " +
    "invalid one. Learned from the host's own 400; never a guess.";
  wrap.appendChild(label);

  const input = document.createElement("input");
  input.type = "text";
  input.className = "cp-dialect-edit";
  input.placeholder = "low, high, max";
  input.value = (provider.reasoning_effort_enum || []).join(", ");
  input.setAttribute("aria-label", `Effort vocabulary for ${provider.provider_id}`);

  const save = document.createElement("button");
  save.type = "button";
  save.className = "ghost-button cp-dialect-save";
  save.textContent = "Save";
  save.addEventListener("click", () =>
    saveCustomProviderDialect(provider, input.value, save),
  );

  const probe = document.createElement("button");
  probe.type = "button";
  probe.className = "ghost-button cp-dialect-probe";
  probe.textContent = "Probe reasoning dialect";
  probe.addEventListener("click", () =>
    probeCustomProviderDialect(provider, probe),
  );

  const capabilities = document.createElement("button");
  capabilities.type = "button";
  capabilities.className = "ghost-button cp-capability-probe";
  capabilities.textContent = "Probe capabilities";
  capabilities.addEventListener("click", () =>
    probeProviderCapabilities(provider, capabilities),
  );

  const forget = document.createElement("button");
  forget.type = "button";
  forget.className = "ghost-button cp-forget-learned";
  forget.textContent = "Forget everything learned";
  forget.addEventListener("click", () => {
    if (
      !window.confirm(
        `Forget every fact MCC has learned about ${provider.display_name || provider.provider_id}? ` +
          "Caps, refusals and probe results all go, and each is re-learned " +
          "the next time this host says it again.",
      )
    ) {
      return;
    }
    forgetLearnedFacts({ providerId: provider.provider_id }, forget);
  });

  wrap.append(input, save, probe, capabilities, forget);
  return wrap;
}

/* Bounded, stated, and never on the request path: the confirm text names the
   number of upstream requests before any are sent, because a gateway listing
   400 models behind one button is 400-1200 of them. */
async function probeProviderCapabilities(provider, button) {
  const models = Math.min(Number(provider.model_count) || 0, 25);
  if (
    !window.confirm(
      `Probe ${models || "up to 25"} model(s) on ${provider.display_name || provider.provider_id}? ` +
        "That is one small upstream request per model per probe (2 probes), " +
        "each capped at 16 output tokens, plus two more for the whole run if " +
        "this host reads a client identity off the request. " +
        "Nothing runs on the request path.",
    )
  ) {
    return;
  }
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Probing";
  try {
    const result = await api(
      `/admin/api/providers/${provider.provider_id}/probe`,
      { method: "POST", body: JSON.stringify({ models: [] }) },
    );
    const learned = (result.results || []).filter(
      (row) => row.status === "learned",
    ).length;
    if (result.status === "unprobeable") {
      showMessage(
        `${provider.display_name || provider.provider_id} could not be probed ` +
          `(${result.detail}). Nothing was claimed.`,
        "warn",
      );
    } else {
      showMessage(
        `Probed ${(result.models || []).length} model(s); learned ${learned} fact(s). ` +
          "See the Models page.",
        learned ? "ok" : "warn",
      );
    }
  } catch (error) {
    showMessage(`Could not probe capabilities: ${error.message}`, "error");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

/** Per-key health for a custom pool, in the static pool's own badges. */
async function loadCustomProviderKeyHealth(providerId) {
  let info;
  try {
    info = await api(`/admin/api/custom-providers/${providerId}/keys`);
  } catch (error) {
    return;
  }
  const card = document.querySelector(`[data-custom-provider="${providerId}"]`);
  if (!card) return;
  const rows = card.querySelectorAll(".cp-key-row");
  const list = card.querySelector(".cp-key-list");
  const entries = Array.isArray(info.rows) ? info.rows : [];
  const pool = {
    stateKey: `custom:${providerId}`,
    panel: card,
    locked: false,
    orderUrl: `/admin/api/custom-providers/${providerId}/keys/order`,
    nameUrl: (id) =>
      `/admin/api/custom-providers/${providerId}/keys/${encodeURIComponent(id)}/name`,
    reload: () => loadCustomProviders(),
  };
  entries.forEach((entry, index) => {
    const row = rows[index];
    if (!row) return;
    const controls = keyRowControls(pool, list, row, entry, index, entries.length);
    row.insertBefore(controls.name, row.firstChild);
    row.insertBefore(controls.grip, row.firstChild);
    const label = row.querySelector(".cp-key-label");
    if (label) paintKeyReference(label, entry.masked, entry.name || "");
    const remove = row.querySelector(".cp-key-remove");
    if (remove) row.insertBefore(controls.up, remove);
    if (remove) row.insertBefore(controls.down, remove);
  });
  if (list) list.addEventListener("pointerover", continueKeyDrag);
  (info.health || []).forEach((health, index) => {
    const row = rows[index];
    if (!row || !health) return;
    const slot = document.createElement("span");
    slot.className = "cp-key-health";
    slot.appendChild(keyHealthBadge(health));
    const benched = Array.isArray(health.model_benches) ? health.model_benches : [];
    if (benched.length) slot.appendChild(modelBenchList(benched));
    // Between the key and the Move buttons, which is where an env-key row
    // and a web-search row both put it. It used to land after Move down, so
    // the same rail read in a different order on a custom card.
    row.insertBefore(
      slot,
      row.querySelector(".key-manager-move") || row.lastElementChild,
    );
  });
  const pending =
    state.keyPoolMessage && state.keyPoolMessage.key === pool.stateKey
      ? state.keyPoolMessage
      : null;
  state.keyPoolMessage = null;
  if (pending) {
    announceKeyPool(
      card,
      pending.sentence,
      pending.undo ? "Undo" : "",
      pending.undo ? undoKeyPoolOrder : null,
    );
  }
}

async function probeCustomProviderDialect(provider, button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Probing";
  try {
    const result = await api(
      `/admin/api/custom-providers/${provider.provider_id}/reasoning-probe`,
      { method: "POST", body: "{}" },
    );
    showMessage(
      `Reasoning dialect: ${result.reasoning_dialect_label}`,
      result.reasoning_effort_enum ? "ok" : "warn",
    );
    await loadCustomProviders();
  } catch (error) {
    showMessage(`Could not probe dialect: ${error.message}`, "error");
    button.disabled = false;
    button.textContent = original;
  }
}

async function saveCustomProviderDialect(provider, value, button) {
  button.disabled = true;
  try {
    const result = await api(
      `/admin/api/custom-providers/${provider.provider_id}`,
      {
        method: "PATCH",
        body: JSON.stringify({ reasoning_effort_enum: value }),
      },
    );
    showMessage(`Reasoning dialect: ${result.reasoning_dialect_label}`, "ok");
    await loadCustomProviders();
  } catch (error) {
    showMessage(`Could not save dialect: ${error.message}`, "error");
    button.disabled = false;
  }
}

async function setCustomProviderEnabled(provider, enabled, button) {
  button.disabled = true;
  try {
    const result = await api(
      `/admin/api/custom-providers/${provider.provider_id}`,
      { method: "PATCH", body: JSON.stringify({ enabled }) },
    );
    showMessage(describeCustomProviderRoutes(result, enabled), "ok");
    await loadCustomProviders();
  } catch (error) {
    showMessage(`Could not update provider: ${error.message}`, "error");
    button.disabled = false;
  }
}

/** Say out loud what happened to the routes that named this provider. */
function describeCustomProviderRoutes(result, enabled) {
  const routes = result.routes || {};
  const touched = routes.paused || routes.unpaused || [];
  const verb = enabled ? "Enabled" : "Disabled";
  if (!touched.length) return `${verb} ${result.display_name}.`;
  const refs = touched.map((entry) => entry.model_ref).join(", ");
  const what = enabled ? "un-paused" : "paused";
  return `${verb} ${result.display_name}; ${what} ${touched.length} chain entry(s): ${refs}.`;
}

function updateCustomProviderCard(providerId, status, label, metaText, modelCount) {
  const card = document.querySelector(`[data-custom-provider="${providerId}"]`);
  if (!card) return;
  const pill = card.querySelector(".status-pill");
  pill.className = `status-pill ${statusClass(status)}`;
  pill.textContent = label;
  if (metaText) {
    card.querySelector(".provider-meta").textContent = metaText;
  }
  // The count line used to be written once, at render time, and never again --
  // so a card could read "0 models" directly under a refresh that had just
  // returned 44.
  if (typeof modelCount !== "number") return;
  const provider = state.customProviders.find(
    (entry) => entry.provider_id === providerId,
  );
  if (!provider) return;
  provider.model_count = modelCount;
  const details = card.querySelector(".cp-details");
  if (details) details.textContent = customProviderDetailsText(provider);
}

async function refreshCustomProviderModels(provider, button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Refreshing";
  try {
    const result = await api(
      `/admin/api/providers/${provider.provider_id}/test`,
      { method: "POST", body: "{}" },
    );
    if (result.ok) {
      updateCustomProviderCard(
        provider.provider_id,
        "reachable",
        `${result.models.length} models`,
        result.models.slice(0, 3).join(", ") || "No models returned",
        result.models.length,
      );
      setModelOptions([
        ...state.modelOptions,
        ...result.models.map((model) => `${provider.provider_id}/${model}`),
      ]);
      // A refresh is when a custom provider's models are re-read, so it is
      // also when its host is re-asked what it spells reasoning_effort with.
      // The two facts go stale together.
      try {
        await api(
          `/admin/api/custom-providers/${provider.provider_id}/reasoning-probe`,
          { method: "POST", body: "{}" },
        );
        await loadCustomProviders();
      } catch (error) {
        /* The models refreshed; the dialect is optional. */
      }
    } else {
      updateCustomProviderCard(
        provider.provider_id,
        "offline",
        result.error_type,
        result.message || result.error_type,
        0,
      );
    }
  } catch (error) {
    updateCustomProviderCard(
      provider.provider_id,
      "offline",
      "error",
      error.message,
    );
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

async function addCustomProviderKey(provider, input, button, nameInput) {
  const key = input.value.trim();
  if (!key) {
    showMessage("Enter a key first", "warn");
    return;
  }
  const name = nameInput ? nameInput.value.trim().slice(0, 60) : "";
  button.disabled = true;
  try {
    const result = await api(
      `/admin/api/custom-providers/${provider.provider_id}/keys`,
      { method: "POST", body: JSON.stringify({ api_key: key, name }) },
    );
    const called = result.name ? ` Called ${result.name}.` : "";
    showMessage(
      `Added key ${result.added} (${result.key_count} configured).${called}`,
      "ok",
    );
    await loadCustomProviders();
  } catch (error) {
    showMessage(`Could not add key: ${error.message}`, "error");
  } finally {
    button.disabled = false;
  }
}

async function removeCustomProviderKey(provider, index, button, id) {
  button.disabled = true;
  const guard = id ? `?id=${encodeURIComponent(id)}` : "";
  try {
    const result = await api(
      `/admin/api/custom-providers/${provider.provider_id}/keys/${index}${guard}`,
      { method: "DELETE" },
    );
    showMessage(`Removed key ${result.removed} (${result.key_count} remaining).`, "ok");
    await loadCustomProviders();
  } catch (error) {
    showMessage(`Could not remove key: ${error.message}`, "error");
  } finally {
    button.disabled = false;
  }
}

async function deleteCustomProvider(provider, button) {
  const confirmed = window.confirm(
    `Delete custom provider "${provider.display_name}" (${provider.provider_id})?`,
  );
  if (!confirmed) return;
  button.disabled = true;
  try {
    const result = await api(
      `/admin/api/custom-providers/${provider.provider_id}`,
      { method: "DELETE" },
    );
    // A silent rewrite of somebody's routing is not an improvement on a
    // crash, so every MODEL* key that lost a ref is named here.
    const removed = result.removed_route_refs || [];
    showMessage(
      removed.length
        ? `Deleted ${provider.display_name}; removed from ${removed.join(", ")}.`
        : `Deleted ${provider.display_name}.`,
      "ok",
    );
    await loadCustomProviders();
  } catch (error) {
    showMessage(`Could not delete provider: ${error.message}`, "error");
    button.disabled = false;
  }
}

function openCustomProviderForm(provider) {
  state.editingCustomProviderId = provider ? provider.provider_id : null;
  byId("cpDisplayName").value = provider ? provider.display_name : "";
  byId("cpBaseUrl").value = provider ? provider.base_url : "";
  byId("cpApiKey").value = "";
  byId("cpApiKeyField").hidden = Boolean(provider);
  byId("cpRotation").value = provider ? provider.credential_rotation : "failover";
  byId("cpProxy").value = provider && provider.proxy ? provider.proxy : "";
  renderCustomProviderSurfaces(provider);
  byId("cpSubmitButton").textContent = provider ? "Save changes" : "Add provider";
  byId("customProviderForm").hidden = false;
  byId("cpDisplayName").focus();
}

function closeCustomProviderForm() {
  byId("customProviderForm").hidden = true;
  state.editingCustomProviderId = null;
}

async function submitCustomProviderForm(event) {
  event.preventDefault();
  const editingId = state.editingCustomProviderId;
  const button = byId("cpSubmitButton");
  button.disabled = true;
  let failedDiscovery = null;
  try {
    if (editingId) {
      await api(`/admin/api/custom-providers/${editingId}`, {
        method: "PATCH",
        body: JSON.stringify({
          display_name: byId("cpDisplayName").value,
          base_url: byId("cpBaseUrl").value,
          credential_rotation: byId("cpRotation").value,
          proxy: byId("cpProxy").value,
          surfaces: customProviderSurfacesValue(),
        }),
      });
      showMessage(`Updated ${editingId}.`, "ok");
    } else {
      const result = await api("/admin/api/custom-providers", {
        method: "POST",
        body: JSON.stringify({
          display_name: byId("cpDisplayName").value,
          base_url: byId("cpBaseUrl").value,
          api_key: byId("cpApiKey").value,
          credential_rotation: byId("cpRotation").value,
          proxy: byId("cpProxy").value,
          surfaces: customProviderSurfacesValue(),
        }),
      });
      const discovery = result.discovery || {};
      if (discovery.ok === false) {
        failedDiscovery = { provider_id: result.provider_id, ...discovery };
      }
      if (result.test_error) {
        showMessage(
          `Added ${result.display_name}, but model discovery failed: ` +
            `${discovery.message || result.test_error}. ` +
            "Press Refresh models on the card to try again.",
          "warn",
        );
      } else {
        const preview = result.models.slice(0, 3).join(", ");
        showMessage(
          `Added ${result.display_name} — ${result.model_count} models detected` +
            (preview ? `: ${preview}` : ""),
          "ok",
        );
      }
      setModelOptions([
        ...state.modelOptions,
        ...result.models.map((model) => `${result.provider_id}/${model}`),
      ]);
    }
    closeCustomProviderForm();
    await loadCustomProviders();
    // The list response knows the catalogue is empty but not *why*. Without
    // this the card comes back looking healthy after a discovery failure --
    // the exact silent success this change exists to remove.
    if (failedDiscovery) {
      updateCustomProviderCard(
        failedDiscovery.provider_id,
        "offline",
        failedDiscovery.error_type,
        failedDiscovery.message || failedDiscovery.error_type,
        0,
      );
    }
  } catch (error) {
    showMessage(`Could not save custom provider: ${error.message}`, "error");
  } finally {
    button.disabled = false;
  }
}

byId("addCustomProviderButton").addEventListener("click", () =>
  openCustomProviderForm(null),
);
byId("cpCancelButton").addEventListener("click", closeCustomProviderForm);
byId("customProviderForm").addEventListener("submit", submitCustomProviderForm);

/* --------------------------------------------------------------------- */
/* Version / self-update                                                 */
/* --------------------------------------------------------------------- */

function versionDismissKey(version) {
  return `mcc-version-dismissed-${version}`;
}

function formatCheckedAt(epochSeconds) {
  if (epochSeconds == null) return "Never checked";
  return new Date(epochSeconds * 1000).toLocaleString();
}

async function loadVersionInfo() {
  try {
    state.versionInfo = await api("/admin/api/version");
  } catch (error) {
    state.versionInfo = { error: error.message };
  }
  renderVersionIndicator();
  renderVersionBanners();
  renderVersionPanel();
}

function renderVersionIndicator() {
  const indicator = byId("versionIndicator");
  if (!indicator) return;
  const info = state.versionInfo;
  indicator.innerHTML = "";
  if (!info) return;
  const label = document.createElement("span");
  label.textContent = info.current ? `v${info.current}` : "version unknown";
  indicator.appendChild(label);
  if (info.update_available) {
    const dot = document.createElement("span");
    dot.className = "version-update-dot";
    dot.title = info.latest ? `Update available: v${info.latest}` : "Update available";
    indicator.appendChild(dot);
  }
}

function renderVersionBanners() {
  const container = byId("versionBanners");
  if (!container) return;
  container.innerHTML = "";
  const info = state.versionInfo;
  if (!info || info.error) return;

  // A deferred install reports its outcome only after the old server has exited,
  // so surface the one-time receipt from the relaunched process.
  if (info.pending_upgrade) {
    const banner = document.createElement("div");
    banner.className = info.pending_upgrade.ok
      ? "version-banner"
      : "version-banner restart-required";
    const body = document.createElement("div");
    body.className = "version-banner-body";
    const title = document.createElement("div");
    title.className = "version-banner-title";
    // `restarted` is the helper's answer to "is there a server?", and it is the
    // half of a failure the user actually needs: until 6.58.3 a failed update
    // left no server running at all, so "the update did not install" was read
    // as "and nothing is coming back". The helper now starts the previously
    // installed server on both branches and says which happened.
    const restartedOld = info.pending_upgrade.restarted === true;
    title.textContent = info.pending_upgrade.ok
      ? `Updated and restarted on v${info.current}`
      : restartedOld
        ? "The update failed — the previous version was restarted"
        : "The staged update did not install";
    const detail = document.createElement("div");
    detail.className = "version-banner-detail";
    detail.textContent = info.pending_upgrade.ok
      ? info.pending_upgrade.message || "The deferred install completed."
      : `${
          info.pending_upgrade.message || "The update helper reported a failure."
        } Re-run the install command to update.`;
    body.append(title, detail);
    banner.appendChild(body);
    container.appendChild(banner);
    return;
  }

  if (info.restart_required) {
    const banner = document.createElement("div");
    banner.className = "version-banner restart-required";
    const body = document.createElement("div");
    body.className = "version-banner-body";
    const title = document.createElement("div");
    title.className = "version-banner-title";
    title.textContent = info.staged_install
      ? "Update staged — restarting automatically"
      : "Update installed — restarting automatically";
    const detail = document.createElement("div");
    detail.className = "version-banner-detail";
    detail.textContent = info.staged_install
      ? "The helper will install after this process closes, then start the updated server."
      : "The new version is installed; the server is closing and will reconnect here.";
    body.append(title, detail);
    banner.appendChild(body);
    container.appendChild(banner);
    return;
  }

  // The desktop app is the other half of "are you up to date". Until 6.60.0
  // the pin was enforced only by the Python tray, so a window launched from
  // the Start Menu could sit fifteen releases behind while this panel said
  // everything was current. It is a banner of its own and not a line in the
  // wheel's, because the two update by different mechanisms and at different
  // moments: the wheel restarts the server, the app changes on its next start.
  if (info.shell_update_available && info.shell_pinned_tag) {
    const banner = document.createElement("div");
    banner.className = "version-banner";
    const body = document.createElement("div");
    body.className = "version-banner-body";
    const title = document.createElement("div");
    title.className = "version-banner-title";
    title.textContent = `Desktop app ${info.shell_pinned_tag} available — it updates the next time you restart the app`;
    const detail = document.createElement("div");
    detail.className = "version-banner-detail";
    detail.textContent = info.shell_installed_tag
      ? `The window you are looking at is ${info.shell_installed_tag}. The new one is downloaded and verified in the background, then swapped in when the app next starts.`
      : "The new one is downloaded and verified in the background, then swapped in when the app next starts.";
    body.append(title, detail);
    banner.appendChild(body);
    container.appendChild(banner);
  }

  if (!info.update_available || !info.latest) return;
  if (localStorage.getItem(versionDismissKey(info.latest)) === "1") return;

  const banner = document.createElement("div");
  banner.className = "version-banner";
  const body = document.createElement("div");
  body.className = "version-banner-body";
  const title = document.createElement("div");
  title.className = "version-banner-title";
  title.textContent = `Update available: v${info.latest}`;
  const detail = document.createElement("div");
  detail.className = "version-banner-detail";
  if (info.release_url) {
    const link = document.createElement("a");
    link.href = info.release_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = info.release_name || `v${info.latest}`;
    detail.appendChild(link);
  } else {
    detail.textContent = info.release_name || `v${info.latest}`;
  }
  body.append(title, detail);

  // Without the notes the banner only says a number changed, which tells you
  // nothing about whether the update matters to you.
  if (info.release_notes) {
    const notes = document.createElement("details");
    notes.className = "version-banner-notes";
    const summary = document.createElement("summary");
    summary.textContent = "What changed";
    const text = document.createElement("pre");
    text.textContent = info.release_notes;
    notes.append(summary, text);
    body.appendChild(notes);
  }

  const actions = document.createElement("div");
  actions.className = "version-banner-actions";
  const updateButton = document.createElement("button");
  updateButton.type = "button";
  updateButton.className = "primary-button";
  updateButton.textContent = "Update now";
  updateButton.addEventListener("click", () => runVersionUpgrade(updateButton));
  const dismissButton = document.createElement("button");
  dismissButton.type = "button";
  dismissButton.className = "ghost-button";
  dismissButton.textContent = "Dismiss";
  dismissButton.addEventListener("click", () => {
    localStorage.setItem(versionDismissKey(info.latest), "1");
    renderVersionBanners();
  });
  actions.append(updateButton, dismissButton);

  banner.append(body, actions);
  container.appendChild(banner);
}

function renderVersionPanel() {
  const details = byId("versionDetails");
  const checkButton = byId("versionCheckButton");
  const updateButton = byId("versionUpdateButton");
  if (!details || !checkButton || !updateButton) return;
  const info = state.versionInfo;

  details.innerHTML = "";
  const entries = [
    ["Current", info?.current ? `v${info.current}` : "—"],
    ["Latest", info?.latest ? `v${info.latest}` : "—"],
    ["Last checked", formatCheckedAt(info?.checked_at)],
  ];
  if (info?.shell_installed_tag || info?.shell_pinned_tag) {
    entries.push([
      "Desktop app",
      info.shell_update_available
        ? `${info.shell_installed_tag || "unknown"} → ${info.shell_pinned_tag} on next restart`
        : info.shell_installed_tag || info.shell_pinned_tag,
    ]);
  }
  entries.forEach(([label, value]) => {
    const dl = document.createElement("dl");
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    dl.append(dt, dd);
    details.appendChild(dl);
  });
  if (info?.error) {
    const note = document.createElement("p");
    note.className = "version-error field-description";
    note.textContent = `Could not check for updates: ${info.error}`;
    details.appendChild(note);
  }

  if (!state.versionUpgrading) {
    updateButton.disabled = !info?.update_available;
    updateButton.textContent = "Update now";
  }
}

async function checkForUpdates(button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Checking...";
  try {
    state.versionInfo = await api("/admin/api/version/check", {
      method: "POST",
      body: "{}",
    });
    renderVersionIndicator();
    renderVersionBanners();
    renderVersionPanel();
    showMessage(
      state.versionInfo.update_available
        ? `Update available: v${state.versionInfo.latest}`
        : "Already up to date",
      "ok",
    );
  } catch (error) {
    showMessage(`Could not check for updates: ${error.message}`, "error");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

// What a browser tab can honestly say about an update it cannot watch.
//
// The dashboard is HTTP-only and the update deliberately stops the server, so
// for the ninety seconds that matter there is nothing for this page to poll and
// no way for it to read a file on the machine. Until 6.71.0 it said "Updating...
// (this can take a few minutes)" and then nothing at all, which is
// indistinguishable from a page that has hung. It now hands over the two paths
// the installer is writing -- so the user can open either one in an editor --
// and names the desktop app, which is the only thing that CAN show the install
// happening line by line.
function describeUpdateOutage(result) {
  const lines = [];
  if (result.log_path) {
    lines.push(`The installer writes to: ${result.log_path}`);
  }
  if (result.progress_path) {
    lines.push(`Its stage receipt is:    ${result.progress_path}`);
  }
  if (!lines.length) return "";
  lines.push("");
  lines.push(
    "This tab cannot read those files or reach the server while it is being replaced,",
  );
  lines.push(
    "so it counts down instead. The desktop app shows the stage timeline and the last",
  );
  lines.push("lines of that log as they are written.");
  return lines.join("\n");
}

async function waitForUpdatedServer(expectedVersion, onCountdown) {
  // The reconnect window comes from the server (the install + graceful-drain +
  // startup budget), not a hard-coded two minutes: a slow upgrade must not be
  // abandoned mid-handoff. Fall back to 120s if the status lacks the field.
  const reconnectSeconds =
    (state.versionInfo && state.versionInfo.dashboard_reconnect_timeout_seconds) ||
    120;
  const deadline = Date.now() + reconnectSeconds * 1000;
  let sawDisconnect = false;
  while (Date.now() < deadline) {
    if (typeof onCountdown === "function") {
      onCountdown(Math.max(0, Math.round((deadline - Date.now()) / 1000)));
    }
    try {
      const info = await api("/admin/api/version");
      if (!expectedVersion || info.current === expectedVersion) return info;
    } catch {
      // The old process is expected to disappear between the upgrade response
      // and the new process binding the port. Silence that ordinary handoff.
      sawDisconnect = true;
    }
    await new Promise((resolve) => setTimeout(resolve, sawDisconnect ? 1000 : 500));
  }
  throw new Error(
    expectedVersion
      ? `The server did not come back on v${expectedVersion} within two minutes.`
      : "The server did not come back within two minutes.",
  );
}

async function runVersionUpgrade(button) {
  if (state.versionUpgrading) return;
  state.versionUpgrading = true;
  const logEl = byId("versionUpgradeLog");
  const updateButton = byId("versionUpdateButton");
  [button, updateButton].forEach((candidate) => {
    if (candidate) {
      candidate.disabled = true;
      candidate.textContent = "Updating... (this can take a few minutes)";
    }
  });
  if (logEl) {
    logEl.hidden = true;
    logEl.textContent = "";
  }
  try {
    // The desktop app injects `window.__mccShellWatching` into every dashboard
    // it loads. It says one thing: a process with a ten-second lifecycle tick
    // is watching this server and will start the new one itself. The update
    // helper therefore installs and exits instead of starting a server nobody
    // supervises -- two owners of "restart the server" is how one update came
    // to start two of them. A dashboard in an ordinary browser tab sends
    // false, and the helper restarts exactly as it always has.
    const result = await api("/admin/api/version/upgrade", {
      method: "POST",
      body: JSON.stringify({ no_restart: !!window.__mccShellWatching }),
    });
    if (logEl && Array.isArray(result.log) && result.log.length) {
      logEl.textContent = result.log.join("\n");
      logEl.hidden = false;
    }
    if (result.ok) {
      // Hand over the paths BEFORE the server goes away: once it does, this
      // page has no channel left at all.
      const outage = describeUpdateOutage(result);
      if (logEl && outage) {
        logEl.textContent = logEl.textContent
          ? `${logEl.textContent}\n\n${outage}`
          : outage;
        logEl.hidden = false;
      }
      [button, updateButton].forEach((candidate) => {
        if (candidate) candidate.textContent = "Restarting — reconnecting...";
      });
      showMessage(result.message || "Update installed; restarting...", "ok");
      state.versionInfo = await waitForUpdatedServer(
        result.installed_version,
        (remaining) => {
          [button, updateButton].forEach((candidate) => {
            if (candidate) {
              candidate.textContent = `Installing — reconnecting in up to ${remaining}s`;
            }
          });
        },
      );
      renderVersionIndicator();
      renderVersionBanners();
      renderVersionPanel();
      const serverHalf = state.versionInfo.current
        ? `Updated and restarted on v${state.versionInfo.current}`
        : "Updated and restarted";
      // Both halves, because an update that moves the shell pin leaves the
      // window on the old build until it is restarted, and a message that only
      // named the server would be telling the user they are finished when they
      // are not.
      showMessage(
        state.versionInfo.shell_update_available && state.versionInfo.shell_pinned_tag
          ? `${serverHalf}. The desktop app updates to ${state.versionInfo.shell_pinned_tag} the next time you restart it.`
          : serverHalf,
        "ok",
      );
    } else {
      showMessage(result.message || "Update failed", "error");
      await loadVersionInfo();
    }
  } catch (error) {
    showMessage(`Update failed: ${error.message}`, "error");
  } finally {
    state.versionUpgrading = false;
    if (button) button.textContent = "Update now";
    renderVersionPanel();
  }
}

byId("versionCheckButton").addEventListener("click", (event) =>
  checkForUpdates(event.currentTarget),
);
byId("versionUpdateButton").addEventListener("click", (event) =>
  runVersionUpgrade(event.currentTarget),
);

/* --------------------------------------------------------------------- */
/* Desktop tray preferences                                                */
/* --------------------------------------------------------------------- */

async function loadDesktopState() {
  try {
    state.desktop = await api("/admin/api/desktop");
  } catch (error) {
    state.desktop = { error: error.message };
  }
  try {
    state.autostartOptions = await api("/admin/api/desktop/autostart-options");
  } catch (error) {
    state.autostartOptions = { error: error.message };
  }
  renderDesktopState();
}

const SERVER_MODE_HINTS = {
  spawn: "The tray starts mcc-server as a child when nothing is listening on the port.",
  attach: "The tray connects to a server you start yourself and never spawns one.",
  off: "The tray never touches the server.",
};

const WINDOW_HINTS = {
  auto: "The default. Uses the My Claude Code desktop app, downloading it once if needed; falls back to a Chromium app-mode window, then a browser tab.",
  "app-mode": "A Chrome/Edge/Brave window with no tabs or URL bar, using its own profile.",
  pywebview:
    "An embedded webview. Not installed by default, and OAuth login, downloads and copy buttons may not work in it.",
  browser: "A normal tab in your default browser.",
};

const WINDOW_PROVIDER_LABELS = {
  shell: "desktop app",
  "app-mode": "app-mode",
  pywebview: "embedded webview",
  browser: "browser tab",
};

function renderDesktopState() {
  const trayEnabled = byId("desktopTrayEnabled");
  const closeToTray = byId("desktopCloseToTray");
  const serverMode = byId("desktopServerMode");
  const hint = byId("desktopServerModeHint");
  if (trayEnabled) {
    trayEnabled.checked = Boolean(state.desktop?.tray_enabled);
    trayEnabled.disabled = state.desktopBusy;
  }
  if (closeToTray) {
    closeToTray.checked = Boolean(state.desktop?.close_to_tray);
    // Without a tray there is nowhere to close to, and a switch that cannot
    // do anything is worse than one that is not there.
    closeToTray.disabled =
      state.desktopBusy || !state.desktop?.tray_enabled;
  }
  if (serverMode && hint) {
    serverMode.value = state.desktop?.server_mode || "spawn";
    serverMode.disabled = state.desktopBusy;
    hint.textContent = SERVER_MODE_HINTS[serverMode.value] || "";
  }
  renderDesktopWindow();
  renderDesktopAutostartOptions();
}

function renderDesktopWindow() {
  const windowSelect = byId("desktopWindow");
  const hint = byId("desktopWindowHint");
  const resolved = byId("desktopWindowResolved");
  if (!windowSelect || !hint) return;

  windowSelect.value = state.desktop?.window || "auto";
  windowSelect.disabled = state.desktopBusy;
  hint.textContent = WINDOW_HINTS[windowSelect.value] || "";

  if (!resolved) return;
  if (windowSelect.value !== "auto") {
    resolved.textContent = "";
    return;
  }
  const provider = state.desktop?.window_auto_provider;
  const reason = state.desktop?.window_auto_reason;
  if (!provider) {
    resolved.textContent = "";
    return;
  }
  const label = WINDOW_PROVIDER_LABELS[provider] || provider;
  resolved.textContent = reason ? `auto → ${label} (${reason})` : `auto → ${label}`;
}

function autostartTargetLabel(target) {
  return target === "tray" ? "Tray (mcc-desktop)" : "Server (mcc-server, headless)";
}

function renderDesktopAutostartOptions() {
  const container = byId("desktopAutostartOptions");
  const originEl = byId("desktopOrigin");
  if (!container || !originEl) return;

  const options = state.autostartOptions;
  originEl.innerHTML = "";
  container.replaceChildren();

  if (options?.error || !options?.targets?.length) {
    originEl.textContent = "Autostart options unavailable.";
    return;
  }

  const origin = document.createElement("span");
  origin.className = "claude-origin";
  origin.textContent = options.origin || "this machine";
  originEl.append(origin);

  const wanted = Boolean(state.desktop?.start_at_login);
  // What the OS actually carries. `null`/undefined means it could not be read,
  // and only then does the box fall back to showing the intent -- a checked
  // box that the machine does not back up is the defect this replaced.
  const registered = state.desktop?.start_at_login_registered;
  const known = registered === true || registered === false;
  const current = known ? registered : wanted;
  options.targets.forEach((target) => {
    const inputId = `desktopAutostart-${target}`;
    const input = document.createElement("input");
    input.type = "checkbox";
    input.id = inputId;
    input.checked = current;
    input.disabled = state.desktopBusy;

    const label = document.createElement("label");
    label.className = "toggle-control";
    label.htmlFor = inputId;
    label.append(input, ` Start at Login (${autostartTargetLabel(target)})`);
    container.append(label);

    if (known && registered !== wanted) {
      const note = document.createElement("p");
      note.className = "desktop-autostart-note";
      note.setAttribute("role", "status");
      note.textContent = wanted
        ? "Saved, but this machine is not registered yet. The next desktop launch registers it."
        : "Still registered on this machine. The next desktop launch removes it.";
      container.append(note);
    }

    input.addEventListener("change", () => {
      updateDesktop("start_at_login", input.checked, input);
    });
  });
}

async function updateDesktop(field, value, control) {
  if (state.desktopBusy) return;
  state.desktopBusy = true;
  renderDesktopState();
  try {
    state.desktop = await api("/admin/api/desktop", {
      method: "POST",
      body: JSON.stringify({ [field]: value }),
    });
    if (field === "start_at_login") {
      // Say what the machine now carries, not what was asked for. The route
      // reconciles the registration before it answers.
      const registered = state.desktop?.start_at_login_registered;
      if (registered === value) {
        showMessage(`Start at Login ${value ? "enabled" : "disabled"}`, "ok");
      } else {
        showMessage(
          `Start at Login ${value ? "enabled" : "disabled"} for the next launch`,
          "ok",
        );
      }
    } else if (field === "server_mode") {
      showMessage(`Server mode set to ${value}.`, "ok");
    } else if (field === "window") {
      showMessage(`Window set to ${value}.`, "ok");
    } else {
      showMessage(`Tray ${value ? "enabled" : "disabled"} for the next tray launch`, "ok");
    }
  } catch (error) {
    if (control && control.type === "checkbox") control.checked = !value;
    showMessage(`Could not save desktop preference: ${error.message}`, "error");
  } finally {
    state.desktopBusy = false;
    renderDesktopState();
  }
}

byId("desktopServerMode").addEventListener("change", (event) => {
  updateDesktop("server_mode", event.currentTarget.value, event.currentTarget);
});
byId("desktopWindow").addEventListener("change", (event) => {
  updateDesktop("window", event.currentTarget.value, event.currentTarget);
});
byId("desktopTrayEnabled").addEventListener("change", (event) => {
  updateDesktop("tray_enabled", event.currentTarget.checked, event.currentTarget);
});
byId("desktopCloseToTray").addEventListener("change", (event) => {
  updateDesktop("close_to_tray", event.currentTarget.checked, event.currentTarget);
});

/* --------------------------------------------------------------------- */
/* Other My Claude Code servers on this machine                            */
/* --------------------------------------------------------------------- */

async function loadOtherServers() {
  try {
    state.otherServers = await api("/admin/api/servers");
  } catch (error) {
    state.otherServers = { error: error.message };
  }
  renderOtherServers();
}

function formatStamp(seconds) {
  if (typeof seconds !== "number") return "unknown";
  return new Date(seconds * 1000).toLocaleString();
}

function formatAge(seconds) {
  if (typeof seconds !== "number") return "never";
  if (seconds < 90) return `${Math.round(seconds)}s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m ago`;
  return `${Math.round(seconds / 3600)}h ago`;
}

// Exactly the fields the decision needs, in the order 6.72.2 fixed them:
// pid, session, port, started, last heartbeat. A dialog that offers to stop a
// process and does not say which process is not a confirmation.
function serverFacts(server) {
  const where =
    server.host && server.port
      ? `${server.host}:${server.port}`
      : server.port
        ? `port ${server.port}`
        : "no recorded address";
  return [
    ["pid", (server.pids || []).join(", ")],
    ["session", server.session_id ? String(server.session_id) : "no session row"],
    ["address", where],
    ["started", formatStamp(server.started_at)],
    ["last heartbeat", formatAge(server.heartbeat_age_seconds)],
  ];
}

function renderOtherServers() {
  const list = byId("otherServersList");
  const statusLine = byId("otherServersStatus");
  const stopButton = byId("otherServersStop");
  if (!list || !statusLine || !stopButton) return;

  list.textContent = "";
  const payload = state.otherServers;
  if (payload?.error) {
    statusLine.textContent = `Could not read the server survey: ${payload.error}`;
    stopButton.disabled = true;
    stopButton.textContent = "No stale servers";
    return;
  }

  const servers = Array.isArray(payload?.servers) ? payload.servers : [];
  if (!servers.length) {
    const empty = document.createElement("p");
    empty.className = "other-servers-empty";
    empty.textContent =
      "No other My Claude Code server is running on this machine.";
    list.append(empty);
  }

  servers.forEach((server) => {
    const card = document.createElement("div");
    card.className = "other-server-card";
    card.dataset.pids = (server.pids || []).join(",");

    const heading = document.createElement("div");
    heading.className = "other-server-heading";
    const badge = document.createElement("span");
    badge.className = `other-server-status${server.actionable ? " is-stale" : ""}`;
    badge.textContent = server.status || "unknown";
    heading.append(badge, document.createTextNode(`pid ${(server.pids || []).join(", ")}`));
    card.append(heading);

    const facts = document.createElement("p");
    facts.className = "other-server-facts";
    serverFacts(server).forEach(([label, value]) => {
      const item = document.createElement("span");
      item.textContent = `${label}: ${value}`;
      facts.append(item);
    });
    card.append(facts);

    const reason = document.createElement("p");
    reason.className = "other-server-reason";
    reason.textContent = server.reason || "";
    card.append(reason);

    list.append(card);
  });

  const stale = servers.filter((server) => server.actionable);
  stopButton.disabled = state.otherServersBusy || stale.length === 0;
  stopButton.textContent = stale.length
    ? `Stop ${stale.length} stale server${stale.length === 1 ? "" : "s"}...`
    : "No stale servers";
  if (!statusLine.textContent) {
    statusLine.textContent = servers.length
      ? `${servers.length} other server${servers.length === 1 ? "" : "s"} seen, ` +
        `${stale.length} stale.`
      : "";
  }
}

function staleServerCandidates() {
  const servers = Array.isArray(state.otherServers?.servers)
    ? state.otherServers.servers
    : [];
  return servers.filter((server) => server.actionable);
}

async function stopStaleServers() {
  if (state.otherServersBusy) return;
  const candidates = staleServerCandidates();
  if (!candidates.length) return;

  // Every field, in the confirmation itself. The near-miss this feature is
  // named after was an investigation deciding two live servers were
  // "abandoned" from a single socket scan.
  const lines = candidates.map((server) => {
    const facts = serverFacts(server)
      .map(([label, value]) => `    ${label}: ${value}`)
      .join("\n");
    return `  ${server.status}\n${facts}\n    why: ${server.reason}`;
  });
  const confirmed = window.confirm(
    `Stop ${candidates.length} stale My Claude Code server` +
      `${candidates.length === 1 ? "" : "s"}?\n\n${lines.join("\n\n")}\n\n` +
      "Each is stopped by its exact pid, and only if it is still stale when " +
      "the server looks again.",
  );
  if (!confirmed) return;

  state.otherServersBusy = true;
  renderOtherServers();
  const statusLine = byId("otherServersStatus");
  try {
    const result = await api("/admin/api/servers/stop", {
      method: "POST",
      body: JSON.stringify({
        pids: candidates.map((server) => server.pids || []),
      }),
    });
    state.otherServers = result;
    const stopped = (result.stopped || []).length;
    const refused = (result.refused || []).length;
    statusLine.textContent =
      `Stopped ${stopped} server${stopped === 1 ? "" : "s"}` +
      (refused ? `; ${refused} refused (no longer stale, or already gone).` : ".");
  } catch (error) {
    statusLine.textContent = `Could not stop: ${error.message}`;
  } finally {
    state.otherServersBusy = false;
    renderOtherServers();
  }
}

byId("otherServersRefresh")?.addEventListener("click", () => {
  const statusLine = byId("otherServersStatus");
  if (statusLine) statusLine.textContent = "";
  loadOtherServers();
});

byId("otherServersStop")?.addEventListener("click", () => {
  stopStaleServers();
});

/* --------------------------------------------------------------------- */
/* Token optimizer (RTK)                                                   */
/* --------------------------------------------------------------------- */

async function loadRtkState() {
  try {
    state.rtk = await api("/admin/api/rtk");
  } catch (error) {
    state.rtk = { error: error.message };
  }
  renderRtkState();
}

function rtkCapableHarnesses() {
  // Falling back to whatever RTK itself reported keeps the checkboxes usable
  // when /admin/api/harnesses is the request that failed.
  if (Array.isArray(state.harnesses)) {
    return state.harnesses.filter((harness) => harness.rtk_agent);
  }
  const agents = state.rtk?.agents;
  if (!agents || typeof agents !== "object") return [];
  return Object.keys(agents).map((id) => ({ id, display_name: id }));
}

function renderRtkState() {
  const container = byId("rtkAgentToggles");
  const statusLine = byId("rtkStatusLine");
  if (!container || !statusLine) return;

  container.textContent = "";
  rtkCapableHarnesses().forEach((harness) => {
    const label = document.createElement("label");
    label.className = "toggle-control";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.id = `rtkAgent-${harness.id}`;
    input.dataset.harness = harness.id;
    input.checked = Boolean(state.rtk?.[harness.id]);
    input.disabled = state.rtkBusy;
    input.addEventListener("change", (event) => {
      updateRtk(harness.id, event.currentTarget.checked, event.currentTarget);
    });
    label.append(input, document.createTextNode(` ${harness.display_name}`));
    container.append(label);
  });

  if (state.rtk?.error) {
    statusLine.textContent = `Could not load RTK status: ${state.rtk.error}`;
    statusLine.className = "rtk-status-line error";
    return;
  }
  if (state.rtk?.installed) {
    const version = state.rtk.version ? ` ${state.rtk.version}` : "";
    statusLine.textContent = `RTK installed${version}`;
    statusLine.className = "rtk-status-line ok";
  } else {
    statusLine.textContent =
      "RTK binary not installed. It is downloaded automatically the first time an agent is enabled.";
    statusLine.className = "rtk-status-line warn";
  }
}

async function updateRtk(field, value, toggle) {
  if (state.rtkBusy) return;
  state.rtkBusy = true;
  renderRtkState();
  try {
    state.rtk = await api("/admin/api/rtk", {
      method: "POST",
      body: JSON.stringify({ [field]: value }),
    });
    const harness = rtkCapableHarnesses().find((entry) => entry.id === field);
    showMessage(
      `RTK ${value ? "enabled" : "disabled"} for ${harness?.display_name || field}`,
      "ok",
    );
  } catch (error) {
    toggle.checked = !value;
    showMessage(`Could not update RTK: ${error.message}`, "error");
  } finally {
    state.rtkBusy = false;
    renderRtkState();
  }
}

/* --------------------------------------------------------------------- */
/* Coding agents                                                           */
/* --------------------------------------------------------------------- */

/** Draw a placeholder while a list is being fetched.
 *
 * Both lists used to paint their section headers with empty bodies for
 * several seconds and no spinner, skeleton or word, so the page read as
 * broken during the gap. This is the smallest honest fix: it says a request
 * is in flight, and it is replaced the moment one lands -- including when it
 * lands as an error, which the list already renders. */
function renderListLoading(elementId, what) {
  const list = byId(elementId);
  if (!list || list.children.length) return;
  const line = document.createElement("p");
  line.className = "field-description list-loading";
  line.setAttribute("role", "status");
  line.textContent = `Loading ${what}…`;
  list.append(line);
}

async function loadHarnesses() {
  renderListLoading("codingAgentsList", "coding agents");
  try {
    const payload = await api("/admin/api/harnesses");
    state.harnesses = Array.isArray(payload.harnesses) ? payload.harnesses : [];
  } catch (error) {
    state.harnesses = [];
    renderHarnesses(error.message);
    renderRtkState();
    return;
  }
  // Separate call, separate failure: the cards are still worth rendering when
  // the request log is off or the usage query fails, they just cannot say how
  // much traffic each agent sent.
  try {
    state.harnessUsage = await api("/admin/api/requests/harness-usage?days=7");
  } catch (_) {
    state.harnessUsage = null;
  }
  await loadHarnessTiers();
  renderHarnesses();
  renderRtkState();
}

function renderHarnesses(errorMessage) {
  const list = byId("codingAgentsList");
  if (!list) return;
  list.textContent = "";

  if (errorMessage) {
    const failed = document.createElement("p");
    failed.className = "field-description";
    failed.textContent = `Could not load coding agents: ${errorMessage}`;
    list.append(failed);
    return;
  }
  clearHarnessTierRails();
  (state.harnesses || []).forEach((harness) => {
    list.append(harnessCard(harness));
  });
  syncRoutePauseUi();
}

function harnessCard(harness) {
  const card = document.createElement("div");
  card.className = "coding-agent-card";
  card.dataset.harness = harness.id;

  const heading = document.createElement("div");
  heading.className = "agent-heading";
  const title = document.createElement("h4");
  title.textContent = harness.display_name;
  const badge = document.createElement("span");
  const servable = harness.available !== false;
  // "Not servable" is a different fact from "Not installed" and must not be
  // told with the same word: one is something the user can fix by installing
  // the CLI, the other is something MCC measured and cannot fix at all.
  badge.className = servable
    ? harness.installed
      ? "agent-state installed"
      : "agent-state"
    : "agent-state unavailable";
  badge.textContent = servable
    ? harness.installed
      ? "Installed"
      : "Not installed"
    : "Not servable";
  heading.append(title, badge);
  card.append(heading);

  if (harness.summary) {
    const summary = document.createElement("p");
    summary.className = "agent-summary";
    summary.textContent = harness.summary;
    card.append(summary);
  }

  if (!servable) {
    // The evidence, verbatim from the registry, with the version and the date
    // it was measured on it. No command block: there is no command.
    const reason = document.createElement("p");
    reason.className = "agent-unavailable-reason";
    reason.textContent = harness.unavailable_reason || "";
    card.append(reason);
    card.append(harnessMeta(harness));
    return card;
  }

  const command = document.createElement("code");
  command.className = "agent-command";
  command.textContent = harness.command;
  card.append(command);

  card.append(harnessCommandList(harness));
  card.append(harnessMeta(harness));

  const tiers = harnessTiersSection(harness);
  if (tiers) card.append(tiers);

  if (!harness.installed && harness.install_hint) {
    const hint = document.createElement("p");
    hint.className = "agent-install-hint";
    // MCC installs no coding agent: the card repeats the vendor's own line
    // and offers no button that would run it.
    hint.textContent = harness.install_hint;
    card.append(hint);
  }
  return card;
}

/* --------------------------------------------------------------------- */
/* Desktop apps                                                            */
/* --------------------------------------------------------------------- */
/* The applications MCC does not launch. A CLI harness card answers "is this
   installed, and what does MCC tell it"; a desktop card has to answer a
   different question -- "is the one file this app reads currently pointed at
   MCC, and what exactly would change if I pressed the button" -- so it is its
   own component rather than a fourth branch inside `harnessCard`.

   Six states, and they are the probe's, not the browser's: the server decides
   whether an app is configured or drifted by comparing the file's owned
   subtree against what a re-apply would write, which is a comparison only the
   server can make. The card renders the answer. */

const DESKTOP_STATE_LABELS = {
  not_installed: "Not installed",
  not_routable: "Not routable",
  installed: "Installed, not configured",
  configured: "Configured by MCC",
  drifted: "Configured but drifted",
  unreadable: "Config file will not parse",
  managed: "Managed by your organisation",
  credential_unresolved: "Written, but the key cannot resolve",
  removed_by_app: "The app removed MCC's configuration",
};

const DESKTOP_STATE_CLASS = {
  configured: "agent-state installed",
  drifted: "agent-state drifted",
  not_routable: "agent-state unavailable",
  unreadable: "agent-state unavailable",
  managed: "agent-state unavailable",
  // Both of these are "MCC wrote here and it is not working", which is the
  // same thing `drifted` means to a reader, so they share its colour rather
  // than inventing two more.
  credential_unresolved: "agent-state drifted",
  removed_by_app: "agent-state drifted",
};

async function loadDesktopApps() {
  renderListLoading("desktopAppsList", "desktop apps");
  try {
    const payload = await api("/admin/api/desktop-apps");
    state.desktopApps = Array.isArray(payload.apps) ? payload.apps : [];
    renderDesktopApps();
  } catch (error) {
    state.desktopApps = [];
    renderDesktopApps(error.message);
  }
}

function renderDesktopApps(errorMessage) {
  const list = byId("desktopAppsList");
  if (!list) return;
  list.textContent = "";

  if (errorMessage) {
    const failed = document.createElement("p");
    failed.className = "field-description";
    failed.textContent = `Could not load desktop apps: ${errorMessage}`;
    list.append(failed);
    return;
  }
  (state.desktopApps || []).forEach((app) => {
    list.append(desktopAppCard(app));
  });
}

function desktopAppCard(app) {
  const card = document.createElement("div");
  card.className = "coding-agent-card desktop-app-card";
  card.dataset.desktopApp = app.id;

  const probe = app.probe || {};
  const stateId = probe.state || "not_installed";

  const heading = document.createElement("div");
  heading.className = "agent-heading";
  const title = document.createElement("h4");
  title.textContent = app.display_name;
  const badge = document.createElement("span");
  badge.className = DESKTOP_STATE_CLASS[stateId] || "agent-state";
  badge.textContent = DESKTOP_STATE_LABELS[stateId] || stateId;
  badge.dataset.state = stateId;
  heading.append(title, badge);
  card.append(heading);

  if (app.summary) {
    const summary = document.createElement("p");
    summary.className = "agent-summary";
    summary.textContent = app.summary;
    card.append(summary);
  }

  if (app.status === "not_routable") {
    // The evidence verbatim, with the date it was measured. No buttons:
    // there is nothing to press, and a disabled button would imply there
    // might be one day.
    const reason = document.createElement("p");
    reason.className = "agent-unavailable-reason";
    reason.textContent = app.unavailable_reason || "";
    card.append(reason);
    card.append(desktopAppMeta(app));
    return card;
  }

  if (app.status === "instructions_only") {
    // Why there is no button, dated, in the same place `not_routable` puts
    // its evidence. A card that simply stopped offering Configure between two
    // releases would read as a regression; this one says what was not proven
    // and how to re-check it.
    if (app.instructions_reason) {
      const reason = document.createElement("p");
      reason.className = "agent-unavailable-reason";
      reason.textContent = app.instructions_reason;
      card.append(reason);
    }
    card.append(desktopInstructionTable(app));
    card.append(desktopAppNotes(app));
    card.append(desktopAppMeta(app));
    return card;
  }

  card.append(desktopAppMeta(app));

  // What the last status poll repaired, if anything. An earlier MCC could
  // write a configuration this app rejects at startup, and the repair happens
  // on probe rather than on Configure -- so the card is the only place a user
  // ever finds out it happened, and the only place that can tell them the app
  // has to be relaunched for it to take.
  (probe.repaired || []).forEach((line) => {
    const repaired = document.createElement("p");
    repaired.className = "desktop-repair-note";
    repaired.textContent = line;
    card.append(repaired);
  });

  if (stateId === "managed") {
    // The file MCC would write is the lowest-precedence source this app
    // reads, and a managed profile replaces it wholesale. A Configure button
    // here would write a file with no effect and then report success, so
    // there is no button -- and the card names what is enforcing it.
    const managed = document.createElement("p");
    managed.className = "agent-unavailable-reason";
    const keys = (probe.managed_keys || []).join(", ");
    managed.textContent =
      `${probe.managed_by} sets this app's configuration, and it outranks ` +
      `the file MCC writes${keys ? ` (${keys})` : ""}. MCC will not write a ` +
      "file the app is going to ignore. Ask whoever manages this device.";
    card.append(managed);
    card.append(desktopAppNotes(app));
    return card;
  }

  if (stateId === "unreadable") {
    const error = document.createElement("p");
    error.className = "agent-unavailable-reason";
    error.textContent = `MCC will not write a file it cannot read: ${probe.error || ""}`;
    card.append(error);
    return card;
  }

  if (stateId === "not_installed") {
    // No buttons, the same as a managed card, and for the same reason: the
    // server refuses the write, so a button here could only ever produce an
    // error. It used to be offered and it used to succeed -- MCC wrote a
    // provider into a file no program on the machine reads, and the card went
    // green. What counts as installed is now the program itself, never a
    // directory: two rows read "installed" from data directories MCC's own
    // launchers had created.
    const missing = document.createElement("p");
    missing.className = "agent-unavailable-reason";
    missing.textContent =
      `MCC cannot find ${app.display_name} on this machine, so there is ` +
      "nothing here that would read the file it would write. MCC looks for " +
      "the program itself -- an executable on PATH, an installed " +
      "application, an editor extension -- and not for a configuration " +
      "directory, which anything that has ever run may have created. " +
      "Install it, start it once, and this card becomes a button.";
    card.append(missing);
    card.append(desktopAppNotes(app));
    return card;
  }

  if (stateId === "drifted") {
    const drift = document.createElement("p");
    drift.className = "desktop-drift-note";
    drift.textContent =
      "One of the keys MCC owns has been changed since MCC wrote it -- by hand, " +
      "by the app, or by an older MCC. Configure will bring it back in line.";
    card.append(drift);
  }

  if (stateId === "credential_unresolved") {
    // The state that used to be reported green. The file is right and the
    // app will still fail on its first request, so the card says the one
    // thing that is actually wrong instead of burying it in the meta table.
    const unresolved = document.createElement("p");
    unresolved.className = "desktop-drift-note";
    unresolved.textContent =
      `MCC's configuration is in the file, but ${app.display_name} reads its ` +
      `credential from ${app.token_env_var}, and that is not set where this ` +
      "server can see it. Export it and restart the app -- until then the " +
      "requests will be refused. MCC does not set variables for you, and " +
      "this card turns green by itself once the variable is there.";
    card.append(unresolved);
  }

  if (stateId === "removed_by_app") {
    const removed = document.createElement("p");
    removed.className = "desktop-drift-note";
    removed.textContent =
      `MCC configured ${app.display_name} and the keys are gone, without ` +
      "anyone pressing Undo -- so the application rewrote its own " +
      "configuration file and dropped them. Configure puts them back; if " +
      "they disappear again, the app is doing it on purpose and the card " +
      "will keep saying so rather than reporting it as never configured.";
    card.append(removed);
  }

  card.append(desktopAppActions(app));
  // The fallback, not the main event: a servable app whose config file has
  // never been created yet may be easier to set up in its own dialog, and
  // some of these apps only create the file once you have used that dialog
  // once. Shown only then, so a card can no longer say "here is how to do
  // this by hand" directly under a button that does it.
  if ((app.instruction_fields || []).length && !probe.document_exists) {
    card.append(desktopInstructionTable(app));
  }
  card.append(desktopAppNotes(app));
  return card;
}

/** The facts a card states whatever its state: file, owned key, URL, token. */
function desktopAppMeta(app) {
  const meta = document.createElement("dl");
  meta.className = "agent-meta desktop-app-meta";
  const probe = app.probe || {};

  const rows = [];
  if (app.display_path) rows.push(["Config file", app.display_path]);
  if (app.owned_key) rows.push(["MCC owns", app.owned_key]);
  if ((app.overwrites || []).length) {
    rows.push(["Replaces", app.overwrites.join(", ")]);
  }
  if (app.sidecar_path) rows.push(["MCC-owned file", app.sidecar_path]);
  if ((app.sidecar_keys || []).length) {
    // Key names, never values: one of them is a credential, and it never
    // leaves the server.
    rows.push(["It writes", app.sidecar_keys.join(", ")]);
  }
  if ((app.managed_source_labels || []).length) {
    rows.push([
      "Outranked by",
      `${app.managed_source_labels.join("; ")} - checked before every write`,
    ]);
  }
  if (app.base_url) rows.push(["Base URL", app.base_url]);
  if (app.token_reference) rows.push(["Token", app.token_reference]);
  if (app.token_env_var) {
    rows.push([
      app.token_env_var,
      probe.token_env_present
        ? "exported where the server can see it"
        : "not exported yet -- export it before starting the app",
    ]);
  }
  if (app.open_command) rows.push(["Open with", app.open_command]);

  rows.forEach(([label, value]) => {
    const term = document.createElement("dt");
    term.textContent = label;
    const definition = document.createElement("dd");
    definition.textContent = value;
    meta.append(term, definition);
  });

  const link = document.createElement("dt");
  link.textContent = "Documented at";
  const anchor = document.createElement("dd");
  const href = document.createElement("a");
  href.href = app.doc_url;
  href.target = "_blank";
  href.rel = "noreferrer noopener";
  href.textContent = app.doc_url;
  anchor.append(href);
  meta.append(link, anchor);

  return meta;
}

function desktopAppNotes(app) {
  const wrapper = document.createElement("div");
  wrapper.className = "desktop-app-notes";
  (app.notes || []).forEach((note) => {
    const line = document.createElement("p");
    line.className = "field-description";
    line.textContent = note;
    wrapper.append(line);
  });
  return wrapper;
}

/** The values a human types into the app's own dialog, each with a copy button.
 *
 * The fallback for the two cases no button can serve: the app's config file
 * does not exist yet, or a managed profile owns it. Where the file is there,
 * Configure writes it and this table is not shown -- a card that offered both
 * at once was the self-contradiction 6.55.0 shipped. */
function desktopInstructionTable(app) {
  const wrapper = document.createElement("div");
  wrapper.className = "desktop-instructions";

  const lead = document.createElement("p");
  lead.className = "field-description";
  lead.textContent =
    app.status === "servable"
      ? "This app has not written its config file yet. Either open it once " +
        "and let it, or enter these in the app's own dialog:"
      : "Enter these in the app's own dialog:";
  wrapper.append(lead);

  (app.instruction_fields || []).forEach((field) => {
    const row = document.createElement("div");
    row.className = "desktop-instruction-row";

    const label = document.createElement("span");
    label.className = "desktop-instruction-label";
    label.textContent = field.label;

    const value = document.createElement("code");
    value.className = "desktop-instruction-value";
    value.textContent = field.value;

    row.append(label, value, desktopCopyButton(field.value));
    wrapper.append(row);
  });
  return wrapper;
}

/** A copy button for one value, feature-detected and quietly optional.
 *
 * 127.0.0.1 over plain http is a secure context under the browser's localhost
 * exception, so the clipboard is expected to work here; a browser that refuses
 * leaves the text selectable, which is what it was before. */
function desktopCopyButton(text) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "guide-copy-button desktop-copy-button";
  button.textContent = "Copy";
  button.setAttribute("aria-label", `Copy ${text}`);
  if (!navigator.clipboard || !navigator.clipboard.writeText) {
    button.disabled = true;
    return button;
  }
  button.addEventListener("click", () => {
    navigator.clipboard
      .writeText(text)
      .then(() => {
        button.textContent = "Copied";
        window.setTimeout(() => {
          button.textContent = "Copy";
        }, 1500);
      })
      .catch(() => {});
  });
  return button;
}

/** Configure, Undo with its mode picker, and the preview the two hang off. */
function desktopAppActions(app) {
  const wrapper = document.createElement("div");
  wrapper.className = "desktop-app-actions";
  const probe = app.probe || {};
  // Every state in which MCC has something on disk to take back out. The
  // two added in 6.84.0 belong here for different reasons: a credential that
  // cannot resolve is a *written* configuration, so Undo has work to do; and
  // keys an application removed still leave MCC's owned file and its restore
  // record behind, which is exactly what Undo cleans up.
  const configured =
    probe.state === "configured" ||
    probe.state === "drifted" ||
    probe.state === "credential_unresolved" ||
    probe.state === "removed_by_app";

  const preview = document.createElement("pre");
  preview.className = "desktop-preview";
  preview.hidden = true;

  const status = document.createElement("p");
  status.className = "desktop-app-status";
  status.setAttribute("role", "status");
  status.textContent = state.desktopAppMessages[app.id] || "";

  // Said once, then forgotten: it describes the write that just happened, not
  // a state of the file, so it must not survive the next reload of the page.
  const say = (message) => {
    state.desktopAppMessages[app.id] = message;
    status.textContent = message;
  };

  const row = document.createElement("div");
  row.className = "desktop-action-row";

  // 7.6.6 removed an "Also set its default model to mcc/best" checkbox from
  // here. It rendered on every servable row that was not Codex, and the only
  // spec whose document has a `model` key is Codex -- which sets it
  // unconditionally, and therefore hid the box. It could not change a byte on
  // any row it appeared on. A control that cannot change anything is not shown.
  const wants = () => ({});

  const previewButton = document.createElement("button");
  previewButton.type = "button";
  previewButton.className = "secondary-button";
  previewButton.dataset.role = "preview";
  previewButton.textContent = "What will this write?";
  previewButton.addEventListener("click", async () => {
    say("");
    try {
      const plan = await api(`/admin/api/desktop-apps/${app.id}/plan`, {
        method: "POST",
        body: JSON.stringify(wants()),
      });
      preview.hidden = false;
      preview.textContent = desktopPlanText(plan);
    } catch (error) {
      say(error.message);
    }
  });

  const configure = document.createElement("button");
  configure.type = "button";
  configure.className = "primary-button";
  configure.dataset.role = "configure";
  configure.textContent = configured ? "Re-apply" : "Configure";
  configure.addEventListener("click", async () => {
    say("");
    try {
      const result = await api(`/admin/api/desktop-apps/${app.id}/configure`, {
        method: "POST",
        body: JSON.stringify(wants()),
      });
      say(
        result.changed
          ? `Wrote ${result.document_path}.` +
            (result.backup_path ? ` Your original is at ${result.backup_path}.` : "")
          : "No change: the file already said this.",
      );
      await loadDesktopApps();
    } catch (error) {
      say(error.message);
    }
  });

  row.append(previewButton, configure);

  if (configured) {
    // Two modes, both offered, and both put back a value MCC replaced -- what
    // separates them is what they refuse. Keys-only never refuses and is the
    // default; restore additionally guarantees the pre-MCC state and declines
    // when the file has been rewritten since, rather than reverting an edit
    // made on purpose.
    const mode = document.createElement("select");
    mode.className = "desktop-undo-mode";
    mode.dataset.role = "undo-mode";
    [
      ["keys_only", "Remove MCC's keys (and put back what it replaced)"],
      ["restore", "Restore the original values exactly"],
    ].forEach(([value, label]) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      if (value === "restore" && !probe.restorable) {
        option.disabled = true;
        option.textContent = `${label} (MCC replaced nothing here)`;
      }
      mode.append(option);
    });

    const undo = document.createElement("button");
    undo.type = "button";
    undo.className = "secondary-button";
    undo.dataset.role = "undo";
    undo.textContent = "Undo";
    undo.addEventListener("click", async () => {
      say("");
      try {
        const result = await api(`/admin/api/desktop-apps/${app.id}/undo`, {
          method: "POST",
          body: JSON.stringify({ mode: mode.value }),
        });
        const restored = (result.restored_keys || []).join(", ");
        say(
          result.changed
            ? `Removed MCC's keys from ${result.document_path}.` +
              (restored ? ` Restored your values at: ${restored}.` : "")
            : "Nothing of MCC's was in that file.",
        );
        await loadDesktopApps();
      } catch (error) {
        say(error.message);
      }
    });
    row.append(mode, undo);
  }

  wrapper.append(row, preview, status);
  return wrapper;
}

/** Render one plan as the text the preview block shows.
 *
 * The diff arrives already masked -- the server never sends a credential to
 * this page -- and the restart line is a plain sentence rather than a state
 * MCC pretends to detect, because for most of these apps the next read is not
 * observable. */
function desktopPlanText(plan) {
  if (plan.no_op) return "Already exactly what MCC would write. Nothing to do.";
  const parts = [];
  parts.push(`--- ${plan.document_path}`);
  parts.push(plan.diff || "(no change to this document)");
  if (plan.sidecar_diff) {
    parts.push(`--- ${plan.sidecar_path}  (a file MCC owns outright)`);
    parts.push(plan.sidecar_diff);
  }
  if ((plan.overwritten_keys || []).length) {
    parts.push(`Replaces existing values at: ${plan.overwritten_keys.join(", ")}`);
  }
  (plan.actions || []).forEach((action) => parts.push(`-> ${action}`));
  return parts.join("\n");
}

/* ------------------------------------------------------------- agent tiers
   Claude Code never names a model: it asks for `claude-sonnet-5` and MCC maps
   that onto whatever MODEL_SONNET points at. Every other coding agent had to
   name a concrete provider/model ref, so moving a route left every agent
   pinned to yesterday's answer. The five tier aliases close that, and this
   section is where one agent is given its own chain for one of them.

   The rails are the *same* component the Model Config page draws
   (`appendRouteRail`), so the grip, the pointer drag, the move buttons, the
   Pause button and the chain editor behave identically on both pages -- a rail
   that reordered differently from the one beside it would read as a bug rather
   than as a distinction. What differs is only where the value goes: these keys
   are not env vars, so they never join `changedValues()` and are written by
   this section's own Save through POST /admin/api/harness-tiers. */

const HARNESS_TIER_KEY_PREFIX = "HARNESS_TIER";

/** The synthetic field key for one (agent, tier, role). Namespaced so it can
 *  never collide with a real setting key, and parseable back to its parts. */
function harnessTierKey(harnessId, tierId, role) {
  return `${HARNESS_TIER_KEY_PREFIX}::${harnessId}::${tierId}::${role}`;
}

function parseHarnessTierKey(key) {
  const parts = String(key || "").split("::");
  if (parts.length !== 4 || parts[0] !== HARNESS_TIER_KEY_PREFIX) return null;
  return { harness: parts[1], tier: parts[2], role: parts[3] };
}

async function loadHarnessTiers() {
  try {
    state.harnessTiers = await api("/admin/api/harness-tiers");
  } catch (_) {
    // The cards are still worth rendering without them: an agent's command,
    // its catalogue path and its request count do not depend on tiers.
    state.harnessTiers = null;
  }
}

/** Forget every rail this section registered, before it draws them again.
 *
 * `state.routeRails` is keyed by chain key and is what a cross-rail drag
 * resolves a drop target through. A rebuilt card leaves its old editors in
 * there pointing at detached DOM, and a drop onto one would write into a rail
 * nobody can see. */
function clearHarnessTierRails() {
  Array.from(state.routeRails.keys()).forEach((key) => {
    if (parseHarnessTierKey(key)) state.routeRails.delete(key);
  });
  Array.from(ROUTE_PAUSE_KEY.keys()).forEach((key) => {
    if (parseHarnessTierKey(key)) ROUTE_PAUSE_KEY.delete(key);
  });
}

function harnessTierField(key, label, type, value) {
  return {
    key,
    label,
    type,
    value: value || "",
    default: "",
    secret: false,
    configured: false,
    locked: false,
  };
}

/** The Tiers block for one agent's card, or null when it has no picker. */
function harnessTiersSection(harness) {
  const payload = state.harnessTiers;
  if (!payload || !harness.catalogue) return null;
  const tiers = Array.isArray(payload.tiers) ? payload.tiers : [];
  const perHarness = (payload.harnesses || {})[harness.id];
  if (!tiers.length || !perHarness) return null;

  const block = document.createElement("details");
  block.className = "agent-tiers";
  block.dataset.harness = harness.id;
  // Open when this agent has said something of its own: a collapsed card would
  // hide the one fact that makes this agent differ from the other twelve.
  block.open = tiers.some((tier) => (perHarness[tier.id] || {}).override);

  const summary = document.createElement("summary");
  summary.className = "agent-tiers-summary";
  summary.textContent = "Tiers";
  summary.append(guideLink("agent_tiers"));
  block.append(summary);

  const intro = document.createElement("p");
  intro.className = "field-description";
  intro.textContent =
    "Five names this agent can pick instead of a provider's model id. Each one " +
    "follows the matching global route until you override it here, and only " +
    "this agent is affected.";
  block.append(intro);

  tiers.forEach((tier) => {
    block.append(harnessTierRow(harness, tier, perHarness[tier.id] || {}));
  });
  return block;
}

function harnessTierRow(harness, tier, entry) {
  const row = document.createElement("div");
  row.className = "agent-tier";
  row.dataset.tier = tier.id;

  const head = document.createElement("div");
  head.className = "agent-tier-head";

  const name = document.createElement("h5");
  name.className = "agent-tier-name";
  name.textContent = tier.label;

  const ref = document.createElement("code");
  ref.className = "agent-tier-ref";
  ref.textContent = tier.ref;

  head.append(name, ref);

  const resolved = entry.resolved || {};
  const globalChain = tier.global || {};
  const overridden = Boolean(entry.override);

  const stateChip = document.createElement("span");
  stateChip.className = `route-state${overridden ? "" : " is-inherited"}`;
  stateChip.textContent = overridden ? "This agent's own chain" : "Same as global";
  head.append(stateChip);
  row.append(head);

  const readout = document.createElement("p");
  readout.className = "agent-tier-readout";
  // The collapse, said out loud. On a default install MODEL_OPUS and friends
  // are unset, so every tier resolves to MODEL -- and a picker showing five
  // entries that are the same model is only confusing if nothing says why.
  readout.textContent = overridden
    ? `Resolves to ${resolved.primary || "nothing yet"}.`
    : `Same as global ${tier.route_label || tier.label} — currently ${
        globalChain.primary || "unset"
      }.${
        tier.inherits_default && tier.env_var !== "MODEL"
          ? ` ${tier.env_var} is unset, so it follows MODEL.`
          : ""
      }`;
  row.append(readout);

  const actions = document.createElement("div");
  actions.className = "agent-tier-actions";

  if (!overridden) {
    const override = document.createElement("button");
    override.type = "button";
    override.className = "secondary-button";
    override.textContent = "Override";
    override.addEventListener("click", () => {
      saveHarnessTier(harness.id, tier.id, {
        override: true,
        // Seeded from what this tier resolves to today, so pressing Override
        // changes nothing until the operator edits the rail. An empty rail
        // would silently move the agent onto MODEL the moment it was saved.
        model: globalChain.primary || "",
        fallbacks: globalChain.fallbacks || [],
        paused: [],
      });
    });
    actions.append(override);
    row.append(actions);
    return row;
  }

  const rail = document.createElement("div");
  rail.className = "route-rail";
  const modelKey = harnessTierKey(harness.id, tier.id, "MODEL");
  const chainKey = harnessTierKey(harness.id, tier.id, "CHAIN");
  const pausedKey = harnessTierKey(harness.id, tier.id, "PAUSED");
  // Registered exactly as a real route's pause list is, so `routePausedRefs`
  // and `syncRoutePauseUi` need no branch at all: they read one field by key.
  ROUTE_PAUSE_KEY.set(modelKey, pausedKey);
  state.fields.set(
    pausedKey,
    harnessTierField(pausedKey, `${tier.label} paused`, "text", (entry.paused || []).join(",")),
  );
  appendRouteRail(
    rail,
    harnessTierField(modelKey, `${harness.display_name} ${tier.label}`, "optional_model", entry.model),
    harnessTierField(
      chainKey,
      `${harness.display_name} ${tier.label} fallbacks`,
      "model_chain",
      (entry.fallbacks || []).join(","),
    ),
  );
  row.append(rail);

  const save = document.createElement("button");
  save.type = "button";
  save.className = "primary-button";
  save.textContent = "Save";
  save.addEventListener("click", () => {
    const editor = state.routeRails.get(chainKey);
    saveHarnessTier(
      harness.id,
      tier.id,
      {
        override: true,
        model: readHarnessTierModel(modelKey),
        fallbacks: editor ? editor.parseValue(editor.input.value) : [],
        paused: routePausedRefs(modelKey),
      },
      save,
    );
  });

  const revert = document.createElement("button");
  revert.type = "button";
  revert.className = "ghost-button";
  revert.textContent = "Revert to global";
  revert.addEventListener("click", () => {
    saveHarnessTier(harness.id, tier.id, { override: false }, revert);
  });

  actions.append(save, revert);
  row.append(actions);
  return row;
}

function readHarnessTierModel(modelKey) {
  const input = document.querySelector(`input[data-key="${modelKey}"]`);
  if (!input) return "";
  const value = input.value.trim();
  // "None" is what an emptied optional_model control writes back into itself;
  // it means unset, not a model called None.
  return value.toLowerCase() === "none" ? "" : value;
}

async function saveHarnessTier(harnessId, tierId, body, button) {
  if (button) button.disabled = true;
  try {
    state.harnessTiers = await api("/admin/api/harness-tiers", {
      method: "POST",
      body: JSON.stringify({ harness: harnessId, tier: tierId, ...body }),
    });
    renderHarnesses();
    showMessage(
      body.override
        ? `Saved the ${tierId} tier for ${harnessId}.`
        : `${harnessId} follows the global ${tierId} route again.`,
      "ok",
    );
  } catch (error) {
    showMessage(error.message, "error");
  } finally {
    if (button) button.disabled = false;
  }
}

/** Write one pause into an agent's own tier entry.
 *
 * A per-agent tier's paused refs live in `harness_tiers.json`, not in an env
 * var, so `/admin/api/config/route-pause` -- which resolves a settings key --
 * cannot express this. Pausing here must also not switch the ref off for every
 * other agent, which is exactly what writing MODEL_HAIKU_PAUSED would do.
 */
async function toggleHarnessTierPause(parsed, modelKey, ref, paused, button) {
  const chainKey = harnessTierKey(parsed.harness, parsed.tier, "CHAIN");
  const editor = state.routeRails.get(chainKey);
  const current = routePausedRefs(modelKey);
  const next = paused
    ? current.includes(ref)
      ? current
      : [...current, ref]
    : current.filter((entry) => entry !== ref);
  await saveHarnessTier(
    parsed.harness,
    parsed.tier,
    {
      override: true,
      model: readHarnessTierModel(modelKey),
      fallbacks: editor ? editor.parseValue(editor.input.value) : [],
      paused: next,
    },
    button,
  );
}

// Every command line this agent answers to, generated server-side from the
// registry: the launcher, its documented arguments, its retired fcc- name and
// the RTK toggles. The list is the reason the page exists -- "what can I
// actually type" used to be answerable only by reading three doc pages that
// disagreed. Each row copies itself, so the answer is one click from useful.
const HARNESS_COMMAND_KIND_LABELS = {
  flag: "argument",
  legacy: "legacy alias",
  rtk: "token optimizer",
};

function harnessCommandList(harness) {
  const list = document.createElement("ul");
  list.className = "agent-command-list";
  const lines = Array.isArray(harness.command_lines) ? harness.command_lines : [];
  lines.forEach((line) => {
    const row = document.createElement("li");
    row.className = "agent-command-row";
    row.dataset.kind = line.kind || "primary";

    const code = document.createElement("code");
    code.className = "agent-command-line";
    code.textContent = line.command;
    row.append(code);

    if (line.help) {
      const help = document.createElement("p");
      help.className = "agent-command-help";
      help.textContent = line.help;
      row.append(help);
    }

    const label = HARNESS_COMMAND_KIND_LABELS[line.kind];
    if (label) {
      const kind = document.createElement("span");
      kind.className = "agent-command-kind";
      kind.textContent = label;
      row.append(kind);
    }

    addCopyButton(row, () => line.command);
    list.append(row);
  });
  return list;
}

/** "Requests (7d)" for one agent.
 *
 * A measured zero is a fact -- the agent is installed and sent nothing -- so
 * it renders as 0. The dash is reserved for the one case where the number
 * does not exist: no request log to count from.
 */
function harnessUsageRow(harness) {
  const usage = state.harnessUsage;
  if (!usage || usage.enabled === false) return ["Requests (7d)", "—"];
  const counts = usage.counts && typeof usage.counts === "object" ? usage.counts : {};
  const count = Number(counts[harness.id] || 0);
  const value = formatAnalyticsNumber(count);
  // Two facts from two different sources, and side by side they read as a
  // contradiction: "Not installed" is a PATH lookup taken just now, and the
  // count is the request log looking back a week. Both can be true -- the
  // agent was uninstalled, or it runs in WSL, or in a container, or from a
  // directory that is not on this process's PATH. Saying so is cheaper than
  // making the reader wonder which number is lying.
  if (count > 0 && harness.installed === false) {
    return [
      "Requests (7d)",
      `${value} - sent before this agent left this machine's PATH, or from ` +
        "another shell, WSL or a container. MCC logs what reaches it; " +
        '"Not installed" is only a PATH lookup here and now.',
    ];
  }
  return ["Requests (7d)", value];
}

function harnessMeta(harness) {
  const meta = document.createElement("dl");
  meta.className = "agent-meta";
  // Both halves of this function render `rows`, and an agent MCC cannot launch
  // can still have sent requests of its own, so the count belongs in both.
  const rows = [["Protocol", harness.protocol_label], harnessUsageRow(harness)];
  const catalogue = harness.catalogue;

  if (harness.available === false) {
    // No catalogue, no launcher, nothing written: the only two facts left are
    // which protocol it would have spoken and that MCC publishes no command.
    rows.push(["Launcher", "None - MCC publishes no command for this agent"]);
    rows.forEach(([label, value]) => {
      const term = document.createElement("dt");
      term.textContent = label;
      const detail = document.createElement("dd");
      detail.textContent = value;
      meta.append(term, detail);
    });
    return meta;
  }

  if (!catalogue) {
    rows.push(["Model list", "Fetched by the agent itself from /v1/models"]);
  } else if (catalogue.delivery === "process_local") {
    rows.push(["Model list", "Registered in-process at launch; no file on disk"]);
  } else {
    const isMerge = catalogue.delivery === "merge";
    rows.push([isMerge ? "Config file" : "Catalogue", catalogue.path]);
    if (!catalogue.exists) {
      rows.push(["Last written", `Never - written on the first ${harness.command}`]);
    } else {
      rows.push(["Last written", catalogue.updated_at || "unknown"]);
      // Cline's providers.json has no per-model array at all, so the number
      // in it counts provider blocks. Reported as "Models: 1" beside twelve
      // agents reporting 140, that read as a defect rather than as Cline's
      // own schema, so the label says what was counted and the note says why.
      rows.push([
        catalogue.model_count_label || "Models",
        String(catalogue.model_count ?? "unknown") +
          (catalogue.model_count_note ? ` - ${catalogue.model_count_note}` : ""),
      ]);
    }
  }

  if (catalogue && catalogue.merged_key) {
    // This agent reads only its own config file, so MCC writes one key into
    // it. Saying which key, and that nothing else is touched, is the whole
    // difference between an edit a user can audit and one they cannot.
    rows.push([
      "Merged key",
      `${catalogue.merged_key} - the only key MCC writes; every other key is ` +
        `left byte-for-byte, and your file is backed up before the first edit`,
    ]);
  }

  if (catalogue && catalogue.config_flag) {
    // Same story as the config variable below, told with the lever this agent
    // actually publishes: MCC owns the document and passes its path on the
    // command line, so the agent's own config file is never edited.
    rows.push([
      "Config flag",
      `${catalogue.config_flag} - passed for this launch only, so your own config file is never edited`,
    ]);
  }

  if (catalogue && catalogue.config_env_var) {
    // The whole zero-clobber story in one row: MCC owns a file of its own and
    // hands the agent its path through the agent's own documented variable.
    rows.push([
      "Config variable",
      `${catalogue.config_env_var} - set for the launched process only, so your own config file is never edited`,
    ]);
  }

  rows.forEach(([label, value]) => {
    const term = document.createElement("dt");
    term.textContent = label;
    const detail = document.createElement("dd");
    detail.textContent = value;
    meta.append(term, detail);
  });

  if (catalogue && catalogue.defaulted_model_count) {
    const term = document.createElement("dt");
    term.textContent = "CLI defaults";
    const detail = document.createElement("dd");
    detail.className = "agent-defaulted";
    detail.textContent =
      `${catalogue.defaulted_model_count} model(s) carry a value ` +
      `${harness.display_name} supplied because no provider published one`;
    meta.append(term, detail);
  } else if (catalogue && catalogue.defaulted_record_in_document === false) {
    // The launcher prints the counts either way -- it reads them from
    // /admin/api/catalogue-models, not from the file -- but the card is built
    // from what is on disk, and for this one agent that is nothing. Saying
    // "0 models" would be a measurement MCC never took.
    const term = document.createElement("dt");
    term.textContent = "CLI defaults";
    const detail = document.createElement("dd");
    detail.className = "agent-defaulted";
    detail.textContent =
      `${harness.display_name} rejects unknown keys in its config, so the ` +
      "record of what it filled in is not written into the file; the launch " +
      "summary on stderr reports it instead";
    meta.append(term, detail);
  }

  if (harness.rtk_agent) {
    const term = document.createElement("dt");
    term.textContent = "Token optimizer";
    const detail = document.createElement("dd");
    detail.textContent = harness.rtk_enabled ? "RTK enabled" : "RTK off";
    meta.append(term, detail);
  }
  return meta;
}

/* --------------------------------------------------------------------- */
/* Claude Code settings file                                               */
/* --------------------------------------------------------------------- */

const CLAUDE_SETTINGS_STATUS_CLASS = {
  unset: "",
  configured: "ok",
  mismatch: "warn",
  unreadable: "error",
};

// The "Choose how you connect" cards each carry a copyable command. Wire the
// copy buttons once: the blocks are static markup, so this runs at startup and
// is idempotent (addCopyButton is only called once per block).
function initClaudeConnectCopyButtons() {
  const blocks = document.querySelectorAll("#view-claude .claude-command-block");
  blocks.forEach((block) => {
    if (block.querySelector(".guide-copy-button")) return;
    addCopyButton(block, () => block.querySelector("code")?.textContent?.trim() || "");
  });
}

function claudeSettingsPathInputValue() {
  return byId("claudeSettingsPath").value.trim();
}

async function loadClaudeSettings(path) {
  const input = byId("claudeSettingsPath");
  if (!input) return;
  const params = path ? `?path=${encodeURIComponent(path)}` : "";
  try {
    state.claudeSettings = await api(`/admin/api/claude-settings${params}`);
    if (!input.value) {
      // Prefer a discovered file already pointing here, then any discovered
      // file, and only then the default path -- which may not exist at all.
      const targets = state.claudeSettings.targets || [];
      const configured = targets.find((target) => target.state === "configured");
      input.value =
        configured?.path || targets[0]?.path || state.claudeSettings.default_path;
    }
  } catch (error) {
    state.claudeSettings = { error: error.message };
  }
  renderClaudeSettings();
}

// The list of settings files this machine actually has, and which world each
// belongs to. On a machine with WSL there are two Claude Code installations and
// two settings.json files; "my setting did not apply" is nearly always the
// other one, so the origin is shown as prominently as the path.
const CC_STATE_LABELS = {
  configured: "Configured for My Claude Code",
  mismatch: "Points somewhere else",
  unset: "Not configured",
  unreadable: "Unreadable",
};

function claudeSelectedTarget(info) {
  const targets = info?.targets || [];
  const selected = claudeSettingsPathInputValue();
  return targets.find((target) => target.path === selected) || null;
}

function renderClaudeSettingsTargets(info) {
  const targetsEl = byId("claudeSettingsTargets");
  if (!targetsEl) return;

  targetsEl.replaceChildren();
  const targets = info?.targets || [];
  const selectedPath = claudeSettingsPathInputValue();

  if (!targets.length) {
    const empty = document.createElement("p");
    empty.className = "claude-settings-empty";
    empty.textContent =
      "No Claude Code settings.json found on this machine. Configure will " +
      "create one at the default location.";
    targetsEl.append(empty);

    const path = document.createElement("p");
    path.className = "claude-target-path";
    path.textContent = info?.default_path || "";
    targetsEl.append(path);
    return;
  }

  const list = document.createElement("ul");
  list.className = "claude-targets";

  targets.forEach((target, index) => {
    const item = document.createElement("li");
    item.className = "claude-target";

    const inputId = `claudeTarget-${index}`;
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "claudeSettingsTarget";
    radio.id = inputId;
    radio.value = target.path;
    radio.checked = target.path === selectedPath;
    radio.addEventListener("change", () => {
      byId("claudeSettingsPath").value = target.path;
      loadClaudeSettings(target.path);
      // Selecting a file here is also what the editor below works on, so the
      // whole page follows one selection rather than two that can disagree.
      loadClaudeConfig(target.path);
    });

    const label = document.createElement("label");
    label.className = "claude-target-body";
    label.htmlFor = inputId;

    const head = document.createElement("span");
    head.className = "claude-target-head";

    const origin = document.createElement("span");
    origin.className = `claude-origin claude-origin-${target.origin}`;
    origin.textContent = target.origin_label;
    head.append(origin);

    if (target.detail && target.detail !== "this machine") {
      const detail = document.createElement("span");
      detail.className = "claude-target-detail";
      detail.textContent = target.detail;
      head.append(detail);
    }

    const state = document.createElement("span");
    state.className = `claude-state claude-state-${target.state}`;
    state.textContent = CC_STATE_LABELS[target.state] || target.state;
    head.append(state);

    const path = document.createElement("span");
    path.className = "claude-target-path";
    path.textContent = target.path;

    label.append(head, path);
    item.append(radio, label);
    if (target.path === selectedPath) item.classList.add("is-selected");
    list.append(item);
  });

  targetsEl.append(list);
}

function renderClaudeSettingsOverrides(status) {
  const overridesEl = byId("claudeSettingsOverrides");
  if (!overridesEl) return;

  overridesEl.innerHTML = "";
  const overrides = status?.overrides || [];
  overrides.forEach((override) => {
    const note = document.createElement("p");
    note.className = "claude-settings-override";
    const variables = override.variables.join(" and ");
    note.textContent =
      `${override.scope === "managed" ? "Enterprise managed settings" : "A higher-precedence settings file"} ` +
      `at ${override.path} set ${variables} and override this file.`;
    overridesEl.appendChild(note);
  });
}

function renderClaudeSettings() {
  const statusEl = byId("claudeSettingsStatus");
  const applyButton = byId("claudeSettingsApplyButton");
  const removeButton = byId("claudeSettingsRemoveButton");
  if (!statusEl || !applyButton || !removeButton) return;

  const info = state.claudeSettings;
  const state_ = info?.status?.state;
  // Remove takes the two proxy keys back out. Offering it on a file that does
  // not have them is a button that can only do nothing, so it is hidden until
  // there is something to remove.
  const hasProxyKeys = state_ === "configured" || state_ === "mismatch";
  applyButton.disabled = state.claudeSettingsBusy;
  removeButton.disabled = state.claudeSettingsBusy || !hasProxyKeys;
  removeButton.hidden = !hasProxyKeys;
  applyButton.textContent =
    state_ === "configured" ? "Reconfigure" : "Configure";

  renderClaudeSettingsTargets(info);

  statusEl.innerHTML = "";
  if (!info) return;

  if (info.error && !info.status) {
    statusEl.className = "claude-settings-status error";
    statusEl.textContent = `Could not read Claude settings: ${info.error}`;
    renderClaudeSettingsOverrides(null);
    return;
  }

  const status = info.status;
  statusEl.className = `claude-settings-status ${CLAUDE_SETTINGS_STATUS_CLASS[status.state] || ""}`.trim();

  const summary = document.createElement("p");
  summary.className = "claude-settings-summary";
  if (status.state === "unset") {
    summary.textContent = "Not configured";
  } else if (status.state === "configured") {
    summary.textContent = "Configured — pointing at this proxy";
  } else if (status.state === "mismatch") {
    const tokenNote = status.auth_token_present
      ? status.auth_token_matches
        ? "the token matches"
        : "the token differs"
      : "no token is set";
    summary.textContent =
      `Points elsewhere — current base URL is ${status.current_base_url || "(none)"}` +
      `, and ${tokenNote}. Configure will overwrite this.`;
  } else if (status.state === "unreadable") {
    summary.textContent = `Cannot read this file: ${status.error || "unknown error"}. ` +
      "Configure will refuse to overwrite it until this is fixed.";
  }
  statusEl.appendChild(summary);

  renderClaudeSettingsOverrides(status);
}

async function applyClaudeSettings() {
  if (state.claudeSettingsBusy) return;
  state.claudeSettingsBusy = true;
  renderClaudeSettings();
  try {
    await api("/admin/api/claude-settings/apply", {
      method: "POST",
      body: JSON.stringify({ path: claudeSettingsPathInputValue() || null }),
    });
    showMessage("Claude Code settings file configured", "ok");
  } catch (error) {
    showMessage(`Could not configure Claude settings: ${error.message}`, "error");
  } finally {
    // Always re-read rather than adopting the write response: that response
    // carries only the status of the file just written, and the page also
    // needs the discovered-file list, whose states this write just changed.
    await loadClaudeSettings(claudeSettingsPathInputValue());
    await loadClaudeConfig(claudeSettingsPathInputValue());
    state.claudeSettingsBusy = false;
    renderClaudeSettings();
  }
}

async function unsetClaudeSettings() {
  if (state.claudeSettingsBusy) return;
  state.claudeSettingsBusy = true;
  renderClaudeSettings();
  try {
    await api("/admin/api/claude-settings/unset", {
      method: "POST",
      body: JSON.stringify({ path: claudeSettingsPathInputValue() || null }),
    });
    showMessage("Claude Code settings file entries removed", "ok");
  } catch (error) {
    showMessage(`Could not remove Claude settings entries: ${error.message}`, "error");
  } finally {
    await loadClaudeSettings(claudeSettingsPathInputValue());
    await loadClaudeConfig(claudeSettingsPathInputValue());
    state.claudeSettingsBusy = false;
    renderClaudeSettings();
  }
}

byId("claudeSettingsApplyButton").addEventListener("click", () => applyClaudeSettings());
byId("claudeSettingsRemoveButton").addEventListener("click", () => unsetClaudeSettings());
byId("claudeSettingsPath").addEventListener("change", (event) => {
  const path = event.currentTarget.value.trim();
  loadClaudeSettings(path);
  loadClaudeConfig(path);
});

// ── The full Claude Code settings editor ────────────────────────────────────
//
// The catalog is generated from the official docs and served by
// /admin/api/claude-config/catalog: 518 entries, each carrying the control it
// wants. Rendering from that rather than a hardcoded form is what keeps this
// page correct as Claude Code ships new settings.
//
// Three control kinds exist because a plain checkbox would be WRONG for them:
//
//   set_or_unset     Read for presence. Writing "0" turns the behaviour ON, so
//                    the off position must delete the key. The backend rewrites
//                    a falsey set into an unset, but the UI says so up front
//                    rather than surprising the reader in the diff.
//   numeric_boolean  FORCE_HYPERLINK parses as a number, so "false" enables it.
//   secret           Never round-trip the masked value back as a write.

// The catalog carries a `group` for every entry: nine sections named for what
// you are configuring, rather than the docs' sixteen mechanical categories,
// several of which hold one or two rows. Grouping lives in the generator so the
// page and docs/CLAUDE-CODE-CONFIG.md can never disagree about where a setting
// is.
const CC_GROUPS = [
  ["model", "Model and reasoning"],
  ["context", "Context and cost"],
  ["permissions", "Permissions and safety"],
  ["tools", "Tools"],
  ["agents", "Agents, skills, and automation"],
  ["mcp", "MCP"],
  ["connection", "Connection and providers"],
  ["interface", "Interface"],
  ["privacy", "Privacy, telemetry, and updates"],
];

const CC_GROUP_TITLES = new Map(CC_GROUPS);
const CC_GROUP_ORDER = new Map(CC_GROUPS.map(([key], index) => [key, index]));

const CC_SECRET_MASK = "********";

// Controls made of several inputs, which therefore cannot be the target of a
// single <label for>.
const CC_GROUP_CONTROLS = new Set([
  "array",
  "toggle",
  "set_or_unset",
  "numeric_boolean",
]);

// Tool names that can start a permission rule, with the shape of what follows.
// Ordered as the docs present them, most-used first.
const CC_RULE_TOOLS = [
  { tool: "Bash", hint: "npm run test *", help: "Command prefix. * matches anything, including spaces." },
  { tool: "PowerShell", hint: "Get-ChildItem *", help: "Same shape as Bash. Aliases are canonicalised." },
  { tool: "Read", hint: "./.env", help: "// absolute, ~/ home, / settings-relative, ./ current directory." },
  { tool: "Edit", hint: "/src/**", help: "Covers every built-in tool that edits files." },
  { tool: "WebFetch", hint: "domain:example.com", help: "Matched against the hostname. *.example.com covers subdomains." },
  { tool: "Agent", hint: "Explore", help: "Names a subagent." },
  { tool: "Cd", hint: "~/code/**", help: "Governs the /cd command, not a model tool." },
  { tool: "WebSearch", hint: "", help: "Bare name matches every use." },
  { tool: "mcp__server", hint: "", help: "One MCP server; add __tool for a single tool." },
];

const CC_RULE_KEYS = new Set([
  "permissions.allow",
  "permissions.ask",
  "permissions.deny",
]);

function ccGroupTitle(group) {
  return CC_GROUP_TITLES.get(group) || group;
}

function ccGroupOrder(group) {
  const index = CC_GROUP_ORDER.get(group);
  return index === undefined ? CC_GROUPS.length : index;
}

// settings.json addresses env vars under an "env" object, so the document and
// the change payloads both use the "env." prefix while the catalog lists the
// bare variable name.
function ccKeyFor(entry) {
  return entry.kind === "env" ? `env.${entry.name}` : entry.name;
}

function ccCurrentValue(key) {
  return state.claudeConfig.values[key];
}

function ccPendingValue(key) {
  return state.claudeConfig.pending.get(key);
}

function ccIsPending(key) {
  return state.claudeConfig.pending.has(key);
}

// The value a control should display: a pending edit if there is one,
// otherwise what the file says.
function ccDisplayValue(key) {
  return ccIsPending(key) ? ccPendingValue(key) : ccCurrentValue(key);
}

function ccSetPending(key, value) {
  const current = ccCurrentValue(key);
  const same =
    JSON.stringify(value === undefined ? null : value) ===
    JSON.stringify(current === undefined ? null : current);
  if (same) {
    state.claudeConfig.pending.delete(key);
  } else {
    state.claudeConfig.pending.set(key, value);
  }
  renderClaudeConfigPending();
}

function ccTruthy(value) {
  if (value === undefined || value === null) return false;
  const text = String(value).trim().toLowerCase();
  return text === "1" || text === "true" || text === "yes" || text === "on";
}

function ccMatches(entry, query) {
  if (!query) return true;
  const haystack = `${entry.name} ${entry.purpose}`.toLowerCase();
  return haystack.includes(query);
}

function ccVisibleEntries() {
  const config = state.claudeConfig;
  const query = config.query.trim().toLowerCase();

  return config.entries.filter((entry) => {
    if (!entry.editable) return false;
    const key = ccKeyFor(entry);
    const configured = ccCurrentValue(key) !== undefined || ccIsPending(key);

    // Search always reaches the whole surface: a name you typed in full should
    // never be hidden by a view filter you forgot was on.
    if (query) return ccMatches(entry, query);
    if (config.configuredOnly && !configured) return false;
    if (!config.showAll && !entry.common && !configured) return false;
    return true;
  });
}

// An array setting is a list of things you add and remove, not a blob of JSON.
// Rendering it as a textarea made the most common edit in the file -- adding
// one permission rule -- an exercise in matching brackets.
function ccCurrentList(key) {
  const value = ccDisplayValue(key);
  return Array.isArray(value) ? value.map(String) : [];
}

function ccWriteList(key, items) {
  ccSetPending(key, items.length ? items : undefined);
}

function ccListRow(key, item, index, items) {
  const row = document.createElement("li");
  row.className = "cc-list-row";

  const text = document.createElement("code");
  text.className = "cc-list-value";
  text.textContent = item;

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "cc-list-remove";
  remove.setAttribute("aria-label", `Remove ${item}`);
  remove.textContent = "×";
  remove.addEventListener("click", () => {
    const next = items.slice();
    next.splice(index, 1);
    ccWriteList(key, next);
    renderClaudeConfig();
  });

  row.append(text, remove);
  return row;
}

// The rule builder. A tool dropdown plus a specifier is enough structure to
// stop the two mistakes the docs warn about: writing a rule for a tool that is
// never consulted for paths, and forgetting that a bare tool name in `deny`
// removes the tool from Claude's context entirely.
function ccRuleBuilder(key, items) {
  const form = document.createElement("div");
  form.className = "cc-rule-builder";

  const toolId = `ccRuleTool-${key.replace(/W/g, "-")}`;
  const specId = `ccRuleSpec-${key.replace(/W/g, "-")}`;

  const toolLabel = document.createElement("label");
  toolLabel.className = "sr-only";
  toolLabel.htmlFor = toolId;
  toolLabel.textContent = "Tool";

  const select = document.createElement("select");
  select.id = toolId;
  CC_RULE_TOOLS.forEach((option) => {
    const node = document.createElement("option");
    node.value = option.tool;
    node.textContent = option.tool;
    select.append(node);
  });

  const specLabel = document.createElement("label");
  specLabel.className = "sr-only";
  specLabel.htmlFor = specId;
  specLabel.textContent = "Specifier";

  const spec = document.createElement("input");
  spec.id = specId;
  spec.type = "text";
  spec.autocomplete = "off";
  spec.spellcheck = false;

  const preview = document.createElement("code");
  preview.className = "cc-rule-preview";

  const help = document.createElement("p");
  help.className = "cc-rule-help";

  const add = document.createElement("button");
  add.type = "button";
  add.className = "secondary-button cc-rule-add";
  add.textContent = "Add";

  const composed = () => {
    const tool = select.value;
    const specifier = spec.value.trim();
    return specifier ? `${tool}(${specifier})` : tool;
  };

  const refresh = () => {
    const option = CC_RULE_TOOLS.find((entry) => entry.tool === select.value);
    spec.placeholder = option?.hint || "(no specifier — matches every use)";
    help.textContent = option?.help || "";
    preview.textContent = composed();
    add.disabled = items.includes(composed());
  };

  select.addEventListener("change", refresh);
  spec.addEventListener("input", refresh);
  add.addEventListener("click", () => {
    const rule = composed();
    if (!rule || items.includes(rule)) return;
    ccWriteList(key, [...items, rule]);
    renderClaudeConfig();
  });

  refresh();

  const controls = document.createElement("div");
  controls.className = "cc-rule-controls";
  controls.append(toolLabel, select, specLabel, spec, add);

  const result = document.createElement("p");
  result.className = "cc-rule-result";
  result.append(document.createTextNode("Adds "), preview);

  form.append(controls, result, help);
  return form;
}

function ccBuildListEditor(entry, key) {
  const wrapper = document.createElement("div");
  wrapper.className = "cc-list-editor";

  const items = ccCurrentList(key);

  if (items.length) {
    const list = document.createElement("ul");
    list.className = "cc-list";
    items.forEach((item, index) => list.append(ccListRow(key, item, index, items)));
    wrapper.append(list);
  } else {
    const empty = document.createElement("p");
    empty.className = "cc-list-empty";
    empty.textContent = "Nothing set.";
    wrapper.append(empty);
  }

  if (CC_RULE_KEYS.has(key)) {
    wrapper.append(ccRuleBuilder(key, items));
    return wrapper;
  }

  const addRow = document.createElement("div");
  addRow.className = "cc-list-add";

  const input = document.createElement("input");
  input.type = "text";
  input.autocomplete = "off";
  input.spellcheck = false;
  input.placeholder = ccPlainText(entry.example) || "Add an entry";
  input.setAttribute("aria-label", `Add an entry to ${entry.name}`);

  const button = document.createElement("button");
  button.type = "button";
  button.className = "secondary-button";
  button.textContent = "Add";

  const commit = () => {
    const value = input.value.trim();
    if (!value || items.includes(value)) return;
    ccWriteList(key, [...items, value]);
    renderClaudeConfig();
  };

  button.addEventListener("click", commit);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      commit();
    }
  });

  addRow.append(input, button);
  wrapper.append(addRow);
  return wrapper;
}

function ccSetFieldError(wrapper, message) {
  let node = wrapper.querySelector(".cc-field-error");
  if (!message) {
    node?.remove();
    return;
  }
  if (!node) {
    node = document.createElement("p");
    node.className = "cc-field-error";
    node.setAttribute("role", "alert");
    wrapper.append(node);
  }
  node.textContent = message;
}

function ccControlId(key) {
  return `ccField-${key.replace(/\W/g, '-')}`;
}

// Every binary setting is three-state, not a checkbox.
//
// A checkbox has two positions and a settings file has three states: the key
// says true, the key says false, or the key is absent and Claude Code uses its
// own default. Unchecking a box used to write `false`, which is a different
// instruction from "I have no opinion" -- and for a setting whose default is
// true, writing false actively changes behaviour the user only meant to stop
// overriding.
//
// The presence-read family is the exception with a reason: Claude Code reads
// only whether those variables exist, so "false" is not a state they can be
// in. Those get two options, and the row's `presence` badge says why.
function ccTriStateOptions(entry) {
  if (entry.control === "set_or_unset") {
    return [
      { id: "on", label: "On", value: "1" },
      { id: "unset", label: "Not set", value: undefined },
    ];
  }

  const onValue = entry.kind === "env" ? "1" : true;
  const offValue = entry.kind === "env" ? "0" : false;
  return [
    { id: "on", label: entry.kind === "env" ? "On" : "True", value: onValue },
    { id: "off", label: entry.kind === "env" ? "Off" : "False", value: offValue },
    { id: "unset", label: "Not set", value: undefined },
  ];
}

function ccTriStateSelection(entry, value) {
  if (value === undefined || value === null || value === "") return "unset";
  // A presence-read variable is on whenever it exists at all, whatever it says.
  if (entry.control === "set_or_unset") return "on";
  if (typeof value === "boolean") return value ? "on" : "off";
  return ccTruthy(value) ? "on" : "off";
}

function ccBuildTriState(entry, key, value, controlId) {
  const group = document.createElement("div");
  group.className = "cc-tristate";
  group.setAttribute("role", "radiogroup");
  group.setAttribute("aria-labelledby", `${controlId}-label`);

  const selected = ccTriStateSelection(entry, value);
  const defaultHint = entry.default ? ` (default ${ccPlainText(entry.default)})` : "";

  ccTriStateOptions(entry).forEach((option) => {
    const optionId = `${controlId}-${option.id}`;

    const input = document.createElement("input");
    input.type = "radio";
    input.name = controlId;
    input.id = optionId;
    input.checked = option.id === selected;
    input.addEventListener("change", () => {
      ccSetPending(key, option.value);
    });

    const label = document.createElement("label");
    label.className = "cc-tristate-option";
    label.htmlFor = optionId;
    label.textContent = option.label;
    if (option.id === "unset") {
      label.title = `Remove the key so Claude Code uses its default${defaultHint}`;
    }

    group.append(input, label);
  });

  return group;
}

function ccBuildControl(entry) {
  const key = ccKeyFor(entry);
  const controlId = ccControlId(key);
  const value = ccDisplayValue(key);
  const wrapper = document.createElement("div");
  wrapper.className = "cc-control";

  if (
    entry.control === "toggle" ||
    entry.control === "set_or_unset" ||
    entry.control === "numeric_boolean"
  ) {
    wrapper.append(ccBuildTriState(entry, key, value, controlId));
    return wrapper;
  }

  if (entry.control === "enum" && entry.values?.length) {
    const select = document.createElement("select");
    select.id = controlId;
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = entry.default
      ? `Default (${ccPlainText(entry.default)})`
      : "Not set";
    select.append(blank);
    entry.values.forEach((option) => {
      const node = document.createElement("option");
      node.value = option;
      node.textContent = option;
      select.append(node);
    });
    // A value the file already holds that upstream no longer documents must
    // still be selectable, or opening the page would silently propose changing it.
    if (value !== undefined && value !== null && !entry.values.includes(String(value))) {
      const custom = document.createElement("option");
      custom.value = String(value);
      custom.textContent = `${value} (not documented)`;
      select.append(custom);
    }
    select.value = value === undefined || value === null ? "" : String(value);
    select.addEventListener("change", () => {
      ccSetPending(key, select.value === "" ? undefined : select.value);
    });
    wrapper.append(select);
    return wrapper;
  }

  if (entry.control === "array") {
    const editor = ccBuildListEditor(entry, key);
    editor.setAttribute("role", "group");
    editor.setAttribute("aria-labelledby", `${controlId}-label`);
    wrapper.append(editor);
    wrapper.classList.add("cc-control-wide");
    return wrapper;
  }

  if (entry.control === "object" || entry.control === "json") {
    const area = document.createElement("textarea");
    area.id = controlId;
    area.rows = 3;
    area.spellcheck = false;
    area.value = value === undefined ? "" : JSON.stringify(value, null, 2);
    area.placeholder = ccPlainText(entry.example) || "JSON";
    area.addEventListener("change", () => {
      const text = area.value.trim();
      if (!text) {
        ccSetPending(key, undefined);
        area.classList.remove("is-invalid");
        area.removeAttribute("aria-invalid");
        ccSetFieldError(wrapper, "");
        return;
      }
      try {
        ccSetPending(key, JSON.parse(text));
        area.classList.remove("is-invalid");
        area.removeAttribute("aria-invalid");
        ccSetFieldError(wrapper, "");
      } catch (error) {
        // Refusing here beats sending malformed JSON and getting a 4xx after
        // the user has already pressed Apply. The message goes next to the
        // field as well as into the toast: an error only at the top of the page
        // is an error the reader has to hunt for.
        area.classList.add("is-invalid");
        area.setAttribute("aria-invalid", "true");
        ccSetFieldError(wrapper, `Not valid JSON: ${error.message}`);
        showMessage(`${entry.name}: not valid JSON`, "error");
      }
    });
    wrapper.append(area);
    return wrapper;
  }

  const input = document.createElement("input");
  input.id = controlId;
  input.type = entry.control === "number" ? "number" : "text";
  input.autocomplete = "off";
  input.spellcheck = false;
  if (entry.control === "secret") {
    input.type = "password";
    input.placeholder = value === CC_SECRET_MASK ? "Set — type to replace" : "Not set";
  } else {
    input.placeholder = entry.default
      ? `Default: ${ccPlainText(entry.default)}`
      : "Not set";
  }
  // A masked secret must not be echoed back as a write: sending "********"
  // would overwrite the real key with eight asterisks.
  input.value =
    value === undefined || value === null || value === CC_SECRET_MASK ? "" : String(value);
  input.addEventListener("change", () => {
    const text = input.value.trim();
    ccSetPending(key, text === "" ? undefined : text);
  });
  wrapper.append(input);
  return wrapper;
}

function ccBuildRow(entry) {
  const key = ccKeyFor(entry);
  const row = document.createElement("div");
  row.className = "cc-row";
  if (ccIsPending(key)) row.classList.add("is-pending");

  const head = document.createElement("div");
  head.className = "cc-row-head";

  // A real <label for> rather than styled text: the setting name is the only
  // thing identifying the control, so a screen reader has to reach it. List and
  // rule editors have no single control to point at, so those are labelled as a
  // group instead -- a <label for> aimed at an id that does not exist is worse
  // than none, because it reads as labelled and is not.
  const singleControl = !CC_GROUP_CONTROLS.has(entry.control);
  const label = document.createElement(singleControl ? "label" : "span");
  label.className = "cc-row-label";
  if (singleControl) {
    label.htmlFor = ccControlId(key);
  } else {
    label.id = `${ccControlId(key)}-label`;
  }

  const name = document.createElement("code");
  name.className = "cc-row-name";
  name.textContent = entry.name;

  label.append(name);
  head.append(label);

  if (entry.kind === "env") {
    const badge = document.createElement("span");
    badge.className = "cc-badge";
    badge.textContent = "env";
    head.append(badge);
  }
  if (entry.managed_only) {
    const badge = document.createElement("span");
    badge.className = "cc-badge cc-badge-warn";
    badge.textContent = "managed only";
    head.append(badge);
  }
  if (entry.control === "set_or_unset") {
    const badge = document.createElement("span");
    badge.className = "cc-badge cc-badge-warn";
    badge.title =
      "Claude Code reads this for presence, so turning it off removes the key entirely.";
    badge.textContent = "presence";
    head.append(badge);
  }

  const purpose = document.createElement("p");
  purpose.className = "cc-row-purpose";
  // Upstream descriptions run to several sentences. Clamped to two lines so a
  // long one cannot push the next control off the screen; the full text is on
  // the title attribute for anyone who wants it.
  purpose.textContent = ccPlainText(entry.purpose);
  purpose.title = purpose.textContent;

  const body = document.createElement("div");
  body.className = "cc-row-body";
  body.append(head, purpose);

  row.append(body, ccBuildControl(entry));
  return row;
}

// The catalog carries the docs' own markdown links and backticks. Rendering
// them raw would be noise, and rendering them as HTML would inject upstream
// markup into the page, so flatten to text.
function ccPlainText(markdown) {
  return String(markdown || "")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/\\([\\`*_[\]])/g, "$1")
    .replace(/`/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function renderClaudeConfig() {
  const host = byId("ccSections");
  if (!host) return;

  const config = state.claudeConfig;
  host.replaceChildren();

  const showAllLabel = byId("ccShowAllLabel");
  if (showAllLabel) {
    showAllLabel.textContent = config.entries.length
      ? `Show all (${config.entries.filter((entry) => entry.editable).length})`
      : "Show all";
  }

  const visible = ccVisibleEntries();
  byId("ccEmpty").hidden = visible.length > 0;

  const grouped = new Map();
  visible.forEach((entry) => {
    const section = entry.group || "interface";
    if (!grouped.has(section)) grouped.set(section, []);
    grouped.get(section).push(entry);
  });

  [...grouped.keys()]
    .sort((left, right) => ccGroupOrder(left) - ccGroupOrder(right))
    .forEach((section) => {
      const block = document.createElement("section");
      block.className = "cc-section";

      const heading = document.createElement("h4");
      heading.className = "cc-section-title";
      heading.textContent = ccGroupTitle(section);
      const count = document.createElement("span");
      count.className = "cc-section-count";
      count.textContent = String(grouped.get(section).length);
      heading.append(count);

      block.append(heading);
      grouped
        .get(section)
        .sort((left, right) => left.name.localeCompare(right.name))
        .forEach((entry) => block.append(ccBuildRow(entry)));
      host.append(block);
    });

  renderClaudeConfigPending();
}

function renderClaudeConfigPending() {
  const config = state.claudeConfig;
  const count = config.pending.size;

  const applyButton = byId("ccApplyButton");
  const discardButton = byId("ccDiscardButton");
  if (applyButton) applyButton.disabled = count === 0 || config.busy;
  if (discardButton) discardButton.disabled = count === 0 || config.busy;

  const bar = byId("ccPendingBar");
  if (bar) {
    bar.hidden = count === 0;
    bar.textContent =
      count === 1 ? "1 pending change" : `${count} pending changes`;
  }

  document.querySelectorAll("#ccSections .cc-row").forEach((row) => {
    const name = row.querySelector(".cc-row-name")?.textContent || "";
    const isEnv = Boolean(row.querySelector(".cc-badge"));
    const key = isEnv && !name.includes(".") ? `env.${name}` : name;
    row.classList.toggle("is-pending", config.pending.has(key));
  });
}

function renderClaudeConfigManagedWarning(overrides) {
  const node = byId("ccManagedWarning");
  if (!node) return;
  if (!overrides?.length) {
    node.hidden = true;
    node.replaceChildren();
    return;
  }
  node.hidden = false;
  node.replaceChildren();
  const intro = document.createElement("p");
  intro.textContent =
    "A managed policy on this machine outranks this file. Editing these keys " +
    "here will not change what Claude Code does:";
  node.append(intro);
  overrides.forEach((override) => {
    const line = document.createElement("p");
    line.className = "cc-managed-line";
    line.textContent = `${override.path} — ${override.keys.join(", ")}`;
    node.append(line);
  });
}

async function loadClaudeConfig(path) {
  const host = byId("ccSections");
  if (!host) return;

  const config = state.claudeConfig;
  const params = path ? `?path=${encodeURIComponent(path)}` : "";

  try {
    if (!config.entries.length) {
      const catalog = await api("/admin/api/claude-config/catalog");
      config.entries = catalog.entries || [];
    }
    const document_ = await api(`/admin/api/claude-config/document${params}`);
    config.values = document_.values || {};
    config.path = document_.path;
    const editingPath = byId("ccEditingPath");
    if (editingPath) editingPath.textContent = `Editing ${document_.path}`;
    config.parsed = document_.parsed;
    config.pending.clear();
    renderClaudeConfigManagedWarning(document_.managed_overrides);
    if (!document_.parsed) {
      showMessage(
        `Claude Code settings file could not be parsed: ${document_.error}`,
        "error",
      );
    }
  } catch (error) {
    showMessage(`Could not load Claude Code settings: ${error.message}`, "error");
    config.values = {};
  }

  renderClaudeConfig();
}

function ccChangePayload() {
  return [...state.claudeConfig.pending.entries()].map(([name, value]) =>
    value === undefined
      ? { name, op: "unset" }
      : { name, op: "set", value },
  );
}

function ccFormatValue(value) {
  if (value === undefined || value === null) return "(not set)";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

function ccRenderReview(plan) {
  const body = byId("ccReviewBody");
  body.replaceChildren();

  byId("ccReviewPath").textContent = plan.path;

  if (!plan.changes.length && !plan.rejected.length) {
    const empty = document.createElement("p");
    empty.textContent = "Nothing would change.";
    body.append(empty);
    return;
  }

  plan.changes
    .filter((change) => !change.noop)
    .forEach((change) => {
      const row = document.createElement("div");
      row.className = `cc-diff cc-diff-${change.op}`;

      const name = document.createElement("code");
      name.className = "cc-diff-name";
      name.textContent = change.name;

      const detail = document.createElement("span");
      detail.className = "cc-diff-detail";
      detail.textContent =
        change.op === "unset"
          ? `${ccFormatValue(change.before)} → removed`
          : `${ccFormatValue(change.before)} → ${ccFormatValue(change.after)}`;

      row.append(name, detail);

      // The backend warns when it has to rewrite a falsey set into a removal.
      // When the control got there first there is no warning to show, but this
      // is the moment the reader is deciding, so explain the removal here too.
      const entry = state.claudeConfig.entries.find(
        (candidate) => ccKeyFor(candidate) === change.name,
      );
      const notes = [...change.warnings];
      if (
        entry?.control === "set_or_unset" &&
        change.op === "unset" &&
        !notes.length
      ) {
        notes.push(
          "Claude Code reads this variable for presence, so writing 0 would " +
            "leave it enabled. Turning it off removes the key.",
        );
      }

      notes.forEach((warning) => {
        const note = document.createElement("p");
        note.className = "cc-diff-warning";
        note.textContent = warning;
        row.append(note);
      });

      body.append(row);
    });

  plan.rejected.forEach((rejection) => {
    const row = document.createElement("div");
    row.className = "cc-diff cc-diff-rejected";
    const name = document.createElement("code");
    name.className = "cc-diff-name";
    name.textContent = rejection.name;
    const detail = document.createElement("span");
    detail.className = "cc-diff-detail";
    detail.textContent = `not applied — ${rejection.reason}`;
    row.append(name, detail);
    body.append(row);
  });

  const backup = document.createElement("p");
  backup.className = "cc-review-backup";
  backup.textContent =
    "The current file is copied to a .fcc-backup sibling before the first write.";
  body.append(backup);
}

function ccCloseReview() {
  byId("ccReviewModal").hidden = true;
}

async function ccOpenReview() {
  const config = state.claudeConfig;
  if (!config.pending.size || config.busy) return;

  try {
    const plan = await api("/admin/api/claude-config/plan", {
      method: "POST",
      body: JSON.stringify({
        path: claudeSettingsPathInputValue() || null,
        changes: ccChangePayload(),
      }),
    });
    ccRenderReview(plan);
    byId("ccReviewModal").hidden = false;
  } catch (error) {
    showMessage(`Could not build the change list: ${error.message}`, "error");
  }
}

async function ccApply() {
  const config = state.claudeConfig;
  if (config.busy) return;
  config.busy = true;
  renderClaudeConfigPending();

  try {
    const result = await api("/admin/api/claude-config/apply", {
      method: "POST",
      body: JSON.stringify({
        path: claudeSettingsPathInputValue() || null,
        changes: ccChangePayload(),
      }),
    });
    const applied = result.applied?.length || 0;
    showMessage(
      applied === 1 ? "1 setting written" : `${applied} settings written`,
      "ok",
    );
    ccCloseReview();
    config.pending.clear();
    config.values = result.values || {};
    renderClaudeConfig();
    // The connect panel reads the same file, so its status is stale now.
    await loadClaudeSettings(claudeSettingsPathInputValue());
  } catch (error) {
    showMessage(`Could not write settings: ${error.message}`, "error");
  } finally {
    config.busy = false;
    renderClaudeConfigPending();
  }
}

byId("ccApplyButton").addEventListener("click", () => ccOpenReview());
byId("ccDiscardButton").addEventListener("click", () => {
  state.claudeConfig.pending.clear();
  renderClaudeConfig();
});
byId("ccReviewClose").addEventListener("click", () => ccCloseReview());
byId("ccReviewCancel").addEventListener("click", () => ccCloseReview());
byId("ccReviewConfirm").addEventListener("click", () => ccApply());
byId("ccSearch").addEventListener("input", (event) => {
  state.claudeConfig.query = event.currentTarget.value;
  renderClaudeConfig();
});
byId("ccConfiguredOnly").addEventListener("change", (event) => {
  state.claudeConfig.configuredOnly = event.currentTarget.checked;
  renderClaudeConfig();
});
byId("ccShowAll").addEventListener("change", (event) => {
  state.claudeConfig.showAll = event.currentTarget.checked;
  renderClaudeConfig();
});

function downloadJson(filename, value) {
  const blob = new Blob([`${JSON.stringify(value, null, 2)}\n`], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

async function clearWebSearchAnalytics() {
  const total = Number(state.webSearchAnalyticsPage?.total || 0);
  if (
    !window.confirm(
      `Delete the entire web-search log${total ? ` (${total} matching rows shown)` : ""}?`,
    )
  ) {
    return;
  }
  await api("/admin/api/websearch/requests", { method: "DELETE" });
  await loadWebSearchAnalytics();
}

byId("validateButton").addEventListener("click", () => validate(true));
byId("applyButton").addEventListener("click", apply);
byId("webSearchStatsApply").addEventListener("click", () =>
  loadWebSearchAnalytics().catch((error) => showMessage(error.message, "error")),
);
byId("webSearchStatsRefresh").addEventListener("click", () =>
  loadWebSearchAnalytics().catch((error) => showMessage(error.message, "error")),
);
byId("webSearchStatsPeriod").addEventListener("change", () => {
  const period = byId("webSearchStatsPeriod")?.value || "daily";
  state.webSearchStatsPeriod = period;
  persistDashboardState();
  loadWebSearchAnalytics().catch((error) => showMessage(error.message, "error"));
});
byId("webSearchFilterQuery").addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  loadWebSearchAnalytics().catch((error) => showMessage(error.message, "error"));
});
byId("webSearchExportButton").addEventListener("click", openExportModal);
byId("webSearchClearButton").addEventListener("click", () =>
  clearWebSearchAnalytics().catch((error) => showMessage(error.message, "error")),
);
byId("webSearchDetailClose").addEventListener("click", closeWebSearchDetail);
byId("webSearchDetailModal").addEventListener("click", (event) => {
  if (event.target === byId("webSearchDetailModal")) closeWebSearchDetail();
});
document.addEventListener("keydown", (event) => {
  trapWebSearchDetailFocus(event);
  if (event.key === "Escape" && !byId("webSearchDetailModal").hidden) {
    closeWebSearchDetail();
  }
});
document.addEventListener("pointerdown", (event) => {
  state.modelComboboxes.forEach((combobox) => {
    if (combobox.isOpen && !combobox.element.contains(event.target)) combobox.close();
  });
});

// Export window.
byId("exportClose").addEventListener("click", closeExportModal);
byId("exportModal").addEventListener("click", (event) => {
  if (event.target === byId("exportModal")) closeExportModal();
});
byId("exportDownloadButton").addEventListener("click", () =>
  runExport().catch((error) => showMessage(error.message, "error")),
);
document.addEventListener("keydown", (event) => {
  trapExportModalFocus(event);
  if (event.key === "Escape" && !byId("exportModal").hidden) {
    closeExportModal();
  }
});
document.querySelectorAll('input[name="exportScope"]').forEach((radio) => {
  radio.addEventListener("change", () => {
    const scope = exportScope();
    renderExportFieldList(scope);
    byId("exportPeriod").value = EXPORT_DEFAULT_PERIOD[scope];
    byId("exportCustomRange").hidden = true;
    syncExportFilterVisibility(scope);
    byId("exportHint").textContent = "";
  });
});
byId("exportPeriod").addEventListener("change", () => {
  byId("exportCustomRange").hidden = byId("exportPeriod").value !== "custom";
});

byId("getStartedDismissButton").addEventListener("click", () => {
  updateOnboarding({ dismissed: true }).catch((error) =>
    showMessage(error.message, "error"),
  );
});

load().catch((error) => {
  showMessage(error.message, "error");
});


/* --------------------------------------------------------------------- */
/* Requests / analytics view                                             */
/* --------------------------------------------------------------------- */

const reqState = {
  offset: 0,
  limit: 25,
  total: 0,
  loadId: 0,
  autoRefreshTimer: null,
  detailReturnFocus: null,
  providerOptions: new Set(),
  modelOptions: new Set(),
  keyOptions: new Set(),
  harnessOptions: new Set(),
  // id -> display name, as the stats payload reports it. The registry lives
  // on the server; the page only ever renders the names it was handed.
  harnessLabels: {},
  // Baseline the pulse poll compares against. Established by the first pulse
  // rather than by a full load: the list query is paged, so its newest visible
  // row is not MAX(ts_epoch) once you are past page 1, and seeding from it
  // would make every later tick look "changed" and reload the whole view.
  lastPulseTotal: null,
  lastPulseTs: null,
  // The filters the baseline above was measured under. A different window or
  // provider has different counts, so comparing across a filter change would
  // report "changed" for something that only moved because the question did.
  lastPulseFilters: null,
  // `null` while a deferred count is in flight, which the pager reads as
  // "counting..." rather than as zero. `hasMore` is what drives Next while it
  // is: the page fetches one row beyond itself to answer that without a count.
  countDeferred: false,
  hasMore: false,
  // The overall TTFT percentiles, which arrive after the page has painted, and
  // the stats payload the cards were last drawn from so they can be redrawn
  // with them.
  ttft: null,
  lastStats: null,
  pageRows: 0,
  lastCaptureBodies: null,
};

function reqWindowSeconds() {
  return Number(byId("reqFilterWindow").value) || 0;
}

function reqFilters() {
  const params = new URLSearchParams();
  const provider = byId("reqFilterProvider").value.trim();
  const model = byId("reqFilterModel").value.trim();
  const key = byId("reqFilterKey").value.trim();
  const harness = byId("reqFilterHarness").value.trim();
  const status = byId("reqFilterStatus").value;
  const search = byId("reqFilterSearch").value.trim();
  const endpoint = byId("reqFilterEndpoint").value.trim();
  const local = byId("reqFilterLocal").value;
  const windowSeconds = byId("reqFilterWindow").value;
  if (provider) params.set("provider", provider);
  if (model) params.set("model", model);
  if (key) params.set("key", key);
  if (harness) params.set("harness", harness);
  if (status) params.set("status", status);
  if (search) params.set("q", search);
  if (endpoint) params.set("endpoint", endpoint);
  // Always sent, including "all": the store's default is "all", so an omitted
  // param and a chosen Show would look the same to the pulse signature.
  if (local) params.set("local", local);
  if (windowSeconds) {
    params.set("since", (Date.now() / 1000 - Number(windowSeconds)).toFixed(0));
  }
  return params;
}

async function loadRequestsView() {
  const loadId = ++reqState.loadId;
  const params = reqFilters();
  let stats;
  let list;
  let lifetime;
  // The cost panel is computed apart from stats -- the stats rollup counts
  // integers over nine dimensions, and a currency in it would mean a versioned
  // rebuild of every bucket on every install -- and it is also by far the
  // slowest thing this page asks for: measured at 9.0 s against 0.11 s for
  // stats, 0.15 s for the list and 0.004 s for lifetime on a 4.5 GB log.
  // Inside the Promise.all that made the whole page wait for it, so opening
  // Analytics cost nine seconds to show numbers that were ready in a tenth of
  // one. The request still starts here, at the same moment as the other three;
  // only the *wait* has moved, to after the page has painted.
  loadRequestCostPanel(loadId, params);
  // Off the paint path for the same reason the cost panel is: it is the one
  // Analytics query that scans `request_attempts`, measured at 3.3 s cold on
  // a 4.5 GB log against 0.11 s for the stats it sits beside.
  loadRequestLatencyPanel(loadId, params);
  // Off the paint path for the same reason as the two above: an exact scan of
  // `requests.ttft_ms`, measured at 0.69-0.99 s over 331,086 rows on a 4.5 GB
  // log against the tenth of a second the rollup-served stats cost.
  loadRequestTtftPanel(loadId, params);
  // A free-text search is the one filter whose *counting* queries cannot use
  // an index: the predicate is substring matching over stored bodies, so the
  // count and the filtered stats both decompress a body per row. Measured on a
  // 4.5 GB log for a term present in real traffic: the 25-row page 0.06 s, the
  // COUNT(*) over the same predicate 383.66 s, and stats(q=) still running at
  // 600 s. Both move off the wait -- exactly as the cost and latency panels
  // above did -- and land when they land. Every other filter is index-served
  // and keeps today's behaviour precisely.
  const deferring = Boolean(params.get("q"));
  reqState.countDeferred = deferring;
  reqState.hasMore = false;
  if (deferring) {
    loadRequestSearchCount(loadId, params);
    loadRequestDeferredStats(loadId, params);
  }
  try {
    [stats, list, lifetime] = await Promise.all([
      deferring
        ? Promise.resolve(reqPlaceholderStats())
        : api(`/admin/api/requests/stats?${params}`),
      api(
        `/admin/api/requests?limit=${reqState.limit}&offset=${reqState.offset}&${params}`,
      ),
      api("/admin/api/requests/lifetime"),
    ]);
  } catch (error) {
    if (loadId !== reqState.loadId) return;
    throw error;
  }
  if (loadId !== reqState.loadId) return;
  if (stats.enabled === false) {
    byId("reqStatsCards").innerHTML = "";
    byId("reqTableBody").innerHTML = "";
    byId("reqProviderBreakdown").innerHTML = "";
    byId("reqHarnessBreakdown").innerHTML = "";
    byId("reqKeyBreakdown").innerHTML = "";
    byId("reqCancelledBreakdown").innerHTML = "";
    byId("reqTopErrors").innerHTML = "";
    byId("reqFallbackRoutes").innerHTML = "";
    byId("reqDivertedRoutes").innerHTML = "";
    byId("reqRetentionNote").hidden = true;
    byId("reqCoverageNote").hidden = true;
    renderRequestLifetime(null);
    clearChart(byId("reqSeriesChart"));
    clearChart(byId("reqModelChart"));
    reqState.total = 0;
    byId("reqBreakdownTruncatedNote").hidden = true;
    renderRequestCost(null);
    renderRequestModelLatency(null);
    byId("reqBodiesIndicator").textContent = "Request log disabled (REQUEST_LOG_ENABLED=false)";
    renderReqPager();
    byId("reqLastUpdated").textContent = "Logging disabled";
    return;
  }
  // From the list payload, not the stats one: both carry the flag, and the
  // list is the payload that is always awaited.
  byId("reqBodiesIndicator").textContent = list.capture_bodies
    ? "Bodies: captured"
    : "Bodies: hashes only (REQUEST_LOG_CAPTURE_BODIES=false)";
  // Set before anything renders: the chips, the filter datalist and the
  // breakdown all read their display names out of this.
  reqState.harnessLabels =
    stats.harness_labels && typeof stats.harness_labels === "object"
      ? stats.harness_labels
      : {};
  // The one name index, resolved by the server from the pools as they are
  // configured now. Every key rendered below reads through it, so a rename is
  // consistent across the table, the modal, the ladder and the breakdown.
  adoptKeyNames(list.key_names || stats.key_names);
  reqState.lastStats = stats;
  renderRequestStatsCards(stats);
  renderRequestRetentionNote(stats);
  renderRequestLifetime(lifetime);
  renderRequestCoverage(stats);
  renderReqSeriesChart(stats.series || []);
  renderReqModelChart(stats.by_model || []);
  populateRequestFilterOptions(stats);
  renderRequestProviderBreakdown(stats.by_provider || []);
  renderRequestHarnessBreakdown(stats.by_harness || []);
  renderRequestKeyBreakdown(stats.by_key || []);
  renderRequestTopErrors(stats.top_errors || []);
  renderRequestUpstreamStatuses(stats.upstream_statuses || []);
  renderRequestFallbackRoutes(stats.fallback_routes || []);
  renderRequestDivertedRoutes(stats.diverted_routes || []);
  renderReqBreakdownTruncatedNote(stats);
  reqState.total = list.total_deferred ? null : list.total || 0;
  reqState.countDeferred = Boolean(list.total_deferred);
  reqState.hasMore = Boolean(list.has_more);
  reqState.pageRows = (list.rows || []).length;
  reqState.lastCaptureBodies = list.capture_bodies;
  renderRequestsTable(list.rows || []);
  renderReqPager();
  byId("reqLastUpdated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
}

/** The shape `renderRequestStatsCards` and friends read, with nothing in it.
 *
 * Used for the one paint where a free-text search has deferred its stats: the
 * page renders its rows immediately and the panels say "counting..." until the
 * real payload lands. `enabled` is true because the log *is* enabled -- the
 * numbers are simply not here yet, which is a different thing from off.
 */
function reqPlaceholderStats() {
  return {
    enabled: true,
    counting: true,
    capture_bodies: reqState.lastCaptureBodies !== false,
    harness_labels: reqState.harnessLabels || {},
    by_provider: [],
    by_model: [],
    by_key: [],
    by_harness: [],
    top_errors: [],
    upstream_statuses: [],
    fallback_routes: [],
    diverted_routes: [],
    series: [],
  };
}

/** Fetch the deferred count and, if the page has not moved on, show it. */
async function loadRequestSearchCount(loadId, params) {
  try {
    const result = await api(`/admin/api/requests/count?${params}`);
    if (loadId !== reqState.loadId) return;
    reqState.total = Number(result.total || 0);
    reqState.countDeferred = false;
    renderReqPager();
  } catch (_error) {
    if (loadId !== reqState.loadId) return;
    reqState.countDeferred = false;
    reqState.total = null;
    renderReqPager();
  }
}

/** The same for the filtered stats a free-text search forces off the rollup. */
async function loadRequestDeferredStats(loadId, params) {
  try {
    const stats = await api(`/admin/api/requests/stats?${params}`);
    if (loadId !== reqState.loadId) return;
    if (stats.enabled === false) return;
    reqState.harnessLabels =
      stats.harness_labels && typeof stats.harness_labels === "object"
        ? stats.harness_labels
        : {};
    adoptKeyNames(stats.key_names);
    reqState.lastStats = stats;
    renderRequestStatsCards(stats);
    renderRequestRetentionNote(stats);
    renderRequestCoverage(stats);
    renderReqSeriesChart(stats.series || []);
    renderReqModelChart(stats.by_model || []);
    populateRequestFilterOptions(stats);
    renderRequestProviderBreakdown(stats.by_provider || []);
    renderRequestHarnessBreakdown(stats.by_harness || []);
    renderRequestKeyBreakdown(stats.by_key || []);
    renderRequestTopErrors(stats.top_errors || []);
    renderRequestUpstreamStatuses(stats.upstream_statuses || []);
    renderRequestFallbackRoutes(stats.fallback_routes || []);
    renderRequestDivertedRoutes(stats.diverted_routes || []);
    renderReqBreakdownTruncatedNote(stats);
  } catch (_error) {
    /* The page already rendered its rows; a failed count is not a failed page. */
  }
}

function populateRequestFilterOptions(stats) {
  // `label` shows the reader the words the table uses while `value` stays the
  // key the filter actually matches on. Synthetic keys ("local:<rule>") are
  // real filter values -- the store resolves them to "no provider, this rule"
  // -- so they belong in the list rather than being hidden from it.
  const populate = (id, rows, known, labelFor) => {
    rows.forEach((row) => known.add(row.key));
    const datalist = byId(id);
    datalist.replaceChildren(
      ...Array.from(known)
        .sort((left, right) => left.localeCompare(right))
        .map((value) => {
          const option = document.createElement("option");
          option.value = value;
          const label = labelFor ? labelFor(value) : "";
          if (label && label !== value) option.label = label;
          return option;
        }),
    );
  };
  populate(
    "reqProviderOptions",
    stats.by_provider || [],
    reqState.providerOptions,
    providerDisplayLabel,
  );
  populate("reqModelOptions", stats.by_model || [], reqState.modelOptions);
  populate("reqKeyOptions", stats.by_key || [], reqState.keyOptions);
  populate(
    "reqHarnessOptions",
    stats.by_harness || [],
    reqState.harnessOptions,
    harnessDisplayLabel,
  );
}

/** Each breakdown (provider/model/key) is capped server-side; surface it when hit. */
function renderReqBreakdownTruncatedNote(stats) {
  const note = byId("reqBreakdownTruncatedNote");
  const truncated = [];
  if (stats.by_provider_truncated) truncated.push("providers");
  if (stats.by_model_truncated) truncated.push("models");
  if (stats.by_key_truncated) truncated.push("keys");
  if (truncated.length === 0) {
    note.hidden = true;
    note.textContent = "";
    return;
  }
  note.hidden = false;
  note.textContent =
    `Showing the top 50 ${truncated.join(", ")} by request volume; ` +
    "narrow the filters to see the rest.";
}

/** "412 (18.4%)" — the count and its share of the window, in one cell. */
function formatTurnShare(count, total) {
  const value = Number(count || 0);
  const denominator = Number(total || 0);
  if (!denominator) return "—";
  return `${formatAnalyticsNumber(value)} (${((value / denominator) * 100).toFixed(1)}%)`;
}

/** "12 (3.1%)", or an em dash when no row in the window carries route data.
 *
 * Rows written before fallback chains existed have no `route_attempt` at all,
 * and 0% would read as "failover never fires" for traffic we know nothing
 * about. The dash says "not reported" instead, the same distinction the cache
 * columns already make.
 */
function formatFallbackShare(stats) {
  const reported = Number(stats.route_reported || 0);
  if (!reported) return "—";
  const served = Number(stats.served_by_fallback || 0);
  return `${formatAnalyticsNumber(served)} (${((served / reported) * 100).toFixed(1)}%)`;
}

/** Which primary failed, and what covered for it. */
/** Plain wording for the detail panel: which link in the chain answered. */
function formatRouteAttempt(row) {
  const attempt = row.route_attempt;
  if (attempt == null) return null;
  // A diverted request is served by attempt 0 of a chain the vision policy
  // rewrote, so "Primary model" would name the wrong decision entirely.
  const diverted = row.route_diverted_from
    ? `${routeDiversionLabel(row.route_diversion)}, instead of ${row.route_diverted_from}`
    : null;
  if (routeVisionUnavailable(row)) {
    const note = "no model on this route can read the attached image";
    return Number(attempt) === 0
      ? `Primary model (${note})`
      : `Fallback ${attempt} (${note})`;
  }
  if (Number(attempt) === 0) return diverted || "Primary model";
  const fallback = row.route_primary_model
    ? `Fallback ${attempt}, after ${row.route_primary_model}`
    : `Fallback ${attempt}`;
  return diverted ? `${fallback} (${diverted})` : fallback;
}

const ROUTE_DIVERSION_LABELS = {
  vision: "Vision adapter",
  vision_unavailable: "No vision route",
  vision_described: "Vision adapter (described)",
};

/** True when an image arrived and nothing on the route could read it. */
function routeVisionUnavailable(row) {
  return row.route_diversion === "vision_unavailable";
}

function routeDiversionLabel(reason) {
  return ROUTE_DIVERSION_LABELS[reason] || reason;
}

/** The models this request was prepared to try, in order.
 *
 * A chain is only legible as a path. Rendering it as a list with the hop that
 * answered marked shows three things at once that no single field can: what
 * was configured, how far down it had to go, and -- when the head was replaced
 * -- that a policy chose the starting point rather than the route.
 */
function renderRequestRouteTrace(row) {
  const container = byId("reqDetailRoute");
  if (!container) return;
  container.innerHTML = "";
  const chain = (row.route_chain || "")
    .split(",")
    .map((ref) => ref.trim())
    .filter(Boolean);
  // Rows written before route tracing have no chain at all. Inventing a
  // single-hop one from resolved_model would claim the route had no fallbacks
  // configured, which is not something those rows recorded either way.
  if (!chain.length) {
    container.hidden = true;
    return;
  }
  container.hidden = false;

  if (row.route_diverted_from) {
    const note = document.createElement("p");
    note.className = "route-trace-note";
    note.textContent =
      `${routeDiversionLabel(row.route_diversion)}: this route resolved to ` +
      `${row.route_diverted_from}, which cannot read the attached image.`;
    container.appendChild(note);
  } else if (routeVisionUnavailable(row)) {
    const note = document.createElement("p");
    note.className = "route-trace-note route-trace-note-warn";
    note.textContent =
      "No vision route: this request carried an image and no model in this " +
      "chain is known to accept one, so it was sent anyway. Set a Vision " +
      "adapter (MODEL_VISION) on the Model Routing page.";
    container.appendChild(note);
  }

  const served = Number(row.route_attempt ?? 0);
  const list = document.createElement("ol");
  list.className = "route-trace-hops";
  chain.forEach((ref, index) => {
    const hop = document.createElement("li");
    hop.className = "route-trace-hop";
    if (index === served) hop.classList.add("route-trace-served");
    else if (index < served) hop.classList.add("route-trace-failed");
    else hop.classList.add("route-trace-untried");

    const name = document.createElement("code");
    name.textContent = ref;
    hop.appendChild(name);

    const state = document.createElement("span");
    state.className = "route-trace-state";
    if (index === served) state.textContent = "answered";
    else if (index < served) state.textContent = "failed";
    else state.textContent = "not needed";
    hop.appendChild(state);
    list.appendChild(hop);
  });
  container.appendChild(list);
}

function renderRequestDivertedRoutes(rows) {
  const container = byId("reqDivertedRoutes");
  if (!container) return;
  container.innerHTML = "";
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "analytics-empty";
    empty.textContent =
      "No request was diverted to a vision model in this window.";
    container.appendChild(empty);
    return;
  }
  rows.forEach((row) => {
    const item = document.createElement("div");
    item.className = "fallback-route";

    const path = document.createElement("div");
    path.className = "fallback-route-path";
    const from = document.createElement("code");
    from.textContent = row.diverted_from;
    const arrow = document.createElement("span");
    arrow.className = "fallback-route-arrow";
    arrow.setAttribute("aria-label", routeDiversionLabel(row.reason));
    arrow.textContent = "→";
    const to = document.createElement("code");
    to.className = "fallback-route-served";
    to.textContent = row.served_by;
    path.append(from, arrow, to);

    const count = document.createElement("span");
    count.className = "fallback-route-count";
    count.textContent = formatAnalyticsNumber(row.count);

    item.append(path, count);
    container.appendChild(item);
  });
}

function renderRequestFallbackRoutes(rows) {
  const container = byId("reqFallbackRoutes");
  if (!container) return;
  container.innerHTML = "";
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "analytics-empty";
    empty.textContent = "No request fell back to another model in this window.";
    container.appendChild(empty);
    return;
  }
  rows.forEach((row) => {
    const item = document.createElement("div");
    item.className = "fallback-route";

    const path = document.createElement("div");
    path.className = "fallback-route-path";
    const from = document.createElement("code");
    from.textContent = row.primary;
    const arrow = document.createElement("span");
    arrow.className = "fallback-route-arrow";
    arrow.setAttribute("aria-label", "fell back to");
    arrow.textContent = "→";
    const to = document.createElement("code");
    to.className = "fallback-route-served";
    to.textContent = row.served_by;
    path.append(from, arrow, to);

    const count = document.createElement("span");
    count.className = "fallback-route-count";
    count.textContent = formatAnalyticsNumber(row.count);

    item.append(path, count);
    container.appendChild(item);
  });
}

function renderRequestStatsCards(stats) {
  const successRate = stats.total
    ? ((Number(stats.success || 0) / Number(stats.total)) * 100).toFixed(1)
    : "0.0";
  const cards = [
    // Not "Total requests": this counts stored rows, which retention caps, so
    // the label promised something the number could not deliver and read as a
    // counter that resets.
    [
      "Stored requests",
      stats.total,
      atRetentionCap(stats) ? "at the storage cap — older ones deleted" : null,
    ],
    ["Success rate", `${successRate}%`],
    ["Error rate", `${((stats.error_rate || 0) * 100).toFixed(1)}%`],
    ["Served by fallback", formatFallbackShare(stats)],
    // Transparent stream recovery: retries and continuations a provider took
    // without the client ever seeing a seam. A zero is a real measured zero;
    // rows written before these were counted contribute nothing rather than
    // dragging the sums down.
    [
      "Early retries",
      formatAnalyticsNumber(stats.recovery?.early_retries ?? 0),
      "Provider stream recovery, invisible to the client",
    ],
    [
      "Midstream recoveries",
      formatAnalyticsNumber(stats.recovery?.midstream_recoveries ?? 0),
    ],
    ["Salvages", formatAnalyticsNumber(stats.recovery?.salvages ?? 0)],
    // Counted separately from the diversion: a vision-capable primary takes an
    // image without any diversion at all, so "how many had a picture in them"
    // and "how many had to be rerouted" are different questions.
    ["With image input", formatAnalyticsNumber(stats.with_images || 0)],
    [
      "Image, no vision route",
      formatAnalyticsNumber(stats.vision_unavailable || 0),
    ],
    // The picture became text and the route's own model answered. Not a
    // diversion: nothing moved.
    ["Image described", formatAnalyticsNumber(stats.vision_described || 0)],
    [
      "Cancelled",
      stats.cancelled,
      cancelledBreakdownNote(stats.cancelled_breakdown),
    ],
    ["Total input", formatAnalyticsNumber(totalInputTokens(stats))],
    ["Input (uncached)", formatAnalyticsNumber(uncachedInputTokens(stats))],
    ["Cached input", formatAnalyticsNumber(stats.cache_read_tokens || 0)],
    ["Cache hit rate", formatCacheHitRate(stats)],
    ["Cache writes", formatAnalyticsNumber(stats.cache_write_tokens || 0)],
    ["Tokens out", formatAnalyticsNumber(stats.tokens_out || 0)],
    ["Tool calls", formatAnalyticsNumber(stats.tool_calls || 0)],
    ["Turns using tools", formatTurnShare(stats.turns_with_tools, stats.total)],
    ["Turns with reasoning", formatTurnShare(stats.turns_with_reasoning, stats.total)],
    ["Avg duration", stats.avg_duration_ms != null ? `${stats.avg_duration_ms} ms` : "—"],
    ["p50 duration", stats.p50_duration_ms != null ? `${stats.p50_duration_ms} ms` : "—"],
    ["p95 duration", stats.p95_duration_ms != null ? `${stats.p95_duration_ms} ms` : "—"],
    // TTFT percentiles, beside the duration ones they mirror. They arrive from
    // their own request (an exact scan of `requests.ttft_ms`, ~0.8 s on a
    // 4.5 GB log) rather than from `stats`, which is rollup-served in a tenth
    // of that on the unfiltered load. Until it lands they say so; a window
    // whose rows all predate TTFT instrumentation says that instead, because
    // "not measured yet" and "instant" are different answers.
    ["p50 TTFT", ttftPercentileText("p50_ttft_ms")],
    ["p95 TTFT", ttftPercentileText("p95_ttft_ms")],
    // Two averages, because they answer different questions and were one
    // number until now: the first is what clients waited, fallbacks included;
    // the second is what the models that answered actually took. Their gap is
    // the cost of the routing, and it is large -- 8.6 s at route attempt 0
    // against 25.2 s above it on the log this was measured on.
    [
      "Avg TTFT (incl. fallbacks)",
      stats.avg_ttft_ms != null ? `${stats.avg_ttft_ms} ms` : "—",
    ],
    [
      "Avg TTFT (winner)",
      stats.avg_ttft_winner_ms != null ? `${stats.avg_ttft_winner_ms} ms` : "—",
      "the answering model's own first token; measured from 7.4.0 on",
    ],
  ];
  if (stats.counting) {
    // A free-text search defers these numbers, and a zero here would read as a
    // measured zero. Same labels, same order, no answer yet.
    renderStatCards(
      byId("reqStatsCards"),
      cards.map(([label, _value, note]) => [label, "counting…", note]),
    );
    return;
  }
  renderStatCards(byId("reqStatsCards"), cards);
  renderRequestCancelledBreakdown(stats.cancelled_breakdown);
}

/* The one-line version, under the Cancelled card itself, so the number is
 * never on the page without the shape of it. Absent when nothing was
 * cancelled: four zeroes would be noise, not information. */
function cancelledBreakdownNote(breakdown) {
  if (!breakdown || !Number(breakdown.total)) return null;
  const counts = breakdown.counts || {};
  return CANCEL_REASON_ORDER.filter((reason) => Number(counts[reason]))
    .map(
      (reason) =>
        `${CANCEL_REASON_LABELS[reason]} ` +
        formatAnalyticsNumber(Number(counts[reason])),
    )
    .join(" · ");
}

/* The Cancelled card, split into the four things that word covers.
 *
 * Every row is shown, including the ones at zero: "none of these were server
 * restarts" is an answer, and a row that disappears when it reaches zero makes
 * the reader wonder whether it was ever measured. The share is of the
 * cancelled population, not of all traffic, because that is the question the
 * panel is under. */
function renderRequestCancelledBreakdown(breakdown) {
  const container = byId("reqCancelledBreakdown");
  if (!container) return;
  container.innerHTML = "";
  const total = breakdown ? Number(breakdown.total || 0) : 0;
  const counts = (breakdown && breakdown.counts) || {};
  const rows = !breakdown
    ? []
    : CANCEL_REASON_ORDER.map((reason) => {
        const count = Number(counts[reason] || 0);
        return [
          CANCEL_REASON_LABELS[reason],
          formatAnalyticsNumber(count),
          total ? `${((count / total) * 100).toFixed(1)}%` : "—",
          CANCEL_REASON_EXPLANATIONS[reason],
        ];
      });
  container.appendChild(
    analyticsTable(
      ["Why", "Requests", "Share", "What it means"],
      rows,
      "Nothing was cancelled in this range.",
    ),
  );
}

/** The text for one TTFT percentile card: a number, or why there is not one. */
function ttftPercentileText(field) {
  const panel = reqState.ttft;
  if (!panel) return "measuring…";
  if (panel.error) return "—";
  if (panel.enabled === false) return "—";
  if (!panel.measured) return "not measured yet";
  const value = panel[field];
  return value != null ? `${value} ms` : "—";
}

/** Fetch the overall TTFT percentiles and repaint the cards when they land.
 *
 * Fired at the same moment as the cost and latency panels and awaited by
 * nobody, for the same reason: it is an exact scan, and the page must not wait
 * for it. The store caches the answer per filter for 5 s, so a repaint inside
 * that window costs nothing.
 */
async function loadRequestTtftPanel(loadId, params) {
  reqState.ttft = null;
  try {
    const panel = await api(`/admin/api/requests/ttft?${params}`);
    if (loadId !== reqState.loadId) return;
    reqState.ttft = panel;
  } catch (error) {
    if (loadId !== reqState.loadId) return;
    // Not rethrown: this promise is not on the paint path, and an unhandled
    // rejection would be a console error for two cards that can say "—".
    reqState.ttft = { error: error.message };
  }
  if (reqState.lastStats) renderRequestStatsCards(reqState.lastStats);
}

/* Reported and estimated are rendered as two numbers and are never added
   into one. A merged total launders a guess into a fact, and afterwards
   nobody can tell which half was which -- which is exactly the failure the
   whole provenance column exists to prevent. Every sum carries its own
   "N of M priced" denominator for the same reason. */
async function loadRequestCostPanel(loadId, params) {
  // Says what it is doing while it does it, then fills the card it owns. A
  // stale load (the user changed a filter while this was in flight) drops its
  // answer on the floor, the same rule the rest of this view follows.
  const note = byId("reqCostNote");
  note.textContent = "Working out what this traffic cost...";
  let cost;
  try {
    cost = await api(`/admin/api/requests/cost?${params}`);
  } catch (error) {
    // Deliberately not rethrown: this promise is not awaited on the paint
    // path, and an unhandled rejection would be a console error for a card
    // that can say so itself.
    if (loadId !== reqState.loadId) return;
    note.textContent = `The cost breakdown could not be loaded: ${error.message}`;
    return;
  }
  if (loadId !== reqState.loadId) return;
  renderRequestCost(cost);
}

function renderRequestCost(cost) {
  const cards = byId("reqCostCards");
  const note = byId("reqCostNote");
  const panels = [
    ["reqCostProvider", "Provider", "by_provider", providerDisplayLabel],
    ["reqCostModel", "Model", "by_model", (key) => key],
    ["reqCostHarness", "Harness", "by_harness", harnessDisplayLabel],
    ["reqCostDay", "Day", "by_day", (key) => key],
  ];
  if (!cost || cost.enabled === false) {
    cards.innerHTML = "";
    note.textContent = "The request log is off, so nothing is being priced.";
    panels.forEach(([id]) => {
      byId(id).innerHTML = "";
    });
    return;
  }
  const totals = cost.totals || {};
  note.textContent = costPanelNote(cost);
  renderStatCards(cards, [
    [
      "Reported",
      formatCostAmount(totals.reported_usd),
      "what the hosts themselves billed",
    ],
    [
      "Estimated",
      formatCostAmount(totals.estimated_usd),
      "computed from a published price — never added to the reported figure",
    ],
    ["Priced", formatPricedShare(totals.priced, totals.requests)],
  ]);
  panels.forEach(([id, header, field, label]) => {
    const container = byId(id);
    container.innerHTML = "";
    container.appendChild(
      analyticsTable(
        [header, "Reported", "Estimated (est.)", "Priced"],
        (cost[field] || []).map((row) => [
          label(row.key) || row.key || "—",
          formatCostAmount(row.reported_usd),
          formatCostAmount(row.estimated_usd),
          formatPricedShare(row.priced, row.requests),
        ]),
        "Nothing priced in this range.",
      ),
    );
  });
}

/* Why a cost panel is empty, in the panel. "Nothing priced" and "nothing was
   asked to price" are different answers and a blank card says neither. */
function costPanelNote(cost) {
  if (!cost.cost_estimation_enabled) {
    return "Cost estimation is off (COST_ESTIMATION_ENABLED=false). Requests already priced keep their figures.";
  }
  const sources = (cost.by_source || [])
    .map(
      (row) =>
        `${COST_SOURCE_LABELS[row.key] || row.key}: ${formatAnalyticsNumber(Number(row.requests || 0))}`,
    )
    .join(" · ");
  const mode =
    cost.cost_estimation_mode === "auto"
      ? ""
      : ` Mode: ${cost.cost_estimation_mode}.`;
  const litellm = cost.cost_source_litellm_enabled ? "" : " LiteLLM source off.";
  const asOf = costAsOfNote(cost);
  const priced = pricedDenominatorNote(cost);
  return sources
    ? `Priced by — ${sources}.${priced}${mode}${litellm}${asOf}`
    : `Nothing in this range could be priced.${priced}${mode}${litellm}${asOf}`;
}

// Every sum ships with its denominator. The store has carried `priced` beside
// `requests` since costing was added, for exactly this reason -- a window where
// nine models in ten are unpriced looks like a cheap week until you can see how
// much of it was priced at all -- and the header never said it.
function pricedDenominatorNote(cost) {
  const totals = cost.totals || {};
  const requests = Number(totals.requests || 0);
  const priced = Number(totals.priced || 0);
  if (!requests) return "";
  const share = ((priced / requests) * 100).toFixed(1);
  return ` Priced: ${formatAnalyticsNumber(priced)} of ${formatAnalyticsNumber(requests)} requests (${share}%).`;
}

// A stored answer says when it was true. Silence would be the dishonest
// option: the whole point of keeping this payload across a restart is that the
// reader does not wait twelve seconds for it, and the price of that is saying
// out loud that the figures are from a moment ago and a fresh set is coming.
function costAsOfNote(cost) {
  if (!cost || !cost.computed_at) return "";
  const when = new Date(Number(cost.computed_at) * 1000);
  if (Number.isNaN(when.getTime())) return "";
  const clock = when.toLocaleTimeString();
  return cost.stale
    ? ` As of ${clock}, refreshing.`
    : ` As of ${clock}.`;
}

// Prune leaves the count just above the cap between runs, so an exact
// comparison would almost never fire.
function atRetentionCap(stats) {
  const cap = Number(stats.retained_rows_max || 0);
  return cap > 0 && Number(stats.total || 0) >= cap;
}

function renderRequestRetentionNote(stats) {
  const note = byId("reqRetentionNote");
  note.hidden = !atRetentionCap(stats);
  if (note.hidden) return;
  const cap = formatAnalyticsNumber(Number(stats.retained_rows_max || 0));
  note.textContent =
    `Only the most recent ${cap} requests are kept. Older ones have been deleted, ` +
    `so they cannot be listed, opened or searched, and every figure above counts ` +
    `just those ${cap} — it will hover around the cap rather than keep rising. ` +
    `All time below keeps counting for good. To browse more of them, raise ` +
    `REQUEST_LOG_MAX_ROWS: bodies are compressed, so each request now costs about ` +
    `7 KB instead of 41 KB.`;
}

// Binary units, because this is a file on a disk and that is what the operating
// system will tell you it is. NULL rows means "could not be counted", never 0.
function formatLogBytes(bytes) {
  const value = Number(bytes || 0);
  if (!Number.isFinite(value) || value <= 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let index = 0;
  let scaled = value;
  while (scaled >= 1024 && index < units.length - 1) {
    scaled /= 1024;
    index += 1;
  }
  const digits = index === 0 || scaled >= 100 ? 0 : scaled >= 10 ? 1 : 2;
  return `${scaled.toFixed(digits)} ${units[index]}`;
}

function logSizeNote(storage) {
  if (!storage) return "";
  const size = formatLogBytes(storage.bytes);
  const rows = storage.rows == null ? null : Number(storage.rows);
  if (!size && rows == null) return "";
  if (rows == null) return ` · ${size} on disk`;
  return ` · ${formatAnalyticsNumber(rows)} rows kept, ${size} on disk`;
}

function renderRequestLifetime(lifetime) {
  const cards = byId("reqLifetimeCards");
  const span = byId("reqLifetimeSpan");
  if (!lifetime || lifetime.enabled === false) {
    cards.innerHTML = "";
    span.textContent = "";
    byId("reqLifetimeModels").innerHTML = "";
    return;
  }
  const requests = Number(lifetime.requests || 0);
  const period =
    lifetime.first_day && lifetime.last_day
      ? `${lifetime.first_day} to ${lifetime.last_day}`
      : "nothing recorded yet";
  // What the log costs, said out loud. It was invisible, which is how it
  // reached four and a half gigabytes without anybody deciding that was fine.
  // Nothing is capped on the strength of it -- the answer to a large log is
  // fast queries, not a limit nobody asked for -- but an invisible number is
  // one a reader cannot act on.
  span.textContent = `${period}${logSizeNote(lifetime.storage)}`;
  const successRate = requests
    ? ((Number(lifetime.success || 0) / requests) * 100).toFixed(1)
    : "0.0";
  renderStatCards(cards, [
    ["Requests", formatAnalyticsNumber(requests)],
    ["Success rate", `${successRate}%`],
    ["Errors", formatAnalyticsNumber(Number(lifetime.error || 0))],
    ["Total input", formatAnalyticsNumber(totalInputTokens(lifetime))],
    ["Cached input", formatAnalyticsNumber(Number(lifetime.cache_read_tokens || 0))],
    ["Tokens out", formatAnalyticsNumber(Number(lifetime.tokens_out || 0))],
    ["Tool calls", formatAnalyticsNumber(Number(lifetime.tool_calls || 0))],
    ["Served by fallback", formatAnalyticsNumber(Number(lifetime.served_by_fallback || 0))],
    ["Diverted for vision", formatAnalyticsNumber(Number(lifetime.diverted || 0))],
  ]);
  const models = byId("reqLifetimeModels");
  models.innerHTML = "";
  models.appendChild(
    analyticsTable(
      ["Model", "Requests", "Input", "Output", "Errors"],
      (lifetime.by_model || []).map((row) => [
        row.name || "unknown",
        formatAnalyticsNumber(Number(row.requests || 0)),
        formatAnalyticsNumber(Number(row.tokens_in || 0)),
        formatAnalyticsNumber(Number(row.tokens_out || 0)),
        formatAnalyticsNumber(Number(row.error || 0)),
      ]),
      "No requests recorded yet.",
    ),
  );
}

function renderRequestCoverage(stats) {
  const note = byId("reqCoverageNote");
  const coverage = stats.coverage;
  const windowSeconds = reqWindowSeconds();
  if (!coverage || !windowSeconds || coverage.tracking_since == null) {
    note.hidden = true;
    return;
  }
  const windowStart = Date.now() / 1000 - windowSeconds;
  // Before the first recorded session there is no uptime data, so a gap means
  // "not recorded", not "the server was down".
  const measurable = Math.min(
    windowSeconds,
    Math.max(0, Date.now() / 1000 - coverage.tracking_since),
  );
  if (measurable <= 0) {
    note.hidden = true;
    return;
  }
  const missing = measurable - Number(coverage.covered_seconds || 0);
  // A restart leaves a gap of seconds. Reporting that on a 24h range would cry
  // wolf, so scale with the range and keep a floor of two missed heartbeats.
  const threshold = Math.max(
    Number(coverage.heartbeat_seconds || 30) * 2,
    windowSeconds * 0.01,
  );
  note.hidden = false;
  if (missing <= threshold) {
    note.textContent =
      coverage.tracking_since > windowStart
        ? "A server has been running for all of this range since uptime tracking began."
        : "A server was running throughout this range, so quiet periods above are idle time, not downtime.";
    return;
  }
  note.textContent =
    `No server was running for ${formatDurationShort(missing)} of this range, ` +
    "so nothing could be recorded then.";
}

function formatDurationShort(seconds) {
  const total = Math.max(0, Math.round(seconds));
  if (total < 90) return `${total}s`;
  const minutes = Math.round(total / 60);
  if (minutes < 90) return `${minutes}m`;
  const hours = total / 3600;
  return hours < 48 ? `${hours.toFixed(1)}h` : `${(hours / 24).toFixed(1)}d`;
}

function renderStatCards(container, cards) {
  container.innerHTML = "";
  cards.forEach(([label, value, note]) => {
    const card = document.createElement("div");
    card.className = "requests-card";
    const valueEl = document.createElement("strong");
    valueEl.textContent = value;
    const labelEl = document.createElement("span");
    labelEl.textContent = label;
    card.append(valueEl, labelEl);
    if (note) {
      const noteEl = document.createElement("small");
      noteEl.textContent = note;
      card.appendChild(noteEl);
    }
    container.appendChild(card);
  });
}

/* How long each model took to say anything, per outcome.
 *
 * Read off `request_attempts` rather than off the rollup, and that is the
 * whole design decision worth knowing about this panel: a rollup bucket's
 * dimensions come from the `requests` row, and a model that *failed* never
 * reaches one. Summing attempt latency into those buckets would file every
 * failed model's time under the model that rescued the request -- the exact
 * misattribution the panel exists to end.
 *
 * Failed attempts are their own outcome row for the same reason. "Quick when
 * it works and a minute when it does not" is the fact the question is about,
 * and one merged average hides it.
 */
async function loadRequestLatencyPanel(loadId, params) {
  const note = byId("reqModelLatencyNote");
  note.textContent = "Measuring what each model took to say anything...";
  // Only the window travels. The store groups attempts, and an attempt has no
  // provider filter, no key and no status of its own -- the row it belongs to
  // may not exist. Answering a filtered question with unfiltered numbers is
  // the failure this whole feature is about, so the panel says which question
  // it answered instead.
  const query = new URLSearchParams();
  const since = params.get("since");
  if (since) query.set("since", since);
  else query.set("days", "0");
  let latency;
  try {
    latency = await api(`/admin/api/requests/latency?${query}`);
  } catch (error) {
    // Not rethrown: this promise is not awaited on the paint path, and an
    // unhandled rejection would be a console error for a panel that can say
    // so itself.
    if (loadId !== reqState.loadId) return;
    note.textContent = `The model latency breakdown could not be loaded: ${error.message}`;
    return;
  }
  if (loadId !== reqState.loadId) return;
  renderRequestModelLatency(latency);
}

/** p50 as the headline with p95 in the title: one 120 s stall moves a mean by
 *  seconds, and the median is what a request usually feels like. */
function latencyPercentileCell(row) {
  const cell = document.createElement("span");
  cell.className = "req-latency-p50";
  cell.textContent =
    row.p50_ttft_ms == null ? NOT_MEASURED : formatChainDuration(row.p50_ttft_ms);
  cell.title =
    row.p95_ttft_ms == null
      ? "No first-token time was measured for this group."
      : `p95 ${formatChainDuration(row.p95_ttft_ms)} — one attempt in twenty` +
        ` waited at least this long. Median shown; ${row.ttft_measured} of` +
        ` ${row.attempts} attempts carry a measurement.`;
  return cell;
}

/** Group tokens per second, or a dash whenever the two halves disagree.
 *
 * `tokens_out` sums every attempt in the group; `avg_generating_ms` averages
 * only those that carry both a duration and a first-token time. Multiplying
 * one by the other's count when the two sets differ produces a number no
 * model ever achieved, so the rate is refused unless every attempt in the
 * group was measured.
 */
function formatGroupRate(row) {
  const attempts = Number(row.attempts || 0);
  const measured = Number(row.ttft_measured || 0);
  const generating = row.avg_generating_ms;
  if (row.tokens_out == null || generating == null) return NOT_MEASURED;
  if (!attempts || measured !== attempts) return NOT_MEASURED;
  const seconds = (Number(generating) * attempts) / 1000;
  if (seconds <= 0) return NOT_MEASURED;
  return `${(Number(row.tokens_out) / seconds).toFixed(1)} tok/s`;
}

function renderRequestModelLatency(latency) {
  const container = byId("reqModelLatency");
  const note = byId("reqModelLatencyNote");
  container.innerHTML = "";
  if (!latency || latency.enabled === false) {
    note.textContent = "The request log is off, so nothing is being measured.";
    container.appendChild(
      analyticsTable(["Model"], [], "The request log is off."),
    );
    return;
  }
  const rows = latency.rows || [];
  // Each model's own total, so an outcome row can say what share of that
  // model's attempts it was -- which is the failure share, read off the
  // failed row, without a column that means nothing on the other rows.
  const perModel = new Map();
  rows.forEach((row) => {
    const ref = String(row.model_ref);
    perModel.set(ref, (perModel.get(ref) || 0) + Number(row.attempts || 0));
  });
  note.textContent = latencyPanelNote(latency);
  container.appendChild(
    analyticsTable(
      [
        "Model",
        "Outcome",
        "Attempts",
        "Share",
        "TTFT measured",
        "p50 TTFT",
        "Avg first reasoning",
        "Avg generating",
        "Tokens out",
        "tok/s",
      ],
      rows.map((row) => {
        const attempts = Number(row.attempts || 0);
        const total = perModel.get(String(row.model_ref)) || 0;
        return [
          row.model_ref || NOT_MEASURED,
          CHAIN_OUTCOME_LABELS[row.outcome] || row.outcome || NOT_MEASURED,
          formatAnalyticsNumber(attempts),
          total ? `${((attempts / total) * 100).toFixed(1)}%` : "—",
          formatAnalyticsNumber(Number(row.ttft_measured || 0)),
          latencyPercentileCell(row),
          row.avg_first_reasoning_ms == null
            ? NOT_MEASURED
            : formatChainDuration(row.avg_first_reasoning_ms),
          row.avg_generating_ms == null
            ? NOT_MEASURED
            : formatChainDuration(row.avg_generating_ms),
          row.tokens_out == null
            ? NOT_MEASURED
            : formatAnalyticsNumber(Number(row.tokens_out)),
          formatGroupRate(row),
        ];
      }),
      "No attempts in this range.",
    ),
  );
}

/* Why the panel says what it says, in the panel. "Nothing measured" and
   "nothing happened" are different answers and an empty table says neither --
   and on any log that predates 7.4.0 the first one is the true one for every
   row in it, because there is no backfill. */
function latencyPanelNote(latency) {
  const attempts = Number(latency.attempts || 0);
  const measured = Number(latency.ttft_measured || 0);
  const scope =
    "Every route attempt in the window, including the ones whose request row" +
    " names a different model. Not narrowed by the filters above.";
  if (!attempts) {
    return `No route attempts in this range. ${scope}`;
  }
  if (!measured) {
    return (
      `Nothing measured yet: none of the ${formatAnalyticsNumber(attempts)}` +
      " attempts in this range carries a first-token time. Attempts recorded" +
      " before 7.4.0 never had one and cannot be given one; the next requests" +
      ` this server handles will fill this in. ${scope}`
    );
  }
  const source =
    latency.p50_source === "sampled"
      ? " Percentiles are over the newest measured attempts rather than all of" +
        " them, so they are close rather than exact."
      : " Percentiles are exact over the measured attempts.";
  const asOf = latencyAsOfNote(latency);
  return (
    `${formatAnalyticsNumber(measured)} of ${formatAnalyticsNumber(attempts)}` +
    ` attempts carry a first-token time.${source} ${scope}${asOf}`
  );
}

/* The same "as of / refreshing" sentence the cost panel uses, and for the
   same reason: the stored answer is still an answer, and a reader owed a
   number in a tenth of a second should not wait three seconds for one that
   moved by a single attempt. */
function latencyAsOfNote(latency) {
  if (!latency.stale || !latency.computed_at) return "";
  const when = new Date(Number(latency.computed_at) * 1000).toLocaleTimeString();
  return ` Measured at ${when}; refreshing.`;
}

/* Same as the key breakdown: COALESCE-d sums, so a zero here was counted. */
function renderRequestProviderBreakdown(rows) {
  const container = byId("reqProviderBreakdown");
  container.innerHTML = "";
  container.appendChild(
    analyticsTable(
      [
        "Provider",
        "Requests",
        "Error rate",
        "Input (uncached)",
        "Cached input",
        "Cache hit",
        "Tokens out",
        "Avg latency",
      ],
      rows.map((row) => {
        const requests = Number(row.requests || 0);
        const errors = Number(row.errors || 0);
        return [
          providerDisplayLabel(row.key) || UNKNOWN_PROVIDER_KEY,
          formatAnalyticsNumber(requests),
          requests ? `${((errors / requests) * 100).toFixed(1)}%` : "0%",
          formatAnalyticsNumber(uncachedInputTokens(row)),
          formatAnalyticsNumber(Number(row.cache_read_tokens || 0)),
          formatCacheHitRate(row),
          formatAnalyticsNumber(Number(row.tokens_out || 0)),
          row.avg_duration_ms != null ? `${row.avg_duration_ms} ms` : "—",
        ];
      }),
      "No provider activity in this range.",
    ),
  );
}

/** The display name the server published for a harness id, or the id.
 *
 * The registry is the server's: `harness_labels` arrives with every stats
 * payload and names exactly the ids in it, so a new agent shows up here the
 * release it starts sending traffic, with no table to keep in step.
 */
function harnessDisplayLabel(harness) {
  if (!harness) return "";
  return reqState.harnessLabels[harness] || harness;
}

/* Same COALESCE-d sums as the provider breakdown: a zero here was counted. */
function renderRequestHarnessBreakdown(rows) {
  const container = byId("reqHarnessBreakdown");
  container.innerHTML = "";
  container.appendChild(
    analyticsTable(
      ["Harness", "Requests", "Error rate", "Tokens in", "Tokens out", "Avg latency"],
      rows.map((row) => {
        const requests = Number(row.requests || 0);
        const errors = Number(row.errors || 0);
        return [
          harnessDisplayLabel(row.key) || "unknown",
          formatAnalyticsNumber(requests),
          requests ? `${((errors / requests) * 100).toFixed(1)}%` : "0%",
          formatAnalyticsNumber(Number(row.tokens_in || 0)),
          formatAnalyticsNumber(Number(row.tokens_out || 0)),
          row.avg_duration_ms != null ? `${row.avg_duration_ms} ms` : "—",
        ];
      }),
      "No harness activity in this range.",
    ),
  );
}

/* The aggregates below are SQL COALESCE(...,0) sums, so their zeros are
   measured zeros and Number(x || 0) is honest here. avg_duration_ms is the
   one genuinely NULL-able column and uses the dash convention. */
/** The breakdown still groups by masked label; it just reads it out loud. */
function keyBreakdownLabel(key) {
  if (!key) return "unknown";
  const name = keyNameForLabel(key);
  return name ? `${name} (${key})` : key;
}

function renderRequestKeyBreakdown(rows) {
  const container = byId("reqKeyBreakdown");
  container.innerHTML = "";
  container.appendChild(
    analyticsTable(
      [
        "Key",
        "Requests",
        "Error rate",
        "Input (uncached)",
        "Cached input",
        "Cache hit",
        "Tokens out",
        "Avg latency",
      ],
      rows.map((row) => {
        const requests = Number(row.requests || 0);
        const errors = Number(row.errors || 0);
        return [
          keyBreakdownLabel(row.key),
          formatAnalyticsNumber(requests),
          requests ? `${((errors / requests) * 100).toFixed(1)}%` : "0%",
          formatAnalyticsNumber(uncachedInputTokens(row)),
          formatAnalyticsNumber(Number(row.cache_read_tokens || 0)),
          formatCacheHitRate(row),
          formatAnalyticsNumber(Number(row.tokens_out || 0)),
          row.avg_duration_ms != null ? `${row.avg_duration_ms} ms` : "—",
        ];
      }),
      "No per-key data yet.",
    ),
  );
}

function renderRequestTopErrors(rows) {
  const container = byId("reqTopErrors");
  container.innerHTML = "";
  container.appendChild(
    analyticsTable(
      ["Message", "Count"],
      rows.map((row) => [
        row.message || "Unknown error",
        formatAnalyticsNumber(row.count || 0),
      ]),
      "No errors in this range.",
    ),
  );
}

/**
 * Count by the status the upstream actually returned.
 *
 * "Top errors" groups by the one message that survived a whole retry ladder,
 * so a request that met twelve 429s before a 502 is counted once, as the 502.
 * This block counts every try. It is empty on a database whose rows all
 * predate the ladder -- nothing was measured, so nothing is claimed.
 */
function renderRequestUpstreamStatuses(rows) {
  const container = byId("reqUpstreamStatuses");
  if (!container) return;
  container.innerHTML = "";
  container.appendChild(
    analyticsTable(
      ["Status", "Count", "Requests"],
      rows.map((row) => [
        String(row.status),
        formatAnalyticsNumber(row.count || 0),
        formatAnalyticsNumber(row.requests || 0),
      ]),
      "No upstream retries recorded in this range.",
    ),
  );
}

/** The model that answered, flagged when it was not the one the route picked.
 *
 * A fallback that quietly works still changes what answered the request, so a
 * row has to say so -- otherwise a chain looks identical to a healthy primary
 * and nobody learns their first choice is failing.
 */
function buildModelCell(row) {
  const td = document.createElement("td");
  const name = document.createElement("span");
  name.textContent = row.resolved_model || row.requested_model || "";
  td.appendChild(name);
  if (row.route_diverted_from) {
    const badge = document.createElement("span");
    badge.className = "fallback-badge route-badge-diverted";
    badge.textContent = row.route_diversion || "diverted";
    badge.title = `Diverted from ${row.route_diverted_from}`;
    td.appendChild(badge);
  } else if (routeVisionUnavailable(row)) {
    // Nothing was diverted, and that is the finding: the image went to a
    // model documented not to accept one because there was no alternative.
    const badge = document.createElement("span");
    badge.className = "fallback-badge route-badge-blind";
    badge.textContent = "no vision route";
    badge.title =
      "This request carried an image and no model on this route is known to " +
      "accept one. Set a Vision adapter (MODEL_VISION).";
    td.appendChild(badge);
  }
  if (Number(row.route_attempt || 0) > 0) {
    const badge = document.createElement("span");
    badge.className = "fallback-badge";
    badge.textContent = `fallback ${row.route_attempt}`;
    badge.title = row.route_primary_model
      ? `Fell back from ${row.route_primary_model}`
      : "Served by a fallback model";
    td.appendChild(badge);
  }
  return td;
}

/** Which client sent the request, as a chip.
 *
 * The label is the server's display name; the raw id is the fallback so a
 * harness the running server knows about but this stats window did not see
 * still reads as itself rather than as nothing.
 */
function buildHarnessCell(row) {
  const td = document.createElement("td");
  if (!row.harness) {
    td.textContent = "—";
    return td;
  }
  const chip = document.createElement("span");
  chip.className = "harness-chip";
  chip.dataset.harness = row.harness;
  chip.textContent = harnessDisplayLabel(row.harness);
  td.appendChild(chip);
  return td;
}

// The empty-state row has to span the header, and the header is markup this
// file cannot see. Counting it keeps the two from drifting apart the way a
// hardcoded 11 did when the Harness column was added.
function requestTableColumnCount() {
  const headers = document.querySelectorAll(".requests-table thead th");
  return headers.length || 12;
}

/* Short names for the rungs of the pricing ladder, in ladder order. These are
   the values stored in `cost_source`, so they are a wire contract with the
   request log and not a display choice.

   The `_backfill` three are the same three rungs resolved long after the
   request happened, by the one-time historical backfill. They are spelled
   apart because "models.dev priced this when it happened" and "models.dev
   prices it like this today" are different claims, and a reader deciding
   whether to trust a figure is entitled to know which one they are looking at.
   `unpriced` is not a price at all: it is the record that the backfill asked
   and nobody published a rate, which is why the row still shows a dash. */
const COST_SOURCE_LABELS = {
  provider: "the host itself",
  models_dev: "models.dev",
  litellm: "LiteLLM",
  cross_provider: "a cross-provider vote",
  models_dev_backfill: "models.dev, priced later",
  litellm_backfill: "LiteLLM, priced later",
  cross_provider_backfill: "a cross-provider vote, priced later",
  unpriced: "nobody publishes a rate",
};

/* A dash, not a zero. `cost_usd` is NULL when nothing priced the request, and
   "$0.00" is a claim that the request was free -- a claim only a source that
   publishes a zero may make. Three of the five implementations surveyed for
   this feature destroy that distinction, and one of them does it here, at
   render time, despite storing the column correctly. */
function formatCostAmount(value) {
  if (value === null || value === undefined) return "—";
  const amount = Number(value);
  if (!Number.isFinite(amount)) return "—";
  if (amount === 0) return "$0.00";
  if (amount < 0.01) return `$${amount.toFixed(6)}`;
  return `$${amount.toFixed(4)}`;
}

/* "N of M priced". Without it a window where nine models in ten are unpriced
   reads as a cheap week rather than as an incomplete one. */
function formatPricedShare(priced, requests) {
  const total = Number(requests || 0);
  if (!total) return "no requests";
  return `${formatAnalyticsNumber(Number(priced || 0))} of ${formatAnalyticsNumber(total)} priced`;
}

/* The request row's and the modal's cost cell: the amount, plus an "est."
   badge naming the rung for everything below the host's own answer. A
   reported cost carries no badge, because it is not an estimate. */
function buildCostCell(row) {
  const td = document.createElement("td");
  if (row.cost_usd === null || row.cost_usd === undefined) {
    td.className = "cost-unpriced";
    td.textContent = "—";
    td.title = "not priced — no source published a rate for this model";
    return td;
  }
  const amount = document.createElement("span");
  amount.className = "cost-amount";
  amount.textContent = formatCostAmount(row.cost_usd);
  td.appendChild(amount);
  if (row.cost_source && row.cost_source !== "provider") {
    const badge = document.createElement("span");
    badge.className = "cost-badge";
    badge.textContent = "est.";
    badge.title = `estimated from ${COST_SOURCE_LABELS[row.cost_source] || row.cost_source}`;
    td.appendChild(badge);
  }
  return td;
}

/* Below this, the two numbers are the same number: the winner's own first
   token arrives a frame after the request's, and calling a millisecond a
   "fallback loss" would put a `+0 s` on nearly every row. */
const FALLBACK_LOSS_FLOOR_MS = 250;

/** The time this request lost to models that did not answer, or null. */
function fallbackLossMs(row) {
  if (row.ttft_winner_ms == null || row.ttft_ms == null) return null;
  const lost = Number(row.ttft_ms) - Number(row.ttft_winner_ms);
  return lost > FALLBACK_LOSS_FLOOR_MS ? lost : null;
}

/* The request row's TTFT cell: the *winning* model's own first-token time,
   with the time the chain lost to its predecessors as a suffix.

   The cell used to show `ttft_ms`, which is what the client waited --
   fallbacks included -- so a model that answered in 300 ms after two dead
   models was recorded in the list as having taken 4.7 seconds. Measured over
   the real log: mean TTFT is 8.6 s at route attempt 0 and 25.2 s above it, so
   roughly 16.5 s of predecessor time was being charged to whichever model
   rescued the request. Both numbers are still true and both are in the title;
   only which one is the headline has changed.

   A row written before 7.4.0 has no winner time at all and falls back to the
   client-facing number, which is exactly what it showed before. */
function buildTtftCell(row) {
  const td = document.createElement("td");
  td.className = "req-ttft-cell";
  const measured = row.ttft_winner_ms != null;
  const shown = measured ? row.ttft_winner_ms : row.ttft_ms;
  if (shown == null) {
    td.textContent = NOT_MEASURED;
    td.title = "No first-token time was recorded for this request.";
    return td;
  }
  td.appendChild(document.createTextNode(`${Math.round(shown)} ms`));
  const lost = fallbackLossMs(row);
  if (lost != null) {
    const suffix = document.createElement("span");
    suffix.className = "req-ttft-lost";
    suffix.textContent = ` +${(lost / 1000).toFixed(1)} s`;
    td.appendChild(suffix);
  }
  const client = row.ttft_ms == null ? null : `${Math.round(row.ttft_ms)} ms`;
  td.title = measured
    ? [
        `The model that answered took ${Math.round(shown)} ms to its first token.`,
        client === null
          ? ""
          : `The client waited ${client}${
              lost == null
                ? "."
                : `, because ${(lost / 1000).toFixed(1)} s went to models that did not answer.`
            }`,
      ]
        .filter(Boolean)
        .join(" ")
    : "What the client waited, fallbacks included. This request predates" +
      " per-attempt measurement, so the answering model's own time is not known.";
  return td;
}

function renderRequestsTable(rows) {
  const body = byId("reqTableBody");
  body.innerHTML = "";
  if (rows.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = requestTableColumnCount();
    td.className = "analytics-empty";
    td.textContent = "No requests match the current filters.";
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    tr.className = `req-row req-status-${row.status}`;
    const addText = (text) => {
      const td = document.createElement("td");
      td.textContent = text;
      tr.appendChild(td);
    };
    addText(formatRequestTime(row));
    addText(row.endpoint || "");
    // Beside Endpoint: both answer "what came in", before the columns that
    // say what MCC did with it.
    tr.appendChild(buildHarnessCell(row));
    addText(providerDisplayLabel(row.provider, row.optimization));
    // A named key reads as its name; the mask stays in the tooltip so the
    // row can still be matched to a key by sight.
    addKeyReference(tr, row.key_label || "");
    tr.appendChild(buildModelCell(row));
    tr.appendChild(buildStatusCell(row));
    tr.appendChild(buildTurnShapeCell(row));
    addText(`${row.tokens_in ?? "—"}/${row.tokens_out ?? "—"}`);
    tr.appendChild(buildCostCell(row));
    tr.appendChild(buildTtftCell(row));
    addText(row.duration_ms != null ? `${Math.round(row.duration_ms)} ms` : "—");
    const actionCell = document.createElement("td");
    const detailButton = document.createElement("button");
    detailButton.type = "button";
    detailButton.className = "secondary-button req-detail-button";
    detailButton.textContent = "View";
    detailButton.setAttribute("aria-label", `View request ${row.id}`);
    detailButton.addEventListener("click", () => openRequestDetail(row.id));
    actionCell.appendChild(detailButton);
    tr.appendChild(actionCell);
    body.appendChild(tr);
  });
}

/* The status, and -- when it is `cancelled` -- which of the four things that
 * word covers. The status text itself is unchanged and still the sixth cell,
 * so the colour rules that key on it keep working; the chip is added beside
 * it, never instead of it. */
function buildStatusCell(row) {
  const td = document.createElement("td");
  const text = document.createElement("span");
  text.className = "req-status-text";
  text.textContent = row.status || "";
  td.appendChild(text);
  const chip = buildCancelReasonChip(row);
  if (chip) td.appendChild(chip);
  return td;
}

/**
 * Show what the assistant turn actually contained. A row with tools and no
 * reply is the normal shape under Claude Code, and it used to look identical
 * to a row that returned nothing at all.
 */
function buildTurnShapeCell(row) {
  const td = document.createElement("td");
  const wrap = document.createElement("div");
  wrap.className = "turn-chips";
  const chips = [];
  // Listed first: an image is what went *in*, ahead of what came back.
  if (row.input_image_count) {
    chips.push([
      "image",
      row.input_image_count === 1 ? "image" : `${row.input_image_count} images`,
    ]);
  }
  if (row.thinking_chars) chips.push(["thinking", "thinking"]);
  if (row.tool_call_count) {
    chips.push(["tools", row.tool_call_count === 1 ? "1 tool" : `${row.tool_call_count} tools`]);
  }
  if (row.output_chars) chips.push(["response", "reply"]);
  if (chips.length === 0) {
    td.className = "turn-chips-empty";
    td.textContent = "—";
    return td;
  }
  chips.forEach(([kind, label]) => {
    const chip = document.createElement("span");
    chip.className = "turn-chip";
    chip.dataset.kind = kind;
    chip.textContent = label;
    wrap.appendChild(chip);
  });
  td.appendChild(wrap);
  return td;
}

function renderReqPager() {
  // With the count deferred the total is `null`, and Next is driven by the
  // has-more signal the page itself carries (one row fetched beyond the page)
  // rather than by a number that has not arrived. Deriving Next from `total`
  // while it was null is the trap this shape exists to avoid: every Next
  // button on the page would have been disabled for the seconds -- or minutes
  // -- before the count landed.
  const counting = reqState.total === null || reqState.total === undefined;
  const start = !counting && reqState.total === 0 ? 0 : reqState.offset + 1;
  if (counting) {
    const end = reqState.offset + reqState.pageRows;
    byId("reqPageInfo").textContent =
      reqState.pageRows === 0
        ? "counting…"
        : `${start}–${end} of counting…`;
    byId("reqPrevPage").disabled = reqState.offset === 0;
    byId("reqNextPage").disabled = !reqState.hasMore;
    return;
  }
  const end = Math.min(reqState.offset + reqState.limit, reqState.total);
  byId("reqPageInfo").textContent = `${start}–${end} of ${reqState.total}`;
  byId("reqPrevPage").disabled = reqState.offset === 0;
  byId("reqNextPage").disabled = end >= reqState.total;
}

/** Read a design token so the charts stay on the same palette as the UI. */
function token(name, fallback) {
  const value = getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim();
  return value || fallback;
}

/* â”€â”€ Theme switching (brand: Midnight / Paper / High Contrast) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
   Themes are applied by setting [data-theme] on <html>; every color is a
   semantic token, so the whole console re-themes at once. Charts read tokens
   through token(), so re-running their last draw re-themes the canvas too. */
const THEME_KEY = "mcc-theme";
const chartRedrawers = new Map();
function registerChartRedraw(canvasId, fn) {
  chartRedrawers.set(canvasId, fn);
}
function applyTheme(name) {
  if (name !== "paper" && name !== "high-contrast" && name !== "velvet") name = "midnight";
  if (name === "midnight") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = name;
  try { localStorage.setItem(THEME_KEY, name); } catch (_) {}
  document.querySelectorAll(".theme-option").forEach((btn) => {
    btn.setAttribute("aria-checked", String(btn.dataset.themeValue === name));
  });
  chartRedrawers.forEach((fn) => { try { fn(); } catch (_) {} });
}
/* ------------------------------------------------- dashboard state persistence
   The theme is persisted separately above (``mcc-theme``); this remembers the
   active view plus the analytics auto-refresh settings and web-search period
   so an F5 refresh picks the user back up where they were. */
const DASH_STATE_KEY = "mcc-dashboard-state";

function persistDashboardState() {
  let stateToSave;
  try {
    stateToSave = {
      activeView: state.activeView === "get_started" ? undefined : state.activeView,
      autoRefresh: byId("reqAutoRefresh")?.checked ?? undefined,
      autoRefreshInterval: byId("reqAutoRefreshInterval")?.value
        ? String(byId("reqAutoRefreshInterval").value)
        : undefined,
      webSearchStatsPeriod: state.webSearchStatsPeriod || undefined,
      // Analytics filters + page so an F5 refresh continues the same query.
      reqFilters: {
        provider: byId("reqFilterProvider")?.value?.trim() || undefined,
        model: byId("reqFilterModel")?.value?.trim() || undefined,
        key: byId("reqFilterKey")?.value?.trim() || undefined,
        harness: byId("reqFilterHarness")?.value?.trim() || undefined,
        search: byId("reqFilterSearch")?.value?.trim() || undefined,
        status: byId("reqFilterStatus")?.value || undefined,
        endpoint: byId("reqFilterEndpoint")?.value?.trim() || undefined,
        local: byId("reqFilterLocal")?.value || undefined,
        window: byId("reqFilterWindow")?.value || undefined,
        pageSize: byId("reqPageSize")?.value || undefined,
      },
      reqOffset: reqState.offset > 0 ? reqState.offset : undefined,
    };
    localStorage.setItem(DASH_STATE_KEY, JSON.stringify(stateToSave));
  } catch (_) {
    /* storage unavailable or full; persistence is best-effort */
  }
}

function restoreDashboardState() {
  try {
    const raw = localStorage.getItem(DASH_STATE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return {};
    return parsed;
  } catch (_) {
    return {};
  }
}

function initThemeSwitch() {
  let saved = "midnight";
  try { saved = localStorage.getItem(THEME_KEY) || "midnight"; } catch (_) {}
  applyTheme(saved);
  const sw = document.getElementById("themeSwitch");
  if (sw) sw.addEventListener("click", (e) => {
    const btn = e.target.closest(".theme-option");
    if (btn) applyTheme(btn.dataset.themeValue);
  });
}

/**
 * Size a canvas to its rendered box at the display's pixel density.
 *
 * The markup pins width/height attributes, so on any HiDPI screen the bitmap
 * was being stretched and every label came out soft.
 */
function prepareCanvas(canvas) {
  const ratio = window.devicePixelRatio || 1;
  // clientWidth/Height are the content box, so the border is not counted twice.
  const width = canvas.clientWidth || canvas.width;
  const height = canvas.clientHeight || canvas.height;
  if (canvas.width !== width * ratio || canvas.height !== height * ratio) {
    canvas.width = width * ratio;
    canvas.height = height * ratio;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return { ctx, width, height };
}

function compactNumber(value) {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return String(Math.round(value));
}

function drawBarChart(canvas, labels, series) {
  const { ctx, width, height } = prepareCanvas(canvas);
  const padX = 40;
  const padY = 22;
  const max = Math.max(1, ...series.flatMap((s) => s.values));
  const groups = labels.length || 1;
  const groupWidth = (width - padX - 12) / groups;
  const plotHeight = height - padY * 2;
  const colors = [token("--accent", "#10b981"), token("--error", "#ef4444")];
  const muted = token("--muted", "#9ca3af");
  const line = token("--line", "rgba(255,255,255,0.06)");

  // A value scale: the bars were previously unreadable in absolute terms.
  ctx.font = "10px system-ui, sans-serif";
  ctx.textBaseline = "middle";
  [0, 0.5, 1].forEach((fraction) => {
    const y = height - padY - plotHeight * fraction;
    ctx.strokeStyle = line;
    ctx.beginPath();
    ctx.moveTo(padX, y + 0.5);
    ctx.lineTo(width - 8, y + 0.5);
    ctx.stroke();
    ctx.fillStyle = muted;
    ctx.textAlign = "right";
    ctx.fillText(compactNumber(max * fraction), padX - 6, y);
  });

  series.forEach((s, seriesIndex) => {
    ctx.fillStyle = colors[seriesIndex % colors.length];
    s.values.forEach((value, i) => {
      const barWidth = groupWidth / (series.length + 1);
      const x = padX + i * groupWidth + seriesIndex * barWidth;
      const barHeight = (plotHeight * value) / max;
      ctx.fillRect(x, height - padY - barHeight, Math.max(1, barWidth * 0.8), barHeight);
    });
  });

  ctx.fillStyle = muted;
  ctx.textAlign = "left";
  labels.forEach((label, i) => {
    if (labels.length > 12 && i % Math.ceil(labels.length / 12) !== 0) return;
    ctx.fillText(label, padX + i * groupWidth, height - padY / 2);
  });
}

function renderReqSeriesChart(series) {
  const labels = series.map((point) => (point.bucket || "").slice(5));
  const draw = () => drawBarChart(document.getElementById("reqSeriesChart"), labels, [
    { values: series.map((point) => point.requests) },
    { values: series.map((point) => point.errors) },
  ]);
  draw();
  registerChartRedraw("reqSeriesChart", draw);
}

function renderReqModelChart(byModel) {
  const top = byModel.slice(0, 10);
  const canvas = document.getElementById("reqModelChart");
  const { ctx, width, height } = prepareCanvas(canvas);
  if (top.length === 0) return;
  // Total input, not just the uncached slice, or a warm model reads as idle.
  const modelTokens = (m) => totalInputTokens(m) + Number(m.tokens_out || 0);
  const max = Math.max(1, ...top.map(modelTokens));
  const labelWidth = 150;
  const valueWidth = 52;
  const rowHeight = Math.min(20, height / top.length);
  const accent = token("--accent", "#10b981");
  const muted = token("--muted", "#9ca3af");
  ctx.font = "10px system-ui, sans-serif";
  ctx.textBaseline = "middle";
  top.forEach((model, i) => {
    const tokens = modelTokens(model);
    const y = i * rowHeight;
    const mid = y + rowHeight / 2;
    const barWidth = ((width - labelWidth - valueWidth) * tokens) / max;
    ctx.fillStyle = accent;
    ctx.fillRect(labelWidth, y + 2, Math.max(1, barWidth), rowHeight - 5);
    ctx.fillStyle = muted;
    ctx.textAlign = "right";
    ctx.fillText(model.key.slice(0, 26), labelWidth - 8, mid);
    // The bar shows proportion; the number is what people actually quote.
    ctx.textAlign = "left";
    ctx.fillText(compactNumber(tokens), labelWidth + barWidth + 6, mid);
  });
}

/** A version string out of a user-agent, when one is cheaply visible.
 *
 * Every client that names itself does so as `<name>/<version>`, so the first
 * slash-prefixed number is the version. Nothing is parsed beyond that: an
 * agent that reports no version simply shows no version.
 */
function harnessVersionFromUserAgent(userAgent) {
  if (typeof userAgent !== "string") return "";
  const match = userAgent.match(/\/(\d+(?:\.\d+)*)/);
  return match ? match[1] : "";
}

/** "OpenCode 1.18.26 (explicit header)" — who sent it, and how we know.
 *
 * The distinction is the point: an `x-mcc-harness` header is our own launcher
 * stating what it is, while everything else is inference from a user-agent
 * that any client is free to spoof or omit.
 */
function formatHarnessDetail(row) {
  const harness = row.harness;
  if (!harness) return "";
  if (harness === "unknown") return "Unknown (no client identification)";
  const headers = row.headers && typeof row.headers === "object" ? row.headers : {};
  const version = harnessVersionFromUserAgent(headers["user-agent"]);
  const label = harnessDisplayLabel(harness);
  const name = version ? `${label} ${version}` : label;
  return `${name} (${headers["x-mcc-harness"] ? "explicit header" : "from user-agent"})`;
}

async function openRequestDetail(requestId) {
  reqState.detailReturnFocus = document.activeElement;
  const row = await api(`/admin/api/requests/${requestId}`);
  byId("reqDetailTitle").textContent = `Request ${row.id}`;
  const meta = byId("reqDetailMeta");
  meta.innerHTML = "";
  const fields = [
    ["Time", row.ts_iso],
    ["Endpoint", row.endpoint],
    ["Harness", formatHarnessDetail(row)],
    ["Protocol", row.protocol],
    ["Requested model", row.requested_model],
    ["Provider", providerDisplayLabel(row.provider, row.optimization) || null],
    ["Resolved model", row.resolved_model],
    ["Route attempt", formatRouteAttempt(row)],
    ["Vision model", formatVisionModel(row)],
    ["Status", row.status],
    ["Error", row.error_kind ? `${row.error_kind}: ${row.error_message || ""}` : ""],
    ["Key", keyReferenceText(row.key_label)],
    ["Total input", formatAnalyticsNumber(totalInputTokens(row))],
    ["Input (uncached)", formatOptionalNumber(row.tokens_in)],
    ["Cached input", formatOptionalNumber(row.cache_read_tokens)],
    ["Cache writes", formatOptionalNumber(row.cache_write_tokens)],
    ["Cache hit", formatRowCacheHit(row)],
    ["Tokens out", formatOptionalNumber(row.tokens_out)],
    ["+ adapter (in/out)", formatAdapterTokens(row)],
    ["Estimated input", formatEstimatedInput(row)],
    ["Output rate", formatOutputRate(row)],
    // Three numbers where there was one, because one could not tell them
    // apart: what the answering model took, what the client waited, and the
    // difference -- which is the chain's own cost and belongs to neither
    // model. The third row is omitted entirely when there is nothing to say.
    ["TTFT (winner)", formatMilliseconds(row.ttft_winner_ms)],
    ["TTFT (incl. fallbacks)", formatMilliseconds(row.ttft_ms)],
    ["Lost to fallbacks", formatFallbackLoss(row)],
    ["Duration", row.duration_ms != null ? `${Math.round(row.duration_ms)} ms` : "—"],
    ["Turn", formatTurnSummary(row)],
    ["Image input", formatImageSummary(row)],
    ["Reasoning policy", row.reasoning],
    ["Requested reasoning", formatRequestedReasoning(row)],
    ["Reasoning adaptation", formatReasoningAdaptation(row)],
    ["Reasoning sent", formatRequestReasoningEmitted(row)],
    ["Reasoning wire", formatWireVerdict(row)],
    ["Params", row.params ? JSON.stringify(row.params) : ""],
    ["Input SHA-256", row.input_sha256],
    ["Output SHA-256", row.output_sha256],
  ];
  fields.forEach(([label, value]) => {
    if (value == null || value === "") return;
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    meta.append(dt, dd);
  });
  appendCancelReasonDetail(meta, row);
  appendRequestCostDetail(meta, row);
  appendToolCatalogueDetail(meta, row);
  renderRequestRouteTrace(row);
  renderRequestImages(row);
  renderRequestChain(row);
  renderWireRequest(row);
  renderTurnTranscript(row);
  byId("reqDetailModal").hidden = false;
  byId("reqDetailClose").focus();
}

/* Which tools array this request carried (7.40.0). The log keeps one hash per
   request and each distinct array once, so this shows the hash, the count and
   -- behind an expander, because a Claude Code session can carry 200+ tools --
   the names in the order the client sent them. Choosing a name lists the
   requests that carried a tool of that name. Names and hashes only: a tool's
   definition is never shown here.

   A request that carried tools but has no hash predates the recording; it says
   so rather than showing nothing, which would read as "no tools". */
function appendToolCatalogueDetail(meta, row) {
  const catalogue = row.tool_catalogue;
  const sha = row.tool_catalogue_sha;
  const declared = Number((row.params && row.params.tools_count) || 0);
  if (!sha && !declared) return;
  const dt = document.createElement("dt");
  dt.textContent = "Tool catalogue";
  const dd = document.createElement("dd");
  dd.className = "req-tool-catalogue";
  if (!sha) {
    dd.classList.add("req-tool-catalogue-missing");
    dd.textContent = `${formatAnalyticsNumber(declared)} tools, not recorded`;
    const why = document.createElement("span");
    why.className = "req-tool-catalogue-note";
    why.textContent =
      "Which tools a request carried is recorded from 7.40.0 on; this request is older.";
    dd.appendChild(why);
    meta.append(dt, dd);
    return;
  }
  const code = document.createElement("code");
  code.className = "req-tool-catalogue-sha";
  code.textContent = sha.slice(0, 16);
  code.title = `SHA-256 of the tools array: ${sha}`;
  dd.appendChild(code);
  const count = document.createElement("span");
  count.className = "req-tool-catalogue-count";
  const total = catalogue ? catalogue.tool_count : declared;
  count.textContent = `${formatAnalyticsNumber(total)} tool${total === 1 ? "" : "s"}`;
  dd.appendChild(count);
  if (!catalogue) {
    const gone = document.createElement("span");
    gone.className = "req-tool-catalogue-note";
    gone.textContent = "The catalogue behind this hash is no longer stored.";
    dd.appendChild(gone);
    meta.append(dt, dd);
    return;
  }
  if (catalogue.seen) {
    const seen = document.createElement("span");
    seen.className = "req-tool-catalogue-note";
    const first = catalogue.first_seen != null
      ? new Date(Number(catalogue.first_seen) * 1000).toLocaleString()
      : null;
    seen.textContent =
      `This exact array was sent with ${formatAnalyticsNumber(catalogue.seen)} ` +
      `request${catalogue.seen === 1 ? "" : "s"}` +
      (first ? `, first on ${first}.` : ".");
    dd.appendChild(seen);
  }
  const tools = Array.isArray(catalogue.tools) ? catalogue.tools : [];
  if (tools.length) {
    const details = document.createElement("details");
    details.className = "req-tool-catalogue-tools";
    const summary = document.createElement("summary");
    summary.textContent = `Show ${tools.length} tool name${tools.length === 1 ? "" : "s"}`;
    details.appendChild(summary);
    const hint = document.createElement("p");
    hint.className = "req-tool-catalogue-note";
    hint.textContent = "Choose a name to list the requests that carried it.";
    details.appendChild(hint);
    const list = document.createElement("ol");
    list.className = "req-tool-catalogue-list";
    const carriers = document.createElement("div");
    carriers.className = "req-tool-carriers";
    carriers.hidden = true;
    carriers.setAttribute("aria-live", "polite");
    tools.forEach((tool) => {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "req-tool-name";
      button.textContent = tool.name || "(unnamed)";
      button.title = `Definition SHA-256: ${tool.sha}`;
      if (tool.name) {
        button.addEventListener("click", () => {
          loadToolCarriers(tool.name, carriers).catch((error) => {
            carriers.hidden = false;
            carriers.textContent = error.message;
          });
        });
      } else {
        button.disabled = true;
      }
      item.appendChild(button);
      list.appendChild(item);
    });
    details.append(list, carriers);
    dd.appendChild(details);
  }
  meta.append(dt, dd);
}

/* The answer to "which requests carried this tool", drawn under the name list.
   Each row opens that request's own detail. */
async function loadToolCarriers(name, container) {
  container.hidden = false;
  container.textContent = `Finding requests that carried ${name}…`;
  const result = await api(
    `/admin/api/tool-catalogues/requests?name=${encodeURIComponent(name)}&limit=25`,
  );
  container.replaceChildren();
  const heading = document.createElement("p");
  heading.className = "req-tool-carriers-heading";
  const label = document.createElement("strong");
  label.textContent = name;
  heading.append("Requests that carried ", label);
  container.appendChild(heading);
  const rows = Array.isArray(result.rows) ? result.rows : [];
  const summary = document.createElement("p");
  summary.className = "req-tool-catalogue-note";
  if (!result.catalogues) {
    summary.textContent = "No recorded request carried a tool of this name.";
    container.appendChild(summary);
    return;
  }
  const parts = [
    `${formatAnalyticsNumber(result.seen)} request${result.seen === 1 ? "" : "s"}`,
    `${formatAnalyticsNumber(result.catalogues)} tools array${result.catalogues === 1 ? "" : "s"}`,
    `${formatAnalyticsNumber(result.definitions)} definition${result.definitions === 1 ? "" : "s"}`,
  ];
  if (result.first_seen != null && result.last_seen != null) {
    parts.push(
      `${new Date(Number(result.first_seen) * 1000).toLocaleString()} – ` +
        new Date(Number(result.last_seen) * 1000).toLocaleString(),
    );
  }
  summary.textContent = parts.join(" · ");
  container.appendChild(summary);
  const list = document.createElement("ol");
  list.className = "req-tool-carriers-list";
  rows.forEach((entry) => {
    const item = document.createElement("li");
    const open = document.createElement("button");
    open.type = "button";
    open.className = "req-tool-carrier";
    const model = entry.resolved_model || entry.requested_model || "—";
    open.textContent =
      `${formatRequestTime(entry)} · ${model} · ${entry.status || "—"}`;
    open.title = entry.id;
    open.addEventListener("click", () => {
      openRequestDetail(entry.id).catch((error) => showMessage(error.message, "error"));
    });
    item.appendChild(open);
    list.appendChild(item);
  });
  container.appendChild(list);
  if (result.has_more) {
    const more = document.createElement("p");
    more.className = "req-tool-catalogue-note";
    more.textContent = `Showing the newest ${rows.length}.`;
    container.appendChild(more);
  }
}

/* Why this request is `cancelled`, in the words the list chip uses plus one
 * sentence saying what they mean. Appended rather than added to `fields`
 * because it is not plain text: the chip has to be the same element the list
 * draws, or the two surfaces would drift. Absent entirely on every request
 * that was not cancelled -- a successful request was not cancelled for any
 * reason, and an empty row saying so is noise. */
function appendCancelReasonDetail(meta, row) {
  const chip = buildCancelReasonChip(row);
  if (!chip) return;
  const dt = document.createElement("dt");
  dt.textContent = "Cancelled because";
  const dd = document.createElement("dd");
  dd.className = "req-cancel-reason";
  dd.appendChild(chip);
  const sentence = cancelReasonSentence(row);
  if (sentence) {
    const detail = document.createElement("p");
    detail.className = "req-cancel-reason-note";
    detail.textContent = sentence;
    dd.appendChild(detail);
  }
  meta.append(dt, dd);
}

/* Appended after the plain fields because it is the one row that is not
   plain text: an estimate has to say so on the number itself, and it has to
   name which of the four rungs answered. A request nothing priced says "not
   priced" in words rather than showing a zero. */
function appendRequestCostDetail(meta, row) {
  const dt = document.createElement("dt");
  dt.textContent = "Cost";
  const dd = document.createElement("dd");
  dd.className = "cost-detail";
  if (row.cost_usd === null || row.cost_usd === undefined) {
    dd.classList.add("cost-unpriced");
    dd.textContent = "— not priced";
    const why = document.createElement("span");
    why.className = "cost-note";
    why.textContent =
      "No source published a rate for this model, so nothing was stored. " +
      "A zero here would be a claim that the request was free.";
    dd.appendChild(why);
    meta.append(dt, dd);
    return;
  }
  const amount = document.createElement("span");
  amount.className = "cost-amount";
  amount.textContent = formatCostAmount(row.cost_usd);
  dd.appendChild(amount);
  const source = COST_SOURCE_LABELS[row.cost_source] || row.cost_source || "unknown";
  if (row.cost_source === "provider") {
    const note = document.createElement("span");
    note.className = "cost-note";
    note.textContent = `reported by ${source}`;
    dd.appendChild(note);
  } else {
    const badge = document.createElement("span");
    badge.className = "cost-badge";
    badge.textContent = "est.";
    dd.appendChild(badge);
    const note = document.createElement("span");
    note.className = "cost-note";
    note.textContent = `estimated from ${source}`;
    dd.appendChild(note);
  }
  const describes = (row.route_attempts || []).filter(
    (attempt) => attempt && attempt.params && attempt.params.kind === "describe",
  );
  if (describes.length) {
    const caveat = document.createElement("span");
    caveat.className = "cost-note";
    const priced = describes.filter((attempt) => attempt.cost_usd != null);
    const spent = priced.reduce(
      (total, attempt) => total + Number(attempt.cost_usd || 0),
      0,
    );
    caveat.textContent = priced.length
      ? `This is the answering model only. The vision adapter's ${describes.length} describe call(s) cost a further ${formatCostAmount(spent)}, listed per attempt below.`
      : `This is the answering model only. The vision adapter made ${describes.length} describe call(s) that nothing could price.`;
    dd.appendChild(caveat);
  }
  meta.append(dt, dd);
}

// The applied policy lives in row.reasoning; row.requested_reasoning is what
// was asked for before per-model gating. Showing both on every request would
// repeat the same string twice, so the requested row appears only when gating
// actually changed something. Null means the row predates the column: nothing
// is known about the request, so nothing is claimed.
function formatRequestedReasoning(row) {
  const requested = row.requested_reasoning;
  if (requested == null || requested === "") return "";
  if (requested === row.reasoning) return "";
  return requested;
}

// Surface why the applied policy differs from what was asked for. The field
// is NULL on every ungated request and on rows written before it existed --
// we only show the row when gating actually raised a warning, so the request
// log never carries an empty "no warning" line.
function formatReasoningAdaptation(row) {
  const message = row.reasoning_adaptation;
  if (message == null || message === "") return "";
  return message;
}

// Intent and action, side by side. "Reasoning adaptation" is what gating
// decided; this is whether the body that left actually carried a reasoning
// instruction. They diverged silently for ~23,000 requests on a provider whose
// encoder discards the policy, and four investigations chased the wrong layer.
// Read off the attempt that answered, since a fallback may differ from the
// primary. Blank when nothing was measured, so no claim is made about old rows.
function formatRequestReasoningEmitted(row) {
  const answered = answeringAttempt(row);
  // A dash, not an empty string: the field loop drops empties, so "this row
  // predates the column" used to render exactly like "nothing to say" -- i.e.
  // as no row at all. Not measured is a fact and gets a line of its own.
  if (!answered || answered.reasoning_emitted == null) return NOT_MEASURED;
  return answered.reasoning_emitted ? "sent" : "not sent (model default applies)";
}

/* The attempt whose verdict the request row describes: the one that answered,
   or the last one tried when none did. */
function answeringAttempt(row) {
  const attempts = chainAttempts(row);
  return (
    attempts.find((attempt) => attempt.outcome === "succeeded") ||
    attempts[attempts.length - 1]
  );
}

/** True for a row written by a describe call rather than by the route itself.
 *
 * They share the request's attempt table because they are hops on this
 * request, but they are not rungs of its chain: a describe call that answered
 * did not answer the client, so anything asking "which model served this
 * request" has to step over them.
 */
function isDescribeAttempt(attempt) {
  return !!(attempt && attempt.params && attempt.params.kind === "describe");
}

/** The route's own attempts, in order, with the describe hops removed. */
function chainAttempts(row) {
  return (row.route_attempts || []).filter(
    (attempt) => !isDescribeAttempt(attempt),
  );
}

/* The measured half of the pair above, in the modal's own dash convention:
   what the body carried, independent of what gating decided it should. */
function formatWireVerdict(row) {
  const answered = answeringAttempt(row);
  if (!answered || answered.reasoning_emitted == null) return NOT_MEASURED;
  return answered.reasoning_emitted ? "sent" : "not sent";
}

function formatChars(count) {
  if (!count) return "";
  return `${count.toLocaleString()} chars`;
}

/* One convention across every surface: a dash means the number was never
   measured; a zero means it was measured and was zero. The two used to be
   rendered identically in the breakdown tables and distinctly in the modal. */
const NOT_MEASURED = "—";

function formatOptionalNumber(value) {
  return value == null ? NOT_MEASURED : Number(value).toLocaleString();
}

/** Share of this request's input that the provider served from its cache. */
function formatRowCacheHit(row) {
  if (row.cache_read_tokens == null) return "not reported";
  const total = totalInputTokens(row);
  if (!total) return "—";
  return `${((Number(row.cache_read_tokens) / total) * 100).toFixed(1)}%`;
}

function formatMilliseconds(value) {
  return value == null ? NOT_MEASURED : `${Math.round(Number(value))} ms`;
}

/* The time the chain spent on models that did not answer, as a modal row.
   Empty -- so the row is not drawn at all -- when there is no fallback to
   account for, when the two numbers are a frame apart, or when either is
   unmeasured. A "0 ms lost" row on every single-model request would be noise
   in front of the reader on the 86% of requests that never fell back. */
function formatFallbackLoss(row) {
  const lost = fallbackLossMs(row);
  if (lost == null) return "";
  return `${Math.round(lost)} ms (charged to models that did not answer)`;
}

/** The first-token time of the model that actually answered, or null.
 *
 * Falls back to the request's own figure for rows written before per-attempt
 * measurement: on those the two are the same claim, because there is nothing
 * finer to say.
 */
function winnerTtftMs(row) {
  if (row.ttft_winner_ms != null) return Number(row.ttft_winner_ms);
  return row.ttft_ms == null ? null : Number(row.ttft_ms);
}

/** Output tokens per second, excluding the wait before the first one.
 *
 * Measured on the *answering attempt* wherever the log has one, because both
 * halves of the sum have to come from the same model. The old form subtracted
 * the request's TTFT from the request's duration, and on a fallback request
 * that is one model's clock minus another's: the predecessors' stall was
 * taken out of the denominator and the rate came out too high, the worse the
 * fallback the better it looked. Taking the winner's TTFT out of the
 * *request's* duration is the same mistake the other way round -- the
 * predecessors' 4.5 s would stay in the denominator and a model producing
 * 9,900 tok/s would be reported at 1.1.
 *
 * The request-level arithmetic is still the fallback, for rows written before
 * per-attempt measurement, and it uses the winner's TTFT where there is one.
 *
 * Refuses to compute rather than guessing: an unmeasured first token is not a
 * first token at zero, so a row with no TTFT gets a dash where it used to get
 * ``tokens / duration`` presented as a generating rate.
 */
function formatOutputRate(row) {
  const answered = answeringAttempt(row);
  if (answered && answered.outcome === "succeeded" && answered.ttft_ms != null) {
    // The winner's own token count is filled from 7.4.0 on; before that the
    // request's is the only one there is, and on the attempt that answered
    // the client they are the same tokens.
    const rate = formatAttemptRate({
      duration_ms: answered.duration_ms,
      ttft_ms: answered.ttft_ms,
      tokens_out: answered.tokens_out ?? row.tokens_out,
    });
    if (rate !== NOT_MEASURED) return rate;
  }
  const tokens = Number(row.tokens_out || 0);
  const duration = Number(row.duration_ms || 0);
  const ttft = winnerTtftMs(row);
  if (!tokens || !duration || ttft == null) return NOT_MEASURED;
  const generating = duration - ttft;
  if (generating <= 0) return NOT_MEASURED;
  return `${(tokens / (generating / 1000)).toFixed(1)} tok/s`;
}

/** The same arithmetic for one attempt, from the attempt's own numbers.
 *
 * The winning attempt is the only one that carries a token count -- a failed
 * attempt produced no answer to count -- so every other row is a dash here
 * rather than a rate computed from the request's tokens, which belong to
 * whichever model answered.
 */
function formatAttemptRate(attempt) {
  const tokens = Number(attempt.tokens_out || 0);
  const duration = Number(attempt.duration_ms || 0);
  const ttft = attempt.ttft_ms == null ? null : Number(attempt.ttft_ms);
  if (!tokens || !duration || ttft == null) return NOT_MEASURED;
  const generating = duration - ttft;
  if (generating <= 0) return NOT_MEASURED;
  return `${(tokens / (generating / 1000)).toFixed(1)} tok/s`;
}

function formatTurnSummary(row) {
  const parts = [];
  if (row.thinking_chars) parts.push(`${row.thinking_chars.toLocaleString()} chars reasoning`);
  if (row.tool_call_count) {
    parts.push(row.tool_call_count === 1 ? "1 tool call" : `${row.tool_call_count} tool calls`);
  }
  if (row.output_chars) parts.push(`${row.output_chars.toLocaleString()} chars reply`);
  return parts.join(" · ");
}

/** Name the vision model this request was handed to, and how it went.
 *
 * The adapter is the head of the chain on a diverted request, which is a fact
 * you can only read off the trace if you already know how diversion works.
 * Saying it outright is the difference between "gpt-5.6-luna answered" and
 * "the vision adapter took this one".
 */
function formatVisionModel(row) {
  if (row.route_diversion !== "vision") return "";
  const chain = (row.route_chain || "")
    .split(",")
    .map((ref) => ref.trim())
    .filter(Boolean);
  const adapter = chain[0];
  if (!adapter) return "";
  const attempt = Number(row.route_attempt ?? 0);
  if (attempt === 0) return `${adapter} — answered`;
  const served = row.provider
    ? `${row.provider}/${row.resolved_model}`
    : row.resolved_model || "a fallback";
  return `${adapter} — failed, answered by ${served}`;
}

/** What each model on the route did, in the order the chain tried them.
 *
 * The request row can only name the model that answered. When a primary failed
 * and a fallback rescued the request, the row said "success" and the reason the
 * primary was abandoned survived only in a log line -- so the one question
 * worth asking of a fallback, "what was wrong with the model I chose?", had no
 * answer here at all.
 *
 * Skipped attempts are drawn too. A three-model chain that only ever ran one
 * looked exactly like a one-model route, which is the difference between "the
 * fallback did not help" and "the fallback was never asked".
 */
/** The stored ladder for one attempt, or null when nothing was measured. */
function ladderOf(attempt) {
  const ladder = attempt && attempt.params && attempt.params.ladder;
  return ladder && typeof ladder === "object" ? ladder : null;
}

/* One upstream try plus a diagnostic probe is exactly the case the operator
   most needs to see -- "why did my 429 go somewhere else?" -- so the probe
   counts toward "was anything hidden", even though it is deliberately not a
   try. */
function ladderRows(ladder) {
  const summary = (ladder && ladder.summary) || {};
  return Number(summary.tries || 0) + Number(summary.probes || 0);
}

function hasLadder(attempt) {
  const ladder = ladderOf(attempt);
  return !!(ladder && ladder.summary && ladderRows(ladder) > 1);
}

/** Render a number of milliseconds as seconds, or nothing when unmeasured. */
function ladderSeconds(ms) {
  const value = Number(ms);
  if (!Number.isFinite(value) || value <= 0) return "";
  return `${Math.round(value / 1000)}s`;
}

/** "15 tries · 12x429, 3x502 · 3 keys · 96s sleeping". */
function ladderHeadline(attempt) {
  const ladder = ladderOf(attempt);
  const summary = ladder && ladder.summary;
  if (!summary || ladderRows(ladder) <= 1) return "";
  const parts = [`${summary.tries} ${Number(summary.tries) === 1 ? "try" : "tries"}`];
  if (Number(summary.probes || 0) > 0) {
    parts.push(`${summary.probes} probe${Number(summary.probes) > 1 ? "s" : ""}`);
  }
  const census = summary.statuses_by_code || {};
  const codes = Object.keys(census);
  if (codes.length) {
    parts.push(codes.map((code) => `${census[code]}×${code}`).join(", "));
  }
  if (Number(summary.keys || 0) > 0) {
    parts.push(`${summary.keys} keys`);
  }
  const sleeping = ladderSeconds(summary.time_sleeping_ms);
  if (sleeping) parts.push(`${sleeping} sleeping`);
  const limiter = ladderSeconds(summary.time_limiter_ms);
  if (limiter) parts.push(`${limiter} on the provider block`);
  if (Number(summary.tries_dropped || 0) > 0) {
    parts.push(`${summary.tries_dropped} tries not stored`);
  }
  return parts.join(" · ");
}

/** Append the "Key" cell: the name when there is one, else the mask. */
function addKeyReference(tr, label) {
  const td = document.createElement("td");
  const name = keyNameForLabel(label);
  td.textContent = credentialDisplay(name, label);
  if (name && label) td.title = label;
  tr.appendChild(td);
}

/** One line per try: what it met, on which key, and what it cost. */
function ladderTryText(entry, position) {
  const parts = [`#${position}`];
  if (entry.key_index === -1) {
    parts.push("no key available");
  } else if (entry.key_index != null) {
    parts.push(
      entry.key_label
        ? `key ${entry.key_index} ${keyReferenceText(entry.key_label)}`
        : `key ${entry.key_index}`,
    );
  }
  const what = entry.status != null ? String(entry.status) : entry.kind || entry.error_kind;
  // A try with no status and no exception name is a wait, not a knock.
  parts.push(what || entry.source);
  // A probe is not a try the client asked for, and reading it as one would
  // make a routed-around 429 look like two knocks on the same model.
  if (entry.source === "probe") {
    parts.push(
      entry.status === 429
        ? "probe — the key is limited, not just the model"
        : "probe — the key is healthy, the model is limited",
    );
  }
  if (entry.upstream_ms != null) parts.push(`${Math.round(entry.upstream_ms)}ms`);
  // Missing terms are omitted rather than rendered as 0: the project's
  // not-measured convention, and a zero wait is a claim we cannot make.
  if (entry.waited_ms != null) parts.push(`waited ${Math.round(entry.waited_ms)}ms`);
  if (entry.retry_after != null) parts.push(`retry-after ${entry.retry_after}s`);
  /* Why this body differs from the one above it. Present only on a try a
     recovery rung rewrote, so every row written before 7.23.0 -- and
     every ordinary try since -- reads exactly as it did. */
  if (entry.recovery) parts.push(`retry: ${entry.recovery}`);
  return parts.join(" · ");
}

/* The head fields, in the order an operator reads them: what answered, how it
   said the body was encoded, who the edge was, and then the bytes themselves.
   Only these keys are rendered, so a field added to the record later cannot
   appear on the page without somebody deciding it should. */
const LADDER_HEAD_FIELDS = [
  ["status", "status"],
  ["content_type", "content-type"],
  ["content_encoding", "content-encoding"],
  ["content_length", "content-length"],
  ["transfer_encoding", "transfer-encoding"],
  ["server", "server"],
  ["cf_ray", "cf-ray"],
  ["cf_placement", "cf-placement"],
  ["retry_after", "retry-after"],
  ["body_bytes", "body bytes"],
  ["decode_error", "decode error"],
];

/**
 * The response head recorded for one try, as text.
 *
 * Recorded only where the body could not be decoded -- the case where the
 * status used to be lost entirely -- so a row from any earlier release has no
 * head at all and renders exactly as it did. Returns "" when there is nothing
 * to show, which is what keeps the block off every other row.
 */
function ladderHeadText(head) {
  if (!head || typeof head !== "object") return "";
  const lines = [];
  LADDER_HEAD_FIELDS.forEach(([key, label]) => {
    const value = head[key];
    // `!= null` rather than a truthiness test: a content-length of 0 is a
    // fact about the reply, and the one this record exists to show.
    if (value != null && value !== "") lines.push(`${label}: ${value}`);
  });
  if (head.body_head_hex) lines.push(`first bytes: ${head.body_head_hex}`);
  return lines.join("\n");
}

/** "key 0 ab...cd - benched 60s (rate_limit): 429 with no Retry-After". */
function ladderDecisionText(decision) {
  const who =
    decision.key_index === -1
      ? "no key available"
      : decision.key_label
        ? `key ${decision.key_index} ${keyReferenceText(decision.key_label)}`
        : `key ${decision.key_index}`;
  // A (key, model) bench is a different fact from a whole-key bench, and
  // the reader needs to see which one happened.
  const verdict =
    decision["class"] == null
      ? "health unchanged"
      : decision.benched_for_s != null
        ? `benched ${Math.round(decision.benched_for_s)}s (${decision["class"]})`
        : decision.model != null && decision.model_benched_for_s != null
          ? `${decision.model} benched ${Math.round(decision.model_benched_for_s)}s (${decision["class"]})`
          : `charged (${decision["class"]})`;
  const reason = decision.reason ? `: ${decision.reason}` : "";
  return `${who} — ${verdict}${reason}`;
}

/**
 * Draw one attempt's retry ladder under its reason line.
 *
 * The root-cause sentence is *stored*, not composed here, so the modal and all
 * four export formats say the same thing and a test can pin the string.
 */
function appendLadder(item, ladder) {
  if (!ladder) return;
  if (ladderRows(ladder) <= 1) return;

  if (ladder.root_cause) {
    const why = document.createElement("p");
    why.className = "req-chain-rootcause";
    why.textContent = ladder.root_cause;
    item.appendChild(why);
  }

  const tries = Array.isArray(ladder.tries) ? ladder.tries : [];
  if (tries.length) {
    const details = document.createElement("details");
    details.className = "req-chain-ladder";
    const label = document.createElement("summary");
    label.textContent = `Show ${tries.length} upstream tries`;
    details.appendChild(label);
    const rows = document.createElement("ol");
    rows.className = "req-chain-tries";
    let position = 0;
    tries.forEach((entry) => {
      position += 1;
      const row = document.createElement("li");
      const state = entry.status != null ? entry.status : entry.source;
      row.className = `req-chain-try is-${state}`;
      row.appendChild(document.createTextNode(ladderTryText(entry, position)));
      if (entry.body) {
        const bodyBox = document.createElement("details");
        bodyBox.className = "req-chain-try-body";
        const bodyLabel = document.createElement("summary");
        bodyLabel.textContent = entry.body_truncated ? "Body (truncated)" : "Body";
        bodyBox.appendChild(bodyLabel);
        const pre = document.createElement("pre");
        pre.textContent = entry.body;
        bodyBox.appendChild(pre);
        row.appendChild(bodyBox);
      }
      const headText = ladderHeadText(entry.response_head);
      if (headText) {
        const headBox = document.createElement("details");
        headBox.className = "req-chain-try-head";
        const headLabel = document.createElement("summary");
        headLabel.textContent = "Response head";
        headBox.appendChild(headLabel);
        const headPre = document.createElement("pre");
        headPre.textContent = headText;
        headBox.appendChild(headPre);
        row.appendChild(headBox);
      }
      rows.appendChild(row);
    });
    details.appendChild(rows);
    item.appendChild(details);
  }

  const decisions = Array.isArray(ladder.credentials) ? ladder.credentials : [];
  if (decisions.length) {
    const list = document.createElement("ul");
    list.className = "req-chain-decisions";
    decisions.forEach((decision) => {
      const row = document.createElement("li");
      if (decision.key_index === -1) row.className = "req-chain-nokey";
      row.textContent = ladderDecisionText(decision);
      list.appendChild(row);
    });
    item.appendChild(list);
  }
}

/** The registry's account of a bench, when this attempt was skipped by one. */
function benchOf(attempt) {
  const params = attempt && attempt.params;
  const bench = params && params.bench;
  return bench && typeof bench === "object" ? bench : null;
}

/** What became of a stream that failed after the client had started reading. */
function truncationOf(attempt) {
  const params = attempt && attempt.params;
  const truncated = params && params.truncated_after_commit;
  return truncated && typeof truncated === "object" ? truncated : null;
}

/**
 * Say what the reader was actually left with when a committed stream died.
 *
 * The attempt row says "timeout" either way, which cannot distinguish the
 * turn that died from the turn that was ended early with a short answer -- and
 * that difference is the whole of what this release changed.
 */
function truncationText(truncated) {
  if (!truncated) return "";
  if (truncated.ended_cleanly === false) {
    return "stalled inside a tool call — cannot be completed";
  }
  const chars = Number(truncated.chars || 0);
  return (
    `ended early after ${chars.toLocaleString()} chars; the answer is` +
    ` incomplete (sent to the client as ${truncated.stop_reason_sent || "—"})`
  );
}

/** The account of a message this attempt inherited half-written. */
function continuationOf(attempt) {
  const params = attempt && attempt.params;
  const continued = params && params.continuation;
  return continued && typeof continued === "object" ? continued : null;
}

/**
 * Name the model that stalled, and how far it had got.
 *
 * There is no seam in the stream itself -- the reader sees one continuous
 * answer, which is the point -- so this row is the only place the model change
 * is recorded at all.
 */
function continuationText(continued) {
  if (!continued) return "";
  const from = continued.resumed_from_model || "—";
  const chars = Number(continued.prefix_chars || 0).toLocaleString();
  if (continued.accepted === false) {
    return `${from} stalled at ${chars} chars; the continuation was not usable`;
  }
  return `continued here after ${from} stalled at ${chars} chars`;
}

/**
 * Say why a skipped model was skipped, in the terms that benched it.
 *
 * The row's own sentence is stored server-side, so this is the breakdown
 * underneath it: which mode decided, on what evidence, and how much of the
 * bench is left at the moment the request ran. Before this the modal showed
 * one fixed line about consecutive failures on a build whose default mode has
 * been rate-based since 5.61.0, so the reader was told about a counter that
 * was never consulted.
 */
function appendBenchReason(item, bench) {
  if (!bench) return;
  const parts = [];
  if (bench.mode === "rate_based" && bench.window) {
    const share =
      bench.rate == null ? "" : ` of at least ${Math.round(bench.rate * 100)}%`;
    parts.push(
      `${bench.failures} counted failure${bench.failures === 1 ? "" : "s"} in the` +
        ` last ${bench.window} attempts${share}`,
    );
  } else {
    parts.push(
      `${bench.failures} consecutive failure${bench.failures === 1 ? "" : "s"}`,
    );
  }
  if (bench.last_kind) {
    parts.push(
      bench.last_status == null
        ? `last: ${bench.last_kind}`
        : `last: ${bench.last_status} ${bench.last_kind}`,
    );
  }
  if (bench.remaining_seconds != null) {
    parts.push(`${formatSeconds(Math.round(bench.remaining_seconds))} left`);
  }
  if (bench.since != null) {
    parts.push(`benched ${formatSeconds(Math.round(bench.since))} ago`);
  }
  const why = document.createElement("p");
  why.className = "req-chain-bench";
  why.textContent = parts.join(" · ");
  item.appendChild(why);
}

/** What ended this attempt, in two or three words rather than a sentence.
 *
 * The prose underneath stays where it is -- it carries the host's own message
 * and a reader needs it -- but the timeline is a list of five attempts and
 * "what ended it" has to be scannable beside the numbers rather than read.
 */
function attemptEndedBy(attempt) {
  const bench = benchOf(attempt);
  if (bench) return bench.last_kind ? `benched (${bench.last_kind})` : "benched";
  if (attempt.error_kind) return String(attempt.error_kind);
  const ladder = ladderOf(attempt);
  if (ladder && ladder.root_cause) return String(ladder.root_cause);
  return CHAIN_OUTCOME_LABELS[attempt.outcome] || attempt.outcome || NOT_MEASURED;
}

/* The per-attempt latency line, under the attempt's own head row.
 *
 * Each of these was previously only ever visible as the request's single
 * number, which is the *chain's* figure: on a request that fell back twice,
 * the answering model's 300 ms was invisible and the 4.7 s the reader saw
 * belonged to two models that never answered. A dash is a measurement that
 * was never taken -- every attempt written before 7.4.0 is unmeasured and
 * there is no backfill -- and is never a zero.
 *
 * A skipped attempt is drawn with the same row, all dashes: it has no latency
 * because it was never asked, and its bench reason is the sentence below.
 * Dropping the row for skipped attempts would reintroduce "the fallback was
 * never tried" as something the panel does not mention.
 */
function appendAttemptMetrics(item, attempt) {
  const row = document.createElement("div");
  row.className = "req-chain-metrics";
  const ttft = attempt.ttft_ms == null ? null : Number(attempt.ttft_ms);
  const duration = attempt.duration_ms == null ? null : Number(attempt.duration_ms);
  // NULL whenever either input is, and refused when the subtraction would be
  // negative: a generating time of "-40 ms" is a measurement error, not a
  // measurement.
  const generating =
    ttft == null || duration == null || duration - ttft < 0 ? null : duration - ttft;
  const cells = [
    ["TTFT", ttft == null ? NOT_MEASURED : formatChainDuration(ttft)],
    [
      "first reasoning",
      attempt.first_reasoning_ms == null
        ? NOT_MEASURED
        : formatChainDuration(attempt.first_reasoning_ms),
    ],
    [
      "generating",
      generating == null ? NOT_MEASURED : formatChainDuration(generating),
    ],
    [
      "tokens out",
      attempt.tokens_out == null
        ? NOT_MEASURED
        : Number(attempt.tokens_out).toLocaleString(),
    ],
    ["rate", formatAttemptRate(attempt)],
    ["ended", attemptEndedBy(attempt)],
  ];
  cells.forEach(([label, value]) => {
    const cell = document.createElement("span");
    cell.className = "req-chain-metric";
    const name = document.createElement("span");
    name.className = "req-chain-metric-label";
    name.textContent = label;
    const shown = document.createElement("span");
    shown.className = "req-chain-metric-value";
    shown.textContent = value;
    cell.append(name, shown);
    row.appendChild(cell);
  });
  row.title =
    "Measured on this attempt alone. A dash is a measurement that was never" +
    " taken: attempts recorded before 7.4.0 carry no first-token time, and a" +
    " model that was never asked has no latency at all.";
  item.appendChild(row);
}

function renderRequestChain(row) {
  const container = byId("reqDetailChain");
  if (!container) return;
  container.innerHTML = "";
  const attempts = row.route_attempts || [];
  const routeAttempts = chainAttempts(row);
  // One attempt that succeeded is just "the model answered" -- the route
  // summary above already says that, and repeating it as a timeline implies a
  // chain did something when it did not. One attempt that knocked fifteen
  // times is a different matter: the ladder is the only place that shows it,
  // so a single attempt with a ladder still gets the panel.
  if (
    routeAttempts.length < 2 &&
    attempts.length === routeAttempts.length &&
    !attempts.some(hasLadder) &&
    !attempts.some((attempt) => truncationOf(attempt)) &&
    !attempts.some((attempt) => continuationOf(attempt))
  ) {
    container.hidden = true;
    return;
  }
  container.hidden = false;

  const heading = document.createElement("h4");
  heading.className = "req-chain-title";
  heading.textContent = "Route attempts";
  container.appendChild(heading);

  // The chain's own root cause, above the per-attempt ones: when models were
  // removed before the request began, what the surviving model answered is
  // not the whole story, and the incident this came from is exactly that --
  // four capable models benched, and the request answered by the one left.
  const benched = attempts.filter((attempt) => benchOf(attempt) !== null).length;
  if (benched) {
    const note = document.createElement("p");
    note.className = "req-chain-rootcause";
    note.textContent =
      `${benched} model${benched === 1 ? " was" : "s were"} benched and never` +
      " tried on this request.";
    container.appendChild(note);
  }

  const list = document.createElement("ol");
  list.className = "req-chain-list";
  attempts.forEach((attempt) => {
    const item = document.createElement("li");
    // The same third group the Model latency panel uses, read off the same
    // column: the attempt is stored as `failed`, but `interrupted` means the
    // client hung up on it and calling that a failure of the model is the
    // misattribution this group exists to end. The stored `outcome` is not
    // rewritten -- only what this badge says about it.
    const shown = chainOutcomeGroup(attempt);
    item.className = `req-chain-item is-${shown}`;

    const head = document.createElement("div");
    head.className = "req-chain-head";

    const badge = document.createElement("span");
    badge.className = "req-chain-outcome";
    badge.textContent = CHAIN_OUTCOME_LABELS[shown] || shown || "—";
    head.appendChild(badge);

    const model = document.createElement("code");
    model.className = "req-chain-model";
    model.textContent = attempt.model_ref || "—";
    model.title = attempt.model_ref || "";
    head.appendChild(model);

    if (isDescribeAttempt(attempt)) {
      const kind = document.createElement("span");
      kind.className = "req-chain-describe";
      kind.textContent = "described an image";
      head.appendChild(kind);
    }

    // Priced on its own row, never folded into the request's. A describe hop
    // is a different model on a different key, and adding it to the answering
    // model's figure would make that model look more expensive than it was
    // while hiding what describe mode actually cost.
    if (attempt.cost_usd != null) {
      const spent = document.createElement("span");
      spent.className = "req-chain-summary cost-amount";
      spent.textContent = formatCostAmount(attempt.cost_usd);
      spent.title =
        attempt.cost_source === "provider"
          ? "reported by the host itself"
          : `estimated from ${COST_SOURCE_LABELS[attempt.cost_source] || attempt.cost_source}`;
      head.appendChild(spent);
      if (attempt.cost_source && attempt.cost_source !== "provider") {
        const badge = document.createElement("span");
        badge.className = "cost-badge";
        badge.textContent = "est.";
        head.appendChild(badge);
      }
    }

    if (attempt.duration_ms != null) {
      const took = document.createElement("span");
      took.className = "req-chain-duration";
      took.textContent = formatChainDuration(attempt.duration_ms);
      head.appendChild(took);
    }

    // What the one recorded status used to hide: how many times this attempt
    // actually knocked, what it met, across how many keys, and how much of its
    // duration was MCC waiting rather than the model thinking.
    const summaryText = ladderHeadline(attempt);
    if (summaryText) {
      const summary = document.createElement("span");
      summary.className = "req-chain-summary";
      summary.textContent = summaryText;
      head.appendChild(summary);
    }

    const truncatedText = truncationText(truncationOf(attempt));
    if (truncatedText) {
      const truncated = document.createElement("span");
      truncated.className = "req-chain-summary req-chain-truncated";
      truncated.textContent = truncatedText;
      head.appendChild(truncated);
    }

    const continuedText = continuationText(continuationOf(attempt));
    if (continuedText) {
      const continued = document.createElement("span");
      continued.className = "req-chain-summary req-chain-continued";
      continued.textContent = continuedText;
      head.appendChild(continued);
    }

    // Which credential served this attempt. The request row names only the
    // last one, so a route that rotated keys used to be attributed whole to
    // whichever key happened to finish it.
    const credential = document.createElement("span");
    credential.className = "req-chain-key";
    if (attempt.key_index === -1) {
      credential.classList.add("req-chain-nokey");
      credential.textContent = "no key available";
      credential.title = "Every credential in the pool was benched; this attempt never reached a key.";
    } else if (attempt.key_label) {
      const name = keyNameForLabel(attempt.key_label);
      credential.textContent = credentialDisplay(name, attempt.key_label);
      if (name) credential.title = attempt.key_label;
    } else {
      credential.textContent = NOT_MEASURED;
      credential.title = "No credential was recorded for this attempt.";
    }
    head.appendChild(credential);
    item.appendChild(head);
    appendAttemptMetrics(item, attempt);

    // The reason, which is the entire point of the panel.
    const reason = attempt.error_message || "";
    if (reason) {
      const why = document.createElement("p");
      why.className = "req-chain-reason";
      if (attempt.error_kind) {
        const kind = document.createElement("span");
        kind.className = "req-chain-kind";
        kind.textContent = attempt.error_kind;
        why.appendChild(kind);
      }
      why.appendChild(document.createTextNode(reason));
      item.appendChild(why);
    }

    appendBenchReason(item, benchOf(attempt));
    appendLadder(item, ladderOf(attempt));
    list.appendChild(item);
  });
  container.appendChild(list);
}

/**
 * Show the body MCC actually sent, per attempt, minus the prompt text.
 *
 * The meta list above shows the *client's* parameters -- what Claude Code
 * asked for. This panel shows what left the process after routing, the output
 * budget, every provider postprocessor and any create-level retry rewrite.
 * They differ constantly: a client asking for 64,000 tokens against a model
 * capped at 16,384 is sent 16,384, and for months only the 64,000 was visible.
 *
 * Message and system text is deliberately absent -- it is captured once, in
 * the Prompt pane below -- but its structure survives, so "40 tools, 12
 * messages, 3 image blocks" is still readable here.
 */
function renderWireRequest(row) {
  const container = byId("reqDetailWire");
  if (!container) return;
  container.innerHTML = "";
  // Every attempt, not only the instrumented ones. Hiding the pane when no
  // body was captured read as "no request body was sent", which is the one
  // thing it never meant: a provider with no instrumented commit boundary
  // still sent a body, and this pane now says so instead of vanishing.
  // Describe hops are left out: their body is a picture and a prompt MCC
  // wrote, not the request this pane is about.
  const attempts = chainAttempts(row);
  if (!attempts.length) {
    container.hidden = true;
    return;
  }
  container.hidden = false;

  const heading = document.createElement("h4");
  heading.className = "req-chain-title";
  heading.textContent = "Request body sent (no prompt text)";
  container.appendChild(heading);

  attempts.forEach((attempt) => {
    const pane = document.createElement("details");
    pane.className = "req-wire-pane";
    if (attempts.length === 1) pane.open = false;

    const summary = document.createElement("summary");
    summary.className = "req-wire-head";

    const model = document.createElement("code");
    model.className = "req-chain-model";
    model.textContent = attempt.model_ref || "—";
    summary.appendChild(model);

    const facts = document.createElement("span");
    facts.className = "req-wire-facts";
    facts.textContent = formatWireFacts(attempt);
    summary.appendChild(facts);

    const reasoning = document.createElement("span");
    reasoning.className = `req-wire-reasoning is-${wireReasoningState(attempt)}`;
    reasoning.textContent = formatReasoningEmitted(attempt);
    reasoning.title =
      "Whether the outbound body actually carried a reasoning instruction, " +
      "as opposed to what reasoning gating decided.";
    summary.appendChild(reasoning);
    pane.appendChild(summary);

    if (wireContradicts(row, attempt)) {
      const clash = document.createElement("span");
      clash.className = "req-wire-contradiction";
      clash.textContent = "gating asked for reasoning; nothing was sent";
      clash.title = row.reasoning_adaptation || "";
      summary.appendChild(clash);
    }

    const body = attempt.wire_body;
    if (body && body._truncated) {
      const note = document.createElement("p");
      note.className = "req-wire-truncated";
      note.textContent =
        `Truncated at ${Number(body._limit).toLocaleString()} of ` +
        `${Number(body._original_chars).toLocaleString()} characters.`;
      pane.appendChild(note);
    }
    if (body && Array.isArray(body._degraded)) {
      const note = document.createElement("p");
      note.className = "req-wire-truncated";
      note.textContent =
        `Message and tool structure reduced to counts at ` +
        `${Number(body._limit).toLocaleString()} of ` +
        `${Number(body._original_chars).toLocaleString()} characters ` +
        `(${body._degraded.join(", ")}). Every parameter is stored whole ` +
        `and shown above.`;
      pane.appendChild(note);
    }

    const knobs = buildWireKnobs(attempt);
    if (knobs) pane.appendChild(knobs);

    const shapePane = buildResponseShape(attempt);
    if (shapePane) pane.appendChild(shapePane);

    if (body == null) {
      const note = document.createElement("p");
      note.className = "req-wire-unmeasured";
      note.textContent =
        attempt.outcome === "skipped"
          ? "Never sent — the chain skipped this model."
          : "Not measured — this provider has no instrumented commit " +
            "boundary, or the attempt was never sent.";
      pane.appendChild(note);
    } else {
      const pre = document.createElement("pre");
      pre.className = "requests-detail-body req-wire-body";
      pre.textContent = formatWireBody(body);
      pane.appendChild(pre);
    }
    container.appendChild(pane);
  });
}

/* Every sampling field the writer summarises, in its declared order.
   tests/contracts/test_admin_wire_view.py pins this list against
   core/wire_capture.py::_SAMPLING_FIELDS so the two cannot drift. */
const WIRE_SAMPLING_FIELDS = [
  "temperature",
  "top_p",
  "top_k",
  "presence_penalty",
  "frequency_penalty",
  "repetition_penalty",
  "seed",
  "stop",
  "n",
];

/* "effort high" is a fact about the request; "set for this model on the
   Models page" is the answer to the question it provokes. The request log is
   the only place that answer can be given, because by the time a body is
   built the preference is indistinguishable from a client's own ask. */
function preferenceSourceText(attempt) {
  const preferences = attempt.params && attempt.params.preferences;
  if (!preferences) return "";
  const parts = Object.keys(preferences)
    .sort()
    .map((name) => {
      const what = name === "reasoning_preference" ? "effort" : "max_tokens";
      const where =
        preferences[name] === "model" ? "for this model" : "for this provider";
      return `${what} set ${where} on the Models page`;
    });
  return parts.join(", ");
}

/** One line of the numbers people open this panel to check. */
function formatWireFacts(attempt) {
  const wire = (attempt.params && attempt.params.wire) || {};
  const widened = attempt.params && attempt.params.output_widened_from;
  const parts = [];
  /* First, because it answers a question that comes before every number below
     it: which of the host's endpoints this attempt was actually posted to, and
     whether that was published, learned or forced. Only a gateway with more
     than one surface records it. */
  if (wire.surface) parts.push(`surface ${wire.surface}`);
  if (wire.max_tokens != null) parts.push(`max_tokens ${Number(wire.max_tokens).toLocaleString()}`);
  /* The "from" for the max_tokens above. Only present when the allowance was
     actually raised because the attempt was going to think, so the line reads
     as an explanation of a number that would otherwise look invented. */
  if (widened != null) parts.push(`raised from ${Number(widened).toLocaleString()} for reasoning`);
  /* Where a number above came from, when it was not the client's idea. Only
     present when a per-model or per-provider preference actually decided
     something, so every row written before this release -- having no key at
     all -- renders exactly as it always did. */
  const preferenceNote = preferenceSourceText(attempt);
  if (preferenceNote) parts.push(preferenceNote);
  if (wire.tools != null) parts.push(wire.tools === 1 ? "1 tool" : `${wire.tools} tools`);
  if (wire.temperature != null) parts.push(`temp ${wire.temperature}`);
  const reasoning = wire.reasoning || null;
  if (reasoning) {
    const keys = Object.keys(reasoning);
    if (keys.length === 1) {
      parts.push(`${keys[0]} ${wireValueText(reasoning[keys[0]])}`);
    } else if (keys.length > 1) {
      parts.push(`${keys.length} reasoning fields`);
    }
  }
  return parts.join(" · ");
}

function wireValueText(value) {
  return typeof value === "string" ? value : JSON.stringify(value);
}

/* Every parameter of the captured body, read from params.wire, which is never
   truncated. The body pane below can degrade its message and tool structure
   under the size cap; this block cannot, because it is built from the compact
   summary the writer always stores whole -- and it is rendered above that pane
   because debugging reads knobs first and structure second.

   Nothing the writer stored is dropped here. The block used to render a
   hard-coded shortlist, which meant a parameter MCC had learned to send but
   this list had never heard of -- min_p, tool_choice, response_format -- was
   captured, stored, and then invisible. The named rows below only fix the
   ORDER the familiar knobs are read in; every remaining key follows them,
   sorted, so a dialect nobody anticipated still shows up whole.

   A key that was not sent has no row: absence is the finding here, so it is
   shown as absence rather than as a dash. */
function buildWireKnobs(attempt) {
  const wire = (attempt.params && attempt.params.wire) || null;
  if (!wire) return null;
  const rows = [];
  /* "reasoning" is a nested container whose keys are rendered individually
     below, so it is claimed here and never printed as a JSON blob of its own. */
  const claimed = new Set(
    ["model", "max_tokens", "tools", "reasoning"].concat(WIRE_SAMPLING_FIELDS),
  );
  ["model", "max_tokens", "tools"].forEach((name) => {
    if (wire[name] != null) rows.push([name, wireValueText(wire[name])]);
  });
  const widened = attempt.params && attempt.params.output_widened_from;
  if (widened != null) rows.push(["output_widened_from", wireValueText(widened)]);
  const preferences = (attempt.params && attempt.params.preferences) || null;
  if (preferences) {
    Object.keys(preferences)
      .sort()
      .forEach((name) => {
        rows.push([
          `${name} set on`,
          `${preferences[name]} row, on the Models page`,
        ]);
      });
  }
  const reasoning = wire.reasoning || {};
  Object.keys(reasoning).forEach((name) => {
    rows.push([name, wireValueText(reasoning[name])]);
  });
  WIRE_SAMPLING_FIELDS.forEach((name) => {
    if (wire[name] != null) rows.push([name, wireValueText(wire[name])]);
  });
  Object.keys(wire)
    .filter((name) => !claimed.has(name) && wire[name] != null)
    .sort()
    .forEach((name) => {
      rows.push([name, wireValueText(wire[name])]);
    });
  if (!rows.length) return null;
  const list = document.createElement("dl");
  list.className = "req-wire-knobs";
  rows.forEach(([name, value]) => {
    const dt = document.createElement("dt");
    dt.textContent = name;
    const dd = document.createElement("dd");
    dd.textContent = value;
    list.append(dt, dd);
  });
  return list;
}

/* Gating said one thing; the wire did another. Both are facts, and only the
   pair is diagnostic: an adaptation that is not a suppression means gating
   intended a reasoning instruction, so an empty body is a gap between the
   policy and the encoder -- the exact divergence reasoning_emitted exists to
   expose. A SUPPRESSED adaptation means gating intended nothing, and an empty
   body agrees with it.

   Keyed on the stored adaptation *kind*, never on the message text: the
   message is prose that gets reworded, and a badge that reads a sentence
   fires or stops firing on an edit nobody connected to it. A row written
   before the kind column existed carries null and is badged as nothing --
   not measured is not a finding.

   "dropped" is deliberately NOT in this list, and "nothing_sent" is not
   either. Until 6.6.0 one value covered both "the level was discarded and
   thinking was switched on through another field" (where an empty body IS a
   contradiction) and "no reasoning instruction was sent at all" (where an
   empty body is the outcome). Stored rows are not migrated -- a row means what
   it meant when it was written -- so every pre-6.6.0 "dropped" row is
   ambiguous, and on the live install the correct, intended case was the
   overwhelming majority: badging them all flagged working behaviour as a
   defect. "substituted" and "clamped" carry no such ambiguity in either
   version: both name a value that gating chose to put on the wire, so an
   empty body still contradicts them and still badges. The cost is that the
   post-6.6.0 "dropped" contradiction is no longer badged; a false alarm on
   correct behaviour is worse than a missed one on a case the adaptation
   message already describes in full. */
const CONTRADICTING_ADAPTATION_KINDS = ["substituted", "clamped"];

function wireContradicts(row, attempt) {
  if (attempt.reasoning_emitted !== 0 && attempt.reasoning_emitted !== false) {
    return false;
  }
  const kind = row.reasoning_adaptation_kind;
  if (kind == null || kind === "") return false;
  return CONTRADICTING_ADAPTATION_KINDS.includes(String(kind).toLowerCase());
}

// Null is "not measured" -- an attempt written before wire capture existed, or
// one whose provider has no instrumented commit boundary. It must not read as
// "reasoning was off", which is the exact confusion this field exists to end.
function wireReasoningState(attempt) {
  if (attempt.reasoning_emitted == null) return "unknown";
  return attempt.reasoning_emitted ? "on" : "off";
}

function formatReasoningEmitted(attempt) {
  const state = wireReasoningState(attempt);
  if (state === "unknown") return "reasoning not measured";
  if (state === "on") {
    const value = wireReasoningValue(attempt);
    return value ? `reasoning sent: ${value}` : "reasoning sent";
  }
  // Not a fault. For a toggle-only model on an effort-only host, sending
  // nothing is the correct outcome and the model's own default applies; the
  // old wording ("no reasoning sent") read as a failure of the proxy.
  return "no reasoning instruction sent (model default applies)";
}

/** What params.wire.reasoning actually carried, for the badge headline. */
function wireReasoningValue(attempt) {
  const reasoning = (attempt.params && attempt.params.wire &&
    attempt.params.wire.reasoning) || null;
  if (!reasoning) return "";
  const keys = Object.keys(reasoning);
  if (!keys.length) return "";
  if (keys.length === 1) return wireValueText(reasoning[keys[0]]);
  return keys.join(", ");
}

/**
 * What came back, opposite the body that asked for it.
 *
 * "reasoning requested 1, returned 0" is two measurements of two different
 * things: the first counts outbound bodies carrying a reasoning field, the
 * second counts requests whose client saw a thinking block. With nothing
 * recorded about the reply there was no way to tell which half was wrong --
 * a host that sent no reasoning, or a translation that dropped it. This is
 * the reply's shape and only its shape: which delta fields arrived, how many
 * and how long, the finish reason, whether usage came. Never the text.
 */
function buildResponseShape(attempt) {
  const shape = attempt.params && attempt.params.response_shape;
  if (!shape) return null;
  const wrap = document.createElement("div");
  wrap.className = "req-shape-pane";

  const title = document.createElement("p");
  title.className = "req-shape-title";
  title.textContent = "Response shape";
  title.title =
    "Structure of the upstream reply, recorded without any of its content.";
  wrap.appendChild(title);

  const grid = document.createElement("dl");
  grid.className = "req-shape-grid";
  const fields = shape.fields || {};
  const names = Object.keys(fields);
  if (names.length === 0) {
    const note = document.createElement("p");
    note.className = "req-shape-empty";
    note.textContent = "No content deltas arrived on this attempt.";
    wrap.appendChild(note);
  }
  names.forEach((name) => {
    const entry = fields[name] || {};
    appendShapeRow(
      grid,
      name,
      `${Number(entry.deltas || 0).toLocaleString()} deltas, ` +
        `${Number(entry.chars || 0).toLocaleString()} chars`,
    );
  });
  appendShapeRow(grid, "finish_reason", shape.finish_reason || "—");
  appendShapeRow(
    grid,
    "usage",
    shape.usage ? (shape.usage_keys || []).join(", ") || "yes" : "absent",
  );
  if (typeof shape.first_chunk_ms === "number") {
    appendShapeRow(grid, "first chunk", `${shape.first_chunk_ms} ms`);
  }
  appendShapeRow(grid, "chunks", Number(shape.chunks || 0).toLocaleString());
  wrap.appendChild(grid);
  return wrap;
}

function appendShapeRow(grid, term, value) {
  const dt = document.createElement("dt");
  dt.textContent = term;
  const dd = document.createElement("dd");
  dd.textContent = value;
  grid.append(dt, dd);
}

function formatWireBody(body) {
  if (body == null) return "";
  // Rows written before the writer stopped cutting JSON stored a truncated
  // string under _preview. It is not parseable, so it is shown as-is with the
  // note above saying why. New bodies never set _truncated and always parse.
  if (body._truncated) return String(body._preview || "");
  try {
    return JSON.stringify(body, null, 2);
  } catch (error) {
    return String(body);
  }
}

const CHAIN_OUTCOME_LABELS = {
  succeeded: "answered",
  failed: "failed",
  skipped: "not tried",
  // Not a failure of this model, and deliberately not counted as one: the
  // attempt is stored as `failed` because the model did not answer, but what
  // ended it was the client closing the connection -- usually its own 300 s or
  // 600 s idle watchdog. Its 300-600 s latency measures the wait, not the
  // model, so it gets a row of its own instead of moving that model's p50.
  interrupted: "client hung up",
};

/* What each of the four kinds of cancellation is called, and what it means.
 *
 * Mirrors `core/cancelled_reasons.py` exactly, and
 * `test_the_dashboard_and_the_store_agree_on_the_sub_labels` is what keeps the
 * two spellings one. Every label is derived from columns the log already had,
 * so a row written a year ago carries one too. */
const CANCEL_REASON_LABELS = {
  server_restart: "server restart",
  stopped_mid_answer: "stopped mid-answer",
  client_gave_up_waiting: "client gave up waiting",
  committed_then_silent: "committed, then silent",
};

const CANCEL_REASON_EXPLANATIONS = {
  server_restart:
    "MCC stopped or restarted while this stream was still open, so the stream" +
    " was cut at the shutdown drain deadline rather than by the client.",
  stopped_mid_answer:
    "Part of the answer had already been delivered when the connection" +
    " closed - normally the user stopping the stream, or the client giving up" +
    " during a long gap between chunks.",
  client_gave_up_waiting:
    "Nothing the client could read ever arrived: MCC was still working" +
    " through the route when the client's own idle timer ended the request.",
  committed_then_silent:
    "MCC had sent the start of the message and then nothing more arrived from" +
    " the model; the client's own idle timer ended it.",
};

/* Biggest first is deliberately not the order: these four are a story --
 * nothing arrived, something arrived and then stopped, the answer was cut
 * short, and MCC itself was what ended it -- and a table that reorders itself
 * between polls is one nobody can read twice. */
const CANCEL_REASON_ORDER = [
  "client_gave_up_waiting",
  "committed_then_silent",
  "stopped_mid_answer",
  "server_restart",
];

/** The chip words for a row's sub-label, or "" when the row is not cancelled. */
function cancelReasonLabel(row) {
  const reason = row && row.cancel_reason;
  if (!reason) return "";
  return CANCEL_REASON_LABELS[reason] || String(reason);
}

/* The sentence under the chip, with the silence measured where it can be.
 *
 * `duration_ms - ttft_ms` is how long MCC held the response open and sent
 * nothing more, which is the whole point of the `committed, then silent`
 * label. Either half missing means unmeasured and the sentence simply stops,
 * rather than claiming a zero: "it went silent instantly" is a measurement
 * nobody made. */
function cancelReasonSentence(row) {
  const reason = row && row.cancel_reason;
  if (!reason) return "";
  const base = CANCEL_REASON_EXPLANATIONS[reason] || "";
  if (reason !== "committed_then_silent") return base;
  const ttft = Number(row.ttft_ms);
  const duration = Number(row.duration_ms);
  if (!Number.isFinite(ttft) || !Number.isFinite(duration)) return base;
  const silent = duration - ttft;
  if (!(silent > 0)) return base;
  return `${base} Nothing arrived for ${formatChainDuration(silent)} after` +
    " that first frame.";
}

/* Which latency group an attempt belongs to, spelled the same way the store
 * spells it in `_LATENCY_OUTCOME_GROUP_SQL`: `interrupted` wins over the
 * stored `failed`, everything else is the stored outcome. */
function chainOutcomeGroup(attempt) {
  if (!attempt) return "skipped";
  if (attempt.error_kind === "interrupted") return "interrupted";
  return attempt.outcome || "skipped";
}

/** The chip itself, so the list and the modal cannot draw it differently. */
function buildCancelReasonChip(row) {
  const label = cancelReasonLabel(row);
  if (!label) return null;
  const chip = document.createElement("span");
  chip.className = "cancel-reason-chip";
  chip.dataset.reason = String(row.cancel_reason);
  chip.textContent = label;
  chip.title = cancelReasonSentence(row);
  return chip;
}

/** Attempt durations span milliseconds to ten minutes, so the unit moves. */
function formatChainDuration(ms) {
  const seconds = Number(ms) / 1000;
  if (!Number.isFinite(seconds)) return "";
  if (seconds < 1) return `${Math.round(Number(ms))} ms`;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.round(seconds - minutes * 60)}s`;
}

/** One line naming what arrived, whether or not its pixels were kept. */
function formatImageSummary(row) {
  const count = Number(row.input_image_count || 0);
  if (!count) return "";
  const images = row.input_images || [];
  const kinds = new Set(images.map((image) => image.kind).filter(Boolean));
  const bytes = images.reduce((total, image) => total + (image.source_bytes || 0), 0);
  const noun = kinds.size === 1 && kinds.has("document") ? "document" : "image";
  const label = count === 1 ? noun : `${count} ${noun}s`;
  return bytes ? `${label} · ${formatImageBytes(bytes)}` : label;
}

function formatImageBytes(bytes) {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

/**
 * Say plainly how the picture travelled.
 *
 * Before 6.49.0 an image a tool returned was flattened into base64 text and
 * billed at roughly a token per byte for something the model never saw. NULL
 * means the row predates the marker, which is not the same as "nothing was
 * sent", so it gets no sentence at all rather than a reassuring one.
 */
/* What the vision adapter's own describe calls cost, kept apart from the
 * answering model's tokens on purpose.
 *
 * The line above this one measures the model that answered the client, and it
 * has measured exactly that since the request log existed. Folding a describe
 * hop into it would silently change what every historical chart means without
 * changing a single stored number, so the adapter gets its own line and an
 * explicit "+". Absent when nothing was measured -- which is every request
 * that ran no describe call, and every row written before 6.53.0.
 */
function formatAdapterTokens(row) {
  const tokensIn = row?.adapter_tokens_in;
  const tokensOut = row?.adapter_tokens_out;
  if (tokensIn == null && tokensOut == null) return "";
  return `+ ${formatOptionalNumber(tokensIn)} / ${formatOptionalNumber(tokensOut)}`;
}

/* What the proxy expected this request to cost, and how much of it was images.
 *
 * Stored so the estimator can be audited against a real bill rather than
 * trusted. Only requests that carried a picture carry an estimate: a text-only
 * request has nothing here to check, and a full token pass on every request
 * would be real CPU on the response path for no answer.
 */
function formatEstimatedInput(row) {
  const estimated = row?.est_tokens_in;
  if (estimated == null) return "";
  const images = row?.est_image_tokens;
  if (images == null) return formatOptionalNumber(estimated);
  return `${formatOptionalNumber(estimated)} (${formatOptionalNumber(images)} images)`;
}

/* The resize, when there was one: what arrived, and what actually left.
 *
 * Read off the pictures rather than off the request, because that is where the
 * before and after live -- ``sent_width``/``sent_height`` are NULL on an image
 * that was sent as it arrived, which is a different fact from "it was sent at
 * its stored size" and the only one worth a sentence.
 */
function formatImageResize(images, row) {
  // Gated on the request's own byte record, not only on the pictures: the
  // sent size lives on the image, one image is shared by every turn that
  // re-sent it, and a request that resized nothing must not inherit a
  // sentence from one that did.
  if (row?.image_bytes_in == null) return "";
  const resized = (images || []).filter(
    (image) => image && image.sent_width && image.sent_height,
  );
  if (!resized.length) return "";
  const shapes = resized
    .map(
      (image) =>
        `${image.width}\u00d7${image.height} \u2192 ${image.sent_width}\u00d7${image.sent_height}`,
    )
    .join(", ");
  const before = Number(row?.image_bytes_in || 0);
  const after = Number(row?.image_bytes_out || 0);
  const bytes =
    before > 0 && after > 0
      ? ` (${formatImageBytes(before)} \u2192 ${formatImageBytes(after)})`
      : "";
  return ` Resized before sending: ${shapes}${bytes}.`;
}

function formatImageDelivery(delivery, count) {
  const plural = count === 1 ? "it was" : "they were";
  switch (delivery) {
    case "image":
      return `Sent to the model as ${count === 1 ? "an image" : "images"}.`;
    case "stripped":
      return `Omitted: this model does not accept images, so ${plural} replaced by a note saying so.`;
    case "described":
      return `Described by another model: ${
        count === 1 ? "it was" : "they were"
      } replaced by the text below, and the model this route picked answered.`;
    case "text":
      return "Sent as base64 text — this model saw characters, not a picture.";
    default:
      return "";
  }
}

/**
 * Show what the model was actually looking at.
 *
 * Only a downscaled copy is stored, so a thumbnail is the whole picture rather
 * than a link to one; clicking opens it at its stored size. An image whose
 * pixels were not kept (capture disabled, or a format the decoder refused)
 * still gets a row, because "an image arrived" is the fact that matters for
 * reading the route beneath it.
 */
function renderRequestImages(row) {
  const container = byId("reqDetailImages");
  if (!container) return;
  container.innerHTML = "";
  const images = row.input_images || [];
  if (!images.length) {
    container.hidden = true;
    return;
  }
  container.hidden = false;
  const heading = document.createElement("h4");
  heading.textContent = images.length === 1 ? "Image input" : `Image input (${images.length})`;
  container.appendChild(heading);
  const delivery =
    formatImageDelivery(row.image_delivery, images.length) +
    formatImageResize(images, row);
  if (delivery.trim()) {
    const note = document.createElement("p");
    note.className = "req-image-delivery";
    note.textContent = delivery;
    container.appendChild(note);
  }
  const described = describedShas(row);
  const grid = document.createElement("div");
  grid.className = "req-image-grid";
  images.forEach((image, index) => {
    grid.appendChild(buildRequestImage(image, index, described));
  });
  container.appendChild(grid);
}

/** Which of this request's pictures were described by a call it paid for.
 *
 * A cached description writes no attempt row and makes no upstream call, so
 * the presence of a describe attempt for an image is exactly the difference
 * between "this request paid for these words" and "they were already known".
 * Reading it off the attempts rather than storing a second flag keeps one
 * fact in one place.
 */
function describedShas(row) {
  const shas = new Set();
  (row.route_attempts || []).forEach((attempt) => {
    const params = attempt.params || {};
    if (params.kind === "describe" && params.image_sha) shas.add(params.image_sha);
  });
  return shas;
}

function buildRequestImage(image, index, describedShas) {
  const figure = document.createElement("figure");
  figure.className = "req-image";
  const source = requestImageSource(image);
  if (source) {
    const link = document.createElement("a");
    link.href = source;
    link.target = "_blank";
    link.rel = "noopener";
    const img = document.createElement("img");
    img.src = source;
    img.alt = `Image ${index + 1} sent with this request`;
    img.loading = "lazy";
    link.appendChild(img);
    figure.appendChild(link);
  } else {
    const placeholder = document.createElement("div");
    placeholder.className = "req-image-missing";
    placeholder.textContent = "no preview stored";
    figure.appendChild(placeholder);
  }
  const caption = document.createElement("figcaption");
  const parts = [];
  if (image.media_type) parts.push(image.media_type.replace(/^(image|application)\//, ""));
  if (image.width && image.height) parts.push(`${image.width}×${image.height}`);
  if (image.source_bytes) parts.push(formatImageBytes(image.source_bytes));
  caption.textContent = parts.join(" · ") || image.kind || "image";
  figure.appendChild(caption);
  const description = buildImageDescription(image, describedShas);
  if (description) figure.appendChild(description);
  return figure;
}

/** What the model actually received in this picture's place.
 *
 * Collapsed, because a description is a paragraph and the grid is a grid; open
 * it and you can read the sentence the coding model read. It is the only way
 * to judge whether describe mode is worth what it costs, which is why it is
 * here rather than only in the log file.
 */
function buildImageDescription(image, describedShas) {
  if (!image.description) return null;
  const details = document.createElement("details");
  details.className = "req-image-description";
  const summary = document.createElement("summary");
  const fresh = describedShas && describedShas.has(image.sha256);
  const by = image.described_by ? ` by ${image.described_by}` : "";
  summary.textContent = `Described${by} · ${fresh ? "fresh" : "cached"}`;
  details.appendChild(summary);
  const body = document.createElement("p");
  body.className = "req-image-description-text";
  body.textContent = image.description;
  details.appendChild(body);
  return details;
}

function requestImageSource(image) {
  if (!image.thumbnail_base64) return "";
  const type = image.thumbnail_media_type || "image/webp";
  return `data:${type};base64,${image.thumbnail_base64}`;
}

/**
 * Fill the prompt / reasoning / tool calls / response panes.
 *
 * Emptiness is not one condition. A pane can be empty because the turn had
 * nothing of that kind, or because body capture is off — those need different
 * words, and the character counts are recorded either way, so we can tell.
 */
function renderTurnTranscript(row) {
  const setBody = (bodyId, metaId, text, chars, emptyText) => {
    const body = byId(bodyId);
    const captured = typeof text === "string" && text !== "";
    body.textContent = captured
      ? text
      : chars
        ? `${chars.toLocaleString()} characters were recorded but not stored. Set REQUEST_LOG_CAPTURE_BODIES=true to keep the text.`
        : emptyText;
    body.classList.toggle("turn-empty-body", !captured);
    byId(metaId).textContent = formatChars(chars);
  };

  setBody(
    "reqDetailInput",
    "reqDetailInputMeta",
    row.input_text,
    row.input_chars,
    "No prompt text recorded.",
  );
  setBody(
    "reqDetailOutput",
    "reqDetailOutputMeta",
    row.output_text,
    row.output_chars,
    row.tool_call_count
      ? "This turn called tools without writing a reply."
      : "No reply text in this turn.",
  );

  const thinkingPane = byId("reqDetailThinkingPane");
  thinkingPane.hidden = !row.thinking_chars;
  if (row.thinking_chars) {
    thinkingPane.open = false;
    setBody(
      "reqDetailThinking",
      "reqDetailThinkingMeta",
      row.thinking_text,
      row.thinking_chars,
      "No reasoning recorded.",
    );
  }

  renderToolCalls(row);
}

function renderToolCalls(row) {
  const pane = byId("reqDetailToolsPane");
  const list = byId("reqDetailTools");
  list.replaceChildren();
  const count = row.tool_call_count || 0;
  pane.hidden = count === 0;
  if (count === 0) return;
  byId("reqDetailToolsMeta").textContent = count === 1 ? "1 call" : `${count} calls`;

  const calls = Array.isArray(row.tool_calls) ? row.tool_calls : null;
  if (!calls) {
    const note = document.createElement("p");
    note.className = "turn-empty";
    note.textContent =
      "Arguments were not stored. Set REQUEST_LOG_CAPTURE_BODIES=true to keep them.";
    list.append(note);
    return;
  }

  calls.forEach((call) => {
    const item = document.createElement("li");
    item.className = "tool-call";

    const head = document.createElement("div");
    head.className = "tool-call-head";
    const ordinal = document.createElement("span");
    ordinal.className = "tool-call-ordinal";
    const name = document.createElement("code");
    name.className = "tool-call-name";
    name.textContent = call.name || "(unnamed tool)";
    head.append(ordinal, name);

    const args = document.createElement("pre");
    args.className = "tool-call-args";
    if (typeof call.input_partial === "string") {
      // The stream ended mid-arguments, so this is a fragment, not JSON.
      args.classList.add("tool-call-partial");
      args.textContent = `${call.input_partial}\n\n— arguments incomplete, the stream ended early —`;
    } else {
      args.textContent = JSON.stringify(call.input ?? {}, null, 2);
    }

    item.append(head, args);
    list.append(item);
  });
}

function clearChart(canvas) {
  const context = canvas.getContext("2d");
  context.clearRect(0, 0, canvas.width, canvas.height);
}

function closeRequestDetail() {
  byId("reqDetailModal").hidden = true;
  if (reqState.detailReturnFocus instanceof HTMLElement) {
    reqState.detailReturnFocus.focus();
  }
  reqState.detailReturnFocus = null;
}

function trapRequestDetailFocus(event) {
  const modal = byId("reqDetailModal");
  if (event.key !== "Tab" || modal.hidden) return;
  const focusable = Array.from(
    modal.querySelectorAll(
      // `summary` is tabbable without carrying a tabindex attribute, so it has
      // to be named explicitly or the reasoning pane becomes unreachable.
      'button:not([disabled]), summary, [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  ).filter(
    (element) =>
      element instanceof HTMLElement &&
      !element.hidden &&
      // Panes are hidden when the turn had no reasoning or no tool calls.
      element.closest("[hidden]") === null,
  );
  if (focusable.length === 0) {
    event.preventDefault();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

/* ------------------------------------------------------------- export window */

// Field definitions per scope, in display order. The ids are the ones the
// export endpoint accepts; the labels are what the user reads.
const EXPORT_FIELDS = {
  requests: [
    { id: "input", label: "Input" },
    { id: "output", label: "Output" },
    { id: "tool_calls", label: "Tool calls" },
    { id: "thinking", label: "Thinking" },
    { id: "providers", label: "Provider" },
    { id: "models", label: "Model" },
    { id: "error_rate", label: "Error rate" },
    { id: "cache_hit", label: "Cache hit" },
    { id: "total_input", label: "Total input" },
    { id: "input_cached", label: "Input cached" },
    { id: "input_uncached", label: "Input uncached" },
    { id: "tokens_out", label: "Tokens out" },
    { id: "turns_with_tools", label: "Turns with tools" },
    { id: "ladder", label: "Upstream retry ladder" },
    { id: "tool_catalogue", label: "Tool catalogue" },
  ],
  /* One row per attempt rather than per request. Structural columns -- the
     request's id, time, harness, endpoint, models and status, and the
     attempt's own provider, model, outcome, key and three latencies -- are
     always present; these are the groups on top of them. Mirrors
     core/export.py::ATTEMPT_FIELD_IDS. */
  attempts: [
    { id: "failure", label: "Failure and skip reason" },
    { id: "tokens", label: "Attempt tokens" },
    { id: "cost", label: "Attempt cost" },
    { id: "ladder", label: "Upstream retry ladder" },
    { id: "wire", label: "Wire surface and credential" },
    { id: "recovery", label: "Stream recovery counters" },
  ],
  websearch: [
    { id: "provider", label: "Provider" },
    { id: "key_label", label: "Key" },
    { id: "query", label: "Query" },
    { id: "results_count", label: "Results" },
    { id: "duration_ms", label: "Duration (ms)" },
    { id: "status", label: "Status" },
    { id: "cost_usd", label: "Cost (USD)" },
    { id: "error_kind", label: "Error kind" },
    { id: "error_message", label: "Error message" },
    { id: "attempt_number", label: "Attempt #" },
    { id: "route_id", label: "Route" },
    { id: "input", label: "Input" },
    { id: "output", label: "Output" },
    { id: "provider_config", label: "Provider config" },
    { id: "content_captured", label: "Content captured" },
  ],
};

const EXPORT_DEFAULT_FIELDS = {
  requests: new Set([
    "providers",
    "models",
    "error_rate",
    "cache_hit",
    "total_input",
    "input_cached",
    "input_uncached",
    "tokens_out",
    "turns_with_tools",
  ]),
  attempts: new Set(["failure", "tokens", "cost", "ladder"]),
  websearch: new Set(["provider", "status", "results_count", "duration_ms", "cost_usd"]),
};

const EXPORT_DEFAULT_PERIOD = { requests: "86400", attempts: "86400", websearch: "604800" };
let exportReturnFocus = null;

function exportScope() {
  const checked = document.querySelector('input[name="exportScope"]:checked');
  return checked ? checked.value : "requests";
}

function renderExportFieldList(scope) {
  const container = byId("exportFieldList");
  container.innerHTML = "";
  const defaults = EXPORT_DEFAULT_FIELDS[scope] || new Set();
  (EXPORT_FIELDS[scope] || []).forEach((field) => {
    const label = document.createElement("label");
    label.className = "export-field";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = field.id;
    checkbox.checked = defaults.has(field.id);
    const span = document.createElement("span");
    span.textContent = field.label;
    label.append(checkbox, span);
    container.appendChild(label);
  });
}

function populateExportFilterOptions() {
  // Reuse the request stats' provider/model/key option sets so the export
  // filter offers the same suggestions as the requests view.
  const populate = (id, known) => {
    const datalist = byId(id);
    if (!datalist) return;
    datalist.replaceChildren(
      ...Array.from(known)
        .sort((left, right) => left.localeCompare(right))
        .map((value) => {
          const option = document.createElement("option");
          option.value = value;
          return option;
        }),
    );
  };
  populate("exportProviderOptions", reqState.providerOptions);
  populate("exportModelOptions", reqState.modelOptions);
}

function syncExportFilterVisibility(scope) {
  // Web Search has no models; hide the model filter for that scope.
  const modelWrap = byId("exportModelFilterWrap");
  if (modelWrap) modelWrap.hidden = scope === "websearch";
  /* Route attempts is detail-only. Grouping it would average latency across
     attempts of different models inside one request, which is the number the
     per-model cards already answer properly -- so the control goes away
     rather than offering a shape the server rejects. */
  const groupWrap = byId("exportGroupByWrap");
  if (groupWrap) groupWrap.hidden = scope === "attempts";
  if (scope === "attempts") byId("exportGroupBy").value = "";
}

function openExportModal(scopeOverride) {
  exportReturnFocus = document.activeElement;
  // Default scope to the view the user opened from, unless a button asked for
  // a specific one.
  const initial =
    typeof scopeOverride === "string"
      ? scopeOverride
      : state.activeView === "web_search"
        ? "websearch"
        : "requests";
  const scopeRadios = document.querySelectorAll('input[name="exportScope"]');
  scopeRadios.forEach((radio) => {
    radio.checked = radio.value === initial;
  });
  byId("exportFormat").value = "json";
  byId("exportPeriod").value = EXPORT_DEFAULT_PERIOD[initial];
  byId("exportCustomRange").hidden = true;
  byId("exportGroupBy").value = "";
  renderExportFieldList(initial);
  syncExportFilterVisibility(initial);
  populateExportFilterOptions();
  byId("exportProviderFilter").value = "";
  byId("exportModelFilter").value = "";
  byId("exportHint").textContent = "";
  byId("exportModal").hidden = false;
  byId("exportDownloadButton").focus();
}

function closeExportModal() {
  byId("exportModal").hidden = true;
  if (exportReturnFocus instanceof HTMLElement) {
    exportReturnFocus.focus();
  }
  exportReturnFocus = null;
}

function trapExportModalFocus(event) {
  const modal = byId("exportModal");
  if (event.key !== "Tab" || modal.hidden) return;
  const focusable = Array.from(
    modal.querySelectorAll(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  ).filter(
    (element) =>
      element instanceof HTMLElement && !element.hidden && element.closest("[hidden]") === null,
  );
  if (focusable.length === 0) {
    event.preventDefault();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function exportPeriodSeconds() {
  const raw = byId("exportPeriod").value;
  // "all" (lifetime) and "custom" both mean "no fixed window": the caller
  // sends no since, or the custom bounds instead.
  if (raw === "all" || raw === "custom") return null;
  return Number(raw) || 0;
}

function exportCustomSince() {
  const value = byId("exportSince").value;
  if (!value) return null;
  return new Date(value).toISOString();
}

function exportCustomUntil() {
  const value = byId("exportUntil").value;
  if (!value) return null;
  return new Date(value).toISOString();
}

function exportParamsFor(scope) {
  const params = new URLSearchParams();
  params.set("format", byId("exportFormat").value);
  params.set("scope", scope);
  const groupBy = byId("exportGroupBy").value;
  if (groupBy) params.set("group_by", groupBy);
  const fields = Array.from(byId("exportFieldList").querySelectorAll("input:checked"))
    .map((input) => input.value);
  if (fields.length) params.set("fields", fields.join(","));
  const provider = byId("exportProviderFilter").value.trim();
  if (provider) params.set("provider", provider);
  const model = byId("exportModelFilter").value.trim();
  if (model) params.set("model", model);
  const periodSeconds = exportPeriodSeconds();
  if (periodSeconds !== null) {
    params.set("since", String(Math.floor(Date.now() / 1000) - periodSeconds));
  } else if (byId("exportPeriod").value === "custom") {
    const since = exportCustomSince();
    if (since) params.set("since", since);
    const until = exportCustomUntil();
    if (until) params.set("until", until);
  }
  return params;
}

async function runExport() {
  const scope = exportScope();
  const params = exportParamsFor(scope);
  // Carry the current view's filters into the export.
  if (scope === "websearch") {
    const ws = webSearchAnalyticsParams({});
    ws.forEach((value, key) => {
      if (!params.has(key)) params.set(key, value);
    });
    params.set("include_content", "true");
  } else {
    reqFilters().forEach((value, key) => {
      if (!params.has(key)) params.set(key, value);
    });
  }
  byId("exportHint").textContent = "Preparing export…";
  try {
    const response = await fetch(`/admin/api/export?${params}`, {
      cache: "no-store",
      headers: { Accept: "application/octet-stream" },
    });
    if (!response.ok) {
      let detail = response.statusText;
      try {
        const body = await response.json();
        detail = body.detail || detail;
      } catch (_) {
        /* non-JSON error body */
      }
      throw new Error(detail || `Export failed (${response.status})`);
    }
    const blob = await response.blob();
    const filename = `mcc-${scope}-${params.get("format")}-${new Date()
      .toISOString()
      .replace(/[:.]/g, "-")}.${params.get("format")}`;
    downloadBlob(filename, blob);
    byId("exportHint").textContent = "Export downloaded.";
  } catch (error) {
    byId("exportHint").textContent = "";
    throw error;
  }
}

function downloadBlob(filename, blob) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function downloadJson(filename, value) {
  const blob = new Blob([`${JSON.stringify(value, null, 2)}\n`], {
    type: "application/json",
  });
  downloadBlob(filename, blob);
}

function requestAutoRefreshEnabled() {
  return byId("reqAutoRefresh").checked;
}

/**
 * Poll the cheap heartbeat endpoint instead of the full stats+list view.
 * Only when the row count or latest timestamp actually moved does this fall
 * through to `loadRequestsView()`, so an idle dashboard stops running the
 * aggregate queries (percentiles, breakdowns, series) on every tick.
 */
async function pollRequestPulse() {
  if (!requestAutoRefreshEnabled()) return;
  if (state.activeView !== "requests") return;
  // A hidden tab must not poll at all, not just skip the expensive call.
  if (document.visibilityState === "hidden") return;
  const params = reqFilters();
  let pulse;
  try {
    pulse = await api(`/admin/api/requests/pulse?${params}`);
  } catch (error) {
    showMessage(error.message, "error");
    return;
  }
  if (pulse.enabled === false) return;
  const signature = params.toString();
  const first =
    reqState.lastPulseTotal === null || signature !== reqState.lastPulseFilters;
  reqState.lastPulseFilters = signature;
  const changed =
    pulse.total !== reqState.lastPulseTotal || pulse.last_ts !== reqState.lastPulseTs;
  reqState.lastPulseTotal = pulse.total;
  reqState.lastPulseTs = pulse.last_ts;
  // The first tick only establishes the baseline; the view was just loaded.
  if (first || !changed) return;
  loadRequestsView().catch((error) => showMessage(error.message, "error"));
}

function updateRequestAutoRefresh() {
  if (reqState.autoRefreshTimer != null) {
    window.clearInterval(reqState.autoRefreshTimer);
    reqState.autoRefreshTimer = null;
  }
  if (!requestAutoRefreshEnabled()) return;
  const intervalMs = Number(byId("reqAutoRefreshInterval").value) || 15000;
  reqState.autoRefreshTimer = window.setInterval(() => {
    pollRequestPulse();
  }, intervalMs);
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && requestAutoRefreshEnabled()) {
    // Catch up immediately instead of waiting out the rest of the interval.
    pollRequestPulse();
  }
});

byId("reqDetailClose").addEventListener("click", closeRequestDetail);
byId("reqDetailModal").addEventListener("click", (event) => {
  if (event.target === byId("reqDetailModal")) closeRequestDetail();
});
document.addEventListener("keydown", (event) => {
  trapRequestDetailFocus(event);
  if (event.key === "Escape" && !byId("reqDetailModal").hidden) {
    closeRequestDetail();
  }
});
/**
 * Re-run the analytics query from page 1 and remember the filters.
 *
 * Every filter control routes through here, so a changed question always
 * produces a matching answer without a second click. `loadRequestsView`
 * already discards stale responses by `loadId`, so overlapping loads from
 * fast typing settle on the last one.
 */
function applyReqFilters() {
  reqState.offset = 0;
  persistDashboardState();
  loadRequestsView().catch((error) => showMessage(error.message, "error"));
}

byId("reqApplyFilters").addEventListener("click", applyReqFilters);

// Selects commit on change; a select has no half-typed state to wait for.
["reqFilterStatus", "reqFilterWindow", "reqFilterLocal"].forEach((id) => {
  byId(id).addEventListener("change", applyReqFilters);
});

// Text inputs wait for a pause instead of querying per keystroke: 400 ms is
// long enough that a typed provider name is one query, short enough that it
// still feels like the view is following along.
let reqFilterTypingTimer = null;
[
  "reqFilterProvider",
  "reqFilterModel",
  "reqFilterKey",
  "reqFilterHarness",
  "reqFilterSearch",
  "reqFilterEndpoint",
].forEach(
  (id) => {
    byId(id).addEventListener("input", () => {
      if (reqFilterTypingTimer) window.clearTimeout(reqFilterTypingTimer);
      reqFilterTypingTimer = window.setTimeout(() => {
        reqFilterTypingTimer = null;
        applyReqFilters();
      }, 400);
    });
  },
);
byId("reqClearFilters").addEventListener("click", () => {
  // Reset every analytics filter to its default and reload, so the view
  // returns to "show everything" without a manual page refresh.
  byId("reqFilterProvider").value = "";
  byId("reqFilterModel").value = "";
  byId("reqFilterKey").value = "";
  byId("reqFilterHarness").value = "";
  byId("reqFilterSearch").value = "";
  byId("reqFilterStatus").value = "";
  byId("reqFilterEndpoint").value = "";
  // Back to the dashboard default, not to "show everything": locally answered
  // rows are hidden unless the reader asked for them.
  byId("reqFilterLocal").value = "hide";
  byId("reqFilterWindow").value = "";
  byId("reqPageSize").value = "25";
  reqState.limit = 25;
  // A pending keystroke from the box we just emptied would otherwise fire
  // straight after this reload and query the same thing again.
  if (reqFilterTypingTimer) {
    window.clearTimeout(reqFilterTypingTimer);
    reqFilterTypingTimer = null;
  }
  applyReqFilters();
});
byId("reqFilterSearch").addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  // Enter is an explicit commit: don't make it wait out the debounce.
  if (reqFilterTypingTimer) {
    window.clearTimeout(reqFilterTypingTimer);
    reqFilterTypingTimer = null;
  }
  applyReqFilters();
});
byId("reqPrevPage").addEventListener("click", () => {
  reqState.offset = Math.max(0, reqState.offset - reqState.limit);
  loadRequestsView().catch((error) => showMessage(error.message, "error"));
});
byId("reqNextPage").addEventListener("click", () => {
  reqState.offset += reqState.limit;
  loadRequestsView().catch((error) => showMessage(error.message, "error"));
});
byId("reqPageSize").addEventListener("change", () => {
  reqState.limit = Number(byId("reqPageSize").value);
  applyReqFilters();
});
byId("reqRefreshButton").addEventListener("click", () =>
  loadRequestsView().catch((error) => showMessage(error.message, "error")),
);
byId("reqAutoRefresh").addEventListener("change", () => {
  updateRequestAutoRefresh();
  if (requestAutoRefreshEnabled()) pollRequestPulse();
  persistDashboardState();
});
byId("reqAutoRefreshInterval").addEventListener("change", () => {
  updateRequestAutoRefresh();
  persistDashboardState();
});
// Must match ``REQUEST_LOG_CLEAR_CONFIRMATION`` in api/admin_routes.py;
// ``tests/contracts/test_config_dir_is_single_sourced.py`` pins the two.
const REQUEST_LOG_CLEAR_CONFIRMATION = "delete-all-request-log-rows";
// Must match ``IMAGE_DESCRIPTION_CLEAR_CONFIRMATION`` in api/admin_routes.py.
const IMAGE_DESCRIPTION_CLEAR_CONFIRMATION = "clear-all-image-descriptions";

const forgetAllLearnedButton = byId("reqForgetLearnedButton");
if (forgetAllLearnedButton) {
  forgetAllLearnedButton.addEventListener("click", () => {
    if (
      !window.confirm(
        "Forget every fact MCC has learned about every host? Caps, refusals " +
          "and probe results all go. Each one is re-learned the next time a " +
          "host says it again, at the cost of one rejected request per model.",
      )
    ) {
      return;
    }
    forgetLearnedFacts({}, forgetAllLearnedButton);
  });
}

byId("reqClearDescriptionsButton").addEventListener("click", () => {
  if (
    !window.confirm(
      "Forget every cached image description? The pictures and the requests " +
        "stay; the next request carrying one of them pays for a fresh " +
        "description.",
    )
  ) {
    return;
  }
  api(
    `/admin/api/requests/image-descriptions?confirm=${IMAGE_DESCRIPTION_CLEAR_CONFIRMATION}`,
    { method: "DELETE" },
  )
    .then((result) =>
      showMessage(
        `Cleared ${result.cleared} cached image description(s).`,
        "success",
      ),
    )
    .catch((error) => showMessage(error.message, "error"));
});

byId("reqExportButton").addEventListener("click", () => openExportModal("requests"));
/* The same window, opened on the attempt scope. A second button rather than
   only a third radio: the models that did not answer are the reason this
   export exists, and a reader who never opens the Scope row would never find
   them. */
byId("reqExportAttemptsButton").addEventListener("click", () =>
  openExportModal("attempts"),
);
byId("reqClearButton").addEventListener("click", () => {
  if (
    !window.confirm(
      `Delete the entire request log? The current filters match ${reqState.total} rows; all stored rows will be deleted.`,
    )
  ) {
    return;
  }
  // The confirmation literal is required by the route itself, not just by
  // the dialog above: a dialog only guards the path through this page.
  api(`/admin/api/requests?confirm=${REQUEST_LOG_CLEAR_CONFIRMATION}`, {
    method: "DELETE",
  })
    .then(() => {
      reqState.offset = 0;
      return loadRequestsView();
    })
    .catch((error) => showMessage(error.message, "error"));
});

/* ------------------------------------------------------------------ guide ---
   Screenshots in the guide are dashboard captures, so at column width the UI
   inside them is unreadable. They open at full size instead. */

let guideLightboxReturnFocus = null;

function openGuideLightbox(image) {
  const lightbox = byId("guideLightbox");
  const full = byId("guideLightboxImage");
  guideLightboxReturnFocus = document.activeElement;
  full.src = image.src;
  full.alt = image.alt || "";
  lightbox.hidden = false;
  byId("guideLightboxClose").focus();
}

function closeGuideLightbox() {
  const lightbox = byId("guideLightbox");
  if (lightbox.hidden) return;
  lightbox.hidden = true;
  byId("guideLightboxImage").src = "";
  if (guideLightboxReturnFocus instanceof HTMLElement) {
    guideLightboxReturnFocus.focus();
  }
  guideLightboxReturnFocus = null;
}

function setupGuideScreenshots() {
  document.querySelectorAll(".guide-shot").forEach((image) => {
    image.tabIndex = 0;
    image.setAttribute("role", "button");
    image.setAttribute(
      "aria-label",
      `${image.alt || "Screenshot"} — open at full size`,
    );
    image.addEventListener("click", () => openGuideLightbox(image));
    image.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      openGuideLightbox(image);
    });
    // The alt text already describes the shot; reuse it as a caption so the
    // click affordance is stated rather than implied.
    if (image.alt && !image.nextElementSibling?.classList.contains("guide-shot-caption")) {
      const caption = document.createElement("p");
      caption.className = "guide-shot-caption";
      caption.textContent = `${image.alt} — click to enlarge`;
      image.insertAdjacentElement("afterend", caption);
    }
  });
  byId("guideLightbox").addEventListener("click", closeGuideLightbox);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeGuideLightbox();
  });
}

/** Mark the section currently being read in an in-page contents rail. */
function setupScrollspy(railSelector) {
  const links = Array.from(document.querySelectorAll(`${railSelector} a`));
  if (links.length === 0) return;
  const byHash = new Map(links.map((link) => [link.getAttribute("href"), link]));
  const headings = links
    .map((link) => document.querySelector(link.getAttribute("href")))
    .filter(Boolean);
  if (headings.length === 0) return;

  const mark = (id) => {
    byHash.forEach((link, hash) => {
      if (hash === `#${id}`) {
        link.setAttribute("aria-current", "true");
      } else {
        link.removeAttribute("aria-current");
      }
    });
  };

  const seen = new Set();
  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          seen.add(entry.target.id);
        } else {
          seen.delete(entry.target.id);
        }
      });
      // Headings leave the band from the top as you scroll down, so the first
      // one still inside it is the section you are reading.
      const current = headings.find((heading) => seen.has(heading.id));
      if (current) mark(current.id);
    },
    { rootMargin: "-8% 0px -70% 0px", threshold: 0 },
  );
  headings.forEach((heading) => observer.observe(heading));
  mark(headings[0].id);
}

// Code blocks hold literal env vars, JSON and commands the reader is about to
// paste elsewhere -- retyping them by hand is exactly the friction this page
// exists to remove. 127.0.0.1 over plain http is a secure context under the
// browser's localhost exception, so navigator.clipboard is expected to work
// here despite the dashboard not being served over https; feature-detected
// anyway, and a rejected write fails quietly rather than breaking the page --
// the code stays selectable and readable either way.
function setupGuideCodeCopy() {
  if (!navigator.clipboard || !navigator.clipboard.writeText) return;
  document.querySelectorAll(".guide-body pre").forEach((block) => {
    const code = block.querySelector("code");
    if (!code) return;

    const button = document.createElement("button");
    button.type = "button";
    button.className = "guide-copy-button";
    button.textContent = "Copy";
    button.setAttribute("aria-label", "Copy code to clipboard");
    button.addEventListener("click", () => {
      navigator.clipboard
        .writeText(code.textContent)
        .then(() => {
          button.textContent = "Copied";
          button.classList.add("is-copied");
          window.setTimeout(() => {
            button.textContent = "Copy";
            button.classList.remove("is-copied");
          }, 1500);
        })
        .catch(() => {
          // Clipboard writes can fail on permissions or browser policy; the
          // reader can still select and copy the text by hand.
        });
    });
    block.appendChild(button);
  });
}

setupGuideScreenshots();
setupScrollspy(".guide-toc");
setupGuideCodeCopy();


/* ------------------------------------------------------------ token optimizer
   A ledger, read top to bottom: what you saved, what is saving it, what could
   save more. Nothing on this page is enabled by rendering it, and nothing here
   invents a number. A figure we could not read is an em dash; a figure we read
   as zero is a zero. Those are different facts and the page never merges them.

   The measured trimming figures below come from
   core/anthropic/tool_result_trimming.py, which holds the full table. They are
   restated here because the reader deciding whether to flip the switch is
   looking at this page, not at that docstring.                              */

const OPT_UNKNOWN = "—";

const optState = {
  stats: null,
  requestStats: null,
  rtk: null,
  rtkGain: null,
  candidates: null,
  candidatesError: null,
  loading: false,
  scanning: false,
};

function optNumber(value) {
  if (value == null || Number.isNaN(Number(value))) return OPT_UNKNOWN;
  return Number(value).toLocaleString();
}

/** Compact form for a headline figure. Exact figures live in the tables. */
function optCompact(value) {
  if (value == null || Number.isNaN(Number(value))) return OPT_UNKNOWN;
  const number = Number(value);
  const abs = Math.abs(number);
  if (abs >= 1e9) return `${(number / 1e9).toFixed(1)}B`;
  if (abs >= 1e6) return `${(number / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `${(number / 1e3).toFixed(1)}K`;
  return String(number);
}

function optKpi({ label, value, sub, unknown = false }) {
  const card = document.createElement("div");
  card.className = `opt-kpi${unknown ? " opt-kpi-unknown" : ""}`;
  const labelEl = document.createElement("div");
  labelEl.className = "opt-kpi-label";
  labelEl.textContent = label;
  const valueEl = document.createElement("div");
  valueEl.className = "opt-kpi-value";
  valueEl.textContent = value;
  const subEl = document.createElement("div");
  subEl.className = "opt-kpi-sub";
  subEl.textContent = sub;
  card.append(labelEl, valueEl, subEl);
  return card;
}

/** Dense table. A header may be a string or {label, right}. */
function optTable(headers, rows, emptyText) {
  const table = document.createElement("table");
  table.className = "opt-table";
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  headers.forEach((header) => {
    const th = document.createElement("th");
    const spec = typeof header === "string" ? { label: header } : header;
    th.textContent = spec.label;
    if (spec.right) th.className = "opt-r";
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  const tbody = document.createElement("tbody");
  if (rows.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = headers.length;
    td.className = "opt-empty";
    td.textContent = emptyText;
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
  rows.forEach((cells) => {
    const tr = document.createElement("tr");
    cells.forEach((cell, index) => {
      const td = document.createElement("td");
      const spec = headers[index];
      if (typeof spec === "object" && spec.right) td.className = "opt-r opt-num";
      if (cell instanceof Node) {
        td.replaceChildren(cell);
      } else {
        td.textContent = cell;
      }
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.append(thead, tbody);
  return table;
}

/** A sparkline and, always, the numbers behind it.
 *
 * Under four points there is no shape to read, so the numbers are shown on
 * their own rather than dressed up as a chart. Above that the bars carry the
 * shape and the <details> carries the values -- a chart with no numeric
 * equivalent is an accessibility gap this dashboard already has too much of.
 */
function optSparkline(points, { valueKey = "requests", label = "" } = {}) {
  const wrap = document.createElement("div");
  const values = points.map((point) => Number(point[valueKey] || 0));
  const peak = values.length ? Math.max(...values) : 0;

  if (points.length < 4) {
    const note = document.createElement("div");
    note.className = "opt-sub";
    note.textContent = points.length
      ? `${points.length} day${points.length === 1 ? "" : "s"} of history — too few to plot`
      : "No daily history yet";
    wrap.appendChild(note);
  } else {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "opt-spark");
    svg.setAttribute("viewBox", `0 0 ${points.length * 10} 38`);
    svg.setAttribute("role", "img");
    svg.setAttribute(
      "aria-label",
      `${label} over ${points.length} days, peak ${optNumber(peak)}`,
    );
    points.forEach((point, index) => {
      const value = Number(point[valueKey] || 0);
      const height = peak > 0 ? Math.max(1, Math.round((value / peak) * 38)) : 1;
      const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      rect.setAttribute("x", String(index * 10));
      rect.setAttribute("y", String(38 - height));
      rect.setAttribute("width", "8");
      rect.setAttribute("height", String(height));
      if (value === peak && peak > 0) rect.setAttribute("class", "opt-spark-hi");
      svg.appendChild(rect);
    });
    wrap.appendChild(svg);
  }

  if (points.length) {
    const details = document.createElement("details");
    details.className = "opt-data";
    const summary = document.createElement("summary");
    summary.textContent = "Show the numbers";
    details.append(
      summary,
      optTable(
        ["Day", { label: "Fired", right: true }, { label: "Tokens", right: true }],
        points
          .slice()
          .reverse()
          .map((point) => [
            point.bucket,
            optNumber(point.requests),
            optNumber(point.tokens_saved),
          ]),
        "No days recorded.",
      ),
    );
    wrap.appendChild(details);
  }
  return wrap;
}

function optPill(text, variant = "") {
  const pill = document.createElement("span");
  pill.className = `opt-pill${variant ? ` opt-pill-${variant}` : ""}`;
  const dot = document.createElement("i");
  dot.className = "opt-dot";
  pill.append(dot, document.createTextNode(text));
  return pill;
}

function optCode(text) {
  const code = document.createElement("code");
  code.textContent = text;
  return code;
}

function optEmpty(text) {
  const box = document.createElement("div");
  box.className = "opt-empty";
  box.textContent = text;
  return box;
}

async function loadOptimizerView({ force = false } = {}) {
  if (optState.loading) return;
  if (optState.stats && !force) {
    renderOptimizerView();
    return;
  }
  optState.loading = true;
  try {
    // Four independent reads. One failing must not blank the other three:
    // "RTK could not be asked" is a fact about RTK, not about the log.
    const [stats, requestStats, rtk, rtkGain] = await Promise.all([
      api("/admin/api/requests/optimization-stats").catch((error) => ({
        enabled: false,
        error: error.message,
      })),
      api("/admin/api/requests/stats").catch((error) => ({
        enabled: false,
        error: error.message,
      })),
      api("/admin/api/rtk").catch((error) => ({ error: error.message })),
      api("/admin/api/rtk/gain").catch((error) => ({
        available: false,
        reason: "run_failed",
        detail: error.message,
      })),
    ]);
    optState.stats = stats;
    optState.requestStats = requestStats;
    optState.rtk = rtk;
    optState.rtkGain = rtkGain;
  } finally {
    optState.loading = false;
  }
  renderOptimizerView();
}

function renderOptimizerView() {
  if (!byId("optKpis")) return;
  renderOptimizerKpis();
  renderOptimizerRules();
  renderOptimizerCandidates();
  renderOptimizerCache();
  syncOptimizerTrimControls();
}

/** Headline figures. Every one of them says where it stopped being knowable. */
function renderOptimizerKpis() {
  const container = byId("optKpis");
  if (!container) return;
  const stats = optState.stats || {};
  const scope = byId("optLedgerScope");
  const cards = [];

  if (stats.enabled === false) {
    scope.textContent = stats.error
      ? `The request log could not be read: ${stats.error}`
      : "Request logging is off, so there is nothing measured to show.";
    container.replaceChildren(
      optEmpty(
        "Turn on request logging to measure what the optimizer is doing. " +
          "Until then this page can only tell you what is switched on, not " +
          "what it saved.",
      ),
    );
    return;
  }

  const total = Number(stats.total_requests || 0);
  const locally = Number(stats.answered_locally || 0);
  scope.textContent = `${optNumber(total)} request${total === 1 ? "" : "s"} recorded · all-time`;

  cards.push(
    optKpi({
      label: "Tokens never sent",
      value: optCompact(stats.tokens_saved),
      sub: "by local rules, all-time",
    }),
  );
  cards.push(
    optKpi({
      label: "Requests answered locally",
      value: optNumber(locally),
      sub: total
        ? `${((locally / total) * 100).toFixed(1)}% of all traffic`
        : "no traffic recorded yet",
    }),
  );

  // RTK is a separate program. "Not installed" and "installed but reported
  // nothing" are different answers and are printed as different answers.
  const gain = optState.rtkGain || {};
  const rtk = optState.rtk || {};
  if (gain.available && gain.summary && gain.summary.total_saved != null) {
    cards.push(
      optKpi({
        label: "RTK savings",
        value: optCompact(gain.summary.total_saved),
        sub:
          gain.summary.avg_savings_pct != null
            ? `${Number(gain.summary.avg_savings_pct).toFixed(1)}% average, RTK's own figure`
            : "RTK's own figure",
      }),
    );
  } else {
    const reasons = {
      not_installed: "not installed",
      run_failed: "could not be run",
      empty_output: "reported nothing",
      invalid_json: "output could not be parsed",
      unexpected_schema: "output was not recognised",
      timeout: "did not answer in time",
    };
    cards.push(
      optKpi({
        label: "RTK savings",
        value: OPT_UNKNOWN,
        sub:
          reasons[gain.reason] ||
          (rtk.installed ? "no figure reported" : "not installed"),
        unknown: true,
      }),
    );
  }

  const trimming = optimizerTrimSummary();
  cards.push(
    optKpi({
      label: "Tool-result trimming",
      value: trimming.headline,
      sub: trimming.detail,
      unknown: !trimming.master,
    }),
  );

  container.replaceChildren(...cards);
}

/** What the trimming settings currently say. Read from the live controls. */
function optimizerTrimSummary() {
  const readField = (key) => {
    const input = document.querySelector(`[data-key="${key}"]`);
    if (input && input.matches("input, select, textarea")) {
      return effectiveControlValue(input);
    }
    const field = state.fields?.get(key);
    return field ? field.value || field.default || "" : "";
  };
  const master = readField("ENABLE_TOOL_RESULT_TRIMMING") === "true";
  const modes = ["READ", "GREP", "GLOB"].map((tool) =>
    readField(`TOOL_RESULT_TRIM_${tool}`),
  );
  const on = modes.filter((mode) => mode === "on").length;
  const observing = modes.filter((mode) => mode === "observe").length;
  if (!master) {
    return { master: false, headline: "off", detail: "master switch is off" };
  }
  if (on === 0 && observing === 0) {
    return { master: true, headline: "idle", detail: "every rule is off" };
  }
  if (on === 0) {
    return {
      master: true,
      headline: "observing",
      detail: `${observing} rule${observing === 1 ? "" : "s"} measuring, wire unchanged`,
    };
  }
  return {
    master: true,
    headline: "trimming",
    detail: `${on} rule${on === 1 ? "" : "s"} editing what the model sees`,
  };
}

function renderOptimizerRules() {
  const container = byId("optRules");
  if (!container) return;
  const stats = optState.stats || {};
  const rules = stats.rules || [];
  const rows = rules.map((rule) => {
    const name = document.createElement("div");
    const title = document.createElement("div");
    title.className = "opt-rule-name";
    title.textContent = rule.label || rule.rule;
    const description = document.createElement("div");
    description.className = "opt-sub";
    description.textContent = rule.description || "";
    name.append(title, description);

    let statePill;
    if (rule.enabled === true) statePill = optPill("on", "on");
    else if (rule.enabled === false) statePill = optPill("off");
    else statePill = optPill("retired", "warn");

    const answer = document.createElement("div");
    if (rule.answer == null) {
      answer.className = "opt-sub";
      answer.textContent = OPT_UNKNOWN;
    } else if (rule.answer === "") {
      answer.append(optCode('""'), document.createTextNode(" (nothing shown)"));
    } else {
      answer.appendChild(optCode(JSON.stringify(rule.answer)));
    }

    return [
      name,
      statePill,
      optNumber(rule.requests),
      // A rule that never fired saved an unknown amount, not zero.
      rule.tokens_saved == null ? OPT_UNKNOWN : optNumber(rule.tokens_saved),
      optSparkline(rule.daily || [], { label: rule.label || rule.rule }),
      answer,
    ];
  });

  container.replaceChildren(
    optTable(
      [
        "Rule",
        "State",
        { label: "Fired", right: true },
        { label: "Tokens avoided", right: true },
        `Last ${stats.series_days || 14} days`,
        "Answers with",
      ],
      rows,
      "No optimization rules are registered.",
    ),
  );

  const partial = rules.filter(
    (rule) =>
      Number(rule.requests || 0) > 0 &&
      Number(rule.tokens_reported || 0) < Number(rule.requests || 0),
  );
  if (partial.length) {
    const note = document.createElement("p");
    note.className = "opt-hint";
    note.textContent =
      "Some rows predate per-request savings accounting, so the tokens above " +
      "cover fewer requests than the fire count. The gap is not zero saving; " +
      "it is saving that was never written down.";
    container.appendChild(note);
  }
}

function renderOptimizerCandidates() {
  const container = byId("optCandidates");
  const scope = byId("optCandidatesScope");
  if (!container || !scope) return;
  if (optState.scanning) {
    container.replaceChildren(optEmpty("Scanning the log…"));
    return;
  }
  if (optState.candidatesError) {
    scope.textContent = "The scan could not be run.";
    container.replaceChildren(optEmpty(optState.candidatesError));
    return;
  }
  const result = optState.candidates;
  if (!result) {
    scope.textContent =
      "Recurring request families no rule covers. Nothing is scanned until you ask.";
    container.replaceChildren(
      optEmpty(
        "No scan has been run. A scan decompresses recent request bodies, " +
          "which costs seconds of CPU, so it happens on demand and never on a timer.",
      ),
    );
    return;
  }
  if (result.enabled === false) {
    scope.textContent = "Request logging is off, so there is nothing to scan.";
    container.replaceChildren(
      optEmpty("Turn on request logging to look for recurring request families."),
    );
    return;
  }

  const scanned = result.scanned || {};
  const parts = [
    `scanned ${optNumber(scanned.rows)} row${scanned.rows === 1 ? "" : "s"}`,
  ];
  if (scanned.elapsed_ms != null) {
    parts.push(`${(Number(scanned.elapsed_ms) / 1000).toFixed(1)} s`);
  }
  if (scanned.truncated) {
    parts.push(
      `bounded at ${optNumber(scanned.row_limit)} of ${optNumber(scanned.matching_rows)} matching — this is a sample`,
    );
  }
  if (Number(scanned.rows_without_prompt_text || 0) > 0) {
    parts.push(
      `${optNumber(scanned.rows_without_prompt_text)} rows carried no prompt text and could not be grouped`,
    );
  }
  if (result.capture_bodies === false) {
    parts.push(
      "body capture is off, so only rows written while it was on can be grouped",
    );
  }
  scope.textContent = parts.join(" · ");

  const rows = (result.candidates || []).map((family) => [
    optCode(family.signature),
    optNumber(family.requests),
    optNumber(family.tokens_total),
    optNumber(family.tokens_per_request),
    `${formatOptDate(family.first_seen)} – ${formatOptDate(family.last_seen)}`,
  ]);
  container.replaceChildren(
    optTable(
      [
        "Family",
        { label: "Requests", right: true },
        { label: "Tokens", right: true },
        { label: "Per request", right: true },
        "Seen",
      ],
      rows,
      "No recurring family in this scan is left uncovered by a rule.",
    ),
  );
  if (result.candidates_truncated) {
    const note = document.createElement("p");
    note.className = "opt-hint";
    note.textContent = `Showing ${optNumber((result.candidates || []).length)} of ${optNumber(result.candidates_total)} families.`;
    container.appendChild(note);
  }
}

function formatOptDate(epoch) {
  if (epoch == null) return OPT_UNKNOWN;
  return new Date(Number(epoch) * 1000).toLocaleDateString();
}

function renderOptimizerCache() {
  const container = byId("optCache");
  if (!container) return;
  const stats = optState.requestStats || {};
  if (stats.enabled === false) {
    container.replaceChildren(
      optEmpty(
        stats.error ||
          "Request logging is off, so cache effectiveness cannot be measured.",
      ),
    );
    return;
  }
  const rows = (stats.by_provider || []).map((row) => {
    const total = totalInputTokens(row);
    const cached = Number(row.cache_read_tokens || 0);
    const reported = row.cache_reported !== 0 && total > 0;
    const percent = reported ? (cached / total) * 100 : null;

    const bar = document.createElement("div");
    if (reported) {
      bar.className = "opt-bar";
      const fill = document.createElement("i");
      // The threshold is the measured trimming break-even, so this bar and the
      // warning at the bottom of the page are talking about the same line.
      if (percent < 90.9) fill.className = "opt-bar-low";
      fill.style.width = `${Math.max(0, Math.min(100, percent)).toFixed(0)}%`;
      bar.appendChild(fill);
    } else {
      bar.className = "opt-sub";
      bar.textContent = "reports no cache figures";
    }

    return [
      providerDisplayLabel(row.key) || UNKNOWN_PROVIDER_KEY,
      optNumber(row.requests),
      formatCacheHitRate(row),
      bar,
    ];
  });
  container.replaceChildren(
    optTable(
      ["Provider", { label: "Requests", right: true }, { label: "Cache hit", right: true }, ""],
      rows,
      "No provider traffic recorded yet.",
    ),
  );
}

/* -------------------------------------------------- trimming settings block */

/** Wrap a manifest field in a control shaped like the thing it controls.
 *
 * The real input from buildFieldControl() goes into the document hidden: the
 * shared dirty/apply machinery walks [data-key] and an input it cannot find is
 * an edit that is silently never saved. The visible control writes through to
 * it and dispatches `change`, so the dirty counter still counts one change per
 * setting rather than one per widget.
 */
/** Write a value into whichever control kind is behind a proxied widget. */
function setControlValue(input, value) {
  if (input.type === "checkbox") {
    input.checked = String(value).toLowerCase() === "true";
    return;
  }
  input.value = value;
}

function optProxiedField(field) {
  const { input, control } = buildFieldControl(field);
  const holder = document.createElement("div");
  holder.className = "opt-proxied-field";
  holder.appendChild(control);
  return { input, holder };
}

function optSwitch(field, describedBy) {
  const { input, holder } = optProxiedField(field);
  const button = document.createElement("button");
  button.type = "button";
  button.className = "opt-switch";
  button.setAttribute("aria-label", field.label);
  if (describedBy) button.setAttribute("aria-describedby", describedBy);
  button.disabled = Boolean(field.locked);
  const isOn = () => effectiveControlValue(input) === "true";
  const sync = () => button.setAttribute("aria-pressed", String(isOn()));
  button.addEventListener("click", () => {
    // Clicking the switch is a choice, so it writes an explicit value rather
    // than leaving the field unset at whatever the default happens to be.
    setControlValue(input, isOn() ? "false" : "true");
    input.dispatchEvent(new Event("change", { bubbles: true }));
    sync();
    renderOptimizerKpis();
    syncOptimizerTrimControls();
  });
  input.addEventListener("change", sync);
  sync();
  const wrap = document.createElement("div");
  wrap.append(holder, button);
  return wrap;
}

function optSegmented(field, options, label) {
  const { input, holder } = optProxiedField(field);
  const group = document.createElement("div");
  group.className = "opt-seg";
  group.setAttribute("role", "group");
  group.setAttribute("aria-label", label);
  const sync = () => {
    const current = effectiveControlValue(input);
    buttons.forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.state === current));
    });
  };
  const buttons = options.map(([value, text]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.state = value;
    button.textContent = text;
    button.disabled = Boolean(field.locked);
    button.addEventListener("click", () => {
      setControlValue(input, value);
      input.dispatchEvent(new Event("change", { bubbles: true }));
      sync();
      renderOptimizerKpis();
    });
    group.appendChild(button);
    return button;
  });
  input.addEventListener("change", sync);
  sync();
  const wrap = document.createElement("div");
  wrap.dataset.optSeg = field.key;
  wrap.append(holder, group);
  return wrap;
}

/** Per-tool controls do nothing while the master switch is off; say so by
 *  disabling them, rather than letting someone set a mode that has no effect
 *  and walk away believing they enabled something. */
function syncOptimizerTrimControls() {
  const master = document.querySelector('[data-key="ENABLE_TOOL_RESULT_TRIMMING"]');
  const note = byId("optPerToolNote");
  if (!master || !note) return;
  const on = effectiveControlValue(master) === "true";
  note.textContent = on
    ? "Each rule runs independently. Observe changes nothing on the wire."
    : "Disabled while the master switch is off.";
  document.querySelectorAll("[data-opt-seg] .opt-seg button").forEach((button) => {
    button.disabled = !on;
  });
}

const OPT_TRIM_MODES = [
  ["off", "Off"],
  ["observe", "Observe"],
  ["on", "On"],
];

const OPT_TOOL_NOTES = {
  TOOL_RESULT_TRIM_READ:
    "Observe records what it would cut and changes nothing on the wire.",
  TOOL_RESULT_TRIM_GREP:
    "Cuts on line boundaries, so every path:line:match stays intact.",
  TOOL_RESULT_TRIM_GLOB: "Path lists; there are no line numbers to preserve.",
};

/** The measured trimming result, stated on the page that offers the switch.
 *
 * These figures are the table in core/anthropic/tool_result_trimming.py. The
 * previous wording here was "unvalidated"; it has since been measured, and a
 * measured loss is a stronger thing to say than an unknown.
 */
function optimizerTrimWarning() {
  const note = document.createElement("div");
  note.className = "opt-note";

  const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  icon.setAttribute("class", "opt-note-icon");
  icon.setAttribute("width", "14");
  icon.setAttribute("height", "14");
  icon.setAttribute("viewBox", "0 0 24 24");
  icon.setAttribute("fill", "none");
  icon.setAttribute("stroke", "currentColor");
  icon.setAttribute("stroke-width", "2");
  icon.setAttribute("aria-hidden", "true");
  const triangle = document.createElementNS("http://www.w3.org/2000/svg", "path");
  triangle.setAttribute(
    "d",
    "M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z",
  );
  triangle.setAttribute("stroke-linejoin", "round");
  const stem = document.createElementNS("http://www.w3.org/2000/svg", "path");
  stem.setAttribute("d", "M12 9v4");
  stem.setAttribute("stroke-linecap", "round");
  const dot = document.createElementNS("http://www.w3.org/2000/svg", "path");
  dot.setAttribute("d", "M12 17h.01");
  dot.setAttribute("stroke-linecap", "round");
  icon.append(triangle, stem, dot);

  const heading = document.createElement("p");
  heading.className = "opt-note-heading";
  const strong = document.createElement("strong");
  strong.textContent = "Measured harmful at your cache rates.";
  heading.append(icon, strong);

  const body = document.createElement("p");
  body.className = "opt-note-body";
  body.textContent =
    "Trimming rewrites bytes in the middle of the prompt, and prompt caching " +
    "depends on those bytes not changing. Measured over a 24-turn session: at " +
    "the shipped protect-recent-results of 2, trimming costs 10.9% more fresh " +
    "input tokens than leaving it off, and switching it on mid-conversation " +
    "costs one near-total cache miss — 3.8% hit and 107,797 fresh tokens " +
    "on that turn. Break-even is a baseline cache hit rate of about 90.9%: " +
    "below that trimming wins, above it trimming loses. Check the cache table " +
    "above against that line, and run a rule in Observe before you run it On.";

  note.append(
    heading,
    body,
    optTable(
      [
        "Protect recent",
        { label: "Fresh tokens", right: true },
        { label: "Cache hit", right: true },
        { label: "Chars removed", right: true },
      ],
      [
        ["0", "72,897", "91.9%", "15,024,408"],
        ["1", "486,278", "62.5%", "13,781,644"],
        ["2 (shipped default)", "521,860", "69.1%", "12,561,418"],
        ["4", "955,949", "59.8%", "10,409,077"],
        ["trimming off", "470,648", "91.8%", "0"],
      ],
      "",
    ),
  );
  return note;
}

/** Custom renderer for the "optimizer" settings section.
 *
 * Registered like renderModelRouting: the generic field grid would render nine
 * unrelated boxes where the shape of the thing is a master switch, three
 * per-tool rules, and the numbers those rules use.
 */
function renderOptimizerSettings(fields) {
  const byKey = new Map(fields.map((field) => [field.key, field]));
  const wrap = document.createElement("div");
  const claimed = new Set();

  const master = byKey.get("ENABLE_TOOL_RESULT_TRIMMING");
  if (master) {
    claimed.add(master.key);
    const bar = document.createElement("div");
    bar.className = "opt-master";
    const text = document.createElement("div");
    const title = document.createElement("div");
    title.className = "opt-master-title";
    // The section heading above already names the feature; this names the
    // control, so the two lines do not say the same words twice.
    title.textContent = "Master switch";
    const detail = document.createElement("div");
    detail.className = "opt-master-detail";
    detail.id = "optMasterDetail";
    detail.textContent =
      "Shortens large Read, Grep and Glob results before they reach the model. " +
      "This is the only feature on this page that changes what the model sees. " +
      "Off by default, and off means the request goes upstream exactly as " +
      "Claude Code sent it.";
    text.append(title, detail);
    bar.append(text, optSwitch(master, "optMasterDetail"));
    wrap.appendChild(bar);
  }

  const perTool = document.createElement("section");
  perTool.className = "settings-section opt-section";
  const heading = document.createElement("div");
  heading.className = "section-heading";
  const headingText = document.createElement("div");
  const headingTitle = document.createElement("h3");
  headingTitle.textContent = "Per-tool rules";
  const headingNote = document.createElement("p");
  headingNote.id = "optPerToolNote";
  headingText.append(headingTitle, headingNote);
  heading.appendChild(headingText);
  perTool.appendChild(heading);

  const toolRows = [
    ["TOOL_RESULT_TRIM_READ", "Read"],
    ["TOOL_RESULT_TRIM_GREP", "Grep"],
    ["TOOL_RESULT_TRIM_GLOB", "Glob"],
  ]
    .filter(([key]) => byKey.has(key))
    .map(([key, tool]) => {
      claimed.add(key);
      const name = document.createElement("strong");
      name.textContent = tool;
      const effect = document.createElement("div");
      effect.className = "opt-sub";
      effect.textContent = OPT_TOOL_NOTES[key] || "";
      return [
        name,
        optSegmented(byKey.get(key), OPT_TRIM_MODES, `${tool} trim mode`),
        effect,
      ];
    });

  const scroll = document.createElement("div");
  scroll.className = "opt-scroll";
  scroll.appendChild(
    optTable(["Tool", "Mode", "Effect"], toolRows, "No trimming rules are registered."),
  );
  perTool.appendChild(scroll);

  const knobKeys = [
    "TOOL_RESULT_TRIM_THRESHOLD_CHARS",
    "TOOL_RESULT_TRIM_KEEP_HEAD_CHARS",
    "TOOL_RESULT_TRIM_KEEP_TAIL_CHARS",
    "TOOL_RESULT_TRIM_PROTECT_RECENT_RESULTS",
  ].filter((key) => byKey.has(key));
  if (knobKeys.length) {
    const grid = document.createElement("div");
    grid.className = "field-grid";
    knobKeys.forEach((key) => {
      claimed.add(key);
      grid.appendChild(renderField(byKey.get(key)));
    });
    perTool.appendChild(grid);
  }

  perTool.appendChild(optimizerTrimWarning());
  wrap.appendChild(perTool);

  // Anything the manifest adds to this section later still renders, rather
  // than silently existing in the API and nowhere on the page. That exact gap
  // shipped once already, as a settings page with no page.
  const unclaimed = fields.filter((field) => !claimed.has(field.key));
  if (unclaimed.length) {
    const rest = document.createElement("div");
    rest.className = "field-grid";
    unclaimed.forEach((field) => rest.appendChild(renderField(field)));
    wrap.appendChild(rest);
  }

  return wrap;
}

function initOptimizerView() {
  byId("optRefresh")?.addEventListener("click", () => {
    loadOptimizerView({ force: true }).catch((error) =>
      showMessage(error.message, "error"),
    );
  });
  byId("optScan")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    optState.scanning = true;
    optState.candidatesError = null;
    button.disabled = true;
    renderOptimizerCandidates();
    try {
      optState.candidates = await api("/admin/api/requests/discover-optimizations");
    } catch (error) {
      optState.candidates = null;
      optState.candidatesError = error.message;
    } finally {
      optState.scanning = false;
      button.disabled = false;
      renderOptimizerCandidates();
    }
  });
}

initOptimizerView();

initThemeSwitch();

/* ----------------------------------------------------------------- models
   The Models page. One payload from /admin/api/model-admin drives two
   sections: which models the catalogue shows, and -- per provider or per
   model -- which request parameters are forced, beside a read-only account of
   what MCC knows about the model and where it learned it.

   Everything is built with createElement/textContent. A model ref is upstream
   text and half of it is user-typed configuration, so none of it is ever
   interpolated into innerHTML.

   The tree is built lazily. A real install answers with ~1000 models across
   ~10 providers; building every override editor and capability table up front
   put 186,000 nodes and 9,279 <select> elements on the page before the user
   had opened anything. Provider bodies are built on first open, model bodies
   on first open, and a provider's model list is paged, so the node count
   tracks what is actually on screen. */

const modelsState = {
  data: null,
  // model_ref -> the latency rollup its chip is drawn from, or null when the
  // endpoint was unreachable. Kept beside `data` rather than in it: it comes
  // from a different endpoint on purpose (see loadModelsView).
  latency: null,
  loading: false,
  filter: "",
  // Which provider/model rows are unfolded, so a re-render after a save does
  // not collapse the row the user is working in.
  open: new Set(),
  // provider_id -> how many of its (filtered) models have been paged in.
  paged: new Map(),
  // Selected model refs. Lives here, not in the DOM: renderModelsTree() does
  // tree.textContent = "" on every filter keystroke, so a selection kept in
  // checkboxes would not survive typing one character.
  selected: new Set(),
  // "all" | "visible" | "hidden" | "configured" | "overridden", or a Set of
  // refs for the synthetic "the ones that did not take" facet.
  facet: "all",
  // { allow: [...], deny: [...] } from the last bulk write, or null.
  undo: null,
};

// Range anchoring is view state, not data state: it must not survive a tree
// rebuild, so it lives beside modelsState rather than in it.
let modelsLastClickedRef = null;
// The refs the last Shift+Arrow run selected, so walking back shrinks the
// range instead of leaving a trail behind the cursor.
let modelsArrowRange = [];
// { on, providerId } while a pointer is dragging across selection boxes.
let modelsDrag = null;

// How many model rows a provider shows before the "Show more" button. Sized so
// a page of rows is a scroll or two, not a wall.
const MODELS_PAGE_SIZE = 40;

async function loadModelsView(force = false) {
  if (modelsState.loading) return;
  if (modelsState.data && !force) return;
  modelsState.loading = true;
  setModelsStatus("Loading models...");
  try {
    // Fetched beside the page rather than folded into its payload. The
    // latency question is a scan of `request_attempts` -- 3.3 s cold on a
    // 4.5 GB log -- and the Models page spent 6.75.0 through 6.81.0 getting
    // off exactly that kind of critical path. Started at the same moment, so
    // it costs nothing in wall clock, and a failure leaves the page intact
    // with no chips rather than no page.
    const [data, latency] = await Promise.all([
      api("/admin/api/model-admin"),
      api("/admin/api/requests/latency").catch(() => null),
    ]);
    modelsState.data = data;
    modelsState.latency = indexLatencyByModel(latency);
    renderModelsPage();
    setModelsStatus("");
  } catch (error) {
    setModelsStatus(error.message);
  } finally {
    modelsState.loading = false;
  }
}

function setModelsStatus(text) {
  const status = byId("modelsTreeStatus");
  if (!status) return;
  status.textContent = text || "";
  status.hidden = !text;
}

function setModelsVisibilityStatus(text, kind = "") {
  const status = byId("modelsVisibilityStatus");
  if (!status) return;
  status.textContent = text || "";
  status.className = `models-status${kind ? ` ${kind}` : ""}`;
}

function renderModelsPage() {
  const data = modelsState.data;
  if (!data) return;
  const notice = byId("modelsHideOnlyNotice");
  if (notice) notice.textContent = data.visibility.hide_only_notice || "";
  syncModelsPatternFields(data);
  renderModelsPatternProvenance();
  renderModelsOwnedElsewhere(data.overrides.owned_elsewhere || {});
  renderModelsHiddenRoutes(data.visibility.hidden_route_refs || []);
  renderModelsTree();
}

function syncModelsPatternFields(data) {
  const allow = byId("modelsAllowPatterns");
  const deny = byId("modelsDenyPatterns");
  if (allow && document.activeElement !== allow) {
    allow.value = data.visibility.allow_raw || "";
  }
  if (deny && document.activeElement !== deny) {
    deny.value = data.visibility.deny_raw || "";
  }
}

/* Was one run-on sentence that repeated "the reasoning pipeline owns thinking
   parameters" four times. The same facts read as a list, grouped by owner. */
function renderModelsOwnedElsewhere(owned) {
  const target = byId("modelsOwnedElsewhere");
  if (!target) return;
  target.textContent = "";
  const names = Object.keys(owned);
  target.hidden = names.length === 0;
  if (!names.length) return;
  const byOwner = new Map();
  names.forEach((name) => {
    const reason = owned[name];
    if (!byOwner.has(reason)) byOwner.set(reason, []);
    byOwner.get(reason).push(name);
  });
  const lead = document.createElement("p");
  lead.className = "models-subhead";
  lead.textContent = "Not editable here";
  target.appendChild(lead);
  const list = document.createElement("ul");
  list.className = "models-owned-list";
  byOwner.forEach((params, reason) => {
    const item = document.createElement("li");
    params.forEach((name, index) => {
      if (index > 0) item.appendChild(document.createTextNode(" "));
      const code = document.createElement("code");
      code.textContent = name;
      item.appendChild(code);
    });
    const why = document.createElement("span");
    why.className = "models-owned-reason";
    why.textContent = reason;
    item.appendChild(why);
    list.appendChild(item);
  });
  target.appendChild(list);
}

function renderModelsHiddenRoutes(routes) {
  const target = byId("modelsHiddenRoutes");
  if (!target) return;
  target.textContent = "";
  target.hidden = routes.length === 0;
  if (!routes.length) return;
  const heading = document.createElement("p");
  heading.textContent =
    "These configured routes are currently hidden. They still resolve and still serve requests -- hiding is display-only.";
  target.appendChild(heading);
  const list = document.createElement("ul");
  routes.forEach((route) => {
    const item = document.createElement("li");
    const code = document.createElement("code");
    code.textContent = route.model_ref;
    item.appendChild(code);
    item.appendChild(
      document.createTextNode(` (${(route.sources || []).join(", ")})`),
    );
    list.appendChild(item);
  });
  target.appendChild(list);
}

function modelsMatchesFilter(text) {
  if (!modelsState.filter) return true;
  return text.toLowerCase().includes(modelsState.filter);
}

/* The facet is the axis a visibility page is actually organised around, and
   the filter never had it: "show me what is hidden" was a question the page
   could not answer. A Set facet is the synthetic one the bulk result panel
   installs when it offers "show the 12 that did not take". */
function modelsMatchesFacet(model) {
  const facet = modelsState.facet;
  if (facet instanceof Set) return facet.has(model.model_ref);
  if (facet === "visible") return Boolean(model.visible);
  if (facet === "hidden") return !model.visible;
  if (facet === "configured") return Boolean(model.configured);
  if (facet === "overridden") {
    /* "Not stock" is the question this facet asks, and a row carrying only a
       preference is not stock. It counts one without a predicate change
       because a preference is stored in the same row as the nine sampling
       parameters and `override` renders every key of that row -- which is
       exactly why the preferences went into the existing row rather than into
       a second store beside it. */
    return Object.keys(model.override || {}).length > 0;
  }
  if (facet === "learned") {
    return Array.isArray(model.learned) && model.learned.length > 0;
  }
  return true;
}

function modelsFacetLabel() {
  const facet = modelsState.facet;
  if (facet instanceof Set) return "the models a pattern overruled";
  return facet;
}

function modelsIsNarrowed() {
  return Boolean(modelsState.filter) || modelsState.facet !== "all";
}

function modelsFilteredFor(provider) {
  // Typing a provider name selects the provider, not zero models: the filter
  // used to match model refs only, so "novita" found nothing on a page whose
  // first column is provider names.
  const wholeProvider = modelsMatchesFilter(provider.provider_id);
  return (provider.models || []).filter(
    (model) =>
      (wholeProvider || modelsMatchesFilter(model.model_ref)) &&
      modelsMatchesFacet(model),
  );
}

function modelsRefsFor(provider) {
  return modelsFilteredFor(provider).map((model) => model.model_ref);
}

function modelsAllFilteredRefs() {
  const data = modelsState.data;
  const refs = [];
  (((data && data.providers) || [])).forEach((provider) => {
    modelsFilteredFor(provider).forEach((model) => refs.push(model.model_ref));
  });
  return refs;
}

function modelsProviderOf(modelRef) {
  const data = modelsState.data;
  const found = (((data && data.providers) || [])).find((provider) =>
    (provider.models || []).some((model) => model.model_ref === modelRef),
  );
  return found ? found.provider_id : null;
}

function modelsSelectionSummary() {
  const providers = new Set();
  modelsState.selected.forEach((ref) => {
    const providerId = modelsProviderOf(ref);
    if (providerId) providers.add(providerId);
  });
  return { count: modelsState.selected.size, providers: providers.size };
}

function setModelsSelection(refs, on) {
  refs.forEach((ref) => {
    if (on) modelsState.selected.add(ref);
    else modelsState.selected.delete(ref);
  });
  syncModelsSelectionUi();
}

function clearModelsSelection() {
  modelsState.selected.clear();
  modelsLastClickedRef = null;
  modelsArrowRange = [];
  syncModelsSelectionUi();
}

/* Walks rendered rows only, so its cost tracks what is on screen rather than
   the thousand models the payload holds. */
function syncModelsSelectionUi() {
  document.querySelectorAll(".models-model-row").forEach((row) => {
    const ref = row.dataset.ref;
    const on = modelsState.selected.has(ref);
    const box = row.querySelector("input.models-select");
    if (box) box.checked = on;
    row.classList.toggle("is-selected", on);
  });
  document.querySelectorAll(".models-provider").forEach((node) => {
    const box = node.querySelector("input.models-select-all");
    const provider = modelsProviderEntry(node.dataset.provider);
    if (!box || !provider) return;
    const refs = modelsRefsFor(provider);
    const picked = refs.filter((ref) => modelsState.selected.has(ref)).length;
    box.checked = refs.length > 0 && picked === refs.length;
    box.indeterminate = picked > 0 && picked < refs.length;
  });
  renderModelsBulkBar();
}

function modelsProviderEntry(providerId) {
  const data = modelsState.data;
  return (((data && data.providers) || [])).find(
    (provider) => provider.provider_id === providerId,
  );
}

/* The contiguous rendered rows between two refs of the same provider. Rows
   that were never paged in are not part of a visual range, so the range is
   read from the list the user can actually see. */
function modelsRenderedRefs(row) {
  const list = row && row.parentElement;
  if (!list) return [];
  return Array.from(list.children)
    .map((child) => child.dataset && child.dataset.ref)
    .filter(Boolean);
}

function modelsRowFor(modelRef) {
  // Scanned rather than selected: a model ref is upstream text that can carry
  // any character, and building a selector out of it needs an escape the page
  // cannot rely on.
  return Array.from(document.querySelectorAll(".models-model-row")).find(
    (row) => row.dataset.ref === modelRef,
  );
}

function modelsRangeRefs(fromRef, toRef) {
  const row = modelsRowFor(fromRef);
  const refs = modelsRenderedRefs(row);
  const start = refs.indexOf(fromRef);
  const end = refs.indexOf(toRef);
  if (start < 0 || end < 0) return [fromRef];
  return refs.slice(Math.min(start, end), Math.max(start, end) + 1);
}

function onModelsSelectClick(modelRef, box, event) {
  const on = box.checked;
  if (event.shiftKey && modelsLastClickedRef) {
    setModelsSelection(modelsRangeRefs(modelsLastClickedRef, modelRef), on);
  } else {
    setModelsSelection([modelRef], on);
    modelsLastClickedRef = modelRef;
  }
  modelsArrowRange = [];
}

/* Range selection must not be pointer-only: WCAG 2.2 asks for a single-pointer
   and keyboard alternative to any author-controlled drag, so the same range is
   reachable with Shift+Space and Shift+Arrow. */
function onModelsSelectKeydown(modelRef, box, event) {
  if (event.key === " " && event.shiftKey) {
    event.preventDefault();
    const on = !box.checked;
    setModelsSelection(modelsRangeRefs(modelsLastClickedRef || modelRef, modelRef), on);
    modelsArrowRange = [];
    return;
  }
  if (!event.shiftKey) return;
  if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
  event.preventDefault();
  const row = modelsRowFor(modelRef);
  const refs = modelsRenderedRefs(row);
  const here = refs.indexOf(modelRef);
  const next = refs[here + (event.key === "ArrowDown" ? 1 : -1)];
  if (!next) return;
  if (!modelsLastClickedRef) modelsLastClickedRef = modelRef;
  const wanted = modelsRangeRefs(modelsLastClickedRef, next);
  // Walking back towards the anchor shrinks the range rather than leaving the
  // rows behind the cursor selected.
  const dropped = modelsArrowRange.filter((ref) => !wanted.includes(ref));
  setModelsSelection(dropped, false);
  setModelsSelection(wanted, true);
  modelsArrowRange = wanted;
  const nextRow = modelsRowFor(next);
  const nextBox = nextRow && nextRow.querySelector("input.models-select");
  if (nextBox) nextBox.focus();
}

function startModelsDrag(modelRef, box, event) {
  // A touch that did not begin on the box itself is a scroll, not a drag.
  if (event.pointerType === "touch" && event.target !== box) return;
  if (typeof event.button === "number" && event.button !== 0) return;
  const on = !modelsState.selected.has(modelRef);
  modelsDrag = { on, providerId: modelsProviderOf(modelRef) };
  const tree = byId("modelsTree");
  if (tree) tree.classList.add("is-dragging");
  if (event.target !== box) {
    // The click that follows a press on the box does the box's own row; a
    // press on the cell around it has to do it here.
    setModelsSelection([modelRef], on);
    modelsLastClickedRef = modelRef;
  }
}

function continueModelsDrag(event) {
  if (!modelsDrag) return;
  const row = event.target.closest && event.target.closest(".models-model-row");
  if (!row || !row.dataset.ref) return;
  if (modelsProviderOf(row.dataset.ref) !== modelsDrag.providerId) return;
  // Every row the pointer crosses takes the anchor row's new state, so a drag
  // never leaves a mixed run behind it.
  setModelsSelection([row.dataset.ref], modelsDrag.on);
}

function endModelsDrag() {
  if (!modelsDrag) return;
  modelsDrag = null;
  const tree = byId("modelsTree");
  if (tree) tree.classList.remove("is-dragging");
}

function renderModelsTree() {
  const tree = byId("modelsTree");
  const data = modelsState.data;
  if (!tree || !data) return;
  tree.textContent = "";
  const providers = data.providers || [];
  const matching = [];
  let shown = 0;
  providers.forEach((provider) => {
    const models = modelsFilteredFor(provider);
    if (!models.length) return;
    shown += models.length;
    matching.push([provider, models]);
  });

  renderModelsFacets();
  renderCatalogueRefreshReadout();
  renderModelsTreeSummary(providers.length, matching.length, shown);
  syncModelsSelectionUi();

  if (!matching.length) {
    const empty = document.createElement("p");
    empty.className = "models-status";
    if (modelsState.facet !== "all") {
      // A bare "0 results" is a dead end. Name the facet that emptied the
      // page and offer the way out in the same sentence.
      empty.textContent = `Nothing matches the "${modelsFacetLabel()}" filter${
        modelsState.filter ? ` and "${modelsState.filter}"` : ""
      }.`;
      const clear = document.createElement("button");
      clear.type = "button";
      clear.className = "models-link-button models-facet-clear";
      clear.textContent = "Show all models again";
      clear.addEventListener("click", () => {
        modelsState.facet = "all";
        modelsState.paged.clear();
        renderModelsTree();
      });
      tree.appendChild(empty);
      tree.appendChild(clear);
      return;
    }
    empty.textContent = modelsState.filter
      ? "No model matches that filter."
      : "No models discovered yet. Refresh provider models on the Providers page.";
    tree.appendChild(empty);
    return;
  }
  // A filter that lands inside exactly one provider is unambiguous, so open
  // it. A filter that spans nine providers is not, and force-opening all of
  // them was how a three-letter search produced twelve thousand pixels of
  // page.
  const auto = Boolean(modelsState.filter) && matching.length === 1;
  matching.forEach(([provider, models]) => {
    tree.appendChild(buildModelsProviderNode(provider, models, auto));
  });
}

function renderModelsTreeSummary(providerCount, matchedProviders, shown) {
  const target = byId("modelsTreeSummary");
  if (!target) return;
  target.textContent = "";
  const line = document.createElement("span");
  if (modelsState.filter) {
    line.textContent = `${shown} model(s) in ${matchedProviders} of ${providerCount} provider(s) match "${modelsState.filter}".`;
  } else {
    line.textContent = `${shown} model(s) across ${providerCount} provider(s). Open a provider to see its models.`;
  }
  if (modelsState.facet !== "all") {
    line.textContent += ` Showing only "${modelsFacetLabel()}".`;
  }
  target.appendChild(line);
  // The page already counted the matches; what was missing was any way to act
  // on what it counted.
  if (modelsIsNarrowed() && shown) {
    const pick = document.createElement("button");
    pick.type = "button";
    pick.className = "models-link-button models-select-matches";
    pick.textContent = `Select all ${shown}`;
    pick.addEventListener("click", () => {
      setModelsSelection(modelsAllFilteredRefs(), true);
    });
    target.appendChild(pick);
  }
}

const MODELS_FACETS = [
  ["all", "All"],
  ["visible", "Visible"],
  ["hidden", "Hidden"],
  ["configured", "Configured"],
  ["overridden", "Overridden"],
  // "Show me every model MCC has learned something about" was a question the
  // page could not answer at all before 6.52.0.
  ["learned", "Learned"],
];

function renderModelsFacets() {
  const target = byId("modelsFacets");
  const data = modelsState.data;
  if (!target || !data) return;
  target.textContent = "";
  const models = [];
  (data.providers || []).forEach((provider) => {
    (provider.models || []).forEach((model) => models.push(model));
  });
  const saved = modelsState.facet;
  const counts = new Map();
  MODELS_FACETS.forEach(([key]) => {
    modelsState.facet = key;
    counts.set(key, models.filter((model) => modelsMatchesFacet(model)).length);
  });
  modelsState.facet = saved;
  MODELS_FACETS.forEach(([key, label]) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "models-facet";
    const active = modelsState.facet === key;
    if (active) chip.classList.add("is-active");
    chip.setAttribute("aria-pressed", active ? "true" : "false");
    chip.textContent = `${label} ${counts.get(key)}`;
    chip.addEventListener("click", () => {
      modelsState.facet = key;
      modelsState.paged.clear();
      renderModelsTree();
    });
    target.appendChild(chip);
  });
  // Appended here rather than mounted once at boot: this function clears its
  // own container on every render, so a link put there earlier would be
  // wiped by the first refresh and nobody would notice.
  target.appendChild(guideLink("learned"));
  if (modelsState.facet instanceof Set) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "models-facet is-active";
    chip.setAttribute("aria-pressed", "true");
    chip.textContent = `Overruled ${modelsState.facet.size}`;
    chip.addEventListener("click", () => {
      modelsState.facet = "all";
      modelsState.paged.clear();
      renderModelsTree();
    });
    target.appendChild(chip);
  }
}

/* The provider header is the page's one sticky element and it carries the
   bulk controls, so a user half way down three hundred rows still knows which
   provider they are in and can act on it without scrolling back.

   It is a button plus a body rather than <details>/<summary> for one reason:
   a checkbox and three buttons may not live inside a <summary> (Chrome
   reported the nested-control violation 1,021 times on the model rows), and a
   <summary> must be the first child of its <details>, which leaves nowhere
   legal to put them. A button with aria-expanded is the same affordance with
   room beside it. */
function buildModelsProviderNode(provider, models, autoOpen) {
  const node = document.createElement("div");
  node.className = "models-provider";
  node.dataset.provider = provider.provider_id;
  const key = `provider:${provider.provider_id}`;
  const open = modelsState.open.has(key) || autoOpen;

  const head = document.createElement("div");
  head.className = "models-provider-head";

  const selectAll = document.createElement("input");
  selectAll.type = "checkbox";
  selectAll.className = "models-select-all";
  selectAll.setAttribute(
    "aria-label",
    `Select every listed ${provider.provider_id} model`,
  );
  selectAll.addEventListener("change", () => {
    setModelsSelection(modelsRefsFor(provider), selectAll.checked);
  });
  head.appendChild(selectAll);

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "models-provider-toggle";
  toggle.setAttribute("aria-expanded", open ? "true" : "false");
  const name = document.createElement("span");
  name.className = "models-provider-name";
  name.textContent = provider.provider_id;
  toggle.appendChild(name);
  const count = document.createElement("span");
  count.className = "models-chip";
  count.textContent = modelsIsNarrowed()
    ? `${models.length} of ${(provider.models || []).length} match`
    : `${models.length} models`;
  toggle.appendChild(count);
  // "0 hidden" on every provider is chrome with no information in it.
  if (provider.hidden_count) {
    toggle.appendChild(
      buildModelsChip("hidden", `${provider.hidden_count} hidden`),
    );
  }
  const configured = (provider.models || []).filter(
    (model) => model.configured,
  ).length;
  if (configured) {
    toggle.appendChild(buildModelsChip("route", `${configured} configured`));
  }
  if (providerHasOverrides(provider)) {
    toggle.appendChild(buildModelsChip("forced", "provider override"));
  }
  appendImageEstimateChips(toggle, provider);
  head.appendChild(toggle);

  const bulk = document.createElement("div");
  bulk.className = "models-provider-bulk";
  const narrowed = modelsIsNarrowed();
  const scope = narrowed
    ? `${models.length} matching ${provider.provider_id} models`
    : `${models.length} ${provider.provider_id} models`;
  [
    ["show", "Show all"],
    ["hide", "Hide all"],
    ["invert", "Invert"],
  ].forEach(([action, label]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary-button models-bulk-button";
    button.textContent = label;
    button.setAttribute(
      "aria-label",
      action === "invert"
        ? `Invert the visibility of ${scope}`
        : `${label} ${scope}`,
    );
    button.addEventListener("click", () => {
      runModelsBulk({
        scope: "provider",
        action,
        providerId: provider.provider_id,
        // No refs means "the whole provider", which the server writes as one
        // glob. A narrowed view is a selection, not a policy, so it sends the
        // refs it can actually see.
        refs: narrowed || action === "invert" ? modelsRefsFor(provider) : [],
        affected: models.length,
        button,
      });
    });
    bulk.appendChild(button);
  });
  if (narrowed) {
    const note = document.createElement("span");
    note.className = "models-provider-match";
    note.textContent = `${models.length} match filter`;
    bulk.appendChild(note);
  }
  head.appendChild(bulk);
  node.appendChild(head);

  const body = document.createElement("div");
  body.className = "models-provider-body";
  body.hidden = !open;
  node.appendChild(body);

  // Built on first open, not up front. Ten providers' worth of editors and a
  // thousand model bodies is the difference between 190,000 nodes and a few
  // hundred.
  const fill = () => {
    if (body.dataset.filled === "1") return;
    body.dataset.filled = "1";
    fillModelsProviderBody(body, provider, models);
  };
  toggle.addEventListener("click", () => {
    const next = body.hidden;
    body.hidden = !next;
    toggle.setAttribute("aria-expanded", next ? "true" : "false");
    if (next) {
      modelsState.open.add(key);
      fill();
      syncModelsSelectionUi();
    } else {
      modelsState.open.delete(key);
    }
  });
  if (open) fill();
  return node;
}

function providerHasOverrides(provider) {
  return Object.keys(provider.override || {}).length > 0;
}

function fillModelsProviderBody(body, provider, models) {
  const data = modelsState.data;
  const editable = (data && data.overrides.editable_parameters) || [];

  // The models are what the user clicked the provider for, so they come
  // first; the provider-wide form is one line of disclosure above them
  // instead of a screenful of selects to scroll past.
  const settings = document.createElement("details");
  settings.className = "models-provider-settings";
  const settingsSummary = document.createElement("summary");
  settingsSummary.textContent = `Parameter overrides for every ${provider.provider_id} model`;
  settings.appendChild(settingsSummary);
  settings.open = providerHasOverrides(provider);
  const editor = document.createElement("div");
  settings.appendChild(editor);
  const buildEditor = () => {
    if (editor.dataset.filled === "1") return;
    editor.dataset.filled = "1";
    editor.appendChild(
      buildOverrideEditor(
        "provider",
        provider.provider_id,
        provider.override,
        editable,
        provider.preferences,
      ),
    );
  };
  if (settings.open) buildEditor();
  settings.addEventListener("toggle", () => {
    if (settings.open) buildEditor();
  });
  body.appendChild(settings);

  const list = document.createElement("div");
  list.className = "models-model-list";
  body.appendChild(list);

  const more = document.createElement("button");
  more.type = "button";
  more.className = "secondary-button models-more";
  body.appendChild(more);

  const page = () => {
    const already = list.childElementCount;
    const next = models.slice(already, already + MODELS_PAGE_SIZE);
    next.forEach((model) => list.appendChild(buildModelRow(model, editable)));
    const remaining = models.length - list.childElementCount;
    more.hidden = remaining <= 0;
    more.textContent = `Show ${Math.min(remaining, MODELS_PAGE_SIZE)} more of ${remaining}`;
    modelsState.paged.set(provider.provider_id, list.childElementCount);
  };
  more.addEventListener("click", page);
  // Re-page up to wherever the user had got to before a filter change rebuilt
  // the tree, so "Show more" is not something you have to press again.
  const wanted = modelsState.paged.get(provider.provider_id) || 0;
  page();
  while (list.childElementCount < wanted && !more.hidden) page();
}

/* The visibility tick is a sibling of the <details>, not a child of its
   <summary>. A control inside a summary is both an accessibility violation
   (Chrome reported it 1,021 times on a real install) and a click-target
   conflict that needed a stopPropagation to paper over. */
function buildModelRow(model, editable) {
  const row = document.createElement("div");
  row.className = "models-model-row";
  row.dataset.ref = model.model_ref;

  // One control per row, not two. The select box in the gutter, plus the
  // action bar it feeds, is the only thing on this page that changes what the
  // catalogue shows; the word beside it is a readout of the result. Two
  // checkboxes on one row was the design's biggest legibility bet and the
  // person using it called it: the second tick was bound to the *recomputed*
  // visibility, so a glob could overrule it and it sprang back with nothing
  // but a toast to say why.
  const cell = document.createElement("div");
  cell.className = "models-select-cell";
  const select = document.createElement("input");
  select.type = "checkbox";
  select.className = "models-select";
  select.checked = modelsState.selected.has(model.model_ref);
  select.setAttribute("aria-label", `Select ${model.model_ref}`);
  select.addEventListener("click", (clicked) =>
    onModelsSelectClick(model.model_ref, select, clicked),
  );
  select.addEventListener("keydown", (pressed) =>
    onModelsSelectKeydown(model.model_ref, select, pressed),
  );
  cell.appendChild(select);
  cell.addEventListener("pointerdown", (pressed) =>
    startModelsDrag(model.model_ref, select, pressed),
  );
  row.appendChild(cell);
  if (select.checked) row.classList.add("is-selected");

  row.appendChild(buildModelsVisibilityState(model));
  row.appendChild(buildModelNode(model, editable));
  return row;
}

/* Shown / Hidden / Hidden by <pattern>. A state, never a verb: "Show" sitting
   in the slot that reports what is *true* is what made an unticked row look
   like a control that had not responded. */
function buildModelsVisibilityState(model) {
  const state = document.createElement("div");
  state.className = "models-visible-state";
  fillModelsVisibilityState(state, model);
  return state;
}

function fillModelsVisibilityState(state, model) {
  state.textContent = "";
  state.classList.toggle("is-hidden", !model.visible);
  const word = document.createElement("span");
  word.className = "models-visible-word";
  word.textContent = model.visible ? "Shown" : "Hidden";
  state.appendChild(word);
  const pattern = model.visible ? "" : model.hidden_by || "";
  if (!pattern) {
    state.title = model.visible
      ? `${model.model_ref} is listed in /v1/models and the admin pickers`
      : `${model.model_ref} is hidden by its own entry in your hide list`;
    return;
  }
  // Rendered every time the row is drawn, not announced once in a toast: with
  // 994 patterns in the list, "a pattern overrules this" is not actionable and
  // a state that disappears is not an explanation.
  // Real spaces, not a flex gap: the readout is read aloud as one phrase, and
  // "Hiddenby*:free" is what a gap-only separation gives a screen reader.
  const by = document.createElement("span");
  by.className = "models-visible-by";
  by.textContent = " by ";
  state.appendChild(by);
  const named = modelsPatternName(pattern);
  const link = document.createElement("button");
  link.type = "button";
  link.className = "models-blocked-pattern";
  link.textContent = named;
  link.setAttribute(
    "aria-label",
    `${model.model_ref} is hidden by ${named}. Review it in the pattern editor.`,
  );
  link.addEventListener("click", () => offerModelsPatternRemoval(pattern, link));
  state.appendChild(link);
  state.title = `${model.model_ref} is hidden by ${named}, not by a choice this row can change`;
}

function modelsPatternName(pattern) {
  return pattern === "__allow_list__"
    ? 'your "Show only these" list'
    : pattern;
}

/* A row whose state is dictated by a glob must not present an affordance that
   cannot change it -- but it must not be a dead end either. Clicking the
   pattern scrolls to the editor that owns it and offers the one edit that
   would free the row, confirmed in place and undoable like every other write
   on this page. */
function offerModelsPatternRemoval(pattern, button) {
  const section = byId("section-model-visibility");
  if (section && section.scrollIntoView) {
    section.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  if (pattern === "__allow_list__") {
    setModelsVisibilityStatus(
      'Your "Show only these" list makes listing opt-in, so every model it does ' +
        "not name is hidden. Name this model there, or empty the list.",
    );
    return;
  }
  if (button.dataset.confirming === "1") {
    button.dataset.confirming = "";
    removeModelsDenyPattern(pattern).catch((error) =>
      setModelsVisibilityStatus(error.message, "error"),
    );
    return;
  }
  const label = button.textContent;
  button.dataset.confirming = "1";
  button.textContent = `remove ${pattern}?`;
  window.setTimeout(() => {
    if (button.dataset.confirming !== "1") return;
    button.dataset.confirming = "";
    button.textContent = label;
  }, 5000);
}

async function removeModelsDenyPattern(pattern) {
  const data = modelsState.data;
  if (!data) return;
  const allow = data.visibility.allow_raw || "";
  const deny = modelsPatternList(data.visibility.deny_raw);
  const next = deny.filter((entry) => entry !== pattern);
  if (next.length === deny.length) return;
  setModelsVisibilityStatus("Removing...");
  await api("/admin/api/model-admin/visibility", {
    method: "POST",
    body: JSON.stringify({ allow, deny: next.join(",") }),
  });
  modelsState.undo = { allow: modelsPatternList(allow), deny };
  await loadModelsView(true);
  setModelsVisibilityStatus("");
  renderModelsWritePanel(
    `Removed ${pattern} from your hide list. Routing is unaffected either way.`,
  );
}

function modelsPatternList(raw) {
  return (raw || "")
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);
}

function buildModelNode(model, editable) {
  const node = document.createElement("details");
  node.className = "models-model";
  const key = `model:${model.model_ref}`;
  node.open = modelsState.open.has(key);

  node.appendChild(buildModelSummary(model));

  const body = document.createElement("div");
  body.className = "models-model-body";
  node.appendChild(body);

  const fill = () => {
    if (body.dataset.filled === "1") return;
    body.dataset.filled = "1";
    fillModelBody(body, model, editable);
  };
  node.addEventListener("toggle", () => {
    if (node.open) {
      modelsState.open.add(key);
      fill();
    } else {
      modelsState.open.delete(key);
    }
  });
  if (node.open) fill();
  return node;
}

function buildModelSummary(model) {
  const summary = document.createElement("summary");
  const ref = document.createElement("span");
  ref.className = "models-ref";
  ref.textContent = model.model_ref;
  summary.appendChild(ref);
  if (model.configured) {
    summary.appendChild(buildModelsChip("route", "named by a MODEL* setting"));
  }
  if (!model.visible) {
    summary.appendChild(buildModelsChip("hidden", "hidden from catalogues"));
  }
  if (!model.has_metadata) {
    summary.appendChild(buildModelsChip("unknown", "no discovered metadata"));
  }
  // Why this row exists at all. A different question from where any of its
  // numbers came from, and the one the page could never answer: a picker
  // entry sourced from the vendor's own catalogue and one sourced from a list
  // somebody typed used to look identical.
  appendListingChips(summary, model.listing);
  const forced = (model.effective || []).filter(
    (row) => row.action !== "inherit",
  );
  if (forced.length) {
    summary.appendChild(
      buildModelsChip("forced", `${forced.length} override(s) active`),
    );
  }
  // Measured, not declared: what the log saw leave and come back. Absent when
  // the model served no succeeded attempt in the window -- never measured is
  // not the same fact as measured zero, so no chip rather than a zeroed one.
  const second = document.createElement("span");
  second.className = "models-chip-row";
  const measured = model.reasoning_measured;
  if (measured && measured.attempts) {
    const days = (modelsState.data && modelsState.data.measured_days) || 7;
    const chip = buildModelsChip(
      "measured",
      `last ${days}d: reasoning requested ${measured.requested}/${measured.attempts}, ` +
        `returned ${measured.returned}/${measured.attempts}`,
    );
    chip.title =
      "Requested is what the outbound body carried; returned is whether the " +
      "reply contained thinking text. Succeeded attempts only.";
    second.appendChild(chip);
  }
  appendLatencyChip(second, model.model_ref);
  // What this deployment taught MCC about itself, with its age and whether it
  // is still being applied. Blank for the common case: most models have never
  // said anything about themselves.
  appendLearnedChips(second, model.learned);
  // The pattern that overrules this row is named in the row's own visibility
  // readout, which is drawn whether or not the summary is on screen.
  if (second.childElementCount) summary.appendChild(second);
  return summary;
}

/** Fold the latency endpoint's (model, outcome) rows into one entry per model.
 *
 * The outcome groups are kept, not summed: "answered in 300 ms, failed after
 * 90 s" is two facts and averaging them together produces a third that is
 * neither. The chip's headline is the answering group, because that is what
 * "how fast is this model" means; the tooltip lists the rest.
 */
function indexLatencyByModel(latency) {
  if (!latency || latency.enabled === false || !Array.isArray(latency.rows)) {
    return null;
  }
  const index = new Map();
  latency.rows.forEach((row) => {
    const ref = String(row.model_ref || "");
    if (!ref) return;
    const entry = index.get(ref) || { attempts: 0, ttft_measured: 0, outcomes: [] };
    entry.attempts += Number(row.attempts || 0);
    entry.ttft_measured += Number(row.ttft_measured || 0);
    entry.outcomes.push(row);
    index.set(ref, entry);
  });
  return index;
}

/** The group whose percentiles the chip leads with: the answering one where
 *  there is one, otherwise whichever group was actually measured. */
function headlineLatencyOutcome(entry) {
  const measured = entry.outcomes.filter(
    (row) => Number(row.ttft_measured || 0) > 0 && row.p50_ttft_ms != null,
  );
  if (!measured.length) return null;
  return (
    measured.find((row) => row.outcome === "succeeded") ||
    measured.reduce((best, row) =>
      Number(row.ttft_measured || 0) > Number(best.ttft_measured || 0) ? row : best,
    )
  );
}

/* Measured, not declared, and absent rather than zeroed: a model that served
   no measured attempt gets no chip at all. Every attempt written before 7.4.0
   is unmeasured and cannot be backfilled, so on an installed log this chip
   appears one model at a time as traffic arrives -- which is the honest
   picture, and a "p50 0 ms" chip on 1,189 models would not be. */
function appendLatencyChip(row, modelRef) {
  const index = modelsState.latency;
  if (!index) return;
  const entry = index.get(String(modelRef || ""));
  if (!entry || !entry.ttft_measured) return;
  const headline = headlineLatencyOutcome(entry);
  if (!headline) return;
  const days = (modelsState.data && modelsState.data.measured_days) || 7;
  const p95 =
    headline.p95_ttft_ms == null
      ? ""
      : `, p95 ${formatChainDuration(headline.p95_ttft_ms)}`;
  const chip = buildModelsChip(
    "latency",
    `last ${days}d: p50 TTFT ${formatChainDuration(headline.p50_ttft_ms)}${p95}` +
      ` over ${entry.ttft_measured} of ${entry.attempts} attempts`,
  );
  chip.title = [
    `Median time to this model's first token when it ${
      CHAIN_OUTCOME_LABELS[headline.outcome] || headline.outcome
    }.`,
    "The denominator counts every attempt this model served in the window," +
      " failed ones included; only attempts recorded from 7.4.0 on carry a" +
      " first-token time.",
    entry.outcomes
      .map(
        (item) =>
          `${CHAIN_OUTCOME_LABELS[item.outcome] || item.outcome}: ${item.attempts}` +
          ` attempts, p50 ${
            item.p50_ttft_ms == null
              ? NOT_MEASURED
              : formatChainDuration(item.p50_ttft_ms)
          }`,
      )
      .join(" · "),
  ].join(" ");
  row.appendChild(chip);
}

/* "3 d ago" beats an ISO timestamp in a chip that has to fit beside four
   others; the exact instant is in the tooltip and in the panel below. */
function learnedAgeText(seconds) {
  if (typeof seconds !== "number" || !isFinite(seconds)) return "age unknown";
  if (seconds < 90) return "just now";
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} d ago`;
}

function learnedChipText(fact) {
  const label = fact.fact_label || fact.fact_kind;
  const value =
    typeof fact.value === "number" ? ` ${fact.value.toLocaleString()}` : "";
  const detail = fact.detail ? ` ${fact.detail}` : "";
  /* A host-wide fact is drawn on every model row of its provider, so it
     has to say whose property it is -- otherwise "tool-name limit 64"
     reads as a statement about this one model. */
  const scope = fact.model_id === "*" ? " (host-wide)" : "";
  return `${label}${value}${detail}${scope} · ${learnedAgeText(fact.age_seconds)}`;
}

function appendLearnedChips(row, facts) {
  if (!Array.isArray(facts) || !facts.length) return;
  facts.forEach((fact) => {
    const chip = buildModelsChip(
      "learned",
      fact.stale ? `${learnedChipText(fact)} · stale` : learnedChipText(fact),
    );
    if (fact.stale) chip.classList.add("models-chip-learned-stale");
    if (fact.agrees === false) chip.classList.add("models-chip-learned-disagree");
    chip.title = [
      `Learned from ${fact.source_label || fact.source}.`,
      fact.evidence,
      fact.stale
        ? "Past its age limit, so it is no longer applied; the next real " +
          "request re-checks it and this row comes back to life."
        : "",
    ]
      .filter(Boolean)
      .join(" ");
    row.appendChild(chip);
  });
}

function fillModelBody(body, model, editable) {
  body.textContent = "";
  body.appendChild(
    buildOverrideEditor(
      "model",
      model.model_ref,
      model.override,
      editable,
      model.preferences,
    ),
  );
  const readouts = document.createElement("div");
  readouts.className = "models-readouts";
  body.appendChild(readouts);
  fillModelReadouts(readouts, model);
}

/* The two read-only panels, refreshed on their own. Rebuilding the whole body
   after a save also rebuilt the Save button's status element, so the "Saved"
   confirmation landed in a node that was no longer on the page and the user
   saw nothing at all. The editor already shows what was saved; only these
   two need repainting. */
function fillModelReadouts(readouts, model) {
  const data = modelsState.data;
  readouts.textContent = "";
  readouts.appendChild(buildEffectiveTable(model.effective || []));
  readouts.appendChild(
    buildCapabilityPanel(
      model.capabilities,
      (data && data.source_labels) || {},
      model,
    ),
  );
  const listing = buildListingPanel(model.listing);
  if (listing) readouts.appendChild(listing);
  const learned = buildLearnedPanel(model);
  if (learned) readouts.appendChild(learned);
}

/* Every fact MCC holds about this model, with its source, both timestamps,
   whether it is still applied, and a Forget control. A probe verdict is drawn
   beside the ladder's claim about the same field with an explicit agree /
   disagree marker: a disagreement is a catalogue lying about a deployment,
   which is the single most valuable thing this feature produces. */
function buildLearnedPanel(model) {
  const facts = Array.isArray(model.learned) ? model.learned : [];
  if (!facts.length) return null;
  const wrap = document.createElement("div");
  wrap.className = "models-learned";
  const head = document.createElement("p");
  head.className = "models-subhead";
  head.textContent = "What this host taught MCC";
  wrap.appendChild(head);

  facts.forEach((fact) => {
    const row = document.createElement("div");
    row.className = "models-learned-row";
    if (fact.stale) row.classList.add("is-stale");

    const what = document.createElement("span");
    what.className = "models-learned-what";
    what.textContent = learnedChipText(fact);
    row.appendChild(what);

    const source = document.createElement("span");
    source.className = "models-learned-source";
    source.textContent = fact.source_label || fact.source || "";
    row.appendChild(source);

    if (fact.agrees === true || fact.agrees === false) {
      const verdict = document.createElement("span");
      verdict.className = fact.agrees
        ? "models-learned-agree"
        : "models-learned-disagree";
      verdict.textContent = fact.agrees
        ? `agrees with the catalogue on ${fact.field}`
        : `disagrees with the catalogue on ${fact.field}`;
      row.appendChild(verdict);
    }

    if (fact.stale) {
      const stale = document.createElement("span");
      stale.className = "models-learned-stale";
      stale.textContent = fact.retired
        ? "the model left this catalogue -- not applied"
        : "stale, will be re-verified";
      row.appendChild(stale);
    }

    if (fact.evidence) {
      const evidence = document.createElement("span");
      evidence.className = "models-learned-evidence";
      evidence.textContent = fact.evidence;
      row.appendChild(evidence);
    }

    const forget = document.createElement("button");
    forget.type = "button";
    forget.className = "ghost-button models-learned-forget";
    forget.textContent = "Forget";
    forget.addEventListener("click", () =>
      forgetLearnedFacts(
        {
          providerId: providerIdOf(model.model_ref),
          /* The fact's own subject, not the row it is drawn on: a
             host-wide fact appears on every model of the provider
             and must be forgotten once. */
          modelId: fact.model_id || modelIdOf(model.model_ref),
          factKind: fact.fact_kind,
        },
        forget,
      ),
    );
    row.appendChild(forget);
    wrap.appendChild(row);
  });
  return wrap;
}

function providerIdOf(modelRef) {
  const index = String(modelRef || "").indexOf("/");
  return index < 0 ? String(modelRef || "") : String(modelRef).slice(0, index);
}

function modelIdOf(modelRef) {
  const index = String(modelRef || "").indexOf("/");
  return index < 0 ? String(modelRef || "") : String(modelRef).slice(index + 1);
}

/* One endpoint, three granularities: an empty provider means everything, a
   provider with no model means that provider, and all three fields means one
   row. They are the same operation at three scopes. */
async function forgetLearnedFacts(payload, button) {
  if (button) button.disabled = true;
  try {
    const result = await api("/admin/api/model-admin/learned/forget", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    showMessage(`Forgot ${result.forgotten} learned fact(s).`, "ok");
    await loadModelsView(true);
  } catch (error) {
    showMessage(`Could not forget: ${error.message}`, "error");
    if (button) button.disabled = false;
  }
}

/* "last refreshed 12 min ago, next in 48 min", or a plain sentence naming the
   setting when the sweep is off. A catalogue whose age is unanswerable is how
   "this gateway added a model today" became a support question. */
function renderCatalogueRefreshReadout() {
  const target = byId("modelsRefreshReadout");
  if (!target) return;
  const status = (modelsState.data && modelsState.data.catalogue_refresh) || {};
  if (!status.enabled) {
    target.textContent =
      "Automatic model catalogue refresh is off (MODEL_DISCOVERY_REFRESH_SECONDS=0).";
    return;
  }
  const now = Date.now() / 1000;
  const parts = [];
  /* "as of" rather than "last refreshed" when the catalogue on show is the one
     a previous run wrote: the age is real, but no sweep has happened in this
     process yet, and saying otherwise is how a page claims a network call it
     never made. */
  parts.push(
    status.last_refreshed_at
      ? `${status.from_stored_catalogue ? "as of" : "last refreshed"} ${learnedAgeText(now - status.last_refreshed_at)}`
      : "not refreshed yet in this process",
  );
  if (status.refreshing) {
    parts.push("refreshing now");
  } else if (status.next_refresh_at && status.next_refresh_at > now) {
    parts.push(
      `next in ${Math.max(1, Math.round((status.next_refresh_at - now) / 60))} min`,
    );
  }
  target.textContent = `Catalogues: ${parts.join(", ")}.`;
}

/* Short chip text per provenance. The long sentence is the server's
   provenance_label and goes in the tooltip beside the evidence itself, so the
   row stays readable when a provider lists thirty models. */
const LISTING_CHIP_TEXT = {
  gateway: "gateway",
  vendor_client: "vendor catalogue",
  observed: "observed",
  models_dev: "models.dev",
  seed: "seed",
};

function appendListingChips(summary, listing) {
  if (!listing) return;
  const kind = String(listing.provenance || "").replace(/_/g, "-");
  const chip = buildModelsChip(
    "provenance",
    LISTING_CHIP_TEXT[listing.provenance] || listing.provenance,
  );
  if (kind) chip.classList.add(`models-chip-provenance-${kind}`);
  chip.title = [listing.provenance_label, listing.detail]
    .filter(Boolean)
    .join(" — ");
  summary.appendChild(chip);
  // The vendor's own retirement date, repeated rather than acted on: the
  // model is still listed and still servable until it passes.
  if (listing.retirement_at) {
    const retiring = buildModelsChip(
      "retiring",
      `retires ${listing.retirement_at}`,
    );
    retiring.title = listing.replacement_model_id
      ? `The vendor says ${listing.replacement_model_id} replaces this model.`
      : "The vendor published a retirement date for this model.";
    summary.appendChild(retiring);
  }
}

/* Existence provenance, drawn as prose rather than as a tier-badged row: none
   of it is a vote between sources, it is a record of which source answered. */
function buildListingPanel(listing) {
  if (!listing) return null;
  const wrap = document.createElement("div");
  wrap.className = "models-listing";
  const head = document.createElement("p");
  head.className = "models-subhead";
  head.textContent = "Why this model is listed";
  wrap.appendChild(head);
  const body = document.createElement("p");
  body.className = "models-empty-note";
  const parts = [listing.provenance_label];
  if (listing.detail) parts.push(listing.detail);
  if (listing.retirement_at) {
    parts.push(
      listing.replacement_model_id
        ? `the vendor retires it at ${listing.retirement_at} and names ` +
            `${listing.replacement_model_id} as its replacement`
        : `the vendor retires it at ${listing.retirement_at}`,
    );
  }
  if (listing.provenance === "seed") {
    parts.push(
      "no source on this machine confirmed it — install the vendor's CLI, " +
        "or route to it once, and this row will say something stronger",
    );
  }
  body.textContent = `${parts.join("; ")}.`;
  wrap.appendChild(body);
  return wrap;
}

/* How this host bills a picture, and whether that claim has held up.
 *
 * The family is declared data on the provider's descriptor -- nothing here or
 * anywhere else branches on a model name to reach it. The ratio beside it is
 * the only thing that can audit that declaration: billed input tokens over
 * estimated input tokens, across the uncached successful requests that carried
 * an image. Near 1.0 means the family is right. A host far from 1.0 has either
 * the wrong family or a formula nobody publishes, and should be marked
 * UNVERIFIED rather than guessed at.
 *
 * Nothing is drawn for a host nobody has sent a picture to: "0 vs 0" would
 * read as a verdict, and it is the absence of a measurement.
 */
function appendImageEstimateChips(toggle, provider) {
  const family = provider.image_token_family;
  if (family) {
    const chip = buildModelsChip("imagefam", `images: ${family}`);
    chip.title =
      family === "unknown"
        ? "This host publishes no image-token formula, so images are estimated with Anthropic's 28-px rule and the fallback is recorded rather than hidden."
        : `Images on this host are estimated with the ${family} formula, declared on its provider descriptor.`;
    toggle.appendChild(chip);
  }
  const estimate = provider.image_estimate;
  if (!estimate || !estimate.requests) return;
  const ratio =
    estimate.ratio == null ? "no usage reported" : `${estimate.ratio.toFixed(2)}\u00d7`;
  const chip = buildModelsChip("imageest", `billed/est ${ratio}`);
  chip.title =
    `Billed ${formatAnalyticsNumber(estimate.billed_tokens_in)} input tokens against ` +
    `${formatAnalyticsNumber(estimate.est_tokens_in)} estimated, over ` +
    `${estimate.requests} uncached request(s) carrying ${estimate.images} image(s); ` +
    `${formatAnalyticsNumber(estimate.est_image_tokens)} of the estimate was pictures. ` +
    "A ratio near 1.0 means this host's image-token family is right.";
  toggle.appendChild(chip);
  toggle.appendChild(guideLink("images"));
}

function buildModelsChip(kind, text) {
  const chip = document.createElement("span");
  chip.className = `models-chip models-chip-${kind}`;
  chip.textContent = text;
  return chip;
}

/* The three-state control. A single text box cannot say "force unset": empty
   and "send null" would look identical, and the difference between them is
   the entire point of the override file. So every parameter is a mode select
   -- inherit / force unset / force value -- and the text box beside it is
   only that third mode's argument, disabled in the other two.

   The grid carries column headers, because "temperature | Inherit | [ ]" with
   nothing above it does not say which of the two controls is the answer. */
/* The two preference controls, drawn above the nine sampling rows and under
   their own heading.

   They are three-state like everything else in this editor -- Inherit /
   Force unset / Force value -- because the file's three states are the whole
   point of it and a second idiom on the same form would be a second thing to
   learn. What differs is only the third mode's argument: a select whose
   options the ROW published (derived from this model's own resolved
   capability and its host's dialect, never a fixed list), and a number input
   bounded by the limit the same ladder resolved.

   An option the model cannot take is rendered disabled with the reason as its
   text, rather than omitted: "Off is not available here, and here is why" is
   a different message from "Off does not exist", and the second one reads as
   a defect. A stored value that is no longer on offer is kept, marked, and
   never silently rewritten -- the catalogue may move back, and editing a
   user's file behind their back is worse than telling them. */
function buildPreferenceRows(form, scope, key, preferences, inputs) {
  if (!preferences) return;
  const names =
    (modelsState.data &&
      modelsState.data.overrides &&
      modelsState.data.overrides.preference_parameters) ||
    {};
  const ordered = Object.keys(names).filter((name) => preferences[name]);
  if (!ordered.length) return;

  const head = document.createElement("div");
  head.className = "models-override-row models-override-head";
  ["Preference", "What to decide", "Value"].forEach((text) => {
    const cell = document.createElement("span");
    cell.textContent = text;
    head.appendChild(cell);
  });
  form.appendChild(head);

  ordered.forEach((name) => {
    const spec = preferences[name];
    const field = document.createElement("div");
    field.className = "models-override-row";
    const boxId = `pref-${scope}-${key}-${name}`.replace(/[^A-Za-z0-9_-]/g, "-");

    const label = document.createElement("label");
    label.className = "models-override-name";
    label.textContent = name === "reasoning_preference" ? "reasoning" : name;
    label.htmlFor = `${boxId}-mode`;
    field.appendChild(label);

    const mode = document.createElement("select");
    mode.className = "models-override-mode";
    mode.id = `${boxId}-mode`;
    [
      ["inherit", "Inherit"],
      ["unset", "Force unset"],
      ["value", "Force value"],
    ].forEach(([value, text]) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = text;
      mode.appendChild(option);
    });
    mode.value = spec.state === "value" || spec.state === "unset" ? spec.state : "inherit";
    field.appendChild(mode);

    const control =
      names[name] === "integer"
        ? buildOutputPreferenceInput(boxId, spec)
        : buildReasoningPreferenceSelect(boxId, spec);
    control.box.disabled = mode.value !== "value";
    mode.addEventListener("change", () => {
      control.box.disabled = mode.value !== "value";
      if (!control.box.disabled) control.box.focus();
    });
    field.appendChild(control.wrap);
    inputs.set(name, { mode: mode, box: control.box, max: control.max });
    form.appendChild(field);
  });
}

function buildReasoningPreferenceSelect(boxId, spec) {
  const wrap = document.createElement("div");
  wrap.className = "models-preference-value";
  const box = document.createElement("select");
  box.className = "models-override-value";
  box.id = `${boxId}-value`;
  box.setAttribute("aria-label", "reasoning preference");
  const options = Array.isArray(spec.options) ? spec.options : [];
  const stored = spec.state === "value" ? String(spec.value) : "";
  let storedIsOffered = false;
  options.forEach((entry) => {
    const option = document.createElement("option");
    option.value = entry.value;
    option.textContent = entry.label;
    if (entry.available === false) {
      option.disabled = true;
      option.textContent = `${entry.label} -- unavailable`;
    }
    if (entry.reason) option.title = entry.reason;
    if (entry.value === stored) storedIsOffered = true;
    box.appendChild(option);
  });
  /* A value the catalogue no longer offers is kept on the list so the select
     can still show what is stored; the note below says it is being clamped. */
  if (stored && !storedIsOffered) {
    const option = document.createElement("option");
    option.value = stored;
    option.textContent = `${stored} -- no longer offered`;
    box.appendChild(option);
  }
  if (stored) box.value = stored;
  wrap.appendChild(box);

  const note = document.createElement("p");
  note.className = "models-preference-note";
  const parts = [];
  if (spec.note) parts.push(spec.note);
  if (spec.can_reason === false) {
    box.disabled = true;
  }
  if (spec.capability_known === false && spec.can_reason !== false) {
    parts.push(
      "Nothing published this model's effort vocabulary, so every rung is " +
        "offered and whatever you pick is clamped to what it accepts.",
    );
  }
  if (stored && !storedIsOffered) {
    parts.push(
      `Your setting "${stored}" is no longer in this model's vocabulary; ` +
        "the nearest rung it does spell is being sent. Nothing was rewritten " +
        "on disk.",
    );
  }
  const chosen = options.find((entry) => entry.value === stored);
  if (chosen && chosen.reason) parts.push(chosen.reason);
  note.textContent = parts.join(" ");
  if (note.textContent) wrap.appendChild(note);
  return { wrap: wrap, box: box, max: null };
}

function buildOutputPreferenceInput(boxId, spec) {
  const wrap = document.createElement("div");
  wrap.className = "models-preference-value";
  const box = document.createElement("input");
  box.type = "number";
  box.min = "1";
  box.step = "1";
  box.className = "models-override-value";
  box.id = `${boxId}-value`;
  box.setAttribute("aria-label", "max output tokens");
  const limit = typeof spec.limit === "number" ? spec.limit : null;
  if (limit != null) box.max = String(limit);
  box.value = spec.state === "value" && spec.value != null ? String(spec.value) : "";
  wrap.appendChild(box);

  const note = document.createElement("p");
  note.className = "models-preference-note";
  const parts = [];
  if (limit != null) {
    const where = [spec.limit_source_label, spec.limit_tier_label]
      .filter(Boolean)
      .join(", ");
    parts.push(
      `This model reports ${limit.toLocaleString()}` +
        (where ? ` (${where})` : "") +
        ".",
    );
  }
  if (spec.note) parts.push(spec.note);
  note.textContent = parts.join(" ");
  if (note.textContent) wrap.appendChild(note);
  return { wrap: wrap, box: box, max: limit };
}

function buildOverrideEditor(scope, key, row, editable, preferences) {
  const form = document.createElement("div");
  form.className = "models-override-editor";
  const inputs = new Map();

  /* Preferences first, and under their own heading: the nine rows below
     are fields of a request BODY, and these two are statements about a
     DECISION MCC makes before any body exists. Drawing them in one
     undifferentiated grid would say they are the same kind of thing. */
  buildPreferenceRows(form, scope, key, preferences, inputs);

  const header = document.createElement("div");
  header.className = "models-override-row models-override-head";
  ["Parameter", "What to send", "Value"].forEach((text) => {
    const cell = document.createElement("span");
    cell.textContent = text;
    header.appendChild(cell);
  });
  form.appendChild(header);

  editable.forEach((name) => {
    const current = (row || {})[name];
    const field = document.createElement("div");
    field.className = "models-override-row";
    const boxId = `ov-${scope}-${key}-${name}`.replace(/[^A-Za-z0-9_-]/g, "-");

    const label = document.createElement("label");
    label.className = "models-override-name";
    label.textContent = name;
    label.htmlFor = `${boxId}-mode`;
    field.appendChild(label);

    const mode = document.createElement("select");
    mode.className = "models-override-mode";
    mode.id = `${boxId}-mode`;
    [
      ["inherit", "Inherit"],
      ["unset", "Force unset"],
      ["value", "Force value"],
    ].forEach(([value, text]) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = text;
      mode.appendChild(option);
    });
    mode.value = current ? current.state : "inherit";
    field.appendChild(mode);

    const box = document.createElement("input");
    box.type = "text";
    box.className = "models-override-value";
    box.id = `${boxId}-value`;
    box.setAttribute("aria-label", `${name} value`);
    box.placeholder = name === "stop" ? "comma-separated" : "";
    box.value =
      current && current.state === "value"
        ? formatOverrideValue(current.value)
        : "";
    box.disabled = mode.value !== "value";
    mode.addEventListener("change", () => {
      box.disabled = mode.value !== "value";
      if (!box.disabled) box.focus();
    });
    field.appendChild(box);
    inputs.set(name, { mode, box });
    form.appendChild(field);
  });

  const actions = document.createElement("div");
  actions.className = "models-actions";
  const save = document.createElement("button");
  save.type = "button";
  save.textContent = "Save overrides";
  const status = document.createElement("span");
  status.className = "models-status";
  save.addEventListener("click", () => {
    const updates = {};
    // "Force value" with an empty box used to save the empty string, which is
    // then forced onto the upstream body as `temperature: ""`. Refuse it.
    const blank = [];
    const overCap = [];
    inputs.forEach((control, name) => {
      if (control.mode.value === "inherit") {
        updates[name] =
          (modelsState.data &&
            modelsState.data.overrides &&
            modelsState.data.overrides.inherit_sentinel) ||
          "inherit";
      } else if (control.mode.value === "unset") {
        updates[name] = null;
      } else if (!control.box.value.trim()) {
        blank.push(name);
      } else {
        const parsed = parseOverrideValue(name, control.box.value);
        /* The published limit is a bound the server applies anyway --
           min(published, yours) -- so a number above it is not an error the
           request would fail on. It is still refused here, because saving a
           number that silently means a different number is the surprise this
           control exists to remove. */
        if (control.max != null && Number(parsed) > control.max) {
          overCap.push(`${name} (limit ${control.max.toLocaleString()})`);
        } else {
          updates[name] = parsed;
        }
      }
    });
    if (overCap.length) {
      const message =
        `This model reports a lower limit than that: ${overCap.join(", ")}.`;
      status.textContent = message;
      status.className = "models-status error";
      showMessage(message, "error");
      return;
    }
    if (blank.length) {
      const many = blank.length > 1;
      const message = `Give ${blank.join(", ")} a value, or set ${many ? "them" : "it"} back to Inherit or Force unset.`;
      status.textContent = message;
      status.className = "models-status error";
      showMessage(message, "error");
      return;
    }
    save.disabled = true;
    status.className = "models-status";
    status.textContent = "Saving...";
    saveModelOverrides(scope, key, updates)
      .then(() => {
        // The tree is not rebuilt on save, so this element is still on the
        // page and the confirmation is actually visible. It used to be
        // written into a node renderModelsPage() had already discarded.
        status.textContent = "Saved";
        status.className = "models-status ok";
      })
      .catch((error) => {
        status.textContent = error.message;
        status.className = "models-status error";
        showMessage(error.message, "error");
      })
      .finally(() => {
        save.disabled = false;
      });
  });
  actions.appendChild(save);
  actions.appendChild(status);
  form.appendChild(actions);
  return form;
}

function formatOverrideValue(value) {
  if (Array.isArray(value)) return value.join(", ");
  if (value === null || value === undefined) return "";
  return String(value);
}

/* `stop` is the one list-valued parameter; everything else is a scalar, and a
   number sent as a string is rejected by most upstream APIs. */
function parseOverrideValue(name, raw) {
  const text = (raw || "").trim();
  if (name === "stop") {
    return text
      .split(",")
      .map((entry) => entry.trim())
      .filter((entry) => entry.length > 0);
  }
  if (text === "") return "";
  if (/^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(text)) return Number(text);
  return text;
}

/* Only the parameters that are actually forced. Nine rows of "not sent" under
   a heading called "Effective request parameters" was the same information as
   nine "Inherit" selects directly above it, restated. */
function buildEffectiveTable(rows) {
  const wrap = document.createElement("div");
  wrap.className = "models-effective";
  const head = document.createElement("p");
  head.className = "models-subhead";
  head.textContent = "What this forces onto the request";
  wrap.appendChild(head);
  const forced = rows.filter((row) => row.action !== "inherit");
  if (!forced.length) {
    const note = document.createElement("p");
    note.className = "models-empty-note";
    note.textContent =
      "Nothing. Every editable parameter is left to the provider.";
    wrap.appendChild(note);
    return wrap;
  }
  const table = document.createElement("table");
  table.className = "models-table";
  forced.forEach((row) => {
    const tr = document.createElement("tr");
    const name = document.createElement("th");
    name.scope = "row";
    name.textContent = row.name;
    tr.appendChild(name);
    const value = document.createElement("td");
    if (row.action === "unset") value.textContent = "removed from the body";
    else value.textContent = formatOverrideValue(row.value);
    tr.appendChild(value);
    const from = document.createElement("td");
    from.className = "models-cell-source";
    from.textContent = row.from ? `from the ${row.from} row` : "";
    tr.appendChild(from);
    table.appendChild(tr);
  });
  wrap.appendChild(table);
  return wrap;
}

/* Read-only. The point of this panel is that a number here can come from the
   provider's own /models, from models.dev, or from a vote across same-named
   rows in *other* providers' buckets -- and the third is regularly wrong. So
   the exact ladder rung (1-8) is rendered beside every field, and an
   approximate one additionally says how many rows voted and how far they
   agreed (1-4 authoritative, 5-6 the OpenRouter reference catalogue, 7-10 the
   vote).

   Below the table sits the other half of the answer: what the HOST parses.
   "This model can reason" is a vote about a model; "this host reads
   reasoning_effort" is a declaration about a gateway. MCC sends a control only
   when both say yes, so a model that sends nothing needs both halves visible
   to explain which one said no. */
function buildCapabilityPanel(capabilities, labels, model) {
  const wrap = document.createElement("div");
  wrap.className = "models-capabilities";
  const head = document.createElement("p");
  head.className = "models-subhead";
  head.textContent = "What MCC knows about this model (read-only)";
  wrap.appendChild(head);
  const table = document.createElement("table");
  table.className = "models-table";
  const rows = [];
  if (capabilities) {
    /* Which of a multi-surface gateway's endpoints this model is actually
       posted to, and where that came from. Absent for every provider that has
       one endpoint, which is why this row appears only on OpenCode rows. A
       model whose surface this provider cannot speak reads "unservable" here
       WITH the reason beside it -- it keeps its row on purpose, because a
       catalogue that silently drops a model the vendor is giving away is
       exactly the failure this row exists to make visible. */
    rows.push(["wire surface", capabilities.response_surface]);
    rows.push(["output limit", capabilities.max_output_tokens]);
    rows.push(["context length", capabilities.context_length]);
    rows.push(["reads images", capabilities.supports_vision]);
    /* The prices resolved long before anything read them: the ladder has
       returned a rate and a tier for every (provider, model) since 6.35.0 and
       no surface showed either. They carry the same provenance badge as every
       other field here, which is the point -- a rate voted across strangers'
       catalogues and a rate this provider published are the same shape of
       number and must not look the same. */
    PRICE_FIELD_LABELS.forEach((entry) => {
      rows.push([entry[1], capabilities[entry[0]], formatPriceValue]);
    });
    rows.push(["gateway default parameters", capabilities.default_parameters]);
    rows.push(["supported parameters", capabilities.supported_parameters]);
    const reasoning = capabilities.reasoning || {};
    Object.keys(reasoning).forEach((name) => {
      rows.push([name.replace(/_/g, " "), reasoning[name]]);
    });
  }
  let written = 0;
  rows.forEach((entry) => {
    if (!entry[1]) return;
    written += 1;
    table.appendChild(buildCapabilityRow(entry[0], entry[1], labels, entry[2]));
  });
  if (!written) {
    const note = document.createElement("p");
    note.className = "models-empty-note";
    note.textContent =
      "Nothing discovered for this model, so MCC falls back to its defaults.";
    wrap.appendChild(note);
    return wrap;
  }
  wrap.appendChild(table);
  const surfaceEditor = buildSurfaceOverrideEditor(
    capabilities && capabilities.response_surface,
    model,
  );
  if (surfaceEditor) wrap.appendChild(surfaceEditor);
  const dialect = buildDialectPanel(capabilities && capabilities.reasoning_dialect);
  if (dialect) wrap.appendChild(dialect);
  return wrap;
}

/* The one writable thing in a read-only panel, and it sits here rather than in
   the parameter grid because it is not a parameter: `response_surface` never
   reaches a body, it picks which endpoint the body is posted to. It offers
   exactly the surfaces the HOST declares -- a profile's for a shipped
   provider, the registry entry's for a hand-configured one -- because a
   surface the host does not serve would be folded straight back to
   "unservable" with a reason, which is a worse way of saying "you cannot pick
   that". Absent for every single-surface provider, which is 39 of the 41 and
   every custom entry that did not opt in. */
function buildSurfaceOverrideEditor(field, model) {
  if (!field || !model || !model.model_ref) return null;
  const offered = Array.isArray(field.offered) ? field.offered : [];
  if (offered.length < 2) return null;
  const wrap = document.createElement("div");
  wrap.className = "models-surface-override";
  const head = document.createElement("p");
  head.className = "models-subhead";
  head.textContent = "Which endpoint to use for this model";
  wrap.appendChild(head);

  const select = document.createElement("select");
  select.className = "models-surface-select";
  select.setAttribute("aria-label", "Wire surface override");
  const auto = document.createElement("option");
  auto.value = "";
  auto.textContent = "Automatic (learned, then published, then default)";
  select.appendChild(auto);
  offered.forEach((surface) => {
    const option = document.createElement("option");
    option.value = surface.value;
    option.textContent = surface.label;
    select.appendChild(option);
  });
  select.value = field.override || "";
  wrap.appendChild(select);

  const status = document.createElement("span");
  status.className = "models-status";
  const save = document.createElement("button");
  save.className = "ghost-button models-surface-save";
  save.type = "button";
  save.textContent = "Pin endpoint";
  save.addEventListener("click", () => {
    save.disabled = true;
    status.className = "models-status";
    status.textContent = "Saving...";
    saveModelSurface(model.model_ref, select.value)
      .then(() => {
        status.textContent = "Saved";
        status.className = "models-status ok";
      })
      .catch((error) => {
        status.textContent = error.message;
        status.className = "models-status error";
        showMessage(error.message, "error");
      })
      .finally(() => {
        save.disabled = false;
      });
  });
  wrap.appendChild(save);
  wrap.appendChild(status);
  return wrap;
}

/* The host half of the two-fact rule. Deliberately plain text rather than
   tier-badged rows: none of this is a vote, it is what the code that builds
   the body will actually emit (narrowed, where the gateway publishes a
   per-model parameter list, by that list). */
function buildDialectPanel(dialect) {
  if (!dialect) return null;
  const wrap = document.createElement("div");
  wrap.className = "models-dialect";
  const head = document.createElement("p");
  head.className = "models-subhead";
  head.textContent = "What this host parses (declared or learned, never voted)";
  wrap.appendChild(head);
  if (dialect.known && dialect.origin_label) {
    const origin = document.createElement("span");
    origin.className = "models-dialect-origin";
    origin.textContent = dialect.origin_label;
    head.appendChild(origin);
  }
  const body = document.createElement("p");
  body.className = "models-empty-note";
  if (!dialect.known) {
    body.textContent =
      "Not declared for this provider, so reasoning is decided by the model's " +
      "capabilities alone — exactly as it was before host dialects existed.";
    wrap.appendChild(body);
    return wrap;
  }
  const parts = [];
  if (dialect.effort_values) {
    parts.push(
      `effort via ${dialect.effort_field || "an effort field"}: ` +
        dialect.effort_values.join(", "),
    );
  } else {
    parts.push("no effort field");
  }
  parts.push(
    dialect.toggle
      ? `on/off via ${dialect.toggle_field || "a toggle field"}`
      : "no on/off field",
  );
  parts.push(
    dialect.budget
      ? `thinking budget via ${dialect.budget_field || "a budget field"}`
      : "no thinking-budget field",
  );
  parts.push(dialect.off ? "can be switched off" : "cannot be switched off");
  if (dialect.adaptive) parts.push("has an adaptive channel");
  body.textContent = parts.join(" · ");
  wrap.appendChild(body);
  const rejections = Array.isArray(dialect.learned_rejections)
    ? dialect.learned_rejections
    : [];
  if (rejections.length) {
    const learned = document.createElement("p");
    learned.className = "models-empty-note";
    learned.textContent = rejections
      .map(
        (entry) =>
          `Not sent since ${entry.since}: ${entry.field} — this host answered ` +
          "400 naming it.",
      )
      .join(" · ");
    wrap.appendChild(learned);
  }
  return wrap;
}

function buildCapabilityRow(label, field, labels, format) {
  const tr = document.createElement("tr");
  const name = document.createElement("th");
  name.scope = "row";
  name.textContent = label;
  tr.appendChild(name);

  const value = document.createElement("td");
  value.textContent = (format || formatCapabilityValue)(field.value);
  tr.appendChild(value);

  const source = document.createElement("td");
  source.className = "models-cell-source";
  const badge = document.createElement("span");
  badge.className = `models-source models-source-${field.source}`;
  const sourceText =
    field.source_label || labels[field.source] || field.source || "unknown";
  // A guessed number and a published one used to wear the same shape of
  // badge. The guessed one now says so on the badge itself.
  badge.textContent = field.approximate ? `${sourceText} — guessed` : sourceText;
  source.appendChild(badge);
  // The exact rung of the resolution ladder, not just the coarse badge: a
  // "provider /models" answer that matched the id exactly reads very
  // differently from one that only matched after the pricing tag came off.
  /* A sentence the resolver wrote about this particular answer -- the npm
     package that selected a wire surface, or why one cannot be reached. Only
     the surface row carries one today; every other field's provenance is
     fully said by the badge and the tier. */
  if (field.note) {
    const note = document.createElement("span");
    note.className = "models-approx-note";
    note.textContent = field.note;
    source.appendChild(note);
  }
  if (field.tier) {
    const tier = document.createElement("span");
    tier.className = "models-approx-note";
    tier.textContent =
      `matched at tier ${field.tier} of 11 — ${field.tier_label || ""}`.trim();
    source.appendChild(tier);
  }
  if (field.approximate) {
    const warn = document.createElement("span");
    warn.className = "models-approx-note models-approx-warn";
    // Agreement is over the rows that actually published the field, which is
    // never the same as the number of rows that merely share the name.
    const agreement =
      field.agreement === null || field.agreement === undefined
        ? "agreement unreported"
        : `${Math.round(field.agreement * 100)}% agreement`;
    const matches =
      field.match_count === null || field.match_count === undefined
        ? "an unknown number of"
        : String(field.match_count);
    const reporters =
      field.reporters === null || field.reporters === undefined
        ? ""
        : ` across ${field.reporters} that published one`;
    warn.textContent = `guessed from ${matches} same-named row(s) in other providers, ${agreement}${reporters}`;
    source.appendChild(warn);
  }
  // `field.note` is rendered once, above, beside the badge it explains. A
  // second copy used to be appended here, so every row carrying a note read
  // its sentence twice.
  tr.appendChild(source);
  return tr;
}

/* models.dev publishes these per million tokens, which is how every price
   list a reader has ever seen is written, so that is how they are shown. The
   request log stores per token; the two are the same number, and the division
   happens once, at the fetcher, because the two sources' units are exact
   opposites and a conversion at a call site is a 1,000,000x bug waiting. */
const PRICE_FIELD_LABELS = [
  ["input_price", "input price (USD / 1M)"],
  ["output_price", "output price (USD / 1M)"],
  ["cache_read_price", "cache read price (USD / 1M)"],
  ["cache_write_price", "cache write price (USD / 1M)"],
  ["reasoning_price", "reasoning price (USD / 1M)"],
];

/* A published zero is a price -- a free tier saying so -- and renders as
   "$0.00", never as "not reported". The two are different facts, and every
   surface in this feature has to keep them apart. */
function formatPriceValue(value) {
  if (value === null || value === undefined) return "not reported";
  if (typeof value !== "number") return String(value);
  if (value === 0) return "$0.00 (free)";
  if (value < 0.01) return `$${value.toFixed(6)}`;
  return `$${value.toFixed(2)}`;
}

function formatCapabilityValue(value) {
  if (value === null || value === undefined) return "not reported";
  if (Array.isArray(value)) {
    if (!value.length) return "none published";
    return value
      .map((entry) =>
        Array.isArray(entry) ? `${entry[0]}=${entry[1]}` : String(entry),
      )
      .join(", ");
  }
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "number") return value.toLocaleString();
  return String(value);
}

/* Both writes patch modelsState.data and refresh only the rows that changed.
   Rebuilding the whole tree on every save discarded unsaved edits in every
   other open editor, and destroyed the button's own status element before the
   "Saved" confirmation could land in it -- a save that worked looked like a
   save that did nothing. */
function applyModelsData(next) {
  modelsState.data = next;
  const notice = byId("modelsHideOnlyNotice");
  if (notice) notice.textContent = next.visibility.hide_only_notice || "";
  syncModelsPatternFields(next);
  renderModelsOwnedElsewhere(next.overrides.owned_elsewhere || {});
  renderModelsHiddenRoutes(next.visibility.hidden_route_refs || []);
}

function findModelInData(modelRef) {
  const data = modelsState.data;
  if (!data) return null;
  for (const provider of data.providers || []) {
    for (const model of provider.models || []) {
      if (model.model_ref === modelRef) return model;
    }
  }
  return null;
}

function refreshProviderRow(providerId) {
  const data = modelsState.data;
  if (!data) return;
  const provider = (data.providers || []).find(
    (entry) => entry.provider_id === providerId,
  );
  if (!provider) return;
  document.querySelectorAll(".models-provider").forEach((node) => {
    if (node.dataset.provider !== providerId) return;
    const toggle = node.querySelector(".models-provider-toggle");
    if (!toggle) return;
    toggle
      .querySelectorAll(".models-chip-forced")
      .forEach((chip) => chip.remove());
    if (providerHasOverrides(provider)) {
      toggle.appendChild(buildModelsChip("forced", "provider override"));
    }
  });
  // A provider override changes what every model under it sends, so the open
  // model bodies below it have to be repainted too.
  refreshModelRows((provider.models || []).map((model) => model.model_ref));
}

/* The one repaint. There used to be two -- a single-write one that moved the
   checkbox and left its word behind, and this one that moved both -- so the
   same row behaved differently depending on which control had touched it.
   One pass over the rendered rows for many refs, because a per-ref function
   running its own document-wide query is quadratic at the size a bulk action
   reaches. */
function refreshModelRows(modelRefs) {
  const wanted = new Set(modelRefs);
  const editable =
    (modelsState.data && modelsState.data.overrides.editable_parameters) || [];
  document.querySelectorAll("details.models-model").forEach((node) => {
    const ref = node.querySelector(".models-ref");
    if (!ref || !wanted.has(ref.textContent)) return;
    const model = findModelInData(ref.textContent);
    if (!model) return;
    const summary = node.querySelector("summary");
    if (summary) node.replaceChild(buildModelSummary(model), summary);
    const row = node.parentElement;
    const state = row && row.querySelector(".models-visible-state");
    if (state) fillModelsVisibilityState(state, model);
    const body = node.querySelector(".models-model-body");
    if (!body || body.dataset.filled !== "1") return;
    const readouts = body.querySelector(".models-readouts");
    if (readouts) fillModelReadouts(readouts, model);
    else fillModelBody(body, model, editable);
  });
}

/* How much of the selection is already in each state. Indexed once rather
   than searched per ref: the payload holds 1,120 models on a real install and
   a selection can hold every one of them. */
function modelsSelectionVisibility() {
  const index = new Map();
  ((modelsState.data && modelsState.data.providers) || []).forEach((provider) => {
    (provider.models || []).forEach((model) => index.set(model.model_ref, model));
  });
  let hidden = 0;
  let shown = 0;
  modelsState.selected.forEach((ref) => {
    const model = index.get(ref);
    if (!model) return;
    if (model.visible) shown += 1;
    else hidden += 1;
  });
  return { hidden, shown };
}

/* The selection covers 100% of exactly one provider's models, or null. */
function modelsWholeProviderSelection() {
  const refs = Array.from(modelsState.selected);
  if (!refs.length) return null;
  const providerId = modelsProviderOf(refs[0]);
  if (!providerId) return null;
  const provider = modelsProviderEntry(providerId);
  if (!provider) return null;
  const all = (provider.models || []).map((model) => model.model_ref);
  if (!all.length || all.length !== refs.length) return null;
  if (!all.every((ref) => modelsState.selected.has(ref))) return null;
  return { providerId, count: all.length, glob: `${providerId}/*` };
}

function renderModelsBulkBar() {
  const bar = byId("modelsBulkBar");
  if (!bar) return;
  bar.textContent = "";
  const { count, providers } = modelsSelectionSummary();
  bar.hidden = count === 0;
  if (!count) return;
  // The global action bar is two lines tall on some views and one on others,
  // so the offset is measured rather than guessed: a hardcoded 56px left this
  // bar half hidden behind it on the Models page.
  const globalBar = document.querySelector(".action-bar");
  bar.style.bottom = `${globalBar ? globalBar.offsetHeight : 56}px`;
  const line = document.createElement("span");
  line.className = "models-bulk-count";
  line.textContent = `${count} selected across ${providers} provider(s)`;
  bar.appendChild(line);
  // No tri-state control -- "Hide" on a mixed selection hides all of it, which
  // is correct and unsurprising -- but the button says how much of the work is
  // already done, which is the fact the bar used to leave the user to count.
  const already = modelsSelectionVisibility();
  [
    ["show", "Show", already.shown, "already shown"],
    ["hide", "Hide", already.hidden, "already hidden"],
    ["invert", "Invert", 0, ""],
  ].forEach(([action, label, done, phrase]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary-button models-bulk-button";
    const suffix = done ? ` (${done} ${phrase})` : "";
    button.textContent = `${label} ${count} selected${suffix}`;
    button.setAttribute("aria-label", `${label} the ${count} selected model(s)`);
    button.addEventListener("click", () => {
      runModelsBulk({
        scope: "selection",
        action,
        providerId: null,
        refs: Array.from(modelsState.selected),
        affected: count,
        button,
      });
    });
    bar.appendChild(button);
  });
  // Offered, never taken automatically: a selection that happens to cover a
  // whole provider today is still a fact about a closed set, and promoting it
  // to a standing policy is the user's call. Not offering it at all is how an
  // install reaches 994 exact patterns and not one glob.
  const whole = modelsWholeProviderSelection();
  if (whole) {
    const promote = document.createElement("button");
    promote.type = "button";
    promote.className = "secondary-button models-bulk-button models-bulk-promote";
    promote.textContent = `Hide all as one pattern, ${whole.glob}`;
    promote.setAttribute(
      "aria-label",
      `Hide every ${whole.providerId} model by writing ${whole.glob} instead of ${whole.count} exact patterns`,
    );
    promote.addEventListener("click", () => {
      runModelsBulk({
        scope: "provider",
        action: "hide",
        providerId: whole.providerId,
        // No refs is what tells the server this is a policy, not a selection.
        refs: [],
        affected: whole.count,
        button: promote,
      });
    });
    bar.appendChild(promote);
  }
  const clear = document.createElement("button");
  clear.type = "button";
  clear.className = "secondary-button models-bulk-button";
  clear.textContent = "Clear";
  clear.addEventListener("click", clearModelsSelection);
  bar.appendChild(clear);
}

/* A persistent, dismissible status panel rather than a toast. The skill's
   toast rule says auto-dismiss after three to five seconds; a report that
   names which of your own globs overruled which refs, and carries the only
   Undo, must not vanish on a timer. One atomic live region, one whole
   sentence, no bare numbers. */
function renderModelsBulkResult(result) {
  const target = byId("modelsBulkResult");
  if (!target) return;
  target.textContent = "";
  target.hidden = false;
  const rows = result.results || [];
  const unhonored = rows.filter((row) => row.honored === false);
  const verb =
    result.action === "hide"
      ? "Hid"
      : result.action === "show"
        ? "Showed"
        : "Inverted";
  const lead = document.createElement("p");
  const where = result.provider_id ? ` ${result.provider_id}` : "";
  let text = `${verb} ${result.honored_count} of ${rows.length}${where} model(s).`;
  if (result.wrote_glob) {
    text += ` Written as one pattern, ${result.wrote_glob}.`;
  } else if (rows.length && result.action !== "show") {
    // A show that only removed patterns has nothing to announce as written,
    // and saying otherwise would name a write that did not happen.
    text += " Written as one exact pattern per model.";
  }
  if ((result.removed_patterns || []).length) {
    text += ` Removed ${result.removed_patterns.join(", ")} from your lists.`;
  }
  text += " Routing is unaffected either way.";
  lead.textContent = text;
  target.appendChild(lead);

  // Lifting a provider glob restores the per-model choices it was shadowing
  // rather than clearing them, so the rows that stay hidden are not a failure
  // -- they are the state the Hide all was covering up. Saying which it is
  // beats leaving the user to read "17 did not change" as a bug.
  const lifted = (result.removed_patterns || []).some((pattern) =>
    pattern.endsWith("/*"),
  );
  if (result.action === "show" && lifted && unhonored.length) {
    const restored = document.createElement("p");
    restored.textContent =
      `Your per-model choices from before the Hide all are back: ` +
      `${unhonored.length} of them stay hidden by their own patterns. ` +
      `Press Show all again to clear those too.`;
    target.appendChild(restored);
  }

  if (unhonored.length) {
    // Grouped by the pattern that won, so one offending glob is named once
    // rather than three hundred times.
    const byPattern = new Map();
    unhonored.forEach((row) => {
      const pattern = row.blocked_by || "";
      if (!byPattern.has(pattern)) byPattern.set(pattern, []);
      byPattern.get(pattern).push(row.model_ref);
    });
    const list = document.createElement("ul");
    list.className = "models-bulk-blocked";
    byPattern.forEach((refs, pattern) => {
      const item = document.createElement("li");
      item.textContent = pattern
        ? `${refs.length} of them did not change: your pattern ${
            pattern === "__allow_list__"
              ? 'in the "Show only these" list'
              : pattern
          } overrules an exact tick.`
        : `${refs.length} of them did not change: your own per-model hide patterns still name them.`;
      list.appendChild(item);
    });
    target.appendChild(list);
    const show = document.createElement("button");
    show.type = "button";
    show.className = "secondary-button models-bulk-button";
    show.textContent = `Show the ${unhonored.length}`;
    show.addEventListener("click", () => {
      modelsState.facet = new Set(unhonored.map((row) => row.model_ref));
      modelsState.paged.clear();
      renderModelsTree();
    });
    target.appendChild(show);
  }

  appendModelsPanelActions(target);
}

/* One sentence carrying the same Undo and Dismiss the bulk panel carries, for
   the writes that are not bulk gestures: removing a pattern a row named, and
   the glob migration. */
function renderModelsWritePanel(sentence) {
  const target = byId("modelsBulkResult");
  if (!target) return;
  target.textContent = "";
  target.hidden = false;
  const lead = document.createElement("p");
  lead.textContent = sentence;
  target.appendChild(lead);
  appendModelsPanelActions(target);
}

function appendModelsPanelActions(target) {
  if (modelsState.undo) {
    const undo = document.createElement("button");
    undo.type = "button";
    undo.className = "secondary-button models-bulk-button models-bulk-undo";
    undo.textContent = "Undo";
    undo.title =
      "Restores the two pattern lists as they were before this action. It does not undo a hand edit of the pattern fields made since.";
    undo.addEventListener("click", () => {
      const previous = modelsState.undo;
      modelsState.undo = null;
      api("/admin/api/model-admin/visibility", {
        method: "POST",
        body: JSON.stringify({
          allow: (previous.allow || []).join(","),
          deny: (previous.deny || []).join(","),
        }),
      })
        .then(() => loadModelsView(true))
        .then(() => {
          target.textContent = "";
          target.hidden = true;
          showMessage("Restored the pattern lists.", "ok");
        })
        .catch((error) => showMessage(error.message, "error"));
    });
    target.appendChild(undo);
  }

  const dismiss = document.createElement("button");
  dismiss.type = "button";
  dismiss.className = "secondary-button models-bulk-button";
  dismiss.textContent = "Dismiss";
  dismiss.addEventListener("click", () => {
    target.textContent = "";
    target.hidden = true;
  });
  target.appendChild(dismiss);
}

/* Offered, never applied on its own. One real install reached 994 exact deny
   patterns and not a single glob -- a ~30 KB line in the managed env file,
   parsed, folded and rewritten on every write -- without ever asking for one.
   The preview names both counts and proves the fold hides exactly the same
   models; only then is the write offered, and it is undoable. */
async function runModelsGlobMigration(button, apply) {
  setModelsVisibilityStatus(apply ? "Migrating..." : "Checking...");
  try {
    const result = await api("/admin/api/model-admin/visibility/migrate-globs", {
      method: "POST",
      body: JSON.stringify({ apply: Boolean(apply) }),
    });
    if ((result.errors || []).length) {
      setModelsVisibilityStatus(result.errors.join(" "), "error");
      return;
    }
    setModelsVisibilityStatus("");
    if (apply) {
      modelsState.undo = result.previous || null;
      await loadModelsView(true);
    }
    renderModelsMigration(result, button);
  } catch (error) {
    setModelsVisibilityStatus(error.message, "error");
  }
}

function renderModelsMigration(result, button) {
  const target = byId("modelsBulkResult");
  if (!target) return;
  target.textContent = "";
  target.hidden = false;
  const lead = document.createElement("p");
  const providers = result.providers || [];
  if (!providers.length) {
    lead.textContent =
      "Nothing to fold: no provider has every one of its models hidden by " +
      "exact patterns of their own.";
    target.appendChild(lead);
    appendModelsPanelActions(target);
    return;
  }
  const removed = (result.removed_patterns || []).length;
  const added = result.added_patterns || [];
  lead.textContent =
    `${result.applied ? "Folded" : "Would fold"} ${removed} exact pattern(s) ` +
    `across ${providers.length} provider(s) into ${added.join(", ")}: ` +
    `${result.pattern_count_before} pattern(s) become ${result.pattern_count_after}. ` +
    `${result.hidden_before} model(s) hidden before, ${result.hidden_after} after` +
    (result.identical
      ? " -- identical, which is the only reason this is offered."
      : " -- not identical, so it will not be written.");
  target.appendChild(lead);
  if (!result.applied && result.identical) {
    const go = document.createElement("button");
    go.type = "button";
    go.className = "secondary-button models-bulk-button";
    go.textContent = `Write the ${added.length} glob(s)`;
    go.addEventListener("click", () => runModelsGlobMigration(button, true));
    target.appendChild(go);
  }
  appendModelsPanelActions(target);
}

/* One POST and one settings commit per gesture. The per-model route re-reads
   the whole 3.4 MB catalogue after every tick; three hundred of those is two
   thirds of a gigabyte and several minutes, and -- because each one derives
   its replacement pattern list from a base it read before the others
   committed -- it also loses writes. */
async function runModelsBulk(request) {
  const refs = request.refs || [];
  const affected = request.affected || refs.length;
  if (request.scope === "selection" && !refs.length) {
    setModelsVisibilityStatus("Select at least one model first.", "error");
    return;
  }
  if (!affected) {
    setModelsVisibilityStatus("Nothing on screen to act on.", "error");
    return;
  }
  const button = request.button;
  // No modal: visibility is display-only and reversible, and a dialog on every
  // "Hide all" is precisely the friction being removed. One inline confirm for
  // the rare very large action.
  if (button && affected >= 200 && button.dataset.confirming !== "1") {
    const label = button.textContent;
    button.dataset.confirming = "1";
    button.textContent = `${label} ${affected} -- confirm`;
    window.setTimeout(() => {
      if (button.dataset.confirming !== "1") return;
      button.dataset.confirming = "";
      button.textContent = label;
    }, 5000);
    return;
  }
  if (button) button.dataset.confirming = "";
  setModelsVisibilityStatus("Applying...");
  try {
    const result = await api("/admin/api/model-admin/visibility/bulk", {
      method: "POST",
      body: JSON.stringify({
        scope: request.scope,
        action: request.action,
        provider_id: request.providerId,
        model_refs: refs,
      }),
    });
    if ((result.errors || []).length) {
      modelsState.undo = null;
      setModelsVisibilityStatus(result.errors.join(" "), "error");
      return;
    }
    setModelsVisibilityStatus("");
    modelsState.undo = result.previous || null;
    applyModelsBulkResult(result);
    clearModelsSelection();
    renderModelsBulkResult(result);
  } catch (error) {
    modelsState.undo = null;
    setModelsVisibilityStatus(error.message, "error");
  }
}

/* Patch the payload the page already holds instead of re-fetching it: one
   bulk gesture must not cost the 3.4 MB the per-tick refetch costs today. The
   Reload button is still there for the server's word on it. */
function applyModelsBulkResult(result) {
  const data = modelsState.data;
  if (!data) return;
  const rows = new Map(
    (result.results || []).map((row) => [row.model_ref, row]),
  );
  (data.providers || []).forEach((provider) => {
    let hidden = 0;
    (provider.models || []).forEach((model) => {
      const row = rows.get(model.model_ref);
      if (row) {
        model.visible = row.visible;
        // What dictates the row's state now, which the row keeps saying long
        // after the result panel is dismissed -- not the same question as
        // "what stopped this gesture", which the panel answers once.
        model.hidden_by = row.hidden_by || "";
      }
      if (!model.visible) hidden += 1;
    });
    provider.hidden_count = hidden;
  });
  const visibility = result.visibility || {};
  data.visibility.allow_raw = (visibility.allow || []).join(",");
  data.visibility.deny_raw = (visibility.deny || []).join(",");
  syncModelsPatternFields(data);
  renderModelsPatternProvenance();
  refreshModelRows(Array.from(rows.keys()));
  refreshModelsProviderHeads();
  renderModelsFacets();
}

/* The header's "N hidden" chip is the answer to the question the sticky header
   exists to answer, so a bulk hide that left it saying zero would undo the
   point of the header. */
function refreshModelsProviderHeads() {
  document.querySelectorAll(".models-provider").forEach((node) => {
    const provider = modelsProviderEntry(node.dataset.provider);
    const toggle = node.querySelector(".models-provider-toggle");
    if (!provider || !toggle) return;
    toggle
      .querySelectorAll(".models-chip-hidden")
      .forEach((chip) => chip.remove());
    if (provider.hidden_count) {
      toggle.appendChild(
        buildModelsChip("hidden", `${provider.hidden_count} hidden`),
      );
    }
  });
}

/* The ticks and the two <textarea>s still share one field. Saying how the
   list is made up is the cheap half of that problem: a user who sees "3
   patterns you wrote by hand" knows Save patterns is about to overwrite the
   other 314. */
function renderModelsPatternProvenance() {
  const target = byId("modelsPatternProvenance");
  const data = modelsState.data;
  if (!target || !data) return;
  const deny = (data.visibility.deny_raw || "")
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);
  const allow = (data.visibility.allow_raw || "")
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);
  const globs = deny
    .concat(allow)
    .filter((entry) => /[*?[]/.test(entry)).length;
  const exact = deny.length + allow.length - globs;
  target.hidden = deny.length + allow.length === 0;
  target.textContent = `${globs} glob pattern(s) and ${exact} exact model pattern(s) in your two lists.`;
}

/* The wire surface has a writer of its own because it has a validator of its
   own: the server checks the chosen endpoint against what that provider
   declares it serves, which the parameter grid's route cannot do. Empty
   unpins. */
async function saveModelSurface(key, surface) {
  applyModelsData(
    await api("/admin/api/model-admin/surface", {
      method: "POST",
      body: JSON.stringify({ key, surface }),
    }),
  );
  refreshModelRows([key]);
}

async function saveModelOverrides(scope, key, updates) {
  applyModelsData(
    await api("/admin/api/model-admin/overrides", {
      method: "POST",
      body: JSON.stringify({ scope, key, updates }),
    }),
  );
  if (scope === "provider") refreshProviderRow(key);
  else refreshModelRows([key]);
}

function renderModelsPreview(result) {
  const target = byId("modelsPreviewResult");
  if (!target) return;
  target.textContent = "";
  target.hidden = false;
  const summary = document.createElement("p");
  summary.textContent = `${result.visible_count} model(s) would stay visible; ${result.hidden_count} would be hidden.`;
  target.appendChild(summary);
  const hidden = result.hidden_model_refs || [];
  if (hidden.length) {
    const list = document.createElement("ul");
    hidden.slice(0, 200).forEach((ref) => {
      const item = document.createElement("li");
      item.textContent = ref;
      list.appendChild(item);
    });
    target.appendChild(list);
    if (hidden.length > 200) {
      const more = document.createElement("p");
      more.textContent = `...and ${hidden.length - 200} more.`;
      target.appendChild(more);
    }
  }
  const routes = result.hidden_route_refs || [];
  if (routes.length) {
    const warn = document.createElement("p");
    warn.className = "models-route-warning-inline";
    warn.textContent = `${routes.length} configured route(s) would be hidden: ${routes
      .map((route) => route.model_ref)
      .join(", ")}. They would still serve requests.`;
    target.appendChild(warn);
  }
}

function initModelsView() {
  const preview = byId("modelsPreviewVisibility");
  const save = byId("modelsSaveVisibility");
  const filter = byId("modelsFilter");
  const reload = byId("modelsReload");
  if (!preview || !save) return;

  const patterns = () => ({
    allow: (byId("modelsAllowPatterns") || {}).value || "",
    deny: (byId("modelsDenyPatterns") || {}).value || "",
  });

  preview.addEventListener("click", () => {
    setModelsVisibilityStatus("Previewing...");
    api("/admin/api/model-admin/visibility/preview", {
      method: "POST",
      body: JSON.stringify(patterns()),
    })
      .then((result) => {
        renderModelsPreview(result);
        setModelsVisibilityStatus("");
      })
      .catch((error) => setModelsVisibilityStatus(error.message, "error"));
  });

  save.addEventListener("click", () => {
    setModelsVisibilityStatus("Saving...");
    api("/admin/api/model-admin/visibility", {
      method: "POST",
      body: JSON.stringify(patterns()),
    })
      .then(() => loadModelsView(true))
      .then(() => setModelsVisibilityStatus("Saved"))
      .catch((error) => setModelsVisibilityStatus(error.message, "error"));
  });

  if (filter) {
    // A thousand model refs re-filter on every keystroke; debounce so typing
    // does not queue a whole tree rebuild per character.
    let pending = null;
    filter.addEventListener("input", () => {
      if (pending) window.clearTimeout(pending);
      pending = window.setTimeout(() => {
        pending = null;
        const next = filter.value.trim().toLowerCase();
        if (next === modelsState.filter) return;
        modelsState.filter = next;
        modelsState.paged.clear();
        renderModelsTree();
      }, 150);
    });
  }
  if (filter) {
    filter.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      // Enter commits the query: open every provider that matched, which is
      // the thing the count sentence has always described but never showed.
      modelsState.filter = filter.value.trim().toLowerCase();
      modelsState.paged.clear();
      ((modelsState.data && modelsState.data.providers) || []).forEach(
        (provider) => {
          if (modelsFilteredFor(provider).length) {
            modelsState.open.add(`provider:${provider.provider_id}`);
          }
        },
      );
      renderModelsTree();
    });
  }
  const migrate = byId("modelsMigrateGlobs");
  if (migrate) {
    migrate.addEventListener("click", () => runModelsGlobMigration(migrate, false));
  }

  if (reload) {
    reload.addEventListener("click", () => {
      clearModelsSelection();
      loadModelsView(true).catch((error) => showMessage(error.message, "error"));
    });
  }

  const tree = byId("modelsTree");
  // Bound on the container: a pointerover dispatched on a row does not reach a
  // listener on the checkbox cell inside it, because events bubble up.
  if (tree) tree.addEventListener("pointerover", continueModelsDrag);
  document.addEventListener("pointerup", endModelsDrag);
  document.addEventListener("pointercancel", endModelsDrag);
  document.addEventListener("keydown", (event) => {
    const view = byId("view-models");
    if (!view || view.hidden) return;
    const target = event.target;
    const typing =
      target &&
      (target.tagName === "INPUT" ||
        target.tagName === "TEXTAREA" ||
        target.isContentEditable);
    if (event.key === "/" && !typing) {
      event.preventDefault();
      if (filter) filter.focus();
      return;
    }
    if (event.key !== "Escape") return;
    // Escape belongs to whichever modal is open; only when none is does it
    // mean "drop this selection".
    const modals = [
      "webSearchDetailModal",
      "exportModal",
      "reqDetailModal",
    ].map(byId);
    if (modals.some((modal) => modal && !modal.hidden)) return;
    if (modelsState.selected.size) {
      clearModelsSelection();
      return;
    }
    if (filter && document.activeElement === filter) filter.blur();
  });
}

initModelsView();

/* ------------------------------------------------------ route rail wiring
   The drop target is tracked by pointerover on the container rather than
   computed from coordinates at pointerup: a pointerover dispatched on a row
   does not reach a listener bound to the grip inside it, and jsdom has no
   layout to hit-test against. */
function initRouteRails() {
  const view = byId("view-model_config");
  if (view) view.addEventListener("pointerover", continueRouteDrag);
  // The same rails are drawn again in each coding agent's Tiers section, and a
  // drag only continues while something is listening for the pointer moving
  // over a drop target.
  const agents = byId("view-coding_agents");
  if (agents) agents.addEventListener("pointerover", continueRouteDrag);
  document.addEventListener("pointerup", endRouteDrag);
  // The key rails live on other views entirely, so they get their own
  // document-level release rather than sharing the route one.
  document.addEventListener("pointerup", endKeyDrag);
  document.addEventListener("pointercancel", () => {
    if (keyPoolDrag) {
      keyPoolDrag.target = null;
      endKeyDrag();
    }
  });
  document.addEventListener("pointercancel", () => {
    if (state.routeDrag) endRouteDrag(null);
  });
  document.addEventListener("keydown", (event) => {
    const routing = byId("view-model_config");
    if (!routing || routing.hidden) return;
    const target = event.target;
    const typing =
      target &&
      (target.tagName === "INPUT" ||
        target.tagName === "TEXTAREA" ||
        target.isContentEditable);
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
      // Never inside a combobox: the browser's own text undo belongs to
      // whoever is typing, and fighting it would be a worse bug than no undo.
      if (typing) return;
      if (!state.routeUndo) return;
      event.preventDefault();
      undoLastRouteDrag();
      return;
    }
    if (event.key !== "Escape") return;
    const modals = ["webSearchDetailModal", "exportModal", "reqDetailModal"].map(byId);
    if (modals.some((modal) => modal && !modal.hidden)) return;
    if (state.routeDrag) {
      // Abandoned, not applied: the target is dropped so endRouteDrag has
      // nothing to act on.
      state.routeDrag = null;
      clearRouteDropIndicator();
      document
        .querySelectorAll(".route-grid, .route-vision")
        .forEach((node) => node.classList.remove("is-dragging"));
      return;
    }
    if (state.routeSelection.size) clearRouteSelection();
  });
}

initRouteRails();
