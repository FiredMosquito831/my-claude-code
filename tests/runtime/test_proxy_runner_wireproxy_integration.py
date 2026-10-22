"""7.108.0: the REAL wireproxy, end to end on loopback -- optional, skipped by default.

Meant for a Linux CI job that downloaded the pinned build with consent; it
runs only when ``MCC_WIREPROXY_INTEGRATION_BINARY`` names a wireproxy
executable. No VPN and no network beyond 127.0.0.1:

* a second wireproxy plays the WireGuard *server*: it listens on a loopback
  UDP port and exposes a ``[TCPServerTunnel]`` -- port 80 of its tunnel
  address forwarded to a loopback echo server;
* MCC's supervisor runs the runner from a generated config (the real
  ``render_wireproxy_config``) whose peer is that server;
* a SOCKS5 request with the runner's local login to ``10.77.0.1:80`` comes back
  through the WireGuard tunnel from the echo server.
"""

import base64
import os
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from my_claude_code.application.proxy_runner_sources import (
    RunnerEdit,
    save_runner_source,
)
from my_claude_code.application.proxy_runner_state import STATE_READY, RunnerHub
from my_claude_code.config.proxy_chains import ProxyChains
from my_claude_code.config.proxy_runner_bins import ResolvedBinary
from my_claude_code.config.proxy_sources import (
    ProxySources,
    current_proxy_sources,
    save_proxy_sources,
)
from my_claude_code.runtime.proxy_runners import ProxyRunnerSupervisor

BINARY = os.environ.get("MCC_WIREPROXY_INTEGRATION_BINARY", "")
pytestmark = [
    pytest.mark.local_serial,
    pytest.mark.skipif(
        not BINARY,
        reason="set MCC_WIREPROXY_INTEGRATION_BINARY to a wireproxy to run this",
    ),
]


def _pair() -> tuple[str, str]:
    key = X25519PrivateKey.generate()
    private = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(private).decode(), base64.b64encode(public).decode()


def _free(kind: int = socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_a_real_wireproxy_runner_carries_a_socks_request_through_wireguard(
    tmp_path: Path,
) -> None:
    server_private, server_public = _pair()
    client_private, client_public = _pair()
    udp_port = _free(socket.SOCK_DGRAM)

    echo = socket.socket()
    echo.bind(("127.0.0.1", 0))
    echo.listen()
    echo_port = echo.getsockname()[1]

    def serve() -> None:
        while True:
            try:
                conn, _ = echo.accept()
            except OSError:
                return
            with conn:
                conn.sendall(b"echo:" + conn.recv(1024))

    threading.Thread(target=serve, daemon=True).start()

    server_conf = tmp_path / "server.conf"
    server_conf.write_text(
        "[Interface]\n"
        f"PrivateKey = {server_private}\n"
        "Address = 10.77.0.1/32\n"
        f"ListenPort = {udp_port}\n\n"
        "[Peer]\n"
        f"PublicKey = {client_public}\n"
        "AllowedIPs = 10.77.0.2/32\n\n"
        "[TCPServerTunnel]\n"
        "ListenPort = 80\n"
        f"Target = 127.0.0.1:{echo_port}\n",
        encoding="utf-8",
    )
    server = subprocess.Popen(
        [BINARY, "-c", str(server_conf)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    profile = (
        "[Interface]\n"
        f"PrivateKey = {client_private}\n"
        "Address = 10.77.0.2/32\n\n"
        "[Peer]\n"
        "# loopback-test\n"
        f"PublicKey = {server_public}\n"
        "AllowedIPs = 10.77.0.0/24\n"
        f"Endpoint = 127.0.0.1:{udp_port}\n"
    )
    ports = [_free(), _free()]
    allowed = set(ports)
    _, sources, source_id = save_runner_source(
        ProxyChains(),
        ProxySources(),
        "",
        RunnerEdit(profile_text=profile),
        port_range=f"{min(ports)}-{max(ports)}",
        port_free=lambda port: port in allowed,
    )
    save_proxy_sources(sources)
    hub = RunnerHub()
    supervisor = ProxyRunnerSupervisor(
        resolve=lambda: ResolvedBinary(Path(BINARY), "test"),
        base_dir=lambda: tmp_path / "runners",
        hub=hub,
        register=lambda pid: None,
        unregister=lambda pid: None,
    )
    try:
        supervisor.start(source_id)
        deadline = time.monotonic() + 30
        while (
            reading := hub.reading(source_id)
        ) is None or reading.state != STATE_READY:
            assert time.monotonic() < deadline, reading
            time.sleep(0.2)
        runner = current_proxy_sources().sources[source_id].runner
        assert runner is not None
        login = current_proxy_sources().secret(runner.login)
        assert login is not None
        with socket.create_connection(
            ("127.0.0.1", runner.socks_port), timeout=10
        ) as conn:
            conn.sendall(b"\x05\x01\x02")
            assert conn.recv(2) == b"\x05\x02"
            user, password = login.username.encode(), login.password.encode()
            conn.sendall(
                bytes([1, len(user)]) + user + bytes([len(password)]) + password
            )
            assert conn.recv(2) == b"\x01\x00"
            conn.sendall(
                b"\x05\x01\x00\x01"
                + socket.inet_aton("10.77.0.1")
                + struct.pack("!H", 80)
            )
            assert conn.recv(10)[1] == 0
            conn.sendall(b"through wireguard")
            assert conn.recv(1024) == b"echo:through wireguard"
    finally:
        supervisor.stop_all()
        server.kill()
        server.wait(10)
        echo.close()
