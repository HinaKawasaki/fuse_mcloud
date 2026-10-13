"""挂载入口的离线回归测试（不需要真实挂载与登录态）。

覆盖：
1. 挂载点自愈——对应线上事故：挂载进程被 systemd 强杀后，会在挂载点留下
   `ENOTCONN` 的失效 FUSE 挂载；此时 `Path.mkdir(exist_ok=True)` 先收到
   `FileExistsError`、再去 `is_dir()` 又撞上 `ENOTCONN`，最终把
   `FileExistsError` 抛出来，使服务陷入 `Restart=on-failure` 的无限重启循环。
2. 打包元数据——`mcloud-fuse` 命令入口可导入、版本号只有 `version.py` 一个来源。
"""

import errno
import importlib
import types
from pathlib import Path

import pytest

from mcloud_fuse import main as mcloud_main
from mcloud_fuse import version

ROOT = Path(__file__).resolve().parent.parent


def test_prepare_mountpoint_creates_missing_dir(tmp_path):
    target = tmp_path / "a" / "b"
    mcloud_main.prepare_mountpoint(target)
    assert target.is_dir()


def test_prepare_mountpoint_recovers_from_stale_mount(tmp_path, monkeypatch):
    """首次 mkdir 撞 FileExistsError（失效挂载）时，应调用 fusermount3 后重试成功。"""
    target = tmp_path / "mnt"
    target.mkdir()

    calls = []
    state = {"raised": False}
    real_mkdir = Path.mkdir

    def flaky_mkdir(self, *args, **kwargs):
        if self == target and not state["raised"]:
            state["raised"] = True
            raise FileExistsError(errno.EEXIST, "File exists", str(self))
        return real_mkdir(self, *args, **kwargs)

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(Path, "mkdir", flaky_mkdir)
    monkeypatch.setattr(mcloud_main.subprocess, "run", fake_run)
    monkeypatch.setattr(mcloud_main.os.path, "ismount", lambda path: False)

    mcloud_main.prepare_mountpoint(target)

    assert state["raised"] is True, "应触发过一次失效挂载分支"
    assert calls and calls[0][:2] == ["fusermount3", "-u"], "应先尝试 fusermount3 -u"


def test_prepare_mountpoint_falls_back_to_lazy_unmount(tmp_path, monkeypatch):
    """`fusermount3 -u` 失败时应回退到 `-uz`（懒卸载）。"""
    target = tmp_path / "mnt"
    target.mkdir()

    calls = []
    real_mkdir = Path.mkdir

    def still_stale_mkdir(self, *args, **kwargs):
        # 模拟挂载点始终不可用：即便卸载失败后重试，mkdir 仍撞 FileExistsError
        if self == target:
            raise FileExistsError(errno.EEXIST, "File exists", str(self))
        return real_mkdir(self, *args, **kwargs)

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return types.SimpleNamespace(returncode=1, stdout="", stderr="busy")

    monkeypatch.setattr(Path, "mkdir", still_stale_mkdir)
    monkeypatch.setattr(mcloud_main.subprocess, "run", fake_run)
    monkeypatch.setattr(mcloud_main.os.path, "ismount", lambda path: False)

    with pytest.raises(SystemExit):
        # 两种卸载都失败、mkdir 仍不可用时，应给出明确报错而非原始 FileExistsError
        mcloud_main.prepare_mountpoint(target)

    assert [call[1] for call in calls] == ["-u", "-uz"]


def test_prepare_mountpoint_rejects_live_mount(tmp_path, monkeypatch):
    target = tmp_path / "mnt"
    target.mkdir()
    monkeypatch.setattr(mcloud_main.os.path, "ismount", lambda path: True)
    with pytest.raises(SystemExit):
        mcloud_main.prepare_mountpoint(target)


def test_version_flag_prints_version_and_exits(capsys):
    with pytest.raises(SystemExit) as excinfo:
        mcloud_main.parse_args(["--version"])

    assert excinfo.value.code == 0
    assert version.__version__ in capsys.readouterr().out


def test_console_script_entrypoint_is_importable():
    """pyproject 里声明的 `mcloud-fuse` 入口必须真实可导入、可调用。"""
    tomllib = pytest.importorskip("tomllib", reason="需要 Python 3.11+ 读取 pyproject")
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    target = pyproject["project"]["scripts"]["mcloud-fuse"]
    module_name, _, attr = target.partition(":")
    assert module_name and attr, f"入口格式应为 <module>:<callable>，实际为 {target!r}"
    assert callable(getattr(importlib.import_module(module_name), attr))


def test_version_has_single_source():
    """pyproject 不得写死版本号，必须从 mcloud_fuse.version 动态读取。"""
    tomllib = pytest.importorskip("tomllib", reason="需要 Python 3.11+ 读取 pyproject")
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    project = pyproject["project"]
    assert "version" not in project, "版本号应只存在于 mcloud_fuse/version.py"
    assert project["dynamic"] == ["version"]

    attr_path = pyproject["tool"]["setuptools"]["dynamic"]["version"]["attr"]
    module_name, _, attr = attr_path.rpartition(".")
    assert getattr(importlib.import_module(module_name), attr) == version.__version__
