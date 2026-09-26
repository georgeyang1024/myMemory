"""语料层：文档与检索单元的数据形态，以及固定窗口切块。

本模块不认识 BM25，也不认识 HTTP，也不碰磁盘。扫描与读取经 storage 层，
按文件增量处理在 index.py（见 docs/adr/0019-index-cache-incremental.md）。

切块策略：统一固定窗口，不做任何结构解析。
理由与取舍见 docs/adr/0005-fixed-window-chunking.md。
"""

from __future__ import annotations

from dataclasses import dataclass

DocKey = tuple[str, str]  # (source, path)——一篇记忆的唯一身份


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
