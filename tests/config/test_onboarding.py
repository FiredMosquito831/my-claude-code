"""Tests for config/onboarding.py."""

import re
from pathlib import Path

import pytest

from my_claude_code.config.admin.manifest import FIELD_BY_KEY
from my_claude_code.config.onboarding import (
    OnboardingError,
    build_state,
    load_persisted,
    save_persisted,
)
from my_claude_code.config.paths import onboarding_state_path

# Dashboard markup the Get Started buttons scroll to and highlight; targets
# are asserted against this file so a renamed id fails the test instead of
# quietly pointing the button at nothing.
ADMIN_STATIC_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "my_claude_code"
    / "api"
    / "admin_static"
)

EXPECTED_STEP_IDS = (
    "provider",
    "models",
    "client",
    "coding_agents",
    "websearch",
    "messaging",
    "analytics",
    "guide",
)


def _set_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)


class TestLoadPersisted:
    def test_missing_state_file_returns_defaults(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)

        dismissed, visited = load_persisted()

        assert dismissed is False
        assert visited == []

    def test_malformed_state_file_returns_defaults(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = onboarding_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json{{{", encoding="utf-8")

        dismissed, visited = load_persisted()

        assert dismissed is False
        assert visited == []

    def test_non_dict_json_returns_defaults(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = onboarding_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[1, 2, 3]", encoding="utf-8")

        dismissed, visited = load_persisted()

        assert dismissed is False
        assert visited == []

    def test_malformed_visited_field_falls_back_to_empty_list(
        self, monkeypatch, tmp_path
    ):
        _set_home(monkeypatch, tmp_path)
        path = onboarding_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"dismissed": true, "visited": "not-a-list"}', encoding="utf-8"
        )

        dismissed, visited = load_persisted()

        assert dismissed is True
        assert visited == []


class TestSavePersisted:
    def test_round_trip(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)

        save_persisted(dismissed=True, visited=["guide"])
        dismissed, visited = load_persisted()

        assert dismissed is True
        assert visited == ["guide"]

    def test_creates_parent_dirs(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        assert not onboarding_state_path().parent.exists()

        save_persisted(dismissed=False, visited=[])

        assert onboarding_state_path().is_file()

    def test_write_failure_raises_onboarding_error(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        # Make the parent path a file so mkdir(parents=True) fails with OSError.
        blocker = onboarding_state_path().parent
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("blocked", encoding="utf-8")

        with pytest.raises(OnboardingError):
            save_persisted(dismissed=False, visited=[])


class TestBuildState:
    def test_step_order_and_ids(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)

        state = build_state(claude_settings_configured=False, has_requests=False)

        assert tuple(step.id for step in state.steps) == EXPECTED_STEP_IDS

    def test_provider_step_done_when_credential_configured(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        monkeypatch.setenv("NVIDIA_NIM_API_KEY", "test-key")

        state = build_state(claude_settings_configured=False, has_requests=False)

        provider_step = next(step for step in state.steps if step.id == "provider")
        assert provider_step.done is True

    def test_provider_step_not_done_when_no_credential(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        for key in (
            "NVIDIA_NIM_API_KEY",
            "OPENROUTER_API_KEY",
            "GEMINI_API_KEY",
            "DEEPSEEK_API_KEY",
            "MISTRAL_API_KEY",
        ):
            monkeypatch.delenv(key, raising=False)

        state = build_state(claude_settings_configured=False, has_requests=False)

        provider_step = next(step for step in state.steps if step.id == "provider")
        assert provider_step.done is False

    def test_client_step_reflects_caller_supplied_flag(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)

        state = build_state(claude_settings_configured=True, has_requests=False)

        client_step = next(step for step in state.steps if step.id == "client")
        assert client_step.done is True

    def test_analytics_step_reflects_caller_supplied_flag(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)

        state = build_state(claude_settings_configured=False, has_requests=True)

        analytics_step = next(step for step in state.steps if step.id == "analytics")
        assert analytics_step.done is True

    def test_guide_step_done_when_visited(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        save_persisted(dismissed=False, visited=["guide"])

        state = build_state(claude_settings_configured=False, has_requests=False)

        guide_step = next(step for step in state.steps if step.id == "guide")
        assert guide_step.done is True

    def test_required_total_counts_only_non_optional_steps(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)

        state = build_state(claude_settings_configured=False, has_requests=False)

        non_optional_ids = {"provider", "models", "client"}
        assert state.required_total == len(non_optional_ids)
        assert {
            step.id for step in state.steps if not step.optional
        } == non_optional_ids

    def test_complete_true_when_all_required_steps_done_regardless_of_optional(
        self, monkeypatch, tmp_path
    ):
        _set_home(monkeypatch, tmp_path)
        monkeypatch.setenv("NVIDIA_NIM_API_KEY", "test-key")

        state = build_state(claude_settings_configured=True, has_requests=False)

        # models is always done (MODEL has a manifest default), provider and
        # client are satisfied above; every optional step here is undone.
        optional_steps = [step for step in state.steps if step.optional]
        assert any(not step.done for step in optional_steps)
        assert state.complete is True

    def test_complete_false_when_a_required_step_is_undone(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        for key in (
            "NVIDIA_NIM_API_KEY",
            "OPENROUTER_API_KEY",
            "GEMINI_API_KEY",
            "DEEPSEEK_API_KEY",
            "MISTRAL_API_KEY",
        ):
            monkeypatch.delenv(key, raising=False)

        state = build_state(claude_settings_configured=True, has_requests=False)

        assert state.complete is False


class TestFallbackModelStep:
    """The models step must mean something on a brand-new install.

    MODEL ships with a template value, so a non-empty check reports this
    required step as already done for a user who has configured nothing. It is
    derived from whether the fallback can actually serve a request instead.
    """

    def _state(self, monkeypatch, values):
        monkeypatch.setattr(
            "my_claude_code.config.onboarding.load_value_state",
            lambda: {
                key: {"value": value, "source": "template"}
                for key, value in values.items()
            },
        )
        return build_state(claude_settings_configured=False, has_requests=False)

    def _step(self, state, step_id):
        return next(step for step in state.steps if step.id == step_id)

    def test_template_model_without_its_provider_credential_is_not_done(
        self, monkeypatch, tmp_path
    ):
        _set_home(monkeypatch, tmp_path)
        state = self._state(
            monkeypatch, {"MODEL": "nvidia_nim/nvidia/nemotron-3-super-120b-a12b"}
        )

        assert self._step(state, "models").done is False
        assert state.required_done == 0

    def test_model_is_done_once_its_provider_has_a_credential(
        self, monkeypatch, tmp_path
    ):
        _set_home(monkeypatch, tmp_path)
        state = self._state(
            monkeypatch,
            {
                "MODEL": "nvidia_nim/nvidia/nemotron-3-super-120b-a12b",
                "NVIDIA_NIM_API_KEY": "nvapi-something",
            },
        )

        assert self._step(state, "models").done is True

    def test_empty_model_is_not_done(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        state = self._state(monkeypatch, {"MODEL": ""})

        assert self._step(state, "models").done is False

    def test_unrecognised_provider_prefix_is_taken_at_face_value(
        self, monkeypatch, tmp_path
    ):
        # A custom provider the catalog does not know about was still a
        # deliberate choice; calling a working setup incomplete is worse.
        _set_home(monkeypatch, tmp_path)
        state = self._state(monkeypatch, {"MODEL": "my_custom_thing/some-model"})

        assert self._step(state, "models").done is True


class TestStepInstructionsAndTargets:
    """Every step must say *how*, and its button must point somewhere real."""

    def test_every_step_has_concrete_instructions(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)

        state = build_state(claude_settings_configured=False, has_requests=False)

        for step in state.steps:
            assert len(step.instructions) > 0, f"{step.id} has no instructions"
            for instruction in step.instructions:
                assert instruction.strip(), f"{step.id} has a blank instruction"

    def test_every_step_view_is_a_known_dashboard_view(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        # Read the real nav rather than restating it: this set was hand-kept
        # and had already fallen four views behind (coding_agents, models,
        # limits, docs), so it would have passed a step pointing at a view
        # that does not exist.
        script = (ADMIN_STATIC_DIR / "admin.js").read_text(encoding="utf-8")
        start = script.index("const VIEW_GROUPS = [")
        block = script[start : script.index("\n];", start)]
        known_views = set(re.findall(r'id:\s*"([a-z_]+)"', block))
        assert "coding_agents" in known_views

        state = build_state(claude_settings_configured=False, has_requests=False)

        for step in state.steps:
            assert step.view in known_views, (
                f"{step.id} targets unknown view {step.view!r}"
            )

    def test_id_selector_targets_exist_in_dashboard_markup(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        index_html = (ADMIN_STATIC_DIR / "index.html").read_text(encoding="utf-8")

        state = build_state(claude_settings_configured=False, has_requests=False)

        id_targets = [
            step.target
            for step in state.steps
            if step.target and step.target.startswith("#")
        ]
        assert id_targets, "expected at least one id-selector target"
        for target in id_targets:
            dom_id = target[1:]
            assert f'id="{dom_id}"' in index_html, (
                f"missing id={dom_id!r} in index.html"
            )

    def test_data_key_selector_targets_reference_real_field_keys(
        self, monkeypatch, tmp_path
    ):
        _set_home(monkeypatch, tmp_path)

        state = build_state(claude_settings_configured=False, has_requests=False)

        data_key_targets = [
            step.target
            for step in state.steps
            if step.target and step.target.startswith('[data-key="')
        ]
        assert data_key_targets, "expected at least one data-key selector target"
        for target in data_key_targets:
            key = target.split('"')[1]
            assert key in FIELD_BY_KEY, f"{key!r} is not a real admin config field key"
