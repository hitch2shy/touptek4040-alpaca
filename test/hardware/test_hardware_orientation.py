from __future__ import annotations

import json

import pytest

from touptek4040.alpaca.image_api import ImageTransactions, stream_image_json
from touptek4040.camera_contract import ReadoutMode, Roi
from touptek4040.image_workspace import ImageWorkspace


@pytest.mark.hardware
def test_real_asymmetric_frame_preserves_yx_storage_and_ascom_x_first_order(
    real_camera,
    hardware_session,
    hardware_evidence,
) -> None:
    frame = real_camera.acquire(
        0.01,
        Roi(10, 12, 96, 64),
        1,
        ReadoutMode.RAW12,
    )
    assert frame.pixels.shape == (64, 96)

    generation = ImageWorkspace().publish(frame)
    payload = json.loads(
        b"".join(
            stream_image_json(
                generation,
                ImageTransactions(
                    client_transaction_id=13,
                    server_transaction_id=13,
                ),
            )
        )
    )
    assert len(payload["Value"]) == 96
    assert len(payload["Value"][0]) == 64
    for x, y in ((0, 0), (95, 0), (0, 63), (37, 29)):
        assert payload["Value"][x][y] == int(frame.pixels[y, x])

    handle = real_camera._handle
    assert handle is not None
    rotate = handle.get_Option(hardware_session.sdk.TOUPCAM_OPTION_ROTATE)
    hflip = bool(handle.get_HFlip())
    vflip = bool(handle.get_VFlip())
    hardware_evidence.record(
        "orientation",
        native_storage="pixels[y,x]",
        alpaca_order="Value[x][y]",
        width=96,
        height=64,
        rotate=rotate,
        hflip=hflip,
        vflip=vflip,
        physical_scene_orientation="not-calibrated-without-a-known-optical-target",
    )
