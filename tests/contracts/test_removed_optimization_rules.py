"""Guard: the three zero-fire local optimization rules stay deleted.

`quota_mock`, `prefix_detection` and `filepath_extraction_mock` never matched a
single request across 153,198 logged production requests -- Claude Code stopped
sending those shapes. They were removed in full. A partial revert (the handler
back but not the setting, or the env key back but not the field) is the failure
mode this file exists to catch, so every surface a setting has is asserted here
rather than only the one that happens to be convenient.
"""

from pathlib import Path

from my_claude_code.api import detection, optimization_handlers
from my_claude_code.config.admin.manifest import FIELDS
from my_claude_code.config.settings import Settings

REMOVED_ENV_KEYS = (
    "FAST_PREFIX_DETECTION",
    "ENABLE_NETWORK_PROBE_MOCK",
    "ENABLE_FILEPATH_EXTRACTION_MOCK",
)
REMOVED_SETTINGS_ATTRS = (
    "fast_prefix_detection",
    "enable_network_probe_mock",
    "enable_filepath_extraction_mock",
)
REMOVED_RULES = ("quota_mock", "prefix_detection", "filepath_extraction_mock")
REMOVED_CALLABLES = (
    ("try_quota_mock", optimization_handlers),
    ("try_prefix_detection", optimization_handlers),
    ("try_filepath_mock", optimization_handlers),
    ("is_quota_check_request", detection),
    ("is_prefix_detection_request", detection),
    ("is_filepath_extraction_request", detection),
)

SURVIVING_RULES = (
    "title_generation_skip",
    "suggestion_mode_skip",
    "probe_auto_response",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_removed_settings_fields_are_gone() -> None:
    for attr in REMOVED_SETTINGS_ATTRS:
        assert attr not in Settings.model_fields, f"{attr} reappeared on Settings"


def test_removed_env_keys_are_gone_from_the_admin_manifest() -> None:
    keys = {field.key for field in FIELDS}
    attrs = {field.settings_attr for field in FIELDS}
    for key in REMOVED_ENV_KEYS:
        assert key not in keys, f"{key} reappeared in the admin manifest"
    for attr in REMOVED_SETTINGS_ATTRS:
        assert attr not in attrs, f"{attr} reappeared in the admin manifest"


def test_removed_env_keys_are_gone_from_env_example() -> None:
    text = (_repo_root() / ".env.example").read_text(encoding="utf-8")
    for key in REMOVED_ENV_KEYS:
        assert key not in text, f"{key} reappeared in .env.example"


def test_removed_handlers_and_detectors_are_gone() -> None:
    for name, module in REMOVED_CALLABLES:
        assert not hasattr(module, name), f"{name} reappeared in {module.__name__}"


def test_removed_rule_names_are_not_published() -> None:
    for rule in REMOVED_RULES:
        assert rule not in optimization_handlers.OPTIMIZATION_RULES


def test_command_utils_module_is_gone() -> None:
    assert not (
        _repo_root() / "src" / "my_claude_code" / "api" / "command_utils.py"
    ).exists()
    assert not (
        _repo_root() / "src" / "free_claude_code" / "api" / "command_utils.py"
    ).exists()


def test_the_two_surviving_rules_are_still_registered() -> None:
    assert optimization_handlers.OPTIMIZATION_RULES == SURVIVING_RULES
    assert len(optimization_handlers.OPTIMIZATION_HANDLERS) == len(SURVIVING_RULES)
    assert hasattr(detection, "is_title_generation_request")
    assert hasattr(detection, "is_suggestion_mode_request")
    assert hasattr(detection, "is_safety_classifier_request")
