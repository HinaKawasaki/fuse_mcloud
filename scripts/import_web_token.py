#!/usr/bin/env python3
"""从 Chrome 的「Copy as cURL」或 Authorization 头导入 Web token。

这是 ``mcloud-fuse setup`` 之外的命令行入口，适合脚本 / 远程粘贴::

    python3 scripts/import_web_token.py            # 粘贴后 Ctrl-D 结束
    python3 scripts/import_web_token.py --check    # 只检查已保存的 token
    xclip -o | python3 scripts/import_web_token.py # 直接从剪贴板读

真正的解析、校验与 0600 落盘都在 :mod:`mcloud_fuse.tokens`，与首次设置向导
共用一套逻辑。脚本只负责读 stdin 与打印结果，不会打印 token 明文。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mcloud_fuse import tokens  # noqa: E402
from mcloud_fuse.client.web139 import Web139AuthError  # noqa: E402


def extract_token(raw: str) -> str:
    """兼容旧导入名：从 cURL / Authorization 行提取 Basic 值。"""
    return tokens.extract_basic(raw)


def main() -> int:
    parser = argparse.ArgumentParser(description="导入 yun.139.com Web token")
    parser.add_argument("--path", type=Path, default=tokens.default_token_path())
    parser.add_argument("--check", action="store_true", help="只检查当前文件，不写")
    parser.add_argument("--force", action="store_true", help="覆盖已存在且仍有效的 token")
    args = parser.parse_args()

    if args.check:
        try:
            token = tokens.load_saved(args.path)
        except Web139AuthError as exc:
            print(f"❌ {exc}")
            return 1
        if token is None:
            print(f"❌ 未找到 token：{args.path}")
            print("   运行 `mcloud-fuse setup`，或直接运行本脚本粘贴。")
            return 1
        print(f"✅ {args.path}\n   {tokens.describe(token)}")
        return 0

    raw = sys.stdin.read()
    try:
        basic = tokens.extract_basic(raw)
        result = tokens.save_basic(basic, path=args.path, force=args.force)
    except (ValueError, Web139AuthError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        print("   请确认复制的是 hcy/file/list 请求头里的 Authorization。", file=sys.stderr)
        return 1

    if result.wrote:
        print("✅ " + result.message)
    else:
        print("ℹ️  " + result.message)
    print("\n下一步：mcloud-fuse doctor   或   mcloud-fuse")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
