#!/usr/bin/env python3
"""对一个已挂载的 mcloud_FUSE 目录跑固定性能矩阵。

用法::

    python3 scripts/bench_mount.py --mount /mnt/mcloud
    python3 scripts/bench_mount.py --mount /mnt/mcloud --big-mb 100 --json-out /tmp/bench.json

脚本只在自己的 ``_mcloud_bench_<时间戳>`` 目录里建/删对象，结束会清理。
读回测试默认是「热读」（写回缓存还在）；冷读请卸载重挂后单独跑
``--read-only <路径>``。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

MiB = 1024 * 1024


def timed(func: Callable, repeat: int = 1) -> Tuple[object, float]:
    samples = []
    result = None
    for _ in range(repeat):
        start = time.perf_counter()
        result = func()
        samples.append(time.perf_counter() - start)
    return result, statistics.median(samples)


def human_mb(seconds: float, size: int) -> str:
    if seconds <= 0:
        return "inf"
    return f"{size / MiB / seconds:.1f} MiB/s"


def list_dir(path: Path) -> int:
    with os.scandir(path) as iterator:
        return sum(1 for _ in iterator)


def list_dir_limited(path: Path, cap: int) -> Tuple[int, bool]:
    count = 0
    with os.scandir(path) as iterator:
        for _ in iterator:
            count += 1
            if count >= cap:
                return count, True
    return count, False


def make_payload(path: Path, size: int, chunk: int = 4 * MiB) -> Tuple[float, float]:
    """写入 size 字节，返回 (write 秒, fsync 秒)。数据用可重复的块，避免占内存。"""
    written = 0
    start = time.perf_counter()
    with path.open("wb") as handle:
        while written < size:
            piece = os.urandom(min(chunk, size - written))
            handle.write(piece)
            written += len(piece)
    write_seconds = time.perf_counter() - start
    sync_start = time.perf_counter()
    with path.open("rb") as handle:
        os.fsync(handle.fileno())
    sync_seconds = time.perf_counter() - sync_start
    return write_seconds, sync_seconds


def read_sequential(path: Path, chunk: int = 4 * MiB) -> Tuple[int, float]:
    total = 0
    start = time.perf_counter()
    with path.open("rb") as handle:
        while True:
            data = handle.read(chunk)
            if not data:
                break
            total += len(data)
    return total, time.perf_counter() - start


def find_large_dir(root: Path, limit: int = 10, cap: int = 5000) -> Optional[Tuple[Path, int]]:
    best: Optional[Tuple[Path, int]] = None
    checked = 0
    try:
        entries = [entry for entry in os.scandir(root) if entry.is_dir(follow_symlinks=False)]
    except OSError:
        return None
    for entry in entries[:limit]:
        try:
            count = 0
            with os.scandir(entry.path) as iterator:
                for _ in iterator:
                    count += 1
                    if count >= cap:
                        break
            checked += 1
            if best is None or count > best[1]:
                best = (Path(entry.path), count)
        except OSError:
            continue
    return best


def bench_metadata(
    root: Path, bench: Path, large_dir: Optional[Path] = None, cap: int = 20000
) -> Dict:
    result: Dict[str, object] = {}

    _, seconds = timed(lambda: list_dir(root), repeat=3)
    result["root_list"] = {"entries": list_dir(root), "seconds": seconds}

    _, seconds = timed(lambda: list_dir(bench), repeat=3)
    result["bench_list"] = {"entries": list_dir(bench), "seconds": seconds}

    if large_dir is not None:
        start = time.perf_counter()
        count, capped = list_dir_limited(large_dir, cap)
        seconds = time.perf_counter() - start
        result["large_list"] = {
            "path": str(large_dir),
            "entries": count,
            "seconds": seconds,
            "capped": capped,
            "cold": True,
        }
    else:
        large = find_large_dir(root, cap=cap)
        if large:
            path, count = large
            start = time.perf_counter()
            count, capped = list_dir_limited(path, cap)
            seconds = time.perf_counter() - start
            result["large_list"] = {
                "path": str(path),
                "entries": count,
                "seconds": seconds,
                "capped": capped,
                "cold": False,
            }

    stats = [os.stat(entry.path) for entry in list(os.scandir(root))[:100]]
    _, seconds = timed(lambda: [os.stat(entry.path) for entry in list(os.scandir(root))[:100]], repeat=3)
    result["stat_100"] = {"count": len(stats), "seconds": seconds}
    return result


def bench_write(bench: Path, size_mb: int) -> Dict:
    path = bench / f"write_{size_mb}MiB.bin"
    size = size_mb * MiB
    write_seconds, fsync_seconds = make_payload(path, size)
    total = write_seconds + fsync_seconds
    result = {
        "size_mib": size_mb,
        "write_seconds": write_seconds,
        "fsync_seconds": fsync_seconds,
        "commit_seconds": total,
        "throughput": human_mb(total, size),
    }
    path.unlink()
    return result


def bench_read(bench: Path, size_mb: int) -> Dict:
    path = bench / f"read_{size_mb}MiB.bin"
    size = size_mb * MiB
    make_payload(path, size)
    total, seconds = read_sequential(path)
    result = {
        "size_mib": size_mb,
        "bytes": total,
        "seconds": seconds,
        "throughput": human_mb(seconds, total),
        "hot": True,
    }
    path.unlink()
    return result


def bench_ops(bench: Path) -> Dict:
    src = bench / "rename_src.txt"
    src.write_bytes(b"x" * 1024)
    _, rename_seconds = timed(lambda: src.rename(bench / "rename_dst.txt"))
    (bench / "rename_dst.txt").unlink()

    _, mkdir_seconds = timed(lambda: (bench / "d").mkdir())
    _, move_seconds = timed(lambda: (bench / "d").rename(bench / "d2"))
    (bench / "d2").rmdir()

    files = [bench / f"rm_{index}.txt" for index in range(20)]
    for path in files:
        path.write_bytes(b"y")
    start = time.perf_counter()
    for path in files:
        path.unlink()
    rm_seconds = time.perf_counter() - start
    return {
        "rename_seconds": rename_seconds,
        "mkdir_seconds": mkdir_seconds,
        "move_dir_seconds": move_seconds,
        "rm_20_files_seconds": rm_seconds,
    }


def bench_concurrency(bench: Path, readers: int = 4, writes: int = 2) -> Dict:
    read_file = bench / "concurrent_read.bin"
    read_file.write_bytes(b"r" * MiB)

    def read_once() -> int:
        with read_file.open("rb") as handle:
            return len(handle.read())

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=readers) as pool:
        list(pool.map(lambda _: read_once(), range(readers * 10)))
    read_seconds = time.perf_counter() - start

    def write_one(index: int) -> float:
        path = bench / f"concurrent_{index}.bin"
        begin = time.perf_counter()
        path.write_bytes(b"w" * MiB)
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
        elapsed = time.perf_counter() - begin
        path.unlink()
        return elapsed

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=writes) as pool:
        latencies = list(pool.map(write_one, range(writes)))
    write_seconds = time.perf_counter() - start
    read_file.unlink()
    return {
        "readers": readers,
        "read_ops": readers * 10,
        "read_seconds": read_seconds,
        "read_ops_per_second": (readers * 10) / read_seconds if read_seconds else 0,
        "writers": writes,
        "write_seconds": write_seconds,
        "write_p50": statistics.median(latencies),
    }


def render_markdown(result: Dict) -> str:
    lines = [
        "# mcloud_FUSE 性能基线",
        "",
        f"- 挂载点：`{result['mount']}`",
        f"- 时间：{result['timestamp']}",
        f"- 主机：{result.get('host', '')}",
        "",
        "## 元数据",
        "",
        "| 场景 | 条目 | 耗时 |",
        "| --- | ---: | ---: |",
    ]
    meta = result["metadata"]
    for key, label in (
        ("root_list", "根目录 ls"),
        ("bench_list", "空目录 ls"),
        ("large_list", "大目录 ls"),
        ("stat_100", "100 次 stat"),
    ):
        if key in meta:
            item = meta[key]
            lines.append(f"| {label} | {item['entries'] if 'entries' in item else item['count']} | {item['seconds']:.3f}s |")
    lines += [
        "",
        "## 写 / 读",
        "",
        "| 场景 | 大小 | 耗时 | 吞吐 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for item in result["writes"]:
        lines.append(
            f"| 写回 + fsync | {item['size_mib']} MiB | {item['commit_seconds']:.2f}s | {item['throughput']} |"
        )
    for item in result["reads"]:
        lines.append(
            f"| 热读 | {item['size_mib']} MiB | {item['seconds']:.2f}s | {item['throughput']} |"
        )
    ops = result["ops"]
    lines += [
        "",
        "## 操作 / 并发",
        "",
        f"- rename：{ops['rename_seconds'] * 1000:.1f} ms",
        f"- mkdir：{ops['mkdir_seconds'] * 1000:.1f} ms",
        f"- 目录 move：{ops['move_dir_seconds'] * 1000:.1f} ms",
        f"- 连续 rm 20 个文件：{ops['rm_20_files_seconds']:.2f}s",
        f"- {result['concurrency']['readers']} 并发读：{result['concurrency']['read_ops_per_second']:.1f} ops/s",
        f"- {result['concurrency']['writers']} 并发写：{result['concurrency']['write_seconds']:.2f}s，p50 {result['concurrency']['write_p50']:.2f}s",
        "",
        "> 热读受本地读缓存影响；冷读请卸载重挂后跑 `--read-only`。",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="mcloud_FUSE 性能基线")
    parser.add_argument("--mount", type=Path, required=True)
    parser.add_argument("--small-mb", type=int, default=1)
    parser.add_argument("--big-mb", type=int, default=100)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--md-out", type=Path)
    parser.add_argument("--read-only", type=Path, help="只读一个指定文件（用于冷读）")
    parser.add_argument("--large-dir", type=Path, help="指定大目录做冷列举（不先扫描）")
    args = parser.parse_args()

    if args.read_only:
        if not args.read_only.is_file():
            print(f"文件不存在：{args.read_only}", file=sys.stderr)
            return 1
        total, seconds = read_sequential(args.read_only)
        print(f"{args.read_only}: {total} bytes, {seconds:.2f}s, {human_mb(seconds, total)}")
        return 0

    mount = args.mount
    if not mount.is_dir():
        print(f"挂载点不存在：{mount}", file=sys.stderr)
        return 1

    stamp = int(time.time())
    bench = mount / f"_mcloud_bench_{stamp}"
    bench.mkdir()
    result: Dict[str, object] = {
        "mount": str(mount),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "host": os.uname().nodename,
        "metadata": {},
        "writes": [],
        "reads": [],
        "ops": {},
        "concurrency": {},
    }
    try:
        print("== 元数据")
        result["metadata"] = bench_metadata(mount, bench, args.large_dir)
        print(json.dumps(result["metadata"], ensure_ascii=False, indent=2))

        for size in (args.small_mb, args.big_mb):
            if size <= 0:
                continue
            if result["writes"] and size == result["writes"][-1]["size_mib"]:
                continue
            print(f"== 写 {size} MiB")
            item = bench_write(bench, size)
            result["writes"].append(item)
            print(item)
            print(f"== 热读 {size} MiB")
            result["reads"].append(bench_read(bench, size))

        print("== 操作")
        result["ops"] = bench_ops(bench)
        print(json.dumps(result["ops"], ensure_ascii=False, indent=2))

        print("== 并发")
        result["concurrency"] = bench_concurrency(bench)
        print(json.dumps(result["concurrency"], ensure_ascii=False, indent=2))
    finally:
        try:
            for child in bench.iterdir():
                if child.is_dir():
                    for sub in child.iterdir():
                        sub.unlink()
                    child.rmdir()
                else:
                    child.unlink()
            bench.rmdir()
        except OSError as exc:
            print(f"清理失败（请手工删除 {bench}）：{exc}", file=sys.stderr)

    markdown = render_markdown(result)
    if args.json_out:
        args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.md_out:
        args.md_out.write_text(markdown, encoding="utf-8")
    print("\n" + markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
