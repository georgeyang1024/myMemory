# myMemory

**A local-first memory service for humans and AI agents.**
人与 AI 共同使用的本机记忆库：双方都往里写、都从里读，跨会话长期保存。

当前版本 **0.1.0**。

记忆就是普通的 Markdown / 文本文件，放在你自己指定的目录里——没有数据库、
没有云端、没有锁定。AI 通过 MCP 工具检索与写入；你随时用编辑器直接增删改，
改动照常进索引。

## 特点

- **多 source**：个人、团队、公司……每个 source 对应一个目录，可放本地盘或挂载盘
- **NAS、webDev跨设备**：记忆目录放 NAS\webDev，多台设备挂载远端文档共用同一份记忆
- **全文检索**：BM25 关键词匹配 + jieba 分词，无向量、无外部服务
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
python3 run.py                 # 前台启动（首次会交互询问记忆目录，回车用默认）
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

- **首次启动**交互询问记忆目录（回车默认 `~/.myMemory/memory`，自动创建），
  生成 `~/.myMemory/config.json`。非交互环境（stdio、后台子进程）遇缺配置会报
  "未指定记忆存储"——先在终端完成建档，或运行 `python3 run.py --init`。
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

共 5 个工具。客户端看到的全名形如 `mcp__myMemory__save`
（`mcp__<服务名>__<工具名>` 由客户端拼接）。

**记忆是本机上的 Markdown 文件**，本质是跨会话的长期记忆。
一次对话里值得复用的结论、决定、上下文，应显式 `save` 成一篇，不要只留在对话里。

### `search(query, limit=5, source="")`

全文检索，返回带来源的原文片段，**默认跨全部 source**。

- `query` — 检索词，超过 500 字符会被截断
- `limit` — 返回条数，越界自动钳制，不报错（默认上限 20，可配）
- `source` — 可选，只在这个 source 内检索

每条结果带独立的 `source`、`path`、`writable` 字段。source 名、分类目录名、
文件名、日期（`26-08-04` 这类 YY-MM-DD）、长标识符（型号、协议名）都参与检索，
可以直接用它们定位记忆。同一文档在结果中最多占 2 个位置，保证覆盖面。
响应中的 `index` 字段给出索引构建时间与规模，可据此判断数据新鲜度。

### `get-document(source, path, offset=0, limit=40000)`

按 source + path 读取原文，支持字符级分页。

- `source` / `path` — **必须是 `search` 或 `recent` 返回过的值**，不要自行拼接；
  不在索引中会被拒绝，并返回最相近的候选 `{source, path}`
- `offset` / `limit` — 分页参数；响应含 `has_more` 与 `next_offset`
- 响应带 `writable` 与 `stale`：`stale: true` 表示该 source 掉盘、全文来自缓存

### `save(source, filename, content, category="")`

**持久化写入**：在 `<source目录>/<category>/<filename>.md` 落一个 Markdown 文件，
分类为空时落在 source 根目录。**文件已存在时整篇覆盖，不可撤销、无备份。**

- `source` — 必填，且必须 `writable: true`；配置只读、掉盘、目录不存在都会被拒绝
- `category` — 一级分类目录名，不支持嵌套（`技术/协议` 会被拒绝）；不存在时自动创建
- `filename` — `.md` 后缀可带可不带
- `content` — **整篇内容**（不是追加），上限 100,000 字符；空正文一律拒绝

分类名与文件名走字符白名单（中英文、数字、空格、连字符、下划线、全角标点、
常用半角符号，长度 1–64 / 1–120）；`\ / : * ? " < > |`、`..`、首尾的 `.` 与空格、
Windows 保留名（`CON` `NUL` `COM1-9` `LPT1-9` 等）一律拒绝，因此不可能跨目录保存。

> **写入前先 `search` 查重**。覆盖没有备份；响应里的 `created` 与
> `replaced_char_count` 是察觉旧内容已被替换的唯一线索。
> 真正在意的记忆，把目录放进版本库才是可靠的兜底。

写入是**异步刷新**的：save 成功立刻返回，索引在后台增量更新。
刚写完立刻 search 可能搜不到——这不代表没写进去，响应里的 `path` 就是凭据。

### `list-sources()`

列出全部 source：`name`、`writable`、`available`、`unavailable_reason`
（`disk_offline` / `dir_missing`）、`doc_count`、`description`。
**不返回目录路径。** 写入目标必须从这里的返回选择（`writable: true` 才能写）。

### `recent(limit=10, source="")`

最近更新的文档，按时间倒序，每个文件一条。`edited_by` 区分：

- `agent` — 最后一次修改经 `save` 写入
- `scan` — 扫描发现的改动（人改的、别的设备或程序改的，不做推断）

绕过本服务的改动要等下一次轮询或 reindex 后才出现。

---

## REST 端点

HTTP 模式下附带 4 个端点：

| 端点 | 用途 |
|---|---|
| `GET /health` | 版本、索引规模、构建时间、`rebuilding`、`verifying`、当前配置、source 列表（含目录路径与可用性） |
| `GET /search?q=…&limit=…&source=…` | 与 MCP `search` 返回结构完全一致 |
| `GET /recent?limit=…&source=…` | 与 MCP `recent` 返回结构完全一致 |
| `POST /reindex[?full=1]` | 立即刷新索引；默认增量，`full=1` 全量重建 |

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
  "sources": [
    {"name": "memory",  "dir": "D:\\memories",              "writable": true,  "description": "个人记忆"},
    {"name": "team",    "dir": "\\\\server\\share\\team",    "writable": true,  "description": "团队共享记忆"},
    {"name": "company", "dir": "Z:\\company\\docs",          "writable": false, "description": "公司制度文档"}
  ],
  "domain_terms": ["RFC9424", "AES-GCM"]
}
```

| 字段 | 默认值 | 说明 |
|---|---|---|
| `sources` | 首次建档生成一个 `memory` | 见下 |
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

**source**：`name` + `dir`（+ 可选 `writable`、`description`、`type`）。

- `name`：中英文、数字、下划线、连字符，1–64 字符，不含 `/`
- `dir`：写什么就用什么（盘符或 UNC 均可），不映射不转换
- `writable`：`false` 即只读；**省略即 `true`**。只读与名称无关
- 目录之间互不重叠、不嵌套（按真实路径比较，不区分大小写）
- 名称非法、重名、重叠、字段拼错或越界时**启动即失败**；目录访问不到只警告、不失败

**`domain_terms`（可选）**：型号、协议名这类 jieba 切不开的标识符。
分词时保持为一个词，且长字母数字串会额外发出它包含的术语，
让"用系列名检索完整型号"命中。改动会作废索引缓存（下次启动全量重建一次）。

**刷新与索引缓存**：

- 启动有缓存：先用缓存立即服务，后台增量校验（`/health` 的 `verifying`）
- 轮询（`poll_interval`）、save 之后、`/reindex` 都走同一条增量刷新路径，
  后台完成后原子替换，刷新期间请求不中断
- 掉盘期间该 source 的索引与缓存不更新、不删除，其他 source 照常；
  盘恢复后下一次轮询自动接上
- `max_cached_docs` 只限制常驻内存的**全文**篇数；所有文档照常进索引、照常可检索，
  不在缓存里的全文按需读盘。`/health` 的 `cached_docs` 是当前缓存篇数

### config.py CLI：管理 source 与刷新周期

只依赖标准库，与服务共用同一套校验——**CLI 放行的配置，服务一定能启动**；
校验不通过时不改文件。

```powershell
python3 config.py source list
python3 config.py source add    <名称> --dir <目录> (--readonly | --writable) [--desc "描述"] [--restart]
python3 config.py source edit   <名称> [--dir <新目录>] [--name <新名称>] [--readonly | --writable] [--desc "描述"] [--restart]
python3 config.py source remove <名称> [--yes] [--restart]
python3 config.py config set poll_interval <秒> [--restart]
python3 config.py config set max_cached_docs <篇> [--restart]
python3 config.py reindex [--full]        # 立即增量刷新（--full 全量），不用重启
python3 config.py restart                 # 调用 run.py --restart
```

> 目录一律用 `--dir` 指定（`add` 时必填）。写盘符（`Z:\…`）还是 UNC（`\\server\share\…`）
> 都行；用盘符时，服务必须运行在映射了该盘符的用户下。
> 修改类命令默认要手动 `config.py restart`，加 `--restart` 则改完直接重启。

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

## License

[MIT](LICENSE)
