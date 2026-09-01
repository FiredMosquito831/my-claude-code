#!/bin/sh
set -eu

FCC_REPO="FiredMosquito831/my-claude-code"
FCC_LATEST_RELEASE_URL="https://api.github.com/repos/${FCC_REPO}/releases/latest"
PYTHON_VERSION="3.14.0"
MIN_UV_VERSION="0.11.0"
UV_INSTALL_URL="https://astral.sh/uv/install.sh"

# Resolved from the release feed at run time (or from --version).
FCC_VERSION=""
FCC_WHEEL_NAME=""
FCC_WHEEL_URL=""
FCC_WHEEL_SHA256=""

dry_run=0
requested_version=""
voice_nim=0
voice_local=0
voice_all=0
torch_backend=""
enable_rtk=0
enable_desktop=0
# Set by the launcher-creation helpers so the closing message reports what
# actually happened instead of hedging with "(if the platform supports it)".
desktop_launcher_created=""
desktop_launcher_error=""
temporary_script=""
temporary_directory=""
release_wheel_path=""

show_usage() {
    cat <<'USAGE'
Usage: install.sh [options]

Installs or updates Free Claude Code to the latest published release.

Installs a compatible uv if one is missing. It does not install Claude Code,
Codex, or Pi -- install whichever of those you use yourself.

Options:
  --version VALUE          Install this exact release instead of the latest.
  --voice-nim              Install NVIDIA NIM voice transcription support.
  --voice-local            Install local Whisper voice transcription support.
  --voice-all              Install all voice transcription backends.
  --torch-backend VALUE    Use a uv PyTorch backend, such as cu130. Requires local voice.
  --rtk                    Enable RTK token optimization for Claude Code, Codex, and Pi.
  --desktop                Create a desktop launcher (app menu entry / .app bundle) for mcc-desktop.
  --dry-run                Print commands without running them.
  --help                   Show this help text.
USAGE
}

fail() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

step() {
    printf '\n==> %s\n' "$1"
}

quote_arg() {
    case "$1" in
        *[!A-Za-z0-9_./:@%+=,-]*|"")
            escaped=$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')
            printf '"%s"' "$escaped"
            ;;
        *)
            printf '%s' "$1"
            ;;
    esac
}

print_command() {
    printf '+'
    for arg in "$@"; do
        printf ' '
        quote_arg "$arg"
    done
    printf '\n'
}

run() {
    print_command "$@"
    if [ "$dry_run" -eq 1 ]; then
        return 0
    fi

    if "$@"; then
        return 0
    else
        status=$?
    fi

    fail "Command failed with exit code $status: $1"
}

cleanup() {
    if [ -n "$temporary_script" ] && [ -e "$temporary_script" ]; then
        rm -f "$temporary_script"
    fi
    if [ -n "$temporary_directory" ] && [ -d "$temporary_directory" ]; then
        rm -rf -- "$temporary_directory"
    fi
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM

add_path_entry() {
    [ -n "$1" ] || return 0
    case ":$PATH:" in
        *":$1:"*) ;;
        *) PATH="$1:$PATH" ;;
    esac
}

add_known_bin_directories() {
    if [ -n "${XDG_BIN_HOME:-}" ]; then
        add_path_entry "$XDG_BIN_HOME"
    fi

    if [ -n "${HOME:-}" ]; then
        add_path_entry "$HOME/.local/bin"
        add_path_entry "$HOME/.cargo/bin"
    fi

    export PATH
    hash -r 2>/dev/null || true
}

require_command() {
    if [ "$dry_run" -eq 0 ] && ! command -v "$1" >/dev/null 2>&1; then
        fail "$1 is required. Install it first, then rerun this installer."
    fi
}

download_and_run() {
    url=$1
    interpreter=$2
    label=$3
    non_interactive=${4:-0}

    if [ "$dry_run" -eq 1 ]; then
        print_command curl -fsSL "$url" -o "<temporary-script>"
        if [ "$non_interactive" -eq 1 ]; then
            printf '+ CODEX_NON_INTERACTIVE=1 '
            quote_arg "$interpreter"
            printf ' <temporary-script>\n'
        else
            print_command "$interpreter" "<temporary-script>"
        fi
        return 0
    fi

    temporary_script=$(mktemp "${TMPDIR:-/tmp}/fcc-install.XXXXXX") || fail "Unable to create a temporary file for $label."
    print_command curl -fsSL "$url" -o "$temporary_script"
    if curl -fsSL "$url" -o "$temporary_script"; then
        :
    else
        status=$?
        fail "Could not download the $label installer (curl exit code $status)."
    fi

    if [ ! -s "$temporary_script" ]; then
        fail "The downloaded $label installer was empty."
    fi

    if [ "$non_interactive" -eq 1 ]; then
        printf '+ CODEX_NON_INTERACTIVE=1 '
        quote_arg "$interpreter"
        printf ' '
        quote_arg "$temporary_script"
        printf '\n'
        if CODEX_NON_INTERACTIVE=1 "$interpreter" "$temporary_script"; then
            :
        else
            status=$?
            fail "$label installation failed with exit code $status."
        fi
    else
        print_command "$interpreter" "$temporary_script"
        if "$interpreter" "$temporary_script"; then
            :
        else
            status=$?
            fail "$label installation failed with exit code $status."
        fi
    fi

    rm -f "$temporary_script"
    temporary_script=""
}

verify_command() {
    command_name=$1
    display_name=$2

    if [ "$dry_run" -eq 1 ]; then
        print_command "$command_name" --version
        return 0
    fi

    command_path=$(command -v "$command_name" 2>/dev/null) || fail "$display_name was installed, but '$command_name' is not available on PATH."
    run "$command_path" --version
}

current_uv_version() {
    if output=$(uv --version); then
        :
    else
        return 1
    fi

    case "$output" in
        uv\ *) version=${output#uv } ;;
        *) version=$output ;;
    esac
    version=${version%% *}

    case "$version" in
        [0-9]*.[0-9]*.[0-9]*) printf '%s\n' "$version" ;;
        *) return 1 ;;
    esac
}

version_ge() {
    current=${1%%[-+]*}
    minimum=${2%%[-+]*}

    old_ifs=$IFS
    IFS=.
    set -- $current
    current_major=${1:-0}
    current_minor=${2:-0}
    current_patch=${3:-0}
    set -- $minimum
    minimum_major=${1:-0}
    minimum_minor=${2:-0}
    minimum_patch=${3:-0}
    IFS=$old_ifs

    case "$current_major$current_minor$current_patch$minimum_major$minimum_minor$minimum_patch" in
        *[!0-9]*) return 1 ;;
    esac

    [ "$current_major" -gt "$minimum_major" ] && return 0
    [ "$current_major" -lt "$minimum_major" ] && return 1
    [ "$current_minor" -gt "$minimum_minor" ] && return 0
    [ "$current_minor" -lt "$minimum_minor" ] && return 1
    [ "$current_patch" -ge "$minimum_patch" ]
}

verify_uv() {
    if [ "$dry_run" -eq 1 ]; then
        print_command uv --version
        return 0
    fi

    command -v uv >/dev/null 2>&1 || fail "uv was installed, but it is not available on PATH."
    version=$(current_uv_version) || fail "uv is present, but 'uv --version' did not return a valid version."
    if ! version_ge "$version" "$MIN_UV_VERSION"; then
        fail "uv $MIN_UV_VERSION or newer is required; found uv $version after installation."
    fi

    printf 'Verified uv %s.\n' "$version"
}

ensure_uv() {
    if [ "$dry_run" -eq 1 ]; then
        if command -v uv >/dev/null 2>&1; then
            print_command uv --version
            printf 'A compatible existing uv will be left unchanged; an obsolete one will be replaced by the standalone installer.\n'
        else
            printf 'uv is not installed; the current standalone uv would be installed.\n'
            download_and_run "$UV_INSTALL_URL" sh "uv"
            verify_uv
        fi
        return 0
    fi

    if command -v uv >/dev/null 2>&1; then
        version=$(current_uv_version) || fail "uv is present, but 'uv --version' did not return a valid version."
        if version_ge "$version" "$MIN_UV_VERSION"; then
            printf 'uv %s already satisfies >=%s; leaving it unchanged.\n' "$version" "$MIN_UV_VERSION"
            return 0
        fi
        printf 'uv %s is below %s; installing the current standalone uv.\n' "$version" "$MIN_UV_VERSION"
    else
        printf 'uv is not installed; installing the current standalone uv.\n'
    fi

    download_and_run "$UV_INSTALL_URL" sh "uv"
    add_known_bin_directories
    verify_uv
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --voice-nim)
                voice_nim=1
                ;;
            --voice-local)
                voice_local=1
                ;;
            --voice-all)
                voice_all=1
                ;;
            --torch-backend)
                shift
                [ "$#" -gt 0 ] || fail "--torch-backend requires a value."
                torch_backend=$1
                [ -n "$torch_backend" ] || fail "--torch-backend requires a non-empty value."
                ;;
            --torch-backend=*)
                torch_backend=${1#*=}
                [ -n "$torch_backend" ] || fail "--torch-backend requires a non-empty value."
                ;;
            --rtk)
                enable_rtk=1
                ;;
            --desktop)
                enable_desktop=1
                ;;
            --version)
                shift
                [ "$#" -gt 0 ] || fail "--version requires a value."
                requested_version=${1#v}
                [ -n "$requested_version" ] || fail "--version requires a value."
                ;;
            --version=*)
                requested_version=${1#*=}
                requested_version=${requested_version#v}
                [ -n "$requested_version" ] || fail "--version requires a value."
                ;;
            --dry-run)
                dry_run=1
                ;;
            --help|-h)
                show_usage
                exit 0
                ;;
            *)
                show_usage >&2
                fail "unknown option: $1"
                ;;
        esac
        shift
    done
}

validate_args() {
    include_local=$voice_local
    if [ "$voice_all" -eq 1 ]; then
        include_local=1
    fi

    if [ -n "$torch_backend" ] && [ "$include_local" -ne 1 ]; then
        fail "--torch-backend requires --voice-local or --voice-all."
    fi
}

extract_wheel_digest() {
    # Scope the search to the asset object whose "name" is the wheel we will
    # download: no other object's digest may satisfy it. Every other "name"
    # line (the top-level release name, sibling assets) and the asset's own
    # "browser_download_url" line end the matched scope, so an asset published
    # without a digest yields nothing instead of borrowing a sibling's, and
    # release-body prose can never be mistaken for the asset's digest.
    printf '%s\n' "$1" |
        awk -v wheel_name="$FCC_WHEEL_NAME" '
            /"name":[[:space:]]*"/ {
                name = $0
                sub(/^.*"name":[[:space:]]*"/, "", name)
                sub(/".*$/, "", name)
                in_asset = (name == wheel_name) ? 1 : 0
            }
            in_asset && /"browser_download_url"/ { in_asset = 0 }
            in_asset && /"digest":[[:space:]]*"sha256:/ {
                line = $0
                sub(/^.*"digest":[[:space:]]*"sha256:/, "", line)
                sub(/".*$/, "", line)
                print line
                exit
            }
        '
}

resolve_release() {
    digest_known=1
    if [ -n "$requested_version" ]; then
        FCC_VERSION=$requested_version
        FCC_WHEEL_NAME="my_claude_code-${FCC_VERSION}-py3-none-any.whl"
        # A pinned install stays verified whenever the tag-scoped feed publishes
        # a digest for the wheel. Only an unreachable feed downgrades to an
        # explicitly reported unverified download; a readable feed that omits
        # the asset's own digest is refused below rather than trusted.
        tag_feed_url="https://api.github.com/repos/${FCC_REPO}/releases/tags/v${FCC_VERSION}"
        print_command curl -fsSL "$tag_feed_url"
        if release_json=$(curl -fsSL -H "Accept: application/vnd.github+json" "$tag_feed_url" 2>/dev/null); then
            :
        else
            printf 'warning: could not reach the release feed to verify v%s -- proceeding unverified.\n' "$FCC_VERSION" >&2
            digest_known=0
        fi
    else
        # Read even during a dry run: it is a GET that changes nothing, and it
        # is the only way to report the version that would actually install.
        print_command curl -fsSL "$FCC_LATEST_RELEASE_URL"
        release_json=$(curl -fsSL -H "Accept: application/vnd.github+json" "$FCC_LATEST_RELEASE_URL" 2>/dev/null) ||
            fail "Could not reach the release feed to find the latest version."
        FCC_VERSION=$(printf '%s\n' "$release_json" |
            grep -m1 '"tag_name"' |
            sed -e 's/.*"tag_name"[[:space:]]*:[[:space:]]*"//' -e 's/".*//' -e 's/^v//')
        [ -n "$FCC_VERSION" ] ||
            fail "Could not read the latest release version from the release feed."
        FCC_WHEEL_NAME="my_claude_code-${FCC_VERSION}-py3-none-any.whl"
    fi

    if [ "$digest_known" -eq 1 ]; then
        # GitHub publishes a sha256 digest per asset, so the download is still
        # verified even though no checksum is pinned in this script. The release
        # body follows the assets in the payload and often repeats the wheel
        # digest as prose, so the digest is taken only from the asset object
        # whose name matches the wheel; an asset without one refuses loudly
        # rather than borrowing a sibling's.
        FCC_WHEEL_SHA256=$(extract_wheel_digest "$release_json")
        [ -n "$FCC_WHEEL_SHA256" ] ||
            fail "No digest published for this asset (${FCC_WHEEL_NAME} in release v${FCC_VERSION}); refusing to install."
    fi
    FCC_WHEEL_URL="https://github.com/${FCC_REPO}/releases/download/v${FCC_VERSION}/${FCC_WHEEL_NAME}"
}

download_verified_release_wheel() {
    if [ "$dry_run" -eq 1 ]; then
        print_command curl -fsSL "$FCC_WHEEL_URL" -o "<temporary-wheel>"
        if [ -n "$FCC_WHEEL_SHA256" ]; then
            printf '+ verify SHA-256 %s for <temporary-wheel>\n' "$FCC_WHEEL_SHA256"
        else
            printf '+ verify the SHA-256 published for this release\n'
        fi
        release_wheel_path="<verified-release-wheel>"
        return 0
    fi

    temporary_directory=$(mktemp -d "${TMPDIR:-/tmp}/fcc-wheel.XXXXXX") ||
        fail "Unable to create a temporary directory for the FCC release wheel."
    release_wheel_path="$temporary_directory/$FCC_WHEEL_NAME"
    print_command curl -fsSL "$FCC_WHEEL_URL" -o "$release_wheel_path"
    if ! curl -fsSL "$FCC_WHEEL_URL" -o "$release_wheel_path"; then
        fail "Could not download the FCC v$FCC_VERSION release wheel."
    fi
    [ -s "$release_wheel_path" ] ||
        fail "The downloaded FCC release wheel was empty."

    if [ -z "$FCC_WHEEL_SHA256" ]; then
        # Reachable only when a --version install could not read the tag feed;
        # resolve_release refuses a missing digest in every other case. The
        # fail-open was announced there and is repeated here so the user sees
        # it immediately before the install happens.
        printf 'warning: installing FCC v%s WITHOUT checksum verification.\n' "$FCC_VERSION" >&2
        return 0
    fi

    if command -v sha256sum >/dev/null 2>&1; then
        actual_sha256=$(sha256sum "$release_wheel_path")
    elif command -v shasum >/dev/null 2>&1; then
        actual_sha256=$(shasum -a 256 "$release_wheel_path")
    else
        fail "sha256sum or shasum is required to verify the FCC release wheel."
    fi
    actual_sha256=${actual_sha256%% *}
    [ "$actual_sha256" = "$FCC_WHEEL_SHA256" ] ||
        fail "FCC release wheel checksum mismatch; refusing to install."
    printf 'Verified FCC v%s release wheel SHA-256.\n' "$FCC_VERSION"
}

package_spec() {
    package_url=$1
    include_nim=$voice_nim
    include_local=$voice_local

    if [ "$voice_all" -eq 1 ]; then
        include_nim=1
        include_local=1
    fi

    if [ "$include_nim" -eq 1 ] && [ "$include_local" -eq 1 ]; then
        printf 'my-claude-code[voice,voice_local] @ %s' "$package_url"
    elif [ "$include_nim" -eq 1 ]; then
        printf 'my-claude-code[voice] @ %s' "$package_url"
    elif [ "$include_local" -eq 1 ]; then
        printf 'my-claude-code[voice_local] @ %s' "$package_url"
    else
        printf 'my-claude-code @ %s' "$package_url"
    fi
}

install_my_claude_code() {
    resolve_release
    download_verified_release_wheel
    package_url="file://$release_wheel_path"
    spec=$(package_spec "$package_url")

    if [ -n "$torch_backend" ]; then
        run uv tool install --force --refresh-package my-claude-code --python "$PYTHON_VERSION" --torch-backend "$torch_backend" "$spec"
    else
        run uv tool install --force --refresh-package my-claude-code --python "$PYTHON_VERSION" "$spec"
    fi
}

enable_rtk_for_agents() {
    [ "$enable_rtk" -eq 1 ] || return 0

    step "Enabling RTK token optimization"
    if [ "$dry_run" -eq 1 ]; then
        print_command mcc-rtk enable claude,codex,pi
        return 0
    fi

    if command -v mcc-rtk >/dev/null 2>&1; then
        run mcc-rtk enable claude,codex,pi
    else
        run "$tool_bin/mcc-rtk" enable claude,codex,pi
    fi
}

create_desktop_shortcut() {
    [ "$enable_desktop" -eq 1 ] || return 0

    step "Creating a desktop launcher"
    if [ "$dry_run" -eq 1 ]; then
        printf '+ export app icon and write a desktop launcher for mcc-desktop\n'
        return 0
    fi

    if desktop_launcher_path=$(command -v mcc-desktop 2>/dev/null); then
        :
    else
        desktop_launcher_path="$tool_bin/mcc-desktop"
    fi

    case "$(uname -s 2>/dev/null)" in
        Darwin)
            if desktop_launcher_created=$(create_macos_app_bundle "$desktop_launcher_path"); then
                printf '%s\n' "$desktop_launcher_created"
            else
                desktop_launcher_error="could not write the app bundle under ~/Applications (icon export or bundle write failed)"
                printf 'warning: %s; continuing without it.\n' "$desktop_launcher_error" >&2
            fi
            ;;
        *)
            if desktop_launcher_created=$(create_linux_desktop_entry "$desktop_launcher_path"); then
                printf '%s\n' "$desktop_launcher_created"
            else
                desktop_launcher_error="could not write the desktop entry under ~/.local/share (icon export or entry write failed)"
                printf 'warning: %s; continuing without it.\n' "$desktop_launcher_error" >&2
            fi
            ;;
    esac
}

create_linux_desktop_entry() {
    launcher_path=$1
    icons_dir="$HOME/.local/share/icons/hicolor/256x256/apps"
    applications_dir="$HOME/.local/share/applications"
    icon_path="$icons_dir/my-claude-code.png"
    desktop_file="$applications_dir/my-claude-code.desktop"

    mkdir -p "$icons_dir" "$applications_dir" || return 1

    # An entry whose Icon= points at a missing file renders as a blank tile, so
    # verify the export produced real bytes rather than trusting the exit code.
    if ! "$launcher_path" --export-icon "$icon_path" >/dev/null 2>&1 || [ ! -s "$icon_path" ]; then
        printf 'warning: could not export the app icon; the entry will use no icon.\n' >&2
        icon_path=""
    fi

    cat > "$desktop_file" <<DESKTOP_ENTRY
[Desktop Entry]
Type=Application
Name=My Claude Code
Comment=Local proxy connecting coding agents to OpenAI-compatible AI providers
Exec=$launcher_path
Icon=$icon_path
Terminal=false
Categories=Development;Utility;
DESKTOP_ENTRY

    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$applications_dir" >/dev/null 2>&1 || true
    fi

    desktop_launcher_created="$desktop_file"
    printf 'Created desktop launcher: %s\n' "$desktop_file"
}

create_macos_app_bundle() {
    launcher_path=$1
    app_dir="$HOME/Applications/My Claude Code.app"
    contents_dir="$app_dir/Contents"
    macos_dir="$contents_dir/MacOS"
    resources_dir="$contents_dir/Resources"
    icns_path="$resources_dir/app-icon.icns"

    mkdir -p "$macos_dir" "$resources_dir" || return 1

    if ! "$launcher_path" --export-icon "$icns_path" >/dev/null 2>&1 || [ ! -s "$icns_path" ]; then
        printf 'warning: could not export the app icon; the bundle will use the default icon.\n' >&2
    fi

    cat > "$contents_dir/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>My Claude Code</string>
    <key>CFBundleDisplayName</key>
    <string>My Claude Code</string>
    <key>CFBundleIdentifier</key>
    <string>com.my-claude-code.desktop</string>
    <key>CFBundleExecutable</key>
    <string>my-claude-code</string>
    <key>CFBundleIconFile</key>
    <string>app-icon.icns</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
</dict>
</plist>
PLIST

    cat > "$macos_dir/my-claude-code" <<WRAPPER
#!/bin/sh
exec "$launcher_path" "\$@"
WRAPPER
    chmod +x "$macos_dir/my-claude-code" || return 1

    desktop_launcher_created="$app_dir"
    printf 'Created macOS app bundle: %s\n' "$app_dir"
}

configure_and_verify_my_claude_code() {
    run uv tool update-shell

    if [ "$dry_run" -eq 1 ]; then
        print_command uv tool dir --bin
        printf '+ verify mcc-server, mcc-claude, mcc-codex, mcc-pi, mcc-help, and my-claude-code in the uv tool bin directory\n'
        print_command mcc-server --version
        return 0
    fi

    print_command uv tool dir --bin
    if tool_bin=$(uv tool dir --bin); then
        :
    else
        status=$?
        fail "Could not determine the uv tool bin directory (exit code $status)."
    fi
    [ -n "$tool_bin" ] || fail "uv returned an empty tool bin directory."

    add_path_entry "$tool_bin"
    export PATH
    hash -r 2>/dev/null || true

    # Verify the native my-claude-code command family (mcc-*) plus the package
    # name shim, exactly as the post-install reference leads with. The legacy
    # fcc-* aliases resolve through the same distribution, so they exist as soon
    # as these do.
    for command_name in mcc-server mcc-claude mcc-claude-old mcc-codex mcc-pi \
        mcc-opencode mcc-opencode2 mcc-kilo \
        mcc-init mcc-chatgpt-oauth-login mcc-compact-log mcc-help mcc-rtk \
        mcc-desktop my-claude-code; do
        [ -x "$tool_bin/$command_name" ] || fail "My Claude Code installation did not create $tool_bin/$command_name."
    done

    print_command "$tool_bin/mcc-server" --version
    if installed_version=$("$tool_bin/mcc-server" --version); then
        printf '%s\n' "$installed_version"
    else
        status=$?
        fail "My Claude Code version verification failed with exit code $status."
    fi
    [ "$installed_version" = "my-claude-code $FCC_VERSION" ] ||
        fail "Expected my-claude-code $FCC_VERSION; found: $installed_version"
}

parse_args "$@"
validate_args
add_known_bin_directories

step "Checking installation prerequisites"
require_command curl
require_command bash
require_command sh
require_command mktemp

step "Ensuring uv $MIN_UV_VERSION or newer is installed"
ensure_uv

step "Installing or updating My Claude Code"
install_my_claude_code

step "Configuring PATH and verifying My Claude Code"
configure_and_verify_my_claude_code

enable_rtk_for_agents
create_desktop_shortcut

if [ "$dry_run" -eq 1 ]; then
    printf '\nDry run complete. No changes were made.\n'
else
    printf '\nMy Claude Code %s is installed and verified.\n' "$FCC_VERSION"
    printf '\nStart the proxy:\n'
    printf '  mcc-server              Start the local proxy and admin dashboard\n'
    printf '\nUse a coding agent through the proxy:\n'
    printf '  mcc-claude              Launch Claude Code through the proxy\n'
    printf '  mcc-claude --discover-models   Enable the model picker from the catalog\n'
    printf '  mcc-codex               Launch Codex through the proxy\n'
    printf '  mcc-pi                  Launch Pi through the proxy\n'
    printf '  mcc-opencode            Launch OpenCode through the proxy\n'
    printf '  mcc-opencode2           Launch the OpenCode 2 preview through the proxy\n'
    printf '  mcc-kilo                Launch Kilo CLI through the proxy\n'
    printf '  mcc-desktop             Open the system tray app (desktop)\n'
    printf '\nManage and inspect:\n'
    printf '  mcc-init                Create or repair ~/.fcc/.env\n'
    printf '  mcc-rtk                 Manage the RTK token optimizer\n'
    printf '  mcc-help                Show what each command does\n'
    if [ "$enable_desktop" -eq 1 ]; then
        if [ -n "$desktop_launcher_created" ]; then
            printf '\nDesktop launcher: %s\n' "$desktop_launcher_created"
        elif [ -n "$desktop_launcher_error" ]; then
            printf '\nThe desktop launcher was not created: %s.\n' "$desktop_launcher_error"
        fi
    fi
    printf '\nThe legacy fcc-* commands (fcc-server, fcc-claude, ...) remain as aliases.\n'
    printf '\nTo use an update installed while the server is running, restart the proxy\n'
    printf 'with: mcc-server\n'
fi
