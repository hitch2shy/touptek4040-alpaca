"""Contract tests for the camera-owner IPC schema.

Each test names a wire-level break it must catch.  The expected values are
literal JSON-compatible payloads so the tests do not mirror schema code.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import json

import pytest

from touptek4040.owner_protocol import (
    MAX_EXPOSURE_DURATION_S,
    MIN_EXPOSURE_DURATION_S,
    PROTOCOL_VERSION,
    AcquireStartAck,
    OwnerError,
    OwnerProtocolError,
    OwnerRequest,
    OwnerResponseTracker,
    decode_acquire_start_ack,
    decode_request,
    decode_response,
    encode_request,
)


def test_request_is_frozen_and_encodes_the_versioned_wire_schema():
    """Removing frozen schema fields or changing their wire names is a bug."""
    request = OwnerRequest(
        PROTOCOL_VERSION,
        7,
        42,
        "connect",
        {"trace": ["usb", 1], "retry": False},
    )

    with pytest.raises(FrozenInstanceError):
        request.operation = "disconnect"  # type: ignore[misc]

    payload = encode_request(request)

    assert payload == {
        "protocol_version": 1,
        "owner_generation": 7,
        "request_id": 42,
        "operation": "connect",
        "arguments": {"trace": ["usb", 1], "retry": False},
    }
    assert json.loads(json.dumps(payload)) == payload


def test_decode_request_rejects_unknown_wire_fields():
    """Silently accepting a future or misspelled request field is a bug."""
    with pytest.raises(OwnerProtocolError):
        decode_request(
            {
                "protocol_version": 1,
                "owner_generation": 7,
                "request_id": 42,
                "operation": "connect",
                "arguments": {},
                "unexpected": True,
            }
        )


@pytest.mark.parametrize("request_id", [True, -1, 2**64])
def test_decode_request_rejects_non_uint64_request_ids(request_id: object):
    """Treating bool, negative, or overflowing request IDs as valid is a bug."""
    with pytest.raises(OwnerProtocolError):
        decode_request(
            {
                "protocol_version": 1,
                "owner_generation": 7,
                "request_id": request_id,
                "operation": "connect",
                "arguments": {},
            }
        )


def test_decode_request_rejects_a_wrong_protocol_version():
    """Accepting a message from an incompatible IPC schema is a bug."""
    with pytest.raises(OwnerProtocolError):
        decode_request(
            {
                "protocol_version": 2,
                "owner_generation": 7,
                "request_id": 42,
                "operation": "connect",
                "arguments": {},
            }
        )


@pytest.mark.parametrize("invalid_value", [float("nan"), object()])
def test_request_arguments_reject_non_json_values(invalid_value: object):
    """Allowing non-finite or arbitrary objects across the IPC boundary is a bug."""
    with pytest.raises(OwnerProtocolError):
        OwnerRequest(PROTOCOL_VERSION, 7, 42, "connect", {"value": invalid_value})


def test_response_generation_and_request_id_must_match():
    expected = OwnerRequest(1, 7, 42, "connect", {})
    with pytest.raises(OwnerProtocolError):
        decode_response(
            {
                "protocol_version": 1,
                "owner_generation": 6,
                "request_id": 42,
                "operation": "connect",
                "result": {},
            },
            expected,
        )

    with pytest.raises(OwnerProtocolError):
        decode_response(
            {
                "protocol_version": 1,
                "owner_generation": 7,
                "request_id": 43,
                "operation": "connect",
                "result": {},
            },
            expected,
        )


def test_response_operation_must_match_the_request():
    """Routing a valid response to a different operation is a bug."""
    expected = OwnerRequest(1, 7, 42, "connect", {})

    with pytest.raises(OwnerProtocolError):
        decode_response(
            {
                "protocol_version": 1,
                "owner_generation": 7,
                "request_id": 42,
                "operation": "disconnect",
                "result": {},
            },
            expected,
        )


def test_response_rejects_a_result_and_error_together():
    """A response with two mutually exclusive outcomes is ambiguous and invalid."""
    expected = OwnerRequest(1, 7, 42, "connect", {})

    with pytest.raises(OwnerProtocolError):
        decode_response(
            {
                "protocol_version": 1,
                "owner_generation": 7,
                "request_id": 42,
                "operation": "connect",
                "result": {},
                "error": {
                    "code": "camera_busy",
                    "message": "camera is busy",
                    "diagnostics": {},
                },
            },
            expected,
        )


@pytest.mark.parametrize(
    "error_payload",
    [
        {"message": "camera is busy", "diagnostics": {}},
        {"code": "camera_busy", "message": "camera is busy", "diagnostics": {"value": float("inf")}},
        {"code": "camera_busy", "message": "camera is busy", "diagnostics": {}, "extra": None},
    ],
)
def test_response_rejects_malformed_error_payloads(error_payload: object):
    """Missing, non-JSON, and unknown error fields must not cross the boundary."""
    expected = OwnerRequest(1, 7, 42, "connect", {})

    with pytest.raises(OwnerProtocolError):
        decode_response(
            {
                "protocol_version": 1,
                "owner_generation": 7,
                "request_id": 42,
                "operation": "connect",
                "error": error_payload,
            },
            expected,
        )


def test_decode_response_rejects_duplicate_request_id_keys_in_raw_json():
    """A JSON response with duplicate response IDs must not be interpreted last-wins."""
    expected = OwnerRequest(1, 7, 42, "connect", {})

    with pytest.raises(OwnerProtocolError):
        decode_response(
            """
            {
              "protocol_version": 1,
              "owner_generation": 7,
              "request_id": 42,
              "request_id": 42,
              "operation": "connect",
              "result": {}
            }
            """,
            expected,
        )


def test_decode_response_builds_a_typed_error_with_json_diagnostics():
    """Dropping a valid owner error's diagnostic data is a bug."""
    expected = OwnerRequest(1, 7, 42, "connect", {})

    response = decode_response(
        {
            "protocol_version": 1,
            "owner_generation": 7,
            "request_id": 42,
            "operation": "connect",
            "error": {
                "code": "camera_busy",
                "message": "camera is busy",
                "diagnostics": {"active_exposure": True, "seconds": 1.5},
            },
        },
        expected,
    )

    assert response.result is None
    assert response.error == OwnerError(
        "camera_busy",
        "camera is busy",
        {"active_exposure": True, "seconds": 1.5},
    )


def test_explicit_response_tracker_rejects_a_duplicate_generation_and_request_id():
    """Accepting the same owner generation/request response twice is a transport bug."""
    expected = OwnerRequest(1, 7, 42, "connect", {})
    payload = {
        "protocol_version": 1,
        "owner_generation": 7,
        "request_id": 42,
        "operation": "connect",
        "result": {"connected": True},
    }
    tracker = OwnerResponseTracker()

    first = tracker.decode(payload, expected)

    assert first.result == {"connected": True}
    with pytest.raises(OwnerProtocolError):
        tracker.decode(payload, expected)


def test_acquire_start_ack_is_a_distinct_exact_versioned_schema():
    request = OwnerRequest(
        PROTOCOL_VERSION,
        7,
        42,
        "acquire",
        {
            "duration_s": 0.0000124,
            "roi": {"x": 0, "y": 0, "width": 2, "height": 2},
            "binning": 1,
            "readout_mode": 0,
        },
    )

    ack = decode_acquire_start_ack(
        {
            "protocol_version": 1,
            "owner_generation": 7,
            "request_id": 42,
            "operation": "acquire",
            "actual_duration_s": 0.000012,
        },
        request,
    )

    assert ack == AcquireStartAck(1, 7, 42, "acquire", 0.000012)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.__setitem__("actual_duration_s", True),
        lambda payload: payload.__setitem__("actual_duration_s", float("nan")),
        lambda payload: payload.__setitem__("actual_duration_s", 10**1000),
        lambda payload: payload.__setitem__(
            "actual_duration_s", MIN_EXPOSURE_DURATION_S / 2
        ),
        lambda payload: payload.__setitem__(
            "actual_duration_s", MAX_EXPOSURE_DURATION_S + 0.000001
        ),
        lambda payload: payload.__setitem__("owner_generation", 8),
        lambda payload: payload.__setitem__("request_id", 43),
        lambda payload: payload.__setitem__("protocol_version", 2),
        lambda payload: payload.__setitem__("operation", "connect"),
        lambda payload: payload.__setitem__("extra", None),
        lambda payload: payload.pop("actual_duration_s"),
    ],
)
def test_acquire_start_ack_rejects_malformed_or_uncorrelated_payloads(mutation):
    request = OwnerRequest(1, 7, 42, "acquire", {})
    payload = {
        "protocol_version": 1,
        "owner_generation": 7,
        "request_id": 42,
        "operation": "acquire",
        "actual_duration_s": 0.001,
    }
    mutation(payload)

    with pytest.raises(OwnerProtocolError):
        decode_acquire_start_ack(payload, request)


def test_acquire_start_ack_decoder_rejects_a_non_acquire_request():
    request = OwnerRequest(1, 7, 42, "connect", {})

    with pytest.raises(OwnerProtocolError):
        decode_acquire_start_ack(
            {
                "protocol_version": 1,
                "owner_generation": 7,
                "request_id": 42,
                "operation": "acquire",
                "actual_duration_s": 0.001,
            },
            request,
        )
