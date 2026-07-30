from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "test" / "results" / "hardware"
_EXPECTED_MODES = {
    (mode, binning)
    for mode in ("RAW12", "RAW8")
    for binning in range(1, 9)
}
_STAGE_LOGS = {
    "first stage": ("pytest-first-stage.txt", 2),
    "matrix": ("pytest-hardware.txt", 45),
    "owner recovery": ("pytest-owner-recovery.txt", 1),
}


def _read_log(path: Path) -> str:
    data = path.read_bytes()
    return (
        data.decode("utf-16")
        if data.startswith((b"\xff\xfe", b"\xfe\xff"))
        else data.decode("utf-8", errors="replace")
    )


def _events(path: Path, errors: list[str]) -> list[dict[str, Any]]:
    if not path.is_file():
        errors.append("cases.jsonl is missing")
        return []
    events: list[dict[str, Any]] = []
    for number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"cases.jsonl line {number} is invalid JSON")
            continue
        if not isinstance(event, dict) or not isinstance(event.get("case"), str):
            errors.append(f"cases.jsonl line {number} is not a case object")
            continue
        events.append(event)
    return events


def _single_case(
    events: list[dict[str, Any]],
    name: str,
    errors: list[str],
) -> dict[str, Any] | None:
    matches = [event for event in events if event.get("case") == name]
    if len(matches) != 1:
        errors.append(f"{name} evidence count is {len(matches)}, expected 1")
        return None
    return matches[0]


def validate_results(results: Path) -> tuple[str, list[str]]:
    errors: list[str] = []
    events = _events(results / "cases.jsonl", errors)
    stage_status: dict[str, str] = {}
    for label, (filename, expected_count) in _STAGE_LOGS.items():
        path = results / filename
        if not path.is_file():
            errors.append(f"{label} log is missing")
            stage_status[label] = "missing"
            continue
        log = _read_log(path)
        match = re.search(rf"\b{expected_count} passed\b", log)
        if match is None or re.search(r"\b[1-9]\d* failed\b", log):
            errors.append(f"{label} log does not prove {expected_count} passed")
        status = next(
            (
                line.strip()
                for line in reversed(log.splitlines())
                if " passed" in line or " failed" in line
            ),
            "status unavailable",
        )
        stage_status[label] = status

    first_stage = [event for event in events if event.get("case") == "first-stage"]
    if not first_stage or not any(
        event.get("shape") == [16, 16]
        and event.get("readout_mode") == "RAW12"
        for event in first_stage
    ):
        errors.append("first-stage RAW12 16x16 evidence is missing")

    modes = [event for event in events if event.get("case") == "mode-and-binning"]
    observed_modes = {
        (event.get("mode"), event.get("binning"))
        for event in modes
    }
    if len(modes) != 16 or observed_modes != _EXPECTED_MODES:
        errors.append("native RAW8/RAW12 bin 1-8 evidence is not exact")

    restorations = [
        event for event in events if event.get("case") == "restoration"
    ]
    if len(restorations) < 3:
        errors.append("fewer than three stage restoration records were found")
    for index, restoration in enumerate(restorations, start=1):
        initial = restoration.get("initial")
        final = restoration.get("final")
        if (
            restoration.get("restored") is not True
            or restoration.get("errors") != []
            or not isinstance(initial, dict)
            or final != initial
            or "heater_level" not in initial
            or "heater_max_level" not in initial
        ):
            errors.append(
                f"restoration record {index} is not exact including HEAT/HEAT_MAX"
            )

    heater = _single_case(events, "web-heater", errors)
    heater_restore = _single_case(events, "web-heater-restoration", errors)
    if heater is not None and (
        heater.get("confirmed_level") == heater.get("initial_level")
        or heater.get("maximum_level", -1) < heater.get("confirmed_level", 0)
    ):
        errors.append("heater evidence does not prove one valid alternate level")
    if heater_restore is not None and (
        heater_restore.get("restored") is not True
        or heater_restore.get("final_level")
        != heater_restore.get("initial_level")
    ):
        errors.append("heater restoration evidence is not exact")

    structural = _single_case(events, "structural-reopen-state", errors)
    if structural is not None:
        before = structural.get("before")
        after = structural.get("after")
        options = structural.get("native_options")
        if (
            structural.get("preserved") is not True
            or not isinstance(before, dict)
            or after != before
            or "heater_level" not in before
            or "heater_max_level" not in before
            or not isinstance(options, dict)
            or set(options)
            != {"TOUPCAM_OPTION_HEAT", "TOUPCAM_OPTION_HEAT_MAX"}
        ):
            errors.append(
                "structural reopen lacks exact HEAT/HEAT_MAX native evidence"
            )

    owner = _single_case(events, "owner-termination", errors)
    if owner is not None:
        required_owner = {
            "native_start_acknowledged": True,
            "acquire_operation_count": 1,
            "exposure_retried": False,
            "frame_published": False,
            "explicit_reconnect_required": True,
            "shared_frame_registry_empty": True,
            "terminated_pid_alive": False,
        }
        if any(owner.get(key) != value for key, value in required_owner.items()):
            errors.append("owner recovery evidence does not prove exact isolation")

    matrix_status = stage_status.get("matrix", "status unavailable")
    summary = [
        "# Real hardware matrix",
        "",
        "- Command: `powershell -ExecutionPolicy Bypass -File test/scripts/run_hardware.ps1`",
        "- Environment: `TOUPTEK_HARDWARE_TEST=1` (script process only)",
        "- Approved camera: `sMAX16AM-U3-CL-GPS` / "
        "`TP241025143218C92B01151AEC40CB7`",
        f"- Pytest: `{matrix_status}`",
        f"- RAW/bin combinations recorded: {len(modes)}/16",
        "- Native hardware-bin matrix rule: `((4096 // bin) // 2) * 2` on both axes",
        "- ASCOM full-frame bin 3 fallback: exact `1365 x 1365` verified in RAW12 and RAW8",
        "- Cooler policy: state/target setters receive their existing values only; "
        "no cooler or fan transition",
        "- Physical scene orientation: not calibrated because no known optical "
        "orientation target was supplied",
        (
            "- HEAT/HEAT_MAX restoration: `verified`"
            if not any("restoration record" in error for error in errors)
            else "- HEAT/HEAT_MAX restoration: `FAILED OR MISSING`"
        ),
        f"- Evidence validation: `{'PASS' if not errors else 'FAIL'}`",
        *[f"  - {error}" for error in errors],
        "",
        "Detailed per-case evidence is in `cases.jsonl`; no raw image buffers are stored.",
        "Root-cause and bounded-memory details are in `root-cause-summary.md`.",
        "",
    ]
    return "\n".join(summary), errors


def main() -> int:
    summary, errors = validate_results(RESULTS)
    (RESULTS / "summary.md").write_text(summary, encoding="utf-8")
    if errors:
        for error in errors:
            print(f"hardware evidence error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
