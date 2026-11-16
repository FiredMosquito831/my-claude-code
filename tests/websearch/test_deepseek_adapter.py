"""DeepSeek Search #16, IV.24 key isolation, and PR 1C adapter bugfix tests."""

from pathlib import Path

import httpx
import pytest

from my_claude_code.config.provider_catalog import configured_credential_values
from my_claude_code.config.settings import BLANK_MEANS_UNSET_FIELDS, Settings
from my_claude_code.config.websearch_catalog import WEBSEARCH_CATALOG
from my_claude_code.core.websearch.models import WebSearchRequestOptions
from my_claude_code.websearch.adapters.brave import BraveWebSearchProvider
from my_claude_code.websearch.adapters.deepseek import DeepSeekWebSearchProvider
from my_claude_code.websearch.adapters.exa import ExaWebSearchProvider
from my_claude_code.websearch.adapters.tavily import TavilyWebSearchProvider
from my_claude_code.websearch.errors import (
    WebSearchAuthError,
    WebSearchConfigError,
    WebSearchInvalidRequestError,
    WebSearchQuotaError,
    WebSearchRateLimitError,
)
from my_claude_code.websearch.registry import build_provider, resolve_provider_id
from tests.support.websearch_credentials import (
    NO_WEB_SEARCH_CREDENTIALS,
    forget_web_search_credentials,
)
from tests.websearch.support import (
    TEST_KEY,
    attach_mock_client,
    build_config,
    json_response,
    request_json_body,
)


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate Settings and dotenv lookups inside ``tmp_path``."""

    forget_web_search_credentials(monkeypatch)
    for var in (
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_SEARCH_API_KEY",
        "DEEPSEEK_SEARCH_USE_INFERENCE_KEY",
        "DEEPSEEK_SEARCH_NEED_CONTENT",
        "DEEPSEEK_SEARCH_LOCALIZATION",
        "WEB_SEARCH_PROVIDER",
        "FCC_ENV_FILE",
        "MCC_ENV_FILE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path / ".mcc"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestDeepSeekAdapter:
    @pytest.mark.asyncio
    async def test_request_payload_mapping_and_cost(self) -> None:
        provider = DeepSeekWebSearchProvider(
            build_config(
                options={
                    "DEEPSEEK_SEARCH_NEED_CONTENT": "true",
                    "DEEPSEEK_SEARCH_LOCALIZATION": "203.0.113.5",
                }
            )
        )
        requests = attach_mock_client(
            provider,
            lambda _req: json_response(
                {
                    "results": [
                        {
                            "title": "DeepSeek Docs",
                            "url": "https://api-docs.deepseek.com/search",
                            "summary": "Official summary",
                            "snippet": "Fallback snippet",
                            "content": "# Markdown body",
                            "date": "2026-03-11",
                            "rerank_score": 0.94,
                            "site_icon": "https://api-docs.deepseek.com/favicon.ico",
                            "site_name": "DeepSeek Docs",
                        },
                        {
                            "title": "Empty content row",
                            "url": "https://example.org/empty",
                            "snippet": "Only snippet here",
                            "content": "",
                        },
                    ]
                }
            ),
        )
        try:
            response = await provider.search(
                "deepseek search api",
                max_results=80,
                allowed_domains=("docs.example.org",),
                blocked_domains=("spam.example.org",),
            )
        finally:
            await provider.close()

        assert len(requests) == 1
        req = requests[0]
        assert req.method == "POST"
        assert req.url.path == "/v1/tools/search"
        assert req.headers["Authorization"] == f"Bearer {TEST_KEY}"
        body = request_json_body(req)
        assert body == {
            "query": "deepseek search api",
            "count": 50,
            "need_content": True,
            "localization": "203.0.113.5",
        }

        assert response.provider == "deepseek"
        assert response.cost_usd == 0.00075
        assert len(response.results) == 2

        first = response.results[0]
        assert first.title == "DeepSeek Docs"
        assert first.url == "https://api-docs.deepseek.com/search"
        assert first.snippet == "Official summary"
        assert first.content == "# Markdown body"
        assert first.published == "2026-03-11"
        assert first.score == 0.94
        assert first.favicon == "https://api-docs.deepseek.com/favicon.ico"
        assert first.site_name == "DeepSeek Docs"

        second = response.results[1]
        assert second.snippet == "Only snippet here"
        assert second.content is None

    @pytest.mark.asyncio
    async def test_need_content_false_forces_none_content_and_ignores_country(
        self,
    ) -> None:
        provider = DeepSeekWebSearchProvider(build_config())
        requests = attach_mock_client(
            provider,
            lambda _req: json_response(
                {
                    "results": [
                        {
                            "title": "Row",
                            "url": "https://example.org/a",
                            "summary": "Sum",
                            "content": "Should be withheld when need_content is false",
                        }
                    ]
                }
            ),
        )
        try:
            response = await provider.search(
                "localization safety",
                request_options=WebSearchRequestOptions(country="de"),
            )
        finally:
            await provider.close()

        body = request_json_body(requests[0])
        assert body["need_content"] is False
        assert "localization" not in body
        assert response.cost_usd == 0.00075
        assert response.results[0].content is None

    @pytest.mark.asyncio
    async def test_request_options_include_content_overrides_env_option(self) -> None:
        provider = DeepSeekWebSearchProvider(
            build_config(options={"DEEPSEEK_SEARCH_NEED_CONTENT": "true"})
        )
        requests = attach_mock_client(
            provider,
            lambda _req: json_response(
                {
                    "results": [
                        {
                            "title": "Row",
                            "url": "https://example.org/b",
                            "snippet": "S",
                            "content": "Body",
                        }
                    ]
                }
            ),
        )
        try:
            resp = await provider.search(
                "override",
                request_options=WebSearchRequestOptions(include_content=False),
            )
        finally:
            await provider.close()

        assert request_json_body(requests[0])["need_content"] is False
        assert resp.results[0].content is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status", "exc_cls"),
        [
            (400, WebSearchInvalidRequestError),
            (401, WebSearchAuthError),
            (422, WebSearchInvalidRequestError),
            (429, WebSearchRateLimitError),
        ],
    )
    async def test_http_error_statuses(
        self, status: int, exc_cls: type[Exception]
    ) -> None:
        provider = DeepSeekWebSearchProvider(build_config())
        attach_mock_client(
            provider, lambda _req: json_response({"error": "fail"}, status=status)
        )
        try:
            with pytest.raises(exc_cls):
                await provider.search("q")
        finally:
            await provider.close()


class TestDeepSeekCredentialSeparationAndAutoSelect:
    def test_iv24_refuses_inference_key_by_default(self, isolated_env: Path) -> None:
        settings = Settings.model_validate(
            {
                **NO_WEB_SEARCH_CREDENTIALS,
                "WEB_SEARCH_PROVIDER": "deepseek",
                "DEEPSEEK_API_KEY": "sk-infer-only",
            }
        )
        with pytest.raises(WebSearchConfigError) as exc_info:
            build_provider(settings, "deepseek")
        assert "DEEPSEEK_SEARCH_API_KEY" in str(exc_info.value)

    def test_iv24_explicit_false_still_refuses_inference_key(
        self, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DEEPSEEK_SEARCH_USE_INFERENCE_KEY", "false")
        settings = Settings.model_validate(
            {
                **NO_WEB_SEARCH_CREDENTIALS,
                "DEEPSEEK_API_KEY": "sk-infer-only",
            }
        )
        with pytest.raises(WebSearchConfigError) as exc_info:
            build_provider(settings, "deepseek")
        assert "DEEPSEEK_SEARCH_API_KEY" in str(exc_info.value)

    def test_iv24_opt_in_true_uses_inference_key_when_search_key_unset(
        self, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DEEPSEEK_SEARCH_USE_INFERENCE_KEY", "true")
        settings = Settings.model_validate(
            {
                **NO_WEB_SEARCH_CREDENTIALS,
                "DEEPSEEK_API_KEY": "sk-infer-optin",
            }
        )
        provider = build_provider(settings, "deepseek")
        assert provider.config.api_keys == ("sk-infer-optin",)

    def test_iv24_dedicated_search_key_takes_precedence_over_inference_key(
        self, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DEEPSEEK_SEARCH_USE_INFERENCE_KEY", "true")
        settings = Settings.model_validate(
            {
                **NO_WEB_SEARCH_CREDENTIALS,
                "DEEPSEEK_API_KEY": "sk-infer-fallback",
                "DEEPSEEK_SEARCH_API_KEY": "sk-search-dedicated",
            }
        )
        provider = build_provider(settings, "deepseek")
        assert provider.config.api_keys == ("sk-search-dedicated",)

    def test_auto_never_picks_deepseek_even_when_configured(
        self, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DEEPSEEK_SEARCH_USE_INFERENCE_KEY", "true")
        assert WEBSEARCH_CATALOG["deepseek"].auto_select is False
        settings = Settings.model_validate(
            {
                **NO_WEB_SEARCH_CREDENTIALS,
                "WEB_SEARCH_PROVIDER": "auto",
                "DEEPSEEK_API_KEY": "sk-infer",
                "DEEPSEEK_SEARCH_API_KEY": "sk-search",
            }
        )
        assert resolve_provider_id(settings) == "ddgs"

    def test_secret_blank_handling_and_redaction_tuple(
        self, isolated_env: Path
    ) -> None:
        from my_claude_code.config.admin.manifest import FIELD_BY_KEY
        from my_claude_code.config.admin.values import blank_is_accepted

        assert "deepseek_search_api_key" in BLANK_MEANS_UNSET_FIELDS
        assert blank_is_accepted(FIELD_BY_KEY["DEEPSEEK_SEARCH_API_KEY"]) is True
        blank_settings = Settings.model_validate(
            {
                **NO_WEB_SEARCH_CREDENTIALS,
                "DEEPSEEK_SEARCH_API_KEY": "",
            }
        )
        assert blank_settings.deepseek_search_api_key is None

        configured_settings = Settings.model_validate(
            {
                **NO_WEB_SEARCH_CREDENTIALS,
                "DEEPSEEK_SEARCH_API_KEY": "sk-ds-secret-1234",
            }
        )
        assert "sk-ds-secret-1234" in configured_credential_values(configured_settings)


class TestPR1CAdapterBugfixes:
    @pytest.mark.asyncio
    async def test_tavily_433_maps_to_quota_error(self) -> None:
        provider = TavilyWebSearchProvider(build_config())
        attach_mock_client(
            provider,
            lambda _req: httpx.Response(
                433, json={"detail": {"error": "Pay-as-you-go limit exceeded"}}
            ),
        )
        try:
            with pytest.raises(WebSearchQuotaError):
                await provider.search("q")
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_tavily_published_date_preserved_and_null_when_absent(self) -> None:
        provider = TavilyWebSearchProvider(build_config())
        attach_mock_client(
            provider,
            lambda _req: json_response(
                {
                    "results": [
                        {
                            "title": "Dated",
                            "url": "https://example.org/dated",
                            "content": "S1",
                            "published_date": "Tue, 11 Mar 2025 17:00:00 GMT",
                        },
                        {
                            "title": "Undated",
                            "url": "https://example.org/undated",
                            "content": "S2",
                        },
                    ]
                }
            ),
        )
        try:
            resp = await provider.search("q")
        finally:
            await provider.close()

        assert resp.results[0].published == "Tue, 11 Mar 2025 17:00:00 GMT"
        assert resp.results[1].published is None

    @pytest.mark.asyncio
    async def test_brave_llm_context_extracts_published_and_site_name(self) -> None:
        provider = BraveWebSearchProvider(
            build_config(options={"BRAVE_SEARCH_MODE": "llm-context"})
        )
        attach_mock_client(
            provider,
            lambda _req: json_response(
                {
                    "grounding": {
                        "generic": [
                            {
                                "title": "Brave Result",
                                "url": "https://docs.example.org/guide",
                                "snippets": ["Excerpt one.", "Excerpt two."],
                                "published": "2026-04-01T12:00:00Z",
                                "site_name": "Example Docs",
                            },
                            {
                                "title": "No Meta",
                                "url": "https://example.org/plain",
                                "snippets": ["Plain excerpt."],
                            },
                        ]
                    }
                }
            ),
        )
        try:
            resp = await provider.search("q")
        finally:
            await provider.close()

        assert resp.results[0].published == "2026-04-01T12:00:00Z"
        assert resp.results[0].site_name == "Example Docs"
        assert resp.results[1].published is None
        assert resp.results[1].site_name is None

    @pytest.mark.asyncio
    async def test_exa_search_type_lite_passes_through_and_is_in_catalog(self) -> None:
        exa_type_opt = next(
            opt
            for opt in WEBSEARCH_CATALOG["exa"].advanced_options
            if opt.env == "EXA_SEARCH_TYPE"
        )
        assert exa_type_opt.options[0][0] == ""
        assert ("lite", "lite (~250-450 ms)") in exa_type_opt.options
        assert (
            WEBSEARCH_CATALOG["parallel"].notes
            == "POST /v1/search with x-api-key header (Search API)."
        )

        provider = ExaWebSearchProvider(
            build_config(options={"EXA_SEARCH_TYPE": "lite"})
        )
        requests = attach_mock_client(
            provider, lambda _req: json_response({"results": []})
        )
        try:
            await provider.search("q")
        finally:
            await provider.close()
        assert request_json_body(requests[0])["type"] == "lite"
