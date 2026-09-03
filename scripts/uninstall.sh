#!/bin/sh
set -eu

# PACKAGE_NAME must equal [project].name in pyproject.toml -- uv installs and
# uninstalls by that name, and anything else silently no-ops. Both names are
# pinned by tests/contracts/test_uninstaller_parity.py.
PACKAGE_NAME="my-claude-code"
# Installs older than 5.14 were published under the free-claude-code name;
# kept as best-effort cleanup. Absence of either tool is acceptable.
LEGACY_PACKAGE_NAME="free-claude-code"
FCC_HOME_DIRNAME=".fcc"
# The new default config directory. On uninstall we purge this and, if still
# present, the legacy ~/.fcc; ~/.fcc-old (the rollback-note dir) is left alone.
MCC_HOME_DIRNAME=".mcc"
RETIRED_HOME_DIRNAME=".fcc-old"
# Must mirror every entry in [project.scripts] + [project.gui-scripts] (the
# same list as Get-LauncherCommands in scripts/install.ps1); pinned by
# tests/contracts/test_uninstaller_parity.py.
FCC_COMMANDS="fcc-server fcc-claude fcc-claude-old fcc-codex fcc-pi fcc-init fcc-chatgpt-oauth-login fcc-compact-log free-claude-code fcc-anthropic-oauth-login fcc-rtk fcc-help fcc-migrate fcc-desktop mcc-server mcc-claude mcc-claude-old mcc-codex mcc-pi mcc-opencode mcc-opencode2 mcc-kilo mcc-commandcode mcc-kimi mcc-qwen mcc-crush mcc-cline mcc-goose mcc-aider mcc-droid mcc-gemini mcc-init mcc-chatgpt-oauth-login mcc-compact-log mcc-anthropic-oauth-login mcc-rtk mcc-help mcc-migrate mcc-desktop my-claude-code"

dry_run=0
uv_tool_bin=""

show_usage() {
    cat <<'USAGE'
Usage: uninstall.sh [options]

Removes the My Claude Code uv tool (plus any legacy Free Claude Code tool)
and deletes ~/.fcc/ after removal is verified.
Does not remove uv, Claude Code, Codex, Pi, the uv-managed Python runtime, or shared PATH entries.

Options:
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

is_missing_uv_tool_error() {
    tool_name=$1
    normalized=$(printf '%s' "$2" | tr '[:upper:]' '[:lower:]')
    case "$normalized" in
        *"$tool_name"*"is not installed"*) return 0 ;;
        *) return 1 ;;
    esac
}

add_path_entry() {
    [ -n "$1" ] || return 0
    case ":$PATH:" in
        *":$1:"*) ;;
        *) PATH="$1:$PATH" ;;
    esac
}

add_known_uv_paths() {
    if [ -n "${XDG_BIN_HOME:-}" ]; then
        add_path_entry "$XDG_BIN_HOME"
    fi
    add_path_entry "$HOME/.local/bin"
    add_path_entry "$HOME/.cargo/bin"
    export PATH
    hash -r 2>/dev/null || true
}

is_fcc_command_running() {
    command_name=$1

    if command -v pgrep >/dev/null 2>&1; then
        if pgrep -x "$command_name" >/dev/null 2>&1; then
            return 0
        fi
        if pgrep -f "(^|/)${command_name}( |$)" >/dev/null 2>&1; then
            return 0
        fi
        return 1
    fi

    ps -A -o comm= 2>/dev/null | grep -qx "$command_name"
}

assert_no_fcc_processes_running() {
    running=""
    for command_name in $FCC_COMMANDS; do
        if is_fcc_command_running "$command_name"; then
            running="${running} ${command_name}"
        fi
    done

    if [ -n "$running" ]; then
        fail "My Claude Code is still running (${running# }). Stop those processes, then rerun uninstall."
    fi
}

initialize_uv_context() {
    add_known_uv_paths

    if [ "$dry_run" -eq 1 ]; then
        print_command uv tool dir --bin
        return 0
    fi

    if ! command -v uv >/dev/null 2>&1; then
        fail "uv is required to remove the My Claude Code tool. Install uv, then rerun this uninstaller; ~/.fcc was not deleted."
    fi

    print_command uv tool dir --bin
    if uv_tool_bin=$(uv tool dir --bin); then
        :
    else
        status=$?
        fail "Could not determine the uv tool bin directory (exit code $status); ~/.fcc was not deleted."
    fi
    [ -n "$uv_tool_bin" ] || fail "uv returned an empty tool bin directory; ~/.fcc was not deleted."
}

uninstall_uv_tool() {
    tool_name=$1

    print_command uv tool uninstall "$tool_name"
    if [ "$dry_run" -eq 1 ]; then
        return 0
    fi

    if output=$(uv tool uninstall "$tool_name" 2>&1); then
        if [ -n "$output" ]; then
            printf '%s\n' "$output"
        fi
        return 0
    else
        status=$?
    fi

    if is_missing_uv_tool_error "$tool_name" "$output"; then
        printf '%s uv tool is already absent; verifying its entry points.\n' "$tool_name"
        return 0
    fi
    if [ -n "$output" ]; then
        printf '%s\n' "$output" >&2
    fi
    fail "uv tool uninstall $tool_name failed with exit code $status; ~/.fcc was not deleted."
}

verify_fcc_commands_removed() {
    if [ "$dry_run" -eq 1 ]; then
        printf '+ verify all My Claude Code entry points are absent from the uv tool bin directory\n'
        return 0
    fi

    remaining=""
    for command_name in $FCC_COMMANDS; do
        command_path="$uv_tool_bin/$command_name"
        if [ -e "$command_path" ] || [ -L "$command_path" ]; then
            remaining="${remaining} ${command_path}"
        fi
    done
    if [ -n "$remaining" ]; then
        fail "My Claude Code entry points remain after uv uninstall:${remaining}; ~/.fcc was not deleted."
    fi
}

purge_config_dir() {
    # Removes one config directory if present. The refuse-while-running guard
    # already ran, so anything still here is safe to delete. The retired
    # ~/.fcc-old dir is reported but never touched: it holds the user's
    # rollback note.
    _dir_name="$1"
    _home="$HOME/$_dir_name"
    if [ ! -e "$_home" ]; then
        return 0
    fi
    if [ "$_dir_name" = "$RETIRED_HOME_DIRNAME" ]; then
        printf 'Leaving %s in place (rollback note).\n' "$_home"
        return 0
    fi
    printf 'Purging %s\n' "$_home"
    run rm -rf "$_home"
    if [ "$dry_run" -eq 0 ] && [ -e "$_home" ]; then
        fail "$_dir_name config directory still exists after deletion: $_home"
    fi
}

purge_fcc_home() {
    purge_config_dir "$MCC_HOME_DIRNAME"
    purge_config_dir "$FCC_HOME_DIRNAME"
    purge_config_dir "$RETIRED_HOME_DIRNAME"
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
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

parse_args "$@"
[ -n "${HOME:-}" ] || fail "HOME is not set; cannot locate My Claude Code data."

step "Checking for running My Claude Code processes"
assert_no_fcc_processes_running

step "Locating the uv-managed My Claude Code installation"
initialize_uv_context

step "Removing the My Claude Code uv tool (and any legacy Free Claude Code tool)"
uninstall_uv_tool "$PACKAGE_NAME"
uninstall_uv_tool "$LEGACY_PACKAGE_NAME"

step "Verifying My Claude Code entry points were removed"
verify_fcc_commands_removed

step "Purging FCC config and data from ~/.fcc"
purge_fcc_home

if [ "$dry_run" -eq 1 ]; then
    printf '\nDry run complete. No changes were made.\n'
else
    printf '\nMy Claude Code has been removed and verified.\n'
    printf 'uv, Claude Code, Codex, Pi, the uv-managed Python runtime, and shared PATH entries were left installed.\n'
fi
