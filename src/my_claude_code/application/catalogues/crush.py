"""Serialise the neutral catalogue into Crush's ``crush.json`` shape.

Every field below came out of the CLI's own published JSON Schema -- Crush
v0.92.0, ``crush schema`` (an undocumented command; it is not in ``--help``)
-- and the behaviours were then measured on the wire against a local
Anthropic-Messages endpoint, because Crush's prose documents ``crushrc``, its
Bash configuration format, and says little about the JSON one.

What the schema states, verbatim:

* ``providers`` is an object keyed by provider id; each value carries ``id``,
  ``name``, ``base_url``, ``type``, ``api_key``, ``discover_models`` and
  ``models``.
* ``type`` is an enum; ``"anthropic"`` is the only member that reaches MCC's
  inbound surface. ``"openai-compat"`` would post ``/chat/completions``,
  which MCC does not serve.
* ``api_key``'s own example is ``"$OPENAI_API_KEY"`` -- the ``$VAR`` form is
  Crush's documented secret reference. MCC writes ``"$MCC_CRUSH_API_KEY"`` and
  the launcher sets that variable in the child process only, so **the proxy
  token never lands on disk.** Confirmed on the wire: the request carried
  ``x-api-key: <the value of that variable>``.
* ``base_url`` is the proxy **root**. Crush's Anthropic provider is
  ``anthropic-sdk-go``, which appends ``/v1/messages`` itself; a trailing
  ``/v1`` would produce ``POST /v1/v1/messages``. Confirmed on the wire:
  ``base_url: "http://127.0.0.1:PORT"`` produced ``POST /v1/messages``.
* ``discover_models`` defaults to **true** and MCC must turn it off. Measured:
  with it on, Crush issues ``GET <base_url>/models`` -- not ``/v1/models`` --
  so against a root base URL it discovers nothing and against a ``/v1`` one it
  would break the messages route. The explicit list is the only correct
  option here.
* ``models[]`` entries have ten **required** fields: ``id``, ``name``, the
  four ``cost_per_1m_*``, ``context_window``, ``default_max_tokens``,
  ``can_reason`` and ``supports_attachments``. Optional: ``reasoning_levels``,
  ``default_reasoning_effort``, ``options``.
* ``models.large`` / ``models.small`` are ``{model, provider}`` pairs naming
  the model each agent role runs on. Crush will not start a session without
  them, so the serialiser names one.

**Reasoning vocabulary.** ``SelectedModel.reasoning_effort`` is an enum of
``low``/``medium``/``high``; :data:`CRUSH_EFFORTS` is that list in that order.
MCC intersects, never extends.

**Unknown stays unknown -- and this is the harness where that costs the most.**
Ten fields are required, so there is no key to omit: a ``None`` from the ladder
has to become *Crush's own* value, and every substitution is recorded per
model under ``_mcc_defaulted``. That root key is tolerated: the schema says
``additionalProperties: false``, but the schema is advisory for editors and
Crush's Go decoder ignores unknown keys -- verified by loading a document
carrying the key and seeing the provider's models still listed. What Crush
itself does with each zero value is written down in
:data:`CLI_DOCUMENTED_DEFAULTS`. Read ``application/catalogues/base.py`` before
changing anything here.
"""

from collections.abc import Iterable, Sequence
from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues.base import (
    DEFAULTED_KEY,
    DefaultedFields,
    attribution_headers,
    can_reason,
    clamp_efforts,
    reasoning_is_mandatory,
    starting_model,
    visible_entries,
)
from my_claude_code.config.harnesses import (
    CRUSH_API_KEY_ENV,
    CRUSH_BASE_URL_SENTINEL,
)
from my_claude_code.core.reasoning import ReasoningEffort

#: The one provider key MCC owns in the generated document. Not ``crush``:
#: a provider id is Crush's own namespace and ``mcc`` is what every other
#: harness catalogue calls MCC's block.
PROVIDER_ID = "mcc"

PROVIDER_DISPLAY_NAME = "My Claude Code"

#: The only ``ProviderConfig.type`` that reaches ``POST /v1/messages``.
PROVIDER_TYPE = "anthropic"

#: Written verbatim into ``api_key``. Crush expands the ``$VAR`` form from the
#: process environment at request time.
API_KEY_REFERENCE = f"${CRUSH_API_KEY_ENV}"

#: Replaced by the caller before the document reaches disk: the serialiser is
#: a pure function of the model records and does not know which port this
#: install listens on.
BASE_URL_SENTINEL = CRUSH_BASE_URL_SENTINEL

#: Crush's own effort vocabulary, in Crush's own order.
CRUSH_EFFORTS: tuple[str, ...] = ("low", "medium", "high")

#: MCC effort -> nearest Crush rung. ``minimal`` folds down to ``low``;
#: ``xhigh`` and ``max`` fold up to ``high``, which is the strongest thing
#: Crush's enum can say.
CRUSH_EFFORT_BY_REASONING_EFFORT: dict[ReasoningEffort, str] = {
    ReasoningEffort.MINIMAL: "low",
    ReasoningEffort.LOW: "low",
    ReasoningEffort.MEDIUM: "medium",
    ReasoningEffort.HIGH: "high",
    ReasoningEffort.XHIGH: "high",
    ReasoningEffort.MAX: "high",
}

#: Crush's own required per-model list, taken verbatim from ``crush schema``
#: -> ``$defs.Model.required`` on v0.92.0 (vendored at
#: ``tests/fixtures/schemas/crush.schema.json``). Ten keys, every one of which
#: must carry a value even when nobody published one -- which is why this is
#: the harness where an unknown costs the most.
CLI_REQUIRED_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "name",
        "cost_per_1m_in",
        "cost_per_1m_out",
        "cost_per_1m_in_cached",
        "cost_per_1m_out_cached",
        "context_window",
        "default_max_tokens",
        "can_reason",
        "supports_attachments",
    }
)

#: Crush's own values for the required fields, and what each one costs.
#: Measured against v0.92.0 rather than assumed. This dict is the one place in
#: this module allowed to hold a literal limit, and the static guard test keys
#: off its name.
CLI_DOCUMENTED_DEFAULTS: dict[str, Any] = {
    # Measured: with ``default_max_tokens: 0`` in the document, the main agent
    # request went out with ``max_tokens: 4096`` while the title-generation
    # request went out with ``max_tokens: 0``. So 0 is not a usable stand-in
    # and Crush's own working number is written instead.
    "default_max_tokens": 4096,
    # ``context_window: 0`` loads and runs; Crush simply shows no context
    # gauge and never compacts early. That is Crush's zero value, and unlike
    # ``default_max_tokens`` it reaches no request body, so it is written as
    # is rather than replaced by a number MCC invented.
    "context_window": 0,
    # Costs are USD per million tokens. Zero reads as "free" in the stats
    # view, which is why every zero is recorded per model.
    "cost_per_1m_in": 0.0,
    "cost_per_1m_out": 0.0,
    "cost_per_1m_in_cached": 0.0,
    "cost_per_1m_out_cached": 0.0,
    # Required booleans: Go's zero value, i.e. the conservative claim.
    "can_reason": False,
    "supports_attachments": False,
}


def build_crush_catalogue(
    models: Iterable[CatalogueModel],
) -> tuple[dict[str, Any], DefaultedFields]:
    """Return Crush's ``crush.json`` document and the record of what was guessed."""

    defaulted = DefaultedFields()
    entries: list[dict[str, Any]] = []
    listed: list[CatalogueModel] = []
    seen: set[str] = set()

    for model in visible_entries(models):
        model_id = _model_id(model)
        if model_id in seen:
            continue
        seen.add(model_id)
        listed.append(model)
        entries.append(_entry(model, model_id, defaulted))

    document: dict[str, Any] = {
        "$schema": "https://charm.land/crush.json",
        "providers": {
            PROVIDER_ID: {
                "id": PROVIDER_ID,
                "name": PROVIDER_DISPLAY_NAME,
                "type": PROVIDER_TYPE,
                "base_url": BASE_URL_SENTINEL,
                "api_key": API_KEY_REFERENCE,
                # ``providers.<id>.extra_headers``, spelled the way Crush's own
                # schema spells it -- "Additional HTTP headers to send with
                # requests", ``tests/fixtures/schemas/crush.schema.json``. The
                # one header MCC adds is its non-secret attribution label; see
                # ``application/catalogues/base.attribution_headers``.
                "extra_headers": attribution_headers(),
                # See the module docstring: discovery would GET ``/models``.
                "discover_models": False,
                "models": entries,
            }
        },
    }
    selected = _selected_models(listed)
    if selected:
        document["models"] = selected
    if defaulted.by_model:
        document[DEFAULTED_KEY] = defaulted.as_document()
    return document, defaulted


def _selected_models(models: Sequence[CatalogueModel]) -> dict[str, Any]:
    """Name the model Crush's large and small agents start on.

    Crush refuses to open a session with no ``models.large``, so leaving this
    out would produce a document that lists every MCC model and cannot run any
    of them. :func:`starting_model` chooses it -- MCC's own configured route
    first -- and the same model fills both roles: inventing a "small" model by
    matching on a name would be MCC guessing which of the user's routes is
    cheap.
    """

    chosen = starting_model(models)
    if chosen is None:
        return {}
    selected = {"model": _model_id(chosen), "provider": PROVIDER_ID}
    return {"large": dict(selected), "small": dict(selected)}


def _model_id(model: CatalogueModel) -> str:
    """Return the id Crush sends as the request's ``model``.

    Crush passes ``models[].id`` through to the Anthropic body untouched --
    measured -- so this is MCC's gateway id verbatim. Crush addresses it as
    ``mcc/<id>`` in its own picker and ``crush models`` listing.
    """

    return model.gateway_id


def _entry(
    model: CatalogueModel, model_id: str, defaulted: DefaultedFields
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": model_id,
        "name": model.display_name,
        "context_window": _required_int(
            model.context_length, "context_window", model_id, defaulted
        ),
        "default_max_tokens": _required_int(
            model.max_output_tokens, "default_max_tokens", model_id, defaulted
        ),
        "supports_attachments": _required_bool(
            model.supports_vision, "supports_attachments", model_id, defaulted
        ),
    }
    entry.update(_costs(model, model_id, defaulted))
    entry.update(_reasoning(model, model_id, defaulted))

    options = _options(model)
    if options:
        entry["options"] = options
    return entry


def _required_int(
    value: int | None, field_name: str, model_id: str, defaulted: DefaultedFields
) -> int:
    if value is not None:
        return value
    defaulted.record(model_id, field_name)
    return int(CLI_DOCUMENTED_DEFAULTS[field_name])


def _required_bool(
    value: bool | None, field_name: str, model_id: str, defaulted: DefaultedFields
) -> bool:
    if value is not None:
        return value
    defaulted.record(model_id, field_name)
    return bool(CLI_DOCUMENTED_DEFAULTS[field_name])


def _costs(
    model: CatalogueModel, model_id: str, defaulted: DefaultedFields
) -> dict[str, float]:
    """Return Crush's four required cost fields, in USD per million tokens.

    That is already MCC's unit, so every rate the ladder resolves passes
    straight through -- the two cache rates included, now that they walk the
    same rungs. All four are required, so any one that stays unknown becomes
    Crush's own ``0.0`` and is recorded; none is ever derived from another.
    """

    costs: dict[str, float] = {}
    for field_name, value in (
        ("cost_per_1m_in", model.input_price),
        ("cost_per_1m_out", model.output_price),
        ("cost_per_1m_in_cached", model.cache_read_price),
        ("cost_per_1m_out_cached", model.cache_write_price),
    ):
        if value is None:
            defaulted.record(model_id, field_name)
            costs[field_name] = float(CLI_DOCUMENTED_DEFAULTS[field_name])
        else:
            costs[field_name] = float(value)
    return costs


def _reasoning(
    model: CatalogueModel, model_id: str, defaulted: DefaultedFields
) -> dict[str, Any]:
    """Return Crush's reasoning keys for one model.

    ``can_reason`` is required, so an unknown becomes Crush's ``False`` and is
    recorded. ``reasoning_levels`` and ``default_reasoning_effort`` are
    optional and are written only where the ladder actually resolved a
    vocabulary; a model whose thinking is ``mandatory`` still gets
    ``can_reason: true`` with its rungs, because Crush's own "off" is
    ``think: false`` on the *selected model*, which MCC does not write.
    """

    if model.force_no_thinking:
        # The variant exists to run without thinking. Stating it is a fact
        # about the variant, not a guess.
        return {"can_reason": False}

    reasons = can_reason(model.reasoning)
    rungs, unknown = clamp_efforts(
        model.reasoning, CRUSH_EFFORTS, CRUSH_EFFORT_BY_REASONING_EFFORT
    )

    if reasons is None:
        defaulted.record(model_id, "can_reason")
        return {"can_reason": bool(CLI_DOCUMENTED_DEFAULTS["can_reason"])}
    if reasons is False:
        return {"can_reason": False}

    block: dict[str, Any] = {"can_reason": True}
    if rungs:
        block["reasoning_levels"] = rungs
        block["default_reasoning_effort"] = _default_effort(model, rungs)
    elif unknown:
        # Known to reason, nothing published about how it is steered: leave
        # the optional keys out and let Crush apply its own.
        defaulted.record(model_id, "reasoning_levels")
    return block


def _default_effort(model: CatalogueModel, rungs: list[str]) -> str:
    """Pick the rung a session starts on.

    The middle of what the model supports, except where thinking cannot be
    turned off at all -- there the strongest rung is the honest starting
    point, because the caller has no "off" to fall back to.
    """

    if reasoning_is_mandatory(model.reasoning):
        return rungs[-1]
    return rungs[len(rungs) // 2]


def _options(model: CatalogueModel) -> dict[str, Any]:
    """Return the sampling parameters the gateway pins and rejects overrides for.

    ``ModelOptions`` accepts exactly five named knobs plus a free-form
    ``provider_options``; anything the gateway pins that is not one of the
    five goes into ``provider_options`` rather than being dropped.
    """

    if not model.default_parameters:
        return {}
    named = ("temperature", "top_p", "top_k", "frequency_penalty", "presence_penalty")
    options: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    for key, value in dict(model.default_parameters).items():
        if key in named:
            options[key] = value
        else:
            extra[key] = value
    if extra:
        options["provider_options"] = extra
    return options
