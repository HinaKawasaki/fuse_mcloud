"""与后端无关的异常类型，避免 FUSE/重试层依赖某个具体后端。"""


class MCloudAPIException(Exception):
    """云盘 API 统一异常。"""


class MCloudAuthException(MCloudAPIException):
    """登录态缺失、过期或续期失败。"""


class MCloudConflictError(MCloudAPIException):
    """覆盖写冲突：远端在本地打开后已被修改。"""
