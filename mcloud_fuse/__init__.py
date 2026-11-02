"""mcloud-fuse：把中国移动云盘挂载为本地目录。

包导入时会把 ``~/.config/mcloud-fuse/config.json`` 里的配置注入环境变量
（``setdefault``，不覆盖显式环境变量）。``mcloud_fuse.config`` 依赖这些环境
变量，因此这一步必须发生在导入 ``config`` 之前。
"""

from mcloud_fuse.userconfig import apply_saved_env as _apply_saved_env

_apply_saved_env()
del _apply_saved_env

__all__ = []
