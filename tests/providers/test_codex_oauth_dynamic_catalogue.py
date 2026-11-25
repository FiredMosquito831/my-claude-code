"""Tests for PR 5 dynamic Codex OAuth model discovery (S1/S2a/S2b/S3/S5 & Option A)."""

import base64
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from my_claude_code.api.admin_routes import _model_knowledge
from my_claude_code.api.model_admin import (
    KNOWLEDGE_SOURCE_PROVIDER,
    SOURCE_PROVIDER,
    build_models_page_payload,
)
from my_claude_code.application.catalogue_model import build_catalogue_models
from my_claude_code.application.catalogues.codex import build_codex_catalogue
from my_claude_code.application.model_metadata import (
    ModelListingProvenance,
    ProviderModelInfo,
)
from my_claude_code.config.constants import CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE
from my_claude_code.config.model_overrides import ModelParameterOverrides
from my_claude_code.config.paths import chatgpt_oauth_auth_path, config_dir_path
from my_claude_code.config.settings import Settings
from my_claude_code.core.model_visibility import ModelVisibility
from my_claude_code.core.reasoning import ReasoningEffort
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.chatgpt_oauth import codex_catalogue, credentials
from my_claude_code.providers.chatgpt_oauth.codex_catalogue import (
    CODEX_CLIENT_VERSION_FLOOR,
    CODEX_LIVE_CACHE_FILENAME,
    CODEX_MODELS_LIVE_TTL_SECONDS,
    CodexCatalogue,
    CodexCatalogueEntry,
    clear_codex_models_live_cache,
    load_codex_catalogue,
    load_codex_models_cache,
    resolve_codex_client_version,
    validate_codex_models_document,
)
from my_claude_code.providers.chatgpt_oauth.provider import ChatGPTOAuthProvider
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.runtime.discovery import ProviderModelDiscovery
from my_claude_code.providers.runtime.factory import create_provider
from my_claude_code.providers.runtime.model_cache import ProviderModelCache
from my_claude_code.providers.runtime.models_dev import (
    enrich_model_infos,
    models_dev_cache_path,
    write_models_dev_cache,
)
from tests.api.support import create_test_app, provider_manager_for_app
from tests.application.test_catalogue_model import FakeRuntime
from tests.providers.support import passthrough_rate_limiter


def _jwt(payload: dict[str, Any]) -> str:
    header = base64.urlsafe_b64encode(b"{}").decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{header}.{body}.sig"


def _write_managed_accounts(accounts: list[dict[str, Any]]) -> Path:
    auth_path = chatgpt_oauth_auth_path()
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = {
        "version": 1,
        "tokens": dict(accounts[0]["tokens"]),
        "accounts_version": 1,
        "accounts": accounts,
    }
    auth_path.write_text(json.dumps(doc), encoding="utf-8")
    return auth_path


def _codex_entry_dict(
    slug: str,
    *,
    visibility: str = "list",
    context_window: int = 272_000,
    max_context_window: int | None = 872_000,
    efforts: tuple[str, ...] = ("low", "medium", "high"),
    modalities: tuple[str, ...] = ("text", "image"),
    service_tiers: list[dict[str, Any]] | None = None,
    additional_speed_tiers: list[str] | None = None,
    default_service_tier: str | None = None,
    description: str = "Codex test model",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "slug": slug,
        "visibility": visibility,
        "description": description,
        "context_window": context_window,
        "input_modalities": list(modalities),
        "supported_reasoning_levels": [{"effort": e} for e in efforts],
    }
    if max_context_window is not None:
        row["max_context_window"] = max_context_window
    if service_tiers is not None:
        row["service_tiers"] = service_tiers
    if additional_speed_tiers is not None:
        row["additional_speed_tiers"] = additional_speed_tiers
    if default_service_tier is not None:
        row["default_service_tier"] = default_service_tier
    return row


@pytest.mark.asyncio
@pytest.mark.real_model_sweep
async def test_m1_s1_live_etag_304_and_chatgpt_oauth_openai_dedup(
    monkeypatch, tmp_path
) -> None:
    """M1: concurrent chatgpt_oauth + openai sweep hits /codex/models once + 304 extends TTL."""
    future = int(time.time()) + 3600
    token = _jwt(
        {
            "exp": future,
            "https://api.openai.com/auth": {
                "chatgpt_plan_type": "plus",
                "chatgpt_account_id": "acct-dedup",
            },
        }
    )
    _write_managed_accounts(
        [
            {
                "id": "acct-dedup",
                "ordinal": 1,
                "origin": "mcc",
                "tokens": {
                    "access_token": token,
                    "refresh_token": "r-secret",
                    "id_token": token,
                    "account_id": "acct-dedup",
                    "expires_at": future,
                },
            }
        ]
    )
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    (codex_home / "version.json").write_text(
        json.dumps({"latest_version": "0.172.0"}), encoding="utf-8"
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    # Prevent models.dev refresh from doing any network IO during sweep.
    write_models_dev_cache({"openai": {"models": {}}}, models_dev_cache_path())

    requests_seen: list[dict[str, Any]] = []
    phase = {"step": 1}

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(
            {
                "path": request.url.path,
                "params": dict(request.url.params),
                "headers": dict(request.headers),
            }
        )
        assert request.url.path == "/backend-api/codex/models"
        assert request.url.params.get("client_version") == "0.172.0"
        assert request.headers.get("originator") == "codex_cli_rs"
        assert request.headers.get("chatgpt-account-id") == "acct-dedup"
        if phase["step"] == 1:
            return httpx.Response(
                200,
                headers={"ETag": '"v1"'},
                json={"models": [_codex_entry_dict("gpt-6.2-sol")]},
            )
        assert request.headers.get("if-none-match") == '"v1"'
        return httpx.Response(304, headers={"ETag": '"v1"'})

    transport = httpx.MockTransport(handler)
    providers: dict[str, ChatGPTOAuthProvider] = {}
    for pid in ("chatgpt_oauth", "openai"):
        p = ChatGPTOAuthProvider(
            ProviderConfig(
                api_key=CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
                base_url="https://chatgpt.com/backend-api",
            ),
            rate_limiter=passthrough_rate_limiter(),
        )
        p._client = httpx.AsyncClient(transport=transport)
        providers[pid] = p

    settings = Settings.model_validate({"MCC_CONFIG_DIR": str(tmp_path / ".mcc")})
    cache = ProviderModelCache()
    discovery = ProviderModelDiscovery(
        settings,
        lambda pid: providers[pid],
        cache,
        connected_provider_ids=("chatgpt_oauth", "openai"),
    )
    result = await discovery.refresh_model_list_cache()

    assert set(result.refreshed_provider_ids) == {"chatgpt_oauth", "openai"}
    assert len(requests_seen) == 1
    infos_by_provider = cache.cached_model_infos_by_provider()
    for pid in ("chatgpt_oauth", "openai"):
        infos = {info.model_id: info for info in infos_by_provider[pid]}
        assert "gpt-6.2-sol" in infos
        assert infos["gpt-6.2-sol"].listing is not None
        assert infos["gpt-6.2-sol"].listing.provenance is ModelListingProvenance.GATEWAY
        assert infos["gpt-6.2-sol"].listing.detail == "Live Codex API 0.172.0"

    # Disk cache persisted without storing the raw access token.
    disk_file = config_dir_path() / "cache" / CODEX_LIVE_CACHE_FILENAME
    assert disk_file.is_file()
    disk_text = disk_file.read_text(encoding="utf-8")
    assert token not in disk_text
    assert "r-secret" not in disk_text

    # Re-hydrate from disk after clearing in-memory cache -> 0 new HTTP requests.
    clear_codex_models_live_cache(remove_disk=False)
    assert await providers["chatgpt_oauth"].list_model_ids() == frozenset(
        {"gpt-6.2-sol"}
    )
    assert len(requests_seen) == 1

    # Advance monotonic clock past 300s TTL -> sends If-None-Match and extends TTL on 304.
    base_mono = time.monotonic()
    monkeypatch.setattr(
        codex_catalogue.time,
        "monotonic",
        lambda: base_mono + CODEX_MODELS_LIVE_TTL_SECONDS + 10.0,
    )
    phase["step"] = 2
    assert await providers["openai"].list_model_ids() == frozenset({"gpt-6.2-sol"})
    assert len(requests_seen) == 2

    # TTL was extended by 304, so an immediate subsequent call makes 0 HTTP requests.
    assert await providers["chatgpt_oauth"].list_model_ids() == frozenset(
        {"gpt-6.2-sol"}
    )
    assert len(requests_seen) == 2

    for p in providers.values():
        await p.cleanup()


@pytest.mark.asyncio
async def test_m2_s1_credential_safety_no_refresh_no_benching_and_error_guards(
    monkeypatch, tmp_path
) -> None:
    """M2: expired tokens skipped without refresh; 401 evicts without benching; 429/bad JSON keep stale or fall through."""
    now = int(time.time())
    expired_token = _jwt({"exp": now - 600})
    valid_token = _jwt(
        {
            "exp": now + 3600,
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct-valid"},
        }
    )
    _write_managed_accounts(
        [
            {
                "id": "acct-expired",
                "ordinal": 1,
                "origin": "mcc",
                "tokens": {
                    "access_token": expired_token,
                    "refresh_token": "r-exp",
                    "account_id": "acct-expired",
                    "expires_at": now - 600,
                },
            },
            {
                "id": "acct-valid",
                "ordinal": 2,
                "origin": "mcc",
                "tokens": {
                    "access_token": valid_token,
                    "refresh_token": "r-val",
                    "account_id": "acct-valid",
                    "expires_at": now + 3600,
                },
            },
        ]
    )

    def _forbid_refresh(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Model discovery must NEVER refresh access tokens")

    monkeypatch.setattr(credentials, "_refresh_access_token", _forbid_refresh)

    # S2a cache present so we can verify clean fall-through on 401/429/error JSON.
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    (codex_home / "models_cache.json").write_text(
        json.dumps(
            {
                "client_version": "0.172.0",
                "models": [_codex_entry_dict("gpt-s2a-fallback")],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    limiter = ProviderRateLimiter(rate_limit=10, rate_window=60, max_concurrency=5)
    mode = {"response": "200"}
    seen_auth_headers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth_headers.append(request.headers["authorization"])
        if mode["response"] == "200":
            return httpx.Response(
                200,
                headers={"ETag": '"v1"'},
                json={"models": [_codex_entry_dict("gpt-s1-live")]},
            )
        if mode["response"] == "429":
            return httpx.Response(429, headers={"Retry-After": "120"}, json={})
        if mode["response"] == "bad_json":
            return httpx.Response(200, json={"error": "upstream error"})
        if mode["response"] == "empty_models":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(401, json={"detail": "unauthorized"})

    provider = ChatGPTOAuthProvider(
        ProviderConfig(
            api_key=CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
            base_url="https://chatgpt.com/backend-api",
        ),
        rate_limiter=limiter,
    )
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    # 1. Expired account skipped, only valid account queried.
    assert await provider.list_model_ids() == frozenset({"gpt-s1-live"})
    assert seen_auth_headers == [f"Bearer {valid_token}"]
    assert limiter.remaining_wait() == 0.0

    # 2. Expire TTL -> 429 / error JSON / empty models must NOT overwrite cached snapshot or bench limiter.
    base_mono = time.monotonic()
    monkeypatch.setattr(
        codex_catalogue.time,
        "monotonic",
        lambda: base_mono + CODEX_MODELS_LIVE_TTL_SECONDS + 20.0,
    )
    for bad_mode in ("429", "bad_json", "empty_models"):
        mode["response"] = bad_mode
        assert await provider.list_model_ids() == frozenset({"gpt-s2a-fallback"})
        assert limiter.remaining_wait() == 0.0
        # Good cached snapshot was NOT overwritten by error payload.
        snap = codex_catalogue.latest_live_codex_catalogue()
        assert snap is not None
        assert {e.slug for e in snap.catalogue.entries} == {"gpt-s1-live"}

    # 3. 401 evicts stale live snapshot, never benches limiter, and falls through to S2a.
    mode["response"] = "401"
    assert await provider.list_model_ids() == frozenset({"gpt-s2a-fallback"})
    assert limiter.remaining_wait() == 0.0
    assert codex_catalogue.latest_live_codex_catalogue() is None

    await provider.cleanup()


@pytest.mark.asyncio
async def test_m3_s1_and_s2a_suppress_stale_s2b_and_desktop_paths_work(
    monkeypatch, tmp_path
) -> None:
    """M3: S1 and S2a suppress stale gpt-5.5 in S2b; Desktop binary fallback works when S1/S2a decline."""
    from my_claude_code.providers.chatgpt_oauth import provider as provider_module

    stale_s2b = CodexCatalogue(
        version="0.151.0",
        source_path="codex.exe",
        entries=(
            CodexCatalogueEntry(slug="gpt-5.5", visibility="list"),
            CodexCatalogueEntry(slug="gpt-6.2-sol", visibility="list"),
        ),
    )
    s2b_calls: list[str] = []

    def _mock_s2b() -> CodexCatalogue:
        s2b_calls.append("called")
        return stale_s2b

    monkeypatch.setattr(provider_module, "load_codex_catalogue", _mock_s2b)

    # Case A: S1 live answers without gpt-5.5 -> S2b never called, gpt-5.5 absent.
    future = int(time.time()) + 3600
    token = _jwt({"exp": future})
    p = ChatGPTOAuthProvider(
        ProviderConfig(api_key=token, base_url="https://chatgpt.com/backend-api"),
        rate_limiter=passthrough_rate_limiter(),
    )
    p._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(
                200, json={"models": [_codex_entry_dict("gpt-6.2-sol")]}
            )
        )
    )
    ids_s1 = await p.list_model_ids()
    assert ids_s1 == frozenset({"gpt-6.2-sol"})
    assert "gpt-5.5" not in ids_s1
    assert s2b_calls == []
    await p.cleanup()

    # Case B: S1 declines (no credentials), S2a models_cache.json answers without gpt-5.5 -> S2b never called.
    clear_codex_models_live_cache(remove_disk=True)
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    (codex_home / "models_cache.json").write_text(
        json.dumps(
            {
                "client_version": "0.172.0",
                "models": [_codex_entry_dict("gpt-6.2-sol")],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    p_offline = ChatGPTOAuthProvider(
        ProviderConfig(api_key="", base_url="https://chatgpt.com/backend-api"),
        rate_limiter=passthrough_rate_limiter(),
    )
    infos_s2a = await p_offline.list_model_infos()
    assert {info.model_id for info in infos_s2a} == {"gpt-6.2-sol"}
    assert s2b_calls == []
    (s2a_info,) = infos_s2a
    assert s2a_info.listing is not None
    assert s2a_info.listing.detail == "Codex cache 0.172.0"

    # Case C: Remove S2a -> S2b Desktop binary path (%LOCALAPPDATA%/OpenAI/Codex/bin/*/codex.exe) answers.
    (codex_home / "models_cache.json").unlink()
    codex_catalogue.clear_codex_catalogue_cache()
    monkeypatch.undo()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(codex_catalogue, "_codex_package_dirs", list)
    localappdata = tmp_path / "localappdata"
    desktop_bin = localappdata / "OpenAI" / "Codex" / "bin" / "abc123" / "codex.exe"
    desktop_bin.parent.mkdir(parents=True)
    desktop_bin.write_bytes(
        b"binpad"
        + json.dumps(
            {"models": [_codex_entry_dict("gpt-desktop-model")]}, indent=2
        ).encode("utf-8")
        + b"tail"
    )
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))

    desktop_cat = load_codex_catalogue()
    assert desktop_cat is not None
    assert {e.slug for e in desktop_cat.entries} == {"gpt-desktop-model"}
    assert await p_offline.list_model_ids() == frozenset({"gpt-desktop-model"})
    await p_offline.cleanup()


@pytest.mark.asyncio
async def test_m4_multi_account_union_behind_rotating_provider(
    monkeypatch, tmp_path
) -> None:
    """M4: RotatingProvider wrapping 2 ChatGPT accounts still unions both accounts via S1."""
    future = int(time.time()) + 3600
    tok_plus = _jwt(
        {
            "exp": future,
            "https://api.openai.com/auth": {
                "chatgpt_plan_type": "plus",
                "chatgpt_account_id": "acct-plus",
            },
        }
    )
    tok_pro = _jwt(
        {
            "exp": future,
            "https://api.openai.com/auth": {
                "chatgpt_plan_type": "pro",
                "chatgpt_account_id": "acct-pro",
            },
        }
    )
    _write_managed_accounts(
        [
            {
                "id": "acct-plus",
                "ordinal": 1,
                "origin": "mcc",
                "tokens": {
                    "access_token": tok_plus,
                    "refresh_token": "r1",
                    "id_token": tok_plus,
                    "account_id": "acct-plus",
                    "expires_at": future,
                },
            },
            {
                "id": "acct-pro",
                "ordinal": 2,
                "origin": "mcc",
                "tokens": {
                    "access_token": tok_pro,
                    "refresh_token": "r2",
                    "id_token": tok_pro,
                    "account_id": "acct-pro",
                    "expires_at": future,
                },
            },
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        acct = request.headers.get("chatgpt-account-id")
        if acct == "acct-plus":
            return httpx.Response(
                200,
                json={
                    "models": [
                        _codex_entry_dict("gpt-6-luna"),
                        _codex_entry_dict("gpt-pro-hidden-on-plus", visibility="hide"),
                    ]
                },
            )
        if acct == "acct-pro":
            return httpx.Response(
                200,
                json={
                    "models": [
                        _codex_entry_dict("gpt-6-luna"),
                        _codex_entry_dict("gpt-6.2-sol"),
                        _codex_entry_dict("gpt-pro-hidden-on-plus", visibility="list"),
                    ]
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    settings = Settings.model_validate(
        {
            "MCC_CONFIG_DIR": str(tmp_path / ".mcc"),
            "CHATGPT_OAUTH_ACCESS_TOKEN": CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
        }
    )
    provider = create_provider("chatgpt_oauth", settings)
    # Attach MockTransport to every leaf ChatGPTOAuthProvider inside RotatingProvider.
    leaves = getattr(provider, "_providers", [provider])
    assert len(leaves) == 2
    for leaf in leaves:
        leaf._client = httpx.AsyncClient(transport=transport)

    infos = await provider.list_model_infos()
    assert {info.model_id for info in infos} == {
        "gpt-6-luna",
        "gpt-6.2-sol",
        "gpt-pro-hidden-on-plus",
    }
    for leaf in leaves:
        await leaf.cleanup()


@pytest.mark.asyncio
async def test_m5_option_a_capabilities_ultra_clamp_models_dev_gap_fill_and_published_row(
    monkeypatch, tmp_path
) -> None:
    """M5: Option A populates Codex limits/efforts/vision/published_row while models.dev fills pricing."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    codex_row = _codex_entry_dict(
        "gpt-6.2-sol",
        context_window=500_000,
        max_context_window=872_000,
        modalities=("text", "image"),
        efforts=("low", "medium", "high", "xhigh", "ultra"),
        service_tiers=[{"id": "priority", "name": "Fast", "description": "2x speed"}],
        additional_speed_tiers=["fast"],
        default_service_tier="priority",
        description="Codex flagship",
    )
    (codex_home / "models_cache.json").write_text(
        json.dumps({"client_version": "0.172.0", "models": [codex_row]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    provider = ChatGPTOAuthProvider(
        ProviderConfig(api_key="", base_url="https://chatgpt.com/backend-api"),
        rate_limiter=passthrough_rate_limiter(),
    )
    raw_infos = await provider.list_model_infos()
    (raw_info,) = raw_infos
    assert raw_info.context_length == 872_000
    assert raw_info.supports_vision is True
    assert raw_info.supports_thinking is True
    assert raw_info.reasoning_capability is not None
    assert raw_info.reasoning_capability.supported_efforts == frozenset(
        {
            ReasoningEffort.LOW,
            ReasoningEffort.MEDIUM,
            ReasoningEffort.HIGH,
            ReasoningEffort.XHIGH,
            ReasoningEffort.MAX,
        }
    )
    assert raw_info.input_price is None
    assert raw_info.output_price is None
    assert raw_info.published_row is not None

    # Enrich with models.dev claiming 1050000 context + $2.5/$15.0 pricing.
    models_dev_index: dict[str, Any] = {
        "openai": {
            "models": {
                "gpt-6.2-sol": {
                    "limit": {"context": 1_050_000, "output": 128_000},
                    "cost": {"input": 2.5, "output": 15.0},
                    "modalities": {"input": ["text", "image"], "output": ["text"]},
                }
            }
        }
    }
    write_models_dev_cache(models_dev_index, models_dev_cache_path())
    enriched_tuple = enrich_model_infos(
        raw_infos, models_dev_index, provider_id="chatgpt_oauth"
    )
    (enriched,) = enriched_tuple
    assert enriched.context_length == 872_000
    assert enriched.input_price == 2.5
    assert enriched.output_price == 15.0

    # Flows into build_catalogue_models -> build_codex_catalogue + Models page + knowledge.
    namespaced = ProviderModelInfo(
        model_id="chatgpt_oauth/gpt-6.2-sol",
        supports_thinking=enriched.supports_thinking,
        supports_vision=enriched.supports_vision,
        context_length=enriched.context_length,
        input_price=enriched.input_price,
        output_price=enriched.output_price,
        max_output_tokens=enriched.max_output_tokens,
        reasoning_capability=enriched.reasoning_capability,
        declared=enriched.declared,
        listing=enriched.listing,
        published_row=enriched.published_row,
    )
    runtime = FakeRuntime(
        settings=Settings.model_validate({"MODEL": "chatgpt_oauth/gpt-6.2-sol"}),
        cached_infos=(namespaced,),
    )
    models = build_catalogue_models(runtime.current_settings(), runtime)
    document, _ = build_codex_catalogue(models)
    by_slug = {entry["slug"]: entry for entry in document["models"]}
    codex_out = by_slug["chatgpt_oauth/gpt-6.2-sol"]
    assert codex_out["context_window"] == 872_000
    assert codex_out["service_tiers"] == [
        {"id": "priority", "name": "Fast", "description": "2x speed"}
    ]
    assert codex_out["additional_speed_tiers"] == ["fast"]
    assert codex_out["default_service_tier"] == "priority"

    app = create_test_app(
        Settings.model_validate(
            {
                "CHATGPT_OAUTH_ACCESS_TOKEN": "tok-m5",
                "MODEL": "chatgpt_oauth/gpt-6.2-sol",
            }
        )
    )
    manager = provider_manager_for_app(app)
    manager.cache_model_infos("chatgpt_oauth", (enriched,))
    knowledge = _model_knowledge(app.state.services, "chatgpt_oauth/gpt-6.2-sol")
    assert knowledge is not None
    cap_fields = {f["key"]: f for f in knowledge["fields"]}
    assert cap_fields["context_length"]["used_source"] == KNOWLEDGE_SOURCE_PROVIDER
    assert cap_fields["context_length"]["used"]["value"] == 872_000
    assert cap_fields["context_length"]["used"]["source"] == SOURCE_PROVIDER
    assert cap_fields["supports_vision"]["used_source"] == KNOWLEDGE_SOURCE_PROVIDER
    assert cap_fields["supports_vision"]["used"]["source"] == SOURCE_PROVIDER
    page = build_models_page_payload(
        (namespaced,), (), ModelVisibility(), ModelParameterOverrides()
    )
    model_row = page["providers"][0]["models"][0]
    assert model_row["capabilities"]["context_length"]["value"] == 872_000
    assert model_row["capabilities"]["supports_vision"]["source"] == SOURCE_PROVIDER
    await provider.cleanup()


def test_resolve_codex_client_version_offline_precedence(monkeypatch, tmp_path) -> None:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    (pkg_dir / "package.json").write_text(
        json.dumps({"version": "0.170.0"}), encoding="utf-8"
    )
    monkeypatch.setattr(codex_catalogue, "_codex_package_dirs", lambda: [pkg_dir])

    # 1. version.json wins.
    (codex_home / "version.json").write_text(
        json.dumps({"latest_version": "0.175.0"}), encoding="utf-8"
    )
    (codex_home / "models_cache.json").write_text(
        json.dumps({"client_version": "0.173.0", "models": []}), encoding="utf-8"
    )
    assert resolve_codex_client_version() == "0.175.0"

    # 2. models_cache.json next.
    (codex_home / "version.json").unlink()
    assert resolve_codex_client_version() == "0.173.0"

    # 3. package.json next.
    (codex_home / "models_cache.json").unlink()
    assert resolve_codex_client_version() == "0.170.0"

    # 4. Floor constant last.
    monkeypatch.setattr(codex_catalogue, "_codex_package_dirs", list)
    assert resolve_codex_client_version() == CODEX_CLIENT_VERSION_FLOOR


def test_validate_codex_models_document_and_models_cache_loader(tmp_path) -> None:
    assert validate_codex_models_document(None) is None
    assert validate_codex_models_document({"error": "bad"}) is None
    assert validate_codex_models_document({"models": []}) is None
    assert validate_codex_models_document({"models": [{"slug": "  "}]}) is None
    assert validate_codex_models_document({"models": [{"slug": "gpt-6"}]}) == {
        "models": [{"slug": "gpt-6"}]
    }

    cache_file = tmp_path / "models_cache.json"
    cache_file.write_text(
        json.dumps(
            {
                "client_version": "0.172.0",
                "models": [_codex_entry_dict("gpt-6-luna")],
            }
        ),
        encoding="utf-8",
    )
    loaded = load_codex_models_cache(cache_path=cache_file)
    assert loaded is not None
    assert loaded.version == "0.172.0"
    assert [e.slug for e in loaded.entries] == ["gpt-6-luna"]
