"""Thread-safe immutable image generations for science and preview frames."""

from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np

from .camera_contract import Frame, ReadoutMode


@dataclass(frozen=True, slots=True)
class ImageGeneration:
    """One stable image snapshot that remains valid across later acquisitions."""

    generation_id: int
    pixels: np.ndarray
    frame: Frame

    @property
    def width(self) -> int:
        return int(self.pixels.shape[1])

    @property
    def height(self) -> int:
        return int(self.pixels.shape[0])


class ImageWorkspace:
    """Publish immutable frames into independent formal and preview slots."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._next_generation_id = 1
        self._formal: ImageGeneration | None = None
        self._preview: ImageGeneration | None = None

    def publish(self, frame: Frame) -> ImageGeneration:
        pixels, immutable_frame = self._snapshot_frame(frame)
        with self._lock:
            generation = self._new_generation_locked(pixels, immutable_frame)
            self._formal = generation
        return generation

    def publish_preview(self, frame: Frame) -> ImageGeneration:
        pixels, immutable_frame = self._snapshot_frame(frame)
        with self._lock:
            generation = self._new_generation_locked(pixels, immutable_frame)
            self._preview = generation
        return generation

    def current(self) -> ImageGeneration | None:
        with self._lock:
            return self._formal

    def current_preview(self) -> ImageGeneration | None:
        with self._lock:
            return self._preview

    def clear_formal(self) -> None:
        with self._lock:
            self._formal = None

    def clear_preview(self) -> None:
        with self._lock:
            self._preview = None

    def _snapshot_frame(self, frame: Frame) -> tuple[np.ndarray, Frame]:
        if not isinstance(frame, Frame):
            raise TypeError("frame must be a Frame")
        source = np.asarray(frame.pixels)
        if source.ndim != 2:
            raise ValueError("frame pixels must be a two-dimensional array")
        expected_shape = (frame.roi.height, frame.roi.width)
        if source.shape != expected_shape:
            raise ValueError(
                "frame pixel dimensions must match the ROI height and width"
            )
        expected_dtype = np.dtype(
            np.uint16
            if frame.readout_mode is ReadoutMode.RAW12
            else np.uint8
        )
        if (
            frame.readout_mode is ReadoutMode.RAW8
            and source.dtype.kind == "u"
            and source.dtype.itemsize <= np.dtype(np.uint16).itemsize
            and (source.size == 0 or int(source.max()) <= 255)
        ):
            normalized = source.astype(expected_dtype, copy=False)
        elif source.dtype == expected_dtype:
            normalized = source
        else:
            raise ValueError(
                "frame pixels must use the unsigned dtype for the readout mode"
            )

        immutable_storage = normalized.tobytes(order="C")
        pixels = np.frombuffer(immutable_storage, dtype=expected_dtype).reshape(
            expected_shape
        )
        immutable_frame = Frame(
            pixels=pixels,
            started_utc=frame.started_utc,
            duration_s=frame.duration_s,
            binning=frame.binning,
            roi=frame.roi,
            readout_mode=frame.readout_mode,
            gain=frame.gain,
        )
        return pixels, immutable_frame

    def _new_generation_locked(
        self,
        pixels: np.ndarray,
        frame: Frame,
    ) -> ImageGeneration:
        generation = ImageGeneration(
            generation_id=self._next_generation_id,
            pixels=pixels,
            frame=frame,
        )
        self._next_generation_id += 1
        return generation
