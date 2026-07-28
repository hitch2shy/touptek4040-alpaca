from __future__ import annotations

import pytest

from touptek4040.alpaca.responses import (
    INVALID_OPERATION,
    INVALID_VALUE,
    METHOD_NOT_IMPLEMENTED,
    NOT_CONNECTED,
)
from touptek4040.errors import CameraBusyError, CameraError


def _get(client, command: str):
    return client.simulate_get(f"/api/v1/camera/0/{command}")


def _put(client, command: str, **values: object):
    return client.simulate_put(f"/api/v1/camera/0/{command}", json=values)


def _value(client, command: str):
    response = _get(client, command)
    assert response.json["ErrorNumber"] == 0
    return response.json["Value"]


def test_cooling_capabilities_and_round_trip(client, api_camera):
    assert _value(client, "cansetccdtemperature") is True
    assert _value(client, "cangetcoolerpower") is True
    assert _value(client, "ccdtemperature") == -4.5
    assert _value(client, "setccdtemperature") == -10.0
    assert _value(client, "cooleron") is False
    assert _get(client, "heatsinktemperature").json["ErrorNumber"] == (
        METHOD_NOT_IMPLEMENTED
    )

    assert (
        _put(
            client,
            "setccdtemperature",
            SetCCDTemperature="-20.5",
        ).json["ErrorNumber"]
        == 0
    )
    assert _put(client, "cooleron", CoolerOn="true").json["ErrorNumber"] == 0
    assert api_camera.target_temperature_c == -20.5
    assert api_camera.cooler_on is True


@pytest.mark.parametrize(
    ("current", "maximum", "expected"),
    [
        (-1.0, 6.0, 0.0),
        (3.0, 6.0, 50.0),
        (9.0, 6.0, 100.0),
    ],
)
def test_cooler_power_uses_current_over_maximum_and_clamps(
    current, maximum, expected, client, api_camera
):
    api_camera.tec_current = current
    api_camera.tec_maximum = maximum

    assert _value(client, "coolerpower") == expected


@pytest.mark.parametrize("maximum", [0.0, -1.0])
def test_cooler_power_rejects_an_unavailable_maximum(
    maximum, client, api_camera
):
    api_camera.tec_maximum = maximum

    response = _get(client, "coolerpower")

    assert response.status_code == 200
    assert response.json["ErrorNumber"] == INVALID_OPERATION


@pytest.mark.parametrize("target", ["NaN", "Infinity", "-Infinity", "1_0", "１２"])
def test_non_finite_or_non_alpaca_temperature_never_reaches_camera(
    target, client, api_camera
):
    before = api_camera.target_temperature_c

    response = _put(
        client,
        "setccdtemperature",
        SetCCDTemperature=target,
    )

    assert response.status_code == 400
    assert response.json["ErrorNumber"] == INVALID_VALUE
    assert api_camera.target_temperature_c == before


@pytest.mark.parametrize(
    ("command", "method", "values"),
    [
        ("ccdtemperature", "GET", {}),
        ("setccdtemperature", "GET", {}),
        ("cooleron", "GET", {}),
        ("coolerpower", "GET", {}),
        ("setccdtemperature", "PUT", {"SetCCDTemperature": "-10"}),
        ("cooleron", "PUT", {"CoolerOn": "true"}),
    ],
)
def test_cooling_hardware_members_require_connection(
    command, method, values, disconnected_client
):
    response = disconnected_client.simulate_request(
        method=method,
        path=f"/api/v1/camera/0/{command}",
        json=values or None,
    )
    assert response.json["ErrorNumber"] == NOT_CONNECTED


@pytest.mark.parametrize(
    ("error", "number"),
    [
        (CameraBusyError("native telemetry is busy"), INVALID_OPERATION),
        (CameraError("native TEC read failed"), 0x500),
    ],
)
def test_cooling_native_failures_have_stable_error_mapping(
    error, number, client, api_camera
):
    api_camera.tec_error = error

    response = _get(client, "coolerpower")

    assert response.status_code == 200
    assert response.json["ErrorNumber"] == number
    assert str(error) in response.json["ErrorMessage"]
