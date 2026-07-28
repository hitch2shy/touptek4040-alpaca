from __future__ import annotations

import time

import pytest

from touptek4040.errors import (
    CameraError,
    CameraNotConnectedError,
    CameraTimeoutError,
)


def _get(client, command: str):
    return client.simulate_get(f"/api/v1/camera/0/{command}")


def _put(client, command: str, **values: object):
    return client.simulate_put(f"/api/v1/camera/0/{command}", json=values)


def _wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        time.sleep(0.005)


def _failure_messages(client) -> list[str]:
    messages = []
    for command in ("camerastate", "imageready", "percentcompleted"):
        response = _get(client, command)
        assert response.status_code == 200
        assert response.json["ErrorNumber"] == 0x500
        messages.append(response.json["ErrorMessage"])
    assert len(set(messages)) == 1
    return messages


class ThrowingTextCameraError(CameraError):
    def __str__(self):
        raise RuntimeError("secondary string conversion failure")


class NonStringTextCameraError(CameraError):
    def __str__(self):
        return 42


class ThrowingTextBaseError(BaseException):
    def __str__(self):
        raise RuntimeError("secondary base-exception conversion failure")


class LongMultilineCameraError(CameraError):
    def __str__(self):
        return "HRESULT 0x8001011F\nSECRET\n" + "A" * 4000


def test_async_failure_is_exposed_by_image_and_state_queries_with_stable_code(
    client, coordinator, api_camera
):
    for command, values in (
        ("binx", {"BinX": "2"}),
        ("startx", {"StartX": "2"}),
        ("starty", {"StartY": "4"}),
        ("numx", {"NumX": "32"}),
        ("numy", {"NumY": "24"}),
        ("readoutmode", {"ReadoutMode": "1"}),
        ("gain", {"Gain": "1650"}),
    ):
        assert _put(client, command, **values).json["ErrorNumber"] == 0
    api_camera.failure = RuntimeError(
        "USB transfer event 0x0081 failed\nAuthorization: secret-token"
    )
    api_camera.release_acquisition.clear()
    started = _put(client, "startexposure", Duration="0.125", Light="true")
    assert started.json["ErrorNumber"] == 0
    assert api_camera.acquisition_started.wait(1.0)
    api_camera.release_acquisition.set()
    _wait_until(lambda: coordinator.snapshot().camera_state == 5)

    message = _failure_messages(client)[0]
    for field in (
        "operation=acquire frame",
        "CameraState=Error(5)",
        "duration=0.125000s",
        "roi=(x=2,y=4,width=32,height=24)",
        "binning=2",
        "readout=RAW8",
        "gain=1650",
        "cause=RuntimeError",
        "event=0x0081",
        "summary=USB transfer event 0x0081 failure",
        "recovery=",
    ):
        assert field in message
    assert "secret-token" not in message
    assert "\n" not in message
    assert len(message) <= 700
    assert coordinator.frame is None


def test_gain_failure_diagnostic_identifies_operation_and_recovery(
    client, coordinator, api_camera
):
    api_camera.gain_error = CameraNotConnectedError(
        "camera disconnected while applying gain"
    )

    started = _put(client, "startexposure", Duration="0.25", Light="false")

    assert started.json["ErrorNumber"] == 0
    _wait_until(lambda: coordinator.snapshot().camera_state == 5)
    message = _failure_messages(client)[0]
    assert "operation=apply gain" in message
    assert "CameraState=Error(5)" in message
    assert "duration=0.250000s" in message
    assert "cause=CameraNotConnectedError" in message
    assert "summary=camera disconnected" in message
    assert "recovery=Disconnect, reconnect the camera, then retry." in message


def test_timeout_hresult_diagnostic_has_stable_code_and_safe_summary(
    client, coordinator, api_camera
):
    api_camera.failure = CameraTimeoutError(
        "acquire frame failed with HRESULT 0x8001011F"
    )

    started = _put(client, "startexposure", Duration="0.5", Light="true")

    assert started.json["ErrorNumber"] == 0
    _wait_until(lambda: coordinator.snapshot().camera_state == 5)
    message = _failure_messages(client)[0]
    assert "operation=acquire frame" in message
    assert "cause=CameraTimeoutError" in message
    assert "hresult=0x8001011F" in message
    assert "summary=camera acquisition timed out" in message
    assert "recovery=Check the USB link, reconnect the camera, then retry." in message


def test_async_diagnostic_bounds_and_allowlists_all_untrusted_metadata(
    client, coordinator, api_camera
):
    untrusted_type = type("SecretCause" + "A" * 2000, (RuntimeError,), {})
    api_camera.failure = untrusted_type(
        "HRESULT 0x8001011f USB transfer event 0x0083 "
        + "A" * 2000
        + " event 0x"
        + "A" * 2000
        + " event 0xDEADBEEF"
        + " token 0xCAFEBABE"
        + " HRESULT 0x123456789"
    )

    started = _put(client, "startexposure", Duration="0.5", Light="true")

    assert started.json["ErrorNumber"] == 0
    _wait_until(lambda: coordinator.snapshot().camera_state == 5)
    message = _failure_messages(client)[0]
    assert len(message) <= 700
    assert "cause=Exception" in message
    assert "hresult=0x8001011F" in message
    assert "event=0x0083" in message
    assert "summary=camera no-packet timeout event 0x0083" in message
    assert "recovery=Check the USB link, reconnect the camera, then retry." in message
    for untrusted in (
        "SecretCause",
        "A" * 100,
        "0xDEADBEEF",
        "0xCAFEBABE",
        "0x123456789",
    ):
        assert untrusted not in message


@pytest.mark.parametrize("failure_stage", ["gain", "acquire"])
@pytest.mark.parametrize(
    "failure",
    [
        ThrowingTextCameraError(),
        NonStringTextCameraError(),
        ThrowingTextBaseError(),
        LongMultilineCameraError(),
    ],
    ids=("throwing-str", "non-string-str", "base-exception", "long-multiline"),
)
def test_pathological_backend_exception_always_publishes_one_safe_failure(
    client,
    coordinator,
    api_camera,
    failure_stage,
    failure,
):
    if failure_stage == "gain":
        api_camera.gain_error = failure
    else:
        api_camera.failure = failure

    started = _put(client, "startexposure", Duration="0.5", Light="true")

    assert started.json["ErrorNumber"] == 0
    if failure_stage == "acquire":
        assert api_camera.acquisition_started.wait(1.0)
        api_camera.release_acquisition.set()
    _wait_until(lambda: coordinator.snapshot().camera_state == 5)
    thread = coordinator._exposure_thread
    assert thread is not None
    thread.join(1.0)
    assert thread.is_alive() is False

    message = _failure_messages(client)[0]
    assert message.startswith("AsyncExposureFailure;")
    assert f"operation={'apply gain' if failure_stage == 'gain' else 'acquire frame'}" in message
    assert "CameraState=Error(5)" in message
    assert "cause=Exception" in message
    assert "summary=" in message
    assert "recovery=" in message
    assert len(message) <= 700
    assert "\n" not in message
    assert "SECRET" not in message
    assert "secondary" not in message


def test_new_exposure_clears_previous_image_and_async_failure(
    client, coordinator, api_camera
):
    completed = _put(client, "startexposure", Duration="0.1", Light="true")
    assert completed.json["ErrorNumber"] == 0
    thread = coordinator._exposure_thread
    assert thread is not None
    thread.join(1.0)
    assert coordinator.frame is not None

    api_camera.failure = RuntimeError("first exposure failed")
    api_camera.acquisition_started.clear()
    failed = _put(client, "startexposure", Duration="0.1", Light="true")
    assert failed.json["ErrorNumber"] == 0
    assert coordinator.frame is None
    _wait_until(lambda: coordinator.snapshot().camera_state == 5)

    api_camera.failure = None
    api_camera.release_acquisition.clear()
    api_camera.acquisition_started.clear()
    retry = _put(client, "startexposure", Duration="0.2", Light="false")

    assert retry.json["ErrorNumber"] == 0
    assert coordinator.snapshot().camera_state == 2
    assert coordinator.snapshot().error is None
    assert coordinator.frame is None
    assert api_camera.acquisition_started.wait(1.0)
    api_camera.release_acquisition.set()
    _wait_until(lambda: coordinator.snapshot().image_ready)


def test_device_state_contains_only_official_camera_operational_fields(client):
    response = _get(client, "devicestate")
    assert response.json["ErrorNumber"] == 0
    values = {item["Name"]: item["Value"] for item in response.json["Value"]}

    assert set(values) == {
        "CameraState",
        "CCDTemperature",
        "CoolerPower",
        "ImageReady",
        "IsPulseGuiding",
        "PercentCompleted",
        "TimeStamp",
    }
    assert values["CameraState"] == 0
    assert values["ImageReady"] is False
    assert values["IsPulseGuiding"] is False
    assert values["PercentCompleted"] == 0
    assert values["TimeStamp"].endswith("Z")
