"""Tests for ``mcc-mcp`` ``ocr_document`` tool derivation and execution."""

from pathlib import Path
from typing import Any

import httpx
import pytest

from my_claude_code.config.mcp_media import TOOL_OCR_DOCUMENT
from my_claude_code.config.settings import Settings
from my_claude_code.mcp_media.tools import derive_tools
from tests.mcp_media.test_round_trip import Rig, Upstream, _text

MISTRAL_KEY = "MistralFakeKey" + "0123456789abcdef" * 2
PDF_BYTES = b"%PDF-1.4\n%mcp-ocr\n"


def test_derive_tools_omits_ocr_document_when_rail_empty() -> None:
    catalogue = {
        "rails": [
            {"rail": "ocr", "label": "OCR", "operations": {"ocr": {"models": []}}}
        ],
        "data": [],
    }
    tools = derive_tools(catalogue)
    assert [tool.name for tool in tools] == ["media_status"]


def test_derive_tools_emits_ocr_document_when_ocr_configured() -> None:
    catalogue = {
        "rails": [
            {
                "rail": "ocr",
                "label": "OCR",
                "operations": {
                    "ocr": {
                        "endpoint": "/v1/ocr",
                        "models": ["mistral/mistral-ocr-latest"],
                        "fields": {
                            "file": {"required_by": [], "accepted_by": []},
                            "prompt": {
                                "not_forwarded_by": ["mistral/mistral-ocr-latest"]
                            },
                        },
                    }
                },
            }
        ],
        "data": [],
    }
    tools = derive_tools(catalogue)
    assert [tool.name for tool in tools] == ["media_status", TOOL_OCR_DOCUMENT]
    ocr_tool = tools[1]
    assert ocr_tool.spends is True
    assert ocr_tool.endpoint == "/v1/ocr"
    assert "file_path" in ocr_tool.definition["inputSchema"]["properties"]
    assert "file_path" in ocr_tool.definition["inputSchema"]["required"]
    assert "prompt" not in ocr_tool.definition["inputSchema"]["properties"]


def test_mcp_ocr_document_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))

    class _OcrUpstream(Upstream):
        def __init__(self) -> None:
            super().__init__()

        def handler(self, request: httpx.Request) -> httpx.Response:
            request.read()
            self.seen.append(request)
            return httpx.Response(
                200,
                json={
                    "model": "mistral-ocr-latest",
                    "pages": [{"index": 0, "markdown": "# MCP OCR Output"}],
                    "usage_info": {"pages_processed": 1},
                },
            )

    upstream = _OcrUpstream()
    settings = Settings.model_validate(
        {
            "MISTRAL_API_KEY": MISTRAL_KEY,
            "PROVIDER_RETRY_ATTEMPTS": "1",
            "MODEL_OCR": "mistral/mistral-ocr-latest",
        }
    )
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    pdf_file = work / "invoice.pdf"
    pdf_file.write_bytes(PDF_BYTES)

    rig = Rig(settings, upstream, work)
    with rig.client:
        result: Any = rig.call(TOOL_OCR_DOCUMENT, {"file_path": str(pdf_file)})

    assert result.is_error is False
    assert _text(result) == "# MCP OCR Output"
    assert "/v1/ocr" in rig.transport.paths
    assert len(upstream.seen) == 1
    assert upstream.seen[0].url.path == "/v1/ocr"
