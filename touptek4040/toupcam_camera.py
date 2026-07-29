"""ToupTek discovery, settings, cooling, and synchronous acquisition."""

from __future__ import annotations

import ctypes
import math
import sys
import time
from contextlib import contextmanager
from ctypes import ArgumentError
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Condition, Lock, RLock
from types import ModuleType
from typing import Any

import numpy as np

from .camera_contract import (
    AcquisitionControl,
    CameraCapabilities,
    Frame,
    ReadoutMode,
    Roi,
)
from .errors import (
    CameraBusyError,
    CameraCleanupError,
    CameraError,
    CameraInvalidValueError,
    CameraNotConnectedError,
    CameraNotSupportedError,
    CameraOperationCancelledError,
    CameraTimeoutError,
    safe_camera_error,
    safe_exception_text,
)
from .toupcam_loader import load_default_toupcam_module


@dataclass(frozen=True)
class CaptureSettings:
    """Requested output geometry and exposure settings."""

    duration_s: float
    roi: Roi
    binning: int
    readout_mode: ReadoutMode


@dataclass(frozen=True)
class AppliedSettings:
    """SDK-confirmed output geometry and storage type."""

    duration_s: float
    width: int
    height: int
    dtype: np.dtype
    gain: int
    capture_width: int
    capture_height: int
    crop_x: int
    crop_y: int
    prebin_width: int
    prebin_height: int
    hardware_binning: int
    software_binning: int


@dataclass(frozen=True)
class CameraIdentity:
    """A hardware identity and its immutable, read-only characteristics."""

    device_id: str
    display_name: str
    model: str
    serial: str
    capabilities: CameraCapabilities
    firmware: str
    hardware: str
    production_date: str
    fpga: str
    model_flags: int
    exposure_time_range_us: tuple[int, int, int]
    gain_range: tuple[int, int, int]
    tec_target_range_deci_c: tuple[int, int] | None


@dataclass(frozen=True)
class _PersistentNativeState:
    """Live settings that a fresh SDK handle does not inherit."""

    gain: int
    exposure_us: int
    auto_exposure: bool
    tec_target_deci_c: int
    tec_enabled: int
    fan: int
    rotate: int
    tec_voltage_max: int
    hflip: bool
    vflip: bool
    heater_max_level: int | None
    heater_level: int | None


def enumerate_cameras(sdk: ModuleType | Any | None = None) -> list[CameraIdentity]:
    """Open every enumerated camera briefly to read its serial and capabilities."""
    active_sdk = sdk if sdk is not None else load_default_toupcam_module()
    identities: list[CameraIdentity] = []
    for device in active_sdk.Toupcam.EnumV2():
        handle = active_sdk.Toupcam.Open(device.id)
        if handle is None:
            continue
        try:
            identities.append(_identity_from_handle(device, handle))
        finally:
            handle.Close()
    return identities


class ToupTekCamera:
    """Stable ToupTek selection with one synchronized hardware handle."""

    def __init__(
        self,
        serial: str | None,
        sdk: ModuleType | Any | None = None,
        *,
        monotonic: Any = time.monotonic,
        sleeper: Any = time.sleep,
        trigger_recovery_s: float = 0.015,
    ) -> None:
        if (
            isinstance(trigger_recovery_s, bool)
            or not isinstance(trigger_recovery_s, (int, float))
            or not math.isfinite(trigger_recovery_s)
            or trigger_recovery_s < 0
        ):
            raise CameraInvalidValueError(
                "trigger_recovery_s must be a finite non-negative number"
            )
        self._sdk = sdk if sdk is not None else load_default_toupcam_module()
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._trigger_recovery_s = float(trigger_recovery_s)
        self._last_trigger_completed_monotonic: float | None = None
        identities = enumerate_cameras(self._sdk)
        if serial is None:
            if len(identities) != 1:
                raise CameraInvalidValueError("serial is required unless exactly one camera is attached")
            self.identity = identities[0]
        else:
            self.identity = next((item for item in identities if item.serial == serial), None)
            if self.identity is None:
                raise CameraNotConnectedError(f"camera with serial {serial!r} is not attached")
        self.serial = self.identity.serial
        self._handle: Any | None = None
        self._state_lock = RLock()
        self._condition = Condition(self._state_lock)
        self._acquisition_lock = Lock()
        self._native_calls = 0
        self._idle_native_active = False
        self._disconnecting = False
        self._unusable = False
        self._acquisition_configuring = False
        self._acquiring = False
        self._frame_committed = False
        self._camera_started = False
        self._stop_claimed = False
        self._stop_in_flight = False
        self._cancel_requested = False
        self._abort_generation = 0
        self._configured_structure: tuple[
            tuple[int, int, int, int], int, ReadoutMode
        ] | None = None

    @property
    def capabilities(self) -> CameraCapabilities:
        return self.identity.capabilities

    def connect(self) -> None:
        with self._condition:
            if self._handle is not None:
                if self._disconnecting or self._unusable:
                    raise CameraNotConnectedError(
                        "camera owner is quarantined until disconnect succeeds"
                    )
                return
            try:
                handle = self._sdk.Toupcam.Open(f"sn:{self.serial}")
            except BaseException as error:
                raise _translate_sdk_error(error, "connect camera") from error
            if handle is None:
                raise CameraNotConnectedError(
                    f"could not open camera with serial {self.serial!r}"
                )
            self._handle = handle
            self._unusable = False
            self._disconnecting = False
            self._configured_structure = None
            self._camera_started = False
            self._last_trigger_completed_monotonic = None

    def disconnect(self) -> None:
        with self._condition:
            if self._handle is None:
                return
            self._disconnecting = True
            acquiring = self._acquiring and not self._frame_committed
        abort_error: CameraError | None = None
        if acquiring:
            try:
                self.abort()
            except CameraError as error:
                abort_error = error
        with self._condition:
            while self._acquiring or self._native_calls or self._stop_in_flight:
                self._condition.wait()
            handle = self._handle
        stream_stop_error = self._stop_persistent_stream()
        with self._condition:
            while self._native_calls or self._stop_in_flight:
                self._condition.wait()
            handle = self._handle
        close_error: CameraError | None = None
        if handle is not None:
            try:
                handle.Close()
            except BaseException as error:
                close_error = _translate_sdk_error(error, "disconnect camera")
        with self._condition:
            if close_error is None and self._handle is handle:
                self._handle = None
                self._disconnecting = False
                self._unusable = False
                self._configured_structure = None
            elif close_error is not None:
                self._disconnecting = True
                self._unusable = True
            self._condition.notify_all()
        disconnect_errors = tuple(
            error
            for error in (abort_error, stream_stop_error, close_error)
            if error is not None
        )
        if len(disconnect_errors) > 1:
            raise CameraCleanupError(
                "abort cleanup and camera close both failed", disconnect_errors
            )
        if disconnect_errors:
            raise disconnect_errors[0]

    def apply_settings(self, settings: CaptureSettings) -> AppliedSettings:
        """Validate and configure every non-running acquisition option."""
        with self._native_lease() as handle:
            return self._apply_settings_locked(handle, settings)

    def acquire(
        self,
        duration_s: float,
        roi: Roi,
        binning: int,
        readout_mode: ReadoutMode,
        control: AcquisitionControl | None = None,
    ) -> Frame:
        """Issue one synchronous software trigger and return its private frame."""
        waited_for_stop = False
        with self._condition:
            while self._stop_in_flight:
                waited_for_stop = True
                self._condition.wait()
        if waited_for_stop:
            self._acquisition_lock.acquire()
        elif not self._acquisition_lock.acquire(blocking=False):
            raise CameraBusyError("another acquisition is already running")
        try:
            settings = CaptureSettings(duration_s, roi, binning, readout_mode)
            with self._condition:
                self._require_connected_locked()
            with self._native_lease(during_acquisition=True) as handle:
                applied = self._apply_settings_locked(handle, settings)
                with self._condition:
                    if self._disconnecting:
                        raise CameraNotConnectedError("camera is disconnecting")
                    handle = self._require_connected_locked()
                buffer = _allocate_buffer(applied)
                frame_info = self._sdk.ToupcamFrameInfoV4()
                timeout_ms = math.ceil(applied.duration_s * 1000 * 1.02 + 4000)
                claim_state: dict[str, int] = {}
                self._pace_reused_trigger()

                def claim() -> None:
                    with self._condition:
                        if self._disconnecting:
                            raise CameraNotConnectedError(
                                "camera is disconnecting"
                            )
                        self._require_connected_locked()
                        claim_state["generation"] = self._abort_generation
                        self._acquisition_configuring = False
                        self._acquiring = True
                        self._frame_committed = False
                        self._cancel_requested = False
                        self._stop_claimed = False

                if control is None:
                    claim()
                elif not control.commit_start(claim):
                    raise CameraOperationCancelledError(
                        "exposure was cancelled before native acquisition started"
                    )
                generation = claim_state["generation"]
                operation_errors: list[CameraError] = []
                try:
                    with self._condition:
                        cancelled = (
                            generation != self._abort_generation
                            or self._disconnecting
                        )
                        stream_started = self._camera_started
                    started_stream = not cancelled and not stream_started
                    if started_stream:
                        handle.StartPullModeWithCallback(None, None)
                    with self._condition:
                        if started_stream:
                            self._camera_started = True
                        cancelled = (
                            generation != self._abort_generation
                            or self._disconnecting
                        )
                    if not cancelled:
                        with self._condition:
                            cancelled = (
                                generation != self._abort_generation
                                or self._disconnecting
                            )
                        if not cancelled:
                            started_utc = datetime.now(timezone.utc)
                            handle.TriggerSyncV4(
                                timeout_ms,
                                buffer,
                                0,
                                -1,
                                frame_info,
                            )
                            self._last_trigger_completed_monotonic = (
                                self._monotonic()
                            )
                except BaseException as error:
                    operation_errors.append(_translate_sdk_error(error, "acquire frame"))
                with self._condition:
                    must_stop = (
                        bool(operation_errors)
                        or generation != self._abort_generation
                    )
                if must_stop:
                    stop_error = self._stop_if_claimed_by_caller()
                    if stop_error is not None:
                        operation_errors.append(stop_error)
                self._raise_acquisition_errors(generation, operation_errors)
                info_width = frame_info.v3.width
                info_height = frame_info.v3.height
                if (info_width, info_height) != (
                    applied.capture_width,
                    applied.capture_height,
                ):
                    dimension_error = CameraError(
                            "SDK frame dimensions "
                            f"{info_width}x{info_height} do not match configured "
                            f"{applied.capture_width}x{applied.capture_height}"
                    )
                    stop_error = self._stop_if_claimed_by_caller()
                    errors = [dimension_error]
                    if stop_error is not None:
                        errors.append(stop_error)
                    self._raise_acquisition_errors(generation, errors)
                try:
                    captured = np.frombuffer(
                        buffer,
                        dtype=applied.dtype,
                        count=applied.capture_width * applied.capture_height,
                    ).reshape(
                        applied.capture_height,
                        applied.capture_width,
                    )[
                        applied.crop_y : applied.crop_y + applied.prebin_height,
                        applied.crop_x : applied.crop_x + applied.prebin_width,
                    ]
                    if applied.software_binning > 1:
                        pixels = _software_bin_saturating(
                            captured,
                            applied.software_binning,
                            4095
                            if readout_mode is ReadoutMode.RAW12
                            else 255,
                        )
                    else:
                        pixels = captured.copy()
                    frame = Frame(
                        pixels=pixels,
                        started_utc=started_utc,
                        duration_s=applied.duration_s,
                        binning=binning,
                        roi=roi,
                        readout_mode=readout_mode,
                        gain=applied.gain,
                    )
                except BaseException as error:
                    stop_error = self._stop_if_claimed_by_caller()
                    if stop_error is not None:
                        error = CameraCleanupError(
                            "frame construction and stream stop both failed",
                            (
                                safe_camera_error(error, "construct frame"),
                                stop_error,
                            ),
                        )
                    completed = self._complete_error(generation, error)
                    if isinstance(completed, CameraOperationCancelledError):
                        raise completed from error
                    raise completed
                return self._commit_frame(generation, frame)
        finally:
            with self._condition:
                if not self._frame_committed:
                    self._acquiring = False
                self._cancel_requested = False
                self._condition.notify_all()
            self._acquisition_lock.release()

    def abort(self) -> None:
        """Cancel a software trigger and invalidate every result from its generation."""
        with self._condition:
            handle = self._require_connected_locked()
            if not self._acquiring or self._frame_committed or self._cancel_requested:
                return
            self._cancel_requested = True
            self._abort_generation += 1
        should_stop, handle = self._claim_stop()
        if not should_stop:
            return
        cleanup_errors: list[CameraError] = []
        try:
            handle.Trigger(0)
        except BaseException as error:
            cleanup_errors.append(_translate_sdk_error(error, "cancel trigger"))
        stop_error: CameraError | None = None
        try:
            handle.Stop()
        except BaseException as error:
            stop_error = _translate_sdk_error(error, "abort running camera")
            cleanup_errors.append(stop_error)
        finally:
            self._finish_stop(stop_error)
        if len(cleanup_errors) > 1:
            raise CameraCleanupError(
                "multiple abort cleanup operations failed", tuple(cleanup_errors)
            )
        if cleanup_errors:
            raise cleanup_errors[0]

    @property
    def gain(self) -> int:
        with self._native_lease() as handle:
            try:
                return handle.get_ExpoAGain()
            except BaseException as error:
                raise _translate_sdk_error(error, "read exposure gain") from error

    @gain.setter
    def gain(self, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise CameraInvalidValueError("gain must be an integer")
        minimum, maximum, _ = self.identity.gain_range
        if not minimum <= value <= maximum:
            raise CameraInvalidValueError(
                f"gain must be between {minimum} and {maximum}"
            )
        with self._native_lease() as handle:
            try:
                handle.put_ExpoAGain(value)
            except BaseException as error:
                raise _translate_sdk_error(error, "set exposure gain") from error

    def _claim_stop(self) -> tuple[bool, Any | None]:
        """Atomically transfer ownership of the one allowed native Stop call."""
        with self._condition:
            if self._stop_claimed or not self._camera_started:
                return False, None
            handle = self._require_connected_locked()
            self._stop_claimed = True
            self._camera_started = False
            self._stop_in_flight = True
            self._native_calls += 1
            return True, handle

    def _finish_stop(self, error: CameraError | None) -> None:
        with self._condition:
            if error is not None:
                self._unusable = True
            self._stop_in_flight = False
            self._native_calls -= 1
            self._condition.notify_all()

    def _stop_persistent_stream(self) -> CameraError | None:
        """Stop an idle pull stream before close without racing acquisition."""
        should_stop, handle = self._claim_stop()
        if not should_stop:
            return None
        error: CameraError | None = None
        try:
            handle.Stop()
        except BaseException as native_error:
            error = _translate_sdk_error(native_error, "stop camera stream")
        finally:
            self._finish_stop(error)
        return error

    def _stop_if_claimed_by_caller(self) -> CameraError | None:
        should_stop, handle = self._claim_stop()
        if not should_stop:
            return None
        error: CameraError | None = None
        try:
            handle.Stop()
        except BaseException as native_error:
            error = _translate_sdk_error(native_error, "stop camera")
        finally:
            self._finish_stop(error)
        return error

    @contextmanager
    def _native_lease(self, *, during_acquisition: bool = False):
        owns_idle_slot = False
        owns_configuration_slot = False
        with self._condition:
            while self._stop_in_flight or self._idle_native_active:
                self._condition.wait()
            handle = self._require_connected_locked()
            if self._disconnecting:
                raise CameraNotConnectedError("camera is disconnecting")
            if during_acquisition:
                self._acquisition_configuring = True
                owns_configuration_slot = True
            else:
                if self._acquisition_configuring or self._acquiring:
                    raise CameraBusyError("camera acquisition is running")
                self._idle_native_active = True
                owns_idle_slot = True
            self._native_calls += 1
        try:
            yield handle
        finally:
            with self._condition:
                self._native_calls -= 1
                if owns_idle_slot:
                    self._idle_native_active = False
                if owns_configuration_slot:
                    self._acquisition_configuring = False
                self._condition.notify_all()

    def _raise_if_aborted(self, generation: int) -> None:
        with self._condition:
            aborted = generation != self._abort_generation
        if aborted:
            raise CameraOperationCancelledError(
                "exposure was aborted; frame discarded"
            )

    def _raise_acquisition_errors(
        self, generation: int, errors: list[CameraError]
    ) -> None:
        if not errors:
            self._raise_if_aborted(generation)
            return
        if len(errors) > 1:
            error: Exception = CameraCleanupError(
                "multiple acquisition operations failed", tuple(errors)
            )
        else:
            error = errors[0]
        completed = self._complete_error(generation, error)
        if isinstance(completed, CameraOperationCancelledError):
            raise completed from error
        raise completed

    def _complete_error(
        self, generation: int, error: Exception
    ) -> Exception:
        """Linearize one error completion against Abort."""
        with self._condition:
            if generation != self._abort_generation:
                return CameraOperationCancelledError(
                    "exposure was aborted; frame discarded"
                )
            self._frame_committed = True
            self._acquiring = False
            self._condition.notify_all()
            return error

    def _commit_frame(self, generation: int, frame: Frame) -> Frame:
        """Linearize successful completion against Abort before returning."""
        with self._condition:
            if generation != self._abort_generation:
                raise CameraOperationCancelledError(
                    "exposure was aborted; frame discarded"
                )
            self._frame_committed = True
            self._acquiring = False
            self._condition.notify_all()
            return frame

    @property
    def temperature_c(self) -> float:
        with self._native_lease() as handle:
            try:
                return handle.get_Temperature() / 10.0
            except BaseException as error:
                raise _translate_sdk_error(error, "read sensor temperature") from error

    @property
    def target_temperature_c(self) -> float:
        return self._get_option("TOUPCAM_OPTION_TECTARGET") / 10.0

    @target_temperature_c.setter
    def target_temperature_c(self, value: float) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CameraInvalidValueError("target temperature must be numeric")
        numeric_value = float(value)
        if not math.isfinite(numeric_value):
            raise CameraInvalidValueError("target temperature must be finite")
        deci_c = round(numeric_value * 10)
        target_range = self.identity.tec_target_range_deci_c
        if target_range is not None and not target_range[0] <= deci_c <= target_range[1]:
            raise CameraInvalidValueError(
                f"target temperature must be between "
                f"{target_range[0] / 10:.1f} and {target_range[1] / 10:.1f} °C"
            )
        with self._native_lease() as handle:
            try:
                handle.put_Temperature(deci_c)
            except BaseException as error:
                raise _translate_sdk_error(error, "set TEC target") from error

    @property
    def cooler_on(self) -> bool:
        return bool(self._get_option("TOUPCAM_OPTION_TEC"))

    @cooler_on.setter
    def cooler_on(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise CameraInvalidValueError("cooler state must be boolean")
        self._put_option("TOUPCAM_OPTION_TEC", int(enabled), "set TEC state")

    def tec_voltage(self) -> tuple[float, float]:
        """Return current and configured maximum TEC voltage in volts."""
        current_option = self._sdk.TOUPCAM_OPTION_TEC_VOLTAGE
        maximum_option = self._sdk.TOUPCAM_OPTION_TEC_VOLTAGE_MAX
        with self._native_lease() as handle:
            try:
                current = handle.get_Option(current_option) / 10.0
                maximum = handle.get_Option(maximum_option) / 10.0
                return current, maximum
            except BaseException as error:
                raise _translate_sdk_error(error, "read TEC voltage") from error

    @property
    def cooler_power_percent(self) -> float:
        current, maximum = self.tec_voltage()
        if maximum <= 0:
            return 0.0
        return min(100.0, max(0.0, current * 100.0 / maximum))

    @property
    def model_flag(self) -> int:
        """The SDK model flags used for feature-specific internal controls."""
        return self.identity.model_flags

    @property
    def heater_supported(self) -> bool:
        return bool(self.model_flag & self._sdk.TOUPCAM_FLAG_HEAT)

    @property
    def heater_max_level(self) -> int:
        if not self.heater_supported:
            return 0
        maximum = self._get_option("TOUPCAM_OPTION_HEAT_MAX")
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0:
            raise CameraInvalidValueError("heater maximum is invalid")
        return maximum

    @property
    def heater_level(self) -> int:
        if not self.heater_supported:
            raise CameraNotSupportedError("camera does not support a heater")
        level = self._get_option("TOUPCAM_OPTION_HEAT")
        if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= self.heater_max_level:
            raise CameraInvalidValueError("heater level is outside the supported range")
        return level

    @heater_level.setter
    def heater_level(self, value: int) -> None:
        if not self.heater_supported:
            raise CameraNotSupportedError("camera does not support a heater")
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= self.heater_max_level:
            raise CameraInvalidValueError(
                f"heater level must be an integer from 0 to {self.heater_max_level}"
            )
        self._put_option("TOUPCAM_OPTION_HEAT", value, "set heater level")

    def _apply_settings_locked(
        self, handle: Any, settings: CaptureSettings
    ) -> AppliedSettings:
        (
            exposure_us,
            sdk_roi,
            dtype,
            crop_x,
            crop_y,
            capture_width,
            capture_height,
            prebin_width,
            prebin_height,
            hardware_binning,
            software_binning,
        ) = self._validated_sdk_settings(settings)
        structure = (sdk_roi, hardware_binning, settings.readout_mode)
        with self._condition:
            structure_changed = self._configured_structure != structure
            stream_started = self._camera_started
        if structure_changed and stream_started:
            try:
                persistent_state = self._snapshot_persistent_native_state(handle)
            except BaseException as error:
                raise _translate_sdk_error(
                    error,
                    "snapshot camera state for reconfiguration",
                ) from error
            try:
                handle.Stop()
            except BaseException as error:
                with self._condition:
                    self._unusable = True
                    self._configured_structure = None
                raise _translate_sdk_error(
                    error, "stop camera for reconfiguration"
                ) from error
            with self._condition:
                self._camera_started = False
                self._stop_claimed = False
                self._last_trigger_completed_monotonic = None
            try:
                handle.Close()
            except BaseException as error:
                with self._condition:
                    self._unusable = True
                    self._configured_structure = None
                raise _translate_sdk_error(
                    error, "close camera for reconfiguration"
                ) from error
            with self._condition:
                if self._handle is handle:
                    self._handle = None
                self._configured_structure = None
            try:
                handle = self._sdk.Toupcam.Open(f"sn:{self.serial}")
            except BaseException as error:
                raise _translate_sdk_error(
                    error, "reopen camera for reconfiguration"
                ) from error
            if handle is None:
                raise CameraNotConnectedError(
                    "could not reopen camera for reconfiguration"
                )
            with self._condition:
                self._handle = handle
                self._unusable = False
            try:
                self._replay_persistent_native_state(handle, persistent_state)
            except BaseException as error:
                replay_error = _translate_sdk_error(
                    error,
                    "replay camera state after reconfiguration",
                )
                close_error: CameraError | None = None
                try:
                    handle.Close()
                except BaseException as close_failure:
                    close_error = _translate_sdk_error(
                        close_failure,
                        "close camera after replay failure",
                    )
                with self._condition:
                    if close_error is None and self._handle is handle:
                        self._handle = None
                    elif close_error is not None:
                        self._handle = handle
                    self._camera_started = False
                    self._configured_structure = None
                    self._unusable = True
                if close_error is not None:
                    raise CameraCleanupError(
                        "state replay and fresh handle close both failed",
                        (replay_error, close_error),
                    ) from error
                raise replay_error from error
        try:
            if structure_changed:
                handle.put_AutoExpoEnable(False)
                handle.put_Option(self._sdk.TOUPCAM_OPTION_RAW, 1)
                handle.put_Option(
                    self._sdk.TOUPCAM_OPTION_BITDEPTH,
                    1 if settings.readout_mode is ReadoutMode.RAW12 else 0,
                )
                handle.put_Option(
                    self._sdk.TOUPCAM_OPTION_BINNING, hardware_binning
                )
                handle.put_Roi(*sdk_roi)
            handle.put_ExpoTime(exposure_us)
            if structure_changed:
                handle.put_Option(self._sdk.TOUPCAM_OPTION_TRIGGER, 1)
            width, height = handle.get_FinalSize()
            gain = handle.get_ExpoAGain()
        except BaseException as error:
            if structure_changed:
                with self._condition:
                    self._configured_structure = None
            raise _translate_sdk_error(error, "apply capture settings") from error
        if width <= 0 or height <= 0:
            raise CameraError(f"SDK returned invalid final size {width}x{height}")
        if (width, height) != (capture_width, capture_height):
            raise CameraInvalidValueError(
                "padded ROI does not produce the expected final SDK dimensions"
            )
        if structure_changed:
            with self._condition:
                self._configured_structure = structure
        return AppliedSettings(
            exposure_us / 1_000_000.0,
            settings.roi.width,
            settings.roi.height,
            dtype,
            gain,
            capture_width,
            capture_height,
            crop_x,
            crop_y,
            prebin_width,
            prebin_height,
            hardware_binning,
            software_binning,
        )

    def _snapshot_persistent_native_state(
        self,
        handle: Any,
    ) -> _PersistentNativeState:
        heater_max_level: int | None = None
        heater_level: int | None = None
        if self.heater_supported:
            heater_max_level = handle.get_Option(
                self._sdk.TOUPCAM_OPTION_HEAT_MAX
            )
            if (
                isinstance(heater_max_level, bool)
                or not isinstance(heater_max_level, int)
                or heater_max_level < 0
            ):
                raise CameraError(
                    "camera returned an invalid heater maximum during reopen"
                )
            heater_level = handle.get_Option(self._sdk.TOUPCAM_OPTION_HEAT)
            if (
                isinstance(heater_level, bool)
                or not isinstance(heater_level, int)
                or not 0 <= heater_level <= heater_max_level
            ):
                raise CameraError(
                    "camera returned an invalid heater level during reopen"
                )
        return _PersistentNativeState(
            gain=handle.get_ExpoAGain(),
            exposure_us=handle.get_ExpoTime(),
            auto_exposure=bool(handle.get_AutoExpoEnable()),
            tec_target_deci_c=handle.get_Option(
                self._sdk.TOUPCAM_OPTION_TECTARGET
            ),
            tec_enabled=handle.get_Option(self._sdk.TOUPCAM_OPTION_TEC),
            fan=handle.get_Option(self._sdk.TOUPCAM_OPTION_FAN),
            rotate=handle.get_Option(self._sdk.TOUPCAM_OPTION_ROTATE),
            tec_voltage_max=handle.get_Option(
                self._sdk.TOUPCAM_OPTION_TEC_VOLTAGE_MAX
            ),
            hflip=bool(handle.get_HFlip()),
            vflip=bool(handle.get_VFlip()),
            heater_max_level=heater_max_level,
            heater_level=heater_level,
        )

    def _replay_persistent_native_state(
        self,
        handle: Any,
        state: _PersistentNativeState,
    ) -> None:
        current = self._snapshot_persistent_native_state(handle)
        if current.gain != state.gain:
            handle.put_ExpoAGain(state.gain)
        if current.exposure_us != state.exposure_us:
            handle.put_ExpoTime(state.exposure_us)
        if current.auto_exposure != state.auto_exposure:
            handle.put_AutoExpoEnable(state.auto_exposure)
        if current.tec_target_deci_c != state.tec_target_deci_c:
            handle.put_Temperature(state.tec_target_deci_c)
        if current.tec_voltage_max != state.tec_voltage_max:
            handle.put_Option(
                self._sdk.TOUPCAM_OPTION_TEC_VOLTAGE_MAX,
                state.tec_voltage_max,
            )
        if current.tec_enabled != state.tec_enabled:
            handle.put_Option(self._sdk.TOUPCAM_OPTION_TEC, state.tec_enabled)
        if current.fan != state.fan:
            handle.put_Option(self._sdk.TOUPCAM_OPTION_FAN, state.fan)
        if current.rotate != state.rotate:
            handle.put_Option(self._sdk.TOUPCAM_OPTION_ROTATE, state.rotate)
        if current.hflip != state.hflip:
            handle.put_HFlip(state.hflip)
        if current.vflip != state.vflip:
            handle.put_VFlip(state.vflip)
        if state.heater_level is not None:
            if (
                current.heater_max_level is None
                or state.heater_level > current.heater_max_level
            ):
                raise CameraError(
                    "saved heater level exceeds the fresh handle maximum"
                )
            if current.heater_level != state.heater_level:
                handle.put_Option(
                    self._sdk.TOUPCAM_OPTION_HEAT,
                    state.heater_level,
                )

    def _pace_reused_trigger(self) -> None:
        """Allow the device's completed-frame pipeline to accept the next trigger."""
        completed = self._last_trigger_completed_monotonic
        if completed is None or self._trigger_recovery_s == 0:
            return
        remaining = self._trigger_recovery_s - (self._monotonic() - completed)
        if remaining > 0:
            self._sleeper(remaining)

    def _validated_sdk_settings(
        self, settings: CaptureSettings
    ) -> tuple[
        int,
        tuple[int, int, int, int],
        np.dtype,
        int,
        int,
        int,
        int,
        int,
        int,
        int,
        int,
    ]:
        if isinstance(settings.duration_s, bool) or not isinstance(
            settings.duration_s, (int, float)
        ):
            raise CameraInvalidValueError("duration_s must be numeric")
        if not math.isfinite(settings.duration_s) or settings.duration_s <= 0:
            raise CameraInvalidValueError("duration_s must be positive")
        exposure_us = round(settings.duration_s * 1_000_000)
        minimum, maximum, _ = self.identity.exposure_time_range_us
        if not minimum <= exposure_us <= maximum:
            raise CameraInvalidValueError(
                f"exposure must be between {minimum} and {maximum} microseconds"
            )
        if isinstance(settings.binning, bool) or not isinstance(settings.binning, int):
            raise CameraInvalidValueError("binning must be an integer")
        if not 1 <= settings.binning <= self.capabilities.max_bin:
            raise CameraInvalidValueError(
                f"binning must be between 1 and {self.capabilities.max_bin}"
            )
        if not isinstance(settings.readout_mode, ReadoutMode):
            raise CameraInvalidValueError("unsupported readout mode")
        roi = settings.roi
        if not isinstance(roi, Roi):
            raise CameraInvalidValueError("ROI must be a Roi")
        if roi.x < 0 or roi.y < 0 or roi.width <= 0 or roi.height <= 0:
            raise CameraInvalidValueError("ROI must have a non-negative origin and positive size")
        scale = settings.binning
        if (
            (roi.x + roi.width) * scale > self.capabilities.width
            or (roi.y + roi.height) * scale > self.capabilities.height
        ):
            raise CameraInvalidValueError("ROI extends outside the sensor")
        hardware_binning = scale
        software_binning = 1
        prebin_width = roi.width
        prebin_height = roi.height
        try:
            sdk_x, sdk_width, crop_x, capture_width = _padded_sdk_axis(
                roi.x,
                roi.width,
                scale,
                self.capabilities.width,
            )
            sdk_y, sdk_height, crop_y, capture_height = _padded_sdk_axis(
                roi.y,
                roi.height,
                scale,
                self.capabilities.height,
            )
        except CameraInvalidValueError:
            if scale == 1:
                raise
            # The SDK rounds a binned final dimension down to an even number,
            # so some valid ASCOM dimensions (for example 4096 // 3 == 1365)
            # cannot be produced by native binning. Capture only the covering
            # unbinned physical interval, crop it, then bin in bounded memory.
            hardware_binning = 1
            software_binning = scale
            physical_x = roi.x * scale
            physical_y = roi.y * scale
            prebin_width = roi.width * scale
            prebin_height = roi.height * scale
            sdk_x, sdk_width, crop_x, capture_width = _padded_sdk_axis(
                physical_x,
                prebin_width,
                1,
                self.capabilities.width,
            )
            sdk_y, sdk_height, crop_y, capture_height = _padded_sdk_axis(
                physical_y,
                prebin_height,
                1,
                self.capabilities.height,
            )
        sdk_roi = (sdk_x, sdk_y, sdk_width, sdk_height)
        dtype = np.dtype(
            np.uint16
            if settings.readout_mode is ReadoutMode.RAW12
            else np.uint8
        )
        return (
            exposure_us,
            sdk_roi,
            dtype,
            crop_x,
            crop_y,
            capture_width,
            capture_height,
            prebin_width,
            prebin_height,
            hardware_binning,
            software_binning,
        )

    def _require_connected_locked(self) -> Any:
        if self._handle is None:
            raise CameraNotConnectedError("camera is not connected")
        if self._unusable:
            raise CameraNotConnectedError(
                "camera handle is unusable; disconnect and reconnect"
            )
        return self._handle

    def _get_option(self, name: str) -> int:
        option = getattr(self._sdk, name)
        with self._native_lease() as handle:
            try:
                return handle.get_Option(option)
            except BaseException as error:
                raise _translate_sdk_error(error, f"read option {name}") from error

    def _put_option(self, name: str, value: int, operation: str) -> None:
        option = getattr(self._sdk, name)
        with self._native_lease() as handle:
            try:
                handle.put_Option(option, value)
            except BaseException as error:
                raise _translate_sdk_error(error, operation) from error


def _allocate_buffer(settings: AppliedSettings) -> Any:
    byte_count = (
        settings.capture_width
        * settings.capture_height
        * settings.dtype.itemsize
    )
    return ctypes.create_string_buffer(byte_count)


def _software_bin_saturating(
    pixels: np.ndarray,
    binning: int,
    maximum: int,
) -> np.ndarray:
    """Saturating n*n sum using one output-sized accumulator.

    ``pixels`` is a view into the SDK buffer. Each source phase is added
    directly into the accumulator, avoiding any full-size intermediate copy.
    """

    output_height = pixels.shape[0] // binning
    output_width = pixels.shape[1] // binning
    accumulator = np.zeros((output_height, output_width), dtype=np.uint64)
    for y_phase in range(binning):
        for x_phase in range(binning):
            np.add(
                accumulator,
                pixels[y_phase::binning, x_phase::binning],
                out=accumulator,
                casting="unsafe",
            )
    np.minimum(accumulator, maximum, out=accumulator)
    return accumulator.astype(pixels.dtype)


def _padded_sdk_axis(
    start: int,
    count: int,
    binning: int,
    sensor_size: int,
) -> tuple[int, int, int, int]:
    """Map one binned ASCOM axis to an even, >=16-pixel SDK interval."""

    minimum_capture = math.ceil(16 / binning)
    for crop_start in range(start + 1):
        sdk_start = (start - crop_start) * binning
        if sdk_start % 2:
            continue
        capture_count = max(crop_start + count, minimum_capture)
        if (capture_count * binning) % 2:
            capture_count += 1
        sdk_count = capture_count * binning
        if sdk_start + sdk_count <= sensor_size:
            return sdk_start, sdk_count, crop_start, capture_count
    raise CameraInvalidValueError(
        "ROI cannot be represented by an SDK-compatible padded interval"
    )


def _translate_sdk_error(error: BaseException, operation: str) -> CameraError:
    if isinstance(error, CameraError):
        return safe_camera_error(error, operation)
    try:
        hr = getattr(error, "hr", None)
    except BaseException:
        hr = None
    if hr is None:
        return safe_camera_error(error, operation)
    try:
        code = int(hr) & 0xFFFFFFFF
    except BaseException:
        return CameraError(f"{operation} failed")
    message = f"{operation} failed with HRESULT 0x{code:08X}"
    if code == 0x80070057:
        return CameraInvalidValueError(message)
    if code == 0x80004001:
        return CameraNotSupportedError(message)
    if code in {0x800700AA, 0x8000FFFF, 0x8001010E, 0x8000000A}:
        return CameraBusyError(message)
    if code == 0x8001011F:
        return CameraTimeoutError(message)
    if code in {0x8007001F, 0x80070005}:
        return CameraNotConnectedError(message)
    return CameraError(message)


def _identity_from_handle(device: Any, handle: Any) -> CameraIdentity:
    model = device.model
    resolution = handle.get_Resolution(0)
    try:
        pixel_size = handle.get_PixelSize(0)
    except ArgumentError as error:
        # SDK 20250623 annotates this call as c_int but passes c_uint internally.
        # Python 3.13 rejects that mismatch; EnumV2 exposes the same static values.
        if not _is_known_pixel_size_wrapper_error(error):
            raise
        pixel_size = (model.xpixsz, model.ypixsz)
    capabilities = CameraCapabilities(
        width=resolution[0],
        height=resolution[1],
        pixel_size_x=pixel_size[0],
        pixel_size_y=pixel_size[1],
        readout_modes=("RAW 12-bit", "RAW 8-bit"),
        max_bin=8,
        can_abort=True,
        can_stop=False,
    )
    tec_target_range = handle.get_TecTargetRange() if hasattr(handle, "get_TecTargetRange") else None
    serial = handle.SerialNumber()
    return CameraIdentity(
        device_id=serial,
        display_name=device.displayname,
        model=model.name,
        serial=serial,
        capabilities=capabilities,
        firmware=handle.FwVersion(),
        hardware=handle.HwVersion(),
        production_date=handle.ProductionDate(),
        fpga=handle.FpgaVersion(),
        model_flags=model.flag,
        exposure_time_range_us=handle.get_ExpTimeRange(),
        gain_range=handle.get_ExpoAGainRange(),
        tec_target_range_deci_c=tec_target_range,
    )


def _is_known_pixel_size_wrapper_error(error: ArgumentError) -> bool:
    """Match only the SDK 20250623 Python 3.13 second-argument annotation defect."""
    message = safe_exception_text(error, fallback="")
    return (
        sys.version_info[:2] == (3, 13)
        and "argument 2:" in message
        and ("c_ulong" in message or "c_uint" in message)
        and "cannot be interpreted as an integer" in message
    )
