from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import falcon.testing
import numpy as np
import pytest

from touptek4040.camera_contract import (
    CameraCapabilities,
    Frame,
    ReadoutMode,
    Roi,
)
from touptek4040.camera_coordinator import CameraState
from touptek4040.image_workspace import ImageWorkspace


class WebCoordinator:
    def __init__(self) -> None:
        self.connected = True
        self.connect_calls: list[bool] = []
        self.preview_calls: list[str] = []
        self.exposure_calls: list[tuple[float, bool, object]] = []
        self.abort_calls = 0
        self.settings_validations = 0
        self.preview_active = False
        self.target_temperature_c = -10.0
        self.cooler_on = False
        self.events = SimpleNamespace(subscribe=lambda: None)
        pixels = np.arange(256, dtype=np.uint16).reshape(16, 16)
        pixels[8, 8] = 4095
        frame = Frame(
            pixels=pixels,
            started_utc=datetime(2026, 7, 28, 12, 34, 56, tzinfo=timezone.utc),
            duration_s=1.25,
            binning=1,
            roi=Roi(0, 0, 16, 16),
            readout_mode=ReadoutMode.RAW12,
            gain=321,
        )
        self.image_generation = ImageWorkspace().publish(frame)
        self.preview_generation = None
        self.capabilities = CameraCapabilities(
            width=4096,
            height=4096,
            pixel_size_x=9.0,
            pixel_size_y=9.0,
            readout_modes=("RAW 12-bit", "RAW 8-bit"),
            max_bin=8,
            can_abort=True,
            can_stop=False,
        )

    def snapshot(self):
        return SimpleNamespace(
            connected=self.connected,
            connecting=False,
            camera_state=CameraState.IDLE,
            image_ready=True,
            percent_completed=None,
            error=None,
        )

    def connect_async(self) -> None:
        self.connect_calls.append(True)
        self.connected = True

    def disconnect_async(self) -> None:
        self.connect_calls.append(False)
        self.connected = False

    def start_preview(self, callback) -> None:
        del callback
        self.preview_calls.append("start")
        self.preview_active = True

    def stop_preview(self) -> None:
        self.preview_calls.append("stop")
        self.preview_active = False

    def start_exposure(self, duration, light, *, settings) -> None:
        self.exposure_calls.append((duration, light, settings(self.capabilities)))

    def abort_exposure(self) -> None:
        self.abort_calls += 1

    def update_settings_if_idle(self, operation):
        self.settings_validations += 1
        return operation(self.capabilities)

    def set_target_temperature_if_idle(self, value: float) -> None:
        self.target_temperature_c = value

    def set_cooler_on_if_idle(self, value: bool) -> None:
        self.cooler_on = value

    @property
    def temperature_c(self) -> float:
        return -4.5

    @property
    def cooler_power_percent(self) -> float:
        return 50.0


@pytest.fixture
def web_coordinator() -> WebCoordinator:
    return WebCoordinator()


@pytest.fixture
def web_config():
    return SimpleNamespace(
        camera=SimpleNamespace(
            serial="TP241025143218C92B01151AEC40CB7",
            readout_mode="RAW 12-bit",
            gain=100,
        ),
        web=SimpleNamespace(enabled=True),
    )


@pytest.fixture
def web_client(web_coordinator, web_config):
    from touptek4040.alpaca.app import create_app

    return falcon.testing.TestClient(create_app(web_coordinator, web_config))


@pytest.fixture
def mutation_headers():
    return {
        "Origin": "http://falconframework.org",
        "Content-Type": "application/json",
        "X-ToupTek-Console": "1",
    }
