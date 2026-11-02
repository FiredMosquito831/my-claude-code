"""Read-only admin API for the providers whose usage rides on their responses.

**One GET, and no write of any kind.** ``/admin/api/usage`` reports what this
process has already observed: the allow-listed usage headers of every upstream
response an opted-in provider has made since the server started. It opens no
connection, reads no credential, starts no task and has no timer. Opening the
Usage page therefore still adds no traffic -- the page draws from this route
and the two payloads the Providers page already fetches, and this route reads
memory.

**Loopback only, like every other admin route** (:func:`require_loopback_admin`,
which every admin route in this package calls). A usage reading is account-level
data; it is not served off this machine.

**What it will not do, deliberately.** No ``POST``, no "read now", no endpoint
call, no cache file. Those are USAGE PR 3 (the on-demand reads) and PR 4 (the
optional disk cache), and putting a stub here now would be a route that looks
like a feature and answers a lie.

**The four states, on the wire.** Each provider row carries the state the page
must draw, decided here rather than in JavaScript, because the decision needs
facts the page does not have (which providers are configured, and whether a
mechanism exists at all):

``measured`` / ``stale``
    A snapshot exists. ``observed_at`` is the instant the provider's bytes
    arrived; :data:`STALE_AFTER_SECONDS` is the bound that separates the two,
    and it is the same 30 minutes the page uses, declared once here so the two
    cannot disagree.
``not_measured``
    A mechanism exists and nothing has arrived through it yet. This covers both
    "nobody has made a request since the server started" and "this provider's
    read is not built yet", which are different facts for the reader but the
    same state for the chip -- and both are ``Not read yet``, never a zero.
``unsupported``
    No mechanism at all. The page prints one sentence and never retries.

``values`` are the provider's own strings, verbatim, and a field it did not send
is **absent from the mapping** rather than ``""``: "the host did not send this"
and "the host sent an empty string" are different, and the page renders the
first as *not measured by this provider*.
"""

import time
from typing import Any

from fastapi import APIRouter, Request

from my_claude_code.config.usage_sources import (
    PROVIDER_SOURCES,
    SOURCE_UNSUPPORTED,
)
from my_claude_code.core.usage_capture import OBSERVER

from .admin_routes import require_loopback_admin

router = APIRouter()

#: How old a reading may be before it stops being a picture of now. The same
#: 30 minutes the Usage page uses (``admin.js``'s ``USAGE_STALE_AFTER_SECONDS``)
#: and the same interval §5.5 sets as the on-demand read interval: one number,
#: so the chip and the sentence beside it cannot disagree.
STALE_AFTER_SECONDS = 30 * 60


def _state_of(observed_at: float | None, *, supported: bool, now: float) -> str:
    """One reading's state, decided in one place.

    ``supported`` is whether ANY mechanism exists for this provider. It is
    checked before the snapshot is, on purpose: a provider with no mechanism
    can never be ``measured``, and one whose only mechanism is an endpoint read
    that is not built yet stays ``not_measured`` rather than claiming the
    provider publishes nothing.
    """

    if not supported:
        return SOURCE_UNSUPPORTED
    if observed_at is None:
        return "not_measured"
    return "stale" if (now - observed_at) > STALE_AFTER_SECONDS else "measured"


def _provider_rows(now: float) -> list[dict[str, Any]]:
    """One row per provider MCC has an opinion about, observed or not.

    The list is the **declared table**, not the observation store: a provider
    that has never sent a header still appears, with ``not_measured``, so the
    page can say "not read yet" instead of leaving a configured provider with
    nothing at all. Providers MCC has no row for -- every custom provider, and
    the catalogue's endpoint-less majority -- are reported by the page's own
    configured-provider list, each with the one sentence, and are not enumerated
    here; enumerating 59 catalogue ids per page load to say "nothing" would be
    a payload nobody reads.
    """

    rows: list[dict[str, Any]] = []
    for declared in PROVIDER_SOURCES:
        snapshot = OBSERVER.latest_for(declared.provider_id)
        observed_at = None if snapshot is None else snapshot.observed_at
        state = _state_of(observed_at, supported=declared.supported, now=now)
        rows.append(
            {
                "provider_id": declared.provider_id,
                "display_name": declared.display_name,
                "mechanisms": list(declared.mechanisms),
                "source": declared.source,
                "detail": declared.detail,
                "state": state,
                "observed_at": observed_at,
                "status_code": None if snapshot is None else snapshot.status_code,
                # Verbatim, allow-listed, and only what the host actually sent.
                "values": {} if snapshot is None else dict(snapshot.values),
            }
        )
    return rows


@router.get("/admin/api/usage")
async def usage_readings(request: Request) -> dict[str, Any]:
    """Report every usage reading this process has already observed.

    Read-only in the strict sense: it touches an in-memory dictionary and
    nothing else. Nothing here can fail a page -- an observer store is always
    a dict, and a provider with no row is a provider the page describes with
    its own declared list.
    """

    require_loopback_admin(request)
    now = time.time()
    return {
        "observed_at": now,
        "stale_after_seconds": STALE_AFTER_SECONDS,
        "providers": _provider_rows(now),
    }
