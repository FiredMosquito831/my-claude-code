"""A stand-in for the wireproxy binary, run by ``sys.executable`` (7.108.0 tests).

It speaks the part of wireproxy's command line and config MCC uses, with no
WireGuard at all: what goes "through the tunnel" goes straight to its target
on this computer. Standard library only, so it runs from any interpreter.

* ``-v`` prints ``wireproxy, version v1.1.3`` (``FAKE_WIREPROXY_VERSION``).
* ``-c <config> -i 127.0.0.1:<port>``: reads ``[Socks5] BindAddress`` /
  ``Username`` / ``Password`` and every ``[TCPClientTunnel] BindAddress`` /
  ``Target``; serves a SOCKS5 proxy there (RFC 1929 username/password when a
  username is set, refusing any other pair), raw TCP forwards for the
  tunnels, and ``/readyz`` (200, or 503 with ``FAKE_WIREPROXY_READY=0``) on
  the ``-i`` address.

Behaviours a test switches on through the environment:

* ``FAKE_WIREPROXY_EXIT_CODE`` / ``FAKE_WIREPROXY_EXIT_AFTER`` -- exit with
  that code after that many seconds (a crash);
* ``FAKE_WIREPROXY_PRINT_KEY=1`` -- print the config's PrivateKey line, so a
  test can prove MCC redacts it from ``runner.log``;
* ``FAKE_WIREPROXY_RESOLVE`` -- ``name=127.0.0.1:port;...``: where a tunnel's
  or a CONNECT's target really goes (a test's in-tunnel fake);
* ``FAKE_WIREPROXY_RECORD`` -- a file each CONNECT appends one JSON line to:
  the target and the username it authenticated with (never the password).
"""

import asyncio
import contextlib
import json
import os
import struct
import sys
import time


def _config(path: str) -> list[tuple[str, dict[str, str]]]:
    sections: list[tuple[str, dict[str, str]]] = []
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("["):
                sections.append((line.strip("[]").lower(), {}))
                continue
            key, _, value = line.partition("=")
            if sections:
                sections[-1][1][key.strip().lower()] = value.strip()
    return sections


def _split(address: str) -> tuple[str, int]:
    host, _, port = address.rpartition(":")
    return host.strip("[]"), int(port)


def _resolved(host: str, port: int) -> tuple[str, int]:
    table = os.environ.get("FAKE_WIREPROXY_RESOLVE", "")
    for item in filter(None, table.split(";")):
        name, _, target = item.partition("=")
        if name.strip() == f"{host}:{port}" or name.strip() == host:
            return _split(target.strip())
    return host, port


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            writer.close()


async def _relay(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    host: str,
    port: int,
) -> bool:
    target = _resolved(host, port)
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(*target)
    except OSError:
        return False
    await asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer))
    return True


def _record(target: str, user: str) -> None:
    path = os.environ.get("FAKE_WIREPROXY_RECORD")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"target": target, "user": user}) + "\n")


def _socks_handler(username: str, password: str):
    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            head = await reader.readexactly(2)
            methods = await reader.readexactly(head[1])
            wanted = 0x02 if username else 0x00
            if head[0] != 5 or wanted not in methods:
                writer.write(b"\x05\xff")
                await writer.drain()
                writer.close()
                return
            writer.write(bytes([5, wanted]))
            await writer.drain()
            user = ""
            if username:
                version = await reader.readexactly(1)
                user = (
                    await reader.readexactly((await reader.readexactly(1))[0])
                ).decode()
                secret = (
                    await reader.readexactly((await reader.readexactly(1))[0])
                ).decode()
                if version != b"\x01" or user != username or secret != password:
                    writer.write(b"\x01\x01")
                    await writer.drain()
                    writer.close()
                    print("socks5: authentication failed", flush=True)
                    return
                writer.write(b"\x01\x00")
                await writer.drain()
            request = await reader.readexactly(4)
            kind = request[3]
            if kind == 1:
                host = ".".join(str(part) for part in await reader.readexactly(4))
            elif kind == 3:
                host = (
                    await reader.readexactly((await reader.readexactly(1))[0])
                ).decode()
            else:
                raw = await reader.readexactly(16)
                host = ":".join(
                    f"{a:02x}{b:02x}" for a, b in zip(raw[::2], raw[1::2], strict=True)
                )
            port = struct.unpack("!H", await reader.readexactly(2))[0]
            _record(f"{host}:{port}", user)
            target = _resolved(host, port)
            try:
                upstream_reader, upstream_writer = await asyncio.open_connection(
                    *target
                )
            except OSError:
                writer.write(b"\x05\x04\x00\x01\x00\x00\x00\x00\x00\x00")
                await writer.drain()
                writer.close()
                return
            writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            await asyncio.gather(
                _pipe(reader, upstream_writer), _pipe(upstream_reader, writer)
            )
        except asyncio.IncompleteReadError, OSError:
            with contextlib.suppress(OSError):
                writer.close()

    return handle


def _tunnel_handler(target: str):
    host, port = _split(target)

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if not await _relay(reader, writer, host, port):
            writer.close()

    return handle


async def _health(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        line = await reader.readline()
        while (await reader.readline()).strip():
            pass
        ready = os.environ.get("FAKE_WIREPROXY_READY", "1") != "0"
        path = line.split(b" ")[1] if b" " in line else b"/"
        if path.startswith(b"/readyz"):
            body = b"{}"
            status = b"200 OK" if ready else b"503 Service Unavailable"
        else:
            body, status = b"not found", b"404 Not Found"
        writer.write(
            b"HTTP/1.1 "
            + status
            + b"\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
    except OSError:
        pass
    finally:
        writer.close()


async def _main(config_path: str, info: str) -> int:
    sections = _config(config_path)
    servers = []
    for name, values in sections:
        if name == "socks5":
            host, port = _split(values["bindaddress"])
            servers.append(
                await asyncio.start_server(
                    _socks_handler(
                        values.get("username", ""), values.get("password", "")
                    ),
                    host,
                    port,
                )
            )
            print(f"socks5 listening on {host}:{port}", flush=True)
        elif name == "tcpclienttunnel":
            host, port = _split(values["bindaddress"])
            servers.append(
                await asyncio.start_server(
                    _tunnel_handler(values["target"]), host, port
                )
            )
            print(f"tunnel {host}:{port} -> {values['target']}", flush=True)
        elif name == "interface" and os.environ.get("FAKE_WIREPROXY_PRINT_KEY") == "1":
            print(f"debug: PrivateKey = {values.get('privatekey', '')}", flush=True)
    if info:
        host, port = _split(info)
        servers.append(await asyncio.start_server(_health, host, port))
    print(
        "peer(fake) - Handshake completed (no WireGuard: a test stand-in)", flush=True
    )
    code = os.environ.get("FAKE_WIREPROXY_EXIT_CODE")
    if code is not None:
        await asyncio.sleep(float(os.environ.get("FAKE_WIREPROXY_EXIT_AFTER", "0")))
        print(f"exiting with code {code}", flush=True)
        return int(code)
    while True:
        await asyncio.sleep(3600)


def main(argv: list[str]) -> int:
    if "-v" in argv or "--version" in argv:
        print(
            f"wireproxy, version v{os.environ.get('FAKE_WIREPROXY_VERSION', '1.1.3')}"
        )
        return 0
    config = argv[argv.index("-c") + 1] if "-c" in argv else ""
    info = argv[argv.index("-i") + 1] if "-i" in argv else ""
    if not config:
        print("no config", flush=True)
        return 2
    started = time.monotonic()
    try:
        return asyncio.run(_main(config, info))
    except KeyboardInterrupt:
        return 0
    finally:
        print(f"ran {time.monotonic() - started:.1f}s", flush=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
