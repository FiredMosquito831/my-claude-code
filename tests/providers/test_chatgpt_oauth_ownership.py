"""7.69.1: a ChatGPT credential imported from Codex is Codex's.

Codex has no lock on ``auth.json``, so the protection is a compare-and-swap on
the refresh token MCC posted plus an account-id check (rule 6). Every token
exchange here is faked; the suite-wide token-host block refuses the rest.
"""

import base64
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.providers.chatgpt_oauth import credentials as chat
from my_claude_code.providers.oauth_ownership import last_decision


def _jwt(payload: dict[str, Any]) -> str:
    def part(data: dict[str, Any]) -> str:
        raw = json.dumps(data).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(payload)}.sig"


def _access(account: str, expires_in: float, tag: str) -> str:
    return _jwt(
        {
            "exp": int(time.time() + expires_in),
            "tag": tag,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": account,
                "chatgpt_plan_type": "plus",
            },
        }
    )


def _id_token(account: str) -> str:
    return _jwt(
        {
            "exp": int(time.time() + 3600),
            "email": f"{account}@example.com",
            "https://api.openai.com/auth": {"chatgpt_account_id": account},
        }
    )


class CodexHome:
    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.path = directory / "auth.json"
        self._tick = time.time_ns()

    def write(
        self, account: str, refresh: str, *, expires_in: float, tag: str = "a"
    ) -> str:
        access = _access(account, expires_in, tag)
        document = {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": _id_token(account),
                "access_token": access,
                "refresh_token": refresh,
                "account_id": account,
            },
            "last_refresh": "2026-09-20T05:18:03Z",
        }
        self.path.write_text(json.dumps(document), encoding="utf-8")
        self._tick += 1_000_000_000
        os.utime(self.path, ns=(self._tick, self._tick))
        return access

    def document(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))


class FakeRefresh:
    def __init__(self, account: str, *valid: str) -> None:
        self.account = account
        self.valid = set(valid)
        self.posts: list[str] = []
        self.during_post: Any = None
        self.issued = 0

    def __call__(self, refresh_token: str) -> tuple[str, str, int, str]:
        self.posts.append(refresh_token)
        if self.during_post is not None:
            self.during_post()
        if refresh_token not in self.valid:
            raise chat.ChatGPTOAuthRefreshError(400)
        self.valid.discard(refresh_token)
        self.issued += 1
        new_refresh = f"rt-new-{self.issued}"
        self.valid.add(new_refresh)
        return (
            _access(self.account, 3600, f"new-{self.issued}"),
            new_refresh,
            int(time.time() + 3600),
            _id_token(self.account),
        )


@pytest.fixture
def codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CodexHome:
    directory = tmp_path / "codex-home"
    directory.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(directory))
    monkeypatch.setattr(chat, "chatgpt_write_back_enabled", lambda: True)
    return CodexHome(directory)


@pytest.fixture
def refresh(monkeypatch: pytest.MonkeyPatch) -> FakeRefresh:
    fake = FakeRefresh("acct-1", "rt-old")
    monkeypatch.setattr(chat, "_refresh_access_token", fake)
    return fake


def _request() -> chat.ChatGPTOAuthCredentials:
    return chat.load_chatgpt_oauth_credentials(purpose="request")


def test_an_imported_codex_account_is_shared_and_re_read(
    codex: CodexHome, refresh: FakeRefresh
) -> None:
    first = codex.write("acct-1", "rt-old", expires_in=3600)
    chat.import_codex_cli_tokens()
    record = chat.load_chatgpt_accounts(migrate=False)[0]
    assert record.is_shared
    assert _request().access_token == first

    rotated = codex.write("acct-1", "rt-codex", expires_in=3600, tag="codex")
    assert _request().access_token == rotated
    decision = last_decision("chatgpt_oauth", "acct-1")
    assert decision is not None and decision[0] == "shared:adopted"
    assert refresh.posts == []


def test_importing_codex_never_refreshes(
    codex: CodexHome, refresh: FakeRefresh
) -> None:
    # Inside the 300 s leeway that would refresh a native account.
    access = codex.write("acct-1", "rt-old", expires_in=60)
    imported = chat.import_codex_cli_tokens()
    assert imported.access_token == access
    assert _request().access_token == access
    assert refresh.posts == []

    expired = codex.write("acct-1", "rt-old", expires_in=-60, tag="expired")
    assert chat.import_codex_cli_tokens().access_token == expired
    assert refresh.posts == []


def test_codex_write_back_requires_the_posted_refresh_token(
    codex: CodexHome, refresh: FakeRefresh
) -> None:
    codex.write("acct-1", "rt-other", expires_in=-60)
    chat.import_codex_cli_tokens()
    record = chat.load_chatgpt_accounts(migrate=False)[0]
    bundle = {
        "access_token": _access("acct-1", 3600, "ours"),
        "refresh_token": "rt-ours",
        "expires_at": int(time.time() + 3600),
    }
    assert not chat.chatgpt_write_back_if_owned(
        record, bundle, posted_refresh_token="rt-old"
    )
    assert codex.document()["tokens"]["refresh_token"] == "rt-other"
    assert chat.chatgpt_write_back_if_owned(
        record, bundle, posted_refresh_token="rt-other"
    )
    assert codex.document()["tokens"]["refresh_token"] == "rt-ours"

    # End to end: Codex refreshes inside MCC's POST window. Its token wins.
    codex.write("acct-1", "rt-old", expires_in=-60, tag="old")
    chat.import_codex_cli_tokens()

    def codex_refreshes() -> None:
        codex.write("acct-1", "rt-codex", expires_in=3600, tag="codex")

    refresh.during_post = codex_refreshes
    served = _request()
    assert codex.document()["tokens"]["refresh_token"] == "rt-codex"
    assert served.refresh_token == "rt-codex"
    decision = last_decision("chatgpt_oauth", "acct-1")
    assert decision is not None and decision[0] == "shared:superseded:codex"


def test_codex_write_back_keeps_account_id_and_other_keys(
    codex: CodexHome, refresh: FakeRefresh
) -> None:
    codex.write("acct-1", "rt-old", expires_in=-60)
    chat.import_codex_cli_tokens()

    served = _request()

    document = codex.document()
    assert refresh.posts == ["rt-old"]
    assert served.refresh_token == "rt-new-1"
    assert document["tokens"]["refresh_token"] == "rt-new-1"
    assert document["tokens"]["account_id"] == "acct-1"
    assert document["auth_mode"] == "chatgpt"
    assert document["OPENAI_API_KEY"] is None
    assert document["last_refresh"] == "2026-09-20T05:18:03Z"
    decision = last_decision("chatgpt_oauth", "acct-1")
    assert decision is not None and decision[0] == "shared:refreshed+wrote-back:codex"

    # Background never refreshes; nothing more was posted.
    codex.write("acct-1", "rt-new-1", expires_in=-60, tag="again")
    with pytest.raises(chat.ChatGPTOAuthError):
        chat.load_chatgpt_oauth_credentials(purpose="background")
    assert refresh.posts == ["rt-old"]


def test_a_codex_account_id_mismatch_is_adopted_not_refreshed(
    codex: CodexHome, refresh: FakeRefresh
) -> None:
    codex.write("acct-1", "rt-old", expires_in=3600)
    chat.import_codex_cli_tokens()
    assert _request().account_id == "acct-1"

    codex.write("acct-2", "rt-two", expires_in=-60, tag="switched")
    with pytest.raises(chat.ChatGPTOAuthError):
        chat.load_chatgpt_oauth_credentials(purpose="background")

    ids = [record.id for record in chat.load_chatgpt_accounts(migrate=False)]
    assert ids == ["acct-2"]
    decision = last_decision("chatgpt_oauth", "acct-1")
    assert decision is not None and decision[0] == "shared:identity-changed"
    assert refresh.posts == []


def test_a_chatgpt_sign_in_stays_native(codex: CodexHome, refresh: FakeRefresh) -> None:
    tokens = {
        "access_token": _access("acct-1", -60, "signed-in"),
        "refresh_token": "rt-old",
        "id_token": _id_token("acct-1"),
        "account_id": "acct-1",
    }
    chat.store_managed_chatgpt_oauth_tokens(tokens, adopt_origin=True)
    record = chat.load_chatgpt_accounts(migrate=False)[0]
    assert not record.is_shared

    # A native refresh posts, keeps the account native and writes nothing
    # anywhere but MCC's own store.
    served = chat.force_refresh_managed_chatgpt_oauth_credentials("acct-1")
    assert served.refresh_token == "rt-new-1"
    assert refresh.posts == ["rt-old"]
    assert not chat.load_chatgpt_accounts(migrate=False)[0].is_shared
    assert not codex.path.exists()

    # Import turns it shared; a sign-in turns it native again.
    codex.write("acct-1", "rt-new-1", expires_in=3600)
    chat.import_codex_cli_tokens()
    assert chat.load_chatgpt_accounts(migrate=False)[0].is_shared
    chat.store_managed_chatgpt_oauth_tokens(tokens, adopt_origin=True)
    assert not chat.load_chatgpt_accounts(migrate=False)[0].is_shared
