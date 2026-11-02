"""命令行入口：挂载、卸载、状态、首次设置。

命令设计只有一个目标：**装好之后不需要再记参数**。

    mcloud-fuse              # 已配置 → 挂载；未配置 → 自动进入首次设置向导
    mcloud-fuse mount        # 显式挂载（参数仍可临时覆盖配置）
    mcloud-fuse unmount      # 卸载（必要时先停 systemd 服务）
    mcloud-fuse status       # 配置 / 挂载 / token / 服务一行一项
    mcloud-fuse setup        # 重新运行设置向导
    mcloud-fuse doctor       # 环境与网络自检
    mcloud-fuse trash ...    # 回收站
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Sequence

from mcloud_fuse import config, sdnotify, service, tokens, userconfig
from mcloud_fuse.cache.data_cache import DataCache
from mcloud_fuse.cache.meta_cache import MetaCache
from mcloud_fuse.client.wrapper import DirectClientWrapper
from mcloud_fuse.fuse.fuse_ops import MCloudFuseOps
from mcloud_fuse.journal import WritebackJournal, default_journal_path
from mcloud_fuse.lock import CacheLock, CacheLockError
from mcloud_fuse.mapping.id_map import InodeMapper
from mcloud_fuse.mapping.tree import DirectoryTree
from mcloud_fuse.version import __version__

_LOG_LEVEL = getattr(logging, os.getenv("MCLOUD_LOG", "INFO").upper(), logging.INFO)
logging.basicConfig(level=_LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("mcloud_main")

HELP = """\
用法：mcloud-fuse [命令] [选项]

命令：
  mount       挂载云盘（默认命令）
  unmount     卸载挂载点，必要时先停止 systemd 用户服务
  status      显示配置、挂载点、token 到期时间与服务状态
  setup       首次设置 / 重新配置（全屏向导）
  doctor      环境、网络、配额自检
  journal     写回日志：list | forget | gc | retry
  trash       回收站：list | restore <fileId>... | empty -y

不带命令时：已配置且 token 可用则直接挂载，否则进入首次设置向导。
"""


def parse_args(argv: Optional[Sequence[str]] = None, cfg: Optional[userconfig.UserConfig] = None):
    defaults = cfg or userconfig.UserConfig()
    parser = argparse.ArgumentParser(
        prog="mcloud-fuse",
        description="把中国移动云盘挂载为 Linux 本地目录",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=HELP,
    )
    parser.add_argument(
        "--version", action="version", version=f"mcloud-fuse {__version__}"
    )
    parser.add_argument(
        "--mountpoint",
        type=str,
        default=defaults.mountpoint,
        help=f"本地挂载点（默认: {defaults.mountpoint}）",
    )
    parser.add_argument(
        "--cache-dir",
        type=str,
        default=defaults.cache_dir,
        help=f"本地缓存目录（默认: {defaults.cache_dir}）",
    )
    parser.add_argument(
        "--root",
        type=str,
        default=defaults.root,
        help="挂载根对应的云盘 fileId（默认 / = 整个云盘）",
    )
    parser.add_argument("--foreground", action="store_true", help="前台运行（默认行为，保留兼容）")
    return parser.parse_args(argv)


def prepare_mountpoint(mountpoint: Path) -> None:
    """确保挂载点存在且可用，并清理残留的失效 FUSE 挂载。"""
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


def _import_fuse():
    """延迟导入 pyfuse3/trio，让 setup / status / doctor 在缺 FUSE 时也能跑。"""
    import pyfuse3
    import trio

    return pyfuse3, trio


async def _watch_termination_signals(pyfuse3, trio, stop_requested) -> None:
    """收到 SIGTERM / SIGINT 时优雅终止 FUSE 主循环，并记录这是主动停止。"""
    with trio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
        async for received in signals:
            logger.info("收到信号 %s，正在优雅卸载 ...", received)
            stop_requested.set()
            pyfuse3.terminate()
            return


def _background_recover(data_cache: DataCache) -> None:
    """挂载就绪后在后台补传，不阻塞 FUSE 可用。"""
    try:
        pending = len(data_cache.pending_uploads())
        if pending:
            logger.info("后台补传：发现 %s 个未完成写回文件，开始逐个上传 ...", pending)
        recovered = data_cache.recover_pending_uploads()
        if recovered:
            logger.info("后台补传完成：已补传 %s 个文件", recovered)
        elif pending:
            logger.info("后台补传结束：没有新的文件补传成功（详见日志）")
    except Exception as exc:  # noqa: BLE001
        logger.warning("后台补传失败（不影响挂载）: %s", exc)


async def main(
    argv: Optional[Sequence[str]] = None,
    cfg: Optional[userconfig.UserConfig] = None,
):
    pyfuse3, trio = _import_fuse()
    args = parse_args(argv, cfg)
    mountpoint = Path(args.mountpoint).expanduser().resolve()
    cache_dir = Path(args.cache_dir).expanduser().resolve()

    prepare_mountpoint(mountpoint)
    cache_dir.mkdir(parents=True, exist_ok=True)

    logger.info("正在初始化移动云盘（yun.139.com Web API）...")
    try:
        wrapper = DirectClientWrapper()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"初始化失败：{exc}\n"
            "请运行 `mcloud-fuse setup` 导入 token（或检查 ~/.config/mcloud-fuse/web_token）。"
        ) from exc

    root_file_id = args.root or "/"
    logger.info("挂载根 fileId = %s", root_file_id)

    mapper = InodeMapper(root_file_id=root_file_id)
    tree = DirectoryTree(mapper=mapper)
    meta_cache = MetaCache(ttl=config.META_CACHE_TTL)
    write_back_dir = Path(
        os.getenv("MCLOUD_WRITE_BACK_DIR", str(cache_dir / "writeback"))
    ).expanduser()
    data_cache = DataCache(
        client_wrapper=wrapper,
        cache_dir=cache_dir,
        write_back_dir=write_back_dir,
    )

    try:
        lost = data_cache.lost_uploads()
        if lost:
            logger.warning(
                "有 %s 个写回文件在此前已丢失（tmpfs 重启/清理），记录在 %s；"
                "可用 `mcloud-fuse journal list --lost` 查看",
                len(lost),
                data_cache.journal_path,
            )
    except Exception:  # noqa: BLE001
        pass

    fuse_ops = MCloudFuseOps(
        mapper=mapper,
        tree=tree,
        meta_cache=meta_cache,
        data_cache=data_cache,
        wrapper=wrapper,
    )

    logger.info("正在挂载到 %s ...", mountpoint)
    fuse_options = set(pyfuse3.default_options)
    fuse_options.add("fsname=mcloud_fuse")

    pyfuse3.init(fuse_ops, str(mountpoint), fuse_options)
    # Type=notify：挂载就绪后才算 systemd start 完成；手动运行没有 NOTIFY_SOCKET 时 no-op。
    sdnotify.ready()
    sdnotify.status(f"mounted on {mountpoint}")

    # 先挂载、再补传：否则大量未上传文件会把挂载点卡在“不可用”状态几分钟。
    recovery_thread = threading.Thread(
        target=_background_recover,
        args=(data_cache,),
        name="mcloud-recover",
        daemon=True,
    )
    recovery_thread.start()

    stop_requested = threading.Event()
    try:
        logger.info("挂载成功！现在可以：ls %s", mountpoint)
        logger.info("按 Ctrl+C 卸载并退出。")
        async with trio.open_nursery() as nursery:
            nursery.start_soon(_watch_termination_signals, pyfuse3, trio, stop_requested)
            await pyfuse3.main()
            nursery.cancel_scope.cancel()
    finally:
        logger.info("正在卸载 ...")
        # 1) 让后台补传停手；2) 在卸载前把 tmpfs 里的完整 payload 抢救到持久盘；
        # 3) 再走原来的删除队列收尾与 close。
        data_cache.begin_shutdown()
        if config.RESCUE_ON_SHUTDOWN and stop_requested.is_set():
            try:
                candidates = data_cache.rescuable_uploads()
                if candidates:
                    # 只申请一次，且不超过硬上限；rescue 自身仍有 20s 硬超时。
                    extension = min(
                        max(0.0, config.STOP_EXTEND_MAX_SEC),
                        max(5.0, config.RESCUE_TIMEOUT_SEC + 10.0),
                    )
                    if extension > 0:
                        sdnotify.extend_timeout(extension)
                        sdnotify.status(f"rescuing {len(candidates)} pending file(s)")
            except Exception as exc:  # noqa: BLE001
                logger.debug("申请 systemd 停止延时失败（继续按本地预算抢救）: %s", exc)
        if recovery_thread.is_alive():
            logger.info("等待后台补传让路（最多 10 秒）...")
            recovery_thread.join(timeout=10)
        try:
            rescue = data_cache.rescue_pending_uploads()
            if rescue.count or rescue.skipped:
                logger.info(
                    "关机遇失抢救：%s 个 / %.1f MiB / %.1f 秒%s",
                    rescue.count,
                    rescue.total_bytes / (1024 * 1024),
                    rescue.elapsed,
                    f"（跳过 {len(rescue.skipped)} 个）" if rescue.skipped else "",
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("关机遇失抢救失败（不影响卸载）: %s", exc)
        try:
            if not wrapper.flush_deletes(timeout=10):
                logger.warning("仍有删除任务未提交完成，已放弃等待")
        except Exception as exc:  # noqa: BLE001
            logger.warning("等待删除队列失败: %s", exc)
        pyfuse3.close()
        if recovery_thread.is_alive():
            logger.info("等待后台补传收尾（最多 10 秒）...")
            recovery_thread.join(timeout=10)
        if recovery_thread.is_alive():
            logger.warning("后台补传仍在进行；日志已保留，下次启动会继续补传")
        else:
            try:
                data_cache.close()
            except Exception:  # noqa: BLE001
                pass
        logger.info("卸载完成。")


# ------------------------------------------------------------------- 子命令


def _load_config(require: bool = False) -> Optional[userconfig.UserConfig]:
    try:
        cfg = userconfig.load()
    except userconfig.ConfigError as exc:
        print(f"❌ 配置无法读取：{exc}", file=sys.stderr)
        print("   可以删掉配置后重新运行 mcloud-fuse setup。", file=sys.stderr)
        return None
    if cfg is None and require:
        print("还没有配置。先运行：mcloud-fuse setup", file=sys.stderr)
    return cfg


def _mount(args: Sequence[str], cfg: Optional[userconfig.UserConfig] = None) -> int:
    cfg = cfg or _load_config() or userconfig.UserConfig()
    try:
        import trio
    except ImportError as exc:
        print(f"❌ 缺少挂载依赖：{exc}\n请先安装：pip install -e .", file=sys.stderr)
        return 1

    # 同一个缓存目录只能有一个挂载进程；flock 在进程被 kill 时自动释放。
    try:
        cache_dir = Path(parse_args(list(args), cfg).cache_dir).expanduser()
        lock = CacheLock(cache_dir)
        lock.acquire()
    except CacheLockError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"❌ 无法锁定缓存目录：{exc}", file=sys.stderr)
        return 1

    try:
        trio.run(main, list(args), cfg)
    except KeyboardInterrupt:
        logger.info("已被用户中断。")
    except (ImportError, OSError) as exc:
        print(f"❌ 挂载失败：{exc}\n请确认已安装 FUSE3 与 pyfuse3。", file=sys.stderr)
        return 1
    except SystemExit as exc:
        if exc.code:
            print(exc.code if isinstance(exc.code, str) else f"退出码 {exc.code}", file=sys.stderr)
            return int(exc.code) if isinstance(exc.code, int) else 1
        return 0
    finally:
        lock.release()
    return 0


def _unmount() -> int:
    cfg = _load_config(require=True)
    if cfg is None:
        return 1
    mountpoint = Path(cfg.mountpoint)
    if service.is_active():
        print("正在停止 systemd 用户服务 ...")
        result = service.stop()
        if not result.ok:
            print(f"⚠️  停止服务失败：{result.detail}（继续尝试直接卸载）", file=sys.stderr)
        for _ in range(20):
            if not os.path.ismount(mountpoint):
                break
            time.sleep(0.25)

    if not os.path.ismount(mountpoint):
        print(f"挂载点未挂载：{mountpoint}")
        return 0

    binary = shutil.which("fusermount3") or "fusermount3"
    for flag in ("-u", "-uz"):
        try:
            result = subprocess.run(
                [binary, flag, str(mountpoint)], check=False, capture_output=True, text=True
            )
        except OSError as exc:
            print(f"❌ 无法执行 {binary}：{exc}", file=sys.stderr)
            return 1
        if result.returncode == 0:
            break
        if result.stderr:
            print(result.stderr.strip(), file=sys.stderr)
    if os.path.ismount(mountpoint):
        print(f"❌ 仍无法卸载 {mountpoint}；请先 cd 出该目录再试。", file=sys.stderr)
        return 1
    print(f"✅ 已卸载 {mountpoint}")
    return 0


def _status(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="mcloud-fuse status")
    parser.add_argument("--cache-dir", help="按该缓存目录查写回日志（默认读配置）")
    status_args = parser.parse_args(list(argv) if argv is not None else [])
    cfg = _load_config()
    if cfg is None:
        if userconfig.config_path().exists():
            return 1
        print("状态：未配置")
        print("运行 `mcloud-fuse setup` 开始首次设置。")
        return 0

    mountpoint = Path(cfg.mountpoint)
    mounted = os.path.ismount(mountpoint)
    print("mcloud-fuse 状态")
    print(f"  配置      {userconfig.config_path()}")
    print(f"  挂载点    {mountpoint}  {'[已挂载]' if mounted else '[未挂载]'}")
    print(f"  缓存      {cfg.cache_dir}")
    print(f"  云盘范围  {'整个云盘' if cfg.root == '/' else cfg.root}")
    print(f"  删除方式  {'永久删除' if cfg.rm_mode == 'delete' else '移入回收站'}")
    print(f"  冲突策略  {cfg.conflict}")
    print(f"  token     {tokens.token_summary()}")
    print(f"  服务      {service.status_text()}")
    try:
        cache_for_journal = (
            Path(status_args.cache_dir).expanduser()
            if status_args.cache_dir
            else Path(cfg.cache_dir)
        )
        write_back = Path(
            os.getenv("MCLOUD_WRITE_BACK_DIR", str(cache_for_journal / "writeback"))
        ).expanduser()
        journal_path = default_journal_path(write_back)
        journal = WritebackJournal(journal_path)
        counts = journal.counts()
        journal.close()
        rescued = counts.get("rescued", 0)
        pending = counts.get("pending", 0) + rescued + counts.get("uploading", 0)
        rescued_note = f"，其中 {rescued} 已抢救" if rescued else ""
        print(
            f"  写回日志  {journal_path} "
            f"（{pending} 待传{rescued_note} / {counts.get('lost', 0)} 丢失）"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  写回日志  不可用（{exc}）")
    return 0


def _default_command() -> int:
    cfg = _load_config()
    if cfg is None:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print("还没有配置。请先运行：mcloud-fuse setup", file=sys.stderr)
            return 1
        from mcloud_fuse.setup import main as setup_main

        return setup_main([])

    try:
        token = tokens.load_saved()
    except Exception:  # noqa: BLE001
        token = None
    if token is None or token.is_expired():
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print("token 缺失或已过期。请先运行：mcloud-fuse setup", file=sys.stderr)
            return 1
        print("token 缺失或已过期，进入设置向导 ...")
        from mcloud_fuse.setup import main as setup_main

        return setup_main([])

    return _mount([], cfg)


def cli(argv: Optional[Sequence[str]] = None) -> int:
    args = list(argv) if argv is not None else sys.argv[1:]
    if not args:
        return _default_command()

    command = args[0]
    rest = args[1:]

    if command in ("-h", "--help", "help"):
        print(HELP)
        return 0
    if command in ("--version", "version"):
        print(f"mcloud-fuse {__version__}")
        return 0
    if command in ("setup", "configure"):
        from mcloud_fuse.setup import main as setup_main

        return setup_main(rest)
    if command == "doctor":
        from mcloud_fuse.doctor import main as doctor_main

        return doctor_main(rest)
    if command == "journal":
        from mcloud_fuse.journal_cli import main as journal_main

        return journal_main(rest)
    if command == "trash":
        from mcloud_fuse.trash_cli import main as trash_main

        return trash_main(rest)
    if command == "mount":
        return _mount(rest)
    if command in ("unmount", "umount"):
        return _unmount()
    if command == "status":
        return _status(rest)

    # 向后兼容：mcloud-fuse --mountpoint X --root Y
    if command.startswith("-"):
        return _mount(args)

    print(f"未知命令：{command}\n", file=sys.stderr)
    print(HELP, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(cli())
