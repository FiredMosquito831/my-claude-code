"""Serialise the neutral catalogue into Aider's two model documents.

Every claim below was read out of the installed tool -- ``aider-chat`` 0.86.2,
with its vendored ``litellm`` -- rather than from a vendor page, because the
published documentation describes the *file names* and almost nothing about
which keys are actually consumed.

**Two files, because Aider reads two.** ``--model-metadata-file`` names a JSON
document and ``--model-settings-file`` a YAML one, and the two carry different
kinds of fact:

* the metadata file is LiteLLM's ``model_cost`` schema -- a **flat** mapping of
  model name to a record of limits and prices. ``Model.get_model_info`` looks
  the model up by the exact string that was passed to ``--model``
  (``models.py`` ``get_model_from_cached_json_db`` -> ``local_model_metadata
  .get(model)``), so the key is the whole ``openai/<gateway id>`` ref, prefix
  included. Aider merges it *over* LiteLLM's own registry rather than replacing
  it, and an exact hit short-circuits the network fetch of LiteLLM's price
  table -- so a generated entry also stops Aider phoning GitHub for a model it
  will never find there.
* the settings file is a **list** of ``ModelSettings`` records, constructed with
  ``ModelSettings(**entry)``. That is a dataclass constructor: an unrecognised
  key raises ``TypeError``, Aider reports it and carries on with the file
  unapplied. So this serialiser emits only fields that exist on the 0.86.2
  dataclass, and -- unlike every other format here -- writes **no**
  ``_mcc_defaulted`` block into it. The record goes in the metadata file, which
  is a plain ``dict.update`` and tolerates anything.

``_mcc_defaulted`` in the metadata file is inert rather than clever: Aider only
ever reads a key it was asked for by name, and its own model listing skips any
entry whose ``mode`` is not ``"chat"``, so the record cannot appear in
``aider --list-models``.

**Base URL and key are not in either file.** Aider reaches MCC through LiteLLM's
OpenAI handler, which reads ``OPENAI_BASE_URL`` (preferred) or
``OPENAI_API_BASE`` and appends ``chat/completions`` to it verbatim -- so the
value is ``<root>/v1``, and ``mcc-aider`` sets it, along with
``OPENAI_API_KEY``, in the launched process only. Nothing MCC writes to disk
carries the proxy token.

**Unknown stays unknown.** Every metadata key except ``litellm_provider`` and
``mode`` is optional, so a ``None`` from the ladder omits the key and the
omission is recorded. What Aider then does instead is written down in
:data:`CLI_DOCUMENTED_DEFAULTS`. Read ``application/catalogues/base.py`` before
changing anything here.
"""

from collections.abc import Iterable
from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues.base import (
    DEFAULTED_KEY,
    DefaultedFields,
    visible_entries,
)

#: LiteLLM's provider prefix for an OpenAI-compatible host. It is the whole
#: mechanism by which Aider reaches MCC: ``--model openai/<id>`` selects
#: LiteLLM's OpenAI handler, which then sends ``<id>`` as the request's model.
LITELLM_PROVIDER = "openai"

#: LiteLLM's own name for "this is a chat model". Aider's model listing filters
#: on it (``attrs.get("mode") != "chat"`` is skipped), so every real entry
#: states it and the ``_mcc_defaulted`` record deliberately does not.
CHAT_MODE = "chat"

#: Prices on ``CatalogueModel`` are USD per **million** tokens, which is what
#: models.dev and the gateways publish. LiteLLM's schema is per **token**.
TOKENS_PER_PRICE_UNIT = 1_000_000

#: LiteLLM's two mandatory keys: without ``litellm_provider`` no lookup is
#: possible, and without ``mode`` ``--list-models`` skips the entry entirely.
#: Every limit and price key is optional, which is why an unknown is omitted.
CLI_REQUIRED_KEYS: frozenset[str] = frozenset({"litellm_provider", "mode"})

#: What Aider itself does when a metadata key is absent, read out of 0.86.2.
#: MCC writes none of these; it omits the key and records the omission. This
#: dict is the one place in this module allowed to hold a literal limit, and
#: the static guard test keys off its name.
CLI_DOCUMENTED_DEFAULTS: dict[str, Any] = {
    # With no ``max_input_tokens`` Aider's token counter reports no context
    # limit for the model, never warns that a chat is approaching one and
    # never suggests ``/drop``. Nothing is sent that would not otherwise be.
    "max_input_tokens": None,
    # With no ``max_output_tokens`` Aider sends no ``max_tokens`` and lets the
    # server choose. MCC's own per-model output budget still applies.
    "max_output_tokens": None,
    # With no cost keys Aider reports ``$0.00`` for the session rather than
    # refusing to run, which is why an unknown price is omitted rather than
    # written as a zero that would read as "free".
    "input_cost_per_token": None,
    "output_cost_per_token": None,
    # LiteLLM's two cached-token rates. Optional exactly like the uncached
    # pair, and read by the same cost reporter, so an unknown is omitted.
    "cache_read_input_token_cost": None,
    "cache_creation_input_token_cost": None,
    # With no ``supports_vision`` Aider refuses to attach an image to the chat
    # and says so. That is the conservative behaviour and the correct one for
    # a model nobody has published an answer for.
    "supports_vision": None,
}


def build_aider_catalogue(
    models: Iterable[CatalogueModel],
) -> tuple[dict[str, Any], DefaultedFields]:
    """Return Aider's model-metadata document and the record of what was omitted."""

    defaulted = DefaultedFields()
    document: dict[str, Any] = {}

    for model in visible_entries(models):
        name = aider_model_name(model)
        if name in document:
            continue
        document[name] = _metadata_entry(model, name, defaulted)

    if defaulted.by_model:
        document[DEFAULTED_KEY] = defaulted.as_document()
    return document, defaulted


def build_aider_model_settings(models: Iterable[CatalogueModel]) -> list[Any]:
    """Return Aider's model-settings list: what the model accepts, not what it is.

    Emitted only where the ladder has something to say. A model whose reasoning
    control and pinned parameters are both unknown produces no record at all,
    because ``ModelSettings`` has no "unknown" for its booleans -- writing one
    would state a default as a fact.
    """

    entries: list[Any] = []
    seen: set[str] = set()
    for model in visible_entries(models):
        name = aider_model_name(model)
        if name in seen:
            continue
        seen.add(name)
        entry = _settings_entry(model, name)
        if entry is not None:
            entries.append(entry)
    return entries


def aider_model_name(model: CatalogueModel) -> str:
    """Return the string a user passes to ``--model``, and the metadata key.

    The gateway id verbatim, behind LiteLLM's ``openai/`` prefix. The
    no-thinking variant keeps its full prefixed id because that prefix is the
    entire mechanism that turns thinking off.
    """

    return f"{LITELLM_PROVIDER}/{model.gateway_id}"


def _metadata_entry(
    model: CatalogueModel, name: str, defaulted: DefaultedFields
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "litellm_provider": LITELLM_PROVIDER,
        "mode": CHAT_MODE,
    }

    if model.context_length is None:
        defaulted.record(name, "max_input_tokens")
    else:
        entry["max_input_tokens"] = model.context_length

    if model.max_output_tokens is None:
        defaulted.record(name, "max_output_tokens")
    else:
        # ``max_tokens`` is LiteLLM's older spelling of the same ceiling and
        # several of its call sites still read it, so both are written from
        # the one figure rather than one being left to a stale registry row.
        entry["max_output_tokens"] = model.max_output_tokens
        entry["max_tokens"] = model.max_output_tokens

    _record_price(entry, "input_cost_per_token", model.input_price, name, defaulted)
    _record_price(entry, "output_cost_per_token", model.output_price, name, defaulted)
    _record_price(
        entry, "cache_read_input_token_cost", model.cache_read_price, name, defaulted
    )
    _record_price(
        entry,
        "cache_creation_input_token_cost",
        model.cache_write_price,
        name,
        defaulted,
    )

    if model.supports_vision is None:
        defaulted.record(name, "supports_vision")
    else:
        entry["supports_vision"] = model.supports_vision

    # Not read by Aider itself, but part of the LiteLLM schema this file is
    # written in, and honest when the gateway published its parameter list.
    if model.supports_tool_calls is not None:
        entry["supports_function_calling"] = model.supports_tool_calls

    return entry


def _record_price(
    entry: dict[str, Any],
    key: str,
    price: float | None,
    name: str,
    defaulted: DefaultedFields,
) -> None:
    if price is None:
        defaulted.record(name, key)
        return
    entry[key] = price / TOKENS_PER_PRICE_UNIT


def _settings_entry(model: CatalogueModel, name: str) -> dict[str, Any] | None:
    """Return one ``ModelSettings`` record, or None when nothing is known.

    ``accepts_settings`` is the list Aider consults before it will honour
    ``--reasoning-effort`` or ``--thinking-tokens`` for a model, so it is
    written from the ladder's own control flags rather than guessed from an
    id. ``use_temperature`` is written ``False`` only when the provider is
    known to pin ``temperature`` and reject an override -- sending one would
    be a 400 on every request.
    """

    accepts: list[str] = []
    reasoning = model.reasoning
    if reasoning is not None and not model.force_no_thinking:
        if reasoning.supports_effort_control is True:
            accepts.append("reasoning_effort")
        if reasoning.supports_budget_control is True:
            accepts.append("thinking_tokens")

    pins_temperature = model.default_parameters is not None and any(
        key == "temperature" for key, _ in model.default_parameters
    )

    if not accepts and not pins_temperature:
        return None

    entry: dict[str, Any] = {"name": name}
    if accepts:
        entry["accepts_settings"] = accepts
    if pins_temperature:
        entry["use_temperature"] = False
    return entry
