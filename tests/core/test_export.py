"""Request-log and web-search export iterators: keyset paging, body decompression,
aggregate grouping, websearch include_content."""

import time

import pytest

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
