"""已删除 fileId 的墓碑集合。

移动云盘接口没有删除能力，本层把「删除」实现为「登记墓碑 + 摘除本地节点」：
云端原文件依然存在，下一次 list 还会把它列出来，因此需要记住已删除的 fileId，
在目录充填时屏蔽它们；墓碑落盘后即使重启挂载也不会复活。

**墓碑是删除语义的唯一实现**。不要再额外把对象复制进 ``.TrashBin``：原对象并没有
消失，复制只会让被删对象占双倍容量，而 ``.TrashBin`` 在挂载里是隐藏目录。
"""

import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Optional, Set

logger = logging.getLogger(__name__)


class TombstoneStore:
    """线程安全、可持久化的已删除 fileId 集合。"""

    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path) if path else None
        self._lock = threading.RLock()
        self._ids: Set[str] = set()
        if self._path is not None:
            self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            if self._path and self._path.exists():
                with open(self._path, "r", encoding="utf-8") as handle:
                    self._ids = {line.strip() for line in handle if line.strip()}
                logger.info("已加载 %s 条删除墓碑", len(self._ids))
        except OSError as exc:
            logger.warning("加载删除墓碑失败: %s", exc)

    def _save_nolock(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".tombstones.", dir=str(self._path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for file_id in sorted(self._ids):
                    handle.write(file_id + "\n")
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.warning("保存删除墓碑失败: %s", exc)

    # ------------------------------------------------------------------
    def add(self, file_id: str) -> None:
        text = str(file_id or "").strip()
        if not text:
            return
        with self._lock:
            if text in self._ids:
                return
            self._ids.add(text)
            self._save_nolock()

    def __contains__(self, file_id: object) -> bool:
        with self._lock:
            return str(file_id) in self._ids

    def __len__(self) -> int:
        with self._lock:
            return len(self._ids)

    def snapshot(self) -> Set[str]:
        with self._lock:
            return set(self._ids)
