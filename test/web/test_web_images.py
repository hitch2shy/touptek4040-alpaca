from __future__ import annotations

import io
import struct
import tracemalloc
import zlib

import numpy as np

from touptek4040.camera_contract import Frame, ReadoutMode, Roi
from touptek4040.image_workspace import ImageWorkspace
from touptek4040.png_stream import stream_viewport_png
from touptek4040.web_console import analyze_generation

def _png_pixels(payload: bytes) -> tuple[int, int, bytes]:
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    offset = 8
    width = height = 0
    compressed = bytearray()
    while offset < len(payload):
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        kind = payload[offset + 4 : offset + 8]
        data = payload[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if kind == b"IHDR":
            width, height = struct.unpack(">II", data[:8])
        elif kind == b"IDAT":
            compressed.extend(data)
        elif kind == b"IEND":
            break
    return width, height, zlib.decompress(compressed)


def test_viewport_png_is_generation_cached_and_display_stretch_is_non_destructive(
    web_client,
    web_coordinator,
):
    before = web_coordinator.image_generation.pixels.tobytes()
    response = web_client.simulate_get(
        "/web/v1/viewport.png?black=100&white=2000&gamma=1.2&maxWidth=8&maxHeight=8"
    )
    width, height, rows = _png_pixels(response.content)

    assert response.status_code == 200
    assert response.content_type == "image/png"
    assert response.headers["etag"] == '"generation-1-b100-w2000-g1.2-8x8"'
    assert response.headers["cache-control"] == "private, max-age=0, must-revalidate"
    assert (width, height) == (8, 8)
    assert len(rows) == height * (width + 1)
    assert web_coordinator.image_generation.pixels.tobytes() == before


def test_statistics_histogram_and_fwhm_are_deterministic_and_bounded(web_client):
    first = web_client.simulate_get("/web/v1/statistics")
    second = web_client.simulate_get("/web/v1/statistics")

    assert first.status_code == 200
    assert first.json == second.json
    assert first.json["generation"] == 1
    assert first.json["count"] == 256
    assert first.json["minimum"] == 0
    assert first.json["maximum"] == 4095
    assert len(first.json["histogram"]) == 256
    assert sum(first.json["histogram"]) == 256
    assert first.json["peak"] == {"x": 8, "y": 8, "value": 4095}
    assert set(first.json["fwhm"]) == {"x", "y"}


def test_raw_pixel_and_fits_consume_the_same_generation(web_client):
    pixel = web_client.simulate_get("/web/v1/pixel?x=8&y=8")
    fits = web_client.simulate_get("/web/v1/image.fits")

    assert pixel.json == {"generation": 1, "x": 8, "y": 8, "value": 4095}
    assert fits.status_code == 200
    assert fits.content_type == "image/fits"
    assert fits.content[:8] == b"SIMPLE  "
    assert len(fits.content) % 2880 == 0


def test_full_frame_viewport_and_analysis_keep_temporary_memory_bounded(
    web_coordinator,
):
    source = web_coordinator.image_generation.frame
    frame = Frame(
        pixels=np.zeros((4096, 4096), dtype=np.uint16),
        started_utc=source.started_utc,
        duration_s=source.duration_s,
        binning=1,
        roi=Roi(0, 0, 4096, 4096),
        readout_mode=ReadoutMode.RAW12,
        gain=source.gain,
    )
    generation = ImageWorkspace().publish(frame)

    tracemalloc.start()
    try:
        png = b"".join(
            stream_viewport_png(
                generation,
                black=0,
                white=4095,
                gamma=1.0,
                max_width=1600,
                max_height=1200,
            )
        )
        _, png_peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    width, height, _ = _png_pixels(png)

    tracemalloc.start()
    try:
        statistics = analyze_generation(generation)
        _, statistics_peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert width <= 1600
    assert height <= 1200
    assert png_peak < 16 * 1024 * 1024
    assert statistics_peak < 20 * 1024 * 1024
    assert statistics["count"] == 4096 * 4096
    assert sum(statistics["histogram"]) == 4096 * 4096
