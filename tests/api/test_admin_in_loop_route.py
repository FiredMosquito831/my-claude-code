"""The Models page's in-loop counter endpoint (7.115.1).

The page keeps its own payload out of this on purpose: the count moves with
every logged request, and the Models page payload is digested against a pinned
equality baseline. So it is fetched beside the page, like the latency rollup,
and this file holds the route's contract: the derivation is the store's, the
payload carries its own denominators, a disabled log answers empty rather than
500, and the detail route does not swallow the path.
"""

import time

import pytest
from fastapi.testclient import TestClient

from my_claude_code.core.request_log import RequestRecord, get_request_log_store
from tests.api.support import create_test_app


@pytest.fixture
def client():
    return TestClient(create_test_app(), client=("127.0.0.1", 50000))


def _turn(
    request_id: str,
    at: float,
    *,
    session: str | None = "s1",
    agent: str | None = None,
    tool_calls: int | None = None,
    text_chars: int | None = 100,
    model: str = "m1",
) -> RequestRecord:
    return RequestRecord(
        id=request_id,
        endpoint="/v1/messages",
        protocol="anthropic",
        provider="p1",
        resolved_model=model,
        ts_epoch=at,
        status="success",
        tool_call_count=tool_calls,
        output_chars=text_chars,
        session_id=session,
        agent_id=agent,
    )


@pytest.fixture
def in_loop_store(tmp_path):
    """Two models: a stopper with one stop out of two, and a quiet one."""

    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    base = time.time() - 600
    store.enqueue(_turn("a_one", base, tool_calls=1, text_chars=0, model="stopper"))
    store.enqueue(
        _turn("a_two", base + 10, tool_calls=None, text_chars=120, model="stopper")
    )
    store.enqueue(_turn("b_one", base + 20, tool_calls=1, text_chars=0, model="quiet"))
    store.enqueue(_turn("b_two", base + 30, tool_calls=2, text_chars=8, model="quiet"))
    store.close()
    yield store


def test_the_route_answers_the_derivation_with_its_denominators(
    client, in_loop_store
) -> None:
    payload = client.get("/admin/api/requests/in-loop").json()

    assert payload["enabled"] is True
    assert payload["models"] == 2
    assert payload["in_loop_turns"] == 2
    assert payload["text_only_turns"] == 1
    assert payload["unmeasured_turns"] == 0
    rows = {row["model_ref"]: row for row in payload["rows"]}
    assert rows["p1/stopper"] == {
        "model_ref": "p1/stopper",
        "in_loop_turns": 1,
        "text_only_turns": 1,
        "unmeasured_turns": 0,
    }
    assert rows["p1/quiet"]["text_only_turns"] == 0
    assert payload["days"] == 7
    assert payload["window_since"] is not None


def test_a_window_with_nothing_in_it_answers_empty_not_error(
    client, in_loop_store
) -> None:
    future = client.get(
        "/admin/api/requests/in-loop", params={"since": time.time() + 3600}
    ).json()

    assert future["enabled"] is True
    assert future["rows"] == []
    assert future["in_loop_turns"] == 0
    assert future["models"] == 0


def test_a_days_window_is_bounded_and_echoed(client, in_loop_store) -> None:
    """A hand-typed window is clamped the way its siblings clamp it."""

    wide = client.get("/admin/api/requests/in-loop", params={"days": 100000}).json()
    assert wide["days"] == 90
    narrow = client.get("/admin/api/requests/in-loop", params={"days": 0}).json()
    assert narrow["days"] == 1


def test_the_detail_route_does_not_swallow_the_path(client, in_loop_store) -> None:
    """FastAPI matches in declaration order; "in-loop" is not a request id."""

    response = client.get("/admin/api/requests/in-loop")
    assert response.status_code == 200
    assert "rows" in response.json()


def test_it_is_loopback_only(in_loop_store) -> None:
    remote = TestClient(create_test_app(), client=("203.0.113.10", 50000))
    assert remote.get("/admin/api/requests/in-loop").status_code == 403


def test_a_disabled_log_answers_empty_rather_than_failing(
    tmp_path, monkeypatch
) -> None:
    """With request logging off the chip is absent, not the page broken."""

    monkeypatch.setenv("REQUEST_LOG_ENABLED", "false")
    from my_claude_code.config.settings import Settings

    app = create_test_app(Settings())
    disabled_client = TestClient(app, client=("127.0.0.1", 50000))
    payload = disabled_client.get("/admin/api/requests/in-loop").json()

    assert payload["enabled"] is False
    assert payload["rows"] == []
    assert payload["in_loop_turns"] == 0
    assert payload["text_only_turns"] == 0
    assert payload["window_since"] is not None
