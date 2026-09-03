"""Bounded, allow-listed capture of inbound HTTP request headers.

The request log has carried a ``headers`` column since it was introduced, but
nothing ever produced a value for it. This module is that producer, and it is
deliberately the only place in the codebase that decides which inbound header
*values* are allowed to reach durable storage.
"""

from collections.abc import Mapping

# POSITIVE allow-list, never a deny-list.
#
# A deny-list ("store everything except authorization, x-api-key, cookie...")
# is wrong the moment a client, proxy, or SDK release sends a credential-
# bearing header nobody anticipated -- the new header is stored in full simply
# because no one had thought to ban it yet, and the leak is silent and
# permanent. A positive list inverts the default: an unknown header is not
# stored, so the failure mode of forgetting to update this set is missing
# diagnostics, not a leaked secret.
ALLOWED_HEADERS: frozenset[str] = frozenset(
    {
        "user-agent",
        "x-app",
        "anthropic-version",
        "anthropic-beta",
        "accept",
        "content-type",
        # The launcher's own attribution claim, and non-secret by
        # construction: MCC writes both of these into the provider document it
        # generates for a harness, so their values are ids this repository
        # already publishes. Storing them is what lets the request-detail pane
        # say "explicit header" rather than "inferred from user-agent" without
        # a second column recording where the harness id came from -- the row
        # keeps the evidence, so the answer stays re-derivable.
        "x-mcc-harness",
        "x-mcc-harness-version",
    }
)

# Reserved key holding the comma-separated NAMES of headers that arrived but
# are not on the allow-list, so "what is this client actually sending?" stays
# answerable without any unknown value touching the database. Parentheses are
# not legal in an HTTP field name (RFC 9110 token), so this key can never
# collide with a real lowercased header name.
UNLISTED_NAMES_KEY = "(unlisted)"

# Caps. The headers column is analytics, not forensics: it exists to answer
# "which client and which API version was this?", and every one of those
# answers fits in a few hundred bytes. The caps below bound what a hostile or
# merely odd client can write into a row while leaving real traffic untouched
# -- a real Claude Code user-agent plus a full anthropic-beta list is well
# under 1 KiB, and browsers send well under 32 distinct headers.
MAX_VALUE_CHARS = 1_024
MAX_UNLISTED_NAMES = 32
MAX_NAME_CHARS = 64
MAX_TOTAL_CHARS = 4_096

# RFC 9110 field-name token characters. Sanitising names to this set is a
# second, structural guarantee that no value text can ride along inside the
# unlisted-names list: ":", whitespace, and quotes cannot survive it.
_TOKEN_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz0123456789!#$%&'*+-.^_`|~",
)


def capture_headers(headers: Mapping[str, str] | None) -> dict[str, str] | None:
    """Return the storable view of one request's inbound headers.

    Allow-listed headers keep their values (keys lowercased, values truncated).
    Every other header contributes its NAME ONLY to a single sorted entry under
    :data:`UNLISTED_NAMES_KEY`; its value is never read into the result.
    Returns ``None`` when there is nothing worth storing.
    """
    if not headers:
        return None

    captured: dict[str, str] = {}
    unlisted: set[str] = set()
    for raw_name, raw_value in headers.items():
        name = str(raw_name).strip().lower()
        if not name:
            continue
        if name in ALLOWED_HEADERS:
            if isinstance(raw_value, str) and raw_value:
                captured[name] = _truncate(raw_value, MAX_VALUE_CHARS)
            continue
        # Not allow-listed: the value is never touched, only the name.
        safe = _sanitise_name(name)
        if safe:
            unlisted.add(safe)

    if unlisted:
        captured[UNLISTED_NAMES_KEY] = _format_unlisted(unlisted)

    if not captured:
        return None
    return _enforce_total_cap(captured)


def _sanitise_name(name: str) -> str:
    """Reduce a header name to token characters and cap its length."""
    cleaned = "".join(char for char in name if char in _TOKEN_CHARS)
    return cleaned[:MAX_NAME_CHARS]


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "..."


def _format_unlisted(names: set[str]) -> str:
    """Sort for stable output, keep at most ``MAX_UNLISTED_NAMES``, note the rest."""
    ordered = sorted(names)
    kept = ordered[:MAX_UNLISTED_NAMES]
    dropped = len(ordered) - len(kept)
    if dropped:
        kept.append(f"+{dropped}-more")
    return ",".join(kept)


def _enforce_total_cap(captured: dict[str, str]) -> dict[str, str]:
    """Bound the serialised size of the whole mapping.

    The unlisted-names list is discretionary diagnostics, so it is dropped
    first; allow-listed values are then shed in reverse key order until the
    mapping fits. Dropping whole entries (rather than shaving characters) keeps
    every value that survives a faithful copy of what the client sent.
    """
    if _serialised_size(captured) <= MAX_TOTAL_CHARS:
        return captured
    captured.pop(UNLISTED_NAMES_KEY, None)
    for key in sorted(captured, reverse=True):
        if _serialised_size(captured) <= MAX_TOTAL_CHARS:
            break
        captured.pop(key, None)
    return captured


def _serialised_size(captured: Mapping[str, str]) -> int:
    """Approximate the JSON length without paying for a real encode each pass."""
    return sum(len(key) + len(value) + 6 for key, value in captured.items()) + 2


__all__ = [
    "ALLOWED_HEADERS",
    "MAX_NAME_CHARS",
    "MAX_TOTAL_CHARS",
    "MAX_UNLISTED_NAMES",
    "MAX_VALUE_CHARS",
    "UNLISTED_NAMES_KEY",
    "capture_headers",
]
