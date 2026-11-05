"""Provider-owned reasoning translations for OpenAI-compatible APIs."""

from dataclasses import dataclass
from typing import Any, Protocol

from loguru import logger

from my_claude_code.core.reasoning import (
    ADAPTIVE_MEANING,
    ReasoningAdaptationKind,
    ReasoningControl,
    ReasoningDialect,
    ReasoningDialectOrigin,
    ReasoningEffort,
    ReasoningPolicy,
    dialect_declares_a_reasoning_channel,
    nearest_effort,
    top_stated_effort,
)
from my_claude_code.core.wire_capture import record_reasoning_adaptation

EffortValues = tuple[tuple[ReasoningEffort, str], ...]


class ReasoningEncoder(Protocol):
    """Translate provider-neutral reasoning intent into one wire shape."""

    def encode(self, body: dict[str, Any], policy: ReasoningPolicy) -> None: ...

    @property
    def dialect(self) -> ReasoningDialect:
        """Which reasoning fields this encoder can actually put on the wire.

        The declaration and :meth:`encode` are two views of one fact, pinned
        together by a test that encodes every policy shape against every
        profile and asserts the emitted keys are a subset of what the dialect
        claims.
        """
        ...


def _host_vocabulary(efforts: EffortValues) -> frozenset[ReasoningEffort] | None:
    """The FCC efforts this host genuinely has distinct wire values for.

    An encoder's map is not a vocabulary: several FCC rungs routinely collapse
    onto one wire word (Cohere sends "high" for all six; Gemini folds xhigh and
    max onto "high"; Command Code has no "minimal"). Declaring all six keys as
    the host's enum would tell gating that a request for ``max`` is accepted
    verbatim, and the clamp -- the thing the operator needs to see in the
    request log -- would never be recorded.

    A rung counts when the host spells it under its own name. Wire values no
    such rung covers still have to be reachable, so each of those contributes
    its highest mapped rung: a gateway whose words are not FCC's at all
    ("brief", "detailed") keeps a vocabulary the same size as its enum rather
    than losing its effort field entirely.
    """
    mapping = dict(efforts)
    if not mapping:
        return None
    natural = {effort for effort, value in mapping.items() if value == effort.value}
    covered = {mapping[effort] for effort in natural}
    by_value: dict[str, list[ReasoningEffort]] = {}
    for effort, value in mapping.items():
        by_value.setdefault(value, []).append(effort)
    for value, rungs in by_value.items():
        if value not in covered:
            natural.add(
                max(rungs, key=lambda effort: tuple(ReasoningEffort).index(effort))
            )
    return frozenset(natural) or None


def _clamped_effort(
    efforts: EffortValues, requested: ReasoningEffort | None
) -> str | None:
    """Translate ``requested`` into this vocabulary, nearest rung at-or-below.

    ``None`` only when there is nothing to translate (no effort asked for) or
    nothing to translate into. The old behaviour -- an exact ``dict.get`` that
    fell through to the encoder's ``enabled_value`` -- sent the *host's*
    default rung in place of the user's ask, so a client asking for ``max``
    against a ``low/medium/high`` vocabulary got groq's ``medium`` rather than
    ``high``. Gating clamps to the model-and-host intersection before this
    point; this is the belt to that braces, and it is what direct provider use
    (token counting, tests) relies on.
    """
    if requested is None:
        return None
    mapping = dict(efforts)
    if not mapping:
        return None
    exact = mapping.get(requested)
    if exact is not None:
        return exact
    return mapping[nearest_effort(requested, frozenset(mapping))]


def _adaptive_rung(
    efforts: EffortValues,
) -> tuple[ReasoningEffort, str] | None:
    """The top rung this host publishes, and the word it spells for that rung.

    ``None`` when this encoder's map names no rung at all, which is the
    "cannot express it" case rather than a rung to invent.
    """

    top = top_stated_effort(_host_vocabulary(efforts))
    if top is None:
        return None
    word = _clamped_effort(efforts, top)
    if word is None:
        return None
    return top, word


def _note_adaptive_rung(
    *, model: str, field: str, effort: ReasoningEffort, word: str
) -> None:
    """Record that an adaptive request left as a named rung.

    A no-op outside a tracked request (``record_reasoning_adaptation``), so
    the encoders stay usable from token counting and from tests.
    """

    record_reasoning_adaptation(
        ReasoningAdaptationKind.SUBSTITUTED,
        f"REASONING ADAPTIVE SUBSTITUTED: '{model}' has no adaptive channel;"
        f" sending effort '{word}' via `{field}` -- its highest published rung"
        f" -- because a host that can only be told a level cannot be told to"
        f" choose one. Adaptive asks to {ADAPTIVE_MEANING}.",
    )
    logger.info(
        "REASONING ADAPTIVE: '{}' has no adaptive channel; sent effort '{}'"
        " via `{}` (its top published rung, '{}')",
        model,
        word,
        field,
        effort.value,
    )


def _note_adaptive_nothing(*, model: str, dialect: ReasoningDialect) -> None:
    """Record an adaptive request this door could not express at all.

    Only when the host parses *some* reasoning field: a door with none sends
    nothing, and on that door nothing IS the adaptive answer, so a warning
    would be a warning about correct behaviour.
    """

    if not dialect_declares_a_reasoning_channel(dialect):
        logger.info(
            "REASONING ADAPTIVE: '{}' parses no reasoning field; nothing is"
            " sent and the model decides for itself",
            model,
        )
        return
    record_reasoning_adaptation(
        ReasoningAdaptationKind.NOTHING_SENT,
        f"REASONING ADAPTIVE NOTHING SENT: '{model}' parses a reasoning field"
        f" but none that can carry 'let the model choose'; nothing was sent and"
        f" the model's own default applies",
    )
    logger.warning(
        "REASONING ADAPTIVE NOTHING SENT: '{}' has no field for a level-less"
        " reasoning request",
        model,
    )


def _encode_adaptive_rung(
    target: dict[str, Any], field: str, efforts: EffortValues, model: str
) -> bool:
    """Write the top published rung, or report that there is none.

    Returns whether a word was written. Shared by every encoder whose only
    word for "reason" is a rung, so ``NamedEffortReasoning`` and
    ``EffortOrThinkingBudgetReasoning`` cannot drift apart about it.
    """

    rung = _adaptive_rung(efforts)
    if rung is None:
        return False
    effort, word = rung
    target[field] = word
    _note_adaptive_rung(model=model, field=field, effort=effort, word=word)
    return True


def _adaptive_model(body: dict[str, Any]) -> str:
    """The model this body names, for an adaptation message."""

    model = body.get("model")
    return model if isinstance(model, str) else "this model"


@dataclass(frozen=True, slots=True)
class NoReasoning:
    """Leave reasoning computation entirely to the upstream provider."""

    def encode(self, body: dict[str, Any], policy: ReasoningPolicy) -> None:
        return

    @property
    def dialect(self) -> ReasoningDialect:
        return ReasoningDialect()


@dataclass(frozen=True, slots=True)
class NamedEffortReasoning:
    """Encode a provider's documented named-effort vocabulary."""

    efforts: EffortValues
    disabled_value: str | bool | None = None
    enabled_value: str | bool | None = None
    field: str = "reasoning_effort"
    budget_field: str | None = None
    use_extra_body: bool = False
    # Provenance for the Models page. Only the fleet default sets ``DEFAULT``;
    # a profile that names its own vocabulary is a declaration by definition.
    origin: ReasoningDialectOrigin = ReasoningDialectOrigin.DECLARED

    def encode(self, body: dict[str, Any], policy: ReasoningPolicy) -> None:
        target = _extra_body(body) if self.use_extra_body else body
        if policy.control is ReasoningControl.OFF:
            if self.disabled_value is not None:
                target[self.field] = self.disabled_value
            return

        if policy.control is ReasoningControl.ADAPTIVE:
            # No adaptive channel on this shape: ``enabled_value``, when there
            # is one, is written into the effort field and is therefore a
            # default *rung*, not a level-less on -- so the honest answer is
            # this host's own highest rung, read from its declared map.
            if not _encode_adaptive_rung(
                target, self.field, self.efforts, _adaptive_model(body)
            ):
                _note_adaptive_nothing(
                    model=_adaptive_model(body), dialect=self.dialect
                )
            return

        if policy.budget_tokens is not None and self.budget_field is not None:
            target[self.budget_field] = policy.budget_tokens
            return

        effort = _clamped_effort(self.efforts, policy.effort)
        if effort is not None:
            target[self.field] = effort
            return

        if policy.control is ReasoningControl.ON and self.enabled_value is not None:
            target[self.field] = self.enabled_value

    @property
    def dialect(self) -> ReasoningDialect:
        return ReasoningDialect(
            effort_values=_host_vocabulary(self.efforts),
            toggle=self.enabled_value is not None,
            budget=self.budget_field is not None,
            off=self.disabled_value is not None,
            effort_field=self.field,
            toggle_field=self.field if self.enabled_value is not None else "",
            budget_field=self.budget_field or "",
            origin=self.origin,
        )


@dataclass(frozen=True, slots=True)
class ReasoningObject:
    """Encode gateways that accept a top-level ``reasoning`` object."""

    efforts: EffortValues
    supports_budget: bool = True

    def encode(self, body: dict[str, Any], policy: ReasoningPolicy) -> None:
        if policy.control is ReasoningControl.OFF:
            _extra_body(body)["reasoning"] = {"enabled": False}
            return

        if policy.control is ReasoningControl.ADAPTIVE:
            # ``reasoning.enabled`` is a real level-less on: it says "reason"
            # without naming a rung, which is exactly what adaptive asks for.
            # The effort field is deliberately not used -- any word in it would
            # be a level nobody asked for.
            _extra_body(body)["reasoning"] = {"enabled": True}
            return

        reasoning: dict[str, Any] = {}
        if policy.budget_tokens is not None and self.supports_budget:
            reasoning["max_tokens"] = policy.budget_tokens
        elif effort := _clamped_effort(self.efforts, policy.effort):
            reasoning["effort"] = effort
        elif policy.control is ReasoningControl.ON:
            reasoning["enabled"] = True

        if reasoning:
            _extra_body(body)["reasoning"] = reasoning

    @property
    def dialect(self) -> ReasoningDialect:
        return ReasoningDialect(
            effort_values=_host_vocabulary(self.efforts),
            toggle=True,
            budget=self.supports_budget,
            off=True,
            effort_field="reasoning.effort",
            toggle_field="reasoning.enabled",
            budget_field="reasoning.max_tokens" if self.supports_budget else "",
        )


@dataclass(frozen=True, slots=True)
class ThinkingObjectReasoning:
    """Encode providers with an enabled/disabled ``thinking`` object."""

    enabled: dict[str, Any]
    disabled: dict[str, Any]

    def encode(self, body: dict[str, Any], policy: ReasoningPolicy) -> None:
        if policy.control is ReasoningControl.OFF:
            _extra_body(body)["thinking"] = dict(self.disabled)
        elif policy.control is ReasoningControl.ADAPTIVE:
            # A toggle object carries no level, so its "on" is the level-less
            # request adaptive asks for -- the host decides the amount.
            _extra_body(body)["thinking"] = dict(self.enabled)
        elif policy.requests_reasoning:
            _extra_body(body)["thinking"] = dict(self.enabled)

    @property
    def dialect(self) -> ReasoningDialect:
        return ReasoningDialect(toggle=True, off=True, toggle_field="thinking")


@dataclass(frozen=True, slots=True)
class EffortOrThinkingBudgetReasoning:
    """Encode providers whose named-effort and numeric-budget fields are
    mutually exclusive on the wire.

    Some OpenAI-compatible gateways expose two reasoning knobs that the
    vendor's own API rejects together in one request: a string enum
    ``reasoning_effort`` field, and a separate ``thinking`` object carrying
    an exact ``budget_tokens`` integer. This encoder picks exactly one shape
    per request and never emits both:

    - An explicit client ``budget_tokens`` always wins and is sent as
      ``{"type": "enabled", "budget_tokens": N}`` under ``thinking_field``,
      clamped up to ``min_budget_tokens`` (never down, per the vendor's
      documented floor). ``field`` is omitted entirely.
    - Otherwise a named effort is translated through ``efforts`` into
      ``field`` (typically ``reasoning_effort``). ``thinking_field`` is
      omitted entirely.
    - With no effort or budget but reasoning explicitly ``ON``,
      ``enabled_value`` (if set) is sent through ``field``.
    - Reasoning explicitly ``OFF`` sends ``{"type": "disabled"}`` under
      ``thinking_field`` and never sets ``field``.
    """

    efforts: EffortValues
    field: str = "reasoning_effort"
    thinking_field: str = "thinking"
    enabled_value: str | None = None
    min_budget_tokens: int = 1024

    def encode(self, body: dict[str, Any], policy: ReasoningPolicy) -> None:
        if policy.control is ReasoningControl.OFF:
            _extra_body(body)[self.thinking_field] = {"type": "disabled"}
            return

        if policy.control is ReasoningControl.ADAPTIVE:
            # The two knobs are mutually exclusive on this host, and the
            # thinking object here carries a *budget*, not a level-less on --
            # so the effort field is the only word that can say "reason".
            if not _encode_adaptive_rung(
                body, self.field, self.efforts, _adaptive_model(body)
            ):
                _note_adaptive_nothing(
                    model=_adaptive_model(body), dialect=self.dialect
                )
            return

        if policy.budget_tokens is not None:
            budget = max(self.min_budget_tokens, policy.budget_tokens)
            _extra_body(body)[self.thinking_field] = {
                "type": "enabled",
                "budget_tokens": budget,
            }
            return

        effort = _clamped_effort(self.efforts, policy.effort)
        if effort is not None:
            body[self.field] = effort
            return

        if policy.control is ReasoningControl.ON and self.enabled_value is not None:
            body[self.field] = self.enabled_value

    @property
    def dialect(self) -> ReasoningDialect:
        return ReasoningDialect(
            effort_values=_host_vocabulary(self.efforts),
            toggle=self.enabled_value is not None,
            budget=True,
            off=True,
            effort_field=self.field,
            toggle_field=self.field if self.enabled_value is not None else "",
            budget_field=f"{self.thinking_field}.budget_tokens",
        )


@dataclass(frozen=True, slots=True)
class ChatTemplateReasoning:
    """Encode a provider-wide chat-template boolean without model guessing."""

    field: str = "thinking"

    def encode(self, body: dict[str, Any], policy: ReasoningPolicy) -> None:
        if (
            not policy.requests_reasoning
            and policy.control is not ReasoningControl.OFF
            and policy.control is not ReasoningControl.ADAPTIVE
        ):
            return
        kwargs = _nested_dict(_extra_body(body), "chat_template_kwargs")
        kwargs[self.field] = policy.control is not ReasoningControl.OFF

    @property
    def dialect(self) -> ReasoningDialect:
        return ReasoningDialect(
            toggle=True,
            off=True,
            toggle_field=f"chat_template_kwargs.{self.field}",
        )


@dataclass(frozen=True, slots=True)
class LlamaCppReasoning:
    """Encode llama.cpp's per-request numeric thinking budget."""

    def encode(self, body: dict[str, Any], policy: ReasoningPolicy) -> None:
        if policy.control is ReasoningControl.OFF:
            _extra_body(body)["thinking_budget_tokens"] = 0
        elif policy.control is ReasoningControl.ADAPTIVE:
            # This host's only reasoning field is a number, and adaptive names
            # none. Inventing one would be exactly the fabricated control the
            # rule forbids, so nothing is sent and the record says so.
            _note_adaptive_nothing(model=_adaptive_model(body), dialect=self.dialect)
        elif (budget := policy.numeric_budget_tokens) is not None:
            _extra_body(body)["thinking_budget_tokens"] = budget

    @property
    def dialect(self) -> ReasoningDialect:
        return ReasoningDialect(
            budget=True, off=True, budget_field="thinking_budget_tokens"
        )


@dataclass(frozen=True, slots=True)
class SplitReasoningOutput:
    """Request separate reasoning output where compute is not controllable."""

    def encode(self, body: dict[str, Any], policy: ReasoningPolicy) -> None:
        _extra_body(body)["reasoning_split"] = True

    @property
    def dialect(self) -> ReasoningDialect:
        # ``reasoning_split`` asks for reasoning to be returned separately; it
        # controls no computation, so this host parses no control field.
        return ReasoningDialect()


def _extra_body(body: dict[str, Any]) -> dict[str, Any]:
    value = body.setdefault("extra_body", {})
    if not isinstance(value, dict):
        raise TypeError("OpenAI extra_body must be an object.")
    return value


def _nested_dict(container: dict[str, Any], key: str) -> dict[str, Any]:
    value = container.setdefault(key, {})
    if not isinstance(value, dict):
        raise TypeError(f"{key} must be an object.")
    return value


NO_REASONING = NoReasoning()
LLAMACPP_REASONING = LlamaCppReasoning()
SPLIT_REASONING_OUTPUT = SplitReasoningOutput()
