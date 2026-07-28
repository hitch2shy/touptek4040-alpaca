import numpy as np
import pytest

from touptek4040.camera_contract import ReadoutMode, Roi
from touptek4040.errors import CameraInvalidValueError, CameraNotConnectedError
from touptek4040.sim_camera import SimCamera


def test_simulator_reports_approved_capabilities():
    cam = SimCamera(serial="SIM-4040")
    caps = cam.capabilities

    assert (caps.width, caps.height) == (4096, 4096)
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
        (Roi(0, 0, 63, 32), 2),
    ],
)
def test_simulator_rejects_invalid_acquisition_geometry(roi: Roi, binning: int):
    cam = SimCamera(serial="SIM-4040")

    with pytest.raises(CameraInvalidValueError):
        cam.acquire(0.001, roi, binning, ReadoutMode.RAW12)
