"""Serialise the neutral catalogue into Kimi Code's ``config.toml`` shape.

Every field below was read out of the installed package's own source -- Kimi
Code CLI 1.50.0, ``kimi_cli/config.py`` (``LLMProvider``, ``LLMModel``,
``Config``), ``kimi_cli/llm.py`` (``ProviderType``, ``ModelCapability``,
``create_llm``, ``compute_max_completion_tokens``) and ``kimi_cli/share.py``
-- rather than from prose, because Moonshot's published docs describe an older
layout. Four of the assumptions the docs would have given are wrong, and each
is stated here so nobody re-derives them:

* The config directory is ``~/.kimi``, overridden by ``KIMI_SHARE_DIR``.
  There is no ``KIMI_CODE_HOME``: the string does not occur in the package.
  ``~/.kimi-code`` is an older layout, and a machine can still have one.
* ``LLMModel`` declares ``provider``, ``model``, ``max_context_size``,
  ``capabilities`` and ``display_name``. **There is no ``max_output_size``.**
  Kimi derives the completion cap itself, in ``compute_max_completion_tokens``,
  from ``max_context_size`` less the tokens already in the request. So the
  ladder's ``max_output_tokens`` has nowhere to go and is not recorded as
  defaulted either -- an absent field is not a field Kimi guessed.
* The capability vocabulary is exactly ``image_in``, ``video_in``,
  ``thinking``, ``always_thinking`` (``ModelCapability``). There is no tools
  capability -- Kimi assumes tool calling of every provider -- and no
  per-model reasoning-effort field anywhere in the schema, so MCC's effort
  rungs have no counterpart to be clamped into. :func:`clamp_efforts` is
  therefore not called from this module, which is a statement about Kimi's
  schema and not an oversight.
* ``max_context_size`` is a *required* ``int``. Its unknown value is not an
  omission but ``0``: ``compute_max_completion_tokens`` branches on
  ``max_context_size <= 0`` and falls back to
  ``DEFAULT_UNKNOWN_CONTEXT_COMPLETION_TOKENS`` (32000). That is Kimi's own
  number for "nobody said", which is what lets MCC state the unknown case
  without inventing a context window.

**Unknown stays unknown.** ``capabilities`` and ``display_name`` are optional,
so a ``None`` from the ladder omits them. ``max_context_size`` is required, so
an unknown one is written as Kimi's own ``0`` and recorded under
``_mcc_defaulted``, which reaches the generated file, the launcher's stderr
summary and the Coding agents card. Read ``application/catalogues/base.py``
before changing anything here.

**Capabilities are a set, and a set has no "no".** ``ModelCapability`` can say
that a model does vision or does think; it cannot say that a model does not.
So a *known-absent* capability and an *unknown* one produce the same document
-- an entry with the capability left out -- and only the unknown one is
recorded as defaulted. Recording the known-absent case would be claiming Kimi
guessed at something MCC actually knows.
"""

from collections.abc import Iterable
from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues.base import (
    DEFAULTED_KEY,
    DefaultedFields,
    can_reason,
    reasoning_is_mandatory,
    visible_entries,
)
from my_claude_code.config.harnesses import (
    KIMI_API_KEY_SENTINEL,
    KIMI_BASE_URL_SENTINEL,
)

#: The one provider key MCC declares inside its own ``config.toml``. Not
#: ``kimi``: that is the name of an upstream Moonshot gateway in
#: ``config/provider_catalog.py``, and Kimi Code itself uses ``kimi`` for the
#: provider *type* that talks to Moonshot's own endpoint.
PROVIDER_ID = "mcc"

#: ``kimi_cli.llm.ProviderType`` accepts ``kimi``, ``openai_legacy``,
#: ``openai_responses``, ``anthropic``, ``google_genai``, ``gemini`` and
#: ``vertexai``. Only ``anthropic`` reaches MCC's inbound surface: it
#: constructs ``kosong.contrib.chat_provider.anthropic.Anthropic``, which is
#: the official ``anthropic`` SDK and posts ``{base_url}/messages`` with an
#: ``x-api-key`` header -- so ``base_url`` is MCC's *root*, not its ``/v1``,
#: because that SDK's route is already ``/v1/messages``.
#: ``openai_legacy`` would post ``/chat/completions``, which MCC does not
#: serve.
PROVIDER_TYPE = "anthropic"

#: Written verbatim and replaced by the caller. See
#: ``config/harness_toml.py:with_kimi_credentials``.
BASE_URL_SENTINEL = KIMI_BASE_URL_SENTINEL
API_KEY_SENTINEL = KIMI_API_KEY_SENTINEL

#: Kimi Code's whole capability vocabulary, in the order ``ModelCapability``
#: declares it. MCC intersects, never extends: an unknown string is dropped by
#: ``augment_provider_with_env_vars``' own filter and would be dropped by
#: ``Config.model_validate`` here, which fails the *entire* config rather than
#: one model.
KIMI_CAPABILITIES: tuple[str, ...] = (
    "image_in",
    "video_in",
    "thinking",
    "always_thinking",
)

#: ``max_context_size`` is a required int: Kimi branches on ``<= 0`` to fall
#: back to its own budget, so an unknown is written as ``0`` -- Kimi's own
#: marker -- rather than omitted. ``provider`` and ``model`` are what make the
#: entry routable at all.
CLI_REQUIRED_KEYS: frozenset[str] = frozenset({"provider", "model", "max_context_size"})

#: What Kimi Code itself does when a value is absent, read out of its own
#: 1.50.0 source. MCC writes only the first of these; the rest are here so the
#: consequence of an omission is stated where a reader will look for it. This
#: dict is the one place in this module allowed to hold a literal limit, and
#: the static guard test keys off its name.
CLI_DOCUMENTED_DEFAULTS: dict[str, Any] = {
    # ``max_context_size`` is required, and ``<= 0`` is Kimi's own marker for
    # "no ceiling published": ``compute_max_completion_tokens`` then returns
    # ``DEFAULT_UNKNOWN_CONTEXT_COMPLETION_TOKENS`` and auto-compaction, which
    # keys off ``max_context_size``, never triggers.
    "max_context_size": 0,
    # No ``capabilities`` means no vision, no video and no thinking offered.
    # ``derive_model_capabilities`` then adds ``thinking``/``always_thinking``
    # back for any model whose *name* contains "thinking" or "reason".
    "capabilities": None,
    # No ``display_name`` means the picker shows the model key itself.
    "display_name": None,
}


def build_kimi_catalogue(
    models: Iterable[CatalogueModel],
) -> tuple[dict[str, Any], DefaultedFields]:
    """Return Kimi Code's config document and the record of what was defaulted."""

    defaulted = DefaultedFields()
    entries: dict[str, dict[str, Any]] = {}

    for model in visible_entries(models):
        model_key = _model_key(model)
        if model_key in entries:
            continue
        entries[model_key] = _entry(model, model_key, defaulted)

    document: dict[str, Any] = {
        "providers": {
            PROVIDER_ID: {
                "type": PROVIDER_TYPE,
                "base_url": BASE_URL_SENTINEL,
                "api_key": API_KEY_SENTINEL,
            }
        },
        "models": entries,
    }
    if defaulted.by_model:
        document[DEFAULTED_KEY] = defaulted.as_document()
    return document, defaulted


def _model_key(model: CatalogueModel) -> str:
    """Return the key ``-m`` and ``/model`` address this entry by.

    ``Config.models`` is a mapping and its key is the whole model id: there is
    no separate id field, and ``app.py`` looks the ``--model`` value up in it
    directly. Prefixing with ``mcc/`` is what keeps MCC's entries apart from a
    ``kimi-for-coding`` the user configured themselves in their own file --
    which they still have, because MCC writes a document of its own.
    """

    ref = model.gateway_id if model.force_no_thinking else model.provider_model_ref
    return f"{PROVIDER_ID}/{ref}"


def _entry(
    model: CatalogueModel, model_key: str, defaulted: DefaultedFields
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "provider": PROVIDER_ID,
        # The name that goes on the wire, which is MCC's own routing ref and
        # not the ``mcc/``-prefixed key above.
        "model": model.gateway_id
        if model.force_no_thinking
        else model.provider_model_ref,
    }

    if model.context_length is None:
        entry["max_context_size"] = CLI_DOCUMENTED_DEFAULTS["max_context_size"]
        defaulted.record(model_key, "max_context_size")
    else:
        entry["max_context_size"] = model.context_length

    capabilities = _capabilities(model, model_key, defaulted)
    if capabilities:
        entry["capabilities"] = capabilities

    if model.display_name:
        entry["display_name"] = model.display_name

    return entry


def _capabilities(
    model: CatalogueModel, model_key: str, defaulted: DefaultedFields
) -> list[str]:
    """Return the subset of Kimi's four capability strings MCC can assert.

    Only positives are expressible, so this reads as three questions rather
    than three mappings: does the ladder say vision, does it say thinking, and
    does it say thinking cannot be turned off. An unknown answer is recorded;
    a known "no" is simply not written, because leaving the capability out is
    already the correct document for it.

    ``video_in`` is never emitted. The ladder resolves no video signal at all,
    so every model would be an unknown, and recording an unknown per model for
    a capability nothing upstream publishes would bury the two that matter.
    """

    capabilities: list[str] = []

    if model.supports_vision is None:
        defaulted.record(model_key, "capabilities.image_in")
    elif model.supports_vision:
        capabilities.append("image_in")

    if model.force_no_thinking:
        # The variant exists to run without thinking. Emitting no thinking
        # capability is a statement about the variant, not a guess -- and it
        # matters, because Kimi's ``derive_model_capabilities`` re-adds
        # ``thinking`` for any model *named* like one.
        return _in_vocabulary_order(capabilities)

    reasons = can_reason(model.reasoning)
    if reasons is None:
        defaulted.record(model_key, "capabilities.thinking")
    elif reasons:
        capabilities.append("thinking")
        if reasoning_is_mandatory(model.reasoning):
            capabilities.append("always_thinking")

    return _in_vocabulary_order(capabilities)


def _in_vocabulary_order(capabilities: Iterable[str]) -> list[str]:
    """Return the capabilities in Kimi's own declaration order.

    A stable order is what makes the write-if-changed byte compare skip an
    unchanged refresh instead of rewriting the file on every launch.
    """

    chosen = set(capabilities)
    return [name for name in KIMI_CAPABILITIES if name in chosen]
