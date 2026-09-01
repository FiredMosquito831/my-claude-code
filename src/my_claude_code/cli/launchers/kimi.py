"""The installed ``mcc-kimi`` launcher.

Kimi Code is Moonshot's ``kimi`` CLI -- a Python tool published on PyPI as
``kimi-cli`` and installed with ``uv tool install`` or ``pipx``, not an npm
package. MCC installs neither; the registry's install hint is printed and the
launcher exits 127 when the binary is missing.

**Which file, and why that one.** Kimi Code 1.50.0 resolves its config as
``get_share_dir() / "config.toml"``, and ``get_share_dir`` is
``$KIMI_SHARE_DIR`` or ``~/.kimi``. The environment variable is *not* the
right lever: that directory also holds the user's sessions, their OAuth
credentials, their plugins and the background-worker state, so pointing it at
an MCC-owned directory would make every session they have disappear from the
picker for as long as they launch through MCC. ``kimi --config-file PATH``
moves the config document alone, which is the only thing MCC has any business
moving, so that is what this launcher passes. ``~/.kimi/config.toml`` is never
read, never written and never backed up: stop launching through MCC and the
file you wrote is the file you have.

The trade is stated rather than hidden. ``--config-file`` *replaces* the
config document, it does not overlay it (``kimi_cli/cli/__init__.py`` picks
one of ``--config``, ``--config-file`` and the default path, and
``load_config`` reads that one file), so an ``mcc-kimi`` session takes Kimi's
own defaults for ``theme``, ``hooks`` and ``loop_control`` rather than the
user's. Sessions, skills and MCP servers are unaffected -- they come from the
share directory and from ``--mcp-config-file``'s own defaults, neither of
which this launcher touches.

**The proxy token is written into that file, and this is the one harness
where that is true.** ``LLMProvider.api_key`` is a plain ``SecretStr``: Kimi
publishes no ``"$VAR"``, ``"{env:VAR}"`` or ``"!command"`` reference form the
way Command Code does, and ``augment_provider_with_env_vars`` overrides
``api_key`` from the environment only for provider types ``kimi``,
``openai_legacy`` and ``openai_responses`` -- an ``anthropic`` provider hits
its ``case _: pass``. There is no out-of-band channel to use, so the choice is
between a literal value and no Kimi Code support at all. The literal goes into
a file MCC owns under ``~/.fcc``, in the same directory as ``~/.fcc/.env``,
which already stores the identical ``ANTHROPIC_AUTH_TOKEN`` in clear -- so
nothing is disclosed that was not disclosed already, and nothing is written
into a document the user owns. When no proxy token is configured at all the
value written is the ``fcc-no-auth`` marker, which is not a credential.
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
from my_claude_code.config.harness_toml import (
    with_kimi_credentials,
    write_toml_document_atomically_if_changed,
)
from my_claude_code.config.harnesses import HarnessSpec
from my_claude_code.config.paths import harness_catalogue_path
from my_claude_code.config.proxy_auth import proxy_auth_token
from my_claude_code.config.server_urls import local_proxy_root_url
from my_claude_code.config.settings import get_settings

from .common import preflight_proxy, run_client_process

HARNESS_ID = "kimi_code"


def launch(argv: Sequence[str] | None = None) -> None:
    """Launch Kimi Code with an MCC-owned config.toml."""

    spec = spec_for(HARNESS_ID)
    args = list(sys.argv[1:] if argv is None else argv)
    binary_path = resolve_harness_binary(spec)

    if is_passthrough(spec, args):
        # ``kimi login`` and ``--version`` have nothing to do with a provider
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
        command=build_kimi_command(binary_path, spec, config_path, args),
        env=os.environ,
        binary_name=spec.binary,
        display_name=spec.display_name,
        install_hint=spec.install_hint,
    )


def is_passthrough(spec: HarnessSpec, argv: Sequence[str]) -> bool:
    """Return whether argv must reach the CLI with no MCC configuration."""

    return bool(argv) and (
        argv[0] in spec.passthrough_commands or argv[0] in spec.passthrough_flags
    )


def build_kimi_command(
    binary_path: str,
    spec: HarnessSpec,
    config_path: Path | None,
    argv: Sequence[str],
) -> list[str]:
    """Return the argv Kimi Code is launched with.

    ``--config-file`` goes *before* the user's arguments because it is an
    option on Kimi's root Typer callback, and Typer binds a root option only
    ahead of a subcommand: ``kimi term --config-file X`` would be read as an
    argument to ``term``. Everything the user typed follows unchanged, so a
    user who passes their own ``--config-file`` still wins -- Typer keeps the
    last occurrence.
    """

    catalogue = spec.catalogue
    if config_path is None or catalogue is None or catalogue.config_flag is None:
        return [binary_path, *argv]
    return [binary_path, catalogue.config_flag, str(config_path), *argv]


def write_harness_config(
    spec: HarnessSpec, proxy_root_url: str, auth_token: str
) -> Path | None:
    """Refresh Kimi Code's MCC-owned config.toml and return its path.

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
        write_toml_document_atomically_if_changed(
            config_path,
            with_kimi_credentials(
                document,
                proxy_root_url=proxy_root_url,
                api_key=proxy_auth_token(auth_token),
            ),
        )
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
    """Narrow the generated config to its owner, best effort.

    This is the one generated catalogue that carries the proxy token, so it is
    the one that gets ``0600``. Best effort on purpose: ``chmod`` is close to
    a no-op on Windows and can fail on a network filesystem, and neither is a
    reason to refuse to launch a coding agent.
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
