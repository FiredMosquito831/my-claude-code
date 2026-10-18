"""The loopback client: ``mcc-mcp`` talks to MCC, and to nothing else (7.99.0).

**Where MCC is, and its token, come from MCC's own settings** (decision 1 of
2026-10-04; spec §3.5) -- never from the app's file, a prompt or the agent's
shell. The address is ``HOST``/``PORT`` and the token ``ANTHROPIC_AUTH_TOKEN``,
read the way every ``mcc-*`` launcher reads them, with one deliberate
difference (risk R14): a ``PORT`` or ``HOST`` in MCC's ``.env`` wins over the
same name in this process's environment, exactly as MCC already lets the
``.env`` token win. The server MCC's installer and desktop app start reads its
port from the ``.env`` (the desktop shell passes no ``PORT``), while an app
that starts an MCP server may hand it a ``PORT`` of its own -- a web project's
dev-server port is the common one -- and that must never redirect a media call.
``media_status`` names the address used and where it came from.

**Loopback only, and never through a proxy** (risk R8). The transport is built
explicitly, so httpx reads no ``*_PROXY`` variable for it; a request is a path
on MCC's root, and an absolute URL is refused before anything is sent. A
picture the provider answered with only a link is downloaded by MCC itself,
through the provider's own client (``MCP_MEDIA_INLINE_IMAGES_HEADER``), so the
user's own address never reaches a provider's file host from here.

The token travels only in the ``Authorization`` header of a loopback request.
It is never put in a tool result, a log line or an error message.
"""

import importlib.metadata
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from my_claude_code.config.env_files import env_file_override
from my_claude_code.config.paths import config_dir_path
from my_claude_code.config.server_urls import local_proxy_root_url
from my_claude_code.config.settings import Settings, get_settings
from my_claude_code.core.client_fingerprint import HARNESS_HEADER

#: ``GET`` route that lists every media rail model and the fields it takes.
#: Spelled here because ``api`` is out of this package's reach; a test pins
#: it to ``api.wire_surfaces.MEDIA_MODELS_ENDPOINT``.
MEDIA_CATALOGUE_PATH = "/v1/media/models"
#: MCC's liveness route (no token needed).
HEALTH_PATH = "/health"

#: The bucket an unnamed caller's requests are logged under.
DEFAULT_APP_ID = "mcc_mcp"

#: One multipart part as httpx takes it: ``(field, (file name, bytes, type))``;
#: a part with no file name is a plain form field.
FilePart = tuple[str, tuple[str | None, bytes, str | None]]


def mcc_version() -> str:
    """The installed MCC's version, for the user-agent and ``serverInfo``."""

    try:
        return importlib.metadata.version("my-claude-code")
    except importlib.metadata.PackageNotFoundError:
        return "0"


@dataclass(frozen=True, slots=True)
class MccAddress:
    """Where MCC is, as this process resolved it, and how."""

    root: str
    #: ``.env``, ``environment`` or ``default`` -- shown by ``media_status``.
    port_source: str
    config_dir: str
    token_set: bool


def _source(settings: Settings, key: str, environ: Mapping[str, str]) -> str:
    if env_file_override(settings.model_config, key) is not None:
        return ".env"
    if key in environ:
        return "environment"
    return "default"


def resolve_address(
    environ: Mapping[str, str], settings: Settings | None = None
) -> tuple[MccAddress, str]:
    """MCC's root URL and token, from MCC's own settings (R14: ``.env`` first).

    Returns the address record and the token. The token is returned on its
    own, never inside the record, so nothing that prints the record can
    print it.
    """

    resolved = settings if settings is not None else get_settings()
    updates: dict[str, Any] = {}
    port_text = env_file_override(resolved.model_config, "PORT")
    if port_text is not None and port_text.strip().isdigit():
        updates["port"] = int(port_text.strip())
    host_text = env_file_override(resolved.model_config, "HOST")
    if host_text is not None and host_text.strip():
        updates["host"] = host_text.strip()
    effective = resolved.model_copy(update=updates) if updates else resolved
    token = (resolved.anthropic_auth_token or "").strip()
    address = MccAddress(
        root=local_proxy_root_url(effective),
        port_source=_source(resolved, "PORT", environ),
        config_dir=str(config_dir_path()),
        token_set=bool(token),
    )
    return address, token


class MccNotAnswering(Exception):
    """MCC refused the connection, or it broke off before the answer was read.

    ``connected`` tells the two apart: False is "nothing is listening there"
    (start MCC), True is a connection that broke mid-answer -- MCC may have
    finished the work, and the request log is where to look.
    """

    def __init__(self, root: str, reason: str, *, connected: bool = False) -> None:
        what = (
            "broke off the connection before answering"
            if connected
            else "is not answering"
        )
        super().__init__(f"MCC {what} at {root} ({reason})")
        self.root = root
        self.reason = reason
        self.connected = connected


@dataclass(frozen=True, slots=True)
class MccResponse:
    status: int
    content_type: str
    content: bytes

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))


class MccClient:
    """Requests to one MCC, on loopback, as one app."""

    def __init__(
        self,
        address: MccAddress,
        token: str,
        *,
        app_id: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.address = address
        headers = {
            HARNESS_HEADER: app_id,
            "user-agent": f"mcc-mcp/{mcc_version()}",
        }
        if token:
            headers["authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url=address.root,
            headers=headers,
            # An explicit transport: httpx then reads no proxy variable for
            # this client (risk R8, loopback only).
            transport=transport
            if transport is not None
            else httpx.AsyncHTTPTransport(),
            # MCC's own deadlines are the user's settings (0 = none); this
            # program adds no limit of its own (IV.1, risk R6).
            timeout=httpx.Timeout(None),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        data: Mapping[str, str] | None = None,
        files: Sequence[FilePart] | None = None,
        headers: Mapping[str, str] | None = None,
        multipart: bool = False,
    ) -> MccResponse:
        """One request to a path on MCC's root; an absolute URL is refused.

        ``multipart`` sends ``data`` as ``multipart/form-data`` even with no
        file: httpx sends a file-less form urlencoded, which a media door does
        not read, and a field with no file name is how it is told otherwise.
        """

        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("mcc-mcp sends requests to MCC's own paths only")
        parts: list[FilePart] = list(files or ())
        form = dict(data) if data is not None else None
        if multipart and not parts and form:
            parts = [
                (name, (None, value.encode("utf-8"), None))
                for name, value in form.items()
            ]
            form = None
        try:
            response = await self._client.request(
                method,
                path,
                json=json_body,
                data=form,
                files=parts or None,
                headers=dict(headers) if headers else None,
            )
        except httpx.TransportError as exc:
            # The type's name only: a transport error's text can carry more
            # than an address.
            raise MccNotAnswering(
                self.address.root,
                type(exc).__name__,
                connected=not isinstance(exc, httpx.ConnectError),
            ) from exc
        return MccResponse(
            status=response.status_code,
            content_type=response.headers.get("content-type", ""),
            content=response.content,
        )


def error_message(response: MccResponse) -> str:
    """MCC's own words for a refused request (its OpenAI error envelope)."""

    try:
        payload = response.json()
    except ValueError, UnicodeDecodeError:
        payload = None
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping) and error.get("message"):
            return str(error["message"])
        if payload.get("detail"):
            return str(payload["detail"])
    text = response.content.decode("utf-8", errors="replace").strip()
    return text[:500] if text else f"HTTP {response.status}"
