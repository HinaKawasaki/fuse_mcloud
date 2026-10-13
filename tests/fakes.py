"""测试共享的内存假云盘。"""
import uuid
from pathlib import Path

from mcloud_fuse.client.wrapper import CloudItem


class FakeCloud:
    """可变的内存云盘：列举 / 下载 / 上传 / 建目录 / 复制。"""

    def __init__(self, initial_dirs=None, initial_content=None):
        self.dirs = initial_dirs if initial_dirs is not None else {
            "root": [{"id": "f_a", "name": "a.txt", "size": 3}]
        }
        self.content = initial_content if initial_content is not None else {"f_a": b"old"}
        self.uploads = []
        self.copies = []
        self.list_calls = []

    def list_directory(self, dir_file_id, recursive=False, page_limit=200, max_items=50000):
        self.list_calls.append(dir_file_id)
        return [
            CloudItem(
                i["id"], i["name"], bool(i.get("isDir")), int(i.get("size") or 0), "", dir_file_id
            )
            for i in self.dirs.get(dir_file_id, [])
        ]

    def download_file(self, file_id, local_save_path):
        path = Path(local_save_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.content[file_id])
        return path

    def upload_file(self, local_file_path, target_dir_id, name=None):
        payload = Path(local_file_path).read_bytes()
        file_name = name or Path(local_file_path).name
        new_id = f"up_{uuid.uuid4().hex[:8]}"
        self.content[new_id] = payload
        self.dirs.setdefault(target_dir_id, []).append(
            {"id": new_id, "name": file_name, "size": len(payload)}
        )
        self.uploads.append((file_name, payload))
        return CloudItem(new_id, file_name, False, len(payload), "", target_dir_id)

    def create_folder(self, name, parent_file_id):
        new_id = f"dir_{uuid.uuid4().hex[:8]}"
        self.dirs.setdefault(parent_file_id, []).append({"id": new_id, "name": name, "isDir": True})
        self.dirs.setdefault(new_id, [])
        return new_id

    def batch_copy(self, file_ids, to_parent_file_id):
        results = []
        for file_id in file_ids:
            new_id = f"copy_{uuid.uuid4().hex[:8]}"
            src = next(
                (i for items in self.dirs.values() for i in items if i["id"] == file_id), None
            )
            self.dirs.setdefault(to_parent_file_id, []).append(
                {"id": new_id, "name": src["name"] if src else file_id,
                 "size": src.get("size", 0) if src else 0}
            )
            if src and file_id in self.content:
                self.content[new_id] = self.content[file_id]
            self.copies.append((file_id, to_parent_file_id))
            results.append({"fileId": file_id, "newFileId": new_id})
        return results
