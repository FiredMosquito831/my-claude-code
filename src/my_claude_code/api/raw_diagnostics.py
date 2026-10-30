"""One request's raw diagnostics, read back out of the log files (7.121.0).

Four settings write material that names a request id:

* ``LOG_RAW_API_PAYLOADS`` -- one ``RAW_PAYLOAD`` line per request in
  ``raw-diagnostics.log``: the payload as a Python repr, not JSON;
* ``LOG_RAW_SSE_EVENTS`` -- one ``RAW_SSE`` line per server-sent event: MCC's
  own stream to the client, and, on the Anthropic Messages and OpenAI
  Responses doors, the provider's own frames as well;
* ``LOG_API_ERROR_TRACEBACKS`` -- ERROR lines in ``server.log`` carrying the
  request id, with the message and the traceback when it was on and the
  exception type alone when it was off;
* ``LOG_RAW_CLI_DIAGNOSTICS`` -- ``RAW_CLI`` lines, which in practice carry
  ``-``: the managed CLI runs outside any request.

Nothing indexes either file, so every read scans the whole retained window --
the active file and every rotated or per-process sibling -- oldest first. That
is the point, not a shortcut: the user's rule for this viewer is that nothing
recorded is lost, and no early stop is sound when several servers can share a
directory and a request's lines can sit files apart (its payload line before
seven minutes of other traffic, its frames after). Measured on a 546 MB window:
about 0.4 s warm.

**What bounds memory is paging, never discarding.** A page holds at most
``PAGE_MAX_LINES`` lines or ``PAGE_MAX_BYTES`` bytes and always at least one
whole line; the rest of the request's lines are counted, hashed and left on
disk. A page carries a digest of every line before it, so the next page can
tell that rotation deleted lines in between and refuse instead of silently
re-numbering. A scan stops only when it has read the window or at a hard
physical limit (``SCAN_DEADLINE_SECONDS``, ``LINE_LIMIT_BYTES``), and then it
refuses, naming the limit -- it never returns part of the answer as the whole.
The only honest absence is a request whose lines rotation already pruned, and
that is said in so many words.

Every byte is returned as it was written: no redaction is applied on the way
out (``server.log`` lines were redacted when they were written; raw lines never
are), and the exports read through this same module, so a file holds exactly
what the viewer showed.
"""

import contextlib
import hashlib
import json
import re
import stat
import tempfile
import time
from array import array
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from my_claude_code.config.admin.manifest import FIELD_BY_KEY, SECTIONS
from my_claude_code.config.logging_config import (
    RAW_DIAGNOSTICS_LOG_FILENAME,
    RAW_MARKER_CLI,
    RAW_MARKER_PAYLOAD,
    RAW_MARKER_SSE,
    log_rotation_bytes,
    raw_diagnostics_log_path,
    server_log_nominal_path,
)

KIND_PAYLOADS = "payloads"
KIND_SSE = "sse"
KIND_TRACEBACK = "traceback"
KIND_CLI = "cli"

#: Box order in the request detail, and row order in every export.
RAW_KINDS: tuple[str, ...] = (KIND_PAYLOADS, KIND_SSE, KIND_TRACEBACK, KIND_CLI)

VIEW_RAW = "raw"
VIEW_NORMALIZED = "normalized"
VIEWS: tuple[str, ...] = (VIEW_RAW, VIEW_NORMALIZED)

#: The four honest capture states (spec §3). ``present`` has lines; the other
#: three never fill a page with anything but the sentence that explains them.
CAPTURE_PRESENT = "present"
CAPTURE_NEVER = "absent_never"
CAPTURE_ROTATED = "absent_rotated"
CAPTURE_SETTING = "absent_setting"

#: A page: at most this many lines, or this many bytes, whichever comes first,
#: and always at least one whole line. Paging is what bounds memory; nothing
#: beyond a page is ever dropped, only left for the next one.
PAGE_MAX_LINES = 4_000
PAGE_MAX_BYTES = 8 * 1024 * 1024

#: One physical line larger than this is never read into memory whole: the
#: viewer shows a marker saying where it is, and an export refuses rather than
#: write a row without it. Measured worst line on a real 50 MB file: 2.5 MB.
LINE_LIMIT_BYTES = 64 * 1024 * 1024

#: A scan that has not read the whole retained window by now stops and
#: refuses. A scan-cost bound, never a data cut: no partial answer leaves it.
SCAN_DEADLINE_SECONDS = 30.0

#: How many times a read is retried when rotation renamed or deleted a file
#: while it ran; past that it refuses rather than answer from a moving window.
WINDOW_ATTEMPTS = 3

#: Excel's own limit on one cell. An XLSX export that would need a longer cell
#: refuses and names the line rather than write a cell Excel would cut.
XLSX_CELL_LIMIT = 32_767

#: The dashboard page each settings section is rendered on. A mirror of the
#: ``VIEW_GROUPS`` table in admin.js -- the server needs it to say where a
#: setting is in an export, which the browser never sees --
#: and ``tests/api/test_raw_diagnostics_reader.py`` pins it to that table.
SETTINGS_PAGE_FOR_SECTION: dict[str, str] = {"diagnostics": "Limits & Resilience"}

SERVER_LOG_FAMILY = "server"
RAW_LOG_FAMILY = "raw"


@dataclass(frozen=True, slots=True)
class RawKind:
    """One box: where its raw lines come from and which setting writes them."""

    kind: str
    label: str
    setting: str
    marker: str | None
    family: str
    noun: str


KINDS: dict[str, RawKind] = {
    KIND_PAYLOADS: RawKind(
        KIND_PAYLOADS,
        "API payloads",
        "LOG_RAW_API_PAYLOADS",
        RAW_MARKER_PAYLOAD,
        RAW_LOG_FAMILY,
        "API payloads",
    ),
    KIND_SSE: RawKind(
        KIND_SSE,
        "SSE events",
        "LOG_RAW_SSE_EVENTS",
        RAW_MARKER_SSE,
        RAW_LOG_FAMILY,
        "raw SSE events",
    ),
    KIND_TRACEBACK: RawKind(
        KIND_TRACEBACK,
        "API error tracebacks",
        "LOG_API_ERROR_TRACEBACKS",
        None,
        SERVER_LOG_FAMILY,
        "API error tracebacks",
    ),
    KIND_CLI: RawKind(
        KIND_CLI,
        "CLI diagnostics",
        "LOG_RAW_CLI_DIAGNOSTICS",
        RAW_MARKER_CLI,
        RAW_LOG_FAMILY,
        "raw CLI diagnostics",
    ),
}

_MARKER_KINDS: dict[bytes, str] = {
    RAW_MARKER_PAYLOAD.encode(): KIND_PAYLOADS,
    RAW_MARKER_SSE.encode(): KIND_SSE,
    RAW_MARKER_CLI.encode(): KIND_CLI,
}

#: ``server.log``'s own file name, for the sentences that name it.
_SERVER_LOG_NAME = "server.log"

#: The request id ``api.request_ids.new_request_id`` mints. Used to find a
#: request's ERROR lines in ``server.log`` with one pass whatever the number of
#: requests asked about; an id of another shape falls back to a substring test.
_REQUEST_ID_SHAPE = re.compile(rb"req_[0-9a-f]{32}")

#: MCC's own Anthropic stream to the client, as ``AnthropicSseEmitter`` logs
#: it: ``SSE_EVENT: <type> - event: <type>\n...`` (the newline escaped by the
#: sink). Everything else under the SSE marker is a provider's own frame -- the
#: Messages door's ``SSE_EVENT: <provider> event: ...`` and the Responses
#: door's ``SSE_EVENT: responses_event {...}``.
_EGRESS_SSE = re.compile(rb"^SSE_EVENT: ([A-Za-z_]+) - event: \1(?:\\n|$)")

_TRACEBACK_TEXT = "Traceback (most recent call last)"
_EXC_TYPE_ONLY = re.compile(r"\bexc_type=\w")

#: Up to this many request ids, ``raw-diagnostics.log`` is searched for each
#: id's own bytes; beyond it (an Analytics export of up to 200 requests), the
#: scan steps from line start to line start instead of reading every block
#: once per id.
_NEEDLE_IDS = 8


class RawScanRefused(Exception):
    """A hard limit stopped a read: say which, and return nothing partial."""

    def __init__(self, reason: str, message: str, *, status: int = 503) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.status = status


class RawWindowChanged(Exception):
    """Rotation renamed or deleted a file while it was being read."""


@dataclass(frozen=True, slots=True)
class LogFile:
    """One file of a log family, as listed before it is read."""

    path: Path
    size: int
    mtime: float


@dataclass(frozen=True, slots=True)
class RawSources:
    """The two nominal files a read starts from; their siblings are found by name."""

    raw_log: Path | None
    server_log: Path | None


@dataclass(frozen=True, slots=True)
class RawSettings:
    """The live settings a read reports against (never their secrets)."""

    enabled: Mapping[str, bool]
    retain_files: int


@dataclass(slots=True)
class RawLine:
    """One recorded line of one request.

    ``text`` is the line's message -- what follows ``<MARKER> <request_id>`` in
    ``raw-diagnostics.log``, or the whole JSON line in ``server.log`` -- exactly
    as written, without its line terminator. It is ``None`` only for a line
    longer than ``LINE_LIMIT_BYTES``, whose first bytes are kept in ``head`` so
    its digest still tells it from its neighbours.
    """

    kind: str
    request_id: str
    text: bytes | None
    nbytes: int
    file: str
    offset: int
    partial: bool = False
    head: bytes = b""
    tag: str = ""


@dataclass(slots=True)
class ScanStats:
    """What a read cost, for the answer to show rather than hide."""

    started: float = field(default_factory=time.monotonic)
    files: int = 0
    bytes_read: int = 0
    window: dict[str, list[LogFile]] = field(default_factory=dict)
    uncorrelated_cli: int = 0

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)


def current_sources() -> RawSources:
    """Where this server writes, as the logging setup adopted it.

    ``None`` for a file this process never configured (an app built without
    the composition root, as the tests build it): such a read finds nothing,
    which every caller reports as an honest absence.
    """

    return RawSources(
        raw_log=raw_diagnostics_log_path(), server_log=server_log_nominal_path()
    )


def raw_settings_from(settings: Any) -> RawSettings:
    """The four switches and the retention count, read from live settings."""

    enabled: dict[str, bool] = {}
    for kind, spec in KINDS.items():
        attr = FIELD_BY_KEY[spec.setting].settings_attr or ""
        enabled[kind] = bool(getattr(settings, attr, False))
    return RawSettings(
        enabled=enabled,
        retain_files=max(0, int(getattr(settings, "server_log_retain_files", 0))),
    )


# --------------------------------------------------------------------------
# The window: the files a family has, and reading them line by line.
# --------------------------------------------------------------------------


def log_family(nominal: Path | None) -> list[LogFile]:
    """The nominal file and every ``<stem>.*<suffix>`` sibling, oldest first.

    Rotated parts (``<stem>.<timestamp>.log``), another process's fallback
    (``<stem>.<pid>.log``) and its rotations all match, exactly the set the
    sink's own retention sweep counts. Ordered by modification time, which
    for a file nobody writes any more is the moment it was rotated aside.
    """

    if nominal is None:
        return []
    candidates: list[Path] = [nominal]
    with contextlib.suppress(OSError):
        candidates.extend(nominal.parent.glob(f"{nominal.stem}.*{nominal.suffix}"))
    files: list[LogFile] = []
    seen: set[str] = set()
    for path in candidates:
        if path.name in seen:
            continue
        seen.add(path.name)
        try:
            info = path.stat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        files.append(LogFile(path=path, size=info.st_size, mtime=info.st_mtime))
    files.sort(key=lambda item: (item.mtime, item.path.name))
    return files


def _rotated_files(nominal: Path | None, files: list[LogFile]) -> list[LogFile]:
    """The rotated parts only: neither the nominal file nor a per-process one.

    The same two exclusions the sink's pruning makes, so "the retention cap may
    have pruned something" is judged on the files that cap counts.
    """

    if nominal is None:
        return []
    active_shape = re.compile(
        rf"{re.escape(nominal.stem)}\.\d+{re.escape(nominal.suffix)}"
    )
    return [
        item
        for item in files
        if item.path.name != nominal.name and not active_shape.fullmatch(item.path.name)
    ]


#: How much of a file one search step reads. Memory for a scan is this plus
#: the longest whole line it meets (at most ``LINE_LIMIT_BYTES``).
_BLOCK_BYTES = 4 * 1024 * 1024

#: ``(buffer, end) -> positions`` of the bytes worth looking at in
#: ``buffer[:end]``; every position names the line that contains it.
Finder = Callable[[bytes, int], list[int]]


def _needle_finder(needles: Collection[bytes]) -> Finder:
    """Every position of any of ``needles``: the block scan's whole speed.

    ``bytes.find`` runs at memory speed (the spec measured 0.036 s for 50 MB),
    so a request's few lines are found without walking the file line by line.
    """

    wanted = [needle for needle in needles if needle]

    def find(buffer: bytes, end: int) -> list[int]:
        found: list[int] = []
        for needle in wanted:
            at = buffer.find(needle, 0, end)
            while at >= 0:
                found.append(at)
                at = buffer.find(needle, at + len(needle), end)
        found.sort()
        return found

    return find


def _line_start_finder(prefixes: Collection[bytes]) -> Finder:
    """The start of every line that begins with one of ``prefixes``.

    For many request ids at once (an Analytics export), where one search per id
    would read the block once per id: raw lines are large on average, so
    stepping from line start to line start is cheap.
    """

    wanted = tuple(prefixes)

    def find(buffer: bytes, end: int) -> list[int]:
        found: list[int] = []
        at = 0
        while at < end:
            if buffer.startswith(wanted, at):
                found.append(at)
            newline = buffer.find(b"\n", at, end)
            if newline < 0:
                break
            at = newline + 1
        return found

    return find


def _strip_cr(body: bytes) -> bytes:
    return body[:-1] if body.endswith(b"\r") else body


def _candidate_lines(
    path: Path,
    find: Finder,
    limit: int,
    stats: ScanStats,
    deadline: float | None,
    total: int,
) -> Iterator[tuple[int, bytes, int, bool, bool]]:
    """``(offset, head, nbytes, whole, partial)`` for each line ``find`` points into.

    The file is read in ``_BLOCK_BYTES`` steps and only the lines a position
    falls in are cut out. ``head`` is the line without its terminator when it
    fits in ``limit``, otherwise its first ``limit`` bytes while the rest is
    skipped without being kept. ``partial`` marks a final line with no
    terminator -- being written this instant, or cut by a crash -- which is
    still recorded data, so it is yielded, not dropped.
    """

    with path.open("rb") as handle:
        pending = b""
        pending_offset = 0
        while True:
            block = handle.read(_BLOCK_BYTES)
            if block:
                stats.bytes_read += len(block)
                _check_deadline(deadline, stats, total)
            if not block:
                if pending:
                    body = _strip_cr(pending)
                    if find(body, len(body)):
                        yield pending_offset, body, len(body), True, True
                return
            buffer = pending + block if pending else block
            buffer_offset = pending_offset
            last_newline = buffer.rfind(b"\n")
            if last_newline < 0:
                if len(buffer) <= limit:
                    pending = buffer
                    continue
                # One line longer than the limit: keep its head, skip the rest.
                head = buffer[:limit]
                candidate = bool(find(head, len(head)))
                consumed = len(buffer)
                tail = buffer[-1:]
                while True:
                    block = handle.read(_BLOCK_BYTES)
                    if not block:
                        if candidate:
                            yield buffer_offset, head, consumed, False, True
                        return
                    stats.bytes_read += len(block)
                    _check_deadline(deadline, stats, total)
                    newline = block.find(b"\n")
                    if newline < 0:
                        consumed += len(block)
                        tail = block[-1:]
                        continue
                    crlf = (
                        block[newline - 1 : newline] == b"\r"
                        if newline > 0
                        else tail == b"\r"
                    )
                    nbytes = consumed + newline - (1 if crlf else 0)
                    if candidate:
                        yield buffer_offset, head, nbytes, False, False
                    pending = block[newline + 1 :]
                    pending_offset = buffer_offset + consumed + newline + 1
                    break
                continue
            complete = last_newline + 1
            next_line = 0
            for at in find(buffer, complete):
                if at < next_line:
                    continue
                line_start = buffer.rfind(b"\n", 0, at) + 1
                line_end = buffer.find(b"\n", at, complete)
                body = _strip_cr(buffer[line_start:line_end])
                if len(body) <= limit:
                    yield buffer_offset + line_start, body, len(body), True, False
                else:
                    yield (
                        buffer_offset + line_start,
                        body[:limit],
                        len(body),
                        False,
                        False,
                    )
                next_line = line_end + 1
            pending = buffer[complete:]
            pending_offset = buffer_offset + complete


def _names(nominal: Path | None) -> set[str]:
    return {item.path.name for item in log_family(nominal)}


def _check_deadline(deadline: float | None, stats: ScanStats, total: int) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise RawScanRefused(
            "deadline",
            f"Reading the retained log window stopped after {SCAN_DEADLINE_SECONDS:g} s "
            f"with {stats.bytes_read:,} of {total:,} bytes read, so nothing is shown "
            "rather than part of it. The disk is slow or the window is very large "
            "(SERVER_LOG_RETAIN_FILES); try again, or keep fewer rotated files.",
            status=503,
        )


def _scan_family(
    family: str,
    nominal: Path | None,
    find: Finder,
    match: Callable[[bytes, int, bool], Iterator[RawLine]],
    stats: ScanStats,
    deadline: float | None,
) -> Iterator[RawLine]:
    files = log_family(nominal)
    stats.window[family] = files
    before = {item.path.name for item in files}
    total = sum(item.size for item in files)
    for item in files:
        stats.files += 1
        try:
            for offset, head, nbytes, whole, partial in _candidate_lines(
                item.path, find, LINE_LIMIT_BYTES, stats, deadline, total
            ):
                for line in match(head, nbytes, whole):
                    line.file = item.path.name
                    line.offset = offset
                    line.partial = partial
                    yield line
        except FileNotFoundError:
            # Pruned between the listing and the read: start again from a
            # fresh listing rather than answer as if the file never existed.
            raise RawWindowChanged from None
    _check_deadline(deadline, stats, total)
    if _names(nominal) - before:
        # A rotation happened while this ran: the active file now has another
        # name, and the lines written since may be in a file not read here.
        raise RawWindowChanged


def _raw_matcher(
    ids: set[bytes], kinds: set[str], stats: ScanStats
) -> Callable[[bytes, int, bool], Iterator[RawLine]]:
    count_uncorrelated = KIND_CLI in kinds
    cli_marker = RAW_MARKER_CLI.encode()

    def match(head: bytes, nbytes: int, whole: bool) -> Iterator[RawLine]:
        first = head.find(b" ")
        if first <= 0:
            return
        kind = _MARKER_KINDS.get(head[:first])
        if kind is None or kind not in kinds:
            return
        second = head.find(b" ", first + 1)
        if second < 0:
            return
        request_id = head[first + 1 : second]
        if request_id not in ids:
            if count_uncorrelated and request_id == b"-" and head[:first] == cli_marker:
                stats.uncorrelated_cli += 1
            return
        message = head[second + 1 :]
        size = nbytes - (second + 1)
        tag = ""
        if kind == KIND_SSE:
            tag = "egress" if _EGRESS_SSE.match(message) else "upstream"
        yield RawLine(
            kind=kind,
            request_id=request_id.decode("utf-8", "replace"),
            text=message if whole else None,
            nbytes=size,
            file="",
            offset=0,
            head=b"" if whole else message[:4096],
            tag=tag,
        )

    return match


def _server_matcher(ids: set[bytes]) -> Callable[[bytes, int, bool], Iterator[RawLine]]:
    canonical = all(_REQUEST_ID_SHAPE.fullmatch(rid) for rid in ids)

    def match(head: bytes, nbytes: int, whole: bool) -> Iterator[RawLine]:
        if canonical:
            found = {rid for rid in _REQUEST_ID_SHAPE.findall(head) if rid in ids}
        else:
            found = {rid for rid in ids if rid in head}
        if not found:
            return
        record: Any = None
        if whole:
            try:
                record = json.loads(head)
            except ValueError:
                record = None
        if isinstance(record, dict):
            level = str(record.get("level", ""))
            if level not in ("ERROR", "CRITICAL"):
                return
            message = str(record.get("message", ""))
            owner = record.get("request_id")
            tag = (
                "traceback"
                if _TRACEBACK_TEXT in message
                else "exc_type"
                if _EXC_TYPE_ONLY.search(message)
                else "error"
            )
            for rid in sorted(found):
                text = rid.decode("utf-8", "replace")
                if owner == text or f"request_id={text}" in message:
                    yield RawLine(
                        kind=KIND_TRACEBACK,
                        request_id=text,
                        text=head,
                        nbytes=nbytes,
                        file="",
                        offset=0,
                        tag=tag,
                    )
            return
        # A line that is not whole JSON -- longer than the line limit, or cut
        # mid-write -- cannot be parsed; it is kept when it plainly says ERROR.
        if b'"level": "ERROR"' not in head and b'"level": "CRITICAL"' not in head:
            return
        for rid in sorted(found):
            yield RawLine(
                kind=KIND_TRACEBACK,
                request_id=rid.decode("utf-8", "replace"),
                text=head if whole else None,
                nbytes=nbytes,
                file="",
                offset=0,
                head=b"" if whole else head[:4096],
                tag="error",
            )

    return match


def read_raw_lines(
    sources: RawSources,
    request_ids: Collection[str],
    kinds: Collection[str],
    *,
    stats: ScanStats | None = None,
    deadline: float | None = None,
) -> Iterator[RawLine]:
    """Every recorded line of these requests and kinds, in the order written.

    THE reader: the request detail's viewer, the per-request export and the
    Analytics export's Raw diagnostics group all read through this function,
    so they cannot disagree about what a request's raw material is. Lines of
    ``raw-diagnostics.log`` kinds come first, file by file, oldest first; then
    the ``server.log`` ERROR lines of the traceback kind the same way.

    Raises :class:`RawWindowChanged` after the last line when rotation moved
    files under the read (callers read again), and :class:`RawScanRefused` at a
    hard limit -- never a quietly shorter answer.
    """

    stats = stats if stats is not None else ScanStats()
    wanted = set(kinds)
    ids = {request_id.encode("utf-8") for request_id in request_ids}
    raw_kinds = wanted & {KIND_PAYLOADS, KIND_SSE, KIND_CLI}
    if raw_kinds:
        if len(ids) <= _NEEDLE_IDS:
            needles = [b" " + request_id + b" " for request_id in ids]
            if KIND_CLI in raw_kinds:
                needles.append(RAW_MARKER_CLI.encode() + b" - ")
            find = _needle_finder(needles)
        else:
            find = _line_start_finder(
                [
                    marker + b" "
                    for marker, kind in _MARKER_KINDS.items()
                    if kind in raw_kinds
                ]
            )
        yield from _scan_family(
            RAW_LOG_FAMILY,
            sources.raw_log,
            find,
            _raw_matcher(ids, raw_kinds, stats),
            stats,
            deadline,
        )
    if KIND_TRACEBACK in wanted:
        # ERROR lines are rare, so they are what the scan looks for; the id is
        # checked on the few it finds. MCC writes every server.log line with
        # ``json.dumps``' own separators, so the key is spelled exactly so.
        yield from _scan_family(
            SERVER_LOG_FAMILY,
            sources.server_log,
            _needle_finder([b'"level": "ERROR"', b'"level": "CRITICAL"']),
            _server_matcher(ids),
            stats,
            deadline,
        )


def _digest_input(line: RawLine) -> bytes:
    if line.text is not None:
        return line.text + b"\n"
    return b"<over-limit:%d:" % line.nbytes + line.head + b">\n"


def _decode(data: bytes) -> str:
    return data.decode("utf-8", "replace")


# --------------------------------------------------------------------------
# Wording: every sentence the viewer and the exports show, written once.
# --------------------------------------------------------------------------


def _section_label(section_id: str) -> str:
    for section in SECTIONS:
        if section.section_id == section_id:
            return section.label
    return section_id


def setting_place(kind: str) -> str:
    """``Limits & Resilience → Diagnostics → "Log Raw SSE Events"``."""

    spec = FIELD_BY_KEY[KINDS[kind].setting]
    page = SETTINGS_PAGE_FOR_SECTION.get(spec.section_id, "the dashboard")
    return f"{page} → {_section_label(spec.section_id)} → “{spec.label}”"


def setting_applies(kind: str) -> str:
    """When a saved change takes effect, from the manifest's own restart facts."""

    spec = FIELD_BY_KEY[KINDS[kind].setting]
    if not spec.restart_required:
        return "Save applies it — no restart."
    if spec.restart_scope == "messaging":
        return (
            "Save applies it to API requests at once; only a running Telegram or "
            "Discord bot keeps the old value until it restarts."
        )
    return "It takes effect after a server restart."


def kind_header(kind: str, settings: RawSettings) -> dict[str, Any]:
    """The facts every answer about one kind starts with."""

    spec = KINDS[kind]
    field_spec = FIELD_BY_KEY[spec.setting]
    return {
        "kind": kind,
        "label": spec.label,
        "setting": spec.setting,
        "setting_title": field_spec.label,
        "setting_place": setting_place(kind),
        "applies": setting_applies(kind),
        "enabled": bool(settings.enabled.get(kind, False)),
        "source": source_label(kind),
        "pretty_print": kind == KIND_PAYLOADS,
    }


def source_label(kind: str) -> str:
    spec = KINDS[kind]
    if spec.family == SERVER_LOG_FAMILY:
        return f"{_SERVER_LOG_NAME} · ERROR lines with this request id"
    return f"{RAW_DIAGNOSTICS_LOG_FILENAME} · {spec.marker} lines"


def _local_time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().strftime("%Y-%m-%d %H:%M")


def _ago(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{int(seconds)} s"
    minutes = seconds / 60
    if minutes < 90:
        return f"{round(minutes)} minutes"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f} hours"
    return f"{hours / 24:.1f} days"


@dataclass(slots=True)
class KindFacts:
    """What one read found for one kind of one request."""

    lines: int = 0
    bytes: int = 0
    egress: int = 0
    upstream: int = 0
    tracebacks: int = 0
    exc_type_only: int = 0
    partial: list[int] = field(default_factory=list)
    over_limit: list[int] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    first_file: str | None = None

    def add(self, line: RawLine) -> None:
        self.lines += 1
        self.bytes += line.nbytes
        if line.tag == "egress":
            self.egress += 1
        elif line.tag == "upstream":
            self.upstream += 1
        elif line.tag == "traceback":
            self.tracebacks += 1
        elif line.tag == "exc_type":
            self.exc_type_only += 1
        if line.partial:
            self.partial.append(self.lines)
        if line.text is None:
            self.over_limit.append(self.lines)
        if not self.files or self.files[-1] != line.file:
            self.files.append(line.file)
        if self.first_file is None:
            self.first_file = line.file


def window_facts(
    family: str,
    nominal: Path | None,
    files: list[LogFile],
    settings: RawSettings,
    now: float,
) -> dict[str, Any]:
    """The retained window, in numbers the sentences quote."""

    rotated = _rotated_files(nominal, files)
    oldest = files[0].mtime if files else None
    retain = settings.retain_files
    return {
        "file": RAW_DIAGNOSTICS_LOG_FILENAME
        if family == RAW_LOG_FAMILY
        else _SERVER_LOG_NAME,
        "files": len(files),
        "bytes": sum(item.size for item in files),
        "rotated_files": len(rotated),
        "retain_files": retain,
        "rotate_mb": log_rotation_bytes() // (1024 * 1024),
        "oldest_modified": _local_time(oldest) if oldest is not None else None,
        "oldest_age_seconds": None if oldest is None else max(0.0, now - oldest),
        # The retention cap only ever deletes rotated parts, and only once it
        # holds as many as it keeps; 0 keeps them all.
        "pruning_possible": bool(rotated) and retain > 0 and len(rotated) >= retain,
        "_oldest_epoch": oldest,
    }


def _older_than_window(window: Mapping[str, Any], request_ts: float | None) -> bool:
    oldest = window.get("_oldest_epoch")
    return (
        request_ts is not None
        and oldest is not None
        and bool(window.get("pruning_possible"))
        and request_ts < oldest
    )


def capture_state(
    kind: str,
    facts: KindFacts,
    *,
    enabled: bool,
    window: Mapping[str, Any],
    row: Mapping[str, Any] | None,
) -> tuple[str, str]:
    """``(capture, reason)``: present, or which of the three absences it is."""

    if facts.lines:
        return CAPTURE_PRESENT, "lines"
    if kind == KIND_CLI:
        # CLI lines carry ``-``, never a request id: nothing of this request
        # was ever in the file to be pruned, so "pruned by rotation" would be
        # untrue however old the request is.
        if not enabled:
            return CAPTURE_SETTING, "setting_off"
        return CAPTURE_NEVER, "not_request_correlated"
    request_ts = _row_ts(row)
    if window.get("files") and _older_than_window(window, request_ts):
        return CAPTURE_ROTATED, "pruned"
    if kind == KIND_TRACEBACK:
        if row is not None and not _row_failed(row):
            return CAPTURE_NEVER, "no_error"
        if not enabled:
            return CAPTURE_SETTING, "setting_off"
        return CAPTURE_NEVER, "no_error_line" if row is not None else "no_row"
    if not enabled:
        return CAPTURE_SETTING, "setting_off"
    if not window.get("files"):
        return CAPTURE_NEVER, "no_file"
    if row is None:
        return CAPTURE_NEVER, "no_row"
    return CAPTURE_NEVER, "not_recorded"


def _row_ts(row: Mapping[str, Any] | None) -> float | None:
    if row is None:
        return None
    value = row.get("ts_epoch")
    return float(value) if isinstance(value, (int, float)) else None


def _row_failed(row: Mapping[str, Any]) -> bool:
    return (
        bool(row.get("error_kind") or row.get("error_message"))
        or str(row.get("status") or "") == "error"
    )


def absent_text(
    kind: str,
    capture: str,
    reason: str,
    *,
    window: Mapping[str, Any],
    row: Mapping[str, Any] | None,
    uncorrelated_cli: int = 0,
    normalized: bool = False,
) -> tuple[str, str]:
    """``(headline, message)`` for a box that has no raw lines to show."""

    spec = KINDS[kind]
    setting = f"`{spec.setting}`"
    place = setting_place(kind)
    applies = setting_applies(kind)
    only_normalized = (
        " What this request did is stored only in normalised form — switch to "
        "Normalized."
        if normalized
        else ""
    )
    if capture == CAPTURE_ROTATED:
        return (
            "No raw lines remain.",
            f"{window.get('file')} keeps {window.get('retain_files')} rotated files of "
            f"{window.get('rotate_mb')} MB (`SERVER_LOG_RETAIN_FILES`); the oldest one "
            f"kept was last written {window.get('oldest_modified')} "
            f"({_ago(float(window.get('oldest_age_seconds') or 0.0))} ago) and this "
            "request started before that, so any lines it wrote were in a file "
            "rotation has since deleted. The normalised text is unaffected."
            + only_normalized,
        )
    if reason == "setting_off":
        tail = ""
        if kind == KIND_CLI:
            tail = (
                " Managed CLI output is not tied to a request id, so even with it on "
                "this box can only show CLI lines logged inside this request."
            )
        elif kind == KIND_TRACEBACK:
            tail = (
                " A provider's classified failure has no traceback either way; "
                "Normalized shows what the request log stored."
            )
        return (
            "Not captured.",
            f"{setting} is off, so no {spec.noun} are being written, and none were "
            f"found for this request. Turn it on in {place}, make the request again, "
            f"then reopen this box. ({applies}){tail}{only_normalized}",
        )
    if reason == "not_request_correlated":
        return (
            "Not tied to this request.",
            "MCC's managed Claude CLI runs outside any HTTP request, so its raw lines "
            "carry `-` instead of a request id and cannot be matched to this one. "
            f"They are in {RAW_DIAGNOSTICS_LOG_FILENAME} under the {RAW_MARKER_CLI} "
            f"marker ({uncorrelated_cli:,} such lines in the retained window), and "
            f"its stderr is in {_SERVER_LOG_NAME} (`Claude CLI stderr:`).",
        )
    if reason == "no_error":
        status = str((row or {}).get("status") or "no error")
        return (
            "No error logged.",
            f"This request did not fail ({status}), so {_SERVER_LOG_NAME} holds no "
            "error line for it.",
        )
    if reason == "no_error_line":
        failed = str((row or {}).get("error_kind") or "an error")
        return (
            "No error line names this request.",
            f"It failed ({failed}), but {_SERVER_LOG_NAME} holds no ERROR line "
            "carrying its id. MCC writes one for an unexpected failure; a provider's "
            "classified failure is stored in the request log instead — Normalized "
            "shows it.",
        )
    if reason == "no_file":
        return (
            "Not captured for this request.",
            f"{setting} is on, but {RAW_DIAGNOSTICS_LOG_FILENAME} does not exist yet: "
            "nothing has been written since it was turned on. Make the request again, "
            f"then reopen this box.{only_normalized}",
        )
    if reason == "no_row":
        return (
            "Not captured for this request.",
            "No raw line carries this request id, and the request log has no row for "
            "it, so whether it is older than the retained window cannot be told.",
        )
    return (
        "Not captured for this request.",
        f"{setting} is on now, but no {spec.noun} were written for this request: "
        "it was off when the request ran, or the request produced none."
        + only_normalized,
    )


def present_notes(
    kind: str,
    facts: KindFacts,
    *,
    enabled: bool,
    window: Mapping[str, Any],
    row: Mapping[str, Any] | None,
) -> list[str]:
    """What a reader must know about lines that are there."""

    spec = KINDS[kind]
    notes: list[str] = []
    if not enabled:
        notes.append(
            f"`{spec.setting}` is off now, so new requests are not recorded; the lines "
            "below were written while it was on."
        )
    if _older_than_window(window, _row_ts(row)):
        notes.append(
            f"This request started before the oldest {window.get('file')} kept (last "
            f"written {window.get('oldest_modified')}): any lines it wrote before that "
            "file began were pruned by rotation and no longer exist."
        )
    if kind == KIND_SSE:
        if facts.upstream == 0:
            notes.append(
                "These are the frames MCC sent to the client — its own re-encoding of "
                "the model's output. No upstream frame was recorded for this request: "
                "only the Anthropic Messages and OpenAI Responses doors log the "
                "provider's own frames, and on OpenAI Chat Completions and Gemini the "
                "provider's raw bytes are never captured."
            )
        else:
            notes.append(
                f"{facts.upstream:,} of these lines are the provider's own frames as "
                f"received; {facts.egress:,} are MCC's stream to the client."
            )
    if kind == KIND_PAYLOADS:
        notes.append(
            "Each line is the payload as a Python repr, not JSON. “Pretty-print as "
            "JSON” converts it in your browser by reading it as a literal; nothing is "
            "evaluated."
        )
    if kind == KIND_TRACEBACK and facts.tracebacks == 0 and facts.exc_type_only:
        notes.append(
            f"`{spec.setting}` was off when this request failed, so "
            f"{_SERVER_LOG_NAME} holds the exception type only. Turn it on in "
            f"{setting_place(kind)} to record the message and the traceback next "
            f"time. ({setting_applies(kind)})"
        )
    elif kind == KIND_TRACEBACK and facts.exc_type_only:
        which = (
            "one of these lines records"
            if facts.exc_type_only == 1
            else (f"{facts.exc_type_only} of these lines record")
        )
        notes.append(
            f"{which} the exception type only, because `{spec.setting}` was off "
            f"for that error; the traceback lines above were written while it was "
            "on."
        )
    if facts.partial:
        numbers = ", ".join(f"{n:,}" for n in facts.partial[:12])
        notes.append(
            f"Line {numbers} ends mid-line: the file was being written at that moment, "
            "or a crash cut it short. It is shown exactly as found."
        )
    if facts.over_limit:
        numbers = ", ".join(f"{n:,}" for n in facts.over_limit[:12])
        notes.append(
            f"Line {numbers} is larger than the {LINE_LIMIT_BYTES // (1024 * 1024)} MB "
            "one line may hold in memory here, so it is marked in place with its file "
            "and byte offset instead of shown. Nothing was cut from the file."
        )
    return notes


def over_limit_marker(n: int, line: RawLine) -> str:
    return (
        f"⟦line {n:,}: {line.nbytes:,} bytes, above the "
        f"{LINE_LIMIT_BYTES // (1024 * 1024)} MB one line may hold here — not shown, "
        f"nothing was cut. It is in {line.file} at byte {line.offset:,}.⟧"
    )


def _public_window(window: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in window.items() if not key.startswith("_")}


def _family_of(kind: str) -> str:
    return KINDS[kind].family


def _nominal_for(sources: RawSources, family: str) -> Path | None:
    return sources.raw_log if family == RAW_LOG_FAMILY else sources.server_log


def _window_for(
    sources: RawSources,
    stats: ScanStats,
    family: str,
    settings: RawSettings,
    now: float,
) -> dict[str, Any]:
    nominal = _nominal_for(sources, family)
    files = stats.window.get(family)
    if files is None:
        files = log_family(nominal)
    return window_facts(family, nominal, files, settings, now)


def _with_retries[T](read: Callable[[], T]) -> T:
    for _attempt in range(WINDOW_ATTEMPTS):
        try:
            return read()
        except RawWindowChanged:
            continue
    raise RawScanRefused(
        "rotating",
        f"The log rotated {WINDOW_ATTEMPTS} times while it was being read, so no "
        "answer is given rather than one from a moving window. Try again in a moment.",
        status=409,
    )


def _refused(base: dict[str, Any], refusal: RawScanRefused) -> dict[str, Any]:
    return {
        **base,
        "lines": [],
        "refused": {"reason": refusal.reason, "message": refusal.message},
    }


# --------------------------------------------------------------------------
# The viewer: a summary of every kind, one page of one kind, or its
# normalised form.
# --------------------------------------------------------------------------


def raw_summary(
    sources: RawSources,
    request_id: str,
    row: Mapping[str, Any] | None,
    settings: RawSettings,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Counts and states for all four kinds, from one read of each family."""

    moment = time.time() if now is None else now

    def read() -> tuple[dict[str, KindFacts], ScanStats]:
        stats = ScanStats()
        facts = {kind: KindFacts() for kind in RAW_KINDS}
        deadline = time.monotonic() + SCAN_DEADLINE_SECONDS
        for line in read_raw_lines(
            sources, [request_id], RAW_KINDS, stats=stats, deadline=deadline
        ):
            facts[line.kind].add(line)
        return facts, stats

    base: dict[str, Any] = {"request_id": request_id, "kinds": []}
    try:
        facts, stats = _with_retries(read)
    except RawScanRefused as refusal:
        return _refused(base, refusal)
    kinds: list[dict[str, Any]] = []
    for kind in RAW_KINDS:
        window = _window_for(sources, stats, _family_of(kind), settings, moment)
        kinds.append(
            _kind_answer(
                kind,
                facts[kind],
                settings,
                window,
                row,
                uncorrelated_cli=stats.uncorrelated_cli,
            )
        )
    return {
        **base,
        "kinds": kinds,
        "windows": {
            family: _public_window(
                _window_for(sources, stats, family, settings, moment)
            )
            for family in (RAW_LOG_FAMILY, SERVER_LOG_FAMILY)
        },
        "scanned_bytes": stats.bytes_read,
        "files_scanned": stats.files,
        "elapsed_ms": stats.elapsed_ms(),
        "refused": None,
    }


def _kind_answer(
    kind: str,
    facts: KindFacts,
    settings: RawSettings,
    window: Mapping[str, Any],
    row: Mapping[str, Any] | None,
    *,
    uncorrelated_cli: int,
) -> dict[str, Any]:
    header = kind_header(kind, settings)
    enabled = bool(header["enabled"])
    capture, reason = capture_state(
        kind, facts, enabled=enabled, window=window, row=row
    )
    normalized = normalized_available(row, kind)
    answer: dict[str, Any] = {
        **header,
        "capture": capture,
        "reason": reason,
        "lines_total": facts.lines,
        "bytes_total": facts.bytes,
        "files": list(facts.files),
        "normalized_available": normalized,
        "window": _public_window(window),
    }
    if capture == CAPTURE_PRESENT:
        answer["headline"] = None
        answer["message"] = None
        answer["notes"] = present_notes(
            kind, facts, enabled=enabled, window=window, row=row
        )
    else:
        headline, message = absent_text(
            kind,
            capture,
            reason,
            window=window,
            row=row,
            uncorrelated_cli=uncorrelated_cli,
            normalized=normalized,
        )
        answer["headline"] = headline
        answer["message"] = message
        answer["notes"] = []
    if kind == KIND_SSE:
        answer["frames"] = {"egress": facts.egress, "upstream": facts.upstream}
    return answer


def raw_page(
    sources: RawSources,
    request_id: str,
    kind: str,
    row: Mapping[str, Any] | None,
    settings: RawSettings,
    *,
    start: int = 0,
    anchor: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """One page of one kind's lines, with everything the page needs to be honest.

    ``start`` is the 0-based index of the first line wanted and ``anchor`` the
    ``anchor`` the previous page returned: a digest of every line before
    ``start``. A mismatch means rotation deleted lines in between since that
    page was read -- the page that would follow cannot be lined up with what is
    on screen, so it is refused rather than served under the wrong numbers.
    """

    moment = time.time() if now is None else now
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind!r}")

    def read() -> dict[str, Any]:
        stats = ScanStats()
        facts = KindFacts()
        running = hashlib.sha256()
        deadline = time.monotonic() + SCAN_DEADLINE_SECONDS
        page: list[dict[str, Any]] = []
        page_bytes = 0
        next_start: int | None = None
        next_anchor: str | None = None
        start_digest: str | None = None
        index = 0
        for line in read_raw_lines(
            sources, [request_id], [kind], stats=stats, deadline=deadline
        ):
            if index == start:
                start_digest = running.hexdigest()
            facts.add(line)
            if index >= start and next_start is None:
                cost = line.nbytes if line.text is not None else 0
                if page and (
                    len(page) >= PAGE_MAX_LINES or page_bytes + cost > PAGE_MAX_BYTES
                ):
                    next_start = index
                    next_anchor = running.hexdigest()
                else:
                    page.append(_line_entry(index + 1, line))
                    page_bytes += cost
            running.update(_digest_input(line))
            index += 1
        if start_digest is None and start == index:
            start_digest = running.hexdigest()
        return {
            "facts": facts,
            "stats": stats,
            "page": page,
            "next_start": next_start,
            "next_anchor": next_anchor,
            "start_digest": start_digest,
            "total": index,
        }

    base: dict[str, Any] = {
        "request_id": request_id,
        "view": VIEW_RAW,
        **kind_header(kind, settings),
        "start": start,
    }
    try:
        result = _with_retries(read)
    except RawScanRefused as refusal:
        return _refused(base, refusal)
    stats: ScanStats = result["stats"]
    facts: KindFacts = result["facts"]
    if start > 0 and (anchor or "") != (result["start_digest"] or ""):
        return _refused(
            base,
            RawScanRefused(
                "moved",
                "The log rotated while you were paging and lines before this page "
                "were pruned, so the next page cannot be lined up with what is "
                "shown. Close this box and open it again to read what remains.",
                status=409,
            ),
        )
    window = _window_for(sources, stats, _family_of(kind), settings, moment)
    answer = _kind_answer(
        kind,
        facts,
        settings,
        window,
        row,
        uncorrelated_cli=stats.uncorrelated_cli,
    )
    return {
        **base,
        **answer,
        "lines": result["page"],
        "next_start": result["next_start"],
        "anchor": result["next_anchor"],
        "scanned_bytes": stats.bytes_read,
        "files_scanned": stats.files,
        "elapsed_ms": stats.elapsed_ms(),
        "refused": None,
    }


def _line_entry(n: int, line: RawLine) -> dict[str, Any]:
    entry: dict[str, Any] = {"n": n, "bytes": line.nbytes}
    if line.text is None:
        entry["text"] = None
        entry["over_limit"] = True
        entry["marker"] = over_limit_marker(n, line)
        entry["file"] = line.file
        entry["offset"] = line.offset
    else:
        entry["text"] = _decode(line.text)
    if line.partial:
        entry["partial"] = True
        entry["file"] = line.file
        entry["offset"] = line.offset
    return entry


# --------------------------------------------------------------------------
# Normalised material: what the request log stored, never a raw byte.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Block:
    label: str
    source: str
    text: str | None
    state: str
    note: str = ""


def _text_block(
    row: Mapping[str, Any], label: str, column: str, chars_column: str
) -> Block:
    value = row.get(column)
    if isinstance(value, str) and value:
        return Block(label, f"requests.{column}", value, "stored")
    chars = row.get(chars_column)
    if isinstance(chars, int) and chars > 0:
        return Block(
            label,
            f"requests.{column}",
            None,
            "not_stored",
            f"{chars:,} characters were recorded but not stored "
            "(REQUEST_LOG_CAPTURE_BODIES=false).",
        )
    return Block(label, f"requests.{column}", None, "none")


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _chain_attempts(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    attempts = row.get("route_attempts")
    if not isinstance(attempts, list):
        return []
    found: list[Mapping[str, Any]] = []
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            continue
        params = attempt.get("params")
        if isinstance(params, Mapping) and params.get("kind") == "describe":
            continue
        found.append(attempt)
    return found


def normalized_blocks(row: Mapping[str, Any] | None, kind: str) -> list[Block]:
    """The request log's own fields for one kind, labelled with where each lives."""

    if row is None or kind == KIND_CLI:
        return []
    if kind == KIND_PAYLOADS:
        blocks = [
            _text_block(
                row,
                "Prompt text (system and message text blocks)",
                "input_text",
                "input_chars",
            )
        ]
        for attempt in _chain_attempts(row):
            body = attempt.get("wire_body")
            if body is None:
                continue
            number = int(attempt.get("attempt") or 0) + 1
            model = attempt.get("model_ref") or "—"
            blocks.append(
                Block(
                    f"Request body sent, attempt {number} · {model} (no prompt text)",
                    "request_attempts.wire_body",
                    _json_text(body),
                    "stored",
                )
            )
        return blocks
    if kind == KIND_SSE:
        blocks = [
            _text_block(row, "Response text", "output_text", "output_chars"),
            _text_block(row, "Reasoning", "thinking_text", "thinking_chars"),
        ]
        calls = row.get("tool_calls")
        if isinstance(calls, list) and calls:
            blocks.append(
                Block("Tool calls", "requests.tool_calls", _json_text(calls), "stored")
            )
        elif row.get("tool_call_count"):
            blocks.append(
                Block(
                    "Tool calls",
                    "requests.tool_calls",
                    None,
                    "not_stored",
                    "Arguments were recorded but not stored "
                    "(REQUEST_LOG_CAPTURE_BODIES=false).",
                )
            )
        return blocks
    blocks = []
    if row.get("error_kind") or row.get("error_message"):
        blocks.append(
            Block(
                "Error",
                "requests.error_kind, requests.error_message",
                f"{row.get('error_kind') or ''}: {row.get('error_message') or ''}",
                "stored",
            )
        )
    for attempt in _chain_attempts(row):
        if not (attempt.get("error_kind") or attempt.get("error_message")):
            continue
        number = int(attempt.get("attempt") or 0) + 1
        blocks.append(
            Block(
                f"Attempt {number} · {attempt.get('model_ref') or '—'} · "
                f"{attempt.get('outcome') or ''}",
                "request_attempts.error_kind, request_attempts.error_message",
                f"{attempt.get('error_kind') or ''}: {attempt.get('error_message') or ''}",
                "stored",
            )
        )
    return blocks


def normalized_available(row: Mapping[str, Any] | None, kind: str) -> bool:
    return any(block.text for block in normalized_blocks(row, kind))


def normalized_text(blocks: Iterable[Block]) -> str:
    """The one text the Normalized view shows and its Copy button copies."""

    parts: list[str] = []
    for block in blocks:
        header = f"── {block.label} · {block.source} ──"
        body = block.text if block.text else f"({block.note or 'nothing stored'})"
        parts.append(f"{header}\n{body}")
    return "\n\n".join(parts)


def normalized_view(
    request_id: str, kind: str, row: Mapping[str, Any] | None, settings: RawSettings
) -> dict[str, Any]:
    """The Normalized side of a box. Reads the request log row only."""

    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind!r}")
    blocks = normalized_blocks(row, kind)
    stored = [block for block in blocks if block.text]
    if row is None:
        state, headline, message = (
            "no_row",
            "No request row.",
            "The request log has no row for this request, so there is no normalised "
            "form to show.",
        )
    elif kind == KIND_CLI:
        state, headline, message = (
            "none",
            "No normalised form.",
            "MCC keeps no normalised form of CLI output; only the raw view exists "
            "for it.",
        )
    elif not stored and kind == KIND_TRACEBACK:
        state, headline, message = (
            "none",
            "No error recorded.",
            "The request log stored no error for this request.",
        )
    elif not stored:
        state, headline, message = (
            "none",
            "Nothing stored.",
            "The request log holds nothing of this kind for this request."
            if not any(block.state == "not_stored" for block in blocks)
            else "The request log recorded sizes for this request but not the text "
            "(REQUEST_LOG_CAPTURE_BODIES=false).",
        )
    else:
        state, headline, message = ("stored", None, None)
    return {
        "request_id": request_id,
        "view": VIEW_NORMALIZED,
        **kind_header(kind, settings),
        "state": state,
        "headline": headline,
        "message": message,
        "blocks": [
            {
                "label": block.label,
                "source": block.source,
                "state": block.state,
                "chars": len(block.text) if block.text else 0,
                "note": block.note,
            }
            for block in blocks
        ],
        "text": normalized_text(blocks) if stored else "",
        "refused": None,
    }


# --------------------------------------------------------------------------
# Exports: the same lines, copied once to a temporary file, then streamed.
# --------------------------------------------------------------------------


def _open_spool_file() -> IO[bytes]:
    """A temporary file that lives as long as the export streaming from it.

    A tiny wrapper so the one long-lived handle -- closed by
    :meth:`RawSpool.close` when the last row has been sent, or the client went
    away -- is a single, obviously intentional call site. Deleted on close by
    the operating system on every platform.
    """

    return tempfile.TemporaryFile(prefix="mcc-raw-export-")


class RawSpool:
    """The lines a read found, held on disk rather than in memory.

    One pass over the window fills it; the export then reads it as many times
    as its format needs (TXT sizes its columns first, XLSX checks its cells
    first) without reading the logs again, so every pass sees the same lines.
    A disk that refuses the copy fails the export before its first byte.
    """

    def __init__(self) -> None:
        try:
            self._file: IO[bytes] = _open_spool_file()
        except OSError as exc:
            raise RawScanRefused(
                "disk",
                f"The export needs a temporary file for the raw lines and the system "
                f"refused it ({exc}), so no file was written.",
                status=507,
            ) from None
        # (request_id, kind) -> flat array of (offset, nbytes, nchars, partial)
        self._index: dict[tuple[str, str], array[int]] = {}
        self.facts: dict[tuple[str, str], KindFacts] = {}
        self._end = 0

    def add(self, line: RawLine) -> None:
        if line.text is None:
            raise RawScanRefused(
                "line_too_large",
                f"A {KINDS[line.kind].label} line of this request is {line.nbytes:,} "
                f"bytes, above the {LINE_LIMIT_BYTES // (1024 * 1024)} MB one export "
                f"row can hold, so no file was written rather than one without it. "
                f"The line is in {line.file} at byte {line.offset:,}.",
                status=422,
            )
        key = (line.request_id, line.kind)
        facts = self.facts.setdefault(key, KindFacts())
        facts.add(line)
        try:
            self._file.seek(self._end)
            self._file.write(line.text)
        except OSError as exc:
            raise RawScanRefused(
                "disk",
                f"The export needs temporary disk space for the raw lines and the "
                f"system refused it ({exc}), so no file was written.",
                status=507,
            ) from None
        entries = self._index.setdefault(key, array("q"))
        entries.extend(
            (self._end, line.nbytes, len(_decode(line.text)), 1 if line.partial else 0)
        )
        self._end += line.nbytes

    def lines(self, request_id: str, kind: str) -> Iterator[tuple[int, str, int, bool]]:
        """``(n, text, nbytes, partial)`` for one request's lines of one kind."""

        entries = self._index.get((request_id, kind))
        if entries is None:
            return
        for position in range(0, len(entries), 4):
            offset, nbytes, _nchars, partial = entries[position : position + 4]
            self._file.seek(offset)
            data = self._file.read(nbytes)
            yield position // 4 + 1, _decode(data), nbytes, bool(partial)

    def max_chars(self, request_id: str, kind: str) -> int:
        entries = self._index.get((request_id, kind))
        if entries is None:
            return 0
        return max((entries[i + 2] for i in range(0, len(entries), 4)), default=0)

    def total_chars(self, request_id: str, kind: str) -> int:
        entries = self._index.get((request_id, kind))
        if entries is None:
            return 0
        return sum(entries[i + 2] for i in range(0, len(entries), 4))

    def close(self) -> None:
        self._file.close()


def fill_spool(
    sources: RawSources,
    request_ids: Collection[str],
    kinds: Collection[str],
) -> tuple[RawSpool, ScanStats]:
    """One read of the window into a fresh spool, retried while rotation moves it."""

    def read() -> tuple[RawSpool, ScanStats]:
        spool = RawSpool()
        stats = ScanStats()
        try:
            for line in read_raw_lines(
                sources,
                request_ids,
                kinds,
                stats=stats,
                deadline=time.monotonic() + SCAN_DEADLINE_SECONDS,
            ):
                spool.add(line)
        except BaseException:
            spool.close()
            raise
        return spool, stats

    return _with_retries(read)


EXPORT_COLUMNS: list[str] = [
    "request_id",
    "kind",
    "view",
    "state",
    "source",
    "n",
    "bytes",
    "text",
]
EXPORT_HEADERS: list[str] = [
    "Request",
    "Kind",
    "View",
    "State",
    "Source",
    "Line",
    "Bytes",
    "Text",
]


@dataclass(slots=True)
class RequestExport:
    """One request's export: what to stream, read once, released after."""

    request_id: str
    kinds: list[str]
    views: list[str]
    spool: RawSpool | None
    answers: dict[str, dict[str, Any]]
    normalized: dict[str, list[Block]]

    def rows(self) -> Iterator[dict[str, Any]]:
        """One row per raw line and per normalised block, kind by kind."""

        for kind in self.kinds:
            if VIEW_RAW in self.views:
                yield from self._raw_rows(kind)
            if VIEW_NORMALIZED in self.views:
                yield from self._normalized_rows(kind)

    def _row(
        self,
        kind: str,
        view: str,
        state: str,
        source: str,
        n: Any,
        size: Any,
        text: Any,
    ) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "kind": kind,
            "view": view,
            "state": state,
            "source": source,
            "n": n,
            "bytes": size,
            "text": text,
        }

    def _raw_rows(self, kind: str) -> Iterator[dict[str, Any]]:
        answer = self.answers[kind]
        source = str(answer["source"])
        if answer["capture"] != CAPTURE_PRESENT:
            yield self._row(
                kind,
                VIEW_RAW,
                str(answer["capture"]),
                source,
                None,
                None,
                f"{answer['headline']} {answer['message']}",
            )
            return
        for note in answer["notes"]:
            yield self._row(kind, VIEW_RAW, "note", source, None, None, note)
        if self.spool is None:
            return
        for n, text, nbytes, partial in self.spool.lines(self.request_id, kind):
            yield self._row(
                kind,
                VIEW_RAW,
                "partial_line" if partial else CAPTURE_PRESENT,
                source,
                n,
                nbytes,
                text,
            )

    def _normalized_rows(self, kind: str) -> Iterator[dict[str, Any]]:
        blocks = self.normalized.get(kind, [])
        if not blocks:
            yield self._row(
                kind,
                VIEW_NORMALIZED,
                "none",
                "requests",
                None,
                None,
                "MCC keeps no normalised form of CLI output; only the raw view exists "
                "for it."
                if kind == KIND_CLI
                else "The request log holds nothing of this kind for this request.",
            )
            return
        for n, block in enumerate(blocks, start=1):
            text = block.text if block.text else block.note or "nothing stored"
            yield self._row(
                kind,
                VIEW_NORMALIZED,
                block.state,
                f"{block.source} · {block.label}",
                n,
                len(block.text.encode("utf-8")) if block.text else None,
                text,
            )

    def max_cell_chars(self) -> tuple[int, str]:
        """The longest text cell, and which row it is, for XLSX's cell limit."""

        longest = 0
        where = ""
        for kind in self.kinds:
            if VIEW_RAW in self.views and self.spool is not None:
                chars = self.spool.max_chars(self.request_id, kind)
                if chars > longest:
                    longest, where = chars, f"a raw {KINDS[kind].label} line"
            if VIEW_NORMALIZED in self.views:
                for block in self.normalized.get(kind, []):
                    chars = len(block.text) if block.text else 0
                    if chars > longest:
                        longest, where = chars, f"the normalised “{block.label}”"
        return longest, where

    def close(self) -> None:
        if self.spool is not None:
            self.spool.close()


def prepare_request_export(
    sources: RawSources,
    request_id: str,
    row: Mapping[str, Any] | None,
    settings: RawSettings,
    *,
    kinds: list[str],
    views: list[str],
    now: float | None = None,
) -> RequestExport:
    """Read everything one request's export will hold, before its first byte."""

    moment = time.time() if now is None else now
    spool: RawSpool | None = None
    answers: dict[str, dict[str, Any]] = {}
    if VIEW_RAW in views:
        spool, stats = fill_spool(sources, [request_id], kinds)
        for kind in kinds:
            facts = spool.facts.get((request_id, kind), KindFacts())
            window = _window_for(sources, stats, _family_of(kind), settings, moment)
            answers[kind] = _kind_answer(
                kind,
                facts,
                settings,
                window,
                row,
                uncorrelated_cli=stats.uncorrelated_cli,
            )
    normalized = {
        kind: normalized_blocks(row, kind) for kind in kinds if VIEW_NORMALIZED in views
    }
    return RequestExport(
        request_id=request_id,
        kinds=kinds,
        views=views,
        spool=spool,
        answers=answers,
        normalized=normalized,
    )


def check_xlsx_cells(longest: int, where: str) -> None:
    if longest > XLSX_CELL_LIMIT:
        raise RawScanRefused(
            "xlsx_cell",
            f"XLSX cannot hold this export without cutting it: Excel keeps at most "
            f"{XLSX_CELL_LIMIT:,} characters in a cell, and {where} is {longest:,}. "
            "Choose JSON, CSV or TXT, which carry every line in full.",
            status=422,
        )


# --------------------------------------------------------------------------
# The Analytics export's Raw diagnostics group: one cell per kind per request.
# --------------------------------------------------------------------------


class BulkRaw:
    """Every selected request's raw lines, read once, rendered a row at a time."""

    def __init__(
        self,
        *,
        sources: RawSources,
        settings: RawSettings,
        now: float,
        columns: Mapping[str, str],
        spool: RawSpool,
        stats: ScanStats,
        prepared: Collection[str],
    ) -> None:
        self.sources = sources
        self.settings = settings
        self.now = now
        self.columns = dict(columns)
        self.spool = spool
        self.stats = stats
        self.prepared = set(prepared)
        self._late: dict[str, tuple[RawSpool, ScanStats]] = {}

    def _source_for(self, request_id: str) -> tuple[RawSpool, ScanStats]:
        if request_id in self.prepared:
            return self.spool, self.stats
        found = self._late.get(request_id)
        if found is None:
            # A request that completed after the export was prepared: read it
            # on its own rather than export its row without its lines.
            found = fill_spool(self.sources, [request_id], RAW_KINDS)
            self._late[request_id] = found
        return found

    def cells(self, row: Mapping[str, Any]) -> dict[str, str]:
        """The Raw diagnostics cells of one exported row, one per kind."""

        request_id = str(row.get("id") or "")
        spool, stats = self._source_for(request_id)
        out: dict[str, str] = {}
        for kind, column in self.columns.items():
            facts = spool.facts.get((request_id, kind), KindFacts())
            window = _window_for(
                self.sources, stats, _family_of(kind), self.settings, self.now
            )
            answer = _kind_answer(
                kind,
                facts,
                self.settings,
                window,
                row,
                uncorrelated_cli=stats.uncorrelated_cli,
            )
            out[column] = _bulk_cell(request_id, kind, answer, spool)
        return out

    def close(self) -> None:
        self.spool.close()
        for spool, _stats in self._late.values():
            spool.close()


def _bulk_cell(
    request_id: str, kind: str, answer: Mapping[str, Any], spool: RawSpool
) -> str:
    spec = KINDS[kind]
    head = (
        f"# {spec.label} · {answer['source']} · request {request_id} · "
        f"{answer['capture']} · {int(answer['lines_total']):,} lines · "
        f"{int(answer['bytes_total']):,} bytes"
    )
    parts = [head]
    if answer["capture"] != CAPTURE_PRESENT:
        parts.append(f"# {answer['headline']} {answer['message']}")
        return "\n".join(parts)
    parts.extend(f"# note: {note}" for note in answer["notes"])
    parts.extend(text for _n, text, _size, _partial in spool.lines(request_id, kind))
    return "\n".join(parts)


def prepare_bulk_raw(
    sources: RawSources,
    request_ids: Collection[str],
    settings: RawSettings,
    columns: Mapping[str, str],
    *,
    now: float | None = None,
) -> BulkRaw:
    """One read of the window for every request in the export."""

    spool, stats = fill_spool(sources, request_ids, RAW_KINDS)
    return BulkRaw(
        sources=sources,
        settings=settings,
        now=time.time() if now is None else now,
        columns=columns,
        spool=spool,
        stats=stats,
        prepared=request_ids,
    )
