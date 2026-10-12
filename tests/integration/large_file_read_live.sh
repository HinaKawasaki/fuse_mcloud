#!/usr/bin/env bash
# 大文件“纯下载读回”真机测试：用全新空缓存挂载，强制从移动云盘下载后校验。
#
# 用法： ./tests/integration/large_file_read_live.sh <云端文件名> <源文件sha256> [源本地文件]
set -u

VENV="${MCLOUD_VENV:-/home/dsh-ssh/venvs/mcloud}"
PROJECT="${MCLOUD_PROJECT:-/home/hello/dsh-code/fuse_mcloud}"
NAME="${1:?用法: $0 <云端文件名> <sha256> [源本地文件]}"
EXPECT_SHA="${2:-}"
SRC="${3:-}"
MNT="${MCLOUD_MNT:-/tmp/mcloud_large_read_mnt}"
CACHE="${MCLOUD_CACHE:-/home/dsh-ssh/.cache/mcloud-large-read}"
MOUNT_LOG=/tmp/mcloud_read_mount.log
PY="$VENV/bin/python"

rm -rf "$CACHE"
mkdir -p "$MNT" "$CACHE"
fusermount3 -u "$MNT" 2>/dev/null || true

echo "===== 大文件纯下载读回测试 $(date -Is) ====="
echo "cloud name : $NAME"
echo "expect sha : $EXPECT_SHA"
echo "cache dir  : $CACHE (全新空目录)"

: > "$MOUNT_LOG"
"$PY" "$PROJECT/src/main.py" --mountpoint "$MNT" --cache-dir "$CACHE" >>"$MOUNT_LOG" 2>&1 &
MOUNT_PID=$!
mounted=0
for _ in $(seq 1 90); do mountpoint -q "$MNT" && { mounted=1; break; }; sleep 1; done
if [ "$mounted" != 1 ]; then echo "❌ 挂载失败"; tail -30 "$MOUNT_LOG"; kill "$MOUNT_PID" 2>/dev/null; exit 3; fi
echo "✅ 已挂载"

TARGET="$MNT/$NAME"
[ -e "$TARGET" ] || { echo "❌ 挂载点中找不到 $NAME"; ls -la "$MNT"; fusermount3 -u "$MNT"; exit 4; }

CLOUD_SIZE=$(stat -c %s "$TARGET")
echo "cloud size : $CLOUD_SIZE bytes"

echo "--- 全量读回（触发从云端下载 756MiB） ---"
T0=$(date +%s)
GOT_SHA=$(cat "$TARGET" | sha256sum | awk '{print $1}')
T1=$(date +%s)
echo "read sha256: $GOT_SHA"
echo "下载+读取耗时: $((T1 - T0)) 秒"
[ -n "$EXPECT_SHA" ] && { [ "$GOT_SHA" = "$EXPECT_SHA" ] && echo "✅ 全量读回一致" || echo "❌ 全量读回不一致"; }
echo "本地缓存占用: $(du -sh "$CACHE" | awk '{print $1}')"

echo "--- 随机位置抽样（0/128/512/700 MiB，各 8MiB） ---"
for SKIP in 0 128 512 700; do
  B=$(dd if="$TARGET" bs=1M skip=$SKIP count=8 2>/dev/null | sha256sum | awk '{print $1}')
  if [ -n "$SRC" ] && [ -f "$SRC" ]; then
    A=$(dd if="$SRC" bs=1M skip=$SKIP count=8 2>/dev/null | sha256sum | awk '{print $1}')
    [ "$A" = "$B" ] && echo "  ✅ skip=${SKIP}MiB 一致 ($B)" || echo "  ❌ skip=${SKIP}MiB 不一致 ($A vs $B)"
  else
    echo "  skip=${SKIP}MiB sha256=$B"
  fi
done

echo "--- 冷/热读性能对比 ---"
T2=$(date +%s); cat "$TARGET" > /dev/null; T3=$(date +%s)
echo "热缓存二次读取耗时: $((T3 - T2)) 秒"

echo "--- 卸载 ---"
fusermount3 -u "$MNT" 2>/dev/null && echo "✅ 已卸载" || echo "⚠️ 卸载返回非 0"
kill "$MOUNT_PID" 2>/dev/null
echo "===== 结束 $(date -Is) ====="
