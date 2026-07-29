from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from threading import Event
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
    OPTION_HEAT,
    FLAG_HEAT,
)
from touptek4040.camera_contract import ReadoutMode, Roi
from touptek4040.errors import (
    CameraBusyError,
    CameraCleanupError,
    CameraError,
    CameraInvalidValueError,
    CameraNotConnectedError,
    CameraNotSupportedError,
    CameraOperationCancelledError,
    CameraTimeoutError,
)
from touptek4040.toupcam_camera import (
    CaptureSettings,
    ToupTekCamera,
    _software_bin_saturating,
)


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


@pytest.mark.parametrize(
    ("roi", "binning", "expected_sdk_roi", "expected_pixel"),
    [
        (Roi(1, 1, 1, 1), 1, (0, 0, 16, 16), 17),
        (Roi(1, 1, 1, 1), 3, (0, 0, 18, 18), 7),
        (Roi(4095, 4095, 1, 1), 1, (4080, 4080, 16, 16), 255),
    ],
)
def test_sdk_padding_and_software_crop_support_ascom_one_pixel_roi(
    camera: ToupTekCamera,
    roi: Roi,
    binning: int,
    expected_sdk_roi: tuple[int, int, int, int],
    expected_pixel: int,
) -> None:
    frame = camera.acquire(0.01, roi, binning, ReadoutMode.RAW12)

    assert frame.pixels.shape == (1, 1)
    assert int(frame.pixels[0, 0]) == expected_pixel
    assert ("put_Roi", *expected_sdk_roi) in camera._sdk.calls


@pytest.mark.parametrize(
    ("mode", "dtype", "maximum"),
    [
        (ReadoutMode.RAW12, np.dtype(np.uint16), 4095),
        (ReadoutMode.RAW8, np.dtype(np.uint8), 255),
    ],
)
def test_unrepresentable_native_bin_uses_crop_before_saturating_software_bin(
    mode: ReadoutMode,
    dtype: np.dtype,
    maximum: int,
) -> None:
    sdk = FakeSdk()
    sdk.Toupcam.handle.get_Resolution = lambda _index: (22, 22)
    camera = ToupTekCamera("TP241025143218C92B01151AEC40CB7", sdk=sdk)
    camera.connect()
    sdk.calls.clear()

    frame = camera.acquire(0.01, Roi(0, 0, 7, 7), 3, mode)

    assert frame.pixels.shape == (7, 7)
    assert frame.pixels.dtype == dtype
    assert int(frame.pixels.max()) <= maximum
    assert int(frame.pixels[0, 0]) == min(
        sum((y * 22 + x) % (maximum + 1) for y in range(3) for x in range(3)),
        maximum,
    )
    assert ("put_Option", sdk.TOUPCAM_OPTION_BINNING, 1) in sdk.calls
    assert ("put_Roi", 0, 0, 22, 22) in sdk.calls


def test_full_4096_bin3_fallback_has_exact_ascom_geometry_and_bounded_buffer(
    camera: ToupTekCamera,
) -> None:
    applied = camera.apply_settings(
        CaptureSettings(
            0.01,
            Roi(0, 0, 4096 // 3, 4096 // 3),
            3,
            ReadoutMode.RAW12,
        )
    )

    assert (applied.width, applied.height) == (1365, 1365)
    assert (applied.prebin_width, applied.prebin_height) == (4095, 4095)
    assert (applied.capture_width, applied.capture_height) == (4096, 4096)
    assert applied.hardware_binning == 1
    assert applied.software_binning == 3
    assert applied.capture_width * applied.capture_height * applied.dtype.itemsize == (
        32 * 1024 * 1024
    )


def test_odd_origin_fallback_crops_physical_pixels_before_binning() -> None:
    sdk = FakeSdk()
    sdk.Toupcam.handle.get_Resolution = lambda _index: (28, 28)
    camera = ToupTekCamera("TP241025143218C92B01151AEC40CB7", sdk=sdk)
    camera.connect()
    sdk.calls.clear()

    frame = camera.acquire(
        0.01,
        Roi(1, 0, 8, 7),
        3,
        ReadoutMode.RAW12,
    )

    assert frame.pixels.shape == (7, 8)
    assert ("put_Option", sdk.TOUPCAM_OPTION_BINNING, 1) in sdk.calls
    assert ("put_Roi", 2, 0, 26, 22) in sdk.calls
    assert int(frame.pixels[0, 0]) == sum(
        y * 26 + x for y in range(3) for x in range(1, 4)
    )


def test_software_bin_saturates_and_allocates_only_output_sized_accumulator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pixels = np.full((6, 6), 250, dtype=np.uint8)
    original_zeros = np.zeros
    allocations: list[tuple[tuple[int, int], np.dtype]] = []

    def tracked_zeros(
        shape: tuple[int, int],
        *,
        dtype: np.dtype,
    ) -> np.ndarray:
        allocations.append((shape, np.dtype(dtype)))
        return original_zeros(shape, dtype=dtype)

    monkeypatch.setattr(np, "zeros", tracked_zeros)

    result = _software_bin_saturating(pixels, 3, 255)

    assert np.array_equal(result, np.full((2, 2), 255, dtype=np.uint8))
    assert allocations == [((2, 2), np.dtype(np.uint64))]


def test_trigger_timeout_includes_sdk_exposure_margin(camera: ToupTekCamera) -> None:
    """A truncated timeout can expire before the SDK's documented margin."""
    camera.acquire(0.25, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW12)

    trigger_call = next(
        call for call in camera._sdk.calls if call[0] == "TriggerSyncV4"
    )
    assert trigger_call == ("TriggerSyncV4", 4255, 0, -1)


def test_identical_exposures_reuse_one_started_pull_stream(camera: ToupTekCamera) -> None:
    """The SDK reference keeps one stream running across synchronous triggers."""
    camera.acquire(0.01, Roi(0, 0, 32, 24), 1, ReadoutMode.RAW12)
    camera.acquire(0.01, Roi(0, 0, 32, 24), 1, ReadoutMode.RAW12)

    names = [call[0] for call in camera._sdk.calls]
    assert names.index("StartPullModeWithCallback") < names.index("TriggerSyncV4")
    assert names.count("StartPullModeWithCallback") == 1
    assert names.count("TriggerSyncV4") == 2
    assert names.count("Stop") == 0

    camera.disconnect()

    assert camera._sdk.calls[-2:] == [("Stop",), ("Close",)]


def test_reused_pull_stream_waits_for_empirical_trigger_recovery_window() -> None:
    sdk = FakeSdk()
    clock_values = iter((10.0, 10.005, 10.020))
    sleeps: list[float] = []
    camera = ToupTekCamera(
        "TP241025143218C92B01151AEC40CB7",
        sdk=sdk,
        monotonic=lambda: next(clock_values),
        sleeper=sleeps.append,
        trigger_recovery_s=0.015,
    )
    camera.connect()
    sdk.calls.clear()

    camera.acquire(0.01, Roi(0, 0, 32, 24), 1, ReadoutMode.RAW12)
    camera.acquire(0.01, Roi(0, 0, 32, 24), 1, ReadoutMode.RAW12)

    assert sleeps == [pytest.approx(0.010)]


def test_structural_change_stops_reconfigures_and_restarts(camera: ToupTekCamera) -> None:
    camera.acquire(0.01, Roi(0, 0, 32, 24), 1, ReadoutMode.RAW12)
    camera.acquire(0.02, Roi(2, 4, 32, 24), 2, ReadoutMode.RAW8)

    names = [call[0] for call in camera._sdk.calls]
    assert names.count("StartPullModeWithCallback") == 2
    assert names.count("TriggerSyncV4") == 2
    assert names.count("Stop") == 1
    assert names.count("Close") == 1
    assert names.count("Open") == 1
    stop_index = names.index("Stop")
    assert stop_index < max(
        index
        for index, call in enumerate(camera._sdk.calls)
        if call == ("put_Option", camera._sdk.TOUPCAM_OPTION_BITDEPTH, 0)
    )


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


def test_abort_does_not_enter_sdk_during_reconfiguration_stop(
    camera: ToupTekCamera,
) -> None:
    """Abort during non-triggering reconfiguration is an idle no-op."""
    handle = camera._handle
    camera.acquire(0.01, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW12)
    handle.block_stop = True

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            camera.acquire, 0.01, Roi(2, 2, 32, 32), 1, ReadoutMode.RAW8
        )
        assert handle.stop_entered.wait(1)
        camera.abort()
        handle.stop_release.set()
        pending.result(1)

    assert sum(call == ("Stop",) for call in camera._sdk.calls) == 1
    assert ("Trigger", 0) not in camera._sdk.calls


def test_abort_is_the_only_stop_claimant_during_a_trigger(
    camera: ToupTekCamera,
) -> None:
    handle = camera._handle
    handle.block_trigger = True
    with ThreadPoolExecutor(max_workers=1) as pool:
        exposure = pool.submit(
            camera.acquire, 30.0, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW12
        )
        assert handle.trigger_started.wait(1)
        camera.abort()
        with pytest.raises(CameraOperationCancelledError):
            exposure.result(1)

    assert sum(call == ("Stop",) for call in camera._sdk.calls) == 1


def test_new_acquire_waits_while_abort_stop_is_in_flight(
    camera: ToupTekCamera,
) -> None:
    """No next acquisition may enter the SDK before native Stop returns."""
    handle = camera._handle
    handle.block_trigger = True
    handle.block_stop = True

    with ThreadPoolExecutor(max_workers=3) as pool:
        exposure = pool.submit(
            camera.acquire, 30.0, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW12
        )
        assert handle.trigger_started.wait(1)
        cancellation = pool.submit(camera.abort)
        assert handle.stop_entered.wait(1)
        call_count = len(camera._sdk.calls)
        next_exposure = pool.submit(
            camera.acquire, 0.01, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW12
        )
        with pytest.raises(FutureTimeoutError):
            next_exposure.result(0.05)
        assert len(camera._sdk.calls) == call_count
        handle.block_trigger = False
        handle.stop_release.set()
        cancellation.result(1)
        with pytest.raises(CameraOperationCancelledError):
            exposure.result(1)
        next_exposure.result(1)


def test_disconnect_waits_for_abort_stop_and_all_native_calls(
    camera: ToupTekCamera,
) -> None:
    """Close must not race Stop or a blocked TriggerSyncV4."""
    handle = camera._handle
    handle.block_trigger = True
    handle.block_stop = True

    with ThreadPoolExecutor(max_workers=3) as pool:
        exposure = pool.submit(
            camera.acquire, 30.0, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW12
        )
        assert handle.trigger_started.wait(1)
        cancellation = pool.submit(camera.abort)
        assert handle.stop_entered.wait(1)
        disconnect = pool.submit(camera.disconnect)
        with pytest.raises(FutureTimeoutError):
            disconnect.result(0.05)
        assert ("Close",) not in camera._sdk.calls
        handle.stop_release.set()
        cancellation.result(1)
        with pytest.raises(CameraOperationCancelledError):
            exposure.result(1)
        disconnect.result(1)
    assert camera._sdk.calls[-1] == ("Close",)


def test_abort_before_final_commit_discards_the_built_frame(
    camera: ToupTekCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Abort wins until the final generation check is committed."""
    entered = Event()
    release = Event()
    original_commit = camera._commit_frame

    def blocked_commit(generation, frame):
        entered.set()
        assert release.wait(1)
        return original_commit(generation, frame)

    monkeypatch.setattr(camera, "_commit_frame", blocked_commit)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            camera.acquire, 0.01, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW12
        )
        assert entered.wait(1)
        camera.abort()
        release.set()
        with pytest.raises(CameraOperationCancelledError):
            pending.result(1)


def test_abort_after_final_commit_cannot_cancel_the_returned_frame(
    camera: ToupTekCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit point linearizes completion before Python returns."""
    committed = Event()
    release = Event()
    original_commit = camera._commit_frame

    def paused_after_commit(generation, frame):
        result = original_commit(generation, frame)
        committed.set()
        assert release.wait(1)
        return result

    monkeypatch.setattr(camera, "_commit_frame", paused_after_commit)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            camera.acquire, 0.01, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW12
        )
        assert committed.wait(1)
        camera.abort()
        release.set()
        assert pending.result(1).pixels.shape == (32, 32)


def test_stop_failure_marks_handle_unusable(camera: ToupTekCamera) -> None:
    """A failed Stop cannot transition the handle back to reusable idle."""
    camera.acquire(0.01, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW12)
    camera._handle.stop_error = FakeHResultError(E_FAIL)

    with pytest.raises(CameraError):
        camera.acquire(0.01, Roi(2, 2, 32, 32), 1, ReadoutMode.RAW8)
    call_count = len(camera._sdk.calls)
    with pytest.raises(CameraNotConnectedError, match="reconnect"):
        _ = camera.temperature_c
    assert len(camera._sdk.calls) == call_count


def test_abort_preserves_non_benign_trigger_cancel_error(
    camera: ToupTekCamera,
) -> None:
    """Only the observed idle E_UNEXPECTED may be ignored."""
    handle = camera._handle
    handle.block_trigger = True
    handle.cancel_error = FakeHResultError(E_FAIL)

    with ThreadPoolExecutor(max_workers=1) as pool:
        exposure = pool.submit(
            camera.acquire, 30.0, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW12
        )
        assert handle.trigger_started.wait(1)
        with pytest.raises(CameraError, match="cancel trigger"):
            camera.abort()
        with pytest.raises(CameraOperationCancelledError):
            exposure.result(1)


def test_disconnect_closes_handle_even_when_abort_reports_cleanup_error(
    camera: ToupTekCamera,
) -> None:
    handle = camera._handle
    handle.block_trigger = True
    handle.cancel_error = FakeHResultError(E_FAIL)
    with ThreadPoolExecutor(max_workers=2) as pool:
        exposure = pool.submit(
            camera.acquire, 30.0, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW12
        )
        assert handle.trigger_started.wait(1)
        disconnect = pool.submit(camera.disconnect)
        with pytest.raises(CameraError):
            disconnect.result(1)
        with pytest.raises(CameraOperationCancelledError):
            exposure.result(1)
    assert ("Close",) in camera._sdk.calls


def test_close_failure_quarantines_handle_until_a_retry_succeeds(
    camera: ToupTekCamera,
) -> None:
    handle = camera._handle
    handle.close_error = FakeHResultError(E_FAIL)

    with pytest.raises(CameraError, match="disconnect camera"):
        camera.disconnect()

    assert camera._handle is handle
    assert handle.closed is False
    with pytest.raises(CameraNotConnectedError):
        _ = camera.temperature_c

    handle.close_error = None
    camera.disconnect()

    assert camera._handle is None
    assert handle.closed is True
    assert camera._sdk.calls.count(("Close",)) == 2


def test_abort_dominates_post_trigger_python_failure(
    camera: ToupTekCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = Event()
    release = Event()

    def failing_frombuffer(*args, **kwargs):
        entered.set()
        assert release.wait(1)
        raise ValueError("copy failed")

    monkeypatch.setattr(np, "frombuffer", failing_frombuffer)
    with ThreadPoolExecutor(max_workers=1) as pool:
        exposure = pool.submit(
            camera.acquire, 0.01, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW12
        )
        assert entered.wait(1)
        camera.abort()
        release.set()
        with pytest.raises(CameraOperationCancelledError):
            exposure.result(1)


@pytest.mark.parametrize(
    "failure_kind", ["native", "dimensions", "copy"]
)
def test_abort_before_error_completion_linearization_wins(
    camera: ToupTekCamera,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    entered = Event()
    release = Event()
    original_complete = camera._complete_error

    def blocked_complete(generation, error):
        entered.set()
        assert release.wait(1)
        return original_complete(generation, error)

    monkeypatch.setattr(camera, "_complete_error", blocked_complete)
    if failure_kind == "native":
        camera._handle.trigger_error = FakeHResultError(E_TIMEOUT)
    elif failure_kind == "dimensions":
        camera._handle.frame_info_size_override = (30, 32)
    else:
        monkeypatch.setattr(
            np, "frombuffer", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("copy"))
        )
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            camera.acquire, 0.01, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW12
        )
        assert entered.wait(1)
        camera.abort()
        release.set()
        with pytest.raises(CameraOperationCancelledError):
            pending.result(1)


def test_abort_after_error_completion_linearization_cannot_replace_error(
    camera: ToupTekCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    committed = Event()
    release = Event()
    original_complete = camera._complete_error

    def paused_after_complete(generation, error):
        result = original_complete(generation, error)
        committed.set()
        assert release.wait(1)
        return result

    monkeypatch.setattr(camera, "_complete_error", paused_after_complete)
    camera._handle.trigger_error = FakeHResultError(E_TIMEOUT)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            camera.acquire, 0.01, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW12
        )
        assert committed.wait(1)
        camera.abort()
        release.set()
        with pytest.raises(CameraTimeoutError):
            pending.result(1)


def test_active_abort_retains_unexpected_trigger_error(
    camera: ToupTekCamera,
) -> None:
    handle = camera._handle
    handle.block_trigger = True
    handle.cancel_error = FakeHResultError(E_UNEXPECTED)
    with ThreadPoolExecutor(max_workers=1) as pool:
        exposure = pool.submit(
            camera.acquire, 30.0, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW12
        )
        assert handle.trigger_started.wait(1)
        with pytest.raises(CameraBusyError, match="cancel trigger"):
            camera.abort()
        with pytest.raises(CameraOperationCancelledError):
            exposure.result(1)


def test_disconnect_combines_abort_cleanup_and_close_errors(
    camera: ToupTekCamera,
) -> None:
    handle = camera._handle
    handle.block_trigger = True
    handle.cancel_error = FakeHResultError(E_FAIL)
    handle.close_error = FakeHResultError(E_FAIL)
    with ThreadPoolExecutor(max_workers=2) as pool:
        exposure = pool.submit(
            camera.acquire, 30.0, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW12
        )
        assert handle.trigger_started.wait(1)
        disconnect = pool.submit(camera.disconnect)
        with pytest.raises(CameraCleanupError) as raised:
            disconnect.result(1)
        with pytest.raises(CameraOperationCancelledError):
            exposure.result(1)
    assert len(raised.value.errors) == 2
    assert ("Close",) in camera._sdk.calls


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


def test_cancelled_start_control_never_reaches_native_acquisition(
    camera: ToupTekCamera,
) -> None:
    class CancelledControl:
        def commit_start(self, claim):
            del claim
            return False

    with pytest.raises(CameraOperationCancelledError):
        camera.acquire(
            0.01,
            Roi(0, 0, 32, 32),
            1,
            ReadoutMode.RAW12,
            control=CancelledControl(),
        )

    assert not any(
        call[0] in {"StartPullModeWithCallback", "TriggerSyncV4"}
        for call in camera._sdk.calls
    )

@pytest.mark.parametrize("operation", ["read", "write"])
def test_heater_operations_are_serialized_behind_an_active_exposure(
    camera: ToupTekCamera, operation: str
) -> None:
    camera.identity = camera.identity.__class__(
        **{**camera.identity.__dict__, "model_flags": FLAG_HEAT}
    )
    handle = camera._handle
    handle.block_trigger = True

    with ThreadPoolExecutor(max_workers=2) as pool:
        exposure = pool.submit(
            camera.acquire, 30.0, Roi(0, 0, 32, 32), 1, ReadoutMode.RAW12
        )
        assert handle.trigger_started.wait(1)
        with pytest.raises(CameraBusyError):
            if operation == "read":
                _ = camera.heater_level
            else:
                camera.heater_level = 1
        assert not any(
            call[:2] == ("put_Option", OPTION_HEAT)
            for call in camera._sdk.calls
        )
        camera.abort()
        with pytest.raises(CameraOperationCancelledError):
            exposure.result(1)
