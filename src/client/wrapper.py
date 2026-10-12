import logging
import math
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# 1. 动态注入 basement/scripts 路径以加载原生底层模块
BASEMENT_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "basement" / "scripts"
if str(BASEMENT_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(BASEMENT_SCRIPTS_DIR))

# 2. 导入底层模块与错误定义
import cm_cloud_client as client  # noqa: E402
import cm_cloud_sandbox as sandbox  # noqa: E402
import cm_cloud_transport as transport  # noqa: E402
from cm_cloud_errors import (  # noqa: E402
    ApiError,
    CloudError,
    PermissionDeniedError,
    ValidationError,
)
from cm_cloud_format import sha256_file  # noqa: E402

from client.auth_manager import AuthManager  # noqa: E402
from config import (  # noqa: E402
    MAX_PARTS,
    MIN_PART_SIZE_BYTES,
    MULTIPART_THRESHOLD_BYTES,
    PART_RETRIES,
)
from router.rate_limiter import global_rate_limiter  # noqa: E402
from router.retry import retry_call  # noqa: E402

logger = logging.getLogger(__name__)

# 单次目录列举的默认分页大小与安全上限，避免异常目录把内存打爆
DEFAULT_PAGE_LIMIT = 200
MAX_PAGES = 200
MAX_LIST_ITEMS = 50000

# 底层上传槽位接口路径（多段上传需要自己拼 partInfos）
_PATH_FILE_CREATE = "/richlifeApp/personalSaas/file/create"


@dataclass
class CloudItem:
    """云盘文件/目录的标准元数据结构 (对应 FUSE getattr/readdir 需求)"""
    file_id: str
    name: str
    is_dir: bool
    size: int
    updated_at: str
    parent_id: str
    raw_data: Dict[str, Any] = field(default_factory=dict)


class MCloudAPIException(Exception):
    """底层 API 统一封装异常"""
    pass


class DirectClientWrapper:
    """基于 cm_cloud_client.py 原生 Python 函数的直接包装器 (Direct Import 模式)

    - 所有云盘请求都经过 ``_api()``：统一限流（并发 + 每秒请求数）与指数退避重试；
    - 大文件上传自动改用多段并发上传（分片可单独重试），失败时回退单段。
    """

    def __init__(self, auth_manager: AuthManager):
        self.auth = auth_manager

    # ------------------------------------------------------------------
    # 统一调用封装：限流 + 退避重试
    # ------------------------------------------------------------------

    def _api(self, func, *args, **kwargs):
        """限流 + 指数退避地执行一次同步云盘调用。"""
        def _do():
            with global_rate_limiter:
                return func(*args, **kwargs)

        return retry_call(_do)

    def _ensure_auth(self):
        """确保 Token 有效并已被 transport 加载"""
        self.auth.get_valid_token(issue_login=False)

    # ------------------------------------------------------------------
    # 搜索 / 目录列举
    # ------------------------------------------------------------------

    def _search_page(
        self,
        keyword: str = "",
        include_file_ids: Optional[Sequence[str]] = None,
        recursive: bool = False,
        page_after: Optional[Any] = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> Dict[str, Any]:
        """调用底层 search 接口取一页，并解析为 CloudItem 列表。"""
        try:
            res = self._api(
                client.api_search_files,
                keyword=keyword,
                include_file_ids=include_file_ids,
                recursive=recursive,
                page_after=page_after,
                limit=limit,
            )
        except (ApiError, ValidationError, PermissionDeniedError, CloudError) as e:
            logger.error("查询文件列表失败: %s", e)
            raise MCloudAPIException(f"查询文件列表失败: {e}") from e

        items = [self._parse_raw_file(f) for f in res.files]
        return {
            "items": items,
            "total_count": res.total_count,
            "next_page_after": res.next_page_after,
            "has_more": res.next_page_after is not None,
        }

    def list_directory(
        self,
        dir_file_id: str,
        recursive: bool = False,
        page_limit: int = DEFAULT_PAGE_LIMIT,
        max_items: int = MAX_LIST_ITEMS,
    ) -> List[CloudItem]:
        """列举指定云盘目录的子项，自动翻页直到取完。

        这是 readdir / lookup 唯一应该使用的目录列举入口。
        同一次列举内按 fileId 去重，避免接口重发导致同名重复项。
        """
        self._ensure_auth()
        if not dir_file_id:
            raise MCloudAPIException("目录 fileId 为空，无法列举")

        collected: List[CloudItem] = []
        seen_ids: set = set()
        page_after: Optional[Any] = None

        for _page_index in range(MAX_PAGES):
            page = self._search_page(
                keyword="",
                include_file_ids=[dir_file_id],
                recursive=recursive,
                page_after=page_after,
                limit=page_limit,
            )
            for item in page["items"]:
                dedupe_key = item.file_id or f"{item.name}:{item.parent_id}"
                if dedupe_key in seen_ids:
                    continue
                seen_ids.add(dedupe_key)
                collected.append(item)
                if len(collected) >= max_items:
                    logger.warning(
                        "目录 %s 子项超过安全上限 %s，已截断", dir_file_id, max_items
                    )
                    return collected

            if not page["has_more"]:
                break
            page_after = page["next_page_after"]
        else:
            logger.warning("目录 %s 翻页超过 %s 页，已停止", dir_file_id, MAX_PAGES)

        return collected

    # ------------------------------------------------------------------
    # 写入 / 目录创建
    # ------------------------------------------------------------------

    def create_folder(self, name: str, parent_file_id: str) -> str:
        """创建文件夹，返回新目录的 file_id"""
        self._ensure_auth()
        try:
            return self._api(client.api_create_folder, parent_file_id=parent_file_id, name=name)
        except CloudError as e:
            raise MCloudAPIException(f"创建文件夹失败: {e}") from e

    def ensure_workspace(self) -> str:
        """确保沙箱工作区建立，返回沙箱根目录 file_id"""
        self._ensure_auth()
        try:
            return self._api(client.ensure_workspace)
        except CloudError as e:
            raise MCloudAPIException(f"确保沙箱目录失败: {e}") from e

    # ------------------------------------------------------------------
    # 批量复制（供回收站等使用）
    # ------------------------------------------------------------------

    def batch_copy(self, file_ids: List[str], to_parent_file_id: str) -> List[Dict[str, Any]]:
        """批量复制文件并等待完成 (供 .TrashBin/ 回收站逻辑使用)"""
        self._ensure_auth()
        try:
            return self._api(
                client.batch_copy_and_wait,
                file_ids=file_ids,
                to_parent_file_id=to_parent_file_id,
            )
        except CloudError as e:
            raise MCloudAPIException(f"批量复制失败: {e}") from e

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------

    def get_download_urls(self, file_ids: Sequence[str]) -> Dict[str, str]:
        """批量获取下载直链，返回 {file_id: url} 字典"""
        self._ensure_auth()
        try:
            res = self._api(client.api_batch_download_urls, list(file_ids))
            if res.failures:
                logger.warning("部分文件下载链接获取失败: %s", res.failures)
            return res.urls
        except CloudError as e:
            raise MCloudAPIException(f"获取下载地址失败: {e}") from e

    def download_file(self, file_id: str, local_save_path: Path) -> Path:
        """把云盘文件下载到本地指定路径，返回本地路径。"""
        self._ensure_auth()
        local_save_path = Path(local_save_path)
        local_save_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            urls = self.get_download_urls([file_id])
        except MCloudAPIException:
            raise
        url = urls.get(str(file_id))
        if not url:
            raise MCloudAPIException(f"未能获取文件 {file_id} 的下载地址（可能不在沙箱内）")

        try:
            written = self._api(transport.stream_download, url, str(local_save_path))
        except (ApiError, OSError) as e:
            raise MCloudAPIException(f"下载文件 {file_id} 失败: {e}") from e

        logger.debug("下载完成: %s -> %s (%s bytes)", file_id, local_save_path, written)
        return local_save_path

    # ------------------------------------------------------------------
    # 上传
    # ------------------------------------------------------------------

    @staticmethod
    def _plan_parts(size: int) -> Optional[List[int]]:
        """规划分片大小。返回 None 表示走单段上传。

        约束（实测得出）：
        - 每个分片（除最后一片）不得小于 ``MIN_PART_SIZE_BYTES``（5MiB），否则接口报“请求参数不合法”；
        - 最后一片可以更小；
        - 总片数不超过 ``MAX_PARTS``。
        """
        if size <= MULTIPART_THRESHOLD_BYTES:
            return None
        if size <= MIN_PART_SIZE_BYTES:
            return None

        max_parts = max(2, int(MAX_PARTS))
        need_parts = max(2, math.ceil(size / MIN_PART_SIZE_BYTES))
        part_size = max(MIN_PART_SIZE_BYTES, math.ceil(size / min(max_parts, need_parts)))

        sizes = [part_size] * (size // part_size)
        remainder = size - part_size * len(sizes)
        if remainder:
            sizes.append(remainder)
        if len(sizes) < 2:
            return None
        return sizes

    def _create_multipart_task(
        self, name: str, size: int, parent_file_id: str, part_sizes: Sequence[int]
    ) -> Dict[str, Any]:
        """申请多段上传槽位，返回 {'upload_id','file_id','urls': {partNumber: url}}。"""
        payload = {
            "size": int(size),
            "name": name,
            "type": "file",
            "partInfos": [
                {"partNumber": index + 1, "partSize": int(part_size)}
                for index, part_size in enumerate(part_sizes)
            ],
            "parentFileId": parent_file_id,
        }
        raw = self._api(
            transport.post_json, _PATH_FILE_CREATE, payload, retries=1
        )
        data = transport.ensure_success(raw, action="创建上传任务")

        urls: Dict[int, str] = {}
        for index, part in enumerate(data.get("partInfos") or []):
            if not isinstance(part, dict):
                continue
            part_number = int(part.get("partNumber") or index + 1)
            url = part.get("uploadUrl") or part.get("url")
            if url:
                urls[part_number] = str(url)

        upload_id = str(data.get("uploadId") or "")
        file_id = str(data.get("fileId") or "")
        if not upload_id or not file_id or len(urls) != len(part_sizes):
            raise ApiError(
                f"多段上传槽位不完整：uploadId={bool(upload_id)} fileId={bool(file_id)} "
                f"parts={len(urls)}/{len(part_sizes)}"
            )
        return {"upload_id": upload_id, "file_id": file_id, "urls": urls}

    def _upload_multipart(
        self, local_path: Path, name: str, size: int, target_dir_id: str
    ) -> CloudItem:
        """多段上传：**必须严格按 partNumber 顺序**逐片上传，末片再 complete。

        实测该后端（EOS）会维护“上一片哈希上下文”，并发或乱序上传会返回
        ``InvalidPartOrder: fail to get previous part hash context``，因此分片
        只能顺序发送。分片级重试仍然保留：单片网络失败只重传该片。
        """
        part_sizes = self._plan_parts(size)
        if not part_sizes:
            raise ApiError("无需多段上传")

        content_hash = sha256_file(str(local_path))
        task = self._create_multipart_task(name, size, target_dir_id, part_sizes)
        urls = task["urls"]

        offset = 0
        total = len(part_sizes)
        tmp_dir = Path(tempfile.mkdtemp(prefix="mcloud-parts-"))
        try:
            with open(local_path, "rb") as source:
                for index, part_size in enumerate(part_sizes):
                    part_number = index + 1
                    source.seek(offset)
                    slice_path = tmp_dir / f"part_{part_number:05d}.bin"
                    with open(slice_path, "wb") as sink:
                        remaining = part_size
                        while remaining > 0:
                            chunk = source.read(min(1 << 20, remaining))
                            if not chunk:
                                break
                            sink.write(chunk)
                            remaining -= len(chunk)
                    offset += part_size
                    try:
                        retry_call(
                            transport.put_file_content,
                            urls[part_number],
                            str(slice_path),
                            max_retries=PART_RETRIES,
                        )
                    except ValidationError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        raise ApiError(
                            f"分片 {part_number}/{total} 上传失败: {exc}"
                        ) from exc
                    finally:
                        try:
                            slice_path.unlink()
                        except OSError:
                            pass
                    if part_number % 5 == 0 or part_number == total:
                        logger.info("多段上传进度: %s/%s 片", part_number, total)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        self._api(
            client.api_file_complete,
            task["upload_id"],
            task["file_id"],
            content_hash,
        )
        logger.info(
            "多段上传完成: %s (%s 片, %s bytes) -> fileId=%s",
            name, total, size, task["file_id"],
        )
        return CloudItem(
            file_id=task["file_id"],
            name=name,
            is_dir=False,
            size=size,
            updated_at="",
            parent_id=target_dir_id,
            raw_data={},
        )

    def _upload_single(
        self, local_path: Path, name: str, size: int, target_dir_id: str
    ) -> CloudItem:
        """单段上传（官方 Skill 原路径）。"""
        content_hash = sha256_file(str(local_path))
        slot = self._api(
            client.api_file_create, name, size, parent_file_id=target_dir_id
        )
        self._api(transport.put_file_content, slot.upload_url, str(local_path))
        self._api(client.api_file_complete, slot.upload_id, slot.file_id, content_hash)
        return CloudItem(
            file_id=slot.file_id,
            name=name,
            is_dir=False,
            size=size,
            updated_at="",
            parent_id=slot.parent_file_id,
            raw_data={},
        )

    def upload_file(
        self,
        local_file_path: Path,
        target_dir_id: str,
        name: Optional[str] = None,
    ) -> CloudItem:
        """把本地文件整文件上传到云盘目录，返回新文件的 CloudItem。

        大文件自动多段并发上传；多段不可用时回退单段。
        """
        self._ensure_auth()
        local_file_path = Path(local_file_path)
        if not local_file_path.is_file():
            raise MCloudAPIException(f"待上传文件不存在: {local_file_path}")

        file_name = name or local_file_path.name
        size = local_file_path.stat().st_size

        # 目标目录必须在沙箱内（多段路径绕过了 api_file_create 的内置校验）
        try:
            self._api(
                sandbox.ensure_files_in_sandbox,
                [target_dir_id],
                label="上传目标目录",
            )
        except CloudError as e:
            raise MCloudAPIException(f"上传目标目录校验失败: {e}") from e

        if self._plan_parts(size):
            try:
                return self._upload_multipart(local_file_path, file_name, size, target_dir_id)
            except (ValidationError, ApiError) as exc:
                logger.warning("多段上传失败，回退单段上传: %s", exc)

        try:
            return self._upload_single(local_file_path, file_name, size, target_dir_id)
        except CloudError as e:
            raise MCloudAPIException(f"上传文件 {file_name} 失败: {e}") from e

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_raw_file(raw: Dict[str, Any]) -> CloudItem:
        """解析底层 normalize 后的字典为 CloudItem 对象"""
        raw_type = str(raw.get("type") or "").strip().lower()
        category = str(raw.get("category") or "").strip().lower()
        is_dir = bool(
            raw.get("isDir")
            or raw.get("is_dir")
            or raw_type in ("folder", "dir", "directory")
            or category == "folder"
        )
        return CloudItem(
            file_id=str(raw.get("fileId") or raw.get("file_id") or raw.get("id") or ""),
            name=str(raw.get("name") or raw.get("fileName") or ""),
            is_dir=is_dir,
            size=int(raw.get("size") or 0),
            updated_at=str(raw.get("updatedAt") or raw.get("updateTime") or ""),
            parent_id=str(raw.get("parentFileId") or raw.get("parentId") or ""),
            raw_data=raw,
        )
