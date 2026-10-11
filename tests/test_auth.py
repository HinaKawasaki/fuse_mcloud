import sys
from pathlib import Path

# 1. 动态将项目根目录下的 src/ 目录添加到 sys.path 中
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from client.auth_manager import AuthManager, AuthRequiredException

auth_mgr = AuthManager()

try:
    # 静默获取 Token (若之前在 CLI 登录过，可以直接拿到)
    token = auth_mgr.get_valid_token(issue_login=True)
    print(f"成功获取有效 Token: {token[:10]}***")
except AuthRequiredException as e:
    print(f"需要登录，请在浏览器中打开此 URL 进行授权:\n{e.login_url}")
    # 授权完成后，再次以 issue_login=True 运行即可换取 creditToken 并自动落盘存入 auth_info.json