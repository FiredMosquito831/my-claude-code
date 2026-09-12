"""Serialise the neutral catalogue into a Goose custom-provider document.

Goose (Block) reads a custom provider from its own JSON file in a
``custom_providers/`` directory beside ``config.yaml`` -- one file per
provider, wholly owned by whoever wrote it. That is the reason this harness is
the least invasive in the desktop set: MCC writes a file of its own and touches
the user's ``config.yaml`` for exactly one scalar, ``GOOSE_PROVIDER``.

**Every key below was read out of Goose's own source, not out of its docs.**
The artefact is the release's own source archive,
``goose-source-v1.50.0.zip`` from ``block/goose`` tag ``v1.50.0``, and the
vendored rule plus the exact extraction commands are
``tests/fixtures/app_rules/goose-1.50.0.json`` and ``versions.txt``. That
matters because what MCC wrote until 6.84.0 was a document Goose cannot parse
at all, in two independent ways at once:

* ``application/catalogues/goose.py`` wrapped the provider in a
  ``{"custom_provider": {...}}`` envelope. Goose's loader
  (``crates/goose/src/config/declarative_providers.rs``:
  ``load_custom_providers`` -> ``deserialize_provider_config``) deserialises
  each ``custom_providers/*.json`` file **directly** into
  ``DeclarativeProviderConfig``. Goose's own writer,
  ``create_custom_provider``, emits exactly that struct with
  ``serde_json::to_string_pretty`` and no wrapper. A wrapped document is a
  struct with every required field missing.
* The desktop writer, which never used that envelope, wrote ``api_url`` and
  ``model_details``. Neither is a field of the struct: the URL is ``base_url``
  (``api_url`` is the name of the *parameter* Goose's own create-provider API
  takes, and it is assigned to ``base_url`` before the file is written) and
  the model list is ``models``. It also omitted ``engine``, which has no serde
  default and is therefore required.

So both halves of the contradiction spec §2.9 recorded were wrong, and there
is now one shape, this one, for one file.

The struct's own rules, from
``crates/goose-providers/src/declarative.rs``:

* Required (no ``#[serde(default)]``, not ``Option``): ``name``, ``engine``,
  ``display_name``, ``base_url``, ``models``.
* ``engine`` is an enum whose ``FromStr`` accepts ``openai`` /
  ``openai_compatible`` / ``anthropic`` / ``anthropic_compatible`` / ``ollama``
  / ``ollama_compatible``. MCC serves an OpenAI-compatible surface at
  ``/v1/chat/completions``, so it is ``openai``.
* ``base_path`` is optional and **overrides** the path derived from
  ``base_url``; ``openai::from_declarative_config`` splits the URL into host
  and path and uses ``base_path`` when it is set. MCC names it rather than
  relying on ``derive_base_path``'s default, which is where
  ``BaseUrlShape.SPLIT_HOST_PATH`` comes from: the host in one field and the
  path in another is Goose's shape, declared as data.
* ``api_key_env`` is the *name of a key*, and it is resolved through
  ``ConfigKeyResolver`` -> ``Config::get_secret`` -- Goose's keyring, or
  ``secrets.yaml`` when ``GOOSE_DISABLE_KEYRING`` is set -- never the process
  environment. A literal written anywhere in this document would silently not
  work, so MCC writes none and the card says where the key goes.
* ``requires_auth`` defaults to true; with it true and the key missing,
  ``from_declarative_config`` bails rather than sending an unauthenticated
  request, which is the honest failure.

**Model entries are Goose's ``ModelInfo``**
(``crates/goose-provider-types/src/base.rs``). Six of its members are
``Option`` *without* ``#[serde(default)]``, so serde requires the **key** to
be present even when the value is null: ``name``, ``context_limit``,
``input_token_cost``, ``output_token_cost``, ``currency`` and
``supports_cache_control``. ``reasoning`` carries a default and is written
anyway because MCC knows the answer. There is no ``id``, no ``output_limit``,
no ``supports_vision`` and no ``supports_tools`` -- the four keys MCC used to
write.

**Costs are per token, not per million.** ``input_token_cost`` is documented
in the struct as "Cost per token for input in USD", while MCC's ladder carries
prices per million, so every price is divided by
:data:`TOKENS_PER_PRICE_UNIT`. Writing MCC's number unconverted would have
overstated Goose's cost display by a factor of a million.

**No attribution header.** Goose's ``headers`` field is real and optional, but
``goose`` stays in ``HARNESSES_WITHOUT_ATTRIBUTION_HEADER`` and is identified
by user-agent instead -- the same answer OpenCode reached once its ignored
``options.headers`` was measured.

**Unknown stays unknown.** A price or a limit the ladder did not resolve is
written as ``null``, which is what Goose's own ``ModelInfo::new`` puts there,
and the omission is recorded in :class:`DefaultedFields` so the dashboard can
still say which numbers came from the provider.
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
    GOOSE_BASE_PATH_VALUE,
    GOOSE_BASE_URL_SENTINEL,
)

#: The provider id MCC claims, matching every other harness catalogue. Goose
#: keys the file by it: ``load_provider`` reads ``custom_providers/<id>.json``
#: and ``validate_provider_id`` allows lowercase ASCII, digits, ``_`` and
#: ``-``, so ``mcc`` is a legal id as well as a consistent one.
PROVIDER_ID = "mcc"

PROVIDER_DISPLAY_NAME = "My Claude Code"

#: ``DeclarativeProviderConfig.engine``, in the spelling Goose's own
#: ``ProviderEngine`` deserialises. MCC's OpenAI-compatible surface.
PROVIDER_ENGINE = "openai"

#: Replaced by the caller before the document reaches disk. This one is the
#: proxy **root**: Goose takes the host from here and the path from
#: :data:`BASE_PATH`.
BASE_URL_SENTINEL = GOOSE_BASE_URL_SENTINEL

#: The other half of the split, written explicitly rather than left to
#: ``derive_base_path``'s default so a Goose release that changes the default
#: cannot silently move MCC's endpoint.
BASE_PATH = GOOSE_BASE_PATH_VALUE

#: The keyring entry Goose resolves the credential from. Never a value.
API_KEY_ENV = "MCC_AUTH_TOKEN"

#: Goose's ``ModelInfo`` members that serde requires a key for, even when the
#: value is null: they are ``Option`` without ``#[serde(default)]``. Read from
#: ``crates/goose-provider-types/src/base.rs``.
CLI_REQUIRED_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "context_limit",
        "input_token_cost",
        "output_token_cost",
        "currency",
        "supports_cache_control",
    }
)

#: MCC's prices are per million tokens; Goose's are per token.
TOKENS_PER_PRICE_UNIT = 1_000_000

#: The currency every price in MCC's ladder is quoted in. Goose's field is a
#: free string defaulting to ``"$"`` in its own UI; MCC states it rather than
#: leaving a required key null, because the number beside it is meaningless
#: without it.
CURRENCY = "USD"

#: Nothing is substituted: an unresolved value is written as ``null``, which
#: is the value Goose's own constructor uses. Declared so the contract test
#: that scans for the name finds a fact rather than an omission.
CLI_DOCUMENTED_DEFAULTS: dict[str, Any] = {}


def build_goose_catalogue(
    models: Iterable[CatalogueModel],
) -> tuple[dict[str, Any], DefaultedFields]:
    """Return the custom-provider document MCC owns whole, and what was guessed.

    The document is a bare ``DeclarativeProviderConfig`` -- the same bytes
    Goose's own ``create_custom_provider`` writes -- and it is the *same*
    document the desktop card writes, because the registry's
    ``DesktopProvider`` names these keys and the desktop writer fills them from
    here. One shape, one file.
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
        "name": PROVIDER_ID,
        "engine": PROVIDER_ENGINE,
        "display_name": PROVIDER_DISPLAY_NAME,
        "base_url": BASE_URL_SENTINEL,
        "base_path": BASE_PATH,
        # The name of a keyring entry. Goose resolves it through
        # ``Config::get_secret``; the value never reaches this document.
        "api_key_env": API_KEY_ENV,
        "requires_auth": True,
        # Named, not discovered: ``dynamic_models`` unset means Goose tries
        # its ``/v1/models`` route first and falls back to this list on 404,
        # and the list is what shows the mcc/* tiers in its picker either way.
        "models": entries,
    }
    if defaulted.by_model:
        document[DEFAULTED_KEY] = defaulted.as_document()
    return document, defaulted


def _entry(model: CatalogueModel, defaulted: DefaultedFields) -> dict[str, Any]:
    """Return one Goose ``ModelInfo``, with null where nobody published a value.

    Every required key is present whatever the ladder resolved, because serde
    requires the key and not the value: an ``Option<T>`` with no
    ``#[serde(default)]`` refuses a document that leaves it out. The record of
    what stayed unknown is kept so the dashboard can still answer "which of
    these numbers came from the provider?".
    """

    # Cache control is knowable only from the cached rates: a model with a
    # cache-read or cache-write price has a cache, and one with neither has
    # published nothing either way. "Nobody said" is not "no".
    caches = (
        True
        if model.cache_read_price is not None or model.cache_write_price is not None
        else None
    )
    return {
        "name": model.gateway_id,
        "context_limit": _known(
            model.context_length, model.gateway_id, "context_limit", defaulted
        ),
        "input_token_cost": _per_token(
            model.input_price, model.gateway_id, "input_token_cost", defaulted
        ),
        "output_token_cost": _per_token(
            model.output_price, model.gateway_id, "output_token_cost", defaulted
        ),
        "currency": CURRENCY,
        "supports_cache_control": _known(
            caches, model.gateway_id, "supports_cache_control", defaulted
        ),
        "reasoning": bool(can_reason(model.reasoning)),
    }


def _known(
    value: object, model_id: str, field_name: str, defaulted: DefaultedFields
) -> Any:
    """Return the value, recording the omission when there is none to state."""

    if value is None:
        defaulted.record(model_id, field_name)
        return None
    return value


def _per_token(
    price: float | None, model_id: str, field_name: str, defaulted: DefaultedFields
) -> float | None:
    """Return a per-million price as the per-token one Goose's field means."""

    if price is None:
        defaulted.record(model_id, field_name)
        return None
    return price / TOKENS_PER_PRICE_UNIT
