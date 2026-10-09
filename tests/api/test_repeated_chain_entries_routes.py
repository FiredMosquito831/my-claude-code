"""The dashboard routes keep a model listed twice in a chain (7.82.0).

Model Config's Apply, a coding agent's tier Save and the Models page's media
section each read or write a chain. Up to 7.81.1 the first two silently
collapsed a repeat and the third had nothing to show; now each keeps every
listing, and a chain without a repeat is written byte for byte as before.
Every key and ref is fake.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_claude_code.config.settings import Settings
from tests.api.support import create_test_app

A = "nvidia_nim/a"
B = "open_router/b"
C = "groq/c"


def _local_client(app) -> TestClient:
    return TestClient(app, client=("127.0.0.1", 50000))


def _home(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    for key in ("MODEL", "MCC_ENV_FILE", "MODEL_OPUS", "MODEL_OPUS_FALLBACKS"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path / ".mcc" / ".env"


def _chain_line(env_file: Path, key: str) -> str:
    (line,) = [
        line
        for line in env_file.read_text(encoding="utf-8").splitlines()
        if line.startswith(f"{key}=")
    ]
    return line


def test_apply_keeps_a_repeated_chain_and_reads_it_back(monkeypatch, tmp_path):
    env_file = _home(monkeypatch, tmp_path)
    client = _local_client(create_test_app())

    response = client.post(
        "/admin/api/config/apply",
        json={"values": {"MODEL_OPUS": A, "MODEL_OPUS_FALLBACKS": f"{B}, {A},{B}"}},
    )

    assert response.status_code == 200, response.text
    assert response.json()["applied"] is True
    assert _chain_line(env_file, "MODEL_OPUS_FALLBACKS") == (
        f"MODEL_OPUS_FALLBACKS={B},{A},{B}"
    )
    fields = {
        field["key"]: field
        for field in client.get("/admin/api/config").json()["fields"]
    }
    assert fields["MODEL_OPUS_FALLBACKS"]["value"] == f"{B},{A},{B}"

    # Saving the page again with the value it shows changes nothing on disk.
    before = env_file.read_bytes()
    again = client.post(
        "/admin/api/config/apply",
        json={
            "values": {"MODEL_OPUS_FALLBACKS": fields["MODEL_OPUS_FALLBACKS"]["value"]}
        },
    )
    assert again.status_code == 200, again.text
    assert env_file.read_bytes() == before


@pytest.mark.parametrize(
    "typed",
    [f"{B},{C}", f" {B} ,{C},", f"{C}"],
)
def test_apply_writes_a_repeat_free_chain_exactly_as_before(
    monkeypatch, tmp_path, typed: str
):
    env_file = _home(monkeypatch, tmp_path)
    client = _local_client(create_test_app())

    response = client.post(
        "/admin/api/config/apply",
        json={"values": {"MODEL_OPUS": A, "MODEL_OPUS_FALLBACKS": typed}},
    )

    assert response.status_code == 200, response.text
    # 7.81.1's normalisation: split, strip, drop empties, join.
    canonical = ",".join(part.strip() for part in typed.split(",") if part.strip())
    assert _chain_line(env_file, "MODEL_OPUS_FALLBACKS") == (
        f"MODEL_OPUS_FALLBACKS={canonical}"
    )


@pytest.fixture
def tier_store(monkeypatch, tmp_path: Path):
    from my_claude_code.api import admin_harness_routes
    from my_claude_code.config import harness_tiers

    path = tmp_path / "harness_tiers.json"
    monkeypatch.setattr(harness_tiers, "harness_tiers_path", lambda: path)
    monkeypatch.setattr(
        admin_harness_routes,
        "current_harness_tiers",
        lambda: harness_tiers.load_harness_tiers(path),
    )
    harness_tiers.reset_harness_tiers_cache()
    yield path
    harness_tiers.reset_harness_tiers_cache()


def test_an_agent_tier_save_keeps_a_repeated_fallback(tier_store: Path) -> None:
    client = _local_client(create_test_app(Settings(model=A)))

    payload = client.post(
        "/admin/api/harness-tiers",
        json={
            "harness": "opencode",
            "tier": "best",
            "override": True,
            "model": B,
            "fallbacks": [A, B, A],
            "paused": [A, A],
        },
    ).json()

    best = payload["harnesses"]["opencode"]["best"]
    assert best["fallbacks"] == [A, B, A]
    assert best["paused"] == [A]
    assert best["resolved"]["primary"] == B
    assert best["resolved"]["fallbacks"] == [A, B, A]
    assert json.loads(tier_store.read_text(encoding="utf-8")) == {
        "harnesses": {
            "opencode": {"best": {"model": B, "fallbacks": [A, B, A], "paused": [A]}}
        }
    }


def test_the_media_section_shows_each_listing_with_its_position(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    image_a = "together/black-forest-labs/FLUX.1-schnell"
    image_b = "deepinfra/stabilityai/sdxl-turbo"
    settings = Settings.model_validate(
        {
            "TOGETHER_API_KEY": "tg-" + "b" * 40,
            "MODEL_IMAGE": image_a,
            "MODEL_IMAGE_FALLBACKS": f"{image_b},{image_a}",
            "MODEL_IMAGE_PAUSED": image_b,
        }
    )
    client = _local_client(create_test_app(settings))

    payload = client.get("/admin/api/media/models").json()

    (image,) = [rail for rail in payload["rails"] if rail["rail"] == "image"]
    assert image["refs"] == [image_a, image_b, image_a]
    # Still one row per model, now with one placement per listing.
    rows = {row["model_ref"]: row for row in payload["models"]}
    assert [row["model_ref"] for row in payload["models"]].count(image_a) == 1
    assert [
        (placement["position"], placement["index"])
        for placement in rows[image_a]["placements"]
    ] == [("primary", 0), ("fallback 2", 2)]
    assert [
        (placement["position"], placement["paused"])
        for placement in rows[image_b]["placements"]
    ] == [("fallback 1", True)]
