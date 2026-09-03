"""Serialise the neutral catalogue into Command Code's ``providers.json`` shape.

Every field below was read out of the installed CLI's own bundle -- Command
Code 1.39.0, ``dist/cli.mjs``, functions ``parseProvidersConfig``,
``parseProvider``, ``parseModel`` and ``parseCost`` -- rather than from prose,
because the vendor's docs describe the ``/connect`` wizard and not the schema
it writes. What that bundle accepts, verbatim:

* the document's provider map is ``provider`` (``providers`` is an alias);
* ``provider.<id>.api`` is ``"openai-completions" | "anthropic-messages"``,
  anything else skips the provider;
* ``provider.<id>.baseURL`` is required and, for the Anthropic wire,
  normalised to end in ``/v1`` before ``@ai-sdk/anthropic`` appends
  ``/messages`` -- so MCC's ``POST /v1/messages`` is reached exactly;
* ``provider.<id>.apiKey`` must be a *reference*: ``"$ENV_VAR"``,
  ``"{env:VAR}"`` or ``"!command"``. A literal is refused with "raw secrets
  don't belong in providers.json" and the provider then has no key at all.
  MCC writes ``"$MCC_COMMANDCODE_API_KEY"`` and the launcher sets that
  variable in the child process only, so **the proxy token never lands on
  disk** even though the document itself is the user's own file;
* per model: ``name``, ``contextWindow`` (or ``limit.context``), ``maxOutput``
  (or ``limit.output``), ``reasoning``, ``reasoningEfforts``, ``cost`` and
  ``options``. Unknown keys are ignored rather than rejected, which is what
  lets the ``_mcc_defaulted`` record live inside MCC's own subtree.

**Reasoning vocabulary.** ``parseModel`` filters ``reasoningEfforts`` against
``new Set(["low","medium","high","xhigh","max"])`` and warns "unknown levels
dropped" for anything else. :data:`COMMANDCODE_EFFORTS` is that set, in that
order; MCC intersects, never extends.

**Unknown stays unknown.** Every per-model key here is optional in the schema,
so a ``None`` from the ladder omits the key. That is not free: with
``maxOutput`` absent Command Code falls back to
``max(1024, floor(contextWindow/2))`` and with ``contextWindow`` absent too it
has no ceiling to offer at all (``moduleFor`` in the same bundle). Those are
*Command Code's* numbers, recorded in :data:`CLI_DOCUMENTED_DEFAULTS` and
reported per model under ``_mcc_defaulted``, on the launcher's stderr and on
the Coding agents card -- so a reader can tell a provider's answer from the
CLI's guess. Read ``application/catalogues/base.py`` before changing anything
here.
"""

from collections.abc import Iterable
from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues.base import (
    DEFAULTED_KEY,
    DefaultedFields,
    attribution_headers,
    can_reason,
    clamp_efforts,
    visible_entries,
)
from my_claude_code.config.harnesses import (
    COMMANDCODE_API_KEY_ENV,
    COMMANDCODE_BASE_URL_SENTINEL,
)
from my_claude_code.core.reasoning import ReasoningEffort

#: The one key MCC owns inside the user's ``providers.json``. It is not
#: ``commandcode``: that is the name of the *upstream gateway provider* in
#: ``config/provider_catalog.py``, and reusing it here would put MCC's harness
#: config under a name that already means something else in this codebase.
PROVIDER_ID = "mcc"

PROVIDER_DISPLAY_NAME = "My Claude Code"

#: ``anthropic-messages`` is the only value that reaches MCC's inbound
#: surface. ``openai-completions`` would post ``/chat/completions``, which MCC
#: does not serve.
PROVIDER_API = "anthropic-messages"

#: Written verbatim into ``apiKey``. ``resolveApiKeyValue`` expands
#: ``^\$([A-Za-z_][A-Za-z0-9_]*)$`` from the process environment at request
#: time and throws "API key environment variable ... is not set" when absent,
#: which is a far better failure than a silent 401.
API_KEY_REFERENCE = f"${COMMANDCODE_API_KEY_ENV}"

#: ``baseURL`` is the one field that cannot be a reference. ``parseProvider``
#: validates it with ``new URL(...)`` and *skips the whole provider* when it
#: does not parse, and no substitution syntax is applied to it -- only
#: ``apiKey`` goes through ``resolveApiKeyValue``. So the serialiser, which is
#: a pure function of the model records and knows nothing about which port
#: this install runs on, writes the sentinel and the caller replaces it
#: through ``config.harness_config_merge.with_base_url`` before the block
#: reaches disk.
BASE_URL_SENTINEL = COMMANDCODE_BASE_URL_SENTINEL

#: Command Code's own effort vocabulary, in Command Code's own order.
COMMANDCODE_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

#: MCC effort -> nearest Command Code rung. Five of the six are identical
#: names; ``minimal`` has no counterpart and folds down to ``low``.
COMMANDCODE_EFFORT_BY_REASONING_EFFORT: dict[ReasoningEffort, str] = {
    ReasoningEffort.MINIMAL: "low",
    ReasoningEffort.LOW: "low",
    ReasoningEffort.MEDIUM: "medium",
    ReasoningEffort.HIGH: "high",
    ReasoningEffort.XHIGH: "xhigh",
    ReasoningEffort.MAX: "max",
}

#: Empty: ``parseModel`` keeps any subset and drops what it does not
#: recognise, so no per-model key is required. What *is* required lives one
#: level up -- a provider with no ``baseURL`` is dropped, and a provider with
#: zero models is dropped entirely -- and both are structural rather than
#: per-entry.
CLI_REQUIRED_KEYS: frozenset[str] = frozenset()

#: What Command Code itself does when a per-model key is absent, read out of
#: ``moduleFor`` and ``parseModel`` in its own 1.39.0 bundle. MCC writes none
#: of these; it omits the key and records the omission, because writing a
#: literal here would be MCC asserting a limit nobody published. This dict is
#: the one place in this module allowed to hold a literal limit, and the
#: static guard test keys off its name.
CLI_DOCUMENTED_DEFAULTS: dict[str, Any] = {
    # maxOutput ?? max(1024, floor(contextWindow / 2)); nothing at all when
    # contextWindow is also absent.
    "maxOutput": {"floor": 1024, "fraction_of_context_window": 0.5},
    # contextWindow absent means Command Code offers no ceiling for the model.
    "contextWindow": None,
    # `reasoning: true` expands to ["low","medium","high"]; absent means the
    # model is treated as non-reasoning and /effort offers nothing.
    "reasoningEfforts": ["low", "medium", "high"],
    "cost": None,
}


def build_commandcode_catalogue(
    models: Iterable[CatalogueModel],
) -> tuple[dict[str, Any], DefaultedFields]:
    """Return Command Code's provider fragment and the record of what was omitted."""

    defaulted = DefaultedFields()
    entries: dict[str, dict[str, Any]] = {}

    for model in visible_entries(models):
        model_id = _model_id(model)
        if model_id in entries:
            continue
        entries[model_id] = _entry(model, model_id, defaulted)

    document: dict[str, Any] = {
        "provider": {
            PROVIDER_ID: {
                "name": PROVIDER_DISPLAY_NAME,
                "api": PROVIDER_API,
                "baseURL": BASE_URL_SENTINEL,
                "apiKey": API_KEY_REFERENCE,
                # A static map, unlike ``apiKey`` beside it: Command Code
                # expands ``"$VAR"`` in the key field and nowhere else, and
                # this value needs no expansion because it is not a secret --
                # it is the id of the CLI this block was written for, which the
                # request log reads back as attribution. See
                # ``application/catalogues/base.attribution_headers``.
                "headers": attribution_headers(),
                "models": entries,
            }
        }
    }
    if defaulted.by_model:
        document[DEFAULTED_KEY] = defaulted.as_document()
    return document, defaulted


def _model_id(model: CatalogueModel) -> str:
    """Return the id Command Code routes with, under the ``mcc/`` prefix.

    ``moduleFor`` publishes each declared id as ``mcc/<id>`` and, when the id
    is not one of its built-in catalogue names, as the bare id too. A normal
    model is addressed by its ``provider/model`` ref; the no-thinking variant
    keeps its full gateway id, because that prefix is the entire mechanism
    that turns thinking off.
    """

    return model.gateway_id if model.force_no_thinking else model.provider_model_ref


def _entry(
    model: CatalogueModel, model_id: str, defaulted: DefaultedFields
) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": model.display_name}

    if model.context_length is None:
        defaulted.record(model_id, "contextWindow")
    else:
        entry["contextWindow"] = model.context_length

    if model.max_output_tokens is None:
        defaulted.record(model_id, "maxOutput")
    else:
        entry["maxOutput"] = model.max_output_tokens

    _apply_reasoning(model, model_id, entry, defaulted)

    cost = _cost(model, model_id, defaulted)
    if cost is not None:
        entry["cost"] = cost

    options = _options(model)
    if options:
        entry["options"] = options

    return entry


def _apply_reasoning(
    model: CatalogueModel,
    model_id: str,
    entry: dict[str, Any],
    defaulted: DefaultedFields,
) -> None:
    """Write Command Code's reasoning keys, or record that nobody published them.

    Command Code reads the two keys together: ``reasoningEfforts`` is the
    effort list ``/effort`` and ``--effort`` offer, and ``reasoning: true`` is
    shorthand for its own ``["low","medium","high"]``. A model that reasons
    without an effort knob therefore gets ``reasoning`` alone, and a model
    whose ``mandatory`` flag says thinking cannot be turned off gets no
    ``reasoning: false`` -- which is exactly the state Command Code's
    ``thinkingHook`` reads as "send no effort" rather than "send off".
    """

    if model.force_no_thinking:
        # The variant exists to run without thinking. Stating it is a fact
        # about the variant, not a guess.
        entry["reasoning"] = False
        return

    reasons = can_reason(model.reasoning)
    rungs, unknown = clamp_efforts(
        model.reasoning, COMMANDCODE_EFFORTS, COMMANDCODE_EFFORT_BY_REASONING_EFFORT
    )

    if reasons is None:
        defaulted.record(model_id, "reasoning")
        defaulted.record(model_id, "reasoningEfforts")
        return
    if reasons is False:
        entry["reasoning"] = False
        return

    entry["reasoning"] = True
    if rungs:
        entry["reasoningEfforts"] = rungs
    elif unknown:
        # Known to reason, nothing published about how it is steered: leave
        # the list out and let Command Code apply its own three rungs.
        defaulted.record(model_id, "reasoningEfforts")
    else:
        # Known to reason with no caller-side knob. An empty list is a
        # statement, not an omission: it removes every rung from /effort.
        entry["reasoningEfforts"] = []


def _cost(
    model: CatalogueModel, model_id: str, defaulted: DefaultedFields
) -> dict[str, float] | None:
    """Return Command Code's cost block, or None when nobody published prices.

    ``parseCost`` keeps any subset of ``input``/``output``/``cacheRead``/
    ``cacheWrite`` and drops the block entirely when all four are absent, and
    its units are models.dev's -- USD per million tokens -- which is where
    MCC's own ``input_price`` and ``output_price`` come from, and the two
    cache rates now resolve down the same ladder. Any one of the four that
    stays unknown is omitted and recorded; none is ever derived from another.
    """

    if model.input_price is None and model.output_price is None:
        defaulted.record(model_id, "cost")
        return None
    cost: dict[str, float] = {}
    if model.input_price is None:
        defaulted.record(model_id, "cost.input")
    else:
        cost["input"] = model.input_price
    if model.output_price is None:
        defaulted.record(model_id, "cost.output")
    else:
        cost["output"] = model.output_price
    for key, value in (
        ("cacheRead", model.cache_read_price),
        ("cacheWrite", model.cache_write_price),
    ):
        if value is None:
            defaulted.record(model_id, f"cost.{key}")
        else:
            cost[key] = value
    return cost


def _options(model: CatalogueModel) -> dict[str, Any]:
    """Return the parameters the gateway pins and rejects overrides for.

    Command Code merges a model's ``options`` block into the request body
    before its own fields (``withBodyOptions``), which is exactly where a
    pinned parameter belongs.
    """

    if not model.default_parameters:
        return {}
    return dict(model.default_parameters)
