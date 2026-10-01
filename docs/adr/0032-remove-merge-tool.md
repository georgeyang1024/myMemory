# ADR-0032 · 移除 merge 工具（连实现）

**状态**：提案 · 2026-10-01（同一轮 grilling 的追加决策）

## 决策

MCP 工具面移除 `merge`，**连实现一起删**（`tool_merge` / `run_merge` /
`MERGE_DESCRIPTION` / `writer.merge_memory` / 相关测试）——与 0.2.0 删
status.json 同风格，代码不留尸体。

- 合并工作流的替代路径：`search` / `get-document` 读两篇 → `save` 合并稿
  （并入段格式由写入方自拟）→ `delete` 源（需 `allow_mcp_delete: true`）；
  或人工在编辑器里做。**不提供替代工具**——实践反馈：merge 基本没什么作用、
  很少用到。
- **断路器语义收窄不改名**：`allow_mcp_delete` 原本同时管 `delete` 与
  `merge`（因为 merge 会删源）；merge 消失后只管 `delete`。字段名不改，
  避免又一次配置 breaking；GLOSSARY 与工具描述措辞同步。

## 理由

- 低频无用：§15 上线后实践反馈几乎不用，工具面每多一项都是 LLM 的误用面。
- merge 删源不可逆，风险大于便利；替代路径（读→写→删）全程经既有工具、
  每步可审计，只多两步。
- 不做"不删源的合并工具"：那个场景由 `save` + `replace` 已覆盖大半，
  为低频需求再加一项工具与"简化工具面"的方向矛盾。

## 后果

- 工具数：断路器关闭时 7 个（不变）、开启时 9 → **8** 个；契约测试基线同步。
- `REQUIREMENTS §15` 中 merge 的决策（并入删源、断路器同管）由本 ADR 终结，
  §15 作为历史记录保留；`agent_mtime` / editor 的写入来源清单里去掉 merge
  （见 [ADR-0031](0031-editor-attribution.md)）。
