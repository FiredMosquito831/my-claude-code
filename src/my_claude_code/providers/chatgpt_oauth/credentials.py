"""FCC-owned ChatGPT/Codex OAuth credential loading and refresh."""

import base64
import dataclasses
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from my_claude_code.config.constants import (
    CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
)
from my_claude_code.config.paths import chatgpt_oauth_auth_path

CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_OAUTH_ORIGINATOR = "codex_cli_rs"
CODEX_OAUTH_SCOPE = (
    "openid profile email offline_access api.connectors.read api.connectors.invoke"
)
MANAGED_CREDENTIAL_SCHEMA_VERSION = 1
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


class ChatGPTOAuthError(Exception):
    """Raised when ChatGPT OAuth credential handling fails."""


#: Statuses that mean the *credential* is finished, rather than that the token
#: endpoint could not answer right now. Everything else -- 408, 429, 5xx, a
#: transport error -- is transient and the credential is kept.
#:
#: Named rather than inlined so that
#: ``tests/providers/test_oauth_refresh_parity.py`` can pin it equal to the
#: Anthropic provider's. The two implementations drifted apart once already:
#: this one classified correctly while ``anthropic_oauth`` treated every
#: failure, 429 included, as a dead credential, and told operators to sign in
#: again -- which rotates a working refresh token away.
DEFINITIVE_REFRESH_STATUSES: frozenset[int] = frozenset({400, 401, 403})


class ChatGPTOAuthRefreshError(ChatGPTOAuthError):
    """Raised when OpenAI rejects or cannot complete a token refresh."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"OAuth refresh failed with HTTP {status_code}")


@dataclasses.dataclass(frozen=True)
class ChatGPTOAuthCredentials:
    """Resolved OAuth credentials for one request."""

    access_token: str
    account_id: str
    refresh_token: str | None = None
    expires_at: int | None = None
    source_name: str = ""


@dataclasses.dataclass(frozen=True)
class _TokenSource:
    name: str
    path: Path
    access_token: str | None
    refresh_token: str | None
    id_token: str | None = None
    account_id: str | None = None
    expires_at: int | None = None

    @property
    def has_access_token(self) -> bool:
        return isinstance(self.access_token, str) and self.access_token.strip() != ""

    @property
    def has_refresh_token(self) -> bool:
        return isinstance(self.refresh_token, str) and self.refresh_token.strip() != ""


def _home() -> Path:
    return Path.home()


def _codex_home() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", "")).expanduser()
    if not str(codex_home).strip() or str(codex_home) == ".":
        codex_home = _home() / ".codex"
    return codex_home


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ChatGPTOAuthError(f"Could not parse {path}: {exc}") from exc
    except OSError as exc:
        raise ChatGPTOAuthError(f"Could not read {path}: {exc}") from exc


def _load_codex_cli_source() -> _TokenSource:
    path = _codex_home() / "auth.json"
    payload = _load_json(path)
    tokens = payload.get("tokens") or {}
    return _TokenSource(
        name="codex-cli",
        path=path,
        access_token=tokens.get("access_token"),
        refresh_token=tokens.get("refresh_token"),
        id_token=tokens.get("id_token"),
        account_id=tokens.get("account_id"),
        expires_at=tokens.get("expires_at"),
    )


def _load_managed_source(path: Path | None = None) -> _TokenSource:
    path = path or chatgpt_oauth_auth_path()
    payload = _load_json(path)
    if payload and payload.get("version") != MANAGED_CREDENTIAL_SCHEMA_VERSION:
        raise ChatGPTOAuthError(
            f"Unsupported FCC ChatGPT OAuth credential schema at {path}."
        )
    tokens = payload.get("tokens") or {}
    return _TokenSource(
        name="fcc-managed",
        path=path,
        access_token=tokens.get("access_token"),
        refresh_token=tokens.get("refresh_token"),
        id_token=tokens.get("id_token"),
        account_id=tokens.get("account_id"),
        expires_at=tokens.get("expires_at"),
    )


def _reload_source(source: _TokenSource) -> _TokenSource:
    """Re-read one token source from disk (e.g. after another thread refreshed)."""
    if source.name == "fcc-managed":
        return _load_managed_source(source.path)
    return source


def _load_sources() -> list[_TokenSource]:
    return [_load_managed_source()]


def _decode_jwt_claims(token: str | None) -> dict[str, Any]:
    if not token or token.count(".") < 2:
        return {}
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")))
    except Exception:
        return {}


def _extract_account_id_from_claims(claims: dict[str, Any]) -> str:
    """Extract the ChatGPT account id from decoded JWT claims.

    Mirrors OpenCode's extraction order: top-level ``chatgpt_account_id``,
    then the namespaced auth claim, then a generic ``account_id``, then the
    first organization id.
    """
    account_id = claims.get("chatgpt_account_id")
    if isinstance(account_id, str) and account_id:
        return account_id
    auth_claim = claims.get("https://api.openai.com/auth") or {}
    account_id = auth_claim.get("chatgpt_account_id")
    if isinstance(account_id, str) and account_id:
        return account_id
    account_id = claims.get("account_id")
    if isinstance(account_id, str) and account_id:
        return account_id
    organizations = claims.get("organizations")
    if isinstance(organizations, list) and organizations:
        first = organizations[0]
        if isinstance(first, dict):
            org_id = first.get("id")
            if isinstance(org_id, str) and org_id:
                return org_id
    return ""


def _extract_account_id(access_token: str) -> str:
    return _extract_account_id_from_claims(_decode_jwt_claims(access_token))


def extract_account_id_from_tokens(
    access_token: str | None = None,
    id_token: str | None = None,
) -> str:
    """Extract the account id, preferring the id token like OpenCode does."""
    if id_token:
        account_id = _extract_account_id_from_claims(_decode_jwt_claims(id_token))
        if account_id:
            return account_id
    if access_token:
        return _extract_account_id_from_claims(_decode_jwt_claims(access_token))
    return ""


def _access_token_seconds_remaining(access_token: str) -> int | None:
    claims = _decode_jwt_claims(access_token)
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    return int(exp - time.time())


def _token_expiry(tokens: dict[str, Any]) -> int | None:
    expires_at = tokens.get("expires_at")
    if isinstance(expires_at, (int, float)):
        return int(expires_at)
    expires_in = tokens.get("expires_in")
    if isinstance(expires_in, (int, float)):
        return int(time.time() + expires_in)
    access_token = tokens.get("access_token")
    claims = _decode_jwt_claims(access_token if isinstance(access_token, str) else None)
    exp = claims.get("exp")
    return int(exp) if isinstance(exp, (int, float)) else None


def _atomic_write_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    if os.name != "nt":
        os.chmod(path.parent, PRIVATE_DIR_MODE)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(temporary, PRIVATE_FILE_MODE)
        os.replace(temporary, path)
        if os.name != "nt":
            os.chmod(path, PRIVATE_FILE_MODE)
    finally:
        temporary.unlink(missing_ok=True)


def store_managed_chatgpt_oauth_tokens(
    tokens: dict[str, Any],
    *,
    auth_path: Path | None = None,
) -> Path:
    """Validate and atomically persist FCC-owned renewable OAuth credentials."""

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    id_token = tokens.get("id_token")
    if not all(
        isinstance(value, str) and value
        for value in (access_token, refresh_token, id_token)
    ):
        raise ChatGPTOAuthError(
            "OpenAI OAuth response did not contain renewable credentials."
        )
    account_id = tokens.get("account_id")
    if not isinstance(account_id, str) or not account_id:
        account_id = extract_account_id_from_tokens(
            access_token=access_token,
            id_token=id_token,
        )
    if not account_id:
        raise ChatGPTOAuthError(
            "OpenAI OAuth response did not contain a ChatGPT account identifier."
        )
    path = auth_path or chatgpt_oauth_auth_path()
    _atomic_write_private_json(
        path,
        {
            "version": MANAGED_CREDENTIAL_SCHEMA_VERSION,
            "tokens": {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "id_token": id_token,
                "account_id": account_id,
                "expires_at": _token_expiry(tokens),
            },
        },
    )
    return path


def _refresh_access_token(
    refresh_token: str,
) -> tuple[str, str | None, int | None, str | None]:
    """Refresh an OAuth access token and return the new credential set."""
    response = httpx.post(
        CODEX_OAUTH_TOKEN_URL,
        json={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CODEX_OAUTH_CLIENT_ID,
        },
        headers={"originator": CODEX_OAUTH_ORIGINATOR},
        timeout=httpx.Timeout(30.0),
    )
    if response.status_code != 200:
        raise ChatGPTOAuthRefreshError(response.status_code)
    payload = response.json()
    new_access = payload.get("access_token")
    new_refresh = payload.get("refresh_token") or refresh_token
    expires_in = payload.get("expires_in")
    if not isinstance(new_access, str) or not new_access:
        raise ChatGPTOAuthError(
            "OAuth refresh response did not contain an access token."
        )
    expires_at = None
    if isinstance(expires_in, (int, float)):
        expires_at = int(time.time() + expires_in)
    new_id_token = payload.get("id_token")
    if not isinstance(new_id_token, str):
        new_id_token = None
    return new_access, new_refresh, expires_at, new_id_token


def _persist_refreshed_tokens(
    source: _TokenSource,
    *,
    access_token: str,
    refresh_token: str | None,
    id_token: str | None,
    expires_at: int | None,
) -> None:
    """Write refreshed tokens back to FCC's private credential store."""
    if source.name != "fcc-managed":
        raise ChatGPTOAuthError(
            "Refusing to write refreshed credentials outside FCC's auth store."
        )
    store_managed_chatgpt_oauth_tokens(
        {
            "access_token": access_token,
            "refresh_token": refresh_token or source.refresh_token,
            "id_token": id_token or source.id_token,
            "account_id": source.account_id,
            "expires_at": expires_at,
        },
        auth_path=source.path,
    )


_REFRESH_LOCK = threading.Lock()


def _ensure_fresh_source(source: _TokenSource) -> _TokenSource:
    remaining = (
        _access_token_seconds_remaining(source.access_token)
        if source.access_token
        else None
    )
    if remaining is None or remaining > 300:
        return source
    if not source.has_refresh_token or source.refresh_token is None:
        # Token is expiring and we cannot refresh; return as-is and let the
        # upstream request fail with a clear 401 if expired.
        return source

    with _REFRESH_LOCK:
        # Another thread may have refreshed while we waited on the lock.
        current = _reload_source(source)
        current_access_token = current.access_token
        if current.has_access_token and current_access_token is not None:
            remaining = _access_token_seconds_remaining(current_access_token)
            if remaining is not None and remaining > 300:
                return current
        if not current.has_refresh_token or current.refresh_token is None:
            return source

        new_access, new_refresh, expires_at, new_id_token = _refresh_access_token(
            current.refresh_token
        )
        _persist_refreshed_tokens(
            current,
            access_token=new_access,
            refresh_token=new_refresh,
            id_token=new_id_token,
            expires_at=expires_at,
        )
        return dataclasses.replace(
            current,
            access_token=new_access,
            refresh_token=new_refresh,
            id_token=new_id_token or current.id_token,
            account_id=(
                extract_account_id_from_tokens(
                    access_token=new_access,
                    id_token=new_id_token or current.id_token,
                )
                or current.account_id
            ),
            expires_at=expires_at,
        )


def _choose_runtime_source(sources: list[_TokenSource]) -> _TokenSource:
    refresh_errors: list[str] = []
    for item in sources:
        if item.has_access_token:
            try:
                return _ensure_fresh_source(item)
            except ChatGPTOAuthError as exc:
                refresh_errors.append(f"{item.name}: {exc}")
    suffix = f" Refresh failures: {'; '.join(refresh_errors)}" if refresh_errors else ""
    raise ChatGPTOAuthError(
        "No usable MCC-managed ChatGPT OAuth credentials found. "
        f"Sign in or import Codex credentials in Admin.{suffix}"
    )


def load_chatgpt_oauth_credentials(
    *,
    access_token: str | None = None,
    account_id: str | None = None,
) -> ChatGPTOAuthCredentials:
    """Resolve OAuth credentials from explicit values or auth files.

    Priority:
      1. Explicit access_token / account_id.
      2. FCC's private renewable credential store.
    """
    normalized_access_token = (access_token or "").strip()
    if (
        normalized_access_token
        and normalized_access_token != CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE
    ):
        resolved_account_id = (account_id or "").strip() or _extract_account_id(
            normalized_access_token
        )
        return ChatGPTOAuthCredentials(
            access_token=normalized_access_token,
            account_id=resolved_account_id,
        )

    source = _choose_runtime_source(_load_sources())
    resolved_account_id = (
        (account_id or "").strip()
        or (source.account_id or "").strip()
        or extract_account_id_from_tokens(
            access_token=source.access_token,
            id_token=source.id_token,
        )
    )
    return ChatGPTOAuthCredentials(
        access_token=source.access_token or "",
        account_id=resolved_account_id,
        refresh_token=source.refresh_token,
        expires_at=source.expires_at,
        source_name=source.name,
    )


def force_refresh_managed_chatgpt_oauth_credentials() -> ChatGPTOAuthCredentials:
    """Refresh FCC-owned credentials after an upstream unauthorized response."""

    with _REFRESH_LOCK:
        source = _load_managed_source()
        if not source.has_refresh_token or source.refresh_token is None:
            raise ChatGPTOAuthError(
                "MCC ChatGPT OAuth credentials cannot be refreshed. Reconnect in Admin."
            )
        try:
            access, refresh, expires_at, id_token = _refresh_access_token(
                source.refresh_token
            )
        except ChatGPTOAuthRefreshError as exc:
            if exc.status_code in DEFINITIVE_REFRESH_STATUSES:
                source.path.unlink(missing_ok=True)
                raise ChatGPTOAuthError(
                    "ChatGPT OAuth session expired. Reconnect in Admin."
                ) from exc
            raise
        resolved_id_token = id_token or source.id_token
        _persist_refreshed_tokens(
            source,
            access_token=access,
            refresh_token=refresh,
            id_token=resolved_id_token,
            expires_at=expires_at,
        )
        account_id = (
            extract_account_id_from_tokens(
                access_token=access,
                id_token=resolved_id_token,
            )
            or source.account_id
            or ""
        )
        return ChatGPTOAuthCredentials(
            access_token=access,
            account_id=account_id,
            refresh_token=refresh,
            expires_at=expires_at,
            source_name="fcc-managed",
        )


def import_codex_cli_tokens() -> ChatGPTOAuthCredentials:
    """Copy renewable Codex CLI tokens into FCC's private credential store.

    Raises ChatGPTOAuthError when the auth file is missing, malformed, or does
    not contain a complete renewable credential bundle. Codex's file is never
    modified.
    """
    source = _load_codex_cli_source()
    if (
        not source.has_access_token
        or not source.has_refresh_token
        or not isinstance(source.id_token, str)
        or not source.id_token
    ):
        path = source.path
        raise ChatGPTOAuthError(
            f"No renewable Codex CLI OAuth credentials found at {path}. "
            "Run 'codex login' first or use the ChatGPT OAuth Login button."
        )
    store_managed_chatgpt_oauth_tokens(
        {
            "access_token": source.access_token,
            "refresh_token": source.refresh_token,
            "id_token": source.id_token,
            "account_id": source.account_id,
            "expires_at": source.expires_at,
        }
    )
    managed = _ensure_fresh_source(_load_managed_source())
    return ChatGPTOAuthCredentials(
        access_token=managed.access_token or "",
        account_id=(managed.account_id or "")
        or extract_account_id_from_tokens(
            access_token=managed.access_token,
            id_token=managed.id_token,
        ),
        refresh_token=managed.refresh_token,
        expires_at=managed.expires_at,
        source_name=managed.name,
    )
