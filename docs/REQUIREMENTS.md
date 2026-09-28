# myMemory 0.1.0 需求

> 状态：**已实施** · 2026-09-24（验收记录见 [TASKS.md](TASKS.md)）
> 0.1.0 = 第一轮改造（多 source、只读、config.json + config.py、最近编辑列表，已实施）
> \+ 第二轮改造（工具短名、单平台运行、索引缓存与增量更新、挂载盘掉线、`/recent`）。
> 这是第一个正式版本号，此前的 3.x / 4.0.0 版本号作废。
> 服务当前版本 **0.3.0**（2026-09-28，掉盘 source 数据保全，见
> [ADR-0026](adr/0026-offline-content-natural-lru-and-cold-start-rescue.md)）。
> 第二轮追加：只读改为配置字段 `writable` 标记，不再用名称前缀（§3.2）。
> 来源：两轮痛点评审（grilling 会话），逐条决策见文末 §14。
> 第三轮改造（写入面扩展与删除断路器，2026-09-27 已实施）见文末 §15。
> 术语以 [GLOSSARY.md](GLOSSARY.md) 为准；实施后同步修订 [ARCHITECTURE.md](ARCHITECTURE.md)。

---

## 1. 背景与痛点

| # | 痛点 | 根因 | 轮次 |
|---|---|---|---|
| P1 | 不能多目录，不能按 source 划分 | 只有一个 `MEMORY_ROOT` | 一 ✅ |
| P2 | 没有只读目录 | 读写边界是同一条线 | 一 ✅ |
| P3 | 无法配置刷新周期、管理 source | 配置全部来自环境变量 | 一 ✅ |
| P4 | 带英文括号的文件名保存失败 | 白名单只放行全角（） | 一 ✅ |
| P5 | 看不到最近改了什么 | 无查询能力 | 一 ✅ |
| P6 | 工具全名太长：`mcp__myMemory__myMemory-save` | 工具名自带 `myMemory-` 前缀，与客户端加的服务名重复 | 二 |
| P7 | 按平台区分虚拟环境等机制已无必要，且造成过 PID 文件互相覆盖 | 为"两个系统共用一个目录"而设计 | 二 |
| P8 | 版本号混乱（3.x → 4.0.0） | 历史沿革 | 二 |
| P9 | 没有 HTTP 方式查最近编辑 | 只有 MCP 工具 | 二 |
| P10 | 挂载盘掉线后，该 source 的内容从检索里全部消失；启动时盘不在直接报错退出 | 掉盘被当成"文件全删了"；目录不存在即 fail-fast | 二 |
| P11 | 挂载部门知识库后启动要 3 分半 | 每次启动全量扫描 + 读取 + 分词，无持久化 | 二 |
| P12 | 只读靠名称前缀 `readonly/` 标记，改只读要改名、文档身份随之改变；检索结果看不出能否写 | 只读编码在名称里 | 二 |

P11 实测（Windows，挂载盘上的部门知识库）：

| 规模 | 扫描网络盘 | 读取正文 | jieba 分词 | 合计 |
|---|---|---|---|---|
| 约 1200 文件 / 1.8 万块 | 62 s | 120 s | 68 s | 210 s（服务日志） |
| 约 1460 文件 / 2.1 万块 / 1400 余万字符 | — | — | 121 s | — |

## 2. 目标与非目标

### 目标

1. 多 source、只读 source、config.json + config.py、文件名白名单修复、最近编辑列表（第一轮，已实施）。
2. 工具名去掉 `myMemory-` 前缀。
3. 只在当前系统（Windows）运行，去掉按平台区分的机制。
4. 版本号统一为 0.1.0。
5. 新增 `GET /recent`（JSON）。
6. 挂载盘掉线时不更新、不删除该 source 的索引。
7. 索引缓存：启动先用缓存立即服务，后台增量校验更新。
8. 只读改为 `writable` 字段标记；所有返回文档的输出都带 `writable`。

### 非目标

| 非目标 | 理由 |
|---|---|
| 向量检索 / Embedding | 仍用 BM25 + jieba（ADR-0002）。**本系统没有向量**，缓存存的是分词结果与 BM25 统计 |
| 通过 MCP 增删改 source 或修改配置 | 免鉴权下等于只读形同虚设（ADR-0017） |
| 配置热加载 | 配置修改一律重启生效；热操作只有 reindex |
| 跨平台运行 / 路径互转 | 只在当前系统运行；config.json 写什么用什么 |
| 编辑日志 / 版本历史 / 内容描述 | 只要编辑列表 |
| 每 source 独立索引 | 统一索引，接受 IDF 相互影响 |
| `/recent` 的 HTML 页面、点击看全文 | 只要 JSON |
| git / http / oss 存储 | 仅预留接口 |

---

## 3. 核心概念

### 3.1 Source

| 属性 | 说明 |
|---|---|
| `name` | 中英文、数字、下划线、连字符，1–64 字符，**不含 `/`**，全局唯一。不再有 `readonly/` 前缀特例 |
| `dir` | 目录路径，原样使用，可在任意位置，含 `Z:\` 与 UNC 挂载盘 |
| `type` | 存储类型，目前只有 `local`；非 `local` 接入后一律先按只读 |
| `writable` | 布尔，是否允许 MCP 写入；**省略即 `true`**。CLI 写配置时总是显式写出 |
| `description` | 选填，一句话描述 |

- **不重叠、不嵌套**：按路径比较（能解析真实路径时解析，含符号链接；Windows 上不区分大小写；掉盘时按配置原样路径比较）。
- **名称非法、重名、重叠**：启动失败（fail fast）。
- **目录不可用**：启动**不失败**，只给警告，见 §8。

### 3.2 只读 source

配置 `"writable": false` 即只读；照常索引、检索、读取；服务永不写入；不改磁盘权限。
名称与只读无关，改只读不改名、不改变文档身份。

**对外的 `writable`（有效可写）** = 配置 `writable` 为 true **且** source 当前可用（§3.4）**且** 存储类型为 `local`。
掉盘或目录不存在时，即使配置可写，对外也是 `false`（原因见 `unavailable_reason`）。

- 不做迁移：旧的 `readonly/xxx` 名称含 `/`，按普通名称校验即被拒绝。
  实施时删除现有 `config.json` 与 `status.json`，由人重建。

### 3.3 文档身份

`(source, path)` 二元组，对外是两个独立字段；path 相对 source 目录，不含 source 名。
MCP 写入形态 `<分类>/<文件名>.md` 或 `<文件名>.md`（分类为空）。

### 3.4 source 可用性

| 状态 | 判定 | `available` | `unavailable_reason` |
|---|---|---|---|
| 正常 | 目录可访问 | `true` | — |
| **掉盘** | source 所在**盘根**不可访问（`Z:\`、`\\server\share\`）；或扫描途中出错且此时盘根不可访问 | `false` | `disk_offline` |
| **目录不存在** | 盘根可访问，但 source 目录不存在（被删除或改名） | `false` | `dir_missing` |

"掉盘"以**磁盘**访问不到为准，不以目录访问不到为准——目录可能是被删除了。

---

## 4. 配置

### 4.1 config.json

- 默认位置：**`~/.myMemory/config.json`**；唯一环境变量 `MEMORY_CONFIG` 覆盖路径。
  `index.cache` 与 `logs/`（日志、PID）放在配置文件所在目录——**代码目录不落任何运行数据**（第二轮追加）。
- 字段与默认值同第一轮：`host`、`port`（7083）、`poll_interval`（600）、`sources`、`extensions`、`chunk_size`、`chunk_overlap`、`max_results`、`snippet_chars`、`max_doc_chars`、`max_create_chars`。
- 修改一律**重启生效**；未知字段、越界、类型错误启动失败。
- 首次启动不存在时**交互式建档**（ADR-0025）：终端逐字询问记忆目录（回车默认
  `~/.myMemory/memory`，自动创建），生成默认值 + 一个可写 source `memory`（描述"默认记忆源"）；
  无法交互（stdio 传输、后台子进程）时报错"未指定记忆存储"，不猜测、不静默生成。

### 4.2 运行时文件（与 config.json 同目录）

| 文件 | 用途 |
|---|---|
| `index.cache` | 索引缓存，见 §7 |
| ~~`status.json`~~ | **删除**。agent 标记并入 `index.cache`（§7.2） |

---

## 5. 运行环境（单平台）

- 只在当前系统（Windows）运行，不再考虑同一目录被另一系统同时使用。
- 虚拟环境统一为 **`.venv`**，保留虚拟环境（依赖锁定精确版本）。
- 删除：按平台分目录（`.venv-windows` / `.venv-linux` / `.venv-darwin`）、拒绝清空另一平台环境、旧 `.venv` 迁移提示，及对应测试。
- 删除磁盘上的 `.venv-windows`、`.venv-linux`，由 `run.py` 重建 `.venv`。
- PID 文件、日志保持单份。
- 开发与测试在 Windows 的 `.venv` 中执行（需安装 `requirements-dev.txt`）。

---

## 6. MCP 工具（0.1.0，共 5 个；第三轮改造后为常驻 7 个 + 断路器控制 2 个，见 §15）

服务名仍为 `myMemory`。**工具名去掉 `myMemory-` 前缀**，客户端全名形如 `mcp__myMemory__save`
（`mcp__<服务名>__<工具名>` 由客户端拼接，服务端无法改成 `mcp_` 形式）。

| 工具 | 旧名 | 说明 |
|---|---|---|
| `search` | `myMemory-search` | 检索，默认跨全部 source，可选 `source` |
| `get-document` | `myMemory-get-document` | `source` + `path` 必填 |
| `save` | `myMemory-save` | `source` 必填，`category` 可空 |
| `list-sources` | `myMemory-list-sources` | 新增字段 `available`、`unavailable_reason` |
| `recent` | `myMemory-recent` | 默认 10 最多 20，每文件一条 |

参数、返回结构同第一轮，以下为变化：

- `get-document`：source 掉盘时从缓存返回全文，响应带 `"stale": true`；正常时 `"stale": false`。
- `save`：source 不可用（掉盘或目录不存在）时拒绝，**不自动创建目录**。
- `list-sources`：每项新增 `available`（bool）与 `unavailable_reason`（`disk_offline` / `dir_missing` / null）；`writable` 改为有效可写。
- **`writable` 字段**：`search` 每条结果、`recent` 每条结果、`get-document` 响应、`list-sources`、`/health` 全部带上，取值为有效可写（§3.2）。REST `/search`、`/recent` 同步。
- instructions 与工具描述："`readonly/` 开头不可写"改为"看 `writable` 字段"。
- `recent`：`edited_by` 的判定数据来自 `index.cache` 中的 `agent_mtime`（§7.2）。
- server instructions 与工具描述中的工具名同步改为短名。

---

## 7. 索引缓存与增量更新

### 7.1 启动流程

1. 读 `config.json`，校验。
2. 加载 `index.cache`：校验格式版本与**配置指纹**，通过则直接构成快照、**立即开始监听**。
3. 后台增量校验：扫描全部可用 source，按 `(mtime, size)` 找出新增、变化、删除的文件，
   只重读、重分词变化的文件，重建 BM25，原子替换快照，写回缓存。
4. 校验完成前 `/health` 的 `verifying` 为 `true`。
5. 缓存不存在、损坏、格式版本或配置指纹不符：告警，走一次全量构建（构建完成后才监听，同现状）。

### 7.2 缓存内容

一个文件 `index.cache`（pickle，带格式版本头），原子写入（临时文件 + 替换），每次重建成功后写回。

| 部分 | 内容 | 实测体积（约 1460 文件） | 加载耗时 |
|---|---|---|---|
| 头 | 格式版本、配置指纹、构建时间 | — | — |
| 文件条目 | 每文件：source、path、mtime、size、正文、块偏移、分词结果、`agent_mtime` | 正文 19 MB + 块 0.6 MB + 分词 36 MB | 约 2.8 s |
| BM25 模型 | 序列化的 BM25 对象，避免启动时重建（重建需 4 s） | 23 MB | 0.7 s |

- **配置指纹** = 切块参数、扩展名、分词词典（`domain_terms` 配置项）、缓存格式版本、代码版本。任一变化则缓存作废。
- **`agent_mtime`**：经 `save` 写入后记录落盘 mtime；当前 mtime 相等为 `agent`，否则 `scan`。
  save 后到下一次缓存写回之间若服务崩溃，该条标记丢失（显示 `scan`），可接受。
- **本系统没有向量**：检索是 BM25 关键词匹配，缓存里是分词结果与 BM25 统计。

### 7.3 增量更新

| 触发 | 方式 |
|---|---|
| 启动后台校验 | 增量 |
| 轮询（`poll_interval`） | 增量 |
| `save` 后刷新 | 增量 |
| `config.py reindex` / `POST /reindex` | **默认增量**；`--full` / `?full=1` 强制全量（忽略缓存） |

- 未变化的文件复用缓存中的分词结果；BM25 每次整体重建（统计量是全局的）。
- 并发合并与失败保留旧快照的机制不变。

### 7.4 缓存清理

| 情况 | 索引 | 缓存 |
|---|---|---|
| 单个文件被删除 | 移除 | 移除 |
| source 目录不存在（`dir_missing`） | 该 source 全部移除 | 全部移除 |
| source 掉盘（`disk_offline`） | **保留，不更新** | **保留，不更新** |
| source 从 config.json 移除 | 移除 | 重启后移除 |

---

## 8. 挂载盘掉线

- **掉盘期间**：该 source 的索引与缓存不更新、不删除；其他 source 照常更新；
  search 照常可搜；get-document 从缓存返回全文并标 `stale: true`；save 拒绝；
  `list-sources` / `/health` 显示 `available: false, unavailable_reason: "disk_offline"`；记警告日志。
- **盘恢复**：下一次轮询自动恢复正常增量更新，无需人工操作。
- **启动时盘不在**：警告，不失败；缓存中有该 source 的条目就照常服务。
- **启动时盘在但目录不存在**：警告，不失败；按目录删除处理（§7.4）。
  config.json 路径拼错的问题由 `config.py source add/edit`（校验目录存在）与 `/health` 的 `available` 发现。
- **目录重叠判定**：仍按路径比较。

---

## 9. CLI：config.py

同第一轮，变化：

- `reindex` 默认增量，新增 `--full` 全量。仍通过 `POST /reindex` 通知运行中的服务。
- `source add/edit` 仍校验目录存在。
- **只读标记**：`add` **必须**指定 `--readonly` 或 `--writable` 之一；`edit` 用 `--readonly` / `--writable` 切换，二者互斥；`--name` 改名不影响只读。
- `source list` 显示读写 / 只读。

```
python config.py source list
python config.py source add <name> --dir <dir> (--readonly | --writable) [--desc "..."] [--restart]
python config.py source edit <name> [--name <新名>] [--dir <新目录>] [--readonly | --writable] [--desc "..."] [--restart]
python config.py source remove <name> [--yes] [--restart]
python config.py config set poll_interval <秒> [--restart]
python config.py reindex [--full]
python config.py restart
```

## 10. REST 端点

| 端点 | 说明 |
|---|---|
| `GET /health` | 同第一轮；`sources` 每项含 `available`、`unavailable_reason`；新增 `verifying`；`version` 为 `0.1.0` |
| `GET /search?q=&limit=&source=` | 不变 |
| `GET /recent?limit=&source=` | **新增**，返回 JSON，与 MCP `recent` 结构一致；未知 source 返回 400 |
| `POST /reindex[?full=1]` | 保留；默认增量，`full=1` 全量 |

## 11. 版本号

- 代码、MCP 握手、`/health` 的版本统一为 **`0.1.0`**。
- 删除 `server.py` 注释中 3.0.0 → 3.3.0 的版本演变史（演变过程由 ADR 记录）。
- 文档去掉"v4"字样；需求与任务合并为不带版本号的单份文档 `docs/REQUIREMENTS.md`、`docs/TASKS.md`。

## 12. 删除项

- `status.py`、`status.json` 及其测试（并入缓存）。
- 名称前缀 `readonly/` 的全部逻辑（`READONLY_PREFIX`、`is_readonly_name`、CLI 的自动加前缀、改名切换只读）。
- 现有 `config.json`（由人按新格式重建）。
- `run.py` 的按平台 venv、跨平台检测、旧 `.venv` 提示及对应测试；磁盘上的 `.venv-windows`、`.venv-linux`。
- README"虚拟环境按平台分开"一节；ADR-0011 中跨平台部分标注已取代。

---

## 13. 验收标准

第一轮验收标准（多 source、只读、配置校验、文件名规则、recent、CLI、首次启动）继续有效，另加：

1. 客户端看到的工具全名为 `mcp__myMemory__search` 等 5 个短名，无 `myMemory-` 前缀。
2. `run.py` 只使用 `.venv`；仓库中无 `.venv-windows` / `.venv-linux` 相关代码。
3. `/health` 与 MCP 握手版本为 `0.1.0`；代码与文档中无"4.0.0 / v4"。
4. `GET /recent` 返回 JSON，默认 10 条、最多 20 条，支持 `source` 过滤。
5. 有缓存时启动：索引从缓存加载后数秒内开始监听；`/health` 先显示 `verifying: true`，后台校验完成后为 `false`。
6. 部门知识库场景（~1400 文件，挂载盘）：有缓存的重启到可服务 ≤ 15 s；无变化时后台校验不重读、不重分词。
7. 缓存损坏 / 配置指纹变化：告警并全量构建，服务可用。
8. 模拟掉盘（盘根不可访问）：该 source 的检索结果与全文仍可用，`stale: true`，`available: false / disk_offline`；轮询不删除其条目；恢复后自动增量更新。
9. 启动时盘不在：警告，服务正常启动，缓存中的内容可检索。
10. 盘在但 source 目录被删：该 source 的索引与缓存清空，`dir_missing`，启动只警告。
11. `save` 到不可用 source 被拒绝，不创建目录。
12. `config.py reindex` 默认增量、`--full` 全量；`POST /reindex?full=1` 全量。
13. agent 标记跨重启保留（来自缓存）；不再生成 `status.json`。
14. `config.json` 中 `"writable": false` 的 source：save 被拒绝；search / recent / get-document / list-sources / `/health` 均显示 `writable: false`。
15. 省略 `writable` 的 source 视为可写；名称含 `/`（含旧 `readonly/xxx`）启动失败。
16. 可写 source 掉盘或目录不存在时，所有输出的 `writable` 为 `false`。
17. `config.py source add` 不带 `--readonly` / `--writable` 报错；`edit --readonly` / `--writable` 切换且不改名；CLI 写出的配置总有 `writable` 字段。
18. 全部测试在 Windows `.venv` 中通过。

---

## 14. 决策记录（grilling 会话，2026-09-24）

### 第一轮

| # | 问题 | 决策 |
|---|---|---|
| 1 | source 是什么 | 用户命名 + 任意位置的真实目录（含挂载盘）；不重叠不嵌套 |
| 2 | 路径表示 | source 独立字段，path 不拼接；分类可为空 |
| 3 | save 的 source | 必填，无默认值 |
| 4 | 检索范围与索引 | 默认跨全部 source；统一全量索引；预留 git/http/oss |
| 5–7 | 配置管理入口 | 不做 MCP 配置工具，改为独立 CLI，人工操作，重启生效 |
| 8 | 刷新周期 | CLI 设置，重启生效；另有热生效的 reindex |
| 9 | 只读表示 | 名称前缀 `readonly/<名称>`；MCP 描述保持静态（**第二轮 Q13 改为 `writable` 字段**） |
| 10 | AI 如何发现 source | `list-sources` 工具 |
| 11 | 文件名规则 | 保留白名单，补常用半角符号，禁止跨目录与系统保留名 |
| 12–15 | 最近编辑 | 编辑列表，默认 10 最大 20，每文件一条；更改类型 scan / agent |
| 16–17 | CLI 形态 | 独立 `config.py`，子命令 `source` 不简写 |
| 18 | 跨平台路径 | 不转换，json 写什么用什么 |

### 第二轮

| # | 问题 | 决策 |
|---|---|---|
| 1 | 工具全名 | 客户端格式 `mcp__<服务>__<工具>` 不可改；去掉工具名 `myMemory-` 前缀 → `mcp__myMemory__save` |
| 2 | "不区分 env" | C：只在当前系统（Windows）运行 |
| 3 | venv | 保留虚拟环境，统一 `.venv`；删除 `.venv-windows` 重建 |
| 4 | 版本号 | 两轮合并为 0.1.0，首个正式版本；文档去版本号、合并为单份 |
| 5 | `/recent` | 只返回 JSON，不做 HTML，不做点击看全文 |
| 6–7 | 去掉 `POST /reindex` | 评估后撤销：端点与 `config.py reindex` 都保留 |
| 8 | 启动策略 | 先用缓存立即服务，后台增量校验更新 |
| 9 | edited_by 存储 | 去掉 status.json，`agent_mtime` 并入索引缓存 |
| 10 | 掉盘判定与行为 | 盘根访问不到才算掉盘；掉盘不更新不删除，其他照常；恢复后轮询自动恢复；启动时盘不在只警告；重叠按路径比较 |
| 11 | 目录被删除 | 按删除处理，启动只警告 |
| 11b | 缓存清理 | 目录删除 → 索引与缓存都清除；掉盘 → 都不清除 |
| 12 | reindex 粒度 | 默认增量，`--full` 全量 |
| 13 | 只读标记 | 不再用名称前缀，改为配置字段 `writable`（省略即 true）；不做迁移，删除旧配置由人重建 |
| 14 | writable 出现在哪 | search / recent / get-document / list-sources / `/health` 全部带；取值计入当前可用性 |
| 15 | CLI 只读 | `--readonly` / `--writable`，add 时必须指定其一，edit 互斥切换；CLI 总是显式写出 `writable` |
| — | 存储核对 | 无向量；缓存存正文、块偏移、分词结果与 BM25 模型（实测 ≈ 79 MB，加载 ≈ 3.5 s） |
| — | 默认项（已确认） | 缓存 `index.cache` 放 config.json 旁；`available` / `unavailable_reason` / `verifying` 字段；掉盘 get-document 返回缓存全文 + `stale`；save 不可用 source 报错不建目录；CLI add/edit 校验目录存在；测试改在 Windows 跑 |

---

## 15. 第三轮改造：写入面扩展与删除断路器（2026-09-27，已实施）

前两轮的写入面只有 `save`（整篇覆盖）。整理已有记忆在实践中频繁需要三类操作——
改名/归类、局部修改、把碎片并成一篇——每次都靠"读回原文 + save 整篇重写"，
既繁琐又放大覆盖风险；同时"删除"从未存在过， pruning 只能靠人手。本轮补齐
写入面，并把删除放进一个默认关闭的**断路器**里：

| # | 需求/决策 | 说明 |
|---|---|---|
| 1 | `rename(source, old_path, new_path)` | 同 source 内改名/移动一级分类。旧文件必须真实存在（文件系统判断），目标存在即拒绝——rename 的覆盖等于直接删文件，必须显式防住；不支持跨 source |
| 2 | `replace(source, path, old_string, new_string)` | 全文**完全字面**替换（无正则、无大小写折叠、不归一化换行），全部命中处替换并返回 `replaced_count`。0 命中、`new_string` 为空、old==new 一律拒绝且不动文件——删段落等改写余文的活不归它 |
| 3 | `merge(source, from_path, to_path)` | 把**已存在**的源并入**已存在**的目标（目标必须已存在，新建走 save），然后删除源文件。并入段以 `## 源文件相对路径` 起头、前有 `---` 分隔线，来源可追溯。先写目标后删源，删除失败不回滚、响应 `source_removed: false` 如实标出 |
| 4 | `delete(source, path)` | 真删（unlink），无备份不可恢复；不做回收站/软删除 |
| 5 | 断路器 `allow_mcp_delete` | config.json 全局布尔，默认 **false**。关闭时 `delete` 与 `merge` 同受控（因为 merge 会删源）：连 MCP 工具都不注册，LLM 在 tools/list 里看不到就不会调用；writer 层 `_require_delete_enabled` 用同一开关再拦一道（兜绕过工具层的调用与配置窗口期）。开启 = 人工改配置 + **重启生效**，无热切换 |
| 6 | 路径入参口径 | 与 search/recent 返回的 `path` 同形（不含 source 名，.md 可带可不带）；路径白名单与 save 同一套（一级分类、无 `..`、无隐藏层、无系统保留名） |
| 7 | 暴露面 | 全部只做 MCP 工具，不开 REST 写端点（与既有写入面一致）；每项能力在 tools/list 的描述里写明关键约束（不覆盖/字面匹配/真删等） |

验收：全量测试 402 passed, 1 skipped；契约测试锁定"开关关闭时工具面收敛为
BASE_TOOLS（7 个）"；真实服务经 MCP 客户端实测 9 工具在线。生产部署时用户显式
选择开启（`allow_mcp_delete: true`）。
