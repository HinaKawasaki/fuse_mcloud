"""token 的解析、描述与落盘。

原来这些逻辑只存在于 ``scripts/import_web_token.py``，首次设置向导没法复用。
现在收敛到这里：脚本、``mcloud-fuse setup``、``mcloud-fuse status`` 共用同一套
校验与 0600 写入，避免三处行为漂移。
"""

from __future__ import annotations

import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mcloud_fuse.client.web139 import Web139AuthError, Web139Token
from mcloud_fuse.userconfig import default_token_path

_BASIC_RE = re.compile(r"authorization\s*:\s*basic\s+([A-Za-z0-9+/=_-]+)", re.IGNORECASE)
_BEARER_RE = re.compile(r"authorization\s*:\s*([A-Za-z0-9+/=_-]{40,})", re.IGNORECASE)
_COOKIE_HINTS = ("skey=", "auth_token=", "token=", "ud_id=")


@dataclass(frozen=True)
class SaveResult:
    token: Web139Token
    wrote: bool
    message: str


def extract_basic(raw: str) -> str:
    """从 cURL / ``Authorization`` 行 / 裸 base64 中提取 Basic 值。

    识别 Cookie 这类常见误粘贴并给出可执行的纠正提示。
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("输入为空：请粘贴 Copy as cURL，或 Authorization 那一行。")

    match = _BASIC_RE.search(text)
    if match:
        return match.group(1)
    match = _BEARER_RE.search(text)
    if match:
        return match.group(1)

    lowered = text.lower()
    cookie_names = [name for name in _COOKIE_HINTS if name in lowered]
    if cookie_names:
        raise ValueError(
            "这看起来是 Cookie（" + "、".join(cookie_names) + "），不是 Authorization。\n"
            "请回到 DevTools → Network → 过滤 file/list → 点开请求 → "
            "Request Headers 里复制 `Authorization: Basic ...` 这一整行。"
        )

    candidate = text.splitlines()[-1].strip().strip("'\"")
    if candidate.lower().startswith("basic "):
        candidate = candidate[6:].strip()
    return candidate


def mask_account(account: str) -> str:
    """手机号等账号的展示脱敏：``138****1234``。"""
    value = (account or "").strip()
    if len(value) >= 8:
        return f"{value[:3]}{'*' * (len(value) - 7)}{value[-4:]}"
    if len(value) > 2:
        return f"{value[0]}{'*' * (len(value) - 2)}{value[-1]}"
    return value or "（未知账号）"


def describe(token: Web139Token, now_ms: Optional[int] = None) -> str:
    expiry = token.expires_at_ms
    account = mask_account(token.account)
    if expiry is None:
        return f"账号 {account}；token 未携带过期时间"
    now = int(time.time() * 1000) if now_ms is None else now_ms
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(expiry / 1000))
    days = (expiry - now) / 86400000
    if days <= 0:
        return f"账号 {account}；已于 {when} 过期"
    return f"账号 {account}；{when} 到期（剩余 {days:.1f} 天）"


def load_saved(path: Optional[Path] = None) -> Optional[Web139Token]:
    """读取已保存 token；不存在返回 ``None``，损坏或过期由调用方处理。

    未显式给 ``path`` 时优先使用 ``MCLOUD_WEB_TOKEN``（与 Web139Auth 一致），
    这样只靠环境变量跑 CI / 临时挂载时，``status`` 也能显示正确的账号。
    """
    if path is None:
        raw_env = os.getenv("MCLOUD_WEB_TOKEN")
        if raw_env and raw_env.strip():
            return Web139Token.parse(raw_env.strip())
    target = Path(path).expanduser() if path else default_token_path()
    try:
        raw = target.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise Web139AuthError(f"无法读取 token 文件 {target}：{exc}") from exc
    if not raw:
        return None
    return Web139Token.parse(raw)


def save_basic(
    basic: str,
    path: Optional[Path] = None,
    *,
    force: bool = False,
    now_ms: Optional[int] = None,
) -> SaveResult:
    """校验并原子写入 token（0600）。

    默认保留「到期更晚」的旧 token，避免用户误粘贴旧内容把好 token 覆盖掉。
    """
    token = Web139Token.parse(basic)
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if token.is_expired(now):
        raise Web139AuthError("这个 token 已经过期，请在 Chrome 里刷新云盘页面后重新复制。")

    target = Path(path).expanduser() if path else default_token_path()
    if target.exists() and not force:
        try:
            old = Web139Token.parse(target.read_text(encoding="utf-8"))
        except (OSError, Web139AuthError):
            old = None
        if (
            old is not None
            and not old.is_expired(now)
            and old.expires_at_ms
            and token.expires_at_ms
            and old.expires_at_ms >= token.expires_at_ms
        ):
            return SaveResult(
                old,
                False,
                f"已存在的 token 更晚到期，保留原文件（{describe(old, now)}）",
            )

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token.to_basic() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    return SaveResult(token, True, f"token 已保存到 {target}（0600）；{describe(token, now)}")


def token_summary(path: Optional[Path] = None) -> str:
    """给 ``status`` 用的一行摘要，不抛异常。"""
    try:
        token = load_saved(path)
    except Web139AuthError as exc:
        return f"不可用（{exc}）"
    if token is None:
        return "未导入"
    if token.is_expired():
        return describe(token) + "，需要重新导入"
    return describe(token)
