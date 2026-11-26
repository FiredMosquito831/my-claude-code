"""Tests for SVG QR generation, Tailscale/Cloudflare tunnel controller, and Git worktrees."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from my_claude_code.config.settings import Settings
from my_claude_code.runtime.remote.git_workspace import (
    collect_git_diff,
    create_session_worktree,
    detect_git_root,
    remove_session_worktree,
)
from my_claude_code.runtime.remote.qr import render_qr_svg
from my_claude_code.runtime.remote.tunnels import TunnelManager


def test_qr_svg_renders_valid_matrix() -> None:
    svg = render_qr_svg("https://laptop.ts.net/rc?pair=abc123")
    assert svg.startswith("<svg ")
    assert svg.endswith("</svg>")
    assert "<rect " in svg

    longer = render_qr_svg(
        "https://very-long-tunnel-subdomain.trycloudflare.com/rc?pair=" + ("x" * 64)
    )
    assert longer.startswith("<svg ")
    assert 'fill="#0f172a"' in longer


def _write_fake_cli(directory: Path, name: str, script_body: str) -> str:
    py_path = directory / f"{name}.py"
    py_path.write_text(script_body, encoding="utf-8")
    if sys.platform == "win32":
        wrapper = directory / f"{name}.cmd"
        wrapper.write_text(f'@"{sys.executable}" -u "{py_path}" %*\n', encoding="utf-8")
        return str(wrapper)
    wrapper = directory / name
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" -u "{py_path}" "$@"\n', encoding="utf-8"
    )
    wrapper.chmod(0o755)
    return str(wrapper)


def test_tunnel_manager_tailscale_and_cloudflare_mocks(
    monkeypatch, tmp_path: Path
) -> None:
    cfg_dir = tmp_path / "mcc_home"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCC_CONFIG_DIR", str(cfg_dir))
    ts_log = tmp_path / "ts_calls.log"
    ts_bin = _write_fake_cli(
        tmp_path,
        "tailscale",
        "\n".join(
            [
                "import json, sys",
                f"open({str(ts_log)!r}, 'a', encoding='utf-8').write(json.dumps(sys.argv[1:]) + '\\n')",
                "if sys.argv[1:3] == ['status', '--json']:",
                "    print(json.dumps({'BackendState':'Running','Self':{'HostName':'box','DNSName':'box.ts.net.'},'TailscaleIPs':['100.64.1.2']}))",
                "elif sys.argv[1:3] == ['serve', 'status']:",
                "    print('{}')",
            ]
        ),
    )
    cf_bin = _write_fake_cli(
        tmp_path,
        "cloudflared",
        "\n".join(
            [
                "import sys, time",
                "sys.stderr.write('INF +https://demo-unit.trycloudflare.com\\n')",
                "sys.stderr.flush()",
                "time.sleep(20)",
            ]
        ),
    )

    async def _run() -> None:
        mgr = TunnelManager()
        settings = Settings.model_validate(
            {
                "host": "127.0.0.1",
                "port": 18567,
                "RC_ENABLED": True,
                "RC_TUNNEL_MODE": "tailscale_serve",
                "RC_TAILSCALE_BINARY": ts_bin,
                "RC_TAILSCALE_HOSTNAME": "",
                "RC_CLOUDFLARE_BINARY": cf_bin,
            }
        )
        info = await mgr.inspect(settings)
        assert info["tailscale"]["installed"] is True
        assert info["tailscale"]["running"] is True
        assert info["tailscale"]["serve_url"] == "https://box.ts.net/rc"
        assert "127.0.0.1" in info["tailscale"]["bind_warning"]

        started_ts = await mgr.start("tailscale_serve", settings)
        assert started_ts["active_mode"] == "tailscale_serve"

        started_cf = await mgr.start("cloudflare", settings)
        assert started_cf["active_mode"] == "cloudflare"
        assert (
            started_cf["cloudflare"]["public_url"]
            == "https://demo-unit.trycloudflare.com/rc"
        )

        await mgr.stop(settings)
        calls = [
            json.loads(line) for line in ts_log.read_text(encoding="utf-8").splitlines()
        ]
        assert any("serve" in c and "--bg" in c for c in calls)
        assert any("serve" in c and "off" in c for c in calls)

    asyncio.run(_run())


def test_git_worktree_and_diff_helpers(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@t.local"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Tester"], check=True
    )
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )

    async def _run() -> None:
        root = await detect_git_root(repo)
        assert root == repo.resolve()
        wt, branch = await create_session_worktree(repo, "rc_1234567890")
        assert wt.is_dir()
        assert branch == "rc/rc_12345"
        (wt / "app.py").write_text("x = 2\n", encoding="utf-8")
        diff = await collect_git_diff(wt)
        assert diff["is_git"] is True
        assert "+x = 2" in diff["diff"]
        await remove_session_worktree(repo, wt)
        assert not wt.exists()

    asyncio.run(_run())
