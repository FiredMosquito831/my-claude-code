""" "Everything known" gains a media-request-fields row for a rail model (7.98.0).

Only for a model on a media rail: every other model's view is exactly what it
was. The row's "MCC uses" cell is what the request-field ladder answered,
field by field, with the same schemas the Models page's media row and
``GET /v1/media/models`` publish; each column lists what its own rung said,
outranked or not, and none is flagged as disagreeing. Every key is fake.
"""

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from my_claude_code.config.media_request_fields import (
    EMPTY_STATEMENTS,
    OperatorFieldStatement,
    save_media_request_fields,
    with_statement,
)
from my_claude_code.config.settings import Settings
from my_claude_code.providers.model_listing import extract_openai_model_infos
from my_claude_code.providers.recovery import learned_fact_store
from tests.api.support import create_test_app, provider_manager_for_app

FLUX = "together/black-forest-labs/FLUX.1-schnell"
CHAT = "together/meta-llama/Llama-3.3-70B-Instruct-Turbo"


def _admin(monkeypatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    app = create_test_app(
        Settings.model_validate(
            {
                "TOGETHER_API_KEY": "tg-" + "b" * 40,
                "MODEL": CHAT,
                "MODEL_IMAGE": FLUX,
            }
        )
    )
    infos = extract_openai_model_infos(
        {
            "data": [
                {"id": "black-forest-labs/FLUX.1-schnell"},
                {"id": "meta-llama/Llama-3.3-70B-Instruct-Turbo"},
            ]
        },
        provider_name="together",
    )
    provider_manager_for_app(app).cache_model_infos(
        "together", sorted(infos, key=lambda info: info.model_id)
    )
    return TestClient(app, client=("127.0.0.1", 50000))


def _knowledge(admin: TestClient, ref: str) -> dict[str, Any]:
    response = admin.get("/admin/api/models/knowledge", params={"ref": ref})
    assert response.status_code == 200, response.text
    return response.json()


def _media_field(payload: dict[str, Any]) -> dict[str, Any] | None:
    return next(
        (f for f in payload["fields"] if f["key"] == "media_request_fields"), None
    )


def test_a_chat_model_has_no_media_row(monkeypatch, tmp_path) -> None:
    admin = _admin(monkeypatch, tmp_path)

    assert _media_field(_knowledge(admin, CHAT)) is None


def test_a_rail_model_shows_what_its_declaration_says(monkeypatch, tmp_path) -> None:
    """Together declares image generation with no stream: that is all known."""

    admin = _admin(monkeypatch, tmp_path)

    field = _media_field(_knowledge(admin, FLUX))

    assert field is not None
    assert field["label"] == "media request fields"
    assert field["used_value"] == ["image generate · stream: not forwarded by MCC"]
    assert list(field["used"]["schemas"]) == ["image_generate"]
    # No operator, learned or listed rung said anything: no column.
    assert field["statements"] == []


def test_each_rung_is_listed_under_its_own_column(monkeypatch, tmp_path) -> None:
    learned_fact_store().record(
        "together",
        "black-forest-labs/FLUX.1-schnell",
        "media_param_refused",
        True,
        detail="image_generate:image_config",
        evidence="image_config is not supported",
    )
    save_media_request_fields(
        with_statement(
            EMPTY_STATEMENTS,
            FLUX,
            "image_generate",
            "image_config",
            OperatorFieldStatement(state="accepted"),
        )
    )
    admin = _admin(monkeypatch, tmp_path)

    field = _media_field(_knowledge(admin, FLUX))

    assert field is not None
    assert field["used_value"] == [
        "image generate · stream: not forwarded by MCC",
        "image generate · image_config: stated",
    ]
    by_column = {s["source"]: s for s in field["statements"]}
    assert by_column["operator"]["value"] == ["image generate · image_config: stated"]
    # Outranked by the operator, and still shown.
    assert by_column["learned"]["value"] == [
        "image generate · image_config: refused (learned)"
    ]
    assert all(s["agrees_with_used"] is None for s in field["statements"])
    assert not any(s["used"] for s in field["statements"])
