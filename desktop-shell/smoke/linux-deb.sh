#!/usr/bin/env bash
# The Linux *package* smoke: install the .deb the release is about to ship,
# prove every path it claims, run the installed binary, remove it, and prove
# the machine came back byte-identical.
#
#     ./smoke/linux-deb.sh ../dist/MyClaudeCode-linux-x86_64.deb
#
# This is the Linux counterpart of `smoke/windows-installer.ps1`, and it is
# built the same way: snapshot, install, assert, uninstall, diff. The Windows
# script's `/VERYSILENT` -- "an installer that prompts makes every unattended
# install hang forever" -- is `dpkg -i` under DEBIAN_FRONTEND=noninteractive
# here, for exactly the same reason.
#
# What it proves, in order:
#
#   1. the control fields are the ones the distribution floor needs -- in
#      particular the `libgtk-3-0t64 | libgtk-3-0` alternative, without which
#      the package is uninstallable on Ubuntu 24.04 and Debian 13;
#   2. `dpkg -i` completes with no prompt and no question;
#   3. every path the package declares is on disk, with the mode it declared,
#      and the .desktop entry passes `desktop-file-validate`;
#   4. the *installed* /usr/bin/MyClaudeCode -- not the one in target/ --
#      starts, reads a status document and paints the port-conflict page
#      without starting a server (that is `smoke/linux.sh`, reused verbatim);
#   5. `dpkg -r` removes every one of those paths;
#   6. and /usr/bin, /usr/share/applications and /usr/share/icons/hicolor are
#      identical to what they were before step 2.
#
# It needs passwordless sudo, which is what a GitHub runner has and a
# developer's laptop usually does not. Run it there, or in a container.

set -euo pipefail

PACKAGE="my-claude-code-desktop"
BINARY="/usr/bin/MyClaudeCode"
ENTRY="/usr/share/applications/my-claude-code-desktop.desktop"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

fail() {
    echo "DEB SMOKE FAIL: $*" >&2
    exit 1
}

ok() {
    echo "  ok: $*"
}

deb="${1:-}"
[ -n "$deb" ] || fail "usage: $0 <path to MyClaudeCode-linux-x86_64.deb>"
[ -f "$deb" ] || fail "no package at $deb"
deb="$(cd "$(dirname "$deb")" && pwd)/$(basename "$deb")"

echo "== My Claude Code: Linux .deb smoke =="
ok "package present: $(stat -c %s "$deb") bytes, $(sha256sum "$deb" | cut -d' ' -f1)"

command -v dpkg-deb >/dev/null 2>&1 || fail "dpkg-deb is not installed"
sudo -n true 2>/dev/null || fail "this smoke needs passwordless sudo"

if dpkg -s "$PACKAGE" >/dev/null 2>&1; then
    fail "$PACKAGE is already installed; this smoke refuses to remove someone else's copy"
fi

# -- 1. the control fields -------------------------------------------------
dpkg-deb --info "$deb"

field() {
    dpkg-deb --field "$deb" "$1"
}

[ "$(field Package)" = "$PACKAGE" ] || fail "Package is '$(field Package)', not $PACKAGE"
[ "$(field Architecture)" = "amd64" ] || fail "Architecture is '$(field Architecture)', not amd64"
[ "$(field Section)" = "utils" ] || fail "Section is '$(field Section)'"
[ "$(field Priority)" = "optional" ] || fail "Priority is '$(field Priority)'"
[ -n "$(field Maintainer)" ] || fail "Maintainer is empty"
[ -n "$(field Homepage)" ] || fail "Homepage is empty"
version="$(field Version)"
case "$version" in
    [0-9]*) ;;
    *) fail "Version '$version' does not start with a digit" ;;
esac
ok "control: $PACKAGE $version amd64"

depends="$(field Depends)"
for required in \
    "libc6 (>= 2.35)" \
    "libwebkit2gtk-4.1-0" \
    "libgtk-3-0t64 | libgtk-3-0" \
    "libayatana-appindicator3-1 | libappindicator3-1"
do
    case "$depends" in
        *"$required"*) ;;
        *) fail "Depends is missing '$required'; it is: $depends" ;;
    esac
done
ok "Depends carries the glibc floor and both renamed-package alternatives"

# -- the snapshot ----------------------------------------------------------
# The three directories the package writes into. They are machine-global and
# cannot be redirected, so they are compared before and after instead --
# exactly what the Windows installer smoke does with HKCU and the Start Menu.
#
# Two files are excluded, and the exclusion is the interesting part: the
# package's postinst and postrm both run `update-desktop-database` and
# `gtk-update-icon-cache`, which *write* `mimeinfo.cache` and
# `icon-theme.cache`. On a machine that had neither, the postrm regenerates
# them after the removal -- so they legitimately exist afterwards and did not
# before. They are caches, derived entirely from the entries around them, and
# comparing them would make this assertion fail for the one thing the
# maintainer scripts are allowed to do.
snapshot() {
    {
        find /usr/bin -maxdepth 1
        find /usr/share/applications -maxdepth 1
        find /usr/share/icons/hicolor -maxdepth 3
        find /usr/share/doc -maxdepth 1
    } 2>/dev/null \
        | grep -v -e '/icon-theme\.cache$' -e '/mimeinfo\.cache$' \
        | LC_ALL=C sort
}

before="$(mktemp)"
after="$(mktemp)"
trap 'rm -f "$before" "$after"' EXIT
snapshot > "$before"
ok "snapshotted $(wc -l < "$before") paths"

# -- 2. a non-interactive install ------------------------------------------
if ! sudo DEBIAN_FRONTEND=noninteractive dpkg -i "$deb"; then
    echo "dpkg -i reported unmet dependencies; resolving them with apt-get -f"
    sudo DEBIAN_FRONTEND=noninteractive apt-get -y -f install
fi
status="$(dpkg -s "$PACKAGE" | awk '/^Status:/ {print $2, $3, $4}')"
[ "$status" = "install ok installed" ] || fail "dpkg status is '$status'"
ok "dpkg -i completed with no prompt: $status"

# -- 3. every declared path, with its declared mode ------------------------
# `dpkg-deb --contents` is the package's own manifest, so this cannot drift
# from what was built: every regular file it lists must be on disk, and the
# binary must be executable.
missing=0
while read -r mode _owner _size _date _time path; do
    case "$mode" in -*) ;; *) continue ;; esac
    installed="/${path#./}"
    if [ ! -f "$installed" ]; then
        echo "  MISSING: $installed" >&2
        missing=$((missing + 1))
        continue
    fi
    expected="$(printf '%s' "$mode" | cut -c2-10)"
    actual="$(stat -c %A "$installed" | cut -c2-10)"
    [ "$expected" = "$actual" ] \
        || fail "$installed is $actual, the package declared $expected"
done < <(dpkg-deb --contents "$deb")
[ "$missing" -eq 0 ] || fail "$missing declared file(s) are not on disk"
ok "every file dpkg-deb --contents declares is installed with its declared mode"

[ -x "$BINARY" ] || fail "$BINARY is not executable"
[ -f "$ENTRY" ] || fail "$ENTRY is missing"
[ -f "/usr/share/doc/$PACKAGE/copyright" ] || fail "no copyright file"
[ -f "/usr/share/doc/$PACKAGE/changelog.gz" ] || fail "no changelog.gz"
for size in 512x512 256x256 128x128 32x32; do
    [ -f "/usr/share/icons/hicolor/$size/apps/my-claude-code-desktop.png" ] \
        || fail "no $size icon"
done
ok "binary, entry, icons, copyright and changelog are all in place"

if command -v desktop-file-validate >/dev/null 2>&1; then
    desktop-file-validate "$ENTRY" || fail "desktop-file-validate rejected $ENTRY"
    ok "desktop-file-validate accepts the entry"
else
    fail "desktop-file-validate is not installed (apt-get install desktop-file-utils)"
fi

# The taskbar groups a window with its launcher by matching the window's
# WM_CLASS against StartupWMClass. GTK derives WM_CLASS from the program name,
# which is the executable's basename -- so these two must agree, and the
# Python contract asserts the same pairing against Cargo.toml's [[bin]] name.
wmclass="$(sed -n 's/^StartupWMClass=//p' "$ENTRY")"
[ "$wmclass" = "$(basename "$BINARY")" ] \
    || fail "StartupWMClass is '$wmclass' but the binary is $(basename "$BINARY")"
ok "StartupWMClass matches the installed binary's name"

# -- 4. the installed binary actually runs ---------------------------------
# The same window smoke the tarball leg runs, pointed at /usr/bin. This is the
# assertion that the .deb ships a working program and not just a file list.
"$here/linux.sh" "$BINARY"

# -- 5. removal ------------------------------------------------------------
sudo DEBIAN_FRONTEND=noninteractive dpkg -r "$PACKAGE"
if dpkg -s "$PACKAGE" 2>/dev/null | grep -q '^Status: install ok installed'; then
    fail "$PACKAGE is still installed after dpkg -r"
fi
ok "dpkg -r completed"

for path in "$BINARY" "$ENTRY" "/usr/share/doc/$PACKAGE/copyright" \
    "/usr/share/doc/$PACKAGE/changelog.gz"
do
    [ ! -e "$path" ] || fail "$path survived the removal"
done
for size in 512x512 256x256 128x128 32x32; do
    icon="/usr/share/icons/hicolor/$size/apps/my-claude-code-desktop.png"
    [ ! -e "$icon" ] || fail "$icon survived the removal"
done
ok "every installed path is gone"

# -- 6. the diff -----------------------------------------------------------
snapshot > "$after"
if ! diff -u "$before" "$after"; then
    fail "the filesystem is not what it was before the install"
fi
ok "/usr/bin, /usr/share/applications, /usr/share/icons and /usr/share/doc are identical"

echo "DEB SMOKE PASS"
