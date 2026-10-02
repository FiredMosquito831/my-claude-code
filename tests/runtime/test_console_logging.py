"""Console timestamps (item 2) and the quiet-poll access filter (item 3).

``runtime/console_logging.py`` is the non-frozen module ``cli/commands.py``
hands to ``uvicorn.Config(log_config=...)`` as its one-line hook. These tests
exercise it directly, with fabricated ``logging.LogRecord``s, rather than
starting a real server.
"""

import logging

from my_claude_code.runtime.console_logging import (
    QuietPollFilter,
    build_uvicorn_log_config,
)


def _access_record(method: str, path: str, status: int) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:54321", method, path, "1.1", status),
        exc_info=None,
    )


def test_build_uvicorn_log_config_adds_a_timestamp_to_both_formatters() -> None:
    config = build_uvicorn_log_config()
    assert config["formatters"]["default"]["fmt"].startswith("%(asctime)s ")
    assert config["formatters"]["access"]["fmt"].startswith("%(asctime)s ")
    assert config["formatters"]["default"]["datefmt"] == "%Y-%m-%d %H:%M:%S"
    assert config["formatters"]["access"]["datefmt"] == "%Y-%m-%d %H:%M:%S"


def test_build_uvicorn_log_config_attaches_the_quiet_filter_to_access_only() -> None:
    config = build_uvicorn_log_config()
    assert "quiet_poll" in config["handlers"]["access"].get("filters", [])
    assert "quiet_poll" not in config["handlers"]["default"].get("filters", [])


def test_quiet_filter_drops_a_successful_health_poll() -> None:
    record = _access_record("GET", "/health", 200)
    assert QuietPollFilter().filter(record) is False


def test_quiet_filter_keeps_a_failing_health_poll() -> None:
    """A non-2xx answer on a quiet path is exactly what the console is for."""
    record = _access_record("GET", "/health", 503)
    assert QuietPollFilter().filter(record) is True


def test_quiet_filter_keeps_a_v1_line() -> None:
    record = _access_record("POST", "/v1/messages", 200)
    assert QuietPollFilter().filter(record) is True


def test_quiet_filter_keeps_an_admin_write() -> None:
    record = _access_record("POST", "/admin/api/config/apply", 200)
    assert QuietPollFilter().filter(record) is True


def test_quiet_filter_keeps_a_401() -> None:
    record = _access_record("GET", "/admin/api/requests/pulse", 401)
    assert QuietPollFilter().filter(record) is True


def test_quiet_filter_drops_the_in_flight_poll_with_a_query_string() -> None:
    record = _access_record("GET", "/admin/api/requests/in-flight?limit=50", 200)
    assert QuietPollFilter().filter(record) is False
