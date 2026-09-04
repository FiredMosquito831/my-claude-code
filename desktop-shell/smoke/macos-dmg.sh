#!/usr/bin/env bash
# The macOS *disk image* smoke: mount the .dmg the release is about to ship,
# prove the bundle inside it, run the program out of it, unmount, and prove the
# machine is untouched.
#
#     ./smoke/macos-dmg.sh ../dist/MyClaudeCode-macos-universal.dmg
#
# This is the macOS counterpart of `smoke/linux-deb.sh` and
# `smoke/windows-installer.ps1`, and it is built the same way: snapshot,
# exercise, assert, undo, diff. It differs from both in one important respect:
# **it never installs anything.** A `.dmg` is not an installer -- the install
# is a human dragging an icon into `/Applications` -- so there is nothing to
# undo and the assertion is stronger, not weaker: `/Applications` and
# `~/Applications` must be byte-for-byte identical afterwards, because this
# smoke may not write to either of them at all.
#
# What it proves, in order:
#
#   1. the image mounts read-only, and carries exactly two items: the .app and
#      the `/Applications` symlink a person drags it into;
#   2. the bundle's layout is the one `installer/macos/build-app.sh` claims --
#      the executable, Info.plist, the icns and PkgInfo, with their modes;
#   3. `Info.plist` is valid and every key the app depends on is present and
#      correct, read back with `plutil` and `defaults read` rather than
#      grepped;
#   4. `codesign --verify --deep --strict` passes -- the ad-hoc seal survived
#      `lipo`, `ditto` and `hdiutil`. A broken seal presents to a user as "the
#      application is damaged", which no log explains;
#   5. `spctl --assess --type execute` **fails**, and is asserted to fail. The
#      app is unsigned in the sense Gatekeeper cares about, and a smoke that
#      let a green `spctl` slide would be a smoke that never noticed the day
#      the claim in the README stopped being true. The rejection text is
#      printed in full;
#   6. nothing in the image carries `com.apple.quarantine` -- a CI-built file
#      never does, and `xattr -dr` is still run afterwards so the command the
#      documentation gives users is the command this script proves works;
#   7. the .app's executable, copied out of the image, runs: it reads a fake
#      `mcc-desktop --print-status`, paints the port-conflict page and starts
#      nothing (`smoke/macos.sh`, reused verbatim);
#   8. the image detaches;
#   9. `/Applications` and `~/Applications` are what they were.

set -euo pipefail

APP_NAME="My Claude Code.app"
EXECUTABLE="MyClaudeCode"
IDENTIFIER="com.myclaudecode.desktop"
VOLUME_NAME="My Claude Code"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

fail() {
    echo "DMG SMOKE FAIL: $*" >&2
    exit 1
}

ok() {
    echo "  ok: $*"
}

dmg="${1:-}"
[ -n "$dmg" ] || fail "usage: $0 <path to MyClaudeCode-macos-universal.dmg>"
[ -f "$dmg" ] || fail "no disk image at $dmg"
dmg="$(cd "$(dirname "$dmg")" && pwd)/$(basename "$dmg")"

echo "== My Claude Code: macOS .dmg smoke =="
ok "image present: $(stat -f %z "$dmg") bytes, $(shasum -a 256 "$dmg" | cut -d' ' -f1)"

# -- the snapshot ----------------------------------------------------------
# Both application directories, before anything is mounted. `~/Applications`
# usually does not exist on a runner, and "absent" is a state this compares
# just as happily as a listing.
# Every `ls` carries `|| true`, and it is not decoration: this script runs
# under `set -euo pipefail`, `~/Applications` does not exist on a runner, and
# a failing `ls` inside a pipeline is a failing pipeline. Without it the smoke
# exits 1 on its second line, before it has asserted anything -- which is
# exactly what happened on the first release that ran it.
snapshot() {
    {
        { ls -1a /Applications 2>/dev/null || true; } | sed 's|^|/Applications/|'
        { ls -1a "$HOME/Applications" 2>/dev/null || true; } | sed 's|^|~/Applications/|'
    } | LC_ALL=C sort
}

before="$(mktemp)"
after="$(mktemp)"
mount=""

cleanup() {
    if [ -n "$mount" ] && [ -d "$mount" ]; then
        hdiutil detach "$mount" -quiet 2>/dev/null || true
    fi
    rm -f "$before" "$after"
}
trap cleanup EXIT

snapshot > "$before"
ok "snapshotted $(wc -l < "$before" | tr -d ' ') entries in /Applications and ~/Applications"

# -- 1. the image mounts, read-only ----------------------------------------
# `-nobrowse` so Finder does not put a volume on a desktop nobody is looking
# at, `-readonly` so nothing here can write into the image, and an explicit
# mount point so the detach at the end names the same thing the attach made
# rather than parsing `hdiutil info`.
mount="$(mktemp -d)/volume"
mkdir -p "$mount"
hdiutil attach "$dmg" -readonly -nobrowse -noverify -mountpoint "$mount"
ok "attached read-only at $mount"

app="$mount/$APP_NAME"
[ -d "$app" ] || fail "the image has no $APP_NAME at its root"
[ -L "$mount/Applications" ] || fail "the image has no /Applications symlink to drag the app into"
[ "$(readlink "$mount/Applications")" = "/Applications" ] \
    || fail "the Applications symlink points at $(readlink "$mount/Applications")"

entries="$(ls -1 "$mount" | LC_ALL=C sort | tr '\n' ',')"
[ "$entries" = "$APP_NAME,Applications," ] \
    || fail "the image root is '$entries', not exactly the app and the symlink"
ok "the image carries the app and the /Applications symlink, and nothing else"

# `hdiutil imageinfo` reads the image itself rather than the mount, so this is
# the format claim and not a property of how it happens to be mounted.
format="$(hdiutil imageinfo -format "$dmg")"
[ "$format" = "UDZO" ] || fail "the image format is '$format', not UDZO"
ok "format UDZO"

# -- 2. the bundle layout --------------------------------------------------
for path in \
    "Contents/Info.plist" \
    "Contents/PkgInfo" \
    "Contents/MacOS/$EXECUTABLE" \
    "Contents/Resources/$EXECUTABLE.icns" \
    "Contents/_CodeSignature/CodeResources"
do
    [ -f "$app/$path" ] || fail "the bundle has no $path"
done
[ -x "$app/Contents/MacOS/$EXECUTABLE" ] || fail "Contents/MacOS/$EXECUTABLE is not executable"
[ "$(head -c 8 "$app/Contents/PkgInfo")" = "APPL????" ] \
    || fail "PkgInfo is not APPL????"
ok "the bundle layout is the one build-app.sh declares"

architectures="$(lipo -archs "$app/Contents/MacOS/$EXECUTABLE")"
case "$architectures" in
    *arm64*) ;;
    *) fail "the executable has no arm64 slice: $architectures" ;;
esac
case "$architectures" in
    *x86_64*) ;;
    *) fail "the executable has no x86_64 slice: $architectures" ;;
esac
ok "the executable is universal: $architectures"

# -- 3. Info.plist ---------------------------------------------------------
plutil -lint "$app/Contents/Info.plist" || fail "Info.plist does not lint"

plist="$app/Contents/Info"
read_key() {
    # `|| true`: a missing key must produce an empty string and a *named*
    # assertion failure below, not a bare `set -e` exit from inside a command
    # substitution.
    defaults read "$plist" "$1" 2>/dev/null || true
}

[ "$(read_key CFBundleIdentifier)" = "$IDENTIFIER" ] \
    || fail "CFBundleIdentifier is '$(read_key CFBundleIdentifier)', not $IDENTIFIER"
[ "$(read_key CFBundleExecutable)" = "$EXECUTABLE" ] \
    || fail "CFBundleExecutable is '$(read_key CFBundleExecutable)', not $EXECUTABLE"
[ "$(read_key CFBundleName)" = "$VOLUME_NAME" ] || fail "CFBundleName is wrong"
[ "$(read_key CFBundleDisplayName)" = "$VOLUME_NAME" ] || fail "CFBundleDisplayName is wrong"
[ "$(read_key CFBundlePackageType)" = "APPL" ] || fail "CFBundlePackageType is not APPL"
[ "$(read_key LSApplicationCategoryType)" = "public.app-category.developer-tools" ] \
    || fail "LSApplicationCategoryType is wrong"
[ "$(read_key NSHighResolutionCapable)" = "1" ] \
    || fail "NSHighResolutionCapable is not true; the dashboard would render blurry"

version="$(read_key CFBundleShortVersionString)"
case "$version" in
    [0-9]*) ;;
    *) fail "CFBundleShortVersionString '$version' does not start with a digit" ;;
esac
[ "$(read_key CFBundleVersion)" = "$version" ] \
    || fail "CFBundleVersion and CFBundleShortVersionString disagree"

minimum="$(read_key LSMinimumSystemVersion)"
case "$minimum" in
    [0-9]*) ;;
    *) fail "LSMinimumSystemVersion '$minimum' is not a version" ;;
esac

# The shell renders http://127.0.0.1:<port>, which App Transport Security
# blocks by default. The narrow loopback exemption must be there and the
# whole-internet one must not.
read_key NSAppTransportSecurity | grep -q "NSAllowsLocalNetworking = 1" \
    || fail "NSAppTransportSecurity does not allow local networking; the window would load nothing"
if read_key NSAppTransportSecurity | grep -q "NSAllowsArbitraryLoads"; then
    fail "NSAppTransportSecurity exempts arbitrary loads; only loopback is needed"
fi
ok "Info.plist: $IDENTIFIER $version, minimum macOS $minimum, loopback HTTP permitted"

# -- 4. the ad-hoc seal ----------------------------------------------------
codesign --verify --deep --strict --verbose=2 "$app" \
    || fail "the ad-hoc signature does not verify; this app would be reported as damaged"
ok "codesign --verify --deep --strict passes (ad-hoc seal intact)"

# The seal is ad-hoc, which means it has no identity. Assert that too, so the
# day somebody quietly adds a certificate the documentation gets updated with
# it rather than after it.
authority="$(codesign --display --verbose=4 "$app" 2>&1 | sed -n 's/^Authority=//p' || true)"
[ -z "$authority" ] \
    || fail "the bundle carries a signing authority ('$authority'); the docs say it is ad-hoc"
ok "no signing authority: the signature is ad-hoc, as documented"

# -- 5. spctl is EXPECTED to reject ----------------------------------------
# This is the assertion the whole Gatekeeper paragraph in README.md and
# docs/USAGE.md rests on. An unsigned, un-notarised app is refused by
# Gatekeeper, and the release notes say so; if `spctl` ever accepted it, the
# documentation would be wrong and this is where that is caught.
spctl_output="$(spctl --assess --type execute --verbose=4 "$app" 2>&1 || true)"
spctl_status=0
spctl --assess --type execute "$app" >/dev/null 2>&1 || spctl_status=$?
echo "---- spctl --assess --type execute (expected to reject) ----"
echo "$spctl_output"
echo "-----------------------------------------------------------"
[ "$spctl_status" -ne 0 ] \
    || fail "spctl ACCEPTED an unsigned app. Gatekeeper's behaviour, or this build, changed; the documented workaround may no longer be right."
case "$spctl_output" in
    *rejected*) ;;
    *) fail "spctl exited $spctl_status but did not say 'rejected': $spctl_output" ;;
esac
ok "spctl rejects the app, as documented (exit $spctl_status)"

# -- 6. quarantine ---------------------------------------------------------
# A CI-built file has never been through a browser, so it carries no
# quarantine attribute and this is an assertion about the artifact rather
# than about macOS. The removal command is still run afterwards, because it is
# the command the documentation tells users to run and a command nobody ever
# executes is a command nobody knows is misspelt.
if xattr -p com.apple.quarantine "$app" >/dev/null 2>&1; then
    fail "the CI-built bundle carries com.apple.quarantine, which it cannot have earned honestly"
fi
ok "the bundle carries no com.apple.quarantine (nothing downloaded it)"

work="$(mktemp -d)"
ditto "$app" "$work/$APP_NAME"
xattr -dr com.apple.quarantine "$work/$APP_NAME"
ok "xattr -dr com.apple.quarantine runs cleanly on a copy (the documented workaround)"

codesign --verify --deep --strict "$work/$APP_NAME" \
    || fail "the signature did not survive being copied out of the image"
ok "the seal survives ditto out of the image"

# -- 7. the program actually runs ------------------------------------------
# The same window smoke every macOS release already runs, pointed at the
# executable inside the copied bundle. This is the assertion that the image
# ships a working program and not just a file list.
MCC_DESKTOP_SKIP_AUTOSTART=1 "$here/macos.sh" "$work/$APP_NAME/Contents/MacOS/$EXECUTABLE"

rm -rf "$work"

# -- 8. detach -------------------------------------------------------------
hdiutil detach "$mount"
mount=""
ok "detached"

# -- 9. the diff -----------------------------------------------------------
snapshot > "$after"
if ! diff -u "$before" "$after"; then
    fail "/Applications or ~/Applications changed; this smoke installs nothing and must touch neither"
fi
ok "/Applications and ~/Applications are identical (nothing was installed)"

echo "DMG SMOKE PASS"
