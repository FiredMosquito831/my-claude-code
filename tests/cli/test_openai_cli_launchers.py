"""How the four OpenAI-era launchers configure their agents without clobbering.

Cline, Goose, Aider and Droid arrived together because ``POST
/v1/chat/completions`` unblocked them, and they ended up using four different
levers. These tests pin each lever, because the failure mode is the same in
every case: a document that loads cleanly, a session that starts, and a request
that goes somewhere other than this proxy.
"""

from pathlib import Path

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues import serialise, serialise_sidecar
from my_claude_code.cli.launchers import aider, cline, droid, goose
from my_claude_code.config.harness_base_url import v1_base_url, with_v1_base_url
from my_claude_code.config.harness_cline import with_api_key, with_selected_model
from my_claude_code.config.harnesses import (
    AIDER_API_KEY_ENV,
    AIDER_BASE_URL_ENV,
    AIDER_LEGACY_BASE_URL_ENV,
    CLINE_PROVIDER_ID,
    DROID_API_KEY_ENV,
    GOOSE_BASE_PATH_VALUE,
    GOOSE_PROVIDER_VALUE,
    harness_spec,
)
from my_claude_code.config.proxy_auth import proxy_auth_token

ROOT = "http://127.0.0.1:8082"


def _model(
    gateway_id: str = "anthropic/openrouter/sonnet",
    *,
    context_length: int | None = 200_000,
) -> CatalogueModel:
    return CatalogueModel(
        gateway_id=gateway_id,
        provider_model_ref="openrouter/sonnet",
        display_name="openrouter/sonnet",
        context_length=context_length,
        max_output_tokens=64_000,
    )


# ------------------------------------------------------------------------ Cline

CLINE_SPEC = harness_spec("cline_cli")


def test_cline_maintenance_subcommands_reach_the_cli_untouched() -> None:
    assert cline.is_passthrough(CLINE_SPEC, ["doctor"]) is True
    assert cline.is_passthrough(CLINE_SPEC, ["mcp"]) is True
    assert cline.is_passthrough(CLINE_SPEC, ["--version"]) is True
    assert cline.is_passthrough(CLINE_SPEC, []) is False
    # ``config`` should report the directory this launch actually uses, and a
    # bare prompt is the headless session form.
    assert cline.is_passthrough(CLINE_SPEC, ["config"]) is False
    assert cline.is_passthrough(CLINE_SPEC, ["say hi"]) is False


def test_cline_is_told_the_directory_and_the_provider() -> None:
    """Both are required, and the second one is not decoration.

    Measured on 3.0.61: with the provider block written but not selected,
    Cline fell back to its own hosted ``cline`` provider and failed with
    "Unauthorized ... re-authenticate your Cline account".
    """

    path = Path("/home/u/.fcc/cline/data/settings/providers.json")

    command = cline.build_cline_command("/bin/cline", path, ["--json", "hi"])

    assert command == [
        "/bin/cline",
        "--config",
        str(Path("/home/u/.fcc/cline")),
        "-P",
        CLINE_PROVIDER_ID,
        "--json",
        "hi",
    ]


def test_cline_launches_bare_when_no_catalogue_could_be_written() -> None:
    # Selecting a provider MCC has not declared would turn a degraded launch
    # into a failed one.
    assert cline.build_cline_command("/bin/cline", None, ["hi"]) == ["/bin/cline", "hi"]


def test_cline_document_carries_the_v1_base_url_and_the_real_token() -> None:
    catalogue = CLINE_SPEC.catalogue
    assert catalogue is not None and catalogue.base_url_sentinel is not None

    document, _ = serialise("cline", [_model()])
    document = with_v1_base_url(document, catalogue.base_url_sentinel, ROOT)
    document = with_api_key(document, CLINE_PROVIDER_ID, proxy_auth_token("secret"))

    settings = document["providers"][CLINE_PROVIDER_ID]["settings"]
    assert settings["baseUrl"] == "http://127.0.0.1:8082/v1"
    assert settings["apiKey"] == proxy_auth_token("secret")


def test_cline_promotes_the_model_named_on_the_command_line() -> None:
    document, _ = serialise(
        "cline",
        [_model(), _model("anthropic/openrouter/haiku", context_length=8_000)],
    )

    promoted = with_selected_model(
        document,
        CLINE_PROVIDER_ID,
        cline.selected_model(["-m", "anthropic/openrouter/haiku"]),
    )

    settings = promoted["providers"][CLINE_PROVIDER_ID]["settings"]
    assert settings["model"] == "anthropic/openrouter/haiku"
    assert settings["contextWindow"] == 8_000


# ------------------------------------------------------------------------ Goose

GOOSE_SPEC = harness_spec("goose")


def test_goose_maintenance_subcommands_reach_the_cli_untouched() -> None:
    assert goose.is_passthrough(GOOSE_SPEC, ["update"]) is True
    assert goose.is_passthrough(GOOSE_SPEC, ["--version"]) is True
    assert goose.is_passthrough(GOOSE_SPEC, []) is False
    # ``configure`` needs the host to list models, and ``run``/``session``
    # need the provider.
    assert goose.is_passthrough(GOOSE_SPEC, ["configure"]) is False
    assert goose.is_passthrough(GOOSE_SPEC, ["run", "-t", "hi"]) is False


def test_goose_writes_no_file_at_all() -> None:
    """The registry, not a comment, is what states this.

    Goose's only file-shaped mechanism is a declarative provider under its own
    config directory, which would put an MCC-owned document beside the user's
    settings. Env-only is the whole design.
    """

    assert GOOSE_SPEC.catalogue is None


def test_goose_env_composes_the_endpoint_from_host_and_path() -> None:
    env = goose.build_goose_launcher_env(
        proxy_root_url=ROOT,
        auth_token="secret",
        model_id="anthropic/openrouter/sonnet",
        context_limit=200_000,
        base_env={"PATH": "/usr/bin"},
    )

    # Goose joins these with RFC 3986 rules and the path carries no leading
    # slash, so the pair resolves to <root>/v1/chat/completions.
    assert env["OPENAI_HOST"] == ROOT
    assert env["OPENAI_BASE_PATH"] == GOOSE_BASE_PATH_VALUE == "v1/chat/completions"
    assert env["OPENAI_API_KEY"] == proxy_auth_token("secret")
    assert env["GOOSE_PROVIDER"] == GOOSE_PROVIDER_VALUE
    assert env["GOOSE_MODEL"] == "anthropic/openrouter/sonnet"
    assert env["GOOSE_CONTEXT_LIMIT"] == "200000"
    assert env["GOOSE_DISABLE_KEYRING"] == "1"
    assert env["PATH"] == "/usr/bin"


def test_goose_env_strips_an_inherited_base_url_that_would_outrank_it() -> None:
    env = goose.build_goose_launcher_env(
        proxy_root_url=ROOT,
        auth_token="secret",
        model_id=None,
        context_limit=None,
        base_env={"OPENAI_BASE_URL": "https://elsewhere.example/v1"},
    )

    assert "OPENAI_BASE_URL" not in env
    # And an unresolved model or limit states nothing rather than guessing.
    assert "GOOSE_MODEL" not in env
    assert "GOOSE_CONTEXT_LIMIT" not in env


def test_goose_reads_the_model_a_user_named() -> None:
    assert goose.selected_model(["run", "--model", "a/b/c"]) == "a/b/c"
    assert goose.selected_model(["run", "--model=a/b/c"]) == "a/b/c"
    assert goose.selected_model(["run", "-t", "hi"]) is None


# ------------------------------------------------------------------------ Aider

AIDER_SPEC = harness_spec("aider")


def test_aider_is_handed_both_of_its_documents() -> None:
    command = aider.build_aider_command(
        "/bin/aider",
        AIDER_SPEC,
        (Path("/home/u/.fcc/aider-model-metadata.json"), Path("/home/u/.fcc/x.yml")),
        ["--message", "hi"],
    )

    assert command == [
        "/bin/aider",
        # Launching a coding agent must not edit the tree it is launched in:
        # without this Aider appends ``.aider*`` to the repo's ``.gitignore``
        # and ``git init``s a directory that is not a repository.
        "--no-gitignore",
        "--model-metadata-file",
        str(Path("/home/u/.fcc/aider-model-metadata.json")),
        "--model-settings-file",
        str(Path("/home/u/.fcc/x.yml")),
        "--message",
        "hi",
    ]


def test_aider_launches_bare_when_no_documents_could_be_written() -> None:
    assert aider.build_aider_command("/bin/aider", AIDER_SPEC, None, ["hi"]) == [
        "/bin/aider",
        "--no-gitignore",
        "hi",
    ]


def test_aider_env_sets_both_base_url_variables_to_the_v1_form() -> None:
    """LiteLLM prefers ``OPENAI_BASE_URL`` and falls back to ``OPENAI_API_BASE``.

    Setting only the first would leave an inherited second in place for any
    code path that reads it, so both are written to the same value.
    """

    env = aider.build_aider_launcher_env(
        proxy_root_url=ROOT,
        auth_token="secret",
        base_env={
            "OPENAI_API_BASE": "https://elsewhere.example/v1",
            "PATH": "/usr/bin",
        },
    )

    assert env[AIDER_BASE_URL_ENV] == "http://127.0.0.1:8082/v1"
    assert env[AIDER_LEGACY_BASE_URL_ENV] == "http://127.0.0.1:8082/v1"
    assert env[AIDER_API_KEY_ENV] == proxy_auth_token("secret")
    assert env["PATH"] == "/usr/bin"


def test_aider_sidecar_is_a_list_and_the_metadata_is_a_map() -> None:
    document, _ = serialise("aider", [_model()])
    sidecar = serialise_sidecar("aider", [_model()])

    assert isinstance(document, dict)
    assert isinstance(sidecar, list)


# ------------------------------------------------------------------------ Droid

DROID_SPEC = harness_spec("droid")


def test_droid_maintenance_subcommands_reach_the_cli_untouched() -> None:
    assert droid.is_passthrough(DROID_SPEC, ["update"]) is True
    assert droid.is_passthrough(DROID_SPEC, ["doctor"]) is True
    assert droid.is_passthrough(DROID_SPEC, ["--version"]) is True
    assert droid.is_passthrough(DROID_SPEC, []) is False
    assert droid.is_passthrough(DROID_SPEC, ["exec", "hi"]) is False


def test_droid_is_handed_its_overlay_before_the_subcommand() -> None:
    """``--settings`` is a root option; Commander binds those ahead of a
    subcommand, so ``droid exec --settings X`` would be an argument to
    ``exec`` rather than an overlay."""

    command = droid.build_droid_command(
        "/bin/droid",
        DROID_SPEC,
        Path("/home/u/.fcc/droid-settings.json"),
        ["exec", "hi"],
    )

    assert command == [
        "/bin/droid",
        "--settings",
        str(Path("/home/u/.fcc/droid-settings.json")),
        "exec",
        "hi",
    ]


def test_droid_launches_bare_when_no_overlay_could_be_written() -> None:
    assert droid.build_droid_command("/bin/droid", DROID_SPEC, None, ["exec"]) == [
        "/bin/droid",
        "exec",
    ]


def test_droid_env_supplies_the_value_its_reference_names() -> None:
    env = droid.build_droid_launcher_env(
        auth_token="secret",
        base_env={
            "PATH": "/usr/bin",
            "FACTORY_RUNTIME_SETTINGS_PATH": "/somewhere/else.json",
        },
    )

    assert env[DROID_API_KEY_ENV] == proxy_auth_token("secret")
    # An inherited overlay path would be a second overlay outranking MCC's.
    assert "FACTORY_RUNTIME_SETTINGS_PATH" not in env
    assert env["PATH"] == "/usr/bin"


def test_droid_speaks_anthropic_messages_so_its_base_url_carries_no_v1() -> None:
    catalogue = DROID_SPEC.catalogue
    assert catalogue is not None
    assert catalogue.base_url_shape == "root"
    assert v1_base_url(ROOT) != ROOT
