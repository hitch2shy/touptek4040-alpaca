"""Single-owner camera lifecycle, exposure, and preview coordination."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum

from .camera_contract import (
    CameraCapabilities,
    CameraContract,
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
    CameraOperationCancelledError,
)
from .event_broker import EventBroker


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
        self.events: EventBroker[CameraSnapshot] = EventBroker()

    def connect_async(self) -> None:
        with self._condition:
            if self._connected and not self._connecting:
                return
            if self._connecting and self._connection_target is True:
                return
            if not self._connecting:
                self._lifecycle_errors.clear()
            self._connection_generation += 1
            generation = self._connection_generation
            self._connection_target = True
            self._connecting = True
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
            if not self._connecting:
                self._lifecycle_errors.clear()
            self._connection_generation += 1
            generation = self._connection_generation
            self._connection_target = False
            self._connecting = True
            self._session_id += 1
            self._invalidate_preview_locked()
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

    def start_exposure(self, duration_s: float, light: bool) -> None:
        duration = self._validated_duration(duration_s)
        del light  # The camera has no mechanical shutter.
        with self._condition:
            camera = self._require_available_camera_locked()
            self._require_idle_locked()
            preview_thread = self._preview_thread
            had_preview = preview_thread is not None and preview_thread.is_alive()
            self._invalidate_preview_locked()
            self._session_id += 1
            session = self._session_id
            self._clear_formal_image_locked()
            self._state = CameraState.EXPOSING
            self._error = None
            self._exposure_duration_s = duration
            self._publish_changed_locked()
        self._exposure_thread = self._start_thread(
            target=self._exposure_worker,
            args=(session, camera, duration, preview_thread if had_preview else None),
            name=f"camera-exposure-{session}",
        )

    def abort_exposure(self) -> None:
        with self._condition:
            camera = self._require_available_camera_locked()
            if self._state is not CameraState.EXPOSING:
                return
            self._session_id += 1
            abort_session = self._session_id
            self._state = CameraState.IDLE
            self._error = None
            self._clear_formal_image_locked()
            self._publish_changed_locked()
        try:
            camera.abort()
        except Exception as error:
            translated = _camera_error(error, "abort exposure")
            with self._condition:
                if (
                    self._session_id == abort_session
                    and self._connected
                    and self._camera is camera
                ):
                    self._state = CameraState.ERROR
                    self._error = translated
                    self._publish_changed_locked()
            raise translated from error

    def start_preview(self, callback: Callable[[Frame], None]) -> None:
        if not callable(callback):
            raise CameraInvalidValueError("preview callback must be callable")
        with self._condition:
            camera = self._require_available_camera_locked()
            self._require_idle_locked()
            if self._preview_thread is not None and self._preview_thread.is_alive():
                raise CameraBusyError("preview is already running")
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
                self._camera = None
                self._connection_target = False
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
            except Exception as caught:
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
    def preview_frame(self) -> Frame | None:
        with self._condition:
            return self._preview_frame

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

    def _connect_worker(self, generation: int) -> None:
        camera: CameraContract | None = None
        error: CameraError | None = None
        with self._lifecycle_lock:
            with self._condition:
                current = (
                    generation == self._connection_generation
                    and self._connection_target is True
                )
                pending_errors = tuple(self._lifecycle_errors)
                if current and pending_errors:
                    self._camera = None
                    self._connected = False
                    self._connecting = False
                    self._state = CameraState.ERROR
                    self._error = _combined_error(
                        "previous camera owner cleanup failed",
                        list(pending_errors),
                    )
                    self._lifecycle_errors.clear()
                    self._publish_changed_locked()
                    return
                if not current:
                    return
            try:
                camera = self._factory(self._source)
                camera.connect()
            except Exception as caught:
                error = _camera_error(caught, "connect camera")
            with self._condition:
                current = (
                    generation == self._connection_generation
                    and self._connection_target is True
                )
                if current and error is None and camera is not None:
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
                    self._connecting = False
                    self._state = CameraState.ERROR
                    self._error = error or CameraError("connect camera failed")
                    self._lifecycle_errors.clear()
                    self._publish_changed_locked()
            if not current and camera is not None:
                cleanup_errors: list[CameraError] = []
                try:
                    camera.disconnect()
                except Exception as cleanup_error:
                    _append_error(
                        cleanup_errors,
                        _camera_error(
                            cleanup_error,
                            "disconnect superseded camera owner",
                        ),
                    )
                if cleanup_errors:
                    with self._condition:
                        self._lifecycle_errors.extend(cleanup_errors)

    def _disconnect_worker(self, generation: int) -> None:
        errors: list[CameraError] = []
        with self._lifecycle_lock:
            with self._condition:
                camera = self._camera
            if camera is not None:
                try:
                    camera.abort()
                except Exception as error:
                    _append_error(
                        errors,
                        _camera_error(error, "abort before disconnect"),
                    )
                try:
                    camera.disconnect()
                except Exception as error:
                    _append_error(
                        errors,
                        _camera_error(error, "disconnect camera"),
                    )
            with self._condition:
                current = (
                    generation == self._connection_generation
                    and self._connection_target is False
                )
                if current:
                    all_errors = [*self._lifecycle_errors, *errors]
                    self._lifecycle_errors.clear()
                    self._camera = None
                    self._connected = False
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
                    self._lifecycle_errors.extend(errors)

    def _exposure_worker(
        self,
        session: int,
        camera: CameraContract,
        duration_s: float,
        preview_thread: threading.Thread | None,
    ) -> None:
        if preview_thread is not None:
            error = self._stop_preview_thread(camera, preview_thread)
            if error is not None:
                self._finish_exposure_error(session, camera, error)
                return
        with self._acquisition_gate:
            with self._condition:
                if not self._is_current_exposure_locked(session, camera):
                    return
            try:
                capabilities = camera.capabilities
            except Exception as error:
                self._finish_exposure_error(session, camera, error)
                return
            with self._condition:
                if not self._is_current_exposure_locked(session, camera):
                    return
                self._exposure_started_monotonic = time.monotonic()
                self._publish_changed_locked()
            try:
                frame = camera.acquire(
                    duration_s,
                    Roi(0, 0, capabilities.width, capabilities.height),
                    1,
                    ReadoutMode.RAW12,
                )
            except Exception as error:
                self._finish_exposure_error(session, camera, error)
                return
            with self._condition:
                if not self._is_current_exposure_locked(session, camera):
                    return
                self._frame = frame
                self._image_ready = True
                self._state = CameraState.IDLE
                self._error = None
                self._exposure_started_monotonic = None
                self._exposure_duration_s = None
                self._publish_changed_locked()

    def _finish_exposure_error(
        self,
        session: int,
        camera: CameraContract,
        error: Exception,
    ) -> None:
        with self._condition:
            if not self._is_current_exposure_locked(session, camera):
                return
            self._state = CameraState.ERROR
            self._error = _camera_error(error, "acquire frame")
            self._clear_formal_image_locked()
            if _contains_not_connected(self._error):
                self._connected = False
                self._camera = None
                self._connection_target = False
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
                        self._preview_frame = frame
                callback(frame)
        except Exception as error:
            with self._condition:
                if not self._is_current_preview_locked(session, camera):
                    return
                self._state = CameraState.ERROR
                self._error = _camera_error(error, "acquire preview frame")
                if _contains_not_connected(self._error):
                    self._connected = False
                    self._camera = None
                    self._connection_target = False
                self._publish_changed_locked()
        finally:
            with self._condition:
                if (
                    self._preview_session == session
                    and self._preview_thread is threading.current_thread()
                ):
                    self._preview_thread = None
                    self._preview_stop = None
                    self._condition.notify_all()

    def _require_available_camera_locked(self) -> CameraContract:
        if not self._connected or self._camera is None:
            raise CameraNotConnectedError("camera is not connected")
        if self._connecting:
            raise CameraBusyError("camera connection is changing")
        return self._camera

    def _available_camera(self) -> CameraContract:
        with self._condition:
            return self._require_available_camera_locked()

    def _require_idle_locked(self) -> None:
        if self._state is not CameraState.IDLE:
            raise CameraBusyError(f"camera is {self._state.name.lower()}")

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

    def _clear_formal_image_locked(self) -> None:
        self._frame = None
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
    def _validated_duration(duration_s: float) -> float:
        if isinstance(duration_s, bool) or not isinstance(duration_s, (int, float)):
            raise CameraInvalidValueError("duration_s must be numeric")
        duration = float(duration_s)
        if not math.isfinite(duration):
            raise CameraInvalidValueError("duration_s must be finite")
        exposure_us = round(duration * 1_000_000)
        if not 12 <= exposure_us <= 3_600_000_000:
            raise CameraInvalidValueError(
                "duration_s must quantize to between 12 and "
                "3600000000 microseconds"
            )
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


def _camera_error(error: Exception, operation: str) -> CameraError:
    if isinstance(error, CameraError):
        return error
    return CameraError(f"{operation} failed: {error}")


def _combined_error(message: str, errors: list[CameraError]) -> CameraError:
    if len(errors) == 1:
        return errors[0]
    return CameraCleanupError(message, tuple(errors))


def _append_error(errors: list[CameraError], error: CameraError) -> None:
    if isinstance(error, CameraCleanupError):
        for nested in error.errors:
            _append_error(errors, nested)
    else:
        errors.append(error)


def _contains_not_connected(error: CameraError) -> bool:
    if isinstance(error, CameraNotConnectedError):
        return True
    if isinstance(error, CameraCleanupError):
        return any(_contains_not_connected(nested) for nested in error.errors)
    return False
