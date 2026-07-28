"""ToupTek discovery, settings, cooling, and synchronous acquisition."""

from __future__ import annotations

import ctypes
import math
import sys
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

    def __init__(self, serial: str | None, sdk: ModuleType | Any | None = None) -> None:
        self._sdk = sdk if sdk is not None else load_default_toupcam_module()
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
            except Exception as error:
                raise _translate_sdk_error(error, "connect camera") from error
            if handle is None:
                raise CameraNotConnectedError(
                    f"could not open camera with serial {self.serial!r}"
                )
            self._handle = handle
            self._unusable = False
            self._disconnecting = False

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
        close_error: CameraError | None = None
        if handle is not None:
            try:
                handle.Close()
            except Exception as error:
                close_error = _translate_sdk_error(error, "disconnect camera")
        with self._condition:
            if close_error is None and self._handle is handle:
                self._handle = None
                self._disconnecting = False
                self._unusable = False
            elif close_error is not None:
                self._disconnecting = True
                self._unusable = True
            self._condition.notify_all()
        disconnect_errors = tuple(
            error for error in (abort_error, close_error) if error is not None
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
                buffer = _allocate_buffer(applied)
                frame_info = self._sdk.ToupcamFrameInfoV4()
                timeout_ms = math.ceil(applied.duration_s * 1000 * 1.02 + 4000)
                claim_state: dict[str, int] = {}

                def claim() -> None:
                    with self._condition:
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
                    handle.StartPullModeWithCallback(None, None)
                    with self._condition:
                        self._camera_started = True
                        cancelled = generation != self._abort_generation
                    if not cancelled:
                        started_utc = datetime.now(timezone.utc)
                        handle.TriggerSyncV4(timeout_ms, buffer, 0, -1, frame_info)
                except Exception as error:
                    operation_errors.append(_translate_sdk_error(error, "acquire frame"))
                stop_error = self._stop_if_claimed_by_caller()
                if stop_error is not None:
                    operation_errors.append(stop_error)
                self._raise_acquisition_errors(generation, operation_errors)
                info_width = frame_info.v3.width
                info_height = frame_info.v3.height
                if (info_width, info_height) != (applied.width, applied.height):
                    raise self._complete_error(
                        generation,
                        CameraError(
                            "SDK frame dimensions "
                            f"{info_width}x{info_height} do not match configured "
                            f"{applied.width}x{applied.height}"
                        ),
                    )
                try:
                    pixels = np.frombuffer(
                        buffer,
                        dtype=applied.dtype,
                        count=applied.width * applied.height,
                    ).reshape(applied.height, applied.width).copy()
                    frame = Frame(
                        pixels=pixels,
                        started_utc=started_utc,
                        duration_s=applied.duration_s,
                        binning=binning,
                        roi=roi,
                        readout_mode=readout_mode,
                        gain=applied.gain,
                    )
                except Exception as error:
                    completed = self._complete_error(generation, error)
                    if isinstance(completed, CameraOperationCancelledError):
                        raise completed from error
                    raise completed
                return self._commit_frame(generation, frame)
        finally:
            with self._condition:
                if not self._frame_committed:
                    self._acquiring = False
                self._camera_started = False
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
        except Exception as error:
            cleanup_errors.append(_translate_sdk_error(error, "cancel trigger"))
        stop_error: CameraError | None = None
        try:
            handle.Stop()
        except Exception as error:
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
            except Exception as error:
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
            except Exception as error:
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

    def _stop_if_claimed_by_caller(self) -> CameraError | None:
        should_stop, handle = self._claim_stop()
        if not should_stop:
            return None
        error: CameraError | None = None
        try:
            handle.Stop()
        except Exception as native_error:
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
            except Exception as error:
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
            except Exception as error:
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
            except Exception as error:
                raise _translate_sdk_error(error, "read TEC voltage") from error

    @property
    def cooler_power_percent(self) -> float:
        current, maximum = self.tec_voltage()
        if maximum <= 0:
            return 0.0
        return min(100.0, max(0.0, current * 100.0 / maximum))

    def _apply_settings_locked(
        self, handle: Any, settings: CaptureSettings
    ) -> AppliedSettings:
        exposure_us, sdk_roi, dtype = self._validated_sdk_settings(settings)
        try:
            handle.put_Option(self._sdk.TOUPCAM_OPTION_RAW, 1)
            handle.put_Option(
                self._sdk.TOUPCAM_OPTION_BITDEPTH,
                1 if settings.readout_mode is ReadoutMode.RAW12 else 0,
            )
            handle.put_Option(self._sdk.TOUPCAM_OPTION_BINNING, settings.binning)
            handle.put_Roi(*sdk_roi)
            handle.put_ExpoTime(exposure_us)
            handle.put_Option(self._sdk.TOUPCAM_OPTION_TRIGGER, 1)
            width, height = handle.get_FinalSize()
            gain = handle.get_ExpoAGain()
        except Exception as error:
            raise _translate_sdk_error(error, "apply capture settings") from error
        if width <= 0 or height <= 0:
            raise CameraError(f"SDK returned invalid final size {width}x{height}")
        if (width, height) != (settings.roi.width, settings.roi.height):
            raise CameraInvalidValueError(
                "requested ROI does not produce the same final SDK dimensions"
            )
        return AppliedSettings(
            exposure_us / 1_000_000.0,
            width,
            height,
            dtype,
            gain,
        )

    def _validated_sdk_settings(
        self, settings: CaptureSettings
    ) -> tuple[int, tuple[int, int, int, int], np.dtype]:
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
        sdk_roi = (
            roi.x * scale,
            roi.y * scale,
            roi.width * scale,
            roi.height * scale,
        )
        x, y, width, height = sdk_roi
        if width < 16 or height < 16 or any(value % 2 for value in sdk_roi):
            raise CameraInvalidValueError(
                "scaled SDK ROI coordinates and dimensions must be even and at least 16 pixels"
            )
        if x + width > self.capabilities.width or y + height > self.capabilities.height:
            raise CameraInvalidValueError("ROI extends outside the sensor")
        if roi.width % 2 or roi.height % 2:
            raise CameraInvalidValueError("final SDK image dimensions must be even")
        dtype = np.dtype(
            np.uint16
            if settings.readout_mode is ReadoutMode.RAW12
            else np.uint8
        )
        return exposure_us, sdk_roi, dtype

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
            except Exception as error:
                raise _translate_sdk_error(error, f"read option {name}") from error

    def _put_option(self, name: str, value: int, operation: str) -> None:
        option = getattr(self._sdk, name)
        with self._native_lease() as handle:
            try:
                handle.put_Option(option, value)
            except Exception as error:
                raise _translate_sdk_error(error, operation) from error


def _allocate_buffer(settings: AppliedSettings) -> Any:
    byte_count = settings.width * settings.height * settings.dtype.itemsize
    return ctypes.create_string_buffer(byte_count)


def _translate_sdk_error(error: Exception, operation: str) -> CameraError:
    if isinstance(error, CameraError):
        return error
    hr = getattr(error, "hr", None)
    if hr is None:
        return CameraError(f"{operation} failed: {error}")
    code = int(hr) & 0xFFFFFFFF
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
    message = str(error)
    return (
        sys.version_info[:2] == (3, 13)
        and "argument 2:" in message
        and ("c_ulong" in message or "c_uint" in message)
        and "cannot be interpreted as an integer" in message
    )
