"""The payloads the Web Search page jsdom scenario renders (7.113.0).

Pure data built from the running tree's own manifest (``FIELDS`` /
``SECTIONS``): no disk, no network, no ``Settings``. It imports nothing that
7.112.1 did not already have, so the BASE tree can run this file by path and
write the page-equality baseline with its own manifest::

    <base>/.venv/Scripts/python.exe -I tests/support/websearch_page_fixture.py OUT

The scenario (``tests/api/admin_jsdom_websearch_chain.mjs``) draws the page
from these payloads on whichever tree it is pointed at.
"""

import json
import sys
from pathlib import Path
from typing import Any

from my_claude_code.config.admin.manifest import FIELDS, SECTIONS, ConfigOptionSpec
from my_claude_code.config.admin.values import MASKED_SECRET

#: The credentials the scenario shows as configured (the user's two, Q1, plus
#: Context7 where the tree has it). Every other key field is "Missing key".
CONFIGURED = frozenset({"EXA_API_KEY", "TAVILY_API_KEY", "CONTEXT7_API_KEY"})

#: Markup-shaped text in places third parties fill: a try's key label and
#: kind (tries_json), a provider's error message, a documentation excerpt.
PROBE = "<img src=x onerror=window.__wsXss=1>probe <b>bold</b>"


def served_field(field: Any, value: str | None = None) -> dict[str, Any]:
    """One manifest field as ``/admin/api/config`` serves it."""

    configured = field.key in CONFIGURED if field.secret else bool(value or field.default)
    if field.secret:
        # The server never sends a secret: a configured one is the mask.
        served = MASKED_SECRET if configured else ""
    else:
        served = value if value is not None else field.default
    return {
        "key": field.key,
        "label": field.label,
        "section": field.section_id,
        "provider": None,
        "type": field.field_type,
        "value": served,
        "effective": field.default,
        "default": field.default,
        "set": value is not None,
        "configured": configured,
        "source": "env" if value is not None else "default",
        "locked": False,
        "secret": field.secret,
        "advanced": field.advanced,
        "restart_required": field.restart_required,
        "session_sensitive": field.session_sensitive,
        "options": [
            {"value": option.value, "label": option.label}
            if isinstance(option, ConfigOptionSpec)
            else {"value": option, "label": option}
            for option in field.options
        ],
        "description": field.description,
        "minimum": field.minimum,
        "maximum": field.maximum,
        "range_hint": field.range_hint,
    }


def websearch_fields(values: dict[str, str] | None = None) -> list[dict[str, Any]]:
    chosen = values or {}
    return [
        served_field(field, chosen.get(field.key))
        for field in FIELDS
        if field.section_id == "websearch"
    ]


def _section() -> dict[str, Any]:
    section = next(spec for spec in SECTIONS if spec.section_id == "websearch")
    return {
        "id": section.section_id,
        "label": section.label,
        "description": section.description,
        "advanced": section.advanced,
    }


def _keys(env: str, labels: list[str], health: Any, facts: Any, pool: str) -> dict:
    return {
        "provider_id": env.split("_", 1)[0].lower(),
        "env_key": env,
        "locked": False,
        "keys": [{"index": i, "key_label": label} for i, label in enumerate(labels)],
        "rows": [
            {
                "index": i,
                "id": f"sha256:{env.lower()[:4]}{i:012d}",
                "masked": label,
                "key_label": label,
                "name": "",
                "health": None,
            }
            for i, label in enumerate(labels)
        ],
        "health": health,
        "key_health": {"pool": pool, "keys": facts},
    }


def _healthy(index: int, label: str, requests: int = 3) -> dict[str, Any]:
    return {
        "index": index,
        "key_label": label,
        "state": "healthy",
        "state_remaining_seconds": 0.0,
        "requests": requests,
        "successes": requests,
        "failures": 0,
        "consecutive_failures": 0,
        "rate_limits": 0,
        "lockouts": 0,
        "last_error": None,
    }


def _benched(index: int, label: str, state: str, remaining: float) -> dict[str, Any]:
    entry = _healthy(index, label, requests=4)
    entry.update(
        {
            "state": state,
            "state_remaining_seconds": remaining,
            "failures": 1,
            "successes": None,
            "consecutive_failures": None,
        }
    )
    return entry


def _fact(index: int, state: str, remaining: float, reason: str | None, tier: int = 0):
    return {
        "index": index,
        "state": state,
        "remaining_s": remaining,
        "reason": reason,
        "lockout_tier": tier,
        "lockout_tiers": 3,
    }


#: Every bench reason the page has words for, one key each, on the chain's
#: books (``pool: "chain"``), plus one healthy key.
WORDS_LABELS = ["exa-…0001", "exa-…0002", "exa-…0003", "exa-…0004", "exa-…0005"]
WORDS_HEALTH = {
    "policy": "failover",
    "keys": [
        _benched(0, WORDS_LABELS[0], "cooldown", 60.0),
        _benched(1, WORDS_LABELS[1], "cooldown", 30.0),
        _benched(2, WORDS_LABELS[2], "locked_out", 3600.0),
        _benched(3, WORDS_LABELS[3], "circuit_open", 45.0),
        _healthy(4, WORDS_LABELS[4]),
    ],
}
WORDS_FACTS = [
    _fact(0, "cooldown", 60.0, "rate_limited"),
    _fact(1, "cooldown", 30.0, "out_of_credits"),
    _fact(2, "locked_out", 3600.0, "rejected", tier=2),
    _fact(3, "circuit_open", 45.0, "failures"),
    _fact(4, "healthy", 0.0, None),
]


def _tries_json(probe: bool = False) -> str:
    label = PROBE if probe else "exa-…0001"
    tries = [
        {
            "key_index": 0,
            "key_label": label,
            "try": 1,
            "status": 429,
            "kind": PROBE if probe else "rate_limit",
            "retry_after": 7.0,
            "ms": 41.0,
            "slept_s": None,
            "ok": False,
            "charged": "rate_limit",
            "benched_for_s": 7.0,
            "decision": "429 with Retry-After 7",
            "rotated": True,
        },
        {
            "key_index": 1,
            "key_label": "exa-…0002",
            "try": 1,
            "status": 503,
            "kind": "upstream",
            "retry_after": None,
            "ms": 12.5,
            "slept_s": None,
            "ok": False,
        },
        {
            "key_index": 1,
            "key_label": "exa-…0002",
            "try": 2,
            "status": None,
            "kind": None,
            "retry_after": None,
            "ms": 30.0,
            "slept_s": 0.5,
            "ok": True,
        },
    ]
    return json.dumps(tries, separators=(",", ":"))


def _row(row_id: int, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": row_id,
        "ts_epoch": 1792800000.0 + row_id,
        "ts_iso": "2026-10-24T10:00:00+00:00",
        "provider": "exa",
        "key_index": 0,
        "key_label": "exa-…0001",
        "query": "fake query",
        "results_count": 3,
        "duration_ms": 120.0,
        "status": "success",
        "error_kind": None,
        "error_message": None,
        "cost_usd": None,
        "route_id": f"route{row_id:04d}aaaa",
        "attempt_number": 1,
        "input_chars": 10,
        "output_chars": 20,
        "input_sha256": None,
        "output_sha256": None,
        "content_captured": True,
    }
    row.update(extra)
    return row


def _no_chain(row_id: int, **extra: Any) -> dict[str, Any]:
    """A row a search with no chain wrote (7.112.0+): the four columns NULL."""

    return _row(
        row_id,
        chain_position=None,
        outcome=None,
        failure_kind=None,
        tries_json=None,
        **extra,
    )


def _stats() -> dict[str, Any]:
    return {
        "enabled": True,
        "capture_content": True,
        "max_content_chars": 2000000,
        "dropped_records": 0,
        "attempts": {"totals": {"requests": 2}, "by_provider": [], "by_key": []},
        "routes": {
            "totals": {"searches": 2, "errors": 0, "results": 6},
            "series": [],
            "by_terminal_provider": [],
            "top_errors": [],
            "last_route": None,
        },
        "last_route": None,
    }


def websearch_page_fixture() -> dict[str, Any]:
    """Everything the scenario serves, by name."""

    exa_unused = _keys("EXA_API_KEY", ["exa-…0001"], None, None, "default")
    exa_healthy = _keys(
        "EXA_API_KEY",
        ["exa-…0001", "exa-…0002"],
        {
            "policy": "failover",
            "keys": [_healthy(0, "exa-…0001"), _healthy(1, "exa-…0002", 1)],
        },
        [_fact(0, "healthy", 0.0, None), _fact(1, "healthy", 0.0, None)],
        "default",
    )
    words = _keys("EXA_API_KEY", WORDS_LABELS, WORDS_HEALTH, WORDS_FACTS, "chain")
    reset_after = _keys(
        "EXA_API_KEY",
        WORDS_LABELS,
        {
            "policy": "failover",
            "keys": [_healthy(0, WORDS_LABELS[0]), *WORDS_HEALTH["keys"][1:]],
        },
        [_fact(0, "healthy", 0.0, None), *WORDS_FACTS[1:]],
        "chain",
    )
    no_chain_rows = [_no_chain(2), _row(1)]
    chain_rows = [
        _row(
            6,
            provider="tavily",
            key_label="tvly-…0001",
            chain_position=2,
            outcome="succeeded",
            failure_kind=None,
            tries_json=json.dumps(
                [
                    {
                        "key_index": 0,
                        "key_label": "tvly-…0001",
                        "try": 1,
                        "status": None,
                        "kind": None,
                        "retry_after": None,
                        "ms": 80.0,
                        "slept_s": None,
                        "ok": True,
                    }
                ],
                separators=(",", ":"),
            ),
        ),
        _row(
            5,
            provider="context7",
            key_label="",
            status="skipped",
            results_count=0,
            error_message="skipped: paused",
            chain_position=1,
            outcome="skipped",
            failure_kind=None,
            tries_json=None,
        ),
        _row(
            4,
            status="error",
            error_kind="upstream",
            error_message=f"exa: {PROBE}",
            results_count=0,
            chain_position=0,
            outcome="failed",
            failure_kind="upstream",
            tries_json=_tries_json(probe=True),
        ),
        _no_chain(3),
        # Written before 7.112.0: the four columns do not exist on the row.
        _row(1),
    ]
    detail_output = {
        "provider": "context7",
        "query": "fake query",
        "answer": None,
        "results": [
            {
                "title": f"Next.js — {PROBE}",
                "url": "https://example.invalid/docs",
                "snippet": f"excerpt {PROBE}",
                "content": None,
                "published": None,
            }
        ],
        "result_count": 1,
        "key_index": 0,
        "cost_usd": None,
    }
    details = {str(row["id"]): dict(row) for row in chain_rows}
    details["4"] = {**details["4"], "output": detail_output, "input": {"query": "q"}}
    return {
        "section": _section(),
        "fields": websearch_fields(),
        "fields_chain_set": websearch_fields(
            {"WEB_SEARCH_CHAIN": "exa,context7,tavily", "WEB_SEARCH_CHAIN_PAUSED": ""}
        ),
        "fields_chain_disabled": websearch_fields(
            {"WEB_SEARCH_CHAIN": "exa,tavily", "WEB_SEARCH_PROVIDER": "disabled"}
        ),
        "manual_only": ["context7"],
        "keys": {
            "unused": exa_unused,
            "healthy": exa_healthy,
            "words": words,
            "reset_after": reset_after,
        },
        "stats": _stats(),
        "requests": {"total": len(no_chain_rows), "items": no_chain_rows},
        "requests_chain": {"total": len(chain_rows), "items": chain_rows},
        "details": details,
        "probe": PROBE,
    }


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(
        json.dumps(websearch_page_fixture(), ensure_ascii=False), encoding="utf-8"
    )
