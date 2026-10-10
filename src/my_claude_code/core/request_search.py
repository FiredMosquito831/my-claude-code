"""The request log's search index (7.92.0): free-text search without decompressing requests.

A free-text search (``q``) matches a request when every word occurs, as part of
any word and ignoring case for plain letters, somewhere in its prompt, reply,
reasoning or the string values of its tool calls (``request_log._bodies_match``).
Until 7.92.0 the only way to answer that was to decompress and parse both
stored bodies of every row in the window: minutes over all time, because every
prompt repeats the whole conversation before it (40 GB of text in 600,000 rows).

This module keeps an index of that text in its own file beside the log
(``requests-search.db``), built once per distinct piece of text rather than per
request:

- **Units.** Each stored body blob is read once, field by field: the prompt
  (``i``), the reply (``o``), the reasoning (``t``) and the tool calls' string
  values (``c``, joined with newlines exactly as ``searchable_text`` joins
  them). A unit is one field of one blob, and a blob is known by its content
  address (sha), never by where the log keeps it: a body the log rewrites,
  moves or stores again under another rowid is still the same text.
- **Chunks.** A unit is cut into chunks right after a newline, once a chunk is
  at least 1 KB, where the line's CRC says so (content-defined, so turn N+1 of
  a conversation reuses every chunk of turn N but its last). A search word
  never contains whitespace, so no occurrence of it can straddle a cut: a word
  is in a unit exactly when it is in one of its chunks. Each distinct chunk is
  stored once, compressed (``chunks``), with the chunk -> unit postings
  (``links``) and each unit's ordered chunk list (``units``) beside it.
- **Trigrams.** The distinct chunks, folded with ``bytes.lower()`` (the log's
  own ASCII-only rule), are indexed by SQLite's FTS5 trigram tokenizer with
  ``case_sensitive 1`` (so it folds nothing itself) and ``detail=none``.

A word of three characters is answered by its trigram's postings alone. A
longer word takes the chunks holding all of its trigrams -- a superset
(``abcxbcdy`` holds both trigrams of ``abcd``) -- and tests each of those
chunks' text. A shorter word, or one holding NUL or a lone surrogate, is
tested against every chunk's text. Chunks the trigram index cannot represent
exactly (NUL, a lone surrogate, under three characters) are kept out of it and
always tested. Nothing ever decompresses a request body.

The log itself is unchanged, so any version reads it as before, and an older
version simply ignores this file. A request is answered from the index only
when both of its blobs are in it under their current content address
(``covered``); every other request -- older history before "Build search
index" was pressed, rows an older version wrote, a blob the index could not
read -- is answered by the old scan of its bodies, in the same pass. So a
search returns exactly the rows it always did, however much the index covers.
"""

import contextlib
import functools
import hashlib
import json
import sqlite3
import sys
import threading
import time
import zlib
from array import array
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from compression import zstd
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

#: The schema this code reads and writes. A file written by another version
#: of the schema is left alone and not used (the scan answers instead).
SEARCH_INDEX_SCHEMA_VERSION = 1

#: Name the index file is attached under on a reading connection of the log.
SEARCH_SCHEMA = "mcc_search"

#: The stored body fields a search reads, in the order ``searchable_text``
#: joins them; a unit's ``field`` is the position here.
SEARCH_FIELDS = ("i", "o", "t", "c")

#: Chunk cutting (measured on the synthetic log of the investigation, §5.1:
#: 1 KB chunks hold 1.62 GB of distinct text against 3.44 GB at 4 KB). A chunk
#: ends right after the first line, at least ``CHUNK_MIN_BYTES`` into it, whose
#: CRC-32 is a multiple of ``CHUNK_DIVISOR``, or after the first line that
#: takes it to ``CHUNK_MAX_BYTES``; a single line is never cut.
CHUNK_MIN_BYTES = 1024
CHUNK_DIVISOR = 16
CHUNK_MAX_BYTES = 64 * 1024

#: Chunk flag: not in the trigram index, so every search tests its text.
FLAG_TESTED = 1

# Chunks are compressed at level 3 with a dictionary trained on chunks
# (measured: 2.81x at 17 us a chunk; level 9 is 3.01x at 113 us).
_CHUNK_LEVEL = 3
_CHUNK_DICT_SIZE = 110 * 1024
# Chunks the index must hold before a dictionary is trained from them; until
# then chunks are stored with none (id 0).
_CHUNK_DICT_MIN_SAMPLES = 4_096
_DIGEST_BYTES = 16
# How much of the index file a search maps (the log's own rule: its size with
# headroom, at most 1 GB) and the page cache it keeps.
_MMAP_MAX_BYTES = 1 << 30
_CACHE_KIB = 64 * 1024
# The size the index file's WAL is cut back to after a checkpoint.
_WAL_LIMIT_BYTES = 64 * 1024 * 1024
# Rows per statement when a list of keys or digests is looked up.
_LOOKUP_CHUNK = 500
# Links a chunk has on average (measured: 16.2 M links for 1.29 M chunks), and
# how many links per unit make reading every unit cheaper than following them.
_LINKS_PER_CHUNK = 13
_FORWARD_FACTOR = 3
# Chunks tested per batch while a search reads candidate chunks.
_TEST_BATCH = 2_000

_META_BUILD = "build"
_META_FIRST_KEY = "first_key"
_META_CATCHUP = "catchup_through"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chunk_dicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    content BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    digest BLOB NOT NULL,
    flags INTEGER NOT NULL,
    dict_id INTEGER NOT NULL,
    data BLOB NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS chunks_digest ON chunks(digest);
CREATE INDEX IF NOT EXISTS chunks_tested ON chunks(id) WHERE flags != 0;
CREATE VIRTUAL TABLE IF NOT EXISTS grams USING fts5(
    text, content='', contentless_delete=1,
    tokenize='trigram case_sensitive 1', detail=none
);
CREATE VIRTUAL TABLE IF NOT EXISTS grams_vocab USING fts5vocab(grams, row);
CREATE TABLE IF NOT EXISTS blobs (
    key INTEGER PRIMARY KEY,
    sha BLOB NOT NULL,
    fields INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS blobs_sha ON blobs(sha);
CREATE TABLE IF NOT EXISTS units (
    key INTEGER NOT NULL,
    field INTEGER NOT NULL,
    manifest BLOB NOT NULL,
    PRIMARY KEY (key, field)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS links (
    chunk INTEGER NOT NULL,
    key INTEGER NOT NULL,
    field INTEGER NOT NULL,
    PRIMARY KEY (chunk, key, field)
) WITHOUT ROWID;
"""


def search_index_path(log_path: Path) -> Path:
    """The index file of the log at ``log_path``: ``requests.db`` -> ``requests-search.db``."""

    return log_path.with_name(f"{log_path.stem}-search{log_path.suffix}")


@functools.cache
def search_index_support() -> tuple[bool, str | None]:
    """Whether this Python's SQLite can keep the index, and why not if it cannot.

    The index needs FTS5 with the trigram tokenizer and ``contentless_delete``
    (SQLite 3.43), ``fts5vocab`` and ``unhex()`` (3.41). Checked once per
    process, in memory. Without them the index is off and every search reads
    the stored bodies, exactly as before 7.92.0.
    """

    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE probe USING fts5(text, content='',"
                " contentless_delete=1, tokenize='trigram case_sensitive 1',"
                " detail=none)"
            )
            conn.execute("CREATE VIRTUAL TABLE probe_vocab USING fts5vocab(probe, row)")
            conn.execute(
                "INSERT INTO probe (rowid, text) VALUES (1, 'abcd'), (2, 'ABCD')"
            )
            found = [
                int(row[0])
                for row in conn.execute(
                    "SELECT rowid FROM probe WHERE probe MATCH ?", ('"abc" AND "bcd"',)
                )
            ]
            conn.execute("DELETE FROM probe WHERE rowid = 1")
            terms = conn.execute("SELECT COUNT(*) FROM probe_vocab").fetchone()[0]
            unhexed = conn.execute("SELECT unhex('00ff')").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return False, (
            f"This Python's SQLite ({sqlite3.sqlite_version}) cannot keep the"
            f" search index ({exc}), so searches read every stored request, as before."
        )
    if found != [1] or terms != 2 or unhexed != b"\x00\xff":
        return False, (
            f"This Python's SQLite ({sqlite3.sqlite_version}) answered the search"
            " index's self-test wrongly, so searches read every stored request, as before."
        )
    return True, None


def cut_chunks(data: bytes) -> list[bytes]:
    """Cut a unit's UTF-8 bytes into chunks, each ending right after a newline.

    Deterministic from the start of the text: a text that only grows at its
    end is cut exactly as before up to its old last chunk. The last chunk ends
    where the text does, newline or not.
    """

    chunks: list[bytes] = []
    size = len(data)
    start = 0
    while start < size:
        end = _chunk_end(data, start, size)
        chunks.append(data[start:end])
        start = end
    return chunks


def _chunk_end(data: bytes, start: int, size: int) -> int:
    probe = start + CHUNK_MIN_BYTES - 1
    if probe >= size:
        return size
    newline = data.find(b"\n", probe)
    if newline < 0:
        return size
    previous = data.rfind(b"\n", start, probe)
    line_start = start if previous < 0 else previous + 1
    while True:
        end = newline + 1
        if (
            zlib.crc32(data[line_start:end]) % CHUNK_DIVISOR == 0
            or end - start >= CHUNK_MAX_BYTES
        ):
            return end
        line_start = end
        newline = data.find(b"\n", end)
        if newline < 0:
            return size


# A unit's manifest: its chunk ids in order, unsigned 32-bit little-endian,
# so a reader decodes thousands of them in one call (``array.frombytes``).
_MANIFEST_TYPE = "I"
if array(_MANIFEST_TYPE).itemsize != 4:  # every platform Python 3.14 ships on
    raise ImportError("the search index needs a 4-byte unsigned int array type")


def manifest_ids(manifest: bytes) -> array:
    """The chunk ids a unit's manifest lists, in order."""

    ids = array(_MANIFEST_TYPE)
    ids.frombytes(manifest)
    if sys.byteorder != "little":
        ids.byteswap()
    return ids


def _manifest(ids: Iterable[int]) -> bytes:
    packed = array(_MANIFEST_TYPE, ids)
    if sys.byteorder != "little":
        packed.byteswap()
    return packed.tobytes()


@dataclass(frozen=True, slots=True)
class TermPlan:
    """How one search word is answered from the index.

    ``probe`` is the word as the scan compares it (UTF-8, ``bytes.lower()``).
    ``grams`` are its distinct trigrams, empty when the word is tested against
    every chunk (under three characters, or holding NUL or a lone surrogate).
    ``exact`` says the one trigram is the whole word, so its postings decide.
    """

    probe: bytes
    grams: tuple[str, ...]
    exact: bool


def plan_terms(q: str) -> list[TermPlan]:
    """Every distinct word of ``q`` as the scan reads it (``q.split()``), planned.

    Two words that fold alike are one word: the scan requires each of them,
    and requiring the same bytes twice is requiring them once.
    """

    plans: list[TermPlan] = []
    seen: set[bytes] = set()
    for term in q.split():
        probe = term.encode("utf-8", "surrogatepass").lower()
        if probe in seen:
            continue
        seen.add(probe)
        try:
            text = probe.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is None or "\x00" in text or len(text) < 3:
            plans.append(TermPlan(probe=probe, grams=(), exact=False))
            continue
        grams = tuple(sorted({text[at : at + 3] for at in range(len(text) - 2)}))
        plans.append(TermPlan(probe=probe, grams=grams, exact=len(text) == 3))
    return plans


def _fts_query(grams: Sequence[str]) -> str:
    return " AND ".join('"' + gram.replace('"', '""') + '"' for gram in grams)


@dataclass(frozen=True, slots=True)
class BlobUnits:
    """One stored body blob as the index keeps it.

    ``sha`` is its content address (raw bytes); ``fields`` has bit ``1 << n``
    set for every field ``n`` of
    ``SEARCH_FIELDS`` the blob holds, whatever its value, because a field the
    prompt blob holds hides the reply blob's field of that name; ``units`` is
    the searchable text of each field that has any.
    """

    sha: bytes
    fields: int
    units: Mapping[int, str]


#: Turns a stored blob's packed bytes into its units' ``(fields, texts)``, or
#: None when the blob cannot be indexed exactly (it is then left to the scan).
#: The second argument asks for the strict check used on history.
UnitsReader = Callable[[bytes, bool], tuple[int, dict[int, str]] | None]
#: Decompresses a stored payload with its dictionary id; None when it cannot.
PayloadReader = Callable[[Any, Any], bytes | None]


@dataclass(slots=True)
class TermTables:
    """Temp tables of one prepared search on a reading connection.

    ``chunks_tested`` chunks had their text read and tested; ``hits`` of them
    held the word (a three-letter word's postings are taken as they are and
    counted in neither).
    """

    masks: list[str]
    chunks_tested: int
    hits: int
    seconds: float


class SearchIndex:
    """The index file beside one request log, and everything that reads or writes it.

    Writes happen on the log's writer thread only (new rows after their
    commit, the build, the sweep), through the connection that thread owns;
    ``clear`` is the one write from elsewhere and takes the file's own lock.
    Reads attach the file to a connection of the log.
    """

    def __init__(self, log_path: Path) -> None:
        self.path = search_index_path(log_path)
        self._dicts: dict[int, zstd.ZstdDict] = {}
        self._dict_lock = threading.Lock()
        # Writer thread only.
        self._compressors: dict[int, zstd.ZstdCompressor] = {}
        self._active_dict = 0
        # Bumped by ``clear``; the writer thread drops what it remembers.
        self._generation = 0
        self._gen_lock = threading.Lock()
        self.unusable: str | None = None

    @property
    def generation(self) -> int:
        with self._gen_lock:
            return self._generation

    # ------------------------------------------------------------ connections

    def _open(self, path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def open_writer(self) -> sqlite3.Connection | None:
        """The writer thread's connection, creating the file and its schema if needed.

        None, with ``unusable`` saying why, when the file belongs to another
        schema version or cannot be opened: the log is then searched as before.
        """

        try:
            conn = sqlite3.connect(self.path, timeout=10)
            try:
                empty = not conn.execute(
                    "SELECT 1 FROM sqlite_master LIMIT 1"
                ).fetchone()
                if empty:
                    # Before the first table, so freed pages can be handed back.
                    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                # A search holds its read snapshot for the whole pass, and a
                # build writes meanwhile, so the WAL can grow by what a build
                # writes in that time; once a checkpoint resets it, it goes
                # back to this size instead of keeping its largest.
                conn.execute(f"PRAGMA journal_size_limit={_WAL_LIMIT_BYTES}")
                version = self._schema_version(conn)
                if version not in (None, SEARCH_INDEX_SCHEMA_VERSION):
                    self.unusable = (
                        f"The search index file was written by another version"
                        f" (schema {version}): it is left as it is, so searches read every stored request, as before."
                    )
                    conn.close()
                    return None
                if version is None:
                    with conn:
                        conn.executescript(_SCHEMA)
                        conn.execute(
                            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema', ?)",
                            (str(SEARCH_INDEX_SCHEMA_VERSION),),
                        )
                self._load_dicts(conn)
            except BaseException:
                conn.close()
                raise
        except Exception as exc:  # an unreadable file only turns the index off
            self.unusable = f"The search index file could not be opened ({exc}), so searches read every stored request, as before."
            logger.warning("Request log search index unavailable: {}", exc)
            return None
        self.unusable = None
        return conn

    @staticmethod
    def _schema_version(conn: sqlite3.Connection) -> int | None:
        has_meta = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
        ).fetchone()
        if not has_meta:
            return None
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
        if row is None:
            return None
        try:
            return int(row[0])
        except ValueError:
            return -1

    def attach(self, conn: sqlite3.Connection) -> bool:
        """Attach the index file to a reading connection of the log as ``SEARCH_SCHEMA``.

        False when there is no usable file; the caller then searches without it.
        Never creates the file: a missing file is checked first, and a file
        that has none of the index's tables is detached again.
        """

        if self.unusable is not None or not self.path.exists():
            return False
        try:
            conn.execute(f"ATTACH DATABASE ? AS {SEARCH_SCHEMA}", (str(self.path),))
        except sqlite3.Error:
            return False
        try:
            version = conn.execute(
                f"SELECT value FROM {SEARCH_SCHEMA}.meta WHERE key = 'schema'"
            ).fetchone()
        except sqlite3.Error:
            version = None
        if version is None or str(version[0]) != str(SEARCH_INDEX_SCHEMA_VERSION):
            with contextlib.suppress(sqlite3.Error):
                conn.execute(f"DETACH DATABASE {SEARCH_SCHEMA}")
            return False
        # A search reads the index's b-trees all over: map the file, as the
        # log's own connections map theirs, rather than read it a page at a
        # time through a 2 MB cache. Literals: PRAGMA takes no parameters.
        with contextlib.suppress(OSError, sqlite3.Error):
            size = min(int(self.path.stat().st_size * 1.25), _MMAP_MAX_BYTES)
            conn.execute(f"PRAGMA {SEARCH_SCHEMA}.mmap_size={int(size)}")
            conn.execute(f"PRAGMA {SEARCH_SCHEMA}.cache_size={-_CACHE_KIB}")
        return True

    # ----------------------------------------------------------- dictionaries

    def _load_dicts(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT id, content FROM chunk_dicts ORDER BY id"
        ).fetchall()
        with self._dict_lock:
            for dict_id, content in rows:
                self._dicts.setdefault(int(dict_id), zstd.ZstdDict(bytes(content)))
        self._active_dict = int(rows[-1][0]) if rows else 0

    def _dictionary(
        self, conn: sqlite3.Connection, dict_id: int
    ) -> zstd.ZstdDict | None:
        if not dict_id:
            return None
        with self._dict_lock:
            found = self._dicts.get(dict_id)
        if found is not None:
            return found
        row = conn.execute(
            f"SELECT content FROM {self._schema_of(conn)}chunk_dicts WHERE id = ?",
            (dict_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"search index dictionary {dict_id} is missing")
        loaded = zstd.ZstdDict(bytes(row[0]))
        with self._dict_lock:
            self._dicts.setdefault(dict_id, loaded)
        return loaded

    @staticmethod
    def _schema_of(conn: sqlite3.Connection) -> str:
        """``mcc_search.`` on a log connection the index is attached to, else nothing."""

        for row in conn.execute("PRAGMA database_list"):
            if str(row[1]) == SEARCH_SCHEMA:
                return f"{SEARCH_SCHEMA}."
        return ""

    def chunk_text(self, conn: sqlite3.Connection, dict_id: int, data: bytes) -> bytes:
        """A stored chunk's original bytes."""

        zdict = self._dictionary(conn, dict_id)
        if zdict is None:
            return zstd.decompress(data)
        return zstd.decompress(data, zstd_dict=zdict.as_digested_dict)

    def _compress(self, data: bytes) -> tuple[int, bytes]:
        dict_id = self._active_dict
        compressor = self._compressors.get(dict_id)
        if compressor is None:
            zdict = self._dicts.get(dict_id) if dict_id else None
            compressor = zstd.ZstdCompressor(
                level=_CHUNK_LEVEL,
                zstd_dict=zdict.as_digested_dict if zdict is not None else None,
            )
            self._compressors[dict_id] = compressor
        return dict_id, compressor.compress(data, mode=zstd.ZstdCompressor.FLUSH_FRAME)

    def train_dictionary_if_due(self, conn: sqlite3.Connection) -> bool:
        """Train the chunk dictionary once enough chunks exist. Writer thread, idle only.

        One dictionary for the life of the file: chunks stored before it keep
        id 0, and every dictionary ever used stays in ``chunk_dicts``.
        """

        if self._active_dict:
            return False
        count = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
        if count < _CHUNK_DICT_MIN_SAMPLES:
            return False
        step = max(1, count // _CHUNK_DICT_MIN_SAMPLES)
        samples = [
            self.chunk_text(conn, int(dict_id), bytes(data))
            for dict_id, data in conn.execute(
                "SELECT dict_id, data FROM chunks WHERE id % ? = 0 LIMIT ?",
                (step, _CHUNK_DICT_MIN_SAMPLES * 2),
            )
        ]
        try:
            trained = zstd.train_dict(samples, _CHUNK_DICT_SIZE)
        except zstd.ZstdError as exc:
            logger.debug("Search index dictionary not trained: {}", exc)
            return False
        with conn:
            cursor = conn.execute(
                "INSERT INTO chunk_dicts (created_at, content) VALUES (?, ?)",
                (time.time(), trained.dict_content),
            )
        dict_id = int(cursor.lastrowid or 0)
        with self._dict_lock:
            self._dicts[dict_id] = zstd.ZstdDict(trained.dict_content)
        self._active_dict = dict_id
        return True

    # ----------------------------------------------------------------- writes

    def known(self, conn: sqlite3.Connection, shas: Iterable[bytes]) -> set[bytes]:
        """The content addresses of ``shas`` the index already holds."""

        found: set[bytes] = set()
        wanted = sorted(set(shas))
        for start in range(0, len(wanted), _LOOKUP_CHUNK):
            part = wanted[start : start + _LOOKUP_CHUNK]
            found.update(
                bytes(row[0])
                for row in conn.execute(
                    f"SELECT sha FROM blobs WHERE sha IN ({', '.join('?' * len(part))})",
                    part,
                )
            )
        return found

    def add(
        self, conn: sqlite3.Connection, blobs: Sequence[BlobUnits]
    ) -> tuple[int, int]:
        """Index ``blobs`` the index does not hold yet; ``(blobs, new chunks)``.

        Runs inside the caller's transaction on the writer's index connection.
        A blob already held (by its address) is skipped.
        """

        held = self.known(conn, [blob.sha for blob in blobs])
        fresh: dict[bytes, BlobUnits] = {}
        for blob in blobs:
            if blob.sha not in held:
                fresh.setdefault(blob.sha, blob)
        blobs = list(fresh.values())
        if not blobs:
            return 0, 0
        keys: dict[bytes, int] = {}
        for blob in blobs:
            cursor = conn.execute(
                "INSERT INTO blobs (sha, fields) VALUES (?, ?)", (blob.sha, blob.fields)
            )
            keys[blob.sha] = int(cursor.lastrowid or 0)
        cut: list[tuple[BlobUnits, int, list[bytes]]] = []
        texts: dict[bytes, bytes] = {}
        for blob in blobs:
            for field, text in blob.units.items():
                digests: list[bytes] = []
                for chunk in cut_chunks(text.encode("utf-8", "surrogatepass")):
                    digest = hashlib.blake2b(chunk, digest_size=_DIGEST_BYTES).digest()
                    texts.setdefault(digest, chunk)
                    digests.append(digest)
                cut.append((blob, field, digests))
        ids = self._chunk_ids(conn, list(texts))
        added = 0
        grams: list[tuple[int, str]] = []
        for digest, chunk in texts.items():
            if digest in ids:
                continue
            flags, folded = _chunk_flags(chunk)
            dict_id, data = self._compress(chunk)
            cursor = conn.execute(
                "INSERT INTO chunks (digest, flags, dict_id, data) VALUES (?, ?, ?, ?)",
                (digest, flags, dict_id, data),
            )
            chunk_id = int(cursor.lastrowid or 0)
            ids[digest] = chunk_id
            if folded is not None:
                grams.append((chunk_id, folded))
            added += 1
        if grams:
            conn.executemany("INSERT INTO grams (rowid, text) VALUES (?, ?)", grams)
        links: set[tuple[int, int, int]] = set()
        units: list[tuple[int, int, bytes]] = []
        for blob, field, digests in cut:
            key = keys[blob.sha]
            chunk_ids = [ids[digest] for digest in digests]
            units.append((key, field, _manifest(chunk_ids)))
            links.update((chunk_id, key, field) for chunk_id in chunk_ids)
        conn.executemany(
            "INSERT OR REPLACE INTO units (key, field, manifest) VALUES (?, ?, ?)",
            units,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO links (chunk, key, field) VALUES (?, ?, ?)",
            sorted(links),
        )
        return len(blobs), added

    def _chunk_ids(
        self, conn: sqlite3.Connection, digests: list[bytes]
    ) -> dict[bytes, int]:
        found: dict[bytes, int] = {}
        for start in range(0, len(digests), _LOOKUP_CHUNK):
            part = digests[start : start + _LOOKUP_CHUNK]
            for chunk_id, digest in conn.execute(
                f"SELECT id, digest FROM chunks WHERE digest IN ({', '.join('?' * len(part))})",
                part,
            ):
                found[bytes(digest)] = int(chunk_id)
        return found

    def remove(self, conn: sqlite3.Connection, keys: Iterable[int]) -> int:
        """Drop the given blob keys, then every chunk no unit names any more."""

        orphans: set[int] = set()
        removed = 0
        for key in keys:
            unit_rows = conn.execute(
                "SELECT field, manifest FROM units WHERE key = ?", (key,)
            ).fetchall()
            for field, manifest in unit_rows:
                chunk_ids = set(manifest_ids(bytes(manifest)))
                conn.executemany(
                    "DELETE FROM links WHERE chunk = ? AND key = ? AND field = ?",
                    [(chunk_id, key, int(field)) for chunk_id in chunk_ids],
                )
                orphans.update(chunk_ids)
            conn.execute("DELETE FROM units WHERE key = ?", (key,))
            removed += conn.execute("DELETE FROM blobs WHERE key = ?", (key,)).rowcount
        for chunk_id in sorted(orphans):
            if conn.execute(
                "SELECT 1 FROM links WHERE chunk = ? LIMIT 1", (chunk_id,)
            ).fetchone():
                continue
            row = conn.execute(
                "SELECT flags FROM chunks WHERE id = ?", (chunk_id,)
            ).fetchone()
            if row is None:
                continue
            if not int(row[0]) & FLAG_TESTED:
                conn.execute("DELETE FROM grams WHERE rowid = ?", (chunk_id,))
            conn.execute("DELETE FROM chunks WHERE id = ?", (chunk_id,))
        return removed

    def clear(self) -> None:
        """Erase every indexed text (the log was cleared). Any thread.

        Dictionaries stay, as the log's own do: a chunk compressed with one is
        unreadable without it, and the writer may hold its id.
        """

        if not self.path.exists():
            return
        conn = self._open(self.path)
        try:
            if self._schema_version(conn) != SEARCH_INDEX_SCHEMA_VERSION:
                return
            with conn:
                conn.execute("INSERT INTO grams (grams) VALUES ('delete-all')")
                for table in ("chunks", "blobs", "units", "links"):
                    conn.execute(f"DELETE FROM {table}")
                conn.execute(
                    "DELETE FROM meta WHERE key IN (?, ?, ?)",
                    (_META_BUILD, _META_FIRST_KEY, _META_CATCHUP),
                )
            with contextlib.suppress(sqlite3.Error):
                conn.execute("PRAGMA incremental_vacuum")
            with contextlib.suppress(sqlite3.Error):
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        with self._gen_lock:
            self._generation += 1

    # ------------------------------------------------------------------- meta

    @staticmethod
    def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    @staticmethod
    def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
        )

    def saved_build_state(self) -> dict[str, Any]:
        """The build state the file holds, read on a short connection; {} if none.

        Read when the log is opened, so a build a restart interrupted is known
        (and continues) before the writer thread has opened the file.
        """

        if not self.path.exists():
            return {}
        try:
            conn = sqlite3.connect(self.path, timeout=10)
            try:
                if self._schema_version(conn) != SEARCH_INDEX_SCHEMA_VERSION:
                    return {}
                return self.build_state(conn)
            finally:
                conn.close()
        except sqlite3.Error:
            return {}

    def build_state(self, conn: sqlite3.Connection) -> dict[str, Any]:
        raw = self.meta_get(conn, _META_BUILD)
        if raw:
            with contextlib.suppress(ValueError, TypeError):
                state = json.loads(raw)
                if isinstance(state, dict):
                    return state
        return {}

    def set_build_state(
        self, conn: sqlite3.Connection, state: Mapping[str, Any]
    ) -> None:
        self.meta_set(conn, _META_BUILD, json.dumps(dict(state), sort_keys=True))

    def first_key(self, conn: sqlite3.Connection) -> int | None:
        raw = self.meta_get(conn, _META_FIRST_KEY)
        return int(raw) if raw and raw.isdigit() else None

    def set_first_key(self, conn: sqlite3.Connection, key: int) -> None:
        self.meta_set(conn, _META_FIRST_KEY, str(key))

    def catchup_through(self, conn: sqlite3.Connection) -> int | None:
        raw = self.meta_get(conn, _META_CATCHUP)
        return int(raw) if raw and raw.isdigit() else None

    def set_catchup_through(self, conn: sqlite3.Connection, key: int) -> None:
        self.meta_set(conn, _META_CATCHUP, str(key))

    # ------------------------------------------------------------------ reads

    def prepare(
        self,
        conn: sqlite3.Connection,
        plans: Sequence[TermPlan],
        *,
        prefix: str = "mcc_search",
        scope: str | None = None,
    ) -> TermTables:
        """Fill one temp table per word: ``key -> mask`` of the units holding it.

        ``conn`` is a log connection with the index attached. Bit ``1 << n`` of
        a key's mask says field ``n`` of that blob holds the word. ``scope``,
        when given, names a temp table (``key INTEGER PRIMARY KEY``) holding
        every blob key the pass will read: only those blobs' chunks are tested
        or looked up, and only their masks are kept. Without it every unit
        the index holds is considered.
        """

        started = time.perf_counter()
        schema = SEARCH_SCHEMA
        scope_chunks = ""
        # The scope's units with their chunks, read once: a scoped mask is
        # found forward (does this unit hold a matched chunk?), never through
        # the links of a chunk that half the log shares.
        scope_units: list[tuple[int, int, array]] = []
        if scope is not None:
            scope_chunks = f"{prefix}_scope"
            conn.execute(f"DROP TABLE IF EXISTS temp.{scope_chunks}")
            conn.execute(
                f"CREATE TEMP TABLE {scope_chunks} (chunk INTEGER PRIMARY KEY)"
            )
            wanted: set[int] = set()
            for key, field, manifest in conn.execute(
                f"SELECT u.key, u.field, u.manifest FROM temp.{scope} AS s"
                f" CROSS JOIN {schema}.units AS u ON u.key = s.key"
            ):
                chunk_ids = manifest_ids(bytes(manifest))
                scope_units.append((int(key), int(field), chunk_ids))
                wanted.update(chunk_ids)
            conn.executemany(
                f"INSERT INTO temp.{scope_chunks} (chunk) VALUES (?)",
                ((chunk_id,) for chunk_id in sorted(wanted)),
            )
        in_scope = (
            f" AND c.id IN (SELECT chunk FROM temp.{scope_chunks})"
            if scope_chunks
            else ""
        )
        # Every join below is a CROSS JOIN: SQLite keeps its left-to-right order,
        # so the few rows on the left drive (it has no statistics for temp
        # tables, and once walked all 16.9 M links for an empty chunk list).
        # With a scope, the window's own chunks drive and the word's postings
        # are one lookup set (never ``rowid IN (...)`` inside the trigram
        # query, which SQLite would answer one rowid at a time).
        matching = f"SELECT rowid FROM {schema}.grams WHERE grams MATCH ?"
        masks: list[str] = []
        tested = 0
        hits = 0
        for number, plan in enumerate(plans):
            chunk_table = f"{prefix}_c{number}"
            mask_table = f"{prefix}_m{number}"
            conn.execute(f"DROP TABLE IF EXISTS temp.{chunk_table}")
            conn.execute(f"DROP TABLE IF EXISTS temp.{mask_table}")
            conn.execute(f"CREATE TEMP TABLE {chunk_table} (chunk INTEGER PRIMARY KEY)")
            conn.execute(
                f"CREATE TEMP TABLE {mask_table} (key INTEGER PRIMARY KEY, mask INTEGER NOT NULL)"
            )
            # Chunks kept out of the trigram index are always tested.
            flagged = (
                f"SELECT c.id, c.dict_id, c.data FROM {schema}.chunks AS c"
                f" INDEXED BY chunks_tested WHERE c.flags != 0{in_scope}",
                (),
            )
            sources: list[tuple[str, tuple[Any, ...]]]
            if plan.grams and plan.exact:
                if scope_chunks:
                    conn.execute(
                        f"INSERT INTO temp.{chunk_table} (chunk)"
                        f" SELECT s.chunk FROM temp.{scope_chunks} AS s"
                        f" WHERE s.chunk IN ({matching})",
                        (_fts_query(plan.grams),),
                    )
                else:
                    conn.execute(
                        f"INSERT INTO temp.{chunk_table} (chunk) {matching}",
                        (_fts_query(plan.grams),),
                    )
                sources = [flagged]
            elif plan.grams and scope_chunks:
                sources = [
                    (
                        f"SELECT c.id, c.dict_id, c.data FROM temp.{scope_chunks} AS s"
                        f" CROSS JOIN {schema}.chunks AS c ON c.id = s.chunk"
                        f" WHERE s.chunk IN ({matching})",
                        (_fts_query(plan.grams),),
                    ),
                    flagged,
                ]
            elif plan.grams:
                sources = [
                    (
                        f"SELECT c.id, c.dict_id, c.data FROM {schema}.grams AS g"
                        f" CROSS JOIN {schema}.chunks AS c ON c.id = g.rowid"
                        " WHERE g.grams MATCH ?",
                        (_fts_query(plan.grams),),
                    ),
                    flagged,
                ]
            elif scope_chunks:
                sources = [
                    (
                        f"SELECT c.id, c.dict_id, c.data FROM temp.{scope_chunks} AS s"
                        f" CROSS JOIN {schema}.chunks AS c ON c.id = s.chunk",
                        (),
                    )
                ]
            else:
                sources = [(f"SELECT id, dict_id, data FROM {schema}.chunks", ())]
            seen, held = self._test_chunks(conn, sources, plan.probe, chunk_table)
            tested += seen
            hits += held
            masks.append(mask_table)
        # Chunks -> the blobs holding them. Through the links when the matched
        # chunks are few; forward -- does a unit hold one of them? -- over the
        # scope's units, or over every unit once the links to follow would
        # outnumber them (a common word: half the log shares its chunks).
        forward: list[int] = []
        if scope is not None:
            forward = list(range(len(plans)))
        else:
            units_estimate = 2 * int(
                conn.execute(f"SELECT COUNT(*) FROM {schema}.blobs").fetchone()[0]
            )
            for number in range(len(plans)):
                matched_count = int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM temp.{prefix}_c{number}"
                    ).fetchone()[0]
                )
                if matched_count * _LINKS_PER_CHUNK > _FORWARD_FACTOR * units_estimate:
                    forward.append(number)
                else:
                    # An upsert per link rather than a GROUP BY: measured 11 s
                    # against 15-19 s for 5.9 M links; memory is the keys.
                    conn.execute(
                        f"INSERT INTO temp.{prefix}_m{number} (key, mask)"
                        f" SELECT l.key, 1 << l.field"
                        f" FROM temp.{prefix}_c{number} AS t CROSS JOIN {schema}.links AS l"
                        " ON l.chunk = t.chunk WHERE 1"
                        " ON CONFLICT(key) DO UPDATE SET mask = mask | excluded.mask"
                    )
        if forward:
            matched_sets = {
                number: {
                    int(row[0])
                    for row in conn.execute(
                        f"SELECT chunk FROM temp.{prefix}_c{number}"
                    )
                }
                for number in forward
            }
            found: dict[int, dict[int, int]] = {number: {} for number in forward}
            for key, field, chunk_ids in (
                scope_units if scope is not None else self._every_unit(conn)
            ):
                for number in forward:
                    if not matched_sets[number].isdisjoint(chunk_ids):
                        masks_of = found[number]
                        masks_of[key] = masks_of.get(key, 0) | (1 << field)
            for number in forward:
                conn.executemany(
                    f"INSERT INTO temp.{prefix}_m{number} (key, mask) VALUES (?, ?)",
                    sorted(found[number].items()),
                )
        return TermTables(
            masks=masks,
            chunks_tested=tested,
            hits=hits,
            seconds=time.perf_counter() - started,
        )

    @staticmethod
    def _every_unit(conn: sqlite3.Connection) -> Iterator[tuple[int, int, array]]:
        """Every unit the index holds, with its chunk ids, read a batch at a time."""

        cursor = conn.execute(f"SELECT key, field, manifest FROM {SEARCH_SCHEMA}.units")
        try:
            while True:
                rows = cursor.fetchmany(_TEST_BATCH)
                if not rows:
                    return
                for key, field, manifest in rows:
                    yield int(key), int(field), manifest_ids(bytes(manifest))
        finally:
            cursor.close()

    def _test_chunks(
        self,
        conn: sqlite3.Connection,
        sources: Sequence[tuple[str, tuple[Any, ...]]],
        probe: bytes,
        chunk_table: str,
    ) -> tuple[int, int]:
        """Test each chunk the ``sources`` yield for ``probe``; keep the ones holding it."""

        seen = 0
        hits = 0
        digested: dict[int, Any] = {}
        for source, args in sources:
            cursor = conn.execute(source, args)
            try:
                while True:
                    rows = cursor.fetchmany(_TEST_BATCH)
                    if not rows:
                        break
                    found: list[tuple[int]] = []
                    for chunk_id, dict_id, data in rows:
                        seen += 1
                        dict_key = int(dict_id)
                        if dict_key not in digested:
                            zdict = self._dictionary(conn, dict_key)
                            digested[dict_key] = (
                                zdict.as_digested_dict if zdict is not None else None
                            )
                        text = zstd.decompress(
                            bytes(data), zstd_dict=digested[dict_key]
                        )
                        if probe in text.lower():
                            found.append((int(chunk_id),))
                    if found:
                        conn.executemany(
                            f"INSERT OR IGNORE INTO temp.{chunk_table} (chunk) VALUES (?)",
                            found,
                        )
                        hits += len(found)
            finally:
                cursor.close()
        return seen, hits

    def held_share(self, conn: sqlite3.Connection) -> float:
        """The share of the log's stored bodies the index holds (0 to 1)."""

        held = int(
            conn.execute(f"SELECT COUNT(*) FROM {SEARCH_SCHEMA}.blobs").fetchone()[0]
        )
        stored = int(conn.execute("SELECT COUNT(*) FROM main.body_blobs").fetchone()[0])
        return min(1.0, held / stored) if stored else 1.0

    def estimate(
        self, conn: sqlite3.Connection, plans: Sequence[TermPlan]
    ) -> dict[str, int]:
        """What ``prepare`` would read for ``plans``, from the index's own counts.

        ``chunks`` is every chunk a word must have tested or looked up (an
        upper bound: the rarest trigram's document count for a word the index
        narrows, every chunk for a word it cannot).
        """

        schema = SEARCH_SCHEMA
        total = int(conn.execute(f"SELECT COUNT(*) FROM {schema}.chunks").fetchone()[0])
        tested = 0
        looked_up = 0
        for plan in plans:
            if not plan.grams:
                tested += total
                continue
            smallest = total
            for gram in plan.grams:
                row = conn.execute(
                    f"SELECT doc FROM {schema}.grams_vocab WHERE term = ?", (gram,)
                ).fetchone()
                smallest = min(smallest, int(row[0]) if row is not None else 0)
                if smallest == 0:
                    break
            if plan.exact:
                looked_up += smallest
            else:
                tested += smallest
        return {"chunks": total, "tested": tested, "looked_up": looked_up}


def _chunk_flags(chunk: bytes) -> tuple[int, str | None]:
    """A chunk's flags and the folded text the trigram index gets, if any."""

    if b"\x00" in chunk:
        return FLAG_TESTED, None
    try:
        folded = chunk.lower().decode("utf-8")
    except UnicodeDecodeError:
        return FLAG_TESTED, None
    if len(folded) < 3:
        return FLAG_TESTED, None
    return 0, folded
