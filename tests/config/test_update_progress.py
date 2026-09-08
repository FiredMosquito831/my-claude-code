"""The update helper's liveness receipt, and who is allowed to believe it.

The question this module answers -- "is an installer running right now?" -- is
the one that decides whether the desktop shell starts a second `uv tool
install` into the tool directory a helper is already writing. It got the wrong
answer on 2026-09-07 because nobody was asking it: the shell read
``NotInstalled`` from a shim the helper had renamed aside and installed over
the top, the helper lost all five of its attempts, and the upgrade landed only
through the shell's emergency reinstall.
"""

import time

import pytest

from my_claude_code.config import update_progress
from my_claude_code.config.update_progress import (
    UPDATE_PROGRESS_FILENAME,
    UPDATE_PROGRESS_STAGES,
    UPDATE_STAGE_DIRNAME,
    active_update,
    helper_is_alive,
    read_update_progress,
    update_progress_path,
    update_report,
)


@pytest.fixture
def stage_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(update_progress, "config_dir_path", lambda: tmp_path)
    directory = tmp_path / UPDATE_STAGE_DIRNAME
    directory.mkdir(parents=True)
    return directory


def _write(stage_dir, *lines: str) -> None:
    (stage_dir / UPDATE_PROGRESS_FILENAME).write_text(
        "".join(line + "\n" for line in lines), encoding="utf-8"
    )


def test_the_path_hangs_off_the_configuration_directory(stage_dir) -> None:
    assert update_progress_path() == stage_dir / UPDATE_PROGRESS_FILENAME


def test_no_receipt_is_no_update_rather_than_an_error(tmp_path, monkeypatch) -> None:
    """The ordinary case, and the one that must not block a first install."""

    monkeypatch.setattr(update_progress, "config_dir_path", lambda: tmp_path)
    assert read_update_progress() is None
    assert active_update() is None
    assert update_report() is None


def test_the_last_parseable_line_wins(stage_dir) -> None:
    """A detached writer and a polling reader; a torn line costs one stage."""

    _write(
        stage_dir,
        '{"stage": "waiting-for-parent"}',
        '{"stage": "installing", "message": "Installing the new version."}',
        '{"stage": "star',
    )
    record = read_update_progress()
    assert record is not None
    assert record["stage"] == "installing"


def test_a_live_helper_is_an_update_in_flight(stage_dir, monkeypatch) -> None:
    monkeypatch.setattr(update_progress, "_pid_is_running", lambda pid: pid == 4242)
    _write(
        stage_dir,
        '{"stage": "installing", "helper_pid": 4242, "helper_done": false,'
        f' "version": "6.58.3", "started_at": {int(time.time() - 12)}}}',
    )
    record = active_update()
    assert record is not None
    assert record["stage"] == "installing"

    report = update_report()
    assert report is not None
    assert report["version"] == "6.58.3"
    assert report["helper_pid"] == 4242
    assert 12 <= report["elapsed_seconds"] < 60


def test_a_helper_whose_process_is_gone_is_not_in_flight(
    stage_dir, monkeypatch
) -> None:
    """The stage says 'installing' forever after a helper is killed.

    Which is why the process id is the fact and the stage is only narration:
    this is the case where an install by somebody else is the right answer.
    """

    monkeypatch.setattr(update_progress, "_pid_is_running", lambda pid: False)
    _write(
        stage_dir, '{"stage": "installing", "helper_pid": 4242, "helper_done": false}'
    )
    assert active_update() is None


def test_a_pid_that_cannot_be_checked_is_treated_as_alive(
    stage_dir, monkeypatch
) -> None:
    """ "I could not tell" must never be read as "the helper is gone"."""

    monkeypatch.setattr(update_progress, "_pid_is_running", lambda pid: None)
    _write(
        stage_dir,
        '{"stage": "installing", "helper_pid": 4242, "helper_done": false,'
        f' "started_at": {int(time.time())}}}',
    )
    assert active_update() is not None


def test_a_helper_that_says_it_is_done_is_finished_whatever_its_pid() -> None:
    """Windows recycles process ids, so a live pid is not a live helper."""

    assert not helper_is_alive(
        {"stage": "done", "helper_pid": 4242, "helper_done": True}
    )
    assert not helper_is_alive(
        {"stage": "recovered", "helper_pid": 4242, "helper_done": True}
    )


def test_an_unknown_pid_falls_back_to_the_start_time(monkeypatch) -> None:
    """A helper cannot heartbeat: it is single-threaded PowerShell inside uv.

    So the fallback has to cover a whole slow install, and anything past that
    is a receipt whose writer nobody can find.
    """

    monkeypatch.setattr(update_progress, "_pid_is_running", lambda pid: None)
    fresh = {"stage": "installing", "helper_pid": 1, "started_at": time.time() - 30}
    stale = {
        "stage": "installing",
        "helper_pid": 1,
        "started_at": time.time() - update_progress.STALE_HELPER_SECONDS - 1,
    }
    assert helper_is_alive(fresh)
    assert not helper_is_alive(stale)


def test_a_receipt_from_an_older_helper_is_judged_by_its_stage() -> None:
    """6.58.2 and earlier wrote no pid. An update under one still counts."""

    assert helper_is_alive({"stage": "installing"})
    assert not helper_is_alive({"stage": "done"})
    assert not helper_is_alive({"stage": "failed"})
    assert not helper_is_alive(None)


def test_recovered_is_part_of_the_stage_vocabulary() -> None:
    """The stage a failed install ends on now that it restarts the old server."""

    assert UPDATE_PROGRESS_STAGES[-1] == "recovered"
    assert "failed" in UPDATE_PROGRESS_STAGES


def test_this_processs_own_id_is_seen_as_running() -> None:
    """The liveness oracle itself, against the one pid known to be alive."""

    import os

    assert update_progress._pid_is_running(os.getpid()) is not False
    assert update_progress._pid_is_running(0) is False
    assert update_progress._pid_is_running(-1) is False
