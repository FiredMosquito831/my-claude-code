"""Application-owned model metadata."""

from dataclasses import dataclass
from enum import StrEnum

from my_claude_code.core.reasoning import ReasoningEffort


class ModelListingProvenance(StrEnum):
    """Why one model id is in a provider's catalogue at all.

    Existence provenance, deliberately separate from the per-field
    :class:`~my_claude_code.core.model_ids.ResolutionTier` a capability value
    carries. The two answer different questions: the tier says where a
    *number* came from, this says why the row is on the page. A provider whose
    gateway publishes no ``/models`` has to answer the second question from
    somewhere, and the honest answer is not always the same rung.

    Ordered strongest-first, and that order is the merge order in
    :mod:`my_claude_code.providers.runtime.served_models`.
    """

    #: The provider's own ``/models`` endpoint answered.
    GATEWAY = "gateway"
    #: The backend's official client ships a catalogue, read from disk. No
    #: network, no credential, and it updates when the user updates that
    #: client.
    VENDOR_CLIENT = "vendor_client"
    #: This credential has already been served this id successfully. The
    #: strongest claim available offline: it is proof rather than a document.
    OBSERVED = "observed"
    #: A reference catalogue (models.dev) that describes the same vendor but
    #: not necessarily the same deployment.
    MODELS_DEV = "models_dev"
    #: A literal list compiled by hand. Nobody stands behind it; it exists so
    #: a fresh offline install has a non-empty picker.
    SEED = "seed"


@dataclass(frozen=True, slots=True)
class ModelListingEvidence:
    """Why one model id is listed, and what its own catalogue said about it.

    Existence facts only. Nothing here is a capability, a limit or a price:
    those keep coming off the resolution ladder, whatever this says. A rung
    that can name a model but not measure it must be able to say so without
    smuggling a number in beside the name.

    ``retirement_at`` and ``replacement_model_id`` are the vendor's own words,
    stored verbatim as they were published. ``offered_by_default`` says the
    vendor's own catalogue offers this model by default; a model the vendor
    marked hidden does not reach a listing at all, so it never reaches this
    record either -- it stays routable, and this project's own visibility
    globs remain the operator's mechanism for hiding anything else.
    """

    provenance: ModelListingProvenance
    #: One sentence naming the source, for the admin page. Never a credential,
    #: never a token, never a path outside the vendor's own install.
    detail: str = ""
    #: The vendor's published retirement instant, verbatim. ``None`` means no
    #: retirement was published, never "not retired soon".
    retirement_at: str | None = None
    #: What the vendor says replaces this model once it retires.
    replacement_model_id: str | None = None
    #: ``True`` when the vendor's own catalogue offers the model by default.
    #: ``None`` when no source said either way -- which includes a vendor
    #: ``visibility`` value this build does not recognise, because unknown is
    #: not the same answer as "no".
    offered_by_default: bool | None = None


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

    Three states must stay distinguishable, and every parser and merge here is
    written to preserve them:

    1. *unknown* -- no source had an opinion; every field is ``None``. The
       lookups return ``None`` in place of the whole object when not even the
       model was found.
    2. *known, no caller control* -- the model reasons but exposes no knob.
       models.dev spells this ``reasoning: true`` with ``reasoning_options:
       []`` (23% of its reasoning models), which parses to ``can_reason=True``
       with all three ``supports_*_control`` explicitly ``False``.
    3. *known, controllable* -- the usual effort/toggle/budget shapes.

    ``supported_efforts`` carries the same distinction one level down:
    ``None`` when no effort option was published at all, and an empty
    ``frozenset`` when one was published with no usable values.
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
    # request is rewritten to the floor (lowest supported effort, or adaptive
    # when no vocabulary is known) instead, because the provider rejects
    # disabled thinking outright. ``None`` -- unknown -- must never change
    # behavior, exactly like every other field here. models.dev publishes no
    # such key on any of its 7,483 rows; OpenRouter-dialect gateways publish it
    # as ``reasoning.mandatory`` and that is the only source that populates it.
    mandatory: bool | None = None
    # Whether the model runs with thinking on unless the caller says otherwise.
    # Published by OpenRouter-dialect gateways as ``reasoning.default_enabled``
    # and by nothing else today. ``None`` -- unknown -- must never change
    # behavior; it is not the same as a known ``False``.
    #
    # Deliberately not consumed by gating. Every decision that could plausibly
    # read it is already answered better by a field that states the thing
    # directly: whether the model reasons is ``can_reason``, whether it can be
    # turned off is ``mandatory``, and whether a knob exists is the three
    # ``supports_*_control`` flags. The one remaining use -- emitting a
    # reasoning block because a model happens to default to thinking on --
    # would send reasoning nobody asked for, which is the opposite of what a
    # policy expressing no opinion means. Kept because it is real published
    # metadata surfaced in the admin model view, not because gating needs it.
    default_enabled: bool | None = None


type ModelDefaultParameterValue = str | int | float | bool
"""A scalar a provider may pin as a per-model default request parameter."""

type ModelDefaultParameters = tuple[tuple[str, ModelDefaultParameterValue], ...]
"""Provider-declared per-model default parameters, sorted by name.

A tuple of pairs rather than a mapping because :class:`ProviderModelInfo` is
hashable and lives in ``frozenset``s. Only scalar values are carried: every
pinned value observed upstream (``temperature``, ``top_p``, ``top_k``) is a
scalar, and a non-scalar default (an array such as ``stop``) is dropped rather
than encoded, because a partially-representable structure would be worse than
an absent one.
"""


@dataclass(frozen=True, slots=True)
class ProviderModelInfo:
    """Provider model metadata used to shape the application model catalog.

    Every optional field is ``None`` when the provider did not report it. That
    is deliberately distinct from a reported value that states the model lacks
    the capability (``supports_vision=False``) or has no such declaration
    (``default_parameters=()``). Consumers must only act on what a source
    actually stated.
    """

    model_id: str
    supports_thinking: bool | None = None
    # ``None`` means the provider does not report image support, which is not
    # the same as reporting that it has none: vision routing only diverts a
    # request when a model is known to lack it.
    supports_vision: bool | None = None
    context_length: int | None = None
    input_price: float | None = None
    output_price: float | None = None
    # The provider's own declared ceiling on generated tokens for this model
    # (OpenRouter dialect: ``top_provider.max_completion_tokens``). Distinct
    # from ``context_length``, which covers prompt plus completion. ``None``
    # means unreported; a non-positive upstream value is read as unreported
    # too, never as "this model can emit zero tokens".
    max_output_tokens: int | None = None
    # The complete ``supported_parameters`` list a gateway publishes, not just
    # the reasoning flag distilled from it. ``None`` means the provider did not
    # publish the list; an empty frozenset means it published an empty one.
    supported_parameters: frozenset[str] | None = None
    # Values the provider pins for this model and rejects being overridden
    # (observed live as ``400 top_p is immutable ... must be 0.95``).
    default_parameters: ModelDefaultParameters | None = None
    # Reasoning capability as the *provider* reports it, which outranks the
    # models.dev fallback field by field. ``None`` means the provider published
    # no reasoning block at all.
    reasoning_capability: ModelReasoningCapability | None = None
    # Why this id is in the catalogue at all, for providers that have to
    # answer that from something other than a gateway ``/models``. Never read
    # by routing and never consulted by the capability ladder: it is a
    # provenance record for the admin page and for the operator's own
    # judgement. ``None`` means nobody recorded one.
    listing: ModelListingEvidence | None = None


@dataclass(frozen=True, slots=True)
class ProviderDiscoveryFailure:
    """Why one provider's model-list query failed, in reportable form.

    Discovery used to swallow this into a log line, so a dashboard card could
    read "healthy" while the catalogue for that provider stayed empty. The
    failure now travels back to the caller that triggered the refresh.
    """

    provider_id: str
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class ProviderModelRefreshResult:
    """Per-provider outcome of one model-catalog refresh."""

    refreshed_provider_ids: tuple[str, ...] = ()
    failed_provider_ids: tuple[str, ...] = ()
    failures: tuple[ProviderDiscoveryFailure, ...] = ()

    def failure_for(self, provider_id: str) -> ProviderDiscoveryFailure | None:
        """Return the recorded failure for one provider, if it failed."""
        for failure in self.failures:
            if failure.provider_id == provider_id:
                return failure
        return None
