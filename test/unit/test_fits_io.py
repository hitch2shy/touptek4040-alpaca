from __future__ import annotations

import struct
import tracemalloc

import numpy as np

from test.fixtures.reference_pixels import reference_frame
from touptek4040.alpaca.image_api import (
    ImageTransactions,
    stream_image_json,
    stream_imagebytes,
)
from touptek4040.camera_contract import Frame, ReadoutMode, Roi
from touptek4040.fits_io import stream_fits
from touptek4040.image_workspace import ImageWorkspace


def _cards(header: bytes) -> dict[str, str]:
    assert len(header) % 2880 == 0
    parsed: dict[str, str] = {}
    for offset in range(0, len(header), 80):
        card = header[offset : offset + 80].decode("ascii")
        key = card[:8].strip()
        if key == "END":
            break
        if card[8:10] == "= ":
            parsed[key] = card[10:80].split("/", 1)[0].strip()
    return parsed


def _split_fits(payload: bytes) -> tuple[bytes, bytes]:
    for offset in range(0, len(payload), 80):
        if payload[offset : offset + 8] == b"END     ":
            header_end = ((offset + 80 + 2879) // 2880) * 2880
            return payload[:header_end], payload[header_end:]
    raise AssertionError("FITS END card is missing")


def test_raw12_fits_has_unsigned16_metadata_utc_observation_and_exact_pixels():
    generation = ImageWorkspace().publish(reference_frame())

    payload = b"".join(stream_fits(generation))
    header, data = _split_fits(payload)
    cards = _cards(header)

    assert cards["SIMPLE"] == "T"
    assert cards["BITPIX"] == "16"
    assert cards["NAXIS"] == "2"
    assert cards["NAXIS1"] == "3"
    assert cards["NAXIS2"] == "2"
    assert cards["BZERO"] == "32768"
    assert cards["BSCALE"] == "1"
    assert cards["DATE-OBS"] == "'2026-07-28T12:34:56.123Z'"
    assert cards["EXPTIME"] == "0.125"
    assert cards["XBINNING"] == "2"
    assert cards["YBINNING"] == "2"
    assert cards["XORGSUBF"] == "14"
    assert cards["YORGSUBF"] == "22"
    assert cards["GAIN"] == "321"
    assert cards["INSTRUME"] == "'sMAX16AM-U3-CL-GPS'"
    assert cards["SENSORTY"] == "'Monochrome'"
    assert cards["READOUT"] == "'RAW 12-bit'"
    assert len(payload) % 2880 == 0
    stored = struct.unpack(">6h", data[:12])
    assert [value + 32768 for value in stored] == [1, 2, 3, 10, 20, 30]
    assert set(data[12:]) <= {0}


def test_raw8_fits_preserves_the_same_scientific_pixel_values():
    generation = ImageWorkspace().publish(
        reference_frame(readout_mode=ReadoutMode.RAW8)
    )

    payload = b"".join(stream_fits(generation))
    header, data = _split_fits(payload)
    cards = _cards(header)

    assert cards["BITPIX"] == "16"
    assert cards["BZERO"] == "32768"
    assert cards["READOUT"] == "'RAW 8-bit'"
    stored = struct.unpack(">6h", data[:12])
    assert [value + 32768 for value in stored] == [1, 2, 3, 10, 20, 30]


def test_full_frame_streams_do_not_materialize_a_transpose_or_python_pixel_list():
    frame = Frame(
        pixels=np.zeros((4096, 4096), dtype=np.uint16),
        started_utc=reference_frame().started_utc,
        duration_s=1.0,
        binning=1,
        roi=Roi(0, 0, 4096, 4096),
        readout_mode=ReadoutMode.RAW12,
        gain=100,
    )
    generation = ImageWorkspace().publish(frame)
    transactions = ImageTransactions(
        client_transaction_id=1,
        server_transaction_id=2,
    )

    for stream_factory in (
        lambda: stream_image_json(generation, transactions),
        lambda: stream_imagebytes(generation, transactions),
        lambda: stream_fits(generation),
    ):
        tracemalloc.start()
        stream = stream_factory()
        iterator = iter(stream)
        largest_chunk = 0
        try:
            for _ in range(2):
                chunk = next(iterator)
                largest_chunk = max(largest_chunk, len(chunk))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
            close = getattr(stream, "close", None)
            if close is not None:
                close()
        assert largest_chunk > 0
        assert largest_chunk < 2 * 1024 * 1024
        assert peak < 16 * 1024 * 1024
