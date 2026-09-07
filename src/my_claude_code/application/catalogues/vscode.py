"""Serialise the neutral catalogue into VS Code's custom chat endpoint shape.

VS Code's Copilot Chat reads custom language-model endpoints from
``chatLanguageModels.json`` in the user's profile directory. The document is a
bare **array** of endpoint objects rather than a keyed map, which makes it the
one shape in this package with no key MCC can own: ownership is instead "the
one element whose ``name`` is MCC's", and the merge engine matches on that.

Fields, as VS Code documents them
(code.visualstudio.com/docs/copilot/customization/language-models):

* ``vendor`` -- ``"customendpoint"`` for a BYO endpoint.
* ``name`` -- the label shown in the model picker. This is also what makes an
  element identifiable, so it is the ownership marker.
* ``url`` -- the endpoint **root**. VS Code appends the method path itself
  according to ``apiType``, so a trailing ``/v1`` would produce ``/v1/v1/...``.
  This is the reason ``BaseUrlShape.ROOT`` exists for an OpenAI-shaped client.
* ``apiType`` -- which wire shape to speak. MCC serves the OpenAI one.
* ``apiKey`` -- accepts VS Code's own ``${input:...}`` reference, which makes
  the editor prompt once and keep the value in its own SecretStorage. MCC
  writes the reference and never a literal.
* ``requestHeaders`` -- an arbitrary header map, so this endpoint carries the
  ``x-mcc-harness`` attribution label like every other harness that has
  somewhere to put it.
* ``models[]`` -- ``{id, name}`` plus optional capability hints.

**Unknown stays unknown.** VS Code requires only ``id`` per model; every
capability field is optional and is therefore *omitted* when the ladder did not
resolve it, rather than filled with a zero. That is the rule in
``application/catalogues/base.py``, and this format is the easy case for it:
there is no required numeric field at all, so :data:`CLI_DOCUMENTED_DEFAULTS`
is empty and MCC never substitutes anything. The omissions are still recorded
through :class:`DefaultedFields`, exactly as ``opencode`` and ``kilo`` record
theirs -- the dashboard asks "which of these numbers came from the provider?",
and for a format that answers by leaving the key out, the list of keys left out
*is* the answer.
"""

from collections.abc import Iterable
from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues.base import (
    DEFAULTED_KEY,
    DefaultedFields,
    attribution_headers,
    visible_entries,
)
from my_claude_code.config.harnesses import VSCODE_BASE_URL_SENTINEL

#: What VS Code calls an endpoint the user supplied themselves.
VENDOR = "customendpoint"

#: The label in VS Code's model picker, and the field ownership is matched on.
#: Changing it would orphan every element MCC has already written -- Undo would
#: no longer recognise its own work -- so it is a constant, not a setting.
ENDPOINT_NAME = "My Claude Code"

#: The wire shape MCC serves and VS Code speaks.
API_TYPE = "openai"

#: VS Code's own secret-reference form. The editor prompts once and stores the
#: answer in SecretStorage, so no token is ever written to this file.
API_KEY_REFERENCE = "${input:mcc_token}"

#: Replaced by the caller before the document reaches disk.
BASE_URL_SENTINEL = VSCODE_BASE_URL_SENTINEL

#: What VS Code refuses a ``models[]`` entry without. Only the identifier:
#: the picker falls back to showing the id when ``name`` is absent, and every
#: capability hint is optional. Recorded here so the contract test can hold
#: this serialiser to it rather than to a rule nobody wrote down.
CLI_REQUIRED_KEYS: frozenset[str] = frozenset({"id"})

#: Empty on purpose: VS Code requires no numeric per-model field, so there is
#: nothing for MCC to substitute and nothing to record. The name is still
#: declared because ``tests/application/test_serialiser_contract.py`` scans for
#: it and its absence would read as an oversight rather than a fact.
CLI_DOCUMENTED_DEFAULTS: dict[str, Any] = {}


def build_vscode_catalogue(
    models: Iterable[CatalogueModel],
) -> tuple[dict[str, Any], DefaultedFields]:
    """Return the one endpoint element MCC owns, wrapped for the merge engine.

    The serialisers in this package all return a *document*; the merge engine
    then lifts MCC's owned subtree out of it. This one is no different -- the
    element lives under a ``chatLanguageModel`` key here and the engine writes
    it into the array as a single element.
    """

    defaulted = DefaultedFields()
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    for model in visible_entries(models):
        if model.gateway_id in seen:
            continue
        seen.add(model.gateway_id)
        entries.append(_entry(model, defaulted))

    document: dict[str, Any] = {
        "chatLanguageModel": {
            "vendor": VENDOR,
            "name": ENDPOINT_NAME,
            "url": BASE_URL_SENTINEL,
            "apiType": API_TYPE,
            "apiKey": API_KEY_REFERENCE,
            "requestHeaders": attribution_headers(),
            "models": entries,
        }
    }
    if defaulted.by_model:
        document[DEFAULTED_KEY] = defaulted.as_document()
    return document, defaulted


def _entry(model: CatalogueModel, defaulted: DefaultedFields) -> dict[str, Any]:
    """Return one model, carrying only what the ladder actually resolved.

    The record of what stayed unknown is kept even though nothing is
    substituted. That is the precedent ``opencode``/``kilo`` already set for a
    format with no required numeric field: the dashboard's question is "which
    of these numbers came from the provider?", and for a format that answers by
    *omission* the honest answer is the list of fields MCC could not state.
    Writing the record is what puts that list on the card.
    """

    entry: dict[str, Any] = {"id": model.gateway_id, "name": model.display_name}
    if model.context_length is not None:
        entry["maxInputTokens"] = model.context_length
    else:
        defaulted.record(model.gateway_id, "maxInputTokens")
    if model.max_output_tokens is not None:
        entry["maxOutputTokens"] = model.max_output_tokens
    else:
        defaulted.record(model.gateway_id, "maxOutputTokens")
    if model.supports_vision is not None:
        entry["vision"] = model.supports_vision
    else:
        defaulted.record(model.gateway_id, "vision")
    if model.supports_tool_calls is not None:
        entry["toolCalling"] = model.supports_tool_calls
    else:
        defaulted.record(model.gateway_id, "toolCalling")
    return entry
