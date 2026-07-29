"""Controlled local server used only for Task 11 browser QA."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
from waitress import serve

from test.web.conftest import WebCoordinator
from touptek4040.alpaca.app import create_app
from touptek4040.camera_contract import Frame, ReadoutMode, Roi
from touptek4040.event_broker import EventBroker
from touptek4040.image_workspace import ImageWorkspace


def main() -> None:
    coordinator = WebCoordinator()
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
