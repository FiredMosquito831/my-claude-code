"""Request-log and web-search export iterators: keyset paging, body decompression,
aggregate grouping, websearch include_content."""

import io
import time

import pytest
from openpyxl import load_workbook

from my_claude_code.core import export as export_engine
from my_claude_code.core.request_log import RequestRecord, get_request_log_store


def _seed_requests(store, count: int = 650) -> None:
    base = time.time() - 1000
    for index in range(count):
        store.enqueue(
            RequestRecord(
                id=f"r{index}",
                endpoint="/v1/messages",
                protocol="anthropic",
                provider="p1" if index % 2 == 0 else "p2",
                resolved_model="m1" if index % 3 else "m2",
                ts_epoch=base + index,
                status="error" if index == 4 else "success",
                error_message="boom" if index == 4 else None,
                tokens_in=10 * index,
                tokens_out=index,
                cache_read_tokens=5 if index % 2 else None,
                tool_call_count=index if index % 2 else 0,
                thinking_chars=100 if index % 2 else 0,
                input_text=f"prompt {index}",
                output_text=f"reply {index}",
                thinking_text=f"reasoning {index}",
            )
        )
    store.close()


def test_iter_export_rows_bypasses_500_row_cap(tmp_path) -> None:
    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    _seed_requests(store, 650)
    columns = export_engine.request_detail_columns(
        ["providers", "models", "tokens_out"]
    )
    rows = list(
        store.iter_export_rows(
            columns=columns,
            need_bodies=False,
        )
    )
    assert len(rows) == 650
    # Newest first, no duplicates.
    ids = [row["id"] for row in rows]
    assert len(set(ids)) == 650
    assert ids[0] == "r649"


def test_body_decompression_only_when_requested(tmp_path) -> None:
    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    _seed_requests(store, 20)
    # No body fields selected: bodies are not decompressed, so the projected
    # row carries no input_text column at all.
    meta_columns = export_engine.request_detail_columns(["providers", "tokens_out"])
    meta_rows = list(store.iter_export_rows(columns=meta_columns, need_bodies=False))
    assert "input_text" not in meta_rows[0]

    # Body fields selected: input_text is decompressed and uncapped.
    body_columns = export_engine.request_detail_columns(["input", "tokens_out"])
    body_rows = list(store.iter_export_rows(columns=body_columns, need_bodies=True))
    assert body_rows[0]["input_text"] == "prompt 19"
    store.close()


def test_iter_export_aggregates_groups_and_orders(tmp_path) -> None:
    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    _seed_requests(store, 12)
    group_by = ["provider", "period", "model"]
    select, names = export_engine.request_aggregate_sql(
        ["providers", "models", "error_rate", "tokens_out"], group_by
    )
    rows = list(
        store.iter_export_aggregates(select=select, names=names, group_by=group_by)
    )
    assert rows
    for row in rows:
        export_engine.compute_request_aggregate_derived(row, ["error_rate"])
    # Ordered by group dimensions.
    keys = [(row["provider"], row["period"], row["model"]) for row in rows]
    assert keys == sorted(keys)
    # Requests/errors present; error_rate derived.
    assert all(row["requests"] > 0 for row in rows)
    assert any(row["error_rate"] is not None for row in rows)
    store.close()


def test_websearch_iter_export_rows_include_content(tmp_path) -> None:
    from my_claude_code.websearch.analytics import WebSearchLogStore
    from my_claude_code.websearch.registry import SearchOutcome

    store = WebSearchLogStore(tmp_path / "ws.db")
    store.record(
        SearchOutcome(
            ts_epoch=time.time(),
            ts_iso="2026-06-01T10:00:00+00:00",
            provider="exa",
            key_index=0,
            key_label="exak…1234",
            query="apple pie",
            results_count=5,
            duration_ms=12.5,
            status="success",
            error_kind=None,
            error_message=None,
            cost_usd=0.01,
            input_payload={"q": "apple pie"},
            output_payload={"answer": "a"},
            provider_config={"provider_id": "exa"},
        )
    )
    store.flush()
    columns = export_engine.websearch_detail_columns(
        ["provider", "results_count", "cost_usd"]
    )
    rows = list(store.iter_export_rows(columns=columns, include_content=False))
    assert len(rows) == 1
    assert rows[0]["provider"] == "exa"
    assert rows[0]["results_count"] == 5
    assert rows[0]["cost_usd"] == 0.01
    store.close()


def test_websearch_aggregate(tmp_path) -> None:
    from my_claude_code.websearch.analytics import WebSearchLogStore
    from my_claude_code.websearch.registry import SearchOutcome

    store = WebSearchLogStore(tmp_path / "ws2.db")
    for i in range(3):
        store.record(
            SearchOutcome(
                ts_epoch=time.time() + i,
                ts_iso="2026-06-01T10:00:00+00:00",
                provider="exa",
                key_index=0,
                key_label="exak…1234",
                query="q",
                results_count=i,
                duration_ms=10.0,
                status="success",
                error_kind=None,
                error_message=None,
                cost_usd=0.01,
                input_payload=None,
                output_payload=None,
                provider_config=None,
            )
        )
    store.flush()
    group_by = ["provider"]
    select, names = export_engine.websearch_aggregate_sql(
        ["provider", "status", "results_count", "cost_usd"], group_by
    )
    rows = list(
        store.iter_export_aggregates(select=select, names=names, group_by=group_by)
    )
    assert len(rows) == 1
    assert rows[0]["provider"] == "exa"
    assert rows[0]["requests"] == 3
    assert rows[0]["results"] == 3  # 0+1+2
    assert rows[0]["cost_usd"] == pytest.approx(0.03)
    store.close()


def test_render_txt_keeps_cells_longer_than_the_width_cap() -> None:
    long_value = "x" * 60
    rows = iter([{"model": long_value}])

    chunks = list(
        export_engine.render_txt(rows, ["model"], ["Model"], "Title", "Summary")
    )

    row_line = chunks[-2].decode("utf-8")
    assert long_value in row_line


# ------------------------------------------------------------------ harness --


def test_harness_is_an_always_present_detail_column() -> None:
    """A column gated by no field group is unreachable, so this one is not.

    ``headers`` sits in the display order belonging to no group and can
    therefore never be exported; ``harness`` must not join it.
    """
    assert "harness" in export_engine._REQUEST_ALWAYS_COLUMNS
    assert "harness" in export_engine._REQUEST_COLUMN_ORDER
    assert export_engine._REQUEST_COLUMN_LABELS["harness"] == "Harness"
    # Empty field selection: the structural columns are all that survive.
    assert "harness" in export_engine.request_detail_columns([])


def _rendered(fmt: str) -> str:
    """One row through one renderer, using the real column list and labels.

    XLSX is a zip archive, so it is read back through openpyxl rather than
    decoded; the other three are text and are compared as text.
    """
    columns = export_engine.request_detail_columns([])
    headers = [export_engine._REQUEST_COLUMN_LABELS.get(name, name) for name in columns]
    row = dict.fromkeys(columns, "")
    row["harness"] = "opencode2"
    if fmt == "csv":
        chunks = export_engine.render_csv([row], columns, headers)
    elif fmt == "json":
        chunks = export_engine.render_json_array([row])
    elif fmt == "txt":
        chunks = export_engine.render_txt([row], columns, headers, "Title", "Summary")
    else:
        rendered = b"".join(export_engine.render_xlsx([row], columns, headers))
        sheet = load_workbook(io.BytesIO(rendered)).active
        assert sheet is not None
        return "\n".join(
            " ".join("" if cell is None else str(cell) for cell in line)
            for line in sheet.values
        )
    return b"".join(chunks).decode("utf-8", "replace")


@pytest.mark.parametrize("fmt", ["csv", "json", "txt", "xlsx"])
def test_every_renderer_carries_the_harness_column(fmt: str) -> None:
    """All four read the same column list, so all four must show the value."""
    rendered = _rendered(fmt)
    assert "opencode2" in rendered
    if fmt == "json":
        # JSON is keyed, not positional, so it names the column rather than
        # its label.
        assert '"harness"' in rendered
    else:
        assert "Harness" in rendered


def test_harness_is_a_group_by_dimension(tmp_path) -> None:
    """Grouping by harness must aggregate the same rows the dashboard does."""
    assert "harness" in export_engine.REQUEST_GROUP_DIMENSIONS
    assert export_engine.validate_group_by(
        export_engine.REQUEST_SCOPE, ["harness"]
    ) == ["harness"]

    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    for index in range(6):
        store.enqueue(
            RequestRecord(
                id=f"r{index}",
                endpoint="/v1/messages",
                protocol="anthropic",
                provider="p1",
                ts_epoch=time.time() - 100 + index,
                harness=("claude", "codex")[index % 2],
                tokens_in=index,
            )
        )
    store.close()

    select, names = export_engine.request_aggregate_sql(["providers"], ["harness"])
    rows = list(
        store.iter_export_aggregates(select=select, names=names, group_by=["harness"])
    )
    assert [row["harness"] for row in rows] == ["claude", "codex"]

    # And the same dimension filters, so an export can be narrowed to one agent.
    only = list(
        store.iter_export_aggregates(
            select=select, names=names, group_by=["harness"], harness="codex"
        )
    )
    assert [row["harness"] for row in only] == ["codex"]
