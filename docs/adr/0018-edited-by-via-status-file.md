# ADR-0018 · 最近编辑列表：用 status.json 区分 human / agent

**状态**：已采纳并实施 · 2026-09-24
**修订**：2026-09-24（二）—— status.json 删除，`agent_mtime` 并入索引缓存，见 [ADR-0019](0019-index-cache-incremental.md)。
**修订**：2026-10-01（三）—— 对外字段 `edited_by` 整体更名为 `editor` 并登记写入主体，判定机制不变，见 [ADR-0031](0031-editor-attribution.md)。

`myMemory-recent` 返回最近更新的文件（默认 10、最多 20，每文件一条，按 mtime 倒序），
不做编辑日志、不做内容描述（服务端无 LLM，见 ADR-0003）。更改类型 `edited_by` 的判定：
每次 `save` 成功后记录该文件落盘后的 mtime，查询时当前 mtime 与之相等为 `agent`，否则为 `human`。
记录存于 `config.json` 旁的 `status.json`，运行期只写（原子替换）、启动时读一次，
读取时丢弃已不存在的文件与 workspace。曾考虑纯内存标记，但重启后无法区分，
要么全标 `human`（错）要么引入 `unknown`（第三态）；持久化一个小文件代价更低。
不放进 workspace 目录：只读 workspace 不能写，也不应把服务内部文件混入用户目录。
