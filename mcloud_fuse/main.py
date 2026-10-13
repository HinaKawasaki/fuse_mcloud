import argparse
import logging
import os
import signal
import subprocess
from pathlib import Path
from typing import Optional, Sequence

import pyfuse3
import trio

from mcloud_fuse.cache.data_cache import DataCache
from mcloud_fuse.cache.meta_cache import MetaCache
from mcloud_fuse.client.auth_manager import AuthManager
from mcloud_fuse.client.wrapper import DirectClientWrapper
from mcloud_fuse.config import (
    CACHE_DIR,
    CLOUD_ROOT_FILE_ID,
    META_CACHE_TTL,
    ROOT_MODE,
    ROOT_MODE_CLOUD,
    ROOT_MODE_SANDBOX,
)
from mcloud_fuse.fuse.fuse_ops import MCloudFuseOps
from mcloud_fuse.mapping.id_map import InodeMapper
from mcloud_fuse.mapping.tombstones import TombstoneStore
from mcloud_fuse.mapping.tree import DirectoryTree
from mcloud_fuse.version import __version__

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("mcloud_main")


def parse_args(argv: Optional[Sequence[str]] = None):
    parser = argparse.ArgumentParser(description="移动云盘 FUSE 文件系统挂载工具")
    parser.add_argument(
        "--version",
        action="version",
        version=f"mcloud-fuse {__version__}",
        help="打印版本号并退出",
    )
    parser.add_argument(
        "--mountpoint",
        type=str,
        default="/tmp/mcloud_mnt",
        help="本地挂载点路径 (默认: /tmp/mcloud_mnt)"
    )
    parser.add_argument(
        "--cache-dir",
        type=str,
        default=str(CACHE_DIR),
        help=f"本地缓存目录 (默认: {CACHE_DIR})"
    )
    parser.add_argument(
        "--root",
        type=str,
        choices=(ROOT_MODE_SANDBOX, ROOT_MODE_CLOUD),
        default=ROOT_MODE if ROOT_MODE in (ROOT_MODE_SANDBOX, ROOT_MODE_CLOUD)
        else ROOT_MODE_SANDBOX,
        help=(
            "挂载根：sandbox=只暴露沙箱 /AI空间/智能体云盘助手（默认）；"
            "cloud=暴露整个云盘根，沙箱外文件读取时按需搬进沙箱暂存目录，"
            "写操作仍只允许落在沙箱内（默认值可用环境变量 MCLOUD_ROOT 指定）"
        ),
    )
    return parser.parse_args(argv)


def prepare_mountpoint(mountpoint: Path) -> None:
    """确保挂载点存在且可用。

    挂载进程被强杀（例如 systemd 的 SIGTERM 直接终止进程）后会留下一个
    ``ENOTCONN`` 的失效 FUSE 挂载点：此时 ``mkdir(exist_ok=True)`` 会先收到
    ``FileExistsError``，再去 ``is_dir()`` 又撞上 ``ENOTCONN``，最终把
    ``FileExistsError`` 抛出来——服务于是陷入崩溃重启循环。这里先卸掉残留挂载。
    """
    try:
        mountpoint.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        logger.warning("挂载点 %s 存在但不可用，按残留的失效 FUSE 挂载处理", mountpoint)
        for flag in ("-u", "-uz"):
            result = subprocess.run(
                ["fusermount3", flag, str(mountpoint)],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                logger.info("已用 fusermount3 %s 清理残留挂载", flag)
                break
        try:
            mountpoint.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SystemExit(
                f"挂载点 {mountpoint} 仍不可用：{exc}\n"
                f"请确认没有进程停留在该目录（先 cd 出去），再执行 "
                f"`fusermount3 -uz {mountpoint}`"
            ) from exc
    except OSError as exc:
        raise SystemExit(f"无法创建挂载点 {mountpoint}: {exc}") from exc

    if os.path.ismount(mountpoint):
        raise SystemExit(
            f"挂载点 {mountpoint} 上已有挂载，请先卸载："
            f"`fusermount3 -u {mountpoint}`（被占用时用 `-uz`）"
        )


async def _watch_termination_signals() -> None:
    """收到 SIGTERM / SIGINT 时优雅终止 FUSE 主循环。

    pyfuse3 自己不安装信号处理器，默认动作会直接杀死进程并把挂载点变成
    失效挂载；因此这里接管信号，让 ``main()`` 正常返回、由 ``close()`` 卸载。
    """
    with trio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
        async for received in signals:
            logger.info("收到信号 %s，正在优雅卸载 ...", received)
            pyfuse3.terminate()
            return


async def main(argv: Optional[Sequence[str]] = None):
    args = parse_args(argv)
    mountpoint = Path(args.mountpoint).resolve()
    cache_dir = Path(args.cache_dir).resolve()

    # 确保挂载点可用（会自动清理残留的失效挂载）与缓存目录存在
    prepare_mountpoint(mountpoint)
    cache_dir.mkdir(parents=True, exist_ok=True)

    logger.info("🚀 正在初始化移动云盘组件...")
    auth_mgr = AuthManager()
    wrapper = DirectClientWrapper(auth_mgr)

    # 获取沙箱根 ID（挂载云盘根时也要用它来定位 .TrashBin 与暂存目录）
    workspace_id = wrapper.sandbox_root_file_id()
    logger.info("✅ 沙箱 Workspace Root ID: %s", workspace_id)

    root_file_id = CLOUD_ROOT_FILE_ID if args.root == ROOT_MODE_CLOUD else workspace_id
    if args.root == ROOT_MODE_CLOUD:
        logger.info(
            "🌐 挂载根 = 云盘根目录（沙箱外文件按需搬运；写操作仍只允许落在沙箱内）"
        )
    else:
        logger.info("📦 挂载根 = 沙箱目录")

    # 初始化核心挂载组件
    mapper = InodeMapper(root_file_id=root_file_id)
    # 删除墓碑持久化：重启挂载后已删除的云端对象不会复活
    tombstones = TombstoneStore(cache_dir / ".tombstones")
    tree = DirectoryTree(mapper=mapper, tombstones=tombstones)
    meta_cache = MetaCache(ttl=META_CACHE_TTL)
    data_cache = DataCache(
        client_wrapper=wrapper,
        cache_dir=cache_dir,
        write_back_dir=cache_dir / "writeback",
    )

    # 崩溃恢复：把上次异常退出时未上传完的写回文件补传回云盘
    try:
        recovered = data_cache.recover_pending_uploads()
        if recovered:
            logger.info("✅ 崩溃恢复：已补传 %s 个文件", recovered)
    except Exception as exc:  # noqa: BLE001
        logger.warning("崩溃恢复失败（不影响挂载）: %s", exc)

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
        # pyfuse3.main 是 trio 框架下的事件循环回调；同时监听退出信号以便干净卸载
        async with trio.open_nursery() as nursery:
            nursery.start_soon(_watch_termination_signals)
            await pyfuse3.main()
            nursery.cancel_scope.cancel()
    finally:
        logger.info("🧹 正在卸载文件系统...")
        pyfuse3.close()
        logger.info("✅ 卸载完成！")


def cli(argv: Optional[Sequence[str]] = None) -> int:
    """同步命令行入口（``mcloud-fuse`` / ``python -m mcloud_fuse``）。

    pyfuse3 要求事件循环由 trio 驱动，因此这里用 ``trio.run`` 而不是 asyncio。
    """
    try:
        trio.run(main, argv)
    except KeyboardInterrupt:
        logger.info("系统已被用户手动中断。")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
