import logging
import threading
import time
from typing import List, Optional, Dict, Iterable

from mapping.id_map import InodeMapper, InodeEntry, PENDING_FILE_ID_PREFIX
from mapping.tombstones import TombstoneStore
from config import TRASH_BIN_DIR_NAME

logger = logging.getLogger(__name__)

# 云盘 search 索引是最终一致的：新建的对象往往几秒内查不到。
# 这段时间内即使云端列举没返回它，也必须保留在本地树里，否则刚建好的
# 目录/文件会被下一次 ls 的“对账清理”误删。
RECENT_GRACE_SEC = 300.0


class DirectoryTree:
    """
    内存目录树管理器：
    1. 协调 CloudItem 数据的解析与 InodeMapper 的注册
    2. 保证“同一目录内名字唯一”这一 POSIX 硬约束，避免 readdir 吐出重复项
    3. 为新建对象提供“索引宽限期”，适配云盘搜索的最终一致性
    4. 用墓碑屏蔽已删除的云端对象
    """

    def __init__(
        self,
        mapper: InodeMapper,
        tombstones: Optional[TombstoneStore] = None,
        recent_grace_sec: float = RECENT_GRACE_SEC,
    ):
        self.mapper = mapper
        self.tombstones = tombstones if tombstones is not None else TombstoneStore(None)
        # 记录已加载/已同步过的目录 inode 集合，避免重复拉取
        self._loaded_dir_inodes: set = set()
        # 新建对象 inode -> 创建时间戳（索引宽限期）
        self._recent: Dict[int, float] = {}
        self.recent_grace_sec = float(recent_grace_sec)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 新建对象宽限期
    # ------------------------------------------------------------------

    def mark_recent(self, inode: int) -> None:
        """标记该节点是刚新建的，云端索引可能还没收录。"""
        with self._lock:
            self._recent[inode] = time.monotonic()

    def clear_recent(self, inode: int) -> None:
        with self._lock:
            self._recent.pop(inode, None)

    def is_recent(self, inode: int) -> bool:
        with self._lock:
            created = self._recent.get(inode)
            if created is None:
                return False
            if time.monotonic() - created > self.recent_grace_sec:
                self._recent.pop(inode, None)
                return False
            return True

    # ------------------------------------------------------------------
    # 路径拼接
    # ------------------------------------------------------------------

    @staticmethod
    def child_path(parent_path: str, name: str) -> str:
        """拼接父目录本地路径与子项名，得到规范化的子路径。"""
        if parent_path == "/":
            return f"/{name}"
        return f"{parent_path.rstrip('/')}/{name}"

    # ------------------------------------------------------------------
    # 目录充填（核心）
    # ------------------------------------------------------------------

    def populate_directory(self, parent_inode: int, cloud_items: Iterable[Dict]) -> List[InodeEntry]:
        """
        将底层接口返回的 CloudItem / Dict 节点批量解析并注册进内存树。

        关键行为：
        - 同一目录内**按名字去重**：云端允许同名，而 POSIX 目录不允许，
          重复名字是 ls 出现重复行的直接原因；此处只保留第一个并记录告警。
        - **对账清理**：重新列举后，云端已不存在的旧子节点会从映射树移除，
          避免删除/重命名后本地仍看得到幽灵条目。
        - 返回值是该目录**当前真实存在、去重后的子节点列表**（按 inode 升序），
          调用方应直接使用该返回值，而不要保留充填过程中的中间列表。
        """
        parent_entry = self.mapper.get_by_inode(parent_inode)
        if not parent_entry or not parent_entry.is_dir:
            logger.error("无法充填非目录节点: inode=%s", parent_inode)
            return []

        # 1. 先按名字归一化并去重。
        #    云端允许同名，POSIX 目录不允许：同名是 ls 出现重复行的直接原因。
        #    选谁：优先保留本地已映射的 fileId（覆盖写后云端会短暂出现新旧两份同名），
        #    其次取更新时间最新的一份，最后退化为第一个。
        grouped: Dict[str, list] = {}
        for item in cloud_items:
            name = str(item.get("name", "") or "").strip()
            if not name or name in (".", ".."):
                continue
            if name == TRASH_BIN_DIR_NAME and parent_inode == InodeMapper.ROOT_INODE:
                logger.debug("跳过回收站系统目录的直接暴露: %s", name)
                continue
            candidate_id = str(
                item.get("id") or item.get("file_id") or item.get("fileId") or ""
            )
            # 已删除（移入回收站）的云端对象不能再出现，否则删除会在下次列举时复活
            if candidate_id and candidate_id in self.tombstones:
                continue
            grouped.setdefault(name, []).append(item)

        # 2. 注册/更新节点，记录本轮真实存在的 inode 集合。
        #    若本地已有一个“刚新建/刚覆盖”的同名节点，说明云端搜索索引可能还没更新，
        #    此时直接保留本地版本，避免被滞后（甚至仍是旧内容）的云端结果覆盖或误删。
        current_inodes: set = set()
        for name, candidates in grouped.items():
            child_path = self.child_path(parent_entry.path, name)
            existing = self.mapper.get_by_path(child_path)
            candidate_ids = {
                str(c.get("id") or c.get("file_id") or c.get("fileId") or "")
                for c in candidates
            }
            if existing is not None and str(existing.file_id).startswith(PENDING_FILE_ID_PREFIX):
                # 尚未上传成功的占位节点，云端不可能有，直接保留
                logger.debug(
                    "目录 inode=%s 保留未上传的占位节点 %r (inode=%s)",
                    parent_inode, name, existing.inode,
                )
                current_inodes.add(existing.inode)
                continue
            if existing is not None and self.is_recent(existing.inode):
                if existing.file_id not in candidate_ids:
                    # 云端索引还没收录这个新对象，保留本地版本以免被误删
                    logger.debug(
                        "目录 inode=%s 保留索引宽限期内的节点 %r (inode=%s)",
                        parent_inode, name, existing.inode,
                    )
                    current_inodes.add(existing.inode)
                    continue
                # 索引已追上，撤销宽限期并走正常流程
                self.clear_recent(existing.inode)

            chosen = candidates[0]
            if len(candidates) > 1:
                chosen = None
                if existing is not None:
                    for candidate in candidates:
                        candidate_id = str(
                            candidate.get("id") or candidate.get("file_id")
                            or candidate.get("fileId") or ""
                        )
                        if candidate_id == existing.file_id:
                            chosen = candidate
                            break
                if chosen is None:
                    chosen = max(
                        candidates,
                        key=lambda c: str(c.get("updateTime") or c.get("updated_at") or ""),
                    )
                logger.warning(
                    "目录 inode=%s 下出现 %s 个同名项 %r，保留 %s",
                    parent_inode, len(candidates), name,
                    chosen.get("id") or chosen.get("file_id"),
                )

            file_id = str(chosen.get("id") or chosen.get("file_id") or chosen.get("fileId") or "")
            if not file_id:
                logger.warning("跳过缺少 fileId 的云端条目: name=%r", name)
                continue
            is_dir = bool(chosen.get("isDir") or chosen.get("is_dir"))
            size = int(chosen.get("size") or 0)
            updated_at = str(chosen.get("updateTime") or chosen.get("updated_at") or "")

            entry = self.mapper.allocate_or_update(
                file_id=file_id,
                path=child_path,
                is_dir=is_dir,
                parent_inode=parent_inode,
                size=size,
                updated_at=updated_at,
            )
            # 云端已经能列出它了，撤销索引宽限期
            self.clear_recent(entry.inode)
            current_inodes.add(entry.inode)

        # 3. 对账：清理本轮云端已不存在的陈旧子节点。
        #    尚未上传的占位节点、以及仍在索引宽限期内的新建对象必须保留。
        for stale_inode in self.mapper.get_children_inodes(parent_inode):
            if stale_inode in current_inodes:
                continue
            stale_entry = self.mapper.get_by_inode(stale_inode)
            if stale_entry is not None and stale_entry.file_id.startswith(PENDING_FILE_ID_PREFIX):
                continue
            if self.is_recent(stale_inode):
                logger.debug(
                    "目录 inode=%s 新对象 inode=%s 仍在索引宽限期，暂不清理",
                    parent_inode, stale_inode,
                )
                continue
            logger.debug(
                "目录 inode=%s 清理陈旧子节点 inode=%s", parent_inode, stale_inode
            )
            self.mapper.remove_by_inode(stale_inode)

        self._loaded_dir_inodes.add(parent_inode)
        return self.get_children(parent_inode)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def is_directory_loaded(self, inode: int) -> bool:
        """检查指定目录的子节点是否已经被拉取并充填"""
        return inode in self._loaded_dir_inodes

    def mark_directory_stale(self, inode: int):
        """标记目录过期（如新建文件/删除文件后需要重新刷新时）"""
        self._loaded_dir_inodes.discard(inode)

    def get_children(self, parent_inode: int) -> List[InodeEntry]:
        """获取指定父节点下已存在的所有子节点，按 inode 升序返回。"""
        children = []
        for child_inode in self.mapper.get_children_inodes(parent_inode):
            child_entry = self.mapper.get_by_inode(child_inode)
            if child_entry is not None:
                children.append(child_entry)
        return children
