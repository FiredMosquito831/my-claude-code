# @firedmosquito831/my-claude-code

Route Claude Code and other coding agents to any model provider through one
local proxy, with a dashboard for routing, fallback chains, reasoning controls,
credential rotation and request analytics.

The server is a Python package. This npm package is a thin wrapper over the
project's own digest-verified install script — it does not reimplement it and
it does not vendor a second copy of the server.

## Install everything (server, every command, desktop app)

```sh
npm install -g @firedmosquito831/my-claude-code
```

A global install runs the official installer with the desktop flag
(`install.ps1 -Desktop` on Windows, `install.sh --desktop` elsewhere). When it
finishes you have exactly what the one-line installer gives you: the server,
every `mcc-*` command on your PATH, and the desktop app — plus `my-claude-code`
and `mcc` as extra aliases from this package. If the installer fails, `npm
install -g` fails with it; nothing is left half-installed.

Skip that and take the launcher only with `MCC_NPM_SKIP_INSTALL=1 npm install -g
@firedmosquito831/my-claude-code`. A local install, an `npx` run, `CI` being set,
and `--ignore-scripts` all skip it too, each printing one line saying so.

## Run without installing globally

```sh
npx @firedmosquito831/my-claude-code            # install if needed, then start the server
npx @firedmosquito831/my-claude-code install    # install or update only
npx @firedmosquito831/my-claude-code desktop    # start the desktop window
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

Requirements: Node 18+, and on Windows PowerShell 5.1+ (the installer brings
`uv` and Python itself).

Docs, releases and the desktop installers for Windows, Linux and macOS:
https://github.com/FiredMosquito831/my-claude-code

License: AGPL-3.0-or-later (commercial license available, see the repository).
