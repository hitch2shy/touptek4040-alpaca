from __future__ import annotations

import time
import threading

from touptek4040.alpaca.responses import (
    INVALID_OPERATION,
    INVALID_VALUE,
    METHOD_NOT_IMPLEMENTED,
)
from touptek4040.camera_contract import ReadoutMode, Roi


def _get(client, command: str):
    return client.simulate_get(f"/api/v1/camera/0/{command}")


def _put(client, command: str, **values: object):
    return client.simulate_put(f"/api/v1/camera/0/{command}", json=values)


def _value(client, command: str):
    response = _get(client, command)
    assert response.json["ErrorNumber"] == 0
    return response.json["Value"]


def _wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        time.sleep(0.005)


def _set_non_default_capture_settings(client) -> None:
    changes = (
        ("binx", {"BinX": "2"}),
        ("startx", {"StartX": "2"}),
        ("starty", {"StartY": "4"}),
        ("numx", {"NumX": "32"}),
        ("numy", {"NumY": "24"}),
        ("readoutmode", {"ReadoutMode": "1"}),
        ("gain", {"Gain": "1650"}),
    )
    for command, values in changes:
        response = _put(client, command, **values)
        assert response.json["ErrorNumber"] == 0


def test_start_exposure_is_nonblocking_and_uses_one_frozen_settings_snapshot(
    client, coordinator, api_camera
):
    _set_non_default_capture_settings(client)
    api_camera.release_acquisition.clear()

    started = time.monotonic()
    response = _put(
        client,
        "startexposure",
        Duration="0.25",
        Light="false",
    )

    assert response.json["ErrorNumber"] == 0
    assert time.monotonic() - started < 0.1
    assert _value(client, "camerastate") == 2
    assert _value(client, "imageready") is False
    assert api_camera.acquisition_started.wait(1.0)
    assert api_camera.requested_acquisitions == [
        (0.25, Roi(2, 4, 32, 24), 2, ReadoutMode.RAW8, 1650)
    ]

    api_camera.release_acquisition.set()
    _wait_until(lambda: coordinator.snapshot().image_ready)
    frame = coordinator.frame
    assert frame is not None
    assert frame.roi == Roi(2, 4, 32, 24)
    assert frame.binning == 2
    assert frame.readout_mode is ReadoutMode.RAW8
    assert frame.gain == 1650
    assert _value(client, "lastexposureduration") == 0.25
    assert _value(client, "lastexposurestarttime") == "2026-07-28T12:34:56Z"


def test_duration_validation_and_settings_snapshot_share_one_linearization(
    client, coordinator, api_camera, monkeypatch
):
    from touptek4040.camera_coordinator import CameraCoordinator

    validation_entered = threading.Event()
    allow_validation = threading.Event()
    setting_finished = threading.Event()
    responses = {}
    original = CameraCoordinator._validated_duration

    def paused_validation(duration_s):
        if threading.current_thread().name == "exposure-put":
            validation_entered.set()
            assert allow_validation.wait(1.0)
        return original(duration_s)

    monkeypatch.setattr(
        CameraCoordinator,
        "_validated_duration",
        staticmethod(paused_validation),
    )
    api_camera.release_acquisition.clear()

    def start_exposure():
        responses["exposure"] = _put(
            client,
            "startexposure",
            Duration="0.1",
            Light="true",
        )

    def write_gain():
        responses["gain"] = _put(client, "gain", Gain="200")
        setting_finished.set()

    starter = threading.Thread(target=start_exposure, name="exposure-put")
    writer = threading.Thread(target=write_gain, name="gain-put")
    starter.start()
    assert validation_entered.wait(1.0)
    writer.start()
    try:
        assert not setting_finished.wait(0.05)
        allow_validation.set()
        starter.join(1.0)
        writer.join(1.0)
        assert responses["exposure"].json["ErrorNumber"] == 0
        assert responses["gain"].json["ErrorNumber"] == INVALID_OPERATION
        assert api_camera.acquisition_started.wait(1.0)
        assert api_camera.requested_acquisitions[0][-1] == 100
    finally:
        allow_validation.set()
        if coordinator.snapshot().camera_state == 2:
            coordinator.abort_exposure()
        starter.join(1.0)
        writer.join(1.0)


def test_stop_exposure_is_not_implemented_when_capability_is_false(
    client, coordinator, api_camera
):
    assert (
        _put(client, "stopexposure").json["ErrorNumber"]
        == METHOD_NOT_IMPLEMENTED
    )
    api_camera.release_acquisition.clear()
    assert (
        _put(client, "startexposure", Duration="1", Light="true").json[
            "ErrorNumber"
        ]
        == 0
    )
    assert api_camera.acquisition_started.wait(1.0)

    response = _put(client, "stopexposure")

    assert response.json["ErrorNumber"] == METHOD_NOT_IMPLEMENTED
    coordinator.abort_exposure()


def test_percent_completed_is_only_available_during_an_exposure(
    client, coordinator, api_camera
):
    assert _get(client, "percentcompleted").json["ErrorNumber"] == INVALID_OPERATION
    api_camera.release_acquisition.clear()
    assert (
        _put(client, "startexposure", Duration="1", Light="true").json[
            "ErrorNumber"
        ]
        == 0
    )
    assert api_camera.acquisition_started.wait(1.0)
    percent = _value(client, "percentcompleted")
    assert 0 <= percent <= 100
    coordinator.abort_exposure()
    assert _get(client, "percentcompleted").json["ErrorNumber"] == INVALID_OPERATION


def test_ascom_one_pixel_odd_origin_roi_reaches_camera_adapter(
    client,
    api_camera,
):
    for command, value in (
        ("startx", 1),
        ("starty", 1),
        ("numx", 1),
        ("numy", 1),
    ):
        assert _put(client, command, **{command: value}).json["ErrorNumber"] == 0

    assert (
        _put(client, "startexposure", Duration="0.01", Light="true").json[
            "ErrorNumber"
        ]
        == 0
    )
    assert api_camera.acquisition_started.wait(1.0)
    assert api_camera.requested_acquisitions[0][1] == Roi(1, 1, 1, 1)


def test_abort_invalidates_a_late_frame_and_returns_to_idle(
    client, coordinator, api_camera
):
    api_camera.release_acquisition.clear()
    assert (
        _put(client, "startexposure", Duration="1", Light="true").json[
            "ErrorNumber"
        ]
        == 0
    )
    assert api_camera.acquisition_started.wait(1.0)

    aborted = _put(client, "abortexposure")
    api_camera.release_acquisition.set()
    thread = coordinator._exposure_thread
    assert thread is not None
    thread.join(1.0)

    assert aborted.json["ErrorNumber"] == 0
    assert _value(client, "camerastate") == 0
    assert _value(client, "imageready") is False
    assert coordinator.frame is None


def test_start_exposure_validates_binned_roi_before_starting(
    client, api_camera
):
    assert _put(client, "binx", BinX="2").json["ErrorNumber"] == 0
    assert _put(client, "startx", StartX="2040").json["ErrorNumber"] == 0
    assert _put(client, "numx", NumX="16").json["ErrorNumber"] == 0

    response = _put(client, "startexposure", Duration="0.1", Light="true")

    assert response.json["ErrorNumber"] == INVALID_VALUE
    assert "NumX" in response.json["ErrorMessage"]
    assert api_camera.requested_acquisitions == []


def test_light_is_parsed_but_ignored_for_a_shutterless_camera(
    client, coordinator, api_camera
):
    invalid = _put(client, "startexposure", Duration="0.1", Light="open")
    assert invalid.status_code == 400
    assert invalid.json["ErrorNumber"] == INVALID_VALUE
    assert api_camera.requested_acquisitions == []

    valid = _put(client, "startexposure", Duration="0.1", Light="false")
    assert valid.json["ErrorNumber"] == 0
    thread = coordinator._exposure_thread
    assert thread is not None
    thread.join(1.0)
    assert coordinator.frame is not None
