from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os

import pytest

from test.scripts.summarize_hardware import validate_results


_RUN_ID = "0123456789abcdef0123456789abcdef"
_STARTED = datetime.now(timezone.utc) - timedelta(seconds=5)


def _valid_results(tmp_path):
    events = [
        {
            "case": "first-stage",
            "shape": [16, 16],
            "readout_mode": "RAW12",
        },
        *[
            {"case": "mode-and-binning", "mode": mode, "binning": binning}
            for mode in ("RAW12", "RAW8")
            for binning in range(1, 9)
        ],
        {
            "case": "web-heater",
            "initial_level": 4,
            "confirmed_level": 0,
            "maximum_level": 4,
        },
        {
            "case": "web-heater-restoration",
            "initial_level": 4,
            "final_level": 4,
            "restored": True,
        },
        {
            "case": "structural-reopen-state",
            "before": {"heater_level": 4, "heater_max_level": 4},
            "after": {"heater_level": 4, "heater_max_level": 4},
            "native_options": {
                "TOUPCAM_OPTION_HEAT": 45,
                "TOUPCAM_OPTION_HEAT_MAX": 46,
            },
            "preserved": True,
        },
        {
            "case": "owner-termination",
            "native_start_acknowledged": True,
            "acquire_request_count": 1,
            "start_ack_count": 1,
            "frame_descriptor_count": 0,
            "frame_result_count": 0,
            "registry_record_count": 0,
            "owner_owned_frame_count": 0,
            "observed_publication_count": 0,
            "exposure_retried": False,
            "frame_published": False,
            "explicit_reconnect_required": True,
            "shared_frame_registry_empty": True,
            "terminated_pid_alive": False,
        },
        *[
            {
                "case": "restoration",
                "restored": True,
                "errors": [],
                "initial": {"heater_level": 4, "heater_max_level": 4},
                "final": {"heater_level": 4, "heater_max_level": 4},
            }
            for _ in range(3)
        ],
    ]
    recorded = (_STARTED + timedelta(seconds=1)).isoformat()
    for event in events:
        event["run_id"] = _RUN_ID
        event["recorded_utc"] = recorded
    (tmp_path / "cases.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    (tmp_path / "run-metadata.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": _RUN_ID,
                "started_utc": _STARTED.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "preflight.json").write_text(
        json.dumps(
            {
                "run_id": _RUN_ID,
                "recorded_utc": recorded,
                "passed": True,
                "enum_count": 1,
                "raw_ids_nonblank": True,
            }
        ),
        encoding="utf-8",
    )
    for stage, name, count, completion_name in (
        ("first-stage", "pytest-first-stage.txt", 2, "first-stage-completion.json"),
        ("matrix", "pytest-hardware.txt", 45, "matrix-completion.json"),
        (
            "owner-recovery",
            "pytest-owner-recovery.txt",
            1,
            "owner-recovery-completion.json",
        ),
    ):
        log = tmp_path / name
        log.write_text(
            f"================ {count} passed in 1.00s ================\n",
            encoding="utf-8",
        )
        (tmp_path / completion_name).write_text(
            json.dumps(
                {
                    "run_id": _RUN_ID,
                    "stage": stage,
                    "passed_count": count,
                    "completed_utc": recorded,
                    "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
                }
            ),
            encoding="utf-8",
        )
    (tmp_path / "completion.json").write_text(
        json.dumps(
            {
                "run_id": _RUN_ID,
                "completed_utc": recorded,
                "passed": True,
                "stages": [
                    "preflight",
                    "first-stage",
                    "matrix",
                    "owner-recovery",
                ],
            }
        ),
        encoding="utf-8",
    )
    return events


def test_hardware_summary_accepts_only_complete_evidence(tmp_path) -> None:
    _valid_results(tmp_path)

    summary, errors = validate_results(tmp_path)

    assert errors == []
    assert "RAW/bin combinations recorded: 16/16" in summary
    assert "HEAT/HEAT_MAX restoration: `verified`" in summary


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-mode",
        "bad-heater-restoration",
        "missing-owner",
        "duplicate-owner-acquire",
        "missing-structural-heat",
        "failed-stage-log",
        "stale-previous-complete",
        "mixed-run-id",
        "old-artifact-mtime",
        "missing-completion",
    ],
)
def test_hardware_summary_rejects_false_pass_mutations(
    tmp_path,
    mutation: str,
) -> None:
    events = _valid_results(tmp_path)
    if mutation == "missing-mode":
        events.remove(
            next(
                event
                for event in events
                if event.get("case") == "mode-and-binning"
                and event.get("mode") == "RAW8"
                and event.get("binning") == 8
            )
        )
    elif mutation == "bad-heater-restoration":
        restoration = next(
            event for event in events if event.get("case") == "restoration"
        )
        restoration["final"] = {"heater_level": 0, "heater_max_level": 4}
    elif mutation == "missing-owner":
        events.remove(
            next(
                event for event in events if event.get("case") == "owner-termination"
            )
        )
    elif mutation == "duplicate-owner-acquire":
        owner = next(
            event for event in events if event.get("case") == "owner-termination"
        )
        owner["acquire_request_count"] = 2
    elif mutation == "missing-structural-heat":
        structural = next(
            event
            for event in events
            if event.get("case") == "structural-reopen-state"
        )
        structural["native_options"] = {}
    elif mutation == "failed-stage-log":
        (tmp_path / "pytest-owner-recovery.txt").write_text(
            "1 failed in 1.00s\n",
            encoding="utf-8",
        )
    elif mutation == "stale-previous-complete":
        (tmp_path / "run-metadata.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "fedcba9876543210fedcba9876543210",
                    "started_utc": datetime.now(timezone.utc).isoformat(),
                }
            ),
            encoding="utf-8",
        )
    elif mutation == "mixed-run-id":
        events[0]["run_id"] = "fedcba9876543210fedcba9876543210"
    elif mutation == "old-artifact-mtime":
        old = _STARTED.timestamp() - 1
        os.utime(tmp_path / "pytest-hardware.txt", (old, old))
    else:
        (tmp_path / "completion.json").unlink()
    (tmp_path / "cases.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )

    _summary, errors = validate_results(tmp_path)

    assert errors
