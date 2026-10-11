import os
import logging
from datetime import datetime
from typing import Optional
from utils.sandbox_router import SandboxRouter, TRASH_BIN_NAME

logger = logging.getLogger(__name__)

class TrashBinHandler:
    """
    回收站处理器：拦截 unlink/rmdir，将删除操作转化为重定向 copy 到 .TrashBin/ 目录
    """
    def __init__(self, router: Optional[SandboxRouter] = None):
        self.router = router or SandboxRouter()

    def get_trash_dir(self) -> str:
        """获取回收站绝对路径，如 '/AI空间/智能体云盘助手/.TrashBin'"""
        return f"{self.router.sandbox_root}/{TRASH_BIN_NAME}"

    def generate_trash_path(self, target_path: str) -> str:
        """
        为被删除的文件生成带时间戳的回收站目标路径
        例如: /path/to/file.txt -> /AI空间/智能体云盘助手/.TrashBin/20261004_013500_file.txt
        """
        filename = os.path.basename(target_path)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        trash_name = f"{timestamp}_{filename}"
        return f"{self.get_trash_dir()}/{trash_name}"

    async def ensure_trash_dir_exists(self, client_wrapper) -> bool:
        """
        确保云盘沙箱内存在 .TrashBin 目录；若不存在则自动调用 client 创建
        """
        trash_dir = self.get_trash_dir()
        try:
            # 尝试创建 .TrashBin 目录
            await client_wrapper.mkdir(trash_dir, confirm=True)
            logger.info(f"回收站目录已就绪: {trash_dir}")
            return True
        except Exception as e:
            # 如果文件夹已存在或创建成功均视为正常
            logger.debug(f"检查/创建回收站目录提示: {e}")
            return True

    async def move_to_trash(self, client_wrapper, target_path: str) -> str:
        """
        执行删除拦截逻辑：调用 copy API 将文件/文件夹复制到回收站目录
        """
        await self.ensure_trash_dir_exists(client_wrapper)
        
        trash_target_path = self.generate_trash_path(target_path)
        logger.info(f"[TrashBin] 拦截删除指令，将文件从 '{target_path}' 移动(copy)至回收站: '{trash_target_path}'")
        
        # 调用底层 API 的 copy 能力实现“移入回收站”
        result = await client_wrapper.copy(
            source_path=target_path,
            target_path=trash_target_path,
            confirm=True
        )
        return trash_target_path