# Release Notes

## v0.3.0

本次是**工程化版本**：用户可见功能与 v0.2.1 一致，重点是把仓库从「clone 下来改源码跑」
变成「可安装、有 CI、有回归保护」的包，并修掉一个读缓存的并发缺陷。

### 打包与入口

- `src/` 改为可安装包 `mcloud_fuse`，内部统一绝对导入；顺带消除 `src/fuse/` 与 PyPI
  `fuse` 包的命名冲突。
- 新增命令入口：`pip install -e .` 后可用 `mcloud-fuse --mountpoint ...`，
  等价写法 `python3 -m mcloud_fuse`；新增 `--version`。
- `pyproject.toml` 补齐 `[build-system]`、`[project.scripts]`、license、classifiers、urls
  与 `dev` extra；`requirements*.txt` 改为转发 pyproject，依赖清单不再双份维护。
- 版本号单一来源：`mcloud_fuse/version.py`，pyproject 用 `dynamic = ["version"]` 读取。
- 官方 Skill 的定位集中到 `mcloud_fuse/basement.py`，支持 `MCLOUD_BASEMENT_DIR`，
  找不到时直接提示运行 `scripts/setup_basement.py`（而不是 `ModuleNotFoundError`）。
- systemd 单元改为 `WorkingDirectory=... + -m mcloud_fuse`；真机脚本同步更新。

### 修复

- **读缓存 LRU 淘汰会把正在读的文件删掉**：`read()` 原先每次按路径重新 `open`，
  而 LRU 淘汰直接 `unlink`。常见触发场景是「`open` 之后隔一会儿才读」的程序：
  期间另一个文件的下载触发淘汰，读操作就报 `EBADF`/`ENOENT`。
  现在 `open(O_RDONLY)` 即持有 fd、`read` 走 `os.pread`，同时 `DataCache.acquire_for_read()`
  在句柄存续期间钉住缓存条目，淘汰改为「摘映射 + 延迟删除」，最后一个句柄关闭时才真正 unlink。
- **同一 inode 并发打开会互相覆盖句柄**：读句柄的 fh 改为独立分配（从 `2**32` 起），
  不再复用 inode；否则 `release` 可能关掉别人的 fd、或泄漏 fd。

### 测试与 CI

- 新增 GitHub Actions（`.github/workflows/ci.yml`）：`ruff` + Python 3.10~3.13 矩阵跑离线用例，
  另有一个「覆盖率 + 真实内核挂载」job（`/dev/fuse` + `fusermount3`）。
- 覆盖率把离线单测与真实挂载脚本合并统计，门槛 `fail_under = 69`（当前 71%）。
- `tests/integration/posix_stress.py` 增加：顺序读全部根文件、16 路并发读的字节数校验，
  以及**确定性复现淘汰竞态**的「打开后延迟读取」用例；可用
  `MCLOUD_STRESS_CACHE_MB=1 MCLOUD_STRESS_FILE_BYTES=16384` 强制小缓存触发淘汰。
  该用例在修复前代码上会失败、修复后通过。
- 新增回归用例：钉住条目不被淘汰、嵌套句柄只在最后一次释放后删除、
  读句柄在条目作废后仍能读完、并发打开的句柄相互独立。

### 已验证

- `ruff check` 通过；`pytest tests -q` 58 项通过（2 项 live 用例按环境变量跳过）。
- `pip install -e .` 后 `mcloud-fuse --version` → `mcloud-fuse 0.3.0`；wheel 构建包含全部子包与 entry_points。
- 真实内核挂载：`fuse_mount_smoke.py`、`posix_stress.py`（含 1MiB 缓存 / 16KiB 文件的强制淘汰场景）全部通过。

## v0.2.1

### 修复

- **写入后立刻读取报 `EACCES`**：`download_file()` 把任何沙箱校验失败都当成「文件在沙箱外」
  并送去懒搬运；但**刚上传 / 刚创建的对象**同样会被校验拒绝（`batchGetPath` 还没收录它的
  路径），于是沙箱内的新文件被误搬运，搬运自己再失败，最终表现为 `Permission denied`。
  现在先用 `batchGetPath` 判别成因：`inside` → 等待路径就绪后重试取直链；`outside` → 才做
  懒搬运；`unknown` → 明确报错。
- **删除报 `EIO`**：回收站定位 `.TrashBin` 时用的是 `list_directory()`（= merge search），
  搜索返回「缓存信息失效」时异常消息不含任何 errno 关键词，被 `_to_fuse_error` 兜底映射成
  `EIO`。改为 `batchCheckExists` + `createFolder`，与懒搬运一致，**不再依赖搜索索引**。

### 测试

- 新增 `test_fresh_inside_file_is_not_hydrated`（沙箱内新文件绝不能被搬运）
  与 `test_trash_dir_does_not_use_search`（回收站不得调用搜索接口）。
- 真机验证：`echo > file` → `cat` 读回 → `rm` 全链路在真实云盘通过。

## v0.2.0

### 新功能

- **云盘根挂载**：新增 `--root sandbox|cloud`（或环境变量 `MCLOUD_ROOT`），挂载根可选
  沙箱目录或整个云盘 `/`；默认仍为 `sandbox`，行为与 v0.1.0 一致。
- **沙箱外文件懒搬运**：挂载云盘根时，读取沙箱外文件会自动先云内复制进沙箱暂存目录
  `.MCloudHydrate/<fileId>/` 再下载；重启后按「槽位 + 文件名」复用已有副本，不重复复制。
  `.TrashBin` 与 `.MCloudHydrate` 在所有层级隐藏。
  搬运的触发依据是 SDK 抛出的 `PermissionDeniedError`（不额外查询路径），且全程只用
  `batchCheckExists` / `batchCopy` / `batchGet`，**不依赖最终一致的搜索接口**。
  注意：首次读取需等一次云端复制任务完成，且副本会占用云盘容量（接口没有真删除）。

### 修复与加固

- **优雅退出**：接管 `SIGTERM` / `SIGINT`，systemd `stop` 时先 `terminate()` 再 `close()`
  卸载，不再因进程被直接杀死而留下失效 FUSE 挂载。
- **挂载点自愈**：启动时若发现 `ENOTCONN` 的残留挂载，自动 `fusermount3 -u` / `-uz`
  清理后重试，不再以 `FileExistsError` 陷入重启循环。
- **新建对象路径延迟容忍**：`ensure_files_in_sandbox()` 会用 `batchGetPath` 反查目标
  路径，刚创建 / 刚复制的对象可能还没被收录（报「无法确认 xxx 的云盘路径」，表现为
  `EACCES`）。搬运时对暂存目录、槽位与副本各做一次有界等待（最多 10×1s）。
- 回收站 / 暂存目录改为始终使用**真实沙箱根**，与挂载根解耦。
- systemd 单元新增 `StartLimitIntervalSec` / `StartLimitBurst` 与 `TimeoutStopSec`。

### 测试

- 新增 `tests/test_hydrate.py`、`tests/test_main.py`：懒搬运（含「不得调用搜索接口」硬约束）、
  重启复用、挂载点自愈与优雅退出。

## v0.1.0

首个可公开使用版本：把中国移动云盘（个人云 / AI 沙箱）挂载为 Linux 本地目录。

### 功能

- **只读挂载**：`getattr` / `lookup` / `readdir` / `open` / `read`，支持 `ls`、`cat`、`find`
  与编辑器读取。
- **写回上传**：`create` / `mkdir` / `write` / `fsync` / `release` / `setattr`(truncate) / `mknod`，
  写入先落本地暂存，关闭时整文件上传。
- **删除进回收站**：`unlink` / `rmdir` 改写为复制进 `.TrashBin/`，并用持久化墓碑防止云端旧对象复活。
- **重命名**：文件通过「下载 + 新名上传 + 旧对象进回收站」实现；目录支持跨目录同名移动，
  目录改名返回 `ENOSYS`。
- **崩溃恢复**：写回日志落盘，异常退出后下次启动自动补传。
- **大文件多段上传**：超过阈值自动分片（≥5MiB/片）按序上传、分片级重试、失败回退单段。
- **限流与退避**：所有云盘请求经过线程安全的并发 / RPS 限流与指数退避重试。
- **systemd 集成**：`scripts/install_service.sh` + `mcloud-fuse.service`。

### 修复

- 修复 `readdir` / `lookup` 误用全盘搜索导致的**目录重复行与串台**
  （详见 [`postmortem-readdir.md`](postmortem-readdir.md)）。
- 修复 FUSE `release` 异步导致 `close()` 后立刻读文件会读到占位 fileId 的问题。
- 修复云盘搜索索引最终一致导致新建对象被对账清理误删的问题。
- 修复删除后云端旧对象在下次列举时「复活」的问题（墓碑）。
- 修复多段上传并发/乱序被后端拒绝的问题（改为严格按序）。
- 补齐此前缺失的下载 / 上传实现与 `_to_fuse_error` 错误码映射。
- 清理死代码、拆分运行时与开发依赖、修正文件名为 `README.md`。

### 已知限制

- 云盘接口没有删除与重命名：删除**不释放容量**，目录改名不可用。
- 覆盖写会在云端留下同名旧版本（本地以墓碑屏蔽）。
- 墓碑是本地状态，换机/清缓存后已“删除”的对象会重新出现。
- 多段上传必须按序，无法并发加速；跨进程不支持分片续传。
- `chmod` / `chown` / 时间戳被忽略；不支持锁、xattr、符号链接、`allow_other`。

### 已验证

- 真实云盘：官方 CLI 与本地列举一致、`cat` 字节一致、`mkdir` / 写入 / 覆盖写 / 删除。
- 756MiB 镜像：多段上传（64 片，133s）与空缓存下载（21s），sha256 完全一致。
- 离线测试：`41 passed, 2 skipped`；另有真实挂载冒烟与 POSIX 压测脚本。
