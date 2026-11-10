"""token 生命周期状态：systemd STATUS 与后台自动续期。

做两件事，都不接触账号密码、浏览器 Cookie 或 token 明文：

1. 把「还剩多久过期」压成一行脱敏文本，通过 ``sd_notify`` 的 ``STATUS=``
   交给 systemd。这样 ``systemctl --user status mcloud-fuse`` 的 ``Status:``
   一行就能看到 token 还有多久到期，不必再单独跑 ``mcloud-fuse status``；
2. 服务运行期间每 12 小时检查一次 token，剩余寿命进入续期窗口
   （``Web139Token.needs_refresh``，默认 15 天）时调用已有的
   ``tellin/authTokenRefresh.do`` 续期并原子写回 0600 文件。

续期失败只记日志并继续报告剩余天数，绝不影响挂载。想关掉空闲时的主动续期，
设 ``MCLOUD_TOKEN_AUTO_REFRESH=0``；按请求触发的续期仍由客户端负责。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Optional

from mcloud_fuse import sdnotify, tokens
from mcloud_fuse.client.web139 import Web139Token

logger = logging.getLogger(__name__)

# 默认 12 小时：token 寿命通常是 30 天，这个频率足够在 15 天续期窗口内发现。
DEFAULT_INTERVAL_SEC = 12 * 60 * 60.0
# 防止误设成 1 秒把接口打爆；真要调试可以显式传 interval。
MIN_INTERVAL_SEC = 60.0
# 剩余寿命低于该值时，状态里直接提示尽快处理（续期失败也至少能看见）。
WARN_DAYS = 7.0


def interval_seconds() -> float:
    """状态刷新间隔：``MCLOUD_TOKEN_STATUS_INTERVAL_SEC``，0 表示只报一次。"""
    raw = os.getenv("MCLOUD_TOKEN_STATUS_INTERVAL_SEC")
    if raw is None or not raw.strip():
        return DEFAULT_INTERVAL_SEC
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "MCLOUD_TOKEN_STATUS_INTERVAL_SEC 不是数字：%r，改用默认 %.0f 秒",
            raw,
            DEFAULT_INTERVAL_SEC,
        )
        return DEFAULT_INTERVAL_SEC
    if value <= 0:
        return 0.0
    return max(MIN_INTERVAL_SEC, value)


def auto_refresh_enabled() -> bool:
    """是否允许后台在空闲时主动续期（默认开启）。"""
    raw = os.getenv("MCLOUD_TOKEN_AUTO_REFRESH", "1")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def cadence_text() -> str:
    """给 ``status`` / 向导摘要用的一句话，描述后台会做什么。"""
    interval = interval_seconds()
    if interval <= 0:
        return "只在启动时报告一次，不做后台续期"
    hours = interval / 3600
    if not auto_refresh_enabled():
        return f"挂载运行时每 {hours:g} 小时报告一次，只报告不续期"
    return f"挂载运行时每 {hours:g} 小时检查一次，剩余不足 15 天自动续期"


def summarize(token: Optional[Web139Token], now_ms: Optional[int] = None) -> str:
    """一行脱敏摘要：账号、到期时间、剩余天数；不包含 token 明文。"""
    if token is None:
        return "token 未导入"

    account = tokens.mask_account(token.account)
    expiry = token.expires_at_ms
    if expiry is None:
        return f"token {account}（未携带过期时间）"

    now = int(time.time() * 1000) if now_ms is None else now_ms
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(expiry / 1000))
    remaining = expiry - now
    if remaining <= 0:
        return f"token {account} 已过期（{when}）"

    days = remaining / 86400000
    if days < WARN_DAYS:
        return f"token {account} 剩余 {days:.1f} 天（{when} 到期，请尽快续期或重新导入）"
    return f"token {account} 剩余 {days:.1f} 天（{when} 到期）"


def status_text(
    mountpoint: str,
    token: Optional[Web139Token],
    now_ms: Optional[int] = None,
) -> str:
    """``sd_notify STATUS=`` 的完整文本，也便于测试直接断言。"""
    return f"mounted on {mountpoint} | {summarize(token, now_ms)}"


class TokenStatusReporter:
    """定期把 token 状态写给 systemd，并在需要时续期。

    ``token`` 是取当前 token 的回调（挂载进程里用 ``wrapper.auth.token``，
    这样能看到刚刚续期后的内存值）；``refresh`` 是续期回调，失败应抛异常，
    reporter 会记录后继续。两个回调都在后台线程里调用，不能阻塞卸载流程。
    """

    def __init__(
        self,
        mountpoint: str,
        *,
        token: Optional[Callable[[], Optional[Web139Token]]] = None,
        refresh: Optional[Callable[[], object]] = None,
        interval: Optional[float] = None,
        now: Optional[Callable[[], float]] = None,
    ) -> None:
        self.mountpoint = str(mountpoint)
        self._token = token or (lambda: tokens.load_saved())
        self._refresh = refresh
        self._interval = interval_seconds() if interval is None else max(0.0, float(interval))
        self._now = now or time.time
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---------------------------------------------------------------- 状态

    def status(self) -> str:
        try:
            token = self._token()
        except Exception as exc:  # noqa: BLE001 - 状态上报不能反过来打断挂载
            logger.warning("读取 token 状态失败：%s", exc)
            return f"mounted on {self.mountpoint} | token 不可用（{exc}）"
        return status_text(self.mountpoint, token, int(self._now() * 1000))

    def report(self, *, refresh: bool = True) -> str:
        """续期（可选）→ 生成状态 → 发给 systemd；返回状态文本。"""
        if refresh and self._refresh is not None:
            try:
                self._refresh()
            except Exception as exc:  # noqa: BLE001 - 续期失败不影响挂载
                logger.warning("token 后台续期失败（保留原 token）：%s", exc)
        text = self.status()
        sdnotify.status(text)
        logger.debug("token 状态：%s", text)
        return text

    # ---------------------------------------------------------------- 线程

    def start(self) -> None:
        """先同步报一次（不阻塞挂载），再启动后台线程。"""
        self.report(refresh=False)
        if self._interval <= 0:
            return
        self._thread = threading.Thread(
            target=self._loop,
            name="mcloud-token-status",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            # 续期请求最长 15 秒；不为它拖慢卸载，线程是 daemon。
            thread.join(timeout=2)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.report()
            except Exception as exc:  # noqa: BLE001
                logger.debug("token 状态上报失败：%s", exc)
            if self._stop.wait(self._next_delay()):
                return

    def _next_delay(self) -> float:
        """常规 12 小时；临近过期时缩短，避免只报一次就错过窗口。"""
        delay = self._interval
        try:
            token = self._token()
        except Exception:  # noqa: BLE001
            return delay
        expiry = token.expires_at_ms if token is not None else None
        if not expiry:
            return delay
        remaining = (expiry - int(self._now() * 1000)) / 1000
        if remaining <= 0:
            return min(delay, 3600.0)
        return min(delay, max(MIN_INTERVAL_SEC, remaining / 2))
