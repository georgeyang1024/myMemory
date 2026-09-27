"""index 层单测：分词、检索排序、多样性约束、快照不变量、轮询重建与失败保护。"""

import os
import sys
import time
from pathlib import Path

import pytest

import index, storage
from storage import DISK_OFFLINE, DIR_MISSING, Availability
from config import Config, Source

from test_corpus import make_config  # noqa: F401  复用配置构造


@pytest.fixture
def kb_root(tmp_path: Path) -> Path:
    raw = tmp_path / "memory"
    (raw / "Record_Docs" / "26-08-04-同步").mkdir(parents=True)
    (raw / "Facts_And_Status").mkdir(parents=True)

    # 正文中不含"规格汇总"四字，语义完全在文件名上——用于验证路径进索引
    (raw / "Facts_And_Status" / "规格汇总.md").write_text(
        "| 字段 | Field:x; | 单元以x档开始运转 |\n" * 40, encoding="utf-8"
    )
    (raw / "Record_Docs" / "26-08-04-同步" / "纪要.md").write_text(
        "记录了 SecProto 机制与防护措施的要点。" * 30, encoding="utf-8"
    )
    (raw / "Facts_And_Status" / "无关.md").write_text(
        "这是一篇完全无关的文档，只讲包装设计。" * 30, encoding="utf-8"
    )
    return tmp_path


def build_index(root: Path, **overrides) -> index.IndexSnapshot:
    return index.build(make_config(root, **overrides))


# --- 分词 -------------------------------------------------------------------

def test_tokenize_lowercases():
    assert "secproto" in index.tokenize("SecProto 机制")


def test_tokenize_path_splits_separators():
    tokens = index.tokenize_path("Record_Docs/26-08-04-同步/纪要.md")
    assert {"record", "docs", "同步", "纪要"} <= set(tokens)


def test_tokenize_keeps_date_atomic():
    """日期必须作为整体存在，且不发出 26/08/04 碎片。

    这三个碎片出现在几乎每一个日期目录里，IDF 被稀释到没有区分度，
    却仍会靠词频把无关文档顶上来。
    """
    for tokens in (
        index.tokenize("26-08-04 同步"),
        index.tokenize_path("Record_Docs/26-08-04-同步/纪要.md"),
    ):
        assert "26-08-04" in tokens
        assert "26" not in tokens and "08" not in tokens and "04" not in tokens


def test_long_alnum_token_expands_to_domain_terms():
    """完整型号必须能被系列名检索到（依赖配置的 domain_terms）。"""
    terms = ("AB123456",)
    tokens = index.tokenize("AB1234567890", terms=terms)
    assert "ab1234567890" in tokens
    assert "ab123456" in tokens


def test_short_alnum_token_not_expanded():
    """展开规则只作用于长标识符，不应波及普通短词。"""
    assert index.tokenize("gatt", terms=("GATT",)) == ["gatt"]


# --- 快照不变量 --------------------------------------------------------------

def test_snapshot_invariants(kb_root: Path):
    snapshot = build_index(kb_root)
    assert snapshot.doc_count == 3
    assert snapshot.chunk_count > 0
    assert snapshot.indexed_paths == frozenset(snapshot.documents)
    assert snapshot.indexed_paths == frozenset(snapshot.contents)
    assert isinstance(snapshot.indexed_paths, frozenset), "安全边界必须不可变"


def test_snapshot_describe_fields(kb_root: Path):
    described = build_index(kb_root).describe()
    assert set(described) == {"built_at", "build_seconds", "doc_count", "chunk_count"}


# --- 检索 -------------------------------------------------------------------

def test_search_matches_filename_semantics(kb_root: Path):
    """文件名承载的语义必须可检索——正文里没有这四个字。"""
    _, hits = build_index(kb_root).search("规格汇总", 3)
    assert hits and hits[0].path.endswith("规格汇总.md")


def test_search_matches_directory_date(kb_root: Path):
    _, hits = build_index(kb_root).search("26-08-04 同步", 3)
    assert hits and "26-08-04" in hits[0].path


def test_search_scores_descending(kb_root: Path):
    _, hits = build_index(kb_root).search("SecProto 机制", 5)
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)


def test_search_excludes_zero_score(kb_root: Path):
    total, hits = build_index(kb_root).search("zzzqqq xxyyzz", 5)
    assert total == 0 and hits == []


def test_search_empty_query_returns_nothing(kb_root: Path):
    assert build_index(kb_root).search("   ", 5) == (0, [])


def test_search_respects_limit(kb_root: Path):
    _, hits = build_index(kb_root).search("SecProto", 1)
    assert len(hits) == 1


def test_search_diversity_cap_per_document(tmp_path: Path):
    """同一文档最多先占 MAX_CHUNKS_PER_DOC 个位置，其他文档才有机会进入结果。"""
    raw = tmp_path / "memory"
    raw.mkdir(parents=True)
    # 大文件：多个 chunk 全部高度相关
    (raw / "大文档.md").write_text("SecProto 机制 攻击 防护 措施。" * 400, encoding="utf-8")
    # 小文件：只有一个 chunk，同样相关
    (raw / "小文档.md").write_text("SecProto 机制 攻击 防护 措施。", encoding="utf-8")

    snapshot = build_index(tmp_path)
    assert len([c for c in snapshot.chunks if c.path == "大文档.md"]) > 2

    _, hits = snapshot.search("SecProto 机制 攻击", 3)
    counts: dict[str, int] = {}
    for hit in hits[:index.MAX_CHUNKS_PER_DOC + 1]:
        counts[hit.path] = counts.get(hit.path, 0) + 1
    assert counts.get("大文档.md", 0) <= index.MAX_CHUNKS_PER_DOC
    assert "小文档.md" in {h.path for h in hits}, "小文档不应被大文档的相邻块挤出"


def test_hit_offsets_map_back_to_content(kb_root: Path):
    snapshot = build_index(kb_root)
    _, hits = snapshot.search("SecProto", 3)
    for hit in hits:
        source = snapshot.contents[(hit.source, hit.path)]
        assert source[hit.char_start : hit.char_end] == hit.text


def test_empty_corpus_does_not_crash(tmp_path: Path):
    (tmp_path / "memory").mkdir()
    snapshot = build_index(tmp_path)
    assert snapshot.doc_count == 0
    assert snapshot.search("任意", 5) == (0, [])


# --- IndexHolder 轮询 --------------------------------------------------------

def test_holder_requires_build_before_use(kb_root: Path):
    holder = index.IndexHolder(make_config(kb_root))
    with pytest.raises(RuntimeError):
        _ = holder.snapshot


def test_holder_rebuilds_on_change(kb_root: Path):
    holder = index.IndexHolder(make_config(kb_root, MEMORY_POLL_INTERVAL="1"))
    first = holder.build_now()
    holder.start_polling()
    try:
        (kb_root / "memory" / "新增文档.md").write_text("新增内容 SecProto", encoding="utf-8")
        deadline = time.time() + 15
        while time.time() < deadline and holder.snapshot is first:
            time.sleep(0.2)
        assert holder.snapshot is not first, "轮询未在预期时间内重建索引"
        assert holder.snapshot.doc_count == first.doc_count + 1
    finally:
        holder.stop()


def test_holder_keeps_old_snapshot_when_rebuild_fails(kb_root: Path, monkeypatch):
    """重建失败必须保留旧快照——服务不能因为一次构建异常而不可用。"""
    holder = index.IndexHolder(make_config(kb_root, MEMORY_POLL_INTERVAL="1"))
    first = holder.build_now()

    def boom(*_args, **_kwargs):
        raise RuntimeError("模拟构建失败")

    monkeypatch.setattr(index, "refresh", boom)
    holder.start_polling()
    try:
        (kb_root / "memory" / "触发重建.md").write_text("x", encoding="utf-8")
        time.sleep(4)
        assert holder.snapshot is first, "构建失败后旧快照必须仍在服务"
    finally:
        holder.stop()


def test_polling_disabled_when_interval_zero(kb_root: Path):
    holder = index.IndexHolder(make_config(kb_root, MEMORY_POLL_INTERVAL="0"))
    holder.build_now()
    holder.start_polling()
    assert holder._thread is None


# --- 多 source -----------------------------------------------------------

@pytest.fixture
def multi_root(tmp_path: Path) -> Path:
    for name in ("memory", "team", "org"):
        (tmp_path / name).mkdir()
    (tmp_path / "memory" / "机制.md").write_text("个人记录：SecProto 机制心得。", encoding="utf-8")
    (tmp_path / "team" / "机制.md").write_text("团队规范：SecProto 机制流程。", encoding="utf-8")
    (tmp_path / "org" / "制度.md").write_text("报销制度说明。", encoding="utf-8")
    return tmp_path


def multi_config(root: Path, **overrides):
    return make_config(root, sources=[
        {"name": "memory", "dir": str(root / "memory")},
        {"name": "team", "dir": str(root / "team")},
        {"name": "org", "dir": str(root / "org"), "writable": False},
    ], **overrides)


def test_indexed_set_is_source_path_pairs(multi_root: Path):
    snapshot = index.build(multi_config(multi_root))
    assert ("memory", "机制.md") in snapshot.indexed_paths
    assert ("team", "机制.md") in snapshot.indexed_paths
    assert "机制.md" not in snapshot.indexed_paths


def test_search_spans_all_sources_by_default(multi_root: Path):
    _, hits = index.build(multi_config(multi_root)).search("SecProto 机制", 5)
    assert {h.source for h in hits} == {"memory", "team"}


def test_search_can_be_scoped_to_one_source(multi_root: Path):
    total, hits = index.build(multi_config(multi_root)).search("SecProto 机制", 5, source="team")
    assert total == 1 and [h.source for h in hits] == ["team"]


def test_source_name_is_searchable(multi_root: Path):
    """source 名参与分词：直接搜 org 就能摸到只读库里有什么。"""
    _, hits = index.build(multi_config(multi_root)).search("org", 5)
    assert hits and hits[0].source == "org"


# --- 增量更新 ---------------------------------------------------------------

def _refresh(config, previous, **kw):
    entries, availability = index.refresh(config, previous, **kw)
    return entries, availability


def test_incremental_reuses_unchanged_entries(kb_root: Path):
    config = make_config(kb_root)
    first, _ = _refresh(config, {})
    second, _ = _refresh(config, first)
    assert all(second[k] is first[k] for k in first), "未变化的文件必须原样复用条目（不重读、不重分词）"


def test_incremental_picks_up_changes_and_deletions(kb_root: Path):
    config = make_config(kb_root)
    first, _ = _refresh(config, {})
    target = kb_root / "memory" / "Facts_And_Status" / "无关.md"
    target.write_text("改过了：SecProto", encoding="utf-8")
    os.utime(target, (time.time() + 5, time.time() + 5))
    (kb_root / "memory" / "新.md").write_text("新文件", encoding="utf-8")
    gone = next(k for k in first if k[1].endswith("纪要.md"))
    (kb_root / "memory" / gone[1]).unlink()

    cache = index.ContentCache(0)
    second, _ = _refresh(config, first, cache=cache)
    changed = ("memory", "Facts_And_Status/无关.md")
    assert second[changed] is not first[changed]
    assert "改过了" in cache.get(changed, second[changed].version), "重读的全文进了全文缓存"
    assert ("memory", "新.md") in second
    assert gone not in second, "磁盘上删除的文件必须移出索引"


def test_full_refresh_rereads_but_keeps_agent_marks(kb_root: Path):
    config = make_config(kb_root)
    first, _ = _refresh(config, {})
    key = next(iter(first))
    marked, _ = _refresh(config, first, agent_marks={key: first[key].mtime})
    assert marked[key].edited_by == "agent"
    again, _ = _refresh(config, marked, full=True)
    assert again[key] is not marked[key], "全量必须重读"
    assert again[key].edited_by == "agent", "全量重建不能丢 agent 标记"


def test_offline_refresh_still_merges_agent_marks(multi_root: Path, monkeypatch):
    """掉盘时沿用旧条目也要并入 agent 标记。

    标记若被当成已消费而清除，盘恢复后该文件的 edited_by 会从 agent 误标为 scan。
    """
    config = multi_config(multi_root)
    first, _ = _refresh(config, {})
    key = ("team", "机制.md")
    _offline(monkeypatch, "team")
    marked, availability = _refresh(config, first, agent_marks={key: first[key].mtime})
    assert availability["team"] == Availability(False, DISK_OFFLINE)
    assert marked[key].edited_by == "agent", "掉盘沿用条目必须并入标记，维持 edited_by"

    # 标记已被消费清除，盘恢复后（文件未变）edited_by 仍是 agent
    monkeypatch.undo()
    back, _ = _refresh(config, marked)
    assert back[key] is marked[key]
    assert back[key].edited_by == "agent"


def test_read_failure_still_merges_agent_marks(kb_root: Path, monkeypatch):
    """单文件读取失败沿用旧条目时，同样要并入 agent 标记。"""
    config = make_config(kb_root)
    first, _ = _refresh(config, {})
    key = ("memory", "Facts_And_Status/无关.md")
    real_read = storage.LocalStorage.read_text

    def read_text(self, path):
        if path == key[1]:
            raise OSError("模拟读取失败")
        return real_read(self, path)

    monkeypatch.setattr(storage.LocalStorage, "read_text", read_text)
    marked, _ = _refresh(config, first, agent_marks={key: first[key].mtime})
    assert marked[key] is not first[key], "沿用的条目应换上标记后的新对象"
    assert marked[key].edited_by == "agent"


# --- 可用性：掉盘与目录删除 ---------------------------------------------------

def _offline(monkeypatch, name: str):
    real = storage.LocalStorage.probe

    def probe(self):
        if self.source.name == name:
            return Availability(False, DISK_OFFLINE)
        return real(self)

    monkeypatch.setattr(storage.LocalStorage, "probe", probe)


def test_disk_offline_keeps_entries_untouched(multi_root: Path, monkeypatch):
    config = multi_config(multi_root)
    first, _ = _refresh(config, {})
    team_keys = {k for k in first if k[0] == "team"}
    _offline(monkeypatch, "team")
    (multi_root / "memory" / "新.md").write_text("其他 source 照常更新", encoding="utf-8")

    second, availability = _refresh(config, first)
    assert availability["team"] == Availability(False, DISK_OFFLINE)
    assert {k for k in second if k[0] == "team"} == team_keys, "掉盘不删除"
    assert all(second[k] is first[k] for k in team_keys), "掉盘不更新"
    assert ("memory", "新.md") in second, "其他 source 照常更新"

    again, _ = _refresh(config, second, full=True)
    assert all(again[k] is first[k] for k in team_keys), "全量重建同样不动掉盘的 source"


def test_disk_dropping_mid_scan_is_treated_as_offline(multi_root: Path, monkeypatch):
    config = multi_config(multi_root)
    first, _ = _refresh(config, {})
    state = {"calls": 0}
    real_probe = storage.LocalStorage.probe

    def probe(self):
        if self.source.name != "team":
            return real_probe(self)
        state["calls"] += 1
        return real_probe(self) if state["calls"] == 1 else Availability(False, DISK_OFFLINE)

    def iter_files(self, extensions):
        if self.source.name == "team":
            raise OSError("网络名不再可用")
        yield from real_iter(self, extensions)

    real_iter = storage.LocalStorage.iter_files
    monkeypatch.setattr(storage.LocalStorage, "probe", probe)
    monkeypatch.setattr(storage.LocalStorage, "iter_files", iter_files)

    second, availability = _refresh(config, first)
    assert availability["team"].reason == DISK_OFFLINE
    assert {k for k in second if k[0] == "team"} == {k for k in first if k[0] == "team"}


def test_disk_recovers_on_next_refresh(multi_root: Path, monkeypatch):
    config = multi_config(multi_root)
    first, _ = _refresh(config, {})
    _offline(monkeypatch, "team")
    offline, _ = _refresh(config, first)
    monkeypatch.undo()
    (multi_root / "team" / "恢复后新增.md").write_text("盘回来了", encoding="utf-8")
    back, availability = _refresh(config, offline)
    assert availability["team"].available
    assert ("team", "恢复后新增.md") in back


def test_deleted_source_directory_is_purged(multi_root: Path):
    import shutil

    config = multi_config(multi_root)
    first, _ = _refresh(config, {})
    shutil.rmtree(multi_root / "team")
    second, availability = _refresh(config, first)
    assert availability["team"] == Availability(False, DIR_MISSING)
    assert not [k for k in second if k[0] == "team"], "目录被删除：条目一并清除"


def test_source_removed_from_config_is_dropped(multi_root: Path):
    first, _ = _refresh(multi_config(multi_root), {})
    config = make_config(multi_root, sources=[{"name": "memory", "dir": str(multi_root / "memory")}])
    second, _ = _refresh(config, first)
    assert {k[0] for k in second} == {"memory"}


def test_probe_distinguishes_disk_offline_from_dir_missing(tmp_path: Path, monkeypatch):
    if sys.platform.startswith("linux"):
        _probe_offline_linux(tmp_path, monkeypatch)
    else:
        _probe_offline_stat(tmp_path, monkeypatch)


def _probe_offline_linux(tmp_path: Path, monkeypatch):
    """Linux：probe 走 _probe_linux 的 ls 子进程判定，不读 os.path.exists，
    模拟掉盘要让 _reachable（ls）失败。"""
    # 目录被删而父目录可达：DIR_MISSING，不误判成掉盘
    missing = storage.LocalStorage(Source(name="x", dir=tmp_path / "gone"))
    monkeypatch.setattr(
        storage.LocalStorage, "_reachable",
        lambda self, path: path == missing.root.parent,
    )
    assert missing.probe() == Availability(False, DIR_MISSING), "父可达、子不可达：目录被删除"

    # 挂载点与父目录都不可达（新起探测失败 / 上轮探测未退出同走此路）：DISK_OFFLINE
    monkeypatch.setattr(storage.LocalStorage, "_reachable", lambda self, path: False)
    offline = storage.LocalStorage(Source(name="x", dir=tmp_path))
    assert offline.probe() == Availability(False, DISK_OFFLINE), "目录不可达：掉盘"


def _probe_offline_stat(tmp_path: Path, monkeypatch):
    """Windows 等：probe 走 _probe_stat 的盘根判定，把 os.path.exists 打成 False 即掉盘。"""
    missing = storage.LocalStorage(Source(name="x", dir=tmp_path / "gone"))
    assert missing.probe() == Availability(False, DIR_MISSING), "盘根在、目录不在：目录被删除"

    monkeypatch.setattr(storage.os.path, "exists", lambda p: False)
    offline = storage.LocalStorage(Source(name="x", dir=tmp_path))
    assert offline.probe() == Availability(False, DISK_OFFLINE), "盘根访问不到：掉盘"


# --- 索引缓存 ---------------------------------------------------------------

def test_cache_roundtrip_serves_without_rebuilding(kb_root: Path, monkeypatch):
    config = make_config(kb_root)
    built = index.IndexHolder(config).build_now()
    assert config.cache_file.exists()

    monkeypatch.setattr(index, "tokenize", lambda *a: pytest.fail("从缓存加载不应重新分词"))
    loaded = index.load_cache(config)
    monkeypatch.undo()  # 检索本身要给查询词分词
    assert loaded is not None
    assert loaded.indexed_paths == built.indexed_paths
    assert loaded.search("规格汇总", 3)[1][0].path == built.search("规格汇总", 3)[1][0].path


@pytest.mark.parametrize("damage", ["garbage", "fingerprint", "format"])
def test_bad_cache_falls_back_to_full_build(kb_root: Path, damage):
    import pickle

    config = make_config(kb_root)
    index.IndexHolder(config).build_now()
    if damage == "garbage":
        config.cache_file.write_bytes(b"not a pickle")
    else:
        payload = pickle.loads(config.cache_file.read_bytes())
        payload[damage] = "changed"
        config.cache_file.write_bytes(pickle.dumps(payload))
    assert index.load_cache(config) is None

    holder = index.IndexHolder(config)
    snapshot = holder.start()
    assert snapshot.doc_count == 3 and not holder.verifying, "缓存不可用时全量构建，服务照常可用"


def test_cache_fingerprint_changes_with_chunking(kb_root: Path):
    assert index.cache_fingerprint(make_config(kb_root)) != \
        index.cache_fingerprint(make_config(kb_root, chunk_size=500))


def test_start_uses_cache_then_verifies_in_background(kb_root: Path):
    config = make_config(kb_root)
    index.IndexHolder(config).build_now()
    (kb_root / "memory" / "关机期间新增.md").write_text("SecProto 新内容", encoding="utf-8")

    holder = index.IndexHolder(config)
    first = holder.start()
    assert first.doc_count == 3, "先用缓存立即服务：还是上次关闭时的内容"
    deadline = time.time() + 30
    while time.time() < deadline and (holder.verifying or holder.rebuilding):
        time.sleep(0.05)
    assert not holder.verifying
    assert holder.snapshot.doc_count == 4, "后台校验后补上关机期间的改动"


def test_offline_source_survives_restart_via_cache(multi_root: Path, monkeypatch):
    """启动时盘不在：缓存里有它的条目就照常服务。"""
    config = multi_config(multi_root)
    index.IndexHolder(config).build_now()
    _offline(monkeypatch, "team")

    holder = index.IndexHolder(config)
    holder.start()
    deadline = time.time() + 30
    while time.time() < deadline and (holder.verifying or holder.rebuilding):
        time.sleep(0.05)
    snapshot = holder.snapshot
    assert snapshot.availability_of("team").reason == DISK_OFFLINE
    assert snapshot.search("SecProto 机制", 5, source="team")[0] == 1


def test_reindex_full_request_upgrades_pending_round(kb_root: Path, monkeypatch):
    config = make_config(kb_root)
    holder = index.IndexHolder(config)
    holder.build_now()
    seen = []
    real = index.refresh

    def spy(cfg, previous, *, full=False, **kw):
        seen.append(full)
        return real(cfg, previous, full=full, **kw)

    monkeypatch.setattr(index, "refresh", spy)
    holder.request_rebuild("测试", full=True)
    deadline = time.time() + 30
    while time.time() < deadline and holder.rebuilding:
        time.sleep(0.05)
    assert seen == [True]


def test_thread_spawn_failure_does_not_deadlock_refresh(kb_root: Path, monkeypatch):
    """刷新线程启动失败必须复位 _rebuilding，否则此后所有刷新被永久挡住且无迹可查。

    异常发生在 _rebuild_loop 的 try/finally 生效之前，那里救不了；
    修复后应保留 pending_full，并在下一次请求时重试成功。
    """
    config = make_config(kb_root)
    holder = index.IndexHolder(config)
    holder.build_now()

    real_thread = index.threading.Thread

    class FailingThread(real_thread):
        def start(self):
            raise RuntimeError("线程资源耗尽（模拟）")

    monkeypatch.setattr(index.threading, "Thread", FailingThread)
    assert holder.request_rebuild("第一次请求", full=True) is False
    assert not holder.rebuilding, "线程启动失败后标志必须复位"
    assert holder._pending_full, "full 请求应保留待重试"

    monkeypatch.undo()
    holder.request_rebuild("第二次请求")  # 无 full：靠保留的 pending_full 升级
    deadline = time.time() + 30
    while time.time() < deadline and holder.rebuilding:
        time.sleep(0.05)
    assert not holder.rebuilding, "重试应正常完成，标志不再卡死"


# --- 全文缓存上限：max_cached_docs（LRU）--------------------------------------

@pytest.fixture
def cache_root(tmp_path: Path) -> Path:
    mem = tmp_path / "memory"
    mem.mkdir()
    for name in ("甲.md", "乙.md", "丙.md"):
        (mem / name).write_text(f"SecProto {name} 的正文", encoding="utf-8")
    return tmp_path


def test_all_docs_indexed_but_only_n_contents_cached(cache_root: Path):
    """所有文档照常进索引、可检索；常驻内存的全文最多 max_cached_docs 篇。"""
    snapshot = index.build(make_config(cache_root, max_cached_docs=1))
    assert snapshot.doc_count == 3
    assert len(snapshot.cache) == 1
    assert snapshot.search("SecProto", 5)[0] == 3


def test_uncached_content_is_read_from_disk_and_cached_lru(cache_root: Path):
    snapshot = index.build(make_config(cache_root, max_cached_docs=2))
    missing = next(k for k in snapshot.entries if snapshot.cache.get(k, snapshot.entries[k].version) is None)
    assert "的正文" in snapshot.contents[missing], "不在缓存里就读盘"
    assert snapshot.cache.get(missing, snapshot.entries[missing].version) is not None, "读过即进缓存"
    assert len(snapshot.cache) == 2, "容量不变：挤掉最久没用的"


def test_zero_means_all_contents_cached(cache_root: Path):
    assert len(index.build(make_config(cache_root, max_cached_docs=0)).cache) == 3


def test_offline_and_uncached_content_is_unavailable(cache_root: Path, monkeypatch):
    config = make_config(cache_root, max_cached_docs=1)
    holder = index.IndexHolder(config)
    holder.build_now()
    _offline(monkeypatch, "memory")
    holder.request_rebuild("掉盘")
    deadline = time.time() + 30
    while time.time() < deadline and holder.rebuilding:
        time.sleep(0.05)
    snapshot = holder.snapshot
    cached = [k for k in snapshot.entries if snapshot.cache.get(k, snapshot.entries[k].version)]
    uncached = [k for k in snapshot.entries if k not in cached]
    assert snapshot.contents[cached[0]], "在缓存里的照常可读"
    with pytest.raises(index.ContentUnavailable):
        snapshot.contents[uncached[0]]
    total, hits = snapshot.search("SecProto", 5)
    assert total == 3, "检索照常"
    assert sum(h.text is None for h in hits) == 2, "不在缓存且掉盘的，片段取不到"


def test_cached_contents_survive_restart(cache_root: Path):
    config = make_config(cache_root, max_cached_docs=2)
    built = index.IndexHolder(config).build_now()
    before = {k for k in built.entries if built.cache.get(k, built.entries[k].version)}
    loaded = index.load_cache(config)
    after = {k for k in loaded.entries if loaded.cache.get(k, loaded.entries[k].version)}
    assert after == before and len(after) == 2
