"""挂载点定位与多用户只读共享（allow_other）的纯逻辑测试。

不依赖 pyfuse3 / curses / root：需要特权的分支只校验「生成的 sudo 命令」，
需要不可写父目录的分支在 root 下跳过（root 的 access(2) 永远为真）。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from mcloud_fuse import mountopts


@pytest.fixture
def world_dir():
    """挂在 /tmp 下的可穿越目录（pytest 的 tmp_path 是 0700，不适合测这一步）。"""
    path = Path(tempfile.mkdtemp(prefix="mcloud-world-", dir="/tmp"))
    os.chmod(path, 0o755)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _write_conf(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "fuse.conf"
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------------ fuse.conf


def test_user_allow_other_parsing(tmp_path: Path):
    assert mountopts.user_allow_other_enabled(_write_conf(tmp_path, "user_allow_other\n"))
    assert mountopts.user_allow_other_enabled(
        _write_conf(tmp_path, "# comment\n\n  user_allow_other   # 行尾注释\n")
    )
    assert not mountopts.user_allow_other_enabled(
        _write_conf(tmp_path, "#user_allow_other\nmount_max = 1000\n")
    )
    assert not mountopts.user_allow_other_enabled(tmp_path / "missing.conf")


def test_fuse_conf_path_honors_env(tmp_path: Path, monkeypatch):
    target = tmp_path / "custom.conf"
    monkeypatch.setenv("MCLOUD_FUSE_CONF", str(target))
    assert mountopts.fuse_conf_path() == target


def test_enable_command_is_idempotent_and_contains_path(tmp_path: Path):
    target = tmp_path / "fuse.conf"
    argv = mountopts.enable_user_allow_other_argv(target)
    assert argv[:3] == ["sudo", "sh", "-c"]
    assert "grep -qs" in argv[3] and "echo user_allow_other >>" in argv[3]
    assert str(target) in argv[3]
    assert mountopts.enable_user_allow_other_command(target).startswith("sudo sh -c")


# --------------------------------------------------------------- 路径可穿越


def test_traversal_gaps_ignores_mountpoint_itself(tmp_path: Path):
    inner = tmp_path / "cloud"
    inner.mkdir()
    # tmp_path 通常是 0700：挂载点父目录不可穿越，应被报出来。
    gaps = mountopts.traversal_gaps(inner)
    assert tmp_path in gaps
    assert inner not in gaps


def test_traversal_gaps_empty_when_world_executable(world_dir: Path):
    mount = world_dir / "cloud"
    mount.mkdir()
    assert mountopts.traversal_gaps(mount) == []


def test_traversal_fix_command():
    assert mountopts.traversal_fix_command(Path("/home/alice")) == (
        "sudo chmod o+x /home/alice"
    )


# --------------------------------------------------------------- 共享前置条件


def test_share_problems_flags_missing_user_allow_other(tmp_path: Path):
    conf = _write_conf(tmp_path, "# 什么都没有\n")
    problems = mountopts.share_problems(tmp_path / "cloud", conf=conf)
    assert problems and problems[0].name == "user_allow_other"
    assert problems[0].fatal
    assert "sudo" in problems[0].fix


def test_share_problems_only_warns_for_traversal(tmp_path: Path):
    conf = _write_conf(tmp_path, "user_allow_other\n")
    mount = tmp_path / "cloud"
    mount.mkdir()
    problems = mountopts.share_problems(mount, conf=conf)
    assert problems, "0700 的父目录应当被提示"
    assert all(not problem.fatal for problem in problems)
    assert all("o+x" in problem.detail for problem in problems)


def test_share_problems_clean_when_everything_ready(tmp_path: Path, world_dir: Path):
    conf = _write_conf(tmp_path, "user_allow_other\n")
    mount = world_dir / "cloud"
    mount.mkdir()
    assert mountopts.share_problems(mount, conf=conf) == []


def test_mount_options_adds_default_permissions():
    options = mountopts.mount_options({"fsname=x"}, allow_other=True)
    assert {"allow_other", "default_permissions", "fsname=x"} <= options
    assert mountopts.mount_options({"fsname=x"}, allow_other=False) == {"fsname=x"}


# ------------------------------------------------------------------- 挂载点


def test_inspect_rejects_relative_and_root():
    assert mountopts.inspect_mountpoint("mnt/cloud").action == mountopts.BLOCKED
    assert mountopts.inspect_mountpoint("").action == mountopts.BLOCKED
    assert mountopts.inspect_mountpoint("/").action == mountopts.BLOCKED


def test_inspect_existing_file_is_blocked(tmp_path: Path):
    target = tmp_path / "file"
    target.write_text("x", encoding="utf-8")
    plan = mountopts.inspect_mountpoint(str(target))
    assert plan.action == mountopts.BLOCKED
    assert "不是目录" in plan.detail


def test_inspect_symlink_is_blocked(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    plan = mountopts.inspect_mountpoint(str(link))
    assert plan.action == mountopts.BLOCKED
    assert "符号链接" in plan.detail


def test_inspect_and_create_missing_path(tmp_path: Path):
    target = tmp_path / "a" / "b" / "cloud"
    plan = mountopts.inspect_mountpoint(str(target))
    assert plan.action == mountopts.CREATE
    ready = mountopts.create_mountpoint(plan)
    assert ready.action == mountopts.READY
    assert target.is_dir()


def test_inspect_warns_for_non_empty_dir(tmp_path: Path):
    target = tmp_path / "cloud"
    target.mkdir()
    (target / "old.txt").write_text("x", encoding="utf-8")
    plan = mountopts.inspect_mountpoint(str(target))
    assert plan.action == mountopts.READY
    assert plan.warnings and "已有 1 项" in plan.warnings[0]


@pytest.mark.skipif(os.geteuid() == 0, reason="root 能写任何目录，测不到 NEED_ROOT")
def test_inspect_needs_root_for_unwritable_parent(tmp_path: Path):
    parent = tmp_path / "system"
    parent.mkdir()
    os.chmod(parent, 0o555)
    try:
        plan = mountopts.inspect_mountpoint(str(parent / "cloud"))
        assert plan.action == mountopts.NEED_ROOT
        assert "sudo install -d" in plan.fix
        assert mountopts.current_user() in plan.fix
    finally:
        os.chmod(parent, 0o755)


def test_describe_share():
    assert "allow_other" in mountopts.describe_share(True)
    assert mountopts.describe_share(False) == "仅当前用户"
