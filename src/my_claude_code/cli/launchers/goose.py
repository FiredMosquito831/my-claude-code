"""The installed ``mcc-goose`` launcher.

Goose is Block's ``goose`` binary, published on its own GitHub releases. MCC
does not install it; the registry's install hint is printed and the launcher
exits 127 when the binary is missing.

**Goose is the one harness in this project with no generated file at all**, and
that is a decision rather than an omission. Goose 1.48.0 does have a
declared-model mechanism -- a JSON file under ``<config dir>/custom_providers/``
whose ``models[]`` carry ``context_limit`` and per-token costs -- but the config
directory is Goose's own (``%APPDATA%\\Block\\goose\\config`` on Windows,
``~/.config/goose`` elsewhere), it shares that directory with the user's
settings, and Goose publishes no variable or flag that moves the config file
alone. Writing there would put an MCC-owned document inside a directory MCC
does not own, which is the one thing every other launcher here exists to avoid.

Everything Goose needs is available from the environment instead, so
``mcc-goose`` writes nothing, anywhere:

* ``OPENAI_HOST`` + ``OPENAI_BASE_PATH`` compose the endpoint. Goose joins them
  with RFC 3986 rules, and ``OPENAI_BASE_PATH`` has no leading slash by its own
  default, so the pair resolves to ``<root>/v1/chat/completions``. Verified on
  the wire: ``endpoint=/v1/chat/completions``, ``protocol=openai_chat``.
* ``OPENAI_API_KEY`` is read directly, sent as ``Authorization: Bearer``. No
  keyring entry is created, and ``GOOSE_DISABLE_KEYRING`` is set so a machine
  with a locked or absent keyring does not prompt for one MCC does not use.
* ``GOOSE_PROVIDER`` and ``GOOSE_MODEL`` select the session without a
  ``config.yaml``. Measured with an empty config directory: Goose ran straight
  through, and its own file stayed "missing (can create)".
* ``GOOSE_CONTEXT_LIMIT`` carries the ladder's real context window for the
  model this session runs on. It is the one place Goose accepts a resolved
  capability, and it is why this launcher fetches the catalogue payload at all.

``goose --model``/``--provider`` outrank both variables, so a user who names a
model still wins; when they do, MCC resolves *that* model's context limit.

**Model discovery still works.** Goose's OpenAI provider fetches
``<host>/v1/models`` for its own picker, and that is a route this proxy serves,
so ``goose configure`` lists exactly what ``GET /v1/models`` publishes.
"""

import os
import sys
from collections.abc import Mapping, Sequence

from my_claude_code.cli.harnesses.catalogue_client import (
    catalogue_model_summaries,
    fetch_catalogue_models,
)
from my_claude_code.cli.harnesses.registry import resolve_harness_binary, spec_for
from my_claude_code.config.harness_base_url import root_base_url
from my_claude_code.config.harnesses import (
    GOOSE_BASE_PATH_VALUE,
    GOOSE_PROVIDER_VALUE,
    HarnessSpec,
)
from my_claude_code.config.proxy_auth import proxy_auth_token
from my_claude_code.config.server_urls import local_proxy_root_url
from my_claude_code.config.settings import get_settings
from my_claude_code.core.catalogue_refs import (
    STARTING_MODEL_REASONS,
    select_starting_index,
)

from .common import preflight_proxy, run_client_process

HARNESS_ID = "goose"

#: Goose's own flag for choosing a model, in the spelling its ``--help``
#: publishes. A user who passes it is naming the model this session runs on,
#: and MCC resolves that model's context limit rather than the default's.
MODEL_FLAG = "--model"

#: Variables MCC owns in the launched environment. Stripped before they are
#: set so a stale value inherited from a parent shell cannot outrank the one
#: this launch resolved. ``OPENAI_BASE_URL`` is here although MCC never sets
#: it: Goose reads it as an alternative to the host/path pair, so an inherited
#: one pointing at another gateway would silently take the session.
_MCC_OWNED_ENV_KEYS = frozenset(
    {
        "OPENAI_HOST",
        "OPENAI_BASE_PATH",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_ORGANIZATION",
        "OPENAI_PROJECT",
        "GOOSE_PROVIDER",
        "GOOSE_MODEL",
        "GOOSE_CONTEXT_LIMIT",
        "GOOSE_DISABLE_KEYRING",
    }
)


def launch(argv: Sequence[str] | None = None) -> None:
    """Launch Goose pointed at MCC through its own environment variables."""

    spec = spec_for(HARNESS_ID)
    args = list(sys.argv[1:] if argv is None else argv)
    binary_path = resolve_harness_binary(spec)

    if is_passthrough(spec, args):
        # ``goose update`` and ``--version`` have nothing to do with a
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

    model_id, context_limit = resolve_model(
        proxy_root_url, settings.anthropic_auth_token, args
    )
    run_client_process(
        command=[binary_path, *args],
        env=build_goose_launcher_env(
            proxy_root_url=proxy_root_url,
            auth_token=settings.anthropic_auth_token,
            model_id=model_id,
            context_limit=context_limit,
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


def selected_model(argv: Sequence[str]) -> str | None:
    """Return the model a user named with ``--model``, in either spelling."""

    found: str | None = None
    for index, argument in enumerate(argv):
        if argument == MODEL_FLAG and index + 1 < len(argv):
            found = argv[index + 1]
        elif argument.startswith(f"{MODEL_FLAG}="):
            found = argument[len(MODEL_FLAG) + 1 :]
    return found or None


def resolve_model(
    proxy_root_url: str, auth_token: str, argv: Sequence[str]
) -> tuple[str | None, int | None]:
    """Return the model this session runs on and its resolved context window.

    The user's own ``--model`` wins; otherwise
    :func:`~my_claude_code.core.catalogue_refs.select_starting_index` chooses,
    on the same rule every other harness that must pin a model applies: MCC's
    own configured ``MODEL`` first, then the first entry that is not a free
    tier. Taking the first entry outright is what this used to do, and on a
    real install that opened every Goose session on a free tier the provider
    had withdrawn.

    Every failure degrades to launching with no ``GOOSE_MODEL`` and no
    ``GOOSE_CONTEXT_LIMIT``, leaving whatever the user configured for
    themselves in place, rather than refusing to launch.
    """

    requested = selected_model(argv)
    try:
        models = catalogue_model_summaries(
            fetch_catalogue_models(proxy_root_url, auth_token)
        )
    except Exception as exc:
        print(
            "My Claude Code warning: could not read the model list "
            f"({exc}); launching Goose without a resolved context limit.",
            file=sys.stderr,
        )
        return requested, None

    if not models:
        print(
            "My Claude Code warning: the Goose model list is empty; launching "
            "without a model selection.",
            file=sys.stderr,
        )
        return requested, None

    chosen = requested
    if chosen is None:
        found = select_starting_index(
            models,
            lambda entry: str(entry.get("provider_model_ref") or ""),
            lambda entry: bool(entry.get("is_primary_route")),
        )
        # ``models`` is non-empty here, so ``found`` is never None; the
        # guard is for the reader, not for a case that can occur.
        if found is not None:
            index, reason = found
            chosen = _string_or_none(models[index].get("gateway_id"))
            print(
                f"My Claude Code: Goose starts on {chosen} "
                f"({STARTING_MODEL_REASONS[reason]}).",
                file=sys.stderr,
            )
    for model in models:
        if model.get("gateway_id") == chosen:
            limit = model.get("context_length")
            if isinstance(limit, int) and limit > 0:
                return chosen, limit
            # Known model, no published context window. Goose's own default
            # applies; say so where the user is already looking.
            print(
                f"My Claude Code: {chosen} publishes no context window, so "
                "Goose falls back to its own default.",
                file=sys.stderr,
            )
            return chosen, None
    return chosen, None


def build_goose_launcher_env(
    *,
    proxy_root_url: str,
    auth_token: str,
    model_id: str | None,
    context_limit: int | None,
    base_env: Mapping[str, str],
) -> dict[str, str]:
    """Return an environment that points Goose at MCC and nothing else."""

    env = {
        key: value for key, value in base_env.items() if key not in _MCC_OWNED_ENV_KEYS
    }
    env["OPENAI_HOST"] = root_base_url(proxy_root_url)
    env["OPENAI_BASE_PATH"] = GOOSE_BASE_PATH_VALUE
    env["OPENAI_API_KEY"] = proxy_auth_token(auth_token)
    env["GOOSE_PROVIDER"] = GOOSE_PROVIDER_VALUE
    # Goose only reads its keyring for a value the environment did not supply,
    # and MCC supplies the only one it needs. Disabling it keeps a locked or
    # absent keyring from prompting for a credential this session never uses.
    env["GOOSE_DISABLE_KEYRING"] = "1"
    if model_id is not None:
        env["GOOSE_MODEL"] = model_id
    if context_limit is not None:
        env["GOOSE_CONTEXT_LIMIT"] = str(context_limit)
    return env


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
