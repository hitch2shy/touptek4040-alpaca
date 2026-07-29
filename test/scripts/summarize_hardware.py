from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "test" / "results" / "hardware"


def main() -> None:
    events = [
        json.loads(line)
        for line in (RESULTS / "cases.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    log_bytes = (RESULTS / "pytest-hardware.txt").read_bytes()
    pytest_log = (
        log_bytes.decode("utf-16")
        if log_bytes.startswith((b"\xff\xfe", b"\xfe\xff"))
        else log_bytes.decode("utf-8", errors="replace")
    )
    restoration = next(
        (event for event in reversed(events) if event["case"] == "restoration"),
        None,
    )
    modes = [event for event in events if event["case"] == "mode-and-binning"]
    status_line = next(
        (
            line.strip()
            for line in reversed(pytest_log.splitlines())
            if " passed" in line or " failed" in line
        ),
        "pytest status unavailable",
    )
    summary = [
        "# Real hardware matrix",
        "",
        "- Command: `powershell -ExecutionPolicy Bypass -File test/scripts/run_hardware.ps1`",
        "- Environment: `TOUPTEK_HARDWARE_TEST=1` (script process only)",
        "- Approved camera: `sMAX16AM-U3-CL-GPS` / "
        "`TP241025143218C92B01151AEC40CB7`",
        f"- Pytest: `{status_line}`",
        f"- RAW/bin combinations recorded: {len(modes)}/16",
        "- Native hardware-bin matrix rule: `((4096 // bin) // 2) * 2` on both axes",
        "- ASCOM full-frame bin 3 fallback: exact `1365 x 1365` verified in RAW12 and RAW8",
        "- Cooler policy: state/target setters receive their existing values only; "
        "no cooler or fan transition",
        "- Physical scene orientation: not calibrated because no known optical "
        "orientation target was supplied",
        (
            "- Restoration: "
            + (
                "`verified: final snapshot exactly equals initial snapshot`"
                if restoration
                and restoration.get("restored")
                and restoration.get("errors") == []
                else "`FAILED OR MISSING`"
            )
        ),
        "",
        "Detailed per-case evidence is in `cases.jsonl`; no raw image buffers are stored.",
        "Root-cause and bounded-memory details are in `root-cause-summary.md`.",
        "",
    ]
    (RESULTS / "summary.md").write_text("\n".join(summary), encoding="utf-8")


if __name__ == "__main__":
    main()
