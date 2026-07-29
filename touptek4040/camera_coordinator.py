"""Single-owner camera lifecycle, exposure, and preview coordination."""

from __future__ import annotations

import math
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import TypeVar

from .camera_contract import (
    AcquisitionControl,
    CameraCapabilities,
    CameraContract,
    ExposureSettings,
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
    CameraOwnerProtocolError,
    CameraOwnerTerminationError,
    CameraOperationCancelledError,
    CameraTimeoutError,
    safe_camera_error,
    safe_exception_identity,
    safe_exception_text,
)
from .event_broker import EventBroker
from .image_workspace import ImageGeneration, ImageWorkspace

_ResultT = TypeVar("_ResultT")
_ASYNC_DIAGNOSTIC_LIMIT = 700
_KNOWN_ASYNC_EVENTS = {
    "0x0080": "camera error event 0x0080",
    "0x0081": "USB transfer event 0x0081 failure",
    "0x0082": "camera no-frame timeout event 0x0082",
    "0x0083": "camera no-packet timeout event 0x0083",
}
_ASYNC_TEXT_SCAN_LIMIT = 512


class CameraState(IntEnum):
    """ASCOM CameraStates numeric values."""

    IDLE = 0
    WAITING = 1
    EXPOSING = 2
    READING = 3
    DOWNLOAD = 4
    ERROR = 5


@dataclass(frozen=True)
class CameraSnapshot:
    connected: bool
    connecting: bool
    camera_state: CameraState
    image_ready: bool
    percent_completed: int | None
    error: CameraError | None


@dataclass(frozen=True)
class HeaterSnapshot:
    """One confirmed heater state from an exact connected camera session."""

    connection_generation: int
    sequence: int
    supported: bool
    maximum: int | None
    level: int | None


@dataclass(frozen=True)
class FormalImageRead:
    """One condition-linearized decision for a formal image download."""

    connected: bool
    error: CameraError | None
    image_ready: bool
    generation: ImageGeneration | None


class _CoordinatorAcquisitionControl(AcquisitionControl):
    def __init__(
        self,
        coordinator: CameraCoordinator,
        session: int,
        camera: CameraContract,
    ) -> None:
        self._coordinator = coordinator
        self._session = session
        self._camera = camera

    def commit_start(self, claim: Callable[[], None]) -> bool:
        coordinator = self._coordinator
        with coordinator._condition:
            if not coordinator._is_current_exposure_locked(
                self._session,
                self._camera,
            ):
                return False
            claim()
            coordinator._exposure_started_monotonic = time.monotonic()
            coordinator._publish_changed_locked()
            return True


class CameraCoordinator:
    """Own exactly one camera and linearize all published camera state."""

    _PREVIEW_DURATION_S = 0.1

    def __init__(
        self,
        factory: Callable[[str], CameraContract],
        source: str,
    ) -> None:
        if not callable(factory):
            raise TypeError("factory must be callable")
        self._factory = factory
        self._source = source
        self._condition = threading.Condition(threading.RLock())
        self._lifecycle_lock = threading.Lock()
        self._acquisition_gate = threading.Lock()
        self._lifecycle_errors: list[CameraError] = []
        self._camera: CameraContract | None = None
        self._connected = False
        self._connecting = False
        self._connection_target: bool | None = None
        self._connection_generation = 0
        self._heater_sequence = 0
        self._heater_snapshot: HeaterSnapshot | None = None
        self._heater_cache_camera: CameraContract | None = None
        self._state = CameraState.IDLE
        self._error: CameraError | None = None
        self._session_id = 0
        self._exposure_started_monotonic: float | None = None
        self._exposure_duration_s: float | None = None
        self._frame: Frame | None = None
        self._image_ready = False
        self._preview_frame: Frame | None = None
        self._preview_session = 0
        self._preview_stop: threading.Event | None = None
        self._preview_thread: threading.Thread | None = None
        self._exposure_thread: threading.Thread | None = None
        self._image_workspace = ImageWorkspace()
        self.events: EventBroker[CameraSnapshot] = EventBroker()

    def connect_async(self) -> None:
        with self._condition:
            if self._connected and not self._connecting:
                return
            if self._connecting and self._connection_target is True:
                return
            quarantined = self._camera is not None and not self._connected
            if not self._connecting and not quarantined:
                self._lifecycle_errors.clear()
            self._connection_generation += 1
            generation = self._connection_generation
            self._invalidate_heater_cache_locked()
            self._connection_target = True
            self._connecting = True
            if not quarantined:
                self._error = None
                if self._state is CameraState.ERROR:
                    self._state = CameraState.IDLE
            self._publish_changed_locked()
        self._start_thread(
            target=self._connect_worker,
            args=(generation,),
            name="camera-connect",
        )

    def disconnect_async(self) -> None:
        with self._condition:
            if (
                not self._connected
                and not self._connecting
                and self._camera is None
            ):
                return
            if self._connecting and self._connection_target is False:
                return
            quarantined = self._camera is not None and not self._connected
            if not self._connecting and not quarantined:
                self._lifecycle_errors.clear()
            self._connection_generation += 1
            generation = self._connection_generation
            self._invalidate_heater_cache_locked()
            self._connection_target = False
            self._connecting = True
            self._session_id += 1
            self._invalidate_preview_locked()
            if not quarantined:
                self._state = CameraState.IDLE
                self._error = None
            self._clear_formal_image_locked()
            self._publish_changed_locked()
        self._start_thread(
            target=self._disconnect_worker,
            args=(generation,),
            name="camera-disconnect",
        )

    def set_connected_sync(self, connected: bool) -> None:
        if not isinstance(connected, bool):
            raise CameraInvalidValueError("connected state must be boolean")
        with self._condition:
            existing_operation = (
                self._connecting and self._connection_target is connected
            )
            previous_generation = self._connection_generation
        if connected:
            self.connect_async()
        else:
            self.disconnect_async()
        with self._condition:
            operation_generation = self._connection_generation
            participated = (
                existing_operation or operation_generation != previous_generation
            )
            while self._connecting and self._connection_target is connected:
                self._condition.wait()
            if (
                participated
                and operation_generation == self._connection_generation
                and self._error is not None
            ):
                raise self._error
            if self._connected is connected:
                return
            if self._error is not None:
                raise self._error
            raise CameraBusyError("connection request was superseded")

    def start_exposure(
        self,
        duration_s: float,
        light: bool,
        validate: Callable[[CameraCapabilities], None] | None = None,
        settings: Callable[[CameraCapabilities], ExposureSettings] | None = None,
    ) -> None:
        if not isinstance(light, bool):
            raise CameraInvalidValueError("light must be boolean")
        if validate is not None and not callable(validate):
            raise CameraInvalidValueError("exposure validator must be callable")
        if settings is not None and not callable(settings):
            raise CameraInvalidValueError("exposure settings provider must be callable")
        if validate is not None and settings is not None:
            raise CameraInvalidValueError(
                "validate and settings cannot both be supplied"
            )
        with self._condition:
            camera = self._require_available_camera_locked()
            self._require_exposure_startable_locked()
            duration = self._validated_duration(duration_s, light)
            capabilities = camera.capabilities
            if validate is not None:
                validate(capabilities)
            exposure_settings = (
                settings(capabilities)
                if settings is not None
                else ExposureSettings(
                    roi=Roi(0, 0, capabilities.width, capabilities.height),
                    binning=1,
                    readout_mode=ReadoutMode.RAW12,
                    gain=100,
                )
            )
            self._validate_exposure_settings(exposure_settings, capabilities)
            preview_thread = self._preview_thread
            had_preview = preview_thread is not None and preview_thread.is_alive()
            self._invalidate_preview_locked()
            self._session_id += 1
            session = self._session_id
            acquisition_control = _CoordinatorAcquisitionControl(
                self,
                session,
                camera,
            )
            self._clear_formal_image_locked()
            self._state = CameraState.EXPOSING
            self._error = None
            self._exposure_duration_s = duration
            self._publish_changed_locked()
        self._exposure_thread = self._start_thread(
            target=self._exposure_worker,
            args=(
                session,
                camera,
                duration,
                exposure_settings,
                acquisition_control,
                preview_thread if had_preview else None,
            ),
            name=f"camera-exposure-{session}",
        )

    def update_settings_if_idle(
        self,
        operation: Callable[[CameraCapabilities], _ResultT],
    ) -> _ResultT:
        """Run a short settings commit at the connected/idle linearization point."""

        if not callable(operation):
            raise CameraInvalidValueError("settings operation must be callable")
        with self._condition:
            camera = self._require_available_camera_locked()
            self._require_idle_locked()
            return operation(camera.capabilities)

    def set_target_temperature_if_idle(self, value: float) -> None:
        with self._condition:
            camera = self._require_available_camera_locked()
            self._require_idle_locked()
            camera.target_temperature_c = value

    def set_cooler_on_if_idle(self, enabled: bool) -> None:
        with self._condition:
            camera = self._require_available_camera_locked()
            self._require_idle_locked()
            camera.cooler_on = enabled

    def set_heater_level_if_idle(self, level: int) -> None:
        self.set_heater_level_confirmed_if_idle(level)

    def set_heater_level_confirmed_if_idle(self, level: int) -> HeaterSnapshot:
        """Set and confirm a heater level without releasing the idle barrier."""

        with self._condition:
            camera = self._require_available_camera_locked()
            self._require_idle_locked()
            if isinstance(level, bool) or not isinstance(level, int):
                raise CameraInvalidValueError(
                    "heater level must be an integer"
                )
            try:
                if not camera.heater_supported:
                    raise CameraNotSupportedError(
                        "camera does not support a heater"
                    )
                maximum = self._validated_heater_maximum(
                    camera.heater_max_level
                )
                if level < 0 or level > maximum:
                    raise CameraInvalidValueError(
                        f"heater level must be an integer from 0 to {maximum}"
                    )
                camera.heater_level = level
                confirmed = self._validated_heater_level(
                    camera.heater_level,
                    maximum,
                )
                return self._cache_heater_snapshot_locked(
                    camera,
                    supported=True,
                    maximum=maximum,
                    level=confirmed,
                )
            except BaseException as error:
                self._invalidate_heater_cache_locked()
                if isinstance(error, CameraError):
                    raise
                raise _camera_error(error, "set heater level") from error

    def heater_snapshot(self) -> HeaterSnapshot:
        """Read heater state while idle or return this session's busy cache."""

        with self._condition:
            camera = self._require_available_camera_locked()
            if self._state is CameraState.IDLE:
                try:
                    return self._read_heater_snapshot_locked(camera)
                except BaseException as error:
                    self._invalidate_heater_cache_locked()
                    if isinstance(error, CameraError):
                        raise
                    raise _camera_error(error, "read heater state") from error
            snapshot = self._heater_snapshot
            if (
                snapshot is not None
                and self._heater_cache_camera is camera
                and snapshot.connection_generation
                == self._connection_generation
            ):
                return snapshot
            raise CameraBusyError(
                "heater state is unavailable while camera is "
                f"{self._state.name.lower()}"
            )

    def abort_exposure(self) -> None:
        with self._condition:
            camera = self._require_available_camera_locked()
            if self._state is not CameraState.EXPOSING:
                return
            self._session_id += 1
            abort_session = self._session_id
        try:
            camera.abort()
        except BaseException as error:
            translated = _camera_error(error, "abort exposure")
            with self._condition:
                if (
                    self._session_id == abort_session
                    and self._connected
                    and self._camera is camera
                ):
                    self._error = translated
                    if _is_session_ending_error(translated):
                        self._state = CameraState.ERROR
                        self._connected = False
                        self._connection_target = False
                        self._invalidate_heater_cache_locked()
                        self._clear_formal_image_locked()
                        self._exposure_started_monotonic = None
                        self._exposure_duration_s = None
                    self._publish_changed_locked()
            raise translated from error
        with self._condition:
            if (
                self._session_id == abort_session
                and self._connected
                and self._camera is camera
            ):
                self._state = CameraState.IDLE
                self._error = None
                self._clear_formal_image_locked()
                self._exposure_started_monotonic = None
                self._exposure_duration_s = None
                self._publish_changed_locked()

    def start_preview(self, callback: Callable[[Frame], None]) -> None:
        if not callable(callback):
            raise CameraInvalidValueError("preview callback must be callable")
        with self._condition:
            camera = self._require_available_camera_locked()
            self._require_idle_locked()
            if self._preview_thread is not None and self._preview_thread.is_alive():
                raise CameraBusyError("preview is already running")
            self._clear_preview_image_locked()
            self._preview_session += 1
            session = self._preview_session
            stop = threading.Event()
            self._preview_stop = stop
            self._error = None
            thread = self._start_thread(
                target=self._preview_worker,
                args=(session, stop, camera, callback),
                name=f"camera-preview-{session}",
            )
            self._preview_thread = thread

    def stop_preview(self) -> None:
        with self._condition:
            thread = self._preview_thread
            if thread is None or not thread.is_alive():
                self._invalidate_preview_locked()
                return
            camera = self._camera
            self._invalidate_preview_locked()
        error = self._stop_preview_thread(camera, thread)
        if error is None:
            return
        with self._condition:
            self._state = CameraState.ERROR
            self._error = error
            if _contains_not_connected(error):
                self._connected = False
                self._connection_target = False
                self._invalidate_heater_cache_locked()
            self._publish_changed_locked()
        raise error

    def _stop_preview_thread(
        self,
        camera: CameraContract | None,
        thread: threading.Thread,
    ) -> CameraError | None:
        error: CameraError | None = None
        if camera is not None:
            try:
                camera.abort()
            except CameraOperationCancelledError:
                pass
            except BaseException as caught:
                error = _camera_error(caught, "stop preview")
        if thread is not threading.current_thread():
            thread.join()
        with self._condition:
            if self._preview_thread is thread:
                self._preview_thread = None
                self._preview_stop = None
                self._condition.notify_all()
        return error

    def snapshot(self) -> CameraSnapshot:
        with self._condition:
            return self._snapshot_locked()

    @property
    def frame(self) -> Frame | None:
        with self._condition:
            return self._frame

    @property
    def image_generation(self) -> ImageGeneration | None:
        with self._condition:
            return self._image_workspace.current()

    @property
    def preview_frame(self) -> Frame | None:
        with self._condition:
            return self._preview_frame

    @property
    def preview_generation(self) -> ImageGeneration | None:
        with self._condition:
            return self._image_workspace.current_preview()

    @property
    def preview_active(self) -> bool:
        with self._condition:
            thread = self._preview_thread
            stop = self._preview_stop
            return (
                thread is not None
                and thread.is_alive()
                and stop is not None
                and not stop.is_set()
            )

    def formal_image_read(self) -> FormalImageRead:
        """Capture connection, failure, readiness, and generation atomically."""

        with self._condition:
            generation = self._image_workspace.current()
            return FormalImageRead(
                connected=self._connected and not self._connecting,
                error=self._error,
                image_ready=self._image_ready,
                generation=generation,
            )

    @property
    def capabilities(self) -> CameraCapabilities:
        return self._available_camera().capabilities

    @property
    def temperature_c(self) -> float:
        return self._available_camera().temperature_c

    @property
    def target_temperature_c(self) -> float:
        return self._available_camera().target_temperature_c

    @target_temperature_c.setter
    def target_temperature_c(self, value: float) -> None:
        self._available_camera().target_temperature_c = value

    @property
    def cooler_on(self) -> bool:
        return self._available_camera().cooler_on

    @cooler_on.setter
    def cooler_on(self, enabled: bool) -> None:
        self._available_camera().cooler_on = enabled

    def tec_voltage(self) -> tuple[float, float]:
        return self._available_camera().tec_voltage()

    @property
    def cooler_power_percent(self) -> float:
        return self._available_camera().cooler_power_percent

    @property
    def heater_supported(self) -> bool:
        return self.heater_snapshot().supported

    @property
    def heater_max_level(self) -> int:
        maximum = self.heater_snapshot().maximum
        if maximum is None:
            raise CameraNotSupportedError("camera does not support a heater")
        return maximum

    @property
    def heater_level(self) -> int:
        level = self.heater_snapshot().level
        if level is None:
            raise CameraNotSupportedError("camera does not support a heater")
        return level

    def _connect_worker(self, generation: int) -> None:
        camera: CameraContract | None = None
        error: CameraError | None = None
        fatal_heater_error: CameraError | None = None
        with self._lifecycle_lock:
            with self._condition:
                current = (
                    generation == self._connection_generation
                    and self._connection_target is True
                )
                pending_errors = tuple(self._lifecycle_errors)
                if not current:
                    return
                stale_camera = self._camera
                self._lifecycle_errors.clear()
            cleanup_errors: list[CameraError] = []
            if stale_camera is not None:
                try:
                    stale_camera.abort()
                except CameraNotConnectedError:
                    pass
                except BaseException as caught:
                    _append_error(
                        cleanup_errors,
                        _camera_error(
                            caught,
                            "abort previous camera owner",
                        ),
                    )
                try:
                    stale_camera.disconnect()
                except BaseException as caught:
                    _append_error(
                        cleanup_errors,
                        _camera_error(
                            caught,
                            "disconnect previous camera owner",
                        ),
                    )
                with self._condition:
                    current = (
                        generation == self._connection_generation
                        and self._connection_target is True
                    )
                    all_cleanup_errors = _unique_errors(
                        [*pending_errors, *cleanup_errors]
                    )
                    if cleanup_errors:
                        if not current:
                            self._lifecycle_errors[:] = all_cleanup_errors
                            return
                        self._connected = False
                        self._invalidate_heater_cache_locked()
                        self._connecting = False
                        self._state = CameraState.ERROR
                        self._error = _combined_error(
                            "previous camera owner cleanup failed",
                            all_cleanup_errors,
                        )
                        self._publish_changed_locked()
                        return
                    if self._camera is stale_camera:
                        self._camera = None
                        self._invalidate_heater_cache_locked()
                    self._lifecycle_errors.clear()
                    if not current:
                        return
                pending_errors = ()
            prior_cleanup_errors = list(pending_errors)
            if prior_cleanup_errors:
                with self._condition:
                    self._connected = False
                    self._invalidate_heater_cache_locked()
                    self._connecting = False
                    self._state = CameraState.ERROR
                    self._error = _combined_error(
                        "previous camera owner cleanup failed",
                        prior_cleanup_errors,
                    )
                    self._publish_changed_locked()
                return
            try:
                camera = self._factory(self._source)
                camera.connect()
            except BaseException as caught:
                error = _camera_error(caught, "connect camera")
            with self._condition:
                current = (
                    generation == self._connection_generation
                    and self._connection_target is True
                )
                if current and error is None and camera is not None:
                    try:
                        self._read_heater_snapshot_locked(camera)
                    except BaseException as caught:
                        self._invalidate_heater_cache_locked()
                        heater_error = _camera_error(
                            caught,
                            "initialize heater state",
                        )
                        if _is_session_ending_error(heater_error):
                            fatal_heater_error = heater_error
                    if fatal_heater_error is None:
                        self._camera = camera
                        self._connected = True
                        self._connecting = False
                        self._state = CameraState.IDLE
                        self._error = None
                        self._lifecycle_errors.clear()
                        self._publish_changed_locked()
                elif current:
                    self._camera = None
                    self._connected = False
                    self._invalidate_heater_cache_locked()
                    self._connecting = False
                    self._state = CameraState.ERROR
                    self._error = error or CameraError("connect camera failed")
                    self._lifecycle_errors.clear()
                    self._publish_changed_locked()
            if fatal_heater_error is not None and camera is not None:
                cleanup_errors: list[CameraError] = []
                try:
                    camera.disconnect()
                except BaseException as cleanup_error:
                    _append_error(
                        cleanup_errors,
                        _camera_error(
                            cleanup_error,
                            "disconnect failed camera initialization",
                        ),
                    )
                with self._condition:
                    current = (
                        generation == self._connection_generation
                        and self._connection_target is True
                    )
                    all_errors = _unique_errors(
                        [fatal_heater_error, *cleanup_errors]
                    )
                    if current:
                        self._camera = camera if cleanup_errors else None
                        self._connected = False
                        self._invalidate_heater_cache_locked()
                        self._connecting = False
                        self._state = CameraState.ERROR
                        self._error = _combined_error(
                            "camera initialization failed",
                            all_errors,
                        )
                        self._lifecycle_errors.clear()
                        self._publish_changed_locked()
                    elif cleanup_errors:
                        self._camera = camera
                        self._connected = False
                        self._invalidate_heater_cache_locked()
                        self._lifecycle_errors[:] = _unique_errors(
                            [*self._lifecycle_errors, *all_errors]
                        )
                return
            if not current and camera is not None:
                cleanup_errors: list[CameraError] = []
                try:
                    camera.disconnect()
                except BaseException as cleanup_error:
                    _append_error(
                        cleanup_errors,
                        _camera_error(
                            cleanup_error,
                            "disconnect superseded camera owner",
                        ),
                    )
                if cleanup_errors:
                    with self._condition:
                        self._camera = camera
                        self._connected = False
                        self._invalidate_heater_cache_locked()
                        self._lifecycle_errors[:] = _unique_errors(
                            [*self._lifecycle_errors, *cleanup_errors]
                        )

    def _disconnect_worker(self, generation: int) -> None:
        errors: list[CameraError] = []
        with self._lifecycle_lock:
            with self._condition:
                camera = self._camera
            if camera is not None:
                try:
                    camera.abort()
                except CameraNotConnectedError:
                    pass
                except BaseException as error:
                    _append_error(
                        errors,
                        _camera_error(error, "abort before disconnect"),
                    )
                try:
                    camera.disconnect()
                except BaseException as error:
                    _append_error(
                        errors,
                        _camera_error(error, "disconnect camera"),
                    )
            with self._condition:
                if not errors and self._camera is camera:
                    self._camera = None
                    self._invalidate_heater_cache_locked()
                current = (
                    generation == self._connection_generation
                    and self._connection_target is False
                )
                if current:
                    all_errors = _unique_errors([*self._lifecycle_errors, *errors])
                    self._lifecycle_errors.clear()
                    self._connected = False
                    self._invalidate_heater_cache_locked()
                    self._connecting = False
                    self._clear_formal_image_locked()
                    if all_errors:
                        self._state = CameraState.ERROR
                        self._error = _combined_error(
                            "camera disconnect failed", all_errors
                        )
                    else:
                        self._state = CameraState.IDLE
                        self._error = None
                    self._publish_changed_locked()
                else:
                    if errors:
                        self._lifecycle_errors[:] = _unique_errors(
                            [*self._lifecycle_errors, *errors]
                        )
                    else:
                        self._lifecycle_errors.clear()

    def _exposure_worker(
        self,
        session: int,
        camera: CameraContract,
        duration_s: float,
        settings: ExposureSettings,
        acquisition_control: AcquisitionControl,
        preview_thread: threading.Thread | None,
    ) -> None:
        if preview_thread is not None:
            error = self._stop_preview_thread(camera, preview_thread)
            if error is not None:
                self._finish_exposure_error(
                    session,
                    camera,
                    error,
                    operation="stop preview",
                    duration_s=duration_s,
                    settings=settings,
                )
                return
        with self._acquisition_gate:
            with self._condition:
                if not self._is_current_exposure_locked(session, camera):
                    return
            try:
                camera.gain = settings.gain
            except BaseException as error:
                self._finish_exposure_error(
                    session,
                    camera,
                    error,
                    operation="apply gain",
                    duration_s=duration_s,
                    settings=settings,
                )
                return
            with self._condition:
                if not self._is_current_exposure_locked(session, camera):
                    return
            try:
                frame = camera.acquire(
                    duration_s,
                    settings.roi,
                    settings.binning,
                    settings.readout_mode,
                    acquisition_control,
                )
            except BaseException as error:
                self._finish_exposure_error(
                    session,
                    camera,
                    error,
                    operation="acquire frame",
                    duration_s=duration_s,
                    settings=settings,
                )
                return
            with self._condition:
                if not self._is_current_exposure_locked(session, camera):
                    return
                try:
                    generation = self._image_workspace.publish(frame)
                except BaseException as error:
                    publication_error = error
                else:
                    publication_error = None
                    self._frame = generation.frame
                    self._image_ready = True
                    self._state = CameraState.IDLE
                    self._error = None
                    self._exposure_started_monotonic = None
                    self._exposure_duration_s = None
                    self._publish_changed_locked()
            if publication_error is not None:
                self._finish_exposure_error(
                    session,
                    camera,
                    publication_error,
                    operation="publish frame",
                    duration_s=duration_s,
                    settings=settings,
                )

    def _finish_exposure_error(
        self,
        session: int,
        camera: CameraContract,
        error: Exception,
        *,
        operation: str,
        duration_s: float,
        settings: ExposureSettings,
    ) -> None:
        try:
            diagnostic = _async_exposure_error(
                operation,
                error,
                duration_s,
                settings,
            )
            if not isinstance(diagnostic, CameraError):
                diagnostic = _fallback_async_exposure_error(operation)
        except BaseException:
            diagnostic = _fallback_async_exposure_error(operation)
        try:
            disconnected = isinstance(error, CameraError) and _contains_not_connected(
                safe_camera_error(error, operation)
            )
        except BaseException:
            disconnected = False
        with self._condition:
            if not self._is_current_exposure_locked(session, camera):
                return
            self._state = CameraState.ERROR
            self._error = diagnostic
            self._clear_formal_image_locked()
            if disconnected:
                self._connected = False
                self._connection_target = False
                self._invalidate_heater_cache_locked()
            self._publish_changed_locked()

    def _preview_worker(
        self,
        session: int,
        stop: threading.Event,
        camera: CameraContract,
        callback: Callable[[Frame], None],
    ) -> None:
        try:
            while not stop.is_set():
                with self._acquisition_gate:
                    with self._condition:
                        if not self._is_current_preview_locked(session, camera):
                            return
                    capabilities = camera.capabilities
                    frame = camera.acquire(
                        self._PREVIEW_DURATION_S,
                        Roi(0, 0, capabilities.width, capabilities.height),
                        1,
                        ReadoutMode.RAW12,
                    )
                    with self._condition:
                        if not self._is_current_preview_locked(session, camera):
                            return
                        generation = self._image_workspace.publish_preview(frame)
                        published_frame = generation.frame
                        self._preview_frame = published_frame
                callback(published_frame)
        except BaseException as error:
            with self._condition:
                if not self._is_current_preview_locked(session, camera):
                    return
                self._state = CameraState.ERROR
                self._error = _camera_error(error, "acquire preview frame")
                self._clear_preview_image_locked()
                if _contains_not_connected(self._error):
                    self._connected = False
                    self._connection_target = False
                    self._invalidate_heater_cache_locked()
                self._publish_changed_locked()
        finally:
            with self._condition:
                if (
                    self._preview_session == session
                    and self._preview_thread is threading.current_thread()
                ):
                    self._clear_preview_image_locked()
                    self._preview_thread = None
                    self._preview_stop = None
                    self._condition.notify_all()

    def _require_available_camera_locked(self) -> CameraContract:
        if not self._connected or self._camera is None:
            raise CameraNotConnectedError("camera is not connected")
        if self._connecting:
            raise CameraBusyError("camera connection is changing")
        return self._camera

    def _read_heater_snapshot_locked(
        self,
        camera: CameraContract,
    ) -> HeaterSnapshot:
        supported = camera.heater_supported
        if not isinstance(supported, bool):
            raise CameraError("camera returned an invalid heater capability")
        if not supported:
            return self._cache_heater_snapshot_locked(
                camera,
                supported=False,
                maximum=None,
                level=None,
            )
        maximum = self._validated_heater_maximum(camera.heater_max_level)
        level = self._validated_heater_level(camera.heater_level, maximum)
        return self._cache_heater_snapshot_locked(
            camera,
            supported=True,
            maximum=maximum,
            level=level,
        )

    def _cache_heater_snapshot_locked(
        self,
        camera: CameraContract,
        *,
        supported: bool,
        maximum: int | None,
        level: int | None,
    ) -> HeaterSnapshot:
        self._heater_sequence += 1
        snapshot = HeaterSnapshot(
            connection_generation=self._connection_generation,
            sequence=self._heater_sequence,
            supported=supported,
            maximum=maximum,
            level=level,
        )
        self._heater_snapshot = snapshot
        self._heater_cache_camera = camera
        return snapshot

    def _invalidate_heater_cache_locked(self) -> None:
        self._heater_snapshot = None
        self._heater_cache_camera = None

    @staticmethod
    def _validated_heater_maximum(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CameraError("camera returned an invalid heater maximum")
        return value

    @staticmethod
    def _validated_heater_level(value: object, maximum: int) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > maximum
        ):
            raise CameraError("camera returned an invalid heater level")
        return value

    def _available_camera(self) -> CameraContract:
        with self._condition:
            return self._require_available_camera_locked()

    def _require_idle_locked(self) -> None:
        if self._state is not CameraState.IDLE:
            raise CameraBusyError(f"camera is {self._state.name.lower()}")

    def _require_exposure_startable_locked(self) -> None:
        if self._state is CameraState.IDLE:
            return
        if self._state is CameraState.ERROR and self._error is not None:
            return
        raise CameraBusyError(f"camera is {self._state.name.lower()}")

    @staticmethod
    def _validate_exposure_settings(
        settings: ExposureSettings,
        capabilities: CameraCapabilities,
    ) -> None:
        if not isinstance(settings, ExposureSettings):
            raise CameraInvalidValueError(
                "settings provider must return ExposureSettings"
            )
        if (
            isinstance(settings.binning, bool)
            or not isinstance(settings.binning, int)
            or not 1 <= settings.binning <= capabilities.max_bin
        ):
            raise CameraInvalidValueError(
                f"binning must be between 1 and {capabilities.max_bin}"
            )
        if not isinstance(settings.readout_mode, ReadoutMode):
            raise CameraInvalidValueError("unsupported readout mode")
        if (
            isinstance(settings.gain, bool)
            or not isinstance(settings.gain, int)
            or not 100 <= settings.gain <= 1650
        ):
            raise CameraInvalidValueError("gain must be between 100 and 1650")
        roi = settings.roi
        if not isinstance(roi, Roi):
            raise CameraInvalidValueError("ROI must be a Roi")
        if roi.x < 0 or roi.y < 0 or roi.width <= 0 or roi.height <= 0:
            raise CameraInvalidValueError(
                "ROI must have a non-negative origin and positive dimensions"
            )
        scale = settings.binning
        if (
            (roi.x + roi.width) * scale > capabilities.width
            or (roi.y + roi.height) * scale > capabilities.height
        ):
            raise CameraInvalidValueError("ROI extends outside the sensor")

    def _is_current_exposure_locked(
        self, session: int, camera: CameraContract
    ) -> bool:
        return (
            session == self._session_id
            and self._connected
            and self._camera is camera
            and self._state is CameraState.EXPOSING
        )

    def _is_current_preview_locked(
        self, session: int, camera: CameraContract
    ) -> bool:
        return (
            session == self._preview_session
            and self._connected
            and self._camera is camera
            and self._state is CameraState.IDLE
            and self._preview_stop is not None
            and not self._preview_stop.is_set()
        )

    def _invalidate_preview_locked(self) -> None:
        self._preview_session += 1
        if self._preview_stop is not None:
            self._preview_stop.set()
        self._clear_preview_image_locked()

    def _clear_preview_image_locked(self) -> None:
        self._preview_frame = None
        self._image_workspace.clear_preview()

    def _clear_formal_image_locked(self) -> None:
        self._frame = None
        self._image_workspace.clear_formal()
        self._image_ready = False
        self._exposure_started_monotonic = None
        self._exposure_duration_s = None

    def _publish_changed_locked(self) -> None:
        self._condition.notify_all()
        self.events.publish(self._snapshot_locked())

    def _snapshot_locked(self) -> CameraSnapshot:
        percent: int | None = None
        if self._state is CameraState.EXPOSING and self._exposure_duration_s is not None:
            if self._exposure_started_monotonic is None:
                percent = 0
            else:
                elapsed = max(
                    0.0,
                    time.monotonic() - self._exposure_started_monotonic,
                )
                percent = min(
                    100,
                    max(0, int(elapsed * 100 / self._exposure_duration_s)),
                )
        return CameraSnapshot(
            connected=self._connected,
            connecting=self._connecting,
            camera_state=self._state,
            image_ready=self._image_ready,
            percent_completed=percent,
            error=self._error,
        )

    @staticmethod
    def _validated_duration(duration_s: float, light: bool = True) -> float:
        if isinstance(duration_s, bool) or not isinstance(duration_s, (int, float)):
            raise CameraInvalidValueError("duration_s must be numeric")
        duration = float(duration_s)
        if not math.isfinite(duration):
            raise CameraInvalidValueError("duration_s must be finite")
        if duration < 0:
            raise CameraInvalidValueError("duration_s must be non-negative")
        exposure_us = round(duration * 1_000_000)
        if exposure_us > 3_600_000_000 or (light and exposure_us < 12):
            raise CameraInvalidValueError(
                "light duration_s must quantize to between 12 and "
                "3600000000 microseconds"
            )
        if not light:
            exposure_us = max(12, exposure_us)
        return exposure_us / 1_000_000

    @staticmethod
    def _start_thread(
        target: Callable[..., None],
        args: tuple[object, ...],
        name: str,
    ) -> threading.Thread:
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        thread.start()
        return thread


def _camera_error(error: BaseException, operation: str) -> CameraError:
    return safe_camera_error(error, operation)


def _async_exposure_error(
    operation: str,
    error: BaseException,
    duration_s: float,
    settings: ExposureSettings,
) -> CameraError:
    try:
        return _build_async_exposure_error(
            operation,
            error,
            duration_s,
            settings,
        )
    except BaseException:
        return _fallback_async_exposure_error(operation)


def _build_async_exposure_error(
    operation: str,
    error: BaseException,
    duration_s: float,
    settings: ExposureSettings,
) -> CameraError:
    cause = _safe_async_cause(error)
    error_text = safe_exception_text(
        error,
        fallback="",
        limit=_ASYNC_TEXT_SCAN_LIMIT,
        reject_line_breaks=False,
    )
    hresult = _safe_hresult(error_text)
    event = _safe_event(error_text)

    if isinstance(error, CameraNotConnectedError):
        summary = "camera disconnected"
        recovery = "Disconnect, reconnect the camera, then retry."
    elif event is not None:
        summary = _KNOWN_ASYNC_EVENTS[event]
        recovery = "Check the USB link, reconnect the camera, then retry."
    elif isinstance(error, CameraTimeoutError) or (
        hresult is not None and hresult == "0x8001011F"
    ):
        summary = "camera acquisition timed out"
        recovery = "Check the USB link, reconnect the camera, then retry."
    elif isinstance(error, CameraOperationCancelledError):
        summary = "camera acquisition was cancelled"
        recovery = "Start a new exposure when the camera is Idle."
    elif isinstance(error, CameraBusyError):
        summary = "camera backend was busy"
        recovery = "Wait for the camera to become Idle, then retry."
    elif isinstance(error, CameraInvalidValueError):
        summary = "camera rejected the frozen exposure settings"
        recovery = "Review ROI, binning, readout mode, and gain, then retry."
    else:
        summary = "unexpected camera backend failure"
        recovery = "Disconnect, reconnect the camera, then retry."

    fields = [
        "AsyncExposureFailure",
        f"operation={operation}",
        "CameraState=Error(5)",
        f"duration={_safe_duration(duration_s)}s",
        (
            "roi=("
            f"x={_safe_diagnostic_integer(settings.roi.x)},"
            f"y={_safe_diagnostic_integer(settings.roi.y)},"
            f"width={_safe_diagnostic_integer(settings.roi.width)},"
            f"height={_safe_diagnostic_integer(settings.roi.height)}"
            ")"
        ),
        f"binning={_safe_diagnostic_integer(settings.binning)}",
        f"readout={settings.readout_mode.name}",
        f"gain={_safe_diagnostic_integer(settings.gain)}",
        f"cause={cause}",
    ]
    if hresult is not None:
        fields.append(f"hresult={hresult}")
    if event is not None:
        fields.append(f"event={event}")
    fields.extend((f"summary={summary}", f"recovery={recovery}"))
    diagnostic = "; ".join(fields)
    if len(diagnostic) > _ASYNC_DIAGNOSTIC_LIMIT:
        return _fallback_async_exposure_error(operation)
    return CameraError(diagnostic)


def _fallback_async_exposure_error(operation: str) -> CameraError:
    safe_operation = (
        operation
        if operation in {"stop preview", "apply gain", "acquire frame"}
        else "camera operation"
    )
    return CameraError(
        "AsyncExposureFailure; "
        f"operation={safe_operation}; "
        "CameraState=Error(5); "
        "duration=bounded; "
        "roi=(x=bounded,y=bounded,width=bounded,height=bounded); "
        "binning=bounded; readout=bounded; gain=bounded; "
        "cause=Exception; "
        "summary=unexpected camera backend failure; "
        "recovery=Disconnect, reconnect the camera, then retry."
    )


def _normalized_hex(value: str) -> str:
    if value.casefold().startswith("0x"):
        return f"0x{value[2:].upper()}"
    return value


def _safe_hresult(error_text: str) -> str | None:
    for match in re.finditer(
        r"\bHRESULT\s+(0x[0-9a-fA-F]{8})\b",
        error_text,
        re.IGNORECASE,
    ):
        value = int(match.group(1), 16)
        if 0 <= value <= 0xFFFFFFFF:
            return f"0x{value:08X}"
    return None


def _safe_event(error_text: str) -> str | None:
    for match in re.finditer(
        r"\bevent\s*[:=]?\s*(0x[0-9a-fA-F]{4})\b",
        error_text,
        re.IGNORECASE,
    ):
        event = _normalized_hex(match.group(1))
        if event in _KNOWN_ASYNC_EVENTS:
            return event
    return None


def _safe_async_cause(error: BaseException) -> str:
    return safe_exception_identity(error)


def _safe_duration(duration_s: float) -> str:
    value = f"{duration_s:.6f}"
    return value if len(value) <= 24 else "bounded"


def _safe_diagnostic_integer(value: int) -> str:
    text = str(value)
    return text if re.fullmatch(r"-?\d{1,12}", text) else "bounded"


def _combined_error(message: str, errors: list[CameraError]) -> CameraError:
    if len(errors) == 1:
        return errors[0]
    return CameraCleanupError(message, tuple(errors))


def _append_error(errors: list[CameraError], error: BaseException) -> None:
    normalized = safe_camera_error(error, "camera cleanup")
    try:
        if isinstance(normalized, CameraCleanupError):
            for nested in normalized.errors:
                _append_error(errors, nested)
            return
    except BaseException:
        errors.append(CameraError("camera cleanup failed"))
        return
    errors.append(normalized)


def _unique_errors(errors: list[CameraError]) -> list[CameraError]:
    unique: list[CameraError] = []
    seen: set[tuple[str, str]] = set()
    for error in errors:
        flattened: list[CameraError] = []
        _append_error(flattened, error)
        for nested in flattened:
            key = (
                safe_exception_identity(nested),
                safe_exception_text(
                    nested,
                    fallback="camera cleanup failed",
                ),
            )
            if key not in seen:
                seen.add(key)
                unique.append(nested)
    return unique


def _contains_not_connected(error: CameraError) -> bool:
    try:
        if isinstance(error, CameraNotConnectedError):
            return True
        if isinstance(error, CameraCleanupError):
            return any(_contains_not_connected(nested) for nested in error.errors)
    except BaseException:
        return False
    return False


def _is_session_ending_error(error: CameraError) -> bool:
    try:
        if isinstance(
            error,
            (
                CameraNotConnectedError,
                CameraTimeoutError,
                CameraOwnerProtocolError,
                CameraOwnerTerminationError,
            ),
        ):
            return True
        if isinstance(error, CameraCleanupError):
            return any(
                _is_session_ending_error(nested)
                for nested in error.errors
            )
    except BaseException:
        return False
    return False
