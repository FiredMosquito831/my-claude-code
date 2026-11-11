"""The Google thought-signature cache, and the file it survives a restart in.

Google validates a ``thoughtSignature`` on the current turn's first tool call.
MCC captures one off the stream whenever the host sends it, keeps it in a
4096-entry in-memory map on the provider, and replays it on the next turn --
which is what makes a Gemini tool loop work at all. Until 8.19.0 that map died
with the process: a loop that spanned a restart lost every signature and fell
back to ``skip_thought_signature_validator``, the escape hatch that tells
Google not to validate. The loop kept working; the operator had no way to see
that it had stopped being validated.

**This module is a cache, not state.** Losing the file costs one turn of
continuity per in-flight tool loop and nothing else. Every failure is therefore
a decline, never an exception: a missing file, a truncated one, one written by
another product, one whose rows do not parse -- each is read as "nothing
cached yet", and the provider carries on with its in-memory behaviour.

**Scope: per provider AND per credential.** A signature is minted by one
account and validated against it; replaying one under another key is at best a
wasted byte and at worst a 400. The credential half is
:func:`~my_claude_code.providers.runtime.exit_rotation.credential_identity`'s
answer -- the same function the exit memory and the request log already use --
hashed through
:func:`~my_claude_code.core.openai_responses.carrier.connection_id`, which is
the rule the reasoning carrier states: **hash the identity, never the token.**
A Vertex access token refreshes hourly; hashing it would refuse every signature
captured before a refresh, silently, every hour.

**Bounds, all constants, none a setting.** At most :data:`MAX_ENTRIES` rows
across every provider and credential -- the same 4096 the in-memory map holds
-- and at most :data:`MAX_CREDENTIALS` credential buckets, so a rotation storm
cannot grow the file without bound. Eviction is FIFO on insertion order, which
is exactly what the in-memory map's ``pop(next(iter(...)))`` does. The store
hands the evicted key back so the caller can drop it from its own map and the
two cannot disagree.

**Never written from the request path.** A record marks the store dirty and
coalesces a write onto a short timer, the rule
:mod:`~my_claude_code.application.proxy_speed_store` states for the same
reason: a signature arrives mid-stream, and a stream must not wait on a disk.
:meth:`GoogleThoughtSignatureStore.flush` writes once more at shutdown.

**Persistence is opt-in and off until :meth:`enable_persistence` is called**,
exactly like :class:`~my_claude_code.providers.recovery.store.LearnedFactStore`.
A store nobody pointed at a file is an ordinary in-memory store, which is what
every test and every embedded use gets; only the application runtime turns
writing on, and it does so at exactly one place.
"""

import asyncio
import contextlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from loguru import logger

from my_claude_code.config.atomic_json import write_json_document_atomically_if_changed
from my_claude_code.config.paths import google_thought_signatures_path

#: The document version this build writes. A document of another version is
#: read as far as its rows parse and rewritten at this one, the rule
#: ``learned_facts.json`` keeps: a cache is upgraded by being adopted, never by
#: a separate migration pass that could half-finish.
DOCUMENT_VERSION = 1

#: How many ``(provider, credential, tool_call_id)`` rows the whole store
#: keeps. The in-memory map's own ceiling, so a restart restores exactly as
#: much as the process would have held.
MAX_ENTRIES = 4096

#: How many credential buckets may exist at once. A rotation storm across a
#: pool of keys must not grow the file without bound; the oldest bucket is
#: dropped whole, which costs that credential one turn of continuity.
MAX_CREDENTIALS = 64

#: How long a burst of newly captured signatures coalesces before the file is
#: rewritten. A signature arrives mid-stream; the stream must not wait on it.
FLUSH_DEBOUNCE_SECONDS = 5.0

_ROWS_KEY = "rows"
_VERSION_KEY = "version"


class GoogleThoughtSignatureStore:
    """Every Google tool-call signature this process knows, and how they persist."""

    def __init__(
        self, *, flush_debounce_seconds: float = FLUSH_DEBOUNCE_SECONDS
    ) -> None:
        self._path: Path | None = None
        self._debounce = max(0.0, flush_debounce_seconds)
        #: ``(provider, credential, tool_call_id)`` -> the whole
        #: ``extra_content`` object Google sent with that tool call. Insertion
        #: ordered, which is the eviction order.
        self._rows: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._dirty = False
        self._flush_task: asyncio.Task[None] | None = None

    # -- lifecycle ------------------------------------------------------

    @property
    def path(self) -> Path | None:
        """Where this store writes, or ``None`` while it is memory-only."""

        return self._path

    def enable_persistence(self, path: Path | None = None) -> None:
        """Point this store at a file and read whatever is already there."""

        self._path = path if path is not None else google_thought_signatures_path()
        self.load()

    def reset(self) -> None:
        """Forget everything, and stop writing. What a test wants between cases."""

        task = self._flush_task
        self._flush_task = None
        if task is not None and not task.done():
            task.cancel()
        self._path = None
        self._rows.clear()
        self._dirty = False

    def load(self) -> None:
        """Read the document, treating every failure as "nothing cached yet".

        Never raises. A corrupt file is a decline, not a startup failure: the
        worst honest outcome is that the first tool loop after a restart falls
        back to the sentinel, which is exactly what every release before
        8.19.0 did on every restart.
        """

        path = self._path
        if path is None:
            return
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("GOOGLE SIGNATURES: cannot read {}: {}", path, exc)
            return
        if not raw.strip():
            return
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("GOOGLE SIGNATURES: cannot parse {}: {}", path, exc)
            return
        self.load_document(document)

    def load_document(self, document: object) -> None:
        """Adopt a parsed document, dropping every row it cannot use."""

        if not isinstance(document, Mapping):
            logger.warning("GOOGLE SIGNATURES: top-level JSON value is not an object")
            return
        rows = document.get(_ROWS_KEY)
        if not isinstance(rows, list):
            logger.warning(
                "GOOGLE SIGNATURES: '{}' is not a list; ignoring it", _ROWS_KEY
            )
            return
        dropped = 0
        for row in rows:
            parsed = _row_of(row)
            if parsed is None:
                dropped += 1
                continue
            key, extra_content = parsed
            self._rows[key] = extra_content
        self._evict_over_capacity()
        if dropped:
            logger.warning("GOOGLE SIGNATURES: dropped {} unusable row(s)", dropped)
        logger.info(
            "GOOGLE SIGNATURES: loaded {} captured signature(s)", len(self._rows)
        )

    # -- reads ----------------------------------------------------------

    def rows_for(
        self, provider_id: str, credential_id: str
    ) -> dict[str, dict[str, Any]]:
        """Every signature this provider and credential captured, by tool-call id.

        A plain copy, so a provider's own map and this store cannot alias each
        other's dicts.
        """

        return {
            tool_call_id: dict(extra_content)
            for (row_provider, row_credential, tool_call_id), extra_content in (
                self._rows.items()
            )
            if row_provider == provider_id and row_credential == credential_id
        }

    def __len__(self) -> int:
        return len(self._rows)

    # -- writes ---------------------------------------------------------

    def record(
        self,
        provider_id: str,
        credential_id: str,
        tool_call_id: str,
        extra_content: Mapping[str, Any],
    ) -> tuple[str, str, str] | None:
        """Keep one signature, and say which row had to go to make room.

        The return value is the evicted ``(provider, credential, tool_call_id)``
        or ``None``. A caller that holds its own copy of the map uses it to drop
        the same entry, so the two ceilings cannot disagree about what is
        cached.
        """

        if not provider_id or not credential_id or not tool_call_id:
            return None
        key = (provider_id, credential_id, tool_call_id)
        # Re-recording an id that is already here replaces the value and keeps
        # the position, which is what a dict assignment does and what the
        # in-memory map does.
        self._rows[key] = dict(extra_content)
        evicted = self._evict_over_capacity()
        self.schedule_flush()
        return evicted

    def _evict_over_capacity(self) -> tuple[str, str, str] | None:
        """Drop the oldest rows until both ceilings hold; return the last one.

        FIFO on insertion order, which is exactly what the in-memory map's
        ``pop(next(iter(...)))`` does -- one rule, so a restart restores the
        same population the process would have held. The credential ceiling is
        enforced by dropping the oldest bucket **whole**: keeping some of a
        credential's signatures and not others would leave a tool loop half
        replayable, which is worse than not replayable at all.
        """

        evicted: tuple[str, str, str] | None = None
        while len(self._rows) > MAX_ENTRIES:
            evicted = next(iter(self._rows))
            del self._rows[evicted]
        while len({key[0:2] for key in self._rows}) > MAX_CREDENTIALS:
            bucket = next(iter(self._rows))[0:2]
            evicted = next(iter(self._rows))
            for key in [key for key in self._rows if key[0:2] == bucket]:
                del self._rows[key]
        return evicted

    def forget_provider(self, provider_id: str) -> int:
        """Forget every signature one provider captured, and write that out."""

        removed = [key for key in self._rows if key[0] == provider_id]
        for key in removed:
            del self._rows[key]
        if removed:
            self.schedule_flush()
        return len(removed)

    # -- flushing -------------------------------------------------------

    def as_document(self) -> dict[str, Any]:
        """The exact document this store would write."""

        return {
            _VERSION_KEY: DOCUMENT_VERSION,
            _ROWS_KEY: [
                {
                    "provider": provider_id,
                    "credential": credential_id,
                    "tool_call_id": tool_call_id,
                    "extra_content": extra_content,
                }
                for (provider_id, credential_id, tool_call_id), extra_content in (
                    self._rows.items()
                )
            ],
        }

    def schedule_flush(self) -> None:
        """Mark the document dirty and coalesce a write onto a short timer."""

        self._dirty = True
        if self._path is None or self._debounce <= 0:
            return
        if self._flush_task is not None and not self._flush_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop: a synchronous caller, or shutdown. ``flush`` still runs
            # from ``close``, so nothing is lost -- it is simply not debounced.
            return
        self._flush_task = loop.create_task(self._flush_after_debounce())

    async def _flush_after_debounce(self) -> None:
        while True:
            await asyncio.sleep(self._debounce)
            if not self._dirty:
                return
            self.flush()
            if not self._dirty:
                return

    def flush(self) -> bool:
        """Write the document if anything changed since the last write.

        Never raises: a disk that refuses a write must not fail a request, and
        the next capture -- or the next shutdown -- tries again. Returns
        whether the file was rewritten.
        """

        path = self._path
        if not self._dirty or path is None:
            return False
        self._dirty = False
        try:
            return write_json_document_atomically_if_changed(path, self.as_document())
        except OSError as exc:
            # Re-marked dirty so the next capture, or ``close``, retries.
            self._dirty = True
            logger.warning("GOOGLE SIGNATURES: cannot write {}: {}", path, exc)
            return False

    async def close(self) -> None:
        """Cancel any pending debounce and write once, on the way out."""

        task = self._flush_task
        self._flush_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.flush()


def _row_of(row: object) -> tuple[tuple[str, str, str], dict[str, Any]] | None:
    """One stored row as ``(key, extra_content)``, or ``None`` if unusable."""

    if not isinstance(row, Mapping):
        return None
    provider_id = row.get("provider")
    credential_id = row.get("credential")
    tool_call_id = row.get("tool_call_id")
    extra_content = row.get("extra_content")
    if not isinstance(provider_id, str) or not provider_id:
        return None
    if not isinstance(credential_id, str) or not credential_id:
        return None
    if not isinstance(tool_call_id, str) or not tool_call_id:
        return None
    if not isinstance(extra_content, Mapping) or not extra_content:
        return None
    # ``dict(Mapping)`` narrows to ``dict[Any, Any]`` only under some checkers;
    # the explicit comprehension gives the declared type and the same copy.
    return (provider_id, credential_id, tool_call_id), {
        str(key): value for key, value in extra_content.items()
    }


#: The one store the process uses. Memory-only until the application runtime
#: calls :meth:`GoogleThoughtSignatureStore.enable_persistence`.
GOOGLE_THOUGHT_SIGNATURES = GoogleThoughtSignatureStore()


__all__ = [
    "DOCUMENT_VERSION",
    "FLUSH_DEBOUNCE_SECONDS",
    "GOOGLE_THOUGHT_SIGNATURES",
    "MAX_CREDENTIALS",
    "MAX_ENTRIES",
    "GoogleThoughtSignatureStore",
]
