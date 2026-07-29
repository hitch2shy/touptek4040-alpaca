from __future__ import annotations

import numpy as np
import pytest
import time

from test.hardware.conftest import (
    EXPECTED_MODEL,
    EXPECTED_SERIAL,
    full_frame_roi,
    require_unique_target,
)
from types import SimpleNamespace
from touptek4040.camera_contract import ReadoutMode, Roi
from touptek4040.camera_coordinator import CameraCoordinator


@pytest.mark.parametrize("binning", range(1, 9))
def test_full_frame_roi_is_largest_even_sdk_valid_square(binning: int) -> None:
    roi = full_frame_roi(binning)
    expected = ((4096 // binning) // 2) * 2

    assert roi.x == 0
    assert roi.y == 0
    assert roi.width == expected
    assert roi.height == expected
    assert roi.width * binning <= 4096
    assert (roi.width + 2) * binning > 4096
    assert all(value % 2 == 0 for value in (roi.x, roi.y, roi.width, roi.height))


def test_hardware_guard_rejects_an_extra_or_wrong_camera() -> None:
    target = SimpleNamespace(serial=EXPECTED_SERIAL, model=EXPECTED_MODEL)
    wrong = SimpleNamespace(serial="OTHER", model=EXPECTED_MODEL)

    assert require_unique_target([target]) is target
    with pytest.raises(RuntimeError, match="exactly one"):
        require_unique_target([target, wrong])
    with pytest.raises(RuntimeError, match="target camera"):
        require_unique_target([wrong])


@pytest.mark.hardware
@pytest.mark.parametrize("mode", [ReadoutMode.RAW12, ReadoutMode.RAW8])
@pytest.mark.parametrize("binning", range(1, 9))
def test_real_camera_mode_and_binning(
    real_camera,
    hardware_evidence,
    mode: ReadoutMode,
    binning: int,
) -> None:
    frame = real_camera.acquire(0.01, full_frame_roi(binning), binning, mode)
    expected = ((4096 // binning) // 2) * 2
    maximum = 4095 if mode is ReadoutMode.RAW12 else 255
    dtype = np.dtype(np.uint16 if mode is ReadoutMode.RAW12 else np.uint8)

    assert frame.pixels.shape == (expected, expected)
    assert frame.pixels.dtype == dtype
    assert int(frame.pixels.max()) <= maximum
    hardware_evidence.record(
        "mode-and-binning",
        serial=EXPECTED_SERIAL,
        mode=mode.name,
        binning=binning,
        width=frame.pixels.shape[1],
        height=frame.pixels.shape[0],
        dtype=str(frame.pixels.dtype),
        minimum=int(frame.pixels.min()),
        maximum=int(frame.pixels.max()),
    )


@pytest.mark.hardware
def test_real_even_offset_roi_and_repeated_short_acquisitions(
    real_camera,
    hardware_evidence,
) -> None:
    frames = [
        real_camera.acquire(
            0.01,
            full_frame_roi(8) if index == 0 else Roi(10, 12, 128, 96),
            8 if index == 0 else 1,
            ReadoutMode.RAW12,
        )
        for index in range(3)
    ]

    assert frames[0].pixels.shape == (512, 512)
    assert [frame.pixels.shape for frame in frames[1:]] == [(96, 128), (96, 128)]
    assert all(
        earlier.started_utc <= later.started_utc
        for earlier, later in zip(frames, frames[1:])
    )
    hardware_evidence.record(
        "roi-and-repeated",
        frame_shapes=[list(frame.pixels.shape) for frame in frames],
        monotonically_started=True,
    )


@pytest.mark.hardware
@pytest.mark.parametrize(
    ("roi", "binning"),
    [
        (Roi(1, 1, 1, 1), 1),
        (Roi(1, 1, 1, 1), 3),
        (Roi(4095, 4095, 1, 1), 1),
    ],
)
def test_real_ascom_one_pixel_roi_uses_sdk_padding_and_software_crop(
    real_camera,
    hardware_evidence,
    roi: Roi,
    binning: int,
) -> None:
    frame = real_camera.acquire(0.01, roi, binning, ReadoutMode.RAW12)

    assert frame.pixels.shape == (1, 1)
    hardware_evidence.record(
        "ascom-one-pixel-roi",
        roi=[roi.x, roi.y, roi.width, roi.height],
        binning=binning,
        shape=list(frame.pixels.shape),
        value=int(frame.pixels[0, 0]),
    )


@pytest.mark.hardware
@pytest.mark.parametrize("mode", [ReadoutMode.RAW12, ReadoutMode.RAW8])
def test_real_ascom_full_frame_bin3_uses_exact_1365_geometry(
    real_camera,
    hardware_evidence,
    mode: ReadoutMode,
) -> None:
    frame = real_camera.acquire(
        0.01,
        Roi(0, 0, 4096 // 3, 4096 // 3),
        3,
        mode,
    )

    assert frame.pixels.shape == (1365, 1365)
    assert frame.pixels.dtype == np.dtype(
        np.uint16 if mode is ReadoutMode.RAW12 else np.uint8
    )
    assert int(frame.pixels.max()) <= (
        4095 if mode is ReadoutMode.RAW12 else 255
    )
    hardware_evidence.record(
        "ascom-full-frame-bin3-fallback",
        serial=EXPECTED_SERIAL,
        mode=mode.name,
        shape=list(frame.pixels.shape),
        dtype=str(frame.pixels.dtype),
        minimum=int(frame.pixels.min()),
        maximum=int(frame.pixels.max()),
    )


@pytest.mark.hardware
def test_real_camera_disconnect_and_serial_reconnect(
    hardware_session,
    hardware_evidence,
) -> None:
    camera = hardware_session.camera
    camera.disconnect()
    assert camera._handle is None
    camera.connect()
    hardware_session.instrument_handle()

    assert camera.serial == EXPECTED_SERIAL
    assert camera._handle is not None
    hardware_evidence.record(
        "reconnect",
        serial=camera.serial,
        reconnected=True,
    )


@pytest.mark.hardware
def test_real_coordinator_preview_publishes_and_stops(
    hardware_session,
    hardware_evidence,
) -> None:
    camera = hardware_session.camera
    coordinator = CameraCoordinator(lambda _source: camera, "hardware")
    received = []
    coordinator.set_connected_sync(True)
    try:
        coordinator.start_preview(received.append)
        deadline = time.monotonic() + 8.0
        while not received and time.monotonic() < deadline:
            time.sleep(0.01)
        assert received
        assert coordinator.preview_active is True
        assert received[0].pixels.shape == (4096, 4096)
        coordinator.stop_preview()
        assert coordinator.preview_active is False
        assert coordinator.preview_frame is None
        hardware_evidence.record(
            "preview",
            frames_received=len(received),
            frame_shape=list(received[0].pixels.shape),
            stopped=True,
        )
    finally:
        if coordinator.preview_active:
            coordinator.stop_preview()
        coordinator.set_connected_sync(False)
        hardware_session.ensure_connected()
