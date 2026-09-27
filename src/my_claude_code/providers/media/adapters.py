"""Translate a canonical media request into one wire-shape family's body.

One function per shape FAMILY, chosen by the provider's declared surface --
never by provider id or model name. Only the OpenAI Images shape ships in
7.60.0; the client already spoke it, so the body is the client's own fields
with ``model`` set to the provider's model id.
"""

from typing import Any

from my_claude_code.application.media.request import MediaAttempt
from my_claude_code.config.media_surfaces import (
    MEDIA_SHAPE_OPENAI_IMAGES,
    MediaSurface,
)


def _openai_images_body(attempt: MediaAttempt) -> dict[str, Any]:
    body = dict(attempt.request.body)
    body["model"] = attempt.resolved.provider_model
    if attempt.request.stream:
        body["stream"] = True
    else:
        body.pop("stream", None)
    return body


def build_request_body(surface: MediaSurface, attempt: MediaAttempt) -> dict[str, Any]:
    """The JSON body for ``attempt`` on ``surface``'s wire shape."""

    if surface.shape == MEDIA_SHAPE_OPENAI_IMAGES:
        return _openai_images_body(attempt)
    raise ValueError(f"unknown media shape {surface.shape!r}")
