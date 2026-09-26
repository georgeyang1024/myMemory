# ADR-0022 · 只读改为配置字段 writable，不再用名称前缀

**状态**：已采纳并实施 · 2026-09-24
**取代**：[ADR-0016](0016-readonly-by-name-prefix.md)
**术语**：2026-09-24 起 workspace 更名为 source，见 [ADR-0023](0023-rename-workspace-to-source.md)；本文保留原措辞。

ADR-0016 用名称前缀 `readonly/` 表示只读，理由是"能不能写"在每条结果的 workspace 名上一眼可见。
代价在使用中显现：切换只读等于改名，改名又改变文档身份 `(workspace, path)`；名称里要为 `/` 开特例。
改为每个 workspace 的布尔字段 `writable`（省略即 true），名称与只读彻底解耦、一律不含 `/`。
"一眼可见"改由输出字段承担：search、recent、get-document、list-workspaces、`/health` 全部带 `writable`，
且取**有效可写**——配置可写、当前可用（未掉盘、目录存在）、存储为 local 三者同时成立。
CLI 的 `add` 必须显式 `--readonly` 或 `--writable`，`edit` 用这两个选项切换。
不做迁移：旧配置删除，由人按新格式重建。
