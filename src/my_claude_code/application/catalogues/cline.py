"""Serialise the neutral catalogue into Cline's ``providers.json`` shape.

Every claim below was read out of the installed CLI -- Cline 3.0.61,
``@cline/core`` and ``@cline/llms`` -- and then confirmed on the wire against
this proxy, because the vendor's prose describes a ``cline auth`` command and
not the document it writes.

**The document.** ``providers.json`` is
``{version, lastUsedProvider, modes, providers: {<id>: {settings, updatedAt,
tokenSource}}}``. The settings object is validated by one shared schema whose
fields are ``provider``, ``apiKey``, ``model``, ``baseUrl``, ``maxTokens``,
``contextWindow``, ``headers``, ``protocol``, ``client``, ``reasoning``,
``timeout`` and a handful of cloud-vendor blocks. Note what is *not* there:
there is no per-model array. Cline's own per-model facts live in a bundled
static catalogue keyed by its own provider ids, and a BYOK provider carries the
numbers for **the one model it is configured with**.

**Cline validates the whole document and discards it on any surprise.**
Measured on 3.0.61: a single unrecognised *root* key -- ``_mcc_models``, or
even ``_mcc_defaulted`` on its own -- makes Cline drop the provider settings it
just read and rewrite the file with ``{"provider": ..., "model": "gpt-4o"}``,
losing the base URL and the key with it. So the document that reaches disk is
strictly the shape Cline writes for itself, and MCC's own bookkeeping is
stripped by ``config/harness_cline.strip_mcc_keys`` on the way there. It exists
up to that point because the launcher needs it: the blocks are how the model
named with ``-m`` gets its own resolved limits into the provider entry.

That shapes this serialiser. It writes one provider entry --
:data:`~my_claude_code.config.harnesses.CLINE_PROVIDER_ID`, which is
``openai-compatible``, the entry Cline documents as "OpenAI-compatible chat
completions endpoint" and the only one that both takes an arbitrary
``baseUrl`` and issues no model-discovery call -- and fills ``contextWindow``
and ``maxTokens`` from the ladder for the model that entry names. Every other
routable model is written into an inert ``_mcc_models`` block, and
``config/harness_cline.py`` promotes whichever one the user asked for with
``-m`` into ``settings`` before the file reaches disk. Measured: with the
promoted numbers in place, Cline's own run result echoed
``info: {contextWindow: 131072, maxInputTokens: 131072}``.

**Base URL shape.** ``baseUrl`` goes to ``@ai-sdk/openai-compatible``, which
appends ``chat/completions`` and nothing else -- so it is ``<root>/v1``.
Cline's own default for this provider is ``https://api.openai.com/v1``, which
is the same shape. A root without ``/v1`` would produce
``POST /chat/completions``, which this proxy does not serve.

**The key is literal, and that is a measured decision.** Cline resolves a
missing ``apiKey`` from ``process.env.OPENAI_API_KEY``. Measured on 3.0.61:
with ``apiKey`` absent and the variable set, the run did not authenticate and
did not terminate. With the key in the document, the same run answered in
885 ms. So MCC writes it -- into ``~/.fcc/cline/data/settings/providers.json``,
a file MCC owns and narrows to ``0600``, never into ``~/.cline``.

**Unknown stays unknown.** ``contextWindow`` and ``maxTokens`` are optional
and are positive integers when present, so a ``None`` from the ladder omits
the key and the omission is recorded. What Cline then does instead is written
down in :data:`CLI_DOCUMENTED_DEFAULTS`. Read
``application/catalogues/base.py`` before changing anything here.
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
    CLINE_API_KEY_SENTINEL,
    CLINE_BASE_URL_SENTINEL,
    CLINE_PROVIDER_ID,
)

#: The provider key MCC owns inside the document.
PROVIDER_ID = CLINE_PROVIDER_ID

#: ``providers.json`` states its own schema version. Cline 3.0.61 writes 1.
DOCUMENT_VERSION = 1

#: Cline's provider entry is ``{settings, updatedAt, tokenSource}`` and
#: ``updatedAt`` is **required**: measured on 3.0.61, an entry without it had
#: its whole ``settings`` object discarded and rewritten as
#: ``{"provider": ..., "model": "gpt-4o"}`` -- base URL and key gone, and the
#: next run reached ``api.openai.com``. A serialiser is a pure function of the
#: model records, so it cannot write "now" without making the document differ
#: from itself on every call and defeating the content-compare skip. It writes
#: the epoch instead, which Cline replaces with a real timestamp the first time
#: it saves the file and never otherwise acts on: there is one provider entry
#: here, so nothing is ordered by it.
UPDATED_AT = "1970-01-01T00:00:00.000Z"

#: How Cline records where a key came from. ``manual`` is what its own
#: ``cline auth --apikey`` writes, and it is the truthful value here: the key
#: was configured rather than obtained through an OAuth flow.
TOKEN_SOURCE = "manual"

#: The inert block carrying every routable model's resolved numbers. Cline
#: ignores an unknown root key, and ``config/harness_cline.py`` reads this to
#: promote the model a user named with ``-m`` into ``settings``.
MODELS_KEY = "_mcc_models"

#: What Cline itself does when one of these keys is absent, read out of
#: 3.0.61. MCC writes none of these; it omits the key and records the
#: omission. This dict is the one place in this module allowed to hold a
#: literal limit, and the static guard test keys off its name.
CLI_DOCUMENTED_DEFAULTS: dict[str, Any] = {
    # With no ``contextWindow`` Cline falls back to its bundled catalogue for
    # the provider, finds no row for an MCC-routed id, and runs with no
    # context gauge and no early compaction. The request itself is unchanged.
    "contextWindow": None,
    # With no ``maxTokens`` Cline sends no output ceiling and lets the server
    # choose. MCC's own per-model output budget still applies.
    "maxTokens": None,
}


def build_cline_catalogue(
    models: Iterable[CatalogueModel],
) -> tuple[dict[str, Any], DefaultedFields]:
    """Return Cline's ``providers.json`` and the record of what was omitted."""

    defaulted = DefaultedFields()
    entries: dict[str, Any] = {}

    for model in visible_entries(models):
        model_id = model.gateway_id
        if model_id in entries:
            continue
        entries[model_id] = _model_record(model, model_id, defaulted)

    settings: dict[str, Any] = {
        "provider": PROVIDER_ID,
        "apiKey": CLINE_API_KEY_SENTINEL,
        "baseUrl": CLINE_BASE_URL_SENTINEL,
    }
    # The first routable model is the session default, and it is the first for
    # a reason a user can predict: this list is the same order, and the same
    # visibility filter, that ``GET /v1/models`` publishes -- their own chain
    # order. ``mcc-cline -m <id>`` replaces it, which is what the dashboard
    # card documents.
    if entries:
        first_id = next(iter(entries))
        settings["model"] = first_id
        settings.update(entries[first_id])

    document: dict[str, Any] = {
        "version": DOCUMENT_VERSION,
        "lastUsedProvider": PROVIDER_ID,
        "modes": {},
        # No models means no provider: an entry with no ``model`` would tell
        # Cline to run this provider on its own bundled default, which is a
        # model MCC does not route. It is also what makes "did I get any
        # models?" answerable from the document, since ``MODEL_ENTRY_PATHS``
        # counts provider entries here rather than a per-model array Cline's
        # schema does not have.
        "providers": (
            {
                PROVIDER_ID: {
                    "settings": settings,
                    "updatedAt": UPDATED_AT,
                    "tokenSource": TOKEN_SOURCE,
                }
            }
            if entries
            else {}
        ),
        MODELS_KEY: entries,
    }
    if defaulted.by_model:
        document[DEFAULTED_KEY] = defaulted.as_document()
    return document, defaulted


def _model_record(
    model: CatalogueModel, model_id: str, defaulted: DefaultedFields
) -> dict[str, Any]:
    """Return the settings keys Cline understands for one model."""

    record: dict[str, Any] = {}

    if model.context_length is None:
        defaulted.record(model_id, "contextWindow")
    else:
        record["contextWindow"] = model.context_length

    if model.max_output_tokens is None:
        defaulted.record(model_id, "maxTokens")
    else:
        record["maxTokens"] = model.max_output_tokens

    return record
