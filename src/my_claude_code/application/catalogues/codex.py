"""Serialise the neutral catalogue into Codex's ``model_catalog_json`` shape.

What this replaced: a builder that emitted the *same dict for every model*,
varying only the slug, the display name and the priority. Every entry claimed
``"context_window": 200000``, the same four reasoning rungs, and
``"input_modalities": ["text"]``, because its only input was ``GET /v1/models``
and that payload carries no capability fields at all. A 32k deployment and a
1M-token model were advertised to Codex's picker as identical.

Now every field that has a ladder answer carries it, and every field that does
not is either omitted (where Codex's schema makes it optional) or filled from
:data:`CLI_DOCUMENTED_DEFAULTS` -- Codex's own numbers, not MCC's -- with the
substitution recorded in the file's ``_mcc_defaulted`` block so the guess is
visible. Read ``application/catalogues/base.py`` before changing anything here.

**Three keys this module used to write are gone**, all three read out of the
0.151.0 binary rather than inferred:

* ``context_window`` / ``max_context_window`` are ``Option<i64>`` with
  ``#[serde(default)]`` -- optional. Writing ``200000`` for an unknown window
  was the very literal this module was created to remove, and it was doing it
  for 54 of 142 models on a real install. Now omitted and recorded.
* ``reasoning_required`` **does not exist in 0.151.0 at all** (``grep -a -c``
  over the binary returns 0, and it is absent from the ``ModelInfo`` serde
  field list). Emitting it protected nothing: a model whose thinking cannot be
  turned off was never actually protected in Codex, and a key the CLI ignores
  is worse than no key, because it reads as a guarantee.
* ``supports_parallel_tool_calls`` exists in the binary but on
  ``RawMcpServerConfig`` and the MCP tool-info struct, **not on ``ModelInfo``**
  (byte evidence: ``tool_timeout_secrequiredsupports_parallel_tool_calls
  omit_tools_from...struct RawMcpServerConfig with 28 elements``). It was the
  single largest defaulted field in the generated document -- 63 of 142
  models -- reporting substitutions into a key Codex does not read.

Where a capability MCC knows has no ``ModelInfo`` field to carry it, the
honest answer is to write nothing and say so, not to write into a field name
that looks right.
"""

from collections.abc import Iterable, Mapping
from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues.base import (
    DEFAULTED_KEY,
    DefaultedFields,
    can_reason,
    clamp_efforts,
    visible_entries,
)
from my_claude_code.core.reasoning import ReasoningEffort

#: Codex's own reasoning vocabulary, in Codex's own order, with the wording
#: Codex's picker shows. MCC never adds a rung to this list; it only ever
#: intersects a model's published efforts with it.
CODEX_REASONING_LEVELS: dict[str, str] = {
    "low": "Fast responses with lighter reasoning",
    "medium": "Balances speed and reasoning depth for everyday tasks",
    "high": "Greater reasoning depth for complex problems",
    "xhigh": "Extra high reasoning depth for complex problems",
}

#: MCC effort -> nearest Codex rung. ``minimal`` and ``max`` have no Codex
#: counterpart of their own and fold onto the adjacent rung rather than being
#: dropped, so a model that only publishes them still gets a usable control.
CODEX_EFFORT_BY_REASONING_EFFORT: dict[ReasoningEffort, str] = {
    ReasoningEffort.MINIMAL: "low",
    ReasoningEffort.LOW: "low",
    ReasoningEffort.MEDIUM: "medium",
    ReasoningEffort.HIGH: "high",
    ReasoningEffort.XHIGH: "xhigh",
    ReasoningEffort.MAX: "xhigh",
}

CODEX_BASE_INSTRUCTIONS = (
    "You are Codex, a coding agent. Help the user understand, modify, test, "
    "and review code in their workspace. Follow the user's instructions, use "
    "tools when needed, and communicate concise progress and verification."
)

#: Keys Codex refuses a catalogue entry without, read out of the 0.151.0
#: binary's ``ModelInfo`` serde field list (a serde field with no
#: ``#[serde(default)]`` is a ``missing field`` parse error when absent).
#: ``context_window`` is deliberately NOT here: it carries
#: ``#[serde(default, skip_serializing_if = "Option::is_none")]``, so it is
#: optional and an unknown window is omitted rather than invented.
CLI_REQUIRED_KEYS: frozenset[str] = frozenset(
    {
        "slug",
        "display_name",
        "description",
        "supported_reasoning_levels",
        "shell_type",
        "visibility",
        "supported_in_api",
        "priority",
        "support_verbosity",
        "default_verbosity",
        "apply_patch_tool_type",
        "truncation_policy",
        "experimental_supported_tools",
    }
)

#: Codex's own numbers. Two kinds live here and both belong to Codex rather
#: than to MCC: the values applied where Codex's schema requires something
#: MCC's ladder could not supply (every such use is recorded under
#: ``_mcc_defaulted``), and Codex's fixed client-side policy values, which are
#: not model capabilities at all. This dict is the one place in the package
#: allowed to hold a literal limit; the static guard test keys off its name.
CLI_DOCUMENTED_DEFAULTS: dict[str, Any] = {
    "truncation_policy": {"mode": "tokens", "limit": 10000},
    "supported_reasoning_levels": ("low", "medium", "high", "xhigh"),
    "default_reasoning_level": "medium",
    "supports_reasoning_summaries": True,
    "input_modalities": ("text",),
}


def build_codex_catalogue(
    models: Iterable[CatalogueModel],
) -> tuple[dict[str, Any], DefaultedFields]:
    """Return Codex's catalogue document and the record of what was defaulted."""

    defaulted = DefaultedFields()
    entries: list[dict[str, Any]] = []
    seen_slugs: set[str] = set()

    for model in visible_entries(models):
        slug = _slug(model)
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        entries.append(_entry(model, slug, priority=len(entries), defaulted=defaulted))

    document: dict[str, Any] = {"models": entries}
    if defaulted.by_model:
        document[DEFAULTED_KEY] = defaulted.as_document()
    return document, defaulted


def _slug(model: CatalogueModel) -> str:
    """Return the id Codex routes with.

    A normal model is addressed by its bare ``provider/model`` ref; the
    no-thinking variant keeps its full gateway id, because the prefix is the
    entire mechanism that turns thinking off.
    """

    return model.gateway_id if model.force_no_thinking else model.provider_model_ref


def _entry(
    model: CatalogueModel,
    slug: str,
    *,
    priority: int,
    defaulted: DefaultedFields,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "slug": slug,
        "display_name": model.display_name,
        "description": "My Claude Code provider model",
        "shell_type": "shell_command",
        "visibility": "list",
        "supported_in_api": True,
        "priority": priority,
        "additional_speed_tiers": [],
        "service_tiers": [],
        "base_instructions": CODEX_BASE_INSTRUCTIONS,
        "default_reasoning_summary": "none",
        "support_verbosity": True,
        "default_verbosity": "low",
        "apply_patch_tool_type": "freeform",
        "web_search_tool_type": "text_and_image",
        "truncation_policy": dict(CLI_DOCUMENTED_DEFAULTS["truncation_policy"]),
        "supports_image_detail_original": True,
        "effective_context_window_percent": 95,
        "experimental_supported_tools": [],
        "input_modalities": _input_modalities(model, slug, defaulted),
        "supports_search_tool": True,
        "use_responses_lite": False,
        **_context_fields(model, slug, defaulted),
        **_reasoning_fields(model, slug, defaulted),
    }
    return entry


def _context_fields(
    model: CatalogueModel, slug: str, defaulted: DefaultedFields
) -> Mapping[str, Any]:
    """Return Codex's two context keys, or neither when nobody published one.

    ``context_window`` and ``max_context_window`` are both
    ``#[serde(default, skip_serializing_if = "Option::is_none")] Option<i64>``
    in 0.151.0 -- **optional**, and Codex supplies its own when they are
    absent. So the rule in ``base.py`` says omit, and this module used to
    write ``200000`` instead: the exact literal its own docstring was written
    to eliminate, reinstated one field over. It is gone, and the omission is
    recorded like every other.
    """

    if model.context_length is None:
        defaulted.record(slug, "context_window")
        defaulted.record(slug, "max_context_window")
        return {}
    return {
        "context_window": model.context_length,
        "max_context_window": model.context_length,
    }


def _input_modalities(
    model: CatalogueModel, slug: str, defaulted: DefaultedFields
) -> list[str]:
    if model.supports_vision is True:
        return ["text", "image"]
    if model.supports_vision is None:
        defaulted.record(slug, "input_modalities")
    return list(CLI_DOCUMENTED_DEFAULTS["input_modalities"])


def _reasoning_fields(
    model: CatalogueModel, slug: str, defaulted: DefaultedFields
) -> Mapping[str, Any]:
    reasoning = model.reasoning
    reasons = can_reason(reasoning)

    if model.force_no_thinking or reasons is False:
        # A model that does not reason gets no effort list at all: an empty
        # picker is the honest shape, and Codex renders it as "no reasoning".
        return {
            "supported_reasoning_levels": [],
            "supports_reasoning_summaries": False,
        }

    rungs, unknown = clamp_efforts(
        reasoning,
        tuple(CODEX_REASONING_LEVELS),
        CODEX_EFFORT_BY_REASONING_EFFORT,
    )
    if unknown:
        rungs = list(CLI_DOCUMENTED_DEFAULTS["supported_reasoning_levels"])
        defaulted.record(slug, "supported_reasoning_levels")

    fields: dict[str, Any] = {
        "supported_reasoning_levels": [
            {"effort": rung, "description": CODEX_REASONING_LEVELS[rung]}
            for rung in rungs
        ]
    }

    if rungs:
        if unknown:
            defaulted.record(slug, "default_reasoning_level")
        fields["default_reasoning_level"] = _default_level(rungs)

    if reasons is None:
        defaulted.record(slug, "supports_reasoning_summaries")
        fields["supports_reasoning_summaries"] = CLI_DOCUMENTED_DEFAULTS[
            "supports_reasoning_summaries"
        ]
    else:
        fields["supports_reasoning_summaries"] = reasons

    return fields


def _default_level(rungs: list[str]) -> str:
    """Pick the starting rung from the model's own published vocabulary.

    Codex's own preferred rung when the model supports it, otherwise the
    weakest rung it does support. Choosing among a model's published rungs is
    not an invention, so it is never recorded as defaulted; only falling back
    to Codex's whole default vocabulary for a model that published none is.
    """

    preferred = CLI_DOCUMENTED_DEFAULTS["default_reasoning_level"]
    return preferred if preferred in rungs else rungs[0]
