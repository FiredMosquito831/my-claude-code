"""Installed `fcc-claude` and `fcc-claude-old` launchers."""

import os
import sys
from collections.abc import Callable, Mapping, Sequence

from my_claude_code.cli.claude_env import (
    build_claude_proxy_env,
    build_minimal_claude_proxy_env,
)
from my_claude_code.cli.harnesses.registry import resolve_harness_binary, spec_for
from my_claude_code.config.server_urls import local_proxy_root_url
from my_claude_code.config.settings import get_settings

from .common import preflight_proxy, run_client_process

HARNESS_ID = "claude"
_DISCOVER_MODELS_FLAG = "--discover-models"

_ClaudeEnvBuilder = Callable[..., dict[str, str]]


def _split_discover_models_flag(argv: Sequence[str]) -> tuple[bool, list[str]]:
    """Strip `--discover-models` from the leading section of argv.

    Only occurrences before the first bare `--` separator are treated as the
    flag and removed; everything at or after a bare `--` is passed through
    untouched, since Claude Code treats it as literal argument text (e.g. a
    `-p` prompt value) rather than a flag for us to interpret. This means a
    bare `--discover-models` occurring after a `--` separator is kept as-is,
    while any occurrence before it — including a repeated one — is treated
    as the flag and stripped.
    """

    if _DISCOVER_MODELS_FLAG not in argv:
        return False, list(argv)

    try:
        separator_index = argv.index("--")
    except ValueError:
        separator_index = len(argv)

    leading = argv[:separator_index]
    trailing = argv[separator_index:]
    found = _DISCOVER_MODELS_FLAG in leading
    remaining = [arg for arg in leading if arg != _DISCOVER_MODELS_FLAG] + list(
        trailing
    )
    return found, remaining


def launch(argv: Sequence[str] | None = None) -> None:
    """Launch Claude Code with the proxy URL and auth token set.

    Also sets `ENABLE_WEB_SERVER_TOOLS=true` on the session when the proxy's
    `enable_web_server_tools` setting is on, so Claude Code offers web tools
    exactly when the proxy can execute them — the dashboard Web Tools toggle
    controls both layers. Accepts an FCC-only `--discover-models` flag (stripped
    before Claude Code ever sees the argument list) that enables the FCC model
    catalog fetch used by Claude Code's native model picker.
    """

    settings = get_settings()
    args = list(sys.argv[1:] if argv is None else argv)
    enable_model_discovery, args = _split_discover_models_flag(args)
    _launch_claude(
        args,
        build_env=build_minimal_claude_proxy_env,
        extra_env_kwargs={
            "enable_model_discovery": enable_model_discovery,
            "enable_web_server_tools": settings.enable_web_server_tools,
        },
    )


def launch_legacy(argv: Sequence[str] | None = None) -> None:
    """Launch Claude Code with the full Free Claude Code proxy environment."""

    _launch_claude(argv, build_env=build_claude_proxy_env)


def _launch_claude(
    argv: Sequence[str] | None,
    *,
    build_env: _ClaudeEnvBuilder,
    extra_env_kwargs: Mapping[str, object] | None = None,
) -> None:
    settings = get_settings()
    proxy_root_url = local_proxy_root_url(settings)
    if error := preflight_proxy(proxy_root_url):
        print(
            f"My Claude Code proxy is not reachable at {proxy_root_url}: {error}",
            file=sys.stderr,
        )
        print("Start it in another terminal with: fcc-server", file=sys.stderr)
        raise SystemExit(1)

    spec = spec_for(HARNESS_ID)
    binary_path = resolve_harness_binary(spec)
    args = list(sys.argv[1:] if argv is None else argv)
    run_client_process(
        command=build_claude_launcher_command(binary_path=binary_path, argv=args),
        env=build_env(
            proxy_root_url=proxy_root_url,
            auth_token=settings.anthropic_auth_token,
            base_env=os.environ,
            **(extra_env_kwargs or {}),
        ),
        binary_name=spec.binary,
        display_name=spec.display_name,
        install_hint=spec.install_hint,
    )


def claude_binary_name() -> str:
    """Return the Claude Code binary name."""

    return spec_for(HARNESS_ID).binary


def build_claude_launcher_command(
    *, binary_path: str, argv: Sequence[str]
) -> list[str]:
    """Return the Claude wrapper command without changing user arguments."""

    return [binary_path, *argv]
