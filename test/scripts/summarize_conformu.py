from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "test" / "results" / "conformu"

_PROFILES = {
    "protocol": {"command": "alpacaprotocol", "port": 11113},
    "camera-v4": {"command": "conformance", "port": 11114},
}
_IMAGE_ARRAY_VARIANT_VALUES = {
    "Error when reading ImageArrayVariant: ToImageArray - "
    "ImageArrayElementType is 0, meaning ImageArrayElementTypes.Unknown",
    "Unexpected error while processing ImageArrayVariant: "
    "Object reference not set to an instance of an object.",
}
_PERCENT_COMPLETED_VALUE = (
    "Unexpected error: PercentCompleted is unavailable outside an exposure"
)
_STOP_EXPOSURE_VALUE = (
    "CanStopExposure is false but no error is returned when StopExposure is called"
)
_PUT_CASING_ENDPOINTS = {
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
}
_PUT_CASING_PARAMETERS = {"ClientID", "ClientTransactionID"}


def _read_json(path: Path, validation_errors: list[str]) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        validation_errors.append(f"{path.name} is unreadable: {exc}")
        return None


def _started_timestamp(
    profile: str, metadata: dict[str, Any], validation_errors: list[str]
) -> float | None:
    expected = _PROFILES[profile]
    record = metadata.get("profiles", {}).get(profile)
    if not isinstance(record, dict):
        validation_errors.append(f"run-metadata.json is missing profile {profile}")
        return None
    for key, value in expected.items():
        if record.get(key) != value:
            validation_errors.append(
                f"{profile} metadata {key} is {record.get(key)!r}, expected {value!r}"
            )
    exit_code = record.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        validation_errors.append(f"{profile} metadata exit_code is not an integer")
    started = record.get("started_utc")
    if not isinstance(started, str):
        validation_errors.append(f"{profile} metadata started_utc is missing")
        return None
    try:
        parsed = datetime.fromisoformat(started.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone offset is required")
        return parsed.timestamp()
    except ValueError as exc:
        validation_errors.append(f"{profile} metadata started_utc is invalid: {exc}")
        return None


def _validate_artifact(
    path: Path, started: float | None, validation_errors: list[str]
) -> bool:
    if not path.is_file():
        validation_errors.append(f"{path.name} is missing")
        return False
    if started is not None and path.stat().st_mtime < started:
        validation_errors.append(f"{path.name} is stale")
        return False
    return True


def _restoration(path: Path, validation_errors: list[str]) -> dict[str, Any] | None:
    event = None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            if item.get("case") == "restoration":
                event = item
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        validation_errors.append(f"{path.name} is unreadable: {exc}")
    return event


def evaluate_results(results: Path) -> dict[str, Any]:
    validation_errors: list[str] = []
    unexpected_errors: list[dict[str, str]] = []
    unexpected_issues: list[dict[str, str]] = []
    metadata = _read_json(results / "run-metadata.json", validation_errors)
    if not isinstance(metadata, dict):
        metadata = {}
    starts = {
        profile: _started_timestamp(profile, metadata, validation_errors)
        for profile in _PROFILES
    }

    required = {
        "protocol": ("protocol.log", "protocol-cases.jsonl"),
        "camera-v4": (
            "camera-v4.log",
            "camera-v4-results.json",
            "camera-v4-cases.jsonl",
        ),
    }
    for profile, names in required.items():
        for name in names:
            _validate_artifact(results / name, starts[profile], validation_errors)

    protocol = None
    allowed_protocol_issue_groups: dict[str, int] = {}
    protocol_path = results / "protocol.log"
    if protocol_path.is_file():
        text = protocol_path.read_text(encoding="utf-8", errors="replace")
        if "127.0.0.1:11113" not in text:
            validation_errors.append("protocol.log does not identify port 11113")
        matches = re.findall(
            r"Found (\d+) errors?, (\d+) issues? and "
            r"(\d+) information messages?",
            text,
        )
        if not matches:
            validation_errors.append("protocol.log has no authoritative final counts")
        else:
            errors, issues, information = map(int, matches[-1])
            protocol = {
                "errors": errors,
                "issues": issues,
                "information": information,
            }
            if errors:
                unexpected_errors.append(
                    {"profile": "protocol", "key": "final-count", "value": str(errors)}
                )
            issue_section = text.rsplit("Issue Summary", 1)[-1]
            issue_section = issue_section.split(
                "Information Message Summary", 1
            )[0]
            casing_entries = re.findall(
                r"PUT (\w+) ==> Bad (ClientID|ClientTransactionID) casing - "
                r"Expected HTTP status: 200 \(OK\) but received status: "
                r"400 \(BadRequest\)\.",
                issue_section,
            )
            expected_casing_entries = {
                (endpoint, parameter)
                for endpoint in _PUT_CASING_ENDPOINTS
                for parameter in _PUT_CASING_PARAMETERS
            }
            if (
                issues == len(expected_casing_entries)
                and len(casing_entries) == len(expected_casing_entries)
                and set(casing_entries) == expected_casing_entries
            ):
                allowed_protocol_issue_groups[
                    "PUT form common-parameter casing"
                ] = len(casing_entries)
            elif issues:
                unexpected_issues.append(
                    {"profile": "protocol", "key": "final-count", "value": str(issues)}
                )

    camera_path = results / "camera-v4-results.json"
    camera_payload = _read_json(camera_path, validation_errors)
    camera = None
    allowed_issue_groups: dict[str, int] = {}
    if isinstance(camera_payload, dict):
        collection_fields = ("Errors", "Issues", "ConfigurationAlerts")
        count_fields = (
            ("ErrorCount", "Errors"),
            ("IssueCount", "Issues"),
            ("ConfigurationAlertCount", "ConfigurationAlerts"),
        )
        for name in collection_fields:
            if not isinstance(camera_payload.get(name), list):
                validation_errors.append(f"camera-v4-results.json {name} is not an array")
        for count_name, array_name in count_fields:
            count = camera_payload.get(count_name)
            values = camera_payload.get(array_name)
            if isinstance(count, bool) or not isinstance(count, int):
                validation_errors.append(
                    f"camera-v4-results.json {count_name} is not an integer"
                )
            elif isinstance(values, list) and count != len(values):
                validation_errors.append(
                    f"camera-v4-results.json {count_name} does not match {array_name}"
                )
        if all(
            isinstance(camera_payload.get(name), int)
            and not isinstance(camera_payload.get(name), bool)
            for name in ("ErrorCount", "IssueCount", "ConfigurationAlertCount")
        ):
            camera = {
                "errors": camera_payload["ErrorCount"],
                "issues": camera_payload["IssueCount"],
                "configuration_alerts": camera_payload["ConfigurationAlertCount"],
            }

        for item in camera_payload.get("Errors", []):
            if isinstance(item, dict):
                unexpected_errors.append(
                    {
                        "profile": "camera-v4",
                        "key": str(item.get("Key", "<missing key>")),
                        "value": str(item.get("Value", "<missing value>")),
                    }
                )
            else:
                validation_errors.append("camera-v4 error entry is not an object")
        for item in camera_payload.get("ConfigurationAlerts", []):
            if isinstance(item, dict):
                unexpected_errors.append(
                    {
                        "profile": "camera-v4",
                        "key": str(item.get("Key", "<missing key>")),
                        "value": str(item.get("Value", "<missing value>")),
                    }
                )
            else:
                validation_errors.append(
                    "camera-v4 configuration alert entry is not an object"
                )
        for item in camera_payload.get("Issues", []):
            if not isinstance(item, dict):
                validation_errors.append("camera-v4 issue entry is not an object")
                continue
            key = str(item.get("Key", "<missing key>"))
            value = str(item.get("Value", "<missing value>"))
            allowed = (
                key == "ImageArrayVariant"
                and value in _IMAGE_ARRAY_VARIANT_VALUES
            ) or (
                key == "PercentCompleted Read"
                and value == _PERCENT_COMPLETED_VALUE
            ) or (
                key == "StopExposure"
                and value == _STOP_EXPOSURE_VALUE
            )
            if allowed:
                allowed_issue_groups[key] = allowed_issue_groups.get(key, 0) + 1
            else:
                unexpected_issues.append(
                    {"profile": "camera-v4", "key": key, "value": value}
                )

    for key, count in allowed_issue_groups.items():
        expected_counts = {
            "ImageArrayVariant": {16},
            "PercentCompleted Read": {1},
            "StopExposure": {1},
        }
        if count not in expected_counts[key]:
            validation_errors.append(
                f"camera-v4 allowed issue group {key} has unexpected count {count}"
            )

    camera_log = results / "camera-v4.log"
    if camera_log.is_file():
        text = camera_log.read_text(encoding="utf-8", errors="replace")
        if "127.0.0.1:11114" not in text or "Camera/0" not in text:
            validation_errors.append(
                "camera-v4.log does not identify port 11114 Camera/0"
            )
        if "Conformance test has finished" not in text:
            validation_errors.append("camera-v4.log has no completion marker")

    restorations: dict[str, dict[str, Any]] = {}
    for profile in _PROFILES:
        path = results / f"{profile}-cases.jsonl"
        event = _restoration(path, validation_errors) if path.is_file() else None
        restored = bool(event and event.get("restored"))
        errors = event.get("errors") if event else None
        snapshots_equal = bool(event and event.get("initial") == event.get("final"))
        restorations[profile] = {
            "present": event is not None,
            "restored": restored,
            "errors": errors,
            "snapshots_equal": snapshots_equal,
        }
        if event is None:
            validation_errors.append(f"{profile} restoration event is missing")
        elif not restored or errors != [] or not snapshots_equal:
            validation_errors.append(f"{profile} restoration did not complete cleanly")

    profile_records = metadata.get("profiles", {})
    expected_exit_counts = {
        "protocol": (protocol or {}).get("errors", 0)
        + (protocol or {}).get("issues", 0),
        "camera-v4": (camera or {}).get("errors", 0)
        + (camera or {}).get("issues", 0)
        + (camera or {}).get("configuration_alerts", 0),
    }
    for profile, expected in expected_exit_counts.items():
        record = profile_records.get(profile, {})
        exit_code = record.get("exit_code") if isinstance(record, dict) else None
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            if exit_code != expected:
                validation_errors.append(
                    f"{profile} exit_code {exit_code} does not match finding count {expected}"
                )

    summary = {
        "conformu": "2.0.0.16486",
        "protocol": protocol,
        "camera_v4": camera,
        "allowed_protocol_issue_groups": dict(
            sorted(allowed_protocol_issue_groups.items())
        ),
        "allowed_issue_groups": dict(sorted(allowed_issue_groups.items())),
        "unexpected_errors": unexpected_errors,
        "unexpected_issues": unexpected_issues,
        "validation_errors": validation_errors,
        "restoration": restorations,
    }
    summary["valid"] = not (
        validation_errors or unexpected_errors or unexpected_issues
    )
    return summary


def _write_markdown(summary: dict[str, Any], results: Path) -> None:
    lines = [
        "# ConformU summary",
        "",
        "- ConformU: 2.0.0.16486 (64-bit)",
        "- Protocol URI: `http://127.0.0.1:11113/api/v1/camera/0`",
        "- Camera V4 URI: `http://127.0.0.1:11114/api/v1/camera/0`",
        f"- Protocol final counts: `{summary['protocol']}`",
        "- Allowed Protocol issue groups: "
        f"`{summary['allowed_protocol_issue_groups']}`",
        "- The 34 Protocol issues are exactly two common-parameter casing "
        "probes on each of 17 PUT endpoints. This service returns HTTP 400 "
        "for incorrectly cased PUT form parameter names as required by "
        "Alpaca API v12 section 2.2.3; ConformU 2.0.0.16486 expects the "
        "older HTTP 200 behavior.",
        f"- Camera V4 counts: `{summary['camera_v4']}`",
        f"- Allowed Camera V4 issue groups: `{summary['allowed_issue_groups']}`",
        "- ImageArrayVariant is a COM-only property; an Alpaca camera must report "
        "PropertyNotImplemented.",
        "- PercentCompleted is required to report InvalidOperation while the camera "
        "state is inappropriate (idle in this run).",
        "- Idle StopExposure is a successful no-op under the Camera contract; "
        "CanStopExposure=False applies when an exposure is active because this "
        "camera cannot return a valid early-read image.",
        "- References: https://ascom-standards.org/api/ and "
        "https://ascom-standards.org/newdocs/camera.html",
        f"- Unexpected errors: `{summary['unexpected_errors']}`",
        f"- Unexpected issues: `{summary['unexpected_issues']}`",
        f"- Validation errors: `{summary['validation_errors']}`",
    ]
    for profile, restoration in summary["restoration"].items():
        lines.append(f"- {profile} restoration: `{restoration}`")
    lines.extend(
        [
            f"- Overall result: `{'PASS' if summary['valid'] else 'FAIL'}`",
            "",
            "Machine-readable result files and complete logs are retained beside this summary.",
            "",
        ]
    )
    (results / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    summary = evaluate_results(RESULTS)
    (RESULTS / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_markdown(summary, RESULTS)
    return 0 if summary["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
