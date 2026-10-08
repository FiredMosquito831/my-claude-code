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

MCC_REPO="FiredMosquito831/my-claude-code"
MCC_LATEST_RELEASE_URL="https://api.github.com/repos/${MCC_REPO}/releases/latest"
PYTHON_VERSION="3.14.0"
MIN_UV_VERSION="0.11.0"
UV_INSTALL_URL="https://astral.sh/uv/install.sh"

# Resolved from the release feed at run time (or from --version).
MCC_VERSION=""
MCC_WHEEL_NAME=""
MCC_WHEEL_URL=""
MCC_WHEEL_SHA256=""

dry_run=0
requested_version=""
voice_nim=0
voice_local=0
voice_all=0
torch_backend=""
enable_rtk=0
enable_desktop=0
# What the caller asked for about the server. MCC_INSTALL_NO_START is the
# environment form of --no-start, for a caller that cannot add a flag.
#
# 7.1.0: THE RESTART IS THE DEFAULT. 6.73.0 shipped it as `--restart`, opt-in,
# and the consequence is the one this series exists to close: an install that
# exits 0 and leaves the machine with no server. The user asked for the default
# on 2026-09-10 and again, bindingly, on 2026-09-13 00:32.
#
#   (nothing)      stop the one server this configuration is for, install,
#                  start mcc-server, wait for /health, then start the desktop
#                  app
#   --no-restart   never stop a running server; start one only if nothing
#                  answers on the configured port
#   --no-start     stop nothing, start nothing (also MCC_INSTALL_NO_START=1)
#   --no-desktop   never start the desktop app (also MCC_INSTALL_NO_DESKTOP=1)
#
# `--restart` is still ACCEPTED and is now a no-op alias for the default, so
# every script, helper and CI leg that passes it keeps working unchanged.
restart_alias_used=0
no_restart_requested=0
no_start_requested=0
no_desktop_requested=0
desktop_app_started=0
# 7.79.1: --allow-root (or MCC_INSTALL_ALLOW_ROOT=1). Without it, a run as
# root through sudo -- root, on behalf of a real SUDO_USER -- is refused; see
# refuse_install_through_sudo. A plain root shell is not affected.
allow_root=0
# 7.79.1: set when the configured port turned out to be answered by a server
# this run did not start and could not stop. The install itself may still
# have happened; the script then ends with exit 1 instead of a success that
# was another server's.
port_held_by_another=0
# The product's own WSL test (config/paths.py, config/claude_discovery.py):
# the kernel's release string names Microsoft. WSL_DISTRO_NAME and WSL_INTEROP
# are the test the sign-in flows use; sudo's env_reset strips those two, the
# kernel string it cannot.
WSL_OSRELEASE_PATH="/proc/sys/kernel/osrelease"
# The first release whose mcc-server understands --report-holder and
# --stop-holder. Older builds do not REFUSE those flags: they ignore every
# argument but --version and start a server.
RESTART_AWARE_VERSION="6.73.0"
restart_report_available=0
[ "${MCC_INSTALL_NO_START:-}" = "1" ] && no_start_requested=1
[ "${MCC_INSTALL_NO_DESKTOP:-}" = "1" ] && no_desktop_requested=1
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

Installs or updates My Claude Code to the latest published release.

Installs a compatible uv if one is missing. It does not install Claude Code,
Codex, or Pi -- install whichever of those you use yourself.

By default this restarts the server. Every install and every update stops the
My Claude Code server on the port this configuration directory is for, waits
for it to close, installs, starts mcc-server again and waits until it answers
/health -- and then opens the desktop app if it is installed here and is not
already running. Every other My Claude Code server on this machine is listed
and left alone. Use --no-restart or --no-start to opt out.

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
  --restart                Accepted and ignored. Restarting is what this
                           installer does by default since 7.1.0; the flag is
                           kept so older scripts keep working.
  --no-restart             Never stop a server that is already running. If
                           nothing answers on the configured port a server is
                           still started, unless --no-start is given too.
  --no-start               Never stop and never start a server, whatever else
                           was asked. Same as setting MCC_INSTALL_NO_START=1.
  --no-desktop             Never start the desktop app. Same as setting
                           MCC_INSTALL_NO_DESKTOP=1.
  --allow-root             Install for root even though this runs through sudo
                           on behalf of another user. Same as setting
                           MCC_INSTALL_ALLOW_ROOT=1. My Claude Code installs per
                           user, so without it a sudo run is refused.
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
    # 7.78.9: sweep what earlier runs left beside uv's tools root on EVERY way
    # out, the failed ones included. It runs just BEFORE the lock is released,
    # and has to: an install that started the moment the lock was free would
    # be staging into that same root under a stamp of its own, which this
    # sweep would take for a leftover. Keeps what this run made and one
    # previous environment; never fails the script.
    if [ "${dry_run:-0}" -ne 1 ] && [ -n "${leftover_sweep_root:-}" ]; then
        remove_update_leftovers "$leftover_sweep_root" "${staged_stamp:-}" || true
    fi
    # Then the lock: every other path out of this script is an exit, and a
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

    temporary_script=$(mktemp "${TMPDIR:-/tmp}/mcc-install.XXXXXX") || fail "Unable to create a temporary file for $label."
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
                restart_alias_used=1
                ;;
            --no-restart)
                no_restart_requested=1
                ;;
            --no-start)
                no_start_requested=1
                ;;
            --no-desktop)
                no_desktop_requested=1
                ;;
            --allow-root)
                allow_root=1
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

refuse_install_through_sudo() {
    # 7.79.1. My Claude Code is a per-user install: uv's tool directory,
    # ~/.local/bin and the configuration home all belong to whoever runs this.
    # Under sudo that is root -- so a global npm install run through sudo,
    # `curl ... | sudo sh` and `sudo mcc update` installed a second, root-owned
    # copy in root's home and started a root-owned server on the configured
    # port, which stopped the user's own server on the way (measured in
    # throwaway containers, specs/INVESTIGATION-NPM-SUDO.md). The user could
    # neither use nor update that copy.
    #
    # Only root acting FOR another user is refused. A root shell, a root VPS and
    # a Docker image have no SUDO_USER (or SUDO_USER=root) and are unaffected.
    # Runs before the update lock and before anything is downloaded, so a
    # refused run touches nothing.
    [ "$(id -u 2>/dev/null || printf '1')" = "0" ] || return 0
    case "${SUDO_USER:-}" in
        ''|root) return 0 ;;
    esac
    [ "$allow_root" -eq 1 ] && return 0
    [ "${MCC_INSTALL_ALLOW_ROOT:-}" = "1" ] && return 0
    resolve_server_address
    {
        printf 'WARNING: this installer is running as root through sudo, on behalf of %s.\n' "$SUDO_USER"
        printf 'My Claude Code installs per user. Run as root it would install a second,\n'
        printf 'root-owned copy in %s that %s cannot use or update, and start a root-owned\n' "${HOME:-/root}" "$SUDO_USER"
        printf 'server on port %s in place of %s'"'"'s own. Nothing was installed.\n' "$server_port" "$SUDO_USER"
        printf '\nTo install for %s, run it again as %s, without sudo:\n' "$SUDO_USER" "$SUDO_USER"
        printf '  curl -fsSL "https://raw.githubusercontent.com/%s/main/scripts/install.sh" | sh\n' "$MCC_REPO"
        printf 'With npm, give npm a folder %s owns and install the package again without sudo:\n' "$SUDO_USER"
        printf '  npm config set prefix ~/.npm-global   (and add ~/.npm-global/bin to PATH)\n'
        printf 'or keep the `mcc` command sudo installed and run `mcc install` as %s.\n' "$SUDO_USER"
        printf '\nTo install for root on purpose, pass --allow-root (or set MCC_INSTALL_ALLOW_ROOT=1).\n'
    } >&2
    exit 1
}

running_under_wsl() {
    # Whether this Linux is WSL, by the product's own test. Under WSL the
    # desktop app to use is the WINDOWS one: mcc-desktop refuses to run here,
    # and a Linux app inside WSL is a second, separate install (7.79.1).
    [ -n "${WSL_DISTRO_NAME:-}" ] && return 0
    [ -n "${WSL_INTEROP:-}" ] && return 0
    [ -r "$WSL_OSRELEASE_PATH" ] || return 1
    grep -qi microsoft "$WSL_OSRELEASE_PATH" 2>/dev/null
}

extract_wheel_digest() {
    # Scope the search to the asset object whose "name" is the wheel we will
    # download: no other object's digest may satisfy it. Every other "name"
    # line (the top-level release name, sibling assets) and the asset's own
    # "browser_download_url" line end the matched scope, so an asset published
    # without a digest yields nothing instead of borrowing a sibling's, and
    # release-body prose can never be mistaken for the asset's digest.
    printf '%s\n' "$1" |
        awk -v wheel_name="$MCC_WHEEL_NAME" '
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
        MCC_VERSION=$requested_version
        MCC_WHEEL_NAME="my_claude_code-${MCC_VERSION}-py3-none-any.whl"
        # A pinned install stays verified whenever the tag-scoped feed publishes
        # a digest for the wheel. Only an unreachable feed downgrades to an
        # explicitly reported unverified download; a readable feed that omits
        # the asset's own digest is refused below rather than trusted.
        tag_feed_url="https://api.github.com/repos/${MCC_REPO}/releases/tags/v${MCC_VERSION}"
        print_command curl -fsSL "$tag_feed_url"
        if release_json=$(curl -fsSL -H "Accept: application/vnd.github+json" "$tag_feed_url" 2>/dev/null); then
            :
        else
            printf 'warning: could not reach the release feed to verify v%s -- proceeding unverified.\n' "$MCC_VERSION" >&2
            digest_known=0
        fi
    else
        # Read even during a dry run: it is a GET that changes nothing, and it
        # is the only way to report the version that would actually install.
        print_command curl -fsSL "$MCC_LATEST_RELEASE_URL"
        release_json=$(curl -fsSL -H "Accept: application/vnd.github+json" "$MCC_LATEST_RELEASE_URL" 2>/dev/null) ||
            fail "Could not reach the release feed to find the latest version."
        MCC_VERSION=$(printf '%s\n' "$release_json" |
            grep -m1 '"tag_name"' |
            sed -e 's/.*"tag_name"[[:space:]]*:[[:space:]]*"//' -e 's/".*//' -e 's/^v//')
        [ -n "$MCC_VERSION" ] ||
            fail "Could not read the latest release version from the release feed."
        MCC_WHEEL_NAME="my_claude_code-${MCC_VERSION}-py3-none-any.whl"
    fi

    if [ "$digest_known" -eq 1 ]; then
        # GitHub publishes a sha256 digest per asset, so the download is still
        # verified even though no checksum is pinned in this script. The release
        # body follows the assets in the payload and often repeats the wheel
        # digest as prose, so the digest is taken only from the asset object
        # whose name matches the wheel; an asset without one refuses loudly
        # rather than borrowing a sibling's.
        MCC_WHEEL_SHA256=$(extract_wheel_digest "$release_json")
        [ -n "$MCC_WHEEL_SHA256" ] ||
            fail "No digest published for this asset (${MCC_WHEEL_NAME} in release v${MCC_VERSION}); refusing to install."
    fi
    MCC_WHEEL_URL="https://github.com/${MCC_REPO}/releases/download/v${MCC_VERSION}/${MCC_WHEEL_NAME}"
}

download_verified_release_wheel() {
    if [ "$dry_run" -eq 1 ]; then
        print_command curl -fsSL "$MCC_WHEEL_URL" -o "<temporary-wheel>"
        if [ -n "$MCC_WHEEL_SHA256" ]; then
            printf '+ verify SHA-256 %s for <temporary-wheel>\n' "$MCC_WHEEL_SHA256"
        else
            printf '+ verify the SHA-256 published for this release\n'
        fi
        release_wheel_path="<verified-release-wheel>"
        return 0
    fi

    temporary_directory=$(mktemp -d "${TMPDIR:-/tmp}/mcc-wheel.XXXXXX") ||
        fail "Unable to create a temporary directory for the My Claude Code release wheel."
    release_wheel_path="$temporary_directory/$MCC_WHEEL_NAME"
    print_command curl -fsSL "$MCC_WHEEL_URL" -o "$release_wheel_path"
    if ! curl -fsSL "$MCC_WHEEL_URL" -o "$release_wheel_path"; then
        fail "Could not download the My Claude Code v$MCC_VERSION release wheel."
    fi
    [ -s "$release_wheel_path" ] ||
        fail "The downloaded My Claude Code release wheel was empty."

    if [ -z "$MCC_WHEEL_SHA256" ]; then
        # Reachable only when a --version install could not read the tag feed;
        # resolve_release refuses a missing digest in every other case. The
        # fail-open was announced there and is repeated here so the user sees
        # it immediately before the install happens.
        printf 'warning: installing My Claude Code v%s WITHOUT checksum verification.\n' "$MCC_VERSION" >&2
        return 0
    fi

    if command -v sha256sum >/dev/null 2>&1; then
        actual_sha256=$(sha256sum "$release_wheel_path")
    elif command -v shasum >/dev/null 2>&1; then
        actual_sha256=$(shasum -a 256 "$release_wheel_path")
    else
        fail "sha256sum or shasum is required to verify the My Claude Code release wheel."
    fi
    actual_sha256=${actual_sha256%% *}
    [ "$actual_sha256" = "$MCC_WHEEL_SHA256" ] ||
        fail "My Claude Code release wheel checksum mismatch; refusing to install."
    printf 'Verified My Claude Code v%s release wheel SHA-256.\n' "$MCC_VERSION"
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

resolve_install_plan() {
    # The release, its verified wheel, and the one uv command every install
    # path runs. Called once per episode: there are two paths that need it --
    # the staged swap and the in-place repair -- and a second resolve would be
    # a second download of the same wheel.
    resolve_release
    download_verified_release_wheel
    package_url="file://$release_wheel_path"
    install_spec=$(package_spec "$package_url")
}

install_my_claude_code() {
    # The in-place install. From 6.82.0 this is the REPAIR path: the ordinary
    # path is the staged swap below, and this runs when there is no tool
    # environment to swap (a first install), when staging could not be built,
    # or when a release adds a launcher uv has to write.
    if [ -z "${install_spec:-}" ]; then
        resolve_install_plan
    fi

    if [ -n "$torch_backend" ]; then
        run_uv_capturing "$uv_bin" tool install --managed-python --force --refresh-package my-claude-code --python "$PYTHON_VERSION" --torch-backend "$torch_backend" "$install_spec"
    else
        run_uv_capturing "$uv_bin" tool install --managed-python --force --refresh-package my-claude-code --python "$PYTHON_VERSION" "$install_spec"
    fi
}

# ===========================================================================
# THE STAGED SWAP (6.82.0). The same shape as scripts/install.ps1 and as the
# 6.72.0 update helper, on all three platforms (decision Q7).
#
# The roots are SIBLINGS of uv's tools root, never children: a child whose name
# does not normalise to a valid package name makes `uv tool list` fail outright
# and list nothing at all. These names are the ones
# src/my_claude_code/config/update_progress.py declares, and a contract test
# compares the three files.
# ===========================================================================
STAGING_ENV_DIRNAME=".mcc-staging"
PREVIOUS_ENV_DIRNAME=".mcc-previous"
PREVIOUS_ENVS_KEPT=1
PACKAGE_ENV_DIRNAME="my-claude-code"

uv_tools_root() {
    "$uv_bin" tool dir 2>/dev/null | head -n 1
}

update_aside_root() {
    # <uv tools root>/../<name>
    aside_tools_root=$1
    aside_name=$2
    [ -n "$aside_tools_root" ] || return 1
    printf '%s/%s' "$(dirname "$aside_tools_root")" "$aside_name"
}

stage_new_environment() {
    # Build the new version beside the running one. Never touches the live
    # environment, so a wheel that cannot be installed costs nothing at all.
    # Sets staged_ok=1 on success. `--force` is deliberately absent: it exists
    # to overwrite a live environment, which is exactly what this path is built
    # never to do.
    staged_ok=0
    staged_reason=""
    staging_root=$(update_aside_root "$1" "$STAGING_ENV_DIRNAME") || return 1
    staging_dir="$staging_root/$staged_stamp"
    staging_bin="$staging_dir/.bin"
    staging_env="$staging_dir/$PACKAGE_ENV_DIRNAME"
    mkdir -p "$staging_bin" 2>/dev/null || {
        staged_reason="could not create $staging_bin"
        return 1
    }

    stage_status_file=$(mktemp "${TMPDIR:-/tmp}/mcc-stage.XXXXXX") || return 1
    stage_capture_file=$(mktemp "${TMPDIR:-/tmp}/mcc-stage-out.XXXXXX") || return 1
    write_install_log "Staging into $staging_dir. The running version is not touched."
    if [ -n "$torch_backend" ]; then
        { UV_TOOL_DIR="$staging_dir" UV_TOOL_BIN_DIR="$staging_bin" "$uv_bin" tool install --managed-python --refresh-package my-claude-code --python "$PYTHON_VERSION" --torch-backend "$torch_backend" "$install_spec" 2>&1; printf '%s' "$?" >"$stage_status_file"; } | tee "$stage_capture_file" |
            while IFS= read -r stage_line; do
                write_install_log "$stage_line"
                printf '%s\n' "$stage_line"
            done
    else
        { UV_TOOL_DIR="$staging_dir" UV_TOOL_BIN_DIR="$staging_bin" "$uv_bin" tool install --managed-python --refresh-package my-claude-code --python "$PYTHON_VERSION" "$install_spec" 2>&1; printf '%s' "$?" >"$stage_status_file"; } | tee "$stage_capture_file" |
            while IFS= read -r stage_line; do
                write_install_log "$stage_line"
                printf '%s\n' "$stage_line"
            done
    fi
    stage_status=$(cat "$stage_status_file" 2>/dev/null)
    [ -n "$stage_status" ] || stage_status=1
    staged_category=$(classify_uv_failure "$(cat "$stage_capture_file" 2>/dev/null)")
    rm -f "$stage_status_file" "$stage_capture_file"
    write_install_log "The staged install exited with $stage_status."

    if [ "$stage_status" -eq 0 ] && [ -d "$staging_env" ]; then
        staged_ok=1
        return 0
    fi
    if [ "$staged_category" = "disk-full" ]; then
        staged_reason="disk-full"
    else
        staged_reason="uv exited $stage_status"
    fi
    rm -rf -- "$staging_dir" 2>/dev/null || true
    write_install_log "Nothing was staged ($staged_reason); the running version is untouched."
    return 1
}

verify_staged_environment() {
    # Run the staged environment once, before it is anywhere near the live
    # path. The gate is EXECUTING the new thing, not trusting an installer's
    # exit code: a wheel that resolves, installs and then cannot import itself
    # is a real failure mode, and it used to be discovered by the user.
    #
    # This runs BEFORE the stop, so a wheel that cannot run costs a download
    # instead of an outage.
    verify_reason="The staged version could not be run."
    # mcc-server and nothing else. The legacy bin/fcc-server was accepted as a
    # fallback until 7.0.0; it is now a tombstone that exits 1, so falling back
    # to it would turn a healthy install into a failed verification.
    staged_server="$staging_env/bin/mcc-server"
    staged_python="$staging_env/bin/python"
    if [ ! -x "$staged_server" ] || [ ! -x "$staged_python" ]; then
        verify_reason="The staged install produced no runnable launcher."
        return 1
    fi
    verify_version_output=$("$staged_server" --version 2>&1) || {
        verify_reason="The staged version did not run: $verify_version_output"
        return 1
    }
    write_install_log "Staged --version said \"$verify_version_output\"."
    verify_import_output=$("$staged_python" -c 'import my_claude_code' 2>&1) || {
        verify_reason="The staged version could not import itself: $verify_import_output"
        return 1
    }
    write_install_log "Staged import succeeded."
    case "$verify_version_output" in
        *"$MCC_VERSION"*) ;;
        *)
            verify_reason="The staged version reported \"$verify_version_output\" rather than $MCC_VERSION."
            return 1
            ;;
    esac
    verify_reason=""
    return 0
}

swap_environment() {
    # Exchange the staged environment with the live one: two directory renames
    # on one filesystem. uv's bin entries are NOT touched -- each is a link or
    # a stub naming <tools root>/my-claude-code/bin/<name>, and it does not
    # care WHICH environment is at that path, so the instant the new one lands
    # there every installed launcher runs the new code (decision Q6,
    # invariant 9).
    swap_tool_dir=$1
    previous_root=$(update_aside_root "$(dirname "$swap_tool_dir")" "$PREVIOUS_ENV_DIRNAME") || return 1
    swap_previous_dir="$previous_root/$staged_stamp"
    swap_aside_env="$swap_previous_dir/$PACKAGE_ENV_DIRNAME"
    mkdir -p "$swap_previous_dir" 2>/dev/null || return 1
    mv "$swap_tool_dir" "$swap_aside_env" 2>/dev/null || {
        write_install_log "The swap failed: the live environment could not be moved aside."
        return 1
    }
    if ! mv "$staging_env" "$swap_tool_dir" 2>/dev/null; then
        write_install_log "The swap failed: the staged environment could not be moved into place."
        mv "$swap_aside_env" "$swap_tool_dir" 2>/dev/null &&
            write_install_log "The previous environment was put back."
        return 1
    fi
    staged_swapped=1
    write_install_log "Swapped. The previous version is at $swap_aside_env."
    # The staging directory is NOT deleted here. What is left in it is an empty
    # shell -- the environment itself has been MOVED out -- but its .bin
    # directory and uv's links are still hundreds of megabytes, and deleting
    # them measured 7.2 s on the Windows twin of this script, every second of
    # it between "the old server stopped" and "the new server started". It is
    # swept after the health gate instead, where nobody is waiting.
    return 0
}

complete_environment_swap() {
    # The tidying the swap leaves behind, and none of it is needed to RUN the
    # new server -- which is why it happens AFTER the start rather than between
    # the stop and the start, where every second of it would be outage.
    #
    # uv writes each launcher in the environment's own bin directory as a
    # script whose shebang is an ABSOLUTE path to that environment's
    # interpreter, and the staged install baked the STAGING path into all of
    # them. After the move those shebangs name a directory that is about to be
    # deleted. The entries in uv's own bin directory are untouched by all of
    # this -- they name <tools root>/my-claude-code/bin/<name>, which is where
    # the new environment now is -- so the server starts fine before this runs
    # and these are fixed behind it.
    swap_tool_dir=$1
    complete_staging_env=$2
    complete_rewritten=0
    for complete_entry in "$swap_tool_dir"/bin/*; do
        if repoint_entry_script "$complete_entry" "$complete_staging_env" "$swap_tool_dir"; then
            complete_rewritten=$((complete_rewritten + 1))
        fi
    done
    write_install_log "Re-pointed $complete_rewritten launcher(s) inside the new environment."

    # uv recorded every entry point under the STAGING bin directory, which is
    # about to be deleted; a receipt left as written would send a later
    # uninstall or upgrade at a path that no longer exists.
    complete_receipt="$swap_tool_dir/uv-receipt.toml"
    if [ -f "$complete_receipt" ] && [ -n "${staged_bin_dir:-}" ]; then
        if sed "s|$staging_bin|$staged_bin_dir|g" "$complete_receipt" \
            > "$complete_receipt.mcc-new" 2>/dev/null; then
            mv "$complete_receipt.mcc-new" "$complete_receipt" 2>/dev/null &&
                write_install_log "Rewrote the receipt entry points to the real bin directory."
        else
            rm -f "$complete_receipt.mcc-new" 2>/dev/null || true
        fi
    fi
    return 0
}

repoint_entry_script() {
    # Rewrite one entry script of the swapped-in environment so its interpreter
    # line names the canonical environment instead of the staging one. Returns
    # 0 when it rewrote the file. Split out of complete_environment_swap in
    # 7.78.9 so the ONE script the start needs can be fixed before the start.
    repoint_entry=$1
    repoint_from=$2
    repoint_to=$3
    [ -f "$repoint_entry" ] || return 1
    head -n 1 "$repoint_entry" 2>/dev/null | grep -q '^#!' || return 1
    grep -q -- "$repoint_from" "$repoint_entry" 2>/dev/null || return 1
    if sed "s|$repoint_from|$repoint_to|g" "$repoint_entry" \
        > "$repoint_entry.mcc-new" 2>/dev/null; then
        chmod 755 "$repoint_entry.mcc-new" 2>/dev/null || true
        mv "$repoint_entry.mcc-new" "$repoint_entry" 2>/dev/null && return 0
    fi
    rm -f "$repoint_entry.mcc-new" 2>/dev/null || true
    return 1
}

missing_launcher_shims() {
    # Commands this release publishes for which no entry exists in uv's bin
    # directory yet. A release that ADDS an entry point cannot be finished by a
    # rename, so it falls through to the in-place install -- against a cache
    # the staging pass just filled.
    #
    # 7.78.9: ONLY THIS PACKAGE'S OWN ENTRY POINTS COUNT. The caller used to
    # pass the environment's own bin/ directory, which also holds the console
    # scripts of every DEPENDENCY (fastapi, uvicorn, typer, tqdm, ...). uv
    # never links those into its bin directory, so every update reported
    # "This release adds fastapi, ..." and ran `uv tool install --force` over
    # the environment the swap had just put in place -- a second full install
    # inside the outage, and on failure `fail` ended the script with the server
    # stopped, nothing restarted and nothing rolled back. The caller now passes
    # the STAGING bin directory: exactly what uv linked for this package. Its
    # entries are symlinks into the staged environment, which the swap moved,
    # so they DANGLE by now -- `-L` counts them, `-f` alone would skip them
    # all. Given an environment's bin/ instead (it holds `python`), only the
    # entry points uv's receipt records count; with no receipt, only the
    # product's own command family.
    missing_bin=$1
    missing_scripts=$2
    missing_names=""
    missing_entry_points=""
    missing_filter=0
    [ -d "$missing_scripts" ] || return 0
    if [ -e "$missing_scripts/python" ] || [ -L "$missing_scripts/python" ]; then
        missing_filter=1
        missing_entry_points=$(package_entry_point_names "$(dirname "$missing_scripts")")
    fi
    for missing_candidate in "$missing_scripts"/*; do
        [ -f "$missing_candidate" ] || [ -L "$missing_candidate" ] || continue
        missing_leaf=$(basename "$missing_candidate")
        case "$missing_leaf" in
            python|python3|python3.*|pip|pip3|activate*|Activate*) continue ;;
        esac
        if [ "$missing_filter" -eq 1 ]; then
            if [ -n "$missing_entry_points" ]; then
                case " $missing_entry_points " in
                    *" $missing_leaf "*) ;;
                    *) continue ;;
                esac
            else
                case "$missing_leaf" in
                    mcc-*|fcc-*|my-claude-code|free-claude-code) ;;
                    *) continue ;;
                esac
            fi
        fi
        [ -e "$missing_bin/$missing_leaf" ] && continue
        [ -L "$missing_bin/$missing_leaf" ] && continue
        missing_names="$missing_names $missing_leaf"
    done
    printf '%s' "${missing_names# }"
}

package_entry_point_names() {
    # The names uv's receipt records as THIS package's entry points, one line,
    # space separated; nothing when the environment has no receipt. uv writes
    # one `{ name = ..., install-path = ... }` per entry point of the installed
    # package and of no other -- a dependency's console scripts are in bin/ but
    # never in the receipt.
    entry_receipt="$1/uv-receipt.toml"
    [ -f "$entry_receipt" ] || return 0
    tr ',{}' '\n\n\n' <"$entry_receipt" 2>/dev/null |
        sed -n 's/^[[:space:]]*install-path[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' |
        sed 's|.*/||' | tr '\n' ' ' | sed 's/ *$//'
}

tool_environment_problem() {
    # Why the installed tool environment cannot be trusted, in $tool_problem;
    # empty when it can, or when there is none ("not installed" is not
    # broken). 7.78.9. The three ways the machine itself says so: the
    # interpreter every launcher runs is missing (`-x` follows the symlink, so
    # a managed Python that was removed counts too), uv's receipt is missing,
    # or uv calls the tool malformed.
    tool_problem=""
    problem_dir=$1
    [ -d "$problem_dir" ] || return 0
    if [ ! -x "$problem_dir/bin/python" ]; then
        tool_problem="it has no runnable bin/python"
    fi
    if [ ! -f "$problem_dir/uv-receipt.toml" ]; then
        tool_problem="${tool_problem:+$tool_problem; }it has no uv-receipt.toml"
    fi
    if [ -z "$tool_problem" ] && [ -n "${uv_bin:-}" ]; then
        problem_name=$(basename "$problem_dir")
        if "$uv_bin" tool list 2>&1 | grep -F "malformed tool \`$problem_name\`" >/dev/null 2>&1; then
            tool_problem="uv tool list reports it as malformed"
        fi
    fi
    return 0
}

move_environment_aside() {
    # Move a tool environment OUT of uv's tools root, to
    # <tools root>/../.mcc-staging/<stamp>-<label>/my-claude-code, where uv
    # never looks. Same filesystem, so a rename. Sets $moved_aside_to.
    aside_source=$1
    aside_tools_root=$2
    aside_stamp=$3
    aside_label=$4
    moved_aside_to=""
    aside_staging_root=$(update_aside_root "$aside_tools_root" "$STAGING_ENV_DIRNAME") || return 1
    aside_container="$aside_staging_root/$aside_stamp-$aside_label"
    aside_suffix=1
    while [ -e "$aside_container/$PACKAGE_ENV_DIRNAME" ]; do
        aside_suffix=$((aside_suffix + 1))
        aside_container="$aside_staging_root/$aside_stamp-$aside_label-$aside_suffix"
    done
    mkdir -p "$aside_container" 2>/dev/null || return 1
    if mv "$aside_source" "$aside_container/$PACKAGE_ENV_DIRNAME" 2>/dev/null; then
        moved_aside_to="$aside_container/$PACKAGE_ENV_DIRNAME"
        return 0
    fi
    rmdir "$aside_container" 2>/dev/null || true
    return 1
}

remove_update_leftovers() {
    # 7.78.9. Runs on EVERY way out of this script (cleanup, the EXIT trap):
    # sweep what earlier runs left in the staging root and keep one previous
    # environment -- the rules the success path already applied, which until
    # now ran only after a new server answered /health, so every failed or
    # killed update left a whole staged environment behind. What THIS run made
    # (its stamp is the prefix) is kept until the next run so a failure can be
    # looked at. Never fails the script.
    leftover_tools_root=$1
    leftover_keep_prefix=$2
    [ -n "$leftover_tools_root" ] || return 0
    leftover_staging_root=$(update_aside_root "$leftover_tools_root" "$STAGING_ENV_DIRNAME") || return 0
    if [ -d "$leftover_staging_root" ]; then
        for leftover_entry in "$leftover_staging_root"/* "$leftover_staging_root"/.[!.]*; do
            [ -d "$leftover_entry" ] || continue
            leftover_name=$(basename "$leftover_entry")
            if [ -n "$leftover_keep_prefix" ]; then
                case "$leftover_name" in
                    "$leftover_keep_prefix"*) continue ;;
                esac
            fi
            if rm -rf -- "$leftover_entry" 2>/dev/null; then
                write_install_log "Removed the leftover $leftover_entry."
            else
                write_install_log "Could not remove $leftover_entry."
            fi
        done
    fi
    leftover_previous_root=$(update_aside_root "$leftover_tools_root" "$PREVIOUS_ENV_DIRNAME") || return 0
    remove_stale_previous_environment "$leftover_previous_root" || true
    return 0
}

undo_in_place_finish() {
    # 7.78.9. The rollback for an in-place finish that failed AFTER a
    # successful swap. `uv tool install --force` removes the environment
    # before it writes the new one, so the swapped-in copy may be gone; the
    # one this run can PROVE works is the previous version the swap moved
    # aside. That goes back (restore_previous_environment, the same rollback
    # the health gate uses), the server this run stopped is started from it,
    # and the script exits non-zero saying so -- instead of `fail` ending the
    # run with the server stopped and nothing put back.
    undo_reason=$1
    undo_headline="The install could not be finished in place: $undo_reason"
    printf '\n%s\n' "$undo_headline"
    write_install_log "$undo_headline"
    if [ -n "${install_progress_log:-}" ]; then
        printf "uv's own output is above and in %s.\n" "$install_progress_log"
    fi
    write_install_progress rolling-back "The install could not be finished, so the previous version is being put back."
    undo_restored=0
    undo_started=0
    undo_attempted=0
    if restore_previous_environment "$staged_tool_dir"; then
        undo_restored=1
    fi
    if [ "$undo_restored" -eq 1 ] && [ "$stage_may_start" -eq 1 ] && [ -x "$staged_tool_dir/bin/python" ]; then
        undo_attempted=1
        undo_log="$(mcc_config_dir)/updates/server-start-rollback-$staged_stamp.log"
        mkdir -p "$(dirname "$undo_log")" 2>/dev/null || true
        start_server_detached "$restart_launcher" "$undo_log"
        printf "Started the previous version's mcc-server (pid %s).\n" "$started_server_pid"
        if wait_for_server_health "$restart_health_url" "$(server_start_budget_seconds)"; then
            undo_started=1
        fi
    fi
    if [ "$undo_restored" -eq 1 ] && [ "$undo_started" -eq 1 ]; then
        undo_message="The previous version was put back and is answering on port $server_port. Nothing was lost; the update did not happen."
        install_progress_restarted=true
    elif [ "$undo_restored" -eq 1 ] && [ "$undo_attempted" -eq 1 ]; then
        undo_message="The previous version was put back, but it did not answer on port $server_port either. Start it with: mcc-server"
        install_progress_restarted=false
    elif [ "$undo_restored" -eq 1 ]; then
        undo_message="The previous version was put back."
        install_progress_restarted=false
    else
        undo_message="The previous version could not be put back. No server was started from $staged_tool_dir. Run the install command again: it repairs a broken install by itself."
        install_progress_restarted=false
    fi
    printf '%s\n' "$undo_message"
    write_install_log "$undo_message"
    write_install_progress recovered "$undo_headline $undo_message"
    exit 1
}

restore_previous_environment() {
    # Put the version that worked back at the canonical path. This is the
    # reason the old environment was renamed rather than deleted.
    restore_tool_dir=$1
    restore_failed_dir="$staging_root/$staged_stamp-failed"
    mkdir -p "$restore_failed_dir" 2>/dev/null || true
    [ -d "$restore_tool_dir" ] && mv "$restore_tool_dir" "$restore_failed_dir/$PACKAGE_ENV_DIRNAME" 2>/dev/null
    if mv "$swap_aside_env" "$restore_tool_dir" 2>/dev/null; then
        write_install_log "The previous environment is back at the canonical path."
        rmdir "$swap_previous_dir" 2>/dev/null || true
        return 0
    fi
    write_install_log "The rollback failed: the previous environment could not be moved back."
    return 1
}

remove_stale_previous_environment() {
    # Keep exactly one previous environment: it is the rollback, and a second
    # one is only disk. Swept after /health answers, never before.
    sweep_root=$1
    [ -d "$sweep_root" ] || return 0
    sweep_index=0
    for sweep_candidate in $(ls -1 "$sweep_root" 2>/dev/null | sort -r); do
        sweep_index=$((sweep_index + 1))
        [ "$sweep_index" -le "$PREVIOUS_ENVS_KEPT" ] && continue
        rm -rf -- "$sweep_root/$sweep_candidate" 2>/dev/null &&
            write_install_log "Removed the superseded previous environment $sweep_candidate."
    done
    return 0
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
    # 7.79.1: WSL gets the server only, even when WSLg reports a display.
    # mcc-desktop refuses to run inside WSL, so a launcher for it would be a
    # menu entry that does nothing; the desktop app to use is the Windows one.
    if running_under_wsl; then
        desktop_launcher_error="this is WSL, which gets the server only -- use the Windows desktop app, or open the dashboard in a Windows browser"
        printf 'Not writing a Linux launcher: %s.\n' "$desktop_launcher_error"
        return 0
    fi
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
    # name shim, exactly as the post-install reference leads with. The retired
    # fcc-* names ship from the same distribution as tombstones, so they exist
    # as soon as these do -- and are never verified by running them, because
    # running one is defined to exit 1.
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
    [ "$installed_version" = "my-claude-code $MCC_VERSION" ] ||
        fail "Expected my-claude-code $MCC_VERSION; found: $installed_version"
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
    # $1 is 6.82.0's, and it is what keeps this OUT of the outage window: on
    # the staged path the environment to compile is the staged one, and it is
    # compiled while the old server is still serving. Compiling after the swap
    # would have put the whole of it between "stopped" and "started", which is
    # the hole this release exists to close.
    if [ -n "${1:-}" ]; then
        tool_dir=$1
    else
        uv_tool_root=$("$uv_bin" tool dir 2>/dev/null) || return 0
        [ -n "$uv_tool_root" ] || return 0
        tool_dir="$uv_tool_root/my-claude-code"
    fi
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
    # 7.79.1: nobody alive holds the lock, and it still could not be taken --
    # the folder (or a lock file left in it) is not this user's to write. An
    # install run as root with this HOME leaves exactly that: a root-owned
    # updates folder. Until now this read as "An update is already running",
    # exit 0, naming a log nobody was writing -- the update path silently dead.
    return 2
}

pid_is_running() {
    # Whether pid $1 exists at all, whoever owns it. `kill -0` cannot tell: for
    # another user's process it fails exactly as it does for a dead one.
    [ -d "/proc/$1" ] && return 0
    command -v ps >/dev/null 2>&1 || return 1
    ps -p "$1" >/dev/null 2>&1
}

path_owner() {
    # The user that owns path $1, or nothing. `ls -ld` is the portable answer
    # (GNU stat and BSD stat disagree about their flags).
    owner_listing=$(ls -ld "$1" 2>/dev/null) || return 0
    printf '%s\n' "$owner_listing" | {
        read -r _mode _links owner_name _rest || true
        printf '%s' "${owner_name:-}"
    }
    return 0
}

write_lock_unavailable_notice() {
    # The honest version of "An update is already running" for the case where
    # none is: whose lock this is, how old, and why it cannot be taken. The
    # caller exits 1 -- nothing was installed, and saying so with exit 0 is
    # how the false notice hid a dead update path.
    unavailable_lock=$(update_lock_path)
    unavailable_dir=$(dirname "$unavailable_lock")
    unavailable_me=$(id -un 2>/dev/null || printf 'this user')
    {
        printf '\nThe update lock could not be taken, and no running update holds it.\n'
        if [ -e "$unavailable_lock" ]; then
            unavailable_pid=$(read_update_lock_field pid)
            unavailable_source=$(read_update_lock_field source)
            unavailable_started=$(read_update_lock_field started_at)
            unavailable_age="age unknown"
            case "$unavailable_started" in
                ''|*[!0-9]*) ;;
                *)
                    unavailable_now=$(date -u +%s 2>/dev/null || printf '0')
                    unavailable_seconds=$((unavailable_now - unavailable_started))
                    if [ "$unavailable_seconds" -lt 0 ]; then
                        unavailable_age="age unknown"
                    elif [ "$unavailable_seconds" -lt 120 ]; then
                        unavailable_age="written ${unavailable_seconds} s ago"
                    else
                        unavailable_age="written $((unavailable_seconds / 60)) min ago"
                    fi
                    ;;
            esac
            unavailable_state="no longer running"
            case "$unavailable_pid" in
                ''|*[!0-9]*) unavailable_pid="unknown" ;;
                *) pid_is_running "$unavailable_pid" && unavailable_state="running, as another user" ;;
            esac
            printf 'Lock: %s\n' "$unavailable_lock"
            printf '  written by %s, pid %s (%s), %s; the file belongs to %s.\n' \
                "${unavailable_source:-an unknown installer}" "$unavailable_pid" "$unavailable_state" \
                "$unavailable_age" "$(path_owner "$unavailable_lock")"
            if [ -w "$unavailable_dir" ]; then
                printf '  %s cannot remove it: it is not a file %s may delete.\n' \
                    "$unavailable_me" "$unavailable_me"
            else
                printf '  %s cannot remove it: %s (owned by %s) is not writable by %s.\n' \
                    "$unavailable_me" "$unavailable_dir" "$(path_owner "$unavailable_dir")" "$unavailable_me"
            fi
        else
            printf 'Lock: %s does not exist, and %s (owned by %s) is not writable by %s.\n' \
                "$unavailable_lock" "$unavailable_dir" "$(path_owner "$unavailable_dir")" "$unavailable_me"
        fi
        printf 'Nothing was installed. An install run as root (for example through sudo) with\n'
        printf 'this home directory leaves root-owned folders like this one. Give it back with\n'
        printf '  sudo chown -R %s "%s"\n' "$unavailable_me" "$(mcc_config_dir)"
        printf 'and run the installer again.\n'
    } >&2
    return 0
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

health_answer() {
    # ONE GET of the /health URL $1, and what it says about who answered
    # (7.79.1):
    #   health_answer_code  the HTTP status; 000 when nothing answered at all
    #   health_answer_pid   the pid the answer named in x-mcc-pid (every
    #                       server since 7.70.0 sends it); "" when it named none
    # A refused or timed-out connect is "nothing answered" and nothing more:
    # port_is_occupied explains why silence cannot be read as "busy" on every
    # machine. Only an actual HTTP answer is evidence of a listener.
    health_answer_code=000
    health_answer_pid=""
    health_answer_headers=$(curl -s -o /dev/null -D - -m 5 "$1" 2>/dev/null) || health_answer_headers=""
    [ -n "$health_answer_headers" ] || return 0
    health_answer_code=$(
        printf '%s\n' "$health_answer_headers" | tr -d '\r' |
            sed -n '1s/^HTTP\/[0-9.]*[[:space:]]*\([0-9][0-9][0-9]\).*/\1/p'
    )
    [ -n "$health_answer_code" ] || health_answer_code=000
    health_answer_pid=$(
        printf '%s\n' "$health_answer_headers" | tr -d '\r' |
            sed -n 's/^[Xx]-[Mm][Cc][Cc]-[Pp][Ii][Dd]:[[:space:]]*\([0-9][0-9]*\)[[:space:]]*$/\1/p' |
            head -n 1
    )
    return 0
}

parent_pid_of() {
    # The parent of pid $1, or nothing when it cannot be read. /proc first
    # (Linux, readable for every user's process), then ps.
    parent_of=""
    if [ -r "/proc/$1/stat" ]; then
        # Field 4, counted after the ")" that closes the command name -- which
        # may itself contain spaces and parentheses, hence the greedy match.
        parent_of=$(sed -n 's/^.*) [A-Za-z] \([0-9][0-9]*\) .*$/\1/p' "/proc/$1/stat" 2>/dev/null)
    fi
    if [ -z "$parent_of" ] && command -v ps >/dev/null 2>&1; then
        parent_of=$(ps -o ppid= -p "$1" 2>/dev/null | tr -d ' ')
    fi
    case "$parent_of" in
        ''|*[!0-9]*) parent_of="" ;;
    esac
    printf '%s' "$parent_of"
}

answer_is_from_started_server() {
    # Whether the server that answered (pid $1) is the one this run started
    # (pid $2) or runs under it -- a launcher may exec its interpreter or spawn
    # it. 0 = ours; 1 = positively not (its parents were walked to the top
    # without meeting ours); 2 = cannot tell (not even its own parent could be
    # read), which the caller treats as ours, exactly as before 7.79.1.
    answer_walk=$1
    answer_started=$2
    answer_depth=0
    while [ "$answer_depth" -lt 32 ]; do
        [ "$answer_walk" = "$answer_started" ] && return 0
        answer_parent=$(parent_pid_of "$answer_walk")
        if [ -z "$answer_parent" ]; then
            [ "$answer_depth" -eq 0 ] && return 2
            return 1
        fi
        case "$answer_parent" in
            0|1) return 1 ;;
        esac
        answer_walk=$answer_parent
        answer_depth=$((answer_depth + 1))
    done
    return 1
}

process_owner() {
    # The user running pid $1, or nothing. ps first; /proc's Uid line where
    # there is no ps (a minimal container image has none).
    owner_of=""
    if command -v ps >/dev/null 2>&1; then
        owner_of=$(ps -o user= -p "$1" 2>/dev/null | tr -d ' ')
    fi
    if [ -z "$owner_of" ] && [ -r "/proc/$1/status" ]; then
        owner_uid=$(sed -n 's/^Uid:[[:space:]]*\([0-9][0-9]*\).*/\1/p' "/proc/$1/status" 2>/dev/null)
        if [ -n "$owner_uid" ]; then
            owner_of=$(id -nu "$owner_uid" 2>/dev/null || printf 'uid %s' "$owner_uid")
        fi
    fi
    printf '%s' "$owner_of"
}

port_answerer() {
    # Who answered the last health_answer, as words: the pid it named and the
    # user running it, when either can be read.
    if [ -z "$health_answer_pid" ]; then
        printf 'a server that does not name its process (an older My Claude Code, or another program)'
        return 0
    fi
    answerer_owner=$(process_owner "$health_answer_pid")
    if [ -n "$answerer_owner" ]; then
        printf 'a My Claude Code server, pid %s, run by %s' "$health_answer_pid" "$answerer_owner"
    else
        printf 'a My Claude Code server, pid %s' "$health_answer_pid"
    fi
}

why_answerer_is_not_stopped() {
    # Why the server that answered the last health_answer is not stopped here.
    # Another user's server cannot be; a server of this user's that the
    # installed mcc-server could not identify (it reads the holder of a port
    # from `ss` or `netstat`, and a minimal image has neither) is not, because
    # which processes may be stopped is the product's decision, not this
    # script's.
    if [ -n "$health_answer_pid" ]; then
        why_owner=$(process_owner "$health_answer_pid")
        why_me=$(id -un 2>/dev/null || printf '')
        if [ -n "$why_owner" ] && [ -n "$why_me" ] && [ "$why_owner" != "$why_me" ]; then
            printf 'another user'"'"'s server (one started through sudo runs as root), which this installer cannot stop'
            return 0
        fi
    fi
    printf 'which the installed mcc-server could not identify as the process holding the port, so this installer does not stop it'
}

stop_hint_for_answerer() {
    # How to stop the server that answered, or a generic sentence.
    if [ -z "$health_answer_pid" ]; then
        printf 'Stop whatever holds port %s' "$server_port"
        return 0
    fi
    hint_owner=$(process_owner "$health_answer_pid")
    hint_me=$(id -un 2>/dev/null || printf '')
    if [ -n "$hint_owner" ] && [ -n "$hint_me" ] && [ "$hint_owner" != "$hint_me" ]; then
        printf 'Stop it (it is %s'"'"'s: sudo kill %s)' "$hint_owner" "$health_answer_pid"
    else
        printf 'Stop it (kill %s)' "$health_answer_pid"
    fi
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

find_server_launcher() {
    # The mcc-server this machine runs. uv's bin directory first, PATH second.
    restart_launcher=""
    if [ -n "${tool_bin:-}" ] && [ -x "$tool_bin/mcc-server" ]; then
        restart_launcher="$tool_bin/mcc-server"
    elif [ -n "${tool_bin:-}" ] && [ -x "$tool_bin/mcc-server.exe" ]; then
        restart_launcher="$tool_bin/mcc-server.exe"
    elif command -v mcc-server >/dev/null 2>&1; then
        restart_launcher=$(command -v mcc-server)
    fi
    [ -n "$restart_launcher" ]
}

installed_server_version() {
    # The version of the mcc-server that is installed RIGHT NOW, read from the
    # launcher itself. 6.82.0 stops the old server BEFORE the swap (decision
    # Q6), so the build that has to answer --report-holder is the one already
    # on disk, not the one being installed. --version is the one argument every
    # build has ever answered; anything else an older mcc-server ignores, and
    # cli.entrypoints.serve then STARTS A SERVER on the configured port.
    installed_version_text=$("$1" --version 2>/dev/null) || return 1
    printf '%s' "$installed_version_text" |
        sed -n 's/.*[^0-9]\([0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\).*/\1/p;s/^\([0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)$/\1/p' |
        head -n 1
}

stop_configured_server() {
    # Stop exactly the one server this install is for, and say what happened in
    # $stop_outcome / $stop_message.
    #
    # "Restart" means exactly one server: the MCC server bound to the PORT of
    # the configuration directory this install is for. Every other MCC server
    # -- other ports, other configuration directories, the user's
    # agent-serving instances -- is listed and never stopped (binding scope
    # decision, 2026-09-11 15:37). A foreign holder is never killed by any path
    # (invariant 1).
    #
    #   stopped           our server was stopped and the port is free
    #   nothing-listening the port was already free
    #   foreign           a non-MCC process holds the port; nothing touched
    #   unclassifiable    the port is busy and this build cannot say by what
    #   held-elsewhere    (7.79.1) the build saw no holder, yet /health answers:
    #                     a server this account cannot see or stop
    #   failed            our server would not stop
    stop_launcher=$1
    stop_launcher_version=$2
    stop_outcome="failed"
    stop_message=""

    if ! version_at_least "$stop_launcher_version" "$RESTART_AWARE_VERSION"; then
        write_install_log "The installed mcc-server (${stop_launcher_version:-version unknown}) predates --report-holder; not asking it."
        stop_report_available=0
    elif ask_the_product_about_the_port "$stop_launcher" --report-holder; then
        stop_report_available=1
    else
        stop_report_available=0
    fi

    if [ "$stop_report_available" -ne 1 ]; then
        # The installed mcc-server cannot classify the port holder, and this
        # script must not try. What it CAN do is ask whether the port is
        # occupied at all -- the socket table, which stops nothing and
        # identifies nothing.
        if port_is_occupied "$server_reachable_host" "$server_port"; then
            stop_outcome="unclassifiable"
            stop_message="Port $server_port is in use and the installed mcc-server cannot say by what, so nothing was stopped and nothing was started."
            return 0
        fi
        stop_outcome="nothing-listening"
        stop_message="Nothing is listening on port $server_port."
        return 0
    fi

    report_other_servers
    install_progress_holder=$mcc_holder_description

    if [ "$mcc_holder_pid" -gt 0 ] && [ "$mcc_holder_is_server" != "1" ]; then
        stop_outcome="foreign"
        stop_message="Port $server_port is held by $mcc_holder_description, which is not a My Claude Code server. Nothing was stopped and nothing was started."
        [ -n "$mcc_holder_reason" ] && stop_message="$stop_message ($mcc_holder_reason)"
        return 0
    fi

    if [ "$mcc_holder_pid" -le 0 ]; then
        # 7.79.1: "nothing" from --report-holder can also mean "nothing THIS
        # account is allowed to see". A user cannot read which process owns
        # another user's socket, so a server started through sudo -- running
        # as root -- reads as a free port, and the installer used to start a
        # server that abandoned its start and then report the root server's
        # answer as its own success. The same happens with the user's OWN
        # server where the installed build cannot read the holder of a port
        # at all (it asks `ss` or `netstat`; a minimal image has neither):
        # the new version was swapped in, the old server kept serving, and
        # the run said "installed and answering". One GET of /health tells
        # "free" from "held": only a real HTTP answer counts, so a dropped SYN
        # never blocks a start.
        health_answer "http://$server_reachable_host:$server_port/health"
        if [ "$health_answer_code" != "000" ]; then
            stop_outcome="held-elsewhere"
            install_progress_holder=$(port_answerer)
            stop_message="Port $server_port is answered by $install_progress_holder -- $(why_answerer_is_not_stopped). Nothing was stopped and nothing was started. $(stop_hint_for_answerer), then start yours with: mcc-server"
            return 0
        fi
        stop_outcome="nothing-listening"
        stop_message="Nothing was listening on port $server_port."
        return 0
    fi

    write_install_progress stopping "Stopping the server on port $server_port."
    printf 'Stopping %s.\n' "$mcc_holder_description"
    if ! ask_the_product_about_the_port "$stop_launcher" --stop-holder; then
        stop_outcome="failed"
        stop_message="The server on port $server_port could not be stopped."
        return 0
    fi
    [ -n "$mcc_message" ] && printf '%s\n' "$mcc_message" && write_install_log "$mcc_message"
    if [ "$mcc_port_free" != "1" ]; then
        stop_outcome="failed"
        stop_message="The server on port $server_port could not be stopped. $mcc_message"
        return 0
    fi
    stop_outcome="stopped"
    stop_message=$mcc_message
    return 0
}

no_stop_verdict() {
    # What --no-restart does instead of stopping anything.
    #
    # --no-restart is a promise, and the promise is that no running server is
    # touched: not classified, not asked to stop, not identified. So this asks
    # the one question that requires touching nothing -- is the port busy? --
    # with the same socket-table check stop_configured_server falls back to.
    #
    #   busy -> left-running: the server that is there keeps serving and
    #           nothing is started. A swap underneath it is still fine: the
    #           launcher shims are version-agnostic, so the running process
    #           keeps its loaded modules and the next start picks up the new
    #           version. That is exactly what every install did before 7.1.0.
    #   free -> nothing-listening: there is nothing to leave running, so the
    #           start still happens. "Do not stop my server" is not "leave this
    #           machine without one".
    if port_is_occupied "$server_reachable_host" "$server_port"; then
        stop_outcome="left-running"
        # "Something", not "a server": nothing was classified, so nothing may be
        # claimed about what is there. That is the whole promise of --no-restart.
        stop_message="Something is already listening on port $server_port and --no-restart was given, so nothing was stopped and nothing was started. Restart your server yourself to pick up this version."
        return 0
    fi
    stop_outcome="nothing-listening"
    stop_message="Nothing is listening on port $server_port."
    return 0
}

desktop_shell_candidates() {
    # Every place a desktop app THIS PRODUCT installed can live on this
    # machine, one per line, whether or not anything is there. The same list as
    # config/desktop_shell.desktop_shell_install_locations() and in the same
    # order: the directory this wheel downloads into (MCC_DESKTOP_SHELL_DIR,
    # else ~/.local/bin), then the native installer's directory.
    if [ -n "${MCC_DESKTOP_SHELL_DIR:-}" ]; then
        printf '%s/MyClaudeCode\n' "${MCC_DESKTOP_SHELL_DIR%/}"
    else
        printf '%s/.local/bin/MyClaudeCode\n' "$HOME"
    fi
    case "$(uname -s 2>/dev/null || printf 'unknown')" in
        Darwin)
            printf '/Applications/MyClaudeCode.app/Contents/MacOS/MyClaudeCode\n'
            ;;
        *)
            printf '/usr/local/bin/MyClaudeCode\n'
            printf '/usr/bin/MyClaudeCode\n'
            ;;
    esac
}

installed_desktop_shells() {
    # The desktop app binaries installed here, one per line.
    #
    # "Installed" is the binary AND the MyClaudeCode.receipt.json this product
    # writes beside it. The receipt is the proof we put the file there; a bare
    # executable with a matching name is somebody else's and is never launched,
    # never counted and never touched. Two file tests: no process started, and
    # in particular NOT `mcc-desktop --print-status`, which costs seconds and
    # cannot answer at all while the environment it runs out of is being
    # replaced.
    desktop_shell_candidates | while IFS= read -r shell_candidate; do
        [ -f "$shell_candidate" ] || continue
        [ -f "${shell_candidate%/*}/MyClaudeCode.receipt.json" ] || continue
        printf '%s\n' "$shell_candidate"
    done
}

desktop_shell_is_running() {
    # Whether one of these exact binaries is already running, BY EXECUTABLE
    # PATH, never by image name. The tool environment on this machine is
    # literally called `my-claude-code` and the product ships twenty-six
    # commands whose names all begin `mcc-`; an image-name or command-line
    # substring match here would sooner or later match something that is not
    # the desktop app.
    #
    # /proc/<pid>/exe is the path, and it is the kernel's answer rather than
    # anything the process chose to call itself. Without /proc (macOS) `ps -o
    # comm=` prints the executable's PATH on Darwin, which is the same fact.
    # A non-empty capture is "found": a `while read` in a pipeline runs in a
    # subshell, so its exit status cannot travel back and a sentinel on stdout
    # is the portable way to carry the answer out.
    desktop_running_hit=$(installed_desktop_shells | while IFS= read -r shell_binary; do
        [ -n "$shell_binary" ] || continue
        if [ -d /proc ]; then
            for proc_entry in /proc/[0-9]*; do
                [ -e "$proc_entry/exe" ] || continue
                proc_exe=$(readlink "$proc_entry/exe" 2>/dev/null) || continue
                if [ "$proc_exe" = "$shell_binary" ]; then
                    printf 'yes'
                    break
                fi
            done
        elif ps -Ao comm= 2>/dev/null | grep -qxF "$shell_binary"; then
            printf 'yes'
        fi
    done)
    [ -n "$desktop_running_hit" ]
}

desktop_skip_reason() {
    # Why the desktop app must not be started here, or nothing when it may be.
    #
    # The rule, in full (binding user addition, 2026-09-13 00:40). The desktop
    # app is started when the installer starts mcc-server, and only when every
    # one of these holds:
    #
    #   1. a server was actually started and answered /health (the caller only
    #      calls this on that branch);
    #   2. --no-start / MCC_INSTALL_NO_START was not given -- there is nothing
    #      for a window to attach to;
    #   3. --no-desktop / MCC_INSTALL_NO_DESKTOP was not given;
    #   4. this is not CI, not WSL (7.79.1: the server only there, whatever
    #      display WSLg reports) and not a headless session (no DISPLAY and no
    #      WAYLAND_DISPLAY on anything but macOS);
    #   5. the desktop shell is installed here, proved by the binary AND the
    #      receipt this product writes beside it;
    #   6. it is NOT already running -- during a helper-driven update the app
    #      is running and watching, and the single-instance plugin would hand a
    #      second launch to the window that is already there.
    if [ "$desktop_allowed" -ne 1 ]; then
        printf 'no server was started or --no-desktop was given'
        return 0
    fi
    case "${CI:-}" in
        ""|0|false|False|FALSE) ;;
        *) printf 'this is CI'; return 0 ;;
    esac
    # 7.79.1: WSL gets the server only, display or not (WSLg reports one).
    if running_under_wsl; then
        printf 'this is WSL, which gets the server only'
        return 0
    fi
    if [ "$(uname -s 2>/dev/null || printf 'unknown')" != "Darwin" ]; then
        if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
            printf 'this session has no display'
            return 0
        fi
    fi
    if [ -z "$(installed_desktop_shells)" ]; then
        printf 'the desktop app is not installed here'
        return 0
    fi
    if desktop_shell_is_running; then
        printf 'it is already running'
        return 0
    fi
    return 0
}

start_desktop_app_if_wanted() {
    # Apply the desktop rule and act on it. Called only after a listener has
    # answered /health on the configured port. Every failure is swallowed: a
    # desktop app that did not open is a nuisance; an install that failed
    # because of one would be a defect.
    desktop_reason=$(desktop_skip_reason)
    if [ -n "$desktop_reason" ]; then
        write_install_log "The desktop app was not started: $desktop_reason."
        return 0
    fi
    desktop_launcher=""
    if [ -n "${tool_bin:-}" ] && [ -x "$tool_bin/mcc-desktop" ]; then
        desktop_launcher="$tool_bin/mcc-desktop"
    elif command -v mcc-desktop >/dev/null 2>&1; then
        desktop_launcher=$(command -v mcc-desktop)
    fi
    if [ -z "$desktop_launcher" ]; then
        write_install_log "The desktop app was not started: mcc-desktop was not found."
        return 0
    fi
    desktop_updates_dir="$(mcc_config_dir)/updates"
    mkdir -p "$desktop_updates_dir" 2>/dev/null || true
    desktop_start_log="$desktop_updates_dir/desktop-start-$(date -u +%Y%m%d-%H%M%S 2>/dev/null || printf 'unknown').log"
    # setsid/nohup and </dev/null for the same reason the server start uses
    # them: a child holding this script's stdin or stdout open holds its caller
    # open too.
    if command -v setsid >/dev/null 2>&1; then
        setsid nohup "$desktop_launcher" < /dev/null > "$desktop_start_log" 2>&1 &
    else
        nohup "$desktop_launcher" < /dev/null > "$desktop_start_log" 2>&1 &
    fi
    desktop_app_started=1
    printf 'Started the desktop app (pid %s).\n' "$!"
    write_install_log "Started mcc-desktop, pid $!."
    return 0
}

restart_after_install() {
    # Stop the one server this install is for, start the new one, prove it.
    # The path taken when nothing was swapped -- a first install, or the
    # in-place repair ladder.
    #
    #   1. Read the port and host of the configuration directory this install
    #      is for. Not "the default port" and not "every MCC port".
    #   2. Ask the product what holds that port. An MCC server is ours to stop;
    #      anything else is reported and left alone, and so is a port whose
    #      holder could not be identified.
    #   3. Stop it by exact pid, within its own configured budget, and wait for
    #      the port to come free.
    #   4. Start mcc-server detached.
    #   5. Wait for /health. A LISTENER ANSWERING is the success condition.
    resolve_server_address
    restart_health_url="http://$server_reachable_host:$server_port/health"
    step "Restarting the My Claude Code server on port $server_port"
    write_install_log "Restart requested for the server on $server_reachable_host:$server_port."

    if ! find_server_launcher; then
        restart_message="The server was not restarted: mcc-server was not found after the install."
        printf '%s\n' "$restart_message"
        install_progress_restarted=false
        write_install_progress failed "$restart_message"
        return 1
    fi

    if [ "$stop_allowed" -eq 1 ]; then
        # On this path the build that answers --report-holder is the one that
        # was just installed, because nothing was swapped and the old
        # environment is gone.
        stop_configured_server "$restart_launcher" "${MCC_VERSION:-}"
    else
        # --no-restart: nothing running is touched. See no_stop_verdict.
        no_stop_verdict
    fi
    case "$stop_outcome" in
        stopped) ;;
        nothing-listening)
            printf 'Nothing was listening on port %s; starting the server.\n' "$server_port"
            write_install_log "Nothing held the port; starting the server."
            ;;
        left-running)
            printf '\n%s\n' "$stop_message"
            write_install_log "$stop_message"
            install_progress_restarted=false
            write_install_progress done "$stop_message"
            return 1
            ;;
        foreign)
            printf '\n%s\n' "$stop_message"
            write_install_log "$stop_message"
            install_progress_restarted=false
            write_install_progress done "$stop_message"
            return 1
            ;;
        held-elsewhere)
            # 7.79.1: the install itself succeeded, but the server it is for
            # cannot run on this port; the script ends with exit 1.
            printf '\n%s\n' "$stop_message"
            write_install_log "$stop_message"
            install_progress_restarted=false
            write_install_progress failed "$stop_message"
            port_held_by_another=1
            return 1
            ;;
        unclassifiable)
            restart_message="$stop_message Run the installer again once this version is installed, or stop the server yourself and start it with: mcc-server"
            printf '%s\n' "$restart_message"
            write_install_log "$restart_message"
            install_progress_restarted=false
            write_install_progress done "$restart_message"
            return 1
            ;;
        *)
            restart_message="$stop_message Nothing was started."
            printf '%s\n' "$restart_message"
            write_install_log "$restart_message"
            install_progress_restarted=false
            write_install_progress failed "$restart_message"
            return 1
            ;;
    esac

    start_and_prove_server "$restart_launcher" "$restart_health_url"
    return $?
}

start_restarted_server() {
    # Start mcc-server detached. Split from the health gate in 6.82.0, and the
    # split is what keeps the outage short: on the staged path the swap is
    # followed by the ordinary post-install work -- PATH, the file-based
    # verification, RTK, the launcher entry -- which was measured at 12.3 s on
    # the Windows twin of this script. Run before the start, every second of it
    # is a second the machine has no server; run beside it, it costs nothing,
    # because the server spends that time booting anyway.
    restart_launcher=$1
    write_install_progress starting "Starting My Claude Code $MCC_VERSION."
    restart_updates_dir="$(mcc_config_dir)/updates"
    mkdir -p "$restart_updates_dir" 2>/dev/null || true
    restart_start_log="$restart_updates_dir/server-start-$(date -u +%Y%m%d-%H%M%S 2>/dev/null || printf 'unknown').log"
    start_server_detached "$restart_launcher" "$restart_start_log"
    printf 'Started mcc-server (pid %s).\n' "$started_server_pid"
    write_install_log "Started mcc-server, pid $started_server_pid."
    return 0
}

confirm_restarted_server() {
    # Wait for /health and write the terminal record. A LISTENER ANSWERING is
    # the success condition; "the install exited 0" is not.
    #
    # $2 = 1 means a rollback is available: on the staged path the version that
    # was working is still on disk, so the honest sentence is "the previous one
    # is being put back", not "run the installer again" -- and the terminal
    # record belongs to the ROLLBACK, which the caller writes.
    restart_health_url=$1
    rollback_available=${2:-0}
    if [ "$rollback_available" -eq 1 ]; then
        no_way_back=" The previous version is still on disk and is being put back."
    else
        no_way_back=" The previous version is no longer installed; run the installer again."
    fi
    printf 'Waiting for it to answer %s.\n' "$restart_health_url"
    write_install_log "Waiting for $restart_health_url."

    if wait_for_server_health "$restart_health_url" "$(server_start_budget_seconds)"; then
        # 7.79.1: an answer is success only when it comes from the server
        # this run started. A server another user started on the same port
        # (through sudo, as root) answers /health too, while the one started
        # here finds the port served and abandons its start -- and that answer
        # used to be reported as "installed and answering". Returns 2, which
        # no caller treats as a reason to roll back: the new version is fine,
        # the port is not ours.
        health_answer "$restart_health_url"
        if [ -n "$health_answer_pid" ] && [ "${started_server_pid:-0}" -gt 0 ]; then
            answer_relation=0
            answer_is_from_started_server "$health_answer_pid" "$started_server_pid" || answer_relation=$?
            if [ "$answer_relation" -eq 1 ]; then
                install_progress_holder=$(port_answerer)
                restart_message="Port $server_port is answered by $install_progress_holder, not by the server this install started (pid $started_server_pid). My Claude Code $MCC_VERSION is installed, but it is not what serves that port. Nothing was stopped. $(stop_hint_for_answerer), then start yours with: mcc-server"
                printf '\n%s\n' "$restart_message"
                if [ -s "${restart_start_log:-}" ]; then
                    printf 'The server this install started wrote (%s):\n' "$restart_start_log"
                    tail -n 12 "$restart_start_log" 2>/dev/null || true
                fi
                write_install_log "$restart_message"
                install_progress_restarted=false
                write_install_progress failed "$restart_message"
                port_held_by_another=1
                return 2
            fi
        fi
        install_progress_restarted=true
        restart_message="My Claude Code $MCC_VERSION is installed and answering on port $server_port."
        printf '%s\n' "$restart_message"
        write_install_log "$restart_message"
        # The server is up. THIS is the moment the desktop app is started --
        # after a listener has answered, never before, so the window has
        # something to attach to on its first probe (binding user addition,
        # 2026-09-13 00:40). The rule and every reason it refuses are in
        # desktop_skip_reason.
        start_desktop_app_if_wanted
        write_install_progress done "$restart_message"
        return 0
    fi

    restart_exit="the process did not exit"
    if ! kill -0 "$started_server_pid" 2>/dev/null; then
        wait "$started_server_pid" 2>/dev/null && restart_exit=0 || restart_exit=$?
    fi
    restart_message="The new server did not answer $restart_health_url. Exit code: $restart_exit.$no_way_back"
    printf '\n%s\n' "$restart_message"
    printf 'Its output is in: %s\n' "$restart_start_log"
    if [ -f "$restart_start_log" ]; then
        printf 'Last lines:\n'
        tail -n 12 "$restart_start_log" 2>/dev/null || true
    fi
    write_install_log "$restart_message"
    install_progress_restarted=false
    if [ "$rollback_available" -ne 1 ]; then
        write_install_progress failed "$restart_message"
    fi
    return 1
}

start_and_prove_server() {
    # The in-place path's shape, where there is no post-install work worth
    # overlapping: nothing was swapped, so the environment the verification
    # checks is the one uv has just written, and it has already been checked by
    # the time this runs.
    start_restarted_server "$1"
    confirm_restarted_server "$2" "${3:-0}"
    return $?
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
'         "$stage"         "$message"         "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || printf '')"         "$$"         "${install_progress_started:-0}"         "$elapsed"         "$helper_done"         "${MCC_VERSION:-}"         "$install_progress_log"         "${install_progress_restarted:-null}"         "${install_progress_holder:-}"         >> "$install_progress_path" 2>/dev/null || true
    return 0
}

parse_args "$@"
validate_args
# Before the lock and before any download: a refused run touches nothing.
refuse_install_through_sudo
add_known_bin_directories

# What this run will do about the server, derived once so no later branch has
# to re-reason about three flags. --no-start is the strongest: it means stop
# nothing and start nothing, so it implies --no-restart and --no-desktop.
stop_allowed=1
start_allowed=1
desktop_allowed=1
if [ "$no_restart_requested" -eq 1 ]; then stop_allowed=0; fi
if [ "$no_start_requested" -eq 1 ]; then
    stop_allowed=0
    start_allowed=0
fi
if [ "$no_desktop_requested" -eq 1 ] || [ "$start_allowed" -eq 0 ]; then desktop_allowed=0; fi

# ONE update at a time, whichever path started it (decision Q5). A second
# installer does not queue and does not install: it names the owner, points at
# the transcript that owner is writing, and exits 0.
#
# 7.79.1: unless NO running update holds the lock and it still could not be
# taken (status 2) -- then it says whose lock it is and why, and exits 1.
if enter_update_lock; then
    :
else
    lock_status=$?
    if [ "$lock_status" -eq 2 ]; then
        write_lock_unavailable_notice
        exit 1
    fi
    write_watching_instead_notice
    exit 0
fi

# What this run will do about the server, in one line, at the top of the
# transcript -- so a reader never has to infer it from what did or did not
# happen three hundred lines later.
if [ "$no_start_requested" -eq 1 ]; then
    write_install_log "Nothing will be stopped and nothing will be started (--no-start)."
elif [ "$stop_allowed" -ne 1 ]; then
    write_install_log "No running server will be stopped (--no-restart); one is started only if nothing answers."
else
    write_install_log "The server on the configured port will be restarted (the default since 7.1.0)."
fi
if [ "$restart_alias_used" -eq 1 ]; then
    write_install_log "--restart was given; it is the default since 7.1.0 and was accepted as a no-op."
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
#
# ===========================================================================
# THE STAGED SWAP (6.82.0). One update path, and this is it.
#
#   staging    build the new environment beside the live one; the old server
#              keeps serving for the whole of it
#   verifying  RUN the staged environment once. A wheel that resolves,
#              installs and cannot import itself used to be discovered by the
#              user. This happens BEFORE the stop, so a bad wheel costs a
#              download rather than an outage
#   stopping   stop EXACTLY the MCC server bound to the configured port of the
#              configuration directory this install is for, by exact pid.
#              Every other MCC server is listed and never touched
#   swapping   two directory renames -- milliseconds, not minutes
#   starting   mcc-server detached, then /health. A LISTENER ANSWERING is the
#              success condition; "the install exited 0" is not
#   rolling-back / recovered  the new one never answered, so the previous
#              environment goes back and IT is started
#
# The in-place `uv tool install --force` below is still here and is still
# correct -- it is now the REPAIR.
# ===========================================================================
resolve_install_plan
staged_swapped=0
staged_ok=0
staged_stamp=$(date -u +%Y%m%d-%H%M%S 2>/dev/null || printf 'unknown')
staged_tools_root=""
staged_tool_dir=""
staged_bin_dir=""
stage_may_start=0
stop_outcome="skipped"
stop_message=""
precompiled_before_swap=0
staged_server_started=0

if [ "$dry_run" -ne 1 ]; then
    staged_tools_root=$(uv_tools_root) || staged_tools_root=""
    [ -n "$staged_tools_root" ] && staged_tool_dir="$staged_tools_root/$PACKAGE_ENV_DIRNAME"
    staged_bin_dir=$("$uv_bin" tool dir --bin 2>/dev/null | head -n 1) || staged_bin_dir=""
    # The end-of-run sweep (cleanup, on every exit) works on this root.
    leftover_sweep_root=$staged_tools_root
    # =======================================================================
    # SELF-HEAL (7.78.9). A broken environment at the canonical path is
    # treated as NOT INSTALLED: moved aside, out of uv's tools root, and the
    # fresh in-place path installs a whole one -- the recovery that brought
    # the reporter's machine back on 2026-10-08, done by the installer itself.
    # Staging beside a broken environment would make it "the previous
    # version": the copy a rollback restores and the sweep keeps.
    # =======================================================================
    staged_broken=0
    if [ -n "$staged_tool_dir" ] && [ -d "$staged_tool_dir" ]; then
        tool_environment_problem "$staged_tool_dir"
        if [ -n "$tool_problem" ]; then
            staged_broken=1
            heal_message="Found a broken My Claude Code environment at $staged_tool_dir: $tool_problem."
            printf '%s\n' "$heal_message"
            write_install_log "$heal_message"
            if move_environment_aside "$staged_tool_dir" "$staged_tools_root" "$staged_stamp" broken; then
                heal_message="Moved it aside to $moved_aside_to and installing a fresh copy in its place."
            else
                heal_message="It could not be moved aside; installing over it in place instead."
            fi
            printf '%s\n' "$heal_message"
            write_install_log "$heal_message"
        fi
    fi
    if [ "$staged_broken" -ne 1 ] && [ -n "$staged_tool_dir" ] && [ -d "$staged_tool_dir" ] && [ -n "$staged_bin_dir" ] && [ -d "$staged_bin_dir" ]; then
        write_install_progress staging "Building the new version beside the running one."
        printf 'Building My Claude Code %s beside the running one; nothing is replaced until it is proved.\n' "$MCC_VERSION"
        stage_new_environment "$staged_tools_root" || true
        if [ "$staged_ok" -ne 1 ] && [ "$staged_reason" = "disk-full" ]; then
            # A staging directory is one more copy of the same files on the
            # same volume. Do not attempt the in-place install.
            write_install_progress failed "The volume is out of space; nothing was installed."
            report_disk_full
        fi
        if [ "$staged_ok" -ne 1 ]; then
            printf 'The new version could not be built beside the old one (%s); installing in place instead.\n' "$staged_reason"
        fi
    else
        write_install_log "There is no existing tool environment to stage beside; installing in place."
    fi
fi

if [ "$staged_ok" -eq 1 ]; then
    # The RECORD for this is written after the stop, not here. Stage ranks are
    # monotonic so a window can draw them as a timeline -- `stopping` is 3 and
    # `verifying` is 5 -- so a `verifying` record written here would make the
    # guard drop the `stopping` record that follows it, which is exactly the
    # defect V1 shipped with. The WORK happens first (a wheel that cannot run
    # must cost a download, not an outage); the receipt reports it in rank
    # order and says so.
    if ! verify_staged_environment; then
        # Nothing has moved and nothing was stopped. The live environment is
        # exactly as it was, so the whole episode cost the user a download. It
        # is NOT a reason to fall through to --force: that would install the
        # wheel that cannot run over the one that can.
        rm -rf -- "$staging_dir" 2>/dev/null || true
        restart_message="$verify_reason Nothing was replaced; the installed version is unchanged and keeps serving."
        printf '\n%s\n' "$restart_message"
        write_install_log "$restart_message"
        install_progress_restarted=false
        write_install_progress failed "$restart_message"
        exit 1
    fi
    printf 'The new version ran; putting it in place.\n'
    precompile_bytecode "$staging_env"
    precompiled_before_swap=1

    resolve_server_address
    restart_health_url="http://$server_reachable_host:$server_port/health"
    if [ "$start_allowed" -eq 1 ] && find_server_launcher; then
        stage_may_start=1
        step "Restarting the My Claude Code server on port $server_port"
        write_install_log "Restart requested for the server on $server_reachable_host:$server_port."
        if [ "$stop_allowed" -eq 1 ]; then
            stop_configured_server "$restart_launcher" "$(installed_server_version "$restart_launcher" || printf '')"
        else
            no_stop_verdict
        fi
        case "$stop_outcome" in
            stopped|nothing-listening) ;;
            failed)
                # The old server is still serving and still owns its
                # environment. Swapping underneath it would leave the machine
                # running one version out of a directory named "previous".
                rm -rf -- "$staging_dir" 2>/dev/null || true
                restart_message="$stop_message Nothing was replaced and nothing was started."
                printf '%s\n' "$restart_message"
                write_install_log "$restart_message"
                install_progress_restarted=false
                write_install_progress failed "$restart_message"
                exit 1
                ;;
            held-elsewhere)
                # 7.79.1: a server this account cannot see or stop answers on
                # the port. As for a foreign holder, the install still happens
                # and nothing is started -- and the run ends with exit 1.
                printf '\n%s\n' "$stop_message"
                write_install_log "$stop_message"
                stage_may_start=0
                port_held_by_another=1
                ;;
            *)
                # Invariant 1: a foreign holder of the port is never killed, by
                # any path. The install still happens -- it replaces files, not
                # processes -- but nothing is stopped and nothing is started.
                # The same is true of `left-running`, which is --no-restart's
                # answer when a server is already there.
                printf '\n%s\n' "$stop_message"
                write_install_log "$stop_message"
                stage_may_start=0
                ;;
        esac
    fi

    write_install_progress verifying "The new version was run before the old one was stopped; it works."
    write_install_progress swapping "Putting the new version in place."
    staged_server_started=0
    if swap_environment "$staged_tool_dir"; then
        # Against the STAGING bin directory -- exactly the entry points uv
        # linked for this package -- never the environment's own bin/, which
        # holds every dependency's console scripts too (7.78.9; see
        # missing_launcher_shims).
        staged_missing=$(missing_launcher_shims "$staged_bin_dir" "$staging_bin")
        if [ -n "$staged_missing" ]; then
            # A release that ADDS a command has no entry anywhere carrying the
            # canonical path for it. That case is finished by uv in place,
            # against a cache the staging pass just filled. The previous
            # environment is already aside, so it is still safe.
            printf 'This release adds %s; uv has to write the launcher(s), so the install is finished in place.\n' "$staged_missing"
            write_install_log "This release adds $staged_missing; finishing in place."
            # 7.78.9: in a SUBSHELL, and with a rollback. `fail` (inside
            # run_uv_capturing) exits; called directly, a failed finish ended
            # the whole script with the server stopped before the swap, the
            # environment possibly emptied by `--force`, nothing started and
            # nothing put back. The subshell turns that exit into a status.
            if ( install_my_claude_code ); then
                finish_failure=""
            else
                finish_failure="uv tool install --force exited non-zero"
            fi
            tool_environment_problem "$staged_tool_dir"
            [ -d "$staged_tool_dir" ] || tool_problem="$staged_tool_dir does not exist"
            if [ -n "$finish_failure" ]; then
                undo_in_place_finish "$finish_failure"
            elif [ -n "$tool_problem" ]; then
                undo_in_place_finish "the environment it left cannot run: $tool_problem"
            fi
        fi
        # Started BEFORE the post-install work rather than after it. The outage
        # ends when a listener answers, so everything between the swap and the
        # start is outage; the verification below checks the canonical install
        # and cannot move ahead of the swap, but it can move beside the boot.
        # 7.78.9: never from an environment with no interpreter; the health
        # gate below then rolls back to the previous version.
        if [ "$stage_may_start" -eq 1 ]; then
            if [ -x "$staged_tool_dir/bin/python" ]; then
                # 7.78.9: the one entry script the start runs gets the
                # canonical interpreter line FIRST. uv's bin entries here are
                # symlinks into <tool dir>/bin, and the staged install wrote
                # the STAGING interpreter into every script there, so a start
                # before the tidy-up below died with "nohup: failed
                # to run command ... mcc-server: No such file or directory"
                # (exit 127) and the health gate rolled back. Every update
                # until now ran the false "This release adds ..." reinstall
                # first, which rewrote them all and hid this; with that gone,
                # the install-smoke Linux leg showed it.
                repoint_entry_script "$staged_tool_dir/bin/mcc-server" "$staging_env" "$staged_tool_dir" || true
                start_restarted_server "$restart_launcher"
                staged_server_started=1
            else
                restart_message="The server was not started: $staged_tool_dir has no runnable bin/python, so mcc-server cannot run from it."
                printf '%s\n' "$restart_message"
                write_install_log "$restart_message"
            fi
        fi
        # AFTER the start: none of it is needed to run the new server, and all
        # of it would otherwise sit inside the outage.
        complete_environment_swap "$staged_tool_dir" "$staging_env"
    else
        rm -rf -- "$staging_dir" 2>/dev/null || true
        staged_ok=0
        printf 'The new version could not be put in place; installing in place instead.\n'
    fi
fi

if [ "$staged_swapped" -ne 1 ]; then
    write_install_progress installing "Installing the new version."
    if ! install_my_claude_code; then
        write_install_progress failed "The install failed."
        exit 1
    fi
fi

step "Configuring PATH and verifying My Claude Code"
# The timeline's fourth stage. A hand-run install narrates itself in the same
# vocabulary the deferred helper uses, so a window watching this file shows the
# same sequence whichever installer is running.
write_install_progress verifying "Checking that every command is in place."
configure_and_verify_my_claude_code

[ "$precompiled_before_swap" -eq 1 ] || precompile_bytecode
enable_rtk_for_agents
create_desktop_shortcut

if [ "$dry_run" -eq 1 ]; then
    printf '\nDry run complete. No changes were made.\n'
else
    printf '\nMy Claude Code %s is installed and verified.\n' "$MCC_VERSION"
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
    printf '\nThe legacy fcc-* commands were retired in 7.0.0: each one now prints the\n'
    printf 'mcc-* name that replaced it and exits 1. They go away entirely in 8.0.0.\n'
    printf '\nIf mcc-server is not found, open a new terminal: this install may have added\n'
    printf 'a directory to PATH that shells started earlier cannot see.\n'
    if [ "$start_allowed" -ne 1 ]; then
        printf '\nNo server was started (--no-start). Start one with: mcc-server\n'
    elif [ "$stop_allowed" -ne 1 ]; then
        printf '\nA server that was already running was left alone (--no-restart). Restart it\n'
        printf 'yourself to pick up this version: mcc-server\n'
    fi
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
    printf 'Nothing was stopped and no server was started: --no-start (or MCC_INSTALL_NO_START=1).\n'
    printf 'Start one with: mcc-server\n'
    write_install_progress done "The new version is installed."
elif [ "$staged_swapped" -eq 1 ]; then
    # The staged path already stopped the one server this install is for and
    # swapped the environment. What is left is the start, the health gate, and
    # the rollback the previous environment was kept for.
    #
    # 7.79.1: the health gate's answer is read once, up front. 0 is a new
    # server answering; 2 is "the port is answered by a server this run did
    # not start" -- the new version is fine and stays, so that is never a
    # rollback; anything else is the rollback it always was.
    staged_confirm=1
    if [ "$stage_may_start" -eq 1 ] && [ "$staged_server_started" -eq 1 ]; then
        if confirm_restarted_server "$restart_health_url" 1; then
            staged_confirm=0
        else
            staged_confirm=$?
        fi
    fi
    if [ "$stage_may_start" -ne 1 ]; then
        restart_message=${stop_message:-"My Claude Code $MCC_VERSION is installed. Start the server with: mcc-server"}
        printf '\n%s\n' "$restart_message"
        install_progress_restarted=false
        if [ "$port_held_by_another" -eq 1 ]; then
            write_install_progress failed "$restart_message"
        else
            write_install_progress done "$restart_message"
        fi
    elif [ "$staged_confirm" -eq 0 ]; then
        # Nothing is deleted until the new server answers, so the copy being
        # swept is never the one a rollback would have needed -- and the sweep
        # itself is out of the outage window, which is why it is here and not
        # beside the swap.
        rm -rf -- "$staging_dir" 2>/dev/null || true
        remove_stale_previous_environment "$(update_aside_root "$staged_tools_root" "$PREVIOUS_ENV_DIRNAME")"
    elif [ "$staged_confirm" -eq 2 ]; then
        # The new version is in place and was never the problem; the previous
        # one stays aside for the ordinary sweep. confirm_restarted_server has
        # said why nothing serves this port and written the terminal record.
        rm -rf -- "$staging_dir" 2>/dev/null || true
    else
        # =====================================================================
        # ROLLBACK. The new version is installed and does not answer, so put
        # the one that did back and start THAT. This is the reason the old
        # environment was renamed rather than deleted.
        # =====================================================================
        write_install_progress rolling-back "The new version did not answer, so the previous one is being put back."
        rollback_started=0
        if restore_previous_environment "$staged_tool_dir"; then
            rollback_log="$(mcc_config_dir)/updates/server-start-rollback-$staged_stamp.log"
            start_server_detached "$restart_launcher" "$rollback_log"
            if wait_for_server_health "$restart_health_url" "$(server_start_budget_seconds)"; then
                rollback_started=1
            fi
            restart_message="The new version was installed but never answered, so the previous version was put back"
            if [ "$rollback_started" -eq 1 ]; then
                restart_message="$restart_message and is answering on port $server_port."
                install_progress_restarted=true
            else
                restart_message="$restart_message, but it could not be started either."
                install_progress_restarted=false
            fi
        else
            restart_message="The new version never answered and the previous version could not be put back. Re-run the install command."
            install_progress_restarted=false
        fi
        printf '\n%s\n' "$restart_message"
        write_install_log "$restart_message"
        write_install_progress recovered "$restart_message"
    fi
elif [ "$dry_run" -ne 1 ]; then
    # 7.1.0: no `--restart` test any more. Every install that installed
    # something ends with a server answering on the configured port, unless the
    # caller asked otherwise -- which is what the two branches above are.
    restart_after_install || true
else
    write_install_progress done "The new version is installed."
fi

# 7.79.1: the configured port is answered by a server this run did not start
# and could not stop (see stop_configured_server and confirm_restarted_server).
# Whatever was installed, the server this install is for is not running, and
# an exit 0 here was how another user's server got reported as this install's
# success.
if [ "$port_held_by_another" -eq 1 ]; then
    exit 1
fi
