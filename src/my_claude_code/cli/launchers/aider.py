"""The installed ``mcc-aider`` launcher.

Aider is the ``aider`` command from the ``aider-chat`` PyPI package. MCC does
not install it; the registry's install hint is printed and the launcher exits
127 when the binary is missing.

**Which files, and why those.** Aider reads two model documents and looks for
each of them in the working directory, the git root *and* the home directory --
so simply writing them would mean writing into the user's own ``~``. Both have
a flag: ``--model-metadata-file`` and ``--model-settings-file``. MCC owns
``~/.fcc/aider-model-metadata.json`` and ``~/.fcc/aider-model-settings.yml``,
passes both flags, and never creates a ``.aider.model.*`` file anywhere.

The two carry different kinds of fact. The metadata file is LiteLLM's
``model_cost`` schema -- context window, output ceiling, per-token prices,
vision -- merged *over* LiteLLM's built-in registry, so an exact hit also stops
Aider fetching LiteLLM's price table from GitHub for a model it would never
find there. The settings file is a list of ``ModelSettings`` records saying
what the model *accepts*: whether ``--reasoning-effort`` and
``--thinking-tokens`` will be honoured, and whether ``temperature`` may be sent
at all. It is written as JSON, which is valid YAML and which ``yaml.safe_load``
parses identically -- one atomic writer, no second serialisation format.

**Base URL shape.** Aider reaches MCC through LiteLLM's OpenAI handler, whose
``get_complete_url`` appends ``chat/completions`` to the configured base and
inserts no ``/v1`` of its own. So the value is ``<root>/v1``.
``OPENAI_BASE_URL`` outranks ``OPENAI_API_BASE`` in LiteLLM's resolution order
and both are set to the same value, so an inherited one cannot win.

**The proxy token is not on disk.** ``OPENAI_API_KEY`` is set in the launched
process only, and LiteLLM sends it as ``Authorization: Bearer``.

**The model ref carries a prefix.** ``--model openai/anthropic/<provider>/<model>``:
``openai/`` selects LiteLLM's OpenAI handler and is stripped before the request
body, which carries the gateway id MCC published. The metadata file is keyed by
the whole prefixed string, because that is the exact key
``Model.get_model_info`` looks up. Verified end to end -- the answer came back
and Aider costed the session from the generated prices.
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
    harness_sidecar,
    print_defaulted_summary,
)
from my_claude_code.cli.harnesses.registry import resolve_harness_binary, spec_for
from my_claude_code.config.atomic_json import (
    write_json_document_atomically_if_changed,
)
from my_claude_code.config.harness_base_url import v1_base_url
from my_claude_code.config.harnesses import (
    AIDER_API_KEY_ENV,
    AIDER_BASE_URL_ENV,
    AIDER_LEGACY_BASE_URL_ENV,
    HarnessSpec,
)
from my_claude_code.config.paths import harness_catalogue_path
from my_claude_code.config.proxy_auth import proxy_auth_token
from my_claude_code.config.server_urls import local_proxy_root_url
from my_claude_code.config.settings import get_settings

from .common import preflight_proxy, run_client_process

HARNESS_ID = "aider"

#: Variables MCC owns in the launched environment. Stripped before they are
#: set so a stale value inherited from a parent shell cannot outrank the one
#: this launch resolved -- ``OPENAI_API_BASE`` in particular, which LiteLLM
#: still reads when ``OPENAI_BASE_URL`` is absent.
_MCC_OWNED_ENV_KEYS = frozenset(
    {AIDER_BASE_URL_ENV, AIDER_LEGACY_BASE_URL_ENV, AIDER_API_KEY_ENV}
)


def launch(argv: Sequence[str] | None = None) -> None:
    """Launch Aider with MCC-owned model metadata and settings documents."""

    spec = spec_for(HARNESS_ID)
    args = list(sys.argv[1:] if argv is None else argv)
    binary_path = resolve_harness_binary(spec)

    if is_passthrough(spec, args):
        # ``aider --version`` has nothing to do with a model and must not
        # require a running proxy to answer.
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

    paths = write_harness_config(spec, proxy_root_url, settings.anthropic_auth_token)
    run_client_process(
        command=build_aider_command(binary_path, spec, paths, args),
        env=build_aider_launcher_env(
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


#: Aider's own opt-out from writing ``.aider*`` into the working tree's
#: ``.gitignore``. Passed on every MCC launch; a user who passes
#: ``--gitignore`` after it overrides it, because Aider keeps the last
#: occurrence.
AIDER_NO_GITIGNORE_FLAG = "--no-gitignore"


def build_aider_command(
    binary_path: str,
    spec: HarnessSpec,
    paths: tuple[Path, Path] | None,
    argv: Sequence[str],
) -> list[str]:
    """Return the argv Aider is launched with.

    Every flag MCC adds goes ahead of the user's arguments; Aider's parser
    keeps the last occurrence, so a user who passes their own value still
    wins -- including ``--gitignore``, which is how somebody who *wants* the
    old behaviour asks for it.

    ``--no-gitignore`` is one of those flags because launching a coding agent
    must not edit the repository it is launched in. Run without it, Aider
    appends ``.aider*`` to the working tree's ``.gitignore`` -- and in a
    directory that is not a repository at all, ``git init``s one. Both are
    silent, both are commits waiting to happen in somebody else's diff, and
    neither is anything a user asked MCC for when they typed ``mcc-aider``.
    Aider's own ``--git``/``--no-git`` is deliberately left alone: MCC has no
    opinion on whether Aider uses git, only on whether launching it writes to
    the user's tree.
    """

    catalogue = spec.catalogue
    if paths is None or catalogue is None:
        return [binary_path, AIDER_NO_GITIGNORE_FLAG, *argv]
    metadata_path, settings_path = paths
    command = [binary_path, AIDER_NO_GITIGNORE_FLAG]
    if catalogue.config_flag is not None:
        command += [catalogue.config_flag, str(metadata_path)]
    if catalogue.sidecar_config_flag is not None:
        command += [catalogue.sidecar_config_flag, str(settings_path)]
    return [*command, *argv]


def build_aider_launcher_env(
    *, proxy_root_url: str, auth_token: str, base_env: Mapping[str, str]
) -> dict[str, str]:
    """Return an environment that points Aider's LiteLLM client at MCC."""

    env = {
        key: value for key, value in base_env.items() if key not in _MCC_OWNED_ENV_KEYS
    }
    base_url = v1_base_url(proxy_root_url)
    env[AIDER_BASE_URL_ENV] = base_url
    env[AIDER_LEGACY_BASE_URL_ENV] = base_url
    env[AIDER_API_KEY_ENV] = proxy_auth_token(auth_token)
    return env


def write_harness_config(
    spec: HarnessSpec, proxy_root_url: str, auth_token: str
) -> tuple[Path, Path] | None:
    """Refresh Aider's two MCC-owned documents and return their paths.

    Every failure -- an unreachable proxy, an older server without the
    capability route, an empty catalogue -- degrades to launching with the
    user's own configuration rather than refusing to launch. A model with no
    published limits is a worse session; a session that will not start is no
    session at all.
    """

    catalogue = spec.catalogue
    if catalogue is None or catalogue.filename is None:
        return None
    if catalogue.sidecar_filename is None:
        return None
    try:
        payload = fetch_catalogue_models(proxy_root_url, auth_token)
        document = harness_catalogue(payload, spec.id)
        if catalogue_model_count(payload, spec.id) == 0:
            print(
                f"My Claude Code warning: the {spec.display_name} model list is "
                "empty; launching without MCC model metadata.",
                file=sys.stderr,
            )
            return None
        metadata_path = harness_catalogue_path(catalogue.filename)
        settings_path = harness_catalogue_path(catalogue.sidecar_filename)
        write_json_document_atomically_if_changed(metadata_path, document)
        # JSON is valid YAML, and Aider loads this file with ``yaml.safe_load``
        # -- so one writer serves both documents and there is no second
        # encoder to keep in step with the first.
        write_json_document_atomically_if_changed(
            settings_path, harness_sidecar(payload, spec.id) or []
        )
        restrict_permissions(metadata_path)
        restrict_permissions(settings_path)
        print_defaulted_summary(
            spec.display_name, catalogue_defaulted(payload, spec.id)
        )
    except Exception as exc:
        print(
            f"My Claude Code warning: could not prepare the {spec.display_name} "
            f"model files ({exc}); launching without MCC model metadata.",
            file=sys.stderr,
        )
        return None
    return metadata_path, settings_path


def restrict_permissions(path: Path) -> None:
    """Narrow a generated document to its owner, best effort.

    No credential is written into either -- the base URL and key are
    environment variables -- but the documents do state which models this
    install routes and what they cost, and MCC owns the files. Best effort on
    purpose: ``chmod`` is close to a no-op on Windows and can fail on a network
    filesystem, and neither is a reason to refuse to launch a coding agent.
    """

    with contextlib.suppress(OSError):
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
