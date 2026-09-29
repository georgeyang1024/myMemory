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


# --- 刷新只填空位、冷启动抢救、指纹去版本号（ADR-0026）-------------------------

def _wait_idle(holder: index.IndexHolder) -> None:
    deadline = time.time() + 30
    while time.time() < deadline and (holder.verifying or holder.rebuilding):
        time.sleep(0.05)


def _cached_keys(cache: index.ContentCache) -> list:
    return [key for key, _, _ in cache.dump()]


def test_offline_get_failure_does_not_enter_cache(cache_root: Path, monkeypatch):
    """掉盘且不在缓存里：get 失败，不进 LRU，也不挤掉别人。"""
    config = make_config(cache_root, max_cached_docs=1)
    holder = index.IndexHolder(config)
    holder.build_now()
    _offline(monkeypatch, "memory")
    holder.request_rebuild("掉盘")
    _wait_idle(holder)
    snapshot = holder.snapshot
    before = _cached_keys(snapshot.cache)
    uncached = next(k for k in snapshot.entries if k not in before)
    with pytest.raises(index.ContentUnavailable):
        snapshot.contents[uncached]
    assert _cached_keys(snapshot.cache) == before


def test_full_refresh_does_not_evict_offline_contents(multi_root: Path, monkeypatch):
    """缓存已满时，全量刷新读进来的在线全文不挤掉掉盘 source 的全文。"""
    config = multi_config(multi_root, max_cached_docs=2)
    snapshot = index.build(config)
    team_key = ("team", "机制.md")
    assert snapshot.contents[team_key]  # 真实使用：确保在缓存里
    _offline(monkeypatch, "team")

    index.refresh(config, snapshot.entries, full=True, cache=snapshot.cache)
    assert team_key in _cached_keys(snapshot.cache), "掉盘全文被刷新挤掉了"
    assert len(snapshot.cache) == 2


def test_refresh_does_not_reorder_lru(cache_root: Path):
    """刷新不算使用：不改变 LRU 顺序，也不在已满时放入新全文。"""
    config = make_config(cache_root, max_cached_docs=2)
    snapshot = index.build(config)
    first, second = _cached_keys(snapshot.cache)
    snapshot.contents[first]  # 使用 first，使它成为最近使用
    order = _cached_keys(snapshot.cache)
    assert order == [second, first]

    index.refresh(config, snapshot.entries, full=True, cache=snapshot.cache)
    assert _cached_keys(snapshot.cache) == order


def test_refresh_updates_cached_content_in_place(cache_root: Path):
    config = make_config(cache_root, max_cached_docs=2)
    snapshot = index.build(config)
    order = _cached_keys(snapshot.cache)
    source, path = order[0]
    target = cache_root / "memory" / path
    target.write_text("SecProto 改过的正文，长度也变了", encoding="utf-8")

    entries, _ = index.refresh(config, snapshot.entries, cache=snapshot.cache)
    assert _cached_keys(snapshot.cache) == order, "原地更新，不移动位置"
    # get 命中算使用、会移动位置，所以放在顺序断言之后
    assert snapshot.cache.get((source, path), entries[(source, path)].version) == \
        "SecProto 改过的正文，长度也变了"


def test_cache_fingerprint_ignores_code_version(kb_root: Path, monkeypatch):
    config = make_config(kb_root)
    before = index.cache_fingerprint(config)
    monkeypatch.setattr(index, "__version__", "9.9.9", raising=False)
    assert index.cache_fingerprint(config) == before, "升版本号不应作废缓存"


@pytest.fixture
def rescue_root(multi_root: Path) -> Path:
    """team 的正文足够长，切块参数一变块偏移就不同。"""
    (multi_root / "team" / "机制.md").write_text("团队规范：SecProto 机制流程。" * 100,
                                                encoding="utf-8")
    (multi_root / "team" / "附录.txt").write_text("团队附录：SecProto 名词表。", encoding="utf-8")
    return multi_root


def test_cold_start_rescues_offline_entries_with_content(rescue_root: Path, monkeypatch):
    """指纹不符 + 掉盘：有全文的掉盘条目按新配置重新切块分词，可搜、可读。"""
    index.IndexHolder(multi_config(rescue_root)).build_now()
    _offline(monkeypatch, "team")
    config = multi_config(rescue_root, chunk_size=300)  # 指纹变化

    holder = index.IndexHolder(config)
    snapshot = holder.start()
    _wait_idle(holder)
    snapshot = holder.snapshot
    key = ("team", "机制.md")
    text = "团队规范：SecProto 机制流程。" * 100
    assert key in snapshot.entries, "掉盘 source 不应在冷启动时消失"
    entry = snapshot.entries[key]
    expected = index._make_entry(config, "team", "机制.md", entry.mtime, entry.size, text, None)
    assert entry.spans == expected.spans and entry.tokens == expected.tokens, "应按新配置重新切块分词"
    assert snapshot.contents[key] == text
    total, hits = snapshot.search("SecProto 机制", 5, source="team")
    assert total >= 1 and all(h.text is not None for h in hits)


def test_cold_start_keeps_offline_entries_without_content(rescue_root: Path, monkeypatch):
    """没有全文的掉盘条目：沿用旧分词，能搜到，片段为空，读不到。"""
    old_config = multi_config(rescue_root, max_cached_docs=1)
    built = index.IndexHolder(old_config).build_now()
    key = ("team", "机制.md")
    assert built.cache.get(key, built.entries[key].version) is None, "前提：team 全文不在缓存里"
    old_entry = built.entries[key]
    _offline(monkeypatch, "team")

    holder = index.IndexHolder(multi_config(rescue_root, max_cached_docs=1, chunk_size=300))
    holder.start()
    _wait_idle(holder)
    snapshot = holder.snapshot
    assert snapshot.entries[key].spans == old_entry.spans, "没有全文只能沿用旧分词"
    total, hits = snapshot.search("团队规范", 5, source="team")
    assert total >= 1 and all(h.text is None for h in hits)
    with pytest.raises(index.ContentUnavailable):
        snapshot.contents[key]


def test_cold_start_rescue_drops_excluded_extensions(rescue_root: Path, monkeypatch):
    index.IndexHolder(multi_config(rescue_root)).build_now()
    _offline(monkeypatch, "team")

    holder = index.IndexHolder(multi_config(rescue_root, extensions=[".md"]))
    holder.start()
    _wait_idle(holder)
    assert ("team", "机制.md") in holder.snapshot.entries
    assert ("team", "附录.txt") not in holder.snapshot.entries


def test_cold_start_keeps_agent_marks(rescue_root: Path):
    """指纹不符的冷启动：在线 source 全量重读，但 edited_by 沿用旧缓存的 agent 标记。"""
    config = multi_config(rescue_root)
    holder = index.IndexHolder(config)
    holder.build_now()
    key = ("memory", "机制.md")
    holder.mark_agent(*key, (rescue_root / "memory" / "机制.md").stat().st_mtime)
    holder.request_rebuild("save")
    _wait_idle(holder)
    assert holder.snapshot.entries[key].edited_by == "agent"

    cold = index.IndexHolder(multi_config(rescue_root, chunk_size=300))
    cold.start()
    _wait_idle(cold)
    assert cold.snapshot.entries[key].edited_by == "agent"


@pytest.mark.parametrize("damage", ["garbage", "format"])
def test_unreadable_cache_is_not_rescued(rescue_root: Path, monkeypatch, damage):
    """格式不符或损坏：结构不可信，不抢救，掉盘 source 照旧丢失。"""
    import pickle

    config = multi_config(rescue_root)
    index.IndexHolder(config).build_now()
    if damage == "garbage":
        config.cache_file.write_bytes(b"not a pickle")
    else:
        payload = pickle.loads(config.cache_file.read_bytes())
        payload["format"] = "changed"
        config.cache_file.write_bytes(pickle.dumps(payload))
    _offline(monkeypatch, "team")

    holder = index.IndexHolder(config)
    holder.start()
    assert not any(k[0] == "team" for k in holder.snapshot.entries)


# --- 打分调整：scoring（ADR-0027） ------------------------------------------------

def scoring(**fields) -> dict:
    """打分调整全关，再按需打开个别项——让每条用例只观察一种加分。"""
    base = {"recency_window_days": 90, "recency_bonus": 0, "path_match_bonus": 0,
            "strip_wikilinks": False}
    base.update(fields)
    return base


@pytest.fixture
def scoring_root(tmp_path: Path) -> Path:
    raw = tmp_path / "memory"
    files = {
        "设计/设备安全方案总览.md": "设备 安全 方案 结论 会议",
        "会议/26-07-15-跨部门同步/会议结论.md": "设备 安全 方案 会议 结论",
        "会议/26-07-15-跨部门同步/方案.md": "设备 安全 方案 会议 结论",
        "会议/设备安全问题分析与解决方案.md": "设备 安全 方案 会议 结论",
    }
    for rel, body in files.items():
        (raw / rel).parent.mkdir(parents=True, exist_ok=True)
        (raw / rel).write_text(body, encoding="utf-8")
    return tmp_path


def _scores(root: Path, query: str, **fields) -> dict[str, float]:
    _, hits = build_index(root, scoring=scoring(**fields)).search(query, 20)
    return {h.path: h.score for h in hits}


def _path_bonus(root: Path, query: str) -> dict[str, float]:
    """开 path_match_bonus=5 与全关相比，每篇命中文档多出的分数。"""
    off = _scores(root, query)
    on = _scores(root, query, path_match_bonus=5)
    assert off.keys() == on.keys(), "加分不应改变命中集合"
    return {path: round(on[path] - off[path], 6) for path in off}


def test_path_bonus_when_filename_contains_whole_query(scoring_root: Path):
    bonus = _path_bonus(scoring_root, "设备安全方案")
    assert bonus["设计/设备安全方案总览.md"] == 5


def test_path_bonus_when_every_term_is_in_path(scoring_root: Path):
    bonus = _path_bonus(scoring_root, "26-07-15 会议结论")
    assert bonus["会议/26-07-15-跨部门同步/会议结论.md"] == 5


def test_no_path_bonus_when_only_some_terms_in_path(scoring_root: Path):
    bonus = _path_bonus(scoring_root, "26-07-15 会议结论")
    assert bonus["会议/26-07-15-跨部门同步/方案.md"] == 0


def test_no_path_bonus_when_query_not_contiguous_in_path(scoring_root: Path):
    bonus = _path_bonus(scoring_root, "设备安全方案")
    assert bonus["会议/设备安全问题分析与解决方案.md"] == 0


def test_path_bonus_lifts_matching_document_to_top(scoring_root: Path):
    _, hits = build_index(scoring_root, scoring=scoring(path_match_bonus=5)).search("设备安全方案", 5)
    assert hits[0].path == "设计/设备安全方案总览.md"
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)


def test_path_bonus_is_counted_once_per_document(scoring_root: Path):
    """重复的词不累加：每篇文档至多加一次。"""
    bonus = _path_bonus(scoring_root, "会议结论 会议结论 26-07-15")
    assert bonus["会议/26-07-15-跨部门同步/会议结论.md"] == 5


def test_bonus_does_not_admit_documents_without_query_terms(tmp_path: Path):
    """路径命中但没有任何检索词匹配的文档不进结果：加分只改变排序，不改变命中集合。"""
    raw = tmp_path / "memory"
    raw.mkdir(parents=True)
    (raw / "labx笔记.md").write_text("完全无关的内容。", encoding="utf-8")  # "ab" 是路径子串，但不是检索词
    (raw / "其它.md").write_text("ab 相关的内容。", encoding="utf-8")

    for fields in ({}, {"path_match_bonus": 5}):
        total, hits = build_index(tmp_path, scoring=scoring(**fields)).search("ab", 10)
        assert total == 1
        assert [h.path for h in hits] == ["其它.md"]


def test_diversity_cap_still_holds_with_path_bonus(tmp_path: Path):
    """路径命中加分让大文档的每个 chunk 都加分，但每篇文档最多两个结果位的规则不变。"""
    raw = tmp_path / "memory"
    raw.mkdir(parents=True)
    (raw / "SecProto大文档.md").write_text("SecProto 机制 攻击 防护 措施。" * 400, encoding="utf-8")
    (raw / "小文档.md").write_text("SecProto 机制 攻击 防护 措施。", encoding="utf-8")

    _, hits = build_index(tmp_path, scoring=scoring(path_match_bonus=5)).search("SecProto", 3)
    assert [h.path for h in hits].count("SecProto大文档.md") == index.MAX_CHUNKS_PER_DOC
    assert "小文档.md" in {h.path for h in hits}


DAY = 86400.0
NOW = 1_800_000_000.0  # 固定"当前时间"，mtime 相对它设置


@pytest.fixture
def aged_root(tmp_path: Path) -> Path:
    """正文完全相同、只有修改时间不同的几篇文档：BM25 分相同，差异只来自时间加分。"""
    raw = tmp_path / "memory"
    raw.mkdir(parents=True)
    for name, age_days in {"新.md": 5, "中.md": 45, "旧.md": 80, "过期.md": 120, "未来.md": -2}.items():
        path = raw / name
        path.write_text("SecProto 机制 防护 措施", encoding="utf-8")
        mtime = NOW - age_days * DAY
        os.utime(path, (mtime, mtime))
    return tmp_path


def _recency_bonus(root: Path, **fields) -> dict[str, float]:
    """开时间加分与全关相比，每篇命中文档多出的分数（当前时间固定为 NOW）。"""
    def scores(**f):
        _, hits = build_index(root, scoring=scoring(**f)).search("SecProto", 20, now=NOW)
        return {h.path: h.score for h in hits}
    off, on = scores(), scores(**fields)
    assert off.keys() == on.keys(), "加分不应改变命中集合"
    return {path: round(on[path] - off[path], 6) for path in off}


def test_recency_bonus_decays_linearly(aged_root: Path):
    bonus = _recency_bonus(aged_root, recency_window_days=90, recency_bonus=10)
    assert bonus["中.md"] == 5
    assert bonus["新.md"] == round(10 * (1 - 5 / 90), 6)


def test_no_recency_bonus_outside_window(aged_root: Path):
    bonus = _recency_bonus(aged_root, recency_window_days=90, recency_bonus=10)
    assert bonus["过期.md"] == 0


def test_future_mtime_gets_at_most_full_bonus(aged_root: Path):
    bonus = _recency_bonus(aged_root, recency_window_days=90, recency_bonus=10)
    assert bonus["未来.md"] == 10


def test_recency_disabled_when_window_or_bonus_is_zero(aged_root: Path):
    for fields in ({"recency_window_days": 0, "recency_bonus": 10},
                   {"recency_window_days": 90, "recency_bonus": 0}):
        assert set(_recency_bonus(aged_root, **fields).values()) == {0}


def test_newer_document_beats_equally_relevant_older_one(aged_root: Path):
    _, hits = build_index(aged_root, scoring=scoring(recency_bonus=10)).search("SecProto", 20, now=NOW)
    order = [h.path for h in hits]
    assert order.index("新.md") < order.index("旧.md")


def test_source_can_disable_recency_bonus(tmp_path: Path):
    """一个 source 关掉时间加分，只影响它自己的文档。"""
    for name in ("fresh", "synced"):
        (tmp_path / name).mkdir()
        path = tmp_path / name / "机制.md"
        path.write_text("SecProto 机制", encoding="utf-8")
        os.utime(path, (NOW, NOW))

    def scores(synced_scoring):
        config = make_config(tmp_path, scoring=scoring(recency_bonus=10), sources=[
            {"name": "fresh", "dir": str(tmp_path / "fresh")},
            {"name": "synced", "dir": str(tmp_path / "synced"), "scoring": synced_scoring},
        ])
        _, hits = index.build(config).search("SecProto", 5, now=NOW)
        return {h.source: h.score for h in hits}

    inherit, disabled = scores({}), scores({"recency_bonus": 0})
    assert round(inherit["synced"] - disabled["synced"], 6) == 10
    assert inherit["fresh"] == disabled["fresh"]


# --- strip_wikilinks：分词前整段去掉 [[...]]（等长遮罩，偏移不变） -------------

@pytest.fixture
def wikilink_root(tmp_path: Path) -> Path:
    raw = tmp_path / "memory"
    raw.mkdir(parents=True)
    (raw / "链接.md").write_text(
        "正文提到 SecProto 机制。见 [[raw/x/zqlinkword|zqalias]] 了解更多。", encoding="utf-8")
    # 链接跨越首个切块的结尾（切块 800 字、步长 680）：半截链接也必须被去掉
    head = "SecProto 机制说明。" * 60
    head = head[:770]
    body = head + "[[raw/zqcrossterm/" + "a" * 30 + "]]" + " 后续正文。" * 40
    (raw / "跨界.md").write_text(body, encoding="utf-8")
    return tmp_path


def _wikilink_search(root: Path, query: str, strip: bool):
    return build_index(root, scoring=scoring(strip_wikilinks=strip)).search(query, 10)


@pytest.mark.parametrize("query", ["zqlinkword", "zqalias", "zqcrossterm"])
def test_wikilink_text_does_not_contribute_terms(wikilink_root: Path, query: str):
    assert _wikilink_search(wikilink_root, query, strip=True) == (0, [])


@pytest.mark.parametrize("query", ["zqlinkword", "zqalias", "zqcrossterm"])
def test_wikilink_text_is_searchable_when_stripping_is_off(wikilink_root: Path, query: str):
    total, _ = _wikilink_search(wikilink_root, query, strip=False)
    assert total >= 1


def test_stripping_keeps_chunk_spans_and_snippets(wikilink_root: Path):
    on = build_index(wikilink_root, scoring=scoring(strip_wikilinks=True))
    off = build_index(wikilink_root, scoring=scoring(strip_wikilinks=False))
    assert {k: e.spans for k, e in on.entries.items()} == {k: e.spans for k, e in off.entries.items()}

    _, hits = on.search("SecProto", 10)
    assert hits
    for hit in hits:
        content = on.contents[(hit.source, hit.path)]
        assert content[hit.char_start:hit.char_end] == hit.text
    assert any("[[raw/x/zqlinkword|zqalias]]" in (h.text or "") for h in hits), "片段仍是原文"


# --- 缓存指纹：strip_wikilinks 改变检索词，必须作废缓存；查询时加分不影响 --------

def _fp(root: Path, **overrides) -> str:
    return index.cache_fingerprint(make_config(root, **overrides))


def test_toggling_strip_wikilinks_invalidates_cache(kb_root: Path):
    assert _fp(kb_root, scoring={"strip_wikilinks": True}) != \
        _fp(kb_root, scoring={"strip_wikilinks": False})


def test_source_level_strip_override_invalidates_cache(kb_root: Path):
    base = [{"name": "memory", "dir": str(kb_root / "memory")}]
    overridden = [{**base[0], "scoring": {"strip_wikilinks": False}}]
    assert _fp(kb_root, sources=base) != _fp(kb_root, sources=overridden)


def test_query_time_bonuses_do_not_invalidate_cache(kb_root: Path):
    assert _fp(kb_root) == _fp(kb_root, scoring={"recency_bonus": 3, "recency_window_days": 7,
                                                 "path_match_bonus": 1})


def test_adding_source_with_default_scoring_keeps_fingerprint(kb_root: Path):
    (kb_root / "team").mkdir()
    one = [{"name": "memory", "dir": str(kb_root / "memory")}]
    two = one + [{"name": "team", "dir": str(kb_root / "team")}]
    assert _fp(kb_root, sources=one) == _fp(kb_root, sources=two)


def test_cache_written_with_other_strip_setting_is_not_fresh(kb_root: Path):
    on = make_config(kb_root, scoring={"strip_wikilinks": True})
    index.save_cache(on, index.build(on))
    assert index.read_cache(on)[1] is True
    off = make_config(kb_root, scoring={"strip_wikilinks": False})
    assert index.read_cache(off)[1] is False


# --- 排序回归：过期会议稿压过当前结论文档（ADR-0027 的原始问题） ------------------

@pytest.fixture
def stale_meeting_root(tmp_path: Path) -> Path:
    """复刻原始问题：会议稿旧、词频高（目录名长题名 + 大量 wikilink），总览新、文件名含查询。"""
    raw = tmp_path / "memory"
    title = "设备安全问题分析与解决方案"
    link = f"[[raw/会议/26-07-15-{title}-跨部门同步/{title}|{title}]]"
    old = NOW - 80 * DAY
    docs = {
        f"会议/26-07-15-{title}-跨部门同步/{title}.md": (f"# {title}\n" + f"- {link}\n" * 12
                                                     + "设备 安全 方案 评审。\n" * 6, old),
        f"会议/26-07-15-{title}-跨部门同步/会议结论.md": ("会议结论：采用方案二。" + link * 6, old),
        f"会议/26-07-17-{title}-调整2/{title}-第二版.md": (f"# {title}\n" + f"- {link}\n" * 10, old),
        f"会议/26-07-23-{title}-组内评审/方案设计文档.md": ("设备 安全 方案 维度。" * 8 + link * 4, old),
        "设计/设备安全方案总览.md": ("# 设备安全方案总览\n一句话结论：新方案已完成设计并通过 Demo 实测。"
                              "安全模式采用新广播与按键鉴权。", NOW - 5 * DAY),
    }
    # 与查询无关的文档：让查询词只出现在少数 chunk 里，IDF 为正——与真实语料一致，
    # 否则分数全挤在 0 附近，排序只剩并列次序。
    for i in range(20):
        docs[f"其它/无关{i}.md"] = (f"包装设计与物流流程说明第{i}篇。", old)
    for rel, (body, mtime) in docs.items():
        path = raw / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        os.utime(path, (mtime, mtime))
    return tmp_path


def test_regression_fixture_reproduces_stale_ranking_without_adjustments(stale_meeting_root: Path):
    """前提成立：关掉全部调整时，总览确实被会议稿压在下面。"""
    _, hits = build_index(stale_meeting_root, scoring=scoring()).search("设备安全方案", 10, now=NOW)
    assert hits[0].score > 0, "IDF 为正，排序由词频决定而非并列次序"
    assert hits[0].path.startswith("会议/")
    order = [h.path for h in hits]
    assert "设计/设备安全方案总览.md" not in order[:3]


def test_default_scoring_puts_current_summary_first(stale_meeting_root: Path):
    _, hits = build_index(stale_meeting_root).search("设备安全方案", 10, now=NOW)
    assert hits[0].path == "设计/设备安全方案总览.md"


def test_default_scoring_keeps_explicit_history_query_on_target(stale_meeting_root: Path):
    """已接受的取舍：近期改过、含查询词的文档可能压过路径完整命中的旧文档，历史目标仍在前 2。"""
    _, hits = build_index(stale_meeting_root).search("26-07-15 会议结论", 10, now=NOW)
    target = "会议/26-07-15-设备安全问题分析与解决方案-跨部门同步/会议结论.md"
    assert target in [h.path for h in hits[:2]]
