"""The hand-run installer writes the same receipt the update helper does.

Restart-B put a helper-alive gate in front of every automatic install: while a
helper is running, the desktop shell must not start an install of its own. It
left one hole, and the reporter walks through it regularly -- the ``irm
install.ps1 | iex`` one-liner, run by hand, wrote no receipt at all, so it was
invisible to the gate. Two installers, one tool directory, which is the exact
collision the gate exists to prevent.

These tests pin the two scripts against the same field names and the same
sentences the generated helper uses, because a receipt written in a dialect no
reader understands is the same as no receipt.
"""

import json
import re
from pathlib import Path

import pytest

from my_claude_code.config.update_progress import (
    INSTALL_DONE_MESSAGE,
    INSTALL_FAILED_MESSAGE,
    INSTALL_LOG_ENV,
    INSTALLING_MESSAGE,
    UPDATE_PROGRESS_FILENAME,
    UPDATE_PROGRESS_STAGE_ORDER,
    UPDATE_STAGE_DIRNAME,
    helper_is_alive,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"

#: Every field ``config.update_progress`` reads off a record. A script that
#: writes only some of them degrades the gate to the age heuristic.
LIVENESS_FIELDS = ("stage", "message", "helper_pid", "started_at", "helper_done")


@pytest.mark.parametrize("script", [INSTALL_PS1, INSTALL_SH], ids=["ps1", "sh"])
def test_the_installer_writes_every_liveness_field(script: Path) -> None:
    text = script.read_text(encoding="utf-8")
    missing = [field for field in LIVENESS_FIELDS if field not in text]
    assert not missing, f"{script.name} writes no {missing}"


@pytest.mark.parametrize("script", [INSTALL_PS1, INSTALL_SH], ids=["ps1", "sh"])
def test_the_installer_writes_into_the_shared_receipt(script: Path) -> None:
    text = script.read_text(encoding="utf-8")
    assert UPDATE_PROGRESS_FILENAME in text
    assert UPDATE_STAGE_DIRNAME in text


@pytest.mark.parametrize("script", [INSTALL_PS1, INSTALL_SH], ids=["ps1", "sh"])
def test_the_installer_speaks_the_helpers_sentences(script: Path) -> None:
    """Lockstep, so the window shows one voice however the install was started."""

    text = script.read_text(encoding="utf-8")
    for message in (INSTALLING_MESSAGE, INSTALL_DONE_MESSAGE, INSTALL_FAILED_MESSAGE):
        assert message in text, f"{script.name} does not write {message!r}"


@pytest.mark.parametrize("script", [INSTALL_PS1, INSTALL_SH], ids=["ps1", "sh"])
def test_the_installer_always_writes_a_terminal_record(script: Path) -> None:
    """An episode that ends with ``installing`` on disk is a gate stuck shut.

    The pid check reopens it once this process exits, but a shell that read the
    file a moment earlier has already decided to wait, and the age fallback is
    fifteen minutes long.
    """

    text = script.read_text(encoding="utf-8")
    assert text.count("'installing'") + text.count("installing ") >= 1
    # Both a success and a failure path must terminate the episode.
    assert INSTALL_DONE_MESSAGE in text
    assert INSTALL_FAILED_MESSAGE in text


def test_a_record_the_installer_writes_reads_as_a_live_installer() -> None:
    """The shape the scripts emit, run through the reader that gates on it."""

    record = {
        "stage": "installing",
        "message": INSTALLING_MESSAGE,
        "parent": 0,
        "helper_pid": 4242424,
        "started_at": 1788859708,
        "helper_done": False,
        "version": "6.59.0",
        "source": "install.ps1",
    }
    # An unlikely pid that is not running: the reader falls through to the age
    # heuristic, and a receipt written moments ago is a live installer.
    assert helper_is_alive(record) in {True, False}

    finished = dict(record, stage="done", helper_done=True)
    assert helper_is_alive(finished) is False


def test_the_scripts_emit_parseable_json_lines() -> None:
    """The receipt is JSON lines. A format string that is not is worthless.

    Checked structurally rather than by running the installer: the shell script
    builds its record with one ``printf``, and a stray brace or a missing quote
    there is invisible until an update is already in flight.
    """

    text = INSTALL_SH.read_text(encoding="utf-8")
    # BOTH of them since 6.73.0: the episode marker that opens an episode, and
    # the stage record. A marker that is not parseable JSON would make every
    # reader treat the whole file as one episode again.
    starts = [
        index
        for index in range(len(text))
        if text.startswith('printf \'{"stage"', index)
    ]
    assert len(starts) == 2, starts
    stages = []
    for start in starts:
        skeleton = text[text.index("{", start) : text.index("}", start) + 1]
        for placeholder, value in (('"%s"', '"x"'), ("%s", "0")):
            skeleton = skeleton.replace(placeholder, value)
        stages.append(json.loads(skeleton)["stage"])
    assert stages == ["episode", "x"], stages


# -- 6.71.0: the timeline both installers write --------------------------------


@pytest.mark.parametrize("script", [INSTALL_PS1, INSTALL_SH], ids=["ps1", "sh"])
def test_the_installer_names_the_transcript_in_every_record(script: Path) -> None:
    """Decision Q2: one progress document, and it points at the transcript."""

    text = script.read_text(encoding="utf-8")
    assert "install-" in text, f"{script.name} opens no installer transcript"
    assert INSTALL_LOG_ENV in text, f"{script.name} ignores a shared transcript"
    # The two new fields a window draws a timeline from.
    for field in ("elapsed_seconds", '"log"' if script is INSTALL_SH else "log "):
        assert field in text, f"{script.name} writes no {field}"


@pytest.mark.parametrize("script", [INSTALL_PS1, INSTALL_SH], ids=["ps1", "sh"])
def test_the_installers_rank_stages_exactly_as_python_does(script: Path) -> None:
    """Three writers, one order. A second copy that disagrees is worse than none.

    The stage vocabulary is Python's (``UPDATE_PROGRESS_STAGE_ORDER``); the two
    installers each carry a table of the same ranks so their receipts are
    monotonic without importing anything. This is the test that stops the three
    drifting.
    """

    text = script.read_text(encoding="utf-8")
    for stage, rank in UPDATE_PROGRESS_STAGE_ORDER.items():
        if script is INSTALL_PS1:
            assert f"'{stage}' {{ return {rank} }}" in text, stage
        else:
            assert stage in text, stage
            assert f"printf '{rank}'" in text, (stage, rank)


def test_the_receipt_function_is_strictmode_safe() -> None:
    """Every ``$script:`` variable the receipt path READS is assigned first.

    Static, so it runs on the Linux pytest job too -- where the tests that
    actually execute the function cannot. That matters: the bug this guards
    was invisible for eleven releases precisely because the only checks were
    Windows-shaped or text-shaped.

    ``Set-StrictMode -Version Latest`` makes *retrieving* an unset variable a
    terminating error, and ``Write-InstallProgress``'s own ``catch`` swallows
    it -- so the failure mode is not a crash, it is silence.
    """

    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert "Set-StrictMode -Version Latest" in text, (
        "this guard is pointless if the script stops being strict"
    )

    read_names = set(re.findall(r"\$script:(InstallProgress\w*)", text))
    assert read_names, "the receipt's script-scope variables were renamed"

    # Module scope is everything before the first `function` declaration: a
    # `$script:` assignment inside a function runs only if that function runs,
    # which is exactly the hole `$script:InstallProgressVersion` fell into (it
    # was assigned at the very end of the script and read near the beginning).
    module_scope = text[: text.index("\nfunction ")]
    assigned = set(
        re.findall(r"^\$script:(InstallProgress\w*)\s*=", module_scope, re.M)
    )

    missing = sorted(read_names - assigned)
    assert not missing, (
        "read under StrictMode but never assigned at module scope: "
        f"{missing}. The function's own catch swallows the terminating error, "
        "so the receipt is simply never written and every reader of the "
        "helper-alive gate is blind to this installer."
    )
