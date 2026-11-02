# `.old-version/`：官方 Skill 通道（v0.3.0）快照

这里只归档**依赖官方 Agent Skill / 沙箱接口的那一层**，不是整个旧版本。
FUSE 回调、读缓存、写回缓存仍留在仓库主体继续复用，只是后端换成了
`yun.139.com` Web API。

## 归档内容

| 文件 | 原职责 |
| --- | --- |
| `mcloud_fuse/basement.py` | 定位并注入官方 Skill 的 `basement/scripts` |
| `mcloud_fuse/client/auth_manager.py` | Skill 的 `creditToken` 生命周期 |
| `mcloud_fuse/client/wrapper.py` | 基于 Skill 的 10 个端点实现列举/下载/上传 |
| `mcloud_fuse/mapping/tombstones.py` | 「删不掉」时代的墓碑 |
| `mcloud_fuse/utils/trash_bin.py` | 墓碑 / 复制进 `.TrashBin` 的删除策略 |
| `scripts/setup_basement.py` | 从 SkillHub 拉取官方 Skill |
| `tests/test_auth.py`、`test_wrapper.py`、`test_trashbin.py` | 上述模块的离线测试 |

另有三个测试因为直接验证沙箱/多段/真实 Skill 行为，一并归档：

- `tests/test_hydrate.py`
- `tests/test_multipart.py`
- `tests/integration/real_mount_smoke.py`

## 注意

- 这里的代码**不参与**打包、CI 与 `pytest`（`pyproject.toml` 只打包
  `mcloud_fuse*`，`tests/` 只收集仓库根的 `tests/`）。
- 它们依赖 `basement/`（官方 Skill，未提交、含凭据），因此**不能直接运行**；
  需要时按旧版 README 重新执行 `setup_basement.py` 并登录。
- 删除/重命名/移动在旧通道里是墓碑 + 复制近似；新实现改为 Web API 的原语，
  语义对照见 `docs/web-api-analysis.md`。

## 为什么要留

1. Skill 通道仍是「厂商授权通道」，未来若厂商重新放行删除/移动/重命名，
   可以直接恢复，不必从 git 历史里翻；
2. 旧实现里的限流、退避、Range 读取、分片重试等经验，是 Web 后端的对照基线；
3. 出问题时可以快速切回旧通道做 A/B 对比。
