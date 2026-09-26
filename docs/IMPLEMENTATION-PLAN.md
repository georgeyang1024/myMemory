# myMemory — 实施计划（首版建设记录）

> ⚠️ **这是历史记录**：本文是 2026-09-02 建设本项目前身（一个团队技术知识库）
> 时的实施计划，其中的语料规模、实测数字与"目标语料 `raw/`"等描述**刻意未改写**——
> 它记的是当时按什么证据、分几步做的。检索引擎本身自那时起没有重写。
> 0.1.0（多 workspace、config.json）的实施清单见 [TASKS.md](TASKS.md)；
> 文中的 `.env` 与 `MEMORY_*` 环境变量已作废。
> 2026-09-17 改造为个人记忆库（改名 + 新增写入）的设计见
> [ADR-0000](adr/0000-lineage.md) 与 [ADR-0014](adr/0014-write-tool-boundary.md)。

> 注：文中 P5 的产出 `run.sh` / `run.ps1` 已于 2026-09-17 删除，
> 平台差异改由 `run.py` 内部判断，见 [ADR-0011](adr/0011-pinned-deps-both-platforms.md) 的修订说明。

> 前置阅读：[ARCHITECTURE.md](ARCHITECTURE.md) · [ADR 目录](adr/)
> 状态：**已完成**（2026-09-02）。P1–P5 全部执行并验证通过。
> 执行结果见 [RETRIEVAL-NOTES.md](RETRIEVAL-NOTES.md)。
> 日期：2026-09-02

---

## 0. 执行前提

| 项 | 状态 |
|---|---|
| 11 项架构决策 | ✅ 已全部锁定（ADR-0001 ~ 0011） |
| Python 3.12.3 | ✅ 本机可用 |
| PyPI 可达 | ✅ 已实测（`mcp 2.1.1` 可下载安装） |
| SDK API 形态 | ✅ 已实测确认（`MCPServer`，非 `FastMCP`） |
| 性能基线 | ✅ 已实测（冷启动 2.2 s / 查询 2 ms） |
| 目标语料 | ✅ 已确定（`raw/` 下 265 个 md/txt） |

## 0.1 执行结果摘要

| 阶段 | 状态 | 产出 |
|---|---|---|
| P1 骨架与配置 | ✅ | `requirements.txt`、`.env.example`、`src/my_memory/config.py`（fail-fast 校验，5 类非法配置均启动即报错） |
| P2 语料与索引 | ✅ | `corpus.py`、`index.py`；实测 265 文档 / 3,106 chunk / 冷启动 2.4–3.1 s |
| P3 服务与工具 | ✅ | `server.py`、`__main__.py`；2 个 MCP 工具 + `/health` + `/search`，MCP 握手验证通过 |
| P4 验证与调优 | ✅ | 抽样机械判定 5/6、实质判定 6/6；三轮定点调优；并发 58,614 请求 0 失败 |
| P5 文档与交付 | ✅ | `README.md`、`run.sh`、`run.ps1`；ADR 补充 0012 / 0013 |
| 单元测试 | ✅ | 56 项全部通过 |

实施过程中发现并记录了两项新决策：
[ADR-0012 检索词构造](adr/0012-path-and-term-tokenization.md)、
[ADR-0013 命中判定](adr/0013-term-presence-over-positive-score.md)。
| 部署环境 | ⛔ 不在本次范围（[ADR-0008](adr/0008-machine-agnostic-delivery.md)） |

**验证环境说明**：当前 Codespace 无法接受局域网入站连接（拓扑限制，见 ADR-0008），
因此全部验证在 `127.0.0.1` 上完成。局域网可达性由使用方在自己的运行环境验证。

---

## 1. 阶段划分

总计 5 个阶段，每阶段有独立的可验证产出。**前一阶段验证不通过不进入下一阶段。**

```
P1 骨架与配置  ──►  P2 语料与索引  ──►  P3 服务与工具
                                            │
                        P5 文档与交付  ◄──  P4 验证与调优
```

---

## 2. P1 · 骨架与配置

**产出**

```
mcp/
├── requirements.txt        锁死版本的 4 个依赖
├── .env.example            全部环境变量及默认值
├── .gitignore              .venv/ __pycache__/
└── src/my_memory/
    ├── __init__.py
    └── config.py        # 即 src/my_memory/config.py，配置校验模块
```

**任务**

1. `requirements.txt` 锁死：`mcp==2.1.1`、`uvicorn==<实测版本>`、`jieba==0.42.1`、`rank_bm25==<实测版本>`
2. `my_memory/config.py`：定义 `Config` 冻结数据类，字段与 [ARCHITECTURE §7](ARCHITECTURE.md#7-配置项全部通过环境变量) 的 12 个环境变量一一对应
   - `MEMORY_ROOT` 默认值 = `Path(__file__).resolve().parents[3]`（即 `mcp/` 的父目录）
   - `MEMORY_SCAN_DIRS` 默认 `raw`，解析为列表
   - 数值型配置做范围校验，越界时启动即失败并给出明确错误信息（fail fast，不静默取默认值）
3. 仓库根 `.gitignore` 追加 `mcp/.venv/`

**验证**

- [ ] `python -m venv .venv && .venv/bin/pip install -r requirements.txt` 成功
- [ ] `python -c "from my_memory.config import Config; print(Config.from_env())"` 打印完整配置
- [ ] 故意设置 `MEMORY_PORT=abc` 与 `MEMORY_CHUNK_OVERLAP=900`（超过 chunk_size），确认启动即报错且信息可读

---

## 3. P2 · 语料与索引

**产出**：`corpus.py`、`index.py`、`tests/test_corpus.py`、`tests/test_index.py`

**任务**

`corpus.py`
1. `scan(config) -> list[DocMeta]`：遍历 `scan_dirs`，按 `extensions` 过滤，
   跳过隐藏目录（`.git/`、`.claude/` 等），记录 `(relpath, mtime, size)`
2. `read_and_chunk(docs, config) -> list[Chunk]`：UTF-8 读取（`errors="ignore"`），
   固定窗口切块 `chunk_size=800` / `overlap=120`，空白块丢弃
   - `Chunk` 字段：`path`、`chunk_index`、`char_start`、`char_end`、`text`
3. `fingerprint(docs) -> tuple[int, float]`：返回 `(文件数, max(mtime))` 用于变更检测

`index.py`
4. `IndexSnapshot` 冻结数据类：`bm25`、`chunks`、`documents`、`indexed_paths`（`frozenset`）、
   `built_at`、`doc_count`、`chunk_count`、`fingerprint`
5. `build(config) -> IndexSnapshot`：scan → chunk → `jieba.cut_for_search` → `BM25Okapi`
6. `search(snapshot, query, limit) -> list[Hit]`：分词 → `get_scores` → 取 top-N（`score > 0`）
   → 片段按 `snippet_chars` 截断
7. `IndexHolder`：持有当前快照的全局引用 + 后台轮询线程
   - 线程循环：sleep(interval) → `fingerprint()` 比对 → 变化则 `build()` → 单条赋值替换
   - `try/except` 包裹重建，异常时保留旧快照 + 记 ERROR 日志
   - `poll_interval=0` 时不启动线程

**验证**

- [ ] 单测：切块边界（空文件、短于窗口的文件、恰好整除窗口的文件）
- [ ] 单测：`indexed_paths` 与 `documents` 的键集合完全一致
- [ ] 单测：`fingerprint` 能检测到「新增文件」「修改文件」「删除文件」三种变更
- [ ] 集成：对真实 `raw/` 构建，断言 `doc_count == 265`、`chunk_count == 3106`
- [ ] 集成：冷启动耗时 < 4 s（基线 2.2 s，留 1.7 s 余量）
- [ ] 集成：检索 `固件指令集`，top-1 命中 `raw/Company_Facts_And_Status/固件指令集.md`
- [ ] 集成：临时改动一个 raw 文件的 mtime，确认轮询线程在一个周期内重建且 `built_at` 更新
- [ ] 集成：让 `build()` 抛异常，确认旧快照仍在服务、日志有 ERROR

---

## 4. P3 · 服务与工具

**产出**：`server.py`、`__main__.py`、`tests/test_server.py`

**任务**

1. `MCPServer(name="myMemory", version=...)`，`instructions` 中写明知识库范围
   （"dx-ble-toy-security 项目的 raw/ 事实源层：BLE 玩具安全方案的会议纪要、
   方案设计稿、公司现状、技术资料与交付规格"）
2. `@server.tool(name="myMemory-search")`
   - description 必须包含：语料范围、返回是证据片段而非答案、
     **以及"先读 `raw/文档索引.md` 获取全量导航"的用法引导**
   - 入参校验：`query` 1–500 字符、`limit` 1–20（越界钳制而非报错）
   - 返回结构见 [ARCHITECTURE §6.1](ARCHITECTURE.md#61-mcp-工具myMemory-search)，`index` 元信息每次附带
3. `@server.tool(name="myMemory-get-document")`
   - **安全核心**：`if path not in snapshot.indexed_paths: return error`
   - 路径非法时，用 `difflib.get_close_matches` 返回最相近的 5 条已索引路径
   - 分页：`offset` / `limit`（上限 `max_doc_chars`），返回 `has_more`
4. `@server.custom_route("/health", ["GET"])` 与 `@server.custom_route("/search", ["GET"])`
5. `__main__.py`：读配置 → **阻塞式首次构建** → 启动轮询线程 →
   `server.streamable_http_app(streamable_http_path="/mcp", transport_security=...)` → `uvicorn.run`
   - `transport_security` 仅在 `MEMORY_ALLOWED_HOSTS` 非空时构造，否则传 `None`
     （实测：传空 `allowed_hosts` 的配置对象会拒绝所有请求）
   - 启动日志打印：绑定地址、端口、root、scan_dirs、doc_count、chunk_count、构建耗时

**验证**

- [ ] `GET /health` 返回 200 且字段完整
- [ ] `GET /search?q=固件指令集&limit=3` 返回 3 条结果，路径与分数合理
- [ ] `myMemory-get-document` 传 `../../etc/passwd`、`/etc/passwd`、`wiki/index.md`、
      `raw/../raw/文档索引.md` 四种路径，**全部被拒绝**（前三种不在集合内，第四种字符串不匹配）
- [ ] `myMemory-get-document` 传 `raw/文档索引.md` 成功，`total_chars` 与实际一致
- [ ] 传 `limit=999999` 被钳制到 40000，`has_more=true`
- [ ] 传 `raw/文档索引.MD`（大小写错误）被拒绝且返回相近路径提示

---

## 5. P4 · 端到端验证与检索质量调优

**任务**

1. 在 `127.0.0.1:7082` 启动服务
2. 用 Claude Code 挂载：`{"myMemory": {"type": "http", "url": "http://127.0.0.1:7082/mcp"}}`
3. **MCP 握手验证**：确认 `tools/list` 返回 2 个工具且 schema 正确
4. **检索质量抽样**：用下列真实问题各跑一遍，人工判断 top-5 是否含有效证据

   | # | 测试问题 | 期望命中方向 |
   |---|---|---|
   | 1 | EFR32BG21 固件指令集有哪些震动控制指令 | `Company_Facts_And_Status/固件指令集.md` |
   | 2 | 推荐方案里 R 是怎么生成的，无按键玩具怎么处理 | `Solution_Design_Docs/推荐方案*` |
   | 3 | 8 月 4 日跨部门同步会议的结论是什么 | `Solution_Meeting_Docs/26-08-04-*` |
   | 4 | LESC 配对的中间人防护机制 | `Technological_Knowledge/LESC-*` |
   | 5 | BtleJack 能做什么攻击 | `Technological_Knowledge/*` |
   | 6 | MSD 广播里的 Company ID 怎么申请 | `Technological_Knowledge/*MSD*` |

   实际执行结果与三轮调优过程见 [RETRIEVAL-NOTES.md](RETRIEVAL-NOTES.md)。

5. **失败样本记录**：任何 top-5 无有效证据的问题，记录到 `docs/RETRIEVAL-NOTES.md`，
   分析是分词问题、切块问题还是 BM25 参数问题
6. **可调旋钮**（按此顺序尝试，每次只动一个）：
   - jieba 自定义词典：把 `EFR32BG21A010F768`、`LESC`、`BtleJack`、`Tophy` 等
     专有名词加入词典，防止被错误切分（**这是最可能有效的一步**）
   - `chunk_size` / `overlap`
   - BM25 的 `k1` / `b` 参数
7. **并发验证**：在持续请求的同时触碰一个 raw 文件触发重建，确认无请求失败、无脏读

**验收标准**

- [ ] 6 个抽样问题中 **≥ 5 个** 的 top-5 含有效证据
- [ ] 重建期间并发请求零失败
- [ ] 服务连续运行 30 分钟无内存增长异常（快照替换后旧对象被回收）

---

## 6. P5 · 文档与交付

**产出**：`README.md`、`run.sh`、`run.ps1`，并提交 git

**任务**

1. `run.sh` / `run.ps1`
   - 检测/创建 `.venv` → 安装依赖 → 用 `python -m my_memory` 启动
   - **所有路径必须引号包裹**（`raw/` 下文件名普遍含空格与中文）
   - 支持从 `.env` 读取配置（若存在）
2. `README.md` 必须包含：
   - 一句话说明服务是什么、索引什么（`raw/` 事实源层）、不索引什么（`wiki/`）
   - 快速启动（两个平台各一段）
   - 客户端 `.mcp.json` 配置示例
   - 环境变量完整表格
   - **`MEMORY_ALLOWED_HOSTS` 的显著警告框**：默认关闭 DNS rebinding 保护；
     传入空 `allowed_hosts` 会拒绝所有请求
   - 故障排查：端口占用、首次启动慢（2.2 s 属正常）、索引未更新（检查轮询间隔与 `built_at`）
   - 明确声明：服务免鉴权，仅应部署在受信任的局域网内
3. `git add mcp/ && git commit`（当前分支 `dev`）

**验收标准**

- [ ] 在干净目录按 README 步骤操作，能成功启动服务
- [ ] `mcp/` 下无 `.venv`、`__pycache__` 被提交
- [ ] `docs/` 下 ADR 链接全部可达（无断链）

---

## 7. 风险与应对

| 风险 | 概率 | 影响 | 应对 |
|---|---|---|---|
| jieba 把专有名词切碎导致检索不准 | **高** | 检索质量不达标 | P4 已列为首选调优手段：自定义词典。词典文件纳入 git |
| P4 抽样验收不通过（< 5/6） | 中 | 需要返工调优 | 先穷尽 P4 第 6 步的三个旋钮；仍不达标则重新评估 [ADR-0002](adr/0002-bm25-over-vectors.md)（引入向量混合），这是**唯一会推翻既有决策**的路径 |
| 固定窗口切块导致会议转录检索体验差 | 中 | 部分问题需多次 get_document | 已在 [ADR-0005](adr/0005-fixed-window-chunking.md) 中接受；若实测严重，改动面仅限 `corpus.py` 的切块函数 |
| `uvicorn` / `rank_bm25` 版本与 `mcp 2.1.1` 冲突 | 低 | 装不上 | P1 立即暴露；锁版本前先跑一次完整 install |
| 使用方运行环境无法访问 PyPI | 低 | 装不上依赖 | README 中给出离线安装说明（`pip download` + `pip install --no-index`） |

---

## 8. 明确不做的事

- 不部署、不申请端口、不配置服务器（[ADR-0008](adr/0008-machine-agnostic-delivery.md)）
- 不实现鉴权、限流、访问审计（[ADR-0010](adr/0010-indexed-set-membership.md)）
- 不索引 `wiki/`、不索引 csv/xlsx（[ADR-0001](adr/0001-corpus-scope.md)、[ADR-0004](adr/0004-md-txt-only.md)）
- 不引入向量检索（[ADR-0002](adr/0002-bm25-over-vectors.md)）——除非 P4 验收失败且旋钮穷尽
- 不做 Web UI（`/search` 端点仅返回 JSON，供调试与未来调用方使用）
- 不修改仓库中 `mcp/` 以外的任何文件（唯一例外：根 `.gitignore` 追加一行）
