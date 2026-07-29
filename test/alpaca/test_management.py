from __future__ import annotations

from types import SimpleNamespace

import falcon.testing
import pytest

from touptek4040.alpaca.app import create_app
from touptek4040.alpaca.management import stable_unique_id


_SERIAL_UID = "cbe660a1-348e-5dac-b4ea-5d045559dddb"
_SIMULATOR_UID = "80454357-54e8-5ca5-8477-289de5d91d90"
_MANAGEMENT_PATHS = (
    "/management/apiversions",
    "/management/v1/description",
    "/management/v1/configureddevices",
)


def _assert_management_envelope(
    body: dict[str, object],
    expected_value: object,
    client_transaction_id: int,
) -> None:
    assert body == {
        "Value": expected_value,
        "ClientTransactionID": client_transaction_id,
        "ServerTransactionID": body["ServerTransactionID"],
        "ErrorNumber": 0,
        "ErrorMessage": "",
    }
    assert type(body["ClientTransactionID"]) is int
    assert type(body["ServerTransactionID"]) is int
    assert body["ServerTransactionID"] > 0
    assert type(body["ErrorNumber"]) is int
    assert type(body["ErrorMessage"]) is str


def test_api_versions_has_exact_official_value_and_envelope(client):
    response = client.simulate_get(
        "/management/apiversions",
        params={"CLIENTTRANSACTIONID": "17"},
    )

    assert response.status_code == 200
    assert response.content_type == "application/json"
    _assert_management_envelope(response.json, [1], 17)
    assert all(type(version) is int for version in response.json["Value"])


def test_description_has_approved_exact_fields_values_and_types(client):
    response = client.simulate_get(
        "/management/v1/description",
        params={"clientTransactionId": "18"},
    )

    assert response.status_code == 200
    assert response.content_type == "application/json"
    expected = {
        "ServerName": "ToupTek 4040 Alpaca Server",
        "Manufacturer": "Hangzhou ToupTek Photonics Co., Ltd.",
        "ManufacturerVersion": "0.1.0",
        "Location": "Local",
    }
    _assert_management_envelope(response.json, expected, 18)
    assert all(type(value) is str for value in response.json["Value"].values())


def test_configured_devices_contains_camera_zero_and_serial_derived_uid(client):
    response = client.simulate_get(
        "/management/v1/configureddevices",
        params={"ClientTransactionID": "19"},
    )

    expected = [
        {
            "DeviceName": "ToupTek 4040 Alpaca Camera",
            "DeviceType": "Camera",
            "DeviceNumber": 0,
            "UniqueID": _SERIAL_UID,
        }
    ]
    assert response.status_code == 200
    _assert_management_envelope(response.json, expected, 19)
    device = response.json["Value"][0]
    assert type(device["DeviceName"]) is str
    assert type(device["DeviceType"]) is str
    assert type(device["DeviceNumber"]) is int
    assert type(device["UniqueID"]) is str


def test_unique_id_is_stable_across_application_restarts(coordinator, config):
    first = falcon.testing.TestClient(create_app(coordinator, config))
    second = falcon.testing.TestClient(create_app(coordinator, config))

    first_uid = first.simulate_get(
        "/management/v1/configureddevices"
    ).json["Value"][0]["UniqueID"]
    second_uid = second.simulate_get(
        "/management/v1/configureddevices"
    ).json["Value"][0]["UniqueID"]

    assert first_uid == second_uid == _SERIAL_UID


def test_simulator_without_configured_serial_has_deterministic_unique_id(
    coordinator,
):
    config = SimpleNamespace(
        camera=SimpleNamespace(
            source="simulator",
            serial=None,
            readout_mode="RAW 12-bit",
            gain=100,
        )
    )

    first = falcon.testing.TestClient(create_app(coordinator, config))
    second = falcon.testing.TestClient(create_app(coordinator, config))

    assert (
        first.simulate_get("/management/v1/configureddevices")
        .json["Value"][0]["UniqueID"]
        == _SIMULATOR_UID
    )
    assert (
        second.simulate_get("/management/v1/configureddevices")
        .json["Value"][0]["UniqueID"]
        == _SIMULATOR_UID
    )


def test_real_source_without_resolved_serial_fails_fast():
    with pytest.raises(
        ValueError,
        match="real camera serial must be resolved before creating the Alpaca app",
    ):
        stable_unique_id(None, "toupcam")


def test_serial_uid_is_stable_and_distinguishes_other_physical_cameras():
    same_first = stable_unique_id(
        "TP241025143218C92B01151AEC40CB7",
        "toupcam",
    )
    same_restart = stable_unique_id(
        "TP241025143218C92B01151AEC40CB7",
        "toupcam",
    )
    other = stable_unique_id(
        "TP241025143218C92B01151AEC40CB8",
        "toupcam",
    )

    assert same_first == same_restart == _SERIAL_UID
    assert other != same_first


def test_management_ignores_unknown_optional_parameters(client):
    response = client.simulate_get(
        "/management/v1/description",
        params={"future-extension": "ignored"},
    )

    assert response.status_code == 200
    assert response.json["ErrorNumber"] == 0
    assert response.json["ClientTransactionID"] == 0


@pytest.mark.parametrize("path", _MANAGEMENT_PATHS)
@pytest.mark.parametrize(
    ("query_string", "expected_transaction_id"),
    [
        ("", 0),
        ("ClientID=0&ClientTransactionID=0", 0),
        (
            "CLIENTID=4294967295&clienttransactionid=4294967295",
            0xFFFFFFFF,
        ),
        ("ClientID=0007&ClientTransactionID=0008", 8),
        ("ClientID=9&ClientID=9&ClientTransactionID=10", 10),
        ("ClientID=9&clientid=9&ClientTransactionID=10", 10),
    ],
)
def test_management_uint32_query_parameters_follow_official_zero_inclusive_range(
    client,
    path,
    query_string,
    expected_transaction_id,
):
    response = client.simulate_get(path, query_string=query_string)

    assert response.status_code == 200
    assert response.json["ClientTransactionID"] == expected_transaction_id
    assert response.json["ErrorNumber"] == 0


@pytest.mark.parametrize("path", _MANAGEMENT_PATHS)
@pytest.mark.parametrize("parameter", ["ClientID", "ClientTransactionID"])
@pytest.mark.parametrize(
    "bad_value",
    ["", "-1", "+1", "4294967296", "true", "1.0", "１２"],
)
def test_management_rejects_malformed_recognized_query_parameters(
    client,
    path,
    parameter,
    bad_value,
):
    response = client.simulate_get(
        path,
        query_string=f"{parameter}={bad_value}",
    )

    assert response.status_code == 400
    assert response.content_type == "application/json"
    assert response.json == {
        "ClientTransactionID": 0,
        "ServerTransactionID": response.json["ServerTransactionID"],
        "ErrorNumber": 0x401,
        "ErrorMessage": "Invalid management query parameters",
    }
    assert response.json["ServerTransactionID"] > 0
    if bad_value:
        assert bad_value not in response.text


@pytest.mark.parametrize("path", _MANAGEMENT_PATHS)
@pytest.mark.parametrize("parameter", ["ClientID", "ClientTransactionID"])
def test_management_rejects_conflicting_duplicate_recognized_parameters(
    client,
    path,
    parameter,
):
    response = client.simulate_get(
        path,
        query_string=f"{parameter}=1&{parameter.lower()}=2",
    )

    assert response.status_code == 400
    assert response.json["ErrorNumber"] == 0x401
    assert response.json["ErrorMessage"] == "Invalid management query parameters"
