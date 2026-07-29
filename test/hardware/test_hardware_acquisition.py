from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import time

import numpy as np
import pytest

from touptek4040.camera_contract import ReadoutMode, Roi
from touptek4040.errors import CameraOperationCancelledError
from touptek4040.toupcam_camera import ToupTekCamera


pytestmark = pytest.mark.skipif(
    os.environ.get("TOUPTEK_HARDWARE_TEST") != "1",
    reason="set TOUPTEK_HARDWARE_TEST=1 to access attached hardware",
)


@pytest.mark.parametrize(
    ("mode", "binning", "dtype", "maximum"),
    [
        (ReadoutMode.RAW12, 1, np.dtype(np.uint16), 4095),
        (ReadoutMode.RAW8, 2, np.dtype(np.uint8), 255),
    ],
)
def test_short_software_trigger_matrix(
    real_camera: ToupTekCamera,
    mode: ReadoutMode,
    binning: int,
    dtype: np.dtype,
    maximum: int,
) -> None:
    frame = real_camera.acquire(0.01, Roi(0, 0, 64, 64), binning, mode)

    assert frame.pixels.shape == (64, 64)
    assert frame.pixels.dtype == dtype
    assert int(frame.pixels.max()) <= maximum
    assert -100.0 < real_camera.temperature_c < 100.0
    assert 0.0 <= real_camera.cooler_power_percent <= 100.0


def test_real_trigger_can_be_aborted_without_publishing_a_frame(
    real_camera: ToupTekCamera,
) -> None:
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            real_camera.acquire,
            1.0,
            Roi(0, 0, 64, 64),
            1,
            ReadoutMode.RAW12,
        )
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with real_camera._state_lock:
                if real_camera._acquiring and real_camera._camera_started:
                    break
            time.sleep(0.001)
        else:
            pytest.fail("camera did not enter its triggerable running state")
        real_camera.abort()
        with pytest.raises(CameraOperationCancelledError):
            pending.result(1)
