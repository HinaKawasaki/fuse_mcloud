# mcloud_FUSE

> 把中国移动云盘（个人云 / AI 沙箱）挂载成 Linux 本地目录的 FUSE 中间层。

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20FUSE3-lightgrey)
![Status](https://img.shields.io/badge/status-beta-orange)

`mcloud_FUSE` 基于 [`pyfuse3`](https://github.com/libfuse/pyfuse3) + [`trio`](https://trio.readthedocs.io/)，
直接导入中国移动云盘官方 Agent Skill 的 Python SDK，在进程内维护「inode ↔ 本地路径 ↔ 云盘 fileId」三向映射、
元数据 TTL 缓存、本地读缓存与写回缓存，从而把云盘沙箱变成一个可以用 `ls` / `cp` / `vim` / `rsync` 直接操作的 POSIX 目录。

> 目前项目已在**真实云盘**上完成只读、写入、删除，以及 756MiB 大文件的上传 / 下载验证；
> 但受云盘接口能力限制，它并不是一个「完整语义」的文件系统，动手前请先阅读 [已知限制](#-已知限制)。

---

## ✨ 特性

- **正确的目录列举**：`readdir` / `lookup` 只按「当前目录自身的 fileId」调用列举接口并自动翻页，
  同名去重、按 inode 稳定分页，绝不使用全盘搜索（这是早期重复行 / 目录串台的根因）。
- **可挂载整个云盘**：`--root cloud` 时挂载根就是云盘 `/`，能浏览全盘目录；
  读沙箱外的文件时自动云内复制进沙箱暂存目录再下载，对上层完全透明。
- **写回缓存（Write-Back）**：`write` 先落本地暂存文件，`fsync` 可中途推送到云端，
  `release` 时整文件上传；收尾用**改名进读缓存（零拷贝）**，不为同一文件再占一份磁盘。
  适配云盘「整文件上传、无随机写」的语义。
- **异步 release 同步**：FUSE 的 `release` 不阻塞 `close()`，因此对「上传在途」做了事件同步，
  `close()` 之后立刻读文件不会读到占位 ID。
- **删除进回收站 + 持久化墓碑**：`unlink` / `rmdir` 改写为复制进 `.TrashBin/` 并登记墓碑，
  避免云端旧对象在下次列举时「复活」；墓碑落盘，重启仍生效。
- **索引最终一致性处理**：云盘搜索索引有延迟，新建对象会进入「索引宽限期」，不会被对账清理误删。
- **大文件多段上传**：超过阈值自动切分（≥5MiB/片）按序上传，分片级重试，失败自动回退单段上传。
- **统一限流与退避重试**：所有云盘请求经过线程安全的并发 / RPS 限流与指数退避；权限与入参错误不重试。
- **崩溃恢复**：写回操作登记持久化日志，进程异常退出后，下次启动自动补传未完成的文件。

---

## 🏗 架构

```text
用户态应用 (ls / cp / vim / rsync ...)
        │  VFS
        ▼
┌──────────────────────────────────────────────┐
│ mcloud_fuse/fuse/fuse_ops.py   MCloudFuseOps         │  pyfuse3.Operations 回调
│  getattr / lookup / readdir / open / read     │  阻塞 I/O 全部丢进 trio 线程
│  create / write / fsync / release / mkdir     │
│  unlink / rmdir / rename / setattr / statfs   │
└───────┬───────────────┬───────────────┬───────┘
        ▼               ▼               ▼
  mapping/          cache/          utils/ + client/
  id_map.py         meta_cache.py   wrapper.py (DirectClientWrapper)
  tree.py           data_cache.py   trash_bin.py
  tombstones.py                      └── router/rate_limiter.py, retry.py
        │
        ▼
  官方 Skill SDK（basement/scripts/*，Direct Import，不走 subprocess）
        │  HTTPS
        ▼
  中国移动云盘开放接口
```

| 模块 | 职责 |
| --- | --- |
| `mcloud_fuse/main.py` | 挂载入口：取沙箱 fileId、装配组件、`pyfuse3.init` + `trio.run` |
| `mcloud_fuse/config.py` | 路径 / TTL / 限流 / 多段上传参数（支持环境变量覆盖） |
| `mcloud_fuse/fuse/fuse_ops.py` | 所有 FUSE 回调实现、写回状态机、错误码映射 |
| `mcloud_fuse/mapping/id_map.py` | inode ↔ path ↔ fileId 映射与分配器 |
| `mcloud_fuse/mapping/tree.py` | 目录充填、同名去重、陈旧对账、索引宽限期 |
| `mcloud_fuse/mapping/tombstones.py` | 已删除 fileId 的持久化墓碑集合 |
| `mcloud_fuse/cache/meta_cache.py` | `getattr` / `readdir` 的 TTL 缓存 |
| `mcloud_fuse/cache/data_cache.py` | 读缓存 + LRU、写回暂存与写回日志（崩溃恢复） |
| `mcloud_fuse/client/wrapper.py` | 云盘 SDK 封装：列举 / 下载 / 上传（多段）、限流与重试落点 |
| `mcloud_fuse/client/auth_manager.py` | 登录态与 creditToken 生命周期 |
| `mcloud_fuse/router/rate_limiter.py` | 线程安全并发 + RPS 限流 |
| `mcloud_fuse/router/retry.py` | 同步指数退避重试 |
| `mcloud_fuse/utils/trash_bin.py` | 回收站（`.TrashBin`）逻辑 |

---

## 📚 文档

- [架构详解](docs/architecture.md)：模块划分、并发模型、读/写/删/恢复四条数据流。
- [开发指南](docs/development.md)：环境搭建、测试策略、如何新增 FUSE 回调、调试与发布。
- [事故复盘：目录列举重复与串台](docs/postmortem-readdir.md)：问题、根因、修复与教训。
- [Release Notes](docs/release-notes.md)。

---

## 📋 环境要求

- Linux，内核支持 FUSE3（需要 `/dev/fuse` 与 `fusermount3`）
- Python 3.10+（开发环境为 3.14）
- 一个中国移动云盘账号

---

## 🚀 安装

### 1. 克隆仓库

```bash
git clone https://github.com/HinaKawasaki/fuse_mcloud.git
cd fuse_mcloud
```

### 2. 安装系统依赖

```bash
# Debian / Ubuntu
sudo apt-get update
sudo apt-get install -y fuse3 libfuse3-dev
```

### 3. 安装本包

```bash
pip3 install -e .            # 安装 mcloud-fuse 命令与运行时依赖
pip3 install -e '.[dev]'     # 可选：额外装 pytest / pytest-cov / ruff（跑测试用）
```

也可以沿用老的 requirements 文件（它们现在只是 `pyproject.toml` 的转发）：

```bash
pip3 install -r requirements.txt
pip3 install -r requirements-dev.txt
```

> `pyfuse3` 需要编译，若失败请确认已安装 `gcc` 与 `libfuse3-dev`。
> 依赖清单以 `pyproject.toml` 为唯一来源，`requirements*.txt` 不再单独维护版本。

### 4. 拉取官方云盘 Skill（必做）

官方 Agent Skill 不在本仓库内（`basement/` 已被 `.gitignore` 排除），需单独下载：

```bash
python3 scripts/setup_basement.py
```

该脚本会从 SkillHub 下载官方 Skill，并把其中的 `scripts/*.py` 复制到 `basement/scripts/`。
官方 Skill 的运行时配置（`config.json`、`.env`，含接口地址与鉴权参数）不在被复制的脚本目录里，
需按官方说明补齐到 `basement/` 下，缺失时命令会提示缺哪些键。

### 5. 登录授权

```bash
python3 basement/scripts/main.py login
```

按输出提示在浏览器完成授权；登录态保存在 `basement/auth_info.json`（**请勿提交到仓库**）。

### 6. 挂载

```bash
mkdir -p /mnt/mcloud
mcloud-fuse --mountpoint /mnt/mcloud --cache-dir ~/.cache/mcloud-fuse
```

> 没走 `pip install`（例如直接在仓库里跑）时，用 `python3 -m mcloud_fuse ...` 等价调用。

默认挂载根是沙箱 `/AI空间/智能体云盘助手`。想浏览**整个云盘**，加 `--root cloud`
（或设 `MCLOUD_ROOT=cloud`）：

```bash
mcloud-fuse --mountpoint /mnt/mcloud --root cloud --cache-dir ~/.cache/mcloud-fuse
```

此时 `ls /mnt/mcloud` 就是云盘根目录。官方 Skill 只允许读沙箱内文件，因此读取沙箱外
文件时，本层会先把云端对象复制到沙箱里的暂存目录 `.MCloudHydrate/<fileId>/` 再下载
（见[已知限制](#-已知限制)）；写操作仍然只能落在沙箱内，越界的 `mkdir` / 写入会返回 `EACCES`。

首次读取某个沙箱外文件要等一次**云端复制任务**完成（秒级到数十秒，取决于文件大小与云端
调度），之后走本地读缓存；重启挂载后按「槽位 + 文件名」复用已有副本，不会重复复制。

另一个终端即可操作：

```bash
ls -la /mnt/mcloud
cat /mnt/mcloud/某个文件
cp ./big.iso /mnt/mcloud/
```

卸载：对运行中的进程按 `Ctrl+C`，或执行 `fusermount3 -u /mnt/mcloud`。
**注意别停在挂载点目录里**执行卸载/重启：`umount` 会因 `EBUSY` 失败，进程被强杀后
挂载点会变成 `ENOTCONN` 的失效挂载（新版启动时会自动清理，见[排障](#-排障)）。

### 7.（可选）安装为 systemd 服务

```bash
sudo ./scripts/install_service.sh /path/to/fuse_mcloud /mnt/mcloud /var/cache/mcloud-fuse
```

脚本会写入 `/etc/default/mcloud-fuse` 并启用 `mcloud-fuse.service`（`Restart=on-failure`）。

---

## ⚙️ 配置

运行时参数集中在 `mcloud_fuse/config.py`，均可用环境变量覆盖：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MCLOUD_CACHE_DIR` | `/tmp/cmcloud-fuse/cache` | 读缓存目录 |
| `MCLOUD_WRITE_BACK_DIR` | `/tmp/cmcloud-fuse/writeback` | 写回暂存 / 写回日志目录 |
| `MCLOUD_API_MAX_CONCURRENT` | `3` | 云盘 API 最大并发 |
| `MCLOUD_API_RPS` | `5` | 云盘 API 每秒请求数上限 |
| `MCLOUD_MULTIPART_THRESHOLD` | `16777216`（16MiB） | 超过该大小改用多段上传 |
| `MCLOUD_MIN_PART_SIZE` | `5242880`（5MiB） | 最小分片（接口硬限制） |
| `MCLOUD_MAX_PARTS` | `64` | 最大分片数（据此自动调整分片大小） |
| `MCLOUD_PART_RETRIES` | `3` | 单个分片上传尝试次数 |
| `MCLOUD_UPLOAD_KEEP_CACHE` | `1` | 上传后是否把写回文件保留为读缓存（`0` = 上传后删除） |
| `MCLOUD_ROOT` | `sandbox` | 挂载根：`sandbox` 只暴露沙箱；`cloud` 暴露云盘根（沙箱外文件懒搬运） |
| `MCLOUD_HYDRATE_DIR` | `.MCloudHydrate` | 沙箱内的懒搬运暂存目录名（挂载云盘根时使用） |
| `MCLOUD_BASEMENT_DIR` | 自动探测 | 官方 Skill 所在目录（`basement` 或 `basement/scripts`），装在仓库外时用它指定 |

`MAX_CACHE_SIZE_MB` 留 `None` 时按物理内存的 25% 自动设定。

---

## 🧪 测试

### 离线单元 / 回归测试（不需要登录态）

```bash
python3 -m pytest tests -q                       # 全部离线用例
python3 -m pytest tests -q --cov=mcloud_fuse     # 附带覆盖率报告
```

覆盖：目录列举去重与稳定分页、按目录列举、陈旧清理、索引延迟、写回上传、覆盖写、截断、
删除进回收站且不复活、墓碑持久化、多段上传规划与字节重组、限流并发上限与退避重试、写回崩溃恢复。

### 真实 FUSE 挂载冒烟（内存假云盘，不需要登录态）

```bash
python3 tests/integration/fuse_mount_smoke.py   # 只读 / 写回 / 删除全链路
python3 tests/integration/posix_stress.py       # 大目录一致性、递归遍历、批量读写删
```

### 真实云盘端到端（需要登录态）

```bash
# 只读校验：官方 CLI 与本地列举一致、cat 字节一致
python3 tests/integration/real_mount_smoke.py

# 追加写入校验（会在云盘留下 _mcloud_fuse_probe 目录与 .TrashBin 副本）
MCLOUD_LIVE_WRITE=1 python3 tests/integration/real_mount_smoke.py

# 大文件上传（cp 走写回 + 多段）与空缓存强制下载
./tests/integration/large_file_live.sh /path/to/big.iso
./tests/integration/large_file_read_live.sh big.iso <预期sha256> [/path/to/big.iso]

# 真实云盘连通性
MCLOUD_LIVE_TESTS=1 python3 -m pytest tests/test_wrapper.py tests/test_auth.py -q
```

> 真机测试会在你的云盘里创建文件。云盘接口**没有删除能力**，测试残留只能手动在 App 里清理。

---

## 🔍 关键设计说明

**为什么 `readdir` 不能用搜索接口？**
云盘「全盘搜索」在不传目录 ID 时会返回整个盘的文件。早期实现把它当成目录列举，导致每个目录都能看到
全盘条目并出现大量重复行。现在只按当前目录 `fileId` 调用列举并自动翻页。

**为什么 `close()` 之后还要等？**
FUSE 的 `release` 是异步的：`close()` 返回时后台上传可能仍在进行。此时若立即 `open` / `read`，
会读到尚未替换的占位 fileId。实现里用「在途上传事件」让后续操作等待；未关闭的写句柄直接读本地暂存。

**为什么读要一直拿着 fd，还要「钉住」缓存？**
读缓存是 LRU 的，下载另一个文件就可能触发淘汰。如果 `read` 每次都按路径重新 `open`，被淘汰的
文件会从正在读的句柄脚下消失（表现为 `EBADF`/`ENOENT`）——尤其常见于「`open` 之后隔一会儿才读」
的程序。现在 `open` 就把文件描述符拿在手上、并用 `pread` 读取，同时把缓存条目标记为「占用中」：
淘汰只会摘掉缓存映射，真正的删除推迟到最后一个句柄 `close()`，与 POSIX 语义一致。

**删除为什么需要墓碑？**
云盘接口没有删除。删除 = 复制进 `.TrashBin` + 本地摘除节点，但云端原对象仍在，下次列举会「复活」。
墓碑记录已删除的 fileId，充填目录时屏蔽它们；墓碑落盘，重启后依然有效。

**索引最终一致怎么办？**
刚创建 / 刚覆盖的对象在几秒内可能查不到。本地会为它们保留一段「宽限期」（默认 300s），
云端收录后自动恢复常规对账。

**多段上传为什么要按序？**
实测该后端会维护「上一片哈希上下文」，并发或乱序会返回 `InvalidPartOrder`；同时除末片外每片不得小于 5MiB。
因此多段的价值在于分片级重试与规避单次超大 PUT，而不是并发提速。

---

## ⚠️ 已知限制

- **没有真正的删除与重命名**：删除只是移入 `.TrashBin` 并登记墓碑，**不释放云盘容量**；
  文件重命名通过「下载 + 新名上传 + 旧对象进回收站」实现，**目录重命名返回 `ENOSYS`**（跨目录同名移动可用）。
- **挂载云盘根时沙箱外文件需「懒搬运」**：官方接口只允许下载沙箱内文件，因此第一次读取
  沙箱外文件会先云内复制到沙箱暂存目录 `.MCloudHydrate/<fileId>/`（对用户隐藏）。
  副本同样**不释放容量**、会占用云盘空间，同一文件同一进程内只搬一次，重启后按
  「槽位 + 文件名」复用；写、改名、删除仍只允许发生在沙箱内。
  首次读延迟取决于云端复制任务；整条搬运链路只用 `batchCheckExists` / `batchCopy` /
  `batchGet`，不依赖最终一致的搜索接口。
- **覆盖写会产生同名旧版本**：云盘无 in-place 更新，覆盖写会生成新 fileId，旧对象留在云端（本地用墓碑屏蔽）。
- **墓碑是本地状态**：换机器 / 清空缓存目录后，之前「删除」过的云端对象会重新出现。
- **多段上传必须按序**，无法并发加速；跨进程不支持从中间分片续传（崩溃后由写回日志整文件重传）。
- **写回必须整文件落本地**：接口要求在创建上传任务时就给出总大小，无法边写边传。不过收尾是
  `os.replace` 改名进读缓存（同文件系统零拷贝），不会为同一文件再占一份磁盘；
  `MCLOUD_UPLOAD_KEEP_CACHE=0` 可在上传后直接删除本地副本。
- **POSIX 语义不完整**：`chmod` / `chown` / 时间戳设置被静默忽略；不支持 `flock` / `fcntl` 锁、xattr、
  符号链接 / 硬链接、`allow_other` 多用户。
- **元数据 TTL 60s**：远端改动最长约 1 分钟才可见；索引宽限期内远端删除也可能被暂时隐藏。
- 文件大小以云端元数据为准；若云端 size 不准，读取会被内核按 `st_size` 截断。

---

## 🩺 排障

| 现象 | 原因 / 处理 |
| --- | --- |
| `未登录授权，请使用 login 命令` | 先执行 `python3 basement/scripts/main.py login` 完成授权 |
| `权限拒绝：... 不在 /AI空间/智能体云盘助手 及其子目录下` | 官方 Skill 只允许写沙箱目录；本挂载的根就是沙箱根，越界路径会被拒绝 |
| 读文件报 `无法确认 pending-xxx 的云盘路径` | 写回尚未完成；正常路径已同步等待，若仍出现请查看日志中的「写回上传失败」 |
| `ls` 看不到刚建的文件 | 云盘索引延迟，几秒后重试；本层有宽限期不会误删，但其它客户端需等索引 |
| 卸载提示 `Device or resource busy` | 仍有进程在使用挂载点，先 `cd` 出去或用 `lsof` 找到占用进程 |
| `Transport endpoint is not connected`（`ENOTCONN`） | 挂载进程被强杀后留下的失效 FUSE 挂载。常见于**在挂载点目录里**执行 `stop`/`restart`：卸载遭遇 `EBUSY`、systemd 随后强杀进程。先 `cd` 出去，再 `fusermount3 -uz /mnt/mcloud` |
| 服务反复重启并报 `FileExistsError: [Errno 17] File exists` | 同上，挂载点是失效挂载，`mkdir(exist_ok=True)` 会失败。新版启动时会自动 `fusermount3 -u`/`-uz` 清理残留；手工处理同样是 `cd` 出去 + `fusermount3 -uz /mnt/mcloud` |
| `systemctl --user stop` 后 `ps` 里还有进程 | 卸载被占用时会等 `TimeoutStopSec`（默认 20s）再强杀；确认没有 shell 停在挂载点里 |
| 上传大文件很慢 | 多段为按序上传，速度受带宽限制；可调 `MCLOUD_MAX_PARTS` 控制分片数量 |

设置 `MCLOUD_LOG=DEBUG`（配合真机脚本）可看到更详细的调用过程。

---

## 🗺 项目状态

- [x] 第一阶段：SDK 集成、鉴权、限流退避、沙箱与回收站路由
- [x] 第二阶段：三向映射、元数据 TTL 缓存、读缓存与 LRU
- [x] 第三阶段：只读挂载、写回缓存与上传、回收站删除
- [x] 第四阶段：崩溃恢复、POSIX 压测、systemd 打包
- [x] 加固：目录列举修复、异步 release 同步、索引最终一致性、多段上传、限流接入、死代码清理
- [x] v0.3.0 工程化：`src/` → 可安装包 `mcloud_fuse`、`mcloud-fuse` 命令入口与 `--version`、
      版本单一来源、GitHub Actions CI（ruff + pytest + 覆盖率门槛）、
      修复读缓存 LRU 淘汰与在途读的竞态（读句柄持有 fd + 缓存条目钉住/延迟删除）

已在真实云盘验证：官方 CLI 与本地列举一致、`cat` 字节一致、`mkdir` / 写入 / 覆盖写 / 删除，
756MiB 文件多段上传（133s）与空缓存下载（21s，sha256 一致）。

后续可做：回收站可视化与一键清理、`rename` 语义增强、`allow_other` 多用户与文件锁支持、Grafana 风格指标。

---

## 📄 License

本项目采用 [MIT License](LICENSE)。

## 🙏 致谢

- 云盘能力来自中国移动云盘官方 Agent Skill（`cm-cloud-manage`），本仓库不包含其代码。
- 文件系统层基于 [`pyfuse3`](https://github.com/libfuse/pyfuse3) 与 [`trio`](https://trio.readthedocs.io/)。
