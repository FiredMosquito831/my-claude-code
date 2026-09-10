"""Noticing when a host starts caring who is calling.

The dangerous property of an identity check is that it fails *quietly*. It
does not answer "you are missing a header"; it moves the caller into a smaller
daily bucket, and the caller finds out hours later as a 429 with no stated
cause. So the day the vendor turns the check back on, MCC should say so on the
Models page rather than silently halve its own quota.

Two signals, and only one of them costs a request.

The **passive** one is here, and it is the load-bearing half: a 429 whose body
names ``FreeUsageLimitError`` and whose ``retryAfter`` points at the next UTC
midnight is the free daily quota, exactly and unmistakably -- the vendor's own
limiter computes that wait as ``ceil((86_400_000 - now % 86_400_000) / 1000)``
and MCC's request log already holds three such bodies from before this module
existed. Seeing it costs nothing and proves a real event.

The **active** one is a probe, behind the *Probe capabilities* button, and it
lives in :mod:`my_claude_code.providers.runtime.identity_probe`.

Nothing here changes how a failure is classified, how long a credential is
benched, or which rung of the recovery ladder runs. It reads an error on its
way past and writes one durable fact.
"""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from my_claude_code.providers.recovery import (
    FACT_CLIENT_IDENTITY_REQUIRED,
    PROVIDER_WIDE_MODEL_ID,
    SOURCE_OBSERVATION,
    learned_fact_store,
)

from .client_identity import ClientIdentity

#: The error type the host names when the free daily allowance is spent. Seen
#: verbatim in MCC's own request log, in both of the shapes the gateway wraps
#: it in (``FreeUsageLimitError`` and ``Account.FreeUsageLimitError``).
FREE_USAGE_LIMIT_TYPE = "freeusagelimiterror"

#: The body field carrying the wait, in seconds. Not a header: this host
#: publishes the wait in JSON, which is why nothing in MCC's rate-limit path
#: has ever seen it.
RETRY_AFTER_FIELD = "retryAfter"

#: How far from exact UTC midnight the published wait may land and still be
#: read as a daily reset. The vendor rounds up to a whole second and the reply
#: spends time in flight, so a couple of minutes of slack is the difference
#: between recognising the fingerprint and never recognising it.
MIDNIGHT_TOLERANCE_SECONDS = 300.0

SECONDS_PER_DAY = 86_400.0

#: The detail that separates this fact from anything a probe records.
DETAIL_FREE_DAILY_QUOTA = "free_daily_quota"


def _payloads(body: object, depth: int = 0) -> tuple[Mapping[str, Any], ...]:
    """Every mapping worth inspecting inside one error body."""
    if depth > 3:
        return ()
    if isinstance(body, str | bytes):
        try:
            return _payloads(json.loads(body), depth + 1)
        except ValueError:
            return ()
    if not isinstance(body, Mapping):
        return ()
    payload: Mapping[str, Any] = {str(key): value for key, value in body.items()}
    found: list[Mapping[str, Any]] = [payload]
    for key in ("error", "data", "detail"):
        found.extend(_payloads(payload.get(key), depth + 1))
    return tuple(found)


def _error_body(error: BaseException) -> object:
    """The parsed body of an upstream refusal, in whatever form it arrived."""
    body = getattr(error, "body", None)
    if body is not None:
        return body
    response = getattr(error, "response", None)
    text = getattr(response, "text", None)
    return text if isinstance(text, str) else None


def free_quota_reset(
    error: BaseException, *, now: datetime | None = None
) -> float | None:
    """Seconds until the free daily quota resets, when that is what this is.

    ``None`` for every other refusal, including a 429 that is a per-minute
    rate limit: those do not name the type and do not land on midnight.
    """
    moment = now or datetime.now(UTC)
    for payload in _payloads(_error_body(error)):
        kind = payload.get("type") or payload.get("code")
        if not isinstance(kind, str) or FREE_USAGE_LIMIT_TYPE not in kind.lower():
            continue
        raw = payload.get(RETRY_AFTER_FIELD)
        if not isinstance(raw, int | float) or isinstance(raw, bool):
            continue
        seconds = float(raw)
        if seconds <= 0 or seconds > SECONDS_PER_DAY:
            continue
        elapsed = (
            moment.hour * 3600
            + moment.minute * 60
            + moment.second
            + moment.microsecond / 1e6
        )
        if abs((SECONDS_PER_DAY - elapsed) - seconds) <= MIDNIGHT_TOLERANCE_SECONDS:
            return seconds
    return None


def observe_identity_enforcement(
    provider_id: str,
    identity: ClientIdentity | None,
    error: BaseException,
    *,
    now: datetime | None = None,
) -> bool:
    """Record that this host benched us on its free daily quota, if it did.

    A no-op for every profile that declares no identity, which is every
    profile but the two this release added one to.
    """
    if identity is None or not provider_id:
        return False
    seconds = free_quota_reset(error, now=now)
    if seconds is None:
        return False
    resets_at = datetime.fromtimestamp(
        (now or datetime.now(UTC)).timestamp() + seconds, UTC
    )
    learned_fact_store().record(
        provider_id,
        PROVIDER_WIDE_MODEL_ID,
        FACT_CLIENT_IDENTITY_REQUIRED,
        True,
        source=SOURCE_OBSERVATION,
        detail=DETAIL_FREE_DAILY_QUOTA,
        evidence=(
            f"429 free daily quota; resets {resets_at.strftime('%Y-%m-%dT%H:%M')}Z"
        ),
    )
    logger.info(
        "{}: free daily quota spent; the host resets it at 00:00 UTC "
        "({:.0f}s from now)",
        provider_id.upper(),
        seconds,
    )
    return True


__all__ = [
    "DETAIL_FREE_DAILY_QUOTA",
    "FREE_USAGE_LIMIT_TYPE",
    "MIDNIGHT_TOLERANCE_SECONDS",
    "RETRY_AFTER_FIELD",
    "free_quota_reset",
    "observe_identity_enforcement",
]
