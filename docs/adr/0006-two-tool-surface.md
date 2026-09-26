# ADR-0006 · 工具面：最小工具面，项目前缀命名

**状态**：已采纳 · 2026-09-02
**修订**：2026-09-17 —— [ADR-0014](0014-write-tool-boundary.md) 加入第三个工具
`myMemory-save`。本 ADR 的"只暴露 2 个"已被取代，但它真正的主张——
**工具面要小，且每多一个都必须自证**——未变，下文的取舍逐条仍然有效。
**修订**：2026-09-24 —— 新增 `myMemory-list-workspaces` 与 `myMemory-recent`，工具面为 5 个；配置类工具不进 MCP，见 [ADR-0017](0017-config-file-and-cli.md)。
**修订**：2026-09-24（二）—— 工具名去掉 `myMemory-` 前缀：客户端已以服务名做命名空间（`mcp__myMemory__save`），前缀重复。

## 背景

工具面是服务对 LLM 的全部语义契约。多一个工具就多一份维护与误用面，
少一个就迫使 LLM 绕路。参考实现 `mobileKnowledgeBase` 暴露 6 个工具。

## 决策

只暴露检索所必需的 2 个工具：

- `myMemory-search(query, limit)`
- `myMemory-get-document(path, offset, limit)`

命名带项目前缀，避免同一 Agent 同时挂载多个知识库 MCP 时产生歧义。

> 2026-09-17 增补：`myMemory-save(category, filename, content)` 是第三个工具。
> 它不是对本决策的松动——写入是一项新需求，无法由既有两个工具表达，
> 其自身的取舍另见 ADR-0014。

## 理由

**保留 `get-document` 是硬性要求**：[ADR-0005](0005-fixed-window-chunking.md) 采用固定
窗口切块，会把会议决策从中间切断，回原文读取是唯一补救路径。这两个决策绑定。

**砍掉 `kb_list`**：分类目录名参与分词（[ADR-0012](0012-path-and-term-tokenization.md)），
拿分类名当检索词就能列举该分类下有什么，专门的列表工具冗余。

**砍掉 `kb_status`**：改为在**每次 search 响应中附带** `index.built_at` /
`doc_count` / `chunk_count`。新鲜度信号照样送达，且不占工具位、不需额外一次调用。

**砍掉 `scope` 参数**：不给 LLM 手动收窄检索范围的手柄，由 BM25 分数自然排序。
减少一个 LLM 可能误用的旋钮。

**不采纳 `kb_evidence` / `kb_answer_status`**：这两个是异步 LLM 合成的配套，
已被 [ADR-0003](0003-evidence-not-answers.md) 排除。

**不采纳强制 `member` 审计字段**：`mobileKnowledgeBase` 要求该字段做使用审计。
本服务免鉴权且面向 Agent，强制字段只会增加调用失败率，收益（谁在用）不足以抵偿。

## 后果

- 工具面极小，LLM 几乎不可能误用
- 新鲜度与文件清单能力以"信息内嵌"而非"独立工具"的方式提供
- 此后每提议一个新工具，都要先回答"为什么它不能由已有工具表达"
