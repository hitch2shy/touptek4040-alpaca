import platform
from pathlib import Path

from touptek4040.config import runtime_base_dir


def test_runtime_base_dir_is_an_absolute_path():
    assert isinstance(runtime_base_dir(), Path)
    assert runtime_base_dir().is_absolute()


def test_python_process_is_64_bit():
    assert platform.architecture()[0] == "64bit"
