"""Serialise the neutral catalogue into Qwen Code's ``modelProviders`` shape.

Every claim below was read out of the installed CLI's own bundle -- Qwen Code
0.15.11, ``@qwen-code/qwen-code/cli.js`` -- and then confirmed on the wire
against a local Anthropic-Messages endpoint, because the vendor's prose says
only "set ``ANTHROPIC_BASE_URL``" and that undersells what the settings schema
can carry.

What the bundle accepts, verbatim:

* ``settings.modelProviders`` is an object keyed by auth type; the value for
  ``"anthropic"`` is an **array** of model records. Its declared merge
  strategy is ``REPLACE`` (``getSettingsSchema().modelProviders.mergeStrategy``),
  so the highest-precedence scope that names the key supplies the whole list.
* Each record is ``{id, name, baseUrl, envKey, generationConfig?}``. That is
  the exact shape Qwen's own ``/model`` provider wizard writes -- the
  ``getPreviewJson`` callback in the same bundle emits it field for field.
* ``envKey`` names an *environment variable* holding the key:
  ``ModelsConfig`` reads ``process.env[model.envKey]`` and never stores the
  value, so **the proxy token never lands on disk.**
* ``baseUrl`` is handed straight to the official ``@anthropic-ai/sdk``
  client, which appends ``/v1/messages`` itself. It is therefore the proxy
  **root**, not ``<root>/v1`` -- a trailing ``/v1`` would produce
  ``POST /v1/v1/messages``. Confirmed on the wire.
* ``generationConfig`` carries the model's real numbers. The fields Qwen reads
  are ``MODEL_GENERATION_CONFIG_FIELDS``; the three this serialiser can fill
  honestly are ``contextWindowSize``, ``modalities`` and ``reasoning``.

**Reasoning vocabulary.** Qwen's Anthropic generator reads
``generationConfig.reasoning`` as either ``false`` or
``{effort?, budget_tokens?}``, and ``resolveEffectiveEffort`` clamps ``"max"``
down to ``"high"`` for every non-DeepSeek host -- so :data:`QWEN_EFFORTS` is
``low``/``medium``/``high``. There is no per-model *list* of allowed efforts
in the schema, only the one the session runs at, so MCC writes the model's
strongest supported rung as the default and leaves the ``/reasoning`` command
to move it. A model known not to reason gets ``reasoning: false``, which is
the one thing that key can state with certainty.

**Unknown stays unknown.** Every ``generationConfig`` key is optional, so a
``None`` from the ladder omits the key and the omission is recorded under
``_mcc_defaulted`` -- a root key Qwen logs as unknown-and-ignored rather than
rejecting. What Qwen then does instead is written down in
:data:`CLI_DOCUMENTED_DEFAULTS`. Read ``application/catalogues/base.py`` before
changing anything here.
"""

from collections.abc import Iterable
from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues.base import (
    DEFAULTED_KEY,
    DefaultedFields,
    can_reason,
    clamp_efforts,
    visible_entries,
)
from my_claude_code.config.harnesses import (
    QWEN_API_KEY_ENV,
    QWEN_BASE_URL_SENTINEL,
    QWEN_SETTINGS_VERSION,
)
from my_claude_code.core.reasoning import ReasoningEffort

#: The auth type whose array MCC fills. Qwen's ``--auth-type anthropic``
#: selects it, which is why ``mcc-qwen`` needs no ``security.auth`` write at
#: all: ``argv.authType`` outranks both the settings key and the environment
#: in ``loadCliConfig``.
AUTH_TYPE = "anthropic"

#: ``$version`` is written so Qwen does not treat the document as a
#: pre-migration file and rewrite MCC's own catalogue in place on first read.
SETTINGS_VERSION = QWEN_SETTINGS_VERSION

#: Replaced by the caller before the document reaches disk, for the same
#: reason Command Code's is: the serialiser is a pure function of the model
#: records and does not know which port this install listens on.
BASE_URL_SENTINEL = QWEN_BASE_URL_SENTINEL

#: Written verbatim into ``envKey``.
API_KEY_ENV = QWEN_API_KEY_ENV

#: Qwen's own effort vocabulary for the Anthropic wire, weakest first.
QWEN_EFFORTS: tuple[str, ...] = ("low", "medium", "high")

#: MCC effort -> nearest Qwen rung. ``minimal`` folds down to ``low`` and both
#: ``xhigh`` and ``max`` fold up to ``high``, which is what
#: ``resolveEffectiveEffort`` does to ``"max"`` anyway.
QWEN_EFFORT_BY_REASONING_EFFORT: dict[ReasoningEffort, str] = {
    ReasoningEffort.MINIMAL: "low",
    ReasoningEffort.LOW: "low",
    ReasoningEffort.MEDIUM: "medium",
    ReasoningEffort.HIGH: "high",
    ReasoningEffort.XHIGH: "high",
    ReasoningEffort.MAX: "high",
}

#: What Qwen Code itself does when a ``generationConfig`` key is absent, read
#: out of its own 0.15.11 bundle. MCC writes none of these; it omits the key
#: and records the omission. This dict is the one place in this module allowed
#: to hold a literal limit, and the static guard test keys off its name.
CLI_DOCUMENTED_DEFAULTS: dict[str, Any] = {
    # No contextWindowSize means Qwen shows no context gauge for the model and
    # never triggers its own compression pass early; it relies on the server
    # to refuse an over-long request.
    "contextWindowSize": None,
    # No modalities block means Qwen offers text only for the model.
    "modalities": None,
    # No reasoning key means Qwen sends no thinking block and no effort, and
    # lets the server decide.
    "reasoning": None,
}


def build_qwen_catalogue(
    models: Iterable[CatalogueModel],
) -> tuple[dict[str, Any], DefaultedFields]:
    """Return Qwen Code's settings fragment and the record of what was omitted."""

    defaulted = DefaultedFields()
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    for model in visible_entries(models):
        model_id = _model_id(model)
        if model_id in seen:
            continue
        seen.add(model_id)
        entries.append(_entry(model, model_id, defaulted))

    document: dict[str, Any] = {
        "$version": SETTINGS_VERSION,
        "modelProviders": {AUTH_TYPE: entries},
    }
    if defaulted.by_model:
        document[DEFAULTED_KEY] = defaulted.as_document()
    return document, defaulted


def _model_id(model: CatalogueModel) -> str:
    """Return the id Qwen sends as the request's ``model``.

    Qwen passes ``modelProviders[].id`` through to the Anthropic body
    untouched, so this is MCC's gateway id verbatim -- the same string
    ``GET /v1/models`` publishes. The no-thinking variant keeps its full
    prefixed id because that prefix is the entire mechanism that turns
    thinking off.
    """

    return model.gateway_id


def _entry(
    model: CatalogueModel, model_id: str, defaulted: DefaultedFields
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": model_id,
        "name": model.display_name,
        "baseUrl": BASE_URL_SENTINEL,
        "envKey": API_KEY_ENV,
    }
    generation = _generation_config(model, model_id, defaulted)
    if generation:
        entry["generationConfig"] = generation
    return entry


def _generation_config(
    model: CatalogueModel, model_id: str, defaulted: DefaultedFields
) -> dict[str, Any]:
    generation: dict[str, Any] = {}

    if model.context_length is None:
        defaulted.record(model_id, "contextWindowSize")
    else:
        generation["contextWindowSize"] = model.context_length

    modalities = _modalities(model, model_id, defaulted)
    if modalities is not None:
        generation["modalities"] = modalities

    reasoning = _reasoning(model, model_id, defaulted)
    if reasoning is not None:
        generation["reasoning"] = reasoning

    return generation


def _modalities(
    model: CatalogueModel, model_id: str, defaulted: DefaultedFields
) -> dict[str, bool] | None:
    """Return Qwen's modality block, or None when vision support is unknown.

    ``modalities`` is a set of opt-in booleans -- ``image``, ``video``,
    ``audio``, ``pdf`` -- so ``{"image": False}`` and an absent block mean the
    same thing to Qwen. Writing the block only when vision is *known present*
    keeps the file honest: an absent block records "nobody said", which is not
    the same claim as "no images".
    """

    if model.supports_vision is None:
        defaulted.record(model_id, "modalities")
        return None
    if model.supports_vision is False:
        return None
    return {"image": True}


def _reasoning(model: CatalogueModel, model_id: str, defaulted: DefaultedFields) -> Any:
    """Return Qwen's reasoning value: ``False``, an effort block, or None.

    Qwen's ``buildThinkingConfig`` reads this key alone; there is no per-model
    list of allowed efforts to emit, only the effort the session starts at. So
    the strongest rung the model actually supports is written as the starting
    point and ``/reasoning`` moves it from there. A model whose thinking is
    ``mandatory`` never gets ``reasoning: false``, because that is the one
    value Qwen would honour into a request the provider rejects.
    """

    if model.force_no_thinking:
        # The variant exists to run without thinking. Stating it is a fact
        # about the variant, not a guess.
        return False

    reasons = can_reason(model.reasoning)
    rungs, unknown = clamp_efforts(
        model.reasoning, QWEN_EFFORTS, QWEN_EFFORT_BY_REASONING_EFFORT
    )

    if reasons is None:
        defaulted.record(model_id, "reasoning")
        return None
    if reasons is False:
        return False
    if rungs:
        return {"effort": rungs[-1]}
    if unknown:
        # Known to reason, nothing published about how it is steered.
        defaulted.record(model_id, "reasoning.effort")
    # Known to reason with no caller-side knob: say so, name no effort.
    return {}
