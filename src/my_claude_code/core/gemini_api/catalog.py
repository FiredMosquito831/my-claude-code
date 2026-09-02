"""The ``models`` listing shape ``GET /v1beta/models`` answers with.

Pure protocol shaping, with no knowledge of where the numbers came from:
``core`` may not import ``application``, so the API boundary resolves the
ladder into these arguments and this module decides only what a Google
``Model`` resource looks like.

**Unknown stays unknown**, the same rule the harness catalogue serialisers
obey. ``inputTokenLimit`` and ``outputTokenLimit`` are optional in Google's own
schema, so a ``None`` from the ladder omits the key rather than publishing a
zero or a number MCC invented. A client that reads the omission learns "nobody
published this", which is not the same claim as "this model has no limit".
"""

from typing import Any

from .paths import SUPPORTED_GENERATION_METHODS, model_resource_name


def gemini_model_entry(
    model_id: str,
    *,
    display_name: str,
    input_token_limit: int | None = None,
    output_token_limit: int | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Return one Google ``Model`` resource for a routable MCC model."""

    entry: dict[str, Any] = {
        "name": model_resource_name(model_id),
        "displayName": display_name,
        "supportedGenerationMethods": list(SUPPORTED_GENERATION_METHODS),
    }
    if description:
        entry["description"] = description
    if input_token_limit is not None:
        entry["inputTokenLimit"] = input_token_limit
    if output_token_limit is not None:
        entry["outputTokenLimit"] = output_token_limit
    return entry


def gemini_models_payload(models: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the ``ListModels`` envelope.

    ``nextPageToken`` is omitted rather than sent empty: Google omits it on the
    last page, and a client that pages until the token is absent would loop for
    ever on an empty string.
    """

    return {"models": models}


def gemini_count_tokens_payload(total_tokens: int) -> dict[str, Any]:
    """Return the ``countTokens`` response body."""

    return {"totalTokens": total_tokens}
