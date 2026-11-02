"""写回日志：持久化的 SQLite 元数据，独立于 ``/tmp`` 里的暂存文件。

为什么不再和写回文件放一起：

- 写回文件（payload）很大，放 tmpfs 能避免把云盘内容写到磁盘；
- 但日志必须跨重启保留，否则进程崩溃后就找不到「哪些文件还没上传」；
- tmpfs 重启即清空：日志若也在里面，崩溃恢复能力等于没有。

拆开后的语义：

- ``~/.local/state/mcloud-fuse/writeback.sqlite3``：只存元数据（几 KB），
  WAL + ``synchronous=FULL``，进程崩溃 / 重启后仍在；
- ``/tmp/cmcloud-fuse/writeback/*.wb``：文件内容，重启后可能消失；
- 启动恢复时发现 payload 不存在，就把条目标记 ``lost`` 并保留现场，
  **不会假装补传成功，也不会静默删除记录**。

代价必须说清楚：payload 在 tmpfs 时，**整机重启会丢失尚未上传的写入**。
这是用内存换速度的必然结果；要跨重启保住待上传内容，请把
``MCLOUD_WRITE_BACK_DIR`` 指到持久化磁盘。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

PENDING = "pending"
BUILDING = "building"
UPLOADING = "uploading"
RESCUED = "rescued"
LOST = "lost"
UPLOADED = "uploaded"


def state_dir() -> Path:
    override = os.getenv("MCLOUD_STATE_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.getenv("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "mcloud-fuse"


def scope_digest(write_back_dir: Path) -> str:
    scope = str(Path(write_back_dir).expanduser().resolve()).encode("utf-8")
    return hashlib.sha256(scope).hexdigest()[:12]


def rescue_dir(write_back_dir: Path) -> Path:
    """关机遇失抢救目录：与日志同一个 scope，0600/0700。"""
    return state_dir() / "rescue" / scope_digest(write_back_dir)


def default_journal_path(write_back_dir: Optional[Path] = None) -> Path:
    """日志路径。

    显式 ``MCLOUD_JOURNAL_DB`` 优先；否则按 ``write_back_dir`` 的哈希分库，
    避免同一用户挂载两个账号时 key 互相覆盖。同一个缓存目录对应同一个日志。
    """
    override = os.getenv("MCLOUD_JOURNAL_DB")
    if override:
        return Path(override).expanduser()
    if write_back_dir is None:
        return state_dir() / "writeback.sqlite3"
    return state_dir() / f"writeback-{scope_digest(write_back_dir)}.sqlite3"


@dataclass(frozen=True)
class JournalEntry:
    key: str
    target_dir_id: str
    file_name: str
    payload_path: str
    base_file_id: Optional[str] = None
    base_updated_at: Optional[str] = None
    state: str = PENDING
    size: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    last_error: Optional[str] = None

    def meta(self) -> dict:
        data = {"target_dir_id": self.target_dir_id, "file_name": self.file_name}
        if self.base_file_id:
            data["base_file_id"] = self.base_file_id
        if self.base_updated_at:
            data["base_updated_at"] = self.base_updated_at
        data["payload_path"] = self.payload_path
        return data


_SCHEMA = """
CREATE TABLE IF NOT EXISTS writeback (
    key            TEXT PRIMARY KEY,
    target_dir_id  TEXT NOT NULL,
    file_name      TEXT NOT NULL,
    payload_path   TEXT NOT NULL,
    base_file_id   TEXT,
    base_updated_at TEXT,
    state          TEXT NOT NULL DEFAULT 'pending',
    size           INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    last_error     TEXT
);
CREATE INDEX IF NOT EXISTS writeback_state ON writeback(state);
"""


class WritebackJournal:
    """线程安全的 SQLite 写回日志。连接不跨进程共享。"""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path).expanduser() if path else default_journal_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                # 日志里含云端文件名 / parentFileId；目录 700 比依赖 WAL 的 644 更稳
                self.path.parent.chmod(0o700)
            except OSError:
                pass
        self._lock = threading.RLock()
        try:
            self._conn = self._connect()
            self._init_schema()
        except sqlite3.DatabaseError:
            if str(self.path) == ":memory:":
                raise
            # 数据库损坏时不阻止挂载：留档后重建，恢复能力降级但读写可用。
            backup = self.path.with_name(self.path.name + f".corrupt-{int(time.time())}")
            existing = getattr(self, "_conn", None)
            if existing is not None:
                try:
                    existing.close()
                except (OSError, sqlite3.Error):
                    pass
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            self._conn = self._connect()
            self._init_schema()

    # ---------------------------------------------------------------- 连接

    def _connect(self) -> sqlite3.Connection:
        target = ":memory:" if str(self.path) == ":memory:" else str(self.path)
        conn = sqlite3.connect(target, check_same_thread=False, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        # payload 默认在 tmpfs，断电本来就会丢；对「进程崩溃」NORMAL 已经足够，
        # 又比 FULL 少一次 fsync。把 writeback 放持久盘、且要抗断电时设 FULL。
        sync = os.getenv("MCLOUD_JOURNAL_SYNC", "NORMAL").strip().upper()
        if sync not in ("OFF", "NORMAL", "FULL"):
            sync = "NORMAL"
        conn.execute(f"PRAGMA synchronous={sync}")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        try:
            if str(self.path) != ":memory:":
                os.chmod(self.path, 0o600)
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    # ---------------------------------------------------------------- 写

    def upsert(self, entry: JournalEntry) -> None:
        now = time.time()
        created = entry.created_at or now
        updated = entry.updated_at or now
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO writeback(
                    key, target_dir_id, file_name, payload_path, base_file_id,
                    base_updated_at, state, size, created_at, updated_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    target_dir_id=excluded.target_dir_id,
                    file_name=excluded.file_name,
                    payload_path=excluded.payload_path,
                    base_file_id=excluded.base_file_id,
                    base_updated_at=excluded.base_updated_at,
                    state=excluded.state,
                    size=excluded.size,
                    updated_at=excluded.updated_at,
                    last_error=excluded.last_error
                """,
                (
                    entry.key,
                    entry.target_dir_id,
                    entry.file_name,
                    entry.payload_path,
                    entry.base_file_id,
                    entry.base_updated_at,
                    entry.state,
                    int(entry.size),
                    created,
                    updated,
                    entry.last_error,
                ),
            )
            self._conn.commit()

    def set_state(
        self,
        key: str,
        state: str,
        *,
        size: Optional[int] = None,
        last_error: Optional[str] = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE writeback
                   SET state=?, size=COALESCE(?, size), last_error=?, updated_at=?
                 WHERE key=?
                """,
                (state, size, last_error, time.time(), key),
            )
            self._conn.commit()

    def relocate(
        self,
        key: str,
        payload_path: str,
        *,
        state: str = RESCUED,
        size: Optional[int] = None,
    ) -> None:
        """payload 被搬到持久盘后，更新路径与状态。"""
        with self._lock:
            self._conn.execute(
                """
                UPDATE writeback
                   SET payload_path=?, state=?, size=COALESCE(?, size), updated_at=?
                 WHERE key=?
                """,
                (str(payload_path), state, size, time.time(), key),
            )
            self._conn.commit()

    def remove(self, key: str) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM writeback WHERE key=?", (key,))
            self._conn.commit()
            return cursor.rowcount > 0

    # ---------------------------------------------------------------- 读

    @staticmethod
    def _entry(row: sqlite3.Row) -> JournalEntry:
        return JournalEntry(
            key=row["key"],
            target_dir_id=row["target_dir_id"],
            file_name=row["file_name"],
            payload_path=row["payload_path"],
            base_file_id=row["base_file_id"],
            base_updated_at=row["base_updated_at"],
            state=row["state"],
            size=int(row["size"] or 0),
            created_at=float(row["created_at"] or 0),
            updated_at=float(row["updated_at"] or 0),
            last_error=row["last_error"],
        )

    def get(self, key: str) -> Optional[JournalEntry]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM writeback WHERE key=?", (key,)).fetchone()
        return self._entry(row) if row else None

    def entries(self, state: Optional[str] = None) -> List[JournalEntry]:
        with self._lock:
            if state is None:
                rows = self._conn.execute(
                    "SELECT * FROM writeback ORDER BY created_at"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM writeback WHERE state=? ORDER BY created_at", (state,)
                ).fetchall()
        return [self._entry(row) for row in rows]

    def pending(self) -> Dict[str, dict]:
        return {entry.key: entry.meta() for entry in self.entries(PENDING)}

    def lost(self) -> Dict[str, dict]:
        return {entry.key: entry.meta() for entry in self.entries(LOST)}

    def counts(self) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, COUNT(*) AS n FROM writeback GROUP BY state"
            ).fetchall()
        return {row["state"]: int(row["n"]) for row in rows}

    def __len__(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM writeback").fetchone()
        return int(row["n"]) if row else 0
