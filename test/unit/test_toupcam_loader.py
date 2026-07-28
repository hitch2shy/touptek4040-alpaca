from __future__ import annotations

import platform

import pytest

from touptek4040.toupcam_loader import load_toupcam_module, validate_runtime_architecture


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
