"""Shared camera abstractions used by hardware and simulator implementations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from typing import Protocol, runtime_checkable

import numpy as np


class ReadoutMode(IntEnum):
    RAW12 = 0
    RAW8 = 1


@dataclass(frozen=True)
class Roi:
    """An unbinned rectangular sensor region in pixel coordinates."""

    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class CameraCapabilities:
    """Static operations and sensor geometry exposed by a camera."""

    width: int
    height: int
    pixel_size_x: float
    pixel_size_y: float
    readout_modes: tuple[str, ...]
    max_bin: int
    can_abort: bool
    can_stop: bool


@dataclass(frozen=True)
class Frame:
    """An acquired image and the settings that produced it."""

    pixels: np.ndarray
    started_utc: datetime
    duration_s: float
    binning: int
    roi: Roi
    readout_mode: ReadoutMode


@runtime_checkable
class CameraContract(Protocol):
    """Minimal synchronous interface shared by all camera backends."""

    @property
    def capabilities(self) -> CameraCapabilities: ...

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def acquire(
        self, duration_s: float, roi: Roi, binning: int, readout_mode: ReadoutMode
    ) -> Frame: ...

    def abort(self) -> None: ...
