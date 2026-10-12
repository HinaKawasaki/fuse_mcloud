# 事故复盘：目录列举重复与「串台」

## 现象

挂载后 `ls` 一个目录，输出里出现大量重复行（`zotero`、`mozilla`、`16` 各重复十几次），
并且根目录/子目录里混进了**整个云盘其它位置**的文件；随手 `cd` 进任意一个名字都能成功。
原始终端记录见仓库外的 `debug/log_repeat_location.log`。

## 根因（三处缺陷叠加）

### 1. 用「全盘搜索」当「目录列举」

云盘的搜索接口在不传目录 ID 时返回**整个盘**的文件。而 `readdir` / `lookup` 当时调用
`wrapper.search_or_list_files(limit=200)` 时**没有传当前目录的 fileId**，
`DirectoryTree.populate_directory` 又按「父路径 + name」把这些条目拍平到当前目录，
于是每个目录都能看到全盘内容。

### 2. 同名条目没有去重

云盘允许同名文件，POSIX 目录不允许。`populate_directory` 把接口返回的每一条都 `append`
进结果列表，`readdir` 直接把该列表原样吐给内核；同名的第 N 条与第 1 条路径相同、inode 不同，
于是 `ls` 出现重复行。

### 3. 分页 `next_id` 不稳定

子节点存放在无序 `set` 中，却用 inode 作为 `next_id` 并据此跳过。pyfuse3 要求 `next_id`
唯一标识位置且「不得重复、不得跳过」；无序集合无法保证。

## 修复

| 位置 | 改动 |
| --- | --- |
| `client/wrapper.py` | 新增 `list_directory(dir_file_id)`：按目录 `includeFileIds` 列举并**自动翻页**，按 fileId 去重 |
| `mapping/id_map.py` | 新增 `get_children_inodes`（按 inode 升序）；修正父节点归属与路径冲突处理 |
| `mapping/tree.py` | `populate_directory` 目录内**按名字唯一化**；对账清理云端已删除的陈旧子节点 |
| `fuse/fuse_ops.py` | `readdir`/`lookup` 改用当前目录 `file_id` 列举；子节点按 inode 升序、以 inode 为 `next_id`；阻塞 I/O 放入线程 |

## 验证

- `tests/test_readdir.py`：同名去重、陈旧清理、按目录列举、分页每条只出现一次、
  `lookup` 只加载目标目录。
- `tests/integration/fuse_mount_smoke.py`：**真实内核挂载**后用 `ls` / `cat` 验证：
  根目录 4 项无重复、子目录只列自己的 3 项、同名文件按目录区分。
- `tests/integration/posix_stress.py`：220 项大目录连续列举 20 次结果一致；800 个文件 `find`
  递归计数正确。
- 真机：官方 CLI `list` 与本地列举完全一致。

## 教训

1. **接口语义要按最小权限使用**：搜索接口能“看到全盘”，不代表它可以当目录列举用。
   现在 `search_or_list_files` 这类危险入口已经被删除，只保留 `list_directory`。
2. **POSIX 约束必须在唯一入口处强制**：同名去重放在 `populate_directory`，
   所有目录视图（`readdir`/`lookup`/对账）就都不会再产生重复。
3. **内核契约要当契约看**：`next_id` 的单调性不是“最好这样”，而是“不这样就会重复/丢条目”。
4. **真实内核挂载测试不能省**：纯逻辑单测很难暴露“重复返回”这类内核侧行为，
   后来的冒烟测试（真实挂载 + `ls`/`cat`）才把它钉死。
