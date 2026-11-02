"""DeepSeek's ``GET /user/balance``: the account's balance, per currency.

The documented route (§3.2). The one thing this adapter must not do is print a
single "balance": ``balance_infos`` is a **list, one entry per currency**, and
an account holding more than one has more than one answer. Summing them would
add CNY to USD, which is the "never sum across currencies" rule (§4.3.4) doing
its actual work.

Within one currency the provider splits the figure again, and the split is the
same subscription-versus-cash distinction the whole feature is about, one level
down: ``granted_balance`` is promotional and ``topped_up_balance`` is
purchased. Both are kept, each with its own ``kind``, so a reader can see which
part of the balance they actually paid for.

``is_available: false`` is a **successful** read whose answer is "nothing here".
It is not an error: the call worked, the provider answered, and its words are
what the card shows (``available=False`` + ``unavailable_reason``).
"""

import time

from .fields import as_list, as_mapping, decode_json, scalar_text
from .reading import Balance, Reading, UsageUnreadable

__all__ = ["parse"]


def parse(body: bytes) -> Reading:
    """``/user/balance`` -> one balance entry per currency, per source."""

    document = decode_json(body)
    root = as_mapping(document)
    infos = as_list(root.get("balance_infos"))
    available = scalar_text(root.get("is_available"))
    # A documented body always carries ``is_available``. A body with neither it
    # nor a balance list is not this route's answer.
    if not available and not infos:
        raise UsageUnreadable(
            "the host's answer is not a balance record: it carries neither "
            "is_available nor balance_infos"
        )

    fetched_at = time.time()
    balances: list[Balance] = []
    for index, raw in enumerate(infos):
        if not isinstance(raw, dict):
            continue
        info = as_mapping(raw)
        currency = scalar_text(info.get("currency"))
        for field, label, kind in (
            ("total_balance", "Balance", "available"),
            ("granted_balance", "Granted (promotional)", "promotional"),
            ("topped_up_balance", "Topped up (purchased)", "purchased"),
        ):
            amount = scalar_text(info.get(field))
            if not amount:
                continue
            balances.append(
                Balance(
                    id=f"{field}-{currency or index}",
                    label=f"{label} ({currency})" if currency else label,
                    amount=amount,
                    currency=currency,
                    kind=kind,
                )
            )

    if available == "false":
        # The provider said the account cannot serve. Its own reason, if it
        # gave one; otherwise the fact itself, in words that are still NOT an
        # invented explanation of why.
        reason = scalar_text(root.get("unavailable_reason")) or (
            "the host reports this account is not available for inference"
        )
        return Reading.at(
            fetched_at,
            balances=tuple(balances),
            available=False,
            unavailable_reason=reason,
        )
    return Reading.at(fetched_at, balances=tuple(balances))
