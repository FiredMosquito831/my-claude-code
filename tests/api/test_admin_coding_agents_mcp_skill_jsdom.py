"""Sequential MCC_CI=1 jsdom tests for PR 39 MCP 3-state modes and Skill UI."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

STATIC_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "my_claude_code"
    / "api"
    / "admin_static"
)
NODE_MODULES = Path(__file__).resolve().parents[1] / "node_modules"
ON_CI = os.environ.get("MCC_CI", "") not in ("", "0", "false")

pytestmark = pytest.mark.xdist_group("admin_static_jsdom")


def _run_pr39_jsdom_scenario() -> dict:
    node = shutil.which("node")
    if node is None:
        if ON_CI:
            pytest.fail("MCC_CI=1 and node is not on PATH.")
        pytest.skip("node is not on PATH")

    script = r"""
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const staticDir = process.argv[2];
const nodeModulesDir = process.argv[3];
const require = createRequire(path.join(nodeModulesDir, "jsdom", "package.json"));
const { JSDOM } = require(path.join(nodeModulesDir, "jsdom", "lib", "api.js"));

const html = fs.readFileSync(path.join(staticDir, "index.html"), "utf8");
const usageLayoutsJs = fs.existsSync(path.join(staticDir, "usage_layouts.js"))
  ? fs.readFileSync(path.join(staticDir, "usage_layouts.js"), "utf8")
  : "";
const designsJs = fs.existsSync(path.join(staticDir, "designs.js"))
  ? fs.readFileSync(path.join(staticDir, "designs.js"), "utf8")
  : "";
const js = [usageLayoutsJs, designsJs, fs.readFileSync(path.join(staticDir, "admin.js"), "utf8")]
  .filter(Boolean)
  .join("\n");

const dom = new JSDOM(html, {
  url: "http://127.0.0.1:8082/",
  runScripts: "outside-only",
  pretendToBeVisual: true,
});
const { window } = dom;
if (!window.requestAnimationFrame) {
  window.requestAnimationFrame = (fn) => setTimeout(fn, 0);
  window.cancelAnimationFrame = (id) => clearTimeout(id);
}
if (!window.matchMedia) {
  window.matchMedia = () => ({
    matches: false,
    media: "",
    addEventListener: () => {},
    removeEventListener: () => {},
  });
}
const doc = window.document;

const mcpState = {
  installed: true,
  command: "D:\\mcc\\mcc-mcp.exe",
  apps_value: "",
  tools: ["media_status", "generate_image"],
  tool_bytes: 2048,
  rails: [{ rail: "image", label: "Image", models: ["p1/i0"], tools: ["generate_image"] }],
  desktop_files: [],
  copy_line: '"D:\\mcc\\mcc-mcp.exe" --app <your-app>',
  copy_json: '{"mcc_media":{"command":"D:\\\\mcc\\\\mcc-mcp.exe"}}',
  permanent_targets: [
    {
      target_id: "claude",
      display_name: "Claude Code",
      supported: true,
      document_path: "/home/user/.claude.json",
      display_path: "~/.claude.json",
      backup_exists: false,
      backup_path: "",
      shared_with: [],
      state: "absent",
      restorable: false,
      reason: "",
    },
    {
      target_id: "gemini_cli",
      display_name: "Gemini CLI",
      supported: true,
      document_path: "/home/user/.gemini/settings.json",
      display_path: "~/.gemini/settings.json",
      backup_exists: false,
      backup_path: "",
      shared_with: [],
      state: "foreign",
      restorable: false,
      reason: "Foreign entry detected.",
    },
  ],
  apps: [
    {
      app_id: "claude",
      display_name: "Claude Code",
      support: "button",
      on: false,
      mode: "off",
      launch_supported: true,
      launch_on: false,
      permanent_supported: true,
      permanent_target_id: "claude",
      permanent_state: "absent",
      permanent_path: "/home/user/.claude.json",
      permanent_display_path: "~/.claude.json",
      permanent_backup_exists: false,
      permanent_backup_path: "",
      permanent_shared_with: [],
      permanent_reason: "",
      restorable: false,
      conflict_warning: "",
      supported_modes: ["off", "launch", "permanent"],
      asks: "",
      note: "",
      pending: "",
    },
    {
      app_id: "gemini_cli",
      display_name: "Gemini CLI",
      support: "own_file_later",
      on: false,
      mode: "off",
      launch_supported: false,
      launch_on: false,
      permanent_supported: true,
      permanent_target_id: "gemini_cli",
      permanent_state: "foreign",
      permanent_path: "/home/user/.gemini/settings.json",
      permanent_display_path: "~/.gemini/settings.json",
      permanent_backup_exists: false,
      permanent_backup_path: "",
      permanent_shared_with: [],
      permanent_reason: "",
      restorable: false,
      conflict_warning: "Your config file already defines an mcc_media entry that MCC did not add.",
      supported_modes: ["off", "permanent"],
      asks: "",
      note: "",
      pending: "",
    },
  ],
};

const skillsState = {
  skill: { name: "mcc-media", version: "8.74.0", files: ["SKILL.md", "scripts/mcc_media.py"], total_bytes: 4096 },
  targets: [
    {
      id: "shared_agents",
      display_name: "Shared Agent Skills",
      summary: "Shared skill dir",
      display_path: "~/.agents/skills/mcc-media",
      harness_ids: ["codex", "opencode"],
      shared_with: [],
      aliases_shared_agents: true,
      also_reads_shared_agents: false,
      install_dir: "/home/user/.agents/skills/mcc-media",
      state: "not_installed",
      installed: false,
      update_available: false,
      drifted: false,
      foreign: false,
      revertable: false,
      backup_dir: null,
      installed_version: null,
      warning: "",
    },
    {
      id: "claude",
      display_name: "Claude Code",
      summary: "Claude skills",
      display_path: "~/.claude/skills/mcc-media",
      harness_ids: ["claude"],
      shared_with: [],
      aliases_shared_agents: false,
      also_reads_shared_agents: true,
      install_dir: "/home/user/.claude/skills/mcc-media",
      state: "foreign",
      installed: false,
      update_available: false,
      drifted: false,
      foreign: true,
      revertable: false,
      backup_dir: null,
      installed_version: null,
      warning: "Existing unowned files present.",
    },
  ],
};

const calls = [];
window.fetch = async (url, options = {}) => {
  const pathOnly = String(url).split("?")[0];
  const method = (options.method || "GET").toUpperCase();
  const bodyObj = options.body ? JSON.parse(options.body) : null;
  calls.push({ method, path: pathOnly, body: bodyObj });

  let status = 200;
  let resp = {};
  if (pathOnly === "/admin/api/config") {
    resp = {
      fields: [{ key: "MCP_MEDIA_APPS", section: "mcp_media", label: "MCP media apps", type: "text", default: "", value: mcpState.apps_value }],
      sections: [{ id: "mcp_media", title: "Media MCP", view: "coding_agents" }],
      provider_status: [],
      paths: { managed: "/tmp/.env" },
      route_tier_aliases: {},
    };
  } else if (pathOnly === "/admin/api/version") {
    resp = { current: "8.74.0" };
  } else if (pathOnly === "/admin/api/onboarding") {
    resp = { dismissed: true, complete: true, steps: [] };
  } else if (pathOnly === "/admin/api/harnesses") {
    resp = {
      harnesses: [
        { id: "claude", display_name: "Claude Code", binary: "claude", installed: true, binary_path: "/usr/bin/claude", command: "mcc-claude", commands: ["mcc-claude"], command_lines: [], protocol: "anthropic_messages", protocol_label: "Anthropic", summary: "Claude", rtk_agent: true },
        { id: "gemini_cli", display_name: "Gemini CLI", binary: "gemini", installed: true, binary_path: "/usr/bin/gemini", command: "mcc-gemini", commands: ["mcc-gemini"], command_lines: [], protocol: "gemini", protocol_label: "Gemini", summary: "Gemini", rtk_agent: false },
      ],
      not_yet: [],
    };
  } else if (pathOnly === "/admin/api/harness-tiers") {
    resp = { tiers: [], harnesses: {} };
  } else if (pathOnly === "/admin/api/harness-surfaces") {
    resp = { providers: [], harnesses: {}, sdk_harnesses: [] };
  } else if (pathOnly === "/admin/api/desktop-apps") {
    resp = { apps: [] };
  } else if (pathOnly === "/admin/api/agent-installs") {
    resp = { agents: [], job: null };
  } else if (pathOnly === "/admin/api/rtk") {
    resp = { installed: false, agents: {} };
  } else if (pathOnly === "/admin/api/mcp-media") {
    resp = mcpState;
  } else if (pathOnly === "/admin/api/skills") {
    resp = skillsState;
  } else if (pathOnly === "/admin/api/mcp-media/permanent/claude/plan") {
    resp = {
      target_id: "claude",
      add: Boolean(bodyObj.add),
      mode: bodyObj.mode || "keys_only",
      document_path: "/home/user/.claude.json",
      backup_path: "/home/user/.claude.json.mcc-backup",
      backup_exists: !bodyObj.add,
      will_create_file: Boolean(bodyObj.add),
      removes_file: false,
      overwrites: [],
      restorable: mcpState.apps[0].restorable,
      no_op: false,
      diff: bodyObj.add ? '+ "mcc_media": { "command": "mcc-mcp" }' : '- "mcc_media": { "command": "mcc-mcp" }',
      ask_keys: [],
    };
  } else if (pathOnly === "/admin/api/mcp-media/permanent/gemini_cli/plan") {
    status = 409;
    resp = { detail: "Foreign mcc_media entry already exists; MCC will not overwrite it." };
  } else if (pathOnly === "/admin/api/mcp-media/mode") {
    const app = mcpState.apps.find((a) => a.app_id === bodyObj.app);
    if (bodyObj.mode === "launch") {
      app.on = true;
      app.launch_on = true;
      app.permanent_state = "absent";
      app.mode = "launch";
      mcpState.apps_value = app.app_id;
    } else if (bodyObj.mode === "permanent") {
      app.on = false;
      app.launch_on = false;
      app.permanent_state = "configured";
      app.restorable = true;
      app.permanent_backup_exists = true;
      app.permanent_backup_path = "/home/user/.claude.json.mcc-backup";
      app.mode = "permanent";
      mcpState.apps_value = "";
    } else {
      app.on = false;
      app.launch_on = false;
      app.permanent_state = "absent";
      app.restorable = false;
      app.mode = "off";
      mcpState.apps_value = "";
    }
    resp = { ...mcpState, message: `Updated ${app.app_id} to ${app.mode}.` };
  } else if (pathOnly.startsWith("/admin/api/skills/") && pathOnly.endsWith("/plan")) {
    const targetId = pathOnly.split("/")[4];
    const target = skillsState.targets.find((t) => t.id === targetId);
    const blocked = target.foreign && !bodyObj.force && !bodyObj.adopt && bodyObj.action !== "remove";
    resp = {
      target_id: targetId,
      action: bodyObj.action,
      install_dir: target.install_dir,
      state: target.state,
      files_to_create: bodyObj.action === "remove" ? [] : ["SKILL.md", "scripts/mcc_media.py"],
      files_to_update: [],
      files_to_delete: bodyObj.action === "remove" ? ["SKILL.md"] : [],
      backup_dir: "/home/user/.mcc-backup",
      no_op: blocked,
      warning: blocked ? "Foreign files present; choose force or adopt." : "",
    };
  } else if (pathOnly.startsWith("/admin/api/skills/") && pathOnly.endsWith("/apply")) {
    const targetId = pathOnly.split("/")[4];
    const target = skillsState.targets.find((t) => t.id === targetId);
    target.installed = bodyObj.action !== "remove";
    target.state = bodyObj.action === "remove" ? "not_installed" : "installed";
    target.foreign = false;
    target.warning = "";
    target.installed_version = bodyObj.action === "remove" ? null : "8.74.0";
    resp = { changed: true, install_dir: target.install_dir, target };
  }

  return {
    ok: status < 400,
    status,
    json: async () => resp,
  };
};

const scriptErrors = [];
window.addEventListener("error", (ev) => scriptErrors.push(ev.message));
window.eval(js);

const settle = () => new Promise((r) => setTimeout(r, 40));
await settle();
const codingNav = Array.from(doc.querySelectorAll("[data-view]")).find((el) => el.dataset.view === "coding_agents");
codingNav.click();
await settle();
await settle();

// 1. Verify initial 3-state MCP mode controls & permanent-only control on Gemini CLI.
const claudeButtons = Array.from(doc.querySelectorAll('.mcp-mode-btn[data-mcp-app="claude"]')).map((b) => ({
  mode: b.dataset.mcpMode,
  active: b.classList.contains("active"),
}));
const geminiButtons = Array.from(doc.querySelectorAll('.mcp-mode-btn[data-mcp-app="gemini_cli"]')).map((b) => b.dataset.mcpMode);

// 2. Click "Launch only" on claude (no modal needed when permanent is off).
doc.querySelector('.mcp-mode-btn[data-mcp-app="claude"][data-mcp-mode="launch"]').click();
await settle();
const afterLaunchActive = doc.querySelector('.mcp-mode-btn[data-mcp-app="claude"][data-mcp-mode="launch"]').classList.contains("active");

// 3. Click "Permanent (user config)" on claude -> opens #mcpModeDiffModal -> confirm.
doc.querySelector('.mcp-mode-btn[data-mcp-app="claude"][data-mcp-mode="permanent"]').click();
await settle();
const mcpModalOpen = !doc.getElementById("mcpModeDiffModal").hidden;
const mcpDiffText = doc.getElementById("mcpModeDiffBody").textContent;
doc.getElementById("mcpModeDiffConfirm").click();
await settle();
const mcpModalClosed = doc.getElementById("mcpModeDiffModal").hidden;
const afterPermActive = doc.querySelector('.mcp-mode-btn[data-mcp-app="claude"][data-mcp-mode="permanent"]').classList.contains("active");

// 4. Click "Remove & Restore" -> verify restore checkbox checked -> confirm.
doc.querySelector('[data-role="mcp-mode-remove-restore"][data-mcp-app="claude"]').click();
await settle();
const restoreChecked = doc.getElementById("mcpModeDiffRestoreCheck").checked;
doc.getElementById("mcpModeDiffConfirm").click();
await settle();
const afterRemoveActive = doc.querySelector('.mcp-mode-btn[data-mcp-app="claude"][data-mcp-mode="off"]').classList.contains("active");

// 5. Foreign Gemini CLI -> clicking Permanent shows 409 warning and keeps confirm disabled.
doc.querySelector('.mcp-mode-btn[data-mcp-app="gemini_cli"][data-mcp-mode="permanent"]').click();
await settle();
const geminiConfirmDisabled = doc.getElementById("mcpModeDiffConfirm").disabled;
doc.getElementById("mcpModeDiffClose").click();
await settle();

// 6. Shared skill panel install via #skillPlanModal + foreign adopt on claude.
const sharedInstallBtn = doc.querySelector('.skill-media-panel [data-skill-target="shared_agents"][data-skill-action="install"]');
sharedInstallBtn.click();
await settle();
const skillModalOpen = !doc.getElementById("skillPlanModal").hidden;
doc.getElementById("skillPlanConfirm").click();
await settle();
const sharedInstalled = Boolean(doc.querySelector('.skill-media-panel [data-skill-target="shared_agents"][data-skill-action="remove"]'));

// Foreign claude skill blocks confirm until adopt checked.
doc.querySelector('[data-skill-app="claude"] [data-skill-action="install"]').click();
await settle();
const blockedInitially = doc.getElementById("skillPlanConfirm").disabled;
doc.getElementById("skillPlanAdoptCheck").checked = true;
doc.getElementById("skillPlanAdoptCheck").dispatchEvent(new window.Event("change"));
await settle();
const unblockedAfterAdopt = !doc.getElementById("skillPlanConfirm").disabled;
doc.getElementById("skillPlanConfirm").click();
await settle();

// Escape closes any open dialog cleanly.
doc.querySelector('[data-skill-app="claude"] [data-skill-action="remove"]').click();
await settle();
const openBeforeEsc = !doc.getElementById("skillPlanModal").hidden;
doc.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
await settle();
const closedAfterEsc = doc.getElementById("skillPlanModal").hidden;

console.log(JSON.stringify({
  scriptErrors,
  claudeButtons,
  geminiButtons,
  afterLaunchActive,
  mcpModalOpen,
  mcpDiffText,
  mcpModalClosed,
  afterPermActive,
  restoreChecked,
  afterRemoveActive,
  geminiConfirmDisabled,
  skillModalOpen,
  sharedInstalled,
  blockedInitially,
  unblockedAfterAdopt,
  openBeforeEsc,
  closedAfterEsc,
  modeCalls: calls.filter((c) => c.path === "/admin/api/mcp-media/mode").map((c) => c.body),
}));
dom.window.close();
process.exit(0);
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".mjs", encoding="utf-8", delete=False
    ) as handle:
        handle.write(script)
        script_path = Path(handle.name)
    try:
        result = subprocess.run(
            [node, str(script_path), str(STATIC_DIR), str(NODE_MODULES)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        if "Cannot find package 'jsdom'" in result.stderr:
            if ON_CI:
                pytest.fail(f"MCC_CI=1 and jsdom missing: {result.stderr}")
            pytest.skip("jsdom is not installed")
        pytest.fail(f"jsdom scenario failed:\n{result.stderr}")
    return json.loads(result.stdout)


def test_coding_agents_mcp_modes_and_skill_ui_jsdom() -> None:
    out = _run_pr39_jsdom_scenario()
    assert out["scriptErrors"] == []
    assert [b["mode"] for b in out["claudeButtons"]] == ["off", "launch", "permanent"]
    assert out["claudeButtons"][0]["active"] is True
    assert out["geminiButtons"] == ["off", "permanent"]
    assert out["afterLaunchActive"] is True
    assert out["mcpModalOpen"] is True
    assert "mcc_media" in out["mcpDiffText"]
    assert out["mcpModalClosed"] is True
    assert out["afterPermActive"] is True
    assert out["restoreChecked"] is True
    assert out["afterRemoveActive"] is True
    assert out["geminiConfirmDisabled"] is True
    assert out["skillModalOpen"] is True
    assert out["sharedInstalled"] is True
    assert out["blockedInitially"] is True
    assert out["unblockedAfterAdopt"] is True
    assert out["openBeforeEsc"] is True
    assert out["closedAfterEsc"] is True
    assert out["modeCalls"] == [
        {"app": "claude", "mode": "launch", "undo_mode": "keys_only"},
        {"app": "claude", "mode": "permanent", "undo_mode": "keys_only"},
        {"app": "claude", "mode": "off", "undo_mode": "restore"},
    ]
