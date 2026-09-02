"""Serialise the neutral catalogue into Droid's ``customModels`` shape.

Every claim below was read out of the installed CLI -- Factory Droid 0.210.0 --
and then confirmed on the wire against this proxy.

**Droid is the one harness in this release that does not speak Chat
Completions.** The brief expected ``provider: "generic-chat-completion-api"``,
because Droid's BYOK page leads with it and because the other three CLIs in
this batch have no Anthropic mode at all. Measured, that is the wrong choice
here: ``provider: "anthropic"`` accepts an arbitrary ``baseUrl``, instantiates
the bundled ``@anthropic-ai/sdk`` against it and reaches
``POST <baseUrl>/v1/messages`` -- MCC's own native protocol, with no
translation layer between the agent and the router. Verified end to end: the
request log row read ``endpoint=/v1/messages protocol=anthropic status=success``.
``generic-chat-completion-api`` would work too, through the ``openai`` npm SDK
and therefore a ``<root>/v1`` base URL; it is simply the longer way round.

**Where the document lives.** ``droid --settings <path>`` is a *runtime
settings overlay*, merged for that process only into the same hierarchy as
``~/.factory/settings.json``. Measured on a machine with no ``~/.factory`` at
all, a file containing nothing but ``customModels`` was enough for the model to
appear and dispatch. So MCC owns ``~/.fcc/droid-settings.json`` outright and
never reads, merges into or backs up the user's own settings.

**Base URL shape.** ``baseUrl`` goes to the Anthropic SDK, which appends
``/v1/messages`` itself, so it is the proxy **root** with no ``/v1``. A
trailing ``/v1`` would produce ``POST /v1/v1/messages``.

**The proxy token is not in that file.** Droid's own documented secret
reference is ``${VAR}``, expanded by ``expandSettingsEnvVarRefs`` from the
process environment. MCC writes the reference and ``mcc-droid`` sets the value
in the child process only. ``authMode: "bearer"`` switches the Anthropic SDK
from ``x-api-key`` to ``Authorization: Bearer``; both are accepted by
``api/dependencies.py``, and Bearer is what MCC states everywhere else.

**Unknown stays unknown.** Every per-model field except ``model``,
``provider`` and ``baseUrl`` is optional, so a ``None`` from the ladder omits
the key and the omission is recorded. What Droid then does instead is written
down in :data:`CLI_DOCUMENTED_DEFAULTS`. Read
``application/catalogues/base.py`` before changing anything here.
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
from my_claude_code.config.harnesses import (
    DROID_API_KEY_REFERENCE,
    DROID_BASE_URL_SENTINEL,
)

#: The key MCC's overlay owns. Droid merges the overlay into the settings
#: hierarchy, so writing only this leaves every other setting the user has.
CUSTOM_MODELS_KEY = "customModels"

#: The provider type MCC declares. See the module docstring for why it is not
#: ``generic-chat-completion-api``.
PROVIDER = "anthropic"

#: Sends the key as ``Authorization: Bearer`` rather than the Anthropic SDK's
#: default ``x-api-key``. Both reach this proxy; one of them is the header
#: every other page of MCC's documentation names.
AUTH_MODE = "bearer"

#: The prefix Droid puts in front of a custom model's id everywhere a user
#: types one -- ``droid exec --model custom:<model>``. It is part of the id in
#: the CLI, and *not* part of the ``model`` field in the document.
MODEL_ID_PREFIX = "custom:"

#: Without all three the model either never appears in Droid's picker or
#: never dispatches. Every capability key beside them is optional.
CLI_REQUIRED_KEYS: frozenset[str] = frozenset({"model", "provider", "baseUrl"})

#: What Droid itself does when one of these keys is absent, read out of
#: 0.210.0. MCC writes none of these; it omits the key and records the
#: omission. This dict is the one place in this module allowed to hold a
#: literal limit, and the static guard test keys off its name.
CLI_DOCUMENTED_DEFAULTS: dict[str, Any] = {
    # With no ``maxContextLimit`` Droid uses its own built-in default for an
    # unknown custom model and never compacts early on the real ceiling.
    "maxContextLimit": None,
    # With no ``maxOutputTokens`` Droid sends no output ceiling and lets the
    # server choose. MCC's own per-model output budget still applies.
    "maxOutputTokens": None,
    # With no ``enableThinking`` Droid sends no thinking block, which is the
    # correct request for a model nobody has published a reasoning answer for.
    "enableThinking": None,
    # ``noImageSupport`` is an opt-*out*: absent means Droid will offer to
    # attach an image. It is written only when vision is known absent, so an
    # unknown is recorded rather than turned into a claim either way.
    "noImageSupport": None,
}


def build_droid_catalogue(
    models: Iterable[CatalogueModel],
) -> tuple[dict[str, Any], DefaultedFields]:
    """Return Droid's settings overlay and the record of what was omitted."""

    defaulted = DefaultedFields()
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    for model in visible_entries(models):
        model_id = model.gateway_id
        if model_id in seen:
            continue
        seen.add(model_id)
        entries.append(_entry(model, model_id, defaulted))

    document: dict[str, Any] = {CUSTOM_MODELS_KEY: entries}
    if defaulted.by_model:
        document[DEFAULTED_KEY] = defaulted.as_document()
    return document, defaulted


def droid_model_id(model: CatalogueModel) -> str:
    """Return the id a user types after ``--model``."""

    return f"{MODEL_ID_PREFIX}{model.gateway_id}"


def _entry(
    model: CatalogueModel, model_id: str, defaulted: DefaultedFields
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "model": model_id,
        "displayName": model.display_name,
        "provider": PROVIDER,
        "baseUrl": DROID_BASE_URL_SENTINEL,
        "apiKey": DROID_API_KEY_REFERENCE,
        "authMode": AUTH_MODE,
    }

    if model.context_length is None:
        defaulted.record(model_id, "maxContextLimit")
    else:
        entry["maxContextLimit"] = model.context_length

    if model.max_output_tokens is None:
        defaulted.record(model_id, "maxOutputTokens")
    else:
        entry["maxOutputTokens"] = model.max_output_tokens

    thinking = _enable_thinking(model, model_id, defaulted)
    if thinking is not None:
        entry["enableThinking"] = thinking

    if model.supports_vision is None:
        defaulted.record(model_id, "noImageSupport")
    elif model.supports_vision is False:
        entry["noImageSupport"] = True

    return entry


def _enable_thinking(
    model: CatalogueModel, model_id: str, defaulted: DefaultedFields
) -> bool | None:
    """Return Droid's thinking switch, or None when the ladder cannot say.

    A model whose thinking is ``mandatory`` never gets ``False``: that is the
    one value Droid would honour into a request the provider rejects.
    """

    if model.force_no_thinking:
        # The variant exists to run without thinking. Stating it is a fact
        # about the variant, not a guess.
        return False

    reasons = can_reason(model.reasoning)
    if reasons is None:
        defaulted.record(model_id, "enableThinking")
        return None
    if reasons is False and model.reasoning is not None and model.reasoning.mandatory:
        defaulted.record(model_id, "enableThinking")
        return None
    return reasons
