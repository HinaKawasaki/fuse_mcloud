#!/usr/bin/env python3
"""端点契约检查。

两种用法：

    python3 scripts/check_contracts.py            # 离线：只校验 fixtures 与 schema 一致
    python3 scripts/check_contracts.py --live     # 真机：逐项请求，报告字段漂移
    python3 scripts/check_contracts.py --live --write   # 额外检查上传/改名/移动/删除

`--live` 只读接口不会改动云盘；`--write` 会创建 `_mcloud_contract_*` 探测对象，
结束时删除。任一必需字段缺失即以非零码退出，可直接接 CI / 定时任务。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcloud_fuse.client.web139 import (  # noqa: E402
    QUOTA_URL,
    Web139Auth,
    Web139AuthError,
    Web139Client,
)

CONTRACT_DIR = ROOT / "tests" / "contracts"
SCHEMA = json.loads((CONTRACT_DIR / "schema.json").read_text(encoding="utf-8"))


def resolve(doc: Any, path: str) -> Any:
    current = doc
    for part in path.split("."):
        if part.isdigit():
            current = current[int(part)]
        else:
            current = current[part]
    return current


def check(name: str, doc: Dict[str, Any]) -> List[str]:
    missing = []
    for path in SCHEMA[name]:
        try:
            resolve(doc, path)
        except (KeyError, IndexError, TypeError):
            missing.append(path)
    return missing


def find_downloadable(client: Web139Client) -> Optional[str]:
    for item in client.iter_files("/"):
        if not item.is_dir:
            return item.file_id
        for child in client.iter_files(item.file_id):
            if not child.is_dir:
                return child.file_id
    return None


def collect_readonly(client: Web139Client) -> Dict[str, Dict[str, Any]]:
    docs: Dict[str, Dict[str, Any]] = {}
    docs["file_list"] = client._post_json(
        "/file/list",
        {
            "imageThumbnailStyleList": ["Small", "Large"],
            "orderBy": "updated_at",
            "orderDirection": "DESC",
            "pageInfo": {"pageCursor": "", "pageSize": 10},
            "parentFileId": "/",
        },
    )
    docs["quota_detail"] = client._post_json("", {}, url=QUOTA_URL, timeout=15)
    docs["recyclebin_list"] = client._post_json(
        "/recyclebin/list", {"pageInfo": {"pageSize": 10, "pageCursor": ""}}
    )
    file_id = find_downloadable(client)
    if file_id:
        docs["download_url"] = client._post_json("/file/getDownloadUrl", {"fileId": file_id})
    return docs


def collect_write(client: Web139Client) -> Dict[str, Dict[str, Any]]:
    """建探测对象，采集 create/update/move/delete/task 的真实响应，最后清理。"""
    docs: Dict[str, Dict[str, Any]] = {}
    stamp = int(time.time())
    folder_id = client.create_folder("/", f"_mcloud_contract_{stamp}")
    try:
        docs["file_update"] = client._post_json(
            "/file/update",
            {"fileId": folder_id, "name": f"_mcloud_contract_{stamp}_renamed", "description": ""},
        )
        dest_id = client.create_folder("/", f"_mcloud_contract_dst_{stamp}")
        try:
            docs["batch_task"] = client._post_json(
                "/file/batchMove", {"fileIds": [folder_id], "toParentFileId": dest_id}
            )
            task_id = str((docs["batch_task"].get("data") or {}).get("taskId") or "")
            if task_id:
                docs["task_get"] = client._post_json("/task/get", {"taskId": task_id})
        finally:
            client.delete([dest_id])
    finally:
        try:
            client.delete([folder_id])
        except Exception:  # noqa: BLE001 - 已随 dest 删除或不存在
            pass

    # 内容必须每次不同：固定内容会命中服务端秒传（rapidUpload=true、partInfos=null），
    # 这样既测不到真实上传槽位，还会被 schema 误报为字段漂移。
    data = f"mcloud-contract-{time.time_ns()}-{secrets.token_hex(8)}".encode()
    digest = hashlib.sha256(data).hexdigest()
    name = f"_mcloud_contract_upload_{stamp}.txt"
    create = client._post_json(
        "/file/create",
        {
            "contentHash": digest,
            "contentHashAlgorithm": "SHA256",
            "contentType": "application/octet-stream",
            "parallelUpload": False,
            "partInfos": [
                {
                    "partNumber": 1,
                    "partSize": len(data),
                    "parallelHashCtx": {"partOffset": 0},
                }
            ],
            "size": len(data),
            "parentFileId": "/",
            "name": name,
            "type": "file",
            "fileRenameMode": "auto_rename",
        },
    )
    docs["file_create"] = create
    created = create.get("data") or {}
    try:
        if created.get("partInfos"):
            import requests

            requests.put(
                created["partInfos"][0]["uploadUrl"],
                data=data,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(data)),
                },
                timeout=60,
            )
            client._post_json(
                "/file/complete",
                {
                    "contentHash": digest,
                    "contentHashAlgorithm": "SHA256",
                    "fileId": created["fileId"],
                    "uploadId": created["uploadId"],
                },
            )
    finally:
        if created.get("fileId"):
            client.delete([created["fileId"]])
    return docs


def main() -> int:
    parser = argparse.ArgumentParser(description="检查端点契约是否漂移")
    parser.add_argument("--live", action="store_true", help="请求真实接口")
    parser.add_argument("--write", action="store_true", help="同时检查写接口（会创建探测对象）")
    parser.add_argument("--dump", type=Path, help="把本次真实响应写到该目录，便于更新 fixtures")
    args = parser.parse_args()

    # 离线先保证 fixtures 自身完整
    for name in SCHEMA:
        doc = json.loads((CONTRACT_DIR / f"{name}.json").read_text(encoding="utf-8"))
        missing = check(name, doc)
        if missing:
            print(f"❌ fixture {name} 缺少字段：{', '.join(missing)}")
            return 1
    print(f"✅ 离线：{len(SCHEMA)} 份 fixture 与 schema 一致")

    if not args.live:
        return 0

    try:
        auth = Web139Auth.load()
    except Web139AuthError as exc:
        print(f"❌ 真机检查无法开始：{exc}", file=sys.stderr)
        return 2

    client = Web139Client(auth)
    try:
        docs = collect_readonly(client)
        if args.write:
            docs.update(collect_write(client))
    except Web139AuthError as exc:
        print(f"❌ 真机检查中断：{exc}", file=sys.stderr)
        return 2

    if args.dump:
        args.dump.mkdir(parents=True, exist_ok=True)
        for name, doc in docs.items():
            (args.dump / f"{name}.json").write_text(
                json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        print(f"已写出 {len(docs)} 份响应到 {args.dump}")

    failed = 0
    for name, doc in docs.items():
        missing = check(name, doc)
        if missing:
            failed += 1
            print(f"❌ {name}: 缺少 {', '.join(missing)}")
        else:
            print(f"✅ {name}")

    if failed:
        print(f"\n{failed} 个端点字段漂移，请对照真实响应更新 tests/contracts/")
        return 1
    print("\n🎉 所有已检查端点字段齐全")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
