# ADR-0034 · 工具输出：原文不进 JSON

**状态**：已采纳并实施 · 2026-10-15（0.6.0）
**修订**：[ADR-0006](0006-two-tool-surface.md) 的工具返回形态（仅 `search` / `get-document`）

## 背景

`search` 与 `get-document` 原先把整个 payload `json.dumps` 成字符串返回，工具签名为 `-> str`。
两层转义叠在一起：

1. **原文作为 JSON 字符串的值**（`snippet` / `content`），引号、反斜杠、换行必须转义；
2. **SDK 自动结构化输出**：mcp 2.x 对返回 `str` 的工具默认开启结构化输出并
   `wrap_output`（`func_metadata.py` 的 `convert_result`），把整段 JSON 文本再包成
   `{"result": "<JSON 文本>"}`；客户端展示这份，原文的 `"` 成了 `\\\"`。

实际后果：调用方从片段/正文照抄 `old_string` 做 `replace`，带进多余反斜杠，命中 0 处。
只关掉第 2 层仍剩第 1 层——只要原文还在 JSON 里，就保证不了"复制即原文"。

## 决策

**原文不进 JSON；输出是单个文本块；元数据一行 JSON。**

- `get-document`：首行元数据（原字段去掉 `content`），第一个换行之后全部是正文原文。
  `json.dumps` 会转义值里的换行，元数据必然只占一行，按第一个换行切分无歧义。
  出错时只有一行 JSON。
- `search`：MIME 式分隔。

  ```
  {"index": …, "query": …, "total_matched": …, "returned": 2, "boundary": "<B>"}
  <B>
  {"n": 1, "source": …, "path": …, "char_start": …, "char_end": …, …}
  片段原文……
  <B>
  {"n": 2, …}
  片段原文……
  <B>
  ```

  `<B>` = `10 个 =` + ` myMemory-<15 位随机 hex> ` + `10 个 =`（46 字符，如
  `========== myMemory-7f3a9c2e41d0b8a ==========`），每次响应
  随机生成，若出现在任一片段里就重新生成，保证不撞。无命中或出错时只有首行。
- 两个工具 `structured_output=False`，不再生成 `{"result": ...}` 包装与 output schema。
- `run_*` 纯逻辑与 REST 端点不变（程序消费者仍拿完整 JSON）；其余工具不变（不含原文）。
- 换行口径：读取一律经通用换行（`storage.read_text`），`get-document` 正文与 `replace` 所见
  文本逐字一致；这是契约基准，不是磁盘字节。

## 考虑过的方案

- **只关 SDK 包装、保留 JSON**：剩一层标准转义，仍不能"复制即原文"。否决。
- **多个内容块**（元数据、原文各一块）：原文最干净，但客户端展示观感乱、部分客户端对多块
  的拼接/截断行为不一。否决，改单块。
- **search 固定分隔行 + 长度字段**：模型不会数字符，撞分隔行时人和模型都会看错。否决。

## 关于"原文不经 JSON 会不会在传输层出问题"

不会。MCP 的 JSON-RPC 传输层本来就把每个文本块作为 JSON 字符串编码、客户端对称解码；
这一层在改动前后都存在，改动只去掉了我们自己多加的编码层。契约测试经真实 Streamable HTTP
+ 官方客户端往返，样本含引号、反斜杠、字面 `\n` `\"`、`"}]}`、制表、ESC、NUL、U+2028、
emoji、零宽字符与仿冒边界行，原文逐字一致（`test_raw_text_survives_real_streamable_http_transport`）。

## 后果

- 契约测试锁住：单块、无结构化包装、正文与 `replace` 所见文本逐字一致、片段为原文切片、
  边界串足够长且撞车重生成、从 `get-document` 复制跨行含引号片段 `replace` 首次命中。
- 解析 MCP 工具 JSON 的客户端需要适配（破坏性），版本升至 0.6.0；仓内无此类消费者
  （`config.py` / `run.py` 走 REST `/reindex`、`/health`）。
