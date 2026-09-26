"""The success sub-label everywhere a reader can meet it.

The list row, the request modal's payload, the no-answer breakdown route, the
status filter, and every export format -- the same surfaces the cancelled
sub-label reaches, from one derivation, so a download and the page it came
from cannot disagree.
"""

import csv
import io
import json
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from my_claude_code.core.request_log import (
    RequestRecord,
    RequestStatus,
    get_request_log_store,
)
from tests.api.support import create_test_app


@pytest.fixture
def client():
    return TestClient(create_test_app(), client=("127.0.0.1", 50000))


@pytest.fixture
def success_store(tmp_path):
    """Each shape once: thought-only, empty, answered, local, cancelled."""

    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    # A day back, for the cancelled fixture's reason: a request that ended
    # within five seconds of the store's own session start is a restart.
    base = time.time() - 86_400

    def record(
        request_id: str,
        *,
        offset: float,
        status: RequestStatus = "success",
        output_chars: int = 0,
        thinking_chars: int = 0,
        tool_call_count: int | None = None,
        tokens_out: int | None = 0,
        provider: str | None = "custom_agnes",
        optimization: str | None = None,
    ) -> RequestRecord:
        return RequestRecord(
            id=request_id,
            endpoint="/v1/messages",
            protocol="anthropic",
            provider=provider,
            requested_model="mcc/best",
            resolved_model="custom_agnes/agnes-3.0-flash" if provider else None,
            harness="claude_code",
            stream=True,
            ts_epoch=base + offset,
            status=status,
            ttft_ms=300.0,
            duration_ms=185_000.0,
            output_chars=output_chars,
            thinking_chars=thinking_chars,
            tool_call_count=tool_call_count,
            tokens_out=tokens_out,
            optimization=optimization,
        )

    store.enqueue(record("thought", offset=1, thinking_chars=18_800, tokens_out=4_200))
    store.enqueue(record("empty", offset=2))
    store.enqueue(record("fine", offset=3, output_chars=900, tokens_out=300))
    store.enqueue(record("tools", offset=4, tool_call_count=2, tokens_out=90))
    store.enqueue(
        record("local", offset=5, provider=None, optimization="suggestion_mode_skip")
    )
    store.enqueue(record("cut", offset=6, status="cancelled", output_chars=140))
    store.close()
    yield store
    store.close()


def _export(client, **params):
    return client.get("/admin/api/export", params=params)


class TestTheApiSurfaces:
    def test_a_list_row_carries_its_label(self, client, success_store) -> None:
        rows = client.get("/admin/api/requests?limit=50").json()["rows"]
        assert {row["id"]: row["success_reason"] for row in rows} == {
            "thought": "thought_only",
            "empty": "empty",
            "fine": None,
            "tools": None,
            # MCC's own local answer is empty on purpose: no label.
            "local": None,
            "cut": None,
        }
        # And the cancelled label is exactly what it was.
        assert {row["id"]: row["cancel_reason"] for row in rows}["cut"] == (
            "stopped_mid_answer"
        )

    def test_the_detail_payload_says_the_same_thing(
        self, client, success_store
    ) -> None:
        assert client.get("/admin/api/requests/thought").json()["success_reason"] == (
            "thought_only"
        )
        assert client.get("/admin/api/requests/fine").json()["success_reason"] is None

    def test_the_breakdown_route_sums_to_what_it_claims(
        self, client, success_store
    ) -> None:
        body = client.get("/admin/api/requests/no-answer").json()
        assert body == {
            "enabled": True,
            "successes": 5,
            "total": 2,
            "counts": {"thought_only": 1, "empty": 1},
            "selected": None,
        }
        hidden = client.get("/admin/api/requests/no-answer?local=hide").json()
        assert (hidden["successes"], hidden["total"]) == (4, 2)
        assert (
            client.get("/admin/api/requests/no-answer?status=nonsense").status_code
            == 422
        )

    def test_the_stats_payload_is_unchanged(self, client, success_store) -> None:
        """No new key on stats and no fourth status: the breakdown has its route."""

        stats = client.get("/admin/api/requests/stats").json()
        assert "no_answer_breakdown" not in stats
        assert (stats["success"], stats["cancelled"], stats["error"]) == (5, 1, 0)
        assert stats["cancelled_breakdown"]["total"] == 1


class TestTheStatusFilter:
    def test_every_old_value_still_means_what_it_meant(
        self, client, success_store
    ) -> None:
        for status, expected in (("success", 5), ("error", 0), ("cancelled", 1)):
            body = client.get(f"/admin/api/requests?limit=50&status={status}").json()
            assert len(body["rows"]) == expected, status
        body = client.get(
            "/admin/api/requests?limit=50&status=cancelled:stopped_mid_answer"
        ).json()
        assert [row["id"] for row in body["rows"]] == ["cut"]

    def test_a_success_sub_label_narrows_to_one_kind(
        self, client, success_store
    ) -> None:
        for status, expected in (
            ("success:thought_only", ["thought"]),
            ("success:empty", ["empty"]),
        ):
            body = client.get(f"/admin/api/requests?limit=50&status={status}").json()
            assert [row["id"] for row in body["rows"]] == expected, status
            count = client.get(f"/admin/api/requests/count?status={status}").json()
            assert count["total"] == len(expected)

    def test_an_unknown_success_sub_label_is_refused(
        self, client, success_store
    ) -> None:
        for status in ("success:nonsense", "success:", "cancelled:thought_only"):
            assert (
                client.get(f"/admin/api/requests?status={status}").status_code == 422
            ), status

    def test_the_export_accepts_exactly_what_the_page_does(
        self, client, success_store
    ) -> None:
        ok = _export(client, format="json", scope="requests", status="success:empty")
        assert ok.status_code == 200
        assert [row["id"] for row in ok.json()] == ["empty"]
        for scope in ("requests", "attempts"):
            assert (
                _export(
                    client, format="json", scope=scope, status="success:nonsense"
                ).status_code
                == 422
            )
        assert (
            _export(
                client, format="json", scope="attempts", status="success:thought_only"
            ).status_code
            == 200
        )


class TestTheExportColumn:
    def test_the_request_export_carries_the_words_the_chip_shows(
        self, client, success_store
    ) -> None:
        rows = {
            row["id"]: row
            for row in _export(client, format="json", scope="requests").json()
        }
        assert rows["thought"]["success_reason"] == "thought only"
        assert rows["empty"]["success_reason"] == "empty"
        assert rows["fine"]["success_reason"] is None
        assert rows["local"]["success_reason"] is None
        assert rows["cut"]["success_reason"] is None
        assert rows["cut"]["cancel_reason"] == "stopped mid-answer"
        # The columns the label was derived from do not leak into the file.
        assert "optimization" not in rows["thought"]
        assert "tool_call_count" not in rows["thought"]

    def test_every_format_labels_the_column(self, client, success_store) -> None:
        response = _export(client, format="csv", scope="requests")
        reader = csv.reader(io.StringIO(response.content.decode("utf-8-sig")))
        header = next(reader)
        assert header.index("No answer") == header.index("Cancelled because") + 1
        column = header.index("No answer")
        values = {row[header.index("ID")]: row[column] for row in reader}
        assert values["thought"] == "thought only"
        assert values["empty"] == "empty"
        assert values["fine"] == ""

        text = _export(client, format="txt", scope="requests").content.decode("utf-8")
        assert "No answer" in text
        assert "thought only" in text

        payload = json.loads(
            _export(client, format="json", scope="requests").content.decode("utf-8")
        )
        assert all("success_reason" in row for row in payload)

        xlsx = _export(client, format="xlsx", scope="requests")
        assert xlsx.status_code == 200
        with zipfile.ZipFile(io.BytesIO(xlsx.content)) as workbook:
            parts = "".join(
                workbook.read(name).decode("utf-8", "replace")
                for name in workbook.namelist()
                if name.endswith(".xml")
            )
        assert "No answer" in parts
        assert "thought only" in parts

    def test_the_attempts_export_carries_it_beside_the_cancelled_one(
        self, client, success_store
    ) -> None:
        response = _export(client, format="csv", scope="attempts")
        header = next(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        assert header.index("Request cancelled because") == (
            header.index("Request status") + 1
        )
        assert header.index("Request no answer") == (
            header.index("Request cancelled because") + 1
        )
