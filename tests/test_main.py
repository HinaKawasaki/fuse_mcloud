"""挂载点自愈与优雅退出的离线回归测试（不需要真实挂载与登录态）。

对应线上事故：挂载进程被 systemd 强杀后，会在挂载点留下 `ENOTCONN` 的失效
FUSE 挂载；此时 `Path.mkdir(exist_ok=True)` 先收到 `FileExistsError`、再去
`is_dir()` 又撞上 `ENOTCONN`，最终把 `FileExistsError` 抛出来，使服务陷入
`Restart=on-failure` 的无限重启循环。
"""
import errno
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def load_main_module():
    """按路径加载 src/main.py（避免与 basement/scripts/main.py 同名冲突）。"""
    spec = importlib.util.spec_from_file_location("mcloud_src_main", SRC_DIR / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prepare_mountpoint_creates_missing_dir(tmp_path):
    module = load_main_module()
    target = tmp_path / "a" / "b"
    module.prepare_mountpoint(target)
    assert target.is_dir()


def test_prepare_mountpoint_recovers_from_stale_mount(tmp_path, monkeypatch):
    """首次 mkdir 撞 FileExistsError（失效挂载）时，应调用 fusermount3 后重试成功。"""
    module = load_main_module()
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
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module.os.path, "ismount", lambda path: False)

    module.prepare_mountpoint(target)

    assert state["raised"] is True, "应触发过一次失效挂载分支"
    assert calls and calls[0][:2] == ["fusermount3", "-u"], "应先尝试 fusermount3 -u"


def test_prepare_mountpoint_falls_back_to_lazy_unmount(tmp_path, monkeypatch):
    """`fusermount3 -u` 失败时应回退到 `-uz`（懒卸载）。"""
    module = load_main_module()
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
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module.os.path, "ismount", lambda path: False)

    with pytest.raises(SystemExit):
        # 两种卸载都失败、mkdir 仍不可用时，应给出明确报错而非原始 FileExistsError
        module.prepare_mountpoint(target)

    assert [call[1] for call in calls] == ["-u", "-uz"]


def test_prepare_mountpoint_rejects_live_mount(tmp_path, monkeypatch):
    module = load_main_module()
    target = tmp_path / "mnt"
    target.mkdir()
    monkeypatch.setattr(module.os.path, "ismount", lambda path: True)
    with pytest.raises(SystemExit):
        module.prepare_mountpoint(target)
