"""The Coding agents page's two routes: what is installed, and what it is told."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from my_claude_code.application.catalogues import serialise
from my_claude_code.application.model_metadata import (
    ModelReasoningCapability,
    ProviderModelInfo,
)
from my_claude_code.config import rtk as rtk_config
from my_claude_code.config.harnesses import harness_ids
from my_claude_code.config.rtk import RtkState
from my_claude_code.config.settings import Settings
from my_claude_code.core.reasoning import ReasoningEffort
from tests.api.support import create_test_app, runtime_for_app


def _set_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)


def _local_client(app):
    return TestClient(app, client=("127.0.0.1", 50000))


def _remote_client(app):
    return TestClient(app, client=("10.0.0.9", 50000))


def test_harnesses_route_lists_every_registered_harness(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "my_claude_code.api.admin_harness_routes.shutil.which",
        lambda name: "/usr/local/bin/codex" if name == "codex" else None,
    )
    rtk_config.save_rtk_state(RtkState(claude=True))
    app = create_test_app()

    with _local_client(app) as client:
        body = client.get("/admin/api/harnesses").json()

    by_id = {entry["id"]: entry for entry in body["harnesses"]}
    assert tuple(by_id) == harness_ids()
    assert by_id["codex"]["installed"] is True
    assert by_id["codex"]["binary_path"] == "/usr/local/bin/codex"
    assert by_id["codex"]["command"] == "mcc-codex"
    assert by_id["codex"]["protocol_label"].startswith("OpenAI Responses")
    assert by_id["pi"]["installed"] is False
    assert by_id["pi"]["protocol_label"].startswith("Anthropic Messages")
    assert by_id["claude"]["rtk_enabled"] is True
    assert by_id["codex"]["rtk_enabled"] is False


def test_an_unservable_harness_is_listed_with_its_dated_reason(monkeypatch, tmp_path):
    """Antigravity is in the registry precisely so the answer is on the page.

    It is measured and cannot be served, and the card renders the reason
    verbatim -- with the version and the date it was measured on it, so a
    reader can re-check it rather than trust it. It publishes no command.
    """

    _set_home(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "my_claude_code.api.admin_harness_routes.shutil.which", lambda name: None
    )
    app = create_test_app()

    with _local_client(app) as client:
        body = client.get("/admin/api/harnesses").json()

    by_id = {entry["id"]: entry for entry in body["harnesses"]}
    antigravity = by_id["antigravity"]
    assert antigravity["available"] is False
    assert antigravity["command_lines"] == []
    assert antigravity["command"] == ""
    assert antigravity["catalogue"] is None
    assert "verified 2026-09-02" in antigravity["unavailable_reason"]
    assert "agy 1.0.14" in antigravity["unavailable_reason"]
    assert "cloudcode-pa.googleapis.com" in antigravity["unavailable_reason"]

    gemini = by_id["gemini_cli"]
    assert gemini["available"] is True
    assert gemini["command"] == "mcc-gemini"
    assert gemini["protocol_label"].startswith("Google Gemini")
    assert gemini["catalogue"]["config_env_var"] == "GEMINI_CLI_SYSTEM_SETTINGS_PATH"


def test_a_missing_binary_reports_the_vendor_install_hint_and_nothing_else(
    monkeypatch, tmp_path
):
    """The card must never offer to install a CLI, only say how the user can."""

    _set_home(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "my_claude_code.api.admin_harness_routes.shutil.which", lambda name: None
    )
    app = create_test_app()

    with _local_client(app) as client:
        body = client.get("/admin/api/harnesses").json()

    for entry in body["harnesses"]:
        assert entry["installed"] is False
        assert entry["binary_path"] is None
        assert entry["install_hint"]


def test_catalogue_card_reports_path_model_count_and_defaulted_count(
    monkeypatch, tmp_path
):
    _set_home(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "my_claude_code.api.admin_harness_routes.shutil.which", lambda name: None
    )
    catalogue = tmp_path / ".fcc" / "codex-model-catalog.json"
    catalogue.parent.mkdir(parents=True, exist_ok=True)
    catalogue.write_text(
        json.dumps(
            {
                "models": [{"slug": "a"}, {"slug": "b"}],
                "_mcc_defaulted": {"a": ["context_window"]},
            }
        ),
        encoding="utf-8",
    )
    app = create_test_app()

    with _local_client(app) as client:
        body = client.get("/admin/api/harnesses").json()

    codex = next(entry for entry in body["harnesses"] if entry["id"] == "codex")
    assert codex["catalogue"]["exists"] is True
    assert codex["catalogue"]["model_count"] == 2
    assert codex["catalogue"]["defaulted_model_count"] == 1
    assert codex["catalogue"]["updated_at"] is not None
    assert codex["catalogue"]["path"].endswith("codex-model-catalog.json")

    pi = next(entry for entry in body["harnesses"] if entry["id"] == "pi")
    assert pi["catalogue"]["delivery"] == "process_local"
    assert pi["catalogue"]["path"] is None

    claude = next(entry for entry in body["harnesses"] if entry["id"] == "claude")
    assert claude["catalogue"] is None


def test_a_never_launched_harness_reports_no_catalogue_file(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "my_claude_code.api.admin_harness_routes.shutil.which", lambda name: None
    )
    app = create_test_app()

    with _local_client(app) as client:
        body = client.get("/admin/api/harnesses").json()

    codex = next(entry for entry in body["harnesses"] if entry["id"] == "codex")
    assert codex["catalogue"]["exists"] is False
    assert codex["catalogue"]["model_count"] is None


def test_both_routes_are_loopback_only(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    app = create_test_app()

    with _remote_client(app) as client:
        assert client.get("/admin/api/harnesses").status_code == 403
        assert client.get("/admin/api/catalogue-models").status_code == 403


def test_catalogue_models_route_carries_capabilities_and_each_cli_document(
    monkeypatch, tmp_path
):
    _set_home(monkeypatch, tmp_path)
    settings = Settings().model_copy(update={"model": "nvidia_nim/configured"})
    app = create_test_app(settings)
    manager = runtime_for_app(app).provider_manager
    manager.cache_model_infos(
        "nvidia_nim",
        {
            ProviderModelInfo(
                "big",
                context_length=262144,
                max_output_tokens=32768,
                supports_vision=True,
                supported_parameters=frozenset({"tools"}),
                reasoning_capability=ModelReasoningCapability(
                    can_reason=True,
                    supports_effort_control=True,
                    supported_efforts=frozenset(
                        {ReasoningEffort.LOW, ReasoningEffort.HIGH}
                    ),
                ),
            )
        },
    )

    with _local_client(app) as client:
        body = client.get("/admin/api/catalogue-models?provenance=1").json()

    entry = next(
        model
        for model in body["models"]
        if model["gateway_id"] == "anthropic/nvidia_nim/big"
    )
    assert entry["context_length"] == 262144
    assert entry["max_output_tokens"] == 32768
    assert entry["supports_vision"] is True
    assert entry["supports_tool_calls"] is True
    assert entry["reasoning"]["supported_efforts"] == ["high", "low"]
    assert entry["provenance"]["context_length"]["source_label"]

    codex_models = body["catalogues"]["codex"]["document"]["models"]
    codex_entry = next(
        model for model in codex_models if model["slug"] == "nvidia_nim/big"
    )
    assert codex_entry["context_window"] == 262144
    assert [rung["effort"] for rung in codex_entry["supported_reasoning_levels"]] == [
        "low",
        "high",
    ]

    pi_models = body["catalogues"]["pi"]["document"]["models"]
    pi_entry = next(model for model in pi_models if model["id"] == "nvidia_nim/big")
    assert pi_entry["contextWindow"] == 262144
    assert pi_entry["maxTokens"] == 32768
    assert pi_entry["input"] == ["text", "image"]


def test_a_model_the_ladder_knows_nothing_about_is_reported_as_defaulted(
    monkeypatch, tmp_path
):
    _set_home(monkeypatch, tmp_path)
    settings = Settings().model_copy(update={"model": "nvidia_nim/configured"})
    app = create_test_app(settings)

    with _local_client(app) as client:
        body = client.get("/admin/api/catalogue-models").json()

    defaulted = body["catalogues"]["codex"]["defaulted"]
    assert "nvidia_nim/configured" in defaulted
    assert "context_window" in defaulted["nvidia_nim/configured"]


def test_a_merge_card_names_the_users_own_file_and_the_one_key_mcc_writes(
    monkeypatch, tmp_path
):
    """The card has to say what MCC edited, because MCC does not own the file."""

    _set_home(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "my_claude_code.api.admin_harness_routes.shutil.which", lambda name: None
    )
    providers = tmp_path / ".commandcode" / "providers.json"
    providers.parent.mkdir(parents=True, exist_ok=True)
    providers.write_text(
        json.dumps(
            {
                "provider": {
                    "ollama": {"baseURL": "http://x/v1"},
                    "mcc": {
                        "models": {"a/b": {}, "a/c": {}},
                        "_mcc_defaulted": {"a/b": ["maxOutput"]},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    app = create_test_app()

    with _local_client(app) as client:
        body = client.get("/admin/api/harnesses").json()

    entry = next(item for item in body["harnesses"] if item["id"] == "commandcode_cli")[
        "catalogue"
    ]
    assert entry["delivery"] == "merge"
    assert entry["config_env_var"] is None
    assert entry["merged_key"] == "provider.mcc"
    assert entry["path"].endswith("providers.json")
    assert entry["exists"] is True
    assert entry["model_count"] == 2
    assert entry["defaulted_model_count"] == 1


def test_a_users_config_without_mccs_key_reads_as_never_launched(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "my_claude_code.api.admin_harness_routes.shutil.which", lambda name: None
    )
    providers = tmp_path / ".commandcode" / "providers.json"
    providers.parent.mkdir(parents=True, exist_ok=True)
    providers.write_text(
        json.dumps({"provider": {"ollama": {"baseURL": "http://x/v1"}}}),
        encoding="utf-8",
    )
    app = create_test_app()

    with _local_client(app) as client:
        body = client.get("/admin/api/harnesses").json()

    entry = next(item for item in body["harnesses"] if item["id"] == "commandcode_cli")[
        "catalogue"
    ]
    assert entry["exists"] is False
    assert entry["model_count"] is None


def test_a_toml_catalogue_is_read_back_with_the_parser_its_format_needs(
    monkeypatch, tmp_path
):
    """A TOML document parsed as JSON reads as "never written" and lies.

    The card would then offer "written on the first mcc-kimi" to someone who
    has run it, which is the one thing that row exists to answer.
    """

    _set_home(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "my_claude_code.api.admin_harness_routes.shutil.which", lambda name: None
    )
    config_dir = tmp_path / ".fcc"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "kimi-code-config.toml").write_text(
        "\n".join(
            [
                "[providers.mcc]",
                'type = "anthropic"',
                "",
                '[models."mcc/a/b"]',
                'provider = "mcc"',
                "",
                '[models."mcc/a/c"]',
                'provider = "mcc"',
                "",
                "[_mcc_defaulted]",
                '"mcc/a/b" = ["max_context_size"]',
                "",
            ]
        ),
        encoding="utf-8",
    )
    app = create_test_app()

    with _local_client(app) as client:
        body = client.get("/admin/api/harnesses").json()

    entry = next(item for item in body["harnesses"] if item["id"] == "kimi_code")[
        "catalogue"
    ]
    assert entry["delivery"] == "file"
    assert entry["config_flag"] == "--config-file"
    assert entry["config_env_var"] is None
    assert entry["merged_key"] is None
    assert entry["path"].endswith("kimi-code-config.toml")
    assert entry["exists"] is True
    assert entry["model_count"] == 2
    assert entry["defaulted_model_count"] == 1


def test_provenance_is_opt_in_on_the_catalogue_route(monkeypatch, tmp_path):
    """The expensive half is bought only when somebody asks for it.

    ``capability_provenance`` walks the resolution ladder once per field per
    model -- 2.74 ms/model warm, 292 models on the install that reported this,
    so 0.8 s warm and about 5 s on a cold models.dev index. The only callers of
    this route are the ``mcc-<agent>`` launchers, which read ``document``,
    ``model_count`` and ``defaulted`` and never look at provenance, so that
    whole cost used to be bought and thrown away on every launch.
    """

    _set_home(monkeypatch, tmp_path)
    settings = Settings().model_copy(update={"model": "nvidia_nim/configured"})
    app = create_test_app(settings)

    with _local_client(app) as client:
        default = client.get("/admin/api/catalogue-models").json()
        asked = client.get("/admin/api/catalogue-models?provenance=1").json()

    assert default["models"]
    assert all(model["provenance"] == {} for model in default["models"])
    assert any(model["provenance"] for model in asked["models"])
    # Everything a launcher reads is identical either way -- the option adds a
    # field, it never changes a document.
    assert default["catalogues"] == asked["catalogues"]


def test_opencode_document_authenticates_with_apikey_alone(monkeypatch, tmp_path):
    """The generated document carries one credential, and MCC accepts it.

    ``@ai-sdk/anthropic`` sends ``options.apiKey`` as ``x-api-key``. The
    document used to also carry an explicit ``Authorization`` header, justified
    by a comment saying MCC did not read ``x-api-key`` -- true when it was
    written, wrong since 6.27.0. This drives the real dependency rather than
    re-reading the comment: the header the SDK sends, against the app's own
    auth, with nothing else presented.
    """

    _set_home(monkeypatch, tmp_path)
    settings = Settings().model_copy(
        update={"model": "nvidia_nim/configured", "anthropic_auth_token": "proxy-token"}
    )
    app = create_test_app(settings)

    document, _ = serialise("opencode", ())
    options = document["provider"]["mcc"]["options"]
    # 6.37.0 added ``headers`` beside them to carry ``x-mcc-harness``. That is
    # an attribution label, not a credential, so the test's teeth move rather
    # than come out: exactly one thing in this document may authenticate, and
    # no header may smuggle a second one back in the way ``Authorization`` did.
    assert set(options) == {"baseURL", "apiKey", "headers"}
    assert set(options["headers"]) == {"x-mcc-harness"}
    assert "authorization" not in {name.lower() for name in options["headers"]}

    with _local_client(app) as client:
        accepted = client.get("/v1/models", headers={"x-api-key": "proxy-token"})
        refused = client.get("/v1/models", headers={"x-api-key": "wrong-token"})
        unauthenticated = client.get("/v1/models")

    assert accepted.status_code == 200
    assert refused.status_code == 401
    assert unauthenticated.status_code == 401
