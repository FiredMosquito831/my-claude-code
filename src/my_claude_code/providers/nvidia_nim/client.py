"""NVIDIA NIM provider implementation."""

from collections.abc import Mapping
from typing import Any

import openai
from loguru import logger

from my_claude_code.config.nim import NimSettings
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.failures import ExecutionFailure
from my_claude_code.core.reasoning import (
    DEFAULT_REASONING_POLICY,
    ReasoningDialect,
    ReasoningPolicy,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.failure_policy import (
    overloaded_provider_failure,
)
from my_claude_code.providers.openai_chat import (
    NO_REASONING,
    OpenAIChatProfile,
    OpenAIChatProvider,
    complaint_evidence_snippet,
    is_bad_request,
    sampling_parameter_evidence,
    upstream_complaint,
)
from my_claude_code.providers.rate_limit import ProviderRateLimiter

from .request_options import NIM_REQUEST_POLICY, build_nim_request_body
from .retry import (
    chat_template_evidence,
    clone_body_without_chat_template,
    clone_body_without_reasoning_budget,
    clone_body_without_reasoning_content,
    reasoning_budget_evidence,
    reasoning_content_evidence,
)
from .tool_schema import (
    body_without_nim_tool_argument_aliases,
    nim_tool_argument_aliases_from_body,
)

_DEGRADED_FUNCTION_STATE = "degraded function cannot be invoked"
_PROFILE = OpenAIChatProfile(
    NIM_REQUEST_POLICY,
    # 2026-08-29 audit: deliberate, not a dead wire. NIM's reasoning control is
    # the chat-template boolean pair written by ``build_nim_request_body`` in
    # ``.request_options`` (``chat_template_kwargs.thinking`` /
    # ``enable_thinking``, plus ``reasoning_budget``), which this provider uses
    # instead of the profile postprocessors. The operator's request log confirms
    # it reaches the model: 18,744 of 24,620 NIM requests (76.1%) came back with
    # thinking. An encoder here would be dead configuration that reads as live.
    # PR F gave every profile-driven host the OpenAI standard dialect; this
    # one is excluded because the profile encoder is not the wire, and its
    # real dialect is declared alongside.
    NO_REASONING,
)


NIM_REASONING_DIALECT = ReasoningDialect(
    toggle=True,
    budget=True,
    off=True,
    toggle_field="chat_template_kwargs.thinking",
    budget_field="chat_template_kwargs.reasoning_budget",
)
"""NIM builds its own body and never reaches the profile encoder.

``request_options`` writes ``chat_template_kwargs.thinking`` /
``enable_thinking`` and a numeric ``reasoning_budget`` beside them, and
strips every ``reasoning_effort``-shaped key on the way out -- so an
effort word has nowhere to go here and must not be claimed.
"""


class NvidiaNimProvider(OpenAIChatProvider):
    """NVIDIA NIM provider using official OpenAI client."""

    def reasoning_dialect(self, model_id: str) -> ReasoningDialect:
        """See :data:`NIM_REASONING_DIALECT`."""
        return NIM_REASONING_DIALECT

    def __init__(
        self,
        config: ProviderConfig,
        *,
        nim_settings: NimSettings,
        rate_limiter: ProviderRateLimiter,
    ):
        super().__init__(
            config,
            profile=_PROFILE,
            rate_limiter=rate_limiter,
            provider_id="nvidia_nim",
        )
        self._nim_settings = nim_settings

    def _build_request_body(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> dict:
        """Internal helper for tests and shared building."""
        return build_nim_request_body(
            request,
            self._nim_settings,
            reasoning=reasoning,
            provider_id=self._provider_id,
        )

    def _prepare_create_body(self, body: dict[str, Any]) -> dict[str, Any]:
        """Strip private request metadata before calling NVIDIA NIM."""
        return body_without_nim_tool_argument_aliases(body)

    def _tool_argument_aliases(self, body: dict[str, Any]) -> dict[str, dict[str, str]]:
        """Return NIM tool argument aliases captured while building this request."""
        return nim_tool_argument_aliases_from_body(body)

    def _get_retry_request_body(self, error: Exception, body: dict) -> dict | None:
        """Retry once with a downgraded body when NIM names the field it rejected.

        Each rung fires only on evidence that the upstream complaint is *about*
        the field that rung removes. Ordering, and why:

        1. ``reasoning_budget`` -- the narrowest removal, and the only one that
           keeps the thinking instruction itself. Also the only rung that
           accepts a 500, because NIM has been seen failing on the budget
           control server-side rather than validating it.
        2. ``chat_template`` / ``chat_template_kwargs`` -- load-bearing recovery
           for models that reject the field outright, so a complaint naming it
           wins even if a sampling parameter is named alongside: naming the
           field is direct evidence, a sampling name is only a negative signal.
        3. sampling-parameter guard -- a 400 that names ``top_p`` and friends
           and does *not* name the chat template is a complaint about sampling.
           Stripping the reasoning instruction cannot fix it and silently
           downgrades the answer, so this returns ``None`` and lets the failure
           surface. (Per-model sampling correction lives in request options.)
        4. ``reasoning_content`` -- replayed assistant reasoning, named.
        5. anything unrecognised -- ``None``. Retrying an unchanged body is
           pointless and retrying a degraded one trades a visible failure for
           an invisible loss of reasoning, so an unreadable 400 is failed
           rather than guessed at.
        """
        status_code = getattr(error, "status_code", None)
        bad_request_like = is_bad_request(error)

        complaint = upstream_complaint(error)
        evidence = complaint_evidence_snippet(complaint)

        budget_match = reasoning_budget_evidence(complaint)
        if budget_match is not None and (bad_request_like or status_code == 500):
            retry_body = clone_body_without_reasoning_budget(body)
            if retry_body is None:
                return None
            logger.warning(
                "NIM_STREAM: retrying without reasoning budget -- upstream named "
                "{!r} (status {}): {}",
                budget_match,
                status_code,
                evidence,
            )
            return retry_body

        if not bad_request_like:
            return None

        template_match = chat_template_evidence(complaint)
        if template_match is not None:
            retry_body = clone_body_without_chat_template(body)
            if retry_body is None:
                return None
            logger.warning(
                "NIM_STREAM: retrying without chat_template -- upstream named "
                "{!r} in a 400: {}",
                template_match,
                evidence,
            )
            return retry_body

        sampling_match = sampling_parameter_evidence(complaint)
        if sampling_match is not None:
            logger.warning(
                "NIM_STREAM: keeping chat_template_kwargs -- the 400 names "
                "sampling parameter {!r}, not the chat template, so removing "
                "the reasoning instruction would degrade the request without "
                "addressing the rejection: {}",
                sampling_match,
                evidence,
            )
            return None

        content_match = reasoning_content_evidence(complaint)
        if content_match is not None:
            retry_body = clone_body_without_reasoning_content(body)
            if retry_body is None:
                return None
            logger.warning(
                "NIM_STREAM: retrying without reasoning_content -- upstream "
                "named {!r} in a 400: {}",
                content_match,
                evidence,
            )
            return retry_body

        logger.warning(
            "NIM_STREAM: no retry -- the 400 names no request field this "
            "provider can downgrade, and reasoning fields are preserved rather "
            "than stripped on an unrecognised rejection: {}",
            evidence,
        )
        return None

    def _provider_failure_override(self, error: Exception) -> ExecutionFailure | None:
        """Map NVIDIA Cloud Function deployment failure onto canonical overload."""
        if not isinstance(error, openai.BadRequestError):
            return None
        if getattr(error, "status_code", None) != 400:
            return None
        body = getattr(error, "body", None)
        if not isinstance(body, Mapping):
            return None
        detail = body.get("detail")
        if not isinstance(detail, str):
            return None
        function_ref, separator, state = detail.lower().partition(": ")
        function_id = function_ref.removeprefix("function id ").strip(" '\"")
        if (
            not separator
            or not function_ref.startswith("function id ")
            or not function_id
            or state.strip() != _DEGRADED_FUNCTION_STATE
        ):
            return None
        return overloaded_provider_failure()
