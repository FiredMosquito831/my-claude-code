"""A small request log built to exercise free-text search (7.91.1 tests).

Written through the real writer, so every column, body, attempt and origin is
what the server stores. It holds what a search answer can be sensitive to:
legacy inline rows and compressed bodies, non-ASCII text with both cases of a
breve, quotes, backslashes, ``%`` and ``_``, terms that occur only inside tool
calls, rows sharing a timestamp (so ordering is decided by the plan alone),
every status and sub-label, local answers, several providers, harnesses and
keys, sessions and folders, and exits on attempts.
"""

import random
from pathlib import Path

from my_claude_code.core.request_log import (
    RequestLogStore,
    RequestRecord,
    RequestStatus,
    RouteAttempt,
    RouteAttemptOutcome,
)

BASE_TS = 1_760_000_000.0

WORDS = (
    "proxy",
    "server",
    "request",
    "search",
    "bodies",
    "window",
    "dashboard",
    "answer",
    "stream",
    "token",
    "rotation",
    "harness",
    "folder",
    "session",
    "export",
    "index",
    "worker",
    "thread",
    "pool",
    "commit",
    "README.md",
    "readme",
    "ReadMe",
    "config",
    "model",
    "provider",
    "attempt",
    "ladder",
    "exit",
    "chain",
    "x",
    "qa",
    "ok",
    "id",
    "->",
    "a.b",
    "a-b",
)
SPICE = (
    "too long",
    "TODO",
    "Traceback (most recent call last):",
    "error 429",
    "proxy 8082",
    "ReadMe.MD",
    "mod_name",
    "100% done",
    'say "hi" to it',
    "back\\slash",
    "done — tests pass",
    "a → b",
    "wait…",
    "✓ passed",
    "Mulțumesc, acum funcționează.",
    "Ăsta e mesajul",
    "ăsta e bun",
    "ș și ț",
    "naïve café",
    "日本語",
)
PROVIDERS = (
    ("anthropic_oauth", "claude-opus-4-1"),
    ("zen", "big-pickle"),
    ("chatgpt_oauth", "gpt-5-codex"),
)
HARNESSES = ("claude", "codex", "unknown")
STATUSES: tuple[RequestStatus, ...] = ("success", "error", "cancelled")
FOLDERS = ("C:\\Users\\dev\\proj", "D:\\work\\alpha", None)
EXITS = ("tor-1", "proxy.example:8080", None)

#: Searches the tests ask: common, rare and absent words, case, wildcards,
#: quotes, backslashes, tool-call-only terms, 1-2 characters, non-ASCII.
CORPUS = (
    "too long",
    "TODO",
    "Traceback",
    "zqxjvkw",
    "README.md",
    "ReadMe.MD",
    "eadm",
    "mod_",
    "%",
    '"hi',
    "back\\slash",
    "C:\\Users\\dev\\proj",
    "toolu_",
    "Bash",
    "x",
    "->",
    "—",
    "✓",
    "ș",
    "Ă",
    "ăsta",
    "funcționează",
    "done — tests",
    "  too   long  ",
)


def _prose(rng: random.Random, lo: int, hi: int) -> str:
    words: list[str] = []
    target = rng.randint(lo, hi)
    while sum(len(word) + 1 for word in words) < target:
        words.append(rng.choice(SPICE) if rng.random() < 0.08 else rng.choice(WORDS))
    return " ".join(words)


def make_record(rng: random.Random, index: int, ts: float) -> RequestRecord:
    local = rng.random() < 0.12
    provider, model = (None, None) if local else rng.choice(PROVIDERS)
    status = rng.choices(STATUSES, [0.75, 0.15, 0.1])[0]
    output = _prose(rng, 20, 200) if rng.random() < 0.85 else ""
    thinking = _prose(rng, 20, 150) if rng.random() < 0.5 else None
    tools = (
        [
            {
                "id": f"toolu_{rng.getrandbits(32):08x}",
                "name": rng.choice(("Bash", "Read", "Edit")),
                "input": {"command": "pytest -q C:\\Users\\dev\\proj\\app.py"},
            }
        ]
        if rng.random() < 0.6
        else None
    )
    session = f"sess-{rng.randint(1, 9):02d}" if rng.random() < 0.7 else None
    exit_label = rng.choice(EXITS)
    return RequestRecord(
        id=f"req-{index:05d}",
        ts_epoch=ts,
        endpoint=rng.choice(("/v1/messages", "/v1/responses")),
        protocol="anthropic",
        requested_model=model or "claude-haiku-4-5",
        provider=provider,
        resolved_model=model,
        stream=True,
        input_text=_prose(rng, 100, 900),
        output_text=output,
        output_chars=len(output),
        thinking_text=thinking,
        thinking_chars=len(thinking) if thinking else 0,
        tool_calls=tools,
        tool_call_count=len(tools) if tools else 0,
        tokens_in=rng.randint(10, 9000),
        tokens_out=rng.randint(0, 900),
        ttft_ms=None if rng.random() < 0.2 else rng.uniform(80, 9000),
        duration_ms=rng.uniform(50, 30_000),
        status=status,
        error_kind="rate_limit" if status == "error" else None,
        key_label=None if local else rng.choice(("sk-a…1111", "sk-b…2222")),
        key_index=0,
        harness=rng.choice(HARNESSES),
        optimization="quota_probe" if local else None,
        session_id=session,
        project_dir=rng.choice(FOLDERS) if session else None,
        origin_source="header" if session else None,
        route_attempt=None if local else 0,
        cost_usd=None if local else round(rng.uniform(0.0001, 0.5), 6),
        cost_source=None if local else "provider",
        attempts=()
        if local
        else (
            RouteAttempt(
                attempt=0,
                provider=provider,
                model_ref=f"{provider}/{model}",
                outcome=RouteAttemptOutcome.SUCCEEDED
                if status == "success"
                else RouteAttemptOutcome.FAILED,
                ladder_tries=1,
                proxy_label=exit_label,
            ),
        ),
    )


def stamps(rows: int, seed: int = 7) -> list[float]:
    """Row timestamps, about one in eight shared with the row before."""

    rng = random.Random(seed)
    out: list[float] = []
    ts = BASE_TS
    for index in range(rows):
        if index and rng.random() < 0.12:
            out.append(out[-1])
            continue
        ts += rng.uniform(1.0, 600.0)
        out.append(round(ts, 3))
    return out


def build_search_log(
    path: Path, rows: int = 160, *, seed: int = 7, inline_share: float = 0.2
) -> tuple[RequestLogStore, list[float]]:
    """Write ``rows`` requests; the first ``inline_share`` with bodies inline.

    Returns an open store (its writer already drained and stopped: reads open
    connections of their own) and every row's timestamp, in written order.
    """

    rng = random.Random(seed)
    times = stamps(rows, seed)
    order = list(range(rows))
    # Written a little out of time order, as requests finish out of order.
    for index in range(0, rows - 3, 11):
        order[index], order[index + 2] = order[index + 2], order[index]
    inline = int(rows * inline_share)
    for part, compress in ((order[:inline], False), (order[inline:], True)):
        writer = RequestLogStore(
            path, max_rows=0, queue_max_size=rows + 10, compress_bodies=compress
        )
        for index in part:
            writer.enqueue(make_record(rng, index, times[index]))
        writer.close()
    store = RequestLogStore(path, max_rows=0)
    store.close()
    return store, [times[index] for index in order]
