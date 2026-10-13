# 开发指南

## 环境准备

```bash
git clone https://github.com/HinaKawasaki/fuse_mcloud.git
cd fuse_mcloud

sudo apt-get install -y fuse3 libfuse3-dev
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'                  # 含运行时依赖 + pytest / pytest-cov / ruff

python3 scripts/setup_basement.py        # 拉取官方云盘 Skill 到 basement/
```

`basement/` 与其中的 `config.json` / `.env` / `auth_info.json` 都被 `.gitignore` 排除，
**任何情况下都不要把它们提交进仓库**。

## 代码风格

- 使用 [`ruff`](https://docs.astral.sh/ruff/)：配置见 `pyproject.toml`（`E/F/W/I`，行宽 100）。
- 提交前请确保：

```bash
ruff check mcloud_fuse tests
ruff format --check mcloud_fuse tests     # 可选：格式化
```

- 导入约定：`mcloud_fuse/` 是常规包，内部一律用绝对导入
  （`from mcloud_fuse.config import ...`）。官方 Skill 是一组顶层模块，
  统一由 `mcloud_fuse/basement.py` 定位并注入 `sys.path`，因此
  `mcloud_fuse/client/*.py` 里 `import cm_cloud_*` 之前有代码，这几处允许 E402。
  测试脚本同样要先注入仓库根，`tests/**` 也豁免 E402。
- 版本号只写在 `mcloud_fuse/version.py`，`pyproject.toml` 用
  `dynamic = ["version"]` 读取；`--version` 也取这一处，不要另写死版本。

## 测试策略

| 层级 | 文件 | 是否需要登录态 | 说明 |
| --- | --- | --- | --- |
| 单元/回归 | `tests/test_*.py` | 否 | 纯逻辑，全部离线可跑 |
| 假后端真挂载 | `tests/integration/fuse_mount_smoke.py`、`posix_stress.py` | 否 | 用内存假云盘，走真实内核挂载 |
| 真机端到端 | `tests/integration/real_mount_smoke.py` | 是 | 真实云盘只读 + 可选写入 |
| 大文件 | `tests/integration/large_file_live.sh`、`large_file_read_live.sh` | 是 | 上传 / 下载 + sha256 校验 |

```bash
# 1) 离线全量（开发中最常用）
python3 -m pytest tests -q

# 2) 假后端真实挂载
python3 tests/integration/fuse_mount_smoke.py
python3 tests/integration/posix_stress.py

# 3) 真机（先登录）
python3 basement/scripts/main.py login
python3 tests/integration/real_mount_smoke.py
MCLOUD_LIVE_WRITE=1 python3 tests/integration/real_mount_smoke.py

# 4) 大文件
./tests/integration/large_file_live.sh /path/to/big.iso
./tests/integration/large_file_read_live.sh big.iso <sha256> /path/to/big.iso
```

写测试的约定：

- 需要云盘登录态的用例用环境变量门控（`MCLOUD_LIVE_TESTS`），默认 skip，避免 CI/本地误触。
- 真机测试会在云盘里留下文件（接口没有删除能力），命名请带前缀便于清理。
- `tests/fakes.py` 提供共享的内存假云盘；`tests/conftest.py` 负责注入 `sys.path`。

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
# 更详细日志（真机脚本会把日志级别交给 MCLOUD_LOG）
MCLOUD_LOG=DEBUG python3 tests/integration/real_mount_smoke.py
```

- 挂载日志默认打在标准错误。
- `fusermount3 -u <mountpoint>` 卸载；`Device or resource busy` 时先 `cd` 出挂载点或用 `lsof` 找占用进程。
- `MCLOUD_API_MAX_CONCURRENT=1 MCLOUD_API_RPS=1` 可以放慢调用，便于观察接口行为。

## 提交与发布

- 提交信息用中文简述「做了什么 + 为什么」，正文可列要点。
- 发布前：

```bash
ruff check mcloud_fuse tests
python3 -m pytest tests -q
python3 -m pytest tests -q --cov=mcloud_fuse --cov-report=term-missing   # 覆盖率
python3 tests/integration/fuse_mount_smoke.py
python3 tests/integration/posix_stress.py
```

- 同样的检查已由 GitHub Actions 在 push / PR 时执行（见 `.github/workflows/ci.yml`），
  本地先跑一遍可以少一轮往返。

- 打标签并写 release notes（参考 `docs/release-notes.md`）。版本号改
  `mcloud_fuse/version.py`，标签名必须与之一致：

```bash
git tag -a v0.3.0 -m "v0.3.0"
git push origin main --tags
```
