"""Process-boundary tests for the isolated camera owner."""

from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
import sys
import time
from threading import Thread
import subprocess
from types import ModuleType

import pytest

from test.fixtures.fake_owner_backend import ExplodingOwnerPayload, FakeOwnerBackend
from touptek4040.camera_owner import (
    OwnerStartupSpec,
    _create_toupcam_backend,
    _instantiate_backend,
    camera_owner_main,
)
from touptek4040.owner_protocol import (
    OwnerRequest,
    decode_acquire_start_ack,
    decode_response,
    encode_request,
)


class OwnerHarness:
    def __init__(
        self,
        temporary_path: Path,
        backend_arguments: dict[str, object] | None = None,
        *,
        backend_factory: str = "create_backend",
        backend_validator: str | None = None,
    ) -> None:
        context = multiprocessing.get_context("spawn")
        self._command_parent, command_child = context.Pipe()
        self._control_parent, control_child = context.Pipe()
        self._generation = 19
        self._request_id = 0
        self.registry_path = temporary_path / "frames.json"
        startup_arguments: dict[str, object] = {
            "serial": "FAKE-OWNER",
            "backend_module": "test.fixtures.fake_owner_backend",
            "backend_factory": backend_factory,
            "backend_arguments": backend_arguments or {},
        }
        if backend_validator is not None:
            startup_arguments["backend_validator"] = backend_validator
        self.process = context.Process(
            target=camera_owner_main,
            args=(
                command_child,
                control_child,
                OwnerStartupSpec(**startup_arguments),
                self._generation,
                str(self.registry_path),
            ),
        )
        self.process.start()
        command_child.close()
        control_child.close()

    def request(self, operation: str, arguments: dict[str, object]) -> dict[str, object]:
        self._request_id += 1
        request = OwnerRequest(1, self._generation, self._request_id, operation, arguments)
        self._command_parent.send(encode_request(request))
        assert self._command_parent.poll(3), f"owner did not answer {operation}"
        if operation == "acquire":
            decode_acquire_start_ack(self._command_parent.recv(), request)
            assert self._command_parent.poll(3), "owner did not finish acquire"
        response = decode_response(self._command_parent.recv(), request)
        assert response.error is None, response.error
        assert isinstance(response.result, dict)
        return dict(response.result)

    def raw_request(self, request_id: int, operation: str, arguments: dict[str, object]):
        request = OwnerRequest(1, self._generation, request_id, operation, arguments)
        self._command_parent.send(encode_request(request))
        assert self._command_parent.poll(3)
        if operation == "acquire":
            decode_acquire_start_ack(self._command_parent.recv(), request)
            assert self._command_parent.poll(3)
        return decode_response(self._command_parent.recv(), request)

    def close(self) -> None:
        if self.process.is_alive() and not self._command_parent.closed:
            try:
                self.request("shutdown", {})
            except (AssertionError, EOFError, OSError):
                pass
            self.process.join(3)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(3)
        if not self._command_parent.closed:
            self._command_parent.close()
        if not self._control_parent.closed:
            self._control_parent.close()


@pytest.fixture
def owner_harness(tmp_path: Path):
    harness = OwnerHarness(tmp_path)
    try:
        yield harness
    finally:
        harness.close()


def test_owner_runs_backend_in_a_different_spawned_process(owner_harness: OwnerHarness):
    response = owner_harness.request("diagnostics", {})
    assert response["backend_pid"] != os.getpid()
    assert response["backend_pid"] == owner_harness.process.pid


def test_constructing_the_owner_harness_does_not_import_the_sdk_loader(tmp_path: Path):
    sys.modules.pop("touptek4040.toupcam_loader", None)
    harness = OwnerHarness(tmp_path)
    try:
        assert "touptek4040.toupcam_loader" not in sys.modules
    finally:
        harness.close()


def test_spawned_owner_defers_backend_construction_until_explicit_connect(
    tmp_path: Path,
) -> None:
    lifecycle_path = tmp_path / "backend-lifecycle.jsonl"
    state_path = tmp_path / "backend-state.json"
    harness = OwnerHarness(
        tmp_path,
        {
            "lifecycle_path": str(lifecycle_path),
            "state_path": str(state_path),
        },
    )
    try:
        handshake = harness.request("handshake", {})
        diagnostics = harness.request("diagnostics", {})

        assert handshake["pid"] == harness.process.pid
        assert diagnostics["backend_pid"] == harness.process.pid
        assert lifecycle_path.exists() is False
        assert state_path.exists() is False

        harness.request("connect", {})

        assert _read_lifecycle(lifecycle_path)[:2] == [
            {
                "operation": "construct",
                "pid": harness.process.pid,
                "serial": "FAKE-OWNER",
            },
            {
                "operation": "connect",
                "pid": harness.process.pid,
                "serial": "FAKE-OWNER",
            },
        ]
    finally:
        harness.close()


def test_disconnected_device_command_rejects_without_constructing_backend(
    tmp_path: Path,
) -> None:
    lifecycle_path = tmp_path / "backend-lifecycle.jsonl"
    harness = OwnerHarness(
        tmp_path,
        {"lifecycle_path": str(lifecycle_path)},
    )
    try:
        response = harness.raw_request(1, "capabilities", {})

        assert response.error is not None
        assert "not connected" in response.error.message
        assert lifecycle_path.exists() is False
        assert harness.process.is_alive()
    finally:
        harness.close()


def test_wrong_identity_connect_fails_and_cleans_deferred_candidate(
    tmp_path: Path,
) -> None:
    lifecycle_path = tmp_path / "backend-lifecycle.jsonl"
    harness = OwnerHarness(
        tmp_path,
        {
            "lifecycle_path": str(lifecycle_path),
            "reported_serial": "WRONG-SERIAL",
            "reported_model": "wrong-model",
        },
        backend_factory="create_identity_checked_backend",
    )
    try:
        response = harness.raw_request(1, "connect", {})

        assert response.error is not None
        assert "approved camera model and serial" in response.error.message
        assert _read_lifecycle(lifecycle_path) == [
            {
                "operation": "construct",
                "pid": harness.process.pid,
                "serial": "FAKE-OWNER",
            },
            {
                "operation": "identity_rejected",
                "pid": harness.process.pid,
                "reported_model": "wrong-model",
                "reported_serial": "WRONG-SERIAL",
            },
            {
                "operation": "disconnect",
                "pid": harness.process.pid,
                "serial": "FAKE-OWNER",
            },
        ]
        harness._request_id = 1
        assert harness.request("diagnostics", {})["connected"] is False
        assert harness.process.is_alive()
    finally:
        harness.close()


@pytest.mark.parametrize(
    ("reported_serial", "reported_model"),
    [
        ("WRONG-SERIAL", "approved-model"),
        ("FAKE-OWNER", "wrong-model"),
    ],
)
def test_identity_rejection_retains_opened_candidate_for_outer_cleanup_retry(
    tmp_path: Path,
    reported_serial: str,
    reported_model: str,
) -> None:
    lifecycle_path = tmp_path / "backend-lifecycle.jsonl"
    state_path = tmp_path / "backend-state.json"
    harness = OwnerHarness(
        tmp_path,
        {
            "fail_disconnects": 1,
            "lifecycle_path": str(lifecycle_path),
            "reported_model": reported_model,
            "reported_serial": reported_serial,
            "state_path": str(state_path),
        },
        backend_factory="create_open_identity_probe_backend",
        backend_validator="validate_identity_probe_backend",
    )
    try:
        response = harness.raw_request(1, "connect", {})
        assert response.error is not None
        assert response.error.message == (
            "real owner requires the approved camera model and serial"
        )
        assert _read_fake_backend_state(state_path)["counts"]["disconnect"] == 1

        harness._command_parent.send(ExplodingOwnerPayload())
        assert harness._command_parent.poll(3)
        diagnostic = harness._command_parent.recv()
        harness.process.join(3)

        _assert_bounded_cleanup_diagnostic(diagnostic)
        errors = diagnostic["error"]["diagnostics"]["errors"]
        assert errors.count("fake disconnect failed") == 1
        assert harness.process.exitcode == 0
        state = _read_fake_backend_state(state_path)
        assert state["counts"]["connect"] == 1
        assert state["counts"]["disconnect"] == 2
        assert state["counts"].get("abort", 0) == 0
        assert state["connected"] is False
        assert [
            event["operation"] for event in _read_lifecycle(lifecycle_path)
        ] == [
            "construct",
            "connect",
            "identity_read",
            "identity_rejected",
            "disconnect",
            "disconnect",
        ]
    finally:
        harness.close()


@pytest.mark.parametrize(
    ("failed_disconnects", "cleanup_connected", "cleanup_error_count"),
    [(1, False, 1), (2, True, 2)],
)
def test_identity_property_failure_preserves_original_error_and_bounds_cleanup(
    tmp_path: Path,
    failed_disconnects: int,
    cleanup_connected: bool,
    cleanup_error_count: int,
) -> None:
    lifecycle_path = tmp_path / "backend-lifecycle.jsonl"
    state_path = tmp_path / "backend-state.json"
    harness = OwnerHarness(
        tmp_path,
        {
            "fail_disconnects": failed_disconnects,
            "identity_property_raises": True,
            "lifecycle_path": str(lifecycle_path),
            "state_path": str(state_path),
        },
        backend_factory="create_open_identity_probe_backend",
        backend_validator="validate_identity_probe_backend",
    )
    try:
        response = harness.raw_request(1, "connect", {})
        assert response.error is not None
        assert response.error.message == "identity property failed"
        assert _read_fake_backend_state(state_path)["counts"]["disconnect"] == 1

        harness._command_parent.send(ExplodingOwnerPayload())
        assert harness._command_parent.poll(3)
        diagnostic = harness._command_parent.recv()
        harness.process.join(3)

        _assert_bounded_cleanup_diagnostic(diagnostic)
        errors = diagnostic["error"]["diagnostics"]["errors"]
        assert errors.count("fake disconnect failed") == cleanup_error_count
        assert "identity property failed" not in errors
        assert harness.process.exitcode == 0
        state = _read_fake_backend_state(state_path)
        assert state["counts"]["connect"] == 1
        assert state["counts"]["disconnect"] == 2
        assert state["counts"].get("abort", 0) == 0
        assert state["connected"] is cleanup_connected
        assert [
            event["operation"] for event in _read_lifecycle(lifecycle_path)
        ] == [
            "construct",
            "connect",
            "identity_read",
            "disconnect",
            "disconnect",
        ]
    finally:
        harness.close()


def test_multiple_connect_disconnect_cycles_construct_fresh_backends(
    tmp_path: Path,
) -> None:
    lifecycle_path = tmp_path / "backend-lifecycle.jsonl"
    harness = OwnerHarness(
        tmp_path,
        {"lifecycle_path": str(lifecycle_path)},
    )
    try:
        harness.request("connect", {})
        harness.request("disconnect", {})
        harness.request("connect", {})
        harness.request("disconnect", {})

        lifecycle = _read_lifecycle(lifecycle_path)
        assert [event["operation"] for event in lifecycle] == [
            "construct",
            "connect",
            "disconnect",
            "construct",
            "connect",
            "disconnect",
        ]
        assert {event["pid"] for event in lifecycle} == {harness.process.pid}
    finally:
        harness.close()


def test_owner_heater_operation_runs_in_the_backend_process(owner_harness: OwnerHarness):
    owner_harness.request("connect", {})
    response = owner_harness.request("get_heater_capabilities", {})
    owner_harness.request("set_heater_level", {"value": 3})

    assert response == {"supported": True, "max_level": 4}
    assert owner_harness.request("diagnostics", {})["backend_pid"] != os.getpid()
    assert owner_harness.request("get_heater_level", {}) == {"value": 3}


def test_owner_publishes_and_releases_a_frame(owner_harness: OwnerHarness):
    owner_harness.request("connect", {})
    response = owner_harness.request(
        "acquire",
        {
            "duration_s": 0.0000124,
            "roi": {"x": 0, "y": 0, "width": 2, "height": 2},
            "binning": 1,
            "readout_mode": 0,
        },
    )

    descriptor = response["descriptor"]
    assert isinstance(descriptor, dict)
    assert response["frame"]["duration_s"] == 0.000012
    owner_harness.request("release_frame", {"descriptor": descriptor})
    assert owner_harness.request("diagnostics", {})["owned_frame_count"] == 0


def test_owner_acknowledges_microsecond_clamp_before_blocking_acquire(
    tmp_path: Path,
):
    state_path = tmp_path / "backend-state.json"
    harness = OwnerHarness(
        tmp_path,
        {"block_acquire": True, "state_path": str(state_path)},
    )
    try:
        harness.request("connect", {})
        harness._request_id += 1
        request = OwnerRequest(
            1,
            harness._generation,
            harness._request_id,
            "acquire",
            {
                "duration_s": 0.0000124,
                "roi": {"x": 0, "y": 0, "width": 2, "height": 2},
                "binning": 1,
                "readout_mode": 0,
            },
        )
        harness._command_parent.send(encode_request(request))

        assert harness._command_parent.poll(1)
        ack = decode_acquire_start_ack(
            harness._command_parent.recv(), request
        )
        assert ack.actual_duration_s == 0.000012
        state = _read_fake_backend_state(state_path)
        assert state["counts"]["acquire"] == 1
        assert state["events"][-1]["duration_s"] == 0.000012
        assert harness._command_parent.poll(0.05) is False
        assert harness.process.is_alive()
        harness._control_parent.send({"operation": "abort"})
        assert harness._control_parent.poll(1)
        assert harness._control_parent.recv()["ok"] is True
        assert harness._command_parent.poll(1)
        response = decode_response(harness._command_parent.recv(), request)
        assert response.error is not None
    finally:
        harness.close()


def test_normal_owner_disconnect_restores_the_connect_time_heater_level(tmp_path: Path):
    restoration_log = tmp_path / "restored-heater.txt"
    state_path = tmp_path / "backend-state.json"
    harness = OwnerHarness(
        tmp_path,
        {
            "restore_log_path": str(restoration_log),
            "state_path": str(state_path),
        },
    )
    try:
        harness.request("connect", {})
        harness.request("set_heater_level", {"value": 4})
        harness.request("disconnect", {})
        assert restoration_log.read_text(encoding="utf-8") == "2"
        state = _read_fake_backend_state(state_path)
        assert state["counts"]["disconnect"] == 1
        assert _heater_write_count(state, 2) == 1
    finally:
        harness.close()


def test_normal_owner_shutdown_restores_the_connect_time_heater_level_once(
    tmp_path: Path,
):
    state_path = tmp_path / "backend-state.json"
    harness = OwnerHarness(tmp_path, {"state_path": str(state_path)})
    try:
        harness.request("connect", {})
        harness.request("set_heater_level", {"value": 4})
        assert harness.request("shutdown", {}) == {"shutdown": True}
        harness.process.join(3)

        assert harness.process.exitcode == 0
        state = _read_fake_backend_state(state_path)
        assert state["counts"]["disconnect"] == 1
        assert _heater_write_count(state, 2) == 1
    finally:
        harness.close()


def test_owner_rejects_duplicate_request_ids_before_repeating_a_setter(owner_harness: OwnerHarness):
    owner_harness.request("connect", {})
    first = owner_harness.raw_request(50, "set_heater_level", {"value": 3})
    duplicate = owner_harness.raw_request(50, "set_heater_level", {"value": 4})

    assert first.error is None
    assert duplicate.error is not None
    owner_harness._request_id = 50
    assert owner_harness.request("get_heater_level", {}) == {"value": 3}


def test_startup_spec_rejects_non_serializable_backend_arguments():
    with pytest.raises(ValueError):
        OwnerStartupSpec(backend_arguments={"nested": [lambda: None]})


@pytest.mark.parametrize("serial", [None, "", "WRONG"])
def test_real_owner_rejects_any_non_approved_identity_before_loading_sdk(serial):
    with pytest.raises(ValueError):
        _create_toupcam_backend(serial)


@pytest.mark.parametrize(
    ("model", "raises"),
    [
        ("sMAX16AM-U3-CL-GPS", False),
        ("wrong-model", True),
    ],
)
def test_production_backend_registers_opened_exact_serial_before_identity_acceptance(
    monkeypatch,
    model: str,
    raises: bool,
) -> None:
    events: list[str] = []
    registered: list[FakeOwnerBackend] = []
    approved_serial = "TP241025143218C92B01151AEC40CB7"

    class Candidate(FakeOwnerBackend):
        def __init__(self, serial: str) -> None:
            events.append("construct")
            assert serial == approved_serial
            super().__init__(serial)
            self.identity = type("Identity", (), {"model": model})()

        def connect(self) -> None:
            events.append("connect")
            super().connect()

        def disconnect(self) -> None:
            events.append("disconnect")
            super().disconnect()

    module = ModuleType("touptek4040.toupcam_camera")
    module.ToupTekCamera = Candidate
    monkeypatch.setitem(sys.modules, "touptek4040.toupcam_camera", module)

    try:
        if raises:
            with pytest.raises(ValueError, match="approved camera model and serial"):
                _instantiate_backend(
                    OwnerStartupSpec(serial=approved_serial),
                    registered.append,
                )
        else:
            assert _instantiate_backend(
                OwnerStartupSpec(serial=approved_serial),
                registered.append,
            ).serial == approved_serial

        assert len(registered) == 1
        assert registered[0].serial == approved_serial
        assert events == ["construct", "connect"]
    finally:
        for candidate in registered:
            candidate.disconnect()


def test_control_shutdown_aborts_a_truly_blocked_spawned_acquisition(tmp_path: Path):
    harness = OwnerHarness(tmp_path, {"block_acquire": True})
    try:
        harness.request("connect", {})
        failure: list[BaseException] = []
        def acquire() -> None:
            try:
                harness.request("acquire", {"duration_s": 1, "roi": {"x": 0, "y": 0, "width": 2, "height": 2}, "binning": 1, "readout_mode": 0})
            except BaseException as error:
                failure.append(error)
        worker = Thread(target=acquire)
        worker.start()
        time.sleep(0.1)
        harness._control_parent.send({"operation": "shutdown"})
        assert harness._control_parent.poll(3)
        assert harness._control_parent.recv()["ok"] is True
        worker.join(3)
        harness.process.join(3)
        assert not harness.process.is_alive()
        assert failure
    finally:
        harness.close()


def test_control_shutdown_before_connect_responds_then_closes_without_factory_events(
    tmp_path: Path,
) -> None:
    lifecycle_path = tmp_path / "backend-lifecycle.jsonl"
    harness = OwnerHarness(
        tmp_path,
        {"lifecycle_path": str(lifecycle_path)},
    )
    try:
        harness._control_parent.send({"operation": "abort"})
        assert harness._control_parent.poll(3)
        assert harness._control_parent.recv() == {
            "operation": "abort",
            "ok": False,
            "error": "camera is not connected",
        }

        harness._control_parent.send({"operation": "shutdown"})
        assert harness._control_parent.poll(3)
        assert harness._control_parent.recv() == {
            "operation": "shutdown",
            "ok": True,
        }

        harness.process.join(3)
        assert harness.process.exitcode == 0
        with pytest.raises(EOFError):
            harness._control_parent.recv()
        with pytest.raises(EOFError):
            harness._command_parent.recv()
        assert lifecycle_path.exists() is False
    finally:
        harness.close()


def test_control_rejects_abort_and_accepts_shutdown_while_candidate_validates(
    tmp_path: Path,
) -> None:
    validation_entered = tmp_path / "validation-entered"
    validation_release = tmp_path / "validation-release"
    state_path = tmp_path / "backend-state.json"
    harness = OwnerHarness(
        tmp_path,
        {
            "state_path": str(state_path),
            "validation_barrier_entered_path": str(validation_entered),
            "validation_barrier_release_path": str(validation_release),
        },
        backend_factory="create_open_identity_probe_backend",
        backend_validator="validate_identity_probe_backend",
    )
    responses: list[object] = []
    worker = Thread(
        target=lambda: responses.append(harness.raw_request(1, "connect", {}))
    )
    try:
        worker.start()
        _wait_for_path(validation_entered)

        harness._control_parent.send({"operation": "abort"})
        assert harness._control_parent.poll(3)
        assert harness._control_parent.recv() == {
            "operation": "abort",
            "ok": False,
            "error": "camera is not connected",
        }
        harness._control_parent.send({"operation": "shutdown"})
        assert harness._control_parent.poll(3)
        assert harness._control_parent.recv() == {
            "operation": "shutdown",
            "ok": True,
        }
        assert _read_fake_backend_state(state_path)["counts"].get("abort", 0) == 0

        validation_release.touch()
        worker.join(3)
        harness.process.join(3)

        assert not worker.is_alive()
        assert len(responses) == 1
        assert responses[0].error is not None
        assert harness.process.exitcode == 0
        state = _read_fake_backend_state(state_path)
        assert state["counts"]["connect"] == 1
        assert state["counts"]["disconnect"] == 1
        assert state["counts"].get("abort", 0) == 0
        assert state["connected"] is False
        with pytest.raises(EOFError):
            harness._control_parent.recv()
    finally:
        validation_release.touch()
        worker.join(3)
        harness.close()


def test_control_rejects_abort_and_accepts_shutdown_during_command_cleanup(
    tmp_path: Path,
) -> None:
    disconnect_entered = tmp_path / "disconnect-entered"
    disconnect_release = tmp_path / "disconnect-release"
    state_path = tmp_path / "backend-state.json"
    harness = OwnerHarness(
        tmp_path,
        {
            "block_disconnects": 1,
            "disconnect_barrier_entered_path": str(disconnect_entered),
            "disconnect_barrier_release_path": str(disconnect_release),
            "state_path": str(state_path),
        },
    )
    responses: list[object] = []
    worker = Thread(
        target=lambda: responses.append(harness.request("disconnect", {}))
    )
    try:
        harness.request("connect", {})
        worker.start()
        _wait_for_path(disconnect_entered)

        harness._control_parent.send({"operation": "abort"})
        assert harness._control_parent.poll(3)
        assert harness._control_parent.recv() == {
            "operation": "abort",
            "ok": False,
            "error": "camera is not connected",
        }
        harness._control_parent.send({"operation": "shutdown"})
        assert harness._control_parent.poll(3)
        assert harness._control_parent.recv() == {
            "operation": "shutdown",
            "ok": True,
        }
        assert _read_fake_backend_state(state_path)["counts"].get("abort", 0) == 0

        disconnect_release.touch()
        worker.join(3)
        harness.process.join(3)

        assert not worker.is_alive()
        assert responses == [{"connected": False}]
        assert harness.process.exitcode == 0
        state = _read_fake_backend_state(state_path)
        assert state["counts"]["disconnect"] == 1
        assert state["counts"].get("abort", 0) == 0
        assert state["connected"] is False
        with pytest.raises(EOFError):
            harness._control_parent.recv()
    finally:
        disconnect_release.touch()
        worker.join(3)
        harness.close()


def test_control_shutdown_during_blocked_failed_rollback_is_stable_and_bounded(
    tmp_path: Path,
) -> None:
    disconnect_entered = tmp_path / "rollback-entered"
    disconnect_release = tmp_path / "rollback-release"
    state_path = tmp_path / "backend-state.json"
    harness = OwnerHarness(
        tmp_path,
        {
            "block_disconnects": 1,
            "disconnect_barrier_entered_path": str(disconnect_entered),
            "disconnect_barrier_release_path": str(disconnect_release),
            "fail_disconnects": 1,
            "fail_snapshot": True,
            "state_path": str(state_path),
        },
    )
    responses: list[object] = []
    worker = Thread(
        target=lambda: responses.append(harness.raw_request(1, "connect", {}))
    )
    try:
        worker.start()
        _wait_for_path(disconnect_entered)

        harness._control_parent.send({"operation": "abort"})
        assert harness._control_parent.poll(3)
        assert harness._control_parent.recv() == {
            "operation": "abort",
            "ok": False,
            "error": "camera is not connected",
        }
        harness._control_parent.send({"operation": "shutdown"})
        assert harness._control_parent.poll(3)
        assert harness._control_parent.recv() == {
            "operation": "shutdown",
            "ok": True,
        }
        assert _read_fake_backend_state(state_path)["counts"].get("abort", 0) == 0

        disconnect_release.touch()
        worker.join(3)
        assert harness._command_parent.poll(3)
        diagnostic = harness._command_parent.recv()
        harness.process.join(3)

        assert not worker.is_alive()
        assert len(responses) == 1
        assert responses[0].error is not None
        assert responses[0].error.message == "snapshot failed"
        _assert_bounded_cleanup_diagnostic(diagnostic)
        assert diagnostic["error"]["diagnostics"]["errors"].count(
            "fake disconnect failed"
        ) == 1
        assert harness.process.exitcode == 0
        state = _read_fake_backend_state(state_path)
        assert state["counts"]["disconnect"] == 2
        assert state["counts"].get("abort", 0) == 0
        assert state["connected"] is False
        with pytest.raises(EOFError):
            harness._control_parent.recv()
    finally:
        disconnect_release.touch()
        worker.join(3)
        harness.close()


def test_control_shutdown_uses_captured_backend_if_command_clears_owner_reference(
    tmp_path: Path,
) -> None:
    abort_entered = tmp_path / "abort-entered"
    abort_release = tmp_path / "abort-release"
    state_path = tmp_path / "backend-state.json"
    harness = OwnerHarness(
        tmp_path,
        {
            "abort_barrier_entered_path": str(abort_entered),
            "abort_barrier_release_path": str(abort_release),
            "block_aborts": 1,
            "state_path": str(state_path),
        },
    )
    responses: list[object] = []
    worker = Thread(
        target=lambda: responses.append(harness.request("disconnect", {}))
    )
    try:
        harness.request("connect", {})
        harness._control_parent.send({"operation": "shutdown"})
        _wait_for_path(abort_entered)

        worker.start()
        worker.join(3)
        assert not worker.is_alive()
        assert responses == [{"connected": False}]
        abort_release.touch()

        assert harness._control_parent.poll(3)
        assert harness._control_parent.recv() == {
            "operation": "shutdown",
            "ok": True,
        }
        harness.process.join(3)

        assert harness.process.exitcode == 0
        state = _read_fake_backend_state(state_path)
        assert state["counts"]["abort"] == 1
        assert state["counts"]["disconnect"] == 1
        assert state["connected"] is False
        with pytest.raises(EOFError):
            harness._control_parent.recv()
    finally:
        abort_release.touch()
        worker.join(3)
        harness.close()


def test_request_id_zero_then_one_are_accepted_and_zero_replay_is_rejected(owner_harness: OwnerHarness):
    first = owner_harness.raw_request(0, "handshake", {})
    second = owner_harness.raw_request(1, "diagnostics", {})
    replay = owner_harness.raw_request(0, "handshake", {})

    assert first.error is None
    assert second.error is None
    assert replay.error is not None


def test_control_messages_always_receive_a_bounded_response(owner_harness: OwnerHarness):
    for message in ({}, {"operation": "unsupported"}, {"operation": "abort"}):
        owner_harness._control_parent.send(message)
        assert owner_harness._control_parent.poll(3)
        response = owner_harness._control_parent.recv()
        assert response["ok"] is False
        assert len(response["error"]) <= 256


@pytest.mark.parametrize(
    ("message", "expected_operation"),
    [
        ({"operation": "unsupported"}, "unsupported"),
        ({"operation": "abort", "extra": True}, "abort"),
        ({"operation": "shutdown", "extra": True}, "shutdown"),
        ({"operation": 1}, "invalid_control"),
        ({"operation": ""}, "invalid_control"),
        ({}, "invalid_control"),
        ([], "invalid_control"),
    ],
)
def test_rejected_control_preserves_correlation_without_backend_invocation(
    tmp_path: Path,
    message: object,
    expected_operation: str,
):
    lifecycle_path = tmp_path / "backend-lifecycle.jsonl"
    harness = OwnerHarness(
        tmp_path,
        {"lifecycle_path": str(lifecycle_path)},
    )
    try:
        assert lifecycle_path.exists() is False
        harness._control_parent.send(message)
        assert harness._control_parent.poll(3)
        response = harness._control_parent.recv()

        assert response["operation"] == expected_operation
        assert response["ok"] is False
        assert len(response["error"]) <= 256
        assert not harness._control_parent.poll(0.1)
        assert lifecycle_path.exists() is False
        assert harness.process.is_alive()
    finally:
        harness.close()


@pytest.mark.parametrize("operation", ["abort", "shutdown"])
def test_control_extra_key_never_executes_the_requested_operation(owner_harness: OwnerHarness, operation: str):
    owner_harness._control_parent.send({"operation": operation, "extra": True})
    assert owner_harness._control_parent.poll(3)
    response = owner_harness._control_parent.recv()
    assert response == {"operation": operation, "ok": False, "error": "control message has an invalid schema"}
    assert owner_harness.process.is_alive()


def test_control_abort_failure_is_returned_to_the_parent(tmp_path: Path):
    harness = OwnerHarness(tmp_path, {"fail_abort": True})
    try:
        harness.request("connect", {})
        harness._control_parent.send({"operation": "abort"})
        assert harness._control_parent.poll(3)
        response = harness._control_parent.recv()
        assert response["operation"] == "abort" and response["ok"] is False
    finally:
        harness.close()


def test_control_shutdown_abort_failure_is_correlated_and_reports_final_cleanup(
    tmp_path: Path,
):
    state_path = tmp_path / "backend-state.json"
    harness = OwnerHarness(
        tmp_path,
        {"fail_abort": True, "state_path": str(state_path)},
    )
    try:
        harness.request("connect", {})
        harness.request("set_heater_level", {"value": 4})
        harness._control_parent.send({"operation": "shutdown"})
        assert harness._control_parent.poll(3)
        assert harness._control_parent.recv() == {
            "operation": "shutdown",
            "ok": False,
            "error": "fake abort failed",
        }
        assert harness._command_parent.poll(3)
        diagnostic = harness._command_parent.recv()
        harness.process.join(3)

        _assert_bounded_cleanup_diagnostic(diagnostic)
        with pytest.raises(EOFError):
            harness._command_parent.recv()
        assert harness.process.exitcode == 0
        state = _read_fake_backend_state(state_path)
        assert state["counts"]["abort"] == 2
        assert state["counts"]["disconnect"] == 1
        assert _heater_write_count(state, 2) == 1
    finally:
        harness.close()


def test_fresh_interpreter_import_of_owner_does_not_import_sdk_loader(tmp_path: Path):
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import touptek4040.camera_owner; assert 'touptek4040.toupcam_loader' not in sys.modules"],
        cwd=tmp_path.parent,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("failed_disconnects", "cleanup_connected"),
    [(1, False), (2, True)],
)
def test_outer_cleanup_retries_snapshot_rollback_and_preserves_original_error(
    tmp_path: Path,
    failed_disconnects: int,
    cleanup_connected: bool,
):
    state_path = tmp_path / "backend-state.json"
    harness = OwnerHarness(
        tmp_path,
        {
            "fail_snapshot": True,
            "fail_disconnects": failed_disconnects,
            "state_path": str(state_path),
        },
    )
    try:
        response = harness.raw_request(1, "connect", {})
        assert response.error is not None
        assert response.error.message == "snapshot failed"
        assert _read_fake_backend_state(state_path)["counts"]["disconnect"] == 1
        harness._command_parent.close()
        harness.process.join(3)

        assert harness.process.exitcode == 0
        state = _read_fake_backend_state(state_path)
        assert state["counts"].get("abort", 0) == 0
        assert state["counts"]["disconnect"] == 2
        assert state["connected"] is cleanup_connected
    finally:
        harness.close()


def test_snapshot_rollback_failure_is_reported_separately_on_outer_exit(
    tmp_path: Path,
):
    state_path = tmp_path / "backend-state.json"
    harness = OwnerHarness(
        tmp_path,
        {
            "fail_snapshot": True,
            "fail_disconnects": 1,
            "state_path": str(state_path),
        },
    )
    try:
        response = harness.raw_request(1, "connect", {})
        assert response.error is not None
        assert response.error.message == "snapshot failed"
        harness._command_parent.send(ExplodingOwnerPayload())
        assert harness._command_parent.poll(3)
        diagnostic = harness._command_parent.recv()
        harness.process.join(3)

        _assert_bounded_cleanup_diagnostic(diagnostic)
        errors = diagnostic["error"]["diagnostics"]["errors"]
        assert "fake disconnect failed" in errors
        assert harness.process.exitcode == 0
        state = _read_fake_backend_state(state_path)
        assert state["counts"].get("abort", 0) == 0
        assert state["counts"]["disconnect"] == 2
    finally:
        harness.close()


def test_genuine_outer_finally_restores_heater_once_and_emits_one_diagnostic(
    tmp_path: Path,
):
    state_path = tmp_path / "backend-state.json"
    harness = OwnerHarness(tmp_path, {"state_path": str(state_path)})
    try:
        harness.request("connect", {})
        harness.request("set_heater_level", {"value": 4})
        harness._command_parent.send(ExplodingOwnerPayload())
        assert harness._command_parent.poll(3)
        diagnostic = harness._command_parent.recv()
        harness.process.join(3)

        _assert_bounded_cleanup_diagnostic(diagnostic)
        with pytest.raises(EOFError):
            harness._command_parent.recv()
        assert harness.process.exitcode == 0
        state = _read_fake_backend_state(state_path)
        assert state["counts"]["abort"] == 1
        assert state["counts"]["disconnect"] == 1
        assert _heater_write_count(state, 2) == 1
    finally:
        harness.close()


def test_malformed_frame_response_releases_registry_and_publisher_ownership(
    tmp_path: Path,
):
    state_path = tmp_path / "backend-state.json"
    harness = OwnerHarness(
        tmp_path,
        {"malformed_frame": True, "state_path": str(state_path)},
    )
    try:
        harness.request("connect", {})
        response = harness.raw_request(
            2,
            "acquire",
            {
                "duration_s": 0.001,
                "roi": {"x": 0, "y": 0, "width": 2, "height": 2},
                "binning": 1,
                "readout_mode": 0,
            },
        )
        assert response.error is not None
        assert response.result is None
        assert "JSON values" in response.error.message
        harness._request_id = 2
        assert harness.request("diagnostics", {})["owned_frame_count"] == 0
        registry = json.loads(harness.registry_path.read_text(encoding="utf-8"))
        assert registry == {"frames": []}
        assert _read_fake_backend_state(state_path)["counts"]["acquire"] == 1
    finally:
        harness.close()


def test_control_abort_unblocks_a_blocked_acquisition_without_stopping_owner(tmp_path: Path):
    harness = OwnerHarness(tmp_path, {"block_acquire": True})
    try:
        harness.request("connect", {})
        failure: list[BaseException] = []
        worker = Thread(target=lambda: _capture_acquire_failure(harness, failure))
        worker.start()
        time.sleep(0.1)
        harness._control_parent.send({"operation": "abort"})
        assert harness._control_parent.poll(3)
        assert harness._control_parent.recv()["ok"] is True
        worker.join(3)
        assert failure and harness.process.is_alive()
    finally:
        harness.close()


def _capture_acquire_failure(harness: OwnerHarness, failures: list[BaseException]) -> None:
    try:
        harness.request("acquire", {"duration_s": 1, "roi": {"x": 0, "y": 0, "width": 2, "height": 2}, "binning": 1, "readout_mode": 0})
    except BaseException as error:
        failures.append(error)


def _read_fake_backend_state(path: Path) -> dict[str, object]:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.01)
            continue
        assert isinstance(state, dict)
        return state
    raise AssertionError("fake backend state was not published")


def _read_lifecycle(path: Path) -> list[dict[str, object]]:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            return [json.loads(line) for line in lines if line]
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.01)
    raise AssertionError("fake backend lifecycle was not published")


def _wait_for_path(path: Path) -> None:
    deadline = time.monotonic() + 3
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"owner barrier was not reached: {path.name}")
        time.sleep(0.005)


def _heater_write_count(state: dict[str, object], value: int) -> int:
    events = state["events"]
    assert isinstance(events, list)
    return sum(
        event.get("operation") == "set_heater_level" and event.get("value") == value
        for event in events
        if isinstance(event, dict)
    )


def _assert_bounded_cleanup_diagnostic(payload: object) -> None:
    assert isinstance(payload, dict)
    assert payload["operation"] == "cleanup"
    assert payload["request_id"] == 0
    error = payload["error"]
    assert isinstance(error, dict)
    assert error["code"] == "cleanup_failed"
    diagnostics = error["diagnostics"]
    assert isinstance(diagnostics, dict)
    errors = diagnostics["errors"]
    assert isinstance(errors, list)
    assert 1 <= len(errors) <= 4
    assert all(isinstance(item, str) and len(item) <= 256 for item in errors)
    assert len(json.dumps(payload)) <= 2048
