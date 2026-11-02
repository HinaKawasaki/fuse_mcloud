"""真实登录态检查（需要本地已有 auth_info.json）。

默认跳过；确需运行时设置环境变量 ``MCLOUD_LIVE_TESTS=1``。
"""
import os
import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parent.parent  # 仓库根（mcloud_fuse/ 所在目录）
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from mcloud_fuse.client.auth_manager import AuthManager, AuthRequiredException  # noqa: E402


@pytest.mark.skipif(
    not os.getenv("MCLOUD_LIVE_TESTS"),
    reason="需要真实云盘登录态，设置 MCLOUD_LIVE_TESTS=1 后运行",
)
def test_auth_token():
    auth_mgr = AuthManager()
    try:
        token = auth_mgr.get_valid_token(issue_login=True)
        print(f"成功获取有效 Token: {token[:10]}***")
    except AuthRequiredException as e:
        print(f"需要登录，请在浏览器中打开此 URL 进行授权:\n{e.login_url}")
        raise


if __name__ == "__main__":
    test_auth_token()
