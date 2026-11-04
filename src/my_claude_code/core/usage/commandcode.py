"""Command Code's ``GET /alpha/billing/credits``: the credit pool this account has.

The route was read out of the installed ``command-code`` 1.65.0 CLI, where the
vendor's own ``projectUsageView`` reads exactly these fields (§3.1). That is
``vendor-binary`` provenance: the route and the field *names* are the vendor's;
the nesting is taken from the same bundle and was **not** confirmed against a
live answer, because this release makes no live calls.

Three blocks, and they are three different questions about one account:

``credits``
    The pool. ``monthlyCredits`` and ``purchasedCredits`` are what is left of
    the monthly allowance and of anything bought; ``freeCredits`` is
    promotional. Kept separate for the same reason DeepSeek's are.
``windowLimits``
    ``fiveHour`` and ``weekly`` slices of that pool -- ``used``, ``cap`` and
    ``resetAt``. These are **windows**, so they reset and are drawn with their
    reset; ``cap`` is a USD allowance, not a balance, hence ``unit="usd"``.
``subscription``
    ``planId``, the period, and a ``status``. The period end is a date, not a
    figure, so it is a note.

A ``planId`` is not a price and is never rendered as one (IV.9): it is the
provider's own identifier for what the account bought.

**There is no monthly grant, and that is a measured fact rather than an
assumption** (SPEC §9.3, which requires the implementer to find the grant rather
than assume its absence). Re-read out of the installed ``command-code`` 1.88.2
bundle's ``projectUsageView`` and every other reader of the credits record:

* the record's fields the vendor's own code reads are exactly ``credits``
  (``monthlyCredits``, ``purchasedCredits``, ``freeCredits``), ``windowLimits``
  (``limited``, ``fiveHour``/``weekly`` each ``{used, cap, resetAt}``),
  ``sandboxMinutes`` (``{usedMinutes, limitMinutes, resetAt}``),
  ``sandboxAccess`` and ``planId``;
* there is **no** monthly grant, allowance, included-credits or total field on
  the record — the identifiers ``monthlyGrant``, ``grantCredits``,
  ``totalCredits``, ``planCredits``, ``monthlyLimit``, ``creditsLimit``,
  ``includedCredits``, ``allowance``, ``monthlyAllowance``, ``creditGrant`` and
  ``monthlyTotal`` do not occur in the bundle at all;
* the only per-plan monthly figure anywhere in the bundle is a **client-side
  price table** (``Ir``: ``individual-go`` 10, ``individual-pro`` 30,
  ``individual-max`` 150, …) that the vendor uses for a *depletion warning*, not
  a value the API returned.

SPEC §9.3 forbids exactly that: "**Never:** … a price table". The pool is the
only monthly figure the API states, so there is **no honest percentage**, so
:func:`parse` emits **no monthly window** — the row is omitted entirely rather
than rendered as a balance, a guess or 0 % — and the provider's declared
``percent_only_note`` says so in one sentence on the card.

If the vendor ever states the grant, this is where it lands: a third ``Window``
under ``windowLimits`` with a real ``cap``, which needs no renderer change
(§9.3's "the unit stays ``usd`` on the window data; the percentage is a
rendering of ``used``/``cap``").
"""

import time

from .fields import as_mapping, decode_json, scalar_text
from .reading import Balance, Reading, UsageUnreadable, Window

__all__ = ["parse"]


def parse(body: bytes) -> Reading:
    """``/alpha/billing/credits`` -> the credit pool, its window slices, the plan."""

    document = decode_json(body)
    root = as_mapping(document)
    credits = _mapping_or_empty(root.get("credits"))
    limits = _mapping_or_empty(root.get("windowLimits"))
    subscription = root.get("subscription")
    if isinstance(subscription, dict):
        subscription = as_mapping(subscription.get("data"))
    else:
        subscription = {}
    if not (credits or limits or subscription):
        raise UsageUnreadable(
            "the host's answer is not a credits record: it carries none of "
            "credits, windowLimits or subscription"
        )

    fetched_at = time.time()
    balances: list[Balance] = []
    for field, label, kind in (
        ("monthlyCredits", "Monthly credits left", "available"),
        ("purchasedCredits", "Purchased credits left", "purchased"),
        ("freeCredits", "Free credits left", "promotional"),
    ):
        amount = scalar_text(credits.get(field))
        if amount:
            balances.append(Balance(id=field, label=label, amount=amount, kind=kind))

    windows: list[Window] = []
    for key, label in (("fiveHour", "5-hour window"), ("weekly", "Weekly window")):
        window = _mapping_or_empty(limits.get(key))
        used = scalar_text(window.get("used"))
        cap = scalar_text(window.get("cap"))
        reset = scalar_text(window.get("resetAt"))
        if not (used or cap or reset):
            continue
        windows.append(
            Window(
                id=key,
                label=label,
                used=used,
                limit=cap,
                unit="usd",
                resets_at=reset,
            )
        )

    plan = scalar_text(subscription.get("planId"))
    notes: list[str] = []
    status = scalar_text(subscription.get("status"))
    if status:
        notes.append(f"Subscription status: {status}.")
    for field, label in (
        ("currentPeriodStart", "This period started"),
        ("currentPeriodEnd", "This period ends"),
    ):
        said = scalar_text(subscription.get(field))
        if said:
            notes.append(f"{label}: {said}.")
    return Reading.at(
        fetched_at,
        windows=tuple(windows),
        balances=tuple(balances),
        plan=plan,
        notes=tuple(notes),
    )


def _mapping_or_empty(value: object) -> dict[str, object]:
    """A nested object, or ``{}`` -- an absent block is fewer facts, not an error."""

    return as_mapping(value) if isinstance(value, dict) else {}
