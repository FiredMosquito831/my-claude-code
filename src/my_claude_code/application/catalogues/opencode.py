"""Serialise the neutral catalogue into OpenCode's provider-config shape.

One serialiser, three harnesses: OpenCode v1, the OpenCode 2 preview and Kilo
CLI, which is a fork of OpenCode and says so -- "The Kilo CLI is a fork of
OpenCode and supports the same configuration options"
(https://kilo.ai/docs/code-with-ai/platforms/cli). All three read the schema
published at https://opencode.ai/config.json, and two of them were checked
against a real binary before this module was written: v1 ``1.18.25`` and v2
``0.0.0-beta-18743`` both loaded the document below and reported MCC's models
with MCC's limits.

**MCC never edits the user's own config.** OpenCode's documented
``OPENCODE_CONFIG`` variable (Kilo's ``KILO_CONFIG``) names an extra config
file that is "loaded between global and project configs in the precedence
order", and "configuration files are merged together, not replaced"
(https://opencode.ai/docs/config/). So MCC writes a file of its own under
``~/.fcc`` and points the launched process at it. Nothing under
``~/.config/opencode`` is read, written, merged or backed up, and a user who
stops launching through MCC is left with exactly the document they wrote.

**The token never lands on disk.** ``options.baseURL``, ``options.apiKey`` and
the ``Authorization`` header are written as OpenCode's own
``{env:VARIABLE_NAME}`` substitutions and the launcher sets those two variables
in the child process only.

**Unknown stays unknown**, in OpenCode's own vocabulary -- but ``limit`` and
``cost`` are all-or-nothing objects and that changes what "omit" may mean.
Read out of the published schema and reproduced against the shipped binary,
the model struct is::

    limit: optional({context: REQUIRED, input: optional, output: REQUIRED})
    cost:  optional({input: REQUIRED, output: REQUIRED,
                     cache_read: optional, cache_write: optional})

So the object may be absent, but a *present* one must carry both required
members. An earlier version of this module emitted whichever half it knew, and
OpenCode answered every such entry with
``Missing key provider.mcc.models.<id>.limit.context`` and refused the **whole
document** -- not the entry, the document. On a real install that was 54 of
142 models and the launch produced zero. The rule this module now obeys is:
emit ``limit`` with both keys, or not at all.

Which half to keep is not a free choice either. ``0`` is OpenCode's own
documented unknown marker -- its coercion reads an absent ``limit.context`` as
``0``, which is exactly how models.dev (OpenCode's own model database) spells
"not applicable or unknown" and how MCC's own models.dev parser already reads
it -- so filling the missing half with ``0`` invents nothing, while dropping
the object would throw away a real ceiling MCC does know. A model with
**neither** half known gets no ``limit`` at all, because an all-zero object
states nothing an absent one does not, and an absent ``limit`` loads fine.

``cost`` keeps the mirror-image rule and does **not** fill: a half-known price
genuinely is no price, and OpenCode publishes no documented spelling for an
unknown price the way it does for an unknown window. The asymmetry is
deliberate.

Every substitution and every omission is recorded under ``_mcc_defaulted`` so
the Coding agents card and the launcher's stderr summary can say which numbers
nobody published. Read ``application/catalogues/base.py`` before changing
anything here.
"""

from collections.abc import Iterable
from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues.base import (
    DEFAULTED_KEY,
    DefaultedFields,
    can_reason,
    clamp_efforts,
    visible_entries,
)
from my_claude_code.config.harnesses import (
    OPENCODE_API_KEY_ENV,
    OPENCODE_BASE_URL_ENV,
)
from my_claude_code.core.reasoning import ReasoningEffort

#: The provider key MCC owns inside the generated document. One key, one
#: owner: MCC writes this subtree and nothing else in the file.
PROVIDER_ID = "mcc"

#: JSON Schema OpenCode publishes for its own config, quoted so an editor
#: opening the generated file gets completion and validation for free.
CONFIG_SCHEMA_URL = "https://opencode.ai/config.json"

#: The AI SDK package OpenCode loads for a provider. ``@ai-sdk/anthropic``
#: speaks the native Anthropic Messages protocol and posts to
#: ``<baseURL>/messages``, which is why ``baseURL`` carries MCC's ``/v1``.
PROVIDER_NPM_PACKAGE = "@ai-sdk/anthropic"

PROVIDER_DISPLAY_NAME = "My Claude Code"

#: Environment variables the launcher sets and the document refers to through
#: OpenCode's ``{env:...}`` substitution. Named in ``config.harnesses`` so the
#: serialiser and the launcher cannot drift apart about them.
BASE_URL_ENV = OPENCODE_BASE_URL_ENV
API_KEY_ENV = OPENCODE_API_KEY_ENV

#: OpenCode's own variant vocabulary, in OpenCode's own order. MCC never adds
#: a rung; it only intersects a model's published efforts with this list.
OPENCODE_VARIANTS: tuple[str, ...] = ("low", "medium", "high", "max")

#: MCC effort -> nearest OpenCode variant name.
OPENCODE_VARIANT_BY_REASONING_EFFORT: dict[ReasoningEffort, str] = {
    ReasoningEffort.MINIMAL: "low",
    ReasoningEffort.LOW: "low",
    ReasoningEffort.MEDIUM: "medium",
    ReasoningEffort.HIGH: "high",
    ReasoningEffort.XHIGH: "max",
    ReasoningEffort.MAX: "max",
}

#: Keys the CLI refuses a model entry without. Empty for OpenCode: read out
#: of the published schema (vendored at ``tests/fixtures/schemas/``), the model
#: object declares **no** ``required`` array at all, so every key here is
#: optional. What is *not* optional is the shape of two of them --
#: ``limit.required = ["context", "output"]`` and
#: ``cost.required = ["input", "output"]`` -- and a present-but-partial object
#: is refused document-wide. That rule is enforced by the schema contract test
#: rather than by a key list, because it is a rule about members, not keys.
CLI_REQUIRED_KEYS: frozenset[str] = frozenset()

#: What OpenCode itself fills in when a per-model key is absent, measured
#: against ``opencode models --verbose`` on 1.18.25 with a model declaring
#: only a name. These are OpenCode's numbers, not MCC's, and MCC writes none
#: of them: it omits the key and records the omission, because writing a
#: literal here would be MCC asserting a limit it was never told. This dict is
#: the one place in this module allowed to hold a literal limit; the static
#: guard test keys off its name.
CLI_DOCUMENTED_DEFAULTS: dict[str, Any] = {
    "limit": {"context": 0, "output": 0},
    "cost": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
    "reasoning": False,
    "tool_call": True,
    "attachment": False,
}


def build_opencode_catalogue(
    models: Iterable[CatalogueModel],
) -> tuple[dict[str, Any], DefaultedFields]:
    """Return OpenCode's provider fragment and the record of what was omitted."""

    defaulted = DefaultedFields()
    entries: dict[str, dict[str, Any]] = {}

    for model in visible_entries(models):
        model_id = _model_id(model)
        if model_id in entries:
            continue
        entries[model_id] = _entry(model, model_id, defaulted)

    document: dict[str, Any] = {
        "$schema": CONFIG_SCHEMA_URL,
        "provider": {
            PROVIDER_ID: {
                "npm": PROVIDER_NPM_PACKAGE,
                "name": PROVIDER_DISPLAY_NAME,
                "options": {
                    "baseURL": f"{{env:{BASE_URL_ENV}}}",
                    # Both, and both are load-bearing. MCC authenticates on
                    # ``Authorization: Bearer`` -- the header Claude Code's own
                    # ANTHROPIC_AUTH_TOKEN produces -- while ``@ai-sdk/anthropic``
                    # would send ``x-api-key``, which MCC does not read. Measured
                    # against OpenCode 1.18.25: with ``apiKey`` alone every request
                    # arrived at ``POST /v1/messages`` with no credential at all
                    # and came back 401; adding this header made the same request
                    # 200. ``apiKey`` stays because the SDK expects a provider to
                    # have one.
                    "apiKey": f"{{env:{API_KEY_ENV}}}",
                    "headers": {"Authorization": f"Bearer {{env:{API_KEY_ENV}}}"},
                },
                "models": entries,
            }
        },
    }
    if defaulted.by_model:
        document[DEFAULTED_KEY] = defaulted.as_document()
    return document, defaulted


def _model_id(model: CatalogueModel) -> str:
    """Return the id OpenCode routes with, under the ``mcc/`` provider prefix.

    A normal model is addressed by its bare ``provider/model`` ref; the
    no-thinking variant keeps its full gateway id, because that prefix is the
    entire mechanism that turns thinking off.
    """

    return model.gateway_id if model.force_no_thinking else model.provider_model_ref


def _entry(
    model: CatalogueModel, model_id: str, defaulted: DefaultedFields
) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": model.display_name}

    limit = _limit(model, model_id, defaulted)
    if limit is not None:
        entry["limit"] = limit

    reasons = can_reason(model.reasoning)
    if model.force_no_thinking:
        # The variant exists to run without reasoning. Declaring it False is a
        # statement about the variant, not a guess, and it also stops OpenCode
        # synthesising thinking variants of its own for this id.
        entry["reasoning"] = False
    elif reasons is None:
        defaulted.record(model_id, "reasoning")
    else:
        entry["reasoning"] = reasons

    if model.supports_tool_calls is None:
        defaulted.record(model_id, "tool_call")
    else:
        entry["tool_call"] = model.supports_tool_calls

    if model.supports_vision is None:
        defaulted.record(model_id, "attachment")
    else:
        entry["attachment"] = model.supports_vision
        entry["modalities"] = {
            "input": ["text", "image"] if model.supports_vision else ["text"],
            "output": ["text"],
        }

    cost = _cost(model, model_id, defaulted)
    if cost is not None:
        entry["cost"] = cost

    options = _options(model)
    if options:
        entry["options"] = options

    variants = _variants(model)
    if variants:
        entry["variants"] = variants

    return entry


def _limit(
    model: CatalogueModel, model_id: str, defaulted: DefaultedFields
) -> dict[str, int] | None:
    """Return OpenCode's limit block with BOTH keys, or None when it knows none.

    A present ``limit`` must carry ``context`` and ``output`` together or the
    whole document is rejected, so the object is never half-populated. The
    unknown half is filled from :data:`CLI_DOCUMENTED_DEFAULTS` -- ``0``, which
    is OpenCode's own marker for an unknown window -- and recorded, so the
    known half survives instead of being discarded to satisfy a schema.

    ``None`` only when neither half is known: an all-zero object asserts
    nothing an absent one does not, and an absent ``limit`` loads.
    """

    context = model.context_length
    output = model.max_output_tokens
    if context is None and output is None:
        defaulted.record(model_id, "limit.context")
        defaulted.record(model_id, "limit.output")
        return None
    limit: dict[str, int] = {}
    if context is None:
        limit["context"] = CLI_DOCUMENTED_DEFAULTS["limit"]["context"]
        defaulted.record(model_id, "limit.context")
    else:
        limit["context"] = context
    if output is None:
        limit["output"] = CLI_DOCUMENTED_DEFAULTS["limit"]["output"]
        defaulted.record(model_id, "limit.output")
    else:
        limit["output"] = output
    return limit


def _cost(
    model: CatalogueModel, model_id: str, defaulted: DefaultedFields
) -> dict[str, float] | None:
    """Return OpenCode's cost block, or None when nobody published prices.

    OpenCode's schema requires ``input`` and ``output`` together inside
    ``cost``, so a half-known price is no price at all -- and unlike
    ``limit.context`` there is no OpenCode-documented spelling for an unknown
    *price* to fill the gap with honestly.

    The two cache rates are genuinely optional inside ``cost``. They are
    emitted when the ladder resolved them and recorded as OpenCode's own
    default when it did not; they are never derived from the uncached rate,
    which would be inventing a number.
    """

    if model.input_price is None or model.output_price is None:
        defaulted.record(model_id, "cost")
        return None
    cost: dict[str, float] = {
        "input": model.input_price,
        "output": model.output_price,
    }
    for key, value in (
        ("cache_read", model.cache_read_price),
        ("cache_write", model.cache_write_price),
    ):
        if value is None:
            defaulted.record(model_id, f"cost.{key}")
        else:
            cost[key] = value
    return cost


def _options(model: CatalogueModel) -> dict[str, Any]:
    """Return the parameters the gateway pins and rejects overrides for.

    OpenCode passes a model's ``options`` block straight to the AI SDK
    provider, which is exactly where a pinned parameter belongs.
    """

    if not model.default_parameters:
        return {}
    return dict(model.default_parameters)


def _variants(model: CatalogueModel) -> dict[str, dict[str, Any]]:
    """Return one variant per reasoning rung the ladder actually published.

    OpenCode's ``--variant`` flag selects a "provider-specific reasoning
    effort" and passes the variant body through to the AI SDK provider, so the
    rungs named here are exactly the model's own, clamped to OpenCode's
    vocabulary and never extended. Nothing is emitted when the ladder does not
    know, when the model cannot reason, or when it exposes no effort knob.

    Measured caveat: for a provider on ``@ai-sdk/anthropic`` OpenCode also
    synthesises ``high`` and ``max`` thinking-budget variants of its own from
    ``limit.output``. MCC cannot remove those; it can only decline to add
    rungs the model never claimed.
    """

    if model.force_no_thinking:
        return {}
    rungs, unknown = clamp_efforts(
        model.reasoning, OPENCODE_VARIANTS, OPENCODE_VARIANT_BY_REASONING_EFFORT
    )
    if unknown or not rungs:
        return {}
    return {rung: {"reasoningEffort": rung} for rung in rungs}
