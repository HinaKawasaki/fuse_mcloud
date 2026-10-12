"""沙箱外文件懒搬运（`--root cloud`）的离线回归测试。

背景：官方 Skill 只允许下载沙箱内文件，但 `copy` 的源可以是全盘任意位置。
因此挂载云盘根时，读取沙箱外文件要先把云端对象复制进沙箱里的暂存目录，
再从副本下载（见 `client/wrapper.py` 的 hydrate_file）。

设计约束（线上事故后加固）：**懒搬运全程不得使用搜索接口**
（`list_directory`）。搜索接口是最终一致的，并会返回「缓存信息失效」这类
服务端错误，所以这里用 StubWrapper.list_directory 直接把它变成断言。
"""
import sys
import types
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import cm_cloud_client as client  # noqa: E402
import cm_cloud_sandbox as sandbox  # noqa: E402
import cm_cloud_transport as transport  # noqa: E402
from cm_cloud_errors import ApiError, PermissionDeniedError  # noqa: E402

import client.wrapper as wrapper_module  # noqa: E402
from client.wrapper import DirectClientWrapper, MCloudAPIException  # noqa: E402
from config import HYDRATE_DIR_NAME, TRASH_BIN_DIR_NAME  # noqa: E402
from mapping.id_map import InodeMapper  # noqa: E402
from mapping.tree import DirectoryTree  # noqa: E402
from utils.trash_bin import TrashBinHandler  # noqa: E402

ROOT = "ROOT"
AI = "AI"
SANDBOX = "SBX"
DOC = "DOC"
OUTSIDE_FILE = "OUT1"
INSIDE_FILE = "IN1"


class StubCloud:
    """最小内存云盘：目录树 + 内容 + 「沙箱内才能取直链」的规则。"""

    def __init__(self):
        self.entries = {}
        self.content = {}
        self.url_to_id = {}
        self.copies = []
        # fileId -> 取直链时要抛出的异常（模拟非沙箱原因的失败）
        self.download_errors = {}
        self._seq = 0

        self.add_dir(AI, ROOT, "AI空间")
        self.add_dir(SANDBOX, AI, "智能体云盘助手")
        self.add_dir(DOC, ROOT, "我的文档")
        self.add_file(OUTSIDE_FILE, DOC, "report.pdf", b"outside-bytes")
        self.add_file(INSIDE_FILE, SANDBOX, "inside.txt", b"inside-bytes")

    # ---------------- 建模 ----------------
    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{self._seq}"

    def add_dir(self, file_id: str, parent: str, name: str) -> str:
        self.entries[file_id] = {"name": name, "is_dir": True, "parent": parent, "size": 0}
        return file_id

    def add_file(self, file_id: str, parent: str, name: str, payload: bytes) -> str:
        self.entries[file_id] = {
            "name": name, "is_dir": False, "parent": parent, "size": len(payload),
        }
        self.content[file_id] = payload
        return file_id

    def path(self, file_id: str) -> str:
        file_id = str(file_id)
        if file_id == ROOT:
            return "/"
        if file_id not in self.entries:
            return ""
        parts = []
        current = file_id
        while current and current != ROOT and current in self.entries:
            parts.append(self.entries[current]["name"])
            current = self.entries[current]["parent"]
        return "/" + "/".join(reversed(parts))

    def in_sandbox(self, file_id: str) -> bool:
        root = self.path(SANDBOX)
        path = self.path(file_id)
        return bool(path) and (path == root or path.startswith(root + "/"))

    # ---------------- 非搜索接口 ----------------
    def check_exists(self, parent_file_id: str, name: str):
        for file_id, entry in self.entries.items():
            if entry["parent"] == parent_file_id and entry["name"] == name:
                return {"exist": True, "fileId": file_id, "fileType": ""}
        return {"exist": False, "fileId": "", "fileType": ""}

    def create_folder(self, parent_file_id: str, name: str) -> str:
        return self.add_dir(self._next("DIR"), parent_file_id, name)

    def names(self, file_ids):
        return {fid: self.entries[fid]["name"] for fid in file_ids if fid in self.entries}

    def download_urls(self, file_ids):
        for file_id in file_ids:
            error = self.download_errors.get(str(file_id))
            if error is not None:
                raise error
            if not self.in_sandbox(file_id):
                raise PermissionDeniedError(
                    f"权限拒绝：待下载文件 {file_id} 不在沙箱目录内"
                )
        urls = {}
        for file_id in file_ids:
            url = f"https://example.invalid/{file_id}"
            self.url_to_id[url] = str(file_id)
            urls[str(file_id)] = url
        return types.SimpleNamespace(urls=urls, failures={})

    def batch_copy(self, file_ids, to_parent_file_id: str):
        results = []
        for file_id in file_ids:
            entry = self.entries[file_id]
            new_id = self.add_file(
                self._next("COPY"), to_parent_file_id, entry["name"], self.content[file_id]
            )
            self.copies.append((file_id, to_parent_file_id))
            results.append({"fileId": file_id, "newFileId": new_id})
        return results


class StubWrapper(DirectClientWrapper):
    """把 wrapper 的底层调用接到 StubCloud 上，保留懒搬运逻辑本体。"""

    def __init__(self, cloud: StubCloud):
        super().__init__(auth_manager=None)
        self.cloud = cloud

    def _ensure_auth(self):
        pass

    def ensure_workspace(self):
        return SANDBOX

    def list_directory(self, *args, **kwargs):
        raise AssertionError("懒搬运路径不应调用搜索接口 list_directory()")

    def _api(self, func, *args, **kwargs):
        if func is client.api_batch_download_urls:
            return self.cloud.download_urls(args[0])
        if func is client.api_check_exists:
            return self.cloud.check_exists(args[0], args[1])
        if func is client.api_create_folder:
            return self.cloud.create_folder(args[0], args[1])
        if func is client.api_batch_get_names:
            return self.cloud.names(args[0])
        if func is sandbox.resolve_cloud_path:
            return self.cloud.path(args[0])
        if func is transport.stream_download:
            url, destination = args
            payload = self.cloud.content[self.cloud.url_to_id[url]]
            Path(destination).write_bytes(payload)
            return len(payload)
        raise AssertionError(f"未预期的底层调用: {func}")

    def batch_copy(self, file_ids, to_parent_file_id):
        return self.cloud.batch_copy(file_ids, to_parent_file_id)


def hydrate_dir_id(cloud: StubCloud) -> str:
    for file_id, entry in cloud.entries.items():
        if entry["parent"] == SANDBOX and entry["name"] == HYDRATE_DIR_NAME:
            return file_id
    raise AssertionError("暂存目录尚未创建")


# ---------------------------------------------------------------------------
# 懒搬运
# ---------------------------------------------------------------------------

def test_outside_file_is_hydrated_then_downloaded(tmp_path):
    cloud = StubCloud()
    wrapper = StubWrapper(cloud)
    destination = tmp_path / "report.pdf"

    wrapper.download_file(OUTSIDE_FILE, destination)

    assert destination.read_bytes() == b"outside-bytes"
    assert len(cloud.copies) == 1
    source, slot = cloud.copies[0]
    assert source == OUTSIDE_FILE
    assert cloud.entries[slot]["name"] == OUTSIDE_FILE
    assert cloud.entries[slot]["parent"] == hydrate_dir_id(cloud)
    assert cloud.entries[hydrate_dir_id(cloud)]["parent"] == SANDBOX


def test_inside_file_downloaded_without_copy(tmp_path):
    cloud = StubCloud()
    wrapper = StubWrapper(cloud)

    wrapper.download_file(INSIDE_FILE, tmp_path / "inside.txt")

    assert (tmp_path / "inside.txt").read_bytes() == b"inside-bytes"
    assert cloud.copies == []
    assert HYDRATE_DIR_NAME not in [e["name"] for e in cloud.entries.values()]


def test_second_download_reuses_cloud_side_copy(tmp_path):
    cloud = StubCloud()
    wrapper = StubWrapper(cloud)

    wrapper.download_file(OUTSIDE_FILE, tmp_path / "first.bin")
    wrapper.download_file(OUTSIDE_FILE, tmp_path / "second.bin")

    assert (tmp_path / "second.bin").read_bytes() == b"outside-bytes"
    assert len(cloud.copies) == 1, "同一文件在同一进程内不应被重复搬运"


def test_restart_reuses_existing_hydrate_slot(tmp_path):
    """进程重启（新 wrapper）后应认出上次留下的副本，不重复搬运。"""
    cloud = StubCloud()
    first = StubWrapper(cloud)
    first.download_file(OUTSIDE_FILE, tmp_path / "before.bin")
    assert len(cloud.copies) == 1

    restarted = StubWrapper(cloud)
    staged = restarted.hydrate_file(OUTSIDE_FILE)

    slot = cloud.copies[0][1]
    assert len(cloud.copies) == 1, "重启后不应再复制一份"
    assert cloud.entries[staged]["name"] == "report.pdf"
    assert cloud.entries[staged]["parent"] == slot
    assert cloud.entries[slot]["name"] == OUTSIDE_FILE


def test_non_permission_failure_is_not_hydrated(tmp_path):
    """取直链因非沙箱原因失败（如接口抖动）时，不应触发搬运。"""
    cloud = StubCloud()
    cloud.download_errors[OUTSIDE_FILE] = ApiError("搜索失败: 缓存信息失效")
    wrapper = StubWrapper(cloud)

    with pytest.raises(MCloudAPIException):
        wrapper.download_file(OUTSIDE_FILE, tmp_path / "boom.bin")

    assert cloud.copies == []
    assert HYDRATE_DIR_NAME not in [e["name"] for e in cloud.entries.values()]


def test_fresh_inside_file_is_not_hydrated(tmp_path, monkeypatch):
    """刚上传到沙箱的文件：沙箱校验暂时失败 ≠ 在沙箱外，应等待重试而非搬运。

    线上现象：`echo x > /mnt/mcloud/AI空间/智能体云盘助手/f` 成功，紧接着
    `cat` 却报 Permission denied —— 因为新对象路径还没被 batchGetPath 收录，
    被误判成「沙箱外」送去搬运，搬运自己再失败，最终 EACCES。
    """
    cloud = StubCloud()
    fresh_id = cloud.add_file("FRESH", SANDBOX, "fresh.txt", b"fresh-bytes")
    wrapper = StubWrapper(cloud)

    state = {"miss": 0}
    real_path = cloud.path

    def laggy_path(file_id):
        if str(file_id) == fresh_id and state["miss"] < 2:
            state["miss"] += 1
            return ""
        return real_path(file_id)

    monkeypatch.setattr(cloud, "path", laggy_path)
    monkeypatch.setattr(wrapper_module.time, "sleep", lambda _seconds: None)

    wrapper.download_file(fresh_id, tmp_path / "fresh.out")

    assert (tmp_path / "fresh.out").read_bytes() == b"fresh-bytes"
    assert cloud.copies == [], "沙箱内的新文件绝不能被搬运"
    assert HYDRATE_DIR_NAME not in [e["name"] for e in cloud.entries.values()]
    assert state["miss"] == 2, "应经历过路径未就绪并等待重试"


def test_trash_dir_does_not_use_search():
    """回收站目录改用 batchCheckExists 定位/创建，不得调用搜索接口。

    StubWrapper.list_directory 会直接抛 AssertionError，因此这条测试同时也是
    「rm 不再依赖搜索索引」的守卫。
    """
    cloud = StubCloud()
    wrapper = StubWrapper(cloud)
    mapper = InodeMapper(root_file_id=SANDBOX)
    tree = types.SimpleNamespace(mark_directory_stale=lambda *_: None)

    handler = TrashBinHandler(wrapper=wrapper, mapper=mapper, tree=tree)
    trash_id = handler.ensure_trash_dir()

    assert cloud.entries[trash_id]["name"] == TRASH_BIN_DIR_NAME
    assert cloud.entries[trash_id]["parent"] == SANDBOX
    # 再调一次应命中进程内缓存，不重复建
    assert handler.ensure_trash_dir() == trash_id
    assert [e["name"] for e in cloud.entries.values()].count(TRASH_BIN_DIR_NAME) == 1


def test_new_object_path_lag_is_tolerated(tmp_path, monkeypatch):
    """新建对象（暂存目录/槽位/副本）的云盘路径短时间查不到时，应等待而不是立刻放弃。

    对应线上现象：SDK 的 ensure_files_in_sandbox 用 batchGetPath 反查刚创建对象的
    路径，索引未收录时报「权限拒绝：无法确认 xxx 的云盘路径」，最终表现为 EACCES。
    """
    cloud = StubCloud()
    wrapper = StubWrapper(cloud)

    known = {ROOT, AI, SANDBOX, DOC, OUTSIDE_FILE, INSIDE_FILE}
    state = {"miss": 0}
    real_path = cloud.path

    def laggy_path(file_id):
        file_id = str(file_id)
        if file_id not in known and state["miss"] < 2:
            state["miss"] += 1
            return ""
        return real_path(file_id)

    monkeypatch.setattr(cloud, "path", laggy_path)
    monkeypatch.setattr(wrapper_module.time, "sleep", lambda _seconds: None)

    wrapper.download_file(OUTSIDE_FILE, tmp_path / "lag.pdf")

    assert (tmp_path / "lag.pdf").read_bytes() == b"outside-bytes"
    assert state["miss"] == 2, "应经历过两次路径未就绪并重试成功"


# ---------------------------------------------------------------------------
# 内部目录隐藏
# ---------------------------------------------------------------------------

def test_tree_hides_internal_dirs_at_any_level():
    mapper = InodeMapper(root_file_id=ROOT)
    tree = DirectoryTree(mapper=mapper)

    sub = tree.populate_directory(
        InodeMapper.ROOT_INODE, [{"id": "sub", "name": "sub", "isDir": True}]
    )[0]
    entries = tree.populate_directory(
        sub.inode,
        [
            {"id": "t1", "name": TRASH_BIN_DIR_NAME, "isDir": True},
            {"id": "t2", "name": HYDRATE_DIR_NAME, "isDir": True},
            {"id": "t3", "name": "normal", "isDir": True},
        ],
    )

    assert [entry.path for entry in entries] == ["/sub/normal"]
