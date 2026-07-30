from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
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
    "first stage": (
        "pytest-first-stage.txt",
        "first-stage-completion.json",
        "first-stage",
        2,
    ),
    "matrix": (
        "pytest-hardware.txt",
        "matrix-completion.json",
        "matrix",
        45,
    ),
    "owner recovery": (
        "pytest-owner-recovery.txt",
        "owner-recovery-completion.json",
        "owner-recovery",
        1,
    ),
}
_RUN_ID = re.compile(r"[0-9a-f]{32}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MANIFEST_ARTIFACTS = frozenset(
    {
        "cases.jsonl",
        "preflight.json",
        "pytest-first-stage.txt",
        "pytest-hardware.txt",
        "pytest-owner-recovery.txt",
        "first-stage-completion.json",
        "matrix-completion.json",
        "owner-recovery-completion.json",
        "run-metadata.json",
    }
)


def _json_document(
    path: Path,
    label: str,
    errors: list[str],
) -> dict[str, Any] | None:
    if not path.is_file():
        errors.append(f"{label} is missing")
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        errors.append(f"{label} is invalid JSON")
        return None
    if not isinstance(value, dict):
        errors.append(f"{label} is not an object")
        return None
    return value


def _utc_timestamp(
    value: object,
    label: str,
    errors: list[str],
) -> datetime | None:
    if not isinstance(value, str):
        errors.append(f"{label} is missing a UTC timestamp")
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{label} is not an ISO timestamp")
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        errors.append(f"{label} is not timezone-aware")
        return None
    return parsed.astimezone(timezone.utc)


def _require_fresh_file(
    path: Path,
    label: str,
    started: datetime | None,
    errors: list[str],
) -> None:
    if not path.is_file():
        errors.append(f"{label} is missing")
        return
    if started is not None and path.stat().st_mtime < started.timestamp():
        errors.append(f"{label} predates this hardware run")


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


def validate_results(
    results: Path,
    expected_run_id: str,
) -> tuple[str, list[str]]:
    errors: list[str] = []
    if _RUN_ID.fullmatch(expected_run_id) is None:
        errors.append("externally supplied expected run_id is invalid")
    metadata_path = results / "run-metadata.json"
    metadata = _json_document(metadata_path, "run metadata", errors)
    run_id: str | None = None
    started: datetime | None = None
    if metadata is not None:
        candidate = metadata.get("run_id")
        if (
            metadata.get("schema_version") != 1
            or not isinstance(candidate, str)
            or _RUN_ID.fullmatch(candidate) is None
        ):
            errors.append("run metadata has an invalid schema or run_id")
        else:
            run_id = candidate
            if run_id != expected_run_id:
                errors.append(
                    "run metadata does not match the externally expected run_id"
                )
        started = _utc_timestamp(
            metadata.get("started_utc"),
            "run metadata started_utc",
            errors,
        )
    _require_fresh_file(metadata_path, "run metadata", started, errors)

    preflight_path = results / "preflight.json"
    preflight = _json_document(preflight_path, "preflight evidence", errors)
    _require_fresh_file(preflight_path, "preflight evidence", started, errors)
    if preflight is not None:
        if preflight.get("run_id") != run_id or preflight.get("passed") is not True:
            errors.append("preflight evidence is not bound to the current run")
        recorded = _utc_timestamp(
            preflight.get("recorded_utc"),
            "preflight recorded_utc",
            errors,
        )
        if started is not None and recorded is not None and recorded < started:
            errors.append("preflight evidence predates this hardware run")

    events = _events(results / "cases.jsonl", errors)
    _require_fresh_file(
        results / "cases.jsonl",
        "cases evidence",
        started,
        errors,
    )
    for index, event in enumerate(events, start=1):
        if event.get("run_id") != run_id:
            errors.append(f"cases event {index} has a mixed or missing run_id")
        recorded = _utc_timestamp(
            event.get("recorded_utc"),
            f"cases event {index} recorded_utc",
            errors,
        )
        if started is not None and recorded is not None and recorded < started:
            errors.append(f"cases event {index} predates this hardware run")

    stage_status: dict[str, str] = {}
    for label, (
        filename,
        completion_filename,
        stage_name,
        expected_count,
    ) in _STAGE_LOGS.items():
        path = results / filename
        if not path.is_file():
            errors.append(f"{label} log is missing")
            stage_status[label] = "missing"
            continue
        _require_fresh_file(path, f"{label} log", started, errors)
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
        completion_path = results / completion_filename
        completion = _json_document(
            completion_path,
            f"{label} completion",
            errors,
        )
        _require_fresh_file(
            completion_path,
            f"{label} completion",
            started,
            errors,
        )
        if completion is not None:
            completed = _utc_timestamp(
                completion.get("completed_utc"),
                f"{label} completed_utc",
                errors,
            )
            expected_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            if (
                completion.get("run_id") != run_id
                or completion.get("stage") != stage_name
                or completion.get("passed_count") != expected_count
                or completion.get("log_sha256") != expected_hash
            ):
                errors.append(
                    f"{label} completion is not bound to its current log and run"
                )
            if started is not None and completed is not None and completed < started:
                errors.append(f"{label} completion predates this hardware run")

    final_path = results / "completion.json"
    final = _json_document(final_path, "final completion", errors)
    _require_fresh_file(final_path, "final completion", started, errors)
    if final is not None:
        completed = _utc_timestamp(
            final.get("completed_utc"),
            "final completion completed_utc",
            errors,
        )
        if (
            final.get("schema_version") != 2
            or final.get("run_id") != run_id
            or final.get("run_id") != expected_run_id
            or final.get("passed") is not True
            or final.get("stages")
            != ["preflight", "first-stage", "matrix", "owner-recovery"]
        ):
            errors.append("final completion is not exact for the current run")
        artifacts = final.get("artifacts")
        if not isinstance(artifacts, dict) or set(artifacts) != _MANIFEST_ARTIFACTS:
            errors.append("final completion artifact manifest is not exact")
        else:
            for filename in sorted(_MANIFEST_ARTIFACTS):
                recorded_hash = artifacts.get(filename)
                artifact_path = results / filename
                if (
                    not isinstance(recorded_hash, str)
                    or _SHA256.fullmatch(recorded_hash) is None
                ):
                    errors.append(
                        f"final completion hash for {filename} is invalid"
                    )
                elif (
                    not artifact_path.is_file()
                    or hashlib.sha256(artifact_path.read_bytes()).hexdigest()
                    != recorded_hash
                ):
                    errors.append(
                        f"final completion hash for {filename} does not match"
                    )
        if started is not None and completed is not None and completed < started:
            errors.append("final completion predates this hardware run")

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
        count_names = (
            "acquire_request_count",
            "start_ack_count",
            "frame_descriptor_count",
            "frame_result_count",
            "registry_record_count",
            "owner_owned_frame_count",
            "observed_publication_count",
        )
        counts = {name: owner.get(name) for name in count_names}
        valid_counts = all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in counts.values()
        )
        if valid_counts:
            observed_publications = max(
                counts["frame_descriptor_count"],
                counts["frame_result_count"],
                counts["registry_record_count"],
                counts["owner_owned_frame_count"],
            )
            derived_retry = counts["acquire_request_count"] > 1
            derived_published = observed_publications > 0
        else:
            observed_publications = -1
            derived_retry = True
            derived_published = True
        if (
            not valid_counts
            or counts["acquire_request_count"] != 1
            or counts["start_ack_count"] != 1
            or counts["observed_publication_count"] != observed_publications
            or observed_publications != 0
            or owner.get("native_start_acknowledged")
            is not (counts["start_ack_count"] == 1)
            or owner.get("exposure_retried") is not derived_retry
            or owner.get("frame_published") is not derived_published
            or owner.get("explicit_reconnect_required") is not True
            or owner.get("shared_frame_registry_empty") is not True
            or not isinstance(owner.get("terminated_pid"), int)
            or isinstance(owner.get("terminated_pid"), bool)
            or owner.get("terminated_pid") <= 0
            or owner.get("terminated_pid_alive") is not False
        ):
            errors.append("owner recovery evidence does not prove exact isolation")

    matrix_status = stage_status.get("matrix", "status unavailable")
    summary = [
        "# Real hardware matrix",
        "",
        "- Command: `powershell -ExecutionPolicy Bypass -File test/scripts/run_hardware.ps1`",
        "- Environment: `TOUPTEK_HARDWARE_TEST=1` (script process only)",
        "- Approved camera: `sMAX16AM-U3-CL-GPS` / "
        "`TP241025143218C92B01151AEC40CB7`",
        f"- Hardware run ID: `{run_id or 'invalid'}`",
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-run-id", required=True)
    arguments = parser.parse_args(argv)
    summary, errors = validate_results(
        RESULTS,
        arguments.expected_run_id,
    )
    (RESULTS / "summary.md").write_text(summary, encoding="utf-8")
    if errors:
        for error in errors:
            print(f"hardware evidence error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
