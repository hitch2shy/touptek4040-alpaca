"""Synchronous main-process proxy for one supervised camera owner generation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
import math
import multiprocessing
from pathlib import Path
import tempfile
import threading
import time
from typing import Any

import numpy as np

from .camera_contract import (
    AcquisitionControl,
    CameraCapabilities,
    Frame,
    ReadoutMode,
    Roi,
)
from .camera_owner import OwnerStartupSpec, camera_owner_main
from .config import OwnerConfig
from .errors import (
    CameraBusyError,
    CameraError,
    CameraInvalidValueError,
    CameraNotConnectedError,
    CameraOperationCancelledError,
    CameraOwnerProtocolError,
    CameraTimeoutError,
    safe_camera_error,
)
from .owner_protocol import (
    MAX_EXPOSURE_DURATION_S,
    MAX_UINT64,
    MIN_EXPOSURE_DURATION_S,
    PROTOCOL_VERSION,
    FrameDescriptor,
    OwnerProtocolError,
    OwnerRequest,
    OwnerResponse,
    OwnerResponseTracker,
    decode_acquire_start_ack,
    encode_request,
)
from .shared_frames import SharedFrameError, SharedFrameRegistry


_PROCESS_TERMINATION_ALLOWANCE_S = 0.25
@dataclass(slots=True)
class OwnerProcessSession:
    """The exact process and private endpoints belonging to one generation."""

    process: Any
    command_connection: Any
    control_connection: Any


class OwnerProcessFactory:
    """Create Windows-spawn-safe owner processes without importing the SDK."""

    def __init__(self, context: Any | None = None) -> None:
        self._context = context or multiprocessing.get_context("spawn")

    def start(
        self,
        startup_spec: OwnerStartupSpec,
        generation: int,
        registry_path: Path,
    ) -> OwnerProcessSession:
        command_parent, command_child = self._context.Pipe(duplex=True)
        control_parent, control_child = self._context.Pipe(duplex=True)
        process = self._context.Process(
            target=camera_owner_main,
            args=(
                command_child,
                control_child,
                startup_spec,
                generation,
                str(registry_path),
            ),
            name=f"touptek4040-camera-owner-{generation}",
        )
        try:
            process.start()
        except BaseException:
            command_parent.close()
            command_child.close()
            control_parent.close()
            control_child.close()
            raise
        command_child.close()
        control_child.close()
        return OwnerProcessSession(process, command_parent, control_parent)


class OwnerSupervisor:
    """Own one command stream and replace its exact child after transport failure."""

    def __init__(
        self,
        startup_spec: OwnerStartupSpec,
        owner_config: OwnerConfig,
        registry_path: str | Path,
        *,
        process_factory: OwnerProcessFactory | Any | None = None,
        registry_factory: Callable[[int, Path], Any] = SharedFrameRegistry,
        termination_allowance_s: float = _PROCESS_TERMINATION_ALLOWANCE_S,
    ) -> None:
        if not isinstance(startup_spec, OwnerStartupSpec):
            raise TypeError("startup_spec must be an OwnerStartupSpec")
        if not isinstance(owner_config, OwnerConfig):
            raise TypeError("owner_config must be an OwnerConfig")
        if (
            isinstance(termination_allowance_s, bool)
            or not isinstance(termination_allowance_s, (int, float))
            or not math.isfinite(float(termination_allowance_s))
            or termination_allowance_s <= 0
        ):
            raise ValueError("termination_allowance_s must be a positive finite number")
        self._startup_spec = startup_spec
        self._config = owner_config
        self._registry_path = Path(registry_path)
        self._process_factory = process_factory or OwnerProcessFactory()
        self._registry_factory = registry_factory
        self._termination_allowance_s = float(termination_allowance_s)
        self._command_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._generation = 0
        self._next_request_id = 0
        self._session: OwnerProcessSession | Any | None = None
        self._registry: Any | None = None
        self._response_tracker: OwnerResponseTracker | None = None
        self._connected = False
        self._exposing = False
        self._confirmed_heater_level: int | None = None
        self._closed = False
        self._last_terminated_pid: int | None = None
        self._last_exit_code: int | None = None
        self._last_cleanup_error: CameraError | None = None
        self._start_idle_owner()

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def connected(self) -> bool:
        with self._state_lock:
            return self._connected

    @property
    def exposing(self) -> bool:
        with self._state_lock:
            return self._exposing

    @property
    def confirmed_heater_level(self) -> int | None:
        with self._state_lock:
            return self._confirmed_heater_level

    @property
    def last_terminated_pid(self) -> int | None:
        return self._last_terminated_pid

    @property
    def last_exit_code(self) -> int | None:
        return self._last_exit_code

    @property
    def last_cleanup_error(self) -> CameraError | None:
        return self._last_cleanup_error

    def deadline_for(
        self,
        operation: str,
        *,
        actual_duration_s: float | None = None,
    ) -> float:
        if operation == "handshake":
            return self._config.startup_timeout_s
        if operation == "connect":
            return self._config.connect_timeout_s
        if operation == "abort":
            return self._config.abort_timeout_s
        if operation == "disconnect":
            return self._config.disconnect_timeout_s
        if operation == "acquire":
            duration = _validated_exposure_duration(actual_duration_s)
            return (
                duration * self._config.exposure_multiplier
                + self._config.exposure_overhead_s
            )
        return self._config.command_timeout_s

    def connect(self) -> None:
        with self._command_lock:
            if self._connected:
                return
            response = self._request_locked(
                "connect", {}, self.deadline_for("connect")
            )
            result = self.validate(
                lambda: _require_result_object(response, {"connected"})
            )
            if result["connected"] is not True:
                self._protocol_failure("Connect response did not confirm connection")
            with self._state_lock:
                self._connected = True
                self._confirmed_heater_level = None

    def disconnect(self) -> None:
        with self._command_lock:
            if not self._connected:
                return
            response = self._request_locked(
                "disconnect", {}, self.deadline_for("disconnect")
            )
            result = self.validate(
                lambda: _require_result_object(response, {"connected"})
            )
            if result["connected"] is not False:
                self._protocol_failure("Disconnect response did not confirm closure")
            with self._state_lock:
                self._connected = False
                self._confirmed_heater_level = None

    def request(
        self,
        operation: str,
        arguments: Mapping[str, object],
        *,
        require_connected: bool = True,
    ) -> OwnerResponse:
        with self._command_lock:
            if require_connected:
                self._require_connected()
            return self._request_locked(
                operation,
                arguments,
                self.deadline_for(operation),
            )

    def acquire(
        self,
        duration_s: float,
        roi: Roi,
        binning: int,
        readout_mode: ReadoutMode,
        control: AcquisitionControl | None,
    ) -> Frame:
        actual_duration_s = _validated_exposure_duration(duration_s)
        if not isinstance(roi, Roi):
            raise CameraInvalidValueError("roi must be a Roi")
        if isinstance(binning, bool) or not isinstance(binning, int):
            raise CameraInvalidValueError("binning must be an integer")
        if not isinstance(readout_mode, ReadoutMode):
            raise CameraInvalidValueError("readout_mode must be a ReadoutMode")
        with self._command_lock:
            self._require_connected()
            acquire_session = self._session

            def claim() -> None:
                with self._state_lock:
                    if not self._connected:
                        raise CameraNotConnectedError("camera is not connected")
                    self._exposing = True

            if control is None:
                claim()
            elif not control.commit_start(claim):
                raise CameraOperationCancelledError(
                    "exposure was cancelled before owner acquisition started"
                )
            try:
                response, acknowledged_duration_s = (
                    self._acquire_request_locked(
                        {
                            "duration_s": actual_duration_s,
                            "roi": {
                                "x": roi.x,
                                "y": roi.y,
                                "width": roi.width,
                                "height": roi.height,
                            },
                            "binning": binning,
                            "readout_mode": int(readout_mode),
                        }
                    )
                )
                if self._session is not acquire_session:
                    raise CameraOwnerProtocolError(
                        "owner generation failed before frame reconstruction"
                    )
                descriptor, frame_values = self._frame_response(
                    response,
                    acknowledged_duration_s=acknowledged_duration_s,
                    failed_session=acquire_session,
                )
                try:
                    pixels = self._registry.consume(descriptor)
                except (SharedFrameError, OSError, ValueError) as error:
                    self._protocol_failure(
                        f"owner frame could not be consumed: {error}",
                        failed_session=acquire_session,
                    )
                if self._session is not acquire_session:
                    raise CameraOwnerProtocolError(
                        "owner generation failed before frame release"
                    )
                release = self._request_locked(
                    "release_frame",
                    {"descriptor": _descriptor_payload(descriptor)},
                    self.deadline_for("release_frame"),
                )
                self.validate(lambda: _require_result_object(release, set()))
                if self._session is not acquire_session:
                    raise CameraOwnerProtocolError(
                        "owner generation failed before frame completion"
                    )
                return Frame(pixels=pixels, **frame_values)
            finally:
                with self._state_lock:
                    self._exposing = False

    def abort(self) -> None:
        with self._state_lock:
            self._require_connected()
        session = self._session
        if session is None:
            raise CameraNotConnectedError("camera owner is unavailable")
        deadline = time.monotonic() + self.deadline_for("abort")
        try:
            session.control_connection.send({"operation": "abort"})
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._fail_owner(
                        CameraTimeoutError("camera owner abort timed out"),
                        failed_session=session,
                    )
                if session.control_connection.poll(remaining):
                    break
                if not session.process.is_alive():
                    self._protocol_failure(
                        "camera owner exited during abort",
                        failed_session=session,
                    )
            payload = session.control_connection.recv()
            if (
                type(payload) is not dict
                or payload.get("operation") != "abort"
                or type(payload.get("ok")) is not bool
                or set(payload)
                not in ({"operation", "ok"}, {"operation", "ok", "error"})
            ):
                self._protocol_failure(
                    "camera owner returned an invalid abort response",
                    failed_session=session,
                )
            if payload["ok"] is not True:
                message = payload.get("error")
                raise CameraError(
                    message if isinstance(message, str) and message else "camera abort failed"
                )
        except (EOFError, OSError) as error:
            self._protocol_failure(
                f"camera owner abort transport failed: {error}",
                failed_session=session,
            )

    def get_heater_level(self) -> int:
        source_session = self._session
        response = self.request("get_heater_level", {})
        value = self.validate(
            lambda: _single_int_result(response, minimum=0)
        )
        with self._state_lock:
            if self._session is not source_session or not self._connected:
                raise CameraNotConnectedError(
                    "heater response belongs to a failed owner generation"
                )
            self._confirmed_heater_level = value
        return value

    def set_heater_level(self, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CameraInvalidValueError(
                "heater level must be a non-negative integer"
            )
        with self._state_lock:
            self._require_connected()
            if self._exposing:
                raise CameraBusyError(
                    "heater level cannot be changed while an exposure is active"
                )
            source_session = self._session
        response = self.request("set_heater_level", {"value": value})
        self.validate(lambda: _require_result_object(response, set()))
        with self._state_lock:
            if self._session is not source_session or not self._connected:
                raise CameraNotConnectedError(
                    "heater response belongs to a failed owner generation"
                )
            self._confirmed_heater_level = value

    def close(self) -> None:
        with self._command_lock:
            if self._closed:
                return
            self._closed = True
            session = self._session
            if session is None:
                return
            try:
                self._exchange(
                    "shutdown",
                    {},
                    self._config.disconnect_timeout_s,
                )
            except CameraError:
                pass
            finally:
                with self._state_lock:
                    self._connected = False
                    self._confirmed_heater_level = None
                self._finish_or_terminate_exact_process()
                self._cleanup_failed_generation_safely()

    def _start_idle_owner(self) -> None:
        last_error: CameraError | None = None
        for _attempt in range(2):
            if self._generation >= MAX_UINT64:
                raise CameraOwnerProtocolError("owner generations are exhausted")
            self._generation += 1
            self._next_request_id = 0
            self._response_tracker = OwnerResponseTracker()
            self._registry = self._registry_factory(
                self._generation, self._registry_path
            )
            try:
                self._session = self._process_factory.start(
                    self._startup_spec,
                    self._generation,
                    self._registry_path,
                )
                response = self._exchange(
                    "handshake",
                    {},
                    self._config.startup_timeout_s,
                )
                _require_result_object(response, {"pid", "generation"})
                result = response.result
                process_pid = self._session.process.pid
                if (
                    type(result["pid"]) is not int
                    or result["pid"] != process_pid
                    or result["generation"] != self._generation
                ):
                    raise CameraOwnerProtocolError(
                        "owner handshake identity does not match the exact child"
                    )
                with self._state_lock:
                    self._connected = False
                    self._exposing = False
                    self._confirmed_heater_level = None
                return
            except CameraError as error:
                last_error = error
                self._terminate_exact_process()
                self._cleanup_failed_generation_safely()
        assert last_error is not None
        raise last_error

    def _request_locked(
        self,
        operation: str,
        arguments: Mapping[str, object],
        timeout_s: float,
    ) -> OwnerResponse:
        failed_session = self._session
        try:
            return self._exchange(operation, arguments, timeout_s)
        except CameraTimeoutError as error:
            self._fail_owner(error, failed_session=failed_session)
        except CameraOwnerProtocolError as error:
            self._fail_owner(error, failed_session=failed_session)
        raise AssertionError("unreachable")

    def _acquire_request_locked(
        self,
        arguments: Mapping[str, object],
    ) -> tuple[OwnerResponse, float]:
        failed_session = self._session
        try:
            return self._acquire_exchange(arguments)
        except CameraTimeoutError as error:
            self._fail_owner(error, failed_session=failed_session)
        except CameraOwnerProtocolError as error:
            self._fail_owner(error, failed_session=failed_session)
        raise AssertionError("unreachable")

    def _acquire_exchange(
        self,
        arguments: Mapping[str, object],
    ) -> tuple[OwnerResponse, float]:
        session = self._session
        tracker = self._response_tracker
        if session is None or tracker is None:
            raise CameraOwnerProtocolError(
                "camera owner session is unavailable"
            )
        if self._next_request_id > MAX_UINT64:
            raise CameraOwnerProtocolError("owner request IDs are exhausted")
        request = OwnerRequest(
            PROTOCOL_VERSION,
            self._generation,
            self._next_request_id,
            "acquire",
            arguments,
        )
        self._next_request_id += 1
        start_deadline = (
            time.monotonic() + self._config.command_timeout_s
        )
        try:
            session.command_connection.send(encode_request(request))
            start_payload = self._receive_until(
                session,
                start_deadline,
                "acquire start acknowledgement",
            )
            try:
                acknowledgement = decode_acquire_start_ack(
                    start_payload, request
                )
            except OwnerProtocolError as error:
                raise CameraOwnerProtocolError(
                    "camera owner returned an invalid acquire start "
                    f"acknowledgement: {error}"
                ) from error
            final_payload = self._receive_until(
                session,
                time.monotonic()
                + self.deadline_for(
                    "acquire",
                    actual_duration_s=acknowledgement.actual_duration_s,
                ),
                "acquire completion",
            )
        except CameraError:
            raise
        except (EOFError, OSError) as error:
            raise CameraOwnerProtocolError(
                f"camera owner transport failed during acquire: {error}"
            ) from error
        try:
            response = tracker.decode(final_payload, request)
        except OwnerProtocolError as error:
            raise CameraOwnerProtocolError(
                f"camera owner returned an invalid acquire response: {error}"
            ) from error
        if response.error is not None:
            if response.error.code == "invalid_request":
                raise CameraInvalidValueError(response.error.message)
            raise CameraError(response.error.message)
        return response, acknowledgement.actual_duration_s

    def _receive_until(
        self,
        session: OwnerProcessSession | Any,
        deadline: float,
        phase: str,
    ) -> object:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CameraTimeoutError(
                    f"camera owner {phase} timed out"
                )
            if session.command_connection.poll(remaining):
                return session.command_connection.recv()
            if not session.process.is_alive():
                raise CameraOwnerProtocolError(
                    f"camera owner exited during {phase}"
                )

    def _exchange(
        self,
        operation: str,
        arguments: Mapping[str, object],
        timeout_s: float,
    ) -> OwnerResponse:
        session = self._session
        tracker = self._response_tracker
        if session is None or tracker is None:
            raise CameraOwnerProtocolError("camera owner session is unavailable")
        if self._next_request_id > MAX_UINT64:
            raise CameraOwnerProtocolError("owner request IDs are exhausted")
        request = OwnerRequest(
            PROTOCOL_VERSION,
            self._generation,
            self._next_request_id,
            operation,
            arguments,
        )
        self._next_request_id += 1
        deadline = time.monotonic() + timeout_s
        try:
            session.command_connection.send(encode_request(request))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CameraTimeoutError(
                        f"camera owner {operation} timed out after {timeout_s:g} seconds"
                    )
                if session.command_connection.poll(remaining):
                    break
                if not session.process.is_alive():
                    raise CameraOwnerProtocolError(
                        f"camera owner exited during {operation}"
                    )
            payload = session.command_connection.recv()
        except CameraError:
            raise
        except (EOFError, OSError) as error:
            raise CameraOwnerProtocolError(
                f"camera owner transport failed during {operation}: {error}"
            ) from error
        try:
            response = tracker.decode(payload, request)
        except OwnerProtocolError as error:
            raise CameraOwnerProtocolError(
                f"camera owner returned an invalid {operation} response: {error}"
            ) from error
        if response.error is not None:
            if response.error.code == "invalid_request":
                raise CameraInvalidValueError(response.error.message)
            raise CameraError(response.error.message)
        return response

    def _frame_response(
        self,
        response: OwnerResponse,
        *,
        acknowledged_duration_s: float,
        failed_session: OwnerProcessSession | Any | None = None,
    ) -> tuple[FrameDescriptor, dict[str, object]]:
        result = _require_result_object(response, {"descriptor", "frame"})
        descriptor_payload = result["descriptor"]
        frame_payload = result["frame"]
        try:
            descriptor = _descriptor_from_payload(descriptor_payload)
            if descriptor.owner_generation != self._generation:
                raise OwnerProtocolError(
                    "frame descriptor belongs to a stale generation"
                )
            frame_values = _frame_values_from_payload(frame_payload)
            if frame_values["duration_s"] != acknowledged_duration_s:
                raise OwnerProtocolError(
                    "frame duration does not match start acknowledgement"
                )
        except (OwnerProtocolError, TypeError, ValueError) as error:
            self._protocol_failure(
                f"owner frame response is invalid: {error}",
                failed_session=failed_session,
            )
        return descriptor, frame_values

    def _protocol_failure(
        self,
        message: str,
        *,
        failed_session: OwnerProcessSession | Any | None = None,
    ) -> None:
        self._fail_owner(
            CameraOwnerProtocolError(message),
            failed_session=failed_session,
        )

    def validate(self, validator: Callable[[], Any]) -> Any:
        """Apply semantic response validation under generation supervision."""
        try:
            return validator()
        except CameraOwnerProtocolError as error:
            self._fail_owner(error)
        raise AssertionError("unreachable")

    def _fail_owner(
        self,
        error: CameraError,
        *,
        failed_session: OwnerProcessSession | Any | None = None,
    ) -> None:
        if failed_session is not None and self._session is not failed_session:
            raise error
        try:
            self._terminate_exact_process()
        finally:
            with self._state_lock:
                self._connected = False
                self._exposing = False
                self._confirmed_heater_level = None
        self._cleanup_failed_generation_safely()
        if not self._closed:
            self._start_idle_owner()
        raise error

    def _terminate_exact_process(self) -> None:
        session = self._session
        if session is None:
            return
        process = session.process
        self._last_terminated_pid = process.pid
        for connection in (
            session.command_connection,
            session.control_connection,
        ):
            try:
                connection.close()
            except (EOFError, OSError):
                pass
        try:
            process.terminate()
        except (OSError, ValueError):
            pass
        process.join(self._termination_allowance_s)
        if process.is_alive():
            process.kill()
            process.join(self._termination_allowance_s)
        self._last_exit_code = process.exitcode
        self._session = None
        self._response_tracker = None

    def _finish_or_terminate_exact_process(self) -> None:
        session = self._session
        if session is None:
            return
        process = session.process
        process.join(self._termination_allowance_s)
        if process.is_alive():
            self._terminate_exact_process()
            return
        self._last_terminated_pid = process.pid
        self._last_exit_code = process.exitcode
        for connection in (
            session.command_connection,
            session.control_connection,
        ):
            try:
                connection.close()
            except (EOFError, OSError):
                pass
        self._session = None
        self._response_tracker = None

    def _cleanup_failed_generation(self) -> None:
        registry = self._registry
        if registry is None:
            return
        cleanup_generation = getattr(registry, "cleanup_generation", None)
        if callable(cleanup_generation):
            cleanup_generation(self._generation)
            return
        # Constructing a registry after the exact child has exited invokes the
        # registry's dead-owner cleanup and only unlinks product-owned names.
        self._registry_factory(self._generation, self._registry_path)

    def _cleanup_failed_generation_safely(self) -> None:
        try:
            self._cleanup_failed_generation()
        except BaseException as error:
            self._last_cleanup_error = safe_camera_error(
                error, "clean failed owner generation"
            )

    def _require_connected(self) -> None:
        if not self._connected:
            raise CameraNotConnectedError("camera is not connected")


class CameraOwnerProxy:
    """CameraContract implementation backed by a supervised owner process."""

    def __init__(
        self,
        serial: str | None = None,
        owner_config: OwnerConfig | None = None,
        runtime_directory: str | Path | None = None,
        *,
        startup_spec: OwnerStartupSpec | None = None,
        supervisor: OwnerSupervisor | None = None,
        process_factory: OwnerProcessFactory | Any | None = None,
    ) -> None:
        if supervisor is not None:
            if any(
                value is not None
                for value in (
                    serial,
                    owner_config,
                    runtime_directory,
                    startup_spec,
                    process_factory,
                )
            ):
                raise TypeError(
                    "supervisor cannot be combined with owner construction arguments"
                )
            self._supervisor = supervisor
            return
        config = owner_config or OwnerConfig()
        spec = startup_spec or OwnerStartupSpec(serial=serial)
        base = (
            Path(runtime_directory)
            if runtime_directory is not None
            else Path(tempfile.gettempdir())
        )
        self._supervisor = OwnerSupervisor(
            spec,
            config,
            base / "touptek4040-shared-frames.json",
            process_factory=process_factory,
        )

    @property
    def connected(self) -> bool:
        return self._supervisor.connected

    @property
    def exposing(self) -> bool:
        return self._supervisor.exposing

    @property
    def owner_generation(self) -> int:
        return self._supervisor.generation

    @property
    def last_terminated_pid(self) -> int | None:
        return self._supervisor.last_terminated_pid

    @property
    def last_owner_exit_code(self) -> int | None:
        return self._supervisor.last_exit_code

    @property
    def last_cleanup_error(self) -> CameraError | None:
        return self._supervisor.last_cleanup_error

    @property
    def confirmed_heater_level(self) -> int | None:
        return self._supervisor.confirmed_heater_level

    @property
    def capabilities(self) -> CameraCapabilities:
        response = self._supervisor.request(
            "capabilities", {}, require_connected=False
        )
        result = self._supervisor.validate(
            lambda: _require_result_object(
                response,
                {
                    "width",
                    "height",
                    "pixel_size_x",
                    "pixel_size_y",
                    "readout_modes",
                    "max_bin",
                    "can_abort",
                    "can_stop",
                },
            )
        )
        try:
            modes = result["readout_modes"]
            if type(modes) is not list or not all(
                isinstance(mode, str) and mode for mode in modes
            ):
                raise ValueError("readout_modes must be a list of strings")
            if any(type(result[name]) is not int for name in ("width", "height", "max_bin")):
                raise ValueError("capability dimensions must be integers")
            if any(type(result[name]) not in (int, float) for name in ("pixel_size_x", "pixel_size_y")):
                raise ValueError("pixel sizes must be finite numbers")
            if any(type(result[name]) is not bool for name in ("can_abort", "can_stop")):
                raise ValueError("abort/stop capabilities must be booleans")
            return CameraCapabilities(
                result["width"],
                result["height"],
                float(result["pixel_size_x"]),
                float(result["pixel_size_y"]),
                tuple(modes),
                result["max_bin"],
                result["can_abort"],
                result["can_stop"],
            )
        except (TypeError, ValueError) as error:
            self._supervisor._protocol_failure(
                f"owner capabilities response is invalid: {error}"
            )

    def connect(self) -> None:
        self._supervisor.connect()

    def disconnect(self) -> None:
        self._supervisor.disconnect()

    def close(self) -> None:
        self._supervisor.close()

    def acquire(
        self,
        duration_s: float,
        roi: Roi,
        binning: int,
        readout_mode: ReadoutMode,
        control: AcquisitionControl | None = None,
    ) -> Frame:
        return self._supervisor.acquire(
            duration_s, roi, binning, readout_mode, control
        )

    def abort(self) -> None:
        self._supervisor.abort()

    @property
    def gain(self) -> int:
        response = self._supervisor.request("get_gain", {})
        return self._supervisor.validate(
            lambda: _single_int_result(response)
        )

    @gain.setter
    def gain(self, value: int) -> None:
        _require_exact_int(value, "gain")
        response = self._supervisor.request("set_gain", {"value": value})
        self._supervisor.validate(
            lambda: _require_result_object(response, set())
        )

    @property
    def temperature_c(self) -> float:
        response = self._supervisor.request("get_temperature", {})
        return self._supervisor.validate(
            lambda: _single_float_result(response)
        )

    @property
    def target_temperature_c(self) -> float:
        response = self._supervisor.request("get_target_temperature", {})
        return self._supervisor.validate(
            lambda: _single_float_result(response)
        )

    @target_temperature_c.setter
    def target_temperature_c(self, value: float) -> None:
        value = _require_finite_number(value, "target temperature")
        response = self._supervisor.request(
            "set_target_temperature", {"value": value}
        )
        self._supervisor.validate(
            lambda: _require_result_object(response, set())
        )

    @property
    def cooler_on(self) -> bool:
        response = self._supervisor.request("get_cooler_on", {})
        result = self._supervisor.validate(
            lambda: _require_result_object(response, {"value"})
        )
        if type(result["value"]) is not bool:
            self._supervisor._protocol_failure(
                "owner cooler state is not a boolean"
            )
        return result["value"]

    @cooler_on.setter
    def cooler_on(self, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise CameraInvalidValueError("cooler state must be a boolean")
        response = self._supervisor.request(
            "set_cooler_on", {"value": enabled}
        )
        self._supervisor.validate(
            lambda: _require_result_object(response, set())
        )

    def tec_voltage(self) -> tuple[float, float]:
        response = self._supervisor.request("get_tec_voltage", {})
        result = self._supervisor.validate(
            lambda: _require_result_object(response, {"current", "maximum"})
        )
        try:
            return (
                _require_finite_response_number(result["current"]),
                _require_finite_response_number(result["maximum"]),
            )
        except ValueError as error:
            self._supervisor._protocol_failure(
                f"owner TEC voltage response is invalid: {error}"
            )

    @property
    def cooler_power_percent(self) -> float:
        response = self._supervisor.request("get_cooler_power", {})
        return self._supervisor.validate(
            lambda: _single_float_result(response)
        )

    @property
    def heater_supported(self) -> bool:
        supported, _maximum = self._heater_capabilities()
        return supported

    @property
    def heater_max_level(self) -> int:
        _supported, maximum = self._heater_capabilities()
        return maximum

    @property
    def heater_level(self) -> int:
        return self._supervisor.get_heater_level()

    @heater_level.setter
    def heater_level(self, value: int) -> None:
        self._supervisor.set_heater_level(value)

    def _heater_capabilities(self) -> tuple[bool, int]:
        response = self._supervisor.request(
            "get_heater_capabilities", {}, require_connected=False
        )
        result = self._supervisor.validate(
            lambda: _require_result_object(
                response, {"supported", "max_level"}
            )
        )
        if (
            type(result["supported"]) is not bool
            or type(result["max_level"]) is not int
            or result["max_level"] < 0
        ):
            self._supervisor._protocol_failure(
                "owner heater capabilities response is invalid"
            )
        return result["supported"], result["max_level"]


def _validated_exposure_duration(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not MIN_EXPOSURE_DURATION_S
        <= float(value)
        <= MAX_EXPOSURE_DURATION_S
    ):
        raise CameraInvalidValueError(
            "exposure duration must be finite and between 0.000012 and 3600 seconds"
        )
    return float(value)


def _require_result_object(
    response: OwnerResponse, fields: set[str]
) -> Mapping[str, object]:
    result = response.result
    if type(result) is not dict or set(result) != fields:
        raise CameraOwnerProtocolError(
            f"owner {response.operation} result has an invalid schema"
        )
    return result


def _require_exact_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CameraInvalidValueError(f"{field} must be an integer")
    return value


def _require_finite_number(value: object, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise CameraInvalidValueError(f"{field} must be a finite number")
    return float(value)


def _require_finite_response_number(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError("value must be a finite number")
    return float(value)


def _single_int_result(
    response: OwnerResponse, *, minimum: int | None = None
) -> int:
    result = _require_result_object(response, {"value"})
    value = result["value"]
    if (
        type(value) is not int
        or (minimum is not None and value < minimum)
    ):
        raise CameraOwnerProtocolError(
            f"owner {response.operation} returned an invalid integer"
        )
    return value


def _single_float_result(response: OwnerResponse) -> float:
    result = _require_result_object(response, {"value"})
    try:
        return _require_finite_response_number(result["value"])
    except ValueError as error:
        raise CameraOwnerProtocolError(
            f"owner {response.operation} returned a non-finite number"
        ) from error


def _descriptor_from_payload(payload: object) -> FrameDescriptor:
    if type(payload) is not dict or set(payload) != {
        "protocol_version",
        "owner_generation",
        "request_id",
        "name",
        "shape",
        "dtype",
        "nbytes",
        "max_adu",
        "metadata",
    }:
        raise OwnerProtocolError("frame descriptor has an invalid schema")
    shape = payload["shape"]
    if type(shape) is not list or len(shape) != 2:
        raise OwnerProtocolError("frame descriptor shape is invalid")
    return FrameDescriptor(
        payload["protocol_version"],
        payload["owner_generation"],
        payload["request_id"],
        payload["name"],
        (shape[0], shape[1]),
        payload["dtype"],
        payload["nbytes"],
        payload["max_adu"],
        payload["metadata"],
    )


def _descriptor_payload(descriptor: FrameDescriptor) -> dict[str, object]:
    return {
        "protocol_version": descriptor.protocol_version,
        "owner_generation": descriptor.owner_generation,
        "request_id": descriptor.request_id,
        "name": descriptor.name,
        "shape": list(descriptor.shape),
        "dtype": descriptor.dtype,
        "nbytes": descriptor.nbytes,
        "max_adu": descriptor.max_adu,
        "metadata": dict(descriptor.metadata),
    }


def _frame_values_from_payload(payload: object) -> dict[str, object]:
    if type(payload) is not dict or set(payload) != {
        "started_utc",
        "duration_s",
        "binning",
        "roi",
        "readout_mode",
        "gain",
    }:
        raise OwnerProtocolError("frame metadata has an invalid schema")
    started = payload["started_utc"]
    if not isinstance(started, str):
        raise OwnerProtocolError("frame timestamp must be a string")
    started_utc = datetime.fromisoformat(started)
    if started_utc.tzinfo is None or started_utc.utcoffset() is None:
        raise OwnerProtocolError("frame timestamp must include a UTC offset")
    duration_s = _require_finite_response_number(payload["duration_s"])
    if not MIN_EXPOSURE_DURATION_S <= duration_s <= MAX_EXPOSURE_DURATION_S:
        raise OwnerProtocolError("frame duration is outside the supported range")
    binning = payload["binning"]
    gain = payload["gain"]
    if type(binning) is not int or type(gain) is not int:
        raise OwnerProtocolError("frame binning and gain must be integers")
    roi_payload = payload["roi"]
    if type(roi_payload) is not dict or set(roi_payload) != {
        "x",
        "y",
        "width",
        "height",
    }:
        raise OwnerProtocolError("frame ROI has an invalid schema")
    if any(type(roi_payload[name]) is not int for name in roi_payload):
        raise OwnerProtocolError("frame ROI values must be integers")
    try:
        readout_mode = ReadoutMode(payload["readout_mode"])
    except (TypeError, ValueError) as error:
        raise OwnerProtocolError("frame readout mode is invalid") from error
    return {
        "started_utc": started_utc,
        "duration_s": duration_s,
        "binning": binning,
        "roi": Roi(
            roi_payload["x"],
            roi_payload["y"],
            roi_payload["width"],
            roi_payload["height"],
        ),
        "readout_mode": readout_mode,
        "gain": gain,
    }
