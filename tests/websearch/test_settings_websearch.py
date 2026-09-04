"""Settings additions for web search providers."""

import pytest
from pydantic import ValidationError

from my_claude_code.config.settings import Settings
from my_claude_code.config.websearch_catalog import WEBSEARCH_CATALOG
from tests.support.websearch_credentials import forget_web_search_credentials


def _settings(monkeypatch, **env: str) -> Settings:
    monkeypatch.setitem(Settings.model_config, "env_file", ())
    forget_web_search_credentials(monkeypatch)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings()


class TestWebSearchSettings:
    def test_defaults(self, monkeypatch) -> None:
        settings = _settings(monkeypatch)
        assert settings.web_search_provider == "auto"
        assert settings.web_search_fallback_policy == "auto"
        assert settings.exa_api_key is None
        assert settings.searxng_base_url is None
        assert settings.websearch_log_enabled is True
        assert settings.websearch_log_max_rows == 50000
        assert settings.websearch_log_capture_content is True
        assert settings.websearch_log_content_max_chars == 2_000_000

    def test_credential_envs_load(self, monkeypatch) -> None:
        settings = _settings(
            monkeypatch,
            OLLAMA_SEARCH_API_KEY="ollama-key",
            EXA_API_KEY="exa-key",
            TAVILY_API_KEY="tavily-key",
            BRAVE_SEARCH_API_KEY="brave-key",
            JINA_API_KEY="jina-key",
            SERPER_API_KEY="serper-key",
            FIRECRAWL_API_KEY="firecrawl-key",
            LINKUP_API_KEY="linkup-key",
            PERPLEXITY_SEARCH_API_KEY="pplx-key",
            PARALLEL_API_KEY="parallel-key",
            SEARCHAPI_API_KEY="searchapi-key",
            SERPAPI_API_KEY="serpapi-key",
        )
        assert settings.ollama_search_api_key == "ollama-key"
        assert settings.exa_api_key == "exa-key"
        assert settings.tavily_api_key == "tavily-key"
        assert settings.brave_search_api_key == "brave-key"
        assert settings.jina_api_key == "jina-key"
        assert settings.serper_api_key == "serper-key"
        assert settings.firecrawl_api_key == "firecrawl-key"
        assert settings.linkup_api_key == "linkup-key"
        assert settings.perplexity_search_api_key == "pplx-key"
        assert settings.parallel_api_key == "parallel-key"
        assert settings.searchapi_api_key == "searchapi-key"
        assert settings.serpapi_api_key == "serpapi-key"

    def test_empty_credential_becomes_none(self, monkeypatch) -> None:
        settings = _settings(monkeypatch, EXA_API_KEY="", SEARXNG_BASE_URL="")
        assert settings.exa_api_key is None
        assert settings.searxng_base_url is None

    def test_searxng_base_url_loads(self, monkeypatch) -> None:
        settings = _settings(monkeypatch, SEARXNG_BASE_URL="https://sx.test")
        assert settings.searxng_base_url == "https://sx.test"

    @pytest.mark.parametrize(
        "value",
        ["auto", "off", "disabled", *WEBSEARCH_CATALOG.keys(), "AUTO", " Exa "],
    )
    def test_web_search_provider_valid_values(self, monkeypatch, value: str) -> None:
        settings = _settings(monkeypatch, WEB_SEARCH_PROVIDER=value)
        assert settings.web_search_provider == value.strip().lower()

    def test_web_search_provider_invalid_rejected(self, monkeypatch) -> None:
        monkeypatch.setitem(Settings.model_config, "env_file", ())
        monkeypatch.setenv("WEB_SEARCH_PROVIDER", "google")
        with pytest.raises(ValidationError, match="web_search_provider"):
            Settings()

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("auto", "auto"),
            ("none", "none"),
            ("ddgs", "ddgs"),
            ("legacy", "legacy"),
            (" LEGACY ", "legacy"),
        ],
    )
    def test_web_search_fallback_policy_valid_values(
        self, monkeypatch, value: str, expected: str
    ) -> None:
        settings = _settings(monkeypatch, WEB_SEARCH_FALLBACK_POLICY=value)
        assert settings.web_search_fallback_policy == expected

    def test_web_search_fallback_policy_invalid_rejected(self, monkeypatch) -> None:
        monkeypatch.setitem(Settings.model_config, "env_file", ())
        monkeypatch.setenv("WEB_SEARCH_FALLBACK_POLICY", "always")
        with pytest.raises(ValidationError, match="web_search_fallback_policy"):
            Settings()

    def test_log_settings_load(self, monkeypatch) -> None:
        settings = _settings(
            monkeypatch,
            WEBSEARCH_LOG_ENABLED="false",
            WEBSEARCH_LOG_MAX_ROWS="100",
            WEBSEARCH_LOG_CAPTURE_CONTENT="false",
            WEBSEARCH_LOG_CONTENT_MAX_CHARS="1234",
        )
        assert settings.websearch_log_enabled is False
        assert settings.websearch_log_max_rows == 100
        assert settings.websearch_log_capture_content is False
        assert settings.websearch_log_content_max_chars == 1234

    def test_content_cap_rejects_unsafe_tiny_values(self, monkeypatch) -> None:
        monkeypatch.setitem(Settings.model_config, "env_file", ())
        monkeypatch.setenv("WEBSEARCH_LOG_CONTENT_MAX_CHARS", "511")

        with pytest.raises(ValidationError, match="WEBSEARCH_LOG_CONTENT_MAX_CHARS"):
            Settings()
