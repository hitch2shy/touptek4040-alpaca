"""Behavior tests for the synchronous main-process camera owner proxy."""

from __future__ import annotations

from collections import Counter, deque
from datetime import datetime, timezone
import math
from pathlib import Path
from types import SimpleNamespace
import time

import numpy as np
import pytest

from touptek4040.camera_contract import (
    CameraCapabilities,
    CameraContract,
    ReadoutMode,
    Roi,
)
from touptek4040.camera_owner import OwnerStartupSpec
from touptek4040.camera_proxy import CameraOwnerProxy, OwnerSupervisor
from touptek4040.config import OwnerConfig
from touptek4040.errors import (
    CameraBusyError,
    CameraNotConnectedError,
    CameraTimeoutError,
)
from touptek4040.owner_protocol import PROTOCOL_VERSION


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.exitcode: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.join_timeouts: list[float] = []

    def is_alive(self) -> bool:
        return self.exitcode is None

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.exitcode = -15

    def kill(self) -> None:
        self.kill_calls += 1
        self.exitcode = -9

    def join(self, timeout: float | None = None) -> None:
        if timeout is not None:
            self.join_timeouts.append(timeout)


class FakeConnection:
    def __init__(self, factory: "ScriptedFactory", generation: int, *, control: bool = False) -> None:
        self.factory = factory
        self.generation = generation
        self.control = control
        self.responses: deque[object] = deque()
        self.late_response: object | None = None
        self.poll_timeouts: list[float] = []
        self.closed = False

    def send(self, payload: object) -> None:
        assert isinstance(payload, dict)
        operation = str(payload["operation"])
        self.factory.calls[operation] += 1
        behavior = self.factory.behaviors.get((self.generation, operation), "ok")
        if behavior == "hang":
            return
        if behavior == "exit":
            self.factory.sessions[-1].process.exitcode = 23
            return
        if self.control:
            self.responses.append({"operation": operation, "ok": True})
            return
        if operation == "acquire":
            if behavior == "missing_ack":
                return
            actual_duration_s = self.factory.actual_duration_s
            if actual_duration_s is None:
                actual_duration_s = round(
                    float(payload["arguments"]["duration_s"]) * 1_000_000
                ) / 1_000_000
            ack: dict[str, object] = {
                "protocol_version": PROTOCOL_VERSION,
                "owner_generation": self.generation,
                "request_id": payload["request_id"],
                "operation": "acquire",
                "actual_duration_s": actual_duration_s,
            }
            if behavior == "stale_ack":
                ack["owner_generation"] = self.generation + 1
            elif behavior == "wrong_version_ack":
                ack["protocol_version"] = PROTOCOL_VERSION + 1
            elif behavior == "wrong_request_ack":
                ack["request_id"] = int(payload["request_id"]) + 1
            elif behavior == "wrong_operation_ack":
                ack["operation"] = "connect"
            elif behavior == "malformed_ack":
                ack["actual_duration_s"] = True
            elif behavior == "out_of_range_ack":
                ack["actual_duration_s"] = 3600.000001
            elif behavior == "nonfinite_ack":
                ack["actual_duration_s"] = float("nan")
            elif behavior == "overflow_ack":
                ack["actual_duration_s"] = 10**1000
            if behavior == "late_ack":
                self.late_response = ack
                return
            if behavior == "duplicate_ack":
                self.responses.append(dict(ack))
            self.responses.append(ack)
        result = self.factory.result_for(operation, payload)
        if behavior == "error":
            self.responses.append(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "owner_generation": self.generation,
                    "request_id": payload["request_id"],
                    "operation": operation,
                    "error": {
                        "code": "camera_error",
                        "message": f"injected {operation} failure",
                        "diagnostics": {},
                    },
                }
            )
            return
        response: dict[str, object] = {
            "protocol_version": PROTOCOL_VERSION,
            "owner_generation": self.generation,
            "request_id": payload["request_id"],
            "operation": operation,
            "result": result,
        }
        if behavior == "stale_generation":
            response["owner_generation"] = self.generation + 100
        elif behavior == "wrong_request":
            response["request_id"] = int(payload["request_id"]) + 1
        elif behavior == "duplicate":
            self.responses.append(dict(response))
        self.responses.append(response)

    def poll(self, _timeout: float = 0.0) -> bool:
        self.poll_timeouts.append(_timeout)
        if self.late_response is not None:
            time.sleep(_timeout)
            self.responses.append(self.late_response)
            self.late_response = None
            return False
        return bool(self.responses)

    def recv(self) -> object:
        if self.responses:
            return self.responses.popleft()
        raise EOFError

    def close(self) -> None:
        self.closed = True


class ScriptedFactory:
    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.behaviors: dict[tuple[int, str], str] = {}
        self.sessions: list[SimpleNamespace] = []
        self.next_pid = 4100
        self.actual_duration_s: float | None = None
        self.startup_specs: list[OwnerStartupSpec] = []

    def start(self, startup_spec, generation: int, registry_path: Path):
        del registry_path
        self.startup_specs.append(startup_spec)
        process = FakeProcess(self.next_pid)
        self.next_pid += 1
        session = SimpleNamespace(
            process=process,
            command_connection=FakeConnection(self, generation),
            control_connection=FakeConnection(self, generation, control=True),
        )
        self.sessions.append(session)
        return session

    def result_for(self, operation: str, payload: dict[str, object]) -> object:
        arguments = payload.get("arguments", {})
        if operation == "handshake":
            return {"pid": self.sessions[-1].process.pid, "generation": self.sessions.index(self.sessions[-1]) + 1}
        if operation == "connect":
            return {"connected": True}
        if operation == "disconnect":
            return {"connected": False}
        if operation == "capabilities":
            return {
                "width": 16,
                "height": 16,
                "pixel_size_x": 9.0,
                "pixel_size_y": 9.0,
                "readout_modes": ["RAW 12-bit", "RAW 8-bit"],
                "max_bin": 1,
                "can_abort": True,
                "can_stop": False,
            }
        if operation == "get_gain":
            return {"value": 100}
        if operation in {"get_temperature", "get_target_temperature", "get_cooler_power"}:
            return {"value": -10.0}
        if operation == "get_cooler_on":
            return {"value": True}
        if operation == "get_tec_voltage":
            return {"current": 1.0, "maximum": 6.0}
        if operation == "get_heater_capabilities":
            return {"supported": True, "max_level": 4}
        if operation == "get_heater_level":
            return {"value": 2}
        if operation == "acquire":
            actual_duration_s = self.actual_duration_s
            if actual_duration_s is None:
                actual_duration_s = round(
                    float(arguments["duration_s"]) * 1_000_000
                ) / 1_000_000
            return {
                "descriptor": {
                    "protocol_version": 1,
                    "owner_generation": self.sessions.index(self.sessions[-1]) + 1,
                    "request_id": 1,
                    "name": f"touptek4040-g{self.sessions.index(self.sessions[-1]) + 1}-r1-" + "a" * 32,
                    "shape": [2, 2],
                    "dtype": "uint16",
                    "nbytes": 8,
                    "max_adu": 4095,
                    "metadata": {"pixel_format": "RAW12", "max_adu": 4095},
                },
                "frame": {
                    "started_utc": datetime.now(timezone.utc).isoformat(),
                    "duration_s": actual_duration_s,
                    "binning": arguments["binning"],
                    "roi": arguments["roi"],
                    "readout_mode": arguments["readout_mode"],
                    "gain": 100,
                },
            }
        return {}


class FakeRegistry:
    def __init__(self, generation: int, _path: Path) -> None:
        self.generation = generation
        self.consumed: list[object] = []
        self.cleaned: list[int] = []

    def consume(self, descriptor) -> np.ndarray:
        self.consumed.append(descriptor)
        pixels = np.arange(4, dtype=np.uint16).reshape(2, 2)
        pixels.setflags(write=False)
        return pixels

    def cleanup_generation(self, generation: int) -> None:
        self.cleaned.append(generation)


def make_proxy(
    tmp_path: Path,
    factory: ScriptedFactory,
    *,
    registry_factory=FakeRegistry,
    owner_config: OwnerConfig | None = None,
) -> CameraOwnerProxy:
    config = owner_config or OwnerConfig(
        startup_timeout_s=0.05,
        connect_timeout_s=0.05,
        command_timeout_s=0.05,
        exposure_overhead_s=0.251,
        exposure_multiplier=1.0,
        abort_timeout_s=0.05,
        disconnect_timeout_s=0.05,
    )
    supervisor = OwnerSupervisor(
        OwnerStartupSpec(),
        config,
        tmp_path / "frames.json",
        process_factory=factory,
        registry_factory=registry_factory,
        termination_allowance_s=0.01,
    )
    return CameraOwnerProxy(supervisor=supervisor)


def test_proxy_forwards_camera_contract_and_reconstructs_a_frozen_frame(tmp_path: Path):
    factory = ScriptedFactory()
    proxy = make_proxy(tmp_path, factory)
    assert isinstance(proxy, CameraContract)
    proxy.connect()

    assert proxy.capabilities == CameraCapabilities(
        16, 16, 9.0, 9.0, ("RAW 12-bit", "RAW 8-bit"), 1, True, False
    )
    assert proxy.gain == 100
    proxy.gain = 200
    assert proxy.temperature_c == -10.0
    assert proxy.target_temperature_c == -10.0
    proxy.target_temperature_c = -20.0
    assert proxy.cooler_on is True
    proxy.cooler_on = False
    assert proxy.tec_voltage() == (1.0, 6.0)
    assert proxy.cooler_power_percent == -10.0

    frame = proxy.acquire(0.01, Roi(0, 0, 2, 2), 1, ReadoutMode.RAW12)

    assert frame.pixels.tolist() == [[0, 1], [2, 3]]
    assert frame.pixels.flags.writeable is False
    assert frame.duration_s == 0.01
    assert factory.calls["release_frame"] == 1
    ack = proxy._supervisor.last_acquire_start_ack
    assert ack is not None
    assert (ack[0], ack[2]) == (1, 0.01)
    assert proxy._supervisor.acquire_start_ack_count == 1


def test_default_real_owner_native_timeout_stays_below_owner_deadline(
    tmp_path: Path,
) -> None:
    factory = ScriptedFactory()
    config = OwnerConfig(
        startup_timeout_s=1.0,
        connect_timeout_s=1.0,
        command_timeout_s=1.0,
        exposure_overhead_s=10.0,
        exposure_multiplier=1.02,
        structural_reopen_settle_s=0.5,
        abort_timeout_s=1.0,
        disconnect_timeout_s=1.0,
    )
    proxy = CameraOwnerProxy(
        serial="TP241025143218C92B01151AEC40CB7",
        owner_config=config,
        runtime_directory=tmp_path,
        process_factory=factory,
    )
    try:
        arguments = factory.startup_specs[0].backend_arguments
        assert arguments == {
            "trigger_timeout_multiplier": 1.02,
            "trigger_timeout_overhead_s": 9.749,
            "structural_reopen_settle_s": 0.5,
        }
        for duration_s in (0.000012, 0.001, 2.0, 3600.0):
            native_deadline_ms = math.ceil(
                (
                    duration_s * arguments["trigger_timeout_multiplier"]
                    + arguments["trigger_timeout_overhead_s"]
                )
                * 1000
            )
            owner_deadline = proxy._supervisor.deadline_for(
                "acquire", actual_duration_s=duration_s
            )
            assert owner_deadline - native_deadline_ms / 1000 >= 0.25
    finally:
        proxy.close()


def test_custom_real_startup_spec_receives_derived_native_timeouts(
    tmp_path: Path,
) -> None:
    factory = ScriptedFactory()
    config = OwnerConfig(
        exposure_overhead_s=0.251,
        exposure_multiplier=1.5,
        structural_reopen_settle_s=0.75,
    )
    proxy = CameraOwnerProxy(
        serial="TP241025143218C92B01151AEC40CB7",
        owner_config=config,
        runtime_directory=tmp_path,
        startup_spec=OwnerStartupSpec(
            serial="TP241025143218C92B01151AEC40CB7",
            backend_arguments={"unrelated": "preserved"},
        ),
        process_factory=factory,
    )
    try:
        arguments = factory.startup_specs[0].backend_arguments
        assert arguments == {
            "unrelated": "preserved",
            "trigger_timeout_multiplier": 1.5,
            "trigger_timeout_overhead_s": 0.0,
            "structural_reopen_settle_s": 0.75,
        }
    finally:
        proxy.close()


def test_heater_proxy_confirms_only_successful_reads_and_writes(tmp_path: Path):
    factory = ScriptedFactory()
    proxy = make_proxy(tmp_path, factory)
    proxy.connect()

    assert proxy.heater_supported is True
    assert proxy.heater_max_level == 4
    assert proxy.heater_level == 2
    assert proxy.confirmed_heater_level == 2

    proxy.heater_level = 3

    assert proxy.confirmed_heater_level == 3


def test_heater_write_is_rejected_before_ipc_while_exposing(tmp_path: Path):
    factory = ScriptedFactory()
    proxy = make_proxy(tmp_path, factory)
    proxy.connect()
    proxy._supervisor._exposing = True

    with pytest.raises(CameraBusyError):
        proxy.heater_level = 3

    assert factory.calls["set_heater_level"] == 0


def test_replacement_generation_requires_explicit_reconnect(tmp_path: Path):
    factory = ScriptedFactory()
    proxy = make_proxy(tmp_path, factory)
    proxy.connect()
    first_generation = proxy.owner_generation
    factory.behaviors[(first_generation, "get_gain")] = "hang"

    with pytest.raises(CameraTimeoutError):
        _ = proxy.gain

    assert proxy.owner_generation == first_generation + 1
    assert proxy.connected is False
    with pytest.raises(CameraNotConnectedError):
        proxy.acquire(0.01, Roi(0, 0, 2, 2), 1, ReadoutMode.RAW12)
    assert factory.calls["acquire"] == 0

    proxy.connect()
    frame = proxy.acquire(0.01, Roi(0, 0, 2, 2), 1, ReadoutMode.RAW12)
    assert frame.pixels.shape == (2, 2)


def test_operation_deadlines_are_selected_by_operation(tmp_path: Path):
    factory = ScriptedFactory()
    proxy = make_proxy(
        tmp_path,
        factory,
        owner_config=OwnerConfig(
            startup_timeout_s=0.011,
            connect_timeout_s=0.022,
            command_timeout_s=0.033,
            exposure_overhead_s=0.251,
            exposure_multiplier=1.5,
            abort_timeout_s=0.055,
            disconnect_timeout_s=0.066,
        ),
    )

    assert proxy._supervisor.deadline_for("handshake") == 0.011
    assert proxy._supervisor.deadline_for("connect") == 0.022
    assert proxy._supervisor.deadline_for("get_gain") == 0.033
    assert proxy._supervisor.deadline_for("abort") == 0.055
    assert proxy._supervisor.deadline_for("disconnect") == 0.066
    assert proxy._supervisor.deadline_for(
        "acquire", actual_duration_s=0.25
    ) == pytest.approx(0.626)


def test_acquire_uses_owner_acknowledged_duration_for_remaining_deadline(
    tmp_path: Path,
):
    factory = ScriptedFactory()
    factory.actual_duration_s = 0.25
    proxy = make_proxy(
        tmp_path,
        factory,
        owner_config=OwnerConfig(
            startup_timeout_s=0.05,
            connect_timeout_s=0.05,
            command_timeout_s=0.033,
            exposure_overhead_s=0.251,
            exposure_multiplier=1.5,
            abort_timeout_s=0.05,
            disconnect_timeout_s=0.05,
        ),
    )
    proxy.connect()

    frame = proxy.acquire(
        0.001, Roi(0, 0, 2, 2), 1, ReadoutMode.RAW12
    )

    command = factory.sessions[0].command_connection
    assert frame.duration_s == 0.25
    assert any(timeout > 0.40 for timeout in command.poll_timeouts)


def test_real_spawn_factory_round_trips_and_releases_a_shared_frame(tmp_path: Path):
    config = OwnerConfig(
        startup_timeout_s=3.0,
        connect_timeout_s=3.0,
        command_timeout_s=3.0,
        exposure_overhead_s=3.0,
        exposure_multiplier=1.0,
        abort_timeout_s=3.0,
        disconnect_timeout_s=3.0,
    )
    proxy = CameraOwnerProxy(
        owner_config=config,
        runtime_directory=tmp_path,
        startup_spec=OwnerStartupSpec(
            backend_module="test.fixtures.fake_owner_backend",
            backend_factory="create_backend",
        ),
    )
    try:
        proxy.connect()
        frame = proxy.acquire(
            0.001, Roi(0, 0, 2, 2), 1, ReadoutMode.RAW12
        )

        assert frame.pixels.tolist() == [[0, 1], [2, 3]]
        assert frame.pixels.flags.writeable is False
        assert factory_registry_document(tmp_path) == {"frames": []}
    finally:
        proxy.close()


def factory_registry_document(tmp_path: Path) -> object:
    import json

    return json.loads(
        (tmp_path / "touptek4040-shared-frames.json").read_text(
            encoding="utf-8"
        )
    )
