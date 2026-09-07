"""The two undo modes back-ported to Configure Claude Code.

Until 6.55.0 Configure overwrote ``ANTHROPIC_BASE_URL`` without recording what
had been there, and Undo deleted it. A user whose variable pointed at another
gateway lost it on Configure and did not get it back on Undo -- the gap the
desktop-app work made unignorable, because the user named this file as the
behaviour the desktop cards should match.
"""

import json

import pytest

from my_claude_code.config.claude_settings import (
    CLAUDE_SETTINGS_BACKUP_SUFFIX,
    ClaudeSettingsError,
    apply_proxy_env,
    clear_proxy_env,
)
from my_claude_code.config.restore_record import UndoMode

BASE_URL = "http://127.0.0.1:8082"
TOKEN = "mcc-token"


def write(path, document) -> None:
    path.write_text(json.dumps(document, indent=2), encoding="utf-8", newline="")


def env_of(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["env"]


def test_restore_puts_back_a_gateway_the_user_had_configured(tmp_path):
    """The case that used to destroy the user's setting silently."""

    path, record = tmp_path / "settings.json", tmp_path / "record.json"
    write(
        path,
        {
            "env": {
                "ANTHROPIC_BASE_URL": "https://gateway.example",
                "ANTHROPIC_AUTH_TOKEN": "their-own-token",
                "OTHER": "untouched",
            },
            "hooks": {"PreToolUse": []},
        },
    )
    original = path.read_bytes()

    apply_proxy_env(path=path, base_url=BASE_URL, auth_token=TOKEN, record_path=record)
    assert env_of(path)["ANTHROPIC_BASE_URL"] == BASE_URL

    clear_proxy_env(path=path, mode=UndoMode.RESTORE, record_path=record)
    restored = json.loads(path.read_text(encoding="utf-8"))
    assert restored["env"]["ANTHROPIC_BASE_URL"] == "https://gateway.example"
    assert restored["env"]["ANTHROPIC_AUTH_TOKEN"] == "their-own-token"
    assert restored["env"]["OTHER"] == "untouched"
    assert restored["hooks"] == {"PreToolUse": []}
    assert json.loads(original) == restored


def test_keys_only_removes_mccs_variables_and_restores_nothing(tmp_path):
    """The default mode, and the one that cannot surprise anyone."""

    path, record = tmp_path / "settings.json", tmp_path / "record.json"
    write(path, {"env": {"ANTHROPIC_BASE_URL": "https://gateway.example", "K": "v"}})

    apply_proxy_env(path=path, base_url=BASE_URL, auth_token=TOKEN, record_path=record)
    clear_proxy_env(path=path, mode=UndoMode.KEYS_ONLY, record_path=record)

    assert env_of(path) == {"K": "v"}


def test_restore_deletes_a_variable_that_did_not_exist_before(tmp_path):
    """A key MCC *created* is removed, not resurrected as null."""

    path, record = tmp_path / "settings.json", tmp_path / "record.json"
    write(path, {"env": {"K": "v"}})

    apply_proxy_env(path=path, base_url=BASE_URL, auth_token=TOKEN, record_path=record)
    clear_proxy_env(path=path, mode=UndoMode.RESTORE, record_path=record)

    assert env_of(path) == {"K": "v"}


def test_restore_refuses_when_the_file_changed_since_configure(tmp_path):
    """Restoring into a rewritten file would revert an edit made on purpose."""

    path, record = tmp_path / "settings.json", tmp_path / "record.json"
    write(path, {"env": {"ANTHROPIC_BASE_URL": "https://gateway.example"}})

    apply_proxy_env(path=path, base_url=BASE_URL, auth_token=TOKEN, record_path=record)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["permissions"] = {"allow": ["Bash(ls)"]}
    write(path, document)

    with pytest.raises(ClaudeSettingsError, match="changed since"):
        clear_proxy_env(path=path, mode=UndoMode.RESTORE, record_path=record)

    # And it names the backup, so the user is not left without a route back.
    assert CLAUDE_SETTINGS_BACKUP_SUFFIX


def test_restore_without_a_record_says_so_rather_than_guessing(tmp_path):
    path, record = tmp_path / "settings.json", tmp_path / "record.json"
    write(path, {"env": {"ANTHROPIC_BASE_URL": BASE_URL}})

    with pytest.raises(ClaudeSettingsError, match="no record"):
        clear_proxy_env(path=path, mode=UndoMode.RESTORE, record_path=record)


def test_configure_still_defaults_to_the_old_undo_behaviour(tmp_path):
    """No caller that did not ask for the new mode gets it."""

    path, record = tmp_path / "settings.json", tmp_path / "record.json"
    write(path, {"env": {"ANTHROPIC_BASE_URL": "https://gateway.example"}})

    apply_proxy_env(path=path, base_url=BASE_URL, auth_token=TOKEN, record_path=record)
    clear_proxy_env(path=path, record_path=record)

    assert "env" not in json.loads(path.read_text(encoding="utf-8"))
