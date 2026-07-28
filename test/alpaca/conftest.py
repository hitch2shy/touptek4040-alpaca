from __future__ import annotations

from datetime import datetime, timezone
import threading
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
from touptek4040.camera_coordinator import CameraCoordinator


class ApiCamera:
    def __init__(self) -> None:
        self.connected = False
        self.target_temperature_c = -10.0
        self.cooler_on = False
        self.abort_calls = 0
        self.acquisition_started = threading.Event()
        self.release_acquisition = threading.Event()
        self.release_acquisition.set()
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

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def acquire(
        self,
        duration_s: float,
        roi: Roi,
        binning: int,
        readout_mode: ReadoutMode,
    ) -> Frame:
        self.acquisition_started.set()
        self.release_acquisition.wait(timeout=2)
        return Frame(
            np.zeros((1, 1), dtype=np.uint16),
            datetime(2026, 7, 28, 12, 34, 56, tzinfo=timezone.utc),
            duration_s,
            binning,
            roi,
            readout_mode,
        )

    def abort(self) -> None:
        self.abort_calls += 1
        self.release_acquisition.set()

    @property
    def temperature_c(self) -> float:
        return -4.5

    def tec_voltage(self) -> tuple[float, float]:
        return (3.0, 6.0)

    @property
    def cooler_power_percent(self) -> float:
        return 50.0


@pytest.fixture
def api_camera() -> ApiCamera:
    return ApiCamera()


@pytest.fixture
def coordinator(api_camera: ApiCamera) -> CameraCoordinator:
    return CameraCoordinator(lambda _source: api_camera, "test")


@pytest.fixture
def config():
    return SimpleNamespace(
        camera=SimpleNamespace(
            serial="TP241025143218C92B01151AEC40CB7",
            readout_mode="RAW 12-bit",
            gain=100,
        )
    )


@pytest.fixture
def disconnected_client(coordinator, config):
    from touptek4040.alpaca.app import create_app

    return falcon.testing.TestClient(create_app(coordinator, config))


@pytest.fixture
def client(coordinator, config):
    from touptek4040.alpaca.app import create_app

    coordinator.set_connected_sync(True)
    return falcon.testing.TestClient(create_app(coordinator, config))


def put(client, command: str, **fields: object):
    return client.simulate_put(
        f"/api/v1/camera/0/{command}",
        json=fields,
    )
