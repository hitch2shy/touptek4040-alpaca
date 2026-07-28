from __future__ import annotations

import ctypes
import gc
import platform
import weakref

import pytest

from touptek4040.toupcam_loader import (
    load_default_toupcam_module,
    load_toupcam_module,
    validate_runtime_architecture,
)


def test_loader_rejects_32_bit_python(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rejecting a 32-bit interpreter prevents loading the x64 SDK incorrectly."""
    monkeypatch.setattr(platform, "architecture", lambda: ("32bit", "WindowsPE"))

    with pytest.raises(RuntimeError, match="64-bit"):
        validate_runtime_architecture()


def test_loader_uses_project_sdk_python_wrapper_when_runtime_files_are_absent(
    tmp_path,
) -> None:
    """The SDK distribution stores its wrapper under python/ and DLL under win/x64/."""
    wrapper = tmp_path / "toupcamsdk.20250623" / "python" / "toupcam.py"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("SOURCE = 'project-sdk'\n", encoding="utf-8")
    dll = tmp_path / "toupcamsdk.20250623" / "win" / "x64" / "toupcam.dll"
    dll.parent.mkdir(parents=True)
    dll.touch()

    sdk = load_toupcam_module(tmp_path)

    assert sdk.SOURCE == "project-sdk"


def test_loader_keeps_dll_directory_alive_for_delayed_wrapper_load(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The directory cookie must survive until a lazily loaded DLL is requested."""
    wrapper = tmp_path / "toupcamsdk.20250623" / "python" / "toupcam.py"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text(
        "import ctypes\n"
        "def delayed_dll_load():\n"
        "    return ctypes.CDLL('toupcam.dll')\n",
        encoding="utf-8",
    )
    dll = tmp_path / "toupcamsdk.20250623" / "win" / "x64" / "toupcam.dll"
    dll.parent.mkdir(parents=True)
    dll.touch()

    class Cookie:
        active = True

        def __del__(self) -> None:
            self.active = False

    cookie = Cookie()
    cookie_ref = weakref.ref(cookie)
    monkeypatch.setattr("touptek4040.toupcam_loader.os.add_dll_directory", lambda _: cookie)
    monkeypatch.setattr(
        ctypes,
        "CDLL",
        lambda _: "loaded" if cookie_ref() is not None and cookie_ref().active else (_ for _ in ()).throw(OSError()),
    )

    sdk = load_toupcam_module(tmp_path)
    del cookie
    gc.collect()

    assert sdk.delayed_dll_load() == "loaded"


def test_default_loader_finds_project_sdk_from_linked_worktree(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The interpreter location, rather than CWD, locates a linked worktree SDK."""
    repository = tmp_path / "repository"
    worktree = repository / ".worktrees" / "camera"
    worktree.mkdir(parents=True)
    wrapper = repository / "toupcamsdk.20250623" / "python" / "toupcam.py"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("SOURCE = 'linked-worktree-sdk'\n", encoding="utf-8")
    dll = repository / "toupcamsdk.20250623" / "win" / "x64" / "toupcam.dll"
    dll.parent.mkdir(parents=True)
    dll.touch()
    unrelated_cwd = tmp_path / "unrelated-cwd"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)
    monkeypatch.setattr("touptek4040.toupcam_loader.sys.executable", str(worktree / ".venv64" / "Scripts" / "python.exe"))

    sdk = load_default_toupcam_module()

    assert sdk.SOURCE == "linked-worktree-sdk"


def test_default_loader_propagates_runtime_wrapper_failure_without_fallback(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A located runtime SDK is authoritative even when its wrapper fails to import."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "toupcam.py").write_text("raise RuntimeError('runtime wrapper failed')\n", encoding="utf-8")
    (runtime / "toupcam.dll").touch()
    fallback = tmp_path / "fallback"
    wrapper = fallback / "toupcamsdk.20250623" / "python" / "toupcam.py"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("SOURCE = 'fallback'\n", encoding="utf-8")
    dll = fallback / "toupcamsdk.20250623" / "win" / "x64" / "toupcam.dll"
    dll.parent.mkdir(parents=True)
    dll.touch()
    monkeypatch.chdir(fallback)
    monkeypatch.setattr("touptek4040.toupcam_loader.sys.executable", str(runtime / "python.exe"))

    with pytest.raises(RuntimeError, match="runtime wrapper failed"):
        load_default_toupcam_module()
