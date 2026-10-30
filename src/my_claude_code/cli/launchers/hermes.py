"""The installed ``mcc-hermes`` launcher.

Hermes Agent is the third harness MCC cannot serve without touching a file the
user owns, and the first whose file is **YAML**. Nous Research's ``hermes``
resolves exactly one document -- ``$HERMES_HOME/config.yaml``, else
``%LOCALAPPDATA%\\hermes\\config.yaml`` on Windows, else
``~/.hermes/config.yaml`` (``hermes_constants.py:46-67`` of 0.19.0) -- and
publishes no *extra*-config lever. The one it does publish, ``HERMES_HOME``,
was measured and rejected: it moves sessions, skills, plugins and credentials
too, which is the same argument that rejected Kimi Code's ``KIMI_SHARE_DIR``.

So this launcher merges exactly one block, ``providers.mcc``, into that file
and leaves every other byte of it alone -- including the comments, the quoting
and the indentation, because ``config/harness_config_merge.py`` edits the
document as text through the nested-table YAML engine 7.117.0 added.

**The proxy token never lands on disk.** ``providers.mcc.key_env`` carries the
*name* ``MCC_HERMES_API_KEY`` and Hermes calls ``os.environ[key_env]`` at
request time; this launcher sets that variable in the child process only. The
base URL beside it *is* written literally -- a loopback address on the user's
own machine, not a credential -- because Hermes validates it as an absolute URL
and drops the whole provider when it does not parse.

**The default model is written, not passed.** Hermes' own ``-m/--model`` is a
per-invocation override (``hermes_cli/_parser.py:131-145``: "Applies to
-z/--oneshot and --tui"), so forcing it on every launch would override whatever
model the user picked in ``hermes model``. The durable statement is
``providers.mcc.default_model``, which the serialiser writes and this launcher
leaves alone, and the CLI is started plainly.

**Undo is RESTORE, honestly.** Hermes rewrites ``config.yaml`` itself --
``_config_version`` migrations and ``save_config`` both re-dump it through
``utils.atomic_yaml_write`` -- so comments and layout can be lost by *Hermes*,
not by MCC. There is no version MCC can pin (``_config_version`` is Hermes'
own), so ``--restore`` is hash-guarded and refuses when the file moved, and
``--disconnect`` removes MCC's block with no record required.
"""

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from my_claude_code.cli.harnesses.catalogue_client import (
    catalogue_defaulted,
    catalogue_model_count,
    fetch_catalogue_models,
    harness_catalogue,
    print_defaulted_summary,
)
from my_claude_code.cli.harnesses.catalogue_documents import (
    warn_catalogue_unavailable,
)
from my_claude_code.cli.harnesses.registry import resolve_harness_binary, spec_for
from my_claude_code.config.harness_config_merge import (
    MergeDocumentError,
    MergeResult,
    configure_owned_block,
    merge_config_path,
    read_text_document,
    unconfigure_owned_block,
)
from my_claude_code.config.harnesses import (
    HERMES_API_KEY_ENV,
    HarnessCatalogue,
    HarnessConfigMerge,
    HarnessSpec,
)
from my_claude_code.config.proxy_auth import proxy_auth_token
from my_claude_code.config.restore_record import UndoMode
from my_claude_code.config.server_urls import local_proxy_root_url
from my_claude_code.config.settings import get_settings

from .common import preflight_proxy, run_client_process

HARNESS_ID = "hermes"

#: MCC's own flags, stripped before Hermes sees argv. None of the three is a
#: Hermes flag (its own are ``-z/--oneshot``, ``-m/--model``, ``--provider``,
#: ``-c/--continue``, ``-r/--resume``, ``--tui``, ``--cli``, ``-V/--version``),
#: so nothing is shadowed: ``--models`` lists what MCC published,
#: ``--disconnect`` takes MCC's block back out, and ``--restore`` does the same
#: with the record's hash check and consumes the record.
MODELS_FLAG = "--models"
DISCONNECT_FLAG = "--disconnect"
RESTORE_FLAG = "--restore"

#: Variables MCC owns in the launched environment. Stripped before they are
#: set so a stale value inherited from a parent shell cannot outrank the one
#: this launch resolved.
_MCC_OWNED_ENV_KEYS = frozenset({HERMES_API_KEY_ENV})


def launch(argv: Sequence[str] | None = None) -> None:
    """Launch Hermes Agent with MCC declared in its config.yaml."""

    spec = spec_for(HARNESS_ID)
    args = list(sys.argv[1:] if argv is None else argv)

    if DISCONNECT_FLAG in args:
        raise SystemExit(disconnect(spec, UndoMode.KEYS_ONLY))
    if RESTORE_FLAG in args:
        raise SystemExit(disconnect(spec, UndoMode.RESTORE))
    if MODELS_FLAG in args:
        raise SystemExit(list_models(spec))

    binary_path = resolve_harness_binary(spec)

    if is_passthrough(spec, args):
        # ``hermes config``, ``sessions``, ``--version``: nothing to do with a
        # provider, and they must answer without a running proxy.
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
        print("Start it in another terminal with: mcc-server", file=sys.stderr)
        raise SystemExit(1)

    merged = write_provider_block(spec, proxy_root_url, settings.anthropic_auth_token)
    run_client_process(
        command=[binary_path, *args],
        env=build_hermes_launcher_env(
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


def _merge_parts(spec: HarnessSpec) -> HarnessConfigMerge:
    """Return this harness's merge spec, or raise if the registry lost it."""

    catalogue: HarnessCatalogue | None = spec.catalogue
    if catalogue is None or catalogue.merge is None:
        raise ValueError(f"{spec.id} declares no config merge")
    return catalogue.merge


def config_path_for(spec: HarnessSpec, env: Mapping[str, str] | None = None) -> Path:
    """Return the config.yaml this CLI will actually read."""

    resolved = env if env is not None else os.environ
    return merge_config_path(_merge_parts(spec), resolved)


def write_provider_block(
    spec: HarnessSpec, proxy_root_url: str, auth_token: str
) -> bool:
    """Write MCC's block into the user's config.yaml.

    Returns whether the CLI can be launched with an MCC provider at all. Every
    failure -- an unreachable server, an empty catalogue, a document MCC
    refuses to touch -- degrades to launching without the provider rather than
    refusing to launch. A picker missing MCC's models is a worse session; a
    session that will not start is no session at all.
    """

    merge = _merge_parts(spec)
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
        result = configure_owned_block(
            merge=merge,
            path=config_path_for(spec),
            document=document,
            proxy_root_url=proxy_root_url,
            subject=spec.id,
            claim=True,
        )
    except MergeDocumentError as exc:
        # Never rewritten from a failed parse: the user's file wins over the
        # launch, and the message says which file and why.
        print(
            f"My Claude Code: left {config_path_for(spec)} untouched -- {exc}. "
            "Launching without an MCC provider.",
            file=sys.stderr,
        )
        return False
    except Exception as exc:
        warn_catalogue_unavailable(
            display_name=spec.display_name,
            launcher_command="mcc-hermes",
            path=config_path_for(spec),
            proxy_root_url=proxy_root_url,
            exc=exc,
        )
        return False
    _print_merge_summary(spec, merge, result)
    print_defaulted_summary(spec.display_name, catalogue_defaulted(payload, spec.id))
    return True


def disconnect(spec: HarnessSpec, mode: UndoMode) -> int:
    """Take MCC's block back out and return an exit code."""

    merge = _merge_parts(spec)
    path = config_path_for(spec)
    try:
        proxy_root_url = local_proxy_root_url(get_settings())
    except Exception:
        # The ownership check is an extra proof, not a precondition: without a
        # resolvable server the record alone still decides.
        proxy_root_url = None
    try:
        result = unconfigure_owned_block(
            merge=merge,
            path=path,
            subject=spec.id,
            mode=mode,
            proxy_root_url=proxy_root_url,
        )
    except MergeDocumentError as exc:
        print(f"Could not update {path}: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Could not update {path}: {exc}", file=sys.stderr)
        return 1
    key = merge.owned_key_label
    if result.removed_file:
        print(
            f"Removed {key} from {path} and deleted the file, which MCC had "
            "created. Nothing of MCC's is left."
        )
        return 0
    if result.changed:
        print(f"Removed {key} from {path}. Every other section is unchanged.")
    else:
        print(f"{path} carries nothing of MCC's; nothing to remove.")
    return 0


def list_models(spec: HarnessSpec) -> int:
    """Print the models MCC published to Hermes, and exit.

    Read from the user's own document rather than from the server: the question
    this answers is "what does Hermes see", and the file is the answer. The
    model list is a mapping here (Hermes' own shape), not an array.
    """

    merge = _merge_parts(spec)
    path = config_path_for(spec)
    document = read_text_document(path, merge.document_format)
    if document is None:
        print(
            f"My Claude Code: no {spec.display_name} config at {path}, or it "
            "does not parse. Run mcc-hermes once to publish one.",
            file=sys.stderr,
        )
        return 1
    node: object = document
    for key in merge.owned_key_path:
        node = node.get(key) if isinstance(node, Mapping) else None
    entries = node.get("models") if isinstance(node, Mapping) else None
    if not isinstance(entries, Mapping) or not entries:
        print(f"{path} carries no {merge.owned_key_label} models.")
        return 0
    for model_id, meta in entries.items():
        context = meta.get("context_length") if isinstance(meta, Mapping) else None
        suffix = f" ({context} tokens)" if isinstance(context, int) else ""
        print(f"{model_id}{suffix}")
    return 0


def _print_merge_summary(
    spec: HarnessSpec, merge: HarnessConfigMerge, result: MergeResult
) -> None:
    """Say what was written to a file MCC does not own, every time it happens."""

    if not result.changed:
        return
    key = merge.owned_key_label
    verb = "Created" if result.created else "Updated"
    print(f"My Claude Code: {verb} {key} in {result.path}.", file=sys.stderr)
    if result.backup_path is not None:
        print(
            f"My Claude Code: your previous file was copied to "
            f"{result.backup_path} before the first edit.",
            file=sys.stderr,
        )


def build_hermes_launcher_env(
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
    env[HERMES_API_KEY_ENV] = proxy_auth_token(auth_token)
    return env
