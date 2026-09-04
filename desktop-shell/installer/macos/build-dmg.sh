#!/usr/bin/env bash
# Wrap `My Claude Code.app` in the disk image people download.
#
#     ./build-dmg.sh --app dist/staging/"My Claude Code.app" \
#                    --output dist/MyClaudeCode-macos-universal.dmg
#
# WHAT IS IN THE IMAGE
#
#   /My Claude Code.app      the bundle build-app.sh produced
#   /Applications            a symlink, so the window Finder opens shows the
#                            app and the folder to drag it into side by side
#
#   That is the whole of it. No background image, no `.DS_Store` with a window
#   layout, no licence agreement. A styled dmg needs a window position baked
#   into a `.DS_Store`, which means mounting the image read-write, driving
#   Finder over AppleScript, and unmounting -- three things that are flaky on
#   a headless runner and buy an arrangement that Finder overrides anyway once
#   the user has opened the image once. Two items and a symlink is
#   unambiguous.
#
# WHY hdiutil, UDZO AND HFS+
#
#   `hdiutil create -srcfolder` is Apple's own documented way to make a
#   distribution image, and `-format UDZO` is the read-only zlib-compressed
#   format every macOS since 10.1 mounts. `-fs HFS+` is explicit rather than
#   left to default: newer macOS versions have started defaulting `hdiutil` to
#   APFS, which older systems cannot mount at all, and the bundle claims
#   `LSMinimumSystemVersion 10.13`. An image the app's own minimum system
#   cannot open would make that claim false.
#
# WHAT THIS DOES NOT DO, AND WILL NOT
#
#   It does not sign the image and it does not notarise it. Signing a dmg
#   needs a Developer ID certificate; notarising it needs a paid Apple
#   Developer account and a round trip to Apple's service. This project has
#   neither, by decision, and the consequence is stated plainly rather than
#   hidden: a copy of this dmg downloaded in a browser carries
#   `com.apple.quarantine`, and on current macOS Gatekeeper refuses to launch
#   the app inside it -- with no Control-click bypass, which Apple removed in
#   Sequoia. The documented one-time workaround is
#
#       xattr -d com.apple.quarantine "/Applications/My Claude Code.app"
#
#   after dragging it across, and the friction-free alternative is the install
#   script plus `mcc-desktop`, which fetches the same binary over HTTPS from
#   Python and is therefore never quarantined at all.

set -euo pipefail

VOLUME_NAME="My Claude Code"

app=""
output=""
volume_name="$VOLUME_NAME"

die() {
    echo "build-dmg: $*" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --app) app="${2:-}"; shift 2 ;;
        --output) output="${2:-}"; shift 2 ;;
        --volname) volume_name="${2:-}"; shift 2 ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

[ -n "$app" ] || die "--app is required"
[ -n "$output" ] || die "--output is required"
[ -d "$app" ] || die "no app bundle at $app"
[ -x "$app/Contents/MacOS/MyClaudeCode" ] || die "$app has no executable at Contents/MacOS/MyClaudeCode"

command -v hdiutil >/dev/null 2>&1 || die "hdiutil is not available; this script runs on macOS"

staging="$(mktemp -d)"
# `mktemp -d` makes 0700, so the directory inside the image would be
# drwx------ and the mounted volume would look empty to anyone but its owner.
chmod 0755 "$staging"
trap 'rm -rf "$staging"' EXIT

# `ditto` and not `cp -R`: it is the only copy on macOS that preserves
# extended attributes and resource forks, and a code signature lives partly in
# those. A `cp -R` of a signed bundle can arrive with a seal that no longer
# verifies, which presents to the user as "the application is damaged".
ditto "$app" "$staging/$(basename "$app")"
ln -s /Applications "$staging/Applications"

# Verify the copy still verifies, here rather than after the image is built:
# a broken seal found now names `ditto` as the cause, and one found after
# `hdiutil` names nothing.
codesign --verify --deep --strict "$staging/$(basename "$app")"

mkdir -p "$(dirname "$output")"
rm -f "$output"
hdiutil create \
    -volname "$volume_name" \
    -srcfolder "$staging" \
    -fs HFS+ \
    -format UDZO \
    -ov \
    "$output"

echo "build-dmg: built $output"
hdiutil imageinfo "$output" | sed -n '1,40p'
