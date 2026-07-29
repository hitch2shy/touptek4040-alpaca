"""Spawned-process evidence for every camera-owner hard timeout boundary."""

from __future__ import annotations

import multiprocessing
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any

import numpy as np

import pytest

from test.fixtures.fake_owner_backend import FakeOwnerBackend
from touptek4040.camera_contract import Frame, ReadoutMode, Roi
from touptek4040.camera_owner import OwnerStartupSpec, camera_owner_main
from touptek4040.camera_proxy import CameraOwnerProxy, OwnerProcessSession
from touptek4040.config import OwnerConfig
from touptek4040.errors import CameraError


REQUIRED_HARD_BOUNDARIES = {
    "startup",
    "connect",
    "get",
    "set",
    "trigger",
    "heater_read",
    "heater_write",
    "frame_publication",
    "abort",
    "disconnect",
}
REQUIRED_CRASH_POINTS = {
    "before_creation",
    "after_creation",
    "after_descriptor_send",
    "after_proxy_copy_before_release_ack",
}


_FAULT_EVENTS: Any = None
_FAULT_CLAIMED: Any = None
_SELECTED_BOUNDARY = ""
_CURRENT_REQUEST = threading.local()


class _ObservedConnection:
    def __init__(
        self,
        connection: Any,
        *,
        control: bool = False,
        default_generation: int | None = None,
    ) -> None:
        self._connection = connection
        self._control = control
        self._default_generation = default_generation

    def poll(self, timeout: float = 0.0) -> bool:
        return self._connection.poll(timeout)

    def recv(self) -> object:
        payload = self._connection.recv()
        if type(payload) is dict:
            operation = payload.get("operation")
            _CURRENT_REQUEST.operation = operation
            _CURRENT_REQUEST.request_id = (
                0 if self._control else payload.get("request_id")
            )
            _CURRENT_REQUEST.generation = payload.get(
                "owner_generation", self._default_generation
            )
        return payload

    def send(self, payload: object) -> None:
        self._connection.send(payload)

    def close(self) -> None:
        self._connection.close()


class HangingOwnerBackend(FakeOwnerBackend):
    """Real owner backend that blocks once at one selected SDK boundary."""

    def connect(self) -> None:
        if getattr(_CURRENT_REQUEST, "operation", None) == "connect":
            _hang_once("connect")
        super().connect()

    def disconnect(self) -> None:
        if getattr(_CURRENT_REQUEST, "operation", None) == "disconnect":
            _hang_once("disconnect")
        super().disconnect()

    @property
    def gain(self) -> int:
        if getattr(_CURRENT_REQUEST, "operation", None) == "get_gain":
            _hang_once("get")
        return super().gain

    @gain.setter
    def gain(self, value: int) -> None:
        if getattr(_CURRENT_REQUEST, "operation", None) == "set_gain":
            _hang_once("set")
        FakeOwnerBackend.gain.fset(self, value)

    def acquire(
        self,
        duration_s: float,
        roi: Roi,
        binning: int,
        readout_mode: ReadoutMode,
        control: Any = None,
    ) -> Frame:
        if getattr(_CURRENT_REQUEST, "operation", None) == "acquire":
            _hang_once("trigger")
        return super().acquire(duration_s, roi, binning, readout_mode, control)

    def abort(self) -> None:
        if getattr(_CURRENT_REQUEST, "operation", None) == "abort":
            _hang_once("abort")
        super().abort()

    @property
    def heater_level(self) -> int:
        if getattr(_CURRENT_REQUEST, "operation", None) == "get_heater_level":
            _hang_once("heater_read")
        return FakeOwnerBackend.heater_level.fget(self)

    @heater_level.setter
    def heater_level(self, value: int) -> None:
        if getattr(_CURRENT_REQUEST, "operation", None) == "set_heater_level":
            _hang_once("heater_write")
        FakeOwnerBackend.heater_level.fset(self, value)


def create_hanging_backend(
    serial: str | None = None, **_kwargs: object
) -> HangingOwnerBackend:
    return HangingOwnerBackend(serial)


def _hang_once(boundary: str) -> None:
    if boundary != _SELECTED_BOUNDARY:
        return
    _observe_invocation(boundary)
    with _FAULT_CLAIMED.get_lock():
        if _FAULT_CLAIMED.value:
            return
        _FAULT_CLAIMED.value = 1
    threading.Event().wait()


def _observe_invocation(boundary: str) -> None:
    if _FAULT_EVENTS is None:
        return
    _FAULT_EVENTS.send(
        {
            "boundary": boundary,
            "pid": os.getpid(),
            "generation": getattr(_CURRENT_REQUEST, "generation", None),
            "request_id": getattr(_CURRENT_REQUEST, "request_id", None),
        }
    )


def _fault_owner_main(
    command_connection: Any,
    control_connection: Any,
    startup_spec: OwnerStartupSpec,
    generation: int,
    registry_path: str,
    selected_boundary: str,
    fault_events: Any,
    fault_claimed: Any,
) -> None:
    global _FAULT_EVENTS, _FAULT_CLAIMED, _SELECTED_BOUNDARY
    _FAULT_EVENTS = fault_events
    _FAULT_CLAIMED = fault_claimed
    _SELECTED_BOUNDARY = selected_boundary
    _CURRENT_REQUEST.generation = generation
    _CURRENT_REQUEST.request_id = 0
    _CURRENT_REQUEST.operation = "handshake"
    if selected_boundary == "startup":
        _hang_once("startup")
    if selected_boundary == "frame_publication":
        import touptek4040.camera_owner as owner_module

        original_registry = owner_module.SharedFrameRegistry

        class HangingPublishRegistry(original_registry):
            def publish(self, pixels: np.ndarray, metadata: Any):
                _hang_once("frame_publication")
                return super().publish(pixels, metadata)

        owner_module.SharedFrameRegistry = HangingPublishRegistry
    camera_owner_main(
        _ObservedConnection(command_connection, default_generation=generation),
        _ObservedConnection(
            control_connection,
            control=True,
            default_generation=generation,
        ),
        startup_spec,
        generation,
        registry_path,
    )


class _FaultProcessFactory:
    def __init__(self, boundary: str) -> None:
        self.context = multiprocessing.get_context("spawn")
        self.boundary = boundary
        self.event_parent, self.event_child = self.context.Pipe(duplex=False)
        self.claimed = self.context.Value("b", 0)

    def start(
        self,
        startup_spec: OwnerStartupSpec,
        generation: int,
        registry_path: Path,
    ) -> OwnerProcessSession:
        command_parent, command_child = self.context.Pipe(duplex=True)
        control_parent, control_child = self.context.Pipe(duplex=True)
        process = self.context.Process(
            target=_fault_owner_main,
            args=(
                command_child,
                control_child,
                startup_spec,
                generation,
                str(registry_path),
                self.boundary,
                self.event_child,
                self.claimed,
            ),
        )
        process.start()
        command_child.close()
        control_child.close()
        return OwnerProcessSession(process, command_parent, control_parent)

    def drain(self) -> list[dict[str, object]]:
        events: list[dict[str, object]] = []
        while self.event_parent.poll(0.02):
            events.append(self.event_parent.recv())
        return events


def run_hung_boundary(boundary: str, tmp_path: Path) -> dict[str, object]:
    config = OwnerConfig(
        startup_timeout_s=0.6,
        connect_timeout_s=0.12,
        command_timeout_s=0.12,
        exposure_overhead_s=0.12,
        exposure_multiplier=1.0,
        abort_timeout_s=0.12,
        disconnect_timeout_s=0.12,
    )
    factory = _FaultProcessFactory(boundary)
    started = time.monotonic()
    proxy: CameraOwnerProxy | None = None
    caught: CameraError | None = None
    try:
        proxy = CameraOwnerProxy(
            owner_config=config,
            runtime_directory=tmp_path,
            startup_spec=OwnerStartupSpec(
                serial="FAULT",
                backend_module=__name__,
                backend_factory="create_hanging_backend",
            ),
            process_factory=factory,
        )
        hung_generation = 1 if boundary == "startup" else proxy.owner_generation
        if boundary != "startup":
            try:
                if boundary == "connect":
                    started = time.monotonic()
                    proxy.connect()
                else:
                    proxy.connect()
                    started = time.monotonic()
                    {
                        "get": lambda: proxy.gain,
                        "set": lambda: setattr(proxy, "gain", 101),
                        "trigger": lambda: proxy.acquire(
                            0.001, Roi(0, 0, 4, 4), 1, ReadoutMode.RAW12
                        ),
                        "heater_read": lambda: proxy.heater_level,
                        "heater_write": lambda: setattr(proxy, "heater_level", 3),
                        "frame_publication": lambda: proxy.acquire(
                            0.001, Roi(0, 0, 4, 4), 1, ReadoutMode.RAW12
                        ),
                        "abort": proxy.abort,
                        "disconnect": proxy.disconnect,
                    }[boundary]()
            except CameraError as error:
                caught = error
            assert caught is not None, f"{boundary} did not cross its hard timeout"
        elapsed = time.monotonic() - started
        events = factory.drain()
        matching = [event for event in events if event["boundary"] == boundary]
        hung_events = [
            event
            for event in matching
            if event["generation"] == hung_generation
        ]
        assert hung_events, events
        event = hung_events[0]
        hung_pid = int(event["pid"])
        invocation_counts = _invocation_counts_by_generation(
            events, boundary
        )
        invocation_counts.setdefault(str(hung_generation), 0)
        invocation_counts.setdefault(str(proxy.owner_generation), 0)
        result = {
            "elapsed_s": elapsed,
            "hard_bound_s": 2.1 if boundary == "startup" else 0.9,
            "hung_pid": hung_pid,
            "hung_generation": hung_generation,
            "terminated_pid": proxy.last_terminated_pid,
            "hung_pid_alive": _pid_alive(hung_pid),
            "invocations_by_generation": invocation_counts,
            "request_id": event["request_id"],
            "replacement_generation": proxy.owner_generation,
            "replacement_connected": proxy.connected,
        }
        return result
    finally:
        if proxy is not None:
            proxy.close()


def _pid_alive(pid: int) -> bool:
    from touptek4040.shared_frames import SharedFrameRegistry

    return not SharedFrameRegistry._pid_is_dead(pid)


def _invocation_counts_by_generation(
    events: list[dict[str, object]],
    boundary: str,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        if event.get("boundary") != boundary:
            continue
        generation = event.get("generation")
        assert isinstance(generation, int)
        key = str(generation)
        counts[key] = counts.get(key, 0) + 1
    return counts


def test_invocation_counts_preserve_each_owner_generation() -> None:
    events = [
        {"boundary": "trigger", "pid": 10, "generation": 4, "request_id": 2},
        {"boundary": "trigger", "pid": 10, "generation": 4, "request_id": 2},
        {"boundary": "trigger", "pid": 11, "generation": 5, "request_id": 2},
    ]

    assert _invocation_counts_by_generation(events, "trigger") == {
        "4": 2,
        "5": 1,
    }


@pytest.mark.parametrize(
    ("mutation", "manifest_violations", "failed_rows"),
    [
        ("substitute", 2, 2),
        ("duplicate", 2, 3),
        ("extra", 1, 1),
        ("replacement_generation", 0, 1),
    ],
)
def test_fault_runner_rejects_corrupt_case_evidence(
    mutation: str,
    manifest_violations: int,
    failed_rows: int,
    tmp_path: Path,
) -> None:
    records = _valid_runner_records()
    if mutation == "substitute":
        records[0]["name"] = "unknown-boundary"
    elif mutation == "duplicate":
        records[1] = dict(records[0])
    elif mutation == "extra":
        records.append(dict(records[0], name="unexpected"))
    else:
        records[0]["invocations_by_generation"]["2"] = 1
    cases_path = tmp_path / "cases.jsonl"
    cases_path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    output_directory = tmp_path / "audit"

    completed = subprocess.run(
        [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(
                Path(__file__).resolve().parents[1]
                / "scripts"
                / "run_owner_faults.ps1"
            ),
            "-CasesInput",
            str(cases_path),
            "-OutputDirectory",
            str(output_directory),
            "-ValidationOnly",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode != 0
    summary = json.loads(
        (output_directory / "validation-summary.json").read_text(
            encoding="utf-8-sig"
        )
    )
    assert summary["passed"] is False
    assert summary["violations"]
    assert summary["counters"]["manifestViolations"] == manifest_violations
    assert (
        summary["counters"]["generationInvocationViolations"]
        == (1 if mutation == "replacement_generation" else 0)
    )
    assert (
        sum(case["status"] == "FAIL" for case in summary["cases"])
        == failed_rows
    )


def test_official_runner_rejects_stale_cases_override(
    tmp_path: Path,
) -> None:
    stale_cases = tmp_path / "stale-cases.jsonl"
    stale_cases.write_text(
        "".join(
            json.dumps(record) + "\n"
            for record in _valid_runner_records()
        ),
        encoding="utf-8",
    )
    output_directory = tmp_path / "official"

    completed = subprocess.run(
        [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(
                Path(__file__).resolve().parents[1]
                / "scripts"
                / "run_owner_faults.ps1"
            ),
            "-CasesInput",
            str(stale_cases),
            "-OutputDirectory",
            str(output_directory),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode != 0
    assert not (output_directory / "summary.json").exists()


def _valid_runner_records() -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for name in sorted(REQUIRED_HARD_BOUNDARIES):
        records.append(
            {
                "kind": "hard-boundary",
                "name": name,
                "elapsed_s": 0.1,
                "hard_bound_s": 1.0,
                "hung_pid_alive": False,
                "hung_generation": 1,
                "replacement_generation": 2,
                "invocations_by_generation": {
                    "1": 1,
                    "2": 1 if name == "startup" else 0,
                },
                "replacement_connected": False,
            }
        )
    for name in sorted(REQUIRED_CRASH_POINTS):
        records.append(
            {
                "kind": "shared-memory",
                "name": name,
                "owner_pid_alive": False,
                "registry_names": [],
                "live_segment_names": [],
            }
        )
    return records


def _record_result(result: dict[str, object]) -> None:
    output = os.environ.get("TOUPTEK_OWNER_FAULT_RESULTS")
    if not output:
        return
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(result, sort_keys=True) + "\n")


@pytest.mark.parametrize(
    "boundary",
    [
        "startup",
        "connect",
        "get",
        "set",
        "trigger",
        "heater_read",
        "heater_write",
        "frame_publication",
        "abort",
        "disconnect",
    ],
)
def test_hung_owner_boundary_is_bounded_and_replaced_disconnected(
    boundary: str,
    tmp_path: Path,
) -> None:
    result = run_hung_boundary(boundary, tmp_path)

    assert result["elapsed_s"] <= result["hard_bound_s"]
    assert result["terminated_pid"] == result["hung_pid"]
    assert result["hung_pid_alive"] is False
    counts = result["invocations_by_generation"]
    assert counts[str(result["hung_generation"])] == 1
    assert counts[str(result["replacement_generation"])] == (
        1 if boundary == "startup" else 0
    )
    assert set(counts) == {
        str(result["hung_generation"]),
        str(result["replacement_generation"]),
    }
    assert isinstance(result["request_id"], int)
    assert result["request_id"] >= 0
    assert result["replacement_generation"] != result["hung_generation"]
    assert result["replacement_connected"] is False
    _record_result({"kind": "hard-boundary", "name": boundary, **result})
