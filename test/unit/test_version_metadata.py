from __future__ import annotations

from pathlib import Path
import tomllib


def test_runtime_and_management_use_one_package_version_source():
    from touptek4040 import __version__
    from touptek4040.alpaca import management
    from touptek4040.alpaca.common_api import DRIVER_VERSION

    assert __version__ == "0.1.0"
    assert DRIVER_VERSION == __version__
    assert management.MANUFACTURER_VERSION == __version__


def test_packaging_metadata_reads_version_from_package_attribute():
    root = Path(__file__).resolve().parents[2]
    pyproject = tomllib.loads(
        (root / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert "version" not in pyproject["project"]
    assert pyproject["project"]["dynamic"] == ["version"]
    assert pyproject["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "touptek4040.__version__"
    }
