# mcloud_FUSE

> 基于 `pyfuse3` 与 `trio` 异步事件循环的移动云盘 POSIX 文件系统挂载中间层。

`mcloud_FUSE` 将中国移动云盘（个人云/沙箱）通过 POSIX 接口直接挂载为 Linux 本地目录。通过直接导入（Direct Import）官方 Agent Skill SDK，配合内存目录树与磁盘缓存机制，提供近乎本地文件系统的流畅浏览与读写体验。

---

## ✨ 核心特性与架构设计

* **高吞吐微秒级响应**：内置 `InodeMapper` 与 `MetaCache`，实现 `getattr` / `readdir` 的微秒级 TTL 元数据缓存。
* **正确的目录列举**：`readdir` / `lookup` 只按“当前目录自身的 fileId”调用 `list` 接口并自动翻页，绝不使用全盘搜索。
* **流式读缓存与 LRU 管理**：采用 `DataCache` 在 `/tmp`（`tmpfs`）暂存热点文件，提升重复读取与大文件分块读取效率。
* **写回缓存（Write-Back）**：`write` 先落本地暂存文件，`release` / `fsync` 时整文件上传，适配云盘“整文件上传、无随机写”的语义。
* **统一限流与退避重试**：所有云盘请求都经过 `router/rate_limiter`（线程安全并发 + RPS 控制）与 `router/retry`（指数退避；权限/入参错误不重试）。
* **大文件多段上传**：超过 16MiB 自动切分为 ≥5MiB 的分片、**按序**上传并支持分片级重试，失败自动回退单段上传。
* **数据安全防误删**：拦截 POSIX `unlink` / `rmdir`，把删除改写为复制进 `.TrashBin/` 隐藏目录，并用**持久化墓碑**防止云端旧对象在下次列举时复活。
* **崩溃恢复**：写回操作登记持久化日志，进程异常退出后，下次启动自动补传未完成的文件。

---

## 🗺️ 项目演进路线图 (12-Week Roadmap)

- [x] **第一阶段：架构重构与底层 API 通路（第 1 - 3 周）**
  - [x] SDK 模块集成与 AuthManager 登录状态维护
  - [x] 并发信号量与退避重试控制器
  - [x] 回收站 (`.TrashBin`) 与沙箱路径路由拦截
- [x] **第二阶段：映射层与强元数据缓存（第 4 - 6 周）**
  - [x] `InodeMapper` ↔ `DirectoryTree` 三向双向映射树
  - [x] `MetaCache` 微秒级 TTL 属性与目录缓存
  - [x] `DataCache` 本地 LRU 磁盘缓存与流式下载
- [x] **第三阶段：FUSE 核心功能与挂载（第 7 - 9 周）**
  - [x] **第 7 周**：`MCloudFuseOps` 只读挂载与文件目录浏览 (`ls -la`, `cat`)
  - [x] **第 8 周**：写回缓存 (Write-Back) 与文件写回上传 (`create`, `mknod`, `write`, `fsync`, `release`, `mkdir`, `setattr`)
  - [x] **第 9 周**：回收站删除对接 (`unlink`, `rmdir`, `rename`)
- [x] **第四阶段：崩溃恢复与压测交付（第 10 - 12 周）**
  - [x] 删除墓碑与写回日志的持久化、异常退出后自动补传
  - [x] POSIX 强压测试（大目录一致性、递归遍历、批量读写删）
  - [x] systemd 自动化挂载服务打包（`scripts/install_service.sh`）

---

## 🐛 已修复的关键问题

| 问题 | 根因 | 修复 |
| --- | --- | --- |
| `ls` 出现大量重复行 | `DirectoryTree.populate_directory` 把接口返回的每一条都返回给 `readdir`，云端同名项未去重 | 目录内按名字唯一化；重名时优先保留本地已映射的 fileId，其次更新时间最新 |
| 任意目录都能看到全盘文件、目录“串台” | `readdir` / `lookup` 调用的是**全盘搜索**（未传 `includeFileIds`） | 改为按当前目录 `file_id` 调用 `list_directory`，并自动翻页 |
| 列举分页时条目重复/遗漏 | 子节点来自无序 `set`，且用未排序的 inode 做 `next_id` | 子节点按 inode 升序；`next_id` 严格单调，符合 pyfuse3 契约 |
| 云端删不掉、删除后“复活” | 接口无删除能力，删除仅摘除本地节点 | 删除改写为复制进 `.TrashBin`，并登记持久化墓碑屏蔽旧 fileId |
| `cat` 直接失败 | `DirectClientWrapper` 缺少 `download_file` / `upload_file` | 补齐下载直链 + 流式落盘、上传槽位 + 哈希 + complete 全链路 |
| 写回上传后出现同名幽灵文件 | 覆盖写会生成新 fileId，旧对象仍在云端 | 上传后把旧 fileId 记为墓碑，并把 inode 重映射到新 fileId |
| `close()` 后立刻 `cat` 报“无法确认 pending-xxx 的路径” | FUSE `release` 是异步的：`close()` 返回时上传仍在后台进行 | 增加“在途上传”事件，后续 `open`/`unlink` 会等待上传完成；未关闭的写句柄直接读本地暂存 |
| 新建目录/文件下一次 `ls` 就消失 | 云盘 search 索引最终一致，刚创建的对象几秒内列举不到，被对账清理误删 | 新建/新覆盖对象进入“索引宽限期”（默认 300s），云端未收录前保留本地版本 |
| 限流/重试形同虚设 | `router/rate_limiter.py`、`retry.py` 从未被调用，且用的是 `asyncio` 原语，在 trio 运行时无法工作 | 改写为线程安全同步实现，并通过 `wrapper._api()` 接入**所有**云盘调用 |
| 大文件单段上传、失败只能整体重传 | 只走官方 `api_file_create`（1 个 part） | 超过 16MiB 自动多段上传（分片级重试），失败回退单段 |
| 多段并发上传被服务端拒绝 | 后端要求分片严格按序（`InvalidPartOrder`） | 改为按 partNumber 顺序上传 |

## ⚠️ 已知限制

* 云盘接口**没有删除与重命名能力**：
  * 删除以「复制进 `.TrashBin` + 本地摘除 + 墓碑」近似实现，云端原对象仍占用容量；
  * 文件重命名通过「下载 + 以新名上传 + 旧对象移入回收站」实现；**目录重命名返回 `ENOSYS`**（跨目录同名移动可用）。
* `chmod` / `chown` / 时间戳设置不被云盘支持，调用会被静默忽略（`getattr` 返回当前真实属性）。
* 覆盖写会在云端短暂保留同名旧对象，靠墓碑与去重策略保证本地视图一致。
* 文件时间戳依赖云端返回的 `updatedAt`；无法解析时退回挂载进程当前时间。
* 云盘搜索索引**最终一致**：新对象通常几秒后才出现在 `list`/`search` 结果里。
  本层用“索引宽限期”保证本地视图正确；宽限期内若在其它设备删除该对象，本挂载最长
  300 秒内仍会显示它。
* 删除只是“移入 `.TrashBin` + 墓碑”，**不会释放云盘容量**；回收站内容需要你在云盘 App 里清理。
* 多段上传受两处服务端约束：分片（除末片）不得小于 **5MiB**，且必须**严格按序**上传，
  因此无法用并发分片加速；分片大小会随 `MCLOUD_MAX_PARTS` 自动调整。
* 分片级重试只在同一次进程内有效；进程崩溃后由写回日志整文件重传（不续传单个分片）。

---

## 📦 依赖配置与安装

### 1. 环境依赖

* Linux（支持 FUSE3）
* Python 3.10+
* `fuse3` 系统软件包

```bash
# Ubuntu / Debian 安装 FUSE3 依赖
sudo apt-get update
sudo apt-get install -y fuse3 libfuse3-dev
```

### 2. Python 依赖

```bash
pip3 install -r requirements.txt       # 运行时：pyfuse3 / trio / requests
pip3 install -r requirements-dev.txt   # 开发/测试：额外 pytest、pytest-trio
```

项目元数据与 ruff / pytest 配置在 `pyproject.toml`。

运行时可通过环境变量调整（见 `src/config.py`）：
`MCLOUD_API_MAX_CONCURRENT`、`MCLOUD_API_RPS`、`MCLOUD_MULTIPART_THRESHOLD`、
`MCLOUD_MIN_PART_SIZE`、`MCLOUD_MAX_PARTS`、`MCLOUD_PART_RETRIES`、`MCLOUD_CACHE_DIR`。

首次使用需先完成云盘登录（生成授权页并点击链接）：

```bash
python3 basement/scripts/main.py login
```

### 3. 启动挂载

```bash
python3 src/main.py --mountpoint /tmp/mcloud_mnt --cache-dir /tmp/cmcloud-fuse
# 另开终端
ls -la /tmp/mcloud_mnt
```

### 4. 安装为 systemd 服务（可选）

```bash
sudo ./scripts/install_service.sh /path/to/fuse_mcloud /mnt/mcloud /var/cache/mcloud-fuse
```

---

## 🧪 测试

```bash
# 单元 / 回归测试（离线，不需要登录态；需在已安装 requirements.txt 的环境中运行）
python3 -m pytest tests -q

# 真实 FUSE 挂载冒烟（内存假云盘，不需要登录态）
python3 tests/integration/fuse_mount_smoke.py

# POSIX 强压 / 一致性冒烟
python3 tests/integration/posix_stress.py

# 真实云端 + 真实挂载端到端（默认只读；MCLOUD_LIVE_WRITE=1 追加写入，
# 会在云盘留下 _mcloud_fuse_probe 目录与 .TrashBin 副本）
python3 tests/integration/real_mount_smoke.py

# 大文件真机测试：cp 上传（写回路径）后校验大小/云端索引/全量 sha256/随机抽样
./tests/integration/large_file_live.sh /path/to/big.iso

# 大文件纯下载读回：用全新空缓存挂载，强制从云端下载后校验
./tests/integration/large_file_read_live.sh big.iso <预期sha256> [/path/to/big.iso]

# 真实云盘连通性（需要登录态）
MCLOUD_LIVE_TESTS=1 python3 -m pytest tests/test_wrapper.py tests/test_auth.py -q
```

测试覆盖：目录列举去重与稳定分页、按目录列举、陈旧节点清理、写回上传、覆盖写、截断、
删除进回收站且不复活、墓碑持久化、写回日志崩溃恢复、云盘索引延迟不丢新文件，以及真实内核
挂载与**真实云盘**下的只读/写回/删除全链路。
