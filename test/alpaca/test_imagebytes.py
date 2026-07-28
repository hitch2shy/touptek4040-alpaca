from __future__ import annotations

import struct
import time

import pytest

from test.fixtures.reference_pixels import reference_frame
from touptek4040.alpaca.image_api import ImageTransactions, stream_imagebytes
from touptek4040.camera_contract import ReadoutMode
from touptek4040.image_workspace import ImageWorkspace

_METADATA = struct.Struct("<iiIIiiiiiii")


def _decode_metadata(payload: bytes) -> tuple[int, ...]:
    return _METADATA.unpack_from(payload)


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


def test_raw12_imagebytes_has_official_v1_metadata_and_x_first_uint16_payload():
    generation = ImageWorkspace().publish(reference_frame())

    payload = b"".join(
        stream_imagebytes(
            generation,
            ImageTransactions(
                client_transaction_id=17,
                server_transaction_id=23,
            ),
        )
    )

    assert _METADATA.size == 44
    assert _decode_metadata(payload) == (
        1,
        0,
        17,
        23,
        44,
        8,
        8,
        2,
        3,
        2,
        0,
    )
    assert payload[44:] == struct.pack("<6H", 1, 10, 2, 20, 3, 30)


def test_raw8_imagebytes_uses_byte_element_types_and_one_byte_pixels():
    generation = ImageWorkspace().publish(
        reference_frame(readout_mode=ReadoutMode.RAW8)
    )

    payload = b"".join(
        stream_imagebytes(
            generation,
            ImageTransactions(
                client_transaction_id=5,
                server_transaction_id=6,
            ),
        )
    )

    assert _decode_metadata(payload)[5:7] == (6, 6)
    assert payload[44:] == bytes((1, 10, 2, 20, 3, 30))


def test_explicit_imagebytes_accept_returns_binary_with_route_transactions(
    client,
    coordinator,
):
    _complete_small_exposure(client, coordinator)

    response = client.simulate_get(
        "/api/v1/camera/0/imagearray",
        params={"ClientTransactionID": "91"},
        headers={
            "Accept": "application/json;q=1, Application/ImageBytes;q=0.5",
        },
    )

    assert response.status_code == 200
    assert response.content_type == "application/imagebytes"
    metadata = _decode_metadata(response.content)
    assert metadata[0] == 1
    assert metadata[1] == 0
    assert metadata[2] == 91
    assert metadata[3] > 0
    assert metadata[4] == 44
    assert metadata[8:11] == (16, 16, 0)


@pytest.mark.parametrize(
    ("fixture_name", "expected_error"),
    [
        ("disconnected_client", 0x407),
        ("client", 0x40B),
    ],
)
def test_imagebytes_errors_use_official_header_and_utf8_message(
    request,
    fixture_name,
    expected_error,
):
    client = request.getfixturevalue(fixture_name)

    response = client.simulate_get(
        "/api/v1/camera/0/imagearray",
        params={"ClientTransactionID": "101"},
        headers={"Accept": "application/imagebytes"},
    )

    assert response.status_code == 200
    assert response.content_type == "application/imagebytes"
    metadata = _decode_metadata(response.content)
    assert metadata[:3] == (1, expected_error, 101)
    assert metadata[3] > 0
    assert metadata[4:] == (44, 0, 0, 0, 0, 0, 0)
    assert response.content[44:].decode("utf-8")
