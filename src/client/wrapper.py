import sys
import os
import time
import shutil
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Dict, Any

# 1. 动态注入 basement/scripts 路径以加载原生底层模块
BASEMENT_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "basement" / "scripts"
if str(BASEMENT_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(BASEMENT_SCRIPTS_DIR))

# 2. 导入底层模块与错误定义
import cm_cloud_client as client  # noqa: E402
from cm_cloud_errors import CloudError, ApiError, ValidationError  # noqa: E402
from client.auth_manager import AuthManager

logger = logging.getLogger(__name__)

# ... (后面的 CloudItem 和 DirectClientWrapper 代码保持不变，不要引用 DataCache)



@dataclass
class CloudItem:
    """云盘文件/目录的标准元数据结构 (对应 FUSE getattr/readdir 需求)"""
    file_id: str
    name: str
    is_dir: bool
    size: int
    updated_at: str
    parent_id: str
    raw_data: Dict[str, Any]


class MCloudAPIException(Exception):
    """底层 API 统一封装异常"""
    pass


class DirectClientWrapper:
    """基于 cm_cloud_client.py 原生 Python 函数的直接包装器 (Direct Import 模式)"""

    def __init__(self, auth_manager: AuthManager):
        self.auth = auth_manager

    def _ensure_auth(self):
        """确保 Token 有效并已被 transport 加载"""
        self.auth.get_valid_token(issue_login=False)

    def search_or_list_files(
        self,
        keyword: str = "",
        include_file_ids: Optional[List[str]] = None,
        recursive: bool = False,
        page_after: Optional[Any] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """
        列出或搜索文件 (底层统一使用 api_search_files)
        """
        self._ensure_auth()
        try:
            res = client.api_search_files(
                keyword=keyword,
                include_file_ids=include_file_ids,
                recursive=recursive,
                page_after=page_after,
                limit=limit
            )
            items = [self._parse_raw_file(f) for f in res.files]
            return {
                "items": items,
                "total_count": res.total_count,
                "next_page_after": res.next_page_after,
                "has_more": res.next_page_after is not None
            }
        except (ApiError, ValidationError, CloudError) as e:
            logger.error(f"查询文件列表失败: {e}")
            raise MCloudAPIException(f"查询文件列表失败: {e}") from e

    def create_folder(self, name: str, parent_file_id: str) -> str:
        """创建文件夹，返回新目录的 file_id"""
        self._ensure_auth()
        try:
            return client.api_create_folder(parent_file_id=parent_file_id, name=name)
        except CloudError as e:
            raise MCloudAPIException(f"创建文件夹失败: {e}") from e

    def ensure_workspace(self) -> str:
        """确保沙箱工作区建立，返回沙箱根目录 file_id"""
        self._ensure_auth()
        try:
            return client.ensure_workspace()
        except CloudError as e:
            raise MCloudAPIException(f"确保沙箱目录失败: {e}") from e

    def batch_copy(self, file_ids: List[str], to_parent_file_id: str) -> List[Dict[str, Any]]:
        """批量复制文件并等待完成 (供 .TrashBin/ 回收站逻辑使用)"""
        self._ensure_auth()
        try:
            return client.batch_copy_and_wait(file_ids=file_ids, to_parent_file_id=to_parent_file_id)
        except CloudError as e:
            raise MCloudAPIException(f"批量复制失败: {e}") from e

    def batch_get_download_urls(self, file_ids: List[str]) -> Dict[str, str]:
        """批量获取下载链接，返回 {file_id: url} 字典"""
        self._ensure_auth()
        try:
            res = client.api_batch_download_urls(file_ids)
            if res.failures:
                logger.warning(f"部分文件下载链接获取失败: {res.failures}")
            return res.urls
        except CloudError as e:
            raise MCloudAPIException(f"获取下载地址失败: {e}") from e

    @staticmethod
    def _parse_raw_file(raw: Dict[str, Any]) -> CloudItem:
        """解析底层 normalize 后的字典为 CloudItem 对象"""
        return CloudItem(
            file_id=str(raw.get("fileId") or raw.get("id") or ""),
            name=str(raw.get("name") or raw.get("fileName") or ""),
            is_dir=bool(raw.get("isDir") or raw.get("type") == "folder"),
            size=int(raw.get("size") or 0),
            updated_at=str(raw.get("updatedAt") or raw.get("updateTime") or ""),
            parent_id=str(raw.get("parentFileId") or raw.get("parentId") or ""),
            raw_data=raw
        )