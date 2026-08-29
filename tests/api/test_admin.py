import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.application.model_metadata import (
    ProviderModelInfo,
    ProviderModelRefreshResult,
)
from my_claude_code.application.release_updates import UpgradeResult
from my_claude_code.config.admin.values import MASKED_SECRET
from my_claude_code.config.server_urls import local_admin_url
from my_claude_code.config.settings import Settings
from my_claude_code.providers.base import BaseProvider, ProviderConfig
from my_claude_code.providers.chatgpt_oauth.browser_login import (
    ChatGPTOAuthBrowserUnavailableError,
)
from my_claude_code.providers.credential_rotation import CredentialRotationState
from my_claude_code.providers.runtime.rotating import RotatingProvider
from tests.api.support import create_test_app, provider_manager_for_app


@pytest.mark.asyncio
async def test_successful_upgrade_schedules_process_restart_after_response(
    monkeypatch,
) -> None:
    process_restart = AsyncMock()
    app = create_test_app(process_restart_callback=process_restart)

    async def successful_upgrade() -> UpgradeResult:
        return UpgradeResult(
            ok=True,
            message="Installed 9.9.9; restarting.",
            installed_version="9.9.9",
        )

    monkeypatch.setattr(
        "my_claude_code.api.admin_routes.perform_upgrade", successful_upgrade
    )

    with _local_client(app) as client:
        response = client.post("/admin/api/version/upgrade", json={})

    assert response.status_code == 200
    assert response.json()["automatic_restart"] is True
    assert response.json()["installed_version"] == "9.9.9"
    process_restart.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_upgrade_does_not_restart(monkeypatch) -> None:
    process_restart = AsyncMock()
    app = create_test_app(process_restart_callback=process_restart)

    async def failed_upgrade() -> UpgradeResult:
        return UpgradeResult(ok=False, message="checksum mismatch")

    monkeypatch.setattr(
        "my_claude_code.api.admin_routes.perform_upgrade", failed_upgrade
    )

    with _local_client(app) as client:
        response = client.post("/admin/api/version/upgrade", json={})

    assert response.status_code == 200
    assert response.json()["automatic_restart"] is False
    process_restart.assert_not_awaited()


def _local_client(app):
    return TestClient(app, client=("127.0.0.1", 50000))


def _set_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)


def _clear_process_config(monkeypatch) -> None:
    for key in (
        "MODEL",
        "NVIDIA_NIM_API_KEY",
        "HUGGINGFACE_API_KEY",
        "OPENROUTER_API_KEY",
        "OLLAMA_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "TELEGRAM_PROXY_URL",
        "FCC_ENV_FILE",
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
        "GITHUB_MODELS_TOKEN",
        "SAMBANOVA_API_KEY",
        "HOST",
        "PORT",
        "FCC_OPEN_BROWSER",
        "VOICE_NOTE_ENABLED",
        "WHISPER_DEVICE",
        "LOG_FILE",
        "ZAI_BASE_URL",
        "CLAUDE_WORKSPACE",
        "CLAUDE_CLI_BIN",
        "WSL_DISTRO_NAME",
        "WSL_INTEROP",
        "SSH_CONNECTION",
        "SSH_CLIENT",
        "SSH_TTY",
        "CODESPACES",
        "GITPOD_WORKSPACE_ID",
        "REMOTE_CONTAINERS",
    ):
        monkeypatch.delenv(key, raising=False)


def test_admin_page_is_loopback_only(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    app = create_test_app()

    assert _local_client(app).get("/admin").status_code == 200
    remote_client = TestClient(app, client=("203.0.113.10", 50000))
    assert remote_client.get("/admin").status_code == 403


@pytest.mark.parametrize(
    "path",
    (
        "/admin",
        "/admin/assets/admin.css",
        "/admin/assets/admin.js",
        "/admin/api/config",
    ),
)
def test_admin_responses_are_never_cached(monkeypatch, tmp_path, path):
    _set_home(monkeypatch, tmp_path)
    response = _local_client(create_test_app()).get(path)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("path", "client_host", "expected_status"),
    (
        ("/admin", "203.0.113.10", 403),
        ("/admin/assets/missing.js", "127.0.0.1", 404),
    ),
)
def test_admin_http_errors_are_never_cached(
    monkeypatch,
    tmp_path,
    path,
    client_host,
    expected_status,
):
    _set_home(monkeypatch, tmp_path)
    client = TestClient(create_test_app(), client=(client_host, 50000))

    response = client.get(path)

    assert response.status_code == expected_status
    assert response.headers["cache-control"] == "no-store"


def test_admin_validation_errors_are_never_cached(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)

    response = _local_client(create_test_app()).post(
        "/admin/api/config/validate",
        content="{",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"


def test_admin_unexpected_errors_are_never_cached(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    client = TestClient(
        create_test_app(),
        client=("127.0.0.1", 50000),
        raise_server_exceptions=False,
    )

    with patch(
        "my_claude_code.api.admin_routes.load_config_response",
        side_effect=RuntimeError("test error"),
    ):
        response = client.get("/admin/api/config")

    assert response.status_code == 500
    assert response.headers["cache-control"] == "no-store"


def test_admin_cache_policy_does_not_match_similar_public_paths(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)

    response = _local_client(create_test_app()).get("/administrator")

    assert response.status_code == 404
    assert "cache-control" not in response.headers


def test_admin_api_fetches_bypass_browser_cache():
    script = Path("src/my_claude_code/api/admin_static/admin.js").read_text(
        encoding="utf-8"
    )

    assert 'cache: "no-store"' in script


def test_admin_static_exposes_explicit_chatgpt_oauth_login_methods():
    script = Path("src/my_claude_code/api/admin_static/admin.js").read_text(
        encoding="utf-8"
    )

    assert '"Log in with device code"' in script
    assert '"Browser login (same device)"' in script
    assert "/chatgpt-oauth/browser/initiate?same_host_confirmed=true" in script
    assert 'accountField.value = accountId || "";' in script
    assert "startChatGPTOAuthDeviceLogin(deviceButton, loginButtons)" in script
    assert "startChatGPTOAuthBrowserLogin(browserButton, loginButtons)" in script


def test_admin_static_exposes_professional_observability_controls():
    html = Path("src/my_claude_code/api/admin_static/index.html").read_text(
        encoding="utf-8"
    )
    script = Path("src/my_claude_code/api/admin_static/admin.js").read_text(
        encoding="utf-8"
    )
    styles = Path("src/my_claude_code/api/admin_static/admin.css").read_text(
        encoding="utf-8"
    )

    for control_id in (
        "reqAutoRefresh",
        "reqFilterEndpoint",
        "reqPageSize",
        "reqExportButton",
        "reqProviderBreakdown",
        "reqTopErrors",
        "webSearchFilterProvider",
        "webSearchFilterStatus",
        "webSearchFilterWindow",
        "webSearchStatsPeriod",
        "webSearchExportButton",
        "webSearchClearButton",
        "webSearchRouteSummary",
        "webSearchDetailModal",
        "webSearchDetailConfig",
        "webSearchDetailInput",
        "webSearchDetailSummary",
        "webSearchDetailOutput",
    ):
        assert f'id="{control_id}"' in html
    assert 'label: "Analytics"' in script
    assert 'title: "Observability"' in script
    assert 'params.set("endpoint", endpoint)' in script
    assert "stats.p50_duration_ms" in script
    assert "stats.top_errors" in script
    assert "stats?.dropped_records" in script
    assert "effectiveWebSearchProvider" in script
    assert "WEB_SEARCH_FALLBACK_POLICY" in script
    assert "ws-hero-path-primary" in script
    assert "ws-hero-path-fallback" in script
    assert '"Logical searches"' in script
    assert '"Fallback rate"' in script
    assert '"Terminal route outcomes"' in script
    assert "stats?.routes?.series" in script
    assert "renderWebSearchObservedRoute" in script
    assert "webSearchAnalyticsStatsKey" in script
    assert "Showing the last successful" in script
    assert "openWebSearchDetail" in script
    assert "renderWebSearchResponseSummary" in script
    assert 'params.set("include_content", "true")' in script
    assert "Full normalized provider input/output is captured" in script
    assert "trapWebSearchDetailFocus" in script
    assert "bucket boundaries use UTC" in script

    # Export window + site persistence.
    for control_id in (
        "exportModal",
        "exportDownloadButton",
        "exportFormat",
        "exportPeriod",
        "exportSince",
        "exportUntil",
        "exportGroupBy",
        "exportFieldList",
        "exportClose",
        "exportCustomRange",
    ):
        assert f'id="{control_id}"' in html
    assert ">Export<" in html
    assert ">Export JSON<" not in html
    for fn in (
        "openExportModal",
        "closeExportModal",
        "trapExportModalFocus",
        "runExport",
        "downloadBlob",
        "downloadJson",
        "persistDashboardState",
        "restoreDashboardState",
    ):
        assert fn in script
    assert '"mcc-dashboard-state"' in script
    assert "exportScope" in script
    assert "Group by" in html
    assert "trapRequestDetailFocus" in script
    assert "View request" in script
    assert "all stored rows will be deleted" in script
    assert 'clearChart(byId("reqSeriesChart"))' in script
    assert 'tr.addEventListener("click"' not in script
    assert "chart bucket boundaries use UTC" in html
    assert "setInterval" in script
    assert "downloadJson" in script
    assert ".route-summary" in styles
    assert ".requests-breakdowns" in styles
    assert ".table-scroll" in styles
    assert ".websearch-result-summary" in styles


def test_admin_page_no_longer_renders_generated_env_panel(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    app = create_test_app()

    response = _local_client(app).get("/admin")

    assert response.status_code == 200
    assert "Generated Env" not in response.text
    assert "envPreview" not in response.text


def test_admin_static_renders_and_binds_rtk_token_optimizer_card():
    html = Path("src/my_claude_code/api/admin_static/index.html").read_text(
        encoding="utf-8"
    )
    script = Path("src/my_claude_code/api/admin_static/admin.js").read_text(
        encoding="utf-8"
    )
    styles = Path("src/my_claude_code/api/admin_static/admin.css").read_text(
        encoding="utf-8"
    )

    # The three toggle checkboxes live in a "Token optimizer" card.
    for control_id in ("rtkClaude", "rtkCodex", "rtkPi", "rtkStatusLine"):
        assert f'id="{control_id}"' in html

    # Each toggle binds to the shared POST endpoint and the shared reconciler
    # path (the server applies the persisted state; the UI only sends the flag).
    assert 'api("/admin/api/rtk", {' in script
    assert 'api("/admin/api/rtk")' in script
    assert "loadRtkState" in script
    assert "renderRtkState" in script
    assert "updateRtk" in script
    assert (
        'updateRtk("claude", event.currentTarget.checked, event.currentTarget)'
        in script
    )
    assert (
        'updateRtk("codex", event.currentTarget.checked, event.currentTarget)' in script
    )
    assert 'updateRtk("pi", event.currentTarget.checked, event.currentTarget)' in script
    assert ".rtk-status-line" in styles
    assert ".rtk-status-line.ok" in styles
    assert ".rtk-status-line.warn" in styles
    assert ".rtk-status-line.error" in styles


def test_admin_static_renders_and_binds_desktop_window_control():
    """The Window select must be a real ``<select>`` in the Deployment section,
    wired to the same POST/GET desktop endpoints as the existing controls --
    not merely present as inert markup.

    Asserted against the source rather than a rendered page: the suite has no
    DOM harness (see ``test_admin_static_keeps_every_field_input_in_the_document``
    for the established rationale), so this only proves the markup and script
    reference each other by id, not that a browser renders or updates it.
    """
    html = Path("src/my_claude_code/api/admin_static/index.html").read_text(
        encoding="utf-8"
    )
    script = Path("src/my_claude_code/api/admin_static/admin.js").read_text(
        encoding="utf-8"
    )

    # The select and its two read-only helper lines live inside the existing
    # Deployment section, not a new one.
    deployment_start = html.index('aria-label="Deployment"')
    deployment_end = html.index("</section>", deployment_start)
    deployment_html = html[deployment_start:deployment_end]
    assert 'id="desktopWindow"' in deployment_html
    assert 'id="desktopWindowHint"' in deployment_html
    assert 'id="desktopWindowResolved"' in deployment_html

    for option_value in ("auto", "app-mode", "pywebview", "browser"):
        assert f'value="{option_value}"' in deployment_html

    # Wired: byId lookup, render function, and a change listener that posts
    # through the same updateDesktop/api("/admin/api/desktop", ...) path the
    # server mode select uses.
    assert 'byId("desktopWindow")' in script
    assert "renderDesktopWindow" in script
    assert 'byId("desktopWindow").addEventListener("change", (event) => {' in script
    assert (
        'updateDesktop("window", event.currentTarget.value, event.currentTarget);'
        in script
    )
    assert "window_auto_provider" in script
    assert "window_auto_reason" in script


def test_admin_page_no_longer_renders_global_status_header(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    app = create_test_app()

    response = _local_client(app).get("/admin")

    assert response.status_code == 200
    assert "Local Admin" not in response.text
    assert "serverStatus" not in response.text
    assert "modelBadge" not in response.text


def test_admin_static_no_longer_fetches_global_status_header():
    script = Path("src/my_claude_code/api/admin_static/admin.js").read_text(
        encoding="utf-8"
    )

    assert 'api("/admin/api/status")' not in script
    assert "updateHeader" not in script
    assert '"Running"' not in script
    assert "serverStatus" not in script
    assert "modelBadge" not in script


def test_admin_static_names_the_managed_source_label():
    """A stored value has to look like one.

    This label used to be blank, on the reasoning that "managed here" was the
    normal case and not worth a badge. It stopped being true once the managed
    file only records choices: a value written from the dashboard is now the
    one thing that outranks a changed default, so the field has to say so.
    """

    script = Path("src/my_claude_code/api/admin_static/admin.js").read_text(
        encoding="utf-8"
    )

    assert 'managed_env: "set here",' in script
    assert "hasOwnProperty.call(labels, source)" in script
    assert 'parts.push("locked")' in script
    assert "sourceEl.textContent = source" in script


def test_admin_static_places_reasoning_fields_in_model_config():
    script = Path("src/my_claude_code/api/admin_static/admin.js").read_text(
        encoding="utf-8"
    )

    assert 'sections: ["models", "reasoning", "web_tools"]' in script
    assert 'sections: ["models", "thinking", "web_tools"]' not in script


def test_admin_static_model_combobox_owns_dropdown_and_search_behavior():
    script = Path("src/my_claude_code/api/admin_static/admin.js").read_text(
        encoding="utf-8"
    )
    styles = Path("src/my_claude_code/api/admin_static/admin.css").read_text(
        encoding="utf-8"
    )

    assert 'api("/admin/api/models" + (refresh ? "/refresh" : "")' in script
    assert 'field.type === "model" || field.type === "optional_model"' in script
    assert 'input.setAttribute("role", "combobox")' in script
    assert 'listbox.setAttribute("role", "listbox")' in script
    assert 'toggle.className = "model-combobox-toggle"' in script
    assert "class ModelCombobox" in script
    assert 'input.addEventListener("click", () => this.open())' in script
    assert "value.toLocaleLowerCase().includes(normalizedQuery)" in script
    assert 'event.key === "ArrowDown" || event.key === "ArrowUp"' in script
    assert "this.setActive(this.visibleOptions.length - 1)" in script
    assert 'event.key === "Enter"' in script
    assert 'event.key === "Escape"' in script
    assert 'document.createElement("datalist")' not in script
    assert ".model-combobox-list" in styles
    assert ".model-combobox-option.active" in styles
    assert styles.count("background-image: var(--dropdown-chevron)") == 2


def test_admin_static_model_combobox_preserves_custom_slugs_and_none_semantics():
    script = Path("src/my_claude_code/api/admin_static/admin.js").read_text(
        encoding="utf-8"
    )

    assert '? ["None", ...state.modelOptions]' in script
    assert "You can still enter a custom slug." in script
    assert 'input.dataset.fieldType === "optional_model"' in script
    assert 'return "";' in script
    assert "await hydrateModelOptions();" in script
    assert "Model fields remain editable" in script
    assert "result.failed_providers || []" in script
    assert '"warn"' in script


def test_admin_config_masks_secrets_and_exposes_manifest(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()

    response = _local_client(app).get("/admin/api/config")

    assert response.status_code == 200
    body = response.json()
    keys = {field["key"] for field in body["fields"]}
    assert "MODEL_FABLE" in keys
    assert "REASONING_FABLE" in keys
    assert "ANTHROPIC_AUTH_TOKEN" in keys
    assert "OPENROUTER_API_KEY" in keys
    assert "FIREWORKS_API_KEY" in keys
    assert "CLOUDFLARE_API_TOKEN" in keys
    assert "CLOUDFLARE_ACCOUNT_ID" in keys
    assert "GITHUB_MODELS_TOKEN" in keys
    assert "GEMINI_API_KEY" in keys
    assert "GROQ_API_KEY" in keys
    assert "SAMBANOVA_API_KEY" in keys
    assert "TELEGRAM_PROXY_URL" in keys
    assert "CEREBRAS_API_KEY" in keys
    assert "OLLAMA_API_KEY" in keys
    assert "FCC_OPEN_BROWSER" in keys
    assert "ZAI_BASE_URL" not in keys
    assert "CLAUDE_WORKSPACE" not in keys
    assert "CLAUDE_CLI_BIN" not in keys
    assert "LOG_FILE" not in keys
    auth_field = next(
        field for field in body["fields"] if field["key"] == "ANTHROPIC_AUTH_TOKEN"
    )
    assert auth_field["secret"] is True
    assert auth_field["value"] == MASKED_SECRET
    assert auth_field["source"] == "template"
    telegram_proxy_field = next(
        field for field in body["fields"] if field["key"] == "TELEGRAM_PROXY_URL"
    )
    assert telegram_proxy_field["secret"] is True
    open_browser_field = next(
        field for field in body["fields"] if field["key"] == "FCC_OPEN_BROWSER"
    )
    assert open_browser_field["type"] == "boolean"
    assert open_browser_field["value"] == "true"
    assert open_browser_field["restart_required"] is False
    model_field_types = {
        field["key"]: field["type"]
        for field in body["fields"]
        if field["key"]
        in {"MODEL", "MODEL_FABLE", "MODEL_OPUS", "MODEL_SONNET", "MODEL_HAIKU"}
    }
    assert model_field_types == {
        "MODEL": "model",
        "MODEL_FABLE": "optional_model",
        "MODEL_OPUS": "optional_model",
        "MODEL_SONNET": "optional_model",
        "MODEL_HAIKU": "optional_model",
    }
    reasoning_policy = next(
        field for field in body["fields"] if field["key"] == "REASONING_POLICY"
    )
    assert reasoning_policy["section"] == "reasoning"
    assert reasoning_policy["type"] == "select"
    assert reasoning_policy["value"] == "client"
    assert reasoning_policy["options"] == [
        {"value": "off", "label": "Off"},
        {"value": "client", "label": "From client"},
        {"value": "adaptive", "label": "Adaptive"},
        {"value": "low", "label": "Low"},
        {"value": "medium", "label": "Medium"},
        {"value": "high", "label": "High"},
        {"value": "xhigh", "label": "X-High"},
        {"value": "max", "label": "Max"},
    ]
    route_reasoning = next(
        field for field in body["fields"] if field["key"] == "REASONING_FABLE"
    )
    assert route_reasoning["options"] == [
        {"value": "inherit", "label": "Inherit"},
        *reasoning_policy["options"],
    ]
    restart_required = {
        field["key"] for field in body["fields"] if field["restart_required"] is True
    }
    assert {
        "ANTHROPIC_AUTH_TOKEN",
        "DEBUG_PLATFORM_EDITS",
        "DEBUG_SUBAGENT_STACK",
        "LOG_RAW_API_PAYLOADS",
        "LOG_API_ERROR_TRACEBACKS",
        "LOG_RAW_MESSAGING_CONTENT",
        "LOG_RAW_CLI_DIAGNOSTICS",
        "LOG_MESSAGING_ERROR_DETAILS",
    } <= restart_required


def test_admin_models_include_configured_and_cached_canonical_slugs():
    settings = Settings()
    settings.model = "nvidia_nim/configured-model"
    settings.model_opus = "open_router/anthropic/configured-opus"
    settings.open_router_api_key = "open-router-key"
    app = create_test_app(settings)
    provider_manager_for_app(app).cache_model_infos(
        "open_router",
        {
            ProviderModelInfo("anthropic/configured-opus"),
            ProviderModelInfo("meta/llama-3.3"),
        },
    )

    response = _local_client(app).get("/admin/api/models")

    assert response.status_code == 200
    assert response.json() == {
        "models": [
            "nvidia_nim/configured-model",
            "open_router/anthropic/configured-opus",
            "open_router/meta/llama-3.3",
        ],
        "failed_providers": [],
        "blind_models": [],
    }


def test_admin_models_apply_the_visibility_filter_to_discovered_models():
    settings = Settings()
    settings.model = "nvidia_nim/configured-model"
    settings.open_router_api_key = "open-router-key"
    settings.model_visibility_deny = "open_router/meta/*"
    app = create_test_app(settings)
    provider_manager_for_app(app).cache_model_infos(
        "open_router",
        {
            ProviderModelInfo("anthropic/kept"),
            ProviderModelInfo("meta/llama-3.3", supports_vision=False),
        },
    )

    body = _local_client(app).get("/admin/api/models").json()

    assert body["models"] == [
        "nvidia_nim/configured-model",
        "open_router/anthropic/kept",
    ]
    # A hidden model has nothing to say about vision either.
    assert body["blind_models"] == []


def test_admin_models_keep_a_configured_model_that_the_filter_hides():
    """A picker must be able to render the value that is actually saved.

    Dropping a hidden-but-configured ref would leave the select empty while
    the route it names carried on serving traffic.
    """
    settings = Settings()
    settings.model = "nvidia_nim/configured-model"
    settings.model_visibility_allow = "open_router/*"
    app = create_test_app(settings)

    body = _local_client(app).get("/admin/api/models").json()

    assert body["models"] == ["nvidia_nim/configured-model"]


def test_admin_model_refresh_returns_the_updated_canonical_catalog():
    settings = Settings()
    settings.model = "deepseek/deepseek-chat"
    settings.deepseek_api_key = "deepseek-key"
    app = create_test_app(settings)
    runtime = app.state.services.admin

    async def refresh_models() -> ProviderModelRefreshResult:
        provider_manager_for_app(app).cache_model_infos(
            "deepseek",
            {ProviderModelInfo("deepseek-reasoner")},
        )
        return ProviderModelRefreshResult(refreshed_provider_ids=("deepseek",))

    runtime.refresh_models = AsyncMock(side_effect=refresh_models)

    response = _local_client(app).post("/admin/api/models/refresh")

    assert response.status_code == 200
    assert response.json() == {
        "models": ["deepseek/deepseek-chat", "deepseek/deepseek-reasoner"],
        "failed_providers": [],
        "blind_models": [],
    }
    runtime.refresh_models.assert_awaited_once_with()


def test_admin_model_refresh_reports_partial_provider_failures():
    settings = Settings()
    settings.model = "deepseek/deepseek-chat"
    app = create_test_app(settings)
    runtime = app.state.services.admin
    runtime.refresh_models = AsyncMock(
        return_value=ProviderModelRefreshResult(
            refreshed_provider_ids=("deepseek",),
            failed_provider_ids=("open_router",),
        )
    )

    response = _local_client(app).post("/admin/api/models/refresh")

    assert response.status_code == 200
    assert response.json() == {
        "models": ["deepseek/deepseek-chat"],
        "failed_providers": ["open_router"],
        "blind_models": [],
    }


def test_admin_config_preserves_managed_env_source_contract(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    env_file = tmp_path / ".fcc" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("MODEL=open_router/managed-model\n", encoding="utf-8")
    app = create_test_app()

    response = _local_client(app).get("/admin/api/config")

    assert response.status_code == 200
    body = response.json()
    model_field = next(field for field in body["fields"] if field["key"] == "MODEL")
    assert model_field["source"] == "managed_env"
    assert model_field["locked"] is False


def test_admin_apply_persists_open_browser_for_next_launch(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()

    response = _local_client(app).post(
        "/admin/api/config/apply",
        json={"values": {"FCC_OPEN_BROWSER": False}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert body["pending_fields"] == []
    assert body["restart"] == {
        "required": False,
        "automatic": False,
        "admin_url": None,
        "fields": [],
    }
    managed_env = tmp_path / ".fcc" / ".env"
    assert "FCC_OPEN_BROWSER=false" in managed_env.read_text(encoding="utf-8")


def test_apply_with_an_unrelated_field_does_not_write_other_defaults(
    monkeypatch, tmp_path
):
    """Saving one field must not record a choice for every other field.

    It used to: the first Save materialised every manifest default into the
    managed file, so a later release could never change one. The install that
    reported this had FALLBACK_BENCH_ENABLED=false written by a dashboard it
    had only ever used to set something else, which made every Eject setting
    inert.
    """

    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    client = _local_client(create_test_app())

    response = client.post(
        "/admin/api/config/apply",
        json={"values": {"LOG_LEVEL": "DEBUG"}},
    )

    assert response.status_code == 200
    assert response.json()["applied"] is True
    assert response.json()["warnings"] == []

    written = (tmp_path / ".fcc" / ".env").read_text(encoding="utf-8")
    value_lines = [
        line
        for line in written.splitlines()
        if line and not line.startswith("#") and "=" in line
    ]
    assert value_lines == ["LOG_LEVEL=DEBUG"]
    assert "# FALLBACK_BENCH_ENABLED= (default: true)" in written


def test_credential_key_management_flow(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    client = _local_client(create_test_app())

    response = client.post(
        "/admin/api/config/apply",
        json={"values": {"NVIDIA_NIM_API_KEY": "sk-first-key-1234"}},
    )
    assert response.status_code == 200
    assert response.json()["applied"] is True

    listed = client.get("/admin/api/credentials/NVIDIA_NIM_API_KEY/keys")
    assert listed.status_code == 200
    data = listed.json()
    assert data["count"] == 1
    assert data["locked"] is False
    assert data["keys"] == ["sk-fir…1234"]
    assert "health" in data
    assert len(data["health"]) == 1
    assert "sk-first-key-1234" not in str(data)

    added = client.post(
        "/admin/api/credentials/NVIDIA_NIM_API_KEY/keys",
        json={"key": "sk-second-key-5678"},
    )
    assert added.status_code == 200
    assert added.json()["count"] == 2

    listed = client.get("/admin/api/credentials/NVIDIA_NIM_API_KEY/keys").json()
    assert listed["count"] == 2
    assert listed["keys"] == ["sk-fir…1234", "sk-sec…5678"]

    removed = client.delete("/admin/api/credentials/NVIDIA_NIM_API_KEY/keys/0")
    assert removed.status_code == 200
    assert removed.json()["count"] == 1
    assert removed.json()["removed"] == "sk-fir…1234"

    managed_env = tmp_path / ".fcc" / ".env"
    env_text = managed_env.read_text(encoding="utf-8")
    assert "NVIDIA_NIM_API_KEY=sk-second-key-5678" in env_text

    listed = client.get("/admin/api/credentials/NVIDIA_NIM_API_KEY/keys").json()
    assert listed["count"] == 1
    assert listed["keys"] == ["sk-sec…5678"]


def test_credential_key_listing_reports_cached_rotating_health(monkeypatch, tmp_path):
    """Key health must be populated from the cached RotatingProvider.

    Regression: the runtime-lease acquisition dropped its ``await``, so the
    ``async with`` raised TypeError, the informational fallback swallowed it,
    and every health entry silently stayed null.
    """
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    rotating = RotatingProvider(
        ProviderConfig(
            api_key="sk-first-key-1234",
            base_url="http://x",
            api_keys=("sk-first-key-1234", "sk-second-key-5678"),
            credential_rotation="round_robin",
        ),
        [MagicMock(spec=BaseProvider), MagicMock(spec=BaseProvider)],
        CredentialRotationState(
            2,
            "round_robin",
            rate_limit_seconds=60.0,
            lockout_tiers=(300.0, 3600.0, 86400.0),
        ),
    )
    app = create_test_app(providers={"nvidia_nim": rotating})
    client = _local_client(app)

    applied = client.post(
        "/admin/api/config/apply",
        json={"values": {"NVIDIA_NIM_API_KEY": "sk-first-key-1234,sk-second-key-5678"}},
    )
    assert applied.status_code == 200

    listed = client.get("/admin/api/credentials/NVIDIA_NIM_API_KEY/keys")

    assert listed.status_code == 200
    health = listed.json()["health"]
    assert len(health) == 2
    assert [entry["state"] for entry in health] == ["HEALTHY", "HEALTHY"]


def test_credential_key_management_rejects_duplicates_and_bad_input(
    monkeypatch, tmp_path
):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    client = _local_client(create_test_app())

    response = client.post(
        "/admin/api/config/apply",
        json={"values": {"NVIDIA_NIM_API_KEY": "sk-first-key-1234"}},
    )
    assert response.status_code == 200

    duplicate = client.post(
        "/admin/api/credentials/NVIDIA_NIM_API_KEY/keys",
        json={"key": "sk-first-key-1234"},
    )
    assert duplicate.status_code == 409

    empty = client.post(
        "/admin/api/credentials/NVIDIA_NIM_API_KEY/keys",
        json={"key": "   "},
    )
    assert empty.status_code == 400

    only_commas = client.post(
        "/admin/api/credentials/NVIDIA_NIM_API_KEY/keys",
        json={"key": " , , "},
    )
    assert only_commas.status_code == 400

    unknown = client.get("/admin/api/credentials/NOT_A_CREDENTIAL/keys")
    assert unknown.status_code == 404

    missing = client.delete("/admin/api/credentials/NVIDIA_NIM_API_KEY/keys/5")
    assert missing.status_code == 404


def test_credential_key_management_masks_short_keys(monkeypatch, tmp_path):
    from my_claude_code.api.admin_routes import _mask_credential_key

    assert _mask_credential_key("ab") == "****"
    assert _mask_credential_key("abcdef") == "ab…ef"
    assert _mask_credential_key("abcdefghijklmnop") == "abcdef…mnop"


def test_admin_apply_masks_telegram_proxy_credentials(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()
    proxy_url = "https://user:password@proxy.example:8443"

    response = _local_client(app).post(
        "/admin/api/config/apply",
        json={"values": {"TELEGRAM_PROXY_URL": proxy_url}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert "TELEGRAM_PROXY_URL=********" in body["env_preview"]
    assert proxy_url not in body["env_preview"]
    env_file = tmp_path / ".fcc" / ".env"
    text = env_file.read_text(encoding="utf-8")
    assert f"TELEGRAM_PROXY_URL={proxy_url}" in text


def test_admin_validate_rejects_bad_model_shape(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()

    response = _local_client(app).post(
        "/admin/api/config/validate",
        json={"values": {"MODEL": "missing-provider-prefix"}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert any("provider type" in error for error in body["errors"])


def test_admin_apply_writes_complete_managed_env_and_masks_preview(
    monkeypatch, tmp_path
):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()

    response = _local_client(app).post(
        "/admin/api/config/apply",
        json={
            "values": {
                "MODEL": "open_router/test-model",
                "OPENROUTER_API_KEY": "router-secret",
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert "OPENROUTER_API_KEY=********" in body["env_preview"]
    env_file = tmp_path / ".fcc" / ".env"
    text = env_file.read_text("utf-8")
    assert "MODEL=open_router/test-model" in text
    assert "OPENROUTER_API_KEY=router-secret" in text
    assert "ANTHROPIC_AUTH_TOKEN=" in text
    assert body["restart"] == {
        "required": False,
        "automatic": False,
        "admin_url": None,
        "fields": [],
    }


def test_admin_apply_writes_fireworks_key_and_masks_preview(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()

    response = _local_client(app).post(
        "/admin/api/config/apply",
        json={
            "values": {
                "MODEL": "fireworks/test-model",
                "FIREWORKS_API_KEY": "fw-secret",
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert "FIREWORKS_API_KEY=********" in body["env_preview"]
    env_file = tmp_path / ".fcc" / ".env"
    text = env_file.read_text(encoding="utf-8")
    assert "MODEL=fireworks/test-model" in text
    assert "FIREWORKS_API_KEY=fw-secret" in text


def test_admin_apply_writes_gemini_key_and_masks_preview(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()

    response = _local_client(app).post(
        "/admin/api/config/apply",
        json={
            "values": {
                "MODEL": "gemini/models/gemini-3.1-flash-lite",
                "GEMINI_API_KEY": "gm-secret",
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert "GEMINI_API_KEY=********" in body["env_preview"]
    env_file = tmp_path / ".fcc" / ".env"
    text = env_file.read_text(encoding="utf-8")
    assert "MODEL=gemini/models/gemini-3.1-flash-lite" in text
    assert "GEMINI_API_KEY=gm-secret" in text


def test_admin_apply_writes_groq_key_and_masks_preview(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()

    response = _local_client(app).post(
        "/admin/api/config/apply",
        json={
            "values": {
                "MODEL": "groq/llama-3.3-70b-versatile",
                "GROQ_API_KEY": "gq-secret",
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert "GROQ_API_KEY=********" in body["env_preview"]
    env_file = tmp_path / ".fcc" / ".env"
    text = env_file.read_text(encoding="utf-8")
    assert "MODEL=groq/llama-3.3-70b-versatile" in text
    assert "GROQ_API_KEY=gq-secret" in text


def test_admin_apply_writes_sambanova_key_and_masks_preview(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()

    response = _local_client(app).post(
        "/admin/api/config/apply",
        json={
            "values": {
                "MODEL": "sambanova/Meta-Llama-3.3-70B-Instruct",
                "SAMBANOVA_API_KEY": "sn-secret",
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert "SAMBANOVA_API_KEY=********" in body["env_preview"]
    env_file = tmp_path / ".fcc" / ".env"
    text = env_file.read_text(encoding="utf-8")
    assert "MODEL=sambanova/Meta-Llama-3.3-70B-Instruct" in text
    assert "SAMBANOVA_API_KEY=sn-secret" in text


def test_admin_apply_writes_cerebras_key_and_masks_preview(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()

    response = _local_client(app).post(
        "/admin/api/config/apply",
        json={
            "values": {
                "MODEL": "cerebras/llama3.1-8b",
                "CEREBRAS_API_KEY": "cb-secret",
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert "CEREBRAS_API_KEY=********" in body["env_preview"]
    env_file = tmp_path / ".fcc" / ".env"
    text = env_file.read_text(encoding="utf-8")
    assert "MODEL=cerebras/llama3.1-8b" in text
    assert "CEREBRAS_API_KEY=cb-secret" in text


def test_admin_apply_writes_cloudflare_fields_and_masks_preview(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()

    response = _local_client(app).post(
        "/admin/api/config/apply",
        json={
            "values": {
                "MODEL": "cloudflare/@cf/moonshotai/kimi-k2.6",
                "CLOUDFLARE_API_TOKEN": "cf-secret",
                "CLOUDFLARE_ACCOUNT_ID": "cf-account",
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert "CLOUDFLARE_API_TOKEN=********" in body["env_preview"]
    assert "CLOUDFLARE_ACCOUNT_ID=cf-account" in body["env_preview"]
    env_file = tmp_path / ".fcc" / ".env"
    text = env_file.read_text(encoding="utf-8")
    assert "MODEL=cloudflare/@cf/moonshotai/kimi-k2.6" in text
    assert "CLOUDFLARE_API_TOKEN=cf-secret" in text
    assert "CLOUDFLARE_ACCOUNT_ID=cf-account" in text


def test_admin_apply_writes_huggingface_key_and_masks_preview(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()

    response = _local_client(app).post(
        "/admin/api/config/apply",
        json={
            "values": {
                "MODEL": "huggingface/openai/gpt-oss-120b:fastest",
                "HUGGINGFACE_API_KEY": "hf-secret",
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    # Nothing sets VOICE_NOTE_ENABLED, so the prospective Settings snapshot
    # uses the code default (on) exactly as the server will when it reloads
    # this file -- which makes the Hugging Face key the live voice credential
    # and its change a restart. The admin used to predict otherwise, because
    # it wrote every manifest default into the file first.
    assert body["pending_fields"] == ["HUGGINGFACE_API_KEY"]
    assert "HUGGINGFACE_API_KEY=********" in body["env_preview"]
    env_file = tmp_path / ".fcc" / ".env"
    text = env_file.read_text(encoding="utf-8")
    assert "MODEL=huggingface/openai/gpt-oss-120b:fastest" in text
    assert "HUGGINGFACE_API_KEY=hf-secret" in text


@pytest.mark.parametrize(
    ("device", "credential_key"),
    [
        ("nvidia_nim", "NVIDIA_NIM_API_KEY"),
        ("cpu", "HUGGINGFACE_API_KEY"),
    ],
)
def test_admin_key_change_requires_restart_for_active_voice_backend(
    monkeypatch,
    tmp_path,
    device,
    credential_key,
):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    env_file = tmp_path / ".fcc" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(
        "\n".join(
            [
                "VOICE_NOTE_ENABLED=true",
                f"WHISPER_DEVICE={device}",
                f"{credential_key}=old-key",
                "",
            ]
        ),
        encoding="utf-8",
    )
    app = create_test_app()

    response = _local_client(app).post(
        "/admin/api/config/apply",
        json={"values": {credential_key: "new-key"}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert body["pending_fields"] == [credential_key]
    assert body["restart"] == {
        "required": True,
        "automatic": False,
        "admin_url": None,
        "fields": [credential_key],
    }


@pytest.mark.parametrize(
    ("key", "initial", "updated"),
    [
        ("ANTHROPIC_AUTH_TOKEN", "old-token", "new-token"),
        ("DEBUG_PLATFORM_EDITS", "true", "false"),
        ("DEBUG_SUBAGENT_STACK", "true", "false"),
        ("LOG_RAW_API_PAYLOADS", "true", "false"),
        ("LOG_API_ERROR_TRACEBACKS", "true", "false"),
        ("LOG_RAW_MESSAGING_CONTENT", "true", "false"),
        ("LOG_RAW_CLI_DIAGNOSTICS", "true", "false"),
        ("LOG_MESSAGING_ERROR_DETAILS", "true", "false"),
    ],
)
def test_admin_constructor_captured_setting_requires_restart(
    monkeypatch,
    tmp_path,
    key,
    initial,
    updated,
):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    env_file = tmp_path / ".fcc" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(f"{key}={initial}\n", encoding="utf-8")
    app = create_test_app()

    response = _local_client(app).post(
        "/admin/api/config/apply",
        json={"values": {key: updated}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert body["pending_fields"] == [key]
    assert body["restart"] == {
        "required": True,
        "automatic": False,
        "admin_url": None,
        "fields": [key],
    }


def test_admin_apply_writes_cohere_key_and_masks_preview(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()

    response = _local_client(app).post(
        "/admin/api/config/apply",
        json={
            "values": {
                "MODEL": "cohere/command-a-plus-05-2026",
                "COHERE_API_KEY": "cohere-secret",
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert "COHERE_API_KEY=********" in body["env_preview"]
    env_file = tmp_path / ".fcc" / ".env"
    text = env_file.read_text(encoding="utf-8")
    assert "MODEL=cohere/command-a-plus-05-2026" in text
    assert "COHERE_API_KEY=cohere-secret" in text


def test_admin_apply_writes_github_models_token_and_masks_preview(
    monkeypatch, tmp_path
):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()

    response = _local_client(app).post(
        "/admin/api/config/apply",
        json={
            "values": {
                "MODEL": "github_models/openai/gpt-4.1",
                "GITHUB_MODELS_TOKEN": "github-secret",
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert "GITHUB_MODELS_TOKEN=********" in body["env_preview"]
    env_file = tmp_path / ".fcc" / ".env"
    text = env_file.read_text(encoding="utf-8")
    assert "MODEL=github_models/openai/gpt-4.1" in text
    assert "GITHUB_MODELS_TOKEN=github-secret" in text


def test_admin_apply_preserves_hidden_diagnostics_and_smoke_values(
    monkeypatch, tmp_path
):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    env_file = tmp_path / ".fcc" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(
        "\n".join(
            [
                "MODEL=nvidia_nim/old-model",
                "LOG_RAW_API_PAYLOADS=true",
                "FCC_SMOKE_MODEL_ZAI=zai/smoke-model",
                "",
            ]
        ),
        encoding="utf-8",
    )
    app = create_test_app()

    response = _local_client(app).post(
        "/admin/api/config/apply",
        json={"values": {"MODEL": "open_router/test-model"}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    text = env_file.read_text("utf-8")
    assert "MODEL=open_router/test-model" in text
    assert "LOG_RAW_API_PAYLOADS=true" in text
    assert "FCC_SMOKE_MODEL_ZAI=zai/smoke-model" in text


def test_admin_apply_omits_stale_zai_base_url(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    env_file = tmp_path / ".fcc" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(
        "\n".join(
            [
                "MODEL=zai/glm-5.2",
                "ZAI_API_KEY=zai-secret",
                "ZAI_BASE_URL=https://custom.zai.invalid/v1",
                "",
            ]
        ),
        encoding="utf-8",
    )
    app = create_test_app()

    response = _local_client(app).post(
        "/admin/api/config/apply",
        json={"values": {"MODEL": "zai/glm-5.2"}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    text = env_file.read_text("utf-8")
    assert "ZAI_API_KEY=zai-secret" in text
    assert "ZAI_BASE_URL" not in text


def test_admin_apply_omits_stale_fixed_claude_runtime_settings(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    env_file = tmp_path / ".fcc" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(
        "\n".join(
            [
                "MODEL=open_router/test-model",
                "CLAUDE_WORKSPACE=C:/custom/workspace",
                "CLAUDE_CLI_BIN=claude-custom",
                "",
            ]
        ),
        encoding="utf-8",
    )
    app = create_test_app()

    response = _local_client(app).post(
        "/admin/api/config/apply",
        json={"values": {"MODEL": "open_router/test-model"}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    text = env_file.read_text("utf-8")
    assert "MODEL=open_router/test-model" in text
    assert "CLAUDE_WORKSPACE" not in text
    assert "CLAUDE_CLI_BIN" not in text


def test_admin_apply_restart_required_reports_automatic_restart(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    callbacks: list[str] = []

    async def restart_callback() -> None:
        callbacks.append("restart")

    app = create_test_app(restart_callback=restart_callback)

    response = _local_client(app).post(
        "/admin/api/config/apply",
        json={"values": {"PORT": "9090"}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert body["pending_fields"] == ["PORT"]
    assert body["restart"] == {
        "required": True,
        "automatic": True,
        "admin_url": "http://127.0.0.1:9090/admin",
        "fields": ["PORT"],
    }
    assert callbacks == ["restart"]


def test_admin_apply_restart_required_reports_manual_fallback(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()

    response = _local_client(app).post(
        "/admin/api/config/apply",
        json={"values": {"PORT": "9091"}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert body["pending_fields"] == ["PORT"]
    assert body["restart"] == {
        "required": True,
        "automatic": False,
        "admin_url": None,
        "fields": ["PORT"],
    }


def test_admin_process_env_values_are_locked_and_not_written(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    monkeypatch.setenv("MODEL", "open_router/process-model")
    app = create_test_app()

    config = _local_client(app).get("/admin/api/config").json()
    model_field = next(field for field in config["fields"] if field["key"] == "MODEL")
    assert model_field["locked"] is True
    assert model_field["source"] == "process"

    response = _local_client(app).post(
        "/admin/api/config/apply",
        json={"values": {"MODEL": "deepseek/managed-model"}},
    )

    assert response.status_code == 200
    env_file = tmp_path / ".fcc" / ".env"
    assert "deepseek/managed-model" not in env_file.read_text("utf-8")


def test_admin_first_apply_migrates_repo_env(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "MODEL=deepseek/deepseek-chat\nDEEPSEEK_API_KEY=deepseek-secret\n",
        encoding="utf-8",
    )
    app = create_test_app()

    config = _local_client(app).get("/admin/api/config").json()
    model_field = next(field for field in config["fields"] if field["key"] == "MODEL")
    assert model_field["value"] == "deepseek/deepseek-chat"
    assert model_field["source"] == "repo_env"

    response = _local_client(app).post(
        "/admin/api/config/apply",
        json={"values": {}},
    )

    assert response.status_code == 200
    managed_text = (tmp_path / ".fcc" / ".env").read_text("utf-8")
    assert "MODEL=deepseek/deepseek-chat" in managed_text
    assert "DEEPSEEK_API_KEY=deepseek-secret" in managed_text


def test_admin_local_provider_status_reports_reachable(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url: str):
            return httpx.Response(200, json={"data": []})

    with patch("my_claude_code.api.admin_routes.httpx.AsyncClient", FakeAsyncClient):
        response = _local_client(app).get("/admin/api/providers/local-status")

    assert response.status_code == 200
    providers = response.json()["providers"]
    assert {provider["status"] for provider in providers} == {"reachable"}


def test_admin_launch_url_uses_loopback_for_wildcard_host():
    settings = Settings.model_construct(host="0.0.0.0", port=8082)

    assert local_admin_url(settings) == "http://127.0.0.1:8082/admin"


def test_admin_chatgpt_oauth_initiate_is_loopback_only(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()
    remote_client = TestClient(app, client=("203.0.113.10", 50000))

    response = remote_client.post("/admin/api/chatgpt-oauth/initiate")

    assert response.status_code == 403


def test_admin_chatgpt_oauth_initiate_returns_device_code(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()

    def fake_initiate():
        return ("device_1", "ABCD-EFGH", 5000)

    with patch(
        "my_claude_code.api.admin_routes._initiate_device_auth",
        fake_initiate,
    ):
        response = _local_client(app).post("/admin/api/chatgpt-oauth/initiate")

    assert response.status_code == 200
    data = response.json()
    assert data["device_auth_id"] == "device_1"
    assert data["user_code"] == "ABCD-EFGH"
    assert "auth.openai.com/codex/device" in data["verification_url"]


def test_admin_chatgpt_oauth_browser_initiate_uses_remote_guard_by_default(
    monkeypatch,
    tmp_path,
):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()

    with patch(
        "my_claude_code.api.admin_routes.start_browser_login",
        side_effect=ChatGPTOAuthBrowserUnavailableError("device login required"),
    ) as start_login:
        response = _local_client(app).post("/admin/api/chatgpt-oauth/browser/initiate")

    assert response.status_code == 503
    assert "device login required" in response.json()["detail"]
    start_login.assert_called_once_with(allow_remote=False)


def test_admin_chatgpt_oauth_browser_initiate_allows_explicit_same_host_override(
    monkeypatch,
    tmp_path,
):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()
    payload = {
        "authorize_url": "https://auth.openai.com/oauth/authorize?state=safe",
        "expires_in": "300",
    }

    with patch(
        "my_claude_code.api.admin_routes.start_browser_login",
        return_value=payload,
    ) as start_login:
        response = _local_client(app).post(
            "/admin/api/chatgpt-oauth/browser/initiate?same_host_confirmed=true"
        )

    assert response.status_code == 200
    assert response.json() == payload
    start_login.assert_called_once_with(allow_remote=True)


def test_admin_chatgpt_oauth_exchange_never_returns_bearer_token(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()

    def fake_exchange(*args, **kwargs):
        return {
            "access_token": "access_1",
            "refresh_token": "refresh_1",
            "account_id": "acct_1",
        }

    with patch(
        "my_claude_code.api.admin_routes.exchange_device_auth_for_tokens",
        fake_exchange,
    ):
        response = _local_client(app).post(
            "/admin/api/chatgpt-oauth/exchange",
            json={"device_auth_id": "device_1", "user_code": "ABCD-EFGH"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "complete"
    assert data["credential_reference"] == "fcc-managed-oauth"
    assert "access_token" not in data
    assert data["account_id"] == "acct_1"


def test_admin_chatgpt_oauth_exchange_returns_pending(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()

    def fake_exchange(*args, **kwargs):
        return None

    with patch(
        "my_claude_code.api.admin_routes.exchange_device_auth_for_tokens",
        fake_exchange,
    ):
        response = _local_client(app).post(
            "/admin/api/chatgpt-oauth/exchange",
            json={"device_auth_id": "device_1", "user_code": "ABCD-EFGH"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "pending"


def test_admin_chatgpt_oauth_import_codex_is_loopback_only(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()
    remote_client = TestClient(app, client=("203.0.113.10", 50000))

    response = remote_client.post("/admin/api/chatgpt-oauth/import-codex")

    assert response.status_code == 403


def test_admin_chatgpt_oauth_import_codex_returns_managed_reference(
    monkeypatch, tmp_path
):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()

    from my_claude_code.providers.chatgpt_oauth.credentials import (
        ChatGPTOAuthCredentials,
    )

    def fake_import():
        return ChatGPTOAuthCredentials(
            access_token="codex_token",
            account_id="codex_acct",
            refresh_token="codex_refresh",
            source_name="fcc-managed",
        )

    with patch(
        "my_claude_code.api.admin_routes.import_codex_cli_tokens",
        fake_import,
    ):
        response = _local_client(app).post("/admin/api/chatgpt-oauth/import-codex")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "complete"
    assert data["credential_reference"] == "fcc-managed-oauth"
    assert "access_token" not in data
    assert data["account_id"] == "codex_acct"


def test_admin_chatgpt_oauth_import_codex_reports_missing_tokens(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()

    from my_claude_code.providers.chatgpt_oauth.credentials import ChatGPTOAuthError

    def fake_import():
        raise ChatGPTOAuthError("No Codex CLI access token found")

    with patch(
        "my_claude_code.api.admin_routes.import_codex_cli_tokens",
        fake_import,
    ):
        response = _local_client(app).post("/admin/api/chatgpt-oauth/import-codex")

    assert response.status_code == 400
    assert "Codex" in response.json()["detail"]


# --------------------------------------------------------------------- Anthropic OAuth


def _write_claude_code_credentials(tmp_path: Path, *, access_token: str) -> Path:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    path = claude_dir / ".credentials.json"
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": access_token,
                    "refreshToken": "refresh-secret-value",
                    "expiresAt": 9_999_999_999_000,
                    "subscriptionType": "max",
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def test_admin_anthropic_oauth_sources_is_loopback_only(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()
    remote_client = TestClient(app, client=("203.0.113.10", 50000))

    response = remote_client.get("/admin/api/anthropic-oauth/sources")

    assert response.status_code == 403


def test_admin_anthropic_oauth_sources_reports_masked_token_only(monkeypatch, tmp_path):
    """No raw token may appear anywhere in the response body."""
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    _write_claude_code_credentials(
        tmp_path, access_token="sk-ant-oat-super-secret-value"
    )
    app = create_test_app()

    response = _local_client(app).get("/admin/api/anthropic-oauth/sources")

    assert response.status_code == 200
    body_text = response.text
    assert "sk-ant-oat-super-secret-value" not in body_text
    assert "refresh-secret-value" not in body_text
    data = response.json()
    assert data["claude_code"]["available"] is True
    assert data["claude_code"]["masked_token"] == "sk-a…alue"
    assert data["claude_code"]["subscription_type"] == "max"
    assert data["mcc"]["available"] is False


def test_admin_anthropic_oauth_sources_reports_nothing_available(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()

    response = _local_client(app).get("/admin/api/anthropic-oauth/sources")

    assert response.status_code == 200
    data = response.json()
    assert data["claude_code"]["available"] is False
    assert data["mcc"]["available"] is False


def test_admin_anthropic_oauth_import_claude_code_is_loopback_only(
    monkeypatch, tmp_path
):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()
    remote_client = TestClient(app, client=("203.0.113.10", 50000))

    response = remote_client.post("/admin/api/anthropic-oauth/import-claude-code")

    assert response.status_code == 403


def test_admin_anthropic_oauth_import_claude_code_stores_into_mcc_managed_store(
    monkeypatch, tmp_path
):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    creds_path = _write_claude_code_credentials(
        tmp_path, access_token="sk-ant-oat-super-secret-value"
    )
    app = create_test_app()

    response = _local_client(app).post("/admin/api/anthropic-oauth/import-claude-code")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "complete"
    assert data["subscription_type"] == "max"
    assert "sk-ant-oat-super-secret-value" not in response.text

    from my_claude_code.providers.anthropic_oauth.credentials import (
        load_managed_tokens,
    )

    stored = load_managed_tokens()
    assert stored is not None
    assert stored.access_token == "sk-ant-oat-super-secret-value"
    # The credential now exists in MCC's own store too.
    assert creds_path.is_file()


def test_admin_anthropic_oauth_import_claude_code_never_writes_claude_credentials_file(
    monkeypatch, tmp_path
):
    """READ-ONLY guarantee: importing must never touch Claude Code's own file."""
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    creds_path = _write_claude_code_credentials(
        tmp_path, access_token="sk-ant-oat-super-secret-value"
    )
    original_bytes = creds_path.read_bytes()
    original_mtime = creds_path.stat().st_mtime_ns
    app = create_test_app()

    response = _local_client(app).post("/admin/api/anthropic-oauth/import-claude-code")

    assert response.status_code == 200
    assert creds_path.read_bytes() == original_bytes
    assert creds_path.stat().st_mtime_ns == original_mtime


def test_admin_anthropic_oauth_import_claude_code_reports_missing_credential(
    monkeypatch, tmp_path
):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()

    response = _local_client(app).post("/admin/api/anthropic-oauth/import-claude-code")

    assert response.status_code == 400
    assert "No Claude Code credential found" in response.json()["detail"]


def test_admin_anthropic_oauth_initiate_returns_authorize_url(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()

    response = _local_client(app).post("/admin/api/anthropic-oauth/initiate")

    assert response.status_code == 200
    data = response.json()
    assert data["authorize_url"].startswith("https://")
    assert len(data["verifier"]) >= 32


def test_admin_anthropic_oauth_complete_never_returns_raw_token(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()

    from my_claude_code.providers.anthropic_oauth.credentials import OAuthTokens

    async def fake_exchange(code, verifier, state=None):
        assert code == "auth-code-value"
        assert verifier == "verifier-value"
        return OAuthTokens(
            access_token="sk-ant-oat-super-secret-value",
            refresh_token="refresh-secret-value",
            subscription_type="pro",
            source="mcc",
        )

    with patch(
        "my_claude_code.api.admin_routes.exchange_anthropic_oauth_code",
        fake_exchange,
    ):
        response = _local_client(app).post(
            "/admin/api/anthropic-oauth/complete",
            json={"pasted_code": "auth-code-value", "verifier": "verifier-value"},
        )

    assert response.status_code == 200
    assert "sk-ant-oat-super-secret-value" not in response.text
    assert "refresh-secret-value" not in response.text
    data = response.json()
    assert data["status"] == "complete"
    assert data["subscription_type"] == "pro"


def test_admin_anthropic_oauth_complete_reports_login_failure(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    app = create_test_app()

    from my_claude_code.providers.anthropic_oauth.oauth_login import (
        AnthropicOAuthLoginError,
    )

    async def fake_exchange(code, verifier, state=None):
        raise AnthropicOAuthLoginError(400, "bad code")

    with patch(
        "my_claude_code.api.admin_routes.exchange_anthropic_oauth_code",
        fake_exchange,
    ):
        response = _local_client(app).post(
            "/admin/api/anthropic-oauth/complete",
            json={"pasted_code": "bad-code", "verifier": "verifier-value"},
        )

    assert response.status_code == 400


def test_admin_static_anthropic_oauth_card_shows_disclaimer_before_buttons():
    """The card must carry the warning, reuse an existing CSS class, and put it
    before both buttons -- not just describe it in prose elsewhere."""
    script = Path("src/my_claude_code/api/admin_static/admin.js").read_text(
        encoding="utf-8"
    )

    control_start = script.index("function buildAnthropicOAuthControl")
    control_end = script.index("function refreshAnthropicOAuthSources", control_start)
    control_body = script[control_start:control_end]

    warning_index = control_body.index("guide-note-warn")
    import_button_index = control_body.index('"Use Claude Code credentials"')
    login_button_index = control_body.index('"Sign in with Anthropic"')

    assert warning_index < import_button_index
    assert warning_index < login_button_index
    assert "does not permit" in control_body
    assert "cc_entrypoint=cli" in control_body
    assert "ANTHROPIC-SUBSCRIPTION.md" in control_body

    css = Path("src/my_claude_code/api/admin_static/admin.css").read_text(
        encoding="utf-8"
    )
    assert ".guide-note-warn" in css


def test_admin_static_keeps_every_field_input_in_the_document():
    """A wrapped control must still place its input in the page.

    ``changedValues()`` collects fields by walking ``[data-key]`` across the
    document, so a wrapper that keeps its input detached yields a field that
    accepts edits, never marks the form dirty, and is silently never saved.
    The fallback chain editor shipped exactly that way and its value could not
    reach Apply at all.

    Asserted against the source rather than a rendered page because the suite
    has no DOM harness; this is the same guard-test approach the installers
    use for behaviour that cannot be executed in CI.
    """
    script = Path("src/my_claude_code/api/admin_static/admin.js").read_text(
        encoding="utf-8"
    )

    # The invariant that makes this true for every control type.
    assert "if (!control.contains(input)) control.appendChild(input);" in script

    # And the chain editor placing its own input, so the intent stays local.
    assert "this.element.append(this.input, this.rowsEl, this.addButton);" in script


def test_admin_static_reorders_a_route_rail_as_one_list():
    """A route's primary model moves with the arrows, like every fallback.

    A route is two settings -- the primary (MODEL, MODEL_OPUS, ...) and the
    comma-joined chain beside it -- drawn as one ordered path. Until they
    reordered together the up/down arrows stopped short of the only entry that
    actually serves traffic, so promoting a fallback meant retyping two fields
    and hoping they agreed.

    Asserted against the source because the suite has no DOM harness. Verified
    for real in jsdom against a running server before shipping: 6 primary
    button clusters on this branch, 0 on main, and a demote/promote round trip
    leaving both hidden inputs byte-identical to where they started.
    """
    script = Path("src/my_claude_code/api/admin_static/admin.js").read_text(
        encoding="utf-8"
    )

    # One builder for all six rails -- the four tier overrides, the default
    # route and the vision adapter. A rail that reorders differently from the
    # one beside it reads as a bug, not as a distinction, so the primary node
    # is built in exactly one place: the definition plus its single call.
    assert "function appendRouteRail(rail, modelField, chainField) {" in script
    assert script.count("routeNode(") == 2, (
        "a route primary is built outside appendRouteRail"
    )
    assert script.count("appendRouteRail(") == 3, (
        "a rail is filled outside appendRouteRail"
    )

    # And the buttons are wired to the chain editor, which is what owns the
    # ordering rules. Rendering them without this is a pair of dead arrows.
    assert (
        "editor.setPrimary({ input, label: modelField.label, upButton, downButton });"
        in script
    )


def test_admin_static_never_lets_a_button_empty_a_route_primary():
    """The primary may only SWAP with fallback 1, never move into a gap.

    Both halves of that matter and they fail differently. An empty MODEL fails
    validation, so the server refuses to start -- loud, and recoverable by
    hand. An empty tier override is quiet and worse: routing reads a route's
    own fallbacks only when that route has its own primary, so the whole chain
    next to it is silently orphaned and the tier falls back to the root chain
    instead (pinned in test_routing_chains.py).

    Demotion is therefore gated on there being something to swap with.
    Promotion deliberately is not: promoting into an unset override is exactly
    how a route stops inheriting the default.
    """
    script = Path("src/my_claude_code/api/admin_static/admin.js").read_text(
        encoding="utf-8"
    )

    assert (
        "return Boolean(this.primary) && this.rows.length > 0"
        ' && this.primaryValue() !== "";' in script
    ), "canDemotePrimary no longer requires a non-empty primary and a fallback"
    assert "return Boolean(this.primary) && this.rows.length > 0;" in script, (
        "canPromoteFirst no longer allows promoting into an unset override"
    )
    assert "downButton.disabled = !this.canDemotePrimary();" in script


def test_admin_static_styles_every_class_the_script_emits():
    """No class may be emitted by the script without a rule in the stylesheet.

    An unstyled class renders as unformatted content rather than an error, so
    losing a block of CSS is invisible to every other check: the page still
    loads, the tests still pass, and only a person looking at it notices. A
    careless conflict resolution dropped the whole failover block exactly that
    way and it reached main.

    Scoped to the feature prefixes rather than every class in the file, so it
    stays a real assertion instead of a list nobody maintains.
    """
    static = Path("src/my_claude_code/api/admin_static")
    script = (static / "admin.js").read_text(encoding="utf-8")
    styles = (static / "admin.css").read_text(encoding="utf-8")

    prefixes = ("fallback-", "route-", "get-started-", "ws-", "model-chain-", "pv-")
    emitted: set[str] = set()
    # Template literals and plain strings are matched separately: one class
    # covering both would stop at the first quote *inside* an interpolation and
    # capture a fragment that was never a class name.
    for match in re.finditer(r"className = (?:`([^`]*)`|\"([^\"]*)\")", script):
        raw = match.group(1) if match.group(1) is not None else match.group(2)
        # `class${cond ? " a" : " b"}` carries its modifiers inside the
        # interpolation; drop those so the base class is what gets checked.
        literal = re.sub(r"\$\{[^}]*\}", " ", raw)
        # A nested template literal leaves an unterminated `${` behind, since
        # the capture stopped at the inner backtick. Cut from there rather
        # than reporting the fragment as an unstyled class -- the test should
        # not dictate how the script writes its interpolations.
        literal = literal.split("${")[0]
        emitted.update(name for name in literal.split() if name.startswith(prefixes))
    for match in re.finditer(r'classList\.add\("([^"]+)"\)', script):
        if match.group(1).startswith(prefixes):
            emitted.add(match.group(1))

    unstyled = sorted(
        name
        for name in emitted
        if not re.search(rf"\.{re.escape(name)}[\s,:{{]", styles)
    )
    assert unstyled == [], f"emitted by admin.js with no CSS rule: {unstyled}"


def test_admin_static_velvet_theme_defines_every_semantic_token():
    """The velvet theme is a full theme, not a partial override.

    It only swaps the same semantic tokens the other themes swap, so every
    surface the console draws picks it up without a single bespoke selector.
    If a new token is added to the midnight block later and not to velvet, the
    missing ones are listed here -- the check is the same shape as the other
    themes, which is the point.
    """
    static = Path("src/my_claude_code/api/admin_static")
    styles = (static / "admin.css").read_text(encoding="utf-8")

    velvet = re.search(
        r":root\[data-theme=\"velvet\"\]\s*\{(.*?)\n\}",
        styles,
        re.DOTALL,
    )
    assert velvet is not None, "velvet theme block missing from admin.css"

    # The baseline is the token set the existing themes override, not every
    # variable in :root: structural tokens (fonts, radii, transitions) are
    # defined once in :root and intentionally inherited by every theme, so
    # paper and high-contrast do not repeat them either. Velvet must cover the
    # same semantic tokens those themes cover, or a surface silently falls
    # back to the midnight default.
    paper = re.search(
        r":root\[data-theme=\"paper\"\]\s*\{(.*?)\n\}",
        styles,
        re.DOTALL,
    )
    assert paper is not None, "paper theme block missing from admin.css"
    baseline = set(re.findall(r"(--[a-z-]+):", paper.group(1)))
    velvet_tokens = set(re.findall(r"(--[a-z-]+):", velvet.group(1)))
    missing = sorted(baseline - velvet_tokens)
    assert missing == [], f"velvet theme omits tokens: {missing}"

    # A few meaningful values: the theme has to be genuinely navy + velvet red,
    # and text on the navy base has to clear WCAG AA (>= 4.5:1).
    def var_value(style_text: str, name: str) -> str:
        match = re.search(rf"{name}\s*:\s*([^;]+);", style_text)
        assert match is not None, f"{name} missing from velvet theme"
        return match.group(1).strip()

    bg = var_value(velvet.group(1), "--bg")
    accent = var_value(velvet.group(1), "--accent")
    assert bg.startswith("#") and accent.startswith("#")

    text = var_value(velvet.group(1), "--text")

    def relative_luminance(hex_color: str) -> float:
        def channel(value: float) -> float:
            value /= 255.0
            return (
                value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
            )

        hex_color = hex_color.lstrip("#")
        r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
        return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)

    def contrast(fg: str, bg_color: str) -> float:
        lighter = max(relative_luminance(fg), relative_luminance(bg_color))
        darker = min(relative_luminance(fg), relative_luminance(bg_color))
        return (lighter + 0.05) / (darker + 0.05)

    assert contrast(text, bg) >= 4.5
    # Red on navy must clear AA for accent text (eyebrows, active nav, links).
    assert contrast(accent, bg) >= 4.5


def test_admin_static_theme_switcher_includes_velvet():
    """The theme switcher offers a Velvet option with the matching value."""
    html = Path("src/my_claude_code/api/admin_static/index.html").read_text(
        encoding="utf-8"
    )
    script = Path("src/my_claude_code/api/admin_static/admin.js").read_text(
        encoding="utf-8"
    )

    assert (
        '<button type="button" class="theme-option" data-theme-value="velvet" '
        'aria-checked="false">Velvet</button>' in html
    )
    # applyTheme() must accept "velvet" instead of treating it as an unknown
    # theme that falls back to midnight.
    assert 'name !== "paper" && name !== "high-contrast" && name !== "velvet"' in script


def test_credential_add_accepts_a_pasted_pool_and_skips_duplicates(
    monkeypatch, tmp_path
):
    """A comma-separated paste adds every new key at once.

    The raw credential field used to be the only way to enter several keys, and
    it did so by REPLACING the whole pool -- reading as "replace" directly above
    a list that adds and removes. That field is no longer shown, so adding a
    pool has to work here.
    """
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    client = _local_client(create_test_app())

    seeded = client.post(
        "/admin/api/config/apply",
        json={"values": {"NVIDIA_NIM_API_KEY": "sk-first-key-1234"}},
    )
    assert seeded.status_code == 200

    added = client.post(
        "/admin/api/credentials/NVIDIA_NIM_API_KEY/keys",
        json={"key": " sk-second-key-2345 , sk-third-key-3456 "},
    )
    assert added.status_code == 200
    body = added.json()
    assert body["added_count"] == 2
    assert body["count"] == 3
    assert body["skipped"] == 0
    # Masked labels only: a raw key must never appear in a response body.
    assert "sk-second-key-2345" not in added.text
    assert "sk-third-key-3456" not in added.text

    listed = client.get("/admin/api/credentials/NVIDIA_NIM_API_KEY/keys")
    assert listed.status_code == 200
    assert listed.json()["count"] == 3

    # A paste that overlaps the pool adds only what is new rather than failing.
    mixed = client.post(
        "/admin/api/credentials/NVIDIA_NIM_API_KEY/keys",
        json={"key": "sk-first-key-1234,sk-fourth-key-4567"},
    )
    assert mixed.status_code == 200
    assert mixed.json()["added_count"] == 1
    assert mixed.json()["skipped"] == 1
    assert mixed.json()["count"] == 4

    # Duplicates within one paste collapse to a single key.
    repeated = client.post(
        "/admin/api/credentials/NVIDIA_NIM_API_KEY/keys",
        json={"key": "sk-fifth-key-5678,sk-fifth-key-5678"},
    )
    assert repeated.status_code == 200
    assert repeated.json()["added_count"] == 1
    assert repeated.json()["count"] == 5

    # Nothing new at all is still a conflict.
    nothing_new = client.post(
        "/admin/api/credentials/NVIDIA_NIM_API_KEY/keys",
        json={"key": "sk-first-key-1234,sk-fourth-key-4567"},
    )
    assert nothing_new.status_code == 409


def test_admin_static_hides_the_raw_field_for_a_pooled_credential():
    """The replace-the-whole-value input must not sit above the add/remove list.

    It stays in the document so the shared dirty/apply machinery is untouched,
    which is why this asserts on how it is hidden rather than that it is absent.
    """
    static = Path("src/my_claude_code/api/admin_static")
    script = (static / "admin.js").read_text(encoding="utf-8")
    styles = (static / "admin.css").read_text(encoding="utf-8")

    assert "control.hidden = true;" in script
    assert 'wrapper.classList.add("field-pooled");' in script
    # A bare `hidden` attribute loses to any later display rule.
    assert ".field-pooled > [hidden]" in styles
    # Opening a card must open the pool, not reveal another button to press.
    assert "openKeyPool" in script


def test_admin_models_report_which_models_reject_images():
    """The routing page needs this to say a tier requires the vision adapter.

    Only a reported refusal counts. A model with no modality metadata is
    absent from the list, because claiming a tier "cannot read images" on the
    strength of silence would be wrong for most of the catalog.
    """
    settings = Settings()
    settings.model = "open_router/text-only"
    settings.open_router_api_key = "open-router-key"
    app = create_test_app(settings)
    provider_manager_for_app(app).cache_model_infos(
        "open_router",
        {
            ProviderModelInfo("text-only", supports_vision=False),
            ProviderModelInfo("sighted", supports_vision=True),
            ProviderModelInfo("unreported"),
        },
    )

    body = _local_client(app).get("/admin/api/models").json()

    assert body["blind_models"] == ["open_router/text-only"]
    assert "open_router/sighted" in body["models"]
    assert "open_router/unreported" in body["models"]
