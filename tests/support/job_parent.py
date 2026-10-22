"""A stand-in server for the Job Object test (7.108.0): it puts one child in a
kill-on-close job, prints the child's pid, and waits to be killed hard.

Usage: job_parent.py <fake_wireproxy.py> <config>
"""

import subprocess
import sys
import time

from my_claude_code.runtime.proxy_runner_process import KillOnCloseJob, popen_flags


def main() -> int:
    job = KillOnCloseJob()
    creationflags, new_session = popen_flags()
    child = subprocess.Popen(
        [sys.executable, sys.argv[1], "-c", sys.argv[2]],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        start_new_session=new_session,
    )
    assigned = job.assign(child.pid)
    print(f"{child.pid} {int(assigned)} {job.problem}", flush=True)
    time.sleep(120)
    return 0


if __name__ == "__main__":
    sys.exit(main())
