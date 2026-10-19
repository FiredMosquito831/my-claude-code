/**
 * The Coding agents page's Install buttons (7.102.0), run instead of the main
 * capture when MCC_JSDOM_SCENARIO=agent_installs.
 *
 * The user's words (2026-10-06): buttons on the Coding agents tab for each
 * agent, CLI and desktop, to download and install it, and Node.js and npm too
 * when they are missing. What this drives, on payloads in the server's own
 * shapes (api/admin_agent_install_routes.py):
 *
 *  - which cards get Install (not installed, a method runs here), which show
 *    a path and version and no button (installed), which say why not (Kimi),
 *    which are a link (Warp), and the picker where an OS has several methods;
 *  - the Copy command button beside Install, following the picker;
 *  - the plan, in words, before anything runs: the Node.js step, the
 *    permission prompt, the PATH / profile lines, the commands it adds;
 *  - Install now: the run request (confirm token, plan token), the progress
 *    lines while the job runs, and the failure panel when it fails.
 *
 * Prints nothing itself: `run()` returns the measurements and the harness
 * prints them as one JSON object.
 */

const clone = (value) => (value === undefined ? value : JSON.parse(JSON.stringify(value)));
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const LOG = "C:\\Users\\u\\.mcc\\logs\\agent-installs\\gemini_cli-20261019-010203-abc123.log";

const method = (overrides) => ({
  arch: "",
  url: "",
  package: "",
  elevates: "none",
  changes: [],
  doc_url: "https://example.invalid/doc",
  checked_on: "2026-10-19",
  checked_by: "Context7",
  needs_node: null,
  gui: false,
  runnable: true,
  refused: "",
  node_install: false,
  os: "windows",
  ...overrides,
});

const CLAUDE_METHODS = [
  method({
    id: "native",
    label: "Anthropic's installer (recommended)",
    kind: "script",
    url: "https://claude.ai/install.ps1",
    copy_command: "irm https://claude.ai/install.ps1 | iex",
    doc_url: "https://code.claude.com/docs/en/setup",
    changes: ["puts claude in ~/.local/bin and adds that folder to your PATH"],
  }),
  method({
    id: "winget",
    label: "winget",
    kind: "winget",
    package: "Anthropic.ClaudeCode",
    elevates: "os_prompt",
    copy_command: "winget install Anthropic.ClaudeCode",
  }),
  method({
    id: "npm",
    label: "npm",
    kind: "npm",
    package: "@anthropic-ai/claude-code",
    copy_command: "npm install -g @anthropic-ai/claude-code",
    needs_node: { minimum: "22", source: "engines.node >=22.0.0" },
    node_install: true,
  }),
];

const GEMINI_METHODS = [
  method({
    id: "npm",
    label: "npm",
    kind: "npm",
    package: "@google/gemini-cli",
    copy_command: "npm install -g @google/gemini-cli",
    doc_url: "https://github.com/google-gemini/gemini-cli#-installation",
    needs_node: { minimum: "20", source: "engines.node >=20" },
    node_install: true,
  }),
];

const agent = (overrides) => ({
  kind: "cli",
  installed: false,
  path: "",
  version: "",
  on_server_path: true,
  offer_install: false,
  recommended: "",
  methods: [],
  link: "",
  hidden_reason: "",
  unavailable_reason: "",
  same_as: "",
  ...overrides,
});

const LISTING = {
  os: "windows",
  arch: "x64",
  wsl: false,
  headless: false,
  headless_reason: "",
  node: { state: "missing", node_path: "", node_version: "" },
  job: null,
  agents: [
    agent({
      id: "claude",
      display_name: "Claude Code",
      offer_install: true,
      recommended: "native",
      methods: CLAUDE_METHODS,
    }),
    agent({
      id: "codex",
      display_name: "Codex CLI",
      installed: true,
      path: "C:\\Users\\u\\.local\\bin\\codex.exe",
      version: "codex-cli 0.163.0",
      methods: [
        method({
          id: "native",
          label: "OpenAI's installer (recommended)",
          kind: "script",
          copy_command: "irm https://chatgpt.com/codex/install.ps1 | iex",
          runnable: false,
          refused: "Codex CLI is already installed at C:\\Users\\u\\.local\\bin\\codex.exe.",
        }),
      ],
    }),
    agent({
      id: "gemini_cli",
      display_name: "Gemini CLI",
      offer_install: true,
      recommended: "npm",
      methods: GEMINI_METHODS,
    }),
    agent({
      id: "kimi_code",
      display_name: "Kimi Code",
      unavailable_reason:
        "No Install button yet: Moonshot now ships Kimi Code (@moonshot-ai/kimi-code) " +
        "in place of the kimi-cli this launcher was built for, and MCC has not yet " +
        "checked that mcc-kimi works with it (7.102.0, 2026-10-19).",
    }),
    agent({
      id: "warp",
      kind: "desktop",
      display_name: "Warp",
      link: "https://www.warp.dev/download",
      methods: [
        method({
          id: "link",
          label: "Warp's download page",
          kind: "link",
          url: "https://www.warp.dev/download",
          copy_command: "https://www.warp.dev/download",
          runnable: false,
          refused: "The vendor publishes no direct download MCC can open here.",
        }),
      ],
    }),
    agent({
      id: "crush_desktop",
      kind: "desktop",
      display_name: "Crush",
      same_as: "crush",
    }),
  ],
};

const CLAUDE_PLAN = {
  target: "claude",
  kind: "cli",
  display_name: "Claude Code",
  os: "windows",
  arch: "x64",
  method: CLAUDE_METHODS[0],
  runnable: true,
  refused: "",
  steps: [
    {
      kind: "download",
      text: "Download the vendor's script to a file (https, https://claude.ai/install.ps1)",
      command: [],
      url: "https://claude.ai/install.ps1",
      hosts: ["claude.ai"],
      timeout_seconds: 60,
      env: {},
    },
    {
      kind: "run",
      text:
        "Run the script by path with Windows PowerShell (-ExecutionPolicy Bypass " +
        "for this one script, as the vendor's own line does)",
      command: [
        "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        "<downloaded file>",
      ],
      url: "",
      hosts: [],
      timeout_seconds: 900,
      env: {},
    },
    {
      kind: "verify",
      text: "Look for claude with the refreshed PATH, then run it with --version (5 s)",
      command: [],
      url: "",
      hosts: [],
      timeout_seconds: 5,
      env: {},
    },
  ],
  elevation: [],
  changes: [
    "puts claude in ~/.local/bin and adds that folder to your PATH",
    "Claude Code then updates itself in the background",
  ],
  node: { needed: false, bound: "", bound_source: "", install: false, text: "", status: {} },
  bins: ["claude"],
  verify: { names: ["claude"], dirs: ["~/.local/bin"] },
  copy_command: "irm https://claude.ai/install.ps1 | iex",
  doc_url: "https://code.claude.com/docs/en/setup",
  plan_token: "a".repeat(64),
  confirm: "install-claude",
};

const GEMINI_PLAN = {
  ...CLAUDE_PLAN,
  target: "gemini_cli",
  display_name: "Gemini CLI",
  method: GEMINI_METHODS[0],
  steps: [
    {
      kind: "node",
      text:
        "Read the newest Node.js LTS release from nodejs.org, download its " +
        "installer (node-<version>-x64.msi or .pkg) and SHASUMS256.txt, and check " +
        "the installer's SHA-256 against it",
      command: [],
      url: "https://nodejs.org/dist/index.json",
      hosts: ["nodejs.org"],
      timeout_seconds: 1200,
      env: {},
    },
    {
      kind: "node",
      text: "Open the Node.js installer by path",
      command: ["msiexec.exe", "/i", "<downloaded file>", "/passive", "/norestart"],
      url: "",
      hosts: [],
      timeout_seconds: 1200,
      env: {},
    },
    {
      kind: "run",
      text: "Install @google/gemini-cli with npm",
      command: ["npm.cmd", "install", "-g", "@google/gemini-cli"],
      url: "",
      hosts: [],
      timeout_seconds: 900,
      env: {},
    },
  ],
  elevation: ["Windows will ask for administrator permission (the Node.js installer)"],
  changes: [
    "installs Node.js LTS and npm into C:\\Program Files\\nodejs",
    "adds that folder and your npm folder (%APPDATA%\\npm) to PATH",
    "adds the package to npm's global folder, which npm keeps on PATH",
  ],
  node: {
    needed: true,
    bound: "Node.js 20 or newer",
    bound_source: "engines.node >=20",
    install: true,
    text:
      "Node.js LTS will be installed first (the newest LTS nodejs.org lists when " +
      "you press Install), then @google/gemini-cli.",
    status: { state: "missing" },
  },
  bins: ["gemini"],
  copy_command: "npm install -g @google/gemini-cli",
  doc_url: "https://github.com/google-gemini/gemini-cli#-installation",
  plan_token: "b".repeat(64),
  confirm: "install-gemini_cli",
};

const RUNNING = {
  job_id: "job-gemini",
  target: "gemini_cli",
  display_name: "Gemini CLI",
  method: "npm",
  dry_run: false,
  stage: "running",
  running: true,
  lines: [
    "Node.js is missing: installing it first.",
    "download https://nodejs.org/dist/index.json",
  ],
  cursor: 2,
  exit_code: null,
  log_path: LOG,
  result: {},
};

const FAILED_LINES = [
  "$ \"C:\\Program Files\\nodejs\\npm.cmd\" install -g @google/gemini-cli",
  "npm ERR! code EACCES",
  "npm ERR! Error: EACCES: permission denied",
];

const FAILED = {
  ...RUNNING,
  stage: "failed",
  running: false,
  lines: FAILED_LINES,
  cursor: 5,
  exit_code: 243,
  result: {
    status: "failed",
    message: "npm.cmd exited 243.",
    classification: "permission",
    classification_label:
      "Permission: the system refused a write (EACCES / EPERM / access denied)",
    exit_code: 243,
    last_lines: [...RUNNING.lines, ...FAILED_LINES],
    log_path: LOG,
    command: "\"C:\\Program Files\\nodejs\\npm.cmd\" install -g @google/gemini-cli",
    copy_command: "npm install -g @google/gemini-cli",
    doc_url: "https://github.com/google-gemini/gemini-cli#-installation",
  },
};

export function prepareAgentInstallsScenario({ window, ROUTES }) {
  const harnesses = ROUTES["/admin/api/harnesses"].harnesses;
  for (const harness of harnesses) {
    if (harness.id === "claude" || harness.id === "gemini_cli") {
      harness.installed = false;
      harness.binary_path = null;
    }
  }
  ROUTES["/admin/api/agent-installs"] = clone(LISTING);

  const doc = window.document;
  const flat = (el) => (el ? (el.textContent || "").replace(/\s+/g, " ").trim() : "");
  const requests = [];
  const copied = [];
  let polls = 0;

  const answer = (status, body) => ({
    ok: status < 400,
    status,
    statusText: status < 400 ? "OK" : "Error",
    json: async () => clone(body),
    text: async () => JSON.stringify(body),
  });

  const installRecorder = () => {
    const realFetch = window.fetch;
    window.fetch = async (url, options = {}) => {
      const path = String(url).split("?")[0];
      const verb = String(options.method || "GET").toUpperCase();
      if (path.startsWith("/admin/api/agent-installs")) {
        requests.push({
          url: String(url),
          method: verb,
          body: options.body ? JSON.parse(options.body) : null,
        });
      }
      const plan = path.match(/^\/admin\/api\/agent-installs\/([^/]+)\/plan$/);
      if (plan && verb === "POST") {
        return answer(200, plan[1] === "gemini_cli" ? GEMINI_PLAN : CLAUDE_PLAN);
      }
      const run = path.match(/^\/admin\/api\/agent-installs\/([^/]+)\/run$/);
      if (run && verb === "POST") {
        return answer(200, { job_id: "job-gemini", log_path: LOG });
      }
      if (path === "/admin/api/agent-installs/jobs/job-gemini") {
        polls += 1;
        return answer(200, polls === 1 ? RUNNING : FAILED);
      }
      return realFetch(url, options);
    };
    Object.defineProperty(window.navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: (text) => {
          copied.push(text);
          return Promise.resolve();
        },
      },
    });
  };

  const host = (id) => doc.querySelector(`.agent-install-control[data-install-target="${id}"]`);
  const describe = (id) => {
    const el = host(id);
    if (!el) return null;
    const button = el.querySelector(".agent-install-button");
    const picker = el.querySelector(".agent-install-method");
    const copy = el.querySelector(".agent-install-copy");
    const link = el.querySelector("a");
    return {
      hidden: el.hidden,
      inCard: Boolean(el.closest(".coding-agent-card")),
      text: flat(el),
      button: button ? flat(button) : null,
      picker: picker
        ? Array.from(picker.options).map((option) => ({
            value: option.value,
            text: flat(option),
            selected: option.selected,
            disabled: option.disabled,
          }))
        : null,
      copy: copy ? { text: flat(copy), data: copy.dataset.copy, disabled: copy.disabled } : null,
      copyBesideButton: Boolean(
        button && copy && button.parentElement === copy.parentElement,
      ),
      link: link ? { href: link.getAttribute("href"), text: flat(link) } : null,
      state: flat(el.querySelector(".agent-install-state")),
      notes: Array.from(el.querySelectorAll(".agent-install-note")).map(flat),
    };
  };

  const describePlan = (id) => {
    const el = host(id);
    const view = el ? el.querySelector(".agent-install-plan") : null;
    if (!view) return null;
    return {
      role: view.getAttribute("role"),
      heading: flat(view.querySelector("h5")),
      node: Array.from(view.querySelectorAll(".agent-install-node")).map(flat),
      elevation: Array.from(view.querySelectorAll(".agent-install-elevation")).map(flat),
      steps: Array.from(view.querySelectorAll(".agent-install-steps > li")).map(flat),
      changes: Array.from(view.querySelectorAll(".agent-install-changes > li")).map(flat),
      notes: Array.from(view.querySelectorAll(".agent-install-note")).map(flat),
      confirm: flat(view.querySelector(".agent-install-confirm")),
      back: flat(view.querySelector(".agent-install-back")),
    };
  };

  async function run({ scriptErrors, consoleErrors }) {
    await wait(900);
    installRecorder();
    const out = { scenarioFatal: null };
    const link = Array.from(doc.querySelectorAll(".nav-link")).find(
      (el) => el.dataset.view === "coding_agents",
    );
    if (!link) {
      out.scenarioFatal = "no Coding agents nav link";
      return out;
    }
    link.click();
    await wait(900);

    out.cards = {};
    for (const id of ["claude", "codex", "gemini_cli", "kimi_code", "warp", "crush_desktop"]) {
      out.cards[id] = describe(id);
    }

    // The copy button follows the picker.
    const claudeHost = host("claude");
    const copyButton = claudeHost.querySelector(".agent-install-copy");
    copyButton.click();
    await wait(50);
    const picker = claudeHost.querySelector(".agent-install-method");
    picker.value = "npm";
    picker.dispatchEvent(new window.Event("change", { bubbles: true }));
    await wait(50);
    out.copyAfterPick = describe("claude").copy;
    out.detailsAfterPick = flat(claudeHost.querySelector(".agent-install-details"));
    claudeHost.querySelector(".agent-install-copy").click();
    await wait(50);
    out.copied = [...copied];
    picker.value = "native";
    picker.dispatchEvent(new window.Event("change", { bubbles: true }));
    await wait(50);

    // Install on Claude Code: the plan, nothing run yet.
    claudeHost.querySelector(".agent-install-button").click();
    await wait(200);
    out.claudePlan = describePlan("claude");
    out.requestsAfterClaudePlan = clone(requests);

    // Install on Gemini CLI (npm, Node.js missing): the Node step and the
    // permission prompt in words; then Install now.
    const geminiHost = host("gemini_cli");
    geminiHost.querySelector(".agent-install-button").click();
    await wait(200);
    out.geminiPlan = describePlan("gemini_cli");
    host("gemini_cli").querySelector(".agent-install-confirm").click();
    await wait(250);
    const progress = host("gemini_cli").querySelector(".agent-install-progress");
    out.progress = progress
      ? {
          role: progress.getAttribute("role"),
          log: (progress.querySelector(".agent-install-log") || {}).textContent || "",
          cancel: flat(progress.querySelector(".agent-install-cancel")),
          text: flat(progress),
        }
      : null;
    await wait(1800);
    const failure = host("gemini_cli").querySelector(".agent-install-failure");
    out.failure = failure
      ? {
          message: flat(failure.querySelector(".agent-install-refused")),
          facts: Array.from(failure.querySelectorAll(".agent-install-facts > li")).map(flat),
          tail: (failure.querySelector(".agent-install-log") || {}).textContent || "",
          command: flat(failure.querySelector(".agent-install-command")),
          copy: (failure.querySelector(".agent-install-copy") || { dataset: {} }).dataset.copy,
          link: (failure.querySelector("a") || { getAttribute: () => null }).getAttribute("href"),
          notes: Array.from(failure.querySelectorAll(".agent-install-note")).map(flat),
        }
      : null;
    out.geminiAfter = describe("gemini_cli");
    out.requests = clone(requests);
    out.polls = polls;
    out.scriptErrors = scriptErrors;
    out.consoleErrors = consoleErrors;
    return out;
  }

  return { run };
}
