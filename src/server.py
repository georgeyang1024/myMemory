"""服务层：MCP 工具定义、REST 端点、入参校验、响应裁剪与序列化。

本模块不做检索逻辑（那是 index.py 的职责），也不做落盘逻辑（那是 writer.py 的职责）。
"""

from __future__ import annotations

import difflib
import json
import logging
from datetime import datetime, timezone
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from config import Config
from index import ContentUnavailable, Hit, IndexHolder, IndexSnapshot
from storage import DISK_OFFLINE, open_storage
from writer import WriteError, delete_memory, merge_memory, rename_memory, replace_memory, save_memory
from version import __version__

logger = logging.getLogger(__name__)

# 服务版本。唯一出处是 version.py 的 __version__；演变过程见 docs/adr/。
VERSION = __version__

RECENT_DEFAULT = 10
RECENT_MAX = 20

SERVER_INSTRUCTIONS = """\
操作者（人）与 AI 共同的记忆系统。双方都往里写、都从里读。
主题不限、范围不限——工作、技术、生活、想法都可能在里面。

记忆是本机上的 Markdown 文件，本质是跨会话的长期记忆。
本次对话值得复用的结论、决定、上下文，显式 `save` 一篇，不要只留在对话里。

## 形态

- 记忆分属若干'source'（个人、团队、组织……），一篇记忆 = `source` + `path`。
- 有哪些 source、哪些可写：调 `list-sources`；每条返回都带 `writable`。
- 你：`save` 写入（分类只有一级，不存在会自动创建，可为空写在根目录）。
- 人：随时用编辑器直接增删改，改动照常进索引。

## 两条规矩

1. 返回的是*原文片段，不是答案*。结论由你写，并标注来源。
2. 写入前先 `search`：同名会直接覆盖。

## 检索特性

BM25 关键词匹配，不做同义改写：搜不到就换记忆里实际写过的词，
或用 source 名、分类名、文件名、日期（`26-08-04`）这类专有名词定位。
"""

SEARCH_DESCRIPTION = """\
关键词检索共同记忆，返回带来源的原文片段。默认跨全部 source。
用于回答"以前记过什么"，或写入前查重。
BM25 匹配，不做同义改写：搜不到就换记忆里实际写过的词。
同一文档最多占 2 个结果位；刚 save 的内容几秒后才可搜到。\
"""

GET_DOCUMENT_DESCRIPTION = """\
按 source + path 读一篇原文，支持字符分页（默认与上限 40000）。
不在索引中会被拒并给出相近候选；掉盘时 stale: true（可能不是最新）。\
"""

SAVE_DESCRIPTION = """\
写 <source>/<分类>/<文件名>.md。同名直接覆盖，不存在则新建。
source 必填且必须 writable 为 true；category 只有一级，不存在会自动创建，可为空；content 整篇替换。
索引异步刷新，刚写完搜不到不代表失败；path 就是凭据，不要重试。\
"""

LIST_SOURCES_DESCRIPTION = """\
列出全部 source：名称、writable、available、doc_count、description。
写入目标与 source 名称都以这里的返回为准。
description 说明该 source 放什么，用来判断一条记忆该写到哪里。\
"""

RECENT_DESCRIPTION = """\
最近更新的文档，按时间倒序，每文件一条。
edited_by 区分 agent（经 save 写入）/ scan（扫描发现改动，可能是人改的，也可能是别的设备或程序改的）。
绕过本服务的改动要等下一次索引刷新后才出现。\
"""

RENAME_DESCRIPTION = """\
给一篇记忆改名或移到另一个一级分类（同一个 source 内）。目标已存在即拒绝，不覆盖。
新文件名要与现有文件不同名，否则会被拒；改名成功后旧 path 不再存在。\
"""

REPLACE_DESCRIPTION = """\
在一篇记忆的全文里做字面替换 old_string → new_string，命中几处换几处，返回替换次数。
完全字面匹配：空格与换行必须逐字一致，不做正则、不做大小写折叠、不归一化换行。
old_string 命中 0 处或 new_string 为空都会被拒绝且不改动文件；整篇重写请改用 save。
path 支持多层目录的文件编辑（编辑的文件必须已存在）。\
"""

MERGE_DESCRIPTION = """\
把一篇记忆并入另一篇已存在的记忆（同一个 source 内），然后删除源文件。
并入段以 ## 源文件路径 为标题、前有 --- 分隔线，来源可追溯。
目标必须已存在，新建用 save。\
"""

DELETE_DESCRIPTION = """\
真删一篇记忆：无备份、不可恢复。删除能力默认关，开启后才出现在工具列表。
path 原样取自 search/recent 的返回值；删除是最终操作，删错只能重新 save。\
"""


def _clamp(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    """把入参钳制到合法范围，而不是报错。

    调用方是 LLM，越界参数是常见的、无害的失误；直接钳制比让它
    读一条报错再重试更省一轮往返。
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _index_meta(snapshot: IndexSnapshot) -> dict[str, Any]:
    """随每次检索返回的索引元信息。新鲜度信号通过这个字段送达调用方（ADR-0006）。"""
    return {
        "built_at": snapshot.built_at.isoformat(timespec="seconds"),
        "doc_count": snapshot.doc_count,
        "chunk_count": snapshot.chunk_count,
    }


def _source_names(config: Config) -> list[str]:
    return [ws.name for ws in config.sources]


def _writable(config: Config, snapshot: IndexSnapshot, name: str) -> bool:
    """有效可写：配置可写（且为 local）并且当前可用。掉盘或目录不存在时一律 false。"""
    ws = config.source(name)
    return bool(ws and ws.can_write and snapshot.availability_of(name).available)


def _writable_sources(config: Config, snapshot: IndexSnapshot) -> list[str]:
    return [ws.name for ws in config.sources if _writable(config, snapshot, ws.name)]


def _serialize_hit(hit: Hit, snippet_chars: int, writable: bool) -> dict[str, Any]:
    # text 为 None：全文不在内存缓存里，且所在 source 掉盘，片段暂时取不到。
    available = hit.text is not None
    snippet = hit.text or ""
    truncated = len(snippet) > snippet_chars
    if truncated:
        snippet = snippet[:snippet_chars]
    return {
        "source": hit.source,
        "path": hit.path,
        "writable": writable,
        "score": round(hit.score, 2),
        "chunk_index": hit.chunk_index,
        "char_start": hit.char_start,
        "char_end": hit.char_end,
        "snippet": snippet,
        "snippet_truncated": truncated,
        "snippet_available": available,
    }


def run_search(config: Config, snapshot: IndexSnapshot, query: str, limit: Any,
               source: str | None = None) -> dict[str, Any]:
    """search 的纯逻辑部分，MCP 工具与 REST 端点共用。"""
    query = (query or "").strip()
    scope = (source or "").strip() or None
    meta = _index_meta(snapshot)

    if scope is not None and config.source(scope) is None:
        return {"index": meta, "query": query, "source": scope, "total_matched": 0,
                "results": [], "error": f"source 不存在：{scope!r}",
                "sources": _source_names(config)}
    if not query:
        return {"index": meta, "query": "", "source": scope, "total_matched": 0,
                "results": [], "error": "query 不能为空"}
    if len(query) > config.max_query_chars:
        query = query[: config.max_query_chars]

    effective_limit = _clamp(
        limit, default=config.default_results, minimum=1, maximum=config.max_results
    )
    total, hits = snapshot.search(query, effective_limit, source=scope)
    return {
        "index": meta,
        "query": query,
        "source": scope,
        "total_matched": total,
        "returned": len(hits),
        "results": [
            _serialize_hit(hit, config.snippet_chars, _writable(config, snapshot, hit.source))
            for hit in hits
        ],
    }


def _suggest(snapshot: IndexSnapshot, source: str, path: str) -> list[dict[str, str]]:
    """拒绝时给出最相近的已索引文档，同 source 优先，也可跨 source。"""
    keys = sorted(snapshot.indexed_paths)
    if not path:
        return []
    same = [p for ws, p in keys if ws == source]
    picked = [(source, p) for p in difflib.get_close_matches(path, same, n=5, cutoff=0.4)]
    if len(picked) < 5:
        labels = {f"{ws}/{p}": (ws, p) for ws, p in keys}
        for label in difflib.get_close_matches(f"{source}/{path}", labels, n=5, cutoff=0.4):
            if labels[label] not in picked:
                picked.append(labels[label])
    if not picked:
        tail = path.rsplit("/", 1)[-1].lower()
        picked = [(ws, p) for ws, p in keys if tail and tail in p.lower()][:5]
    return [{"source": ws, "path": p} for ws, p in picked[:5]]


def run_get_document(
    config: Config, snapshot: IndexSnapshot, source: str, path: str, offset: Any, limit: Any
) -> dict[str, Any]:
    """get_document 的纯逻辑部分。

    安全边界就在这里：(source, path) 必须精确命中已索引集合。
    不做 realpath、不做前缀比较——因此路径穿越与符号链接逃逸
    在语义上无法发生。见 docs/adr/0010-indexed-set-membership.md。
    """
    source = (source or "").strip()
    path = (path or "").strip()
    key = (source, path)

    if key not in snapshot.indexed_paths:
        if not source:
            error = "source 不能为空。请原样传入 search 返回的 source。"
        elif config.source(source) is None:
            error = f"source 不存在：{source!r}。"
        else:
            error = "路径不在索引中。只能读取 search 返回过的文档。"
        return {
            "error": error,
            "source": source,
            "path": path,
            "suggestions": _suggest(snapshot, source, path),
        }

    cached = snapshot.cache.get(key, snapshot.entries[key].version) is not None
    try:
        content = snapshot.contents[key]
    except ContentUnavailable:
        return {
            "error": "这篇文档的全文不在内存缓存中，且所在 source 当前不可用（挂载盘掉线），暂时读不到。"
                     "盘恢复后即可读取。",
            "source": source,
            "path": path,
            "suggestions": [],
        }
    total_chars = len(content)
    start = _clamp(offset, default=0, minimum=0, maximum=max(total_chars, 0))
    length = _clamp(limit, default=config.max_doc_chars, minimum=1, maximum=config.max_doc_chars)
    piece = content[start : start + length]

    return {
        "source": source,
        "path": path,
        "writable": _writable(config, snapshot, source),
        # 掉盘时内容来自内存缓存，可能不是磁盘上的最新版本。
        "stale": cached and snapshot.availability_of(source).reason == DISK_OFFLINE,
        "total_chars": total_chars,
        "offset": start,
        "returned_chars": len(piece),
        "has_more": start + len(piece) < total_chars,
        "next_offset": start + len(piece) if start + len(piece) < total_chars else None,
        "content": piece,
    }


def run_save(config: Config, holder: IndexHolder, source: str,
             category: str, filename: str, content: str) -> dict[str, Any]:
    """save 的纯逻辑部分。

    与读取类工具不同，这里会改变磁盘状态，且**可能覆盖已有内容**。
    入参的合法性判断全在 writer.py：本函数只负责把 WriteError 翻译成调用方
    能读懂的响应体，记下 agent 标记，以及在写入成功后触发一次后台增量刷新。

    刷新是异步的（不等待构建完成就返回），因此返回体里要明确告诉调用方
    "现在还搜不到" —— 否则它会把紧接着的一次 search 落空误判为写入失败。
    """
    try:
        written = save_memory(config, source, category, filename, content)
    except WriteError as exc:
        # 被动探测：可用性平时只在刷新时更新，两次刷新之间盘掉了，快照会说"可写"而写入失败。
        # 写失败时对这个 source 探一次，状态变了就立即更新快照并请求增量刷新——
        # 报错里的可写列表与紧接着的 list-sources 因此与"为什么写不进去"一致。
        target = config.source((source or "").strip())
        if target is not None:
            if holder.update_availability(target.name, open_storage(target).probe()):
                holder.request_rebuild(f"写入 {target.name} 失败后可用性变化")
        return {"saved": False, "error": str(exc),
                "writable_sources": _writable_sources(config, holder.snapshot)}

    holder.mark_agent(written.source, written.path, written.mtime)
    started = holder.request_rebuild(
        f"{'创建' if written.created else '覆盖'}记忆 {written.source}/{written.path}"
    )
    return {
        "saved": True,
        "source": written.source,
        "path": written.path,
        "char_count": written.char_count,
        "created": written.created,
        "replaced_char_count": written.replaced_char_count,
        "index_refresh": "started" if started else "merged",
    }


def run_rename(config: Config, holder: IndexHolder, source: str,
               old_path: str, new_path: str) -> dict[str, Any]:
    """rename 的纯逻辑部分。与 run_save 同一形态：WriteError 翻译成响应体，
    成功后记下 agent 标记并触发后台增量刷新。

    旧路径是删、新路径是增：一改一删两条变化都靠同一轮刷新进索引，
    返回体里照例要提醒"新旧 path 此刻可能都搜不准"。
    """
    try:
        written = rename_memory(config, source, old_path, new_path)
    except WriteError as exc:
        target = config.source((source or "").strip())
        if target is not None:
            if holder.update_availability(target.name, open_storage(target).probe()):
                holder.request_rebuild(f"改名 {target.name} 失败后可用性变化")
        return {"renamed": False, "error": str(exc),
                "writable_sources": _writable_sources(config, holder.snapshot)}

    holder.mark_agent(written.source, written.path, written.mtime)
    started = holder.request_rebuild(f"改名记忆 {written.source} {old_path} → {written.path}")
    return {
        "renamed": True,
        "source": written.source,
        "old_path": old_path.strip(),
        "path": written.path,
        "index_refresh": "started" if started else "merged",
    }


def run_replace(config: Config, holder: IndexHolder, source: str, path: str,
                old_string: str, new_string: str) -> dict[str, Any]:
    """replace 的纯逻辑部分。响应里的 replaced_count 是调用方唯一的"改了几处"凭据。"""
    try:
        written = replace_memory(config, source, path, old_string, new_string)
    except WriteError as exc:
        target = config.source((source or "").strip())
        if target is not None:
            if holder.update_availability(target.name, open_storage(target).probe()):
                holder.request_rebuild(f"替换 {target.name} 失败后可用性变化")
        return {"replaced": False, "error": str(exc),
                "writable_sources": _writable_sources(config, holder.snapshot)}

    holder.mark_agent(written.source, written.path, written.mtime)
    started = holder.request_rebuild(f"替换记忆 {written.source}/{written.path} 的内容")
    return {
        "replaced": True,
        "source": written.source,
        "path": written.path,
        "replaced_count": written.replaced_char_count,
        "char_count": written.char_count,
        "index_refresh": "started" if started else "merged",
    }


def run_merge(config: Config, holder: IndexHolder, source: str,
              from_path: str, to_path: str) -> dict[str, Any]:
    """merge 的纯逻辑部分。

    一调用两条磁盘变化（目标更新 + 源文件删除），索引要等刷新后才会
    反映出来；source_removed=False 时内容已安全并入，只是源文件没删掉。
    """
    try:
        written = merge_memory(config, source, from_path, to_path)
    except WriteError as exc:
        target = config.source((source or "").strip())
        if target is not None:
            if holder.update_availability(target.name, open_storage(target).probe()):
                holder.request_rebuild(f"合并 {target.name} 失败后可用性变化")
        return {"merged": False, "error": str(exc),
                "writable_sources": _writable_sources(config, holder.snapshot)}

    holder.mark_agent(written.source, written.path, written.mtime)
    started = holder.request_rebuild(f"合并记忆 {written.source} {from_path.strip()} → {written.path}")
    return {
        "merged": True,
        "source": written.source,
        "from_path": from_path.strip(),
        "path": written.path,
        "old_char_count": written.old_char_count,
        "char_count": written.char_count,
        "source_removed": written.source_removed,
        "index_refresh": "started" if started else "merged",
    }


def run_delete(config: Config, holder: IndexHolder, source: str, path: str) -> dict[str, Any]:
    """delete 的纯逻辑部分。文件已删，没有 agent 标记要落——刷新后条目自动消失。

    删除被配置闸挡下时也走 WriteError → 响应体路径；工具在关闭时根本
    不会注册，这条报错只为 config 变更窗口期兜底。
    """
    try:
        written = delete_memory(config, source, path)
    except WriteError as exc:
        target = config.source((source or "").strip())
        if target is not None:
            if holder.update_availability(target.name, open_storage(target).probe()):
                holder.request_rebuild(f"删除 {target.name} 失败后可用性变化")
        return {"deleted": False, "error": str(exc),
                "writable_sources": _writable_sources(config, holder.snapshot)}

    started = holder.request_rebuild(f"删除记忆 {written.source}/{written.path}")
    return {
        "deleted": True,
        "source": written.source,
        "path": written.path,
        "char_count": written.char_count,
        "index_refresh": "started" if started else "merged",
    }


def run_list_sources(config: Config, snapshot: IndexSnapshot) -> dict[str, Any]:
    """不返回目录绝对路径：不向局域网调用方暴露磁盘结构。"""
    return {"sources": [
        {
            "name": ws.name,
            "writable": _writable(config, snapshot, ws.name),
            "available": snapshot.availability_of(ws.name).available,
            "unavailable_reason": snapshot.availability_of(ws.name).reason,
            "doc_count": snapshot.doc_count_of(ws.name),
            "description": ws.description,
        }
        for ws in config.sources
    ]}


def run_recent(config: Config, snapshot: IndexSnapshot, limit: Any,
               source: str | None = None) -> dict[str, Any]:
    """最近编辑列表：取自当前索引快照的 mtime，每个文件一条。"""
    scope = (source or "").strip() or None
    meta = {"built_at": snapshot.built_at.isoformat(timespec="seconds")}
    if scope is not None and config.source(scope) is None:
        return {"index": meta, "source": scope, "results": [],
                "error": f"source 不存在：{scope!r}", "sources": _source_names(config)}

    count = _clamp(limit, default=RECENT_DEFAULT, minimum=1, maximum=RECENT_MAX)
    entries = [e for e in snapshot.entries.values() if scope is None or e.source == scope]
    entries.sort(key=lambda e: (-e.mtime, e.source, e.path))
    results = [
        {
            "source": e.source,
            "path": e.path,
            "writable": _writable(config, snapshot, e.source),
            "updated_at": datetime.fromtimestamp(e.mtime, timezone.utc)
                                  .astimezone().isoformat(timespec="seconds"),
            "size": e.size,
            "edited_by": e.edited_by,
        }
        for e in entries[:count]
    ]
    return {"index": meta, "source": scope, "returned": len(results), "results": results}


def run_health(config: Config, snapshot: IndexSnapshot, rebuilding: bool = False,
               verifying: bool = False) -> dict[str, Any]:
    """/health 的响应体：索引规模、当前配置、source 列表。"""
    body: dict[str, Any] = {"status": "ok", "service": "myMemory", "version": VERSION}
    body.update(snapshot.describe())
    body.update(config.describe())
    # 给人排障用：带上每个 source 的目录（配置里写的原样路径）。
    # MCP 的 list-sources 刻意不返回目录，不向 AI 调用方暴露磁盘结构。
    dirs = {src.name: str(src.dir) for src in config.sources}
    body["sources"] = [
        {**item, "dir": dirs[item["name"]]}
        for item in run_list_sources(config, snapshot)["sources"]
    ]
    # 刚 save 过一条记忆或刚 reindex 时这里是 true：索引正在后台重建，
    # 此刻的 doc_count / built_at 还是上一版。
    body["rebuilding"] = rebuilding
    # 启动时用了缓存、后台增量校验还没完成：内容可能是上次关闭时的样子。
    body["verifying"] = verifying
    # 常驻内存的全文篇数（上限 max_cached_docs，LRU）。其余文档照常可检索，全文按需读盘。
    body["cached_docs"] = len(snapshot.cache)
    return body


def create_server(config: Config, holder: IndexHolder) -> MCPServer:
    """组装 MCP server：6 个常驻工具 + 4 个 REST 端点。
    delete 与 merge 受 allow_mcp_delete 控制（默认关）：开启时才注册，
    关闭时对 AI 彻底不可见。
    """
    server = MCPServer(
        name="myMemory",
        title="人与 AI 的共同记忆",
        version=VERSION,
        instructions=SERVER_INSTRUCTIONS,
    )

    @server.tool(
        name="search",
        title="检索共同记忆",
        description=SEARCH_DESCRIPTION,
    )
    def tool_search(
        query: Annotated[str, Field(description=(
            "检索词。BM25 匹配，不做同义改写。专有名词命中最准："
            "source 名、分类名、文件名、日期（26-08-04）、长标识符。"
            "多个词以空格分隔，超过 500 字符会被截断。"
        ))],
        limit: Annotated[int, Field(description=(
            "返回条数，1-20，默认 5。越界自动钳制，不报错。"
        ))] = 5,
        source: Annotated[str, Field(description=(
            "可选。只在这个 source 内检索；不传或传空字符串则跨全部 source。"
        ))] = "",
    ) -> str:
        payload = run_search(config, holder.snapshot, query, limit, source)
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @server.tool(
        name="get-document",
        title="读取记忆原文（可分页）",
        description=GET_DOCUMENT_DESCRIPTION,
    )
    def tool_get_document(
        source: Annotated[str, Field(description=(
            "必填。source 名，原样取自 search / recent 的返回值。"
        ))],
        path: Annotated[str, Field(description=(
            "文档路径，原样复制自 search 返回的 path，如 工作/周报.md，"
            "不含 source 名。普遍含中文与空格，不要自行拼接或猜测。"
        ))],
        offset: Annotated[int, Field(description=(
            "起始字符偏移，默认 0。翻页时把上次响应的 next_offset 传回。"
        ))] = 0,
        limit: Annotated[int, Field(description=(
            "本次返回字符数，上限 40000（也是默认值）。越界自动钳制。"
        ))] = 40000,
    ) -> str:
        payload = run_get_document(config, holder.snapshot, source, path, offset, limit)
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @server.tool(
        name="save",
        title="写入记忆（新建或覆盖）",
        description=SAVE_DESCRIPTION,
    )
    def tool_save(
        source: Annotated[str, Field(description=(
            "必填。要写入的 source，必须可写（list-sources 里 writable 为 true）。"
        ))],
        filename: Annotated[str, Field(description=(
            "文件名，.md 可带可不带，可含 ()[] 等常用符号。"
            "它和 source、category 一起决定覆盖谁——同名直接覆盖。"
        ))],
        content: Annotated[str, Field(description=(
            "整篇 Markdown 正文，**整篇替换**目标文件，不是追加。"
            "服务不加 frontmatter、标题或时间戳。"
        ))],
        category: Annotated[str, Field(description=(
            "一级分类目录名，如 工作、技术、生活。不能含 / —— 不支持嵌套目录。"
            "可为空，为空时写在 source 根目录。不存在时自动创建，建议复用分类。"
        ))] = "",
    ) -> str:
        payload = run_save(config, holder, source, category, filename, content)
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @server.tool(
        name="rename",
        title="改名或移动记忆（不覆盖）",
        description=RENAME_DESCRIPTION,
    )
    def tool_rename(
        source: Annotated[str, Field(description=(
            "必填。文件所在的 source，必须可写（list-sources 里 writable 为 true）。"
        ))],
        old_path: Annotated[str, Field(description=(
            "现路径，原样复制自 search/recent 返回的 path，如 工作/旧名.md，"
            "不含 source 名，.md 后缀可带可不带。"
        ))],
        new_path: Annotated[str, Field(description=(
            "新路径，格式同 old_path。只换文件名或换一级分类均可；"
            "目标已存在会被拒绝。"
        ))],
    ) -> str:
        payload = run_rename(config, holder, source, old_path, new_path)
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @server.tool(
        name="replace",
        title="局部替换记忆内容",
        description=REPLACE_DESCRIPTION,
    )
    def tool_replace(
        source: Annotated[str, Field(description=(
            "必填。文件所在的 source，必须可写（list-sources 里 writable 为 true）。"
        ))],
        path: Annotated[str, Field(description=(
            "文件路径，原样复制自 search/recent 返回的 path，"
            "不含 source 名，.md 后缀可带可不带。支持多层目录的文件，"
            "须已存在（先 search 确认）。"
        ))],
        old_string: Annotated[str, Field(description=(
            "要被替换的原文片段，必须逐字一致，建议连同少量上下文保证唯一。"
        ))],
        new_string: Annotated[str, Field(description=(
            "替换后的文本，不能为空。要删除内容时请传改写后的剩余句子。"
        ))],
    ) -> str:
        payload = run_replace(config, holder, source, path, old_string, new_string)
        return json.dumps(payload, ensure_ascii=False, indent=2)

    # delete 与 merge 都会真删文件，同受 allow_mcp_delete 断路器控制；
    # 关闭时连注册都不注册——LLM 在 tools/list 里看不到，就不会去调用。
    if config.allow_mcp_delete:

        @server.tool(
            name="merge",
            title="合并两篇记忆（并删源）",
            description=MERGE_DESCRIPTION,
        )
        def tool_merge(
            source: Annotated[str, Field(description=(
                "必填。两篇文件所在的 source，必须可写（list-sources 里 writable 为 true）。"
            ))],
            from_path: Annotated[str, Field(description=(
                "要并入的源文件路径，原样复制自 search/recent 返回的 path，"
                "不含 source 名，.md 后缀可带可不带。并入成功后此文件会被删除。"
            ))],
            to_path: Annotated[str, Field(description=(
                "合并目标路径，格式同 from_path，必须已存在——不存在时先 save 再合并。"
            ))],
        ) -> str:
            payload = run_merge(config, holder, source, from_path, to_path)
            return json.dumps(payload, ensure_ascii=False, indent=2)

        @server.tool(
            name="delete",
            title="删除记忆",
            description=DELETE_DESCRIPTION,
        )
        def tool_delete(
            source: Annotated[str, Field(description=(
                "必填。文件所在的 source，必须可写（list-sources 里 writable 为 true）。"
            ))],
            path: Annotated[str, Field(description=(
                "文件路径，原样复制自 search/recent 返回的 path，"
                "不含 source 名，.md 后缀可带可不带。"
            ))],
        ) -> str:
            payload = run_delete(config, holder, source, path)
            return json.dumps(payload, ensure_ascii=False, indent=2)

    @server.tool(
        name="list-sources",
        title="列出 source",
        description=LIST_SOURCES_DESCRIPTION,
    )
    def tool_list_sources() -> str:
        payload = run_list_sources(config, holder.snapshot)
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @server.tool(
        name="recent",
        title="最近编辑列表",
        description=RECENT_DESCRIPTION,
    )
    def tool_recent(
        limit: Annotated[int, Field(description=(
            "返回条数，1-20，默认 10。越界自动钳制，不报错。"
        ))] = RECENT_DEFAULT,
        source: Annotated[str, Field(description=(
            "可选。只看这个 source；不传或传空字符串则看全部。"
        ))] = "",
    ) -> str:
        payload = run_recent(config, holder.snapshot, limit, source)
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @server.custom_route("/health", ["GET"])
    async def health(_request: Request) -> JSONResponse:
        try:
            snapshot = holder.snapshot
        except RuntimeError as exc:
            return JSONResponse({"status": "starting", "detail": str(exc)}, status_code=503)
        return JSONResponse(run_health(config, snapshot, holder.rebuilding, holder.verifying))

    @server.custom_route("/search", ["GET"])
    async def search_endpoint(request: Request) -> JSONResponse:
        query = request.query_params.get("q", "")
        limit = request.query_params.get("limit", config.default_results)
        source = request.query_params.get("source", "")
        payload = run_search(config, holder.snapshot, query, limit, source)
        status_code = 400 if payload.get("error") else 200
        return JSONResponse(payload, status_code=status_code)

    @server.custom_route("/recent", ["GET"])
    async def recent_endpoint(request: Request) -> JSONResponse:
        limit = request.query_params.get("limit", RECENT_DEFAULT)
        source = request.query_params.get("source", "")
        payload = run_recent(config, holder.snapshot, limit, source)
        return JSONResponse(payload, status_code=400 if payload.get("error") else 200)

    @server.custom_route("/reindex", ["POST"])
    async def reindex_endpoint(request: Request) -> JSONResponse:
        # 没有写入能力，只触发一次刷新并与在跑的那轮合并——免鉴权下开放可接受。
        # 默认增量；?full=1 忽略缓存全量重建。
        full = request.query_params.get("full", "").lower() in ("1", "true", "yes")
        started = holder.request_rebuild("reindex 请求" + ("（全量）" if full else ""), full=full)
        return JSONResponse({"index_refresh": "started" if started else "merged",
                             "mode": "full" if full else "incremental"})

    return server
