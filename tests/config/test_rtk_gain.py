"""Tests for reading RTK's own token-savings report (``rtk gain``)."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from my_claude_code.config import rtk as rtk_config
from my_claude_code.config.rtk import (
    RTK_GAIN_SUMMARY_FIELDS,
    RTK_SUBPROCESS_TIMEOUT_SECONDS,
    RTK_TELEMETRY_ENV,
    read_rtk_gain,
)

#: Shape taken from RTK's ``ExportData``/``ExportSummary`` structs in
#: ``src/analytics/gain.rs`` (rtk-ai/rtk @ master, v0.45.0).
_GAIN_PAYLOAD = {
    "summary": {
        "total_commands": 128,
        "total_input": 900_000,
        "total_output": 120_000,
        "total_saved": 780_000,
        "avg_savings_pct": 86.7,
        "total_time_ms": 45_000,
        "avg_time_ms": 351,
    },
    "daily": [
        {
            "date": "2026-08-20",
            "commands": 12,
            "input_tokens": 90_000,
            "output_tokens": 9_000,
            "saved_tokens": 81_000,
            "savings_pct": 90.0,
            "total_time_ms": 4_000,
        }
    ],
    "weekly": [],
    "monthly": [],
}


def _use_binary(monkeypatch, path: object) -> None:
    monkeypatch.setattr(rtk_config, "_available_binary", lambda: path)


def _fake_run(monkeypatch, *, stdout: str = "", stderr: str = "", returncode: int = 0):
    calls: list[dict[str, object]] = []

    def run(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)

    monkeypatch.setattr(rtk_config.subprocess, "run", run)
    return calls


def test_parses_summary_and_periods(monkeypatch):
    _use_binary(monkeypatch, Path("/usr/bin/rtk"))
    calls = _fake_run(monkeypatch, stdout=json.dumps(_GAIN_PAYLOAD))

    result = read_rtk_gain()

    assert result["available"] is True
    assert result["reason"] is None
    assert result["summary"] == _GAIN_PAYLOAD["summary"]
    assert result["periods"]["daily"] == _GAIN_PAYLOAD["daily"]
    assert result["raw"] == _GAIN_PAYLOAD
    assert calls[0]["command"][1:] == ["gain", "--all", "--format", "json"]


def test_uses_timeout_and_disables_telemetry(monkeypatch):
    _use_binary(monkeypatch, Path("/usr/bin/rtk"))
    calls = _fake_run(monkeypatch, stdout=json.dumps(_GAIN_PAYLOAD))

    read_rtk_gain()

    assert calls[0]["timeout"] == RTK_SUBPROCESS_TIMEOUT_SECONDS
    assert calls[0]["env"][RTK_TELEMETRY_ENV] == "1"


def test_missing_binary_reports_not_installed(monkeypatch):
    _use_binary(monkeypatch, None)

    def explode(*args, **kwargs):
        raise AssertionError("must not spawn a subprocess without a binary")

    monkeypatch.setattr(rtk_config.subprocess, "run", explode)

    result = read_rtk_gain()

    assert result["available"] is False
    assert result["reason"] == "not_installed"
    assert result["summary"] is None


def test_non_zero_exit_reports_run_failed(monkeypatch):
    _use_binary(monkeypatch, Path("/usr/bin/rtk"))
    _fake_run(monkeypatch, stderr="no database", returncode=1)

    result = read_rtk_gain()

    assert result["available"] is False
    assert result["reason"] == "run_failed"
    assert "no database" in result["detail"]


def test_os_error_reports_run_failed(monkeypatch):
    _use_binary(monkeypatch, Path("/usr/bin/rtk"))

    def run(*args, **kwargs):
        raise OSError("binary vanished")

    monkeypatch.setattr(rtk_config.subprocess, "run", run)

    result = read_rtk_gain()

    assert result["available"] is False
    assert result["reason"] == "run_failed"


def test_empty_stdout_reports_empty_output(monkeypatch):
    _use_binary(monkeypatch, Path("/usr/bin/rtk"))
    _fake_run(monkeypatch, stdout="   \n")

    result = read_rtk_gain()

    assert result["available"] is False
    assert result["reason"] == "empty_output"


def test_invalid_json_reports_invalid_json(monkeypatch):
    _use_binary(monkeypatch, Path("/usr/bin/rtk"))
    _fake_run(monkeypatch, stdout="not json at all")

    result = read_rtk_gain()

    assert result["available"] is False
    assert result["reason"] == "invalid_json"


@pytest.mark.parametrize(
    "stdout",
    ["[1, 2, 3]", '"a string"', "{}", '{"summary": []}', '{"summary": {"nope": 1}}'],
)
def test_unexpected_shapes_report_unexpected_schema(monkeypatch, stdout):
    _use_binary(monkeypatch, Path("/usr/bin/rtk"))
    _fake_run(monkeypatch, stdout=stdout)

    result = read_rtk_gain()

    assert result["available"] is False
    assert result["reason"] == "unexpected_schema"


def test_timeout_reports_timeout(monkeypatch):
    _use_binary(monkeypatch, Path("/usr/bin/rtk"))

    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="rtk", timeout=15)

    monkeypatch.setattr(rtk_config.subprocess, "run", run)

    result = read_rtk_gain()

    assert result["available"] is False
    assert result["reason"] == "timeout"


def test_partial_summary_keeps_missing_fields_none(monkeypatch):
    """A field RTK did not report must be ``None``, never a fabricated zero."""

    _use_binary(monkeypatch, Path("/usr/bin/rtk"))
    _fake_run(
        monkeypatch,
        stdout=json.dumps({"summary": {"total_commands": 0, "total_saved": "lots"}}),
    )

    result = read_rtk_gain()

    assert result["available"] is True
    assert result["summary"]["total_commands"] == 0
    assert result["summary"]["total_saved"] is None
    assert result["summary"]["avg_savings_pct"] is None
    assert set(result["summary"]) == set(RTK_GAIN_SUMMARY_FIELDS)


def test_booleans_are_not_numbers(monkeypatch):
    _use_binary(monkeypatch, Path("/usr/bin/rtk"))
    _fake_run(
        monkeypatch,
        stdout=json.dumps({"summary": {"total_commands": True, "total_saved": 5}}),
    )

    result = read_rtk_gain()

    assert result["summary"]["total_commands"] is None
    assert result["summary"]["total_saved"] == 5


def test_non_list_periods_become_none(monkeypatch):
    _use_binary(monkeypatch, Path("/usr/bin/rtk"))
    _fake_run(
        monkeypatch,
        stdout=json.dumps({"summary": {"total_saved": 1}, "daily": {"oops": True}}),
    )

    result = read_rtk_gain()

    assert result["periods"] == {"daily": None, "weekly": None, "monthly": None}


def _write_stub(tmp_path: Path, body: str) -> Path:
    """Write a real, executable stub ``rtk`` that prints ``body`` on stdout."""

    if sys.platform == "win32":
        stub = tmp_path / "rtk.cmd"
        escaped = body.replace("%", "%%").replace("^", "^^")
        escaped = escaped.replace("&", "^&").replace("<", "^<").replace(">", "^>")
        stub.write_text(f"@echo off\r\necho {escaped}\r\n", encoding="utf-8")
        return stub
    stub = tmp_path / "rtk"
    quoted = body.replace("'", "'''")
    stub.write_text(f"#!/bin/sh\nprintf '%s' '{quoted}'\n", encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return stub


@pytest.mark.local_serial
def test_real_stub_executable_end_to_end(monkeypatch, tmp_path):
    """Exercise the real subprocess path against an on-disk stub binary."""

    payload = json.dumps({"summary": {"total_saved": 4242, "total_commands": 7}})
    stub = _write_stub(tmp_path, payload)
    _use_binary(monkeypatch, stub)
    monkeypatch.delenv(RTK_TELEMETRY_ENV, raising=False)

    result = read_rtk_gain()

    assert result["available"] is True, result
    assert result["summary"]["total_saved"] == 4242
    assert result["summary"]["total_commands"] == 7
    assert result["binary_path"] == str(stub)
    assert os.environ.get(RTK_TELEMETRY_ENV) is None
