"""Serialise the neutral catalogue into a Goose custom-provider document.

Goose (Block) reads a custom provider from its own JSON file in a
``custom_providers/`` directory beside ``config.yaml`` -- one file per
provider, wholly owned by whoever wrote it. That is the reason this harness is
the least invasive in the desktop set: MCC writes a file of its own and touches
the user's ``config.yaml`` for exactly one scalar, ``GOOSE_PROVIDER``.

Two behaviours that shape everything below, both of them Goose's:

* **Keys placed in Goose's config are ignored.** Goose reads credentials from
  its keyring, or from ``secrets.yaml`` when ``GOOSE_DISABLE_KEYRING`` is set.
  A token written into either this document or ``config.yaml`` would silently
  not work, which is worse than not working loudly -- so MCC writes none at
  all, and the card tells the user where the key goes.
* **The base URL is split.** Goose keeps the host in one field and the request
  path in another rather than taking one URL. That is why
  ``BaseUrlShape.SPLIT_HOST_PATH`` exists: it is not a special case in the
  writer, it is a fact about this app declared as data.

**No attribution header.** Goose publishes no per-provider request-header
field, so ``goose`` stays in ``HARNESSES_WITHOUT_ATTRIBUTION_HEADER`` and is
identified by user-agent instead. The contract test that asserts no header
reaches a generated document for those harnesses covers this one too --
:func:`build_goose_catalogue` deliberately never calls ``attribution_headers``.

**Unknown stays unknown.** Every capability field here is optional to Goose, so
an unresolved value is omitted rather than defaulted and
:data:`CLI_DOCUMENTED_DEFAULTS` is empty. The omissions are still recorded, the
way ``opencode`` and ``kilo`` record theirs, so the dashboard can say which
numbers Goose was told and which it was left to guess.
"""

from collections.abc import Iterable
from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues.base import (
    DEFAULTED_KEY,
    DefaultedFields,
    visible_entries,
)
from my_claude_code.config.harnesses import (
    GOOSE_BASE_PATH_VALUE,
    GOOSE_BASE_URL_SENTINEL,
)

#: The provider id MCC claims, matching every other harness catalogue.
PROVIDER_ID = "mcc"

PROVIDER_DISPLAY_NAME = "My Claude Code"

#: Replaced by the caller before the document reaches disk. This one is the
#: proxy **root**: Goose appends the path below itself.
BASE_URL_SENTINEL = GOOSE_BASE_URL_SENTINEL

#: The other half of the split. Goose's OpenAI-compatible provider posts to
#: ``<host>/<base_path>``, so MCC names ``v1/chat/completions`` explicitly
#: rather than relying on a default that differs between Goose releases.
BASE_PATH = GOOSE_BASE_PATH_VALUE

#: What Goose refuses a custom-provider model entry without. The id is the
#: only one; ``name`` and every limit are optional and Goose fills its own.
CLI_REQUIRED_KEYS: frozenset[str] = frozenset({"id"})

#: Empty on purpose -- see the module docstring. Declared so the contract test
#: that scans for the name finds a fact rather than an omission.
CLI_DOCUMENTED_DEFAULTS: dict[str, Any] = {}


def build_goose_catalogue(
    models: Iterable[CatalogueModel],
) -> tuple[dict[str, Any], DefaultedFields]:
    """Return the custom-provider document MCC owns whole, and what was guessed."""

    defaulted = DefaultedFields()
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    for model in visible_entries(models):
        if model.gateway_id in seen:
            continue
        seen.add(model.gateway_id)
        entries.append(_entry(model, defaulted))

    document: dict[str, Any] = {
        "custom_provider": {
            "name": PROVIDER_ID,
            "display_name": PROVIDER_DISPLAY_NAME,
            "api_url": BASE_URL_SENTINEL,
            "base_path": BASE_PATH,
            # Named, not discovered: Goose's discovery would GET a models
            # route MCC serves under a different prefix, and an explicit list
            # is the only form that shows the mcc/* tiers in its picker.
            "models": [entry["id"] for entry in entries],
            "model_details": entries,
            # Goose reads the key from its keyring under this name. The value
            # is never written anywhere by MCC.
            "api_key_env": "MCC_AUTH_TOKEN",
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
        entry["context_limit"] = model.context_length
    else:
        defaulted.record(model.gateway_id, "context_limit")
    if model.max_output_tokens is not None:
        entry["output_limit"] = model.max_output_tokens
    else:
        defaulted.record(model.gateway_id, "output_limit")
    if model.supports_vision is not None:
        entry["supports_vision"] = model.supports_vision
    else:
        defaulted.record(model.gateway_id, "supports_vision")
    if model.supports_tool_calls is not None:
        entry["supports_tools"] = model.supports_tool_calls
    else:
        defaulted.record(model.gateway_id, "supports_tools")
    return entry
