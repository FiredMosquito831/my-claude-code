"""Fake command-line programs for the Install-button tests (7.102.0).

Spec §8.1: "a PATH holding only a temp ``bin`` with fake ``node``/``npm``/
``npm.cmd``/``claude`` scripts that record their argv to a file". Each fake is
a tiny Python program behind a platform launcher (``<name>.cmd`` on Windows,
an executable ``sh`` script elsewhere), so the code under test finds it with
``shutil.which`` exactly as it would find the real one, runs it by path, and
reads what it prints. What every fake does is data in ``<name>.json`` beside
it:

``outputs``   first argument -> the text printed (``"--version": "v22.1.0"``)
``default``   printed for any other argument list
``exit``      the exit code (``exit_for`` maps a first argument to one)
``drop``      ``[path, content]`` pairs written when it runs -- an installer
              that "installs" a program
``sleep``     seconds to sleep before exiting
``spawn``     a child to start (and record) before sleeping, for the PID-tree
              cancel test

Every run appends ``{"argv": [...], "pid": ...}`` to ``calls.jsonl`` in the
bin folder, so a test can assert what ran and in what order.
"""

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

_PROGRAM = r"""
import json, os, subprocess, sys, time
here = os.path.dirname(os.path.abspath(__file__))
name = os.path.splitext(os.path.basename(__file__))[0]
with open(os.path.join(here, name + ".json"), encoding="utf-8") as handle:
    spec = json.load(handle)
argv = sys.argv[1:]
with open(os.path.join(here, "calls.jsonl"), "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"program": name, "argv": argv, "pid": os.getpid()}) + "\n")
for target, content in spec.get("drop", []):
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)
    if not target.endswith(".cmd"):
        os.chmod(target, 0o755)
first = argv[0] if argv else ""
text = spec.get("outputs", {}).get(first, spec.get("default", ""))
if text:
    print(text, flush=True)
child = spec.get("spawn")
if child:
    process = subprocess.Popen([sys.executable, "-I", "-c", child["code"]])
    with open(child["pid_file"], "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"child": os.getpid(), "grandchild": process.pid}))
time.sleep(float(spec.get("sleep", 0)))
sys.exit(int(spec.get("exit_for", {}).get(first, spec.get("exit", 0))))
"""


def launcher_name(name: str) -> str:
    """Return the file name ``shutil.which(name)`` finds on this platform."""

    return f"{name}.cmd" if os.name == "nt" else name


def write_fake_program(bin_dir: Path, name: str, **spec: Any) -> Path:
    """Create one fake program in ``bin_dir`` and return its launcher's path."""

    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / f"{name}.py").write_text(_PROGRAM, encoding="utf-8")
    (bin_dir / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")
    launcher = bin_dir / launcher_name(name)
    script = bin_dir / f"{name}.py"
    if os.name == "nt":
        launcher.write_text(
            f'@"{sys.executable}" -I "{script}" %*\r\n', encoding="utf-8", newline=""
        )
    else:
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" -I "{script}" "$@"\n',
            encoding="utf-8",
        )
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return launcher


def fake_launcher_text(script: Path) -> str:
    """Return a launcher body for a fake program another fake "installs"."""

    if os.name == "nt":
        return f'@"{sys.executable}" -I "{script}" %*\r\n'
    return f'#!/bin/sh\nexec "{sys.executable}" -I "{script}" "$@"\n'


def calls(bin_dir: Path) -> list[dict[str, Any]]:
    """Return every recorded run in ``bin_dir``, oldest first."""

    path = bin_dir / "calls.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
