from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import time

import pytest

from test.hardware.conftest import abort_readiness_timeout_s
from touptek4040.camera_contract import ReadoutMode, Roi
from touptek4040.errors import CameraOperationCancelledError


@pytest.mark.hardware
def test_abort_discards_the_pending_real_frame(
    real_camera,
    hardware_evidence,
) -> None:
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            real_camera.acquire,
            1.0,
            Roi(0, 0, 64, 64),
            1,
            ReadoutMode.RAW12,
        )
        timeout_s = abort_readiness_timeout_s(real_camera)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with real_camera._state_lock:
                if real_camera._acquiring and real_camera._camera_started:
                    break
            time.sleep(0.002)
        else:
            real_camera.abort()
            pytest.fail("camera did not enter the native running state")

        real_camera.abort()
        with pytest.raises(CameraOperationCancelledError):
            pending.result(timeout=timeout_s)

    hardware_evidence.record(
        "abort",
        exposure_seconds=1.0,
        discarded=True,
        camera_started_after=False,
    )
