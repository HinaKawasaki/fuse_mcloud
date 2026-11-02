# ADR-0002：把 yun.139.com Web API 作为隔离的 opt-in 后端

- 状态：**已接受**（Accepted，2026-10-28）
- 日期：2026-10-28
- 相关：[ADR-0001：私有接口红线](0001-private-api-boundary.md)、
  [Web API 可行性分析](../web-api-analysis.md)

## 背景

ADR-0001 把「厂商未注册的 App / Web 端点」定为 L3，默认禁止，理由是：
越界即从有身份归属的授权通道走入无归属通道，且可能推翻厂商的显式范围决策。

但 2026-10-28 的核实表明：

1. 官方 Skill v2.0.2 已把 `batchDeleteAsync`、`batchMoveAsync`、`file/update`
   整体注释停用；继续等厂商放行，等于接受「删除不释放容量、目录不能改名、
   移动要复制双份」这个天花板。
2. Web 端 `/hcy/*` 端点完整存在，并且 AList/OpenList 已长期使用；
   协议、签名、续期方式都有可读实现。
3. 本项目最核心的三个缺陷（无删除、无 rename、沙箱限制）都只能靠这批端点解决。

ADR-0001 的结论「不分析 Web 端接口」因此需要被重新审视。**本 ADR 不否认
ADR-0001 的风险判断**，只改变「是否允许以受控方式使用」这一决策。

## 决策

允许实现 L3 后端，但必须同时满足以下约束：

1. **物理隔离**：L3 代码只放在 `mcloud_fuse/client/web139.py`，不得被
   `client/wrapper.py`、`mapping/`、`cache/` 直接 import。FUSE 层只依赖
   一个与后端无关的协议。
2. **显式启用**：没有 token 时启动直接失败并提示导入方式，不会自动改走
   任何其他通道。启用动作就是「导入 token」这一次显式操作。
3. **凭据显式提供**：只接受 `MCLOUD_WEB_TOKEN` 或 `MCLOUD_WEB_TOKEN_FILE`
   里的 `Authorization: Basic` 值。**不实现账号密码登录**，不落盘密码。
   token 文件权限 0600，日志中不得出现。
4. **不做静默降级**：Web 后端报错时直接向用户报错，不得偷偷切回 Skill。
   两条通道的容量语义不同（真删 vs 墓碑），静默切换会造成数据认知混乱。
5. **限流一致**：复用 `router/rate_limiter.py` 的并发/RPS 上限；新增端点
   必须先在 `docs/api-capability-matrix.md` 第 2 节登记。
6. **可一键停用**：删除 token 文件（或改 `MCLOUD_WEB_TOKEN_FILE`）后进程无法
   启动挂载，不迁移数据、不留后台任务。
7. **真机验证先于默认路径**：`scripts/web139_smoke.py` 全绿之前，Web 后端
   只能以显式开关运行，不得成为默认。

## 后果

正面：

- `unlink` / `rmdir` 变成真删除（回收站），容量最终可释放；
- 目录 rename、跨目录 move 成为原语，不再搬运整文件；
- 目录列举不再依赖最终一致的搜索索引；
- 全盘读写不再需要 `.MCloudHydrate` 副本。

负面：

- 从「厂商授权通道」退到「未注册通道」，ToS 与封号风险由使用者承担；
- token 是账号全权凭据，泄漏面比 Skill 的 creditToken 更大；
- 上游随时可能改签名/字段，维护成本高于 L2；
- 删除进回收站不等于立即释放容量，文档必须写清。

## 合规检查

- [x] 能力矩阵登记端点（`docs/web-api-analysis.md` 2.1）
- [x] 无 token 即失败，不静默降级
- [x] 凭据不落密码、0600
- [x] 不静默回退
- [x] 真机冒烟脚本（`scripts/web139_smoke.py`，待导入 token 后执行）
- [x] 把 `/file/batchDelete`（rm）、`/file/update`、`/file/batchMove` 写成离线契约测试
- [ ] 真机验证后再把能力矩阵重写为「已验证」

## 复评条件

出现以下任一情况时重新评估，必要时回到 ADR-0001 的默认禁止：

- 厂商在 Skill v2.x 中重新启用删除/移动/重命名；
- Web 端开始要求设备指纹/验证码，续期不可用；
- 出现账号因非官方客户端被限制的实际案例。
