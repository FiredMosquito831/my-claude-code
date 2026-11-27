"""ChatGPT/Codex OAuth standalone Web Search adapter (``POST /codex/responses``).

Strictly opt-in (``CHATGPT_OAUTH_WEBSEARCH_ENABLED=true``, ``auto_select=False``):
reuses ChatGPT/Codex OAuth credentials (managed store or explicit token) and
managed ``401`` refresh without modifying frozen ``IV.8`` modules.
"""

import asyncio
import json
import platform
import uuid
from collections.abc import Mapping
from compression import zstd
from typing import Any, ClassVar

import httpx

from my_claude_code.core.chatgpt_oauth_websearch import (
    CHATGPT_OAUTH_WEBSEARCH_DEFAULT_MODEL,
    CHATGPT_OAUTH_ZSTD_LEVEL,
    CHATGPT_OAUTH_ZSTD_THRESHOLD_BYTES,
    ChatGPTOAuthWebSearchAnswerError,
    build_chatgpt_oauth_websearch_payload,
    normalize_chatgpt_oauth_base_url,
    parse_chatgpt_oauth_websearch_sse,
)
from my_claude_code.core.credential_refresh_scope import (
    claimed_scope,
    refresh_exit,
    stream_purpose,
)
from my_claude_code.core.version import package_version
from my_claude_code.core.websearch.models import (
    WebSearchRequestOptions,
    WebSearchResponse,
)
from my_claude_code.providers.chatgpt_oauth.credentials import (
    CHATGPT_OAUTH_MANAGED_SOURCE_NAME,
    CODEX_OAUTH_ORIGINATOR,
    ChatGPTOAuthCredentials,
    ChatGPTOAuthError,
    force_refresh_managed_chatgpt_oauth_credentials,
    load_chatgpt_oauth_credentials,
)
from my_claude_code.providers.chatgpt_oauth.response_headers import (
    OBSERVER as RESPONSE_HEADER_OBSERVER,
)

from ..base import BaseWebSearchProvider, WebSearchProviderConfig
from ..errors import (
    KIND_DETAIL_TIMEOUT,
    KIND_DETAIL_TRANSPORT,
    WebSearchAuthError,
    WebSearchConfigError,
    WebSearchQuotaError,
    WebSearchRateLimitError,
    WebSearchUpstreamError,
    reset_last_http_status,
    set_last_http_status,
)
from ._common import opt_str
from .http import build_async_client, map_status_error


def _codex_user_agent() -> str:
    return (
        f"{CODEX_OAUTH_ORIGINATOR}/{package_version()} "
        f"({platform.system()} {platform.release()}; {platform.machine()})"
    )


def _build_codex_headers(
    credentials: ChatGPTOAuthCredentials,
    session_id: str,
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {credentials.access_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "OpenAI-Beta": "responses=experimental",
        "originator": CODEX_OAUTH_ORIGINATOR,
        "User-Agent": _codex_user_agent(),
        "session-id": session_id,
    }
    if credentials.account_id:
        headers["ChatGPT-Account-ID"] = credentials.account_id
    return headers


def _encode_request_payload(
    body: Mapping[str, Any],
    headers: Mapping[str, str],
    *,
    force_zstd: bool = False,
) -> tuple[bytes, dict[str, str], bool]:
    raw_bytes = json.dumps(
        body, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    compress = force_zstd or len(raw_bytes) >= CHATGPT_OAUTH_ZSTD_THRESHOLD_BYTES
    if compress:
        payload = zstd.compress(raw_bytes, level=CHATGPT_OAUTH_ZSTD_LEVEL)
        wire_headers = dict(headers)
        wire_headers["Content-Encoding"] = "zstd"
        return payload, wire_headers, True
    return raw_bytes, dict(headers), False


class ChatGPTOAuthWebSearchProvider(BaseWebSearchProvider):
    """Opt-in standalone web search adapter backed by ChatGPT/Codex OAuth."""

    PROVIDER_ID: ClassVar[str] = "chatgpt_oauth"
    SUPPORTS_DOMAINS: ClassVar[bool] = True

    def __init__(self, config: WebSearchProviderConfig) -> None:
        super().__init__(config)
        self._base_url = normalize_chatgpt_oauth_base_url(config.base_url)
        self._model = (
            opt_str(config.options.get("CHATGPT_OAUTH_WEBSEARCH_MODEL"))
            or CHATGPT_OAUTH_WEBSEARCH_DEFAULT_MODEL
        )
        self._session_id = str(uuid.uuid4())
        self._client = build_async_client(
            proxy=config.proxy, http_timeout=config.http_timeout
        )

    async def _send_codex_post(
        self,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any],
        *,
        account_id: str | None,
    ) -> httpx.Response:
        client = self._require_client()
        content, wire_headers, compressed = _encode_request_payload(body, headers)
        request = client.build_request(
            "POST", url, headers=wire_headers, content=content
        )
        try:
            response = await client.send(request, stream=True)
            if response.status_code == 413 and not compressed:
                await response.aclose()
                content, wire_headers, _ = _encode_request_payload(
                    body, headers, force_zstd=True
                )
                request = client.build_request(
                    "POST", url, headers=wire_headers, content=content
                )
                response = await client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            raise WebSearchUpstreamError(
                self.provider_id,
                f"request timed out ({type(exc).__name__})",
                kind_detail=KIND_DETAIL_TIMEOUT,
            ) from exc
        except httpx.TransportError as exc:
            raise WebSearchUpstreamError(
                self.provider_id,
                f"transport error ({type(exc).__name__})",
                kind_detail=KIND_DETAIL_TRANSPORT,
            ) from exc

        RESPONSE_HEADER_OBSERVER.observe(
            response.headers,
            status_code=response.status_code,
            account_id=account_id or "",
        )
        return response

    async def _search_with_key(
        self,
        query: str,
        key: str,
        key_index: int,
        *,
        max_results: int,
        allowed_domains: tuple[str, ...],
        blocked_domains: tuple[str, ...],
    ) -> WebSearchResponse:
        return await self._search_with_key_options(
            query,
            key,
            key_index,
            max_results=max_results,
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
            request_options=None,
        )

    async def _search_with_key_options(
        self,
        query: str,
        key: str,
        key_index: int,
        *,
        max_results: int,
        allowed_domains: tuple[str, ...],
        blocked_domains: tuple[str, ...],
        request_options: WebSearchRequestOptions | None = None,
    ) -> WebSearchResponse:
        del key
        model = self._model
        if request_options is not None and isinstance(
            request_options.provider_params, Mapping
        ):
            override_model = opt_str(request_options.provider_params.get("model"))
            if override_model:
                model = override_model

        purpose = stream_purpose()
        try:
            with claimed_scope(purpose), refresh_exit(self._config.proxy):
                credentials = await asyncio.to_thread(
                    load_chatgpt_oauth_credentials,
                    access_token=self._config.oauth_access_token or None,
                    account_id=self._config.oauth_account_id or None,
                )
        except ChatGPTOAuthError as exc:
            raise WebSearchConfigError(self.provider_id, str(exc)) from exc

        payload = build_chatgpt_oauth_websearch_payload(
            query,
            model=model,
            allowed_domains=allowed_domains,
        )
        url = f"{self._base_url}/codex/responses"
        headers = _build_codex_headers(credentials, self._session_id)

        reset_last_http_status()
        response = await self._send_codex_post(
            url,
            headers,
            payload,
            account_id=credentials.account_id or None,
        )
        if (
            response.status_code == 401
            and credentials.source_name == CHATGPT_OAUTH_MANAGED_SOURCE_NAME
        ):
            await response.aclose()
            try:
                with claimed_scope(purpose), refresh_exit(self._config.proxy):
                    credentials = await asyncio.to_thread(
                        force_refresh_managed_chatgpt_oauth_credentials,
                        credentials.account_id or None,
                    )
            except ChatGPTOAuthError as exc:
                set_last_http_status(401)
                raise WebSearchAuthError(
                    self.provider_id, str(exc), status_code=401
                ) from exc
            headers = _build_codex_headers(credentials, self._session_id)
            response = await self._send_codex_post(
                url,
                headers,
                payload,
                account_id=credentials.account_id or None,
            )

        try:
            raw_body = await response.aread()
        finally:
            await response.aclose()

        set_last_http_status(response.status_code)
        if response.status_code >= 400:
            raise map_status_error(self.provider_id, response)

        try:
            parsed = await asyncio.to_thread(
                parse_chatgpt_oauth_websearch_sse,
                raw_body,
                query=query,
                max_results=max_results,
                blocked_domains=blocked_domains,
            )
        except ChatGPTOAuthWebSearchAnswerError as exc:
            if exc.error_kind == "rate_limit":
                raise WebSearchRateLimitError(
                    self.provider_id, str(exc), status_code=exc.status_code
                ) from exc
            if exc.error_kind == "quota":
                raise WebSearchQuotaError(
                    self.provider_id, str(exc), status_code=exc.status_code
                ) from exc
            if exc.error_kind == "auth":
                raise WebSearchAuthError(
                    self.provider_id, str(exc), status_code=exc.status_code
                ) from exc
            raise WebSearchUpstreamError(
                self.provider_id, str(exc), status_code=exc.status_code
            ) from exc

        return WebSearchResponse(
            provider=self.provider_id,
            query=query,
            results=parsed.results,
            key_index=key_index,
            cost_usd=None,
            answer=parsed.answer,
            request_id=parsed.request_id,
            http_status=response.status_code,
        )
