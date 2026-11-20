"""挂载点位置与多用户共享（``allow_other``）的纯逻辑。

两件事：

1. **挂载点能不能挂**：任意绝对路径（``/mnt/mcloud``、``/srv/cloud`` …）都允许，
   但目录要么已存在且当前用户可读写、要么能由当前用户创建；需要 root 时这里
   只生成一条可复制的 ``sudo install -d ...``，**不**自己提权。
2. **其他本地用户能不能看见**：FUSE 的 ``allow_other`` 需要
   ``/etc/fuse.conf`` 里的 ``user_allow_other``，并且从 ``/`` 到挂载点父目录
   的每一级都要有 ``o+x``（否则别的用户在路径解析处就 ``EACCES``）。

本模块不导入 pyfuse3 / curses，也不访问网络，因此在没装 FUSE 的机器与 CI 上
可以直接单测；真正的特权操作由调用方（TUI / 行模式）决定是否执行。
"""

from __future__ import annotations

import grp
import os
import pwd
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

FUSE_CONF_PATH = Path("/etc/fuse.conf")

#: allow_other 时始终配套的选项：内核按 attrs 里的 mode/uid/gid 做权限检查。
ALWAYS_WITH_ALLOW_OTHER = ("allow_other", "default_permissions")


# --------------------------------------------------------------------- 身份


def current_user() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, OSError):
        return str(os.getuid())


def current_group() -> str:
    try:
        return grp.getgrgid(os.getgid()).gr_name
    except (KeyError, OSError):
        return str(os.getgid())


# ----------------------------------------------------------------- fuse.conf


def fuse_conf_path() -> Path:
    """``/etc/fuse.conf``；测试可用 ``MCLOUD_FUSE_CONF`` 指向临时文件。"""
    override = os.getenv("MCLOUD_FUSE_CONF")
    return Path(override).expanduser() if override else FUSE_CONF_PATH


def user_allow_other_enabled(path: Optional[Path] = None) -> bool:
    """配置里是否真的有生效的 ``user_allow_other``（忽略注释与行尾空白）。"""
    target = Path(path) if path else fuse_conf_path()
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line == "user_allow_other":
            return True
    return False


def enable_user_allow_other_argv(path: Optional[Path] = None) -> List[str]:
    """追加 ``user_allow_other`` 的 argv（幂等：已存在时不重复追加）。"""
    target = path or fuse_conf_path()
    quoted = shlex.quote(str(target))
    script = f"grep -qs '^user_allow_other' {quoted} || echo user_allow_other >> {quoted}"
    return ["sudo", "sh", "-c", script]


def enable_user_allow_other_command(path: Optional[Path] = None) -> str:
    """同上的可复制命令行版本（路径没有特殊字符时用更好读的写法）。"""
    target = path or fuse_conf_path()
    text = str(target)
    if not any(char in text for char in " \t\"'\\$"):
        return (
            "sudo sh -c \"grep -qs '^user_allow_other' "
            f"{text} || echo user_allow_other >> {text}\""
        )
    return shlex.join(enable_user_allow_other_argv(target))


# ------------------------------------------------------------- 路径可穿越性


def traversal_gaps(mountpoint: Path) -> List[Path]:
    """返回从 ``/`` 到挂载点父目录中，其他用户无法进入（缺 ``o+x``）的目录。

    挂载点自身不参与判断：挂载后它的权限由 FUSE 根 inode（``0755``）决定，
    真正挡人的是它上面那几级目录（典型是 ``$HOME`` 为 ``0700``）。
    """
    target = Path(os.path.abspath(os.path.expanduser(str(mountpoint))))
    gaps: List[Path] = []
    for parent in reversed(target.parents):
        if parent == Path(os.path.sep):
            continue
        try:
            mode = parent.stat().st_mode
        except OSError:
            # 路径不存在/不可访问时交给 mountpoint 检查去报，这里跳过。
            continue
        if stat.S_ISDIR(mode) and not mode & stat.S_IXOTH:
            gaps.append(parent)
    return gaps


def traversal_fix_command(directory: Path) -> str:
    return f"sudo chmod o+x {shlex.quote(str(directory))}"


# -------------------------------------------------------------- allow_other


@dataclass(frozen=True)
class ShareProblem:
    """开启 allow_other 前发现的一个问题。"""

    name: str
    detail: str
    fix: str = ""
    fatal: bool = False


def share_problems(mountpoint: Path, *, conf: Optional[Path] = None) -> List[ShareProblem]:
    """列出「其他用户只读访问」还缺什么；``fatal`` 表示不解决就挂不上。

    - ``user_allow_other`` 缺失是 fatal：fusermount3 会直接拒绝 ``allow_other``；
    - 父目录缺 ``o+x`` 只是 warning：能挂上，但别人在路径解析处进不来。
    """
    problems: List[ShareProblem] = []
    if not user_allow_other_enabled(conf):
        target = conf or fuse_conf_path()
        problems.append(
            ShareProblem(
                "user_allow_other",
                f"{target} 里没有 user_allow_other，非 root 挂载会被拒绝",
                enable_user_allow_other_command(target),
                fatal=True,
            )
        )
    gaps = traversal_gaps(mountpoint)
    for directory in gaps[:3]:
        problems.append(
            ShareProblem(
                "路径权限",
                f"{directory} 没有 o+x，其他用户无法进入这个目录",
                traversal_fix_command(directory),
                fatal=False,
            )
        )
    return problems


def mount_options(base: Iterable[str], allow_other: bool) -> set:
    """在 pyfuse3 默认选项上按需加 ``allow_other`` / ``default_permissions``。"""
    options = set(base)
    if allow_other:
        options.update(ALWAYS_WITH_ALLOW_OTHER)
    return options


# ----------------------------------------------------------------- 挂载点

#: inspect_mountpoint 的动作
READY = "ready"  # 已存在、可读写、未挂载
CREATE = "create"  # 不存在，但父目录可写，调用方直接 mkdir
NEED_ROOT = "need_root"  # 需要 sudo 创建或改属主
BLOCKED = "blocked"  # 这个路径不能用


@dataclass(frozen=True)
class MountpointPlan:
    path: Path
    action: str
    detail: str
    fix: str = ""
    warnings: Tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.action in (READY, CREATE)


def sudo_prepare_argv(
    path: Path, *, user: Optional[str] = None, group: Optional[str] = None
) -> List[str]:
    """建目录并交给当前用户（``install -d`` 会自动补父目录）。"""
    return [
        "sudo",
        "install",
        "-d",
        "-m",
        "755",
        "-o",
        user or current_user(),
        "-g",
        group or current_group(),
        str(path),
    ]


def sudo_prepare_command(
    path: Path, *, user: Optional[str] = None, group: Optional[str] = None
) -> str:
    """同上的可复制命令行版本。"""
    return shlex.join(sudo_prepare_argv(path, user=user, group=group))


def _missing_ancestor(path: Path) -> Optional[Path]:
    """找到最近一个已存在的父目录（用于判断能不能自己 mkdir）。"""
    for parent in path.parents:
        if parent.exists():
            return parent
    return None


def inspect_mountpoint(value: str) -> MountpointPlan:
    """判断一个挂载点是否可用；只读，不创建目录、不执行 sudo。"""
    text = (value or "").strip()
    if not text:
        return MountpointPlan(Path("."), BLOCKED, "挂载点不能为空")
    expanded = os.path.expanduser(text)
    if not os.path.isabs(expanded):
        return MountpointPlan(
            Path(text), BLOCKED, "请填绝对路径，例如 ~/mnt/mcloud 或 /mnt/mcloud"
        )
    path = Path(os.path.abspath(expanded))
    if str(path) == os.path.sep:
        return MountpointPlan(path, BLOCKED, "不能把云盘挂载到 /")

    try:
        exists = path.exists()
    except OSError as exc:  # ENOTCONN：失效的 FUSE 挂载
        return MountpointPlan(
            path,
            BLOCKED,
            f"无法访问 {path}：{exc}（可能是失效的 FUSE 挂载）",
            f"fusermount3 -uz {shlex.quote(str(path))}",
        )

    if exists:
        if os.path.islink(path):
            return MountpointPlan(path, BLOCKED, "挂载点不能是符号链接")
        if not path.is_dir():
            return MountpointPlan(path, BLOCKED, f"{path} 已存在但不是目录")
        try:
            if os.path.ismount(path):
                return MountpointPlan(
                    path,
                    BLOCKED,
                    f"{path} 已经是一个挂载点；请先卸载或换一个目录",
                    f"fusermount3 -u {shlex.quote(str(path))}",
                )
        except OSError:
            pass
        if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
            return MountpointPlan(
                path,
                NEED_ROOT,
                f"{path} 存在，但当前用户没有读写权限",
                sudo_prepare_command(path),
            )
        warnings: Tuple[str, ...] = ()
        try:
            children = sum(1 for _ in os.scandir(path))
        except OSError:
            children = 0
        if children:
            warnings = (
                f"目录里已有 {children} 项内容，挂载后会被暂时遮住（不会删除）",
            )
        return MountpointPlan(path, READY, f"已就绪：{path}", "", warnings)

    ancestor = _missing_ancestor(path)
    if ancestor is None:
        return MountpointPlan(path, BLOCKED, f"找不到 {path} 的父目录")
    if os.access(ancestor, os.W_OK | os.X_OK):
        return MountpointPlan(path, CREATE, f"将创建目录：{path}")
    return MountpointPlan(
        path,
        NEED_ROOT,
        f"{path} 还不存在，{ancestor} 只允许 root 写入",
        sudo_prepare_command(path),
    )


def create_mountpoint(plan: MountpointPlan) -> MountpointPlan:
    """执行 ``CREATE`` 动作（普通 mkdir），再重新检查一遍。"""
    if plan.action != CREATE:
        return plan
    try:
        plan.path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return MountpointPlan(
            plan.path, BLOCKED, f"无法创建 {plan.path}：{exc}", sudo_prepare_command(plan.path)
        )
    return inspect_mountpoint(str(plan.path))


def describe_share(allow_other: bool) -> str:
    return "其他本地用户可读（allow_other）" if allow_other else "仅当前用户"
