"""Tests for opt-in ``chatgpt_oauth`` standalone web search adapter, registry, and REST route."""

import json
from collections.abc import Iterator
from compression import zstd
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.web_tools import outbound, search_chain
from my_claude_code.api.web_tools.search_providers import reset_runtime_providers
from my_claude_code.config.constants import CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE
from my_claude_code.config.paths import chatgpt_oauth_auth_path, reset_config_dir_cache
from my_claude_code.config.settings import Settings, get_settings
from my_claude_code.config.websearch_catalog import WEBSEARCH_CATALOG
from my_claude_code.core.chatgpt_oauth_websearch import (
    CHATGPT_OAUTH_WEBSEARCH_DEFAULT_MODEL,
    CHATGPT_OAUTH_ZSTD_THRESHOLD_BYTES,
    CODEX_BASE_INSTRUCTIONS,
)
from my_claude_code.core.request_log import (
    get_request_log_store,
    set_request_log_path,
)
from my_claude_code.providers.chatgpt_oauth import credentials as oauth_creds
from my_claude_code.websearch.adapters.chatgpt_oauth import (
    ChatGPTOAuthWebSearchProvider,
)
from my_claude_code.websearch.analytics import reset_analytics_state
from my_claude_code.websearch.base import WebSearchProviderConfig
from my_claude_code.websearch.errors import (
    WebSearchAuthError,
    WebSearchConfigError,
    WebSearchQuotaError,
    WebSearchRateLimitError,
    WebSearchUpstreamError,
)
from my_claude_code.websearch.registry import (
    build_provider,
    build_providers,
    resolve_provider_id,
    resolve_search_route,
)
from tests.api.support import create_test_app
from tests.support.websearch_credentials import (
    NO_WEB_SEARCH_CREDENTIALS,
    forget_web_search_credentials,
)


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    forget_web_search_credentials(monkeypatch)
    for var in (
        "WEB_SEARCH_PROVIDER",
        "WEB_SEARCH_CHAIN",
        "CHATGPT_OAUTH_WEBSEARCH_ENABLED",
        "CHATGPT_OAUTH_WEBSEARCH_MODEL",
        "CHATGPT_OAUTH_ACCESS_TOKEN",
        "CHATGPT_OAUTH_ACCOUNT_ID",
        "CHATGPT_OAUTH_BASE_URL",
        "CHATGPT_OAUTH_PROXY",
        "FCC_ENV_FILE",
        "MCC_ENV_FILE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path / ".mcc"))
    monkeypatch.chdir(tmp_path)
    reset_config_dir_cache()
    set_request_log_path(tmp_path / "requests.db")
    yield tmp_path
    store = get_request_log_store()
    if store is not None:
        store.close()
    set_request_log_path(None)
    reset_analytics_state()
    reset_config_dir_cache()


def _write_managed_oauth_store(
    *,
    access_token: str = "managed-access-token",
    refresh_token: str = "managed-refresh-token",
    account_id: str = "acct_managed_1",
) -> Path:
    path = chatgpt_oauth_auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "accounts_version": 1,
        "tokens": {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "account_id": account_id,
            "expires_at": 4_102_444_800,
        },
        "accounts": [
            {
                "id": account_id,
                "origin": "mcc",
                "write_back": False,
                "added_at": "2026-11-27T00:00:00Z",
                "ordinal": 1,
                "tokens": {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "account_id": account_id,
                    "expires_at": 4_102_444_800,
                },
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _sse_response(
    events: list[dict[str, Any]],
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    chunks = [
        f"data: {json.dumps(event, separators=(',', ':'))}\n\n".encode()
        for event in events
    ]
    chunks.append(b"data: [DONE]\n\n")
    return httpx.Response(
        status,
        headers={"content-type": "text/event-stream", **(headers or {})},
        content=b"".join(chunks),
    )


def _sample_codex_sse_events() -> list[dict[str, Any]]:
    answer_text = "Latest Python 3.14 notes are documented at Python Docs and PEP 779."
    return [
        {
            "type": "response.output_item.done",
            "item": {
                "id": "ws_1",
                "type": "web_search_call",
                "status": "completed",
                "action": {
                    "type": "search",
                    "query": "python 3.14",
                    "sources": [
                        {
                            "url": "https://docs.python.org/3.14/",
                            "title": "Python 3.14 Docs",
                            "snippet": "Official 3.14 release documentation.",
                        },
                        {
                            "url": "https://spam.example.org/noise",
                            "title": "Spam Mirror",
                            "snippet": "Should be post-filtered.",
                        },
                    ],
                },
            },
        },
        {
            "type": "response.output_item.done",
            "item": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": answer_text,
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url": "https://peps.python.org/pep-0779/",
                                "title": "PEP 779",
                                "start_index": 56,
                                "end_index": 63,
                            },
                            {
                                "type": "url_citation",
                                "url": "https://docs.python.org/3.14/",
                                "title": "Python 3.14 Documentation",
                                "start_index": 38,
                                "end_index": 49,
                            },
                        ],
                    }
                ],
            },
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp_ws_123",
                "status": "completed",
                "output": [],
            },
        },
    ]


class TestChatGPTOAuthRegistryGate:
    def test_catalog_contract_opt_in_and_no_anthropic_oauth(self) -> None:
        desc = WEBSEARCH_CATALOG["chatgpt_oauth"]
        assert desc.auth_kind == "oauth"
        assert desc.requires_key is False
        assert desc.supports_domains is True
        assert desc.auto_select is False
        assert "anthropic_oauth" not in WEBSEARCH_CATALOG

    def test_disabled_by_default_rejects_build_provider_and_stays_out_of_auto(
        self, isolated_env: Path
    ) -> None:
        del isolated_env
        _write_managed_oauth_store()
        settings = Settings.model_validate(
            {
                **NO_WEB_SEARCH_CREDENTIALS,
                "CHATGPT_OAUTH_ACCESS_TOKEN": CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
            }
        )
        assert settings.chatgpt_oauth_websearch_enabled is False
        with pytest.raises(
            WebSearchConfigError, match="CHATGPT_OAUTH_WEBSEARCH_ENABLED"
        ):
            build_provider(settings, "chatgpt_oauth")
        assert "chatgpt_oauth" not in build_providers(settings)
        assert resolve_provider_id(settings) == "ddgs"
        assert "chatgpt_oauth" not in resolve_search_route(settings).provider_ids

    def test_enabled_without_oauth_credentials_raises_config_error(
        self, isolated_env: Path
    ) -> None:
        del isolated_env
        settings = Settings.model_validate(
            {
                **NO_WEB_SEARCH_CREDENTIALS,
                "CHATGPT_OAUTH_WEBSEARCH_ENABLED": True,
            }
        )
        with pytest.raises(WebSearchConfigError, match="ChatGPT OAuth credentials"):
            build_provider(settings, "chatgpt_oauth")

    def test_enabled_with_credentials_still_never_auto_selected(
        self, isolated_env: Path
    ) -> None:
        del isolated_env
        _write_managed_oauth_store()
        settings = Settings.model_validate(
            {
                **NO_WEB_SEARCH_CREDENTIALS,
                "CHATGPT_OAUTH_WEBSEARCH_ENABLED": True,
                "CHATGPT_OAUTH_ACCESS_TOKEN": CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
                "WEB_SEARCH_PROVIDER": "auto",
            }
        )
        assert resolve_provider_id(settings) == "ddgs"
        assert "chatgpt_oauth" not in resolve_search_route(settings).provider_ids
        built = build_provider(settings, "chatgpt_oauth")
        assert isinstance(built, ChatGPTOAuthWebSearchProvider)


class TestChatGPTOAuthAdapter:
    @pytest.mark.asyncio
    async def test_codex_responses_request_and_sse_extraction(
        self, isolated_env: Path
    ) -> None:
        del isolated_env
        _write_managed_oauth_store(
            access_token="oauth-tok-123",
            account_id="acct_42",
        )
        settings = Settings.model_validate(
            {
                **NO_WEB_SEARCH_CREDENTIALS,
                "CHATGPT_OAUTH_WEBSEARCH_ENABLED": True,
                "CHATGPT_OAUTH_ACCESS_TOKEN": CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
                "CHATGPT_OAUTH_BASE_URL": "https://stub.codex.test/backend-api/codex",
            }
        )
        provider = build_provider(settings, "chatgpt_oauth")
        captured: list[httpx.Request] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return _sse_response(_sample_codex_sse_events())

        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        try:
            response = await provider.search(
                "python 3.14 release",
                max_results=5,
                allowed_domains=("docs.python.org", "peps.python.org"),
                blocked_domains=("spam.example.org",),
            )
        finally:
            await provider.close()

        assert len(captured) == 1
        req = captured[0]
        assert req.method == "POST"
        assert str(req.url) == "https://stub.codex.test/backend-api/codex/responses"
        assert req.headers["authorization"] == "Bearer oauth-tok-123"
        assert req.headers["openai-beta"] == "responses=experimental"
        assert req.headers["originator"] == "codex_cli_rs"
        assert req.headers["chatgpt-account-id"] == "acct_42"
        assert req.headers["accept"] == "text/event-stream"
        assert "content-encoding" not in req.headers

        body = json.loads(req.content.decode("utf-8"))
        assert body == {
            "model": CHATGPT_OAUTH_WEBSEARCH_DEFAULT_MODEL,
            "instructions": CODEX_BASE_INSTRUCTIONS,
            "store": False,
            "stream": True,
            "tools": [
                {
                    "type": "web_search",
                    "filters": {
                        "allowed_domains": ["docs.python.org", "peps.python.org"]
                    },
                }
            ],
            "tool_choice": {"type": "web_search"},
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "python 3.14 release"}],
                }
            ],
        }

        assert response.provider == "chatgpt_oauth"
        assert response.request_id == "resp_ws_123"
        assert response.http_status == 200
        assert (
            response.answer
            == "Latest Python 3.14 notes are documented at Python Docs and PEP 779."
        )
        urls = [item.url for item in response.results]
        assert urls == [
            "https://docs.python.org/3.14/",
            "https://peps.python.org/pep-0779/",
        ]
        assert response.results[0].title == "Python 3.14 Docs"
        assert response.results[1].title == "PEP 779"
        assert "PEP 779" in response.results[1].snippet

    @pytest.mark.asyncio
    async def test_401_refreshes_managed_credentials_and_retries(
        self, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        del isolated_env
        _write_managed_oauth_store(
            access_token="stale-access-token",
            account_id="acct_refresh",
        )
        refreshed_calls: list[str | None] = []

        def _fake_refresh(
            account_id: str | None = None, *, purpose: Any = None
        ) -> oauth_creds.ChatGPTOAuthCredentials:
            del purpose
            refreshed_calls.append(account_id)
            return oauth_creds.ChatGPTOAuthCredentials(
                access_token="fresh-access-token",
                account_id="acct_refresh",
                source_name=oauth_creds.CHATGPT_OAUTH_MANAGED_SOURCE_NAME,
            )

        monkeypatch.setattr(
            "my_claude_code.websearch.adapters.chatgpt_oauth.force_refresh_managed_chatgpt_oauth_credentials",
            _fake_refresh,
        )

        settings = Settings.model_validate(
            {
                **NO_WEB_SEARCH_CREDENTIALS,
                "CHATGPT_OAUTH_WEBSEARCH_ENABLED": True,
                "CHATGPT_OAUTH_ACCESS_TOKEN": CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
            }
        )
        provider = build_provider(settings, "chatgpt_oauth")
        auth_headers: list[str] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            auth_headers.append(request.headers["authorization"])
            if len(auth_headers) == 1:
                return httpx.Response(401, json={"error": {"message": "token expired"}})
            return _sse_response(_sample_codex_sse_events())

        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        try:
            response = await provider.search("refresh check")
        finally:
            await provider.close()

        assert refreshed_calls == ["acct_refresh"]
        assert auth_headers == [
            "Bearer stale-access-token",
            "Bearer fresh-access-token",
        ]
        assert len(response.results) == 3

    @pytest.mark.asyncio
    async def test_large_payload_uses_zstd_and_413_retries_with_zstd(
        self, isolated_env: Path
    ) -> None:
        del isolated_env
        provider = ChatGPTOAuthWebSearchProvider(
            WebSearchProviderConfig(
                api_keys=(),
                credential_rotation="single",
                base_url="https://stub.codex.test/backend-api",
                proxy=None,
                http_timeout=10.0,
                oauth_access_token="tok-direct",
                oauth_account_id="acct-direct",
            )
        )
        seen: list[tuple[str | None, dict[str, Any]]] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            enc = request.headers.get("content-encoding")
            raw = zstd.decompress(request.content) if enc == "zstd" else request.content
            seen.append((enc, json.loads(raw.decode("utf-8"))))
            if (
                enc is None
                and seen[-1][1]["input"][0]["content"][0]["text"] == "retry-413"
            ):
                return httpx.Response(413, text="Payload Too Large")
            return _sse_response(_sample_codex_sse_events())

        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        try:
            await provider.search("retry-413")
            assert [item[0] for item in seen] == [None, "zstd"]

            seen.clear()
            big_query = "q" * CHATGPT_OAUTH_ZSTD_THRESHOLD_BYTES
            await provider.search(big_query)
            assert [item[0] for item in seen] == ["zstd"]
            assert seen[0][1]["input"][0]["content"][0]["text"] == big_query
        finally:
            await provider.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("events", "status", "expected_exc"),
        [
            ([], 429, WebSearchRateLimitError),
            ([], 402, WebSearchQuotaError),
            ([], 503, WebSearchUpstreamError),
            (
                [
                    {
                        "type": "error",
                        "error": {
                            "code": "rate_limit_exceeded",
                            "message": "slow down",
                        },
                    }
                ],
                200,
                WebSearchRateLimitError,
            ),
            (
                [
                    {
                        "type": "response.failed",
                        "response": {
                            "status": "failed",
                            "error": {
                                "code": "insufficient_quota",
                                "message": "quota out",
                            },
                        },
                    }
                ],
                200,
                WebSearchQuotaError,
            ),
            (
                [
                    {
                        "type": "error",
                        "error": {"code": "unauthorized", "message": "bad auth"},
                    }
                ],
                200,
                WebSearchAuthError,
            ),
        ],
    )
    async def test_error_mapping(
        self,
        isolated_env: Path,
        events: list[dict[str, Any]],
        status: int,
        expected_exc: type[Exception],
    ) -> None:
        del isolated_env
        provider = ChatGPTOAuthWebSearchProvider(
            WebSearchProviderConfig(
                api_keys=(),
                credential_rotation="single",
                base_url="https://stub.codex.test/backend-api",
                proxy=None,
                http_timeout=10.0,
                oauth_access_token="tok-direct",
            )
        )

        def _handler(_request: httpx.Request) -> httpx.Response:
            if status >= 400:
                return httpx.Response(status, json={"error": {"message": "failed"}})
            return _sse_response(events, status=status)

        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        try:
            with pytest.raises(expected_exc):
                await provider.search("err")
        finally:
            await provider.close()


class TestRestWebSearchChatGPTOAuthRoute:
    @pytest.mark.asyncio
    async def test_post_v1_websearch_rejects_disabled_and_succeeds_when_enabled(
        self, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        del isolated_env
        _write_managed_oauth_store(
            access_token="rest-oauth-tok", account_id="acct_rest"
        )

        disabled_settings = Settings.model_validate(
            {
                **NO_WEB_SEARCH_CREDENTIALS,
                "ENABLE_WEB_SERVER_TOOLS": True,
                "WEB_SEARCH_PROVIDER": "auto",
                "CHATGPT_OAUTH_WEBSEARCH_ENABLED": False,
                "CHATGPT_OAUTH_ACCESS_TOKEN": CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
                "REQUEST_LOG_ENABLED": False,
                "WEBSEARCH_LOG_ENABLED": False,
            }
        )
        app_disabled = create_test_app(disabled_settings)
        app_disabled.dependency_overrides[get_settings] = lambda: disabled_settings
        client_disabled = TestClient(app_disabled, client=("127.0.0.1", 50000))
        res_disabled = client_disabled.post(
            "/v1/websearch",
            json={"query": "mcc", "provider": "chatgpt_oauth"},
        )
        assert res_disabled.status_code == 503
        assert "CHATGPT_OAUTH_WEBSEARCH_ENABLED" in res_disabled.text

        enabled_settings = Settings.model_validate(
            {
                **NO_WEB_SEARCH_CREDENTIALS,
                "ENABLE_WEB_SERVER_TOOLS": True,
                "WEB_SEARCH_PROVIDER": "chatgpt_oauth",
                "CHATGPT_OAUTH_WEBSEARCH_ENABLED": True,
                "CHATGPT_OAUTH_ACCESS_TOKEN": CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
                "REQUEST_LOG_ENABLED": False,
                "WEBSEARCH_LOG_ENABLED": False,
            }
        )
        recorded: list[httpx.Request] = []

        async def _fake_runtime_provider(cfg: Settings, provider_id: str) -> Any:
            prov = build_provider(cfg, provider_id)
            old = prov.__dict__.get("_client")
            if old is not None:
                await old.aclose()

            def _dispatch(req: httpx.Request) -> httpx.Response:
                recorded.append(req)
                return _sse_response(_sample_codex_sse_events())

            prov.__dict__["_client"] = httpx.AsyncClient(
                transport=httpx.MockTransport(_dispatch)
            )
            return prov

        monkeypatch.setattr(outbound, "runtime_provider", _fake_runtime_provider)
        monkeypatch.setattr(search_chain, "runtime_provider", _fake_runtime_provider)
        app_enabled = create_test_app(enabled_settings)
        app_enabled.dependency_overrides[get_settings] = lambda: enabled_settings
        client_enabled = TestClient(app_enabled, client=("127.0.0.1", 50000))
        try:
            res_ok = client_enabled.post(
                "/v1/websearch",
                json={
                    "query": "python 3.14",
                    "provider": "chatgpt_oauth",
                    "allowed_domains": ["docs.python.org"],
                    "blocked_domains": ["spam.example.org"],
                },
            )
        finally:
            await reset_runtime_providers()

        assert res_ok.status_code == 200, res_ok.text
        payload = res_ok.json()
        assert payload["provider"] == "chatgpt_oauth"
        assert [r["url"] for r in payload["results"]] == [
            "https://docs.python.org/3.14/",
            "https://peps.python.org/pep-0779/",
        ]
        assert len(recorded) == 1
        assert recorded[0].headers["authorization"] == "Bearer rest-oauth-tok"
