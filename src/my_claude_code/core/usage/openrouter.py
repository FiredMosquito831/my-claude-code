"""OpenRouter's ``GET /api/v1/key``: this key's spend, and its remaining limit.

The documented per-key route (§3.2 of
``specs/INVESTIGATION-USAGE-AND-CREDITS.md``). **Not ``/api/v1/credits``**:
that one needs a management key, answers an ordinary inference key with a
``403``, and this feature never asks the user for a credential they do not
already have (§6.1.2).

Two blocks, and the split is the whole reason this row is worth having:

* ``usage`` / ``usage_daily`` / ``usage_weekly`` / ``usage_monthly`` are
  **spend** -- money already gone, in dollars.
* ``limit_remaining`` is an **allowance that resets** at ``limit_reset``. It is
  drawn as a window with a reset, never as a balance (§4.3 rule 2: a balance
  never renders as a bar with a reset, and a window never renders as money --
  so it is a ``Window`` with ``unit="usd"``, the shape §4.3 rule 1 asks for).

The documented sentinel this adapter exists to get right: **``limit: null``
means "no cap", not zero.** The provider's own reference says so. A zero here
would read as "you have nothing left", which is the opposite of the truth, so a
null limit produces no ceiling field at all rather than ``"0"`` -- §4.1's rule
and IV.16's, in one branch.
"""

import time

from .fields import as_mapping, decode_json, first_text, scalar_text
from .reading import Balance, Reading, UsageUnreadable, Window

__all__ = ["parse"]


def parse(body: bytes) -> Reading:
    """``/api/v1/key`` -> this key's spend and remaining allowance.

    The documented envelope is ``{"data": {...}}``. A body that carries the
    fields at the top level instead is read too, because a gateway in front of
    this route is common and unwrapping is a one-line tolerance rather than a
    guess: the *fields* are still the provider's, and a body with neither shape
    raises.
    """

    document = decode_json(body)
    root = as_mapping(document)
    data = root.get("data")
    entry = as_mapping(data) if isinstance(data, dict) else root
    if not entry:
        raise UsageUnreadable("the host's answer carried no key record")
    if not any(scalar_text(entry.get(field)) for field in _KNOWN_FIELDS):
        raise UsageUnreadable(
            "the host's answer is not a key record: none of its documented "
            "fields are present"
        )

    fetched_at = time.time()
    balances: list[Balance] = []
    for field, label, kind in (
        ("usage", "Total spend on this key", "spend"),
        ("usage_daily", "Spend today", "spend"),
        ("usage_weekly", "Spend this week", "spend"),
        ("usage_monthly", "Spend this month", "spend"),
    ):
        amount = scalar_text(entry.get(field))
        if amount:
            balances.append(
                Balance(id=field, label=label, amount=amount, currency="USD", kind=kind)
            )

    windows: list[Window] = []
    for field, label in (
        ("free_model_daily_requests", "Free model requests today"),
        ("free_model_weekly_requests", "Free model requests this week"),
    ):
        window = _free_model_window(entry.get(field), field, label)
        if window is not None:
            windows.append(window)

    limit = scalar_text(entry.get("limit"))  # None -> "" : "no cap", never "0"
    remaining = first_text(entry, "limit_remaining", "limit_remaining_usd")
    reset = first_text(entry, "limit_reset", "limit_reset_at")
    if limit or remaining or reset:
        windows.append(
            Window(
                id="key-limit",
                label="Key limit",
                # ``used`` is the ceiling's complement only when BOTH numbers
                # arrived; one number alone is left where it is rather than
                # computed from the other, because a derived figure is exactly
                # what §4.1 forbids.
                used=remaining,
                limit=limit,
                unit="usd",
                resets_at=reset,
            )
        )

    notes: list[str] = []
    label = scalar_text(entry.get("label"))
    if label:
        notes.append(f"This key is labelled {label}.")
    free_tier = scalar_text(entry.get("is_free_tier"))
    if free_tier:
        notes.append(f"Free tier: {free_tier}.")
    return Reading.at(
        fetched_at,
        windows=tuple(windows),
        balances=tuple(balances),
        plan="free tier" if free_tier == "true" else "",
        notes=tuple(notes),
    )


def _free_model_window(value: object, field: str, label: str) -> Window | None:
    """The free-model allowance, as a used/limit pair, or nothing.

    A host that sends neither half has said nothing about it; one that sends
    only the limit still has a limit worth showing, with no ``used`` -- the
    page draws the pair it was given and never invents the other half.
    """

    if not isinstance(value, dict):
        return None
    used = scalar_text(value.get("used"))
    limit = scalar_text(value.get("limit"))
    if not (used or limit):
        return None
    return Window(id=field, label=label, used=used, limit=limit, unit="requests")


#: The fields a documented key record carries. Used only to tell a real record
#: from an unrelated 200 -- a dashboard shell served in place of the API, say.
_KNOWN_FIELDS: tuple[str, ...] = (
    "label",
    "usage",
    "usage_daily",
    "usage_weekly",
    "usage_monthly",
    "limit",
    "limit_remaining",
    "is_free_tier",
    "free_model_daily_requests",
)
