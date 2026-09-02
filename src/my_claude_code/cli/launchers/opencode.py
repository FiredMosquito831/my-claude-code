"""Installed ``mcc-opencode`` / ``mcc-opencode2`` / ``mcc-kilo`` launchers.

Three binaries, one mechanism. OpenCode, its v2 preview and Kilo CLI all read
their provider configuration from files rather than from argv, which is the
first time an MCC harness has needed something other than an ephemeral flag
list. None of them is edited in place: each CLI documents an environment
variable naming an *extra* config file that is merged into the precedence
chain rather than replacing it (``OPENCODE_CONFIG`` for OpenCode, ``KILO_CONFIG``
for Kilo), so MCC writes a document of its own under ``~/.fcc`` and points the
launched process at it. The user's ``~/.config/opencode/opencode.json`` is
never read, never written, never backed up and never needs to be: stop
launching through MCC and the file you wrote is the file you have.

The proxy token is not in the generated document either. OpenCode resolves
``{env:VARIABLE}`` substitutions inside a trusted config, so the file names two
variables and this launcher sets them in the child process only.
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
from my_claude_code.cli.harnesses.registry import resolve_harness_binary, spec_for
from my_claude_code.config.atomic_json import (
    write_json_document_atomically_if_changed,
)
from my_claude_code.config.harnesses import (
    OPENCODE_API_KEY_ENV as API_KEY_ENV,
)
from my_claude_code.config.harnesses import (
    OPENCODE_BASE_URL_ENV as BASE_URL_ENV,
)
from my_claude_code.config.harnesses import HarnessSpec
from my_claude_code.config.paths import harness_catalogue_path
from my_claude_code.config.proxy_auth import proxy_auth_token
from my_claude_code.config.server_urls import local_proxy_root_url
from my_claude_code.config.settings import get_settings

from .common import preflight_proxy, run_client_process

#: Variables MCC owns in the launched environment. Stripped before they are
#: set so a stale value inherited from a parent shell cannot outrank the one
#: this launch resolved.
_MCC_OWNED_ENV_KEYS = frozenset({BASE_URL_ENV, API_KEY_ENV})


def launch(argv: Sequence[str] | None = None) -> None:
    """Launch OpenCode with an MCC-owned config file."""

    _launch("opencode", argv)


def launch_v2(argv: Sequence[str] | None = None) -> None:
    """Launch the OpenCode 2 preview with an MCC-owned config file."""

    _launch("opencode2", argv)


def launch_kilo(argv: Sequence[str] | None = None) -> None:
    """Launch Kilo CLI with an MCC-owned config file."""

    _launch("kilo", argv)


def _launch(harness_id: str, argv: Sequence[str] | None) -> None:
    spec = spec_for(harness_id)
    args = list(sys.argv[1:] if argv is None else argv)
    binary_path = resolve_harness_binary(spec)

    if is_passthrough(spec, args):
        # ``opencode upgrade`` and ``--version`` have nothing to do with a
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

    config_path = write_harness_config(spec, proxy_root_url, settings)
    run_client_process(
        command=[binary_path, *args],
        env=build_opencode_launcher_env(
            spec=spec,
            config_path=config_path,
            proxy_root_url=proxy_root_url,
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


def write_harness_config(
    spec: HarnessSpec, proxy_root_url: str, settings: object
) -> Path | None:
    """Refresh this harness's MCC-owned config file and return its path.

    Every failure -- an unreachable proxy, an older server without the
    capability route, an empty catalogue -- degrades to launching with no MCC
    provider rather than refusing to launch. A picker missing MCC's models is
    a worse session; a session that will not start is no session at all.
    """

    catalogue = spec.catalogue
    if catalogue is None or catalogue.filename is None:
        return None
    try:
        payload = fetch_catalogue_models(
            proxy_root_url, getattr(settings, "anthropic_auth_token", "")
        )
        document = harness_catalogue(payload, spec.id)
        if catalogue_model_count(payload, spec.id) == 0:
            print(
                f"My Claude Code warning: the {spec.display_name} model list is "
                "empty; launching without an MCC provider.",
                file=sys.stderr,
            )
            return None
        config_path = harness_catalogue_path(catalogue.filename)
        write_json_document_atomically_if_changed(config_path, document)
        print_defaulted_summary(
            spec.display_name, catalogue_defaulted(payload, spec.id)
        )
    except Exception as exc:
        print(
            f"My Claude Code warning: could not prepare the {spec.display_name} "
            f"config ({exc}); launching without an MCC provider.",
            file=sys.stderr,
        )
        return None
    return config_path


def build_opencode_launcher_env(
    *,
    spec: HarnessSpec,
    config_path: Path | None,
    proxy_root_url: str,
    auth_token: str,
    base_env: Mapping[str, str],
) -> dict[str, str]:
    """Return an environment that points one OpenCode-family CLI at MCC."""

    env = {
        key: value for key, value in base_env.items() if key not in _MCC_OWNED_ENV_KEYS
    }
    if config_path is None:
        return env
    catalogue = spec.catalogue
    if catalogue is not None and catalogue.config_env_var is not None:
        env[catalogue.config_env_var] = str(config_path)
    env[BASE_URL_ENV] = messages_base_url(proxy_root_url)
    env[API_KEY_ENV] = proxy_auth_token(auth_token)
    return env


def messages_base_url(proxy_root_url: str) -> str:
    """Return the base URL the Anthropic AI SDK appends ``/messages`` to."""

    stripped = proxy_root_url.rstrip("/")
    return stripped if stripped.endswith("/v1") else f"{stripped}/v1"
