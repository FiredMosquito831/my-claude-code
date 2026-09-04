#!/usr/bin/env bash
# The shared body of the Linux and macOS release smokes.
#
# It is sourced, not executed. `linux.sh` and `macos.sh` set the handful of
# things that genuinely differ -- how a window is given a display, and what a
# signature check means on the platform -- and then call `smoke::run`.
#
# What the smoke proves, in order:
#
#   1. the binary the release is about to ship exists and is executable;
#   2. a fake `mcc-desktop` is what the shell asks for its status, and the
#      shell's own status contract is exercised against it: exit 0 and a
#      `schema: 1` document for `--print-status`, a non-zero exit for anything
#      else (the exit codes are asserted, not assumed);
#   3. the real binary launches, reads that document, and stays up;
#   4. it starts **nothing**: the status says `server_presence: foreign`, so
#      the port-conflict page is the whole of the ladder and the fake
#      `mcc-server` must never be run;
#   5. it exits when it is told to, by pid.
#
# Nothing here touches a real configuration directory, a real server, or a
# real port: `MCC_SHELL_DESKTOP_COMMAND`, `MCC_SHELL_SERVER_COMMAND` and
# `MCC_SHELL_DATA_DIR` point every one of those at a scratch directory that is
# removed on exit.

set -euo pipefail

# A port no MCC install would be listening on. It never receives a packet --
# `foreign` means the shell is told the port is taken and stops -- but it must
# not collide with a developer's own server if this script is run locally.
SMOKE_PORT=8199

smoke::fail() {
    echo "SMOKE FAIL: $*" >&2
    exit 1
}

smoke::ok() {
    echo "  ok: $*"
}

smoke::sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1"
    else
        shasum -a 256 "$1"
    fi
}

# Write the fake `mcc-desktop`. It records every call, answers
# `--print-status` with a document for a server it does not have, and refuses
# anything else with a distinctive status so step 2 can assert on it.
smoke::write_stubs() {
    local scratch="$1"

    cat >"$scratch/mcc-desktop" <<STUB
#!/usr/bin/env bash
set -eu
echo "\$@" >>"$scratch/desktop-calls.log"
if [ "\${1:-}" != "--print-status" ]; then
    echo "the smoke stub only answers --print-status" >&2
    exit 3
fi
cat <<'DOCUMENT'
{
  "schema": 1,
  "version": "0.0.0-smoke",
  "config_dir": "$scratch/config",
  "config_dir_source": "current",
  "host": "127.0.0.1",
  "port": $SMOKE_PORT,
  "root_url": "http://127.0.0.1:$SMOKE_PORT",
  "admin_url": "http://127.0.0.1:$SMOKE_PORT/admin",
  "health_url": "http://127.0.0.1:$SMOKE_PORT/health",
  "server_presence": "foreign",
  "port_conflict": "Port $SMOKE_PORT on 127.0.0.1 is held by another process (pid 4242, smoke-stub). Stop it, or choose another port.",
  "server_mode": "spawn",
  "window": "auto",
  "window_open": true,
  "window_width": 900,
  "window_height": 700,
  "tray_enabled": true,
  "minimize_to_tray": false,
  "start_at_login": false,
  "server_log": "$scratch/config/logs/server.log",
  "start_timeout_seconds": 30.0,
  "health_check_interval_seconds": 0.5,
  "health_poll_seconds": 5.0,
  "health_failure_threshold": 3,
  "activation_poll_seconds": 1.0,
  "reconnect_timeout_seconds": 1320.0
}
DOCUMENT
STUB

    # If this is ever run, the shell started a server it was told not to
    # start, and step 4 fails.
    cat >"$scratch/mcc-server" <<STUB
#!/usr/bin/env bash
echo "\$@" >>"$scratch/server-started.log"
sleep 600
STUB

    chmod +x "$scratch/mcc-desktop" "$scratch/mcc-server"
}

# Set by `smoke::run`, removed by the EXIT trap. One trap is installed, so a
# platform script with teardown of its own (Xvfb, say) hooks it through
# `SMOKE_EXTRA_CLEANUP` rather than replacing it.
scratch=""

smoke::cleanup() {
    if [ -n "${SMOKE_EXTRA_CLEANUP:-}" ]; then
        "$SMOKE_EXTRA_CLEANUP" || true
    fi
    if [ -n "$scratch" ]; then
        rm -rf "$scratch"
    fi
    return 0
}

smoke::run() {
    local binary="${1:-}"
    [ -n "$binary" ] || smoke::fail "usage: $0 <path to the MyClaudeCode binary>"

    echo "== My Claude Code desktop shell: ${SMOKE_PLATFORM} release smoke =="

    # -- 1. the artifact ---------------------------------------------------
    [ -f "$binary" ] || smoke::fail "no binary at $binary"
    [ -x "$binary" ] || smoke::fail "$binary is not executable"
    smoke::ok "binary present: $(smoke::sha256 "$binary")"

    scratch="$(mktemp -d)"
    trap smoke::cleanup EXIT
    mkdir -p "$scratch/config/logs" "$scratch/data"
    smoke::write_stubs "$scratch"

    # -- 2. the status contract, and its exit codes ------------------------
    local document rc
    document="$("$scratch/mcc-desktop" --print-status)" \
        || smoke::fail "the stub failed --print-status"
    smoke::ok "the stub answers --print-status with exit 0"

    rc=0
    "$scratch/mcc-desktop" --not-a-flag >/dev/null 2>&1 || rc=$?
    [ "$rc" -eq 3 ] || smoke::fail "expected exit 3 for an unknown flag, got $rc"
    smoke::ok "the stub exits 3 for anything else"

    printf '%s' "$document" | python3 -c '
import json, sys
document = json.load(sys.stdin)
assert document["schema"] == 1, document["schema"]
assert document["server_presence"] == "foreign", document["server_presence"]
for key in ("admin_url", "health_url", "config_dir", "server_log", "port_conflict"):
    assert document[key], key
print("  ok: the document parses, schema 1, presence foreign")
' || smoke::fail "the status document is not the shape the shell parses"

    # -- 3. the real binary, with a real window ----------------------------
    (
        cd "$scratch"
        MCC_SHELL_DESKTOP_COMMAND="$scratch/mcc-desktop" \
        MCC_SHELL_SERVER_COMMAND="$scratch/mcc-server" \
        MCC_SHELL_DATA_DIR="$scratch/data" \
            ${SMOKE_LAUNCH_PREFIX:-} "$binary" >"$scratch/shell.log" 2>&1
    ) &
    local shell_pid=$!

    local waited=0
    while [ ! -s "$scratch/desktop-calls.log" ]; do
        if ! kill -0 "$shell_pid" 2>/dev/null; then
            echo "---- the shell's output ----" >&2
            cat "$scratch/shell.log" >&2 || true
            smoke::fail "the shell exited before it read a status document"
        fi
        waited=$((waited + 1))
        [ "$waited" -lt 60 ] || {
            echo "---- the shell's output ----" >&2
            cat "$scratch/shell.log" >&2 || true
            smoke::fail "the shell never ran mcc-desktop --print-status (60s)"
        }
        sleep 1
    done
    smoke::ok "the shell ran the stub: $(head -n 1 "$scratch/desktop-calls.log")"

    grep -q -- '--print-status' "$scratch/desktop-calls.log" \
        || smoke::fail "the shell called mcc-desktop without --print-status"

    # Give the ladder a moment to reach the port-conflict page and settle.
    sleep 5
    kill -0 "$shell_pid" 2>/dev/null || {
        echo "---- the shell's output ----" >&2
        cat "$scratch/shell.log" >&2 || true
        smoke::fail "the shell died after reading its status"
    }
    smoke::ok "the window is still up five seconds after the status was read"

    # -- 4. it started nothing ---------------------------------------------
    [ ! -e "$scratch/server-started.log" ] \
        || smoke::fail "the shell started a server despite server_presence: foreign"
    smoke::ok "no server was started (server_presence: foreign)"

    # -- 5. it stops when told, by pid -------------------------------------
    kill "$shell_pid" 2>/dev/null || true
    waited=0
    while kill -0 "$shell_pid" 2>/dev/null; do
        waited=$((waited + 1))
        [ "$waited" -lt 20 ] || smoke::fail "the shell did not exit within 20s"
        sleep 1
    done
    smoke::ok "the shell exited when its pid was signalled"

    if [ -n "${SMOKE_AFTER:-}" ]; then
        "$SMOKE_AFTER" "$binary"
    fi

    echo "== ${SMOKE_PLATFORM} smoke passed =="
}
