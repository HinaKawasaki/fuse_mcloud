#!/usr/bin/env bash
# 安装并启用 mcloud-fuse 的 systemd 服务。
# 用法： sudo ./scripts/install_service.sh [项目目录] [挂载点] [缓存目录]
set -euo pipefail

PROJECT_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
MOUNTPOINT="${2:-/mnt/mcloud}"
CACHE_DIR="${3:-/var/cache/mcloud-fuse}"
PYTHON_BIN="${MCLOUD_PYTHON:-$(command -v python3)}"

if [[ $EUID -ne 0 ]]; then
  echo "请用 root 运行： sudo $0" >&2
  exit 1
fi

echo "项目目录 : $PROJECT_DIR"
echo "挂载点   : $MOUNTPOINT"
echo "缓存目录 : $CACHE_DIR"
echo "Python   : $PYTHON_BIN"

command -v fusermount3 >/dev/null || { echo "缺少 fusermount3，请先安装 fuse3" >&2; exit 1; }

mkdir -p "$MOUNTPOINT" "$CACHE_DIR"

cat > /etc/default/mcloud-fuse <<EOF
# mcloud-fuse 运行参数
MCLOUD_PROJECT_DIR=$PROJECT_DIR
MCLOUD_MOUNTPOINT=$MOUNTPOINT
MCLOUD_CACHE_DIR=$CACHE_DIR
MCLOUD_PYTHON=$PYTHON_BIN
EOF

install -m 0644 "$PROJECT_DIR/scripts/mcloud-fuse.service" /etc/systemd/system/mcloud-fuse.service
systemctl daemon-reload
systemctl enable --now mcloud-fuse.service

sleep 2
systemctl --no-pager --full status mcloud-fuse.service || true
echo
echo "提示：如挂载失败，请先在 '$PROJECT_DIR' 下完成云盘登录（执行 basement/scripts/main.py login）。"
