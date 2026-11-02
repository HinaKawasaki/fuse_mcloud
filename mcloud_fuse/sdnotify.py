"""最小 sd_notify 客户端：只做 READY / STATUS / EXTEND_TIMEOUT_USEC。

不引入 systemd-python 依赖：Unix datagram 协议很简单。
未在 systemd 下运行（没有 ``NOTIFY_SOCKET``）时全部 no-op，绝不阻塞挂载/卸载。
"""

from __future__ import annotations

import os
import socket
from typing import Optional


def _address() -> Optional[str]:
    raw = os.getenv("NOTIFY_SOCKET")
    if not raw:
        return None
    # Linux abstract namespace: "@name" -> "\0name"
    return "\0" + raw[1:] if raw.startswith("@") else raw


def notify(message: str) -> bool:
    """发送一条 sd_notify 消息；失败返回 False，不抛异常。"""
    address = _address()
    if not address:
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.connect(address)
        sock.send(message.encode("utf-8", "replace"))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def ready() -> bool:
    return notify("READY=1")


def status(text: str) -> bool:
    return notify("STATUS=" + str(text).replace("\n", " ")[:200])


def extend_timeout(seconds: float) -> bool:
    """请求 systemd 把当前 start/stop 超时延长 ``seconds`` 秒。

    注意：systemd 对每次 EXTEND 是追加的，调用方必须自己设总上限；
    这里只负责发送，不做策略判断。
    """
    usec = max(0, int(float(seconds) * 1_000_000))
    return notify(f"EXTEND_TIMEOUT_USEC={usec}")
