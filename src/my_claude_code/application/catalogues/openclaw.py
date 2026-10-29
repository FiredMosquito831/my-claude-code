"""Serialise the neutral catalogue into OpenClaw's provider shape.

Read out of the config schema OpenClaw 2026.10.8 generates for itself
(``openclaw config schema``) rather than from prose, because the prose describes
the wizard and not the document it writes. What that schema accepts, verbatim:

* the model root is ``models: { mode, providers: { <id>: Provider } }``, and
  **both** levels declare ``additionalProperties: false`` -- one stray key and
  the provider is rejected, which is why this serialiser writes no
  ``_mcc_defaulted`` record anywhere in the document and the card reads the
  count from ``GET /admin/api/catalogue-models`` instead (the Kilo precedent);
* a provider entry carries ``baseUrl``, ``apiKey``, ``api``, ``headers`` and
  ``models[]`` among others -- ``api`` is an enum and MCC uses
  ``openai-completions``, the one value that reaches MCC's
  ``POST /v1/chat/completions``;
* ``apiKey`` accepts a ``$VAR`` / ``${VAR}`` reference, so the proxy token
  never lands in a file the user owns. The launcher sets the variable in the
  child process only;
* a model entry **requires** ``id`` and ``name``; ``contextWindow``,
  ``maxTokens``, ``input``, ``reasoning``, ``cost`` and ``compat`` are all
  optional, and every one of them is omitted here when the ladder does not
  know it. A ``0`` window would be MCC inventing a limit nobody published;
* ``cost`` takes ``input``/``output``/``cacheRead``/``cacheWrite`` in **USD per
  million tokens**, which is exactly the unit MCC's ``*_price`` fields are
  stored in (models.dev's), so no conversion happens here. A price MCC does
  not know is omitted -- never a zero, which would read as "free";
* ``compat.supportedReasoningEfforts`` is the provider's own effort
  vocabulary, and MCC's ``ReasoningEffort`` values are already the OpenAI
  spellings that ``api: openai-completions`` sends as ``reasoning_effort``.

**The one value outside MCC's block.** OpenClaw's default model is
``agents.defaults.model.primary``. This serialiser states it at the same
document root so the caller can write it in the same pass, and the Configure
engine records what it replaced -- Undo puts the user's value back. The block
itself stays the only subtree MCC owns.

Read ``application/catalogues/base.py`` before changing anything here.
"""

from collections.abc import Iterable
from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues.base import (
    DefaultedFields,
    attribution_headers,
    can_reason,
    clamp_efforts,
    starting_model,
    visible_entries,
)
from my_claude_code.config.harnesses import (
    OPENCLAW_API_KEY_ENV,
    OPENCLAW_BASE_URL_SENTINEL,
)
from my_claude_code.core.reasoning import ReasoningEffort

#: The provider id MCC declares inside ``models.providers``. OpenClaw addresses
#: a model as ``<provider>/<model>``, so the same id is the prefix of every
#: ref the picker shows.
PROVIDER_ID = "mcc"

PROVIDER_DISPLAY_NAME = "My Claude Code"

#: The one ``api`` value that reaches MCC's ``POST /v1/chat/completions``.
#: ``anthropic-messages`` would work too, but MCC already serves the OpenAI
#: chat surface for every other harness and the model ids are the same.
PROVIDER_API = "openai-completions"

#: Written verbatim into ``apiKey``. OpenClaw expands ``$VAR`` and ``${VAR}``
#: from the process environment; the launcher sets this variable in the child
#: process only.
API_KEY_REFERENCE = f"${OPENCLAW_API_KEY_ENV}"

#: ``baseUrl`` gets the real proxy root, written by
#: ``config.harness_config_merge.with_base_url`` on the way to disk: it is a
#: loopback address on the user's own machine, not a credential, and the
#: serialiser cannot know which port this install runs on.
BASE_URL_SENTINEL = OPENCLAW_BASE_URL_SENTINEL

#: The key OpenClaw keeps the default model under. Shared with the registry so
#: the merge layer records exactly the key this document writes.
PRIMARY_MODEL_PATH: tuple[str, ...] = ("agents", "defaults", "model", "primary")

#: OpenClaw's own effort vocabulary for the OpenAI chat wire: the six rungs
#: MCC's ``ReasoningEffort`` already speaks. MCC intersects, never extends.
OPENCLAW_EFFORTS: tuple[str, ...] = (
    ReasoningEffort.MINIMAL.value,
    ReasoningEffort.LOW.value,
    ReasoningEffort.MEDIUM.value,
    ReasoningEffort.HIGH.value,
    ReasoningEffort.XHIGH.value,
    ReasoningEffort.MAX.value,
)

#: MCC effort -> the rung this wire spells the same way. Identity, stated so
#: ``clamp_efforts`` has one mapping to read and a future divergence has one
#: place to be declared.
OPENCLAW_EFFORT_BY_REASONING_EFFORT: dict[ReasoningEffort, str] = {
    effort: effort.value for effort in ReasoningEffort
}

#: The two keys OpenClaw refuses a model entry without. Read out of the
#: generated schema: ``"required": ["id", "name"]`` on the model entry, and no
#: required array at all on the provider entry.
CLI_REQUIRED_KEYS: frozenset[str] = frozenset({"id", "name"})

#: What OpenClaw itself does when a per-model key is absent. MCC writes none of
#: these: it omits the key and records the omission, because writing a literal
#: here would be MCC asserting a limit or a price nobody published. This dict
#: is the one place in this module allowed to hold a limit-shaped key, and it
#: deliberately holds no integer at all.
CLI_DOCUMENTED_DEFAULTS: dict[str, Any] = {
    # contextWindow absent: OpenClaw leaves the window unset and the provider's
    # native window applies.
    "contextWindow": None,
    # maxTokens absent: the field is left off the request entirely.
    "maxTokens": None,
    # input absent: the model is treated as text-only.
    "input": ["text"],
    # reasoning absent: the schema's own default is false, which is a claim
    # MCC must not make on a model's behalf.
    "reasoning": False,
    "cost": None,
}


def build_openclaw_catalogue(
    models: Iterable[CatalogueModel],
) -> tuple[dict[str, Any], DefaultedFields]:
    """Return OpenClaw's model document and the record of what was omitted."""

    defaulted = DefaultedFields()
    entries: list[dict[str, Any]] = []
    visible = visible_entries(models)
    for model in visible:
        model_id = _model_id(model)
        entries.append(_entry(model, model_id, defaulted))

    document: dict[str, Any] = {
        "models": {
            "providers": {
                PROVIDER_ID: {
                    "baseUrl": BASE_URL_SENTINEL,
                    "apiKey": API_KEY_REFERENCE,
                    "api": PROVIDER_API,
                    # A static map: OpenClaw merges provider headers into every
                    # request, and this one is a label rather than a
                    # credential -- see
                    # ``application/catalogues/base.attribution_headers``.
                    "headers": attribution_headers(),
                    "models": entries,
                }
            }
        }
    }
    primary = _primary_model(visible)
    if primary is not None:
        document["agents"] = {"defaults": {"model": {"primary": _model_id(primary)}}}
    return document, defaulted


def _primary_model(visible: list[CatalogueModel]) -> CatalogueModel | None:
    """Return the model OpenClaw should open on, if there is one."""

    return starting_model(visible)


def _model_id(model: CatalogueModel) -> str:
    """Return the id OpenClaw routes with.

    The bare ``provider/model`` ref for every model a picker lists; the
    no-thinking twin keeps its gateway id, because that prefix is the whole
    mechanism and no other harness spelling of it exists here.
    """

    return model.gateway_id if model.force_no_thinking else model.provider_model_ref


def _entry(
    model: CatalogueModel, model_id: str, defaulted: DefaultedFields
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": model_id,
        "name": model.display_name or model_id,
    }

    if model.context_length is None:
        defaulted.record(model_id, "contextWindow")
    else:
        entry["contextWindow"] = model.context_length

    if model.max_output_tokens is None:
        defaulted.record(model_id, "maxTokens")
    else:
        entry["maxTokens"] = model.max_output_tokens

    _apply_input(model, model_id, entry, defaulted)
    _apply_reasoning(model, model_id, entry, defaulted)

    cost = _cost(model, model_id, defaulted)
    if cost is not None:
        entry["cost"] = cost

    if model.supports_tool_calls is False:
        # Only the negative is a statement MCC can make: ``None`` and ``True``
        # both leave the gateway to decide, and OpenClaw's own default already
        # allows tools. Merged rather than assigned, or the effort list above
        # would go with it.
        _compat(entry)["supportsTools"] = False

    return entry


def _compat(entry: dict[str, Any]) -> dict[str, Any]:
    """Return ``entry``'s ``compat`` map, creating it when it is absent."""

    existing = entry.get("compat")
    if isinstance(existing, dict):
        return existing
    created: dict[str, Any] = {}
    entry["compat"] = created
    return created


def _apply_input(
    model: CatalogueModel,
    model_id: str,
    entry: dict[str, Any],
    defaulted: DefaultedFields,
) -> None:
    """Write the modality list, or record that nobody published one.

    ``input`` accepts ``text``/``image``/``video``/``audio``. MCC has no
    capability bit for the last two, so a model that accepts them and nothing
    else would be described as text-only -- which is correct for every model
    MCC routes today and stated here so a future addition is a deliberate one.
    """

    if model.supports_vision is None:
        defaulted.record(model_id, "input")
        return
    entry["input"] = ["text", "image"] if model.supports_vision else ["text"]


def _apply_reasoning(
    model: CatalogueModel,
    model_id: str,
    entry: dict[str, Any],
    defaulted: DefaultedFields,
) -> None:
    """Write ``reasoning`` and the effort list the provider published."""

    reasons = can_reason(model.reasoning)
    if reasons is None:
        # The schema's own default is ``false``, which is a claim about the
        # model rather than a fact about it. Leave the key out and say so.
        defaulted.record(model_id, "reasoning")
        return
    entry["reasoning"] = reasons
    if not reasons:
        return
    rungs, unknown = clamp_efforts(
        model.reasoning, OPENCLAW_EFFORTS, OPENCLAW_EFFORT_BY_REASONING_EFFORT
    )
    if rungs:
        _compat(entry)["supportedReasoningEfforts"] = rungs
    elif unknown:
        # Known to reason, nothing published about how it is steered: no list,
        # and the record says why it is missing.
        defaulted.record(model_id, "supportedReasoningEfforts")


def _cost(
    model: CatalogueModel, model_id: str, defaulted: DefaultedFields
) -> dict[str, float] | None:
    """Return OpenClaw's cost block, or None when nobody published prices.

    Units are USD per million tokens on both sides (MCC stores models.dev's
    rates), so the numbers go across unchanged. None of the four is ever
    derived from another and none is ever written as ``0``.
    """

    cost: dict[str, float] = {}
    for key, value in (
        ("input", model.input_price),
        ("output", model.output_price),
        ("cacheRead", model.cache_read_price),
        ("cacheWrite", model.cache_write_price),
    ):
        if value is None:
            defaulted.record(model_id, f"cost.{key}")
        else:
            cost[key] = value
    return cost or None
