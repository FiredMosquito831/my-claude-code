"""Tests for config/onboarding.py."""

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from my_claude_code.config.admin.manifest import FIELD_BY_KEY
from my_claude_code.config.onboarding import (
    MAX_SKIPPED,
    OnboardingAgent,
    OnboardingError,
    build_state,
    detect_agents,
    is_skippable_id,
    load_persisted,
    normalize_skipped,
    read_persisted,
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
    "connect_agents",
    # 7.99.0: the media server step (the user's 2026-10-18 12:53 amendment).
    "media_tools",
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

        # 7.122.0 (decision 2): "Check it worked" is the fourth essential.
        non_optional_ids = {"provider", "models", "client", "analytics"}
        assert state.required_total == len(non_optional_ids) == 4
        assert {
            step.id for step in state.steps if not step.optional
        } == non_optional_ids

    def test_complete_true_when_all_required_steps_done_regardless_of_optional(
        self, monkeypatch, tmp_path
    ):
        _set_home(monkeypatch, tmp_path)
        monkeypatch.setenv("NVIDIA_NIM_API_KEY", "test-key")

        # 7.122.0: the fourth essential is a first request, so it is given here.
        state = build_state(claude_settings_configured=True, has_requests=True)

        # models is always done (MODEL has a manifest default), provider,
        # client and analytics are satisfied above; every optional step here
        # is undone.
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


class TestConnectAgentsStep:
    """The Get Started step that lists what is actually on this machine.

    Until 6.56.0 there were two steps here and both were "done" because the
    user had *visited* the Coding agents page -- a fact about navigation, not
    about anything being connected. These assert the replacement: real
    detection, and completion derived from real traffic or a real file.
    """

    def test_the_step_lists_clis_and_desktop_apps_it_detected(
        self, monkeypatch, tmp_path
    ):
        _set_home(monkeypatch, tmp_path)
        agents = detect_agents(
            env={"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
        )

        state = build_state(
            claude_settings_configured=False, has_requests=False, agents=agents
        )
        step = next(s for s in state.steps if s.id == "connect_agents")

        assert step.agents == agents
        assert {agent.kind for agent in step.agents} == {"cli", "desktop"}
        assert any(agent.id == "claude" for agent in step.agents)
        assert any(agent.id == "claude_desktop" for agent in step.agents)
        # A NOT_ROUTABLE app is not something the user can connect, so it is
        # not offered as one.
        assert not any(agent.id == "lm_studio" for agent in step.agents)

    def test_the_step_links_to_both_guide_sections(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        state = build_state(claude_settings_configured=False, has_requests=False)
        step = next(s for s in state.steps if s.id == "connect_agents")

        assert step.guide_anchors == ("guide-cli", "guide-desktop-apps")

    def test_no_agent_no_traffic_leaves_the_step_undone(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        state = build_state(
            claude_settings_configured=False, has_requests=False, agents=()
        )

        assert not next(s for s in state.steps if s.id == "connect_agents").done

    def test_one_request_in_seven_days_completes_the_step(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        agents = (
            OnboardingAgent(
                id="codex",
                display_name="Codex",
                kind="cli",
                installed=True,
                requests_7d=1,
            ),
        )

        state = build_state(
            claude_settings_configured=False, has_requests=False, agents=agents
        )

        assert next(s for s in state.steps if s.id == "connect_agents").done

    def test_a_configured_desktop_app_completes_the_step_without_traffic(
        self, monkeypatch, tmp_path
    ):
        """Two ways to be connected, and a file that points here is one."""

        _set_home(monkeypatch, tmp_path)
        agents = (
            OnboardingAgent(
                id="codex_desktop",
                display_name="Codex desktop",
                kind="desktop",
                installed=True,
                state="configured",
            ),
        )

        state = build_state(
            claude_settings_configured=False, has_requests=False, agents=agents
        )

        assert next(s for s in state.steps if s.id == "connect_agents").done

    def test_detection_reports_the_same_answer_as_the_coding_agents_page(
        self, monkeypatch, tmp_path
    ):
        """One detection, not two that agree until one is edited."""

        from my_claude_code.config.desktop_apply import is_installed
        from my_claude_code.config.desktop_apps import DESKTOP_APPS_BY_ID

        _set_home(monkeypatch, tmp_path)
        env = {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
        agents = {a.id: a for a in detect_agents(env=env) if a.kind == "desktop"}

        for app_id, agent in agents.items():
            assert agent.installed is is_installed(DESKTOP_APPS_BY_ID[app_id], env)

    def test_detection_never_raises_on_a_machine_it_cannot_probe(
        self, monkeypatch, tmp_path
    ):
        """The checklist is the first page a new user sees; it cannot throw."""

        _set_home(monkeypatch, tmp_path)
        assert detect_agents(env={}) is not None


def _step(state, step_id):
    return next(step for step in state.steps if step.id == step_id)


class TestConnectACodingAgent:
    """Essential 3 (7.122.0, decision 2): ANY agent reaching MCC counts.

    Until 7.122.0 the step read only the native settings.json, so a user who
    ran ``mcc-claude`` -- or Claude Code inside WSL, or Codex, or a desktop
    app -- could never finish it while the same page listed Claude Code as
    Connected, and the dashboard reopened Get Started on every load (U2).
    """

    def test_a_launcher_only_claude_code_user_is_connected(self, monkeypatch, tmp_path):
        """The U2 case: mcc-claude sent requests, no settings.json points here."""

        _set_home(monkeypatch, tmp_path)
        agents = (
            OnboardingAgent(
                id="claude",
                display_name="Claude Code",
                kind="cli",
                installed=True,
                command="mcc-claude",
                requests_7d=5,
            ),
        )

        state = build_state(
            claude_settings_configured=False, has_requests=False, agents=agents
        )

        assert _step(state, "client").done is True
        assert _step(state, "client").label == "Connect a coding agent"

    def test_another_agents_traffic_connects_it(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        agents = (
            OnboardingAgent(
                id="codex",
                display_name="Codex CLI",
                kind="cli",
                installed=False,
                requests_7d=1,
            ),
        )

        state = build_state(
            claude_settings_configured=False, has_requests=False, agents=agents
        )

        assert _step(state, "client").done is True

    def test_a_desktop_app_whose_file_points_here_connects_it(
        self, monkeypatch, tmp_path
    ):
        _set_home(monkeypatch, tmp_path)
        agents = (
            OnboardingAgent(
                id="codex_desktop",
                display_name="Codex desktop",
                kind="desktop",
                installed=True,
                state="configured",
            ),
        )

        state = build_state(
            claude_settings_configured=False, has_requests=False, agents=agents
        )

        assert _step(state, "client").done is True

    def test_any_logged_request_connects_it_however_old(self, monkeypatch, tmp_path):
        """An idle week must not bring the page back for a working setup."""

        _set_home(monkeypatch, tmp_path)
        state = build_state(claude_settings_configured=False, has_requests=True)

        assert _step(state, "client").done is True

    def test_claude_settings_pointing_here_connect_it(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        state = build_state(claude_settings_configured=True, has_requests=False)

        assert _step(state, "client").done is True

    def test_nothing_connected_leaves_it_to_do(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        agents = (
            OnboardingAgent(
                id="claude", display_name="Claude Code", kind="cli", installed=True
            ),
            OnboardingAgent(
                id="codex_desktop",
                display_name="Codex desktop",
                kind="desktop",
                installed=True,
                state="installed",
            ),
        )

        state = build_state(
            claude_settings_configured=False, has_requests=False, agents=agents
        )

        assert _step(state, "client").done is False
        assert state.complete is False

    def test_the_old_connect_agents_step_is_unchanged(self, monkeypatch, tmp_path):
        """The pinned optional step keeps its own rule and words."""

        _set_home(monkeypatch, tmp_path)
        state = build_state(claude_settings_configured=True, has_requests=True)
        step = _step(state, "connect_agents")

        assert step.done is False  # no agent passed: its rule is the agents'
        assert step.label == "Connect your coding agents (optional)"
        assert step.optional is True


class TestCheckItWorked:
    """Essential 4 (7.122.0, decision 2), and when MCC cannot measure it."""

    def test_it_is_an_essential_done_by_a_first_request(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        before = build_state(claude_settings_configured=True, has_requests=False)
        after = build_state(claude_settings_configured=True, has_requests=True)

        assert _step(before, "analytics").optional is False
        assert _step(before, "analytics").done is False
        assert _step(after, "analytics").done is True
        assert after.required_done == before.required_done + 1
        assert _step(after, "analytics").label == (
            "Check it worked — see your first request in Analytics"
        )

    def test_with_the_request_log_off_it_is_not_measured_and_not_counted(
        self, monkeypatch, tmp_path
    ):
        _set_home(monkeypatch, tmp_path)
        monkeypatch.setenv("NVIDIA_NIM_API_KEY", "test-key")

        state = build_state(
            claude_settings_configured=True,
            has_requests=False,
            request_log_enabled=False,
        )

        step = _step(state, "analytics")
        assert step.measurable is False
        assert step.optional is False
        assert state.required_total == 3
        # Provider, model and agent are done, and nothing else is counted.
        assert state.required_done == 3
        assert state.complete is True

    def test_every_other_step_is_always_measurable(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        state = build_state(
            claude_settings_configured=False,
            has_requests=False,
            request_log_enabled=False,
        )

        assert [step.id for step in state.steps if not step.measurable] == ["analytics"]


class TestWebSearchStepWords:
    """U3: web search works with no key, and keys are added from Manage keys."""

    def test_the_step_no_longer_asks_for_a_key_first(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        step = _step(
            build_state(claude_settings_configured=False, has_requests=False),
            "websearch",
        )

        text = " ".join((step.description, *step.instructions))
        assert "no key" in step.description
        assert "Manage keys" in text and "Add key" in text
        assert "field that appears below" not in text
        assert "Click Apply" not in text


class TestHiddenCards:
    """ "Not for me" (7.122.0, decision 5): stored in MCC's config folder."""

    def test_a_hidden_list_round_trips(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)

        save_persisted(
            dismissed=False, visited=["guide"], skipped=["proxying", "reasoning"]
        )

        persisted = read_persisted()
        assert persisted.skipped == ("proxying", "reasoning")
        assert persisted.visited == ("guide",)
        state = build_state(claude_settings_configured=False, has_requests=False)
        assert state.skipped == ("proxying", "reasoning")

    def test_a_file_written_before_7_122_reads_as_nothing_hidden(
        self, monkeypatch, tmp_path
    ):
        _set_home(monkeypatch, tmp_path)
        path = onboarding_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"dismissed": True, "visited": ["guide"]}), encoding="utf-8"
        )

        assert load_persisted() == (True, ["guide"])
        assert read_persisted().skipped == ()
        assert (
            build_state(claude_settings_configured=False, has_requests=False).skipped
            == ()
        )

    @pytest.mark.parametrize(
        "content",
        [
            "not json{{{",
            "[1, 2]",
            '{"dismissed": false, "skipped": "proxying"}',
            '{"dismissed": false, "skipped": {"a": 1}}',
            "",
        ],
    )
    def test_a_damaged_file_hides_nothing_and_never_raises(
        self, monkeypatch, tmp_path, content
    ):
        _set_home(monkeypatch, tmp_path)
        path = onboarding_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

        assert read_persisted().skipped == ()
        assert (
            build_state(claude_settings_configured=False, has_requests=False).skipped
            == ()
        )

    def test_only_well_formed_ids_are_ever_kept(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = onboarding_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "skipped": [
                        "reasoning",
                        "<img src=x onerror=alert(1)>",
                        "Proxying",
                        "../../etc",
                        "a" * 65,
                        7,
                        None,
                        "reasoning",
                        "fallback_chains",
                    ]
                }
            ),
            encoding="utf-8",
        )

        assert read_persisted().skipped == ("fallback_chains", "reasoning")

    def test_the_hidden_list_is_capped(self):
        many = [f"card_{index:03d}" for index in range(MAX_SKIPPED + 40)]

        assert len(normalize_skipped(many)) == MAX_SKIPPED
        assert is_skippable_id("more_api") is True
        assert is_skippable_id("") is False
        assert is_skippable_id("x y") is False

    def test_a_save_that_does_not_mention_the_list_keeps_it(
        self, monkeypatch, tmp_path
    ):
        """Dismiss and a visit can never un-hide a card."""

        _set_home(monkeypatch, tmp_path)
        save_persisted(dismissed=False, visited=[], skipped=["proxying"])

        save_persisted(dismissed=True, visited=["guide"])

        assert read_persisted().skipped == ("proxying",)
        assert read_persisted().dismissed is True

    def test_an_empty_list_shows_everything_again(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        save_persisted(dismissed=False, visited=[], skipped=["proxying"])

        save_persisted(dismissed=False, visited=[], skipped=[])

        assert read_persisted().skipped == ()

    def test_the_file_is_written_by_the_owner_only_writer(self, monkeypatch, tmp_path):
        """Atomic and owner-only: the staged write proxy_chains.json uses."""

        from my_claude_code.config import onboarding

        _set_home(monkeypatch, tmp_path)
        calls = []
        real = onboarding.write_owner_only_bytes

        def spy(path, content):
            calls.append((path, content))
            return real(path, content)

        monkeypatch.setattr(onboarding, "write_owner_only_bytes", spy)

        save_persisted(dismissed=True, visited=["guide"], skipped=["proxying"])

        assert calls == [
            (
                onboarding_state_path(),
                b'{"dismissed": true, "visited": ["guide"], "skipped": ["proxying"]}',
            )
        ]
        assert not list(onboarding_state_path().parent.glob("*.fcc-tmp"))

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
    def test_the_file_is_0600_on_posix(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        old = os.umask(0o022)
        try:
            save_persisted(dismissed=False, visited=[], skipped=["proxying"])
        finally:
            os.umask(old)

        assert stat.S_IMODE(onboarding_state_path().stat().st_mode) == 0o600

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows access lists")
    @pytest.mark.spawns_process
    @pytest.mark.local_serial
    def test_the_file_is_owner_only_on_windows(self, monkeypatch, tmp_path):
        """Read back with ``icacls``: this user, SYSTEM and Administrators."""

        _set_home(monkeypatch, tmp_path)
        save_persisted(dismissed=False, visited=[], skipped=["proxying"])
        path = onboarding_state_path()

        completed = subprocess.run(
            ["icacls", str(path)], capture_output=True, text=True, check=True
        )
        lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        lines[0] = lines[0][len(str(path)) :].strip()
        entries = [
            line
            for line in lines
            if ":" in line and not line.startswith("Successfully")
        ]
        assert len(entries) == 3, entries
        assert all(entry.endswith(":(F)") for entry in entries), entries
        assert not any("(I)" in entry for entry in entries), entries
        principals = sorted(
            entry.rsplit(":", 1)[0].split("\\")[-1] for entry in entries
        )
        user = os.environ.get("USERNAME") or os.getlogin()
        assert principals == sorted(["Administrators", "SYSTEM", user]), entries
