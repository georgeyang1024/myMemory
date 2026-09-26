# ADR-0016 · 只读 workspace 由名称前缀 `readonly/` 决定

**状态**：已采纳并实施 · 2026-09-24
**已被取代**：[ADR-0022](0022-writable-field.md)（2026-09-24 第二轮）——只读改为配置字段 `writable`。
**术语**：2026-09-24 起 workspace 更名为 source，见 [ADR-0023](0023-rename-workspace-to-source.md)；本文保留原措辞。

名称以 `readonly/` 开头的 workspace 即为只读（如 `readonly/company`），配置中不设 mode 字段；
普通名称不得以 `readonly` 开头、不得含 `/`。服务永不向只读目录写入，但照常索引、检索、读取，
且不修改磁盘权限。选前缀而非字段，是为了让"能不能写"在每条检索结果的 `workspace` 字段上
**一眼可见**，MCP 描述保持静态也能讲清规则（"`readonly/` 开头的不可写"）。
代价是切换只读等于改名，改名会改变文档身份；这只能由人经 CLI 完成（ADR-0017）。
曾考虑"单个虚拟 `readonly` workspace 挂多个目录"，因违背"一个 workspace 一个目录"而放弃。
