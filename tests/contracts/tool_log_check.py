"""Read-only check of a request log: did any client get a tool name it never sent?

The second half of the tool round-trip contract (``test_tool_round_trip.py``).
That file proves the translation on fixtures; this one reads what really
happened. For every logged request that carried tools, it compares the tool
calls the client received (``tool_calls``, inline or in the compressed reply
blob) with the catalogue the client sent (``tool_catalogues`` ->
``tool_schemas``). A returned name outside the catalogue is one of two things:

* ``stand-in``: a role MCC appended on the Zen free tier, and the model called
  it. The client then answers it as an unknown tool; this is by design, but it
  is worth counting.
* ``not offered``: anything else. That is either a wire name that leaked, such
  as ``read`` reaching Claude Code or a ``*_<16 hex>`` alias, or a tool the
  model invented. Either way it is a finding.

It needs no new column. The database is opened ``mode=ro`` and nothing is
written. Usage::

    uv run --offline python -m tests.contracts.tool_log_check <requests.db> [--days N]
"""

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from collections.abc import Iterator
from compression import zstd
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from my_claude_code.core.request_log import unpack_bodies
from my_claude_code.providers.openai_chat.opencode_catalogue import (
    OPENCODE_TOOL_FAMILIES,
)

STAND_IN_ROLES = frozenset(
    role for family in OPENCODE_TOOL_FAMILIES for role in family.stand_ins
)
TOOL_SHA_BYTES = 32


@dataclass(frozen=True, slots=True)
class Finding:
    request_id: str
    harness: str
    provider: str
    model: str
    name: str
    kind: str


@dataclass(slots=True)
class Report:
    examined: int = 0
    with_calls: int = 0
    calls: int = 0
    findings: list[Finding] = field(default_factory=list)

    def by_kind(self) -> Counter[str]:
        return Counter(finding.kind for finding in self.findings)


def _catalogues(con: sqlite3.Connection) -> dict[bytes, frozenset[str]]:
    names = {
        bytes(sha): name
        for sha, name in con.execute("SELECT sha, name FROM tool_schemas")
    }
    out: dict[bytes, frozenset[str]] = {}
    for sha, blob in con.execute("SELECT sha, member_shas FROM tool_catalogues"):
        members = bytes(blob)
        out[bytes(sha)] = frozenset(
            names.get(members[i : i + TOOL_SHA_BYTES], "")
            for i in range(0, len(members), TOOL_SHA_BYTES)
        )
    return out


def _dictionary(
    con: sqlite3.Connection, dict_id: int, cache: dict[int, zstd.ZstdDict]
) -> zstd.ZstdDict | None:
    if dict_id not in cache:
        row = con.execute(
            "SELECT content FROM body_dictionaries WHERE id = ?", (dict_id,)
        ).fetchone()
        if row is None:
            return None
        cache[dict_id] = zstd.ZstdDict(bytes(row[0]))
    return cache[dict_id]


def _returned_calls(
    con: sqlite3.Connection,
    inline: Any,
    dict_id: Any,
    payload: Any,
    cache: dict[int, zstd.ZstdDict],
) -> list[dict[str, Any]]:
    if isinstance(inline, str) and inline:
        try:
            value = json.loads(inline)
        except ValueError:
            value = None
        return value if isinstance(value, list) else []
    if payload is None:
        return []
    try:
        dictionary = (
            _dictionary(con, int(dict_id), cache) if dict_id is not None else None
        )
        raw = zstd.decompress(bytes(payload), zstd_dict=dictionary)
    except zstd.ZstdError, ValueError:
        return []
    calls = unpack_bodies(raw).get("tool_calls")
    return calls if isinstance(calls, list) else []


def _rows(con: sqlite3.Connection, since: float | None) -> Iterator[tuple[Any, ...]]:
    sql = (
        "SELECT r.id, r.harness, r.provider, r.resolved_model, r.tool_catalogue_sha,"
        " r.tool_calls, bb.dict_id, bb.payload"
        " FROM requests r"
        " LEFT JOIN request_bodies b ON b.request_id = r.id"
        " LEFT JOIN body_blobs bb ON bb.sha = b.sha"
        " WHERE r.tool_catalogue_sha IS NOT NULL"
    )
    params: tuple[Any, ...] = ()
    if since is not None:
        sql += " AND r.ts_epoch >= ?"
        params = (since,)
    yield from con.execute(sql, params)


def check(db_path: Path, *, since: float | None = None) -> Report:
    """Every request whose received tool names are not a subset of what it sent."""

    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        catalogues = _catalogues(con)
        cache: dict[int, zstd.ZstdDict] = {}
        report = Report()
        for rid, harness, provider, model, sha, inline, dict_id, payload in _rows(
            con, since
        ):
            report.examined += 1
            sent = catalogues.get(bytes(sha), frozenset())
            calls = _returned_calls(con, inline, dict_id, payload, cache)
            if not calls:
                continue
            report.with_calls += 1
            for call in calls:
                name = call.get("name") if isinstance(call, dict) else None
                if not isinstance(name, str):
                    continue
                report.calls += 1
                if name in sent:
                    continue
                kind = "stand-in" if name in STAND_IN_ROLES else "not offered"
                report.findings.append(
                    Finding(
                        request_id=str(rid),
                        harness=str(harness or ""),
                        provider=str(provider or ""),
                        model=str(model or ""),
                        name=name,
                        kind=kind,
                    )
                )
        return report
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else ""
    )
    parser.add_argument("db", type=Path)
    parser.add_argument("--days", type=float, default=None)
    args = parser.parse_args(argv)
    since = time.time() - args.days * 86400 if args.days else None
    report = check(args.db, since=since)
    print(
        f"requests with tools: {report.examined}, with tool calls: {report.with_calls}, "
        f"calls: {report.calls}, findings: {len(report.findings)} {dict(report.by_kind())}"
    )
    for finding in report.findings[:50]:
        print(
            f"  {finding.kind:11s} {finding.request_id} {finding.harness}/"
            f"{finding.provider}/{finding.model}: {finding.name!r}"
        )
    return 1 if any(f.kind == "not offered" for f in report.findings) else 0


if __name__ == "__main__":
    sys.exit(main())
