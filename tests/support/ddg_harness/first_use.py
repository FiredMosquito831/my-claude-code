"""Run the first-use timing probe (``first_use_probe.py``) in a fresh interpreter.

The page is one fixed recorded article (``PROBE_PAGE`` of the python-readability
corpus), written to ``workdir`` and served by the probe's own loopback server.
The child gets a scratch ``MCC_CONFIG_DIR`` under ``workdir``; it reads no real
configuration and reaches nothing but 127.0.0.1.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import corpus

PROBE_SCRIPT = Path(__file__).resolve().with_name("first_use_probe.py")
#: The article the probe fetches (a 2026 python.org release page, python-readability ``base``).
PROBE_PAGE = "base/python-315-alpha-6"


def probe_page() -> corpus.ExtractionPage:
    return next(page for page in corpus.readability_pages() if page.name == PROBE_PAGE)


def run_probe(workdir: Path) -> dict[str, Any]:
    page = probe_page()
    workdir.mkdir(parents=True, exist_ok=True)
    page_file = workdir / "probe_page.html"
    page_file.write_bytes(page.body)
    config_dir = workdir / "config"
    config_dir.mkdir(exist_ok=True)
    environment = dict(os.environ)
    environment["MCC_CONFIG_DIR"] = str(config_dir)
    completed = subprocess.run(
        [sys.executable, "-I", str(PROBE_SCRIPT), str(page_file), page.content_type],
        capture_output=True,
        check=False,
        text=True,
        env=environment,
        timeout=180,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"first-use probe failed: {completed.stderr or completed.stdout}"
        )
    result: dict[str, Any] = json.loads(completed.stdout.strip().splitlines()[-1])
    result["page"] = f"readability/{PROBE_PAGE} ({len(page.body)} bytes)"
    return result
