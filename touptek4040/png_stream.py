"""Bounded grayscale PNG rendering for immutable image generations."""

from __future__ import annotations

import math
import struct
import zlib
from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

from .image_workspace import ImageGeneration

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_VIEWPORT_EDGE = 2048


@dataclass(frozen=True, slots=True)
class ViewportCrop:
    x: int
    y: int
    width: int
    height: int


def stream_viewport_png(
    generation: ImageGeneration,
    *,
    black: int,
    white: int,
    gamma: float,
    max_width: int,
    max_height: int,
    crop: ViewportCrop | None = None,
) -> Iterable[bytes]:
    """Render an 8-bit display view without mutating scientific pixels."""

    _validate_stretch(black, white, gamma, max_width, max_height)
    crop = _validate_crop(generation, crop)
    step = max(
        1,
        math.ceil(crop.width / max_width),
        math.ceil(crop.height / max_height),
    )
    width = math.ceil(crop.width / step)
    height = math.ceil(crop.height / step)

    yield _PNG_SIGNATURE
    yield _chunk(
        b"IHDR",
        struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0),
    )

    compressor = zlib.compressobj(level=6)
    inverse_gamma = 1.0 / gamma
    for source_y in range(0, crop.height, step):
        source = generation.pixels[
            crop.y + source_y,
            crop.x : crop.x + crop.width : step,
        ][:width]
        values = source.astype(np.float32, copy=True)
        values -= float(black)
        values /= float(white - black)
        np.clip(values, 0.0, 1.0, out=values)
        np.power(values, inverse_gamma, out=values)
        values *= 255.0
        row = b"\x00" + values.astype(np.uint8).tobytes()
        compressed = compressor.compress(row)
        if compressed:
            yield _chunk(b"IDAT", compressed)
    tail = compressor.flush()
    if tail:
        yield _chunk(b"IDAT", tail)
    yield _chunk(b"IEND", b"")


def _validate_crop(
    generation: ImageGeneration,
    crop: ViewportCrop | None,
) -> ViewportCrop:
    if crop is None:
        crop = ViewportCrop(0, 0, generation.width, generation.height)
    if not isinstance(crop, ViewportCrop):
        raise ValueError("crop must be a ViewportCrop")
    for value, name in (
        (crop.x, "crop x"),
        (crop.y, "crop y"),
        (crop.width, "crop width"),
        (crop.height, "crop height"),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
    if crop.x < 0 or crop.y < 0:
        raise ValueError("crop origin must not be negative")
    if crop.width <= 0 or crop.height <= 0:
        raise ValueError("crop size must be positive")
    if (
        crop.x + crop.width > generation.width
        or crop.y + crop.height > generation.height
    ):
        raise ValueError("crop is outside the image generation")
    return crop


def _validate_stretch(
    black: int,
    white: int,
    gamma: float,
    max_width: int,
    max_height: int,
) -> None:
    for value, name in ((black, "black"), (white, "white")):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
        if not 0 <= value <= 4095:
            raise ValueError(f"{name} must be between 0 and 4095")
    if white <= black:
        raise ValueError("white must be greater than black")
    if (
        isinstance(gamma, bool)
        or not isinstance(gamma, (int, float))
        or not math.isfinite(float(gamma))
        or not 0.1 <= float(gamma) <= 5.0
    ):
        raise ValueError("gamma must be between 0.1 and 5.0")
    for value, name in (
        (max_width, "maxWidth"),
        (max_height, "maxHeight"),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= _MAX_VIEWPORT_EDGE
        ):
            raise ValueError(
                f"{name} must be an integer from 1 through {_MAX_VIEWPORT_EDGE}"
            )


def _chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(kind)
    checksum = zlib.crc32(payload, checksum)
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", checksum & 0xFFFFFFFF)
    )
