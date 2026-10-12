import logging
import sys
from pathlib import Path
from typing import Dict, Optional

# 1. 确保将 basement/scripts 注入 sys.path (层级为 parent.parent.parent)
BASEMENT_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "basement" / "scripts"
if str(BASEMENT_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(BASEMENT_SCRIPTS_DIR))

# 2. 导入官方 SDK
import cm_cloud_auth  # noqa: E402
import cm_cloud_config  # noqa: E402
from cm_cloud_errors import AuthError, NeedAuthError  # noqa: E402

logger = logging.getLogger(__name__)


class AuthManager:
    """管理中国移动云盘凭据、Token 生命周期与登录状态"""

    def __init__(self):
        # 确保官方 SDK 配置初始化
        cm_cloud_config.ensure_api_config()

    def get_valid_token(self, issue_login: bool = False) -> str:
        """
        获取当前有效的 creditToken。

        :param issue_login: 是否允许发起新的浏览器授权页面生成请求 (一般仅在 login 命令或首次初始化时设为 True)
        :return: 有效的 creditToken 字符串
        """
        try:
            # 直接调用官方 cm_cloud_auth 中的逻辑，优先读取本地 auth_info.json，
            # 过期时会自动使用 accessToken 进行静默 preAuth 换票
            token = cm_cloud_auth.ensure_credit_token(issue_login=issue_login)
            return token
        except NeedAuthError as e:
            if e.login_url:
                raise AuthRequiredException(
                message=str(e),
                login_url=getattr(e, 'login_url', None)
            ) from e
        except AuthError as e:
            logger.error(f"鉴权过程出错: {e}")
            raise MCloudAuthException(str(e)) from e

    def invalidate_token(self) -> None:
        """在捕获到 401 报错或 Token 被服务端作废时主动清空内存与磁盘缓存"""
        cm_cloud_auth.invalidate_cached_token()
        logger.info("已作废本地缓存的 creditToken")

    def get_auth_headers(self) -> Dict[str, str]:
        """获取云盘 API 请求所需的请求头"""
        return cm_cloud_auth.get_request_headers()


class MCloudAuthException(Exception):
    """通用鉴权异常"""
    pass


class AuthRequiredException(MCloudAuthException):
    """需要用户在浏览器中授权登录的异常"""

    def __init__(self, message: str, login_url: Optional[str] = None):
        super().__init__(message)
        self.login_url = login_url
