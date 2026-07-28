from __future__ import annotations

import pytest


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
        "/api/v1/Camera/0/connected",
        "/api/v1/camera/0/Connected",
        "/api/v1/camera/0/connected/",
        "/management/apiversions/",
        "/management/v1/Description",
        "/not-an-alpaca-path",
    ],
)
def test_invalid_routes_and_casing_have_stable_safe_http_400_json(path, client):
    response = client.simulate_get(path)

    assert response.status_code == 400
    assert response.content_type == "application/json"
    assert response.json == {
        "ErrorNumber": 0x401,
        "ErrorMessage": "Invalid Alpaca API path",
    }
    assert path not in response.text


def test_missing_required_parameter_is_http_400_with_transaction_envelope(client):
    response = client.simulate_put(
        "/api/v1/camera/0/startexposure",
        json={"Light": True, "ClientTransactionID": 31},
    )

    assert response.status_code == 400
    assert response.content_type == "application/json"
    assert response.json["ClientTransactionID"] == 31
    assert response.json["ServerTransactionID"] > 0
    assert response.json["ErrorNumber"] == 0x401
    assert response.json["ErrorMessage"] == "Missing required parameter: Duration"


def test_malformed_json_is_stable_http_400_without_parser_or_input_leak(client):
    response = client.simulate_put(
        "/api/v1/camera/0/gain",
        body='{"Gain": !!! SECRET-native-parser-detail',
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.content_type == "application/json"
    assert response.json["ErrorNumber"] == 0x401
    assert response.json["ErrorMessage"] == "Invalid request parameters"
    assert "SECRET" not in response.text


def test_valid_device_level_ascom_error_remains_http_200(disconnected_client):
    response = disconnected_client.simulate_get(
        "/api/v1/camera/0/ccdtemperature",
        params={"ClientTransactionID": 32},
    )

    assert response.status_code == 200
    assert response.json["ClientTransactionID"] == 32
    assert response.json["ServerTransactionID"] > 0
    assert response.json["ErrorNumber"] == 0x407
    assert response.json["ErrorMessage"]


def test_unknown_method_on_known_route_is_http_405_with_allow(client):
    response = client.simulate_post("/api/v1/camera/0/connected")

    assert response.status_code == 405
    assert {
        method.strip()
        for method in response.headers["allow"].split(",")
    } == {"GET", "PUT", "OPTIONS"}
    assert response.content_type == "application/json"


def test_official_form_encoded_put_still_uses_task7_parsing(client):
    response = client.simulate_put(
        "/api/v1/camera/0/gain",
        body="Gain=250&ClientTransactionID=33",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 200
    assert response.json["ClientTransactionID"] == 33
    assert response.json["ErrorNumber"] == 0
    assert client.simulate_get("/api/v1/camera/0/gain").json["Value"] == 250
