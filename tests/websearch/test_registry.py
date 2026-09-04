"""Registry build/resolve tests and the analytics recorder seam."""

import pytest

from my_claude_code.config.settings import Settings
from my_claude_code.websearch import registry
from my_claude_code.websearch.errors import (
    WebSearchConfigError,
    WebSearchUpstreamError,
)
from my_claude_code.websearch.registry import (
    SearchOutcome,
    active_provider,
    build_provider,
    build_providers,
    resolve_provider_id,
    resolve_search_route,
    search,
    search_with_logging,
)
from tests.support.websearch_credentials import forget_web_search_credentials
from tests.websearch.support import StubWebSearchProvider, build_config


def _settings(monkeypatch, **env: str) -> Settings:
    monkeypatch.setitem(Settings.model_config, "env_file", ())
    # Only the credentials the case names may be visible. Otherwise "nothing is
    # configured" means "nothing is configured on the machine that ran this".
    forget_web_search_credentials(monkeypatch)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings()


class TestBuildProviders:
    def test_only_ddgs_when_nothing_configured(self, monkeypatch) -> None:
        settings = _settings(monkeypatch)
        providers = build_providers(settings)
        assert list(providers) == ["ddgs"]

    def test_keyed_provider_built_with_parsed_keys(self, monkeypatch) -> None:
        settings = _settings(
            monkeypatch, EXA_API_KEY="k1-aaaa1111bbbb, k2-cccc2222dddd"
        )
        providers = build_providers(settings)
        exa = providers["exa"]
        assert exa.config.api_keys == ("k1-aaaa1111bbbb", "k2-cccc2222dddd")
        assert exa.config.credential_rotation == "failover"
        assert exa.config.base_url == "https://api.exa.ai"

    def test_single_key_defaults_to_single_policy(self, monkeypatch) -> None:
        settings = _settings(monkeypatch, EXA_API_KEY="k1-aaaa1111bbbb")
        assert build_provider(settings, "exa").config.credential_rotation == "single"

    def test_rotation_policy_from_process_env(self, monkeypatch) -> None:
        settings = _settings(
            monkeypatch,
            EXA_API_KEY="k1,k2",
            EXA_API_KEY_ROTATION="round_robin",
        )
        assert build_provider(settings, "exa").config.credential_rotation == (
            "round_robin"
        )

    def test_rotation_policy_from_dotenv(self, monkeypatch, tmp_path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text(
            "TAVILY_API_KEY=tvly-aaaa1111bbbb\nTAVILY_API_KEY_ROTATION=least_used\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("TAVILY_API_KEY_ROTATION", raising=False)
        monkeypatch.setitem(Settings.model_config, "env_file", (env_file,))
        provider = build_provider(Settings(), "tavily")
        assert provider.config.credential_rotation == "least_used"

    def test_invalid_rotation_falls_back_to_default(self, monkeypatch) -> None:
        settings = _settings(
            monkeypatch, EXA_API_KEY="k1,k2", EXA_API_KEY_ROTATION="chaos"
        )
        assert build_provider(settings, "exa").config.credential_rotation == "failover"

    def test_searxng_requires_base_url(self, monkeypatch) -> None:
        settings = _settings(monkeypatch)
        with pytest.raises(WebSearchConfigError, match="SEARXNG_BASE_URL"):
            build_provider(settings, "searxng")

    def test_searxng_built_with_base_url(self, monkeypatch) -> None:
        settings = _settings(
            monkeypatch, SEARXNG_BASE_URL="https://searxng.example.test/"
        )
        provider = build_provider(settings, "searxng")
        assert provider.config.api_keys == ()
        assert provider.config.base_url == "https://searxng.example.test/"

    def test_unknown_provider_rejected(self, monkeypatch) -> None:
        settings = _settings(monkeypatch)
        with pytest.raises(WebSearchConfigError, match="unknown web search provider"):
            build_provider(settings, "nope")

    def test_websearch_proxy_env_flows_to_config(self, monkeypatch) -> None:
        settings = _settings(
            monkeypatch,
            EXA_API_KEY="k1-aaaa1111bbbb",
            WEBSEARCH_PROXY="http://proxy.test:8080",
        )
        assert build_provider(settings, "exa").config.proxy == (
            "http://proxy.test:8080"
        )


class TestResolve:
    def test_auto_picks_first_configured_catalog_provider(self, monkeypatch) -> None:
        settings = _settings(monkeypatch, EXA_API_KEY="k1", TAVILY_API_KEY="k2")
        assert resolve_provider_id(settings) == "exa"

    def test_auto_honors_catalog_order(self, monkeypatch) -> None:
        settings = _settings(monkeypatch, OLLAMA_SEARCH_API_KEY="k1", EXA_API_KEY="k2")
        assert resolve_provider_id(settings) == "ollama"

    def test_auto_falls_back_to_ddgs(self, monkeypatch) -> None:
        assert resolve_provider_id(_settings(monkeypatch)) == "ddgs"

    def test_auto_uses_configured_searxng(self, monkeypatch) -> None:
        settings = _settings(monkeypatch, SEARXNG_BASE_URL="https://sx.test")
        assert resolve_provider_id(settings) == "searxng"

    def test_off_resolves_to_none(self, monkeypatch) -> None:
        settings = _settings(monkeypatch, WEB_SEARCH_PROVIDER="off")
        assert resolve_provider_id(settings) is None
        assert active_provider(settings) is None

    def test_disabled_resolves_to_none(self, monkeypatch) -> None:
        settings = _settings(monkeypatch, WEB_SEARCH_PROVIDER="disabled")
        assert resolve_provider_id(settings) is None
        assert active_provider(settings) is None

    def test_explicit_provider_builds(self, monkeypatch) -> None:
        settings = _settings(
            monkeypatch, WEB_SEARCH_PROVIDER="exa", EXA_API_KEY="k1-aaaa1111bbbb"
        )
        provider = active_provider(settings)
        assert provider is not None
        assert provider.provider_id == "exa"

    def test_explicit_unconfigured_provider_raises(self, monkeypatch) -> None:
        settings = _settings(monkeypatch, WEB_SEARCH_PROVIDER="exa")
        with pytest.raises(WebSearchConfigError, match="EXA_API_KEY"):
            active_provider(settings)

    @pytest.mark.parametrize(
        ("selection", "policy", "expected_ids", "expected_legacy", "disabled"),
        [
            ("auto", "auto", ("exa", "ddgs"), True, False),
            ("auto", "none", ("exa",), False, False),
            ("auto", "ddgs", ("exa", "ddgs"), False, False),
            ("auto", "legacy", ("exa", "ddgs"), True, False),
            ("exa", "auto", ("exa",), False, False),
            ("exa", "none", ("exa",), False, False),
            ("exa", "ddgs", ("exa", "ddgs"), False, False),
            ("exa", "legacy", ("exa", "ddgs"), True, False),
            ("ddgs", "ddgs", ("ddgs",), False, False),
            ("ddgs", "legacy", ("ddgs",), True, False),
            ("off", "none", (), True, False),
            ("disabled", "legacy", (), False, True),
        ],
    )
    def test_search_route_semantics(
        self,
        monkeypatch,
        selection: str,
        policy: str,
        expected_ids: tuple[str, ...],
        expected_legacy: bool,
        disabled: bool,
    ) -> None:
        settings = _settings(
            monkeypatch,
            WEB_SEARCH_PROVIDER=selection,
            WEB_SEARCH_FALLBACK_POLICY=policy,
            EXA_API_KEY="k1",
        )

        route = resolve_search_route(settings)

        assert route.provider_ids == expected_ids
        assert route.use_legacy_scrape is expected_legacy
        assert route.disabled is disabled

    def test_auto_route_without_credentials_starts_with_ddgs(self, monkeypatch) -> None:
        route = resolve_search_route(_settings(monkeypatch))
        assert route.provider_ids == ("ddgs",)
        assert route.use_legacy_scrape is True


class TestRecorderSeam:
    @pytest.mark.asyncio
    async def test_success_outcome_recorded(self) -> None:
        outcomes: list[SearchOutcome] = []
        provider = StubWebSearchProvider(
            build_config(
                api_keys=("sk-live-0001wxyz",),
                base_url="https://user:password@example.test/search?token=secret",
                proxy="http://proxy-user:proxy-pass@proxy.test:8080",
                options={"MODE": "deep"},
            )
        )
        response = await search(
            provider,
            "hello",
            max_results=7,
            recorder=outcomes.append,
            route_id="route-123",
            attempt_number=2,
            route_context={
                "selected_provider": "stub",
                "fallback_policy": "none",
            },
        )
        assert response.results
        (outcome,) = outcomes
        assert outcome.provider == "stub"
        assert outcome.status == "success"
        assert outcome.key_index == 0
        assert outcome.key_label == "sk-l…wxyz"
        assert outcome.results_count == 1
        assert outcome.duration_ms >= 0
        assert outcome.error_kind is None
        assert outcome.cost_usd is None
        assert outcome.ts_iso
        assert outcome.route_id == "route-123"
        assert outcome.attempt_number == 2
        assert outcome.input_payload == {
            "query": "hello",
            "max_results": 7,
            "allowed_domains": [],
            "blocked_domains": [],
        }
        assert outcome.output_payload is not None
        assert outcome.output_payload["provider"] == "stub"
        assert outcome.output_payload["result_count"] == 1
        assert outcome.output_payload["results"] == [
            {
                "title": "t",
                "url": "https://example.com",
                "snippet": "s",
                "content": None,
                "published": None,
            }
        ]
        assert outcome.provider_config == {
            "provider_id": "stub",
            "credential_rotation": "failover",
            "credential_count": 1,
            "base_url": "https://example.test/search",
            "proxy": "http://proxy.test:8080",
            "http_timeout_seconds": 20.0,
            "supports_domain_filters": False,
            "options": {"MODE": "deep"},
            "route": {
                "selected_provider": "stub",
                "fallback_policy": "none",
            },
        }
        assert "sk-live-0001wxyz" not in str(outcome.provider_config)
        assert "password" not in str(outcome.provider_config)

    @pytest.mark.asyncio
    async def test_error_outcome_recorded_with_kind_and_key(self) -> None:
        outcomes: list[SearchOutcome] = []
        provider = StubWebSearchProvider(
            build_config(),
            behavior={0: WebSearchUpstreamError("stub", "kaput " * 200)},
        )
        with pytest.raises(WebSearchUpstreamError):
            await search(provider, "q", recorder=outcomes.append)
        (outcome,) = outcomes
        assert outcome.status == "error"
        assert outcome.error_kind == "upstream"
        assert outcome.key_index == 0
        assert len(outcome.error_message or "") <= 500
        assert outcome.results_count == 0
        assert outcome.input_payload == {
            "query": "q",
            "max_results": 10,
            "allowed_domains": [],
            "blocked_domains": [],
        }
        assert outcome.output_payload is not None
        error_payload = outcome.output_payload["error"]
        assert isinstance(error_payload, dict)
        assert error_payload.get("kind") == "upstream"
        assert error_payload.get("type") == "WebSearchUpstreamError"
        assert "kaput" in str(error_payload.get("message"))
        assert len(str(error_payload.get("message"))) == 500

    @pytest.mark.asyncio
    async def test_non_websearch_error_recorded_as_internal(self) -> None:
        outcomes: list[SearchOutcome] = []
        provider = StubWebSearchProvider(
            build_config(), behavior={0: RuntimeError("bug")}
        )
        with pytest.raises(RuntimeError):
            await search(provider, "q", recorder=outcomes.append)
        assert outcomes[0].error_kind == "internal"

    @pytest.mark.asyncio
    async def test_query_is_capped_at_256_chars(self) -> None:
        outcomes: list[SearchOutcome] = []
        provider = StubWebSearchProvider(build_config())
        await search(provider, "x" * 1000, recorder=outcomes.append)
        assert len(outcomes[0].query) == 256

    @pytest.mark.asyncio
    async def test_recorder_failure_does_not_break_search(self) -> None:
        def bad_recorder(outcome: SearchOutcome) -> None:
            raise RuntimeError("recorder bug")

        provider = StubWebSearchProvider(build_config())
        response = await search(provider, "q", recorder=bad_recorder)
        assert response.results

    @pytest.mark.asyncio
    async def test_no_recorder_is_a_noop(self) -> None:
        provider = StubWebSearchProvider(build_config())
        assert (await search(provider, "q")).results

    @pytest.mark.asyncio
    async def test_search_with_logging_uses_explicit_recorder(self) -> None:
        outcomes: list[SearchOutcome] = []
        provider = StubWebSearchProvider(build_config())
        await search_with_logging(provider, "q", recorder=outcomes.append)
        assert len(outcomes) == 1

    @pytest.mark.asyncio
    async def test_search_with_logging_defaults_to_analytics_recorder(
        self, monkeypatch, tmp_path
    ) -> None:
        # Worker B landed: the seam resolves to websearch.analytics.record_search.
        from my_claude_code.websearch.analytics import (
            default_websearch_db_path,
            record_search,
            record_search_route,
            reset_analytics_state,
        )

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("WEBSEARCH_LOG_ENABLED", "false")
        reset_analytics_state()
        try:
            assert registry._default_recorder() is record_search
            assert registry._default_route_recorder() is record_search_route
            provider = StubWebSearchProvider(build_config())
            assert (await search_with_logging(provider, "q")).results
            assert not default_websearch_db_path().exists()
        finally:
            reset_analytics_state()
