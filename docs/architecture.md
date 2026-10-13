# 架构详解

本文说明 `mcloud_FUSE` 的模块划分、数据流与并发模型，以及为了适配云盘接口而做的关键设计。

## 组件总览

```text
                        ┌───────────────────────────────┐
             VFS 调用 → │  mcloud_fuse.fuse.fuse_ops    │
                        │  MCloudFuseOps                │
                        └───────┬───────────┬───────────┘
                                │           │
              ┌─────────────────┘           └──────────────────┐
              ▼                                                ▼
   ┌──────────────────────┐                        ┌────────────────────────┐
   │ mapping/             │                        │ cache/                 │
   │  id_map  inode↔path  │                        │  meta_cache 属性/目录  │
   │  tree    目录充填     │                        │  data_cache 读/写回    │
   │  tombstones 删除墓碑  │                        └───────────┬────────────┘
   └──────────┬───────────┘                                    │
              └───────────────────┬────────────────────────────┘
                                  ▼
                        ┌────────────────────────┐      ┌───────────────────────┐
                        │ client/wrapper.py      │─────▶│ router/rate_limiter   │
                        │ DirectClientWrapper    │      │ router/retry          │
                        └───────────┬────────────┘      └───────────────────────┘
                                    ▼
                        ┌────────────────────────┐
                        │ basement/scripts/*.py  │  官方 Skill SDK（Direct Import）
                        └───────────┬────────────┘
                                    ▼
                             云盘 HTTPS 接口
```

## 进程与并发模型

- FUSE 事件循环跑在 **trio** 上（`pyfuse3` 要求）。所有 `Operations` 回调都是 `async`。
- 一切**阻塞操作**（云盘 HTTP、磁盘读写、sha256）都通过 `trio.to_thread.run_sync` 丢到工作线程，
  避免阻塞事件循环。
- 跨线程共享的状态用 `threading.RLock` 保护：
  - `InodeMapper`（三向映射）
  - `MetaCache`（TTL 缓存）
  - `DataCache`（读缓存 / 写回）
  - `DirectoryTree`（目录树、索引宽限期）
  - `TombstoneStore`（墓碑）
- **限流器**（`router/rate_limiter.py`）用 `threading.BoundedSemaphore` + 单调时钟实现。之所以不用
  asyncio/trio 原语，是因为调用发生在工作线程里，且 `DirectClientWrapper` 也会被脚本同步调用。
- **读句柄持有真实 fd**：`open(O_RDONLY)` 时就把本地缓存文件 `open(2)` 出来、把 fd 记在
  `_read_fds[fh]` 上，`read` 用 `os.pread`（不改偏移，天然可并发）。fh 从 `2**32` 起分配，
  与 inode 区分开：同一个 inode 被并发打开多次时，若复用 inode 当 fh，句柄会互相覆盖、
  `release` 还可能关掉别人的 fd。
- **缓存条目会被「钉住」**：`DataCache.acquire_for_read()` 在读句柄存续期间给条目计数，
  LRU 淘汰跳过它们。淘汰一个仍被占用的条目时只摘掉缓存映射，真正的 `unlink` 推迟到最后一个
  `release_read()`（POSIX 语义：已打开的文件不会凭空消失）。这条路径修复了
  「下载 A 触发淘汰，把正在读的 B 删掉 → `read` 报 EBADF」的竞态。

## inode / 路径 / fileId 映射

`InodeMapper` 维护三张表：

| 表 | 方向 | 用途 |
| --- | --- | --- |
| `_inode_to_entry` | inode → `InodeEntry` | `getattr` / 内核回调 |
| `_path_to_inode` | 本地绝对路径 → inode | `lookup` / 路径解析 |
| `_file_id_to_inode` | 云盘 fileId → inode | 上传后重映射、去重 |

- inode 从 2 单调递增分配（1 是挂载根），**永不复用**。这也是 `readdir` 能用 inode 当 `next_id` 的前提：
  新文件 inode 一定更大，追加在末尾，不会跳过或重复已返回条目。
- 新建文件先分配一个 `pending-<uuid>` 占位 fileId；上传成功后用 `remap_file_id` 换成真实 fileId，
  **inode 不变**，因此已打开的句柄不会失效。
- 同一路径出现新 fileId（覆盖写）时，旧 inode 会被回收，路径归属唯一。

## 目录充填与一致性

`DirectoryTree.populate_directory` 是目录视图的唯一入口：

1. **过滤**：跳过 `.TrashBin`（根目录）、跳过在墓碑里的 fileId、跳过没有 fileId 的脏条目。
2. **同名去重**：云盘允许同名，POSIX 不允许。同名时优先保留“本地已映射的 fileId”，
   否则取 `updateTime` 最新的一条。
3. **索引宽限期**：如果本地同一路径上是一个 `pending-` 或刚创建（默认 300s 内）的节点，
   就保留本地版本，不采用云端（可能还没收录）的结果。
4. **对账清理**：本轮云端没返回、且不在宽限期内的旧子节点会被移除，避免幽灵条目。

## 读路径

```text
open(inode)
  └─ DataCache.prepare_file_for_read(fileId)
       命中缓存 → 直接返回本地文件
       未命中   → wrapper.download_file()（直链 + 流式落盘 + LRU 淘汰）
                    ├─ 沙箱内文件   → 直接取直链下载
                    └─ 沙箱外文件（--root cloud）→ hydrate_file() 先云内复制到
                       .MCloudHydrate/<fileId>/，再对副本取直链下载
read(fh, off, size)
  └─ 本地文件 seek + read（工作线程）
```

官方 Skill 的下载接口带沙箱校验（`ensure_files_in_sandbox`），而 `copy` 的**源**允许是
全盘任意位置。因此挂载云盘根时，读取沙箱外文件走「云内复制 → 再下载」两步：每个源文件在
沙箱暂存目录里占一个以自身 fileId 命名的子目录，副本与源同名也不冲突，进程重启后按
「槽位 + 文件名」做同名检查即可复用、不重复搬运。

搬运的触发点是取直链时的 `PermissionDeniedError`（这已经是最准确的「不在沙箱内」信号，
无需再查一次路径）；并且整条链路只用 `batchCheckExists` / `batchCopy` / `batchGet`
这类强一致接口，**不使用搜索接口**——目录列举用的 merge search 是最终一致的，会返回
「缓存信息失效」之类的服务端错误，不该被懒搬运继承。写路径不做搬运
（`upload` / `createFolder` 的目标必须在沙箱内），越界写会由 SDK 拒绝并映射为 `EACCES`。

## 写路径（Write-Back）

```text
create()/open(O_WRONLY)
  └─ DataCache.create_write_buffer()      新建 .wb 暂存，并登记写回日志
write()
  └─ 写入 .wb；更新 mapper 中的 size
fsync(fh)
  └─ _commit_write(final=False)           上传，但**保留** .wb（文件还开着）
release(fh)
  └─ _commit_write(final=True)
       1) 若 dirty/新建 → 上传（>16MiB 走多段）
       2) remap fileId（inode 不变）
       3) finalize：把 .wb **改名**进读缓存目录（同文件系统零拷贝，跨文件系统回退拷贝）
       4) 旧 fileId 记墓碑（覆盖写会在云端留下旧对象）
```

要点：

- **异步 release**：FUSE 的 `release` 不阻塞 `close()`。本层用 `_commit_events`（每个 inode 一个
  `threading.Event`）标记「上传在途」，后续 `open` / `unlink` 会等待它完成，避免读到占位 fileId。
- **零拷贝收尾**：`os.replace(.wb → cache/<fileId>_<name>)`，不再为同一个文件额外拷贝一份。
  可用 `MCLOUD_UPLOAD_KEEP_CACHE=0` 关闭“保留本地副本”，上传后直接删除暂存。
- **为什么不流式上传**：云盘 `file/create` 必须在创建任务时就给出**总大小**与每片大小，
  而写文件时总大小未知。因此只能先落本地、关闭时上传；这是接口限制，不是实现取舍。

## 大文件多段上传

`DirectClientWrapper.upload_file`：

- `size <= MCLOUD_MULTIPART_THRESHOLD`（默认 16MiB）→ 单段上传（官方 `file/create` 一个 part）。
- 否则按 `MIN_PART_SIZE`（5MiB）与 `MAX_PARTS`（64）规划分片，**严格按 partNumber 顺序**上传，
  每片独立重试；最后 `file/complete` 带整文件 SHA-256。
- 任一步失败回退单段上传。

两条实测约束（都是真机上撞出来的）：

1. 除末片外，每片不得小于 **5MiB**，否则返回 `请求参数不合法(00010002)`。
2. 分片必须**按序**上传，并发/乱序会返回 `InvalidPartOrder: fail to get previous part hash context`。

## 删除与墓碑

云盘接口没有删除能力，因此：

```text
unlink/rmdir
  └─ TrashBinHandler.move_to_trash(entry)
       1) copy <fileId> → .TrashBin/<fileId>
       2) mapper 摘除该节点
       3) tombstones.add(fileId)   ← 落盘 cache_dir/.tombstones
```

墓碑在目录充填时过滤对应 fileId。它是**本地状态**：换机器/清缓存后，云端旧对象会重新出现。

## 崩溃恢复

- `DataCache` 在 `write_back_dir/writeback_journal.json` 里记录每个写回文件的
  `{target_dir_id, file_name}`。
- 启动时 `main.py` 调 `recover_pending_uploads()`：对每个仍有 `.wb` 的日志项重新上传，
  成功后改名进读缓存并清除日志。
- 未完成的多段上传无法续传（接口不提供 resume），会作为整文件重新上传。

## 限流与重试

- `RateLimiter(max_concurrent, requests_per_second)`：并发信号量 + 请求间隔。
- `retry_call(func, max_retries, ...)`：指数退避 + 抖动；`ApiError/OSError/超时` 重试，
  `权限拒绝/入参错误/未登录` 立即抛出。
- `DirectClientWrapper._api()` 把两者串起来，所有云盘调用都必须走它。

## 错误映射

`MCloudFuseOps._to_fuse_error` 把内部异常翻译成 `errno`：

| 内部异常 | errno |
| --- | --- |
| `AuthRequiredException` / `MCloudAuthException` | `EACCES` |
| `PermissionDeniedError`（沙箱越界） | `EACCES` |
| 不存在 / not found | `ENOENT` |
| 已存在 | `EEXIST` |
| 其它 API 异常 | `EIO` |
