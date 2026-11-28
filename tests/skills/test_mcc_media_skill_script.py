"""Unit and fake-server integration tests for ``skills/mcc-media/scripts/``."""

import base64
import importlib.util
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_SCRIPT = REPO_ROOT / "skills" / "mcc-media" / "scripts" / "_mcc_env.py"
CLI_SCRIPT = REPO_ROOT / "skills" / "mcc-media" / "scripts" / "mcc_media.py"

PNG = b"\x89PNG\r\n\x1a\n" + b"\x08" * 32
MP3 = b"ID3\x04" + b"\x02" * 24
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x03" * 24
PDF = b"%PDF-1.4\n%skill-test\n"


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeMediaServer:
    def __init__(self, token: str = "secret-tok") -> None:
        self.token = token
        self.seen: list[tuple[str, str, dict[str, str], bytes]] = []
        self.video_polls = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> _FakeMediaServer:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj: object) -> None:
                self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

            def _dispatch(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                headers = {k.lower(): v for k, v in self.headers.items()}
                owner.seen.append((method, self.path, headers, body))
                if (
                    owner.token
                    and headers.get("authorization") != f"Bearer {owner.token}"
                ):
                    return self._json(401, {"error": {"message": "unauthorized"}})

                path_only = self.path.split("?", 1)[0]
                if method == "GET" and path_only == "/v1/media/models":
                    return self._json(
                        200,
                        {
                            "data": [
                                {
                                    "id": "fake/img",
                                    "provider": "fake",
                                    "operations": {
                                        "image_generate": {
                                            "endpoint": "/v1/images/generations",
                                            "capabilities": {
                                                "accepted_sizes": ["1024x1024"]
                                            },
                                            "request_schema": {
                                                "required": ["prompt"],
                                                "properties": {
                                                    "prompt": {"type": "string"}
                                                },
                                            },
                                        }
                                    },
                                }
                            ],
                            "rails": [
                                {
                                    "rail": "image",
                                    "label": "Image",
                                    "models": ["fake/img"],
                                    "operations": {
                                        "image_generate": {
                                            "endpoint": "/v1/images/generations",
                                            "models": ["fake/img"],
                                        }
                                    },
                                }
                            ],
                        },
                    )
                if method == "GET" and path_only == "/v1/audio/voices":
                    return self._json(
                        200,
                        {
                            "object": "list",
                            "voices": [
                                {
                                    "id": "alloy",
                                    "model_ref": "fake/tts",
                                    "source": "stated",
                                }
                            ],
                        },
                    )
                if method == "POST" and path_only in (
                    "/v1/images/generations",
                    "/v1/images/edits",
                    "/v1/images/variations",
                ):
                    return self._json(
                        200,
                        {
                            "created": 1,
                            "data": [{"b64_json": base64.b64encode(PNG).decode()}],
                        },
                    )
                if method == "POST" and path_only == "/v1/audio/speech":
                    return self._send(200, MP3, "audio/mpeg")
                if method == "POST" and path_only in (
                    "/v1/audio/transcriptions",
                    "/v1/audio/translations",
                ):
                    label = (
                        "translated" if "translations" in path_only else "transcribed"
                    )
                    return self._json(200, {"text": f"{label} text"})
                if method == "POST" and path_only in ("/v1/videos", "/v1/videos/edits"):
                    jid = "vid_edit_1" if path_only.endswith("/edits") else "vid_1"
                    return self._json(202, {"id": jid, "status": "queued"})
                if method == "GET" and path_only in (
                    "/v1/videos/vid_1",
                    "/v1/videos/vid_edit_1",
                ):
                    owner.video_polls += 1
                    return self._json(
                        200, {"id": path_only.rsplit("/", 1)[-1], "status": "completed"}
                    )
                if method == "GET" and path_only in (
                    "/v1/videos/vid_1/content",
                    "/v1/videos/vid_edit_1/content",
                ):
                    return self._send(200, MP4, "video/mp4")
                if method == "GET" and path_only == "/v1/videos":
                    return self._json(
                        200,
                        {
                            "object": "list",
                            "data": [{"id": "vid_1", "status": "completed"}],
                        },
                    )
                if method == "DELETE" and path_only == "/v1/videos/vid_1":
                    return self._json(200, {"id": "vid_1", "deleted": True})
                if method == "POST" and path_only == "/v1/ocr":
                    return self._json(
                        200,
                        {
                            "pages": [
                                {
                                    "index": 0,
                                    "markdown": "# OCR Markdown",
                                    "images": [
                                        {
                                            "id": "page0-fig1",
                                            "image_base64": "data:image/png;base64,"
                                            + base64.b64encode(PNG).decode(),
                                        }
                                    ],
                                }
                            ]
                        },
                    )
                return self._json(404, {"error": {"message": "not found"}})

            def do_GET(self) -> None:
                self._dispatch("GET")

            def do_POST(self) -> None:
                self._dispatch("POST")

            def do_DELETE(self) -> None:
                self._dispatch("DELETE")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(5)


def test_r14_env_precedence_legacy_fallback_and_missing_config(tmp_path: Path) -> None:
    env_mod = _load_module("_mcc_env_test", ENV_SCRIPT)
    home = tmp_path / "home"
    fcc = home / ".fcc"
    fcc.mkdir(parents=True)
    (fcc / ".env").write_text(
        "HOST=127.0.0.1\nPORT=19002\nANTHROPIC_AUTH_TOKEN=fcc-tok\n",
        encoding="utf-8",
    )

    # Legacy ~/.fcc fallback when ~/.mcc does not exist, beating inherited PORT=3000 (R14).
    cfg = env_mod.resolve_mcc_config(
        environ={"PORT": "3000", "ANTHROPIC_AUTH_TOKEN": "wrong"},
        home=home,
        cwd=tmp_path,
    )
    assert cfg.base_url == "http://127.0.0.1:19002"
    assert cfg.token == "fcc-tok"
    assert "fcc-tok" not in repr(cfg)

    # Once ~/.mcc exists, it wins over ~/.fcc.
    mcc = home / ".mcc"
    mcc.mkdir()
    (mcc / ".env").write_text(
        "MCC_BASE_URL=https://mcc.local:8443/\nANTHROPIC_AUTH_TOKEN=mcc-tok\n",
        encoding="utf-8",
    )
    cfg2 = env_mod.resolve_mcc_config(
        environ={"MCC_BASE_URL": "http://wrong.test", "PORT": "3000"},
        home=home,
        cwd=tmp_path,
    )
    assert cfg2.base_url == "https://mcc.local:8443"
    assert cfg2.token == "mcc-tok"

    # CLI flags beat .env.
    cfg3 = env_mod.resolve_mcc_config(
        base_url="http://127.0.0.1:18589/",
        token="cli-tok",
        environ={"PORT": "3000"},
        home=home,
        cwd=tmp_path,
    )
    assert cfg3.base_url == "http://127.0.0.1:18589"
    assert cfg3.token == "cli-tok"

    # Missing config fails fast with actionable message naming mcc-init and mcc-server.
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    with pytest.raises(env_mod.MccEnvError) as err:
        env_mod.resolve_mcc_config(
            environ={"PORT": "3000"}, home=empty_home, cwd=tmp_path
        )
    msg = str(err.value)
    assert "mcc-init" in msg and "mcc-server" in msg


@pytest.mark.spawns_process
@pytest.mark.local_serial
def test_skill_cli_missing_config_exits_2(tmp_path: Path) -> None:
    empty = tmp_path / "no-cfg"
    empty.mkdir()
    proc = subprocess.run(
        [sys.executable, str(CLI_SCRIPT), "--config-dir", str(empty), "models"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "mcc-init" in proc.stderr and "mcc-server" in proc.stderr


def test_all_14_skill_subcommands_against_fake_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli_mod = _load_module("mcc_media_skill_test", CLI_SCRIPT)
    server = _FakeMediaServer(token="skill-secret").start()
    try:
        cfg_dir = tmp_path / "mcc"
        cfg_dir.mkdir()
        out_dir = tmp_path / "out"
        (cfg_dir / ".env").write_text(
            f"MCC_BASE_URL={server.url}\n"
            f"ANTHROPIC_AUTH_TOKEN=skill-secret\n"
            f"MCC_MEDIA_OUTPUT_DIR={out_dir}\n",
            encoding="utf-8",
        )
        img_in = tmp_path / "in.png"
        img_in.write_bytes(PNG)
        mask_in = tmp_path / "mask.png"
        mask_in.write_bytes(PNG)
        aud_in = tmp_path / "in.mp3"
        aud_in.write_bytes(MP3)
        vid_in = tmp_path / "in.mp4"
        vid_in.write_bytes(MP4)
        pdf_in = tmp_path / "in.pdf"
        pdf_in.write_bytes(PDF)

        monkeypatch.setenv("MCC_CONFIG_DIR", str(cfg_dir))
        monkeypatch.setattr("time.sleep", lambda _s: None)

        def run_cli(*argv: str) -> dict[str, Any]:
            code = cli_mod.main([*argv, "--json"])
            captured = capsys.readouterr()
            assert code == 0, captured.err
            return json.loads(captured.out)

        models = run_cli("models", "--model", "fake/img", "--op", "image_generate")
        assert models["data"][0]["id"] == "fake/img"

        voices = run_cli("voices", "--provider", "fake")
        assert voices["voices"][0]["id"] == "alloy"

        gen = run_cli("image-gen", "a kite")
        assert Path(gen["paths"][0]).read_bytes() == PNG

        edit = run_cli(
            "image-edit", "blue", "--image", str(img_in), "--mask", str(mask_in)
        )
        assert Path(edit["paths"][0]).read_bytes() == PNG

        var = run_cli("image-variation", "--image", str(img_in))
        assert Path(var["paths"][0]).read_bytes() == PNG

        spk = run_cli("speak", "hello world", "--voice", "alloy")
        assert Path(spk["path"]).read_bytes() == MP3

        trn = run_cli("transcribe", str(aud_in), "--language", "en")
        assert trn["text"] == "transcribed text"

        trl = run_cli("translate", str(aud_in))
        assert trl["text"] == "translated text"

        vcreate = run_cli(
            "video-create", "flying kite", "--wait", "--poll-interval", "1"
        )
        assert Path(vcreate["path"]).read_bytes() == MP4

        vedit = run_cli(
            "video-edit",
            "brighter",
            "--video",
            str(vid_in),
            "--wait",
            "--poll-interval",
            "1",
        )
        assert Path(vedit["path"]).read_bytes() == MP4

        vst = run_cli("video-status", "vid_1", "--wait", "--variant", "thumbnail")
        assert Path(vst["path"]).read_bytes() == MP4

        vls = run_cli("video-list", "--limit", "5", "--order", "desc")
        assert vls["data"][0]["id"] == "vid_1"

        # video-delete without --yes fails cleanly with exit 2.
        assert cli_mod.main(["video-delete", "vid_1"]) == 2
        capsys.readouterr()

        vdel = run_cli("video-delete", "vid_1", "--yes")
        assert vdel["deleted"] is True

        ocr_md = tmp_path / "ocr_out" / "doc.md"
        ocr = run_cli("ocr", str(pdf_in), "--save-to", str(ocr_md), "--save-images")
        assert ocr["text"] == "# OCR Markdown"
        assert ocr_md.read_text(encoding="utf-8") == "# OCR Markdown"
        assert (ocr_md.parent / "page0-fig1.png").read_bytes() == PNG

        # Verify X-MCC-Inline-Images: 1 was sent on all image endpoints.
        img_reqs = [
            headers
            for method, path, headers, _ in server.seen
            if method == "POST" and path.startswith("/v1/images/")
        ]
        assert len(img_reqs) == 3
        assert all(h.get("x-mcc-inline-images") == "1" for h in img_reqs)
    finally:
        server.stop()
