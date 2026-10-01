"""索引层：jieba 分词、BM25、按文件增量更新、索引缓存、快照原子替换。

本模块不认识 HTTP，也不做入参校验——那是 server.py 的职责。

核心设计：

- 索引快照是不可变对象，更新通过整体替换全局引用完成，请求路径无锁
  （docs/adr/0007-in-memory-index-with-polling.md）。
- 索引按**文件条目**组织：每个文件的正文、块偏移、分词结果与 agent 标记。
  重建时 (mtime, size) 未变的文件直接复用条目，只重读、重分词变化的文件；
  BM25 的统计量是全局的，每次整体重建（docs/adr/0019-index-cache-incremental.md）。
- 条目与 BM25 模型持久化为 index.cache：启动先用缓存立即服务，后台再增量校验。
- 挂载盘掉线时，该 source 的条目原样保留，不更新、不删除
  （docs/adr/0020-mounted-disk-offline.md）。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import pickle
import re
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath

import jieba
from rank_bm25 import BM25Okapi

from config import Config, Scoring, effective_sources
from corpus import Chunk, DocKey, DocMeta, split_text
from storage import AVAILABLE, DISK_OFFLINE, Availability, open_storage

logger = logging.getLogger(__name__)

# 缓存格式版本。改动 FileEntry 或缓存结构时递增，旧缓存随之作废。
CACHE_FORMAT = 4  # 2：workspace 更名为 source；3：全文移出条目，改为 LRU 全文缓存；
                  # 4：条目增 agent_editor（编辑者主体登记，ADR-0031；不兼容 3，升 0.5.0 全量重建一次）

# 同一文档在单次检索结果中最多占的位置数。
MAX_CHUNKS_PER_DOC = 2


@dataclass(frozen=True, slots=True)
class Hit:
    """一条检索结果。"""

    source: str
    path: str
    score: float
    chunk_index: int
    char_start: int
    char_end: int
    text: str | None   # None：全文不在缓存且所在 source 掉盘，片段暂时取不到


@dataclass(frozen=True, slots=True)
class FileEntry:
    """一个文件的索引数据（不含全文）。也是 index.cache 里的存储单元。

    全文不放在条目里，而是放在容量受限的 ContentCache（config.max_cached_docs）里：
    所有文档照常可检索，只是常驻内存的全文篇数有上限。

    agent_mtime：该文件经 myMemory save 写入后的落盘 mtime；与当前 mtime 相等即
    写后未被动过。agent_editor：那次写入的路由身份（用户名/管理员名/guest；
    单机写入为 None）。二者决定 editor 属性（docs/adr/0018、0031）：
    mtime 失配 → scan（扫描发现改动；可能是人改的，也可能是别的
    设备/程序改的，不做推断）；匹配且 agent_editor 为 None → agent；
    否则 → agent_editor。
    """

    source: str
    path: str
    mtime: float
    size: int
    char_count: int                           # 全文字符数（全文本身在 ContentCache 里）
    spans: tuple[tuple[int, int], ...]        # 每个块的 (char_start, char_end)
    tokens: tuple[tuple[str, ...], ...]      # 每个块正文的分词结果
    path_tokens: tuple[str, ...]              # source 名 + 路径的分词结果
    agent_mtime: float | None = None
    agent_editor: str | None = None

    @property
    def key(self) -> DocKey:
        return (self.source, self.path)

    @property
    def version(self) -> tuple[float, int]:
        return (self.mtime, self.size)

    @property
    def editor(self) -> str:
        if self.agent_mtime is None or self.agent_mtime != self.mtime:
            return "scan"
        return self.agent_editor or "agent"


def _content_signature(entries: dict[DocKey, FileEntry]) -> tuple:
    """决定 BM25 与块列表能否复用：参与检索的内容是否一模一样。agent 标记不算。"""
    return tuple(sorted((key, entry.version) for key, entry in entries.items()))


class ContentUnavailable(Exception):
    """全文不在缓存里，且所在 source 当前不可用（掉盘），读不到。"""


class ContentCache:
    """常驻内存的全文 LRU 缓存：(source, path) → (版本, 全文)。容量 0 表示不限。

    get 命中即记为"最近使用"；超出容量时丢弃最久没用的。版本 = (mtime, size)，
    与条目不一致的缓存视为过期。线程安全：检索线程与刷新线程都会读写它。
    """

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._items: "OrderedDict[DocKey, tuple[tuple[float, int], str]]" = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._items)

    def get(self, key: DocKey, version: tuple[float, int]) -> str | None:
        with self._lock:
            item = self._items.get(key)
            if item is None or item[0] != version:
                return None
            self._items.move_to_end(key)
            return item[1]

    def put(self, key: DocKey, version: tuple[float, int], text: str) -> None:
        with self._lock:
            self._items[key] = (version, text)
            self._items.move_to_end(key)
            if self.capacity > 0:
                while len(self._items) > self.capacity:
                    self._items.popitem(last=False)

    def offer(self, key: DocKey, version: tuple[float, int], text: str) -> None:
        """刷新用：只填空位、不挤人（docs/adr/0026）。

        刷新是后台维护，不算使用：key 已在缓存里就原地更新、不移动位置；
        有空位才放到末尾；满了就不放——免得整批灌入挤掉掉盘 source 读不回来的全文。
        """
        with self._lock:
            if key in self._items:
                self._items[key] = (version, text)
            elif self.capacity <= 0 or len(self._items) < self.capacity:
                self._items[key] = (version, text)

    def retain(self, entries: dict[DocKey, "FileEntry"]) -> None:
        """只保留仍在索引里、且版本一致的全文。"""
        with self._lock:
            for key in [k for k, (v, _) in self._items.items()
                        if k not in entries or entries[k].version != v]:
                del self._items[key]

    def dump(self) -> list[tuple[DocKey, tuple[float, int], str]]:
        """按最近使用从旧到新导出，供写入 index.cache。"""
        with self._lock:
            return [(k, v, text) for k, (v, text) in self._items.items()]

    def load(self, items) -> None:
        for key, version, text in items:
            self.put(key, version, text)


class _Contents(Mapping):
    """snapshot.contents：(source, path) → 全文。先查缓存，没有再读盘并放进缓存。

    所在 source 掉盘且不在缓存里时抛 ContentUnavailable——不去碰一块访问不到的盘。
    """

    def __init__(self, snapshot: "IndexSnapshot") -> None:
        self._snapshot = snapshot

    def __getitem__(self, key: DocKey) -> str:
        snap = self._snapshot
        entry = snap.entries[key]              # 不在索引里：KeyError
        text = snap.cache.get(key, entry.version)
        if text is not None:
            return text
        if not snap.availability_of(entry.source).available:
            raise ContentUnavailable(key)
        storage = snap.storages.get(entry.source)
        if storage is None:
            raise ContentUnavailable(key)
        try:
            text = storage.read_text(entry.path)
            stat = storage.stat(entry.path)
        except OSError as exc:
            raise ContentUnavailable(key) from exc
        # 磁盘上已是更新的版本时照样返回（更新鲜），但不进缓存——偏移以索引为准，等下一轮刷新。
        if stat is not None and (stat.mtime, stat.size) == entry.version:
            snap.cache.put(key, entry.version, text)
        return text

    def __iter__(self):
        return iter(self._snapshot.entries)

    def __len__(self) -> int:
        return len(self._snapshot.entries)


class IndexSnapshot:
    """一次构建的产物。构造后视为不可变（全文缓存 cache 是共享的可变对象）。"""

    __slots__ = (
        "entries", "availability", "bm25", "chunks", "documents", "contents", "cache",
        "storages", "indexed_paths", "built_at", "build_seconds", "signature", "scoring",
    )

    def __init__(
        self,
        *,
        entries: dict[DocKey, FileEntry],
        availability: dict[str, Availability],
        cache: ContentCache,
        storages: dict[str, object] | None = None,
        build_seconds: float = 0.0,
        bm25: BM25Okapi | None = None,
        reuse: "IndexSnapshot | None" = None,
        scoring: Mapping[str, Scoring] | None = None,
    ) -> None:
        """reuse：内容签名相同的旧快照，直接沿用它的块列表与 BM25，只换条目与可用性。"""
        self.entries = entries
        self.availability = availability
        # source 名 → 有效打分调整。未列出的 source 不做任何加分。
        self.scoring: Mapping[str, Scoring] = dict(scoring or {})
        self.cache = cache
        self.storages = storages or {}
        self.signature = _content_signature(entries)
        keys = sorted(entries)

        if reuse is not None and reuse.signature == self.signature:
            self.chunks = reuse.chunks
            self.bm25 = reuse.bm25
        else:
            self.chunks = [
                Chunk(source=entry.source, path=entry.path, chunk_index=i,
                      char_start=start, char_end=end)
                for entry in (entries[k] for k in keys)
                for i, (start, end) in enumerate(entry.spans)
            ]
            if bm25 is None and self.chunks:
                bm25 = BM25Okapi([
                    list(entry.path_tokens) + list(tokens)
                    for entry in (entries[k] for k in keys)
                    for tokens in entry.tokens
                ])
            self.bm25 = bm25 if self.chunks else None

        self.documents = {
            key: DocMeta(source=e.source, path=e.path, mtime=e.mtime, size=e.size,
                         char_count=e.char_count)
            for key, e in entries.items()
        }
        self.contents = _Contents(self)
        # 唯一的安全边界：get-document 只接受该集合中的 (source, path)。
        # 见 docs/adr/0010-indexed-set-membership.md
        self.indexed_paths: frozenset[DocKey] = frozenset(entries)
        self.built_at = datetime.now(timezone.utc).astimezone()
        self.build_seconds = build_seconds

    @property
    def doc_count(self) -> int:
        return len(self.entries)

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    def describe(self) -> dict[str, object]:
        return {
            "built_at": self.built_at.isoformat(timespec="seconds"),
            "build_seconds": round(self.build_seconds, 2),
            "doc_count": self.doc_count,
            "chunk_count": self.chunk_count,
        }

    def doc_count_of(self, source: str) -> int:
        return sum(1 for ws, _ in self.entries if ws == source)

    def _chunk_text(self, chunk: Chunk) -> str | None:
        try:
            return self.contents[chunk.key][chunk.char_start:chunk.char_end]
        except ContentUnavailable:
            return None

    def availability_of(self, source: str) -> Availability:
        return self.availability.get(source, AVAILABLE)

    def search(self, query: str, limit: int, source: str | None = None, *,
               allowed_sources: frozenset[str] | None = None,
               now: float | None = None) -> tuple[int, list[Hit]]:
        """BM25 检索。返回 (命中总数, 前 limit 条)。

        source 为 None 时跨全部 source（统一索引）；否则只保留该 source 的命中。
        allowed_sources（多人共用限域，ADR-0029 修订）：命中 chunk 所在 source
        必须在该名集内——与 source 二选一或叠加均可；默认路径（source=None）没有
        它就会把全局索引里所有用户的内容都返回，因此限域调用必须传。

        命中判定用"chunk 中确实出现了至少一个查询词"，而不是"BM25 分数 > 0"。

        原因：rank_bm25 的 BM25Okapi 用
        idf = log(N - n + 0.5) - log(n + 0.5)，当一个词出现在超过半数
        chunk 中时 idf 为负；其 epsilon 兜底取的是 average_idf 的倍数，
        在平均 idf 本身为负时依然为负。此时真实命中的分数是负的，
        按 score > 0 过滤会把它们整个丢掉——查询词越常见，丢得越彻底。

        返回的 score = BM25 分 + 路径命中加分 + 时间加分 − 历史关键字降分（见 _document_bonus）。
        now：计算时间加分用的当前时间（epoch 秒），缺省取 time.time()，便于测试固定时间。
        """
        if self.bm25 is None or not self.chunks:
            return 0, []

        tokens = tokenize(query)
        if not tokens:
            return 0, []

        scores = self.bm25.get_scores(tokens)
        unique_tokens = set(tokens)
        doc_freqs = self.bm25.doc_freqs
        ranked = [
            i for i in range(len(self.chunks))
            if not unique_tokens.isdisjoint(doc_freqs[i])
            and (source is None or self.chunks[i].source == source)
            and (allowed_sources is None or self.chunks[i].source in allowed_sources)
        ]
        if not ranked:
            return 0, []
        # 加分只作用于已确定的命中，命中集合与总数不受影响（ADR-0013 不变）。
        bonus = self._bonus_function(query, time.time() if now is None else now)
        final = {i: float(scores[i]) + bonus(self.chunks[i]) for i in ranked}
        ranked.sort(key=final.__getitem__, reverse=True)

        # 多样性约束：同一文档最多先占 MAX_CHUNKS_PER_DOC 个位置。
        # 一个大文件的相邻 chunk 分数往往接近，不加约束会让 top-5 全来自
        # 同一篇文档，白白浪费调用方有限的上下文预算。
        # 若因此不足 limit 条，再从被压下的结果里按分数回填。
        primary: list[int] = []
        deferred: list[int] = []
        per_doc: dict[DocKey, int] = {}
        for i in ranked:
            key = self.chunks[i].key
            if per_doc.get(key, 0) < MAX_CHUNKS_PER_DOC:
                per_doc[key] = per_doc.get(key, 0) + 1
                primary.append(i)
                if len(primary) >= limit:
                    break
            else:
                deferred.append(i)

        selected = primary[:limit]
        if len(selected) < limit:
            selected.extend(deferred[: limit - len(selected)])

        hits = []
        for i in selected:
            chunk = self.chunks[i]
            hits.append(
                Hit(
                    source=chunk.source,
                    path=chunk.path,
                    score=final[i],
                    chunk_index=chunk.chunk_index,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    text=self._chunk_text(chunk),
                )
            )
        return len(ranked), hits

    def _bonus_function(self, query: str, now: float):
        """返回 chunk → 加分的函数。同一文档只算一次（同一文档的所有 chunk 加同样的分）。"""
        terms = query.lower().split()
        per_doc: dict[DocKey, float] = {}

        def bonus(chunk: Chunk) -> float:
            key = chunk.key
            if key not in per_doc:
                per_doc[key] = _document_bonus(self.scoring.get(chunk.source, NO_SCORING),
                                               chunk.path, self.entries[key].mtime, terms, now)
            return per_doc[key]

        return bonus


# 快照未提供某 source 的打分配置时使用：不做任何调整。
NO_SCORING = Scoring(recency_bonus=0, path_match_bonus=0, historical_penalty=0,
                     strip_wikilinks=False)


def _document_bonus(scoring: Scoring, path: str, mtime: float, terms: list[str],
                    now: float) -> float:
    """一篇文档在 BM25 分之上的加分（可能为负）。

    - 路径命中：查询的每个词都是 source 内相对路径（小写）的子串时加固定分，至多一次。
    - 时间：按 mtime 在窗口内线性衰减；窗口外为 0；mtime 在未来按年龄 0 计（不超过上限）。
      mtime 不等于内容日期——批量改动或同步会刷新它，这类 source 应把 recency_bonus 设为 0。
    - 历史关键字：相对路径含任一 historical_keywords（子串、不区分大小写）时扣固定分，
      命中多个也只扣一次（docs/adr/0033）。关键字匹配在路径上，不依赖 mtime：
      mtime 失效时由它兜底。
    """
    bonus = 0.0
    lowered = path.lower()
    if scoring.path_match_bonus and terms and all(term in lowered for term in terms):
        bonus += scoring.path_match_bonus
    window = scoring.recency_window_days
    if window and scoring.recency_bonus:
        age_days = max(0.0, (now - mtime) / 86400.0)
        if age_days < window:
            bonus += scoring.recency_bonus * (1 - age_days / window)
    if scoring.historical_penalty and any(
            kw in lowered for kw in scoring.historical_keywords):
        bonus -= scoring.historical_penalty
    return bonus


def scoring_of(config: Config) -> dict[str, Scoring]:
    return {src.name: src.scoring for src in config.sources}


_jieba_ready = False
_jieba_lock = threading.Lock()


def _ensure_jieba(terms: tuple[str, ...] = ()) -> None:
    """初始化 jieba 并载入领域词典。幂等，线程安全。

    terms 来自配置的 domain_terms：型号、协议名这类 jieba 切不开的
    标识符，先 add_word 让它们保持为一个词。
    """
    global _jieba_ready
    if _jieba_ready:
        jieba_add = jieba.add_word
        for term in terms:
            jieba_add(term)
        return
    with _jieba_lock:
        if not _jieba_ready:
            jieba.initialize()
            _jieba_ready = True
        for term in terms:
            jieba.add_word(term)


_LONG_ALNUM = re.compile(r"^[a-z0-9]{8,}$")
# 目录常用 YY-MM-DD（26-08-04 这类）命名。jieba 会把它拆成 26 / 08 / 04 三个词，
# 这些碎片几乎出现在每一个带日期的目录里，IDF 被稀释到没有区分度，却仍然靠
# 词频把无关文档顶上来；而日期作为整体的语义则完全丢失。
# 因此：把日期串从待分词文本中摘出，只作为一个完整检索词发出。
_DATE = re.compile(r"\d{2,4}-\d{1,2}-\d{1,2}")


def _expand_long_token(token: str, terms: tuple[str, ...]) -> list[str]:
    """把长字母数字串中包含的领域术语额外发出为独立词。

    jieba 的搜索模式只对中文长词做二次切分，字母数字串始终是一个整体：
    完整型号串不会产出它的系列名，导致用系列名检索命中不到完整型号。
    这条规则只作用于长度 >= 8 的纯字母数字 token，
    影响面被限制在型号 / 协议名这类标识符上。
    """
    lowered_terms = {t.lower() for t in terms}
    return [t for t in lowered_terms if t in token and t != token]


def tokenize(text: str, terms: tuple[str, ...] = ()) -> list[str]:
    """检索用分词。搜索引擎模式会产出更细粒度的重叠切分，提高召回。"""
    _ensure_jieba(terms)
    lowered = text.lower()
    dates = _DATE.findall(lowered)
    if dates:
        lowered = _DATE.sub(" ", lowered)
    tokens: list[str] = []
    for token in jieba.cut_for_search(lowered):
        token = token.strip()
        if not token:
            continue
        tokens.append(token)
        if _LONG_ALNUM.match(token):
            tokens.extend(_expand_long_token(token, terms))
    tokens.extend(dates)
    return tokens


def tokenize_path(path: str, terms: tuple[str, ...] = ()) -> list[str]:
    """路径分词。

    文件名与目录名往往承载正文里没有的语义：
      - 纯表格文档，正文中不含它的文件名里的关键词
      - 带日期的目录名，日期只存在于目录名上
      - 型号/标识符，同样只出现在路径上

    因此把路径一并纳入检索词。分隔符替换为空格，让 jieba 能正确切分。
    """
    lowered = path.lower()
    dates = _DATE.findall(lowered)
    # 先摘出日期，再替换分隔符——否则分隔符替换会把日期串毁掉。
    normalized = _DATE.sub(" ", lowered)
    for separator in ("/", "-", "_", ".", "＿"):
        normalized = normalized.replace(separator, " ")
    return tokenize(normalized, terms) + dates


# --- 构建与增量更新 --------------------------------------------------------------

# Obsidian 式双链 [[目标|别名]]，只在一行之内。
_WIKILINK = re.compile(r"\[\[[^\n]*?\]\]")


def mask_wikilinks(text: str) -> str:
    """把每个 [[...]] 整段替换为等长空格：字符偏移不变，跨切块边界的链接也能完整去掉。"""
    return _WIKILINK.sub(lambda m: " " * len(m.group()), text)


def _source_scoring(config: Config, name: str) -> Scoring:
    for src in config.sources:
        if src.name == name:
            return src.scoring
    return NO_SCORING


def _make_entry(config: Config, source: str, path: str, mtime: float, size: int,
                content: str, agent_mtime: float | None,
                agent_editor: str | None = None) -> FileEntry:
    # 切块区间取自原文；检索词取自同一区间——开启 strip_wikilinks 时取自遮罩文本。
    pieces = split_text(content, size=config.chunk_size, step=config.chunk_step)
    if _source_scoring(config, source).strip_wikilinks:
        masked = mask_wikilinks(content)
        pieces = [(start, end, masked[start:end]) for start, end, _ in pieces]
    intern = sys.intern  # 同一个词在十几万个块里反复出现，驻留后只占一份内存
    return FileEntry(
        source=source,
        path=path,
        mtime=mtime,
        size=size,
        char_count=len(content),
        spans=tuple((start, end) for start, end, _ in pieces),
        tokens=tuple(tuple(intern(tok) for tok in tokenize(piece, config.domain_terms))
                     for _, _, piece in pieces),
        path_tokens=tuple(intern(tok) for tok in tokenize_path(f"{source}/{path}",
                                                               config.domain_terms)),
        agent_mtime=agent_mtime,
        agent_editor=agent_editor,
    )


def _with_agent_mark(entry: FileEntry, key: DocKey,
                     marks: dict[DocKey, tuple[float, str | None]]) -> FileEntry:
    """把待并入的 agent 标记替换进沿用的旧条目。

    掉盘、扫描失败、单文件读取失败时，条目被原样沿用；若不在此处并入标记，
    调用方会把标记当成已消费而清除，盘恢复后 editor 就会误标为 scan。
    版本 (mtime, size) 不变，不影响增量复用与 BM25 签名。
    """
    mark = marks.get(key)
    if mark is None:
        return entry
    agent_mtime, agent_editor = mark
    if (agent_mtime, agent_editor) == (entry.agent_mtime, entry.agent_editor):
        return entry
    return dataclasses.replace(entry, agent_mtime=agent_mtime, agent_editor=agent_editor)


def refresh(
    config: Config,
    previous: dict[DocKey, FileEntry],
    *,
    full: bool = False,
    agent_marks: "dict[DocKey, tuple[float, str | None]] | None" = None,
    cache: ContentCache | None = None,
) -> tuple[dict[DocKey, FileEntry], dict[str, Availability]]:
    """按文件增量更新，返回 (新条目, 各 source 的可用性)。

    - 可用的 source：扫描；(mtime, size) 未变的文件复用旧条目，其余重读、重分词；
      消失的文件移除。full=True 时一律重读（agent 标记照样沿用）。
    - 掉盘（盘根访问不到，或扫描途中出错且盘根访问不到）：该 source 的旧条目原样沿用，
      待并入的 agent 标记替换进沿用条目（下同）——否则标记会被当成已消费而丢掉。
    - 目录不存在（盘在）：按删除处理，该 source 的条目全部移除。
    - 不在配置里的 source：条目移除。
    - 新读进来的全文 offer 给 cache：只填空位、不挤人，不改变 LRU 顺序（docs/adr/0026）。
    """
    marks = agent_marks or {}
    extensions = set(config.extensions)
    entries: dict[DocKey, FileEntry] = {}
    availability: dict[str, Availability] = {}

    for source in config.sources:
        storage = open_storage(source)
        old = {k: e for k, e in previous.items() if k[0] == source.name}
        state = storage.probe()
        stats = None
        if state.available:
            try:
                stats = list(storage.iter_files(extensions))
            except OSError as exc:
                logger.warning("扫描 source %s 出错：%s", source.name, exc)
            # 扫描完再探一次盘根：盘可能在扫描途中掉了，那样的扫描结果不可信。
            after = storage.probe()
            if not after.available:
                state = after
            elif stats is None:
                logger.error("source %s 扫描失败但盘根可访问，本轮保留旧条目", source.name)
                entries.update({k: _with_agent_mark(e, k, marks) for k, e in old.items()})
                availability[source.name] = state
                continue

        if not state.available:
            availability[source.name] = state
            if state.reason == DISK_OFFLINE:
                logger.warning("source %s 掉盘（%s 不可访问），保留 %d 个旧条目，不更新不删除",
                               source.name, source.dir.anchor, len(old))
                entries.update({k: _with_agent_mark(e, k, marks) for k, e in old.items()})
            else:
                logger.warning("source %s 的目录不存在：%s，按删除处理（移除 %d 个条目）",
                               source.name, source.dir, len(old))
            continue

        availability[source.name] = state
        for stat in stats:
            key = (source.name, stat.path)
            prior = old.get(key)
            mark = marks.get(key)
            if mark is not None:
                agent_mtime, agent_editor = mark
            elif prior is not None:
                agent_mtime, agent_editor = prior.agent_mtime, prior.agent_editor
            else:
                agent_mtime, agent_editor = None, None
            if not full and prior is not None and prior.version == (stat.mtime, stat.size):
                if (prior.agent_mtime, prior.agent_editor) == (agent_mtime, agent_editor):
                    entry = prior
                else:
                    entry = dataclasses.replace(prior, agent_mtime=agent_mtime,
                                                agent_editor=agent_editor)
            else:
                try:
                    content = storage.read_text(stat.path)
                except OSError as exc:
                    logger.warning("跳过无法读取的文件 %s/%s: %s", source.name, stat.path, exc)
                    if prior is not None:
                        entries[key] = _with_agent_mark(prior, key, marks)
                    continue
                entry = _make_entry(config, source.name, stat.path, stat.mtime, stat.size,
                                    content, agent_mtime, agent_editor)
                if cache is not None:
                    cache.offer(key, entry.version, content)
            entries[key] = entry

    return entries, availability


def open_storages(config: Config) -> dict[str, object]:
    return {src.name: open_storage(src) for src in config.sources}


def build(config: Config, cache: ContentCache | None = None,
          rescue: dict | None = None) -> IndexSnapshot:
    """不借助 index.cache 的全量构建。用于自检、无缓存启动与测试。

    rescue：指纹不符的旧缓存载荷（见 read_cache）。给了就从中沿用 agent 标记，
    并抢救掉盘 source 的条目与全文（docs/adr/0026）。
    """
    started = time.perf_counter()
    cache = ContentCache(config.max_cached_docs) if cache is None else cache
    previous = _rescue_previous(config, rescue, cache) if rescue is not None else {}
    entries, availability = refresh(config, previous, full=True, cache=cache)
    cache.retain(entries)
    snapshot = IndexSnapshot(entries=entries, availability=availability, cache=cache,
                             storages=open_storages(config), scoring=scoring_of(config),
                             build_seconds=time.perf_counter() - started)
    if not snapshot.chunks:
        logger.warning("语料为空，索引不含任何 chunk")
    logger.info("索引构建完成：%d 文档 / %d chunk / %.2fs",
                snapshot.doc_count, snapshot.chunk_count, snapshot.build_seconds)
    return snapshot


def _rescue_previous(config: Config, payload: dict, cache: ContentCache) -> dict[DocKey, FileEntry]:
    """从指纹不符的旧缓存里挑出还能用的旧条目，作为全量 refresh 的 previous。

    - 在线 source：旧条目只用来沿用 agent_mtime——full 一律重读、重分词；
    - 掉盘 source：refresh 会原样沿用，所以先在这里处理好：
      旧缓存有全文（版本一致）的按新配置重新切块分词，全文按旧的使用顺序放回 cache；
      没有全文的沿用旧分词（能搜到，片段为空），盘恢复后等被动更新；
      扩展名已不在当前配置里的丢弃。
    """
    names = {src.name for src in config.sources}
    offline = {src.name for src in config.sources
               if open_storage(src).probe().reason == DISK_OFFLINE}
    extensions = set(config.extensions)
    contents = payload.get("contents") or []
    texts = {key: (version, text) for key, version, text in contents}

    previous: dict[DocKey, FileEntry] = {}
    retokenized = kept = 0
    for entry in payload["entries"]:
        if entry.source not in names:
            continue
        if entry.source in offline:
            if PurePosixPath(entry.path).suffix.lower() not in extensions:
                continue
            cached = texts.get(entry.key)
            if cached is not None and cached[0] == entry.version:
                entry = _make_entry(config, entry.source, entry.path, entry.mtime, entry.size,
                                    cached[1], entry.agent_mtime)
                retokenized += 1
            else:
                kept += 1
        previous[entry.key] = entry

    cache.load(item for item in contents
               if item[0][0] in offline and item[0] in previous
               and previous[item[0]].version == item[1])
    if offline:
        logger.warning("从旧缓存抢救掉盘 source %s：%d 篇按新配置重新分词，%d 篇沿用旧分词（无全文）",
                       "、".join(sorted(offline)), retokenized, kept)
    return previous


# --- 索引缓存 -----------------------------------------------------------------

def cache_fingerprint(config: Config) -> str:
    """配置指纹：影响切块或分词的任何东西变了，缓存就作废。

    不含代码版本号：升版本不作废缓存（docs/adr/0026）。改了切块或分词逻辑而配置没变时，
    必须手动递增 CACHE_FORMAT。
    """
    basis = {
        "format": CACHE_FORMAT,
        "chunk_size": config.chunk_size,
        "chunk_overlap": config.chunk_overlap,
        "extensions": sorted(config.extensions),
        "domain_terms": sorted(config.domain_terms),
        "jieba": getattr(jieba, "__version__", ""),
        # strip_wikilinks 改变检索词。只列与全局不同的 source：新增一个用默认值的 source
        # 不作废缓存。查询时的加分（recency_*、path_match_bonus）不进指纹。
        "strip_wikilinks": {
            "default": config.scoring.strip_wikilinks,
            "overrides": {src.name: src.scoring.strip_wikilinks for src in sorted(
                config.sources, key=lambda s: s.name)
                if src.scoring.strip_wikilinks != config.scoring.strip_wikilinks},
        },
    }
    return hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()


def save_cache(config: Config, snapshot: IndexSnapshot) -> None:
    """原子写入：先写临时文件再替换，崩溃时不会留下半截缓存。失败只告警。"""
    path = config.cache_file
    payload = {
        "format": CACHE_FORMAT,
        "fingerprint": cache_fingerprint(config),
        "built_at": snapshot.built_at.isoformat(),
        "entries": list(snapshot.entries.values()),
        "bm25": snapshot.bm25,
        # 只持久化当前缓存着的全文（≤ max_cached_docs 篇），按最近使用从旧到新。
        "contents": snapshot.cache.dump(),
    }
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        with open(tmp, "wb") as f:
            pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("索引缓存写入失败 %s：%s", path, exc)


def read_cache(config: Config) -> tuple[dict | None, bool]:
    """读取缓存载荷，返回 (载荷, 指纹是否一致)。

    缺失、损坏、格式不符 → (None, False)：结构不可信，什么都不能用。
    指纹不符 → (载荷, False)：结构可信，只是切块或分词可能过时，可供 build 抢救。
    """
    path = config.cache_file
    if not path.exists():
        logger.info("没有索引缓存（%s），将全量构建", path)
        return None, False
    try:
        with open(path, "rb") as f:
            payload = pickle.load(f)
        if payload.get("format") != CACHE_FORMAT:
            logger.warning("索引缓存格式版本不符，将全量构建")
            return None, False
        payload["entries"]  # noqa: B018  结构检查：缺字段按损坏处理
    except Exception as exc:  # noqa: BLE001  损坏的 pickle 可能抛出任意异常
        logger.warning("索引缓存无法读取或已损坏（%s），将全量构建", exc)
        return None, False
    if payload.get("fingerprint") != cache_fingerprint(config):
        logger.warning("索引缓存的配置指纹不符（切块、扩展名或词典变了），将全量构建并抢救掉盘 source")
        return payload, False
    return payload, True


def load_cache(config: Config, cache: ContentCache | None = None) -> IndexSnapshot | None:
    """读取缓存并直接构成快照。缺失、损坏、格式或指纹不符时返回 None 并告警。"""
    started = time.perf_counter()
    payload, fresh = read_cache(config)
    if not fresh:
        return None
    return _snapshot_from_cache(config, payload, cache, started)


def _snapshot_from_cache(config: Config, payload: dict, cache: ContentCache | None,
                         started: float) -> IndexSnapshot:
    """配置里已删除的 source 的条目在这里丢弃；可用性只做一次廉价的盘根/目录探测，
    真正的内容校验交给随后的后台增量更新。
    """
    stored: list[FileEntry] = payload["entries"]
    bm25 = payload.get("bm25")
    cached_contents = payload.get("contents") or []
    names = {ws.name for ws in config.sources}
    entries = {e.key: e for e in stored if e.source in names}
    if len(entries) != len(stored):
        logger.info("索引缓存：丢弃 %d 个已移除 source 的条目", len(stored) - len(entries))
        bm25 = None  # 条目集合变了，BM25 统计必须重建
    cache = ContentCache(config.max_cached_docs) if cache is None else cache
    cache.load(item for item in cached_contents
               if item[0] in entries and entries[item[0]].version == item[1])
    availability = {ws.name: open_storage(ws).probe() for ws in config.sources}
    snapshot = IndexSnapshot(entries=entries, availability=availability, bm25=bm25,
                             cache=cache, storages=open_storages(config),
                             scoring=scoring_of(config),
                             build_seconds=time.perf_counter() - started)
    logger.info("已从缓存加载索引：%d 文档 / %d chunk / %.2fs",
                snapshot.doc_count, snapshot.chunk_count, snapshot.build_seconds)
    return snapshot


# --- 持有者：快照、后台刷新、轮询 ------------------------------------------------

class IndexHolder:
    """持有当前快照，负责启动加载、后台增量刷新与轮询。

    所有重建（启动校验、轮询、save、reindex）都走同一条 request_rebuild 路径：
    同一时刻只有一个刷新线程，并发请求合并；缓存只在这条线程里写。
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._snapshot: IndexSnapshot | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._rebuild_lock = threading.Lock()
        self._rebuilding = False
        self._rebuild_pending = False
        self._pending_full = False
        self._verifying = False
        # save 写入后、下一轮刷新前的 agent 标记。刷新时并入条目。
        # 值 = (落盘 mtime, 编辑者路由身份)，见 docs/adr/0031。
        self._agent_marks: dict[DocKey, tuple[float, str | None]] = {}
        # 常驻内存的全文缓存（LRU，容量 max_cached_docs），所有快照共用这一份。
        self._cache = ContentCache(config.max_cached_docs)
        # 构建与刷新的输入是 effective sources（公共 + 当场枚举的个人 source，
        # ADR-0029）：每次用到都重新枚举，新用户目录的出现/消失/改名即时生效。
        self._storages = open_storages(self.effective_config())

    def effective_config(self) -> Config:
        """把基座 config 的 sources 替换为当场枚举的 effective sources。

        其余字段（config_file、缓存路径、切块参数）原样——缓存身份不受
        用户目录增删影响（cache_fingerprint 不含动态 source 名集）。
        """
        return dataclasses.replace(self._config, sources=effective_sources(self._config))

    @property
    def snapshot(self) -> IndexSnapshot:
        """当前快照。读取一次即在整个请求内持有，无需加锁。"""
        snapshot = self._snapshot
        if snapshot is None:
            raise RuntimeError("索引尚未构建；服务不应在索引就绪之前开始接受请求")
        return snapshot

    @property
    def rebuilding(self) -> bool:
        return self._rebuilding

    @property
    def verifying(self) -> bool:
        """启动时用了缓存、后台校验还没完成。此刻的内容可能是上次关闭时的样子。"""
        return self._verifying

    def build_now(self, rescue: dict | None = None) -> IndexSnapshot:
        """阻塞式全量构建并写缓存。rescue：指纹不符的旧缓存载荷，见 build。"""
        effective = self.effective_config()
        self._snapshot = build(effective, self._cache, rescue=rescue)
        self._storages = open_storages(effective)
        save_cache(effective, self._snapshot)
        return self._snapshot

    def start(self) -> IndexSnapshot:
        """启动：有可用缓存就立即用它服务并在后台增量校验；否则阻塞全量构建。

        指纹不符时全量构建，但从旧缓存沿用 agent 标记、抢救掉盘 source（docs/adr/0026）。
        必须在开始监听端口之前调用，以避免出现"服务已启动但索引未就绪"的窗口。
        缓存读取用 effective config：个人 source 的条目不能被基座 sources 过滤掉，
        否则重启后、后台校验完成前个人记忆会"暂时搜不到"。
        """
        started = time.perf_counter()
        effective = self.effective_config()
        payload, fresh = read_cache(effective)
        if not fresh:
            return self.build_now(rescue=payload)
        cached = _snapshot_from_cache(effective, payload, self._cache, started)
        self._snapshot = cached
        self._verifying = True
        self.request_rebuild("启动后台校验")
        return cached

    def update_availability(self, name: str, state: Availability) -> bool:
        """把快照里某个 source 的可用性替换为新探测的结果（写入失败后的被动探测用）。

        只换可用性：条目、块列表与 BM25 原样复用，因此是即时的；内容的对齐交给随后的刷新。
        返回 True 表示状态确实变了。
        """
        current = self._snapshot
        if current is None or current.availability_of(name) == state:
            return False
        self._snapshot = IndexSnapshot(
            entries=current.entries,
            availability={**current.availability, name: state},
            cache=self._cache,
            storages=self._storages,
            build_seconds=current.build_seconds,
            reuse=current,
            scoring=current.scoring,
        )
        logger.warning("写入失败后探测到 source %s 的可用性变化：%s", name,
                       state.reason or "恢复可用")
        return True

    def mark_agent(self, source: str, path: str, mtime: float,
                   editor: str | None = None) -> None:
        """save 等写入成功后调用：记下落盘 mtime 与编辑者，下一轮刷新时写进该文件的条目。"""
        with self._rebuild_lock:
            self._agent_marks[(source, path)] = (mtime, editor)

    def request_rebuild(self, reason: str, *, full: bool = False) -> bool:
        """请求一次后台刷新，**立即返回**。

        已有刷新在跑时只置 pending（full 请求会让下一轮升级为全量），
        由在跑的线程结束后再跑一轮。返回 True 表示本次启动了线程，False 表示已合并。
        """
        with self._rebuild_lock:
            if full:
                self._pending_full = True
            if self._rebuilding:
                self._rebuild_pending = True
                logger.info("索引刷新已在进行中，本次请求（%s）合并到下一轮", reason)
                return False
            self._rebuilding = True

        try:
            threading.Thread(target=self._rebuild_loop, args=(reason,),
                             name="myMemory-index-rebuild", daemon=True).start()
        except BaseException:
            # 线程起不来（极少见）：此刻 _rebuild_loop 的 try/finally 还没生效，
            # 标志若不复位，此后所有刷新都会被"已在进行中"挡掉且无迹可查。
            # 保留 _pending_full，下一次请求或轮询会带着它重试。
            with self._rebuild_lock:
                self._rebuilding = False
            logger.exception("索引刷新线程启动失败（%s），等待下一次请求或轮询重试", reason)
            return False
        return True

    def _rebuild_loop(self, reason: str) -> None:
        """刷新线程体。跑完若有 pending 就再跑一轮。

        try/finally 是必须的：标志若因意外异常卡在 True，此后所有刷新都会被
        "已在进行中"挡掉，且没有任何迹象可查。
        """
        try:
            while True:
                with self._rebuild_lock:
                    full = self._pending_full
                    self._pending_full = False
                    marks = dict(self._agent_marks)
                self._rebuild_once(reason, full=full, marks=marks)
                with self._rebuild_lock:
                    if not self._rebuild_pending:
                        self._rebuilding = False
                        return
                    self._rebuild_pending = False
                reason = "合并的后续请求"
        except BaseException:
            with self._rebuild_lock:
                self._rebuilding = False
                self._rebuild_pending = False
            raise

    def _rebuild_once(self, reason: str, *, full: bool, marks: dict[DocKey, float]) -> None:
        current = self._snapshot
        previous = current.entries if current is not None else {}
        logger.info("开始%s刷新索引（%s）", "全量" if full else "增量", reason)
        started = time.perf_counter()
        effective = self.effective_config()
        try:
            entries, availability = refresh(effective, previous, full=full,
                                            agent_marks=marks, cache=self._cache)
        except Exception:
            # 保留旧快照，服务永不因刷新失败而不可用。
            logger.exception("索引刷新失败（%s），保留旧快照", reason)
            return
        finally:
            self._verifying = False

        with self._rebuild_lock:
            for key, mark in marks.items():
                if self._agent_marks.get(key) == mark:
                    del self._agent_marks[key]

        entries_changed = current is None or entries != current.entries
        availability_changed = current is None or availability != current.availability
        if not (full or entries_changed or availability_changed):
            logger.info("索引无变化（%.2fs，%s）", time.perf_counter() - started, reason)
            return
        # 原子替换：单条属性赋值。在途请求继续使用旧快照。
        self._cache.retain(entries)
        # 每轮重建 storages：动态个人 source 在上一轮可能还没有句柄。
        self._storages = open_storages(effective)
        snapshot = IndexSnapshot(entries=entries, availability=availability,
                                 cache=self._cache, storages=self._storages,
                                 scoring=scoring_of(effective),
                                 build_seconds=time.perf_counter() - started,
                                 reuse=None if full else current)
        self._snapshot = snapshot
        logger.info("索引已刷新：%d 文档 / %d chunk / %.2fs（%s）",
                    snapshot.doc_count, snapshot.chunk_count, snapshot.build_seconds, reason)
        if full or entries_changed:
            save_cache(effective, snapshot)

    def start_polling(self) -> None:
        if self._config.poll_interval <= 0:
            logger.info("poll_interval=0，已关闭轮询")
            return
        self._thread = threading.Thread(target=self._poll_loop, name="myMemory-index-poll",
                                        daemon=True)
        self._thread.start()
        logger.info("轮询线程已启动，间隔 %d 秒", self._config.poll_interval)

    def stop(self) -> None:
        self._stop.set()

    def _poll_loop(self) -> None:
        """每个周期请求一次增量刷新：捕获绕过本服务的改动，以及掉盘与恢复。"""
        while not self._stop.wait(self._config.poll_interval):
            self.request_rebuild("轮询")
