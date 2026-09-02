"""The ordered set of downgrades one provider may try after an upstream 400.

One rung, one recovery, one piece of evidence. The ladder is asked for the next
body after every failed try and answers with a rewrite or with nothing; when it
answers with nothing the error is raised, because an unrecognised 400 must fail
visibly rather than be answered by guessing at a smaller request.

Rung order is narrowest-and-most-certain first: an output cap is a *number the
provider stated*, a stream-shape rejection is a shape the SDK names, a
provider-specific rung knows its own gateway. The generic reasoning strip is
deliberately **last**, because where a provider has its own reasoning recovery
it is strictly the better one -- Mistral strips the rejected field *and* the
replayed thinking blocks its models refuse alongside it, NIM removes the
chat-template pair that is its actual reasoning control -- and firing the
generic rung first would answer a 400 by removing one field, succeed at
nothing, and burn the budget the complete recovery needed.

Every rung logs the token it matched and a bounded excerpt of the provider's
own words, so a request that quietly lost a field can always be traced back to
the sentence that cost it.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from loguru import logger

from .complaint import (
    complaint_evidence_snippet,
    sampling_parameter_evidence,
    upstream_complaint,
)
from .memory import RecoveryMemory
from .output_cap import (
    OPENAI_CHAT_OUTPUT_FIELDS,
    clamp_output_tokens,
    parse_output_token_cap,
)
from .reasoning_reject import (
    clone_body_without_reasoning_field,
    rejected_reasoning_field,
)

#: What a rung returns: the rewritten body plus, when the rung removed a
#: reasoning instruction, the field name so the caller can remember the refusal
#: once the retry has actually been accepted.
type RungResult = tuple[dict[str, Any], str | None] | None

#: A rung: given the upstream error and the body that produced it, either a
#: rewrite to try or ``None`` for "this is not my kind of rejection".
type RungApply = Callable[[Exception, dict[str, Any]], RungResult]


@dataclass(frozen=True, slots=True)
class RecoveryRung:
    """One named downgrade the ladder may try."""

    kind: str
    apply: RungApply
    #: Whether the rung may fire at most once per attempt chain. The output cap
    #: is the one rung that may fire repeatedly: each firing lowers the number
    #: to something the host stated, so a second, smaller cap is new evidence
    #: rather than a repeat of the same guess.
    once: bool = True


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    """The ladder's answer for one failed try."""

    body: dict[str, Any] | None = None
    kind: str = ""
    stripped_reasoning_field: str | None = None

    @property
    def retry(self) -> bool:
        """Whether the caller should try again with :attr:`body`."""
        return self.body is not None


GIVE_UP = RecoveryDecision()
"""No rung recognised this rejection; raise it."""


class RecoveryLadder:
    """An ordered set of rungs, consulted once per failed try."""

    def __init__(self, rungs: Iterable[RecoveryRung]) -> None:
        self.rungs = tuple(rungs)

    def next_body(
        self,
        error: Exception,
        body: dict[str, Any],
        used_kinds: set[str],
    ) -> RecoveryDecision:
        """Return the next downgraded body, or :data:`GIVE_UP`.

        ``used_kinds`` is per attempt chain and owned by the caller, so a
        request never strips two reasoning fields in one chain and concurrent
        requests through the same provider never share it.
        """
        for rung in self.rungs:
            if rung.once and rung.kind in used_kinds:
                continue
            result = rung.apply(error, body)
            if result is None:
                continue
            retry_body, stripped = result
            if rung.once:
                used_kinds.add(rung.kind)
            return RecoveryDecision(
                body=retry_body,
                kind=rung.kind,
                stripped_reasoning_field=stripped,
            )
        return GIVE_UP


@dataclass(slots=True)
class OutputCapRecovery:
    """Learn an upstream output-token cap from a 400 and clamp for one retry."""

    memory: RecoveryMemory
    log_tag: str
    fields: tuple[str, ...] = OPENAI_CHAT_OUTPUT_FIELDS
    kind: str = "output_cap"

    def rung(self) -> RecoveryRung:
        return RecoveryRung(kind=self.kind, apply=self, once=False)

    def apply_learned(self, body: dict[str, Any]) -> dict[str, Any]:
        """Clamp output tokens to a previously learned cap for this model.

        This runs after the routed budget has already been sized from the
        model's published limit, and it only ever lowers -- which is what makes
        it the deciding word. A cap learned from the provider's own 400 is
        ground truth about this deployment; a catalogue limit is a published
        claim, and where a gateway resells a model on a smaller deployment the
        claim is the one that is wrong. It never raises the budget: the 400
        says "at most N", which does not contradict a catalogue value below N.
        """
        model = body.get("model")
        if not isinstance(model, str):
            return body
        cap = self.memory.cap_for(model)
        if cap is None:
            return body
        clamped = clamp_output_tokens(body, cap, fields=self.fields)
        return clamped if clamped is not None else body

    def __call__(self, error: Exception, body: dict[str, Any]) -> RungResult:
        cap = parse_output_token_cap(error, fields=self.fields)
        if cap is None:
            return None
        model = body.get("model")
        if isinstance(model, str):
            cap = self.memory.learn_cap(model, cap)
        clamped = clamp_output_tokens(body, cap, fields=self.fields)
        if clamped is None:
            return None
        logger.warning(
            "{}: clamping output tokens to {} after upstream cap rejection",
            self.log_tag,
            cap,
        )
        return clamped, None


@dataclass(slots=True)
class ReasoningStripRecovery:
    """Strip, for one retry, the reasoning field this 400 named.

    Nothing is remembered here. A rejection is an inference, not a stated fact
    the way an output cap is, so the table is written only once the stripped
    request has actually been accepted -- otherwise a request that would have
    failed anyway teaches the process to stop asking for thinking on a model
    that was never the problem.
    """

    log_tag: str
    kind: str = "reasoning_field"

    def rung(self) -> RecoveryRung:
        return RecoveryRung(kind=self.kind, apply=self)

    def __call__(self, error: Exception, body: dict[str, Any]) -> RungResult:
        rejected = rejected_reasoning_field(error, body)
        if rejected is None:
            complaint = upstream_complaint(error)
            sampling = sampling_parameter_evidence(complaint)
            if sampling is not None:
                logger.warning(
                    "{}: 400 names sampling parameter {!r}, not a "
                    "reasoning field -- failing rather than silently dropping "
                    "thinking: {}",
                    self.log_tag,
                    sampling,
                    complaint_evidence_snippet(complaint),
                )
            return None
        retry_body = clone_body_without_reasoning_field(body, rejected)
        if retry_body is None:
            return None
        logger.warning(
            "{}: retrying without {!r} -- upstream named it: {}",
            self.log_tag,
            rejected,
            complaint_evidence_snippet(upstream_complaint(error)),
        )
        return retry_body, rejected


@dataclass(slots=True)
class ProviderRecovery:
    """A rung whose whole decision is a provider's own predicate and rewrite.

    The escape hatch for the recoveries only one gateway knows about -- NIM's
    chat-template pair, DeepSeek's forced ``tool_choice``, the SDK's
    ``stream_options`` shape. It carries no matcher of its own on purpose: the
    provider owns the judgement, the ladder owns only the order.
    """

    kind: str
    rewrite: Callable[[Exception, dict[str, Any]], dict[str, Any] | None]

    def rung(self) -> RecoveryRung:
        return RecoveryRung(kind=self.kind, apply=self)

    def __call__(self, error: Exception, body: dict[str, Any]) -> RungResult:
        retry_body = self.rewrite(error, body)
        if retry_body is None:
            return None
        return retry_body, None
