# Release Notes

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
