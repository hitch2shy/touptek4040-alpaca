from __future__ import annotations

import pytest

from touptek4040.alpaca.responses import METHOD_NOT_IMPLEMENTED, NOT_CONNECTED


@pytest.mark.parametrize(
    ("command", "value"),
    [
        ("cameraxsize", 4096),
        ("cameraysize", 4096),
        ("pixelsizex", 9.0),
        ("pixelsizey", 9.0),
        ("canabortexposure", True),
        ("canstopexposure", False),
        ("canasymmetricbin", False),
        ("maxbinx", 8),
        ("maxbiny", 8),
        ("sensorname", "sMAX16AM-U3-CL-GPS"),
        ("sensortype", 0),
        ("readoutmodes", ["RAW 12-bit", "RAW 8-bit"]),
        ("canfastreadout", False),
        ("cangetcoolerpower", True),
        ("canpulseguide", False),
        ("cansetccdtemperature", True),
        ("hasshutter", False),
        ("ispulseguiding", False),
        ("exposuremin", 0.000012),
        ("exposuremax", 3600.0),
        ("exposureresolution", 0.000001),
    ],
)
def test_camera_capability(command, value, client):
    response = client.simulate_get(f"/api/v1/camera/0/{command}")
    assert response.status_code == 200
    assert response.json["Value"] == value
    assert response.json["ErrorNumber"] == 0


@pytest.mark.parametrize(
    ("method", "command", "fields"),
    [
        ("GET", "bayeroffsetx", {}),
        ("GET", "bayeroffsety", {}),
        ("GET", "fastreadout", {}),
        ("PUT", "fastreadout", {"FastReadout": "false"}),
        ("GET", "offset", {}),
        ("PUT", "offset", {"Offset": "0"}),
        ("GET", "offsetmin", {}),
        ("GET", "offsetmax", {}),
        ("GET", "offsets", {}),
        ("GET", "gains", {}),
        ("GET", "electronsperadu", {}),
        ("GET", "fullwellcapacity", {}),
        ("GET", "heatsinktemperature", {}),
        ("GET", "subexposureduration", {}),
        ("PUT", "subexposureduration", {"SubExposureDuration": "1.0"}),
        ("PUT", "pulseguide", {"Direction": "0", "Duration": "100"}),
    ],
)
def test_unsupported_camera_members_are_explicit(method, command, fields, client):
    response = client.simulate_request(
        method=method,
        path=f"/api/v1/camera/0/{command}",
        json=fields if method == "PUT" else None,
    )
    assert response.status_code == 200
    assert response.json["ErrorNumber"] == METHOD_NOT_IMPLEMENTED


@pytest.mark.parametrize(
    ("command", "fields"),
    [
        ("fastreadout", {"FastReadout": "yes"}),
        ("offset", {"Offset": "bad"}),
        ("subexposureduration", {"SubExposureDuration": "1_0"}),
        ("pulseguide", {"Direction": "bad", "Duration": "100"}),
        ("pulseguide", {"Direction": "0", "Duration": "bad"}),
    ],
)
def test_unsupported_camera_puts_parse_parameters_before_business_error(
    command, fields, disconnected_client
):
    response = disconnected_client.simulate_put(
        f"/api/v1/camera/0/{command}",
        json=fields,
    )
    assert response.status_code == 400
    assert response.json["ErrorNumber"] == 0x401


@pytest.mark.parametrize(
    "command",
    [
        "cameraxsize",
        "gain",
        "readoutmode",
        "ccdtemperature",
        "cooleron",
        "coolerpower",
    ],
)
def test_hardware_dependent_property_requires_connection(command, disconnected_client):
    response = disconnected_client.simulate_get(f"/api/v1/camera/0/{command}")
    assert response.status_code == 200
    assert response.json["ErrorNumber"] == NOT_CONNECTED
