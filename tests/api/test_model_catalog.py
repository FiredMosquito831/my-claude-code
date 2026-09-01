"""A custom provider's models must reach ``/v1/models`` on the create request.

The reported symptom was a healthy-looking provider card next to a client that
could not see a single one of its models. Everything between the create route
and the model list is real here; only the upstream HTTP client is doubled.
"""

from fastapi.testclient import TestClient

from tests.api.support import ModelListingProviderDouble, create_custom_provider_app


def test_custom_provider_models_appear_in_v1_models_after_create(
    monkeypatch, tmp_path
) -> None:
    upstream = ModelListingProviderDouble(("glm-5.3-flash", "glm-5.3"))
    app, _ = create_custom_provider_app(
        monkeypatch, tmp_path, {"custom_acme_ai": upstream}
    )
    admin = TestClient(app, client=("127.0.0.1", 50000))

    created = admin.post(
        "/admin/api/custom-providers",
        json={
            "display_name": "Acme AI",
            "base_url": "https://api.acme.example/v1",
            "api_key": "sk-acme-aaaa1111bbbb",
        },
    )
    assert created.status_code == 200

    ids = [item["id"] for item in TestClient(app).get("/v1/models").json()["data"]]

    # Both prefixed variants, for every discovered model, with no restart and
    # no second mutation.
    for model_id in ("glm-5.3-flash", "glm-5.3"):
        assert f"anthropic/custom_acme_ai/{model_id}" in ids
        assert f"claude-3-freecc-no-thinking/custom_acme_ai/{model_id}" in ids


def test_custom_provider_model_admin_block_reports_the_catalogue_count(
    monkeypatch, tmp_path
) -> None:
    upstream = ModelListingProviderDouble(("glm-5.3-flash", "glm-5.3", "glm-5.3-air"))
    app, _ = create_custom_provider_app(
        monkeypatch, tmp_path, {"custom_acme_ai": upstream}
    )
    admin = TestClient(app, client=("127.0.0.1", 50000))
    admin.post(
        "/admin/api/custom-providers",
        json={
            "display_name": "Acme AI",
            "base_url": "https://api.acme.example/v1",
            "api_key": "sk-acme-aaaa1111bbbb",
        },
    )

    page = admin.get("/admin/api/model-admin").json()

    block = next(
        provider
        for provider in page["providers"]
        if provider["provider_id"] == "custom_acme_ai"
    )
    assert block["model_count"] == 3
