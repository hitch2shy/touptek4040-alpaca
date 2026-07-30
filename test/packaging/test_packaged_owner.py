from __future__ import annotations

import os
import json
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


def test_frozen_executable_spawns_child_and_round_trips_shared_memory(
    tmp_path: Path,
) -> None:
    executable = DIST_DIR / "ToupTek4040Alpaca.exe"
    result_path = tmp_path / "owner-probe.json"
    completed = subprocess.run(
        [str(executable), "--release-owner-probe", str(result_path)],
        cwd=DIST_DIR,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["parent_pid"] != result["child_pid"]
    assert result["generation"] == 1
    assert result["publication_count"] == 1
    assert result["ack_count"] == 1
    assert result["payload_round_trip"] is True
    assert result["child_dead"] is True
    assert result["registry_names"] == []
    assert result["live_segment_names"] == []
