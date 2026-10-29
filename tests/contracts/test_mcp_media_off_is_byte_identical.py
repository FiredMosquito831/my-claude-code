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

7.100.0 added the five other Desktop apps documents (``desktop:codex_desktop``,
``goose_desktop``, ``crush_desktop``, ``roo_code``, ``commandcode``), captured
at ``fork/main`` = ``9fa8f43d`` (7.99.0); that recapture reproduced every one
of the 33 surfaces above byte for byte, so they still stand as ``51a00ea1``
wrote them. It also added the Windows cycle baseline and the R13 test at the
foot of this file.

7.116.0 recaptured it for one added harness, OpenClaw, and the recapture was
checked the only way this file's promise can be checked: every surface above
is byte-identical, and the two that enumerate harnesses -- the payload and the
route's own answer -- are byte-identical once ``catalogues.openclaw`` is
removed. Adding a harness cannot leave those two unchanged; it must leave
everything else alone, and this release's recapture proved it did.

To refresh the baseline from a base checkout: copy this file there and run it
with ``MCC_MCP_OFF_BASELINE_WRITE=<path>``.
"""

import json
import os
import re
import sys
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
from my_claude_code.config import desktop_apply, mcp_media, paths
from my_claude_code.config.desktop_apply import document_path_for, sidecar_path_for
from my_claude_code.config.desktop_apps import desktop_app
from my_claude_code.config.harnesses import harness_specs
from my_claude_code.config.settings import Settings
from my_claude_code.providers.media.registry import MediaRegistry
from my_claude_code.runtime import harness_catalogues
from my_claude_code.runtime.harness_catalogues import HarnessCatalogueFanoutPublisher
from tests.api.support import create_test_app
from tests.config.test_desktop_apply import env_for, make_marker
from tests.runtime.test_catalogue_fanout import FakeRuntime

BASELINE = Path(__file__).with_name("mcp_media_off_baseline.json")
WRITE_BASELINE = os.environ.get("MCC_MCP_OFF_BASELINE_WRITE", "")
TOKEN = "proxy-token-for-the-equality-run"
XAI = "api.x.ai"
OPENROUTER = "openrouter.ai"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x06" * 40
#: ``MCP_MEDIA_APPS`` values that must all leave everything as it was.
OFF_VALUES: tuple[str | None, ...] = (None, "", "aider,pi,not-an-app")
#: The servable Desktop apps cards beyond the two 7.99.0 already pinned.
DESKTOP_DOCUMENT_APPS: tuple[str, ...] = (
    "codex_desktop",
    "goose_desktop",
    "crush_desktop",
    "roo_code",
    "commandcode",
)
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


def _norm_escaped(text: str, tmp_path: Path) -> str:
    """``_norm`` for a surface that quotes a document that quotes a path.

    A plan diff of a JSON file is JSON inside JSON, so the scratch folder is
    spelled with its backslashes escaped twice there; the longest spelling goes
    first so a shorter one cannot take part of it.
    """

    once = json.dumps(str(tmp_path))[1:-1]
    for spelling in (json.dumps(once)[1:-1], once, str(tmp_path), tmp_path.as_posix()):
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
    # 7.100.0: every other Desktop apps document too. The media server entry
    # is a second key those cards own in the user's own file, and none of it
    # may reach what Configure writes. Separators are made one spelling so the
    # baseline (captured on Windows) holds on every platform: only Roo Code's
    # settings key carries a path, and it is MCC's own config folder.
    for app_id in DESKTOP_DOCUMENT_APPS:
        block, scalars, sidecar = _documents(desktop_app(app_id), services, settings)
        surface[f"desktop:{app_id}"] = _norm_escaped(
            json.dumps({"block": block, "scalars": scalars, "sidecar": sidecar}),
            tmp_path,
        ).replace("\\\\", "/")
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


# ------------------------------------------- the user's own files (7.100.0)
#
# Decision 11: the Desktop apps engine owns a second key per document -- MCC's
# media server entry -- in the user's own Codex ``config.toml``, OpenCode
# ``opencode.json``, Command Code ``mcp.json`` and Claude Desktop
# ``claude_desktop_config.json``. With nothing added, every card's whole
# Configure / plan / Undo cycle must be what ``fork/main`` = ``9fa8f43d``
# (7.99.0) does: ``mcp_media_off_desktop_baseline.json`` was captured there
# by running this module with ``MCC_MCP_OFF_DESKTOP_BASELINE_WRITE`` set.
#
# Windows only: the documents live where each app keeps them on the platform
# (``%APPDATA%`` against ``~/.config``), so a baseline is one platform's. The
# Windows full-suite job runs it nightly and on every release; the portable
# half of the same promise -- every desktop document's block, scalars and
# sidecar -- is in the baseline above and runs everywhere.

DESKTOP_BASELINE = Path(__file__).with_name("mcp_media_off_desktop_baseline.json")
WRITE_DESKTOP_BASELINE = os.environ.get("MCC_MCP_OFF_DESKTOP_BASELINE_WRITE", "")

#: One realistic file of the user's own per servable card, with the comments
#: and layout the format keeps.
DESKTOP_SEEDS: dict[str, str] = {
    "codex_desktop": (
        "# my Codex settings\n"
        'approval_policy = "on-request"\n'
        "\n"
        "[mcp_servers.github]\n"
        'command = "npx"\n'
        'args = ["-y", "@modelcontextprotocol/server-github"]\n'
        "\n"
        '[projects."C:/work"]\n'
        'trust_level = "trusted"\n'
    ),
    "opencode_desktop": (
        "{\n"
        '  "$schema": "https://opencode.ai/config.json",\n'
        "  // my comment\n"
        '  "theme": "tokyonight",\n'
        '  "mcp": {\n'
        '    "context7": {"type": "remote", "url": "https://mcp.context7.com/mcp"}\n'
        "  }\n"
        "}\n"
    ),
    "goose_desktop": "# goose\nGOOSE_MODEL: gpt-5.6\nGOOSE_MODE: approve\n",
    "crush_desktop": '{\n    "options": {"debug": false}\n}\n',
    "roo_code": '{\n\t// VS Code\n\t"editor.fontSize": 14\n}\n',
    "commandcode": '{\n  "provider": {\n    "openrouter": {"name": "OpenRouter"}\n  }\n}\n',
    "claude_desktop": (
        "{\n"
        '  "appliedId": "3fd258a0-0379-416e-b3b5-0b72a6ac5392",\n'
        '  "entries": [\n'
        '    {"id": "3fd258a0-0379-416e-b3b5-0b72a6ac5392", "name": "My own gateway"}\n'
        "  ]\n"
        "}"
    ),
}

#: Keys 7.100.0 adds to a card's payload. They do not exist at ``fork/main``,
#: carry no document, and are compared by their own tests.
_NEW_PAYLOAD_KEYS = ("mcp", "mcp_note", "removed_mcp_server")


def _desktop_home(work: Path, monkeypatch) -> Path:
    home = work / "home"
    env = env_for(home)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("MCC_CONFIG_DIR", str(work / "mcc"))
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", "18999")
    for name in ("CODEX_HOME", "OPENCODE_CONFIG_DIR", "CRUSH_GLOBAL_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    for app_id, seed in DESKTOP_SEEDS.items():
        spec = desktop_app(app_id)
        make_marker(home, spec)
        path = document_path_for(spec, env)
        assert path is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(seed, encoding="utf-8", newline="")
    return home


def _desktop_cycle_surface(work: Path, monkeypatch) -> dict[str, str]:
    home = _desktop_home(work, monkeypatch)
    env = env_for(home)
    app = create_test_app(_settings(None))
    surface: dict[str, str] = {}

    def strip(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value for key, value in payload.items() if key not in _NEW_PAYLOAD_KEYS
        }

    def files(app_id: str) -> str:
        spec = desktop_app(app_id)
        captured: dict[str, str | None] = {}
        for label, path in (
            ("document", document_path_for(spec, env)),
            ("sidecar", sidecar_path_for(spec, env)),
        ):
            if path is not None:
                captured[label] = (
                    path.read_text(encoding="utf-8") if path.exists() else None
                )
        return json.dumps(captured)

    with TestClient(
        app, client=("127.0.0.1", 50000), headers={"origin": "http://127.0.0.1:18999"}
    ) as client:
        listing = client.get("/admin/api/desktop-apps").json()["apps"]
        for card in listing:
            surface[f"card:{card['id']}"] = json.dumps(strip(card))
        for app_id in DESKTOP_SEEDS:
            base = f"/admin/api/desktop-apps/{app_id}"
            steps = (
                ("plan", "plan", {}),
                ("configure", "configure", {}),
                ("undo keys_only", "undo", {"mode": "keys_only"}),
                ("configure again", "configure", {}),
                ("undo restore", "undo", {"mode": "restore"}),
            )
            for label, route, body in steps:
                answer = client.post(f"{base}/{route}", json=body)
                payload = answer.json()
                surface[f"{app_id}:{label}"] = f"{answer.status_code} " + json.dumps(
                    strip(payload) if isinstance(payload, dict) else payload
                )
                surface[f"{app_id}:{label}:files"] = files(app_id)
    return {key: _norm_escaped(value, work) for key, value in surface.items()}


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="the baseline is Windows' file layout; the Windows full-suite job runs it",
)
def test_every_desktop_apps_cycle_is_byte_identical_with_nothing_added(
    tmp_path: Path, monkeypatch
):
    surface = _desktop_cycle_surface(tmp_path, monkeypatch)
    if WRITE_DESKTOP_BASELINE:
        Path(WRITE_DESKTOP_BASELINE).write_text(
            json.dumps(surface, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
        pytest.skip("wrote the desktop baseline")
    baseline = json.loads(DESKTOP_BASELINE.read_text(encoding="utf-8"))
    assert sorted(surface) == sorted(baseline)
    different = sorted(key for key in baseline if surface[key] != baseline[key])
    assert different == [], different


def test_the_opencode_launcher_layer_never_carries_the_desktop_entry(
    tmp_path: Path, monkeypatch
):
    """Risk R13: OpenCode's launcher layer and its desktop document share a
    serialiser, so the desktop card's entry -- written into the user's own
    ``opencode.json`` -- must leave every document MCC owns as it was."""

    home = tmp_path / "home"
    for name, value in env_for(home).items():
        monkeypatch.setenv(name, value)
    for run in ("plain", "with-entry"):
        (tmp_path / run).mkdir()
    plain = _surfaces(None, tmp_path / "plain", monkeypatch)

    spec = desktop_app("opencode_desktop")
    make_marker(home, spec)
    env = dict(os.environ)
    assert spec.mcp is not None
    # Reached through the modules, so this file still imports at a base
    # checkout that predates them -- which is where its baselines come from.
    entry = mcp_media.desktop_file_entry(
        spec.mcp.shape, spec.id, str(tmp_path / "mcc-mcp.exe"), {}
    )
    result = desktop_apply.apply_mcp(spec, env=env, add=True, entry=entry)
    assert result.changed
    path = desktop_apply.mcp_path_for(spec, env)
    assert path is not None and "mcc_media" in path.read_text(encoding="utf-8")

    # The config folder is resolved once per process; each capture has its own.
    paths.reset_config_dir_cache()
    with_entry = _surfaces(None, tmp_path / "with-entry", monkeypatch)
    assert sorted(with_entry) == sorted(plain)
    assert [key for key in plain if with_entry[key] != plain[key]] == []
    for key in ("document:opencode", "document:opencode2", "desktop:opencode_desktop"):
        assert "mcc_media" not in with_entry[key]
