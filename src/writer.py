"""写入层：把一条记忆落成某个 source 目录下的一个 Markdown 文件。

这是本服务里唯一一处写磁盘的代码（index.cache 与 config.json 属于服务自身状态，
不在记忆目录里），因此写入边界必须在这里一次性守死：

1. **source 必须显式指定，且可写、当前可用**。配置只读（writable: false）、非 local 存储、
   掉盘、目录不存在一律拒绝；storage 层在落盘前还会再拦一次，且绝不重建被删除的目录。
2. **分类与文件名是标识符，不是路径**。二者都经过字符集白名单校验
   （config 模块的 is_valid_category / is_valid_filename）：白名单不含 / 与 \\，
   ".." 与首尾的 "." 另行禁止，所以 "../x"、"a/b"、".hidden" 在语法层就被拒绝，
   落盘路径只可能是 <source目录>/<分类>/<文件名>.md 或 <source目录>/<文件名>.md。
   不做 realpath、不做前缀比较——与 get-document 的集合成员判断同源。
3. **只写一级目录，分类可为空**。分类名不可嵌套；为空时直接写在 source 根目录。
4. **同路径即覆盖**（upsert）。目标文件已存在时整篇替换，不改名、不追加、不合并。
   代价是**覆盖不可撤销且没有备份**——见 ADR-0014。为了让这件事至少是**可见的**，
   覆盖时返回 created=False 与被替换内容的长度。
5. **rename、replace 与 merge 走同一套边界**。rename 在同 source 内改名/移一级分类，
   目标存在即拒绝（覆盖等于把已有文件直接删掉，不可撤销）；replace 做字面
   old→new 替换，命中 0 处或 new_string 为空一律拒绝；merge 把一篇已存在的
   记忆并入另一篇并删源，并入段带来源标题。三者的源/目标文件都必须真实
   存在；不认识索引，存在性按文件系统判断。
6. **删除是断路器保护的能力（配置 allow_mcp_delete，默认关）**。delete 真删
   （无备份）；merge 删源同受此闸。关闭时 server 层根本不注册这两个工具，
   这里再拦一道——双保险必须连接到同一个开关。

写入成功后由调用方（server.py）记下 agent 标记并触发索引刷新，本模块不认识索引。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from config import Config, Source
from storage import DISK_OFFLINE, open_storage

logger = logging.getLogger(__name__)

EXTENSION = ".md"


class WriteError(ValueError):
    """入参不合法或落盘失败。消息直接返回给调用方（LLM），因此要可读且可据以纠正。"""


@dataclass(frozen=True, slots=True)
class Written:
    """一次成功的写入。(source, path) 与检索结果同形，可直接喂给 get-document。"""

    source: str
    path: str          # 相对 source 目录的 POSIX 路径，如 "技术/协议要点.md"
    absolute: Path
    char_count: int
    mtime: float                         # 落盘后的 mtime，用于判定 edited_by
    created: bool                        # True=新建，False=覆盖了已有文件
    replaced_char_count: int | None      # 覆盖时为被替换内容的长度，新建时为 None


@dataclass(frozen=True, slots=True)
class Merged:
    """一次成功的合并。(source, path) 是并入目标；from_path 是已删除（或删失败的）源文件。"""

    source: str
    path: str             # 目标相对路径，如 "技术/汇总.md"
    absolute: Path
    from_path: str        # 源相对路径，如 "工作/周报.md"
    char_count: int       # 并入后目标全文长度
    old_char_count: int   # 并入前目标全文长度
    mtime: float
    source_removed: bool  # False = 内容已并入但源文件删除失败，仍在原处


def _normalize_stem(filename: str) -> str:
    """去掉调用方可能带上的 .md 后缀，返回文件名主干。

    调用方是 LLM，"传不传扩展名"是个纯粹的无谓分歧：两种都接受，
    对齐到同一种形态即可，不值得为此让它读一条报错再重试一轮。
    """
    stem = filename.strip()
    if stem.lower().endswith(EXTENSION):
        stem = stem[: -len(EXTENSION)]
    return stem.strip()


def _writable_target(config: Config, name: str) -> Source:
    """共用前置校验：source 存在、可写、当前可用。返回该 source。"""
    target = config.source(name)
    if target is None:
        raise WriteError(f"source 不存在：{name!r}。")
    if not target.can_write:
        raise WriteError(f"source {name} 为只读，不能写入。")
    state = open_storage(target).probe()
    if not state.available:
        why = "挂载盘掉线" if state.reason == DISK_OFFLINE else "目录不存在"
        raise WriteError(f"source {name} 当前不可用（{why}），不能写入。")
    return target


def _validate_category(config: Config, category: str, which: str = "category") -> None:
    """分类的白名单校验，与 save 同一套规则。which 指出报错对应哪个入参。"""
    if not category or config.is_valid_category(category):
        return
    if which == "category":
        raise WriteError(
            f"category 非法：{category!r}。只允许中英文、数字、空格、常用标点与 ()[]{{}}.,&#+@!'=~% ，"
            f"长度 1-64，不能以 . 或空格开头结尾、不能含 .. 或 / —— 分类只有一级，不支持嵌套目录。"
            f"不需要分类时传空字符串。"
        )
    raise WriteError(
        f"{which} 里的分类部分非法：{category!r}。只允许中英文、数字、空格、常用标点与 "
        f"()[]{{}}.,&#+@!'=~% ，长度 1-64，不能以 . 或空格开头结尾、不能含 .. 或 / "
        f"—— 分类只有一级，不支持嵌套目录。"
    )


def _validate_filename(config: Config, stem: str, which: str = "filename") -> None:
    """文件名主干的白名单校验，与 save 同一套规则。which 指出报错对应哪个入参。"""
    if config.is_valid_filename(stem):
        return
    if which == "filename":
        raise WriteError(
            f"filename 非法：{stem!r}。只允许中英文、数字、空格、常用标点与 ()[]{{}}.,&#+@!'=~% ，"
            f"长度 1-120，不能以 . 或空格开头结尾、不能含 .. / \\ : * ? \" < > |，"
            f"也不能是 CON、NUL 这类系统保留名 —— 目录层级由 category 决定。"
        )
    raise WriteError(
        f"{which} 的文件名部分非法：{stem!r}。只允许中英文、数字、空格、常用标点与 "
        f"()[]{{}}.,&#+@!'=~% ，长度 1-120，不能以 . 或空格开头结尾、"
        f"不能含 .. / \\ : * ? \" < > |，也不能是 CON、NUL 这类系统保留名。"
    )


def save_memory(config: Config, source: str, category: str, filename: str,
                content: str) -> Written:
    """把一条记忆写到 <source目录>/[<category>/]<filename>.md，已存在即整篇覆盖。

    source 必须已配置且可写；category 可为空（写到 source 根目录）。
    category 与 filename 均为标识符而非路径，校验不通过即抛 WriteError。
    """
    name = (source or "").strip()
    if not name:
        raise WriteError("source 不能为空。可写的 source 见 list-sources。")
    target = _writable_target(config, name)

    category = (category or "").strip()
    stem = _normalize_stem(filename or "")

    if not stem:
        raise WriteError("filename 不能为空。传文件名即可，.md 后缀可带可不带。")
    _validate_category(config, category)
    _validate_filename(config, stem)

    if not (content or "").strip():
        raise WriteError(
            "content 不能为空。请传入这条记忆的 Markdown 正文。"
            "（空内容会覆盖掉已有记忆，因此这里一律拒绝。）"
        )
    if len(content) > config.max_create_chars:
        raise WriteError(
            f"content 过长：{len(content)} 字符，上限 {config.max_create_chars}。"
            f"请拆成多条记忆，或精简内容。"
        )

    relative = f"{category}/{stem}{EXTENSION}" if category else f"{stem}{EXTENSION}"
    storage = open_storage(target)

    # 覆盖前先量一下旧内容的长度。目的不是备份（没有备份），而是让"这次覆盖掉了
    # 一篇 3000 字的记忆"这件事出现在响应里——否则调用方无从察觉自己刚刚
    # 替换了什么。读失败不阻断写入：拿不到长度只是少一条提示。
    replaced_char_count: int | None = None
    created = storage.stat(relative) is None
    if not created:
        try:
            replaced_char_count = len(storage.read_text(relative))
        except OSError as exc:
            logger.warning("覆盖前读取旧内容失败 %s/%s: %s", name, relative, exc)

    # 结尾补一个换行：Markdown 文件不以换行结尾在各类工具里都是噪音来源。
    text = content if content.endswith("\n") else content + "\n"
    try:
        stat = storage.write_text(relative, text)
    except OSError as exc:
        raise WriteError(f"写入失败：{name}/{relative}（{exc}）") from exc

    if created:
        logger.info("已创建记忆 %s/%s（%d 字符）", name, relative, len(text))
    else:
        logger.info(
            "已覆盖记忆 %s/%s（%s → %d 字符）", name, relative,
            "?" if replaced_char_count is None else replaced_char_count, len(text),
        )
    return Written(
        source=name,
        path=relative,
        absolute=target.dir.joinpath(*relative.split("/")),
        char_count=len(text),
        mtime=stat.mtime,
        created=created,
        replaced_char_count=replaced_char_count,
    )


def _normalize_path(raw: str) -> str:
    """把调用方给的 path 对齐成内部形态：去空白、去 .md 后缀。

    接受 "技术/旧名.md"、"技术/旧名" 两种形态，与 save 对 filename 的
    宽容口径一致——不值得为"传不传扩展名"让 LLM 多跑一轮报错重试。
    """
    text = raw.strip()
    if text.lower().endswith(EXTENSION):
        text = text[: -len(EXTENSION)]
    return text.strip()


def _split(relative: str) -> tuple[str, str]:
    """把 "技术/旧名" 拆成 (分类, 文件名主干)；无分类时分类为空。"""
    if "/" in relative:
        category, stem = relative.rsplit("/", 1)
        return category.strip(), stem.strip()
    return "", relative


def _validate_location(config: Config, category: str, stem: str,
                       which: str) -> tuple[str, str]:
    """校验一条位置（分类 + 文件名主干），与 save 同一套白名单。返回对齐后的二元组。"""
    if not stem:
        raise WriteError(f"{which} 的文件名部分为空。")
    _validate_category(config, category, which=which)
    _validate_filename(config, stem, which=which)
    return category, stem


def rename_memory(config: Config, source: str, path: str, new_path: str) -> Written:
    """把 <source>/<path>.md 改名（或移动到另一个一级分类），目标已存在即拒绝。

    rename 只在同一个 source 内移动：跨 source 属于复制+删除的语义，不归这里管。
    path/new_path 都是相对 source 目录的完整路径（与 search 返回的 path 同形），
    .md 后缀可带可不带。与 save 的 upsert 不同，rename 要求旧文件真实存在，
    且目标存在时明确拒绝——rename 的覆盖等于把已有文件直接删掉，不可撤销。
    本函数不认识索引：按文件系统判断存在与否。
    """
    name = (source or "").strip()
    if not name:
        raise WriteError("source 不能为空。可写的 source 见 list-sources。")
    target = _writable_target(config, name)

    old_relative = _normalize_path(path or "")
    if not old_relative:
        raise WriteError("path 不能为空。传 search/recent 返回的 path 即可，.md 可带可不带。")
    new_relative = _normalize_path(new_path or "")
    if not new_relative:
        raise WriteError("new_path 不能为空。传改名后的完整路径即可，.md 可带可不带。")

    old_category, old_stem = _validate_location(
        config, *_split(old_relative), which="path")
    new_category, new_stem = _validate_location(
        config, *_split(new_relative), which="new_path")

    old_relative = f"{old_category}/{old_stem}{EXTENSION}" if old_category else f"{old_stem}{EXTENSION}"
    new_relative = f"{new_category}/{new_stem}{EXTENSION}" if new_category else f"{new_stem}{EXTENSION}"
    if old_relative == new_relative:
        raise WriteError(f"path 与 new_path 相同：{old_relative!r}，无事可做。")

    storage = open_storage(target)
    if storage.stat(old_relative) is None:
        raise WriteError(f"文件不存在：{name}/{old_relative}。先 search 确认实际路径。")
    if storage.stat(new_relative) is not None:
        # 不覆盖：rename 的覆盖等于把已有文件直接删掉，且不可撤销。
        raise WriteError(
            f"目标文件已存在：{name}/{new_relative}。rename 不覆盖已有文件；"
            f"如确要替换其内容，请改用 save。"
        )

    try:
        stat = storage.move(old_relative, new_relative)
    except OSError as exc:
        raise WriteError(
            f"改名失败：{name}/{old_relative} → {new_relative}（{exc}）"
        ) from exc

    logger.info("已改名记忆 %s/%s → %s", name, old_relative, new_relative)
    return Written(
        source=name,
        path=new_relative,
        absolute=target.dir.joinpath(*new_relative.split("/")),
        char_count=stat.size,
        mtime=stat.mtime,
        created=False,
        replaced_char_count=None,
    )


def replace_memory(config: Config, source: str, path: str,
                   old_string: str, new_string: str) -> Written:
    """在 <source>/<path>.md 的全文里做字面替换 old→new，全部命中处一起换。

    与 save（整篇覆盖）不同，replace 只动 old_string 命中的片段——长文里改
    一两处时，调用方不必为了局部修改重发整篇内容。匹配是完全字面的：不做
    换行归一化、不做正则、不做大小写折叠。old_string 命中 0 处（内容没变）
    或 new_string 为空都直接拒绝；new_string 为空等于删除片段，同样拒绝。
    """
    name = (source or "").strip()
    if not name:
        raise WriteError("source 不能为空。可写的 source 见 list-sources。")
    target = _writable_target(config, name)

    relative = _normalize_path(path or "")
    if not relative:
        raise WriteError("path 不能为空。传 search/recent 返回的 path 即可，.md 可带可不带。")
    category, stem = _validate_location(config, *_split(relative), which="path")
    relative = f"{category}/{stem}{EXTENSION}" if category else f"{stem}{EXTENSION}"

    if not (old_string or "").strip():
        raise WriteError(
            "old_string 不能为空。请传入文件里要被替换的原文片段（建议连同少量上下文）。"
        )
    if not (new_string or "").strip():
        raise WriteError(
            "new_string 不能为空。把 old 换成空串等于删除片段，这里不支持——"
            "请写入替换后的剩余文本；要整篇重写请改用 save。"
        )
    if old_string == new_string:
        raise WriteError("old_string 与 new_string 相同，内容不会变化。")

    storage = open_storage(target)
    if storage.stat(relative) is None:
        raise WriteError(f"文件不存在：{name}/{relative}。先 search 确认实际路径。")
    try:
        content = storage.read_text(relative)
    except OSError as exc:
        raise WriteError(f"读取失败：{name}/{relative}（{exc}）") from exc

    count = content.count(old_string)
    if count == 0:
        raise WriteError(
            f"old_string 在 {name}/{relative} 中命中 0 处，未做任何修改。"
            f"请用 get-document 核对原文（注意空格与换行必须逐字一致）。"
        )

    updated = content.replace(old_string, new_string)
    try:
        stat = storage.write_text(relative, updated)
    except OSError as exc:
        raise WriteError(f"写入失败：{name}/{relative}（{exc}）") from exc

    logger.info("已替换记忆 %s/%s（%d 处）", name, relative, count)
    return Written(
        source=name,
        path=relative,
        absolute=target.dir.joinpath(*relative.split("/")),
        char_count=len(updated),
        mtime=stat.mtime,
        created=False,
        replaced_char_count=count,
    )


def _require_delete_enabled(config: Config) -> None:
    """删除类操作（delete 真删、merge 删源）的配置闸：默认关闭。

    工具在关闭时根本不出现在 tools/list 里，LLM 不应该走到这里；
    这道闸兜住的是绕过工具层的调用与配置回退的窗口期。
    """
    if not config.allow_mcp_delete:
        raise WriteError(
            "删除能力未开启（配置 allow_mcp_delete 默认 false）：AI 不能删除记忆。"
            "需要删除时请把 config.json 里 \"allow_mcp_delete\" 设为 true 并重启服务。"
        )


def delete_memory(config: Config, source: str, path: str) -> Written:
    """真删 <source>/<path>.md：unlink，不备份、不隔离（与覆盖无备份的哲学一致）。

    边界口径与 rename/merge 相同：同 source、文件必须真实存在、路径走同一套
    白名单校验。不可恢复是刻意的——删除的豁口只该在配置里开一次，而不是靠
    运行时的花活（回收站/软删除）来消解。
    """
    _require_delete_enabled(config)

    name = (source or "").strip()
    if not name:
        raise WriteError("source 不能为空。可写的 source 见 list-sources。")
    target = _writable_target(config, name)

    relative = _normalize_path(path or "")
    if not relative:
        raise WriteError("path 不能为空。传 search/recent 返回的 path 即可，.md 可带可不带。")
    category, stem = _validate_location(config, *_split(relative), which="path")
    relative = f"{category}/{stem}{EXTENSION}" if category else f"{stem}{EXTENSION}"

    storage = open_storage(target)
    stat = storage.stat(relative)
    if stat is None:
        raise WriteError(f"文件不存在：{name}/{relative}。先 search 确认实际路径。")
    try:
        storage.remove(relative)
    except OSError as exc:
        raise WriteError(f"删除失败：{name}/{relative}（{exc}）") from exc

    logger.info("已删除记忆 %s/%s（%d 字符）", name, relative, stat.size)
    return Written(
        source=name,
        path=relative,
        absolute=target.dir.joinpath(*relative.split("/")),
        char_count=stat.size,
        mtime=stat.mtime,
        created=False,
        replaced_char_count=None,
    )


def merge_memory(config: Config, source: str, from_path: str, to_path: str) -> Merged:
    """把 <source>/from_path.md 并入已存在的 <source>/to_path.md，然后删除源文件。

    与 rename（移动）、replace（字面替换）不同，merge 改变的是目标的完整内容：
    并入段以 "## 源文件相对路径" 起头，前面用 " --- "（前后各空一行）与原文
    分隔——合并后仍能看出这段内容来自谁，符合"记忆要留证据"的定位。

    merge 会删除源文件，因此同样受配置 allow_mcp_delete 的闸（默认关）。
    边界口径与 rename 相同：同一 source 内；from 与 to 都必须真实存在
    （合并不负责新建，目标为空是 save 的事）；from 与 to 相同直接拒绝。
    删除在并入成功之后做，删除失败不回滚、不误报失败——响应里会标 source_removed。
    """
    _require_delete_enabled(config)

    name = (source or "").strip()
    if not name:
        raise WriteError("source 不能为空。可写的 source 见 list-sources。")
    target = _writable_target(config, name)

    from_relative = _normalize_path(from_path or "")
    if not from_relative:
        raise WriteError("from_path 不能为空。传 search/recent 返回的 path 即可，.md 可带可不带。")
    to_relative = _normalize_path(to_path or "")
    if not to_relative:
        raise WriteError("to_path 不能为空。传合并目标的 path 即可，.md 可带可不带。")

    from_category, from_stem = _validate_location(
        config, *_split(from_relative), which="from_path")
    to_category, to_stem = _validate_location(
        config, *_split(to_relative), which="to_path")

    from_relative = f"{from_category}/{from_stem}{EXTENSION}" if from_category else f"{from_stem}{EXTENSION}"
    to_relative = f"{to_category}/{to_stem}{EXTENSION}" if to_category else f"{to_stem}{EXTENSION}"
    if from_relative == to_relative:
        raise WriteError(f"from_path 与 to_path 相同：{to_relative!r}，无法自己并入自己。")

    storage = open_storage(target)
    if storage.stat(to_relative) is None:
        raise WriteError(
            f"目标文件不存在：{name}/{to_relative}。merge 只并入已有记忆，"
            f"新建一篇用 save。"
        )
    if storage.stat(from_relative) is None:
        raise WriteError(f"源文件不存在：{name}/{from_relative}。先 search 确认实际路径。")

    try:
        target_content = storage.read_text(to_relative)
        from_content = storage.read_text(from_relative)
    except OSError as exc:
        raise WriteError(f"读取失败：{name}（{exc}）") from exc
    if not from_content.strip():
        raise WriteError(f"源文件是空的：{name}/{from_relative}，没有可并入的内容。")

    # 标题用源文件去掉 .md 的完整路径："工作/周会纪要"，跨分类合并时仍能追溯出处。
    heading = f"## {from_relative[: -len(EXTENSION)]}"
    body = from_content.strip("\n")
    if target_content.strip():
        updated = target_content.rstrip("\n") + "\n\n---\n\n" + heading + "\n\n" + body + "\n"
    else:
        updated = heading + "\n\n" + body + "\n"

    replaced_char_count = len(target_content)
    try:
        stat = storage.write_text(to_relative, updated)
    except OSError as exc:
        raise WriteError(f"写入失败：{name}/{to_relative}（{exc}）") from exc

    # 先并入、后删源：删除失败时内容已经安全落在目标里，
    # 不能把这次合并报成失败，交给响应体的 source_removed 提示。
    source_removed = True
    try:
        storage.remove(from_relative)
    except OSError as exc:
        source_removed = False
        logger.warning("合并成功但删除源文件失败 %s/%s: %s", name, from_relative, exc)

    logger.info(
        "已合并记忆 %s/%s → %s（源%s）", name, from_relative, to_relative,
        "已删除" if source_removed else "删除失败，仍在原处",
    )
    return Merged(
        source=name,
        path=to_relative,
        absolute=target.dir.joinpath(*to_relative.split("/")),
        from_path=from_relative,
        char_count=len(updated),
        old_char_count=replaced_char_count,
        mtime=stat.mtime,
        source_removed=source_removed,
    )
