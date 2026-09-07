"""One learned fact about one deployment, and how it stops being true.

Every negative this proxy learns -- a ceiling, a refusal, an absence -- is
learned from something the upstream itself said, and none of them can ever be
contradicted: MCC stops sending the thing that would produce a positive. A
host that raises its own output cap has no way to tell us, because we never
ask for more than the cap again.

That asymmetry is why a persisted negative must expire. The store is durable
so a restart does not re-pay every 400; the TTLs below are what restores the
self-healing property the per-process memories used to get for free from a
restart. Expiry never deletes: a fact past its TTL is loaded, shown on the
Models page marked *stale*, and simply not applied, so the next real request
re-pays one 400 and the row becomes fresh again.

Three evidence classes, three clocks:

* a number or an enum the host **stated** in its own words is durable
  (:data:`STATED_FACT_TTL_SECONDS`, 30 days);
* a refusal **inferred** from "the request worked once the field was gone" is
  weaker (:data:`INFERRED_FACT_TTL_SECONDS`, 7 days);
* a **withheld model id** is the weakest negative this project has -- its own
  docstring calls forgetting it the safety property -- so it lasts hours, not
  days (:data:`WITHHELD_FACT_TTL_SECONDS`, 72 hours).

No response text is ever stored. Evidence is the bounded, redacted excerpt the
recovery matchers already produce for their log lines, and for a probe it is a
status word and at most an HTTP code.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Self

from my_claude_code.core.diagnostics import redact_sensitive_error_text

#: An output-token maximum the host stated in a 400. Value: ``int``.
FACT_OUTPUT_CAP = "output_cap"
#: A reasoning field the host refused, proven by a retry that succeeded
#: without it. Value: ``True`` (the field is the ``model_id``'s companion --
#: see :attr:`LearnedFact.detail`).
FACT_REASONING_FIELD_REJECTED = "reasoning_field_rejected"
#: A model id the backend refused by name. Hide-only. Value: ``True``.
FACT_MODEL_WITHHELD = "model_withheld"
#: ``stream_options.include_usage`` this host answers with a 400. Value: ``True``.
FACT_STREAM_USAGE_UNSUPPORTED = "stream_usage_unsupported"
#: The effort words a host named in its own rejection. Value: ``list[str]``.
FACT_EFFORT_ENUM = "effort_enum"
#: The host answered an image block with a modality 400. Value: ``True``.
FACT_VISION_UNSUPPORTED = "vision_unsupported"
#: The host answered a trivial tool definition with a 400. Value: ``True``.
FACT_TOOL_CALLS_UNSUPPORTED = "tool_calls_unsupported"
#: The validator a provider's ``/models`` last sent, replayed on the next
#: sweep. Provider-wide, ``source=observation``. Value: ``dict[str, str]``.
FACT_MODELS_ETAG = "models_etag"

#: The allow-list. Whatever lands in this document is read back into live
#: request-shaping state, so an unknown kind is dropped with one log line
#: rather than carried -- the rule ``config/model_overrides.py`` already
#: applies to override parameters, for the same reason.
ALLOWED_FACT_KINDS: frozenset[str] = frozenset(
    {
        FACT_OUTPUT_CAP,
        FACT_REASONING_FIELD_REJECTED,
        FACT_MODEL_WITHHELD,
        FACT_STREAM_USAGE_UNSUPPORTED,
        FACT_EFFORT_ENUM,
        FACT_VISION_UNSUPPORTED,
        FACT_TOOL_CALLS_UNSUPPORTED,
        FACT_MODELS_ETAG,
    }
)

#: The host said it, in a rejection of a request MCC actually sent.
SOURCE_REJECTION = "rejection"
#: An operator pressed *Probe capabilities* and the host answered.
SOURCE_PROBE = "probe"
#: Read off a response MCC was not asking a question with (a validator header).
SOURCE_OBSERVATION = "observation"

ALLOWED_FACT_SOURCES: frozenset[str] = frozenset(
    {SOURCE_REJECTION, SOURCE_PROBE, SOURCE_OBSERVATION}
)

#: The ``model_id`` of a fact about the whole provider rather than one model.
PROVIDER_WIDE_MODEL_ID = "*"

DAY_SECONDS = 86_400.0

#: A cap or an enum the host stated. Durable: it is a published property of
#: the deployment, not an inference.
STATED_FACT_TTL_SECONDS = 30 * DAY_SECONDS
#: A refusal inferred from a successful strip. Weaker evidence, shorter clock.
INFERRED_FACT_TTL_SECONDS = 7 * DAY_SECONDS
#: A withheld model id. The weakest negative here: a 404 that was really an
#: outage, or a model the vendor has since launched, must not be a permanent
#: hole in the catalogue.
WITHHELD_FACT_TTL_SECONDS = 72 * 3600.0

FACT_TTL_SECONDS: Mapping[str, float] = {
    FACT_OUTPUT_CAP: STATED_FACT_TTL_SECONDS,
    FACT_EFFORT_ENUM: STATED_FACT_TTL_SECONDS,
    FACT_MODELS_ETAG: STATED_FACT_TTL_SECONDS,
    FACT_REASONING_FIELD_REJECTED: INFERRED_FACT_TTL_SECONDS,
    FACT_STREAM_USAGE_UNSUPPORTED: INFERRED_FACT_TTL_SECONDS,
    FACT_VISION_UNSUPPORTED: INFERRED_FACT_TTL_SECONDS,
    FACT_TOOL_CALLS_UNSUPPORTED: INFERRED_FACT_TTL_SECONDS,
    FACT_MODEL_WITHHELD: WITHHELD_FACT_TTL_SECONDS,
}

#: Long enough to recognise the sentence, short enough that no transcript,
#: prompt echo or key fragment can survive being stored.
MAX_EVIDENCE_CHARS = 160

#: A gateway listing hundreds of models could grow this file without bound, so
#: it has a stated ceiling and evicts the least recently confirmed row first.
#: One WARNING when it fires, because silently dropping a learned fact would
#: look exactly like the fact never being learned.
MAX_FACT_ROWS = 5000

DOCUMENT_VERSION = 1
FACTS_KEY = "facts"
VERSION_KEY = "version"

#: What a :class:`RecoveryMemory` calls when it learns something worth keeping:
#: ``(fact_kind, model_id, value, detail, evidence)``. The memory does not know
#: which provider it belongs to; the store binds that when it hands one out.
type FactSink = Callable[[str, str, Any, str, str], None]


def utc_now_iso() -> str:
    """The timestamp format every row in this document uses."""

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_timestamp(raw: object) -> datetime | None:
    """Read one stored timestamp, tolerating anything a hand-edit produced."""

    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def bounded_evidence(text: str) -> str:
    """Redact and bound one excerpt of a host's own words.

    Two rules, both hard: the redactor that already guards discovery failures
    runs first, and what survives is truncated. A learned fact is a permanent
    record, and a permanent record of an upstream error body is the one place
    a bearer token would be least noticed.
    """

    redacted = redact_sensitive_error_text((text or "").strip())
    collapsed = " ".join(redacted.split())
    if len(collapsed) <= MAX_EVIDENCE_CHARS:
        return collapsed
    return collapsed[: MAX_EVIDENCE_CHARS - 1] + "…"


@dataclass(frozen=True, slots=True)
class LearnedFact:
    """One row: what was learned, about what, from what, and when."""

    provider_id: str
    model_id: str
    fact_kind: str
    value: Any
    learned_at: str
    last_confirmed_at: str
    source: str
    #: The second half of a key whose subject is not the model alone -- the
    #: refused reasoning field, for instance. Empty for every other kind.
    detail: str = ""
    evidence: str = ""
    hits: int = 1
    #: Set when the model disappeared from the provider's catalogue. The
    #: deployment the fact describes is gone, so the fact stops being applied
    #: without its ``last_confirmed_at`` being falsified to say so.
    retired: bool = False

    @property
    def key(self) -> tuple[str, str, str, str]:
        """The identity a fact is stored under."""

        return (self.provider_id, self.model_id, self.fact_kind, self.detail)

    @property
    def ttl_seconds(self) -> float:
        """How long this evidence class stays applicable."""

        return FACT_TTL_SECONDS.get(self.fact_kind, INFERRED_FACT_TTL_SECONDS)

    def age_seconds(self, now: datetime) -> float:
        """Seconds since the last piece of evidence re-affirmed this fact."""

        confirmed = parse_timestamp(self.last_confirmed_at)
        if confirmed is None:
            # An unreadable timestamp is not evidence of freshness.
            return float("inf")
        return max(0.0, (now - confirmed).total_seconds())

    def is_stale(self, now: datetime) -> bool:
        """Whether this fact has stopped being applied.

        Stale is never deletion. The row stays in the file and stays on the
        page, so an operator can see what MCC used to believe and why it
        stopped believing it.
        """

        return self.retired or self.age_seconds(now) > self.ttl_seconds

    def confirmed(self, *, value: Any, evidence: str, now_iso: str) -> Self:
        """Re-affirm this fact from new evidence, keeping ``learned_at``."""

        return replace(
            self,
            value=value,
            evidence=evidence or self.evidence,
            last_confirmed_at=now_iso,
            hits=self.hits + 1,
            retired=False,
        )

    def as_row(self) -> dict[str, Any]:
        """Render to the on-disk shape."""

        row: dict[str, Any] = {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "fact_kind": self.fact_kind,
            "value": self.value,
            "learned_at": self.learned_at,
            "last_confirmed_at": self.last_confirmed_at,
            "source": self.source,
            "evidence": self.evidence,
            "hits": self.hits,
        }
        if self.detail:
            row["detail"] = self.detail
        if self.retired:
            row["retired"] = True
        return row


def fact_from_row(row: object) -> LearnedFact | None:
    """Build one fact from a parsed row, or ``None`` when it is unusable.

    Deliberately strict and deliberately silent about *why* beyond the caller's
    one log line: a row this cannot read is a row nothing should act on.
    """

    if not isinstance(row, Mapping):
        return None
    provider_id = str(row.get("provider_id") or "").strip()
    model_id = str(row.get("model_id") or "").strip()
    fact_kind = str(row.get("fact_kind") or "").strip()
    source = str(row.get("source") or "").strip()
    if not provider_id or not model_id or fact_kind not in ALLOWED_FACT_KINDS:
        return None
    if source not in ALLOWED_FACT_SOURCES:
        return None
    learned_at = str(row.get("learned_at") or "").strip()
    last_confirmed_at = str(row.get("last_confirmed_at") or learned_at).strip()
    if parse_timestamp(last_confirmed_at) is None:
        return None
    hits = row.get("hits")
    return LearnedFact(
        provider_id=provider_id,
        model_id=model_id,
        fact_kind=fact_kind,
        value=row.get("value"),
        learned_at=learned_at or last_confirmed_at,
        last_confirmed_at=last_confirmed_at,
        source=source,
        detail=str(row.get("detail") or ""),
        evidence=bounded_evidence(str(row.get("evidence") or "")),
        hits=hits if isinstance(hits, int) and not isinstance(hits, bool) else 1,
        retired=bool(row.get("retired")),
    )
