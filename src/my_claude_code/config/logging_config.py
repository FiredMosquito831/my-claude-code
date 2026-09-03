"""Loguru-based structured logging configuration.

Structured logs are written as JSON lines to a configurable path (default
``logs/server.log``). Stdlib logging is intercepted and funneled to loguru.
Context vars (request_id, node_id, chat_id) from contextualize() are
included at top level for easy grep/filter.
"""

import json
import logging
import re
import threading
from pathlib import Path

from loguru import logger

_configured = False
_current_path: Path | None = None
_current_level = "INFO"
_current_verbose: bool | None = None
_sink_id: int | None = None
# Default number of rotated ``server.*.log`` files to keep. ``0`` keeps them
# all. Applied both when loguru rotates and by the startup sweep below.
_default_retain_files = 10

_THIRD_PARTY_LOGGERS = (
    "httpx",
    "httpcore",
    "httpcore.http11",
    "httpcore.connection",
    "telegram",
    "telegram.ext",
)

# Loguru ``logger.bind()`` key used by structured TRACE payloads; ``core/trace.py``
# uses the identical string constant ``TRACE_PAYLOAD_BINDING``.
_TRACE_PAYLOAD_BINDING = "trace_payload"

# Beyond this many rotated files the sweep is skipped entirely: a directory with
# an enormous number of files is more likely a misconfiguration (wrong glob,
# wrong directory) than a real retention problem, and we refuse to bulk-delete
# on a guess. ``retain_files`` itself is capped well below this.
_MAX_ROTATED_FILES = 100_000

# Context keys we promote to top-level JSON for traceability / grep
_CONTEXT_KEYS = (
    "request_id",
    "node_id",
    "chat_id",
    "claude_session_id",
    "http_method",
    "http_path",
)

_TELEGRAM_BOT_RE = re.compile(
    r"(https?://api\.telegram\.org/)bot([0-9]+:[A-Za-z0-9_-]+)(/?)",
    re.IGNORECASE,
)
# Authorization: Bearer <token> (HTTP client / proxy debug lines)
_AUTH_BEARER_RE = re.compile(
    r"(\bAuthorization\s*:\s*Bearer\s+)([^\s'\"]+)",
    re.IGNORECASE,
)


def _redact_sensitive_substrings(message: str) -> str:
    """Remove obvious API tokens and secrets before JSON log line emission."""
    text = _TELEGRAM_BOT_RE.sub(r"\1bot<redacted>\3", message)
    return _AUTH_BEARER_RE.sub(r"\1<redacted>", text)


def _serialize_with_context(record) -> str:
    """Format record as JSON with context vars at top level.
    Returns a format template; we inject _json into record for output.
    """
    extra = record.get("extra", {})
    out = {
        "time": str(record["time"]),
        "level": record["level"].name,
        "message": _redact_sensitive_substrings(str(record["message"])),
        "module": record["name"],
        "function": record["function"],
        "line": record["line"],
    }
    trace_payload = extra.get(_TRACE_PAYLOAD_BINDING)
    for key in _CONTEXT_KEYS:
        if key in extra and extra[key] is not None:
            out[key] = extra[key]
    if isinstance(trace_payload, dict):
        for tk, tv in trace_payload.items():
            if tk in out:
                continue
            out[tk] = tv
        out["trace"] = True
    record["_json"] = json.dumps(out, default=str)
    return "{_json}\n"


class InterceptHandler(logging.Handler):
    """Redirect stdlib logging to loguru."""

    def __init__(self) -> None:
        super().__init__()
        self._local = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._local, "active", False):
            # Avoid deadlock when nested stdlib records fire during a loguru emit.
            return
        self._local.active = True
        try:
            try:
                level = logger.level(record.levelname).name
            except ValueError:
                level = record.levelno

            frame, depth = logging.currentframe(), 2
            while frame is not None and frame.f_code.co_filename == logging.__file__:
                frame = frame.f_back
                depth += 1

            logger.opt(depth=depth, exception=record.exc_info).log(
                level, record.getMessage()
            )
        finally:
            self._local.active = False


def _set_third_party_levels(verbose: bool) -> None:
    level = logging.NOTSET if verbose else logging.WARNING
    for name in _THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(level)


def _add_file_sink(log_file: str | Path, level: str, retain_files: int) -> int:
    log_path = Path(log_file)
    # ``retain_files`` is the number of rotated ``server.*.log`` files to keep.
    # ``0`` means keep them all, which loguru expresses as ``retention=None``.
    retention: int | None = retain_files if retain_files > 0 else None
    return logger.add(
        log_path,
        level=level,
        format=_serialize_with_context,
        encoding="utf-8",
        mode="a",
        rotation="50 MB",
        retention=retention,
        enqueue=True,
    )


def _sweep_rotated_logs(log_path: Path, retain_files: int) -> None:
    """Delete rotated ``server.*.log`` files beyond ``retain_files``.

    The current log file (``log_path`` itself) is never touched, and ``retain_files``
    of the newest rotated files are kept; only older ones are removed. Loguru's
    own ``retention`` only prunes as it rotates, so a directory that already holds
    more files than the cap (an earlier install left ~340 rotated files) keeps
    them until each one is rotated past again -- the sweep fixes that on the
    next startup. Logs each deletion so the cap is observable.
    """

    if retain_files <= 0:
        return
    if retain_files >= _MAX_ROTATED_FILES:
        return
    stem = log_path.stem
    suffix = log_path.suffix
    try:
        rotated = sorted(
            log_path.parent.glob(f"{stem}.*{suffix}"),
            key=lambda candidate: candidate.stat().st_mtime,
        )
    except OSError as exc:
        logger.warning("Rotated-log sweep could not list {}: {}", log_path.parent, exc)
        return
    excess = len(rotated) - retain_files
    if excess <= 0:
        return
    for old in rotated[:excess]:
        try:
            old.unlink()
        except OSError as exc:
            logger.warning("Rotated-log sweep could not remove {}: {}", old, exc)
            continue
        logger.info("Removed rotated log {} (retain {} files).", old, retain_files)


def configure_logging(
    log_file: str | Path,
    *,
    force: bool = False,
    verbose_third_party: bool = False,
    level: str = "INFO",
    retain_files: int = _default_retain_files,
) -> None:
    """Configure loguru with JSON output to log_file and intercept stdlib logging.

    Idempotent: skips if already configured with the same path, level, and verbosity.
    On path or level change, replaces only the file sink without truncating.
    On verbosity change alone, updates only the third-party logger levels.
    Use force=True to reconfigure from scratch.

    ``retain_files`` caps the number of rotated ``server.*.log`` files kept;
    ``0`` keeps them all. The cap is applied both as loguru's rotation retention
    and by a startup sweep, so a directory that already holds more rotated files
    than the cap is trimmed on the next start rather than only as each one rotates
    past. The current log file is never deleted.

    When ``verbose_third_party`` is false, noisy HTTP and Telegram loggers are
    capped at WARNING unless explicitly configured otherwise.
    """
    global _configured, _current_path, _current_level, _current_verbose, _sink_id

    retain_files = max(0, int(retain_files))
    log_path = Path(log_file).expanduser().resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if (
        _configured
        and not force
        and log_path == _current_path
        and level == _current_level
        and verbose_third_party == _current_verbose
    ):
        return

    if not _configured or force:
        _configured = True

        logger.remove()

        log_path.write_text("")

        _sink_id = _add_file_sink(log_path, level, retain_files)

        intercept = InterceptHandler()
        logging.root.handlers = [intercept]
        logging.root.setLevel(logging.DEBUG)

        _set_third_party_levels(verbose_third_party)
    elif log_path != _current_path or level != _current_level:
        if _sink_id is not None:
            logger.remove(_sink_id)
        _sink_id = _add_file_sink(log_path, level, retain_files)
        if verbose_third_party != _current_verbose:
            _set_third_party_levels(verbose_third_party)
    else:
        _set_third_party_levels(verbose_third_party)

    _sweep_rotated_logs(log_path, retain_files)

    _current_path = log_path
    _current_level = level
    _current_verbose = verbose_third_party
