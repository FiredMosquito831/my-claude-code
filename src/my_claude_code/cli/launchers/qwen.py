"""The installed ``mcc-qwen`` launcher.

Qwen Code is Alibaba's ``qwen`` CLI, published on npm as
``@qwen-code/qwen-code``. MCC does not install it; the registry's install hint
is printed and the launcher exits 127 when the binary is missing.

**Which file, and why that one.** The spec this work came from expected an
env-only harness -- ``ANTHROPIC_BASE_URL`` plus ``ANTHROPIC_API_KEY`` plus
``ANTHROPIC_MODEL``, the Claude Code shape -- and a possible write of
``security.auth.selectedType``. Reading Qwen Code 0.15.11's own bundle
(``cli.js``) showed both halves of that to be wrong in MCC's favour:

* ``getAuthTypeFromEnv`` does infer ``anthropic`` from those three variables,
  but it is the *lowest*-precedence source. ``loadCliConfig`` resolves
  ``argv.authType || settings.security.auth.selectedType || getAuthTypeFromEnv()``,
  so a user who has ever picked an auth type in the UI would have their
  choice silently outrank MCC's environment. ``--auth-type`` is a real flag
  with ``anthropic`` among its ``choices``, and it outranks everything. MCC
  passes it, which is why **no ``security.auth`` key is ever written.**
* The env route also carries exactly one model. ``modelProviders.anthropic``
  is an array of ``{id, name, baseUrl, envKey, generationConfig}`` records --
  the shape Qwen's own provider wizard writes -- so MCC can publish the whole
  ladder with real context windows instead of a single ``ANTHROPIC_MODEL``.

That array lives in a settings document, and Qwen names an override for one:
``QWEN_CODE_SYSTEM_SETTINGS_PATH``. MCC writes its catalogue there and sets
the variable in the child process only. ``~/.qwen/settings.json`` is never
read for MCC's sake, never written and never backed up.

The trade is stated rather than hidden. ``modelProviders`` declares
``mergeStrategy: REPLACE`` and the *System* scope is the highest-precedence
one, so for the duration of an ``mcc-qwen`` session MCC's provider list is the
whole list -- a user's own ``modelProviders`` entries are not merged in. Every
other setting they have deep-merges as usual, because the document MCC writes
contains nothing else. Stop launching through MCC and the settings you had are
the settings you have.

**The proxy token is not in that document.** ``envKey`` names an environment
variable and ``ModelsConfig`` reads ``process.env[envKey]`` at request time,
so the launcher sets ``MCC_QWEN_API_KEY`` in the child process and the file on
disk holds only the variable's name.

**Base URL shape.** ``baseUrl`` goes to the official ``@anthropic-ai/sdk``
client, which appends ``/v1/messages``. It is therefore the proxy root with no
``/v1``; verified on the wire. Because the host is not ``*.anthropic.com``,
Qwen's ``AnthropicContentGenerator`` sets ``useProxyIdentity`` and sends the
key as ``Authorization: Bearer`` rather than ``x-api-key`` -- both of which
``api/dependencies.py`` already accepts.
"""

import contextlib
import os
import stat
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
    document_on_disk,
    warn_catalogue_unavailable,
)
from my_claude_code.cli.harnesses.registry import resolve_harness_binary, spec_for
from my_claude_code.config.atomic_json import (
    write_json_document_atomically_if_changed,
)
from my_claude_code.config.harness_base_url import with_root_base_url
from my_claude_code.config.harnesses import QWEN_API_KEY_ENV, HarnessSpec
from my_claude_code.config.paths import harness_catalogue_path
from my_claude_code.config.proxy_auth import proxy_auth_token
from my_claude_code.config.server_urls import local_proxy_root_url
from my_claude_code.config.settings import get_settings

from .common import preflight_proxy, run_client_process

HARNESS_ID = "qwen_code"

#: Qwen's own flag for the auth type, and the value MCC needs. Passed on every
#: session launch because it is the only source that outranks a saved
#: ``security.auth.selectedType``.
AUTH_TYPE_FLAG = "--auth-type"
AUTH_TYPE_VALUE = "anthropic"

#: Variables MCC owns in the launched environment. Stripped before they are
#: set so a stale value inherited from a parent shell cannot outrank the one
#: this launch resolved. The three ``ANTHROPIC_*`` names are here because
#: ``getAuthTypeFromEnv`` and ``AUTH_ENV_MAPPINGS`` both read them: a user's
#: leftover ``ANTHROPIC_BASE_URL`` pointing at a different host would be
#: picked up as this session's base URL.
_MCC_OWNED_ENV_KEYS = frozenset(
    {
        QWEN_API_KEY_ENV,
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_MODEL",
    }
)


def launch(argv: Sequence[str] | None = None) -> None:
    """Launch Qwen Code with an MCC-owned settings document."""

    spec = spec_for(HARNESS_ID)
    args = list(sys.argv[1:] if argv is None else argv)
    binary_path = resolve_harness_binary(spec)

    if is_passthrough(spec, args):
        # ``qwen mcp`` and ``--version`` have nothing to do with a provider
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
        command=build_qwen_command(binary_path, config_path, args),
        env=build_qwen_launcher_env(
            spec=spec,
            config_path=config_path,
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


def build_qwen_command(
    binary_path: str, config_path: Path | None, argv: Sequence[str]
) -> list[str]:
    """Return the argv Qwen Code is launched with.

    ``--auth-type anthropic`` goes ahead of the user's arguments because it is
    a root option and yargs binds those before a subcommand. Everything the
    user typed follows unchanged, so a user who passes their own
    ``--auth-type`` still wins -- yargs keeps the last occurrence.

    It is omitted entirely when no catalogue could be written: telling Qwen to
    authenticate as ``anthropic`` while giving it no Anthropic provider would
    turn a degraded launch into a failed one.
    """

    if config_path is None:
        return [binary_path, *argv]
    return [binary_path, AUTH_TYPE_FLAG, AUTH_TYPE_VALUE, *argv]


def build_qwen_launcher_env(
    *,
    spec: HarnessSpec,
    config_path: Path | None,
    auth_token: str,
    base_env: Mapping[str, str],
) -> dict[str, str]:
    """Return an environment that points Qwen Code at MCC."""

    env = {
        key: value for key, value in base_env.items() if key not in _MCC_OWNED_ENV_KEYS
    }
    if config_path is None:
        return env
    catalogue = spec.catalogue
    if catalogue is not None and catalogue.config_env_var is not None:
        env[catalogue.config_env_var] = str(config_path)
    env[QWEN_API_KEY_ENV] = proxy_auth_token(auth_token)
    return env


def write_harness_config(
    spec: HarnessSpec, proxy_root_url: str, auth_token: str
) -> Path | None:
    """Refresh Qwen Code's MCC-owned settings document and return its path.

    Every failure -- an unreachable proxy, an older server without the
    capability route, an empty catalogue -- degrades to launching with the
    user's own configuration rather than refusing to launch. A picker missing
    MCC's models is a worse session; a session that will not start is no
    session at all.
    """

    catalogue = spec.catalogue
    if catalogue is None or catalogue.filename is None:
        return None
    config_path = harness_catalogue_path(catalogue.filename)
    if document_on_disk(config_path, catalogue.document_format):
        # The server writes this document at startup and rewrites it on every
        # catalogue publish, so the file on disk is the current one and this
        # launch costs no HTTP at all. The fetch below exists to create it.
        return config_path
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
        if catalogue.base_url_sentinel is not None:
            document = with_root_base_url(
                document, catalogue.base_url_sentinel, proxy_root_url
            )
        write_json_document_atomically_if_changed(config_path, document)
        restrict_permissions(config_path)
        print_defaulted_summary(
            spec.display_name, catalogue_defaulted(payload, spec.id)
        )
    except Exception as exc:
        warn_catalogue_unavailable(
            display_name=spec.display_name,
            launcher_command="mcc-qwen",
            path=config_path,
            proxy_root_url=proxy_root_url,
            exc=exc,
        )
        return None
    return config_path


def restrict_permissions(path: Path) -> None:
    """Narrow the generated document to its owner, best effort.

    No credential is written into it -- ``envKey`` names a variable rather than
    holding a value -- but the document does state which models this install
    routes and where the proxy listens, and MCC owns the file. Best effort on
    purpose: ``chmod`` is close to a no-op on Windows and can fail on a
    network filesystem, and neither is a reason to refuse to launch a coding
    agent.
    """

    with contextlib.suppress(OSError):
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
