"""版本号的唯一来源。

``pyproject.toml`` 通过 ``dynamic = ["version"]`` 读取这里的 ``__version__``，
``mcloud-fuse --version`` 也从这里取值，避免多处手工同步。
"""

__version__ = "0.3.0"

__all__ = ["__version__"]
