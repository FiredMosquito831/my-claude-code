#!/usr/bin/env node
"use strict";

// `npm install -g @firedmosquito831/my-claude-code` must leave the machine in
// the state the one-line installer does -- the server, every `mcc-*` command,
// and, where there is a screen to draw on, the desktop application -- and not
// merely drop a launcher that installs on first use. That is what this hook is
// for, and it is deliberately the only place in this package that installs
// anything during `npm install`.
//
// It does NOT reimplement the installer. The server half runs the repository's
// own digest-verified script (`install.ps1 -Desktop`, `install.sh --desktop`),
// streams its output, and exits with its status. A failed server install
// therefore fails `npm install -g`, which is the honest outcome: npm should
// not report success for a package whose whole job is to put a server on your
// PATH.
//
// What is new since 6.57.1 is that the hook no longer assumes a desktop. It
// asks `runtime-install.decide()` -- platform, arch, DISPLAY/WAYLAND_DISPLAY,
// SSH, CI, and the explicit overrides -- and on a VPS, in a container or in a
// WSL shell with no display it installs the server WITHOUT the `-Desktop`
// flag, so no shortcuts are created for a screen that does not exist. Where
// there is a desktop it also installs the OS-native application (setup.exe,
// .dmg into ~/Applications, .deb/tarball), each verified against the release's
// own SHA256SUMS-desktop-shell.txt before it is run.
//
// Four situations must NOT install at all:
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

const runtime = require("./runtime-install.js");

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

async function main() {
  const reason = skipReason(process.env);
  if (reason !== null) {
    note(reason);
    return 0;
  }

  note(
    "global install -- deciding what this machine needs. Set MCC_NPM_SKIP_INSTALL=1 to skip this, " +
      "or MCC_NPM_INSTALL=server to take the server only."
  );

  // No argv: `npm install -g` passes none. Overrides here are the environment
  // variable, which is the only channel a package manager or Dockerfile has.
  const status = await runtime.performInstall({ argv: [], log: note });
  if (status === 0) {
    note("done. Open a new terminal so PATH picks up the mcc-* commands.");
  }
  return status;
}

main().then(
  (status) => {
    process.exitCode = status;
  },
  (error) => {
    console.error(`my-claude-code: the install hook failed: ${error && error.message}`);
    process.exitCode = 1;
  }
);
