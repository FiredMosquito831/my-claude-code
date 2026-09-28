"""A fake Codex that rewrites ``auth.json`` inside MCC's refresh POST window.

Started by ``tests/providers/test_oauth_refresh_race_process.py``. Codex has
no lock on ``auth.json`` (spec rule 6), so there is nothing to join: this
process waits for the fake token endpoint to report MCC's POST in flight
(``<endpoint dir>/inflight-1``), rewrites ``tokens`` while the endpoint holds
its answer back, and only then lets the endpoint answer (``release``).

Scenarios:

``newer``
    The same account signs in again: a newer token family, same
    ``account_id``.
``foreign``
    The user switched accounts in Codex: a different ``account_id``.
``none``
    The control: Codex touches nothing, so MCC's own write-back must land.

Prints one JSON line: fingerprints and the ``sha256`` of the bytes written,
never a token.
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
    import time
    from typing import Any

    parser = argparse.ArgumentParser()
    parser.add_argument("--auth-json", required=True)
    parser.add_argument("--sync-dir", required=True)
    parser.add_argument("--endpoint-dir", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--scenario", choices=("newer", "foreign", "none"))
    parser.add_argument("--foreign-account", default="acct-foreign")
    args = parser.parse_args()

    auth_json = Path(args.auth_json)
    sync = Path(args.sync_dir)
    endpoint_dir = Path(args.endpoint_dir)
    result: dict[str, Any] = {
        "role": "codex",
        "name": args.name,
        "scenario": args.scenario,
        "wrote": False,
        "saw_inflight": False,
    }

    def fingerprint(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]

    def b64url(document: dict[str, Any]) -> str:
        raw = json.dumps(document, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def jwt(claims: dict[str, Any]) -> str:
        return f"{b64url({'alg': 'none', 'typ': 'JWT'})}.{b64url(claims)}.sig"

    def wait_for(path: Path, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while not path.exists():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        return True

    def read_document() -> dict[str, Any]:
        for _ in range(50):
            try:
                document = json.loads(auth_json.read_text(encoding="utf-8"))
                return document if isinstance(document, dict) else {}
            except PermissionError:
                time.sleep(0.01)
        return {}

    def write_atomically(data: bytes) -> None:
        temporary = auth_json.with_name(f"auth.json.codex-{os.getpid()}.tmp")
        temporary.write_bytes(data)
        for attempt in range(50):
            try:
                os.replace(temporary, auth_json)
                return
            except PermissionError:
                if attempt == 49:
                    raise
                time.sleep(0.01)

    (sync / f"ready-{args.name}").write_text("1", encoding="utf-8")
    if not wait_for(sync / "go", 60.0):
        result["error"] = "no go"
        print(json.dumps(result))
        return 1
    try:
        result["saw_inflight"] = wait_for(endpoint_dir / "inflight-1", 30.0)
        if result["saw_inflight"] and args.scenario != "none":
            document = read_document()
            tokens = document.get("tokens")
            tokens = dict(tokens) if isinstance(tokens, dict) else {}
            account = (
                args.foreign_account
                if args.scenario == "foreign"
                else str(tokens.get("account_id") or "")
            )
            now = int(time.time())
            auth = {"chatgpt_account_id": account}
            suffix = secrets.token_hex(6)
            tokens.update(
                {
                    "access_token": jwt(
                        {
                            "exp": now + 7200,
                            "iat": now,
                            "jti": f"codex-{suffix}",
                            "https://api.openai.com/auth": auth,
                        }
                    ),
                    "refresh_token": f"codex-rt-{args.scenario}-{suffix}",
                    "id_token": jwt(
                        {
                            "exp": now + 7200,
                            "iat": now,
                            "email": "codex@example.invalid",
                            "https://api.openai.com/auth": auth,
                        }
                    ),
                    "account_id": account,
                }
            )
            document["tokens"] = tokens
            document["last_refresh"] = "2026-09-28T12:00:00Z"
            data = json.dumps(document, indent=2).encode("utf-8")
            write_atomically(data)
            result.update(
                wrote=True,
                file_sha=hashlib.sha256(data).hexdigest(),
                access_fp=fingerprint(str(tokens["access_token"])),
                refresh_fp=fingerprint(str(tokens["refresh_token"])),
                account_id=account,
            )
    finally:
        # Let the endpoint answer MCC now, whatever happened above.
        (endpoint_dir / "release").write_text("1", encoding="utf-8")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
