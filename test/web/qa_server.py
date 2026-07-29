"""Controlled local server used only for Task 11 browser QA."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
from waitress import serve

from test.web.conftest import WebCoordinator
from touptek4040.alpaca.app import create_app
from touptek4040.camera_contract import Frame, ReadoutMode, Roi
from touptek4040.camera_coordinator import CameraState
from touptek4040.errors import CameraError
from touptek4040.event_broker import EventBroker
from touptek4040.image_workspace import ImageWorkspace


class QaCoordinator(WebCoordinator):
    def __init__(self) -> None:
        super().__init__()
        self.qa_state = CameraState.IDLE
        self.qa_error = None
        self.qa_percent = None

    def snapshot(self):
        return SimpleNamespace(
            connected=self.connected,
            connecting=False,
            camera_state=self.qa_state,
            image_ready=self.image_generation is not None,
            percent_completed=self.qa_percent,
            error=self.qa_error,
        )

    def connect_async(self) -> None:
        super().connect_async()
        self.qa_state = CameraState.IDLE
        self.qa_error = None

    def disconnect_async(self) -> None:
        super().disconnect_async()
        self.preview_active = False

    def start_exposure(self, duration, light, *, settings) -> None:
        snapshot = settings(self.capabilities)
        self.exposure_calls.append((duration, light, snapshot))
        if duration == 0.13:
            self.connected = False
            self.qa_state = CameraState.ERROR
            self.qa_error = CameraError(
                "USB link lost; reconnect the camera before retrying."
            )
            self.qa_percent = None
            return
        if duration >= 2:
            self.qa_state = CameraState.EXPOSING
            self.qa_error = None
            self.qa_percent = 37
            return
        source = self.image_generation.frame
        dtype = (
            np.uint8
            if snapshot.readout_mode is ReadoutMode.RAW8
            else np.uint16
        )
        max_adu = 255 if dtype is np.uint8 else 4095
        axis = np.arange(1024, dtype=np.uint16)
        pixels = ((axis[:, None] + axis[None, :]) // 8) % (max_adu + 1)
        pixels = pixels.astype(dtype)
        pixels[430:438, 604:612] = max_adu
        frame = Frame(
            pixels=pixels,
            started_utc=source.started_utc,
            duration_s=duration,
            binning=snapshot.binning,
            roi=Roi(0, 0, 1024, 1024),
            readout_mode=snapshot.readout_mode,
            gain=snapshot.gain,
        )
        workspace = ImageWorkspace()
        workspace.publish(source)
        self.image_generation = workspace.publish(frame)
        self.qa_state = CameraState.IDLE
        self.qa_error = None
        self.qa_percent = None

    def abort_exposure(self) -> None:
        super().abort_exposure()
        self.qa_state = CameraState.IDLE
        self.qa_percent = None


def main() -> None:
    coordinator = QaCoordinator()
    coordinator.events = EventBroker(max_pending=4)
    axis = np.arange(1024, dtype=np.uint16)
    pixels = ((axis[:, None] + axis[None, :]) // 2).astype(np.uint16)
    pixels[430:438, 604:612] = 4095
    pixels[692:697, 280:285] = 3300
    pixels[228:232, 760:764] = 2800
    frame = Frame(
        pixels=pixels,
        started_utc=coordinator.image_generation.frame.started_utc,
        duration_s=1.25,
        binning=4,
        roi=Roi(0, 0, 1024, 1024),
        readout_mode=ReadoutMode.RAW12,
        gain=321,
    )
    coordinator.image_generation = ImageWorkspace().publish(frame)
    config = SimpleNamespace(
        camera=SimpleNamespace(
            serial="TP241025143218C92B01151AEC40CB7",
            readout_mode="RAW 12-bit",
            gain=100,
        ),
        web=SimpleNamespace(enabled=True),
    )
    serve(create_app(coordinator, config), host="127.0.0.1", port=18766, threads=4)


if __name__ == "__main__":
    main()
