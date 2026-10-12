#!/usr/bin/env bash
# 大文件真机测试：把本地大文件通过真实 FUSE 挂载上传到移动云盘，再读回校验。
#
# 用法： ./tests/integration/large_file_live.sh <本地文件> [挂载内目标名]
# 需要已经登录（basement/auth_info.json 有效）。
set -u

VENV="${MCLOUD_VENV:-/home/dsh-ssh/venvs/mcloud}"
PROJECT="${MCLOUD_PROJECT:-/home/hello/dsh-code/fuse_mcloud}"
SRC="${1:?用法: $0 <本地文件> [目标名]}"
NAME="${2:-$(basename "$SRC")}"
MNT="${MCLOUD_MNT:-/tmp/mcloud_large_mnt}"
CACHE="${MCLOUD_CACHE:-/home/dsh-ssh/.cache/mcloud-large}"
MOUNT_LOG=/tmp/mcloud_large_mount.log
PY="$VENV/bin/python"

mkdir -p "$MNT" "$CACHE"
fusermount3 -u "$MNT" 2>/dev/null || true

echo "===== 大文件真机测试 $(date -Is) ====="
echo "source      : $SRC"
echo "target name : $NAME"
echo "mountpoint  : $MNT"
echo "cache dir   : $CACHE"

[ -f "$SRC" ] || { echo "❌ 源文件不存在"; exit 2; }
SIZE=$(stat -c %s "$SRC")
echo "source size : $SIZE bytes"

echo "--- 计算源文件 sha256 ---"
SRC_SHA=$(sha256sum "$SRC" | awk '{print $1}')
echo "source sha256: $SRC_SHA"

echo "--- 启动挂载 ---"
: > "$MOUNT_LOG"
"$PY" "$PROJECT/src/main.py" --mountpoint "$MNT" --cache-dir "$CACHE" >>"$MOUNT_LOG" 2>&1 &
MOUNT_PID=$!
echo "mount pid: $MOUNT_PID"

mounted=0
for _ in $(seq 1 90); do
  if mountpoint -q "$MNT"; then mounted=1; break; fi
  sleep 1
done
if [ "$mounted" != 1 ]; then
  echo "❌ 挂载失败"; tail -30 "$MOUNT_LOG"; kill "$MOUNT_PID" 2>/dev/null; exit 3
fi
ROOT_ID=$(grep -oE "沙箱 Workspace Root ID: [A-Za-z0-9_-]+" "$MOUNT_LOG" | awk '{print $NF}' | tail -1)
echo "✅ 已挂载, sandbox root id = ${ROOT_ID:-unknown}"

echo "--- 上传：cp 到挂载点（写回缓存路径） ---"
T0=$(date +%s)
cp "$SRC" "$MNT/$NAME"
CP_RC=$?
T1=$(date +%s)
echo "cp rc=$CP_RC, 耗时 $((T1 - T0)) 秒"
if [ "$CP_RC" != 0 ]; then
  echo "❌ cp 失败"; grep -E "写回上传失败|ERROR|Exception" "$MOUNT_LOG" | tail -20
  fusermount3 -u "$MNT" 2>/dev/null; kill "$MOUNT_PID" 2>/dev/null; exit 4
fi

echo "--- 等待后台上传完成（release 异步） ---"
UPLOAD_DONE=0
for _ in $(seq 1 300); do
  if grep -q "写回完成: $NAME" "$MOUNT_LOG"; then UPLOAD_DONE=1; break; fi
  if grep -q "写回上传失败" "$MOUNT_LOG"; then break; fi
  sleep 2
done
T2=$(date +%s)
if [ "$UPLOAD_DONE" = 1 ]; then
  echo "✅ 上传完成，cp 返回后再耗时 $((T2 - T1)) 秒（总计 $((T2 - T0)) 秒）"
  grep "写回完成: $NAME" "$MOUNT_LOG" | tail -1
else
  echo "❌ 上传未完成或失败"
  grep -E "写回上传失败|ERROR" "$MOUNT_LOG" | tail -20
  fusermount3 -u "$MNT" 2>/dev/null; kill "$MOUNT_PID" 2>/dev/null; exit 5
fi

echo "--- FUSE stat 校验 ---"
FUSE_SIZE=$(stat -c %s "$MNT/$NAME" 2>&1)
echo "fuse size: $FUSE_SIZE (expect $SIZE)"
[ "$FUSE_SIZE" = "$SIZE" ] && echo "✅ 大小一致" || echo "❌ 大小不一致"

echo "--- 云端索引校验（CLI list） ---"
if [ -n "${ROOT_ID:-}" ]; then
  "$PY" "$PROJECT/basement/scripts/main.py" list "$ROOT_ID" --limit 200 > /tmp/mcloud_large_list.jsonl 2>&1
  CLOUD_LINE=$(grep "\"name\": \"$NAME\"" /tmp/mcloud_large_list.jsonl | head -1)
  echo "cloud entry: ${CLOUD_LINE:-<未找到>}"
  [ -n "$CLOUD_LINE" ] && echo "✅ 云端已可见" || echo "❌ 云端未列出（可能仍在索引中）"
else
  echo "⚠️ 无 root id，跳过 CLI 校验"
fi

echo "--- 经 FUSE 全量读回并计算 sha256 ---"
T3=$(date +%s)
FUSE_SHA=$(cat "$MNT/$NAME" | sha256sum | awk '{print $1}')
T4=$(date +%s)
echo "fuse sha256  : $FUSE_SHA"
echo "source sha256: $SRC_SHA"
echo "读回耗时 $((T4 - T3)) 秒"
[ "$FUSE_SHA" = "$SRC_SHA" ] && echo "✅ 全量读回内容一致" || echo "❌ 全量读回内容不一致"

echo "--- 随机位置抽样比对（4 处 8MiB） ---"
for SKIP in 0 128 512 700; do
  A=$(dd if="$SRC" bs=1M skip=$SKIP count=8 2>/dev/null | sha256sum | awk '{print $1}')
  B=$(dd if="$MNT/$NAME" bs=1M skip=$SKIP count=8 2>/dev/null | sha256sum | awk '{print $1}')
  if [ "$A" = "$B" ]; then echo "  ✅ skip=${SKIP}MiB 一致"; else echo "  ❌ skip=${SKIP}MiB 不一致 ($A vs $B)"; fi
done

echo "--- 卸载 ---"
fusermount3 -u "$MNT" 2>/dev/null && echo "✅ 已卸载" || echo "⚠️ 卸载返回非 0"
kill "$MOUNT_PID" 2>/dev/null
echo "===== 测试结束 $(date -Is) ====="
