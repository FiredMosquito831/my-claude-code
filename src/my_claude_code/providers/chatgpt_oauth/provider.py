"""Direct ChatGPT/Codex OAuth provider using the Responses API."""

import asyncio
import platform
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
from loguru import logger

from my_claude_code.application.errors import ApplicationUnavailableError
from my_claude_code.application.model_metadata import (
    ModelListingEvidence,
    ModelListingProvenance,
    ProviderModelInfo,
)
from my_claude_code.config.constants import HTTP_CONNECT_TIMEOUT_DEFAULT
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.anthropic.streaming import AnthropicStreamLedger
from my_claude_code.core.diagnostics import (
    exception_cause_types,
    redacted_exception_traceback,
)
from my_claude_code.core.failures import ExecutionFailure
from my_claude_code.core.reasoning import (
    DEFAULT_REASONING_POLICY,
    ReasoningAdaptationKind,
    ReasoningDialect,
    ReasoningEffort,
    ReasoningPolicy,
    narrow_dialect_by_rejections,
)
from my_claude_code.core.request_log import observed_served_models
from my_claude_code.core.trace import trace_event
from my_claude_code.core.version import package_version
from my_claude_code.core.wire_capture import (
    record_reasoning_adaptation,
    record_response_shape,
    record_wire_request,
    start_response_shape,
)
from my_claude_code.providers.base import BaseProvider, ProviderConfig
from my_claude_code.providers.failure_policy import classify_provider_failure
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.recovery import (
    ReasoningStripRecovery,
    RecoveryLadder,
    RecoveryMemory,
)
from my_claude_code.providers.runtime.served_models import resolve_served_models

from .codex_catalogue import catalogue_evidence, load_codex_catalogue
from .conversion import build_chatgpt_oauth_request_body
from .credentials import (
    CODEX_OAUTH_ORIGINATOR,
    ChatGPTOAuthError,
    force_refresh_managed_chatgpt_oauth_credentials,
    load_chatgpt_oauth_credentials,
    stored_chatgpt_plan_type,
)
from .response_headers import OBSERVER as RESPONSE_HEADER_OBSERVER
from .streaming import (
    ChatGPTOAuthStreamConverter,
    iter_chatgpt_oauth_sse_events,
    note_responses_event_shape,
)

CHATGPT_OAUTH_DEFAULT_BASE = "https://chatgpt.com/backend-api"
#: Both ids this same provider is registered under. ``provider_catalog.py``
#: gives ``openai`` the *same* credential env and the same factory entry
#: (``runtime/factory.py`` maps both to ``_create_chatgpt_oauth``), so one
#: subscription can be routed as either and the request log records whichever
#: id the route named. The observed rung has to look under both or it would
#: lose half its evidence depending on how the operator spelled the route.
CHATGPT_OAUTH_PROVIDER_IDS: tuple[str, ...] = ("chatgpt_oauth", "openai")

#: Last resort, and nothing else: the five ids Codex CLI 0.151.0 publishes with
#: ``visibility: "list"``. It exists so a brand-new offline install with no
#: Codex and no request log still draws a picker, and it is labelled ``seed``
#: on the Models page precisely because nobody stands behind it. No filter is
#: applied to it -- the list that preceded it held fifteen ids of which the
#: filter reading it deleted eight, so more than half of it was unreachable.
CHATGPT_OAUTH_SEED_MODELS: tuple[str, ...] = (
    "gpt-5.2",
    "gpt-5.5",
    "gpt-5.6-luna",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
)

#: What the backend says when it does not have a model, in its own words.
#: Matched case-insensitively against the error body beside a 404.
MODEL_DENIAL_MARKERS: tuple[str, ...] = (
    "model_not_found",
    "unsupported_model",
    "does not exist",
)


class WithheldModelIds:
    """Model ids this process has watched the backend refuse, by name.

    Deliberately the weakest possible form of a negative:

    * **per-process and never persisted** -- a restart forgets it, so a model
      OpenAI adds (or a 404 that was really an outage) costs one wasted
      request per process rather than a permanent hole in the catalogue;
    * **withheld from listings only**. It never benches a credential, never
      marks a model unsupported, and never removes a ref the operator
      configured: exactly the hide-only contract this project's visibility
      globs already have. A route that names a withheld id still resolves and
      still serves.

    It replaces a hand-written blocklist whose two entries happened to be
    right for a reason the code never recorded.
    """

    __slots__ = ("_ids",)

    def __init__(self) -> None:
        self._ids: set[str] = set()

    def remember(self, model_id: str) -> bool:
        """Record one refusal; ``True`` the first time this id is seen."""
        if not model_id.strip() or model_id in self._ids:
            return False
        self._ids.add(model_id)
        return True

    def __contains__(self, model_id: object) -> bool:
        return model_id in self._ids

    def snapshot(self) -> frozenset[str]:
        return frozenset(self._ids)

    def clear(self) -> None:
        self._ids.clear()


#: Process-wide, because a provider instance is rebuilt on every config apply
#: while the backend's opinion of a model id is not.
WITHHELD_MODEL_IDS = WithheldModelIds()


def is_model_denial(status_code: int, error_text: str) -> bool:
    """Whether one upstream refusal was about the *model*, not the request.

    A 404 on this endpoint can only be about the model: the path is fixed and
    every other 4xx names a field. The three markers cover the wording seen in
    OpenAI's Responses errors elsewhere; a 400 that merely mentions one is
    accepted too, because the status a gateway chooses for an unknown model is
    not consistent and the body is the part that actually says so.
    """
    haystack = error_text.lower()
    if status_code == 404:
        return True
    return status_code == 400 and any(
        marker in haystack for marker in MODEL_DENIAL_MARKERS
    )


def _user_agent() -> str:
    """Return the Codex OAuth client identity used by the upstream provider."""
    return (
        f"{CODEX_OAUTH_ORIGINATOR}/{package_version()} "
        f"({platform.system()} {platform.release()}; {platform.machine()})"
    )


def vendor_client_evidence() -> dict[str, ModelListingEvidence]:
    """Rung S2 -- Codex CLI's own bundled catalogue, read off disk.

    The plan is decoded from the stored ID token locally; no network call and
    no token refresh happen here or anywhere below. An unknown plan applies no
    plan filter at all, because unknown is not excluded.
    """
    catalogue = load_codex_catalogue()
    if catalogue is None:
        return {}
    return catalogue_evidence(catalogue, plan_type=stored_chatgpt_plan_type())


def observed_evidence() -> dict[str, ModelListingEvidence]:
    """Rung S3 -- ids this credential has already been served successfully.

    The one rung that is proof rather than a document, and the reason S2 and
    S3 union instead of one shadowing the other: a catalogue can be stale
    about a model the user is using right now.
    """
    successes: dict[str, int] = {}
    last_seen: dict[str, str] = {}
    for provider_id in CHATGPT_OAUTH_PROVIDER_IDS:
        for observation in observed_served_models(provider_id):
            successes[observation.model_id] = (
                successes.get(observation.model_id, 0) + observation.successes
            )
            if observation.last_ts_iso > last_seen.get(observation.model_id, ""):
                last_seen[observation.model_id] = observation.last_ts_iso
    return {
        model_id: ModelListingEvidence(
            provenance=ModelListingProvenance.OBSERVED,
            detail=(
                f"served {count}x, last {last_seen[model_id]}"
                if last_seen.get(model_id)
                else f"served {count}x"
            ),
        )
        for model_id, count in successes.items()
    }


def seed_evidence() -> dict[str, ModelListingEvidence]:
    """Rung S5 -- the literal list, reached only when nothing else answered."""
    return {
        model_id: ModelListingEvidence(
            provenance=ModelListingProvenance.SEED,
            detail="offline seed list; no source on this machine confirmed it",
        )
        for model_id in CHATGPT_OAUTH_SEED_MODELS
    }


def chatgpt_oauth_served_models() -> dict[str, ModelListingEvidence]:
    """Resolve this provider's model ids from every offline source it has.

    No gateway rung: the backend publishes no model-list endpoint, and its own
    client does not call one either -- Codex 0.151.0 contains zero occurrences
    of ``backend-api/models``. Nothing here is added speculatively.

    **No models.dev rung either, and that is a deliberate departure.** The
    ``chatgpt_oauth -> openai`` alias stays exactly where it is and keeps
    supplying every capability and every limit, but it is the wrong source for
    *existence*: its bucket describes OpenAI's paid API deployment, so it
    carries 44 ids this surface has never served -- embeddings, image models,
    the whole ``*-codex`` family -- and the hand filter that used to trim them
    is what this change removes. Using it as a fallback id set would put those
    44 ids in the picker the moment Codex was uninstalled.
    """
    resolved = resolve_served_models(
        vendor_client=vendor_client_evidence,
        observed=observed_evidence,
        seed=seed_evidence,
        log_tag="CHATGPT_OAUTH",
    )
    return {
        model_id: evidence
        for model_id, evidence in resolved.items()
        if model_id not in WITHHELD_MODEL_IDS
    }


def _build_headers(credentials: Any, session_id: str) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {credentials.access_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "OpenAI-Beta": "responses=experimental",
        "originator": CODEX_OAUTH_ORIGINATOR,
        "User-Agent": _user_agent(),
        "session-id": session_id,
    }
    if credentials.account_id:
        headers["ChatGPT-Account-ID"] = credentials.account_id
    return headers


CHATGPT_OAUTH_REASONING_DIALECT = ReasoningDialect(
    effort_values=frozenset(ReasoningEffort),
    toggle=True,
    off=False,
    effort_field="reasoning.effort",
    toggle_field="reasoning.effort",
)
"""The Responses endpoint takes ``reasoning.effort`` and nothing else.

``off=False`` on purpose: an explicit OFF omits the whole ``reasoning``
block rather than spelling a disable, so the endpoint has no OFF at
all. It also has no bare ON -- a policy naming no effort falls back to
the endpoint's long-standing ``medium`` -- so the toggle channel is
real and its on-value is a default rung.
"""


class ChatGPTOAuthProvider(BaseProvider):
    """ChatGPT/Codex OAuth provider using the Responses API."""

    def reasoning_dialect(self, model_id: str) -> ReasoningDialect:
        """See :data:`CHATGPT_OAUTH_REASONING_DIALECT`, minus what was refused.

        The endpoint has retired reasoning-shaped fields before. A model that
        answers a ``reasoning`` block with a 400 has said so itself, and that
        outranks the declaration above for that model.
        """
        rejections = self._recovery_memory.rejections_for(model_id)
        if not rejections:
            return CHATGPT_OAUTH_REASONING_DIALECT
        return narrow_dialect_by_rejections(CHATGPT_OAUTH_REASONING_DIALECT, rejections)

    def _remember_reasoning_rejection(self, body: dict[str, Any], field: str) -> None:
        """Record that this model refused a reasoning field, once it is proven.

        Reached only after the stripped body was actually accepted, so the
        strip is what fixed it.
        """
        model = body.get("model")
        if not isinstance(model, str):
            return
        if not self._recovery_memory.remember_rejection(model, field):
            return
        record_reasoning_adaptation(
            ReasoningAdaptationKind.SUPPRESSED,
            f"CHATGPT_OAUTH rejected {field!r} for {model}; the request was "
            f"retried without it and this model will not be sent it again.",
        )
        logger.warning(
            "CHATGPT_OAUTH_STREAM: {!r} learned as rejected for {} -- "
            "later requests omit it",
            field,
            model,
        )

    def __init__(
        self,
        config: ProviderConfig,
        *,
        rate_limiter: ProviderRateLimiter,
        account_id: str = "",
    ):
        super().__init__(config)
        self._rate_limiter = rate_limiter
        self._base_url = (config.base_url or CHATGPT_OAUTH_DEFAULT_BASE).rstrip("/")
        self._account_id = account_id
        self._api_key = config.api_key
        self._proxy = config.proxy
        self._session_id = str(uuid.uuid4())
        # What this endpoint has taught this process about itself. Only the
        # reasoning half is wired: the Responses encoder emits no output-token
        # field at all, so there is no budget for a host to cap and an
        # output-cap rung here would be a rung that can never fire.
        self._recovery_memory = RecoveryMemory()
        self._recovery_ladder = RecoveryLadder(
            (ReasoningStripRecovery(log_tag="CHATGPT_OAUTH_STREAM").rung(),)
        )
        self._client = httpx.AsyncClient(
            proxy=config.proxy if config.proxy else None,
            timeout=httpx.Timeout(
                config.http_read_timeout,
                connect=config.http_connect_timeout or HTTP_CONNECT_TIMEOUT_DEFAULT,
                read=config.http_read_timeout,
                write=config.http_write_timeout,
            ),
        )

    def throttle_remaining(self, model: str | None = None) -> float:
        """Seconds this credential is rate-limited for; 0 when free to serve."""
        return self._rate_limiter.remaining_wait()

    async def cleanup(self) -> None:
        await self._client.aclose()

    async def list_model_ids(self) -> frozenset[str]:
        """Return the ChatGPT/Codex OAuth model ids this credential may use.

        What this replaced: a four-id allowlist copied from OpenCode plus a
        ``float(version) > 5.4`` heuristic. Of the seven ids it admitted,
        ``gpt-5.3-codex-spark`` appears in no vendor artefact on this machine,
        and ``gpt-5.4`` / ``gpt-5.4-mini`` were retired by OpenAI on
        2026-08-31; meanwhile ``gpt-5.2`` -- currently listed by OpenAI's own
        client -- was excluded because ``5.2`` is not greater than ``5.4``.
        The heuristic also could not survive ``gpt-5.10``, which parses as
        ``5.1``.

        See :func:`chatgpt_oauth_served_models` for what answers instead.
        """
        return frozenset(await self.list_model_evidence())

    async def list_model_evidence(self) -> dict[str, ModelListingEvidence]:
        """The served ids plus why each one is listed.

        Off the event loop: rung S2 memory-maps a 300 MB executable on its
        first call per Codex version and rung S3 runs a read-only SQL query,
        and neither belongs on a thread that is also serving ``/v1/messages``.
        Both run inside discovery, never on the request path.
        """
        return await asyncio.to_thread(chatgpt_oauth_served_models)

    async def list_model_infos(self) -> frozenset[ProviderModelInfo]:
        """Model infos carrying provenance and lifecycle facts -- no numbers.

        Every capability field is left ``None`` on purpose. The evidence chain
        answers "which ids exist"; the resolution ladder answers "what are they
        like", and it keeps answering it through the existing
        ``chatgpt_oauth -> openai`` models.dev alias exactly as before. Codex's
        catalogue does publish a ``context_window`` that disagrees with
        models.dev, and reading it here would move a number from tier 3 to
        tier 1 -- a change to the ladder, which is out of scope for this
        change and was decided against explicitly.
        """
        return frozenset(
            ProviderModelInfo(model_id=model_id, listing=evidence)
            for model_id, evidence in (await self.list_model_evidence()).items()
        )

    def preflight_stream(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> None:
        """Validate the upstream request before streaming."""
        build_chatgpt_oauth_request_body(request, reasoning=reasoning)

    async def _send_stream_request(
        self,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
    ) -> httpx.Response:
        """Build and send a streaming POST, preserving 401 for one refresh.

        ``httpx.AsyncClient.stream`` returns an async context manager, which is
        not awaitable and therefore cannot be passed directly to the retry
        helper. We instead build the request and call ``send(..., stream=True)``,
        which returns an awaitable ``Response`` while still keeping the body
        stream open until we explicitly close it.
        """
        request = self._client.build_request("POST", url, headers=headers, json=body)
        response = await self._client.send(request, stream=True)
        # Every response, success or refusal: the quota windows and the credits
        # balance ride on both, and until now this provider threw all of them
        # away. Allow-listed, stored verbatim, never computed.
        RESPONSE_HEADER_OBSERVER.observe(
            response.headers, status_code=response.status_code
        )
        if response.status_code >= 400 and response.status_code != 401:
            error_body = await response.aread()
            await response.aclose()
            error_text = error_body.decode("utf-8", errors="replace")
            self._remember_model_denial(body, response.status_code, error_text)
            raise httpx.HTTPStatusError(
                f"ChatGPT OAuth API error {response.status_code}: {error_text[:1000]}",
                request=request,
                response=response,
            )
        return response

    def _remember_model_denial(
        self, body: Mapping[str, Any], status_code: int, error_text: str
    ) -> None:
        """Withhold a model the backend has just said it does not have.

        A read signal, not an invented threshold: the id leaves future
        listings because the backend refused it by name, and it comes back on
        the next restart. Nothing else changes -- the credential is untouched
        and a route that names this id still resolves and still serves.
        """
        model = body.get("model")
        if not isinstance(model, str) or not is_model_denial(status_code, error_text):
            return
        if not WITHHELD_MODEL_IDS.remember(model):
            return
        logger.warning(
            "CHATGPT_OAUTH: {} was refused by the backend (HTTP {}) and will be "
            "withheld from model listings for the rest of this process. "
            "Routes naming it still resolve.",
            model,
            status_code,
        )

    def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        tag = "CHATGPT_OAUTH"
        req_tag = f" request_id={request_id}" if request_id else ""
        logger.debug("{}_STREAM: starting{}", tag, req_tag)

        try:
            credentials = load_chatgpt_oauth_credentials(
                access_token=self._api_key or None,
                account_id=self._account_id or None,
            )
        except ChatGPTOAuthError as exc:
            logger.error("{}_ERROR:{} {}", tag, req_tag, exc)
            raise ApplicationUnavailableError(str(exc)) from exc

        body = build_chatgpt_oauth_request_body(request, reasoning=reasoning)
        url = f"{self._base_url}/codex/responses"
        headers = _build_headers(credentials, self._session_id)

        trace_event(
            stage="provider",
            event="provider.request.sent",
            source="provider",
            provider=tag,
            request_id=request_id,
            gateway_model=request.model,
            downstream_model=body.get("model"),
            message_count=len(body.get("input", [])),
            tool_count=len(body.get("tools", [])),
            body={
                "model": body.get("model"),
                "input_count": len(body.get("input", [])),
                "tool_count": len(body.get("tools", [])),
            },
        )

        async def _stream() -> AsyncIterator[str]:
            message_id = f"msg_{uuid.uuid4()}"
            ledger = AnthropicStreamLedger(
                message_id,
                request.model,
                input_tokens,
                log_raw_events=self._config.log_raw_sse_events,
            )
            converter = ChatGPTOAuthStreamConverter(
                ledger,
                log_raw_events=self._config.log_raw_sse_events,
            )

            async with self._rate_limiter.concurrency_slot():
                try:
                    active_credentials = credentials
                    active_headers = headers
                    refreshed_after_unauthorized = False
                    # Per attempt chain: a rung fires at most once, and the
                    # refusal is only written down once the retry is accepted.
                    used_retry_kinds: set[str] = set()
                    stripped_reasoning: str | None = None
                    # A local of this generator, not the enclosing function's
                    # body: a recovery rewrites what goes on the wire for this
                    # attempt only.
                    attempt_body = body
                    while True:
                        # Commit boundary: the body is final once it is handed
                        # to the sender. Headers are not recorded -- they carry
                        # the bearer token.
                        record_wire_request(attempt_body)
                        try:
                            response = await self._rate_limiter.execute_with_retry(
                                self._send_stream_request,
                                provider_failure_override=(
                                    self._provider_failure_override
                                ),
                                url=url,
                                headers=active_headers,
                                body=attempt_body,
                            )
                            if (
                                response.status_code == 401
                                and active_credentials.source_name == "fcc-managed"
                            ):
                                await response.aclose()
                                try:
                                    active_credentials = await asyncio.to_thread(
                                        force_refresh_managed_chatgpt_oauth_credentials
                                    )
                                except ChatGPTOAuthError as exc:
                                    raise ApplicationUnavailableError(str(exc)) from exc
                                active_headers = _build_headers(
                                    active_credentials, self._session_id
                                )
                                refreshed_after_unauthorized = True
                                response = await self._rate_limiter.execute_with_retry(
                                    self._send_stream_request,
                                    provider_failure_override=(
                                        self._provider_failure_override
                                    ),
                                    url=url,
                                    headers=active_headers,
                                    body=attempt_body,
                                )
                        except ApplicationUnavailableError:
                            raise
                        except Exception as error:
                            recovered = self._recovery_ladder.next_body(
                                error, attempt_body, used_retry_kinds
                            )
                            if recovered.body is None:
                                raise
                            if recovered.stripped_reasoning_field is not None:
                                stripped_reasoning = recovered.stripped_reasoning_field
                            attempt_body = recovered.body
                            continue
                        break
                    if stripped_reasoning is not None:
                        self._remember_reasoning_rejection(
                            attempt_body, stripped_reasoning
                        )
                    try:
                        if response.status_code >= 400:
                            self._log_error(tag, req_tag, None, request_id)
                            if (
                                response.status_code == 401
                                and refreshed_after_unauthorized
                            ):
                                raise ApplicationUnavailableError(
                                    "ChatGPT OAuth authorization was rejected after "
                                    "one refresh. Reconnect in Admin."
                                )
                            if response.status_code == 401:
                                raise ApplicationUnavailableError(
                                    "ChatGPT OAuth access token was rejected. "
                                    "Sign in again in Admin."
                                )
                            raise ApplicationUnavailableError(
                                f"ChatGPT OAuth API error {response.status_code}"
                            )

                        yield ledger.message_start()
                        shape = start_response_shape()
                        async for event in iter_chatgpt_oauth_sse_events(
                            response.aiter_raw()
                        ):
                            note_responses_event_shape(shape, event)
                            for sse_event in converter.feed(event):
                                yield sse_event

                        for sse_event in converter.finish():
                            yield sse_event
                        record_response_shape(shape)
                    finally:
                        await response.aclose()

                except ApplicationUnavailableError:
                    raise
                except Exception as error:
                    self._log_error(tag, req_tag, error, request_id)
                    failure = classify_provider_failure(
                        error,
                        provider_name=tag,
                        read_timeout_s=self._config.http_read_timeout,
                        request_id=request_id,
                        mark_rate_limited=self._rate_limiter.extend_reactive_block,
                        provider_failure_override=self._provider_failure_override,
                        mark_rate_limited_enabled=(
                            not self._config.routes_around_model
                        ),
                    )
                    trace_event(
                        stage="provider",
                        event="provider.response.error",
                        source="provider",
                        provider=tag,
                        request_id=request_id,
                        exc_type=type(error).__name__,
                        failure_kind=failure.kind.value,
                        status_code=failure.status_code,
                        provider_retryable=failure.retryable,
                    )
                    raise failure from error

        return _stream()

    def _provider_failure_override(self, error: Exception) -> ExecutionFailure | None:
        return None

    def _log_error(
        self,
        tag: str,
        req_tag: str,
        error: Exception | None,
        request_id: str | None,
    ) -> None:
        if error is None:
            logger.error("{}_ERROR:{} transport error", tag, req_tag)
            return
        if self._config.log_api_error_tracebacks:
            logger.error(
                "{}_ERROR:{} exc_type={}\n{}",
                tag,
                req_tag,
                type(error).__name__,
                redacted_exception_traceback(error),
            )
        else:
            logger.error(
                "{}_ERROR:{} exc_type={} cause_types={}",
                tag,
                req_tag,
                type(error).__name__,
                ",".join(exception_cause_types(error)),
            )
