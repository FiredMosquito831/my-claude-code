"""Every declared adapter, against a real-shaped body and a malformed one.

``specs/INVESTIGATION-USAGE-AND-CREDITS.md`` §7 PR 3's proof bar, item 1: "a
fixture per adapter: a real-shaped response -> the expected reading; a 401 ->
state 4; a 429 -> deadline; a malformed body -> state 4 with a reason, never a
crash."

The 401 and 429 halves are in ``tests/api/test_admin_usage_reads.py``, because
they are facts about the *transport* and are proved against a fake host
in-process. This file is the other half: the decoders, one fixture each, and the
properties that make them honest rather than merely functional --

* a figure is the provider's own text: ``1.0`` stays ``"1.0"``, never a float;
* a missing value is **absent**, never a zero and never ``""`` standing in for a
  number;
* a sentinel means what the provider documented it to mean: OpenRouter's
  ``limit: null`` is "no cap", not "zero left";
* an "expired / not available" answer is a **successful** reading with
  ``available=False`` and the host's own reason, never an error;
* a body that is not the declared shape raises ``UsageUnreadable`` with a
  sentence, never a ``KeyError`` and never a card of blanks.

**No live call is made anywhere in this file, or in any file in this release.**
Every body below is a fixture written from the documented or vendor-binary shape
recorded in §3 of the investigation and §3 of the research dossier.
"""

import json
from typing import Any

import pytest

from my_claude_code.core.usage import adapter_for, parse_with
from my_claude_code.core.usage.reading import UsageUnreadable

# --------------------------------------------------------------------- fixtures


OPENROUTER_KEY: dict[str, Any] = {
    "data": {
        "label": "sk-or-v1-…-dashboard",
        "usage": 12.3456,
        "usage_daily": 0.5,
        "usage_weekly": 2.25,
        "usage_monthly": 9.75,
        "limit": 50,
        "limit_remaining": 37.6544,
        "limit_reset": "monthly",
        "is_free_tier": False,
        "free_model_daily_requests": {"used": 12, "limit": 50},
    }
}

OPENROUTER_NO_CAP: dict[str, Any] = {
    "data": {
        "label": "no cap on this key",
        "usage": 3,
        "limit": None,
        "limit_remaining": None,
        "is_free_tier": False,
    }
}

DEEPSEEK_BALANCE: dict[str, Any] = {
    "is_available": True,
    "balance_infos": [
        {
            "currency": "USD",
            "total_balance": "12.34",
            "granted_balance": "2.00",
            "topped_up_balance": "10.34",
        },
        {
            "currency": "CNY",
            "total_balance": "88.00",
            "granted_balance": "0.00",
            "topped_up_balance": "88.00",
        },
    ],
}

DEEPSEEK_UNAVAILABLE: dict[str, Any] = {
    "is_available": False,
    "balance_infos": [],
}

KIMI_BALANCE: dict[str, Any] = {
    "data": {
        "available_balance": "41.20",
        "voucher_balance": "0.00",
        "cash_balance": "41.20",
    }
}

KIMI_EXPIRED: dict[str, Any] = {
    "data": {
        "available_balance": "0.00",
        "voucher_balance": "0.00",
        "cash_balance": "0.00",
        "status": "EXPIRED",
    }
}

COMMANDCODE_CREDITS: dict[str, Any] = {
    "credits": {
        "monthlyCredits": 42.5,
        "purchasedCredits": 5,
        "freeCredits": 0,
    },
    "windowLimits": {
        "fiveHour": {"used": 1.25, "cap": 8, "resetAt": "2026-11-02T18:00:00Z"},
        "weekly": {"used": 14.5, "cap": 60, "resetAt": "2026-11-07T00:00:00Z"},
    },
    "subscription": {
        "data": {
            "planId": "individual-pro",
            "currentPeriodStart": "2026-11-01T00:00:00Z",
            "currentPeriodEnd": "2026-12-01T00:00:00Z",
            "status": "active",
        }
    },
}

OPENCODE_GO_USAGE: dict[str, Any] = {
    "usage": {
        "rolling": {
            "status": "ok",
            "percent": 37.5,
            "resetsAt": "2026-11-02T18:00:00Z",
        },
        "weekly": {
            "status": "ok",
            "percent": 12,
            "resetsAt": "2026-11-07T00:00:00Z",
        },
        "monthly": {
            "status": "ok",
            "percent": 4,
            "resetsAt": "2026-12-01T00:00:00Z",
        },
    }
}

CLAUDE_USAGE: dict[str, Any] = {
    "five_hour": {"utilization": 38.0, "resets_at": "2026-11-02T18:00:00Z"},
    "seven_day": {"utilization": 11.5, "resets_at": "2026-11-09T00:00:00Z"},
    "seven_day_opus": {"utilization": 7.0, "resets_at": "2026-11-09T00:00:00Z"},
    "extra_usage": {
        "is_enabled": True,
        "monthly_limit": 25.0,
        "used_credits": 0.0,
        "utilization": 0.0,
        "currency": "USD",
    },
    "limits": [
        {
            "kind": "weekly_scoped",
            "scope": {"model": {"display_name": "Opus", "id": "claude-opus-5-5"}},
            "percent": 7.0,
        }
    ],
}

CODEX_USAGE: dict[str, Any] = {
    "plan_type": "plus",
    "rate_limit": {
        "primary_window": {
            "used_percent": 24,
            "reset_at": 1793600000,
            "limit_window_minutes": 300,
        },
        "secondary_window": {
            "used_percent": 62,
            "reset_at": 1793700000,
            "limit_window_minutes": 10080,
        },
    },
    "additional_rate_limits": [
        {
            "limit_name": "gpt-5.5-codex",
            "rate_limit": {"used_percent": 12, "reset_at": 1793600000},
        }
    ],
    "credits": {"has_credits": True, "unlimited": False, "balance": "4.18"},
}

CODEX_NO_PLAN: dict[str, Any] = {
    "plan_type": "none",
    "status": "none",
    "rate_limit": None,
    "credits": None,
}


def _body(document: dict[str, Any]) -> bytes:
    return json.dumps(document).encode("utf-8")


# ------------------------------------------------------------------- openrouter


def test_openrouter_keeps_spend_and_the_remaining_allowance_apart() -> None:
    reading = parse_with("openrouter", _body(OPENROUTER_KEY))
    spend = {balance.id: balance.amount for balance in reading.balances}
    # The provider's OWN text: ``12.3456`` did not become ``12.345599999``.
    assert spend["usage"] == "12.3456"
    assert spend["usage_daily"] == "0.5"
    windows = {window.id: window for window in reading.windows}
    assert windows["key-limit"].limit == "50"
    assert windows["key-limit"].used == "37.6544"
    assert windows["key-limit"].resets_at == "monthly"
    assert windows["free_model_daily_requests"].used == "12"
    assert windows["free_model_daily_requests"].limit == "50"


def test_openrouter_null_limit_is_no_cap_and_never_zero() -> None:
    """The documented sentinel. ``None`` -> absent, never ``"0"``."""

    reading = parse_with("openrouter", _body(OPENROUTER_NO_CAP))
    windows = {window.id: window for window in reading.windows}
    assert "key-limit" not in windows
    assert all(window.limit != "0" for window in reading.windows)


def test_openrouter_rejects_a_body_that_is_not_a_key_record() -> None:
    with pytest.raises(UsageUnreadable):
        parse_with("openrouter", b"<html><body>dashboard</body></html>")
    with pytest.raises(UsageUnreadable):
        parse_with("openrouter", _body({"data": {"unrelated": "shape"}}))


# --------------------------------------------------------------------- deepseek


def test_deepseek_keeps_every_currency_apart() -> None:
    reading = parse_with("deepseek", _body(DEEPSEEK_BALANCE))
    by_id = {balance.id: balance for balance in reading.balances}
    assert by_id["total_balance-USD"].amount == "12.34"
    assert by_id["total_balance-CNY"].amount == "88.00"
    assert by_id["granted_balance-USD"].kind == "promotional"
    assert by_id["topped_up_balance-USD"].kind == "purchased"
    # Six figures, four of them most people never look at -- and NOT one total.
    assert len(reading.balances) == 6


def test_deepseek_unavailable_is_a_reading_not_an_error() -> None:
    reading = parse_with("deepseek", _body(DEEPSEEK_UNAVAILABLE))
    assert reading.available is False
    assert reading.unavailable_reason
    assert reading.balances == ()


def test_deepseek_rejects_a_body_with_neither_field() -> None:
    with pytest.raises(UsageUnreadable):
        parse_with("deepseek", _body({"something": "else"}))


# ------------------------------------------------------------------------- kimi


def test_kimi_keeps_the_three_balances_apart() -> None:
    reading = parse_with("kimi", _body(KIMI_BALANCE))
    by_id = {balance.id: balance for balance in reading.balances}
    assert by_id["available_balance"].amount == "41.20"
    assert by_id["voucher_balance"].kind == "promotional"
    assert by_id["cash_balance"].kind == "purchased"


def test_kimi_an_expired_plan_is_a_real_not_available_state() -> None:
    """The user's own note: expired but the credential is still present."""

    reading = parse_with("kimi", _body(KIMI_EXPIRED))
    assert reading.available is False
    assert reading.plan == "EXPIRED"
    assert "EXPIRED" in reading.unavailable_reason
    # And the figures it did send are still there, at zero, honestly measured.
    assert {b.id for b in reading.balances} == {
        "available_balance",
        "voucher_balance",
        "cash_balance",
    }


def test_kimi_rejects_a_body_with_no_balance_fields() -> None:
    with pytest.raises(UsageUnreadable):
        parse_with("kimi", _body({"data": {"unrelated": True}}))


# ------------------------------------------------------------------ commandcode


def test_commandcode_reads_the_pool_the_windows_and_the_plan() -> None:
    reading = parse_with("commandcode", _body(COMMANDCODE_CREDITS))
    assert reading.plan == "individual-pro"
    pool = {balance.id: balance.amount for balance in reading.balances}
    assert pool == {
        "monthlyCredits": "42.5",
        "purchasedCredits": "5",
        "freeCredits": "0",
    }
    windows = {window.id: window for window in reading.windows}
    assert windows["fiveHour"].used == "1.25"
    assert windows["fiveHour"].limit == "8"
    assert windows["fiveHour"].unit == "usd"
    assert windows["weekly"].resets_at == "2026-11-07T00:00:00Z"
    assert any("Subscription status: active" in note for note in reading.notes)


def test_commandcode_rejects_an_empty_record() -> None:
    with pytest.raises(UsageUnreadable):
        parse_with("commandcode", _body({"nothing": "here"}))


# ------------------------------------------------------------------ opencode go


def test_opencode_go_reads_the_three_windows() -> None:
    reading = parse_with("opencode_go", _body(OPENCODE_GO_USAGE))
    windows = {window.id: window for window in reading.windows}
    assert set(windows) == {"rolling", "weekly", "monthly"}
    assert windows["rolling"].used == "37.5"
    assert windows["rolling"].unit == "percent"
    assert windows["rolling"].resets_at == "2026-11-02T18:00:00Z"
    # A window is a percent of an allowance: nothing here becomes a balance.
    assert reading.balances == ()


def test_opencode_go_rejects_a_body_with_no_window() -> None:
    with pytest.raises(UsageUnreadable):
        parse_with("opencode_go", _body({"usage": {}}))


# ----------------------------------------------------------------------- claude


def test_claude_reads_the_windows_the_per_model_window_and_overage() -> None:
    reading = parse_with("claude", _body(CLAUDE_USAGE))
    windows = {window.id: window for window in reading.windows}
    assert windows["five_hour"].used == "38.0"
    assert windows["seven_day"].used == "11.5"
    assert windows["seven_day_opus"].used == "7.0"
    # Overage is dollars, and it is a window with a limit -- never a balance.
    assert windows["extra_usage"].unit == "usd"
    assert windows["extra_usage"].limit == "25.0"
    assert windows["extra_usage"].used == "0.0"
    assert reading.balances == ()
    assert any("weekly_scoped" in note for note in reading.notes)


def test_claude_rejects_a_body_with_no_windows_and_no_refusal() -> None:
    with pytest.raises(UsageUnreadable):
        parse_with("claude", _body({"unrelated": 1}))


def test_claude_says_unavailable_in_its_own_words_when_it_does() -> None:
    reading = parse_with("claude", _body({"status": "no_subscription"}))
    assert reading.available is False
    assert reading.unavailable_reason


# ------------------------------------------------------------------------ codex


def test_codex_keeps_the_percentage_and_the_cash_in_two_blocks() -> None:
    """The card §4.3 calls the proof: one response, two kinds of number."""

    reading = parse_with("codex", _body(CODEX_USAGE))
    windows = {window.id: window for window in reading.windows}
    assert windows["primary_window"].used == "24"
    assert windows["secondary_window"].used == "62"
    assert windows["codex:gpt-5.5-codex"].used == "12"
    balances = {balance.id: balance for balance in reading.balances}
    assert balances["credits"].amount == "4.18"
    assert reading.plan == "plus"
    # Nothing anywhere combines them.
    assert all(window.limit == "" for window in reading.windows)


def test_codex_no_plan_is_unavailable_not_a_failure() -> None:
    reading = parse_with("codex", _body(CODEX_NO_PLAN))
    assert reading.available is False
    assert reading.unavailable_reason


def test_codex_rejects_a_body_with_neither_windows_nor_credits() -> None:
    with pytest.raises(UsageUnreadable):
        parse_with("codex", _body({"unrelated": []}))


# ------------------------------------------------------------- shared properties


@pytest.mark.parametrize(
    "adapter",
    [
        "openrouter",
        "deepseek",
        "kimi",
        "commandcode",
        "opencode_go",
        "claude",
        "codex",
    ],
)
@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json at all",
        b"[]",
        b"null",
        b'{"data": null}',
        b'{"usage": "a string where an object belongs"}',
        b'{"rate_limit": 7}',
    ],
)
def test_no_malformed_body_ever_crashes_an_adapter(adapter: str, body: bytes) -> None:
    """Every adapter either answers or raises ``UsageUnreadable`` -- never a
    ``TypeError``, never a ``KeyError``, never an ``AttributeError``.

    This is the property the route depends on: it catches ``UsageUnreadable``
    for state 4 and catches everything else as a *bug*, so an adapter that
    raises something else is a card that says "the host's answer could not be
    read (AttributeError)", which is a sentence no reader can act on.
    """

    try:
        reading = parse_with(adapter, body)
    except UsageUnreadable:
        return
    # An adapter that accepted it must still have produced a real reading.
    assert reading.fetched_at > 0


def test_every_adapter_stamps_a_fetched_at() -> None:
    for adapter, document in (
        ("openrouter", OPENROUTER_KEY),
        ("deepseek", DEEPSEEK_BALANCE),
        ("kimi", KIMI_BALANCE),
        ("commandcode", COMMANDCODE_CREDITS),
        ("opencode_go", OPENCODE_GO_USAGE),
        ("claude", CLAUDE_USAGE),
        ("codex", CODEX_USAGE),
    ):
        reading = parse_with(adapter, _body(document))
        assert reading.fetched_at > 0, adapter


def test_an_unknown_adapter_name_is_a_loud_error_not_a_stub() -> None:
    with pytest.raises(LookupError):
        adapter_for("not_a_real_adapter")
