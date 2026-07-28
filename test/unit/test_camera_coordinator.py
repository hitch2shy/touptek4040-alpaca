from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import numpy as np
import pytest

import touptek4040.camera_coordinator as camera_coordinator_module
from touptek4040.camera_contract import (
    CameraCapabilities,
    ExposureSettings,
    Frame,
    ReadoutMode,
    Roi,
)
from touptek4040.camera_coordinator import CameraCoordinator, CameraState
from touptek4040.errors import (
    CameraBusyError,
    CameraCleanupError,
    CameraError,
    CameraInvalidValueError,
    CameraNotConnectedError,
    CameraOperationCancelledError,
)


class ThrowingTextCleanupError(CameraError):
    def __str__(self):
        raise RuntimeError("secondary cleanup string failure")


class NonStringTextCleanupError(CameraError):
    def __str__(self):
        return object()


class LongMultilineCleanupError(CameraError):
    def __str__(self):
        return "close failed\nSECRET\n" + "B" * 4000


class ThrowingTextCleanupBaseError(BaseException):
    def __str__(self):
        raise RuntimeError("secondary cleanup base failure")


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
        self.disconnect_count = 0
        self._releases = threading.Semaphore(0)
        self._acquisition_condition = threading.Condition()
        self.acquire_count = 0
        self.active_acquisitions = 0
        self.max_active_acquisitions = 0
        self.abort_count = 0
        self.abort_error: Exception | None = None
        self.disconnect_error: Exception | None = None
        self.failure: Exception | None = None
        self._gain = 100
        self.gain_error: Exception | None = None
        self.block_gain = False
        self.gain_started = threading.Event()
        self.gain_release = threading.Event()
        self.gain_release.set()
        self.block_acquisition_claim = False
        self.acquisition_claim_entered = threading.Event()
        self.acquisition_claim_release = threading.Event()
        self.acquisition_claim_release.set()
        self.operation_order: list[tuple[object, ...]] = []
        self.requested_acquisitions: list[
            tuple[float, Roi, int, ReadoutMode, int]
        ] = []
        self.requested_durations: list[float] = []

    def connect(self) -> None:
        self.connect_started.set()
        self.connect_release.wait()
        self.connected = True

    def disconnect(self) -> None:
        self.disconnect_count += 1
        self.disconnect_started.set()
        self.disconnect_release.wait()
        self.connected = False
        if self.disconnect_error is not None:
            raise self.disconnect_error

    def acquire(
        self,
        duration_s: float,
        roi: Roi,
        binning: int,
        readout_mode: ReadoutMode,
        control=None,
    ) -> Frame:
        self.acquisition_claim_entered.set()
        if self.block_acquisition_claim:
            self.acquisition_claim_release.wait(2.0)

        def claim() -> None:
            with self._acquisition_condition:
                self.operation_order.append(
                    ("acquire", duration_s, roi, binning, readout_mode, self._gain)
                )
                self.requested_acquisitions.append(
                    (duration_s, roi, binning, readout_mode, self._gain)
                )
                self.requested_durations.append(duration_s)
                self.acquire_count += 1
                self.active_acquisitions += 1
                self.max_active_acquisitions = max(
                    self.max_active_acquisitions, self.active_acquisitions
                )
                self._acquisition_condition.notify_all()

        if control is not None:
            if not control.commit_start(claim):
                raise CameraOperationCancelledError(
                    "exposure was cancelled before acquisition started"
                )
        else:
            claim()
        with self._acquisition_condition:
            sequence = self.acquire_count
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
            gain=self._gain,
        )

    @property
    def gain(self) -> int:
        return self._gain

    @gain.setter
    def gain(self, value: int) -> None:
        self.operation_order.append(("gain", value))
        self.gain_started.set()
        if self.block_gain:
            self.gain_release.wait(2.0)
        if self.gain_error is not None:
            raise self.gain_error
        self._gain = value

    def abort(self) -> None:
        self.abort_count += 1
        if self.abort_error is not None:
            if isinstance(self.abort_error, CameraNotConnectedError):
                self.connected = False
            raise self.abort_error

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


def test_exposure_worker_applies_gain_then_acquires_with_frozen_settings(
    coordinator, controlled_camera
):
    settings = ExposureSettings(
        roi=Roi(1, 1, 3, 2),
        binning=2,
        readout_mode=ReadoutMode.RAW8,
        gain=1650,
    )

    coordinator.start_exposure(
        0.25,
        False,
        settings=lambda capabilities: settings,
    )
    controlled_camera.wait_for_acquisitions(1)

    assert controlled_camera.operation_order[:2] == [
        ("gain", 1650),
        ("acquire", 0.25, Roi(1, 1, 3, 2), 2, ReadoutMode.RAW8, 1650),
    ]
    controlled_camera.release_frame()
    wait_until(lambda: coordinator.snapshot().image_ready)
    assert coordinator.frame is not None
    assert coordinator.frame.gain == 1650


def test_abort_while_gain_is_being_applied_never_starts_acquisition(
    coordinator, controlled_camera
):
    controlled_camera.block_gain = True
    controlled_camera.gain_release.clear()
    settings = ExposureSettings(
        roi=Roi(0, 0, 4, 3),
        binning=1,
        readout_mode=ReadoutMode.RAW12,
        gain=200,
    )
    coordinator.start_exposure(
        1.0,
        True,
        settings=lambda capabilities: settings,
    )
    assert controlled_camera.gain_started.wait(1.0)

    coordinator.abort_exposure()
    controlled_camera.gain_release.set()
    thread = coordinator._exposure_thread
    assert thread is not None
    thread.join(1.0)

    assert controlled_camera.acquire_count == 0
    assert coordinator.snapshot().camera_state is CameraState.IDLE
    assert coordinator.frame is None


def test_percent_stays_zero_while_gain_is_being_applied(
    coordinator, controlled_camera
):
    controlled_camera.block_gain = True
    controlled_camera.gain_release.clear()

    started = time.monotonic()
    coordinator.start_exposure(0.012, True)

    assert time.monotonic() - started < 0.1
    assert controlled_camera.gain_started.wait(1.0)
    time.sleep(0.03)
    snapshot = coordinator.snapshot()
    assert snapshot.camera_state is CameraState.EXPOSING
    assert snapshot.percent_completed == 0
    assert controlled_camera.acquire_count == 0

    coordinator.abort_exposure()
    controlled_camera.gain_release.set()


def test_abort_linearizes_before_backend_acquisition_claim(
    coordinator, controlled_camera
):
    controlled_camera.block_acquisition_claim = True
    controlled_camera.acquisition_claim_release.clear()
    coordinator.start_exposure(1.0, True)
    assert controlled_camera.acquisition_claim_entered.wait(1.0)

    coordinator.abort_exposure()
    controlled_camera.acquisition_claim_release.set()
    thread = coordinator._exposure_thread
    assert thread is not None
    thread.join(1.0)

    assert controlled_camera.acquire_count == 0
    assert coordinator.snapshot().camera_state is CameraState.IDLE
    assert coordinator.frame is None


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
    assert "summary=unexpected camera backend failure" in str(snapshot.error)
    assert "sensor transfer failed" not in str(snapshot.error)


@pytest.mark.parametrize("failure_stage", ["gain", "acquire"])
def test_not_connected_exposure_failure_cleans_old_owner_before_new_factory(
    failure_stage,
):
    old_camera = ControlledCamera()
    new_camera = ControlledCamera()
    cameras = iter((old_camera, new_camera))
    factory_calls = []
    coordinator = CameraCoordinator(
        lambda source: factory_calls.append(source) or next(cameras),
        "controlled",
    )
    coordinator.set_connected_sync(True)
    old_camera.disconnect_release.clear()
    failure = CameraNotConnectedError(f"{failure_stage} lost camera")
    if failure_stage == "gain":
        old_camera.gain_error = failure
    else:
        old_camera.failure = failure

    coordinator.start_exposure(0.1, True)
    if failure_stage == "acquire":
        old_camera.wait_for_acquisitions(1)
        old_camera.release_frame()
    wait_until(lambda: coordinator.snapshot().camera_state is CameraState.ERROR)

    coordinator.connect_async()
    try:
        assert old_camera.disconnect_started.wait(1.0)
        assert new_camera.connect_started.is_set() is False
        assert factory_calls == ["controlled"]

        old_camera.disconnect_release.set()
        wait_until(lambda: coordinator.snapshot().connected)
        assert new_camera.connect_started.is_set()
        assert factory_calls == ["controlled", "controlled"]
        assert old_camera.connected is False
    finally:
        old_camera.disconnect_release.set()
        coordinator.disconnect_async()


def test_stale_owner_cleanup_failure_prevents_new_factory_and_is_diagnostic():
    old_camera = ControlledCamera()
    new_camera = ControlledCamera()
    cameras = iter((old_camera, new_camera))
    factory_calls = []
    coordinator = CameraCoordinator(
        lambda source: factory_calls.append(source) or next(cameras),
        "controlled",
    )
    coordinator.set_connected_sync(True)
    old_camera.gain_error = CameraNotConnectedError("gain lost camera")
    old_camera.disconnect_error = CameraError("old owner close failed")
    old_camera.disconnect_release.clear()

    coordinator.start_exposure(0.1, True)
    wait_until(lambda: coordinator.snapshot().camera_state is CameraState.ERROR)
    coordinator.connect_async()
    assert old_camera.disconnect_started.wait(1.0)
    old_camera.disconnect_release.set()
    wait_until(lambda: coordinator.snapshot().connecting is False)

    snapshot = coordinator.snapshot()
    assert snapshot.connected is False
    assert snapshot.camera_state is CameraState.ERROR
    assert snapshot.error is not None
    assert "old owner close failed" in str(snapshot.error)
    assert new_camera.connect_started.is_set() is False
    assert factory_calls == ["controlled"]


def test_failed_owner_stays_quarantined_across_retries_until_cleanup_succeeds():
    old_camera = ControlledCamera()
    new_camera = ControlledCamera()
    cameras = iter((old_camera, new_camera))
    factory_calls = []
    coordinator = CameraCoordinator(
        lambda source: factory_calls.append(source) or next(cameras),
        "controlled",
    )
    coordinator.set_connected_sync(True)
    old_camera.gain_error = CameraNotConnectedError("gain lost camera")
    cleanup_error = CameraError("persistent close failure")
    old_camera.disconnect_error = cleanup_error

    coordinator.start_exposure(0.1, True)
    wait_until(lambda: coordinator.snapshot().camera_state is CameraState.ERROR)

    old_camera.disconnect_release.clear()
    coordinator.connect_async()
    assert old_camera.disconnect_started.wait(1.0)
    coordinator.connect_async()
    coordinator.connect_async()
    assert old_camera.disconnect_count == 1
    assert factory_calls == ["controlled"]
    old_camera.disconnect_release.set()
    wait_until(lambda: coordinator.snapshot().connecting is False)

    for expected_attempts in (1, 2, 3):
        if expected_attempts > 1:
            with pytest.raises(CameraError, match="persistent close failure"):
                coordinator.set_connected_sync(True)
        snapshot = coordinator.snapshot()
        assert snapshot.connected is False
        assert snapshot.camera_state is CameraState.ERROR
        assert snapshot.error is not None
        assert "persistent close failure" in str(snapshot.error)
        assert str(snapshot.error).count("persistent close failure") == 1
        assert old_camera.disconnect_count == expected_attempts
        assert new_camera.connect_started.is_set() is False
        assert factory_calls == ["controlled"]

    old_camera.disconnect_error = None
    coordinator.set_connected_sync(True)

    assert old_camera.disconnect_count == 4
    assert coordinator.snapshot().connected is True
    assert new_camera.connect_started.is_set()
    assert factory_calls == ["controlled", "controlled"]


@pytest.mark.parametrize(
    "cleanup_error",
    [
        ThrowingTextCleanupError(),
        NonStringTextCleanupError(),
        LongMultilineCleanupError(),
        ThrowingTextCleanupBaseError(),
    ],
    ids=("throwing-str", "non-string-str", "long-multiline", "base-exception"),
)
def test_pathological_cleanup_error_keeps_owner_quarantined_and_retryable(
    cleanup_error,
):
    old_camera = ControlledCamera()
    new_camera = ControlledCamera()
    cameras = iter((old_camera, new_camera))
    factory_calls = []
    coordinator = CameraCoordinator(
        lambda source: factory_calls.append(source) or next(cameras),
        "controlled",
    )
    coordinator.set_connected_sync(True)
    old_camera.gain_error = CameraNotConnectedError("gain lost camera")
    old_camera.disconnect_error = cleanup_error

    coordinator.start_exposure(0.1, True)
    wait_until(lambda: coordinator.snapshot().camera_state is CameraState.ERROR)
    coordinator.connect_async()
    wait_until(lambda: coordinator.snapshot().connecting is False)

    snapshot = coordinator.snapshot()
    assert snapshot.connected is False
    assert snapshot.camera_state is CameraState.ERROR
    assert snapshot.error is not None
    message = str(snapshot.error)
    assert len(message) <= 700
    assert "\n" not in message
    assert "SECRET" not in message
    assert "secondary" not in message
    assert factory_calls == ["controlled"]

    old_camera.disconnect_error = None
    coordinator.set_connected_sync(True)

    assert coordinator.snapshot().connected is True
    assert factory_calls == ["controlled", "controlled"]


def test_diagnostic_builder_failure_is_atomically_replaced_by_safe_fallback(
    coordinator,
    controlled_camera,
    monkeypatch,
):
    def fail_diagnostic(*args, **kwargs):
        del args, kwargs
        raise ThrowingTextCleanupBaseError()

    monkeypatch.setattr(
        camera_coordinator_module,
        "_async_exposure_error",
        fail_diagnostic,
    )
    controlled_camera.failure = CameraError("sensor failed")
    coordinator.start_exposure(0.1, True)
    controlled_camera.wait_for_acquisitions(1)
    controlled_camera.release_frame()
    wait_until(lambda: coordinator.snapshot().camera_state is CameraState.ERROR)
    thread = coordinator._exposure_thread
    assert thread is not None
    thread.join(1.0)

    snapshot = coordinator.snapshot()
    assert thread.is_alive() is False
    assert snapshot.error is not None
    message = str(snapshot.error)
    assert message.startswith("AsyncExposureFailure;")
    assert "CameraState=Error(5)" in message
    assert "summary=unexpected camera backend failure" in message
    assert "recovery=Disconnect, reconnect the camera, then retry." in message
    assert len(message) <= 700
    assert "\n" not in message


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


@pytest.mark.parametrize(
    ("requested", "applied"),
    [
        (0.000012, 0.000012),
        (0.0000124, 0.000012),
        (3_600.0, 3_600.0),
    ],
)
def test_exposure_duration_is_quantized_and_accepts_inclusive_microsecond_bounds(
    coordinator, controlled_camera, requested, applied
):
    coordinator.start_exposure(requested, True)
    controlled_camera.wait_for_acquisitions(1)

    assert controlled_camera.requested_durations == [applied]
    coordinator.abort_exposure()
    controlled_camera.release_frame()


@pytest.mark.parametrize(
    "duration_s",
    [0.0000004, 0.000011, 3_600.000001, float("nan"), float("inf")],
)
def test_invalid_quantized_duration_is_rejected_before_state_changes(
    coordinator, controlled_camera, duration_s
):
    before = coordinator.snapshot()

    with pytest.raises(CameraInvalidValueError):
        coordinator.start_exposure(duration_s, True)

    assert coordinator.snapshot() == before
    assert controlled_camera.acquire_count == 0


def test_superseded_connect_cleanup_failure_is_reported_with_all_causes():
    first = CameraBusyError("close still busy")
    second = CameraNotConnectedError("owner vanished during close")
    cleanup = CameraCleanupError("old owner cleanup failed", (first, second))
    camera = ControlledCamera()
    camera.connect_release.clear()
    camera.disconnect_error = cleanup
    coordinator = CameraCoordinator(lambda source: camera, "controlled")

    coordinator.connect_async()
    assert camera.connect_started.wait(1.0)
    coordinator.disconnect_async()
    camera.connect_release.set()
    wait_until(lambda: coordinator.snapshot().connecting is False)

    snapshot = coordinator.snapshot()
    assert snapshot.connected is False
    assert snapshot.camera_state == CameraState.ERROR
    assert isinstance(snapshot.error, CameraCleanupError)
    assert tuple(type(error) for error in snapshot.error.errors) == (
        CameraBusyError,
        CameraNotConnectedError,
    )
    assert tuple(str(error) for error in snapshot.error.errors) == (
        "close still busy",
        "owner vanished during close",
    )


def test_superseded_disconnect_cleanup_failure_prevents_new_owner_success():
    first = CameraBusyError("abort cleanup failed")
    second = CameraError("close cleanup failed")
    cleanup = CameraCleanupError("old owner cleanup failed", (first, second))
    old_camera = ControlledCamera()
    new_camera = ControlledCamera()
    cameras = iter((old_camera, new_camera))
    coordinator = CameraCoordinator(lambda source: next(cameras), "controlled")
    coordinator.set_connected_sync(True)
    old_camera.disconnect_release.clear()
    old_camera.disconnect_error = cleanup

    coordinator.disconnect_async()
    assert old_camera.disconnect_started.wait(1.0)
    coordinator.connect_async()
    old_camera.disconnect_release.set()
    wait_until(lambda: coordinator.snapshot().connecting is False)

    snapshot = coordinator.snapshot()
    assert snapshot.connected is False
    assert snapshot.camera_state == CameraState.ERROR
    assert isinstance(snapshot.error, CameraCleanupError)
    assert tuple(type(error) for error in snapshot.error.errors) == (
        CameraBusyError,
        CameraError,
    )
    assert tuple(str(error) for error in snapshot.error.errors) == (
        "abort cleanup failed",
        "close cleanup failed",
    )
    assert new_camera.connect_started.is_set() is False
