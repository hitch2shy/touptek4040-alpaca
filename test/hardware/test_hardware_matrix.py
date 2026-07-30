from __future__ import annotations

import numpy as np
import pytest
import time

import test.hardware.conftest as hardware_support
from test.hardware.conftest import (
    EXPECTED_MODEL,
    EXPECTED_SERIAL,
    full_frame_roi,
    open_verified_target,
    require_unique_raw_target,
)
from types import SimpleNamespace
from touptek4040.camera_contract import ExposureSettings, ReadoutMode, Roi
from touptek4040.camera_coordinator import CameraCoordinator, CameraState


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


def test_hardware_guard_rejects_an_extra_or_wrong_camera_before_open() -> None:
    target = SimpleNamespace(
        id=r"\\?\usb#approved-interface",
        displayname=EXPECTED_MODEL,
        model=SimpleNamespace(name=EXPECTED_MODEL),
    )
    wrong = SimpleNamespace(
        id=r"\\?\usb#other-interface",
        displayname="OTHER",
        model=SimpleNamespace(name="OTHER"),
    )

    assert require_unique_raw_target([target]) is target

    missing_id = SimpleNamespace(
        id="",
        displayname=EXPECTED_MODEL,
        model=SimpleNamespace(name=EXPECTED_MODEL),
    )
    for devices in ([target, wrong], [wrong], [missing_id]):
        opens = []
        sdk = SimpleNamespace(
            Toupcam=SimpleNamespace(
                EnumV2=lambda: devices,
                Open=lambda selector: opens.append(selector),
            )
        )
        with pytest.raises(RuntimeError):
            open_verified_target(sdk)
        assert opens == []


def test_hardware_guard_opens_only_the_approved_serial_and_closes(monkeypatch) -> None:
    device = SimpleNamespace(
        id=r"\\?\usb#opaque-interface-id",
        displayname=EXPECTED_MODEL,
        model=SimpleNamespace(name=EXPECTED_MODEL),
    )
    calls = []
    handle = SimpleNamespace(Close=lambda: calls.append("Close"))
    expected_identity = SimpleNamespace(
        serial=EXPECTED_SERIAL,
        model=EXPECTED_MODEL,
    )
    sdk = SimpleNamespace(
        Toupcam=SimpleNamespace(
            EnumV2=lambda: calls.append("EnumV2") or [device],
            Open=lambda selector: calls.append(("Open", selector)) or handle,
        )
    )
    monkeypatch.setattr(
        hardware_support,
        "_identity_from_handle",
        lambda enumerated, opened: (
            calls.append(("identity", enumerated is device, opened is handle))
            or expected_identity
        ),
    )

    assert open_verified_target(sdk) is expected_identity
    assert calls == [
        "EnumV2",
        ("Open", f"sn:{EXPECTED_SERIAL}"),
        ("identity", True, True),
        "Close",
    ]


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
def test_real_structural_reopens_preserve_persistent_native_state(
    hardware_session,
    hardware_evidence,
) -> None:
    camera = hardware_session.camera
    camera.acquire(0.01, Roi(0, 0, 256, 256), 1, ReadoutMode.RAW12)
    before = hardware_session.read_snapshot()
    call_start = len(hardware_session.native_calls)

    camera.acquire(0.01, Roi(0, 0, 128, 96), 1, ReadoutMode.RAW8)
    camera.acquire(0.01, Roi(0, 0, 128, 128), 2, ReadoutMode.RAW12)
    after = hardware_session.read_snapshot()
    reconfiguration_calls = hardware_session.native_calls[call_start:]
    persistent_options = {
        hardware_session.sdk.TOUPCAM_OPTION_TEC,
        hardware_session.sdk.TOUPCAM_OPTION_FAN,
        hardware_session.sdk.TOUPCAM_OPTION_ROTATE,
        hardware_session.sdk.TOUPCAM_OPTION_TEC_VOLTAGE_MAX,
    }
    redundant_setters = [
        [name, list(arguments)]
        for name, arguments in reconfiguration_calls
        if name in {"put_ExpoAGain", "put_Temperature", "put_HFlip", "put_VFlip"}
        or (
            name == "put_Option"
            and arguments
            and arguments[0] in persistent_options
        )
    ]
    persistent_fields = (
        "gain",
        "exposure_us",
        "auto_exposure",
        "tec_target",
        "tec",
        "fan",
        "rotate",
        "tec_voltage_max",
        "hflip",
        "vflip",
        "heater_level",
        "heater_max_level",
    )
    before_state = {name: getattr(before, name) for name in persistent_fields}
    after_state = {name: getattr(after, name) for name in persistent_fields}

    assert after_state == before_state
    assert redundant_setters == []
    hardware_evidence.record(
        "structural-reopen-state",
        serial=EXPECTED_SERIAL,
        before=before_state,
        after=after_state,
        preserved=True,
        native_options={
            "TOUPCAM_OPTION_HEAT": hardware_session.sdk.TOUPCAM_OPTION_HEAT,
            "TOUPCAM_OPTION_HEAT_MAX": (
                hardware_session.sdk.TOUPCAM_OPTION_HEAT_MAX
            ),
        },
        redundant_persistent_setters=redundant_setters,
    )


@pytest.mark.hardware
def test_real_dark_bias_zero_duration_clamps_to_sdk_minimum(
    hardware_session,
    hardware_evidence,
) -> None:
    camera = hardware_session.camera
    coordinator = CameraCoordinator(lambda _source: camera, "hardware")
    coordinator.set_connected_sync(True)
    try:
        coordinator.start_exposure(
            0.0,
            False,
            settings=lambda _capabilities: ExposureSettings(
                roi=Roi(0, 0, 128, 128),
                binning=1,
                readout_mode=ReadoutMode.RAW12,
                gain=camera.gain,
            ),
        )
        deadline = time.monotonic() + 8.0
        while (
            coordinator.snapshot().camera_state is not CameraState.IDLE
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        snapshot = coordinator.snapshot()
        frame = coordinator.frame

        assert snapshot.camera_state is CameraState.IDLE
        assert snapshot.image_ready is True
        assert frame is not None
        assert frame.duration_s == pytest.approx(12 / 1_000_000)
        hardware_evidence.record(
            "dark-bias-minimum",
            requested_duration_s=0.0,
            actual_duration_s=frame.duration_s,
            light=False,
            shape=list(frame.pixels.shape),
        )
    finally:
        coordinator.set_connected_sync(False)
        hardware_session.ensure_connected()


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
