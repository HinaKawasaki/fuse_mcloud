import logging
from typing import Optional

from mcloud_fuse.config import TRASH_BIN_DIR_NAME, TRASH_COPY_ENABLED
from mcloud_fuse.mapping.id_map import InodeEntry, InodeMapper

logger = logging.getLogger(__name__)


class TrashBinHandler:
    """
    回收站处理器：拦截 unlink/rmdir，把删除动作改写为「登记墓碑 + 本地摘除」。

    移动云盘接口**没有任何删除能力**（整个 Skill 只有 create/search/copy/download 一类
    接口），因此 POSIX 删除无法真正落到云端：原对象永远留在云盘上，由墓碑（tombstone）
    保证它不再出现在目录列举里，对用户表现为「已删除」。

    历史上这里还会把对象云内复制到 ``.TrashBin``，但那只是额外占用一倍容量，
    而 ``.TrashBin`` 在本挂载里是隐藏目录、用户看不到也删不掉，收益为零。
    现在默认只登记墓碑（``MCLOUD_TRASH_COPY=1`` 可恢复旧的复制行为）。
    """

    def __init__(
        self,
        wrapper,
        mapper: InodeMapper,
        tree,
        *,
        trash_name: str = TRASH_BIN_DIR_NAME,
        tombstones=None,
        copy_to_trash: Optional[bool] = None,
    ):
        self.wrapper = wrapper
        self.mapper = mapper
        self.tree = tree
        self.trash_name = trash_name
        self.tombstones = (
            tombstones if tombstones is not None else getattr(tree, "tombstones", None)
        )
        # None = 每次按 config 决定（便于测试与运行时覆盖）；显式 bool 则固定
        self.copy_to_trash = copy_to_trash
        self._trash_file_id: Optional[str] = None

    # ------------------------------------------------------------------
    # 回收站目录
    # ------------------------------------------------------------------

    def should_copy(self) -> bool:
        """当前是否把被删对象额外复制一份进 ``.TrashBin``。

        显式传了 ``copy_to_trash`` 就固定；否则每次读配置，便于运行时/测试覆盖。
        """
        if self.copy_to_trash is None:
            return bool(TRASH_COPY_ENABLED)
        return bool(self.copy_to_trash)

    @property
    def sandbox_root_file_id(self) -> str:
        """沙箱根对应的云盘 fileId。

        挂载云盘根（--root cloud）时，挂载根是云盘 root，而回收站仍必须建在
        沙箱内（官方 Skill 只允许向沙箱写），因此优先向 wrapper 要沙箱根。
        """
        getter = getattr(self.wrapper, "sandbox_root_file_id", None)
        if callable(getter):
            return getter()

        root_entry = self.mapper.get_by_inode(InodeMapper.ROOT_INODE)
        if root_entry is None:
            raise RuntimeError("根节点缺失，无法定位沙箱根目录")
        return root_entry.file_id

    def ensure_trash_dir(self) -> str:
        """确保沙箱根下存在 .TrashBin 目录，返回其 fileId（进程内缓存）。

        优先走 wrapper.ensure_child_folder（batchCheckExists + createFolder），
        **不碰搜索接口**：搜索是最终一致的，且会返回「缓存信息失效」这类服务端
        错误，removal 不该被它拖垮。测试替身没有该方法时退回列举/创建。
        """
        if self._trash_file_id:
            return self._trash_file_id

        root_id = self.sandbox_root_file_id
        ensure_child_folder = getattr(self.wrapper, "ensure_child_folder", None)
        if callable(ensure_child_folder):
            self._trash_file_id = ensure_child_folder(root_id, self.trash_name)
            logger.info("回收站目录 %s → %s", self.trash_name, self._trash_file_id)
            return self._trash_file_id

        for item in self.wrapper.list_directory(root_id):
            if item.name == self.trash_name:
                self._trash_file_id = item.file_id
                return item.file_id

        self._trash_file_id = self.wrapper.create_folder(self.trash_name, root_id)
        logger.info("已创建回收站目录 %s (%s)", self.trash_name, self._trash_file_id)
        return self._trash_file_id

    # ------------------------------------------------------------------
    # 移入回收站
    # ------------------------------------------------------------------

    def move_to_trash(self, entry: InodeEntry) -> Optional[str]:
        """把节点从本地目录树摘除，并登记墓碑防止它下次列举时「复活」。

        :return: 复制模式下回收站副本的 fileId；墓碑模式（默认）下返回 ``None``，
                 因为云端根本没有产生副本——原对象留在原处，只是被墓碑隐藏。
        """
        if entry.inode == InodeMapper.ROOT_INODE:
            raise ValueError("不允许删除根目录")

        new_file_id = None
        if self.should_copy():
            trash_file_id = self.ensure_trash_dir()
            try:
                results = self.wrapper.batch_copy([entry.file_id], trash_file_id)
            except Exception as exc:  # noqa: BLE001 - 交由上层映射 errno
                logger.error("移入回收站失败 fileId=%s: %s", entry.file_id, exc)
                raise

            for row in results or []:
                if str(row.get("fileId") or "") == str(entry.file_id):
                    new_file_id = row.get("newFileId") or None
                    break
            if new_file_id is None and results:
                new_file_id = results[0].get("newFileId") or None
            logger.info("已复制到回收站: %s -> %s", entry.path, new_file_id or "(未知副本)")
        else:
            logger.info(
                "删除 %s：接口无删除能力，登记墓碑并本地摘除（云端原对象保留，不额外占用容量）",
                entry.path,
            )

        parent_inode = entry.parent_inode
        # 云端原对象依然存在（接口无删除能力），登记墓碑以免下次列举时复活
        if self.tombstones is not None:
            self.tombstones.add(entry.file_id)
        self.mapper.remove_by_inode(entry.inode)
        self.tree.mark_directory_stale(parent_inode)
        return new_file_id
