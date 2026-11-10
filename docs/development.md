# 开发指南

> v0.4.0 的后端是 `yun.139.com` Web API；`basement/`、Skill 登录与
> `scripts/setup_basement.py` 已归档到 `.old-version/`，不再参与构建与测试。

## 环境准备

```bash
git clone https://github.com/HinaKawasaki/fuse_mcloud.git
cd fuse_mcloud

sudo apt-get install -y fuse3 libfuse3-dev
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'                  # 运行时依赖 + pytest / pytest-cov / ruff
```

首次配置：

```bash
mcloud-fuse setup          # 全屏向导；或 --no-tui 走行模式
mcloud-fuse doctor         # 环境/网络/配额自检
```

`~/.config/mcloud-fuse/config.json` 与 `web_token` 都是本机私有文件（0600），
**不要提交**。测试通过 `tests/conftest.py` 把 `MCLOUD_CONFIG_FILE`、`MCLOUD_JOURNAL_DB`、
`MCLOUD_STATE_DIR` 指到临时路径，不会读写开发机的真实配置/日志。

## 代码风格

- 使用 [`ruff`](https://docs.astral.sh/ruff/)：配置见 `pyproject.toml`（`E/F/W/I`，行宽 100）。
- 提交前请确保：

```bash
ruff check mcloud_fuse tests scripts
ruff format --check mcloud_fuse tests scripts   # 可选：格式化
```

- 导入约定：`mcloud_fuse/` 是常规包，内部一律用绝对导入
  （`from mcloud_fuse.config import ...`）。`tests/**` 与 `scripts/**` 需要先注入
  仓库根再 import，所以豁免 E402。
- 版本号只写在 `mcloud_fuse/version.py`，`pyproject.toml` 用
  `dynamic = ["version"]` 读取；`--version` 也取这一处，不要另写死版本。
- **写回日志与 payload 分离**：读缓存 / 待上传文件在 `/tmp/cmcloud-fuse`，SQLite
  日志在 `~/.local/state/mcloud-fuse/writeback-<hash>.sqlite3`（按写回目录分库）。
  测试通过 `tests/conftest.py` 的 `MCLOUD_JOURNAL_DB` 自动隔离，不会污染真实 state。
- **配置读取顺序**：显式环境变量 > `config.json` > `config.py` 默认值。
  `mcloud_fuse/__init__.py` 在包导入时调用 `userconfig.apply_saved_env()`，
  用 `setdefault` 注入，不能覆盖用户显式设置的环境变量。

## 测试策略

| 层级 | 文件 | 是否需要登录态 | 说明 |
| --- | --- | --- | --- |
| 单元/回归 | `tests/test_*.py` | 否 | 纯逻辑，全部离线可跑 |
| 假后端真挂载 | `tests/integration/fuse_mount_smoke.py`、`posix_stress.py` | 否 | 用内存假云盘，走真实内核挂载 |
| 真机脚本 | `scripts/web139_smoke.py`、`scripts/check_contracts.py --live` | 是 | 真实云盘只读 / 全链路写入（会建删探针） |
| 大文件 | `tests/integration/large_file_live.sh`、`large_file_read_live.sh` | 是 | 上传 / 下载 + sha256 校验 |

```bash
# 1) 离线全量（开发中最常用）
python3 -m pytest tests -q

# 2) 假后端真实挂载
python3 tests/integration/fuse_mount_smoke.py
python3 tests/integration/posix_stress.py
# 小缓存强制 LRU 淘汰：回归「淘汰把正在读的文件删掉」的竞态
MCLOUD_STRESS_CACHE_MB=1 MCLOUD_STRESS_FILE_BYTES=16384 python3 tests/integration/posix_stress.py

# 3) 真机（先 mcloud-fuse setup 导入 token）
python3 scripts/web139_smoke.py --list-only   # 只读：列目录 + Range
python3 scripts/web139_smoke.py              # 全链路：建目录/上传/改名/移动/真删除
python3 scripts/check_contracts.py --live

# 4) 大文件
./tests/integration/large_file_live.sh /path/to/big.iso
./tests/integration/large_file_read_live.sh big.iso <sha256> /path/to/big.iso
```

写测试的约定：

- 需要云盘登录态的用例用环境变量门控（`MCLOUD_LIVE_TESTS`），默认 skip，避免 CI/本地误触。
- 真机测试会在云盘里留下文件，命名请带前缀便于清理。
- `tests/fakes.py` 提供共享的内存假云盘；`tests/conftest.py` 负责注入 `sys.path`
  并隔离真实用户配置。
- TUI 的纯逻辑（`display_width` / `wrap_text` / `LineEditor` / `visible_field`）
  在 `tests/test_tui.py`；向导校验与执行在 `tests/test_setup_doctor.py`；
  配置与 token 分别在 `test_userconfig.py` / `test_tokens.py`。
- 资源上限、稀疏文件计量、元数据 LRU、缓存目录锁在 `tests/test_resources.py`；
  写回日志状态机 / 迁移 / 清理在 `tests/test_journal.py` 与 `test_recovery.py`；
  关机抢救在 `tests/test_rescue.py`，sd_notify 协议在 `tests/test_sdnotify.py`。
- 读路径默认全量懒加载；涉及整文件下载的旧用例要显式传 `lazy_read=False`。

## 新增一个 FUSE 回调

1. 在 `mcloud_fuse/fuse/fuse_ops.py` 的 `MCloudFuseOps` 里实现同名方法（签名参考 `pyfuse3.Operations`）。
2. 阻塞逻辑写成同步函数，再用 `await trio.to_thread.run_sync(...)` 调用；不要直接在协程里做网络/磁盘 IO。
3. 需要访问云盘时只使用 `DirectClientWrapper` 的方法（它们自带限流与重试）；
   不要再开新的 HTTP 客户端。
4. 出错时用 `self._to_fuse_error(exc)` 映射 errno；新增错误类型要同步更新映射表。
5. 改动涉及目录视图时，记得失效 `meta_cache` 并 `tree.mark_directory_stale(parent)`。
6. 在 `tests/test_writeback.py` / `test_readdir.py` 里补一条离线用例。

## 调试

```bash
# 更详细日志
MCLOUD_LOG=DEBUG mcloud-fuse mount
```

- 挂载日志默认打在标准错误。
- `fusermount3 -u <mountpoint>` 卸载；`Device or resource busy` 时先 `cd` 出挂载点或用 `lsof` 找占用进程。
- `MCLOUD_API_MAX_CONCURRENT=1 MCLOUD_API_RPS=1` 可以放慢调用，便于观察接口行为。

## 提交与发布

- 提交信息用中文简述「做了什么 + 为什么」，正文可列要点。
- 发布前：

```bash
ruff check mcloud_fuse tests scripts
python3 -m pytest tests -q
python3 -m pytest tests -q --cov=mcloud_fuse --cov-report=term-missing   # 覆盖率
python3 tests/integration/fuse_mount_smoke.py
python3 tests/integration/posix_stress.py
python3 scripts/web139_smoke.py              # 真机全链路（会建删探针）
```

- 同样的检查已由 GitHub Actions 在 push / PR 时执行（见 `.github/workflows/ci.yml`），
  本地先跑一遍可以少一轮往返。

- 打标签并写 release notes（参考 `docs/release-notes.md`）。版本号改
  `mcloud_fuse/version.py`，标签名必须与之一致：

```bash
git tag -a v0.5.0 -m "v0.5.0"
git push origin main --tags
```
