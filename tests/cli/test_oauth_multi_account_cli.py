"""``--list``, ``--add`` and ``--remove`` on both OAuth login commands.

None of these asks Anthropic or OpenAI anything, so all three must work under
a non-tty -- the same guarantee ``--help`` already had, for the same reason: a
console entry point that needs a terminal to tell you what it holds is one you
cannot use from a script.
"""

from pathlib import Path

import pytest

from my_claude_code.config.credential_names import (
    oauth_credential_id,
    oauth_pool_id,
    set_name,
)
from my_claude_code.providers.anthropic_oauth import credentials as creds
from my_claude_code.providers.anthropic_oauth.cli import (
    anthropic_oauth_login_command,
)
from my_claude_code.providers.chatgpt_oauth import credentials as chat
from my_claude_code.providers.chatgpt_oauth.browser_login import (
    chatgpt_oauth_login_command,
)
from my_claude_code.providers.oauth_account_store import ORIGIN_MCC


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "my_claude_code.config.credential_names.credential_names_path",
        lambda: tmp_path / "credential_names.json",
    )
    monkeypatch.setattr(
        creds, "managed_store_path", lambda: tmp_path / "anthropic_oauth.json"
    )
    creds._REFRESH_LOCKS.clear()


def _seed(count: int = 2) -> list[str]:
    ids = []
    for index in range(count):
        record = creds.add_or_update_account(
            creds.OAuthTokens(
                access_token=f"sk-ant-oat01-secret-{index}",
                refresh_token=f"sk-ant-ort01-secret-{index}",
                expires_at=9_999_999_999,
                account_uuid=f"uuid-{index}",
                subscription_type="max",
            ),
            origin=ORIGIN_MCC,
        )
        ids.append(record.id)
    return ids


def test_list_prints_every_account_and_no_token(capsys) -> None:
    account_ids = _seed(2)
    set_name(
        oauth_pool_id("anthropic_oauth"),
        oauth_credential_id(account_ids[0]),
        "the work one",
    )

    anthropic_oauth_login_command(["--list"])

    output = capsys.readouterr().out
    assert account_ids[0] in output
    assert account_ids[1] in output
    assert "the work one" in output
    assert "max" in output
    # Not one token character, on any row.
    assert "sk-ant-oat01-secret-0" not in output
    assert "sk-ant-ort01-secret-1" not in output


def test_list_works_under_a_non_tty(monkeypatch, capsys) -> None:
    """No prompt, no ``input()``, no consent notice -- it just prints."""
    _seed(1)

    def no_input(*args, **kwargs):
        raise AssertionError("--list must never prompt")

    monkeypatch.setattr("builtins.input", no_input)
    anthropic_oauth_login_command(["--list"])

    assert "uuid-0" in capsys.readouterr().out


def test_list_says_so_when_nothing_is_stored(capsys) -> None:
    anthropic_oauth_login_command(["--list"])

    assert "No Claude subscription accounts are stored." in capsys.readouterr().out


def test_remove_takes_an_account_id(capsys) -> None:
    account_ids = _seed(2)

    anthropic_oauth_login_command(["--remove", account_ids[0]])

    assert [record.id for record in creds.load_accounts()] == [account_ids[1]]
    assert "Disconnected" in capsys.readouterr().out


def test_remove_needs_an_id_and_says_so(capsys) -> None:
    _seed(1)

    with pytest.raises(SystemExit) as exit_info:
        anthropic_oauth_login_command(["--remove"])

    assert exit_info.value.code == 1
    assert "--remove needs an account id" in capsys.readouterr().err


def test_remove_reports_an_unknown_id_rather_than_pretending(capsys) -> None:
    _seed(1)

    with pytest.raises(SystemExit) as exit_info:
        anthropic_oauth_login_command(["--remove", "not-a-real-account"])

    assert exit_info.value.code == 1
    assert len(creds.load_accounts()) == 1


def test_add_is_a_known_option_and_does_not_prompt_to_replace(
    monkeypatch, capsys
) -> None:
    """``--add`` is accepted, and the notice says ADDS rather than replaces."""
    _seed(1)
    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: "no")

    anthropic_oauth_login_command(["--add"])

    output = capsys.readouterr().out
    assert "Continuing ADDS another account." in output
    assert "Continuing will replace it." not in output
    assert "Aborted. Nothing was changed." in output


def test_an_unknown_option_is_still_refused(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        anthropic_oauth_login_command(["--nonsense"])

    assert exit_info.value.code == 1
    assert "Unknown option" in capsys.readouterr().err


def test_the_chatgpt_command_lists_and_removes(monkeypatch, tmp_path, capsys) -> None:
    path = tmp_path / "chatgpt-oauth.json"
    monkeypatch.setattr(chat, "chatgpt_oauth_auth_path", lambda: path)
    chat.add_or_update_chatgpt_account(
        {"access_token": "super-secret", "account_id": "acct-1"}, auth_path=path
    )
    chat.add_or_update_chatgpt_account(
        {"access_token": "also-secret", "account_id": "acct-2"}, auth_path=path
    )

    monkeypatch.setattr("sys.argv", ["mcc-chatgpt-oauth-login", "--list"])
    chatgpt_oauth_login_command()
    output = capsys.readouterr().out
    assert "acct-1" in output and "acct-2" in output
    assert "super-secret" not in output

    monkeypatch.setattr("sys.argv", ["mcc-chatgpt-oauth-login", "--remove", "acct-1"])
    chatgpt_oauth_login_command()

    assert [r.id for r in chat.load_chatgpt_accounts(auth_path=path)] == ["acct-2"]


def test_the_chatgpt_help_works_under_a_non_tty(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.argv", ["mcc-chatgpt-oauth-login", "--help"])

    chatgpt_oauth_login_command()

    assert "--remove <account id>" in capsys.readouterr().out
