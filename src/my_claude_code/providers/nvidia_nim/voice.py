"""NVIDIA NIM / Riva offline ASR for voice notes and media rail (provider-owned transport)."""

import asyncio
import json
from pathlib import Path
from typing import Any

from loguru import logger

from my_claude_code.core.failures import ExecutionFailure, FailureKind

# NVIDIA NIM Whisper model mapping: (function_id, language_code)
_NIM_ASR_MODEL_MAP: dict[str, tuple[str, str]] = {
    "nvidia/parakeet-ctc-0.6b-zh-tw": ("8473f56d-51ef-473c-bb26-efd4f5def2bf", "zh-TW"),
    "nvidia/parakeet-ctc-0.6b-zh-cn": ("9add5ef7-322e-47e0-ad7a-5653fb8d259b", "zh-CN"),
    # function-id from NVIDIA NIM API docs (parakeet-ctc-0.6b-es).
    "nvidia/parakeet-ctc-0.6b-es": ("a9eeee8f-b509-4712-b19d-194361fa5f31", "es-US"),
    "nvidia/parakeet-ctc-0.6b-vi": ("f3dff2bb-99f9-403d-a5f1-f574a757deb0", "vi-VN"),
    "nvidia/parakeet-ctc-1.1b-asr": ("1598d209-5e27-4d3c-8079-4751568b1081", "en-US"),
    "nvidia/parakeet-ctc-0.6b-asr": ("d8dd4e9b-fbf5-4fb0-9dba-8cf436c8d965", "en-US"),
    "nvidia/parakeet-1.1b-rnnt-multilingual-asr": (
        "71203149-d3b7-4460-8231-1be2543a1fca",
        "",
    ),
    "openai/whisper-large-v3": ("b702f636-f60c-4a3d-a6f4-f3568c13bd7d", "multi"),
}

_RIVA_SERVER = "grpc.nvcf.nvidia.com:443"


def _import_riva_client(riva_client_module: Any = None) -> Any:
    if riva_client_module is not None:
        return riva_client_module
    import riva.client  # type: ignore[import-not-found]

    return riva.client


def _run_riva_recognize(
    data: bytes,
    *,
    function_id: str,
    language_code: str,
    api_key: str,
    riva_client: Any,
) -> str:
    auth = riva_client.Auth(
        use_ssl=True,
        uri=_RIVA_SERVER,
        metadata_args=[
            ["function-id", function_id],
            ["authorization", f"Bearer {api_key}"],
        ],
    )
    try:
        asr_service = riva_client.ASRService(auth)
        config = riva_client.RecognitionConfig(
            language_code=language_code,
            max_alternatives=1,
            verbatim_transcripts=True,
        )
        response = asr_service.offline_recognize(data, config)
        transcript = ""
        results = getattr(response, "results", None)
        if results and results[0].alternatives:
            transcript = results[0].alternatives[0].transcript
        logger.debug("NIM transcription: {} chars", len(transcript))
        return transcript
    finally:
        auth.channel.close()


def transcribe_riva_bytes(
    audio_bytes: bytes,
    *,
    model: str,
    api_key: str,
    language: str | None = None,
    riva_client_module: Any = None,
    response_format: str = "json",
) -> tuple[bytes, str]:
    """Synchronously transcribe raw audio bytes via NVIDIA NIM Riva gRPC.

    Must be invoked off the event loop (e.g. ``asyncio.to_thread``).
    Returns ``(response_body, content_type)`` in OpenAI transcription format.
    """

    cleaned_key = api_key.strip()
    if not cleaned_key:
        raise ExecutionFailure(
            kind=FailureKind.INVALID_REQUEST,
            status_code=400,
            message=(
                "NVIDIA NIM transcription requires a non-empty "
                "nvidia_nim_api_key (configure NVIDIA_NIM_API_KEY)."
            ),
            retryable=False,
        )
    cleaned_model = model.strip()
    if not cleaned_model:
        raise ExecutionFailure(
            kind=FailureKind.INVALID_REQUEST,
            status_code=400,
            message="NVIDIA NIM transcription requires a non-empty model or function id.",
            retryable=False,
        )
    model_config = _NIM_ASR_MODEL_MAP.get(cleaned_model)
    if model_config is not None:
        function_id, default_lang = model_config
    else:
        function_id, default_lang = cleaned_model, ""
    language_code = (
        language.strip()
        if isinstance(language, str) and language.strip()
        else default_lang
    )
    try:
        client_mod = _import_riva_client(riva_client_module)
    except ImportError as exc:
        raise ExecutionFailure(
            kind=FailureKind.UPSTREAM,
            status_code=501,
            message=(
                "NVIDIA NIM Riva gRPC transcription requires the voice extra. "
                "Install with: uv sync --extra voice"
            ),
            retryable=False,
        ) from exc

    transcript = _run_riva_recognize(
        audio_bytes,
        function_id=function_id,
        language_code=language_code,
        api_key=cleaned_key,
        riva_client=client_mod,
    )
    if response_format == "text":
        return transcript.encode("utf-8"), "text/plain; charset=utf-8"
    payload = json.dumps({"text": transcript}, ensure_ascii=False).encode("utf-8")
    return payload, "application/json"


class NvidiaNimTranscriber:
    """Own configured NVIDIA NIM / Riva transcription."""

    def __init__(self, *, model: str, api_key: str) -> None:
        self._model = model
        self._key = api_key.strip()
        self._lock = asyncio.Lock()
        self._closed = False

    async def transcribe(self, file_path: Path) -> str:
        """Transcribe one audio file without blocking the event loop."""
        async with self._lock:
            if self._closed:
                raise RuntimeError("NVIDIA NIM transcriber is closed.")
            worker = asyncio.create_task(
                asyncio.to_thread(self._transcribe_sync, file_path)
            )
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                await _wait_for_thread_exit(worker)
                raise

    async def close(self) -> None:
        """Close this stateless adapter to future work."""
        self._closed = True
        async with self._lock:
            self._key = ""

    def _transcribe_sync(self, file_path: Path) -> str:
        if not self._key:
            raise ValueError(
                "NVIDIA NIM transcription requires a non-empty "
                "nvidia_nim_api_key (configure NVIDIA_NIM_API_KEY)."
            )
        model_config = _NIM_ASR_MODEL_MAP.get(self._model)
        if model_config is None:
            raise ValueError(
                f"No NVIDIA NIM config found for model: {self._model}. "
                f"Supported models: {', '.join(_NIM_ASR_MODEL_MAP)}"
            )
        function_id, language_code = model_config
        try:
            client_mod = _import_riva_client()
        except ImportError as exc:
            raise ImportError(
                "NVIDIA NIM transcription requires the voice extra. "
                "Install with: uv sync --extra voice"
            ) from exc

        data = file_path.read_bytes()
        transcript = _run_riva_recognize(
            data,
            function_id=function_id,
            language_code=language_code,
            api_key=self._key,
            riva_client=client_mod,
        )
        return transcript or "(no speech detected)"


async def _wait_for_thread_exit(worker: asyncio.Task[str]) -> None:
    """Wait through repeated caller cancellation without cancelling thread work."""
    while not worker.done():
        try:
            await asyncio.shield(asyncio.wait((worker,)))
        except asyncio.CancelledError:
            continue
    if not worker.cancelled():
        worker.exception()
