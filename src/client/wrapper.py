import logging
import math
import shutil
import sys
import tempfile
import time
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
    HYDRATE_DIR_NAME,
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
        # 沙箱根 fileId（懒查询并缓存），挂在云盘根时用于定位 .TrashBin / 暂存目录
        self._sandbox_root_id: Optional[str] = None
        self._hydrate_dir_id: Optional[str] = None
        # 源 fileId -> 暂存副本 fileId（进程内缓存，避免重复搬运）
        self._staged_by_source: Dict[str, str] = {}

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
    # 沙箱外文件懒搬运（挂载云盘根时读取沙箱外文件）
    #
    # 注意：这一整段**刻意不使用 list_directory()（=搜索接口）**。
    # 搜索接口是最终一致的，且会返回「缓存信息失效」这类服务端错误；懒搬运只
    # 依赖 batchCheckExists（同名检查）、batchCopy（云内复制）与 batchGet（取名）。
    # ------------------------------------------------------------------

    def sandbox_root_file_id(self) -> str:
        """沙箱根 fileId；与挂载根解耦（挂载根可能是云盘 root）。"""
        if self._sandbox_root_id is None:
            self._sandbox_root_id = self.ensure_workspace()
        return self._sandbox_root_id

    def ensure_child_folder(self, parent_file_id: str, name: str) -> str:
        """返回父目录下名为 name 的子目录 fileId，不存在则创建（不依赖搜索接口）。"""
        existing = self._api(client.api_check_exists, parent_file_id, name)
        if existing.get("exist") and existing.get("fileId"):
            return str(existing["fileId"])

        try:
            return self._api(client.api_create_folder, parent_file_id, name)
        except (ApiError, ValidationError):
            # 并发/索引场景下可能已经建好了，再查一次
            recheck = self._api(client.api_check_exists, parent_file_id, name)
            if recheck.get("exist") and recheck.get("fileId"):
                return str(recheck["fileId"])
            raise

    def ensure_hydrate_dir(self) -> str:
        """确保沙箱根下存在懒搬运暂存目录，返回其 fileId（进程内缓存）。"""
        if self._hydrate_dir_id:
            return self._hydrate_dir_id

        self._hydrate_dir_id = self.ensure_child_folder(
            self.sandbox_root_file_id(), HYDRATE_DIR_NAME
        )
        logger.info("懒搬运暂存目录 %s → %s", HYDRATE_DIR_NAME, self._hydrate_dir_id)
        self._await_path_visible(self._hydrate_dir_id, label="暂存目录")
        return self._hydrate_dir_id

    def _await_path_visible(
        self, file_id: str, *, label: str, attempts: int = 10, pause: float = 1.0
    ) -> str:
        """等待新对象的云盘路径可被 batchGetPath 查到，返回查到的路径（超时返回 ""）。

        官方 Skill 的写操作（`batchCopy` 等）在真正提交前会调用
        `ensure_files_in_sandbox()`，用 batchGetPath 反查目标路径是否在沙箱内。
        刚创建 / 刚复制出来的对象可能还没被路径索引收录，于是抛
        「权限拒绝：无法确认 xxx 的云盘路径」——被 FUSE 映射成 EACCES。
        这里先等路径出现，再让 SDK 去做校验。
        """
        for index in range(1, attempts + 1):
            try:
                path = self._api(sandbox.resolve_cloud_path, file_id)
            except CloudError as exc:  # 路径查询本身也可能偶发失败
                path = ""
                logger.debug("路径查询失败 %s: %s", file_id, exc)
            if path:
                if index > 1:
                    logger.info("%s 的路径已就绪（第 %s 次）: %s", label, index, path)
                return str(path)
            if index == 1:
                logger.info("%s 的云盘路径尚未就绪，等待索引 ...", label)
            time.sleep(pause)
        logger.warning("%s 的云盘路径在 %.0fs 内仍不可见: %s", label, attempts * pause, file_id)
        return ""

    def file_name(self, file_id: str) -> str:
        """取云端文件名（batchGet），失败时退回 fileId。"""
        try:
            names = self._api(client.api_batch_get_names, [str(file_id)])
        except CloudError as exc:
            logger.debug("取文件名失败 %s: %s", file_id, exc)
            return str(file_id)
        return str(names.get(str(file_id)) or file_id)

    def hydrate_file(self, file_id: str) -> str:
        """把沙箱外文件云内复制进沙箱暂存目录，返回副本 fileId。

        每个源文件在暂存目录下占一个以自身 fileId 命名的子目录，因此副本与源
        文件同名也不会冲突；进程重启后按「槽位 + 文件名」做同名检查即可找回已有
        副本，不会重复搬运。
        """
        key = str(file_id)
        cached = self._staged_by_source.get(key)
        if cached:
            return cached

        slot = self.ensure_child_folder(self.ensure_hydrate_dir(), key)
        name = self.file_name(key)

        existing = self._api(client.api_check_exists, slot, name)
        if existing.get("exist") and existing.get("fileId"):
            new_id = str(existing["fileId"])
            logger.info("复用已有暂存副本: %s -> %s", key, new_id)
        else:
            # 新目录/新对象要等路径索引收录，否则 SDK 的沙箱校验会判「无法确认路径」
            self._await_path_visible(slot, label="暂存槽位")
            new_id = self._extract_copied_id(key, self.batch_copy([key], slot))
            if not new_id:
                # 接口未回传 newFileId 时，按文件名再查一次（仍然不碰搜索接口）
                recheck = self._api(client.api_check_exists, slot, name)
                new_id = str(recheck.get("fileId") or "")
            if not new_id:
                raise MCloudAPIException(f"懒搬运失败：{key} 的副本未出现在暂存目录")
            logger.info("沙箱外文件已搬入暂存目录: %s -> %s", key, new_id)
            self._await_path_visible(new_id, label="暂存副本")

        self._staged_by_source[key] = new_id
        return new_id

    @staticmethod
    def _extract_copied_id(
        source_id: str, results: Optional[Sequence[Dict[str, Any]]]
    ) -> str:
        """从 batch_copy 结果里取出源文件对应的副本 fileId。"""
        rows = list(results or [])
        for row in rows:
            if str(row.get("fileId") or "") == source_id:
                return str(row.get("newFileId") or "")
        for row in rows:
            new_id = str(row.get("newFileId") or "")
            if new_id:
                return new_id
        return ""

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
        """把云盘文件下载到本地指定路径，返回本地路径。

        沙箱外文件（挂载云盘根时常见）先云内复制进沙箱暂存目录再下载：
        官方接口只允许下载沙箱内文件，copy 的源则可以是全盘任意位置。
        「是否在沙箱外」不额外查询路径——取直链时 SDK 会抛出
        `PermissionDeniedError`，这本身就是最准确的信号，也省掉一次接口调用。
        """
        self._ensure_auth()
        local_save_path = Path(local_save_path)
        local_save_path.parent.mkdir(parents=True, exist_ok=True)

        url, outside_sandbox = self._try_download_url(file_id)
        if url is None and outside_sandbox:
            staged = self.hydrate_file(file_id)
            url, still_denied = self._try_download_url(staged)
            if url is None and still_denied:
                raise MCloudAPIException(
                    f"懒搬运已完成，但暂存副本 {staged} 仍取不到下载地址"
                    f"（沙箱路径可能还没就绪），原文件 {file_id}"
                )
        if not url:
            raise MCloudAPIException(f"未能获取文件 {file_id} 的下载地址（可能不在沙箱内）")

        try:
            written = self._api(transport.stream_download, url, str(local_save_path))
        except (ApiError, OSError) as e:
            raise MCloudAPIException(f"下载文件 {file_id} 失败: {e}") from e

        logger.debug("下载完成: %s -> %s (%s bytes)", file_id, local_save_path, written)
        return local_save_path

    def _try_download_url(self, file_id: str) -> tuple:
        """取下载直链，返回 ``(url_or_None, 是否因沙箱权限被拒)``。"""
        try:
            result = self._api(client.api_batch_download_urls, [str(file_id)])
        except PermissionDeniedError:
            # 官方 Skill 的沙箱校验：目标不在沙箱内 → 需要先搬运
            logger.info("文件 %s 不在沙箱内，将在云内搬运副本后再下载", file_id)
            return None, True
        except CloudError as exc:
            logger.warning("获取文件 %s 的下载地址失败: %s", file_id, exc)
            return None, False
        return result.urls.get(str(file_id)), False

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
