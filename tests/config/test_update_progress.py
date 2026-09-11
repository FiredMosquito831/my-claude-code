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


# -- 6.71.0: monotonic stages and the installer transcript ---------------------


def test_the_new_stages_are_part_of_the_vocabulary() -> None:
    """``stopping``, ``verifying`` and ``handing-off`` (decision Q2)."""

    for stage in ("stopping", "verifying", "handing-off"):
        assert stage in UPDATE_PROGRESS_STAGES, stage
    # ``recovered`` stays last: it is the stage a failed install ends on.
    assert UPDATE_PROGRESS_STAGES[-1] == "recovered"


def test_stages_are_monotonic_within_one_episode() -> None:
    """An episode only moves forward, so a window can draw it as a timeline.

    The vocabulary tuple and the rank table have to agree, or a writer would
    silently drop a stage a reader is waiting for.
    """

    from my_claude_code.config.update_progress import (
        UPDATE_PROGRESS_STAGE_ORDER,
        stage_rank,
    )

    assert set(UPDATE_PROGRESS_STAGE_ORDER) == set(UPDATE_PROGRESS_STAGES)
    ranks = [stage_rank(stage) for stage in UPDATE_PROGRESS_STAGES]
    assert ranks == sorted(ranks), list(zip(UPDATE_PROGRESS_STAGES, ranks, strict=True))
    # The terminal stages share the last rank: an episode ends once, and
    # ``failed`` may be followed by ``recovered``.
    assert stage_rank("failed") == stage_rank("recovered") == stage_rank("done")
    # ``handing-off`` is where ``starting`` would be, because they are the same
    # moment told from the two sides of decision Q4.
    assert stage_rank("handing-off") == stage_rank("starting")
    # An unknown stage ranks below every known one, so a guard never drops a
    # record it cannot place.
    assert stage_rank("something-a-later-release-invents") == 0


def test_handing_off_does_not_end_the_episode() -> None:
    """The helper is still running when it writes one.

    Reading it as terminal would reopen the "one installer at a time" gate a
    beat before the installer actually stopped.
    """

    from my_claude_code.config.update_progress import UPDATE_TERMINAL_STAGES

    assert "handing-off" not in UPDATE_TERMINAL_STAGES
    assert {"done", "failed", "recovered"} == UPDATE_TERMINAL_STAGES
    assert helper_is_alive({"stage": "handing-off"})
    assert helper_is_alive({"stage": "verifying"})


def test_the_report_names_the_transcript_a_window_can_tail(
    monkeypatch, tmp_path
) -> None:
    """Decision Q2's "see everything happening", from the Python side."""

    import json
    import os

    stage_dir = tmp_path / "updates"
    stage_dir.mkdir(parents=True)
    record = {
        "stage": "installing",
        "message": "Installing the new version.",
        "helper_pid": os.getpid(),
        "started_at": 1788859708,
        "elapsed_seconds": 24.757,
        "helper_done": False,
        "version": "6.71.0",
        "log": str(stage_dir / "install-20260911-082114.log"),
    }
    (stage_dir / "progress.json").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        update_progress, "config_dir_path", lambda: tmp_path, raising=True
    )

    report = update_progress.update_report()
    assert report is not None
    assert report["stage"] == "installing"
    assert report["log"] == str(stage_dir / "install-20260911-082114.log")
    assert report["version"] == "6.71.0"

    # A record from a build that named no transcript reports none rather than
    # a path it made up.
    (stage_dir / "progress.json").write_text(
        json.dumps({k: v for k, v in record.items() if k != "log"}) + "\n",
        encoding="utf-8",
    )
    without_log = update_progress.update_report()
    assert without_log is not None
    assert without_log["log"] is None


def test_the_transcript_lives_beside_the_receipt(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        update_progress, "config_dir_path", lambda: tmp_path, raising=True
    )
    path = update_progress.install_log_path("20260911-082114")
    assert path.name == "install-20260911-082114.log"
    assert path.parent == tmp_path / "updates"
    assert path.parent == update_progress.update_progress_path().parent
