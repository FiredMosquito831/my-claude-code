#!/usr/bin/env bash
# Build `My Claude Code.app` from one or more already-compiled shell binaries.
#
#     ./build-app.sh --binary path/to/MyClaudeCode \
#                    --version 6.45.2 \
#                    --output dist/staging
#
# Give `--binary` twice and the two Mach-O files are merged with `lipo` into a
# single universal executable, which is how the shipped `.app` is built: one
# bundle that runs natively on Apple silicon and on Intel.
#
#     ./build-app.sh --binary arm64/MyClaudeCode --binary x86_64/MyClaudeCode \
#                    --version 6.45.2 --output dist/staging
#
# WHY THIS IS HAND-ROLLED AND NOT `cargo tauri build --bundles app`
#
#   The same four reasons `installer/linux/build-deb.sh` gives, and they are
#   the same four reasons:
#
#     1. it needs `tauri-cli`, which the release workflow deliberately does not
#        install -- `cargo build --release --features custom-protocol` is
#        documented there as the exact equivalent of `cargo tauri build
#        --no-bundle`, at the cost of not compiling the CLI on four runners;
#     2. it names the bundle from `productName` and the archive from
#        `<productName>_<version>_<arch>.dmg`, i.e. a space and a version in an
#        asset name, both of which decision Q5 forbids;
#     3. the version would come from `tauri.conf.json` (0.1.0), not from the
#        release tag. Two places to bump is one too many;
#     4. it cannot make a universal bundle out of two per-runner builds. The
#        two macOS legs of `shell-release.yml` are separate machines; the merge
#        has to happen after both, which is `lipo`, which is this script.
#
# WHAT THE BUNDLE CONTAINS -- the whole of it:
#
#   My Claude Code.app/Contents/Info.plist                     0644
#   My Claude Code.app/Contents/PkgInfo                        0644
#   My Claude Code.app/Contents/MacOS/MyClaudeCode             0755
#   My Claude Code.app/Contents/Resources/MyClaudeCode.icns    0644
#   My Claude Code.app/Contents/_CodeSignature/CodeResources   0644 (codesign)
#
#   No server, no Python, no `uv`, no configuration -- decision Q4: the app
#   bootstraps My Claude Code itself on first launch, in front of the user.
#
# THE BUNDLE IS VERSIONED, THE EXECUTABLE IS NOT
#
#   `CFBundleShortVersionString` and `CFBundleVersion` carry the release tag
#   without its leading `v`, because macOS shows that string in Finder's Get
#   Info and in the About box and an app that says 0.1.0 forever is an app
#   nobody can report a bug against. The *executable* keeps its
#   version-agnostic name `MyClaudeCode` (decision Q5), because a Start Menu
#   shortcut, a `.desktop` Exec= line and the Python-side pin all name a file
#   that must never be renamed. The two are not in tension: the version lives
#   in a plist key, which nothing points at.
#
# THE SIGNATURE IS AD-HOC, WHICH IS NOT A DEVELOPER ID
#
#   `codesign --sign -` seals the bundle: every file is hashed into
#   `_CodeSignature/CodeResources`, so a corrupted or tampered copy is refused
#   by the loader instead of crashing strangely. On Apple silicon that seal is
#   *mandatory* -- all arm64 code must carry at least an ad-hoc signature, and
#   `lipo` produces a fresh file whose arm64 slice would otherwise carry a
#   signature computed over a different file. That is the whole reason this
#   step exists.
#
#   It is NOT a Developer ID signature and it is NOT notarisation. An ad-hoc
#   signature carries no identity, so Gatekeeper cannot attribute the app to
#   anyone, `spctl --assess` rejects it, and a copy downloaded in a browser is
#   quarantined and refused on first launch. That is documented honestly in
#   README.md, docs/USAGE.md and the release notes rather than papered over;
#   notarisation needs a paid Apple Developer account, which this project does
#   not have.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

APP_NAME="My Claude Code"
EXECUTABLE="MyClaudeCode"
IDENTIFIER="com.myclaudecode.desktop"
ICON_STEM="MyClaudeCode"

# Tauri's own bundler defaults `minimumSystemVersion` to 10.13 (its
# distribute/macos-application-bundle documentation), so a hand-written
# Info.plist that claims anything lower would be claiming more than the
# framework does. In practice the floor is 10.13 for the Intel slice and 11.0
# for the arm64 one, because Apple silicon did not exist before Big Sur --
# there is no plist key that can say that, and no need for one: a Mac that
# cannot run a slice does not have that slice's hardware.
MINIMUM_SYSTEM_VERSION="10.13"

# A plain array plus an explicit count, because macOS ships bash 3.2 as
# /bin/bash and `${#array[@]}` on an *empty* array is an unbound-variable
# error there under `set -u`. The count is never expanded from the array, so
# the empty case is a plain integer comparison.
binaries=()
binary_count=0
version=""
output=""
icons_dir="$here/../../src-tauri/icons"

die() {
    echo "build-app: $*" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --binary) binaries[$binary_count]="${2:-}"; binary_count=$((binary_count + 1)); shift 2 ;;
        --version) version="${2:-}"; shift 2 ;;
        --output) output="${2:-}"; shift 2 ;;
        --icons) icons_dir="${2:-}"; shift 2 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

[ "$binary_count" -gt 0 ] || die "--binary is required (give it twice for a universal build)"
[ -n "$version" ] || die "--version is required"
[ -n "$output" ] || die "--output is required (the directory the .app is written into)"
for binary in "${binaries[@]}"; do
    [ -f "$binary" ] || die "no binary at $binary"
done

# The version is stamped into two plist keys and shown to a human. A tag that
# is not a version would put `main` or `v` in Finder's Get Info.
case "$version" in
    [0-9]*) ;;
    *) die "--version must start with a digit (got '$version')" ;;
esac
case "$version" in
    *[!0-9.]*) die "--version must be digits and dots only (got '$version')" ;;
esac

for tool in lipo codesign plutil; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is not available; this script runs on macOS"
done

app="$output/$APP_NAME.app"
contents="$app/Contents"

rm -rf "$app"
install -d -m 0755 "$contents/MacOS" "$contents/Resources"

# -- the executable --------------------------------------------------------
# `lipo -create` even for a single input, so the one-binary and two-binary
# paths produce a file made the same way; with one input it is a copy that
# happens to go through the same tool. The `strip = true` release profile
# means the linker's own ad-hoc signature may already be stale here, which is
# what the `codesign --force` below is for.
lipo -create "${binaries[@]}" -output "$contents/MacOS/$EXECUTABLE"
chmod 0755 "$contents/MacOS/$EXECUTABLE"
architectures="$(lipo -archs "$contents/MacOS/$EXECUTABLE")"
echo "build-app: executable architectures: $architectures"

# -- the icon --------------------------------------------------------------
# Generated from `icons/icon.png` rather than copied from the checked-in
# `icon.icns`, so the bundle's icon is derived from the one source image the
# rest of the tree already uses and cannot drift from it. `iconutil` is the
# supported path and produces every retina variant; `sips -s format icns` is
# the fallback for a machine without the Xcode command line tools, and it
# writes a single-representation icns, which renders but does not scale as
# well. The checked-in `icon.icns` is the last resort, so a build never fails
# for want of an icon. Which one was used is printed, because "the icon looks
# slightly wrong" is otherwise an unsearchable bug.
icns="$contents/Resources/$ICON_STEM.icns"
icon_png="$icons_dir/icon.png"
icon_source="none"

if [ -f "$icon_png" ] && command -v iconutil >/dev/null 2>&1 \
    && command -v sips >/dev/null 2>&1
then
    iconset="$(mktemp -d)/$ICON_STEM.iconset"
    mkdir -p "$iconset"
    # The exact set `iconutil` expects. A missing size is tolerated by
    # iconutil; a *wrong* name is not, so they are spelled out rather than
    # generated from a loop over sizes alone.
    for pair in \
        "16:icon_16x16.png" "32:icon_16x16@2x.png" \
        "32:icon_32x32.png" "64:icon_32x32@2x.png" \
        "128:icon_128x128.png" "256:icon_128x128@2x.png" \
        "256:icon_256x256.png" "512:icon_256x256@2x.png" \
        "512:icon_512x512.png" "1024:icon_512x512@2x.png"
    do
        sips -z "${pair%%:*}" "${pair%%:*}" "$icon_png" \
            --out "$iconset/${pair#*:}" >/dev/null 2>&1 || true
    done
    if iconutil -c icns "$iconset" -o "$icns" >/dev/null 2>&1; then
        icon_source="iconutil (from icons/icon.png)"
    fi
    rm -rf "$(dirname "$iconset")"
fi

if [ ! -s "$icns" ] && [ -f "$icon_png" ] && command -v sips >/dev/null 2>&1; then
    if sips -s format icns "$icon_png" --out "$icns" >/dev/null 2>&1; then
        icon_source="sips (from icons/icon.png; single representation)"
    fi
fi

if [ ! -s "$icns" ] && [ -f "$icons_dir/icon.icns" ]; then
    install -m 0644 "$icons_dir/icon.icns" "$icns"
    icon_source="the checked-in icons/icon.icns"
fi

[ -s "$icns" ] || die "no icon could be produced from $icons_dir"
chmod 0644 "$icns"
echo "build-app: icon produced by $icon_source"

# -- Info.plist ------------------------------------------------------------
# Every key here is load-bearing:
#
#   CFBundleIdentifier          the app's identity to macOS. It is also how
#                               `scripts/install.sh --desktop` recognises this
#                               bundle and steps aside instead of writing its
#                               own launcher bundle over it, and how
#                               `scripts/uninstall.sh` knows not to delete it.
#                               The launcher bundle uses a *different* string
#                               (`com.my-claude-code.desktop`) precisely so the
#                               two can be told apart; the parity contract
#                               pins both.
#   CFBundleExecutable          must equal the file in Contents/MacOS, which
#                               is `[[bin]] name` in Cargo.toml.
#   CFBundleShortVersionString  the human version, shown in Get Info.
#   CFBundleVersion             the build version. The same string: this
#                               project has one release per version and no
#                               build numbering underneath it, and two keys
#                               with different answers would be a lie.
#   LSMinimumSystemVersion      see MINIMUM_SYSTEM_VERSION above.
#   NSHighResolutionCapable     without it macOS runs the window through the
#                               1x magnifier and the whole dashboard is blurry
#                               on every Mac made since 2012.
#   LSApplicationCategoryType   how Launchpad and Finder file it.
#   NSAppTransportSecurity      the shell renders http://127.0.0.1:<port>, and
#                               App Transport Security blocks cleartext HTTP by
#                               default. `NSAllowsLocalNetworking` is the
#                               narrow exemption Apple documents for exactly
#                               this -- loopback and .local names only. It is
#                               deliberately not `NSAllowsArbitraryLoads`,
#                               which would exempt the whole internet.
cat > "$contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>en</string>
    <key>CFBundleDisplayName</key>
    <string>$APP_NAME</string>
    <key>CFBundleExecutable</key>
    <string>$EXECUTABLE</string>
    <key>CFBundleIconFile</key>
    <string>$ICON_STEM</string>
    <key>CFBundleIdentifier</key>
    <string>$IDENTIFIER</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>$APP_NAME</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>$version</string>
    <key>CFBundleVersion</key>
    <string>$version</string>
    <key>LSApplicationCategoryType</key>
    <string>public.app-category.developer-tools</string>
    <key>LSMinimumSystemVersion</key>
    <string>$MINIMUM_SYSTEM_VERSION</string>
    <key>NSAppTransportSecurity</key>
    <dict>
        <key>NSAllowsLocalNetworking</key>
        <true/>
    </dict>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSHumanReadableCopyright</key>
    <string>My Claude Code contributors</string>
</dict>
</plist>
PLIST
chmod 0644 "$contents/Info.plist"
plutil -lint "$contents/Info.plist"

# -- PkgInfo ---------------------------------------------------------------
# Eight bytes, no newline: the four-character type and the four-character
# creator. `????` is the documented "no creator code", which is correct -- the
# registry that issued them closed in 2011. It is vestigial and it is also the
# first thing some tools look at, so it costs one line to be right.
printf 'APPL????' > "$contents/PkgInfo"
chmod 0644 "$contents/PkgInfo"

# -- the ad-hoc seal -------------------------------------------------------
# `--force` because `lipo` may have carried a stale signature in from a slice;
# `--deep` because the bundle is signed as a whole, resources included;
# `--timestamp=none` because a trusted timestamp needs Apple's timestamp
# server, is meaningless without an identity to attach it to, and would make
# the build depend on a network round trip.
codesign --force --deep --sign - --timestamp=none "$app"
codesign --verify --deep --strict --verbose=2 "$app"

echo "build-app: built $app"
echo "build-app: version $version, minimum macOS $MINIMUM_SYSTEM_VERSION, ad-hoc signed (NOT Developer ID, NOT notarised)"
find "$app" -type f -exec ls -l {} \;
