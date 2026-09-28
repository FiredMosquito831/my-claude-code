"""One MCC server process, asking for its OAuth credential for a real request.

Started by ``tests/providers/test_oauth_refresh_race_process.py``, with
``HOME``, ``MCC_CONFIG_DIR``, ``CLAUDE_CONFIG_DIR`` and ``CODEX_HOME`` all
inside the test's ``tmp_path``. MCC's real code runs unmodified except for
three seams every child sets before the race starts:

* the token URLs point at the fake endpoint on 127.0.0.1 (its port is read
  from ``--port-file``);
* the lock waiting budget is shrunk (``--stale`` and friends) so a race costs
  seconds, not the real 60 s / 5 x 1-2 s;
* the POST function is wrapped to *count* calls -- it still posts.

Modes (``--mode``):

``claude``
    ``AnthropicOAuthAuth(account_id=...).current_tokens(purpose="request")``.
    Whether that credential is SHARED (the fallback to Claude Code's file, or
    an imported record) or NATIVE (MCC's own store) is decided by the files
    the test laid out, exactly as in production.
``codex``
    ``--codex-import`` first imports Codex's ``auth.json`` (which must never
    POST), then ``load_chatgpt_oauth_credentials(purpose="request")``.

The race starts when ``<sync>/go`` exists; ``--wait-for`` delays this child
until another marker exists too. Prints one JSON line: the final access
token's ``sha256[:16]``, the POSTs this process made and the decision codes it
logged. Never a token.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.support.token_host_block import install_token_host_block

install_token_host_block()


def main() -> int:
    import argparse
    import hashlib
    import json
    import time
    import traceback
    from collections.abc import Callable
    from typing import Any

    main_started = time.monotonic()
    root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(root / "src"))

    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("claude", "codex"), required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--port-file", required=True)
    parser.add_argument("--sync-dir", required=True)
    parser.add_argument("--account-id", default="")
    parser.add_argument("--wait-for", default="")
    parser.add_argument("--codex-import", action="store_true")
    parser.add_argument("--stale", type=float, required=True)
    parser.add_argument("--heartbeat", type=float, required=True)
    parser.add_argument("--retries", type=int, required=True)
    parser.add_argument("--retry-min", type=float, required=True)
    parser.add_argument("--retry-max", type=float, required=True)
    parser.add_argument("--liveness", type=float, required=True)
    args = parser.parse_args()
    sync = Path(args.sync_dir)

    result: dict[str, Any] = {
        "role": "mcc",
        "name": args.name,
        "mode": args.mode,
        "ok": False,
        "posts": 0,
        "statuses": [],
        "posts_before_go": 0,
        "decisions": [],
    }

    def fingerprint(token: str | None) -> str:
        return hashlib.sha256((token or "").encode("utf-8")).hexdigest()[:16]

    def wait_for(path: Path, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while not path.exists():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        return True

    def patch(owner: object, name: str, value: object) -> None:
        """``monkeypatch.setattr`` for a child: the seam must already exist."""
        if not hasattr(owner, name):
            raise AttributeError(f"{owner!r} has no seam {name!r}")
        setattr(owner, name, value)

    def claude_mode(base: str, timing: object) -> Callable[[], None]:
        """Point the Claude provider at the endpoint; return the race's call."""
        import asyncio

        import httpx

        from my_claude_code.providers.anthropic_oauth import credentials as creds
        from my_claude_code.providers.anthropic_oauth import shared
        from my_claude_code.providers.anthropic_oauth.auth import (
            AnthropicOAuthAuth,
        )

        patch(creds, "TOKEN_URL", f"{base}/v1/oauth/token")
        patch(creds, "LEGACY_TOKEN_URL", f"{base}/legacy/v1/oauth/token")
        # Bound by name at import in both modules, so both are patched.
        patch(creds, "REFRESH_LOCK_TIMING", timing)
        patch(shared, "REFRESH_LOCK_TIMING", timing)
        patch(shared, "LEGACY_LOCK_TIMING", timing)

        real_post = creds._post_refresh

        async def counting_post(refresh_token: str) -> httpx.Response:
            result["posts"] += 1
            response = await real_post(refresh_token)
            result["statuses"].append(response.status_code)
            return response

        patch(creds, "_post_refresh", counting_post)

        def run() -> None:
            auth = AnthropicOAuthAuth(account_id=args.account_id)
            tokens = asyncio.run(auth.current_tokens(purpose="request"))
            result["credential_mode"] = auth.mode
            result["final_access_fp"] = fingerprint(tokens.access_token)
            result["final_refresh_fp"] = fingerprint(tokens.refresh_token)

        return run

    def codex_mode(base: str, timing: object) -> Callable[[], None]:
        """Point the ChatGPT provider at the endpoint; import; return the call."""
        from my_claude_code.providers.chatgpt_oauth import credentials as creds

        patch(creds, "CODEX_OAUTH_TOKEN_URL", f"{base}/oauth/token")
        patch(creds, "REFRESH_LOCK_TIMING", timing)

        real_post = creds._refresh_access_token

        def counting_post(
            refresh_token: str,
        ) -> tuple[str, str | None, int | None, str | None]:
            result["posts"] += 1
            try:
                answer = real_post(refresh_token)
            except creds.ChatGPTOAuthRefreshError as error:
                result["statuses"].append(error.status_code)
                raise
            result["statuses"].append(200)
            return answer

        patch(creds, "_refresh_access_token", counting_post)
        if args.codex_import:
            creds.import_codex_cli_tokens()

        def run() -> None:
            credentials = creds.load_chatgpt_oauth_credentials(purpose="request")
            result["credential_mode"] = "shared"
            result["final_access_fp"] = fingerprint(credentials.access_token)
            result["final_refresh_fp"] = fingerprint(credentials.refresh_token)
            result["account_id"] = credentials.account_id

        return run

    try:
        from loguru import logger

        from my_claude_code.providers import oauth_file_lock

        # -- decisions: record_decision logs one line per code --------------
        prefix = "OAuth credential decision: "

        def capture(message: Any) -> None:
            text = str(message.record["message"])
            if text.startswith(prefix):
                result["decisions"].append(text[len(prefix) :].split(" ", 1)[0])

        logger.remove()
        logger.add(sys.stderr, level="WARNING")
        logger.add(capture, level="INFO", format="{message}")

        # -- a race in seconds: the lock budget, shrunk ---------------------
        timing = oauth_file_lock.LockTiming(
            stale_seconds=args.stale,
            heartbeat_seconds=args.heartbeat,
            retries=args.retries,
            retry_min_seconds=args.retry_min,
            retry_max_seconds=args.retry_max,
            backoff=False,
            liveness_seconds=args.liveness,
            liveness_poll_seconds=0.05,
        )
        patch(oauth_file_lock, "REFRESH_LOCK_TIMING", timing)

        port_file = Path(args.port_file)
        if not wait_for(port_file, 60.0):
            raise RuntimeError("the fake token endpoint never reported its port")
        base = f"http://127.0.0.1:{int(port_file.read_text(encoding='utf-8'))}"
        run = (
            claude_mode(base, timing)
            if args.mode == "claude"
            else codex_mode(base, timing)
        )

        result["posts_before_go"] = result["posts"]
        result["startup_seconds"] = round(time.monotonic() - main_started, 3)
        (sync / f"ready-{args.name}").write_text("1", encoding="utf-8")
        if not wait_for(sync / "go", 60.0):
            raise RuntimeError("the go file never appeared")
        if args.wait_for and not wait_for(Path(args.wait_for), 30.0):
            raise RuntimeError(f"{args.wait_for} never appeared")
        started = time.monotonic()
        run()
        result["elapsed"] = round(time.monotonic() - started, 3)
        result["ok"] = True
    except BaseException as error:
        result["error"] = f"{type(error).__name__}: {error}"
        traceback.print_exc()
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
