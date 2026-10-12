"""真实 wrapper 连通性测试（需要云盘登录态与网络）。

默认跳过；确需运行时设置环境变量 ``MCLOUD_LIVE_TESTS=1``。
"""
import os
import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from client.auth_manager import AuthManager  # noqa: E402
from client.wrapper import DirectClientWrapper  # noqa: E402


@pytest.mark.skipif(
    not os.getenv("MCLOUD_LIVE_TESTS"),
    reason="需要真实云盘登录态，设置 MCLOUD_LIVE_TESTS=1 后运行",
)
def test_wrapper_live():
    print("🚀 初始化 AuthManager 与 DirectClientWrapper...")
    auth_mgr = AuthManager()
    client_wrapper = DirectClientWrapper(auth_mgr)

    print("\n1. 检查并获取沙箱 Workspace ID...")
    workspace_id = client_wrapper.ensure_workspace()
    print(f"✅ 沙箱目录 File ID: {workspace_id}")

    print("\n2. 测试拉取沙箱目录下的文件列表...")
    items = client_wrapper.list_directory(workspace_id, page_limit=50)
    print(f"✅ 沙箱目录下共 {len(items)} 个直接子项:")
    for item in items:
        kind = "[目录]" if item.is_dir else "[文件]"
        print(f"   {kind} {item.name} | ID: {item.file_id} | 大小: {item.size} Bytes")


if __name__ == "__main__":
    test_wrapper_live()
