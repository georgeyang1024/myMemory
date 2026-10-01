"""服务层：MCP 工具定义、REST 端点、入参校验、响应裁剪与序列化。

本模块不做检索逻辑（那是 index.py 的职责），也不做落盘逻辑（那是 writer.py 的职责）。
"""

from __future__ import annotations

import dataclasses
import difflib
import json
import logging
from datetime import datetime, timezone
from typing import Annotated, Any
from urllib.parse import parse_qs, quote, unquote

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from config import (Config, effective_sources, find_user, is_admin,
                    personal_sources_report)
from index import ContentUnavailable, Hit, IndexHolder, IndexSnapshot
from storage import DISK_OFFLINE, open_storage
from writer import WriteError, delete_memory, rename_memory, replace_memory, save_memory
from version import __version__

logger = logging.getLogger(__name__)

# 服务版本。唯一出处是 version.py 的 __version__；演变过程见 docs/adr/。
VERSION = __version__

RECENT_DEFAULT = 10
RECENT_MAX = 20

# 多人共用路由（docs/adr/0028/0029）：身份只来自 ?user= URL 参数；
# 这个头是中间件 → 工具层的内部通道，不是身份来源。
USER_HEADER = "x-mymemory-user"
_USER_HEADER_BYTES = USER_HEADER.encode("ascii")
# ASGI 头值按 latin-1 解码，中文等非 ASCII 用户名必须 percent-encode 传输。
_USER_MAX_LEN = 64
_VALIDATED_PATHS = frozenset({"/mcp", "/search", "/recent", "/health", "/reindex"})


def _user_from_query(query_string: bytes) -> str | None:
    """解析 ?user=：URL 解码（parse_qs 自带 unquote）、strip 一次；缺失/空白 → None（访客）。

    重复参数取首个；strip 后超 64 字符的值由调用方拒绝（这里不做，为了
    "strip 后为空视同缺省"与"超长 400"两条规则互不干扰）。
    """
    try:
        pairs = query_string.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001  decode with errors=replace 不会抛，兜底而已
        return None
    values = parse_qs(pairs, keep_blank_values=True).get("user")
    if not values:
        return None
    name = values[0].strip()
    return name or None

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

## 多人共用

多人共用部署时一个会话只包含公共 source 与本人（或管理员全域）的
个人 source：`list-sources` 返回即本会话全集，`search` / `recent`
默认检索也只覆盖这个范围；会话之外的 source 按"不存在"拒绝。\
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
editor 登记"谁动的"：agent（经写入工具写入且之后未被改动）/ scan（扫描发现改动，
可能是人改的，也可能是别的设备或程序改的）；多人共用形态下经 MCP 写入记路由身份
（用户名 / 管理员名 / guest）。
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


# --- 多人共用：路由中间件与会话限域（docs/adr/0028/0029/0030） ------------------

def scope_names(config: Config) -> frozenset[str]:
    """会话范围内的 source 名集：默认跨 source 路径（search/recent/get-document/
    suggest）用它过滤，否则会从全局索引快照泄漏范围外的内容。"""
    return frozenset(ws.name for ws in config.sources)


def user_from_context(ctx: Context) -> str | None:
    """从工具上下文读路由身份（中间件注入；percent-encode 传输，读侧解码）。

    stdio / 无传输 / 头缺失 → None（访客；单机形态同样为 None）。
    """
    headers = ctx.headers
    if not headers:
        return None
    raw = headers.get(USER_HEADER)
    if raw is None:
        return None
    return unquote(raw) or None


def scoped_config(config: Config, user: str | None) -> Config:
    """会话限域视图（ADR-0029 决策一）：访客 / 普通用户 / 管理员的唯一分叉点。

    - `multi_user` 未配置（单机形态）→ 原样返回（stdio 行为与从前一致）；
    - user 为 None/空 → 访客：仅公共 source；`guest_writable: false`（默认）时
      各公共 source 的 writable 一律置 false——访客写团队公共记忆默认禁止
      （ADR-0030 修订），写拒绝沿现有只读路径，list-sources 对访客显示只读；
    - is_admin → 全域：公共 + 全部派生个人 source（ADR-0030）；
    - 其余 → 公共 + 该用户的个人 source（范围外名字到不了这里：中间件已 400）。
    """
    multi = config.multi_user
    if multi is None:
        return config
    if not user:
        if multi.guest_writable:
            return dataclasses.replace(config, sources=config.sources)
        return dataclasses.replace(config, sources=tuple(
            dataclasses.replace(src, writable=False) for src in config.sources))
    if is_admin(config, user):
        return dataclasses.replace(config, sources=effective_sources(config))
    personal = find_user(config, user)
    return dataclasses.replace(
        config, sources=config.sources + ((personal,) if personal else ()))


def editor_identity(config: Config, user: str | None) -> str | None:
    """写入路径登记的编辑者（ADR-0031）：多人形态记路由身份（动作者非属主），
    单机形态 None（editor 属性回退为 agent）。访客写公共（guest_writable: true）
    记 "guest"。"""
    if config.multi_user is None:
        return None
    return user or "guest"


def _editor_reason(editor: str | None, action: str) -> str:
    """刷新日志理由带上编辑者——轻量审计线索（数据层不留历史，ADR-0031）。"""
    return f"{editor} {action}" if editor else action


class UserScopeMiddleware:
    """多人共用路由层（ADR-0028/0029）：解析 `?user=`、校验、注入内部头。

    - 校验路径 = `/mcp`、`/search`、`/recent`、`/health`、`/reindex`
      （先去掉一个尾斜杠再判定）；尾斜杠变体同样校验、未开通同样 400——
      fail loud，不静默降级为访客。其他路径（404）忽略 user 参数原样放行。
    - **身份只来自 `?user=`**：每个请求无条件删除客户端自带的
      `x-mymemory-user` 头，合法时以本中间件算出的值为准重新注入——
      客户端伪造该头绕不过路由校验。
    - user 值 URL 解码、strip 一次；strip 后为空视同缺省（访客）；
      超过 64 字符 400；未命中（既非管理员也无同名个人目录）400 并提示联系
      管理员开通，不列现有用户、不暴露 store_dir。
    - 注入值 percent-encode（UTF-8）：ASGI 头值按 latin-1 解码，中文等
      非 ASCII 用户名必须 ASCII 安全传输；读侧 `user_from_context` 解码。
    - user 是每请求判定，不绑定 mcp 会话（ADR-0028 收紧候选：initialize 绑定）。
    """

    def __init__(self, app, config: Config) -> None:
        self.app = app
        self._config = config

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        normalized = path[:-1] if path.endswith("/") and len(path) > 1 else path
        if normalized not in _VALIDATED_PATHS:
            await self.app(scope, receive, send)
            return
        headers = [(key, value) for key, value in scope.get("headers", [])
                   if key.lower() != _USER_HEADER_BYTES]
        name = _user_from_query(scope.get("query_string") or b"")
        if name is None:
            scope["headers"] = headers
            await self.app(scope, receive, send)
            return
        if len(name) > _USER_MAX_LEN:
            await self._reject(send, name)
            return
        if is_admin(self._config, name) or find_user(self._config, name) is not None:
            scope["headers"] = [*headers,
                                (_USER_HEADER_BYTES, quote(name, safe="").encode("ascii"))]
            await self.app(scope, receive, send)
            return
        await self._reject(send, name)

    async def _reject(self, send, name: str) -> None:  # noqa: ANN001
        body = json.dumps(
            {"error": f"user 不存在：{name}。该用户未开通（个人根目录下无此目录）"
                       "或不在管理员名单中，请联系管理员开通后重试"},
            ensure_ascii=False).encode("utf-8")
        await send({"type": "http.response.start", "status": 400, "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]})
        await send({"type": "http.response.body", "body": body})


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
               source: str | None = None,
               allowed_sources: frozenset[str] | None = None) -> dict[str, Any]:
    """search 的纯逻辑部分，MCP 工具与 REST 端点共用。

    allowed_sources：会话范围名集（多人共用限域的第二层，ADR-0029 修订）——
    默认跨 source 检索只在此集合内取材；单机形态恒为 None（不过滤）。
    """
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
    total, hits = snapshot.search(query, effective_limit, source=scope,
                                  allowed_sources=allowed_sources)
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


def _suggest(snapshot: IndexSnapshot, source: str, path: str,
             allowed_sources: frozenset[str] | None = None) -> list[dict[str, str]]:
    """拒绝时给出最相近的已索引文档，同 source 优先，也可跨 source。

    allowed_sources：只在会话范围内找——范围外用户的文档路径不能出现在建议里。
    """
    keys = sorted(snapshot.indexed_paths)
    if allowed_sources is not None:
        keys = [(ws, p) for ws, p in keys if ws in allowed_sources]
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
    config: Config, snapshot: IndexSnapshot, source: str, path: str, offset: Any, limit: Any,
    allowed_sources: frozenset[str] | None = None,
) -> dict[str, Any]:
    """get_document 的纯逻辑部分。

    安全边界有两层（docs/adr/0010-indexed-set-membership.md + ADR-0029 修订）：
    (source, path) 必须精确命中已索引集合，且 source 必须在会话范围内——
    indexed_paths 是全局集合（含所有用户的个人 source），范围检查不能只靠它，
    否则知道路径就能读到别人的记忆。不做 realpath、不做前缀比较——路径穿越
    与符号链接逃逸在语义上无法发生。
    """
    source = (source or "").strip()
    path = (path or "").strip()
    key = (source, path)

    if not source:
        return {
            "error": "source 不能为空。请原样传入 search 返回的 source。",
            "source": source,
            "path": path,
            "suggestions": [],
        }
    if config.source(source) is None:
        return {"error": f"source 不存在：{source!r}。", "source": source, "path": path,
                "suggestions": _suggest(snapshot, source, path, allowed_sources)}
    if key not in snapshot.indexed_paths:
        return {
            "error": "路径不在索引中。只能读取 search 返回过的文档。",
            "source": source,
            "path": path,
            "suggestions": _suggest(snapshot, source, path, allowed_sources),
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
             category: str, filename: str, content: str,
             editor: str | None = None) -> dict[str, Any]:
    """save 的纯逻辑部分。

    与读取类工具不同，这里会改变磁盘状态，且**可能覆盖已有内容**。
    入参的合法性判断全在 writer.py：本函数只负责把 WriteError 翻译成调用方
    能读懂的响应体，记下 agent 标记，以及在写入成功后触发一次后台增量刷新。

    editor：写入主体登记（ADR-0031）——多人形态记路由身份，单机 None。

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

    holder.mark_agent(written.source, written.path, written.mtime, editor)
    started = holder.request_rebuild(
        _editor_reason(editor, f"{'创建' if written.created else '覆盖'}记忆 "
                               f"{written.source}/{written.path}")
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
               old_path: str, new_path: str,
               editor: str | None = None) -> dict[str, Any]:
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

    holder.mark_agent(written.source, written.path, written.mtime, editor)
    started = holder.request_rebuild(_editor_reason(
        editor, f"改名记忆 {written.source} {old_path} → {written.path}"))
    return {
        "renamed": True,
        "source": written.source,
        "old_path": old_path.strip(),
        "path": written.path,
        "index_refresh": "started" if started else "merged",
    }


def run_replace(config: Config, holder: IndexHolder, source: str, path: str,
                old_string: str, new_string: str,
                editor: str | None = None) -> dict[str, Any]:
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

    holder.mark_agent(written.source, written.path, written.mtime, editor)
    started = holder.request_rebuild(_editor_reason(
        editor, f"替换记忆 {written.source}/{written.path} 的内容"))
    return {
        "replaced": True,
        "source": written.source,
        "path": written.path,
        "replaced_count": written.replaced_char_count,
        "char_count": written.char_count,
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
               source: str | None = None,
               allowed_sources: frozenset[str] | None = None) -> dict[str, Any]:
    """最近编辑列表：取自当前索引快照的 mtime，每个文件一条。

    allowed_sources：会话范围名集（多人共用限域的第二层）——默认跨 source
    只列范围内的条目，否则会把全局快照里其他用户的内容也列出来。
    """
    scope = (source or "").strip() or None
    meta = {"built_at": snapshot.built_at.isoformat(timespec="seconds")}
    if scope is not None and config.source(scope) is None:
        return {"index": meta, "source": scope, "results": [],
                "error": f"source 不存在：{scope!r}", "sources": _source_names(config)}

    count = _clamp(limit, default=RECENT_DEFAULT, minimum=1, maximum=RECENT_MAX)
    entries = [e for e in snapshot.entries.values()
               if (scope is None or e.source == scope)
               and (allowed_sources is None or e.source in allowed_sources)]
    entries.sort(key=lambda e: (-e.mtime, e.source, e.path))
    results = [
        {
            "source": e.source,
            "path": e.path,
            "writable": _writable(config, snapshot, e.source),
            "updated_at": datetime.fromtimestamp(e.mtime, timezone.utc)
                                  .astimezone().isoformat(timespec="seconds"),
            "size": e.size,
            "editor": e.editor,
        }
        for e in entries[:count]
    ]
    return {"index": meta, "source": scope, "returned": len(results), "results": results}


def run_health(config: Config, snapshot: IndexSnapshot, rebuilding: bool = False,
               verifying: bool = False, admin_view: bool = False) -> dict[str, Any]:
    """/health 的响应体：索引规模、当前配置、source 列表。

    admin_view（?user=<管理员名>）才揭示 admins 名单与开通的用户清单——
    不对局域网默认暴露谁开通了、谁是管理员。
    """
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
    if config.multi_user is not None:
        # 默认视角：只给目录位置与访客写开关（与上面的 dir 揭示同口径的内网排障信息）。
        mu: dict[str, Any] = {
            "store_dir": str(config.multi_user.store_dir),
            "guest_writable": config.multi_user.guest_writable,
        }
        if admin_view:
            mu["admins"] = list(config.multi_user.admins)
            users, _skipped = personal_sources_report(
                config.multi_user.store_dir, config.sources)
            mu["users"] = [
                {"name": src.name,
                 "available": snapshot.availability_of(src.name).available,
                 "doc_count": snapshot.doc_count_of(src.name)}
                for src in users
            ]
        body["multi_user"] = mu
    return body


def create_server(config: Config, holder: IndexHolder) -> MCPServer:
    """组装 MCP server：7 个常驻工具 + 4 个 REST 端点。
    delete 受 allow_mcp_delete 控制（默认关）：开启时才注册，
    关闭时对 AI 彻底不可见。merge 已移除（ADR-0032）。

    多人共用（ADR-0028/0029）：每个工具经 `ctx.headers` 读路由身份
    （中间件注入、只来自 `?user=`），构造会话限域视图与范围名集后调用
    run_* ——范围外 source 按"不存在"拒绝，默认检索/列表不泄漏其他用户。
    """
    server = MCPServer(
        name="myMemory",
        title="人与 AI 的共同记忆",
        version=VERSION,
        instructions=SERVER_INSTRUCTIONS,
    )

    def _session(ctx: Context) -> tuple[Config, frozenset[str], str | None]:
        """一次工具调用的会话三件套：限域配置、范围名集、编辑者身份。"""
        user = user_from_context(ctx)
        scoped = scoped_config(config, user)
        return scoped, scope_names(scoped), editor_identity(config, user)

    @server.tool(
        name="search",
        title="检索共同记忆",
        description=SEARCH_DESCRIPTION,
    )
    def tool_search(
        ctx: Context,
        query: Annotated[str, Field(description=(
            "检索词。BM25 匹配，不做同义改写。专有名词命中最准："
            "source 名、分类名、文件名、日期（26-08-04）、长标识符。"
            "多个词以空格分隔，超过 500 字符会被截断。"
        ))],
        limit: Annotated[int, Field(description=(
            "返回条数，1-20，默认 10。越界自动钳制，不报错。"
        ))] = 10,
        source: Annotated[str, Field(description=(
            "可选。只在这个 source 内检索；不传或传空字符串则跨全部 source。"
        ))] = "",
    ) -> str:
        scoped, allowed, _editor = _session(ctx)
        payload = run_search(scoped, holder.snapshot, query, limit, source, allowed)
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @server.tool(
        name="get-document",
        title="读取记忆原文（可分页）",
        description=GET_DOCUMENT_DESCRIPTION,
    )
    def tool_get_document(
        ctx: Context,
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
        scoped, allowed, _editor = _session(ctx)
        payload = run_get_document(scoped, holder.snapshot, source, path, offset, limit,
                                   allowed)
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @server.tool(
        name="save",
        title="写入记忆（新建或覆盖）",
        description=SAVE_DESCRIPTION,
    )
    def tool_save(
        ctx: Context,
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
        scoped, _allowed, editor = _session(ctx)
        payload = run_save(scoped, holder, source, category, filename, content, editor)
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @server.tool(
        name="rename",
        title="改名或移动记忆（不覆盖）",
        description=RENAME_DESCRIPTION,
    )
    def tool_rename(
        ctx: Context,
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
        scoped, _allowed, editor = _session(ctx)
        payload = run_rename(scoped, holder, source, old_path, new_path, editor)
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @server.tool(
        name="replace",
        title="局部替换记忆内容",
        description=REPLACE_DESCRIPTION,
    )
    def tool_replace(
        ctx: Context,
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
        scoped, _allowed, editor = _session(ctx)
        payload = run_replace(scoped, holder, source, path, old_string, new_string, editor)
        return json.dumps(payload, ensure_ascii=False, indent=2)

    # delete 会真删文件，受 allow_mcp_delete 断路器控制；
    # 关闭时连注册都不注册——LLM 在 tools/list 里看不到，就不会去调用。
    # merge 已移除（ADR-0032）：合并走"读两篇 → save → delete"或人工。
    if config.allow_mcp_delete:

        @server.tool(
            name="delete",
            title="删除记忆",
            description=DELETE_DESCRIPTION,
        )
        def tool_delete(
            ctx: Context,
            source: Annotated[str, Field(description=(
                "必填。文件所在的 source，必须可写（list-sources 里 writable 为 true）。"
            ))],
            path: Annotated[str, Field(description=(
                "文件路径，原样复制自 search/recent 返回的 path，"
                "不含 source 名，.md 后缀可带可不带。"
            ))],
        ) -> str:
            scoped, _allowed, _editor = _session(ctx)
            payload = run_delete(scoped, holder, source, path)
            return json.dumps(payload, ensure_ascii=False, indent=2)

    @server.tool(
        name="list-sources",
        title="列出 source",
        description=LIST_SOURCES_DESCRIPTION,
    )
    def tool_list_sources(ctx: Context) -> str:
        scoped, _allowed, _editor = _session(ctx)
        payload = run_list_sources(scoped, holder.snapshot)
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @server.tool(
        name="recent",
        title="最近编辑列表",
        description=RECENT_DESCRIPTION,
    )
    def tool_recent(
        ctx: Context,
        limit: Annotated[int, Field(description=(
            "返回条数，1-20，默认 10。越界自动钳制，不报错。"
        ))] = RECENT_DEFAULT,
        source: Annotated[str, Field(description=(
            "可选。只看这个 source；不传或传空字符串则看全部。"
        ))] = "",
    ) -> str:
        scoped, allowed, _editor = _session(ctx)
        payload = run_recent(scoped, holder.snapshot, limit, source, allowed)
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @server.custom_route("/health", ["GET"])
    async def health(request: Request) -> JSONResponse:
        try:
            snapshot = holder.snapshot
        except RuntimeError as exc:
            return JSONResponse({"status": "starting", "detail": str(exc)}, status_code=503)
        # ?user= 已由 UserScopeMiddleware 校验（未开通到不了这里，与其他端点同规则）；
        # 管理员身份才揭示 admins 与开通的用户清单。
        user = (request.query_params.get("user") or "").strip()
        admin_view = bool(user) and is_admin(config, user)
        return JSONResponse(run_health(config, snapshot, holder.rebuilding,
                                       holder.verifying, admin_view))

    @server.custom_route("/search", ["GET"])
    async def search_endpoint(request: Request) -> JSONResponse:
        # ?user= 已由 UserScopeMiddleware 校验（未命中到不了这里）；这里读参数
        # 构造限域视图——与工具层同一套规则（ADR-0028 REST 同规则）。
        user = (request.query_params.get("user") or "").strip() or None
        scoped = scoped_config(config, user)
        allowed = scope_names(scoped)
        query = request.query_params.get("q", "")
        limit = request.query_params.get("limit", config.default_results)
        source = request.query_params.get("source", "")
        payload = run_search(scoped, holder.snapshot, query, limit, source, allowed)
        status_code = 400 if payload.get("error") else 200
        return JSONResponse(payload, status_code=status_code)

    @server.custom_route("/recent", ["GET"])
    async def recent_endpoint(request: Request) -> JSONResponse:
        user = (request.query_params.get("user") or "").strip() or None
        scoped = scoped_config(config, user)
        allowed = scope_names(scoped)
        limit = request.query_params.get("limit", RECENT_DEFAULT)
        source = request.query_params.get("source", "")
        payload = run_recent(scoped, holder.snapshot, limit, source, allowed)
        return JSONResponse(payload, status_code=400 if payload.get("error") else 200)

    @server.custom_route("/reindex", ["POST"])
    async def reindex_endpoint(request: Request) -> JSONResponse:
        # 多人共用（ADR-0028）：重建索引影响整个实例，仅管理员可触发，
        # 其余 403；单机形态（multi_user 未配置）保持开放不受限。
        # 默认增量；?full=1 忽略缓存全量重建。
        user = (request.query_params.get("user") or "").strip()
        if config.multi_user is not None and not is_admin(config, user):
            return JSONResponse(
                {"error": "reindex 仅限管理员：多人共用下请带 ?user=<管理员名>"
                          "（单机形态不受此限）"},
                status_code=403)
        full = request.query_params.get("full", "").lower() in ("1", "true", "yes")
        started = holder.request_rebuild("reindex 请求" + ("（全量）" if full else ""), full=full)
        return JSONResponse({"index_refresh": "started" if started else "merged",
                             "mode": "full" if full else "incremental"})

    return server
