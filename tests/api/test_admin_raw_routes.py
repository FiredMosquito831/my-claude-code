"""The raw-diagnostics read route and both exports (7.121.0).

``GET /admin/api/requests/raw`` and ``/raw/export`` serve verbatim log lines,
which can hold anything a user pasted into a prompt -- so the guard, the
content type and what never appears in an answer are tested here as much as
the lines themselves. The exports are held to the viewer: one reader (pinned
by a spy), the same bytes, the same masking (none), the same sentences.

Every log line and every key in this file is made up.
"""

import asyncio
import csv
import io
import json
import os
import re
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api import raw_diagnostics as rd
from my_claude_code.config.logging_config import RAW_DIAGNOSTICS_LOG_FILENAME
from my_claude_code.config.settings import Settings
from my_claude_code.core import export as export_engine
from my_claude_code.core.request_log import RequestRecord, get_request_log_store
from tests.api.support import create_test_app

RID = "req_" + "1" * 32
OTHER = "req_" + "2" * 32
#: Credential-shaped, entirely fake: raw lines are never masked, so whatever a
#: user pasted is in them -- the viewer and the export must agree on it byte
#: for byte (spec Q14).
PASTED = "Authorization: Bearer FAKE-TOKEN-0000-not-a-real-credential"
FAKE_KEY = "nvapi-FAKE0000000000000000000000000000000000000000000000000000000"


def _sink(marker: str, request_id: str, message: str) -> bytes:
    text = f"{marker} {request_id} {message}"
    return text.replace("\r", "\\r").replace("\n", "\\n").encode("utf-8")


def _payload_line(request_id: str, payload: dict[str, Any]) -> bytes:
    return _sink("RAW_PAYLOAD", request_id, f"FULL_PAYLOAD [{request_id}]: {payload!r}")


def _sse_line(request_id: str, text: str) -> bytes:
    frame = f'event: content_block_delta\ndata: {{"delta": {{"text": "{text}"}}}}'
    return _sink("RAW_SSE", request_id, f"SSE_EVENT: content_block_delta - {frame}")


@pytest.fixture
def logs(tmp_path: Path) -> Path:
    folder = tmp_path / "logs"
    folder.mkdir()
    lines = [
        _payload_line(
            RID, {"model": "fake", "messages": [{"role": "user", "content": PASTED}]}
        ),
        *[_sse_line(RID, f"<thinking>{n}</thinking>") for n in range(5)],
        _sse_line(OTHER, "someone else"),
    ]
    path = folder / RAW_DIAGNOSTICS_LOG_FILENAME
    path.write_bytes(b"".join(line + b"\r\n" for line in lines))
    now = time.time()
    os.utime(path, (now, now))
    error = {
        "time": "2026-10-30 10:00:00+03:00",
        "level": "ERROR",
        "message": f"Messages request failed request_id={RID} exc_type=ValueError",
        "module": "m",
        "function": "f",
        "line": 1,
        "request_id": RID,
    }
    (folder / "server.log").write_text(json.dumps(error) + "\n", encoding="utf-8")
    return folder


@pytest.fixture
def sources(logs: Path, monkeypatch) -> rd.RawSources:
    found = rd.RawSources(
        raw_log=logs / RAW_DIAGNOSTICS_LOG_FILENAME, server_log=logs / "server.log"
    )
    monkeypatch.setattr(rd, "current_sources", lambda: found)
    return found


@pytest.fixture
def store(tmp_path: Path):
    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    base = time.time() - 50
    for index, request_id in enumerate((RID, OTHER)):
        store.enqueue(
            RequestRecord(
                id=request_id,
                endpoint="/v1/messages",
                protocol="anthropic",
                provider="p1",
                resolved_model="m1",
                ts_epoch=base + index,
                status="error" if request_id == RID else "success",
                error_kind="api_error" if request_id == RID else None,
                error_message="boom" if request_id == RID else None,
                output_text="the stored answer",
                input_text="the stored prompt",
            )
        )
    store.close()
    yield store


def _settings(**env: str) -> Settings:
    values = {
        "LOG_RAW_SSE_EVENTS": "true",
        "LOG_RAW_API_PAYLOADS": "true",
        "LOG_API_ERROR_TRACEBACKS": "false",
        "LOG_RAW_CLI_DIAGNOSTICS": "false",
        "NVIDIA_NIM_API_KEY": FAKE_KEY,
    }
    values.update(env)
    return Settings.model_validate(values)


@pytest.fixture
def client(sources, store):
    return TestClient(create_test_app(_settings()), client=("127.0.0.1", 50000))


def _raw(client: TestClient, **params: Any):
    return client.get("/admin/api/requests/raw", params={"id": RID, **params})


# ---------------------------------------------------------------------------
# the guard, the shape, the route order
# ---------------------------------------------------------------------------


def test_a_non_loopback_client_and_a_foreign_origin_are_refused(client) -> None:
    remote = TestClient(client.app, client=("203.0.113.10", 50000))
    assert remote.get("/admin/api/requests/raw", params={"id": RID}).status_code == 403
    foreign = client.get(
        "/admin/api/requests/raw",
        params={"id": RID},
        headers={"Origin": "https://attacker.example"},
    )
    assert foreign.status_code == 403
    export = client.get(
        "/admin/api/requests/raw/export",
        params={"id": RID, "format": "json"},
        headers={"Origin": "http://evil.example"},
    )
    assert export.status_code == 403
    local = client.get(
        "/admin/api/requests/raw",
        params={"id": RID},
        headers={"Origin": "http://127.0.0.1:8082"},
    )
    assert local.status_code == 200


def test_the_route_is_json_no_store_and_not_eaten_by_the_id_route(client) -> None:
    response = _raw(client)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    # The {request_id} route would have answered 404 "not found" for "raw".
    assert [kind["kind"] for kind in body["kinds"]] == list(rd.RAW_KINDS)
    by_kind = {kind["kind"]: kind for kind in body["kinds"]}
    assert by_kind["payloads"]["lines_total"] == 1
    assert by_kind["sse"]["lines_total"] == 5
    assert by_kind["traceback"]["capture"] == rd.CAPTURE_PRESENT
    assert by_kind["cli"]["capture"] == rd.CAPTURE_SETTING


def test_bad_parameters_are_400s(client) -> None:
    assert _raw(client, kind="everything").status_code == 400
    assert _raw(client, view="pretty").status_code == 400
    assert _raw(client, view="normalized").status_code == 400  # needs a kind
    assert _raw(client, kind="sse", start=-1).status_code == 422


def test_no_secret_and_no_path_leaves_in_any_answer(client, logs) -> None:
    answers = [
        _raw(client).text,
        _raw(client, kind="sse").text,
        _raw(client, kind="payloads").text,
        _raw(client, kind="sse", view="normalized").text,
    ]
    for text in answers:
        assert FAKE_KEY not in text
        assert str(logs) not in text and logs.as_posix() not in text
    # The pasted line itself is returned verbatim -- raw lines are never masked.
    assert PASTED in json.loads(answers[2])["lines"][0]["text"]


def test_every_read_runs_off_the_event_loop(client, monkeypatch) -> None:
    seen: list[bool] = []
    real_summary, real_page = rd.raw_summary, rd.raw_page

    def on_loop() -> bool:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        return True

    def summary(*args, **kwargs):
        seen.append(on_loop())
        return real_summary(*args, **kwargs)

    def page(*args, **kwargs):
        seen.append(on_loop())
        return real_page(*args, **kwargs)

    monkeypatch.setattr(rd, "raw_summary", summary)
    monkeypatch.setattr(rd, "raw_page", page)
    assert _raw(client).status_code == 200
    assert _raw(client, kind="sse").status_code == 200
    assert seen == [False, False]


# ---------------------------------------------------------------------------
# Raw and Normalized each read their own source
# ---------------------------------------------------------------------------


def test_raw_reads_the_log_and_normalized_reads_the_row(client) -> None:
    raw = _raw(client, kind="sse").json()
    normalized = _raw(client, kind="sse", view="normalized").json()
    assert all("<thinking>" in line["text"] for line in raw["lines"])
    assert "the stored answer" not in json.dumps(raw)
    assert "the stored answer" in normalized["text"]
    assert "<thinking>" not in normalized["text"]


# ---------------------------------------------------------------------------
# the per-request export: four formats, one reader, the viewer's bytes
# ---------------------------------------------------------------------------


def _export(client: TestClient, **params: Any):
    return client.get("/admin/api/requests/raw/export", params={"id": RID, **params})


def test_the_json_export_holds_every_line_and_block(client) -> None:
    response = _export(client, format="json")
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith(
        f'attachment; filename="mcc-request-{RID}-json-'
    )
    rows = response.json()
    raw_sse = [
        row
        for row in rows
        if row["kind"] == "sse" and row["view"] == "raw" and row["state"] == "present"
    ]
    viewer = _raw(client, kind="sse").json()["lines"]
    assert [row["text"] for row in raw_sse] == [line["text"] for line in viewer]
    assert [row["n"] for row in raw_sse] == [1, 2, 3, 4, 5]
    normalized = [
        row for row in rows if row["view"] == "normalized" and row["kind"] == "sse"
    ]
    assert any(row["text"] == "the stored answer" for row in normalized)
    cli = [row for row in rows if row["kind"] == "cli" and row["view"] == "raw"]
    assert cli[0]["state"] == rd.CAPTURE_SETTING
    assert "LOG_RAW_CLI_DIAGNOSTICS" in cli[0]["text"]


def test_masking_is_identical_in_the_viewer_and_every_export_format(client) -> None:
    viewer = _raw(client, kind="payloads").json()["lines"][0]["text"]
    assert PASTED in viewer
    exported = {
        fmt: _export(client, format=fmt, kinds="payloads", view="raw")
        for fmt in ("json", "csv", "txt")
    }
    assert all(response.status_code == 200 for response in exported.values())
    json_rows = exported["json"].json()
    assert [row["text"] for row in json_rows if row["state"] == "present"] == [viewer]
    csv_rows = list(
        csv.DictReader(io.StringIO(exported["csv"].content.decode("utf-8-sig")))
    )
    assert [row["Text"] for row in csv_rows if row["State"] == "present"] == [viewer]
    assert viewer in exported["txt"].content.decode("utf-8")


def test_the_txt_export_streams_the_same_bytes_the_buffered_report_would(
    client,
) -> None:
    response = _export(client, format="txt")
    assert response.status_code == 200
    rows = _export(client, format="json").json()
    expected = b"".join(
        export_engine.render_txt(
            rows,
            rd.EXPORT_COLUMNS,
            rd.EXPORT_HEADERS,
            title="Export x",
            summary="Exported y",
        )
    )
    streamed = response.content
    # The first two lines are the title and the time; everything after them is
    # the table, which must be byte-identical to the buffered renderer's.
    assert streamed.split(b"\n", 2)[2] == expected.split(b"\n", 2)[2]


def test_the_xlsx_export_reads_back_and_refuses_a_cell_excel_would_cut(
    client, monkeypatch
) -> None:
    import openpyxl

    response = _export(client, format="xlsx", kinds="sse", view="raw")
    assert response.status_code == 200
    sheet = openpyxl.load_workbook(io.BytesIO(response.content)).active
    assert sheet is not None
    values = list(sheet.iter_rows(values_only=True))
    assert list(values[0]) == rd.EXPORT_HEADERS
    assert sum(1 for row in values[1:] if row[3] == "present") == 5
    monkeypatch.setattr(rd, "XLSX_CELL_LIMIT", 20)
    refused = _export(client, format="xlsx", kinds="sse", view="raw")
    assert refused.status_code == 422
    assert "Excel keeps at most 20 characters in a cell" in refused.json()["detail"]
    assert "JSON, CSV or TXT" in refused.json()["detail"]


def test_an_export_with_a_line_too_large_refuses_instead_of_writing_without_it(
    client, monkeypatch
) -> None:
    monkeypatch.setattr(rd, "LINE_LIMIT_BYTES", 50)
    response = _export(client, format="json", kinds="payloads", view="raw")
    assert response.status_code == 422
    assert "no file was written rather than one without it" in response.json()["detail"]


def test_export_parameters_are_validated(client) -> None:
    assert _export(client, format="pdf").status_code == 400
    assert _export(client, format="json", kinds="sse,bogus").status_code == 400
    assert _export(client, format="json", kinds=" , ").status_code == 400
    assert _export(client, format="json", view="half").status_code == 400


def test_the_viewer_and_both_exports_read_through_one_function(
    client, monkeypatch
) -> None:
    """``read_raw_lines`` is the only reader: a second code path would miss this."""

    calls: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    real = rd.read_raw_lines

    def spy(sources, request_ids, kinds, **kwargs):
        calls.append((tuple(sorted(request_ids)), tuple(sorted(kinds))))
        return real(sources, request_ids, kinds, **kwargs)

    monkeypatch.setattr(rd, "read_raw_lines", spy)
    assert _raw(client).status_code == 200
    assert _raw(client, kind="sse").status_code == 200
    assert _export(client, format="csv").status_code == 200
    bulk = client.get(
        "/admin/api/export",
        params={"format": "json", "scope": "requests", "fields": "raw_diagnostics"},
    )
    assert bulk.status_code == 200
    assert calls[0] == ((RID,), tuple(sorted(rd.RAW_KINDS)))
    assert calls[1] == ((RID,), ("sse",))
    assert calls[2] == ((RID,), tuple(sorted(rd.RAW_KINDS)))
    assert calls[3] == (tuple(sorted((RID, OTHER))), tuple(sorted(rd.RAW_KINDS)))


# ---------------------------------------------------------------------------
# the Analytics export's Raw diagnostics group
# ---------------------------------------------------------------------------


def test_the_raw_group_adds_one_cell_per_kind_with_its_provenance(client) -> None:
    response = client.get(
        "/admin/api/export",
        params={"format": "json", "scope": "requests", "fields": "raw_diagnostics"},
    )
    assert response.status_code == 200
    rows = {row["id"]: row for row in response.json()}
    mine = rows[RID]
    assert set(export_engine.RAW_DIAGNOSTICS_COLUMNS) <= set(mine)
    sse = mine["raw_sse_events"].split("\n")
    assert sse[0].startswith(
        f"# SSE events · raw-diagnostics.log · RAW_SSE lines · request {RID} · present · 5 lines"
    )
    body = [line for line in sse if not line.startswith("#")]
    assert body == [line["text"] for line in _raw(client, kind="sse").json()["lines"]]
    assert PASTED in mine["raw_payloads"]
    assert "absent_setting" in mine["raw_cli_diagnostics"]
    other_sse = rows[OTHER]["raw_sse_events"]
    assert "someone else" in other_sse and "<thinking>" not in other_sse


def test_the_raw_group_refuses_grouping_other_scopes_and_too_many_requests(
    client, monkeypatch
) -> None:
    grouped = client.get(
        "/admin/api/export",
        params={
            "format": "json",
            "scope": "requests",
            "fields": "raw_diagnostics",
            "group_by": "provider",
        },
    )
    assert (
        grouped.status_code == 400 and "cannot be grouped" in grouped.json()["detail"]
    )
    attempts = client.get(
        "/admin/api/export",
        params={"format": "json", "scope": "attempts", "fields": "raw_diagnostics"},
    )
    assert attempts.status_code == 400
    monkeypatch.setattr(export_engine, "RAW_DIAGNOSTICS_MAX_REQUESTS", 1)
    too_many = client.get(
        "/admin/api/export",
        params={"format": "csv", "scope": "requests", "fields": "raw_diagnostics"},
    )
    assert too_many.status_code == 400
    assert "Narrow the filter" in too_many.json()["detail"]


def test_the_raw_group_in_xlsx_refuses_a_cell_excel_would_cut(
    client, monkeypatch
) -> None:
    params = {"format": "xlsx", "scope": "requests", "fields": "raw_diagnostics"}
    # The fixture's cells fit Excel's real limit...
    assert client.get("/admin/api/export", params=params).status_code == 200
    # ...and a limit they exceed refuses before a byte is written.
    monkeypatch.setattr(rd, "XLSX_CELL_LIMIT", 40)
    refused = client.get("/admin/api/export", params=params)
    assert refused.status_code == 422
    detail = refused.json()["detail"]
    assert "Excel keeps at most 40 characters in a cell" in detail
    assert f"request {RID}'s" in detail or f"request {OTHER}'s" in detail


def test_the_raw_group_txt_is_streamed_with_the_buffered_widths(client) -> None:
    rows = client.get(
        "/admin/api/export",
        params={"format": "json", "scope": "requests", "fields": "raw_diagnostics"},
    ).json()
    txt = client.get(
        "/admin/api/export",
        params={"format": "txt", "scope": "requests", "fields": "raw_diagnostics"},
    )
    assert txt.status_code == 200
    columns = list(rows[0])
    headers = export_engine.request_detail_headers(columns)
    expected = b"".join(
        export_engine.render_txt(rows, columns, headers, title="t", summary="s")
    )
    assert txt.content.split(b"\n", 2)[2] == expected.split(b"\n", 2)[2]


#: The workbook's own creation time in ``docProps/core.xml`` (whole seconds).
_CORE_STAMP = re.compile(rb"(<dcterms:(?:created|modified)\b[^>]*>)[^<]*(</dcterms:)")


def _workbook_parts(data: bytes) -> list[tuple[str, int, bytes]]:
    """Every part of an XLSX, in archive order, without the render's own clock.

    Each zip member carries the time it was written (2 s steps) and
    ``docProps/core.xml`` the moment the workbook was made: two renders of the
    same rows differ in those and nothing else, so the bytes of two renders a
    second apart differ (Windows release run 41155123641, a 2 s boundary).
    """

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        parts = [
            (info.filename, info.compress_type, archive.read(info))
            for info in archive.infolist()
        ]
    return [
        (
            name,
            kind,
            _CORE_STAMP.sub(rb"\1\2", body) if name == "docProps/core.xml" else body,
        )
        for name, kind, body in parts
    ]


def test_the_default_exports_render_through_the_unchanged_path(client) -> None:
    """With the group off, each format is the renderer's own bytes, as before.

    The route computes the columns, rows, headers and filename exactly as it
    did; these assertions rebuild every format from the JSON rows through the
    same renderers with no ``widths`` -- the pre-7.121.0 signature -- and the
    bytes must be identical (TXT from its table down, since its title carries
    the export's own timestamp; XLSX part for part, since its archive carries
    the moment it was written).
    """

    params = {"scope": "requests"}
    rows = client.get("/admin/api/export", params={**params, "format": "json"}).json()
    assert rows, "the fixture must export at least one row"
    columns = list(rows[0])
    headers = export_engine.request_detail_headers(columns)
    json_bytes = client.get(
        "/admin/api/export", params={**params, "format": "json"}
    ).content
    assert json_bytes == b"".join(export_engine.render_json_array(rows))
    csv_bytes = client.get(
        "/admin/api/export", params={**params, "format": "csv"}
    ).content
    assert csv_bytes == b"".join(export_engine.render_csv(rows, columns, headers))
    txt_bytes = client.get(
        "/admin/api/export", params={**params, "format": "txt"}
    ).content
    buffered = b"".join(
        export_engine.render_txt(rows, columns, headers, title="t", summary="s")
    )
    assert txt_bytes.split(b"\n", 2)[2] == buffered.split(b"\n", 2)[2]
    xlsx_bytes = client.get(
        "/admin/api/export", params={**params, "format": "xlsx"}
    ).content
    assert _workbook_parts(xlsx_bytes) == _workbook_parts(
        b"".join(export_engine.render_xlsx(rows, columns, headers))
    )


def test_a_default_export_never_reads_the_raw_logs(client, monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("a default export read the raw logs")

    monkeypatch.setattr(rd, "read_raw_lines", forbidden)
    for fmt in ("json", "csv", "xlsx", "txt"):
        response = client.get(
            "/admin/api/export", params={"format": fmt, "scope": "requests"}
        )
        assert response.status_code == 200, fmt
    rows = client.get(
        "/admin/api/export", params={"format": "json", "scope": "requests"}
    ).json()
    assert not set(export_engine.RAW_DIAGNOSTICS_COLUMNS) & set(rows[0])


def test_the_group_is_opt_in_and_origin_stays_the_last_group() -> None:
    assert export_engine.RAW_DIAGNOSTICS_FIELD in export_engine.REQUEST_FIELD_IDS
    assert (
        export_engine.RAW_DIAGNOSTICS_FIELD not in export_engine.DEFAULT_REQUEST_FIELDS
    )
    assert export_engine.REQUEST_FIELD_IDS[-2:] == ("exit", "origin")
    assert export_engine.request_detail_columns(
        [export_engine.RAW_DIAGNOSTICS_FIELD]
    ) == (export_engine.request_detail_columns([]))
    assert export_engine.request_detail_derived_columns(
        [export_engine.RAW_DIAGNOSTICS_FIELD]
    )[-4:] == list(export_engine.RAW_DIAGNOSTICS_COLUMNS)
    source = (
        Path(__file__).resolve().parents[2]
        / "src/my_claude_code/api/admin_static/admin.js"
    ).read_text(encoding="utf-8")
    fields = source.split("const EXPORT_FIELDS = {", 1)[1].split("],", 1)[0]
    assert '{ id: "raw_diagnostics", label: "Raw diagnostics" }' in fields
    defaults = source.split("const EXPORT_DEFAULT_FIELDS = {", 1)[1].split("]),", 1)[0]
    assert '"raw_diagnostics"' not in defaults
