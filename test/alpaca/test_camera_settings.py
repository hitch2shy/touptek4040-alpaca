from __future__ import annotations

from datetime import datetime, timezone

import pytest

from touptek4040.alpaca.responses import (
    INVALID_OPERATION,
    INVALID_VALUE,
    METHOD_NOT_IMPLEMENTED,
    VALUE_NOT_SET,
)


def get_value(client, command: str):
    response = client.simulate_get(f"/api/v1/camera/0/{command}")
    assert response.json["ErrorNumber"] == 0
    return response.json["Value"]


def put_value(client, command: str, parameter: str, value: object):
    return client.simulate_put(
        f"/api/v1/camera/0/{command}",
        json={parameter: value},
    )


@pytest.mark.parametrize("command", ["binx", "biny"])
@pytest.mark.parametrize("binning", range(1, 9))
def test_writing_either_bin_axis_synchronizes_both(command, binning, client):
    response = put_value(client, command, command, str(binning))
    assert response.json["ErrorNumber"] == 0
    assert get_value(client, "binx") == binning
    assert get_value(client, "biny") == binning


@pytest.mark.parametrize("bad_value", [0, 9, -1, 1.5, "1.0", True, "", None])
def test_binning_rejects_invalid_values_without_changing_state(bad_value, client):
    response = put_value(client, "binx", "BinX", bad_value)
    assert response.status_code == 200
    assert response.json["ErrorNumber"] == INVALID_VALUE
    assert get_value(client, "binx") == 1
    assert get_value(client, "biny") == 1


@pytest.mark.parametrize(
    ("command", "parameter", "valid", "invalid"),
    [
        ("startx", "StartX", 4095, 4096),
        ("starty", "StartY", 4095, -1),
        ("numx", "NumX", 1, 0),
        ("numy", "NumY", 4096, 4097),
    ],
)
def test_roi_fields_enforce_individual_ranges_but_defer_combination_validation(
    command, parameter, valid, invalid, client
):
    assert put_value(client, command, parameter, valid).json["ErrorNumber"] == 0
    assert get_value(client, command) == valid
    assert (
        put_value(client, command, parameter, invalid).json["ErrorNumber"]
        == INVALID_VALUE
    )


def test_binning_write_does_not_reject_temporarily_invalid_roi_combination(client):
    assert put_value(client, "startx", "StartX", 4095).json["ErrorNumber"] == 0
    assert put_value(client, "binx", "BinX", 8).json["ErrorNumber"] == 0
    assert get_value(client, "startx") == 4095
    assert get_value(client, "numx") == 4096


def test_gain_is_numeric_and_bounded(client):
    assert get_value(client, "gainmin") == 100
    assert get_value(client, "gainmax") == 1650
    assert put_value(client, "gain", "Gain", "1650").json["ErrorNumber"] == 0
    assert get_value(client, "gain") == 1650
    assert (
        put_value(client, "gain", "Gain", "1651").json["ErrorNumber"]
        == INVALID_VALUE
    )


def test_readout_mode_updates_maxadu_immediately(client):
    assert get_value(client, "readoutmode") == 0
    assert get_value(client, "maxadu") == 4095
    assert (
        put_value(client, "readoutmode", "ReadoutMode", "1").json["ErrorNumber"]
        == 0
    )
    assert get_value(client, "readoutmode") == 1
    assert get_value(client, "maxadu") == 255


@pytest.mark.parametrize("bad_value", [-1, 2, 1.5, "RAW 8-bit", True, None])
def test_readout_mode_rejects_invalid_enum_values(bad_value, client):
    response = put_value(client, "readoutmode", "ReadoutMode", bad_value)
    assert response.status_code == 200
    assert response.json["ErrorNumber"] == INVALID_VALUE


def test_temperature_and_cooler_round_trip_through_coordinator(client, api_camera):
    assert get_value(client, "ccdtemperature") == -4.5
    assert get_value(client, "setccdtemperature") == -10.0
    assert (
        put_value(
            client, "setccdtemperature", "SetCCDTemperature", "-20.5"
        ).json["ErrorNumber"]
        == 0
    )
    assert api_camera.target_temperature_c == -20.5
    assert put_value(client, "cooleron", "CoolerOn", "true").json["ErrorNumber"] == 0
    assert api_camera.cooler_on is True
    assert get_value(client, "coolerpower") == 50.0


@pytest.mark.parametrize("bad_target", [-50.1, 40.1, "nan", "inf", True, None])
def test_temperature_target_rejects_invalid_values(bad_target, client):
    response = put_value(
        client, "setccdtemperature", "SetCCDTemperature", bad_target
    )
    assert response.status_code == 200
    assert response.json["ErrorNumber"] == INVALID_VALUE


def test_snapshot_properties_and_last_exposure_metadata(client, coordinator):
    assert get_value(client, "camerastate") == 0
    assert get_value(client, "imageready") is False
    assert (
        client.simulate_get("/api/v1/camera/0/percentcompleted").json["ErrorNumber"]
        == INVALID_OPERATION
    )
    assert (
        client.simulate_get(
            "/api/v1/camera/0/lastexposureduration"
        ).json["ErrorNumber"]
        == VALUE_NOT_SET
    )

    coordinator.start_exposure(0.001, True)
    thread = coordinator._exposure_thread
    assert thread is not None
    thread.join()
    assert get_value(client, "imageready") is True
    assert get_value(client, "lastexposureduration") == 0.001
    assert (
        get_value(client, "lastexposurestarttime")
        == datetime(2026, 7, 28, 12, 34, 56, tzinfo=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def test_abort_exposure_delegates_to_coordinator(client, coordinator, api_camera):
    api_camera.release_acquisition.clear()
    coordinator.start_exposure(0.001, True)
    assert api_camera.acquisition_started.wait(timeout=1)
    response = client.simulate_put("/api/v1/camera/0/abortexposure")
    assert response.json["ErrorNumber"] == 0
    assert api_camera.abort_calls >= 1


def test_stop_exposure_is_idle_noop_and_canstop_is_false(client):
    assert get_value(client, "canstopexposure") is False
    response = client.simulate_put("/api/v1/camera/0/stopexposure")
    assert response.json["ErrorNumber"] == 0


def test_start_exposure_validates_parameters_without_native_exceptions(client):
    response = client.simulate_put(
        "/api/v1/camera/0/startexposure",
        json={"Duration": "not-a-number", "Light": "true"},
    )
    assert response.status_code == 200
    assert response.json["ErrorNumber"] == INVALID_VALUE


def test_start_exposure_validates_the_nearest_integer_microsecond(client, coordinator):
    response = client.simulate_put(
        "/api/v1/camera/0/startexposure",
        json={"Duration": "0.0000116", "Light": "false"},
    )
    assert response.json["ErrorNumber"] == 0
    thread = coordinator._exposure_thread
    assert thread is not None
    thread.join()
    assert coordinator.frame is not None
    assert coordinator.frame.duration_s == 0.000012


def test_stop_exposure_is_not_implemented_while_exposing(
    client, coordinator, monkeypatch
):
    # The branch itself is covered with a state double because stopping an
    # actual acquisition races the intentionally minimal test camera.
    from touptek4040.camera_coordinator import CameraSnapshot, CameraState

    monkeypatch.setattr(
        coordinator,
        "snapshot",
        lambda: CameraSnapshot(True, False, CameraState.EXPOSING, False, 0, None),
    )
    response = client.simulate_put("/api/v1/camera/0/stopexposure")
    assert response.json["ErrorNumber"] == METHOD_NOT_IMPLEMENTED
