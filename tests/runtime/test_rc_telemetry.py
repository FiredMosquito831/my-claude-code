"""Tests for live /rc telemetry SQLite + inflight join (PR 19)."""

import json
import sqlite3
from pathlib import Path

from my_claude_code.runtime.remote.telemetry import collect_session_telemetry


def _create_requests_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE requests (
                id TEXT PRIMARY KEY,
                ts_epoch REAL NOT NULL,
                endpoint TEXT,
                protocol TEXT,
                requested_model TEXT,
                provider TEXT,
                resolved_model TEXT,
                route_attempt INTEGER,
                tokens_in INTEGER,
                tokens_out INTEGER,
                cache_read_tokens INTEGER,
                cache_write_tokens INTEGER,
                reasoning_tokens INTEGER,
                ttft_ms REAL,
                duration_ms REAL,
                status TEXT,
                status_code INTEGER,
                error_kind TEXT,
                error_message TEXT,
                cost_usd REAL,
                estimated_cost_usd REAL,
                cost_source TEXT,
                session_id TEXT,
                project_dir TEXT,
                params TEXT
            );
            CREATE TABLE request_attempts (
                request_id TEXT,
                attempt INTEGER,
                provider TEXT,
                model_ref TEXT,
                outcome TEXT,
                params TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _insert_request(
    db_path: Path,
    *,
    req_id: str,
    ts_epoch: float,
    session_id: str | None,
    project_dir: str,
    provider: str = "openai",
    requested_model: str = "gpt-5.4",
    resolved_model: str = "openai/gpt-5.4",
    endpoint: str = "/v1/messages",
    protocol: str = "anthropic",
    tokens_in: int = 100,
    tokens_out: int = 40,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    reasoning_tokens: int = 0,
    ttft_ms: float = 120.0,
    duration_ms: float = 450.0,
    status: str = "success",
    status_code: int = 200,
    cost_usd: float | None = None,
    estimated_cost_usd: float | None = None,
    cost_source: str | None = None,
    wire_surface: str | None = None,
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        params = (
            json.dumps({"wire": {"surface": wire_surface}}) if wire_surface else None
        )
        conn.execute(
            """
            INSERT INTO requests (
                id, ts_epoch, endpoint, protocol, requested_model, provider,
                resolved_model, route_attempt, tokens_in, tokens_out,
                cache_read_tokens, cache_write_tokens, reasoning_tokens,
                ttft_ms, duration_ms, status, status_code, error_kind,
                error_message, cost_usd, estimated_cost_usd, cost_source,
                session_id, project_dir, params
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                req_id,
                ts_epoch,
                endpoint,
                protocol,
                requested_model,
                provider,
                resolved_model,
                0,
                tokens_in,
                tokens_out,
                cache_read_tokens,
                cache_write_tokens,
                reasoning_tokens,
                ttft_ms,
                duration_ms,
                status,
                status_code,
                None,
                None,
                cost_usd,
                estimated_cost_usd,
                cost_source,
                session_id,
                project_dir,
                params,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_collect_telemetry_joins_session_isolates_siblings_and_splits_cost(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "requests.db"
    _create_requests_db(db_path)
    cwd = str(tmp_path / "workspace")

    # 1. Matched by RC session_id, provider-reported cost + Anthropic->Responses translation
    _insert_request(
        db_path,
        req_id="req_1",
        ts_epoch=1000.0,
        session_id="rc_sess_1",
        project_dir=cwd,
        provider="openai",
        tokens_in=500,
        tokens_out=120,
        cache_read_tokens=50,
        ttft_ms=88.5,
        cost_usd=0.012,
        cost_source="provider",
        wire_surface="responses",
    )
    # 2. Matched by external_session_id, estimated cost only
    _insert_request(
        db_path,
        req_id="req_2",
        ts_epoch=1005.0,
        session_id="ext_sess_1",
        project_dir=cwd,
        provider="anthropic",
        tokens_in=200,
        tokens_out=80,
        cost_usd=0.004,
        estimated_cost_usd=0.004,
        cost_source="catalog",
        wire_surface="messages",
    )
    # 3. Matched by project_dir when session_id is NULL
    _insert_request(
        db_path,
        req_id="req_3",
        ts_epoch=1002.0,
        session_id=None,
        project_dir=cwd,
        tokens_in=100,
        tokens_out=25,
        status="error",
        status_code=502,
        cost_usd=0.0,
        cost_source="unpriced",
    )
    # 4. DIFFERENT session in the SAME cwd — MUST NOT leak into rc_sess_1
    _insert_request(
        db_path,
        req_id="req_other_session",
        ts_epoch=1010.0,
        session_id="rc_other_999",
        project_dir=cwd,
        tokens_in=99999,
        tokens_out=99999,
        cost_usd=99.0,
        cost_source="provider",
    )

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO request_attempts VALUES (?, ?, ?, ?, ?, ?)",
        (
            "req_1",
            0,
            "openai",
            "openai/gpt-5.4",
            "succeeded",
            json.dumps({"wire": {"surface": "responses"}}),
        ),
    )
    conn.commit()
    conn.close()

    session = {
        "id": "rc_sess_1",
        "external_session_id": "ext_sess_1",
        "cwd": cwd,
        "worktree_path": None,
    }
    inflight_data = {
        "enabled": True,
        "rows": [
            {
                "id": "inf_mine",
                "session_id": "rc_sess_1",
                "phase": "streaming",
                "attempt_index": 0,
                "waited_s": 0.4,
                "model_ref": "openai/gpt-5.4",
                "provider": "openai",
                "ttft_ms": 95.0,
                "elapsed_ms": 400.0,
                "phase_elapsed_ms": 200.0,
                "tier": "sonnet",
            },
            {
                "id": "inf_other",
                "session_id": "rc_other_999",
                "project_dir": cwd,
                "phase": "streaming",
                "model_ref": "other",
                "provider": "other",
            },
        ],
    }

    tel = collect_session_telemetry(
        session, db_path=db_path, inflight_data=inflight_data
    )

    assert tel["session_id"] == "rc_sess_1"
    assert tel["request_count"] == 3
    assert tel["error_count"] == 1
    assert tel["input_tokens"] == 800
    assert tel["output_tokens"] == 225
    assert tel["cached_tokens"] == 50
    # Strict separation of reported vs estimated cost fidelity
    assert abs(tel["reported_cost_usd"] - 0.012) < 1e-9
    assert abs(tel["estimated_cost_usd"] - 0.004) < 1e-9
    assert abs(tel["total_cost_usd"] - 0.016) < 1e-9

    req_ids = [item["request_id"] for item in tel["recent_requests"]]
    assert req_ids == ["req_2", "req_3", "req_1"]
    assert "req_other_session" not in req_ids

    req1 = tel["recent_requests"][2]
    assert req1["translated"] is True
    assert "Messages" in req1["translation_label"]
    assert "Responses" in req1["translation_label"]
    assert len(tel["inflight"]) == 1
    assert tel["inflight"][0]["request_id"] == "inf_mine"


def test_collect_telemetry_handles_missing_db_and_caps_recent_at_10(
    tmp_path: Path,
) -> None:
    missing = collect_session_telemetry(
        {"id": "rc_missing", "cwd": str(tmp_path)},
        db_path=tmp_path / "does_not_exist.db",
        inflight_data={"enabled": True, "rows": []},
    )
    assert missing["request_count"] == 0
    assert missing["total_cost_usd"] == 0.0
    assert missing["reported_cost_usd"] is None
    assert missing["recent_requests"] == []

    db_path = tmp_path / "requests_many.db"
    _create_requests_db(db_path)
    for idx in range(12):
        _insert_request(
            db_path,
            req_id=f"r_{idx}",
            ts_epoch=2000.0 + idx,
            session_id="rc_many",
            project_dir=str(tmp_path),
            tokens_in=10,
            tokens_out=5,
            cost_usd=0.001,
            cost_source="provider",
        )
    res = collect_session_telemetry(
        {"id": "rc_many", "cwd": str(tmp_path)},
        db_path=db_path,
        inflight_data={"enabled": True, "rows": []},
    )
    assert res["request_count"] == 12
    assert res["input_tokens"] == 120
    assert len(res["recent_requests"]) == 10
    assert res["recent_requests"][0]["request_id"] == "r_11"
