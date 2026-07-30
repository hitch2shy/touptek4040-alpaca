from __future__ import annotations

import json

import pytest

from test.scripts.summarize_hardware import validate_results


def _valid_results(tmp_path):
    events = [
        {"case": "first-stage", "shape": [16, 16], "readout_mode": "RAW12"},
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
            "acquire_operation_count": 1,
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
    (tmp_path / "cases.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    for name, count in (
        ("pytest-first-stage.txt", 2),
        ("pytest-hardware.txt", 45),
        ("pytest-owner-recovery.txt", 1),
    ):
        (tmp_path / name).write_text(
            f"================ {count} passed in 1.00s ================\n",
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
        owner["acquire_operation_count"] = 2
    elif mutation == "missing-structural-heat":
        structural = next(
            event
            for event in events
            if event.get("case") == "structural-reopen-state"
        )
        structural["native_options"] = {}
    else:
        (tmp_path / "pytest-owner-recovery.txt").write_text(
            "1 failed in 1.00s\n",
            encoding="utf-8",
        )
    (tmp_path / "cases.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )

    _summary, errors = validate_results(tmp_path)

    assert errors
