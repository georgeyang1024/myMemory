# myMemory — 架构设计

> 人与 AI 共同记忆的 MCP 检索与写入服务（多 source），版本 **0.1.0**
> 状态：已实施并验证通过（2026-09-02 首版检索服务；2026-09-17 改造为可写的个人记忆库；
> 2026-09-24 0.1.0：多 source、writable 字段、config.json + config.py、最近编辑列表、
> 索引缓存与增量更新、挂载盘掉线处理、工具短名、单平台运行。需求见 [REQUIREMENTS](REQUIREMENTS.md)）

> **本文档描述现状。** 本项目的由来、以及为什么 ADR-0002/0004/0011/0012 里
> 还留着另一个语料的实测数字，见 [ADR-0000](adr/0000-lineage.md)。

---

## 1. 目标与非目标

### 目标

把若干记忆目录（**source**：个人、团队、公司……）变成一个**局域网内、免鉴权、
HTTP 可访问的 MCP 服务**，让任意 LLM Agent（Claude Code、Codex 等）能够：

1. 跨 source 检索并按需读取已有记忆；
2. 把值得长期留存的新信息写进某个可写的 source，并让它随后可被检索到；
3. 查看最近更新了哪些文档、是人改的还是 agent 写的。

即使某个 source 在网络盘上、盘掉线了，已索引的内容仍可检索与读取；重启要快。

### 非目标（明确排除）

| 非目标 | 排除理由 | 决策 |
|---|---|---|
| 服务端 LLM 答案合成 | 消费方本身就是 LLM；免鉴权服务持有 API Key 不可接受 | [ADR-0003](adr/0003-evidence-not-answers.md) |
| 向量检索 / Embedding | 语料以专有名词为主，BM25 更准。**本系统没有向量** | [ADR-0002](adr/0002-bm25-over-vectors.md) |
| **经 MCP 管理 source 或配置** | 免鉴权下等于任何调用方都能解除只读、挂载任意目录读取 | [ADR-0017](adr/0017-config-file-and-cli.md) |
| 配置热加载 | 配置修改一律重启生效；热操作只有 reindex | [ADR-0017](adr/0017-config-file-and-cli.md) |
| 每 source 独立索引 | 先用统一索引，接受 IDF 相互影响 | [ADR-0015](adr/0015-workspaces.md) |
| 跨平台运行 / 路径互转 | 只在当前系统运行；config.json 写什么用什么 | [ADR-0021](adr/0021-single-platform.md) |
| **删除 / 改名 / 移动记忆** | 代码中不存在这三类路径 | [ADR-0014](adr/0014-write-tool-boundary.md) |
| 覆盖时留备份 | 备份文件会进索引、出现在检索结果里 | [ADR-0014](adr/0014-write-tool-boundary.md) |
| 编辑日志 / 版本历史 | 只要编辑列表，每文件一条 | [ADR-0018](adr/0018-edited-by-via-status-file.md) |
| **MCP 写入**嵌套分类目录 | 只约束工具写入——索引与人工编辑对任意层级都不设限 | [ADR-0014](adr/0014-write-tool-boundary.md) |
| 部署与运维 | 只交付源码，运行由使用方自行执行 | [ADR-0008](adr/0008-machine-agnostic-delivery.md) |
| 鉴权 / 限流 | 需求明确要求免授权 | [ADR-0010](adr/0010-indexed-set-membership.md) |

> 曾经的非目标"索引持久化""增量索引"已被 [ADR-0019](adr/0019-index-cache-incremental.md) 推翻：
> 挂载部门知识库后全量构建要 4 分钟，前提不再成立。

---

## 2. 语料画像

| 指标 | 说明 |
|---|---|
| 语料来源 | `config.json` 里的全部 source；每个 source 一个目录，可在任意位置（含网络挂载盘） |
| 目录约束 | 互不重叠、不嵌套（能解析真实路径时按真实路径；访问不到时按原样路径；不区分大小写） |
| 只读 | 配置 `"writable": false`；照常索引与读取，服务永不写入。与名称无关 |
| 可用性 | 盘根访问不到 = 掉盘（`disk_offline`）；盘在目录不在 = 目录不存在（`dir_missing`）。探测按平台分派：Windows 探盘根 anchor（`Z:\`、`\\server\share\`）；Linux 挂载点盘根恒为 `/`，改为带 3 秒外置超时（`timeout(1)`）的 `ls`，目录不可达时再探父目录区分掉盘与目录被删 | [ADR-0020](adr/0020-mounted-disk-offline.md) |
| 纳入索引的扩展名 | `.md`、`.txt`（`save` 只写 `.md`） |
| 排除 | source 目录之外的一切，以及任意层级的隐藏目录 |
| 文档身份 | `(source, path)`；path 相对该 source 目录，不含 source 名 |
| 路径形态（MCP 写入） | `<分类>/<文件名>.md`，分类为空时 `<文件名>.md` |
| 路径形态（索引与人工） | **任意层级** |

**人工操作不受 MCP 与 RAG 约束**：你在编辑器里建几层目录、怎么组织、
直接改哪一篇，索引都照收、轮询都照更新。一级分类只是约束 Agent 自动写入的约定。

source 名、分类名与文件名都参与分词（[ADR-0012](adr/0012-path-and-term-tokenization.md)）。

### 记忆的结构不均匀性（已知约束）

本设计采用统一固定窗口切块（800/120），**明确接受**长文被切断语义边界的损失，
由 `get-document` 回原文补齐上下文。详见 [ADR-0005](adr/0005-fixed-window-chunking.md)。

### 工程约束

分类名与文件名普遍是中文，且允许含空格与括号。写入侧它们**不是路径**，而是过白名单的标识符：
白名单不含 `/` 与 `\`，`..` 与首尾的 `.` 另行禁止。见 [ADR-0014](adr/0014-write-tool-boundary.md)。

---

## 3. 性能基线（实测）

**早期基线**（Codespace 容器，265 文件，本地盘）：冷启动 2.4–3.1 s，单次查询 2–12 ms，RSS 约 199 MB。
这组数字曾是"无需持久化、无需增量"的地基。

**部门知识库上线后**（Windows，网络挂载盘）：

| 规模 | 扫描网络盘 | 读取正文 | jieba 分词 | 全量合计 |
|---|---|---|---|---|
| 1222 文件 / 18,414 块 | 62 s | 120 s | 68 s | 210 s |
| 1462 文件 / 21,407 块 / 1423 万字符 | — | — | 121 s | ≈ 4 min |

索引缓存（1462 文件）：

| 部分 | 体积 | 加载 |
|---|---|---|
| 正文 | 19 MB | 0.06 s |
| 块偏移 | 0.6 MB | 0.01 s |
| 分词结果 | 36 MB | 2.7 s |
| BM25 模型 | 23 MB | 0.7 s（不存则现场重建 4 s） |

因此：**有缓存的启动 ≈ 4 s 即可服务**；无变化时的增量校验只付扫描的代价，不读正文、不分词。

---

## 4. 组件结构

```
                    局域网客户端                                  人（本机）
        ┌──────────────┬──────────────┬───────────────┐     ┌──────────────┐
        │ Claude Code  │  其他 Agent   │  curl / 浏览器 │     │  config.py   │
        └──────┬───────┴──────┬───────┴──────┬────────┘     └──┬───────┬───┘
               │ MCP          │ MCP          │ REST            │改配置  │reindex / restart
               ▼              ▼              ▼                 ▼       │
        ┌────────────────────────────────────────────────┐  config.json│
        │           uvicorn (Starlette ASGI)             │◄────────────┘
        ├──────────────────────┬─────────────────────────┤  POST /reindex；
        │  POST/GET  /mcp      │  GET /health  /search   │  restart 调 run.py
        │  (MCPServer 挂载)     │  GET /recent            │
        │                      │  POST /reindex          │
        ├──────────────────────┴─────────────────────────┤
        │                  server.py                     │
        │  search / get-document / save /                │
        │  list-sources / recent（均带 writable）       │
        └──┬─────────────────────┬───────────────────────┘
           │ 读取（只读引用）       │ 写入 + mark_agent + 请求刷新
           ▼                     │
        ┌───────────────────────────────┐        index.cache
        │           index.py            │◄──────（启动加载 / 刷新后写回）
        │ IndexSnapshot（不可变）         │
        │  entries: (ws,path)→FileEntry │  FileEntry = 正文 + 块偏移 + 分词 + agent_mtime
        │  availability: ws→可用性        │
        │  bm25 / chunks / indexed_paths │
        │ IndexHolder：启动、刷新、轮询    │
        └──────────────┬────────────────┘
                       │ refresh()：按文件增量；掉盘保留；目录删除清除
                       ▼              ┌────────────────┐
        ┌──────────────────────────┐  │   writer.py    │
        │ corpus.py：数据形态、切块   │  │ 校验、只读/可用  │
        └──────────────────────────┘  │ 性拒写、落盘     │
                       │              └───────┬────────┘
                       ▼                      ▼
        ┌──────────────────────────────────────────────┐
         │  storage.py：Storage / LocalStorage           │  ← 预留 git / http / oss
         │  probe()：Windows 探盘根；Linux 3s 超时 ls 探挂载点 │
         │           （父目录可达 = 目录被删，否则掉盘）        │
        └──────────────────────┬───────────────────────┘
                               ▼
          <source 目录>/[<分类>/]<文件名>.md   × N 个 source
```

### 模块职责

| 模块 | 职责 | 不做什么 |
|---|---|---|
| `src/config.py` | 读 config.json，校验并构造不可变配置；source 规则（含 `writable`）；字符白名单；`domain_terms` 词表。仅标准库，根目录 CLI config.py 与 run.py 复用 | 不检查目录是否存在 |
| `storage.py` | 存储接口与 `LocalStorage`：可用性探测（Windows 探盘根；Linux 带 3 秒超时的 `ls`，超时不等遗留子进程、其退出前不再起新探测）、列文件、读、写；只读与目录不存在的最后一道闸 | 不认识索引；`iter_files`/`read_text` 无超时，hard mount 途中掉盘仍可能阻塞扫描 |
| `corpus.py` | DocMeta / Chunk 数据形态、固定窗口切块 | 不碰磁盘，不认识 BM25 |
| `index.py` | jieba 分词、按文件增量刷新、掉盘保留、BM25、索引缓存读写、查询打分、轮询、快照原子替换 | 不认识 HTTP，不做参数校验 |
| `writer.py` | **唯一的记忆写入路径**：source/分类/文件名校验、只读与可用性拒写、落盘 | 不认识索引，不认识 HTTP |
| `server.py` | MCP 工具、REST 端点、有效 `writable` 计算、入参校验、响应裁剪 | 不做检索逻辑，不做落盘逻辑 |
| `src/main.py` | 组装：读配置 → 探测可用性并警告 → 加载缓存或全量构建 → 启动轮询 → 起传输层 | 不含业务逻辑 |
| `run.py` | 启动入口：`.venv`、依赖、前台/后台启停；向子进程钉死绝对 `MEMORY_CONFIG` | 不管配置内容 |
| 仓库根 `config.py`（CLI） | 人工管理 source、刷新周期、reindex、restart | 不起服务 |

---

## 5. 索引生命周期

```
启动
 └─► Config.load()                 MEMORY_CONFIG 或 ~/.myMemory/config.json；缺失则交互式建档（ADR-0025），无法交互则报错
     └─► 逐个 probe() source     不可用只警告，不失败
         └─► load_cache()          格式 + 配置指纹校验
              ├─ 命中 → 直接构成快照（含序列化的 BM25），立即监听；verifying = true
              │        └─► 后台 refresh()（增量）→ 原子替换 → 写回缓存；verifying = false
              └─ 未命中 → build()（全量）→ 写缓存 → 监听
     └─► 轮询线程：每 poll_interval 秒 request_rebuild("轮询")

refresh(previous, full)，对每个 source：
  probe() 掉盘            → 旧条目原样保留（不更新、不删除）
  probe() 目录不存在       → 条目全部移除
  可用 → 扫描 → 扫描后再 probe()（盘在途中掉了 → 按掉盘处理）
       → (mtime, size) 未变且非 full：复用条目；否则重读 + 切块 + 分词
       → 消失的文件移除；agent 标记合并进条目
  不在配置里的 source → 移除
  注：Linux 上 probe 最坏 ~8s/个 source（根+父各 4s），仅挂载死掉的首次发生；
      每轮 refresh 新建 LocalStorage 实例，挂载一直死着时每轮仍等 ~8s（后台线程，不挡服务）
 然后：内容签名未变 → 复用 BM25 与块列表；变了 → 重建 BM25（统计量是全局的）

save
 └─► writer.save_memory()   校验 → 只读 / 可用性拒写 → 落盘，返回落盘 mtime
     ├─► holder.mark_agent()        记下 (source, path) → mtime
     └─► holder.request_rebuild()   增量刷新，合并并发请求

POST /reindex[?full=1]（config.py reindex [--full]）
 └─► holder.request_rebuild(full)   同一条路径；full 请求会让下一轮升级为全量
```

**并发模型**：快照是**不可变对象**，请求处理路径只读取全局引用一次并在整个请求内持有。
所有刷新都走 `request_rebuild`：同一时刻只有一个刷新线程，并发请求合并；缓存只在这条线程里写，
先写临时文件再原子替换。

**失败策略**：刷新抛异常时**保留旧快照不替换**，记录 ERROR 日志，下一轮重试，并**务必复位
`_rebuilding` 标志**。缓存缺失、损坏或指纹不符时告警并全量构建，不阻断服务。

---

## 6. 对外契约

版本 **0.1.0**。完整字段见 [REQUIREMENTS](REQUIREMENTS.md) §6。
工具名不带前缀，客户端全名形如 `mcp__myMemory__save`。

**有效可写 `writable`** = 配置 `writable` 为 true **且** 存储为 local **且** source 当前可用。
它出现在 search、recent、get-document、list-sources 与 `/health` 的每条输出里。

### 6.1 `search(query, limit=5, source="")`

BM25 全文检索，默认跨全部 source；`source` 可选。

```json
{
  "index": {"built_at": "2026-09-24T10:30:00+08:00", "doc_count": 42, "chunk_count": 310},
  "query": "BLE 配对流程", "source": null, "total_matched": 12, "returned": 5,
  "results": [
    {"source": "team", "path": "技术/BLE配对流程.md", "writable": true, "score": 18.42,
     "chunk_index": 3, "char_start": 2040, "char_end": 2840,
     "snippet": "…", "snippet_truncated": false}
  ]
}
```

实现要点（沿用）：路径与 source 名参与检索但不进片段正文（[ADR-0012](adr/0012-path-and-term-tokenization.md)）；
命中按查询词实际出现判定（[ADR-0013](adr/0013-term-presence-over-positive-score.md)）；
同一文档最多先占 2 个结果位；入参越界钳制而非报错。

### 6.2 `get-document(source, path, offset=0, limit=40000)`

`(source, path)` 必须精确命中已索引集合。响应带 `writable` 与 `stale`
（掉盘时内容来自缓存，`stale: true`）。拒绝时返回 `suggestions: [{"source", "path"}]`。

### 6.3 `save(source, filename, content, category="")`

`source` 必填、无默认值，必须有效可写；否则返回 `{"saved": false, "error", "writable_sources"}`，
且不会重建被删除的目录。`category` 可为空。同路径整篇覆盖；成功后记 agent 标记并异步增量刷新。

### 6.4 `list-sources()`

`{"sources": [{"name", "writable", "available", "unavailable_reason", "doc_count", "description"}]}`，
**不含目录路径**。MCP instructions 保持静态，只写规则，不列具体名称。

### 6.5 `recent(limit=10, source="")`

默认 10、最多 20 条，按文件 mtime 倒序，每文件一条：
`{"source", "path", "writable", "updated_at", "size", "edited_by": "agent" | "scan"}`。
`edited_by` 由条目的 `agent_mtime` 判定（[ADR-0018](adr/0018-edited-by-via-status-file.md)）。

### 6.6 REST 端点

| 端点 | 用途 |
|---|---|
| `GET /health` | 版本、索引规模、`rebuilding`、`verifying`、当前配置、`config_file`、`sources`（含 `dir`；MCP `list-sources` 不含） |
| `GET /search?q=…&limit=…&source=…` | 与 `search` 完全一致的 JSON |
| `GET /recent?limit=…&source=…` | 与 `recent` 完全一致的 JSON |
| `POST /reindex[?full=1]` | 刷新索引（合并并发请求），返回 `{"index_refresh", "mode"}` |

---

## 7. 配置

全部配置在 `config.json`：默认位于 **`~/.myMemory/`**，唯一环境变量 `MEMORY_CONFIG` 覆盖其位置；
`index.cache` 与 `logs/`（日志、PID）都放在配置文件所在目录，代码目录不落任何运行数据；
配置不存在时在终端**交互式建档**（ADR-0025）：询问记忆目录，生成默认值 + source `memory`
（描述"默认记忆源"）；无法交互（stdio、后台）时报错"未指定记忆存储"。
修改一律**重启生效**。人可直接编辑，或用 `config.py`（source 增删改含 `--readonly` / `--writable`、
`poll_interval`）。字段与默认值见 README「配置」一节。

`index.cache` 与 config.json 同目录，带格式版本与配置指纹（切块参数、扩展名、词典、版本）。

`run.py` 解析出绝对路径后通过 `MEMORY_CONFIG` 传给子进程——后台模式的子进程工作目录是 `mcp/`。

---

## 8. 安全模型

服务**无鉴权**，这是明确的需求前提。安全设计的全部重心在于：
**即使任意局域网用户可以任意调用，也不可能造成越权读取、越界写入或服务损害。**

### 8.1 配置面：不经 MCP

source 决定了"什么能被读、什么能被写"。它若能经 MCP 修改，调用方就能挂载任意目录、
把只读改回可写、删掉别人的库。因此 source 的增删改与只读切换只能由人完成
（config.py 或直接编辑 config.json）。见 [ADR-0017](adr/0017-config-file-and-cli.md)。

`POST /reindex` 只触发刷新、与在跑的那轮合并，无写入能力。

### 8.2 读取面：`(source, path)` 集合成员判断

`get_document` 只做一次集合成员判断，不做路径解析——路径穿越、符号链接逃逸、编码绕过
在语义上无法发生。见 [ADR-0010](adr/0010-indexed-set-membership.md)。

### 8.3 写入面：只读、可用性、标识符、一级目录

- **只读**：`writable: false` 时 writer 拒绝，storage 层落盘前再拦一次；非 local 存储一律按只读
- **可用性**：掉盘或目录不存在时拒写；storage 层绝不用 `mkdir(parents=True)` 重建被删除的 source 目录
- **分类名与文件名是标识符**：白名单不含 `/` 与 `\`，`..`、首尾 `.`、Windows 保留名另行禁止
- 同路径整篇覆盖，不可撤销、无备份；空正文一律拒绝；不存在删除、改名或移动文件的代码路径

详见 [ADR-0014](adr/0014-write-tool-boundary.md) 与 [ADR-0022](adr/0022-writable-field.md)。

### 8.4 缓存文件

`index.cache` 是 pickle。它由本服务写在 config.json 旁边，只从这个位置读；
能改写它的人本就能改写 config.json 与代码本身，因此不单独设防。

### 8.5 资源保护

| 限制 | 值 |
|---|---|
| search 返回条数 | ≤ 20（默认 5） |
| recent 返回条数 | ≤ 20（默认 10） |
| 片段长度 | ≤ 1200 字符 |
| get_document 单次返回 | ≤ 40,000 字符 |
| query 长度 | ≤ 500 字符 |
| save 正文长度 | ≤ 100,000 字符 |
| 分类名 / 文件名长度 | ≤ 64 / ≤ 120 字符 |

### 8.6 DNS Rebinding 保护：不启用

| 陷阱 | 现象 | 正确做法 |
|---|---|---|
| 传入 `allowed_hosts` 为空的 `TransportSecuritySettings` | **拒绝所有请求**（全站 400） | "不保护"必须传 `transport_security=None` |
| 只传 `None`、省略 `host` 参数 | SDK 自动装上 localhost-only 白名单，`/mcp` 从非 localhost 访问返回 421 | 显式传 `host=config.host` |

回归测试见 `tests/test_transport_security.py`。

---

## 9. 已知取舍与风险

| # | 取舍 / 风险 | 影响 | 缓解 | 决策来源 |
|---|---|---|---|---|
| 1 | 统一索引跨 source | 某个 source 的大量重复文本会拉低高频词区分度 | 可按 source 过滤检索 | [ADR-0015](adr/0015-workspaces.md) |
| 2 | BM25 无语义改写能力 | 换个说法就搜不到 | 消费方是 LLM，可自行多轮改写 | [ADR-0002](adr/0002-bm25-over-vectors.md) |
| 3 | 固定窗口切断语义边界 | 长记忆的片段可能缺上下文 | `get-document` 回原文补齐 | [ADR-0005](adr/0005-fixed-window-chunking.md) |
| 4 | 启动先用缓存服务 | 后台校验完成前（网络盘约 1 分钟）内容可能是上次关闭时的样子 | `/health` 的 `verifying` 如实标出 | [ADR-0019](adr/0019-index-cache-incremental.md) |
| 5a | 可用性只在刷新时更新 | 两次刷新之间盘掉了，list-sources 可能仍显示可写 | save 失败时被动探测该 source，即时更新快照中的可用性并触发增量刷新 | [ADR-0020](adr/0020-mounted-disk-offline.md) |
| 5 | 掉盘时内容来自缓存 | 可能不是磁盘上的最新版本 | `get-document` 的 `stale`、`available: false` | [ADR-0020](adr/0020-mounted-disk-offline.md) |
| 6 | 目录不存在只警告 | config.json 路径拼错不会让启动失败 | CLI 添加时校验；`/health` 的 `dir_missing` | [ADR-0020](adr/0020-mounted-disk-offline.md) |
| 7a | 全文缓存上限 `max_cached_docs`（默认 1000，LRU） | 不在缓存里的全文与片段要现场读盘（网络盘慢一点）；掉盘时读不到 | 所有文档照常可检索；缓存随 index.cache 持久化 | [ADR-0024](adr/0024-max-cached-docs.md) |
| 7 | 缓存约 80 MB / 1500 文件，常驻内存随之增加 | 磁盘与内存占用 | 分词结果驻留（`sys.intern`）；缓存可随时删除重建 | [ADR-0019](adr/0019-index-cache-incremental.md) |
| 8 | agent 标记在缓存写回前只在内存 | save 后到下一次缓存写回之间崩溃会丢这一条标记 | save 立即触发刷新，窗口只有数秒 | [ADR-0018](adr/0018-edited-by-via-status-file.md) |
| 9 | 多个 stdio 进程共用一份缓存 | 最后写回者生效，别人的 agent 标记可能被覆盖 | 需要准确时用 HTTP 共享一个实例 | [ADR-0019](adr/0019-index-cache-incremental.md) |
| 10 | 配置修改需重启 | 改完 source 不立即生效 | `config.py … --restart`；有缓存时重启很快 | [ADR-0017](adr/0017-config-file-and-cli.md) |
| 11 | 免鉴权且**可写** | 任意局域网用户可读全部 source，并往可写 source 投放文件 | 本机使用时 `host` 设 `127.0.0.1`；敏感目录配为只读 | [ADR-0014](adr/0014-write-tool-boundary.md) |
| 12 | 依赖 `mcp` SDK 2.x API | SDK 破坏性升级会导致服务失效 | `requirements.txt` 锁死精确版本号 | [ADR-0011](adr/0011-pinned-deps-both-platforms.md) |
| 13 | 记忆文件写入非原子（直接 `write_text`） | 落盘途中崩溃/断电会留下半截文件并被下一轮索引进去；config.json 与 index.cache 均为原子写，唯独正文没有 | 写入窗口极短；真在意的内容进版本库 | 暂不处理（2026-09-26 审查标记为已知） |
| 14 | 首轮构建时扫描失败但盘根可访问 | 该 source 被标为"可用"却没有任何条目，直到下一轮刷新 | 首次启动的短暂窗口，能自愈 | 暂不处理（2026-09-26 审查标记为已知） |

---

## 10. 决策索引

| ADR | 标题 | 结论 |
|---|---|---|
| [0000](adr/0000-lineage.md) | 项目由来 | 由 `ble-sec-kb` 改造而来 |
| [0001](adr/0001-corpus-scope.md) | 记忆边界 | 独立的 `memory/`（已被 0015 取代） |
| [0002](adr/0002-bm25-over-vectors.md) | 检索内核 | BM25 + jieba，不引入向量 |
| [0003](adr/0003-evidence-not-answers.md) | 服务语义 | 纯检索返回证据 |
| [0004](adr/0004-md-txt-only.md) | 文件类型 | 仅 .md/.txt |
| [0005](adr/0005-fixed-window-chunking.md) | 切块策略 | 统一固定窗口 800/120 |
| [0006](adr/0006-two-tool-surface.md) | 工具面 | 最小工具面（现为 5 个），工具名不带前缀 |
| [0007](adr/0007-in-memory-index-with-polling.md) | 索引生命周期 | 全内存快照 + 轮询 + 原子替换（持久化部分被 0019 取代） |
| [0008](adr/0008-machine-agnostic-delivery.md) | 交付边界 | 只交付源码，不含部署 |
| [0009](adr/0009-official-sdk-streamable-http.md) | 实现栈 | 官方 SDK + Streamable HTTP + REST 端点 |
| [0010](adr/0010-indexed-set-membership.md) | 读取边界 | 索引集合成员校验，元素为 (source, path) |
| [0011](adr/0011-pinned-deps-both-platforms.md) | 交付形态 | 锁定精确版本（双平台部分被 0021 取代） |
| [0012](adr/0012-path-and-term-tokenization.md) | 检索词构造 | 路径入索引、长标识符展开、日期原子化 |
| [0013](adr/0013-term-presence-over-positive-score.md) | 命中判定 | 按查询词实际出现 |
| [0014](adr/0014-write-tool-boundary.md) | 写入边界 | 标识符而非路径，一级分类，同路径覆盖 |
| [0015](adr/0015-workspaces.md) | 多 source | 一个 source 一个目录，统一索引，身份 (source, path) |
| [0016](adr/0016-readonly-by-name-prefix.md) | 只读（旧） | 名称前缀 `readonly/`（已被 0022 取代） |
| [0017](adr/0017-config-file-and-cli.md) | 配置 | config.json + config.py，不开放 MCP 配置工具 |
| [0018](adr/0018-edited-by-via-status-file.md) | 最近编辑列表 | scan / agent（status.json 已并入缓存） |
| [0019](adr/0019-index-cache-incremental.md) | 索引缓存 | 启动先用缓存服务，后台增量校验 |
| [0020](adr/0020-mounted-disk-offline.md) | 挂载盘掉线 | 掉盘不更新不删除；Windows 判盘根，Linux 用 3 秒超时 ls 判挂载点 |
| [0021](adr/0021-single-platform.md) | 运行环境 | 只在当前系统运行，`.venv` |
| [0022](adr/0022-writable-field.md) | 只读 | 配置字段 `writable`，输出带有效可写 |
| [0023](adr/0023-rename-workspace-to-source.md) | 术语 | workspace 更名为 source |
| [0024](adr/0024-max-cached-docs.md) | 内存上限 | 常驻内存的全文最多 max_cached_docs 篇（LRU） |

术语定义见 [GLOSSARY.md](GLOSSARY.md)。
