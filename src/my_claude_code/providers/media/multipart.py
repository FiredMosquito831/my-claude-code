"""A multipart/form-data body streamed from spooled uploads, off the event loop.

httpx's own multipart encoder reads file objects synchronously while it sends,
which on the event loop is a blocking disk read per chunk of every uploaded
image. This encoder yields the same wire format from an async generator whose
every file read goes through ``asyncio.to_thread``, and computes the exact
``Content-Length`` up front so hosts that refuse chunked uploads accept it.

Each call of :meth:`MultipartBody.stream` starts from the beginning of every
file, so a retried or fallen-back attempt sends the same bytes again.
"""

import asyncio
import secrets
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from my_claude_code.application.media.request import MediaUpload

#: Bytes read from a spooled upload per worker-thread hop.
READ_CHUNK_BYTES = 256 * 1024


def _quote(value: str) -> str:
    """Escape a header parameter the way browsers and httpx do."""

    return (
        value.replace("\\", "\\\\")
        .replace('"', "%22")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )


@dataclass(frozen=True, slots=True)
class MultipartBody:
    fields: tuple[tuple[str, str], ...]
    uploads: tuple[MediaUpload, ...]
    boundary: str

    @classmethod
    def build(
        cls, fields: Sequence[tuple[str, str]], uploads: Sequence[MediaUpload]
    ) -> MultipartBody:
        return cls(tuple(fields), tuple(uploads), secrets.token_hex(16))

    @property
    def content_type(self) -> str:
        return f"multipart/form-data; boundary={self.boundary}"

    def _field_head(self, name: str) -> bytes:
        return (
            f"--{self.boundary}\r\n"
            f'Content-Disposition: form-data; name="{_quote(name)}"\r\n\r\n'
        ).encode()

    def _file_head(self, upload: MediaUpload) -> bytes:
        return (
            f"--{self.boundary}\r\n"
            f'Content-Disposition: form-data; name="{_quote(upload.field)}"; '
            f'filename="{_quote(upload.filename)}"\r\n'
            f"Content-Type: {upload.content_type or 'application/octet-stream'}\r\n\r\n"
        ).encode()

    def _tail(self) -> bytes:
        return f"--{self.boundary}--\r\n".encode()

    @property
    def content_length(self) -> int:
        total = len(self._tail())
        for name, value in self.fields:
            total += len(self._field_head(name)) + len(value.encode()) + 2
        for upload in self.uploads:
            total += len(self._file_head(upload)) + upload.size + 2
        return total

    async def stream(self) -> AsyncIterator[bytes]:
        for name, value in self.fields:
            yield self._field_head(name) + value.encode() + b"\r\n"
        for upload in self.uploads:
            yield self._file_head(upload)
            await asyncio.to_thread(upload.file.seek, 0)
            while chunk := await asyncio.to_thread(upload.file.read, READ_CHUNK_BYTES):
                yield chunk
            yield b"\r\n"
        yield self._tail()
