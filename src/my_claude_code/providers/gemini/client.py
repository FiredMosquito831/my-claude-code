"""Google AI Studio Gemini provider (OpenAI-compatible chat completions)."""

from copy import deepcopy
from typing import Any

from my_claude_code.config.credential_names import credential_fingerprint
from my_claude_code.core.anthropic import ReasoningReplayMode
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.openai_responses import connection_id
from my_claude_code.core.reasoning import (
    DEFAULT_REASONING_POLICY,
    ReasoningEffort,
    ReasoningPolicy,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.google_openai.signature_cache import (
    GOOGLE_THOUGHT_SIGNATURES,
)
from my_claude_code.providers.openai_chat import (
    NamedEffortReasoning,
    OpenAIChatProfile,
    OpenAIChatProvider,
    OpenAIChatRequestPolicy,
    build_openai_chat_request_body,
)
from my_claude_code.providers.rate_limit import ProviderRateLimiter

from .quirks import apply_gemini_request_quirks

_MAX_TOOL_CALL_EXTRA_CONTENT_CACHE = 4096

_REQUEST_POLICY = OpenAIChatRequestPolicy(
    provider_name="GEMINI",
    reasoning_replay=ReasoningReplayMode.REASONING_CONTENT,
)
_PROFILE = OpenAIChatProfile(
    _REQUEST_POLICY,
    NamedEffortReasoning(
        (
            (ReasoningEffort.MINIMAL, "minimal"),
            (ReasoningEffort.LOW, "low"),
            (ReasoningEffort.MEDIUM, "medium"),
            (ReasoningEffort.HIGH, "high"),
            (ReasoningEffort.XHIGH, "high"),
            (ReasoningEffort.MAX, "high"),
        ),
        disabled_value="none",
    ),
)


class GeminiProvider(OpenAIChatProvider):
    """Gemini API using ``https://generativelanguage.googleapis.com/v1beta/openai/``."""

    def __init__(self, config: ProviderConfig, *, rate_limiter: ProviderRateLimiter):
        super().__init__(
            config,
            profile=_PROFILE,
            rate_limiter=rate_limiter,
            provider_id="gemini",
        )
        # The signatures this credential captured, by tool-call id. Restored
        # from the durable store on construction (8.19.0), so a tool loop that
        # spans a restart still replays them instead of falling back to the
        # skip-validator sentinel.
        self._tool_call_extra_content_by_id: dict[str, dict[str, Any]] = (
            GOOGLE_THOUGHT_SIGNATURES.rows_for(self._provider_id, self._credential_id)
        )

    @property
    def _credential_id(self) -> str:
        """Which credential this instance holds -- hashed, never the secret.

        ``connection_id`` of the credential's fingerprint, the rule
        ``core/openai_responses/carrier.py`` states for the reasoning carrier:
        hash the identity, never the token. The access token of a Google
        account refreshes; hashing it would refuse every signature captured
        before a refresh, silently, every hour. The fingerprint is the same
        value the request log and the exit memory key on, so a signature and
        the request that used it name the credential the same way.

        ``""`` when no credential is configured, which is a stable constant and
        keeps a keyless instance's cache separate from every keyed one.
        """

        key = self._config.api_key
        return connection_id(credential_fingerprint(key) if key else "")

    def _record_tool_call_extra_content(
        self, tool_call_id: str, extra_content: dict[str, Any]
    ) -> None:
        if (
            tool_call_id not in self._tool_call_extra_content_by_id
            and len(self._tool_call_extra_content_by_id)
            >= _MAX_TOOL_CALL_EXTRA_CONTENT_CACHE
        ):
            self._tool_call_extra_content_by_id.pop(
                next(iter(self._tool_call_extra_content_by_id))
            )
        self._tool_call_extra_content_by_id[tool_call_id] = deepcopy(extra_content)
        # The durable half. It evicts on its own ceiling and hands back the row
        # it dropped, so the two maps cannot disagree about what is cached.
        evicted = GOOGLE_THOUGHT_SIGNATURES.record(
            self._provider_id,
            self._credential_id,
            tool_call_id,
            extra_content,
        )
        if evicted is not None and evicted[0:2] == (
            self._provider_id,
            self._credential_id,
        ):
            self._tool_call_extra_content_by_id.pop(evicted[2], None)

    def _build_request_body(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
        wire_notes: dict[str, Any] | None = None,
    ) -> dict:
        return build_openai_chat_request_body(
            request,
            reasoning=reasoning,
            policy=_REQUEST_POLICY,
            postprocessors=(
                lambda body, request_data, policy: apply_gemini_request_quirks(
                    body,
                    request_data,
                    policy,
                    tool_call_extra_content_by_id=self._tool_call_extra_content_by_id,
                    wire_notes=wire_notes,
                ),
                _PROFILE.apply_reasoning,
            ),
            provider_id=self._provider_id,
        )


__all__ = ["GeminiProvider"]
