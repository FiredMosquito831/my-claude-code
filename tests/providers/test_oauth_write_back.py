"""Write-back: the lock, the monotonicity guard, and everything it preserves.

The point of write-back is that a refresh MCC performs does not log the
operator out of their own Claude Code or Codex session. That is only true if
three things hold, and each has a test here:

* MCC writes **only** ``claudeAiOauth`` / ``tokens`` and leaves the rest of the
  file exactly as found -- this machine's ``.credentials.json`` carries eight
  ``mcpOAuth`` entries beside the one MCC touches;
* MCC takes Claude Code's own ``.storage-write`` lock and **skips** rather than
  forces when it cannot have it;
* MCC never writes an older token over a newer one.

Every file here is a fixture written by this module. No real credential file
is read and no token endpoint is contacted.
"""

import json
import time
from pathlib import Path

import pytest

from my_claude_code.providers.anthropic_oauth import credentials as creds
from my_claude_code.providers.chatgpt_oauth import credentials as chat
from my_claude_code.providers.oauth_account_store import (
    ORIGIN_CLAUDE_CODE,
    ORIGIN_CODEX,
    ORIGIN_MCC,
    OAuthStorageLockUnavailable,
    storage_write_lock,
    storage_write_lock_path,
)

_NOW = int(time.time())


@pytest.fixture(autouse=True)
def _write_back_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(creds, "write_back_enabled", lambda: True)
    monkeypatch.setattr(chat, "chatgpt_write_back_enabled", lambda: True)
    monkeypatch.setattr(
        "my_claude_code.config.credential_names.credential_names_path",
        lambda: tmp_path / "credential_names.json",
    )


def _claude_file(tmp_path: Path, expires_at: int, *, refresh_expiry: int = 0) -> Path:
    """A ``.credentials.json`` shaped like the real one, decoys included."""
    directory = tmp_path / ".claude"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / ".credentials.json"
    target.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "old-access",
                    "refreshToken": "old-refresh",
                    "expiresAt": expires_at * 1000,
                    "refreshTokenExpiresAt": (refresh_expiry or expires_at) * 1000,
                    "scopes": ["user:inference"],
                    "subscriptionType": "max",
                },
                # The decoys. Replacing the document rather than the one key
                # would silently disconnect every one of these.
                "mcpOAuth": {"server-a": {"token": "keep-me"}},
                "somethingThisBuildHasNeverHeardOf": [1, 2, 3],
            }
        ),
        encoding="utf-8",
    )
    return target


def _imported_record(target: Path) -> creds.AccountRecord:
    return creds.AccountRecord(
        id="uuid-a",
        tokens=creds.OAuthTokens(access_token="old-access", refresh_token="old"),
        origin=ORIGIN_CLAUDE_CODE,
        origin_path=str(target),
        write_back=True,
    )


def _refreshed(expires_at: int, *, refresh_expiry: int = 0) -> creds.OAuthTokens:
    return creds.OAuthTokens(
        access_token="new-access",
        refresh_token="new-refresh",
        expires_at=expires_at,
        refresh_token_expires_at=refresh_expiry or expires_at + 86400,
        scopes=("user:inference",),
        subscription_type="max",
        rate_limit_tier="default_claude_max_5x",
    )


def test_a_refreshed_imported_account_is_written_back_preserving_other_keys(
    tmp_path: Path,
) -> None:
    target = _claude_file(tmp_path, _NOW)

    assert creds.write_back_if_owned(_imported_record(target), _refreshed(_NOW + 3600))

    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["claudeAiOauth"]["accessToken"] == "new-access"
    assert document["claudeAiOauth"]["refreshToken"] == "new-refresh"
    assert document["claudeAiOauth"]["expiresAt"] == (_NOW + 3600) * 1000
    assert document["somethingThisBuildHasNeverHeardOf"] == [1, 2, 3]


def test_write_back_preserves_the_mcp_oauth_block_on_the_claude_file(
    tmp_path: Path,
) -> None:
    target = _claude_file(tmp_path, _NOW)

    creds.write_back_if_owned(_imported_record(target), _refreshed(_NOW + 3600))

    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["mcpOAuth"] == {"server-a": {"token": "keep-me"}}


def test_write_back_is_skipped_when_the_source_file_already_holds_a_newer_token(
    tmp_path: Path,
) -> None:
    # The user's real Claude Code refreshed while MCC was mid-flight. Its
    # token is the live one; ours would log them out of their own client.
    target = _claude_file(tmp_path, _NOW + 7200)

    assert not creds.write_back_if_owned(
        _imported_record(target), _refreshed(_NOW + 3600)
    )

    assert (
        json.loads(target.read_text(encoding="utf-8"))["claudeAiOauth"]["accessToken"]
        == "old-access"
    )


def test_write_back_is_skipped_when_the_expiries_are_exactly_equal(
    tmp_path: Path,
) -> None:
    """Greater-than-or-equal, not greater-than: a tie goes to the other client."""
    target = _claude_file(tmp_path, _NOW + 3600, refresh_expiry=_NOW + 999_999)

    assert not creds.write_back_if_owned(
        _imported_record(target),
        _refreshed(_NOW + 3600, refresh_expiry=_NOW + 100),
    )


def test_write_back_breaks_an_expiry_tie_on_the_refresh_token_expiry(
    tmp_path: Path,
) -> None:
    target = _claude_file(tmp_path, _NOW + 3600, refresh_expiry=_NOW + 100)

    assert creds.write_back_if_owned(
        _imported_record(target),
        _refreshed(_NOW + 3600, refresh_expiry=_NOW + 999_999),
    )


def test_write_back_never_touches_a_source_for_an_account_mcc_signed_it_itself(
    tmp_path: Path,
) -> None:
    target = _claude_file(tmp_path, _NOW)
    mcc_own = creds.AccountRecord(
        id="uuid-a",
        tokens=creds.OAuthTokens(access_token="x"),
        origin=ORIGIN_MCC,
        # Even with a path and the flag on: an account MCC signed in itself
        # has no source file to own, and the origin is what says so.
        origin_path=str(target),
        write_back=True,
    )

    assert not creds.write_back_if_owned(mcc_own, _refreshed(_NOW + 3600))
    assert (
        json.loads(target.read_text(encoding="utf-8"))["claudeAiOauth"]["accessToken"]
        == "old-access"
    )


def test_write_back_backs_the_source_file_up_exactly_once(tmp_path: Path) -> None:
    target = _claude_file(tmp_path, _NOW)

    creds.write_back_if_owned(_imported_record(target), _refreshed(_NOW + 3600))
    first = sorted(target.parent.glob(".credentials.json.bak-*"))
    creds.write_back_if_owned(_imported_record(target), _refreshed(_NOW + 7200))

    assert len(first) == 1
    assert sorted(target.parent.glob(".credentials.json.bak-*")) == first
    # The backup holds the token as it was before MCC ever wrote to it.
    backup = json.loads(first[0].read_text(encoding="utf-8"))
    assert backup["claudeAiOauth"]["accessToken"] == "old-access"


def test_write_back_is_off_when_the_setting_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = _claude_file(tmp_path, _NOW)
    monkeypatch.setattr(creds, "write_back_enabled", lambda: False)

    assert not creds.write_back_if_owned(
        _imported_record(target), _refreshed(_NOW + 3600)
    )


def test_write_back_is_off_when_the_accounts_own_flag_says_so(
    tmp_path: Path,
) -> None:
    target = _claude_file(tmp_path, _NOW)
    record = creds.AccountRecord(
        id="uuid-a",
        tokens=creds.OAuthTokens(access_token="x"),
        origin=ORIGIN_CLAUDE_CODE,
        origin_path=str(target),
        write_back=False,
    )

    assert not creds.write_back_if_owned(record, _refreshed(_NOW + 3600))


def test_write_back_is_a_no_op_when_the_source_file_is_absent(
    tmp_path: Path,
) -> None:
    """macOS keychain, or a windows-credman install. No file, no write."""
    missing = tmp_path / ".claude" / ".credentials.json"
    record = creds.AccountRecord(
        id="uuid-a",
        tokens=creds.OAuthTokens(access_token="x"),
        origin=ORIGIN_CLAUDE_CODE,
        origin_path=str(missing),
        write_back=True,
    )

    assert not creds.write_back_if_owned(record, _refreshed(_NOW + 3600))
    assert not missing.exists()


def test_the_written_back_file_is_zero_six_hundred(tmp_path: Path) -> None:
    target = _claude_file(tmp_path, _NOW)

    creds.write_back_if_owned(_imported_record(target), _refreshed(_NOW + 3600))

    # chmod is a no-op on Windows, where the profile directory's ACL governs,
    # so the assertion is that the mode bits are not *wider* than 0600.
    mode = target.stat().st_mode & 0o777
    assert mode & 0o077 == 0 or __import__("os").name == "nt"


def test_write_back_takes_claude_codes_storage_write_lock_before_writing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = _claude_file(tmp_path, _NOW)
    held: list[bool] = []

    original = creds.storage_write_lock

    def spy(directory, **kwargs):
        held.append(directory == target.parent)
        return original(directory, **kwargs)

    monkeypatch.setattr(creds, "storage_write_lock", spy)
    creds.write_back_if_owned(_imported_record(target), _refreshed(_NOW + 3600))

    assert held == [True]


def test_write_back_skips_the_write_when_the_lock_cannot_be_acquired(
    tmp_path: Path,
) -> None:
    target = _claude_file(tmp_path, _NOW)
    # Somebody else is holding it and heartbeating: not stale, not ours.
    storage_write_lock_path(target.parent).mkdir()

    assert not creds.write_back_if_owned(
        _imported_record(target), _refreshed(_NOW + 3600)
    )

    assert (
        json.loads(target.read_text(encoding="utf-8"))["claudeAiOauth"]["accessToken"]
        == "old-access"
    )


def test_write_back_releases_the_lock_even_when_the_write_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = _claude_file(tmp_path, _NOW)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(creds, "_atomic_write_private_json", boom)
    with pytest.raises(OSError):
        creds.write_back_if_owned(_imported_record(target), _refreshed(_NOW + 3600))

    # A lock left behind would block the operator's real client for the whole
    # stale window on every write it tried.
    assert not storage_write_lock_path(target.parent).exists()


def test_the_lock_is_broken_when_it_is_stale_and_kept_when_it_is_not(
    tmp_path: Path,
) -> None:
    directory = tmp_path / ".claude"
    directory.mkdir()
    lock = storage_write_lock_path(directory)
    lock.mkdir()
    import os

    # 60 s old against a 15 s stale window: its owner has stopped
    # heartbeating, and breaking it is its owner's own rule.
    os.utime(lock, (time.time() - 60, time.time() - 60))
    with storage_write_lock(directory, retries=0):
        assert lock.exists()
    assert not lock.exists()

    lock.mkdir()
    with (
        pytest.raises(OAuthStorageLockUnavailable),
        storage_write_lock(directory, retries=1, min_timeout=0.001),
    ):
        pass
    lock.rmdir()


def test_write_back_leaves_the_codex_account_id_unchanged(tmp_path: Path) -> None:
    """Codex guards its own reload on the account id; changing it breaks that."""
    target = tmp_path / "auth.json"
    target.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": "old",
                    "refresh_token": "old-r",
                    "id_token": "old-id",
                    "account_id": "acct-1",
                    "expires_at": _NOW,
                },
                "last_refresh": "2026-09-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    record = chat.ChatGPTAccountRecord(
        id="acct-1",
        tokens={"access_token": "old"},
        origin=ORIGIN_CODEX,
        origin_path=str(target),
        write_back=True,
    )

    assert chat.chatgpt_write_back_if_owned(
        record,
        {
            "access_token": "new",
            "refresh_token": "new-r",
            "id_token": "new-id",
            "expires_at": _NOW + 3600,
        },
    )

    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["tokens"]["account_id"] == "acct-1"
    assert document["tokens"]["access_token"] == "new"
    assert document["auth_mode"] == "chatgpt"
    assert document["last_refresh"] == "2026-09-01T00:00:00Z"


def test_the_codex_write_back_is_skipped_when_the_file_is_newer(
    tmp_path: Path,
) -> None:
    target = tmp_path / "auth.json"
    target.write_text(
        json.dumps({"tokens": {"access_token": "theirs", "expires_at": _NOW + 7200}}),
        encoding="utf-8",
    )
    record = chat.ChatGPTAccountRecord(
        id="acct-1",
        tokens={"access_token": "ours"},
        origin=ORIGIN_CODEX,
        origin_path=str(target),
        write_back=True,
    )

    assert not chat.chatgpt_write_back_if_owned(
        record, {"access_token": "ours", "expires_at": _NOW + 3600}
    )
    assert (
        json.loads(target.read_text(encoding="utf-8"))["tokens"]["access_token"]
        == "theirs"
    )
