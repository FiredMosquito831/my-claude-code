"""A child process that holds a log file open, to produce a real WinError 32.

Started by ``tests/config/test_logging_config_held_rotation.py``. Opens the
given path for append -- which on Windows denies a rename of it from another
process, because plain ``open()`` does not request ``FILE_SHARE_DELETE`` --
writes a marker, signals readiness by creating ``ready_path``, and holds the
handle open until ``stop_path`` appears or 30 s pass.
"""

import sys
import time


def main() -> int:
    log_path, ready_path, stop_path = sys.argv[1], sys.argv[2], sys.argv[3]

    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write("held by child\n")
        handle.flush()
        with open(ready_path, "w", encoding="utf-8") as ready:
            ready.write("ready\n")
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            try:
                with open(stop_path, encoding="utf-8"):
                    break
            except OSError:
                time.sleep(0.05)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
