"""Server log files stay bounded while the server runs (7.69.8).

7.69.6 replaced loguru's ``rotation="50 MB"`` / ``retention=`` with
``_ServerLogSink`` so a ``server.log`` held by another process is never
emptied. Two things that loguru used to do went missing with it:

- G1: once this process had fallen back to its own ``server.<pid>.log``, the
  sink never size-rotated again, so that file grew for the life of the
  process.
- G2: rotated ``server.*.log`` files were pruned only by the startup sweep,
  so a server that ran for days kept every rotation until its next start.

``_MAX_LOG_BYTES`` is patched small throughout: nothing here writes 50 MB.
Where the module ``logger`` is asserted on it is a stub -- the sink runs
inside loguru's own writer and must never call back into it (and a real
loguru sink in this suite sees each record twice anyway).
"""

import os
import re
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from loguru import logger

from my_claude_code.config import logging_config

_LINE = "x" * 60 + "\n"  # 61 bytes: three fit under a 200-byte cap, four do not
_ACTIVE = re.compile(r"server\.\d+\.log")


def _fallback_sink(
    tmp_path: Path, *, retain_files: int
) -> tuple[Path, Path, logging_config._ServerLogSink]:
    """A sink in the state a held ``server.log`` at startup leaves it in."""

    log_path = tmp_path / "server.log"
    fallback = logging_config._per_process_log_path(log_path)
    sink = logging_config._ServerLogSink(
        fallback, nominal_path=log_path, retain_files=retain_files
    )
    return log_path, fallback, sink


def _rotated(directory: Path) -> list[Path]:
    """Every rotated log: matches the sweep's glob, and is nobody's active file."""

    return sorted(
        path
        for path in directory.glob("server.*.log")
        if not _ACTIVE.fullmatch(path.name)
    )


def _aged(directory: Path, names: list[str]) -> list[Path]:
    """Create ``names`` as small files, oldest first by modification time."""

    base = time.time() - 10 * len(names)
    files = []
    for index, name in enumerate(names):
        path = directory / name
        path.write_text(f"{name}\n", encoding="utf-8")
        os.utime(path, (base + 10 * index, base + 10 * index))
        files.append(path)
    return files


_STAMPS = [f"server.2026-01-01_00-00-0{n}_000000.log" for n in range(1, 6)]


# ------------------------------------------------------------------- G1


def test_the_per_process_fallback_file_rotates_at_the_cap(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(logging_config, "_MAX_LOG_BYTES", 200)
    log_path, fallback, sink = _fallback_sink(tmp_path, retain_files=0)
    try:
        for _ in range(20):  # 1,220 bytes: six caps' worth and two lines over
            sink.write(_LINE)
    finally:
        sink.stop()

    assert sink.active_path == fallback
    assert fallback.stat().st_size <= 200
    parts = sorted(tmp_path.glob(f"server.{os.getpid()}.*.log"))
    assert len(parts) == 6, parts
    assert all(part.stat().st_size <= 200 for part in parts)
    # Each part matches the sweep's glob and is not active-shaped, so the cap
    # can reach it; none of it went near the other process's server.log.
    assert parts == _rotated(tmp_path)
    assert not log_path.exists()
    # Nothing was lost on the way.
    every_byte = fallback.read_text(encoding="utf-8") + "".join(
        part.read_text(encoding="utf-8") for part in parts
    )
    assert every_byte.count(_LINE) == 20


def test_a_fallback_file_that_cannot_be_renamed_keeps_every_record(
    tmp_path, monkeypatch, capfd
) -> None:
    """The rename of ``server.<pid>.log`` itself fails: keep appending.

    No exception, no record dropped, no traceback or console line per record,
    and no rename attempt per record either -- one per cap's worth of bytes.
    """

    monkeypatch.setattr(logging_config, "_MAX_LOG_BYTES", 200)
    stub = MagicMock()
    monkeypatch.setattr(logging_config, "logger", stub)
    replace = MagicMock(side_effect=PermissionError(13, "held by someone"))
    monkeypatch.setattr(logging_config.os, "replace", replace)
    _log_path, fallback, sink = _fallback_sink(tmp_path, retain_files=1)
    sent = [f"record {index:02d} {'y' * 50}" for index in range(30)]
    try:
        for line in sent:
            sink.write(line + "\n")
    finally:
        sink.stop()

    content = fallback.read_text(encoding="utf-8")
    written = [line for line in content.splitlines() if line.startswith("record ")]
    assert written == sent
    # 30 x 61 bytes against a 200-byte cap: one attempt per ~3 records.
    assert 1 <= replace.call_count <= len(sent) * 61 // 200 + 1
    assert content.count("could not be rotated aside") == replace.call_count
    out, err = capfd.readouterr()
    assert "Traceback" not in out + err
    assert "[mcc]" not in out
    assert stub.mock_calls == []


# ------------------------------------------------------------------- G2


def test_retention_holds_while_the_server_runs(tmp_path, monkeypatch) -> None:
    """End to end through loguru: many rotations, never more than the cap."""

    monkeypatch.setattr(logging_config, "_MAX_LOG_BYTES", 600)
    pruned_on: list[str] = []
    real_prune = logging_config._prune_rotated_logs

    def recording_prune(*args, **kwargs) -> None:
        pruned_on.append(threading.current_thread().name)
        real_prune(*args, **kwargs)

    monkeypatch.setattr(logging_config, "_prune_rotated_logs", recording_prune)
    log_path = tmp_path / "server.log"

    logging_config.configure_logging(log_path, force=True, retain_files=3)
    for index in range(60):
        logger.info("runtime rotation filler {:02d}", index)
    logger.complete()

    assert len(pruned_on) >= 10, pruned_on
    assert len(_rotated(tmp_path)) == 3, _rotated(tmp_path)
    assert "runtime rotation filler 59" in log_path.read_text(encoding="utf-8")
    # On loguru's writer thread -- never the caller's, so never the event loop.
    assert threading.current_thread().name not in pruned_on


def test_retention_holds_for_the_fallback_file_too(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(logging_config, "_MAX_LOG_BYTES", 200)
    _log_path, fallback, sink = _fallback_sink(tmp_path, retain_files=2)
    try:
        for _ in range(30):
            sink.write(_LINE)
    finally:
        sink.stop()

    assert len(_rotated(tmp_path)) == 2, _rotated(tmp_path)
    assert fallback.exists()
    assert sink.active_path == fallback


def test_retain_zero_keeps_every_rotation(tmp_path, monkeypatch) -> None:
    """``SERVER_LOG_RETAIN_FILES=0`` still means keep them all."""

    monkeypatch.setattr(logging_config, "_MAX_LOG_BYTES", 200)
    log_path = tmp_path / "server.log"
    sink = logging_config._ServerLogSink(log_path, retain_files=0)
    try:
        for _ in range(20):
            sink.write(_LINE)
    finally:
        sink.stop()

    assert len(_rotated(tmp_path)) == 6


def test_this_process_active_file_is_never_pruned(tmp_path) -> None:
    log_path = tmp_path / "server.log"
    files = _aged(tmp_path, _STAMPS[:3])
    active = files[0]  # the oldest: first in line if it were a candidate

    logging_config._prune_rotated_logs(log_path, 1, exclude=active)

    assert active.exists()
    assert [path.name for path in _rotated(tmp_path)] == [_STAMPS[0], _STAMPS[2]]


def test_another_server_active_fallback_file_is_never_pruned(tmp_path) -> None:
    """``server.<pid>.log`` is somebody's active file; it is not a candidate.

    Telling a live pid from a dead one would mean probing processes, and on
    POSIX a held file CAN be unlinked -- its writer would carry on into an
    invisible inode -- so the shape alone keeps it out.
    """

    log_path = tmp_path / "server.log"
    other, *rotated = _aged(tmp_path, ["server.4242.log", *_STAMPS[:3]])

    logging_config._prune_rotated_logs(log_path, 1)

    assert other.exists()
    assert [path.name for path in _rotated(tmp_path)] == [rotated[-1].name]


def test_an_undeletable_file_is_skipped_without_raising(tmp_path, monkeypatch) -> None:
    log_path = tmp_path / "server.log"
    files = _aged(tmp_path, _STAMPS[:4])
    held = files[0]
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self == held:
            raise PermissionError(13, "being used by another process", str(self))
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    stub = MagicMock()
    monkeypatch.setattr(logging_config, "logger", stub)

    logging_config._prune_rotated_logs(log_path, 1)

    assert held.exists()
    assert not files[1].exists()
    assert not files[2].exists()
    assert files[3].exists()
    assert stub.mock_calls == []


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="POSIX unlinks a file another handle holds; there is nothing to skip",
)
def test_a_file_held_open_is_skipped_for_real_on_windows(tmp_path) -> None:
    log_path = tmp_path / "server.log"
    files = _aged(tmp_path, _STAMPS[:3])
    with files[0].open("a", encoding="utf-8"):
        logging_config._prune_rotated_logs(log_path, 1)
        assert files[0].exists()
    assert not files[1].exists()
    assert files[2].exists()
