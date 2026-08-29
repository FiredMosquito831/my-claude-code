
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
    id: "claude",
    label: "Configure Claude Code",
    title: "Configure Claude Code",
    sections: [],
    containerId: null,
  },
  {
    id: "model_config",
    label: "Model Config",
    title: "Model Config",
    sections: ["models", "reasoning", "web_tools"],
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
    // is what these tables and content search can ever display.
    sections: ["request_log"],
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
    explicit_env_file: "FCC_ENV_FILE",
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
    if (byId("reqFilterSearch")) byId("reqFilterSearch").value = f.search || "";
    if (f.status && byId("reqFilterStatus")) byId("reqFilterStatus").value = f.status;
    if (byId("reqFilterEndpoint")) byId("reqFilterEndpoint").value = f.endpoint || "";
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

  if (activeView.id === "docs") {
    loadDocsView().catch((error) => showMessage(error.message, "error"));
  }
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
        if (step.id === "guide") {
          updateOnboarding({ visited: ["guide"] }).catch((error) =>
            showMessage(error.message, "error"),
          );
        }
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
  { id: "fable", label: "Fable", modelKey: "MODEL_FABLE", chainKey: "MODEL_FABLE_FALLBACKS" },
  { id: "opus", label: "Opus", modelKey: "MODEL_OPUS", chainKey: "MODEL_OPUS_FALLBACKS" },
  { id: "sonnet", label: "Sonnet", modelKey: "MODEL_SONNET", chainKey: "MODEL_SONNET_FALLBACKS" },
  { id: "haiku", label: "Haiku", modelKey: "MODEL_HAIKU", chainKey: "MODEL_HAIKU_FALLBACKS" },
];

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
function buildVisionHop(tierModel, visionModel) {
  const hop = document.createElement("p");
  hop.className = `route-vision-hop${visionModel ? "" : " is-unset"}`;

  const label = document.createElement("span");
  label.className = "route-vision-hop-label";
  label.textContent = "Images";
  hop.appendChild(label);

  const arrow = document.createElement("span");
  arrow.className = "route-vision-hop-arrow";
  arrow.setAttribute("aria-hidden", "true");
  arrow.textContent = "→";
  hop.appendChild(arrow);

  const target = document.createElement("code");
  target.textContent = visionModel || "nowhere — set a Vision adapter";
  hop.appendChild(target);

  const why = document.createElement("span");
  why.className = "route-vision-hop-why";
  why.textContent = visionModel
    ? `${tierModel} cannot read them`
    : `${tierModel} cannot read them, so they will fail here`;
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
function updateVisionRouting() {
  const visionModel = String(
    (state.fields.get("MODEL_VISION") || {}).value || "",
  ).trim();
  const covered = [];
  ROUTE_TIERS.forEach((tier) => {
    const card = document.querySelector(`.route-card[data-tier="${tier.id}"]`);
    if (!card) return;
    const existing = card.querySelector(".route-vision-hop");
    if (existing) existing.remove();
    const tierModel = routedModelValue(tier.modelKey);
    if (!tierModel || !state.blindModels.has(tierModel)) return;
    covered.push(tier.label);
    card.appendChild(buildVisionHop(tierModel, visionModel));
  });

  const summary = document.querySelector(".route-vision-summary");
  if (!summary) return;
  summary.classList.toggle("is-idle", covered.length === 0);
  summary.textContent = covered.length
    ? `Currently covers ${covered.join(", ")} — those tiers picked a model ` +
      "that cannot read images."
    : "No tier needs it right now: no tier's model is known to reject images.";
}

function renderModelRouting(fields) {
  const fieldByKey = new Map(fields.map((field) => [field.key, field]));
  const wrap = document.createElement("div");
  wrap.className = "route-layout";

  const rule = document.createElement("p");
  rule.className = "route-rule";
  rule.textContent =
    "Each tier tries its models in order. If one cannot serve a request the " +
    "next takes over, up until the response starts streaming.";
  wrap.appendChild(rule);

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
    vision.dataset.key = visionField.key;

    const head = document.createElement("header");
    head.className = "route-card-head";
    const name = document.createElement("h4");
    name.className = "route-tier";
    name.textContent = "Vision adapter";
    head.appendChild(name);
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
    ...ROUTE_TIERS.flatMap((tier) => [tier.modelKey, tier.chainKey]),
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

/** The control bound to a key, never the .field wrapper that repeats data-key. */
const CONTROL_FOR_KEY = (key) =>
  `input[data-key="${key}"], select[data-key="${key}"], textarea[data-key="${key}"]`;

function chainLength(modelKey, chainKey) {
  const primary = liveValue(modelKey).trim();
  const chain = liveValue(chainKey)
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
  return (primary && primary.toLowerCase() !== "none" ? 1 : 0) + chain.length;
}

/** Mirror of `_attempt_deadline` + `_chunk_timeout` before the first chunk. */
function firstTokenShare(total, first, n) {
  const share = total > 0 ? total / Math.max(1, n) : Infinity;
  const cap = first > 0 ? first : Infinity;
  return Math.min(share, cap);
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

  const total = Number(liveValue("FALLBACK_TOTAL_TIMEOUT")) || 0;
  const first = Number(liveValue("FALLBACK_FIRST_TOKEN_TIMEOUT")) || 0;
  const httpRead = Number(liveValue("HTTP_READ_TIMEOUT")) || 0;
  const shutdown = Number(liveValue("SERVER_GRACEFUL_SHUTDOWN_SECONDS")) || 0;
  const routes = calculatorRoutes().map((route) => ({
    ...route,
    share: firstTokenShare(total, first, route.models),
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

  const terms = [];
  if (first > 0) terms.push(`the first-token deadline (${first} s)`);
  if (total > 0) {
    terms.push(
      `its share of the total budget (${total} ÷ ${longest.models} = ` +
        `${formatShare(total / longest.models)})`,
    );
  }
  formula.textContent =
    terms.length > 1 ? `= the smaller of ${terms.join(" and ")}.` : `= ${terms[0]}.`;

  // One warning at a time, most severe first. The word "Warning" carries the
  // meaning; the colour is redundant.
  let text = "";
  if (first > 0 && total > 0 && total / longest.models < first) {
    text =
      "Warning: The first-token deadline never applies on this route -- the " +
      "budget share is smaller. A total budget of " +
      `${Math.ceil(first * longest.models)} s would give every model the ` +
      `${first} s you asked for.`;
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
  rule.textContent =
    "A 401/403 walks the lockout ladder; a 429 benches the key for the " +
    "provider's Retry-After, or the cooldown above when it sends none; a " +
    "timeout or 5xx costs a key nothing.";
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
};

function renderSections(sections, fields) {
  state.modelComboboxes.clear();
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
      const gridFields = sectionFields.filter(
        (field) =>
          !field.key.endsWith("_ROTATION") &&
          !(field.section === "websearch" && field.advanced),
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
        sectionEl.appendChild(renderer(gridFields));
      } else {
        const grid = document.createElement("div");
        grid.className = "field-grid";
        gridFields.forEach((field) => {
          grid.appendChild(renderField(field));
        });
        sectionEl.appendChild(grid);
      }

      // The providers section handles "advanced" per-card (see
      // renderProviderGroups) rather than with this section-wide toggle.
      if (section.id !== "providers" && gridFields.some((field) => field.advanced)) {
        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "ghost-button advanced-toggle";
        toggle.textContent = "Show advanced";
        toggle.addEventListener("click", () => {
          const showing = sectionEl.classList.toggle("show-advanced");
          toggle.textContent = showing ? "Hide advanced" : "Show advanced";
        });
        sectionEl.appendChild(toggle);
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
  fields.forEach((field) => body.appendChild(renderField(field)));

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
    input.addEventListener("input", updateDirtyState);
    input.addEventListener("change", updateDirtyState);
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

  const list = document.createElement("div");
  list.className = "key-manager-list";
  if (info.count === 0) {
    const empty = document.createElement("div");
    empty.className = "key-manager-empty";
    empty.textContent = "No keys configured.";
    list.appendChild(empty);
  }
  info.keys.forEach((masked, index) => {
    const row = document.createElement("div");
    row.className = "key-manager-row";

    const label = document.createElement("code");
    label.className = "key-manager-key";
    label.textContent = masked;

    row.appendChild(label);

    const health = Array.isArray(info.health) ? info.health[index] : null;
    if (health && health.state) {
      row.appendChild(keyHealthBadge(health));
    }

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "ghost-button key-manager-remove";
    remove.textContent = "Remove";
    remove.disabled = info.locked;
    remove.addEventListener("click", () =>
      removeCredentialKey(field, index, remove),
    );

    row.appendChild(remove);
    list.appendChild(row);
  });
  panel.appendChild(list);

  const addRow = document.createElement("div");
  addRow.className = "key-manager-add";
  const input = document.createElement("input");
  input.type = "password";
  input.autocomplete = "off";
  input.placeholder = info.locked
    ? "Locked by process environment"
    : "Paste a key, or several separated by commas";
  input.disabled = info.locked;

  const add = document.createElement("button");
  add.type = "button";
  add.className = "secondary-button";
  add.textContent = "Add key";
  add.disabled = info.locked;

  const submit = () => addCredentialKey(field, input, add);
  add.addEventListener("click", submit);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") submit();
  });

  addRow.append(input, add);
  panel.appendChild(addRow);

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

  const requests = health.request_count || 0;
  const failures = health.failure_count || 0;
  badge.title = `${state}${backIn} — ${requests} requests, ${failures} failures`;
  return badge;
}

async function reloadAndReopenKeyManager(field, message) {
  state.reopenKeyManager = field.key;
  await load();
  showMessage(message, "ok");
}

async function addCredentialKey(field, input, button) {
  const value = input.value.trim();
  if (!value) return;
  button.disabled = true;
  try {
    const result = await api(`/admin/api/credentials/${field.key}/keys`, {
      method: "POST",
      body: JSON.stringify({ key: value }),
    });
    await reloadAndReopenKeyManager(
      field,
      `Added key ${result.added} (${result.count} configured). Applied.`,
    );
  } catch (error) {
    button.disabled = false;
    showMessage(`Could not add key: ${error.message}`, "error");
  }
}

async function removeCredentialKey(field, index, button) {
  button.disabled = true;
  try {
    const result = await api(
      `/admin/api/credentials/${field.key}/keys/${index}`,
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
  }

  addRow(value, notify) {
    const row = {};
    const rowField = {
      type: "model",
      key: `${this.field.key}__chain_${this.rowSeq++}`,
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
    wrapper.append(numberEl, combobox.element, upButton, downButton, removeButton);

    Object.assign(row, { wrapper, combobox, numberEl, upButton, downButton, removeButton });
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
    input.addEventListener("change", () => this.renumberPrimary());
    this.renumberPrimary();
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
 */
function syncSharedControls(source) {
  const key = source.dataset.key;
  document
    .querySelectorAll(`input[data-key="${key}"], select[data-key="${key}"]`)
    .forEach((twin) => {
      if (twin !== source && twin.value !== source.value) twin.value = source.value;
    });
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
    "refuses any request that does not report cc_entrypoint=cli. Read " +
    "docs/ANTHROPIC-SUBSCRIPTION.md before using either option below.";
  warning.appendChild(warningText);

  const status = document.createElement("div");
  status.className = "field-description";
  status.textContent = "Checking for available credentials...";

  const importButton = document.createElement("button");
  importButton.type = "button";
  importButton.className = "secondary-button";
  importButton.textContent = "Use Claude Code credentials";
  importButton.disabled = true;

  const loginButton = document.createElement("button");
  loginButton.type = "button";
  loginButton.className = "primary-button";
  loginButton.textContent = "Sign in with Anthropic";

  const buttons = [importButton, loginButton];
  importButton.addEventListener("click", () => {
    importAnthropicOAuthClaudeCode(importButton, buttons, status);
  });
  loginButton.addEventListener("click", () => {
    startAnthropicOAuthLogin(loginButton, buttons, status);
  });

  wrapper.append(warning, status, importButton, loginButton);
  refreshAnthropicOAuthSources(importButton, status);
  return wrapper;
}

async function refreshAnthropicOAuthSources(importButton, status) {
  try {
    const sources = await api("/admin/api/anthropic-oauth/sources");
    const mccNote = sources.mcc.available
      ? `An MCC credential is already stored (${sources.mcc.masked_token}).`
      : "No credential stored in MCC yet.";
    if (sources.claude_code.available) {
      importButton.disabled = false;
      status.textContent =
        `Claude Code credential found (${sources.claude_code.masked_token}). ` +
        mccNote;
    } else {
      importButton.disabled = true;
      status.textContent = sources.mcc.available
        ? `Signed in. ${mccNote}`
        : "No credentials found. Sign in below, or log in to Claude Code first.";
    }
  } catch (error) {
    status.textContent = `Could not check credential sources: ${error.message}`;
  }
}

async function importAnthropicOAuthClaudeCode(button, buttons, status) {
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
      showMessage(
        "Imported the Claude Code credential into MCC's private store.",
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
    refreshAnthropicOAuthSources(buttons[0], status);
  }
}

async function startAnthropicOAuthLogin(button, buttons, status) {
  buttons.forEach((candidate) => {
    candidate.disabled = true;
  });
  const original = button.textContent;
  button.textContent = "Starting sign-in...";
  try {
    const initiate = await api("/admin/api/anthropic-oauth/initiate", {
      method: "POST",
      body: "{}",
    });
    window.open(initiate.authorize_url, "_blank", "noopener");
    showMessage(
      "Anthropic OAuth: approve access in the new tab, then paste the code " +
        "it shows back here.",
      "warn",
    );
    const pasted = window.prompt(
      "Paste the code Anthropic showed after you approved access:",
    );
    if (!pasted || !pasted.trim()) {
      status.textContent = "Sign-in cancelled: no code entered.";
      return;
    }
    const result = await api("/admin/api/anthropic-oauth/complete", {
      method: "POST",
      body: JSON.stringify({
        pasted_code: pasted.trim(),
        verifier: initiate.verifier,
      }),
    });
    if (result.status === "complete") {
      showMessage(
        "Signed in with Anthropic. Credential stored in MCC's private store.",
        "ok",
      );
    }
  } catch (error) {
    showMessage(`Anthropic sign-in failed: ${error.message}`, "error");
  } finally {
    button.textContent = original;
    buttons.forEach((candidate) => {
      candidate.disabled = false;
    });
    refreshAnthropicOAuthSources(buttons[0], status);
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
  const list = document.createElement("div");
  list.className = "ws-key-list";
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
  result.keys.forEach((entry) => {
    const row = document.createElement("div");
    row.className = "ws-key-row";
    const label = document.createElement("span");
    label.className = "ws-key-label";
    label.textContent = entry.key_label || "(empty)";
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
      deleteWebSearchKey(provider, entry.index, panel, remove),
    );
    row.append(label, healthEl, remove);
    list.appendChild(row);
  });
  const form = document.createElement("div");
  form.className = "ws-key-add";
  const input = document.createElement("input");
  input.type = "password";
  input.placeholder = "Paste a new API key";
  input.autocomplete = "off";
  input.disabled = result.locked;
  const add = document.createElement("button");
  add.type = "button";
  add.className = "secondary-button";
  add.textContent = "Add key";
  add.disabled = result.locked;
  add.addEventListener("click", () => addWebSearchKey(provider, input, panel, add));
  form.append(input, add);
  panel.appendChild(form);
  if (result.locked) {
    const note = document.createElement("div");
    note.className = "field-description";
    note.textContent = "This credential is locked by an external source; edit it there.";
    panel.appendChild(note);
  }
}

async function addWebSearchKey(provider, input, panel, button) {
  const key = input.value.trim();
  if (!key) {
    showMessage("Enter a key first", "warn");
    return;
  }
  button.disabled = true;
  try {
    const result = await api(
      `/admin/api/websearch/credentials/${provider.envKey}/keys`,
      { method: "POST", body: JSON.stringify({ key }) },
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

async function deleteWebSearchKey(provider, index, panel, button) {
  button.disabled = true;
  try {
    const result = await api(
      `/admin/api/websearch/credentials/${provider.envKey}/keys/${index}`,
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
  details.textContent =
    `${provider.key_count} key${provider.key_count === 1 ? "" : "s"} · ` +
    `${provider.credential_rotation} · ${provider.model_count} models` +
    (provider.proxy ? ` · proxy ${provider.proxy}` : "");

  const keyList = document.createElement("div");
  keyList.className = "cp-key-list";
  provider.masked_keys.forEach((masked, index) => {
    const row = document.createElement("div");
    row.className = "cp-key-row";
    const label = document.createElement("code");
    label.className = "cp-key-label";
    label.textContent = masked;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "ghost-button";
    remove.textContent = "Remove";
    remove.addEventListener("click", () =>
      removeCustomProviderKey(provider, index, remove),
    );
    row.append(label, remove);
    keyList.appendChild(row);
  });

  const addRow = document.createElement("div");
  addRow.className = "cp-key-add";
  const keyInput = document.createElement("input");
  keyInput.type = "password";
  keyInput.autocomplete = "off";
  keyInput.placeholder = "Paste a key, or several separated by commas";
  const addButton = document.createElement("button");
  addButton.type = "button";
  addButton.className = "secondary-button";
  addButton.textContent = "Add key";
  const submitKey = () => addCustomProviderKey(provider, keyInput, addButton);
  addButton.addEventListener("click", submitKey);
  keyInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") submitKey();
  });
  addRow.append(keyInput, addButton);
  keyList.appendChild(addRow);

  const actions = document.createElement("div");
  actions.className = "card-actions";

  const testButton = document.createElement("button");
  testButton.type = "button";
  testButton.className = "test-button";
  testButton.textContent = "Test";
  testButton.addEventListener("click", () =>
    testCustomProvider(provider, testButton),
  );

  const editButton = document.createElement("button");
  editButton.type = "button";
  editButton.className = "secondary-button";
  editButton.textContent = "Edit";
  editButton.addEventListener("click", () => openCustomProviderForm(provider));

  const deleteButton = document.createElement("button");
  deleteButton.type = "button";
  deleteButton.className = "secondary-button danger";
  deleteButton.textContent = "Delete";
  deleteButton.addEventListener("click", () =>
    deleteCustomProvider(provider, deleteButton),
  );

  actions.append(testButton, editButton, deleteButton);
  card.append(title, meta, details, keyList, actions);
  return card;
}

function updateCustomProviderCard(providerId, status, label, metaText) {
  const card = document.querySelector(`[data-custom-provider="${providerId}"]`);
  if (!card) return;
  const pill = card.querySelector(".status-pill");
  pill.className = `status-pill ${statusClass(status)}`;
  pill.textContent = label;
  if (metaText) {
    card.querySelector(".provider-meta").textContent = metaText;
  }
}

async function testCustomProvider(provider, button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Testing";
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
      );
      setModelOptions([
        ...state.modelOptions,
        ...result.models.map((model) => `${provider.provider_id}/${model}`),
      ]);
    } else {
      updateCustomProviderCard(
        provider.provider_id,
        "offline",
        result.error_type,
        result.error_type,
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

async function addCustomProviderKey(provider, input, button) {
  const key = input.value.trim();
  if (!key) {
    showMessage("Enter a key first", "warn");
    return;
  }
  button.disabled = true;
  try {
    const result = await api(
      `/admin/api/custom-providers/${provider.provider_id}/keys`,
      { method: "POST", body: JSON.stringify({ api_key: key }) },
    );
    showMessage(`Added key ${result.added} (${result.key_count} configured).`, "ok");
    await loadCustomProviders();
  } catch (error) {
    showMessage(`Could not add key: ${error.message}`, "error");
  } finally {
    button.disabled = false;
  }
}

async function removeCustomProviderKey(provider, index, button) {
  button.disabled = true;
  try {
    const result = await api(
      `/admin/api/custom-providers/${provider.provider_id}/keys/${index}`,
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
    await api(`/admin/api/custom-providers/${provider.provider_id}`, {
      method: "DELETE",
    });
    showMessage(`Deleted ${provider.display_name}.`, "ok");
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
  try {
    if (editingId) {
      await api(`/admin/api/custom-providers/${editingId}`, {
        method: "PATCH",
        body: JSON.stringify({
          display_name: byId("cpDisplayName").value,
          base_url: byId("cpBaseUrl").value,
          credential_rotation: byId("cpRotation").value,
          proxy: byId("cpProxy").value,
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
        }),
      });
      if (result.test_error) {
        showMessage(
          `Added ${result.display_name}, but the live test failed: ${result.test_error}`,
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
    title.textContent = info.pending_upgrade.ok
      ? `Updated and restarted on v${info.current}`
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

async function waitForUpdatedServer(expectedVersion) {
  // The reconnect window comes from the server (the install + graceful-drain +
  // startup budget), not a hard-coded two minutes: a slow upgrade must not be
  // abandoned mid-handoff. Fall back to 120s if the status lacks the field.
  const reconnectSeconds =
    (state.versionInfo && state.versionInfo.dashboard_reconnect_timeout_seconds) ||
    120;
  const deadline = Date.now() + reconnectSeconds * 1000;
  let sawDisconnect = false;
  while (Date.now() < deadline) {
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
    const result = await api("/admin/api/version/upgrade", {
      method: "POST",
      body: "{}",
    });
    if (logEl && Array.isArray(result.log) && result.log.length) {
      logEl.textContent = result.log.join("\n");
      logEl.hidden = false;
    }
    if (result.ok) {
      [button, updateButton].forEach((candidate) => {
        if (candidate) candidate.textContent = "Restarting — reconnecting...";
      });
      showMessage(result.message || "Update installed; restarting...", "ok");
      state.versionInfo = await waitForUpdatedServer(result.installed_version);
      renderVersionIndicator();
      renderVersionBanners();
      renderVersionPanel();
      showMessage(
        state.versionInfo.current
          ? `Updated and restarted on v${state.versionInfo.current}`
          : "Updated and restarted",
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
  auto: "The default. Uses an app window if a Chromium-family browser is available, otherwise a browser tab.",
  "app-mode": "A Chrome/Edge/Brave window with no tabs or URL bar, using its own profile.",
  pywebview:
    "An embedded webview. Not installed by default, and OAuth login, downloads and copy buttons may not work in it.",
  browser: "A normal tab in your default browser.",
};

const WINDOW_PROVIDER_LABELS = {
  "app-mode": "app-mode",
  pywebview: "embedded webview",
  browser: "browser tab",
};

function renderDesktopState() {
  const trayEnabled = byId("desktopTrayEnabled");
  const serverMode = byId("desktopServerMode");
  const hint = byId("desktopServerModeHint");
  if (trayEnabled) {
    trayEnabled.checked = Boolean(state.desktop?.tray_enabled);
    trayEnabled.disabled = state.desktopBusy;
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

  const current = Boolean(state.desktop?.start_at_login);
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

    input.addEventListener("change", () => {
      updateDesktop("start_at_login", input.checked, input);
    });
    container.append(label);
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
      showMessage(
        `Start at Login ${value ? "enabled" : "disabled"} for the next launch`,
        "ok",
      );
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

function renderRtkState() {
  const claude = byId("rtkClaude");
  const codex = byId("rtkCodex");
  const pi = byId("rtkPi");
  const statusLine = byId("rtkStatusLine");
  if (!claude || !codex || !pi || !statusLine) return;
  claude.checked = Boolean(state.rtk?.claude);
  codex.checked = Boolean(state.rtk?.codex);
  pi.checked = Boolean(state.rtk?.pi);
  claude.disabled = state.rtkBusy;
  codex.disabled = state.rtkBusy;
  pi.disabled = state.rtkBusy;

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
    const label = field === "claude" ? "Claude Code" : field === "codex" ? "Codex" : "Pi";
    showMessage(`RTK ${value ? "enabled" : "disabled"} for ${label}`, "ok");
  } catch (error) {
    toggle.checked = !value;
    showMessage(`Could not update RTK: ${error.message}`, "error");
  } finally {
    state.rtkBusy = false;
    renderRtkState();
  }
}

byId("rtkClaude").addEventListener("change", (event) => {
  updateRtk("claude", event.currentTarget.checked, event.currentTarget);
});
byId("rtkCodex").addEventListener("change", (event) => {
  updateRtk("codex", event.currentTarget.checked, event.currentTarget);
});
byId("rtkPi").addEventListener("change", (event) => {
  updateRtk("pi", event.currentTarget.checked, event.currentTarget);
});

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
};

function reqWindowSeconds() {
  return Number(byId("reqFilterWindow").value) || 0;
}

function reqFilters() {
  const params = new URLSearchParams();
  const provider = byId("reqFilterProvider").value.trim();
  const model = byId("reqFilterModel").value.trim();
  const key = byId("reqFilterKey").value.trim();
  const status = byId("reqFilterStatus").value;
  const search = byId("reqFilterSearch").value.trim();
  const endpoint = byId("reqFilterEndpoint").value.trim();
  const windowSeconds = byId("reqFilterWindow").value;
  if (provider) params.set("provider", provider);
  if (model) params.set("model", model);
  if (key) params.set("key", key);
  if (status) params.set("status", status);
  if (search) params.set("q", search);
  if (endpoint) params.set("endpoint", endpoint);
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
  try {
    [stats, list, lifetime] = await Promise.all([
      api(`/admin/api/requests/stats?${params}`),
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
    byId("reqKeyBreakdown").innerHTML = "";
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
    byId("reqBodiesIndicator").textContent = "Request log disabled (REQUEST_LOG_ENABLED=false)";
    renderReqPager();
    byId("reqLastUpdated").textContent = "Logging disabled";
    return;
  }
  byId("reqBodiesIndicator").textContent = stats.capture_bodies
    ? "Bodies: captured"
    : "Bodies: hashes only (REQUEST_LOG_CAPTURE_BODIES=false)";
  renderRequestStatsCards(stats);
  renderRequestRetentionNote(stats);
  renderRequestLifetime(lifetime);
  renderRequestCoverage(stats);
  renderReqSeriesChart(stats.series || []);
  renderReqModelChart(stats.by_model || []);
  populateRequestFilterOptions(stats);
  renderRequestProviderBreakdown(stats.by_provider || []);
  renderRequestKeyBreakdown(stats.by_key || []);
  renderRequestTopErrors(stats.top_errors || []);
  renderRequestFallbackRoutes(stats.fallback_routes || []);
  renderRequestDivertedRoutes(stats.diverted_routes || []);
  renderReqBreakdownTruncatedNote(stats);
  reqState.total = list.total || 0;
  renderRequestsTable(list.rows || []);
  renderReqPager();
  byId("reqLastUpdated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
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
    ["Cancelled", stats.cancelled],
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
    ["Avg TTFT", stats.avg_ttft_ms != null ? `${stats.avg_ttft_ms} ms` : "—"],
  ];
  renderStatCards(byId("reqStatsCards"), cards);
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
  span.textContent =
    lifetime.first_day && lifetime.last_day
      ? `${lifetime.first_day} to ${lifetime.last_day}`
      : "nothing recorded yet";
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

/* The aggregates below are SQL COALESCE(...,0) sums, so their zeros are
   measured zeros and Number(x || 0) is honest here. avg_duration_ms is the
   one genuinely NULL-able column and uses the dash convention. */
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
          row.key || "unknown",
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

function renderRequestsTable(rows) {
  const body = byId("reqTableBody");
  body.innerHTML = "";
  if (rows.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 11;
    td.className = "analytics-empty";
    td.textContent = "No requests match the current filters.";
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    tr.className = `req-row req-status-${row.status}`;
    const cells = [
      formatRequestTime(row),
      row.endpoint || "",
      providerDisplayLabel(row.provider, row.optimization),
      row.key_label || "",
    ];
    cells.forEach((text) => {
      const td = document.createElement("td");
      td.textContent = text;
      tr.appendChild(td);
    });
    tr.appendChild(buildModelCell(row));
    const statusCell = document.createElement("td");
    statusCell.textContent = row.status;
    tr.appendChild(statusCell);
    tr.appendChild(buildTurnShapeCell(row));
    [
      `${row.tokens_in ?? "—"}/${row.tokens_out ?? "—"}`,
      row.ttft_ms != null ? `${Math.round(row.ttft_ms)} ms` : "—",
      row.duration_ms != null ? `${Math.round(row.duration_ms)} ms` : "—",
    ].forEach((text) => {
      const td = document.createElement("td");
      td.textContent = text;
      tr.appendChild(td);
    });
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
  const start = reqState.total === 0 ? 0 : reqState.offset + 1;
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
        search: byId("reqFilterSearch")?.value?.trim() || undefined,
        status: byId("reqFilterStatus")?.value || undefined,
        endpoint: byId("reqFilterEndpoint")?.value?.trim() || undefined,
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

async function openRequestDetail(requestId) {
  reqState.detailReturnFocus = document.activeElement;
  const row = await api(`/admin/api/requests/${requestId}`);
  byId("reqDetailTitle").textContent = `Request ${row.id}`;
  const meta = byId("reqDetailMeta");
  meta.innerHTML = "";
  const fields = [
    ["Time", row.ts_iso],
    ["Endpoint", row.endpoint],
    ["Protocol", row.protocol],
    ["Requested model", row.requested_model],
    ["Provider", providerDisplayLabel(row.provider, row.optimization) || null],
    ["Resolved model", row.resolved_model],
    ["Route attempt", formatRouteAttempt(row)],
    ["Vision model", formatVisionModel(row)],
    ["Status", row.status],
    ["Error", row.error_kind ? `${row.error_kind}: ${row.error_message || ""}` : ""],
    ["Key", row.key_label],
    ["Total input", formatAnalyticsNumber(totalInputTokens(row))],
    ["Input (uncached)", formatOptionalNumber(row.tokens_in)],
    ["Cached input", formatOptionalNumber(row.cache_read_tokens)],
    ["Cache writes", formatOptionalNumber(row.cache_write_tokens)],
    ["Cache hit", formatRowCacheHit(row)],
    ["Tokens out", formatOptionalNumber(row.tokens_out)],
    ["Output rate", formatOutputRate(row)],
    ["TTFT", row.ttft_ms != null ? `${Math.round(row.ttft_ms)} ms` : "—"],
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
  renderRequestRouteTrace(row);
  renderRequestImages(row);
  renderRequestChain(row);
  renderWireRequest(row);
  renderTurnTranscript(row);
  byId("reqDetailModal").hidden = false;
  byId("reqDetailClose").focus();
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
  const attempts = row.route_attempts || [];
  return (
    attempts.find((attempt) => attempt.outcome === "succeeded") ||
    attempts[attempts.length - 1]
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

/** Output tokens per second, excluding the wait before the first one. */
function formatOutputRate(row) {
  const tokens = Number(row.tokens_out || 0);
  const duration = Number(row.duration_ms || 0);
  const ttft = Number(row.ttft_ms || 0);
  const generating = duration - ttft;
  if (!tokens || generating <= 0) return "—";
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
function renderRequestChain(row) {
  const container = byId("reqDetailChain");
  if (!container) return;
  container.innerHTML = "";
  const attempts = row.route_attempts || [];
  // One attempt that succeeded is just "the model answered" -- the route
  // summary above already says that, and repeating it as a timeline implies a
  // chain did something when it did not.
  if (attempts.length < 2) {
    container.hidden = true;
    return;
  }
  container.hidden = false;

  const heading = document.createElement("h4");
  heading.className = "req-chain-title";
  heading.textContent = "Route attempts";
  container.appendChild(heading);

  const list = document.createElement("ol");
  list.className = "req-chain-list";
  attempts.forEach((attempt) => {
    const item = document.createElement("li");
    item.className = `req-chain-item is-${attempt.outcome || "skipped"}`;

    const head = document.createElement("div");
    head.className = "req-chain-head";

    const badge = document.createElement("span");
    badge.className = "req-chain-outcome";
    badge.textContent = CHAIN_OUTCOME_LABELS[attempt.outcome] || attempt.outcome || "—";
    head.appendChild(badge);

    const model = document.createElement("code");
    model.className = "req-chain-model";
    model.textContent = attempt.model_ref || "—";
    model.title = attempt.model_ref || "";
    head.appendChild(model);

    if (attempt.duration_ms != null) {
      const took = document.createElement("span");
      took.className = "req-chain-duration";
      took.textContent = formatChainDuration(attempt.duration_ms);
      head.appendChild(took);
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
      credential.textContent = attempt.key_label;
    } else {
      credential.textContent = NOT_MEASURED;
      credential.title = "No credential was recorded for this attempt.";
    }
    head.appendChild(credential);
    item.appendChild(head);

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
  const attempts = row.route_attempts || [];
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

/** One line of the numbers people open this panel to check. */
function formatWireFacts(attempt) {
  const wire = (attempt.params && attempt.params.wire) || {};
  const widened = attempt.params && attempt.params.output_widened_from;
  const parts = [];
  if (wire.max_tokens != null) parts.push(`max_tokens ${Number(wire.max_tokens).toLocaleString()}`);
  /* The "from" for the max_tokens above. Only present when the allowance was
     actually raised because the attempt was going to think, so the line reads
     as an explanation of a number that would otherwise look invented. */
  if (widened != null) parts.push(`raised from ${Number(widened).toLocaleString()} for reasoning`);
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
};

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
  const grid = document.createElement("div");
  grid.className = "req-image-grid";
  images.forEach((image, index) => {
    grid.appendChild(buildRequestImage(image, index));
  });
  container.appendChild(grid);
}

function buildRequestImage(image, index) {
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
  return figure;
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
  websearch: new Set(["provider", "status", "results_count", "duration_ms", "cost_usd"]),
};

const EXPORT_DEFAULT_PERIOD = { requests: "86400", websearch: "604800" };
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
}

function openExportModal() {
  exportReturnFocus = document.activeElement;
  // Default scope to the view the user opened from.
  const initial = state.activeView === "web_search" ? "websearch" : "requests";
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
byId("reqApplyFilters").addEventListener("click", () => {
  reqState.offset = 0;
  persistDashboardState();
  loadRequestsView().catch((error) => showMessage(error.message, "error"));
});
byId("reqClearFilters").addEventListener("click", () => {
  // Reset every analytics filter to its default and reload, so the view
  // returns to "show everything" without a manual page refresh.
  byId("reqFilterProvider").value = "";
  byId("reqFilterModel").value = "";
  byId("reqFilterKey").value = "";
  byId("reqFilterSearch").value = "";
  byId("reqFilterStatus").value = "";
  byId("reqFilterEndpoint").value = "";
  byId("reqFilterWindow").value = "";
  byId("reqPageSize").value = "25";
  reqState.limit = 25;
  reqState.offset = 0;
  loadRequestsView().catch((error) => showMessage(error.message, "error"));
  persistDashboardState();
});
byId("reqFilterSearch").addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  reqState.offset = 0;
  loadRequestsView().catch((error) => showMessage(error.message, "error"));
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
  reqState.offset = 0;
  loadRequestsView().catch((error) => showMessage(error.message, "error"));
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
byId("reqExportButton").addEventListener("click", openExportModal);
byId("reqClearButton").addEventListener("click", () => {
  if (
    !window.confirm(
      `Delete the entire request log? The current filters match ${reqState.total} rows; all stored rows will be deleted.`,
    )
  ) {
    return;
  }
  api("/admin/api/requests", { method: "DELETE" })
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
    modelsState.data = await api("/admin/api/model-admin");
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
    return Object.keys(model.override || {}).length > 0;
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

  // Two checkboxes on one row is the design's biggest legibility bet, so they
  // are separated three ways: the selection box sits in its own ruled gutter
  // cell, it carries no visible word, and the visibility control keeps its
  // "Show" label.
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

  const tick = document.createElement("label");
  tick.className = "models-visible-toggle";
  const toggle = document.createElement("input");
  toggle.type = "checkbox";
  toggle.checked = model.visible;
  toggle.addEventListener("change", () => {
    toggleModelVisibility(model.model_ref, toggle.checked).catch((error) => {
      toggle.checked = !toggle.checked;
      showMessage(error.message, "error");
    });
  });
  tick.appendChild(toggle);
  const tickText = document.createElement("span");
  tickText.textContent = model.visible ? "Show" : "Hidden";
  tick.appendChild(tickText);
  tick.title = `Show ${model.model_ref} in /v1/models and the admin pickers`;
  row.appendChild(tick);

  row.appendChild(buildModelNode(model, editable));
  return row;
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
  // Only when a pattern that is not this model's own exact ref is what hides
  // it: a row that springs back with no explanation is the complaint the
  // single-toggle path never answered.
  if (model.blocked_by) {
    const note = document.createElement("span");
    note.className = "models-blocked-note";
    note.textContent = `hidden by your pattern ${model.blocked_by}`;
    second.appendChild(note);
  }
  if (second.childElementCount) summary.appendChild(second);
  return summary;
}

function fillModelBody(body, model, editable) {
  body.textContent = "";
  body.appendChild(
    buildOverrideEditor("model", model.model_ref, model.override, editable),
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
    buildCapabilityPanel(model.capabilities, (data && data.source_labels) || {}),
  );
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
function buildOverrideEditor(scope, key, row, editable) {
  const form = document.createElement("div");
  form.className = "models-override-editor";
  const inputs = new Map();

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
        updates[name] = parseOverrideValue(name, control.box.value);
      }
    });
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
function buildCapabilityPanel(capabilities, labels) {
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
    rows.push(["output limit", capabilities.max_output_tokens]);
    rows.push(["context length", capabilities.context_length]);
    rows.push(["reads images", capabilities.supports_vision]);
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
    table.appendChild(buildCapabilityRow(entry[0], entry[1], labels));
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
  const dialect = buildDialectPanel(capabilities && capabilities.reasoning_dialect);
  if (dialect) wrap.appendChild(dialect);
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

function buildCapabilityRow(label, field, labels) {
  const tr = document.createElement("tr");
  const name = document.createElement("th");
  name.scope = "row";
  name.textContent = label;
  tr.appendChild(name);

  const value = document.createElement("td");
  value.textContent = formatCapabilityValue(field.value);
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
  if (field.note) {
    const note = document.createElement("span");
    note.className = "models-approx-note";
    note.textContent = field.note;
    source.appendChild(note);
  }
  tr.appendChild(source);
  return tr;
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

/* Repaint one model row from the current payload without touching the tree
   around it: the summary chips, the tick, and the body if it is open. */
function refreshModelRow(modelRef) {
  const model = findModelInData(modelRef);
  if (!model) return;
  const editable =
    (modelsState.data && modelsState.data.overrides.editable_parameters) || [];
  document.querySelectorAll("details.models-model").forEach((node) => {
    const ref = node.querySelector(".models-ref");
    if (!ref || ref.textContent !== modelRef) return;
    const summary = node.querySelector("summary");
    if (summary) node.replaceChild(buildModelSummary(model), summary);
    const row = node.parentElement;
    const tick =
      row && row.querySelector(".models-visible-toggle input[type=checkbox]");
    if (tick) tick.checked = model.visible;
    const body = node.querySelector(".models-model-body");
    if (!body || body.dataset.filled !== "1") return;
    const readouts = body.querySelector(".models-readouts");
    if (readouts) fillModelReadouts(readouts, model);
    else fillModelBody(body, model, editable);
  });
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
  (provider.models || []).forEach((model) => refreshModelRow(model.model_ref));
}

/* One pass over the rendered rows for many refs. refreshModelRow() runs its
   own document-wide query, so calling it three hundred times is quadratic in
   the number of rows on screen -- which is exactly the size a bulk action
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
    const tick =
      row && row.querySelector(".models-visible-toggle input[type=checkbox]");
    if (tick) tick.checked = model.visible;
    const word = row && row.querySelector(".models-visible-toggle span");
    if (word) word.textContent = model.visible ? "Show" : "Hidden";
    const body = node.querySelector(".models-model-body");
    if (!body || body.dataset.filled !== "1") return;
    const readouts = body.querySelector(".models-readouts");
    if (readouts) fillModelReadouts(readouts, model);
    else fillModelBody(body, model, editable);
  });
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
  [
    ["show", "Show"],
    ["hide", "Hide"],
    ["invert", "Invert"],
  ].forEach(([action, label]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary-button models-bulk-button";
    button.textContent = label;
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
        : `${refs.length} of them did not change.`;
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
        model.blocked_by = row.honored === false ? row.blocked_by || "" : "";
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

async function toggleModelVisibility(modelRef, visible) {
  const result = await api("/admin/api/model-admin/visibility/toggle", {
    method: "POST",
    body: JSON.stringify({ model_ref: modelRef, visible }),
  });
  applyModelsData(await api("/admin/api/model-admin"));
  refreshModelRow(modelRef);
  if (result.honored === false) {
    // An exact pattern cannot beat a broader glob the user wrote. Saying so
    // beats a checkbox that springs back with no explanation.
    showMessage(
      `${modelRef} is still ${result.visible ? "visible" : "hidden"}: a pattern in your allow/deny lists overrules this tick.`,
      "error",
    );
  } else {
    showMessage(
      `${modelRef} is now ${visible ? "shown in" : "hidden from"} /v1/models and the admin pickers. Routing is unaffected either way.`,
      "ok",
    );
  }
}

async function saveModelOverrides(scope, key, updates) {
  applyModelsData(
    await api("/admin/api/model-admin/overrides", {
      method: "POST",
      body: JSON.stringify({ scope, key, updates }),
    }),
  );
  if (scope === "provider") refreshProviderRow(key);
  else refreshModelRow(key);
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
