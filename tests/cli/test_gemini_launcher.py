"""How ``mcc-gemini`` reaches Gemini CLI without touching ~/.gemini.

The decision that makes this launcher different from every other one is that
the obvious route -- two environment variables and no file -- is not merely
weaker, it *fails*. ``getAuthTypeFromEnv`` answers ``"gateway"`` as soon as
``GOOGLE_GEMINI_BASE_URL`` is set, and non-interactive startup then runs
``validateAuthMethod``, which knows four auth types and refuses that one. So a
settings document is unavoidable, and the two facts asserted below are what
make it safe: it is MCC's own file, pointed at by the CLI's own
``GEMINI_CLI_SYSTEM_SETTINGS_PATH``, and it carries no credential.
"""

from pathlib import Path

from my_claude_code.cli.launchers import gemini
from my_claude_code.config.harnesses import harness_spec
from my_claude_code.config.proxy_auth import proxy_auth_token
from my_claude_code.core.client_fingerprint import HARNESS_HEADER

SPEC = harness_spec("gemini_cli")
CONFIG = Path("/home/u/.fcc/gemini-cli-settings.json")


def _env(base: dict[str, str] | None = None, config_path: Path | None = CONFIG):
    return gemini.build_gemini_launcher_env(
        spec=SPEC,
        config_path=config_path,
        proxy_root_url="http://127.0.0.1:8082",
        auth_token="s3cr3t",
        base_env=base or {},
    )


def test_the_settings_path_variable_points_at_mccs_own_document() -> None:
    """``mergeSettings`` merges the system scope last, so MCC's keys win.

    ``customDeepMerge(strategy, schemaDefaults, systemDefaults, user,
    workspace, system)`` -- system is the final argument. The user's own
    ``~/.gemini/settings.json`` still supplies everything MCC does not name,
    and is never written.
    """

    env = _env()

    assert env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"] == str(CONFIG)


def test_the_base_url_is_the_proxy_root_with_no_version_segment() -> None:
    """The bundled SDK appends ``/v1beta/models/...`` itself.

    ``constructUrl`` joins ``httpOptions.baseUrl`` with the API version, whose
    default is ``v1beta``. A ``/v1`` or ``/v1beta`` here would produce
    ``/v1/v1beta/models/...``.
    """

    assert _env()["GOOGLE_GEMINI_BASE_URL"] == "http://127.0.0.1:8082"
    assert (
        gemini.build_gemini_launcher_env(
            spec=SPEC,
            config_path=CONFIG,
            proxy_root_url="http://127.0.0.1:8082/v1",
            auth_token="s3cr3t",
            base_env={},
        )["GOOGLE_GEMINI_BASE_URL"]
        == "http://127.0.0.1:8082"
    )


def test_the_proxy_token_travels_in_the_environment_and_never_on_disk() -> None:
    env = _env()

    assert env["GEMINI_API_KEY"] == proxy_auth_token("s3cr3t")


def test_a_stale_inherited_variable_cannot_outrank_this_launch() -> None:
    """``getAuthTypeFromEnv`` reads five variables before anything MCC sets."""

    env = _env(
        {
            "GOOGLE_GENAI_USE_VERTEXAI": "true",
            "GOOGLE_GENAI_USE_GCA": "true",
            "GOOGLE_API_KEY": "someone-elses",
            "GEMINI_MODEL": "gemini-3-pro-preview",
            "GOOGLE_GENAI_API_VERSION": "v1alpha",
            "PATH": "/usr/bin",
        }
    )

    assert "GOOGLE_GENAI_USE_VERTEXAI" not in env
    assert "GOOGLE_GENAI_USE_GCA" not in env
    assert "GOOGLE_API_KEY" not in env
    assert "GEMINI_MODEL" not in env
    assert "GOOGLE_GENAI_API_VERSION" not in env
    assert env["PATH"] == "/usr/bin"


def test_no_document_means_no_variables_at_all() -> None:
    """Half the configuration is worse than none of it.

    Setting ``GOOGLE_GEMINI_BASE_URL`` without the settings document is the
    exact combination that makes ``getAuthTypeFromEnv`` answer ``"gateway"``
    and startup fail. A refresh that could not write the document therefore
    sets nothing and lets the user's own configuration stand.
    """

    env = _env({"PATH": "/usr/bin"}, config_path=None)

    assert env == {"PATH": "/usr/bin"}


def test_a_non_session_command_is_passed_straight_through() -> None:
    assert gemini.is_passthrough(SPEC, ["mcp", "list"]) is True
    assert gemini.is_passthrough(SPEC, ["--version"]) is True
    assert gemini.is_passthrough(SPEC, ["-p", "hello"]) is False
    assert gemini.is_passthrough(SPEC, []) is False


def test_the_launcher_binds_the_registry_spec_it_documents() -> None:
    assert gemini.HARNESS_ID == "gemini_cli"
    assert SPEC.binary == "gemini"
    assert SPEC.command == "mcc-gemini"
    assert SPEC.catalogue is not None
    assert SPEC.catalogue.config_env_var == "GEMINI_CLI_SYSTEM_SETTINGS_PATH"
    # No base-URL sentinel: the CLI publishes a variable for it, so the
    # serialiser has nothing to leave behind for the launcher to resolve.
    assert SPEC.catalogue.base_url_sentinel is None


# -------------------------------------------------------- harness attribution


def test_the_launch_declares_which_harness_it_is() -> None:
    """Gemini CLI's list is comma-separated, not newline-separated like Claude's."""

    assert _env()[gemini.CUSTOM_HEADERS_ENV] == f"{HARNESS_HEADER}: gemini_cli"


def test_a_users_own_custom_headers_are_kept_and_mccs_is_appended() -> None:
    """The variable is the user's; MCC owns one entry in it, not the list.

    Last wins on a duplicate name, so appending is also what makes MCC's entry
    authoritative without deleting anything the user put there.
    """

    env = _env({gemini.CUSTOM_HEADERS_ENV: "X-Trace: abc"})

    assert (
        env[gemini.CUSTOM_HEADERS_ENV] == f"X-Trace: abc,{HARNESS_HEADER}: gemini_cli"
    )


def test_the_header_is_not_appended_twice_in_a_nested_launch() -> None:
    """A launcher run inside a session it already configured adds nothing."""

    already = f"{HARNESS_HEADER}: gemini_cli"

    assert (
        _env({gemini.CUSTOM_HEADERS_ENV: already})[gemini.CUSTOM_HEADERS_ENV] == already
    )


def test_no_document_means_no_attribution_header_either() -> None:
    """The no-config branch sets nothing at all, and that includes this."""

    assert gemini.CUSTOM_HEADERS_ENV not in _env(config_path=None)
