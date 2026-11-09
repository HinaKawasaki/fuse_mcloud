"""用户级配置：``~/.config/mcloud-fuse/config.json``。

设计目标只有一个：**装完之后不用再记命令行参数**。

- ``mcloud-fuse setup``（首次运行自动触发）把向导结果写进这个文件；
- 之后 ``mcloud-fuse`` / ``mcloud-fuse mount`` / ``status`` / ``unmount``
  都从这里读挂载点、缓存目录、云盘根、删除语义与冲突策略；
- 环境变量仍然优先（``MCLOUD_CACHE_DIR`` 等），方便临时覆盖与 CI。

配置在包导入时通过 :func:`apply_saved_env` 注入 ``os.environ``（仅 setdefault），
因此 ``mcloud_fuse.config`` 里的模块级常量无需改成延迟读取。显式环境变量永远
不会被配置文件覆盖。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

CONFIG_VERSION = 1
_RM_MODES = ("delete", "trash")
_CONFLICT_MODES = ("copy", "error")


class ConfigError(Exception):
    """配置文件存在但无法解析 / 字段非法。"""


def config_dir() -> Path:
    """配置目录（遵循 ``XDG_CONFIG_HOME`` 与 ``MCLOUD_CONFIG_DIR``）。"""
    override = os.getenv("MCLOUD_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.getenv("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "mcloud-fuse"


def config_path() -> Path:
    override = os.getenv("MCLOUD_CONFIG_FILE")
    if override:
        return Path(override).expanduser()
    return config_dir() / "config.json"


def default_token_path() -> Path:
    override = os.getenv("MCLOUD_WEB_TOKEN_FILE")
    if override:
        return Path(override).expanduser()
    return config_dir() / "web_token"


def default_mountpoint() -> Path:
    override = os.getenv("MCLOUD_MOUNTPOINT")
    if override:
        return Path(override).expanduser()
    return Path.home() / "mnt" / "mcloud"


def default_cache_dir() -> Path:
    override = os.getenv("MCLOUD_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    return Path("/tmp/cmcloud-fuse")


def _absolute(value: str) -> str:
    """展开 ``~`` 并转成绝对路径；不做 ``resolve()``，避免跟随不存在的符号链接。"""
    expanded = os.path.expanduser(str(value).strip())
    return os.path.abspath(expanded)


@dataclass(frozen=True)
class UserConfig:
    """一次安装的固定参数。"""

    mountpoint: str = ""
    cache_dir: str = ""
    root: str = "/"
    rm_mode: str = "delete"
    conflict: str = "copy"
    install_service: bool = True
    version: int = CONFIG_VERSION

    def __post_init__(self) -> None:
        # 允许构造时留空，用默认值补齐（dataclass 不方便直接引用 home）
        if not self.mountpoint:
            object.__setattr__(self, "mountpoint", str(default_mountpoint()))
        if not self.cache_dir:
            object.__setattr__(self, "cache_dir", str(default_cache_dir()))
        object.__setattr__(self, "mountpoint", _absolute(self.mountpoint))
        object.__setattr__(self, "cache_dir", _absolute(self.cache_dir))
        object.__setattr__(self, "root", (str(self.root).strip() or "/"))
        object.__setattr__(self, "rm_mode", str(self.rm_mode).strip().lower())
        object.__setattr__(self, "conflict", str(self.conflict).strip().lower())
        if self.rm_mode not in _RM_MODES:
            raise ConfigError(f"rm_mode 必须是 {_RM_MODES} 之一，收到 {self.rm_mode!r}")
        if self.conflict not in _CONFLICT_MODES:
            raise ConfigError(f"conflict 必须是 {_CONFLICT_MODES} 之一，收到 {self.conflict!r}")
        if self.mountpoint == os.path.sep:
            raise ConfigError("挂载点不能是 /")

    # ---------------------------------------------------------------- 读写

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "UserConfig":
        if not isinstance(data, Mapping):
            raise ConfigError("配置根节点必须是对象")
        known = {field for field in cls.__dataclass_fields__}
        # 忽略未知字段：旧版本读到新版本写的配置时不至于直接拒绝启动。
        values: Dict[str, Any] = {key: data[key] for key in known & set(data)}
        if "install_service" in values and not isinstance(values["install_service"], bool):
            raise ConfigError("install_service 必须是布尔值")
        if "version" in values and not isinstance(values["version"], int):
            raise ConfigError("version 必须是整数")
        try:
            return cls(**values)
        except TypeError as exc:
            raise ConfigError(f"配置字段类型错误：{exc}") from exc

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def env(self) -> Dict[str, str]:
        """可注入子进程的环境变量（不会覆盖已有值）。"""
        return {
            "MCLOUD_MOUNTPOINT": self.mountpoint,
            "MCLOUD_ROOT": self.root,
            "MCLOUD_CACHE_DIR": self.cache_dir,
            "MCLOUD_RM_MODE": self.rm_mode,
            "MCLOUD_CONFLICT": self.conflict,
            "MCLOUD_WEB_TOKEN_FILE": str(default_token_path()),
        }

    def apply_env(self, environ: Optional[Dict[str, str]] = None) -> None:
        target = os.environ if environ is None else environ
        for key, value in self.env().items():
            target.setdefault(key, value)

    def with_changes(self, **changes: Any) -> "UserConfig":
        return replace(self, **changes)


def load(path: Optional[Path] = None) -> Optional[UserConfig]:
    """读取配置；文件不存在返回 ``None``，内容非法抛 :class:`ConfigError`。"""
    target = Path(path).expanduser() if path else config_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigError(f"无法读取配置 {target}：{exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON：{target}（{exc}）") from exc
    config = UserConfig.from_mapping(data)
    return config


def save(config: UserConfig, path: Optional[Path] = None) -> Path:
    """原子写入 0600 的 JSON 配置。"""
    target = Path(path).expanduser() if path else config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(config.to_dict(), ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    return target


def apply_saved_env() -> Optional[UserConfig]:
    """把已保存配置注入环境变量；任何错误都静默忽略，绝不阻止包导入。

    这是 ``mcloud_fuse/__init__.py`` 里的唯一副作用：``config.py`` 在导入时
    读取环境变量，因此必须赶在它之前执行。
    """
    try:
        saved = load()
    except ConfigError:
        return None
    if saved is not None:
        saved.apply_env()
    return saved
