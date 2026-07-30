"""Attempt to reopen every observed shared-memory mapping."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from touptek4040.release_probe import probe_live_names


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observed", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    arguments = parser.parse_args()
    observed = json.loads(arguments.observed.read_text(encoding="utf-8"))[
        "observed_names"
    ]
    result = {
        "probed_names": sorted(set(observed)),
        "live_segment_names": probe_live_names(observed),
    }
    arguments.result.write_text(
        json.dumps(result, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
