# @firedmosquito831/my-claude-code

Route Claude Code and other coding agents to any model provider through one
local proxy, with a dashboard for routing, fallback chains, reasoning controls,
credential rotation and request analytics.

The server is a Python package. This npm package is a thin wrapper over the
project's own digest-verified install script — it does not reimplement it and
it does not vendor a second copy of the server.

## Install what this machine actually needs

```sh
npm install -g @firedmosquito831/my-claude-code
```

A global install looks at the machine first and then installs the latest
release of the right shape:

- **the server, always**, through the official installer (`install.ps1` /
  `install.sh`), which brings `uv`, Python 3.14 and every `mcc-*` command;
- **the native desktop application, where there is a desktop session** —
  Windows and macOS always, Linux only with `DISPLAY` or `WAYLAND_DISPLAY`.
  Over SSH, in CI, on a headless server or in WSL without a display it
  installs the server alone, without even the installer's desktop flag, so no
  shortcuts are created for a screen that does not exist.

It prints one line saying what it decided and why before it does anything.

| Platform | What the desktop half installs |
| --- | --- |
| Windows | `MyClaudeCode-Setup-windows-x86_64.exe` run `/VERYSILENT`, per-user, no UAC prompt |
| macOS | the universal `.dmg`, mounted with `hdiutil`, copied into `~/Applications`, quarantine cleared (it says so) |
| Linux with `dpkg` | the `.deb`, downloaded and verified — it **prints** `sudo dpkg -i …` rather than escalating, unless you pass `--yes-sudo` |
| Linux without `dpkg` | the tarball's own `install-desktop.sh`, per-user, no root |

Every one of those downloads is checked against
`SHA256SUMS-desktop-shell.txt` from the same release **before** it is run,
mounted or unpacked; a file whose digest does not match is deleted and nothing
is executed.

This is the OS-native shape on purpose. `mcc-desktop` also downloads the same
verified Tauri shell on first launch, into your config home — that needs no
installer and no root, but leaves no Start Menu entry, `.desktop` file or
Applications icon. Typing `npm install -g` asks for an installed application,
so that is what you get; `mcc-desktop` stays as the zero-install fallback.

### Choosing for yourself

```sh
npx @firedmosquito831/my-claude-code install --help   # the list, installs nothing
npx @firedmosquito831/my-claude-code install --server-only
npx @firedmosquito831/my-claude-code install --desktop-only
npx @firedmosquito831/my-claude-code install --yes-sudo     # Linux: run dpkg for me
MCC_NPM_INSTALL=server npm install -g @firedmosquito831/my-claude-code
```

`MCC_NPM_INSTALL` takes `server`, `desktop`, `both` or `none` and is the
channel for Dockerfiles and provisioning scripts; a command-line flag beats it.
Skip the hook entirely and take the launcher only with
`MCC_NPM_SKIP_INSTALL=1 npm install -g @firedmosquito831/my-claude-code`. A
local install, an `npx` run, `CI` being set, and `--ignore-scripts` all skip it
too, each printing one line saying so.

If the server installs and the desktop half fails — a flaky network, a 404 —
the install stays green and tells you how to retry just that half. A failing
*server* install fails `npm install -g` with it; nothing is left
half-installed.

## Run without installing globally

```sh
npx @firedmosquito831/my-claude-code            # install if needed, then start the server
npx @firedmosquito831/my-claude-code install    # install or update, deciding as above
npx @firedmosquito831/my-claude-code desktop    # open the desktop app, installing it first if missing
npx @firedmosquito831/my-claude-code claude     # launch Claude Code through the proxy
npx @firedmosquito831/my-claude-code help       # list every mcc-* command
```

`npx … --version` installs nothing: the launcher installs the server only when
you ask it to run something that needs one.

## Uninstall

```sh
mcc uninstall          # removes the server, its commands and the config home
npm uninstall -g @firedmosquito831/my-claude-code   # removes only this launcher
```

Run `mcc uninstall` **first**: npm removes the launcher it installed and nothing
else, so uninstalling the package on its own leaves the Python server in place.
The desktop application is uninstalled the way its platform expects — Add or
remove programs on Windows, deleting it from `~/Applications` on macOS,
`sudo dpkg -r my-claude-code-desktop` or `install-desktop.sh --uninstall` on
Linux.

Requirements: Node 18+, and on Windows PowerShell 5.1+ (the installer brings
`uv` and Python itself).

Docs, releases and the desktop installers for Windows, Linux and macOS:
https://github.com/FiredMosquito831/my-claude-code

License: AGPL-3.0-or-later (commercial license available, see the repository).
