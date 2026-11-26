"""Persistent SQLite event/session store and atomic device registry for /rc."""

import contextlib
import json
import os
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from my_claude_code.config.paths import (
    rc_devices_path,
    rc_session_secret_path,
    rc_sessions_db_path,
)


def _atomic_write_text(path: Path, text: str, *, private: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        if private and sys.platform != "win32":
            with contextlib.suppress(OSError):
                os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        if private:
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


class RemoteStore:
    """Thread-safe SQLite WAL store + atomic JSON device/secret persistence."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._db_path: Path | None = None

    def _connect(self) -> sqlite3.Connection:
        target = rc_sessions_db_path()
        if self._conn is not None and self._db_path == target:
            return self._conn
        if self._conn is not None:
            with contextlib.suppress(sqlite3.Error):
                self._conn.close()
            self._conn = None
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(target), check_same_thread=False, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema(conn)
        self._conn = conn
        self._db_path = target
        return conn

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS rc_sessions (
                id TEXT PRIMARY KEY,
                harness TEXT NOT NULL,
                driver_tier TEXT NOT NULL,
                title TEXT NOT NULL,
                cwd TEXT NOT NULL,
                worktree_path TEXT NOT NULL DEFAULT '',
                branch_name TEXT NOT NULL DEFAULT '',
                permission_mode TEXT NOT NULL,
                status TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                provider_id TEXT NOT NULL DEFAULT '',
                external_session_id TEXT NOT NULL DEFAULT '',
                server_url TEXT NOT NULL DEFAULT '',
                attach_hint TEXT NOT NULL DEFAULT '',
                pid INTEGER,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_error TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS rc_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                ts REAL NOT NULL,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_rc_events_session_seq
                ON rc_events (session_id, seq);

            CREATE TABLE IF NOT EXISTS rc_approvals (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                tool_use_id TEXT NOT NULL DEFAULT '',
                input_json TEXT NOT NULL,
                status TEXT NOT NULL,
                decision TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                resolved_at REAL
            );

            CREATE INDEX IF NOT EXISTS idx_rc_approvals_session_status
                ON rc_approvals (session_id, status);
            """
        )
        cols = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(rc_sessions)").fetchall()
        }
        if "server_url" not in cols:
            conn.execute(
                "ALTER TABLE rc_sessions ADD COLUMN server_url TEXT NOT NULL DEFAULT ''"
            )
        if "attach_hint" not in cols:
            conn.execute(
                "ALTER TABLE rc_sessions ADD COLUMN attach_hint TEXT NOT NULL DEFAULT ''"
            )
        conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.close()
                self._conn = None
                self._db_path = None

    # ------------------------------------------------------------ sessions --

    def create_session(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO rc_sessions (
                    id, harness, driver_tier, title, cwd, worktree_path,
                    branch_name, permission_mode, status, model, provider_id,
                    external_session_id, server_url, attach_hint,
                    pid, created_at, updated_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["id"],
                    record["harness"],
                    record["driver_tier"],
                    record["title"],
                    record["cwd"],
                    record.get("worktree_path", ""),
                    record.get("branch_name", ""),
                    record["permission_mode"],
                    record["status"],
                    record.get("model", ""),
                    record.get("provider_id", ""),
                    record.get("external_session_id", ""),
                    record.get("server_url", ""),
                    record.get("attach_hint", ""),
                    record.get("pid"),
                    record["created_at"],
                    record["updated_at"],
                    record.get("last_error", ""),
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM rc_sessions WHERE id = ?", (record["id"],)
            ).fetchone()
            return dict(row) if row is not None else dict(record)

    def update_session(self, session_id: str, **fields: Any) -> dict[str, Any] | None:
        if not fields:
            return self.get_session(session_id)
        allowed = {
            "driver_tier",
            "title",
            "cwd",
            "worktree_path",
            "branch_name",
            "permission_mode",
            "status",
            "model",
            "provider_id",
            "external_session_id",
            "server_url",
            "attach_hint",
            "pid",
            "updated_at",
            "last_error",
        }
        filtered = {k: v for k, v in fields.items() if k in allowed}
        filtered.setdefault("updated_at", time.time())
        columns = ", ".join(f"{key} = ?" for key in filtered)
        values = [*filtered.values(), session_id]
        with self._lock:
            conn = self._connect()
            conn.execute(f"UPDATE rc_sessions SET {columns} WHERE id = ?", values)
            conn.commit()
            row = conn.execute(
                "SELECT * FROM rc_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            return dict(row) if row is not None else None

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM rc_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            return dict(row) if row is not None else None

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT * FROM rc_sessions ORDER BY updated_at DESC, created_at DESC"
            ).fetchall()
            return [dict(row) for row in rows]

    def delete_session(self, session_id: str) -> bool:
        with self._lock:
            conn = self._connect()
            conn.execute("DELETE FROM rc_approvals WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM rc_events WHERE session_id = ?", (session_id,))
            cur = conn.execute("DELETE FROM rc_sessions WHERE id = ?", (session_id,))
            conn.commit()
            return cur.rowcount > 0

    # -------------------------------------------------------------- events --

    def append_event(
        self, session_id: str, kind: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        ts = time.time()
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                """
                INSERT INTO rc_events (session_id, ts, kind, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, ts, kind, payload_json),
            )
            conn.commit()
            seq = int(cur.lastrowid or 0)
            return {
                "seq": seq,
                "session_id": session_id,
                "ts": ts,
                "kind": kind,
                "payload": payload,
            }

    def list_events(
        self, session_id: str, *, since_seq: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        bounded = max(1, min(limit, 5000))
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                """
                SELECT seq, session_id, ts, kind, payload_json
                FROM rc_events
                WHERE session_id = ? AND seq > ?
                ORDER BY seq ASC
                LIMIT ?
                """,
                (session_id, max(0, since_seq), bounded),
            ).fetchall()
            events: list[dict[str, Any]] = []
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"])
                except json.JSONDecodeError:
                    payload = {}
                events.append(
                    {
                        "seq": int(row["seq"]),
                        "session_id": str(row["session_id"]),
                        "ts": float(row["ts"]),
                        "kind": str(row["kind"]),
                        "payload": payload,
                    }
                )
            return events

    # ----------------------------------------------------------- approvals --

    def create_approval(
        self,
        *,
        approval_id: str,
        session_id: str,
        tool_name: str,
        tool_use_id: str,
        tool_input: dict[str, Any],
    ) -> dict[str, Any]:
        created_at = time.time()
        input_json = json.dumps(tool_input, ensure_ascii=False)
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO rc_approvals (
                    id, session_id, tool_name, tool_use_id, input_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    approval_id,
                    session_id,
                    tool_name,
                    tool_use_id,
                    input_json,
                    created_at,
                ),
            )
            conn.commit()
            return {
                "id": approval_id,
                "session_id": session_id,
                "tool_name": tool_name,
                "tool_use_id": tool_use_id,
                "input": tool_input,
                "status": "pending",
                "decision": "",
                "created_at": created_at,
                "resolved_at": None,
            }

    def resolve_approval(
        self, approval_id: str, *, status: str, decision: str
    ) -> dict[str, Any] | None:
        resolved_at = time.time()
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                UPDATE rc_approvals
                SET status = ?, decision = ?, resolved_at = ?
                WHERE id = ?
                """,
                (status, decision, resolved_at, approval_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM rc_approvals WHERE id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                return None
            out = dict(row)
            try:
                out["input"] = json.loads(out.pop("input_json", "{}"))
            except json.JSONDecodeError:
                out["input"] = {}
            return out

    def list_pending_approvals(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                """
                SELECT * FROM rc_approvals
                WHERE session_id = ? AND status = 'pending'
                ORDER BY created_at ASC
                """,
                (session_id,),
            ).fetchall()
            out: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                try:
                    item["input"] = json.loads(item.pop("input_json", "{}"))
                except json.JSONDecodeError:
                    item["input"] = {}
                out.append(item)
            return out

    # ---------------------------------------------------- devices & secret --

    def get_or_create_signing_secret(self, configured_secret: str) -> str:
        explicit = configured_secret.strip()
        if explicit:
            return explicit
        with self._lock:
            path = rc_session_secret_path()
            if path.is_file():
                existing = path.read_text(encoding="utf-8").strip()
                if existing:
                    return existing
            generated = secrets.token_hex(32)
            _atomic_write_text(path, generated + "\n", private=True)
            return generated

    def _read_devices_map(self) -> dict[str, dict[str, Any]]:
        path = rc_devices_path()
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            return {}
        if not isinstance(raw, dict):
            return {}
        devices = raw.get("devices")
        if not isinstance(devices, dict):
            return {}
        return {
            str(k): dict(v)
            for k, v in devices.items()
            if isinstance(k, str) and isinstance(v, dict)
        }

    def _write_devices_map(self, devices: dict[str, dict[str, Any]]) -> None:
        payload = json.dumps({"devices": devices}, indent=2, sort_keys=True) + "\n"
        _atomic_write_text(rc_devices_path(), payload, private=True)

    def register_device(
        self,
        *,
        device_id: str,
        label: str,
        user_agent: str,
        ip: str,
    ) -> dict[str, Any]:
        now = time.time()
        record = {
            "device_id": device_id,
            "label": label,
            "user_agent": user_agent,
            "ip": ip,
            "created_at": now,
            "last_seen_at": now,
            "revoked": False,
            "revoked_at": None,
        }
        with self._lock:
            devices = self._read_devices_map()
            devices[device_id] = record
            self._write_devices_map(devices)
        return dict(record)

    def list_devices(self) -> list[dict[str, Any]]:
        with self._lock:
            devices = self._read_devices_map()
        active = [dict(rec) for rec in devices.values() if not rec.get("revoked")]
        active.sort(key=lambda item: float(item.get("last_seen_at", 0.0)), reverse=True)
        return active

    def is_device_active(self, device_id: str) -> bool:
        with self._lock:
            devices = self._read_devices_map()
            rec = devices.get(device_id)
            return bool(rec is not None and not rec.get("revoked"))

    def touch_device(self, device_id: str, *, ip: str, user_agent: str) -> bool:
        now = time.time()
        with self._lock:
            devices = self._read_devices_map()
            rec = devices.get(device_id)
            if rec is None or rec.get("revoked"):
                return False
            # Throttle disk writes to at most once every 30s unless IP changed.
            last_seen = float(rec.get("last_seen_at") or 0.0)
            if now - last_seen < 30.0 and rec.get("ip") == ip:
                return True
            rec["last_seen_at"] = now
            if ip:
                rec["ip"] = ip
            if user_agent:
                rec["user_agent"] = user_agent
            self._write_devices_map(devices)
            return True

    def revoke_device(self, device_id: str) -> bool:
        with self._lock:
            devices = self._read_devices_map()
            rec = devices.get(device_id)
            if rec is None or rec.get("revoked"):
                return False
            rec["revoked"] = True
            rec["revoked_at"] = time.time()
            self._write_devices_map(devices)
            return True

    def revoke_all_devices(self) -> int:
        now = time.time()
        with self._lock:
            devices = self._read_devices_map()
            count = 0
            for rec in devices.values():
                if not rec.get("revoked"):
                    rec["revoked"] = True
                    rec["revoked_at"] = now
                    count += 1
            if count:
                self._write_devices_map(devices)
            return count
