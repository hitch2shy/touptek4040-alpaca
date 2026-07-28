from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import numpy as np
import pytest

from touptek4040.camera_contract import CameraCapabilities, Frame, ReadoutMode, Roi
from touptek4040.camera_coordinator import CameraCoordinator, CameraState
from touptek4040.errors import (
    CameraBusyError,
    CameraError,
    CameraNotConnectedError,
)


def wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        time.sleep(0.005)


class ControlledCamera:
    capabilities = CameraCapabilities(
        width=8,
        height=6,
        pixel_size_x=9.0,
        pixel_size_y=9.0,
        readout_modes=("RAW 12-bit", "RAW 8-bit"),
        max_bin=8,
        can_abort=True,
        can_stop=False,
    )

    def __init__(self) -> None:
        self.connected = False
        self._target_temperature_c = 0.0
        self._cooler_on = False
        self.connect_started = threading.Event()
        self.connect_release = threading.Event()
        self.connect_release.set()
        self.disconnect_started = threading.Event()
        self.disconnect_release = threading.Event()
        self.disconnect_release.set()
        self._releases = threading.Semaphore(0)
        self._acquisition_condition = threading.Condition()
        self.acquire_count = 0
        self.active_acquisitions = 0
        self.max_active_acquisitions = 0
        self.abort_count = 0
        self.failure: Exception | None = None

    def connect(self) -> None:
        self.connect_started.set()
        self.connect_release.wait()
        self.connected = True

    def disconnect(self) -> None:
        self.disconnect_started.set()
        self.disconnect_release.wait()
        self.connected = False

    def acquire(
        self, duration_s: float, roi: Roi, binning: int, readout_mode: ReadoutMode
    ) -> Frame:
        with self._acquisition_condition:
            self.acquire_count += 1
            sequence = self.acquire_count
            self.active_acquisitions += 1
            self.max_active_acquisitions = max(
                self.max_active_acquisitions, self.active_acquisitions
            )
            self._acquisition_condition.notify_all()
        self._releases.acquire()
        with self._acquisition_condition:
            self.active_acquisitions -= 1
            self._acquisition_condition.notify_all()
        if self.failure is not None:
            raise self.failure
        pixels = np.full((roi.height, roi.width), sequence, dtype=np.uint16)
        return Frame(
            pixels=pixels,
            started_utc=datetime.now(timezone.utc),
            duration_s=duration_s,
            binning=binning,
            roi=roi,
            readout_mode=readout_mode,
        )

    def abort(self) -> None:
        self.abort_count += 1

    @property
    def temperature_c(self) -> float:
        return 20.0

    @property
    def target_temperature_c(self) -> float:
        return self._target_temperature_c

    @target_temperature_c.setter
    def target_temperature_c(self, value: float) -> None:
        self._target_temperature_c = value

    @property
    def cooler_on(self) -> bool:
        return self._cooler_on

    @cooler_on.setter
    def cooler_on(self, enabled: bool) -> None:
        self._cooler_on = enabled

    def tec_voltage(self) -> tuple[float, float]:
        return (3.0 if self._cooler_on else 0.0, 6.0)

    @property
    def cooler_power_percent(self) -> float:
        return 50.0 if self._cooler_on else 0.0

    def release_frame(self) -> None:
        self._releases.release()

    def wait_for_acquisitions(self, count: int, timeout: float = 1.0) -> None:
        deadline = time.monotonic() + timeout
        with self._acquisition_condition:
            while self.acquire_count < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"acquisition {count} did not start")
                self._acquisition_condition.wait(remaining)


@pytest.fixture
def controlled_camera():
    camera = ControlledCamera()
    yield camera
    for _ in range(8):
        camera.release_frame()
    camera.connect_release.set()
    camera.disconnect_release.set()


@pytest.fixture
def coordinator(controlled_camera):
    value = CameraCoordinator(lambda source: controlled_camera, "controlled")
    value.set_connected_sync(True)
    yield value
    value.disconnect_async()


def test_connect_async_is_non_blocking_and_reports_transition(controlled_camera):
    controlled_camera.connect_release.clear()
    seen_sources: list[str] = []
    coordinator = CameraCoordinator(
        lambda source: seen_sources.append(source) or controlled_camera,
        "camera-4040",
    )

    started = time.monotonic()
    coordinator.connect_async()

    assert time.monotonic() - started < 0.1
    assert coordinator.snapshot().connecting is True
    assert coordinator.snapshot().connected is False
    controlled_camera.connect_release.set()
    wait_until(lambda: coordinator.snapshot().connecting is False)
    assert coordinator.snapshot().connected is True
    assert seen_sources == ["camera-4040"]


def test_set_connected_sync_opens_and_closes_the_owned_camera(controlled_camera):
    coordinator = CameraCoordinator(lambda source: controlled_camera, "controlled")

    coordinator.set_connected_sync(True)
    assert coordinator.snapshot().connected is True
    coordinator.set_connected_sync(False)

    assert coordinator.snapshot().connected is False
    assert controlled_camera.connected is False


def test_start_exposure_is_non_blocking(coordinator, controlled_camera):
    started = time.monotonic()
    coordinator.start_exposure(10.0, True)

    assert time.monotonic() - started < 0.1
    assert coordinator.snapshot().camera_state == CameraState.EXPOSING
    controlled_camera.wait_for_acquisitions(1)


def test_completed_exposure_atomically_publishes_formal_frame(
    coordinator, controlled_camera
):
    coordinator.start_exposure(0.1, True)
    controlled_camera.wait_for_acquisitions(1)

    controlled_camera.release_frame()
    wait_until(lambda: coordinator.snapshot().image_ready)

    snapshot = coordinator.snapshot()
    assert snapshot.camera_state == CameraState.IDLE
    assert snapshot.error is None
    assert snapshot.percent_completed is None
    assert coordinator.frame is not None
    assert int(coordinator.frame.pixels[0, 0]) == 1


def test_percent_completed_is_available_only_during_exposure(
    coordinator, controlled_camera
):
    assert coordinator.snapshot().percent_completed is None

    coordinator.start_exposure(0.05, True)
    controlled_camera.wait_for_acquisitions(1)
    time.sleep(0.03)

    assert 1 <= coordinator.snapshot().percent_completed <= 100
    controlled_camera.release_frame()
    wait_until(lambda: coordinator.snapshot().camera_state == CameraState.IDLE)
    assert coordinator.snapshot().percent_completed is None


def test_late_frame_after_abort_is_never_published(coordinator, controlled_camera):
    coordinator.start_exposure(10.0, True)
    controlled_camera.wait_for_acquisitions(1)

    coordinator.abort_exposure()
    controlled_camera.release_frame()
    wait_until(lambda: controlled_camera.active_acquisitions == 0)

    snapshot = coordinator.snapshot()
    assert snapshot.camera_state == CameraState.IDLE
    assert snapshot.image_ready is False
    assert coordinator.frame is None


def test_async_acquisition_failure_enters_error_without_image(
    coordinator, controlled_camera
):
    controlled_camera.failure = RuntimeError("sensor transfer failed")
    coordinator.start_exposure(0.1, True)
    controlled_camera.wait_for_acquisitions(1)

    controlled_camera.release_frame()
    wait_until(lambda: coordinator.snapshot().camera_state == CameraState.ERROR)

    snapshot = coordinator.snapshot()
    assert snapshot.image_ready is False
    assert isinstance(snapshot.error, CameraError)
    assert "sensor transfer failed" in str(snapshot.error)


def test_exposure_requires_connection():
    coordinator = CameraCoordinator(lambda source: ControlledCamera(), "controlled")

    with pytest.raises(CameraNotConnectedError):
        coordinator.start_exposure(1.0, True)


def test_second_formal_exposure_is_rejected_while_busy(
    coordinator, controlled_camera
):
    coordinator.start_exposure(1.0, True)
    controlled_camera.wait_for_acquisitions(1)

    with pytest.raises(CameraBusyError):
        coordinator.start_exposure(1.0, True)


def test_coordinator_is_the_only_gateway_to_capabilities_and_cooling(
    coordinator, controlled_camera
):
    assert coordinator.capabilities is controlled_camera.capabilities
    assert coordinator.temperature_c == 20.0
    assert coordinator.target_temperature_c == 0.0
    coordinator.target_temperature_c = -10.0
    coordinator.cooler_on = True
    assert coordinator.target_temperature_c == -10.0
    assert coordinator.cooler_on is True
    assert coordinator.tec_voltage() == (3.0, 6.0)
    assert coordinator.cooler_power_percent == 50.0


def test_disconnected_coordinator_rejects_cooling_access():
    coordinator = CameraCoordinator(lambda source: ControlledCamera(), "controlled")

    with pytest.raises(CameraNotConnectedError):
        _ = coordinator.temperature_c
