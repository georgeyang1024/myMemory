import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def pytest_configure(config):
    """测试临时目录放到 ~/.myMemory/pytest-tmp：不落在代码目录，也避开 Windows
    系统临时目录（E:\\Windows\\Temp）对 pytest-of-<用户> 目录的权限问题。"""
    if config.option.basetemp is None:
        config.option.basetemp = str(Path.home() / ".myMemory" / "pytest-tmp")
