# Release Notes

## v0.4.0：后端换成 yun.139.com Web API

这是一次**破坏性后端替换**，不是增量版本。

### 能力

- `rm` 真删除：`/file/batchDelete`（`MCLOUD_RM_MODE=trash` 可退回回收站）；
- 删除/移动会轮询 `/task/get` 到 `Succeed` 再返回，避免异步任务造成「删不掉的幽灵目录」；
- `statfs` 接入 `/disk/quota/detail`：`df -h` 显示真实容量，删除后可用空间回升；
- 新增 `mcloud-fuse trash list / restore / empty`：回收站查看、恢复与清空；
- `statfs` 的 inode 数改为按约定报 0（未知），不再编造 10 亿上限；
- 新增端点契约：`tests/contracts/*.json` + `scripts/check_contracts.py`，
  每周真机检查字段漂移（`.github/workflows/contract-live.yml`）；
- 覆盖写冲突检测：提交前比对远端 `updatedAt`，默认把本地版本另存为
  `name.conflict-<host>-<ts>`（`MCLOUD_CONFLICT=copy`），可切 `error` 返回 `ESTALE`；
- 多用户定为「每人一个账号」：systemd --user 模板（v0.4.0 起由
  `mcloud_fuse/service.py` 生成），不做共享写 ACL / `allow_other`；
- 文件锁确认为内核本地 advisory：`flock` / `fcntl` 单挂载内可用，跨主机无效；
- 性能：页大小 500、去掉缩略图请求、删除批处理（`rm` 20 文件 13.8s → 0.6s）；
- 新增 `scripts/bootstrap.sh` 一键部署、`mcloud-fuse doctor` 自检、`mcloud-fuse setup` 向导；
- **首次设置改为全屏 TUI**（标准库 `curses`，无新依赖）：7 步向导完成 token、
  挂载点、云盘范围、缓存、删除语义、冲突策略与服务安装；有 `--no-tui` / `--yes`
  行模式，非 TTY 自动降级。设计见 [docs/tui.md](tui.md)；
- **配置持久化**：向导写 `~/.config/mcloud-fuse/config.json`（0600），之后
  `mcloud-fuse` 不带参数即可挂载，不再需要背 `--mountpoint` / `--root`；
- 新增 `mcloud-fuse status` / `mcloud-fuse unmount`；`mcloud-fuse mount` 仍可用
  命令行参数临时覆盖配置；
- `systemd --user` 单元由 Python 直接生成（`mcloud_fuse/service.py`），不再依赖
  `scripts/install_user_service.sh` 与 `setup.env`；服务单元不含 token 明文；
- token 解析/脱敏/0600 落盘收敛到 `mcloud_fuse/tokens.py`，向导、`status` 与
  `scripts/import_web_token.py` 共用；
- 写回日志从 JSON 换成 **SQLite**（`~/.local/state/mcloud-fuse/writeback-<hash>.sqlite3`），
  与 `/tmp/cmcloud-fuse/writeback` 的 payload 分离：进程崩溃可恢复，tmpfs 重启后
  payload 丢失的条目标记 `lost` 并在 `status` / `doctor` 中显示；
- 读缓存与待上传文件仍放 `/tmp/cmcloud-fuse`（tmpfs）；需要跨整机重启保住写入时，
  可把 `MCLOUD_WRITE_BACK_DIR` 指到持久化磁盘；
- **默认全量懒加载**：`open()` 不再下载内容，`read()` 才按 4MiB Range 拉取；
  `MCLOUD_LAZY_READ=0` 才回到整文件下载。`ls` / `stat` 只访问元数据；
- **资源硬上限**：读缓存自动 128~512MiB、写回 256~1024MiB，另有 64MiB 保底剩余空间；
  超过返回 `ENOSPC`，不再可能把 tmpfs 写满；元数据缓存加 LRU 上限（默认 4096 条）；
- **补传后台化**：挂载先就绪，未上传文件在后台补传，不再阻塞 `pyfuse3.init`；
- **关机遇失抢救**：收到 SIGTERM 时先通过 `sd_notify` 的
  `EXTEND_TIMEOUT_USEC` 向 systemd 申请一次有上限的停止延时（默认最多 30 秒），
  再在卸载前把 `pending` / `uploading` 的 payload 从 tmpfs 复制到
  `~/.local/state/mcloud-fuse/rescue/`；默认总上限 1GiB、单文件 512MiB、20 秒。
  下次启动从持久盘补传。`building` 半成品不抢救，断电 / SIGKILL 仍无法抢救；
- systemd 单元改为 `Type=notify`：挂载真正就绪后才向 systemd 发 `READY=1`，
  `systemctl start` 不再“进程起来但挂载点还没好”就返回；
- **缓存目录互斥**：`flock` 锁住同一 cache dir，两个挂载进程不会互相覆盖缓存；
- 新增 `mcloud-fuse journal list / forget / gc / retry`：查看、清理 `lost`、回收孤儿
  payload、未挂载时手动补传；
- `status --cache-dir` 可以显式指定要查的缓存目录；`doctor` 会显示缓存文件系统类型；
- `rename` 支持目录：`/file/update`，不再 `ENOSYS`；
- 跨目录移动是原语：`/file/batchMove`，不再复制双份；
- 全盘直读，删除 `.MCloudHydrate` 懒搬运与沙箱边界；
- 目录枚举改用 `/file/list` 游标分页，不再依赖搜索索引。

### 工程

- 新增隔离的 L3 协议层 `client/web139.py`（token/续期/签名/上传/冲突处理）；
- `client/wrapper.py` 改为后端适配器，保留原有 FUSE/缓存接口；
- token 导入脚本与真机冒烟脚本；
- Skill 通道整体归档到 `.old-version/`，不参与打包、CI 与测试；
- 版本号升至 `0.4.0`。


## v0.3.0

把 v0.2.1 之后的三块工作（工程化、读路径修复、两项行为改进）合并为同一个版本发布。

### 打包与入口：从「clone 下来改源码跑」到「可安装的包」

- `src/` 改为可安装包 `mcloud_fuse`，内部统一绝对导入；顺带消除 `src/fuse/`
  与 PyPI `fuse` 包的命名冲突。
- 新增命令入口：`pip install -e .` 后 `mcloud-fuse --mountpoint ...`，
  等价写法 `python3 -m mcloud_fuse`；新增 `--version`。
- `pyproject.toml` 补齐 `[build-system]`、`[project.scripts]`、license、classifiers、
  urls 与 `dev` extra；`requirements*.txt` 改为转发 pyproject，依赖清单不再双份维护。
- 版本号单一来源 `mcloud_fuse/version.py`，pyproject 用 `dynamic = ["version"]` 读取。
- 官方 Skill 的定位集中到 `mcloud_fuse/basement.py`，支持 `MCLOUD_BASEMENT_DIR`；
  找不到时提示运行 `scripts/setup_basement.py`，而不是抛 `ModuleNotFoundError`。
- systemd 单元改为 `WorkingDirectory=... + -m mcloud_fuse`；真机脚本同步更新。

### 修复：读缓存的并发缺陷

- **LRU 淘汰会把正在读的文件删掉**：`read()` 原先每次按路径重新 `open`，
  而 LRU 淘汰直接 `unlink`。典型触发是「`open` 之后隔一会儿才读」的程序：
  期间另一个文件的下载触发淘汰，读操作就报 `EBADF`/`ENOENT`。
  现在 `open(O_RDONLY)` 即持有 fd、`read` 走 `os.pread`，同时缓存条目在读句柄
  存续期间被「钉住」，淘汰改为「摘映射 + 延迟删除」，最后一个句柄关闭才真正 unlink。
- **同一 inode 并发打开会互相覆盖句柄**：读句柄的 fh 改为独立分配（从 `2**32` 起），
  不再复用 inode；否则 `release` 可能关掉别人的 fd、或泄漏 fd。

### 新功能：大文件按需（Range）读取

- 超过 `MCLOUD_EAGER_DOWNLOAD_LIMIT`（默认 256MiB）的文件在 `open()` 时不再整文件下载，
  而是建一个大小已知的本地空壳，`read()` 按 `MCLOUD_RANGE_CHUNK`（默认 4MiB）对齐拉取缺口，
  重复读不重下。读大文件的头尾（日志、媒体）不再付出整文件下载 + 整文件磁盘占用。
- **服务端不支持 Range 时自动降级**：若直链忽略 `Range` 返回 200，本次响应体整文件落盘
  并立即升级为完整缓存，行为与旧版一致，不会退化成「每次读都重下整文件」。
- **部分文件绝不冒充完整文件**：部分内容只对 `ensure_range` 可见；覆盖写、`truncate`、
  `rename`（需要整文件上传）一律先经 `ensure_complete` 补齐。
- **宁可报错也不猜**：`Content-Range` 起点对不上、总长与元数据 `size` 不符、
  200 的整文件长度不足时一律报错，避免留下「被截断却看起来完整」的副本被上传回云盘。
- 新增 `scripts/check_range_support.py`：探测下载直链是否真的支持 Range。
- 新增环境变量 `MCLOUD_EAGER_DOWNLOAD_LIMIT`（`0` = 关闭按需读）、`MCLOUD_RANGE_CHUNK`、
  `MCLOUD_RANGE_TIMEOUT`。

### 行为改进：删除不再白烧一倍云盘容量

- 核实了整个官方 Skill 的接口面（只有 create / search / copy / download 一类），
  **不存在任何删除或回收站接口**：原对象永远删不掉。
- 旧实现还会把对象云内复制一份进 `.TrashBin`，于是被删对象占**双倍**容量；
  目录移动更会 `batch_copy` **整棵子树**；而 `.TrashBin` 属于隐藏目录，
  用户既看不到也删不掉，副本收益为零。
- 现在 `unlink` / `rmdir` / 改名 / 移动改为**只登记墓碑 + 本地摘除**（默认
  `MCLOUD_TRASH_COPY=0`），连 `.TrashBin` 目录都不创建；`=1` 可恢复旧的复制行为。
- 副作用是恢复变得可行：原对象从未移动，移除墓碑即可让它回来。
- 能力边界写进文档：想真正腾容量、想清理历史 `.TrashBin` 副本，只能在官方 App / 网页端删除。

### 测试与 CI

- 新增 GitHub Actions：`ruff` + Python 3.10~3.13 矩阵跑离线用例；另有
  「覆盖率 + 真实内核挂载」job（`/dev/fuse` + `fusermount3`）。
- 覆盖率把离线单测与真实挂载脚本合并统计，门槛 `fail_under = 70`（当前 73%）。
- CI 额外用两种极端参数跑真实内核压测：`MCLOUD_STRESS_CACHE_MB=1`（强制 LRU 淘汰，
  回归「淘汰删掉正在读的文件」）与 `MCLOUD_STRESS_EAGER_LIMIT=1024`（全量走 Range 按需读）。
- `posix_stress.py` 新增：顺序读全部根文件、16 路并发读字节数校验，以及**确定性复现淘汰竞态**
  的「打开后延迟读取」阶段——该用例在修复前代码上会失败、修复后通过。
- 新增离线用例：钉住条目不被淘汰、嵌套句柄最后释放才删、条目作废后仍可读完、
  并发打开句柄独立、按需读只取缺口/降级/越界/写路径必须补齐，
  以及按需读 HTTP 层的 206/200/416/短包/总长不符/Content-Range 错位/5xx 七个分支。
- `ruff` 检查纳入 `scripts/`，顺带修掉 `scripts/setup_basement.py` 的未用导入等问题；
  修正三个文档要求直接执行、却缺可执行位的脚本。

### 已验证

- `ruff check` 通过；`pytest tests -q` 69 项通过（2 项 live 用例按环境变量跳过）。
- `pip install -e .` 后 `mcloud-fuse --version` 正常；wheel 包含全部子包与 entry_points。
- 真实内核挂载：`fuse_mount_smoke.py` 与 `posix_stress.py`（默认 / 1MiB 缓存强制淘汰 /
  全量 Range + 16 路并发）全部通过。
- 历史基线仍有效：真实云盘上官方 CLI 与本地列举一致、`cat` 字节一致、
  756MiB 多段上传（133s）与空缓存下载（21s，sha256 一致）。

### 升级注意

- 调用方式变化：`python3 src/main.py ...` → `mcloud-fuse ...` 或 `python3 -m mcloud_fuse ...`。
- 删除语义变化：默认不再产生 `.TrashBin` 副本；旧版本留下的副本只能在官方 App 清理。
- 建议登录后跑一次 `python3 scripts/check_range_support.py`，确认按需读是否真正生效
  （不支持时功能不受影响，只是没有收益）。

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
