"""云盘后端适配层：把 yun.139.com Web API 包装成 FUSE 层熟悉的接口。

这个文件**不是** L3 协议实现——协议在 ``client/web139.py``（隔离层）。
这里只做三件事：

1. 把 ``WebFile`` 转成 FUSE 层一直使用的 ``CloudItem``；
2. 保留 v0.3.0 的公开方法名与签名（``list_directory`` / ``download_file`` /
   ``download_range`` / ``upload_file`` / ``create_folder``），让 FUSE 与缓存层
   无需改动；
3. 提供旧通道没有的 ``rename`` / ``move`` / ``delete``，其中 ``delete`` 默认
   调用 ``/file/batchDelete``，对应真正的 ``rm`` 语义（不进回收站）。
"""

from __future__ import annotations

import logging
import queue
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

from mcloud_fuse.client.web139 import (
    DiskQuota,
    RecycleItem,
    Web139Auth,
    Web139AuthError,
    Web139Client,
    Web139Error,
    WebFile,
)
from mcloud_fuse.config import (
    CONFLICT_MODE,
    RANGE_TIMEOUT_SEC,
    RM_PERMANENT,
    WEB_CLIENT_TYPE,
    WEB_PART_SIZE_BYTES,
)
from mcloud_fuse.errors import (
    MCloudAPIException,
    MCloudAuthException,
    MCloudConflictError,
)

logger = logging.getLogger(__name__)

DEFAULT_PAGE_LIMIT = 500
# 删除是异步任务：本地先隐藏，后台批量提交，避免 rm 逐个等 0.7s
DELETE_HIDE_TTL = 60.0
DELETE_BATCH_MAX = 100
MAX_PAGES = 200
MAX_LIST_ITEMS = 50000


@dataclass
class CloudItem:
    """云盘文件/目录的标准元数据结构（FUSE getattr / readdir 使用）。"""

    file_id: str
    name: str
    is_dir: bool
    size: int
    updated_at: str
    parent_id: str
    raw_data: Dict[str, Any] = field(default_factory=dict)


class DirectClientWrapper:
    """Web API 后端。构造时不会发起网络请求，第一次调用才解析 token。"""

    def __init__(self, client: Optional[Web139Client] = None):
        self.client = client or Web139Client(
            Web139Auth.load(),
            part_size=WEB_PART_SIZE_BYTES,
            client_type=WEB_CLIENT_TYPE,
        )
        self.auth = self.client.auth
        self.root_file_id = "/"

        # 已提交删除的 fileId -> 提交时间；用于在云端任务完成前把它们从列举里隐藏
        self._deleted: Dict[str, float] = {}
        self._deleted_lock = threading.Lock()
        self._delete_queue: "queue.Queue[tuple]" = queue.Queue()
        self._delete_cond = threading.Condition()
        self._delete_pending = 0
        self._delete_worker = threading.Thread(
            target=self._delete_loop, name="mcloud-delete", daemon=True
        )
        self._delete_worker.start()

    # ------------------------------------------------------------------ 兼容

    def _ensure_auth(self) -> None:
        """token 在每次请求时按需续期，这里只做存在性检查。"""
        if self.auth.token.is_expired():
            raise MCloudAuthException("Web token 已过期，请重新执行 import_web_token.py")

    # ------------------------------------------------------------------ 读

    @staticmethod
    def _to_item(item: WebFile) -> CloudItem:
        return CloudItem(
            file_id=item.file_id,
            name=item.name,
            is_dir=item.is_dir,
            size=item.size,
            updated_at=item.updated_at,
            parent_id=item.parent_id,
            raw_data=item.raw,
        )

    def list_directory(
        self,
        dir_file_id: str,
        recursive: bool = False,
        page_limit: int = DEFAULT_PAGE_LIMIT,
        max_items: int = MAX_LIST_ITEMS,
    ) -> List[CloudItem]:
        self._ensure_auth()
        if not dir_file_id:
            raise MCloudAPIException("目录 fileId 为空，无法列举")

        page_size = max(1, min(int(page_limit), 500))
        try:
            if not recursive:
                return [
                    self._to_item(item)
                    for item in self._take(
                        self.client.iter_files(dir_file_id, page_size=page_size), max_items
                    )
                    if not self._is_deleted(item.file_id)
                ]

            collected: List[CloudItem] = []
            queue = [dir_file_id]
            while queue and len(collected) < max_items:
                current = queue.pop(0)
                for item in self.client.iter_files(current, page_size=page_size):
                    if self._is_deleted(item.file_id):
                        continue
                    collected.append(self._to_item(item))
                    if item.is_dir:
                        queue.append(item.file_id)
                    if len(collected) >= max_items:
                        break
            return collected
        except Web139AuthError as exc:
            raise MCloudAuthException(str(exc)) from exc
        except Web139Error as exc:
            raise MCloudAPIException(str(exc)) from exc

    @staticmethod
    def _take(iterator, limit: int):
        for index, item in enumerate(iterator):
            if index >= limit:
                return
            yield item

    def find_child(self, parent_file_id: str, name: str) -> Optional[CloudItem]:
        """在目录里按名字找子项；命中即返回，不做整目录缓存。"""
        self._ensure_auth()
        try:
            item = self.client.find_child(parent_file_id, name)
        except Web139AuthError as exc:
            raise MCloudAuthException(str(exc)) from exc
        except Web139Error as exc:
            raise MCloudAPIException(str(exc)) from exc
        return self._to_item(item) if item is not None else None

    def file_name(self, file_id: str) -> str:
        try:
            return self.client.get_file(file_id).name
        except Web139Error as exc:
            raise MCloudAPIException(str(exc)) from exc

    def get_item(self, file_id: str) -> CloudItem:
        try:
            return self._to_item(self.client.get_file(file_id))
        except Web139Error as exc:
            raise MCloudAPIException(str(exc)) from exc

    def quota(self) -> DiskQuota:
        """个人云配额，单位 MB。"""
        try:
            return self.client.quota()
        except Web139AuthError as exc:
            raise MCloudAuthException(str(exc)) from exc
        except Web139Error as exc:
            raise MCloudAPIException(str(exc)) from exc

    def get_download_urls(self, file_ids: Sequence[str]) -> Dict[str, str]:
        urls: Dict[str, str] = {}
        for file_id in file_ids:
            try:
                urls[str(file_id)] = self.client.get_download_url(str(file_id))
            except Web139Error as exc:
                logger.warning("获取文件 %s 下载地址失败: %s", file_id, exc)
        return urls

    def download_file(self, file_id: str, local_save_path: Path) -> Path:
        self._ensure_auth()
        path = Path(local_save_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        url = self._download_url(file_id)
        try:
            with requests.get(url, stream=True, timeout=300) as resp:
                if resp.status_code != 200:
                    raise MCloudAPIException(f"下载失败：HTTP {resp.status_code}")
                written = 0
                with path.open("wb") as sink:
                    for chunk in resp.iter_content(1 << 20):
                        if chunk:
                            sink.write(chunk)
                            written += len(chunk)
        except requests.RequestException as exc:
            raise MCloudAPIException(f"下载文件 {file_id} 失败: {exc}") from exc
        logger.debug("下载完成: %s -> %s (%s bytes)", file_id, path, written)
        return path

    def download_range(
        self,
        file_id: str,
        local_save_path: Path,
        offset: int,
        length: int,
        expected_total: Optional[int] = None,
    ) -> Optional[int]:
        if length <= 0:
            return 0
        self._ensure_auth()
        path = Path(local_save_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        return self._range_get(
            self._download_url(file_id), str(path), int(offset), int(length), expected_total
        )

    def _download_url(self, file_id: str) -> str:
        try:
            return self.client.get_download_url(file_id)
        except Web139AuthError as exc:
            raise MCloudAuthException(str(exc)) from exc
        except Web139Error as exc:
            raise MCloudAPIException(str(exc)) from exc

    @staticmethod
    def _range_get(
        url: str,
        dest_path: str,
        offset: int,
        length: int,
        expected_total: Optional[int] = None,
    ) -> Optional[int]:
        headers = {"Range": f"bytes={offset}-{offset + length - 1}"}
        try:
            with requests.get(url, headers=headers, stream=True, timeout=RANGE_TIMEOUT_SEC) as resp:
                if resp.status_code == 416:
                    return 0
                if resp.status_code not in (200, 206):
                    raise MCloudAPIException(f"分片下载失败：HTTP {resp.status_code}")

                full_body = resp.status_code == 200
                if not full_body:
                    content_range = resp.headers.get("Content-Range", "")
                    parsed = _parse_content_range(content_range)
                    if parsed is None or parsed[0] != offset:
                        raise MCloudAPIException(
                            f"分片响应 Content-Range 异常: {content_range!r}，期望 offset={offset}"
                        )
                    total = parsed[2]
                    if expected_total is not None and total is not None and total != expected_total:
                        raise MCloudAPIException(
                            f"云端文件大小与元数据不一致：{total} != {expected_total}"
                        )

                mode = "wb" if full_body else "r+b"
                written = 0
                with open(dest_path, mode) as sink:
                    if not full_body:
                        sink.seek(offset)
                    for chunk in resp.iter_content(1 << 20):
                        if chunk:
                            sink.write(chunk)
                            written += len(chunk)

                if full_body:
                    if expected_total is not None and written != expected_total:
                        raise MCloudAPIException(
                            f"整文件下载不完整：收到 {written} 字节，期望 {expected_total}"
                        )
                    logger.info("服务端不支持 Range（HTTP 200），已整文件下载 %s 字节", written)
                    return None
                return written
        except requests.RequestException as exc:
            raise MCloudAPIException(f"分片下载失败：网络异常: {exc}") from exc
        except OSError as exc:
            raise MCloudAPIException(f"分片下载写盘失败: {exc}") from exc

    # ------------------------------------------------------------------ 写

    def create_folder(self, name: str, parent_file_id: str) -> str:
        try:
            return self.client.create_folder(parent_file_id, name)
        except Web139Error as exc:
            raise MCloudAPIException(str(exc)) from exc

    def rename(self, file_id: str, new_name: str) -> None:
        try:
            self.client.rename(file_id, new_name)
        except Web139Error as exc:
            raise MCloudAPIException(str(exc)) from exc

    def move(self, file_ids: Sequence[str], to_parent_file_id: str) -> None:
        try:
            self.client.move(file_ids, to_parent_file_id)
        except Web139Error as exc:
            raise MCloudAPIException(str(exc)) from exc

    def batch_copy(
        self, file_ids: Sequence[str], to_parent_file_id: str
    ) -> List[Dict[str, Any]]:
        try:
            self.client.copy(file_ids, to_parent_file_id)
        except Web139Error as exc:
            raise MCloudAPIException(str(exc)) from exc
        return [{"fileId": file_id, "newFileId": ""} for file_id in file_ids]

    def delete(self, file_ids: Sequence[str], *, permanent: Optional[bool] = None) -> None:
        """删除。默认按 ``MCLOUD_RM_MODE``：``delete``=真删，``trash``=回收站。

        删除是异步任务，逐个等待会让 ``rm -rf`` 慢到不可用。这里只做两件事：
        1. 立即把 fileId 记入本地隐藏集（列举时过滤）；
        2. 投入后台队列，由 worker 合并成一个 ``batchDelete`` 提交。
        """
        ids = [str(file_id) for file_id in file_ids if file_id]
        if not ids:
            return
        use_permanent = RM_PERMANENT if permanent is None else permanent
        now = time.monotonic()
        with self._deleted_lock:
            for file_id in ids:
                self._deleted[file_id] = now
        with self._delete_cond:
            self._delete_pending += 1
        self._delete_queue.put((ids, use_permanent))

    def _is_deleted(self, file_id: str) -> bool:
        now = time.monotonic()
        with self._deleted_lock:
            stamp = self._deleted.get(file_id)
            if stamp is None:
                return False
            if now - stamp <= DELETE_HIDE_TTL:
                return True
            self._deleted.pop(file_id, None)
            return False

    def _delete_loop(self) -> None:
        while True:
            entry = self._delete_queue.get()
            batch = [entry]
            try:
                while len(batch) < DELETE_BATCH_MAX:
                    try:
                        batch.append(self._delete_queue.get_nowait())
                    except queue.Empty:
                        break
                by_mode: Dict[bool, List[str]] = {}
                for ids, permanent in batch:
                    by_mode.setdefault(bool(permanent), []).extend(ids)
                for permanent, ids in by_mode.items():
                    try:
                        self.client.delete(ids, permanent=permanent)
                    except Exception as exc:  # noqa: BLE001 - 后台失败只能告警
                        logger.warning("后台删除 %s 个对象失败: %s", len(ids), exc)
            finally:
                with self._delete_cond:
                    self._delete_pending -= len(batch)
                    self._delete_cond.notify_all()

    def flush_deletes(self, timeout: float = 30.0) -> bool:
        """等待后台删除队列清空（测试与卸载前使用）。"""
        deadline = time.monotonic() + timeout
        with self._delete_cond:
            while self._delete_pending > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._delete_cond.wait(remaining)
        return True

    def trash(self, file_ids: Sequence[str]) -> None:
        self.delete(file_ids, permanent=False)

    def list_recycle_bin(self, page_size: int = 100) -> List[RecycleItem]:
        try:
            return self.client.list_recycle_bin(page_size)
        except Web139Error as exc:
            raise MCloudAPIException(str(exc)) from exc

    def restore_recycle(self, file_ids: Sequence[str]) -> None:
        try:
            self.client.restore_recycle(file_ids)
        except Web139Error as exc:
            raise MCloudAPIException(str(exc)) from exc

    def clear_recycle_bin(self) -> None:
        try:
            self.client.clear_recycle_bin()
        except Web139Error as exc:
            raise MCloudAPIException(str(exc)) from exc

    def upload_file(
        self,
        local_file_path: Path,
        target_dir_id: str,
        name: Optional[str] = None,
        *,
        expected_file_id: Optional[str] = None,
        expected_updated_at: Optional[str] = None,
    ) -> CloudItem:
        path = Path(local_file_path)
        if not path.is_file():
            raise MCloudAPIException(f"待上传文件不存在: {path}")
        file_name = name or path.name

        if expected_file_id and expected_updated_at:
            try:
                current = self.client.get_file(expected_file_id)
                remote_updated = current.updated_at
            except Web139Error:
                remote_updated = ""
            if remote_updated != expected_updated_at:
                if CONFLICT_MODE == "error":
                    raise MCloudConflictError(
                        f"{file_name} 在打开后被远端修改（{expected_updated_at} -> {remote_updated or '已删除'}）"
                    )
                file_name = _conflict_name(file_name)
                logger.warning(
                    "检测到远端修改，本地版本另存为 %s（远端原文件保留）", file_name
                )
        try:
            file_id = self.client.upload_file(path, target_dir_id, file_name)
        except Web139AuthError as exc:
            raise MCloudAuthException(str(exc)) from exc
        except Web139Error as exc:
            raise MCloudAPIException(f"上传文件 {file_name} 失败: {exc}") from exc
        return CloudItem(
            file_id=file_id,
            name=file_name,
            is_dir=False,
            size=path.stat().st_size,
            updated_at="",
            parent_id=target_dir_id,
            raw_data={},
        )


def _conflict_name(name: str, limit: int = 255) -> str:
    """生成 ``name.conflict-<host>-<timestamp>``，保持文件名不超长。"""
    host = re.sub(r"[^A-Za-z0-9._-]", "_", socket.gethostname())[:32] or "host"
    suffix = f".conflict-{host}-{time.strftime('%Y%m%d-%H%M%S')}"
    return name[: max(1, limit - len(suffix))] + suffix


def _parse_content_range(value: str) -> Optional[Tuple[int, int, Optional[int]]]:
    if not value or not value.startswith("bytes "):
        return None
    span, _, total_text = value[6:].partition("/")
    start_text, _, end_text = span.partition("-")
    if not start_text.strip().isdigit() or not end_text.strip().isdigit():
        return None
    start = int(start_text.strip())
    end = int(end_text.strip())
    total = int(total_text.strip()) if total_text.strip().isdigit() else None
    return start, end, total
