from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os

from test.scripts.summarize_conformu import evaluate_results


_IMAGE_UNKNOWN = (
    "Error when reading ImageArrayVariant: ToImageArray - "
    "ImageArrayElementType is 0, meaning ImageArrayElementTypes.Unknown"
)
_IMAGE_NULL = (
    "Unexpected error while processing ImageArrayVariant: "
    "Object reference not set to an instance of an object."
)
_PUT_ENDPOINTS = (
    "AbortExposure",
    "BinX",
    "BinY",
    "CoolerOn",
    "FastReadout",
    "Gain",
    "NumX",
    "NumY",
    "Offset",
    "PulseGuide",
    "ReadoutMode",
    "SetCCDTemperature",
    "StartExposure",
    "StartX",
    "StartY",
    "StopExposure",
    "SubExposureDuration",
)


def _write_fresh_results(path):
    started = datetime.now(timezone.utc) - timedelta(seconds=1)
    metadata = {
        "profiles": {
            "protocol": {
                "command": "alpacaprotocol",
                "port": 11113,
                "started_utc": started.isoformat(),
                "exit_code": 0,
            },
            "camera-v4": {
                "command": "conformance",
                "port": 11114,
                "started_utc": started.isoformat(),
                "exit_code": 17,
            },
        }
    }
    (path / "run-metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (path / "protocol.log").write_text(
        "Connecting to device: 127.0.0.1:11113\n"
        "Found 0 errors, 0 issues and 4 information messages.\n",
        encoding="utf-8",
    )
    (path / "camera-v4.log").write_text(
        "Alpaca device: Camera (127.0.0.1:11114 Camera/0)\n"
        "Conformance test has finished\n",
        encoding="utf-8",
    )
    issues = [
        {"Key": "PercentCompleted Read", "Value": "Unexpected error: PercentCompleted is unavailable outside an exposure"}
    ]
    for _ in range(8):
        issues.extend(
            [
                {"Key": "ImageArrayVariant", "Value": _IMAGE_UNKNOWN},
                {"Key": "ImageArrayVariant", "Value": _IMAGE_NULL},
            ]
        )
    camera = {
        "ErrorCount": 0,
        "IssueCount": len(issues),
        "ConfigurationAlertCount": 0,
        "Errors": [],
        "Issues": issues,
        "ConfigurationAlerts": [],
    }
    (path / "camera-v4-results.json").write_text(
        json.dumps(camera),
        encoding="utf-8",
    )
    restoration = {
        "case": "restoration",
        "restored": True,
        "errors": [],
        "initial": {"gain": 100},
        "final": {"gain": 100},
    }
    for profile in ("protocol", "camera-v4"):
        (path / f"{profile}-cases.jsonl").write_text(
            json.dumps(restoration) + "\n",
            encoding="utf-8",
        )
    return camera, started


def test_fresh_conform_results_allow_only_two_documented_camera_conflicts(
    tmp_path,
):
    _write_fresh_results(tmp_path)

    summary = evaluate_results(tmp_path)

    assert summary["valid"] is True
    assert summary["protocol"]["issues"] == 0
    assert summary["camera_v4"]["issues"] == 17
    assert summary["allowed_issue_groups"] == {
        "ImageArrayVariant": 16,
        "PercentCompleted Read": 1,
    }
    assert summary["unexpected_errors"] == []
    assert summary["unexpected_issues"] == []


def test_unallowlisted_camera_issue_makes_conform_summary_fail(tmp_path):
    camera, _started = _write_fresh_results(tmp_path)
    camera["Issues"].append({"Key": "StartExposure", "Value": "timed out"})
    camera["IssueCount"] += 1
    (tmp_path / "camera-v4-results.json").write_text(
        json.dumps(camera),
        encoding="utf-8",
    )

    summary = evaluate_results(tmp_path)

    assert summary["valid"] is False
    assert summary["unexpected_issues"] == [
        {"profile": "camera-v4", "key": "StartExposure", "value": "timed out"}
    ]


def test_stale_profile_artifact_makes_conform_summary_fail(tmp_path):
    _camera, started = _write_fresh_results(tmp_path)
    stale = (started - timedelta(seconds=1)).timestamp()
    os.utime(tmp_path / "protocol.log", (stale, stale))

    summary = evaluate_results(tmp_path)

    assert summary["valid"] is False
    assert any("protocol.log is stale" in error for error in summary["validation_errors"])


def test_runner_replaces_profile_artifacts_and_records_raw_exit_codes():
    script = (
        __import__("pathlib").Path(__file__).parents[1]
        / "scripts"
        / "run_conformu.ps1"
    ).read_text(encoding="utf-8")

    assert "$profileArtifacts" in script
    assert 'Remove-Item -LiteralPath $profileArtifacts' in script
    assert 'started_utc = $startedUtc.ToString("o")' in script
    assert "exit_code = [int]$LASTEXITCODE" in script
    assert 'Join-Path $results "run-metadata.json"' in script
    assert "[System.IO.File]::WriteAllText(" in script
    assert "New-Object System.Text.UTF8Encoding($false)" in script
    assert "Set-Content -LiteralPath (Join-Path $results \"run-metadata.json\")" not in script
    assert "$protocolExit -ne 0" not in script
    assert "$summaryExit -ne 0" in script


def test_protocol_singular_error_count_is_authoritative(tmp_path):
    _camera, _started = _write_fresh_results(tmp_path)
    metadata = json.loads(
        (tmp_path / "run-metadata.json").read_text(encoding="utf-8")
    )
    metadata["profiles"]["protocol"]["exit_code"] = 1
    (tmp_path / "run-metadata.json").write_text(
        json.dumps(metadata),
        encoding="utf-8",
    )
    (tmp_path / "protocol.log").write_text(
        "Connecting to device: 127.0.0.1:11113\n"
        "Found 1 error, 0 issues and 4 information messages.\n",
        encoding="utf-8",
    )

    summary = evaluate_results(tmp_path)

    assert summary["protocol"] == {
        "errors": 1,
        "issues": 0,
        "information": 4,
    }
    assert summary["unexpected_errors"] == [
        {"profile": "protocol", "key": "final-count", "value": "1"}
    ]


def test_exact_put_casing_and_idle_stop_conflicts_are_allowlisted(tmp_path):
    camera, _started = _write_fresh_results(tmp_path)
    camera["Issues"].append(
        {
            "Key": "StopExposure",
            "Value": (
                "CanStopExposure is false but no error is returned when "
                "StopExposure is called"
            ),
        }
    )
    camera["IssueCount"] += 1
    (tmp_path / "camera-v4-results.json").write_text(
        json.dumps(camera),
        encoding="utf-8",
    )
    issue_lines = [
        (
            f"PUT {endpoint} ==> Bad {parameter} casing - Expected HTTP "
            "status: 200 (OK) but received status: 400 (BadRequest)."
        )
        for endpoint in _PUT_ENDPOINTS
        for parameter in ("ClientID", "ClientTransactionID")
    ]
    (tmp_path / "protocol.log").write_text(
        "Connecting to device: 127.0.0.1:11113\n"
        "Found 0 errors, 34 issues and 4 information messages.\n"
        "Issue Summary\n"
        + "\n".join(issue_lines)
        + "\nInformation Message Summary\n",
        encoding="utf-8",
    )
    metadata = json.loads(
        (tmp_path / "run-metadata.json").read_text(encoding="utf-8")
    )
    metadata["profiles"]["protocol"]["exit_code"] = 34
    metadata["profiles"]["camera-v4"]["exit_code"] = 18
    (tmp_path / "run-metadata.json").write_text(
        json.dumps(metadata),
        encoding="utf-8",
    )

    summary = evaluate_results(tmp_path)

    assert summary["valid"] is True
    assert summary["allowed_protocol_issue_groups"] == {
        "PUT form common-parameter casing": 34
    }
    assert summary["allowed_issue_groups"]["StopExposure"] == 1
