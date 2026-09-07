"""The one durable home for what MCC has learned about a host.

``~/.mcc/learned_facts.json``, one document, written atomically, loaded once at
startup into the in-memory shapes that already exist. This is an adapter, not a
rewrite: :class:`~my_claude_code.providers.recovery.memory.RecoveryMemory` keeps
its exact public API and every consumer of it is untouched -- a provider asks
the store for a memory instead of constructing an empty one, and the memory
calls back here when it learns something.

Why not the request-log database, which is already durable:

1. ``RequestLog.clear()`` deletes the image-blob rows, and the dashboard has a
   button for it. A learned fact must not be collateral damage of "Clear log".
2. Retention prunes orphaned blobs, so time destroys it too.
3. ``core`` may not import ``config``, and the store keys on provider ids that
   only ``config.provider_registry`` knows.
4. The log has a background writer with a bounded queue; a fact learned during
   shutdown could be dropped at the queue limit. An ``os.replace`` cannot be
   half-written.

**Persistence is opt-in and off until :meth:`LearnedFactStore.enable_persistence`
is called.** A store nobody pointed at a file is an ordinary in-memory store,
which is what every test and every embedded use gets; only the application
runtime turns writing on, and it does so at exactly one place.
"""

import asyncio
import contextlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from my_claude_code.config.atomic_json import write_json_document_atomically_if_changed
from my_claude_code.config.paths import learned_facts_path

from .facts import (
    ALLOWED_FACT_KINDS,
    DOCUMENT_VERSION,
    FACT_MODEL_WITHHELD,
    FACT_MODELS_ETAG,
    FACT_OUTPUT_CAP,
    FACT_REASONING_FIELD_REJECTED,
    FACT_STREAM_USAGE_UNSUPPORTED,
    FACTS_KEY,
    MAX_FACT_ROWS,
    PROVIDER_WIDE_MODEL_ID,
    SOURCE_REJECTION,
    VERSION_KEY,
    FactSink,
    LearnedFact,
    bounded_evidence,
    fact_from_row,
    parse_timestamp,
    utc_now_iso,
)
from .memory import RecoveryMemory

#: How long a burst of newly learned facts is allowed to coalesce before the
#: document is rewritten. A learned fact must never make a request wait on a
#: disk write, and a chain of 400s across a fallback ladder can produce several
#: within the same second.
FLUSH_DEBOUNCE_SECONDS = 5.0


class LearnedFactStore:
    """Every learned fact this process holds, and how they reach the disk."""

    def __init__(
        self,
        *,
        path: Path | None = None,
        flush_debounce_seconds: float = FLUSH_DEBOUNCE_SECONDS,
    ) -> None:
        self._path = path
        self._debounce = max(0.0, flush_debounce_seconds)
        self._facts: dict[tuple[str, str, str, str], LearnedFact] = {}
        self._dirty = False
        self._flush_task: asyncio.Task[None] | None = None
        self._memories: dict[str, RecoveryMemory] = {}

    # -- lifecycle ------------------------------------------------------

    @property
    def path(self) -> Path | None:
        """Where this store writes, or ``None`` while it is memory-only."""

        return self._path

    def enable_persistence(self, path: Path | None = None) -> None:
        """Point this store at a file and read whatever is already there."""

        self._path = path if path is not None else learned_facts_path()
        self.load()

    def load(self) -> None:
        """Read the document, treating every failure as "nothing learned yet".

        A corrupt or unreadable file is a decline, never a startup failure: the
        worst honest outcome is that MCC re-pays one 400 per model, which is
        exactly what 6.51.0 did on every restart.
        """

        path = self._path
        if path is None:
            return
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("LEARNED FACTS: cannot read {}: {}", path, exc)
            return
        if not raw.strip():
            return
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("LEARNED FACTS: cannot parse {}: {}", path, exc)
            return
        self.load_document(document)

    def load_document(self, document: object) -> None:
        """Adopt a parsed document, dropping every row it cannot use."""

        if not isinstance(document, Mapping):
            logger.warning("LEARNED FACTS: top-level JSON value is not an object")
            return
        rows = document.get(FACTS_KEY)
        if not isinstance(rows, list):
            logger.warning("LEARNED FACTS: '{}' is not a list; ignoring it", FACTS_KEY)
            return
        dropped = 0
        unknown_kinds: set[str] = set()
        for row in rows:
            fact = fact_from_row(row)
            if fact is None:
                dropped += 1
                if isinstance(row, Mapping):
                    kind = str(row.get("fact_kind") or "")
                    if kind and kind not in ALLOWED_FACT_KINDS:
                        unknown_kinds.add(kind)
                continue
            self._facts[fact.key] = fact
        if dropped:
            logger.warning(
                "LEARNED FACTS: dropped {} unusable row(s){}",
                dropped,
                (
                    f"; unknown fact kinds: {', '.join(sorted(unknown_kinds))}"
                    if unknown_kinds
                    else ""
                ),
            )
        logger.info("LEARNED FACTS: loaded {} fact(s)", len(self._facts))

    async def close(self) -> None:
        """Cancel any pending debounce and write once, on the way out."""

        task = self._flush_task
        self._flush_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.flush()

    # -- reads ----------------------------------------------------------

    def all_facts(self) -> tuple[LearnedFact, ...]:
        """Every stored fact, stale ones included."""

        return tuple(self._facts.values())

    def facts_for_provider(self, provider_id: str) -> tuple[LearnedFact, ...]:
        """Every fact about one provider, stale ones included."""

        return tuple(
            fact for fact in self._facts.values() if fact.provider_id == provider_id
        )

    def facts_for_model(
        self, provider_id: str, model_id: str
    ) -> tuple[LearnedFact, ...]:
        """Every fact about one model, stale ones included."""

        return tuple(
            fact
            for fact in self._facts.values()
            if fact.provider_id == provider_id and fact.model_id == model_id
        )

    def fresh_facts_for_provider(
        self, provider_id: str, *, now: datetime | None = None
    ) -> tuple[LearnedFact, ...]:
        """Only the facts that are still applied."""

        moment = now or datetime.now(UTC)
        return tuple(
            fact
            for fact in self.facts_for_provider(provider_id)
            if not fact.is_stale(moment)
        )

    # -- writes ---------------------------------------------------------

    def record(
        self,
        provider_id: str,
        model_id: str,
        fact_kind: str,
        value: Any,
        *,
        source: str = SOURCE_REJECTION,
        detail: str = "",
        evidence: str = "",
    ) -> LearnedFact | None:
        """Establish or re-confirm one fact, and schedule a write.

        ``learned_at`` is never touched again after the first time: the two
        timestamps answer different questions, and a re-confirmation that
        overwrote the first one would make a fact MCC has believed for a month
        look like it was discovered a second ago.
        """

        provider_id = provider_id.strip()
        model_id = model_id.strip()
        if not provider_id or not model_id or fact_kind not in ALLOWED_FACT_KINDS:
            logger.debug(
                "LEARNED FACTS: refusing an unusable fact provider={} model={} kind={}",
                provider_id,
                model_id,
                fact_kind,
            )
            return None
        now_iso = utc_now_iso()
        excerpt = bounded_evidence(evidence)
        key = (provider_id, model_id, fact_kind, detail)
        existing = self._facts.get(key)
        if existing is not None:
            fact = existing.confirmed(value=value, evidence=excerpt, now_iso=now_iso)
        else:
            fact = LearnedFact(
                provider_id=provider_id,
                model_id=model_id,
                fact_kind=fact_kind,
                value=value,
                learned_at=now_iso,
                last_confirmed_at=now_iso,
                source=source,
                detail=detail,
                evidence=excerpt,
            )
        self._facts[key] = fact
        self._evict_if_over_capacity()
        self.schedule_flush()
        return fact

    def retire_model(self, provider_id: str, model_id: str) -> int:
        """Stop applying a vanished model's facts, without deleting them.

        A model that disappeared from a provider's catalogue took its
        deployment with it, and a cap measured against a deployment that is
        gone is not evidence about whatever replaces it. The rows stay so the
        page can still say what MCC used to believe.
        """

        retired = 0
        for key, fact in list(self._facts.items()):
            if fact.provider_id != provider_id or fact.model_id != model_id:
                continue
            if fact.retired:
                continue
            self._facts[key] = replace(fact, retired=True)
            retired += 1
        if retired:
            self.schedule_flush()
        return retired

    def forget(self, provider_id: str, model_id: str, fact_kind: str = "") -> int:
        """Forget one row, or every row about one model."""

        removed = [
            key
            for key, fact in self._facts.items()
            if fact.provider_id == provider_id
            and fact.model_id == model_id
            and (not fact_kind or fact.fact_kind == fact_kind)
        ]
        for key in removed:
            del self._facts[key]
        if removed:
            logger.info(
                "LEARNED FACTS: forgot {} fact(s) for {}/{}{}",
                len(removed),
                provider_id,
                model_id,
                f" ({fact_kind})" if fact_kind else "",
            )
            self._resync_memories()
            self.schedule_flush()
        return len(removed)

    def forget_provider(self, provider_id: str) -> int:
        """Forget everything learned about one provider."""

        removed = [
            key for key, fact in self._facts.items() if fact.provider_id == provider_id
        ]
        for key in removed:
            del self._facts[key]
        if removed:
            logger.info(
                "LEARNED FACTS: forgot {} fact(s) for provider {}",
                len(removed),
                provider_id,
            )
            self._resync_memories()
            self.schedule_flush()
        return len(removed)

    def forget_all(self) -> int:
        """Forget every learned fact this process holds."""

        count = len(self._facts)
        self._facts.clear()
        if count:
            logger.info("LEARNED FACTS: forgot all {} fact(s)", count)
            self._resync_memories()
            self.schedule_flush()
        return count

    # -- flushing -------------------------------------------------------

    def as_document(self) -> dict[str, Any]:
        """The exact document this store would write."""

        rows = sorted(
            (fact.as_row() for fact in self._facts.values()),
            key=lambda row: (
                str(row["provider_id"]),
                str(row["model_id"]),
                str(row["fact_kind"]),
                str(row.get("detail", "")),
            ),
        )
        return {VERSION_KEY: DOCUMENT_VERSION, FACTS_KEY: rows}

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
        try:
            await asyncio.sleep(self._debounce)
        except asyncio.CancelledError:
            raise
        await asyncio.to_thread(self.flush)

    def flush(self) -> bool:
        """Write the document now, if anything changed. Never raises."""

        if not self._dirty or self._path is None:
            return False
        self._dirty = False
        try:
            return write_json_document_atomically_if_changed(
                self._path, self.as_document()
            )
        except OSError as exc:
            logger.warning("LEARNED FACTS: cannot write {}: {}", self._path, exc)
            return False

    def _evict_if_over_capacity(self) -> None:
        if len(self._facts) <= MAX_FACT_ROWS:
            return
        surplus = len(self._facts) - MAX_FACT_ROWS
        ordered = sorted(
            self._facts.items(),
            key=lambda item: (
                parse_timestamp(item[1].last_confirmed_at)
                or datetime.fromtimestamp(0, UTC)
            ),
        )
        for key, _fact in ordered[:surplus]:
            del self._facts[key]
        logger.warning(
            "LEARNED FACTS: at the {}-row ceiling; evicted {} least recently "
            "confirmed row(s)",
            MAX_FACT_ROWS,
            surplus,
        )

    # -- adapters onto the in-memory shapes ------------------------------

    def memory_for(self, provider_id: str) -> RecoveryMemory:
        """Return this provider's recovery memory, pre-loaded and write-through.

        One memory per provider id, shared by every instance the manager
        rebuilds: a config apply used to forget a cap without a restart, and a
        fact about a deployment does not stop being true because the process
        rebuilt an HTTP client.
        """

        memory = self._memories.get(provider_id)
        if memory is None:
            memory = RecoveryMemory()
            memory.sink = _sink_for(self, provider_id)
            self._memories[provider_id] = memory
            self._populate_memory(provider_id, memory)
        return memory

    def withheld_model_ids(
        self, provider_id: str, *, now: datetime | None = None
    ) -> frozenset[str]:
        """The still-applicable model ids this provider refused by name."""

        moment = now or datetime.now(UTC)
        return frozenset(
            fact.model_id
            for fact in self.facts_for_provider(provider_id)
            if fact.fact_kind == FACT_MODEL_WITHHELD and not fact.is_stale(moment)
        )

    def models_validators(self, provider_id: str) -> dict[str, str]:
        """The conditional-GET validators last seen on this provider's /models."""

        fact = self._facts.get(
            (provider_id, PROVIDER_WIDE_MODEL_ID, FACT_MODELS_ETAG, "")
        )
        if fact is None or not isinstance(fact.value, Mapping):
            return {}
        return {
            str(name): str(value)
            for name, value in fact.value.items()
            if isinstance(value, str) and value
        }

    def _populate_memory(self, provider_id: str, memory: RecoveryMemory) -> None:
        now = datetime.now(UTC)
        for fact in self.facts_for_provider(provider_id):
            if fact.is_stale(now):
                continue
            if fact.fact_kind == FACT_OUTPUT_CAP and isinstance(fact.value, int):
                memory.output_caps[fact.model_id] = fact.value
            elif fact.fact_kind == FACT_REASONING_FIELD_REJECTED and fact.detail:
                memory.rejected_reasoning_fields.setdefault(fact.model_id, {})[
                    fact.detail
                ] = fact.last_confirmed_at[:10]
            elif fact.fact_kind == FACT_STREAM_USAGE_UNSUPPORTED:
                memory.stream_usage_unsupported.add(fact.model_id)

    def _resync_memories(self) -> None:
        """Rebuild the handed-out memories after a forget.

        Forgetting has to reach the live provider, or "Forget this fact" would
        remove a row from a page while the clamp it describes kept firing until
        the next restart.
        """

        for provider_id, memory in self._memories.items():
            memory.output_caps.clear()
            memory.rejected_reasoning_fields.clear()
            memory.stream_usage_unsupported.clear()
            self._populate_memory(provider_id, memory)


def _sink_for(store: LearnedFactStore, provider_id: str) -> FactSink:
    def sink(
        fact_kind: str, model_id: str, value: Any, detail: str, evidence: str
    ) -> None:
        store.record(
            provider_id,
            model_id,
            fact_kind,
            value,
            source=SOURCE_REJECTION,
            detail=detail,
            evidence=evidence,
        )

    return sink


_STORE: LearnedFactStore | None = None


def learned_fact_store() -> LearnedFactStore:
    """The process-wide store.

    A module singleton for the same reason ``WITHHELD_MODEL_IDS`` is one: a
    provider instance is rebuilt on every config apply, while what the host
    said about itself is not.
    """

    global _STORE
    if _STORE is None:
        _STORE = LearnedFactStore()
    return _STORE


def set_learned_fact_store(store: LearnedFactStore) -> None:
    """Install a store, for the runtime and for tests."""

    global _STORE
    _STORE = store


def reset_learned_fact_store() -> None:
    """Drop the process-wide store, so the next read builds an empty one."""

    global _STORE
    _STORE = None


def withheld_ids_from(facts: Iterable[LearnedFact]) -> frozenset[str]:
    """The model ids among these facts that are withheld from listings."""

    return frozenset(
        fact.model_id for fact in facts if fact.fact_kind == FACT_MODEL_WITHHELD
    )
