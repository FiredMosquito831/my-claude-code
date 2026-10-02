"""Loguru-based structured logging configuration.

Structured logs are written as JSON lines to a configurable path (default
``logs/server.log``). Stdlib logging is intercepted and funneled to loguru.
Context vars (request_id, node_id, chat_id) from contextualize() are
included at top level for easy grep/filter.

Each start rotates the previous ``server.log`` aside as
``server.<timestamp>.log`` rather than truncating it, and the startup sweep
caps those together with loguru's own rotations under
``SERVER_LOG_RETAIN_FILES``. A restart is the commonest thing anyone needs a
log to explain, and until 6.58.1 a restart was what destroyed it.
"""

import contextlib
import json
import logging
import os
import re
import threading
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from loguru import logger

_configured = False
_current_path: Path | None = None
_current_level = "INFO"
_current_verbose: bool | None = None
_sink_id: int | None = None
_active_sink: _ServerLogSink | None = None
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
# on a guess. ``retain_files`` itself is capped well below this by the
# ``server_log_retain_files`` field's own range.
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


def set_third_party_verbosity(verbose: bool) -> bool:
    """Apply a saved ``LOG_RAW_API_PAYLOADS`` to the third-party loggers.

    The one part of that switch that ``configure_logging`` owns is the level
    of the noisy HTTP and Telegram loggers -- not the file sink -- and
    ``configure_logging`` already changes it alone when only the verbosity
    moved. This is that branch, callable after an admin apply without knowing
    the sink's path or level. Does nothing before logging is configured (the
    first ``configure_logging`` sets it then) and returns whether it applied.
    """

    global _current_verbose
    if not _configured:
        return False
    verbose = bool(verbose)
    if verbose != _current_verbose:
        _set_third_party_levels(verbose)
        _current_verbose = verbose
    return True


#: Loguru's own size-rotation ("50 MB") renames the current file to make room
#: for a new one. When that rename fails because another process holds the
#: file (INVESTIGATION-SELF-INFLICTED-LOAD.md §6), loguru's file sink is left
#: broken: every later record raises the same error, which loguru's default
#: handler prints as a traceback to stderr once *per record*, forever, while
#: the record itself is dropped. ``_ServerLogSink`` below does its own size
#: check and its own rename so it can fall back instead of breaking.
_MAX_LOG_BYTES = 50 * 1024 * 1024


def _open_for_append(path: Path):
    """Open a log file for append. A tiny wrapper so a long-lived handle held
    on ``self`` for the life of the sink is one obviously-intentional call
    site rather than three places that look like a forgotten ``with``."""

    return path.open("a", encoding="utf-8")


def _per_process_log_path(log_path: Path) -> Path:
    """Where THIS process logs when ``log_path`` cannot be rotated aside.

    Named by pid rather than by timestamp so two servers sharing one config
    directory never collide, and so the file is identifiable from a directory
    listing alone without opening it.
    """

    return log_path.with_name(f"{log_path.stem}.{os.getpid()}{log_path.suffix}")


def _manual_log_line(level: str, message: str) -> str:
    """One line in the same JSON shape ``_serialize_with_context`` writes.

    Used only from inside :meth:`_ServerLogSink._switch_to_fallback`, where
    calling back into ``logger`` while loguru is in the middle of calling this
    very sink's ``write`` is the kind of reentrancy that is worth avoiding
    rather than reasoning carefully about.
    """

    payload = {
        "time": datetime.now().astimezone().isoformat(),
        "level": level,
        "message": message,
        "module": "my_claude_code.config.logging_config",
        "function": "_switch_to_fallback",
        "line": 0,
    }
    return json.dumps(payload, default=str) + "\n"


class _ServerLogSink:
    """A loguru sink that falls back instead of breaking.

    Behaves like loguru's own file sink with ``rotation="50 MB"`` -- append,
    rotate the old file aside past the size cap -- except that when the
    rotation rename fails because another process holds ``log_path`` (Windows
    ``WinError 32``), this process switches its own writes to
    ``server.<pid>.log`` beside it and keeps going, rather than dropping every
    subsequent record. ``SERVER_LOG_RETAIN_FILES`` still bounds these through
    :func:`_sweep_rotated_logs`, which globs the same ``{stem}.*{suffix}``
    pattern loguru's own rotated files use.
    """

    def __init__(self, initial_path: Path, *, nominal_path: Path | None = None) -> None:
        # The *nominal* path is always ``server.log``, even when
        # ``initial_path`` is already a per-process fallback (a startup
        # rotation that lost the race) -- so a later size rotation that also
        # falls back names the file ``server.<pid>.log`` once, not twice.
        self._nominal_path = nominal_path if nominal_path is not None else initial_path
        self._active_path = initial_path
        self._fallback = initial_path != self._nominal_path
        self._fh = _open_for_append(Path(initial_path))
        self._size = self._fh.tell()

    @property
    def active_path(self) -> Path:
        return self._active_path

    def write(self, message: object) -> None:
        data = str(message)
        encoded_len = len(data.encode("utf-8"))
        if not self._fallback and self._size + encoded_len > _MAX_LOG_BYTES:
            self._rotate()
        self._fh.write(data)
        self._fh.flush()
        self._size += encoded_len

    def _rotate(self) -> None:
        current_path = self._active_path
        self._fh.close()
        target = _rotated_name(current_path)
        try:
            os.replace(current_path, target)
        except OSError:
            self._switch_to_fallback()
            return
        self._fh = _open_for_append(Path(current_path))
        self._size = 0

    def _switch_to_fallback(self) -> None:
        fallback_path = _per_process_log_path(self._nominal_path)
        self._fh = _open_for_append(fallback_path)
        self._size = self._fh.tell()
        self._active_path = fallback_path
        self._fallback = True
        notice = (
            f"{self._nominal_path.name} passed {_MAX_LOG_BYTES // (1024 * 1024)} MB "
            f"and could not be rotated aside (another process holds it); this "
            f"process is now logging to {fallback_path.name} instead."
        )
        print(f"[mcc] {notice}")
        self._fh.write(_manual_log_line("WARNING", notice))
        self._fh.flush()

    def flush(self) -> None:
        with suppress(OSError):
            self._fh.flush()

    def stop(self) -> None:
        with suppress(OSError):
            self._fh.close()


def _add_file_sink(
    log_file: str | Path,
    level: str,
    retain_files: int,
    *,
    nominal_path: Path | None = None,
) -> tuple[int, _ServerLogSink]:
    log_path = Path(log_file)
    sink = _ServerLogSink(log_path, nominal_path=nominal_path)
    sink_id = logger.add(
        sink,
        level=level,
        format=_serialize_with_context,
        enqueue=True,
    )
    return sink_id, sink


def append_to_server_log(log_file: str | Path, level: str, message: str) -> bool:
    """Write one line into the server log without reconfiguring logging.

    For the handful of things a server has to say *before* it is a server.
    The 6.30.0 refusal is the one that matters: it ends the process before
    the composition root exists, so until 6.65.0 it was written nowhere at
    all -- a refused start left an empty config directory, and the desktop
    app's error page named a ``server_log`` that did not exist.

    Deliberately NOT ``configure_logging``. That function owns the process's
    logging: it removes every existing sink, replaces ``logging.root``'s
    handlers and rotates the previous log. Calling it from a guard that may
    run inside somebody else's process -- a test, an embedded runner -- takes
    their handlers away from them. This adds one sink, writes one line and
    removes it again, in the same format the real sink uses so the line is
    readable by whatever reads the rest of the file.

    Returns whether the line was written; a log that cannot be opened is
    never a reason to fail differently.
    """

    try:
        log_path = Path(log_file).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        sink_id = logger.add(
            log_path,
            level=level,
            format=_serialize_with_context,
            encoding="utf-8",
            mode="a",
            enqueue=False,
        )
    except OSError, ValueError:
        return False
    try:
        logger.log(level, message)
    finally:
        with contextlib.suppress(ValueError):
            logger.remove(sink_id)
    return True


def _rotated_name(log_path: Path) -> Path:
    """A ``{stem}.<timestamp>{suffix}`` name that does not exist yet.

    Matches the ``{stem}.*{suffix}`` glob loguru's own rotation uses, so the
    startup sweep caps these and loguru-style rotations together under one
    ``SERVER_LOG_RETAIN_FILES``.
    """

    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
    target = log_path.with_name(f"{log_path.stem}.{stamp}{log_path.suffix}")
    # A second start inside the same microsecond is not a thing, but a clock
    # that went backwards is: never overwrite a log that is already there.
    counter = 1
    while target.exists():
        target = log_path.with_name(
            f"{log_path.stem}.{stamp}-{counter}{log_path.suffix}"
        )
        counter += 1
    return target


#: What :func:`_rotate_current_log` found.
ROTATE_EMPTY = "empty"  #: nothing to rotate (no file, or a zero-byte one).
ROTATE_OK = "rotated"  #: the previous log was moved aside successfully.
ROTATE_HELD = "held"  #: another process holds it; the rename failed.


def _rotate_current_log(log_path: Path) -> tuple[str, Path | None]:
    """Move an existing ``server.log`` aside so the new run starts a fresh one.

    Until 6.58.1 the line here was ``log_path.write_text("")``: every server
    start destroyed the previous run's log. That is why a report of "the app
    hung and I restarted it" could only ever be reconstructed from database
    rows and file mtimes -- the one file that would have said what happened had
    been emptied by the very restart being investigated.

    Returns ``(ROTATE_OK, target)`` on success, ``(ROTATE_EMPTY, None)`` when
    there was nothing to rotate, or ``(ROTATE_HELD, None)`` when the rename
    failed because another process holds ``log_path`` (Windows ``WinError
    32``). The ``ROTATE_HELD`` case used to fall back to
    ``log_path.write_text("")`` -- silently emptying the very log a reader
    would go looking for (INVESTIGATION-HEALTH-PING-UNDER-LOAD.md §4). It no
    longer touches ``log_path`` at all; the caller switches this process to a
    per-process file instead.
    """

    try:
        if not log_path.is_file() or log_path.stat().st_size == 0:
            return ROTATE_EMPTY, None
    except OSError:
        return ROTATE_EMPTY, None
    target = _rotated_name(log_path)
    try:
        os.replace(log_path, target)
    except OSError:
        return ROTATE_HELD, None
    return ROTATE_OK, target


def _sweep_rotated_logs(
    log_path: Path, retain_files: int, *, exclude: Path | None = None
) -> None:
    """Delete rotated ``server.*.log`` files beyond ``retain_files``.

    The current log file (``log_path`` itself) is never touched -- it never
    matches the ``{stem}.*{suffix}`` glob below, which requires an extra
    ``.``-separated segment -- and neither does ``exclude``, which is this
    process's own active file when a startup or size rotation fell back to a
    per-process ``server.<pid>.log`` (that per-process name DOES match the
    glob, so without this it would be a candidate for deletion by its own
    writer). ``retain_files`` of the newest remaining files are kept; only
    older ones are removed. Loguru's own ``retention`` only prunes as it
    rotates, so a directory that already holds more files than the cap (an
    earlier install left ~340 rotated files) keeps them until each one is
    rotated past again -- the sweep fixes that on the next startup. Logs each
    deletion so the cap is observable.
    """

    if retain_files <= 0:
        return
    stem = log_path.stem
    suffix = log_path.suffix
    try:
        rotated = sorted(
            (
                candidate
                for candidate in log_path.parent.glob(f"{stem}.*{suffix}")
                if exclude is None or candidate != exclude
            ),
            key=lambda candidate: candidate.stat().st_mtime,
        )
    except OSError as exc:
        logger.warning("Rotated-log sweep could not list {}: {}", log_path.parent, exc)
        return
    # The guard is on what we *found*, not on the cap. Until 6.41.1 this read
    # ``if retain_files >= _MAX_ROTATED_FILES`` -- a comparison between a small
    # user setting and 100,000, so it could never fire and the "refuse to
    # bulk-delete on a guess" protection the comment promised did not exist.
    if len(rotated) >= _MAX_ROTATED_FILES:
        logger.warning(
            "Rotated-log sweep skipped: {} holds {} files matching {}.*{}, "
            "which is more likely a wrong directory than a retention problem. "
            "Nothing was deleted.",
            log_path.parent,
            len(rotated),
            stem,
            suffix,
        )
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

    The previous ``server.log`` is rotated aside rather than truncated, so a
    restart no longer destroys the evidence of what happened before it.

    ``retain_files`` caps the number of rotated ``server.*.log`` files kept;
    ``0`` keeps them all. The cap is applied both as loguru's rotation retention
    and by a startup sweep, so a directory that already holds more rotated files
    than the cap is trimmed on the next start rather than only as each one rotates
    past. The current log file is never deleted.

    When ``verbose_third_party`` is false, noisy HTTP and Telegram loggers are
    capped at WARNING unless explicitly configured otherwise.
    """
    global _configured, _current_path, _current_level, _current_verbose, _sink_id
    global _active_sink

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

        outcome, rotated = _rotate_current_log(log_path)

        if outcome == ROTATE_HELD:
            # Do NOT truncate (INVESTIGATION-HEALTH-PING-UNDER-LOAD.md §4):
            # the existing content stays exactly where it is, and THIS
            # process writes its own run to a sibling file instead.
            initial_path = _per_process_log_path(log_path)
            held_notice = (
                f"{log_path.name} is held by another process; this process "
                f"is logging to {initial_path.name} instead."
            )
            print(f"[mcc] {held_notice}")
        else:
            initial_path = log_path

        _sink_id, _active_sink = _add_file_sink(
            initial_path, level, retain_files, nominal_path=log_path
        )

        if outcome == ROTATE_OK:
            # Said in the new log, because the whole point of keeping the old
            # one is that somebody will go looking for it later.
            logger.info("Previous server log rotated to {}.", rotated)
        elif outcome == ROTATE_HELD:
            logger.warning(held_notice)

        intercept = InterceptHandler()
        logging.root.handlers = [intercept]
        logging.root.setLevel(logging.DEBUG)

        _set_third_party_levels(verbose_third_party)
    elif log_path != _current_path or level != _current_level:
        if _sink_id is not None:
            logger.remove(_sink_id)
        _sink_id, _active_sink = _add_file_sink(log_path, level, retain_files)
        if verbose_third_party != _current_verbose:
            _set_third_party_levels(verbose_third_party)
    else:
        _set_third_party_levels(verbose_third_party)

    active_path = _active_sink.active_path if _active_sink is not None else log_path
    _sweep_rotated_logs(log_path, retain_files, exclude=active_path)

    _current_path = log_path
    _current_level = level
    _current_verbose = verbose_third_party


def current_active_log_path() -> Path | None:
    """The file this process is actually writing to right now.

    Usually the same as the nominal ``server.log`` path; differs only while a
    startup or size-based rotation fell back to a per-process
    ``server.<pid>.log`` beside it. ``None`` before ``configure_logging`` has
    run.
    """

    if _active_sink is not None:
        return _active_sink.active_path
    return _current_path
