from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import numpy as np

from touptek4040.camera_contract import CameraCapabilities, Frame, ReadoutMode, Roi
from touptek4040.camera_coordinator import CameraCoordinator, CameraState


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
        with self._condition:
            self.active -= 1
            self._condition.notify_all()
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
