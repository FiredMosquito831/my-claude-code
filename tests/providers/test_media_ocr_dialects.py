"""Tests for PR-M5 OCR codecs, provider surfaces, and off-loop adapters."""

import asyncio
import base64
import io
import json
from typing import Any

import pytest

from my_claude_code.application.media.request import (
    MediaAttempt,
    MediaRail,
    MediaRequest,
    MediaUpload,
)
from my_claude_code.application.routing import ResolvedModel
from my_claude_code.config.media_surfaces import (
    MEDIA_OPERATION_OCR,
    MEDIA_SHAPE_GEMINI_NATIVE_OCR,
    MEDIA_SHAPE_MISTRAL_OCR,
    MEDIA_SHAPE_VISION_OCR_CHAT,
    surface_for,
)
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.core.ocr_media import (
    DEFAULT_OCR_PROMPT,
    OcrAnswerError,
    gemini_ocr_request,
    mistral_ocr_request,
    parse_gemini_ocr_answer,
    parse_mistral_ocr_answer,
    parse_vision_chat_ocr_answer,
    vision_chat_ocr_request,
)
from my_claude_code.providers.media.adapters import build_wire_body

PDF_BYTES = b"%PDF-1.4\n%fake\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
PNG_B64 = base64.b64encode(PNG_BYTES).decode()
PDF_B64 = base64.b64encode(PDF_BYTES).decode()


def test_provider_catalog_declares_expected_ocr_surfaces() -> None:
    mistral = surface_for(
        PROVIDER_CATALOG["mistral"].media_surfaces, MEDIA_OPERATION_OCR
    )
    gemini = surface_for(PROVIDER_CATALOG["gemini"].media_surfaces, MEDIA_OPERATION_OCR)
    openai = surface_for(PROVIDER_CATALOG["openai"].media_surfaces, MEDIA_OPERATION_OCR)
    open_router = surface_for(
        PROVIDER_CATALOG["open_router"].media_surfaces, MEDIA_OPERATION_OCR
    )
    vibe = surface_for(
        PROVIDER_CATALOG["mistral_vibe"].media_surfaces, MEDIA_OPERATION_OCR
    )

    assert mistral is not None and mistral.shape == MEDIA_SHAPE_MISTRAL_OCR
    assert mistral.path == "ocr"
    assert gemini is not None and gemini.shape == MEDIA_SHAPE_GEMINI_NATIVE_OCR
    assert gemini.auth_header == "x-goog-api-key"
    assert openai is not None and openai.shape == MEDIA_SHAPE_VISION_OCR_CHAT
    assert open_router is not None and open_router.shape == MEDIA_SHAPE_VISION_OCR_CHAT
    assert vibe is None


def test_mistral_ocr_codec_round_trip() -> None:
    req = mistral_ocr_request(
        {
            "document": {
                "type": "document_url",
                "document_url": "https://x.test/a.pdf",
            },
            "pages": [0, 1],
            "include_image_base64": False,
        },
        model="mistral-ocr-latest",
    )
    assert req == {
        "model": "mistral-ocr-latest",
        "document": {"type": "document_url", "document_url": "https://x.test/a.pdf"},
        "pages": [0, 1],
        "include_image_base64": False,
    }

    img_req = mistral_ocr_request(
        {}, model="mistral-ocr-latest", data_uri=f"data:image/png;base64,{PNG_B64}"
    )
    assert img_req["document"] == {
        "type": "image_url",
        "image_url": f"data:image/png;base64,{PNG_B64}",
    }

    raw = json.dumps(
        {
            "model": "mistral-ocr-latest",
            "pages": [
                {"index": 0, "markdown": "# Page 1"},
                {"index": 1, "markdown": "Page 2 body"},
            ],
            "usage_info": {"pages_processed": 2, "doc_size_bytes": 1024},
        }
    ).encode("utf-8")
    parsed = parse_mistral_ocr_answer(raw)
    assert parsed.text == "# Page 1\n\nPage 2 body"
    assert parsed.pages_processed == 2
    body = json.loads(parsed.body)
    assert body["object"] == "ocr"
    assert body["model"] == "mistral-ocr-latest"
    assert len(body["pages"]) == 2
    assert body["usage_info"]["pages_processed"] == 2

    with pytest.raises(OcrAnswerError):
        parse_mistral_ocr_answer(b'{"pages": []}')


def test_gemini_native_ocr_codec_round_trip() -> None:
    req = gemini_ocr_request(
        {"output_format": "json"},
        data=PDF_BYTES,
        mime_type="application/pdf",
    )
    parts = req["contents"][0]["parts"]
    assert parts[0] == {"inlineData": {"mimeType": "application/pdf", "data": PDF_B64}}
    assert parts[1] == {"text": DEFAULT_OCR_PROMPT}
    assert req["generationConfig"] == {"responseMimeType": "application/json"}

    uri_req = gemini_ocr_request(
        {"document_url": f"data:image/png;base64,{PNG_B64}", "prompt": "Read receipt"}
    )
    assert uri_req["contents"][0]["parts"][0] == {
        "inlineData": {"mimeType": "image/png", "data": PNG_B64}
    }
    assert uri_req["contents"][0]["parts"][1] == {"text": "Read receipt"}

    raw = json.dumps(
        {
            "candidates": [
                {"content": {"parts": [{"text": "Invoice #42\nTotal: $10"}]}}
            ],
            "usageMetadata": {
                "promptTokenCount": 120,
                "candidatesTokenCount": 15,
                "totalTokenCount": 135,
            },
        }
    ).encode("utf-8")
    parsed = parse_gemini_ocr_answer(raw)
    assert parsed.text == "Invoice #42\nTotal: $10"
    assert parsed.usage == {
        "type": "tokens",
        "input_tokens": 120,
        "output_tokens": 15,
        "total_tokens": 135,
    }
    body = json.loads(parsed.body)
    assert body["pages"] == [{"index": 0, "markdown": "Invoice #42\nTotal: $10"}]

    with pytest.raises(OcrAnswerError):
        parse_gemini_ocr_answer(b'{"candidates": []}')


def test_vision_chat_ocr_codec_round_trip() -> None:
    img_req = vision_chat_ocr_request(
        {"prompt": "Extract text"},
        model="openai/gpt-4o",
        data_uri=f"data:image/png;base64,{PNG_B64}",
    )
    assert img_req["model"] == "openai/gpt-4o"
    assert img_req["messages"][0]["content"][1]["type"] == "image_url"

    pdf_req = vision_chat_ocr_request(
        {},
        model="openai/gpt-4o",
        data_uri=f"data:application/pdf;base64,{PDF_B64}",
        filename="scan.pdf",
    )
    assert pdf_req["messages"][0]["content"][1] == {
        "type": "file",
        "file": {
            "filename": "scan.pdf",
            "file_data": f"data:application/pdf;base64,{PDF_B64}",
        },
    }

    raw = json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": "Extracted VLM"}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
        }
    ).encode("utf-8")
    parsed = parse_vision_chat_ocr_answer(raw)
    assert parsed.text == "Extracted VLM"
    assert parsed.usage is not None
    assert parsed.usage["input_tokens"] == 50
    assert parsed.usage["output_tokens"] == 10

    with pytest.raises(OcrAnswerError):
        parse_vision_chat_ocr_answer(b'{"choices": [{"message": {"content": ""}}]}')


def test_ocr_adapters_offload_to_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real_to_thread = asyncio.to_thread

    async def spy_to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(getattr(func, "__name__", str(func)))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy_to_thread)

    async def run() -> None:
        upload = MediaUpload(
            field="file",
            filename="doc.pdf",
            content_type="application/pdf",
            size=len(PDF_BYTES),
            sha256="a" * 64,
            file=io.BytesIO(PDF_BYTES),
        )
        req = MediaRequest(
            operation=MEDIA_OPERATION_OCR,
            rail=MediaRail.OCR,
            model="",
            body={"pages": "0,2", "include_image_base64": "false"},
            uploads=(upload,),
            multipart=True,
        )
        for provider_id, model_id in (
            ("mistral", "mistral-ocr-latest"),
            ("gemini", "gemini-2.5-flash"),
            ("open_router", "openai/gpt-4o"),
        ):
            surface = surface_for(
                PROVIDER_CATALOG[provider_id].media_surfaces, MEDIA_OPERATION_OCR
            )
            assert surface is not None
            attempt = MediaAttempt(
                request=req,
                resolved=ResolvedModel(
                    original_model="",
                    provider_id=provider_id,
                    provider_model=model_id,
                    provider_model_ref=f"{provider_id}/{model_id}",
                    reasoning_preference=ReasoningPreference.INHERIT,
                ),
            )
            wire = await build_wire_body(surface, attempt)
            assert wire.encoded is not None
            assert "encoding:multipart->json" in wire.adaptations

    asyncio.run(run())
    assert calls == ["build_request_body", "build_request_body", "build_request_body"]
