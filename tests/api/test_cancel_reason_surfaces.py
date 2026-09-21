"""The cancelled sub-label everywhere a reader can meet it.

The list row, the request modal's payload, the Analytics breakdown, the status
filter, and every export format. One derivation behind all of them, so a
download and the page it came from cannot disagree.
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
def cancelled_store(tmp_path):
    """One of each shape, plus a request that was never cancelled at all."""

    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    # A day back, deliberately: the store registers a server session of its own
    # when it opens, and a request that happened to end within five seconds of
    # one is a restart -- correctly, but not the thing these tests are about.
    base = time.time() - 86_400

    def record(
        request_id: str,
        *,
        offset: float,
        status: RequestStatus,
        ttft_ms: float | None,
        duration_ms: float,
        output_chars: int = 0,
        thinking_chars: int = 0,
    ) -> RequestRecord:
        return RequestRecord(
            id=request_id,
            endpoint="/v1/messages",
            protocol="anthropic",
            provider="p1",
            requested_model="mcc/best",
            resolved_model="p1/fast",
            harness="claude_code",
            stream=True,
            ts_epoch=base + offset,
            status=status,
            ttft_ms=ttft_ms,
            duration_ms=duration_ms,
            output_chars=output_chars,
            thinking_chars=thinking_chars,
        )

    store.enqueue(
        record(
            "gave_up",
            offset=1,
            status="cancelled",
            ttft_ms=None,
            duration_ms=600_000.0,
        )
    )
    store.enqueue(
        record(
            "silent",
            offset=2,
            status="cancelled",
            ttft_ms=16_700.0,
            duration_ms=313_400.0,
        )
    )
    store.enqueue(
        record(
            "mid",
            offset=3,
            status="cancelled",
            ttft_ms=200.0,
            duration_ms=9_000.0,
            output_chars=140,
        )
    )
    store.enqueue(
        record(
            "fine",
            offset=4,
            status="success",
            ttft_ms=200.0,
            duration_ms=900.0,
            output_chars=900,
        )
    )
    store.close()
    yield store
    store.close()


def _export(client, **params):
    return client.get("/admin/api/export", params=params)


class TestTheApiSurfaces:
    def test_a_list_row_carries_its_reason(self, client, cancelled_store) -> None:
        rows = client.get("/admin/api/requests?limit=50").json()["rows"]
        reasons = {row["id"]: row["cancel_reason"] for row in rows}
        assert reasons == {
            "gave_up": "client_gave_up_waiting",
            "silent": "committed_then_silent",
            "mid": "stopped_mid_answer",
            # Present and empty, never absent: a successful request was not
            # cancelled for any reason.
            "fine": None,
        }

    def test_the_detail_payload_says_the_same_thing(
        self, client, cancelled_store
    ) -> None:
        row = client.get("/admin/api/requests/silent").json()
        assert row["cancel_reason"] == "committed_then_silent"
        # The modal's sentence is computed from these two, so they have to be
        # on the payload the modal reads.
        assert row["ttft_ms"] == 16_700.0
        assert row["duration_ms"] == 313_400.0

    def test_the_stats_payload_gains_the_breakdown_and_nothing_else(
        self, client, cancelled_store
    ) -> None:
        stats = client.get("/admin/api/requests/stats").json()
        assert stats["cancelled"] == 3
        breakdown = stats["cancelled_breakdown"]
        assert breakdown["total"] == 3
        assert breakdown["counts"] == {
            "server_restart": 0,
            "stopped_mid_answer": 1,
            "client_gave_up_waiting": 1,
            "committed_then_silent": 1,
        }
        # The breakdown sums to the card it sits under; if it ever did not, one
        # of the two would be wrong and the page could not say which.
        assert sum(breakdown["counts"].values()) == stats["cancelled"]


class TestTheStatusFilter:
    def test_the_three_original_values_still_mean_what_they_meant(
        self, client, cancelled_store
    ) -> None:
        for status, expected in (("success", 1), ("error", 0), ("cancelled", 3)):
            body = client.get(f"/admin/api/requests?limit=50&status={status}").json()
            assert len(body["rows"]) == expected, status

    def test_a_sub_label_narrows_to_one_kind(self, client, cancelled_store) -> None:
        body = client.get(
            "/admin/api/requests?limit=50&status=cancelled:committed_then_silent"
        ).json()
        assert [row["id"] for row in body["rows"]] == ["silent"]

    def test_an_unknown_sub_label_is_refused(self, client, cancelled_store) -> None:
        assert (
            client.get("/admin/api/requests?status=cancelled:nonsense").status_code
            == 422
        )
        assert client.get("/admin/api/requests?status=nonsense").status_code == 422

    def test_the_export_accepts_exactly_what_the_page_does(
        self, client, cancelled_store
    ) -> None:
        """Or a download would silently be a different population."""

        ok = _export(
            client,
            format="json",
            scope="requests",
            status="cancelled:stopped_mid_answer",
        )
        assert ok.status_code == 200
        assert [row["id"] for row in ok.json()] == ["mid"]
        assert (
            _export(
                client, format="json", scope="requests", status="cancelled:nonsense"
            ).status_code
            == 422
        )
        assert (
            _export(
                client, format="json", scope="attempts", status="cancelled:nonsense"
            ).status_code
            == 422
        )


class TestTheExportColumn:
    def test_the_request_export_carries_the_words_the_chip_shows(
        self, client, cancelled_store
    ) -> None:
        rows = {
            row["id"]: row
            for row in _export(client, format="json", scope="requests").json()
        }
        assert rows["silent"]["cancel_reason"] == "committed, then silent"
        assert rows["mid"]["cancel_reason"] == "stopped mid-answer"
        assert rows["gave_up"]["cancel_reason"] == "client gave up waiting"
        # Empty, not "none": a successful request was not cancelled.
        assert rows["fine"]["cancel_reason"] is None

    def test_the_extra_columns_the_derivation_needed_do_not_leak(
        self, client, cancelled_store
    ) -> None:
        """The store tops its SELECT up; the route projects the row back down."""

        row = _export(client, format="json", scope="requests").json()[0]
        assert "thinking_chars" not in row
        assert "output_chars" not in row

    def test_every_format_labels_the_column(self, client, cancelled_store) -> None:
        response = _export(client, format="csv", scope="requests")
        reader = csv.reader(io.StringIO(response.content.decode("utf-8-sig")))
        header = next(reader)
        assert "Cancelled because" in header
        column = header.index("Cancelled because")
        values = {row[header.index("ID")]: row[column] for row in reader}
        assert values["silent"] == "committed, then silent"
        assert values["fine"] == ""

        text = _export(client, format="txt", scope="requests").content.decode("utf-8")
        assert "Cancelled because" in text
        assert "committed, then silent" in text

        payload = json.loads(
            _export(client, format="json", scope="requests").content.decode("utf-8")
        )
        assert all("cancel_reason" in row for row in payload)

        # The workbook is a zip, so the header is not findable in the bytes;
        # unpacked, the shared-strings part carries it.
        xlsx = _export(client, format="xlsx", scope="requests")
        assert xlsx.status_code == 200
        with zipfile.ZipFile(io.BytesIO(xlsx.content)) as workbook:
            parts = "".join(
                workbook.read(name).decode("utf-8", "replace")
                for name in workbook.namelist()
                if name.endswith(".xml")
            )
        assert "Cancelled because" in parts
        assert "committed, then silent" in parts

    def test_the_attempts_export_carries_it_beside_request_status(
        self, client, cancelled_store
    ) -> None:
        """``request_status`` is the column that raised the question."""

        response = _export(client, format="csv", scope="attempts")
        header = next(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        assert "Request status" in header
        assert "Request cancelled because" in header
        assert header.index("Request cancelled because") == (
            header.index("Request status") + 1
        )
