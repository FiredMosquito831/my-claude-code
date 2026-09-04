#!/usr/bin/env bash
# The tarball smoke: extract the archive the release is about to ship, run the
# per-user installer inside it, prove what it wrote, uninstall, and prove the
# home directory came back identical.
#
#     ./smoke/linux-tarball.sh ../dist/MyClaudeCode-linux-x86_64.tar.gz
#
# This is the Fedora/Arch/no-sudo path, so nothing here needs root and nothing
# here writes outside a scratch HOME: the whole point of `install-desktop.sh`
# is that it is per-user, and a smoke that ran it against the runner's own
# home would not be testing that.
#
# It also pins the archive's shape, which delivery path A depends on:
# `config/desktop_shell.py` extracts the single member named `MyClaudeCode`
# and ignores everything else, so the installer and the icons may ride along
# -- but only as long as there is exactly one member with that basename and
# no links.

set -euo pipefail

BINARY_NAME="MyClaudeCode"
ENTRY_NAME="my-claude-code-desktop.desktop"
ICON_NAME="my-claude-code-desktop"

fail() {
    echo "TARBALL SMOKE FAIL: $*" >&2
    exit 1
}

ok() {
    echo "  ok: $*"
}

archive="${1:-}"
[ -n "$archive" ] || fail "usage: $0 <path to MyClaudeCode-linux-x86_64.tar.gz>"
[ -f "$archive" ] || fail "no archive at $archive"
archive="$(cd "$(dirname "$archive")" && pwd)/$(basename "$archive")"

echo "== My Claude Code: Linux tarball smoke =="
ok "archive present: $(stat -c %s "$archive") bytes, $(sha256sum "$archive" | cut -d' ' -f1)"

scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
home="$scratch/home"
extracted="$scratch/extracted"
mkdir -p "$home" "$extracted"

# -- 1. the archive's shape ------------------------------------------------
members="$(tar -tzf "$archive")"
echo "$members" | sed 's/^/    /'

binaries="$(echo "$members" | grep -c "^${BINARY_NAME}\$" || true)"
[ "$binaries" -eq 1 ] \
    || fail "the archive holds $binaries top-level $BINARY_NAME members; path A needs exactly one"
ok "exactly one $BINARY_NAME member"

if tar -tvzf "$archive" | grep -qE '^[hl]'; then
    fail "the archive contains a hard or symbolic link; path A refuses those"
fi
if echo "$members" | grep -qE '(^/|^[A-Za-z]:|(^|/)\.\.(/|$))'; then
    fail "the archive contains an unsafe member path"
fi
ok "no links and no unsafe paths"

for required in "install-desktop.sh" "$ENTRY_NAME"; do
    echo "$members" | grep -qx "$required" \
        || fail "the archive is missing $required"
done
ok "the per-user installer and the desktop entry ride along"

tar -xzf "$archive" -C "$extracted"
[ -x "$extracted/$BINARY_NAME" ] || fail "$BINARY_NAME is not executable after extraction"
[ -x "$extracted/install-desktop.sh" ] || fail "install-desktop.sh is not executable after extraction"
icons="$(find "$extracted/icons" -name '*.png' 2>/dev/null | wc -l)"
[ "$icons" -ge 3 ] || fail "only $icons icon(s) in the archive"
ok "extracted: a runnable binary, a runnable installer and $icons icons"

# -- 2. the snapshot -------------------------------------------------------
# `icon-theme.cache` and `mimeinfo.cache` are excluded for the same reason the
# .deb smoke excludes them: `install-desktop.sh` refreshes both caches on
# install *and* on uninstall, so a machine that had neither ends up with them
# afterwards. They are derived files, and the entries they are derived from
# are what this smoke actually checks.
snapshot() {
    find "$home" 2>/dev/null \
        | grep -v -e '/icon-theme\.cache$' -e '/mimeinfo\.cache$' \
        | LC_ALL=C sort
}
before="$(mktemp)"
after="$(mktemp)"
trap 'rm -rf "$scratch" "$before" "$after"' EXIT
snapshot > "$before"

# -- 3. install ------------------------------------------------------------
(cd "$extracted" && HOME="$home" ./install-desktop.sh)

installed_binary="$home/.local/bin/$BINARY_NAME"
installed_entry="$home/.local/share/applications/$ENTRY_NAME"
receipt="$home/.local/bin/MyClaudeCode.tarball.receipt.json"

[ -x "$installed_binary" ] || fail "no executable at $installed_binary"
[ -f "$installed_entry" ] || fail "no desktop entry at $installed_entry"
[ -f "$receipt" ] || fail "no receipt at $receipt"
[ "$(stat -c %a "$installed_binary")" = "755" ] \
    || fail "$installed_binary is $(stat -c %a "$installed_binary"), not 755"
ok "the binary, the entry and the receipt are in place"

placed="$(find "$home/.local/share/icons/hicolor" -name "$ICON_NAME.png" | wc -l)"
[ "$placed" -ge 3 ] || fail "only $placed icon(s) were installed"
ok "$placed icon sizes under ~/.local/share/icons/hicolor"

# The Exec line must be absolute: a GUI launcher does not inherit a login
# shell's PATH, and ~/.local/bin is exactly the directory that is often not on
# it yet.
exec_line="$(sed -n 's/^Exec=//p' "$installed_entry")"
[ "$exec_line" = "$installed_binary" ] \
    || fail "Exec= is '$exec_line'; it must be the absolute path $installed_binary"
ok "Exec= is the absolute installed path"

if command -v desktop-file-validate >/dev/null 2>&1; then
    desktop-file-validate "$installed_entry" \
        || fail "desktop-file-validate rejected $installed_entry"
    ok "desktop-file-validate accepts the installed entry"
fi

# Nothing may have been written outside the scratch HOME and the extraction
# directory. `install-desktop.sh` is per-user by contract.
outside="$(find "$home" -maxdepth 1 -mindepth 1 | LC_ALL=C sort | tr '\n' ' ')"
[ "$outside" = "$home/.local " ] \
    || fail "install-desktop.sh wrote outside ~/.local: $outside"
ok "everything it wrote is under ~/.local"

# -- 4. uninstall ----------------------------------------------------------
(cd "$extracted" && HOME="$home" ./install-desktop.sh --uninstall)
for path in "$installed_binary" "$installed_entry" "$receipt"; do
    [ ! -e "$path" ] || fail "$path survived --uninstall"
done
[ "$(find "$home/.local/share/icons" -name "$ICON_NAME.png" | wc -l)" -eq 0 ] \
    || fail "an icon survived --uninstall"
ok "--uninstall removed every file the install wrote"

# Running it twice must not fail: an uninstall is the one command a user runs
# when they are not sure what state they are in.
(cd "$extracted" && HOME="$home" ./install-desktop.sh --uninstall) >/dev/null
ok "--uninstall is idempotent"

# -- 5. the diff -----------------------------------------------------------
# Directories are left behind on purpose -- ~/.local/bin is not this
# installer's to delete -- so the diff is over files, not over the tree.
snapshot | while read -r path; do
    if [ -f "$path" ]; then echo "$path"; fi
done > "$after"
if [ -s "$after" ]; then
    cat "$after" >&2
    fail "files survived the uninstall"
fi
ok "no file survived the uninstall"

echo "TARBALL SMOKE PASS"
