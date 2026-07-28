from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import time

import numpy as np
import pytest

from touptek4040.camera_contract import ReadoutMode, Roi
from touptek4040.errors import CameraOperationCancelledError
from touptek4040.toupcam_camera import ToupTekCamera, enumerate_cameras
from touptek4040.toupcam_loader import load_toupcam_module


pytestmark = pytest.mark.skipif(
    os.environ.get("TOUPTEK_HARDWARE_TEST") != "1",
    reason="set TOUPTEK_HARDWARE_TEST=1 to access attached hardware",
)


@pytest.fixture
def real_camera():
    sdk = load_toupcam_module(Path(__file__).resolve().parents[2])
    identity = enumerate_cameras(sdk)[0]
    camera = ToupTekCamera(identity.serial, sdk=sdk)
    camera.connect()
    handle = None
    saved = None
    try:
        handle = camera._handle
        saved = {
            "raw": handle.get_Option(sdk.TOUPCAM_OPTION_RAW),
            "bitdepth": handle.get_Option(sdk.TOUPCAM_OPTION_BITDEPTH),
            "gain": handle.get_ExpoAGain(),
            "roi": handle.get_Roi(),
            "binning": handle.get_Option(sdk.TOUPCAM_OPTION_BINNING),
            "trigger": handle.get_Option(sdk.TOUPCAM_OPTION_TRIGGER),
            "tec_target": handle.get_Option(sdk.TOUPCAM_OPTION_TECTARGET),
            "tec": handle.get_Option(sdk.TOUPCAM_OPTION_TEC),
            "fan": handle.get_Option(sdk.TOUPCAM_OPTION_FAN),
        }
        yield camera
    finally:
        restore_errors: list[Exception] = []
        try:
            if handle is not None and saved is not None:
                try:
                    handle.Trigger(0)
                except sdk.HRESULTException as error:
                    if (error.hr & 0xFFFFFFFF) != 0x8000FFFF:
                        restore_errors.append(error)
                restorations = (
                    lambda: handle.put_Option(
                        sdk.TOUPCAM_OPTION_TRIGGER, saved["trigger"]
                    ),
                    lambda: handle.put_Option(sdk.TOUPCAM_OPTION_RAW, saved["raw"]),
                    lambda: handle.put_Option(
                        sdk.TOUPCAM_OPTION_BITDEPTH, saved["bitdepth"]
                    ),
                    lambda: handle.put_Option(
                        sdk.TOUPCAM_OPTION_BINNING, saved["binning"]
                    ),
                    lambda: handle.put_Roi(*saved["roi"]),
                    lambda: handle.put_ExpoAGain(saved["gain"]),
                    lambda: handle.put_Temperature(saved["tec_target"]),
                    lambda: handle.put_Option(sdk.TOUPCAM_OPTION_TEC, saved["tec"]),
                    lambda: handle.put_Option(sdk.TOUPCAM_OPTION_FAN, saved["fan"]),
                )
                for restore in restorations:
                    try:
                        restore()
                    except Exception as error:
                        restore_errors.append(error)
        finally:
            camera.disconnect()
        if restore_errors:
            raise ExceptionGroup("camera setting restoration failed", restore_errors)


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
                if real_camera._camera_started:
                    break
            time.sleep(0.001)
        else:
            pytest.fail("camera did not enter its triggerable running state")
        real_camera.abort()
        with pytest.raises(CameraOperationCancelledError):
            pending.result(1)
