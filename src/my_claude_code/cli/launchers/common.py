"""Shared process helpers for installed client CLI launchers."""

import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from my_claude_code.cli.process_registry import (
    kill_pid_tree_best_effort,
    register_pid,
    unregister_pid,
)

PROXY_PREFLIGHT_PATH = "/health"
PROXY_PREFLIGHT_TIMEOUT_SECONDS = 1.5


@dataclass(frozen=True, slots=True)
class PreflightResult:
    """What one loopback ``/health`` probe actually saw.

    ``preflight_proxy`` flattens every outcome to a sentence, and a sentence
    cannot be branched on: ``returned HTTP 503`` and ``returned HTTP 500`` read
    the same to a caller, so a server that is deliberately refusing new work
    during its own shutdown was indistinguishable from one that is broken --
    and from a stranger holding the port. This keeps the status code and the
    response headers, so a caller that needs the distinction can have it
    without a second request.

    ``headers`` is lower-cased on the way in: header names are
    case-insensitive on the wire and a caller comparing them should not have
    to know that.
    """

    status_code: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    #: The first kilobyte of the response body, when there was one. Only a
    #: refusal carries anything worth reading here: the startup gate names the
    #: stage it is in, and a window that can say "loading provider catalogues"
    #: instead of "starting" is the difference between a wait and a hang. Bounded
    #: because this is a diagnostic, not a transfer.
    body: str = ""

    @property
    def ok(self) -> bool:
        """Whether a healthy MCC answered."""

        return self.error is None

    def header(self, name: str) -> str | None:
        """Return one response header, matched case-insensitively."""

        return self.headers.get(name.strip().lower())


def _lowercased(pairs: object) -> dict[str, str]:
    """Return response headers keyed by their lower-cased names."""

    items = getattr(pairs, "items", None)
    if items is None:
        return {}
    return {str(key).strip().lower(): str(value) for key, value in items()}


#: Bytes of a refusal body kept for diagnosis. The startup gate's body is a
#: couple of hundred; anything much larger is not this protocol.
_MAX_BODY_BYTES = 1024


def _read_body(response: object) -> str:
    """Read a bounded, best-effort body off a response-like object."""

    read = getattr(response, "read", None)
    if not callable(read):
        return ""
    try:
        raw = read(_MAX_BODY_BYTES)
    except Exception:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def preflight_result(proxy_root_url: str) -> PreflightResult:
    """Probe the local proxy's health endpoint and report what answered.

    Bounded by ``PROXY_PREFLIGHT_TIMEOUT_SECONDS`` and loopback-only, exactly
    as ``preflight_proxy`` is -- this is the same single request, reported in
    full rather than as a sentence.
    """

    url = f"{proxy_root_url.rstrip('/')}{PROXY_PREFLIGHT_PATH}"
    request = Request(url, method="GET")
    try:
        with urlopen(request, timeout=PROXY_PREFLIGHT_TIMEOUT_SECONDS) as response:
            status_code = int(response.getcode())
            headers = _lowercased(response.headers)
    except HTTPError as exc:
        # An HTTPError IS the response: it carries the code and the headers,
        # which is exactly what a 503 during a drain has to be recognised by.
        return PreflightResult(
            status_code=int(exc.code),
            headers=_lowercased(exc.headers),
            error=f"returned HTTP {exc.code}",
            body=_read_body(exc),
        )
    except URLError as exc:
        return PreflightResult(error=str(exc.reason))
    except OSError as exc:
        return PreflightResult(error=str(exc))

    if not 200 <= status_code < 300:
        return PreflightResult(
            status_code=status_code,
            headers=headers,
            error=f"returned HTTP {status_code}",
        )
    return PreflightResult(status_code=status_code, headers=headers)


def preflight_proxy(proxy_root_url: str) -> str | None:
    """Return an error message when the local proxy health check is unreachable."""

    return preflight_result(proxy_root_url).error


def resolve_client_binary(
    *,
    binary_name: str,
    display_name: str,
    install_hint: str,
) -> str:
    """Resolve an installed client binary or exit with a user-facing hint."""

    client_command = shutil.which(binary_name)
    if client_command is None:
        print(
            f"Could not find {display_name} command: {binary_name}",
            file=sys.stderr,
        )
        print(install_hint, file=sys.stderr)
        raise SystemExit(127)
    return client_command


def run_client_process(
    *,
    command: list[str],
    env: Mapping[str, str],
    binary_name: str,
    display_name: str,
    install_hint: str,
) -> None:
    """Run a client CLI command and mirror its exit code."""

    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(command, env=dict(env))
        if process.pid:
            register_pid(process.pid)
        return_code = process.wait()
    except FileNotFoundError:
        print(
            f"Could not find {display_name} command: {binary_name}",
            file=sys.stderr,
        )
        print(install_hint, file=sys.stderr)
        raise SystemExit(127) from None
    except KeyboardInterrupt:
        if process is not None and process.pid:
            kill_pid_tree_best_effort(process.pid)
            process.wait()
        raise
    finally:
        if process is not None and process.pid:
            unregister_pid(process.pid)

    raise SystemExit(return_code)
