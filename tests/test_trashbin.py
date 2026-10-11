import sys
import os
import asyncio
from pathlib import Path

# 1. 获取当前文件的真实绝对物理路径
CURRENT_FILE = Path(__file__).resolve()
# 强制定位项目根目录 (fuse_mcloud)
PROJECT_ROOT = CURRENT_FILE.parents[1]
SRC_DIR = PROJECT_ROOT / "src"

# 2. 将真实物理路径 AND 软链接路径全部插入 sys.path 首位
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(SRC_DIR.resolve()))

print(f"🔍 [DEBUG] CURRENT_FILE : {CURRENT_FILE}")
print(f"🔍 [DEBUG] PROJECT_ROOT   : {PROJECT_ROOT}")
print(f"🔍 [DEBUG] SRC_DIR        : {SRC_DIR}")
print(f"🔍 [DEBUG] sys.path[0..2] : {sys.path[:2]}")

try:
    from utils.sandbox_router import SandboxRouter
    from utils.trash_bin import TrashBinHandler
    print("✅ 模块导入成功！")
except ModuleNotFoundError as e:
    print(f"❌ 导入失败，当前 sys.path 为: {sys.path}")
    print(f"❌ 请检查 {SRC_DIR}/utils/sandbox_router.py 文件是否存在！")
    sys.exit(1)

# 模拟底层 Client Wrapper
class MockClientWrapper:
    def __init__(self):
        self.copied_history = []
        self.created_dirs = []

    async def mkdir(self, path: str, confirm: bool = True):
        self.created_dirs.append(path)
        print(f"  [MockClient] 成功创建目录: {path} (confirm={confirm})")
        return True

    async def copy(self, source_path: str, target_path: str, confirm: bool = True):
        self.copied_history.append((source_path, target_path))
        print(f"  [MockClient] 成功复制文件: {source_path} -> {target_path} (confirm={confirm})")
        return True

async def main():
    print("\n🚀 1. 测试 SandboxRouter 路径自动解析与确认选项...")
    router = SandboxRouter()
    
    test_path = "my_folder/docs"
    sandbox_path = router.to_sandbox_path(test_path)
    print(f"  原始相对路径: {test_path}")
    print(f"  拼接后的沙箱路径: {sandbox_path}")
    assert sandbox_path == "/AI空间/智能体云盘助手/my_folder/docs"
    assert router.is_in_sandbox(sandbox_path) is True
    print("✅ 沙箱路由测试通过！")

    print("\n🚀 2. 测试 TrashBinHandler 拦截删除并转存回收站...")
    trash_handler = TrashBinHandler(router)
    mock_client = MockClientWrapper()

    original_file = "/AI空间/智能体云盘助手/test_file.txt"
    trash_dest = await trash_handler.move_to_trash(mock_client, original_file)
    
    print(f"  拦截 unlink({original_file}) -> 实际重定向为: {trash_dest}")
    assert ".TrashBin" in trash_dest
    assert len(mock_client.copied_history) == 1
    print("✅ 回收站移动拦截测试通过！")

if __name__ == "__main__":
    asyncio.run(main())