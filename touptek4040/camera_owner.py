"""Windows-spawn-safe process entry point for the sole SDK-owning camera."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import importlib
import math
import os
from pathlib import Path
from threading import Event, Thread
from typing import Any

from .camera_contract import CameraCapabilities, Frame, ReadoutMode, Roi
from .owner_backend import OwnerBackend
from .owner_protocol import (
    FrameDescriptor,
    OwnerError,
    OwnerProtocolError,
    OwnerRequest,
    OwnerResponse,
PROTOCOL_VERSION,
    decode_request,
)
from .shared_frames import SharedFrameRegistry


APPROVED_CAMERA_SERIAL = "TP241025143218C92B01151AEC40CB7"
APPROVED_CAMERA_MODEL = "sMAX16AM-U3-CL-GPS"


@dataclass(frozen=True, slots=True)
class OwnerStartupSpec:
    """Pickle-safe description of the backend created by a spawned owner."""

    serial: str | None = None
    backend_module: str = "touptek4040.camera_owner"
    backend_factory: str = "_create_toupcam_backend"
    backend_arguments: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.serial is not None and (not isinstance(self.serial, str) or not self.serial):
            raise ValueError("serial must be a non-empty string or None")
        if not isinstance(self.backend_module, str) or not self.backend_module:
            raise ValueError("backend_module must be a non-empty string")
        if not isinstance(self.backend_factory, str) or not self.backend_factory:
            raise ValueError("backend_factory must be a non-empty string")
        if not isinstance(self.backend_arguments, Mapping):
            raise ValueError("backend_arguments must be a mapping")
        if not all(isinstance(key, str) for key in self.backend_arguments):
            raise ValueError("backend_arguments keys must be strings")
        if not _is_safe_startup_value(dict(self.backend_arguments)):
            raise ValueError("backend_arguments must contain only finite JSON values")
        object.__setattr__(self, "backend_arguments", dict(self.backend_arguments))


def _create_toupcam_backend(serial: str | None = None, **_kwargs: object) -> OwnerBackend:
    """Import the SDK adapter only after this spawned process has started."""
    if serial != APPROVED_CAMERA_SERIAL:
        raise ValueError("real owner requires the approved camera serial")
    from .toupcam_camera import ToupTekCamera

    camera = ToupTekCamera(serial)
    if camera.identity.model != APPROVED_CAMERA_MODEL or camera.serial != APPROVED_CAMERA_SERIAL:
        camera.disconnect()
        raise ValueError("real owner requires the approved camera model and serial")
    return camera


def camera_owner_main(
    command_connection: Any,
    control_connection: Any,
    startup_spec: OwnerStartupSpec,
    generation: int,
    registry_path: str | os.PathLike[str],
) -> None:
    """Serve one sequential command pipe and an independent Abort control pipe."""
    backend: OwnerBackend | None = None
    registry: SharedFrameRegistry | None = None
    connected = False
    saved_heater_level: int | None = None
    owned_descriptors: dict[str, FrameDescriptor] = {}
    stop_requested = Event()
    cleanup_errors: list[str] = []
    last_request_id: int | None = None
    backend_may_own_handle = False

    def report_cleanup_error(error: BaseException) -> None:
        if len(cleanup_errors) < 4:
            try:
                message = str(error)
            except BaseException:
                message = type(error).__name__
            cleanup_errors.append(message[:256] or type(error).__name__)

    def restore_heater() -> None:
        nonlocal saved_heater_level
        if backend is None or saved_heater_level is None:
            return
        backend.heater_level = saved_heater_level
        saved_heater_level = None

    def disconnect_backend() -> None:
        nonlocal connected, backend_may_own_handle
        if backend is None or not backend_may_own_handle:
            return
        restoration_error: BaseException | None = None
        try:
            restore_heater()
        except BaseException as error:
            restoration_error = error
        try:
            backend.disconnect()
        finally:
            connected = False
        backend_may_own_handle = False
        if restoration_error is not None:
            raise restoration_error

    def control_loop() -> None:
        while not stop_requested.is_set():
            try:
                if not control_connection.poll(0.05):
                    continue
                message = control_connection.recv()
            except (EOFError, OSError):
                return
            if not isinstance(message, Mapping):
                control_connection.send({"operation": "invalid_control", "ok": False, "error": "control message must be an object"})
                continue
            operation = message.get("operation")
            if set(message) != {"operation"}:
                response_operation = _control_response_operation(operation)
                control_connection.send({"operation": response_operation, "ok": False, "error": "control message has an invalid schema"})
                continue
            if not isinstance(operation, str) or not operation:
                control_connection.send({"operation": "invalid_control", "ok": False, "error": "control operation is required"})
                continue
            if operation == "shutdown":
                if backend is not None:
                    try:
                        backend.abort()
                        control_connection.send({"operation": "shutdown", "ok": True})
                    except BaseException as error:
                        control_connection.send({"operation": "shutdown", "ok": False, "error": str(error)[:256]})
                stop_requested.set()
                continue
            if operation == "abort" and backend is not None and connected:
                try:
                    backend.abort()
                    control_connection.send({"operation": "abort", "ok": True})
                except BaseException as error:
                    control_connection.send({"operation": "abort", "ok": False, "error": str(error)[:256]})
            elif operation == "abort":
                control_connection.send({"operation": "abort", "ok": False, "error": "camera is not connected"})
            else:
                control_connection.send({"operation": operation, "ok": False, "error": "unsupported control operation"})

    control_thread: Thread | None = None
    try:
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
            raise OwnerProtocolError("owner generation must be a positive integer")
        if not isinstance(startup_spec, OwnerStartupSpec):
            raise OwnerProtocolError("startup_spec must be an OwnerStartupSpec")
        registry = SharedFrameRegistry(generation, Path(registry_path))
        backend = _instantiate_backend(startup_spec)
        backend_may_own_handle = True
        control_thread = Thread(target=control_loop, name="camera-owner-control", daemon=True)
        control_thread.start()
        while not stop_requested.is_set():
            if not command_connection.poll(0.05):
                continue
            try:
                payload = command_connection.recv()
            except (EOFError, OSError):
                break
            try:
                request: OwnerRequest | None = None
                request = decode_request(payload)
                if request.owner_generation != generation:
                    raise OwnerProtocolError("request belongs to a different owner generation")
                if last_request_id is not None and request.request_id <= last_request_id:
                    raise OwnerProtocolError("request_id must increase monotonically")
                last_request_id = request.request_id
                response = _handle_command(
                    payload, generation, backend, registry, owned_descriptors,
                    connected, saved_heater_level, disconnect_backend,
                    report_cleanup_error,
                )
            except BaseException as error:
                if isinstance(request, OwnerRequest):
                    response = (_response_payload(request, error=_owner_error(error)), connected, saved_heater_level, False)
                else:
                    response = (
                        _invalid_payload_response(payload, generation, error),
                        connected,
                        saved_heater_level,
                        False,
                    )
            connected = bool(response[1])
            saved_heater_level = response[2]
            should_stop = bool(response[3])
            try:
                command_connection.send(response[0])
            except (EOFError, OSError):
                break
            if should_stop:
                stop_requested.set()
    except BaseException as error:
        report_cleanup_error(error)
    finally:
        stop_requested.set()
        if backend is not None and backend_may_own_handle:
            try:
                backend.abort()
            except BaseException as error:
                report_cleanup_error(error)
            try:
                disconnect_backend()
            except BaseException as error:
                report_cleanup_error(error)
        if registry is not None:
            for descriptor in tuple(owned_descriptors.values()):
                try:
                    registry.release(descriptor)
                except BaseException as error:
                    report_cleanup_error(error)
        if control_thread is not None:
            control_thread.join(0.2)
        if cleanup_errors:
            try:
                command_connection.send(
                    _cleanup_diagnostic_payload(generation, cleanup_errors)
                )
            except BaseException:
                pass
        try:
            command_connection.close()
        finally:
            control_connection.close()


def _instantiate_backend(spec: OwnerStartupSpec) -> OwnerBackend:
    module = importlib.import_module(spec.backend_module)
    factory = getattr(module, spec.backend_factory)
    backend = factory(spec.serial, **dict(spec.backend_arguments))
    if not isinstance(backend, OwnerBackend):
        raise TypeError("owner backend does not implement CameraContract")
    return backend


def _handle_command(
    payload: object,
    generation: int,
    backend: OwnerBackend,
    registry: SharedFrameRegistry,
    owned_descriptors: dict[str, FrameDescriptor],
    connected: bool,
    saved_heater_level: int | None,
    disconnect_backend: Any,
    report_cleanup_error: Any,
) -> tuple[dict[str, object], bool, int | None, bool]:
    request: OwnerRequest | None = None
    try:
        request = decode_request(payload)
        if request.owner_generation != generation:
            raise OwnerProtocolError("request belongs to a different owner generation")
        result, connected, saved_heater_level, shutdown = _dispatch(
            request,
            backend,
            registry,
            owned_descriptors,
            connected,
            saved_heater_level,
            disconnect_backend,
            report_cleanup_error,
        )
        return _response_payload(request, result=result), connected, saved_heater_level, shutdown
    except BaseException as error:
        if request is None:
            return _invalid_payload_response(payload, generation, error), connected, saved_heater_level, False
        return _response_payload(request, error=_owner_error(error)), connected, saved_heater_level, False


def _dispatch(
    request: OwnerRequest,
    backend: OwnerBackend,
    registry: SharedFrameRegistry,
    owned_descriptors: dict[str, FrameDescriptor],
    connected: bool,
    saved_heater_level: int | None,
    disconnect_backend: Any,
    report_cleanup_error: Any,
) -> tuple[dict[str, object], bool, int | None, bool]:
    operation = request.operation
    arguments = request.arguments
    if operation == "handshake":
        _require_keys(arguments, set())
        return {"pid": os.getpid(), "generation": request.owner_generation}, connected, saved_heater_level, False
    if operation == "connect":
        _require_keys(arguments, set())
        if not connected:
            backend.connect()
            try:
                saved_heater_level = backend.heater_level if backend.heater_supported else None
            except BaseException:
                try:
                    disconnect_backend()
                except BaseException as rollback_error:
                    report_cleanup_error(rollback_error)
                raise
            connected = True
        return {"connected": True}, connected, saved_heater_level, False
    if operation == "disconnect":
        _require_keys(arguments, set())
        disconnect_backend()
        return {"connected": False}, False, None, False
    if operation == "capabilities":
        _require_keys(arguments, set())
        return _capabilities_payload(backend.capabilities), connected, saved_heater_level, False
    if operation == "acquire":
        duration_s, roi, binning, readout_mode = _acquire_arguments(arguments)
        frame = backend.acquire(duration_s, roi, binning, readout_mode)
        descriptor = registry.publish(frame.pixels, _frame_pixel_metadata(frame))
        try:
            result = {"descriptor": _descriptor_payload(descriptor), "frame": _frame_payload(frame)}
            _response_payload(request, result=result)
        except BaseException:
            registry.release(descriptor)
            raise
        owned_descriptors[descriptor.name] = descriptor
        return result, connected, saved_heater_level, False
    if operation == "abort":
        _require_keys(arguments, set())
        backend.abort()
        return {}, connected, saved_heater_level, False
    if operation == "get_gain":
        _require_keys(arguments, set())
        return {"value": backend.gain}, connected, saved_heater_level, False
    if operation == "set_gain":
        backend.gain = _int_argument(arguments, "value")
        return {}, connected, saved_heater_level, False
    if operation == "get_temperature":
        _require_keys(arguments, set())
        return {"value": backend.temperature_c}, connected, saved_heater_level, False
    if operation == "get_target_temperature":
        _require_keys(arguments, set())
        return {"value": backend.target_temperature_c}, connected, saved_heater_level, False
    if operation == "set_target_temperature":
        backend.target_temperature_c = _number_argument(arguments, "value")
        return {}, connected, saved_heater_level, False
    if operation == "get_cooler_on":
        _require_keys(arguments, set())
        return {"value": backend.cooler_on}, connected, saved_heater_level, False
    if operation == "set_cooler_on":
        _require_keys(arguments, {"value"})
        value = arguments["value"]
        if not isinstance(value, bool):
            raise OwnerProtocolError("value must be boolean")
        backend.cooler_on = value
        return {}, connected, saved_heater_level, False
    if operation == "get_tec_voltage":
        _require_keys(arguments, set())
        current, maximum = backend.tec_voltage()
        return {"current": current, "maximum": maximum}, connected, saved_heater_level, False
    if operation == "get_cooler_power":
        _require_keys(arguments, set())
        return {"value": backend.cooler_power_percent}, connected, saved_heater_level, False
    if operation == "get_heater_capabilities":
        _require_keys(arguments, set())
        return {"supported": backend.heater_supported, "max_level": backend.heater_max_level}, connected, saved_heater_level, False
    if operation == "get_heater_level":
        _require_keys(arguments, set())
        return {"value": backend.heater_level}, connected, saved_heater_level, False
    if operation == "set_heater_level":
        backend.heater_level = _int_argument(arguments, "value")
        return {}, connected, saved_heater_level, False
    if operation == "diagnostics":
        _require_keys(arguments, set())
        return {"backend_pid": os.getpid(), "connected": connected, "owned_frame_count": len(owned_descriptors)}, connected, saved_heater_level, False
    if operation == "release_frame":
        _require_keys(arguments, {"descriptor"})
        descriptor = _descriptor_from_payload(arguments["descriptor"])
        registry.release(descriptor)
        owned_descriptors.pop(descriptor.name, None)
        return {}, connected, saved_heater_level, False
    if operation == "shutdown":
        _require_keys(arguments, set())
        disconnect_backend()
        return {"shutdown": True}, False, None, True
    raise OwnerProtocolError("unsupported owner operation")


def _response_payload(request: OwnerRequest, *, result: object | None = None, error: OwnerError | None = None) -> dict[str, object]:
    response = OwnerResponse(PROTOCOL_VERSION, request.owner_generation, request.request_id, request.operation, result, error)
    payload: dict[str, object] = {"protocol_version": response.protocol_version, "owner_generation": response.owner_generation, "request_id": response.request_id, "operation": response.operation}
    if response.error is None:
        payload["result"] = response.result
    else:
        payload["error"] = {"code": response.error.code, "message": response.error.message, "diagnostics": dict(response.error.diagnostics)}
    return payload


def _invalid_payload_response(
    payload: object,
    generation: int,
    error: BaseException,
) -> dict[str, object]:
    del payload
    request = OwnerRequest(PROTOCOL_VERSION, generation, 0, "invalid", {})
    return _response_payload(request, error=_owner_error(error))


def _control_response_operation(operation: object) -> str:
    if isinstance(operation, str) and operation:
        return operation
    return "invalid_control"


def _cleanup_diagnostic_payload(
    generation: int,
    cleanup_errors: list[str],
) -> dict[str, object]:
    request = OwnerRequest(PROTOCOL_VERSION, generation, 0, "cleanup", {})
    return _response_payload(
        request,
        error=OwnerError(
            "cleanup_failed",
            "camera owner final cleanup failed",
            {"errors": list(cleanup_errors)},
        ),
    )


def _owner_error(error: BaseException) -> OwnerError:
    code = "invalid_request" if isinstance(error, OwnerProtocolError) else "camera_error"
    message = str(error)[:512] or type(error).__name__
    return OwnerError(code, message, {})


def _require_keys(arguments: Mapping[str, object], expected: set[str]) -> None:
    if set(arguments) != expected:
        raise OwnerProtocolError("operation arguments have an invalid schema")


def _int_argument(arguments: Mapping[str, object], name: str) -> int:
    _require_keys(arguments, {name})
    value = arguments[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise OwnerProtocolError(f"{name} must be an integer")
    return value


def _number_argument(arguments: Mapping[str, object], name: str) -> float:
    _require_keys(arguments, {name})
    value = arguments[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise OwnerProtocolError(f"{name} must be a finite number")
    return float(value)


def _acquire_arguments(arguments: Mapping[str, object]) -> tuple[float, Roi, int, ReadoutMode]:
    _require_keys(arguments, {"duration_s", "roi", "binning", "readout_mode"})
    duration_s = _number_argument({"value": arguments["duration_s"]}, "value")
    if duration_s < 0:
        raise OwnerProtocolError("duration_s must be non-negative")
    roi_payload = arguments["roi"]
    if not isinstance(roi_payload, Mapping):
        raise OwnerProtocolError("roi must be an object")
    _require_keys(roi_payload, {"x", "y", "width", "height"})
    roi = Roi(*(_int_argument({"value": roi_payload[name]}, "value") for name in ("x", "y", "width", "height")))
    binning = _int_argument({"value": arguments["binning"]}, "value")
    readout_value = _int_argument({"value": arguments["readout_mode"]}, "value")
    try:
        return duration_s, roi, binning, ReadoutMode(readout_value)
    except ValueError as error:
        raise OwnerProtocolError("unsupported readout_mode") from error


def _capabilities_payload(capabilities: CameraCapabilities) -> dict[str, object]:
    return {"width": capabilities.width, "height": capabilities.height, "pixel_size_x": capabilities.pixel_size_x, "pixel_size_y": capabilities.pixel_size_y, "readout_modes": list(capabilities.readout_modes), "max_bin": capabilities.max_bin, "can_abort": capabilities.can_abort, "can_stop": capabilities.can_stop}


def _frame_pixel_metadata(frame: Frame) -> dict[str, object]:
    return {"pixel_format": "RAW12" if frame.pixels.dtype.name == "uint16" else "RAW8", "max_adu": 4095 if frame.pixels.dtype.name == "uint16" else 255}


def _frame_payload(frame: Frame) -> dict[str, object]:
    return {"started_utc": frame.started_utc.isoformat(), "duration_s": frame.duration_s, "binning": frame.binning, "roi": {"x": frame.roi.x, "y": frame.roi.y, "width": frame.roi.width, "height": frame.roi.height}, "readout_mode": int(frame.readout_mode), "gain": frame.gain}


def _descriptor_payload(descriptor: FrameDescriptor) -> dict[str, object]:
    return {"protocol_version": descriptor.protocol_version, "owner_generation": descriptor.owner_generation, "request_id": descriptor.request_id, "name": descriptor.name, "shape": list(descriptor.shape), "dtype": descriptor.dtype, "nbytes": descriptor.nbytes, "max_adu": descriptor.max_adu, "metadata": dict(descriptor.metadata)}


def _descriptor_from_payload(payload: object) -> FrameDescriptor:
    if not isinstance(payload, Mapping):
        raise OwnerProtocolError("descriptor must be an object")
    _require_keys(payload, {"protocol_version", "owner_generation", "request_id", "name", "shape", "dtype", "nbytes", "max_adu", "metadata"})
    shape = payload["shape"]
    if not isinstance(shape, list) or len(shape) != 2:
        raise OwnerProtocolError("descriptor shape must be a two-item list")
    try:
        return FrameDescriptor(payload["protocol_version"], payload["owner_generation"], payload["request_id"], payload["name"], (shape[0], shape[1]), payload["dtype"], payload["nbytes"], payload["max_adu"], payload["metadata"])
    except OwnerProtocolError:
        raise
    except (TypeError, ValueError) as error:
        raise OwnerProtocolError("descriptor is invalid") from error


def _is_safe_startup_value(value: object) -> bool:
    if value is None or isinstance(value, (bool, str)):
        return True
    if isinstance(value, int) and not isinstance(value, bool):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_safe_startup_value(item) for item in value)
    if isinstance(value, Mapping):
        return all(isinstance(key, str) and _is_safe_startup_value(item) for key, item in value.items())
    return False
