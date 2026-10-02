"""Size-based rotation when another process holds the file (item 4b).

INVESTIGATION-SELF-INFLICTED-LOAD.md §6: past 50 MB, loguru's own
``rotation="50 MB"`` closes the current file and renames it; when that rename
fails because another process holds it, the sink is left broken -- every later
record raises the same error, which loguru's default handler prints as a
traceback to stderr *per record*, forever, while the record is dropped.
``_ServerLogSink._rotate`` catches exactly that failure and switches this
process's own writes to ``server.<pid>.log`` instead, so records keep being
written rather than lost.
"""

from pathlib import Path
from unittest.mock import patch

from my_claude_code.config import logging_config


def test_a_rename_failure_during_size_rotation_falls_back_without_raising(
    tmp_path,
) -> None:
    log_path = tmp_path / "server.log"
    sink = logging_config._ServerLogSink(log_path)
    try:
        sink.write("short\n")
        with (
            patch.object(
                logging_config.os, "replace", side_effect=OSError("WinError 32")
            ),
            patch.object(logging_config, "_MAX_LOG_BYTES", 20),
        ):
            # This write's length alone pushes the running size over the
            # (patched, tiny) cap, triggering a rotation attempt; the rename
            # is forced to fail exactly as it would when another process
            # holds the file.
            sink.write("this line overflows the twenty byte cap\n")
            # A second write must not raise either -- once in fallback, the
            # sink stays there rather than retrying every write.
            sink.write("a second line\n")
    finally:
        sink.stop()

    assert sink.active_path != log_path
    assert sink.active_path.name.startswith("server.")
    # Nothing was dropped: the line that triggered the fallback, and the one
    # after it, both made it to disk.
    fallback_content = sink.active_path.read_text(encoding="utf-8")
    assert "this line overflows the twenty byte cap" in fallback_content
    assert "a second line" in fallback_content
    assert "another process holds it" in fallback_content
    # And the content already on server.log before the failed rotation is
    # untouched -- not truncated, not lost.
    assert log_path.read_text(encoding="utf-8") == "short\n"


def test_a_successful_size_rotation_still_rotates_the_old_file_aside(
    tmp_path,
) -> None:
    """The happy path -- nothing holds the file -- still rotates as before."""
    log_path = tmp_path / "server.log"
    sink = logging_config._ServerLogSink(log_path)
    try:
        sink.write("short\n")
        with patch.object(logging_config, "_MAX_LOG_BYTES", 20):
            sink.write("this line overflows the twenty byte cap\n")
    finally:
        sink.stop()

    assert sink.active_path == log_path
    rotated = sorted(Path(tmp_path).glob("server.*.log"))
    assert len(rotated) == 1, rotated
    assert rotated[0].read_text(encoding="utf-8") == "short\n"
    assert "this line overflows the twenty byte cap" in log_path.read_text(
        encoding="utf-8"
    )
