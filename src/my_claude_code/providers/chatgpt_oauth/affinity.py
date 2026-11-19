"""Per-conversation cache affinity and account-fenced turn-state cache."""

import hashlib
import re
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.client_fingerprint import (
    MAX_CHATGPT_SESSION_CHARS,
    current_chatgpt_session_id,
)

#: Fixed UUIDv5 namespace for deterministic, restart-stable ChatGPT OAuth
#: ``session-id`` / ``thread-id`` derivation.
_MCC_CHATGPT_AFFINITY_NAMESPACE = uuid.UUID("a3f8c2d0-7b1e-5a94-8c6d-1e2f3a4b5c6d")

#: ``user_<hash>_account_<uuid>_session_<uuid>``: matches ``core/request_origin.py``.
_METADATA_SESSION_TAIL = re.compile(r"(?:^|_)session_(?P<value>[0-9A-Za-z-]{8,128})$")

#: Hard limit on ``prompt_cache_key`` (``C-Q7``).
PROMPT_CACHE_KEY_MAX_CHARS = 64

#: Cap on concatenated ``system + first_user`` fallback seed text.
MAX_SEED_TEXT_CHARS = 2_048

#: Sticky ``x-codex-turn-state`` lifetime (2 hours).
TURN_STATE_TTL_SECONDS = 7_200.0

#: Bounded LRU capacity for cached ``x-codex-turn-state`` entries.
TURN_STATE_MAX_ENTRIES = 1_024

#: Maximum length of a captured ``x-codex-turn-state`` header value.
TURN_STATE_MAX_VALUE_CHARS = 512


@dataclass(frozen=True, slots=True)
class ConversationAffinity:
    """Deterministic per-conversation wire identifiers for ChatGPT OAuth."""

    session_id: str
    thread_id: str
    prompt_cache_key: str
    source: str


def _extract_system_text(system: object) -> str:
    if isinstance(system, str):
        return system.strip()
    if isinstance(system, Sequence) and not isinstance(system, bytes):
        parts: list[str] = []
        for item in system:
            if isinstance(item, str):
                text = item.strip()
            elif isinstance(item, Mapping):
                raw = item.get("text")
                text = raw.strip() if isinstance(raw, str) else ""
            else:
                raw = getattr(item, "text", None)
                text = raw.strip() if isinstance(raw, str) else ""
            if text:
                parts.append(text)
        return "\n".join(parts)
    return ""


def _extract_content_text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, Sequence) and not isinstance(content, bytes):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                text = block.strip()
            elif isinstance(block, Mapping):
                raw = block.get("text")
                text = raw.strip() if isinstance(raw, str) else ""
            else:
                raw = getattr(block, "text", None)
                text = raw.strip() if isinstance(raw, str) else ""
            if text:
                parts.append(text)
        return "\n".join(parts)
    return ""


def _first_user_text(messages: Sequence[Any]) -> str:
    for message in messages:
        role = (
            message.get("role")
            if isinstance(message, Mapping)
            else getattr(message, "role", None)
        )
        if role != "user":
            continue
        content = (
            message.get("content")
            if isinstance(message, Mapping)
            else getattr(message, "content", None)
        )
        text = _extract_content_text(content)
        if text:
            return text
    return ""


def resolve_conversation_seed(
    request: MessagesRequest,
    *,
    header_session_id: str | None = None,
) -> tuple[str, str] | None:
    """Resolve the highest-precedence conversation seed, or ``None`` if empty."""
    candidate = (
        current_chatgpt_session_id() if header_session_id is None else header_session_id
    )
    if isinstance(candidate, str):
        cleaned = candidate.strip()
        if cleaned:
            return ("header", cleaned[:MAX_CHATGPT_SESSION_CHARS])

    metadata = request.metadata
    if isinstance(metadata, Mapping):
        user_id = metadata.get("user_id")
        if isinstance(user_id, str) and user_id.strip():
            match = _METADATA_SESSION_TAIL.search(user_id.strip())
            if match:
                return ("metadata", match.group("value"))
        meta_session_id = metadata.get("session_id")
        if isinstance(meta_session_id, str) and meta_session_id.strip():
            return ("metadata", meta_session_id.strip()[:MAX_CHATGPT_SESSION_CHARS])

    system_text = _extract_system_text(request.system)
    user_text = _first_user_text(request.messages)
    if not system_text and not user_text:
        return None
    combined = f"{system_text}\x00{user_text}"[:MAX_SEED_TEXT_CHARS]
    return ("messages", combined)


def derive_conversation_affinity(
    request: MessagesRequest,
    *,
    fallback_session_id: str,
    header_session_id: str | None = None,
) -> ConversationAffinity:
    """Derive per-conversation ``session-id``, ``thread-id``, and ``prompt_cache_key``."""
    seed = resolve_conversation_seed(request, header_session_id=header_session_id)
    if seed is not None:
        source, value = seed
        session_id = str(
            uuid.uuid5(_MCC_CHATGPT_AFFINITY_NAMESPACE, f"session:{source}:{value}")
        )
        cache_digest = hashlib.sha256(f"cache:{source}:{value}".encode()).hexdigest()[
            :32
        ]
        return ConversationAffinity(
            session_id=session_id,
            thread_id=session_id,
            prompt_cache_key=f"mcc-cgpt-{cache_digest}",
            source=source,
        )

    cache_digest = hashlib.sha256(
        f"cache:fallback:{fallback_session_id}".encode()
    ).hexdigest()[:32]
    return ConversationAffinity(
        session_id=fallback_session_id,
        thread_id=fallback_session_id,
        prompt_cache_key=f"mcc-cgpt-{cache_digest}",
        source="fallback",
    )


def new_client_request_id() -> str:
    """Return a fresh per-attempt UUID4 correlation id for ``x-client-request-id``."""
    return str(uuid.uuid4())


def account_scope_key(credentials: Any) -> str:
    """Return a strict per-account scope token so turn state never crosses accounts."""
    account_id = getattr(credentials, "account_id", "")
    if isinstance(account_id, str) and account_id.strip():
        return account_id.strip()
    access_token = getattr(credentials, "access_token", "") or ""
    token_hash = hashlib.sha256(str(access_token).encode("utf-8")).hexdigest()[:16]
    return f"tok:{token_hash}"


def turn_state_cache_key(
    base_url: str,
    credentials: Any,
    conversation_session_id: str,
) -> tuple[str, str, str]:
    """Build the strict 3-tuple cache key ``(base_url, account_scope_key, session_id)``."""
    return (
        base_url.rstrip("/"),
        account_scope_key(credentials),
        conversation_session_id,
    )


def normalise_turn_state(raw: object) -> str | None:
    """Validate and trim an inbound ``x-codex-turn-state`` header value."""
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if not cleaned or len(cleaned) > TURN_STATE_MAX_VALUE_CHARS:
        return None
    if "\r" in cleaned or "\n" in cleaned or not cleaned.isascii():
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in cleaned):
        return None
    return cleaned


@dataclass(frozen=True, slots=True)
class _TurnStateEntry:
    value: str
    stored_at: float


class TurnStateCache:
    """Thread-safe bounded LRU + TTL cache for ``x-codex-turn-state`` headers."""

    def __init__(
        self,
        *,
        ttl_seconds: float = TURN_STATE_TTL_SECONDS,
        max_entries: int = TURN_STATE_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_entries = max(1, max_entries)
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[str, str, str], _TurnStateEntry] = (
            OrderedDict()
        )

    def _prune_expired_locked(self, now: float) -> None:
        expired = [
            key
            for key, entry in self._entries.items()
            if now - entry.stored_at >= self._ttl_seconds
        ]
        for key in expired:
            del self._entries[key]

    def get(self, key: tuple[str, str, str]) -> str | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            now = self._clock()
            if now - entry.stored_at >= self._ttl_seconds:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return entry.value

    def set(self, key: tuple[str, str, str], value: object) -> bool:
        normalised = normalise_turn_state(value)
        if normalised is None:
            return False
        with self._lock:
            now = self._clock()
            self._prune_expired_locked(now)
            self._entries[key] = _TurnStateEntry(value=normalised, stored_at=now)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
            return True

    def delete(self, key: tuple[str, str, str]) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


_TURN_STATE_CACHE = TurnStateCache()


def get_chatgpt_turn_state_cache() -> TurnStateCache:
    """Return the process-wide ChatGPT OAuth ``x-codex-turn-state`` cache."""
    return _TURN_STATE_CACHE


def clear_chatgpt_turn_state_cache() -> None:
    """Clear all cached ``x-codex-turn-state`` entries (used by test isolation)."""
    _TURN_STATE_CACHE.clear()
