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
from touptek4040.errors import (
    CameraBusyError,
    CameraError,
    CameraInvalidValueError,
    CameraNotConnectedError,
    CameraNotSupportedError,
)
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
        self.camera_state = CameraState.IDLE
        self.heater_supported = True
        self.heater_max_level = 5
        self._heater_level = 0
        self.heater_capability_reads = 0
        self.heater_level_reads = 0
        self.heater_writes: list[int] = []
        self.heater_read_error: Exception | None = None
        self.heater_write_error: Exception | None = None
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
            camera_state=self.camera_state,
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
    def heater_supported(self) -> bool:
        self.heater_capability_reads += 1
        return self._heater_supported

    @heater_supported.setter
    def heater_supported(self, value: bool) -> None:
        self._heater_supported = value

    @property
    def heater_max_level(self) -> int:
        self.heater_capability_reads += 1
        return self._heater_max_level

    @heater_max_level.setter
    def heater_max_level(self, value: int) -> None:
        self._heater_max_level = value

    @property
    def heater_level(self) -> int:
        self.heater_level_reads += 1
        if self.heater_read_error is not None:
            raise self.heater_read_error
        return self._heater_level

    def set_heater_level_if_idle(self, value: int) -> None:
        if not self.connected:
            raise CameraNotConnectedError("camera is not connected")
        if self.camera_state is not CameraState.IDLE:
            raise CameraBusyError("camera is exposing")
        if not self.heater_supported:
            raise CameraNotSupportedError("camera does not support a heater")
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= self.heater_max_level
        ):
            raise CameraInvalidValueError("heater level is outside the supported range")
        if self.heater_write_error is not None:
            error = self.heater_write_error
            if isinstance(error, CameraError):
                raise error
            raise CameraError(str(error))
        self._heater_level = value
        self.heater_writes.append(value)

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
