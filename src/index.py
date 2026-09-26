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

import jieba
from rank_bm25 import BM25Okapi

from version import __version__
from config import Config
from corpus import Chunk, DocKey, DocMeta, split_text
from storage import AVAILABLE, DISK_OFFLINE, Availability, open_storage

logger = logging.getLogger(__name__)

# 缓存格式版本。改动 FileEntry 或缓存结构时递增，旧缓存随之作废。
CACHE_FORMAT = 3  # 2：workspace 更名为 source；3：全文移出条目，改为 LRU 全文缓存

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
    edited_by = agent，否则为 scan（扫描发现改动；可能是人改的，也可能是别的
    设备/程序改的，不做推断。docs/adr/0018-edited-by-via-status-file.md，
    取代了 status.json）。
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

    @property
    def key(self) -> DocKey:
        return (self.source, self.path)

    @property
    def version(self) -> tuple[float, int]:
        return (self.mtime, self.size)

    @property
    def edited_by(self) -> str:
        return "agent" if self.agent_mtime is not None and self.agent_mtime == self.mtime else "scan"


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
        "storages", "indexed_paths", "built_at", "build_seconds", "signature",
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
    ) -> None:
        """reuse：内容签名相同的旧快照，直接沿用它的块列表与 BM25，只换条目与可用性。"""
        self.entries = entries
        self.availability = availability
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

    def search(self, query: str, limit: int,
               source: str | None = None) -> tuple[int, list[Hit]]:
        """BM25 检索。返回 (命中总数, 前 limit 条)。

        source 为 None 时跨全部 source（统一索引）；否则只保留该 source 的命中。

        命中判定用"chunk 中确实出现了至少一个查询词"，而不是"BM25 分数 > 0"。

        原因：rank_bm25 的 BM25Okapi 用
        idf = log(N - n + 0.5) - log(n + 0.5)，当一个词出现在超过半数
        chunk 中时 idf 为负；其 epsilon 兜底取的是 average_idf 的倍数，
        在平均 idf 本身为负时依然为负。此时真实命中的分数是负的，
        按 score > 0 过滤会把它们整个丢掉——查询词越常见，丢得越彻底。
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
        ]
        if not ranked:
            return 0, []
        ranked.sort(key=lambda i: scores[i], reverse=True)

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
                    score=float(scores[i]),
                    chunk_index=chunk.chunk_index,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    text=self._chunk_text(chunk),
                )
            )
        return len(ranked), hits


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

def _make_entry(config: Config, source: str, path: str, mtime: float, size: int,
                content: str, agent_mtime: float | None) -> FileEntry:
    pieces = split_text(content, size=config.chunk_size, step=config.chunk_step)
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
    )


def _with_agent_mark(entry: FileEntry, key: DocKey, marks: dict[DocKey, float]) -> FileEntry:
    """把待并入的 agent 标记替换进沿用的旧条目。

    掉盘、扫描失败、单文件读取失败时，条目被原样沿用；若不在此处并入标记，
    调用方会把标记当成已消费而清除，盘恢复后 edited_by 就会误标为 scan。
    版本 (mtime, size) 不变，不影响增量复用与 BM25 签名。
    """
    agent_mtime = marks.get(key, entry.agent_mtime)
    if agent_mtime == entry.agent_mtime:
        return entry
    return dataclasses.replace(entry, agent_mtime=agent_mtime)


def refresh(
    config: Config,
    previous: dict[DocKey, FileEntry],
    *,
    full: bool = False,
    agent_marks: dict[DocKey, float] | None = None,
    cache: ContentCache | None = None,
) -> tuple[dict[DocKey, FileEntry], dict[str, Availability]]:
    """按文件增量更新，返回 (新条目, 各 source 的可用性)。

    - 可用的 source：扫描；(mtime, size) 未变的文件复用旧条目，其余重读、重分词；
      消失的文件移除。full=True 时一律重读（agent 标记照样沿用）。
    - 掉盘（盘根访问不到，或扫描途中出错且盘根访问不到）：该 source 的旧条目原样沿用，
      待并入的 agent 标记替换进沿用条目（下同）——否则标记会被当成已消费而丢掉。
    - 目录不存在（盘在）：按删除处理，该 source 的条目全部移除。
    - 不在配置里的 source：条目移除。
    - 新读进来的全文放进 cache（容量受 max_cached_docs 限制，LRU）。
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
            agent_mtime = marks.get(key, prior.agent_mtime if prior else None)
            if not full and prior is not None and prior.version == (stat.mtime, stat.size):
                entry = prior if prior.agent_mtime == agent_mtime else \
                    dataclasses.replace(prior, agent_mtime=agent_mtime)
            else:
                try:
                    content = storage.read_text(stat.path)
                except OSError as exc:
                    logger.warning("跳过无法读取的文件 %s/%s: %s", source.name, stat.path, exc)
                    if prior is not None:
                        entries[key] = _with_agent_mark(prior, key, marks)
                    continue
                entry = _make_entry(config, source.name, stat.path, stat.mtime, stat.size,
                                    content, agent_mtime)
                if cache is not None:
                    cache.put(key, entry.version, content)
            entries[key] = entry

    return entries, availability


def open_storages(config: Config) -> dict[str, object]:
    return {src.name: open_storage(src) for src in config.sources}


def build(config: Config, cache: ContentCache | None = None) -> IndexSnapshot:
    """不借助 index.cache 的全量构建。用于自检、无缓存启动与测试。"""
    started = time.perf_counter()
    cache = ContentCache(config.max_cached_docs) if cache is None else cache
    entries, availability = refresh(config, {}, full=True, cache=cache)
    cache.retain(entries)
    snapshot = IndexSnapshot(entries=entries, availability=availability, cache=cache,
                             storages=open_storages(config),
                             build_seconds=time.perf_counter() - started)
    if not snapshot.chunks:
        logger.warning("语料为空，索引不含任何 chunk")
    logger.info("索引构建完成：%d 文档 / %d chunk / %.2fs",
                snapshot.doc_count, snapshot.chunk_count, snapshot.build_seconds)
    return snapshot


# --- 索引缓存 -----------------------------------------------------------------

def cache_fingerprint(config: Config) -> str:
    """配置指纹：影响切块或分词的任何东西变了，缓存就作废。"""
    basis = {
        "format": CACHE_FORMAT,
        "version": __version__,
        "chunk_size": config.chunk_size,
        "chunk_overlap": config.chunk_overlap,
        "extensions": sorted(config.extensions),
        "domain_terms": sorted(config.domain_terms),
        "jieba": getattr(jieba, "__version__", ""),
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


def load_cache(config: Config, cache: ContentCache | None = None) -> IndexSnapshot | None:
    """读取缓存并直接构成快照。缺失、损坏、格式或指纹不符时返回 None 并告警。

    配置里已删除的 source 的条目在这里丢弃；可用性只做一次廉价的盘根/目录探测，
    真正的内容校验交给随后的后台增量更新。
    """
    path = config.cache_file
    if not path.exists():
        logger.info("没有索引缓存（%s），将全量构建", path)
        return None
    started = time.perf_counter()
    try:
        with open(path, "rb") as f:
            payload = pickle.load(f)
        if payload.get("format") != CACHE_FORMAT:
            logger.warning("索引缓存格式版本不符，将全量构建")
            return None
        if payload.get("fingerprint") != cache_fingerprint(config):
            logger.warning("索引缓存的配置指纹不符（切块、扩展名、词典或版本变了），将全量构建")
            return None
        stored: list[FileEntry] = payload["entries"]
        bm25 = payload.get("bm25")
        cached_contents = payload.get("contents") or []
    except Exception as exc:  # noqa: BLE001  损坏的 pickle 可能抛出任意异常
        logger.warning("索引缓存无法读取或已损坏（%s），将全量构建", exc)
        return None

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
        self._agent_marks: dict[DocKey, float] = {}
        # 常驻内存的全文缓存（LRU，容量 max_cached_docs），所有快照共用这一份。
        self._cache = ContentCache(config.max_cached_docs)
        self._storages = open_storages(config)

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

    def build_now(self) -> IndexSnapshot:
        """阻塞式全量构建并写缓存。"""
        self._snapshot = build(self._config, self._cache)
        save_cache(self._config, self._snapshot)
        return self._snapshot

    def start(self) -> IndexSnapshot:
        """启动：有可用缓存就立即用它服务并在后台增量校验；否则阻塞全量构建。

        必须在开始监听端口之前调用，以避免出现"服务已启动但索引未就绪"的窗口。
        """
        cached = load_cache(self._config, self._cache)
        if cached is None:
            return self.build_now()
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
        )
        logger.warning("写入失败后探测到 source %s 的可用性变化：%s", name,
                       state.reason or "恢复可用")
        return True

    def mark_agent(self, source: str, path: str, mtime: float) -> None:
        """save 成功后调用：记下落盘 mtime，下一轮刷新时写进该文件的条目。"""
        with self._rebuild_lock:
            self._agent_marks[(source, path)] = mtime

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
        try:
            entries, availability = refresh(self._config, previous, full=full,
                                            agent_marks=marks, cache=self._cache)
        except Exception:
            # 保留旧快照，服务永不因刷新失败而不可用。
            logger.exception("索引刷新失败（%s），保留旧快照", reason)
            return
        finally:
            self._verifying = False

        with self._rebuild_lock:
            for key, mtime in marks.items():
                if self._agent_marks.get(key) == mtime:
                    del self._agent_marks[key]

        entries_changed = current is None or entries != current.entries
        availability_changed = current is None or availability != current.availability
        if not (full or entries_changed or availability_changed):
            logger.info("索引无变化（%.2fs，%s）", time.perf_counter() - started, reason)
            return
        # 原子替换：单条属性赋值。在途请求继续使用旧快照。
        self._cache.retain(entries)
        snapshot = IndexSnapshot(entries=entries, availability=availability,
                                 cache=self._cache, storages=self._storages,
                                 build_seconds=time.perf_counter() - started,
                                 reuse=None if full else current)
        self._snapshot = snapshot
        logger.info("索引已刷新：%d 文档 / %d chunk / %.2fs（%s）",
                    snapshot.doc_count, snapshot.chunk_count, snapshot.build_seconds, reason)
        if full or entries_changed:
            save_cache(self._config, snapshot)

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
