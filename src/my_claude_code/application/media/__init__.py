"""Media routing: image, speech, transcription and video requests.

A request path beside chat, not through it. Its attempt loop is a separate
copy of the chat executor's rules (user decision 2026-09-26 03:38 #4), kept
honest by ``tests/contracts/test_media_chat_parity.py``, and it owns its own
key, route and proxy health books so a media failure can never change a chat
decision.
"""

from .endpoint_ladder import (
    EMPTY_MEASURED_MEDIA_ENDPOINTS,
    FACT_MEDIA_ENDPOINT_REFUSED,
    FACT_MEDIA_ENDPOINT_SURFACE,
    MeasuredMediaEndpointEntry,
    MeasuredMediaEndpointSet,
    MediaEndpointCandidate,
    ResolvedMediaEndpointLadder,
    resolve_media_endpoint_ladder,
)

__all__ = [
    "EMPTY_MEASURED_MEDIA_ENDPOINTS",
    "FACT_MEDIA_ENDPOINT_REFUSED",
    "FACT_MEDIA_ENDPOINT_SURFACE",
    "MeasuredMediaEndpointEntry",
    "MeasuredMediaEndpointSet",
    "MediaEndpointCandidate",
    "ResolvedMediaEndpointLadder",
    "resolve_media_endpoint_ladder",
]
