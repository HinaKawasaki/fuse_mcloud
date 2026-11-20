"""`mcloud-fuse doctor`：安装/运行环境自检。

只依赖标准库与 `client/web139.py`，不导入 pyfuse3；因此可以在装 FUSE 之前跑。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional

from mcloud_fuse import config, delete_failures, mountopts, service, userconfig
from mcloud_fuse.client.web139 import (
    Web139Auth,
    Web139AuthError,
    Web139Client,
    Web139Error,
    Web139Token,
)
from mcloud_fuse.fsutil import filesystem_type
from mcloud_fuse.journal import (
    LOST,
    PENDING,
    RESCUED,
    UPLOADING,
    WritebackJournal,
    default_journal_path,
)

OK = "ok"
WARN = "warn"
FAIL = "fail"
_ICON = {OK: "✅", WARN: "⚠️ ", FAIL: "❌"}


@dataclass
class Check:
    name: str
    status: str
    detail: str


def _check_python() -> Check:
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info >= (3, 10):
        return Check("Python", OK, version)
    return Check("Python", FAIL, f"{version}（需要 3.10+）")


def _check_fuse() -> List[Check]:
    checks = []
    binary = shutil.which("fusermount3")
    if binary:
        checks.append(Check("fusermount3", OK, binary))
    else:
        checks.append(Check("fusermount3", FAIL, "未找到；安装 fuse3"))
    device = Path("/dev/fuse")
    if device.exists() and os.access(device, os.R_OK | os.W_OK):
        checks.append(Check("/dev/fuse", OK, "可读写"))
    elif device.exists():
        checks.append(Check("/dev/fuse", FAIL, "存在但当前用户不可读写"))
    else:
        checks.append(Check("/dev/fuse", FAIL, "不存在；需要 FUSE 内核模块"))
    return checks


def _check_token(path: Path) -> List[Check]:
    checks: List[Check] = []
    if not path.exists():
        checks.append(Check("token 文件", FAIL, f"不存在：{path}"))
        return checks
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        checks.append(Check("token 权限", WARN, f"{oct(mode)}，建议 600"))
    else:
        checks.append(Check("token 权限", OK, oct(mode)))
    try:
        token = Web139Token.parse(path.read_text(encoding="utf-8"))
    except (OSError, Web139AuthError) as exc:
        checks.append(Check("token 解析", FAIL, str(exc)))
        return checks
    expiry = token.expires_at_ms
    if expiry is None:
        checks.append(Check("token 到期", WARN, "未携带过期时间"))
    else:
        days = (expiry - int(time.time() * 1000)) / 86400000
        if days <= 0:
            checks.append(Check("token 到期", FAIL, "已过期，请重新导入"))
        elif days < 3:
            checks.append(Check("token 到期", WARN, f"剩余 {days:.1f} 天"))
        else:
            checks.append(Check("token 到期", OK, f"剩余 {days:.1f} 天"))
    return checks


def _check_cache(path: Path) -> Check:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".doctor-write-test"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        free = shutil.disk_usage(path).free / (1024 ** 3)
        return Check(
            "缓存目录",
            OK,
            f"{path}（{filesystem_type(path)}，可用 {free:.1f} GiB）",
        )
    except OSError as exc:
        return Check("缓存目录", FAIL, f"{path}: {exc}")


def _check_api() -> List[Check]:
    try:
        client = Web139Client(Web139Auth.load())
        item = next(iter(client.iter_files("/")), None)
        checks = [Check("API 列举", OK, f"根目录可读{'（示例：' + item.name + '）' if item else ''}")]
    except (Web139AuthError, Web139Error, OSError) as exc:
        return [Check("API 列举", FAIL, str(exc))]
    try:
        quota = client.quota()
        checks.append(
            Check(
                "API 配额",
                OK,
                f"{quota.used_mb / 1024:.1f} GiB / {quota.total_mb / 1024:.1f} GiB",
            )
        )
    except (Web139Error, OSError) as exc:
        checks.append(Check("API 配额", WARN, str(exc)))
    return checks


def _check_journal(write_back_dir: Optional[Path] = None) -> List[Check]:
    path = default_journal_path(write_back_dir)
    try:
        journal = WritebackJournal(path)
    except Exception as exc:  # noqa: BLE001
        return [Check("写回日志", FAIL, f"{path}: {exc}")]
    try:
        counts = journal.counts()
    finally:
        journal.close()
    pending = (
        counts.get(PENDING, 0) + counts.get(RESCUED, 0) + counts.get(UPLOADING, 0)
    )
    rescued = counts.get(RESCUED, 0)
    note = f"，其中 {rescued} 条已抢救" if rescued else ""
    checks = [
        Check(
            "写回日志",
            OK,
            f"{path}（{pending} 条待上传{note}）",
        )
    ]
    if counts.get(LOST):
        checks.append(
            Check(
                "丢失的写回",
                WARN,
                f"{counts[LOST]} 条 payload 已不存在（tmpfs 重启后可能发生）",
            )
        )
    if counts.get(RESCUED):
        checks.append(
            Check("关机抢救", OK, f"{counts[RESCUED]} 条已搬到持久盘，等待补传")
        )
    return checks


def _check_delete_failures() -> Check:
    items = delete_failures.load()
    if not items:
        return Check("删除失败", OK, "无")
    latest = items[-1]
    label = latest.get("name") or latest.get("file_id")
    return Check(
        "删除失败",
        WARN,
        f"{len(items)} 个后台删除失败，对象已重新出现（最近：{label}：{latest.get('error')}）；"
        "重新 rm 或检查网络",
    )


def _check_mountpoint(path: Path) -> Check:
    plan = mountopts.inspect_mountpoint(str(path))
    if plan.action == mountopts.BLOCKED:
        detail = plan.detail + (f"；修复：{plan.fix}" if plan.fix else "")
        return Check("挂载点", FAIL, detail)
    if plan.action == mountopts.NEED_ROOT:
        return Check("挂载点", FAIL, f"{plan.detail}；修复：{plan.fix}")
    if plan.action == mountopts.CREATE:
        return Check("挂载点", WARN, f"还不存在，挂载时会自动创建：{path}")
    if os.path.ismount(path):
        return Check("挂载点", OK, f"已挂载：{path}")
    return Check("挂载点", WARN, f"存在但未挂载：{path}")


def _check_share(allow_other: bool, mountpoint: Path) -> List[Check]:
    if not allow_other:
        return [Check("共享访问", OK, "仅当前用户（allow_other 未开启）")]
    checks: List[Check] = []
    conf = mountopts.fuse_conf_path()
    if mountopts.user_allow_other_enabled():
        checks.append(Check("user_allow_other", OK, str(conf)))
    else:
        checks.append(
            Check(
                "user_allow_other",
                FAIL,
                f"{conf} 里没有 user_allow_other；修复："
                f"{mountopts.enable_user_allow_other_command(conf)}",
            )
        )
    gaps = mountopts.traversal_gaps(mountpoint)
    if gaps:
        shown = "、".join(str(directory) for directory in gaps[:3])
        checks.append(
            Check("父目录可穿越", WARN, f"{shown} 缺 o+x，其他用户可能进不来")
        )
    else:
        checks.append(Check("父目录可穿越", OK, "从 / 到挂载点父目录都有 o+x"))
    checks.append(Check("共享语义", OK, "只读：文件 0644 / 目录 0755，其他用户不能写"))
    return checks


def run_checks(mountpoint: Optional[Path] = None, skip_api: bool = False) -> List[Check]:
    checks = [_check_python()]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        checks.append(
            Check("运行身份", WARN, "root：用户级挂载请不要用 sudo，否则日志/token 会变成 root 所有")
        )
    checks.extend(_check_fuse())
    token_path = config.WEB_TOKEN_FILE
    checks.extend(_check_token(token_path))
    checks.append(_check_cache(config.CACHE_DIR))
    try:
        saved = userconfig.load()
    except userconfig.ConfigError:
        saved = None
    write_back = Path(
        os.getenv(
            "MCLOUD_WRITE_BACK_DIR",
            str(Path(saved.cache_dir) / "writeback") if saved else str(config.WRITE_BACK_DIR),
        )
    ).expanduser()
    checks.extend(_check_journal(write_back))
    if not skip_api:
        checks.extend(_check_api())
    if mountpoint is not None:
        checks.append(_check_mountpoint(mountpoint))
    checks.extend(
        _check_share(bool(saved.allow_other) if saved else False, mountpoint or Path("."))
    )
    checks.append(_check_delete_failures())
    checks.append(Check("rm 模式", OK, "永久删除" if config.RM_PERMANENT else "回收站"))
    checks.append(Check("冲突策略", OK, config.CONFLICT_MODE))
    return checks


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="mcloud-fuse doctor", description="环境自检")
    parser.add_argument("--mountpoint", type=Path, help="顺便检查该挂载点（默认读用户配置）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--skip-api", action="store_true", help="跳过网络检查")
    args = parser.parse_args(argv)

    if args.mountpoint is None:
        try:
            saved = userconfig.load()
        except userconfig.ConfigError:
            saved = None
        args.mountpoint = Path(saved.mountpoint) if saved else None
    checks = run_checks(args.mountpoint, skip_api=args.skip_api)
    checks.append(Check("用户服务", OK if service.status_text() == "运行中" else WARN, service.status_text()))
    if args.json:
        print(json.dumps([asdict(check) for check in checks], ensure_ascii=False, indent=2))
    else:
        for check in checks:
            print(f"{_ICON.get(check.status, '?')} {check.name}: {check.detail}")

    failed = [check for check in checks if check.status == FAIL]
    warned = [check for check in checks if check.status == WARN]
    if failed:
        print(f"\n{len(failed)} 项失败，{len(warned)} 项警告", file=sys.stderr)
        return 1
    print(f"\n✅ 自检通过（{len(warned)} 项警告）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
