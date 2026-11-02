"""语料层：文档与检索单元的数据形态，以及固定窗口切块。

本模块不认识 BM25，也不认识 HTTP，也不碰磁盘。扫描与读取经 storage 层，
按文件增量处理在 index.py（见 docs/adr/0019-index-cache-incremental.md）。

切块策略：统一固定窗口，不做任何结构解析。
理由与取舍见 docs/adr/0005-fixed-window-chunking.md。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DocKey = tuple[str, str]  # (source, path)——一篇记忆的唯一身份


# --- 相似词头（ADR-0037 · 修订：front matter 形态，形态与 skill metadata 一致）----
# 文件头部的 front matter（--- 围栏块）里的一行 metadata：
#   ---
#   similar_terms: 词1 词2 ……
#   ---
# 同义词/近似词/英文缩写/简写由写入端 LLM 沉淀，随文件走、人可改——代表整篇文档的
# 额外检索词，与 path_tokens 同构。本层只做"识别与解析"，不认识 BM25，不碰磁盘。

# 认英文键 similar_terms 与中文键 相似词：大小写不敏感、容忍全角冒号。
# 其他变体不认——键是给机器的契约，不是给人猜的。
_SIMILAR_TERMS_KEY = re.compile(
    r"(?:similar_terms|相似词)\s*[:：]\s*(.*)$", re.IGNORECASE)

# 块内通用键：无空格、无冒号的键段 + 冒号 + 值（front matter 每行一个键）。
# similar_terms/相似词 由上面的专用正则先行接管，这里只兜其余键。
_GENERIC_KEY = re.compile(r"([^\s:：]{1,64})\s*[:：]\s*(.*)$", re.IGNORECASE)

# YAML 块列表项：`- 值`（缩进可有可无）；只认挂在空值键下面、紧随其后的连续项。
_LIST_ITEM = re.compile(r"\s*-(?:\s+(.*))?$")

# front matter 围栏：独立成行、臂上不带任何内容的 `---`。
_FM_FENCE = re.compile(r"---\s*$")
# 闭合线只往前找这么多行；块内出现空行或找满仍未闭合 → 不算 front matter。
_MAX_FM_LINES = 20
# 除 similar_terms 外，其它键值进检索词通道的总预算（字符）——按长度升序装满为止。
# 通道进每个 chunk，预算即每个 chunk 的固定附加量，宜小。
_FM_BUDGET = 128

# 其它键里没有检索价值的值：布尔/空值字面量、纯数字（含小数点、正负号）、
# 整值为 http(s) URL（拆词后是 https/com/域名/ID 一类噪声）。
# 不进通道、不占预算，所在行照样遮罩（不留在 chunk 0 正文里）。
_JUNK_WORDS = frozenset({"true", "false", "yes", "no", "on", "off", "null"})
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")
_URL = re.compile(r"https?://\S+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class SimilarTermsBlock:
    """头部相似词的解析结果；字符偏移相对传入的 content（BOM 计入首行）。

    terms：词组（按空白拆分、大小写不敏感去重保序）；
    block_len：头部围栏块总长 = BOM + 起始行到闭合行末尾；
    key_start/key_len：相似词键在 content 里占的区间——单行形态是键行本身，
        YAML 块列表形态含紧随其后的全部列表项行；确有围栏块但块内没有该键
        → None（写端往块里补键，索引对这种块不遮罩任何内容）。
    """

    terms: tuple[str, ...]
    block_len: int
    key_start: int | None
    key_len: int | None


@dataclass(frozen=True, slots=True)
class _FrontMatterEntry:
    """围栏块内的一个顶层键：区间覆盖键行与它名下的列表项行。"""

    start: int
    length: int
    similar: bool             # 是否 similar_terms / 相似词 键
    items: tuple[str, ...]    # 去引号后的值：标量 1 项（空值为 ()），列表逐项


def _dedupe_terms(raw: str) -> tuple[str, ...]:
    seen: set[str] = set()
    terms: list[str] = []
    for word in raw.split():
        fold = word.casefold()
        if fold not in seen:
            seen.add(fold)
            terms.append(word)
    return tuple(terms)


def _unquote(text: str) -> str:
    """去掉值两端成对的引号（YAML 标量常见写法）。"""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1].strip()
    return text


def _is_junk_value(value: str) -> bool:
    return (value.casefold() in _JUNK_WORDS or _NUMBER.fullmatch(value) is not None
            or _URL.fullmatch(value) is not None)


def _scalar_or_inline_list(raw: str) -> tuple[str, ...]:
    """键行上的值：`[a, b]` 行内列表拆成多项，否则是一个标量；空值为 ()。"""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == "[" and raw[-1] == "]":
        return tuple(item for item in (_unquote(part) for part in raw[1:-1].split(","))
                     if item)
    value = _unquote(raw)
    return (value,) if value else ()


def _fence_block_len(content: str) -> int | None:
    """头部合法围栏块的总长（含 BOM 与闭合行）；不是合法块 → None。

    闭合线缺失、块内出现空行、找满 20 行仍未闭合都不算——宁可少认，不可认错：
    认错一段正文 header 就会被写端重排。
    """
    if not content:
        return None
    lines = content.splitlines(keepends=True)
    first = lines[0]
    bom = 1 if first.startswith("﻿") else 0
    if not _FM_FENCE.match(first[bom:].rstrip("\r\n")):
        return None
    for i in range(1, min(len(lines), _MAX_FM_LINES + 1)):
        stripped = lines[i].rstrip("\r\n")
        if not stripped.strip():
            return None  # 块内空行：不是合法 front matter
        if _FM_FENCE.match(stripped):
            return sum(len(line) for line in lines[: i + 1])
    return None


def _scan_front_matter(content: str, block_len: int) -> tuple[list[_FrontMatterEntry], int]:
    """逐行扫描围栏块内部，返回顶层键条目与闭合行的起始偏移。

    键行值为空时，紧随其后的连续 `- 值` 行归它（YAML 块列表）；
    不挂在空值键下的游离列表项、缩进的嵌套映射行一律不碰。
    """
    lines = content[:block_len].splitlines(keepends=True)
    starts: list[int] = []
    offset = 0
    for line in lines:
        starts.append(offset)
        offset += len(line)
    close = len(lines) - 1
    entries: list[_FrontMatterEntry] = []
    i = 1
    while i < close:
        stripped = lines[i].rstrip("\r\n")
        similar = _SIMILAR_TERMS_KEY.match(stripped)
        match = similar or _GENERIC_KEY.match(stripped)
        if match is None:
            i += 1
            continue
        raw = match.group(1) if similar else match.group(2)
        first, length = i, len(lines[i])
        i += 1
        items = _scalar_or_inline_list(raw)
        if not raw.strip():
            listed: list[str] = []
            while i < close and (item := _LIST_ITEM.match(lines[i].rstrip("\r\n"))):
                value = _unquote(item.group(1) or "")
                if value:
                    listed.append(value)
                length += len(lines[i])
                i += 1
            items = tuple(listed)
        entries.append(_FrontMatterEntry(start=starts[first], length=length,
                                         similar=similar is not None, items=items))
    return entries, starts[close]


def parse_similar_terms_block(content: str) -> SimilarTermsBlock | None:
    """解析文件头部 front matter（`---` 围栏块）里的相似词键。

    形态固定：文件从头（容忍 BOM）起 `---` 开、`---` 闭的围栏块，块内任一顶层键
    形如 `similar_terms: …` / `相似词：…`——值可以是空格分隔的单行、
    `[a, b]` 行内列表或 YAML 块列表；块内其它键原样保留。块内没有相似词键
    但确有围栏块 → terms 为 () 照常返回；开头没有围栏块 → None。

    只认围栏块这一种形态（0.8.0 开发中途的平文首行已废弃、不做兼容）。
    """
    block_len = _fence_block_len(content)
    if block_len is None:
        return None
    entries, _ = _scan_front_matter(content, block_len)
    for entry in entries:
        if entry.similar:  # 块内从上往下第一处
            return SimilarTermsBlock(terms=_dedupe_terms(" ".join(entry.items)),
                                     block_len=block_len,
                                     key_start=entry.start, key_len=entry.length)
    return SimilarTermsBlock(terms=(), block_len=block_len, key_start=None, key_len=None)


@dataclass(frozen=True, slots=True)
class FrontMatterPlan:
    """头部围栏块的索引端规划（ADR-0037）：哪些键行遮罩、哪些值进检索词通道。

    mask_spans：被遮罩键的 (start, length)，按 start 升序——索引端逐段替换
        为等长空格，字符偏移不变（ADR-0035 同手法）。遮的是整个键（键行 +
        名下列表项行）：键名/冒号不该成为词条，值已单独进通道。
    values：进每个 chunk 检索词通道的源文本，similar_terms 词组在前、
        其余选中键值随后；None 通道全空时也返回空 tuple。
    """

    mask_spans: tuple[tuple[int, int], ...] = ()
    values: tuple[str, ...] = ()


def plan_front_matter(content: str) -> FrontMatterPlan | None:
    """解析头部 front matter，给出遮罩区间与检索词通道的值。

    规则（0.8.0 定稿）：
    - 头部没有合法围栏块 → None，不遮任何东西（宁可少认不可认错，与
      parse_similar_terms_block 同一口径）；
    - `similar_terms:` / `相似词：` 键**必须遮罩**，词组必进通道，不受预算限制、
      不做垃圾值剔除（调用方显式给的词）；
    - 其它顶层键（title/keywords/tags/category/description/……一律通用，
      不维护键名清单）：值先去引号；布尔/null 字面量、纯数字与整值 http(s) URL
      剔除（不进通道、不占预算，键照样遮罩）；列表（块列表或 `[a, b]`）逐项剔除后用空格拼成
      一个值，整组进或整组不进；键值**按长度短的优先**进通道，累计不超过
      _FM_BUDGET 字符——装不下的（含单值超长）整个键留正文，只算 chunk 0；
    - 空值键（含剔除后为空）不占预算：值没词可进通道，但键名照样遮掉（防键名噪音）。

    这里只产出规划；遮罩替换与通道 token 的生成在 index.py。
    """
    block_len = _fence_block_len(content)
    if block_len is None:
        return None
    entries, _ = _scan_front_matter(content, block_len)

    spans: list[tuple[int, int]] = []
    candidates: list[tuple[int, str, int]] = []  # (start, value, length)
    terms: tuple[str, ...] | None = None
    for entry in entries:
        if entry.similar:
            # 相似词键必须遮罩（重复键一并不留）；词组取第一处的去重组
            spans.append((entry.start, entry.length))
            if terms is None:
                terms = _dedupe_terms(" ".join(entry.items))
            continue
        value = " ".join(item for item in entry.items if not _is_junk_value(item))
        candidates.append((entry.start, value, entry.length))

    # 键值按长度升序（同长按文件次序）装入总预算；空的天然最先且不占预算
    candidates.sort(key=lambda cand: (len(cand[1]), cand[0]))
    values: list[str] = list(terms or ())
    budget = 0
    for start, value, length in candidates:
        if len(value) > _FM_BUDGET - budget:
            continue  # 装不下（或单值超长）：整个键留正文，只算 chunk 0
        budget += len(value)
        spans.append((start, length))
        if value:
            values.append(value)
    spans.sort()
    return FrontMatterPlan(mask_spans=tuple(spans), values=tuple(values))


def format_similar_terms_line(terms: tuple[str, ...]) -> str:
    """把词组写成围栏块内的一行平文本。写端生成、索端解析同一格式。"""
    return "similar_terms: " + " ".join(terms)


def assemble_similar_terms_header(terms: tuple[str, ...], content: str) -> str:
    """写入端装配：把参数词表并进 content 的头部围栏块，返回最终落盘全文。

    - 头部有围栏块且已有相似词键 → 第一处整个键（含 YAML 列表项行）由参数行
      替换，重复的相似词键一并不留，块内其它键与正文一律不动；
    - 有围栏块但没有该键 → 参数行补作块内最后一行；
    - 无围栏块 → 新建一个只含参数行的块；
    - content 开头的 BOM 被去掉（写入后文件不再带 BOM）。
    """
    body = content[1:] if content.startswith("﻿") else (content or "")
    key = format_similar_terms_line(terms) + "\n"
    block_len = _fence_block_len(body)
    if block_len is None:
        return f"---\n{key}---\n\n{body}"

    entries, close_start = _scan_front_matter(body, block_len)
    similar = [entry for entry in entries if entry.similar]
    if not similar:
        return body[:close_start] + key + body[close_start:]
    parts: list[str] = []
    prev = 0
    for n, entry in enumerate(similar):
        parts.append(body[prev:entry.start])
        if n == 0:
            parts.append(key)
        prev = entry.start + entry.length
    parts.append(body[prev:])
    return "".join(parts)


def _top_level_groups(content: str, block_len: int) -> tuple[str, list[tuple[str | None, str]], str]:
    """把围栏块切成 (开围栏行, 顶层键组, 闭围栏行)，供覆盖时按键合并。

    键组 = 顶格的键行 + 其后直到下一个顶格键行之前的所有行（列表项、缩进子行、
    不像键的行都归上一个键）——与 YAML 的归属一致，整组换才不会留下孤儿行。
    键名大小写不敏感；similar_terms / 相似词 统一记作 "similar_terms"。
    块内第一个键之前的行（罕见）自成一组，键为 None。
    """
    lines = content[:block_len].splitlines(keepends=True)
    groups: list[list] = []
    for line in lines[1:-1]:
        stripped = line.rstrip("\r\n")
        key = None
        if stripped[:1] and not stripped[0].isspace() and not _LIST_ITEM.match(stripped):
            if _SIMILAR_TERMS_KEY.match(stripped):
                key = "similar_terms"
            elif (match := _GENERIC_KEY.match(stripped)) is not None:
                key = match.group(1).casefold()
        if key is not None or not groups:
            groups.append([key, line])
        else:
            groups[-1][1] += line
    return lines[0], [(key, text) for key, text in groups], lines[-1]


def merge_front_matter(old: str, new: str) -> str:
    """覆盖写入前，把旧文件头部与新 content 按键合并，返回待装配的 content。

    save 覆盖已有文件时只换正文与 similar_terms，不该把旧头部的其它键一起冲掉：
    - 旧文件没有合法围栏块 → 原样返回 new（与新建同一行为）；
    - 以旧头部为底，旧键全部保留、顺序不变；
    - new 自带头部时，同名键（大小写不敏感）用新值整组原位替换，旧头部里
      重复的同名键一并不留；new 里多出来的键补在块末；
    - new 头部里的 similar_terms / 相似词 忽略——相似词以参数为准，由
      assemble_similar_terms_header 随后原位替换旧键或补键；
    - 两边开头的 BOM 都去掉。要删除头部某个键只能用 replace。
    """
    old = old[1:] if old.startswith("\ufeff") else (old or "")
    new = new[1:] if new.startswith("\ufeff") else (new or "")
    old_len = _fence_block_len(old)
    if old_len is None:
        return new
    open_line, old_groups, close_line = _top_level_groups(old, old_len)

    new_len = _fence_block_len(new)
    incoming: dict[str, str] = {}
    if new_len is None:
        body = "\n" + new
    else:
        body = new[new_len:]
        for key, text in _top_level_groups(new, new_len)[1]:
            if key is None or key == "similar_terms":
                continue
            incoming[key] = incoming.get(key, "") + text

    merged: list[str] = []
    used: set[str] = set()
    for key, text in old_groups:
        if key in incoming:
            if key not in used:  # 第一处原位替换，其余同名旧键不留
                merged.append(incoming[key])
                used.add(key)
            continue
        merged.append(text)
    merged.extend(text for key, text in incoming.items() if key not in used)
    return open_line + "".join(merged) + close_line + body


@dataclass(frozen=True, slots=True)
class DocMeta:
    """一个被纳入索引的物理文件。"""

    source: str
    path: str  # 相对该 source 目录的 POSIX 路径，不含 source 名
    mtime: float
    size: int
    char_count: int

    @property
    def key(self) -> DocKey:
        return (self.source, self.path)


@dataclass(frozen=True, slots=True)
class Chunk:
    """检索单元。char_start/char_end 是相对原文的字符偏移。

    不存正文副本：片段在需要时从全文缓存（或磁盘）按偏移切出来，
    否则"限制常驻内存的全文篇数"就省不下内存（见 docs/adr/0024-max-cached-docs.md）。
    """

    source: str
    path: str
    chunk_index: int
    char_start: int
    char_end: int

    @property
    def key(self) -> DocKey:
        return (self.source, self.path)


def split_text(text: str, *, size: int, step: int) -> list[tuple[int, int, str]]:
    """把一段文本切成 (start, end, piece) 三元组列表。

    空白块被丢弃，但不影响后续块的偏移量——偏移量始终相对原文。
    """
    pieces: list[tuple[int, int, str]] = []
    if not text:
        return pieces
    for start in range(0, len(text), step):
        piece = text[start : start + size]
        if not piece.strip():
            continue
        pieces.append((start, start + len(piece), piece))
        if start + size >= len(text):
            break  # 已覆盖到结尾，避免重叠导致的尾部空转
    return pieces
