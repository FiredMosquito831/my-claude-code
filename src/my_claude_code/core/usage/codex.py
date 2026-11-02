"""``GET chatgpt.com/backend-api/wham/usage``: the Codex plan's windows and credits.

``vendor-binary`` provenance for the route -- §3.1 records it three times inside
the installed ``codex.exe`` 0.166.0 alongside every body field name -- and
**third-party for the nesting**, which Poracode's parser mirrors from Codex's
Rust structs. No live call was made to confirm the nesting, so every field is
read through :func:`first_text` at several plausible paths and an answer with
none of them raises rather than producing a card of blanks.

THE CARD THIS FEEDS IS THE ONE §4.3 CALLS THE PROOF: Codex reports a percentage
of each window **and** a credits balance on the same response, which is why the
page keeps subscription allowance and cash in two blocks and never sums them.
Here that means ``windows`` for the percentages and ``balances`` for the
credits, and no field anywhere that combines them.

The account id travels as ``ChatGPT-Account-ID``; its *value* comes from the
account store the transport reads, never from this module and never from a
literal.
"""

import time

from .fields import as_list, as_mapping, decode_json, first_text, scalar_text
from .reading import Balance, Reading, UsageUnreadable, Window

__all__ = ["parse"]


def parse(body: bytes) -> Reading:
    """``/backend-api/wham/usage`` -> the plan's windows, credits and limits."""

    root = as_mapping(decode_json(body))
    plan = first_text(root, "plan_type", "plan", "subscription.plan")
    windows: list[Window] = []
    notes: list[str] = []

    rate_limit = root.get("rate_limit")
    if isinstance(rate_limit, dict):
        limit = as_mapping(rate_limit)
        for key, label in (
            ("primary_window", "Primary window"),
            ("secondary_window", "Secondary window"),
        ):
            entry = limit.get(key)
            if not isinstance(entry, dict):
                continue
            window = as_mapping(entry)
            used = first_text(window, "used_percent", "usedPercent")
            reset = first_text(window, "reset_at", "resetsAt", "reset_after_seconds")
            minutes = first_text(window, "limit_window_minutes", "window_minutes")
            if not (used or reset or minutes):
                continue
            windows.append(
                Window(
                    id=key,
                    label=label,
                    used=used,
                    unit="percent",
                    resets_at=reset,
                )
            )
            if minutes:
                notes.append(f"{label} length: {minutes} minutes.")

    # ``additional_rate_limits[]``: one entry per named limit, each with its
    # own id. The id is namespaced (``codex:<limitId>``) rather than given a
    # house name, which is §3.9.1's best idea -- a provider's per-model windows
    # flow through without a schema change.
    for raw in as_list(root.get("additional_rate_limits")):
        if not isinstance(raw, dict):
            continue
        entry = as_mapping(raw)
        name = first_text(entry, "limit_name", "name", "id") or "additional limit"
        nested = entry.get("rate_limit")
        window = as_mapping(nested) if isinstance(nested, dict) else entry
        used = first_text(window, "used_percent", "usedPercent")
        reset = first_text(window, "reset_at", "resetsAt", "reset_after_seconds")
        if not (used or reset):
            continue
        windows.append(
            Window(
                id=f"codex:{name}",
                label=name,
                used=used,
                unit="percent",
                resets_at=reset,
            )
        )

    balances: list[Balance] = []
    credits = root.get("credits")
    if isinstance(credits, dict):
        credit = as_mapping(credits)
        amount = first_text(credit, "balance", "credits_balance", "remaining")
        unlimited = scalar_text(credit.get("unlimited"))
        has_credits = scalar_text(credit.get("has_credits"))
        if amount:
            balances.append(
                Balance(
                    id="credits",
                    label="Credits balance",
                    amount=amount,
                    kind="available",
                )
            )
        if unlimited:
            notes.append(f"Credits unlimited: {unlimited}.")
        if has_credits:
            notes.append(f"Has credits: {has_credits}.")
    balance = first_text(root, "x-codex-credits-balance", "credits_balance")
    if balance and not any(entry.id == "credits" for entry in balances):
        balances.append(
            Balance(
                id="credits", label="Credits balance", amount=balance, kind="available"
            )
        )

    limit_name = first_text(root, "limit_name", "rate_limit.limit_name")
    if limit_name:
        notes.append(f"Limit name: {limit_name}.")

    if not windows and not balances:
        if _says_nothing_available(root):
            return Reading.at(
                time.time(),
                plan=plan,
                available=False,
                unavailable_reason=first_text(root, "message", "status", "detail")
                or "the host reports no usage windows for this account",
            )
        raise UsageUnreadable(
            "the host's answer carried neither a usage window nor a credits balance"
        )
    return Reading.at(
        time.time(),
        windows=tuple(windows),
        balances=tuple(balances),
        plan=plan,
        notes=tuple(notes),
    )


def _says_nothing_available(root: dict[str, object]) -> bool:
    """An explicit "nothing here", never merely an unrecognisable body."""

    for field in ("status", "plan_type"):
        said = scalar_text(root.get(field)).lower()
        if said in {"none", "no_plan", "inactive", "expired", "canceled", "cancelled"}:
            return True
    return root.get("has_subscription") is False
