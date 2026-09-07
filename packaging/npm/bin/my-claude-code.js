#!/usr/bin/env node
"use strict";

// my-claude-code on npm is a launcher, not the server. The server is a Python
// package installed with uv from the GitHub release (digest-verified by the
// official install script). This file installs it when it is missing and then
// hands over to the real command with every argument untouched.
//
//   npx my-claude-code            -> install if needed, then run mcc-server
//   npx @firedmosquito831/my-claude-code install    -> install or update only
//   npx my-claude-code desktop    -> install if needed, then run mcc-desktop
//   npx my-claude-code <mcc-cmd>  -> any mcc-* command, e.g. `claude`, `help`
//   npx my-claude-code --version  -> versions of this launcher and the server

const { spawnSync } = require("node:child_process");
const os = require("node:os");
const path = require("node:path");

const LAUNCHER_VERSION = require("../package.json").version;
const REPO_RAW = "https://raw.githubusercontent.com/FiredMosquito831/my-claude-code/main/scripts";
const IS_WINDOWS = process.platform === "win32";

// Every published command lives in uv's tool bin dir. PATH may not carry it in
// the shell that runs npx, so look there explicitly before giving up.
function candidateBinDirs() {
  const home = os.homedir();
  const dirs = [path.join(home, ".local", "bin")];
  if (IS_WINDOWS) {
    const appData = process.env.APPDATA;
    if (appData) dirs.push(path.join(appData, "uv", "tools", "my-claude-code", "Scripts"));
  }
  return dirs;
}

function resolveCommand(name) {
  const exe = IS_WINDOWS ? `${name}.exe` : name;
  const probe = spawnSync(IS_WINDOWS ? "where" : "which", [exe], { encoding: "utf8" });
  if (probe.status === 0 && probe.stdout.trim()) {
    return probe.stdout.trim().split(/\r?\n/)[0];
  }
  const fs = require("node:fs");
  for (const dir of candidateBinDirs()) {
    const full = path.join(dir, exe);
    if (fs.existsSync(full)) return full;
  }
  return null;
}

function runInstaller() {
  console.error("my-claude-code: server not found, running the official installer...");
  let result;
  if (IS_WINDOWS) {
    const script = `& ([scriptblock]::Create((irm "${REPO_RAW}/install.ps1")))`;
    result = spawnSync("powershell", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script], {
      stdio: "inherit",
    });
  } else {
    result = spawnSync("sh", ["-c", `curl -fsSL "${REPO_RAW}/install.sh" | sh`], { stdio: "inherit" });
  }
  if (result.error) {
    console.error(`my-claude-code: could not start the installer: ${result.error.message}`);
    return 1;
  }
  return result.status ?? 1;
}

function runUninstaller() {
  console.error("my-claude-code: running the official uninstaller (removes the server, its commands and the config home)...");
  let result;
  if (IS_WINDOWS) {
    const script = `& ([scriptblock]::Create((irm "${REPO_RAW}/uninstall.ps1")))`;
    result = spawnSync("powershell", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script], {
      stdio: "inherit",
    });
  } else {
    result = spawnSync("sh", ["-c", `curl -fsSL "${REPO_RAW}/uninstall.sh" | sh`], { stdio: "inherit" });
  }
  if (result.error) {
    console.error(`my-claude-code: could not start the uninstaller: ${result.error.message}`);
    return 1;
  }
  return result.status ?? 1;
}

const USAGE = `my-claude-code ${LAUNCHER_VERSION} -- launcher for the MCC proxy server and desktop app

  my-claude-code              install the server if missing, then start it (mcc-server)
  my-claude-code desktop      install if missing, then open the desktop app (mcc-desktop
                              downloads the verified desktop shell on first launch)
  my-claude-code install      install or update the server from the official installer
  my-claude-code uninstall    remove the server, its commands and the config home
  my-claude-code <cmd> ...    run any mcc-* command, e.g. claude, codex, opencode, help
  my-claude-code --version    launcher and server versions

Everything else is passed to mcc-server unchanged. Installs use the digest-verified
scripts from https://github.com/FiredMosquito831/my-claude-code`;

function exec(command, args) {
  const result = spawnSync(command, args, { stdio: "inherit" });
  if (result.error) {
    console.error(`my-claude-code: could not run ${command}: ${result.error.message}`);
    return 1;
  }
  return result.status ?? 1;
}

function main() {
  const argv = process.argv.slice(2);
  const first = argv[0];

  if (first === "--version" || first === "-v") {
    console.log(`my-claude-code launcher ${LAUNCHER_VERSION}`);
    const server = resolveCommand("mcc-server");
    if (server) return exec(server, ["--version"]);
    console.log("server: not installed (run `npx @firedmosquito831/my-claude-code install`)");
    return 0;
  }

  if (first === "--help" || first === "-h" || first === "help" && !resolveCommand("mcc-help")) {
    console.log(USAGE);
    return 0;
  }

  if (first === "install" || first === "update") {
    return runInstaller();
  }

  if (first === "uninstall") {
    return runUninstaller();
  }

  // `desktop` -> mcc-desktop, `claude` -> mcc-claude, `help` -> mcc-help ...
  // Anything that is not a known mcc-* command is an argument for mcc-server.
  let target = "mcc-server";
  let rest = argv;
  if (first && !first.startsWith("-") && resolveCommand(`mcc-${first}`)) {
    target = `mcc-${first}`;
    rest = argv.slice(1);
  }

  let resolved = resolveCommand(target);
  if (!resolved) {
    const status = runInstaller();
    if (status !== 0) return status;
    resolved = resolveCommand(target);
    if (!resolved) {
      console.error(`my-claude-code: installed, but ${target} is still not on PATH. Open a new terminal and try again.`);
      return 1;
    }
  }
  return exec(resolved, rest);
}

process.exit(main());
