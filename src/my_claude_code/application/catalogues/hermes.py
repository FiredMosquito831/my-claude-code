"""Serialise the neutral catalogue into Hermes Agent's provider shape.

Read out of the published ``hermes-agent`` 0.19.0 wheel rather than out of its
documentation site, because the site describes the wizard and this is the
document the wizard writes. What the wheel accepts, from
``hermes_cli/config.py:4990-5175`` (``_normalize_custom_provider_entry``) and
``hermes_cli/providers.py:620-650`` (``resolve_user_provider``):

* the model root is ``providers: { <name>: Provider }``. The legacy
  ``custom_providers: [...]`` list is still read, but ``providers`` is the keyed
  schema the config version migrated *to*, and an entry under it is what a
  modern picker shows;
* a provider entry needs a base URL in ``api`` (or its ``url``/``base_url``
  aliases) that parses as an absolute URL -- an entry without one is dropped
  whole, with a warning -- and takes ``key_env``, ``transport``,
  ``default_model``, ``models`` and ``extra_headers`` among its known keys.
  **Unknown keys are warned about and ignored** (``:5037-5041``), which is why
  this serialiser writes no ``_mcc_defaulted`` record anywhere: MCC's
  bookkeeping must not become a warning in somebody else's tool, so the card
  and the launcher read the count from ``GET /admin/api/catalogue-models``
  instead (the Kilo and OpenClaw precedent);
* ``key_env`` is an environment-variable **name**, and Hermes calls
  ``os.environ[key_env]`` at request time. The proxy token therefore never
  lands in the file the user owns; ``mcc-hermes`` sets the variable in the
  child process only;
* ``transport`` is ``openai_chat`` | ``anthropic_messages`` | ``codex_responses``
  (``hermes_cli/providers.py:38``). MCC uses ``openai_chat``: it maps to
  ``chat_completions`` (``:401``) and Hermes builds its request URL as
  ``f"{base_url}{'/chat/completions'}"`` (``agent/agent_runtime_helpers.py:1626``),
  so the base URL must carry ``/v1`` -- the same shape the registry declares
  for every other OpenAI-style harness through ``base_url_shape``;
* ``models`` is a mapping of **model id to per-model metadata**, and the only
  per-model key the shipped code reads is ``context_length``
  (``get_custom_provider_context_length``, ``hermes_cli/config.py:5414-5477``,
  called from ``agent/agent_init.py:1765-1775``). So that is the only per-model
  field written here: no ``maxTokens``, no prices, no tool/vision/reasoning
  flags -- writing them would be MCC asserting what Hermes cannot act on;
* ``extra_headers`` is "Per-provider extra HTTP headers" (``:5150-5155``), so
  Hermes is one of the harnesses that can carry MCC's ``x-mcc-harness`` label.

**Unknown stays unknown.** A model whose context window nobody published gets
no ``context_length`` key at all, and the omission is recorded through
:class:`DefaultedFields` so the launcher and the card can say so. Hermes then
falls back to its own detection, which is its documented behaviour; a ``0``
would be a window MCC invented.

Read ``application/catalogues/base.py`` before changing anything here.
"""

from collections.abc import Iterable
from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues.base import (
    DefaultedFields,
    attribution_headers,
    starting_model,
    visible_entries,
)
from my_claude_code.config.harnesses import (
    HERMES_API_KEY_ENV,
    HERMES_BASE_URL_SENTINEL,
    HERMES_PROVIDER_ID,
)

#: The provider id MCC declares inside ``providers``. Hermes addresses a model
#: as ``<provider>/<model>`` in its picker, so the same id is the prefix shown
#: there.
PROVIDER_ID = HERMES_PROVIDER_ID

PROVIDER_DISPLAY_NAME = "My Claude Code"

#: The one transport that reaches MCC's ``POST /v1/chat/completions``.
#: ``anthropic_messages`` would work too -- MCC serves it -- but it would move
#: ``api`` back to the bare proxy root, and every other chat-completions
#: harness in the registry is already on this wire.
PROVIDER_TRANSPORT = "openai_chat"

#: Written verbatim into ``api``. Hermes validates it as an absolute URL before
#: it will keep the provider at all (``hermes_cli/config.py:5055-5075``), and
#: ``config/harness_config_merge.with_base_url`` replaces it with this
#: install's own ``<proxy root>/v1`` on the way to disk.
BASE_URL_SENTINEL = HERMES_BASE_URL_SENTINEL

#: Written verbatim into ``key_env``: a *name*, never a value. The launcher
#: sets the variable in the child process only.
KEY_ENV_REFERENCE = HERMES_API_KEY_ENV

#: The key Hermes falls back to when the user has picked the provider but no
#: model (``hermes_cli/config.py:5103-5106`` maps ``default_model`` onto the
#: provider's ``model``).
DEFAULT_MODEL_KEY = "default_model"

#: The keys a **model entry** is dropped without. Hermes requires none: its
#: normaliser keeps whatever metadata mapping the model carries, including an
#: empty one, and the model id is the mapping key rather than a field. The
#: provider-level requirement -- a base URL Hermes can parse -- is
#: :data:`PROVIDER_REQUIRED_KEYS`, because ``GET /admin/api/catalogue-models``
#: and the schema contract ask about entries, not about the provider.
CLI_REQUIRED_KEYS: frozenset[str] = frozenset()

#: The one key a **provider entry** is dropped without: Hermes parses ``api``
#: (or its ``url``/``base_url`` aliases) as a URL and skips the whole entry --
#: with a warning -- when it does not parse
#: (``hermes_cli/config.py:5055-5075``). Nothing else is required; ``name``
#: defaults to the mapping key.
PROVIDER_REQUIRED_KEYS: frozenset[str] = frozenset({"api"})

#: What Hermes itself does when the one per-model key MCC writes is absent.
#: MCC writes none of these values: it omits the key and records the omission,
#: because a literal here would be MCC asserting a window nobody published.
#: This dict is the one place in this module allowed to hold a limit-shaped
#: key, and it deliberately holds no integer at all.
CLI_DOCUMENTED_DEFAULTS: dict[str, Any] = {
    # context_length absent: Hermes runs its own detection for the model
    # (``agent/model_metadata.py``), which is the honest answer when the
    # resolution ladder has nothing to say.
    "context_length": None,
}


def build_hermes_catalogue(
    models: Iterable[CatalogueModel],
) -> tuple[dict[str, Any], DefaultedFields]:
    """Return Hermes' provider document and the record of what was omitted."""

    defaulted = DefaultedFields()
    visible = visible_entries(models)

    entries: dict[str, dict[str, Any]] = {}
    for model in visible:
        model_id = _model_id(model)
        entries[model_id] = _entry(model, model_id, defaulted)

    provider: dict[str, Any] = {
        "api": BASE_URL_SENTINEL,
        "key_env": KEY_ENV_REFERENCE,
        "transport": PROVIDER_TRANSPORT,
    }
    primary = starting_model(visible)
    if primary is not None:
        provider[DEFAULT_MODEL_KEY] = _model_id(primary)
    provider["models"] = entries
    # ``extra_headers`` is a provider key Hermes documents and merges into
    # every request, so the label MCC's request log reads back is a fact and
    # not a guess. The value is a sentinel the caller resolves per harness.
    provider["extra_headers"] = attribution_headers()

    return {"providers": {PROVIDER_ID: provider}}, defaulted


def _model_id(model: CatalogueModel) -> str:
    """Return the id Hermes routes with: the bare ``provider/model`` ref.

    ``visible_entries`` has already collapsed the no-thinking twin onto its
    plain ref, so the Claude Code only ``claude-3-freecc-no-thinking/`` prefix
    can never reach this picker.
    """

    return model.provider_model_ref


def _entry(
    model: CatalogueModel, model_id: str, defaulted: DefaultedFields
) -> dict[str, Any]:
    """Return one model's metadata: ``context_length``, or nothing at all.

    A window of ``0`` is treated as unknown rather than written: Hermes' own
    normaliser keeps any int above zero and would accept a zero as a literal
    claim that the model holds nothing (``hermes_cli/config.py:5135-5137``),
    and the ladder producing one means nobody stated a real window.
    """

    if model.context_length is None or model.context_length <= 0:
        defaulted.record(model_id, "context_length")
        return {}
    return {"context_length": model.context_length}
