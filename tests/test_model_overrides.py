"""Per-provider and per-model request parameter overrides."""

import json
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.config import model_overrides as overrides_module
from my_claude_code.config.model_overrides import (
    ALLOWED_OVERRIDE_PARAMETERS,
    EMPTY_MODEL_OVERRIDES,
    OWNED_ELSEWHERE_PARAMETERS,
    ModelParameterOverrides,
    apply_model_parameter_overrides,
    current_model_overrides,
    load_model_overrides,
    model_ref_for,
    save_model_overrides,
)
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import DEFAULT_REASONING_POLICY
from my_claude_code.providers.nvidia_nim.request_options import build_nim_request_body
from my_claude_code.providers.openai_chat import build_openai_chat_request_body
from my_claude_code.providers.openrouter_gateway import openrouter_gateway_profile

# Real refs from the user's own routing, so a rename upstream is visible here.
NIM_REF = "nvidia_nim/moonshotai/kimi-k3"
COMMANDCODE_REF = "commandcode/z-ai/glm-5.3-flash"
NOUS_REF = "nous_portal/tencent/hy3:free"


def _request(model: str) -> MessagesRequest:
    return MessagesRequest.model_validate(
        {
            "model": model,
            "max_tokens": 128,
            "messages": [{"role": "user", "content": "hello"}],
        }
    )


def _write(path: Path, document: object) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _clear_cache():
    overrides_module.reset_model_overrides_cache()
    yield
    overrides_module.reset_model_overrides_cache()


def test_the_allowed_set_excludes_everything_another_layer_owns():
    assert ALLOWED_OVERRIDE_PARAMETERS.isdisjoint(OWNED_ELSEWHERE_PARAMETERS)
    assert "max_tokens" in OWNED_ELSEWHERE_PARAMETERS
    assert not {name for name in ALLOWED_OVERRIDE_PARAMETERS if "reason" in name}
    assert not {name for name in ALLOWED_OVERRIDE_PARAMETERS if "think" in name}


def test_a_missing_file_is_not_an_error(tmp_path: Path):
    assert load_model_overrides(tmp_path / "nothing.json") == EMPTY_MODEL_OVERRIDES


def test_an_empty_file_is_not_an_error(tmp_path: Path):
    path = tmp_path / "model_overrides.json"
    path.write_text("   \n", encoding="utf-8")
    assert load_model_overrides(path) == EMPTY_MODEL_OVERRIDES


def test_corrupt_json_is_logged_and_treated_as_empty(tmp_path: Path):
    path = tmp_path / "model_overrides.json"
    path.write_text('{"providers": {', encoding="utf-8")
    assert load_model_overrides(path) == EMPTY_MODEL_OVERRIDES


def test_a_non_object_document_is_treated_as_empty(tmp_path: Path):
    assert load_model_overrides(_write(tmp_path / "a.json", [1, 2])).is_empty


def test_malformed_sections_and_entries_are_ignored(tmp_path: Path):
    path = _write(
        tmp_path / "a.json",
        {
            "providers": "not an object",
            "models": {
                NIM_REF: ["not", "an", "object"],
                COMMANDCODE_REF: {"top_p": 0.5},
            },
        },
    )
    loaded = load_model_overrides(path)
    assert loaded.providers == {}
    assert loaded.models == {COMMANDCODE_REF: {"top_p": 0.5}}


def test_an_unknown_parameter_is_dropped_rather_than_injected(tmp_path: Path):
    path = _write(
        tmp_path / "a.json",
        {"providers": {"nvidia_nim": {"top_pp": 0.9, "top_p": 0.95}}},
    )
    assert load_model_overrides(path).providers == {"nvidia_nim": {"top_p": 0.95}}


def test_parameters_owned_elsewhere_are_dropped(tmp_path: Path):
    path = _write(
        tmp_path / "a.json",
        {
            "providers": {
                "nvidia_nim": {
                    "max_tokens": 100000,
                    "reasoning_effort": "high",
                    "thinking": {"type": "enabled"},
                    "temperature": 0.2,
                }
            }
        },
    )
    assert load_model_overrides(path).providers == {"nvidia_nim": {"temperature": 0.2}}


def test_an_object_value_is_rejected(tmp_path: Path):
    path = _write(tmp_path / "a.json", {"providers": {"x": {"top_p": {"a": 1}}}})
    assert load_model_overrides(path).is_empty


def test_a_list_value_is_kept_for_stop(tmp_path: Path):
    path = _write(tmp_path / "a.json", {"providers": {"x": {"stop": ["\n\n"]}}})
    assert load_model_overrides(path).providers == {"x": {"stop": ["\n\n"]}}


def test_model_beats_provider_and_null_beats_a_value():
    table = ModelParameterOverrides(
        providers={"nvidia_nim": {"top_p": 0.5, "temperature": 0.7, "seed": 3}},
        models={NIM_REF: {"top_p": 0.95, "temperature": None}},
    )
    assert table.resolve("nvidia_nim", NIM_REF) == {
        "top_p": 0.95,
        "temperature": None,
        "seed": 3,
    }


def test_keys_are_matched_case_insensitively(tmp_path: Path):
    path = _write(
        tmp_path / "a.json",
        {"models": {" NVIDIA_NIM/MoonshotAI/Kimi-K3 ": {"seed": 1}}},
    )
    assert load_model_overrides(path).resolve("nvidia_nim", NIM_REF) == {"seed": 1}


def test_model_ref_is_built_from_provider_and_model():
    assert model_ref_for("nvidia_nim", "moonshotai/kimi-k3") == NIM_REF
    assert model_ref_for("nous_portal", "tencent/hy3:free") == NOUS_REF
    # A caller that already passed a full ref must not have it doubled.
    assert model_ref_for("nvidia_nim", NIM_REF) == NIM_REF
    assert model_ref_for("", "kimi") == "kimi"


def test_applying_forces_values_and_removes_nulls():
    body: dict[str, Any] = {"model": "x", "temperature": 0.9, "top_k": 40}
    applied = apply_model_parameter_overrides(
        body,
        provider_id="nvidia_nim",
        model_ref=NIM_REF,
        overrides=ModelParameterOverrides(
            providers={"nvidia_nim": {"temperature": 0.1}},
            models={NIM_REF: {"top_p": 0.95, "temperature": None}},
        ),
    )
    assert body == {"model": "x", "top_k": 40, "top_p": 0.95}
    assert applied == {"top_p": 0.95, "temperature": None}


def test_forcing_off_a_key_nothing_set_reports_nothing_applied():
    body: dict[str, Any] = {"model": "x"}
    applied = apply_model_parameter_overrides(
        body,
        provider_id="nvidia_nim",
        model_ref=NIM_REF,
        overrides=ModelParameterOverrides(models={NIM_REF: {"top_p": None}}),
    )
    assert body == {"model": "x"}
    assert applied == {}


def test_an_empty_table_never_touches_the_body():
    body: dict[str, Any] = {"model": "x", "temperature": 0.9}
    assert (
        apply_model_parameter_overrides(
            body,
            provider_id="nvidia_nim",
            model_ref=NIM_REF,
            overrides=EMPTY_MODEL_OVERRIDES,
        )
        == {}
    )
    assert body == {"model": "x", "temperature": 0.9}


def test_saving_is_atomic_and_leaves_no_staging_file(tmp_path: Path):
    path = tmp_path / "nested" / "model_overrides.json"
    save_model_overrides(
        ModelParameterOverrides(models={NOUS_REF: {"top_p": 0.9}}), path
    )
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "providers": {},
        "models": {NOUS_REF: {"top_p": 0.9}},
    }
    assert [child.name for child in path.parent.iterdir()] == [path.name]
    assert load_model_overrides(path).models == {NOUS_REF: {"top_p": 0.9}}


def test_the_cache_follows_the_file(tmp_path: Path):
    path = tmp_path / "model_overrides.json"
    assert current_model_overrides(path).is_empty

    save_model_overrides(ModelParameterOverrides(providers={"kilo": {"seed": 7}}), path)
    assert current_model_overrides(path).providers == {"kilo": {"seed": 7}}

    save_model_overrides(ModelParameterOverrides(providers={"kilo": {"seed": 8}}), path)
    assert current_model_overrides(path).providers == {"kilo": {"seed": 8}}


# --- End to end through a real provider profile -----------------------------


def _commandcode_body(overrides: ModelParameterOverrides) -> dict[str, Any]:
    profile = openrouter_gateway_profile("COMMANDCODE")
    return build_openai_chat_request_body(
        _request("z-ai/glm-5.3-flash"),
        reasoning=DEFAULT_REASONING_POLICY,
        policy=profile.request_policy,
        postprocessors=profile.request_postprocessors,
        provider_id="commandcode",
        overrides=overrides,
    )


def test_an_absent_override_leaves_the_body_byte_identical():
    """The regression guard: nothing is injected until somebody asks for it."""

    profile = openrouter_gateway_profile("COMMANDCODE")
    baseline = build_openai_chat_request_body(
        _request("z-ai/glm-5.3-flash"),
        reasoning=DEFAULT_REASONING_POLICY,
        policy=profile.request_policy,
        postprocessors=profile.request_postprocessors,
    )
    assert json.dumps(_commandcode_body(EMPTY_MODEL_OVERRIDES), sort_keys=True) == (
        json.dumps(baseline, sort_keys=True)
    )
    assert "top_p" not in baseline
    assert "temperature" not in baseline


def test_a_provider_level_value_reaches_a_real_body():
    body = _commandcode_body(
        ModelParameterOverrides(providers={"commandcode": {"top_p": 0.8}})
    )
    assert body["top_p"] == 0.8


def test_a_model_level_value_beats_the_provider_level_one_end_to_end():
    body = _commandcode_body(
        ModelParameterOverrides(
            providers={"commandcode": {"top_p": 0.8}},
            models={COMMANDCODE_REF: {"top_p": 0.95}},
        )
    )
    assert body["top_p"] == 0.95


def test_a_model_level_null_removes_what_the_provider_level_set():
    body = _commandcode_body(
        ModelParameterOverrides(
            providers={"commandcode": {"top_p": 0.8}},
            models={COMMANDCODE_REF: {"top_p": None}},
        )
    )
    assert "top_p" not in body


def test_an_override_wins_over_a_provider_postprocessor(
    monkeypatch: pytest.MonkeyPatch,
):
    """NIM's own settings run first; the user's value has to survive them."""

    from my_claude_code.config.nim import NimSettings

    monkeypatch.setattr(
        "my_claude_code.providers.openai_chat.request_policy.current_model_overrides",
        lambda: EMPTY_MODEL_OVERRIDES,
    )
    nim = NimSettings(top_p=0.5)
    without = build_nim_request_body(
        _request("moonshotai/kimi-k3"),
        nim,
        reasoning=DEFAULT_REASONING_POLICY,
        provider_id="nvidia_nim",
    )
    assert without["top_p"] == 0.5

    # No ``overrides=`` this time: the default path reads the process-wide
    # table, which is what a real request does.
    monkeypatch.setattr(
        "my_claude_code.providers.openai_chat.request_policy.current_model_overrides",
        lambda: ModelParameterOverrides(models={NIM_REF: {"top_p": 0.95}}),
    )
    with_override = build_nim_request_body(
        _request("moonshotai/kimi-k3"),
        nim,
        reasoning=DEFAULT_REASONING_POLICY,
        provider_id="nvidia_nim",
    )
    assert with_override["top_p"] == 0.95


def test_no_provider_id_means_no_override_lookup():
    body = build_openai_chat_request_body(
        _request("tencent/hy3:free"),
        reasoning=DEFAULT_REASONING_POLICY,
        policy=openrouter_gateway_profile("COMMANDCODE").request_policy,
        overrides=ModelParameterOverrides(providers={"": {"top_p": 0.1}}),
    )
    assert "top_p" not in body
