"""Serialise the neutral catalogue into Gemini CLI's settings shape.

Every claim below was read out of the installed CLI's own bundle -- Gemini CLI
0.49.0, ``@google/gemini-cli/bundle/*.js`` -- because the vendor's prose says
only "set ``GEMINI_API_KEY``" and that undersells what the settings schema can
carry, and because the obvious environment-only route is a trap.

**Why a settings document exists at all.** The spec this work came from
expected an env-only harness: ``GEMINI_API_KEY`` plus ``GOOGLE_GEMINI_BASE_URL``
and nothing on disk. That does not work, and it fails *loudly and confusingly*.
``getAuthTypeFromEnv`` returns ``"gateway"`` as soon as ``GOOGLE_GEMINI_BASE_URL``
is set, and non-interactive startup then runs
``validateAuthMethod(authType)`` -- which handles ``oauth-personal``,
``compute-default-credentials``, ``gemini-api-key`` and ``vertex-ai`` and
returns ``"Invalid auth method selected."`` for everything else. So pointing
the CLI at a base URL and nothing more is a ``FATAL_AUTHENTICATION_ERROR``
before a single request is made. One settings key --
``security.auth.selectedType: "gemini-api-key"`` -- short-circuits
``getAuthTypeFromEnv`` entirely, and ``validateAuthMethod("gemini-api-key")``
asks only that ``GEMINI_API_KEY`` be non-empty.

**Which document, and why the user's is never touched.**
``getSystemSettingsPath()`` honours ``GEMINI_CLI_SYSTEM_SETTINGS_PATH``, and
``mergeSettings(system, systemDefaults, user, workspace, isTrusted)`` calls
``customDeepMerge(strategy, schemaDefaults, systemDefaults, user,
safeWorkspace, system)`` -- **system is merged last and therefore wins**. MCC
writes its own file there and sets the variable in the launched process only,
so ``~/.gemini/settings.json`` is read for everything MCC does not name (the
user's theme, their MCP servers, their memory settings all still apply) and is
never written, never backed up, and never consulted for auth. The OAuth tokens
that sit beside it are never read: the API-key path returns before
``createCodeAssistContentGenerator`` is ever reached.

**The proxy token is not in this document.** ``GEMINI_API_KEY`` is an
environment variable the launcher sets in the child process; the file names no
credential at all.

**The base URL is not in this document either.** ``GOOGLE_GEMINI_BASE_URL`` is
the only lever the CLI publishes for it (``createContentGeneratorConfig`` reads
it into ``httpOptions.baseUrl``), and it is an environment variable, so this
serialiser stays a pure function of the model records with no sentinel to
resolve. ``validateBaseUrl`` is ``new URL(...)`` and nothing more -- there is
no HTTPS enforcement in the code, whatever the docs claim -- so
``http://127.0.0.1:<port>`` is accepted.

**The model list.** Gemini CLI runs one model at a time and builds its picker
from a hardcoded constant (``VALID_GEMINI_MODELS``), so MCC cannot add entries
to that list and does not pretend to: ``model.name`` is set to the ladder's
first visible model and ``-m`` reaches any of the others. What MCC *can*
publish per model is ``modelConfigs.customAliases``, whose own schema
description is "Custom named presets for model configs. These are merged with
(and override) the built-in aliases" -- and ``getResolvedConfig({model, ...})``
is called unconditionally on the main chat path, not behind the
``experimentalDynamicModelConfiguration`` flag. So an alias keyed by MCC's
gateway id really does supply that model's ``generateContentConfig``. The
built-in aliases survive, because ``customAliases`` is a key of its own rather
than a replacement for ``aliases``.

**Telemetry.** ``privacy.usageStatisticsEnabled`` defaults to ``true`` and
``ClearcutLogger`` posts to ``play.googleapis.com`` every 60 seconds off the
back of it. There is no environment variable for it. MCC turns it off in its
own document, because a session routed through a local proxy has no business
reporting itself to Google.
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
from my_claude_code.core.reasoning import ReasoningEffort

#: ``security.auth.selectedType``. The one key that stops
#: ``getAuthTypeFromEnv`` from choosing ``"gateway"``, which
#: ``validateAuthMethod`` then refuses.
AUTH_TYPE = "gemini-api-key"

#: Where the per-model presets go. ``customAliases``, not ``aliases``: the
#: latter's schema default *is* the built-in preset chain (``base``,
#: ``chat-base``, ``chat-base-3``), and naming it would replace them.
CUSTOM_ALIASES_PATH: tuple[str, ...] = ("modelConfigs", "customAliases")

#: Gemini 3's reasoning vocabulary, weakest first. ``thinkingLevel`` is what
#: the CLI's own ``chat-base-3`` preset sends and what MCC's inbound surface
#: reads back into a named effort.
GEMINI_THINKING_LEVELS: tuple[str, ...] = ("LOW", "MEDIUM", "HIGH")

#: MCC effort -> nearest Gemini rung. ``minimal`` folds down to ``LOW`` and
#: both ``xhigh`` and ``max`` fold up to ``HIGH``, which are the only three
#: values Google documents for ``thinkingLevel``.
GEMINI_LEVEL_BY_REASONING_EFFORT: dict[ReasoningEffort, str] = {
    ReasoningEffort.MINIMAL: "LOW",
    ReasoningEffort.LOW: "LOW",
    ReasoningEffort.MEDIUM: "MEDIUM",
    ReasoningEffort.HIGH: "HIGH",
    ReasoningEffort.XHIGH: "HIGH",
    ReasoningEffort.MAX: "HIGH",
}

#: What Gemini CLI itself does when a ``generateContentConfig`` key is absent,
#: read out of its own 0.49.0 bundle. MCC writes none of these; it omits the
#: key and records the omission. This dict is the one place in this module
#: allowed to hold a literal limit, and the static guard test keys off its name.
CLI_DOCUMENTED_DEFAULTS: dict[str, Any] = {
    # No maxOutputTokens means the CLI sends none and lets the server decide
    # where the answer stops.
    "maxOutputTokens": None,
    # No thinkingConfig means the alias inherits ``chat-base``'s, which asks
    # for thoughts and names no budget.
    "thinkingConfig": None,
}


def build_gemini_cli_catalogue(
    models: Iterable[CatalogueModel],
) -> tuple[dict[str, Any], DefaultedFields]:
    """Return Gemini CLI's settings document and the record of what was omitted."""

    defaulted = DefaultedFields()
    aliases: dict[str, Any] = {}
    first_model: str | None = None

    for model in visible_entries(models):
        model_id = model.gateway_id
        if model_id in aliases:
            continue
        if first_model is None:
            first_model = model_id
        aliases[model_id] = _alias(model, model_id, defaulted)

    document: dict[str, Any] = {
        "security": {"auth": {"selectedType": AUTH_TYPE}},
        # A session routed through a local proxy has no business reporting
        # itself to Google, and there is no environment variable for this.
        "privacy": {"usageStatisticsEnabled": False},
        "modelConfigs": {"customAliases": aliases},
    }
    if first_model is not None:
        # Gemini CLI runs one model at a time; ``-m`` moves it. Without this
        # the CLI would default to the alias ``auto`` and resolve it to a
        # Google model id this proxy has never heard of.
        document["model"] = {"name": first_model}
    if defaulted.by_model:
        document[DEFAULTED_KEY] = defaulted.as_document()
    return document, defaulted


def _alias(
    model: CatalogueModel, model_id: str, defaulted: DefaultedFields
) -> dict[str, Any]:
    """Return one ``customAliases`` entry.

    ``extends: "chat-base"`` rather than ``chat-base-3`` on purpose: the
    Gemini-3 preset sends ``thinkingLevel: HIGH`` unconditionally, which is a
    claim about a Google model and not about whatever MCC routes this id to.
    Inheriting the neutral chat preset takes the sampling defaults and leaves
    the reasoning key to be written only where the ladder actually knows
    something.
    """

    model_config: dict[str, Any] = {"model": model_id}
    generate = _generate_content_config(model, model_id, defaulted)
    if generate:
        model_config["generateContentConfig"] = generate
    return {"extends": "chat-base", "modelConfig": model_config}


def _generate_content_config(
    model: CatalogueModel, model_id: str, defaulted: DefaultedFields
) -> dict[str, Any]:
    config: dict[str, Any] = {}

    if model.max_output_tokens is None:
        defaulted.record(model_id, "maxOutputTokens")
    else:
        config["maxOutputTokens"] = model.max_output_tokens

    thinking = _thinking_config(model, model_id, defaulted)
    if thinking is not None:
        config["thinkingConfig"] = thinking

    return config


def _thinking_config(
    model: CatalogueModel, model_id: str, defaulted: DefaultedFields
) -> dict[str, Any] | None:
    """Return Gemini's ``thinkingConfig``, or ``None`` when nothing is known.

    ``thinkingBudget: 0`` is Google's own documented way of spelling "do not
    think", which is exactly what the no-thinking variant of a ref exists to
    say, so it is the one value this function may state with certainty. A
    model known to reason gets its strongest supported rung as
    ``thinkingLevel`` and ``includeThoughts`` so the CLI renders the thinking
    it paid for; a model that reasons with no caller-side knob gets
    ``includeThoughts`` alone.
    """

    if model.force_no_thinking:
        return {"thinkingBudget": 0}

    reasons = can_reason(model.reasoning)
    rungs, unknown = clamp_efforts(
        model.reasoning, GEMINI_THINKING_LEVELS, GEMINI_LEVEL_BY_REASONING_EFFORT
    )

    if reasons is None:
        defaulted.record(model_id, "thinkingConfig")
        return None
    if reasons is False:
        return {"thinkingBudget": 0}
    if rungs:
        return {"thinkingLevel": rungs[-1], "includeThoughts": True}
    if unknown:
        # Known to reason, nothing published about how it is steered.
        defaulted.record(model_id, "thinkingConfig.thinkingLevel")
    return {"includeThoughts": True}
