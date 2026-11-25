"""PR 13 (EM-PR5): Multi-protocol inbound launcher switch & persisted overrides."""

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues import serialise
from my_claude_code.application.catalogues.inbound_protocol import (
    apply_inbound_protocol,
    document_inbound_protocol,
    document_matches_inbound_protocol,
    inbound_protocol_base_url_shape,
)
from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.cli.launchers import (
    commandcode,
    crush,
    droid,
    hermes,
    openclaw,
    opencode,
)
from my_claude_code.cli.launchers.common import (
    INBOUND_PROTOCOL_ENV,
    resolve_launch_inbound_protocol,
    strip_inbound_protocol_flag,
)
from my_claude_code.config.document_codecs import yaml_mapping_at
from my_claude_code.config.harness_config_merge import (
    merge_config_path,
    owned_block_names,
)
from my_claude_code.config.harnesses import (
    HARNESS_SPECS,
    HarnessProtocol,
    harness_spec,
    normalize_inbound_protocol,
)
from my_claude_code.config.model_overrides import (
    HARNESS_PROTOCOLS_KEY,
    ModelParameterOverrides,
    load_model_overrides,
    reset_model_overrides_cache,
    save_model_overrides,
    with_harness_protocol_row,
)
from my_claude_code.config.settings import Settings
from my_claude_code.providers.base import BaseProvider, ProviderConfig
from my_claude_code.runtime.harness_catalogues import HarnessCatalogueFanoutPublisher
from tests.api.support import create_test_app, provider_manager_for_app

ROOT = "http://127.0.0.1:8940"
FAKE_TOKEN = "mcc-fake-token-0123456789abcdef"


class _JsonResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _JsonResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv(INBOUND_PROTOCOL_ENV, raising=False)
    reset_model_overrides_cache()


def _sample_models() -> list[CatalogueModel]:
    return [
        CatalogueModel(
            gateway_id="anthropic/openrouter/configured",
            provider_model_ref="openrouter/configured",
            display_name="Configured",
            context_length=200_000,
            max_output_tokens=16_000,
            is_primary_route=True,
        )
    ]


def _catalogue_payload(harness_id: str, format_id: str) -> dict[str, Any]:
    document, defaulted = serialise(format_id, _sample_models())
    return {
        "models": [],
        "catalogues": {
            harness_id: {
                "document": document,
                "model_count": 1,
                "defaulted": defaulted.as_document(),
            }
        },
    }


class _SettingsStub:
    def __init__(self, token: str = FAKE_TOKEN) -> None:
        self.anthropic_auth_token = token
        self.local_proxy_port = 8940
        self.host = "127.0.0.1"


# --------------------------------------------------------------------------
# 1. Registry switchable_protocols & normalize_inbound_protocol
# --------------------------------------------------------------------------


def test_switchable_protocols_matrix_matches_verified_harnesses() -> None:
    expected_switchable = {
        "opencode": (
            HarnessProtocol.ANTHROPIC_MESSAGES,
            HarnessProtocol.OPENAI_RESPONSES,
            HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
        ),
        "opencode2": (
            HarnessProtocol.ANTHROPIC_MESSAGES,
            HarnessProtocol.OPENAI_RESPONSES,
            HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
        ),
        "kilo": (
            HarnessProtocol.ANTHROPIC_MESSAGES,
            HarnessProtocol.OPENAI_RESPONSES,
            HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
        ),
        "commandcode_cli": (
            HarnessProtocol.ANTHROPIC_MESSAGES,
            HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
        ),
        "openclaw": (
            HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
            HarnessProtocol.ANTHROPIC_MESSAGES,
        ),
        "hermes": (
            HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
            HarnessProtocol.ANTHROPIC_MESSAGES,
        ),
        "crush": (
            HarnessProtocol.ANTHROPIC_MESSAGES,
            HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
        ),
        "droid": (
            HarnessProtocol.ANTHROPIC_MESSAGES,
            HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
        ),
    }
    for spec in HARNESS_SPECS:
        if spec.id in expected_switchable:
            assert spec.switchable_protocols == expected_switchable[spec.id], spec.id
            assert spec.protocol in spec.switchable_protocols, spec.id
            assert set(spec.switchable_protocols) <= set(spec.effective_speaks())
            assert spec.launchable_inbound_protocols() == expected_switchable[spec.id]
        else:
            assert spec.switchable_protocols == (), spec.id
            assert spec.launchable_inbound_protocols() == (spec.protocol,), spec.id


def test_normalize_inbound_protocol_accepts_canonical_and_short_aliases() -> None:
    assert normalize_inbound_protocol(None) is None
    assert normalize_inbound_protocol("") is None
    assert normalize_inbound_protocol("  ") is None
    assert normalize_inbound_protocol("auto") is None
    assert normalize_inbound_protocol("DEFAULT") is None

    assert (
        normalize_inbound_protocol("anthropic_messages")
        == HarnessProtocol.ANTHROPIC_MESSAGES
    )
    assert normalize_inbound_protocol("messages") == HarnessProtocol.ANTHROPIC_MESSAGES
    assert (
        normalize_inbound_protocol("openai_responses")
        == HarnessProtocol.OPENAI_RESPONSES
    )
    assert normalize_inbound_protocol("responses") == HarnessProtocol.OPENAI_RESPONSES
    assert (
        normalize_inbound_protocol("openai_chat_completions")
        == HarnessProtocol.OPENAI_CHAT_COMPLETIONS
    )
    assert (
        normalize_inbound_protocol("chat_completions")
        == HarnessProtocol.OPENAI_CHAT_COMPLETIONS
    )
    assert (
        normalize_inbound_protocol("chat-completions")
        == HarnessProtocol.OPENAI_CHAT_COMPLETIONS
    )
    assert normalize_inbound_protocol("chat") == HarnessProtocol.OPENAI_CHAT_COMPLETIONS
    assert normalize_inbound_protocol("gemini") == HarnessProtocol.GEMINI

    with pytest.raises(ValueError):
        normalize_inbound_protocol("gopher")


# --------------------------------------------------------------------------
# 2. model_overrides.json harness_protocols persistence & validation
# --------------------------------------------------------------------------


def test_model_overrides_persists_and_validates_harness_protocols(
    tmp_path: Path,
) -> None:
    doc: dict[str, Any] = {
        HARNESS_PROTOCOLS_KEY: {
            "opencode": "openai_responses",
            "commandcode_cli": "chat_completions",
            "hermes": "messages",
            "claude": "openai_responses",  # unswitchable -> dropped
            "hermes_bad": "openai_responses",  # unknown harness -> dropped
            "droid": "openai_responses",  # unsupported for droid -> dropped
            "crush": "not_a_protocol",  # invalid -> dropped
        }
    }
    parsed = ModelParameterOverrides.from_document(doc)
    assert parsed.harness_protocol("opencode") == HarnessProtocol.OPENAI_RESPONSES
    assert (
        parsed.harness_protocol("commandcode_cli")
        == HarnessProtocol.OPENAI_CHAT_COMPLETIONS
    )
    assert parsed.harness_protocol("hermes") == HarnessProtocol.ANTHROPIC_MESSAGES
    assert parsed.harness_protocol("claude") is None
    assert parsed.harness_protocol("hermes_bad") is None
    assert parsed.harness_protocol("droid") is None
    assert parsed.harness_protocol("crush") is None

    save_model_overrides(parsed)
    reset_model_overrides_cache()
    reloaded = load_model_overrides()
    assert reloaded.harness_protocol("opencode") == HarnessProtocol.OPENAI_RESPONSES

    cleared = with_harness_protocol_row(reloaded, harness="opencode", protocol="auto")
    assert cleared.harness_protocol("opencode") is None
    save_model_overrides(cleared)
    on_disk = json.loads(
        (tmp_path / "model_overrides.json").read_text(encoding="utf-8")
    )
    assert "opencode" not in on_disk.get(HARNESS_PROTOCOLS_KEY, {})


# --------------------------------------------------------------------------
# 3. apply_inbound_protocol transformation & 0-diff when None
# --------------------------------------------------------------------------


def test_apply_inbound_protocol_zero_diff_when_none_and_accurate_schemas() -> None:
    models = _sample_models()

    for fmt in (
        "opencode",
        "kilo",
        "commandcode",
        "openclaw",
        "hermes",
        "crush",
        "droid",
    ):
        base_doc, _ = serialise(fmt, models)
        assert apply_inbound_protocol(fmt, base_doc, None) == base_doc

    # OpenCode / Kilo
    oc_base, _ = serialise("opencode", models)
    oc_resp = apply_inbound_protocol(
        "opencode", oc_base, HarnessProtocol.OPENAI_RESPONSES
    )
    assert oc_resp["provider"]["mcc"]["npm"] == "@ai-sdk/openai"
    assert oc_base["provider"]["mcc"]["npm"] == "@ai-sdk/anthropic"
    oc_chat = apply_inbound_protocol(
        "opencode", oc_resp, HarnessProtocol.OPENAI_CHAT_COMPLETIONS
    )
    assert oc_chat["provider"]["mcc"]["npm"] == "@ai-sdk/openai-compatible"
    assert (
        apply_inbound_protocol("opencode", oc_chat, HarnessProtocol.ANTHROPIC_MESSAGES)[
            "provider"
        ]["mcc"]["npm"]
        == "@ai-sdk/anthropic"
    )

    # CommandCode
    cc_base, _ = serialise("commandcode", models)
    cc_chat = apply_inbound_protocol(
        "commandcode",
        cc_base,
        HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
        proxy_root_url=ROOT,
    )
    assert cc_chat["provider"]["mcc"]["api"] == "openai-completions"
    assert cc_chat["provider"]["mcc"]["baseURL"] == f"{ROOT}/v1"

    # OpenClaw: root for anthropic-messages, /v1 for openai-completions
    claw_base, _ = serialise("openclaw", models)
    claw_msg = apply_inbound_protocol(
        "openclaw",
        claw_base,
        HarnessProtocol.ANTHROPIC_MESSAGES,
        proxy_root_url=ROOT,
    )
    assert claw_msg["models"]["providers"]["mcc"]["api"] == "anthropic-messages"
    assert claw_msg["models"]["providers"]["mcc"]["baseUrl"] == ROOT
    claw_chat = apply_inbound_protocol(
        "openclaw",
        claw_msg,
        HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
        proxy_root_url=ROOT,
    )
    assert claw_chat["models"]["providers"]["mcc"]["api"] == "openai-completions"
    assert claw_chat["models"]["providers"]["mcc"]["baseUrl"] == f"{ROOT}/v1"

    # Hermes: root for anthropic_messages, /v1 for openai_chat
    hermes_base, _ = serialise("hermes", models)
    hermes_msg = apply_inbound_protocol(
        "hermes",
        hermes_base,
        HarnessProtocol.ANTHROPIC_MESSAGES,
        proxy_root_url=ROOT,
    )
    assert hermes_msg["providers"]["mcc"]["transport"] == "anthropic_messages"
    assert hermes_msg["providers"]["mcc"]["api"] == ROOT

    # Crush: anthropic -> root, openai-compat -> /v1
    crush_base, _ = serialise("crush", models)
    crush_chat = apply_inbound_protocol(
        "crush",
        crush_base,
        HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
        proxy_root_url=ROOT,
    )
    assert crush_chat["providers"]["mcc"]["type"] == "openai-compat"
    assert crush_chat["providers"]["mcc"]["base_url"] == f"{ROOT}/v1"
    assert document_matches_inbound_protocol(
        "crush", crush_chat, HarnessProtocol.OPENAI_CHAT_COMPLETIONS
    )
    assert not document_matches_inbound_protocol(
        "crush", crush_chat, HarnessProtocol.ANTHROPIC_MESSAGES
    )
    assert (
        inbound_protocol_base_url_shape("crush", HarnessProtocol.ANTHROPIC_MESSAGES)
        == "root"
    )
    assert (
        inbound_protocol_base_url_shape(
            "crush", HarnessProtocol.OPENAI_CHAT_COMPLETIONS
        )
        == "v1"
    )

    # Droid: anthropic -> root, generic-chat-completion-api -> /v1
    droid_base, _ = serialise("droid", models)
    droid_chat = apply_inbound_protocol(
        "droid",
        droid_base,
        HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
        proxy_root_url=ROOT,
    )
    assert droid_chat["customModels"][0]["provider"] == "generic-chat-completion-api"
    assert droid_chat["customModels"][0]["baseUrl"] == f"{ROOT}/v1"
    assert (
        document_inbound_protocol("droid", droid_chat)
        == HarnessProtocol.OPENAI_CHAT_COMPLETIONS
    )


# --------------------------------------------------------------------------
# 4. Admin API routes + catalogue fan-out
# --------------------------------------------------------------------------


class _DummyProvider(BaseProvider):
    def __init__(self) -> None:
        super().__init__(
            ProviderConfig(api_key="sk-test-1234", base_url="http://127.0.0.1:18564/v1")
        )

    def preflight_stream(self, request: Any, *, reasoning: Any = None) -> None:
        return None

    async def stream_response(
        self,
        request: Any,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: Any = None,
    ) -> Any:
        if False:
            yield ""

    async def list_model_ids(self) -> frozenset[str]:
        return frozenset({"gpt-5"})

    async def cleanup(self) -> None:
        return None


def test_admin_api_harness_inbound_protocol_and_catalogue_models(
    tmp_path: Path,
) -> None:
    settings = Settings.model_validate(
        {"OPENAI_API_KEY": "sk-test-1234", "MODEL": "openai/gpt-5"}
    )
    app = create_test_app(settings, providers={"openai": _DummyProvider()})
    manager = provider_manager_for_app(app)
    manager.cache_model_infos("openai", (ProviderModelInfo("gpt-5"),))

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        initial = client.get("/admin/api/harnesses").json()
        by_id = {entry["id"]: entry for entry in initial["harnesses"]}
        assert by_id["claude"]["switchable_protocols"] == []
        assert by_id["codex"]["switchable_protocols"] == []
        assert by_id["gemini_cli"]["switchable_protocols"] == []
        assert [opt["value"] for opt in by_id["opencode"]["switchable_protocols"]] == [
            "anthropic_messages",
            "openai_responses",
            "openai_chat_completions",
        ]
        assert by_id["opencode"]["inbound_protocol"] == "anthropic_messages"
        assert by_id["opencode"]["inbound_protocol_override"] == ""

        # Reject unswitchable or unknown harnesses and unsupported protocols
        for bad in (
            {"harness": "claude", "protocol": "openai_responses"},
            {"harness": "codex", "protocol": "anthropic_messages"},
            {"harness": "antigravity", "protocol": "anthropic_messages"},
            {"harness": "nonexistent", "protocol": "openai_responses"},
            {"harness": "droid", "protocol": "openai_responses"},
            {"harness": "opencode", "protocol": "bogus"},
        ):
            assert (
                client.post("/admin/api/harness-inbound-protocol", json=bad).status_code
                == 422
            )

        # Switch opencode -> openai_responses
        resp = client.post(
            "/admin/api/harness-inbound-protocol",
            json={"harness": "opencode", "protocol": "openai_responses"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        updated_by_id = {entry["id"]: entry for entry in body["harnesses"]}
        assert updated_by_id["opencode"]["inbound_protocol"] == "openai_responses"
        assert (
            updated_by_id["opencode"]["inbound_protocol_override"] == "openai_responses"
        )
        assert "OpenAI Responses" in updated_by_id["opencode"]["protocol_label"]

        disk = json.loads(
            (tmp_path / "model_overrides.json").read_text(encoding="utf-8")
        )
        assert disk[HARNESS_PROTOCOLS_KEY] == {"opencode": "openai_responses"}

        cats = client.get("/admin/api/catalogue-models").json()["catalogues"]
        assert (
            cats["opencode"]["document"]["provider"]["mcc"]["npm"] == "@ai-sdk/openai"
        )
        assert (
            cats["opencode2"]["document"]["provider"]["mcc"]["npm"]
            == "@ai-sdk/anthropic"
        )

        # Reset back to default
        reset = client.post(
            "/admin/api/harness-inbound-protocol",
            json={"harness": "opencode", "protocol": ""},
        )
        assert reset.status_code == 200
        reset_by_id = {entry["id"]: entry for entry in reset.json()["harnesses"]}
        assert reset_by_id["opencode"]["inbound_protocol"] == "anthropic_messages"
        assert reset_by_id["opencode"]["inbound_protocol_override"] == ""
        cats_after = client.get("/admin/api/catalogue-models").json()["catalogues"]
        assert (
            cats_after["opencode"]["document"]["provider"]["mcc"]["npm"]
            == "@ai-sdk/anthropic"
        )


def test_fanout_publisher_honors_persisted_inbound_protocol_and_root_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PORT", "8940")
    save_model_overrides(
        ModelParameterOverrides.from_document(
            {
                HARNESS_PROTOCOLS_KEY: {
                    "opencode": "openai_chat_completions",
                    "crush": "openai_chat_completions",
                    "openclaw": "anthropic_messages",
                }
            }
        )
    )
    settings = Settings.model_validate(
        {"OPENAI_API_KEY": "sk-test-1234", "MODEL": "openai/gpt-5", "PORT": 8940}
    )
    app = create_test_app(settings, providers={"openai": _DummyProvider()})
    manager = provider_manager_for_app(app)
    manager._model_cache.cache_model_infos("openai", ())

    opencode_path = tmp_path / "opencode-config.json"
    crush_path = tmp_path / "crush" / "crush.json"
    openclaw_path = tmp_path / "openclaw.json"
    openclaw_path.write_text('{"theme": "dark"}\n', encoding="utf-8")

    # Claim openclaw once as anthropic_messages -> writes root baseUrl without /v1
    doc_oc, _ = serialise("openclaw", _sample_models())
    doc_oc = apply_inbound_protocol(
        "openclaw",
        doc_oc,
        HarnessProtocol.ANTHROPIC_MESSAGES,
        proxy_root_url="http://127.0.0.1:8940",
    )
    from my_claude_code.config.harness_config_merge import configure_owned_block

    claw_spec = harness_spec("openclaw")
    assert claw_spec.catalogue is not None and claw_spec.catalogue.merge is not None
    configure_owned_block(
        merge=claw_spec.catalogue.merge,
        path=openclaw_path,
        document=doc_oc,
        proxy_root_url="http://127.0.0.1:8940",
        subject="openclaw",
        claim=True,
        proxy_token=FAKE_TOKEN,
    )
    assert (
        owned_block_names(
            openclaw_path,
            claw_spec.catalogue.merge.owned_key_path,
            "http://127.0.0.1:8940",
            key=claw_spec.catalogue.merge.base_url_key,
            sentinel=claw_spec.catalogue.merge.base_url_sentinel,
            preserve_bytes=True,
            accept_root=True,
        )
        is True
    )

    publisher = HarnessCatalogueFanoutPublisher(
        catalogue_paths={
            "opencode": opencode_path,
            "crush": crush_path,
            "openclaw": openclaw_path,
        }
    )
    publisher.publish(manager)

    oc_written = json.loads(opencode_path.read_text(encoding="utf-8"))
    assert oc_written["provider"]["mcc"]["npm"] == "@ai-sdk/openai-compatible"

    crush_written = json.loads(crush_path.read_text(encoding="utf-8"))
    assert crush_written["providers"]["mcc"]["type"] == "openai-compat"
    assert crush_written["providers"]["mcc"]["base_url"] == "http://127.0.0.1:8940/v1"

    claw_written = json.loads(openclaw_path.read_text(encoding="utf-8"))
    assert claw_written["models"]["providers"]["mcc"]["api"] == "anthropic-messages"
    assert (
        claw_written["models"]["providers"]["mcc"]["baseUrl"] == "http://127.0.0.1:8940"
    )


# --------------------------------------------------------------------------
# 5. CLI flag / env precedence & stale on-disk regeneration in launchers
# --------------------------------------------------------------------------


def test_strip_inbound_protocol_flag_and_precedence() -> None:
    spec = harness_spec("opencode")
    override, cleaned = strip_inbound_protocol_flag(
        spec, ["--inbound-protocol", "responses", "run", "hi"]
    )
    assert override is not None
    assert override.protocol == HarnessProtocol.OPENAI_RESPONSES
    assert cleaned == ["run", "hi"]

    override_eq, cleaned_eq = strip_inbound_protocol_flag(
        spec, ["--inbound-protocol=chat", "--", "--inbound-protocol", "bogus"]
    )
    assert override_eq is not None
    assert override_eq.protocol == HarnessProtocol.OPENAI_CHAT_COMPLETIONS
    assert cleaned_eq == ["--", "--inbound-protocol", "bogus"]

    save_model_overrides(
        ModelParameterOverrides.from_document(
            {HARNESS_PROTOCOLS_KEY: {"opencode": "openai_responses"}}
        )
    )
    # CLI flag wins over env and model_overrides
    assert (
        resolve_launch_inbound_protocol(
            spec, override_eq, {INBOUND_PROTOCOL_ENV: "messages"}
        )
        == HarnessProtocol.OPENAI_CHAT_COMPLETIONS
    )
    # --inbound-protocol default overrides persisted model_overrides back to spec.protocol
    default_override, _ = strip_inbound_protocol_flag(
        spec, ["--inbound-protocol=default", "run"]
    )
    assert (
        resolve_launch_inbound_protocol(
            spec, default_override, {INBOUND_PROTOCOL_ENV: "chat"}
        )
        == HarnessProtocol.ANTHROPIC_MESSAGES
    )
    # Env wins over model_overrides when no flag
    assert (
        resolve_launch_inbound_protocol(spec, None, {INBOUND_PROTOCOL_ENV: "chat"})
        == HarnessProtocol.OPENAI_CHAT_COMPLETIONS
    )
    # model_overrides wins when neither flag nor env
    assert (
        resolve_launch_inbound_protocol(spec, None, {})
        == HarnessProtocol.OPENAI_RESPONSES
    )

    # Invalid value raises SystemExit(2)
    with pytest.raises(SystemExit) as exc_info:
        strip_inbound_protocol_flag(spec, ["--inbound-protocol", "nope"])
    assert exc_info.value.code == 2

    # Unswitchable harness rejects non-default protocol
    with pytest.raises(SystemExit) as exc_claude:
        strip_inbound_protocol_flag(
            harness_spec("claude"), ["--inbound-protocol", "responses"]
        )
    assert exc_claude.value.code == 2


def test_file_launchers_regenerate_when_on_disk_protocol_differs(
    tmp_path: Path,
) -> None:
    # 1. OpenCode: stale anthropic file on disk MUST regenerate to @ai-sdk/openai
    oc_path = tmp_path / "opencode-config.json"
    oc_default, _ = serialise("opencode", _sample_models())
    oc_path.write_text(json.dumps(oc_default), encoding="utf-8")

    with (
        patch(
            "my_claude_code.cli.launchers.opencode.harness_catalogue_path",
            return_value=oc_path,
        ),
        patch(
            "my_claude_code.cli.harnesses.catalogue_client.urlopen",
            return_value=_JsonResponse(_catalogue_payload("opencode", "opencode")),
        ) as mock_open,
    ):
        # Default stays 0-HTTP
        assert (
            opencode.write_harness_config(
                harness_spec("opencode"), ROOT, _SettingsStub()
            )
            == oc_path
        )
        mock_open.assert_not_called()

        # Override to responses regenerates!
        assert (
            opencode.write_harness_config(
                harness_spec("opencode"),
                ROOT,
                _SettingsStub(),
                inbound_protocol=HarnessProtocol.OPENAI_RESPONSES,
            )
            == oc_path
        )
        mock_open.assert_called_once()
    assert (
        json.loads(oc_path.read_text(encoding="utf-8"))["provider"]["mcc"]["npm"]
        == "@ai-sdk/openai"
    )

    # 2. Crush: stale anthropic (root) -> openai-compat (/v1)
    crush_path = tmp_path / "crush" / "crush.json"
    crush_path.parent.mkdir(parents=True)
    crush_default, _ = serialise("crush", _sample_models())
    crush_path.write_text(json.dumps(crush_default), encoding="utf-8")

    with (
        patch(
            "my_claude_code.cli.launchers.crush.harness_catalogue_path",
            return_value=crush_path,
        ),
        patch(
            "my_claude_code.cli.harnesses.catalogue_client.urlopen",
            return_value=_JsonResponse(_catalogue_payload("crush", "crush")),
        ),
    ):
        assert (
            crush.write_harness_config(
                harness_spec("crush"),
                ROOT,
                FAKE_TOKEN,
                inbound_protocol=HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
            )
            == crush_path
        )
    crush_doc = json.loads(crush_path.read_text(encoding="utf-8"))
    assert crush_doc["providers"]["mcc"]["type"] == "openai-compat"
    assert crush_doc["providers"]["mcc"]["base_url"] == f"{ROOT}/v1"

    # 3. Droid: stale anthropic (root) -> generic-chat-completion-api (/v1)
    droid_path = tmp_path / "droid-settings.json"
    droid_default, _ = serialise("droid", _sample_models())
    droid_path.write_text(json.dumps(droid_default), encoding="utf-8")

    with (
        patch(
            "my_claude_code.cli.launchers.droid.harness_catalogue_path",
            return_value=droid_path,
        ),
        patch(
            "my_claude_code.cli.harnesses.catalogue_client.urlopen",
            return_value=_JsonResponse(_catalogue_payload("droid", "droid")),
        ),
    ):
        assert (
            droid.write_harness_config(
                harness_spec("droid"),
                ROOT,
                FAKE_TOKEN,
                inbound_protocol=HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
            )
            == droid_path
        )
    droid_doc = json.loads(droid_path.read_text(encoding="utf-8"))
    assert droid_doc["customModels"][0]["provider"] == "generic-chat-completion-api"
    assert droid_doc["customModels"][0]["baseUrl"] == f"{ROOT}/v1"


def test_merge_launchers_switch_protocol_and_preserve_user_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # CommandCode -> openai-completions
    cc_spec = harness_spec("commandcode_cli")
    assert cc_spec.catalogue is not None and cc_spec.catalogue.merge is not None
    cc_path = merge_config_path(cc_spec.catalogue.merge, os.environ)
    cc_path.parent.mkdir(parents=True, exist_ok=True)
    cc_path.write_text(
        json.dumps({"provider": {"ollama": {}}, "theme": "dark"}), encoding="utf-8"
    )
    monkeypatch.setattr(
        commandcode,
        "fetch_catalogue_models",
        lambda *_a, **_k: _catalogue_payload("commandcode_cli", "commandcode"),
    )
    assert (
        commandcode.merge_provider_block(
            cc_spec,
            ROOT,
            FAKE_TOKEN,
            inbound_protocol=HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
        )
        is True
    )
    cc_doc = json.loads(cc_path.read_text(encoding="utf-8"))
    assert cc_doc["provider"]["mcc"]["api"] == "openai-completions"
    assert cc_doc["provider"]["mcc"]["baseURL"] == f"{ROOT}/v1"
    assert cc_doc["provider"]["ollama"] == {}
    assert cc_doc["theme"] == "dark"

    # Hermes -> anthropic_messages (root URL without /v1)
    hermes_spec_entry = harness_spec("hermes")
    assert (
        hermes_spec_entry.catalogue is not None
        and hermes_spec_entry.catalogue.merge is not None
    )
    hermes_path = merge_config_path(hermes_spec_entry.catalogue.merge, os.environ)
    hermes_path.parent.mkdir(parents=True, exist_ok=True)
    hermes_path.write_text("# user comment\n_config_version: 33\n", encoding="utf-8")
    monkeypatch.setattr(
        hermes,
        "fetch_catalogue_models",
        lambda *_a, **_k: _catalogue_payload("hermes", "hermes"),
    )
    assert (
        hermes.write_provider_block(
            hermes_spec_entry,
            ROOT,
            FAKE_TOKEN,
            inbound_protocol=HarnessProtocol.ANTHROPIC_MESSAGES,
        )
        is True
    )
    hermes_text = hermes_path.read_text(encoding="utf-8")
    assert "# user comment" in hermes_text
    hermes_block = yaml_mapping_at(hermes_text, ("providers", "mcc"))
    assert hermes_block is not None
    assert hermes_block["transport"] == "anthropic_messages"
    assert hermes_block["api"] == ROOT

    # OpenClaw -> anthropic-messages (root URL without /v1)
    claw_spec = harness_spec("openclaw")
    assert claw_spec.catalogue is not None and claw_spec.catalogue.merge is not None
    claw_path = merge_config_path(claw_spec.catalogue.merge, os.environ)
    claw_path.parent.mkdir(parents=True, exist_ok=True)
    claw_path.write_text('// keep comment\n{"theme": "dark"}\n', encoding="utf-8")
    monkeypatch.setattr(
        openclaw,
        "fetch_catalogue_models",
        lambda *_a, **_k: _catalogue_payload("openclaw", "openclaw"),
    )
    monkeypatch.setattr(
        "my_claude_code.config.harness_config_merge.get_settings",
        lambda: _SettingsStub(FAKE_TOKEN),
    )
    assert (
        openclaw.write_provider_block(
            claw_spec,
            ROOT,
            FAKE_TOKEN,
            inbound_protocol=HarnessProtocol.ANTHROPIC_MESSAGES,
        )
        is True
    )
    claw_text = claw_path.read_text(encoding="utf-8")
    assert "// keep comment" in claw_text
    claw_doc = json.loads(claw_text.replace("// keep comment\n", ""))
    assert claw_doc["models"]["providers"]["mcc"]["api"] == "anthropic-messages"
    assert claw_doc["models"]["providers"]["mcc"]["baseUrl"] == ROOT


def test_launcher_end_to_end_strips_flag_and_runs_clean_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oc_path = tmp_path / "opencode-config.json"
    captured: dict[str, Any] = {}

    def fake_run(**kwargs: Any) -> None:
        captured["command"] = kwargs["command"]
        captured["env"] = kwargs["env"]

    monkeypatch.setattr(
        opencode, "resolve_harness_binary", lambda _spec: "opencode-bin"
    )
    monkeypatch.setattr(opencode, "preflight_proxy", lambda _url: None)
    monkeypatch.setattr(opencode, "get_settings", lambda: _SettingsStub(FAKE_TOKEN))
    monkeypatch.setattr(opencode, "local_proxy_root_url", lambda _s: ROOT)
    monkeypatch.setattr(opencode, "harness_catalogue_path", lambda _fn: oc_path)
    monkeypatch.setattr(opencode, "run_client_process", fake_run)
    monkeypatch.setattr(
        "my_claude_code.cli.harnesses.catalogue_client.urlopen",
        lambda *_a, **_k: _JsonResponse(_catalogue_payload("opencode", "opencode")),
    )

    opencode.launch(["--inbound-protocol", "responses", "run", "hello"])

    assert captured["command"] == ["opencode-bin", "run", "hello"]
    assert captured["env"]["OPENCODE_CONFIG"] == str(oc_path)
    assert (
        json.loads(oc_path.read_text(encoding="utf-8"))["provider"]["mcc"]["npm"]
        == "@ai-sdk/openai"
    )
