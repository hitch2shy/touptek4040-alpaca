"""Spawn-safe fake backend used by camera-owner process tests."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import Event, Lock
import time

import numpy as np

from touptek4040.camera_contract import CameraCapabilities, Frame, ReadoutMode, Roi
from touptek4040.errors import CameraInvalidValueError, CameraNotConnectedError


class UnrenderableOwnerError(RuntimeError):
    """Exercise the owner's last-resort outer-finally path."""

    def __str__(self) -> str:
        raise RuntimeError("fake owner error rendering failed")


class ExplodingOwnerPayload(Mapping[str, object]):
    """Pickle-safe malformed payload that escapes normal error rendering."""

    def __getitem__(self, key: str) -> object:
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        raise UnrenderableOwnerError()

    def __len__(self) -> int:
        return 1


class _MalformedTimestamp:
    def isoformat(self) -> object:
        return object()


class FakeOwnerBackend:
    """Small deterministic backend whose process identity is observable."""

    def __init__(
        self,
        serial: str | None = None,
        *,
        restore_log_path: str | None = None,
        state_path: str | None = None,
        block_acquire: bool = False,
        fail_snapshot: bool = False,
        fail_disconnects: int = 0,
        fail_abort: bool = False,
        malformed_frame: bool = False,
    ) -> None:
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
        self._malformed_frame = malformed_frame
        self._state_path = Path(state_path) if state_path is not None else None
        self._state_lock = Lock()
        self._events: list[dict[str, object]] = []
        self._write_state()

    @property
    def capabilities(self) -> CameraCapabilities:
        return CameraCapabilities(16, 16, 9.0, 9.0, ("RAW 12-bit",), 1, True, False)

    def connect(self) -> None:
        self.connected = True
        self._record("connect")

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        if self._fail_disconnects:
            self._fail_disconnects -= 1
            self._record("disconnect", outcome="failed")
            raise RuntimeError("fake disconnect failed")
        self.connected = False
        self._record("disconnect", outcome="succeeded")
        if self._restore_log_path is not None:
            Path(self._restore_log_path).write_text(str(self._heater_level), encoding="utf-8")

    def acquire(self, duration_s: float, roi: Roi, binning: int, readout_mode: ReadoutMode, control=None) -> Frame:
        self._require_connected()
        if control is None:
            self._record("acquire", duration_s=duration_s)
        elif not control.commit_start(
            lambda: self._record("acquire", duration_s=duration_s)
        ):
            raise RuntimeError("fake acquisition was cancelled before start")
        if self._block_acquire:
            deadline = time.monotonic() + 10
            while not self._abort_requested.wait(0.01):
                if time.monotonic() >= deadline:
                    raise TimeoutError("test acquisition remained blocked")
            raise RuntimeError("fake acquisition aborted")
        pixels = np.arange(roi.width * roi.height, dtype=np.uint16).reshape(roi.height, roi.width)
        started_utc = _MalformedTimestamp() if self._malformed_frame else datetime.now(timezone.utc)
        return Frame(pixels, started_utc, duration_s, binning, roi, readout_mode, self._gain)

    def abort(self) -> None:
        self.abort_calls += 1
        self._abort_requested.set()
        self._record("abort", outcome="failed" if self._fail_abort else "succeeded")
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
        self._record("get_heater_level")
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
        self._record("set_heater_level", value=value)

    def _require_connected(self) -> None:
        if not self.connected:
            raise CameraNotConnectedError("camera is not connected")

    def _record(self, operation: str, **details: object) -> None:
        with self._state_lock:
            self._events.append({"operation": operation, **details})
            self._write_state_locked()

    def _write_state(self) -> None:
        with self._state_lock:
            self._write_state_locked()

    def _write_state_locked(self) -> None:
        if self._state_path is None:
            return
        counts: dict[str, int] = {}
        for event in self._events:
            operation = str(event["operation"])
            counts[operation] = counts.get(operation, 0) + 1
        state = {
            "backend_pid": self.backend_pid,
            "connected": self.connected,
            "heater_level": self._heater_level,
            "counts": counts,
            "events": self._events,
        }
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self._state_path.with_name(f".{self._state_path.name}.tmp")
        temporary_path.write_text(
            json.dumps(state, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary_path, self._state_path)


def create_backend(serial: str | None = None, **kwargs: object) -> FakeOwnerBackend:
    restore_log_path = kwargs.get("restore_log_path")
    if restore_log_path is not None and not isinstance(restore_log_path, str):
        raise TypeError("restore_log_path must be a string")
    state_path = kwargs.get("state_path")
    if state_path is not None and not isinstance(state_path, str):
        raise TypeError("state_path must be a string")
    return FakeOwnerBackend(
        serial,
        restore_log_path=restore_log_path,
        state_path=state_path,
        block_acquire=bool(kwargs.get("block_acquire", False)),
        fail_snapshot=bool(kwargs.get("fail_snapshot", False)),
        fail_disconnects=int(kwargs.get("fail_disconnects", 0)),
        fail_abort=bool(kwargs.get("fail_abort", False)),
        malformed_frame=bool(kwargs.get("malformed_frame", False)),
    )
