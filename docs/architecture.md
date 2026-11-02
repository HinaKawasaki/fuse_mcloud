# 架构详解

> 适用版本：v0.4.0（`yun.139.com` Web API 后端）。
> 旧 Skill / 沙箱 / 墓碑实现已整体归档在 [`.old-version/`](../.old-version/README.md)，
> 不参与打包、测试与运行。

本文说明当前实现的模块划分、数据流、并发模型，以及几个关键取舍：
**懒加载、写回、SQLite 日志、关机抢救**。

## 组件总览

```text
┌──────────────────────────── CLI ────────────────────────────┐
│ main.py   mount / unmount / status / setup / doctor / journal│
│ userconfig.py  tokens.py  service.py  sdnotify.py            │
└─────────────────────────────┬───────────────────────────────┘
                              ▼
                    ┌───────────────────┐
                    │ fuse/fuse_ops.py  │  pyfuse3 回调
                    │ MCloudFuseOps     │  阻塞操作进 trio 线程
                    └───┬───────────┬───┘
                        ▼           ▼
              ┌──────────────┐  ┌────────────────────┐
              │ mapping/     │  │ cache/             │
              │ id_map       │  │ meta_cache (LRU)   │
              │ tree         │  │ data_cache         │
              └──────┬───────┘  └─────────┬──────────┘
                     └──────────┬─────────┘
                                ▼
                    ┌──────────────────────┐     ┌───────────────────┐
                    │ client/wrapper.py    │────▶│ router/           │
                    │ DirectClientWrapper  │     │ rate_limiter/retry│
                    └──────────┬───────────┘     └───────────────────┘
                               ▼
                    ┌──────────────────────┐
                    │ client/web139.py     │  签名 / 鉴权 / 端点
                    └──────────┬───────────┘
                               ▼
                         yun.139.com Web API
```

旁路组件：

| 模块 | 职责 |
| --- | --- |
| `journal.py` | SQLite 写回日志；元数据持久化，payload 留在 tmpfs |
| `fsutil.py` | 判断目录在 tmpfs 还是持久盘 |
| `lock.py` | 同一缓存目录的 `flock` 互斥 |
| `setup.py` + `tui.py` | 首次设置向导（curses）与行模式 |
| `service.py` | `systemd --user` 单元生成、安装、状态 |
| `sdnotify.py` | `READY` / `STATUS` / `EXTEND_TIMEOUT_USEC` |

## 启动与生命周期

```text
mcloud-fuse
  ├─ 读 config.json + 环境变量
  ├─ token 缺失/过期 → setup TUI
  ├─ CacheLock(cache_dir).acquire()          # 同一目录只允许一个挂载
  ├─ DirectClientWrapper()                    # 加载 token、准备 HTTP
  ├─ pyfuse3.init(mountpoint)
  ├─ sdnotify.ready()                         # systemd Type=notify 就绪
  ├─ 后台线程 recover_pending_uploads()
  └─ trio + pyfuse3.main()
        │
        └─ SIGTERM / SIGINT
             ├─ sdnotify.extend_timeout()     # 有界，最多 30s
             ├─ rescue_pending_uploads()      # tmpfs → state/rescue
             ├─ flush_deletes()               # 删除队列收尾
             └─ pyfuse3.close()
```

- `mcloud-fuse mount` 也走同一条路径；unit 的 `ExecStart` 就是它。
- 手动运行没有 `NOTIFY_SOCKET` 时，`sdnotify` 全部 no-op。
- systemd 单元的 `Type=notify` 保证 `systemctl start` 在挂载点真正可用后才返回。

## 进程与并发

- FUSE 事件循环跑在 **trio** 上；所有回调是 `async`。
- 阻塞操作（HTTP、磁盘、sha256）通过 `trio.to_thread.run_sync` 进入工作线程。
- 共享状态用 `threading.RLock` 保护：`InodeMapper`、`MetaCache`、`DataCache`、
  `DirectoryTree`、SQLite 日志。
- **读句柄持有真实 fd**：`open` 后把 fd 记在 `_read_fds[fh]`，`read` 用 `os.pread`，
  不会互相改文件偏移。
- **缓存条目钉住**：读句柄存续期间禁止 LRU 删除；淘汰只摘映射，最后一个句柄
  `release` 后才真正 `unlink`（POSIX 语义）。
- **缓存目录锁**：`flock` 锁住 `cache_dir/.mcloud-fuse.lock`，进程被 kill 时内核自动释放。

## inode / 路径 / fileId

`InodeMapper` 维护三张表：

| 表 | 方向 | 用途 |
| --- | --- | --- |
| `_inode_to_entry` | inode → `InodeEntry` | `getattr` / 内核回调 |
| `_path_to_inode` | 本地绝对路径 → inode | `lookup` / 路径解析 |
| `_file_id_to_inode` | 云盘 fileId → inode | 上传后重映射、去重 |

- inode 从 2 单调递增（1 是根），**永不复用**；`readdir` 的 `next_id` 依赖这一点。
- 新建文件先分配 `pending-<uuid>` 占位 fileId，上传成功后 `remap_file_id` 换成真实
  fileId，**inode 不变**，已打开句柄不受影响。
- 同名覆盖时，旧 inode 被回收，路径归属唯一。

## 目录充填

`DirectoryTree.populate_directory` 是目录视图唯一入口：

1. 过滤历史内部目录（`.TrashBin` / `.MCloudHydrate`）；
2. 同名去重：云端允许同名，POSIX 不允许；优先保留本地已映射的 fileId，否则取
   `updatedAt` 最新的一条；
3. 索引宽限期：本地刚创建的节点在云端索引里可能还查不到，默认保留 300 秒；
4. 对账清理：本轮云端没返回且不在宽限期的旧子节点会被移除。

`MetaCache` 对 `getattr` / `readdir` 做 TTL 缓存（默认 60 秒），并用 LRU 限制条数
（默认 4096），避免浏览超大目录时内存无限增长。

## 懒加载读路径

默认 **所有文件** 懒加载（`MCLOUD_LAZY_READ=1`）：

```text
open(inode, O_RDONLY)
  └─ open_lazy(fileId, name, size)
       └─ 建一个大小已知的稀疏文件，不下载内容；句柄钉住该条目

read(fh, off, size)
  └─ ensure_range(fileId, off, size)
       ├─ 命中已覆盖分片 → 直接 pread
       ├─ 缺块 → download_range(4MiB 对齐)
       └─ 服务端不支持 Range（HTTP 200）
            → 整文件落盘并升级为完整缓存
```

- 稀疏文件按 `st_blocks` / 已覆盖分片计费，100GB 空壳不会算成 100GB 缓存；
- 需要完整内容的地方（覆盖写、`truncate`、`rename` 上传）必须走
  `ensure_complete()`，绝不把部分文件当完整文件上传；
- `MCLOUD_LAZY_READ=0` 或 `MCLOUD_EAGER_DOWNLOAD_LIMIT>0` 可退回整文件下载策略；
- 不预取、不全盘同步、启动时不列举整棵树。

## 写回路径

```text
create()/open(O_WRONLY)
  └─ create_write_buffer()        新建 .wb，日志状态 building

write(fh, off, data)
  └─ 直接写 .wb；检查写回上限 + 剩余空间（ENOSPC）

fsync(fh)
  └─ mark ready → uploading → 上传 → 清日志，保留 .wb

release(fh)
  └─ mark ready → uploading → 上传 → remap fileId → finalize
       └─ .wb 改名进读缓存（同文件系统零拷贝；跨文件系统回退拷贝）
```

要点：

- 写回日志状态：`building → pending → uploading → uploaded/清除`；
- 崩溃在 `building` 阶段不会上传半成品；
- 上传成功后再清日志；`uploading` 崩溃由下次启动核对远端同名同大小文件；
- 云盘要求先给总大小，所以必须整文件落本地后上传，**不支持随机写**；
- 分片大小默认 100MiB，严格按序上传；失败重试，末片后 `file/complete`；
- 覆盖写冲突：提交前比对 `updatedAt`，默认另存 `name.conflict-<host>-<ts>`，
  `MCLOUD_CONFLICT=error` 则返回 `ESTALE`。

## 删除 / 回收站

`unlink` / `rmdir`：

```text
默认：/file/batchDelete → 轮询 /task/get 到 Succeed
trash：/recyclebin/batchTrash → 可在 mcloud-fuse trash 恢复
```

- 删除是**真删除**，不是墓碑；
- `batchDelete` / `batchMove` 返回 `taskId`，必须等任务结束再返回，避免 `rm` 后
  对象又出现在下一次 `ls`；
- 网页端删除的对象也出现在回收站 CLI 里。

## SQLite 写回日志与恢复

日志数据库：`~/.local/state/mcloud-fuse/writeback-<hash>.sqlite3`（0600，按写回目录分库）。

| 字段 | 说明 |
| --- | --- |
| `key` | 本地写回 key（通常 `inode-N`） |
| `target_dir_id` / `file_name` | 目标目录与文件名 |
| `payload_path` | `.wb` 或 rescue 文件路径 |
| `base_file_id` / `base_updated_at` | 覆盖写冲突基线 |
| `state` | `building` / `pending` / `uploading` / `rescued` / `lost` |
| `size` / `last_error` | 诊断信息 |

启动时后台补传：

1. `uploading`：先核对远端是否有同名同大小对象；有则清日志，避免重复上传；
2. `building`：半成品，标记 `lost`，不上传；
3. `pending` / `rescued`：从 payload 路径补传；成功转读缓存并清日志；
4. payload 不存在：标记 `lost`，保留记录供 `journal list` 查看。

补传在挂载后后台执行，不阻塞 FUSE 就绪。

## 关机抢救

收到 `SIGTERM` / `SIGINT`：

1. 通过 `sd_notify` 申请一次有界停止延时（默认最多 30 秒）；
2. 只挑 `pending` / `uploading`，按大小从小到大复制到
   `~/.local/state/mcloud-fuse/rescue/<hash>/`；
3. `.part` + `fsync` + 原子 `rename`，更新日志 `payload_path`，状态 `rescued`；
4. 超时/超单文件/超总量/空间不足就跳过，记录原因；`building` 不搬；
5. 下次启动从持久盘补传。

`TimeoutStopSec=60`，抢救自身默认 20 秒硬超时；断电 / SIGKILL 仍无法抢救，
只能靠把 `MCLOUD_WRITE_BACK_DIR` 指到持久盘。

## 资源上限

| 资源 | 默认 | 机制 |
| --- | --- | --- |
| 读缓存 | `min(512, 10% RAM)`，至少 128MiB | LRU，按实际占用 / 已读分片计费 |
| 写回 | `min(1024, 10% RAM)`，至少 256MiB | 写前检查，超限 `ENOSPC` |
| 剩余空间 | 64MiB 保底 | `disk_usage` |
| 元数据 | 4096 条 | `MetaCache` LRU |
| 关机遇失抢救 | 1GiB / 单文件 512MiB / 20 秒 | 有界复制 |

## 限流、重试与错误映射

- `RateLimiter`：并发 3 + 5 RPS（可环境变量覆盖）。
- `retry_call`：指数退避 + 抖动；权限/入参错误不重试。
- `MCloudFuseOps._to_fuse_error`：

| 内部异常 | errno |
| --- | --- |
| 鉴权失败 / token 过期 | `EACCES` |
| 不存在 | `ENOENT` |
| 已存在 | `EEXIST` |
| 写回超过上限 / 磁盘满 | `ENOSPC` |
| 其它 API 异常 | `EIO` |

## 历史实现

v0.3.0 的 Skill 通道、沙箱、`.MCloudHydrate`、墓碑删除与 `.TrashBin` 复制逻辑
全部在 [`.old-version/`](../.old-version/README.md)，仅作对照，不参与构建与测试。
