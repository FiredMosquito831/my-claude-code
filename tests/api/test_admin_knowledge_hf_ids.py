"""The "Everything known" rows show a catalogue's Hugging Face ids as themselves (7.97.0).

About twenty long Hugging Face ids read ``<redacted>`` in the view: the rows'
scrubber took them for keys. The catalogue lists them, which makes them
declared ids, so the view hands the scrubber the catalogue's ids and the ids
each row is filed under; a value equal to one of those skips the entropy rung,
and every other value -- a key-shaped one in the same row included -- is
scrubbed exactly as before. Every key here is fake.
"""

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from my_claude_code.config.settings import Settings
from my_claude_code.providers.model_listing import extract_openai_model_infos
from my_claude_code.providers.runtime.models_dev import (
    enrich_model_infos,
    read_models_dev_cache,
    write_models_dev_cache,
)
from tests.api.support import create_test_app, provider_manager_for_app

HF_ID = "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8"
#: Listed by no provider here: a models.dev row's own text that is not an id.
UNLISTED_RUN = "Xq7Zk2LmP9vRt4WbN8sYc3HdJ6gFa1QeU5oTiK0zM4nB"
KEY_SHAPED = "Zr8Wq3NcV5bT1yHk6Lp2Ms9Jd4Gf7Xa0Ue3Ri8Ot5Yw"

NOVITA_ROW: dict[str, Any] = {
    "id": HF_ID,
    "display_name": "Llama 4 Maverick",
    "context_size": 1048576,
    "input_modalities": ["text", "image"],
    "output_modalities": ["text"],
    "session_hint": KEY_SHAPED,
}
MODELS_DEV: dict[str, Any] = {
    "novita-ai": {
        "id": "novita-ai",
        "models": {
            HF_ID: {
                "id": HF_ID,
                "name": "Llama 4 Maverick",
                "limit": {"context": 1048576, "output": 8192},
                "modalities": {"input": ["text", "image"], "output": ["text"]},
                "family": UNLISTED_RUN,
            }
        },
    }
}


def _app(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    write_models_dev_cache(MODELS_DEV)
    app = create_test_app(
        Settings.model_validate(
            {"NOVITA_API_KEY": "sk_" + "3" * 40, "MODEL": f"novita/{HF_ID}"}
        )
    )
    cache = read_models_dev_cache()
    assert cache is not None
    novita = enrich_model_infos(
        sorted(
            extract_openai_model_infos({"data": [NOVITA_ROW]}, provider_name="novita"),
            key=lambda info: info.model_id,
        ),
        cache.index,
        "novita",
    )
    provider_manager_for_app(app).cache_model_infos("novita", novita)
    return app


def test_a_listed_hf_id_reads_as_itself_and_a_key_beside_it_stays_redacted(
    monkeypatch, tmp_path
) -> None:
    admin = TestClient(_app(monkeypatch, tmp_path), client=("127.0.0.1", 50000))
    response = admin.get(
        "/admin/api/models/knowledge", params={"ref": f"novita/{HF_ID}"}
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    rows = {row["source"]: row for row in payload["rows"]}

    provider = rows["provider_list"]["row"]
    assert provider["id"] == HF_ID
    assert provider["session_hint"] == "<redacted>"
    bucket = rows["models_dev_bucket"]["row"]
    assert bucket["id"] == HF_ID
    # Not an id anything lists: the entropy rung still has it.
    assert bucket["family"] == "<redacted>"
    text = json.dumps(payload)
    assert KEY_SHAPED not in text
    assert UNLISTED_RUN not in text
