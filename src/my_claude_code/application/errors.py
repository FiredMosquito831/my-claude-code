"""Deterministic application and readiness errors."""

from collections.abc import Iterable

from my_claude_code.core.failures import FailureKind


class ApplicationError(Exception):
    """Base for request/readiness failures, not finalized upstream failures."""

    kind: FailureKind
    status_code: int

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidRequestError(ApplicationError):
    """The accepted request cannot be executed deterministically."""

    kind = FailureKind.INVALID_REQUEST
    status_code = 400


class UnknownProviderError(InvalidRequestError):
    """The configured provider identifier is not registered."""

    @classmethod
    def for_provider(
        cls, provider_id: str, supported_provider_ids: Iterable[str]
    ) -> UnknownProviderError:
        supported = "', '".join(supported_provider_ids)
        return cls(f"Unknown provider_type: '{provider_id}'. Supported: '{supported}'")


class ApplicationUnavailableError(ApplicationError):
    """The application cannot currently provide a request runtime."""

    kind = FailureKind.UNAVAILABLE
    status_code = 503


class ModelRateLimited(Exception):
    """This (key, model) pair is rate-limited; another model may still serve.

    Carries no advice about the key. The pool has already benched the pair;
    this only tells the executor that trying a *different model on the same
    provider* is the move with evidence behind it (NVIDIA NIM 429s one model
    while answering on another with the same key in the same second), and
    that the pool deliberately did not rotate.

    ``failure`` is the provider's own classified ``rate_limit``
    :class:`~my_claude_code.core.failures.ExecutionFailure`. The executor
    unwraps it the moment it has decided where to go next, so nothing
    downstream -- the request log's ``error_kind``, ``FALLBACK_SKIP_KINDS``,
    the ejection registry, the wire adapters -- ever sees this class. The
    ``kind`` and ``status_code`` attributes exist for the one case where it
    escapes anyway: :func:`~my_claude_code.core.failures.failure_kind` reads
    them and still answers ``rate_limit``.
    """

    kind = FailureKind.RATE_LIMIT
    status_code = 429

    def __init__(
        self,
        *,
        provider_id: str,
        model: str,
        key_index: int,
        retry_after: float | None,
        failure: Exception,
    ) -> None:
        super().__init__(f"{provider_id}/{model} is rate-limited on key {key_index}")
        self.provider_id = provider_id
        self.model = model
        self.key_index = key_index
        self.retry_after = retry_after
        self.failure = failure
