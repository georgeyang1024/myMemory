# ADR-0015 · 多 workspace：一个 workspace 对应一个目录

**状态**：已采纳并实施 · 2026-09-24
**取代**：[ADR-0001](0001-corpus-scope.md) 的"单一 `memory/` 根目录"
**修订**：2026-09-24（二）—— 目录不可用时启动只警告不失败，见 [ADR-0020](0020-mounted-disk-offline.md)。
**术语**：2026-09-24 起 workspace 更名为 source，见 [ADR-0023](0023-rename-workspace-to-source.md)；本文保留原措辞。

## 背景

单一 `MEMORY_ROOT` 无法同时容纳个人、团队、机构等不同来源的记忆，
`MEMORY_SCAN_DIRS` 只能挑根目录下的子目录，挂载盘上的目录无从纳入。

## 决策

- 引入 workspace：用户命名 + 任意位置的真实目录（原样使用，不做跨平台路径转换）。
- 目录之间不重叠、不嵌套；任一目录不存在则启动失败。
- 文档身份为 `(workspace, path)`，对外是两个独立字段，path 不拼接 workspace 名。
- 统一全量索引覆盖全部 workspace，search 默认跨库；接受 IDF 相互影响。
- 存储抽象为接口，目前只实现 `local`，预留 git / http / oss；配置保留 `type` 字段，非 `local` 存储接入后一律先按只读处理。

## 备选与取舍

- **workspace 名拼进 path**（`team/技术/x.md`）：一个字段即可定位，但 path 语义混杂；
  改为独立字段，get-document / save 必须显式传 workspace。
- **每 workspace 独立索引**：IDF 隔离、可单独刷新，但跨库排序需归一化。
  先求简单，规模变大后再调整。

## 后果

- ADR-0001"读写边界是同一条线"的主张在 workspace 粒度上保留：只读 workspace 另见 ADR-0016。
- ADR-0010 的集合成员判断不变，元素变为 `(workspace, path)`。
- 对外契约破坏性变更（0.1.0）。
