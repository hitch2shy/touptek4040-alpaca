from __future__ import annotations

import argparse
from pathlib import Path
import threading
import time

import run_alpaca

from test.hardware.conftest import (
    EXPECTED_SERIAL,
    HardwareEvidence,
    HardwareSession,
    open_verified_target,
)
from touptek4040.toupcam_loader import load_toupcam_module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[2]
    sdk = load_toupcam_module(root)
    evidence = HardwareEvidence(root / "test" / "results" / "conformu")
    identity = open_verified_target(sdk, evidence)
    guard = HardwareSession(sdk, identity, evidence)
    guard.connect_and_snapshot()
    guard.camera.disconnect()

    config = run_alpaca.config_from_args(
        [
            "--source",
            "toupcam",
            "--serial",
            EXPECTED_SERIAL,
            "--host",
            "127.0.0.1",
            "--port",
            str(args.port),
            "--discovery-port",
            "32228",
            "--log-directory",
            str(root / "test" / "results" / "conformu" / "server-logs"),
            "--no-web",
        ]
    )
    server = None

    def stop_when_requested() -> None:
        while not args.stop_file.exists():
            time.sleep(0.1)
        if server is not None:
            server.shutdown()

    try:
        server = run_alpaca.build_server(config, start_discovery=False)
        args.ready_file.write_text(
            f"http://127.0.0.1:{server.port}/api/v1/camera/0\n",
            encoding="utf-8",
        )
        watcher = threading.Thread(target=stop_when_requested, daemon=True)
        watcher.start()
        server.serve_forever()
        return 0
    finally:
        if server is not None:
            server.shutdown()
        guard.restore_and_close()


if __name__ == "__main__":
    raise SystemExit(main())
