from __future__ import annotations

import time


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


def test_async_failure_is_exposed_by_image_and_state_queries_with_stable_code(
    client, coordinator, api_camera
):
    api_camera.failure = RuntimeError("USB transfer event 0x0081 failed")
    api_camera.release_acquisition.clear()
    started = _put(client, "startexposure", Duration="0.1", Light="true")
    assert started.json["ErrorNumber"] == 0
    assert api_camera.acquisition_started.wait(1.0)
    api_camera.release_acquisition.set()
    _wait_until(lambda: coordinator.snapshot().camera_state == 5)

    messages = []
    for command in ("camerastate", "imageready", "percentcompleted"):
        response = _get(client, command)
        assert response.status_code == 200
        assert response.json["ErrorNumber"] == 0x500
        assert "USB transfer event 0x0081 failed" in response.json["ErrorMessage"]
        messages.append(response.json["ErrorMessage"])
    assert len(set(messages)) == 1
    assert coordinator.frame is None


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
