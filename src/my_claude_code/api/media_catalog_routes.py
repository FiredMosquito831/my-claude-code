"""``GET /v1/media/models``: the media rails' models and their request fields.

7.98.0, user decision 1 of 2026-10-16: the request fields each media model
takes are published where a client can read them -- and where O's MCP tools
will -- not only on the Models page. Behind the same proxy token as every
other route, answered like ``/v1/models``, and never a chat list: a media
model stays out of ``/v1/models`` and every coding agent's catalogue.

One entry per model on any media rail, each with a JSON Schema per operation
it serves (``application/media/schemas.py``), and per rail which of its
models accept, need, refuse or say nothing about each field. Publish-only:
nothing a request sends, and no route it takes, reads any of it.
"""

import asyncio
from typing import Any

from fastapi import APIRouter, Depends

from my_claude_code.config.media_surfaces import (
    MEDIA_OPERATION_IMAGE_EDIT,
    MEDIA_OPERATION_IMAGE_GENERATE,
    MEDIA_OPERATION_SPEECH,
    MEDIA_OPERATION_TRANSCRIBE,
    MEDIA_OPERATION_TRANSLATE,
    MEDIA_OPERATION_VIDEO_CREATE,
)
from my_claude_code.config.settings import Settings

from .admin_media_routes import media_request_catalogue, request_schema_inputs
from .dependencies import get_services, get_settings, require_proxy_auth
from .ports import ApiServices
from .wire_surfaces import (
    AUDIO_SPEECH_ENDPOINT,
    AUDIO_TRANSCRIPTIONS_ENDPOINT,
    AUDIO_TRANSLATIONS_ENDPOINT,
    IMAGES_EDITS_ENDPOINT,
    IMAGES_GENERATIONS_ENDPOINT,
    MEDIA_MODELS_ENDPOINT,
    VIDEOS_ENDPOINT,
)

router = APIRouter()

#: Where a client sends each routed operation (the OpenAI-shaped doors).
OPERATION_ENDPOINTS: dict[str, str] = {
    MEDIA_OPERATION_IMAGE_GENERATE: IMAGES_GENERATIONS_ENDPOINT,
    MEDIA_OPERATION_IMAGE_EDIT: IMAGES_EDITS_ENDPOINT,
    MEDIA_OPERATION_SPEECH: AUDIO_SPEECH_ENDPOINT,
    MEDIA_OPERATION_TRANSCRIBE: AUDIO_TRANSCRIPTIONS_ENDPOINT,
    MEDIA_OPERATION_TRANSLATE: AUDIO_TRANSLATIONS_ENDPOINT,
    MEDIA_OPERATION_VIDEO_CREATE: VIDEOS_ENDPOINT,
}


@router.get(MEDIA_MODELS_ENDPOINT)
async def list_media_models(
    services: ApiServices = Depends(get_services),
    settings: Settings = Depends(get_settings),
    _auth=Depends(require_proxy_auth),
):
    """Every media rail model, with the request fields it takes per operation."""

    # The learned facts belong to the event loop (their store is written on
    # it); they are copied here and only the copy goes to the worker.
    learned = services.admin.learned_facts_by_model()

    def build() -> dict[str, Any]:
        return media_request_catalogue(
            settings,
            request_schema_inputs(services, learned),
            OPERATION_ENDPOINTS,
        )

    return await asyncio.to_thread(build)
