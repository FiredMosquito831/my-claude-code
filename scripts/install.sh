#!/bin/sh
set -eu

# My Claude Code installer (POSIX shells: Linux, macOS, WSL).
#
# This script owns every prerequisite the proxy needs, so a machine with
# nothing but a shell and curl ends up with a working install:
#   * uv           -- installed from https://astral.sh/uv/install.sh when it is
#                     missing, and REPLACED when the uv already on PATH is older
#                     than the floor below. The floor is MIN_UV_VERSION and it
#                     tracks [tool.uv] required-version in pyproject.toml.
#   * Python       -- PYTHON_VERSION is downloaded by uv itself
#                     (`uv python install`) BEFORE the tool environment is
#                     built, and the tool environment is pinned to a uv-managed
#                     interpreter (`--managed-python`). A system Python is never
#                     used, and none needs to exist.
#   * My Claude Code -- installed into an isolated uv tool environment from the
#                     release wheel, after its SHA-256 is verified.
#
# The ONE thing this script cannot bootstrap is curl: it is what fetches
# everything else. require_curl below names the package for the local distro
# and exits 1 rather than failing later with a confusing error.
#
# Behind a proxy, export HTTPS_PROXY (and HTTP_PROXY/NO_PROXY) before running
# this script: curl and uv both honour those variables, so every download here
# -- the uv installer, the Python build, the release wheel -- goes through it.
#
# Desktop app prerequisites are NOT this script's job and are not installed
# here: on Linux the .deb declares webkit2gtk in its Depends and apt pulls it
# in; on Windows the Inno Setup installer detects and bootstraps WebView2; on
# macOS the .dmg needs nothing. --desktop below only writes a launcher entry
# for the mcc-desktop command that this install already provides.

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
# What the caller asked for about the server (6.73.0). MCC_INSTALL_NO_START is
# the environment form of --no-start, for a caller that cannot add a flag.
restart_requested=0
no_start_requested=0
# The first release whose mcc-server understands --report-holder and
# --stop-holder. Older builds do not REFUSE those flags: they ignore every
# argument but --version and start a server.
RESTART_AWARE_VERSION="6.73.0"
restart_report_available=0
[ "${MCC_INSTALL_NO_START:-}" = "1" ] && no_start_requested=1
# The exclusive update lock (decision Q5). Until 6.73.0 a hand-run install and
# a dashboard-triggered one shared nothing: they wrote the same receipt, into
# the same tool directory, with no coordination at all.
holds_update_lock=0
# 6.73.0's two extra receipt fields. `null` means "this episode has not decided
# yet", which is what a reader shows nothing for.
install_progress_restarted=null
install_progress_holder=""
# Set by restart_after_install; read by nothing else, but named here so the
# script has no undeclared globals under `set -u`.
server_port=8082
server_bind_host=127.0.0.1
server_reachable_host=127.0.0.1
started_server_pid=0
# Set by the launcher-creation helpers so the closing message reports what
# actually happened instead of hedging with "(if the platform supports it)".
desktop_launcher_created=""
desktop_launcher_error=""
# Absolute path to the uv this script uses. ensure_uv replaces the bare name
# with the resolved path so no later step re-searches PATH for it.
uv_bin="uv"
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
                           The tray app needs webkit2gtk on Linux (the .deb
                           package declares it in Depends) and WebView2 on
                           Windows (the Setup .exe bootstraps it); macOS needs
                           nothing.
  --restart                After a successful install, restart the My Claude
                           Code server on the port this configuration directory
                           is for: stop that one server by its exact process
                           id, start mcc-server again, and wait until it
                           answers /health. Every other My Claude Code server
                           is listed and left running.
  --no-start               Never start a server, whatever else was asked. Same
                           as setting MCC_INSTALL_NO_START=1.
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

# How a uv failure is classified from the text uv printed. The same two tables
# exist in scripts/install.ps1, and a contract test compares them, because the
# two installers must reach the same verdict about the same machine.
#
#   disk-full  the volume is out of space. Retrying cannot help, and the
#              Windows installer's locked-file ladder writes MORE files, so it
#              makes a full disk worse. Stop, and say how much room to make.
#   locked     a file is held by another process. That is what Windows'
#              rename-then-reinstall ladder is for, and all it is for.
#   unknown    everything else keeps the historical behaviour.
uv_disk_full_signatures='os error 112|not enough space on the disk|no space left on device|enospc'
uv_locked_signatures='os error 32|access is denied|being used by another process'

# What a complete install costs on disk, so a full-disk failure can say how
# much room to make. Measured on 2026-09-09 in scratch uv directories: the tool
# environment, the launcher shims, managed CPython and the uv cache the install
# fills come to about this much. Ask for more than that: uv unpacks through its
# cache before it hardlinks into place, so the peak is above the resting size.
install_footprint_mb=340
install_recommended_mb=1024

classify_uv_failure() {
    lowered=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
    # Disk-full is tested first on purpose: when a message somehow carries both
    # shapes, the one no retry can fix has to win.
    if printf '%s\n' "$lowered" | grep -Eq "$uv_disk_full_signatures"; then
        printf 'disk-full'
        return 0
    fi
    if printf '%s\n' "$lowered" | grep -Eq "$uv_locked_signatures"; then
        printf 'locked'
        return 0
    fi
    printf 'unknown'
}

report_disk_full() {
    disk_target=$("$uv_bin" tool dir 2>/dev/null) || disk_target=""
    [ -n "$disk_target" ] || disk_target="${UV_TOOL_DIR:-$HOME/.local/share/uv/tools}"
    disk_free_mb=$(df -Pk "$disk_target" 2>/dev/null | awk 'NR==2 {printf "%d", $4 / 1024}')
    [ -n "$disk_free_mb" ] || disk_free_mb="unknown"
    disk_filesystem=$(df -Pk "$disk_target" 2>/dev/null | awk 'NR==2 {print $1}')
    [ -n "$disk_filesystem" ] || disk_filesystem="the install filesystem"
    printf '\n' >&2
    printf 'The install stopped because %s has no space left.\n' "$disk_filesystem" >&2
    printf '  free on %s: %s MB\n' "$disk_filesystem" "$disk_free_mb" >&2
    printf '  this install needs: about %s MB (Python %s, the tool environment and the uv cache it unpacks through); leave %s MB free\n' \
        "$install_footprint_mb" "$PYTHON_VERSION" "$install_recommended_mb" >&2
    printf '  it writes to: %s\n' "$disk_target" >&2
    printf 'A full disk is not a locked file. Retrying writes more files, so the installer stops here instead.\n' >&2
    printf 'Free space on %s and run the install command again.\n' "$disk_filesystem" >&2
    printf 'uv tool install --force removes the previous environment before it writes the new one, so this machine has no mcc-server until that re-run finishes.\n' >&2
    exit 1
}

# Run uv and keep what it said.
#
# uv reports every failure the same way -- a non-zero exit status and a
# sentence on stderr -- so the status alone cannot tell a full disk from a
# locked file. `run` above throws that text away. This keeps it, still shows it
# while the install happens, and reads it before deciding what the failure was.
run_uv_capturing() {
    print_command "$@"
    if [ "$dry_run" -eq 1 ]; then
        return 0
    fi
    write_install_log "+ $*"

    uv_capture_file=$(mktemp "${TMPDIR:-/tmp}/mcc-uv.XXXXXX") || fail "Could not create a temporary file."
    uv_status_file=$(mktemp "${TMPDIR:-/tmp}/mcc-uv-status.XXXXXX") || fail "Could not create a temporary file."
    # A pipeline's $? is the LAST command's, so the exit status travels through
    # a file rather than through the pipe. PIPESTATUS is bash-only and this
    # script runs under /bin/sh.
    # Two destinations: the capture file the failure classifier reads at the
    # end, and this episode's transcript, appended a line at a time WHILE the
    # install happens because the desktop window tails it every tick.
    { "$@" 2>&1; printf '%s' "$?" >"$uv_status_file"; } | tee "$uv_capture_file" |
        while IFS= read -r uv_line; do
            write_install_log "$uv_line"
            printf '%s\n' "$uv_line"
        done
    status=$(cat "$uv_status_file" 2>/dev/null)
    [ -n "$status" ] || status=1
    rm -f "$uv_status_file"

    if [ "$status" -eq 0 ]; then
        rm -f "$uv_capture_file"
        return 0
    fi

    uv_category=$(classify_uv_failure "$(cat "$uv_capture_file" 2>/dev/null)")
    rm -f "$uv_capture_file"
    if [ "$uv_category" = "disk-full" ]; then
        report_disk_full
    fi
    fail "Command failed with exit code $status: $1"
}

cleanup() {
    # The lock first: every other path out of this script is an exit, and a
    # lock that outlives its owner locks the machine out of updating.
    exit_update_lock
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

    # uv's standalone installer writes its binaries to $XDG_DATA_HOME/../bin
    # when XDG_DATA_HOME is set, which is NOT $HOME/.local/bin on a machine
    # that points XDG_DATA_HOME elsewhere. Missing this is how a fresh install
    # ends with "uv was installed, but it is not available on PATH".
    if [ -n "${XDG_DATA_HOME:-}" ]; then
        add_path_entry "${XDG_DATA_HOME%/}/../bin"
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

# The install command for curl on this machine, chosen from the package manager
# that is actually present (/etc/os-release IDs vary too much to trust alone --
# a machine can say "debian" and only have apt-get, or be a container with
# neither). Falls back to a generic sentence when nothing is recognised.
curl_install_hint() {
    if command -v apt-get >/dev/null 2>&1; then
        printf 'sudo apt-get update && sudo apt-get install -y curl'
    elif command -v dnf >/dev/null 2>&1; then
        printf 'sudo dnf install -y curl'
    elif command -v yum >/dev/null 2>&1; then
        printf 'sudo yum install -y curl'
    elif command -v zypper >/dev/null 2>&1; then
        printf 'sudo zypper install -y curl'
    elif command -v pacman >/dev/null 2>&1; then
        printf 'sudo pacman -S --noconfirm curl'
    elif command -v apk >/dev/null 2>&1; then
        printf 'sudo apk add curl'
    elif command -v brew >/dev/null 2>&1; then
        printf 'brew install curl'
    elif command -v pkg >/dev/null 2>&1; then
        printf 'sudo pkg install -y curl'
    else
        printf 'install curl with your system package manager'
    fi
}

# curl is the only prerequisite this installer cannot install for you: it is
# what downloads uv, Python and the release wheel. Say exactly what to run.
require_curl() {
    [ "$dry_run" -eq 1 ] && return 0
    command -v curl >/dev/null 2>&1 && return 0

    printf 'error: curl is required and was not found.\n' >&2
    printf '\n' >&2
    printf 'curl is the one thing this installer cannot install for you -- it is what\n' >&2
    printf 'downloads uv, Python and the My Claude Code release wheel.\n' >&2
    printf '\n' >&2
    printf 'Install it with:\n' >&2
    printf '  %s\n' "$(curl_install_hint)" >&2
    printf '\n' >&2
    printf 'Then run this installer again.\n' >&2
    exit 1
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
    # Pin the absolute path once. Everything after this calls "$uv_bin" instead
    # of a bare uv, so the rest of the install cannot be hijacked by a PATH that
    # changes underneath it -- and works in a shell that never had ~/.local/bin.
    uv_bin=$(command -v uv)
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
            uv_bin=$(command -v uv)
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
            --restart)
                restart_requested=1
                ;;
            --no-start)
                no_start_requested=1
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

# Download PYTHON_VERSION through uv before anything needs an interpreter, so
# a machine with no Python at all installs cleanly. --no-bin and --no-registry
# keep this to a self-contained interpreter under UV_PYTHON_INSTALL_DIR: no
# python/python3 shim is dropped on PATH and, on Windows, no PEP 514 registry
# entry is written. The tool environment finds it by version, not by PATH.
install_managed_python() {
    run "$uv_bin" python install --no-bin --no-registry "$PYTHON_VERSION"
}

install_my_claude_code() {
    resolve_release
    download_verified_release_wheel
    package_url="file://$release_wheel_path"
    spec=$(package_spec "$package_url")

    if [ -n "$torch_backend" ]; then
        run_uv_capturing "$uv_bin" tool install --managed-python --force --refresh-package my-claude-code --python "$PYTHON_VERSION" --torch-backend "$torch_backend" "$spec"
    else
        run_uv_capturing "$uv_bin" tool install --managed-python --force --refresh-package my-claude-code --python "$PYTHON_VERSION" "$spec"
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

    # ONE icon, not two. The desktop app (the .deb, or the tarball's
    # install-desktop.sh) writes my-claude-code-desktop.desktop, and it is the
    # better launcher of the two: it opens the dashboard in its own window,
    # and it installs My Claude Code itself if it is missing. This entry --
    # which only runs `mcc-desktop` -- steps aside for it rather than putting
    # a second, near-identical tile in the applications menu.
    #
    # It steps aside; it does not remove anything. The app's entry belongs to
    # the app's installer, and `scripts/uninstall.sh` is the only thing here
    # that deletes either of them.
    for app_entry in \
        "/usr/share/applications/my-claude-code-desktop.desktop" \
        "$applications_dir/my-claude-code-desktop.desktop"
    do
        if [ -f "$app_entry" ]; then
            printf 'The desktop app is already registered (%s); not adding a second launcher.\n' "$app_entry"
            return 0
        fi
    done

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

# The CFBundleIdentifier the *desktop app* carries -- the real .app, built by
# desktop-shell/installer/macos/build-app.sh and dragged out of the .dmg into
# /Applications. The launcher bundle written below deliberately carries a
# different one (com.my-claude-code.desktop), which is the whole mechanism by
# which the two can be told apart at the same path and under the same name.
# Pinned by tests/contracts/test_uninstaller_parity.py.
MACOS_DESKTOP_APP_IDENTIFIER="com.myclaudecode.desktop"

macos_bundle_is_the_desktop_app() {
    # True when the bundle at $1 is the desktop app rather than the launcher
    # bundle this script writes. A bundle with no readable Info.plist is not
    # the desktop app: build-app.sh always writes one, and treating an
    # unreadable bundle as "the app" would make this script refuse to install
    # its launcher because of a directory somebody left behind.
    _bundle_plist="$1/Contents/Info.plist"
    [ -f "$_bundle_plist" ] || return 1
    grep -Fq "$MACOS_DESKTOP_APP_IDENTIFIER" "$_bundle_plist"
}

create_macos_app_bundle() {
    launcher_path=$1
    app_dir="$HOME/Applications/My Claude Code.app"
    contents_dir="$app_dir/Contents"

    # ONE application, not two. The desktop app (the .dmg) installs
    # "My Claude Code.app" into /Applications -- or into ~/Applications, if
    # that is where the user dragged it -- and it is the better launcher of
    # the two: it opens the dashboard in its own window, and it installs My
    # Claude Code itself if it is missing. This bundle, which only runs
    # `mcc-desktop`, steps aside for it rather than writing a second
    # application with the same name and icon.
    #
    # In ~/Applications it would not merely be a duplicate: it would be an
    # overwrite. The two bundles have the same name, so writing this one over
    # the real app would replace a working application with a shell wrapper.
    #
    # It steps aside; it does not remove anything. The app belongs to whoever
    # dragged it there, and `scripts/uninstall.sh` never deletes it either.
    # This mirrors create_linux_desktop_entry above.
    for app_bundle in \
        "/Applications/My Claude Code.app" \
        "$HOME/Applications/My Claude Code.app"
    do
        if macos_bundle_is_the_desktop_app "$app_bundle"; then
            printf 'The desktop app is already installed (%s); not adding a second launcher.\n' "$app_bundle"
            return 0
        fi
    done
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
    run "$uv_bin" tool update-shell

    if [ "$dry_run" -eq 1 ]; then
        print_command "$uv_bin" tool dir --bin
        printf '+ verify mcc-server, mcc-claude, mcc-codex, mcc-pi, mcc-help, and my-claude-code in the uv tool bin directory\n'
        print_command mcc-server --version
        return 0
    fi

    print_command "$uv_bin" tool dir --bin
    if tool_bin=$("$uv_bin" tool dir --bin); then
        :
    else
        status=$?
        fail "Could not determine the uv tool bin directory (exit code $status)."
    fi
    [ -n "$tool_bin" ] || fail "uv returned an empty tool bin directory."

    # The PATH the USER's next shell will search, captured BEFORE this script
    # puts the tool bin directory at the front of its own. Without this the
    # shadow check below could never fire: add_path_entry prepends $tool_bin,
    # so `command -v` would always answer with our own launcher, whatever else
    # is installed on the machine. What the warning is about is what the user
    # gets when they type the name -- and that is decided by their profile, not
    # by this process.
    shadow_search_path="$PATH"

    add_path_entry "$tool_bin"
    export PATH
    hash -r 2>/dev/null || true

    # Verify the native my-claude-code command family (mcc-*) plus the package
    # name shim, exactly as the post-install reference leads with. The legacy
    # fcc-* aliases resolve through the same distribution, so they exist as soon
    # as these do.
    # Report EVERY missing command at once, not just the first. The Windows
    # installer used to stop at the first miss (and, worse, skip the check
    # altogether in one branch) and so reported "verified" for commands that did
    # not exist. Same honest accounting here keeps the two installers saying the
    # same thing.
    # A different program earlier on PATH answering to one of these names is a
    # fact about PATH order on this machine, not a failed install: the file is
    # where it belongs and every command works when called by its path. Windows
    # used to THROW here (it asked PATH instead of the directory), so a
    # leftover `my-claude-code` shim from an old npm package broke every later
    # install on a machine where nothing was wrong. Warn on both platforms,
    # fail on neither.
    missing_commands=""
    shadowed_commands=""
    for command_name in mcc-server mcc-claude mcc-claude-old mcc-codex mcc-pi \
        mcc-opencode mcc-opencode2 mcc-kilo mcc-commandcode mcc-kimi \
        mcc-qwen mcc-crush \
        mcc-cline mcc-goose mcc-aider mcc-droid mcc-gemini \
        mcc-init mcc-chatgpt-oauth-login mcc-anthropic-oauth-login \
        mcc-compact-log mcc-help mcc-rtk mcc-migrate mcc-apps \
        mcc-desktop my-claude-code; do
        if [ ! -x "$tool_bin/$command_name" ]; then
            if [ -z "$missing_commands" ]; then
                missing_commands="$command_name"
            else
                missing_commands="$missing_commands, $command_name"
            fi
            continue
        fi
        resolved=$(PATH="$shadow_search_path" command -v "$command_name" 2>/dev/null) || resolved=""
        if [ -n "$resolved" ] && [ "$resolved" != "$tool_bin/$command_name" ]; then
            shadowed_commands="$shadowed_commands$command_name -> $resolved
"
        fi
    done
    if [ -n "$missing_commands" ]; then
        printf 'Installed, but these commands are missing: %s\n' "$missing_commands" >&2
        fail "Re-run the install command."
    fi
    warn_about_shadowing_programs "$tool_bin" "$shadowed_commands"

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

warn_about_shadowing_programs() {
    shadow_tool_bin="$1"
    shadow_list="$2"
    [ -n "$shadow_list" ] || return 0

    shadow_npm_prefix=""
    if command -v npm >/dev/null 2>&1; then
        shadow_npm_prefix=$(npm prefix -g 2>/dev/null) || shadow_npm_prefix=""
    fi

    printf '\n' >&2
    shadow_from_npm=0
    printf '%s' "$shadow_list" | while IFS= read -r shadow_line; do
        [ -n "$shadow_line" ] || continue
        printf 'WARNING: Another program earlier on PATH answers to this name: %s\n' "$shadow_line" >&2
    done
    if [ -n "$shadow_npm_prefix" ]; then
        case "$shadow_list" in
            *"$shadow_npm_prefix/bin/"*) shadow_from_npm=1 ;;
        esac
    fi
    printf 'The install itself is fine: every command was verified in %s.\n' "$shadow_tool_bin" >&2
    printf 'Until that other program is removed, or %s comes first on PATH, typing the name above runs it instead.\n' "$shadow_tool_bin" >&2
    if [ "$shadow_from_npm" -eq 1 ]; then
        printf 'That path belongs to npm. An older version of the npm package published this name; remove it with:\n' >&2
        printf '  npm uninstall -g @firedmosquito831/my-claude-code\n' >&2
    fi
}

precompile_bytecode() {
    # Write __pycache__ for the freshly installed tool environment.
    #
    # Measured on Windows, and the same shape everywhere: the FIRST server
    # start after an update costs about 3.5 seconds more than every later one,
    # because CPython compiles every module it imports and writes the .pyc
    # files as it goes. With releases arriving hourly, "the first start after
    # an update" is most starts the user ever sees -- and it is the part of a
    # start that happens before the port is bound.
    #
    # Best effort by design: a failure costs those seconds back and nothing
    # else, so it must never fail an install that otherwise worked.
    if [ "$dry_run" -eq 1 ]; then
        return 0
    fi
    uv_tool_root=$("$uv_bin" tool dir 2>/dev/null) || return 0
    [ -n "$uv_tool_root" ] || return 0
    tool_dir="$uv_tool_root/my-claude-code"
    for python in "$tool_dir/bin/python" "$tool_dir/bin/python3" "$tool_dir/Scripts/python.exe"; do
        if [ -x "$python" ]; then
            printf 'Precompiling My Claude Code (saves a few seconds on the next start)...\n'
            "$python" -m compileall -q "$tool_dir" >/dev/null 2>&1 || true
            return 0
        fi
    done
    return 0
}

mcc_config_dir() {
    # The same three rungs the server walks, in the same order: an explicit
    # MCC_CONFIG_DIR, then ~/.mcc, then a legacy ~/.fcc an older install left
    # behind. A hard-coded path here would write a scratch install's receipt
    # into the real config home.
    if [ -n "${MCC_CONFIG_DIR:-}" ]; then
        printf '%s' "$MCC_CONFIG_DIR"
        return
    fi
    if [ -d "$HOME/.mcc" ]; then
        printf '%s' "$HOME/.mcc"
        return
    fi
    if [ -d "$HOME/.fcc" ]; then
        printf '%s' "$HOME/.fcc"
        return
    fi
    printf '%s' "$HOME/.mcc"
}

mcc_env_setting() {
    # One setting, as this installation's server would read it: the process
    # environment first -- which is what the server itself does, and what keeps
    # a scratch install reading a scratch configuration -- then
    # <config dir>/.env.
    #
    # The installer READS. It never creates the file, never migrates a legacy
    # directory and never writes a default back: an installer that repaired
    # configuration would be a second mcc-init, and the one thing a restart
    # must not do is change what the machine is configured to be while it is
    # installing.
    setting_name=$1
    setting_default=${2:-}
    setting_value=$(printenv "$setting_name" 2>/dev/null || printf '')
    if [ -n "$setting_value" ]; then
        printf '%s' "$setting_value"
        return 0
    fi
    setting_file="$(mcc_config_dir)/.env"
    if [ -f "$setting_file" ]; then
        setting_value=$(
            sed -n "s/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}${setting_name}[[:space:]]*=[[:space:]]*//p" \
                "$setting_file" 2>/dev/null | tail -n 1
        )
        # Strip one matched pair of surrounding quotes, and nothing else: a
        # value is used as text, never evaluated.
        setting_value=${setting_value%\"}
        setting_value=${setting_value#\"}
        setting_value=${setting_value%\'}
        setting_value=${setting_value#\'}
        setting_value=$(printf '%s' "$setting_value" | tr -d '\r')
        if [ -n "$setting_value" ]; then
            printf '%s' "$setting_value"
            return 0
        fi
    fi
    printf '%s' "$setting_default"
}

resolve_server_address() {
    # The host and port of the ONE server this install is for. "Restart" means
    # exactly one server: the one bound to the port of the configuration
    # directory this install is for. Every other My Claude Code server -- other
    # ports, other configuration directories, the user's agent-serving
    # instances -- is listed and never stopped.
    server_port=$(mcc_env_setting PORT 8082)
    case "$server_port" in
        ''|*[!0-9]*) server_port=8082 ;;
    esac
    server_bind_host=$(mcc_env_setting HOST 127.0.0.1)
    # 0.0.0.0 and :: are what the server BINDS, not addresses a health probe
    # can dial.
    case "$server_bind_host" in
        ''|0.0.0.0|::) server_reachable_host=127.0.0.1 ;;
        *) server_reachable_host=$server_bind_host ;;
    esac
}

server_start_budget_seconds() {
    # The desktop shell's own start budget, because it is the same question
    # asked by a different watcher: DESKTOP_SERVER_START_TIMEOUT once per
    # attempt, DESKTOP_SERVER_START_RETRIES attempts. A cold first start was
    # measured at eighteen seconds, so the floor is not decorative.
    start_timeout=$(mcc_env_setting DESKTOP_SERVER_START_TIMEOUT 20)
    case "$start_timeout" in
        ''|*[!0-9]*) start_timeout=20 ;;
    esac
    start_retries=$(mcc_env_setting DESKTOP_SERVER_START_RETRIES 2)
    case "$start_retries" in
        ''|*[!0-9]*|0) start_retries=2 ;;
    esac
    start_budget=$((start_timeout * start_retries))
    [ "$start_budget" -lt 30 ] && start_budget=30
    printf '%s' "$start_budget"
}

update_lock_path() {
    printf '%s' "$(mcc_config_dir)/updates/update.lock"
}

read_update_lock_field() {
    # One field out of the lock record, by text. The lock is a single flat JSON
    # object written by three programs (this script, install.ps1 and the
    # dashboard's helper), so a field is a quoted key followed by its value --
    # no nesting, nothing to parse, and nothing evaluated.
    sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([^\",}]*\)\"\{0,1\}.*/\1/p" \
        "$(update_lock_path)" 2>/dev/null | head -n 1
}

enter_update_lock() {
    # ONE update at a time, whichever path started it (decision Q5).
    #
    # `set -C` makes the redirect O_EXCL: the first writer to reach it wins and
    # everyone else fails, atomically. A lock whose owner is GONE is reclaimed
    # rather than waited on -- the pid decides, exactly as it decides for the
    # helper-alive gate -- because the alternative is a crashed installer
    # locking the machine out of updating for the rest of the day.
    [ "$dry_run" -eq 1 ] && return 0
    lock_file=$(update_lock_path)
    mkdir -p "$(dirname "$lock_file")" 2>/dev/null || return 0
    lock_attempt=0
    while [ "$lock_attempt" -lt 2 ]; do
        lock_attempt=$((lock_attempt + 1))
        if (
            set -C
            printf '{"pid":%s,"started_at":%s,"started_display":"%s","source":"install.sh"}' \
                "$$" \
                "$(date -u +%s 2>/dev/null || printf '0')" \
                "$(date +%H:%M:%S 2>/dev/null || printf '')" \
                > "$lock_file"
        ) 2>/dev/null; then
            holds_update_lock=1
            return 0
        fi
        lock_owner_pid=$(read_update_lock_field pid)
        case "$lock_owner_pid" in
            ''|*[!0-9]*) lock_owner_pid=0 ;;
        esac
        if [ "$lock_owner_pid" -gt 0 ] && [ "$lock_owner_pid" -ne "$$" ] \
            && kill -0 "$lock_owner_pid" 2>/dev/null; then
            return 1
        fi
        rm -f "$lock_file" 2>/dev/null || true
    done
    return 1
}

exit_update_lock() {
    [ "${holds_update_lock:-0}" -eq 1 ] || return 0
    holds_update_lock=0
    rm -f "$(update_lock_path)" 2>/dev/null || true
}

write_watching_instead_notice() {
    # A second installer does not queue and does not install: it names the
    # owner, points at the transcript that owner is writing, and exits 0. Two
    # installers in one tool directory is the collision this lock exists to
    # stop, and "wait for it" is a worse answer than "here is where to look"
    # for a process that can take a quarter of an hour.
    notice_pid=$(read_update_lock_field pid)
    notice_started=$(read_update_lock_field started_display)
    printf '\n'
    if [ -n "$notice_pid" ] && [ -n "$notice_started" ]; then
        printf 'An update is already running (pid %s, started %s) -- watching it instead.\n' \
            "$notice_pid" "$notice_started"
    elif [ -n "$notice_pid" ]; then
        printf 'An update is already running (pid %s) -- watching it instead.\n' "$notice_pid"
    else
        printf 'An update is already running -- watching it instead.\n'
    fi
    notice_progress="$(mcc_config_dir)/updates/progress.json"
    if [ -f "$notice_progress" ]; then
        notice_log=$(
            sed -n 's/.*"log"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
                "$notice_progress" 2>/dev/null | tail -n 1
        )
        [ -n "$notice_log" ] && printf 'It is writing: %s\n' "$notice_log"
    fi
    return 0
}

ask_the_product_about_the_port() {
    # The installer does not decide which processes My Claude Code is allowed
    # to stop. That rule is 6.59.0's and 6.72.2's, both of them Python, and a
    # second opinion written in sh is exactly how a product comes to stop
    # something it should not have: the uv tool environment is a directory
    # literally named my-claude-code, so every launcher's command line contains
    # the product's name.
    #
    # `--format shell` prints KEY=value lines that a `case` reads into named
    # variables. Nothing is eval'd: a shell that evaluates text another process
    # wrote is a shell that runs it.
    ask_launcher=$1
    ask_flag=$2
    mcc_holder_pid=0
    mcc_holder_is_server=0
    mcc_holder_description=""
    mcc_holder_reason=""
    mcc_port_free=0
    mcc_stopped=0
    mcc_message=""
    mcc_other_servers=0
    mcc_other_server_lines=""
    ask_output=$(
        "$ask_launcher" "$ask_flag" "$server_port" --host "$server_reachable_host" \
            --format shell 2>/dev/null
    ) || return 1
    [ -n "$ask_output" ] || return 1
    # Read the lines in THIS shell, from a file. A pipeline would run the loop
    # in a subshell and lose every assignment, which is the classic way a POSIX
    # script silently reads nothing and carries on.
    ask_temp=$(mktemp 2>/dev/null || printf '')
    if [ -z "$ask_temp" ]; then
        return 1
    fi
    printf '%s\n' "$ask_output" > "$ask_temp"
    while IFS= read -r ask_line; do
        case "$ask_line" in
            MCC_HOLDER_PID=*) mcc_holder_pid=${ask_line#MCC_HOLDER_PID=} ;;
            MCC_HOLDER_IS_SERVER=*) mcc_holder_is_server=${ask_line#MCC_HOLDER_IS_SERVER=} ;;
            MCC_HOLDER_DESCRIPTION=*) mcc_holder_description=${ask_line#MCC_HOLDER_DESCRIPTION=} ;;
            MCC_HOLDER_REASON=*) mcc_holder_reason=${ask_line#MCC_HOLDER_REASON=} ;;
            MCC_PORT_FREE=*) mcc_port_free=${ask_line#MCC_PORT_FREE=} ;;
            MCC_STOPPED=*) mcc_stopped=${ask_line#MCC_STOPPED=} ;;
            MCC_MESSAGE=*) mcc_message=${ask_line#MCC_MESSAGE=} ;;
            MCC_OTHER_SERVERS=*) mcc_other_servers=${ask_line#MCC_OTHER_SERVERS=} ;;
            MCC_OTHER_SERVER_*=*)
                mcc_other_server_lines="${mcc_other_server_lines}  ${ask_line#*=}
"
                ;;
        esac
    done < "$ask_temp"
    rm -f "$ask_temp" 2>/dev/null || true
    case "$mcc_holder_pid" in
        ''|*[!0-9]*) mcc_holder_pid=0 ;;
    esac
    return 0
}

report_other_servers() {
    # Every OTHER My Claude Code server, named. None of them is ever stopped:
    # the user runs several on several ports with agents waiting on them.
    [ "${mcc_other_servers:-0}" != "0" ] || return 0
    [ -n "$mcc_other_server_lines" ] || return 0
    printf '\nOther My Claude Code servers are running. None of them is touched:\n'
    printf '%s' "$mcc_other_server_lines"
    return 0
}

version_at_least() {
    # Whether $1 is at least $2, compared field by field as numbers so 6.73.10
    # sorts above 6.73.9. "Cannot read it" is NO: a version this cannot parse
    # must never be treated as new enough to be asked a question that an older
    # build answers by starting a server.
    have=${1#v}
    want=$2
    [ -n "$have" ] || return 1
    index=1
    while [ "$index" -le 3 ]; do
        have_part=$(printf '%s' "$have" | cut -d. -f"$index")
        want_part=$(printf '%s' "$want" | cut -d. -f"$index")
        case "$have_part" in ''|*[!0-9]*) have_part=-1 ;; esac
        case "$want_part" in ''|*[!0-9]*) want_part=0 ;; esac
        [ "$have_part" -lt 0 ] && return 1
        [ "$have_part" -gt "$want_part" ] && return 0
        [ "$have_part" -lt "$want_part" ] && return 1
        index=$((index + 1))
    done
    return 0
}

port_is_occupied() {
    # Whether anything is LISTENING on this address. It identifies nobody,
    # signals nobody and names nobody -- it exists for one case: an installed
    # mcc-server that predates --report-holder and so cannot classify a holder.
    # A free port is safe to start into; an occupied one is reported and left.
    #
    # The socket table first, because it is the direct answer. A connect was the
    # obvious test and is wrong on at least one real machine: a SYN to a closed
    # loopback port there is DROPPED rather than refused, so every free port
    # reads as "in use" and the installer refuses to start anything, anywhere.
    # curl is only the last resort, for a machine with neither tool.
    occupied_host=$1
    occupied_port=$2
    for probe in "ss -ltn" "netstat -ltn" "netstat -an"; do
        # shellcheck disable=SC2086
        command -v ${probe%% *} >/dev/null 2>&1 || continue
        # shellcheck disable=SC2086
        if $probe 2>/dev/null | grep -E "[:.]$occupied_port[[:space:]]" | grep -qi "listen"; then
            return 0
        fi
        # The tool ran and saw no listener on that port. That is an answer.
        # shellcheck disable=SC2086
        $probe >/dev/null 2>&1 && return 1
    done
    # curl exit 7 is "failed to connect", which is a free port. Every other
    # outcome -- an answer, a protocol error, a timeout -- is treated as
    # occupied: "I could not tell" must never be the reading that starts a
    # second server onto somebody else's socket.
    curl -s -o /dev/null -m 2 "http://$occupied_host:$occupied_port/" 2>/dev/null
    [ "$?" -eq 7 ] && return 1
    return 0
}

wait_for_server_health() {
    # THIS is the success condition of a restart. "The install exited 0" is
    # not: on 2026-09-11 two installs exited 0 fifteen minutes apart and the
    # server was down for both of them and after both of them.
    health_url=$1
    health_budget=$2
    health_waited=0
    while [ "$health_waited" -lt "$health_budget" ]; do
        if curl -fsS -o /dev/null -m 5 "$health_url" 2>/dev/null; then
            return 0
        fi
        sleep 1
        health_waited=$((health_waited + 1))
    done
    curl -fsS -o /dev/null -m 5 "$health_url" 2>/dev/null
}

start_server_detached() {
    # Detached, so the server outlives this installer: an installer that held
    # the server open would take it down with itself. Both streams go to this
    # episode's own start log, so a server that dies on its first breath leaves
    # the reason on disk instead of in a terminal that has moved on.
    #
    # MCC_OPEN_BROWSER is deliberately not touched: whatever the configuration
    # says is what the started server does, exactly as if the user had typed
    # mcc-server.
    start_launcher=$1
    start_log=$2
    # All three streams, and </dev/null is not decoration: a child that keeps
    # the installer's stdin -- or, on the Windows twin of this, its stdout pipe
    # -- open holds its caller open too, and a GitHub run: step, a shell
    # pipeline and the update helper all read this script through a pipe.
    if command -v setsid >/dev/null 2>&1; then
        setsid nohup "$start_launcher" < /dev/null > "$start_log" 2>&1 &
    else
        nohup "$start_launcher" < /dev/null > "$start_log" 2>&1 &
    fi
    started_server_pid=$!
    return 0
}

restart_after_install() {
    # Stop the one server this install is for, start the new one, prove it.
    #
    #   1. Read the port and host of the configuration directory this install
    #      is for. Not "the default port" and not "every MCC port".
    #   2. Ask the product what holds that port. An MCC server is ours to stop;
    #      anything else is reported and left alone, and so is a port whose
    #      holder could not be identified.
    #   3. Stop it by exact pid, within its own configured budget, and wait for
    #      the port to come free.
    #   4. Start mcc-server detached.
    #   5. Wait for /health. If it never answers, say so plainly -- V1 has no
    #      staged swap in the installer, so the previous version is no longer
    #      on disk and the honest instruction is to run the installer again.
    resolve_server_address
    restart_health_url="http://$server_reachable_host:$server_port/health"
    step "Restarting the My Claude Code server on port $server_port"
    write_install_log "Restart requested for the server on $server_reachable_host:$server_port."

    restart_launcher=""
    if [ -n "${tool_bin:-}" ] && [ -x "$tool_bin/mcc-server" ]; then
        restart_launcher="$tool_bin/mcc-server"
    elif [ -n "${tool_bin:-}" ] && [ -x "$tool_bin/mcc-server.exe" ]; then
        restart_launcher="$tool_bin/mcc-server.exe"
    elif command -v mcc-server >/dev/null 2>&1; then
        restart_launcher=$(command -v mcc-server)
    fi
    if [ -z "$restart_launcher" ]; then
        restart_message="The server was not restarted: mcc-server was not found after the install."
        printf '%s\n' "$restart_message"
        install_progress_restarted=false
        write_install_progress failed "$restart_message"
        return 1
    fi

    # ONLY of a build that has the question. cli.entrypoints.serve ignores every
    # argument but --version, so an older mcc-server does not fail on
    # --report-holder -- it STARTS A SERVER on the configured port, and the
    # installer waiting for its answer blocks behind it for ever. Measured on
    # the real installer at 20:12 on 2026-09-11.
    if ! version_at_least "${FCC_VERSION:-}" "$RESTART_AWARE_VERSION"; then
        write_install_log "Installed version ${FCC_VERSION:-unknown} predates --report-holder; not asking it."
        mcc_holder_pid=0
        mcc_holder_is_server=0
        mcc_holder_description=""
        mcc_holder_reason=""
        mcc_other_servers=0
        mcc_other_server_lines=""
        restart_report_available=0
    elif ask_the_product_about_the_port "$restart_launcher" --report-holder; then
        restart_report_available=1
    else
        restart_report_available=0
    fi
    if [ "$restart_report_available" -ne 1 ]; then
        # The installed mcc-server predates --report-holder (a pinned --version,
        # or the first install of this release, whose wheel is the one BEFORE
        # it). It cannot classify the port holder, and this script must not try.
        #
        # What it CAN do without classifying anything is ask whether the port is
        # occupied at all -- a connect, which stops nothing and identifies
        # nothing. A refused connection is a free port and safe to start into;
        # anything else is reported and left exactly as it is.
        if port_is_occupied "$server_reachable_host" "$server_port"; then
            restart_message="Port $server_port is in use and this build of mcc-server cannot say by what, so nothing was stopped and nothing was started. Run the installer again once this version is installed, or stop the server yourself and start it with: mcc-server"
            printf '%s\n' "$restart_message"
            write_install_log "$restart_message"
            install_progress_restarted=false
            write_install_progress done "$restart_message"
            return 1
        fi
        printf 'Nothing is listening on port %s; starting the server.\n' "$server_port"
        write_install_log "The installed build cannot classify a port holder, and nothing holds the port; starting."
        start_and_prove_server "$restart_launcher" "$restart_health_url"
        return $?
    fi
    report_other_servers
    install_progress_holder=$mcc_holder_description

    if [ "$mcc_holder_pid" -gt 0 ] && [ "$mcc_holder_is_server" != "1" ]; then
        # A foreign holder of the port is never killed, by any path.
        restart_message="Port $server_port is held by $mcc_holder_description, which is not a My Claude Code server. Nothing was stopped and nothing was started."
        printf '\n%s\n' "$restart_message"
        [ -n "$mcc_holder_reason" ] && printf '  (%s)\n' "$mcc_holder_reason"
        write_install_log "$restart_message"
        install_progress_restarted=false
        write_install_progress done "$restart_message"
        return 1
    fi

    if [ "$mcc_holder_pid" -gt 0 ]; then
        write_install_progress stopping "Stopping the server on port $server_port."
        printf 'Stopping %s.\n' "$mcc_holder_description"
        if ! ask_the_product_about_the_port "$restart_launcher" --stop-holder; then
            restart_message="The server on port $server_port could not be stopped, so nothing was started."
            printf '%s\n' "$restart_message"
            write_install_log "$restart_message"
            install_progress_restarted=false
            write_install_progress failed "$restart_message"
            return 1
        fi
        [ -n "$mcc_message" ] && printf '%s\n' "$mcc_message" && write_install_log "$mcc_message"
        if [ "$mcc_port_free" != "1" ]; then
            restart_message="The server on port $server_port could not be stopped, so nothing was started. $mcc_message"
            printf '%s\n' "$restart_message"
            write_install_log "$restart_message"
            install_progress_restarted=false
            write_install_progress failed "$restart_message"
            return 1
        fi
    else
        printf 'Nothing was listening on port %s; starting the server.\n' "$server_port"
        write_install_log "Nothing held the port; starting the server."
    fi

    start_and_prove_server "$restart_launcher" "$restart_health_url"
    return $?
}

start_and_prove_server() {
    # Start mcc-server detached and wait for /health. The second half of the
    # restart, in a function of its own because two paths reach it -- the
    # ordinary one and the fallback for an installed build that cannot classify
    # a port holder -- and a second copy of "start it and prove it" is a second
    # definition of success.
    restart_launcher=$1
    restart_health_url=$2
    write_install_progress starting "Starting My Claude Code $FCC_VERSION."
    restart_updates_dir="$(mcc_config_dir)/updates"
    mkdir -p "$restart_updates_dir" 2>/dev/null || true
    restart_start_log="$restart_updates_dir/server-start-$(date -u +%Y%m%d-%H%M%S 2>/dev/null || printf 'unknown').log"
    start_server_detached "$restart_launcher" "$restart_start_log"
    printf 'Started mcc-server (pid %s). Waiting for it to answer %s.\n' \
        "$started_server_pid" "$restart_health_url"
    write_install_log "Started mcc-server, pid $started_server_pid; waiting for $restart_health_url."

    if wait_for_server_health "$restart_health_url" "$(server_start_budget_seconds)"; then
        install_progress_restarted=true
        restart_message="My Claude Code $FCC_VERSION is installed and answering on port $server_port."
        printf '%s\n' "$restart_message"
        write_install_log "$restart_message"
        write_install_progress done "$restart_message"
        return 0
    fi

    restart_exit="the process did not exit"
    if ! kill -0 "$started_server_pid" 2>/dev/null; then
        wait "$started_server_pid" 2>/dev/null && restart_exit=0 || restart_exit=$?
    fi
    restart_message="The new server did not answer $restart_health_url. Exit code: $restart_exit. The previous version is no longer installed; run the installer again."
    printf '\n%s\n' "$restart_message"
    printf 'Its output is in: %s\n' "$restart_start_log"
    if [ -f "$restart_start_log" ]; then
        printf 'Last lines:\n'
        tail -n 12 "$restart_start_log" 2>/dev/null || true
    fi
    write_install_log "$restart_message"
    install_progress_restarted=false
    write_install_progress failed "$restart_message"
    return 1
}

install_progress_started=""
install_progress_path=""
install_progress_log=""
install_progress_rank=0

install_stage_rank() {
    # How far through an episode a stage is; 0 for one this build does not
    # know. The same table as config/update_progress.py's
    # UPDATE_PROGRESS_STAGE_ORDER, and a contract test compares them.
    case "$1" in
        # Rank 0: the marker that OPENS an episode, written before any work.
        # The monotonic guard reads rank 0 as "keep the rank you had", so a
        # marker never blocks the stage that follows it.
        episode) printf '0' ;;
        waiting-for-parent) printf '1' ;;
        staging) printf '2' ;;
        stopping) printf '3' ;;
        installing) printf '4' ;;
        verifying) printf '5' ;;
        swapping) printf '6' ;;
        starting|handing-off) printf '7' ;;
        rolling-back) printf '8' ;;
        done|failed|recovered) printf '9' ;;
        *) printf '0' ;;
    esac
}

open_install_progress() {
    # Open this episode's receipt and transcript, once. Never fails the install.
    #
    # MCC_INSTALL_LOG is how a caller that already owns a transcript gets ONE
    # file for the whole episode instead of two half stories; unset, this
    # install opens its own install-<stamp>.log beside the receipt.
    [ -n "$install_progress_path" ] && return 0
    updates_dir="$(mcc_config_dir)/updates"
    mkdir -p "$updates_dir" 2>/dev/null || return 1
    install_progress_started="$(date -u +%s 2>/dev/null || printf '0')"
    if [ -n "${MCC_INSTALL_LOG:-}" ]; then
        install_progress_log="$MCC_INSTALL_LOG"
    else
        install_progress_log="$updates_dir/install-$(date -u +%Y%m%d-%H%M%S 2>/dev/null || printf 'unknown').log"
        : > "$install_progress_log" 2>/dev/null || install_progress_log=""
    fi
    install_progress_path="$updates_dir/progress.json"
    # APPEND. Until 6.73.0 this line truncated the receipt, and at 15:04 on
    # 2026-09-11 the Windows twin of it erased the whole record of the update
    # helper that had finished two minutes earlier -- while a desktop window
    # was supposed to be reading it. An episode is opened by a MARKER record
    # instead, so a reader that arrives a minute late can still find where the
    # current episode begins (decision Q5).
    touch "$install_progress_path" 2>/dev/null || { install_progress_path=""; return 1; }
    printf '{"stage":"episode","message":"An update started.","at":"%s","parent":0,"helper_pid":%s,"started_at":%s,"elapsed_seconds":0,"helper_done":false,"version":"","log":"%s","source":"install.sh","restarted":null,"holder":""}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || printf '')" \
        "$$" \
        "${install_progress_started:-0}" \
        "$install_progress_log" \
        >> "$install_progress_path" 2>/dev/null || true
    return 0
}

write_install_log() {
    # Append one line to this episode's installer transcript, as it happens.
    # One append per line, so a reader in another process sees it immediately
    # and a killed install does not truncate what it already said.
    [ "$dry_run" -eq 1 ] && return 0
    open_install_progress || return 0
    [ -n "$install_progress_log" ] || return 0
    printf '[%s] %s\n' "$(date -u +%H:%M:%S 2>/dev/null || printf '')" "$1" \
        >> "$install_progress_log" 2>/dev/null || true
    return 0
}

write_install_progress() {
    # Append one liveness record to the update receipt this machine shares --
    # the same file, fields and sentences the deferred update helper writes
    # (src/my_claude_code/application/release_updates.py and
    # src/my_claude_code/config/update_progress.py). Until 6.59.0 only the
    # helper wrote it, so a hand-run installer was invisible to every reader:
    # the desktop shell saw no installer in flight and was free to start one of
    # its own into the tool directory this script is writing.
    #
    # Never fails the install: every write is best effort.
    stage="$1"
    message="$2"
    if [ "$dry_run" -eq 1 ]; then
        return 0
    fi
    open_install_progress || return 0
    # Monotonic, exactly as the deferred helper is: an episode only moves
    # forward, so a window can draw the records as a timeline. A stage this
    # table does not know is written rather than dropped.
    stage_rank="$(install_stage_rank "$stage")"
    [ "$stage_rank" -eq 0 ] && stage_rank="$install_progress_rank"
    [ "$stage_rank" -lt "$install_progress_rank" ] && return 0
    install_progress_rank="$stage_rank"
    now_seconds="$(date -u +%s 2>/dev/null || printf '0')"
    elapsed=$((now_seconds - ${install_progress_started:-0}))
    [ "$elapsed" -lt 0 ] && elapsed=0
    helper_done=false
    case "$stage" in
        done|failed|recovered) helper_done=true ;;
    esac
    printf '{"stage":"%s","message":"%s","at":"%s","parent":0,"helper_pid":%s,"started_at":%s,"elapsed_seconds":%s,"helper_done":%s,"version":"%s","log":"%s","source":"install.sh","restarted":%s,"holder":"%s"}
'         "$stage"         "$message"         "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || printf '')"         "$$"         "${install_progress_started:-0}"         "$elapsed"         "$helper_done"         "${FCC_VERSION:-}"         "$install_progress_log"         "${install_progress_restarted:-null}"         "${install_progress_holder:-}"         >> "$install_progress_path" 2>/dev/null || true
    return 0
}

parse_args "$@"
validate_args
add_known_bin_directories

# ONE update at a time, whichever path started it (decision Q5). A second
# installer does not queue and does not install: it names the owner, points at
# the transcript that owner is writing, and exits 0.
if ! enter_update_lock; then
    write_watching_instead_notice
    exit 0
fi

step "Checking installation prerequisites"
require_curl
require_command bash
require_command sh
require_command mktemp

step "Ensuring uv $MIN_UV_VERSION or newer is installed"
ensure_uv

step "Installing Python $PYTHON_VERSION through uv"
install_managed_python

step "Installing or updating My Claude Code"
# From here to the last line, anything that reads the update receipt sees an
# installer in flight and stays out of the way.
write_install_progress installing "Installing the new version."
if ! install_my_claude_code; then
    write_install_progress failed "The install failed."
    exit 1
fi

step "Configuring PATH and verifying My Claude Code"
# The timeline's fourth stage. A hand-run install narrates itself in the same
# vocabulary the deferred helper uses, so a window watching this file shows the
# same sequence whichever installer is running.
write_install_progress verifying "Checking that every command is in place."
configure_and_verify_my_claude_code

precompile_bytecode
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
    printf '  mcc-commandcode         Launch Command Code through the proxy\n'
    printf '  mcc-kimi                Launch Kimi Code through the proxy\n'
    printf '  mcc-qwen                Launch Qwen Code through the proxy\n'
    printf '  mcc-crush               Launch Crush through the proxy\n'
    printf '  mcc-cline               Launch Cline through the proxy\n'
    printf '  mcc-goose               Launch Goose through the proxy\n'
    printf '  mcc-aider               Launch Aider through the proxy\n'
    printf '  mcc-droid               Launch Droid through the proxy\n'
    printf '  mcc-gemini              Launch Gemini CLI through the proxy\n'
    printf '  mcc-desktop             Open the system tray app (desktop)\n'
    printf '\nManage and inspect:\n'
    printf '  mcc-init                Create or repair ~/.mcc/.env\n'
    printf '  mcc-rtk                 Manage the RTK token optimizer\n'
    printf '  mcc-apps                Point desktop apps here (list/status/configure/undo)\n'
    printf '  mcc-help                Show what each command does\n'
    if [ "$enable_desktop" -eq 1 ]; then
        if [ -n "$desktop_launcher_created" ]; then
            printf '\nDesktop launcher: %s\n' "$desktop_launcher_created"
        elif [ -n "$desktop_launcher_error" ]; then
            printf '\nThe desktop launcher was not created: %s.\n' "$desktop_launcher_error"
        fi
    fi
    printf '\nThe legacy fcc-* commands (fcc-server, fcc-claude, ...) remain as aliases.\n'
    printf '\nIf mcc-server is not found, open a new terminal: this install may have added\n'
    printf 'a directory to PATH that shells started earlier cannot see.\n'
    printf '\nTo use an update installed while the server is running, restart the proxy\n'
    printf 'with: mcc-server\n'
fi

# The terminal record. Whichever way the install went, the receipt stops
# saying "installing" and the helper-alive gate reopens for everyone else.
#
# With --restart the terminal record is the RESTART's: `done` with
# `restarted: true` once a listener answers /health on the configured port, and
# `failed` with the child's exit code and the last lines it wrote when it never
# does. "The install exited 0" is not success -- on 2026-09-11 two installs
# exited 0 fifteen minutes apart with the user's server down through both.
if [ "$no_start_requested" -eq 1 ]; then
    printf '\n'
    if [ "$restart_requested" -eq 1 ]; then
        printf 'No server was started: --no-start (or MCC_INSTALL_NO_START=1) overrides --restart.\n'
    else
        printf 'No server was started. Start one with: mcc-server\n'
    fi
    write_install_progress done "The new version is installed."
elif [ "$restart_requested" -eq 1 ] && [ "$dry_run" -ne 1 ]; then
    restart_after_install || true
else
    write_install_progress done "The new version is installed."
fi
