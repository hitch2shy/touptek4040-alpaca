from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timezone

import numpy as np
import pytest

from test.fixtures.fake_toupcam import (
    E_BUSY,
    E_FAIL,
    E_GEN_FAILURE,
    E_INVALIDARG,
    E_NOTIMPL,
    E_TIMEOUT,
    E_UNEXPECTED,
    FakeHResultError,
    FakeSdk,
)
from touptek4040.camera_contract import ReadoutMode, Roi
from touptek4040.errors import (
    CameraBusyError,
    CameraError,
    CameraInvalidValueError,
    CameraNotConnectedError,
    CameraNotSupportedError,
    CameraOperationCancelledError,
    CameraTimeoutError,
)
from touptek4040.toupcam_camera import ToupTekCamera


@pytest.fixture
def camera() -> ToupTekCamera:
    sdk = FakeSdk()
    result = ToupTekCamera("TP241025143218C92B01151AEC40CB7", sdk=sdk)
    result.connect()
    sdk.calls.clear()
    return result


@pytest.mark.parametrize(
    ("mode", "dtype", "maximum"),
    [
        (ReadoutMode.RAW12, np.dtype(np.uint16), 4095),
        (ReadoutMode.RAW8, np.dtype(np.uint8), 255),
    ],
)
def test_triggered_frame_has_final_shape_dtype_and_metadata(
    camera: ToupTekCamera,
    mode: ReadoutMode,
    dtype: np.dtype,
    maximum: int,
) -> None:
    """Wrong buffer geometry or dtype corrupts the returned RAW frame."""
    frame = camera.acquire(0.025, Roi(2, 4, 32, 24), 2, mode)

    assert frame.pixels.shape == (24, 32)
    assert frame.pixels.dtype == dtype
    assert int(frame.pixels.max()) <= maximum
    assert frame.started_utc.tzinfo is timezone.utc
    assert frame.duration_s == 0.025
    assert frame.roi == Roi(2, 4, 32, 24)
    assert frame.binning == 2
    assert frame.readout_mode is mode


def test_trigger_timeout_includes_sdk_exposure_margin(camera: ToupTekCamera) -> None:
    """A truncated timeout can expire before the SDK's documented margin."""
    camera.acquire(0.25, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW12)

    trigger_call = next(
        call for call in camera._sdk.calls if call[0] == "TriggerSyncV4"
    )
    assert trigger_call == ("TriggerSyncV4", 4255, 0, -1)


def test_camera_runs_only_around_the_synchronous_trigger(camera: ToupTekCamera) -> None:
    """Missing Stop would make the next settings change violate SDK state rules."""
    camera.acquire(0.01, Roi(0, 0, 32, 24), 1, ReadoutMode.RAW12)

    names = [call[0] for call in camera._sdk.calls]
    assert names.index("StartPullModeWithCallback") < names.index("TriggerSyncV4")
    assert names.index("TriggerSyncV4") < names.index("Stop")


def test_camera_is_stopped_when_trigger_fails(camera: ToupTekCamera) -> None:
    """An SDK error must not strand the camera in its running state."""
    camera._handle.trigger_error = FakeHResultError(E_TIMEOUT)

    with pytest.raises(CameraTimeoutError):
        camera.acquire(0.01, Roi(0, 0, 32, 24), 1, ReadoutMode.RAW12)

    assert camera._sdk.calls[-1] == ("Stop",)
    assert camera._handle.started is False


def test_frame_info_dimension_mismatch_is_rejected(camera: ToupTekCamera) -> None:
    """A short or stale SDK frame must never be published with requested metadata."""
    camera._handle.frame_info_size_override = (30, 24)

    with pytest.raises(CameraError, match="dimensions"):
        camera.acquire(0.01, Roi(0, 0, 32, 24), 1, ReadoutMode.RAW12)


def test_abort_cancels_trigger_and_discards_frame(camera: ToupTekCamera) -> None:
    """Cancel must unblock acquisition and make its result unusable."""
    handle = camera._handle
    handle.block_trigger = True

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            camera.acquire, 30.0, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW12
        )
        assert handle.trigger_started.wait(1)
        camera.abort()
        with pytest.raises(CameraOperationCancelledError):
            pending.result(1)

    assert ("Trigger", 0) in camera._sdk.calls
    assert sum(call == ("Stop",) for call in camera._sdk.calls) == 1


def test_abort_generation_discards_a_late_successful_frame(camera: ToupTekCamera) -> None:
    """Even a successful SDK return after cancel belongs to the old generation."""
    handle = camera._handle
    handle.block_trigger = True
    handle.deliver_after_abort = True

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            camera.acquire, 30.0, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW12
        )
        assert handle.trigger_started.wait(1)
        camera.abort()
        with pytest.raises(CameraOperationCancelledError):
            pending.result(1)


def test_abort_during_start_prevents_the_trigger(camera: ToupTekCamera) -> None:
    """Abort in the start transition must not launch an uncancellable exposure."""
    handle = camera._handle
    handle.block_start = True

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            camera.acquire, 30.0, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW12
        )
        assert handle.start_entered.wait(1)
        camera.abort()
        handle.start_release.set()
        with pytest.raises(CameraOperationCancelledError):
            pending.result(1)

    assert not any(call[0] == "TriggerSyncV4" for call in camera._sdk.calls)
    assert ("Trigger", 0) not in camera._sdk.calls
    assert ("Stop",) in camera._sdk.calls


def test_abort_is_an_idle_noop(camera: ToupTekCamera) -> None:
    """An idle camera has no software trigger to cancel."""
    camera.abort()

    assert ("Trigger", 0) not in camera._sdk.calls


def test_abort_does_not_enter_sdk_during_normal_stop(camera: ToupTekCamera) -> None:
    """Normal completion and Abort must never call Stop concurrently."""
    handle = camera._handle
    handle.block_stop = True

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            camera.acquire, 0.01, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW12
        )
        assert handle.stop_entered.wait(1)
        camera.abort()
        handle.stop_release.set()
        with pytest.raises(CameraOperationCancelledError):
            pending.result(1)

    assert sum(call == ("Stop",) for call in camera._sdk.calls) == 1
    assert ("Trigger", 0) not in camera._sdk.calls


def test_a_second_acquisition_is_busy(camera: ToupTekCamera) -> None:
    """Two TriggerSyncV4 calls must never share one hardware handle."""
    handle = camera._handle
    handle.block_trigger = True

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            camera.acquire, 30.0, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW12
        )
        assert handle.trigger_started.wait(1)
        with pytest.raises(CameraBusyError):
            camera.acquire(0.1, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW12)
        camera.abort()
        with pytest.raises(CameraOperationCancelledError):
            pending.result(1)


@pytest.mark.parametrize(
    ("hr", "error_type"),
    [
        (E_INVALIDARG, CameraInvalidValueError),
        (E_NOTIMPL, CameraNotSupportedError),
        (E_BUSY, CameraBusyError),
        (E_UNEXPECTED, CameraBusyError),
        (E_TIMEOUT, CameraTimeoutError),
        (E_GEN_FAILURE, CameraNotConnectedError),
        (E_FAIL, CameraError),
    ],
)
def test_sdk_hresult_is_translated_to_internal_error(
    camera: ToupTekCamera, hr: int, error_type: type[CameraError]
) -> None:
    """Native wrapper exceptions must not leak across the camera contract."""
    camera._handle.trigger_error = FakeHResultError(hr)

    with pytest.raises(error_type):
        camera.acquire(0.01, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW12)


def test_acquisition_requires_connection() -> None:
    """A disconnected adapter must not dereference a stale handle."""
    camera = ToupTekCamera(
        "TP241025143218C92B01151AEC40CB7", sdk=FakeSdk()
    )

    with pytest.raises(CameraNotConnectedError):
        camera.acquire(0.01, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW12)
