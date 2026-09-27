"""server 层单测：入参钳制、安全边界、分页、响应结构。"""

from pathlib import Path

import pytest

import index
from server import run_get_document, run_health, run_search

from test_corpus import make_config


@pytest.fixture
def env(tmp_path: Path):
    raw = tmp_path / "memory" / "Facts_And_Status"
    raw.mkdir(parents=True)
    (raw / "规格汇总.md").write_text("字段控制 Field:x; 单元以x档开始运转。" * 200, encoding="utf-8")
    (raw / "项目背景.md").write_text("当前模块与调用方的适配说明。" * 50, encoding="utf-8")
    config = make_config(tmp_path)
    return config, index.build(config)


# --- search ----------------------------------------------------------------

def test_search_response_shape(env):
    config, snapshot = env
    payload = run_search(config, snapshot, "字段控制", 3)
    assert set(payload) == {"index", "query", "source", "total_matched", "returned", "results"}
    assert set(payload["index"]) == {"built_at", "doc_count", "chunk_count"}
    for result in payload["results"]:
        assert set(result) == {
            "source", "path", "writable", "score", "chunk_index", "char_start", "char_end",
            "snippet", "snippet_truncated", "snippet_available",
        }


def test_search_index_meta_carries_freshness(env):
    config, snapshot = env
    payload = run_search(config, snapshot, "字段", 1)
    assert payload["index"]["doc_count"] == snapshot.doc_count
    assert payload["index"]["built_at"] == snapshot.built_at.isoformat(timespec="seconds")


def test_search_empty_query_is_rejected(env):
    config, snapshot = env
    payload = run_search(config, snapshot, "   ", 5)
    assert payload["error"] and payload["results"] == []


def test_search_limit_is_clamped_not_rejected(env):
    config, snapshot = env
    assert len(run_search(config, snapshot, "字段", 999_999)["results"]) <= config.max_results
    assert len(run_search(config, snapshot, "字段", -5)["results"]) >= 1
    assert len(run_search(config, snapshot, "字段", "abc")["results"]) <= config.default_results


def test_search_query_is_truncated_not_rejected(env):
    config, snapshot = env
    payload = run_search(config, snapshot, "字段" * 1000, 3)
    assert len(payload["query"]) == config.max_query_chars


def test_snippet_is_truncated_and_flagged(env):
    config, snapshot = env
    small = make_config(config.config_file.parent, MEMORY_SNIPPET_CHARS="100")
    payload = run_search(small, snapshot, "字段控制", 1)
    result = payload["results"][0]
    assert len(result["snippet"]) == 100
    assert result["snippet_truncated"] is True


# --- get_document 安全边界 ---------------------------------------------------

@pytest.mark.parametrize(
    "attack",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "../raw/Facts_And_Status/规格汇总.md",
        "Facts_And_Status/../Facts_And_Status/规格汇总.md",
        "wiki/index.md",
        "../src/config.py",
        "Facts_And_Status/规格汇总.MD",
        "",
        "   ",
    ],
)
def test_get_document_rejects_non_indexed_paths(env, attack):
    config, snapshot = env
    payload = run_get_document(config, snapshot, "memory", attack, 0, 1000)
    assert "error" in payload, f"必须拒绝：{attack!r}"
    assert "content" not in payload


def test_get_document_rejection_offers_suggestions(env):
    config, snapshot = env
    payload = run_get_document(config, snapshot, "memory", "规格汇总.md", 0, 1000)
    assert payload["suggestions"], "拒绝时应给出相近路径，避免 LLM 盲目重试"
    assert any("规格汇总" in s["path"] and s["source"] == "memory" for s in payload["suggestions"])


def test_get_document_accepts_indexed_path(env):
    config, snapshot = env
    path = "Facts_And_Status/规格汇总.md"
    payload = run_get_document(config, snapshot, "memory", path, 0, 40_000)
    assert payload["source"] == "memory" and payload["path"] == path
    assert payload["content"] == snapshot.contents[("memory", path)][: payload["returned_chars"]]
    assert payload["total_chars"] == len(snapshot.contents[("memory", path)])


# --- get_document 分页 -------------------------------------------------------

def test_get_document_paginates(env):
    config, snapshot = env
    path = "Facts_And_Status/规格汇总.md"
    first = run_get_document(config, snapshot, "memory", path, 0, 100)
    assert first["returned_chars"] == 100
    assert first["has_more"] is True
    assert first["next_offset"] == 100

    second = run_get_document(config, snapshot, "memory", path, first["next_offset"], 100)
    assert second["offset"] == 100
    assert first["content"] + second["content"] == snapshot.contents[("memory", path)][:200]


def test_get_document_last_page_reports_no_more(env):
    config, snapshot = env
    path = "Facts_And_Status/项目背景.md"
    payload = run_get_document(config, snapshot, "memory", path, 0, 40_000)
    assert payload["has_more"] is False
    assert payload["next_offset"] is None


def test_get_document_limit_is_clamped(env):
    config, snapshot = env
    path = "Facts_And_Status/规格汇总.md"
    payload = run_get_document(config, snapshot, "memory", path, 0, 999_999)
    assert payload["returned_chars"] <= config.max_doc_chars


def test_get_document_offset_beyond_end_returns_empty(env):
    config, snapshot = env
    path = "Facts_And_Status/项目背景.md"
    total = len(snapshot.contents[("memory", path)])
    payload = run_get_document(config, snapshot, "memory", path, total, 100)
    assert payload["returned_chars"] == 0
    assert payload["has_more"] is False


# --- source 维度 ----------------------------------------------------------

def test_get_document_requires_matching_source(env):
    """同一个 path 换一个 source 名就不是同一篇——身份是 (source, path)。"""
    config, snapshot = env
    path = "Facts_And_Status/规格汇总.md"
    for source in ("", "   ", "team", "memory/Facts_And_Status", "Memory"):
        payload = run_get_document(config, snapshot, source, path, 0, 100)
        assert "error" in payload and "content" not in payload, source


def test_get_document_rejects_source_glued_into_path(env):
    config, snapshot = env
    payload = run_get_document(config, snapshot, "memory",
                               "memory/Facts_And_Status/规格汇总.md", 0, 100)
    assert "error" in payload


def test_search_unknown_source_returns_error_with_names(env):
    config, snapshot = env
    payload = run_search(config, snapshot, "字段", 3, "不存在")
    assert payload["error"] and payload["sources"] == ["memory"]


def test_search_scoped_to_source(env):
    config, snapshot = env
    payload = run_search(config, snapshot, "字段", 3, "memory")
    assert payload["source"] == "memory"
    assert payload["results"] and all(r["source"] == "memory" for r in payload["results"])


# --- health ----------------------------------------------------------------

def test_health_carries_index_config_and_sources(env):
    """/health 回答：索引多大、配置文件在哪、有哪些 source。"""
    config, snapshot = env
    body = run_health(config, snapshot)

    assert body["status"] == "ok"
    assert body["version"] == "0.2.0"
    assert body["doc_count"] == snapshot.doc_count
    assert body["config_file"] == str(config.config_file)
    assert body["sources"] == [
        {"name": "memory", "writable": True, "available": True, "unavailable_reason": None,
         "doc_count": 2, "description": "", "dir": str(config.source("memory").dir)}
    ]
    assert body["verifying"] is False
    assert body["cached_docs"] == 2 and body["max_cached_docs"] == 1000


def test_health_has_no_git_or_single_root(env):
    config, snapshot = env
    body = run_health(config, snapshot)
    assert "git" not in body
    assert "root" not in body and "scan_dirs" not in body


# --- list-sources 与 recent ---------------------------------------------

import os  # noqa: E402

from server import run_list_sources, run_recent, run_save  # noqa: E402
from storage import DISK_OFFLINE, Availability  # noqa: E402


@pytest.fixture
def multi(tmp_path: Path):
    for name in ("memory", "team", "org"):
        (tmp_path / name).mkdir()
    config = make_config(tmp_path, sources=[
        {"name": "memory", "dir": str(tmp_path / "memory"), "description": "个人"},
        {"name": "team", "dir": str(tmp_path / "team")},
        {"name": "org", "dir": str(tmp_path / "org"), "writable": False, "description": "制度"},
    ])
    return config


def _touch(path: Path, text: str, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))


def _settle(holder):
    import time
    deadline = time.time() + 30
    while time.time() < deadline and holder.rebuilding:
        time.sleep(0.05)
    return holder.snapshot


def test_list_sources_reports_writability_without_dirs(multi):
    _touch(multi.source("org").dir / "制度.md", "报销", 1_000_000)
    body = run_list_sources(multi, index.build(multi))
    assert body["sources"] == [
        {"name": "memory", "writable": True, "available": True, "unavailable_reason": None,
         "doc_count": 0, "description": "个人"},
        {"name": "team", "writable": True, "available": True, "unavailable_reason": None,
         "doc_count": 0, "description": ""},
        {"name": "org", "writable": False, "available": True, "unavailable_reason": None,
         "doc_count": 1, "description": "制度"},
    ]
    assert str(multi.source("memory").dir) not in str(body), "不得暴露目录绝对路径"


def test_writable_flag_on_every_document_output(multi):
    """search / recent / get-document 每条都带 writable，只读 source 为 false。"""
    _touch(multi.source("team").dir / "a.md", "SecProto 团队", 1_700_000_000)
    _touch(multi.source("org").dir / "b.md", "SecProto 制度", 1_700_000_001)
    snapshot = index.build(multi)
    by_ws = {r["source"]: r["writable"] for r in run_search(multi, snapshot, "SecProto", 5)["results"]}
    assert by_ws == {"team": True, "org": False}
    by_ws = {r["source"]: r["writable"] for r in run_recent(multi, snapshot, 10)["results"]}
    assert by_ws == {"team": True, "org": False}
    assert run_get_document(multi, snapshot, "org", "b.md", 0, 100)["writable"] is False
    assert run_get_document(multi, snapshot, "team", "a.md", 0, 100)["writable"] is True


def test_offline_source_is_stale_and_not_writable(multi, monkeypatch):
    """掉盘：内容仍可检索与读取（stale），但 writable 为 false、available 为 false。"""
    import storage

    _touch(multi.source("team").dir / "a.md", "SecProto 团队", 1_700_000_000)
    holder = index.IndexHolder(multi)
    holder.build_now()
    monkeypatch.setattr(storage.LocalStorage, "probe",
                        lambda self: Availability(False, DISK_OFFLINE) if self.source.name == "team"
                        else storage.AVAILABLE)
    holder.request_rebuild("测试掉盘")
    snapshot = _settle(holder)

    doc = run_get_document(multi, snapshot, "team", "a.md", 0, 100)
    assert doc["content"].startswith("SecProto 团队") and doc["stale"] is True and doc["writable"] is False
    assert run_get_document(multi, snapshot, "memory", "x", 0, 1).get("stale") is None  # 被拒绝的路径
    hit = run_search(multi, snapshot, "SecProto", 5)["results"][0]
    assert hit["source"] == "team" and hit["writable"] is False
    team = next(w for w in run_list_sources(multi, snapshot)["sources"] if w["name"] == "team")
    assert team["available"] is False and team["unavailable_reason"] == DISK_OFFLINE
    assert team["writable"] is False and team["doc_count"] == 1

    payload = run_save(multi, holder, "team", "", "新", "内容")
    assert payload["saved"] is False and "team" not in payload["writable_sources"]


def test_recent_orders_by_mtime_and_limits(multi):
    base = 1_700_000_000
    for i in range(25):
        _touch(multi.source("memory").dir / f"第{i:02d}篇.md", "x" * i, base + i)
    snapshot = index.build(multi)

    default = run_recent(multi, snapshot, None)
    assert default["returned"] == 10, "默认 10 条"
    assert [r["path"] for r in default["results"]][:2] == ["第24篇.md", "第23篇.md"]
    assert run_recent(multi, snapshot, 999)["returned"] == 20, "最多 20 条"
    assert run_recent(multi, snapshot, -1)["returned"] == 1

    first = default["results"][0]
    assert set(first) == {"source", "path", "writable", "updated_at", "size", "edited_by"}
    assert first["source"] == "memory" and first["size"] == 24
    assert first["updated_at"].startswith("2023-11-")


def test_recent_one_entry_per_file(multi):
    target = multi.source("memory").dir / "同一篇.md"
    for mtime in (1_700_000_000, 1_700_000_100, 1_700_000_200):
        _touch(target, "改了又改", mtime)
    body = run_recent(multi, index.build(multi), 20)
    assert [r["path"] for r in body["results"]] == ["同一篇.md"]


def test_recent_edited_by_agent_then_scan(multi):
    holder = index.IndexHolder(multi)
    holder.build_now()
    run_save(multi, holder, "team", "技术", "规范", "agent 写的")
    _touch(multi.source("memory").dir / "扫描发现的.md", "扫描发现的", 1_600_000_000)
    holder.request_rebuild("测试")
    snapshot = _settle(holder)

    by_key = {(r["source"], r["path"]): r["edited_by"]
              for r in run_recent(multi, snapshot, 20)["results"]}
    assert by_key[("team", "技术/规范.md")] == "agent"
    assert by_key[("memory", "扫描发现的.md")] == "scan"

    # save 之后文件又被改动（编辑器、别的设备或程序）→ 以扫描到的现状为准
    agent_file = multi.source("team").dir / "技术" / "规范.md"
    later = agent_file.stat().st_mtime + 10
    os.utime(agent_file, (later, later))
    holder.request_rebuild("测试")
    snapshot = _settle(holder)
    by_key = {(r["source"], r["path"]): r["edited_by"]
              for r in run_recent(multi, snapshot, 20)["results"]}
    assert by_key[("team", "技术/规范.md")] == "scan"


def test_recent_agent_mark_survives_restart(multi):
    holder = index.IndexHolder(multi)
    holder.build_now()
    run_save(multi, holder, "memory", "", "重启前", "内容")
    _settle(holder)

    restarted = index.IndexHolder(multi)
    restarted.start()
    snapshot = _settle(restarted)
    body = run_recent(multi, snapshot, 5)
    assert body["results"][0]["path"] == "重启前.md"
    assert body["results"][0]["edited_by"] == "agent"


def test_recent_scoped_and_unknown_source(multi):
    _touch(multi.source("memory").dir / "a.md", "a", 1_700_000_000)
    _touch(multi.source("team").dir / "b.md", "b", 1_700_000_001)
    snapshot = index.build(multi)
    assert [r["path"] for r in run_recent(multi, snapshot, 10, "memory")["results"]] == ["a.md"]
    assert run_recent(multi, snapshot, 10, "nope")["error"]


def test_failed_save_passively_probes_and_updates_availability(multi, monkeypatch):
    """快照说可写、盘却在两次刷新之间掉了：写失败时被动探测，立即把状态更新过来。

    曾出现 list-sources 显示 available / writable、save 却报"挂载盘掉线"的矛盾。
    """
    import storage

    holder = index.IndexHolder(multi)
    holder.build_now()
    assert holder.snapshot.availability_of("team").available  # 快照：可用

    monkeypatch.setattr(storage.LocalStorage, "probe",
                        lambda self: Availability(False, DISK_OFFLINE) if self.source.name == "team"
                        else storage.AVAILABLE)
    before = holder.snapshot
    payload = run_save(multi, holder, "team", "", "新", "内容")

    assert payload["saved"] is False and "掉线" in payload["error"]
    assert "team" not in payload["writable_sources"], "报错里的可写列表必须与失败原因一致"
    team = next(w for w in run_list_sources(multi, holder.snapshot)["sources"] if w["name"] == "team")
    assert team["available"] is False and team["unavailable_reason"] == DISK_OFFLINE
    assert team["writable"] is False
    assert holder.snapshot.bm25 is before.bm25, "只换可用性，索引内容原样复用"
    _settle(holder)


def test_list_sources_does_not_probe_live(multi, monkeypatch):
    """平时的 list-sources 只读快照，不做现场探测。"""
    import storage

    snapshot = index.build(multi)
    monkeypatch.setattr(storage.LocalStorage, "probe", lambda self: pytest.fail("不应现场探测"))
    run_list_sources(multi, snapshot)
