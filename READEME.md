# mcloud_FUSE

> 基于 `pyfuse3` 与 `trio` 异步事件循环的移动云盘 POSIX 文件系统挂载中间层。

`mcloud_FUSE` 将中国移动云盘（个人云/沙箱）通过 POSIX 接口直接挂载为 Linux 本地目录。通过直接导入（Direct Import）官方 Agent Skill SDK，配合内存目录树与磁盘缓存机制，提供近乎本地文件系统的流畅浏览与读写体验。

---

## ✨ 核心特性与架构设计

* **高吞吐微秒级响应**：内置 `InodeMapper` 与 `MetaCache`，实现 `getattr` / `readdir` 的微秒级 TTL 元数据缓存。
* **流式读缓存与 LRU 管理**：采用 `DataCache` 在 `/tmp`（`tmpfs`）暂存热点文件，提升重复读取与大文件分块读取效率。
* **高并发与稳定控频**：摒弃低效的 `subprocess` CLI 启动开销，在 Python 进程内直接管理 API 调用的并发与指数退避重试（Backoff）。
* **数据安全防误删**：拦截 POSIX `unlink` / `rmdir` 操作，将删除动作自动重定向并移动至 `.TrashBin/` 回收站隐藏目录[cite: 1]。

---

## 🗺️ 项目演进路线图 (12-Week Roadmap)

- [x] **第一阶段：架构重构与底层 API 通路（第 1 - 3 周）**
  - [x] SDK 模块集成与 AuthManager 登录状态维护
  - [x] 并发信号量与退避重试控制器
  - [x] 回收站 (`.TrashBin`) 与沙箱路径路由拦截[cite: 1]
- [x] **第二阶段：映射层与强元数据缓存（第 4 - 6 周）**
  - [x] `InodeMapper` ↔ `DirectoryTree` 三向双向映射树
  - [x] `MetaCache` 微秒级 TTL 属性与目录缓存
  - [x] `DataCache` 本地 LRU 磁盘缓存与流式下载
- [x] **第三阶段：FUSE 核心功能与挂载（第 7 - 9 周）** 🚧 *(当前版本进度)*
  - [x] **第 7 周**：`MCloudFuseOps` 只读挂载与文件目录浏览 (`ls -la`, `cat`)
  - [ ] **第 8 周**：写回缓存 (Write-Back) 与文件写回上传 (`mknod`, `write`, `release`)
  - [ ] **第 9 周**：回收站删除对接 (`unlink`, `rmdir`)[cite: 1]
- [ ] **第四阶段：崩溃恢复与压测交付（第 10 - 12 周）**
  - [ ] 异常断连恢复与持久化缓存恢复
  - [ ] POSIX 强压测试与自动化挂载服务打包

---

## 📦 依赖配置与安装

### 1. 环境依赖

* Linux (支持 FUSE3)
* Python 3.10+
* `fuse3` 系统软件包

```bash
# Ubuntu / Debian 安装 FUSE3 依赖
sudo apt-get update
sudo apt-get install -y fuse3 libfuse3-dev