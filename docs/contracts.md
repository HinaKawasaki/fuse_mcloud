# 端点契约测试

`yun.139.com` 的接口没有文档、没有版本承诺。我们无法阻止上游改字段，但可以让
「字段变了」这件事**尽早变红**，而不是等用户 `ls` 报错才发现。

这套机制分两层：

| 层 | 运行位置 | 作用 |
| --- | --- | --- |
| 离线契约 | 每次 CI / `pytest` | 锁定 `tests/contracts/*.json`（真实响应脱敏样例）与解析器；我们改坏解析器时立刻红 |
| 真机契约 | 每周定时 / 手动 | `scripts/check_contracts.py --live` 请求真实接口，字段缺失即失败；只读默认，`--write` 才创建探测对象 |

## 目录

```text
tests/contracts/
├── schema.json            # 每个端点必须存在的字段路径
├── file_list.json         # GET 列表 / hcy/file/list 的真实响应样例
├── file_create.json       # 创建上传槽位
├── file_update.json       # 重命名
├── batch_task.json        # 删除/移动/恢复的 {taskId}
├── task_get.json          # 异步任务状态
├── quota_detail.json      # 配额（MB）
├── recyclebin_list.json   # 回收站列表
└── download_url.json      # 下载直链
```

字段路径用点号表示，列表下标写成数字，例如：

```text
data.items.0.fileId
data.taskInfo.status
```

## 离线

```bash
python3 -m pytest tests/test_contracts.py -q
python3 scripts/check_contracts.py          # 只校验 fixture 自身
```

`test_contracts.py` 同时做两件事：

1. 断言每份 fixture 都满足 `schema.json`；
2. 把 fixture 喂给 `WebFile` / `RecycleItem` / `DiskQuota` 的解析器，确认真实字段还能被读懂。

## 真机

```bash
export MCLOUD_WEB_TOKEN='<Basic 后面的值>'

# 只读端点：file/list、quota、recyclebin/list、getDownloadUrl
python3 scripts/check_contracts.py --live

# 额外检查写端点：会创建并删除 _mcloud_contract_* 探测对象
python3 scripts/check_contracts.py --live --write
```

输出示例：

```text
✅ 离线：8 份 fixture 与 schema 一致
✅ file_list
✅ quota_detail
✅ recyclebin_list
✅ download_url
✅ file_update
✅ batch_task
✅ task_get
✅ file_create

🎉 所有已检查端点字段齐全
```

缺失字段会逐条列出并以非零码退出，可直接接 CI。

## 上游改字段后怎么更新

1. 先跑 `--live --dump /tmp/new_contracts`，拿到真实响应；
2. 人工脱敏（文件名、fileId、URL 签名、账号相关字段）后覆盖 `tests/contracts/*.json`；
3. 如果字段是**新增**，通常只更新 fixture；如果是**改名/删除**，先改解析器，再更新 fixture 与 `schema.json`；
4. 跑 `pytest tests/test_contracts.py` 与 `check_contracts.py --live`，全绿再提交。

## GitHub Actions

`.github/workflows/contract-live.yml` 每周一 03:00 UTC 跑一次真机检查。
需要在仓库 Secrets 里配置：

```text
MCLOUD_WEB_TOKEN = <浏览器里复制的 Authorization: Basic 后面的值>
```

没有配置 secret 时 workflow 会失败在「未找到 Web token」，这是预期行为；
如果不想启用，直接在 Actions 里禁用该 workflow 即可。
