"""Every route that saves settings refuses cleanly while the file is busy (7.69.7).

With the managed ``.env`` unreadable, each saving route answers **503** with
the plain sentence as ``detail`` -- the admin routes' own envelope for a
transient failure, which the dashboard shows as it is -- and the file on disk
is byte-identical afterwards. Before 7.69.7 the same moment rewrote the file
from an empty read (a pause kept one key; a credential add kept one key of the
pool). The read-only ``GET /admin/api/config`` still answers 200: a busy file
must not stop the dashboard from loading.
"""

import hashlib
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_claude_code.config.admin import env_io, sources
from my_claude_code.config.settings import Settings
from tests.api.support import create_test_app

FABLE_A = "nvidia_nim/vendor/fable-a"
FABLE_B = "nvidia_nim/vendor/fable-b"

SEED = (
    "DEEPSEEK_API_KEY=sk-test-fake-0001,sk-test-fake-0002,sk-test-fake-0003\n"
    "TAVILY_API_KEY=tvly-test-fake-0001,tvly-test-fake-0002\n"
    f"MODEL_FABLE={FABLE_A}\n"
    f"MODEL_FABLE_FALLBACKS={FABLE_B}\n"
    "LOG_LEVEL=INFO\n"
    "MODEL_VISIBILITY_DENY=*:free\n"
)

_REAL_READ = env_io._attempt_read


def _same(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


def _sha(path: Path) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


@pytest.fixture
def managed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / ".mcc"
    config.mkdir()
    monkeypatch.setenv("MCC_CONFIG_DIR", str(config))
    monkeypatch.chdir(tmp_path)
    for key in (
        "DEEPSEEK_API_KEY",
        "TAVILY_API_KEY",
        "MODEL_FABLE",
        "MODEL_FABLE_FALLBACKS",
        "MODEL_FABLE_PAUSED",
        "LOG_LEVEL",
        "MODEL_VISIBILITY_ALLOW",
        "MODEL_VISIBILITY_DENY",
        "MCC_ENV_FILE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(env_io, "_sleep", lambda _seconds: None)
    sources.clear_env_parse_cache()
    path = config / ".env"
    path.write_text(SEED, encoding="utf-8")
    return path


@pytest.fixture
def client() -> TestClient:
    settings = Settings().model_copy(
        update={
            "model_fable": FABLE_A,
            "model_fable_fallbacks": FABLE_B,
            "deepseek_api_key": "sk-test-fake-0001,sk-test-fake-0002,sk-test-fake-0003",
        }
    )
    return TestClient(create_test_app(settings), client=("127.0.0.1", 50000))


@pytest.fixture
def busy(managed: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every save-path read of the managed file is refused, as on Windows."""

    def read(path: Path) -> tuple[bytes, int]:
        if _same(path, managed):
            raise PermissionError(13, "Permission denied", str(path))
        return _REAL_READ(path)

    monkeypatch.setattr(env_io, "_attempt_read", read)
    return managed


SAVING_REQUESTS = [
    ("POST", "/admin/api/config/apply", {"values": {"LOG_LEVEL": "DEBUG"}}),
    (
        "POST",
        "/admin/api/config/route-pause",
        {"model_key": "MODEL_FABLE", "model_ref": FABLE_B, "paused": True},
    ),
    ("POST", "/admin/api/credentials/DEEPSEEK_API_KEY/keys", {"key": "sk-test-new"}),
    ("DELETE", "/admin/api/credentials/DEEPSEEK_API_KEY/keys/1", None),
    (
        "PUT",
        "/admin/api/credentials/DEEPSEEK_API_KEY/keys/order",
        # Key ids; the refusal comes from the read, before they are looked at.
        {"order": ["third", "second", "first"]},
    ),
    (
        "POST",
        "/admin/api/websearch/credentials/TAVILY_API_KEY/keys",
        {"key": "tvly-test-new"},
    ),
    ("DELETE", "/admin/api/websearch/credentials/TAVILY_API_KEY/keys/0", None),
    (
        "POST",
        "/admin/api/model-admin/visibility",
        {"allow": "", "deny": "*:free,*-preview"},
    ),
]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    SAVING_REQUESTS,
    ids=[f"{method} {path}" for method, path, _ in SAVING_REQUESTS],
)
def test_a_saving_route_refuses_with_503_and_changes_nothing(
    busy: Path, client: TestClient, method: str, path: str, body: object
) -> None:
    before = _sha(busy)

    response = client.request(method, path, json=body)

    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert detail.startswith("Not saved: the settings file was busy")
    assert "nothing was changed. Try again." in detail
    assert "sk-test-fake" not in response.text
    assert _sha(busy) == before
    assert sorted(p.name for p in busy.parent.iterdir()) == [".env"]


def test_the_dashboard_still_loads_while_the_file_is_busy(
    busy: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = Path.read_bytes

    def display_busy(self: Path) -> bytes:
        if _same(self, busy):
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", display_busy)

    assert client.get("/admin/api/config").status_code == 200


def test_the_same_save_goes_through_once_the_file_is_free(
    managed: Path, client: TestClient
) -> None:
    response = client.post(
        "/admin/api/config/route-pause",
        json={"model_key": "MODEL_FABLE", "model_ref": FABLE_B, "paused": True},
    )

    assert response.status_code == 200, response.text
    text = managed.read_text(encoding="utf-8")
    assert f"MODEL_FABLE_PAUSED={FABLE_B}" in text
    assert "DEEPSEEK_API_KEY=sk-test-fake-0001,sk-test-fake-0002" in text
    assert (managed.parent / ".env.previous").read_text(encoding="utf-8") == SEED
