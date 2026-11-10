# mcloud-fuse

> 把中国移动云盘（yun.139.com）挂载成 Linux 本地目录，用 `ls` / `cp` / `vim` / `rsync` 直接操作。

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20FUSE3-lightgrey)
![Status](https://img.shields.io/badge/status-beta-orange)

基于 [`pyfuse3`](https://github.com/libfuse/pyfuse3) + [`trio`](https://trio.readthedocs.io/)。
v0.4.0 换成网页端 `/file/*`、`/recyclebin/*` 接口，因此 `rm` 是真删除、目录可以改名、
跨目录移动是原语，不再受 AI 沙箱限制。

> **风险提示**：Web 端接口是第三方逆向接口，不在厂商授权范围内。账号风控与接口漂移
> 风险由使用者承担。启用前请阅读 [ADR-0002](docs/adr/0002-web-api-optin.md)。

---

## 30 秒上手

需要 Linux、Python 3.10+、FUSE3（`/dev/fuse` 与 `fusermount3`），以及一个已登录
`yun.139.com` 的浏览器。

```bash
# 1. 系统依赖
sudo apt-get install -y fuse3 libfuse3-dev

# 2. 安装（在仓库根目录）
python3 -m venv .venv && . .venv/bin/activate
pip install -e .

# 3. 首次设置
mcloud-fuse
```

`mcloud-fuse` 第一次运行会打开**全屏设置向导**：粘贴 token、选挂载点、写配置、
可选安装 `systemd --user` 服务。设置完成后再运行一次 `mcloud-fuse` 就是挂载。

已经 clone 好仓库、想一条命令装完：

```bash
./scripts/bootstrap.sh --deps
```

它会建 venv、装依赖，然后交给同一个设置向导。

---

## 首次设置向导

```
┌ mcloud-fuse 首次设置 ───────────────────────────── 步骤 2/7 ┐
│                                                              │
│  访问令牌                                                    │
│  令牌等同于账号访问权，只保存在本机 0600 文件里。            │
│                                                              │
│  › 使用已保存的令牌                                          │
│    账号 138****8000；2026-12-02 到期（剩余 29.8 天）         │
│    重新粘贴令牌                                              │
│    从浏览器重新复制 Authorization，或整段 cURL               │
│                                                              │
│  ↑↓ 选择   Enter 确认   Esc 上一步   q 退出                  │
└──────────────────────────────────────────────────────────────┘
```

| 步骤 | 会问什么 | 默认 |
| --- | --- | --- |
| 1 | 访问令牌：浏览器里的 `Authorization: Basic ...` 或整段 cURL | 已有则复用 |
| 2 | 挂载点：云盘内容出现在哪个目录 | `~/mnt/mcloud` |
| 3 | 云盘范围：整个云盘，或某个目录的路径（如 `/Photos/2026`） | 整个云盘 |
| 4 | 缓存目录：读缓存与写回暂存 | `/tmp/cmcloud-fuse` |
| 5 | 删除方式：永久删除 / 移入回收站 | 永久删除 |
| 6 | 覆盖写冲突：另存副本 / 报错 | 另存副本 |
| 7 | 启动方式：systemd 用户服务 / 仅保存配置 | 安装服务 |

**怎么拿 token**：

1. 打开 <https://yun.139.com/> 并确认已登录；
2. `F12` → **Network** → 过滤 `file/list` → 点任意一条请求；
3. 右侧 **Request Headers** → 复制 `Authorization: Basic ...` 整行；
   或右键请求 → **Copy → Copy as cURL**。

粘贴时按 `Ctrl-D` 结束。向导只显示脱敏账号与到期时间，不显示 token 明文。
不要复制 Cookie 里的 `skey` / `auth_token` / `ud_id`，它们不是 Authorization。

已有 token 时，向导会多一个**「尝试自动续期」**：调用客户端已有的
`tellin/authTokenRefresh.do`，不重新粘贴、不读浏览器 Cookie。续期被拒绝
（接口可能要求设备指纹）时会给出原因，再退回粘贴。

**token 会自动续期，也会在 `systemctl` 里报剩余时间**：

- 服务运行期间每 12 小时检查一次 token；剩余不足 15 天时调用续期接口，
  新 token 仍以 0600 原子写回，不写日志、不进服务单元；
- `systemctl --user status mcloud-fuse` 的 `Status:` 一行会显示脱敏账号与剩余天数：

  ```text
  Status: "mounted on /home/you/mnt/mcloud | token 138****8000 剩余 29.8 天（2026-12-02 12:00 到期）"
  ```

- **默认开启，推荐保持开启**：服务 / 挂载运行期间自动续期，省去每隔一个月
  回浏览器复制 token；确实不想让空闲挂载续期时设
  `MCLOUD_TOKEN_AUTO_REFRESH=0`（请求时的续期仍由客户端按需处理）；
- 手动查看 / 续期：`mcloud-fuse token status`、`mcloud-fuse token refresh`。
  续期不会涉及账号密码，也不会读取浏览器 Cookie；失败就回到浏览器重新复制。

> 没有已保存 token 时**仍然需要显式粘贴一次**：自动续期只延长已有 token 的
> 有效期，不会凭空获取账号凭据，也不会读取浏览器 Cookie / 本地存储。

以下情况仍需要回浏览器复制一次：

- 服务 / 挂载停用时间超过 token 剩余寿命（例如笔记本关机一个月）；
- 续期接口拒绝（设备指纹、风控或接口漂移）；
- 换账号、token 被吊销，或手动删掉了 token 文件。

非交互环境用行模式：

```bash
mcloud-fuse setup --no-tui --yes \
  --mountpoint ~/mnt/cloud --cache-dir ~/.cache/mcloud-fuse --no-service
```

已有 token 时也可以只尝试续期再进入提问：`mcloud-fuse setup --no-tui --refresh-token`。

---

## 懒加载：不访问就不下载

挂载点空闲时，本地只有**元数据映射**，没有云盘文件的副本：

- `ls` / `stat`：只拉当前目录的元数据（带 TTL 缓存），不下载内容；
- `open()`：只建一个大小已知的稀疏空壳，不下载；
- `read()`：才按 4MiB 分片发起 Range 请求，只保留读过的块；
- `write()`：才创建写回暂存 `.wb`，`close` / `fsync` 时才上传；
- 没有后台预取、没有全盘同步、没有启动时列举整棵树。

所以「挂上一个 1TB 云盘」不会先占 1TB 本地空间；只有真正读写的文件才产生缓存。
`MCLOUD_LAZY_READ=0` 可以退回旧的整文件下载行为（不推荐）。

---

## 日常使用

| 命令 | 作用 |
| --- | --- |
| `mcloud-fuse` | 挂载（未配置或 token 失效时自动进入向导） |
| `mcloud-fuse mount` | 显式挂载；可临时覆盖 `--mountpoint` / `--root` / `--cache-dir` |
| `mcloud-fuse unmount` | 卸载；必要时先停 systemd 用户服务 |
| `mcloud-fuse status` | 配置、挂载状态、token 到期时间、服务状态 |
| `mcloud-fuse token status \| refresh` | 查看 token 剩余时间 / 尝试自动续期 |
| `mcloud-fuse setup` | 重新运行设置向导 |
| `mcloud-fuse journal list \| forget \| gc \| retry` | 写回日志查看 / 清理 / 补传 |
| `mcloud-fuse doctor` | 环境、网络、配额自检 |
| `mcloud-fuse trash list \| restore \| empty` | 回收站查看 / 恢复 / 清空 |

```bash
mcloud-fuse                 # 挂载，前台运行；Ctrl+C 卸载
mcloud-fuse status          # 看是否挂上了
ls ~/mnt/mcloud             # 像本地目录一样用
mcloud-fuse unmount         # 卸载

# 只挂云盘里的某个目录（不用翻 Network 找 parentFileId）
mcloud-fuse mount --root /Photos/2026
```

`Ctrl+C` 是优雅卸载：会等删除队列提交完、补传未完成的写回，再 `pyfuse3.close()`。
不要在挂载点目录里执行 `unmount`，先 `cd` 出去。

---

## 配置文件

路径：`~/.config/mcloud-fuse/config.json`（0600，向导自动生成）

```json
{
  "mountpoint": "/home/you/mnt/mcloud",
  "cache_dir": "/home/you/.cache/mcloud-fuse",
  "root": "/",
  "rm_mode": "delete",
  "conflict": "copy",
  "install_service": true,
  "version": 1
}
```

`root` 可以是 `/`、`/Photos/2026` 这样的云盘路径，也可以是旧版直接填的 fileId
（包括 `/synbox` 这类以 `/` 开头的特殊值；路径解析失败时会回退为 fileId）。
命令行 `--root` 只覆盖本次挂载，不会写回配置；想长期只挂某个目录，重新运行
`mcloud-fuse setup` 改第 3 步。

环境变量优先于配置文件，常用覆盖：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MCLOUD_CACHE_DIR` | `/tmp/cmcloud-fuse` | 读缓存 / 写回文件（tmpfs） |
| `MCLOUD_MAX_CACHE_MB` | 自动（128~512） | 读缓存上限 |
| `MCLOUD_WRITE_BACK_DIR` | `<缓存目录>/writeback` | 待上传文件；指到持久盘可跨重启保留 |
| `MCLOUD_MAX_WRITE_BACK_MB` | 自动（256~1024） | 待上传文件上限；超过返回 ENOSPC |
| `MCLOUD_MIN_FREE_MB` | `64` | 缓存目录保留空间 |
| `MCLOUD_LAZY_READ` | `1` | 所有文件默认按需读；`0` 退回整文件下载 |
| `MCLOUD_META_CACHE_ENTRIES` | `4096` | 元数据缓存条数上限 |
| `MCLOUD_WEB_TOKEN_FILE` | `~/.config/mcloud-fuse/web_token` | token 文件（0600） |
| `MCLOUD_TOKEN_AUTO_REFRESH` | `1` | 服务运行时是否在空闲时主动续期；`0` 只报告不续期 |
| `MCLOUD_TOKEN_STATUS_INTERVAL_SEC` | `43200` | 向 systemd 刷新 token 状态的间隔；`0` 只在启动时报一次（不后台续期） |
| `MCLOUD_JOURNAL_DB` | `~/.local/state/mcloud-fuse/writeback-<hash>.sqlite3` | 写回日志（SQLite，持久化；按缓存目录分库） |
| `MCLOUD_STATE_DIR` | `~/.local/state/mcloud-fuse` | 状态目录（放日志） |
| `MCLOUD_JOURNAL_SYNC` | `NORMAL` | SQLite 同步级别；payload 放持久盘且要抗断电时设 `FULL` |
| `MCLOUD_RESCUE_ON_SHUTDOWN` | `1` | 关机时把 tmpfs 里的待上传文件抢救到持久盘 |
| `MCLOUD_RESCUE_MAX_MB` | `1024` | 单次关机抢救总量上限 |
| `MCLOUD_RESCUE_FILE_MAX_MB` | `512` | 单文件超过该大小直接跳过 |
| `MCLOUD_RESCUE_TIMEOUT_SEC` | `20` | 抢救总超时，超时立即停止 |
| `MCLOUD_RESCUE_MIN_FREE_MB` | `256` | 目标盘保留空间 |
| `MCLOUD_STOP_EXTEND_MAX_SEC` | `30` | 关机时向 systemd 申请延长停止超时的上限 |
| `MCLOUD_RM_MODE` | `delete` | `delete`=真删；`trash`=进回收站 |
| `MCLOUD_CONFLICT` | `copy` | 覆盖写冲突：`copy` 另存副本；`error` 返回 ESTALE |
| `MCLOUD_API_MAX_CONCURRENT` | `3` | API 最大并发 |
| `MCLOUD_API_RPS` | `5` | 每秒请求上限 |
| `MCLOUD_EAGER_DOWNLOAD_LIMIT` | `0` | `0`=所有文件按需读；>0 时只对大文件按需读 |
| `MCLOUD_RANGE_CHUNK` | `4194304` | 按需读分片大小 |
| `MCLOUD_WEB_PART_SIZE` | `104857600` | 上传分片大小 |
| `MCLOUD_LOG` | `INFO` | 设为 `DEBUG` 看详细调用 |

完整列表见 `mcloud_fuse/config.py`；临时调试可以直接
`MCLOUD_CACHE_DIR=/tmp/cache mcloud-fuse mount`。

---

## 写回日志与重启：为什么日志不在 `/tmp`

读写缓存和待上传文件都放在 `/tmp/cmcloud-fuse`（tmpfs，内存盘）：

```text
/tmp/cmcloud-fuse             读缓存，丢了可以重新下载
/tmp/cmcloud-fuse/writeback   待上传文件（.wb），重启后会消失
```

但「哪些文件还没上传」这份**元数据**不能也放 tmpfs，否则进程崩溃后连线索都没了。
所以 v0.4.0 拆成两层：

```text
~/.local/state/mcloud-fuse/writeback-<hash>.sqlite3   SQLite 日志（几 KB，持久化）
/tmp/cmcloud-fuse/writeback/*.wb               文件内容（大，tmpfs）
```

- 进程崩溃 / 被 kill：payload 还在，下次启动按 SQLite 日志补传；
- **正常关机（SIGTERM）**：先通过 `sd_notify` 向 systemd 申请最多 30 秒停止延时，
  再把 `pending` / `uploading` 的 payload 抢救到
  `~/.local/state/mcloud-fuse/rescue/`；下次启动从持久盘补传。`building` 半成品不抢救；
- 整机重启 / 断电：payload 没了，日志条目会标成 `lost`，`mcloud-fuse status`
  和 `doctor` 会显示数量，不会假装补传成功；
- 想连整机重启也保住待上传内容：把 `MCLOUD_WRITE_BACK_DIR` 指到持久化磁盘
  （例如 `~/.cache/mcloud-fuse/writeback`），日志仍然单独放 state 目录。

这是「内存换速度」的明确取舍，不是静默行为。

### 实际使用中要注意

- **待上传文件不参与读缓存 LRU**：`cp` 一个大文件进挂载点时，`.wb` 会一直占着
  tmpfs，直到 `close` 后上传完成；tmpfs 被写满会报 `ENOSPC`，严重时影响整机内存。
  大文件写入建议把 `MCLOUD_CACHE_DIR` 指到持久化磁盘。
- **补传在挂载后后台进行**：进程崩溃后若有大量待传文件，挂载点会先可用，
  补传在后台逐个进行；`status` 的“待传”数量会逐步下降。
- **只有 `fsync` / `close` 之后才进入可恢复状态**：只 `write` 没 `fsync` 就崩溃，
  按 POSIX 不保证数据；系统会把条目标成 `lost`，而不是上传半成品。
- **默认就是懒加载**：`open()` 不下载内容，`read()` 才按 4MiB 分片拉取；`ls` / `stat`
  只访问元数据。想恢复旧的整文件下载，设 `MCLOUD_LAZY_READ=0`。
- **写回有硬上限**：默认 256~1024MiB（随内存），超过直接 `ENOSPC`，不会把 tmpfs
  写满；大文件写入前可以先调大上限或把写回目录指到磁盘。
- **多账号要分开缓存目录**：不同账号共用同一个 `/tmp/cmcloud-fuse` 可能发生
  fileId 撞车；每个账号用独立的 `--cache-dir`（日志也会按目录自动分库）。
  同一缓存目录同一时间只允许一个挂载进程（`flock` 锁），第二个会直接报错。

### 写回日志操作

```bash
mcloud-fuse journal list                 # 待传 / building / lost
mcloud-fuse journal list --lost --json   # 只看看丢了什么
mcloud-fuse journal forget --lost --purge  # 清掉 lost 记录和 payload
mcloud-fuse journal gc                   # 清理孤儿 .wb 和过期 building
mcloud-fuse journal retry                # 未挂载时手动补传 pending
```

`forget` / `gc` 默认拒绝在挂载运行时执行（避免删掉在途写回）；确认要操作时加
`--force`。

---

## 常见问题

| 现象 | 处理 |
| --- | --- |
| `未找到 Web token` | 运行 `mcloud-fuse setup`，重新粘贴 token |
| `token 已过期` / 401 | 浏览器刷新云盘页面后重新复制；确认是 `file/list` 的 Authorization |
| `token 续期被拒绝` | 续期接口可能需要设备指纹，直接重新导入 token |
| 找不到 `fusermount3` | `sudo apt-get install -y fuse3 libfuse3-dev` |
| `Device or resource busy` | 有进程停在挂载点，先 `cd` 出去再 `mcloud-fuse unmount` |
| `Transport endpoint is not connected` | 进程被强杀后的失效挂载：`fusermount3 -uz <挂载点>` |
| `df -i` 显示 `-` | 云盘没有 inode 概念，`statfs` 按约定报 0 |
| `ls` 很慢 | 首次列举要拉全目录；检查 `MCLOUD_API_RPS` 与网络 |
| 服务没起来 | `systemctl --user status mcloud-fuse`；`mcloud-fuse doctor` |

自检顺序：`mcloud-fuse doctor` → 看 token 是否有效 → 看 `/dev/fuse` 权限 →
`mcloud-fuse status` 看服务与挂载点。

---

## 安全与边界

- **token 是账号全权凭据**：只放本机 0600 文件；泄漏等于账号被接管。本项目不接触
  账号密码，也不会把 token 写进日志或服务单元。
- **`rm` 默认是永久删除**：`/file/batchDelete` 不进回收站。需要可恢复时设
  `MCLOUD_RM_MODE=trash`，或改配置里的 `rm_mode`。
- **无随机写**：上传接口要求先给出总大小，所以 `write` 先落本地、`release` 整文件上传。
- **tmpfs 重启会丢待上传内容**：日志能保留，但 payload 在 `/tmp`；需要跨重启保住
  写回时改 `MCLOUD_WRITE_BACK_DIR`。
- **上传按序**：分片必须按 partNumber 顺序发送；不支持跨进程断点续传。
- **POSIX 语义不完整**：`chmod` / `chown` / 时间戳被忽略；不支持 xattr、符号链接、
  硬链接、`allow_other`。
- **锁只在本机有效**：`flock` / `fcntl` 由内核在本地维护；跨挂载、跨主机、手机 App /
  网页端都不受约束。
- **每人一个账号**：一个挂载对应一个账号；共享只读 `allow_other` 不在当前计划。

---

## 更多文档

- [文档索引](docs/README.md)：按入门 / 设计 / 运维 / 历史分类
- [架构详解](docs/architecture.md)：FUSE 回调、缓存、写回、映射层
- [TUI 设计](docs/tui.md)：首次设置向导的页面、按键与降级行为
- [开发指南](docs/development.md)：环境、测试分层、新增 FUSE 回调
- [性能基线](docs/performance.md)：大目录、上传、删除、冷/热读的实测数字
- [端点契约](docs/contracts.md)：真实响应样例与漂移检查
- [Web API 分析](docs/web-api-analysis.md)：端点、鉴权、签名、风险
- [ADR-0002](docs/adr/0002-web-api-optin.md)：为什么启用 Web API、如何隔离
- [ADR-0003](docs/adr/0003-multiuser-and-locking.md)：多用户与文件锁
- [发布记录](docs/release-notes.md)

---

## 测试

```bash
python3 -m pytest tests -q                    # 离线单测 / 回归
python3 -m pytest tests -q --cov=mcloud_fuse

python3 tests/integration/fuse_mount_smoke.py # 内存假云盘 + 真实内核挂载
python3 tests/integration/posix_stress.py
```

真实云盘与契约检查：

```bash
python3 scripts/web139_smoke.py --list-only
python3 scripts/check_contracts.py --live
```

---

## License

MIT。云盘接口的使用请自行遵守服务条款。

协议细节参考 [OpenList/AList 的 139 驱动](https://github.com/AlistGo/alist/tree/main/drivers/139)；
文件系统层基于 [`pyfuse3`](https://github.com/libfuse/pyfuse3) 与
[`trio`](https://trio.readthedocs.io/)。
