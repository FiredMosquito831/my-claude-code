"""Per-request record of which egress address a request went out through.

The sibling of :mod:`my_claude_code.core.credential_attribution`, and it exists
for the same reason: the proxy pool picks an address deep inside the provider
call stack, and the request log is finalized at the API boundary. Rather than
widening every provider signature with an out-parameter, the API layer installs
one mutable slot per request and the proxy pool writes its choice into it.

The slot is mutable for the reason the credential one is: a ``ContextVar``
holding an immutable value is invisible to the installer whenever the provider
runs in a child task, because that task mutates its own copy of the context.

**Only a label is ever recorded here.** ``host:port`` with any ``user:pass``
removed, produced by ``config.credentials.mask_proxy_label`` before it reaches
this module -- a proxy password in the request log would be a worse leak than
the thing a proxy chain is trying to avoid. The literal :data:`DIRECT_PROXY_LABEL`
means "this machine's own address, deliberately"; ``None`` means "not measured",
and the two must never be conflated.

Every dial, not only the last
-----------------------------

``label`` is last-write-wins, so on its own it can only ever say where a
request *ended up*. The 09-16 park was diagnosed from exactly that: nine
requests carried their first rung's label and seven their second, which proved
the nine never reached the second dial -- and nothing recorded more than that.
The slot therefore also carries an optional ``on_dial`` observer, installed by
the API layer beside the request's upstream ladder, and :func:`record_proxy`
tells it about every dial as it happens. This module does not know what the
observer is: ``core/upstream_ladder`` already reads this module, so the
dependency points one way and the API layer, which owns both, joins them.
"""

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass

#: A chain rung that uses no proxy at all. A value, never ``None``: an operator
#: who put Direct in their chain measured something, and a NULL would read as a
#: request nobody watched.
DIRECT_PROXY_LABEL = "direct"


@dataclass(slots=True)
class ProxyAttribution:
    """Mutable slot holding the egress address chosen for one request."""

    label: str | None = None
    #: Told about every dial, after ``label`` is written. ``None`` -- the
    #: default, and every request whose log is off -- records nothing more
    #: than the label always did.
    on_dial: Callable[[str | None], None] | None = None


_CURRENT: ContextVar[ProxyAttribution | None] = ContextVar(
    "fcc_proxy_attribution", default=None
)


def install_proxy_attribution(
    on_dial: Callable[[str | None], None] | None = None,
) -> ProxyAttribution:
    """Start recording egress choices for the current request.

    ``on_dial`` is called with the label of every dial the proxy pool makes
    for this request, in order -- the per-dial record the single label cannot
    keep.
    """

    slot = ProxyAttribution(on_dial=on_dial)
    _CURRENT.set(slot)
    return slot


def record_proxy(label: str | None) -> None:
    """Record the address serving the current request, if one is tracked.

    A no-op outside a tracked request, so providers exercised directly -- unit
    tests, token counting, model discovery -- need no special handling. Later
    calls overwrite earlier ones, so after a switch the address actually last
    tried is the one attributed.

    Called by the pool immediately *before* it dials, so the observer hears
    of a dial even when the dial never completes -- which is the one case the
    last-write-wins label could not describe.
    """

    slot = _CURRENT.get()
    if slot is not None:
        slot.label = label
        if slot.on_dial is not None:
            slot.on_dial(label)


def current_proxy() -> str | None:
    """The egress label in flight right now, or ``None`` if untracked."""

    slot = _CURRENT.get()
    return None if slot is None else slot.label
