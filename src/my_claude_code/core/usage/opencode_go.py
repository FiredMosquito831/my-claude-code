"""OpenCode Go's ``GET /zen/go/v1/usage``: the rolling, weekly and monthly windows.

**Third-party-verified, not vendor-published.** Four independent open-source
implementations call this exact path with the Go key as a Bearer token
(Poracode ``openCodeGoApi.ts:13``, CodexBar ``OpenCodeGoUsageFetcher.swift:34``,
Pulse ``OpenCodeGoUsageService.swift:32``, TokenBar
``local_auth_b_providers.dart:914`` -- ``specs/RESEARCH-BILLING-ENDPOINT-DISCOVERY.md``
§3.5). That is strong enough to build on and **not** strong enough to call the
vendor's own word, which is why the row is declared ``third-party`` and the card
says so. Private routes rot (§3.9.8); when this one does, the read fails as
state 4 with the reason and no number, which is the honest degradation.

The response is ``{usage: {rolling|weekly|monthly: {status, percent, resetsAt}}}``
-- three **windows**, each a percent of an allowance with an ISO reset. Nothing
here is money, so nothing here becomes a balance.
"""

import time

from .fields import as_mapping, decode_json, first_text, scalar_text
from .reading import Reading, UsageUnreadable, Window

__all__ = ["parse"]

#: The three windows, in the order the implementations present them and the
#: order a card should read: the shortest one first, because it is the one that
#: runs out first.
_WINDOWS: tuple[tuple[str, str], ...] = (
    ("rolling", "Rolling window"),
    ("weekly", "Weekly window"),
    ("monthly", "Monthly window"),
)


def parse(body: bytes) -> Reading:
    """``/zen/go/v1/usage`` -> one window per period the host reported."""

    document = decode_json(body)
    root = as_mapping(document)
    usage = root.get("usage")
    container = as_mapping(usage) if isinstance(usage, dict) else _flat(root)
    windows: list[Window] = []
    notes: list[str] = []
    for key, label in _WINDOWS:
        entry = container.get(key)
        if not isinstance(entry, dict):
            continue
        window = as_mapping(entry)
        percent = scalar_text(window.get("percent"))
        reset = scalar_text(window.get("resetsAt"))
        status = scalar_text(window.get("status"))
        if not (percent or reset or status):
            continue
        windows.append(
            Window(
                id=key,
                label=label,
                used=percent,
                unit="percent",
                resets_at=reset,
            )
        )
        if status:
            notes.append(f"{label}: {status}.")
    if not windows:
        raise UsageUnreadable(
            "the host's answer carried none of the rolling, weekly or monthly windows"
        )
    plan = first_text(root, "plan", "planName", "usage.plan")
    return Reading.at(
        time.time(), windows=tuple(windows), plan=plan, notes=tuple(notes)
    )


def _flat(root: dict[str, object]) -> dict[str, object]:
    """Tolerate the three keys sitting directly on the root.

    One implementation reads the windows from ``usage`` and the others from
    ``usage`` too; a gateway that unwraps one level is cheap to accept and costs
    nothing when it is not there. The *fields* are still the provider's.
    """

    if any(isinstance(root.get(key), dict) for key, _ in _WINDOWS):
        return root
    return {}
