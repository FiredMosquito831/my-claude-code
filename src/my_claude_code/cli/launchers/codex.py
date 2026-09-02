"""Installed `mcc-codex` / `fcc-codex` launcher."""

import json
import os
import sys
from collections.abc import Mapping, Sequence

from my_claude_code.cli.harnesses.catalogue_client import (
    catalogue_defaulted,
    fetch_catalogue_models,
    harness_catalogue,
    print_defaulted_summary,
)
from my_claude_code.cli.harnesses.registry import resolve_harness_binary, spec_for
from my_claude_code.config.atomic_json import (
    write_json_document_atomically_if_changed,
)
from my_claude_code.config.paths import codex_model_catalog_path
from my_claude_code.config.proxy_auth import proxy_auth_token
from my_claude_code.config.server_urls import local_proxy_root_url
from my_claude_code.config.settings import Settings, get_settings

from .common import preflight_proxy, run_client_process

HARNESS_ID = "codex"
_CODEX_AUTH_ENV_KEY = "FCC_CODEX_API_KEY"
# Preserve CODEX_HOME: it owns durable user configuration, not parent-task identity.
_STRIPPED_CODEX_ENV_KEYS = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "OPENAI_ORG_ID",
        "OPENAI_ORGANIZATION",
        "CODEX_API_KEY",
        "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
        "CODEX_PERMISSION_PROFILE",
        "CODEX_SHELL",
        "CODEX_THREAD_ID",
        _CODEX_AUTH_ENV_KEY,
    }
)


def launch(argv: Sequence[str] | None = None) -> None:
    """Launch Codex CLI with My Claude Code proxy configuration."""

    spec = spec_for(HARNESS_ID)
    settings = get_settings()
    proxy_root_url = local_proxy_root_url(settings)
    if error := preflight_proxy(proxy_root_url):
        print(
            f"My Claude Code proxy is not reachable at {proxy_root_url}: {error}",
            file=sys.stderr,
        )
        print("Start it in another terminal with: fcc-server", file=sys.stderr)
        raise SystemExit(1)

    binary_path = resolve_harness_binary(spec)
    catalog_args = codex_model_catalog_config_args(proxy_root_url, settings)
    args = list(sys.argv[1:] if argv is None else argv)
    run_client_process(
        command=build_codex_launcher_command(
            binary_path=binary_path,
            argv=args,
            settings=settings,
            proxy_root_url=proxy_root_url,
            catalog_config_args=catalog_args,
        ),
        env=build_codex_launcher_env(
            auth_token=settings.anthropic_auth_token,
            base_env=os.environ,
        ),
        binary_name=spec.binary,
        display_name=spec.display_name,
        install_hint=spec.install_hint,
    )


def codex_binary_name() -> str:
    """Return the Codex CLI binary name."""

    return spec_for(HARNESS_ID).binary


def build_codex_launcher_command(
    *,
    binary_path: str,
    argv: Sequence[str],
    settings: Settings,
    proxy_root_url: str,
    catalog_config_args: Sequence[str] = (),
) -> list[str]:
    """Return a Codex command with ephemeral FCC provider config."""

    return [
        binary_path,
        *catalog_config_args,
        *codex_config_args(
            api_url=_ensure_v1_url(proxy_root_url),
            model=getattr(settings, "model", None),
        ),
        *argv,
    ]


def build_codex_launcher_env(
    *,
    auth_token: str,
    base_env: Mapping[str, str],
) -> dict[str, str]:
    """Return a Codex environment that targets the local proxy provider."""

    env = {
        key: value
        for key, value in base_env.items()
        if key not in _STRIPPED_CODEX_ENV_KEYS and not key.startswith("OPENAI_")
    }
    env[_CODEX_AUTH_ENV_KEY] = proxy_auth_token(auth_token)
    return env


def codex_model_catalog_config_args(
    proxy_root_url: str, settings: Settings
) -> list[str]:
    """Refresh the generated Codex model catalog and return its config args.

    Every failure -- unreachable proxy, an older server without the catalogue
    route, an empty catalogue -- degrades to launching Codex with no catalogue
    rather than refusing to launch. A model picker without MCC's models is a
    worse session; a session that will not start is no session at all.
    """

    try:
        payload = fetch_catalogue_models(proxy_root_url, settings.anthropic_auth_token)
        catalog = harness_catalogue(payload, HARNESS_ID)
        models = catalog.get("models")
        if not isinstance(models, list) or not models:
            print(
                "My Claude Code warning: Codex model catalog is empty; "
                "launching without model picker catalog.",
                file=sys.stderr,
            )
            return []
        catalog_path = codex_model_catalog_path()
        write_json_document_atomically_if_changed(catalog_path, catalog)
        print_defaulted_summary("Codex", catalogue_defaulted(payload, HARNESS_ID))
    except Exception as exc:
        print(
            "My Claude Code warning: could not prepare Codex model catalog "
            f"({exc}); launching without model picker catalog.",
            file=sys.stderr,
        )
        return []

    return build_model_catalog_config_args(str(catalog_path))


def build_model_catalog_config_args(catalog_path: str) -> list[str]:
    """Return Codex config args for a generated model catalog."""

    return ["-c", _toml_assignment("model_catalog_json", catalog_path)]


def codex_config_args(*, api_url: str, model: str | None = None) -> list[str]:
    """Return Codex `-c` assignments for the ephemeral FCC provider."""

    args = [
        "-c",
        _toml_assignment("model_provider", "fcc"),
        "-c",
        _toml_assignment("model_providers.fcc.name", "My Claude Code"),
        "-c",
        _toml_assignment("model_providers.fcc.base_url", _ensure_v1_url(api_url)),
        "-c",
        _toml_assignment("model_providers.fcc.env_key", _CODEX_AUTH_ENV_KEY),
        "-c",
        _toml_assignment("model_providers.fcc.wire_api", "responses"),
    ]
    if model:
        args.extend(["-c", _toml_assignment("model", model)])
    return args


def _ensure_v1_url(url: str) -> str:
    stripped = url.rstrip("/")
    return stripped if stripped.endswith("/v1") else f"{stripped}/v1"


def _toml_assignment(key: str, value: str) -> str:
    return f"{key}={json.dumps(value)}"
