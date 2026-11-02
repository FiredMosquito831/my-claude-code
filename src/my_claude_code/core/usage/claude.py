"""``GET api.anthropic.com/api/oauth/usage``: the subscription's windows.

``vendor-binary`` provenance: §3.1 records the route ten times inside the
installed ``claude.exe``, together with the ``oauth-2025-04-20`` beta header the
CLI sends with it. The **body shape is third-party** -- Poracode's parser, and a
third-party support matrix -- so every field is read defensively and an answer
that carries none of them raises rather than producing a card of blanks.

The credential is the Claude OAuth token MCC already holds for inference, from
the store it already reads (``providers/anthropic_oauth``). It is resolved by
the transport, never here: this module is a pure function of bytes, which is why
it cannot leak a token even in a traceback.

WHAT A CARD GETS, and the split §4.3 forces:

* ``five_hour`` and ``seven_day`` windows -- percentages of a **subscription
  allowance**, with their resets. A percentage without its reset cannot be
  acted on, so both fields travel together.
* ``seven_day_opus`` / ``seven_day_sonnet`` / ``seven_day_fable`` when the host
  reports them: per-model weekly windows, which are what tells a Max user which
  model is eating the week.
* ``extra_usage`` -- **dollars**, an overage allowance separate from the
  windows. It is a ``Window`` with ``unit="usd"`` (an allowance with a monthly
  limit), never a balance: §4.3 rule 1.

``limits[]`` (the ``weekly_scoped`` list) is summarised as notes rather than
parsed into windows, because its ids are namespaced and dynamic and a made-up
window id is worse than the provider's own words.
"""

import time

from .fields import as_list, as_mapping, decode_json, first_text, scalar_text
from .reading import Reading, UsageUnreadable, Window

__all__ = ["parse"]

#: The top-level windows, and the label a card gives each.
_WINDOWS: tuple[tuple[str, str], ...] = (
    ("five_hour", "5-hour window"),
    ("seven_day", "Weekly window"),
    ("seven_day_opus", "Weekly window · Opus"),
    ("seven_day_sonnet", "Weekly window · Sonnet"),
    ("seven_day_fable", "Weekly window · Fable"),
)


def parse(body: bytes) -> Reading:
    """``/api/oauth/usage`` -> the utilisation windows, overage and limits."""

    root = as_mapping(decode_json(body))
    windows: list[Window] = []
    for key, label in _WINDOWS:
        entry = root.get(key)
        if not isinstance(entry, dict):
            continue
        window = as_mapping(entry)
        utilization = first_text(window, "utilization", "used_percent", "percent")
        reset = first_text(window, "resets_at", "resetsAt", "reset")
        if not (utilization or reset):
            continue
        windows.append(
            Window(
                id=key,
                label=label,
                used=utilization,
                unit="percent",
                resets_at=reset,
            )
        )

    if not windows:
        if _says_nothing_available(root):
            # Anthropic's answer that this credential has no subscription
            # behind it. The call worked; there is simply nothing to show.
            return Reading.at(
                time.time(),
                available=False,
                unavailable_reason=first_text(
                    root, "message", "error.message", "status"
                )
                or "the host reports no subscription windows for this account",
            )
        raise UsageUnreadable(
            "the host's answer carried none of the documented usage windows"
        )

    notes: list[str] = []
    extra = root.get("extra_usage")
    if isinstance(extra, dict):
        overage = as_mapping(extra)
        limit = first_text(overage, "monthly_limit", "monthly_limit_usd")
        used = first_text(overage, "used_credits", "used_credits_usd")
        currency = scalar_text(overage.get("currency"))
        enabled = scalar_text(overage.get("is_enabled"))
        if limit or used:
            windows.append(
                Window(
                    id="extra_usage",
                    label="Extra usage" + (f" ({currency})" if currency else ""),
                    used=used,
                    limit=limit,
                    unit="usd",
                )
            )
        if enabled:
            notes.append(f"Extra usage enabled: {enabled}.")
        utilization = first_text(overage, "utilization")
        if utilization:
            notes.append(f"Extra usage utilisation: {utilization}.")

    for raw in as_list(root.get("limits")):
        if not isinstance(raw, dict):
            continue
        entry = as_mapping(raw)
        kind = scalar_text(entry.get("kind")) or scalar_text(entry.get("type"))
        scope = (
            as_mapping(entry.get("scope"))
            if isinstance(entry.get("scope"), dict)
            else {}
        )
        model = (
            as_mapping(scope.get("model"))
            if isinstance(scope.get("model"), dict)
            else {}
        )
        name = scalar_text(model.get("display_name")) or scalar_text(model.get("id"))
        percent = first_text(entry, "percent", "utilization")
        described = " ".join(part for part in (kind, name) if part)
        if described or percent:
            notes.append(" · ".join(part for part in (described, percent) if part))

    plan = first_text(root, "plan", "subscription_type", "tier")
    return Reading.at(
        time.time(), windows=tuple(windows), plan=plan, notes=tuple(notes)
    )


def _says_nothing_available(root: dict[str, object]) -> bool:
    """Whether the host said "nothing here" in its own words.

    Deliberately narrow: only an explicit status or an explicit empty-windows
    marker counts. A body that is merely unrecognisable raises instead, because
    "we could not read this" (state 4) and "there is nothing to read"
    (unavailable) are the two facts this feature must never confuse.
    """

    for field in ("status", "subscription_status"):
        said = scalar_text(root.get(field)).lower()
        if said in {
            "none",
            "no_subscription",
            "inactive",
            "expired",
            "canceled",
            "cancelled",
        }:
            return True
    return root.get("has_subscription") is False
