# myMemory — 术语表

> 本表定义本项目文档与代码中使用的术语。同一概念在文档、代码标识符、
> 对外 API 字段中必须使用同一名称。

---

## 服务与部署

**myMemory**
本 MCP 服务的名称，同时是 MCP server name。代码位于 `src/`（扁平模块布局，无包子目录）。
工具名**不带前缀**（`search`、`save`……）：客户端已按服务名做命名空间，全名形如 `mcp__myMemory__save`。
_Avoid_: `myMemory-save`（旧名）

> 下列 source 相关术语来自 [REQUIREMENTS](REQUIREMENTS.md)（0.1.0；2026-09-24 由 workspace 更名为 source，见 [ADR-0023](adr/0023-rename-workspace-to-source.md)），
> 取代原"记忆根目录 / root"与"扫描目录 / scan_dirs"。

**来源 / source**
一个有名字的记忆来源，**一个 source 恰好对应一个真实存在的目录**
（任意位置，含挂载盘）。名称由人定义，不含特殊字符；目录之间不得重叠或嵌套。
_Avoid_: workspace、工作区（旧名）、记忆根目录、root、库、scan_dirs

**只读来源 / readonly source**
config.json 中 `"writable": false` 的 source。服务永不向其目录写入，但照常索引、检索、读取；
不改磁盘权限，人可照常编辑。与名称无关。
_Avoid_: 用名称前缀 `readonly/` 表示只读（已废弃）、只读目录

**可写 / writable**
对外字段，表示"此刻能否经 MCP 写入"：配置 `writable` 为 true、source 可用、存储为 local 三者同时成立。
出现在 search、recent、get-document、list-sources 与 `/health` 的输出中。

**文档身份 / (source, path)**
一篇记忆的唯一标识是 source 名与 path 的二元组，二者在对外契约中是**两个独立字段**，
path 不拼接 source 名。

**配置文件 / config.json**
服务的全部配置（source 列表、刷新周期、网络与各项上限）。默认位于 `~/.myMemory/`，
唯一的环境变量 `MEMORY_CONFIG` 可覆盖其位置。由人或 CLI 修改，重启生效。

**存储类型 / storage type**
source 的内容从哪里来。目前只有 `local`（本地或挂载目录），预留 git / http / oss。

**编辑列表 / recent**
按文件 mtime 倒序的最近更新文件清单，每个文件只出现一次。不是日志，不保留历史版本。
_Avoid_: 编辑日志、历史记录

**编辑者 / editor**
recent 输出里登记"这篇记忆最后一次经 MCP 改动是谁动的"（原 `edited_by`
字段整体更名，见 [ADR-0031](adr/0031-editor-attribution.md)）。取值：
单机形态 `agent`（经 `save` / `rename` / `replace` 写入且之后未被改动）或 `scan`（扫描发现的改动；可能是人改的，也可能是别的设备或程序改的，
不做推断）；多人形态 MCP 写入记**路由身份**——用户名、管理员名或 `guest`
（访客写入；登记动作者而非记忆属主），非 MCP 改动仍 `scan`。
`agent` / `scan` / `guest` 是保留值，不得用作个人目录名与管理员名。
判定机制见 [ADR-0018](adr/0018-edited-by-via-status-file.md)。
_Avoid_: edited_by（旧字段名）、操作者、author

**可用性 / available**
source 当前能否访问。不可用有两种原因：**掉盘**（`disk_offline`）与**目录不存在**（`dir_missing`）。

**掉盘 / disk_offline**
source 所在**盘根**（`Z:\`、`\\server\share\`）访问不到。掉盘期间该 source 的索引与缓存不更新、不删除。
_Avoid_: 用"目录访问不到"判定掉盘——目录可能是被删除了

**目录不存在 / dir_missing**
盘根可访问，但 source 目录不见了（被删除或改名）。按删除处理：索引与缓存一并清除。

**陈旧 / stale**
get-document 在掉盘时从缓存返回的全文，可能不是磁盘上的最新版本。

**索引缓存 / index.cache**
与 `config.json` 同目录的持久化索引：每文件的块偏移、分词结果、`agent_mtime`、BM25 模型，以及当前缓存着的全文（≤ max_cached_docs 篇）。
启动时先用它立即服务，后台再增量校验。**不含向量**——本系统没有向量。
见 [ADR-0019](adr/0019-index-cache-incremental.md)。
_Avoid_: 向量库、向量存储

**增量更新 / incremental**
只重读、重分词 `(mtime, size)` 变化的文件，其余复用缓存；BM25 整体重建。
与之相对的**全量**（`--full`）忽略缓存。

**全文缓存 / max_cached_docs**
常驻内存的全文最多缓存多少篇，默认 1000，`0` 为不限；超出按 **LRU**（最近最少使用）丢弃。
只限制全文，**不限制索引**：所有文档照常可检索，不在缓存里的全文与片段按需读盘。
_Avoid_: max_docs（旧名，曾误实现为限制索引总数）

**删除断路器 / allow_mcp_delete**
config.json 的全局布尔开关，默认 **false**。关闭（或缺省）时 `delete`（真删）
不注册——工具列表里根本看不到；写入层再用同一开关兜一道闸。
merge 工具已移除（[ADR-0032](adr/0032-remove-merge-tool.md)），断路器语义
收窄为只管 `delete`。开启需人工改配置并重启生效。
_Avoid_: 运行时开关（删除能力没有热切换；也不做回收站 / 软删除）

**agent_mtime**
缓存条目上的字段：该文件经 save 写入后的落盘 mtime。当前 mtime 与之相等则 `edited_by` 为 `agent`。
取代已删除的 `status.json`。

**config.py（CLI）**
管理记忆服务配置的独立 CLI（位于仓库根目录；`src/config.py` 是配置校验模块）：source 增删改、刷新周期、reindex（默认增量，`--full` 全量）、restart。

---

## 多人共用

**多人共用 / multi-user**
`multi_user` 配置子项存在且 `enabled: true` 时的服务形态：一个服务进程部署在服务器上，
多人各带自己的
`?user=` 参数连接同一端点。未配置 `multi_user` 或开关关闭（`enabled` 默认
false）时是单机形态，行为与从前完全一致。
见 [ADR-0028](adr/0028-multi-user-shared-deployment.md)。
_Avoid_: 多租户、团队版

**公共 source / shared source**
`config.json` 的 `sources` 列表里配置的 source（概念沿用，字段不变）。
多人共用形态下对**全部会话**可见可检索；可写性照旧由 `writable` 字段决定。
_Avoid_: 团队 source、共享 source

**多人共用配置 / multi_user**
config.json 的可选大配置子项：
`{"multi_user": {"enabled": true, "store_dir": "…", "admins": ["…"], "guest_writable": false}}`。
`enabled`（默认 false）是显式开关：块可预先配好而不启用，只有 true 才进入
多人共用形态；内部 `store_dir` 必填、`admins` 可选（未配置默认
`["admin"]`，即默认管理员 admin；显式给出——含空数组——完全按给定值）、
`guest_writable` 可选（默认 false：访客对公共 source 只读）。
单机形态下没有管理员概念。

**个人根目录 / personal root**
config.json `multi_user.store_dir`（多人共用形态内必填）。
指向一个目录，其**一级子目录**每个对应一个用户。个人根目录与任何公共 source
目录不得重叠或嵌套。

**个人 source / personal source**
个人根目录下的一个一级子目录派生出的 source：目录名 = source 名 = 用户名，
`writable` 恒为 true。派生规则与热发现见 [ADR-0029](adr/0029-single-instance-scoping-and-dynamic-personal-sources.md)。
_Avoid_: 私有 source、用户 source

**用户名 / user name**
`?user=` 路由参数的值，必须与个人根目录下某个一级子目录名**逐字一致**
（目录名是权威拼写，大小写敏感比较——Windows 文件系统不区分大小写，但路由层区分）。

**会话范围 / session scope**
一个会话可见可操作的 source 集 = 全部公共 source + 至多一个个人 source
（由该请求的 `?user=` 决定）。范围之外的个人 source 一律按"source 不存在"拒绝，
**不揭示其他用户的存在**。

**访客 / guest**
不带 `?user=` 参数连接的会话：会话范围 = 仅公共 source，且默认**只读**
（`multi_user.guest_writable: false` 时即使公共 source 配 `writable: true`
也不可写——匿名写团队公共记忆默认禁止，见 [ADR-0030](adr/0030-multi-user-config-and-admin-full-scope.md)）。

**文件夹即授权 / folder-as-authorization**
开通模型：在个人根目录下创建 `<名字>` 目录，`?user=<名字>` 即可用。
**无身份验证**——知道用户名即可冒充（路由层硬、身份层软），是已接受的已知风险，
见 [ADR-0028](adr/0028-multi-user-shared-deployment.md)。管理员不走此通道，
走配置白名单（见下）。
_Avoid_: 账号、认证、登录

**管理员 / admin**
`multi_user.admins` 名单里的名字（未配置名单时默认 `admin`）：**纯授权身份**——不要求有同名个人目录、
名字不做命名白名单校验（与 `?user=` 值逐字匹配即可；含控制字符的名字无效，
防头注入）。会话范围为全域。开通 = 改配置 + 重启生效。
见 [ADR-0030](adr/0030-multi-user-config-and-admin-full-scope.md)。
_Avoid_: root、超管、超级用户

**全域范围 / full scope**
管理员会话的 source 集 = 全部公共 source + **全部**个人 source：
list-sources 全量、search/recent 默认跨全部。管理员对公共 source 照旧尊重
`writable`，对删除断路器不豁免。
_Avoid_: 管理员视图、超管模式

---

## 记忆与分类

**记忆 / memory**
某个 source 目录下的一个 Markdown 文件。path 相对该 source 目录，
MCP 写入形态为 `<分类>/<文件名>.md` 或（分类为空时）`<文件名>.md`。
可以由人直接在编辑器里写，也可以由 `save` 写入。

**分类 / category**
path 的第一段，即 source 下的一级目录名；**可为空**，为空时记忆直接位于 source 根目录。
**MCP 写入侧不支持嵌套**——`技术/协议` 不是合法的 category。注意这只约束工具写入：
索引用 `rglob` 递归任意层级，人工在编辑器里建多深的目录都照样能搜到、能读到。
分类不预设、不固定，由写入时决定；分类名参与分词，因此拿它当检索词就能
列举该分类下有什么。在写入侧它是一个过白名单的**标识符**，不是路径片段，
见 [ADR-0014](adr/0014-write-tool-boundary.md)。

---

## 索引结构

**文档 / Document（DocMeta）**
一个被纳入索引的物理文件。字段：source、相对该 source 的路径、mtime、字节大小、字符数。

**块 / Chunk**
文档按固定窗口切分出的检索单元。字段：`source`、`path`、`chunk_index`、
`char_start`、`char_end`、`text`。
切分规则：窗口 800 字符、相邻重叠 120 字符，见 [ADR-0005](adr/0005-fixed-window-chunking.md)。

**片段 / Snippet**
返回给调用方的 chunk 正文，按 `snippet_chars`（默认 1200 字符）截断后的形式。
"chunk"是索引内部单元，"snippet"是对外返回形式，二者不可混用。

**索引快照 / IndexSnapshot**
一次完整构建产出的**不可变**对象，包含 BM25 模型、全部 chunk、
文档元数据、`indexed_paths` 集合与构建时间戳。
服务在任意时刻只有一个"当前快照"，更新通过整体替换引用完成，不做原地修改。
见 [ADR-0007](adr/0007-in-memory-index-with-polling.md)。

**已索引路径集合 / indexed_paths**
当前快照中全部文档 `(source, path)` 二元组构成的 `frozenset`。
它是 get-document 的**文档身份边界**——只能读该集合中的二元组；多人共用形态下
它之上还叠着**会话范围**一层（范围外 source 按"不存在"拒绝），见
[ADR-0010](adr/0010-indexed-set-membership.md) 与 [ADR-0028](adr/0028-multi-user-shared-deployment.md)。

**指纹 / fingerprint**
索引缓存的配置指纹（`cache_fingerprint`：CACHE_FORMAT、切块参数、扩展名、
词典、strip_wikilinks 覆盖）——配置变了缓存作废，**不含**动态 source 名集
（用户增删由增量刷新消化，不作废缓存，见 [ADR-0029](adr/0029-single-instance-scoping-and-dynamic-personal-sources.md)）。
_Exists elsewhere_: ~~轮询指纹~~——2026-10-01 审查确认轮询是每轮无条件增量
refresh，不存在"(source 名集, 文件数, max mtime) 轮询指纹"，勿再引用。

**原子替换 / atomic swap**
后台重建完成后，用单条属性赋值把全局快照引用指向新快照。
Python 的属性赋值是原子的，因此不需要锁：在途请求继续持有旧快照直到结束。

---

## 检索

**BM25**
基于词频与逆文档频率的经典关键词排序算法。本服务用 `rank_bm25` 的 `BM25Okapi` 实现。
选它而非向量检索的理由见 [ADR-0002](adr/0002-bm25-over-vectors.md)。

**IDF 污染**
当语料中混入大量重复性数据（如枚举列表），高频词的逆文档频率被拉低，
导致该词失去区分度，**整个索引的打分基准**被破坏。
这是排除 CSV 的决定性理由，见 [ADR-0004](adr/0004-md-txt-only.md)。

**证据 / evidence**
本服务返回的内容形态：带来源路径与偏移量的原文片段。
与"答案"相对——服务不做合成，判断权留给调用方 LLM。
见 [ADR-0003](adr/0003-evidence-not-answers.md)。

---

## 协议与传输

**MCP（Model Context Protocol）**
LLM 客户端与工具服务之间的标准协议。本服务用官方 `mcp` Python SDK 实现。

**Streamable HTTP**
MCP 的 HTTP 传输方式，单端点 `POST/GET /mcp`。
本服务采用它，与团队既有知识库服务形态一致。
客户端配置写法：`{"type": "http", "url": "http://<ip>:<port>/mcp"}`。
区别于已被标记为 legacy 的 HTTP+SSE 双端点传输。

**MCPServer**
`mcp` SDK 2.x 中的服务端类，从 `mcp.server.mcpserver` 导入。
**注意**：1.x 中它叫 `FastMCP`，2.x 已更名，v1 代码在 2.x 下会 `ModuleNotFoundError`。
这是 `requirements.txt` 锁死版本的直接原因，见 [ADR-0011](adr/0011-pinned-deps-both-platforms.md)。

**DNS Rebinding 保护**
SDK 提供的 Host / Origin 头校验机制，防止局域网服务被恶意网页通过浏览器当作跳板访问。
本服务**不启用**该保护——面向受信任的局域网且免鉴权，Host 白名单不解决实际威胁。
实现上有两个陷阱：传入 `allowed_hosts` 为空的配置对象会拒绝所有请求（而非放行所有）；
且必须向 `streamable_http_app()` 显式传 `host`，否则 SDK 按默认值 `127.0.0.1`
自动装上 localhost-only 白名单。
见 [ARCHITECTURE §8.5](ARCHITECTURE.md#85-dns-rebinding-保护不启用)。

---

## 领域术语（`domain_terms` 配置项）

型号、协议名这类 jieba 切不开的标识符，配置在 config.json 的
`domain_terms` 数组（可选，默认为空）：分词时保持为一个词，且长字母数字串
会额外发出其包含的术语，让"用系列名检索完整型号"命中。
按你实际记什么来增删；改动会作废索引缓存（下次启动全量重建一次）。
实现见 `src/index.py` 的 `tokenize` / `_expand_long_token`。

> 为什么需要这个词表：`ABC123456789` 这类长标识符若被切成碎片，
> 用其中的系列名检索就无法命中。设计见
> [ADR-0012](adr/0012-path-and-term-tokenization.md)。
