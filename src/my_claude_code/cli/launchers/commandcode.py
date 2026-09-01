"""The installed ``mcc-commandcode`` launcher.

Command Code is the first harness MCC cannot serve without touching a file the
user owns. Every other one has an escape: Claude Code takes two environment
variables, Codex takes ephemeral ``-c`` assignments, Pi registers its provider
in process, and the OpenCode family reads an extra config file named by its own
``OPENCODE_CONFIG`` / ``KILO_CONFIG`` variable. Command Code 1.39.0 has none --
its bundled ``dist/cli.mjs`` resolves ``$HOME/.commandcode/providers.json``
(``USERPROFILE`` as the fallback) and reads no other document, accepts no
config path on the command line, and defines no environment variable for one.
So this launcher merges exactly one key, ``provider.mcc``, into that file and
leaves every other byte of it alone. The guarantees -- one owner, one backup
taken before the first edit, an idempotent content-compare, and a reversible
``--disconnect`` -- live in ``config/harness_config_merge.py``.

**The proxy token never lands on disk.** ``providers.json`` refuses a literal
key ("raw secrets don't belong in providers.json") and expands ``"$VAR"`` from
the process environment at request time instead, so the merged block carries
``"$MCC_COMMANDCODE_API_KEY"`` and this launcher sets that variable in the
child process only. The base URL beside it *is* written literally, because
Command Code validates that field with ``new URL(...)`` and substitutes
nothing into it -- it is a loopback address on the user's own machine, not a
credential.
"""

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from my_claude_code.cli.harnesses.catalogue_client import (
    catalogue_model_count,
    defaulted_summary_lines,
    fetch_catalogue_models,
    harness_catalogue,
)
from my_claude_code.cli.harnesses.registry import resolve_harness_binary, spec_for
from my_claude_code.config.harness_config_merge import (
    MergeResult,
    merge_config_path,
    merge_owned_block,
    owned_block,
    remove_owned_block,
    with_base_url,
)
from my_claude_code.config.harnesses import (
    COMMANDCODE_API_KEY_ENV,
    HarnessSpec,
)
from my_claude_code.config.proxy_auth import proxy_auth_token
from my_claude_code.config.server_urls import local_proxy_root_url
from my_claude_code.config.settings import get_settings

from .common import preflight_proxy, run_client_process

HARNESS_ID = "commandcode_cli"

#: MCC's own flag, stripped before Command Code sees argv. It removes MCC's
#: one key from the user's document and exits: the counterpart to a merge that
#: had to happen in a file MCC does not own. Command Code has no flag of this
#: name, so nothing is shadowed.
DISCONNECT_FLAG = "--disconnect"

#: Variables MCC owns in the launched environment. Stripped before they are
#: set so a stale value inherited from a parent shell cannot outrank the one
#: this launch resolved.
_MCC_OWNED_ENV_KEYS = frozenset({COMMANDCODE_API_KEY_ENV})


def launch(argv: Sequence[str] | None = None) -> None:
    """Launch Command Code with MCC declared in its providers.json."""

    spec = spec_for(HARNESS_ID)
    args = list(sys.argv[1:] if argv is None else argv)

    if DISCONNECT_FLAG in args:
        raise SystemExit(disconnect(spec))

    binary_path = resolve_harness_binary(spec)

    if is_passthrough(spec, args):
        # ``command-code update`` and ``--version`` have nothing to do with a
        # provider and must not require a running proxy to answer.
        run_client_process(
            command=[binary_path, *args],
            env=os.environ,
            binary_name=spec.binary,
            display_name=spec.display_name,
            install_hint=spec.install_hint,
        )
        return

    settings = get_settings()
    proxy_root_url = local_proxy_root_url(settings)
    if error := preflight_proxy(proxy_root_url):
        print(
            f"My Claude Code proxy is not reachable at {proxy_root_url}: {error}",
            file=sys.stderr,
        )
        print("Start it in another terminal with: fcc-server", file=sys.stderr)
        raise SystemExit(1)

    merged = merge_provider_block(spec, proxy_root_url, settings.anthropic_auth_token)
    run_client_process(
        command=[binary_path, *args],
        env=build_commandcode_launcher_env(
            merged=merged,
            auth_token=settings.anthropic_auth_token,
            base_env=os.environ,
        ),
        binary_name=spec.binary,
        display_name=spec.display_name,
        install_hint=spec.install_hint,
    )


def is_passthrough(spec: HarnessSpec, argv: Sequence[str]) -> bool:
    """Return whether argv must reach the CLI with no MCC configuration."""

    return bool(argv) and (
        argv[0] in spec.passthrough_commands or argv[0] in spec.passthrough_flags
    )


def config_path_for(spec: HarnessSpec, env: Mapping[str, str] | None = None) -> Path:
    """Return the providers.json this CLI will actually read."""

    catalogue = spec.catalogue
    if catalogue is None or catalogue.merge is None:
        raise ValueError(f"{spec.id} declares no config merge")
    return merge_config_path(catalogue.merge, env if env is not None else os.environ)


def merge_provider_block(
    spec: HarnessSpec, proxy_root_url: str, auth_token: str
) -> bool:
    """Write MCC's one key into the user's providers.json.

    Returns whether the CLI can be launched with an MCC provider at all. Every
    failure -- an unreachable proxy, an older server without the capability
    route, an empty catalogue, an unwritable file -- degrades to launching
    without the provider rather than refusing to launch. A picker missing
    MCC's models is a worse session; a session that will not start is no
    session at all.
    """

    catalogue = spec.catalogue
    if catalogue is None or catalogue.merge is None:
        return False
    try:
        payload = fetch_catalogue_models(proxy_root_url, auth_token)
        document = harness_catalogue(payload, spec.id)
        if catalogue_model_count(payload, spec.id) == 0:
            print(
                f"My Claude Code warning: the {spec.display_name} model list is "
                "empty; launching without an MCC provider.",
                file=sys.stderr,
            )
            return False
        block = with_base_url(
            owned_block(document, catalogue.merge.owned_key_path), proxy_root_url
        )
        result = merge_owned_block(
            path=config_path_for(spec),
            owned_key_path=catalogue.merge.owned_key_path,
            block=block,
            backup_suffix=catalogue.merge.backup_suffix,
        )
    except Exception as exc:
        print(
            f"My Claude Code warning: could not update the {spec.display_name} "
            f"config ({exc}); launching without an MCC provider.",
            file=sys.stderr,
        )
        return False
    _print_merge_summary(spec, result)
    _print_defaulted_summary(spec, document)
    return True


def disconnect(spec: HarnessSpec) -> int:
    """Remove MCC's key from the user's providers.json and return an exit code."""

    catalogue = spec.catalogue
    if catalogue is None or catalogue.merge is None:
        print(f"{spec.display_name} has no MCC-owned config key.", file=sys.stderr)
        return 0
    path = config_path_for(spec)
    try:
        result = remove_owned_block(
            path=path,
            owned_key_path=catalogue.merge.owned_key_path,
            backup_suffix=catalogue.merge.backup_suffix,
        )
    except OSError as exc:
        print(f"Could not update {path}: {exc}", file=sys.stderr)
        return 1
    key = catalogue.merge.owned_key_label
    if result.changed:
        print(f"Removed {key} from {path}. Every other key is unchanged.")
    else:
        print(f"{path} carries no {key} block; nothing to remove.")
    return 0


def _print_merge_summary(spec: HarnessSpec, result: MergeResult) -> None:
    """Say what was written to a file MCC does not own, every time it happens."""

    catalogue = spec.catalogue
    if catalogue is None or catalogue.merge is None or not result.changed:
        return
    key = catalogue.merge.owned_key_label
    verb = "Created" if result.created else "Updated"
    print(f"My Claude Code: {verb} {key} in {result.path}.", file=sys.stderr)
    if result.backup_path is not None:
        print(
            f"My Claude Code: your previous file was copied to "
            f"{result.backup_path} before the first edit.",
            file=sys.stderr,
        )


def _print_defaulted_summary(spec: HarnessSpec, document: Mapping[str, object]) -> None:
    """Say which figures nobody published, where the user is already looking."""

    lines = defaulted_summary_lines(document)
    if not lines:
        return
    print(
        f"My Claude Code: {len(lines)} model(s) publish no value for one or more "
        f"fields, so {spec.display_name} falls back to its own default:",
        file=sys.stderr,
    )
    for line in lines:
        print(line, file=sys.stderr)


def build_commandcode_launcher_env(
    *,
    merged: bool,
    auth_token: str,
    base_env: Mapping[str, str],
) -> dict[str, str]:
    """Return an environment that lets the merged provider block authenticate."""

    env = {
        key: value for key, value in base_env.items() if key not in _MCC_OWNED_ENV_KEYS
    }
    if not merged:
        return env
    env[COMMANDCODE_API_KEY_ENV] = proxy_auth_token(auth_token)
    return env
