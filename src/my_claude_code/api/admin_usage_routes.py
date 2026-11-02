"""Read-only admin API for the providers whose usage rides on their responses.

**One GET and two POSTs, and no write of any kind.** ``GET /admin/api/usage``
reports what this process has already observed; ``POST /admin/api/usage/read``
asks ONE named provider for its own figure, once; and 8.7.0's
``POST /admin/api/usage/read-all`` is the page-level **Refresh now**, which
asks every provider whose own deadline allows it, one at a time, through the
same gate.

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

`values` are the provider's own strings, verbatim, and a field it did not send
is **absent from the mapping** rather than ``""``: "the host did not send this"
and "the host sent an empty string" are different, and the page renders the
first as *not measured by this provider*.

8.7.0 (USAGE-LAYOUTS) adds three things to this route and **no fourth**:

``layout``
    Which of the five Usage layouts the page draws, from the machine setting.
    The route reports the word; it never acts on it. ``specs/PR-USAGE-LAYOUTS-
    SPEC.md`` §3: "nothing about layout in the poller, the payload or the
    routes" -- so this is one key in the answer, and the page's registry
    decides what to draw.

``coverage``
    **One row per configured provider, including every custom one** -- §2's
    rule, derived from configuration and never a hard-coded list. A catalogue
    id in ``PROVIDER_SOURCES`` was always reported; the gap 8.7.0 closes is
    every configured provider MCC has no row for (a custom provider, a
    catalogue id whose row is absent). Each carries the state
    :func:`_coverage_state` decides and, where the state is *possible endpoint,
    unverified*, the recogniser's own words -- **never a number**.

``POST /admin/api/usage/read-all``
    The page-level **Refresh now** button: every configured, readable provider
    whose own deadline has elapsed, one at a time, each behind the same gate
    the single read uses. It is not a second mechanism -- it calls
    :func:`read_one_provider`'s helpers, honours every per-provider deadline,
    and stops the walk the moment the tab goes hidden.
"""

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from my_claude_code.config.constants import (
    USAGE_AUTO_REFRESH_MAX_SECONDS,
    USAGE_AUTO_REFRESH_MIN_SECONDS,
    USAGE_LAYOUT_DEFAULT,
    USAGE_LAYOUT_VALUES,
)
from my_claude_code.config.usage_sources import (
    PROVIDER_SOURCES,
    SOURCE_UNSUPPORTED,
    STAGE_DISCOVERY,
    STAGE_READ,
    UsageSource,
    families_for_base_url,
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
from .credential_display import configured_oauth_pools, configured_pools
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


#: 8.7.0 (§2's eight states, as the ROUTE decides them). Six of these are
#: `core.usage.reading.state_of`'s words and are not restated here; the two that
#: only the page's coverage list can produce are named so the vocabulary is in
#: one place:
#:
#: ``possible_endpoint``
#:     A custom provider whose host or gateway family MCC recognises. WORDS
#:     ONLY -- never a number, never a bar, never a retry. It is a hypothesis
#:     about a route, which is a different fact from having read one.
#: ``needs_credential``
#:     The provider publishes a route and MCC will not use it, because it needs
#:     a secret the user does not have and must not be asked for (§6.1.2).
#:     A fact about the keyring, not about the provider -- which is why it is
#:     kept apart from ``unsupported``.
STATE_POSSIBLE_ENDPOINT = "possible_endpoint"
STATE_NEEDS_CREDENTIAL = "needs_credential"

#: The eight states, in the order the page lists them: the ones with figures
#: first, then the ones a reader has to act on, then the honest absences. The
#: page's layouts sort by this and none of them may invent a ninth.
USAGE_STATE_ORDER = {
    "measured": 0,
    "stale": 1,
    "failed": 2,
    "unavailable": 3,
    "needs_credential": 4,
    STATE_POSSIBLE_ENDPOINT: 5,
    "not_measured": 6,
    "unsupported": 7,
}


def _configured_provider_ids() -> list[tuple[str, str, int]]:
    """Every configured provider, as ``(id, display name, credential count)``.

    **Derived from configuration, never a list.** Between them
    :func:`~my_claude_code.api.credential_display.configured_pools` and
    :func:`~my_claude_code.api.credential_display.configured_oauth_pools` cover
    every API-key pool, every custom provider's key list and both OAuth account
    stores -- so a provider the user added a minute ago appears with no code
    change, which is §2's whole rule.

    The display name is the id for a provider the catalogue does not describe
    (a custom one whose name the page already knows), and the count is what the
    card's "N credentials in this pool" line prints. Both are names and counts:
    no secret, at any point, ever.
    """

    rows: list[tuple[str, str, int]] = []
    seen: set[str] = set()
    for pool_id, secrets in configured_pools():
        provider_id = _provider_id_from_pool(pool_id)
        if provider_id is None or provider_id in seen:
            continue
        seen.add(provider_id)
        rows.append((provider_id, _display_name_for(provider_id), len(list(secrets))))
    for pool_id, accounts in configured_oauth_pools():
        provider_id = _provider_id_from_pool(pool_id)
        if provider_id is None or provider_id in seen:
            continue
        seen.add(provider_id)
        rows.append((provider_id, _display_name_for(provider_id), len(list(accounts))))
    return rows


#: The prefix each pool id family carries, from ``config.credential_names``.
#: Spelled here rather than imported because this route only needs to STRIP
#: them, and a pool id that carries none of them belongs to a family this route
#: does not serve (the web-search pools).
_POOL_PREFIXES = ("env:", "custom:", "oauth:", "websearch:")


def _provider_id_from_pool(pool_id: str) -> str | None:
    """The provider id behind one pool id, or ``None`` for a family we skip.

    ``env:OPENROUTER_API_KEY`` names a credential, not a provider: the mapping
    from an env key to its owning provider is the catalogue's (one provider owns
    each shared credential, ``credential_env_owner``), so it is read there
    rather than guessed at.
    """

    text = str(pool_id or "")
    for prefix in _POOL_PREFIXES:
        if not text.startswith(prefix):
            continue
        rest = text[len(prefix) :]
        if prefix == "custom:" or prefix == "oauth:":
            return rest or None
        if prefix == "env:":
            return _provider_for_env_key(rest)
        return None
    return None


def _provider_for_env_key(env_key: str) -> str | None:
    """The catalogue provider that OWNS one credential env var, or ``None``.

    The owner is the provider whose card edits the field; every other provider
    drawing on the same key is the same credential's sharer
    (``credential_env_owner``). Using the owner means
    ``env:CHATGPT_OAUTH_ACCESS_TOKEN`` reports as ``chatgpt_oauth`` rather than
    as whichever catalogue entry happens to be declared first.
    """

    from my_claude_code.config.admin.provider_manifest import credential_env_owner

    return credential_env_owner(env_key)


def _display_name_for(provider_id: str) -> str:
    """The provider's own name where the catalogue or the table has one."""

    from my_claude_code.config.provider_catalog import PROVIDER_CATALOG

    descriptor = PROVIDER_CATALOG.get(provider_id)
    if descriptor is not None:
        return descriptor.display_name
    declared = source_for(provider_id)
    return declared.display_name or provider_id


def _base_url_for(provider_id: str) -> str:
    """A custom provider's configured base URL, or ``""``.

    Read from the registry rather than from settings: a custom provider's host
    is its own record, and this is the one field the recognisers need. **Never
    a key**: the record carries ``api_keys`` and this function touches
    ``base_url`` only.
    """

    from my_claude_code.config.provider_registry import get_provider_registry

    try:
        entry = get_provider_registry().get(provider_id)
    except Exception:  # pragma: no cover - a registry that cannot be read
        return ""
    return "" if entry is None else str(entry.base_url or "")


def _coverage_state(
    provider_id: str,
    declared: UsageSource,
    state: str,
    *,
    readable: bool,
) -> tuple[str, str, str]:
    """One coverage row's state, plus the two clauses the page may print.

    Returns ``(state, sentence, recognised)`` where ``recognised`` names WHAT
    was recognised (a models.dev provider or a gateway family) and is empty for
    every state that is not a hypothesis.

    The order is the argument, and it mirrors :func:`_read_state`'s:

    1. **A row the table declares** keeps whatever the read state says -- but
       a *credential-bound* row is promoted out of ``unsupported`` first, so
       "needs a management key" never reads as "publishes nothing".
    2. **A custom provider** (or any configured id with no row) is recognised
       by host or gateway family. A recogniser's answer is a hypothesis and
       never a number, so this branch can only produce
       ``possible_endpoint``.
    3. **Nothing recognised** is ``unsupported`` -- honest, and still one row,
       which is exactly what §2 asks: a custom provider that matches nothing
       lands in *Not published* and still appears.
    """

    if declared.supported or declared.endpoint is not None:
        if state == SOURCE_UNSUPPORTED and declared.credential_bound:
            return STATE_NEEDS_CREDENTIAL, "", ""
        return state, "", ""
    if declared.credential_bound and declared.readable is False:
        # Declared, route known, credential absent: never `unsupported`.
        return STATE_NEEDS_CREDENTIAL, "", ""
    recognised = _recognise(provider_id, _base_url_for(provider_id))
    if recognised is not None:
        return STATE_POSSIBLE_ENDPOINT, recognised[0], recognised[1]
    return SOURCE_UNSUPPORTED, "", ""


def _recognise(provider_id: str, base_url: str) -> tuple[str, str] | None:
    """What an unrecognised provider's address says about it, in words.

    Two declarers, in the order the dossier ranks them:

    1. **models.dev's ``api`` host match** -- the stronger rung, because it
       names a provider that is in a published catalogue. A host two providers
       claim is AMBIGUOUS and resolves to nothing (the Q7 refusal).
    2. **A gateway family** from the inference base URL. Two families claiming
       one URL is the same refusal.

    ``None`` means neither claim it, and the caller renders *Not published*.

    **This never produces a route MCC will call and never a figure.** It
    answers "would it be worth a later release's probe", which is the only
    question a hypothesis is entitled to answer.
    """

    host = _host_of(base_url)
    if host:
        from my_claude_code.providers.runtime.models_dev import (
            models_dev_api_hosts,
        )

        claimants = models_dev_api_hosts().get(host)
        if claimants is not None and len(claimants) == 1:
            name = next(iter(claimants))
            return (
                f"its host is the one models.dev names for {name}",
                f"models.dev api host · {name}",
            )

    families = families_for_base_url(base_url)
    if len(families) == 1:
        family = families[0]
        route = family.routes[0] if family.routes else ""
        return (
            f"its address identifies a {family.name} gateway"
            + (f", which serves {route} ({family.reads})" if route else ""),
            f"gateway family · {family.id}",
        )
    return None


def _host_of(base_url: str) -> str:
    """The bare host of a base URL, or ``""``. Never raises on a bad URL."""

    if not base_url:
        return ""
    from urllib.parse import urlsplit

    text = base_url.strip()
    try:
        parts = urlsplit(text if "//" in text else f"https://{text}")
        host = (parts.hostname or "").lower()
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host if "." in host else ""


def _layout_for(settings: object) -> str:
    """Which Usage layout the page should draw, from the machine setting.

    One word, and it is the whole of this route's interest in layout: the page's
    registry renders from it, and nothing here branches on it
    (`specs/PR-USAGE-LAYOUTS-SPEC.md` §3). An unknown word is the fallback --
    the settings layer has already logged the one line, so this stays pure.
    """

    value = str(getattr(settings, "usage_layout", "") or "").strip().lower()
    return value if value in USAGE_LAYOUT_VALUES else USAGE_LAYOUT_DEFAULT


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
    settings = services.requests.current_settings()
    interval = _interval_for(settings)
    return {
        "observed_at": now,
        "stale_after_seconds": STALE_AFTER_SECONDS,
        "minimum_interval_seconds": MINIMUM_INTERVAL_SECONDS,
        "auto_refresh_seconds": int(interval)
        if interval != MINIMUM_INTERVAL_SECONDS
        else 0,
        "read_interval_seconds": int(interval),
        # 8.7.0: the machine setting's word, and the ONE row per configured
        # provider the page's coverage rule needs. Both are additive: a page
        # from an older release ignores them, and nothing below changed shape.
        "layout": _layout_for(settings),
        "providers": _provider_rows(now, interval),
        "coverage": _coverage_rows(now, interval),
    }


def _coverage_rows(now: float, interval: float) -> list[dict[str, Any]]:
    """One row per CONFIGURED provider, including every custom one.

    §2's rule in one function, and the reason it is on the ROUTE rather than in
    the page: the decision needs the declared table, the recognisers and the
    read store at once, and a browser holding only some of them would be a
    second answer to the same question.

    A provider configured on this machine gets a row whether or not anything
    was ever read from it -- **that is the coverage rule** -- and a configured
    provider MCC has never heard of gets ``possible_endpoint`` or
    ``unsupported``, never silence.

    Two providers MCC knows about but which are not separately configurable
    are folded into the provider they belong to:

    * ``openai`` shares ChatGPT's OAuth token (the same subscription behind
      another name), so reporting it separately would print the same accounts
      twice with two different answers;
    * a catalogue id whose ``credential_env`` is owned by another provider is
      already covered by the owner's row.

    Their absence from this list is not a dropped provider -- it is the same
    pool under one name, which the ``shares_credential_with`` key states.
    """

    declared_rows = {row["provider_id"]: row for row in _provider_rows(now, interval)}
    out: list[dict[str, Any]] = []
    for provider_id, display_name, credential_count in _configured_provider_ids():
        declared = source_for(provider_id)
        row = declared_rows.get(provider_id)
        state = SOURCE_UNSUPPORTED if row is None else str(row["state"])
        coverage_state, sentence, recognised = _coverage_state(
            provider_id,
            declared,
            state,
            readable=declared.readable,
        )
        entry: dict[str, Any] = {
            "provider_id": provider_id,
            "display_name": display_name,
            "credentials": credential_count,
            "state": coverage_state,
            #: Which of the two stages produced this state. ``read`` means the
            #: state came from a mechanism MCC actually has; ``discovery``
            #: means it is a HYPOTHESIS and the page must print words only.
            "stage": STAGE_DISCOVERY
            if coverage_state == STATE_POSSIBLE_ENDPOINT
            else STAGE_READ,
            "recognised": recognised,
            "sentence": sentence,
            # Carried through from the declared read so a layout never has to
            # look a provider up a second way.
            "readable": declared.readable,
            "mechanisms": list(declared.mechanisms),
            "detail": declared.detail,
            "needs_credential": declared.needs_credential,
        }
        if row is not None:
            for key in (
                "reading",
                "fetched_at",
                "observed_at",
                "attempted_at",
                "read_error",
                "not_sent",
                "retry_in_seconds",
                "next_read_in_seconds",
                "consecutive_failures",
                "interval_seconds",
                "values",
                "endpoint",
            ):
                entry[key] = row.get(key)
        else:
            # Every layout reads the same keys. A provider with no declared row
            # reports absence explicitly instead of omitting them, so a
            # renderer cannot tell a missing key from a missing figure.
            entry.update(
                {
                    "reading": None,
                    "fetched_at": None,
                    "observed_at": None,
                    "attempted_at": None,
                    "read_error": "",
                    "not_sent": False,
                    "retry_in_seconds": None,
                    "next_read_in_seconds": None,
                    "consecutive_failures": 0,
                    "interval_seconds": int(interval),
                    "values": {},
                    "endpoint": None,
                }
            )
        out.append(entry)
    return out


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
        "hidden": "The tab went hidden; nothing further was asked.",
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


class UsageReadAllRequest(BaseModel):
    """The page-level Refresh now's body. No id, no options, no credentials.

    An empty body is accepted on purpose: the route's own job is to work out
    WHICH providers are due, from the same gate the single read uses, so a page
    cannot ask for a provider it has no business asking for by naming it.
    """

    model_config = {"extra": "forbid"}


@router.post("/admin/api/usage/read-all")
async def read_all_providers(
    request: Request,
    services: ApiServices = Depends(get_services),
) -> dict[str, Any]:
    """The page's **Refresh now**: every due provider, one at a time.

    This is NOT a second read mechanism. It walks the providers the single
    read would accept -- declared, readable, configured -- and asks each one
    through exactly the path :func:`read_one_provider` uses, including
    :meth:`UsageReadStore.claim`, so:

    * a provider inside its own ``Retry-After`` deadline is skipped and says so;
    * a provider read less than the interval ago is skipped and says so;
    * one provider's failure cannot stop the walk or shorten another's wait;
    * **nothing here can charge a key**, because the read path it calls cannot
      (``application/usage_reads.py`` imports no rotation engine, constructs no
      ``FailureKind`` and touches no route health).

    Three refusals are the request's own, as on the single read: a remote
    client (403), a foreign or missing ``Origin`` (403). A provider the walk
    skips is a 200 with the same shape the single read's refusal carries, so
    the page has one render path.

    **The walk stops when the tab goes hidden.** That is the third safety
    behaviour 8.6.0 shipped for the poller, applied to a button: a reader who
    switches away mid-refresh should not have the remaining providers asked in
    the background, because "nothing reads while the tab is hidden" is what
    makes a short interval affordable at all. The browser signals it with the
    ``X-MCC-Visible`` request header -- a header a page on another origin
    cannot set on a cross-origin request without a preflight, and this route is
    loopback-and-Origin-guarded besides.

    The answer carries one entry per provider ATTEMPTED plus the current
    coverage list, so the page repaints from the server's own words rather than
    from what it hoped happened.
    """

    require_loopback_admin(request)
    require_local_origin(request)

    settings = services.requests.current_settings()
    interval = _interval_for(settings)
    attempts: list[dict[str, Any]] = []
    for provider_id, _name, _count in _configured_provider_ids():
        declared = source_for(provider_id)
        if declared.endpoint is None or not declared.readable:
            # Not "skipped": this provider has no read, so asking it is not a
            # question anybody refused. It is simply not a candidate, and the
            # coverage list says why in its own words.
            continue
        if request.headers.get("x-mcc-visible", "").strip().lower() == "hidden":
            attempts.append(
                {
                    "provider_id": provider_id,
                    "requested": False,
                    "refusal": "hidden",
                    "message": "The tab went hidden; nothing further was asked.",
                }
            )
            break
        claim_now = time.time()
        refusal = READINGS.claim(provider_id, now=claim_now, interval=interval)
        if refusal:
            attempts.append(_refused_payload(provider_id, refusal, claim_now, interval))
            continue
        outcome = await perform_read(declared, settings=settings, now=claim_now)
        READINGS.finish(provider_id, outcome.reading, outcome.record)
        attempts.append(
            _result_payload(declared, outcome.reading, outcome.record, interval)
        )
    return {
        "attempted": attempts,
        "layout": _layout_for(settings),
        "providers": _provider_rows(time.time(), interval),
        "coverage": _coverage_rows(time.time(), interval),
    }
