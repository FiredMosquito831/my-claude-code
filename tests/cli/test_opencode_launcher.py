"""The OpenCode family is pointed at MCC without touching the user's config.

The whole point of this launcher is what it does *not* do. OpenCode, its v2
preview and Kilo all read provider configuration from files, and the obvious
implementation -- merge a provider block into
``~/.config/opencode/opencode.json`` -- would make MCC a co-owner of a document
the user wrote by hand. Each CLI publishes an environment variable naming an
extra config file instead, so MCC owns a file under ``~/.fcc`` and hands over
its path. These tests pin that, and pin the token staying out of the file.
"""

from pathlib import Path

from my_claude_code.cli.launchers.opencode import (
    build_opencode_launcher_env,
    is_passthrough,
    messages_base_url,
)
from my_claude_code.config.harnesses import (
    OPENCODE_API_KEY_ENV,
    OPENCODE_BASE_URL_ENV,
    harness_spec,
)


def test_each_harness_is_pointed_with_its_own_documented_variable() -> None:
    expected = {
        "opencode": "OPENCODE_CONFIG",
        "opencode2": "OPENCODE_CONFIG",
        "kilo": "KILO_CONFIG",
    }
    for harness_id, variable in expected.items():
        spec = harness_spec(harness_id)
        assert spec.catalogue is not None
        assert spec.catalogue.config_env_var == variable

        env = build_opencode_launcher_env(
            spec=spec,
            config_path=Path("/home/u/.fcc/generated.json"),
            proxy_root_url="http://127.0.0.1:8082/",
            auth_token="token",
            base_env={"PATH": "p"},
        )
        assert env[variable] == str(Path("/home/u/.fcc/generated.json"))


def test_the_launched_environment_carries_the_url_and_token_only() -> None:
    env = build_opencode_launcher_env(
        spec=harness_spec("opencode"),
        config_path=Path("cfg.json"),
        proxy_root_url="http://127.0.0.1:8082/",
        auth_token="token",
        base_env={"PATH": "p", OPENCODE_BASE_URL_ENV: "stale"},
    )

    assert env["PATH"] == "p"
    # A stale value inherited from a parent shell must not outrank the one
    # this launch resolved.
    assert env[OPENCODE_BASE_URL_ENV] == "http://127.0.0.1:8082/v1"
    assert env[OPENCODE_API_KEY_ENV] == "token"


def test_no_config_means_no_mcc_variables_at_all() -> None:
    """A failed catalogue fetch launches the CLI plain, not half-configured."""

    env = build_opencode_launcher_env(
        spec=harness_spec("opencode"),
        config_path=None,
        proxy_root_url="http://127.0.0.1:8082/",
        auth_token="token",
        base_env={"PATH": "p"},
    )

    assert env == {"PATH": "p"}


def test_the_base_url_is_what_the_anthropic_sdk_appends_messages_to() -> None:
    assert messages_base_url("http://127.0.0.1:8082") == "http://127.0.0.1:8082/v1"
    assert messages_base_url("http://127.0.0.1:8082/v1/") == "http://127.0.0.1:8082/v1"


def test_maintenance_commands_reach_the_cli_without_a_running_proxy() -> None:
    spec = harness_spec("opencode")

    assert is_passthrough(spec, ["upgrade"])
    assert is_passthrough(spec, ["--version"])
    assert not is_passthrough(spec, ["run", "hello"])
    assert not is_passthrough(spec, [])
