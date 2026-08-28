"""Application-owned model metadata."""

from dataclasses import dataclass

from my_claude_code.core.reasoning import ReasoningEffort


@dataclass(frozen=True, slots=True)
class ModelReasoningCapability:
    """What one model is known to support for reasoning/thinking control.

    Every field is ``None`` when models.dev (or a provider) has not stated an
    opinion, which is deliberately distinct from a value that states the
    model *lacks* the capability (e.g. ``can_reason=False``, or
    ``supports_effort_control=False``). A later PR that consumes this must be
    able to tell "known unsupported" from "unknown" so it only changes
    request-building behavior for capabilities it can actually confirm.

    The three ``supports_*_control`` booleans mirror the three
    ``reasoning_options`` entry types models.dev publishes (``effort``,
    ``toggle``, ``budget_tokens``) rather than a single enum/flag set, because
    a model can advertise more than one control style at once (e.g. both an
    effort enum and a raw token budget) and call sites care about each style
    independently (e.g. "can I send an effort string" vs "can I send a
    numeric budget").
    """

    can_reason: bool | None = None
    supports_effort_control: bool | None = None
    supports_toggle_control: bool | None = None
    supports_budget_control: bool | None = None
    # Populated only when ``supports_effort_control`` is True; the enum
    # values models.dev's ``effort`` values list maps onto, after dropping
    # any string that is not a member of ``ReasoningEffort`` (e.g. the stray
    # "default" value some entries carry). ``None`` means unknown; an empty
    # frozenset means the effort option is known to have no usable values.
    supported_efforts: frozenset[ReasoningEffort] | None = None
    # True only when the model cannot run with thinking disabled: an OFF
    # request must be rewritten to the floor (lowest supported effort, or
    # adaptive when no vocabulary is known) instead, because the provider
    # rejects disabled thinking outright. ``None`` -- unknown -- must never
    # change behavior, exactly like every other field here. models.dev
    # publishes no such flag today, so this stays None until a source
    # carries it; the gating branch exists so the rewrite is ready.
    mandatory: bool | None = None


@dataclass(frozen=True, slots=True)
class ProviderModelInfo:
    """Provider model metadata used to shape the application model catalog."""

    model_id: str
    supports_thinking: bool | None = None
    # ``None`` means the provider does not report image support, which is not
    # the same as reporting that it has none: vision routing only diverts a
    # request when a model is known to lack it.
    supports_vision: bool | None = None
    context_length: int | None = None
    input_price: float | None = None
    output_price: float | None = None


@dataclass(frozen=True, slots=True)
class ProviderModelRefreshResult:
    """Per-provider outcome of one model-catalog refresh."""

    refreshed_provider_ids: tuple[str, ...] = ()
    failed_provider_ids: tuple[str, ...] = ()
