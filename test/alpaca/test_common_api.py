from __future__ import annotations

import pytest

from touptek4040.alpaca.manifest import CAMERA_V4_ENDPOINTS
from touptek4040.alpaca.responses import (
    ACTION_NOT_IMPLEMENTED,
    METHOD_NOT_IMPLEMENTED,
)


@pytest.mark.parametrize(
    "spec",
    [endpoint for endpoint in CAMERA_V4_ENDPOINTS if endpoint.category != "image"],
    ids=lambda endpoint: f"{endpoint.http_method}-{endpoint.command}",
)
def test_every_non_image_manifest_endpoint_is_registered(client, spec):
    response = client.simulate_request(
        method=spec.http_method,
        path=f"/api/v1/camera/0/{spec.command}",
    )
    assert response.status_code not in {404, 405}
    assert response.content_type == "application/json"
    assert "ErrorNumber" in response.json


@pytest.mark.parametrize(
    ("command", "value"),
    [
        ("connected", True),
        ("connecting", False),
        ("name", "ToupTek 4040 Alpaca Camera"),
        ("interfaceversion", 4),
        ("supportedactions", []),
    ],
)
def test_common_properties(command, value, client):
    response = client.simulate_get(f"/api/v1/camera/0/{command}")
    assert response.status_code == 200
    assert response.json["Value"] == value
    assert response.json["ErrorNumber"] == 0


def test_common_identification_strings_match_approved_device(client):
    description = client.simulate_get("/api/v1/camera/0/description").json["Value"]
    driver_info = client.simulate_get("/api/v1/camera/0/driverinfo").json["Value"]
    driver_version = client.simulate_get(
        "/api/v1/camera/0/driverversion"
    ).json["Value"]
    assert "sMAX16AM-U3-CL-GPS" in description
    assert len(description) <= 64
    assert description.isascii()
    assert all(part.isdecimal() for part in driver_version.split("."))
    for field in ("SDK", "firmware", "hardware", "FPGA"):
        assert field in driver_info


def test_error_and_success_envelopes_preserve_transactions(client):
    unsupported = client.simulate_put(
        "/api/v1/camera/0/action",
        params={"ClientTransactionID": "23"},
        json={"Action": "x", "Parameters": ""},
    )
    assert unsupported.status_code == 200
    assert unsupported.json["ClientTransactionID"] == 23
    assert unsupported.json["ServerTransactionID"] > 0
    assert unsupported.json["ErrorNumber"] == ACTION_NOT_IMPLEMENTED
    assert unsupported.json["ErrorMessage"]


@pytest.mark.parametrize("command", ["commandblind", "commandbool", "commandstring"])
def test_unsupported_commands_are_explicit(command, client):
    response = client.simulate_put(
        f"/api/v1/camera/0/{command}",
        json={"Command": "x", "Raw": False},
    )
    assert response.status_code == 200
    assert response.json["ErrorNumber"] == METHOD_NOT_IMPLEMENTED


@pytest.mark.parametrize(
    ("command", "fields"),
    [
        ("action", {"Action": 1, "Parameters": ""}),
        ("commandblind", {"Command": "x", "Raw": "yes"}),
        ("commandbool", {"Command": None, "Raw": "true"}),
        ("commandstring", {"Command": "x", "Raw": 1}),
    ],
)
def test_unsupported_common_puts_parse_parameters_before_business_error(
    command, fields, disconnected_client
):
    response = disconnected_client.simulate_put(
        f"/api/v1/camera/0/{command}",
        json=fields,
    )
    assert response.status_code == 400
    assert response.json["ErrorNumber"] == 0x401


def test_connected_put_is_synchronous_and_case_insensitive(disconnected_client):
    response = disconnected_client.simulate_put(
        "/api/v1/camera/0/connected",
        json={"cOnNeCtEd": "true"},
    )
    assert response.json["ErrorNumber"] == 0
    assert (
        disconnected_client.simulate_get("/api/v1/camera/0/connected").json["Value"]
        is True
    )


def test_connect_and_disconnect_methods_change_connection_asynchronously(
    disconnected_client,
):
    connected = disconnected_client.simulate_put("/api/v1/camera/0/connect")
    assert connected.json["ErrorNumber"] == 0
    disconnected = disconnected_client.simulate_put("/api/v1/camera/0/disconnect")
    assert disconnected.json["ErrorNumber"] == 0


@pytest.mark.parametrize("bad_value", ["yes", "1", 1, None, [], {}])
def test_connected_rejects_non_alpaca_boolean(bad_value, disconnected_client):
    response = disconnected_client.simulate_put(
        "/api/v1/camera/0/connected",
        json={"Connected": bad_value},
    )
    assert response.status_code == 400
    assert response.json["ErrorNumber"] == 0x401


def test_device_state_uses_coordinator_snapshot_and_connected_telemetry(client):
    response = client.simulate_get("/api/v1/camera/0/devicestate")
    values = {entry["Name"]: entry["Value"] for entry in response.json["Value"]}
    timestamp = values.pop("TimeStamp")
    assert timestamp.endswith("Z")
    assert values == {
        "CameraState": 0,
        "ImageReady": False,
        "IsPulseGuiding": False,
        "PercentCompleted": 0,
        "CCDTemperature": -4.5,
        "CoolerPower": 50.0,
    }


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/telescope/0/connected",
        "/api/v1/camera/1/connected",
        "/api/v1/camera/not-a-number/connected",
        "/api/v1/camera/-1/connected",
        "/api/v1/camera/0/not-a-camera-command",
        "/api/v2/camera/0/connected",
        "/API/v1/camera/0/connected",
        "/not-an-alpaca-path",
    ],
)
def test_invalid_api_paths_return_stable_http_400(path, client):
    response = client.simulate_get(path)
    assert response.status_code == 400
    assert response.content_type == "application/json"
    assert response.json == {
        "ErrorNumber": 0x401,
        "ErrorMessage": "Invalid Alpaca API path",
    }


def test_malformed_json_is_http_400_without_parser_details(client):
    response = client.simulate_put(
        "/api/v1/camera/0/gain",
        body='{"Gain": !!! native-parser-detail',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    assert response.content_type == "application/json"
    assert response.json["ErrorNumber"] == 0x401
    assert response.json["ErrorMessage"] == "Invalid request parameters"
    assert "native-parser-detail" not in response.text


def test_lexically_invalid_value_is_http_400_but_domain_range_is_http_200(client):
    lexical = client.simulate_put(
        "/api/v1/camera/0/gain",
        json={"Gain": "bad"},
    )
    assert lexical.status_code == 400
    assert lexical.json["ErrorNumber"] == 0x401

    domain = client.simulate_put(
        "/api/v1/camera/0/gain",
        json={"Gain": "99"},
    )
    assert domain.status_code == 200
    assert domain.json["ErrorNumber"] == 0x401


@pytest.mark.parametrize(
    ("command", "method", "fields", "expected_error"),
    [
        ("ccdtemperature", "GET", None, 0x407),
        ("pulseguide", "PUT", {"Direction": "0", "Duration": "100"}, 0x400),
        ("percentcompleted", "GET", None, 0x40B),
    ],
)
def test_device_errors_remain_http_200(
    command, method, fields, expected_error, disconnected_client
):
    response = disconnected_client.simulate_request(
        method=method,
        path=f"/api/v1/camera/0/{command}",
        json=fields,
    )
    assert response.status_code == 200
    assert response.json["ErrorNumber"] == expected_error
