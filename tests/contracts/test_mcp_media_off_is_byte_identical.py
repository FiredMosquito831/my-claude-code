"""With no app added, MCC's media MCP server changes no byte anywhere (7.99.0).

IV.7 and the user's standing rule: opt-in only, and everything byte-identical
while it is off. ``mcp_media_off_baseline.json`` was captured from the code at
``fork/main`` = ``51a00ea1`` (7.98.0, before the server existed) by running this
very module there with ``MCC_MCP_OFF_BASELINE_WRITE`` set; this release must
reproduce every surface in it exactly, with ``MCP_MEDIA_APPS`` unset, empty,
or naming only apps that have no button:

* every MCC-owned coding-agent document the publisher writes at startup;
* ``GET /admin/api/catalogue-models`` (the launchers' fallback documents);
* the ``mcc-claude``, ``mcc-claude-old`` and ``mcc-codex`` commands and
  environments;
* the Claude Desktop document and the OpenCode desktop document Configure
  writes (the second shares OpenCode's serialiser -- risk R13);
* ``GET /v1/models`` and ``GET /v1beta/models``;
* the bodies MCC sends a provider for an image, an image edit and a video
  create, and what the client is answered -- without the opt-in
  ``x-mcc-inline-images`` header.

To refresh the baseline from a base checkout: copy this file there and run it
with ``MCC_MCP_OFF_BASELINE_WRITE=<path>``.
"""

import json
import os
import re
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.admin_desktop_app_routes import _documents
from my_claude_code.api.admin_harness_routes import _catalogue_models_payload
from my_claude_code.cli.launchers import claude as claude_launcher
from my_claude_code.cli.launchers import codex as codex_launcher
from my_claude_code.config.desktop_apps import desktop_app
from my_claude_code.config.harnesses import harness_specs
from my_claude_code.config.settings import Settings
from my_claude_code.providers.media.registry import MediaRegistry
from my_claude_code.runtime import harness_catalogues
from my_claude_code.runtime.harness_catalogues import HarnessCatalogueFanoutPublisher
from tests.api.support import create_test_app
from tests.runtime.test_catalogue_fanout import FakeRuntime

BASELINE = Path(__file__).with_name("mcp_media_off_baseline.json")
WRITE_BASELINE = os.environ.get("MCC_MCP_OFF_BASELINE_WRITE", "")
TOKEN = "proxy-token-for-the-equality-run"
XAI = "api.x.ai"
OPENROUTER = "openrouter.ai"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x06" * 40
#: ``MCP_MEDIA_APPS`` values that must all leave everything as it was.
OFF_VALUES: tuple[str | None, ...] = (None, "", "aider,pi,not-an-app")
_REQUEST_ID = re.compile(r"req_[0-9a-f]{32}")
_BOUNDARY = re.compile(r"boundary=([0-9a-zA-Z'()+_,./:=?-]+)")


def _settings(apps: str | None, **values: str) -> Settings:
    base: dict[str, Any] = {
        "ANTHROPIC_AUTH_TOKEN": TOKEN,
        "MODEL": "nvidia_nim/configured",
        "XAI_API_KEY": "xai-" + "a" * 40,
        "OPENROUTER_API_KEY": "sk-or-v1-" + "a" * 48,
        "PROVIDER_RETRY_ATTEMPTS": "1",
        "MODEL_IMAGE": "xai/grok-2-image",
        "MODEL_VIDEO": "open_router/google/veo-3.1",
        **values,
    }
    if apps is not None:
        base["MCP_MEDIA_APPS"] = apps
    return Settings.model_validate(base)


def _norm(text: str, tmp_path: Path) -> str:
    for spelling in (str(tmp_path), json.dumps(str(tmp_path))[1:-1]):
        text = text.replace(spelling, "<tmp>")
    return _REQUEST_ID.sub("req_<id>", text)


def _documents_surface(settings: Settings, tmp_path: Path) -> dict[str, str]:
    runtime = FakeRuntime(settings=settings)
    folder = tmp_path / "documents"
    paths = {
        spec.id: folder / f"{spec.id}{Path(spec.catalogue.filename).suffix}"
        for spec in harness_specs()
        if spec.catalogue is not None
        and spec.catalogue.filename is not None
        and spec.catalogue.merge is None
    }
    HarnessCatalogueFanoutPublisher(paths).ensure_exists(runtime)
    surface = {
        f"document:{harness_id}": path.read_text(encoding="utf-8")
        for harness_id, path in sorted(paths.items())
    }
    # The two route helpers read only ``services.requests``.
    services: Any = types.SimpleNamespace(requests=runtime)
    surface["admin/api/catalogue-models (payload)"] = json.dumps(
        _catalogue_models_payload(services, with_provenance=False)
    )
    for app_id in ("claude_desktop", "opencode_desktop"):
        block, scalars, sidecar = _documents(desktop_app(app_id), services, settings)
        surface[f"desktop:{app_id}"] = json.dumps(
            {"block": block, "scalars": scalars, "sidecar": sidecar}
        )
    return surface


def _launch_surface(settings: Settings, tmp_path: Path) -> dict[str, str]:
    surface: dict[str, str] = {}
    fixed_env = {"PATH": "/usr/bin", "SHELL_FOR_TEST": "1"}
    catalog = tmp_path / "codex-model-catalog.json"
    catalog.write_text('{"models": [{"slug": "a"}]}', encoding="utf-8")
    for module, entries in (
        (claude_launcher, ("launch", "launch_legacy")),
        (codex_launcher, ("launch",)),
    ):
        for entry in entries:
            for argv in (["fix it"], ["-p", "fix it"], ["--resume"], ["mcp", "list"]):
                captured: dict[str, Any] = {}
                with (
                    patch.object(module, "get_settings", return_value=settings),
                    patch.object(module, "preflight_proxy", return_value=None),
                    patch.object(module, "resolve_harness_binary", return_value="BIN"),
                    # The launchers call it with keywords only (command=, env=).
                    patch.object(
                        module, "run_client_process", side_effect=captured.update
                    ),
                    patch.object(
                        module, "os", types.SimpleNamespace(environ=fixed_env)
                    ),
                    patch.object(
                        codex_launcher, "codex_model_catalog_path", return_value=catalog
                    ),
                ):
                    getattr(module, entry)(argv)
                key = f"launch:{module.__name__.rsplit('.', 1)[-1]}.{entry}:{argv!r}"
                # The catalogue's path is this machine's; its place is not.
                command = [
                    part.replace(json.dumps(str(catalog))[1:-1], "<catalog>").replace(
                        str(catalog), "<catalog>"
                    )
                    for part in captured["command"]
                ]
                surface[key] = json.dumps(
                    {"command": command, "env": dict(sorted(captured["env"].items()))}
                )
    return surface


class _Upstream:
    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.seen.append(request)
        host, path = request.url.host, request.url.path
        if host == XAI and path in ("/v1/images/generations", "/v1/images/edits"):
            return httpx.Response(
                200,
                json={
                    "created": 1,
                    "data": [{"url": "https://cdn.example.test/a.png"}],
                },
            )
        if host == OPENROUTER and request.method == "POST":
            return httpx.Response(202, json={"id": "or-job-1", "status": "pending"})
        return httpx.Response(404, json={"error": {"message": "no such route"}})


def _body(request: httpx.Request) -> str:
    content = request.content.decode("utf-8", errors="replace")
    match = _BOUNDARY.search(request.headers.get("content-type", ""))
    if match:
        content = content.replace(match.group(1), "<boundary>")
    return content


def _wire_surface(settings: Settings, tmp_path: Path) -> dict[str, str]:
    upstream = _Upstream()
    registry = MediaRegistry(transport=httpx.MockTransport(upstream.handler))
    app = create_test_app(settings, media=registry)
    auth = {"authorization": f"Bearer {TOKEN}"}
    surface: dict[str, str] = {}
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        for path in ("/v1/models", "/v1beta/models"):
            answer = client.get(path, headers=auth)
            surface[f"GET {path}"] = f"{answer.status_code} {answer.text}"
        answer = client.get("/admin/api/catalogue-models")
        surface["GET /admin/api/catalogue-models"] = (
            f"{answer.status_code} {answer.text}"
        )
        answers = {
            "images.generate": client.post(
                "/v1/images/generations",
                json={"prompt": "a red kite", "size": "1024x1024", "n": 1},
                headers=auth,
            ),
            "images.edit": client.post(
                "/v1/images/edits",
                files=[("image", ("kite.png", PNG, "image/png"))],
                data={"prompt": "make it blue"},
                headers=auth,
            ),
            "videos.create": client.post(
                "/v1/videos", json={"prompt": "a kite", "seconds": "4"}, headers=auth
            ),
        }
        for name, answer in answers.items():
            text = answer.text if name != "videos.create" else ""
            surface[f"answer:{name}"] = f"{answer.status_code} {text}"
    surface["upstream"] = json.dumps(
        [
            {
                "method": request.method,
                "url": str(request.url),
                "content_type": _BOUNDARY.sub(
                    "boundary=<boundary>", request.headers.get("content-type", "")
                ),
                "body": _body(request),
            }
            for request in upstream.seen
        ]
    )
    return {key: _norm(value, tmp_path) for key, value in surface.items()}


def _surfaces(apps: str | None, tmp_path: Path, monkeypatch) -> dict[str, str]:
    work = tmp_path / f"run-{apps!r}".replace("'", "").replace(",", "-")
    work.mkdir()
    monkeypatch.setenv("MCC_CONFIG_DIR", str(work))
    # Pinned, so no shell's address reaches the captured documents.
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", "18999")
    monkeypatch.setattr(
        harness_catalogues,
        "harness_catalogue_path",
        lambda name: work / "catalogues" / name,
    )
    settings = _settings(apps)
    surface = {
        **_documents_surface(settings, work),
        **_launch_surface(settings, work),
        **_wire_surface(settings, work),
    }
    return {key: value.replace(str(work), "<tmp>") for key, value in surface.items()}


@pytest.mark.parametrize("apps", OFF_VALUES, ids=["unset", "empty", "no-button-apps"])
def test_mcp_media_off_is_byte_identical(apps: str | None, tmp_path: Path, monkeypatch):
    surface = _surfaces(apps, tmp_path, monkeypatch)
    if WRITE_BASELINE:
        if apps is None:
            Path(WRITE_BASELINE).write_text(
                json.dumps(surface, indent=1, sort_keys=True) + "\n", encoding="utf-8"
            )
        pytest.skip("wrote the baseline")
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert sorted(surface) == sorted(baseline)
    different = sorted(key for key in baseline if surface[key] != baseline[key])
    assert different == [], different
