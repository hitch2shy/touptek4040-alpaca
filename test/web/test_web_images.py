from __future__ import annotations

import io
import struct
import threading
import tracemalloc
import zlib
from datetime import datetime, timezone

import numpy as np
import pytest

from touptek4040.camera_contract import Frame, ReadoutMode, Roi
from touptek4040.image_workspace import ImageWorkspace
from touptek4040.png_stream import ViewportCrop, stream_viewport_png
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
        "/web/v1/viewport.png?generation=1&black=100&white=2000"
        "&gamma=1.2&maxWidth=8&maxHeight=8"
    )
    width, height, rows = _png_pixels(response.content)

    assert response.status_code == 200
    assert response.content_type == "image/png"
    assert response.headers["etag"] == '"generation-1-b100-w2000-g1.2-8x8"'
    assert response.headers["cache-control"] == "private, max-age=0, must-revalidate"
    assert (width, height) == (8, 8)
    assert len(rows) == height * (width + 1)
    assert web_coordinator.image_generation.pixels.tobytes() == before


@pytest.mark.parametrize(
    "query",
    [
        "x=0",
        "x=0&y=0",
        "x=0&y=0&width=1",
        "x=-1&y=0&width=1&height=1",
        "x=15&y=15&width=2&height=2",
        "x=0&y=0&width=0&height=1",
    ],
)
def test_viewport_crop_rejects_incomplete_or_out_of_bounds_tuples(
    web_client,
    query,
):
    response = web_client.simulate_get(
        f"/web/v1/viewport.png?generation=1&{query}"
    )

    assert response.status_code == 400


def test_viewport_crop_etag_pins_crop_and_stretch(web_client):
    response = web_client.simulate_get(
        "/web/v1/viewport.png?generation=1&x=2&y=3&width=4&height=5"
        "&black=10&white=2000&gamma=1.25&maxWidth=4&maxHeight=5"
    )

    assert response.status_code == 200
    assert response.headers["etag"] == (
        '"generation-1-c2-3-4x5-b10-w2000-g1.25-4x5"'
    )


def test_statistics_histogram_and_fwhm_are_deterministic_and_bounded(web_client):
    first = web_client.simulate_get("/web/v1/statistics?generation=1")
    second = web_client.simulate_get("/web/v1/statistics?generation=1")

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
    pixel = web_client.simulate_get("/web/v1/pixel?generation=1&x=8&y=8")
    fits = web_client.simulate_get("/web/v1/image.fits?generation=1")

    assert pixel.json == {"generation": 1, "x": 8, "y": 8, "value": 4095}
    assert fits.status_code == 200
    assert fits.content_type == "image/fits"
    assert fits.content[:8] == b"SIMPLE  "
    assert len(fits.content) % 2880 == 0


@pytest.mark.parametrize(
    "path",
    [
        "/web/v1/viewport.png",
        "/web/v1/statistics",
        "/web/v1/pixel?x=0&y=0",
        "/web/v1/image.fits",
    ],
)
def test_image_services_require_an_explicit_generation(web_client, path):
    response = web_client.simulate_get(path)

    assert response.status_code == 400
    assert response.json["error"] == "Invalid request body"


@pytest.mark.parametrize(
    "path",
    [
        "/web/v1/viewport.png?generation=2",
        "/web/v1/statistics?generation=2",
        "/web/v1/pixel?generation=2&x=0&y=0",
        "/web/v1/image.fits?generation=2",
    ],
)
def test_image_services_reject_a_stale_generation_with_stable_conflict(
    web_client,
    path,
):
    response = web_client.simulate_get(path)

    assert response.status_code == 409
    assert response.json == {"error": "Image generation changed"}


@pytest.mark.parametrize(
    "path",
    [
        "/web/v1/viewport.png?generation=1",
        "/web/v1/statistics?generation=1",
        "/web/v1/pixel?generation=1&x=0&y=0",
        "/web/v1/image.fits?generation=1",
    ],
)
def test_image_services_return_stable_not_found_when_no_generation_exists(
    web_client,
    web_coordinator,
    path,
):
    web_coordinator.image_generation = None

    response = web_client.simulate_get(path)

    assert response.status_code == 404
    assert response.json == {"error": "No image generation is available"}


def _generation_for(
    pixels: np.ndarray,
    *,
    readout_mode: ReadoutMode = ReadoutMode.RAW8,
) -> object:
    height, width = pixels.shape
    frame = Frame(
        pixels=pixels,
        started_utc=datetime(2026, 7, 28, tzinfo=timezone.utc),
        duration_s=1.0,
        binning=1,
        roi=Roi(0, 0, width, height),
        readout_mode=readout_mode,
        gain=100,
    )
    return ImageWorkspace().publish(frame)


@pytest.mark.parametrize(
    ("shape", "limits", "expected"),
    [
        ((2, 8), (4, 4), (4, 1)),
        ((8, 2), (4, 4), (1, 4)),
        ((1, 5), (2, 2), (2, 1)),
        ((5, 1), (2, 2), (1, 2)),
    ],
)
def test_png_uses_one_proportional_step_for_wide_tall_and_one_pixel_rois(
    shape,
    limits,
    expected,
):
    generation = _generation_for(np.zeros(shape, dtype=np.uint8))
    payload = b"".join(
        stream_viewport_png(
            generation,
            black=0,
            white=255,
            gamma=1.0,
            max_width=limits[0],
            max_height=limits[1],
        )
    )

    width, height, _ = _png_pixels(payload)
    assert (width, height) == expected


def test_png_common_step_preserves_sensor_orientation_and_pixel_mapping():
    generation = _generation_for(
        np.array(
            [
                [0, 10, 20, 30],
                [40, 50, 60, 70],
                [80, 90, 100, 110],
                [120, 130, 140, 150],
            ],
            dtype=np.uint8,
        )
    )
    payload = b"".join(
        stream_viewport_png(
            generation,
            black=0,
            white=255,
            gamma=1.0,
            max_width=2,
            max_height=2,
        )
    )

    width, height, rows = _png_pixels(payload)
    assert (width, height) == (2, 2)
    assert rows == bytes([0, 0, 20, 0, 80, 100])


def test_viewport_crop_uses_exact_original_coordinates_without_mutation():
    pixels = np.array(
        [
            [0, 10, 20, 30],
            [40, 50, 60, 70],
            [80, 90, 100, 110],
            [120, 130, 140, 150],
        ],
        dtype=np.uint8,
    )
    generation = _generation_for(pixels)
    before = pixels.tobytes()
    payload = b"".join(
        stream_viewport_png(
            generation,
            black=0,
            white=255,
            gamma=1.0,
            max_width=2,
            max_height=2,
            crop=ViewportCrop(1, 1, 2, 2),
        )
    )

    assert _png_pixels(payload) == (
        2,
        2,
        bytes([0, 50, 60, 0, 90, 100]),
    )
    assert pixels.tobytes() == before


@pytest.mark.parametrize(
    ("pixels", "crop", "limits", "stretch", "expected"),
    [
        (
            np.array(
                [
                    [0, 10, 20, 30],
                    [40, 50, 60, 70],
                    [80, 90, 100, 110],
                    [120, 130, 140, 150],
                ],
                dtype=np.uint8,
            ),
            (0, 0, 2, 2),
            (2, 2),
            (0, 255),
            (2, 2, bytes([0, 0, 10, 0, 40, 50])),
        ),
        (
            np.array(
                [
                    [0, 10, 20, 30],
                    [40, 50, 60, 70],
                    [80, 90, 100, 110],
                    [120, 130, 140, 150],
                ],
                dtype=np.uint8,
            ),
            (2, 2, 2, 2),
            (2, 2),
            (0, 255),
            (2, 2, bytes([0, 100, 110, 0, 140, 150])),
        ),
        (
            np.array(
                [
                    [0, 10, 20, 30],
                    [40, 50, 60, 70],
                    [80, 90, 100, 110],
                    [120, 130, 140, 150],
                ],
                dtype=np.uint8,
            ),
            (3, 0, 1, 1),
            (1, 1),
            (0, 255),
            (1, 1, bytes([0, 30])),
        ),
        (
            np.arange(25, dtype=np.uint8).reshape(5, 5),
            (1, 1, 3, 3),
            (2, 2),
            (0, 255),
            (2, 2, bytes([0, 6, 8, 0, 16, 18])),
        ),
        (
            np.array(
                [
                    [0, 0, 0, 0],
                    [0, 0, 4095, 0],
                    [0, 2048, 1024, 0],
                    [0, 0, 0, 0],
                ],
                dtype=np.uint16,
            ),
            (1, 1, 2, 2),
            (2, 2),
            (0, 4095),
            (2, 2, bytes([0, 0, 255, 0, 127, 63])),
        ),
        (
            np.array(
                [
                    [0, 10, 20, 30],
                    [40, 50, 60, 70],
                    [80, 90, 100, 110],
                    [120, 130, 140, 150],
                ],
                dtype=np.uint8,
            ),
            (0, 0, 4, 4),
            (2, 2),
            (0, 255),
            (2, 2, bytes([0, 0, 20, 0, 80, 100])),
        ),
    ],
)
def test_viewport_crop_preserves_edges_raw_depth_and_common_step(
    pixels,
    crop,
    limits,
    stretch,
    expected,
):
    generation = _generation_for(
        pixels,
        readout_mode=(
            ReadoutMode.RAW12 if pixels.dtype == np.uint16 else ReadoutMode.RAW8
        ),
    )

    payload = b"".join(
        stream_viewport_png(
            generation,
            black=stretch[0],
            white=stretch[1],
            gamma=1.0,
            max_width=limits[0],
            max_height=limits[1],
            crop=ViewportCrop(*crop),
        )
    )

    assert _png_pixels(payload) == expected


def test_viewport_crop_can_render_at_the_exact_2048_output_edges():
    generation = _generation_for(np.zeros((2048, 2048), dtype=np.uint8))

    payload = b"".join(
        stream_viewport_png(
            generation,
            black=0,
            white=255,
            gamma=1.0,
            max_width=2048,
            max_height=2048,
            crop=ViewportCrop(0, 0, 2048, 2048),
        )
    )

    width, height, rows = _png_pixels(payload)
    assert (width, height) == (2048, 2048)
    assert len(rows) == 2048 * 2049


@pytest.mark.parametrize(
    "crop",
    [
        ViewportCrop(-1, 0, 1, 1),
        ViewportCrop(0, 0, 0, 1),
        ViewportCrop(0, 0, 1, 0),
        ViewportCrop(3, 3, 2, 2),
        ViewportCrop(True, 0, 1, 1),
    ],
)
def test_viewport_crop_rejects_invalid_original_pixel_bounds_before_output(crop):
    generation = _generation_for(np.zeros((4, 4), dtype=np.uint8))

    with pytest.raises(ValueError):
        next(
            stream_viewport_png(
                generation,
                black=0,
                white=255,
                gamma=1.0,
                max_width=2,
                max_height=2,
                crop=crop,
            )
        )


def test_raw8_generation_publishes_8bit_limits_and_accepts_default_stretch(
    web_client,
    web_coordinator,
):
    source = web_coordinator.image_generation.frame
    frame = Frame(
        pixels=np.arange(256, dtype=np.uint8).reshape(16, 16),
        started_utc=source.started_utc,
        duration_s=source.duration_s,
        binning=1,
        roi=Roi(0, 0, 16, 16),
        readout_mode=ReadoutMode.RAW8,
        gain=source.gain,
    )
    web_coordinator.image_generation = ImageWorkspace().publish(frame)

    state = web_client.simulate_get("/web/v1/state")
    viewport = web_client.simulate_get("/web/v1/viewport.png?generation=1")
    invalid_12bit = web_client.simulate_get(
        "/web/v1/viewport.png?generation=1&white=4095"
    )

    assert state.json["image"] == {
        "width": 16,
        "height": 16,
        "maxADU": 255,
        "readoutMode": "RAW 8-bit",
    }
    assert viewport.status_code == 200
    assert invalid_12bit.status_code == 400


def test_pixel_request_keeps_the_pinned_generation_during_concurrent_publish(
    web_coordinator,
    web_config,
):
    import falcon.testing

    from touptek4040.alpaca.app import create_app

    source = web_coordinator.image_generation.frame
    workspace = ImageWorkspace()
    first = workspace.publish(source)
    second = workspace.publish(
        Frame(
            pixels=np.full((16, 16), 17, dtype=np.uint16),
            started_utc=source.started_utc,
            duration_s=source.duration_s,
            binning=1,
            roi=source.roi,
            readout_mode=source.readout_mode,
            gain=source.gain,
        )
    )
    entered = threading.Event()
    release = threading.Event()

    class RacingCoordinator:
        def __init__(self, wrapped):
            self._wrapped = wrapped
            self._current = first

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

        @property
        def preview_generation(self):
            return None

        @property
        def image_generation(self):
            captured = self._current
            entered.set()
            assert release.wait(1)
            return captured

    coordinator = RacingCoordinator(web_coordinator)
    client = falcon.testing.TestClient(create_app(coordinator, web_config))
    response = {}

    def request_pixel():
        response["value"] = client.simulate_get(
            "/web/v1/pixel?generation=1&x=8&y=8"
        )

    request = threading.Thread(target=request_pixel)
    request.start()
    assert entered.wait(1)
    coordinator._current = second
    release.set()
    request.join(1)

    assert response["value"].json == {
        "generation": 1,
        "x": 8,
        "y": 8,
        "value": 4095,
    }


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
