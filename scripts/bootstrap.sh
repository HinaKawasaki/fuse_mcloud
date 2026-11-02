#!/usr/bin/env bash
# 一键部署：检查依赖 → 建 venv → 安装 → 交给 `mcloud-fuse setup` 完成其余设置。
#
# 用法：
#   ./scripts/bootstrap.sh                          # 交互式首次设置（推荐）
#   ./scripts/bootstrap.sh --deps                   # 缺 fuse3 时尝试 apt 安装（需 sudo）
#   ./scripts/bootstrap.sh --mountpoint ~/cloud --root / --no-service
#   ./scripts/bootstrap.sh --yes                    # 非交互，全部用默认值
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${MCLOUD_PYTHON:-python3}"
INSTALL_DEPS=0
SETUP_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --deps) INSTALL_DEPS=1; shift ;;
    --yes|-y|--no-service|--no-tui|--no-check-api|--force-token)
      SETUP_ARGS+=("$1"); shift ;;
    --mountpoint|--root|--cache-dir|--rm-mode|--conflict)
      SETUP_ARGS+=("$1" "$2"); shift 2 ;;
    -h|--help)
      sed -n '2,10p' "$0"
      exit 0
      ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

step "检查 Python"
command -v "$PYTHON_BIN" >/dev/null || { echo "找不到 $PYTHON_BIN" >&2; exit 1; }
"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit(f"需要 Python 3.10+，当前 {sys.version.split()[0]}")
print(sys.version.split()[0])
PY

step "检查 FUSE"
if ! command -v fusermount3 >/dev/null; then
  if [[ "$INSTALL_DEPS" == "1" ]] && command -v apt-get >/dev/null; then
    sudo apt-get update
    sudo apt-get install -y fuse3 libfuse3-dev
  else
    echo "缺少 fusermount3。请先安装：" >&2
    echo "  sudo apt-get install -y fuse3 libfuse3-dev" >&2
    echo "或加 --deps 让脚本尝试安装。" >&2
    exit 1
  fi
fi
[[ -e /dev/fuse ]] || { echo "/dev/fuse 不存在，请确认内核支持 FUSE" >&2; exit 1; }
echo "fusermount3: $(command -v fusermount3)"

step "准备虚拟环境"
VENV="$PROJECT_DIR/.venv"
if [[ ! -x "$VENV/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV" || {
    echo "创建 venv 失败。Debian/Ubuntu 请先安装 python3-venv。" >&2
    exit 1
  }
fi
"$VENV/bin/python" -V

step "安装 mcloud-fuse"
"$VENV/bin/python" -m pip install --upgrade pip >/dev/null 2>&1 || true
"$VENV/bin/python" -m pip install -e "$PROJECT_DIR"

step "首次设置"
if [[ -t 0 ]]; then
  exec "$VENV/bin/mcloud-fuse" setup "${SETUP_ARGS[@]}"
fi
if "$VENV/bin/python" -c "from mcloud_fuse import tokens; assert tokens.load_saved()" >/dev/null 2>&1; then
  exec "$VENV/bin/mcloud-fuse" setup --no-tui --yes "${SETUP_ARGS[@]}"
fi
echo "当前不是交互终端，且还没有 token。" >&2
echo "请先导入 token，再重新运行本脚本：" >&2
echo "  $VENV/bin/mcloud-fuse setup" >&2
exit 1
