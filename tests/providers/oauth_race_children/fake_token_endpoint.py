"""A fake OAuth token endpoint on 127.0.0.1 that rotates refresh tokens once.

Started by ``tests/providers/test_oauth_refresh_race_process.py``. It binds
port 0, reports the port through ``<workdir>/endpoint.port`` and serves until
its stdin reaches EOF (the test closes it), then prints one JSON line.

Single-use rotation, like the real hosts: a refresh token that is valid right
now is spent the moment a POST presents it and a new pair is issued; a reused
or unknown refresh token gets ``400 {"error": "invalid_grant"}``. The outcome
is decided when the POST arrives, under one lock, and every POST is appended
to ``<workdir>/endpoint.log`` as a JSON line holding fingerprints only
(``sha256[:16]``), never a token.

The answer is held back for ``delay_seconds`` -- or, when ``hold_until`` names
a file, until that file exists -- so the POST window the race tests need is a
real one. ``<workdir>/inflight-<n>`` is written as each POST arrives, which is
how the fake Codex writer knows MCC is inside its window.

``mode`` ``claude`` answers the Anthropic shape; ``codex`` issues JWT-shaped
access and id tokens carrying an ``exp`` claim and the family's account id.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.support.token_host_block import install_token_host_block

install_token_host_block()


def main() -> int:
    import argparse
    import base64
    import hashlib
    import json
    import os
    import secrets
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from typing import Any

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    workdir = Path(config["workdir"])
    mode = str(config["mode"])
    delay = float(config.get("delay_seconds", 0.0))
    hold_until = config.get("hold_until")
    hold_timeout = float(config.get("hold_timeout", 20.0))
    expires_in = int(config.get("expires_in", 3600))
    log_path = workdir / "endpoint.log"

    def fingerprint(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]

    def b64url(document: dict[str, Any]) -> str:
        raw = json.dumps(document, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def jwt(claims: dict[str, Any]) -> str:
        return f"{b64url({'alg': 'none', 'typ': 'JWT'})}.{b64url(claims)}.sig"

    lock = threading.Lock()
    #: refresh token -> the account id of its family. Spent tokens are removed.
    valid: dict[str, str] = {
        str(token): str(account)
        for token, account in dict(config["valid_refresh_tokens"]).items()
    }
    events: list[dict[str, Any]] = []

    def issue(account: str, serial: int) -> dict[str, Any]:
        suffix = f"{serial}-{secrets.token_hex(6)}"
        refresh = f"fake-rt-{suffix}"
        if mode == "codex":
            now = int(time.time())
            auth = {"chatgpt_account_id": account}
            access = jwt(
                {
                    "exp": now + expires_in,
                    "iat": now,
                    "jti": suffix,
                    "https://api.openai.com/auth": auth,
                }
            )
            id_token = jwt(
                {
                    "exp": now + expires_in,
                    "iat": now,
                    "email": "race@example.invalid",
                    "https://api.openai.com/auth": auth,
                }
            )
            return {
                "access_token": access,
                "refresh_token": refresh,
                "id_token": id_token,
                "expires_in": expires_in,
                "token_type": "Bearer",
            }
        return {
            "access_token": f"fake-at-{suffix}",
            "refresh_token": refresh,
            "expires_in": expires_in,
            "token_type": "Bearer",
            "scope": "user:inference user:profile",
        }

    def hold_the_answer() -> None:
        if hold_until:
            marker = Path(str(hold_until))
            deadline = time.monotonic() + hold_timeout
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
        elif delay > 0:
            time.sleep(delay)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length > 0 else b""
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except ValueError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            presented = payload.get("refresh_token")
            presented = presented if isinstance(presented, str) else ""
            with lock:
                serial = len(events) + 1
                account = (
                    valid.pop(presented, None)
                    if payload.get("grant_type") == "refresh_token"
                    else None
                )
                issued: dict[str, Any] | None = None
                event: dict[str, Any] = {
                    "n": serial,
                    "path": self.path,
                    "client": self.headers.get("User-Agent") or "",
                    "presented_fp": fingerprint(presented),
                    "at": time.time(),
                }
                if account is None:
                    event["outcome"] = "invalid_grant"
                    event["status"] = 400
                else:
                    issued = issue(account, serial)
                    valid[str(issued["refresh_token"])] = account
                    event.update(
                        outcome="ok",
                        status=200,
                        account_id=account,
                        issued_access_fp=fingerprint(str(issued["access_token"])),
                        issued_refresh_fp=fingerprint(str(issued["refresh_token"])),
                    )
                events.append(event)
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event) + "\n")
            (workdir / f"inflight-{serial}").write_text("1", encoding="utf-8")
            hold_the_answer()
            if issued is None:
                status = 400
                answer: dict[str, Any] = {
                    "error": "invalid_grant",
                    "error_description": "refresh token already used or unknown",
                }
            else:
                status = 200
                answer = issued
            data = json.dumps(answer).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    port = int(server.server_address[1])
    serving = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}
    )
    serving.daemon = True
    serving.start()
    temporary = workdir / f"endpoint.port.{os.getpid()}.tmp"
    temporary.write_text(str(port), encoding="utf-8")
    os.replace(temporary, workdir / "endpoint.port")

    # Serve until the test closes stdin. A dead parent closes it too.
    sys.stdin.read()
    server.shutdown()
    server.server_close()
    with lock:
        print(json.dumps({"role": "endpoint", "port": port, "posts": events}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
