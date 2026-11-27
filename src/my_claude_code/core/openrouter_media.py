"""OpenRouter Chat Completions image generation & editing (``modalities: ["image", "text"]``).

OpenRouter has no ``/v1/images/generations`` or ``/v1/images/edits``. It serves
image models through ``POST /chat/completions`` with ``modalities: ["image",
"text"]`` (and optional ``image_config``) and returns images inside
``choices[0].message.images[]`` (``image_url.url`` data URIs or HTTPS URLs).

Pure and synchronous: callers run base64 / JSON work through ``asyncio.to_thread``.
"""

import base64
import binascii
import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from my_claude_code.core.openai_images import sniff_image_mime

_DATA_URI_RE = re.compile(
    r"^\s*data:(?P<mime>[A-Za-z0-9.+/-]+)?(?:;[A-Za-z0-9.=+-]+)*;base64,(?P<data>.+)\s*$",
    re.DOTALL,
)


class OpenRouterAnswerError(ValueError):
    """The answer holds nothing this request can be served with.

    The message completes "<provider> answered ...", e.g. "without an image".
    """


@dataclass(frozen=True, slots=True)
class OpenRouterImageAnswer:
    """An OpenRouter chat image answer translated to OpenAI Images JSON."""

    body: bytes
    content_type: str = "application/json"
    usage: dict[str, Any] | None = None
    text: str | None = None


def openrouter_chat_image_request(
    prompt: str,
    model: str,
    *,
    image_data_uris: Sequence[str] = (),
    aspect_ratio: str | None = None,
    image_size: str | None = None,
) -> dict[str, Any]:
    """Build a Chat Completions body requesting image output."""

    if image_data_uris:
        content: Any = [
            {"type": "text", "text": prompt},
            *(
                {"type": "image_url", "image_url": {"url": uri}}
                for uri in image_data_uris
            ),
        ]
    else:
        content = prompt
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "modalities": ["image", "text"],
        "stream": False,
    }
    image_config: dict[str, str] = {}
    if isinstance(aspect_ratio, str) and aspect_ratio.strip():
        image_config["aspect_ratio"] = aspect_ratio.strip()
    if isinstance(image_size, str) and image_size.strip():
        image_config["image_size"] = image_size.strip()
    if image_config:
        payload["image_config"] = image_config
    return payload


def _mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {str(key): item for key, item in value.items()}


def _usage(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    block = _mapping(payload.get("usage"))
    if block is None:
        return None
    usage = dict(block)
    for source, target in (
        ("prompt_tokens", "input_tokens"),
        ("completion_tokens", "output_tokens"),
    ):
        value = block.get(source)
        if isinstance(value, int) and not isinstance(value, bool):
            usage.setdefault(target, value)
    return usage


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        part = _mapping(item)
        if part is None:
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "".join(parts)


def _decode_b64_item(raw_b64: str) -> dict[str, Any] | None:
    cleaned = re.sub(r"\s+", "", raw_b64)
    if not cleaned:
        return None
    try:
        decoded = base64.b64decode(cleaned, validate=False)
    except binascii.Error, ValueError:
        return None
    if not decoded:
        return None
    return {"b64_json": cleaned}


def _item_from_ref(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        match = _DATA_URI_RE.match(stripped)
        if match is not None:
            return _decode_b64_item(match.group("data"))
        if stripped.startswith(("https://", "http://")):
            return {"url": stripped}
        decoded = _decode_b64_item(stripped)
        if decoded is not None:
            try:
                raw = base64.b64decode(decoded["b64_json"], validate=False)
            except binascii.Error, ValueError:
                raw = b""
            if sniff_image_mime(raw) is not None:
                return decoded
        return None
    entry = _mapping(value)
    if entry is None:
        return None
    for b64_key in ("b64_json", "b64", "image_base64", "base64", "data"):
        b64_val = entry.get(b64_key)
        if isinstance(b64_val, str) and b64_val.strip():
            decoded = _decode_b64_item(b64_val)
            if decoded is not None:
                return decoded
    image_url = entry.get("image_url")
    if isinstance(image_url, str):
        return _item_from_ref(image_url)
    if isinstance(image_url, dict):
        url = image_url.get("url")
        if isinstance(url, str):
            return _item_from_ref(url)
    url = entry.get("url")
    if isinstance(url, str):
        return _item_from_ref(url)
    return None


def parse_openrouter_image_answer(
    raw: bytes, *, created: int | None = None
) -> OpenRouterImageAnswer:
    """Translate an OpenRouter Chat Completions image answer to OpenAI Images JSON."""

    try:
        payload = _mapping(json.loads(raw))
    except ValueError, UnicodeDecodeError:
        payload = None
    if payload is None:
        raise OpenRouterAnswerError("with a body that is not a JSON object")

    choices = payload.get("choices")
    message: dict[str, Any] = {}
    if isinstance(choices, list) and choices:
        first_choice = _mapping(choices[0])
        if first_choice is not None:
            message = _mapping(first_choice.get("message")) or {}

    items: list[dict[str, Any]] = []
    raw_images = message.get("images")
    if isinstance(raw_images, list):
        for entry in raw_images:
            item = _item_from_ref(entry)
            if item is not None:
                items.append(item)

    content = message.get("content")
    if not items and isinstance(content, list):
        for part in content:
            part_map = _mapping(part)
            if part_map is None:
                continue
            if part_map.get("type") == "text":
                continue
            item = _item_from_ref(part_map)
            if item is not None:
                items.append(item)

    if not items:
        raise OpenRouterAnswerError("without an image")

    usage = _usage(payload)
    payload_created = payload.get("created")
    effective_created = (
        created
        if created is not None
        else (
            payload_created
            if isinstance(payload_created, int)
            and not isinstance(payload_created, bool)
            else int(time.time())
        )
    )
    out: dict[str, Any] = {"created": effective_created, "data": items}
    if usage is not None:
        out["usage"] = usage
    text = _text_of(content).strip() or None
    return OpenRouterImageAnswer(
        body=json.dumps(out, separators=(",", ":")).encode("utf-8"),
        usage=usage,
        text=text,
    )
