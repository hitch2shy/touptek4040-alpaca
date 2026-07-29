from __future__ import annotations

import json
import struct
import threading
import time

import pytest

from test.fixtures.reference_pixels import reference_frame, x_first_values
from touptek4040.alpaca.image_api import ImageTransactions, stream_image_json
from touptek4040.camera_coordinator import CameraState
from touptek4040.errors import CameraError
from touptek4040.image_workspace import ImageWorkspace


def _wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        time.sleep(0.005)


def _complete_small_exposure(client, coordinator) -> None:
    for command, value in (("numx", 16), ("numy", 16)):
        response = client.simulate_put(
            f"/api/v1/camera/0/{command}",
            json={command: value},
        )
        assert response.json["ErrorNumber"] == 0
    started = client.simulate_put(
        "/api/v1/camera/0/startexposure",
        json={"Duration": "0.1", "Light": "true"},
    )
    assert started.json["ErrorNumber"] == 0
    _wait_until(lambda: coordinator.snapshot().image_ready)


def test_streaming_json_uses_ascom_x_first_pixels_and_complete_envelope():
    generation = ImageWorkspace().publish(reference_frame())

    body = json.loads(
        b"".join(
            stream_image_json(
                generation,
                ImageTransactions(
                    client_transaction_id=17,
                    server_transaction_id=23,
                ),
            )
        )
    )

    assert body == {
        "Value": x_first_values(),
        "ClientTransactionID": 17,
        "ServerTransactionID": 23,
        "ErrorNumber": 0,
        "ErrorMessage": "",
    }
    assert body["Value"][2][1] == generation.pixels[1, 2]


def test_imagearray_defaults_to_streaming_json_without_explicit_imagebytes_accept(
    client,
    coordinator,
):
    _complete_small_exposure(client, coordinator)
    generation = coordinator.image_generation
    assert generation is not None

    response = client.simulate_get(
        "/api/v1/camera/0/imagearray",
        params={"ClientTransactionID": "41"},
        headers={"Accept": "*/*"},
    )

    assert response.status_code == 200
    assert response.content_type == "application/json"
    assert response.json["Value"] == [[0] * 16 for _ in range(16)]
    assert response.json["ClientTransactionID"] == 41
    assert response.json["ServerTransactionID"] > 0
    assert response.json["ErrorNumber"] == 0
    assert coordinator.image_generation is generation
    assert coordinator.frame is generation.frame


def test_imagebytes_with_zero_quality_is_not_negotiated(client, coordinator):
    _complete_small_exposure(client, coordinator)

    response = client.simulate_get(
        "/api/v1/camera/0/imagearray",
        headers={
            "Accept": "application/imagebytes;q=0, application/json;q=1",
        },
    )

    assert response.content_type == "application/json"
    assert response.json["Value"] == [[0] * 16 for _ in range(16)]


def test_json_imagearray_reports_disconnected_no_image_and_async_errors(
    disconnected_client,
    client,
    coordinator,
    api_camera,
):
    coordinator.set_connected_sync(False)
    disconnected = disconnected_client.simulate_get(
        "/api/v1/camera/0/imagearray"
    )
    assert disconnected.status_code == 200
    assert disconnected.json["ErrorNumber"] == 0x407

    coordinator.set_connected_sync(True)
    no_image = client.simulate_get("/api/v1/camera/0/imagearray")
    assert no_image.status_code == 200
    assert no_image.json["ErrorNumber"] == 0x40B

    api_camera.failure = CameraError("image acquisition failed")
    started = client.simulate_put(
        "/api/v1/camera/0/startexposure",
        json={"Duration": "0.1", "Light": "true"},
    )
    assert started.json["ErrorNumber"] == 0
    _wait_until(lambda: coordinator.snapshot().camera_state is CameraState.ERROR)

    failed = client.simulate_get("/api/v1/camera/0/imagearray")
    assert failed.status_code == 200
    assert failed.json["ErrorNumber"] == 0x500
    assert failed.json["ErrorMessage"].startswith("AsyncExposureFailure;")


def test_imagearrayvariant_is_a_transactional_not_implemented_error(client):
    response = client.simulate_get(
        "/api/v1/camera/0/imagearrayvariant",
        params={"ClientTransactionID": "73"},
    )

    assert response.status_code == 200
    assert response.content_type == "application/json"
    assert response.json["ClientTransactionID"] == 73
    assert response.json["ServerTransactionID"] > 0
    assert response.json["ErrorNumber"] == 0x400


@pytest.mark.parametrize("command", ["imagearray", "imagearrayvariant"])
@pytest.mark.parametrize("invalid_client_id", ["", " ", "-1", "not-an-integer"])
def test_image_routes_reject_invalid_supplied_client_id(
    client,
    command,
    invalid_client_id,
):
    response = client.simulate_get(
        f"/api/v1/camera/0/{command}",
        params={
            "ClientID": invalid_client_id,
            "ClientTransactionID": "67890",
        },
    )

    assert response.status_code == 400
    assert response.json["ClientTransactionID"] == 67890
    assert response.json["ErrorNumber"] == 0x401


def _image_error_number(response, imagebytes: bool) -> int:
    if imagebytes:
        return struct.unpack_from("<i", response.content, 4)[0]
    return response.json["ErrorNumber"]


class _ObservableCondition:
    def __init__(self, condition, transition_attempted: threading.Event) -> None:
        self._condition = condition
        self._transition_attempted = transition_attempted

    def __enter__(self):
        if threading.current_thread().name == "formal-transition":
            self._transition_attempted.set()
        return self._condition.__enter__()

    def __exit__(self, *args):
        return self._condition.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self._condition, name)


@pytest.mark.parametrize("imagebytes", [False, True], ids=("json", "imagebytes"))
@pytest.mark.parametrize(
    ("transition", "final_error"),
    [
        ("disconnect", 0x407),
        ("async-failure", 0x500),
        ("new-exposure", 0x40B),
    ],
)
def test_formal_image_read_is_one_linearized_generation_decision(
    client,
    coordinator,
    api_camera,
    monkeypatch,
    imagebytes,
    transition,
    final_error,
):
    _complete_small_exposure(client, coordinator)
    original_generation = coordinator.image_generation
    assert original_generation is not None

    current_entered = threading.Event()
    current_release = threading.Event()
    workspace = coordinator._image_workspace
    original_current = workspace.current

    def blocked_current():
        current_entered.set()
        assert current_release.wait(1.0)
        return original_current()

    monkeypatch.setattr(workspace, "current", blocked_current)
    transition_attempted = threading.Event()
    original_condition = coordinator._condition
    coordinator._condition = _ObservableCondition(
        original_condition,
        transition_attempted,
    )
    headers = {"Accept": "application/imagebytes"} if imagebytes else None
    response_holder = {}

    def read_image() -> None:
        response_holder["response"] = client.simulate_get(
            "/api/v1/camera/0/imagearray",
            headers=headers,
        )

    read_thread = threading.Thread(target=read_image)
    read_thread.start()
    assert current_entered.wait(1.0)

    transition_started = threading.Event()

    def change_formal_state() -> None:
        transition_started.set()
        if transition == "disconnect":
            coordinator.set_connected_sync(False)
        elif transition == "async-failure":
            api_camera.failure = CameraError("formal read race failure")
            coordinator.start_exposure(0.1, True)
        else:
            api_camera.release_acquisition.clear()
            coordinator.start_exposure(0.1, True)

    transition_thread = threading.Thread(
        target=change_formal_state,
        name="formal-transition",
    )
    transition_thread.start()
    assert transition_started.wait(1.0)
    assert transition_attempted.wait(1.0)
    current_release.set()
    read_thread.join(1.0)
    transition_thread.join(1.0)
    assert read_thread.is_alive() is False
    assert transition_thread.is_alive() is False
    monkeypatch.setattr(workspace, "current", original_current)
    coordinator._condition = original_condition

    first = response_holder["response"]
    assert _image_error_number(first, imagebytes) == 0
    if imagebytes:
        assert struct.unpack_from("<ii", first.content) == (1, 0)
    else:
        assert first.json["Value"] == [[0] * 16 for _ in range(16)]

    if transition == "async-failure":
        _wait_until(lambda: coordinator.snapshot().camera_state is CameraState.ERROR)
    elif transition == "new-exposure":
        _wait_until(
            lambda: coordinator.snapshot().camera_state is CameraState.EXPOSING
        )

    final = client.simulate_get(
        "/api/v1/camera/0/imagearray",
        headers=headers,
    )
    assert _image_error_number(final, imagebytes) == final_error
    if transition == "async-failure":
        diagnostic = client.simulate_get(
            "/api/v1/camera/0/camerastate"
        ).json["ErrorMessage"]
        if imagebytes:
            assert final.content[44:].decode("utf-8") == diagnostic
        else:
            assert final.json["ErrorMessage"] == diagnostic
    if transition == "new-exposure":
        coordinator.abort_exposure()
        api_camera.release_acquisition.set()
