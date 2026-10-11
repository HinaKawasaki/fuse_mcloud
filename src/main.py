import sys
import os
import argparse
import logging
import trio
from pathlib import Path
import pyfuse3

# 1. 注入 sys.path
SRC_DIR = Path(__file__).resolve().parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

BASEMENT_SCRIPTS_DIR = SRC_DIR.parent / "basement" / "scripts"
if str(BASEMENT_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(BASEMENT_SCRIPTS_DIR))

from client.auth_manager import AuthManager
from client.wrapper import DirectClientWrapper
from mapping.id_map import InodeMapper
from mapping.tree import DirectoryTree
from cache.meta_cache import MetaCache
from cache.data_cache import DataCache
from fuse.fuse_ops import MCloudFuseOps

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("mcloud_main")


def parse_args():
    parser = argparse.ArgumentParser(description="移动云盘 FUSE 文件系统挂载工具")
    parser.add_argument(
        "--mountpoint",
        type=str,
        default="/tmp/mcloud_mnt",
        help="本地挂载点路径 (默认: /tmp/mcloud_mnt)"
    )
    parser.add_argument(
        "--cache-dir",
        type=str,
        default="/tmp/mcloud_data_cache",
        help="本地缓存目录 (默认: /tmp/mcloud_data_cache)"
    )
    return parser.parse_args()


async def main():
    args = parse_args()
    mountpoint = Path(args.mountpoint).resolve()
    cache_dir = Path(args.cache_dir).resolve()

    # 确保挂载点与缓存目录存在
    mountpoint.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    logger.info("🚀 正在初始化移动云盘组件...")
    auth_mgr = AuthManager()
    wrapper = DirectClientWrapper(auth_mgr)

    # 获取沙箱根 ID
    workspace_id = wrapper.ensure_workspace()
    logger.info(f"✅ 沙箱 Workspace Root ID: {workspace_id}")

    # 初始化核心挂载组件
    mapper = InodeMapper(root_file_id=workspace_id)
    tree = DirectoryTree(mapper=mapper)
    meta_cache = MetaCache(ttl=60.0)
    data_cache = DataCache(client_wrapper=wrapper, cache_dir=cache_dir)

    fuse_ops = MCloudFuseOps(
        mapper=mapper,
        tree=tree,
        meta_cache=meta_cache,
        data_cache=data_cache,
        wrapper=wrapper
    )

    # 初始化 pyfuse3 并挂载
    logger.info(f"📌 正在挂载移动云盘到本地目录: {mountpoint} ...")
    fuse_options = set(pyfuse3.default_options)
    fuse_options.add("fsname=mcloud_fuse")

    pyfuse3.init(fuse_ops, str(mountpoint), fuse_options)

    try:
        logger.info("🎉 挂载成功！你现在可以在终端中打开新窗口执行 'ls -la %s' 进行查看。", mountpoint)
        logger.info("按下 Ctrl+C 可停止运行并优雅卸载。")
        # pyfuse3.main 是 trio 框架下的事件循环回调
        await pyfuse3.main()
    finally:
        logger.info("🧹 正在卸载文件系统...")
        pyfuse3.close()
        logger.info("✅ 卸载完成！")


if __name__ == "__main__":
    try:
        # 使用 trio.run 代替 asyncio.run，匹配 pyfuse3 的事件循环要求
        trio.run(main)
    except KeyboardInterrupt:
        logger.info("\n系统已被用户手动中断。")