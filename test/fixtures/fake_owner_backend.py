"""Spawn-safe fake backend used by camera-owner process tests."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from threading import Event
import time

import numpy as np

from touptek4040.camera_contract import CameraCapabilities, Frame, ReadoutMode, Roi
from touptek4040.errors import CameraInvalidValueError, CameraNotConnectedError


class FakeOwnerBackend:
    """Small deterministic backend whose process identity is observable."""

    def __init__(self, serial: str | None = None, restore_log_path: str | None = None, block_acquire: bool = False, fail_snapshot: bool = False, fail_disconnects: int = 0, fail_abort: bool = False) -> None:
        self.serial = serial or "FAKE-OWNER"
        self.backend_pid = os.getpid()
        self.connected = False
        self._gain = 100
        self._target_temperature_c = -10.0
        self._cooler_on = False
        self._heater_level = 2
        self.abort_calls = 0
        self.disconnect_calls = 0
        self._restore_log_path = restore_log_path
        self._block_acquire = block_acquire
        self._fail_snapshot = fail_snapshot
        self._abort_requested = Event()
        self._fail_disconnects = fail_disconnects
        self._fail_abort = fail_abort

    @property
    def capabilities(self) -> CameraCapabilities:
        return CameraCapabilities(16, 16, 9.0, 9.0, ("RAW 12-bit",), 1, True, False)

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        if self._fail_disconnects:
            self._fail_disconnects -= 1
            raise RuntimeError("fake disconnect failed")
        if self._restore_log_path is not None:
            Path(self._restore_log_path).write_text(str(self._heater_level), encoding="utf-8")
        self.connected = False

    def acquire(self, duration_s: float, roi: Roi, binning: int, readout_mode: ReadoutMode, control=None) -> Frame:
        self._require_connected()
        if self._block_acquire:
            deadline = time.monotonic() + 10
            while not self._abort_requested.wait(0.01):
                if time.monotonic() >= deadline:
                    raise TimeoutError("test acquisition remained blocked")
            raise RuntimeError("fake acquisition aborted")
        pixels = np.arange(roi.width * roi.height, dtype=np.uint16).reshape(roi.height, roi.width)
        return Frame(pixels, datetime.now(timezone.utc), duration_s, binning, roi, readout_mode, self._gain)

    def abort(self) -> None:
        self.abort_calls += 1
        self._abort_requested.set()
        if self._fail_abort:
            raise RuntimeError("fake abort failed")

    @property
    def gain(self) -> int:
        self._require_connected()
        return self._gain

    @gain.setter
    def gain(self, value: int) -> None:
        self._require_connected()
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
        self._cooler_on = bool(enabled)

    def tec_voltage(self) -> tuple[float, float]:
        self._require_connected()
        return (0.0, 6.0)

    @property
    def cooler_power_percent(self) -> float:
        return 0.0

    @property
    def heater_supported(self) -> bool:
        return True

    @property
    def heater_max_level(self) -> int:
        return 4

    @property
    def heater_level(self) -> int:
        self._require_connected()
        if self._fail_snapshot:
            self._fail_snapshot = False
            raise RuntimeError("snapshot failed")
        return self._heater_level

    @heater_level.setter
    def heater_level(self, value: int) -> None:
        self._require_connected()
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 4:
            raise CameraInvalidValueError("heater level must be an integer from 0 to 4")
        self._heater_level = value

    def _require_connected(self) -> None:
        if not self.connected:
            raise CameraNotConnectedError("camera is not connected")


def create_backend(serial: str | None = None, **kwargs: object) -> FakeOwnerBackend:
    restore_log_path = kwargs.get("restore_log_path")
    if restore_log_path is not None and not isinstance(restore_log_path, str):
        raise TypeError("restore_log_path must be a string")
    return FakeOwnerBackend(serial, restore_log_path, bool(kwargs.get("block_acquire", False)), bool(kwargs.get("fail_snapshot", False)), int(kwargs.get("fail_disconnects", 0)), bool(kwargs.get("fail_abort", False)))
