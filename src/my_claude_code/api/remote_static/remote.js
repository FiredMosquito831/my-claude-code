(() => {
  "use strict";

  const state = {
    authenticated: false,
    csrfToken: "",
    sessions: [],
    harnesses: [],
    activeSessionId: "",
    activeSession: null,
    activeTab: "chat",
    lastSeq: 0,
    wsEvents: null,
    wsPty: null,
    term: null,
    fitAddon: null,
    pendingApprovals: [],
    streamingAssistantEl: null,
    streamingThinkingEl: null,
    telemetry: null,
    telemetryTimer: null,
    catalog: null,
    pushSubscribed: false,
    swRegistration: null,
  };

  function el(id) {
    return document.getElementById(id);
  }

  function readCookie(name) {
    const prefix = `${name}=`;
    for (const part of document.cookie.split(";")) {
      const trimmed = part.trim();
      if (trimmed.startsWith(prefix)) {
        return decodeURIComponent(trimmed.slice(prefix.length));
      }
    }
    return "";
  }

  function currentCsrf() {
    return state.csrfToken || readCookie("mcc_rc_csrf");
  }

  async function apiJson(path, options = {}) {
    const method = (options.method || "GET").toUpperCase();
    const headers = { ...(options.headers || {}) };
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
    }
    if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
      const csrf = currentCsrf();
      if (csrf) headers["X-MCC-RC-CSRF"] = csrf;
    }
    const resp = await fetch(path, {
      method,
      headers,
      credentials: "same-origin",
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    });
    const text = await resp.text();
    let data = {};
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (_err) {
        data = { detail: text };
      }
    }
    if (!resp.ok) {
      if (resp.status === 401) {
        setAuthenticated(false);
      }
      throw new Error(data.detail || `Request failed (${resp.status})`);
    }
    return data;
  }

  function setAuthenticated(flag) {
    state.authenticated = Boolean(flag);
    el("pairView").hidden = state.authenticated;
    el("workspaceView").hidden = !state.authenticated;
    el("btnNewSessionOpen").hidden = !state.authenticated;
    el("btnLogout").hidden = !state.authenticated;
    el("btnPushToggle").hidden = !state.authenticated;
    if (!state.authenticated) {
      el("btnTestPush").hidden = true;
      stopTelemetryPolling();
      closeWebSockets();
    }
    el("connDot").classList.toggle("is-online", state.authenticated);
  }

  function closeWebSockets() {
    if (state.wsEvents) {
      state.wsEvents.close();
      state.wsEvents = null;
    }
    if (state.wsPty) {
      state.wsPty.close();
      state.wsPty = null;
    }
  }

  function appendMarkdownBlocks(container, text) {
    const raw = String(text || "");
    const parts = raw.split("```");
    parts.forEach((segment, idx) => {
      if (idx % 2 === 1) {
        const pre = document.createElement("pre");
        pre.className = "rc-code";
        const firstNewline = segment.indexOf("\n");
        pre.textContent =
          firstNewline >= 0 ? segment.slice(firstNewline + 1) : segment;
        container.appendChild(pre);
      } else if (segment) {
        const span = document.createElement("div");
        span.textContent = segment;
        container.appendChild(span);
      }
    });
  }

  function addTimelineEntry(className, builder) {
    const node = document.createElement("div");
    node.className = `rc-msg ${className || ""}`.trim();
    builder(node);
    const tl = el("timeline");
    tl.appendChild(node);
    tl.scrollTop = tl.scrollHeight;
    return node;
  }

  function isStructuredTier(tier) {
    return Boolean(tier && tier !== "tier_3_pty");
  }

  function tierBadgeLabel(tier) {
    switch (tier) {
      case "tier_1b_claude":
        return "Tier 1B · Claude";
      case "tier_1a_opencode":
        return "Tier 1A · OpenCode";
      case "tier_1a_codex":
        return "Tier 1A · Codex";
      case "tier_1a_openclaw":
        return "Tier 1A · OpenClaw";
      case "tier_2_acp":
        return "Tier 2 · ACP";
      case "tier_2_pi_rpc":
        return "Tier 2 · RPC";
      case "tier_2_commandcode":
        return "Tier 2 · JSON";
      default:
        return "Tier 3 · PTY";
    }
  }

  function harnessTierShortLabel(tier) {
    switch (tier) {
      case "tier_1a_opencode":
      case "tier_1a_codex":
      case "tier_1a_openclaw":
        return "Tier 1A";
      case "tier_1b_claude":
        return "Tier 1B";
      case "tier_2_acp":
        return "Tier 2 ACP";
      case "tier_2_pi_rpc":
        return "Tier 2 RPC";
      case "tier_2_commandcode":
        return "Tier 2 JSON";
      default:
        return "Tier 3 PTY";
    }
  }

  function renderApprovalBanner() {
    const banner = el("approvalBanner");
    banner.replaceChildren();
    if (!state.pendingApprovals.length) {
      banner.hidden = true;
      return;
    }
    banner.hidden = false;
    const item = state.pendingApprovals[0];
    const title = document.createElement("strong");
    title.textContent = item.isQuestion
      ? "Agent asks a question"
      : `Approve tool: ${item.tool_name || "tool"}`;
    banner.appendChild(title);

    if (item.isQuestion) {
      const form = document.createElement("div");
      form.className = "rc-stack";
      const questions = Array.isArray(item.questions) ? item.questions : [];
      const answerInputs = [];
      questions.forEach((q, idx) => {
        const label = document.createElement("label");
        label.className = "rc-label";
        label.textContent = q.question || q.header || `Question ${idx + 1}`;
        const input = document.createElement("input");
        input.className = "rc-input";
        input.type = "text";
        input.placeholder = "Type answer...";
        const opts = Array.isArray(q.options) ? q.options : [];
        if (opts.length) {
          input.value = String(opts[0].label || "");
        }
        label.appendChild(input);
        form.appendChild(label);
        answerInputs.push({ key: q.question || q.header || String(idx), input });
      });
      const submitBtn = document.createElement("button");
      submitBtn.type = "button";
      submitBtn.className = "rc-btn rc-btn-primary";
      submitBtn.textContent = "Submit Answers";
      submitBtn.addEventListener("click", async () => {
        const answers = {};
        answerInputs.forEach(({ key, input }) => {
          answers[key] = input.value;
        });
        await submitApproval(item.approval_id, "allow_once", { answers });
      });
      form.appendChild(submitBtn);
      banner.appendChild(form);
      return;
    }

    const pre = document.createElement("pre");
    pre.className = "rc-code";
    pre.textContent = JSON.stringify(item.input || {}, null, 2);
    banner.appendChild(pre);

    const row = document.createElement("div");
    row.className = "rc-approval-actions";
    [
      ["allow_once", "Allow Once", "rc-btn rc-btn-primary"],
      ["allow_always", "Allow Always", "rc-btn rc-btn-ghost"],
      ["deny", "Deny", "rc-btn rc-btn-danger"],
    ].forEach(([decision, label, cls]) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = cls;
      btn.textContent = label;
      btn.addEventListener("click", () => submitApproval(item.approval_id, decision));
      row.appendChild(btn);
    });
    banner.appendChild(row);
  }

  async function submitApproval(approvalId, decision, extra = {}) {
    if (!state.activeSessionId) return;
    await apiJson(`/rc/api/sessions/${state.activeSessionId}/approve`, {
      method: "POST",
      body: { approval_id: approvalId, decision, ...extra },
    });
    state.pendingApprovals = state.pendingApprovals.filter(
      (x) => x.approval_id !== approvalId,
    );
    renderApprovalBanner();
  }

  function handleSessionEvent(ev) {
    if (!ev || typeof ev !== "object") return;
    if (typeof ev.seq === "number" && ev.seq > state.lastSeq) {
      state.lastSeq = ev.seq;
    }
    const kind = String(ev.kind || "");
    const payload = ev.payload || {};

    if (kind === "message.user") {
      state.streamingAssistantEl = null;
      state.streamingThinkingEl = null;
      addTimelineEntry("rc-msg-user", (node) => {
        node.textContent = String(payload.text || "");
      });
    } else if (kind === "message.delta") {
      if (payload.channel === "thinking") {
        if (!state.streamingThinkingEl) {
          const box = addTimelineEntry("", (node) => {
            const details = document.createElement("details");
            const summary = document.createElement("summary");
            summary.textContent = "Thinking";
            const body = document.createElement("div");
            details.appendChild(summary);
            details.appendChild(body);
            node.appendChild(details);
            state.streamingThinkingEl = body;
          });
          void box;
        }
        state.streamingThinkingEl.textContent += String(payload.delta || "");
      } else if (payload.channel === "text") {
        if (!state.streamingAssistantEl) {
          state.streamingAssistantEl = addTimelineEntry("", () => {});
        }
        state.streamingAssistantEl.textContent += String(payload.delta || "");
      }
    } else if (kind === "message.assistant") {
      if (payload.channel === "text") {
        if (state.streamingAssistantEl) {
          state.streamingAssistantEl.replaceChildren();
          appendMarkdownBlocks(state.streamingAssistantEl, payload.text || "");
          state.streamingAssistantEl = null;
        } else {
          addTimelineEntry("", (node) =>
            appendMarkdownBlocks(node, payload.text || ""),
          );
        }
      }
    } else if (kind === "tool.started") {
      addTimelineEntry("rc-msg-tool", (node) => {
        node.textContent = `Tool ▶ ${payload.tool_name || ""} ${JSON.stringify(
          payload.input || {},
        )}`;
      });
    } else if (kind === "tool.completed") {
      addTimelineEntry("rc-msg-tool", (node) => {
        node.textContent = `${payload.is_error ? "Error" : "Result"}: ${
          typeof payload.content === "string"
            ? payload.content
            : JSON.stringify(payload.content || "")
        }`;
      });
    } else if (kind === "approval.requested") {
      state.pendingApprovals.push({
        approval_id: payload.approval_id,
        tool_name: payload.tool_name,
        input: payload.input,
        isQuestion: false,
      });
      renderApprovalBanner();
    } else if (kind === "question.requested") {
      state.pendingApprovals.push({
        approval_id: payload.approval_id,
        questions: payload.questions || [],
        isQuestion: true,
      });
      renderApprovalBanner();
    } else if (kind === "approval.resolved") {
      state.pendingApprovals = state.pendingApprovals.filter(
        (x) => x.approval_id !== payload.approval_id,
      );
      renderApprovalBanner();
    } else if (kind === "approval.auto_approved") {
      addTimelineEntry("rc-msg-tool", (node) => {
        node.textContent = `Auto-approved (${payload.mode || "policy"}): ${payload.tool_name || "tool"}`;
      });
    } else if (kind === "tool.output") {
      addTimelineEntry("rc-msg-tool", (node) => {
        node.textContent = String(payload.delta || "");
      });
    } else if (kind === "turn.completed") {
      state.streamingAssistantEl = null;
      state.streamingThinkingEl = null;
      refreshActiveMeta().catch(() => {});
      loadTelemetry().catch(() => {});
    } else if (kind === "session.telemetry") {
      if (payload && (!state.activeSessionId || payload.session_id === state.activeSessionId)) {
        state.telemetry = payload;
        renderTelemetryStrip();
        renderRecentRequests();
      }
    } else if (kind === "session.controls_updated") {
      refreshActiveMeta().catch(() => {});
    } else if (kind === "session.mode_changed" || kind === "session.state" || kind === "session.exited") {
      if (payload.recommend_pty && state.activeSession && isStructuredTier(state.activeSession.driver_tier)) {
        addTimelineEntry("rc-alert rc-alert-warn", (node) => {
          const msg = document.createElement("span");
          msg.textContent = String(
            payload.message || "Switch to Interactive TUI (PTY) to approve tools."
          );
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "rc-btn rc-btn-primary";
          btn.textContent = "Open Interactive TUI (PTY)";
          btn.addEventListener("click", () => {
            switchToInteractivePty().catch(() => {});
          });
          node.appendChild(msg);
          node.appendChild(btn);
        });
      }
      refreshActiveMeta().catch(() => {});
    } else if (kind === "session.error") {
      addTimelineEntry("rc-alert rc-alert-error", (node) => {
        node.textContent = String(payload.message || "Session error");
      });
    }
  }

  async function refreshActiveMeta() {
    if (!state.activeSessionId) return;
    const data = await apiJson(
      `/rc/api/sessions/${state.activeSessionId}?since_seq=${state.lastSeq}`,
    );
    state.activeSession = data.session;
    renderActiveSessionHeader();
    renderMetaTab();
  }

  function renderSessionList() {
    const list = el("sessionList");
    list.replaceChildren();
    if (!state.sessions.length) {
      const empty = document.createElement("p");
      empty.className = "rc-muted";
      empty.textContent = "No sessions yet.";
      list.appendChild(empty);
      return;
    }
    state.sessions.forEach((sess) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `rc-session-item ${
        sess.id === state.activeSessionId ? "is-selected" : ""
      }`.trim();
      const title = document.createElement("div");
      title.className = "rc-session-item-title";
      title.textContent = sess.title || sess.id;
      const sub = document.createElement("div");
      sub.className = "rc-session-item-sub";
      sub.textContent = `${sess.harness} · ${sess.status} · ${sess.permission_mode}`;
      btn.appendChild(title);
      btn.appendChild(sub);
      btn.addEventListener("click", () => {
        el("sessionSidebar").classList.remove("is-open");
        selectSession(sess.id);
      });
      list.appendChild(btn);
    });
  }

  function renderActiveSessionHeader() {
    const sess = state.activeSession;
    if (!sess) return;
    el("emptyState").hidden = true;
    el("sessionDetail").hidden = false;
    el("activeTitle").textContent = sess.title || sess.id;
    el("activeStatusBadge").textContent = sess.status;
    el("activeTierBadge").textContent = tierBadgeLabel(sess.driver_tier);
    el("activeModeSelect").value = sess.permission_mode || "ask";
    const branchBadge = el("activeBranchBadge");
    if (sess.branch_name) {
      branchBadge.hidden = false;
      branchBadge.textContent = sess.branch_name;
    } else {
      branchBadge.hidden = true;
    }
    const attachBadge = el("activeAttachBadge");
    if (sess.attach_hint || sess.server_url) {
      attachBadge.hidden = false;
      attachBadge.textContent = sess.attach_hint || sess.server_url;
    } else {
      attachBadge.hidden = true;
    }
    el("btnSwitchTerminal").hidden = !isStructuredTier(sess.driver_tier);
    const tabSwitchBar = el("terminalSwitchBar");
    if (tabSwitchBar) {
      tabSwitchBar.hidden = !isStructuredTier(sess.driver_tier);
    }
    el("btnForkSession").hidden = !sess.external_session_id;
  }

  function renderMetaTab() {
    const dl = el("sessionMetaList");
    dl.replaceChildren();
    const sess = state.activeSession;
    if (!sess) return;
    [
      ["Session ID", sess.id],
      ["Harness", sess.harness],
      ["Driver", sess.driver_tier],
      ["Status", sess.status],
      ["CWD", sess.cwd],
      ["Worktree", sess.worktree_path || "—"],
      ["Branch", sess.branch_name || "—"],
      ["Model", sess.model || "—"],
      ["Reasoning Effort", sess.reasoning_effort || "—"],
      ["External ID", sess.external_session_id || "—"],
      ["Server URL", sess.server_url || "—"],
      ["Attach Command", sess.attach_hint || "—"],
      ["PID", sess.pid != null ? String(sess.pid) : "—"],
    ].forEach(([label, val]) => {
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = String(val);
      dl.appendChild(dt);
      dl.appendChild(dd);
    });
  }

  function wsUrl(path) {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${window.location.host}${path}`;
  }

  function connectEventSocket(sessionId) {
    if (state.wsEvents) state.wsEvents.close();
    const sock = new WebSocket(
      wsUrl(`/rc/ws/sessions/${sessionId}?since_seq=${state.lastSeq}`),
    );
    sock.addEventListener("message", (msg) => {
      try {
        const parsed = JSON.parse(msg.data);
        if (parsed.kind === "pong") return;
        handleSessionEvent(parsed);
      } catch (_err) {
        // Ignore malformed frame
      }
    });
    sock.addEventListener("close", (ev) => {
      if (state.authenticated && state.activeSessionId === sessionId && ev.code !== 1008) {
        setTimeout(() => {
          if (state.authenticated && state.activeSessionId === sessionId) {
            connectEventSocket(sessionId);
          }
        }, 1500);
      }
    });
    state.wsEvents = sock;
  }

  function ensureTerminalMounted(sessionId) {
    if (!window.Terminal || !window.FitAddon) return;
    if (!state.term) {
      const container = el("ptyContainer");
      const term = new window.Terminal({
        cursorBlink: true,
        fontSize: 13,
        theme: { background: "#050810" },
      });
      const fit = new window.FitAddon.FitAddon();
      term.loadAddon(fit);
      term.open(container);
      fit.fit();
      term.onData((data) => {
        if (state.wsPty && state.wsPty.readyState === WebSocket.OPEN) {
          state.wsPty.send(JSON.stringify({ type: "input", data }));
        }
      });
      window.addEventListener("resize", () => {
        if (state.fitAddon && state.activeTab === "terminal") {
          state.fitAddon.fit();
          sendPtyResize();
        }
      });
      state.term = term;
      state.fitAddon = fit;
    } else {
      state.term.reset();
      state.fitAddon.fit();
    }
    if (state.wsPty) state.wsPty.close();
    const ptySock = new WebSocket(wsUrl(`/rc/ws/sessions/${sessionId}/pty`));
    ptySock.binaryType = "arraybuffer";
    ptySock.addEventListener("open", () => {
      if (state.fitAddon) state.fitAddon.fit();
      sendPtyResize();
    });
    ptySock.addEventListener("message", (msg) => {
      if (!state.term) return;
      if (msg.data instanceof ArrayBuffer) {
        state.term.write(new Uint8Array(msg.data));
      } else {
        state.term.write(String(msg.data || ""));
      }
    });
    state.wsPty = ptySock;
  }

  function sendPtyResize() {
    if (!state.term || !state.wsPty || state.wsPty.readyState !== WebSocket.OPEN) {
      return;
    }
    state.wsPty.send(
      JSON.stringify({
        type: "resize",
        cols: state.term.cols || 120,
        rows: state.term.rows || 36,
      }),
    );
  }

  async function selectSession(sessionId) {
    state.activeSessionId = sessionId;
    state.lastSeq = 0;
    state.streamingAssistantEl = null;
    state.streamingThinkingEl = null;
    state.pendingApprovals = [];
    el("timeline").replaceChildren();
    renderSessionList();

    const data = await apiJson(`/rc/api/sessions/${sessionId}?since_seq=0`);
    state.activeSession = data.session;
    state.telemetry = null;
    renderActiveSessionHeader();
    renderMetaTab();
    renderTelemetryStrip();
    populateQuickControls();
    (data.events || []).forEach(handleSessionEvent);
    connectEventSocket(sessionId);
    loadTelemetry().catch(() => {});
    startTelemetryPolling();

    if (!isStructuredTier(data.session.driver_tier)) {
      setActiveTab("terminal");
    } else if (state.activeTab === "terminal") {
      setActiveTab("chat");
    }
  }

  function setActiveTab(name) {
    state.activeTab = name;
    document.querySelectorAll(".rc-tab").forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.tab === name);
    });
    el("tabChat").hidden = name !== "chat";
    el("tabTerminal").hidden = name !== "terminal";
    el("tabChanges").hidden = name !== "changes";
    el("tabInfo").hidden = name !== "info";
    if (name === "terminal" && state.activeSessionId) {
      if (state.activeSession && isStructuredTier(state.activeSession.driver_tier)) {
        apiJson(`/rc/api/sessions/${state.activeSessionId}/terminal`, {
          method: "POST",
          body: { cols: 120, rows: 36 },
        })
          .then(() => ensureTerminalMounted(state.activeSessionId))
          .catch(() => {});
      } else {
        ensureTerminalMounted(state.activeSessionId);
      }
    } else if (name === "changes" && state.activeSessionId) {
      loadDiff().catch(() => {});
    }
  }

  async function loadDiff() {
    if (!state.activeSessionId) return;
    const data = await apiJson(`/rc/api/sessions/${state.activeSessionId}/diff`);
    el("diffSummary").textContent = data.is_git
      ? `${data.branch || "HEAD"}\n${data.diffstat || "Clean"}`
      : data.message || "Not a git workspace";
    el("diffStatusPre").textContent = data.status || "";
    el("diffPatchPre").textContent = data.patch || "";
  }

  async function loadInitialWorkspace() {
    const [sessResp, harnessResp, dirResp, catalogResp] = await Promise.all([
      apiJson("/rc/api/sessions"),
      apiJson("/rc/api/harnesses"),
      apiJson("/rc/api/fs/dirs").catch(() => ({ entries: [] })),
      apiJson("/rc/api/catalog").catch(() => null),
    ]);
    state.sessions = sessResp.sessions || [];
    state.harnesses = harnessResp.harnesses || [];
    if (catalogResp) {
      state.catalog = catalogResp;
      populateQuickControls();
    }
    renderSessionList();
    populateNewSessionDialog(dirResp);
    const params = new URLSearchParams(window.location.search);
    const requestedSession = params.get("session");
    if (requestedSession && state.sessions.some((s) => s.id === requestedSession)) {
      if (state.activeSessionId !== requestedSession) {
        await selectSession(requestedSession);
      }
    } else if (state.sessions.length && !state.activeSessionId) {
      await selectSession(state.sessions[0].id);
    }
    refreshPushUi().catch(() => {});
  }

  function populateNewSessionDialog(dirResp) {
    const select = el("newHarnessSelect");
    select.replaceChildren();
    state.harnesses.forEach((h) => {
      const opt = document.createElement("option");
      opt.value = h.id;
      const tierLabel = harnessTierShortLabel(h.driver_tier);
      const installed = h.installed ? "" : " (not found)";
      opt.textContent = `${h.label} [${tierLabel}]${installed}`;
      select.appendChild(opt);
    });
    const entries = (dirResp && dirResp.entries) || [];
    if (entries.length && !el("newCwdInput").value) {
      el("newCwdInput").value = entries[0].path;
    }
  }

  function setModalTab(tabName) {
    const isDiscover = tabName === "discover";
    el("btnModalTabNew").classList.toggle("is-active", !isDiscover);
    el("btnModalTabDiscover").classList.toggle("is-active", isDiscover);
    el("newSessionForm").hidden = isDiscover;
    el("discoveredPane").hidden = !isDiscover;
    if (isDiscover) {
      loadDiscoveredSessions().catch((err) => {
        el("discoveredError").hidden = false;
        el("discoveredError").textContent = err.message;
      });
    }
  }

  async function importDiscoveredSession(item, forkFlag) {
    el("discoveredError").hidden = true;
    try {
      const resp = await apiJson("/rc/api/sessions/import", {
        method: "POST",
        body: {
          external_session_id: item.external_session_id,
          harness: item.source_harness,
          cwd: item.cwd,
          title: item.title || "",
          permission_mode: el("discoverModeSelect").value,
          model: item.model || null,
          fork_session: Boolean(forkFlag),
          server_url: item.server_url || null,
        },
      });
      el("newSessionDialog").close();
      await loadInitialWorkspace();
      if (resp.session && resp.session.id) {
        await selectSession(resp.session.id);
      }
    } catch (err) {
      el("discoveredError").hidden = false;
      el("discoveredError").textContent = err.message;
    }
  }

  async function loadDiscoveredSessions() {
    el("discoveredError").hidden = true;
    const filter = el("discoverHarnessFilter").value;
    const query = filter ? `?harness=${encodeURIComponent(filter)}` : "";
    const data = await apiJson(`/rc/api/discovered-sessions${query}`);
    const list = el("discoveredList");
    list.replaceChildren();
    const sessions = data.sessions || [];
    if (!sessions.length) {
      const empty = document.createElement("p");
      empty.className = "rc-muted";
      empty.textContent = "No local sessions discovered for this filter.";
      list.appendChild(empty);
      return;
    }
    sessions.forEach((item) => {
      const card = document.createElement("div");
      card.className = "rc-discover-item";

      const head = document.createElement("div");
      head.className = "rc-discover-head";
      const title = document.createElement("strong");
      title.textContent = item.title || item.external_session_id;
      const badge = document.createElement("span");
      badge.className = "rc-pill";
      badge.textContent = item.source_harness;
      head.appendChild(title);
      head.appendChild(badge);
      card.appendChild(head);

      const meta = document.createElement("div");
      meta.className = "rc-discover-meta";
      const when = item.updated_at
        ? new Date(Number(item.updated_at) * 1000).toLocaleString()
        : "unknown time";
      meta.textContent = `${item.cwd || "(no workspace)"} · ${when}${item.model ? ` · ${item.model}` : ""}`;
      card.appendChild(meta);

      const actions = document.createElement("div");
      actions.className = "rc-discover-actions";
      if (!item.within_allowed_dirs) {
        const warn = document.createElement("span");
        warn.className = "rc-pill rc-pill-warn";
        warn.textContent = "Outside RC_ALLOWED_DIRS";
        actions.appendChild(warn);
      }
      const resumeBtn = document.createElement("button");
      resumeBtn.type = "button";
      resumeBtn.className = "rc-btn rc-btn-primary";
      resumeBtn.textContent = "Resume";
      resumeBtn.disabled = !item.within_allowed_dirs;
      resumeBtn.addEventListener("click", () => importDiscoveredSession(item, false));

      const forkBtn = document.createElement("button");
      forkBtn.type = "button";
      forkBtn.className = "rc-btn rc-btn-ghost";
      forkBtn.textContent = "Fork";
      forkBtn.disabled = !item.within_allowed_dirs;
      forkBtn.addEventListener("click", () => importDiscoveredSession(item, true));

      actions.appendChild(resumeBtn);
      actions.appendChild(forkBtn);
      card.appendChild(actions);
      list.appendChild(card);
    });
  }

  async function browseDirs(path) {
    const query = path ? `?path=${encodeURIComponent(path)}` : "";
    const data = await apiJson(`/rc/api/fs/dirs${query}`);
    const list = el("dirBrowserList");
    list.hidden = false;
    list.replaceChildren();
    (data.entries || []).forEach((entry) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "rc-dir-item";
      btn.textContent = entry.path;
      btn.addEventListener("click", () => {
        el("newCwdInput").value = entry.path;
        browseDirs(entry.path).catch(() => {});
      });
      list.appendChild(btn);
    });
  }

  function formatUsd(val) {
    const num = Number(val || 0);
    if (!Number.isFinite(num) || num <= 0) return "$0.0000";
    return `$${num.toFixed(4)}`;
  }

  function formatCompactInt(val) {
    const num = Number(val || 0);
    if (!Number.isFinite(num)) return "0";
    if (num >= 1000000) return `${(num / 1000000).toFixed(1)}M`;
    if (num >= 1000) return `${(num / 1000).toFixed(1)}k`;
    return String(Math.round(num));
  }

  function renderTelemetryStrip() {
    const strip = el("telemetryStrip");
    if (!strip) return;
    const tel = state.telemetry;
    if (!state.activeSessionId) {
      strip.hidden = true;
      return;
    }
    strip.hidden = false;
    const inflightEl = el("telInflight");
    const costEl = el("telCost");
    const tokensEl = el("telTokens");
    const ttftEl = el("telTtft");
    const endpointEl = el("telEndpoint");
    const transEl = el("telTranslation");

    if (!tel) {
      inflightEl.hidden = true;
      costEl.textContent = "Cost: $0.0000";
      tokensEl.textContent = "Tokens: 0 in / 0 out";
      ttftEl.textContent = "TTFT: —";
      endpointEl.textContent = "Route: —";
      transEl.hidden = true;
      return;
    }

    const inflight = Array.isArray(tel.inflight) ? tel.inflight : [];
    if (inflight.length > 0) {
      const active = inflight[0];
      const phase = active.phase || "streaming";
      const suffix = inflight.length > 1 ? ` (+${inflight.length - 1})` : "";
      inflightEl.hidden = false;
      inflightEl.textContent = `Live: ${phase}${suffix}`;
    } else {
      inflightEl.hidden = true;
    }

    const reported = Number(tel.reported_cost_usd || 0);
    const estimated = Number(tel.estimated_cost_usd || 0);
    if (reported > 0 && estimated > 0) {
      costEl.textContent = `Cost: ${formatUsd(reported)} rep + ${formatUsd(estimated)} est`;
    } else if (reported > 0) {
      costEl.textContent = `Cost: ${formatUsd(reported)} (reported)`;
    } else if (estimated > 0) {
      costEl.textContent = `Cost: ${formatUsd(estimated)} (estimated)`;
    } else {
      costEl.textContent = "Cost: $0.0000";
    }

    const cachedPart = tel.cached_tokens
      ? ` · ${formatCompactInt(tel.cached_tokens)} cached`
      : "";
    tokensEl.textContent = `Tokens: ${formatCompactInt(tel.input_tokens)} in / ${formatCompactInt(tel.output_tokens)} out${cachedPart}`;

    const last = tel.last_request || null;
    if (last && last.ttft_ms != null) {
      ttftEl.textContent = `TTFT: ${Math.round(Number(last.ttft_ms))}ms`;
    } else {
      ttftEl.textContent = "TTFT: —";
    }

    if (last) {
      const modelName = last.model || "—";
      const providerName = last.provider_id || "—";
      const surfaceName = last.provider_endpoint || "";
      endpointEl.textContent = surfaceName
        ? `Route: ${providerName} · ${modelName} (${surfaceName})`
        : `Route: ${providerName} · ${modelName}`;
      if (last.translation_label) {
        transEl.hidden = false;
        transEl.textContent = last.translation_label;
      } else {
        transEl.hidden = true;
      }
    } else {
      endpointEl.textContent =
        state.activeSession && state.activeSession.model
          ? `Route: ${state.activeSession.model}`
          : "Route: —";
      transEl.hidden = true;
    }
  }

  function renderRecentRequests() {
    const list = el("telRecentList");
    if (!list) return;
    list.replaceChildren();
    const recent =
      state.telemetry && Array.isArray(state.telemetry.recent_requests)
        ? state.telemetry.recent_requests
        : [];
    if (!recent.length) {
      const empty = document.createElement("p");
      empty.className = "rc-muted";
      empty.textContent = "No matched proxy requests yet.";
      list.appendChild(empty);
      return;
    }
    recent.forEach((req) => {
      const row = document.createElement("div");
      row.className = "rc-recent-item";
      const left = document.createElement("span");
      const trans = req.translation_label ? ` [${req.translation_label}]` : "";
      left.textContent = `${req.provider_id || "?"} · ${req.model || "?"}${trans}`;
      const right = document.createElement("span");
      const costSrc = req.cost_source === "provider" ? "rep" : "est";
      const costVal = req.cost_usd != null ? formatUsd(req.cost_usd) : "$0.0000";
      right.textContent = `${req.status || "ok"} · ${costVal} (${costSrc}) · ${req.tokens_in || 0}/${req.tokens_out || 0}`;
      row.appendChild(left);
      row.appendChild(right);
      list.appendChild(row);
    });
  }

  async function loadTelemetry() {
    if (!state.authenticated || !state.activeSessionId) return;
    try {
      const data = await apiJson(`/rc/api/sessions/${state.activeSessionId}/telemetry`);
      state.telemetry = data;
      renderTelemetryStrip();
      renderRecentRequests();
    } catch (_err) {
      // Ignore transient telemetry errors
    }
  }

  function stopTelemetryPolling() {
    if (state.telemetryTimer) {
      clearInterval(state.telemetryTimer);
      state.telemetryTimer = null;
    }
  }

  function startTelemetryPolling() {
    stopTelemetryPolling();
    if (!state.authenticated || !state.activeSessionId || document.hidden) return;
    state.telemetryTimer = setInterval(() => {
      if (!state.authenticated || !state.activeSessionId || document.hidden) return;
      loadTelemetry().catch(() => {});
    }, 5000);
  }

  function populateQuickControls() {
    const modelSelect = el("qcModelSelect");
    const reasoningSelect = el("qcReasoningSelect");
    const provSelect = el("qcSurfaceProvider");
    const filterInput = el("qcModelSearch");
    if (!modelSelect || !reasoningSelect || !provSelect) return;

    const catalog = state.catalog || {
      tiers: [],
      models: [],
      multi_door_providers: [],
      reasoning_efforts: ["off", "minimal", "low", "medium", "high", "xhigh", "max", "auto"],
    };
    const query = ((filterInput && filterInput.value) || "").trim().toLowerCase();

    const prevModel =
      (state.activeSession && state.activeSession.model) || modelSelect.value || "";
    modelSelect.replaceChildren();
    const keepOpt = document.createElement("option");
    keepOpt.value = "";
    keepOpt.textContent = prevModel
      ? `Keep current (${prevModel})`
      : "Default (MCC routing)";
    modelSelect.appendChild(keepOpt);

    (catalog.tiers || []).forEach((t) => {
      const text = `${t.label || t.id} -> ${t.target || ""}`;
      if (query && !text.toLowerCase().includes(query) && !t.id.toLowerCase().includes(query)) {
        return;
      }
      const opt = document.createElement("option");
      opt.value = t.id;
      opt.textContent = `[tier] ${text}`;
      modelSelect.appendChild(opt);
    });

    (catalog.models || []).forEach((m) => {
      const ref = m.id || m.gateway_id || "";
      if (!ref || (query && !ref.toLowerCase().includes(query))) return;
      const opt = document.createElement("option");
      opt.value = ref;
      opt.textContent = m.display_name && m.display_name !== ref
        ? `${ref} (${m.display_name})`
        : ref;
      modelSelect.appendChild(opt);
    });

    reasoningSelect.replaceChildren();
    const defaultReason = document.createElement("option");
    defaultReason.value = "";
    defaultReason.textContent = "Default (harness/MCC)";
    reasoningSelect.appendChild(defaultReason);
    (catalog.reasoning_efforts || []).forEach((effort) => {
      const opt = document.createElement("option");
      opt.value = effort;
      opt.textContent = effort;
      reasoningSelect.appendChild(opt);
    });
    if (state.activeSession && state.activeSession.reasoning_effort) {
      reasoningSelect.value = state.activeSession.reasoning_effort;
    }

    const prevKey = provSelect.value;
    provSelect.replaceChildren();
    const noneProv = document.createElement("option");
    noneProv.value = "";
    noneProv.textContent = "— No global endpoint change —";
    provSelect.appendChild(noneProv);
    (catalog.multi_door_providers || []).forEach((entry) => {
      const opt = document.createElement("option");
      opt.value = `provider::${entry.provider_id}`;
      opt.textContent = `Provider: ${entry.provider_id} (${entry.preferred_surface || "default"})`;
      provSelect.appendChild(opt);
    });
    (catalog.models || []).forEach((m) => {
      if (!Array.isArray(m.offered_surfaces) || m.offered_surfaces.length < 2) return;
      const opt = document.createElement("option");
      opt.value = `model::${m.id}`;
      opt.textContent = `Model: ${m.id} (${m.preferred_surface || "inherit"})`;
      provSelect.appendChild(opt);
    });
    if (prevKey) {
      provSelect.value = prevKey;
    }
    renderSurfaceSelect();
  }

  function renderSurfaceSelect() {
    const provSelect = el("qcSurfaceProvider");
    const surfaceSelect = el("qcSurfaceSelect");
    if (!provSelect || !surfaceSelect) return;
    surfaceSelect.replaceChildren();
    const key = provSelect.value;
    if (!key) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "Select a multi-door provider/model first";
      surfaceSelect.appendChild(opt);
      surfaceSelect.disabled = true;
      return;
    }
    surfaceSelect.disabled = false;
    const [scope, targetKey] = key.split("::");
    const catalog = state.catalog || { multi_door_providers: [], models: [] };
    let entry = null;
    if (scope === "provider") {
      entry = (catalog.multi_door_providers || []).find(
        (item) => item.provider_id === targetKey,
      );
    } else {
      entry = (catalog.models || []).find((item) => item.id === targetKey);
    }
    const surfaces = (entry && entry.offered_surfaces) || [];
    if (scope === "model") {
      const inheritOpt = document.createElement("option");
      inheritOpt.value = "__inherit__";
      inheritOpt.textContent = "__inherit__ (use provider default)";
      surfaceSelect.appendChild(inheritOpt);
    }
    surfaces.forEach((surface) => {
      const opt = document.createElement("option");
      opt.value = surface;
      opt.textContent = surface;
      surfaceSelect.appendChild(opt);
    });
    if (entry && entry.preferred_surface) {
      surfaceSelect.value = entry.preferred_surface;
    }
  }

  async function applyQuickControls() {
    if (!state.activeSessionId) return;
    const modelSelect = el("qcModelSelect");
    const modelSearch = el("qcModelSearch");
    const reasoningSelect = el("qcReasoningSelect");
    const provSelect = el("qcSurfaceProvider");
    const surfaceSelect = el("qcSurfaceSelect");
    const statusEl = el("qcStatus");

    const body = {};
    const selectedModel = (modelSelect && modelSelect.value) || "";
    const typedModel = (modelSearch && modelSearch.value.trim()) || "";
    if (selectedModel) {
      body.model = selectedModel;
    } else if (typedModel && !(modelSelect && modelSelect.options.length > 1)) {
      body.model = typedModel;
    }
    if (reasoningSelect && reasoningSelect.value) {
      body.reasoning_effort = reasoningSelect.value;
    }
    if (provSelect && provSelect.value && surfaceSelect && surfaceSelect.value) {
      const [scope, targetKey] = provSelect.value.split("::");
      body.preferred_surface_scope = scope;
      body.preferred_surface_key = targetKey;
      body.preferred_surface = surfaceSelect.value;
    }

    statusEl.textContent = "Applying...";
    try {
      const resp = await apiJson(`/rc/api/sessions/${state.activeSessionId}/proxy-controls`, {
        method: "POST",
        body,
      });
      if (resp && resp.session) {
        state.activeSession = resp.session;
      }
      if (resp && resp.preferred_surface) {
        const refreshedCatalog = await apiJson("/rc/api/catalog").catch(() => null);
        if (refreshedCatalog) state.catalog = refreshedCatalog;
      }
      renderActiveSessionHeader();
      renderMetaTab();
      populateQuickControls();
      statusEl.textContent = "Controls updated.";
    } catch (err) {
      statusEl.textContent = err.message || "Update failed.";
    }
  }

  function urlBase64ToUint8Array(base64String) {
    const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    const rawData = window.atob(base64);
    const outputArray = new Uint8Array(rawData.length);
    for (let i = 0; i < rawData.length; i += 1) {
      outputArray[i] = rawData.charCodeAt(i);
    }
    return outputArray;
  }

  async function refreshPushUi() {
    const toggleBtn = el("btnPushToggle");
    const testBtn = el("btnTestPush");
    if (!toggleBtn || !testBtn) return;
    const supported =
      "serviceWorker" in navigator &&
      "PushManager" in window &&
      "Notification" in window;
    if (!supported) {
      toggleBtn.textContent = "Alerts: Unsupported";
      testBtn.hidden = true;
      return;
    }
    try {
      const reg =
        state.swRegistration || (await navigator.serviceWorker.getRegistration("/rc/"));
      if (reg) state.swRegistration = reg;
      const sub = reg ? await reg.pushManager.getSubscription() : null;
      state.pushSubscribed = Boolean(sub);
      toggleBtn.textContent = state.pushSubscribed ? "Alerts: On" : "Alerts: Off";
      testBtn.hidden = !state.pushSubscribed;
    } catch (_err) {
      toggleBtn.textContent = "Alerts: Off";
      testBtn.hidden = true;
    }
  }

  async function togglePushSubscription() {
    const toggleBtn = el("btnPushToggle");
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
      if (toggleBtn) toggleBtn.textContent = "Alerts: Unsupported";
      return;
    }
    const reg =
      state.swRegistration || (await navigator.serviceWorker.register("/rc/sw.js"));
    state.swRegistration = reg;
    const existing = await reg.pushManager.getSubscription();
    if (existing) {
      await apiJson("/rc/api/push/subscribe", {
        method: "DELETE",
        body: { endpoint: existing.endpoint },
      }).catch(() => {});
      await existing.unsubscribe().catch(() => {});
      await refreshPushUi();
      return;
    }
    const perm = await Notification.requestPermission();
    if (perm !== "granted") {
      if (toggleBtn) toggleBtn.textContent = "Alerts: Blocked";
      return;
    }
    const keyResp = await apiJson("/rc/api/push/vapid-public-key");
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(keyResp.public_key),
    });
    const jsonSub = sub.toJSON();
    await apiJson("/rc/api/push/subscribe", {
      method: "POST",
      body: {
        endpoint: jsonSub.endpoint,
        keys: jsonSub.keys || {},
        session_id: state.activeSessionId || null,
      },
    });
    await refreshPushUi();
  }

  async function pairWithToken(token, label) {
    el("pairError").hidden = true;
    const data = await apiJson("/rc/api/pair", {
      method: "POST",
      body: { token: token.trim(), label: (label || "").trim() },
    });
    state.csrfToken = data.csrf_token || readCookie("mcc_rc_csrf");
    setAuthenticated(true);
    await loadInitialWorkspace();
  }

  async function bootstrap() {
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker
        .register("/rc/sw.js")
        .then((reg) => {
          state.swRegistration = reg;
          refreshPushUi().catch(() => {});
        })
        .catch(() => {});
    }
    const params = new URLSearchParams(window.location.search);
    const pairToken = params.get("pair");
    if (pairToken) {
      window.history.replaceState({}, document.title, window.location.pathname);
      try {
        await pairWithToken(pairToken, "Mobile Browser");
        return;
      } catch (err) {
        el("pairError").hidden = false;
        el("pairError").textContent = err.message;
      }
    }
    try {
      const me = await apiJson("/rc/api/me");
      if (me.authenticated) {
        state.csrfToken = readCookie("mcc_rc_csrf");
        setAuthenticated(true);
        await loadInitialWorkspace();
      } else {
        setAuthenticated(false);
      }
    } catch (_err) {
      setAuthenticated(false);
    }
  }

  // Event wiring
  el("btnToggleSidebar").addEventListener("click", () => {
    el("sessionSidebar").classList.toggle("is-open");
  });

  el("pairForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await pairWithToken(el("pairTokenInput").value, el("pairLabelInput").value);
    } catch (err) {
      el("pairError").hidden = false;
      el("pairError").textContent = err.message;
    }
  });

  el("btnLogout").addEventListener("click", async () => {
    await apiJson("/rc/api/logout", { method: "POST" }).catch(() => {});
    state.sessions = [];
    state.activeSessionId = "";
    state.activeSession = null;
    setAuthenticated(false);
  });

  el("btnRefreshSessions").addEventListener("click", () => {
    loadInitialWorkspace().catch(() => {});
  });

  [el("btnNewSessionOpen"), el("btnEmptyNewSession")].forEach((btn) => {
    btn.addEventListener("click", () => {
      el("newSessionError").hidden = true;
      setModalTab("new");
      el("newSessionDialog").showModal();
    });
  });

  el("btnEmptyDiscoverSession").addEventListener("click", () => {
    el("newSessionError").hidden = true;
    setModalTab("discover");
    el("newSessionDialog").showModal();
  });

  el("btnModalTabNew").addEventListener("click", () => setModalTab("new"));
  el("btnModalTabDiscover").addEventListener("click", () => setModalTab("discover"));
  el("btnRefreshDiscovered").addEventListener("click", () => {
    loadDiscoveredSessions().catch((err) => {
      el("discoveredError").hidden = false;
      el("discoveredError").textContent = err.message;
    });
  });
  el("discoverHarnessFilter").addEventListener("change", () => {
    loadDiscoveredSessions().catch((err) => {
      el("discoveredError").hidden = false;
      el("discoveredError").textContent = err.message;
    });
  });

  async function switchToInteractivePty() {
    if (!state.activeSessionId) return;
    const cols = (state.term && state.term.cols) || 120;
    const rows = (state.term && state.term.rows) || 36;
    const resp = await apiJson(`/rc/api/sessions/${state.activeSessionId}/switch-pty`, {
      method: "POST",
      body: { cols, rows },
    });
    if (resp && resp.session) {
      state.activeSession = resp.session;
    }
    renderActiveSessionHeader();
    renderMetaTab();
    await loadInitialWorkspace();
    setActiveTab("terminal");
  }

  el("btnSwitchTerminal").addEventListener("click", () => {
    switchToInteractivePty().catch(() => {});
  });

  if (el("btnTabSwitchPty")) {
    el("btnTabSwitchPty").addEventListener("click", () => {
      switchToInteractivePty().catch(() => {});
    });
  }

  el("btnCloseDialog").addEventListener("click", () => {
    el("newSessionDialog").close();
  });

  el("newModeSelect").addEventListener("change", () => {
    el("yoloWarning").hidden = el("newModeSelect").value !== "yolo";
  });

  el("btnBrowseDirs").addEventListener("click", () => {
    browseDirs(el("newCwdInput").value.trim()).catch((err) => {
      el("newSessionError").hidden = false;
      el("newSessionError").textContent = err.message;
    });
  });

  el("newSessionForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    el("newSessionError").hidden = true;
    try {
      const resp = await apiJson("/rc/api/sessions", {
        method: "POST",
        body: {
          harness: el("newHarnessSelect").value,
          cwd: el("newCwdInput").value.trim(),
          permission_mode: el("newModeSelect").value,
          create_worktree: el("newWorktreeCheck").checked,
          model: el("newModelInput").value.trim() || null,
          prompt: el("newPromptInput").value,
        },
      });
      el("newSessionDialog").close();
      await loadInitialWorkspace();
      if (resp.session && resp.session.id) {
        await selectSession(resp.session.id);
      }
    } catch (err) {
      el("newSessionError").hidden = false;
      el("newSessionError").textContent = err.message;
    }
  });

  document.querySelectorAll(".rc-tab").forEach((btn) => {
    btn.addEventListener("click", () => setActiveTab(btn.dataset.tab));
  });

  el("composerForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = el("composerInput").value.trim();
    if (!text || !state.activeSessionId) return;
    el("composerInput").value = "";
    await apiJson(`/rc/api/sessions/${state.activeSessionId}/message`, {
      method: "POST",
      body: { text },
    });
  });

  el("btnInterrupt").addEventListener("click", async () => {
    if (!state.activeSessionId) return;
    await apiJson(`/rc/api/sessions/${state.activeSessionId}/interrupt`, {
      method: "POST",
    });
  });

  el("activeModeSelect").addEventListener("change", async () => {
    if (!state.activeSessionId) return;
    await apiJson(`/rc/api/sessions/${state.activeSessionId}/mode`, {
      method: "POST",
      body: { mode: el("activeModeSelect").value },
    });
  });

  el("btnSetModel").addEventListener("click", async () => {
    const model = el("modelOverrideInput").value.trim();
    if (!state.activeSessionId || !model) return;
    await apiJson(`/rc/api/sessions/${state.activeSessionId}/model`, {
      method: "POST",
      body: { model },
    });
    await refreshActiveMeta();
  });

  el("btnForkSession").addEventListener("click", async () => {
    if (!state.activeSessionId) return;
    const resp = await apiJson(`/rc/api/sessions/${state.activeSessionId}/fork`, {
      method: "POST",
      body: {},
    });
    await loadInitialWorkspace();
    if (resp.session && resp.session.id) {
      await selectSession(resp.session.id);
    }
  });

  el("btnStopSession").addEventListener("click", async () => {
    if (!state.activeSessionId) return;
    await apiJson(`/rc/api/sessions/${state.activeSessionId}/stop`, {
      method: "POST",
    });
    await loadInitialWorkspace();
    await refreshActiveMeta();
  });

  el("btnDeleteSession").addEventListener("click", async () => {
    if (!state.activeSessionId) return;
    await apiJson(`/rc/api/sessions/${state.activeSessionId}`, {
      method: "DELETE",
    });
    state.activeSessionId = "";
    state.activeSession = null;
    el("sessionDetail").hidden = true;
    el("emptyState").hidden = false;
    await loadInitialWorkspace();
  });

  el("btnRefreshDiff").addEventListener("click", () => {
    loadDiff().catch(() => {});
  });

  el("btnQuickControls").addEventListener("click", () => {
    const drawer = el("quickControlsDrawer");
    drawer.hidden = !drawer.hidden;
    if (!drawer.hidden) {
      el("qcStatus").textContent = "";
      populateQuickControls();
    }
  });

  el("btnCloseQuickControls").addEventListener("click", () => {
    el("quickControlsDrawer").hidden = true;
  });

  el("qcModelSearch").addEventListener("input", () => {
    populateQuickControls();
  });

  el("qcSurfaceProvider").addEventListener("change", () => {
    renderSurfaceSelect();
  });

  el("btnApplyQuickControls").addEventListener("click", () => {
    applyQuickControls().catch(() => {});
  });

  el("btnPushToggle").addEventListener("click", () => {
    togglePushSubscription().catch(() => {});
  });

  el("btnTestPush").addEventListener("click", () => {
    apiJson("/rc/api/push/test", {
      method: "POST",
      body: {
        title: "MCC Remote Control",
        body: "Test push notification from /rc",
        session_id: state.activeSessionId || null,
      },
    }).catch(() => {});
  });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopTelemetryPolling();
    } else if (state.authenticated && state.activeSessionId) {
      loadTelemetry().catch(() => {});
      startTelemetryPolling();
    }
  });

  document.querySelectorAll(".rc-key").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (!state.wsPty || state.wsPty.readyState !== WebSocket.OPEN) return;
      const raw = JSON.parse(`"${btn.dataset.key}"`);
      state.wsPty.send(JSON.stringify({ type: "input", data: raw }));
      if (state.term) state.term.focus();
    });
  });

  bootstrap();
})();
