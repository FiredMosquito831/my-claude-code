"""The provider a chain becomes when it may not go direct and cannot go anywhere.

A provider whose proxy chain is switched on with **Direct fallback off** has
been told, by its operator, never to reach its host from this computer's own
address. When that chain has nothing left to route through -- every entry
paused, every address removed, no entry at all, or a chain file that could not
be read -- ``resolve_proxy_chain`` answers a
:class:`~my_claude_code.providers.base.MaskedRefusalPlan`, and the factory
builds this in place of the leaf it would otherwise have built with no proxy at
all (7.78.8).

It owns no client and dials nothing. Every call -- the preflight the executor
runs before any attempt, the stream itself, a model listing from the hourly
sweep or the card's Test button -- answers the same ``UNAVAILABLE`` 503 whose
message names the provider, the cause and the setting. The executor treats it
exactly as it treats any provider that cannot serve: the attempt is recorded,
and the request moves on to the next entry of its fallback chain. Nothing
about how that move happens is new.
"""

from collections.abc import AsyncIterator

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.reasoning import DEFAULT_REASONING_POLICY, ReasoningPolicy
from my_claude_code.providers.base import BaseProvider, ProviderConfig

#: The status a refused call answers: the provider is not available from here
#: under the operator's own rule, which is what a 503 says.
MASKED_REFUSAL_STATUS = 503


class MaskedRefusalFailure(ExecutionFailure):
    """The refusal, as the ``ExecutionFailure`` every caller already handles.

    A subclass only for :attr:`safe_message`, the marker the model-list log
    reads (``providers.runtime.validation.provider_query_failure_reason``)
    before it quotes a failure: this sentence is MCC's own and carries no
    upstream body, so the hourly sweep's log line can say *why* the listing
    was refused instead of only the class name.
    """

    safe_message = True


def masked_refusal_failure(message: str) -> ExecutionFailure:
    """The one failure every refused call raises.

    Not retryable: nothing about the next try would differ until the operator
    changes the chain, so a retry would only repeat the same answer.
    """

    return MaskedRefusalFailure(
        kind=FailureKind.UNAVAILABLE,
        status_code=MASKED_REFUSAL_STATUS,
        message=message,
        retryable=False,
    )


class _RefusedStream:
    """An async iterator whose first step raises the refusal."""

    def __init__(self, message: str) -> None:
        self._message = message

    def __aiter__(self) -> _RefusedStream:
        return self

    async def __anext__(self) -> str:
        raise masked_refusal_failure(self._message)

    async def aclose(self) -> None:
        return None


class MaskedRefusalProvider(BaseProvider):
    """Answers every call with "not sent: Direct fallback is off"."""

    def __init__(
        self, config: ProviderConfig, *, provider_id: str, message: str
    ) -> None:
        super().__init__(config)
        self._provider_id = provider_id
        self._message = message

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def refusal(self) -> str:
        """The sentence every call answers with."""

        return self._message

    def preflight_stream(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> None:
        raise masked_refusal_failure(self._message)

    def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        return _RefusedStream(self._message)

    async def list_model_ids(self) -> frozenset[str]:
        raise masked_refusal_failure(self._message)

    async def cleanup(self) -> None:
        return None


__all__ = [
    "MASKED_REFUSAL_STATUS",
    "MaskedRefusalFailure",
    "MaskedRefusalProvider",
    "masked_refusal_failure",
]
