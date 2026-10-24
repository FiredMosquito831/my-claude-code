"""Waiting for the Install button's job threads: on the thread, not on a field.

``agent_installer._finish`` publishes the job's ``stage`` first and frees the
one-install slot (``_ACTIVE``) and the lock file after it, so a test that waits
for ``stage`` to leave "running" and then starts the next job can meet the
first one still in the slot ("... is being installed. One install runs at a
time.": three Windows suite runs). The worker thread ending is the one signal
that comes after all of it.

And a job a test started and never waited for keeps running into the next
test: its ``_finish`` resolves the config directory while HOME is being put
back, and the next test's routes write into the real home -- the HOME leak
behind three 10,000-error Windows suite runs. ``join_leftover_install_jobs``
is called by the autouse ``_reset_agent_install_jobs`` fixture at teardown.
"""

import threading
from typing import Any

#: The prefix ``agent_installer.start_job`` names its worker threads with.
WORKER_PREFIX = "agent-install-"

#: How long a test may wait for one job's thread. A hang guard, not a timing:
#: every fake in the suite finishes in well under a second.
HANG_BOUND_SECONDS = 120.0


def worker_of(job: Any) -> threading.Thread | None:
    """The job's worker thread, or ``None`` once it has ended."""

    name = f"{WORKER_PREFIX}{job.id[:8]}"
    return next(
        (thread for thread in threading.enumerate() if thread.name == name), None
    )


def wait_for_job(job: Any, *, seconds: float = HANG_BOUND_SECONDS) -> Any:
    """Return ``job`` once its worker thread has ended (stage set, slot free)."""

    worker = worker_of(job)
    if worker is not None:
        worker.join(seconds)
        assert not worker.is_alive(), (
            f"the install job's thread was still running after {seconds} s",
            job.payload(),
        )
    assert job.stage != "running", job.payload()
    return job


def join_leftover_install_jobs(*, seconds: float = HANG_BOUND_SECONDS) -> list[str]:
    """Join every install worker still alive; return the names that outlived it."""

    leftovers = [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith(WORKER_PREFIX)
    ]
    for thread in leftovers:
        thread.join(seconds)
    return [thread.name for thread in leftovers if thread.is_alive()]
