"""Tailscale and Cloudflare Tunnel inspection, control, and shutdown cleanup."""

import asyncio
import contextlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from loguru import logger

from my_claude_code.application.remote_ports import RemoteControlError
from my_claude_code.config.server_urls import local_browser_host
from my_claude_code.config.settings import Settings

from .pty_bridge import win_exec_argv

_PROBE_TIMEOUT_SECONDS = 5.0
_QUICK_TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.I)


def _subprocess_flags() -> int:
    if sys.platform == "win32":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    return 0


def resolve_tailscale_binary(configured: str) -> str | None:
    """Resolve the tailscale CLI path from explicit config, PATH, or Windows default."""

    explicit = configured.strip()
    if explicit:
        return explicit if Path(explicit).is_file() else shutil.which(explicit)
    found = shutil.which("tailscale")
    if found:
        return found
    if sys.platform == "win32":
        win_default = Path(r"C:\Program Files\Tailscale\tailscale.exe")
        if win_default.is_file():
            return str(win_default)
    return None


def resolve_cloudflared_binary(configured: str) -> str | None:
    """Resolve the cloudflared CLI path from explicit config or PATH."""

    explicit = configured.strip()
    if explicit:
        return explicit if Path(explicit).is_file() else shutil.which(explicit)
    return shutil.which("cloudflared")


async def _exec_probe(
    argv: list[str], timeout: float = _PROBE_TIMEOUT_SECONDS
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *win_exec_argv(argv),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=_subprocess_flags(),
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        with contextlib.suppress(OSError):
            proc.kill()
        await proc.wait()
        return -1, "", "timed out"
    return (
        int(proc.returncode or 0),
        out_b.decode("utf-8", errors="replace"),
        err_b.decode("utf-8", errors="replace"),
    )


def _normalize_public_url(raw: str, *, suffix: str = "/rc") -> str:
    text = raw.strip().rstrip("/")
    if not text:
        return ""
    if "://" not in text:
        text = f"https://{text}"
    if not text.endswith(suffix):
        text = f"{text}{suffix}"
    return text


class TunnelManager:
    """Supervises Tailscale Serve / Local and Cloudflare Tunnel transports."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._active_mode: str = "off"
        self._cf_proc: asyncio.subprocess.Process | None = None
        self._cf_reader_task: asyncio.Task[None] | None = None
        self._cf_public_url: str = ""
        self._cf_last_error: str = ""
        self._ts_serve_started: bool = False
        self._ts_last_error: str = ""

    def active_mode(self) -> str:
        return self._active_mode

    def _local_rc_url(self, settings: Settings) -> str:
        return f"http://{local_browser_host(settings)}:{settings.port}/rc"

    async def _probe_tailscale(self, settings: Settings) -> dict[str, Any]:
        ts_bin = resolve_tailscale_binary(settings.rc_tailscale_binary)
        override_host = settings.rc_tailscale_hostname.strip().rstrip(".")
        result: dict[str, Any] = {
            "installed": ts_bin is not None,
            "binary_path": ts_bin or "",
            "running": False,
            "backend_state": "",
            "hostname": "",
            "dns_name": "",
            "tailscale_ips": [],
            "local_url": "",
            "serve_url": "",
            "serve_active": self._ts_serve_started,
            "bind_warning": "",
            "error": self._ts_last_error,
        }
        if ts_bin is None:
            return result

        rc, out, err = await _exec_probe([ts_bin, "status", "--json"])
        if rc != 0:
            result["error"] = err.strip() or f"tailscale status exited {rc}"
            return result
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            result["error"] = "tailscale status returned invalid JSON"
            return result

        backend = str(data.get("BackendState") or "")
        self_info = data.get("Self") if isinstance(data.get("Self"), dict) else {}
        dns_name = str(self_info.get("DNSName") or "").rstrip(".")
        hostname = str(self_info.get("HostName") or "")
        ips_raw = data.get("TailscaleIPs") or self_info.get("TailscaleIPs") or []
        ips = [str(ip) for ip in ips_raw if isinstance(ip, str)]
        ipv4 = next((ip for ip in ips if "." in ip), ips[0] if ips else "")

        result["backend_state"] = backend
        result["running"] = backend.lower() == "running"
        result["hostname"] = hostname
        result["dns_name"] = dns_name
        result["tailscale_ips"] = ips

        effective_host = override_host or dns_name
        if effective_host:
            result["serve_url"] = _normalize_public_url(effective_host)
        if ipv4:
            host_part = f"[{ipv4}]" if ":" in ipv4 else ipv4
            result["local_url"] = f"http://{host_part}:{settings.port}/rc"
        if settings.host.strip() in {"127.0.0.1", "localhost", "::1", "[::1]"}:
            result["bind_warning"] = (
                "HOST is loopback-only: tailscale_serve proxies 127.0.0.1 safely, "
                "whereas direct Tailnet IP access (tailscale_local) requires HOST=0.0.0.0."
            )

        rc_srv, srv_out, _ = await _exec_probe([ts_bin, "serve", "status", "--json"])
        if rc_srv == 0 and srv_out.strip():
            try:
                srv_data = json.loads(srv_out)
                if isinstance(srv_data, dict) and (
                    srv_data.get("TCP") or srv_data.get("Web")
                ):
                    result["serve_active"] = True
            except json.JSONDecodeError:
                pass
        return result

    def _probe_cloudflared(self, settings: Settings) -> dict[str, Any]:
        cf_bin = resolve_cloudflared_binary(settings.rc_cloudflare_binary)
        named_configured = bool(settings.rc_cloudflare_tunnel_token.strip())
        running = self._cf_proc is not None and self._cf_proc.returncode is None
        public_url = ""
        if named_configured and settings.rc_cloudflare_public_hostname.strip():
            public_url = _normalize_public_url(
                settings.rc_cloudflare_public_hostname.strip()
            )
        elif self._cf_public_url:
            public_url = _normalize_public_url(self._cf_public_url)
        return {
            "installed": cf_bin is not None,
            "binary_path": cf_bin or "",
            "running": running,
            "mode": "named" if named_configured else "quick",
            "public_url": public_url,
            "error": self._cf_last_error,
            "install_hint": (
                ""
                if cf_bin is not None
                else "cloudflared is not installed on PATH. Install Cloudflare Tunnel "
                "or set RC_CLOUDFLARE_BINARY to its executable path."
            ),
        }

    async def inspect(self, settings: Settings) -> dict[str, Any]:
        ts_info = await self._probe_tailscale(settings)
        cf_info = self._probe_cloudflared(settings)
        local_url = self._local_rc_url(settings)
        recommended_url = local_url

        mode = (
            self._active_mode if self._active_mode != "off" else settings.rc_tunnel_mode
        )
        if mode == "tailscale_serve" and ts_info["serve_url"]:
            recommended_url = ts_info["serve_url"]
        elif mode == "tailscale_local" and ts_info["local_url"]:
            recommended_url = ts_info["local_url"]
        elif mode == "cloudflare" and cf_info["public_url"]:
            recommended_url = cf_info["public_url"]

        return {
            "configured_mode": settings.rc_tunnel_mode,
            "active_mode": self._active_mode,
            "local_url": local_url,
            "recommended_url": recommended_url,
            "tailscale": ts_info,
            "cloudflare": cf_info,
        }

    async def start(self, mode: str, settings: Settings) -> dict[str, Any]:
        normalized = mode.strip().lower()
        if normalized not in {
            "off",
            "tailscale_serve",
            "tailscale_local",
            "cloudflare",
        }:
            raise RemoteControlError(f"Unsupported tunnel mode: {mode!r}")

        async with self._lock:
            await self._stop_locked(settings)
            if normalized == "off":
                self._active_mode = "off"
                return await self.inspect(settings)

            if normalized == "tailscale_local":
                ts = await self._probe_tailscale(settings)
                if not ts["installed"]:
                    raise RemoteControlError("tailscale binary not found")
                if not ts["running"]:
                    raise RemoteControlError(
                        ts["error"] or "Tailscale daemon is not running"
                    )
                self._active_mode = "tailscale_local"
                return await self.inspect(settings)

            if normalized == "tailscale_serve":
                ts_bin = resolve_tailscale_binary(settings.rc_tailscale_binary)
                if ts_bin is None:
                    raise RemoteControlError("tailscale binary not found")
                target = f"http://127.0.0.1:{settings.port}"
                rc, _out, err = await _exec_probe(
                    [ts_bin, "serve", "--bg", "--yes", target], timeout=10.0
                )
                if rc != 0:
                    rc2, _out2, err2 = await _exec_probe(
                        [ts_bin, "serve", "--bg", target], timeout=10.0
                    )
                    if rc2 != 0:
                        msg = (err2 or err).strip() or f"tailscale serve exited {rc2}"
                        self._ts_last_error = msg
                        raise RemoteControlError(
                            f"tailscale serve failed: {msg}. Ensure Tailscale Serve "
                            "is enabled in your Tailnet ACL/admin console."
                        )
                self._ts_serve_started = True
                self._ts_last_error = ""
                self._active_mode = "tailscale_serve"
                return await self.inspect(settings)

            # cloudflare
            cf_bin = resolve_cloudflared_binary(settings.rc_cloudflare_binary)
            if cf_bin is None:
                raise RemoteControlError(
                    "cloudflared binary not found on PATH or RC_CLOUDFLARE_BINARY"
                )
            token = settings.rc_cloudflare_tunnel_token.strip()
            self._cf_public_url = ""
            self._cf_last_error = ""
            if token:
                argv = [cf_bin, "tunnel", "--no-autoupdate", "run", "--token", token]
            else:
                argv = [
                    cf_bin,
                    "tunnel",
                    "--no-autoupdate",
                    "--url",
                    f"http://127.0.0.1:{settings.port}",
                ]

            proc = await asyncio.create_subprocess_exec(
                *win_exec_argv(argv),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=_subprocess_flags(),
            )
            self._cf_proc = proc
            ready = asyncio.Event()
            if token:
                ready.set()

            async def _drain() -> None:
                assert proc.stderr is not None
                try:
                    while True:
                        line_b = await proc.stderr.readline()
                        if not line_b:
                            break
                        line = line_b.decode("utf-8", errors="replace").strip()
                        match = _QUICK_TUNNEL_URL_RE.search(line)
                        if match and not self._cf_public_url:
                            self._cf_public_url = match.group(0)
                            ready.set()
                except Exception as exc:  # pragma: no cover - defensive
                    self._cf_last_error = str(exc)
                finally:
                    ready.set()

            self._cf_reader_task = asyncio.create_task(_drain())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(ready.wait(), timeout=15.0)
            if proc.returncode is not None and proc.returncode != 0:
                raise RemoteControlError(
                    self._cf_last_error
                    or f"cloudflared exited early with code {proc.returncode}"
                )
            self._active_mode = "cloudflare"
            return await self.inspect(settings)

    async def _stop_locked(self, settings: Settings) -> None:
        if self._cf_proc is not None:
            proc = self._cf_proc
            self._cf_proc = None
            if proc.returncode is None:
                with contextlib.suppress(OSError):
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except TimeoutError:
                    with contextlib.suppress(OSError):
                        proc.kill()
                    await proc.wait()
        if self._cf_reader_task is not None:
            task = self._cf_reader_task
            self._cf_reader_task = None
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._cf_public_url = ""

        if self._ts_serve_started:
            ts_bin = resolve_tailscale_binary(settings.rc_tailscale_binary)
            if ts_bin is not None:
                rc, _, _ = await _exec_probe(
                    [ts_bin, "serve", "--https=443", "off"], timeout=5.0
                )
                if rc != 0:
                    with contextlib.suppress(Exception):
                        await _exec_probe([ts_bin, "serve", "reset"], timeout=5.0)
            self._ts_serve_started = False

        self._active_mode = "off"

    async def stop(self, settings: Settings) -> dict[str, Any]:
        async with self._lock:
            await self._stop_locked(settings)
            return await self.inspect(settings)

    async def close(self, settings: Settings) -> None:
        try:
            async with self._lock:
                await self._stop_locked(settings)
        except Exception as exc:  # pragma: no cover - shutdown safety
            logger.debug("Remote tunnel cleanup error: {}", type(exc).__name__)
