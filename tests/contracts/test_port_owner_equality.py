"""Where ``ss``/``netstat`` answer, the port owner is what it was before 7.111.3.

PORT-OWNER (7.111.3) extends ``cli.port_diagnostics``' command ladder with
``lsof`` and ``/proc/net/tcp`` rungs AFTER ``ss -ltnp`` and ``netstat -ano``,
and adds an "undetermined" state beside the old ``None``. The user's rule for
it (2026-10-24, "ADDENDUM ONLY"): wherever the two existing commands exist and
answer, every output is byte-identical to today.

``port_owner_equality_baseline.json`` was written by THIS module run on the
code before the change (``fork/main`` = the commit named in the file), with
``MCC_PORT_OWNER_BASELINE_WRITE`` set to the baseline's path. It holds, for
every recorded command output in ``port_owner_corpus.py`` and every port in
``PORTS``, the ``PortOwner`` the old ``diagnose_port_owner`` returned (or
``None``). The changed code must return the same value -- same type, same
pid, same name, same command -- on every platform branch the new ladder has
(Windows, Linux, macOS), with the new rungs unavailable, and must never ask a
new rung once ``ss`` or ``netstat`` has named an owner.

To refresh: copy this file and ``port_owner_corpus.py`` to a base checkout and
run it there with ``MCC_PORT_OWNER_BASELINE_WRITE=<path to the baseline>``.
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.cli import port_diagnostics
from tests.contracts.port_owner_corpus import CASES, PORTS

BASELINE = Path(__file__).with_name("port_owner_equality_baseline.json")
WRITE_ENV = "MCC_PORT_OWNER_BASELINE_WRITE"
PLATFORMS = ("win32", "linux", "darwin")


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _fake_run(case: dict[str, dict[str, Any]], calls: list[str]):
    """``subprocess.run`` answering from one corpus case; new rungs are absent."""

    def run(command, *args, **kwargs):
        name = command[0]
        calls.append(name)
        behaviour = case.get(name)
        if behaviour is None:
            # lsof, or anything else the ladder may grow: not installed here.
            raise FileNotFoundError(2, "No such file or directory", name)
        kind = behaviour["kind"]
        if kind == "missing":
            raise FileNotFoundError(2, "No such file or directory", name)
        if kind == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 2.0))
        if kind == "error":
            raise OSError("the command could not be started")
        return _FakeCompleted(behaviour["returncode"], behaviour["stdout"])

    return run


def _as_record(owner: Any) -> dict[str, Any] | None:
    if owner is None:
        return None
    return {
        "type": type(owner).__name__,
        "pid": owner.pid,
        "name": owner.name,
        "command": owner.command,
    }


def _answers(monkeypatch, calls_by_case: dict[str, list[str]]) -> dict[str, Any]:
    answers: dict[str, Any] = {}
    for name, case in CASES.items():
        per_port: dict[str, Any] = {}
        for port in PORTS:
            calls: list[str] = []
            monkeypatch.setattr(subprocess, "run", _fake_run(case, calls))
            per_port[str(port)] = _as_record(
                port_diagnostics.diagnose_port_owner("0.0.0.0", port)
            )
            calls_by_case[f"{name}:{port}"] = calls
        answers[name] = per_port
    return answers


def _base_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).parent,
    )
    return completed.stdout.strip()


def test_write_the_baseline_on_the_base_tree(monkeypatch) -> None:
    target = os.environ.get(WRITE_ENV)
    if not target:
        pytest.skip(f"set {WRITE_ENV} on a base checkout to (re)write the baseline")
    commit = _base_commit()
    answers = _answers(monkeypatch, {})
    monkeypatch.undo()
    document = {
        "base_commit": commit,
        "ports": list(PORTS),
        "cases": answers,
    }
    Path(target).write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _baseline() -> dict[str, Any]:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def test_the_baseline_covers_the_whole_corpus() -> None:
    baseline = _baseline()
    assert set(baseline["cases"]) == set(CASES)
    assert baseline["ports"] == list(PORTS)
    # It pins real answers, not only None: owners on all three shapes.
    named = [
        record
        for per_port in baseline["cases"].values()
        for record in per_port.values()
        if record is not None
    ]
    assert len(named) >= 10


@pytest.mark.parametrize("platform", PLATFORMS)
def test_every_recorded_answer_is_unchanged(monkeypatch, tmp_path, platform) -> None:
    if os.environ.get(WRITE_ENV):
        pytest.skip("writing the baseline, not checking it")
    monkeypatch.setattr(port_diagnostics, "_PLATFORM", platform)
    monkeypatch.setattr(port_diagnostics, "_PROC_ROOT", tmp_path / "no-proc")
    calls_by_case: dict[str, list[str]] = {}

    answers = _answers(monkeypatch, calls_by_case)

    expected = _baseline()["cases"]
    differing = [
        (name, port, expected[name][port], answers[name][port])
        for name in CASES
        for port in expected[name]
        if expected[name][port] != answers[name][port]
    ]
    assert differing == []
    # Once ss or netstat has named an owner, no new rung is ever asked.
    for name in CASES:
        for port, record in expected[name].items():
            if record is not None:
                calls = calls_by_case[f"{name}:{port}"]
                assert "lsof" not in calls, (name, port, calls)
                assert set(calls) <= {"ss", "netstat"}, (name, port, calls)


@pytest.mark.parametrize("platform", PLATFORMS)
def test_the_lookup_agrees_with_the_old_answer(monkeypatch, tmp_path, platform) -> None:
    """``lookup_port_owner().owner`` is exactly what ``diagnose_port_owner`` says.

    And "undetermined" appears only where neither of the two old commands
    produced a listing at all -- every case where one of them answered keeps
    the old meaning of ``None``: nothing listening.
    """

    if os.environ.get(WRITE_ENV):
        pytest.skip("writing the baseline, not checking it")
    monkeypatch.setattr(port_diagnostics, "_PLATFORM", platform)
    monkeypatch.setattr(port_diagnostics, "_PROC_ROOT", tmp_path / "no-proc")
    expected = _baseline()["cases"]
    for name, case in CASES.items():
        answered = any(
            behaviour["kind"] == "answer" and behaviour["returncode"] == 0
            for behaviour in case.values()
        )
        for port in PORTS:
            monkeypatch.setattr(subprocess, "run", _fake_run(case, []))
            lookup = port_diagnostics.lookup_port_owner("0.0.0.0", port)
            assert _as_record(lookup.owner) == expected[name][str(port)], (name, port)
            if lookup.owner is not None or answered:
                assert lookup.undetermined is False, (name, port, lookup)
            else:
                assert lookup.undetermined is True, (name, port, lookup)
                assert lookup.reason, (name, port, lookup)
