"""What the media executor needs from a provider, and from the runtime.

The chat executor reaches providers through ``ProviderPort``; media gets its own
port because its unit of work is not a ``MessagesRequest``. The concrete media
stack (key pool, proxy pool, leaf) lives in ``providers/media`` and is wired in
by the runtime, since ``api`` may not import ``providers``.
"""

from collections.abc import AsyncIterator, Callable, Mapping
from typing import Protocol, runtime_checkable

from my_claude_code.config.settings import Settings

from .request import (
    MediaAttempt,
    MediaChunk,
    MediaDownload,
    MediaRequest,
    MediaResponse,
)


class MediaProviderPort(Protocol):
    """One provider's media side: its own keys, proxies and health books."""

    @property
    def credential_label(self) -> str | None: ...

    def throttle_remaining(self, model: str | None = None) -> float: ...

    def supports(self, request: MediaRequest) -> bool:
        """Whether this provider declares a surface able to serve ``request``.

        A ``False`` skips the candidate without charging it anything: a
        provider that never offered an endpoint did not fail at it.
        """
        ...

    def preflight(self, attempt: MediaAttempt) -> None: ...

    def execute(
        self, attempt: MediaAttempt, *, request_id: str | None = None
    ) -> AsyncIterator[MediaChunk]: ...


@runtime_checkable
class PooledMediaPort(Protocol):
    """A media provider over two or more keys (the media copy of the pool)."""

    async def escalate_model_bench_to_key(
        self, key_index: int, model: str, retry_after: float | None
    ) -> bool: ...

    def key_throttle_remaining(self, key_index: int) -> float:
        """Seconds this key's own limiter is blocked for (0 when free)."""
        ...


MediaProviderResolver = Callable[[str], MediaProviderPort]


class MediaJobClient(Protocol):
    """Calls on one accepted job, pinned to the provider, key and leg that took it.

    One upstream call per method call, through that key's own client and
    limiter; a failure is charged to that key and that model the way an
    attempt's is, and nothing ever rotates or falls back -- the job exists
    only where it was accepted.
    """

    async def call(
        self,
        operation: str,
        upstream_id: str,
        *,
        model: str,
        request_id: str,
        query: Mapping[str, str] | None = None,
    ) -> MediaResponse:
        """A buffered call on the job (retrieve, delete)."""
        ...

    async def download(
        self,
        *,
        operation: str | None,
        upstream_id: str,
        url: str | None,
        model: str,
        request_id: str,
        query: Mapping[str, str] | None = None,
    ) -> MediaDownload:
        """Open the finished file: the declared ``operation``'s path, else ``url``."""
        ...


class MediaRuntimePort(Protocol):
    """The process-wide owner of every provider's media stack."""

    def resolver(self, settings: Settings) -> MediaProviderResolver: ...

    def job_client(
        self,
        settings: Settings,
        provider_id: str,
        *,
        key_fingerprint: str | None,
        key_index: int | None,
        proxy_label: str | None,
    ) -> MediaJobClient | None:
        """The pinned client for a job, or ``None`` when its key is gone."""
        ...

    def key_fingerprint(
        self, settings: Settings, provider_id: str, key_index: int | None
    ) -> str | None:
        """The stable id of the provider's key at ``key_index`` (never the key)."""
        ...

    async def close(self) -> None: ...
