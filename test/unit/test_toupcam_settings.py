from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

import pytest

from test.fixtures.fake_toupcam import (
    OPTION_BINNING,
    OPTION_BITDEPTH,
    OPTION_RAW,
    OPTION_TEC,
    OPTION_TEC_VOLTAGE,
    OPTION_TEC_VOLTAGE_MAX,
    OPTION_TECTARGET,
    OPTION_TRIGGER,
    FakeSdk,
)
from touptek4040.camera_contract import ReadoutMode, Roi
from touptek4040.errors import CameraBusyError, CameraInvalidValueError
from touptek4040.toupcam_camera import CaptureSettings, ToupTekCamera


@pytest.fixture
def camera() -> ToupTekCamera:
    sdk = FakeSdk()
    result = ToupTekCamera("TP241025143218C92B01151AEC40CB7", sdk=sdk)
    result.connect()
    sdk.calls.clear()
    return result


def test_settings_are_applied_before_the_synchronous_trigger(camera: ToupTekCamera) -> None:
    """Wrong order would configure an SDK option after acquisition has begun."""
    camera.acquire(0.25, Roi(10, 12, 100, 80), 2, ReadoutMode.RAW12)

    assert camera._sdk.calls[:6] == [
        ("put_Option", OPTION_RAW, 1),
        ("put_Option", OPTION_BITDEPTH, 1),
        ("put_Option", OPTION_BINNING, 2),
        ("put_Roi", 20, 24, 200, 160),
        ("put_ExpoTime", 250_000),
        ("put_Option", OPTION_TRIGGER, 1),
    ]
    assert camera._sdk.calls.index(("put_Option", OPTION_TRIGGER, 1)) < next(
        index
        for index, call in enumerate(camera._sdk.calls)
        if call[0] == "StartPullModeWithCallback"
    )


def test_apply_settings_returns_final_sdk_dimensions(camera: ToupTekCamera) -> None:
    """Calculating dimensions locally can disagree with SDK rounding rules."""
    applied = camera.apply_settings(
        CaptureSettings(0.125, Roi(4, 6, 50, 40), 2, ReadoutMode.RAW8)
    )

    assert applied.duration_s == 0.125
    assert applied.width == 50
    assert applied.height == 40
    assert applied.dtype.name == "uint8"


@pytest.mark.parametrize(
    "settings",
    [
        CaptureSettings(0, Roi(0, 0, 64, 64), 1, ReadoutMode.RAW12),
        CaptureSettings(0.1, Roi(0, 0, 64, 64), 0, ReadoutMode.RAW12),
        CaptureSettings(0.1, Roi(0, 0, 64, 64), 9, ReadoutMode.RAW12),
        CaptureSettings(0.1, Roi(-1, 0, 64, 64), 1, ReadoutMode.RAW12),
        CaptureSettings(0.1, Roi(0, 0, 15, 64), 1, ReadoutMode.RAW12),
        CaptureSettings(0.1, Roi(0, 0, 4097, 64), 1, ReadoutMode.RAW12),
    ],
)
def test_invalid_settings_never_reach_the_sdk(
    camera: ToupTekCamera, settings: CaptureSettings
) -> None:
    """Invalid values must not leave the camera partially reconfigured."""
    with pytest.raises(CameraInvalidValueError):
        camera.apply_settings(settings)

    assert camera._sdk.calls == []


def test_configuration_is_rejected_while_trigger_is_running(camera: ToupTekCamera) -> None:
    """The SDK forbids changing acquisition options on a running camera."""
    handle = camera._handle
    handle.block_trigger = True

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            camera.acquire, 0.1, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW12
        )
        assert handle.trigger_started.wait(1)
        with pytest.raises(CameraBusyError):
            camera.apply_settings(
                CaptureSettings(0.2, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW8)
            )
        handle.trigger_release.set()
        pending.result(1)


def test_temperature_and_tec_values_are_converted_to_public_units(
    camera: ToupTekCamera,
) -> None:
    """SDK deci-degrees and deci-volts must not leak into the public API."""
    assert camera.temperature_c == -7.5
    assert camera.target_temperature_c == -10.0
    assert camera.cooler_on is True
    assert camera.tec_voltage() == (5.0, 6.0)
    assert camera.cooler_power_percent == pytest.approx(83.333333)

    camera.target_temperature_c = -8.5
    camera.cooler_on = False

    assert ("put_Temperature", -85) in camera._sdk.calls
    assert ("put_Option", OPTION_TEC, 0) in camera._sdk.calls
    assert camera._handle.options[OPTION_TECTARGET] == -85
    assert camera._handle.options[OPTION_TEC_VOLTAGE] == 50
    assert camera._handle.options[OPTION_TEC_VOLTAGE_MAX] == 60


def test_zero_maximum_tec_voltage_has_zero_cooler_power(camera: ToupTekCamera) -> None:
    """A zero maximum voltage must not cause division by zero."""
    camera._handle.options[OPTION_TEC_VOLTAGE_MAX] = 0

    assert camera.cooler_power_percent == 0.0


def test_temperature_is_busy_during_acquisition(camera: ToupTekCamera) -> None:
    handle = camera._handle
    handle.block_trigger = True
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            camera.acquire, 30.0, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW12
        )
        assert handle.trigger_started.wait(1)
        with pytest.raises(CameraBusyError):
            _ = camera.temperature_c
        camera.abort()
        with pytest.raises(Exception, match="aborted"):
            pending.result(1)


def test_disconnect_waits_for_temperature_native_lease(camera: ToupTekCamera) -> None:
    handle = camera._handle
    handle.block_temperature = True
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(lambda: camera.temperature_c)
        assert handle.temperature_entered.wait(1)
        disconnecting = pool.submit(camera.disconnect)
        with pytest.raises(FutureTimeoutError):
            disconnecting.result(0.05)
        assert ("Close",) not in camera._sdk.calls
        handle.temperature_release.set()
        assert reading.result(1) == -7.5
        disconnecting.result(1)
    assert camera._sdk.calls[-1] == ("Close",)


def test_tec_voltage_holds_one_lease_across_both_option_reads(
    camera: ToupTekCamera,
) -> None:
    handle = camera._handle
    handle.block_option = True
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(camera.tec_voltage)
        assert handle.option_entered.wait(1)
        disconnecting = pool.submit(camera.disconnect)
        with pytest.raises(FutureTimeoutError):
            disconnecting.result(0.05)
        handle.option_release.set()
        assert reading.result(1) == (5.0, 6.0)
        disconnecting.result(1)
    option_calls = [
        call for call in camera._sdk.calls if call[0] == "get_Option"
    ]
    assert option_calls[-2:] == [
        ("get_Option", OPTION_TEC_VOLTAGE),
        ("get_Option", OPTION_TEC_VOLTAGE_MAX),
    ]


def test_settings_require_a_connected_handle() -> None:
    """Settings on a probe-only object must fail before accessing the SDK."""
    camera = ToupTekCamera(
        "TP241025143218C92B01151AEC40CB7", sdk=FakeSdk()
    )

    with pytest.raises(Exception, match="not connected"):
        camera.apply_settings(
            CaptureSettings(0.1, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW12)
        )
