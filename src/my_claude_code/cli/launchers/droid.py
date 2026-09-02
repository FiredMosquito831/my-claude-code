"""The installed ``mcc-droid`` launcher.

Droid is Factory's ``droid`` binary, installed from ``app.factory.ai/cli``. MCC
does not install it; the registry's install hint is printed and the launcher
exits 127 when the binary is missing.

**Droid speaks Anthropic Messages here, not Chat Completions.** The spec this
work came from grouped Droid with the OpenAI-only CLIs and said to declare a
``generic-chat-completion-api`` custom model, adding "try ``anthropic`` first".
Trying it settled it: ``provider: "anthropic"`` accepts an arbitrary
``baseUrl``, instantiates the bundled ``@anthropic-ai/sdk`` against it and
reaches ``POST <baseUrl>/v1/messages``. Verified end to end -- the request log
row read ``endpoint=/v1/messages protocol=anthropic status=success``. That is
MCC's own native protocol, so it is the one used.

**Which file, and why that one.** The spec expected a merge into
``~/.factory/config.json``. ``droid --settings <path>`` is better: it is a
*runtime settings overlay*, merged into the same hierarchy for that process
only. Measured on a machine with no ``~/.factory`` at all, a file containing
nothing but ``customModels`` was enough for the model to appear and dispatch.
So MCC owns ``~/.fcc/droid-settings.json`` outright and never reads, merges
into or backs up the user's own settings. (Droid's persistent file is
``~/.factory/settings.json`` in current versions, with ``config.json`` kept as
a legacy snake_case fallback -- MCC touches neither.)

**Base URL shape.** The Anthropic SDK appends ``/v1/messages`` itself, so
``baseUrl`` is the proxy **root** with no ``/v1``.

**The proxy token is not in that file.** Droid's own documented secret
reference is ``${VAR}``, expanded by its ``expandSettingsEnvVarRefs`` pass, so
the document holds ``${MCC_DROID_API_KEY}`` and this launcher sets the variable
in the child process only.

**No Factory account is required.** Measured with a fresh home and no login:
``droid exec --model custom:...`` logged "Invalid auth", classified the model
``isByok``, made no ``whoami`` call and went straight to the custom ``baseUrl``.
"""

import contextlib
import os
import stat
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
from my_claude_code.config.atomic_json import (
    write_json_document_atomically_if_changed,
)
from my_claude_code.config.harness_base_url import with_root_base_url
from my_claude_code.config.harnesses import DROID_API_KEY_ENV, HarnessSpec
from my_claude_code.config.paths import harness_catalogue_path
from my_claude_code.config.proxy_auth import proxy_auth_token
from my_claude_code.config.server_urls import local_proxy_root_url
from my_claude_code.config.settings import get_settings

from .common import preflight_proxy, run_client_process

HARNESS_ID = "droid"

#: Variables MCC owns in the launched environment. Stripped before they are
#: set so a stale value inherited from a parent shell cannot outrank the one
#: this launch resolved. ``FACTORY_RUNTIME_SETTINGS_PATH`` is Droid's own
#: environment equivalent of ``--settings``; leaving an inherited one in place
#: would let a second overlay silently outrank MCC's.
_MCC_OWNED_ENV_KEYS = frozenset({DROID_API_KEY_ENV, "FACTORY_RUNTIME_SETTINGS_PATH"})


def launch(argv: Sequence[str] | None = None) -> None:
    """Launch Droid with an MCC-owned runtime settings overlay."""

    spec = spec_for(HARNESS_ID)
    args = list(sys.argv[1:] if argv is None else argv)
    binary_path = resolve_harness_binary(spec)

    if is_passthrough(spec, args):
        # ``droid update`` and ``--version`` have nothing to do with a model
        # and must not require a running proxy to answer.
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

    config_path = write_harness_config(
        spec, proxy_root_url, settings.anthropic_auth_token
    )
    run_client_process(
        command=build_droid_command(binary_path, spec, config_path, args),
        env=build_droid_launcher_env(
            auth_token=settings.anthropic_auth_token, base_env=os.environ
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


def build_droid_command(
    binary_path: str,
    spec: HarnessSpec,
    config_path: Path | None,
    argv: Sequence[str],
) -> list[str]:
    """Return the argv Droid is launched with.

    ``--settings`` goes *before* the user's arguments because it is a root
    option and Commander binds those ahead of a subcommand: ``droid exec
    --settings X`` would be read as an argument to ``exec``. Everything the
    user typed follows unchanged.
    """

    catalogue = spec.catalogue
    if config_path is None or catalogue is None or catalogue.config_flag is None:
        return [binary_path, *argv]
    return [binary_path, catalogue.config_flag, str(config_path), *argv]


def build_droid_launcher_env(
    *, auth_token: str, base_env: Mapping[str, str]
) -> dict[str, str]:
    """Return an environment carrying the value Droid's ``${VAR}`` refers to."""

    env = {
        key: value for key, value in base_env.items() if key not in _MCC_OWNED_ENV_KEYS
    }
    env[DROID_API_KEY_ENV] = proxy_auth_token(auth_token)
    return env


def write_harness_config(
    spec: HarnessSpec, proxy_root_url: str, auth_token: str
) -> Path | None:
    """Refresh Droid's MCC-owned settings overlay and return its path.

    Every failure -- an unreachable proxy, an older server without the
    capability route, an empty catalogue -- degrades to launching with the
    user's own configuration rather than refusing to launch. A picker missing
    MCC's models is a worse session; a session that will not start is no
    session at all.
    """

    catalogue = spec.catalogue
    if catalogue is None or catalogue.filename is None:
        return None
    try:
        payload = fetch_catalogue_models(proxy_root_url, auth_token)
        document = harness_catalogue(payload, spec.id)
        if catalogue_model_count(payload, spec.id) == 0:
            print(
                f"My Claude Code warning: the {spec.display_name} model list is "
                "empty; launching without an MCC provider.",
                file=sys.stderr,
            )
            return None
        config_path = harness_catalogue_path(catalogue.filename)
        if catalogue.base_url_sentinel is not None:
            document = with_root_base_url(
                document, catalogue.base_url_sentinel, proxy_root_url
            )
        write_json_document_atomically_if_changed(config_path, document)
        restrict_permissions(config_path)
        _print_defaulted_summary(spec, document)
    except Exception as exc:
        print(
            f"My Claude Code warning: could not prepare the {spec.display_name} "
            f"config ({exc}); launching without an MCC provider.",
            file=sys.stderr,
        )
        return None
    return config_path


def restrict_permissions(path: Path) -> None:
    """Narrow the generated overlay to its owner, best effort.

    No credential is written into it -- ``apiKey`` is a ``${VAR}`` reference --
    but the document does state which models this install routes and where the
    proxy listens, and MCC owns the file. Best effort on purpose: ``chmod`` is
    close to a no-op on Windows and can fail on a network filesystem, and
    neither is a reason to refuse to launch a coding agent.
    """

    with contextlib.suppress(OSError):
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


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
