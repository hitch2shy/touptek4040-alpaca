from __future__ import annotations

import json
import time

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
