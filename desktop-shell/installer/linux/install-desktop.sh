#!/bin/sh
# My Claude Code -- the desktop app, installed for one user, without root.
#
#     tar -xzf MyClaudeCode-linux-x86_64.tar.gz
#     ./install-desktop.sh              # install
#     ./install-desktop.sh --uninstall  # remove exactly what that installed
#
# WHO THIS IS FOR
#   Fedora, Arch, openSUSE, NixOS, a shared machine with no sudo -- anywhere
#   the .deb does not apply. Ubuntu and Debian users want the .deb instead;
#   it is the same binary with a package manager in front of it.
#
# WHAT IT WRITES, and nothing else:
#
#   ~/.local/bin/MyClaudeCode                                          0755
#   ~/.local/bin/MyClaudeCode.tarball.receipt.json                     0644
#   ~/.local/share/applications/my-claude-code-desktop.desktop         0644
#   ~/.local/share/icons/hicolor/<size>/apps/my-claude-code-desktop.png
#
#   No server, no Python, no `uv`, no configuration directory. If My Claude
#   Code is not installed, the first launch of the window shows the official
#   install command and runs it in front of you (decision Q4).
#
#   `scripts/uninstall.sh` removes all four as well, so "remove My Claude Code"
#   and "remove just the window" both work and neither leaves the other's
#   files behind. The pairing is pinned by
#   tests/contracts/test_uninstaller_parity.py.
#
# POSIX sh, not bash: the same constraint scripts/install.sh works under, for
# the same reason -- this is the first thing a new machine runs.

set -eu

BINARY_NAME="MyClaudeCode"
ENTRY_NAME="my-claude-code-desktop.desktop"
ICON_NAME="my-claude-code-desktop"
RECEIPT_NAME="MyClaudeCode.tarball.receipt.json"

here=$(cd "$(dirname "$0")" && pwd)

bin_dir="$HOME/.local/bin"
applications_dir="$HOME/.local/share/applications"
icons_root="$HOME/.local/share/icons/hicolor"

binary_path="$bin_dir/$BINARY_NAME"
receipt_path="$bin_dir/$RECEIPT_NAME"
entry_path="$applications_dir/$ENTRY_NAME"

action="install"

fail() {
    printf 'install-desktop: %s\n' "$*" >&2
    exit 1
}

show_usage() {
    cat <<'USAGE'
Usage: install-desktop.sh [--uninstall] [--help]

Installs the My Claude Code desktop app for the current user only: the
binary into ~/.local/bin, a .desktop entry and its icons into
~/.local/share. Needs no root and touches no system directory.

Options:
  --uninstall   Remove exactly what an install wrote, and nothing else.
  --help        Show this text.
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --uninstall) action="uninstall" ;;
        -h|--help) show_usage; exit 0 ;;
        *) printf 'install-desktop: unknown option: %s\n' "$1" >&2; show_usage >&2; exit 2 ;;
    esac
    shift
done

[ -n "${HOME:-}" ] || fail "HOME is not set; there is no per-user directory to install into."

# Every icon this installer knows how to place. The sizes mirror
# build-deb.sh's; there is no 48x48 source in the tree, and resampling one
# here would put a different image on every machine.
icon_sizes="512x512 256x256 128x128 32x32"

refresh_caches() {
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$applications_dir" >/dev/null 2>&1 || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q -t -f "$icons_root" >/dev/null 2>&1 || true
    fi
}

do_install() {
    [ -f "$here/$BINARY_NAME" ] \
        || fail "no $BINARY_NAME beside this script. Run it from the directory the tarball extracted into."
    [ -f "$here/$ENTRY_NAME" ] \
        || fail "no $ENTRY_NAME beside this script; this is not a complete My Claude Code tarball."

    mkdir -p "$bin_dir" "$applications_dir" || fail "could not create ~/.local/bin and ~/.local/share/applications"

    # Written to a neighbouring name and renamed, so a running window is
    # replaced atomically rather than truncated under itself.
    cp "$here/$BINARY_NAME" "$binary_path.new" || fail "could not copy the binary into $bin_dir"
    chmod 0755 "$binary_path.new"
    mv -f "$binary_path.new" "$binary_path" || fail "could not place $binary_path"

    placed=0
    for size in $icon_sizes; do
        source_icon="$here/icons/$size.png"
        [ -f "$source_icon" ] || continue
        mkdir -p "$icons_root/$size/apps" || continue
        cp "$source_icon" "$icons_root/$size/apps/$ICON_NAME.png" || continue
        chmod 0644 "$icons_root/$size/apps/$ICON_NAME.png"
        placed=$((placed + 1))
    done
    if [ "$placed" -eq 0 ]; then
        printf 'warning: no icons were installed; the launcher entry will show a blank tile.\n' >&2
    fi

    # The Exec line must be absolute. A GUI launcher does not inherit a login
    # shell's PATH, and on Debian and Ubuntu ~/.profile only adds
    # ~/.local/bin to PATH when the directory already existed at login -- so a
    # relative `Exec=MyClaudeCode` works from a terminal and fails from the
    # menu, which is the worst of the two.
    sed -e "s|^Exec=.*|Exec=$binary_path|" "$here/$ENTRY_NAME" > "$entry_path.new" \
        || fail "could not write $entry_path"
    chmod 0644 "$entry_path.new"
    mv -f "$entry_path.new" "$entry_path"

    cat > "$receipt_path" <<RECEIPT
{
  "installer": "install-desktop.sh",
  "binary": "$binary_path",
  "entry": "$entry_path",
  "icon": "$ICON_NAME"
}
RECEIPT
    chmod 0644 "$receipt_path"

    refresh_caches

    printf 'Installed the My Claude Code desktop app for %s:\n' "${USER:-this user}"
    printf '  %s\n' "$binary_path"
    printf '  %s\n' "$entry_path"
    printf '  %s icon size(s) under %s\n' "$placed" "$icons_root"
    printf '\nStart it from your applications menu, or run:\n  %s\n' "$binary_path"
    case ":${PATH:-}:" in
        *":$bin_dir:"*) ;;
        *) printf '\nNote: %s is not on your PATH. Add it, or use the full path above.\n' "$bin_dir" ;;
    esac
    printf '\nTo remove it again: %s --uninstall\n' "$here/$(basename "$0")"
}

remove_path() {
    _target="$1"
    if [ -e "$_target" ] || [ -L "$_target" ]; then
        rm -f "$_target" || fail "could not remove $_target"
        printf 'Removed %s\n' "$_target"
        return 0
    fi
    return 1
}

do_uninstall() {
    removed=0
    remove_path "$binary_path" && removed=$((removed + 1))
    remove_path "$receipt_path" && removed=$((removed + 1))
    remove_path "$entry_path" && removed=$((removed + 1))
    for size in $icon_sizes; do
        remove_path "$icons_root/$size/apps/$ICON_NAME.png" && removed=$((removed + 1))
    done

    refresh_caches

    if [ "$removed" -eq 0 ]; then
        printf 'Nothing to remove: the desktop app is not installed for this user.\n'
    else
        printf 'Removed %s file(s).\n' "$removed"
    fi
    printf 'My Claude Code itself, your configuration and your providers were not touched.\n'
}

case "$action" in
    install) do_install ;;
    uninstall) do_uninstall ;;
esac
