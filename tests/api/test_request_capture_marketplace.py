"""E7 (7.109.0): ``params.marketplace`` on a marketplace attempt, and nowhere else."""

from pathlib import Path
from typing import Any

from my_claude_code.api.request_capture import RequestCapture
from my_claude_code.application.execution import RouteAttemptRecord
from my_claude_code.core.request_log import RequestLogStore
from my_claude_code.core.wire_capture import (
    record_marketplace_readback,
    record_wire_request,
)


def _stored(folder: Path, request_id: str, *, marketplace: bool) -> dict[str, Any]:
    store = RequestLogStore(folder / f"{request_id}.db")
    capture = RequestCapture(
        store,
        request_id=request_id,
        endpoint="/v1/messages",
        protocol="anthropic",
        stream=True,
        requested_model="claude-sonnet-4-5",
        input_text="hello",
        params={"max_tokens": 100},
    )
    record_wire_request({"model": "cc/claude-opus-4-6", "max_tokens": 100})
    if marketplace:
        record_marketplace_readback(
            headers={
                "x-inferhub-model": "cc/claude-opus-4-6",
                "x-inferhub-attempt": "1",
            }
        )
        record_marketplace_readback(charged_usd=0.000123)
    capture.record_attempt_result(
        RouteAttemptRecord(
            attempt=0,
            provider_id="inferhub" if marketplace else "bai",
            model_ref="inferhub/cc/claude-opus-4-6" if marketplace else "bai/m",
            outcome="succeeded",
        )
    )
    capture.finish_success("ok")
    store.close()
    row = store.get_request(request_id)
    assert row is not None
    (attempt,) = row["route_attempts"]
    return attempt


def test_a_marketplace_attempt_keeps_the_headers_and_the_charge(tmp_path: Path) -> None:
    attempt = _stored(tmp_path, "req_market", marketplace=True)
    assert attempt["params"]["marketplace"] == {
        "headers": {
            "x-inferhub-model": "cc/claude-opus-4-6",
            "x-inferhub-attempt": "1",
        },
        "charged_usd": 0.000123,
    }


def test_any_other_attempt_has_no_marketplace_key(tmp_path: Path) -> None:
    attempt = _stored(tmp_path, "req_other", marketplace=False)
    assert "marketplace" not in (attempt["params"] or {})


def test_nothing_reported_records_nothing() -> None:
    # Outside a tracked request the reader is a no-op, and an empty report is
    # never stored as an entry (NULL is not reported, never 0).
    record_marketplace_readback(headers={})
    record_marketplace_readback(charged_usd=None)
