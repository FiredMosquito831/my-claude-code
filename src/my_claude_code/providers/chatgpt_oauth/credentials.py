"""FCC-owned ChatGPT/Codex OAuth credential loading and refresh."""

import base64
import dataclasses
import json
import os
import tempfile
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from my_claude_code.config.constants import (
    CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
)
from my_claude_code.config.paths import chatgpt_oauth_auth_path
from my_claude_code.providers.oauth_account_store import (
    ORIGIN_CODEX,
    ORIGIN_MCC,
    account_record_fields,
    backup_once,
    monotonic_write_allowed,
    normalise_epoch_seconds,
    now_iso,
    synthetic_account_id,
)
from my_claude_code.providers.oauth_names import (
    forget_account_name,
    seed_default_name,
)

CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_OAUTH_ORIGINATOR = "codex_cli_rs"
#: The namespaced claim bag OpenAI puts its ChatGPT identity into. Codex reads
#: the same key -- its own error literal is "JWT payload missing expected
#: 'https://api.openai.com/auth' object".
CHATGPT_AUTH_CLAIM = "https://api.openai.com/auth"
CODEX_OAUTH_SCOPE = (
    "openid profile email offline_access api.connectors.read api.connectors.invoke"
)
MANAGED_CREDENTIAL_SCHEMA_VERSION = 1
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600

#: The leeway this module has always used, now named. It was inlined twice,
#: which is how a constant drifts: the Anthropic side names its 120 in
#: ``constants.py`` and ``tests/providers/test_oauth_refresh_parity.py`` exists
#: because these two implementations diverged once already. The **value is
#: unchanged**; only the number of places it is written down is.
REFRESH_LEEWAY_SECONDS = 300

#: The account list lives beside ``tokens``, and ``version`` deliberately
#: **stays 1**: :func:`_load_managed_source` raises on a version it does not
#: know, so bumping it would make a 7.29.x build fail loudly instead of
#: degrading to the primary account. ``accounts`` is an additive key the old
#: reader ignores, and ``tokens`` mirrors ``accounts[0]``.
ACCOUNTS_VERSION = 1
ACCOUNTS_VERSION_KEY = "accounts_version"
ACCOUNTS_KEY = "accounts"

PROVIDER_ID = "chatgpt_oauth"
PROVIDER_LABEL = "ChatGPT"


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


def _load_managed_source(
    path: Path | None = None, *, account_id: str | None = None
) -> _TokenSource:
    """Read the managed store's primary account, or one named account.

    ``account_id`` is how a per-account leaf provider resolves *its own*
    credential rather than whichever account happens to be first. With it
    unset the behaviour is exactly what shipped: the ``tokens`` block, which
    is always a mirror of ``accounts[0]``.
    """
    path = path or chatgpt_oauth_auth_path()
    payload = _load_json(path)
    if payload and payload.get("version") != MANAGED_CREDENTIAL_SCHEMA_VERSION:
        raise ChatGPTOAuthError(
            f"Unsupported FCC ChatGPT OAuth credential schema at {path}."
        )
    tokens = payload.get("tokens") or {}
    if account_id:
        tokens = _account_tokens_from_document(payload, account_id) or tokens
    return _TokenSource(
        name="fcc-managed",
        path=path,
        access_token=tokens.get("access_token"),
        refresh_token=tokens.get("refresh_token"),
        id_token=tokens.get("id_token"),
        account_id=tokens.get("account_id"),
        expires_at=tokens.get("expires_at"),
    )


def _account_tokens_from_document(
    payload: dict[str, Any], account_id: str
) -> dict[str, Any] | None:
    raw = payload.get(ACCOUNTS_KEY)
    if not isinstance(raw, list):
        return None
    for entry in raw:
        if not isinstance(entry, dict) or str(entry.get("id", "")) != account_id:
            continue
        tokens = entry.get("tokens")
        return tokens if isinstance(tokens, dict) else None
    return None


def _reload_source(source: _TokenSource) -> _TokenSource:
    """Re-read one token source from disk (e.g. after another thread refreshed)."""
    if source.name == "fcc-managed":
        return _load_managed_source(source.path, account_id=source.account_id)
    return source


def _load_sources(account_id: str | None = None) -> list[_TokenSource]:
    return [_load_managed_source(account_id=account_id)]


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
    auth_claim = claims.get(CHATGPT_AUTH_CLAIM) or {}
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


def _extract_plan_type_from_claims(claims: dict[str, Any]) -> str:
    """Read ``chatgpt_plan_type`` out of already-decoded claims.

    The plan is the only claim this project reads beyond the account id and
    the expiry. It is never logged, never returned to a client and never
    stored: it is consulted in memory to answer "may this subscription use
    this model?", which is the same question Codex answers client-side from
    the same claim.
    """
    plan = claims.get("chatgpt_plan_type")
    if isinstance(plan, str) and plan.strip():
        return plan.strip()
    auth_claim = claims.get(CHATGPT_AUTH_CLAIM) or {}
    if isinstance(auth_claim, dict):
        plan = auth_claim.get("chatgpt_plan_type")
        if isinstance(plan, str) and plan.strip():
            return plan.strip()
    return ""


def stored_chatgpt_plan_type(*, auth_path: Path | None = None) -> str:
    """Return this credential's ChatGPT plan, decoded locally, or ``""``.

    Strictly local: it reads the stored ID token that is already on disk and
    base64-decodes its payload through the same helper the account-id
    extraction uses. **No network call and no token refresh**, on purpose --
    a model list must never be able to spend a refresh, rotate a refresh token
    or wake a credential that was quietly working.

    ``""`` means unknown, which callers must treat as "do not filter": an
    unreadable plan can never be allowed to hide a model the subscription can
    actually use.
    """
    try:
        source = _load_managed_source(auth_path)
    except ChatGPTOAuthError:
        return ""
    for token in (source.id_token, source.access_token):
        plan = _extract_plan_type_from_claims(_decode_jwt_claims(token))
        if plan:
            return plan
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


@dataclasses.dataclass(frozen=True)
class ChatGPTAccountRecord:
    """One stored ChatGPT/Codex account."""

    id: str
    tokens: dict[str, Any]
    origin: str = ORIGIN_MCC
    origin_path: str = ""
    write_back: bool = False
    added_at: str = ""
    ordinal: int = 1

    @property
    def owns_a_source_file(self) -> bool:
        return self.origin == ORIGIN_CODEX and bool(self.origin_path)


def _entry_has_access_token(entry: dict[Any, Any]) -> bool:
    """Whether one stored entry actually carries a credential."""
    tokens = entry.get("tokens")
    return isinstance(tokens, dict) and bool(tokens.get("access_token"))


def _record_from_entry(entry: dict[Any, Any], *, index: int) -> ChatGPTAccountRecord:
    fields = account_record_fields(
        entry,
        origin_path_key="origin_path",
        write_back_key="write_back",
        added_at_key="added_at",
    )
    tokens = entry.get("tokens")
    tokens = dict(tokens) if isinstance(tokens, dict) else {}
    return ChatGPTAccountRecord(
        id=fields["id"] or str(tokens.get("account_id") or ""),
        tokens=tokens,
        origin=fields["origin"],
        origin_path=fields["origin_path"],
        write_back=fields["write_back"],
        added_at=fields["added_at"] or now_iso(),
        ordinal=fields["ordinal"] if fields["ordinal"] > 1 else index + 1,
    )


def _entry_from_record(record: ChatGPTAccountRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "tokens": dict(record.tokens),
        "origin": record.origin,
        "origin_path": record.origin_path,
        "write_back": record.write_back,
        "added_at": record.added_at,
        "ordinal": record.ordinal,
    }


def load_chatgpt_accounts(
    *, auth_path: Path | None = None, migrate: bool = True
) -> list[ChatGPTAccountRecord]:
    """Every stored ChatGPT account, migrating the single-account shape once.

    Never raises -- not even on the version mismatch the runtime reader raises
    on, because an account *listing* that throws is a dashboard that cannot
    render, and a store this build cannot parse is a credential problem, not a
    page problem.
    """
    path = auth_path or chatgpt_oauth_auth_path()
    try:
        payload = _load_json(path)
    except ChatGPTOAuthError:
        return []
    if payload.get("version") not in (None, MANAGED_CREDENTIAL_SCHEMA_VERSION):
        return []
    raw = payload.get(ACCOUNTS_KEY)
    if isinstance(raw, list) and raw:
        records = [
            _record_from_entry(entry, index=index)
            for index, entry in enumerate(raw)
            if isinstance(entry, dict) and _entry_has_access_token(entry)
        ]
        if records:
            return records
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        return []
    record = ChatGPTAccountRecord(
        id=str(tokens.get("account_id") or "")
        or extract_account_id_from_tokens(
            access_token=tokens.get("access_token"),
            id_token=tokens.get("id_token"),
        )
        or synthetic_account_id(),
        tokens=dict(tokens),
        # Provenance was never persisted before 7.30.0, so claiming this was
        # imported from Codex would be inventing a fact. ``mcc`` is the honest
        # answer and also the safe one: write-back leaves Codex's file alone
        # until the user imports again and says otherwise.
        origin=ORIGIN_MCC,
        write_back=False,
        added_at=now_iso(),
        ordinal=1,
    )
    if migrate:
        backup_once(path)
        save_chatgpt_accounts([record], auth_path=path)
    return [record]


def save_chatgpt_accounts(
    records: Sequence[ChatGPTAccountRecord], *, auth_path: Path | None = None
) -> Path:
    """Write the account list, mirroring the primary to ``tokens``.

    ``version`` stays 1 on purpose: the shipped reader raises on a mismatch,
    so a bump would turn a downgrade into a hard failure rather than a
    degradation to the primary account.
    """
    path = auth_path or chatgpt_oauth_auth_path()
    if not records:
        path.unlink(missing_ok=True)
        return path
    _atomic_write_private_json(
        path,
        {
            "version": MANAGED_CREDENTIAL_SCHEMA_VERSION,
            "tokens": dict(records[0].tokens),
            ACCOUNTS_VERSION_KEY: ACCOUNTS_VERSION,
            ACCOUNTS_KEY: [_entry_from_record(record) for record in records],
        },
    )
    return path


def stored_chatgpt_account_email(
    *, auth_path: Path | None = None, account_id: str = ""
) -> str:
    """The ``email`` claim of a stored id_token, read **only** to seed a name.

    The card's "never reads the identity claims" invariant is narrowed here
    rather than dropped: ``sub`` is still never read, the raw claim is never
    returned by an endpoint, and the email's only destination is
    ``credential_names.json``, as a name like any other the operator can
    change or clear. No network call is involved -- the claim is already on
    disk, in the id_token this store has always kept.
    """
    for record in load_chatgpt_accounts(auth_path=auth_path, migrate=False):
        if account_id and record.id != account_id:
            continue
        claims = _decode_jwt_claims(record.tokens.get("id_token"))
        email = claims.get("email")
        if isinstance(email, str) and email.strip():
            return email.strip()
        if account_id:
            return ""
    return ""


def add_or_update_chatgpt_account(
    tokens: dict[str, Any],
    *,
    origin: str = ORIGIN_MCC,
    origin_path: str = "",
    write_back: bool | None = None,
    auth_path: Path | None = None,
) -> ChatGPTAccountRecord:
    """Add an account, or update in place the one with the same account id.

    The ChatGPT account id is preserved across refresh by OpenAI itself, so
    unlike the Anthropic side there is never any doubt about which record a
    credential belongs to.
    """
    path = auth_path or chatgpt_oauth_auth_path()
    records = list(load_chatgpt_accounts(auth_path=path))
    account_id = str(tokens.get("account_id") or "")
    index = next(
        (i for i, record in enumerate(records) if record.id == account_id), None
    )
    if index is None:
        ordinal = max((record.ordinal for record in records), default=0) + 1
        record = ChatGPTAccountRecord(
            id=account_id,
            tokens=dict(tokens),
            origin=origin,
            origin_path=origin_path,
            write_back=(
                write_back if write_back is not None else origin == ORIGIN_CODEX
            ),
            added_at=now_iso(),
            ordinal=ordinal,
        )
        records.append(record)
    else:
        previous = records[index]
        record = dataclasses.replace(previous, tokens=dict(tokens))
        records[index] = record
    save_chatgpt_accounts(records, auth_path=path)
    seed_default_name(
        PROVIDER_ID,
        record.id,
        email=stored_chatgpt_account_email(auth_path=path, account_id=record.id),
        provider_label=PROVIDER_LABEL,
        ordinal=record.ordinal,
    )
    return record


def remove_chatgpt_account(
    account_id: str, *, auth_path: Path | None = None
) -> ChatGPTAccountRecord | None:
    """Disconnect one ChatGPT account. The file survives while any remain."""
    path = auth_path or chatgpt_oauth_auth_path()
    records = list(load_chatgpt_accounts(auth_path=path))
    remaining = [record for record in records if record.id != account_id]
    if len(remaining) == len(records):
        return None
    removed = next(record for record in records if record.id == account_id)
    save_chatgpt_accounts(remaining, auth_path=path)
    forget_account_name(PROVIDER_ID, account_id)
    return removed


WRITE_BACK_ENV = "CHATGPT_OAUTH_WRITE_BACK"


def chatgpt_write_back_enabled() -> bool:
    """Whether Codex write-back is on. Default on; one switch turns it off.

    Read through ``Settings`` for the reason the Anthropic twin is: the
    dashboard writes ``.env``, which ``pydantic-settings`` reads without
    exporting, so an ``os.environ`` read would ignore the operator's switch.
    """
    try:
        from my_claude_code.config.settings import get_settings

        return bool(getattr(get_settings(), "chatgpt_oauth_write_back", True))
    except Exception:  # pragma: no cover - a settings problem is not a refusal
        return os.environ.get(WRITE_BACK_ENV, "").strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
        )


def chatgpt_write_back_if_owned(
    record: ChatGPTAccountRecord, tokens: dict[str, Any]
) -> bool:
    """Write a refreshed token back into the Codex ``auth.json`` it came from.

    Codex has **no** filesystem lock on its primary auth file -- it has locks
    for its MCP OAuth, its secrets store, its daemon and its pid, but not for
    this -- so unlike the Anthropic side there is no protocol to join and the
    monotonicity guard is the entire protection. Written carefully, because
    OpenAI rotates the refresh token: writing an older bundle over a newer one
    is precisely the failure write-back exists to prevent.

    ``account_id`` on the target is left exactly as found, so Codex's own
    account-id-guarded reload still matches and it does not skip the file as
    changed underneath it.
    """
    if not record.owns_a_source_file or not record.write_back:
        return False
    if not chatgpt_write_back_enabled():
        return False
    target = Path(record.origin_path)
    if not target.is_file():
        return False
    try:
        document = _load_json(target)
    except ChatGPTOAuthError:
        return False
    existing = document.get("tokens")
    existing = dict(existing) if isinstance(existing, dict) else {}
    if not monotonic_write_allowed(
        target_expires_at=_expiry_for_guard(existing),
        target_refresh_expires_at=None,
        ours_expires_at=_expiry_for_guard(tokens),
        ours_refresh_expires_at=None,
    ):
        return False
    backup_once(target)
    existing.update(
        {
            "access_token": tokens.get("access_token"),
            "refresh_token": tokens.get("refresh_token"),
            "id_token": tokens.get("id_token") or existing.get("id_token"),
            "expires_at": tokens.get("expires_at"),
        }
    )
    document["tokens"] = existing
    _atomic_write_private_json(target, document)
    return True


def _expiry_for_guard(tokens: dict[str, Any]) -> int | None:
    """The expiry the race rule compares, falling back to the id_token ``exp``."""
    expires_at = normalise_epoch_seconds(tokens.get("expires_at"))
    if expires_at is not None:
        return expires_at
    claims = _decode_jwt_claims(tokens.get("id_token"))
    return normalise_epoch_seconds(claims.get("exp"))


def store_managed_chatgpt_oauth_tokens(
    tokens: dict[str, Any],
    *,
    auth_path: Path | None = None,
    origin: str = ORIGIN_MCC,
    origin_path: str = "",
    write_back: bool | None = None,
) -> Path:
    """Validate and atomically persist FCC-owned renewable OAuth credentials.

    A thin wrapper over :func:`add_or_update_chatgpt_account` since 7.30.0, so
    a refresh written through it updates *that account's* record and re-mirrors
    the primary rather than replacing the document and dropping the rest.
    """

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
    add_or_update_chatgpt_account(
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": id_token,
            "account_id": account_id,
            "expires_at": _token_expiry(tokens),
        },
        origin=origin,
        origin_path=origin_path,
        write_back=write_back,
        auth_path=path,
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
    """Write refreshed tokens back to FCC's private credential store.

    ...and, when that account was imported from Codex and write-back is on,
    onwards into Codex's own ``auth.json`` under the monotonicity guard.
    """
    if source.name != "fcc-managed":
        raise ChatGPTOAuthError(
            "Refusing to write refreshed credentials outside FCC's auth store."
        )
    bundle = {
        "access_token": access_token,
        "refresh_token": refresh_token or source.refresh_token,
        "id_token": id_token or source.id_token,
        "account_id": source.account_id,
        "expires_at": expires_at,
    }
    record = add_or_update_chatgpt_account(bundle, auth_path=source.path)
    chatgpt_write_back_if_owned(record, bundle)


#: One lock per ``(store path, account id)``. This was a single global
#: ``threading.Lock``, which was right while there was one credential and
#: wrong the moment there were two: a second account's refresh would queue
#: behind the first's network round trip for no reason.
_REFRESH_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_REFRESH_LOCKS_GUARD = threading.Lock()


def _refresh_lock(path: Path, account_id: str | None) -> threading.Lock:
    key = (str(path), account_id or "")
    with _REFRESH_LOCKS_GUARD:
        lock = _REFRESH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _REFRESH_LOCKS[key] = lock
        return lock


def _ensure_fresh_source(source: _TokenSource) -> _TokenSource:
    remaining = (
        _access_token_seconds_remaining(source.access_token)
        if source.access_token
        else None
    )
    if remaining is None or remaining > REFRESH_LEEWAY_SECONDS:
        return source
    if not source.has_refresh_token or source.refresh_token is None:
        # Token is expiring and we cannot refresh; return as-is and let the
        # upstream request fail with a clear 401 if expired.
        return source

    with _refresh_lock(source.path, source.account_id):
        # Another thread may have refreshed while we waited on the lock.
        current = _reload_source(source)
        current_access_token = current.access_token
        if current.has_access_token and current_access_token is not None:
            remaining = _access_token_seconds_remaining(current_access_token)
            if remaining is not None and remaining > REFRESH_LEEWAY_SECONDS:
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
    pinned_account_id: str | None = None,
) -> ChatGPTOAuthCredentials:
    """Resolve OAuth credentials from explicit values or auth files.

    Priority:
      1. Explicit access_token / account_id.
      2. FCC's private renewable credential store.

    ``pinned_account_id`` selects **which stored account** to resolve, and is
    how a per-account leaf provider gets its own credential instead of
    whichever one happens to be primary. ``account_id`` is a different thing
    and always was: the value of the ``ChatGPT-Account-ID`` *header*.
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

    source = _choose_runtime_source(_load_sources(pinned_account_id))
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


def force_refresh_managed_chatgpt_oauth_credentials(
    account_id: str | None = None,
) -> ChatGPTOAuthCredentials:
    """Refresh FCC-owned credentials after an upstream unauthorized response.

    ``account_id`` names the account the request that got the 401 was served
    by, so a 401 on one account refreshes **that** account and leaves the
    others -- and their refresh tokens -- untouched.
    """

    with _refresh_lock(chatgpt_oauth_auth_path(), account_id):
        source = _load_managed_source(account_id=account_id)
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
                # Retire **that account only**. Unlinking the file was right
                # while it held one credential and is wrong now: one dead
                # account must not take every other account's credential with
                # it. The file survives while any account remains.
                retired = source.account_id or account_id or ""
                if retired and remove_chatgpt_account(retired, auth_path=source.path):
                    pass
                else:
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
        },
        # Provenance, persisted. Without it write-back is impossible -- there
        # is nothing on disk that says this credential belongs to a file MCC
        # does not own.
        origin=ORIGIN_CODEX,
        origin_path=str(source.path),
        write_back=True,
    )
    managed = _ensure_fresh_source(_load_managed_source(account_id=source.account_id))
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
