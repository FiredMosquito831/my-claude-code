"""Serialise the neutral catalogue into Pi's ``ProviderModelConfig`` shape.

What this replaced: a TypeScript builder inside the bundled Pi extension that
gave every model ``contextWindow: 128000``, ``maxTokens: 16384`` and
``cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0}``, and decided
``reasoning`` purely from which id prefix the model had arrived under. It could
not have done better: it read ``GET /v1/models``, which carries no capability
fields. The extension now fetches the capability-bearing route instead and
registers what MCC's ladder actually resolved.

Pi's ``ProviderModelConfig`` requires every field it declares, so nothing here
can be omitted the way Codex's optional keys can. Unknowns therefore take Pi's
own defaults and are recorded; read ``application/catalogues/base.py``.
"""

from collections.abc import Iterable
from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues.base import (
    DEFAULTED_KEY,
    DefaultedFields,
    can_reason,
    visible_entries,
)

#: The values Pi's own extension surface uses when a provider states nothing.
#: They are Pi's numbers, not MCC's: every use is recorded under
#: ``_mcc_defaulted`` so a reader can see which figure is a guess. This dict is
#: the one place in this module allowed to hold a literal limit; the static
#: guard test keys off its name.
CLI_DOCUMENTED_DEFAULTS: dict[str, Any] = {
    "contextWindow": 128000,
    "maxTokens": 16384,
    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
}


def build_pi_catalogue(
    models: Iterable[CatalogueModel],
) -> tuple[dict[str, Any], DefaultedFields]:
    """Return Pi's model list and the record of what had to be defaulted."""

    defaulted = DefaultedFields()
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    for model in visible_entries(models):
        model_id = model.provider_model_ref
        if model_id in seen:
            continue
        seen.add(model_id)
        entries.append(_entry(model, model_id, defaulted))

    document: dict[str, Any] = {"models": entries}
    if defaulted.by_model:
        document[DEFAULTED_KEY] = defaulted.as_document()
    return document, defaulted


def _entry(
    model: CatalogueModel, model_id: str, defaulted: DefaultedFields
) -> dict[str, Any]:
    reasons = can_reason(model.reasoning)
    if model.force_no_thinking:
        reasons = False
    elif reasons is None:
        # Pi's field is a plain boolean with no "unknown" to express. False is
        # the safe half of the guess: it offers no control the model may
        # reject, and it is recorded so the guess is visible.
        defaulted.record(model_id, "reasoning")
        reasons = False

    context_window = model.context_length
    if context_window is None:
        context_window = CLI_DOCUMENTED_DEFAULTS["contextWindow"]
        defaulted.record(model_id, "contextWindow")

    max_tokens = model.max_output_tokens
    if max_tokens is None:
        max_tokens = CLI_DOCUMENTED_DEFAULTS["maxTokens"]
        defaulted.record(model_id, "maxTokens")

    return {
        "id": model_id,
        "name": model_id,
        "reasoning": reasons,
        "input": _input_modalities(model, model_id, defaulted),
        "cost": _cost(model, model_id, defaulted),
        "contextWindow": context_window,
        "maxTokens": max_tokens,
    }


def _input_modalities(
    model: CatalogueModel, model_id: str, defaulted: DefaultedFields
) -> list[str]:
    if model.supports_vision is True:
        return ["text", "image"]
    if model.supports_vision is None:
        defaulted.record(model_id, "input")
    return ["text"]


def _cost(
    model: CatalogueModel, model_id: str, defaulted: DefaultedFields
) -> dict[str, float]:
    if model.input_price is None or model.output_price is None:
        defaulted.record(model_id, "cost")
        return dict(CLI_DOCUMENTED_DEFAULTS["cost"])
    # Pi has no separate cached-token rates and MCC resolves none, so the two
    # cache figures stay at Pi's own default rather than being invented from
    # the uncached rate.
    defaulted.record(model_id, "cost.cacheRead")
    defaulted.record(model_id, "cost.cacheWrite")
    return {
        "input": model.input_price,
        "output": model.output_price,
        "cacheRead": CLI_DOCUMENTED_DEFAULTS["cost"]["cacheRead"],
        "cacheWrite": CLI_DOCUMENTED_DEFAULTS["cost"]["cacheWrite"],
    }
