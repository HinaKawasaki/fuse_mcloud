"""定位官方云盘 Agent Skill（``basement/scripts``）并注入 ``sys.path``。

官方 Skill 不在本仓库内（``basement/`` 已被 ``.gitignore`` 排除），且它是一组
顶层模块（``cm_cloud_client`` / ``cm_cloud_auth`` / ...），因此必须在导入它们
之前把所在目录加进 ``sys.path``。定位顺序：

1. 环境变量 ``MCLOUD_BASEMENT_DIR``（可指向 ``basement`` 或 ``basement/scripts``）；
2. 仓库布局 ``<repo>/basement/scripts``（源码运行与 editable 安装均适用）；
3. 当前工作目录下的 ``basement/scripts``。

集中在这里处理，避免 ``client/wrapper.py`` 与 ``client/auth_manager.py``
各自硬编码一份相对路径。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Optional

_SKILL_MARKER = "cm_cloud_client.py"

# <repo>/mcloud_fuse/basement.py -> parent=mcloud_fuse, parent.parent=<repo>
_REPO_BASEMENT = Path(__file__).resolve().parent.parent / "basement" / "scripts"
_CWD_BASEMENT = Path.cwd() / "basement" / "scripts"

_cached_dir: Optional[Path] = None


def _normalize(candidate: Path) -> Path:
    """允许 ``MCLOUD_BASEMENT_DIR`` 指向 ``basement`` 或 ``basement/scripts``。"""
    if (candidate / _SKILL_MARKER).is_file():
        return candidate
    nested = candidate / "scripts"
    if (nested / _SKILL_MARKER).is_file():
        return nested
    return candidate


def _candidates() -> List[Path]:
    candidates: List[Path] = []
    env = os.getenv("MCLOUD_BASEMENT_DIR", "").strip()
    if env:
        candidates.append(_normalize(Path(env).expanduser()))
    candidates.append(_REPO_BASEMENT)
    if _CWD_BASEMENT not in candidates:
        candidates.append(_CWD_BASEMENT)
    return candidates


def basement_scripts_dir() -> Path:
    """返回官方 Skill 的 ``scripts`` 目录；找不到时返回首选候选（便于报错展示）。"""
    candidates = _candidates()
    for candidate in candidates:
        if (candidate / _SKILL_MARKER).is_file():
            return candidate
    return candidates[0]


def ensure_basement_on_path() -> Path:
    """把官方 Skill 目录注入 ``sys.path`` 并返回该目录（结果进程内缓存）。

    找不到时抛 ``RuntimeError``，提示先运行 ``scripts/setup_basement.py``，
    而不是让 ``ImportError: No module named 'cm_cloud_client'`` 直接糊到用户脸上。
    """
    global _cached_dir
    if _cached_dir is None:
        directory = basement_scripts_dir()
        if not (directory / _SKILL_MARKER).is_file():
            tried = "\n".join(f"  - {path}" for path in _candidates())
            raise RuntimeError(
                "找不到官方云盘 Skill（缺少 cm_cloud_client.py）。已尝试：\n"
                f"{tried}\n"
                "请先运行 `python3 scripts/setup_basement.py` 拉取官方 Skill，"
                "或设置环境变量 MCLOUD_BASEMENT_DIR 指向 basement/scripts。"
            )
        _cached_dir = directory

    if str(_cached_dir) not in sys.path:
        sys.path.insert(0, str(_cached_dir))
    return _cached_dir


__all__ = ["basement_scripts_dir", "ensure_basement_on_path"]
