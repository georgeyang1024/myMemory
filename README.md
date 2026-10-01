# myMemory

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**中文** | [English](README_EN.md)

**A local-first memory service for humans and AI agents.**
人与 AI 共同使用的本机记忆库：双方都往里写、都从里读，跨会话长期保存。

记忆就是普通的 Markdown / 文本文件，放在你自己指定的目录里——没有数据库、
没有云端、没有锁定。AI 通过 MCP 工具检索与写入；你随时用编辑器直接增删改，
改动照常进索引。

---

## English overview

**myMemory** is a local-first, self-hosted **long-term memory server for AI agents and humans**. Plain Markdown files are the single source of truth — no vector database, no cloud dependency, no lock-in. Agents read and write through [MCP (Model Context Protocol)](https://modelcontextprotocol.io) tools; you edit the same notes in any editor at any time, and your changes enter the search index automatically. For the full English README, see [`README_EN.md`](README_EN.md).

- **Full-text retrieval with BM25** keyword ranking + jieba tokenization — deterministic, explainable, fully offline
- Multiple named *sources* (personal / team / company), each a directory on local disk or a mounted volume (NAS) → one shared memory across people and devices
- Returns **evidence snippets with provenance** (source + path), not pre-baked answers
- Fast start via persistent index cache; keeps serving when the mount is briefly offline

Use cases: personal AI note-taking, team knowledge bases that agents can query, lightweight local search over your own documents.

---

## 特点

- **多 source**：个人、团队、组织……每个 source 对应一个目录，可放本地盘或挂载盘
- **NAS、webDev跨设备**：记忆目录放 NAS\webDev，多台设备挂载远端文档共用同一份记忆
- **全文检索**：BM25 关键词匹配 + jieba 分词，无向量、无外部服务
- **打分可调**：近期修改、路径命中的文档加分，类似的文档旧文档降级（`scoring`）
- **证据而非答案**：检索返回带来源（source + path）的原文片段，结论由 AI 自己写
- **启动快**：索引持久化缓存，启动先用缓存应答、后台增量校验
- **掉盘可用**：挂载盘临时掉线时，索引与缓存原样保留，照常检索与读取
- **可写面收敛**：按配置区分只读 / 可写 source；AI 只能写显式可写的 source

设计与决策依据见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) 与 [`docs/adr/`](docs/adr/)。

---

## 目录

- [快速开始](#快速开始)
- [MCP 工具](#mcp-工具)
- [REST 端点](#rest-端点)
- [配置](#配置)
- [客户端接入](#客户端接入)
- [部署（Windows）](#部署windows)
- [更新记录](#更新记录)
- [License](#license)

---

## 快速开始

需要 Python 3.10+（Windows 上建议在 python.org 安装时勾选 `Add python.exe to PATH`）。

```powershell
git clone https://github.com/georgeyang1024/myMemory.git
cd myMemory
python3 run.py
```

`run.py` 是唯一的启动入口，只依赖标准库：自动创建虚拟环境 `.venv`、安装依赖、
启动服务，并负责后台启停。依赖装齐后重复启动秒过。

```powershell
python3 run.py                 # 前台启动（首次会交互建档：个人使用 / 团队使用）
python3 run.py --check         # 自检：构建一次索引并报告规模，不监听端口
python3 run.py --init          # 建档/修复配置 + 装齐依赖；已就绪则秒过
python3 run.py --reinstall     # 强制重装依赖
python3 run.py --recreate      # 删除并重建虚拟环境（环境坏掉时用）
python3 run.py --no-venv       # 用当前解释器跑，不建 venv
python3 run.py --index-url <URL>            # 走内网 / 镜像源
python3 run.py --bundle ./wheels            # 在联网机器上打离线依赖包
python3 run.py --find-links ./wheels --offline   # 在离线机器上安装
python3 run.py --help          # 全部选项
```

几点说明：

- **首次启动**交互建档（生成 `~/.myMemory/config.json`）：
  - **个人使用**（默认）：询问记忆目录，回车默认 `~/.myMemory/memory`（自动创建）；
  - **团队使用**：询问团队存储目录（子文件夹就是团队成员，一人一个，回车默认
    `~/.myMemory/users`）与管理员账号（逗号分隔多个，回车默认 admin），写入
    `multi_user`（`enabled: true`）；不建公共 source，需要共享记忆时再
    `config.py source add`。完成后按提示配 MCP 端点：`http://<服务器>:7083/mcp?user=<成员名>`。
  非交互环境（stdio、后台子进程）遇缺配置会报"未指定记忆存储"——先在终端完成
  建档，或运行 `python3 run.py --init`（非终端下按个人使用 + 默认目录兜底）。
- 未被识别的参数（如 `--check`、`--stdio`）会原样传给服务本身。
- `requirements.txt` 变更时会自动重装依赖，不需要手工清理环境。
- 配置里的环境变量只有 `MEMORY_CONFIG`（指定配置文件位置）；另有可选的
  `MYMEMORY_READY_TIMEOUT` 调整后台启动的就绪等待秒数（默认 180）。

### 后台运行

```powershell
python3 run.py --background   # 后台启动，就绪后打印端口与 PID 再退出
python3 run.py --status       # 看状态：PID、端口、索引规模
python3 run.py --stop         # 停止
python3 run.py --restart      # 重启
python3 run.py --logs         # 跟踪日志（Ctrl-C 只退出跟踪，不影响服务）
```

- 等的是"就绪"而不是"启动"：服务在索引就绪前不监听端口，`/health` 一通即可接请求。
- 已在跑时重复执行不会起第二个实例。
- 日志追加写入 `~/.myMemory/logs/myMemory.log`，PID 文件在同一目录。
- `--background` 与 `--stdio` / `--check` 互斥，会直接报错说明原因。
- 开机自启用 Windows 任务计划程序（见[部署](#部署windows)）。

---

## MCP 工具

常驻 7 个 + 删除类 2 个（默认隐藏），客户端看到的全名形如 `mcp__myMemory__save`
（服务名+工具名由客户端拼接）。

| 工具 | 一句话说明 |
|---|---|
| `search(query, limit=10, source="")` | BM25 全文检索，默认跨全部 source；返回带 `source`/`path`/`writable` 的原文片段，source 名、分类、文件名、日期（`26-08-04`）、型号类标识符都可直接当检索词 |
| `get-document(source, path, offset=0, limit=40000)` | 读取原文，字符级分页；响应带 `writable` 与 `stale`（掉盘时内容来自缓存） |
| `save(source, filename, content, category="")` | 写一篇记忆到 `<source>/<category>/<filename>.md`；**已存在即整篇覆盖**（不可撤销、无备份），空正文拒绝 |
| `rename(source, old_path, new_path)` | 同 source 内改名/移动一级分类；旧文件必须存在，**目标存在即拒绝**，不覆盖 |
| `replace(source, path, old_string, new_string)` | 全文**完全字面**替换 old→new（无正则/大小写折叠），命中几处换几处并返回 `replaced_count`；0 命中或空 new_string 拒绝 |
| `list-sources()` | 列出全部 source（`writable`/`available`/`doc_count`…），**不返回目录路径**；写入目标从这里选 |
| `recent(limit=10, source="")` | 最近更新，每文件一条；`edited_by` 区分 `agent`（工具写入）/ `scan`（人改的或绕过服务的改动） |
| `merge(source, from_path, to_path)`¹ | 把一篇**已存在**的记忆并入另一篇（并入段带 `## 来源` 标题与 `---` 分隔线），然后**删除源文件**；先写后删，删除失败以 `source_removed: false` 标出 |
| `delete(source, path)`¹ | **真删**（无备份、不可恢复），`path` 与 `search` 返回同形 |

> ¹ `merge` / `delete` 都真删文件，受配置开关 `allow_mcp_delete`（默认 `false`，重启生效）
> 控制：关闭时连工具都不会出现在 tools/list 里，LLM 看不到就不会调；开启需人工改配置。

**通用口径**（详细规则见各工具的参数描述与 docs/）：

- `path` 全部与 `search` / `recent` 返回值同形（含一级分类，不含 source 名，
  `.md` 后缀可带可不带）——原样复制，不要自行拼接
- 写入类工具创建路径：一级分类 + 文件名走同一套字符白名单，`..`、`\ / : * ? " < > |`
  与 Windows 保留名在语法层拒绝——不可能拼出 source 目录之外的落盘点
- 写入是**异步刷新**的：工具立刻返回，索引后台更新，可能几秒搜不到——响应里的
  `path` 就是凭据，不要重试
- 写入目标必须 `writable: true`；只读、掉盘、目录不存在一律拒绝
- **写入只有 MCP 工具**，REST 端点全为只读

---

## REST 端点

HTTP 模式下附带 4 个端点：

| 端点 | 用途 |
|---|---|
| `GET /health` | 版本、索引规模、构建时间、`rebuilding`、`verifying`、当前配置、source 列表（含目录路径与可用性） |
| `GET /search?q=…&limit=…&source=…` | 与 MCP `search` 返回结构完全一致 |
| `GET /recent?limit=…&source=…` | 与 MCP `recent` 返回结构完全一致 |
| `POST /reindex[?full=1]` | 立即刷新索引；默认增量，`full=1` 全量重建（多人共用下仅管理员，见下文） |

```powershell
curl.exe http://127.0.0.1:7083/health
curl.exe "http://127.0.0.1:7083/search?q=hello&limit=3"
curl.exe -X POST http://127.0.0.1:7083/reindex
```

**写入没有 REST 端点**，只能走 MCP 工具。注意 `/health` 会返回 source 的目录
路径（供人排障），部署到局域网前请确认这个暴露面可以接受。

---

## 配置

全部配置在一个 JSON 文件里：默认 `~/.myMemory/config.json`，
可用环境变量 `MEMORY_CONFIG` 指到别处。`index.cache` 与 `logs\` 都放在配置文件
所在目录，因此代码目录不落任何运行数据。配置修改**一律重启生效**。

```json
{
  "host": "127.0.0.1",
  "port": 7083,
  "poll_interval": 600,
  "scoring": {
    "recency_window_days": 30,
    "recency_bonus": 10,
    "path_match_bonus": 5,
    "strip_wikilinks": true,
    "historical_penalty": 0,
    "historical_keywords": []
  },
  "sources": [
    {
      "name": "memory",
      "dir": "D:\\memories",
      "writable": true,
      "description": "个人记忆"
    },
    {
      "name": "team",
      "dir": "\\\\server\\share\\team",
      "writable": true,
      "description": "团队共享记忆",
      "scoring": {
        "recency_bonus": 0
      }
    },
    {
      "name": "org",
      "dir": "Z:\\org\\docs",
      "writable": false,
      "description": "制度文档"
    }
  ],
  "domain_terms": ["RFC9424", "AES-GCM"],
  "allow_mcp_delete": false
}
```

| 字段 | 默认值 | 说明 |
|---|---|---|
| `sources` | 首次建档生成一个 `memory`（团队形态可无） | 见下 |
| `host` | `127.0.0.1` | 绑定地址。默认只监听本机；要局域网访问改 `0.0.0.0` |
| `port` | `7083` | 监听端口 |
| `poll_interval` | `600` | 轮询间隔（秒）；`0` 关闭（不影响 save 的主动刷新） |
| `extensions` | `[".md", ".txt"]` | 纳入索引的扩展名 |
| `chunk_size` / `chunk_overlap` | `800` / `120` | 切块窗口与重叠（字符） |
| `max_results` | `20` | 单次检索返回条数上限 |
| `snippet_chars` | `1200` | 单条片段截断长度 |
| `max_doc_chars` | `40000` | 单次读原文返回上限 |
| `max_create_chars` | `100000` | 单条记忆正文上限 |
| `max_cached_docs` | `1000` | 常驻内存的全文篇数上限（LRU）；`0` 不限 |
| `domain_terms` | `[]` | 领域术语词表，见下 |
| `allow_mcp_delete` | `false` | **AI 删除断路器**：`true` 时 `delete` 与 `merge`（删源）才对 AI 开放，关闭时两个工具不出现在工具列表 |
| `scoring` | 见下 | BM25 之外的打分调整，source 可按字段覆盖 |

**source**：`name` + `dir`（+ 可选 `writable`、`description`、`type`、`scoring`）。

- `name`：中英文、数字、下划线、连字符，1–64 字符，不含 `/`
- `dir`：写什么就用什么（盘符或 UNC 均可），不映射不转换
- `writable`：`false` 即只读；**省略即 `true`**。只读与名称无关
- `scoring`：可选，只对这个 source 生效的打分覆盖，见下方 `scoring` 说明
- 目录之间互不重叠、不嵌套（按真实路径比较，不区分大小写）
- 名称非法、重名、重叠、字段拼错或越界时**启动即失败**；目录访问不到只警告、不失败

**`domain_terms`（可选）**：型号、协议名这类 jieba 切不开的标识符。
分词时保持为一个词，且长字母数字串会额外发出它包含的术语，
让"用系列名检索完整型号"命中。改动会作废索引缓存（下次启动全量重建一次）。

**`scoring`（打分调整）**：`score` = BM25 分 + 路径命中加分 + 时间加分 − 历史降分，
近期修改、路径命中的文档排到前面，路径含历史标记词的旧文档降级。写法见上方 config.json：
全局一份，source 里的 `scoring` 只写要改的字段（上例 `team` 关掉时间加分），其余继承全局。
决策与实测见 [ADR-0027](docs/adr/0027-search-scoring-adjustments.md)、
[ADR-0033](docs/adr/0033-search-historical-keyword-penalty.md)。

| 字段 | 默认值 | 说明 |
|---|---|---|
| `recency_window_days` / `recency_bonus` | `30` / `10` | 按时间加分：30天内，按最近更新日期衰减加分，最大10分, `0` 即关闭 |
| `path_match_bonus` | `5` | 路径命中加分：查询的每个词都出现在文档路径（含文件名）时，整篇加一次 |
| `strip_wikilinks` | `true` | 分词前去掉 `[[...]]`，原文与偏移不变；改动会全量重建一次索引 |
| `historical_penalty` | `0` | 历史降分：相对路径（含目录与文件名）命中任一关键字时整篇扣一次，`0` 即关闭 |
| `historical_keywords` | `[]` | 历史标记关键字数组（≤100 条），子串匹配、不区分大小写；如 `["meeting", "已废弃"]` |

- 加分/降分只改排序，不改命中集合；数值不宜过大（BM25 分通常几到二十几，建议 |分值| ≤ 10）
- 批量改动、同步会刷新旧文档的修改时间——这类 source（如外部同步库）设 `"recency_bonus": 0`，
  并靠 `historical_penalty` + `historical_keywords` 兜底（不依赖 mtime）
- 历史降分默认关闭：关键字是子串匹配，任何路径含关键字（哪怕无关笔记）都会被降分，
  开启前先在语料里 grep 误伤面
- 想保持旧排序：`"scoring": {"recency_bonus": 0, "path_match_bonus": 0, "strip_wikilinks": false}`

CLI 等价写法：`python3 config.py config edit --scoring recency_bonus=8`（全局）、
`python3 config.py source edit team --scoring recency_bonus=0`（单个 source）、
`--reset-scoring 字段|all` 删除覆盖。改完重启生效；`source list` 显示覆盖，
`/health` 的 `scoring` 显示全局有效值与各 source 的合并结果。

**刷新与索引缓存**：

- 启动有缓存：先用缓存立即服务，后台增量校验（`/health` 的 `verifying`）
- 轮询（`poll_interval`）、save 之后、`/reindex` 都走同一条增量刷新路径，
  后台完成后原子替换，刷新期间请求不中断
- 掉盘期间该 source 的索引与缓存不更新、不删除，其他 source 照常；
  盘恢复后下一次轮询自动接上
- `max_cached_docs` 只限制常驻内存的**全文**篇数；所有文档照常进索引、照常可检索，
  不在缓存里的全文按需读盘。`/health` 的 `cached_docs` 是当前缓存篇数

### config.py CLI：管理配置

只依赖标准库，与服务共用同一套校验——**CLI 放行的配置，服务一定能启动**；
校验不通过时不改文件。

```powershell
python3 config.py source list
python3 config.py source add    <名称> --dir <目录> (--readonly | --writable) [--desc "描述"] [--restart]
python3 config.py source edit   <名称> [--dir <新目录>] [--name <新名称>] [--readonly | --writable] [--desc "描述"] [--restart]
python3 config.py source remove <名称> [--yes] [--restart]
python3 config.py config set poll_interval <秒> [--restart]
python3 config.py config set max_cached_docs <篇> [--restart]
python3 config.py config set allow_mcp_delete <true|false> [--restart]   # AI 删除开关，默认 false
python3 config.py config edit   --scoring <字段>=<值> [--scoring …] [--reset-scoring <字段>|all] [--restart]   # 全局打分调整
python3 config.py source edit   <名称> --scoring <字段>=<值> [--scoring …] [--reset-scoring <字段>|all] [--restart]
python3 config.py reindex [--full]        # 立即增量刷新（--full 全量），不用重启
python3 config.py restart                 # 调用 run.py --restart
```

> 目录一律用 `--dir` 指定（`add` 时必填）。写盘符（`Z:\…`）还是 UNC（`\\server\share\…`）
> 都行；用盘符时，服务必须运行在映射了该盘符的用户下。
> 修改类命令默认要手动 `config.py restart`，加 `--restart` 则改完直接重启。

### 多人共用配置（适合≤10人小队）

一个实例供小队共用：`sources` 里的为公共 source（对全部会话可见），服务器上
一个"个人根目录"的一级子目录 = 一个用户 = 一个个人 source。身份只来自
URL 上的 `?user=` 参数（知道用户名即可冒充——已接受的已知风险，后续收紧再议）。

配置写在 `config.json` 的 `multi_user` 子项（也可只用 CLI，不必手工编辑）：

```json
{
  "multi_user": {
    "enabled": true,
    "store_dir": "E:\\UserDocs",
    "admins": ["admin"],
    "guest_writable": false
  }
}
```

- `enabled`（默认 false）：多人共用总开关。缺省（或 false）时即使配了
  `multi_user` 块也是单机形态；预配好暂不启用很常见，要开就显式 true
  （CLI：`multi-user enable` / `multi-user disable`）。
- `store_dir`（必填）：个人根目录，其一级子目录 = 用户 = 个人 source；
- `admins`（可选）：管理员名单，`?user=<管理员名>` 时全域读写；
  未显式配置时默认 `["admin"]`（默认管理员是 admin）；
- `guest_writable`（默认 false）：访客（无 `?user=` 的匿名连接）能否写公共 source，
  默认只读，确需匿名写入才改 true。

```
服务器（管理员本人，终端操作）            用户（每人各自）
1. config.py source add 公共 --dir <目录> --readonly
2. config.py multi-user set <个人根目录>    # 写 enabled: true（重启生效）
3. config.py multi-user user add 张三   # 或手工建目录；建目录 = 开通，热生效
4. config.py multi-user admin add admin   # 可选：默认管理员已是 admin，无需再加
5. python run.py --background     # 启动/重启服务
                                           MCP 客户端配置（普通用户）：
                                           {"type": "http",
                                            "url": "http://<服务器>:7083/mcp?user=张三"}
                                           MCP 客户端配置（管理员）：
                                           {"type": "http",
                                            "url": "http://<服务器>:7083/mcp?user=李四"}
```

- 会话范围 = 全部公共 source + 本人个人 source（管理员为全域）；范围外按
  "source 不存在"拒绝，不揭示其他用户。误拼用户名 → 400 提示。
- `POST /reindex` 多人共用下**仅管理员**可触发（其余 403）；`config.py reindex`
  自动携带管理员身份（`admins` 非空时取名单首个，未配置 admins 时用默认
  admin，或用 `--user` 指定）。单机形态（开关关闭）不受限。
- 新用户开通：服务器建目录即可，服务不重启；新管理员：`multi-user admin add` + 重启。
- 收回用户：删目录（索引与缓存随之清除；先备份内容——无回收站）。
- 访客（无 `?user=` 的匿名连接）：只见公共 source，默认**只读**
  （确需匿名写入才在 `multi_user` 里设 `"guest_writable": true`）。
- `/health` 的 `multi_user` 块按身份分层：默认只报 `store_dir`（个人根目录）与
  `guest_writable`；`/health?user=<管理员名>` 才另给 `admins` 名单与各用户的
  开通情况。日常开通管理仍在服务器上用 `config.py multi-user show` 与
  `config.py multi-user user list`。
  
---

## 客户端接入

### HTTP（推荐：多客户端共享一个实例）

先独立启动服务（`run.py --background`），客户端配置：

```json
{
  "mcpServers": {
    "myMemory": {
      "type": "http",
      "url": "http://127.0.0.1:7083/mcp"
    }
  }
}
```

写入几秒内全局可见，且带 `/health`、`/search`、`/recent`。代价是要单独保活，
且**可写 + 免鉴权**的暴露面需要你判断部署位置（默认只监听 `127.0.0.1`）。

### stdio（客户端自己拉起进程）

```powershell
claude mcp add myMemory --scope user `
  -e MEMORY_CONFIG=%USERPROFILE%\.myMemory\config.json `
  -- C:\path\to\mymemory\.venv\Scripts\python.exe C:\path\to\mymemory\src\main.py --stdio
```

三个要点：

1. **命令必须直接指向 `.venv` 里的 python 和 `src/main.py`**：stdio 模式下 stdout 是
   JSON-RPC 的数据通道，任何多余输出都会让客户端解析失败。
   先用 `run.py` 把环境建好，再让客户端直接调。
2. **`MEMORY_CONFIG` 必须是绝对路径**：客户端启动子进程时的工作目录不确定。
3. **多个 stdio 进程共用一份 `index.cache`**：各自刷新后原子写回，谁最后写谁生效。
   需要准确的 `edited_by` 时用 HTTP 模式共享一个实例。

---

## 部署（Windows）

1. **Python 3.10+**：python.org 安装时勾选 `Add python.exe to PATH`。
   如果 `python3` 弹出 Microsoft Store 或没有输出，
   在「设置 → 应用 → 高级应用设置 → 应用执行别名」里关掉 `python3.exe` 占位程序。
2. **首次启动**：`python3 run.py --check`（建档 + 装依赖 + 自检），然后
   `python3 run.py --background`。也可以用 `config.py source add` 显式建档。
3. **防火墙**（局域网访问时，管理员 PowerShell）：

   ```powershell
   New-NetFirewallRule -DisplayName "myMemory MCP" -Direction Inbound `
     -Protocol TCP -LocalPort 7083 -Action Allow -Profile Private
   ```

   默认 `host` 是 `127.0.0.1`，只有本机使用时无需这一步；要开放给局域网，
   先把 `host` 改为 `0.0.0.0` 再放行端口。**本服务可写且免鉴权**，请确认网络可信。
4. **开机自启**：任务计划程序创建任务，程序 `python3`，
   参数 `"<仓库目录>\run.py" --background`，起始位置设为仓库目录。

---

## 更新记录

各版本变化见 [CHANGELOG.md](CHANGELOG.md)

---

## License

[MIT](LICENSE)
