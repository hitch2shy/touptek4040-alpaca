"""Streaming FITS output for immutable monochrome image generations."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timezone

import numpy as np

from .camera_contract import ReadoutMode
from .image_workspace import ImageGeneration

_FITS_BLOCK_SIZE = 2880
_CARD_SIZE = 80
_ROWS_PER_CHUNK = 128
_SENSOR_NAME = "sMAX16AM-U3-CL-GPS"


def stream_fits(generation: ImageGeneration) -> Iterable[bytes]:
    """Yield a standards-compliant unsigned 16-bit primary FITS HDU."""

    yield _header(generation)
    pixels = generation.pixels
    for y0 in range(0, generation.height, _ROWS_PER_CHUNK):
        block = pixels[y0 : y0 + _ROWS_PER_CHUNK, :]
        signed = (block.astype(np.int32) - 32768).astype(">i2")
        yield signed.tobytes(order="C")

    data_size = generation.width * generation.height * 2
    padding = (-data_size) % _FITS_BLOCK_SIZE
    if padding:
        yield bytes(padding)


def _header(generation: ImageGeneration) -> bytes:
    frame = generation.frame
    started_utc = frame.started_utc.astimezone(timezone.utc)
    date_obs = (
        started_utc.isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    readout = (
        "RAW 12-bit"
        if frame.readout_mode is ReadoutMode.RAW12
        else "RAW 8-bit"
    )
    values: tuple[tuple[str, object], ...] = (
        ("SIMPLE", True),
        ("BITPIX", 16),
        ("NAXIS", 2),
        ("NAXIS1", generation.width),
        ("NAXIS2", generation.height),
        ("BZERO", 32768),
        ("BSCALE", 1),
        ("DATE-OBS", date_obs),
        ("EXPTIME", frame.duration_s),
        ("XBINNING", frame.binning),
        ("YBINNING", frame.binning),
        ("XORGSUBF", frame.roi.x * frame.binning),
        ("YORGSUBF", frame.roi.y * frame.binning),
        ("GAIN", frame.gain),
        ("INSTRUME", _SENSOR_NAME),
        ("SENSORTY", "Monochrome"),
        ("READOUT", readout),
    )
    cards = b"".join(_card(keyword, value) for keyword, value in values)
    cards += b"END".ljust(_CARD_SIZE, b" ")
    return cards.ljust(
        ((len(cards) + _FITS_BLOCK_SIZE - 1) // _FITS_BLOCK_SIZE)
        * _FITS_BLOCK_SIZE,
        b" ",
    )


def _card(keyword: str, value: object) -> bytes:
    if isinstance(value, bool):
        encoded = "T" if value else "F"
        field = encoded.rjust(20)
    elif isinstance(value, int):
        field = str(value).rjust(20)
    elif isinstance(value, float):
        field = format(value, ".15g").rjust(20)
    elif isinstance(value, str):
        escaped = value.replace("'", "''")
        field = f"'{escaped}'"
    else:
        raise TypeError(f"unsupported FITS value type: {type(value).__name__}")
    card = f"{keyword:<8}= {field}"
    encoded_card = card.encode("ascii")
    if len(encoded_card) > _CARD_SIZE:
        raise ValueError(f"FITS card is too long: {keyword}")
    return encoded_card.ljust(_CARD_SIZE, b" ")
