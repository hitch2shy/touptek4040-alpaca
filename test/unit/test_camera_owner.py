"""Process-boundary tests for the isolated camera owner."""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
import sys
import time

import pytest

from touptek4040.camera_owner import OwnerStartupSpec, camera_owner_main
from touptek4040.owner_protocol import OwnerRequest, decode_response, encode_request


class OwnerHarness:
    def __init__(self, temporary_path: Path, backend_arguments: dict[str, object] | None = None) -> None:
        context = multiprocessing.get_context("spawn")
        self._command_parent, command_child = context.Pipe()
        self._control_parent, control_child = context.Pipe()
        self._generation = 19
        self._request_id = 0
        self.process = context.Process(
            target=camera_owner_main,
            args=(
                command_child,
                control_child,
                OwnerStartupSpec(
                    serial="FAKE-OWNER",
                    backend_module="test.fixtures.fake_owner_backend",
                    backend_factory="create_backend",
                    backend_arguments=backend_arguments or {},
                ),
                self._generation,
                str(temporary_path / "frames.json"),
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
        response = decode_response(self._command_parent.recv(), request)
        assert response.error is None, response.error
        assert isinstance(response.result, dict)
        return dict(response.result)

    def close(self) -> None:
        if self.process.is_alive():
            try:
                self.request("shutdown", {})
            except (AssertionError, EOFError, OSError):
                pass
            self.process.join(3)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(3)
        self._command_parent.close()
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
            "duration_s": 0.001,
            "roi": {"x": 0, "y": 0, "width": 2, "height": 2},
            "binning": 1,
            "readout_mode": 0,
        },
    )

    descriptor = response["descriptor"]
    assert isinstance(descriptor, dict)
    owner_harness.request("release_frame", {"descriptor": descriptor})
    assert owner_harness.request("diagnostics", {})["owned_frame_count"] == 0


def test_normal_owner_disconnect_restores_the_connect_time_heater_level(tmp_path: Path):
    restoration_log = tmp_path / "restored-heater.txt"
    harness = OwnerHarness(tmp_path, {"restore_log_path": str(restoration_log)})
    try:
        harness.request("connect", {})
        harness.request("set_heater_level", {"value": 4})
        harness.request("disconnect", {})
        assert restoration_log.read_text(encoding="utf-8") == "2"
    finally:
        harness.close()
