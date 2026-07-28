from dataclasses import FrozenInstanceError
from datetime import datetime

import numpy as np
import pytest

from touptek4040.camera_contract import CameraCapabilities, CameraContract, Frame, ReadoutMode, Roi
from touptek4040.sim_camera import SimCamera


def test_capabilities_describe_the_supported_sensor_operations():
    caps = CameraCapabilities(
        width=4096,
        height=4096,
        readout_modes=("RAW 12-bit", "RAW 8-bit"),
        max_bin=8,
        can_abort=True,
        can_stop=False,
    )

    assert caps.width == 4096
    assert caps.height == 4096
    assert caps.readout_modes == ("RAW 12-bit", "RAW 8-bit")
    assert caps.max_bin == 8
    assert caps.can_abort is True
    assert caps.can_stop is False


def test_contract_values_are_immutable():
    frame = Frame(
        pixels=np.zeros((1, 1), dtype=np.uint16),
        started_utc=datetime.now(),
        duration_s=0.1,
        binning=1,
        roi=Roi(0, 0, 1, 1),
        readout_mode=ReadoutMode.RAW12,
    )

    with pytest.raises(FrozenInstanceError):
        frame.duration_s = 0.2


def test_simulator_implements_camera_contract():
    assert isinstance(SimCamera(serial="SIM-4040"), CameraContract)
