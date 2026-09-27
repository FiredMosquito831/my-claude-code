"""The request-log row for one media request.

``api/request_capture.py`` describes a ``MessagesRequest`` and cannot be reused
as-is. This is the media twin: the same ``requests`` row and the same
``request_attempts`` rows (the chain's own account of itself, from the media
executor's ledger), plus the media columns -- the operation, how many images
came back, their size and the first one's content address -- and, only when
``MEDIA_STORE_ENABLED`` is on, the files themselves.

Hashing and file writes happen in ``asyncio.to_thread``; the row goes to the
request log's own writer thread.
"""

import asyncio
import time
from collections.abc import Mapping
from typing import Any, Literal

from loguru import logger

from my_claude_code.application.execution import RouteAttemptRecord
from my_claude_code.application.media.request import (
    MediaAttempt,
    MediaPlan,
    MediaRequest,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.client_fingerprint import (
    harness_from_headers,
    install_fingerprint,
)
from my_claude_code.core.credential_attribution import install_attribution
from my_claude_code.core.diagnostics import safe_exception_message
from my_claude_code.core.failures import failure_kind_name
from my_claude_code.core.media_outputs import MediaOutputs
from my_claude_code.core.media_store import (
    MediaOutputRecord,
    media_root,
    write_media_file,
)
from my_claude_code.core.proxy_attribution import install_proxy_attribution
from my_claude_code.core.request_headers import capture_headers
from my_claude_code.core.request_images import CapturedImage, capture_upload
from my_claude_code.core.request_log import (
    RequestRecord,
    RouteAttempt,
    RouteAttemptOutcome,
    store_from_settings,
)

#: The ``protocol`` a media row is logged under when the client spoke OpenAI.
MEDIA_PROTOCOL_OPENAI = "openai_images"


def _usage_int(usage: Mapping[str, Any] | None, key: str) -> int | None:
    if usage is None:
        return None
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class MediaCapture:
    """Collects one media request's story and writes it once, at the end."""

    def __init__(
        self,
        settings: Settings,
        *,
        request_id: str,
        endpoint: str,
        request: MediaRequest,
        headers: Mapping[str, str] | None,
        protocol: str = MEDIA_PROTOCOL_OPENAI,
    ) -> None:
        self._store = store_from_settings(settings)
        self._protocol = protocol
        self._store_bytes = bool(getattr(settings, "media_store_enabled", False))
        # Uploaded inputs: metadata always; a thumbnail only when media
        # storage is on (user decision 7) and image thumbnails are too.
        self._thumb_pixels = (
            int(getattr(settings, "request_log_image_max_pixels", 0) or 0)
            if self._store_bytes
            and bool(getattr(settings, "request_log_capture_images", True))
            else 0
        )
        self._request_id = request_id
        self._endpoint = endpoint
        self._request = request
        self._ts = time.time()
        self._started = time.monotonic()
        self._attempts: list[RouteAttempt] = []
        self._routed: MediaAttempt | None = None
        self._route_index: int | None = None
        self._plan: MediaPlan | None = None
        install_fingerprint(headers)
        self._harness = harness_from_headers(headers).harness
        self._headers = capture_headers(headers)
        self._credential = install_attribution()
        self._proxy = install_proxy_attribution()
        self._job_id: str | None = None
        self._finished = False

    @property
    def enabled(self) -> bool:
        return self._store is not None

    @property
    def request_id(self) -> str:
        return self._request_id

    @property
    def routed(self) -> MediaAttempt | None:
        """The attempt the chain last announced: the one that answered, on success."""
        return self._routed

    @property
    def proxy_label(self) -> str | None:
        """The egress address the last dial of this request went out through."""
        return self._proxy.label

    def set_plan(self, plan: MediaPlan) -> None:
        self._plan = plan

    def set_job(self, job_id: str) -> None:
        """The video job this request's answer created (``media_jobs``)."""
        self._job_id = job_id

    def on_attempt(self, attempt: MediaAttempt, index: int) -> None:
        """The model the chain is about to try; the last one names the row."""
        self._routed = attempt
        self._route_index = index

    def record_attempt_result(self, attempt: RouteAttemptRecord) -> None:
        if not self.enabled:
            return
        self._attempts.append(
            RouteAttempt(
                attempt=attempt.attempt,
                provider=attempt.provider_id or None,
                model_ref=attempt.model_ref or None,
                outcome=RouteAttemptOutcome(attempt.outcome),
                error_kind=attempt.error_kind,
                error_message=attempt.error_message,
                duration_ms=attempt.duration_ms,
                params=None if attempt.bench is None else {"bench": attempt.bench},
                ttft_ms=attempt.ttft_ms,
            )
        )

    def _params(self, outputs: MediaOutputs | None) -> dict[str, Any]:
        params = {
            str(key): value
            for key, value in self._request.body.items()
            if key not in {"prompt", "input", "stream"}
        }
        media: dict[str, Any] = {"operation": self._request.operation}
        if self._request.uploads:
            media["uploads"] = [
                {
                    "field": upload.field,
                    "filename": upload.filename,
                    "content_type": upload.content_type,
                    "bytes": upload.size,
                    "sha256": upload.sha256,
                }
                for upload in self._request.uploads
            ]
        not_forwarded = list(self._request.not_forwarded)
        if outputs is not None:
            not_forwarded.extend(
                name for name in outputs.not_forwarded if name not in not_forwarded
            )
        if not_forwarded:
            media["not_forwarded"] = not_forwarded
        params["media"] = media
        return params

    def _input_audio_seconds(self, outputs: MediaOutputs | None) -> float | None:
        """What the host says it heard, else what a WAV upload's header says."""
        if outputs is not None and outputs.input_audio_seconds is not None:
            return outputs.input_audio_seconds
        stated = [
            upload.audio_seconds
            for upload in self._request.uploads
            if upload.audio_seconds is not None
        ]
        return sum(stated) if stated else None

    def _input_records(self) -> tuple[MediaOutputRecord, ...]:
        return tuple(
            MediaOutputRecord(
                sha256=upload.sha256,
                mime=upload.content_type or None,
                bytes=upload.size,
                stored=False,
                idx=position,
                direction="in",
            )
            for position, upload in enumerate(self._request.uploads)
        )

    def _input_thumbnails(self) -> tuple[CapturedImage, ...]:
        """Runs in a worker thread: thumbnail each uploaded image."""
        captured: list[CapturedImage] = []
        for upload in self._request.uploads:
            if not (upload.content_type or "").startswith("image/"):
                continue
            upload.file.seek(0)
            captured.append(
                capture_upload(
                    upload.file.read(),
                    sha256=upload.sha256,
                    media_type=upload.content_type,
                    max_pixels=self._thumb_pixels,
                )
            )
        return tuple(captured)

    def _write_files(self, outputs: MediaOutputs) -> tuple[MediaOutputRecord, ...]:
        """Runs in a worker thread: store each image when the store is on."""
        root = None if self._store is None else media_root(self._store.db_path)
        records: list[MediaOutputRecord] = []
        for position, image in enumerate(outputs.items):
            stored = False
            if self._store_bytes and root is not None:
                try:
                    stored = write_media_file(
                        root, image.sha256, image.mime, image.data
                    )
                except OSError as exc:
                    logger.warning(
                        "MEDIA STORE: could not write {}: {}", image.sha256[:12], exc
                    )
            records.append(
                MediaOutputRecord(
                    sha256=image.sha256,
                    mime=image.mime,
                    bytes=len(image.data),
                    stored=stored,
                    idx=position,
                )
            )
        return tuple(records)

    async def finish(
        self,
        status: Literal["success", "error", "cancelled"],
        *,
        error: BaseException | None = None,
        outputs: MediaOutputs | None = None,
    ) -> None:
        """Write the row. Idempotent: the first call wins."""
        if self._finished or self._store is None:
            return
        self._finished = True
        media_outputs: tuple[MediaOutputRecord, ...] = self._input_records()
        if outputs is not None and outputs.items:
            media_outputs += await asyncio.to_thread(self._write_files, outputs)
        thumbnails: tuple[CapturedImage, ...] = ()
        if self._thumb_pixels > 0 and self._request.uploads:
            thumbnails = await asyncio.to_thread(self._input_thumbnails)
        image_inputs = sum(
            1
            for upload in self._request.uploads
            if (upload.content_type or "").startswith("image/")
        )
        routed = self._routed
        prompt = self._request.prompt
        usage = None if outputs is None else outputs.usage
        refs = () if self._plan is None else self._plan.model_refs()
        record = RequestRecord(
            id=self._request_id,
            endpoint=self._endpoint,
            protocol=self._protocol,
            ts_epoch=self._ts,
            requested_model=self._request.model or None,
            provider=None if routed is None else routed.resolved.provider_id,
            resolved_model=None if routed is None else routed.resolved.provider_model,
            route_attempt=self._route_index,
            route_primary_model=refs[0] if refs else None,
            route_chain=",".join(refs) if len(refs) > 1 else None,
            stream=self._request.stream,
            input_text=prompt,
            input_chars=None if prompt is None else len(prompt),
            params=self._params(outputs),
            tokens_in=_usage_int(usage, "input_tokens"),
            tokens_out=_usage_int(usage, "output_tokens"),
            duration_ms=(time.monotonic() - self._started) * 1000.0,
            status=status,
            error_kind=None if error is None else failure_kind_name(error),
            error_message=None if error is None else safe_exception_message(error),
            headers=self._headers,
            key_index=self._credential.index,
            key_label=self._credential.label,
            harness=self._harness,
            attempts=tuple(self._attempts),
            media_operation=self._request.operation,
            input_image_count=image_inputs if self._request.uploads else None,
            images=thumbnails,
            output_image_count=(
                None
                if outputs is None or not self._request.operation.startswith("image")
                else outputs.count
            ),
            output_audio_seconds=None if outputs is None else outputs.audio_seconds,
            input_audio_seconds=self._input_audio_seconds(outputs),
            media_job_id=self._job_id,
            output_text=None if outputs is None else outputs.text,
            output_chars=(
                None if outputs is None or outputs.text is None else len(outputs.text)
            ),
            media_bytes_out=None if outputs is None else outputs.bytes_total,
            media_sha_out=None if outputs is None else outputs.first_sha,
            media_outputs=media_outputs,
        )
        self._store.enqueue(record)
