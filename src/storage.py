"""存储层：一个 source 的内容从哪里来、写到哪里去。

索引层与工具层只通过这里的接口接触文件，不直接拼 source 目录——
今后接入 git / http / oss 时只新增一种 Storage 实现（需求 §3.4）。
目前只有 LocalStorage：本地目录或挂载盘（如 Z:\\）。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator, Protocol

from config import Source

logger = logging.getLogger(__name__)


DISK_OFFLINE = "disk_offline"   # source 所在盘根访问不到：掉盘
DIR_MISSING = "dir_missing"     # 盘根可访问，但 source 目录不存在：按删除处理

# POSIX 探测的超时秒数：坏挂载的 stat 可能无限阻塞，必须带外置超时。
PROBE_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True, slots=True)
class Availability:
    """source 当前能否访问。reason 仅在不可用时有值。"""

    available: bool
    reason: str | None = None


AVAILABLE = Availability(True)


@dataclass(frozen=True, slots=True)
class FileStat:
    path: str      # 相对 source 根目录的 POSIX 路径
    mtime: float
    size: int


class Storage(Protocol):
    source: Source

    def probe(self) -> Availability: ...
    def iter_files(self, extensions: set[str]) -> Iterator[FileStat]: ...
    def read_text(self, path: str) -> str: ...
    def stat(self, path: str) -> FileStat | None: ...
    def write_text(self, path: str, text: str) -> FileStat: ...
    def move(self, old_path: str, new_path: str) -> FileStat: ...
    def remove(self, path: str) -> None: ...


def _is_hidden(relative: PurePosixPath) -> bool:
    """路径中任一层以 . 开头即视为隐藏（.git/、.obsidian/ 等工具产物）。"""
    return any(part.startswith(".") for part in relative.parts)


class LocalStorage:
    """本地目录。path 一律是相对 source 目录的 POSIX 路径。"""

    def __init__(self, source: Source) -> None:
        self.source = source
        self.root = source.dir
        # Linux 探测子进程：超时后可能仍活着（坏挂载收不掉），留着引用避免下次堆积。
        self._probe_proc: subprocess.Popen[bytes] | None = None

    def probe(self) -> Availability:
        """判定可用性。

        Windows：以**盘根**能否访问判定掉盘（见 _probe_stat）。
        Linux：挂载点（如 /mnt/nas/docs）的盘根恒为 /，盘根判定永远通过，
        改为对挂载点做一次带 3 秒超时的列目录（见 _probe_linux）。
        两种情况都不以目录是否存在判定掉盘——目录可能是被人删除了。
        """
        if sys.platform.startswith("linux"):
            return self._probe_linux()
        return self._probe_stat()

    def _probe_stat(self) -> Availability:
        """Windows 及其他平台的判定：盘根（anchor）能否访问。

        盘根是路径的 anchor：`Z:\\`、`\\\\server\\share\\`（POSIX 上是 `/`，恒可访问）。
        """
        anchor = self.root.anchor
        try:
            if anchor and not os.path.exists(anchor):
                return Availability(False, DISK_OFFLINE)
            if not self.root.is_dir():
                return Availability(False, DIR_MISSING)
        except OSError:
            return Availability(False, DISK_OFFLINE)
        return AVAILABLE

    def _probe_linux(self) -> Availability:
        """Linux 的判定：对挂载点做一次 `timeout 3 ls`，通了才算可用。

        坏挂载（hard mount）里的 stat 会陷入不可中断睡眠，进程收不掉，
        所以超时后不等待、直接判 offline；遗留子进程由外置 timeout(1) 终止，
        它退出前不再起新的探测——挂载恢复时它自己会退出，不会永久堆积。
        """
        if self._probe_proc is not None and self._probe_proc.poll() is None:
            return Availability(False, DISK_OFFLINE)  # 上一轮还没退，盘仍不可达
        if not self._reachable(self.root):
            # 目录不可达：区分挂载死了（父目录也不可达）与目录被删（父目录可达）
            if self._reachable(self.root.parent):
                return Availability(False, DIR_MISSING)
            return Availability(False, DISK_OFFLINE)
        try:
            if not self.root.is_dir():
                return Availability(False, DIR_MISSING)
        except OSError:
            return Availability(False, DISK_OFFLINE)
        return AVAILABLE

    def _reachable(self, path: Path) -> bool:
        """path 能否在超时内被 ls 到。"""
        try:
            self._probe_proc = subprocess.Popen(
                ["timeout", "--kill-after=1", str(int(PROBE_TIMEOUT_SECONDS)), "ls", "-d", str(path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError:  # 没有外置 timeout(1)，退回裸判定
            logger.warning("探测子进程启动失败，退回直接 stat：%s", path)
            try:
                return path.exists()
            except OSError:
                return False
        deadline = time.monotonic() + PROBE_TIMEOUT_SECONDS + 1.0
        while time.monotonic() < deadline:
            if self._probe_proc.poll() is not None:
                return self._probe_proc.returncode == 0
            time.sleep(0.05)
        return False  # 超时即不可达，子进程交给 timeout(1) 收尾，不等待

    def _absolute(self, path: str) -> Path:
        return self.root.joinpath(*PurePosixPath(path).parts)

    def iter_files(self, extensions: set[str]) -> Iterator[FileStat]:
        for absolute in self.root.rglob("*"):
            if absolute.suffix.lower() not in extensions:
                continue
            try:
                if not absolute.is_file():
                    continue
                relative = PurePosixPath(absolute.relative_to(self.root).as_posix())
                if _is_hidden(relative):
                    continue
                st = absolute.stat()
            except OSError as exc:  # 扫描与 stat 之间文件被删
                logger.warning("跳过无法 stat 的文件 %s/%s: %s", self.source.name, absolute, exc)
                continue
            yield FileStat(path=relative.as_posix(), mtime=st.st_mtime, size=st.st_size)

    def read_text(self, path: str) -> str:
        return self._absolute(path).read_text(encoding="utf-8", errors="ignore")

    def stat(self, path: str) -> FileStat | None:
        try:
            st = self._absolute(path).stat()
        except OSError:
            return None
        return FileStat(path=path, mtime=st.st_mtime, size=st.st_size)

    def write_text(self, path: str, text: str) -> FileStat:
        """写入并返回落盘后的状态。

        两道最后的闸：只读 source 拒写；source 目录不存在时拒写，
        绝不因为 mkdir(parents=True) 把一个被删除（或掉盘）的目录重新建出来。
        """
        if not self.source.can_write:
            raise PermissionError(f"source {self.source.name} 为只读")
        if not self.root.is_dir():
            raise FileNotFoundError(f"source {self.source.name} 的目录不存在：{self.root}")
        absolute = self._absolute(path)
        absolute.parent.mkdir(parents=True, exist_ok=True)
        absolute.write_text(text, encoding="utf-8", newline="\n")
        st = absolute.stat()
        return FileStat(path=path, mtime=st.st_mtime, size=st.st_size)

    def move(self, old_path: str, new_path: str) -> FileStat:
        """把 source 目录内的一个文件挪到另一处并返回新位置的状态。

        与 write_text 同一套闸（只读拒、目录不存在拒）；先建目标所在目录、
        再改名，失败时不会留下半移动的状态（改名本身是原子的）。
        """
        if not self.source.can_write:
            raise PermissionError(f"source {self.source.name} 为只读")
        if not self.root.is_dir():
            raise FileNotFoundError(f"source {self.source.name} 的目录不存在：{self.root}")
        source_absolute = self._absolute(old_path)
        target_absolute = self._absolute(new_path)
        target_absolute.parent.mkdir(parents=True, exist_ok=True)
        source_absolute.rename(target_absolute)
        st = target_absolute.stat()
        return FileStat(path=new_path, mtime=st.st_mtime, size=st.st_size)

    def remove(self, path: str) -> None:
        """删除 source 目录内的一个文件，与 write_text 同一套前置闸。"""
        if not self.source.can_write:
            raise PermissionError(f"source {self.source.name} 为只读")
        if not self.root.is_dir():
            raise FileNotFoundError(f"source {self.source.name} 的目录不存在：{self.root}")
        self._absolute(path).unlink(missing_ok=True)


def open_storage(source: Source) -> Storage:
    if source.type == "local":
        return LocalStorage(source)
    raise ValueError(f"不支持的存储类型：{source.type}")  # config 已拦截，这里只是兜底
