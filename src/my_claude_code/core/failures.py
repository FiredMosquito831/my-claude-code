"""Protocol-neutral execution failure semantics."""

from dataclasses import FrozenInstanceError, dataclass
from enum import StrEnum


class FailureKind(StrEnum):
    """Stable failure categories shared across execution and wire adapters."""

    INVALID_REQUEST = "invalid_request"
    CONTEXT_LENGTH = "context_length"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    QUOTA = "quota"
    RATE_LIMIT = "rate_limit"
    OVERLOADED = "overloaded"
    TIMEOUT = "timeout"
    UPSTREAM = "upstream"
    UNAVAILABLE = "unavailable"


@dataclass(slots=True, eq=False)
class ExecutionFailure(Exception):
    """A finalized provider-execution failure independent of any wire protocol."""

    kind: FailureKind
    status_code: int
    message: str
    retryable: bool
    #: For a ``RATE_LIMIT`` failure, how long the upstream said to wait --
    #: parsed from its own ``Retry-After`` / ``x-ratelimit-reset-*`` headers,
    #: or the operator's configured default when it published none. Carried on
    #: the failure so every bench downstream (the credential pool, the route's
    #: ejection registry) uses the provider's number instead of inventing one.
    #: ``None`` on every other kind, and on a rate limit no classifier touched.
    #:
    #: A ``QUOTA`` failure reuses the same field to carry the operator's
    #: ``RATE_LIMIT_COOLDOWN_SECONDS``, and *only* when the upstream named an
    #: explicit billing phrase. ``None`` there means the opposite of a missing
    #: header: it means the evidence was a bare ``402`` with no phrase behind
    #: it, so the pool must rotate without charging the credential at all.
    retry_after_seconds: float | None = None

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def __setattr__(self, name: str, value: object) -> None:
        # Exception machinery must be able to update __traceback__, __cause__,
        # and __context__ while semantic failure fields remain immutable.
        if name in self.__slots__ and hasattr(self, name):
            raise FrozenInstanceError(f"cannot assign to field {name!r}")
        super().__setattr__(name, value)


def find_execution_failure(exc: BaseException) -> ExecutionFailure | None:
    """Return the first canonical failure in an exception or nested group."""
    pending = [exc]
    while pending:
        current = pending.pop()
        if isinstance(current, ExecutionFailure):
            return current
        if isinstance(current, BaseExceptionGroup):
            pending.extend(reversed(current.exceptions))
    return None


def failure_kind(exc: BaseException) -> FailureKind | None:
    """Return the canonical kind of a failure, whatever raised it.

    Three shapes carry a kind and only one of them was ever read. An
    ``ExecutionFailure`` (or one nested in a group) is a provider's classified
    failure; an ``ApplicationError`` -- ``InvalidRequestError``,
    ``ApplicationUnavailableError`` -- carries a ``kind`` class attribute; and
    anything else has none.

    Reading only the first is why the request log's ``error_kind`` column mixes
    vocabularies: ``timeout`` and ``rate_limit`` sit alongside
    ``InvalidRequestError`` and ``ApplicationUnavailableError``, so grouping by
    it splits the same failure across two spellings.
    """
    failure = find_execution_failure(exc)
    if failure is not None:
        return failure.kind
    kind = getattr(exc, "kind", None)
    return kind if isinstance(kind, FailureKind) else None


def failure_kind_name(exc: BaseException) -> str:
    """Name a failure by its kind where it has one, else by its class."""
    kind = failure_kind(exc)
    return kind.value if kind is not None else type(exc).__name__


def parse_failure_kinds(value: str | None) -> frozenset[FailureKind]:
    """Parse a comma-separated list of kind names, ignoring blanks.

    Unknown names are dropped rather than raised on: settings validation
    already rejects them at load, and this is also reached by callers holding
    a value that predates a renamed kind.
    """
    known = {kind.value: kind for kind in FailureKind}
    return frozenset(
        known[name]
        for name in (part.strip().lower() for part in (value or "").split(","))
        if name in known
    )
