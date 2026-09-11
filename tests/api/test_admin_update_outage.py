"""What the dashboard says while the server it is talking to is replaced.

Spec F7: the update deliberately stops the server, and a dashboard in an
ordinary browser tab has no other channel -- it cannot read a file on the disk
and there is nothing left to poll. Until 6.71.0 it set one button label,
"Updating... (this can take a few minutes)", and then went silent for the whole
outage, which is indistinguishable from a page that has hung.

It now hands over the two paths the installer is writing *before* the server
stops, and says plainly that the desktop app is the thing that can show the
install happening. This test RUNS that function in node rather than grepping
for it: a sentence assembled by string concatenation is exactly the kind of
code that is green in a grep and wrong on the screen.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ADMIN_JS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "my_claude_code"
    / "api"
    / "admin_static"
    / "admin.js"
)


def _extract(name: str) -> str:
    text = ADMIN_JS.read_text(encoding="utf-8")
    start = text.index(f"function {name}(")
    depth = 0
    seen = False
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
            seen = True
        elif text[index] == "}":
            depth -= 1
            if seen and depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"{name} is not closed")


def _call(result: dict) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    script = f"{_extract('describeUpdateOutage')}\nconsole.log(JSON.stringify(describeUpdateOutage({json.dumps(result)})));\n"
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return json.loads(completed.stdout)


def test_the_banner_names_both_files_before_the_server_stops() -> None:
    text = _call(
        {
            "ok": True,
            "log_path": "C:/config/updates/install-20260911-082114.log",
            "progress_path": "C:/config/updates/progress.json",
        }
    )
    assert "C:/config/updates/install-20260911-082114.log" in text
    assert "C:/config/updates/progress.json" in text
    # And it is honest about what this tab cannot do, rather than looking hung.
    assert "cannot read those files" in text
    assert "desktop app" in text


def test_a_response_with_no_paths_says_nothing_rather_than_a_blank_heading() -> None:
    """The POSIX path installs in-process: there is no transcript to name."""

    assert _call({"ok": True}) == ""
    assert _call({"ok": True, "log_path": None, "progress_path": None}) == ""


def test_one_path_alone_is_still_worth_showing() -> None:
    text = _call({"ok": True, "log_path": "/home/u/.mcc/updates/install-1.log"})
    assert "/home/u/.mcc/updates/install-1.log" in text
    assert "progress.json" not in text


def test_the_reconnect_wait_reports_a_countdown() -> None:
    """A browser tab can only count down, so it must actually do it.

    The callback is what turns the button from a frozen sentence into a
    number that changes, which is the only honest signal this page has left.
    """

    source = _extract("waitForUpdatedServer")
    assert "onCountdown" in source, source[:400]
    assert 'typeof onCountdown === "function"' in source
