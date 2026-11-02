"""yun.139.com Web API 客户端（隔离的 L3 适配层）。

背景：官方 Skill（L2）只暴露 10 个端点，且厂商在 v2.0.2 里**主动停用**了
删除 / 移动 / 重命名。Web 端 `https://yun.139.com/hcy/*` 仍然提供这些能力：

    /file/list          目录枚举（专用接口，不依赖搜索索引）
    /file/getDownloadUrl 下载直链
    /file/create        创建上传任务 / 新建目录
    /file/getUploadUrl  追加分片上传地址
    /file/complete      完成上传
    /file/update        重命名
    /file/batchMove     移动
    /file/batchCopy     复制
    /recyclebin/batchTrash  删除到回收站

本模块**不进入默认挂载路径**，只提供可独立测试的客户端。启用方式与风险见
``docs/adr/0002-web-api-optin.md``。协议细节参考 OpenList/AList 的 139 驱动
（``drivers/139``）与浏览器 DevTools 的实测请求头。

鉴权有两条路：
1. 直接提供浏览器里 ``hcy/file/list`` 请求头 ``Authorization: Basic <...>`` 的
   值（粘贴到文件或环境变量）；
2. 在 token 临近过期时调用 ``tellin/authTokenRefresh.do`` 静默续期。

不在这里实现账号密码登录（需要处理 RSA/AES 与风控），也绝不落盘账号密码。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import string
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

import requests

from mcloud_fuse import config
from mcloud_fuse.router.rate_limiter import global_rate_limiter

logger = logging.getLogger(__name__)

WEB_BASE = "https://yun.139.com/hcy"
ROUTE_URL = "https://user-njs.yun.139.com/user/route/qryRoutePolicy"
REFRESH_URL = "https://aas.caiyun.feixin.10086.cn/tellin/authTokenRefresh.do"
QUOTA_URL = "https://user-njs.yun.139.com/user/disk/quota/detail"

# token 剩余寿命低于该阈值时续期（与 AList 驱动一致）
REFRESH_LEAD_MS = 15 * 24 * 60 * 60 * 1000

DEFAULT_PART_SIZE = 100 * 1024 * 1024
MAX_PARTS_PER_CREATE = 100
# 删除/移动是异步任务：轮询 /task/get，直到 Succeed/Failed
TASK_POLL_INTERVAL = 0.3
TASK_TIMEOUT_SEC = 60.0

_UNRESERVED = "-_.!~*'()"


class Web139Error(Exception):
    """Web API 调用失败。"""

    def __init__(self, message: str, code: Optional[str] = None, status: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.status = status


class Web139AuthError(Web139Error):
    """token 缺失、过期或续期失败。"""


@dataclass(frozen=True)
class Web139Token:
    """``Authorization: Basic`` 里那段 base64 的解码结果。"""

    prefix: str
    account: str
    payload: str

    @classmethod
    def parse(cls, raw: str) -> "Web139Token":
        value = (raw or "").strip()
        if value.lower().startswith("basic "):
            value = value[6:].strip()
        if not value:
            raise Web139AuthError("Authorization 为空")
        try:
            padded = value + "=" * (-len(value) % 4)
            decoded = base64.b64decode(padded, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise Web139AuthError(f"Authorization 不是合法的 base64：{exc}") from exc
        parts = decoded.split(":", 2)
        if len(parts) < 3 or not parts[1].strip():
            raise Web139AuthError("Authorization 结构不合法（期望 prefix:account:payload）")
        if not parts[2]:
            raise Web139AuthError("Authorization payload 为空")
        return cls(parts[0], parts[1], parts[2])

    def to_basic(self) -> str:
        raw = f"{self.prefix}:{self.account}:{self.payload}".encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    @property
    def expires_at_ms(self) -> Optional[int]:
        fields = self.payload.split("|")
        if len(fields) >= 4 and fields[3].strip().isdigit():
            return int(fields[3])
        return None

    def needs_refresh(self, now_ms: Optional[int] = None, lead_ms: int = REFRESH_LEAD_MS) -> bool:
        expiry = self.expires_at_ms
        if expiry is None:
            return False
        now = int(time.time() * 1000) if now_ms is None else now_ms
        return expiry - now <= lead_ms

    def is_expired(self, now_ms: Optional[int] = None) -> bool:
        expiry = self.expires_at_ms
        if expiry is None:
            return False
        now = int(time.time() * 1000) if now_ms is None else now_ms
        return expiry <= now


class Web139Auth:
    """token 的加载、续期与落盘（0600）。"""

    def __init__(self, token: Web139Token, path: Optional[Path] = None):
        self._token = token
        self.path = path
        self._lock = threading.Lock()

    @classmethod
    def load(
        cls,
        token: Optional[str] = None,
        path: Optional[Path] = None,
    ) -> "Web139Auth":
        token_path = Path(path) if path else config.WEB_TOKEN_FILE
        raw = token or os.getenv("MCLOUD_WEB_TOKEN")
        if not raw:
            try:
                raw = token_path.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                raw = ""
        if not raw:
            raise Web139AuthError(
                f"未找到 Web token。请把 yun.139.com 的 Authorization（Basic 后面的值）"
                f"写入 {token_path}，或设置 MCLOUD_WEB_TOKEN"
            )
        return cls(Web139Token.parse(raw), token_path)

    @property
    def token(self) -> Web139Token:
        return self._token

    def authorization(self, session: requests.Session, timeout: float = 15) -> str:
        """返回可用的 Basic 值，必要时先续期。"""
        with self._lock:
            if not self._token.needs_refresh():
                if self._token.is_expired():
                    raise Web139AuthError("Web token 已过期，请重新从浏览器复制 Authorization")
                return self._token.to_basic()
            self._token = self._refresh(session, timeout)
            self.save()
            return self._token.to_basic()

    def _refresh(self, session: requests.Session, timeout: float) -> Web139Token:
        body = (
            "<root><token>"
            + _xml_escape(self._token.payload)
            + "</token><account>"
            + _xml_escape(self._token.account)
            + "</account><clienttype>656</clienttype></root>"
        )
        try:
            resp = session.request(
                "POST",
                REFRESH_URL,
                data=body.encode("utf-8"),
                headers={"Content-Type": "application/xml"},
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise Web139AuthError(f"token 续期请求失败：{exc}") from exc
        if resp.status_code != 200:
            raise Web139AuthError(f"token 续期失败：HTTP {resp.status_code}")

        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as exc:
            raise Web139AuthError(f"token 续期响应不是合法 XML：{exc}") from exc
        if (root.findtext("return") or "") != "0":
            desc = root.findtext("desc") or "未知原因"
            raise Web139AuthError(f"token 续期被拒绝：{desc}")

        new_payload = root.findtext("token") or ""
        if not new_payload:
            raise Web139AuthError("token 续期响应缺少新 token")

        refreshed = Web139Token(self._token.prefix, self._token.account, new_payload)
        if refreshed.expires_at_ms is None:
            expire_s = root.findtext("expiretime") or ""
            expiry = (
                int(time.time() * 1000) + int(expire_s) * 1000
                if expire_s.isdigit()
                else int(time.time() * 1000) + 30 * 24 * 60 * 60 * 1000
            )
            fields = refreshed.payload.split("|")
            if len(fields) >= 4:
                fields[3] = str(expiry)
                payload = "|".join(fields)
            else:
                payload = refreshed.payload + "|" + str(expiry)
            refreshed = Web139Token(refreshed.prefix, refreshed.account, payload)
        logger.info("Web token 已自动续期")
        return refreshed

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(self._token.to_basic() + "\n")
            os.replace(tmp, self.path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass


@dataclass(frozen=True)
class DiskQuota:
    """配额，单位 MB（实测：上传 64MiB 后 freeDiskSize 减少 64）。"""

    total_mb: int
    free_mb: int
    used_mb: int
    raw: Dict[str, Any]

    @classmethod
    def from_api(cls, data: Dict[str, Any]) -> "DiskQuota":
        total = int(data.get("diskSize") or 0)
        free = int(data.get("freeDiskSize") or 0)
        if total <= 0:
            raise Web139Error("配额接口没有返回 diskSize")
        return cls(
            total_mb=total,
            free_mb=max(0, free),
            used_mb=max(0, total - free),
            raw=data,
        )


@dataclass(frozen=True)
class RecycleItem:
    file_id: str
    name: str
    is_dir: bool
    size: int
    trashed_at: str
    remaining_days: int
    raw: Dict[str, Any]

    @classmethod
    def from_api(cls, item: Dict[str, Any]) -> "RecycleItem":
        stats = item.get("trashedDirStatistics") or {}
        size = int(item.get("size") or 0)
        if item.get("type") == "folder" and stats:
            size = int(stats.get("totalSize") or size)
        return cls(
            file_id=str(item.get("fileId") or ""),
            name=str(item.get("name") or ""),
            is_dir=item.get("type") == "folder",
            size=size,
            trashed_at=str(item.get("trashedAt") or ""),
            remaining_days=int(item.get("remainingTime") or 0),
            raw=item,
        )


@dataclass(frozen=True)
class WebFile:
    file_id: str
    name: str
    is_dir: bool
    size: int
    created_at: str
    updated_at: str
    parent_id: str
    raw: Dict[str, Any]

    @classmethod
    def from_api(cls, item: Dict[str, Any], parent_id: str) -> "WebFile":
        return cls(
            file_id=str(item.get("fileId", "")),
            name=str(item.get("name", "")),
            is_dir=item.get("type") == "folder",
            size=int(item.get("size") or 0),
            created_at=str(item.get("createdAt") or ""),
            updated_at=str(item.get("updatedAt") or ""),
            parent_id=parent_id,
            raw=item,
        )


def mcloud_sign(body: str, timestamp: str, rand: str) -> str:
    """复刻 Web 端的 ``mcloud-sign``：排序后的 URL 编码 body + 时间/随机串。"""
    encoded = urllib.parse.quote(body, safe=_UNRESERVED)
    shuffled = "".join(sorted(encoded))
    digest = hashlib.md5(base64.b64encode(shuffled.encode("utf-8"))).hexdigest()
    digest += hashlib.md5(f"{timestamp}:{rand}".encode("utf-8")).hexdigest()
    return hashlib.md5(digest.encode("utf-8")).hexdigest().upper()


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _random_str(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _plan_parts(size: int, part_size: int) -> List[Dict[str, Any]]:
    if part_size <= 0:
        raise ValueError("part_size 必须大于 0")
    count = size // part_size
    if size % part_size or count == 0:
        count += 1
    parts = []
    for index in range(count):
        start = index * part_size
        length = min(part_size, size - start)
        parts.append(
            {
                "partNumber": index + 1,
                "partSize": length,
                "parallelHashCtx": {"partOffset": start},
            }
        )
    return parts


class _FileSlice:
    """供 requests 流式上传的只读文件片段，避免整片读进内存。"""

    def __init__(self, path: Path, offset: int, length: int):
        self._handle = path.open("rb")
        self._handle.seek(offset)
        self._remaining = length
        self._length = length

    def read(self, amount: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        if amount is None or amount < 0 or amount > self._remaining:
            amount = self._remaining
        data = self._handle.read(amount)
        self._remaining -= len(data)
        return data

    def __len__(self) -> int:
        return self._length

    def close(self) -> None:
        self._handle.close()


class Web139Client:
    """最小可用的 yun.139.com Web 客户端。

    只负责协议；限流、重试与缓存由上层决定。所有方法都是同步阻塞的。
    """

    def __init__(
        self,
        auth: Optional[Web139Auth] = None,
        *,
        session: Optional[requests.Session] = None,
        timeout: float = 30.0,
        part_size: int = DEFAULT_PART_SIZE,
        client_type: str = "13",
        rate_limiter: Any = global_rate_limiter,
        base_url: Optional[str] = None,
    ):
        self.auth = auth or Web139Auth.load()
        self._session = session or requests.Session()
        self._timeout = timeout
        self._part_size = part_size
        self._client_type = client_type
        self._limiter = rate_limiter
        self._base_url = base_url
        self._host: Optional[str] = base_url
        self._host_lock = threading.Lock()

    # ------------------------------------------------------------------ 传输

    def _headers(self, body: str, timestamp: str, rand: str) -> Dict[str, str]:
        sign = mcloud_sign(body, timestamp, rand)
        return {
            "Accept": "application/json, text/plain, */*",
            "Authorization": "Basic " + self.auth.authorization(self._session),
            "Caller": "web",
            "Cms-Device": "default",
            "Content-Type": "application/json;charset=UTF-8",
            "Mcloud-Channel": "1000101",
            "Mcloud-Client": "10701",
            "Mcloud-Route": "001",
            "Mcloud-Sign": f"{timestamp},{rand},{sign}",
            "Mcloud-Version": "7.14.0",
            "x-DeviceInfo": "||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||",
            "x-huawei-channelSrc": "10000034",
            "x-inner-ntwk": "2",
            "x-m4c-caller": "PC",
            "x-m4c-src": "10002",
            "x-SvcType": "1",
            "X-Yun-Api-Version": "v1",
            "X-Yun-App-Channel": "10000034",
            "X-Yun-Channel-Source": "10000034",
            "X-Yun-Client-Info": (
                f"||{self._client_type}|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||"
                "dW5kZWZpbmVk||"
            ),
            "X-Yun-Module-Type": "100",
            "X-Yun-Svc-Type": "1",
        }

    def _post_json(
        self,
        path: str,
        payload: Dict[str, Any],
        *,
        url: Optional[str] = None,
        headers_factory: Optional[Callable[[str, str, str], Dict[str, str]]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        rand = _random_str()
        headers = (headers_factory or self._headers)(body, timestamp, rand)
        target = url or (self._get_host() + path)
        if self._limiter is not None:
            self._limiter.acquire()
        try:
            resp = self._session.request(
                "POST",
                target,
                data=body.encode("utf-8"),
                headers=headers,
                timeout=timeout or self._timeout,
            )
        except requests.RequestException as exc:
            raise Web139Error(f"请求 {path} 失败：{exc}") from exc
        finally:
            if self._limiter is not None:
                self._limiter.release()

        if resp.status_code in (401, 403):
            raise Web139AuthError(
                f"接口返回 HTTP {resp.status_code}，token 可能失效", status=resp.status_code
            )
        if resp.status_code != 200:
            raise Web139Error(
                f"接口 {path} 返回 HTTP {resp.status_code}: {resp.text[:200]}",
                status=resp.status_code,
            )
        try:
            doc = resp.json()
        except ValueError as exc:
            raise Web139Error(f"接口 {path} 返回非 JSON：{resp.text[:200]}") from exc
        if isinstance(doc, dict) and doc.get("success") is False:
            code = str(doc.get("code") or "")
            message = str(doc.get("message") or "未知错误")
            if code in ("401", "403"):
                raise Web139AuthError(f"接口拒绝：{message}", code=code)
            raise Web139Error(f"接口 {path} 失败：{message}", code=code)
        return doc if isinstance(doc, dict) else {"data": doc}

    @staticmethod
    def _data(doc: Dict[str, Any]) -> Dict[str, Any]:
        data = doc.get("data")
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ 路由

    def _route_headers(self, body: str, timestamp: str, rand: str) -> Dict[str, str]:
        sign = mcloud_sign(body, timestamp, rand)
        return {
            "Accept": "application/json, text/plain, */*",
            "Authorization": "Basic " + self.auth.authorization(self._session),
            "CMS-DEVICE": "default",
            "Content-Type": "application/json;charset=UTF-8",
            "Inner-Hcy-Router-Https": "1",
            "Origin": "https://yun.139.com",
            "Referer": "https://yun.139.com/w/",
            "mcloud-channel": "1000101",
            "mcloud-client": "10701",
            "mcloud-sign": f"{timestamp},{rand},{sign}",
            "mcloud-version": "7.14.0",
            "x-DeviceInfo": "||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||",
            "x-huawei-channelSrc": "10000034",
            "x-inner-ntwk": "2",
            "x-m4c-caller": "PC",
            "x-m4c-src": "10002",
            "x-SvcType": "1",
        }

    def _get_host(self) -> str:
        if self._host:
            return self._host
        with self._host_lock:
            if self._host:
                return self._host
            account = self.auth.token.account
            doc = self._post_json(
                "",
                {
                    "userInfo": {"userType": 1, "accountType": 1, "accountName": account},
                    "modAddrType": 1,
                },
                url=ROUTE_URL,
                headers_factory=self._route_headers,
                timeout=15,
            )
            host = ""
            for item in self._data(doc).get("routePolicyList") or []:
                if item.get("modName") == "personal" and item.get("httpsUrl"):
                    host = str(item["httpsUrl"]).rstrip("/")
                    break
            if not host:
                logger.warning("未从路由策略拿到 personal 域名，回退到 %s", WEB_BASE)
                host = WEB_BASE
            self._host = host
            return host

    # ------------------------------------------------------------------ 读

    def iter_files(self, parent_id: str = "/", page_size: int = 500) -> Iterator[WebFile]:
        """按页遍历目录（服务端游标分页）。"""
        cursor = ""
        while True:
            doc = self._post_json(
                "/file/list",
                {
                    # 不请求缩略图：FUSE 用不到，省掉每页的 URL 生成与服务端开销
                    "orderBy": "updated_at",
                    "orderDirection": "DESC",
                    "pageInfo": {"pageCursor": cursor, "pageSize": page_size},
                    "parentFileId": parent_id,
                },
            )
            data = self._data(doc)
            for item in data.get("items") or []:
                yield WebFile.from_api(item, parent_id)
            cursor = str(data.get("nextPageCursor") or "")
            if not cursor:
                return

    def list_files(self, parent_id: str = "/", page_size: int = 500) -> List[WebFile]:
        return list(self.iter_files(parent_id, page_size))

    def find_child(self, parent_id: str, name: str) -> Optional[WebFile]:
        for item in self.iter_files(parent_id):
            if item.name == name:
                return item
        return None

    def quota(self) -> DiskQuota:
        """取个人云配额。返回值的单位是 MB。"""
        doc = self._post_json("", {}, url=QUOTA_URL, timeout=15)
        return DiskQuota.from_api(self._data(doc))

    def iter_recycle_bin(self, page_size: int = 100) -> Iterator[RecycleItem]:
        """遍历回收站（游标分页）。"""
        cursor = ""
        while True:
            doc = self._post_json(
                "/recyclebin/list",
                {"pageInfo": {"pageSize": page_size, "pageCursor": cursor}},
            )
            data = self._data(doc)
            for item in data.get("items") or []:
                yield RecycleItem.from_api(item)
            cursor = str(data.get("nextPageCursor") or "")
            if not cursor:
                return

    def list_recycle_bin(self, page_size: int = 100) -> List[RecycleItem]:
        return list(self.iter_recycle_bin(page_size))

    def get_download_url(self, file_id: str) -> str:
        doc = self._post_json("/file/getDownloadUrl", {"fileId": file_id})
        data = self._data(doc)
        url = data.get("cdnUrl") or data.get("url")
        if not url:
            raise Web139Error(f"文件 {file_id} 没有返回下载地址")
        return str(url)

    def get_file(self, file_id: str) -> WebFile:
        """按 fileId 取单个对象的元数据（``/file/get``）。"""
        doc = self._post_json("/file/get", {"fileId": file_id})
        data = self._data(doc)
        item = data.get("file") if isinstance(data.get("file"), dict) else data
        if not item or not item.get("fileId"):
            raise Web139Error(f"文件 {file_id} 不存在或响应缺少元数据")
        return WebFile.from_api(item, str(item.get("parentFileId") or ""))

    def batch_get(self, file_ids: Sequence[str]) -> List[WebFile]:
        """批量取元数据（``/file/batchGet``）。"""
        if not file_ids:
            return []
        doc = self._post_json("/file/batchGet", {"fileIds": list(file_ids)})
        data = self._data(doc)
        items = data.get("items") or data.get("files") or []
        return [WebFile.from_api(item, str(item.get("parentFileId") or "")) for item in items]

    # ------------------------------------------------------------------ 写

    def create_folder(self, parent_id: str, name: str, *, rename_mode: str = "refuse") -> str:
        """新建目录；默认 ``refuse`` 让同名冲突显式失败（对应 EEXIST）。"""
        doc = self._post_json(
            "/file/create",
            {
                "parentFileId": parent_id,
                "name": name,
                "description": "",
                "type": "folder",
                "fileRenameMode": rename_mode,
            },
        )
        file_id = str(self._data(doc).get("fileId") or "")
        if not file_id:
            raise Web139Error("新建目录成功但响应缺少 fileId")
        return file_id

    def rename(self, file_id: str, new_name: str) -> None:
        self._post_json("/file/update", {"fileId": file_id, "name": new_name, "description": ""})

    def move(self, file_ids: Sequence[str], to_parent_id: str) -> None:
        if not file_ids:
            return
        doc = self._post_json(
            "/file/batchMove", {"fileIds": list(file_ids), "toParentFileId": to_parent_id}
        )
        self._wait_task(doc, "移动")

    def copy(self, file_ids: Sequence[str], to_parent_id: str) -> None:
        if not file_ids:
            return
        doc = self._post_json(
            "/file/batchCopy", {"fileIds": list(file_ids), "toParentFileId": to_parent_id}
        )
        self._wait_task(doc, "复制")

    def delete(self, file_ids: Sequence[str], *, permanent: bool = True) -> None:
        """删除。

        ``permanent=True`` 走 ``/file/batchDelete``（对应 Web 端的 logicalDelete，
        不进回收站，这是 ``rm`` 应有的语义）；``False`` 走 ``/recyclebin/batchTrash``。
        """
        if not file_ids:
            return
        path = "/file/batchDelete" if permanent else "/recyclebin/batchTrash"
        doc = self._post_json(path, {"fileIds": list(file_ids)})
        self._wait_task(doc, "删除")

    def trash(self, file_ids: Sequence[str]) -> None:
        """删除到回收站（真正释放容量还需在回收站里清空）。"""
        self.delete(file_ids, permanent=False)

    def restore_recycle(self, file_ids: Sequence[str]) -> None:
        """从回收站恢复。"""
        if not file_ids:
            return
        doc = self._post_json("/recyclebin/batchRestore", {"fileIds": list(file_ids)})
        self._wait_task(doc, "恢复")

    def clear_recycle_bin(self) -> None:
        """清空回收站。"""
        doc = self._post_json("/recyclebin/clear", {})
        self._wait_task(doc, "清空回收站")

    def _wait_task(self, doc: Dict[str, Any], action: str) -> None:
        """删除/移动/复制返回 taskId 时，等任务真正结束再返回。

        不等的话，FUSE 的 unlink 返回后立刻重新列举，可能又把还没删完的
        对象读回来（表现为「删不掉的幽灵目录」）。
        """
        task_id = str(self._data(doc).get("taskId") or "")
        if not task_id:
            return
        deadline = time.monotonic() + TASK_TIMEOUT_SEC
        while True:
            try:
                info_doc = self._post_json("/task/get", {"taskId": task_id})
                info = self._data(info_doc).get("taskInfo") or {}
                status = str(info.get("status") or "")
            except Web139Error as exc:
                raise Web139Error(f"{action}任务查询失败：{exc}") from exc
            if status in ("Succeed", "Failed"):
                if status == "Failed":
                    raise Web139Error(
                        f"{action}任务失败：{info.get('message') or info.get('code') or '未知原因'}"
                    )
                return
            if time.monotonic() >= deadline:
                raise Web139Error(f"{action}任务 {task_id} 超时（最后状态 {status or '未知'}）")
            time.sleep(TASK_POLL_INTERVAL)

    # ------------------------------------------------------------------ 上传

    def upload_file(
        self,
        local_path: Path,
        parent_id: str,
        name: Optional[str] = None,
        *,
        part_size: Optional[int] = None,
        overwrite: bool = True,
        retries: int = 3,
    ) -> str:
        """整文件上传，返回 fileId。按 100 片一批获取上传地址，顺序 PUT。"""
        path = Path(local_path)
        filename = name or path.name
        size = path.stat().st_size
        chunk = part_size or self._part_size
        parts = _plan_parts(size, chunk)
        content_hash = _sha256_file(path)

        create_body: Dict[str, Any] = {
            "contentHash": content_hash,
            "contentHashAlgorithm": "SHA256",
            "contentType": "application/octet-stream",
            "parallelUpload": False,
            "partInfos": parts[:MAX_PARTS_PER_CREATE],
            "size": size,
            "parentFileId": parent_id,
            "name": filename,
            "type": "file",
            "fileRenameMode": "auto_rename" if overwrite else "refuse",
        }
        doc = self._post_json("/file/create", create_body)
        data = self._data(doc)
        file_id = str(data.get("fileId") or "")
        if not file_id:
            raise Web139Error("创建上传任务成功但响应缺少 fileId")
        if data.get("exist"):
            return file_id

        upload_id = str(data.get("uploadId") or "")
        upload_parts = data.get("partInfos") or []
        if upload_parts:
            for start in range(MAX_PARTS_PER_CREATE, len(parts), MAX_PARTS_PER_CREATE):
                batch = parts[start : start + MAX_PARTS_PER_CREATE]
                more = self._post_json(
                    "/file/getUploadUrl",
                    {
                        "fileId": file_id,
                        "uploadId": upload_id,
                        "partInfos": batch,
                        "commonAccountInfo": {
                            "account": self.auth.token.account,
                            "accountType": 1,
                        },
                    },
                )
                upload_parts = list(upload_parts) + list(self._data(more).get("partInfos") or [])

            for part in upload_parts:
                part_number = int(part.get("partNumber") or 0)
                if part_number < 1 or part_number > len(parts):
                    raise Web139Error(f"服务端返回了越界分片序号 {part_number}")
                plan = parts[part_number - 1]
                self._put_part(
                    str(part.get("uploadUrl") or ""),
                    path,
                    int(plan["parallelHashCtx"]["partOffset"]),
                    int(plan["partSize"]),
                    retries=retries,
                )

        if upload_id:
            self._post_json(
                "/file/complete",
                {
                    "contentHash": content_hash,
                    "contentHashAlgorithm": "SHA256",
                    "fileId": file_id,
                    "uploadId": upload_id,
                },
            )

        server_name = str(data.get("fileName") or filename)
        if overwrite and server_name != filename:
            self._resolve_conflict(parent_id, filename, file_id, server_name)
        return file_id

    def _put_part(self, url: str, path: Path, offset: int, length: int, *, retries: int) -> None:
        if not url:
            raise Web139Error("分片缺少上传地址")
        last_error: Optional[Exception] = None
        for attempt in range(max(1, retries)):
            stream = _FileSlice(path, offset, length)
            try:
                resp = self._session.request(
                    "PUT",
                    url,
                    data=stream,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(length),
                        "Origin": "https://yun.139.com",
                        "Referer": "https://yun.139.com/",
                    },
                    timeout=600,
                )
            except requests.RequestException as exc:
                last_error = exc
                continue
            finally:
                stream.close()
            if resp.status_code == 200:
                return
            last_error = Web139Error(f"分片上传返回 HTTP {resp.status_code}")
            logger.warning("分片上传失败（第 %d 次）：%s", attempt + 1, last_error)
        assert last_error is not None
        raise Web139Error(f"分片上传失败：{last_error}") from last_error

    def _resolve_conflict(self, parent_id: str, name: str, new_id: str, server_name: str) -> None:
        """服务端把同名文件改成了 ``name(1)``：清掉旧对象再把新对象改回原名。"""
        try:
            existing = self.find_child(parent_id, name)
            if existing and existing.file_id != new_id:
                tombstone = f"{name}.mcloud-old-{_random_str(6)}"
                self.rename(existing.file_id, tombstone)
                self.delete([existing.file_id], permanent=config.RM_PERMANENT)
            self.rename(new_id, name)
        except Web139Error as exc:
            logger.warning("覆盖写冲突处理未完成：%s（新文件保留为 %s）", exc, server_name)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
