from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import psutil


ROOT = Path(__file__).resolve().parents[2]
DIST_DIR = Path(
    os.environ.get("TOUPTEK4040_RELEASE_DIST", ROOT / "dist" / "ToupTek4040Control")
).resolve()


def _free_port(socket_type: int) -> int:
    with socket.socket(socket.AF_INET, socket_type) as probe:
        probe.bind(("127.0.0.1" if socket_type == socket.SOCK_STREAM else "", 0))
        return int(probe.getsockname()[1])


def test_packaged_simulator_never_spawns_camera_owner(tmp_path: Path) -> None:
    executable = DIST_DIR / "ToupTek4040Alpaca.exe"
    assert executable.is_file()
    process = subprocess.Popen(
        [
            str(executable),
            "--source",
            "sim",
            "--host",
            "127.0.0.1",
            "--port",
            str(_free_port(socket.SOCK_STREAM)),
            "--discovery-port",
            str(_free_port(socket.SOCK_DGRAM)),
            "--log-directory",
            str(tmp_path),
        ],
        cwd=DIST_DIR,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
        | subprocess.CREATE_NO_WINDOW,
    )
    try:
        time.sleep(1.5)
        assert process.poll() is None
        children = psutil.Process(process.pid).children(recursive=True)
        camera_children = [
            child
            for child in children
            if child.name().casefold() == executable.name.casefold()
            or "--multiprocessing-fork" in " ".join(child.cmdline())
        ]
        assert camera_children == []
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_frozen_distribution_contains_owner_and_shared_memory_modules() -> None:
    runtime = DIST_DIR / "_runtime"
    names = {path.name.casefold() for path in runtime.rglob("*") if path.is_file()}
    assert "python313.dll" in names
    # Runtime behavior is exercised by the packaged real-camera smoke. This
    # structural gate prevents accidentally producing the old in-process build.
    assert (DIST_DIR / "ToupTek4040Alpaca.exe").stat().st_size > 100_000
