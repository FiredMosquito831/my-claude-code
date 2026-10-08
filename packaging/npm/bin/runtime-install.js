"use strict";

// What `npm install -g` and `npx … install` decide, and how they carry it out.
//
// Both entry points face the same question: this machine is a Windows laptop,
// a Mac, a Linux desktop, a VPS reached over SSH, a CI runner or a WSL shell
// with no display -- which halves of MCC does it actually want? Getting that
// wrong is expensive in both directions. Installing a desktop app on a
// headless VPS leaves launcher shortcuts nobody can click and a download
// nobody asked for; installing only the server on a laptop makes `npm install
// -g` quietly worse than the one-line installer it wraps.
//
// So the decision is one pure function (`decide`) over platform, arch,
// environment and argv, it is testable without a network or an installer, and
// whoever runs it prints one line saying what it chose and why.
//
// The desktop half installs the OS-NATIVE shape: the Inno setup on Windows,
// the .dmg into ~/Applications on macOS, the .deb (or the tarball's own
// per-user installer) on Linux. That is deliberately not what `mcc-desktop`
// does -- `mcc-desktop` downloads the same Tauri shell into the config home on
// first launch, which needs no installer and no root but also leaves no Start
// Menu entry, no .desktop file and no Applications icon. Someone who typed
// `npm install -g` asked for an installed application, so that is what they
// get, and the config-home copy stays as the zero-install fallback.
//
// Every download is verified against `SHA256SUMS-desktop-shell.txt` from the
// same release before anything is run, mounted or unpacked. A file whose
// digest does not match is deleted, not quarantined: there is no case where
// keeping it helps and several where a later run picking it up hurts.

const childProcess = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const RELEASES = "https://github.com/FiredMosquito831/my-claude-code/releases/latest/download";
const SUMS_ASSET = "SHA256SUMS-desktop-shell.txt";

/** Release asset names, by `process.platform` and `process.arch`. */
const DESKTOP_ASSETS = {
  // The Windows shell is x86_64 only; an arm64 Windows machine runs it under
  // the OS's own emulation, which is why arm64 maps to the same file rather
  // than to "unsupported".
  win32: { x64: "MyClaudeCode-Setup-windows-x86_64.exe", arm64: "MyClaudeCode-Setup-windows-x86_64.exe" },
  // One universal dmg covers both Mac architectures.
  darwin: { x64: "MyClaudeCode-macos-universal.dmg", arm64: "MyClaudeCode-macos-universal.dmg" },
  // Linux ships x86_64 only. arm64 gets the server and an honest sentence.
  linux: { x64: { deb: "MyClaudeCode-linux-x86_64.deb", tarball: "MyClaudeCode-linux-x86_64.tar.gz" } },
};

const OVERRIDE_FLAGS = [
  "--server-only",
  "--desktop-only",
  "--no-desktop",
  "--yes-sudo",
  // Passed straight through to the official installer. Since 7.1.0 that
  // installer restarts the server by default and opens the desktop app, so a
  // provisioning script that must not touch a running server needs a way to
  // say so through this wrapper too. `MCC_INSTALL_NO_START=1` is the
  // environment form and reaches the installer through the inherited
  // environment without any help from here.
  "--no-restart",
  "--no-start",
  // 7.79.1: install for root even when this runs as root through sudo on
  // behalf of another user. Passed through to install.sh, which refuses that
  // case too; the environment form is MCC_INSTALL_ALLOW_ROOT=1.
  "--allow-root",
];

/**
 * The install.ps1 switch each installer flag becomes on Windows.
 *
 * The keys are the POSIX spellings install.sh accepts, because that is what
 * the caller writes and what the POSIX branch passes through untouched.
 *
 * `--no-desktop` means two different things on the two sides of this file and
 * the difference is deliberate: to THIS package it means "do not install the
 * desktop application"; to the official installer it means "do not START the
 * desktop app once the server is up". They meet in `decide()` below, where a
 * run that is not installing a desktop app also tells the installer not to
 * open one.
 */
const WINDOWS_SERVER_FLAGS = {
  "--no-restart": "-NoRestart",
  "--no-start": "-NoStart",
  "--no-desktop": "-NoDesktop",
};

const HELP = `my-claude-code install -- install the latest MCC for this machine

By default it installs the server (always) and, when this machine has a
desktop session, the desktop application in its native form: the setup.exe on
Windows, the .dmg into ~/Applications on macOS, the .deb (or the tarball's
per-user installer) on Linux. On a headless box, over SSH, in CI or in WSL
(display or not -- the desktop app for a Windows PC is the Windows one) it
installs the server only and says so.

My Claude Code installs per user. Run as root through sudo on behalf of
another user, it installs nothing and says how to install for that user.

  --server-only      install the server, never the desktop app
  --desktop-only     install the desktop app, leave the server alone
  --no-desktop       alias for --server-only
  --yes-sudo         on Linux, run \`sudo dpkg -i\` instead of printing it
  --no-restart       do not stop a My Claude Code server that is already
                     running. One is still started if nothing answers.
  --no-start         do not stop and do not start any server
  --allow-root       install for root even under sudo (MCC_INSTALL_ALLOW_ROOT=1)
  --help             this text

The official installer restarts the server on the configured port by default
since 7.1.0, and opens the desktop app when one is installed here and is not
already running. \`--no-restart\`, \`--no-start\` and \`MCC_INSTALL_NO_START=1\`
are the ways out; a run that is not installing a desktop app never opens one.

  MCC_NPM_INSTALL=server|desktop|both|none   the same choice as an environment
                     variable, for images and provisioning scripts

Every desktop download is checked against ${SUMS_ASSET} from
the same release before it is run, and deleted if the digest does not match.`;

/**
 * Does this machine have a desktop session to install an application into?
 *
 * Windows and macOS always do -- there is no headless variant of either that
 * npm can reach. Linux is the interesting case: a display server is the thing
 * that distinguishes a workstation from the VPS, container or WSL shell that
 * this same command runs in far more often. CI and SSH override everything:
 * both mean "no human is looking at this screen", whatever the platform says.
 */
function displayState(platform, env, wsl) {
  if (env.CI) return { desktop: false, why: "CI is set" };
  if (env.SSH_CONNECTION || env.SSH_TTY || env.SSH_CLIENT) {
    return { desktop: false, why: "this is an SSH session" };
  }
  if (platform === "win32") return { desktop: true, why: "Windows always has a session" };
  if (platform === "darwin") return { desktop: true, why: "macOS always has a session" };
  if (platform === "linux") {
    // 7.79.1: WSL is server only, display or not. WSLg sets DISPLAY and
    // WAYLAND_DISPLAY, so this used to install the LINUX desktop app inside
    // WSL -- a second, separate install on a PC whose desktop app is the
    // Windows one (and `mcc-desktop` refuses to run in WSL at all).
    if (wsl ?? isWsl(platform, env)) {
      return { desktop: false, why: "WSL, which gets the server only -- the desktop app for this PC is the Windows one" };
    }
    if (env.WAYLAND_DISPLAY) return { desktop: true, why: `WAYLAND_DISPLAY=${env.WAYLAND_DISPLAY}` };
    if (env.DISPLAY) return { desktop: true, why: `DISPLAY=${env.DISPLAY}` };
    return { desktop: false, why: "no DISPLAY or WAYLAND_DISPLAY" };
  }
  return { desktop: false, why: `${platform} has no desktop build` };
}

/** The kernel file the product reads to recognise WSL (config/paths.py). */
const WSL_OSRELEASE_PATH = "/proc/sys/kernel/osrelease";

/**
 * Whether this Linux is WSL, by the product's own tests: the kernel's release
 * string names Microsoft (config/paths.py, config/claude_discovery.py), or
 * WSL_DISTRO_NAME / WSL_INTEROP are set (the sign-in flows' test). sudo's
 * env_reset strips the two variables; it cannot strip the kernel string.
 */
function isWsl(platform, env) {
  if (platform !== "linux") return false;
  if (env.WSL_DISTRO_NAME || env.WSL_INTEROP) return true;
  try {
    return /microsoft/i.test(fs.readFileSync(WSL_OSRELEASE_PATH, "utf8"));
  } catch {
    return false;
  }
}

/** The overrides present in `argv`, and any argument that is not one. */
function parseFlags(argv) {
  const flags = new Set();
  const unknown = [];
  let help = false;
  for (const argument of argv) {
    if (argument === "--help" || argument === "-h") {
      help = true;
    } else if (OVERRIDE_FLAGS.includes(argument)) {
      flags.add(argument);
    } else {
      unknown.push(argument);
    }
  }
  return { flags, unknown, help };
}

/**
 * What to install, and the one line that explains it.
 *
 * Precedence, highest first: the command-line flags (the person is typing
 * right now), `MCC_NPM_INSTALL` (an image or a provisioning script decided
 * earlier), then the detected runtime. Nothing here touches the disk or the
 * network, which is what makes the whole matrix testable.
 */
function decide(options) {
  const platform = options.platform ?? process.platform;
  const arch = options.arch ?? process.arch;
  const env = options.env ?? process.env;
  const { flags, help, unknown } = parseFlags(options.argv ?? []);

  const state = displayState(platform, env);
  let server = true;
  let desktop = state.desktop;
  let why = state.why;
  let source = "detected";

  const mode = String(env.MCC_NPM_INSTALL ?? "").toLowerCase();
  if (mode) {
    if (!["server", "desktop", "both", "none"].includes(mode)) {
      return {
        error: `MCC_NPM_INSTALL=${env.MCC_NPM_INSTALL} is not one of server, desktop, both, none`,
        server: false,
        desktop: false,
        help,
        unknown,
      };
    }
    server = mode === "server" || mode === "both";
    desktop = mode === "desktop" || mode === "both";
    why = `MCC_NPM_INSTALL=${mode}`;
    source = "env";
  }

  if (flags.has("--desktop-only")) {
    server = false;
    desktop = true;
    why = "--desktop-only";
    source = "flag";
  } else if (flags.has("--server-only") || flags.has("--no-desktop")) {
    server = true;
    desktop = false;
    why = flags.has("--server-only") ? "--server-only" : "--no-desktop";
    source = "flag";
  }

  // A desktop this release has no asset for is not a decision anyone can act
  // on, so it degrades to the server rather than failing the install.
  // Only asked when a desktop app is actually on the cards: resolving the
  // Linux asset shells out to `command -v dpkg`, and a server-only install has
  // no business probing the package manager.
  const asset = desktop ? desktopAsset(platform, arch) : null;
  if (desktop && asset === null) {
    desktop = false;
    why = `${why}, but there is no desktop build for ${platform}/${arch}`;
    source = "unsupported";
  }

  const chose = !server && !desktop
    ? "nothing"
    : desktop && server
      ? "server + desktop app"
      : desktop
        ? "desktop app only"
        : "server only";
  // What this run tells the official installer about the server and the app.
  // Since 7.1.0 the installer restarts the server and opens the desktop app by
  // default; a run that is not installing a desktop app has no business
  // opening one, and a caller that said --no-restart/--no-start meant it.
  const serverFlags = [];
  if (flags.has("--no-start")) serverFlags.push("--no-start");
  else if (flags.has("--no-restart")) serverFlags.push("--no-restart");
  if (!desktop && !flags.has("--no-start")) serverFlags.push("--no-desktop");
  // install.sh's own sudo guard reads the same choice. install.ps1 has no
  // such guard (and no such switch), so Windows never passes it.
  if (flags.has("--allow-root") && platform !== "win32") serverFlags.push("--allow-root");

  return {
    server,
    desktop,
    serverFlags,
    allowRoot: flags.has("--allow-root"),
    // The server installer's `-Desktop`/`--desktop` flag adds the `mcc-desktop`
    // launcher and its shortcuts. A headless box has nowhere to put them.
    desktopFlag: desktop,
    sudo: flags.has("--yes-sudo"),
    asset,
    help,
    unknown,
    source,
    reason: `installing ${chose} on ${platform}/${arch} (${why})`,
  };
}

/** The release asset for this runtime, or null when there is no build. */
function desktopAsset(platform, arch) {
  const forPlatform = DESKTOP_ASSETS[platform];
  if (!forPlatform) return null;
  const entry = forPlatform[arch];
  if (!entry) return null;
  if (typeof entry === "string") return { kind: platform === "win32" ? "exe" : "dmg", name: entry };
  // Linux: the .deb when dpkg can install it, the tarball otherwise. `dpkg`
  // existing is the honest test -- a Fedora box has neither the command nor a
  // use for the file.
  const dpkg = childProcess.spawnSync("sh", ["-c", "command -v dpkg"], { encoding: "utf8" });
  if (dpkg.status === 0 && String(dpkg.stdout ?? "").trim()) {
    return { kind: "deb", name: entry.deb };
  }
  return { kind: "tarball", name: entry.tarball };
}

/** `releases/latest/download/<name>` -- no version in the URL, by design. */
function assetUrl(name) {
  return `${RELEASES}/${name}`;
}

async function fetchBuffer(url) {
  const response = await fetch(url, { redirect: "follow" });
  if (!response.ok) {
    throw new Error(`${url} -> HTTP ${response.status}`);
  }
  return Buffer.from(await response.arrayBuffer());
}

/** `{ filename: sha256 }` parsed from the `<64 hex><two spaces><name>` file. */
function parseSums(text) {
  const table = {};
  for (const line of text.split(/\r?\n/)) {
    const match = /^([0-9a-f]{64})\s\s(\S+)$/.exec(line.trim());
    if (match) table[match[2]] = match[1];
  }
  return table;
}

function sha256(buffer) {
  return crypto.createHash("sha256").update(buffer).digest("hex");
}

/**
 * Download one release asset into `directory`, verified against the release's
 * own checksum file, and return its path.
 *
 * The verification happens in memory, before a single byte reaches a path any
 * other process could execute. A mismatch deletes whatever was written and
 * throws: an unverified installer is not a degraded install, it is one that
 * must not happen.
 */
async function downloadVerified(name, directory, log) {
  const sumsText = (await fetchBuffer(assetUrl(SUMS_ASSET))).toString("utf8");
  const sums = parseSums(sumsText);
  const expected = sums[name];
  if (!expected) {
    throw new Error(`${SUMS_ASSET} on the latest release does not list ${name}`);
  }
  log(`downloading ${name} from the latest release...`);
  const payload = await fetchBuffer(assetUrl(name));
  const actual = sha256(payload);
  const target = path.join(directory, name);
  if (actual !== expected) {
    try {
      fs.rmSync(target, { force: true });
    } catch {
      // Nothing was written yet in the normal case; removing it is best effort.
    }
    throw new Error(
      `${name} failed its SHA-256 check (expected ${expected}, got ${actual}). Nothing was run and the file was deleted.`
    );
  }
  fs.mkdirSync(directory, { recursive: true });
  fs.writeFileSync(target, payload);
  log(`verified ${name} (sha256 ${actual})`);
  return target;
}

/** Windows: the Inno setup, silent and per-user, so nothing prompts for UAC. */
function windowsInstallArgv(installer, directory) {
  // `PrivilegesRequired=lowest` in the .iss means this never elevates.
  // /SUPPRESSMSGBOXES and /NORESTART matter because npm's postinstall has no
  // console to answer a dialog with. The .iss marks its [Run] entry
  // `skipifsilent`, so this installs the app without launching it.
  const argv = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"];
  if (directory) argv.push(`/DIR=${directory}`);
  return { command: installer, args: argv };
}

/**
 * The name this package used to publish, and must never publish again.
 *
 * `my-claude-code` is a console script of the WHEEL (`[project.scripts]`), and
 * on Windows npm's global bin directory precedes `~/.local/bin` on PATH. So an
 * `npm install -g` of 6.53.1 through 6.63.0 left `%APPDATA%\npm\my-claude-code.cmd`
 * sitting in front of the real launcher -- and `install.ps1` verified its
 * launchers by asking PATH, resolved npm's shim, concluded that a complete
 * install had put its files somewhere illegal, and threw. Permanently: every
 * later run of the one-liner failed the same way, on a machine where nothing
 * was wrong.
 *
 * 6.64.0 publishes one bin, `mcc`, which no wheel entry point claims. This
 * removes the leftover from the earlier versions, because npm does not
 * reliably reap a bin its package stopped declaring, and because the machines
 * that need it most are exactly the ones already broken.
 */
const STALE_BIN_NAME = "my-claude-code";
const PACKAGE_NAME = "@firedmosquito831/my-claude-code";

/** npm's global bin directory, or null when this is not a global install. */
function npmGlobalBinDirectory(env, platform) {
  const prefix = env.npm_config_global_prefix || env.npm_config_prefix;
  if (!prefix) return null;
  // On Windows the prefix IS the directory holding the shims; everywhere else
  // they are in bin/ under it.
  return platform === "win32" ? prefix : path.join(prefix, "bin");
}

/** Every shape npm writes a global bin in, for one name. */
function globalShimPaths(binDir, name, platform) {
  if (platform === "win32") {
    return [
      path.join(binDir, name),
      path.join(binDir, `${name}.cmd`),
      path.join(binDir, `${name}.ps1`),
    ];
  }
  return [path.join(binDir, name)];
}

/**
 * Is this file a shim npm wrote for THIS package?
 *
 * A symlink's target and a .cmd shim's text both name the package directory
 * they point into, so the check is the package name -- never the file name.
 * Deleting a `my-claude-code` that belongs to somebody else would be exactly
 * the kind of damage this whole change exists to stop.
 */
function isShimForThisPackage(file) {
  let text = null;
  try {
    const stat = fs.lstatSync(file);
    text = stat.isSymbolicLink() ? fs.readlinkSync(file) : fs.readFileSync(file, "utf8");
  } catch {
    return false;
  }
  if (typeof text !== "string") return false;
  return text.replace(/\\/g, "/").includes(PACKAGE_NAME);
}

/** Remove the `my-claude-code` shim an earlier version of this package left. */
function removeStaleGlobalShim(options) {
  const env = options.env ?? process.env;
  const platform = options.platform ?? process.platform;
  const log = options.log ?? ((message) => console.log(`my-claude-code: ${message}`));
  const binDir = options.binDir ?? npmGlobalBinDirectory(env, platform);
  if (!binDir) return [];

  const removed = [];
  for (const file of globalShimPaths(binDir, STALE_BIN_NAME, platform)) {
    if (!isShimForThisPackage(file)) continue;
    try {
      fs.rmSync(file, { force: true });
      removed.push(file);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      log(
        `could not remove the old ${STALE_BIN_NAME} command at ${file}: ${message}. ` +
          `Remove it by hand, or run \`npm uninstall -g ${PACKAGE_NAME}\` and install again.`
      );
    }
  }
  if (removed.length > 0) {
    log(
      `removed the old ${STALE_BIN_NAME} command this package used to publish (${removed.join(", ")}). ` +
        "It shadowed the launcher the installer provides, which is what made the installer refuse to verify itself."
    );
  }
  return removed;
}

/**
 * Where the installer's full output is kept for a hook that cannot show it,
 * or null when nothing here can be written.
 *
 * `os.tmpdir()` is a guess -- it reads TMPDIR/TEMP/TMP and falls back to the
 * system directory, and on a Windows runner with a stripped environment that
 * fallback was `C:\Windows\temp`, which did not exist. So the directory is
 * created and the path is probed HERE, where a failure is a missing log and
 * not a crashed install.
 */
function installerLogPath() {
  const candidate = path.join(os.tmpdir(), `mcc-install-${process.pid}.log`);
  try {
    fs.mkdirSync(path.dirname(candidate), { recursive: true });
    fs.writeFileSync(candidate, "");
    return candidate;
  } catch {
    return null;
  }
}

/**
 * Run the official installer, streaming its output into a log file.
 *
 * `npm install -g` swallows a postinstall's stdout, and on failure replays the
 * WHOLE captured stream under an `npm error` prefix -- which is how a fresh
 * machine came to read a page of ordinary uv progress as if every line were an
 * error. So the full text goes to a file, only the installer's own step lines
 * (`==> ...`) and its warnings reach the console, and the failure message
 * names both the file and `--foreground-scripts`.
 */
function runInstallerLogged(command, args, log, logPath) {
  return new Promise((resolve, reject) => {
    let stream = null;
    if (logPath) {
      try {
        stream = fs.createWriteStream(logPath, { flags: "a" });
        // A WriteStream reports a failed open ASYNCHRONOUSLY, as an 'error'
        // event -- and an unhandled 'error' on a stream throws out of the
        // event loop and kills the process. That is a log file taking an
        // install down with it, which is the opposite of the point.
        stream.on("error", () => {
          stream = null;
        });
      } catch {
        stream = null;
      }
    }
    const child = childProcess.spawn(command, args, { stdio: ["ignore", "pipe", "pipe"] });
    let pending = "";
    const consume = (chunk) => {
      const text = chunk.toString();
      if (stream) stream.write(text);
      pending += text;
      const lines = pending.split(/\r?\n/);
      pending = lines.pop() ?? "";
      for (const line of lines) {
        if (line.startsWith("==> ") || line.startsWith("WARNING:")) {
          log(line);
        }
      }
    };
    child.stdout.on("data", consume);
    child.stderr.on("data", consume);
    child.on("error", (error) => {
      if (stream) stream.end();
      reject(new Error(`could not run ${command}: ${error.message}`));
    });
    child.on("close", (code) => {
      if (stream) stream.end();
      resolve(code ?? 1);
    });
  });
}

function run(command, args, options) {
  const result = childProcess.spawnSync(command, args, { stdio: "inherit", ...options });
  if (result.error) throw new Error(`could not run ${command}: ${result.error.message}`);
  return result.status ?? 1;
}

/**
 * Install the downloaded desktop app for this platform.
 *
 * Returns 0 when the application is installed, and 0 with a printed command
 * when Linux needs a root the user did not offer -- an install that stopped to
 * ask is not a failure, and failing here would fail an `npm install -g` whose
 * server half already succeeded.
 */
function installDesktopFrom(file, decision, platform, log) {
  if (decision.asset.kind === "exe") {
    const { command, args } = windowsInstallArgv(file, process.env.MCC_NPM_DESKTOP_DIR);
    const status = run(command, args);
    if (status !== 0) throw new Error(`the desktop installer exited ${status}`);
    log("the desktop app is installed (Start Menu -> My Claude Code).");
    return 0;
  }

  if (decision.asset.kind === "dmg") {
    const applications = path.join(os.homedir(), "Applications");
    const mount = path.join(os.tmpdir(), `mcc-dmg-${process.pid}`);
    fs.mkdirSync(applications, { recursive: true });
    run("hdiutil", ["attach", "-nobrowse", "-quiet", "-mountpoint", mount, file]);
    try {
      const app = fs.readdirSync(mount).find((entry) => entry.endsWith(".app"));
      if (!app) throw new Error("the .dmg contains no .app bundle");
      const destination = path.join(applications, app);
      fs.rmSync(destination, { recursive: true, force: true });
      run("cp", ["-R", path.join(mount, app), destination]);
      // The shell is not notarised, so Gatekeeper would refuse the copy on
      // first open with a dialog that offers no way forward. Clearing the
      // quarantine bit here is the same trust decision the user already made
      // by running this installer -- but it is not one to make silently.
      run("xattr", ["-dr", "com.apple.quarantine", destination]);
      log(`cleared com.apple.quarantine on ${destination} so macOS will open the unsigned app.`);
    } finally {
      run("hdiutil", ["detach", "-quiet", mount]);
    }
    return 0;
  }

  if (decision.asset.kind === "deb") {
    if (!decision.sudo) {
      // Escalating without being asked is how a package manager earns a
      // reputation. The file is downloaded and verified; the last step is one
      // line the user can read before they type it.
      log(`the .deb is verified and ready. Finish the desktop app with:\n\n    sudo dpkg -i ${file}\n\n(or rerun with --yes-sudo). The server is installed either way.`);
      return 0;
    }
    const status = run("sudo", ["dpkg", "-i", file]);
    if (status !== 0) throw new Error(`sudo dpkg -i exited ${status}`);
    log("the desktop app is installed.");
    return 0;
  }

  // The tarball's own installer is per-user and needs no root at all.
  const staging = fs.mkdtempSync(path.join(os.tmpdir(), "mcc-desktop-"));
  run("tar", ["-xzf", file, "-C", staging]);
  const status = run("sh", [path.join(staging, "install-desktop.sh")]);
  if (status !== 0) throw new Error(`install-desktop.sh exited ${status}`);
  log("the desktop app is installed for this user (no root needed).");
  return 0;
}

const REPO_RAW = "https://raw.githubusercontent.com/FiredMosquito831/my-claude-code/main/scripts";

/**
 * The official install script for this platform, with the desktop flag only
 * when there is a desktop to put shortcuts on.
 *
 * This package has never carried a second copy of the installer and must not
 * start: the script it fetches is the one that verifies the release wheel's
 * digest, installs `uv` and provisions Python.
 */
function serverInstallerCommand(platform, withDesktop, script, serverFlags) {
  const name = script ?? "install";
  const extras = serverFlags ?? [];
  if (platform === "win32") {
    // The scriptblock form, not `irm … | iex`: only a scriptblock can bind
    // `-Desktop` to the script's own param() block.
    const windows = (withDesktop ? ["-Desktop"] : []).concat(
      extras.map((flag) => WINDOWS_SERVER_FLAGS[flag] ?? flag)
    );
    const flag = windows.length ? ` ${windows.join(" ")}` : "";
    return {
      command: "powershell",
      args: [
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        `& ([scriptblock]::Create((irm "${REPO_RAW}/${name}.ps1")))${flag}`,
      ],
    };
  }
  const posix = (withDesktop ? ["--desktop"] : []).concat(extras);
  const flag = posix.length ? ` -s -- ${posix.join(" ")}` : "";
  // 7.79.1: download first, then run -- never `curl ... | sh`. A pipeline
  // exits with its LAST command's status, and `sh` fed an empty script exits
  // 0, so a missing curl or a failed download was reported as a successful
  // install ("done", with nothing installed) and `mcc uninstall` as a
  // successful uninstall. Holding the whole script before running it also
  // means a download cut off half way is never executed. The WARNING: prefix
  // is what the npm hook's console filter lets through.
  const url = `${REPO_RAW}/${name}.sh`;
  const posixCommand =
    `command -v curl >/dev/null 2>&1 || { echo "WARNING: curl is not installed, and it is what downloads ${name}.sh. ` +
    `Install curl (for example: sudo apt install curl) and run this again." >&2; exit 127; }; ` +
    `script=$(curl -fsSL "${url}") || { status=$?; echo "WARNING: could not download ${url} (curl exit $status). ` +
    `Nothing was run. Check the network (and HTTPS_PROXY, if you use one) and run this again." >&2; exit $status; }; ` +
    `[ -n "$script" ] || { echo "WARNING: ${url} came back empty. Nothing was run." >&2; exit 1; }; ` +
    `printf '%s\\n' "$script" | sh${flag}`;
  return { command: "sh", args: ["-c", posixCommand] };
}

/**
 * The user this run acts for when it is root through sudo, or null.
 *
 * 7.79.1. My Claude Code is a per-user install (uv's tool directory,
 * ~/.local/bin, the config home), so `sudo npm install -g` -- which a system
 * Node needs only to write the `mcc` command into a root-owned prefix --
 * installed a second, root-owned copy for root and started a root server on
 * the user's port (specs/INVESTIGATION-NPM-SUDO.md). Only root acting FOR a
 * real user is refused: a root shell, a root VPS and a Docker image have no
 * SUDO_USER (or SUDO_USER=root) and are unaffected.
 */
function sudoInvoker(platform, env, uid, allowRoot) {
  if (platform === "win32" || uid !== 0) return null;
  const user = env.SUDO_USER;
  if (!user || user === "root") return null;
  if (allowRoot || env.MCC_INSTALL_ALLOW_ROOT === "1") return null;
  return user;
}

/** This process's uid, or -1 where there is none (Windows). */
function currentUid() {
  return typeof process.getuid === "function" ? process.getuid() : -1;
}

/** What a sudo run says instead of installing anything for root. */
function sudoRefusal(user) {
  return (
    `this is running as root through sudo, on behalf of ${user}, and My Claude Code installs per user. ` +
    `As root it would install a second, root-owned copy that ${user} cannot use or update, and start a root-owned ` +
    `server on ${user}'s port in place of ${user}'s own -- so nothing was installed for root. ` +
    `The \`mcc\` command itself is in place: run \`mcc install\` (or just \`mcc\`) as ${user}, without sudo. ` +
    `Next time, a user-level npm prefix needs no sudo at all: \`npm config set prefix ~/.npm-global\` ` +
    "(and add ~/.npm-global/bin to PATH), then `npm install -g @firedmosquito831/my-claude-code`. " +
    "To install for root on purpose: MCC_INSTALL_ALLOW_ROOT=1, or `mcc install --allow-root`."
  );
}

/**
 * Decide, say so in one line, then install what was decided.
 *
 * The desktop half is deliberately not allowed to fail the whole run when the
 * server half succeeded: someone who typed `npm install -g` on a laptop with a
 * flaky network still wants the server they now have, and the app is one
 * `npx … install --desktop-only` away. It does fail the run when the desktop
 * app was the only thing asked for.
 */
async function performInstall(options) {
  const log = options.log ?? ((message) => console.log(`my-claude-code: ${message}`));
  const platform = options.platform ?? process.platform;
  const decision = decide({ ...options, platform });

  if (decision.help) {
    console.log(HELP);
    return 0;
  }
  if (decision.error) {
    console.error(`my-claude-code: ${decision.error}`);
    return 1;
  }
  if (decision.unknown.length > 0) {
    console.error(
      `my-claude-code: unknown option ${decision.unknown[0]}. Run \`install --help\` for the list.`
    );
    return 1;
  }

  // The one line the spec asks for: what, where, and why.
  log(decision.reason);
  // 7.79.1: nothing at all for root acting for another user -- no server and
  // no desktop app. `npm install -g` passes `refusedStatus: 0`, so the `mcc`
  // command it wrote (the only thing sudo was needed for) stays; `mcc
  // install` and a bare `mcc` keep the default 1 and fail honestly.
  const invoker = sudoInvoker(
    platform,
    options.env ?? process.env,
    options.uid ?? currentUid(),
    decision.allowRoot
  );
  if (invoker) {
    log(sudoRefusal(invoker));
    if (options.onRefused) options.onRefused(invoker);
    return options.refusedStatus ?? 1;
  }
  if (!decision.server && !decision.desktop) {
    log("nothing to do (MCC_NPM_INSTALL=none).");
    return 0;
  }

  if (decision.server) {
    const { command, args } = serverInstallerCommand(
      platform,
      decision.desktopFlag,
      undefined,
      decision.serverFlags
    );
    const logPath = options.logPath ?? installerLogPath();
    log(
      logPath
        ? `installing the server; the installer's full output goes to ${logPath}`
        : "installing the server (no writable temporary directory, so the full output is not being kept)"
    );
    const status = await runInstallerLogged(command, args, log, logPath);
    if (status !== 0) {
      // Say which half landed. This used to claim "Nothing was left
      // half-installed by this hook" unconditionally -- and on the machine
      // that prompted this change the server WAS installed and the desktop app
      // was not, so the one sentence the user had was the false one.
      console.error(
        `my-claude-code: the server installer exited ${status}.\n` +
          `my-claude-code: the desktop app was not attempted. The server may be partly installed: check with \`mcc-server --version\`, ` +
          "and the installer names any command that is missing.\n" +
          (logPath
            ? `my-claude-code: the full output is in ${logPath}.\n`
            : "my-claude-code: the full output could not be kept (no writable temporary directory).\n") +
          "my-claude-code: retry the server with `npx @firedmosquito831/my-claude-code install --server-only`, the app with " +
          "`npx @firedmosquito831/my-claude-code install --desktop-only`, or rerun " +
          "`npm install -g --foreground-scripts @firedmosquito831/my-claude-code` to watch the installer live."
      );
      return status;
    }
  }

  if (decision.desktop) {
    // MCC_NPM_DOWNLOAD_DIR keeps the verified artefact somewhere the caller
    // chose, which is what makes a real download provable without a real
    // install; without it the file lands in a temp directory.
    const directory =
      options.downloadDir ??
      process.env.MCC_NPM_DOWNLOAD_DIR ??
      fs.mkdtempSync(path.join(os.tmpdir(), "mcc-npm-"));
    try {
      const file = await downloadVerified(decision.asset.name, directory, log);
      installDesktopFrom(file, decision, platform, log);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (!decision.server) {
        console.error(`my-claude-code: the desktop install failed: ${message}`);
        return 1;
      }
      console.error(
        `my-claude-code: the server is installed, but the desktop app is not: ${message}\n` +
          "my-claude-code: retry it on its own with `npx @firedmosquito831/my-claude-code install --desktop-only`, " +
          "or run `mcc-desktop`, which downloads the same verified shell into your config home."
      );
      return 0;
    }
  }

  return 0;
}

module.exports = {
  DESKTOP_ASSETS,
  PACKAGE_NAME,
  REPO_RAW,
  STALE_BIN_NAME,
  globalShimPaths,
  installerLogPath,
  isShimForThisPackage,
  npmGlobalBinDirectory,
  removeStaleGlobalShim,
  runInstallerLogged,
  performInstall,
  serverInstallerCommand,
  HELP,
  RELEASES,
  SUMS_ASSET,
  assetUrl,
  decide,
  desktopAsset,
  displayState,
  downloadVerified,
  installDesktopFrom,
  isWsl,
  parseFlags,
  parseSums,
  sha256,
  sudoInvoker,
  sudoRefusal,
  windowsInstallArgv,
  WSL_OSRELEASE_PATH,
};
