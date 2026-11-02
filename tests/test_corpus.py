"""corpus 层单测：切块边界、扫描过滤、指纹变更检测。"""

import json
import os
from pathlib import Path

import pytest

import corpus
from config import Config, ConfigError


def make_config(root: Path, **overrides) -> Config:
    """在 root 下写一份 config.json 并加载它。

    默认只有一个读写 source `memory` → root/memory。
    overrides 用 config.json 的字段名；为少改旧用例，也接受 MEMORY_XXX 形式。
    """
    data = {
        "host": "127.0.0.1",
        "port": 7099,
        "poll_interval": 0,
        "sources": [{"name": "memory", "dir": str(root / "memory")}],
    }
    for key, value in overrides.items():
        if key.startswith("MEMORY_"):
            key = key[len("MEMORY_"):].lower()
            value = int(value) if str(value).lstrip("-").isdigit() else value
        data[key] = value
    path = root / "config.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return Config.load(path, create_default=False)


@pytest.fixture
def sample_root(tmp_path: Path) -> Path:
    root = tmp_path / "memory"
    (root / "sub").mkdir(parents=True)
    (root / "a.md").write_text("x" * 1000, encoding="utf-8")
    (root / "sub" / "b.txt").write_text("hello", encoding="utf-8")
    (root / "sub" / "空 格 文件.md").write_text("有空格的文件名", encoding="utf-8")
    (root / "ignored.csv").write_text("a,b,c", encoding="utf-8")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "junk.md").write_text("工具产物", encoding="utf-8")
    return tmp_path


# --- split_text 边界 -------------------------------------------------------

def test_split_empty_text():
    assert corpus.split_text("", size=800, step=680) == []


def test_split_blank_only_text():
    assert corpus.split_text("   \n\n  ", size=800, step=680) == []


def test_split_shorter_than_window():
    pieces = corpus.split_text("abc", size=800, step=680)
    assert pieces == [(0, 3, "abc")]


def test_split_exactly_one_window():
    text = "x" * 800
    pieces = corpus.split_text(text, size=800, step=680)
    assert len(pieces) == 1
    assert pieces[0][:2] == (0, 800)


def test_split_covers_whole_text_with_overlap():
    text = "".join(str(i % 10) for i in range(2500))
    pieces = corpus.split_text(text, size=800, step=680)
    assert pieces[0][0] == 0
    assert pieces[-1][1] == len(text), "最后一块必须覆盖到原文结尾"
    for start, end, piece in pieces:
        assert text[start:end] == piece, "偏移量必须能还原原文片段"
    starts = [p[0] for p in pieces]
    assert starts == sorted(starts) and len(set(starts)) == len(starts)


def test_split_no_redundant_tail_chunk():
    """尾部不应产生一个完全被前一块覆盖的冗余小块。"""
    text = "y" * 1400
    pieces = corpus.split_text(text, size=800, step=680)
    assert len(pieces) == 2
    assert pieces[-1][1] == 1400


# --- 扫描过滤（经 storage + index.build）-----------------------------------------

def _keys(config):
    import index
    return {key for key in index.build(config).documents}


def test_scan_filters_extensions_and_hidden(sample_root: Path):
    paths = {path for _, path in _keys(make_config(sample_root))}
    assert paths == {"a.md", "sub/b.txt", "sub/空 格 文件.md"}
    assert not any("/." in p for p in paths), "隐藏目录下的文件必须被排除"


def test_scan_case_insensitive_extension(sample_root: Path):
    (sample_root / "memory" / "UPPER.MD").write_text("大写扩展名", encoding="utf-8")
    assert ("memory", "UPPER.MD") in _keys(make_config(sample_root))


def test_contents_and_chunk_offsets_map_back_to_source(sample_root: Path):
    import index
    snapshot = index.build(make_config(sample_root))
    assert snapshot.documents[("memory", "a.md")].char_count == 1000
    assert snapshot.contents[("memory", "sub/b.txt")] == "hello"
    assert {c.key for c in snapshot.chunks} == set(snapshot.contents)
    for chunk in snapshot.chunks:
        piece = snapshot.contents[chunk.key][chunk.char_start : chunk.char_end]
        assert piece.strip() and len(piece) == chunk.char_end - chunk.char_start


def test_config_rejects_overlap_ge_chunk_size(sample_root: Path):
    with pytest.raises(ConfigError):
        make_config(sample_root, MEMORY_CHUNK_SIZE="200", MEMORY_CHUNK_OVERLAP="200")


# --- 多 source ----------------------------------------------------------

def test_scan_covers_every_source_with_relative_paths(sample_root: Path):
    """同名 path 可以出现在不同 source 中，身份是 (source, path)。"""
    team = sample_root / "team"
    team.mkdir()
    (team / "a.md").write_text("团队的 a", encoding="utf-8")
    config = make_config(sample_root, sources=[
        {"name": "memory", "dir": str(sample_root / "memory")},
        {"name": "team", "dir": str(team), "writable": False},
    ])
    keys = _keys(config)
    assert ("memory", "a.md") in keys and ("team", "a.md") in keys
    assert all(not path.startswith(("memory/", "team/")) for _, path in keys), "path 不拼接 source 名"


# --- 相似词头（ADR-0037 · front matter 形态）------------------------------------

def test_parse_similar_terms_block_basic():
    block = corpus.parse_similar_terms_block(
        "---\nsimilar_terms: replay nonce 重放攻击\n---\n\n正文")
    assert block.terms == ("replay", "nonce", "重放攻击")
    assert block.block_len == len("---\nsimilar_terms: replay nonce 重放攻击\n---\n")
    assert block.key_start == len("---\n")
    assert block.key_len == len("similar_terms: replay nonce 重放攻击\n")


def test_parse_similar_terms_block_tolerates_bom_fullwidth_case():
    """BOM、全角冒号、大小写都在容忍范围——认得出比挑写法重要。"""
    assert corpus.parse_similar_terms_block(
        "\ufeff---\n相似词：重放 攻击\n---\n正文").terms == ("重放", "攻击")
    assert corpus.parse_similar_terms_block(
        "---\nSIMILAR_TERMS: Alpha\n---\nx").terms == ("Alpha",)
    assert corpus.parse_similar_terms_block(
        "---\nSimilar_Terms:alpha\n---\nx").terms == ("alpha",)


def test_parse_similar_terms_block_dedupes_case_insensitively():
    assert corpus.parse_similar_terms_block(
        "---\nsimilar_terms: ALPHA alpha Beta\n---\n\nx").terms == ("ALPHA", "Beta")


def test_parse_similar_terms_block_none_cases():
    """没有围栏块一律 None；旧平文首行形态已废弃、不再兼容。"""
    assert corpus.parse_similar_terms_block("") is None
    assert corpus.parse_similar_terms_block("正文\nsimilar_terms: alpha\n") is None
    assert corpus.parse_similar_terms_block("similar_terms 没有 冒号\n") is None
    assert corpus.parse_similar_terms_block("keywords: alpha\n") is None
    assert corpus.parse_similar_terms_block(
        "similar_terms: 旧版平文\n\n正文") is None, "0.8.0 平文形态不认"
    # 围栏块自身不合法也不认
    assert corpus.parse_similar_terms_block("---\nsimilar_terms: 甲\n\n正文") is None
    assert corpus.parse_similar_terms_block("---\n\nsimilar_terms: 甲\n---\n\n正文") is None


def test_parse_similar_terms_block_without_key_still_a_block():
    """块内没有该键仍然是合法围栏块：写端往里补键，索引不遮任何东西。"""
    block = corpus.parse_similar_terms_block("---\ndescription: 介绍\n---\n\n正文")
    assert block.terms == () and block.key_start is None
    assert block.block_len == len("---\ndescription: 介绍\n---\n")
    # 键写在闭合线之后不算块内内容
    loose = corpus.parse_similar_terms_block("---\n---\nsimilar_terms: 甲\n\n正文")
    assert loose.terms == () and loose.key_start is None


def test_format_similar_terms_line_roundtrip():
    line = corpus.format_similar_terms_line(("replay", "重放"))
    assert line == "similar_terms: replay 重放"
    assert corpus.parse_similar_terms_block(f"---\n{line}\n---\n\nx").terms == ("replay", "重放")


def test_assemble_creates_block_when_none():
    assert corpus.assemble_similar_terms_header(("词",), "正文") == (
        "---\nsimilar_terms: 词\n---\n\n正文")


def test_assemble_appends_key_into_existing_block():
    """块内已有其它键：参数行补作块内最后一行，其它键一字不动。"""
    body = "---\nname: 标题\ndescription: 介绍\n---\n\n正文"
    assert corpus.assemble_similar_terms_header(("新词",), body) == (
        "---\nname: 标题\ndescription: 介绍\nsimilar_terms: 新词\n---\n\n正文")


def test_assemble_replaces_key_in_place_keeps_others():
    """块内已有相似词键：参数行原位替换；块内其它键与正文一字不动。"""
    body = "---\nname: 标题\nsimilar_terms: 旧词\ndescription: 介绍\n---\n\n正文"
    assert corpus.assemble_similar_terms_header(("新词",), body) == (
        "---\nname: 标题\nsimilar_terms: 新词\ndescription: 介绍\n---\n\n正文")


def test_assemble_drops_duplicate_key_lines():
    assert corpus.assemble_similar_terms_header(("新",), (
        "---\na: 1\nsimilar_terms: 旧\nb: 2\nsimilar_terms: 再\n---\n\nx")) == (
        "---\na: 1\nsimilar_terms: 新\nb: 2\n---\n\nx")


def test_assemble_strips_bom():
    out = corpus.assemble_similar_terms_header(("词",), "\ufeff正文")
    assert out == "---\nsimilar_terms: 词\n---\n\n正文"


# --- 头部键遮罩规划（ADR-0037 · 全部短键值同通道 + 总预算）------------------------

def test_plan_masks_similar_terms_line_and_other_short_keys():
    """similar_terms 行必遮；其它短键值按长度短的优先一并遮罩进通道。"""
    plan = corpus.plan_front_matter(
        "---\ntitle: Node.js 升级\nsimilar_terms: replay 重放\ntags: a b c\n---\n\n正文")
    assert plan.mask_spans == ((4, len("title: Node.js 升级\n")),
                               (22, len("similar_terms: replay 重放\n")),
                               (47, len("tags: a b c\n")))
    # similar_terms 词组在前，其余键值按"短的优先"次序随后
    assert plan.values == ("replay", "重放", "a b c", "Node.js 升级")


def test_plan_budget_128_shortest_first():
    """总预算 128 字符：按值长度升序装入，装不下的整行留正文。"""
    small = "甲词" * 10        # 20
    big = "乙词兜底" * 25     # 100
    huge = "超长往下留" * 6   # 30：20+30=50 先装，100 再装 → 150 > 128
    plan = corpus.plan_front_matter(
        f"---\ntags: {small}\ncategory: {big}\nseries: {huge}\n"
        "similar_terms: 一定要进\n---\n\n正文")
    assert plan.values == ("一定要进", small, huge), "短的优先：20、30 装入，100 装不下"
    offset_big = len("---\n") + len(f"tags: {small}\n")
    assert offset_big not in {s for s, _ in plan.mask_spans}, "装不下的键值整行留正文，不遮罩"


def test_plan_overlong_single_value_stays_in_body(tmp_path=None):
    """单值超长（>128）不进通道不遮罩；空值键遮键名但不占预算。"""
    plan = corpus.plan_front_matter(
        "---\nbig: " + "超" * 129 + "\nempty:\ntitle: 标题\n---\n\n正文")
    assert plan.values == ("标题",)
    # empty 与 title 被遮，big 没有
    spans = plan.mask_spans
    assert len(spans) == 2
    assert any(l == len("empty:\n") for _, l in spans)
    assert any(l == len("title: 标题\n") for _, l in spans)


def test_plan_duplicate_similar_terms_lines_all_masked():
    """多处相似词行全部遮罩；词组取去重保序后的第一组。"""
    plan = corpus.plan_front_matter(
        "---\nsimilar_terms: 甲\ntitle: 标题\n相似词：乙\n---\n\n正文")
    assert len(plan.mask_spans) == 3
    assert plan.values == ("甲", "标题"), "第二处相似词行的词不重复进通道"


def test_plan_no_block_or_empty_block():
    """无围栏块 → None；围栏块内没有键 → 空规划（遮罩与通道都为空）。"""
    assert corpus.plan_front_matter("正文") is None
    plan = corpus.plan_front_matter("---\n---\n\n正文")
    assert plan.mask_spans == () and plan.values == ()


# --- 0.8.0 调整：剔除布尔/纯数字、引号、YAML 列表（ADR-0037）----------------------

def test_plan_drops_booleans_and_numbers_but_masks_their_lines():
    """布尔与纯数字（含小数点、正负号）不进通道、不占预算，整行仍遮罩。"""
    content = ("---\nimport_body: false\nflag: YES\nnil: null\ndepth: 1\n"
               "ts: \"1738978466000\"\nratio: -1.5\nver: +.5\ntitle: 标题\n---\n\n正文")
    plan = corpus.plan_front_matter(content)
    assert plan.values == ("标题",)
    assert len(plan.mask_spans) == 8, "垃圾值所在行也遮掉：不进 chunk 0 正文"


def test_plan_drops_urls_like_numbers_and_booleans():
    """整值是 http(s) URL 的同样剔除：不进通道、不占预算，整行遮罩；列表逐项剔除。"""
    content = ("---\ndoc_url: \"https://alidocs.dingtalk.com/i/nodes/7NkD?utm_scene=team_space\"\n"
               "home: HTTP://Example.com\nlinks:\n  - https://a.example/x\n  - 官网\n"
               "title: 标题\n---\n\n正文")
    plan = corpus.plan_front_matter(content)
    assert plan.values == ("官网", "标题")
    assert len(plan.mask_spans) == 4, "doc_url、home 行整行遮掉，links 整组遮掉"


def test_plan_keeps_values_merely_containing_urls():
    """只剔整值为 URL 的：夹在文字里的、非 http(s) 协议的照常进通道。"""
    plan = corpus.plan_front_matter(
        "---\na: 见 https://x.example\nb: ftp://files.example\nc: example.com\n---\n\n正文")
    assert set(plan.values) == {"见 https://x.example", "ftp://files.example", "example.com"}


def test_plan_keeps_mixed_and_short_meaningful_values():
    """只剔布尔与纯数字：短中文、版本号、带字母的数字照常进通道。"""
    plan = corpus.plan_front_matter(
        "---\na: 蓝牙\nb: 1.2.3\nc: 5G\nd: v1\ne: falsey\n---\n\n正文")
    assert set(plan.values) == {"蓝牙", "1.2.3", "5G", "v1", "falsey"}


def test_plan_strips_surrounding_quotes():
    """值两端的引号先去掉再判断、再计长度、再进通道。"""
    plan = corpus.plan_front_matter(
        "---\ntitle: \"Android 绑定\"\nkind: 'adoc'\nflag: \"false\"\n---\n\n正文")
    assert plan.values == ("adoc", "Android 绑定")


def test_plan_block_list_is_one_candidate():
    """块列表整组算一个候选：项用空格拼接计长度，整组进或整组不进，键行连同列表项一起遮。"""
    content = "---\ntags:\n  - 蓝牙\n  - \"BLE\"\n- 3\naliases:\n  - 别名\n---\n\n正文"
    plan = corpus.plan_front_matter(content)
    assert plan.values == ("别名", "蓝牙 BLE"), "纯数字项逐项剔除；短的优先"
    tags_start = len("---\n")
    tags_len = len("tags:\n  - 蓝牙\n  - \"BLE\"\n- 3\n")
    assert (tags_start, tags_len) in plan.mask_spans


def test_plan_block_list_over_budget_stays_whole_in_body():
    big = "乙词兜底" * 20  # 80
    content = f"---\na:\n  - {big}\n  - {big}\n---\n\n正文"
    plan = corpus.plan_front_matter(content)
    assert plan.values == () and plan.mask_spans == (), "160 > 128：整组留正文"


def test_plan_block_list_all_junk_masks_without_budget():
    plan = corpus.plan_front_matter("---\nflags:\n  - true\n  - 0\ntitle: 标题\n---\n\n正文")
    assert plan.values == ("标题",)
    assert plan.mask_spans[0] == (4, len("flags:\n  - true\n  - 0\n"))


def test_plan_inline_list():
    plan = corpus.plan_front_matter(
        "---\ntags: [蓝牙, \"BLE\", 'GATT', 42, false]\n---\n\n正文")
    assert plan.values == ("蓝牙 BLE GATT",)
    assert len(plan.mask_spans) == 1


def test_plan_list_items_without_key_are_left_alone():
    """不挂在空值键下面的列表项、缩进的嵌套映射行都不碰（宁可少认）。"""
    content = "---\ntitle: 标题\n  - 游离\nmetadata:\n  author: 某人\n---\n\n正文"
    plan = corpus.plan_front_matter(content)
    assert plan.values == ("标题",)
    assert len(plan.mask_spans) == 2, "title 行与 metadata 空值键行；游离项和子键行留正文"


def test_parse_similar_terms_block_list():
    content = "---\nsimilar_terms:\n  - replay\n  - \"防 重放\"\n  - Replay\n---\n\n正文"
    block = corpus.parse_similar_terms_block(content)
    assert block.terms == ("replay", "防", "重放")
    assert block.key_start == 4
    assert block.key_len == len("similar_terms:\n  - replay\n  - \"防 重放\"\n  - Replay\n")


def test_parse_similar_terms_inline_list():
    block = corpus.parse_similar_terms_block("---\n相似词: [replay, 'nonce', 重放]\n---\n\nx")
    assert block.terms == ("replay", "nonce", "重放")


def test_similar_terms_not_filtered_as_junk():
    """相似词是调用方显式给的：纯数字也照收（只有其它键做剔除）。"""
    plan = corpus.plan_front_matter("---\nsimilar_terms: 802 true\n---\n\n正文")
    assert plan.values == ("802", "true")


def test_plan_similar_terms_list_masked_whole():
    content = "---\nsimilar_terms:\n  - 甲\n  - 乙\ntitle: 标题\n---\n\n正文"
    plan = corpus.plan_front_matter(content)
    assert plan.values == ("甲", "乙", "标题")
    assert (4, len("similar_terms:\n  - 甲\n  - 乙\n")) in plan.mask_spans


def test_assemble_replaces_list_form_with_single_line():
    body = "---\nname: 标题\nsimilar_terms:\n  - 旧甲\n  - 旧乙\ntags: [a]\n---\n\n正文"
    assert corpus.assemble_similar_terms_header(("新词",), body) == (
        "---\nname: 标题\nsimilar_terms: 新词\ntags: [a]\n---\n\n正文")


def test_assemble_appends_after_trailing_list():
    body = "---\ntags:\n  - a\n---\n\n正文"
    assert corpus.assemble_similar_terms_header(("词",), body) == (
        "---\ntags:\n  - a\nsimilar_terms: 词\n---\n\n正文")


def test_front_matter_still_limited_to_20_lines():
    """20 行上限不变：列表让头部变长时，超限的整块不认。"""
    items = "".join(f"  - 词{i}\n" for i in range(20))
    assert corpus.parse_similar_terms_block(f"---\ntags:\n{items}---\n\n正文") is None


# --- 覆盖时头部按键合并（ADR-0037 · 0.8.0 定稿调整）------------------------------

def test_merge_keeps_old_header_when_new_has_none():
    old = "---\ntitle: 旧标题\ntags: [蓝牙]\nsimilar_terms: 旧词\n---\n\n旧正文\n"
    assert corpus.merge_front_matter(old, "新正文") == (
        "---\ntitle: 旧标题\ntags: [蓝牙]\nsimilar_terms: 旧词\n---\n\n新正文")


def test_merge_by_key_replaces_in_place_and_appends_new():
    old = "---\ntitle: 旧标题\ntags: [蓝牙]\nsimilar_terms: 旧词\n---\n\n旧正文\n"
    new = "---\ntitle: 新标题\nauthor: 某人\nsimilar_terms: 内容自带\n---\n\n新正文"
    assert corpus.merge_front_matter(old, new) == (
        "---\ntitle: 新标题\ntags: [蓝牙]\nsimilar_terms: 旧词\nauthor: 某人\n---\n\n新正文")


def test_merge_replaces_whole_group_with_list_items_and_children():
    """同名键整组替换：旧键名下的列表项、缩进子行随旧键一起换掉。"""
    old = ("---\ntags:\n  - 甲\n  - 乙\nmetadata:\n  author: 旧人\n"
           "keep: 留着\n---\n\n旧正文")
    new = "---\ntags: [丙]\nmetadata:\n  author: 新人\n---\n\n新正文"
    assert corpus.merge_front_matter(old, new) == (
        "---\ntags: [丙]\nmetadata:\n  author: 新人\nkeep: 留着\n---\n\n新正文")


def test_merge_key_names_case_insensitive_and_old_duplicates_dropped():
    old = "---\nTitle: 一\nother: 二\ntitle: 三\n---\n\n旧"
    assert corpus.merge_front_matter(old, "---\ntitle: 新\n---\n\n新") == (
        "---\ntitle: 新\nother: 二\n---\n\n新")


def test_merge_without_old_header_returns_new_unchanged():
    new = "---\ntitle: 新\n---\n\n新正文"
    assert corpus.merge_front_matter("旧正文没有头部\n", new) == new
    assert corpus.merge_front_matter("", "新正文") == "新正文"


def test_merge_strips_bom_from_both():
    old = "\ufeff---\ntitle: 旧\n---\n旧"
    assert corpus.merge_front_matter(old, "\ufeff新正文") == "---\ntitle: 旧\n---\n\n新正文"
