"""Fetch what a media answer points at, through the key and leg that answered it.

A provider can answer an image request with a link instead of the picture. MCC
downloads such a picture itself -- with the answering key, through the same
proxy leg, never with the client's own address -- in two places:

* inside the attempt, by the leaf, for a request marked ``inline_urls``
  (``MEDIA_FALLBACK_ON_UNDOWNLOADABLE`` on: a download that fails falls back);
* after the answer committed, here, when that setting is off: a download that
  fails is the client's error, never a second picture billed elsewhere.

The second path served only the Gemini image door until 7.99.0, which also
lets an OpenAI image door use it for a request carrying
``MCP_MEDIA_INLINE_IMAGES_HEADER`` -- ``mcc-mcp`` sends it, so the media server
never fetches a provider's link itself (full IP masking). The helpers moved
here unchanged from ``gemini_media_routes`` and ``media_video_routes`` so all
three route modules can share them without importing one another in a circle.
"""

import asyncio
import dataclasses

from my_claude_code.application.media.ports import MediaJobClient, MediaRuntimePort
from my_claude_code.application.media.request import MediaResponse
from my_claude_code.config.settings import Settings
from my_claude_code.core.credential_attribution import current_credential
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.openai_images import inline_image_urls, url_only_images

from .media_capture import MediaCapture


def answering_key(
    media: MediaRuntimePort, settings: Settings, provider_id: str
) -> tuple[int | None, str | None, str | None]:
    """The key that answered this request: ``(index, label, fingerprint)``.

    Read from the credential attribution the capture installed; the
    fingerprint (never the key) is what finds the key again after a reorder.
    """
    key_index, key_label = current_credential()
    fingerprint = (
        media.key_fingerprint(settings, provider_id, key_index)
        if key_index is not None and key_index >= 0
        else None
    )
    return key_index, key_label, fingerprint


def answer_client(
    media: MediaRuntimePort, settings: Settings, capture: MediaCapture
) -> tuple[MediaJobClient, str]:
    """The key and leg that answered, to fetch what its answer points at."""
    routed = capture.routed
    if routed is None:
        raise ExecutionFailure(
            kind=FailureKind.UPSTREAM,
            status_code=502,
            message="The image answer named URLs, and no provider answered it.",
            retryable=False,
        )
    provider_id = routed.resolved.provider_id
    key_index, _label, fingerprint = answering_key(media, settings, provider_id)
    client = media.job_client(
        settings,
        provider_id,
        key_fingerprint=fingerprint,
        key_index=key_index,
        proxy_label=capture.proxy_label,
    )
    if client is None:
        raise ExecutionFailure(
            kind=FailureKind.UPSTREAM,
            status_code=502,
            message=(
                f"{provider_id} answered with image URLs, and the key that asked "
                "is no longer configured to fetch them."
            ),
            retryable=False,
        )
    return client, routed.resolved.provider_model


async def fetch_answer_file(
    client: MediaJobClient, url: str, *, model: str, request_id: str
) -> tuple[bytes, str]:
    """One URL-only image, downloaded whole: its bytes and the host's type.

    Through the answering key's own leaf, one call, never retried elsewhere;
    a key goes only to the provider's own host (a CDN URL is fetched bare).
    """
    download = await client.download(
        operation=None, upstream_id="", url=url, model=model, request_id=request_id
    )
    data = bytearray()
    try:
        async for chunk in download.chunks:
            data.extend(chunk)
    finally:
        await download.close()
    return bytes(data), download.content_type


async def inline_url_pictures(
    media: MediaRuntimePort,
    settings: Settings,
    capture: MediaCapture,
    answer: MediaResponse,
) -> MediaResponse:
    """An images answer with every URL-only picture handed over as ``b64_json``.

    After the commit, with the same download the Gemini image door uses; an
    answer that already carries its bytes comes back unchanged.
    """
    urls = await asyncio.to_thread(url_only_images, answer.body)
    if not urls:
        return answer
    client, model = answer_client(media, settings, capture)
    fetched: dict[int, bytes] = {}
    for position, url in urls:
        data, _content_type = await fetch_answer_file(
            client, url, model=model, request_id=capture.request_id
        )
        fetched[position] = data
    body = await asyncio.to_thread(inline_image_urls, answer.body, fetched)
    return dataclasses.replace(answer, body=body)
