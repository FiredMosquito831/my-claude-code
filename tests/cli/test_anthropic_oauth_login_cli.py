"""``mcc-anthropic-oauth-login`` must never show an operator a traceback.

The reported symptom was ``mcc-anthropic-oauth-login --help`` printing the
legal warning and then dying with ``EOFError: EOF when reading a line`` -- the
command had no ``--help`` at all, called bare ``input()``, and wrapped none of
it. A rejected code and a Ctrl-C failed the same way.
"""

from pathlib import Path

import pytest

from my_claude_code.providers.anthropic_oauth import cli as oauth_cli
from my_claude_code.providers.anthropic_oauth.oauth_login import (
    AnthropicOAuthLoginError,
)


@pytest.fixture(autouse=True)
def _scratch_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No test may read or write the real credential stores."""
    managed = tmp_path / "anthropic_oauth.json"
    claude = tmp_path / ".credentials.json"
    monkeypatch.setattr(oauth_cli, "managed_store_path", lambda: managed)
    monkeypatch.setattr(oauth_cli, "claude_credentials_path", lambda: claude)
    monkeypatch.setattr(
        oauth_cli,
        "detect_available_sources",
        lambda: {"mcc": False, "claude_code": False},
    )


def _no_input(prompt: str = "") -> str:
    raise AssertionError(f"the command prompted when it must not: {prompt!r}")


def test_cli_login_help_exits_zero_without_prompting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("builtins.input", _no_input)

    # No SystemExit at all: a plain return is exit status 0 for a console script.
    oauth_cli.anthropic_oauth_login_command(["--help"])

    out = capsys.readouterr().out
    assert "Usage:" in out
    assert "--paste" in out
    # The legal warning is a consent notice for someone about to sign in, not
    # something to shout at anyone who asks how the command works.
    assert "READ THIS" not in out


def test_cli_login_help_works_for_the_short_flag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("builtins.input", _no_input)
    oauth_cli.anthropic_oauth_login_command(["-h"])
    assert "Usage:" in capsys.readouterr().out


def test_cli_login_reports_a_message_not_a_traceback_on_eof(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def eof(prompt: str = "") -> str:
        raise EOFError("EOF when reading a line")

    monkeypatch.setattr("builtins.input", eof)

    with pytest.raises(SystemExit) as excinfo:
        oauth_cli.anthropic_oauth_login_command([])

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "EOFError" not in captured.err
    assert "stdin is closed" in captured.err
    assert "Nothing was changed" in captured.err


def test_cli_login_reports_a_ctrl_c_as_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupt(prompt: str = "") -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupt)

    with pytest.raises(SystemExit) as excinfo:
        oauth_cli.anthropic_oauth_login_command([])

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "Cancelled" in err


def test_cli_login_reports_a_rejected_code_as_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = iter(["yes", "definitely-not-a-valid-code"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(oauth_cli, "loopback_unavailable_reason", lambda: None)

    def reject(*args: object, **kwargs: object) -> None:
        raise AnthropicOAuthLoginError(400, "the pasted code was rejected")

    monkeypatch.setattr(oauth_cli, "exchange_code", reject)

    with pytest.raises(SystemExit) as excinfo:
        oauth_cli.anthropic_oauth_login_command(["--paste", "--no-browser"])

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "Sign-in failed" in err
    assert "the pasted code was rejected" in err


def test_cli_login_declining_consent_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")

    oauth_cli.anthropic_oauth_login_command([])

    assert "Aborted" in capsys.readouterr().out


def test_cli_login_rejects_an_unknown_option_without_prompting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("builtins.input", _no_input)

    with pytest.raises(SystemExit) as excinfo:
        oauth_cli.anthropic_oauth_login_command(["--nonsense"])

    assert excinfo.value.code == 1
    assert "Unknown option" in capsys.readouterr().err


def test_cli_login_falls_back_to_pasting_when_loopback_cannot_work(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Under WSL the browser's localhost is not this process's localhost."""
    answers = iter(["yes", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(
        oauth_cli, "loopback_unavailable_reason", lambda: "MCC is running under WSL"
    )

    def never(*args: object, **kwargs: object) -> None:
        raise AssertionError("the loopback flow must not start here")

    monkeypatch.setattr(oauth_cli, "start_loopback_login", never)

    oauth_cli.anthropic_oauth_login_command(["--no-browser"])

    out = capsys.readouterr().out
    assert "Using the paste flow" in out
    assert "No code entered" in out
