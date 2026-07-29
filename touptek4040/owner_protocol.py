"""Strict, versioned messages exchanged with the camera owner process."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import math
from typing import TypeAlias


PROTOCOL_VERSION: int = 1
"""The only IPC schema version this process accepts."""

MAX_UINT64 = (1 << 64) - 1
PRODUCT_SHARED_MEMORY_PREFIX = "touptek4040-"
MIN_EXPOSURE_DURATION_S = 0.000012
MAX_EXPOSURE_DURATION_S = 3600.0

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


class OwnerProtocolError(ValueError):
    """Raised when an IPC message is not an exact, safe protocol value."""


def _validate_uint64(value: object, field: str, *, allow_zero: bool) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OwnerProtocolError(f"{field} must be an integer")
    if value < (0 if allow_zero else 1) or value > MAX_UINT64:
        raise OwnerProtocolError(f"{field} must be an unsigned 64-bit integer")
    return value


def _validate_protocol_version(value: object) -> int:
    version = _validate_uint64(value, "protocol_version", allow_zero=False)
    if version != PROTOCOL_VERSION:
        raise OwnerProtocolError(f"unsupported protocol_version {version}")
    return version


def _validate_nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise OwnerProtocolError(f"{field} must be a non-empty string")
    return value


def _copy_json_value(value: object, field: str) -> JsonValue:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise OwnerProtocolError(f"{field} must not contain a non-finite float")
        return value
    if isinstance(value, list):
        return [_copy_json_value(item, f"{field}[]") for item in value]
    if isinstance(value, dict):
        copied: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise OwnerProtocolError(f"{field} keys must be strings")
            copied[key] = _copy_json_value(item, f"{field}.{key}")
        return copied
    raise OwnerProtocolError(f"{field} must contain only JSON values")


def _copy_json_object(value: object, field: str) -> dict[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise OwnerProtocolError(f"{field} must be an object")
    copied: dict[str, JsonValue] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise OwnerProtocolError(f"{field} keys must be strings")
        copied[key] = _copy_json_value(item, f"{field}.{key}")
    return copied


def _wire_json_value(value: JsonValue) -> JsonValue:
    """Return a fresh JSON value so callers cannot mutate a schema object."""
    return _copy_json_value(value, "wire value")


@dataclass(frozen=True, slots=True)
class OwnerRequest:
    protocol_version: int
    owner_generation: int
    request_id: int
    operation: str
    arguments: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        object.__setattr__(self, "protocol_version", _validate_protocol_version(self.protocol_version))
        object.__setattr__(
            self,
            "owner_generation",
            _validate_uint64(self.owner_generation, "owner_generation", allow_zero=False),
        )
        object.__setattr__(self, "request_id", _validate_uint64(self.request_id, "request_id", allow_zero=True))
        object.__setattr__(self, "operation", _validate_nonempty_string(self.operation, "operation"))
        object.__setattr__(self, "arguments", _copy_json_object(self.arguments, "arguments"))


@dataclass(frozen=True, slots=True)
class OwnerError:
    code: str
    message: str
    diagnostics: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _validate_nonempty_string(self.code, "error.code"))
        object.__setattr__(self, "message", _validate_nonempty_string(self.message, "error.message"))
        object.__setattr__(self, "diagnostics", _copy_json_object(self.diagnostics, "error.diagnostics"))


@dataclass(frozen=True, slots=True)
class OwnerResponse:
    protocol_version: int
    owner_generation: int
    request_id: int
    operation: str
    result: JsonValue | None = None
    error: OwnerError | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "protocol_version", _validate_protocol_version(self.protocol_version))
        object.__setattr__(
            self,
            "owner_generation",
            _validate_uint64(self.owner_generation, "owner_generation", allow_zero=False),
        )
        object.__setattr__(self, "request_id", _validate_uint64(self.request_id, "request_id", allow_zero=True))
        object.__setattr__(self, "operation", _validate_nonempty_string(self.operation, "operation"))
        object.__setattr__(self, "result", _copy_json_value(self.result, "result"))
        if self.error is not None and not isinstance(self.error, OwnerError):
            raise OwnerProtocolError("error must be an OwnerError")
        if self.error is not None and self.result is not None:
            raise OwnerProtocolError("response cannot contain both result and error")


@dataclass(frozen=True, slots=True)
class AcquireStartAck:
    """Distinct correlation message sent before blocking acquisition work."""

    protocol_version: int
    owner_generation: int
    request_id: int
    operation: str
    actual_duration_s: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "protocol_version",
            _validate_protocol_version(self.protocol_version),
        )
        object.__setattr__(
            self,
            "owner_generation",
            _validate_uint64(
                self.owner_generation,
                "owner_generation",
                allow_zero=False,
            ),
        )
        object.__setattr__(
            self,
            "request_id",
            _validate_uint64(
                self.request_id, "request_id", allow_zero=True
            ),
        )
        if self.operation != "acquire":
            raise OwnerProtocolError(
                "acquire start acknowledgement operation must be acquire"
            )
        duration = self.actual_duration_s
        if isinstance(duration, bool) or not isinstance(
            duration, (int, float)
        ):
            raise OwnerProtocolError(
                "actual_duration_s is outside the supported camera range"
            )
        try:
            finite_duration = float(duration)
        except (OverflowError, ValueError) as error:
            raise OwnerProtocolError(
                "actual_duration_s is outside the supported camera range"
            ) from error
        if (
            not math.isfinite(finite_duration)
            or not MIN_EXPOSURE_DURATION_S
            <= finite_duration
            <= MAX_EXPOSURE_DURATION_S
        ):
            raise OwnerProtocolError(
                "actual_duration_s is outside the supported camera range"
            )
        object.__setattr__(
            self, "actual_duration_s", finite_duration
        )


@dataclass(frozen=True, slots=True)
class FrameDescriptor:
    """The validated description of one raw frame owned by shared memory."""

    protocol_version: int
    owner_generation: int
    request_id: int
    name: str
    shape: tuple[int, int]
    dtype: str
    nbytes: int
    max_adu: int
    metadata: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        object.__setattr__(self, "protocol_version", _validate_protocol_version(self.protocol_version))
        object.__setattr__(
            self,
            "owner_generation",
            _validate_uint64(self.owner_generation, "owner_generation", allow_zero=False),
        )
        object.__setattr__(self, "request_id", _validate_uint64(self.request_id, "request_id", allow_zero=True))
        object.__setattr__(self, "name", _validate_nonempty_string(self.name, "frame name"))
        if not isinstance(self.shape, tuple) or len(self.shape) != 2:
            raise OwnerProtocolError("frame shape must be a two-dimensional tuple")
        validated_shape: list[int] = []
        for dimension in self.shape:
            if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension <= 0:
                raise OwnerProtocolError("frame dimensions must be positive integers")
            if dimension > MAX_UINT64:
                raise OwnerProtocolError("frame dimensions exceed the protocol limit")
            validated_shape.append(dimension)
        object.__setattr__(self, "shape", (validated_shape[0], validated_shape[1]))
        if self.dtype not in {"uint8", "uint16"}:
            raise OwnerProtocolError("frame dtype must be uint8 or uint16")
        itemsize = 1 if self.dtype == "uint8" else 2
        expected_nbytes = validated_shape[0] * validated_shape[1] * itemsize
        if expected_nbytes > MAX_UINT64:
            raise OwnerProtocolError("frame byte length exceeds the protocol limit")
        object.__setattr__(self, "nbytes", _validate_uint64(self.nbytes, "frame nbytes", allow_zero=False))
        if self.nbytes != expected_nbytes:
            raise OwnerProtocolError("frame nbytes does not match shape and dtype")
        object.__setattr__(self, "max_adu", _validate_uint64(self.max_adu, "max_adu", allow_zero=True))
        object.__setattr__(self, "metadata", _copy_json_object(self.metadata, "metadata"))


def _reject_duplicate_object_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise OwnerProtocolError(f"duplicate JSON key {key!r}")
        payload[key] = value
    return payload


def _as_object(payload: object, message_type: str) -> Mapping[str, object]:
    if isinstance(payload, (str, bytes, bytearray)):
        try:
            payload = json.loads(payload, object_pairs_hook=_reject_duplicate_object_keys)
        except OwnerProtocolError:
            raise
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise OwnerProtocolError(f"{message_type} is not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise OwnerProtocolError(f"{message_type} must be an object")
    if not all(isinstance(key, str) for key in payload):
        raise OwnerProtocolError(f"{message_type} keys must be strings")
    return payload


def _require_exact_keys(payload: Mapping[str, object], expected: set[str], message_type: str) -> None:
    actual = set(payload)
    if actual != expected:
        unknown = actual - expected
        missing = expected - actual
        raise OwnerProtocolError(f"{message_type} fields differ; unknown={unknown}, missing={missing}")


def encode_request(request: OwnerRequest) -> dict[str, object]:
    """Encode a request as a fresh, JSON-compatible, versioned object."""
    if not isinstance(request, OwnerRequest):
        raise OwnerProtocolError("request must be an OwnerRequest")
    return {
        "protocol_version": request.protocol_version,
        "owner_generation": request.owner_generation,
        "request_id": request.request_id,
        "operation": request.operation,
        "arguments": _wire_json_value(dict(request.arguments)),
    }


def encode_acquire_start_ack(
    acknowledgement: AcquireStartAck,
) -> dict[str, object]:
    """Encode one exact command-start acknowledgement."""
    if not isinstance(acknowledgement, AcquireStartAck):
        raise OwnerProtocolError(
            "acknowledgement must be an AcquireStartAck"
        )
    return {
        "protocol_version": acknowledgement.protocol_version,
        "owner_generation": acknowledgement.owner_generation,
        "request_id": acknowledgement.request_id,
        "operation": acknowledgement.operation,
        "actual_duration_s": acknowledgement.actual_duration_s,
    }


def decode_acquire_start_ack(
    payload: object,
    expected: OwnerRequest,
) -> AcquireStartAck:
    """Decode an acknowledgement correlated to one acquire request."""
    if not isinstance(expected, OwnerRequest):
        raise OwnerProtocolError("expected request must be an OwnerRequest")
    if expected.operation != "acquire":
        raise OwnerProtocolError(
            "acquire start acknowledgement requires an acquire request"
        )
    object_payload = _as_object(
        payload, "acquire start acknowledgement"
    )
    _require_exact_keys(
        object_payload,
        {
            "protocol_version",
            "owner_generation",
            "request_id",
            "operation",
            "actual_duration_s",
        },
        "acquire start acknowledgement",
    )
    acknowledgement = AcquireStartAck(
        object_payload["protocol_version"],
        object_payload["owner_generation"],
        object_payload["request_id"],
        object_payload["operation"],
        object_payload["actual_duration_s"],
    )
    if acknowledgement.owner_generation != expected.owner_generation:
        raise OwnerProtocolError(
            "acknowledgement owner_generation does not match request"
        )
    if acknowledgement.request_id != expected.request_id:
        raise OwnerProtocolError(
            "acknowledgement request_id does not match request"
        )
    return acknowledgement


def decode_request(payload: object) -> OwnerRequest:
    """Validate and decode exactly one owner request."""
    object_payload = _as_object(payload, "request")
    _require_exact_keys(
        object_payload,
        {"protocol_version", "owner_generation", "request_id", "operation", "arguments"},
        "request",
    )
    return OwnerRequest(
        object_payload["protocol_version"],
        object_payload["owner_generation"],
        object_payload["request_id"],
        object_payload["operation"],
        object_payload["arguments"],
    )


def _decode_error(payload: object) -> OwnerError:
    error_payload = _as_object(payload, "error")
    _require_exact_keys(error_payload, {"code", "message", "diagnostics"}, "error")
    return OwnerError(
        error_payload["code"],
        error_payload["message"],
        error_payload["diagnostics"],
    )


def decode_response(payload: object, expected: OwnerRequest) -> OwnerResponse:
    """Decode a response only when it precisely belongs to ``expected``."""
    if not isinstance(expected, OwnerRequest):
        raise OwnerProtocolError("expected request must be an OwnerRequest")
    object_payload = _as_object(payload, "response")
    base_fields = {"protocol_version", "owner_generation", "request_id", "operation"}
    has_result = "result" in object_payload
    has_error = "error" in object_payload
    if has_result == has_error:
        raise OwnerProtocolError("response must contain exactly one of result or error")
    _require_exact_keys(object_payload, base_fields | ({"result"} if has_result else {"error"}), "response")

    response = OwnerResponse(
        object_payload["protocol_version"],
        object_payload["owner_generation"],
        object_payload["request_id"],
        object_payload["operation"],
        _copy_json_value(object_payload["result"], "result") if has_result else None,
        None if has_result else _decode_error(object_payload["error"]),
    )
    if response.owner_generation != expected.owner_generation:
        raise OwnerProtocolError("response owner_generation does not match request")
    if response.request_id != expected.request_id:
        raise OwnerProtocolError("response request_id does not match request")
    if response.operation != expected.operation:
        raise OwnerProtocolError("response operation does not match request")
    return response


@dataclass(slots=True)
class OwnerResponseTracker:
    """Transport-owned replay protection for owner generation/request pairs."""

    _seen: set[tuple[int, int]] = field(default_factory=set)

    def decode(self, payload: object, expected: OwnerRequest) -> OwnerResponse:
        """Decode one response and reject a repeated owner response ID."""
        response = decode_response(payload, expected)
        response_key = (response.owner_generation, response.request_id)
        if response_key in self._seen:
            raise OwnerProtocolError("duplicate response owner_generation/request_id")
        self._seen.add(response_key)
        return response
