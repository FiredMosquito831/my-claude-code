#!/usr/bin/env bash
# The macOS release smoke: a real window, driven by a fake `mcc-desktop`.
#
#     ./smoke/macos.sh src-tauri/target/aarch64-apple-darwin/release/MyClaudeCode
#
# macOS needs no virtual display: a runner has a window server and WKWebView
# is the system's own. What it does need, and what the other two platforms do
# not, is a signature check. On Apple silicon *all* code must carry at least
# an ad-hoc signature; the linker applies one, but a binary that is stripped,
# copied or archived carelessly can arrive with a broken seal, and a broken
# seal presents to the user as "the application is damaged" rather than as
# anything a log would explain. `codesign --verify` after the archive is
# therefore part of the release, not a nicety.
#
# No `.dmg` and no notarisation: decision Q7. The macOS artifact is a tarball
# that `mcc-desktop` fetches (Path A), and a file fetched by Python is never
# given the quarantine attribute a browser download would carry.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./_posix.sh
. "$here/_posix.sh"

SMOKE_PLATFORM="macOS ($(uname -m))"
SMOKE_LAUNCH_PREFIX=""

# Runs after the window has been exercised and stopped.
verify_signature() {
    local binary="$1"
    if ! codesign --verify --strict --verbose=2 "$binary" 2>&1; then
        if [ "$(uname -m)" = "arm64" ]; then
            smoke::fail "the ad-hoc signature does not verify on arm64; this \
binary would be reported as damaged rather than as unsigned"
        fi
        echo "  note: no signature on this x86_64 binary, which is expected \
and permitted -- only arm64 requires one"
        return 0
    fi
    smoke::ok "codesign --verify --strict passes"
}
SMOKE_AFTER=verify_signature

smoke::run "$@"
