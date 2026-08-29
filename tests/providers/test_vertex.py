"""Focused tests for the Google Vertex AI provider construction and auth."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from google.auth.credentials import Credentials
from google.auth.exceptions import DefaultCredentialsError, TransportError

from my_claude_code.application.errors import ApplicationUnavailableError
from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.config.provider_catalog import VERTEX_DEFAULT_BASE
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.model_listing import ModelListResponseError
from my_claude_code.providers.vertex import VertexProvider
from my_claude_code.providers.vertex.auth import GoogleAccessTokenProvider
from my_claude_code.providers.vertex.endpoint import (
    vertex_openai_base_url,
    vertex_publisher_models_url,
    vertex_service_endpoint,
)
from my_claude_code.providers.vertex.models import extract_vertex_model_page
from tests.providers.support import passthrough_rate_limiter

_PROJECT_ID = "my-project"
_GLOBAL_MODELS_URL = f"{VERTEX_DEFAULT_BASE}/v1beta1/publishers/google/models"


def _provider_config():
    return MagicMock(
        base_url=VERTEX_DEFAULT_BASE,
        proxy="",
        http_read_timeout=120.0,
        http_connect_timeout=10.0,
        http_write_timeout=10.0,
        api_key="",
    )


class FakeCredentials(Credentials):
    """Minimal mutable Google credentials for deterministic refresh tests."""

    def __init__(
        self,
        *,
        token: str | None = None,
        expired: bool = True,
        refresh_error: Exception | None = None,
    ) -> None:
        super().__init__()
        self.token = token
        self._expired = expired
        self._refresh_error = refresh_error
        self.refresh_count = 0

    @property
    def expired(self) -> bool:
        return self._expired

    def refresh(self, request: object) -> None:
        del request
        self.refresh_count += 1
        if self._refresh_error is not None:
            raise self._refresh_error
        self.token = "refreshed-token"
        self._expired = False


def test_vertex_endpoints_use_global_or_regional_hosts() -> None:
    assert vertex_service_endpoint("global") == VERTEX_DEFAULT_BASE
    assert vertex_service_endpoint("us-central1") == (
        "https://us-central1-aiplatform.googleapis.com"
    )
    assert vertex_openai_base_url("my-project", "global") == (
        f"{VERTEX_DEFAULT_BASE}/v1/projects/my-project/locations/global/"
        "endpoints/openapi"
    )
    assert vertex_publisher_models_url("global") == (
        f"{VERTEX_DEFAULT_BASE}/v1beta1/publishers/google/models"
    )


def test_vertex_openai_base_url_encodes_project_id() -> None:
    assert vertex_openai_base_url("project/name", "global") == (
        f"{VERTEX_DEFAULT_BASE}/v1/projects/project%2Fname/locations/global/"
        "endpoints/openapi"
    )


@pytest.mark.asyncio
async def test_access_token_provider_reuses_valid_token_without_refresh() -> None:
    credentials = FakeCredentials(token="cached-token", expired=False)
    token_provider = GoogleAccessTokenProvider(lambda: credentials)

    assert await token_provider() == "cached-token"
    assert await token_provider() == "cached-token"
    assert credentials.refresh_count == 0


@pytest.mark.asyncio
async def test_missing_adc_is_non_retryable_authentication_failure() -> None:
    def missing_credentials() -> Credentials:
        raise DefaultCredentialsError("sensitive local path")

    token_provider = GoogleAccessTokenProvider(missing_credentials)

    with pytest.raises(ExecutionFailure) as exc_info:
        await token_provider()

    failure = exc_info.value
    assert failure.kind is FailureKind.AUTHENTICATION
    assert failure.status_code == 401
    assert failure.retryable is False
    assert "gcloud auth application-default login" in failure.message
    assert "sensitive local path" not in failure.message


@pytest.mark.asyncio
async def test_transient_adc_refresh_failure_is_retryable() -> None:
    credentials = FakeCredentials(
        refresh_error=TransportError("temporary auth service failure")
    )
    token_provider = GoogleAccessTokenProvider(lambda: credentials)

    with pytest.raises(ExecutionFailure) as exc_info:
        await token_provider()

    failure = exc_info.value
    assert failure.kind is FailureKind.UNAVAILABLE
    assert failure.status_code == 503
    assert failure.retryable is True


def _factory_settings() -> MagicMock:
    settings = MagicMock()
    settings.vertex_project_id = "my-project"
    settings.vertex_location = "global"
    settings.vertex_proxy = ""
    # Numeric knobs are arithmetic in the factory, so a bare MagicMock cannot
    # stand in for them.
    settings.provider_retry_attempts = 5
    settings.stream_early_retry_attempts = 5
    settings.stream_midstream_recovery_attempts = 5
    settings.stream_commit_holdback_seconds = 0.75
    settings.rate_limit_cooldown_seconds = 60.0
    settings.credential_lockout_tiers = "300,3600,86400"
    settings.provider_retry_backoff_base_seconds = 2.0
    settings.provider_retry_backoff_max_seconds = 60.0
    settings.provider_retry_backoff_jitter_seconds = 1.0
    settings.provider_rate_limit = 40
    settings.provider_rate_window = 60
    settings.provider_max_concurrency = 5
    settings.http_read_timeout = 300.0
    settings.http_write_timeout = 10.0
    settings.http_connect_timeout = 10.0
    settings.log_raw_sse_events = False
    settings.log_api_error_tracebacks = False
    return settings


def test_create_vertex_provider_via_factory_sets_project() -> None:
    settings = _factory_settings()

    with (
        patch("my_claude_code.providers.openai_chat.provider.AsyncOpenAI"),
        patch("httpx.AsyncClient"),
    ):
        from my_claude_code.providers.runtime import create_provider

        provider = create_provider("vertex", settings)

    assert isinstance(provider, VertexProvider)
    assert provider._project_id == "my-project"
    assert provider._location == "global"
    assert provider._base_url == (
        f"{VERTEX_DEFAULT_BASE}/v1/projects/my-project/locations/global/"
        "endpoints/openapi"
    )


def test_vertex_openai_base_url_requires_project_id() -> None:
    with pytest.raises(ApplicationUnavailableError, match="VERTEX_PROJECT_ID"):
        vertex_openai_base_url("", "global")


@pytest.mark.asyncio
async def test_vertex_model_discovery_follows_native_pagination() -> None:
    token_provider = GoogleAccessTokenProvider(
        lambda: FakeCredentials(token="access-token", expired=False)
    )

    with (
        patch("my_claude_code.providers.openai_chat.provider.AsyncOpenAI"),
        patch("httpx.AsyncClient"),
    ):
        provider = VertexProvider(
            ProviderConfig(
                api_key="",
                base_url=VERTEX_DEFAULT_BASE,
                rate_limit=10,
                rate_window=60,
            ),
            project_id=_PROJECT_ID,
            location="global",
            rate_limiter=passthrough_rate_limiter(),
            access_token_provider=token_provider,
        )
    responses = [
        httpx.Response(
            200,
            json={
                "publisherModels": [
                    {"name": "publishers/google/models/gemini-3.5-flash"}
                ],
                "nextPageToken": "page-2",
            },
            request=httpx.Request("GET", _GLOBAL_MODELS_URL),
        ),
        httpx.Response(
            200,
            json={
                "publisherModels": [{"name": "publishers/google/models/gemini-3.1-pro"}]
            },
            request=httpx.Request("GET", _GLOBAL_MODELS_URL),
        ),
    ]
    with patch.object(
        provider._model_list_client,
        "get",
        new_callable=AsyncMock,
        side_effect=responses,
    ) as get:
        model_infos = await provider.list_model_infos()

    assert model_infos == frozenset(
        {
            ProviderModelInfo("google/gemini-3.5-flash"),
            ProviderModelInfo("google/gemini-3.1-pro"),
        }
    )
    assert get.await_args_list[0].kwargs == {
        "params": None,
        "headers": {
            "Authorization": "Bearer access-token",
            "x-goog-user-project": _PROJECT_ID,
        },
    }
    assert get.await_args_list[1].kwargs == {
        "params": {"pageToken": "page-2"},
        "headers": {
            "Authorization": "Bearer access-token",
            "x-goog-user-project": _PROJECT_ID,
        },
    }


def test_vertex_model_page_translates_google_resource_names_generically() -> None:
    model_ids, page_token = extract_vertex_model_page(
        {
            "publisherModels": [
                {"name": "publishers/google/models/gemini-3.5-flash"},
                {"name": "publishers/acme/models/custom-chat"},
            ],
            "nextPageToken": "next-page",
        }
    )

    assert model_ids == frozenset({"google/gemini-3.5-flash", "acme/custom-chat"})
    assert page_token == "next-page"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"publisherModels": "not-a-list"},
        {"publisherModels": [{}]},
        {"publisherModels": [{"name": "models/missing-publisher"}]},
        {"publisherModels": [], "nextPageToken": 123},
    ],
)
def test_vertex_model_page_rejects_malformed_responses(payload: object) -> None:
    with pytest.raises(ModelListResponseError, match="VERTEX model-list response"):
        extract_vertex_model_page(payload)


def _vertex_provider() -> VertexProvider:
    access_token_provider = GoogleAccessTokenProvider(
        lambda: FakeCredentials(token="access-token", expired=False)
    )

    with (
        patch("my_claude_code.providers.openai_chat.provider.AsyncOpenAI"),
        patch("httpx.AsyncClient"),
    ):
        return VertexProvider(
            ProviderConfig(
                api_key="",
                base_url=VERTEX_DEFAULT_BASE,
                rate_limit=10,
                rate_window=60,
            ),
            project_id=_PROJECT_ID,
            location="global",
            rate_limiter=passthrough_rate_limiter(),
            access_token_provider=access_token_provider,
        )


def test_vertex_request_uses_google_thinking_budget_without_named_effort() -> None:
    from tests.providers.request_factory import make_messages_request
    from tests.providers.support import reasoning_for

    provider = _vertex_provider()
    request = make_messages_request(
        "google/gemini-3.5-flash",
        thinking={"type": "enabled", "budget_tokens": 2048},
    )

    body = provider._build_request_body(request, reasoning=reasoning_for(request))

    assert body["model"] == "google/gemini-3.5-flash"
    assert "reasoning_effort" not in body
    assert body["extra_body"]["extra_body"]["google"]["thinking_config"] == {
        "include_thoughts": True,
        "thinking_budget": 2048,
    }


def test_vertex_reasoning_off_maps_to_zero_budget() -> None:
    from tests.providers.request_factory import make_messages_request
    from tests.providers.support import reasoning_for

    provider = _vertex_provider()
    request = make_messages_request(
        "google/gemini-3.5-flash",
        thinking={"type": "disabled"},
    )

    body = provider._build_request_body(request, reasoning=reasoning_for(request))

    assert body["extra_body"]["extra_body"]["google"]["thinking_config"] == {
        "thinking_budget": 0,
        "include_thoughts": False,
    }
