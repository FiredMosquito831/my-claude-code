"""A fake Claude Code that refreshes its OAuth token the way 2.1.283 does.

Started by ``tests/providers/test_oauth_refresh_race_process.py``. It speaks
the real ``proper-lockfile`` protocol (see
``src/my_claude_code/providers/oauth_file_lock.py``): every lock is a
directory made with ``mkdir``, its mtime is the heartbeat, and a lock is
broken only when its mtime is older than ``stale``.

1. Re-read the credential file; stop (``race_resolved``) if the access token
   changed since this process last read it.
2. Take ``<dir>/.oauth_refresh.lock``, then the legacy
   ``<realpath(dir)>.lock``; if the legacy one is busy, release the first and
   treat the attempt as ELOCKED. On ELOCKED retry a few times, then watch a
   short liveness window, then give up (``lock_busy``) without a POST.
3. Heartbeat both lock directories from a thread. A heartbeat that finds a
   directory gone or touched by somebody else marks the lock compromised
   (``onCompromised``); a compromised holder never saves.
4. Write the ``.oauth_refresh.lock.owner`` pid record, re-read the file under
   the lock and stop if the access token changed; otherwise POST.
5. Take ``<dir>/.storage-write.lock`` and compare-and-swap: write only while
   ``claudeAiOauth.refreshToken`` is still ``""`` or the one posted (3 tries,
   ``100 * i`` ms apart); otherwise keep the file (``adopted_sibling``).
6. Release the owner record, the legacy lock, then the refresh lock -- only
   after the write.

Prints one JSON line. Tokens appear only as ``sha256[:16]`` fingerprints.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.support.token_host_block import install_token_host_block

install_token_host_block()


def main() -> int:
    import argparse
    import contextlib
    import hashlib
    import json
    import os
    import random
    import threading
    import time
    import urllib.error
    import urllib.request
    from typing import Any

    parser = argparse.ArgumentParser()
    parser.add_argument("--claude-dir", required=True)
    parser.add_argument("--sync-dir", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--port-file", required=True)
    parser.add_argument("--stale", type=float, default=60.0)
    parser.add_argument("--heartbeat", type=float, default=5.0)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--retry-min", type=float, default=1.0)
    parser.add_argument("--retry-max", type=float, default=2.0)
    parser.add_argument("--liveness", type=float, default=7.5)
    args = parser.parse_args()

    claude_dir = Path(args.claude_dir)
    sync = Path(args.sync_dir)
    credentials = claude_dir / ".credentials.json"
    refresh_lock = claude_dir / ".oauth_refresh.lock"
    legacy_lock = Path(os.path.realpath(claude_dir) + ".lock")
    storage_lock = claude_dir / ".storage-write.lock"
    owner_record = claude_dir / ".oauth_refresh.lock.owner"
    port_file = Path(args.port_file)

    result: dict[str, Any] = {
        "role": "claude-code",
        "name": args.name,
        "posts": 0,
        "statuses": [],
        "outcome": "",
        "compromised": False,
        "heartbeats": 0,
        "held_seconds": 0.0,
    }

    def fingerprint(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]

    def wait_for(path: Path, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while not path.exists():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        return True

    if not wait_for(port_file, 60.0):
        result["outcome"] = "no_endpoint"
        print(json.dumps(result))
        return 1
    port = int(port_file.read_text(encoding="utf-8"))
    token_url = f"http://127.0.0.1:{port}/v1/oauth/token"

    def read_document() -> dict[str, Any]:
        for _ in range(50):
            try:
                with credentials.open("r", encoding="utf-8") as handle:
                    document = json.load(handle)
                return document if isinstance(document, dict) else {}
            except PermissionError:
                time.sleep(0.01)
            except FileNotFoundError, ValueError:
                return {}
        return {}

    def read_block() -> dict[str, Any]:
        block = read_document().get("claudeAiOauth")
        return dict(block) if isinstance(block, dict) else {}

    def write_atomically(target: Path, text: str) -> None:
        temporary = target.with_name(f"{target.name}.cc-{os.getpid()}.tmp")
        temporary.write_text(text, encoding="utf-8")
        for attempt in range(50):
            try:
                os.replace(temporary, target)
                return
            except PermissionError:
                # Windows refuses a rename over a file another process has
                # open this instant; Claude Code's own writer retries too.
                if attempt == 49:
                    raise
                time.sleep(0.01)

    def try_mkdir_lock(path: Path, stale: float) -> bool:
        """proper-lockfile's acquire: mkdir, else break it only when stale."""
        try:
            path.mkdir()
            return True
        except FileExistsError:
            pass
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            # Released between the mkdir and the stat: one more try.
            try:
                path.mkdir()
                return True
            except OSError:
                return False
        if age <= stale:
            return False
        try:
            path.rmdir()
            path.mkdir()
            return True
        except OSError:
            return False

    def release(path: Path) -> None:
        with contextlib.suppress(OSError):
            path.rmdir()

    class Heartbeat:
        """Touch the held lock directories; notice anybody else touching them."""

        def __init__(self, paths: list[Path], interval: float) -> None:
            self.paths = paths
            self.interval = interval
            self.compromised = False
            self.beats = 0
            self._stop = threading.Event()
            self._last = {path: path.stat().st_mtime_ns for path in paths}
            self._thread = threading.Thread(target=self._run, name="cc-heartbeat")
            self._thread.daemon = True

        def start(self) -> None:
            self._thread.start()

        def _run(self) -> None:
            while not self._stop.wait(self.interval):
                for path in self.paths:
                    try:
                        current = path.stat().st_mtime_ns
                    except OSError:
                        self.compromised = True
                        continue
                    if current != self._last[path]:
                        self.compromised = True
                        continue
                    try:
                        os.utime(path, None)
                        self._last[path] = path.stat().st_mtime_ns
                    except OSError:
                        self.compromised = True
                self.beats += 1

        def stop(self) -> None:
            self._stop.set()
            self._thread.join(timeout=5.0)

    def post(refresh_token: str) -> tuple[int, dict[str, Any]]:
        body = json.dumps(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": "fake-claude-code-client",
                "scope": "user:inference user:profile",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            token_url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "fake-claude-code",
            },
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=30) as response:
                return int(response.status), json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            return int(error.code), json.loads(error.read() or b"{}")

    def cas_save(posted: str, answer: dict[str, Any]) -> str:
        """``saveRefreshedOAuthTokensRespectingLock``: 3 tries, CAS on refresh."""
        for attempt in range(3):
            if attempt > 0:
                time.sleep(0.1 * attempt)
            acquired = False
            # ``.storage-write``: retries 10, 100 ms doubling to 1 s, stale 15 s.
            for wait in (0.0, 0.1, 0.2, 0.4, 0.8, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0):
                time.sleep(wait)
                if try_mkdir_lock(storage_lock, 15.0):
                    acquired = True
                    break
            if not acquired:
                continue
            try:
                document = read_document()
                block = document.get("claudeAiOauth")
                if not isinstance(block, dict):
                    continue
                if block.get("refreshToken") not in ("", posted):
                    return "adopted_sibling"
                now_ms = int(time.time() * 1000)
                block.update(
                    {
                        "accessToken": answer["access_token"],
                        "refreshToken": answer.get("refresh_token") or posted,
                        "expiresAt": now_ms
                        + int(answer.get("expires_in", 3600)) * 1000,
                    }
                )
                document["claudeAiOauth"] = block
                write_atomically(credentials, json.dumps(document, indent=2))
                return "saved"
            finally:
                release(storage_lock)
        return "save_failed"

    def run_locked(entry_access: str) -> str:
        """Both refresh locks are held: heartbeat, re-read, POST, CAS, release."""
        started = time.monotonic()
        heartbeat = Heartbeat([refresh_lock, legacy_lock], args.heartbeat)
        heartbeat.start()
        try:
            write_atomically(
                owner_record,
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "pidDomain": "fake",
                        "pidSpace": "fake",
                        "lockBirthtimeMs": time.time() * 1000,
                    }
                ),
            )
            (sync / f"holding-{args.name}").write_text("1", encoding="utf-8")
            under = read_block()
            if str(under.get("accessToken") or "") != entry_access:
                return "race_resolved"
            posted = str(under.get("refreshToken") or "")
            status, answer = post(posted)
            result["posts"] += 1
            result["statuses"].append(status)
            if status != 200:
                return "invalid_grant" if status == 400 else f"http_{status}"
            if heartbeat.compromised:
                return "lock_compromised"
            saved = cas_save(posted, answer)
            return "refreshed" if saved == "saved" else saved
        finally:
            heartbeat.stop()
            result["compromised"] = heartbeat.compromised
            result["heartbeats"] = heartbeat.beats
            with contextlib.suppress(OSError):
                owner_record.unlink()
            release(legacy_lock)
            release(refresh_lock)
            result["held_seconds"] = round(time.monotonic() - started, 3)

    (sync / f"ready-{args.name}").write_text("1", encoding="utf-8")
    if not wait_for(sync / "go", 60.0):
        result["outcome"] = "no_go"
        print(json.dumps(result))
        return 1

    entry = read_block()
    entry_access = str(entry.get("accessToken") or "")
    expires_at_ms = entry.get("expiresAt")
    if (
        isinstance(expires_at_ms, (int, float))
        and time.time() * 1000 + 300_000 < expires_at_ms
    ):
        result["outcome"] = "not_needed"
    else:
        attempt = 0
        deadline: float | None = None
        while True:
            # Claude Code re-reads before every lock attempt (``Sa`` recurses).
            if str(read_block().get("accessToken") or "") != entry_access:
                result["outcome"] = "race_resolved"
                break
            locked = False
            if try_mkdir_lock(refresh_lock, args.stale):
                if try_mkdir_lock(legacy_lock, args.stale):
                    locked = True
                else:
                    release(refresh_lock)
            if locked:
                result["outcome"] = run_locked(entry_access)
                break
            if attempt < args.retries:
                attempt += 1
                time.sleep(random.uniform(args.retry_min, args.retry_max))
                continue
            if deadline is None:
                deadline = time.monotonic() + args.liveness
            if time.monotonic() >= deadline:
                result["outcome"] = "lock_busy"
                break
            time.sleep(0.1)

    final = read_block()
    result["final_access_fp"] = fingerprint(str(final.get("accessToken") or ""))
    result["final_refresh_fp"] = fingerprint(str(final.get("refreshToken") or ""))
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
