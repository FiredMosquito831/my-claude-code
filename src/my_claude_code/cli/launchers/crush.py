"""The installed ``mcc-crush`` launcher.

Crush is Charm's ``crush`` CLI, published on npm as ``@charmland/crush`` and as
a Go binary on its own GitHub releases. MCC installs neither; the registry's
install hint is printed and the launcher exits 127 when the binary is missing.

**Which file, and why that one.** The spec this work came from described
``crushrc`` -- a Bash script Crush executes at load time -- as the current
format and ``crush.json`` as deprecated, and proposed either generating a shell
script or shelling out to ``crush provider add``. Reading v0.92.0 showed a
better option than any of those. ``crush --help`` lists no ``provider``
command at all, and ``crush schema`` (undocumented; not in ``--help``) still
publishes the full JSON schema, so ``crush.json`` is a supported input rather
than a dead one. More importantly Crush names its own override:
``CRUSH_GLOBAL_CONFIG`` replaces the global configuration *directory*, so MCC
can own a directory under ``~/.fcc``, write one ``crush.json`` into it and
point the launched process there. ``~/.config/crush`` is never read for a
provider, never written and never backed up -- so no merge and no backup file,
which is what the brief allowed for only if no override existed.

The trade is stated rather than hidden. ``CRUSH_GLOBAL_CONFIG`` moves the whole
global layer, so an ``mcc-crush`` session takes Crush's own defaults for the
LSP servers, MCP servers, permissions and theme the user set globally. Two
things limit that. Project-local configuration -- ``.crushrc``, ``crushrc``,
``.crush.json``, ``crush.json`` in or above the working directory -- is a
separate layer and still applies. And the *data* directory is untouched, so
every session, log and statistic the user has stays exactly where it was; only
``crush dirs``' first line changes.

**The proxy token is not in that file.** Crush's schema gives
``"$OPENAI_API_KEY"`` as the example for ``providers.<id>.api_key``: the
``$VAR`` form is its documented secret reference, expanded from the process
environment at request time. MCC writes ``"$MCC_CRUSH_API_KEY"`` and this
launcher sets that variable in the child process only. Verified on the wire --
the outgoing request carried the variable's value as ``x-api-key``.

**Base URL shape.** ``base_url`` goes to ``anthropic-sdk-go``, which appends
``/v1/messages`` itself, so it is the proxy root with no ``/v1``; verified on
the wire (``POST /v1/messages``, not ``/v1/v1/messages``). The same measurement
is why the generated provider sets ``discover_models: false``: with discovery
on, Crush issues ``GET <base_url>/models``, which is not a route MCC serves,
and the ``/v1`` base URL that would make discovery work breaks the messages
route. The explicit model list is the only correct configuration here.
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
from my_claude_code.config.harnesses import CRUSH_API_KEY_ENV, HarnessSpec
from my_claude_code.config.paths import harness_catalogue_path
from my_claude_code.config.proxy_auth import proxy_auth_token
from my_claude_code.config.server_urls import local_proxy_root_url
from my_claude_code.config.settings import get_settings

from .common import preflight_proxy, run_client_process

HARNESS_ID = "crush"

#: Variables MCC owns in the launched environment. Stripped before they are
#: set so a stale value inherited from a parent shell cannot outrank the one
#: this launch resolved.
_MCC_OWNED_ENV_KEYS = frozenset({CRUSH_API_KEY_ENV, "CRUSH_GLOBAL_CONFIG"})


def launch(argv: Sequence[str] | None = None) -> None:
    """Launch Crush with an MCC-owned crush.json."""

    spec = spec_for(HARNESS_ID)
    args = list(sys.argv[1:] if argv is None else argv)
    binary_path = resolve_harness_binary(spec)

    if is_passthrough(spec, args):
        # ``crush login`` and ``--version`` have nothing to do with a provider
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
        env=build_crush_launcher_env(
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


def build_crush_launcher_env(
    *,
    spec: HarnessSpec,
    config_path: Path | None,
    auth_token: str,
    base_env: Mapping[str, str],
) -> dict[str, str]:
    """Return an environment that points Crush at MCC.

    ``CRUSH_GLOBAL_CONFIG`` names a *directory*, not a file -- ``crush dirs``
    echoes whatever it is given as the config directory and Crush then looks
    for ``crush.json`` inside it -- so the variable gets the generated file's
    parent. That is the one place a harness's ``config_env_var`` does not
    carry the catalogue path verbatim, and it is why the registry spells the
    filename ``crush/crush.json``: the directory has to be MCC's alone.
    """

    env = {
        key: value for key, value in base_env.items() if key not in _MCC_OWNED_ENV_KEYS
    }
    if config_path is None:
        return env
    catalogue = spec.catalogue
    if catalogue is not None and catalogue.config_env_var is not None:
        env[catalogue.config_env_var] = str(config_path.parent)
    env[CRUSH_API_KEY_ENV] = proxy_auth_token(auth_token)
    return env


def write_harness_config(
    spec: HarnessSpec, proxy_root_url: str, auth_token: str
) -> Path | None:
    """Refresh Crush's MCC-owned crush.json and return its path.

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
            launcher_command="mcc-crush",
            path=config_path,
            proxy_root_url=proxy_root_url,
            exc=exc,
        )
        return None
    return config_path


def restrict_permissions(path: Path) -> None:
    """Narrow the generated config to its owner, best effort.

    No credential is written into it -- ``api_key`` is a ``$VAR`` reference --
    but the document does state which models this install routes and where the
    proxy listens, and MCC owns the file. Best effort on purpose: ``chmod`` is
    close to a no-op on Windows and can fail on a network filesystem, and
    neither is a reason to refuse to launch a coding agent.
    """

    with contextlib.suppress(OSError):
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
