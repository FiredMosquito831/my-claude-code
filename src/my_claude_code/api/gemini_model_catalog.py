"""``GET /v1beta/models`` built from the resolution ladder.

``/v1/models`` answers the OpenAI-shaped question -- "what ids may I send?" --
and carries an id and a display name because that is all its schema has room
for. Google's ``Model`` resource has room for two more facts a coding agent
actually uses: ``inputTokenLimit``, which is the context gauge every Gemini
client renders, and ``outputTokenLimit``, which is what one of them uses to cap
``maxOutputTokens``. Those come from :class:`CatalogueModel` -- the ladder's own
answer -- rather than from the ``/v1/models`` payload, which never had them.

**Unknown stays unknown.** A ``None`` from the ladder omits the key; nothing
here substitutes a number. See ``application/catalogues/base.py`` for the rule
and why it is load-bearing.

The listing is built from the same enumeration and the same visibility filter
as ``/v1/models``, and then through the same
:func:`~my_claude_code.application.catalogues.base.visible_entries` collapse
every generated harness catalogue uses. That last step is the difference
between this route and ``/v1/models``, and it is deliberate: a Gemini client
is a picker, not a protocol surface, so it gets the picker's list -- one entry
per model under its plain id, no ``claude-3-freecc-no-thinking/`` twin and no
``:batch`` pricing tier. The eight fixed Claude protocol aliases are absent
from both, being protocol names rather than routable refs.
"""

from typing import Any

from my_claude_code.application.catalogue_model import build_catalogue_models
from my_claude_code.application.catalogues.base import visible_entries
from my_claude_code.application.ports import RequestRuntimePort
from my_claude_code.config.settings import Settings
from my_claude_code.core.gemini_api import gemini_model_entry, gemini_models_payload


def build_gemini_models_payload(
    settings: Settings, runtime: RequestRuntimePort
) -> dict[str, Any]:
    """Return the ``ListModels`` body for every model MCC can route."""

    models = [
        gemini_model_entry(
            model.gateway_id,
            display_name=model.display_name,
            input_token_limit=model.context_length,
            output_token_limit=model.max_output_tokens,
        )
        for model in visible_entries(build_catalogue_models(settings, runtime))
    ]
    return gemini_models_payload(models)


def find_gemini_model_entry(
    settings: Settings, runtime: RequestRuntimePort, model_id: str
) -> dict[str, Any] | None:
    """Return one model's ``Model`` resource, or ``None`` when it is unknown.

    Matched on the gateway id exactly as the path carried it. A Claude
    protocol alias resolves through the router at request time and is
    deliberately not described here: this route answers "what did the ladder
    resolve for this ref", and an alias has no ref of its own.
    """

    for model in visible_entries(build_catalogue_models(settings, runtime)):
        if model.gateway_id == model_id:
            return gemini_model_entry(
                model.gateway_id,
                display_name=model.display_name,
                input_token_limit=model.context_length,
                output_token_limit=model.max_output_tokens,
            )
    return None
