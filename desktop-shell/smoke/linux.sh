#!/usr/bin/env bash
# The Linux release smoke: a real window, on a real X display, driven by a
# fake `mcc-desktop`.
#
#     ./smoke/linux.sh src-tauri/target/release/MyClaudeCode
#
# Linux is the platform the shell is new work on -- `mcc-desktop` refuses to
# start here today because no Linux tray backend is packaged -- so this is the
# leg that most needs to run a real binary rather than a unit test.
#
# `Xvfb` is started here rather than through `xvfb-run` on purpose: the smoke
# stops the shell by its exact pid, and a wrapper script would put its own
# process in between. The two WEBKIT_* variables are the well-known pair that
# stop WebKitGTK reaching for a compositor and a DMA-BUF renderer a virtual
# framebuffer does not have. Without them the process aborts inside
# WebKitWebProcess and no window appears -- silently, which is exactly the
# failure mode this smoke exists to catch.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./_posix.sh
. "$here/_posix.sh"

SMOKE_PLATFORM="Linux"

command -v Xvfb >/dev/null 2>&1 \
    || smoke::fail "Xvfb is not installed (apt-get install xvfb)"

# The distribution floor is webkit2gtk **4.1** (Ubuntu 22.04+, Debian 12+,
# Fedora 40+). Say so here rather than letting the loader say it.
if ! ldconfig -p 2>/dev/null | grep -q 'libwebkit2gtk-4\.1'; then
    smoke::fail "libwebkit2gtk-4.1 is missing. Install libwebkit2gtk-4.1-0 \
(Ubuntu 22.04+, Debian 12+, Fedora 40+); 4.0 is not a substitute."
fi
smoke::ok "libwebkit2gtk-4.1 is present"

display=":99"
Xvfb "$display" -screen 0 1280x1024x24 >/dev/null 2>&1 &
xvfb_pid=$!
stop_xvfb() { kill "$xvfb_pid" 2>/dev/null || true; }
SMOKE_EXTRA_CLEANUP=stop_xvfb
trap smoke::cleanup EXIT
sleep 2
kill -0 "$xvfb_pid" 2>/dev/null || smoke::fail "Xvfb did not stay up on $display"
smoke::ok "Xvfb is serving $display"

export DISPLAY="$display"
export WEBKIT_DISABLE_COMPOSITING_MODE=1
export WEBKIT_DISABLE_DMABUF_RENDERER=1
export GDK_BACKEND=x11
SMOKE_LAUNCH_PREFIX=""

# The status document says `tray_enabled: true`, so the shell will try to
# create a tray here too. A virtual framebuffer has no StatusNotifier host, so
# that attempt is expected to log and be ignored -- `build_tray`'s failure is
# caught, never fatal. The tray *appearing* is not something CI can assert;
# that it cannot take the window down with it, is.
smoke::run "$@"
