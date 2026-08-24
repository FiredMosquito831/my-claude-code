"""Operator-allowlist state for the messaging platforms, as pure predicates.

Lives in ``core`` because two declared-legal consumers need the same answer:
the platform factory warns at startup, and the admin config API reports the
state to the dashboard -- and ``api -> messaging`` is not a declared package
edge. Zero dependencies beyond the standard library.
"""

import re


def telegram_auth_open(allowed_user_id: str | None) -> bool:
    """True when Telegram runs without an operator allowlist.

    Blank-after-strip counts as unconfigured so the log warning, the admin
    ``messaging_auth_open`` payload, and the dashboard's own ``configured``
    flag agree on the same notion of "no allowlist".
    """
    return not str(allowed_user_id or "").strip()


# Mirrors messaging.platforms.discord_inbound.parse_allowed_channels
# acceptance semantics without importing across the boundary: a channel list
# is effective only if at least one comma-separated entry survives stripping.
_CSV_SPLIT = re.compile(r"[,]")


def discord_auth_open(allowed_channel_ids: str | None) -> bool:
    """True when Discord runs without a channel allowlist.

    Degenerate lists ("", " ", " ,") accept every channel, so they are
    reported as open, matching runtime behavior.
    """
    raw = str(allowed_channel_ids or "").strip()
    if not raw:
        return True
    return not any(part.strip() for part in _CSV_SPLIT.split(raw))
