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

from my_claude_code.config.settings import get_settings
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
#: One reasoning *value* a model refused, proven by a retry that succeeded
#: without it. ``detail`` is ``"<field>=<value>"``; value: ``True``.
#:
#: Beside :data:`FACT_REASONING_FIELD_REJECTED`, not instead of it. That one
#: says "this model has no such knob" and costs the whole channel; this one
#: says "this model has the knob and not that setting", which is the smallest
#: thing a 400 on ``xhigh`` actually proves. Before 7.12.0 there was only the
#: first, so one 400 on ``xhigh`` pinned a model to the endpoint's own default
#: for the rest of the TTL -- including the requests that only wanted
#: ``medium``.
FACT_EFFORT_VALUE_REJECTED = "effort_value_rejected"
#: The host answered an image block with a modality 400. Value: ``True``.
FACT_VISION_UNSUPPORTED = "vision_unsupported"
#: The host answered a trivial tool definition with a 400. Value: ``True``.
FACT_TOOL_CALLS_UNSUPPORTED = "tool_calls_unsupported"
#: The validator a provider's ``/models`` last sent, replayed on the next
#: sweep. Provider-wide, ``source=observation``. Value: ``dict[str, str]``.
FACT_MODELS_ETAG = "models_etag"
#: This host acts on who is calling: it either refused a request naming a
#: client header, or it benched the credential on a free daily quota that
#: only requests carrying the right identity are counted generously
#: against. Provider-wide. Value: ``True`` when the host was seen doing
#: it, ``False`` when a probe looked and found no difference -- which is a
#: real answer with a date on it, not an absence.
FACT_CLIENT_IDENTITY_REQUIRED = "client_identity_required"
#: Which wire surface this host actually serves one model on, proven by a
#: probe that succeeded there after the resolved surface failed in a
#: surface-shaped way. Value: a :class:`ResponseSurface` value
#: (``chat_completions`` / ``responses`` / ``messages`` / ``unservable``).
#:
#: The *positive* of the pair the rest of this file is made of, and the
#: exception that proves its rule: every other fact here is a negative MCC can
#: never be contradicted about, because it stops sending the thing that would
#: produce a positive. This one is re-proved on every request that uses it --
#: if the learned surface stops answering, the probe runs again and the fact is
#: rewritten. ``unservable`` is the only negative it can hold, and it is a
#: *listing* statement, never a hiding one: the model keeps its row, with the
#: reason on it.
FACT_RESPONSE_SURFACE = "response_surface"
#: The longest tool name this host's Responses surface accepts, stated by the
#: host in a 400 that named the ``name`` parameter. Provider-wide, because a
#: request validator sits in front of the whole deployment rather than behind
#: one model: OpenCode Zen answers the same sentence for muse-spark-1.2 and
#: 1.3, and a per-model fact would re-pay the 400 once for every model in the
#: catalogue. Value: ``int``.
FACT_RESPONSES_TOOL_NAME_MAX_LENGTH = "responses_tool_name_max_length"
#: This model's Responses surface takes no ``tool_choice`` but ``auto``,
#: proven by a retry that succeeded once the field was gone. Value: ``True``.
#:
#: Per ``(provider, model)`` on purpose and on evidence: the 2026-09-17 probe
#: measured the refusal on ``muse-spark-1.3-contributor-free`` only, and a
#: gateway reselling twenty models does not owe them all the same validator.
#: If a host is later shown to refuse it deployment-wide, this fact widens to
#: :data:`PROVIDER_WIDE_MODEL_ID` with no change to anything that reads it --
#: the reader already asks for the model and falls back to the provider row.
FACT_RESPONSES_TOOL_CHOICE_AUTO_ONLY = "responses_tool_choice_auto_only"

#: A JSON-Schema keyword this host's request validator refuses in a tool
#: schema, and the regex construct that made it offend. ``detail`` is
#: ``"<keyword>:<construct>"`` (``"pattern:lookaround"``), or
#: ``"<keyword>:*"`` when the host named the keyword without naming what was
#: wrong with the value. Value: ``True``.
#:
#: Provider-wide, and keyed on the **keyword class** rather than on the tool
#: and JSON path the 400 happened to name. A request validator sits in front
#: of the whole deployment and states a property of its regex engine -- the
#: Rust ``regex`` crate has no lookaround, on every model behind it and for
#: every catalogue. The tool that carried the offending pattern, by contrast,
#: is gone by the next session. Keying it the other way round would re-pay the
#: 400 once per catalogue forever and learn nothing that outlived a
#: conversation.
FACT_RESPONSES_TOOL_SCHEMA_KEYWORD = "responses_tool_schema_keyword"

#: The most tools this host's Responses validator accepts in one request,
#: stated by the host in its own 400 (``Invalid 'tools': array too long.
#: Expected an array with maximum length 128``, code
#: ``array_above_max_length``). Provider-wide, for the reason the tool-name
#: ceiling is: the validator that states it sits in front of the whole
#: deployment, and a per-model row would re-pay the 400 once per model.
#: Value: ``int``.
FACT_RESPONSES_TOOLS_MAX_COUNT = "responses_tools_max_count"

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
        FACT_EFFORT_VALUE_REJECTED,
        FACT_VISION_UNSUPPORTED,
        FACT_TOOL_CALLS_UNSUPPORTED,
        FACT_MODELS_ETAG,
        FACT_CLIENT_IDENTITY_REQUIRED,
        FACT_RESPONSE_SURFACE,
        FACT_RESPONSES_TOOL_NAME_MAX_LENGTH,
        FACT_RESPONSES_TOOL_CHOICE_AUTO_ONLY,
        FACT_RESPONSES_TOOL_SCHEMA_KEYWORD,
        FACT_RESPONSES_TOOLS_MAX_COUNT,
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
    # A number the host named in its own sentence -- the same evidence class
    # as an output cap, and a request validator's ceiling changes about as
    # often.
    FACT_RESPONSES_TOOL_NAME_MAX_LENGTH: STATED_FACT_TTL_SECONDS,
    # The same evidence class, and for the same reason: the host named the
    # keyword and the construct in its own sentence, and what it stated is a
    # property of the regex engine behind its validator rather than an
    # inference from a retry that happened to work.
    FACT_RESPONSES_TOOL_SCHEMA_KEYWORD: STATED_FACT_TTL_SECONDS,
    # A number the host stated, like the tool-name ceiling beside it.
    FACT_RESPONSES_TOOLS_MAX_COUNT: STATED_FACT_TTL_SECONDS,
    # Stated in words but *proven* by the retry, which is the weaker of the
    # two and decides the clock: the host said "only auto", and what MCC
    # wrote down is "the request worked once the field was gone".
    FACT_RESPONSES_TOOL_CHOICE_AUTO_ONLY: INFERRED_FACT_TTL_SECONDS,
    # A published property of the deployment, not an inference: the host
    # either did the thing or a probe watched it not do the thing, and
    # both answers are worth a month before they are asked again.
    FACT_CLIENT_IDENTITY_REQUIRED: STATED_FACT_TTL_SECONDS,
    # An endpoint that answered 200 where the other answered 500 is as stated
    # as evidence gets -- the deployment demonstrated it rather than described
    # it -- but a gateway rearranges which of its APIs serves a model far more
    # often than it changes an output cap (four of Zen's free models moved in
    # six weeks), so this sits on the weaker clock on purpose.
    FACT_RESPONSE_SURFACE: INFERRED_FACT_TTL_SECONDS,
    FACT_REASONING_FIELD_REJECTED: INFERRED_FACT_TTL_SECONDS,
    # The same clock as the field rejection it narrows, deliberately: both are
    # inferences proven by a retry, and a coarse fact that outlived the fine
    # one would silently re-take the whole channel.
    FACT_EFFORT_VALUE_REJECTED: INFERRED_FACT_TTL_SECONDS,
    FACT_STREAM_USAGE_UNSUPPORTED: INFERRED_FACT_TTL_SECONDS,
    FACT_VISION_UNSUPPORTED: INFERRED_FACT_TTL_SECONDS,
    FACT_TOOL_CALLS_UNSUPPORTED: INFERRED_FACT_TTL_SECONDS,
    FACT_MODEL_WITHHELD: WITHHELD_FACT_TTL_SECONDS,
}

#: The three literals above, by the evidence class each one names. Since
#: 7.32.0 each class is also a setting; this is how a class is turned into the
#: setting that overrides it, and the shipped value when there is none.
_TTL_SETTING_BY_SHIPPED: Mapping[float, str] = {
    STATED_FACT_TTL_SECONDS: "stated_fact_ttl_seconds",
    INFERRED_FACT_TTL_SECONDS: "inferred_fact_ttl_seconds",
    WITHHELD_FACT_TTL_SECONDS: "withheld_fact_ttl_seconds",
}


def fact_ttl_seconds(fact_kind: str) -> float:
    """How long a fact of this kind stays applicable, per the operator.

    Asked per fact rather than captured once, which is what makes the three
    settings hot: the store reads this while it walks its rows, so a shortened
    clock applies to the next row rather than to the next restart. A settings
    object that will not load is never a reason a learned fact changes meaning
    -- the shipped number answers instead.
    """

    shipped = FACT_TTL_SECONDS.get(fact_kind, INFERRED_FACT_TTL_SECONDS)
    attribute = _TTL_SETTING_BY_SHIPPED.get(shipped)
    if attribute is None:
        return shipped
    try:
        return float(getattr(get_settings(), attribute))
    except Exception:
        return shipped


#: Long enough to recognise the sentence, short enough that no transcript,
#: prompt echo or key fragment can survive being stored.
MAX_EVIDENCE_CHARS = 160

#: A gateway listing hundreds of models could grow this file without bound, so
#: it has a stated ceiling and evicts the least recently confirmed row first.
#: One WARNING when it fires, because silently dropping a learned fact would
#: look exactly like the fact never being learned.
MAX_FACT_ROWS = 5000

#: The shape of the stored document.
#:
#: ``2`` (7.12.0) is the first version whose reasoning learnings can be
#: value-level. Every row a version-1 document holds is still readable --
#: nothing about a row's *fields* changed -- but one **meaning** did: before
#: 7.12.0 a :data:`FACT_REASONING_FIELD_REJECTED` row was the only thing a
#: 400 on a single effort word could be written down as, so a version-1
#: document cannot distinguish "this model has no reasoning knob" from "this
#: model would not take ``xhigh`` that once". Carrying those rows forward
#: would pin models to the endpoint default that 7.12.0 would have kept
#: thinking on, and for the rest of a six-week TTL. See
#: :data:`SUPERSEDED_KINDS_BEFORE_VERSION`.
DOCUMENT_VERSION = 2
FACTS_KEY = "facts"
VERSION_KEY = "version"

#: Fact kinds a document written before a given version cannot be trusted on,
#: and which are therefore dropped when that document is adopted.
#:
#: Dropping costs at most one re-paid 400 per model -- the same price this
#: store already declares acceptable for a file it cannot read at all -- and
#: buys back every effort the coarse row was silently spending.
SUPERSEDED_KINDS_BEFORE_VERSION: Mapping[int, frozenset[str]] = {
    2: frozenset({FACT_REASONING_FIELD_REJECTED}),
}


def superseded_kinds(document_version: int) -> frozenset[str]:
    """Fact kinds a document of this version must not be believed about."""

    superseded: set[str] = set()
    for version, kinds in SUPERSEDED_KINDS_BEFORE_VERSION.items():
        if document_version < version:
            superseded.update(kinds)
    return frozenset(superseded)


def document_version_of(document: Mapping[Any, Any]) -> int:
    """The version a stored document declares; ``1`` when it declares none.

    A missing marker means 6.51.0-era code wrote it, which is version 1 by
    definition -- that release is what introduced the file. Anything
    unreadable is treated the same way, because the conservative reading is
    the older one: it can only cause a migration to run twice, never to be
    skipped.
    """

    raw = document.get(VERSION_KEY)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 1
    return raw if raw >= 1 else 1


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
        """How long this evidence class stays applicable.

        Read through :func:`fact_ttl_seconds`, which asks ``Settings`` rather
        than the table above, so the three clocks are live: an operator who
        shortens ``INFERRED_FACT_TTL_SECONDS`` on the Model Config page
        changes what the very next row is worth, with no restart. The table
        stays the shipped answer and the mapping from kind to class.
        """

        return fact_ttl_seconds(self.fact_kind)

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
