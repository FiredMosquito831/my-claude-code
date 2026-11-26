"""Real OS ConPTY / POSIX PTY bridge tests."""

import asyncio
import sys
from pathlib import Path

from my_claude_code.runtime.remote.pty_bridge import PtySession


def test_real_pty_bidirectional_io_resize_and_scrollback(tmp_path: Path) -> None:
    script = tmp_path / "echo_pty.py"
    script.write_text(
        "\n".join(
            [
                "import sys",
                "sys.stdout.write('READY\\n')",
                "sys.stdout.flush()",
                "for line in sys.stdin:",
                "    s = line.strip()",
                "    if s == 'QUIT':",
                "        sys.stdout.write('BYE\\n')",
                "        sys.stdout.flush()",
                "        break",
                "    if s:",
                "        sys.stdout.write(f'OUT:{s.upper()}\\n')",
                "        sys.stdout.flush()",
            ]
        ),
        encoding="utf-8",
    )

    async def _run() -> None:
        pty = PtySession()
        await pty.spawn(
            [sys.executable, "-u", str(script)],
            cwd=str(tmp_path),
            cols=100,
            rows=30,
        )
        assert pty.pid is not None

        collected = bytearray()

        async def _read_until(needle: bytes, timeout: float = 8.0) -> None:
            async def _drain() -> None:
                async for chunk in pty.subscribe():
                    collected.extend(chunk)
                    if needle in collected:
                        return

            await asyncio.wait_for(_drain(), timeout=timeout)

        await _read_until(b"READY")
        await pty.resize(132, 42)
        await pty.write(b"hello conpty\r")
        await _read_until(b"OUT:HELLO CONPTY")

        # Scrollback must contain earlier output for newly attached subscriber
        assert b"READY" in pty.scrollback_bytes()

        await pty.write(b"QUIT\r")
        await _read_until(b"BYE")
        await pty.close()

    asyncio.run(_run())
