"""Deterministic, bounded-memory camera implementation for local use."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from .camera_contract import (
    AcquisitionControl,
    CameraCapabilities,
    CameraContract,
    Frame,
    ReadoutMode,
    Roi,
)
from .errors import (
    CameraInvalidValueError,
    CameraNotConnectedError,
    CameraOperationCancelledError,
)


class SimCamera(CameraContract):
    """A connected-by-default simulator matching the 4096 square camera."""

    _CAPABILITIES = CameraCapabilities(
        width=4096,
        height=4096,
        pixel_size_x=9.0,
        pixel_size_y=9.0,
        readout_modes=("RAW 12-bit", "RAW 8-bit"),
        max_bin=8,
        can_abort=True,
        can_stop=False,
    )

    def __init__(self, serial: str, *, heater_max_level: int = 4) -> None:
        if (
            isinstance(heater_max_level, bool)
            or not isinstance(heater_max_level, int)
            or heater_max_level < 0
        ):
            raise CameraInvalidValueError("heater_max_level must be a non-negative integer")
        self.serial = serial
        self._connected = True
        self._target_temperature_c = 0.0
        self._cooler_on = False
        self._gain = 100
        self._heater_max_level = heater_max_level
        self._heater_level = 0

    @property
    def capabilities(self) -> CameraCapabilities:
        return self._CAPABILITIES

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def acquire(
        self,
        duration_s: float,
        roi: Roi,
        binning: int,
        readout_mode: ReadoutMode,
        control: AcquisitionControl | None = None,
    ) -> Frame:
        self._require_connected()
        self._validate_acquisition(duration_s, roi, binning, readout_mode)
        if control is not None and not control.commit_start(lambda: None):
            raise CameraOperationCancelledError(
                "exposure was cancelled before acquisition started"
            )
        started_utc = datetime.now(timezone.utc)
        pixels = self._pixels(roi, binning, readout_mode)
        return Frame(
            pixels,
            started_utc,
            duration_s,
            binning,
            roi,
            readout_mode,
            self._gain,
        )

    def abort(self) -> None:
        self._require_connected()

    @property
    def gain(self) -> int:
        self._require_connected()
        return self._gain

    @gain.setter
    def gain(self, value: int) -> None:
        self._require_connected()
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 100 <= value <= 1650
        ):
            raise CameraInvalidValueError("gain must be an integer from 100 to 1650")
        self._gain = value

    @property
    def temperature_c(self) -> float:
        self._require_connected()
        return 20.0

    @property
    def target_temperature_c(self) -> float:
        self._require_connected()
        return self._target_temperature_c

    @target_temperature_c.setter
    def target_temperature_c(self, value: float) -> None:
        self._require_connected()
        self._target_temperature_c = float(value)

    @property
    def cooler_on(self) -> bool:
        self._require_connected()
        return self._cooler_on

    @cooler_on.setter
    def cooler_on(self, enabled: bool) -> None:
        self._require_connected()
        if not isinstance(enabled, bool):
            raise CameraInvalidValueError("cooler state must be boolean")
        self._cooler_on = enabled

    def tec_voltage(self) -> tuple[float, float]:
        self._require_connected()
        return (6.0 if self._cooler_on else 0.0, 6.0)

    @property
    def cooler_power_percent(self) -> float:
        current, maximum = self.tec_voltage()
        return current * 100.0 / maximum

    @property
    def heater_supported(self) -> bool:
        return True

    @property
    def heater_max_level(self) -> int:
        return self._heater_max_level

    @property
    def heater_level(self) -> int:
        self._require_connected()
        return self._heater_level

    @heater_level.setter
    def heater_level(self, value: int) -> None:
        self._require_connected()
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= self._heater_max_level
        ):
            raise CameraInvalidValueError(
                f"heater level must be an integer from 0 to {self._heater_max_level}"
            )
        self._heater_level = value

    def _require_connected(self) -> None:
        if not self._connected:
            raise CameraNotConnectedError("camera is not connected")

    def _validate_acquisition(
        self, duration_s: float, roi: Roi, binning: int, readout_mode: ReadoutMode
    ) -> None:
        if duration_s < 0:
            raise CameraInvalidValueError("duration_s must be non-negative")
        if binning < 1 or binning > self.capabilities.max_bin:
            raise CameraInvalidValueError("binning must be between 1 and 8")
        if not isinstance(readout_mode, ReadoutMode):
            raise CameraInvalidValueError("unsupported readout mode")
        if roi.width <= 0 or roi.height <= 0:
            raise CameraInvalidValueError("ROI dimensions must be positive")
        if roi.x < 0 or roi.y < 0:
            raise CameraInvalidValueError("ROI origin must be non-negative")
        if (
            (roi.x + roi.width) * binning > self.capabilities.width
            or (roi.y + roi.height) * binning > self.capabilities.height
        ):
            raise CameraInvalidValueError("ROI extends outside the sensor")

    @staticmethod
    def _pixels(roi: Roi, binning: int, readout_mode: ReadoutMode) -> np.ndarray:
        height = roi.height
        width = roi.width
        maximum = 4095 if readout_mode is ReadoutMode.RAW12 else 255
        dtype = np.uint16 if readout_mode is ReadoutMode.RAW12 else np.uint8
        pixels = np.empty((height, width), dtype=dtype)
        sensor_x = roi.x * binning
        sensor_y = roi.y * binning
        x_values = np.arange(
            sensor_x, sensor_x + width * binning, dtype=np.uint32
        ).reshape(width, binning)
        for row in range(height):
            y_value = sensor_y + row * binning
            block_sums = np.zeros(width, dtype=np.uint32)
            for y_offset in range(binning):
                block_sums += ((x_values + y_value + y_offset) % (maximum + 1)).sum(axis=1)
            pixels[row, :] = np.minimum(block_sums, maximum)
        return pixels
