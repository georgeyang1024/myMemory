"""writer 层与 create 工具：写入边界、不覆盖、异步刷新。

写入是本服务唯一一条能改变磁盘状态的路径，因此这里的每一条用例
都对应一条明确的边界，而不是"覆盖率"。
"""

import time
from pathlib import Path

import pytest

import index
from index import IndexHolder
from server import run_delete, run_rename, run_replace, run_save, run_search
from writer import (WriteError, delete_memory, rename_memory,
                    replace_memory, save_memory)

from test_corpus import make_config


@pytest.fixture
def config(tmp_path: Path):
    """写操作通用配置：allow_mcp_delete=True，供 rename/delete 用例直接使用。

    删除开关默认关闭的行为在 config_nodelete / delete 专用用例里单测，
    这里保持生活场景（人类已人工开闸）。
    """
    for name in ("memory", "team", "org"):
        (tmp_path / name).mkdir()
    return make_config(tmp_path, allow_mcp_delete=True, sources=[
        {"name": "memory", "dir": str(tmp_path / "memory")},
        {"name": "team", "dir": str(tmp_path / "team")},
        {"name": "org", "dir": str(tmp_path / "org"), "writable": False},
    ])


@pytest.fixture
def config_nodelete(tmp_path: Path):
    """删除封闭（默认配置）环境：验证 delete 未开启、写拒绝路径可用。"""
    for name in ("memory", "team"):
        (tmp_path / name).mkdir()
    return make_config(tmp_path, sources=[
        {"name": "memory", "dir": str(tmp_path / "memory")},
        {"name": "team", "dir": str(tmp_path / "team")},
    ])


def root(config):
    return config.source("memory").dir


# --- 落盘形态 --------------------------------------------------------------

def test_creates_file_under_one_level_category(config):
    written = save_memory(config, "memory", "技术", "机制流程", "# 机制\n\nSecProto 走流程 A。")
    assert written.path == "技术/机制流程.md"
    assert written.created is True and written.replaced_char_count is None
    assert written.absolute == root(config) / "技术" / "机制流程.md"
    assert written.absolute.read_text(encoding="utf-8") == "# 机制\n\nSecProto 走流程 A。\n"


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
    "评审纪要(9月)", "v1.2 发布说明", "[草稿] 方案", "A&B", "{模板}", "a,b", "#1 问题",
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


@pytest.mark.parametrize("source", ["", "   ", None, "不存在", "org"])
def test_missing_unknown_or_readonly_source_is_rejected(config, source, tmp_path):
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    with pytest.raises(WriteError):
        save_memory(config, source, "技术", "文件", "内容")
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before, "被拒绝的写入不得落任何文件"


def test_storage_refuses_readonly_even_if_writer_is_bypassed(config):
    """只读的最后一道闸在 storage 层：绕过 writer 的校验也写不进去。"""
    from storage import open_storage

    with pytest.raises(PermissionError):
        open_storage(config.source("org")).write_text("x.md", "内容")


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
    payload = run_save(config, holder, "org",
                       "技术", "文件", "内容")
    assert payload["saved"] is False and "只读" in payload["error"]
    assert payload["writable_sources"] == ["memory", "team"]
    assert not any(config.source("org").dir.iterdir())


def test_run_save_marks_agent_in_index_and_cache(config):
    """agent 标记并入索引条目，并随缓存持久化——不再有 status.json。"""
    holder = IndexHolder(config)
    holder.build_now()
    run_save(config, holder, "team", "", "记录", "内容")
    assert _wait_for(lambda: ("team", "记录.md") in holder.snapshot.entries and not holder.rebuilding)
    assert holder.snapshot.entries[("team", "记录.md")].editor == "agent"
    assert not (config.config_file.parent / "status.json").exists()

    from index import load_cache
    assert load_cache(config).entries[("team", "记录.md")].editor == "agent", "重启后仍然是 agent"


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


# --- rename：同 source 内改名/移分类 ----------------------------------------

def test_rename_moves_file_within_source(config):
    save_memory(config, "memory", "技术", "旧名", "内容")
    written = rename_memory(config, "memory", "技术/旧名.md", "技术/新名")
    assert written.path == "技术/新名.md"
    assert (root(config) / "技术" / "新名.md").read_text(encoding="utf-8") == "内容\n"
    assert not (root(config) / "技术" / "旧名.md").exists()


def test_rename_moves_between_categories(config):
    save_memory(config, "memory", "技术", "跨类", "内容")
    rename_memory(config, "memory", "技术/跨类.md", "杂记/跨类.md")
    assert (root(config) / "杂记" / "跨类.md").exists()
    assert not (root(config) / "技术" / "跨类.md").exists()


def test_rename_moves_to_source_root(config):
    save_memory(config, "memory", "技术", "从类", "内容")
    written = rename_memory(config, "memory", "技术/从类.md", "裸名")
    assert written.path == "裸名.md"
    assert (root(config) / "裸名.md").exists()
    assert not (root(config) / "技术" / "从类.md").exists()


def test_rename_target_exists_is_rejected(config):
    """rename 的覆盖等于把已有文件直接删掉，必须拒绝而不是 upsert。"""
    save_memory(config, "memory", "技术", "甲", "甲的内容")
    save_memory(config, "memory", "技术", "乙", "乙的内容")
    with pytest.raises(WriteError, match="目标文件已存在"):
        rename_memory(config, "memory", "技术/甲.md", "技术/乙.md")
    assert (root(config) / "技术" / "甲.md").exists()
    assert (root(config) / "技术" / "乙.md").read_text(encoding="utf-8") == "乙的内容\n"


def test_rename_same_path_is_rejected(config):
    save_memory(config, "memory", "技术", "原地", "内容")
    with pytest.raises(WriteError, match="相同"):
        rename_memory(config, "memory", "技术/原地.md", "技术/原地.md")


def test_rename_missing_file_is_rejected(config):
    with pytest.raises(WriteError, match="文件不存在"):
        rename_memory(config, "memory", "技术/没有.md", "技术/别处.md")


def test_rename_rejects_cross_source_paths(config):
    save_memory(config, "memory", "技术", "客の", "内容")
    with pytest.raises(WriteError):
        rename_memory(config, "memory", "技术/客の.md", "team/技术/客の.md")
    assert (config.source("team").dir / "技术" / "客の.md").exists() is False


@pytest.mark.parametrize("new_path", ["../外面", "a/b/c", "", "   ", "CON"])
def test_rename_illegal_new_path_is_rejected(config, new_path):
    save_memory(config, "memory", "技术", "原名", "内容")
    before = sorted(str(p) for p in root(config).rglob("*"))
    with pytest.raises(WriteError):
        rename_memory(config, "memory", "技术/原名.md", new_path)
    assert sorted(str(p) for p in root(config).rglob("*")) == before


def test_rename_to_readonly_source_is_rejected(config):
    save_memory(config, "team", "技术", "团队件", "内容")
    with pytest.raises(WriteError, match="只读"):
        rename_memory(config, "org", "技术/不存在.md", "技术/别处.md")


def test_run_rename_reports_old_and_new_path(config):
    holder = IndexHolder(config)
    holder.build_now()
    run_save(config, holder, "memory", "技术", "待改", "正文")
    payload = run_rename(config, holder, "memory", "技术/待改.md", "技术/已改")
    assert payload["renamed"] is True
    assert payload["old_path"] == "技术/待改.md"
    assert payload["path"] == "技术/已改.md"
    assert payload["index_refresh"] in {"started", "merged"}
    assert _wait_for(lambda: ("memory", "技术/已改.md") in holder.snapshot.entries
                     and not holder.rebuilding), "改名后新路径应可检索"
    payload = run_search(config, holder.snapshot, "正文内容", 5)
    assert payload["results"][0]["path"] == "技术/已改.md"


def test_run_rename_reports_error_without_raising(config):
    holder = IndexHolder(config)
    holder.build_now()
    payload = run_rename(config, holder, "memory", "技术/没有.md", "技术/别处.md")
    assert payload["renamed"] is False
    assert "文件不存在" in payload["error"]
    assert payload["writable_sources"] == ["memory", "team"]


def test_rename_marks_agent_on_new_path(config):
    holder = IndexHolder(config)
    holder.build_now()
    run_save(config, holder, "memory", "技术", "被改名", "内容")
    assert _wait_for(lambda: ("memory", "技术/被改名.md") in holder.snapshot.entries
                     and not holder.rebuilding)
    run_rename(config, holder, "memory", "技术/被改名.md", "技术/改名后")
    assert _wait_for(lambda: ("memory", "技术/改名后.md") in holder.snapshot.entries
                     and not holder.rebuilding)
    assert holder.snapshot.entries[("memory", "技术/改名后.md")].editor == "agent"


# --- replace：字面替换 -------------------------------------------------------

def test_replace_swaps_all_occurrences(config):
    save_memory(config, "memory", "技术", "替换目标", "旧词在前，旧词在中，旧词在后。")
    written = replace_memory(config, "memory", "技术/替换目标.md", "旧词", "新词")
    content = (root(config) / "技术" / "替换目标.md").read_text(encoding="utf-8")
    assert content == "新词在前，新词在中，新词在后。\n"
    assert written.replaced_char_count == 3


def test_replace_requires_exact_match(config):
    save_memory(config, "memory", "技术", "精确", "Learned BLE pairing.")
    with pytest.raises(WriteError, match="命中 0 处"):
        replace_memory(config, "memory", "技术/精确.md", "learned", "掌握")
    assert "Learned" in (root(config) / "技术" / "精确.md").read_text(encoding="utf-8")


def test_replace_empty_new_string_is_rejected(config):
    save_memory(config, "memory", "技术", "删段", "重要内容")
    with pytest.raises(WriteError, match="new_string 不能为空"):
        replace_memory(config, "memory", "技术/删段.md", "重要", "")
    assert (root(config) / "技术" / "删段.md").read_text(encoding="utf-8") == "重要内容\n"


def test_replace_no_hit_leaves_file_untouched(config):
    save_memory(config, "memory", "技术", "未命中", "原文")
    with pytest.raises(WriteError):
        replace_memory(config, "memory", "技术/未命中.md", "不在", "改成")
    assert (root(config) / "技术" / "未命中.md").read_text(encoding="utf-8") == "原文\n"


def test_replace_on_missing_file_is_rejected(config):
    with pytest.raises(WriteError, match="文件不存在"):
        replace_memory(config, "memory", "技术/没有.md", "旧", "新")


def test_replace_with_multiline_string(config):
    save_memory(config, "memory", "技术", "多行", "第一行\n第二行\n第三行")
    replace_memory(config, "memory", "技术/多行.md", "第二行\n第三行", "改写的末两行")
    content = (root(config) / "技术" / "多行.md").read_text(encoding="utf-8")
    assert content == "第一行\n改写的末两行\n"


def test_run_replace_returns_count_and_marks_agent(config):
    holder = IndexHolder(config)
    holder.build_now()
    run_save(config, holder, "memory", "技术", "计处", "一个旧词又一个旧词")
    assert _wait_for(lambda: ("memory", "技术/计处.md") in holder.snapshot.entries
                     and not holder.rebuilding)
    payload = run_replace(config, holder, "memory", "技术/计处.md", "旧词", "新词")
    assert payload["replaced"] is True
    assert payload["replaced_count"] == 2
    assert payload["path"] == "技术/计处.md"
    assert payload["index_refresh"] in {"started", "merged"}
    assert _wait_for(lambda: not holder.rebuilding)
    assert holder.snapshot.entries[("memory", "技术/计处.md")].editor == "agent"


def test_run_replace_reports_error_without_raising(config):
    holder = IndexHolder(config)
    holder.build_now()
    payload = run_replace(config, holder, "memory", "技术/没有.md", "旧", "新")
    assert payload["replaced"] is False
    assert "文件不存在" in payload["error"]
    assert payload["writable_sources"] == ["memory", "team"]


# --- replace：多层目录 -------------------------------------------------------

def test_replace_supports_nested_category_when_file_exists(config):
    """replace 只编辑已有文件，多层分类路径允许到达（文件须人工先建好）。"""
    nested = root(config) / "技术" / "协议" / "HTTP"
    nested.mkdir(parents=True)
    (nested / "要点.md").write_text("HTTP 是无状态的。", encoding="utf-8")
    written = replace_memory(config, "memory", "技术/协议/HTTP/要点.md", "是无状态", "是无状态（短连接）")
    assert written.path == "技术/协议/HTTP/要点.md"
    assert written.absolute == nested / "要点.md"
    assert (nested / "要点.md").read_text(encoding="utf-8") == "HTTP 是无状态（短连接）的。"


def test_replace_supports_deep_and_hidden_directories(config):
    """层级不限，隐藏目录也放行——replace 只编辑人工已建好的文件。"""
    deep = root(config) / "技术" / "知识库私有" / ".工作区" / "深" / "更深" / "底"
    deep.mkdir(parents=True)
    (deep / "便签.md").write_text("一句原文", encoding="utf-8")
    written = replace_memory(config, "memory", "技术/知识库私有/.工作区/深/更深/底/便签.md",
                             "原文", "改后")
    assert written.created is False
    assert (deep / "便签.md").read_text(encoding="utf-8") == "一句改后"


@pytest.mark.parametrize(
    "path",
    [
        "../外面/文件",          # 穿越段
        "技术/../协议/文件",
        "技术/./协议/文件",
        "技术//协议/文件",       # 空段
        "/技术/协议/文件",        # 首段为空
    ],
)
def test_replace_rejects_illegal_nested_category(config, path):
    with pytest.raises(WriteError):
        replace_memory(config, "memory", path, "旧", "新")


def test_replace_nothing_written_outside_root(config, tmp_path):
    """穿越路径被拒绝后，记忆根目录之外不得出现任何新文件。"""
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    for bad in ("../../外面/文件", "技术/../../外面/文件"):
        with pytest.raises(WriteError):
            replace_memory(config, "memory", bad, "旧", "新")
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before


def test_replace_still_rejects_illegal_filename_stem(config):
    """文件名主干仍走一级白名单（这块边界不在本次放宽范围）。"""
    with pytest.raises(WriteError, match="文件名部分非法"):
        replace_memory(config, "memory", "技术/问号?.md", "旧", "新")
    with pytest.raises(WriteError, match="文件名部分非法"):
        replace_memory(config, "memory", "CON.md", "旧", "新")


def test_replace_nested_keeps_save_one_level(config):
    """同一台机器上，save 对同样的嵌套路径仍然拒绝——只有 replace 放开。"""
    with pytest.raises(WriteError, match="分类只有一级"):
        save_memory(config, "memory", "技术/协议", "文件", "内容")


# --- delete：真删 + 断路器 ---------------------------------------------------

def test_delete_removes_the_file(config):
    save_memory(config, "memory", "技术", "待删", "内容。")
    written = delete_memory(config, "memory", "技术/待删.md")
    assert written.path == "技术/待删.md"
    assert not (root(config) / "技术" / "待删.md").exists()


def test_delete_accepts_stem_form(config):
    save_memory(config, "memory", "技术", "后缀宽", "内容。")
    delete_memory(config, "memory", "技术/后缀宽")  # 不带 .md
    assert not (root(config) / "技术" / "后缀宽.md").exists()


def test_delete_missing_file_is_rejected(config):
    with pytest.raises(WriteError, match="文件不存在"):
        delete_memory(config, "memory", "技术/没有.md")


def test_delete_illegal_or_traversal_paths_are_rejected(config):
    save_memory(config, "memory", "技术", "保留", "内容。")
    before = sorted(str(p) for p in root(config).rglob("*"))
    for bad in ("../外面.md", "a/b/c.md", "", ".隐藏/x.md"):
        with pytest.raises(WriteError):
            delete_memory(config, "memory", bad)
    assert sorted(str(p) for p in root(config).rglob("*")) == before


def test_delete_to_readonly_source_is_rejected(config):
    (config.source("org").dir / "制度.md").write_text("制度。", encoding="utf-8")
    with pytest.raises(WriteError, match="只读"):
        delete_memory(config, "org", "制度.md")
    assert (config.source("org").dir / "制度.md").exists()


@pytest.mark.parametrize("unknown", ["", "不存在"])
def test_delete_unknown_source_is_rejected(config, unknown):
    with pytest.raises(WriteError, match="source"):
        delete_memory(config, unknown, "技术/x.md")


def test_delete_is_blocked_by_default(config_nodelete):
    """默认（allow_mcp_delete 缺省）删除必须被拒，文件保持原样。"""
    save_memory(config_nodelete, "memory", "技术", "幸存者", "内容。")
    with pytest.raises(WriteError, match="删除能力未开启"):
        delete_memory(config_nodelete, "memory", "技术/幸存者.md")
    assert (config_nodelete.source("memory").dir / "技术" / "幸存者.md").exists()


def test_run_delete_reports_result_and_refresh(config):
    holder = IndexHolder(config)
    holder.build_now()
    run_save(config, holder, "memory", "技术", "被删改", "唯有正文。")
    assert _wait_for(lambda: ("memory", "技术/被删改.md") in holder.snapshot.entries
                     and not holder.rebuilding)
    payload = run_delete(config, holder, "memory", "技术/被删改.md")
    assert payload["deleted"] is True
    assert payload["path"] == "技术/被删改.md"
    assert payload["index_refresh"] in {"started", "merged"}
    assert _wait_for(lambda: ("memory", "技术/被删改.md") not in holder.snapshot.entries
                     and not holder.rebuilding), "刷新后删除的条目必须从索引里消失"
    assert not (root(config) / "技术" / "被删改.md").exists()


def test_run_delete_reports_error_without_raising(config):
    holder = IndexHolder(config)
    holder.build_now()
    payload = run_delete(config, holder, "memory", "技术/没有.md")
    assert payload["deleted"] is False
    assert "文件不存在" in payload["error"]
    assert payload["writable_sources"] == ["memory", "team"]


def test_run_delete_blocked_when_capability_off(config_nodelete):
    holder = IndexHolder(config_nodelete)
    holder.build_now()
    save_memory(config_nodelete, "memory", "技术", "定存", "内容。")
    payload = run_delete(config_nodelete, holder, "memory", "技术/定存.md")
    assert payload["deleted"] is False
    assert "删除能力未开启" in payload["error"]
    assert (config_nodelete.source("memory").dir / "技术" / "定存.md").exists()
