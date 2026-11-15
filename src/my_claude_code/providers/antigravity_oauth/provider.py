"""The Antigravity door: a hand-written ``BaseProvider`` over ``v1internal``.

MCC's ``core/gemini_api/`` is the **inbound** adapter (a Gemini client talking
to MCC) and its converters run the opposite way. This module is the outbound
door, modelled on ``providers/anthropic_messages/provider.py``: the same
``BaseProvider`` contract, its own SSE reader, its own failure classification.

What P1 implements is the *scaffold*, and it is deliberately narrow:

* the envelope — ``{project, model, request, requestType, userAgent,
  requestId}`` with ``request.sessionId`` inside ``request``, ``safetySettings``
  deleted and a leaked ``toolConfig`` relocated;
* the SSE reader — ``data:`` lines only, the mandatory ``{"response": …}``
  unwrap, and terminality on "finishReason present OR ``[DONE]`` OR body end";
* the six-counter usage mapping, disjoint, with ``toolUsePromptTokenCount``
  recorded and never summed;
* a ``functionCall`` part that ends a tool-use stop with the whole
  ``input_json_delta`` and the backend's own ``id``;
* a 401 that refreshes once and retries once;
* the refusal classifier's two shapes, mapped to ``FREE_TIER``;
* a zero-output stream that is a classified failure, never an empty success.

What it deliberately does **not** implement (P3): the full Anthropic → Gemini
body translation (images, documents, tools, thinking, signatures, schema
sanitisation), thought-signature capture and replay, and the provisioning
sequence. The body builder below therefore converts text blocks and the
scalar generation knobs only, and says so where a block is skipped.

No production path can reach this door in P1 — there is no way to sign in — and
every test drives it against a loopback fake upstream.
"""

import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.config.constants import (
    ANTIGRAVITY_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
)
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.anthropic.streaming.emitter import format_sse_event
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.providers.base import (
    DEFAULT_REASONING_POLICY,
    BaseProvider,
    ProviderConfig,
    ReasoningPolicy,
)
from my_claude_code.providers.failure_policy import (
    ProviderFailureOverride,
    classify_provider_failure,
    retry_after_from_error,
)
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.runtime.served_models import ModelListingEvidence

from . import credentials, identity, listing, refusals
from .constants import (
    CACHED_CONTENT_TOKENS_KEY,
    CANDIDATES_TOKENS_KEY,
    CLIENT_IDENTITY_DEFAULT,
    CLIENT_PROFILE_DEFAULT,
    DEFAULT_BASE_URL,
    DEFAULT_PROJECT,
    ENVELOPE_USER_AGENT,
    FETCH_AVAILABLE_MODELS_PATH,
    PROMPT_TOKENS_KEY,
    PROVIDER_ID,
    REQUEST_ID_PREFIX,
    REQUEST_TYPE_AGENT,
    STREAM_GENERATE_CONTENT_PATH,
    THOUGHTS_TOKENS_KEY,
    TOOL_USE_PROMPT_TOKENS_KEY,
    TOTAL_TOKENS_KEY,
)
from .credentials import (
    AntigravityOAuthError,
    AntigravityRefreshError,
    OAuthTokens,
)

#: Google's block-style finish reasons. They are not a clean stop and must not
#: be reported as one; ``refusal`` is Anthropic's own word for "the model
#: declined to answer".
REFUSAL_FINISH_REASONS = frozenset(
    {"BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "RECITATION"}
)
#: The reasons that mean a clean stop.
STOP_FINISH_REASONS = frozenset({"STOP", "FINISH_REASON_UNSPECIFIED"})

SSE_DONE = "[DONE]"

#: How many recent raw finish reasons to keep for the operator. Bounded: this
#: is a diagnostic, not a log.
MAX_RECORDED_STOP_DETAILS = 20

_ZERO_OUTPUT_MESSAGE = (
    "The Antigravity upstream closed the stream without sending any output."
)


class AntigravityUnavailableError(AntigravityOAuthError):
    """No usable Antigravity credential exists for this request."""


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """One ``usageMetadata`` block, mapped onto Anthropic's usage fields.

    The six counters are **disjoint** (specs/IMPLEMENTATION §3.I): the captured
    frame's arithmetic closes only that way (153,609 + 17 + 94 = 153,720), and
    both translation twins compute ``output = candidates + thoughts``.
    """

    prompt_tokens: int | None = None
    candidates_tokens: int | None = None
    thoughts_tokens: int | None = None
    cached_tokens: int | None = None
    tool_use_prompt_tokens: int | None = None
    total_tokens: int | None = None

    @property
    def input_tokens(self) -> int | None:
        """``promptTokenCount`` minus ``cachedContentTokenCount``, or absent."""

        if self.prompt_tokens is None:
            return None
        return max(self.prompt_tokens - (self.cached_tokens or 0), 0)

    @property
    def output_tokens(self) -> int | None:
        """``candidatesTokenCount + thoughtsTokenCount``, or absent."""

        if self.candidates_tokens is None and self.thoughts_tokens is None:
            return None
        return (self.candidates_tokens or 0) + (self.thoughts_tokens or 0)

    def anthropic_usage(self, *, include_cache: bool) -> dict[str, int]:
        """The subset of Anthropic usage fields worth emitting.

        Absent stays absent: a counter the host never sent is not a zero. The
        one exception is ``cache_creation_input_tokens``, which Antigravity
        does not have and whose absence would break clients that read it —
        ``message_start`` carries an explicit ``0`` there (specs §3.I).
        """

        usage: dict[str, int] = {}
        if self.input_tokens is not None:
            usage["input_tokens"] = self.input_tokens
        if self.output_tokens is not None:
            usage["output_tokens"] = self.output_tokens
        if include_cache:
            usage["cache_read_input_tokens"] = self.cached_tokens or 0
            usage["cache_creation_input_tokens"] = 0
        return usage

    def side_record(self) -> dict[str, int]:
        """The counters that are recorded and never summed into a total."""

        record: dict[str, int] = {}
        if self.tool_use_prompt_tokens is not None:
            record["tool_use_prompt_tokens"] = self.tool_use_prompt_tokens
        if self.total_tokens is not None:
            record["total_tokens"] = self.total_tokens
        return record

    def invariant_holds(self) -> bool | None:
        """``input + cache_read == promptTokenCount``, when all three are known."""

        if self.prompt_tokens is None:
            return None
        return self.input_tokens == self.prompt_tokens - (self.cached_tokens or 0)


def _counter(metadata: Mapping[str, Any], key: str) -> int | None:
    value = metadata.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def usage_from_metadata(metadata: Any) -> UsageRecord | None:
    """Read one ``usageMetadata`` object. Absent stays absent."""

    document = metadata if isinstance(metadata, dict) else {}
    if not document:
        return None
    return UsageRecord(
        prompt_tokens=_counter(document, PROMPT_TOKENS_KEY),
        candidates_tokens=_counter(document, CANDIDATES_TOKENS_KEY),
        thoughts_tokens=_counter(document, THOUGHTS_TOKENS_KEY),
        cached_tokens=_counter(document, CACHED_CONTENT_TOKENS_KEY),
        tool_use_prompt_tokens=_counter(document, TOOL_USE_PROMPT_TOKENS_KEY),
        total_tokens=_counter(document, TOTAL_TOKENS_KEY),
    )


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------


def _text_of(content: Any) -> str:
    """The text a message/block carries, for the parts this build converts.

    A string is itself; a list contributes its text blocks. Everything else
    (images, documents, tool calls, thinking) is P3's translation and is
    skipped here rather than silently mangled.
    """

    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            kind = getattr(block, "type", None)
            if kind == "text":
                text = getattr(block, "text", "")
                if isinstance(text, str):
                    parts.append(text)
    return "\n".join(part for part in parts if part)


def derived_session_id(account_id: str, first_user_text: str) -> str:
    """A stable session id from the **whole** first user turn and the account.

    The peer's measured trap is a prefix hash: Claude Code opens with a long
    shared preamble, so every conversation in a project maps to one session and
    they evict each other's cache. The full text is the key.
    """

    digest = hashlib.sha256(
        f"{account_id}\n{first_user_text}".encode("utf-8", "replace")
    ).hexdigest()
    return f"antigravity-{digest[:32]}"


def _part_has_content(part: dict[str, Any]) -> bool:
    """Whether one part carries something the client could read.

    An empty text part is not content: ``message_start`` is a commitment (the
    executor commits an attempt on its first chunk), so it is held until there
    is something to commit to.
    """

    if isinstance(part.get("functionCall"), dict):
        return True
    if isinstance(part.get("inlineData"), dict):
        return True
    text = part.get("text")
    return isinstance(text, str) and text != ""


class AntigravityOAuthProvider(BaseProvider):
    """``BaseProvider`` over Antigravity's private ``v1internal`` surface."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        rate_limiter: ProviderRateLimiter | None = None,
        provider_id: str = PROVIDER_ID,
        account_id: str = "",
        base_url: str = "",
        client_identity: str = CLIENT_IDENTITY_DEFAULT,
        client_profile: str = CLIENT_PROFILE_DEFAULT,
        client_version: str = "",
        access_token: str = "",
        store_path: Path | None = None,
    ) -> None:
        super().__init__(config)
        self._rate_limiter = rate_limiter
        self._provider_id = provider_id
        self._account_id = account_id
        #: The door's base URL is a constructor argument, so a scratch proof can
        #: point it at a loopback fake. Production hosts come from
        #: ``constants.py``; the setting merely overrides the first rung.
        self._base_url = (base_url or config.base_url or DEFAULT_BASE_URL).rstrip("/")
        self._identity = identity.active_profile(
            profile=client_profile, version=client_version
        )
        self._client_identity = identity.client_identity(client_identity)
        self._access_token = access_token
        self._store_path = store_path
        self._tokens: OAuthTokens | None = None
        self._roster: listing.RosterParse | None = None
        self._client: httpx.AsyncClient | None = None
        self._failure_override: ProviderFailureOverride | None = None
        #: Raw ``finishReason`` values this process could not map cleanly, most
        #: recent last. Read by the tests and by an operator debugging an odd
        #: stop; never a request-log column in P1.
        self.stop_reason_details: list[dict[str, Any]] = []
        self._skipped_content_blocks = 0

    # -- small seams -------------------------------------------------------

    def set_failure_override(self, override: ProviderFailureOverride | None) -> None:
        """Classify provider-specific errors ahead of the shared policy."""

        self._failure_override = override

    @property
    def profile(self) -> identity.IdentityProfile:
        """The identity profile this instance presents."""

        return self._identity

    @property
    def skipped_content_blocks(self) -> int:
        """How many non-text blocks the P1 body builder skipped."""

        return self._skipped_content_blocks

    def throttle_remaining(self, model: str | None = None) -> float:
        return (
            0.0 if self._rate_limiter is None else self._rate_limiter.remaining_wait()
        )

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                proxy=self._config.proxy or None,
                timeout=httpx.Timeout(
                    self._config.http_read_timeout,
                    connect=self._config.http_connect_timeout,
                    read=self._config.http_read_timeout,
                    write=self._config.http_write_timeout,
                ),
            )
        return self._client

    async def cleanup(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- credential --------------------------------------------------------

    def _resolve_tokens(self) -> OAuthTokens:
        if self._tokens is not None and self._tokens.has_access_token:
            return self._tokens
        if self._access_token.strip():
            return OAuthTokens(access_token=self._access_token, source="setting")
        stored = credentials.load_tokens_for(self._account_id, path=self._store_path)
        if stored is not None and stored.has_access_token:
            self._tokens = stored
            return stored
        raw = (self._config.api_key or "").strip()
        if raw and raw != ANTIGRAVITY_OAUTH_MANAGED_CREDENTIAL_REFERENCE:
            return OAuthTokens(access_token=raw, source="setting")
        raise AntigravityUnavailableError(
            "No usable Antigravity credential is stored. Sign in on the "
            "provider card (sign-in arrives in the next release)."
        )

    def _project(self) -> str:
        if self._tokens is not None and self._tokens.project_id:
            return self._tokens.project_id
        stored = credentials.load_tokens_for(self._account_id, path=self._store_path)
        if stored is not None and stored.project_id:
            return stored.project_id
        return DEFAULT_PROJECT

    # -- the body ----------------------------------------------------------

    def build_request(
        self, request: MessagesRequest, tokens: OAuthTokens | None = None
    ) -> dict[str, Any]:
        """The envelope this door posts, key for key.

        ``{project, model, request, requestType, userAgent, requestId}`` with
        ``request.sessionId`` inside ``request``. The envelope's keys and the
        two normalisations (``safetySettings`` deleted, ``toolConfig``
        relocated) are the reference's, CONFIRMED at
        ``CLIProxyAPI antigravity_executor_request.go:718-775``.
        """

        self._skipped_content_blocks = 0
        contents: list[dict[str, Any]] = []
        first_user_text = ""
        for message in request.messages:
            role = "model" if message.role == "assistant" else "user"
            text = _text_of(message.content)
            if message.content is not None and not isinstance(message.content, str):
                blocks = message.content if isinstance(message.content, list) else []
                self._skipped_content_blocks += sum(
                    1 for block in blocks if getattr(block, "type", None) != "text"
                )
            if not text.strip():
                continue
            if role == "user" and not first_user_text:
                first_user_text = text
            contents.append({"role": role, "parts": [{"text": text}]})

        system_text = _text_of(request.system) if request.system is not None else ""
        generation_config: dict[str, Any] = {}
        if request.max_tokens is not None:
            generation_config["maxOutputTokens"] = request.max_tokens
        if request.temperature is not None:
            generation_config["temperature"] = request.temperature
        if request.top_p is not None:
            generation_config["topP"] = request.top_p
        if request.top_k is not None:
            generation_config["topK"] = request.top_k
        if request.stop_sequences:
            generation_config["stopSequences"] = list(request.stop_sequences)

        inner: dict[str, Any] = {
            "contents": contents,
            "sessionId": derived_session_id(self._account_id, first_user_text),
        }
        if system_text.strip():
            inner["systemInstruction"] = {"parts": [{"text": system_text}]}
        if generation_config:
            inner["generationConfig"] = generation_config
        # A Gemini-shaped ``extra_body`` is merged so a caller can supply what
        # this build does not translate yet. The two normalisations then run on
        # whatever arrived, exactly as the reference does.
        extra = request.extra_body if isinstance(request.extra_body, dict) else {}
        for key, value in extra.items():
            inner.setdefault(key, value)
        inner.pop("safetySettings", None)
        envelope: dict[str, Any] = {
            "project": self._project(),
            "model": request.model,
            "request": inner,
            "requestType": REQUEST_TYPE_AGENT,
            "userAgent": ENVELOPE_USER_AGENT,
            "requestId": f"{REQUEST_ID_PREFIX}{uuid.uuid4()}",
        }
        # A ``toolConfig`` that leaked to the top of the envelope is relocated
        # inside the request — never dropped, never duplicated.
        leaked_tool_config = envelope.pop("toolConfig", None)
        if leaked_tool_config is None:
            leaked_tool_config = extra.get("toolConfig")
        if leaked_tool_config is not None:
            inner["toolConfig"] = leaked_tool_config
        return envelope

    def _headers(self, tokens: OAuthTokens) -> dict[str, str]:
        return identity.request_headers(
            self._identity, tokens.access_token, identity=self._client_identity
        )

    def _build_request(
        self,
        client: httpx.AsyncClient,
        path: str,
        body: dict[str, Any],
        tokens: OAuthTokens,
    ) -> httpx.Request:
        """The request, with httpx's own default ``Accept`` removed.

        The native Go client sends no ``Accept`` header at all (NQ5), and
        httpx adds ``Accept: */*`` to every request it builds. Sending it would
        be a claim the profile says is not made, so it is deleted rather than
        left to the library.
        """

        request = client.build_request(
            "POST",
            self._url(path),
            json=body,
            headers=self._headers(tokens),
        )
        if self._identity.accept_header is None:
            request.headers.pop("accept", None)
        return request

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    # -- preflight ---------------------------------------------------------

    def preflight_stream(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> None:
        """Refuse before opening a stream when there is nothing to open it with."""

        if not request.model.strip():
            raise ExecutionFailure(
                kind=FailureKind.INVALID_REQUEST,
                status_code=400,
                message="A model is required.",
                retryable=False,
            )
        try:
            self._resolve_tokens()
        except AntigravityUnavailableError as error:
            raise ExecutionFailure(
                kind=FailureKind.UNAVAILABLE,
                status_code=503,
                message=str(error),
                retryable=False,
            ) from error

    # -- failure classification --------------------------------------------

    def _classify_failure(self, error: Exception) -> ExecutionFailure | None:
        """The provider override, in front of the shared classifier.

        ``AntigravityRefusal`` -> ``FREE_TIER`` (402 internally, never a
        credential charge). A credential-endpoint failure is classified with the
        existing kinds exactly as the two OAuth providers do — no new
        ``FailureKind`` and no OAuth-only retry loop.
        """

        refusal = refusals.classify_antigravity_failure(error)
        if refusal is not None:
            return refusal
        if isinstance(error, AntigravityUnavailableError):
            return ExecutionFailure(
                kind=FailureKind.UNAVAILABLE,
                status_code=503,
                message=str(error),
                retryable=False,
            )
        if not isinstance(error, AntigravityRefreshError):
            return None
        if error.definitive:
            return ExecutionFailure(
                kind=FailureKind.AUTHENTICATION,
                status_code=401,
                message=str(error),
                retryable=False,
            )
        if error.status_code == 429:
            return ExecutionFailure(
                kind=FailureKind.RATE_LIMIT,
                status_code=429,
                message=str(error),
                retryable=True,
                retry_after_seconds=retry_after_from_error(
                    error, cooldown=self.rate_limit_cooldown()
                ),
            )
        return ExecutionFailure(
            kind=FailureKind.OVERLOADED,
            status_code=529 if error.status_code >= 500 else 503,
            message=str(error),
            retryable=True,
            retry_after_seconds=retry_after_from_error(
                error, cooldown=self.rate_limit_cooldown()
            ),
        )

    def _failure_for_response(
        self, response: httpx.Response, payload: Any
    ) -> ExecutionFailure:
        refusal = refusals.refusal_from_error_response(response.status_code, payload)
        if refusal is not None:
            return refusal.failure()
        error = httpx.HTTPStatusError(
            f"Antigravity answered HTTP {response.status_code}",
            request=response.request,
            response=response,
        )
        return classify_provider_failure(
            error,
            provider_name=self._provider_id,
            read_timeout_s=self._config.http_read_timeout,
            request_id=None,
            mark_rate_limited=lambda _seconds: None,
            provider_failure_override=self._failure_override,
            cooldown_seconds=self._config.rate_limit_cooldown_seconds,
            cooldown=self.rate_limit_cooldown(),
        )

    @staticmethod
    async def _payload_of(response: httpx.Response) -> Any:
        """The answer's JSON body, or ``None`` — reading a streamed body first."""

        try:
            if not response.is_closed:
                await response.aread()
            return response.json()
        except ValueError, TypeError, httpx.HTTPError:
            return None

    # -- the 401 -> refresh once path --------------------------------------

    async def _retry_after_auth_failure(self, status_code: int) -> bool:
        """Refresh the credential once, so one stale token costs one retry.

        Exactly one retry, inside the attempt the executor already budgeted: no
        extra layer on the retry ladder.
        """

        if status_code != 401:
            return False
        try:
            tokens = self._resolve_tokens()
        except AntigravityUnavailableError:
            return False
        if not tokens.has_refresh_token:
            return False
        try:
            self._tokens = await credentials.refresh_tokens(
                tokens, account_id=self._account_id, path=self._store_path
            )
        except AntigravityOAuthError:
            # The refresh failure is classified by the caller's override when
            # this exception reaches it; there is nothing to retry here.
            raise
        return True

    # -- the stream --------------------------------------------------------

    async def _post_stream(
        self, body: dict[str, Any], tokens: OAuthTokens
    ) -> httpx.Response:
        client = self._http()
        request = self._build_request(
            client, STREAM_GENERATE_CONTENT_PATH, body, tokens
        )
        return await client.send(request, stream=True)

    async def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        """Stream one Anthropic-shaped answer from the Antigravity door."""

        try:
            tokens = self._resolve_tokens()
        except AntigravityUnavailableError as error:
            raise self._classified(error) from error

        body = self.build_request(request, tokens)
        response = await self._post_stream(body, tokens)
        try:
            if response.status_code == 401:
                try:
                    refreshed = await self._retry_after_auth_failure(
                        response.status_code
                    )
                except AntigravityOAuthError as error:
                    # The refresh failed: classify it as the provider sees it
                    # (definitive -> AUTHENTICATION, 429 -> RATE_LIMIT, 5xx ->
                    # OVERLOADED) rather than letting a bare exception reach
                    # the shared classifier as UPSTREAM.
                    raise self._classified(error) from error
                if refreshed:
                    await response.aclose()
                    tokens = self._resolve_tokens()
                    body = self.build_request(request, tokens)
                    response = await self._post_stream(body, tokens)
            if response.status_code >= 400:
                payload = await self._payload_of(response)
                raise self._failure_for_response(response, payload)
            async for event in self._read_stream(response, request):
                yield event
        finally:
            await response.aclose()

    def _classified(self, error: Exception) -> ExecutionFailure:
        override = self._classify_failure(error)
        if override is not None:
            return override
        return ExecutionFailure(
            kind=FailureKind.UPSTREAM,
            status_code=502,
            message=str(error),
            retryable=False,
        )

    async def _read_stream(
        self, response: httpx.Response, request: MessagesRequest
    ) -> AsyncIterator[str]:
        """Parse the host's SSE and emit Anthropic's.

        Terminality is three conditions — a ``finishReason``, ``[DONE]``, or the
        end of the body — because a real tool-call stream can end with **no**
        ``finishReason`` at all (the captured frame). A ``functionCall`` part
        alone is what makes the stop a tool stop.
        """

        message_id = f"msg_{uuid.uuid4().hex}"
        started = False
        block_index = 0
        open_block: str | None = None
        usage: UsageRecord | None = None
        stop_reason = "end_turn"
        saw_output = False
        saw_tool_call = False
        terminal = False

        buffer = ""
        async for line in response.aiter_lines():
            stripped = line.rstrip("\r")
            if stripped == "":
                if buffer:
                    # A blank line ends a multi-line payload that never parsed;
                    # a genuinely malformed frame is dropped, never guessed at.
                    buffer = ""
                continue
            if not stripped.startswith("data:"):
                continue
            data = stripped[len("data:") :].strip()
            if data == SSE_DONE:
                terminal = True
                break
            # Multi-line JSON is tolerated by buffering: a payload that does not
            # parse yet is a continuation, not a frame.
            candidate = f"{buffer}\n{data}" if buffer else data
            try:
                payload = json.loads(candidate)
            except ValueError:
                buffer = candidate
                continue
            buffer = ""

            if isinstance(payload, dict) and "error" in payload:
                refusal = refusals.refusal_from_error_response(502, payload)
                if refusal is not None:
                    raise refusal.failure()
                error = payload.get("error")
                message = (
                    str(error.get("message"))
                    if isinstance(error, dict) and error.get("message")
                    else "Antigravity sent an error frame"
                )
                raise ExecutionFailure(
                    kind=FailureKind.UPSTREAM,
                    status_code=502,
                    message=message,
                    retryable=True,
                )

            inner = payload.get("response") if isinstance(payload, dict) else None
            document = inner if isinstance(inner, dict) else payload
            if not isinstance(document, dict):
                continue
            frame_usage = usage_from_metadata(document.get("usageMetadata"))
            if frame_usage is not None:
                # The final delta is authoritative: early chunks carry interim
                # prompt counts and no cached count.
                usage = frame_usage
            candidates = document.get("candidates")
            if not isinstance(candidates, list) or not candidates:
                continue
            candidate = candidates[0] if isinstance(candidates[0], dict) else {}
            content = candidate.get("content")
            parts = content.get("parts") if isinstance(content, dict) else None
            parts = (
                [part for part in parts if isinstance(part, dict)]
                if isinstance(parts, list)
                else []
            )

            if not started and any(_part_has_content(part) for part in parts):
                started = True
                initial = usage.anthropic_usage(include_cache=True) if usage else {}
                yield format_sse_event(
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {
                            "id": message_id,
                            "type": "message",
                            "role": "assistant",
                            "content": [],
                            "model": request.model,
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {
                                "input_tokens": initial.get("input_tokens", 0),
                                "output_tokens": 0,
                                **(
                                    {
                                        "cache_read_input_tokens": initial[
                                            "cache_read_input_tokens"
                                        ],
                                        "cache_creation_input_tokens": 0,
                                    }
                                    if "cache_read_input_tokens" in initial
                                    else {}
                                ),
                            },
                        },
                    },
                )

            for part in parts:
                function_call = part.get("functionCall")
                if isinstance(function_call, dict):
                    saw_output = True
                    saw_tool_call = True
                    if open_block is not None:
                        yield format_sse_event(
                            "content_block_stop",
                            {"type": "content_block_stop", "index": block_index},
                        )
                        block_index += 1
                        open_block = None
                    call_id = function_call.get("id")
                    tool_id = (
                        call_id
                        if isinstance(call_id, str) and call_id
                        else f"toolu_{uuid.uuid4().hex}"
                    )
                    name = function_call.get("name")
                    args = function_call.get("args")
                    yield format_sse_event(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": block_index,
                            "content_block": {
                                "type": "tool_use",
                                "id": tool_id,
                                "name": name if isinstance(name, str) else "",
                                "input": {},
                            },
                        },
                    )
                    yield format_sse_event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": block_index,
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": json.dumps(
                                    args if isinstance(args, dict) else {}
                                ),
                            },
                        },
                    )
                    yield format_sse_event(
                        "content_block_stop",
                        {"type": "content_block_stop", "index": block_index},
                    )
                    block_index += 1
                    stop_reason = "tool_use"
                    continue

                inline = part.get("inlineData")
                if isinstance(inline, dict):
                    saw_output = True
                    if open_block is not None:
                        yield format_sse_event(
                            "content_block_stop",
                            {"type": "content_block_stop", "index": block_index},
                        )
                        block_index += 1
                        open_block = None
                    yield format_sse_event(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": block_index,
                            "content_block": {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": str(
                                        inline.get("mimeType") or "image/png"
                                    ),
                                    "data": str(inline.get("data") or ""),
                                },
                            },
                        },
                    )
                    yield format_sse_event(
                        "content_block_stop",
                        {"type": "content_block_stop", "index": block_index},
                    )
                    block_index += 1
                    continue

                text = part.get("text")
                if not isinstance(text, str) or text == "":
                    continue
                saw_output = True
                if part.get("thought") is True:
                    if open_block != "thinking":
                        if open_block is not None:
                            yield format_sse_event(
                                "content_block_stop",
                                {"type": "content_block_stop", "index": block_index},
                            )
                            block_index += 1
                        open_block = "thinking"
                        yield format_sse_event(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": block_index,
                                "content_block": {"type": "thinking", "thinking": ""},
                            },
                        )
                    yield format_sse_event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": block_index,
                            "delta": {"type": "thinking_delta", "thinking": text},
                        },
                    )
                    continue
                if open_block != "text":
                    if open_block is not None:
                        yield format_sse_event(
                            "content_block_stop",
                            {"type": "content_block_stop", "index": block_index},
                        )
                        block_index += 1
                    open_block = "text"
                    yield format_sse_event(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": block_index,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )
                yield format_sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": block_index,
                        "delta": {"type": "text_delta", "text": text},
                    },
                )

            finish_reason = candidate.get("finishReason")
            if isinstance(finish_reason, str) and finish_reason:
                terminal = True
                stop_reason = self._stop_reason_for(
                    finish_reason, saw_tool_call=saw_tool_call
                )
                break

        if buffer.strip():
            try:
                payload = json.loads(buffer)
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                inner = payload.get("response")
                document = inner if isinstance(inner, dict) else payload
                frame_usage = usage_from_metadata(document.get("usageMetadata"))
                if frame_usage is not None:
                    usage = frame_usage
        if terminal and stop_reason == "end_turn" and saw_tool_call:
            stop_reason = "tool_use"

        if open_block is not None:
            yield format_sse_event(
                "content_block_stop",
                {"type": "content_block_stop", "index": block_index},
            )

        if not saw_output:
            raise ExecutionFailure(
                kind=FailureKind.OVERLOADED,
                status_code=502,
                message=_ZERO_OUTPUT_MESSAGE,
                retryable=True,
            )

        if usage is not None and usage.invariant_holds() is False:
            logger.warning(
                "Antigravity usage invariant failed: prompt={} cached={} input={}",
                usage.prompt_tokens,
                usage.cached_tokens,
                usage.input_tokens,
            )
        delta_usage = usage.anthropic_usage(include_cache=False) if usage else {}
        yield format_sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": delta_usage,
            },
        )
        yield format_sse_event("message_stop", {"type": "message_stop"})

    def _stop_reason_for(self, finish_reason: str, *, saw_tool_call: bool) -> str:
        """Map the host's reason honestly.

        A tool call already made the stop a tool stop. Beyond that: a clean
        ``STOP`` is ``end_turn``, ``MAX_TOKENS`` is ``max_tokens``, a block is
        ``refusal``. **Anything else is never reported as a clean stop** — the
        raw value is recorded and the honest Anthropic equivalent is
        ``end_turn`` with the detail kept, or ``max_tokens`` for the one
        truncation-shaped reason the spec allows.
        """

        if saw_tool_call:
            return "tool_use"
        if finish_reason in STOP_FINISH_REASONS:
            return "end_turn"
        if finish_reason == "MAX_TOKENS":
            return "max_tokens"
        if finish_reason in REFUSAL_FINISH_REASONS:
            self._record_stop_detail(finish_reason, "refusal")
            return "refusal"
        self._record_stop_detail(finish_reason, "end_turn")
        logger.info(
            "Antigravity sent a finishReason this build does not map: {!r}. "
            "Reported as end_turn and recorded rather than silently treated "
            "as a clean stop.",
            finish_reason,
        )
        return "end_turn"

    def _record_stop_detail(self, finish_reason: str, stop_reason: str) -> None:
        self.stop_reason_details.append(
            {"finishReason": finish_reason, "stop_reason": stop_reason}
        )
        if len(self.stop_reason_details) > MAX_RECORDED_STOP_DETAILS:
            del self.stop_reason_details[:-MAX_RECORDED_STOP_DETAILS]

    # -- listing -----------------------------------------------------------

    async def _fetch_roster(self) -> listing.RosterParse:
        tokens = self._resolve_tokens()
        client = self._http()
        body: dict[str, Any] = {}
        project = self._project()
        if project:
            body["project"] = project
        response = await client.send(
            self._build_request(client, FETCH_AVAILABLE_MODELS_PATH, body, tokens)
        )
        if response.status_code >= 400:
            payload = await self._payload_of(response)
            raise self._failure_for_response(response, payload)
        parse = listing.parse_available_models(await self._payload_of(response))
        self._roster = parse
        return parse

    def _roster_evidence(
        self, parse: listing.RosterParse
    ) -> dict[str, ModelListingEvidence]:
        """The S1 gateway rung: the endpoint's own roster."""

        return {
            model_id: ModelListingEvidence(
                provenance=listing.ModelListingProvenance.GATEWAY,
                detail="fetchAvailableModels",
            )
            for model_id in parse.listed_ids()
        }

    async def _listing_evidence(self) -> dict[str, ModelListingEvidence]:
        """The listing rungs, strongest first, with the dated seed as the floor.

        The gateway rung is the roster the endpoint publishes; when it cannot be
        reached the seed answers, exactly as ``resolve_served_models`` documents.
        """

        try:
            parse = await self._fetch_roster()
        except Exception as error:
            logger.warning("Antigravity model list declined: {}", error)
            return listing.resolve(gateway=None)
        return listing.resolve(gateway=lambda: self._roster_evidence(parse))

    async def list_model_ids(self) -> frozenset[str]:
        """The listable ids, from ``fetchAvailableModels`` (S1) or the seed (S5)."""

        return frozenset(await self._listing_evidence())

    async def list_model_infos(self) -> frozenset[ProviderModelInfo]:
        """The roster's records, each carrying its own published output cap."""

        try:
            parse = await self._fetch_roster()
        except Exception:
            return listing.model_infos(listing.RosterParse())
        return listing.model_infos(parse)

    def output_cap_for_model(self, model_id: str) -> int | None:
        """The published ceiling for one model, or ``None`` (the ladder decides)."""

        if self._roster is None:
            return None
        return listing.output_cap_for(self._roster, model_id)


__all__ = [
    "REFUSAL_FINISH_REASONS",
    "SSE_DONE",
    "AntigravityOAuthProvider",
    "AntigravityUnavailableError",
    "UsageRecord",
    "derived_session_id",
    "usage_from_metadata",
]
