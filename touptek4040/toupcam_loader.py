"""Deterministic loading of the bundled 64-bit ToupTek Python SDK."""

from __future__ import annotations

import importlib.util
import os
import platform
import struct
import sys
from pathlib import Path
from types import ModuleType


def validate_runtime_architecture() -> None:
    """Ensure the interpreter can load the required x64 native library."""
    if struct.calcsize("P") != 8 or platform.architecture()[0] != "64bit":
        raise RuntimeError("ToupTek4040 requires 64-bit Python and 64-bit toupcam.dll")


def load_toupcam_module(base_dir: Path) -> ModuleType:
    """Load the SDK wrapper beside the x64 DLL, preferring runtime files."""
    validate_runtime_architecture()
    for root in _search_roots(base_dir):
        wrapper = root / "toupcam.py"
        dll = root / "toupcam.dll"
        if wrapper.is_file() and dll.is_file():
            return _load_wrapper(wrapper, dll.parent)

        sdk_root = root / "toupcamsdk.20250623"
        sdk_dll = sdk_root / "win" / "x64" / "toupcam.dll"
        sdk_wrapper = sdk_root / "python" / "toupcam.py"
        if sdk_wrapper.is_file() and sdk_dll.is_file():
            return _load_wrapper(sdk_wrapper, sdk_dll.parent)
    raise RuntimeError("could not locate toupcam.py and 64-bit toupcam.dll")


def load_default_toupcam_module() -> ModuleType:
    """Load from the runtime first, then from an active development worktree."""
    runtime_dir = Path(sys.executable).resolve().parent
    try:
        return load_toupcam_module(runtime_dir)
    except RuntimeError as runtime_error:
        try:
            return load_toupcam_module(Path.cwd())
        except RuntimeError:
            raise runtime_error


def _search_roots(base_dir: Path) -> tuple[Path, ...]:
    root = base_dir.resolve()
    roots = [root]
    # Linked development worktrees share their SDK only with the repository root.
    if root.parent.name == ".worktrees":
        roots.append(root.parent.parent)
    return tuple(roots)


def _load_wrapper(wrapper: Path, dll_directory: Path) -> ModuleType:
    dll_directory_cookie = None
    if os.name == "nt":
        dll_directory_cookie = os.add_dll_directory(str(dll_directory))
    module_name = "_touptek4040_toupcam"
    existing = sys.modules.get(module_name)
    if existing is not None and Path(getattr(existing, "__file__", "")) == wrapper:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, wrapper)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load ToupTek wrapper from {wrapper}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    # The object returned by add_dll_directory removes the directory on finalization.
    # Keep it on the imported module because the wrapper loads the DLL lazily.
    module._touptek4040_dll_directory_cookie = dll_directory_cookie
    return module
