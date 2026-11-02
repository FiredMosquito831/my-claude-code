"""Perform one declared usage read: one GET, its own client, and no bookkeeping.

This is the module `specs/INVESTIGATION-USAGE-AND-CREDITS.md` §6.3 is really
about. A usage read is a GET against a host MCC already talks to, and it must be
**impossible for it to charge a key or bench a provider** -- so it does not
enter the machinery that could:

* it builds its **own** ``httpx.AsyncClient``, a sibling of the two probe
  families already in this package (``reasoning_probe.py``,
  ``capability_probes.py``), and passes the credential as a string;
* it never imports :mod:`my_claude_code.core.credential_rotation`, never
  constructs a :class:`~my_claude_code.core.failures.FailureKind`, never calls
  ``RateLimitCooldown.resolve`` and never touches
  :mod:`my_claude_code.application.route_health`;
* a ``401`` becomes **state 4 with the provider's own reason**, and a ``429``
  becomes **this provider's usage deadline** -- read out of ``Retry-After`` with
  the existing parser (``core.rate_limit.parse_rate_limit_duration``, imported
  and not edited) -- and nothing else happens in either case.

The proof that this is true rather than intended is a test that monkeypatches
every rotation and bench recorder to raise and then asks the route for a reading
against a fake host answering 401 and 429 in turn
(``tests/api/test_admin_usage_reads.py``). It is the mutation-shaped proof III.4
asks for: remove the isolation and the test fails loudly.

**Where the read leaves from** is the provider's own configured exit
(:func:`~.config.masked_exit_for`, the same single read ``admin_routes`` uses for
the Providers card's probes): a host this operator only reaches through proxies
must not see this computer's address because a card button asked it something.
When the exit is refused, nothing is sent and the card says so.

**What it will not do**, each from §3.4's list: follow a redirect (a 3xx is a
refusal, because the credential must not travel to a host the operator never
named), use anything but GET, send a body, or read an unbounded response (a
bounded prefix is enough for a balance, and a mis-typed path that returns a
40 MB dashboard must not be parsed).
"""

import time

import httpx
from loguru import logger

from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.settings import Settings
from my_claude_code.config.usage_sources import UsageEndpoint, UsageSource
from my_claude_code.core.rate_limit import parse_rate_limit_duration
from my_claude_code.core.usage import parse_with
from my_claude_code.core.usage.reading import (
    Reading,
    ReadRecord,
    UsageCredential,
    UsageUnreadable,
)

from .config import masked_exit_for
from .usage_credentials import CredentialRefusal, resolve_credential

__all__ = ["MAX_RESPONSE_BYTES", "READ_TIMEOUT_SECONDS", "ReadOutcome", "perform_read"]

#: One attempt, no retry ladder (§6.1: "a probe that retries multiplies the
#: budget silently", and the same rule is right for a read). Ten seconds is
#: enough for a balance route and short enough that a button never hangs.
READ_TIMEOUT_SECONDS = 10.0

#: The most of a response this module will look at. A balance route is a few
#: hundred bytes; a mis-typed path that returns a dashboard must not be parsed.
MAX_RESPONSE_BYTES = 256 * 1024


class ReadOutcome:
    """What one attempt left behind: a reading, or the reason there is none."""

    __slots__ = ("reading", "record")

    def __init__(self, reading: Reading | None, record: ReadRecord) -> None:
        self.reading = reading
        self.record = record


async def perform_read(
    declared: UsageSource,
    *,
    settings: Settings,
    now: float | None = None,
    client: httpx.AsyncClient | None = None,
) -> ReadOutcome:
    """Read one provider's declared route and return the outcome. Never raises.

    ``client`` is for tests: the fake host in-process is an ``httpx.AsyncClient``
    with a ``MockTransport`` behind it, which is how every adapter in this
    release is proved without a single live call. When it is ``None`` -- the
    production path -- the client is this function's own, built here and closed
    here.
    """

    endpoint = declared.endpoint
    stamped_at = time.time() if now is None else now
    if endpoint is None:  # pragma: no cover - the route refuses this first
        return ReadOutcome(
            None,
            ReadRecord(
                attempted_at=stamped_at,
                error="MCC has no read for this provider.",
                not_sent=True,
            ),
        )
    try:
        credential = resolve_credential(endpoint, settings)
    except CredentialRefusal as refusal:
        # "No credential" is a fact the page prints, not a traceback, and it is
        # `not_sent`: nothing left this computer.
        return ReadOutcome(
            None,
            ReadRecord(attempted_at=stamped_at, error=str(refusal), not_sent=True),
        )
    return await _send(
        declared,
        endpoint,
        credential,
        stamped_at=stamped_at,
        settings=settings,
        client=client,
    )


def _headers(endpoint: UsageEndpoint, credential: UsageCredential) -> dict[str, str]:
    """The declared headers plus the credential's own. Never logged."""

    headers = {
        "Authorization": f"Bearer {credential.token}",
        "Accept": "application/json",
        **dict(endpoint.headers),
    }
    if endpoint.account_header and credential.account_id:
        headers[endpoint.account_header] = credential.account_id
    return headers


def _exit_for(declared: UsageSource, settings: Settings) -> tuple[str, str]:
    """``(proxy, refusal)`` for this provider: its own configured exit.

    The provider the **row** names, not the one its credential variable happens
    to belong to -- ``opencode`` and ``opencode_go`` share one variable and have
    different proxies, and ``open_router`` shares nothing. A provider with no
    descriptor (an id the catalogue does not carry) is answered by the same
    function with an empty chain, which is this computer's own address exactly
    as it is for any provider with no proxy.
    """

    descriptor = PROVIDER_CATALOG.get(declared.provider_id)
    attr = descriptor.proxy_attr if descriptor is not None else None
    static = str(getattr(settings, attr, "") or "") if attr else ""
    exit_ = masked_exit_for(
        declared.provider_id,
        static,
        settings,
        name=descriptor.display_name
        if descriptor is not None
        else declared.display_name,
    )
    if exit_.refused:
        return "", exit_.refused
    return exit_.proxy or "", ""


async def _send(
    declared: UsageSource,
    endpoint: UsageEndpoint,
    credential: UsageCredential,
    *,
    stamped_at: float,
    settings: Settings,
    client: httpx.AsyncClient | None,
) -> ReadOutcome:
    """One GET, one answer, and nothing else touched.

    Every return path builds a :class:`ReadRecord`; no path raises. The
    classification is §3.3's table, restricted to the rows a declared read can
    produce: a 200 that parses, a 200 that does not, a refusal, a redirect, a
    429 and a transport fault.
    """

    proxy, refused = _exit_for(declared, settings)
    if refused:
        return ReadOutcome(
            None,
            ReadRecord(attempted_at=stamped_at, error=refused, not_sent=True),
        )

    owned = client is None
    http = client or httpx.AsyncClient(
        proxy=proxy or None,
        timeout=READ_TIMEOUT_SECONDS,
        follow_redirects=False,
    )
    try:
        response = await http.get(endpoint.url, headers=_headers(endpoint, credential))
    except httpx.TimeoutException:
        return ReadOutcome(
            None,
            ReadRecord(
                attempted_at=stamped_at, error="the host did not answer in time"
            ),
        )
    except Exception as error:
        # A transport failure is never a number and never a state-3 claim. The
        # type name is enough for a reader, and it carries no address, no header
        # and no credential.
        logger.debug(
            "Usage read for {} failed: {}", declared.provider_id, type(error).__name__
        )
        return ReadOutcome(
            None,
            ReadRecord(
                attempted_at=stamped_at,
                error=f"the request did not complete ({type(error).__name__})",
            ),
        )
    try:
        return _classify(declared, endpoint, response, stamped_at=stamped_at)
    finally:
        # An owned client is closed on every path. A client a caller supplied is
        # left alone: it is the caller's object, and in a test it is the
        # recorder whose calls are asserted afterwards.
        if owned:
            await http.aclose()


def _classify(
    declared: UsageSource,
    endpoint: UsageEndpoint,
    response: httpx.Response,
    *,
    stamped_at: float,
) -> ReadOutcome:
    status = response.status_code
    if status == 429:
        wait = _retry_after(response.headers)
        return ReadOutcome(
            None,
            ReadRecord(
                attempted_at=stamped_at,
                status_code=status,
                error="the provider is rate-limiting usage reads",
                retry_at=None if wait is None else stamped_at + wait,
            ),
        )
    if 300 <= status < 400:
        # Not followed, and not followed on purpose: the credential must not
        # travel to a host the operator never named (§3.4 rule 2).
        return ReadOutcome(
            None,
            ReadRecord(
                attempted_at=stamped_at,
                status_code=status,
                error=(
                    "the host answered with a redirect, which MCC does not follow "
                    "for a credential-bearing request"
                ),
            ),
        )
    if status in (401, 403):
        said = _reason_from_body(response)
        return ReadOutcome(
            None,
            ReadRecord(
                attempted_at=stamped_at,
                status_code=status,
                error=said
                or (
                    "the stored credential was refused. Re-authorise it on the "
                    "Providers page."
                ),
            ),
        )
    if status != 200:
        said = _reason_from_body(response)
        return ReadOutcome(
            None,
            ReadRecord(
                attempted_at=stamped_at,
                status_code=status,
                error=said or f"the host answered HTTP {status}",
            ),
        )

    try:
        reading = parse_with(endpoint.adapter, response.content[:MAX_RESPONSE_BYTES])
    except UsageUnreadable as unreadable:
        return ReadOutcome(
            None,
            ReadRecord(
                attempted_at=stamped_at,
                status_code=status,
                error=f"could not read the host's answer: {unreadable}",
            ),
        )
    except Exception as error:  # a broken adapter must not become a 500
        logger.warning(
            "Usage adapter {} raised {} for {}.",
            endpoint.adapter,
            type(error).__name__,
            declared.provider_id,
        )
        return ReadOutcome(
            None,
            ReadRecord(
                attempted_at=stamped_at,
                status_code=status,
                error=f"could not read the host's answer ({type(error).__name__})",
            ),
        )
    return ReadOutcome(reading, ReadRecord(attempted_at=stamped_at, status_code=status))


def _retry_after(headers: httpx.Headers) -> float | None:
    """The provider's own wait, if it stated one -- never a guess.

    ``parse_rate_limit_duration`` is imported from ``core.rate_limit`` and is
    *only* imported: that module's ``RateLimitCooldown`` is one of exactly three
    signals that charge a key (V.3), and this read must never reach it.
    """

    raw = headers.get("retry-after")
    if not raw:
        return None
    seconds = parse_rate_limit_duration("retry-after", raw)
    if seconds is None or seconds < 0:
        return None
    return seconds


def _reason_from_body(response: httpx.Response) -> str:
    """The provider's own short sentence, if its body carries one.

    Never echoes an arbitrary body and never returns more than a sentence: a
    refusal body can be a whole HTML page, and the card's reason is one line.
    Only the fields an API error envelope uses are read.
    """

    try:
        document = response.json()
    except Exception:
        return ""
    if not isinstance(document, dict):
        return ""
    for key in ("error", "message", "detail", "error_description"):
        value = document.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
        if isinstance(value, dict):
            message = value.get("message")
            if isinstance(message, str) and message.strip():
                return " ".join(message.split())
    return ""
