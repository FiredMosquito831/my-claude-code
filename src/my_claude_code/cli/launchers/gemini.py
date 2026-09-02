"""The installed ``mcc-gemini`` launcher.

Gemini CLI is Google's ``gemini``, published on npm as ``@google/gemini-cli``.
MCC does not install it; the registry's install hint is printed and the
launcher exits 127 when the binary is missing.

**The lever, and why it is not the obvious one.** Gemini CLI publishes exactly
one variable for the endpoint -- ``GOOGLE_GEMINI_BASE_URL`` -- and setting it
alone does not work. ``getAuthTypeFromEnv`` returns ``"gateway"`` the moment it
sees that variable, and non-interactive startup then runs
``validateAuthMethod``, which knows four auth types and refuses ``gateway``
with ``"Invalid auth method selected."`` The failure is a
``FATAL_AUTHENTICATION_ERROR`` before any request is made. So one settings key
is unavoidable: ``security.auth.selectedType: "gemini-api-key"``, which
short-circuits the environment sniff entirely. Everything else stays in the
environment.

That key goes in a document MCC owns, pointed at by Gemini CLI's own
``GEMINI_CLI_SYSTEM_SETTINGS_PATH``, set in the child process only.
``mergeSettings`` merges the *system* scope last, so MCC's keys win while the
user's own ``~/.gemini/settings.json`` still supplies everything MCC does not
name -- their theme, their MCP servers, their memory. It is never written,
never backed up, and never read for auth: the API-key path returns before
``createCodeAssistContentGenerator``, so the OAuth tokens under ``~/.gemini``
are not touched.

**The proxy token is not on disk.** ``GEMINI_API_KEY`` is read from the
environment (``createContentGeneratorConfig``), so the launcher sets it in the
child process and the generated document names no credential.

**Base URL shape.** ``GOOGLE_GEMINI_BASE_URL`` goes into
``httpOptions.baseUrl`` and the SDK appends ``/v1beta/models/...`` itself
(``constructUrl``, with ``GOOGLE_AI_API_DEFAULT_VERSION = "v1beta"``). It is
therefore the proxy **root** with no ``/v1`` and no ``/v1beta``; verified on
the wire.

Every fact above was read out of Gemini CLI 0.49.0's own bundle. See
``application/catalogues/gemini_cli.py`` for the settings document's side of
the same story.
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
from my_claude_code.config.harness_base_url import root_base_url
from my_claude_code.config.harnesses import HarnessSpec
from my_claude_code.config.paths import harness_catalogue_path
from my_claude_code.config.proxy_auth import proxy_auth_token
from my_claude_code.config.server_urls import local_proxy_root_url
from my_claude_code.config.settings import get_settings

from .common import preflight_proxy, run_client_process

HARNESS_ID = "gemini_cli"

#: The endpoint variable Gemini CLI publishes, and the key variable its
#: API-key path reads. Both are set in the launched process only.
BASE_URL_ENV = "GOOGLE_GEMINI_BASE_URL"
API_KEY_ENV = "GEMINI_API_KEY"

#: Variables MCC owns in the launched environment. Stripped before they are
#: set so a stale value inherited from a parent shell cannot outrank the one
#: this launch resolved. ``GOOGLE_GENAI_USE_GCA`` and ``GOOGLE_GENAI_USE_VERTEXAI``
#: are here because ``getAuthTypeFromEnv`` reads them ahead of everything else;
#: ``GOOGLE_API_KEY`` because the Vertex path reads it; ``GEMINI_MODEL`` because
#: it outranks the model MCC wrote into its settings document.
_MCC_OWNED_ENV_KEYS = frozenset(
    {
        API_KEY_ENV,
        BASE_URL_ENV,
        "GEMINI_CLI_SYSTEM_SETTINGS_PATH",
        "GEMINI_MODEL",
        "GOOGLE_API_KEY",
        "GOOGLE_GENAI_USE_GCA",
        "GOOGLE_GENAI_USE_VERTEXAI",
        "GOOGLE_GENAI_API_VERSION",
        "GOOGLE_VERTEX_BASE_URL",
    }
)


def launch(argv: Sequence[str] | None = None) -> None:
    """Launch Gemini CLI with an MCC-owned settings document."""

    spec = spec_for(HARNESS_ID)
    args = list(sys.argv[1:] if argv is None else argv)
    binary_path = resolve_harness_binary(spec)

    if is_passthrough(spec, args):
        # ``gemini mcp`` and ``--version`` have nothing to do with a provider
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
        command=[binary_path, *args],
        env=build_gemini_launcher_env(
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


def build_gemini_launcher_env(
    *,
    spec: HarnessSpec,
    config_path: Path | None,
    proxy_root_url: str,
    auth_token: str,
    base_env: Mapping[str, str],
) -> dict[str, str]:
    """Return an environment that points Gemini CLI at MCC.

    ``GOOGLE_GEMINI_BASE_URL`` is set even when no catalogue could be written:
    without it the CLI would talk to Google, which is a different and much
    worse failure than a session with no MCC model preset. Without the
    settings document, though, that same variable makes ``getAuthTypeFromEnv``
    answer ``"gateway"`` and startup fail -- so when the document is missing
    the launcher sets nothing at all and lets the user's own configuration
    stand.
    """

    env = {
        key: value for key, value in base_env.items() if key not in _MCC_OWNED_ENV_KEYS
    }
    if config_path is None:
        return env
    catalogue = spec.catalogue
    if catalogue is not None and catalogue.config_env_var is not None:
        env[catalogue.config_env_var] = str(config_path)
    env[BASE_URL_ENV] = root_base_url(proxy_root_url)
    env[API_KEY_ENV] = proxy_auth_token(auth_token)
    return env


def write_harness_config(
    spec: HarnessSpec, proxy_root_url: str, auth_token: str
) -> Path | None:
    """Refresh Gemini CLI's MCC-owned settings document and return its path.

    Every failure -- an unreachable proxy, an older server without the
    capability route, an empty catalogue -- degrades to launching with the
    user's own configuration rather than refusing to launch.
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
        write_json_document_atomically_if_changed(config_path, document)
        restrict_permissions(config_path)
        _print_model_summary(spec, document)
        print_defaulted_summary(
            spec.display_name, catalogue_defaulted(payload, spec.id)
        )
    except Exception as exc:
        warn_catalogue_unavailable(
            display_name=spec.display_name,
            launcher_command="mcc-gemini",
            path=config_path,
            proxy_root_url=proxy_root_url,
            exc=exc,
        )
        return None
    return config_path


def restrict_permissions(path: Path) -> None:
    """Narrow the generated document to its owner, best effort.

    No credential is written into it -- the key lives in an environment
    variable -- but the document does state which models this install routes,
    and MCC owns the file. Best effort on purpose: ``chmod`` is close to a
    no-op on Windows and can fail on a network filesystem, and neither is a
    reason to refuse to launch a coding agent.
    """

    with contextlib.suppress(OSError):
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _print_model_summary(spec: HarnessSpec, document: Mapping[str, object]) -> None:
    """Say which model this session starts on and how to reach the others.

    Gemini CLI's own ``/model`` picker is built from a hardcoded list of
    Google models and MCC cannot add to it, so the list has to be printed
    somewhere -- and stderr, at the moment the user runs the command, is where
    they are already looking.
    """

    aliases = _aliases(document)
    if not aliases:
        return
    selected = _selected_model(document)
    if selected:
        print(
            f"My Claude Code: {spec.display_name} starts on {selected}.",
            file=sys.stderr,
        )
    print(
        f"My Claude Code: {len(aliases)} model(s) routed; reach any of them "
        f"with {spec.command} -m <id>:",
        file=sys.stderr,
    )
    for model_id in aliases:
        print(f"  {model_id}", file=sys.stderr)


def _aliases(document: Mapping[str, object]) -> list[str]:
    configs = document.get("modelConfigs")
    if not isinstance(configs, Mapping):
        return []
    aliases = configs.get("customAliases")
    if not isinstance(aliases, Mapping):
        return []
    return [str(key) for key in aliases]


def _selected_model(document: Mapping[str, object]) -> str:
    model = document.get("model")
    if not isinstance(model, Mapping):
        return ""
    name = model.get("name")
    return str(name) if isinstance(name, str) else ""
