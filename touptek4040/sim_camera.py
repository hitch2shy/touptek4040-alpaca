"""Deterministic, bounded-memory camera implementation for local use."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from .camera_contract import CameraCapabilities, CameraContract, Frame, ReadoutMode, Roi
from .errors import CameraInvalidValueError, CameraNotConnectedError


class SimCamera(CameraContract):
    """A connected-by-default simulator matching the 4096 square camera."""

    _CAPABILITIES = CameraCapabilities(
        width=4096,
        height=4096,
        readout_modes=("RAW 12-bit", "RAW 8-bit"),
        max_bin=8,
        can_abort=True,
        can_stop=False,
    )

    def __init__(self, serial: str) -> None:
        self.serial = serial
        self._connected = True

    @property
    def capabilities(self) -> CameraCapabilities:
        return self._CAPABILITIES

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def acquire(
        self, duration_s: float, roi: Roi, binning: int, readout_mode: ReadoutMode
    ) -> Frame:
        self._require_connected()
        self._validate_acquisition(duration_s, roi, binning, readout_mode)
        started_utc = datetime.now(timezone.utc)
        pixels = self._pixels(roi, binning, readout_mode)
        return Frame(pixels, started_utc, duration_s, binning, roi, readout_mode)

    def abort(self) -> None:
        self._require_connected()

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
        if roi.x + roi.width > self.capabilities.width or roi.y + roi.height > self.capabilities.height:
            raise CameraInvalidValueError("ROI extends outside the sensor")
        if roi.width % binning or roi.height % binning:
            raise CameraInvalidValueError("ROI dimensions must be divisible by binning")

    @staticmethod
    def _pixels(roi: Roi, binning: int, readout_mode: ReadoutMode) -> np.ndarray:
        height = roi.height // binning
        width = roi.width // binning
        maximum = 4095 if readout_mode is ReadoutMode.RAW12 else 255
        dtype = np.uint16 if readout_mode is ReadoutMode.RAW12 else np.uint8
        pixels = np.empty((height, width), dtype=dtype)
        x_values = np.arange(roi.x, roi.x + roi.width, binning, dtype=np.uint32)
        for row, y_value in enumerate(range(roi.y, roi.y + roi.height, binning)):
            pixels[row, :] = (x_values + y_value) % (maximum + 1)
        return pixels
