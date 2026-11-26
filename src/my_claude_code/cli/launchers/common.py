"""Shared process helpers for installed client CLI launchers."""

import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TypedDict
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from my_claude_code.cli.process_registry import (
    kill_pid_tree_best_effort,
    register_pid,
    unregister_pid,
)
from my_claude_code.config.harnesses import (
    HarnessProtocol,
    HarnessSpec,
    normalize_inbound_protocol,
)
from my_claude_code.config.model_overrides import current_model_overrides

PROXY_PREFLIGHT_PATH = "/health"
PROXY_PREFLIGHT_TIMEOUT_SECONDS = 1.5
INBOUND_PROTOCOL_FLAG = "--inbound-protocol"
INBOUND_PROTOCOL_ENV = "MCC_INBOUND_PROTOCOL"
RC_FLAG = "--rc"


def strip_rc_flag(argv: Sequence[str]) -> tuple[bool, list[str]]:
    """Extract ``--rc`` strictly before any bare ``--`` separator."""

    args = list(argv)
    try:
        separator_index = args.index("--")
    except ValueError:
        separator_index = len(args)

    leading = args[:separator_index]
    trailing = args[separator_index:]
    rc_enabled = RC_FLAG in leading
    clean = [arg for arg in leading if arg != RC_FLAG] + trailing
    return rc_enabled, clean


@dataclass(frozen=True, slots=True)
class InboundProtocolOverride:
    """Explicit per-launch inbound protocol request (``None`` means use default)."""

    protocol: HarnessProtocol | None


def _invalid_inbound_protocol(spec: HarnessSpec, raw: str) -> SystemExit:
    valid = ", ".join(str(p) for p in spec.launchable_inbound_protocols())
    print(
        f"My Claude Code: invalid {INBOUND_PROTOCOL_FLAG} value {raw!r} for "
        f"{spec.display_name}. Supported: {valid} (or 'default').",
        file=sys.stderr,
    )
    return SystemExit(2)


def strip_inbound_protocol_flag(
    spec: HarnessSpec, argv: Sequence[str]
) -> tuple[InboundProtocolOverride | None, list[str]]:
    """Extract and validate ``--inbound-protocol`` before any ``--`` separator."""

    args = list(argv)
    try:
        separator_index = args.index("--")
    except ValueError:
        separator_index = len(args)

    leading = args[:separator_index]
    trailing = args[separator_index:]
    cleaned: list[str] = []
    raw_value: str | None = None
    index = 0
    while index < len(leading):
        arg = leading[index]
        if arg == INBOUND_PROTOCOL_FLAG:
            if index + 1 >= len(leading):
                print(
                    f"My Claude Code: {INBOUND_PROTOCOL_FLAG} requires a value.",
                    file=sys.stderr,
                )
                raise SystemExit(2)
            raw_value = leading[index + 1]
            index += 2
            continue
        if arg.startswith(f"{INBOUND_PROTOCOL_FLAG}="):
            raw_value = arg.split("=", 1)[1]
            if not raw_value.strip():
                print(
                    f"My Claude Code: {INBOUND_PROTOCOL_FLAG} requires a value.",
                    file=sys.stderr,
                )
                raise SystemExit(2)
            index += 1
            continue
        cleaned.append(arg)
        index += 1

    cleaned.extend(trailing)
    if raw_value is None:
        return None, cleaned

    try:
        parsed = normalize_inbound_protocol(raw_value)
    except ValueError:
        raise _invalid_inbound_protocol(spec, raw_value) from None
    if parsed is not None and parsed not in spec.launchable_inbound_protocols():
        raise _invalid_inbound_protocol(spec, raw_value)
    return InboundProtocolOverride(protocol=parsed), cleaned


def resolve_launch_inbound_protocol(
    spec: HarnessSpec,
    flag_override: InboundProtocolOverride | None,
    env: Mapping[str, str],
) -> HarnessProtocol | None:
    """Resolve active inbound protocol override: CLI flag > env var > model_overrides."""

    if flag_override is not None:
        return (
            flag_override.protocol
            if flag_override.protocol is not None
            else spec.protocol
        )

    raw_env = env.get(INBOUND_PROTOCOL_ENV, "").strip()
    if raw_env:
        try:
            parsed = normalize_inbound_protocol(raw_env)
        except ValueError:
            raise _invalid_inbound_protocol(spec, raw_env) from None
        if parsed is None:
            return spec.protocol
        if parsed not in spec.launchable_inbound_protocols():
            raise _invalid_inbound_protocol(spec, raw_env)
        return parsed

    return current_model_overrides().harness_protocol(spec.id)


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


class RcLaunchKwargs(TypedDict, total=False):
    """Optional keyword arguments enabling ``--rc`` dual-sync on a launcher."""

    rc_enabled: bool
    rc_harness_id: str
    rc_proxy_root_url: str


def rc_launch_kwargs(
    rc_enabled: bool,
    *,
    harness_id: str,
    proxy_root_url: str,
) -> RcLaunchKwargs:
    """Return ``run_client_process`` RC kwargs only when ``--rc`` was requested."""

    if not rc_enabled:
        return {}
    return {
        "rc_enabled": True,
        "rc_harness_id": harness_id,
        "rc_proxy_root_url": proxy_root_url,
    }


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
    rc_enabled: bool = False,
    rc_harness_id: str | None = None,
    rc_cwd: str | None = None,
    rc_proxy_root_url: str | None = None,
) -> None:
    """Run a client CLI command and mirror its exit code."""

    if rc_enabled:
        from .rc_common import run_rc_dual_sync_process

        run_rc_dual_sync_process(
            command=command,
            env=env,
            binary_name=binary_name,
            display_name=display_name,
            install_hint=install_hint,
            harness_id=rc_harness_id or "terminal",
            cwd=rc_cwd,
            proxy_root_url=rc_proxy_root_url,
        )
        return

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
