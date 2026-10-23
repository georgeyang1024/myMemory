"""关联图（ADR-0035）：[[链接]] 抽取、同 source 解析、search 附 related、开关与工具说明。"""

import asyncio
import os
from pathlib import Path

import pytest

import index
from config import Config, ConfigError
from index import IndexHolder, build_link_graph, extract_links
from server import create_server, run_search

from test_corpus import make_config


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --- 抽取 -------------------------------------------------------------------

def test_extract_links_strips_alias_heading_and_dedupes():
    text = "见 [[方案A|别名]]、[[方案A#结论]]、![[技术/方案B]] 与 [[ 方案C.md ]]，[[#本文标题]] 不算。"
    assert extract_links(text) == ("方案A", "技术/方案B", "方案C.md")


def test_extract_links_ignores_cross_line_brackets():
    assert extract_links("[[断\n开]]") == ()


def test_links_are_recorded_even_when_wikilinks_are_stripped(tmp_path: Path):
    write(tmp_path / "memory" / "a.md", "引用 [[b]]")
    write(tmp_path / "memory" / "b.md", "被引用")
    snapshot = index.build(make_config(tmp_path))  # strip_wikilinks 默认开
    assert snapshot.entries[("memory", "a.md")].links == ("b",)


# --- 解析 -------------------------------------------------------------------

@pytest.fixture
def graph_root(tmp_path: Path) -> Path:
    mem = tmp_path / "memory"
    write(mem / "工作" / "周报.md", "本周推进 [[方案]]，参考 [[技术/接口规范]] 与 [[不存在的文档]]，自引 [[周报]]。")
    write(mem / "技术" / "方案.md", "方案正文。")
    write(mem / "技术" / "接口规范.md", "接口正文。")
    write(mem / "归档" / "深层" / "方案.md", "同名但路径更长。")
    write(mem / "笔记.txt", "提到 [[工作/周报]]。")
    write(tmp_path / "team" / "方案.md", "团队方案，链接 [[周报]]（个人 source 的文档）。")
    return tmp_path


def graph_config(root: Path, **overrides) -> Config:
    return make_config(root, sources=[
        {"name": "memory", "dir": str(root / "memory")},
        {"name": "team", "dir": str(root / "team")},
    ], **overrides)


def test_graph_resolves_within_source_shortest_path_first(graph_root: Path):
    graph = build_link_graph(index.build(graph_config(graph_root)).entries)
    # 同名"方案"取路径最短的 技术/方案.md；断链与自引丢弃
    assert graph[("memory", "工作/周报.md")] == ("技术/接口规范.md", "技术/方案.md", "笔记.txt")


def test_graph_is_undirected(graph_root: Path):
    graph = build_link_graph(index.build(graph_config(graph_root)).entries)
    assert graph[("memory", "技术/方案.md")] == ("工作/周报.md",)
    assert graph[("memory", "笔记.txt")] == ("工作/周报.md",)


def test_graph_never_crosses_sources(graph_root: Path):
    graph = build_link_graph(index.build(graph_config(graph_root)).entries)
    assert ("team", "方案.md") not in graph  # [[周报]] 在 team 内解析不到
    assert all(src == "memory" for src, _ in graph)


def test_resolution_is_case_insensitive(tmp_path: Path):
    write(tmp_path / "memory" / "Notes" / "BLE.md", "x")
    write(tmp_path / "memory" / "a.md", "[[ble]] [[notes/Ble.md]]")
    graph = build_link_graph(index.build(make_config(tmp_path)).entries)
    assert graph[("memory", "a.md")] == ("Notes/BLE.md",)


def test_vault_root_prefix_is_stripped(tmp_path: Path):
    """Obsidian 仓库根在 source 目录之上：[[raw/分类/文件]] 仍能解析到 分类/文件.md。"""
    write(tmp_path / "memory" / "会议" / "26-08-04" / "纪要.md", "x")
    write(tmp_path / "memory" / "a.md", "[[raw/会议/26-08-04/纪要]]")
    graph = build_link_graph(index.build(make_config(tmp_path)).entries)
    assert graph[("memory", "a.md")] == ("会议/26-08-04/纪要.md",)


# --- 抽样：至多 3 篇，稳定，文档更新才变 ---------------------------------------

@pytest.fixture
def hub_root(tmp_path: Path) -> Path:
    mem = tmp_path / "memory"
    write(mem / "中心.md", "中心文档 " + " ".join(f"[[叶{i}]]" for i in range(10)))
    for i in range(10):
        write(mem / f"叶{i}.md", f"叶子 {i}")
    return tmp_path


def test_related_caps_at_three_and_is_stable(hub_root: Path):
    config = make_config(hub_root)
    first = index.build(config).related("memory", "中心.md")
    assert len(first) == 3
    assert set(first) <= {f"叶{i}.md" for i in range(10)}
    # 重建快照（模拟重启）不变
    assert index.build(config).related("memory", "中心.md") == first


def test_related_resamples_when_document_changes(hub_root: Path):
    config = make_config(hub_root)
    picks = set()
    target = hub_root / "memory" / "中心.md"
    for n in range(8):
        target.write_text(target.read_text(encoding="utf-8") + " ", encoding="utf-8")
        os.utime(target, (1_700_000_000 + n, 1_700_000_000 + n))
        picks.add(tuple(index.build(config).related("memory", "中心.md")))
    assert len(picks) > 1


def test_related_unknown_or_unlinked_document_is_empty(hub_root: Path):
    snapshot = index.build(make_config(hub_root))
    assert snapshot.related("memory", "不存在.md") == []
    assert snapshot.related("nope", "中心.md") == []


# --- search 输出与开关 ---------------------------------------------------------

def test_search_hits_carry_related(graph_root: Path):
    config = graph_config(graph_root)
    payload = run_search(config, index.build(config), "接口正文", 5)
    hit = next(r for r in payload["results"] if r["path"] == "技术/接口规范.md")
    assert hit["related"] == ["工作/周报.md"]


def test_search_hits_without_links_omit_related(graph_root: Path):
    config = graph_config(graph_root)
    payload = run_search(config, index.build(config), "团队方案", 5, source="team")
    assert payload["results"] and all("related" not in r for r in payload["results"])


def test_switch_off_removes_related(graph_root: Path):
    config = graph_config(graph_root, link_graph=False)
    payload = run_search(config, index.build(config), "接口正文", 5)
    assert payload["results"] and all("related" not in r for r in payload["results"])


def test_link_graph_defaults_on_and_must_be_boolean(tmp_path: Path):
    (tmp_path / "memory").mkdir()
    assert make_config(tmp_path).link_graph is True
    assert make_config(tmp_path).describe()["link_graph"] is True
    with pytest.raises(ConfigError, match="link_graph"):
        make_config(tmp_path, link_graph="yes")


@pytest.mark.parametrize("enabled", [True, False])
def test_tool_descriptions_follow_switch(tmp_path: Path, enabled: bool):
    write(tmp_path / "memory" / "a.md", "x")
    config = make_config(tmp_path, link_graph=enabled)
    holder = IndexHolder(config)
    holder.build_now()
    tools = {t.name: t for t in asyncio.run(create_server(config, holder).list_tools())}
    assert ("[[分类/文件名.md]]" in tools["save"].description) is enabled
    assert ("related" in tools["search"].description) is enabled
