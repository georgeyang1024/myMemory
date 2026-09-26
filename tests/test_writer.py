"""writer 层与 create 工具：写入边界、不覆盖、异步刷新。

写入是本服务唯一一条能改变磁盘状态的路径，因此这里的每一条用例
都对应一条明确的边界，而不是"覆盖率"。
"""

import time
from pathlib import Path

import pytest

import index
from index import IndexHolder
from server import run_save, run_search
from writer import WriteError, save_memory

from test_corpus import make_config


@pytest.fixture
def config(tmp_path: Path):
    for name in ("memory", "team", "company"):
        (tmp_path / name).mkdir()
    return make_config(tmp_path, sources=[
        {"name": "memory", "dir": str(tmp_path / "memory")},
        {"name": "team", "dir": str(tmp_path / "team")},
        {"name": "company", "dir": str(tmp_path / "company"), "writable": False},
    ])


def root(config):
    return config.source("memory").dir


# --- 落盘形态 --------------------------------------------------------------

def test_creates_file_under_one_level_category(config):
    written = save_memory(config, "memory", "技术", "BLE配对流程", "# 配对\n\nLESC 走 ECDH。")
    assert written.path == "技术/BLE配对流程.md"
    assert written.created is True and written.replaced_char_count is None
    assert written.absolute == root(config) / "技术" / "BLE配对流程.md"
    assert written.absolute.read_text(encoding="utf-8") == "# 配对\n\nLESC 走 ECDH。\n"


def test_content_is_written_verbatim_without_frontmatter(config):
    """服务不加 frontmatter、标题或时间戳——写进去的就是调用方给的内容。

    加了就意味着每篇记忆都带一段调用方没要求、也不知道存在的文本，
    它会进索引、会出现在检索片段里，且无法关掉。
    """
    body = "随手记一句。"
    written = save_memory(config, "memory", "杂记", "随手", body)
    assert written.absolute.read_text(encoding="utf-8") == body + "\n"


def test_trailing_newline_is_not_doubled(config):
    written = save_memory(config, "memory", "杂记", "已有换行", "正文。\n")
    assert written.absolute.read_text(encoding="utf-8") == "正文。\n"


@pytest.mark.parametrize("filename", ["笔记", "笔记.md", "笔记.MD", "  笔记.md  "])
def test_md_suffix_is_optional_and_normalized(config, filename):
    """带不带 .md 都接受，对齐到同一种形态——不值得为此让 LLM 多跑一轮。"""
    written = save_memory(config, "memory", "杂记", filename, "内容")
    assert written.path == "杂记/笔记.md"


def test_new_category_directory_is_created(config):
    save_memory(config, "memory", "全新分类", "第一篇", "内容")
    assert (root(config) / "全新分类").is_dir()


@pytest.mark.parametrize("filename", [
    "会议纪要(9月)", "v1.2 发布说明", "[草稿] 方案", "A&B", "{模板}", "a,b", "#1 问题",
    "C++ 笔记", "me@home", "重要!", "it's", "a=b", "~备忘", "100%", "（全角括号）",
])
def test_common_ascii_symbols_are_allowed(config, filename):
    """需求 4：英文括号等常用符号必须能保存。"""
    written = save_memory(config, "memory", "技术", filename, "内容")
    assert written.path == f"技术/{filename}.md"
    assert (root(config) / "技术" / f"{filename}.md").exists()


def test_category_with_symbols_is_allowed(config):
    written = save_memory(config, "memory", "项目(A)", "说明", "内容")
    assert written.path == "项目(A)/说明.md"


@pytest.mark.parametrize("category", ["", "   ", None])
def test_empty_category_writes_to_source_root(config, category):
    written = save_memory(config, "memory", category, "根目录记忆", "内容")
    assert written.path == "根目录记忆.md"
    assert written.absolute == root(config) / "根目录记忆.md"
    assert written.absolute.exists()


def test_writes_go_to_the_named_source(config):
    written = save_memory(config, "team", "技术", "规范", "内容")
    assert written.source == "team"
    assert written.absolute == config.source("team").dir / "技术" / "规范.md"
    assert not (root(config) / "技术" / "规范.md").exists()


@pytest.mark.parametrize("source", ["", "   ", None, "不存在", "company"])
def test_missing_unknown_or_readonly_source_is_rejected(config, source, tmp_path):
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    with pytest.raises(WriteError):
        save_memory(config, source, "技术", "文件", "内容")
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before, "被拒绝的写入不得落任何文件"


def test_storage_refuses_readonly_even_if_writer_is_bypassed(config):
    """只读的最后一道闸在 storage 层：绕过 writer 的校验也写不进去。"""
    from storage import open_storage

    with pytest.raises(PermissionError):
        open_storage(config.source("company")).write_text("x.md", "内容")


def test_written_reports_mtime_on_disk(config):
    written = save_memory(config, "memory", "技术", "时间", "内容")
    assert written.mtime == written.absolute.stat().st_mtime


# --- 写入边界 --------------------------------------------------------------

@pytest.mark.parametrize(
    "category",
    [
        "..",
        "../外面",
        "技术/协议",      # 嵌套：分类只有一级
        "/绝对路径",
        ".",
        ".隐藏",          # 隐藏目录不会被 corpus.scan 索引，写了也搜不到
        "尾点.",
        "a\\b",
        "a:b",
        "a" * 65,
    ],
)
def test_illegal_category_is_rejected(config, category):
    with pytest.raises(WriteError):
        save_memory(config, "memory", category, "文件", "内容")


@pytest.mark.parametrize(
    "filename",
    ["../逃逸", "子目录/文件", "/绝对", "..", "", "   ", "x" * 121,
     "a\\b", "a:b", "问号?", "星*号", '引"号', "尖<括>号", "竖|线",
     ".hidden", "尾点.", "双..点", "CON", "nul", "Com1", "LPT9.备份"],
)
def test_illegal_filename_is_rejected(config, filename):
    with pytest.raises(WriteError):
        save_memory(config, "memory", "技术", filename, "内容")


def test_nothing_is_written_outside_root(config, tmp_path):
    """越界入参被拒绝后，记忆根目录之外不应出现任何新文件。"""
    before = sorted(p.name for p in tmp_path.iterdir())
    for bad in ("../../外面", "..", "/etc"):
        with pytest.raises(WriteError):
            save_memory(config, "memory", bad, "文件", "内容")
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_empty_content_is_rejected(config):
    with pytest.raises(WriteError):
        save_memory(config, "memory", "技术", "空的", "   \n  ")


def test_oversized_content_is_rejected(config):
    with pytest.raises(WriteError):
        save_memory(config, "memory", "技术", "太长", "x" * (config.max_create_chars + 1))


# --- 覆盖（upsert）---------------------------------------------------------

def test_existing_file_is_overwritten_in_full(config):
    """同 category + filename 整篇替换，不追加、不合并。"""
    save_memory(config, "memory", "技术", "同名", "原始内容")
    written = save_memory(config, "memory", "技术", "同名", "新内容")
    assert (root(config) / "技术" / "同名.md").read_text(encoding="utf-8") == "新内容\n"
    assert written.created is False


def test_overwrite_reports_what_it_replaced(config):
    """覆盖必须是**可见**的：没有备份，响应里的这两个字段是调用方唯一的察觉机会。"""
    save_memory(config, "memory", "技术", "同名", "一段挺长的原始内容" * 10)
    old_len = len((root(config) / "技术" / "同名.md").read_text(encoding="utf-8"))
    written = save_memory(config, "memory", "技术", "同名", "短")
    assert written.created is False
    assert written.replaced_char_count == old_len


def test_overwrite_does_not_leave_trailing_remnants(config):
    """长内容被短内容覆盖后，文件里不能残留旧尾巴（写模式必须截断）。"""
    save_memory(config, "memory", "技术", "同名", "x" * 5000)
    save_memory(config, "memory", "技术", "同名", "短")
    assert (root(config) / "技术" / "同名.md").read_text(encoding="utf-8") == "短\n"


def test_empty_content_cannot_wipe_an_existing_memory(config):
    """空正文一律拒绝——否则一次误调用就能把一篇记忆清空。"""
    save_memory(config, "memory", "技术", "同名", "重要内容")
    with pytest.raises(WriteError):
        save_memory(config, "memory", "技术", "同名", "   \n ")
    assert (root(config) / "技术" / "同名.md").read_text(encoding="utf-8") == "重要内容\n"


# --- create 工具的响应体 ----------------------------------------------------

def test_run_save_reports_path_and_refresh(config):
    holder = IndexHolder(config)
    holder.build_now()
    payload = run_save(config, holder, "memory", "技术", "新记忆", "正文内容")
    assert payload["saved"] is True
    assert payload["created"] is True
    assert payload["replaced_char_count"] is None
    assert payload["source"] == "memory" and payload["path"] == "技术/新记忆.md"
    assert payload["index_refresh"] in {"started", "merged"}


def test_run_save_distinguishes_overwrite_from_create(config):
    holder = IndexHolder(config)
    holder.build_now()
    run_save(config, holder, "memory", "技术", "同名", "原始内容")
    payload = run_save(config, holder, "memory", "技术", "同名", "新内容")
    assert payload["saved"] is True
    assert payload["created"] is False, "覆盖必须与新建可区分，否则调用方无从告知用户"
    assert payload["replaced_char_count"] == len("原始内容\n")


def test_run_save_reports_error_without_raising(config):
    """入参非法时返回 {"created": false, "error"}，而不是抛异常。

    MCP 工具抛异常对调用方是一条协议级错误，可读性远不如一条它能据以
    纠正的消息。
    """
    holder = IndexHolder(config)
    holder.build_now()
    payload = run_save(config, holder, "memory", "技术/嵌套", "文件", "内容")
    assert payload["saved"] is False
    assert "嵌套" in payload["error"]
    assert payload["writable_sources"] == ["memory", "team"]


def test_run_save_to_readonly_lists_writable_sources(config):
    holder = IndexHolder(config)
    holder.build_now()
    payload = run_save(config, holder, "company",
                       "技术", "文件", "内容")
    assert payload["saved"] is False and "只读" in payload["error"]
    assert payload["writable_sources"] == ["memory", "team"]
    assert not any(config.source("company").dir.iterdir())


def test_run_save_marks_agent_in_index_and_cache(config):
    """agent 标记并入索引条目，并随缓存持久化——不再有 status.json。"""
    holder = IndexHolder(config)
    holder.build_now()
    run_save(config, holder, "team", "", "记录", "内容")
    assert _wait_for(lambda: ("team", "记录.md") in holder.snapshot.entries and not holder.rebuilding)
    assert holder.snapshot.entries[("team", "记录.md")].edited_by == "agent"
    assert not (config.config_file.parent / "status.json").exists()

    from index import load_cache
    assert load_cache(config).entries[("team", "记录.md")].edited_by == "agent", "重启后仍然是 agent"


def test_save_to_unavailable_source_is_rejected_without_recreating_dir(config):
    import shutil

    shutil.rmtree(config.source("team").dir)
    with pytest.raises(WriteError, match="不可用"):
        save_memory(config, "team", "技术", "文件", "内容")
    assert not config.source("team").dir.exists(), "不得把被删除的目录重新建出来"


def test_save_to_offline_source_is_rejected(config, monkeypatch):
    import storage
    from storage import DISK_OFFLINE, Availability

    monkeypatch.setattr(storage.LocalStorage, "probe", lambda self: Availability(False, DISK_OFFLINE))
    with pytest.raises(WriteError, match="掉线"):
        save_memory(config, "team", "", "文件", "内容")


# --- 异步刷新 --------------------------------------------------------------

def _wait_for(predicate, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_created_memory_becomes_searchable_after_refresh(config):
    holder = IndexHolder(config)
    holder.build_now()
    assert holder.snapshot.doc_count == 0

    run_save(config, holder, "memory", "技术", "跳频机制", "BLE 的自适应跳频每个连接事件换一次信道。")

    assert _wait_for(lambda: holder.snapshot.doc_count == 1), "后台刷新未在超时内完成"
    payload = run_search(config, holder.snapshot, "跳频", 5)
    assert payload["results"][0]["path"] == "技术/跳频机制.md"
    assert payload["results"][0]["source"] == "memory"


def test_concurrent_rebuilds_are_merged_not_stacked(config, monkeypatch):
    """连续创建多条记忆只跑有限几轮重建，而不是每条起一个线程全量构建。

    不合并的话，N 条记忆就是 N 个线程同时做全量构建，既浪费 CPU，
    也可能让先启动的线程用旧语料的结果覆盖后启动线程的新结果。
    """
    holder = IndexHolder(config)
    holder.build_now()

    builds = []
    real_refresh = index.refresh

    def counting_refresh(*args, **kwargs):
        builds.append(1)
        time.sleep(0.2)  # 拉长构建窗口，让后续请求必然撞进"已在进行中"
        return real_refresh(*args, **kwargs)

    monkeypatch.setattr(index, "refresh", counting_refresh)

    for i in range(5):
        run_save(config, holder, "memory", "技术", f"记忆{i}", f"第 {i} 条内容。")

    assert _wait_for(lambda: not holder.rebuilding), "重建标志未复位，后续刷新会被永久挡住"
    assert len(builds) < 5, f"5 次创建触发了 {len(builds)} 次构建，说明请求没有被合并"
    assert holder.snapshot.doc_count == 5, "合并后仍必须反映出全部 5 条记忆"


def test_rebuild_failure_keeps_old_snapshot(config, monkeypatch):
    """构建失败时保留旧快照，并把重建标志复位。

    标志若卡在 True，此后所有主动刷新都会被"已在进行中"挡掉，
    而且没有任何迹象可查——这是最难排查的一类故障。
    """
    save_memory(config, "memory", "技术", "已有记忆", "旧内容")
    holder = IndexHolder(config)
    holder.build_now()
    old = holder.snapshot

    monkeypatch.setattr(index, "refresh", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    run_save(config, holder, "memory", "技术", "新记忆", "新内容")

    assert _wait_for(lambda: not holder.rebuilding), "构建失败后重建标志未复位"
    assert holder.snapshot is old, "构建失败不得丢弃旧快照"
