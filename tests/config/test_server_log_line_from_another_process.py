"""``write_server_log_line``: one JSON line in server.log from the desktop host (7.70.0).

The host is a separate process; what it tells the user must land in the file
the user and the desktop app already read, in the shape the real sink writes,
and without passing through loguru (whose default stderr sink would print the
sentence a second time on a terminal host).
"""

import json

from loguru import logger

from my_claude_code.config.logging_config import (
    _serialize_with_context,
    write_server_log_line,
)


def test_the_line_has_the_keys_the_real_sink_writes(tmp_path) -> None:
    path = tmp_path / "logs" / "server.log"

    assert write_server_log_line(path, "warning", "hello", module="m", function="f")

    [line] = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    assert record["level"] == "WARNING"
    assert record["message"] == "hello"
    assert record["module"] == "m"
    assert record["function"] == "f"
    # The same top-level keys a loguru-written line carries.
    captured: list[dict] = []

    def sink(message) -> None:
        _serialize_with_context(message.record)
        captured.append(json.loads(message.record["_json"]))

    sink_id = logger.add(sink, level="INFO")
    try:
        logger.info("x")
    finally:
        logger.remove(sink_id)
    assert set(captured[0]) <= set(record) | {"request_id"}
    assert {"time", "level", "message", "module", "function", "line"} <= set(record)


def test_secrets_are_redacted_as_the_real_sink_does(tmp_path) -> None:
    path = tmp_path / "server.log"

    write_server_log_line(
        path, "WARNING", "Authorization: Bearer sk-secret-123", module="m"
    )

    assert "sk-secret-123" not in path.read_text(encoding="utf-8")


def test_it_appends_and_never_truncates(tmp_path) -> None:
    path = tmp_path / "server.log"
    path.write_text('{"message":"earlier"}\n', encoding="utf-8")

    write_server_log_line(path, "WARNING", "later", module="m")

    lines = path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["message"] == "earlier"
    assert json.loads(lines[1])["message"] == "later"


def test_a_log_that_cannot_be_opened_is_reported_not_raised(tmp_path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")

    assert (
        write_server_log_line(blocker / "server.log", "WARNING", "m", module="m")
        is False
    )


def test_nothing_reaches_any_loguru_sink(tmp_path) -> None:
    seen: list[str] = []
    sink_id = logger.add(lambda message: seen.append(str(message)), level="DEBUG")
    try:
        write_server_log_line(tmp_path / "server.log", "WARNING", "quiet", module="m")
    finally:
        logger.remove(sink_id)
    assert seen == []
