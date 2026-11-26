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
    el("connDot").classList.toggle("is-online", state.authenticated);
    if (!state.authenticated) {
      closeWebSockets();
    }
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
      ? "Claude asks a question"
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
    } else if (kind === "turn.completed") {
      state.streamingAssistantEl = null;
      state.streamingThinkingEl = null;
      refreshActiveMeta().catch(() => {});
    } else if (kind === "session.mode_changed" || kind === "session.state" || kind === "session.exited") {
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
    el("activeTierBadge").textContent =
      sess.driver_tier === "tier_1b_claude" ? "Structured" : "PTY";
    el("activeModeSelect").value = sess.permission_mode || "ask";
    const branchBadge = el("activeBranchBadge");
    if (sess.branch_name) {
      branchBadge.hidden = false;
      branchBadge.textContent = sess.branch_name;
    } else {
      branchBadge.hidden = true;
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
      ["Claude ID", sess.external_session_id || "—"],
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
    renderActiveSessionHeader();
    renderMetaTab();
    (data.events || []).forEach(handleSessionEvent);
    connectEventSocket(sessionId);

    if (state.activeTab === "terminal" || data.session.driver_tier !== "tier_1b_claude") {
      if (data.session.driver_tier !== "tier_1b_claude") {
        setActiveTab("terminal");
      } else {
        ensureTerminalMounted(sessionId);
      }
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
      ensureTerminalMounted(state.activeSessionId);
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
    const [sessResp, harnessResp, dirResp] = await Promise.all([
      apiJson("/rc/api/sessions"),
      apiJson("/rc/api/harnesses"),
      apiJson("/rc/api/fs/dirs").catch(() => ({ entries: [] })),
    ]);
    state.sessions = sessResp.sessions || [];
    state.harnesses = harnessResp.harnesses || [];
    renderSessionList();
    populateNewSessionDialog(dirResp);
    if (state.sessions.length && !state.activeSessionId) {
      await selectSession(state.sessions[0].id);
    }
  }

  function populateNewSessionDialog(dirResp) {
    const select = el("newHarnessSelect");
    select.replaceChildren();
    state.harnesses.forEach((h) => {
      const opt = document.createElement("option");
      opt.value = h.id;
      const tierLabel = h.driver_tier === "tier_1b_claude" ? "Structured" : "PTY";
      const installed = h.installed ? "" : " (not found)";
      opt.textContent = `${h.label} [${tierLabel}]${installed}`;
      select.appendChild(opt);
    });
    const entries = (dirResp && dirResp.entries) || [];
    if (entries.length && !el("newCwdInput").value) {
      el("newCwdInput").value = entries[0].path;
    }
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
      navigator.serviceWorker.register("/rc/sw.js").catch(() => {});
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
      el("newSessionDialog").showModal();
    });
  });

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
