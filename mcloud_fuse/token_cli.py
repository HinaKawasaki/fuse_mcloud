"""``mcloud-fuse token``：查看 token 剩余时间，或手动触发一次续期。

续期只调用客户端已有的 ``tellin/authTokenRefresh.do``：不接触账号密码，
不读取浏览器 Cookie。续期被拒绝（接口可能要求设备指纹）时，仍然需要回到
浏览器复制新的 ``Authorization``，本命令会给出这一步提示。

    mcloud-fuse token            # 等价于 status
    mcloud-fuse token status
    mcloud-fuse token refresh    # 立即续期并原子写回 0600 文件
"""

from __future__ import annotations

import sys
from typing import List, Optional, Sequence

from mcloud_fuse import tokens
from mcloud_fuse.client.web139 import (
    Web139Auth,
    Web139AuthError,
    Web139Client,
    Web139Error,
)

USAGE = "用法：mcloud-fuse token [status|refresh]"


def refresh_saved() -> tokens.SaveResult:
    """读取已保存 token，强制续期，再按「保留更晚到期」的规则写回。

    构造 ``Web139Auth`` 时故意不传 ``path``：续期结果统一走
    :func:`mcloud_fuse.tokens.save_basic`，复用 0600 原子写入与旧 token 保护。
    """
    token = tokens.load_saved()
    if token is None:
        raise Web139AuthError("未导入 token，先运行 mcloud-fuse setup")
    auth = Web139Auth(token, path=None)
    client = Web139Client(auth)
    refreshed = client.refresh_token(force=True)
    return tokens.save_basic(refreshed.to_basic(), force=False)


def _status() -> int:
    print(tokens.token_summary())
    return 0


def _refresh() -> int:
    try:
        result = refresh_saved()
    except (Web139AuthError, Web139Error, OSError) as exc:
        print(f"❌ 自动续期失败：{exc}", file=sys.stderr)
        print(
            "   续期接口可能要求设备指纹；请在浏览器刷新云盘后运行 "
            "`mcloud-fuse setup` 重新粘贴 Authorization。",
            file=sys.stderr,
        )
        return 1
    marker = "✅" if result.wrote else "ℹ️ "
    print(f"{marker} {result.message}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args: List[str] = list(argv) if argv is not None else sys.argv[1:]
    command = args[0] if args else "status"
    if command in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if command in ("status", "show", "check"):
        return _status()
    if command in ("refresh", "renew", "renew-token"):
        return _refresh()
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
