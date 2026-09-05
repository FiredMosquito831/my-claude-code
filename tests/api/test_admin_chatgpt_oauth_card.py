"""The ChatGPT subscription card, and the provenance badge on the Models page.

Both surfaces answer the same question the dashboard could not answer before:
*why is this model in my picker, and where am I in my quota?* Everything here
is offline -- the plan comes off a credential already on disk, the catalogue
off the installed Codex CLI, the windows off headers a real response carried.
No test in this file contacts OpenAI, and none of them reads the developer's
own Codex install.
"""

import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api import admin_routes
from my_claude_code.api.admin_routes import NOT_YET_OBSERVED
from my_claude_code.api.model_admin import build_models_page_payload, listing_payload
from my_claude_code.application.model_metadata import (
    ModelListingEvidence,
    ModelListingProvenance,
    ProviderModelInfo,
)
from my_claude_code.config.model_overrides import ModelParameterOverrides
from my_claude_code.core.model_visibility import ModelVisibility
from my_claude_code.providers.chatgpt_oauth import response_headers as rh
from my_claude_code.providers.chatgpt_oauth.codex_catalogue import (
    CodexCatalogue,
    CodexCatalogueEntry,
)
from tests.api.support import create_test_app

STATUS_ENDPOINT = "/admin/api/chatgpt-oauth/status"

CATALOGUE = CodexCatalogue(
    version="0.151.0",
    source_path="/somewhere/vendor/bin/codex.exe",
    entries=(
        CodexCatalogueEntry(slug="gpt-5.6-luna", visibility="list"),
        CodexCatalogueEntry(
            slug="gpt-5.4",
            visibility="hide",
            retirement_at="2026-08-31T19:00:00Z",
            replacement_model_id="gpt-5.6-terra",
        ),
    ),
)


@pytest.fixture(autouse=True)
def _clean_observer():
    rh.OBSERVER._latest = None
    yield
    rh.OBSERVER._latest = None


@pytest.fixture(autouse=True)
def _no_real_codex_scan(monkeypatch):
    """Never memory-map the developer's own 300 MB Codex binary in a test."""
    monkeypatch.setattr(admin_routes, "load_codex_catalogue", lambda: None)


def _app(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return create_test_app()


def _get(app):
    return TestClient(app, client=("127.0.0.1", 50000)).get(STATUS_ENDPOINT)


def test_the_card_refuses_a_non_loopback_client(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    response = TestClient(app, client=("10.0.0.4", 51000)).get(STATUS_ENDPOINT)
    assert response.status_code == 403


def test_with_nothing_installed_the_card_says_so_rather_than_guessing(
    monkeypatch, tmp_path
):
    payload = _get(_app(monkeypatch, tmp_path)).json()

    assert payload["plan_type"] == ""
    assert payload["catalogue"]["available"] is False
    assert payload["windows"]["observed"] is False
    assert payload["windows"]["primary_used_percent"] == NOT_YET_OBSERVED


def test_the_card_names_the_vendor_catalogue_and_what_it_retired(monkeypatch, tmp_path):
    monkeypatch.setattr(admin_routes, "load_codex_catalogue", lambda: CATALOGUE)

    catalogue = _get(_app(monkeypatch, tmp_path)).json()["catalogue"]

    assert catalogue["available"] is True
    assert catalogue["version"] == "0.151.0"
    assert catalogue["model_count"] == 2
    # File name only: the full path is a local detail the card does not need.
    assert catalogue["source_name"] == "codex.exe"
    assert catalogue["retired_model_ids"] == ["gpt-5.4"]


def test_the_windows_are_only_what_a_real_response_carried(monkeypatch, tmp_path):
    rh.OBSERVER.observe(
        {
            "x-codex-primary-used-percent": "41.7",
            "x-codex-primary-reset-at": "2026-09-05T22:00:00Z",
            "x-codex-credits-balance": "0",
            "set-cookie": "session=secret",
        },
        status_code=200,
        now=1_760_000_000.0,
    )

    windows = _get(_app(monkeypatch, tmp_path)).json()["windows"]

    assert windows["observed"] is True
    assert windows["primary_used_percent"] == "41.7"
    assert windows["primary_reset_at"] == "2026-09-05T22:00:00Z"
    # A balance of "0" is a value OpenAI sent, not an absence.
    assert windows["credits_balance"] == "0"
    # Nothing OpenAI did not send: the secondary window was never observed.
    assert windows["secondary_used_percent"] == NOT_YET_OBSERVED
    assert "session=secret" not in json.dumps(windows)


def test_the_card_reads_the_plan_locally_and_never_the_identity_claims(
    monkeypatch, tmp_path
):
    """Q3's boundary, pinned: the plan may be read; the token, sub and email may not."""
    claims = {
        "sub": "user-must-never-appear",
        "email": "person@example.com",
        "https://api.openai.com/auth": {
            "chatgpt_plan_type": "plus",
            "chatgpt_account_id": "acct-123",
        },
    }
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    id_token = f"header.{body}.signature"
    store = tmp_path / ".mcc" / "auth"
    store.mkdir(parents=True, exist_ok=True)
    (store / "chatgpt-oauth.json").write_text(
        json.dumps(
            {
                "version": 1,
                "tokens": {
                    "access_token": "access.secret.value",
                    "refresh_token": "refresh-secret-value",
                    "id_token": id_token,
                    "account_id": "acct-123",
                },
            }
        ),
        encoding="utf-8",
    )

    response = _get(_app(monkeypatch, tmp_path))
    text = response.text

    assert response.json()["plan_type"] == "plus"
    for secret in (
        "user-must-never-appear",
        "person@example.com",
        "access.secret.value",
        "refresh-secret-value",
        id_token,
    ):
        assert secret not in text


# --------------------------------------------------- the Models page badge


def test_listing_payload_is_absent_when_nobody_recorded_a_provenance():
    """A badge that says nothing is worse than no badge."""
    assert listing_payload(None) is None


def test_the_models_page_carries_the_provenance_and_the_vendors_own_dates():
    infos = [
        ProviderModelInfo(
            model_id="chatgpt_oauth/gpt-5.4",
            listing=ModelListingEvidence(
                provenance=ModelListingProvenance.OBSERVED,
                detail="served 145x, last 2026-09-04",
                retirement_at="2026-09-30T19:00:00Z",
                replacement_model_id="gpt-5.6-terra",
                offered_by_default=True,
            ),
        ),
        ProviderModelInfo(model_id="open_router/plain"),
    ]

    payload = build_models_page_payload(
        infos, (), ModelVisibility(), ModelParameterOverrides()
    )
    rows = {
        model["model_ref"]: model
        for provider in payload["providers"]
        for model in provider["models"]
    }

    listing = rows["chatgpt_oauth/gpt-5.4"]["listing"]
    assert listing["provenance"] == "observed"
    assert listing["provenance_label"] == (payload["provenance_labels"]["observed"])
    assert listing["detail"] == "served 145x, last 2026-09-04"
    assert listing["retirement_at"] == "2026-09-30T19:00:00Z"
    assert listing["replacement_model_id"] == "gpt-5.6-terra"
    # ``False`` is unreachable since 6.48.1: an entry the vendor marked
    # ``visibility: hide`` never reaches a listing, so nothing in a listing
    # can be badged "not offered by default" -- and the badge that said so
    # went with it.
    assert listing["offered_by_default"] is True
    # Every other provider is untouched: a gateway that answers /models has no
    # existence question to answer, so it gets no badge.
    assert rows["open_router/plain"]["listing"] is None


def test_the_provenance_labels_cover_every_rung():
    payload = build_models_page_payload(
        [], (), ModelVisibility(), ModelParameterOverrides()
    )
    assert set(payload["provenance_labels"]) == {
        str(member) for member in ModelListingProvenance
    }
