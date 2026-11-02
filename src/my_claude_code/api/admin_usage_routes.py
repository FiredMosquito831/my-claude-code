"""Read-only admin API for the providers whose usage rides on their responses.

**One GET and one POST, and no write of any kind.** ``GET /admin/api/usage``
reports what this process has already observed; ``POST /admin/api/usage/read``
asks ONE named provider for its own figure, once.

``GET /admin/api/usage``
    The allow-listed usage headers of every upstream response an opted-in
    provider has made since the server started, plus the last answer each
    declared read left behind. It opens no connection, reads no credential,
    starts no task and has no timer. Opening the Usage page therefore adds no
    upstream traffic: the page draws from this route and the two payloads the
    Providers page already fetches, and this route reads memory.

``POST /admin/api/usage/read``
    The on-demand read. It is the only path in MCC that asks a provider for a
    usage figure, and it has four guards, each of which exists because it
    would otherwise be missing:

    * **loopback only** (:func:`require_loopback_admin`, like every admin
      route) -- a usage reading is account-level data and is not served off
      this machine;
    * **a local ``Origin``** -- a browser attaches one to every non-GET
      request, so the dashboard satisfies it for free while a page on another
      origin cannot forge one (:func:`require_local_origin`). There is no
      destructive-confirm token because the route destroys nothing; the
      origin rule is what makes it unforgeable from a page;
    * **one named provider**, and only one whose row declares a read this
      release builds. An id the table does not carry is refused, never
      guessed at;
    * **one read per provider per interval**, floored at 30 minutes, with the
      provider's own ``Retry-After`` honoured as a deadline -- claimed
      atomically in :class:`my_claude_code.core.usage.reading.UsageReadStore`,
      so two page loads inside the interval make one request between them.

**The read path cannot charge a key or bench a provider**, and that is a
property of the modules it calls rather than of this file:
:mod:`my_claude_code.application.usage_reads` builds its own client and never
imports the rotation engine, constructs no ``FailureKind``, calls no
``RateLimitCooldown`` and touches no route health. A 401 is state 4 with the
reason; a 429 is this provider's usage deadline and nothing else.

**The four states, on the wire**, decided here rather than in JavaScript,
because the decision needs facts the page does not have:

``measured`` / ``stale``
    A reading exists. ``fetched_at`` is the instant the provider's bytes
    arrived; :data:`STALE_AFTER_SECONDS` is the bound that separates the two,
    and it is the same 30 minutes the page uses and the same interval the read
    gate enforces -- one number, so the chip and the sentence beside it cannot
    disagree.
``unavailable``
    The read succeeded and the provider's own answer was "there is nothing
    available here" -- an expired plan, an account not enabled. Not an error
    (the call worked) and not ``unsupported`` (the provider does publish).
``not_measured``
    A mechanism exists and nothing has come through it yet.
``unsupported``
    No mechanism at all. The page prints one sentence and never retries.
``failed``
    The read did not happen, for the reason carried in ``error``.

``values`` are the provider's own strings, verbatim, and a field it did not send
is **absent from the mapping** rather than ``""``: "the host did not send this"
and "the host sent an empty string" are different, and the page renders the
first as *not measured by this provider*.
"""

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from my_claude_code.config.constants import (
    USAGE_AUTO_REFRESH_MAX_SECONDS,
    USAGE_AUTO_REFRESH_MIN_SECONDS,
)
from my_claude_code.config.usage_sources import (
    PROVIDER_SOURCES,
    SOURCE_UNSUPPORTED,
    source_for,
)
from my_claude_code.core.usage.reading import (
    READINGS,
    Reading,
    ReadRecord,
    backoff_seconds,
    freshest_observation,
    state_of,
)
from my_claude_code.core.usage_capture import OBSERVER
from my_claude_code.providers.runtime.usage_reads import perform_read

from .admin_routes import require_loopback_admin
from .dependencies import get_services
from .ports import ApiServices

router = APIRouter()

#: How old a reading may be before it stops being a picture of now. The same
#: 30 minutes the Usage page uses (``admin.js``'s ``USAGE_STALE_AFTER_SECONDS``)
#: and 8.5.0's on-demand read interval: one number, so the chip and the
#: sentence beside it cannot disagree. Since 8.6.0 this is the interval used
#: when ``USAGE_AUTO_REFRESH_SECONDS`` is 0; a configured auto-refresh replaces
#: it (see :func:`_interval_for`).
STALE_AFTER_SECONDS = 30 * 60

#: The on-demand floor: what one provider's read interval is when the operator
#: has turned the automatic refresh off. Below this nothing in the research
#: justified a read (§3.6) and the two OAuth routes are the ones third parties
#: report as "rate-limit aggressively".
MINIMUM_INTERVAL_SECONDS = 30 * 60

#: 8.6.0's bounds on a configured auto-refresh interval. They live in
#: ``config/constants.py`` beside the default so Settings, the admin manifest
#: and this route quote one source; imported here rather than restated.
#: ``USAGE_AUTO_REFRESH_MIN_SECONDS`` is the floor a configured value is
#: clamped to, and ``BACKOFF_CAP_SECONDS`` is the longest a repeatedly failing
#: provider's interval is stretched to -- an hour, the ceiling on the setting
#: itself, so one broken host can never be asked more rarely than the rarest
#: legitimate configuration.
BACKOFF_CAP_SECONDS = USAGE_AUTO_REFRESH_MAX_SECONDS


class UsageReadRequest(BaseModel):
    """The one field the read takes. No credential, no URL, no options."""

    provider_id: str


def require_local_origin(request: Request) -> None:
    """Refuse a non-GET that no browser on this machine could have made.

    The apply route's rule, reused through the same loopback test: a browser
    attaches ``Origin`` to every non-GET request, so the dashboard sends it, and
    a page served anywhere else cannot forge a loopback one. A request with no
    ``Origin`` at all is refused here (unlike a read route): a plain ``curl``
    has no business spending a provider's read budget, and the page always has
    an origin.
    """

    origin = request.headers.get("origin")
    if origin is None or not _origin_is_local(origin):
        raise HTTPException(
            status_code=403,
            detail="This action requires a local Origin header",
        )


def _origin_is_local(origin: str) -> bool:
    from urllib.parse import urlsplit

    from .admin_routes import _is_loopback_host

    try:
        return _is_loopback_host(urlsplit(origin).hostname)
    except ValueError:
        return False


def _read_state(
    *,
    declared_supported: bool,
    readable: bool,
    snapshot_at: float | None,
    reading: Reading | None,
    record: ReadRecord | None,
    now: float,
) -> str:
    """One provider's state, decided in one place for the chip and the words.

    **Two sources, one answer.** A provider can report on its own traffic (a
    header, stamped by the observer) and through a declared read (a reading,
    stamped by the adapter), and the chip describes the CARD, not one of its
    halves. So the freshest observation wins and the older one does not get a
    vote -- a provider whose endpoint said "nothing available" ten seconds ago
    is unavailable even if a header carried a figure an hour ago.

    ``readable`` is the one fact the shared arithmetic in
    :func:`~my_claude_code.core.usage.reading.state_of` does not have: whether
    this release can perform the read at all. A provider whose only mechanism
    is an unbuilt endpoint stays ``not_measured`` (a point of capture exists,
    nothing has arrived), never ``unsupported`` -- IV.16 at the wire.
    """

    if not declared_supported:
        return SOURCE_UNSUPPORTED
    if not readable and reading is None and record is None and snapshot_at is None:
        return "not_measured"
    observed_at, available = freshest_observation(
        (snapshot_at, True),
        (
            None if reading is None else reading.fetched_at,
            reading is None or reading.available,
        ),
    )
    return state_of(
        supported=declared_supported,
        observed_at=observed_at,
        failure=record if record is not None and record.error else None,
        now=now,
        stale_after_seconds=STALE_AFTER_SECONDS,
        available=available,
    )


def _provider_rows(now: float, interval: float) -> list[dict[str, Any]]:
    """One row per provider MCC has an opinion about, observed or not.

    The list is the **declared table**, not the observation store: a provider
    that has never sent a header still appears, with ``not_measured``, so the
    page can say "not read yet" instead of leaving a configured provider with
    nothing at all. Providers MCC has no row for -- every custom provider, and
    the catalogue's endpoint-less majority -- are reported by the page's own
    configured-provider list, each with the one sentence, and are not
    enumerated here; enumerating 59 catalogue ids per page load to say
    "nothing" would be a payload nobody reads.
    """

    rows: list[dict[str, Any]] = []
    for declared in PROVIDER_SOURCES:
        snapshot = OBSERVER.latest_for(declared.provider_id)
        observed_at = None if snapshot is None else snapshot.observed_at
        reading = READINGS.reading_for(declared.provider_id)
        record = READINGS.record_for(declared.provider_id)
        state = _read_state(
            declared_supported=declared.supported,
            readable=declared.readable,
            snapshot_at=observed_at,
            reading=reading,
            record=record,
            now=now,
        )
        row: dict[str, Any] = {
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
            # The declared read, and what it last said. ``readable`` is what
            # the page uses to decide whether to draw a "Read now" button at
            # all; everything else is facts about the last attempt.
            "readable": declared.readable,
            "endpoint": _endpoint_payload(declared),
            "fetched_at": None if reading is None else reading.fetched_at,
            "reading": None if reading is None else reading.as_payload(),
            "attempted_at": None if record is None else (record.attempted_at or None),
            "read_error": "" if record is None else record.error,
            "not_sent": bool(record is not None and record.not_sent),
            "retry_at": None if record is None else record.retry_at,
            "retry_in_seconds": _retry_in(record, now),
            # The interval this process is on, and the wait this provider's own
            # failures have stretched it to. Both are numbers the page can act
            # on without recomputing anything: `next_read_in_seconds` is the
            # answer, and `consecutive_failures` is why it is longer than the
            # interval.
            "interval_seconds": int(interval),
            "consecutive_failures": 0
            if record is None
            else record.consecutive_failures,
            "next_read_in_seconds": _next_read_at(record, now, interval),
        }
        rows.append(row)
    return rows


def _endpoint_payload(declared: Any) -> dict[str, Any] | None:
    """The declared read, as the page needs to describe it. Never a credential."""

    endpoint = declared.endpoint
    if endpoint is None:
        return None
    return {
        "host": endpoint.host,
        "path": endpoint.path,
        "provenance": endpoint.provenance,
        "reads": endpoint.reads,
        "credential": endpoint.credential,
        "credential_env": endpoint.credential_env or endpoint.oauth_provider,
    }


def _retry_in(record: ReadRecord | None, now: float) -> int | None:
    """Seconds until the provider's own deadline passes, or ``None``."""

    if record is None or record.retry_at is None:
        return None
    remaining = record.retry_at - now
    return int(remaining) if remaining > 0 else None


def _next_read_at(record: ReadRecord | None, now: float, interval: float) -> int | None:
    """Seconds until this provider may be read again. ``None`` is "now".

    Three facts combine, and the latest wins:

    * the **interval** the page polls at (``USAGE_AUTO_REFRESH_SECONDS``, or
      the 30-minute on-demand floor when that setting is 0), lengthened by the
      per-provider **backoff** while this provider keeps failing;
    * the provider's own **``Retry-After``**, which is a fact about the host and
      is not negotiable by either of the other two.

    One arithmetic, on the server, so the button's countdown and the poller's
    next tick cannot disagree about when a read may happen.
    """

    if record is None or not record.attempted_at:
        return None
    wait = backoff_seconds(
        interval, record.consecutive_failures, cap=BACKOFF_CAP_SECONDS
    )
    earliest = record.attempted_at + wait
    if record.retry_at is not None:
        earliest = max(earliest, record.retry_at)
    remaining = earliest - now
    return int(remaining) if remaining > 0 else None


def _interval_for(settings: object) -> float:
    """This process's per-provider read interval, in seconds.

    **One setting, two behaviours.** Above zero, the operator's
    ``USAGE_AUTO_REFRESH_SECONDS`` is both how often the page polls and how
    often one provider may be re-read; below its floor it is clamped, because
    below ten seconds MCC would ask the two OAuth routes faster than any
    implementation this was researched against does. At zero the automatic
    refresh is off and the gate reverts to 8.5.0's 30-minute on-demand floor --
    which is what "on demand only" has to mean if a held-down button is not to
    become a poll.
    """

    configured = getattr(settings, "usage_auto_refresh_seconds", None)
    if not isinstance(configured, (int, float)):
        return MINIMUM_INTERVAL_SECONDS
    seconds = float(configured)
    if seconds <= 0:
        return MINIMUM_INTERVAL_SECONDS
    return max(
        USAGE_AUTO_REFRESH_MIN_SECONDS, min(seconds, USAGE_AUTO_REFRESH_MAX_SECONDS)
    )


@router.get("/admin/api/usage")
async def usage_readings(
    request: Request,
    services: ApiServices = Depends(get_services),
) -> dict[str, Any]:
    """Report every usage reading this process has already observed.

    Read-only in the strict sense: it touches two in-memory dictionaries and
    the settings snapshot, and nothing else. Nothing here can fail a page -- an
    observer store is always a dict, and a provider with no row is a provider
    the page describes with its own declared list.

    The auto-refresh interval is reported **here**, from the server's own
    settings, rather than being typed into the page: one source, so the
    poller's cadence and the interval the read gate enforces cannot drift.
    """

    require_loopback_admin(request)
    now = time.time()
    interval = _interval_for(services.requests.current_settings())
    return {
        "observed_at": now,
        "stale_after_seconds": STALE_AFTER_SECONDS,
        "minimum_interval_seconds": MINIMUM_INTERVAL_SECONDS,
        "auto_refresh_seconds": int(interval)
        if interval != MINIMUM_INTERVAL_SECONDS
        else 0,
        "read_interval_seconds": int(interval),
        "providers": _provider_rows(now, interval),
    }


@router.post("/admin/api/usage/read")
async def read_one_provider(
    payload: UsageReadRequest,
    request: Request,
) -> dict[str, Any]:
    """Ask ONE declared provider for its usage, once.

    Every refusal is a 200 with a state and a sentence rather than an error
    status, except the three that are about the *request* rather than the
    provider: a remote client (403), a foreign or missing Origin (403) and an
    unknown provider id (404). "Cooling down" and "we just read it" are
    normal answers a page draws, not faults.
    """

    require_loopback_admin(request)
    require_local_origin(request)

    provider_id = (payload.provider_id or "").strip()
    declared = source_for(provider_id)
    if declared.endpoint is None or not declared.readable:
        raise HTTPException(
            status_code=404,
            detail=(
                f"MCC has no usage read for {provider_id!r}. Only a provider "
                "whose row declares a read this release builds can be asked."
            ),
        )

    now = time.time()
    settings = get_services(request).requests.current_settings()
    interval = _interval_for(settings)
    refusal = READINGS.claim(provider_id, now=now, interval=interval)
    if refusal:
        return _refused_payload(provider_id, refusal, now, interval)

    outcome = await perform_read(declared, settings=settings, now=now)
    READINGS.finish(provider_id, outcome.reading, outcome.record)
    return _result_payload(declared, outcome.reading, outcome.record, interval)


def _refused_payload(
    provider_id: str, refusal: str, now: float, interval: float
) -> dict[str, Any]:
    """The answer when the gate, not the provider, said no.

    It carries the same shape as a real answer so the page has one render path:
    the reading it already holds, this provider's current state, and a sentence
    saying why nothing new was asked.
    """

    record = READINGS.record_for(provider_id)
    reading = READINGS.reading_for(provider_id)
    wait = _next_read_at(record, now, interval)
    sentences = {
        "in_flight": "A read for this provider is already running; this press "
        "waited rather than sending a second request.",
        "cooldown": "The provider asked MCC to wait before reading again.",
        "interval": (
            f"This provider was read less than {_human_wait(interval)} ago; "
            "nothing was sent."
        ),
    }
    row = _row_for(provider_id, now, interval)
    return {
        "provider_id": provider_id,
        "requested": False,
        "refusal": refusal,
        "message": sentences.get(refusal, "Nothing was sent."),
        "attempted_at": None if record is None else (record.attempted_at or None),
        "next_read_in_seconds": wait,
        "retry_in_seconds": _retry_in(record, now),
        "consecutive_failures": 0 if record is None else record.consecutive_failures,
        "state": row["state"],
        "fetched_at": None if reading is None else reading.fetched_at,
        "reading": None if reading is None else reading.as_payload(),
        "read_error": "" if record is None else record.error,
        "not_sent": False,
    }


def _human_wait(seconds: float) -> str:
    """One wait, in the words a card would print. Never a bare number."""

    total = max(0, round(seconds))
    if total < 60:
        return f"{total} second{'' if total == 1 else 's'}"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    hours = minutes // 60
    return f"{hours} hour{'' if hours == 1 else 's'}"


def _result_payload(
    declared: Any, reading: Reading | None, record: ReadRecord, interval: float
) -> dict[str, Any]:
    """The answer to a read that was actually performed.

    A failed read still returns the reading it did not replace: the page shows
    the old number with its age *and* the failure's own sentence, because
    "this host said nothing this time" must not erase what it said an hour ago.
    So ``reading`` here is the store's, not the attempt's -- a successful
    attempt has just written its own into the same place, which is what makes
    one function enough for both cases.
    """

    kept = READINGS.reading_for(declared.provider_id)
    # The row is built from the store's own record, which `finish` has already
    # given this attempt's failure streak. Reading `record` here instead would
    # report the count on the way in rather than the one that was stored.
    stored = READINGS.record_for(declared.provider_id) or record
    row = _row_for(declared.provider_id, time.time(), interval)
    return {
        "provider_id": declared.provider_id,
        "requested": True,
        "refusal": "",
        "message": "",
        "attempted_at": record.attempted_at,
        "next_read_in_seconds": _next_read_at(stored, time.time(), interval),
        "retry_in_seconds": _retry_in(stored, time.time()),
        "consecutive_failures": stored.consecutive_failures,
        "status_code": record.status_code,
        "state": row["state"],
        "fetched_at": None if kept is None else kept.fetched_at,
        "reading": None if kept is None else kept.as_payload(),
        "read_error": record.error,
        "not_sent": record.not_sent,
    }


def _row_for(provider_id: str, now: float, interval: float) -> dict[str, Any]:
    """One row, from the same builder the GET uses -- one answer, not two."""

    for row in _provider_rows(now, interval):
        if row["provider_id"] == provider_id:
            return row
    return {"provider_id": provider_id, "state": SOURCE_UNSUPPORTED}
