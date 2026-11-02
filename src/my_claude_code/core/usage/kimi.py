"""Kimi / Moonshot's ``GET /v1/users/me/balance``: available, voucher and cash.

The documented route (§3.2). The user's own note is why this adapter has an
``available=False`` branch at all: **his plan is expired but the credential is
still present**, so the card has to render a real "not available" rather than
an error. The call works, the provider answers, and what it says is a fact about
the account -- so this is never state 4 and never a red panel.

The three figures are kept apart because they answer three questions:

* ``available_balance`` -- what can actually be spent;
* ``cash_balance`` -- money put in;
* ``voucher_balance`` -- promotional credit.

Summed into a "voucher + cash" total they would still add up, but the page
never does it: a reader who has an expired voucher needs to know WHICH half is
empty, and a total hides exactly that.
"""

import time

from .fields import as_mapping, decode_json, scalar_text
from .reading import Balance, Reading, UsageUnreadable

__all__ = ["parse"]


def parse(body: bytes) -> Reading:
    """``/v1/users/me/balance`` -> the three balances under ``data``."""

    document = decode_json(body)
    root = as_mapping(document)
    data = root.get("data")
    entry = as_mapping(data) if isinstance(data, dict) else root
    if not entry:
        raise UsageUnreadable("the host's answer carried no balance record")
    if not any(scalar_text(entry.get(field)) for field in _KNOWN_FIELDS):
        raise UsageUnreadable(
            "the host's answer is not a balance record: none of "
            "available_balance, cash_balance or voucher_balance are present"
        )

    fetched_at = time.time()
    balances: list[Balance] = []
    for field, label, kind in (
        ("available_balance", "Available", "available"),
        ("voucher_balance", "Voucher (promotional)", "promotional"),
        ("cash_balance", "Cash (purchased)", "purchased"),
    ):
        amount = scalar_text(entry.get(field))
        if not amount:
            continue
        balances.append(Balance(id=field, label=label, amount=amount, kind=kind))

    available = scalar_text(entry.get("available_balance"))
    status = scalar_text(entry.get("status")) or scalar_text(entry.get("plan_status"))
    # "Nothing available" is said three ways on this platform and none of them
    # is an HTTP error: a zero available balance, a status word that is not an
    # active one, or the host's own sentence. Any of them is a measurement of
    # an empty allowance, so the card says so in the provider's words.
    if _is_empty(available, status):
        return Reading.at(
            fetched_at,
            balances=tuple(balances),
            plan=status,
            available=False,
            unavailable_reason=_reason(entry, status),
        )
    return Reading.at(fetched_at, balances=tuple(balances), plan=status)


def _is_empty(available: str, status: str) -> bool:
    """Whether the provider's own numbers and words say the plan cannot serve.

    A missing ``available_balance`` is NOT emptiness -- "the host did not send
    it" and "it is zero" are different facts (IV.16) -- so only an explicit
    zero (in any spelling a decimal string takes) counts, plus a status word
    that names a stopped state.
    """

    if status and status.strip().lower() in _EXPIRED_STATUS_WORDS:
        return True
    if not available:
        return False
    try:
        return float(available) == 0.0
    except ValueError:
        return False


def _reason(entry: dict[str, object], status: str) -> str:
    """The host's own sentence where it gave one, else the fact in words."""

    for field in ("unavailable_reason", "message", "status_message"):
        said = scalar_text(entry.get(field))
        if said:
            return said
    if status:
        return f"the host reports the plan status as {status}"
    return "the host reports a zero available balance"


#: Status words that mean "this plan cannot serve", from the platform's own
#: vocabulary. Matched case-insensitively and only ever used to choose between
#: "empty allowance" and "a balance", never to compute one.
_EXPIRED_STATUS_WORDS: frozenset[str] = frozenset(
    {"expired", "inactive", "suspended", "disabled", "ended", "cancelled", "canceled"}
)

#: The fields a documented balance record carries. Used only to tell a real
#: record from an unrelated 200.
_KNOWN_FIELDS: tuple[str, ...] = (
    "available_balance",
    "voucher_balance",
    "cash_balance",
)
