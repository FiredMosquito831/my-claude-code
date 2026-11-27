"""OCR request and response codecs across Mistral OCR, Gemini native, and Vision chat.

Pure and synchronous: callers run base64 / JSON work through ``asyncio.to_thread``.
Imports stdlib only so ``core`` stays free of ``config`` and ``providers``.
"""

import base64
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

DEFAULT_OCR_PROMPT = (
    "Extract all text and structure from this document or image verbatim as "
    "Markdown. Return only the extracted content without commentary."
)

_DATA_URI_RE = re.compile(
    r"^\s*data:(?P<mime>[A-Za-z0-9.+/-]+)?(?:;[A-Za-z0-9.=+-]+)*;base64,(?P<data>.+)\s*$",
    re.DOTALL,
)

_MISTRAL_OPTIONAL_FIELDS = (
    "pages",
    "include_image_base64",
    "image_limit",
    "image_min_size",
    "document_annotation_format",
    "bbox_annotation_format",
    "table_format",
    "extract_header",
    "extract_footer",
)


class OcrAnswerError(ValueError):
    """The answer holds no OCR result this request can be served with.

    The message completes "<provider> answered ...".
    """


@dataclass(frozen=True, slots=True)
class OcrMediaAnswer:
    """A normalized OCR answer in MCC's canonical JSON format."""

    body: bytes
    content_type: str = "application/json"
    text: str | None = None
    usage: dict[str, Any] | None = None
    pages_processed: int | None = None


def _mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _parse_json_object(raw: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except ValueError, UnicodeDecodeError:
        parsed = None
    payload = _mapping(parsed)
    if payload is None:
        raise OcrAnswerError("with a body that is not a JSON object")
    return payload


def _is_image_uri(uri: str) -> bool:
    lowered = uri.strip().lower()
    if lowered.startswith("data:image/"):
        return True
    path_part = lowered.split("?", 1)[0].split("#", 1)[0]
    return path_part.endswith(
        (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff")
    )


def _extract_source_url(body: Mapping[str, Any]) -> str | None:
    document = body.get("document")
    if isinstance(document, str) and document.strip():
        return document.strip()
    if isinstance(document, Mapping):
        for key in ("document_url", "image_url", "file_url", "url"):
            val = document.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
            if isinstance(val, Mapping):
                nested = val.get("url")
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    for key in ("document_url", "file_url"):
        val = body.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    image_url = body.get("image_url")
    if isinstance(image_url, str) and image_url.strip():
        return image_url.strip()
    if isinstance(image_url, Mapping):
        nested = image_url.get("url")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return None


def _mistral_document_block(
    body: Mapping[str, Any], data_uri: str | None = None
) -> dict[str, Any]:
    if data_uri is not None:
        if data_uri.strip().lower().startswith("data:image/"):
            return {"type": "image_url", "image_url": data_uri}
        return {"type": "document_url", "document_url": data_uri}

    raw_doc = body.get("document")
    if isinstance(raw_doc, Mapping) and raw_doc.get("type"):
        return {str(k): v for k, v in raw_doc.items()}

    url = _extract_source_url(body)
    if url is not None:
        if body.get("image_url") is not None or _is_image_uri(url):
            return {"type": "image_url", "image_url": url}
        return {"type": "document_url", "document_url": url}
    return {}


def mistral_ocr_request(
    body: Mapping[str, Any],
    *,
    model: str,
    data_uri: str | None = None,
) -> dict[str, Any]:
    """Build a Mistral ``POST /v1/ocr`` JSON request payload."""

    payload: dict[str, Any] = {
        "model": model,
        "document": _mistral_document_block(body, data_uri=data_uri),
    }
    for key in _MISTRAL_OPTIONAL_FIELDS:
        if key in body and body[key] is not None:
            payload[key] = body[key]
    return payload


def _normalize_mistral_usage(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, int | None]:
    usage_info = _mapping(payload.get("usage_info"))
    pages_processed = (
        _count(usage_info.get("pages_processed")) if usage_info is not None else None
    )
    raw_usage = _mapping(payload.get("usage"))
    usage: dict[str, Any] | None = None
    if raw_usage is not None:
        usage = dict(raw_usage)
        for src, dst in (
            ("prompt_tokens", "input_tokens"),
            ("completion_tokens", "output_tokens"),
        ):
            val = _count(raw_usage.get(src))
            if val is not None:
                usage.setdefault(dst, val)
    return usage, pages_processed


def parse_mistral_ocr_answer(raw: bytes) -> OcrMediaAnswer:
    """Normalize a Mistral ``/v1/ocr`` JSON response into canonical OCR output."""

    payload = _parse_json_object(raw)
    raw_pages = payload.get("pages")
    if not isinstance(raw_pages, Sequence) or isinstance(raw_pages, str | bytes):
        raise OcrAnswerError("without OCR pages")

    pages: list[dict[str, Any]] = []
    markdowns: list[str] = []
    for idx, entry in enumerate(raw_pages):
        page = _mapping(entry)
        if page is None:
            continue
        index = _count(page.get("index"))
        md = page.get("markdown")
        md_text = md if isinstance(md, str) else ""
        normalized_page: dict[str, Any] = {
            "index": idx if index is None else index,
            "markdown": md_text,
        }
        for extra in ("images", "dimensions"):
            if extra in page:
                normalized_page[extra] = page[extra]
        pages.append(normalized_page)
        if md_text.strip():
            markdowns.append(md_text)

    if not pages:
        raise OcrAnswerError("without OCR pages")

    text = "\n\n".join(markdowns)
    usage, pages_processed = _normalize_mistral_usage(payload)
    if pages_processed is None:
        pages_processed = len(pages)

    out: dict[str, Any] = {
        "object": "ocr",
        "pages": pages,
        "text": text,
    }
    if isinstance(payload.get("model"), str) and payload["model"]:
        out["model"] = payload["model"]
    if payload.get("usage_info") is not None:
        out["usage_info"] = payload["usage_info"]
    if usage is not None:
        out["usage"] = usage

    return OcrMediaAnswer(
        body=json.dumps(out, separators=(",", ":")).encode("utf-8"),
        text=text,
        usage=usage,
        pages_processed=pages_processed,
    )


def gemini_ocr_request(
    body: Mapping[str, Any],
    *,
    data: bytes | None = None,
    mime_type: str | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    """Build a Gemini native ``:generateContent`` OCR request body."""

    prompt = body.get("prompt")
    instruction = (
        prompt.strip()
        if isinstance(prompt, str) and prompt.strip()
        else DEFAULT_OCR_PROMPT
    )

    parts: list[dict[str, Any]] = []
    if data is not None:
        parts.append(
            {
                "inlineData": {
                    "mimeType": mime_type or "application/pdf",
                    "data": base64.b64encode(data).decode("ascii"),
                }
            }
        )
    else:
        source_url = url or _extract_source_url(body)
        if source_url is not None:
            match = _DATA_URI_RE.match(source_url)
            if match is not None:
                uri_mime = match.group("mime") or mime_type or "application/pdf"
                uri_data = re.sub(r"\s+", "", match.group("data"))
                parts.append(
                    {
                        "inlineData": {
                            "mimeType": uri_mime,
                            "data": uri_data,
                        }
                    }
                )
            else:
                inferred_mime = mime_type or (
                    "image/png" if _is_image_uri(source_url) else "application/pdf"
                )
                parts.append(
                    {
                        "fileData": {
                            "mimeType": inferred_mime,
                            "fileUri": source_url,
                        }
                    }
                )
    parts.append({"text": instruction})

    payload: dict[str, Any] = {"contents": [{"parts": parts}]}
    generation: dict[str, Any] = {}
    output_format = body.get("output_format")
    if isinstance(output_format, str) and output_format.strip().lower() == "json":
        generation["responseMimeType"] = "application/json"
    if isinstance(body.get("response_schema"), Mapping):
        generation["responseSchema"] = dict(body["response_schema"])
    if generation:
        payload["generationConfig"] = generation
    return payload


def _gemini_usage(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = payload.get("usageMetadata")
    if raw is None:
        raw = payload.get("usage_metadata")
    metadata = _mapping(raw)
    if metadata is None:
        return None
    prompt = _count(
        metadata.get("promptTokenCount")
        if "promptTokenCount" in metadata
        else metadata.get("prompt_token_count")
    )
    output = _count(
        metadata.get("candidatesTokenCount")
        if "candidatesTokenCount" in metadata
        else metadata.get("candidates_token_count")
    )
    total = _count(
        metadata.get("totalTokenCount")
        if "totalTokenCount" in metadata
        else metadata.get("total_token_count")
    )
    if total is None and prompt is not None and output is not None:
        total = prompt + output
    if prompt is None and output is None and total is None:
        return None
    usage: dict[str, Any] = {"type": "tokens"}
    if prompt is not None:
        usage["input_tokens"] = prompt
    if output is not None:
        usage["output_tokens"] = output
    if total is not None:
        usage["total_tokens"] = total
    return usage


def parse_gemini_ocr_answer(raw: bytes) -> OcrMediaAnswer:
    """Normalize a Gemini native ``generateContent`` OCR answer."""

    payload = _parse_json_object(raw)
    candidates = payload.get("candidates")
    texts: list[str] = []
    if isinstance(candidates, list) and candidates:
        first = _mapping(candidates[0])
        content = None if first is None else _mapping(first.get("content"))
        parts = None if content is None else content.get("parts")
        if isinstance(parts, list):
            for item in parts:
                part = _mapping(item)
                if part is None:
                    continue
                text_val = part.get("text")
                if isinstance(text_val, str) and text_val:
                    texts.append(text_val)
    text = "".join(texts).strip()
    if not text:
        raise OcrAnswerError("without OCR text")

    usage = _gemini_usage(payload)
    out: dict[str, Any] = {
        "object": "ocr",
        "pages": [{"index": 0, "markdown": text}],
        "text": text,
    }
    if usage is not None:
        out["usage"] = usage

    return OcrMediaAnswer(
        body=json.dumps(out, separators=(",", ":")).encode("utf-8"),
        text=text,
        usage=usage,
        pages_processed=1,
    )


def _is_pdf_uri(uri: str) -> bool:
    lowered = uri.strip().lower()
    if lowered.startswith("data:application/pdf"):
        return True
    path_part = lowered.split("?", 1)[0].split("#", 1)[0]
    return path_part.endswith(".pdf")


def vision_chat_ocr_request(
    body: Mapping[str, Any],
    *,
    model: str,
    data_uri: str | None = None,
    url: str | None = None,
    filename: str = "document.pdf",
) -> dict[str, Any]:
    """Build an OpenAI ``POST /chat/completions`` vision/document OCR payload."""

    prompt = body.get("prompt")
    instruction = (
        prompt.strip()
        if isinstance(prompt, str) and prompt.strip()
        else DEFAULT_OCR_PROMPT
    )
    source = data_uri or url or _extract_source_url(body)
    content: list[dict[str, Any]] = [{"type": "text", "text": instruction}]
    if source is not None:
        if _is_pdf_uri(source):
            content.append(
                {
                    "type": "file",
                    "file": {
                        "filename": filename,
                        "file_data": source,
                    },
                }
            )
        else:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": source},
                }
            )
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "stream": False,
    }
    output_format = body.get("output_format")
    if isinstance(output_format, str) and output_format.strip().lower() == "json":
        payload["response_format"] = {"type": "json_object"}
    return payload


def _chat_usage(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    block = _mapping(payload.get("usage"))
    if block is None:
        return None
    usage = dict(block)
    for src, dst in (
        ("prompt_tokens", "input_tokens"),
        ("completion_tokens", "output_tokens"),
    ):
        val = _count(block.get(src))
        if val is not None:
            usage.setdefault(dst, val)
    return usage


def _chat_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        part = _mapping(item)
        if part is None:
            continue
        text_val = part.get("text")
        if isinstance(text_val, str) and text_val:
            parts.append(text_val)
    return "".join(parts)


def parse_vision_chat_ocr_answer(raw: bytes) -> OcrMediaAnswer:
    """Normalize an OpenAI Chat Completions vision OCR answer."""

    payload = _parse_json_object(raw)
    choices = payload.get("choices")
    text = ""
    if isinstance(choices, list) and choices:
        first = _mapping(choices[0])
        message = None if first is None else _mapping(first.get("message"))
        if message is not None:
            text = _chat_text(message.get("content")).strip()
    if not text:
        raise OcrAnswerError("without OCR text")

    usage = _chat_usage(payload)
    out: dict[str, Any] = {
        "object": "ocr",
        "pages": [{"index": 0, "markdown": text}],
        "text": text,
    }
    if usage is not None:
        out["usage"] = usage

    return OcrMediaAnswer(
        body=json.dumps(out, separators=(",", ":")).encode("utf-8"),
        text=text,
        usage=usage,
        pages_processed=1,
    )
