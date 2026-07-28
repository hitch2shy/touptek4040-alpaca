import numpy as np
import pytest

from touptek4040.camera_contract import ReadoutMode, Roi
from touptek4040.errors import (
    CameraInvalidValueError,
    CameraNotConnectedError,
    CameraOperationCancelledError,
)
from touptek4040.sim_camera import SimCamera


def test_simulator_reports_approved_capabilities():
    cam = SimCamera(serial="SIM-4040")
    caps = cam.capabilities

    assert (caps.width, caps.height) == (4096, 4096)
    assert (caps.pixel_size_x, caps.pixel_size_y) == (9.0, 9.0)
    assert caps.readout_modes == ("RAW 12-bit", "RAW 8-bit")
    assert caps.max_bin == 8
    assert caps.can_abort is True
    assert caps.can_stop is False


def test_simulator_raw12_roi_is_uint16():
    cam = SimCamera(serial="SIM-4040")

    frame = cam.acquire(0.001, Roi(0, 0, 64, 32), 1, ReadoutMode.RAW12)

    assert frame.pixels.shape == (32, 64)
    assert frame.pixels.dtype == np.uint16
    assert int(frame.pixels.max()) <= 4095


def test_simulator_raw8_roi_is_uint8():
    cam = SimCamera(serial="SIM-4040")

    frame = cam.acquire(0.001, Roi(0, 0, 64, 32), 1, ReadoutMode.RAW8)

    assert frame.pixels.shape == (32, 64)
    assert frame.pixels.dtype == np.uint8
    assert int(frame.pixels.max()) <= 255


def test_simulator_binning_sums_raw12_blocks_and_saturates():
    cam = SimCamera(serial="SIM-4040")

    summed = cam.acquire(0.001, Roi(0, 0, 2, 1), 2, ReadoutMode.RAW12)
    saturated = cam.acquire(0.001, Roi(2047, 2047, 1, 1), 2, ReadoutMode.RAW12)

    np.testing.assert_array_equal(summed.pixels, np.array([[4, 12]], dtype=np.uint16))
    np.testing.assert_array_equal(saturated.pixels, np.array([[4095]], dtype=np.uint16))


def test_simulator_binning_sums_raw8_blocks_and_saturates():
    cam = SimCamera(serial="SIM-4040")

    summed = cam.acquire(0.001, Roi(0, 0, 2, 1), 2, ReadoutMode.RAW8)
    saturated = cam.acquire(0.001, Roi(127, 127, 1, 1), 2, ReadoutMode.RAW8)

    np.testing.assert_array_equal(summed.pixels, np.array([[4, 12]], dtype=np.uint8))
    np.testing.assert_array_equal(saturated.pixels, np.array([[255]], dtype=np.uint8))


def test_disconnected_simulator_rejects_acquisition():
    cam = SimCamera(serial="SIM-4040")
    cam.disconnect()

    with pytest.raises(CameraNotConnectedError):
        cam.acquire(0.001, Roi(0, 0, 64, 32), 1, ReadoutMode.RAW12)


@pytest.mark.parametrize(
    ("roi", "binning"),
    [
        (Roi(0, 0, 64, 32), 0),
        (Roi(4090, 0, 64, 32), 1),
        (Roi(2040, 0, 64, 32), 2),
    ],
)
def test_simulator_rejects_invalid_acquisition_geometry(roi: Roi, binning: int):
    cam = SimCamera(serial="SIM-4040")

    with pytest.raises(CameraInvalidValueError):
        cam.acquire(0.001, roi, binning, ReadoutMode.RAW12)


def test_simulator_roi_is_binned_output_geometry():
    frame = SimCamera("SIM-4040").acquire(
        0.001, Roi(3, 4, 5, 6), 3, ReadoutMode.RAW12
    )
    assert frame.pixels.shape == (6, 5)


def test_simulator_has_deterministic_cooling_contract():
    camera = SimCamera("SIM-4040")
    assert camera.temperature_c == 20.0
    assert camera.target_temperature_c == 0.0
    assert camera.cooler_on is False
    assert camera.tec_voltage() == (0.0, 6.0)
    assert camera.cooler_power_percent == 0.0
    camera.target_temperature_c = -10.0
    camera.cooler_on = True
    assert camera.target_temperature_c == -10.0
    assert camera.cooler_on is True
    assert camera.tec_voltage() == (6.0, 6.0)
    assert camera.cooler_power_percent == 100.0


def test_simulator_gain_round_trips_and_is_recorded_in_the_frame():
    camera = SimCamera("SIM-4040")

    camera.gain = 1650
    frame = camera.acquire(
        0.001,
        Roi(0, 0, 16, 16),
        1,
        ReadoutMode.RAW12,
    )

    assert camera.gain == 1650
    assert frame.gain == 1650


@pytest.mark.parametrize("gain", [99, 1651, 1.5, True])
def test_simulator_rejects_gain_outside_the_approved_numeric_range(gain):
    camera = SimCamera("SIM-4040")

    with pytest.raises(CameraInvalidValueError):
        camera.gain = gain


def test_simulator_does_not_acquire_when_start_control_was_cancelled():
    camera = SimCamera("SIM-4040")

    class CancelledControl:
        def commit_start(self, claim):
            del claim
            return False

    with pytest.raises(CameraOperationCancelledError):
        camera.acquire(
            0.001,
            Roi(0, 0, 16, 16),
            1,
            ReadoutMode.RAW12,
            control=CancelledControl(),
        )
