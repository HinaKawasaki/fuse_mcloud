import os
import logging
from typing import Tuple

logger = logging.getLogger(__name__)

# 移动云盘 Skill 规定的默认沙箱根路径
SANDBOX_ROOT = "/AI空间/智能体云盘助手"
TRASH_BIN_NAME = ".TrashBin"

class SandboxRouter:
    """
    沙箱路径路由与自动确认管理器
    """
    def __init__(self, sandbox_root: str = SANDBOX_ROOT, auto_confirm: bool = True):
        self.sandbox_root = sandbox_root.rstrip("/")
        self.auto_confirm = auto_confirm

    def normalize_path(self, path: str) -> str:
        """将 POSIX 绝对/相对路径归一化为以 '/' 开头的标准路径"""
        path = os.path.normpath(path)
        if not path.startswith("/"):
            path = "/" + path
        return path

    def is_in_sandbox(self, path: str) -> bool:
        """检查路径是否位于沙箱目录内"""
        norm_path = self.normalize_path(path)
        return norm_path == self.sandbox_root or norm_path.startswith(self.sandbox_root + "/")

    def to_sandbox_path(self, path: str) -> str:
        """
        确保路径位于沙箱内：如果传入相对沙箱的路径，自动补全沙箱根前缀
        """
        norm_path = self.normalize_path(path)
        if self.is_in_sandbox(norm_path):
            return norm_path
        
        # 如果是相对根路径，自动拼接沙箱前缀
        return os.path.normpath(os.path.join(self.sandbox_root, norm_path.lstrip("/")))

    def get_confirm_option(self) -> dict:
        """获取自动确认参数字典，用于直接传递给底层 API"""
        return {"confirm": True} if self.auto_confirm else {}