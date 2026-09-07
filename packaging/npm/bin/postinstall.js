#!/usr/bin/env node
"use strict";

// `npm install -g @firedmosquito831/my-claude-code` must leave the machine in
// the same state the one-line installer does -- the server, every `mcc-*`
// command, and the desktop app -- and not merely drop a launcher that installs
// on first use. That is what this hook is for, and it is deliberately the only
// place in this package that installs anything.
//
// It does NOT reimplement the installer. It runs the official, digest-verified
// script from the repository with the desktop flag the installer already has
// (`install.ps1 -Desktop`, `install.sh --desktop`), streams its output, and
// exits with its status. A failed install therefore fails `npm install -g`,
// which is the honest outcome: npm should not report success for a package
// whose whole job is to put a server on your PATH.
//
// Four situations must NOT install:
//
//   * a local `npm install` or an `npx` run (`npm_config_global` is not
//     "true"). `npx @firedmosquito831/my-claude-code --version` has to stay
//     cheap and must never touch the machine; the launcher installs on demand;
//   * `CI` is set -- a CI job that happens to depend on this package is not
//     asking for a machine-wide Python install;
//   * `MCC_NPM_SKIP_INSTALL=1`, the explicit opt-out for package managers,
//     Docker builds and anyone who wants the launcher only;
//   * `npm_config_ignore_scripts` -- npm normally suppresses the hook itself,
//     but a wrapper that sets the config without honouring it exists.
//
// Every one of them prints a single line saying what happened and why, then
// exits 0. Silence here is indistinguishable from a broken hook.

const { spawnSync } = require("node:child_process");

const REPO_RAW =
  "https://raw.githubusercontent.com/FiredMosquito831/my-claude-code/main/scripts";
const IS_WINDOWS = process.platform === "win32";

function note(message) {
  console.log(`my-claude-code: ${message}`);
}

/** Why this run must not install, or null when it should. */
function skipReason(env) {
  if (env.MCC_NPM_SKIP_INSTALL === "1") {
    return "MCC_NPM_SKIP_INSTALL=1 is set, so the server was not installed. Run `mcc install` when you want it.";
  }
  if (env.npm_config_ignore_scripts === "true") {
    return "install scripts are disabled (--ignore-scripts), so the server was not installed. Run `mcc install` when you want it.";
  }
  if (env.CI) {
    return "CI is set, so the server was not installed. Run `mcc install` (or unset CI) if a CI job really needs it.";
  }
  if (env.npm_config_global !== "true") {
    return "this is not a global install, so nothing was installed. `npx my-claude-code` installs the server on first use; `npm install -g @firedmosquito831/my-claude-code` installs it now.";
  }
  return null;
}

/** The installer command for this platform, with the desktop flag on. */
function installerCommand() {
  if (IS_WINDOWS) {
    // The scriptblock form, not `-File`: there is no file to point at when the
    // script is fetched over the network. `& ([scriptblock]::Create(...))
    // -Desktop` binds `-Desktop` to the script's own `param()` block, which a
    // plain `irm ... | iex` cannot do.
    return {
      command: "powershell",
      args: [
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        `& ([scriptblock]::Create((irm "${REPO_RAW}/install.ps1"))) -Desktop`,
      ],
    };
  }
  return {
    command: "sh",
    args: ["-c", `curl -fsSL "${REPO_RAW}/install.sh" | sh -s -- --desktop`],
  };
}

function main() {
  const reason = skipReason(process.env);
  if (reason !== null) {
    note(reason);
    return 0;
  }

  note(
    "global install -- running the official installer with the desktop app (`" +
      (IS_WINDOWS ? "install.ps1 -Desktop" : "install.sh --desktop") +
      "`). Set MCC_NPM_SKIP_INSTALL=1 to skip this."
  );

  const { command, args } = installerCommand();
  const result = spawnSync(command, args, { stdio: "inherit" });
  if (result.error) {
    console.error(
      `my-claude-code: could not start the installer: ${result.error.message}`
    );
    return 1;
  }
  const status = result.status ?? 1;
  if (status === 0) {
    note(
      "the server, every mcc-* command and the desktop app are installed. Open a new terminal so PATH picks them up."
    );
  } else {
    console.error(
      `my-claude-code: the installer exited ${status}. Nothing was left half-installed by this hook; rerun \`mcc install\` or install manually from https://github.com/FiredMosquito831/my-claude-code`
    );
  }
  return status;
}

process.exit(main());
