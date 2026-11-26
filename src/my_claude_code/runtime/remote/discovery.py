"""Read-only Unified Local Session Discovery Scanner for Remote Control (/rc)."""

import contextlib
import json
import os
import sqlite3
import sys
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from my_claude_code.config.paths import request_log_path

_MAX_FILES_PER_SOURCE = 40
_MAX_LINES_PER_JSONL = 40

_RESUMABLE_HARNESSES = frozenset(
    {
        "claude",
        "opencode",
        "opencode2",
        "kilo",
        "codex",
        "openclaw",
        "hermes",
        "gemini_cli",
        "qwen_code",
        "kimi_code",
        "commandcode_cli",
        "pi",
        "goose",
    }
)


def is_within_allowed_roots(candidate: Path, roots: list[Path]) -> bool:
    """Return True only when *candidate* resolves inside one of *roots*."""

    if not roots:
        return False
    try:
        resolved = candidate.expanduser().resolve()
    except OSError, RuntimeError:
        return False
    return any(resolved == root or resolved.is_relative_to(root) for root in roots)


def _check_cwd_allowed(raw_cwd: str, allowed_roots: list[Path]) -> tuple[str, bool]:
    text = (raw_cwd or "").strip()
    if not text:
        return "", False
    candidate = Path(text).expanduser()
    allowed = is_within_allowed_roots(candidate, allowed_roots)
    try:
        resolved = str(candidate.resolve())
    except OSError, RuntimeError:
        return text, False
    return resolved, allowed


def _parse_timestamp(value: Any, fallback: float) -> float:
    if isinstance(value, (int, float)):
        num = float(value)
        if num > 1e12:
            num /= 1000.0
        return num if num > 0 else fallback
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            num = float(text)
            if num > 1e12:
                num /= 1000.0
            return num if num > 0 else fallback
        except ValueError:
            pass
        iso = text[:-1] + "+00:00" if text.endswith("Z") else text
        with contextlib.suppress(ValueError):
            return datetime.fromisoformat(iso).timestamp()
    return fallback


def _open_sqlite_readonly(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.is_file():
        return None
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row[1]) for row in rows}
    except sqlite3.Error:
        return set()


def _snippet_from_text(text: Any, limit: int = 80) -> str:
    if not isinstance(text, str):
        return ""
    cleaned = " ".join(text.split()).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


def _extract_claude_message_text(msg: Any) -> str:
    if isinstance(msg, str):
        return msg
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    txt = block.get("text")
                    if isinstance(txt, str) and txt.strip():
                        return txt
    return ""


def _scan_claude_sessions(
    base_home: Path,
    source_env: Mapping[str, str],
    allowed_roots: list[Path],
) -> list[dict[str, Any]]:
    override = (source_env.get("CLAUDE_CONFIG_DIR") or "").strip()
    claude_dir = Path(override).expanduser() if override else (base_home / ".claude")
    projects_dir = claude_dir / "projects"
    if not projects_dir.is_dir():
        return []

    candidates: list[tuple[float, Path]] = []
    with contextlib.suppress(OSError):
        for proj in projects_dir.iterdir():
            if not proj.is_dir():
                continue
            with contextlib.suppress(OSError):
                for item in proj.iterdir():
                    if item.is_file() and item.suffix == ".jsonl":
                        with contextlib.suppress(OSError):
                            candidates.append((item.stat().st_mtime, item))
    candidates.sort(key=lambda pair: pair[0], reverse=True)

    results: list[dict[str, Any]] = []
    for mtime, path in candidates[:_MAX_FILES_PER_SOURCE]:
        ext_id = path.stem
        cwd = ""
        title = ""
        model = ""
        updated_at = mtime
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for idx, raw_line in enumerate(handle):
                    if idx >= _MAX_LINES_PER_JSONL:
                        break
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    sid = rec.get("sessionId")
                    if isinstance(sid, str) and sid.strip():
                        ext_id = sid.strip()
                    if not cwd and isinstance(rec.get("cwd"), str):
                        cwd = rec["cwd"]
                    if "timestamp" in rec:
                        updated_at = max(
                            updated_at, _parse_timestamp(rec["timestamp"], mtime)
                        )
                    msg = rec.get("message")
                    if not model and isinstance(msg, dict):
                        mval = msg.get("model")
                        if isinstance(mval, str):
                            model = mval
                    if not title and rec.get("type") == "user":
                        title = _snippet_from_text(_extract_claude_message_text(msg))
        except OSError:
            continue

        resolved_cwd, allowed = _check_cwd_allowed(cwd, allowed_roots)
        results.append(
            {
                "source_harness": "claude",
                "external_session_id": ext_id,
                "title": title or (Path(resolved_cwd).name if resolved_cwd else ext_id),
                "cwd": resolved_cwd,
                "within_allowed_dirs": allowed,
                "model": model,
                "updated_at": updated_at,
                "resumable": True,
                "source": "disk",
                "server_url": "",
            }
        )
    return results


def _opencode_data_dirs(
    app_name: str, base_home: Path, source_env: Mapping[str, str]
) -> list[Path]:
    dirs: list[Path] = []
    xdg = (source_env.get("XDG_DATA_HOME") or "").strip()
    if xdg:
        dirs.append(Path(xdg).expanduser() / app_name)
    dirs.append(base_home / ".local" / "share" / app_name)
    local_app = (source_env.get("LOCALAPPDATA") or "").strip()
    if local_app:
        dirs.append(Path(local_app).expanduser() / app_name)
    dirs.append(base_home / "AppData" / "Local" / app_name)

    unique: list[Path] = []
    seen: set[Path] = set()
    for d in dirs:
        if d not in seen:
            seen.add(d)
            unique.append(d)
    return unique


def _scan_opencode_family(
    base_home: Path,
    source_env: Mapping[str, str],
    allowed_roots: list[Path],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for app_name, db_names in (
        ("opencode", ("opencode.db", "opencode.sqlite")),
        ("kilo", ("kilo.db", "kilo.sqlite", "opencode.db")),
    ):
        for data_dir in _opencode_data_dirs(app_name, base_home, source_env):
            if not data_dir.is_dir():
                continue
            for db_name in db_names:
                conn = _open_sqlite_readonly(data_dir / db_name)
                if conn is None:
                    continue
                try:
                    cols = _table_columns(conn, "session")
                    if "id" in cols:
                        dir_col = (
                            "directory"
                            if "directory" in cols
                            else ("cwd" if "cwd" in cols else "''")
                        )
                        title_col = "title" if "title" in cols else "''"
                        time_col = (
                            "time_updated"
                            if "time_updated" in cols
                            else (
                                "updated_at"
                                if "updated_at" in cols
                                else ("time_created" if "time_created" in cols else "0")
                            )
                        )
                        rows = conn.execute(
                            f"SELECT id, {title_col} AS title, {dir_col} AS cwd, "
                            f"{time_col} AS updated FROM session "
                            f"ORDER BY updated DESC LIMIT ?",
                            (_MAX_FILES_PER_SOURCE,),
                        ).fetchall()
                        for row in rows:
                            ext_id = str(row["id"] or "").strip()
                            if not ext_id:
                                continue
                            resolved_cwd, allowed = _check_cwd_allowed(
                                str(row["cwd"] or ""), allowed_roots
                            )
                            results.append(
                                {
                                    "source_harness": app_name,
                                    "external_session_id": ext_id,
                                    "title": _snippet_from_text(row["title"]) or ext_id,
                                    "cwd": resolved_cwd,
                                    "within_allowed_dirs": allowed,
                                    "model": "",
                                    "updated_at": _parse_timestamp(row["updated"], 0.0),
                                    "resumable": True,
                                    "source": "disk",
                                    "server_url": "",
                                }
                            )
                except sqlite3.Error:
                    pass
                finally:
                    conn.close()

            # Legacy JSON session storage under storage/session/
            storage_dir = data_dir / "storage" / "session"
            if storage_dir.is_dir():
                json_files: list[tuple[float, Path]] = []
                with contextlib.suppress(OSError):
                    for path in storage_dir.rglob("*.json"):
                        if path.is_file():
                            with contextlib.suppress(OSError):
                                json_files.append((path.stat().st_mtime, path))
                json_files.sort(key=lambda pair: pair[0], reverse=True)
                for mtime, path in json_files[:_MAX_FILES_PER_SOURCE]:
                    try:
                        doc = json.loads(path.read_text(encoding="utf-8"))
                    except OSError, json.JSONDecodeError:
                        continue
                    if not isinstance(doc, dict):
                        continue
                    ext_id = str(doc.get("id") or path.stem).strip()
                    raw_cwd = str(doc.get("directory") or doc.get("cwd") or "")
                    resolved_cwd, allowed = _check_cwd_allowed(raw_cwd, allowed_roots)
                    time_obj = doc.get("time")
                    updated_raw = (
                        time_obj.get("updated")
                        if isinstance(time_obj, dict)
                        else doc.get("updated_at")
                    )
                    results.append(
                        {
                            "source_harness": app_name,
                            "external_session_id": ext_id,
                            "title": _snippet_from_text(doc.get("title")) or ext_id,
                            "cwd": resolved_cwd,
                            "within_allowed_dirs": allowed,
                            "model": "",
                            "updated_at": _parse_timestamp(updated_raw, mtime),
                            "resumable": True,
                            "source": "disk",
                            "server_url": "",
                        }
                    )
    return results


def _scan_codex_sessions(
    base_home: Path,
    source_env: Mapping[str, str],
    allowed_roots: list[Path],
) -> list[dict[str, Any]]:
    override = (source_env.get("CODEX_HOME") or "").strip()
    codex_home = Path(override).expanduser() if override else (base_home / ".codex")
    results: list[dict[str, Any]] = []

    sessions_dir = codex_home / "sessions"
    if sessions_dir.is_dir():
        candidates: list[tuple[float, Path]] = []
        with contextlib.suppress(OSError):
            for path in sessions_dir.rglob("rollout-*.jsonl"):
                if path.is_file():
                    with contextlib.suppress(OSError):
                        candidates.append((path.stat().st_mtime, path))
        candidates.sort(key=lambda pair: pair[0], reverse=True)

        for mtime, path in candidates[:_MAX_FILES_PER_SOURCE]:
            ext_id = path.stem
            cwd = ""
            model = ""
            title = ""
            updated_at = mtime
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    for idx, raw_line in enumerate(handle):
                        if idx >= _MAX_LINES_PER_JSONL:
                            break
                        line = raw_line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(rec, dict):
                            continue
                        if rec.get("type") == "session_meta" and isinstance(
                            rec.get("payload"), dict
                        ):
                            meta = rec["payload"]
                            if isinstance(meta.get("id"), str) and meta["id"]:
                                ext_id = meta["id"]
                            if isinstance(meta.get("cwd"), str):
                                cwd = meta["cwd"]
                            if isinstance(meta.get("model"), str):
                                model = meta["model"]
                            updated_at = _parse_timestamp(meta.get("timestamp"), mtime)
                        else:
                            if isinstance(rec.get("id"), str) and rec["id"]:
                                ext_id = rec["id"]
                            if not cwd and isinstance(rec.get("cwd"), str):
                                cwd = rec["cwd"]
                            if not model and isinstance(rec.get("model"), str):
                                model = rec["model"]
                            if not title and rec.get("type") in {
                                "event_msg",
                                "response_item",
                            }:
                                payload = rec.get("payload")
                                if isinstance(payload, dict):
                                    txt = payload.get("message") or payload.get("text")
                                    if isinstance(txt, str) and txt.strip():
                                        title = _snippet_from_text(txt)
            except OSError:
                continue

            resolved_cwd, allowed = _check_cwd_allowed(cwd, allowed_roots)
            results.append(
                {
                    "source_harness": "codex",
                    "external_session_id": ext_id,
                    "title": title
                    or (Path(resolved_cwd).name if resolved_cwd else ext_id),
                    "cwd": resolved_cwd,
                    "within_allowed_dirs": allowed,
                    "model": model,
                    "updated_at": updated_at,
                    "resumable": True,
                    "source": "disk",
                    "server_url": "",
                }
            )

    # Also inspect sqlite thread DBs under CODEX_HOME if present
    if codex_home.is_dir():
        for db_path in sorted(codex_home.glob("*.sqlite")):
            conn = _open_sqlite_readonly(db_path)
            if conn is None:
                continue
            try:
                cols = _table_columns(conn, "threads")
                if "id" in cols:
                    cwd_col = "cwd" if "cwd" in cols else "''"
                    title_col = "title" if "title" in cols else "''"
                    model_col = "model" if "model" in cols else "''"
                    ts_col = (
                        "updated_at"
                        if "updated_at" in cols
                        else ("created_at" if "created_at" in cols else "0")
                    )
                    rows = conn.execute(
                        f"SELECT id, {cwd_col} AS cwd, {title_col} AS title, "
                        f"{model_col} AS model, {ts_col} AS updated FROM threads "
                        f"ORDER BY updated DESC LIMIT ?",
                        (_MAX_FILES_PER_SOURCE,),
                    ).fetchall()
                    for row in rows:
                        ext_id = str(row["id"] or "").strip()
                        if not ext_id:
                            continue
                        resolved_cwd, allowed = _check_cwd_allowed(
                            str(row["cwd"] or ""), allowed_roots
                        )
                        results.append(
                            {
                                "source_harness": "codex",
                                "external_session_id": ext_id,
                                "title": _snippet_from_text(row["title"]) or ext_id,
                                "cwd": resolved_cwd,
                                "within_allowed_dirs": allowed,
                                "model": str(row["model"] or ""),
                                "updated_at": _parse_timestamp(row["updated"], 0.0),
                                "resumable": True,
                                "source": "disk",
                                "server_url": "",
                            }
                        )
            except sqlite3.Error:
                pass
            finally:
                conn.close()

    return results


def _scan_hermes_sessions(
    base_home: Path,
    source_env: Mapping[str, str],
    allowed_roots: list[Path],
) -> list[dict[str, Any]]:
    candidates: list[Path] = []
    hermes_home = (source_env.get("HERMES_HOME") or "").strip()
    if hermes_home:
        candidates.append(Path(hermes_home).expanduser() / "state.db")
    local_app = (source_env.get("LOCALAPPDATA") or "").strip()
    if local_app:
        candidates.append(Path(local_app).expanduser() / "hermes" / "state.db")
    candidates.append(base_home / ".hermes" / "state.db")

    results: list[dict[str, Any]] = []
    seen_db: set[Path] = set()
    for db_path in candidates:
        if db_path in seen_db:
            continue
        seen_db.add(db_path)
        conn = _open_sqlite_readonly(db_path)
        if conn is None:
            continue
        try:
            cols = _table_columns(conn, "sessions")
            id_col = (
                "id" if "id" in cols else ("session_id" if "session_id" in cols else "")
            )
            if not id_col:
                continue
            cwd_col = (
                "cwd"
                if "cwd" in cols
                else ("working_dir" if "working_dir" in cols else "''")
            )
            title_col = (
                "title"
                if "title" in cols
                else ("summary" if "summary" in cols else "''")
            )
            model_col = "model" if "model" in cols else "''"
            ts_col = (
                "updated_at"
                if "updated_at" in cols
                else (
                    "last_active_at"
                    if "last_active_at" in cols
                    else ("created_at" if "created_at" in cols else "0")
                )
            )
            rows = conn.execute(
                f"SELECT {id_col} AS id, {cwd_col} AS cwd, {title_col} AS title, "
                f"{model_col} AS model, {ts_col} AS updated FROM sessions "
                f"ORDER BY updated DESC LIMIT ?",
                (_MAX_FILES_PER_SOURCE,),
            ).fetchall()
            for row in rows:
                ext_id = str(row["id"] or "").strip()
                if not ext_id:
                    continue
                resolved_cwd, allowed = _check_cwd_allowed(
                    str(row["cwd"] or ""), allowed_roots
                )
                results.append(
                    {
                        "source_harness": "hermes",
                        "external_session_id": ext_id,
                        "title": _snippet_from_text(row["title"]) or ext_id,
                        "cwd": resolved_cwd,
                        "within_allowed_dirs": allowed,
                        "model": str(row["model"] or ""),
                        "updated_at": _parse_timestamp(row["updated"], 0.0),
                        "resumable": True,
                        "source": "disk",
                        "server_url": "",
                    }
                )
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return results


def _openclaw_state_dirs(base_home: Path, source_env: Mapping[str, str]) -> list[Path]:
    dirs: list[Path] = []
    state_dir = (source_env.get("OPENCLAW_STATE_DIR") or "").strip()
    if state_dir:
        dirs.append(Path(state_dir).expanduser())
    cfg_path = (source_env.get("OPENCLAW_CONFIG_PATH") or "").strip()
    if cfg_path:
        dirs.append(Path(cfg_path).expanduser().parent)
    oc_home = (source_env.get("OPENCLAW_HOME") or "").strip()
    if oc_home:
        dirs.append(Path(oc_home).expanduser() / ".openclaw")
    dirs.append(base_home / ".openclaw")

    unique: list[Path] = []
    seen: set[Path] = set()
    for d in dirs:
        if d not in seen:
            seen.add(d)
            unique.append(d)
    return unique


def _scan_openclaw_sessions(
    base_home: Path,
    source_env: Mapping[str, str],
    allowed_roots: list[Path],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for state_dir in _openclaw_state_dirs(base_home, source_env):
        agents_dir = state_dir / "agents"
        if not agents_dir.is_dir():
            continue
        with contextlib.suppress(OSError):
            for sessions_json in sorted(agents_dir.glob("*/sessions/sessions.json")):
                try:
                    mtime = sessions_json.stat().st_mtime
                    raw = json.loads(sessions_json.read_text(encoding="utf-8"))
                except OSError, json.JSONDecodeError:
                    continue
                items: list[tuple[str, dict[str, Any]]] = []
                if isinstance(raw, dict):
                    for key, val in raw.items():
                        if isinstance(val, dict):
                            items.append((str(key), val))
                elif isinstance(raw, list):
                    for val in raw:
                        if isinstance(val, dict):
                            key = str(
                                val.get("sessionKey") or val.get("sessionId") or ""
                            )
                            if key:
                                items.append((key, val))
                for sess_key, meta in items[:_MAX_FILES_PER_SOURCE]:
                    raw_cwd = str(
                        meta.get("cwd")
                        or meta.get("workspace")
                        or (
                            meta.get("origin", {}).get("cwd")
                            if isinstance(meta.get("origin"), dict)
                            else ""
                        )
                        or ""
                    )
                    resolved_cwd, allowed = _check_cwd_allowed(raw_cwd, allowed_roots)
                    title = (
                        _snippet_from_text(
                            meta.get("displayName")
                            or meta.get("label")
                            or meta.get("subject")
                        )
                        or sess_key
                    )
                    results.append(
                        {
                            "source_harness": "openclaw",
                            "external_session_id": sess_key,
                            "title": title,
                            "cwd": resolved_cwd,
                            "within_allowed_dirs": allowed,
                            "model": str(meta.get("model") or ""),
                            "updated_at": _parse_timestamp(
                                meta.get("updatedAt"), mtime
                            ),
                            "resumable": True,
                            "source": "disk",
                            "server_url": "",
                        }
                    )
    return results


def _scan_requests_db(allowed_roots: list[Path]) -> list[dict[str, Any]]:
    db_path = request_log_path()
    conn = _open_sqlite_readonly(db_path)
    if conn is None:
        return []
    results: list[dict[str, Any]] = []
    try:
        cols = _table_columns(conn, "requests")
        if not {"harness", "session_id", "ts_epoch"} <= cols:
            return []
        proj_col = "project_dir" if "project_dir" in cols else "''"
        model_col = "resolved_model" if "resolved_model" in cols else "''"
        rows = conn.execute(
            f"""
            SELECT harness, session_id, {proj_col} AS project_dir,
                   {model_col} AS resolved_model, MAX(ts_epoch) AS last_ts
            FROM requests
            WHERE session_id IS NOT NULL AND session_id != ''
            GROUP BY harness, session_id
            ORDER BY last_ts DESC
            LIMIT 50
            """
        ).fetchall()
        for row in rows:
            harness = str(row["harness"] or "claude").strip() or "claude"
            ext_id = str(row["session_id"] or "").strip()
            if not ext_id:
                continue
            resolved_cwd, allowed = _check_cwd_allowed(
                str(row["project_dir"] or ""), allowed_roots
            )
            results.append(
                {
                    "source_harness": harness,
                    "external_session_id": ext_id,
                    "title": (
                        f"{harness} — {Path(resolved_cwd).name}"
                        if resolved_cwd
                        else ext_id
                    ),
                    "cwd": resolved_cwd,
                    "within_allowed_dirs": allowed,
                    "model": str(row["resolved_model"] or ""),
                    "updated_at": float(row["last_ts"] or 0.0),
                    "resumable": harness in _RESUMABLE_HARNESSES,
                    "source": "requests_db",
                    "server_url": "",
                }
            )
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return results


def discover_local_sessions(
    allowed_roots: list[Path],
    *,
    harness_filter: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    limit: int = 80,
) -> list[dict[str, Any]]:
    """Scan local CLI stores and ``requests.db`` (read-only) for resumable sessions."""

    source_env = os.environ if env is None else env
    base_home = Path.home() if home is None else home
    filter_norm = (harness_filter or "").strip().lower()

    all_items: list[dict[str, Any]] = []
    if not filter_norm or filter_norm == "claude":
        all_items.extend(_scan_claude_sessions(base_home, source_env, allowed_roots))
    if not filter_norm or filter_norm in {"opencode", "opencode2", "kilo"}:
        all_items.extend(_scan_opencode_family(base_home, source_env, allowed_roots))
    if not filter_norm or filter_norm == "codex":
        all_items.extend(_scan_codex_sessions(base_home, source_env, allowed_roots))
    if not filter_norm or filter_norm == "hermes":
        all_items.extend(_scan_hermes_sessions(base_home, source_env, allowed_roots))
    if not filter_norm or filter_norm == "openclaw":
        all_items.extend(_scan_openclaw_sessions(base_home, source_env, allowed_roots))
    all_items.extend(_scan_requests_db(allowed_roots))

    # Deduplicate by (source_harness, external_session_id), preferring richer disk entries
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for item in all_items:
        h = str(item["source_harness"])
        eid = str(item["external_session_id"])
        if filter_norm:
            if filter_norm == "opencode2" and h == "opencode":
                pass
            elif h != filter_norm:
                continue
        key = (h, eid)
        existing = merged.get(key)
        if existing is None:
            merged[key] = item
        else:
            if not existing["cwd"] and item["cwd"]:
                existing["cwd"] = item["cwd"]
                existing["within_allowed_dirs"] = item["within_allowed_dirs"]
            if not existing["model"] and item["model"]:
                existing["model"] = item["model"]
            if float(item["updated_at"]) > float(existing["updated_at"]):
                existing["updated_at"] = item["updated_at"]

    out = list(merged.values())
    out.sort(key=lambda item: float(item.get("updated_at") or 0.0), reverse=True)
    return out[: max(1, limit)]


def discovery_platform() -> str:
    return sys.platform
