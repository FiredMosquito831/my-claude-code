"""The request detail's raw-diagnostics reader (7.121.0): nothing recorded is lost.

The user's rule for this viewer, binding (spec USER DECISIONS 2026-10-30,
Q2 overridden): "we don't want to lose data this is why it is called a raw log
nothing lost". So what these tests hold:

* every page of a request, put together, is exactly the lines the files hold
  for it -- counted and hashed against an independent read of the files, across
  a rotation, at any block size, with either line ending;
* a hard limit (the scan's deadline) refuses, it never answers with part;
* a line too large to hold is marked in place with where it is, never cut;
* lines pruned between two pages refuse the next page instead of shifting it;
* the four honest states, and the Raw view never carrying normalised text.

Every file here is written by the test, in the sink's own line format
(``RAW_<KIND> <request_id> <message>``, newlines escaped), with made-up ids and
text; one test drives the real sink to prove the format is the real one.
"""

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import pytest
from loguru import logger

from my_claude_code.api import raw_diagnostics as rd
from my_claude_code.config.logging_config import (
    RAW_DIAGNOSTICS_LOG_FILENAME,
    configure_logging,
    configure_raw_diagnostics,
)

STATIC = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "my_claude_code"
    / "api"
    / "admin_static"
)

RID = "req_" + "a" * 32
OTHER = "req_" + "b" * 32
THIRD = "req_" + "c" * 32

ALL_ON = rd.RawSettings(enabled=dict.fromkeys(rd.RAW_KINDS, True), retain_files=10)
ALL_OFF = rd.RawSettings(enabled=dict.fromkeys(rd.RAW_KINDS, False), retain_files=10)


def _sink_line(marker: str, request_id: str, message: str) -> bytes:
    """One record exactly as ``_RawDiagnosticsSink`` writes it, minus the EOL."""

    text = f"{marker} {request_id} {message}"
    return text.replace("\r", "\\r").replace("\n", "\\n").encode("utf-8")


def _write(
    path: Path, lines: list[bytes], *, eol: bytes = b"\r\n", mtime: float
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(line + eol for line in lines))
    os.utime(path, (mtime, mtime))
    return path


def _sse(request_id: str, n: int) -> bytes:
    frame = f'event: content_block_delta\ndata: {{"type":"content_block_delta","index":0,"delta":{{"type":"text_delta","text":"<thinking>part {n}</thinking>"}}}}'
    return _sink_line(
        "RAW_SSE", request_id, f"SSE_EVENT: content_block_delta - {frame}"
    )


def _direct(paths: list[Path], marker: str, request_id: str) -> list[str]:
    """An independent read: every file whole, in mtime order, split on newlines."""

    prefix = f"{marker} {request_id} "
    found: list[str] = []
    for path in sorted(paths, key=lambda p: (p.stat().st_mtime, p.name)):
        found.extend(
            line[len(prefix) :]
            for line in path.read_bytes().decode("utf-8").splitlines()
            if line.startswith(prefix)
        )
    return found


def _digest(texts: list[str]) -> str:
    return hashlib.sha256("\n".join(texts).encode("utf-8")).hexdigest()


def _all_pages(
    sources: rd.RawSources, kind: str, row: Any = None
) -> list[dict[str, Any]]:
    pages = [rd.raw_page(sources, RID, kind, row, ALL_ON)]
    while pages[-1].get("next_start") is not None:
        last = pages[-1]
        pages.append(
            rd.raw_page(
                sources,
                RID,
                kind,
                row,
                ALL_ON,
                start=last["next_start"],
                anchor=last["anchor"],
            )
        )
        assert len(pages) < 1000, "paging never ended"
    return pages


@pytest.fixture
def rotated_family(tmp_path: Path) -> tuple[rd.RawSources, list[Path]]:
    """Fifty RID frames across a rotated part and the active file, interleaved."""

    now = time.time()
    logs = tmp_path / "logs"
    older: list[bytes] = []
    newer: list[bytes] = []
    for n in range(20):
        older += [_sse(OTHER, n), _sse(RID, n)]
    for n in range(20, 50):
        newer += [_sse(RID, n), _sse(THIRD, n)]
    rotated = _write(
        logs / "raw-diagnostics.2026-10-30_10-00-00_000001.log", older, mtime=now - 600
    )
    active = _write(logs / RAW_DIAGNOSTICS_LOG_FILENAME, newer, mtime=now - 5)
    return (
        rd.RawSources(raw_log=active, server_log=logs / "server.log"),
        [rotated, active],
    )


# ---------------------------------------------------------------------------
# nothing lost: paging, rotation, blocks, line endings
# ---------------------------------------------------------------------------


def test_every_page_together_is_exactly_the_recorded_lines(
    rotated_family, monkeypatch
) -> None:
    """Count and SHA-256 of all pages equal a direct read of the files."""

    sources, paths = rotated_family
    monkeypatch.setattr(rd, "PAGE_MAX_LINES", 7)
    pages = _all_pages(sources, rd.KIND_SSE)

    texts = [line["text"] for page in pages for line in page["lines"]]
    expected = _direct(paths, "RAW_SSE", RID)
    assert len(expected) == 50
    assert len(texts) == len(expected)
    assert _digest(texts) == _digest(expected)
    assert texts == expected
    # Numbered once, in order, every page full but the last.
    assert [line["n"] for page in pages for line in page["lines"]] == list(range(1, 51))
    assert [len(page["lines"]) for page in pages] == [7] * 7 + [1]
    assert all(page["lines_total"] == 50 for page in pages)
    # The request spans the rotation: its first part is the rotated file's.
    assert pages[0]["files"] == [paths[0].name, paths[1].name]
    assert "part 0<" in texts[0] and "part 49<" in texts[-1]
    assert all(page["refused"] is None for page in pages)


def _part_number(text: bytes | None) -> int:
    match = re.search(rb"part (\d+)<", text or b"")
    assert match is not None, text
    return int(match.group(1))


def test_a_request_across_two_rotated_files_comes_back_in_order(rotated_family) -> None:
    sources, paths = rotated_family
    lines = list(rd.read_raw_lines(sources, [RID], [rd.KIND_SSE]))
    assert [line.file for line in lines] == [paths[0].name] * 20 + [paths[1].name] * 30
    assert [_part_number(line.text) for line in lines] == list(range(50))


@pytest.mark.parametrize("block", [7, 64, 257, 4096, 1 << 20])
@pytest.mark.parametrize("eol", [b"\r\n", b"\n"])
def test_block_size_and_line_endings_never_change_the_lines(
    tmp_path, monkeypatch, block, eol
) -> None:
    """The block scan cuts lines out of buffers: no size may move a boundary."""

    monkeypatch.setattr(rd, "_BLOCK_BYTES", block)
    lines = []
    for n in range(40):
        lines.append(_sse(RID, n) + b"x" * (n * 13))
        lines.append(_sse(OTHER, n))
    path = _write(
        tmp_path / RAW_DIAGNOSTICS_LOG_FILENAME, lines, eol=eol, mtime=time.time()
    )
    sources = rd.RawSources(raw_log=path, server_log=None)
    found = [
        (line.text or b"").decode("utf-8")
        for line in rd.read_raw_lines(sources, [RID], [rd.KIND_SSE])
    ]
    assert found == _direct([path], "RAW_SSE", RID)
    assert len(found) == 40


def test_a_page_is_bounded_by_bytes_but_always_holds_one_whole_line(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(rd, "PAGE_MAX_BYTES", 300)
    big = _sink_line(
        "RAW_PAYLOAD", RID, "FULL_PAYLOAD [x]: {'k': '" + "y" * 2000 + "'}"
    )
    small = [
        _sink_line("RAW_PAYLOAD", RID, f"FULL_PAYLOAD [x]: {{'n': {n}}}")
        for n in range(5)
    ]
    path = _write(
        tmp_path / RAW_DIAGNOSTICS_LOG_FILENAME,
        [*small[:2], big, *small[2:]],
        mtime=time.time(),
    )
    sources = rd.RawSources(raw_log=path, server_log=None)
    pages = _all_pages(sources, rd.KIND_PAYLOADS)
    sizes = [[line["bytes"] for line in page["lines"]] for page in pages]
    # The 2 KB line is a page of its own; nothing is split or skipped.
    assert any(len(page) == 1 and page[0] > 2000 for page in sizes)
    assert sum(len(page) for page in sizes) == 6
    assert [line["n"] for page in pages for line in page["lines"]] == list(range(1, 7))


def test_the_scan_deadline_refuses_rather_than_answers_partly(
    rotated_family, monkeypatch
) -> None:
    sources, _paths = rotated_family
    monkeypatch.setattr(rd, "SCAN_DEADLINE_SECONDS", -1.0)
    page = rd.raw_page(sources, RID, rd.KIND_SSE, None, ALL_ON)
    assert page["refused"]["reason"] == "deadline"
    assert "nothing is shown rather than part of it" in page["refused"]["message"]
    assert page["lines"] == []
    summary = rd.raw_summary(sources, RID, None, ALL_ON)
    assert summary["refused"]["reason"] == "deadline"
    assert summary["kinds"] == []


def test_a_line_over_the_limit_is_marked_in_place_and_never_cut(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(rd, "LINE_LIMIT_BYTES", 400)
    huge = _sink_line(
        "RAW_PAYLOAD", RID, "FULL_PAYLOAD [x]: {'k': '" + "z" * 5000 + "'}"
    )
    after = _sink_line("RAW_PAYLOAD", RID, "FULL_PAYLOAD [x]: {'after': True}")
    path = _write(
        tmp_path / RAW_DIAGNOSTICS_LOG_FILENAME, [huge, after], mtime=time.time()
    )
    sources = rd.RawSources(raw_log=path, server_log=None)
    page = rd.raw_page(sources, RID, rd.KIND_PAYLOADS, None, ALL_ON)
    first, second = page["lines"]
    assert first["text"] is None and first["over_limit"] is True
    assert first["bytes"] == len(huge) - len(f"RAW_PAYLOAD {RID} ")
    assert path.name in first["marker"] and "byte 0" in first["marker"]
    assert "nothing was cut" in first["marker"]
    assert second["text"] == "FULL_PAYLOAD [x]: {'after': True}"
    assert any("larger than" in note for note in page["notes"])


def test_a_final_line_without_a_terminator_is_kept_and_marked(tmp_path) -> None:
    path = tmp_path / RAW_DIAGNOSTICS_LOG_FILENAME
    path.write_bytes(_sse(RID, 1) + b"\r\n" + _sse(RID, 2))
    sources = rd.RawSources(raw_log=path, server_log=None)
    page = rd.raw_page(sources, RID, rd.KIND_SSE, None, ALL_ON)
    assert [line.get("partial", False) for line in page["lines"]] == [False, True]
    assert page["lines"][1]["text"].endswith('part 2</thinking>"}}')
    assert any("ends mid-line" in note for note in page["notes"])


def test_lines_pruned_between_two_pages_refuse_the_next_page(
    rotated_family, monkeypatch
) -> None:
    sources, paths = rotated_family
    monkeypatch.setattr(rd, "PAGE_MAX_LINES", 10)
    first = rd.raw_page(sources, RID, rd.KIND_SSE, None, ALL_ON)
    assert first["next_start"] == 10
    paths[0].unlink()  # rotation's retention deleted the oldest part
    second = rd.raw_page(
        sources,
        RID,
        rd.KIND_SSE,
        None,
        ALL_ON,
        start=first["next_start"],
        anchor=first["anchor"],
    )
    assert second["refused"]["reason"] == "moved"
    assert second["lines"] == []


def test_a_rotation_during_the_read_is_read_again(rotated_family, monkeypatch) -> None:
    sources, _paths = rotated_family
    real = rd._names
    calls = {"n": 0}

    def renamed_once(nominal):
        calls["n"] += 1
        names = real(nominal)
        return (
            names | {"raw-diagnostics.2026-10-30_12-00-00_000009.log"}
            if calls["n"] == 1
            else names
        )

    monkeypatch.setattr(rd, "_names", renamed_once)
    page = rd.raw_page(sources, RID, rd.KIND_SSE, None, ALL_ON)
    assert page["refused"] is None
    assert page["lines_total"] == 50
    assert calls["n"] == 2


def test_a_window_that_keeps_rotating_refuses(rotated_family, monkeypatch) -> None:
    sources, _paths = rotated_family
    real = rd._names
    monkeypatch.setattr(
        rd, "_names", lambda nominal: real(nominal) | {"raw-diagnostics.new.log"}
    )
    page = rd.raw_page(sources, RID, rd.KIND_SSE, None, ALL_ON)
    assert page["refused"]["reason"] == "rotating"
    assert page["lines"] == []


# ---------------------------------------------------------------------------
# the four honest states, and what each says
# ---------------------------------------------------------------------------


def _row(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": RID,
        "ts_epoch": time.time() - 30,
        "status": "success",
    }
    base.update(fields)
    return base


def test_lines_present_with_the_setting_off_now_are_shown_and_say_so(
    rotated_family,
) -> None:
    sources, _paths = rotated_family
    page = rd.raw_page(sources, RID, rd.KIND_SSE, _row(), ALL_OFF)
    assert page["capture"] == rd.CAPTURE_PRESENT
    assert page["enabled"] is False
    assert any("is off now" in note for note in page["notes"])


def test_no_lines_with_the_setting_off_names_the_setting_and_its_page(tmp_path) -> None:
    sources = rd.RawSources(
        raw_log=tmp_path / RAW_DIAGNOSTICS_LOG_FILENAME, server_log=None
    )
    page = rd.raw_page(sources, RID, rd.KIND_SSE, _row(), ALL_OFF)
    assert (page["capture"], page["reason"]) == (rd.CAPTURE_SETTING, "setting_off")
    assert page["headline"] == "Not captured."
    assert "`LOG_RAW_SSE_EVENTS` is off" in page["message"]
    assert "Limits & Resilience → Diagnostics → “Log Raw SSE Events”" in page["message"]
    assert "Save applies it — no restart." in page["message"]
    assert page["lines"] == []
    payloads = rd.raw_page(sources, RID, rd.KIND_PAYLOADS, _row(), ALL_OFF)
    assert "“Log Raw API Payloads”" in payloads["message"]


def test_a_request_older_than_the_retained_window_is_pruned_by_rotation(
    tmp_path,
) -> None:
    now = time.time()
    logs = tmp_path / "logs"
    for index in range(2):
        _write(
            logs / f"raw-diagnostics.2026-10-30_1{index}-00-00_00000{index}.log",
            [_sse(OTHER, index)],
            mtime=now - 3000 + index * 600,
        )
    active = _write(logs / RAW_DIAGNOSTICS_LOG_FILENAME, [_sse(OTHER, 9)], mtime=now)
    sources = rd.RawSources(raw_log=active, server_log=None)
    keeps_two = rd.RawSettings(
        enabled=dict.fromkeys(rd.RAW_KINDS, True), retain_files=2
    )
    page = rd.raw_page(sources, RID, rd.KIND_SSE, _row(ts_epoch=now - 7200), keeps_two)
    assert (page["capture"], page["reason"]) == (rd.CAPTURE_ROTATED, "pruned")
    assert page["headline"] == "No raw lines remain."
    assert "keeps 2 rotated files of 50 MB" in page["message"]
    # A request inside the window, with the setting on, was simply not recorded.
    inside = rd.raw_page(sources, RID, rd.KIND_SSE, _row(ts_epoch=now - 60), keeps_two)
    assert (inside["capture"], inside["reason"]) == (rd.CAPTURE_NEVER, "not_recorded")


def test_raw_absent_with_a_stored_answer_points_at_normalized_never_shows_it(
    tmp_path,
) -> None:
    sources = rd.RawSources(
        raw_log=tmp_path / RAW_DIAGNOSTICS_LOG_FILENAME, server_log=None
    )
    marker = "NORMALISED-ONLY-ANSWER-7121"
    row = _row(output_text=marker, output_chars=len(marker))
    page = rd.raw_page(sources, RID, rd.KIND_SSE, row, ALL_ON)
    assert page["capture"] != rd.CAPTURE_PRESENT
    assert "only in normalised form" in page["message"]
    assert marker not in json.dumps(page)
    normalized = rd.normalized_view(RID, rd.KIND_SSE, row, ALL_ON)
    assert marker in normalized["text"]
    assert normalized["state"] == "stored"


def test_only_mcc_frames_say_the_upstream_bytes_were_never_captured(tmp_path) -> None:
    path = _write(
        tmp_path / RAW_DIAGNOSTICS_LOG_FILENAME,
        [_sse(RID, n) for n in range(3)],
        mtime=time.time(),
    )
    sources = rd.RawSources(raw_log=path, server_log=None)
    page = rd.raw_page(sources, RID, rd.KIND_SSE, _row(), ALL_ON)
    assert page["frames"] == {"egress": 3, "upstream": 0}
    assert any("raw bytes are never captured" in note for note in page["notes"])


def test_upstream_frames_are_told_apart_from_mccs_own(tmp_path) -> None:
    upstream = [
        _sink_line(
            "RAW_SSE",
            RID,
            'SSE_EVENT: anthropic event: message_start\ndata: {"type":"message_start"}',
        ),
        _sink_line(
            "RAW_SSE", RID, 'SSE_EVENT: responses_event {"type": "response.incomplete"}'
        ),
    ]
    path = _write(
        tmp_path / RAW_DIAGNOSTICS_LOG_FILENAME,
        [*upstream, _sse(RID, 1)],
        mtime=time.time(),
    )
    page = rd.raw_page(
        rd.RawSources(raw_log=path, server_log=None), RID, rd.KIND_SSE, _row(), ALL_ON
    )
    assert page["frames"] == {"egress": 1, "upstream": 2}
    assert not any("never captured" in note for note in page["notes"])


# ---------------------------------------------------------------------------
# tracebacks: server.log ERROR lines carrying the id
# ---------------------------------------------------------------------------


def _server_record(level: str, message: str, request_id: str | None) -> bytes:
    record: dict[str, Any] = {
        "time": "2026-10-30 10:00:00.000000+03:00",
        "level": level,
        "message": message,
        "module": "my_claude_code.api.request_errors",
        "function": "log_unexpected_api_exception",
        "line": 0,
    }
    if request_id is not None:
        record["request_id"] = request_id
    return json.dumps(record).encode("utf-8")


def test_traceback_lines_join_by_request_id_and_skip_other_levels(tmp_path) -> None:
    lines = [
        _server_record("INFO", f"request handled request_id={RID}", RID),
        _server_record("ERROR", f"Messages request failed request_id={RID}: boom", RID),
        _server_record(
            "ERROR",
            'Traceback (most recent call last):\n  File "x.py"\nValueError: boom',
            RID,
        ),
        _server_record("ERROR", f"Background task failed request_id={RID}: late", None),
        _server_record("ERROR", "someone else", OTHER),
    ]
    server = _write(tmp_path / "server.log", lines, mtime=time.time())
    sources = rd.RawSources(raw_log=None, server_log=server)
    page = rd.raw_page(
        sources,
        RID,
        rd.KIND_TRACEBACK,
        _row(status="error", error_kind="api_error"),
        ALL_ON,
    )
    assert page["lines_total"] == 3
    assert all(json.loads(line["text"])["level"] == "ERROR" for line in page["lines"])
    assert not any("was off when" in note for note in page["notes"])


def test_an_exception_type_alone_says_the_traceback_setting_was_off(tmp_path) -> None:
    server = _write(
        tmp_path / "server.log",
        [
            _server_record(
                "ERROR",
                f"Messages request failed request_id={RID} exc_type=ValueError",
                RID,
            )
        ],
        mtime=time.time(),
    )
    page = rd.raw_page(
        rd.RawSources(raw_log=None, server_log=server),
        RID,
        rd.KIND_TRACEBACK,
        _row(status="error", error_kind="api_error"),
        ALL_OFF,
    )
    assert page["capture"] == rd.CAPTURE_PRESENT
    note = next(note for note in page["notes"] if "was off when" in note)
    assert "`LOG_API_ERROR_TRACEBACKS`" in note and "“Log API Error Tracebacks”" in note


def test_a_request_that_did_not_fail_has_no_error_to_show(tmp_path) -> None:
    sources = rd.RawSources(raw_log=None, server_log=tmp_path / "server.log")
    page = rd.raw_page(sources, RID, rd.KIND_TRACEBACK, _row(), ALL_ON)
    assert (page["capture"], page["reason"]) == (rd.CAPTURE_NEVER, "no_error")
    failed = rd.raw_page(
        sources,
        RID,
        rd.KIND_TRACEBACK,
        _row(status="error", error_kind="overloaded"),
        ALL_ON,
    )
    assert (failed["capture"], failed["reason"]) == (rd.CAPTURE_NEVER, "no_error_line")
    assert "Normalized shows it" in failed["message"]


def _raised_value_error() -> ValueError:
    try:
        raise ValueError("upstream said no")
    except ValueError as raised:
        return raised
    raise AssertionError("unreachable")


def test_the_real_traceback_producer_reaches_the_box(tmp_path) -> None:
    """``log_unexpected_api_exception`` -> server.log -> the traceback box.

    The producer, the settings switch and the sink are the real ones
    (``configure_logging`` writes JSON lines with the contextvar's id at top
    level); only the row and the exception are made up. With the setting on the
    box shows the message and the traceback; with it off it shows the exception
    type alone and names the setting.
    """

    from my_claude_code.api.request_errors import log_unexpected_api_exception
    from my_claude_code.config.settings import Settings

    configure_logging(tmp_path / "logs" / "server.log", force=True)
    server = tmp_path / "logs" / "server.log"
    # Raised and caught, so the traceback it logs is a real one: a bare
    # exception object has no stack and formats as its message alone.
    exc = _raised_value_error()
    for setting in (True, False):
        with logger.contextualize(request_id=RID):
            log_unexpected_api_exception(
                Settings.model_validate({"LOG_API_ERROR_TRACEBACKS": str(setting)}),
                exc,
                context="Messages request failed",
                request_id=RID,
            )
        logger.complete()
    sources = rd.RawSources(raw_log=None, server_log=server)
    found = list(rd.read_raw_lines(sources, [RID], [rd.KIND_TRACEBACK]))
    # Setting on: the message line and the traceback line. Setting off: the
    # one line carrying ``exc_type``. Every one names the request.
    assert [line.tag for line in found] == ["error", "traceback", "exc_type"]
    assert found[0].text is not None and b"upstream said no" in found[0].text
    assert found[1].text is not None
    assert b"Traceback (most recent call last)" in found[1].text
    assert found[2].text is not None and b"exc_type=ValueError" in found[2].text
    page = rd.raw_page(sources, RID, rd.KIND_TRACEBACK, _row(status="error"), ALL_ON)
    assert [line["n"] for line in page["lines"]] == [1, 2, 3]
    # One line has a traceback, one has the type alone: the note says which.
    assert any(
        "one of these lines records the exception type only" in note
        for note in page["notes"]
    )


# ---------------------------------------------------------------------------
# CLI: never tied to a request
# ---------------------------------------------------------------------------


def test_cli_lines_without_a_request_id_are_counted_not_claimed(tmp_path) -> None:
    lines = [_sink_line("RAW_CLI", "-", f"Non-JSON output: line {n}") for n in range(4)]
    path = _write(tmp_path / RAW_DIAGNOSTICS_LOG_FILENAME, lines, mtime=time.time())
    sources = rd.RawSources(raw_log=path, server_log=None)
    page = rd.raw_page(sources, RID, rd.KIND_CLI, _row(), ALL_ON)
    assert (page["capture"], page["reason"]) == (
        rd.CAPTURE_NEVER,
        "not_request_correlated",
    )
    assert "(4 such lines in the retained window)" in page["message"]
    off = rd.raw_page(sources, RID, rd.KIND_CLI, _row(), ALL_OFF)
    assert off["capture"] == rd.CAPTURE_SETTING
    assert "“Log Raw CLI Diagnostics”" in off["message"]


def test_an_old_requests_cli_box_never_claims_rotation_pruned_it(tmp_path) -> None:
    """No CLI line ever carried this request's id, so none was there to prune."""

    now = time.time()
    logs = tmp_path / "logs"
    for index in range(2):
        _write(
            logs / f"raw-diagnostics.2026-10-30_1{index}-00-00_00000{index}.log",
            [_sse(OTHER, index)],
            mtime=now - 3000 + index * 600,
        )
    active = _write(logs / RAW_DIAGNOSTICS_LOG_FILENAME, [_sse(OTHER, 9)], mtime=now)
    keeps_two_off = rd.RawSettings(
        enabled=dict.fromkeys(rd.RAW_KINDS, False), retain_files=2
    )
    keeps_two_on = rd.RawSettings(
        enabled=dict.fromkeys(rd.RAW_KINDS, True), retain_files=2
    )
    sources = rd.RawSources(raw_log=active, server_log=None)
    old = _row(ts_epoch=now - 7200)
    assert (
        rd.raw_page(sources, RID, rd.KIND_CLI, old, keeps_two_off)["capture"]
        == rd.CAPTURE_SETTING
    )
    on = rd.raw_page(sources, RID, rd.KIND_CLI, old, keeps_two_on)
    assert (on["capture"], on["reason"]) == (rd.CAPTURE_NEVER, "not_request_correlated")
    # The same old request's SSE box is the one rotation explains.
    assert (
        rd.raw_page(sources, RID, rd.KIND_SSE, old, keeps_two_on)["capture"]
        == rd.CAPTURE_ROTATED
    )


# ---------------------------------------------------------------------------
# normalised material: the request log's own fields, never a raw byte
# ---------------------------------------------------------------------------


def test_normalized_blocks_name_where_each_field_lives() -> None:
    row = _row(
        input_text="the prompt",
        output_text="the answer",
        thinking_text="the reasoning",
        tool_calls=[{"name": "Read", "input": {"path": "a.txt"}}],
        tool_call_count=1,
        error_kind="api_error",
        error_message="boom",
        route_attempts=[
            {
                "attempt": 0,
                "model_ref": "p/m",
                "wire_body": {"max_tokens": 5},
                "params": {},
            },
            {
                "attempt": 1,
                "model_ref": "vision/describe",
                "wire_body": {"x": 1},
                "params": {"kind": "describe"},
            },
        ],
    )
    sse = rd.normalized_view(RID, rd.KIND_SSE, row, ALL_ON)
    assert [block["source"] for block in sse["blocks"]] == [
        "requests.output_text",
        "requests.thinking_text",
        "requests.tool_calls",
    ]
    assert "the answer" in sse["text"] and "the reasoning" in sse["text"]
    payloads = rd.normalized_view(RID, rd.KIND_PAYLOADS, row, ALL_ON)
    assert [block["source"] for block in payloads["blocks"]] == [
        "requests.input_text",
        "request_attempts.wire_body",
    ]
    assert '"max_tokens": 5' in payloads["text"] and '"x": 1' not in payloads["text"]
    cli = rd.normalized_view(RID, rd.KIND_CLI, row, ALL_ON)
    assert cli["state"] == "none" and "no normalised form" in cli["message"]


def test_bodies_that_were_not_stored_say_so() -> None:
    row = _row(output_text=None, output_chars=1234, thinking_chars=0)
    view = rd.normalized_view(RID, rd.KIND_SSE, row, ALL_ON)
    assert view["state"] == "none"
    assert "REQUEST_LOG_CAPTURE_BODIES=false" in view["message"]


# ---------------------------------------------------------------------------
# the real sink writes what this reader reads
# ---------------------------------------------------------------------------


def test_the_real_sink_and_the_reader_agree_byte_for_byte(tmp_path) -> None:
    configure_logging(tmp_path / "logs" / "server.log", force=True)
    path = tmp_path / "logs" / RAW_DIAGNOSTICS_LOG_FILENAME
    assert configure_raw_diagnostics(path, sse=True, payloads=True, retain_files=10)
    payload = {
        "model": "fake",
        "messages": [{"role": "user", "content": "a {brace}\nnew line"}],
    }
    frame = 'event: content_block_delta\ndata: {"delta": {"text": "<thinking>t</thinking>"}}'
    with logger.contextualize(request_id=RID):
        logger.debug("FULL_PAYLOAD [{}]: {}", RID, payload)
        logger.debug("SSE_EVENT: {} - {}", "content_block_delta", frame)
    with logger.contextualize(request_id=OTHER):
        logger.debug("SSE_EVENT: {} - {}", "ping", "event: ping")
    logger.complete()

    sources = rd.RawSources(raw_log=path, server_log=None)
    found = list(rd.read_raw_lines(sources, [RID], [rd.KIND_PAYLOADS, rd.KIND_SSE]))
    assert [line.kind for line in found] == [rd.KIND_PAYLOADS, rd.KIND_SSE]
    assert (
        found[0].text
        == f"FULL_PAYLOAD [{RID}]: {payload}".replace("\n", "\\n").encode()
    )
    assert (
        found[1].text
        == f"SSE_EVENT: content_block_delta - {frame}".replace("\n", "\\n").encode()
    )
    assert found[1].tag == "egress"


# ---------------------------------------------------------------------------
# the mirrors this module keeps, pinned to what they mirror
# ---------------------------------------------------------------------------


def test_the_settings_page_names_are_the_dashboards_own() -> None:
    """``SETTINGS_PAGE_FOR_SECTION`` must say the page admin.js renders it on."""

    source = (STATIC / "admin.js").read_text(encoding="utf-8")
    table = source.split("const VIEW_GROUPS = [", 1)[1].split("\n];", 1)[0]
    for section, page in rd.SETTINGS_PAGE_FOR_SECTION.items():
        groups = re.findall(r"\{(.*?)\n  \}", table, flags=re.DOTALL)
        owners = [
            group
            for group in groups
            if f'"{section}"' in group.split("sections:", 1)[-1]
        ]
        assert len(owners) == 1, section
        assert f'label: "{page}"' in owners[0]


def test_the_export_pickers_kind_labels_are_the_readers_own() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    picker = html.split('id="reqRawExportPanel"', 1)[1].split("</fieldset>", 1)[0]
    pairs = re.findall(r'value="(\w+)"[^>]*/>\s*<span>([^<]+)</span>', picker)
    assert pairs == [(kind, rd.KINDS[kind].label) for kind in rd.RAW_KINDS]
