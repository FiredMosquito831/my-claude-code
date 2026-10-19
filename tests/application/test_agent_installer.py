"""The Install button's runner (7.102.0, spec §8): plan, dry run, run, verify.

Nothing here installs anything. Every program a run executes is a fake from
``tests/support/fake_programs.py`` in a temp ``bin``; every download comes
from a fake vendor on loopback over https with a throwaway certificate; the
allow-list hosts are rewritten for the test only.
"""

import dataclasses
import datetime as dt
import ipaddress
import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from my_claude_code.application import agent_installer
from my_claude_code.application.agent_installer import (
    InstallBusyError,
    InstallTarget,
    Machine,
    NodeStatus,
    Plan,
    PlanStep,
    StepFailedError,
    TargetState,
    build_plan,
    classify_failure,
    install_target,
    install_targets,
    start_job,
)
from my_claude_code.config.install_methods import (
    Elevation,
    InstallKind,
    InstallMethod,
    InstallOs,
    Interpreter,
    methods_for_os,
)
from my_claude_code.core.mcc_processes import process_is_alive
from tests.support.fake_programs import (
    calls,
    fake_launcher_text,
    launcher_name,
    write_fake_program,
)

HERE_OS = (
    InstallOs.WINDOWS
    if sys.platform == "win32"
    else InstallOs.MACOS
    if sys.platform == "darwin"
    else InstallOs.LINUX
)
NODE_OK = NodeStatus(
    state="ok",
    node_path="/fake/node",
    node_version="v99.0.0",
    npm_path="/fake/npm",
    npm_version="99.0.0",
    prefix="/fake/prefix",
    prefix_writable=True,
)
NOT_INSTALLED = TargetState(installed=False)


@pytest.fixture(autouse=True)
def _no_registry_path(monkeypatch):
    """The registry PATH is the real machine's on a Windows box; never read it."""

    monkeypatch.setattr(agent_installer, "_registry_path", lambda environ: "")


#: The only variables a run here inherits from the machine: what Windows
#: needs to start a process at all. Everything naming a folder a real
#: program could live in (PATH, ProgramFiles, the npm and fnm folders) is the
#: test's own -- a copy of os.environ once let the real Node.js in
#: C:\Program Files\nodejs answer a fake install.
_KEPT = ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "SYSTEMDRIVE")


def _env(bin_dir: Path, tmp_path: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key.upper() in _KEPT}
    home = tmp_path / "env-home"
    env.update(
        {
            "PATH": str(bin_dir),
            "PROGRAMFILES": str(tmp_path / "ProgramFiles"),
            "FNM_DIR": str(tmp_path / "fnm"),
            "HOME": str(home),
            "USERPROFILE": str(home),
            "APPDATA": str(home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(home / "AppData" / "Local"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "NPM_CONFIG_PREFIX": str(tmp_path / "npm-prefix-unused"),
            "NPM_CONFIG_OFFLINE": "true",
        }
    )
    return env


def _wait_for(job, *, seconds: float = 60.0):
    deadline = time.monotonic() + seconds
    while job.stage == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert job.stage != "running", job.payload()
    return job


# ------------------------------------------------------------------ dry run


def test_agent_install_dry_run_never_spawns_or_downloads(monkeypatch, tmp_path):
    """Spec §8.2: ``dry_run`` over every method on every OS table spawns and
    downloads nothing -- spawn, download and every probe raise if touched."""

    spawned: list[Any] = []
    downloaded: list[Any] = []

    def no_popen(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("a dry run spawned a process")

    def no_download(*args, **kwargs):
        downloaded.append(args)
        raise AssertionError("a dry run downloaded something")

    monkeypatch.setattr(subprocess, "Popen", no_popen)
    monkeypatch.setattr(agent_installer, "download", no_download)
    monkeypatch.setattr(agent_installer, "probe_output", no_popen)
    # Make every method plannable on every OS from this one box.
    monkeypatch.setattr(
        agent_installer,
        "_interpreter_path",
        lambda interpreter, machine, environ: f"/fake/{interpreter.value}",
    )
    monkeypatch.setattr(
        agent_installer,
        "_program_available",
        lambda names, environ: f"/fake/{names[0]}",
    )
    env = _env(tmp_path / "empty-bin", tmp_path)
    env["LOCALAPPDATA"] = str(tmp_path / "local")
    env["HOME"] = env["USERPROFILE"] = str(tmp_path / "home")

    walked = 0
    by_kind: set[str] = set()
    for node in (NODE_OK, NodeStatus(state="missing")):
        for target in install_targets():
            for os_ in InstallOs:
                for arch in ("x64", "arm64"):
                    machine = Machine(os_, arch)
                    for method in methods_for_os(target.methods, os_, arch):
                        plan = build_plan(
                            target, method.id, machine, node, env, state=NOT_INSTALLED
                        )
                        if not plan.runnable:
                            continue
                        job = start_job(plan, dry_run=True, environ=env, wait=True)
                        assert job.stage == "done", job.payload()
                        assert job.result["status"] == "dry_run"
                        lines, _cursor = job.lines_since(0)
                        would = [line for line in lines if line.startswith("would ")]
                        assert len(would) == len(plan.steps), (target.id, method.id)
                        walked += 1
                        by_kind.add(method.kind.value)
    assert spawned == []
    assert downloaded == []
    # Not vacuous: every runnable kind was walked, Node install steps too.
    assert walked > 80
    assert by_kind >= {
        "script",
        "npm",
        "winget",
        "brew",
        "download",
        "vscode_extension",
    }


# ---------------------------------------------------------- Node detection


def _fake_node(bin_dir: Path, version: str, prefix: Path) -> None:
    write_fake_program(bin_dir, "node", outputs={"--version": version})
    write_fake_program(
        bin_dir,
        "npm",
        outputs={"--version": "10.9.0", "config": str(prefix)},
    )


@pytest.mark.spawns_process
@pytest.mark.local_serial
def test_node_detection_missing_too_old_unwritable_ok(monkeypatch, tmp_path):
    machine = Machine(HERE_OS, "x64")
    gemini = install_target("gemini_cli")

    # missing: nothing on PATH, nothing in the known folders.
    missing = agent_installer.detect_node(
        machine, _env(tmp_path / "empty", tmp_path), cached=False
    )
    assert missing.state == "missing"

    # ok: a writable npm prefix.
    bin_ok = tmp_path / "bin-ok"
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    _fake_node(bin_ok, "v22.12.0", prefix)
    ok = agent_installer.detect_node(machine, _env(bin_ok, tmp_path), cached=False)
    assert ok.state == "ok"
    assert ok.node_version == "v22.12.0"
    assert ok.npm_version == "10.9.0"
    assert ok.prefix == str(prefix)
    assert ok.prefix_writable is True
    assert os.path.basename(ok.npm_path).startswith("npm")
    # too old is per package: 22.12 meets Gemini's 20 and Pi's 22.19 not.
    gemini_bound = gemini.methods[-1].needs_node
    assert gemini_bound is not None
    assert ok.satisfies(gemini_bound)
    pi_npm = next(m for m in install_target("pi").methods if m.kind is InstallKind.NPM)
    assert pi_npm.needs_node is not None
    assert not ok.satisfies(pi_npm.needs_node)

    # too old: Node 18 for a package that needs 20 -> refused, never upgraded.
    bin_old = tmp_path / "bin-old"
    _fake_node(bin_old, "v18.20.0", prefix)
    old = agent_installer.detect_node(machine, _env(bin_old, tmp_path), cached=False)
    assert old.state == "ok"
    plan = build_plan(gemini, "npm", machine, old, _env(bin_old, tmp_path))
    assert plan.runnable is False
    assert "older than this package needs" in plan.refused
    assert "never replaces your Node.js" in plan.refused
    assert plan.node_install is False

    # unwritable: a prefix npm -g would need sudo for.
    monkeypatch.setattr(agent_installer, "_writable", lambda path: False)
    locked = agent_installer.detect_node(machine, _env(bin_ok, tmp_path), cached=False)
    assert locked.state == "unwritable"
    plan = build_plan(gemini, "npm", machine, locked, _env(bin_ok, tmp_path))
    assert plan.runnable is False
    assert "not writable" in plan.refused
    assert "never runs" in plan.refused


def test_npm_method_installs_node_first_only_when_missing(tmp_path):
    gemini = install_target("gemini_cli")
    env = _env(tmp_path / "empty", tmp_path)
    for os_ in InstallOs:
        machine = Machine(os_, "x64")
        missing = build_plan(
            gemini,
            "npm",
            machine,
            NodeStatus(state="missing"),
            env,
            state=NOT_INSTALLED,
        )
        assert missing.runnable, missing.refused
        assert missing.node_install is True
        kinds = [step.kind for step in missing.steps]
        assert kinds[0] == "node"
        assert kinds.index("run") > max(i for i, k in enumerate(kinds) if k == "node")
        assert "Node.js LTS will be installed first" in missing.node_text

        present = build_plan(gemini, "npm", machine, NODE_OK, env, state=NOT_INSTALLED)
        assert present.node_install is False
        assert [step.kind for step in present.steps] == ["run", "verify"]
        assert "Uses your Node.js v99.0.0" in present.node_text

    windows = build_plan(
        gemini,
        "npm",
        Machine(InstallOs.WINDOWS, "x64"),
        NodeStatus(state="missing"),
        env,
        state=NOT_INSTALLED,
    )
    assert windows.elevation[0].startswith(
        "Windows will ask for administrator permission"
    )
    linux = build_plan(
        gemini,
        "npm",
        Machine(InstallOs.LINUX, "x64"),
        NodeStatus(state="missing"),
        env,
        state=NOT_INSTALLED,
    )
    # fnm per user: no permission prompt, and the profile line is named.
    assert linux.elevation == ()
    assert any(".bashrc" in change for change in linux.changes)


@pytest.mark.spawns_process
@pytest.mark.local_serial
def test_a_missing_node_is_installed_before_npm_runs(monkeypatch, tmp_path):
    """The Windows order: release list, the .msi + SHASUMS256.txt, the check,
    msiexec by path, then npm -- and npm with the new Node first on PATH."""

    bin_dir = tmp_path / "bin"
    program_files = tmp_path / "ProgramFiles"
    node_dir = program_files / "nodejs"
    prefix = tmp_path / "npm-prefix"
    prefix.mkdir()
    env = _env(bin_dir, tmp_path)
    bin_dir.mkdir()
    machine = Machine(InstallOs.WINDOWS, "x64")
    gemini = install_target("gemini_cli")
    plan = build_plan(gemini, "npm", machine, NodeStatus(state="missing"), env)
    assert plan.node_install

    msi = b"fake msi bytes"
    digest = __import__("hashlib").sha256(msi).hexdigest()
    fetched: list[str] = []

    def fake_download(url, hosts, destination, *, cancel):
        agent_installer.check_url(url, hosts)
        fetched.append(url)
        if url.endswith("index.json"):
            body = json.dumps(
                [
                    {"version": "v25.0.0", "lts": False},
                    {"version": "v24.22.0", "lts": "Krypton"},
                ]
            ).encode()
        elif url.endswith(".msi"):
            body = msi
        else:
            body = f"{digest}  node-v24.22.0-x64.msi\n".encode()
        destination.write_bytes(body)
        return agent_installer.Downloaded(
            destination, __import__("hashlib").sha256(body).hexdigest(), len(body), url
        )

    ran: list[list[str]] = []
    real_spawn = agent_installer._Runner._spawn

    def fake_spawn(self, argv, *, timeout, env, cwd=None):
        ran.append(list(argv))
        if os.path.basename(argv[0]).lower() == "msiexec.exe":
            # "Install" Node.js: the fakes land where the MSI puts them.
            _fake_node(node_dir, "v24.22.0", prefix)
            return 0
        return real_spawn(self, argv, timeout=timeout, env=env, cwd=cwd)

    monkeypatch.setattr(agent_installer, "download", fake_download)
    monkeypatch.setattr(agent_installer._Runner, "_spawn", fake_spawn)
    monkeypatch.setattr(
        agent_installer, "current_machine", lambda environ=None: machine
    )
    # npm puts global shims in the prefix on Windows, in prefix/bin elsewhere.
    shims = prefix if os.name == "nt" else prefix / "bin"
    write_fake_program(shims, "gemini", outputs={"--version": "0.64.0"})
    job = _wait_for(start_job(plan, dry_run=False, environ=env))

    assert fetched == [
        "https://nodejs.org/dist/index.json",
        "https://nodejs.org/dist/v24.22.0/node-v24.22.0-x64.msi",
        "https://nodejs.org/dist/v24.22.0/SHASUMS256.txt",
    ]
    assert os.path.basename(ran[0][0]).lower() == "msiexec.exe"
    assert ran[0][1:2] == ["/i"] and ran[0][-2:] == ["/passive", "/norestart"]
    assert os.path.basename(ran[1][0]).lower().startswith("npm")
    assert ran[1][1:] == ["install", "-g", "@google/gemini-cli"]
    npm_call = [
        c
        for c in calls(node_dir)
        if c["program"] == "npm" and c["argv"][:1] == ["install"]
    ]
    assert npm_call, calls(node_dir)
    assert job.result["status"] in {"verified", "not_on_path"}, job.result
    assert "gemini 0.64.0" in job.result["message"].replace(".cmd", "")


def test_a_node_installer_that_does_not_match_its_checksum_never_runs(
    monkeypatch, tmp_path
):
    env = _env(tmp_path / "bin", tmp_path)
    machine = Machine(InstallOs.WINDOWS, "x64")
    plan = build_plan(
        install_target("gemini_cli"), "npm", machine, NodeStatus(state="missing"), env
    )

    def fake_download(url, hosts, destination, *, cancel):
        body = (
            json.dumps([{"version": "v24.22.0", "lts": "Krypton"}]).encode()
            if url.endswith("index.json")
            else b"tampered"
            if url.endswith(".msi")
            else b"0000  node-v24.22.0-x64.msi\n"
        )
        destination.write_bytes(body)
        return agent_installer.Downloaded(
            destination, __import__("hashlib").sha256(body).hexdigest(), len(body), url
        )

    def no_spawn(self, argv, **kwargs):
        raise AssertionError(f"ran {argv} after a checksum mismatch")

    monkeypatch.setattr(agent_installer, "download", fake_download)
    monkeypatch.setattr(agent_installer._Runner, "_spawn", no_spawn)
    job = _wait_for(start_job(plan, dry_run=False, environ=env))
    assert job.stage == "failed"
    assert job.result["classification"] == "checksum"


# ------------------------------------------------------------- verification


@pytest.mark.spawns_process
@pytest.mark.local_serial
def test_install_verification_reads_refreshed_path(monkeypatch, tmp_path):
    """After an install the server's own PATH is stale (spec §5): the program
    is found through the registry PATH or the vendor's folder, verified with
    --version, and the wording says to open a new terminal."""

    payload = tmp_path / "payload"
    write_fake_program(
        payload, "claude", outputs={"--version": "2.1.301 (Claude Code)"}
    )
    registry_dir = tmp_path / "registry-path-dir"
    server_bin = tmp_path / "server-bin"
    server_bin.mkdir()
    env = _env(server_bin, tmp_path)

    # A step that "installs" claude where only the registry PATH can see it.
    method = InstallMethod(
        id="native",
        label="fake",
        os=HERE_OS,
        kind=InstallKind.VSCODE_EXTENSION,
        doc_url="https://example.invalid/doc",
        checked_on="2026-10-19",
        checked_by="test",
        copy_command="fake",
    )
    target = InstallTarget(
        id="claude",
        kind="cli",
        display_name="Claude Code",
        binaries=("claude",),
        methods=(),
    )
    dropper = write_fake_program(
        tmp_path / "tools",
        "dropper",
        drop=[
            [
                str(registry_dir / launcher_name("claude")),
                fake_launcher_text(payload / "claude.py"),
            ]
        ],
    )
    plan = _hand_plan(target, method, [str(dropper)])
    monkeypatch.setattr(
        agent_installer, "_registry_path", lambda environ: str(registry_dir)
    )
    job = _wait_for(start_job(plan, dry_run=False, environ=env))

    assert job.result["status"] == "not_on_path", job.result
    assert job.result["message"].startswith("Installed and verified: claude 2.1.301")
    assert (
        "not on PATH in this server yet: open a new terminal" in job.result["message"]
    )
    assert job.result["path"].startswith(str(registry_dir))
    assert any(
        c["program"] == "claude" and c["argv"] == ["--version"] for c in calls(payload)
    )

    # On the server's own PATH it is "installed and verified" plain.
    write_fake_program(
        server_bin, "claude", outputs={"--version": "2.1.301 (Claude Code)"}
    )
    job = _wait_for(start_job(plan, dry_run=False, environ=env))
    assert job.result["status"] == "verified"
    # ``shutil.which`` answers with PATHEXT's spelling (``claude.CMD``).
    assert os.path.normcase(job.result["message"]) == os.path.normcase(
        f"Installed and verified: claude 2.1.301 (Claude Code) at "
        f"{server_bin / launcher_name('claude')}."
    )


def _hand_plan(
    target: InstallTarget,
    method: InstallMethod,
    command: list[str],
    *,
    timeout: int = 60,
) -> Plan:
    machine = Machine(HERE_OS, "x64")
    return Plan(
        target=target,
        method=method,
        machine=machine,
        node=NodeStatus(state="missing"),
        runnable=True,
        refused="",
        steps=(
            PlanStep(
                kind="run", text="run", command=tuple(command), timeout_seconds=timeout
            ),
            PlanStep(kind="verify", text="verify"),
        ),
        elevation=(),
        changes=(),
        node_text="",
        node_install=False,
        verify_names=target.binaries,
        verify_dirs=(),
    )


# ---------------------------------------------------------------- failures


@pytest.mark.spawns_process
@pytest.mark.local_serial
def test_install_failure_is_classified_and_logged(tmp_path):
    bin_dir = tmp_path / "bin"
    env = _env(bin_dir, tmp_path)
    npm = write_fake_program(
        bin_dir,
        "npm",
        default=(
            "npm ERR! code EACCES\nnpm ERR! syscall mkdir\n"
            "npm ERR! Error: EACCES: permission denied, mkdir '/usr/lib/node_modules'"
        ),
        exit=243,
    )
    target = install_target("cline_cli")
    method = next(m for m in target.methods if m.os is HERE_OS)
    plan = _hand_plan(target, method, [str(npm), "install", "-g", "cline"])
    job = _wait_for(start_job(plan, dry_run=False, environ=env))

    result = job.result
    assert job.stage == "failed"
    assert result["classification"] == "permission"
    assert result["classification_label"].startswith("Permission")
    assert result["exit_code"] == 243
    assert any("EACCES" in line for line in result["last_lines"])
    assert len(result["last_lines"]) <= 40
    assert result["copy_command"] == "npm install -g cline"
    assert result["doc_url"].startswith("https://docs.cline.bot/")
    assert "install -g cline" in result["command"]
    log = Path(result["log_path"])
    assert log.parent.name == "agent-installs"
    assert log.name.startswith("cline_cli-")
    text = log.read_text(encoding="utf-8")
    assert "npm ERR! code EACCES" in text
    assert "[failed]" in text


@pytest.mark.parametrize(
    ("exit_code", "lines", "os_", "expected"),
    [
        (
            1,
            ["npm ERR! code ENOSPC", "no space left on device"],
            InstallOs.LINUX,
            "disk",
        ),
        (
            1,
            ["npm WARN EBADENGINE Unsupported engine"],
            InstallOs.LINUX,
            "node_too_old",
        ),
        (1, ["npm ERR! code ETIMEDOUT"], InstallOs.LINUX, "network"),
        (1, ["curl: (6) Could not resolve host: example"], InstallOs.MACOS, "network"),
        (225, ["Operation did not complete"], InstallOs.WINDOWS, "antivirus"),
        (1602, ["User cancelled installation."], InstallOs.WINDOWS, "declined"),
        (1, ["Access is denied."], InstallOs.WINDOWS, "permission"),
        (3, ["something else entirely"], InstallOs.WINDOWS, "other"),
    ],
)
def test_failures_are_classified_from_exit_code_and_output(
    exit_code, lines, os_, expected
):
    assert classify_failure(exit_code, lines, os_=os_) == expected


def test_a_downloaded_file_that_vanished_is_classified_as_antivirus(
    monkeypatch, tmp_path
):
    env = _env(tmp_path / "bin", tmp_path)
    target = install_target("claude")
    script = next(
        m for m in target.methods if m.os is HERE_OS and m.kind is InstallKind.SCRIPT
    )

    def vanishing_download(url, hosts, destination, *, cancel):
        destination.write_text("echo hi", encoding="utf-8")
        result = agent_installer.Downloaded(destination, "0" * 64, 7, url)
        destination.unlink()  # what a quarantine looks like from here
        return result

    monkeypatch.setattr(agent_installer, "download", vanishing_download)
    monkeypatch.setattr(
        agent_installer,
        "_interpreter_path",
        lambda interpreter, machine, environ: sys.executable,
    )
    plan = build_plan(
        target, script.id, Machine(HERE_OS, "x64"), NODE_OK, env, state=NOT_INSTALLED
    )
    job = _wait_for(start_job(plan, dry_run=False, environ=env))
    assert job.result["classification"] == "antivirus"
    assert "vanished" in job.result["message"]


# ------------------------------------------------------------------- cancel


_CHILD_SLEEP = "import time; time.sleep(120)"


@pytest.mark.spawns_process
@pytest.mark.local_serial
def test_cancel_stops_only_its_own_pid_tree(tmp_path):
    """Cancel stops the child the run started and that child's children --
    by exact PID -- and nothing else, not even a process started the same
    way a moment earlier (II.6 rule 3)."""

    bystander = subprocess.Popen([sys.executable, "-I", "-c", _CHILD_SLEEP])
    try:
        pid_file = tmp_path / "pids.json"
        installer = write_fake_program(
            tmp_path / "tools",
            "slow-installer",
            spawn={"code": _CHILD_SLEEP, "pid_file": str(pid_file)},
            sleep=120,
        )
        target = install_target("cline_cli")
        method = next(m for m in target.methods if m.os is HERE_OS)
        plan = _hand_plan(target, method, [str(installer)], timeout=300)
        job = start_job(plan, dry_run=False, environ=_env(tmp_path / "bin", tmp_path))
        deadline = time.monotonic() + 60
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        pids = json.loads(pid_file.read_text(encoding="utf-8"))
        assert process_is_alive(pids["grandchild"])

        agent_installer.cancel_job(job.id)
        _wait_for(job)

        assert job.stage == "cancelled"
        assert job.result["classification"] == "cancelled"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and (
            process_is_alive(pids["child"]) or process_is_alive(pids["grandchild"])
        ):
            time.sleep(0.2)
        assert not process_is_alive(pids["child"])
        assert not process_is_alive(pids["grandchild"])
        assert bystander.poll() is None, "cancel stopped a process it did not start"
    finally:
        bystander.kill()
        bystander.wait(timeout=30)


# --------------------------------------------------------------- one at a time


def test_agent_install_one_job_at_a_time(monkeypatch, tmp_path):
    env = _env(tmp_path / "bin", tmp_path)
    release = threading.Event()

    def held_spawn(self, argv, *, timeout, env, cwd=None):
        release.wait(30)
        return 0

    monkeypatch.setattr(agent_installer._Runner, "_spawn", held_spawn)
    target = install_target("cline_cli")
    method = next(m for m in target.methods if m.os is HERE_OS)
    plan = _hand_plan(target, method, ["npm", "install", "-g", "cline"])
    first = start_job(plan, dry_run=False, environ=env)
    try:
        with pytest.raises(InstallBusyError):
            start_job(plan, dry_run=False, environ=env)
        with pytest.raises(InstallBusyError):
            start_job(plan, dry_run=True, environ=env)
    finally:
        release.set()
    _wait_for(first)
    # Once it is over, the next one may run.
    second = start_job(plan, dry_run=True, environ=env, wait=True)
    assert second.result["status"] == "dry_run"


def test_the_lock_file_keeps_two_mcc_processes_apart(monkeypatch, tmp_path):
    env = _env(tmp_path / "bin", tmp_path)
    lock = agent_installer.agent_install_lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    target = install_target("cline_cli")
    method = next(m for m in target.methods if m.os is HERE_OS)
    plan = _hand_plan(target, method, ["npm"])

    # Another live MCC process holds it.
    lock.write_text(json.dumps({"pid": 4242, "job": "theirs"}), encoding="utf-8")
    monkeypatch.setattr(agent_installer, "process_is_alive", lambda pid: True)
    with pytest.raises(InstallBusyError, match="pid 4242"):
        start_job(plan, dry_run=True, environ=env)

    # A stale lock (its process is gone) is taken over.
    monkeypatch.setattr(agent_installer, "process_is_alive", lambda pid: False)
    job = start_job(plan, dry_run=True, environ=env, wait=True)
    assert job.result["status"] == "dry_run"
    assert not lock.exists()


# ----------------------------------------------------------------- downloads


def _self_signed(tmp_path: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "vendor.pem"
    key_path = tmp_path / "vendor.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


class _Vendor(BaseHTTPRequestHandler):
    routes: ClassVar[dict[str, tuple[int, dict[str, str], bytes]]] = {}
    seen: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        type(self).seen.append(self.path)
        status, headers, body = type(self).routes.get(self.path, (404, {}, b""))
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


@pytest.fixture
def fake_vendor(tmp_path, monkeypatch) -> Iterator[tuple[str, type[_Vendor]]]:
    """An https vendor on loopback; the runner's client trusts its cert only."""

    cert, key = _self_signed(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Vendor)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client_context = ssl.create_default_context(cafile=str(cert))
    monkeypatch.setattr(
        agent_installer,
        "_http_client",
        lambda: httpx.Client(
            follow_redirects=False,
            verify=client_context,
            timeout=10,
            trust_env=False,
        ),
    )
    _Vendor.routes = {}
    _Vendor.seen = []
    try:
        yield f"https://127.0.0.1:{server.server_address[1]}", _Vendor
    finally:
        server.shutdown()
        server.server_close()


def test_downloads_refuse_http_and_hosts_off_the_allow_list(fake_vendor, tmp_path):
    base, vendor = fake_vendor
    cancel = threading.Event()
    vendor.routes["/ok"] = (200, {}, b"hello")
    vendor.routes["/hop"] = (302, {"Location": "https://evil.example/x"}, b"")

    done = agent_installer.download(
        f"{base}/ok", ("127.0.0.1",), tmp_path / "a", cancel=cancel
    )
    assert done.size == 5
    assert done.sha256 == __import__("hashlib").sha256(b"hello").hexdigest()

    with pytest.raises(StepFailedError, match="only over https"):
        agent_installer.download(
            base.replace("https", "http") + "/ok",
            ("127.0.0.1",),
            tmp_path / "b",
            cancel=cancel,
        )
    with pytest.raises(StepFailedError, match="not on this method's allow-list"):
        agent_installer.download(
            f"{base}/ok", ("example.com",), tmp_path / "c", cancel=cancel
        )
    # A redirect is checked hop by hop: evil.example is never contacted.
    with pytest.raises(StepFailedError, match=r"evil\.example is not on"):
        agent_installer.download(
            f"{base}/hop", ("127.0.0.1",), tmp_path / "d", cancel=cancel
        )


@pytest.mark.spawns_process
@pytest.mark.local_serial
def test_a_vendor_script_is_downloaded_over_https_and_run_by_path(
    fake_vendor, monkeypatch, tmp_path
):
    """Spec §8.1 end to end: Install -> plan -> run -> the fake vendor's script
    is fetched over https, hashed, run by path with the interpreter named, it
    drops a fake claude into the bin, and the card's result is verified."""

    base, vendor = fake_vendor
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    payload = tmp_path / "payload"
    write_fake_program(
        payload, "claude", outputs={"--version": "2.1.301 (Claude Code)"}
    )
    destination = bin_dir / launcher_name("claude")
    launcher = fake_launcher_text(payload / "claude.py")
    if HERE_OS is InstallOs.WINDOWS:
        body = (
            "$ErrorActionPreference = 'Stop'\r\n"
            "Write-Output 'fake vendor: installing claude'\r\n"
            f"Set-Content -LiteralPath '{destination}' -Value '{launcher.strip()}' "
            "-Encoding ascii\r\n"
            "Write-Output 'fake vendor: done'\r\n"
        )
        path = "/install.ps1"
    else:
        # The run inherits a PATH holding only the test's bin, so the script
        # names chmod by its absolute path (a real vendor script runs with
        # the user's own PATH).
        chmod = shutil.which("chmod") or "/bin/chmod"
        body = (
            "set -e\n"
            "echo 'fake vendor: installing claude'\n"
            f"printf '%s\\n' '#!/bin/sh' '{launcher.splitlines()[1]}' > '{destination}'\n"
            f"'{chmod}' +x '{destination}'\n"
            "echo 'fake vendor: done'\n"
        )
        path = "/install.sh"
    vendor.routes["/redirect"] = (302, {"Location": path}, b"")
    vendor.routes[path] = (200, {}, body.encode())

    real = install_target("claude")
    native = next(
        m for m in real.methods if m.os is HERE_OS and m.kind is InstallKind.SCRIPT
    )
    rewritten = dataclasses.replace(
        native, url=f"{base}/redirect", hosts=("127.0.0.1",)
    )
    target = dataclasses.replace(real, methods=(rewritten,))
    env = _env(bin_dir, tmp_path)
    machine = Machine(HERE_OS, "x64")
    if HERE_OS is not InstallOs.WINDOWS:
        monkeypatch.setattr(
            agent_installer,
            "_interpreter_path",
            lambda interpreter, machine, environ: "/bin/sh",
        )
    plan = build_plan(target, rewritten.id, machine, NODE_OK, env)
    assert plan.runnable, plan.refused
    assert [step.kind for step in plan.steps] == ["download", "run", "verify"]
    run_step = plan.steps[1]
    assert agent_installer.DOWNLOADED in run_step.command
    if HERE_OS is InstallOs.WINDOWS:
        assert run_step.command[1:7] == (
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            agent_installer.DOWNLOADED,
        )
        assert "iex" not in " ".join(run_step.command).lower()

    job = _wait_for(start_job(plan, dry_run=False, environ=env), seconds=120)

    assert job.stage == "done", job.payload()
    assert job.result["status"] == "verified", job.result
    assert job.result["message"].startswith("Installed and verified: claude 2.1.301")
    assert vendor.seen == ["/redirect", path]
    lines, _ = job.lines_since(0)
    assert any(line.startswith("downloaded ") and "sha256 " in line for line in lines)
    assert any("fake vendor: done" in line for line in lines)
    assert destination.exists()


# ---------------------------------------------------------------- headless


def test_headless_server_hides_desktop_and_refuses_os_prompts(tmp_path):
    env = _env(tmp_path / "bin", tmp_path)
    env["LOCALAPPDATA"] = str(tmp_path / "local")
    headless = Machine(
        InstallOs.WINDOWS,
        "x64",
        headless=True,
        headless_reason="this server runs as a service",
    )
    claude_desktop = install_target("claude_desktop")
    plan = build_plan(
        claude_desktop, "download", headless, NODE_OK, env, state=NOT_INSTALLED
    )
    assert plan.runnable is False
    assert "this server runs as a service" in plan.refused
    payload = agent_installer.target_payload(
        claude_desktop, headless, NODE_OK, env, NOT_INSTALLED
    )
    assert payload["offer_install"] is False
    assert payload["hidden_reason"].startswith(
        "Desktop apps are not installed from here"
    )

    # A CLI's own script still runs; an OS-prompt method (winget, the Node
    # .msi) is refused with the reason.
    claude = install_target("claude")
    winget = build_plan(claude, "winget", headless, NODE_OK, env, state=NOT_INSTALLED)
    assert winget.runnable is False
    assert "permission prompt" in winget.refused
    node_missing = build_plan(
        install_target("gemini_cli"),
        "npm",
        headless,
        NodeStatus(state="missing"),
        env,
        state=NOT_INSTALLED,
    )
    assert node_missing.runnable is False
    assert "Node.js is missing" in node_missing.refused


def test_wsl_is_server_only_and_linux_without_a_display_is_headless(monkeypatch):
    monkeypatch.setattr(agent_installer, "_platform", lambda: "linux")
    monkeypatch.setattr(agent_installer, "_is_wsl", lambda: True)
    assert agent_installer.current_machine({}).headless is True
    monkeypatch.setattr(agent_installer, "_is_wsl", lambda: False)
    assert agent_installer.current_machine({}).headless is True
    assert agent_installer.current_machine({"DISPLAY": ":0"}).headless is False


# --------------------------------------------------------------- the guards


def test_a_run_never_spawns_sudo_runas_or_unsafe_perm(monkeypatch, tmp_path):
    spawned: list[Any] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a))
    target = install_target("cline_cli")
    method = next(m for m in target.methods if m.os is HERE_OS)
    for argv in (
        ["/usr/bin/sudo", "npm", "install"],
        ["runas", "/user:Administrator", "x"],
        ["npm", "install", "-g", "--unsafe-perm", "x"],
    ):
        job = _wait_for(
            start_job(
                _hand_plan(target, method, argv), dry_run=False, environ=os.environ
            )
        )
        assert job.stage == "failed"
        assert "Refused to run a command naming" in job.result["message"]
    assert spawned == []


def test_installed_programs_get_no_plan_and_link_rows_never_run(tmp_path):
    env = _env(tmp_path / "bin", tmp_path)
    machine = Machine(HERE_OS, "x64")
    claude = install_target("claude")
    method = methods_for_os(claude.methods, HERE_OS, "x64")[0]
    plan = build_plan(
        claude, method.id, machine, NODE_OK, env, state=TargetState(True, "/x/claude")
    )
    assert plan.runnable is False
    assert plan.steps == ()
    assert "already installed at /x/claude" in plan.refused
    warp = install_target("warp")
    link = build_plan(warp, "link", machine, NODE_OK, env, state=NOT_INSTALLED)
    assert link.runnable is False
    assert link.method.kind is InstallKind.LINK
    with pytest.raises(agent_installer.InstallError):
        start_job(link, dry_run=True, environ=env)


def test_a_print_only_method_is_never_runnable(tmp_path):
    env = _env(tmp_path / "bin", tmp_path)
    opencode_desktop = install_target("opencode_desktop")
    plan = build_plan(
        opencode_desktop,
        "deb",
        Machine(InstallOs.LINUX, "x64"),
        NODE_OK,
        env,
        state=NOT_INSTALLED,
    )
    assert plan.runnable is False
    assert plan.method.elevates is Elevation.SUDO_PRINTED_ONLY
    assert "sudo" in plan.payload()["copy_command"]
    assert "never runs" in plan.refused


def test_plans_name_the_interpreter_and_never_a_shell_string(monkeypatch, tmp_path):
    monkeypatch.setattr(
        agent_installer,
        "_interpreter_path",
        lambda interpreter, machine, environ: f"/fake/{interpreter.value}",
    )
    env = _env(tmp_path / "bin", tmp_path)
    for target in install_targets():
        for os_ in InstallOs:
            machine = Machine(os_, "x64")
            for method in methods_for_os(target.methods, os_, "x64"):
                if method.kind is not InstallKind.SCRIPT:
                    continue
                plan = build_plan(
                    target, method.id, machine, NODE_OK, env, state=NOT_INSTALLED
                )
                run = next(step for step in plan.steps if step.kind == "run")
                assert method.interpreter is not None
                assert run.command[0] == f"/fake/{method.interpreter.value}"
                assert agent_installer.DOWNLOADED in run.command
                assert not {"-c", "-Command", "iex", "|"} & set(run.command)
                if method.interpreter is Interpreter.POWERSHELL:
                    assert "-File" in run.command
