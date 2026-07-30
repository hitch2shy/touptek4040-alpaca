"""Observe every shared-frame name visible in registry snapshots."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time


def _names(path: Path) -> list[str]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return []
    frames = document.get("frames") if isinstance(document, dict) else None
    if not isinstance(frames, list):
        return []
    return [
        frame["name"]
        for frame in frames
        if isinstance(frame, dict) and isinstance(frame.get("name"), str)
    ]


def _write(path: Path, observed: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps({"observed_names": sorted(observed)}) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    arguments = parser.parse_args()

    observed: set[str] = set()
    _write(arguments.output, observed)
    while not arguments.stop_file.exists():
        observed.update(_names(arguments.registry))
        time.sleep(0.001)
    observed.update(_names(arguments.registry))
    _write(arguments.output, observed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
