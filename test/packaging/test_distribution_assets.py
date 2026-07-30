from __future__ import annotations

import json
import os
import re
import zipfile
from pathlib import Path

from touptek4040.config import load_config


ROOT = Path(__file__).resolve().parents[2]
DIST_DIR = Path(
    os.environ.get("TOUPTEK4040_TEST_DIST", ROOT / "dist" / "ToupTek4040-Test")
).resolve()
ZIP_PATH = DIST_DIR.with_suffix(".zip")
EXPECTED_SERIAL = "TP241025143218C92B01151AEC40CB7"
REQUIRED_ROOT = {
    "ToupTek4040GUI.exe",
    "ToupTek4040Alpaca.exe",
    "toupcam.dll",
    "touptek4040.json",
    "使用说明.txt",
    "BUILD-INFO.txt",
    "_runtime",
}
MUTABLE_RUNTIME_ROOTS = {"logs"}


def _immutable_public_files(root: Path) -> set[str]:
    files: set[str] = set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0].casefold() in MUTABLE_RUNTIME_ROOTS:
            continue
        files.add(relative.as_posix())
    return files


def test_distribution_contains_public_contract() -> None:
    assert DIST_DIR.is_dir(), f"build the temporary package first: {DIST_DIR}"
    names = {path.name for path in DIST_DIR.iterdir()}
    assert REQUIRED_ROOT <= names
    assert "touptek4040.ini" not in names
    assert not any(path.suffix.casefold() == ".lib" for path in DIST_DIR.rglob("*"))
    assert not any(path.name.casefold() == "toupcam.py" for path in DIST_DIR.rglob("*"))
    assert not any("test" in path.parts for path in DIST_DIR.rglob("*"))


def test_distributed_json_loads_portably_with_approved_identity() -> None:
    config_path = DIST_DIR / "touptek4040.json"
    parsed = json.loads(config_path.read_text(encoding="utf-8"))
    assert not Path(parsed["logging"]["directory"]).is_absolute()

    config = load_config(config_path, {})
    assert config.camera.source == "toupcam"
    assert config.camera.serial == EXPECTED_SERIAL
    assert config.logging.directory == (DIST_DIR / "logs").resolve()
    assert config.owner.startup_timeout_s == 10.0
    assert config.owner.connect_timeout_s == 20.0
    assert config.owner.command_timeout_s == 5.0
    assert config.owner.exposure_multiplier == 1.02
    assert config.owner.exposure_overhead_s == 10.0
    assert config.owner.abort_timeout_s == 2.0
    assert config.owner.disconnect_timeout_s == 10.0


def test_build_identity_is_structured_and_labels_temporary_dirty_build() -> None:
    fields = {}
    for line in (DIST_DIR / "BUILD-INFO.txt").read_text(encoding="utf-8-sig").splitlines():
        key, separator, value = line.partition(":")
        assert separator == ":"
        fields[key.strip()] = value.strip()
    assert fields["Designation"] == "TEMPORARY TEST BUILD"
    assert re.fullmatch(r"[0-9a-f]{40}", fields["Commit"])
    assert fields["Dirty"] in {"true", "false"}
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?[+-]\d{2}:\d{2}",
        fields["Built"],
    )


def test_zip_matches_the_public_distribution_tree() -> None:
    assert ZIP_PATH.is_file()
    expected = _immutable_public_files(DIST_DIR)
    with zipfile.ZipFile(ZIP_PATH) as archive:
        actual = {
            Path(name).relative_to("ToupTek4040-Test").as_posix()
            for name in archive.namelist()
            if not name.endswith("/")
        }
    assert actual == expected


def test_runtime_logs_are_not_part_of_the_immutable_package_contract(
    tmp_path: Path,
) -> None:
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "runtime.log").write_text("", encoding="utf-8")
    (tmp_path / "asset.txt").write_text("asset", encoding="utf-8")

    assert _immutable_public_files(tmp_path) == {"asset.txt"}
