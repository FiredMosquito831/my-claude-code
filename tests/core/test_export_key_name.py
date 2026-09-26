"""The ``key_name`` export column: added beside ``key_label``, never instead.

``key_label`` and ``key_index`` are what history is keyed on and are left
exactly as they were, values and labels alike. ``key_name`` is a display join
resolved at export time, so a rename rewrites no row and an ambiguous mask
exports nothing rather than a guess.
"""

from my_claude_code.core import export as export_engine


def test_the_request_export_adds_a_key_name_column_beside_key_label() -> None:
    columns = export_engine.request_detail_columns(
        export_engine.DEFAULT_REQUEST_FIELDS
    ) + export_engine.request_detail_derived_columns(
        export_engine.DEFAULT_REQUEST_FIELDS
    )

    assert "key_label" in columns
    assert "key_name" in columns
    headers = export_engine.request_detail_headers(columns)
    assert headers[columns.index("key_label")] == "Key"
    assert headers[columns.index("key_name")] == "Key name"


def test_the_attempt_export_adds_key_name_and_keeps_key_label_and_key_index() -> None:
    fields = [*export_engine.DEFAULT_ATTEMPT_FIELDS, "wire"]
    columns = export_engine.attempt_output_columns(fields)
    headers = export_engine.attempt_detail_headers(columns)

    assert columns.index("key_label") < columns.index("key_name")
    assert columns.index("key_name") < columns.index("key_index")
    assert headers[columns.index("key_label")] == "Key"
    assert headers[columns.index("key_index")] == "Key index"
    assert headers[columns.index("key_name")] == "Key name"


def test_a_named_key_exports_its_name() -> None:
    row = {"key_label": "sk-a…1111", "ttft_ms": None, "ttft_winner_ms": None}

    export_engine.compute_request_detail_derived(
        row, export_engine.DEFAULT_REQUEST_FIELDS, {"sk-a…1111": "Work"}
    )

    assert row["key_name"] == "Work"
    assert row["key_label"] == "sk-a…1111"


def test_an_unnamed_key_exports_an_empty_key_name() -> None:
    row = {"key_label": "sk-b…2222", "ttft_ms": None, "ttft_winner_ms": None}

    export_engine.compute_request_detail_derived(
        row, export_engine.DEFAULT_REQUEST_FIELDS, {"sk-a…1111": "Work"}
    )

    assert row["key_name"] == ""


def test_an_ambiguous_masked_label_exports_an_empty_key_name() -> None:
    """The index already dropped the ambiguous label, so nothing resolves."""

    row = {"key_label": "nvap…zzzz", "ttft_ms": None, "ttft_winner_ms": None}

    export_engine.compute_request_detail_derived(row, (), {})

    assert row["key_name"] == ""


def test_a_row_with_no_key_at_all_exports_an_empty_key_name() -> None:
    row = {"key_label": None, "ttft_ms": None, "ttft_winner_ms": None}

    export_engine.compute_request_detail_derived(row, (), {"sk-a…1111": "Work"})

    assert row["key_name"] == ""


def test_the_attempt_row_resolves_the_same_way() -> None:
    row = {"key_label": "sk-a…1111", "key_index": 0, "outcome": "success"}

    export_engine.compute_attempt_detail_derived(row, (), {"sk-a…1111": "Work"})

    assert row["key_name"] == "Work"
    assert row["key_index"] == 0


def test_the_websearch_detail_export_carries_the_column_too() -> None:
    columns = export_engine.websearch_detail_columns(
        export_engine.DEFAULT_WEBSEARCH_FIELDS
    )
    derived = export_engine.websearch_detail_derived_columns()

    assert "key_name" not in columns
    assert derived == ["key_name"]
    row = {"key_label": "exa-…9999"}
    export_engine.compute_websearch_detail_derived(row, {"exa-…9999": "Search"})
    assert row["key_name"] == "Search"


def test_the_existing_request_columns_are_unchanged_apart_from_the_insert() -> None:
    """A golden: every column that existed still exists, with its own label."""

    columns = export_engine.request_detail_columns(
        export_engine.DEFAULT_REQUEST_FIELDS
    ) + export_engine.request_detail_derived_columns(
        export_engine.DEFAULT_REQUEST_FIELDS
    )
    # ``cancel_reason`` joins ``key_name`` as an always-derived column, and
    # like it, it is appended rather than inserted: every column that existed
    # keeps its exact position, so an export that ignores the new one is the
    # file it was before.
    # ``success_reason`` (7.57.0) is appended beside ``cancel_reason`` the same
    # way.
    without = [
        column
        for column in columns
        if column not in {"key_name", "cancel_reason", "success_reason"}
    ]

    assert without == [
        "ts_epoch",
        "stream",
        "id",
        "ts_iso",
        "endpoint",
        "protocol",
        "provider",
        "requested_model",
        "resolved_model",
        "key_label",
        "status",
        "error_kind",
        "error_message",
        "tokens_in",
        "cache_read_tokens",
        "cache_write_tokens",
        "tokens_out",
        "ttft_ms",
        "ttft_winner_ms",
        "duration_ms",
        "route_attempt",
        "route_primary_model",
        "route_chain",
        "route_diverted_from",
        "route_diversion",
        "harness",
        "ttft_lost_to_fallbacks_ms",
        "cache_hit_rate",
    ]
    # And the additions are exactly where "appended" says they are.
    assert columns[-4:] == [
        "cancel_reason",
        "success_reason",
        "key_name",
        "cache_hit_rate",
    ]
