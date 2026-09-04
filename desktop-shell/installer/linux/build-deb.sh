#!/usr/bin/env bash
# Build `MyClaudeCode-linux-x86_64.deb` from an already-compiled shell binary.
#
#     ./build-deb.sh --binary path/to/MyClaudeCode \
#                    --version 6.45.1 \
#                    --output dist/MyClaudeCode-linux-x86_64.deb
#
# WHY THIS IS HAND-ROLLED AND NOT `cargo tauri build --bundles deb`
#
#   Tauri's own bundler can produce a .deb, and it can pin `depends`. It was
#   still the wrong tool here, for four reasons that are all about *this*
#   repository rather than about the bundler:
#
#     1. it needs `tauri-cli`, which the release workflow deliberately does not
#        install -- `cargo build --release --features custom-protocol` is
#        documented there as the exact equivalent of `cargo tauri build
#        --no-bundle`, at the cost of not compiling the CLI on four runners;
#     2. it names the file `<productName>_<version>_<arch>.deb`, i.e.
#        `My Claude Code_0.1.0_amd64.deb` -- a space, and a version. Decision
#        Q5 says the shipped asset name carries neither, so it would be
#        renamed on every build anyway;
#     3. the version would come from `tauri.conf.json` (0.1.0), not from the
#        release tag. Two places to bump is one too many;
#     4. it appends its own dependency list to `depends`, so the
#        `libgtk-3-0t64 | libgtk-3-0` alternative -- the whole point of the
#        exercise, since Ubuntu 24.04 and Debian 13 renamed the package -- is
#        not the only thing in the field, and what else lands there changes
#        with the bundler version.
#
#   `dpkg-deb --build` gives exact control over all four, is one shell script,
#   and the layout it produces is asserted end to end by
#   `desktop-shell/smoke/linux-deb.sh` (install, check every path, run the
#   binary, remove, diff) on every release.
#
# WHAT THE PACKAGE CONTAINS -- the whole of it:
#
#   /usr/bin/MyClaudeCode                                       0755
#   /usr/share/applications/my-claude-code-desktop.desktop      0644
#   /usr/share/icons/hicolor/<size>/apps/my-claude-code-desktop.png
#   /usr/share/doc/my-claude-code-desktop/copyright             0644
#   /usr/share/doc/my-claude-code-desktop/changelog.gz          0644
#
#   No server, no Python, no `uv`, no configuration -- decision Q4: the app
#   bootstraps My Claude Code itself on first launch, in front of the user.
#   Nothing is written under $HOME by the package; the maintainer scripts only
#   refresh two caches, and both calls are `|| true`.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PACKAGE="my-claude-code-desktop"
ARCH="amd64"
ICON_NAME="my-claude-code-desktop"

binary=""
version=""
output=""
icons_dir="$here/../../src-tauri/icons"

die() {
    echo "build-deb: $*" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --binary) binary="${2:-}"; shift 2 ;;
        --version) version="${2:-}"; shift 2 ;;
        --output) output="${2:-}"; shift 2 ;;
        --icons) icons_dir="${2:-}"; shift 2 ;;
        -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

[ -n "$binary" ] || die "--binary is required"
[ -n "$version" ] || die "--version is required"
[ -n "$output" ] || die "--output is required"
[ -f "$binary" ] || die "no binary at $binary"

# A Debian version with no revision is a *native* package, which is what this
# is: there is no separate upstream tarball. That also fixes the changelog's
# name -- `changelog.gz`, not `changelog.Debian.gz`.
case "$version" in
    [0-9]*) ;;
    *) die "--version must start with a digit (got '$version')" ;;
esac
case "$version" in
    *-*) die "--version must not carry a Debian revision (got '$version')" ;;
esac

command -v dpkg-deb >/dev/null 2>&1 || die "dpkg-deb is not installed"

staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT

install -d -m 0755 "$staging/DEBIAN"
install -d -m 0755 "$staging/usr/bin"
install -d -m 0755 "$staging/usr/share/applications"
install -d -m 0755 "$staging/usr/share/doc/$PACKAGE"

install -m 0755 "$binary" "$staging/usr/bin/MyClaudeCode"
install -m 0644 "$here/my-claude-code-desktop.desktop" \
    "$staging/usr/share/applications/$PACKAGE.desktop"
install -m 0644 "$here/copyright" "$staging/usr/share/doc/$PACKAGE/copyright"

# Icons. Only the sizes that exist as real files are installed: there is no
# 48x48 source in the tree and resampling one in CI would put a different
# image in every build. hicolor scales between 32 and 128 for the sizes in
# between, which is what a theme is for.
#
# The icon is named `my-claude-code-desktop`, not `my-claude-code`: the
# server's own `install.sh --desktop` exports `my-claude-code.png` into
# ~/.local/share/icons, and a per-user icon *shadows* a system one of the same
# name. Two different images under one name is how an upgrade silently changes
# an icon.
icon_installed=0
for pair in "512x512:icon.png" "256x256:128x128@2x.png" "128x128:128x128.png" "32x32:32x32.png"; do
    size="${pair%%:*}"
    source_file="$icons_dir/${pair#*:}"
    [ -f "$source_file" ] || continue
    install -d -m 0755 "$staging/usr/share/icons/hicolor/$size/apps"
    install -m 0644 "$source_file" \
        "$staging/usr/share/icons/hicolor/$size/apps/$ICON_NAME.png"
    icon_installed=$((icon_installed + 1))
done
[ "$icon_installed" -ge 3 ] \
    || die "only $icon_installed icon sizes were found under $icons_dir"

# The changelog. Debian policy wants one in every binary package; `-9n` so the
# gzip member carries no timestamp and no name, and two identical builds
# produce identical bytes.
{
    printf '%s (%s) unstable; urgency=medium\n\n' "$PACKAGE" "$version"
    printf '  * My Claude Code desktop app %s.\n' "$version"
    printf '  * Release notes: https://github.com/FiredMosquito831/my-claude-code/releases/tag/v%s\n\n' "$version"
    printf ' -- My Claude Code <my-claude-code@users.noreply.github.com>  Thu, 01 Jan 1970 00:00:00 +0000\n'
} > "$staging/usr/share/doc/$PACKAGE/changelog"
gzip -9n "$staging/usr/share/doc/$PACKAGE/changelog"
chmod 0644 "$staging/usr/share/doc/$PACKAGE/changelog.gz"

installed_size="$(du -ks "$staging/usr" | cut -f1)"

# DEPENDS, and why each alternative is not optional:
#
#   libwebkit2gtk-4.1-0        the webview. 4.0 is not a substitute and
#                              Ubuntu 24.04 does not ship it at all.
#   libgtk-3-0t64 | libgtk-3-0 Ubuntu 24.04 and Debian 13 renamed the package
#                              during the 64-bit time_t transition. Naming only
#                              one of the two makes the package uninstallable
#                              on half the supported distributions.
#   libayatana-appindicator3-1 | libappindicator3-1
#                              the tray. The spec's first draft had this as a
#                              Recommends; it is a Depends deliberately,
#                              because `apt install --no-install-recommends`
#                              is common and a tray that silently is not there
#                              is the failure this whole arc exists to avoid.
#
# `${shlibs:Depends}` is absent because there is no `dh_shlibdeps` here: the
# three lines above are what the binary dlopens or links that a minimal
# desktop does not already carry, and every other symbol it needs comes from
# glibc, which the ubuntu-22.04 build floor already pins.
cat > "$staging/DEBIAN/control" <<CONTROL
Package: $PACKAGE
Version: $version
Architecture: $ARCH
Section: utils
Priority: optional
Maintainer: My Claude Code <my-claude-code@users.noreply.github.com>
Homepage: https://github.com/FiredMosquito831/my-claude-code
Depends: libwebkit2gtk-4.1-0, libgtk-3-0t64 | libgtk-3-0, libayatana-appindicator3-1 | libappindicator3-1
Installed-Size: $installed_size
Description: My Claude Code dashboard in its own window
 A desktop window and a tray icon for My Claude Code, the local proxy that
 connects coding agents to OpenAI-compatible AI providers.
 .
 The window renders the admin dashboard the local My Claude Code server
 already serves. This package carries the window and nothing else: no Python,
 no server, no configuration. If My Claude Code is not installed, the first
 launch shows the official install command and runs it in front of you.
 .
 Removing this package removes the window. It does not remove My Claude Code
 itself, your configuration, or your providers.
CONTROL

# Maintainer scripts. Both do exactly one thing -- refresh a cache that is
# only a cache -- and both tolerate the tool being absent, because a package
# that fails to configure because `desktop-file-utils` is not installed is a
# worse outcome than a menu that updates on next login.
cat > "$staging/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e

if [ "$1" = "configure" ]; then
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q /usr/share/applications || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
    fi
fi

exit 0
POSTINST

cat > "$staging/DEBIAN/postrm" <<'POSTRM'
#!/bin/sh
set -e

if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q /usr/share/applications || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
    fi
fi

exit 0
POSTRM

chmod 0755 "$staging/DEBIAN/postinst" "$staging/DEBIAN/postrm"

# md5sums. `dpkg-deb` does not write one; lintian and `dpkg --verify` both
# want it, and it costs one command.
(
    cd "$staging"
    find usr -type f -print0 | LC_ALL=C sort -z \
        | xargs -0 md5sum > DEBIAN/md5sums
)
chmod 0644 "$staging/DEBIAN/md5sums"

mkdir -p "$(dirname "$output")"
# `--root-owner-group` so the package's files are root:root whatever the
# builder's uid is; without it a runner-built .deb installs files owned by
# uid 1001, which lintian reports and dpkg happily ships.
dpkg-deb --root-owner-group --build "$staging" "$output" >/dev/null

echo "built $output"
dpkg-deb --info "$output"
dpkg-deb --contents "$output"
