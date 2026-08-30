"""Per-request record of which credential served a request.

The rotation engine picks a credential deep inside the provider call stack,
while the request log is finalized at the API boundary. Rather than widening
every provider signature with an out-parameter, the API layer installs a
mutable slot for the duration of one request and the rotating provider writes
its choice into it.

The slot is mutable on purpose. A ``ContextVar`` holding an immutable value
would be invisible to the installer whenever the provider runs in a child task,
because that task mutates its own copy of the context. Mutating one shared
object is visible through any number of context copies.

Only the credential *index* and a masked label are recorded here; the raw key
never enters this module.
"""

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(slots=True)
class CredentialAttribution:
    """Mutable slot holding the credential chosen for one request."""

    index: int | None = None
    label: str | None = None


# The pool had nothing to offer: every credential was benched before one was
# chosen, so no key served this attempt. Recorded as a value rather than left
# NULL, because NULL already means "not measured" and a benched-out pool is a
# measurement, not a gap.
NO_CREDENTIAL_INDEX = -1
NO_CREDENTIAL_LABEL = "(no key available)"

_CURRENT: ContextVar[CredentialAttribution | None] = ContextVar(
    "fcc_credential_attribution", default=None
)


def install_attribution() -> CredentialAttribution:
    """Start recording credential choices for the current request."""
    slot = CredentialAttribution()
    _CURRENT.set(slot)
    return slot


def record_credential(index: int, label: str | None) -> None:
    """Record the credential serving the current request, if one is tracked.

    A no-op outside a tracked request, so providers used directly (tests, the
    token-count path) need no special handling. Later calls overwrite earlier
    ones so that after a failover the last credential actually tried is the one
    attributed to the request.
    """
    slot = _CURRENT.get()
    if slot is not None:
        slot.index = index
        slot.label = label


def current_credential() -> tuple[int | None, str | None]:
    """The credential in flight right now, as ``(index, label)``.

    Read-only: the retry ladder records which key each individual upstream try
    used, and the rotating loop is the only writer. ``(None, None)`` outside a
    tracked request, which reads downstream as "not measured".
    """
    slot = _CURRENT.get()
    if slot is None:
        return None, None
    return slot.index, slot.label
