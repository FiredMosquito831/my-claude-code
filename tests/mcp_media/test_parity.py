"""Comprehensive parity tests for ``mcc-mcp`` (PR 37 / v8.72.0)."""

import base64
import json
import sys
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from my_claude_code.config.env_files import clear_env_file_cache
from my_claude_code.config.mcp_media import (
    MCP_MEDIA_FREE_TOOLS,
    MCP_MEDIA_SPENDING_TOOLS,
    TOOL_CREATE_IMAGE_VARIATION,
    TOOL_DELETE_VIDEO,
    TOOL_EDIT_IMAGE,
    TOOL_EDIT_VIDEO,
    TOOL_GET_VIDEO,
    TOOL_LIST_VIDEOS,
    TOOL_LIST_VOICES,
    TOOL_MEDIA_STATUS,
    TOOL_OCR_DOCUMENT,
    opencode_permission_keys,
)
from my_claude_code.config.settings import Settings
from my_claude_code.mcp_media.client import MccAddress, MccClient, resolve_address
from my_claude_code.mcp_media.server import (
    ServerState,
    build_state,
    call_tool,
    load_catalogue,
)
from my_claude_code.mcp_media.tools import derive_tools
from tests.api.support import create_test_app
from tests.mcp_media.mcp_fake_mcc import MP4, PNG, FakeMcc
from tests.mcp_media.test_tools import PLAIN_KEYWORDS, _field, _keywords, _schema

pytestmark = pytest.mark.local_serial

TOKEN = "parity-token"


class _RouterTransport(httpx.AsyncBaseTransport):
    """Route ``MccClient`` requests to an in-memory handler and record every call."""

    def __init__(self, handler) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.requests.append(request)
        return self._handler(request)


def _full_catalogue() -> dict[str, Any]:
    img = "fake_img/artist"
    vid = "fake_vid/director"
    tts = "fake_tts/speaker"
    ocr = "fake_ocr/reader"
    return {
        "object": "list",
        "data": [
            {
                "id": img,
                "object": "media.model",
                "provider": "fake_img",
                "operations": {
                    "image_generate": {
                        "endpoint": "/v1/images/generations",
                        "request_schema": _schema(
                            img,
                            {"prompt": {"type": "string"}, "size": {"type": "string"}},
                        ),
                        "capabilities": {
                            "accepted_sizes": ["1024x1024", "1792x1024"],
                            "supports_streaming": False,
                        },
                    },
                    "image_edit": {
                        "endpoint": "/v1/images/edits",
                        "request_schema": _schema(
                            img,
                            {
                                "prompt": {"type": "string"},
                                "image": {"type": "string"},
                                "mask": {"type": "string"},
                            },
                        ),
                        "capabilities": {
                            "supports_mask": False,
                            "max_input_images": 4,
                            "refused_params": ["mask"],
                        },
                    },
                    "image_variation": {
                        "endpoint": "/v1/images/variations",
                        "request_schema": _schema(
                            img,
                            {
                                "image": {"type": "string"},
                                "n": {"type": "integer"},
                                "size": {"type": "string"},
                                "response_format": {"type": "string"},
                            },
                        ),
                        "capabilities": {"accepted_sizes": ["512x512", "1024x1024"]},
                    },
                },
            },
            {
                "id": tts,
                "object": "media.model",
                "provider": "fake_tts",
                "operations": {
                    "speech": {
                        "endpoint": "/v1/audio/speech",
                        "request_schema": _schema(
                            tts,
                            {"input": {"type": "string"}, "voice": {"type": "string"}},
                        ),
                        "capabilities": {"accepted_voices": ["alloy", "nova"]},
                    }
                },
            },
            {
                "id": vid,
                "object": "media.model",
                "provider": "fake_vid",
                "operations": {
                    "video_create": {
                        "endpoint": "/v1/videos",
                        "request_schema": _schema(
                            vid,
                            {
                                "prompt": {"type": "string"},
                                "seconds": {"type": "string"},
                            },
                        ),
                        "capabilities": {"accepted_durations": [5, 10]},
                    },
                    "video_edit": {
                        "endpoint": "/v1/videos/edits",
                        "request_schema": _schema(
                            vid,
                            {
                                "prompt": {"type": "string"},
                                "video": {"type": "string"},
                                "video_url": {"type": "string"},
                                "input_reference": {"type": "string"},
                            },
                        ),
                        "capabilities": {"accepted_durations": [5]},
                    },
                },
            },
            {
                "id": ocr,
                "object": "media.model",
                "provider": "fake_ocr",
                "operations": {
                    "ocr": {
                        "endpoint": "/v1/ocr",
                        "request_schema": _schema(
                            ocr,
                            {
                                "file": {"type": "string"},
                                "document_url": {"type": "string"},
                                "image_url": {"type": "string"},
                                "include_image_base64": {"type": "boolean"},
                            },
                        ),
                    }
                },
            },
        ],
        "rails": [
            {
                "rail": "image",
                "label": "Image",
                "models": [img],
                "operations": {
                    "image_generate": {
                        "endpoint": "/v1/images/generations",
                        "models": [img],
                        "fields": {
                            "prompt": _field(required=(img,)),
                            "size": _field(accepted=(img,)),
                        },
                    },
                    "image_edit": {
                        "endpoint": "/v1/images/edits",
                        "models": [img],
                        "fields": {
                            "prompt": _field(required=(img,)),
                            "image": _field(accepted=(img,)),
                            "mask": _field(accepted=(img,)),
                        },
                    },
                    "image_variation": {
                        "endpoint": "/v1/images/variations",
                        "models": [img],
                        "fields": {
                            "image": _field(required=(img,)),
                            "n": _field(accepted=(img,)),
                            "size": _field(accepted=(img,)),
                            "response_format": _field(accepted=(img,)),
                        },
                    },
                },
            },
            {
                "rail": "tts",
                "label": "Speech",
                "models": [tts],
                "operations": {
                    "speech": {
                        "endpoint": "/v1/audio/speech",
                        "voices_endpoint": "/v1/audio/voices",
                        "models": [tts],
                        "fields": {
                            "input": _field(required=(tts,)),
                            "voice": _field(accepted=(tts,)),
                        },
                    }
                },
            },
            {
                "rail": "video",
                "label": "Video",
                "models": [vid],
                "operations": {
                    "video_create": {
                        "endpoint": "/v1/videos",
                        "jobs_endpoint": "/v1/videos",
                        "delete_endpoint": "/v1/videos",
                        "models": [vid],
                        "fields": {
                            "prompt": _field(required=(vid,)),
                            "seconds": _field(accepted=(vid,)),
                        },
                    },
                    "video_edit": {
                        "endpoint": "/v1/videos/edits",
                        "models": [vid],
                        "fields": {
                            "prompt": _field(required=(vid,)),
                            "video": _field(accepted=(vid,)),
                            "video_url": _field(accepted=(vid,)),
                            "input_reference": _field(accepted=(vid,)),
                        },
                    },
                },
            },
            {
                "rail": "ocr",
                "label": "OCR",
                "models": [ocr],
                "operations": {
                    "ocr": {
                        "endpoint": "/v1/ocr",
                        "models": [ocr],
                        "fields": {
                            "file": _field(accepted=(ocr,)),
                            "document_url": _field(accepted=(ocr,)),
                            "image_url": _field(accepted=(ocr,)),
                            "include_image_base64": _field(accepted=(ocr,)),
                        },
                    }
                },
            },
        ],
    }


def _state(
    tmp_path: Path, handler, *, inline_images: bool = True
) -> tuple[ServerState, _RouterTransport]:
    transport = _RouterTransport(handler)
    address = MccAddress(
        root="http://127.0.0.1:18588",
        port_source="test",
        config_dir=str(tmp_path),
        token_set=False,
    )
    out = tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)
    state = ServerState(
        app_id="pytest_parity",
        address=address,
        client=MccClient(address, "", app_id="pytest_parity", transport=transport),
        cwd=out,
        out_dir=out,
        inline_images=inline_images,
    )
    return state, transport


def _text(result: Any) -> str:
    return "\n".join(
        block.text for block in result.content if getattr(block, "type", "") == "text"
    )


def test_derive_tools_full_parity_schemas_and_annotations() -> None:
    cat = _full_catalogue()
    derived = derive_tools(cat)
    by_name = {t.name: t for t in derived}

    expected_order = [
        TOOL_MEDIA_STATUS,
        "generate_image",
        TOOL_EDIT_IMAGE,
        TOOL_CREATE_IMAGE_VARIATION,
        "speak",
        TOOL_LIST_VOICES,
        "create_video",
        TOOL_EDIT_VIDEO,
        TOOL_OCR_DOCUMENT,
        TOOL_GET_VIDEO,
        TOOL_LIST_VIDEOS,
        TOOL_DELETE_VIDEO,
    ]
    assert [t.name for t in derived] == expected_order

    # R9 plain JSON Schema subset across every derived tool.
    for tool in derived:
        assert _keywords(tool.definition["inputSchema"]) <= PLAIN_KEYWORDS, tool.name
        dumped = json.dumps(tool.definition)
        assert "x-mcc" not in dumped

    # Annotations & OpenCode ask-gate checks.
    for name, tool in by_name.items():
        hints = tool.definition["annotations"]
        if name in MCP_MEDIA_SPENDING_TOOLS:
            assert hints["readOnlyHint"] is False
            assert hints["destructiveHint"] is False
        elif name in MCP_MEDIA_FREE_TOOLS:
            assert hints["readOnlyHint"] is True
        elif name == TOOL_DELETE_VIDEO:
            assert hints["readOnlyHint"] is False
            assert hints["destructiveHint"] is True
            assert f"mcc_media_{TOOL_DELETE_VIDEO}" in opencode_permission_keys()

    # image_variation schema & capability hints
    var_schema = by_name[TOOL_CREATE_IMAGE_VARIATION].definition["inputSchema"]
    assert "image_path" in var_schema["properties"]
    assert "image_path" in var_schema["required"]
    assert "response_format" not in var_schema["properties"]
    assert "512x512" in var_schema["properties"]["size"]["description"]

    # edit_image capability hints
    edit_props = by_name[TOOL_EDIT_IMAGE].definition["inputSchema"]["properties"]
    assert "mask not supported" in edit_props["mask_path"]["description"]
    assert (
        "max input images: fake_img/artist: 4"
        in edit_props["image_paths"]["description"]
    )

    # video_create, video_edit, get_video wait params + variant
    for vname in ("create_video", TOOL_EDIT_VIDEO, TOOL_GET_VIDEO):
        vprops = by_name[vname].definition["inputSchema"]["properties"]
        assert vprops["wait_seconds"]["type"] == "integer"
        assert vprops["poll_interval_seconds"]["type"] == "integer"
    assert "variant" in by_name[TOOL_GET_VIDEO].definition["inputSchema"]["properties"]
    ve_props = by_name[TOOL_EDIT_VIDEO].definition["inputSchema"]["properties"]
    assert "video_path" in ve_props
    assert "input_reference_path" in ve_props
    assert ve_props["video_url"]["type"] == "string"

    # ocr_document allows URL without requiring file_path when document_url is present
    ocr_schema = by_name[TOOL_OCR_DOCUMENT].definition["inputSchema"]
    assert "file_path" not in ocr_schema.get("required", [])
    assert "save_to" in ocr_schema["properties"]
    assert ocr_schema["properties"]["save_images"]["type"] == "boolean"

    # media_status has optional model and operation filters
    status_props = by_name[TOOL_MEDIA_STATUS].definition["inputSchema"]["properties"]
    assert set(status_props) == {"model", "operation"}
    assert "required" not in by_name[TOOL_MEDIA_STATUS].definition["inputSchema"]


def test_variation_and_edit_image_required_file_validation(tmp_path: Path) -> None:
    cat = _full_catalogue()
    img_file = tmp_path / "source.png"
    img_file.write_bytes(PNG)
    mask_file = tmp_path / "mask.png"
    mask_file.write_bytes(PNG)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/media/models":
            return httpx.Response(200, json=cat)
        if request.url.path == "/v1/images/variations":
            return httpx.Response(
                200,
                json={
                    "created": 1,
                    "data": [{"b64_json": base64.b64encode(PNG).decode()}],
                },
            )
        return httpx.Response(404, json={"error": {"message": "not found"}})

    state, transport = _state(tmp_path, handler)

    async def run() -> tuple[Any, Any, Any]:
        await load_catalogue(state)
        bad_edit = await call_tool(
            state, TOOL_EDIT_IMAGE, {"prompt": "add hat", "mask_path": str(mask_file)}
        )
        bad_var = await call_tool(state, TOOL_CREATE_IMAGE_VARIATION, {})
        ok_var = await call_tool(
            state, TOOL_CREATE_IMAGE_VARIATION, {"image_path": str(img_file)}
        )
        return bad_edit, bad_var, ok_var

    bad_edit, bad_var, ok_var = anyio.run(run)
    assert bad_edit.is_error is True
    assert "image_paths" in _text(bad_edit)
    assert bad_var.is_error is True
    assert "image_path" in _text(bad_var)
    assert ok_var.is_error is False
    assert ok_var.structured_content["saved_count"] == 1
    saved = Path(ok_var.structured_content["paths"][0])
    assert saved.read_bytes() == PNG
    assert any(r.url.path == "/v1/images/variations" for r in transport.requests)


def test_inline_images_env_disable_and_n_gt_2_cap(tmp_path: Path) -> None:
    cat = _full_catalogue()
    encoded = base64.b64encode(PNG).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/media/models":
            return httpx.Response(200, json=cat)
        if request.url.path == "/v1/images/generations":
            return httpx.Response(
                200,
                json={"created": 1, "data": [{"b64_json": encoded} for _ in range(4)]},
            )
        return httpx.Response(404, json={"error": {"message": "not found"}})

    # n > 2 cap keeps all 4 on disk and caps inline ImageContent at 2.
    state_on, _ = _state(tmp_path / "cap", handler, inline_images=True)

    async def run_on() -> Any:
        await load_catalogue(state_on)
        return await call_tool(
            state_on, "generate_image", {"prompt": "four kites", "n": 4}
        )

    res_on = anyio.run(run_on)
    assert res_on.is_error is False
    assert res_on.structured_content["saved_count"] == 4
    assert len(res_on.structured_content["paths"]) == 4
    for p in res_on.structured_content["paths"]:
        assert Path(p).read_bytes() == PNG
    images_on = [b for b in res_on.content if getattr(b, "type", "") == "image"]
    assert len(images_on) == 2

    # MCC_MCP_INLINE_IMAGES=0 disables inline ImageContent completely.
    state_off, _ = _state(tmp_path / "off", handler, inline_images=False)

    async def run_off() -> Any:
        await load_catalogue(state_off)
        return await call_tool(
            state_off, "generate_image", {"prompt": "four kites", "n": 4}
        )

    res_off = anyio.run(run_off)
    assert res_off.is_error is False
    assert res_off.structured_content["saved_count"] == 4
    images_off = [b for b in res_off.content if getattr(b, "type", "") == "image"]
    assert images_off == []


def test_video_edit_wait_fast_complete_timeout_variant_list_and_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cat = _full_catalogue()
    vid_file = tmp_path / "clip.mp4"
    vid_file.write_bytes(MP4)
    polls = {"job_fast": 0, "job_slow": 0}
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(anyio, "sleep", fake_sleep)
    clock = {"now": 100.0}

    def fake_time() -> float:
        return clock["now"]

    monkeypatch.setattr(anyio, "current_time", fake_time)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/media/models":
            return httpx.Response(200, json=cat)
        if request.method == "POST" and path == "/v1/videos/edits":
            return httpx.Response(202, json={"id": "job_fast", "status": "queued"})
        if request.method == "POST" and path == "/v1/videos":
            return httpx.Response(202, json={"id": "job_slow", "status": "queued"})
        if request.method == "GET" and path == "/v1/videos/job_fast":
            polls["job_fast"] += 1
            return httpx.Response(200, json={"id": "job_fast", "status": "completed"})
        if request.method == "GET" and path == "/v1/videos/job_fast/content":
            return httpx.Response(
                200, content=MP4, headers={"content-type": "video/mp4"}
            )
        if request.method == "GET" and path == "/v1/videos/job_slow":
            polls["job_slow"] += 1
            clock["now"] += 2.0
            return httpx.Response(
                200, json={"id": "job_slow", "status": "in_progress", "progress": 40}
            )
        if request.method == "GET" and path == "/v1/videos":
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": "job_fast",
                            "status": "completed",
                            "model": "fake_vid/director",
                        }
                    ],
                    "has_more": False,
                },
            )
        if request.method == "DELETE" and path == "/v1/videos/job_fast":
            return httpx.Response(
                200, json={"id": "job_fast", "object": "video.deleted", "deleted": True}
            )
        return httpx.Response(404, json={"error": {"message": "not found"}})

    state, transport = _state(tmp_path, handler)

    async def run() -> tuple[Any, Any, Any, Any, Any]:
        await load_catalogue(state)
        edited = await call_tool(
            state,
            TOOL_EDIT_VIDEO,
            {
                "prompt": "make brighter",
                "video_path": str(vid_file),
                "wait_seconds": 10,
                "poll_interval_seconds": 2,
            },
        )
        slow = await call_tool(
            state,
            "create_video",
            {"prompt": "slow clip", "wait_seconds": 2, "poll_interval_seconds": 1},
        )
        variant_res = await call_tool(
            state,
            TOOL_GET_VIDEO,
            {"video_id": "job_fast", "variant": "thumbnail"},
        )
        listed = await call_tool(state, TOOL_LIST_VIDEOS, {"limit": 5, "order": "desc"})
        deleted = await call_tool(state, TOOL_DELETE_VIDEO, {"video_id": "job_fast"})
        return edited, slow, variant_res, listed, deleted

    edited, slow, variant_res, listed, deleted = anyio.run(run)

    # edit_video polled /v1/videos/job_fast and saved MP4!
    assert edited.is_error is False, _text(edited)
    assert edited.structured_content["status"] == "completed"
    assert Path(edited.structured_content["paths"][0]).read_bytes() == MP4
    assert polls["job_fast"] >= 1

    # bounded timeout returns non-error progress + structured next_tool
    assert slow.is_error is False
    assert slow.structured_content["job_id"] == "job_slow"
    assert slow.structured_content["status"] == "in_progress"
    assert slow.structured_content["progress"] == 40
    assert slow.structured_content["next_tool"] == TOOL_GET_VIDEO
    assert "job_slow" in state.log.video_jobs

    # variant query param forwarded to /content
    assert variant_res.is_error is False
    content_reqs = [
        r for r in transport.requests if r.url.path == "/v1/videos/job_fast/content"
    ]
    assert any("variant=thumbnail" in str(r.url.query) for r in content_reqs)

    # list_videos & delete_video
    assert listed.is_error is False
    assert "job_fast" in _text(listed)
    list_req = next(
        r
        for r in transport.requests
        if r.method == "GET" and r.url.path == "/v1/videos"
    )
    assert "limit=5" in str(list_req.url.query)
    assert deleted.is_error is False
    assert deleted.structured_content["deleted"] is True
    assert "job_fast" not in state.log.video_jobs


def test_list_voices_and_media_status_inspect_zero_spend_and_refused_hint(
    tmp_path: Path,
) -> None:
    cat = _full_catalogue()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/media/models":
            return httpx.Response(200, json=cat)
        if request.url.path == "/v1/audio/voices":
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "voices": [
                        {
                            "id": "alloy",
                            "model_ref": "fake_tts/speaker",
                            "source": "stated",
                        }
                    ],
                },
            )
        if request.url.path == "/v1/images/generations":
            return httpx.Response(
                400,
                json={"error": {"message": "unsupported size", "param": "size"}},
            )
        return httpx.Response(401, json={"error": {"message": "bad token"}})

    state, transport = _state(tmp_path, handler)

    async def run() -> tuple[Any, Any, Any, Any]:
        await load_catalogue(state)
        before_inspect = len(transport.requests)
        inspected = await call_tool(
            state,
            TOOL_MEDIA_STATUS,
            {"model": "fake_img/artist", "operation": "image_edit"},
        )
        after_inspect = len(transport.requests)
        assert before_inspect == after_inspect  # zero network calls!
        voices = await call_tool(state, TOOL_LIST_VOICES, {"provider": "fake_tts"})
        refused_400 = await call_tool(
            state,
            "generate_image",
            {"prompt": "kite", "model": "fake_img/artist", "size": "9x9"},
        )
        refused_401 = await call_tool(state, "speak", {"input": "hello"})
        return inspected, voices, refused_400, refused_401

    inspected, voices, refused_400, refused_401 = anyio.run(run)
    assert "supports_mask: False" in _text(inspected)
    assert "max_input_images: 4" in _text(inspected)
    assert voices.is_error is False
    assert "alloy" in _text(voices)
    assert refused_400.is_error is True
    assert "Hint: call media_status with operation='image_generate'" in _text(
        refused_400
    )
    assert "1024x1024" in _text(refused_400)
    assert refused_401.is_error is True
    assert "Hint:" not in _text(refused_401)


def test_ocr_url_without_file_path_save_to_and_extracted_images(tmp_path: Path) -> None:
    cat = _full_catalogue()
    encoded_img = "data:image/png;base64," + base64.b64encode(PNG).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/media/models":
            return httpx.Response(200, json=cat)
        if request.url.path == "/v1/ocr":
            return httpx.Response(
                200,
                json={
                    "pages": [
                        {
                            "index": 0,
                            "markdown": "# Extracted Page",
                            "images": [{"id": "fig-1", "image_base64": encoded_img}],
                        }
                    ]
                },
            )
        return httpx.Response(404, json={"error": {"message": "not found"}})

    state, _transport = _state(tmp_path, handler)

    async def run() -> tuple[Any, Any, Any]:
        await load_catalogue(state)
        missing = await call_tool(state, TOOL_OCR_DOCUMENT, {})
        url_saved = await call_tool(
            state,
            TOOL_OCR_DOCUMENT,
            {
                "document_url": "https://example.test/doc.pdf",
                "include_image_base64": True,
                "save_to": "ocr_out/report.md",
            },
        )
        no_save_imgs = await call_tool(
            state,
            TOOL_OCR_DOCUMENT,
            {
                "document_url": "https://example.test/doc.pdf",
                "save_images": False,
            },
        )
        return missing, url_saved, no_save_imgs

    missing, url_saved, no_save_imgs = anyio.run(run)
    assert missing.is_error is True
    assert url_saved.is_error is False
    paths = [Path(p) for p in url_saved.structured_content["paths"]]
    assert len(paths) == 2
    assert (
        paths[0].name == "report.md"
        and paths[0].read_text("utf-8") == "# Extracted Page"
    )
    assert paths[1].name == "fig-1.png" and paths[1].read_bytes() == PNG
    assert "# Extracted Page" in _text(url_saved)
    assert no_save_imgs.is_error is False
    assert _text(no_save_imgs) == "# Extracted Page"
    assert no_save_imgs.structured_content["paths"] == []


def test_env_overrides_base_url_output_dir_and_inline_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / ".env").write_text(
        "HOST=127.0.0.1\nPORT=19001\nANTHROPIC_AUTH_TOKEN=dot-env-tok\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MCC_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("PORT", "3000")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "wrong-tok")

    # Child env MCC_BASE_URL works while token still comes from .env (R14)
    settings = Settings.model_validate(
        {
            "HOST": "127.0.0.1",
            "PORT": "19001",
            "ANTHROPIC_AUTH_TOKEN": "dot-env-tok",
        }
    )
    addr, tok = resolve_address(
        {"MCC_BASE_URL": "https://proxy.example.test/mcc/"}, settings
    )
    assert addr.root == "https://proxy.example.test/mcc"
    assert addr.port_source == "MCC_BASE_URL"
    assert tok == "dot-env-tok"

    with pytest.raises(ValueError, match="http:// or https://"):
        resolve_address({"MCC_BASE_URL": "ftp://invalid.test"}, settings)

    # .env MCC_BASE_URL beats child env MCC_BASE_URL
    (cfg / ".env").write_text(
        "MCC_BASE_URL=https://dotenv.example.test/\nANTHROPIC_AUTH_TOKEN=dot-env-tok\n",
        encoding="utf-8",
    )
    clear_env_file_cache()
    monkeypatch.setattr(
        Settings, "model_config", {**Settings.model_config, "env_file": cfg / ".env"}
    )
    addr_env, _ = resolve_address(
        {"MCC_BASE_URL": "http://child.example.test"}, Settings.model_validate({})
    )
    assert addr_env.root == "https://dotenv.example.test"

    # build_state respects MCC_MEDIA_OUTPUT_DIR unless --out is passed, and MCC_MCP_INLINE_IMAGES
    custom_out = tmp_path / "custom-out"
    cli_out = tmp_path / "cli-out"
    s1 = build_state(
        app_id="app",
        out_dir=None,
        environ={"MCC_MEDIA_OUTPUT_DIR": str(custom_out), "MCC_MCP_INLINE_IMAGES": "0"},
        cwd=tmp_path,
    )
    assert s1.out_dir == custom_out.resolve()
    assert s1.inline_images is False
    s2 = build_state(
        app_id="app",
        out_dir=str(cli_out),
        environ={
            "MCC_MEDIA_OUTPUT_DIR": str(custom_out),
            "MCC_MCP_INLINE_IMAGES": "true",
        },
        cwd=tmp_path,
    )
    assert s2.out_dir == cli_out.resolve()
    assert s2.inline_images is True


def test_stdio_full_parity_session(tmp_path: Path) -> None:
    fake = FakeMcc(token=TOKEN).start()
    try:
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        (cfg / ".env").write_text(
            f"HOST=127.0.0.1\nPORT={fake.port}\nANTHROPIC_AUTH_TOKEN={TOKEN}\n",
            encoding="utf-8",
        )
        out_dir = tmp_path / "env_out"
        work = tmp_path / "work"
        work.mkdir()
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "my_claude_code.mcp_media.server", "--app", "stdio_parity"],
            env={
                "MCC_CONFIG_DIR": str(cfg),
                "MCC_MEDIA_OUTPUT_DIR": str(out_dir),
                "MCC_MCP_INLINE_IMAGES": "0",
            },
            cwd=str(work),
        )

        async def run() -> tuple[Any, Any]:
            async with Client(params, mode="legacy") as client:
                img = await client.call_tool("generate_image", {"prompt": "red kite"})
                vid = await client.call_tool(
                    "create_video",
                    {
                        "prompt": "red kite",
                        "wait_seconds": 5,
                        "poll_interval_seconds": 1,
                    },
                )
                return img, vid

        img, vid = anyio.run(run)
        assert img.is_error is False
        # MCC_MCP_INLINE_IMAGES=0 -> no ImageContent returned over stdio
        assert [b for b in img.content if getattr(b, "type", "") == "image"] == []
        # MCC_MEDIA_OUTPUT_DIR used as output folder
        assert img.structured_content["paths"][0].startswith(str(out_dir.resolve()))
        assert vid.is_error is False
        assert vid.structured_content["status"] == "completed"
        assert Path(vid.structured_content["paths"][0]).read_bytes() == MP4
    finally:
        fake.stop()


def test_real_media_catalogue_derives_full_parity_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    settings = Settings.model_validate(
        {
            "ANTHROPIC_AUTH_TOKEN": TOKEN,
            "XAI_API_KEY": "xai-" + "a" * 40,
            "GEMINI_API_KEY": "gm-" + "b" * 40,
            "OPENROUTER_API_KEY": "sk-or-" + "c" * 40,
            "MISTRAL_API_KEY": "ms-" + "d" * 40,
            "MODEL_IMAGE": "xai/grok-2-image",
            "MODEL_TTS": "gemini/gemini-2.5-flash-preview-tts",
            "MODEL_ASR": "gemini/gemini-2.5-flash",
            "MODEL_VIDEO": "open_router/google/veo-3.1",
            "MODEL_OCR": "mistral/mistral-ocr-latest",
        }
    )
    with TestClient(create_test_app(settings)) as client:
        resp = client.get(
            "/v1/media/models", headers={"Authorization": f"Bearer {TOKEN}"}
        )
    assert resp.status_code == 200
    derived_names = [t.name for t in derive_tools(resp.json())]
    for expected in (
        TOOL_MEDIA_STATUS,
        "generate_image",
        TOOL_EDIT_IMAGE,
        TOOL_CREATE_IMAGE_VARIATION,
        "speak",
        TOOL_LIST_VOICES,
        "transcribe",
        "translate_audio",
        "create_video",
        TOOL_EDIT_VIDEO,
        TOOL_OCR_DOCUMENT,
        TOOL_GET_VIDEO,
        TOOL_LIST_VIDEOS,
        TOOL_DELETE_VIDEO,
    ):
        assert expected in derived_names
