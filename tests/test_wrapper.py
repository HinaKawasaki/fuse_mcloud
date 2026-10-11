import sys
from pathlib import Path

# 1. 导入路径配置
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from client.auth_manager import AuthManager
from client.wrapper import DirectClientWrapper

def test_wrapper():
    print("🚀 初始化 AuthManager 与 DirectClientWrapper...")
    auth_mgr = AuthManager()
    client_wrapper = DirectClientWrapper(auth_mgr)

    print("\n1. 检查并获取沙箱 Workspace ID...")
    workspace_id = client_wrapper.ensure_workspace()
    print(f"✅ 沙箱目录 File ID: {workspace_id}")

    print("\n2. 测试拉取沙箱目录下的文件列表...")
    res = client_wrapper.search_or_list_files(limit=10)
    print(f"✅ 成功查询到 {len(res['items'])} 个文件/文件夹 (全盘共 {res['total_count']} 项):")
    for item in res["items"]:
        kind = "[目录]" if item.is_dir else "[文件]"
        print(f"   {kind} {item.name} | ID: {item.file_id} | 大小: {item.size} Bytes")

if __name__ == "__main__":
    test_wrapper()