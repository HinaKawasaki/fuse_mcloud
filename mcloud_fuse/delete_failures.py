"""删除失败的持久记录：让后台删除的失败在 CLI 里也能看见。

删除是批量异步的：``unlink`` 立刻返回，后台 worker 合并成 ``batchDelete``。
失败时对象仍在云端，会重新出现在 ``ls`` 里（这是**故意**的：删没删掉要让用户
看见），但如果用户没再 ``ls``，就永远不知道。

这里把失败写进 state 目录的一个小 JSON（0600），``status`` / ``doctor`` 读取；
下次删除成功时自动清掉对应条目。写盘失败只记日志，绝不影响删除流程。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List

from mcloud_fuse.journal import state_dir

logger = logging.getLogger(__name__)

#: 最多保留多少条失败记录，避免长期挂载下无限增长。
MAX_ENTRIES = 100


def failures_path() -> Path:
    override = os.getenv("MCLOUD_DELETE_FAILURES_FILE")
    if override:
        return Path(override).expanduser()
    return state_dir() / "delete-failures.json"


def load() -> List[Dict[str, Any]]:
    """读取失败记录；文件不存在/损坏都返回空列表。"""
    path = failures_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("删除失败记录不是合法 JSON，忽略：%s", path)
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict) and item.get("file_id")]


def _write(entries: List[Dict[str, Any]]) -> None:
    path = failures_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(entries[-MAX_ENTRIES:], handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise
    except OSError as exc:  # 记录失败不能拖垮挂载
        logger.warning("写入删除失败记录失败（忽略）：%s", exc)


def record(items: Iterable[Dict[str, Any]]) -> None:
    """登记一批失败（按 file_id 覆盖旧记录）。"""
    incoming = [item for item in items if item.get("file_id")]
    if not incoming:
        return
    by_id = {str(item["file_id"]): item for item in load()}
    for item in incoming:
        entry = dict(item)
        entry["file_id"] = str(entry["file_id"])
        entry.setdefault("at", time.time())
        by_id[entry["file_id"]] = entry
    _write(list(by_id.values()))


def forget(file_ids: Iterable[str]) -> None:
    """删除成功（或用户重试成功）后清掉对应记录。"""
    ids = {str(file_id) for file_id in file_ids if file_id}
    if not ids:
        return
    existing = load()
    remaining = [item for item in existing if str(item.get("file_id")) not in ids]
    if len(remaining) != len(existing):
        _write(remaining)


def clear() -> None:
    path = failures_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("清理删除失败记录失败：%s", exc)
