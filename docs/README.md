# 文档索引

## 入门

| 文档 | 内容 |
| --- | --- |
| [README](../README.md) | 安装、首次设置、日常命令、配置、常见问题 |
| [TUI 设计](tui.md) | 首次设置向导的流程、页面草图、按键、降级行为 |

## 设计

| 文档 | 内容 |
| --- | --- |
| [架构详解](architecture.md) | 模块、并发、懒加载、写回、SQLite 日志、关机抢救 |
| [ADR-0001](adr/0001-private-api-boundary.md) | 早期「只走官方 Skill」的边界决策（历史） |
| [ADR-0002](adr/0002-web-api-optin.md) | 为什么启用 Web API、如何隔离、风险承担 |
| [ADR-0003](adr/0003-multiuser-and-locking.md) | 每人一个账号、本地 advisory 锁、冲突策略 |

## 运维与验证

| 文档 | 内容 |
| --- | --- |
| [开发指南](development.md) | 环境、测试分层、新增 FUSE 回调、发布步骤 |
| [端点契约](contracts.md) | 离线样例 + 每周真机字段漂移检查 |
| [性能基线](performance.md) | 大目录、上传、删除、冷/热读实测 |
| [Web API 分析](web-api-analysis.md) | 端点、鉴权、签名、风险、真机验证记录 |
| [发布记录](release-notes.md) | v0.4.0 行为变化与升级说明 |

## 历史

| 文档 | 说明 |
| --- | --- |
| [API 能力矩阵](api-capability-matrix.md) | v0.3.0 Skill 通道的能力/风险登记，仅作对照 |
| [readdir 事故复盘](postmortem-readdir.md) | 早期目录重复/串台问题的根因 |
| [.old-version](../.old-version/README.md) | 旧 Skill、沙箱、墓碑实现归档 |
