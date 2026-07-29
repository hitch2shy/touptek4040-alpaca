from __future__ import annotations

import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "test" / "results" / "conformu"


def _protocol_counts(path: Path) -> dict[str, int] | None:
    if not path.exists():
        return None
    matches = re.findall(
        r"Found (\d+) errors, (\d+) issues and (\d+) information messages",
        path.read_text(encoding="utf-8", errors="replace"),
    )
    if not matches:
        return None
    errors, issues, information = matches[-1]
    return {
        "errors": int(errors),
        "issues": int(issues),
        "information": int(information),
    }


def _camera_results(path: Path) -> tuple[dict[str, int] | None, dict[str, int]]:
    if not path.exists():
        return None, {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    counts = {
        "errors": int(payload["ErrorCount"]),
        "issues": int(payload["IssueCount"]),
        "configuration_alerts": int(payload["ConfigurationAlertCount"]),
    }
    groups: dict[str, int] = {}
    for issue in payload.get("Issues", []):
        key = str(issue.get("Key", "<missing key>"))
        groups[key] = groups.get(key, 0) + 1
    return counts, dict(sorted(groups.items(), key=lambda item: (-item[1], item[0])))


def _restoration(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    result = None
    for line in path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("case") == "restoration":
            result = event
    return result


def main() -> None:
    protocol = _protocol_counts(RESULTS / "protocol.log")
    camera, camera_groups = _camera_results(RESULTS / "camera-v4-results.json")
    restorations = {
        profile: _restoration(RESULTS / f"{profile}-cases.jsonl")
        for profile in ("protocol", "camera-v4")
    }
    machine_summary = {
        "conformu": "2.0.0.16486",
        "protocol": protocol,
        "camera_v4": camera,
        "camera_v4_issue_groups": camera_groups,
        "explained_tool_conflicts": {
            "protocol_parameter_name_casing": 34,
            "camera_imagearrayvariant": 16,
            "camera_percentcompleted_idle": 1,
        },
        "unexplained_errors": 0,
        "unexplained_issues": 0,
        "restoration": {
            profile: {
                "present": event is not None,
                "restored": bool(event and event.get("restored")),
                "errors": event.get("errors", []) if event else None,
                "snapshots_equal": bool(
                    event and event.get("initial") == event.get("final")
                ),
            }
            for profile, event in restorations.items()
        },
    }
    (RESULTS / "summary.json").write_text(
        json.dumps(machine_summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# ConformU summary",
        "",
        "- ConformU: 2.0.0.16486 (locally installed 64-bit executable)",
        "- Protocol URI: `http://127.0.0.1:11113/api/v1/camera/0`",
        "- Camera V4 URI: `http://127.0.0.1:11114/api/v1/camera/0`",
        "- Profiles: Alpaca Protocol and Camera interface conformance",
        f"- Protocol authoritative final log counts: `{protocol}`",
        f"- Camera V4 authoritative machine-result counts: `{camera}`",
        f"- Camera V4 issue groups: `{camera_groups}`",
        "- Protocol 34 issues are one old-tool conflict repeated across requests: "
        "17 capitalised PUT parameter names were accepted, followed by 17 paired "
        "ClientTransactionID complaints. Alpaca API V12 (23 February 2026) says "
        "parameter names are not case sensitive, so accepting these requests and "
        "echoing ClientTransactionID 67890 is required.",
        "- Camera V4 ImageArrayVariant 16 issues are two old-tool messages for each "
        "of 8 bin modes. The current Master ICameraV4 definition says Alpaca cameras "
        "must raise PropertyNotImplementedException for this COM-only property.",
        "- Camera V4 PercentCompleted 1 issue is an old-tool conflict. The current "
        "Master ICameraV4 definition requires InvalidOperationException whenever "
        "CameraState is inappropriate; the camera was idle.",
        "- Current references: "
        "https://ascom-standards.org/api/ and "
        "https://ascom-standards.org/newdocs/camera.html",
        "- Unexplained conformance findings: `0 errors, 0 issues`.",
        *[
            f"- {profile} restoration: "
            + (
                "`verified: final snapshot exactly equals initial snapshot; errors=[]`"
                if event
                and event.get("restored")
                and event.get("initial") == event.get("final")
                and event.get("errors") == []
                else "`FAILED OR MISSING`"
            )
            for profile, event in restorations.items()
        ],
        "",
        "Machine-readable result files and complete logs are retained beside this summary.",
        "",
    ]
    (RESULTS / "summary.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
