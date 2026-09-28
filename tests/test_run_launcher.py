"""run.py 启动器的回归测试。

run.py 是本项目唯一的入口（曾经还有 run.sh / run.ps1 / start.sh / start.ps1，
已全部删除）。这里锁死两类曾经造成实际损失的行为：

1. 指纹文件匹配并不等于依赖可用（流出到使用方的缺陷）；
2. **绝不清空属于另一个平台的虚拟环境**——旧版会这么做，结果是把 Windows 侧
   建好的 .venv 无声删掉（实测发生过）。
"""

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[1]


def load_run_module():
    spec = importlib.util.spec_from_file_location("mymemory_run", MCP_DIR / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run = load_run_module()


def make_args(**overrides) -> argparse.Namespace:
    defaults = dict(reinstall=False, recreate=False, index_url=None,
                    find_links=None, offline=False, bundle=None,
                    no_venv=False, venv=str(run.default_venv_dir()),
                    background=False, stop=False, restart=False,
                    status=False, logs=False)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# --- deps_importable ---------------------------------------------------------

def test_deps_importable_true_for_current_interpreter():
    """跑测试的解释器里依赖是齐的。"""
    assert run.deps_importable(Path(sys.executable)) is True


def test_deps_importable_false_for_missing_interpreter():
    assert run.deps_importable(Path("/nonexistent/python")) is False


def test_probe_covers_every_pinned_requirement():
    """探测清单必须覆盖 requirements.txt 里的每个包，否则漏检。"""
    text = (MCP_DIR / "requirements.txt").read_text(encoding="utf-8")
    pinned = {
        line.split("==")[0].strip().lower().replace("-", "_")
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    probed = {name.lower() for name in run.PROBE_IMPORTS}
    assert pinned == probed, f"requirements 与 PROBE_IMPORTS 不一致：{pinned ^ probed}"


# --- 安装决策：指纹匹配 ≠ 依赖可用 --------------------------------------------

@pytest.fixture
def fake_venv(tmp_path: Path):
    """构造一个"看起来装好了"的 venv：指纹匹配，但依赖不可导入。"""
    venv_dir = tmp_path / ".venv"
    bindir = venv_dir / ("Scripts" if run.IS_WINDOWS else "bin")
    bindir.mkdir(parents=True)
    python = bindir / ("python.exe" if run.IS_WINDOWS else "python")
    python.write_text("", encoding="utf-8")
    digest = hashlib.sha256(run.REQUIREMENTS.read_bytes()).hexdigest()
    (venv_dir / run.STAMP_NAME).write_text(digest, encoding="utf-8")
    return python


def test_matching_stamp_does_not_skip_install_when_imports_fail(fake_venv, monkeypatch):
    """这是流出到使用方的那个缺陷：指纹对得上就跳过安装，最终用空环境启动服务，
    报 ModuleNotFoundError: No module named 'uvicorn'。"""
    calls = []
    monkeypatch.setattr(run, "deps_importable", lambda python: False)
    monkeypatch.setattr(run, "log", lambda message: calls.append(("log", message)))

    def fake_run(cmd, **kwargs):
        calls.append(("pip", cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(run.subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as exc:
        run.install_dependencies(fake_venv, make_args())

    pip_calls = [c for c in calls if c[0] == "pip"]
    assert pip_calls, "指纹匹配但依赖不可导入时，必须重新安装"
    assert any("依赖缺失或损坏" in m for kind, m in calls if kind == "log")
    # 装完仍导入不了 -> 明确失败，而不是继续启动一个坏环境
    assert "仍无法导入" in str(exc.value)


def test_matching_stamp_skips_install_when_imports_succeed(fake_venv, monkeypatch):
    calls = []
    monkeypatch.setattr(run, "deps_importable", lambda python: True)
    monkeypatch.setattr(run.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0))

    run.install_dependencies(fake_venv, make_args())
    assert calls == [], "环境可用时不应重复安装"


def test_reinstall_forces_install_even_when_healthy(fake_venv, monkeypatch):
    calls = []
    monkeypatch.setattr(run, "deps_importable", lambda python: True)

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(run.subprocess, "run", fake_run)
    run.install_dependencies(fake_venv, make_args(reinstall=True))
    assert any("-r" in cmd for cmd in calls)


def test_changed_requirements_triggers_install(fake_venv, monkeypatch):
    (fake_venv.parent.parent / run.STAMP_NAME).write_text("stale-digest", encoding="utf-8")
    calls = []
    monkeypatch.setattr(run, "deps_importable", lambda python: True)
    monkeypatch.setattr(run, "log", lambda m: calls.append(m))
    monkeypatch.setattr(run.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0))

    run.install_dependencies(fake_venv, make_args())
    assert any("requirements.txt 已变更" in m for m in calls)


# --- 虚拟环境 -----------------------------------------------------------------

def test_default_venv_is_single_dot_venv():
    """只在当前系统运行，虚拟环境统一为 mcp/.venv（ADR-0021）。"""
    assert run.default_venv_dir() == run.HERE / ".venv"


def test_recreate_clears_existing_venv(tmp_path, monkeypatch):
    """--recreate 是明确的意图：清空重建。"""
    venv_dir = tmp_path / "venv"
    venv_dir.mkdir()
    captured = {}

    class FakeBuilder:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def create(self, target):
            path = run.venv_python(Path(target))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")

    fake_venv_module = type(sys)("venv")
    fake_venv_module.EnvBuilder = FakeBuilder
    monkeypatch.setitem(sys.modules, "venv", fake_venv_module)
    monkeypatch.setattr(run, "log", lambda m: None)

    run.ensure_venv(venv_dir, recreate=True)
    assert captured.get("clear") is True


def test_incomplete_venv_dir_is_filled_in_place(tmp_path: Path, monkeypatch):
    """目录在、解释器不在：是个残缺目录，就地补建即可。"""
    venv_dir = tmp_path / "half-built"
    venv_dir.mkdir()
    captured = {}

    class FakeBuilder:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def create(self, target):
            path = run.venv_python(Path(target))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")

    fake_venv_module = type(sys)("venv")
    fake_venv_module.EnvBuilder = FakeBuilder
    monkeypatch.setitem(sys.modules, "venv", fake_venv_module)
    monkeypatch.setattr(run, "log", lambda m: None)

    run.ensure_venv(venv_dir)
    assert captured.get("clear") is False, "残缺目录不需要清空"


# --- 后台进程管理 -------------------------------------------------------------

def test_process_alive_never_uses_os_kill_on_windows(monkeypatch):
    """Windows 的 os.kill 会调 TerminateProcess——信号 0 也照样杀进程。

    因此 Windows 分支必须走 tasklist。这条一旦写错，"查状态"会变成"杀服务"，
    而且只在 Windows 上复现。
    """
    monkeypatch.setattr(run, "IS_WINDOWS", True)
    monkeypatch.setattr(run.os, "kill",
                        lambda *a, **k: pytest.fail("Windows 上不得调用 os.kill"))
    monkeypatch.setattr(run.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""))
    assert run.process_alive(4321) is False


def test_process_alive_true_for_self():
    assert run.process_alive(os.getpid()) is True


@pytest.mark.skipif(run.IS_WINDOWS, reason="僵尸进程是 POSIX 概念")
def test_zombie_process_counts_as_dead():
    """已退出但未被回收的子进程不算"活着"。

    实测踩过：后台启动的服务被 PID 1 收养，而容器里的 PID 1 常常只是
    `sleep infinity`，不回收子进程。服务 1 秒内就优雅退出了，
    os.kill(pid, 0) 却仍然成功，于是 --stop 白等 10 秒、再对死进程发 SIGKILL，
    最后打印一条"未能优雅退出"的误导性警告。
    """
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    for _ in range(100):          # 等它退出，但**不** wait()，保持僵尸状态
        if run.is_zombie(child.pid):
            break
        time.sleep(0.05)
    try:
        assert run.is_zombie(child.pid) is True, "没能造出僵尸进程，用例前提不成立"
        assert run.process_alive(child.pid) is False
    finally:
        child.wait()


def test_process_alive_false_after_reaping():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert run.process_alive(child.pid) is False


def test_read_pid_clears_stale_file(tmp_path: Path, monkeypatch):
    """PID 文件指向一个已经不在的进程时，要清掉它而不是报告"运行中"。"""
    pid_file = tmp_path / "myMemory.pid"
    pid_file.write_text("999999", encoding="utf-8")
    monkeypatch.setattr(run, "PID_FILE", pid_file)
    monkeypatch.setattr(run, "process_alive", lambda pid: False)

    assert run.read_pid() is None
    assert not pid_file.exists(), "残留的 PID 文件必须被清掉"


def test_status_detects_service_without_pid_file(monkeypatch):
    """前台启动（或启动器被杀）的服务没有 PID 文件，但端口上 /health 通——应报告运行中。"""
    messages: list[str] = []
    monkeypatch.setattr(run, "log", messages.append)
    monkeypatch.setattr(run, "resolve_port", lambda: 7083)
    monkeypatch.setattr(run, "read_pid", lambda: None)
    monkeypatch.setattr(run, "fetch_health", lambda port, timeout=2.0: {
        "doc_count": 3, "chunk_count": 9, "rebuilding": False,
        "sources": [
            {"name": "memory", "writable": True, "available": True,
             "unavailable_reason": None, "doc_count": 2, "dir": "/mnt/nas/memory",
             "description": "主记忆库（NAS）"},
            {"name": "local", "writable": False, "available": False,
             "unavailable_reason": "目录不存在", "doc_count": 1,
             "dir": "/tmp/other", "description": None},
        ],
    })

    assert run.do_status() == 0
    assert any("运行中" in m for m in messages)
    # 每个 source 都要报出自己的目录与可用性，而不是显示一个已废弃的"根目录"。
    assert any("/mnt/nas/memory" in m for m in messages)
    assert any("/tmp/other" in m for m in messages)
    assert any("目录不存在" in m for m in messages)


def test_status_reports_stopped_when_no_pid_and_no_health(monkeypatch):
    messages: list[str] = []
    monkeypatch.setattr(run, "log", messages.append)
    monkeypatch.setattr(run, "resolve_port", lambda: 7083)
    monkeypatch.setattr(run, "read_pid", lambda: None)
    monkeypatch.setattr(run, "fetch_health", lambda port, timeout=2.0: None)

    assert run.do_status() == 1
    assert any("未在后台运行" in m for m in messages)


def test_background_start_skips_when_port_already_serving(monkeypatch):
    """没有 PID 文件但服务已在端口上——--background 不能再拉起第二个实例。"""
    monkeypatch.setattr(run, "read_pid", lambda: None)
    monkeypatch.setattr(run, "fetch_health", lambda port, timeout=2.0: {"doc_count": 1})
    assert run.already_serving(7083) is True
    monkeypatch.setattr(run, "fetch_health", lambda port, timeout=2.0: None)
    assert run.already_serving(7083) is False


def _spawn_capturing_kwargs(tmp_path: Path, monkeypatch, *, windows: bool) -> dict:
    """跑一次 spawn_background，把传给 Popen 的参数抓出来。"""
    monkeypatch.setattr(run, "IS_WINDOWS", windows)
    monkeypatch.setattr(run, "LOG_DIR", tmp_path)
    monkeypatch.setattr(run, "LOG_FILE", tmp_path / "myMemory.log")
    monkeypatch.setattr(run, "PID_FILE", tmp_path / "myMemory.pid")

    captured: dict = {}

    class FakePopen:
        pid = 4242

        def __init__(self, command, **kwargs):
            captured["command"] = command
            captured.update(kwargs)

    monkeypatch.setattr(run.subprocess, "Popen", FakePopen)
    run.spawn_background(["python", "src/main.py"], {})
    return captured


def test_windows_background_spawns_without_a_console_window(tmp_path, monkeypatch):
    """Windows 后台启动不能弹出黑窗。

    实测报告：用 DETACHED_PROCESS 时屏幕上会多出一个控制台窗口。
    原因是 DETACHED_PROCESS 只表示"不继承父进程的控制台"，而 python.exe 是
    控制台程序，Windows 于是**另给它新建一个控制台**。要的是 CREATE_NO_WINDOW
    ——两者互斥，写错一个就复现黑窗，且只在 Windows 上看得见。
    """
    captured = _spawn_capturing_kwargs(tmp_path, monkeypatch, windows=True)
    flags = captured["creationflags"]

    assert flags & run.CREATE_NO_WINDOW, "必须带 CREATE_NO_WINDOW，否则会弹出黑窗"
    assert flags & run.CREATE_NEW_PROCESS_GROUP, "必须自成进程组，Ctrl-C 才不会误伤它"
    detached = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    assert not (flags & detached), (
        "DETACHED_PROCESS 与 CREATE_NO_WINDOW 互斥，且它正是黑窗的来源"
    )


def test_posix_background_detaches_from_the_terminal(tmp_path, monkeypatch):
    """POSIX 侧靠 start_new_session 脱离控制终端，且不该带 Windows 的标志。"""
    captured = _spawn_capturing_kwargs(tmp_path, monkeypatch, windows=False)
    assert captured.get("start_new_session") is True
    assert "creationflags" not in captured


def test_background_child_never_reads_stdin(tmp_path, monkeypatch):
    """stdin 必须接 DEVNULL：后台进程去读终端会被 SIGTTIN 挂起。"""
    for windows in (True, False):
        captured = _spawn_capturing_kwargs(tmp_path, monkeypatch, windows=windows)
        assert captured["stdin"] == subprocess.DEVNULL


def test_resolve_port_reads_config_json(tmp_path: Path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text('{"port": 9999, "sources": []}', encoding="utf-8")
    monkeypatch.setenv("MEMORY_CONFIG", str(config))
    assert run.resolve_port() == 9999


def test_resolve_port_ignores_memory_port_env(tmp_path: Path, monkeypatch):
    """MEMORY_PORT 已作废：唯一的环境变量是 MEMORY_CONFIG。"""
    monkeypatch.setenv("MEMORY_CONFIG", str(tmp_path / "missing.json"))
    monkeypatch.setenv("MEMORY_PORT", "9999")
    assert run.resolve_port() == run.DEFAULT_PORT


def test_resolve_port_falls_back_when_config_missing_or_garbage(tmp_path: Path, monkeypatch):
    """配置缺失或写坏时用默认值，而不是崩在启动脚本里——报错交给服务本身。"""
    monkeypatch.setenv("MEMORY_CONFIG", str(tmp_path / "missing.json"))
    assert run.resolve_port() == run.DEFAULT_PORT
    bad = tmp_path / "bad.json"
    bad.write_text('{"port": "不是数字"', encoding="utf-8")
    monkeypatch.setenv("MEMORY_CONFIG", str(bad))
    assert run.resolve_port() == run.DEFAULT_PORT


def test_child_env_pins_absolute_config_path(tmp_path: Path, monkeypatch):
    """把绝对路径钉给子进程：默认 ~/.myMemory/config.json，与运行目录无关。"""
    monkeypatch.delenv("MEMORY_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    environ = run.build_child_env()
    assert environ["MEMORY_CONFIG"] == str((Path.home() / ".myMemory" / "config.json").resolve())


def test_logs_live_next_to_config_not_in_code_dir():
    """日志与 PID 文件不落在代码目录里。"""
    assert run.LOG_DIR == run.config_path().parent / "logs"
    assert run.HERE not in run.LOG_DIR.parents


# --- 首次启动建档（ADR-0025） -------------------------------------------------

class _FakeStd:
    """只够 ensure_config 用的标准流替身：isatty + print/input 需要的最小接口。"""

    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self):
        return self._tty

    def write(self, s):
        pass

    def flush(self):
        pass

    def readline(self):
        return ""


def test_ensure_config_skipped_when_config_exists(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text('{"sources": []}', encoding="utf-8")
    monkeypatch.setenv("MEMORY_CONFIG", str(config))
    assert run.ensure_config([]) is None  # 已有配置：直接返回，不提问不报错


def test_ensure_config_interactively_bootstraps(tmp_path, monkeypatch, capsys):
    """终端首次启动：逐字询问记忆目录，写出 source memory（默认记忆源、可写）。"""
    import config as cm

    memory = tmp_path / "我的记忆"
    config = tmp_path / "config.json"
    monkeypatch.setenv("MEMORY_CONFIG", str(config))
    monkeypatch.setattr("sys.stdin", _FakeStd(tty=True))
    monkeypatch.setattr("sys.stdout", _FakeStd(tty=True))
    monkeypatch.setattr("builtins.input", lambda _p: str(memory))

    run.ensure_config([])

    saved = json.loads(config.read_text(encoding="utf-8"))
    assert saved["sources"] == [{"name": "memory", "dir": os.path.abspath(memory),
                                 "writable": True, "description": "默认记忆源"}]
    assert saved["port"] == cm.DEFAULTS["port"]
    assert memory.is_dir(), "记忆目录不存在时应自动创建"


def test_ensure_config_stdio_fails_without_memory_dir(tmp_path, monkeypatch):
    """stdio 由客户端拉起、stdin 是管道：无法提问就失败，提示未指定记忆存储，不写文件。"""
    config = tmp_path / "config.json"
    monkeypatch.setenv("MEMORY_CONFIG", str(config))
    monkeypatch.setattr("sys.stdin", _FakeStd(tty=False))
    monkeypatch.setattr("sys.stdout", _FakeStd(tty=False))

    with pytest.raises(SystemExit, match="未指定记忆存储"):
        run.ensure_config(["--stdio"])
    assert not config.exists(), "非交互下不得用猜测的路径建档"


# --- --init：检查 / 生成 / 修复 ------------------------------------------------

@pytest.fixture
def no_deps(monkeypatch):
    """do_init 末尾会装依赖（与正常启动共用 setup_environment）：
    单测里用桩替换，只记录被调用，不真建 venv / 跑 pip。"""
    calls = []
    monkeypatch.setattr(run, "setup_environment",
                        lambda args: calls.append(args) or Path(sys.executable))
    return calls


def _init_args(**overrides) -> argparse.Namespace:
    return make_args(init=True, **overrides)


def test_init_installs_dependencies(tmp_path, monkeypatch, no_deps):
    """--init 的最后一步是装依赖（与正常启动同一口径）。"""
    memory = tmp_path / "mem"
    memory.mkdir()
    config = tmp_path / "config.json"
    monkeypatch.setenv("MEMORY_CONFIG", str(config))
    monkeypatch.setattr("builtins.input", lambda _p: str(memory))

    assert run.do_init(_init_args()) == 0
    assert len(no_deps) == 1, "do_init 必须走一遍 setup_environment"


def test_init_generates_config_when_missing(tmp_path, monkeypatch, no_deps):
    import config as cm

    memory = tmp_path / "mem"
    config = tmp_path / "config.json"
    monkeypatch.setenv("MEMORY_CONFIG", str(config))
    monkeypatch.setattr("sys.stdin", _FakeStd(tty=True))
    monkeypatch.setattr("sys.stdout", _FakeStd(tty=True))
    monkeypatch.setattr("builtins.input", lambda _p: str(memory))

    assert run.do_init(_init_args()) == 0
    saved = json.loads(config.read_text(encoding="utf-8"))
    assert saved["sources"] == [{"name": "memory", "dir": os.path.abspath(memory),
                                 "writable": True, "description": "默认记忆源"}]
    assert saved["port"] == cm.DEFAULTS["port"]


def test_init_non_interactive_uses_default_dir(tmp_path, monkeypatch, no_deps):
    """--init 是显式动作：非终端下重建/建档允许默认目录兜底，并明确打印。"""
    import config as cm

    home = tmp_path / "home"
    config = tmp_path / "config.json"
    monkeypatch.setenv("MEMORY_CONFIG", str(config))
    monkeypatch.setattr(cm, "DATA_DIR", home)  # 默认记忆目录 = home/memory
    monkeypatch.setattr("sys.stdin", _FakeStd(tty=False))
    monkeypatch.setattr("sys.stdout", _FakeStd(tty=False))

    assert run.do_init(_init_args()) == 0
    saved = json.loads(config.read_text(encoding="utf-8"))
    assert saved["sources"] == [{"name": "memory",
                                 "dir": os.path.abspath(home / "memory"),
                                 "writable": True, "description": "默认记忆源"}]
    assert (home / "memory").is_dir()


def test_init_repairs_broken_json_with_backup(tmp_path, monkeypatch, no_deps):
    """JSON 损坏：先备份为 .bak（覆盖旧备份），再重建同构配置。"""
    import config as cm

    memory = tmp_path / "mem"
    memory.mkdir()
    config = tmp_path / "config.json"
    broken = '{"sources": [不是合法 JSON'
    config.write_text(broken, encoding="utf-8")
    (config.parent / "config.json.bak").write_text("旧备份", encoding="utf-8")
    monkeypatch.setenv("MEMORY_CONFIG", str(config))
    monkeypatch.setattr("sys.stdin", _FakeStd(tty=True))
    monkeypatch.setattr("sys.stdout", _FakeStd(tty=True))
    monkeypatch.setattr("builtins.input", lambda _p: str(memory))

    assert run.do_init(_init_args()) == 0
    assert (config.parent / "config.json.bak").read_text(encoding="utf-8") == broken, \
        "重建前必须用坏文件覆盖旧备份"
    saved = json.loads(config.read_text(encoding="utf-8"))
    assert saved["sources"][0]["name"] == "memory"
    assert cm.Config.from_data(saved, config_file=config)  # 重建结果可被服务校验


def test_init_valid_config_is_report_only(tmp_path, monkeypatch, no_deps, capsys):
    """配置完好：只校验报告（含目录可用性），不改一个字节。"""
    memory = tmp_path / "mem"
    memory.mkdir()
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "sources": [{"name": "memory", "dir": str(memory), "writable": True,
                     "description": "默认记忆源"},
                    {"name": "gone", "dir": str(tmp_path / "nope"), "writable": False}],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("MEMORY_CONFIG", str(config))
    before = config.read_bytes()

    assert run.do_init(_init_args()) == 0
    assert config.read_bytes() == before, "完好配置不得被 --init 改动"
    out = capsys.readouterr().out
    assert "配置校验通过" in out
    assert "memory" in out and "默认记忆源" in out
    assert "gone" in out and "目录不存在" in out


def test_existing_valid_venv_is_reused(tmp_path: Path, monkeypatch):
    venv_dir = tmp_path / ".venv"
    python = run.venv_python(venv_dir)
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")

    monkeypatch.setattr(run, "log", lambda m: pytest.fail(f"不应重建：{m}"))
    assert run.ensure_venv(venv_dir) == python
