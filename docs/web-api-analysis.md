# yun.139.com Web API 可行性分析

> 结论先行：Web 端（`https://yun.139.com/hcy/*`）确实补上了官方 Skill 缺的
> **删除 / 重命名 / 移动 / 全盘直读 / 专用目录枚举**，能力上足以让本项目从
> 「只增不减的半吊子文件系统」变成语义完整的挂载。
> 代价是：这是 ADR-0001 明确划为 L3 的**未注册通道**，而且需要用户提供
> 一个等同于账号全权的 `Authorization` token。
>
> 本文只陈述事实与方案，是否越过红线由项目所有者决定，见
> [ADR-0002](adr/0002-web-api-optin.md)。

- 核实日期：2026-10-28；真机复核：2026-11-02
- 核实方式：阅读 OpenList/AList 的 139 驱动源码（`drivers/139`）与官方文档，
  并用 `scripts/web139_smoke.py` 在真实账号上跑通全链路（建目录 / 上传 / Range /
  改名 / 移动 / 真删除），探针对象已清理。

---

## 1. 为什么 Skill 不够

官方 Skill v1.0.4 只暴露 10 个端点，其中：

- 目录列举靠搜索索引（`search/merge/file`），天生最终一致；
- **没有删除、没有回收站、没有 rename/move 原语**；
- 只能读写沙箱 `/AI空间/智能体云盘助手`，沙箱外要「云内复制 + 下载」。

v2.0.2 虽然把端点扩到 54 个，但 `batchDeleteAsync` / `batchMoveAsync` /
`file/update` 被厂商**整体注释停用**。也就是说：缺的能力不是没实现，而是
厂商在 Skill 这条通道上主动关掉了。

## 2. Web 端提供了什么

Web 端走的是另一套网关与鉴权：

| 维度 | 官方 Skill（L2） | Web 端（L3） |
| --- | --- | --- |
| 基址 | `orches.yun.139.com/adaptor/new/skill/nk/` | 路由策略返回的 `personal` 域名，默认 `yun.139.com` |
| 鉴权 | 注册身份 + `creditToken` | `Authorization: Basic <base64(prefix:account:payload)>` |
| 签名 | `IFACE_VERSION` 等 | `mcloud-sign: ts,rand,MD5(...)` |
| 端点前缀 | `personalSaas/*` / `idaasV3/*` | `/file/*`、`/recyclebin/*` |

### 2.1 端点清单（按 AList 驱动的实际调用）

| 端点 | 用途 | 关键入参 |
| --- | --- | --- |
| `POST /file/list` | 目录枚举，游标分页 | `parentFileId`、`pageInfo.pageCursor/pageSize` |
| `POST /file/getDownloadUrl` | 取下载直链 | `fileId` |
| `POST /file/create` | 建目录 / 创建上传任务 | `type=folder` 或 `type=file` + `contentHash` + `partInfos` |
| `POST /file/getUploadUrl` | 追加分片上传地址（每批 ≤100） | `fileId`、`uploadId`、`partInfos` |
| `POST /file/complete` | 完成上传 | `contentHash`、`fileId`、`uploadId` |
| `POST /file/update` | **重命名**（文件与目录同一端点） | `fileId`、`name` |
| `POST /file/batchMove` | **移动**（目录也可） | `fileIds`、`toParentFileId` |
| `POST /file/batchCopy` | 复制 | `fileIds`、`toParentFileId` |
| `POST /file/batchDelete` | **永久删除（rm）** | `fileIds` |
| `POST /recyclebin/batchTrash` | 删除到回收站 | `fileIds` |
| `POST /task/get` | 查询删除/移动异步任务 | `taskId` |
| `POST /disk/quota/detail` | 配额（MB） | `{}`（user-njs 主机） |
| `POST /user/route/qryRoutePolicy` | 取 personal 域名 | `userInfo.accountName` |

对照 POSIX 语义：

| POSIX | 现在（Skill） | Web 端后 |
| --- | --- | --- |
| `readdir` | 搜索索引，最终一致 | ✅ 专用枚举，强一致（真机确认） |
| `unlink` / `rmdir` | 墓碑 + 本地摘除，不释放容量 | ✅ `/file/batchDelete` 真删并轮询任务；`MCLOUD_RM_MODE=trash` 可改为回收站 |
| `rename`（文件） | 下载 + 重传 + 墓碑 | ✅ 原语直改 |
| `rename`（目录） | `ENOSYS` | ✅ 原语直改 |
| 跨目录移动 | 复制 + 墓碑，双倍容量 | ✅ 原语直移，不占双倍 |
| 覆盖写 | 新 fileId，旧对象永留 | ✅ 冲突时「旧对象改名 + 删除 + 新对象改回原名」 |
| 全盘读写 | 沙箱外只读（懒搬运） | ✅ 全盘直接读写 |
| `statfs` | 配额缺失，靠估算 | ✅ `/disk/quota/detail`，实测单位 MB，删除后可用空间回升 |
| 大文件上传 | 5MiB 分片、按序 | ✅ 同样分片；`X-Yun-Client-Info` 客户端类型 13 时单文件上限 500GB |

### 2.2 鉴权与续期

- 最短路径：登录 `yun.139.com`，在 DevTools 里找 `hcy/file/list`，复制请求头
  `Authorization: Basic ...` 中 `Basic` 后面的值。该值 base64 解码后是
  `prefix:account:payload`，`payload` 的第 4 段是毫秒过期时间。
- 续期：`POST https://aas.caiyun.feixin.10086.cn/tellin/authTokenRefresh.do`，
  XML body `<root><token>payload</token><account>...</account><clienttype>656</clienttype></root>`，
  返回新 `payload`。AList 在剩余寿命 <15 天时续期，可长期使用。
- 续期失败或 token 过期时，只能让用户重新粘贴。**不实现账号密码登录**：
  那需要复刻邮箱登录的 SHA1/AES 与风控参数，还要把账号密码落盘，收益低、风险高。

## 3. 风险

| 风险 | 说明 | 缓解 |
| --- | --- | --- |
| **授权边界** | 这些端点厂商没有注册给第三方；删除/移动/重命名在 Skill 里被显式停用。用它等于推翻厂商的范围决策 | 无 token 不启动、隔离模块、ADR 留痕、用户显式导入 token |
| **账号风控** | 非官方客户端调用模式可能触发风控甚至封号 | 复用 3 并发 / 5 RPS 限流；不做高频轮询；出错即退避 |
| **token 全权** | token 等同于账号访问权，泄漏后果严重 | 0600 落盘、不写日志、不提交仓库；只支持显式提供 |
| **接口漂移** | 无文档、无 SLA，字段随时可能变 | 端点契约测试；失败时明确报错，不静默回退 |
| **删除语义** | `/file/batchDelete` 在网页端被标为 logicalDelete，是否立即释放容量未经真机确认 | 真机核对容量；必要时改为「trash + clear」 |
| **续期不确定** | 续期接口可能需要设备指纹，未必长期有效 | 真机观察；失败时提示重新粘贴 |

## 4. 已完成的代码（离线可验证）

- `mcloud_fuse/client/web139.py`
  - token 解析 / 15 天窗口自动续期 / 0600 落盘
  - `mcloud-sign` 复刻、路由策略、统一错误映射
  - `iter_files` / `get_download_url` / `create_folder` / `rename` / `move` /
    `copy` / `trash` / `upload_file`（>100 片自动补地址、分片重试、覆盖冲突处理）
- `tests/test_web139.py`：17 个离线用例，覆盖签名、分页、分片、冲突、续期；
  不依赖网络与 pytest 插件。
- 配置：`MCLOUD_WEB_TOKEN` / `MCLOUD_WEB_TOKEN_FILE`、
  `MCLOUD_WEB_PART_SIZE`、`MCLOUD_WEB_CLIENT_TYPE`、`MCLOUD_RM_MODE`。

**已接入 FUSE**（v0.4.0）：`client/wrapper.py` 适配层复用原有 FUSE/缓存接口，
`unlink`/`rmdir` 走 `/file/batchDelete`，`rename` 走 `/file/update`，跨目录走
`/file/batchMove`。协议本身仍未经真机验证，**首个真实账号冒烟前请按第 5 节逐项确认**。

## 5. 真机验证清单

设置 `MCLOUD_WEB_TOKEN` 后运行 `scripts/web139_smoke.py`（待补），至少确认：

1. `file/list` 能列根目录，游标能翻页；
2. `getDownloadUrl` 的直链支持 Range；
3. `create folder` → `rename` → `move` → `trash` 全链路；
4. 上传 1 个小文件、1 个 >100 片的文件，sha256 一致；
5. 覆盖写冲突分支真的把旧对象删掉（`/file/batchDelete`）；
6. `batchDelete` 后配额是否立即回升 —— **已确认**：删除探测对象后 `freeDiskSize` 回到初始值。

## 6. 接入方案（验证通过后）

1. 抽一个 `CloudBackend` 协议：`list / stat / download / upload / mkdir /
   rename / move / delete / quota`。
2. `DirectClientWrapper` 已改为 Web 后端适配器；旧 Skill 实现归档在
   `.old-version/`，不再参与装配。
3. 删除语义改为「真删」：不再登记墓碑；旧墓碑实现只在 `.old-version/`。
4. `rename` 目录不再返回 `ENOSYS`；跨目录移动不再复制。
5. 全盘模式不再需要 `.MCloudHydrate`；旧的隐藏目录只做兼容过滤。
6. 重写 `docs/api-capability-matrix.md` 第 3 节，并把本文件第 5 节的结论
   写成契约测试。

## 7. 参考

- OpenList 139Yun 驱动文档：<https://doc.oplist.org/guide/drivers/139>
- AList 驱动源码：<https://github.com/AlistGo/alist/tree/main/drivers/139>
- 本仓库：[ADR-0001](adr/0001-private-api-boundary.md)、
  [能力矩阵](api-capability-matrix.md)
