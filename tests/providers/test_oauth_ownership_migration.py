"""7.69.1 rule 14: the one-time migration, on fixtures only.

MCC writes its store's primary token into Claude Code's file only over an
expired predecessor, for the same account, with no Claude Code write since,
under all three locks -- and otherwise writes nothing. On the developer's
machine (no live store, four ``.dead-*`` copies) it is a no-op; the last test
here rebuilds that exact layout with synthetic files and proves it.
"""

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.providers.anthropic_oauth import credentials as creds
from my_claude_code.providers.anthropic_oauth import shared
from my_claude_code.providers.anthropic_oauth.auth import AnthropicOAuthAuth
from my_claude_code.providers.anthropic_oauth.credentials import OAuthTokens
from my_claude_code.providers.oauth_account_store import ORIGIN_MCC
from my_claude_code.providers.oauth_ownership import migration_marker


@pytest.fixture
def claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "claude-config"
    directory.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(directory))
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(creds, "write_back_enabled", lambda: True)
    monkeypatch.setattr(shared, "_platform", lambda: "win32")
    shared.reset_migration_flag()
    return directory


def _write_claude(directory: Path, *, expires_in: float, mtime: float) -> Path:
    path = directory / ".credentials.json"
    document = {
        "claudeAiOauth": {
            "accessToken": "at-cc-old",
            "refreshToken": "rt-cc-old",
            "expiresAt": int((time.time() + expires_in) * 1000),
            "scopes": ["user:inference"],
            "subscriptionType": "max",
        },
        "mcpOAuth": {"server": {"k": 1}},
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _identity(directory: Path, uuid: str) -> None:
    (directory / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": uuid}}), encoding="utf-8"
    )


def _store_mcc(uuid: str, *, expires_in: float, mtime: float) -> None:
    creds.add_or_update_account(
        OAuthTokens(
            access_token="at-mcc",
            refresh_token="rt-mcc",
            expires_at=int(time.time() + expires_in),
            subscription_type="max",
            account_uuid=uuid,
            source="mcc",
        ),
        origin=ORIGIN_MCC,
        adopt_origin=True,
    )
    store = creds.managed_store_path()
    os.utime(store, (mtime, mtime))


def _migrate() -> str:
    return asyncio.run(shared.migrate_once())


def test_migration_writes_back_once_over_claude_codes_expired_predecessor(
    claude: Path,
) -> None:
    now = time.time()
    target = _write_claude(claude, expires_in=-600, mtime=now - 3600)
    _identity(claude, "uuid-a")
    _store_mcc("uuid-a", expires_in=3600, mtime=now - 60)

    assert _migrate() == "migration:wrote-back"

    block = json.loads(target.read_text(encoding="utf-8"))
    assert block["claudeAiOauth"]["accessToken"] == "at-mcc"
    assert block["claudeAiOauth"]["refreshToken"] == "rt-mcc"
    assert block["mcpOAuth"] == {"server": {"k": 1}}
    assert len(list(claude.glob(".credentials.json.bak-*"))) == 1
    record = creds.load_accounts(migrate=False)[0]
    assert record.is_shared  # the record becomes SHARED
    marker = migration_marker("anthropic_oauth")
    assert marker is not None and marker["code"] == "migration:wrote-back"
    assert not shared.refresh_lock_path().exists()
    assert not shared.legacy_lock_path().exists()


def test_migration_never_crosses_identities(claude: Path) -> None:
    now = time.time()
    target = _write_claude(claude, expires_in=-600, mtime=now - 3600)
    before = target.read_bytes()
    _identity(claude, "uuid-b")
    _store_mcc("uuid-a", expires_in=3600, mtime=now - 60)

    assert _migrate() == "migration:skipped:other-identity"
    assert target.read_bytes() == before
    assert not creds.load_accounts(migrate=False)[0].is_shared


def test_migration_never_writes_when_claude_codes_file_is_newer(claude: Path) -> None:
    now = time.time()
    target = _write_claude(claude, expires_in=-600, mtime=now - 10)
    before = target.read_bytes()
    _identity(claude, "uuid-a")
    _store_mcc("uuid-a", expires_in=3600, mtime=now - 3600)

    assert _migrate() == "migration:skipped:file-newer"
    assert target.read_bytes() == before

    # A file Claude Code can still use is never touched either.
    shared.reset_migration_flag()
    creds.managed_store_path().parent.joinpath("oauth_ownership.json").unlink()
    target = _write_claude(claude, expires_in=3600, mtime=now - 7200)
    assert _migrate() == "migration:skipped:file-valid"


def test_migration_never_reads_dead_stores(
    claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This machine's layout, rebuilt with synthetic files: no live store,
    # four quarantined copies. Their contents are never opened.
    store = creds.managed_store_path()
    store.parent.mkdir(parents=True, exist_ok=True)
    for stamp in (1788530361, 1788555949, 1788797863, 1790455981):
        dead = store.with_name(f"{store.name}.dead-{stamp}")
        dead.write_text(
            json.dumps({"accessToken": "synthetic", "refreshToken": "synthetic"}),
            encoding="utf-8",
        )
    auth_dir = store.parent / "auth"
    auth_dir.mkdir(exist_ok=True)
    (auth_dir / "chatgpt-oauth.json").write_text("{}", encoding="utf-8")
    now = time.time()
    target = _write_claude(claude, expires_in=-600, mtime=now - 3600)
    before = target.read_bytes()
    _identity(claude, "uuid-a")

    opened: list[str] = []
    real_open = Path.open

    def spy(self: Path, *args: Any, **kwargs: Any) -> Any:
        opened.append(self.name)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy)
    code = _migrate()
    monkeypatch.setattr(Path, "open", real_open)

    assert code == "migration:skipped:no-store"
    assert not [name for name in opened if ".dead-" in name]
    assert target.read_bytes() == before
    assert not store.exists()


def test_migration_runs_once(claude: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real = shared._migrate

    async def counted() -> str:
        calls.append(1)
        return await real()

    monkeypatch.setattr(shared, "_migrate", counted)
    now = time.time()
    _write_claude(claude, expires_in=3600, mtime=now)

    auth = AnthropicOAuthAuth()
    for _ in range(3):
        asyncio.run(auth.current_tokens(purpose="background"))
    assert calls == [1]
    marker = migration_marker("anthropic_oauth")
    assert marker is not None and marker["code"] == "migration:skipped:no-store"

    # A second process after the upgrade: the marker, not the flag, stops it.
    shared.reset_migration_flag()
    assert _migrate() == "migration:already-ran"
    assert calls == [1]
