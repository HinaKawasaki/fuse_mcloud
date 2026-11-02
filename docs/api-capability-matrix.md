# API 能力矩阵与风险登记册（历史：Skill 通道）

> **本文描述的是已归档的 `.old-version/` Skill 通道，不再代表当前实现。**
> 当前后端的端点、能力与风险见 [Web API 可行性分析](web-api-analysis.md)
> 与 [ADR-0002](adr/0002-web-api-optin.md)。保留本文仅作历史对照。

> 本文是 `mcloud_FUSE` 的**能力边界契约**：哪些 POSIX 语义能真做、哪些只能近似、哪些做不到，
> 以及每个结论的依据出处在哪。新增功能前请先对照本文；改动上游依赖后请回来更新本文。

- 适用版本：`mcloud_FUSE` v0.3.0 + 官方 Skill **`cm-cloud-manage` v1.0.4**（2026-10-14 核实）
- 核实方式：逐一读取 `basement/scripts/*` 与 `mcloud_fuse/client/wrapper.py` 的实际调用，
  而不是依据文档描述
- 相关决策：[ADR-0001：私有接口红线](adr/0001-private-api-boundary.md)

> **基线锁定**：CI 通过 `MCLOUD_SKILL_VERSION=1.0.4` 显式锁定 Skill 版本
> （`.github/workflows/ci.yml:46-49`、`:74-77`），安装后的 Skill 在 `basement/_meta.json`
> 自报版本。
>
> ⚠️ **重大更新（2026-10-14 追加核实）**：官方 Skill 已发布 **v2.0.2**，接口代际更换
> （`personalSaas/*` → `idaasV3/*`，网关迁至 `openapi.yun.139.com/open-ai-space`）。
> 下文第 3 节的能力矩阵是 **v1.0.4 基线的快照**，其中若干「❌ 不支持」在 v2.0.2 中
> 已有对应端点。对照见本文 **第 7 节**。
> **在迁移 v2.x 完成之前，第 3 节仍是当前代码的真实边界。**
>
> **2026-10-28 追加**：已按 [ADR-0002](adr/0002-web-api-optin.md) 实现隔离的
> Web 后端客户端（`mcloud_fuse/client/web139.py`，需显式导入 token），
> 端点与能力见 [Web API 可行性分析](web-api-analysis.md)。**在真机冒烟完成前，
> 它不属于默认路径，第 3 节仍然是默认挂载的真实边界。**

---

## 0. 结论摘要

1. **本项目依附的是厂商提供的集成通道，不是未授权接口。** 官方 Skill 由认证企业
   「中移互联网」在 SkillHub 上架（`@org-vt43r0t0/cm-cloud-manage`，`ownerId 631940`），
   经 skill 专用适配网关 `orches.yun.139.com/adaptor/new/skill/nk/` 发起请求，
   携带注册身份（`AUTH_SKILL_ID` / `AUTH_DEV_ID` / `AUTH_CLIENT_ID`）。
   它是**未文档化**的（对我们而言），但不是**未授权**的——这两者必须分清。
2. **目录列举没有专用接口**，它建立在搜索索引（`search/merge/file`）之上。
   这是 v1.0.4 基线最根本的能力约束：`readdir` 天生带最终一致性。
   （v2.0.2 的 `file/list` 已提供专用枚举端点，见第 7 节。）
3. **v1.0.4 没有删除、没有回收站、没有原位更新、没有 rename 原语。** 这四条决定了
   「删除不释放容量」「覆盖写产生旧版本」「目录改名 ENOSYS」在 v1.0.4 下不是 bug，而是天花板。
4. 红线应画在**「已封装的端点」与「我们新增的端点」之间**——但理由不是
   「反正都在用私有接口」，而是**「越界即从授权通道走入未授权通道」**。
5. 新增 L3 的收益是**补齐个别语义**，代价是**从有身份归属的授权调用变成无归属调用**，
   且可能**推翻厂商的显式范围决策**（删除/移动/重命名在 v2.0.2 中被主动停用）。
   当前决策：不进入默认路径，仅允许 opt-in 隔离适配器。
6. **真正的天花板是 Skill 版本，不是 API。** 优先做 v1.0.4 → v2.0.2 迁移评估，
   而不是去分析 Web 端接口。

---

## 1. 依赖分层

| 层 | 内容 | 是否已依赖 | 契约稳定性 | 本项目的态度 |
| --- | --- | --- | --- | --- |
| **L0** | 本仓库代码（`mcloud_fuse/`） | — | 自控 | 可自由演进 |
| **L1** | Skill 的对外面：CLI 子命令 + `cm_cloud_*` Python 函数 | 依赖 | 中（Skill 自己的版本） | 唯一允许调用的编程接口 |
| **L2** | Skill 内部实际打的上游端点（v1.0.4 为 `/richlifeApp/personalSaas/*`、`/aiService/*`） | **已在依赖** | 中低（厂商网关 + 注册身份，但无对外文档/SLA） | 只允许「Skill 已经打过」的那批 |
| **L3** | Skill **未注册**的 App / Web 端点（删除、回收站、真正的移动/重命名、账号操作…） | **不依赖** | 低 | **默认禁止**，见 ADR-0001 |

> 关键澄清：L2 与 L3 的区别**不是「公开 vs 私有」**——L2 同样未文档化。
> 区别在于：L2 是**厂商自己封装、登记了路由、有身份归属**的调用；
> L3 是**厂商未注册、无归属**的调用。前者出错由上游负责，后者由我们自己负责。
> 因此「既然已经在用 L2，L3 也无所谓」是错误推论——两者风险性质不同，不是程度的差别。

---

## 2. 已依赖端点清单（L2，证据）

传输层：`HOST=https://orches.yun.139.com/adaptor/new/skill/nk/`；
鉴权层：`https://idaas.cmpassport.com/keyManage/custody/preAuth`（`basement/.env`）。

| 端点 | 用途 | 定义处 |
| --- | --- | --- |
| `personalSaas/file/create` | 创建上传任务（**必须一次性给出总大小**） | `cm_cloud_client.py:22` |
| `personalSaas/file/complete` | 完成上传（需 `content_hash`） | `cm_cloud_client.py:23` |
| `personalSaas/file/createFolder` | 建目录 | `cm_cloud_client.py:24` |
| `personalSaas/file/batchCheckExists` | 同名存在性检查 | `cm_cloud_client.py:25` |
| `personalSaas/file/batchGet` | 批量取元数据 | `cm_cloud_client.py:26` |
| `personalSaas/file/batchGetDownloadUrl` | 批量取下载直链 | `cm_cloud_client.py:27` |
| `personalSaas/file/batchCopy` | 云内复制（**异步任务**） | `cm_cloud_client.py:28` |
| `personalSaas/task/get` | 查询异步任务 | `cm_cloud_client.py:29` |
| `personalSaas/file/batchGetPath` | fileId → 云盘路径 | `cm_cloud_sandbox.py:15` |
| `aiService/.../search/merge/file` | 搜索 / **目录列举** | `cm_cloud_client.py:30` |

**被依赖的能力面只有 10 个端点。** 这份清单就是可接受爆炸半径的上限。

---

## 3. 能力矩阵

图例：✅ 原生支持 ｜ 🟡 近似/有条件 ｜ ❌ 不支持

| POSIX 语义 | 状态 | 实现方式 | 证据 | 后果 / 兜底 |
| --- | --- | --- | --- | --- |
| `lookup` / `getattr` | ✅ | `batchGet` + 本地 TTL 60s | `wrapper.py:285` | 远端改动最长 1 分钟可见 |
| `readdir` | 🟡 | **搜索索引**按 `include_file_ids=[父目录]` 过滤 | `cm_cloud_client.py:213-283` | 新建对象可能短暂不可见；有索引宽限期防误删 |
| 整文件下载 | ✅ | `batchGetDownloadUrl` + 流式 GET | `wrapper.py:406` | 支持 Range 可做按需读，否则降级整文件 |
| 按需（Range）读 | 🟡 | 直链 `Range` 分片 | `release-notes.md` v0.3.0 | 服务端不支持则自动降级；元数据 size 不符即报错 |
| `create` / `write` | 🟡 | 本地写回暂存，`release` 时整文件上传 | `wrapper.py:583-692` | **无随机写**；`create` 时必须已知总大小 |
| 覆盖写 | 🟡 | 新 fileId + 墓碑屏蔽旧对象 | README 已知限制 | 云端留同名旧版本，**本地看不见、容量照样占** |
| `fsync` | 🟡 | 中途推送整文件 | `fuse_ops.py` 写回状态机 | 不等价于 POSIX 的持久化语义 |
| 多段上传（大文件） | 🟡 | `file/create` 的 `partInfos`，**按序** | `wrapper.py:583+` | 不能并发加速，不能从中断处分片续传 |
| `mkdir` | ✅ | `createFolder` | `cm_cloud_client.py:290` | **仅限沙箱内**，越界 EACCES |
| `unlink` / `rmdir` | ❌→🟡 | **无删除端点**：墓碑 + 本地摘除 | README 已知限制 | **不释放容量**；墓碑是本地状态，换机/清缓存后对象复活 |
| `rename`（文件） | 🟡 | 下载 + 新名上传 + 旧对象墓碑 | README 已知限制 | 大文件代价高 |
| `rename`（目录） | ❌ | `ENOSYS` | README 已知限制 | 不假装支持，明确报错 |
| 跨目录移动 | 🟡 | `batchCopy` + 墓碑（异步等待） | `wrapper.py:350` | 复制即占双倍容量直至手删 |
| `chmod` / `chown` / 时间戳 | ❌ | 静默忽略 | README 已知限制 | 不报错也不生效 |
| `flock` / `fcntl` 锁 / xattr / 符号链接 / 硬链接 | ❌ | 不支持 | README 已知限制 | 依赖这些的工具（git、部分编辑器）会退化 |
| `allow_other` 多用户 | ❌ | 不支持 | README 已知限制 | 单用户挂载 |
| `statfs` | 🟡 | 配额接口缺失，靠估算 | `fuse_ops.py` | `df` 数字不可信 |
| 沙箱外**读** | 🟡 | 云内复制进 `.MCloudHydrate/` 再下载 | README 已知限制 | 首次读有延迟；副本**占容量且删不掉** |
| 沙箱外**写** | ❌ | 明确 `EACCES` | `cm_cloud_sandbox.py:79-84` | 有清晰报错，不是玄学失败 |

---

## 4. 风险登记册

等级：🔴 高 ｜ 🟠 中 ｜ 🟡 低。「现有缓解」之外的剩余风险即使已缓解也照实记录。

| ID | 风险 | 触发条件 | 影响 | 等级 | 现有缓解 | 剩余风险 / 待办 |
| --- | --- | --- | --- | --- | --- | --- |
| **R1** | 列举依赖搜索索引（最终一致） | 任何 `readdir` | 新对象延迟可见、远端删除延迟消失 | 🟠 | 60s TTL + 索引宽限期 + 陈旧对账 | **不可根治**；只能文档化预期 |
| **R2** | 无删除/回收站接口 | 用户执行 `rm` | 容量只增不减，用户以为已释放 | 🔴 | 墓碑 + 本地摘除；README/排障明确说明 | 需「墓碑管理」来列出/恢复→已列入后续可做 |
| **R3** | 无原位更新 | 任意覆盖写 | 云端堆叠旧版本、白占容量 | 🟠 | 写回缓存 + 崩溃恢复日志 | 旧版本清理只能去 App |
| **R4** | 无 rename/move 原语 | 目录改名、大文件改名 | `ENOSYS` / 高代价搬运 | 🟡 | 明确报错不假装支持 | 语义增强已列入后续可做 |
| **R5** | **L2 私有端点无版本承诺** | 上游改端点/签名/`IFACE_VERSION` | **全盘不可用** | 🔴 | `.env` 集中配置；CI 锁 Skill 1.0.4；`MCLOUD_BASEMENT_DIR` 定位；排障表 | **缺端点契约测试与运行时版本校验**——见第 5 节 |
| **R6** | 账号级风控 | RPS/并发过高或调用模式异常 | 用户**整个云盘账号**被限流/封禁 | 🔴 | 并发 3 / RPS 5 / 指数退避 / 权限错不重试 | 阈值可调但无自适应；无「触发熔断即停」提示 |
| **R7** | 凭据与 token 落盘 | device id、creditToken、`AUTH_CLIENT_ID` 等 | 凭据泄漏 → 账号被冒用 | 🟠 | `_write_json_private`（0600） | 文档未说明存储位置与轮换方式 |
| **R8** | 沙箱边界 | 沙箱外读/写、挂载根 | 写 EACCES；读产生**占容量且删不掉**的副本 | 🟠 | 边界校验 + 副本按槽位复用 | 副本容量对用户不可见、不可回收 |
| **R9** | **引入 L3 私有接口** | 为补功能去调 App/Web 端点 | ToS 风险、封号、随时断链，且回退成本高 | 🔴 | 本决策：默认禁止，仅 opt-in 隔离 | 需在 ADR-0001 中以机制固化，而非口头约定 |
| **R10** | 上游 Skill 与本仓库分发不同步 | 用户装旧/新版 Skill | 行为漂移、难以复现的故障 | 🟠 | **CI 已锁 `MCLOUD_SKILL_VERSION=1.0.4`**；`basement/_meta.json` 自报版本；`setup_basement.py` 版本不符时明确报错 | 运行时**不校验**用户本地装的是不是被测过的版本；且 10 个端点**没有契约测试**——上游改端点时 CI 未必变红 |

### 风险之间不是独立的

R2、R3、R8 是同一件事的三个面：**云盘接口只会「增」不会「减」**。
每一次删除、覆盖、跨沙箱读取都在云端留下一个用户看不见也删不掉的副本。
所以本项目的容量行为必须按「只增」来设计和对用户解释。

R5 与 R9 共同决定：**扩能力的边际风险远高于扩稳定性的边际收益。**

---

## 5. 上游变动时的维护流程

R5 无法靠代码消除，只能靠流程收敛：

1. **版本已锁，但只锁住了 CI。** CI 通过 `MCLOUD_SKILL_VERSION=1.0.4` 固定拉取版本，
   安装后的 Skill 也在 `basement/_meta.json` 自报版本；缺的是**运行时校验**——
   用户本地装了什么版本，本挂载并不知情。
2. **契约测试**：对本文第 2 节的 10 个端点各写一个最小探针（离线用假传输层，
   真机用只读调用），上游一改即红。这是把「能力矩阵」从文档变成可执行断言。
   **这是当前最大的单点缺口**：端点变了，现有 CI 大概率仍是绿的。
3. **跟版纪律**：上游变动后 24h 内完成探针回归 + 发一篇 release note。
   这既是安全网，也是公开的更新承诺——见「每日更新节奏」讨论。
4. **本文优先更新**：任何新增端点必须先在本文第 2 节登记，再写代码。
   未经登记的端点不得出现在默认路径。

---

## 6. 本文不回答的问题

- 「要不要为了功能去调 Web 接口」→ 见 [ADR-0001](adr/0001-private-api-boundary.md)。
- 「没有 API 的日子怎么做到每天一更」→ 更新节奏问题，见后续《更新节奏与 backlog 拆分》。
- 每个端点具体的请求/响应字段语义 → 属于上游实现细节，以 `basement/scripts/*` 为准，
  本文只登记「我们依赖了它」。

---

## 7. v2.0.2 追加发现（2026-10-14）

核实第 2 节端点的过程中发现：官方 Skill 已从 v1.0.4 迭代到 **v2.0.2**，
且不是增量更新，而是**接口代际更换**。

### 7.1 代际差异

| 维度 | 本项目当前依赖 v1.0.4 | 官方 v2.0.2 |
| --- | --- | --- |
| 端点路径 | `/richlifeApp/personalSaas/*`、`aiService/*` | `/richlifeApp/idaasV3/*` |
| 网关 | `orches.yun.139.com/adaptor/new/skill/nk/` | `openapi.yun.139.com/open-ai-space`（`cmcloud.json`） |
| 版本头 | `IFACE_VERSION` | `version: 2.0.0` |
| 代码组织 | `cm_cloud_client` / `cm_cloud_transport` / `cm_cloud_sandbox` / `cm_cloud_format` / `cm_cloud_errors` | `adapter_*` / `core_*` / `command_*` / `cmcloud_*` |
| 端点数量 | 10（本项目用到的） | 54 个 adapter，其中 **49 个启用**、5 个被注释停用 |

> **迁移不是改版本号**：v1.0.4 的那批 `cm_cloud_*` 模块在 v2.0.2 中**不存在**，
> 而 `mcloud_fuse/client/wrapper.py` 在 import 期直接依赖它们。
> 迁移等价于**客户端层重写**。

### 7.2 v2.0.2 中被厂商主动停用的能力（关键）

以下 5 个 adapter 在 v2.0.2 源码中被**整体注释**，文件首行注明：

> 「本期场景范围外：整体注释停用，保留原实现供后续明确纳入范围时恢复。
> 不注册路由、不定义调用入口、不发起网络请求。」

| 被停用的 adapter | 端点 | 对应能力 |
| --- | --- | --- |
| `adapter_file_batch_delete_async` | `file/batchDeleteAsync` | **删除** |
| `adapter_file_batch_move_async` | `file/batchMoveAsync` | **移动** |
| `adapter_file_update` | `file/update` | **重命名 / 更新**（含 `force_rename` / `refuse` 两种改名模式） |
| `adapter_file_batch_get_path` | `file/batchGetPath` | fileId → 路径 |
| `adapter_file_search_v2` | `file/searchV2` | 搜索 v2 |

**这三条是本登记册最重要的结论**：

1. 删除 / 移动 / 重命名**不是厂商忘了做**，实现代码就在官方包里，是**被主动停用**的。
2. 因此绕开它去调 Web 端等价接口，性质是**推翻厂商的显式范围决策**，
   而不是「使用一个我们已经在用的 API」——见 ADR-0001。
3. 正确做法是**等待或通过正规渠道请求厂商纳入范围**；厂商一旦放行，改版本即可获得，
   不需要我们承担任何未授权风险。

### 7.3 v2.0.2 启用的能力中，对本项目最有价值的

| 端点 | 价值 | 影响条目 |
| --- | --- | --- |
| `file/list` | **专用目录枚举**，不再依赖最终一致的搜索索引 | **R1 有望根治**（从「缓解」升级为「消除」） |
| `file/listUploadedParts` | 分片上传断点续传 | 大文件续传 |
| `idaas/file/batchCopySync` | 同步复制 | 跨目录移动链路简化 |
| 相册 / 故事 / 分享 / 活动（约 30 个 adapter） | 全新场景面 | 新的功能与更新供给来源 |

### 7.4 由此得出的行动优先级

1. **做 v1.0.4 → v2.0.2 迁移评估**（不是立刻重写）：确认 49 个启用端点中哪些覆盖
   本项目现有能力、`file/list` 的语义是否能承载 `readdir`、鉴权链路差异与工作量。
2. **不分析 Web 端接口**：删除/移动/重命名已在厂商包内且被显式停用，
   分析 Web 接口既无必要，也把项目推入未授权通道。
3. **迁移完成后重写本文第 3 节**：当前矩阵是 v1.0.4 快照。
4. **保留红线**：迁移到 v2.x 拿到的是**厂商愿意给的能力**，红线依然成立。

