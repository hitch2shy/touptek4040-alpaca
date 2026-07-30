from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
OFFICIAL_SDK = ROOT.parent.parent / "toupcamsdk.20250623"
BUILD_SCRIPT = ROOT / "test" / "scripts" / "build_release.ps1"
DIST = ROOT / "dist" / "ToupTek4040Control"
ZIP = DIST.with_suffix(".zip")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "relative_path",
    ("python/toupcam.py", "win/x64/toupcam.dll"),
)
def test_build_rejects_changed_official_sdk_before_touching_dist(
    tmp_path: Path,
    relative_path: str,
) -> None:
    sdk = tmp_path / "toupcamsdk.20250623"
    for relative in ("python/toupcam.py", "win/x64/toupcam.dll"):
        target = sdk / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(OFFICIAL_SDK / relative, target)
    changed = sdk / relative_path
    payload = bytearray(changed.read_bytes())
    payload[len(payload) // 2] ^= 0x01
    changed.write_bytes(payload)

    before_info = _sha256(DIST / "BUILD-INFO.txt")
    before_zip = _sha256(ZIP)
    completed = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(BUILD_SCRIPT),
            "-SdkRoot",
            str(sdk),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "Official SDK lock mismatch" in output
    assert _sha256(DIST / "BUILD-INFO.txt") == before_info
    assert _sha256(ZIP) == before_zip


def test_distribution_records_exact_locked_external_sdk() -> None:
    manifest = json.loads((DIST / "RELEASE-INPUTS.json").read_text("utf-8"))
    external = manifest["external_sdk"]
    assert external["lock_path"] == "official-touptek-sdk.lock.json"
    assert external["files"] == [
        {
            "path": "python/toupcam.py",
            "size": 171655,
            "version": "59.28760.20250623",
            "sha256": (
                "b472d455d4506a3ca642a1df0b77986f"
                "ea42d97f7deffe1ea33b6b1bb06f5520"
            ),
        },
        {
            "path": "win/x64/toupcam.dll",
            "size": 27495712,
            "version": "59.28760.20250623",
            "sha256": (
                "f81d9705c1c6af3b5d5c07fedec2cf09"
                "a8a113dc6d6efebd58798e9b12c04460"
            ),
        },
    ]
    fields = dict(
        line.split(":", 1)
        for line in (DIST / "BUILD-INFO.txt").read_text(
            encoding="utf-8-sig"
        ).splitlines()
    )
    assert fields["OfficialSdkWrapperSize"].strip() == "171655"
    assert fields["OfficialSdkWrapperSHA256"].strip().casefold() == external[
        "files"
    ][0]["sha256"]
    assert fields["OfficialSdkDllSize"].strip() == "27495712"
    assert fields["OfficialSdkDllSHA256"].strip().casefold() == external[
        "files"
    ][1]["sha256"]
    assert fields["OfficialSdkInputsSHA256"].strip().casefold() == external[
        "aggregate_sha256"
    ]
    aggregate = "".join(
        (
            f"{entry['path']}|{entry['size']}|{entry['version']}|"
            f"{entry['sha256']}\n"
        )
        for entry in external["files"]
    ).encode("utf-8")
    assert hashlib.sha256(aggregate).hexdigest() == external[
        "aggregate_sha256"
    ]
    lock = ROOT / external["lock_path"]
    assert _sha256(lock) == external["lock_sha256"]
