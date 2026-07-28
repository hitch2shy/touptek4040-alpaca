from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import numpy as np
import pytest

from touptek4040.camera_contract import CameraCapabilities, Frame, ReadoutMode, Roi
from touptek4040.camera_coordinator import (
    CameraCoordinator,
    CameraSnapshot,
    CameraState,
)
from touptek4040.errors import (
    CameraBusyError,
    CameraCleanupError,
    CameraError,
    CameraNotConnectedError,
)


def wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        time.sleep(0.005)


class SequencedCamera:
    capabilities = CameraCapabilities(
        width=4,
        height=4,
        pixel_size_x=9.0,
        pixel_size_y=9.0,
        readout_modes=("RAW 12-bit", "RAW 8-bit"),
        max_bin=4,
        can_abort=True,
        can_stop=False,
    )

    def __init__(self) -> None:
        self.connected = False
        self._condition = threading.Condition()
        self._releases = threading.Semaphore(0)
        self.acquire_count = 0
        self.active = 0
        self.max_active = 0
        self.abort_count = 0
        self.abort_called = threading.Event()
        self.abort_error: Exception | None = None
        self.failure: Exception | None = None
        self.cleanup_release = threading.Event()
        self.cleanup_release.set()
        self.disconnect_started = threading.Event()
        self.disconnect_release = threading.Event()
        self.disconnect_release.set()

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.disconnect_started.set()
        self.disconnect_release.wait()
        self.connected = False

    def acquire(
        self, duration_s: float, roi: Roi, binning: int, readout_mode: ReadoutMode
    ) -> Frame:
        with self._condition:
            self.acquire_count += 1
            sequence = self.acquire_count
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self._condition.notify_all()
        self._releases.acquire()
        self.cleanup_release.wait()
        with self._condition:
            self.active -= 1
            self._condition.notify_all()
        if self.failure is not None:
            raise self.failure
        return Frame(
            pixels=np.full((roi.height, roi.width), sequence, dtype=np.uint16),
            started_utc=datetime.now(timezone.utc),
            duration_s=duration_s,
            binning=binning,
            roi=roi,
            readout_mode=readout_mode,
        )

    def abort(self) -> None:
        self.abort_count += 1
        self.abort_called.set()
        if self.abort_error is not None:
            if isinstance(self.abort_error, CameraNotConnectedError):
                self.connected = False
            raise self.abort_error

    def release(self) -> None:
        self._releases.release()

    def wait_for_count(self, count: int, timeout: float = 1.0) -> None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self.acquire_count < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"acquisition {count} did not start")
                self._condition.wait(remaining)


def connected_coordinator(camera):
    coordinator = CameraCoordinator(lambda source: camera, "controlled")
    coordinator.set_connected_sync(True)
    return coordinator


class GatedEventRecorder:
    def __init__(self, predicate) -> None:
        self._predicate = predicate
        self._lock = threading.Lock()
        self._claimed = False
        self.blocked = threading.Event()
        self.release = threading.Event()
        self.events: list[CameraSnapshot] = []

    def publish(self, snapshot: CameraSnapshot) -> None:
        with self._lock:
            should_block = not self._claimed and self._predicate(snapshot)
            if should_block:
                self._claimed = True
        if should_block:
            self.blocked.set()
            self.release.wait()
        with self._lock:
            self.events.append(snapshot)


class ObservableGate:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._condition = threading.Condition()
        self._attempts = 0

    def __enter__(self):
        with self._condition:
            self._attempts += 1
            self._condition.notify_all()
        self._lock.acquire()
        return self

    def __exit__(self, *args) -> None:
        self._lock.release()

    def wait_for_attempts(self, count: int, timeout: float = 1.0) -> None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._attempts < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"gate attempt {count} did not occur")
                self._condition.wait(remaining)


def test_formal_exposure_stops_preview_without_overlapping_camera_calls():
    camera = SequencedCamera()
    coordinator = connected_coordinator(camera)
    preview_frames: list[Frame] = []
    preview_received = threading.Event()

    def receive_preview(frame: Frame) -> None:
        preview_frames.append(frame)
        preview_received.set()

    coordinator.start_preview(receive_preview)
    camera.wait_for_count(1)
    camera.release()
    assert preview_received.wait(1.0)
    camera.wait_for_count(2)

    coordinator.start_exposure(1.0, True)
    assert coordinator.snapshot().camera_state == CameraState.EXPOSING
    assert camera.abort_count >= 1
    camera.release()
    camera.wait_for_count(3)

    assert camera.max_active == 1
    assert len(preview_frames) == 1
    assert coordinator.preview_frame is preview_frames[0]
    assert coordinator.frame is None
    camera.release()
    wait_until(lambda: coordinator.snapshot().image_ready)
    assert int(coordinator.frame.pixels[0, 0]) == 3
    assert coordinator.preview_frame is preview_frames[0]


def test_disconnect_invalidates_in_flight_frame_before_close_finishes():
    camera = SequencedCamera()
    coordinator = connected_coordinator(camera)
    camera.disconnect_release.clear()
    coordinator.start_exposure(1.0, True)
    camera.wait_for_count(1)

    coordinator.disconnect_async()
    assert coordinator.snapshot().connecting is True
    assert camera.disconnect_started.wait(1.0)
    camera.release()
    wait_until(lambda: camera.active == 0)

    assert coordinator.snapshot().image_ready is False
    assert coordinator.frame is None
    camera.disconnect_release.set()
    wait_until(lambda: coordinator.snapshot().connecting is False)
    assert coordinator.snapshot().connected is False


def test_abort_and_disconnect_race_does_not_deadlock_or_publish_late_frame():
    camera = SequencedCamera()
    coordinator = connected_coordinator(camera)
    coordinator.start_exposure(1.0, True)
    camera.wait_for_count(1)

    abort_thread = threading.Thread(target=coordinator.abort_exposure)
    disconnect_thread = threading.Thread(target=coordinator.disconnect_async)
    abort_thread.start()
    disconnect_thread.start()
    abort_thread.join(1.0)
    disconnect_thread.join(1.0)

    assert not abort_thread.is_alive()
    assert not disconnect_thread.is_alive()
    camera.release()
    wait_until(lambda: coordinator.snapshot().connecting is False)
    assert coordinator.snapshot().connected is False
    assert coordinator.snapshot().image_ready is False
    assert coordinator.frame is None


def test_completion_event_cannot_arrive_after_next_exposure_event():
    camera = SequencedCamera()
    coordinator = connected_coordinator(camera)
    recorder = GatedEventRecorder(
        lambda event: event.camera_state == CameraState.IDLE and event.image_ready
    )
    coordinator.events = recorder
    coordinator.start_exposure(1.0, True)
    camera.wait_for_count(1)
    camera.release()
    assert recorder.blocked.wait(1.0)

    restarted = threading.Event()

    def restart() -> None:
        coordinator.start_exposure(1.0, True)
        restarted.set()

    thread = threading.Thread(target=restart)
    thread.start()
    restarted.wait(0.1)
    recorder.release.set()
    thread.join(1.0)
    wait_until(
        lambda: any(
            event.camera_state == CameraState.IDLE and event.image_ready
            for event in recorder.events
        )
    )
    camera.release()

    completion_index = next(
        index
        for index, event in enumerate(recorder.events)
        if event.camera_state == CameraState.IDLE and event.image_ready
    )
    exposure_indices = [
        index
        for index, event in enumerate(recorder.events)
        if event.camera_state == CameraState.EXPOSING
    ]
    assert completion_index < exposure_indices[-1]


def test_exposing_event_cannot_arrive_after_abort_event():
    camera = SequencedCamera()
    coordinator = connected_coordinator(camera)
    recorder = GatedEventRecorder(
        lambda event: event.camera_state == CameraState.EXPOSING
    )
    coordinator.events = recorder
    start_thread = threading.Thread(
        target=coordinator.start_exposure, args=(1.0, True)
    )
    start_thread.start()
    assert recorder.blocked.wait(1.0)

    abort_thread = threading.Thread(target=coordinator.abort_exposure)
    abort_thread.start()
    camera.abort_called.wait(0.1)
    recorder.release.set()
    start_thread.join(1.0)
    abort_thread.join(1.0)
    camera.release()

    states = [event.camera_state for event in recorder.events]
    assert states.index(CameraState.EXPOSING) < states.index(CameraState.IDLE)


def test_error_event_cannot_arrive_after_disconnect_and_reconnect_events():
    camera = SequencedCamera()
    coordinator = connected_coordinator(camera)
    camera.abort_error = None
    recorder = GatedEventRecorder(
        lambda event: event.camera_state == CameraState.ERROR
    )
    coordinator.events = recorder
    camera.failure = CameraError("frame failed")
    coordinator.start_exposure(1.0, True)
    camera.wait_for_count(1)
    camera.release()
    assert recorder.blocked.wait(1.0)

    reconnected = threading.Event()

    def reconnect() -> None:
        coordinator.set_connected_sync(False)
        coordinator.set_connected_sync(True)
        reconnected.set()

    thread = threading.Thread(target=reconnect)
    thread.start()
    reconnected.wait(0.1)
    recorder.release.set()
    thread.join(1.0)

    error_index = next(
        index
        for index, event in enumerate(recorder.events)
        if event.camera_state == CameraState.ERROR
    )
    later_connection_events = [
        index
        for index, event in enumerate(recorder.events)
        if event.connecting
    ]
    assert all(error_index < index for index in later_connection_events)


def test_abort_then_new_exposure_waits_for_old_worker_cleanup():
    camera = SequencedCamera()
    coordinator = connected_coordinator(camera)
    gate = ObservableGate()
    coordinator._acquisition_gate = gate
    camera.cleanup_release.clear()
    coordinator.start_exposure(1.0, True)
    gate.wait_for_attempts(1)
    camera.wait_for_count(1)

    coordinator.abort_exposure()
    started = time.monotonic()
    coordinator.start_exposure(1.0, True)
    assert time.monotonic() - started < 0.1
    assert coordinator.snapshot().camera_state == CameraState.EXPOSING
    gate.wait_for_attempts(2)
    camera.release()
    assert camera.acquire_count == 1

    camera.cleanup_release.set()
    camera.wait_for_count(2)
    camera.release()
    assert camera.max_active == 1


def test_abort_then_preview_waits_for_old_formal_worker_cleanup():
    camera = SequencedCamera()
    coordinator = connected_coordinator(camera)
    gate = ObservableGate()
    coordinator._acquisition_gate = gate
    camera.cleanup_release.clear()
    coordinator.start_exposure(1.0, True)
    gate.wait_for_attempts(1)
    camera.wait_for_count(1)

    coordinator.abort_exposure()
    coordinator.start_preview(lambda frame: None)
    gate.wait_for_attempts(2)
    camera.release()
    assert camera.acquire_count == 1

    camera.cleanup_release.set()
    camera.wait_for_count(2)
    stop_thread = threading.Thread(target=coordinator.stop_preview)
    stop_thread.start()
    assert camera.abort_called.wait(1.0)
    camera.release()
    stop_thread.join(1.0)
    assert camera.max_active == 1


@pytest.mark.parametrize(
    "abort_error",
    [
        CameraBusyError("abort busy"),
        CameraNotConnectedError("camera disappeared"),
        CameraCleanupError(
            "multiple cleanup failures",
            (CameraBusyError("trigger busy"), CameraError("stop failed")),
        ),
    ],
)
def test_stop_preview_waits_for_worker_and_preserves_abort_error(abort_error):
    camera = SequencedCamera()
    coordinator = connected_coordinator(camera)
    coordinator.start_preview(lambda frame: None)
    camera.wait_for_count(1)
    camera.abort_error = abort_error
    result: list[Exception] = []
    returned = threading.Event()

    def stop() -> None:
        try:
            coordinator.stop_preview()
        except Exception as error:
            result.append(error)
        finally:
            returned.set()

    thread = threading.Thread(target=stop)
    thread.start()
    assert camera.abort_called.wait(1.0)
    returned_early = returned.wait(0.05)
    camera.release()
    thread.join(1.0)

    snapshot = coordinator.snapshot()
    assert returned_early is False
    assert result == [abort_error]
    assert snapshot.camera_state == CameraState.ERROR
    assert snapshot.error is abort_error
    assert snapshot.connected is not isinstance(abort_error, CameraNotConnectedError)
    assert camera.active == 0


def test_percent_stays_zero_until_slow_preview_callback_finishes():
    camera = SequencedCamera()
    coordinator = connected_coordinator(camera)
    callback_started = threading.Event()
    callback_release = threading.Event()

    def slow_callback(frame: Frame) -> None:
        callback_started.set()
        callback_release.wait()

    coordinator.start_preview(slow_callback)
    camera.wait_for_count(1)
    camera.release()
    assert callback_started.wait(1.0)

    started = time.monotonic()
    coordinator.start_exposure(0.012, True)
    assert time.monotonic() - started < 0.1
    time.sleep(0.03)
    assert camera.acquire_count == 1
    assert coordinator.snapshot().percent_completed == 0

    callback_release.set()
    camera.wait_for_count(2)
    camera.release()


def test_percent_stays_zero_while_preview_cancel_is_delayed():
    camera = SequencedCamera()
    coordinator = connected_coordinator(camera)
    coordinator.start_preview(lambda frame: None)
    camera.wait_for_count(1)

    coordinator.start_exposure(0.012, True)
    assert camera.abort_called.wait(1.0)
    time.sleep(0.03)
    assert camera.acquire_count == 1
    assert coordinator.snapshot().percent_completed == 0

    camera.release()
    camera.wait_for_count(2)
    camera.release()
