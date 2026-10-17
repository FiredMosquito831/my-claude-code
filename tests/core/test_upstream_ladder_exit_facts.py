"""Exit refusal facts, derived when a row is read (7.96.0, J-1).

Two things an exit can meet that are about the *address* rather than the key
or the model: a host refusing the caller's country (``region_refused``) and an
edge putting a bot challenge in front of the host (``challenged``). Both are
counted from the tries the ladder already stores -- status, the redacted body,
the response head -- so nothing new is written and every attempt logged before
this release shows them too.

Held here:

- **The words.** The country words are the ones the live pool reads
  (``providers/recovery/surface.py``), pinned together; a challenge is
  Cloudflare's ``cf-mitigated`` / "Just a moment..." / ``challenge-platform``,
  in the body or in a recorded response head.
- **Absent when zero.** ``exit_facts`` answers ``{}`` for an attempt that met
  neither, ``ladder_payload`` -- what is stored -- never carries them, and
  every read of a row without them is byte-identical to before.
- **Where they show.** The request detail's attempt ladder
  (``exit_facts``), the Requests table's Exit cell (``exit.facts``) and the
  opt-in Exit export group (two count columns).
"""

import json
from typing import Any

import pytest

from my_claude_code.core import upstream_ladder
from my_claude_code.core.export import (
    request_detail_columns,
    request_detail_derived_columns,
    request_detail_headers,
)
from my_claude_code.core.request_log import (
    RequestLogStore,
    RequestRecord,
    RouteAttempt,
    RouteAttemptOutcome,
)
from my_claude_code.core.upstream_ladder import (
    _LADDER,
    add_exit_facts,
    exit_facts,
    install_ladder_trace,
    ladder_payload,
    note_response_head,
    record_upstream_try,
)
from my_claude_code.providers.recovery import surface

BASE_TS = 1_790_000_000.0

#: What OpenCode Zen sends, byte for byte.
REGION_BODY = (
    '{"type":"error","error":{"type":"RegionError",'
    '"message":"This model is not available in your country."}}'
)
FREE_429_BODY = (
    '{"type":"error","error":{"type":"FreeUsageLimitError",'
    '"message":"Rate limit exceeded. Please try again later."}}'
)
CHALLENGE_HTML = (
    '<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title>'
    '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1">'
    "</script></head><body></body></html>"
)


def _ladder(*tries: dict[str, Any]) -> dict[str, Any]:
    """Record ``tries`` through the real recorder and return the stored payload."""
    install_ladder_trace()
    try:
        for entry in tries:
            head = entry.pop("head", None)
            if head is not None:
                note_response_head(head)
            record_upstream_try(**entry)
        trace = _LADDER.get()
        assert trace is not None
        return ladder_payload(trace.slot())
    finally:
        _LADDER.set(None)


def _try(status: int | None, body: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"key_index": 0, "status": status, "body": body, **extra}


class TestTheWords:
    def test_the_country_words_are_the_live_pools_words(self) -> None:
        """``core`` cannot import ``providers``: the mirror is pinned here."""
        assert upstream_ladder._REGION_WORDS.pattern == surface._REGION_PATTERN.pattern
        assert upstream_ladder._REGION_WORDS.flags == surface._REGION_PATTERN.flags

    def test_a_zen_country_refusal_is_counted(self) -> None:
        payload = _ladder(_try(403, REGION_BODY, proxy="203.0.113.7:1080"))
        assert exit_facts(payload["tries"]) == {"region_refused": 1}

    def test_each_refusing_try_counts_once(self) -> None:
        payload = _ladder(
            _try(403, REGION_BODY, proxy="exit-a.example:1080"),
            _try(429, FREE_429_BODY, proxy="exit-b.example:1080"),
            _try(403, REGION_BODY, proxy="exit-c.example:1080"),
            _try(200, None, proxy="exit-d.example:1080"),
        )
        assert exit_facts(payload["tries"]) == {"region_refused": 2}

    @pytest.mark.parametrize("status", [200, 401, 429, 500, None])
    def test_the_words_on_any_other_status_are_not_a_country_refusal(
        self, status: int | None
    ) -> None:
        payload = _ladder(_try(status, REGION_BODY))
        assert exit_facts(payload["tries"]) == {}

    @pytest.mark.parametrize("status", [403, 503])
    def test_a_challenge_page_in_the_body_is_counted(self, status: int) -> None:
        payload = _ladder(_try(status, CHALLENGE_HTML))
        assert exit_facts(payload["tries"]) == {"challenged": 1}

    def test_a_challenge_seen_only_in_the_response_head_is_counted(self) -> None:
        """An undecodable body keeps its first bytes as hex on the head."""
        head = {
            "status": 403,
            "content_type": "text/html; charset=UTF-8",
            "server": "cloudflare",
            "body_head_hex": CHALLENGE_HTML.encode()[:128].hex(),
        }
        payload = _ladder(_try(403, None, head=head))
        assert payload["tries"][0]["response_head"] == head
        assert exit_facts(payload["tries"]) == {"challenged": 1}

    def test_a_plain_forbidden_or_a_cloudflare_403_alone_is_neither(self) -> None:
        head = {"status": 403, "server": "cloudflare", "body_head_hex": "7b7d"}
        payload = _ladder(
            _try(403, '{"error":{"message":"Forbidden"}}'),
            _try(403, None, head=head),
        )
        assert exit_facts(payload["tries"]) == {}

    def test_waits_and_benches_are_not_tries(self) -> None:
        tries = [
            {"source": "backoff", "status": 403, "body": REGION_BODY},
            {"source": "bench", "status": 403, "body": REGION_BODY},
        ]
        assert exit_facts(tries) == {}

    @pytest.mark.parametrize("value", [None, "tries", {}, [None, 3, "x"]])
    def test_anything_unreadable_is_nothing(self, value: Any) -> None:
        assert exit_facts(value) == {}


class TestAbsentWhenZero:
    def test_the_stored_payload_never_carries_the_facts(self) -> None:
        """Derived when read, never written: what is stored is unchanged."""
        payload = _ladder(_try(403, REGION_BODY), _try(403, CHALLENGE_HTML))
        assert "exit_facts" not in payload
        assert "exit_facts" not in json.dumps(payload)

    def test_reading_adds_them_only_where_there_are_some(self) -> None:
        refused = {"ladder": _ladder(_try(403, REGION_BODY))}
        plain = {"ladder": _ladder(_try(429, FREE_429_BODY), _try(200))}
        before = json.dumps(plain, sort_keys=True)

        add_exit_facts(refused)
        add_exit_facts(plain)

        assert refused["ladder"]["exit_facts"] == {"region_refused": 1}
        assert json.dumps(plain, sort_keys=True) == before

    @pytest.mark.parametrize("params", [None, {}, {"ladder": None}, {"ladder": []}, []])
    def test_reading_tolerates_a_row_with_no_ladder(self, params: Any) -> None:
        before = json.dumps(params)
        add_exit_facts(params)
        assert json.dumps(params) == before


# ------------------------------------------------------------- the store --


def _attempt(
    index: int,
    params: dict[str, Any] | None,
    *,
    label: str | None = None,
    outcome: RouteAttemptOutcome = RouteAttemptOutcome.FAILED,
) -> RouteAttempt:
    return RouteAttempt(
        attempt=index,
        provider="opencode",
        model_ref=f"opencode/model-{index}",
        outcome=outcome,
        params=params,
        proxy_label=label,
    )


def _record(request_id: str, offset: int, *attempts: RouteAttempt) -> RequestRecord:
    return RequestRecord(
        id=request_id,
        ts_epoch=BASE_TS + offset,
        endpoint="/v1/messages",
        protocol="anthropic",
        requested_model="claude-sonnet-4-5",
        provider="opencode",
        resolved_model="model",
        harness="claude",
        stream=True,
        tokens_in=10,
        tokens_out=20,
        duration_ms=100.0,
        ttft_ms=30.0,
        status="success",
        route_attempt=attempts[-1].attempt if attempts else None,
        attempts=attempts,
    )


@pytest.fixture
def store(tmp_path):
    store = RequestLogStore(tmp_path / "requests.db", max_rows=10_000)
    refused = {"ladder": _ladder(_try(403, REGION_BODY, proxy="203.0.113.7:1080"))}
    rotated = {
        "ladder": _ladder(
            _try(403, REGION_BODY, proxy="exit-a.example:1080"),
            _try(403, CHALLENGE_HTML, proxy="exit-b.example:1080"),
            _try(200, None, proxy="exit-c.example:1080"),
        )
    }
    plain = {"ladder": _ladder(_try(429, FREE_429_BODY), _try(200))}
    for record in (
        # The refused attempt, then the next model answering with no chain.
        _record(
            "refused",
            0,
            _attempt(0, refused, label="203.0.113.7:1080"),
            _attempt(1, plain, outcome=RouteAttemptOutcome.SUCCEEDED),
        ),
        _record(
            "rotated",
            1,
            _attempt(
                0,
                rotated,
                label="exit-c.example:1080",
                outcome=RouteAttemptOutcome.SUCCEEDED,
            ),
        ),
        _record("plain", 2, _attempt(0, plain, outcome=RouteAttemptOutcome.SUCCEEDED)),
        # No ladder stored at all: not measured.
        _record("bare", 3, _attempt(0, None, outcome=RouteAttemptOutcome.SUCCEEDED)),
    ):
        store.enqueue(record)
    store.close()
    yield store
    store.close()


def _stored_params(store: RequestLogStore, request_id: str) -> list[Any]:
    with store._connection() as conn:
        rows = conn.execute(
            "SELECT params FROM request_attempts WHERE request_id = ? ORDER BY attempt",
            (request_id,),
        ).fetchall()
    return [None if row[0] is None else json.loads(row[0]) for row in rows]


class TestTheRequestDetail:
    def test_the_refusing_attempt_carries_its_facts(self, store) -> None:
        attempts = store.get_request("refused")["route_attempts"]
        assert attempts[0]["params"]["ladder"]["exit_facts"] == {"region_refused": 1}
        assert "exit_facts" not in attempts[1]["params"]["ladder"]

    def test_a_rotated_attempt_counts_every_exit(self, store) -> None:
        (attempt,) = store.get_request("rotated")["route_attempts"]
        assert attempt["params"]["ladder"]["exit_facts"] == {
            "region_refused": 1,
            "challenged": 1,
        }

    @pytest.mark.parametrize("request_id", ["plain", "bare"])
    def test_a_row_with_no_fact_reads_exactly_as_stored(
        self, store, request_id: str
    ) -> None:
        attempts = store.get_request(request_id)["route_attempts"]
        assert [attempt["params"] for attempt in attempts] == _stored_params(
            store, request_id
        )

    def test_nothing_is_written_back(self, store) -> None:
        store.get_request("refused")
        assert all(
            params is None or "exit_facts" not in params.get("ladder", {})
            for params in _stored_params(store, "refused")
        )


class TestTheExitCell:
    def _exits(self, store) -> dict[str, dict[str, Any]]:
        rows, _total, _more = store.list_requests_page(limit=50, include_exits=True)
        return {row["id"]: row["exit"] for row in rows}

    def test_facts_ride_on_the_rows_that_met_them(self, store) -> None:
        exits = self._exits(store)
        assert exits["refused"]["facts"] == {"region_refused": 1}
        assert exits["rotated"]["facts"] == {"region_refused": 1, "challenged": 1}

    def test_every_other_row_is_what_7_88_0_returned(self, store) -> None:
        exits = self._exits(store)
        assert exits["plain"] == {"label": None, "tried": []}
        assert exits["bare"] == {"label": None, "tried": []}
        assert set(exits["refused"]) == {"label", "tried", "facts"}


class TestTheExport:
    def test_two_counts_join_the_opt_in_exit_group(self) -> None:
        derived = request_detail_derived_columns(["exit"])
        assert derived[-2:] == ["exit_region_refused", "exit_challenged"]
        assert request_detail_headers(derived[-2:]) == ["Country refused", "Challenged"]
        assert "exit_region_refused" not in request_detail_derived_columns([])

    def test_counts_where_a_ladder_was_stored_and_empty_where_none(self, store) -> None:
        rows = {
            row["id"]: row
            for row in store.iter_export_rows(
                columns=request_detail_columns(["exit"]),
                need_bodies=False,
                need_exits=True,
            )
        }
        assert rows["refused"]["exit_region_refused"] == 1
        assert rows["refused"]["exit_challenged"] == 0
        assert rows["rotated"]["exit_region_refused"] == 1
        assert rows["rotated"]["exit_challenged"] == 1
        assert rows["plain"]["exit_region_refused"] == 0
        assert rows["plain"]["exit_challenged"] == 0
        assert rows["bare"]["exit_region_refused"] is None
        assert rows["bare"]["exit_challenged"] is None

    def test_an_export_that_does_not_ask_has_no_such_column(self, store) -> None:
        rows = list(
            store.iter_export_rows(
                columns=request_detail_columns([]), need_bodies=False
            )
        )
        assert rows
        assert all("exit_region_refused" not in row for row in rows)
