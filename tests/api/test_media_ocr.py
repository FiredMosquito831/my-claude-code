"""End-to-end API tests for ``POST /v1/ocr`` on the OCR rail."""

import asyncio
import base64
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.api import media_routes
from my_claude_code.config.settings import Settings
from my_claude_code.core import ocr_media, request_log
from my_claude_code.providers.media.registry import MediaRegistry
from tests.api.support import create_test_app, requests_as_read

MISTRAL_KEY = "MistralFakeKey" + "0123456789abcdef" * 2
GEMINI_KEY = "AIza" + "ef56" * 9
OPENROUTER_KEY = "sk-or-v1-" + "ab12" * 12

PDF_BYTES = b"%PDF-1.4\n%test\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def _settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **extra: str
) -> Settings:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    base = {
        "MISTRAL_API_KEY": MISTRAL_KEY,
        "GEMINI_API_KEY": GEMINI_KEY,
        "OPENROUTER_API_KEY": OPENROUTER_KEY,
        "PROVIDER_RETRY_ATTEMPTS": "1",
    }
    base.update(extra)
    return Settings.model_validate(base)


def _read_rows(tmp_path: Path) -> list[dict[str, Any]]:
    request_log.reset_request_log_stores()
    conn = sqlite3.connect(tmp_path / "requests.db")
    conn.row_factory = sqlite3.Row
    try:
        return requests_as_read(
            conn, conn.execute("SELECT * FROM requests ORDER BY ts_epoch").fetchall()
        )
    finally:
        conn.close()


def test_ocr_probe_and_validation_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(monkeypatch, tmp_path)
    registry = MediaRegistry(
        transport=httpx.MockTransport(lambda req: httpx.Response(500))
    )
    with TestClient(create_test_app(settings, media=registry)) as client:
        for method in ("HEAD", "OPTIONS"):
            probe = client.request(method, "/v1/ocr")
            assert probe.status_code == 204
            assert probe.headers["Allow"] == "POST, HEAD, OPTIONS"

        empty = client.post(
            "/v1/ocr",
            json={
                "document": {
                    "type": "document_url",
                    "document_url": "https://x.test/a.pdf",
                }
            },
        )
        assert empty.status_code == 404
        assert "MODEL_OCR" in empty.json()["error"]["message"]

        bad_json = client.post("/v1/ocr", json={})
        assert bad_json.status_code == 400
        assert "error" in bad_json.json()

        bad_form = client.post(
            "/v1/ocr",
            files=[("other", ("note.txt", b"hi", "text/plain"))],
        )
        assert bad_form.status_code == 400
        assert "file" in bad_form.json()["error"]["message"]


def test_ocr_mistral_json_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "model": "mistral-ocr-latest",
                "pages": [{"index": 0, "markdown": "# Receipt\n\nTotal: $12.00"}],
                "usage_info": {"pages_processed": 1, "doc_size_bytes": 512},
            },
        )

    settings = _settings(
        monkeypatch,
        tmp_path,
        MODEL_OCR="mistral/mistral-ocr-latest",
    )
    registry = MediaRegistry(transport=httpx.MockTransport(handler))
    with TestClient(create_test_app(settings, media=registry)) as client:
        resp = client.post(
            "/v1/ocr",
            json={
                "document": {
                    "type": "document_url",
                    "document_url": "https://example.test/receipt.pdf",
                },
                "pages": [0],
            },
        )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["object"] == "ocr"
    assert payload["text"] == "# Receipt\n\nTotal: $12.00"
    assert payload["pages"][0]["markdown"] == "# Receipt\n\nTotal: $12.00"
    assert len(seen) == 1
    assert seen[0].url.path == "/v1/ocr"
    assert seen[0].headers["authorization"] == f"Bearer {MISTRAL_KEY}"
    sent_body = json.loads(seen[0].content)
    assert sent_body["model"] == "mistral-ocr-latest"
    assert sent_body["pages"] == [0]

    rows = _read_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["media_operation"] == "ocr"
    assert rows[0]["status"] == "success"
    assert rows[0]["output_chars"] == len("# Receipt\n\nTotal: $12.00")


def test_ocr_gemini_multipart_and_vlm_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        body = json.loads(request.content)
        calls.append((request.url.host, request.url.path, body))
        if request.url.host == "generativelanguage.googleapis.com":
            return httpx.Response(500, json={"error": {"message": "gemini down"}})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "Fallback OCR"}}
                ],
                "usage": {
                    "prompt_tokens": 42,
                    "completion_tokens": 8,
                    "total_tokens": 50,
                },
            },
        )

    settings = _settings(
        monkeypatch,
        tmp_path,
        MODEL_OCR="gemini/gemini-2.5-flash",
        MODEL_OCR_FALLBACKS="open_router/openai/gpt-4o",
    )
    registry = MediaRegistry(transport=httpx.MockTransport(handler))
    with TestClient(create_test_app(settings, media=registry)) as client:
        resp = client.post(
            "/v1/ocr",
            data={"prompt": "Extract everything"},
            files=[("file", ("scan.png", PNG_BYTES, "image/png"))],
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["text"] == "Fallback OCR"
    assert data["usage"]["input_tokens"] == 42
    assert len(calls) == 2
    assert calls[0][0] == "generativelanguage.googleapis.com"
    assert ":generateContent" in calls[0][1]
    assert calls[0][2]["contents"][0]["parts"][0] == {
        "inlineData": {
            "mimeType": "image/png",
            "data": base64.b64encode(PNG_BYTES).decode(),
        }
    }
    assert calls[1][0] == "openrouter.ai"
    assert calls[1][1] == "/api/v1/chat/completions"
    assert calls[1][2]["messages"][0]["content"][1]["type"] == "image_url"


def test_ocr_pinned_model_off_rail_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(monkeypatch, tmp_path, MODEL_OCR="mistral/mistral-ocr-latest")
    registry = MediaRegistry(
        transport=httpx.MockTransport(lambda req: httpx.Response(500))
    )
    with TestClient(create_test_app(settings, media=registry)) as client:
        resp = client.post(
            "/v1/ocr",
            json={
                "model": "gemini/gemini-2.5-flash",
                "document_url": "https://example.test/doc.pdf",
            },
        )
    assert resp.status_code == 400
    assert "not on the OCR rail" in resp.json()["error"]["message"]


def test_ocr_hashing_and_parsing_run_off_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    def off_loop(name: str, real: Any) -> Any:
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                calls.append(name)
                return real(*args, **kwargs)
            raise AssertionError(f"{name} ran on event loop")

        return wrapped

    monkeypatch.setattr(
        media_routes, "_measure", off_loop("measure", media_routes._measure)
    )
    monkeypatch.setattr(
        media_routes, "_parse_ocr", off_loop("parse_ocr", media_routes._parse_ocr)
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": "Off-loop text"}]}}],
            },
        )

    settings = _settings(monkeypatch, tmp_path, MODEL_OCR="gemini/gemini-2.5-flash")
    registry = MediaRegistry(transport=httpx.MockTransport(handler))
    with TestClient(create_test_app(settings, media=registry)) as client:
        resp = client.post(
            "/v1/ocr",
            files=[("file", ("a.pdf", PDF_BYTES, "application/pdf"))],
        )
    assert resp.status_code == 200
    assert calls == ["measure", "parse_ocr"]
    assert ocr_media.DEFAULT_OCR_PROMPT
