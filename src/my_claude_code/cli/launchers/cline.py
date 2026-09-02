"""The installed ``mcc-cline`` launcher.

Cline is the ``cline`` CLI, published on npm as ``cline``. MCC does not install
it; the registry's install hint is printed and the launcher exits 127 when the
binary is missing.

**Which file, and why that one.** The spec this work came from proposed running
``cline auth --provider openai-native --baseurl ... --apikey ...`` as a one-shot
against the user's own ``~/.cline``. Reading 3.0.61 showed a better option and
one wrong assumption:

* ``cline --config <dir>`` moves the whole configuration directory, and Cline
  derives its data directory from the settings file inside it -- so a single
  flag is enough for MCC to own a tree under ``~/.fcc/cline`` and never read,
  write or back up anything under ``~/.cline``. Measured: passing only
  ``--config`` produced the full ``data/{cache,logs,settings}`` layout there.
* ``openai-native`` is the wrong provider. It is OpenAI's own hosted entry;
  ``openai`` is an alias that normalises to ``openai-compatible``; and
  ``openai-compatible`` is the one Cline documents as "OpenAI-compatible chat
  completions endpoint", the one that takes an arbitrary ``baseUrl``, and the
  only one with no ``modelsSourceUrl`` -- so it issues no discovery call to a
  route this proxy does not serve under that provider id.

``-P openai-compatible`` is passed on every session launch, ahead of the user's
own arguments. It is required, not decorative: with the provider block written
but not selected, Cline 3.0.61 fell back to its own hosted ``cline`` provider
and failed with "Unauthorized ... re-authenticate your Cline account".

**Base URL shape.** ``baseUrl`` goes to ``@ai-sdk/openai-compatible``, which
appends ``chat/completions`` and nothing else, so it is ``<root>/v1``. Verified
on the wire: ``POST /v1/chat/completions``, ``protocol=openai_chat``.

**The proxy token is literal in that file, and that is measured.** Cline falls
back to ``process.env.OPENAI_API_KEY`` only when ``apiKey`` is absent, and on
3.0.61 that path did not authenticate and did not terminate -- the run hung.
With the key in the document the same run answered in 885 ms. So it is written
into MCC's own file, narrowed to ``0600``, in the same directory tree as
``~/.fcc/.env`` which already holds the identical token in clear. Nothing is
written into a file the user owns.

**Nothing of MCC's own survives in that file.** Cline validates
``providers.json`` as a whole and discards it on any surprise -- measured on
3.0.61, one unrecognised root key made it drop the settings it had just read
and rewrite them with its own bundled default model, losing the base URL and
the key with it, and the next run reached ``api.openai.com``. So
``config/harness_cline.strip_mcc_keys`` removes MCC's blocks immediately before
the write, and the defaulted record is printed to stderr instead.

**Per-model limits.** Cline's settings schema has no per-model array: the
provider block carries the numbers for the one model it names. So the
serialiser records every routable model's resolved limits in an inert
``_mcc_models`` block and ``config/harness_cline.py`` promotes the one named by
``-m``/``--model`` into ``settings`` before the file reaches disk. Verified:
Cline's own run result echoed back ``contextWindow: 131072``, the ladder's
figure for that ref.
"""

import contextlib
import os
import stat
import sys
from collections.abc import Sequence
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
from my_claude_code.config.harness_base_url import with_v1_base_url
from my_claude_code.config.harness_cline import (
    selected_model,
    strip_mcc_keys,
    with_api_key,
    with_selected_model,
)
from my_claude_code.config.harnesses import CLINE_PROVIDER_ID, HarnessSpec
from my_claude_code.config.paths import harness_catalogue_path
from my_claude_code.config.proxy_auth import proxy_auth_token
from my_claude_code.config.server_urls import local_proxy_root_url
from my_claude_code.config.settings import get_settings

from .common import preflight_proxy, run_client_process

HARNESS_ID = "cline_cli"

#: Cline's own flag for choosing the provider, and the value MCC needs.
PROVIDER_FLAG = "-P"


def launch(argv: Sequence[str] | None = None) -> None:
    """Launch Cline with an MCC-owned configuration directory."""

    spec = spec_for(HARNESS_ID)
    args = list(sys.argv[1:] if argv is None else argv)
    binary_path = resolve_harness_binary(spec)

    if is_passthrough(spec, args):
        # ``cline doctor`` and ``--version`` have nothing to do with a
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

    config_path = write_harness_config(
        spec, proxy_root_url, settings.anthropic_auth_token, args
    )
    run_client_process(
        command=build_cline_command(binary_path, config_path, args),
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


def cline_config_dir(config_path: Path) -> Path:
    """Return the directory ``--config`` names, given the settings file path.

    Cline reads ``<dir>/data/settings/providers.json`` and derives its data
    directory back out of that path, so the flag gets the root of the tree
    rather than the file. Three levels up is the whole of the relationship,
    and the registry spells the filename out so it can be read here.
    """

    return config_path.parent.parent.parent


def build_cline_command(
    binary_path: str, config_path: Path | None, argv: Sequence[str]
) -> list[str]:
    """Return the argv Cline is launched with.

    ``--config`` and ``-P`` go ahead of the user's arguments because both are
    root options and Commander binds those before a subcommand. Everything the
    user typed follows unchanged, so a user who passes their own ``-P`` still
    wins -- Commander keeps the last occurrence.

    Both are omitted entirely when no catalogue could be written: selecting a
    provider MCC has not declared would turn a degraded launch into a failed
    one.
    """

    if config_path is None:
        return [binary_path, *argv]
    return [
        binary_path,
        "--config",
        str(cline_config_dir(config_path)),
        PROVIDER_FLAG,
        CLINE_PROVIDER_ID,
        *argv,
    ]


def write_harness_config(
    spec: HarnessSpec, proxy_root_url: str, auth_token: str, argv: Sequence[str]
) -> Path | None:
    """Refresh Cline's MCC-owned ``providers.json`` and return its path.

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
            document = with_v1_base_url(
                document, catalogue.base_url_sentinel, proxy_root_url
            )
        document = with_api_key(
            document, CLINE_PROVIDER_ID, proxy_auth_token(auth_token)
        )
        document = with_selected_model(
            document, CLINE_PROVIDER_ID, selected_model(argv)
        )
        # Last, and never earlier: Cline discards the whole document on any
        # unrecognised root key, so MCC's own bookkeeping is reported to the
        # user here and dropped on the way to disk.
        print_defaulted_summary(
            spec.display_name, catalogue_defaulted(payload, spec.id)
        )
        write_json_document_atomically_if_changed(config_path, strip_mcc_keys(document))
        restrict_permissions(config_path)
    except Exception as exc:
        print(
            f"My Claude Code warning: could not prepare the {spec.display_name} "
            f"config ({exc}); launching without an MCC provider.",
            file=sys.stderr,
        )
        return None
    return config_path


def restrict_permissions(path: Path) -> None:
    """Narrow the generated document to its owner, best effort.

    This is one of the two generated catalogues that carries the proxy token,
    so it gets ``0600``. Best effort on purpose: ``chmod`` is close to a no-op
    on Windows and can fail on a network filesystem, and neither is a reason
    to refuse to launch a coding agent.
    """

    with contextlib.suppress(OSError):
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
