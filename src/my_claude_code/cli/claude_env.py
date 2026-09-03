"""Shared Claude Code environment policy for FCC client surfaces."""

from collections.abc import Mapping

from my_claude_code.config.harnesses import harness_spec
from my_claude_code.config.proxy_auth import proxy_auth_token
from my_claude_code.core.client_fingerprint import HARNESS_HEADER

CLAUDE_CODE_AUTO_COMPACT_WINDOW = "190000"
#: Read from the harness registry so the managed session, the launcher and the
#: dashboard's installed-probe can never look for different executables.
CLAUDE_BINARY_NAME = harness_spec("claude").binary

#: The harness id this module configures, and the registry is what says so:
#: the header value has to be the same string the request log keys on.
CLAUDE_HARNESS_ID = harness_spec("claude").id

#: Claude Code's own custom-header variable. ``Name: Value`` per line,
#: newline-separated for several -- ``docs/CLAUDE-CODE-CONFIG.md`` and
#: ``config/data/claude_code_config_catalog.json`` both record the format, and
#: v2.1.227 is the floor. An older Claude Code ignores the variable, which is
#: exactly the right failure: the request is still served and the log falls
#: back to attributing it by user-agent.
CUSTOM_HEADERS_ENV = "ANTHROPIC_CUSTOM_HEADERS"


def with_harness_header(env: dict[str, str]) -> dict[str, str]:
    """Add MCC's attribution header to ``ANTHROPIC_CUSTOM_HEADERS``, in place.

    Appends rather than assigns. A user who set the variable themselves --
    a corporate gateway token, a tracing id -- put it there for a reason, and
    a launcher that overwrote it would break their session to gain a
    diagnostic label. MCC's line is added last so it wins on a duplicate name
    without removing anything they wrote.

    Nothing is appended if the inherited value already names this header: a
    launcher that ran inside a session it had already configured would
    otherwise grow one line per generation.
    """

    line = f"{HARNESS_HEADER}: {CLAUDE_HARNESS_ID}"
    existing = env.get(CUSTOM_HEADERS_ENV, "").strip()
    if not existing:
        env[CUSTOM_HEADERS_ENV] = line
    elif not any(
        entry.split(":", 1)[0].strip().lower() == HARNESS_HEADER
        for entry in existing.splitlines()
    ):
        env[CUSTOM_HEADERS_ENV] = "\n".join((existing, line))
    return env


def build_claude_proxy_env(
    *,
    proxy_root_url: str,
    auth_token: str,
    base_env: Mapping[str, str],
) -> dict[str, str]:
    """Return the canonical environment for Claude Code proxy sessions."""

    # Claude's aggregate traffic flag also suppresses gateway model discovery.
    env = {
        key: value
        for key, value in base_env.items()
        if not key.startswith("ANTHROPIC_")
        and key != "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"
    }
    env["ANTHROPIC_BASE_URL"] = proxy_root_url
    env["ANTHROPIC_AUTH_TOKEN"] = proxy_auth_token(auth_token)
    env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] = "1"
    env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = CLAUDE_CODE_AUTO_COMPACT_WINDOW
    env["DISABLE_AUTOUPDATER"] = "1"
    env["DISABLE_FEEDBACK_COMMAND"] = "1"
    env["DISABLE_ERROR_REPORTING"] = "1"
    env["DISABLE_TELEMETRY"] = "1"
    return with_harness_header(env)


def build_minimal_claude_proxy_env(
    *,
    proxy_root_url: str,
    auth_token: str,
    base_env: Mapping[str, str],
    enable_model_discovery: bool = False,
    enable_web_server_tools: bool = False,
) -> dict[str, str]:
    """Return the inherited environment with only the proxy variables set.

    Claude Code's `~/.claude/settings.json` takes precedence over environment
    variables, so `build_claude_proxy_env`'s aggressive stripping and extra
    flags are wasted effort for users who already configured that file. This
    builder is for users who have not: it sets exactly `ANTHROPIC_BASE_URL`
    and `ANTHROPIC_AUTH_TOKEN` on top of the inherited process environment,
    removing nothing and adding nothing else.

    `enable_web_server_tools` additionally sets `ENABLE_WEB_SERVER_TOOLS=true`,
    which tells Claude Code to offer `web_search` / `web_fetch` tools on the
    session. It mirrors the proxy's own `enable_web_server_tools` setting so the
    dashboard Web Tools toggle controls both layers with one switch: when the
    proxy is not executing local web tools, the client does not advertise them
    either, avoiding the "local web server tools are disabled" error on use.

    `enable_model_discovery` additionally sets
    `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`, which makes Claude Code
    fetch the FCC model catalog (`GET /v1/models`) on every launch so its
    native model picker lists FCC's models. It defaults to `False` because
    that fetch is an extra request to the proxy the minimal launcher
    otherwise avoids.
    """

    env = dict(base_env)
    env["ANTHROPIC_BASE_URL"] = proxy_root_url
    env["ANTHROPIC_AUTH_TOKEN"] = proxy_auth_token(auth_token)
    if enable_web_server_tools:
        env["ENABLE_WEB_SERVER_TOOLS"] = "true"
    if enable_model_discovery:
        env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] = "1"
    return with_harness_header(env)
