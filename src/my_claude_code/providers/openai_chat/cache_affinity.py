"""Opaque restart-stable session digest and declarative cache affinity policy."""

import hashlib
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.client_fingerprint import (
    MAX_CHATGPT_SESSION_CHARS,
    current_affinity_session_id,
)

#: HTTP header carrying worker cache affinity for Mistral & opted-in custom hosts.
CACHE_AFFINITY_HEADER_NAME = "x-affinity"

#: Opaque prefix for MCC's deterministic prompt cache affinity digest.
CACHE_AFFINITY_DIGEST_PREFIX = "mcc-aff-"

#: Bounded length of the emitted digest (8-char prefix + 32 hex chars = 40 <= 64).
CACHE_AFFINITY_DIGEST_MAX_CHARS = 64

#: Cap on concatenated ``system + first_user`` fallback seed text before hashing.
MAX_AFFINITY_SEED_TEXT_CHARS = 4_096

#: ``user_<hash>_account_<uuid>_session_<uuid>``: matches ``core/request_origin.py``.
_METADATA_SESSION_TAIL = re.compile(r"(?:^|_)session_(?P<value>[0-9A-Za-z-]{8,128})$")


@dataclass(frozen=True, slots=True)
class CacheAffinitySpec:
    """Declarative worker cache-affinity policy for an OpenAI-chat host.

    Separate from :class:`~my_claude_code.providers.openai_chat.ClientIdentity`:
    adding cache affinity never alters ``client_identity`` or ``default_headers``.
    """

    header_name: str | None = CACHE_AFFINITY_HEADER_NAME
    send_body_key: bool = True
    enabled: Callable[[], bool] | None = None

    def is_active(self) -> bool:
        """Whether cache affinity is enabled for the current request."""
        return True if self.enabled is None else bool(self.enabled())


def cache_affinity_spec_for_mode(mode: str) -> CacheAffinitySpec | None:
    """Build a :class:`CacheAffinitySpec` for one custom-provider mode."""
    normalized = str(mode or "").strip().lower()
    if normalized == "body":
        return CacheAffinitySpec(header_name=None, send_body_key=True)
    if normalized == "header":
        return CacheAffinitySpec(
            header_name=CACHE_AFFINITY_HEADER_NAME, send_body_key=False
        )
    if normalized == "both":
        return CacheAffinitySpec(
            header_name=CACHE_AFFINITY_HEADER_NAME, send_body_key=True
        )
    return None


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


def _opening_turns_from_mapping(body: Mapping[str, Any]) -> tuple[str, str]:
    messages = body.get("messages")
    if isinstance(messages, Sequence) and not isinstance(messages, str | bytes):
        system_parts: list[str] = []
        first_user_text = ""
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            role = message.get("role")
            if role in ("system", "developer") and not system_parts:
                text = _extract_content_text(message.get("content"))
                if text:
                    system_parts.append(text)
            elif role == "user" and not first_user_text:
                first_user_text = _extract_content_text(message.get("content"))
            if system_parts and first_user_text:
                break
        return ("\n".join(system_parts), first_user_text)

    instructions = body.get("instructions")
    system_text = instructions.strip() if isinstance(instructions, str) else ""
    items = body.get("input")
    first_user_text = ""
    if isinstance(items, Sequence) and not isinstance(items, str | bytes):
        first_user_text = _first_user_text(items)
    elif isinstance(items, str) and items.strip():
        first_user_text = items.strip()
    return (system_text, first_user_text)


def resolve_affinity_seed(
    request_or_body: MessagesRequest | Mapping[str, Any] | None,
    *,
    header_session_id: str | None = None,
    fallback_session_id: str | None = None,
) -> str | None:
    """Resolve the highest-precedence root-session seed, or ``None`` if empty."""
    candidate = (
        current_affinity_session_id()
        if header_session_id is None
        else header_session_id
    )
    if isinstance(candidate, str):
        cleaned = candidate.strip()
        if cleaned:
            return f"header:{cleaned[:MAX_CHATGPT_SESSION_CHARS]}"

    metadata = (
        request_or_body.metadata
        if isinstance(request_or_body, MessagesRequest)
        else (
            request_or_body.get("metadata")
            if isinstance(request_or_body, Mapping)
            else None
        )
    )
    if isinstance(metadata, Mapping):
        user_id = metadata.get("user_id")
        if isinstance(user_id, str) and user_id.strip():
            match = _METADATA_SESSION_TAIL.search(user_id.strip())
            if match:
                return f"metadata:{match.group('value')}"
        meta_session_id = metadata.get("session_id")
        if isinstance(meta_session_id, str) and meta_session_id.strip():
            return f"metadata:{meta_session_id.strip()[:MAX_CHATGPT_SESSION_CHARS]}"

    system_text = ""
    first_user_text = ""
    if isinstance(request_or_body, MessagesRequest):
        system_text = _extract_system_text(request_or_body.system)
        first_user_text = _first_user_text(request_or_body.messages)
    elif isinstance(request_or_body, Mapping):
        system_text, first_user_text = _opening_turns_from_mapping(request_or_body)

    if system_text or first_user_text:
        combined = f"{system_text}\x00{first_user_text}"[:MAX_AFFINITY_SEED_TEXT_CHARS]
        return f"messages:{combined}"

    if isinstance(fallback_session_id, str) and fallback_session_id.strip():
        return f"fallback:{fallback_session_id.strip()[:MAX_CHATGPT_SESSION_CHARS]}"
    return None


def resolve_affinity_digest(
    request_or_body: MessagesRequest | Mapping[str, Any] | None,
    *,
    header_session_id: str | None = None,
    fallback_session_id: str | None = None,
) -> str | None:
    """Return an opaque, restart-stable 40-char ``mcc-aff-...`` digest or ``None``."""
    seed = resolve_affinity_seed(
        request_or_body,
        header_session_id=header_session_id,
        fallback_session_id=fallback_session_id,
    )
    if seed is None:
        return None
    digest = hashlib.sha256(
        b"mcc:cache-affinity:v1:" + seed.encode("utf-8")
    ).hexdigest()[:32]
    return f"{CACHE_AFFINITY_DIGEST_PREFIX}{digest}"


def apply_cache_affinity_body(
    body: dict[str, Any],
    request: MessagesRequest,
    spec: CacheAffinitySpec | None,
    *,
    unsupported_body_keys: Iterable[str] = (),
) -> str | None:
    """Set ``body['prompt_cache_key']`` when ``spec`` sends it and seed is non-empty."""
    if spec is None or not spec.is_active():
        return None
    digest = resolve_affinity_digest(request)
    if digest is None:
        return None
    if spec.send_body_key and "prompt_cache_key" not in unsupported_body_keys:
        body.setdefault("prompt_cache_key", digest)
    return digest


def cache_affinity_headers(
    spec: CacheAffinitySpec | None,
    body_or_request: MessagesRequest | Mapping[str, Any] | None,
    *,
    digest: str | None = None,
) -> dict[str, str]:
    """Return per-call cache affinity HTTP headers when active on ``spec``."""
    if spec is None or not spec.is_active() or not spec.header_name:
        return {}
    resolved = digest
    if resolved is None and isinstance(body_or_request, Mapping):
        existing = body_or_request.get("prompt_cache_key")
        if isinstance(existing, str) and existing.strip():
            resolved = existing.strip()
    if resolved is None:
        resolved = resolve_affinity_digest(body_or_request)
    if not resolved:
        return {}
    return {spec.header_name: resolved}
