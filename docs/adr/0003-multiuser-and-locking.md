# ADR-0003：多用户与文件锁的设计

- 状态：**已接受**（Accepted，2026-10-28；2026-11-19 修订：落地只读 `allow_other`）
- 日期：2026-10-28
- 相关：[ADR-0002](0002-web-api-optin.md)、[Web API 分析](../web-api-analysis.md)

## 1. 结论摘要（已定稿）

1. **采用「每人一个账号、各自挂载」**（下文的形态 A）。一个挂载只持有一个
   Web token，也只代表一个云盘账号；不实现「一个挂载、多个身份」。
2. **接受「权限只在本机有效」**。因此不实现共享写的本地 ACL 覆盖层；
   `allow_other` 作为「共享只读」的可选项已经落地（opt-in、默认关闭，
   见 §8 修订记录），共享写仍然不做。
3. **文件锁不在 FUSE 层实现**。pyfuse3 3.5 没有暴露 `lock` / `flock` 回调；
   内核把锁保留在本地，`flock` 与 `fcntl` 在单机单挂载内已经可用（已实测）。
   锁是**本机 advisory**，不能跨主机、也不能约束手机 App / 网页端。
4. **覆盖写默认 `copy`**：提交前比对远端 `updatedAt`；发现别人改过，就把本地
   版本另存为 `name.conflict-<host>-<ts>`，远端原文件保留。`MCLOUD_CONFLICT=error`
   可改为 `ESTALE` 失败并保留写回缓冲。
5. 共享写（形态 C）与多租户 broker（形态 D）**不进入实现计划**，仅在需求变化时
   重新评估。

## 2. 约束（为什么不能照搬本地文件系统）

| 约束 | 影响 |
| --- | --- |
| 一个 token 只对应一个账号 | 一个挂载无法按本地 uid 切换云端身份 |
| Web API 没有 ACL / chmod / chown | 云端权限只能在本机模拟 |
| Web API 没有 open / flock / fcntl | 跨主机、跨客户端的锁不存在 |
| 写入是「整文件上传」，`updatedAt` 由服务端生成 | 两个写入者会互相覆盖，需要应用层检测 |
| `rename`/`move` 是异步任务 | 锁的粒度不能依赖服务端事务 |
| `pyfuse3 3.5.0` 无 `lock`/`flock` 回调 | 不能把 POSIX 锁转发到用户态 |
| 当前 attrs 固定 `uid/gid = 挂载进程`、`0644/0755` | 多用户写共享目录目前不可行 |

实测记录（2026-10-28，pyfuse3 3.5.0）：

```text
$ flock -x file -c 'sleep 3' & flock -n file -c 'echo acquired'
second_rc=1                      # 第二个 flock 被阻塞

$ fcntl.lockf(LOCK_EX|LOCK_NB)   # 第二个进程
second blocked errno 11          # EAGAIN
```

说明：**即使不实现 FUSE 锁回调，内核也会在本地维护锁**；这正是我们需要的
「单机 advisory」语义，不需要也不应该在 pyfuse3 里伪造一套。

## 3. 多用户：可选形态与本次选择

### A. 每用户独立挂载（**本次采用**）

每个本地用户用自己的 token、自己的挂载点、自己的进程：

```text
alice  ~/mnt/cloud  ->  token A  ->  账号 A
bob    ~/mnt/cloud  ->  token B  ->  账号 B
```

- 优点：零共享状态；权限就是本地文件系统权限；一个用户崩溃不影响另一个；
  无需 `allow_other`、无需 root、无需本地 ACL。
- 缺点：不能共享同一个云盘目录；每个用户要各自导入 token。
- 适用：绝大多数「多用户」需求其实是「每人一个账号」。

### B. 共享一个账号，只读（v0.5.0 起已实现，opt-in）

挂载进程用专用服务账号（或 root）持 token，开启：

```text
-o allow_other,default_permissions
```

文件 `0644`、目录 `0755`，其他本地用户可读/可遍历，不能写。

- 前提：`/etc/fuse.conf` 里的 `user_allow_other`（当前宿主机是注释状态，
  所以非 root 用户挂载时 `allow_other` 会被拒绝；systemd 以 root 跑则可用）。
  向导会用一条 `sudo sh -c ...` 追加这一行，并检查从 `/` 到挂载点父目录的
  `o+x`（`$HOME` 为 `0700` 时其他用户进不去）。
- 适用：家庭媒体库、团队只读资料。
- 风险：token 必须只对服务账号可读；root 天然可读，威胁模型不覆盖 root。

### C. 共享一个账号，读写（未实现，明确不在当前计划内）

在 Phase 1 基础上增加本地元数据：

```text
cache_dir/local_acl.sqlite3
  file_id TEXT PRIMARY KEY
  uid INTEGER, gid INTEGER, mode INTEGER, updated_at INTEGER
```

行为：

- `create` / `mkdir` 用 FUSE `ctx.uid/gid` 与请求 mode（`mode & ~umask`）写入；
- `_to_attrs` 优先读覆盖层，没有记录时回退 `0644/0755` + 挂载进程 uid/gid；
- `setattr` 的 `chmod` / `chown` 更新覆盖层（`chown` 仅 root，按 POSIX）；
- 内核 `default_permissions` 依据这些 attrs 做真正的权限检查；
- 覆盖层丢失时回退默认权限，**不阻塞挂载**；
- 以 `fileId` 为键，rename/move 不丢权限。

必须写清楚的限制：

- 只在本机生效；网页端、手机 App、另一台机器看到的仍是「账号内全权」；
- 删除覆盖层/换机器后权限回到默认；
- root 与服务账号可绕过；
- 云端没有「文件属于谁」的概念，无法审计到具体本地用户（只能在本地日志里记）。

### D. 多租户 broker（不推荐）

一个守护进程持多个 token、按调用者切换账号、维护多套 namespace。理论上可行，
但会把 inode 缓存、目录树、写回、锁、错误处理全部变成多租户状态机，
安全边界也从一个进程退化成一张路由表。当前阶段不做。

## 4. 文件锁：分层设计

### 4.1 本机锁（已有，保持）

- 不实现 `lock` / `flock` 回调；依赖内核本地锁；
- 挂载参数保持 `default_permissions`；
- 文档明确：锁只在**同一个挂载内**有效，跨挂载/跨主机/跨 App 无效；
- 不做 mandatory locking（`-o mand`），FUSE 也不支持。

### 4.2 云端乐观并发（建议下一步实现）

本地锁管不住「另一台设备也在改同一个文件」。写入提交时做一次检查：

1. `open(O_WRONLY/O_RDWR)` 时记录基线：`base_updated_at`、`base_size`
   （必要时调 `/file/get` 拿最新，而不是只信 60s TTL 的本地缓存）；
2. `release` / `fsync` 提交前再调 `/file/get`；
3. 若 `updatedAt` 变了：
   - 默认 `MCLOUD_CONFLICT=copy`：把本地写回内容上传为
     `name.conflict-<host>-<时间戳>`，远端保持原样，日志告警，**不丢数据**；
   - `MCLOUD_CONFLICT=error`：`fsync` 返回 `ESTALE`，保留写回缓冲供重试；
4. 新建文件遇到同名：`O_CREAT|O_EXCL` 返回 `EEXIST`，不再走「auto_rename
   后删旧对象」的自动覆盖（当前行为在多设备场景下太激进）。

### 4.3 不做的事

- 不把 `flock` 转发到 Web API（没有对应端点）；
- 不做跨主机锁服务（需要额外守护进程与租约协议，超出当前项目边界）；
- 不承诺 SQLite / git 这类依赖严格锁的负载在共享写场景下安全。

## 5. 安全边界

| 项 | 设计 |
| --- | --- |
| token | 只对挂载进程的服务账号可读（0600）；`allow_other` 时其他用户不可读 |
| 挂载权限 | 始终保持 `default_permissions`；不允许 `allow_other` 但不带它 |
| 共享只读 | `allow_other` opt-in：`MCLOUD_ALLOW_OTHER=1` 或 `mount --allow-other`；attrs 固定 `0644/0755` |
| 共享写 | 不做；需本地 ACL 覆盖层（形态 C），仍在「不做」列表 |
| 世界可写 | 不提供 `0777` 快捷方式；共享写必须走 ACL 覆盖层 |
| root | 视为可信；root 可读 token、可绕过本地权限 |
| 云端 | 所有本地用户的操作都记在同一个云盘账号下，云端无本地用户审计 |

## 6. 实施顺序（已执行）

1. **写冲突检测**：`MCLOUD_CONFLICT=copy|error`，默认 `copy`；
2. **每用户挂载的 systemd user 模板**：v0.4.0 起由 `mcloud_fuse/service.py`
   生成（`mcloud-fuse setup` 安装），不再使用 shell 模板；
3. 本地 ACL：**不做**；`allow_other` 只读共享：**v0.5.0 已实现**（见 §8）；
4. 多租户 broker：**不做**。

## 7. 决策记录（2026-10-28）

- 多用户需求：**每人一个账号** → 采用形态 A，不做共享 ACL。
- 权限只在本机有效：**接受**。
- 覆盖写冲突：**默认 `copy`**（`MCLOUD_CONFLICT=copy`）。
- 文件锁：接受「本机 advisory」，不做跨主机锁。
- 未决但不在当前范围：共享写 ACL 覆盖层、多租户 broker。

## 8. 修订记录（2026-11-19）：只读共享落地

需求变化：使用者希望「挂载点放到 `/mnt` 让家里/团队其他人也能读」。这与
形态 A 的「每用户一个挂载」不冲突——一个挂载仍只持有一个 token，只是允许
其他本地 uid 通过同一个挂载读取。

### 8.1 做了什么

1. **挂载点可以是任意绝对路径**（`/mnt/mcloud`、`/srv/cloud`、……）。
   需要 root 的只有「建目录 + 改属主」这一步，由向导带一条
   `sudo install -d -m 755 -o <user> -g <group> <path>` 完成；之后挂载、
   卸载、systemd 用户服务都不需要 root。
2. **`allow_other` + `default_permissions`**，由 `config.json` 的
   `allow_other` 字段（或 `MCLOUD_ALLOW_OTHER=1` / `--allow-other`）控制，
   默认关闭。默认值不变，因此老配置的行为完全不变。
3. **向导第 3 步「共享访问」**：检查 `/etc/fuse.conf` 的 `user_allow_other`，
   缺了就用一条 sudo 追加；再检查父目录 `o+x`，列出不能穿越的目录和
   `sudo chmod o+x <dir>`，不静默改家目录权限。
4. `mcloud-fuse doctor` 增加 `user_allow_other` 与「父目录可穿越」两项；
   `status` / 向导确认页显示共享状态。

### 8.2 为什么仍是「只读」

- `attrs` 固定 `st_uid/st_gid = 挂载进程`、`0644/0755`，没有本地 ACL 覆盖层
  （形态 C）。`default_permissions` 让内核按这些 mode 判定：其他 uid 能读、
  能遍历，写/删/改名得到 `EACCES`。
- 想放开写，就必须实现形态 C 的 `local_acl.sqlite3`，并接受「权限只在本机
  有效、换机即失效」的语义；这一步不在本次范围内。

### 8.3 已知限制

| 限制 | 说明 |
| --- | --- |
| 只读 | 其他用户不能写；共享写仍是「不做」 |
| 云端身份唯一 | 其他用户的所有操作都记在挂载进程的账号下，云端无法区分本地 uid |
| 依赖 `user_allow_other` | 非 root 挂载的硬性要求；没有它时挂载会明确失败，不会静默降级 |
| 依赖父目录 `o+x` | `$HOME` 为 `0700` 时其他用户进不来，向导只提示不改权限 |
| token 仍私有 | 0600，其他用户读不到；共享不扩大凭据的可见范围 |
