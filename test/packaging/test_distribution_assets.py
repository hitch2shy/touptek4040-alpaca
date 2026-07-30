from __future__ import annotations

import json
import os
import zipfile
import subprocess
from pathlib import Path

from touptek4040.config import load_config


ROOT = Path(__file__).resolve().parents[2]
DIST_DIR = Path(
    os.environ.get("TOUPTEK4040_RELEASE_DIST", ROOT / "dist" / "ToupTek4040Control")
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
    "RELEASE-INPUTS.json",
    "_runtime",
}


def _public_files(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and not path.relative_to(root).parts[0].casefold() == "logs"
    }


def test_formal_distribution_contains_exact_public_contract() -> None:
    assert DIST_DIR.is_dir(), f"build the formal release first: {DIST_DIR}"
    names = {path.name for path in DIST_DIR.iterdir()}
    assert REQUIRED_ROOT <= names
    assert "touptek4040.ini" not in names
    assert not any(path.suffix.casefold() == ".lib" for path in DIST_DIR.rglob("*"))
    assert not any(path.name.casefold() == "toupcam.py" for path in DIST_DIR.rglob("*"))
    assert not any(part.casefold() in {"test", "tests"} for path in DIST_DIR.rglob("*") for part in path.relative_to(DIST_DIR).parts)
    dlls = [path for path in DIST_DIR.rglob("toupcam.dll")]
    assert len(dlls) == 1


def test_distributed_json_has_all_owner_defaults_and_exact_target() -> None:
    path = DIST_DIR / "touptek4040.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["camera"] == {
        "source": "toupcam",
        "serial": EXPECTED_SERIAL,
        "readout_mode": "RAW 12-bit",
        "gain": None,
    }
    assert raw["owner"] == {
        "startup_timeout_s": 10.0,
        "connect_timeout_s": 20.0,
        "command_timeout_s": 5.0,
        "exposure_overhead_s": 10.0,
        "exposure_multiplier": 1.02,
        "structural_reopen_settle_s": 0.5,
        "abort_timeout_s": 2.0,
        "disconnect_timeout_s": 10.0,
    }
    config = load_config(path, {})
    assert config.logging.directory == (DIST_DIR / "logs").resolve()


def test_build_identity_labels_formal_release() -> None:
    fields = dict(
        line.split(":", 1)
        for line in (DIST_DIR / "BUILD-INFO.txt").read_text(
            encoding="utf-8-sig"
        ).splitlines()
    )
    assert fields["Designation"].strip() == "FORMAL RELEASE"
    source_commit = fields["SourceCommit"].strip()
    assert len(source_commit) == 40
    manifest = DIST_DIR / "RELEASE-INPUTS.json"
    assert fields["ReleaseInputsSHA256"].strip() == __import__(
        "hashlib"
    ).sha256(manifest.read_bytes()).hexdigest().upper()


def test_release_input_manifest_matches_exact_committed_source() -> None:
    manifest = json.loads((DIST_DIR / "RELEASE-INPUTS.json").read_text("utf-8"))
    source_commit = manifest["source_commit"]
    assert subprocess.run(
        ["git", "cat-file", "-e", f"{source_commit}^{{commit}}"],
        cwd=ROOT,
        check=False,
    ).returncode == 0
    assert manifest["files"]
    for entry in manifest["files"]:
        blob = subprocess.run(
            ["git", "show", f"{source_commit}:{entry['path']}"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
        assert __import__("hashlib").sha256(blob).hexdigest() == entry["sha256"]


def test_formal_zip_exactly_matches_distribution() -> None:
    assert ZIP_PATH.is_file()
    with zipfile.ZipFile(ZIP_PATH) as archive:
        actual = {
            Path(name).relative_to(DIST_DIR.name).as_posix()
            for name in archive.namelist()
            if not name.endswith("/")
        }
    assert actual == _public_files(DIST_DIR)
